"""
CopyEngine — watches the top high-win-rate wallet on Polymarket's Data API
and emits a CopySignal whenever that wallet places a fresh BTC 5-minute trade.

Data API endpoint used:
  GET https://data-api.polymarket.com/trades?user=<proxyWallet>&limit=20
  (public, no auth required)

Fields in each trade record:
  proxyWallet, conditionId, slug, title, side (BUY/SELL), size, price,
  usdcSize, timestamp (Unix seconds as string or float)
"""
import asyncio
import os
import re
import logging
import aiohttp
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional, Dict, List


@dataclass
class CopySignal:
    source: str = "copy_engine"
    wallet: str = ""
    market_id: str = ""          # conditionId
    market_slug: str = ""        # e.g. btc-updown-5m-1773901500
    market_question: str = ""
    direction: str = ""          # "UP" or "DOWN"  (mapped from BUY outcome token)
    outcome: str = ""            # raw outcome name from trade (e.g. "Up", "Down")
    raw_size: float = 0.0        # USDC size
    raw_price: float = 0.0       # price they paid (0–1)
    timestamp: float = 0.0
    age_seconds: float = 0.0


class CopyEngine:
    """
    Polls the top wallet from WalletScanner every POLL_INTERVAL seconds.
    When a new BTC 5-minute trade appears (BUY side), emits a CopySignal.
    Does NOT place any orders — notification only.
    """

    DATA_API = "https://data-api.polymarket.com"
    POLL_INTERVAL = 12   # seconds between polls

    def __init__(self, scanner=None):
        self.scanner = scanner
        # Track last trade seen per wallet so we don't re-fire on old trades
        self._last_trade_ts: Dict[str, float] = {}
        self.signal_queue: asyncio.Queue = asyncio.Queue()
        self.latest_signal: Optional[CopySignal] = None
        self._headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }

    # ── Public accessors ───────────────────────────────────────────────────────

    def get_latest_signal(self) -> Optional[CopySignal]:
        if self.latest_signal:
            now = time.time()
            self.latest_signal.age_seconds = now - self.latest_signal.timestamp
        return self.latest_signal

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run_loop(self):
        """
        Main background loop.
        Watches all leaderboard wallets (top 3), not just the single top one,
        so we catch more coverage even if the top wallet is inactive.
        """
        logging.info("CopyEngine started — watching top wallets for BTC 5-min trades.")
        while True:
            wallets = self._get_target_wallets()
            if not wallets:
                logging.debug("CopyEngine: no qualified wallets yet, waiting 60s…")
                await asyncio.sleep(60)
                continue

            for wallet in wallets:
                try:
                    await self._poll_wallet(wallet)
                except Exception as e:
                    logging.error(f"CopyEngine poll error for {wallet[:10]}…: {e}")

            await asyncio.sleep(self.POLL_INTERVAL)

    # ── Polling ───────────────────────────────────────────────────────────────

    def _get_target_wallets(self) -> List[str]:
        """Return up to 3 top-ranked wallets from the scanner leaderboard."""
        if not self.scanner:
            return []
        lb = self.scanner.get_leaderboard()
        return [entry["address"] for entry in lb[:3]]

    async def _poll_wallet(self, wallet: str):
        """
        Fetch the 20 most recent trades for `wallet`.
        Only process BTC 5-min markets (slug matches btc-updown-5m-*).
        Only process BUY trades newer than the last trade we saw for this wallet.
        """
        url = f"{self.DATA_API}/trades?user={wallet}&limit=20"
        try:
            async with aiohttp.ClientSession(headers=self._headers) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logging.debug(f"CopyEngine: trades API {resp.status} for {wallet[:10]}…")
                        return
                    trades = await resp.json()
        except Exception as e:
            logging.error(f"CopyEngine fetch error: {e}")
            return

        if not trades:
            return

        last_seen = self._last_trade_ts.get(wallet, 0.0)
        new_trades = []

        for t in trades:
            ts = float(t.get("timestamp") or t.get("createdAt") or 0)
            slug = t.get("slug", "") or t.get("market_slug", "")
            side = (t.get("side") or t.get("traderSide") or "").upper()

            # Only BUY trades on btc-updown-5m-* markets
            if not re.match(r"btc-updown-5m-\d+", slug):
                continue
            if side != "BUY":
                continue
            if ts <= last_seen:
                continue

            new_trades.append((ts, t))

        if not new_trades:
            return

        # Update watermark
        self._last_trade_ts[wallet] = max(ts for ts, _ in new_trades)

        # Sort ascending so we emit oldest first
        for ts, t in sorted(new_trades, key=lambda x: x[0]):
            await self._emit_signal(t, wallet, ts)

    async def _emit_signal(self, trade: Dict, wallet: str, ts: float):
        """
        Convert a raw trade record into a CopySignal and put it on the queue.
        Also sends a Telegram notification.
        """
        now = time.time()
        age = now - ts
        if age > 270:
            # Stale — older than one full window, skip
            logging.debug(f"CopyEngine: skipping stale trade ({age:.0f}s old) from {wallet[:10]}…")
            return

        slug     = trade.get("slug", "") or trade.get("market_slug", "")
        cid      = trade.get("conditionId", "") or trade.get("condition_id", "")
        title    = trade.get("title", slug)
        outcome  = trade.get("outcome", "")         # "Up" or "Down"
        price    = float(trade.get("price") or 0.5)
        size     = float(trade.get("usdcSize") or trade.get("size") or 0.0)

        # Determine direction from outcome token name
        if outcome.lower() == "up":
            direction = "UP"
        elif outcome.lower() == "down":
            direction = "DOWN"
        else:
            # Fallback: infer from price — if they paid >0.5 for a token, likely "Up"
            direction = "UP" if price >= 0.5 else "DOWN"

        signal = CopySignal(
            wallet=wallet,
            market_id=cid,
            market_slug=slug,
            market_question=title,
            direction=direction,
            outcome=outcome,
            raw_size=size,
            raw_price=price,
            timestamp=ts,
            age_seconds=age,
        )
        self.latest_signal = signal
        await self.signal_queue.put(signal)

        win_rate = self._get_wallet_win_rate(wallet)
        logging.info(
            f"📋 CopySignal: {direction} on {slug} | "
            f"wallet={wallet[:10]}… | win_rate={win_rate:.0%} | "
            f"size=${size:.2f} @ {price:.3f} | age={age:.0f}s"
        )
        await self._send_copy_telegram(signal, win_rate)

    # ── Telegram ──────────────────────────────────────────────────────────────

    async def _send_copy_telegram(self, signal: CopySignal, win_rate: float):
        """
        Send a Telegram alert showing what the high-win-rate wallet just bet.
        This is the primary notification — pure copy-signal, no model prediction.
        """
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id   = os.getenv("TELEGRAM_CHAT_ID")
        if not bot_token or not chat_id:
            return

        direction  = signal.direction
        dir_emoji  = "📈 UP" if direction == "UP" else "📉 DOWN"
        slug       = signal.market_slug
        wallet_short = signal.wallet[:6] + "…" + signal.wallet[-4:]

        # Parse window time from slug
        try:
            window_ts = int(slug.split("-")[-1])
            window_end = window_ts + 300
            def fmt_bt(ts):
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                from datetime import timedelta
                dt_bt = dt + timedelta(hours=6)
                return dt_bt.strftime("%-I:%M%p").lower()
            window_str = f"{fmt_bt(window_ts)}–{fmt_bt(window_end)} BT"
        except Exception:
            window_str = slug

        text = (
            f"<b>🐋 WHALE COPY SIGNAL</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 Window: <b>{window_str}</b>\n"
            f"📊 They bet: <b>{dir_emoji}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏆 Wallet win rate: <b>{win_rate:.0%}</b>\n"
            f"💵 Their size: <code>${signal.raw_size:.2f} USDC</code>\n"
            f"💰 Price paid: <code>{signal.raw_price:.3f}</code>\n"
            f"⏱ Signal age: <code>{signal.age_seconds:.0f}s</code>\n"
            f"👛 Wallet: <code>{wallet_short}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ <b>ACTION: Go to Polymarket and bet {dir_emoji}</b>\n"
        )
        if slug:
            text += f"\n<a href='https://polymarket.com/event/{slug}'>🔗 Open market on Polymarket</a>"

        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json={
                    "chat_id":   chat_id,
                    "text":      text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": False,
                }) as r:
                    resp = await r.json()
                    if r.status == 200:
                        logging.info(f"✅ Copy Telegram sent: {direction} | {slug}")
                    else:
                        logging.error(f"Telegram error (copy): {resp}")
        except Exception as e:
            logging.error(f"Failed to send copy Telegram: {e}")

    def _get_wallet_win_rate(self, wallet: str) -> float:
        if self.scanner:
            lb = self.scanner.get_leaderboard()
            for entry in lb:
                if entry["address"] == wallet:
                    return entry["win_rate"]
        return 0.0
