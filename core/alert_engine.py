import asyncio
import os
import aiohttp
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import List, Tuple, Optional
from dotenv import load_dotenv

load_dotenv()

@dataclass
class CopySignal:
    source: str = "copy_engine"
    wallet: str = ""
    market_id: str = ""
    direction: str = ""
    raw_size: float = 0.0
    raw_price: float = 0.0
    timestamp: float = 0.0
    age_seconds: float = 0.0

class AlertEngine:
    def __init__(self, scanner=None, copy_engine=None, signal_engine=None, paper_trader=None, order_executor=None, risk_manager=None):
        self.scanner = scanner
        self.copy_engine = copy_engine
        self.signal_engine = signal_engine
        self.paper_trader = paper_trader
        self.order_executor = order_executor
        self.risk_manager = risk_manager
        self.balance = 500.0  # Default or fetched from balance provider
        
    def generate_windows(self, hours_ahead: int = 24) -> List[datetime]:
        """
        Pre-compute all 5-minute window open times for the next N hours.
        Windows are aligned to UTC 5-minute boundaries (matching slug timestamps).
        Returns list of UTC datetime objects.
        """
        now_ts = int(time.time())
        # Start from the next 5-min boundary
        first_window = ((now_ts // 300) + 1) * 300
        num_windows  = hours_ahead * 12  # 12 windows per hour

        return [
            datetime.fromtimestamp(first_window + (i * 300), tz=timezone.utc)
            for i in range(num_windows)
        ]

    def compute_ev(
        self,
        win_rate: float,      # copy wallet's historical win rate (e.g. 0.84)
        market_price: float,  # current YES price from Polymarket CLOB (e.g. 0.52)
    ) -> float:
        """
        Returns expected value per $1 risked.
        Positive = trade has edge. Negative = skip.
        """
        payout   = 1.0 - market_price
        ev       = win_rate * payout - (1 - win_rate) * market_price
        return round(ev, 4)

    def compute_kelly_size(
        self,
        win_rate: float,
        market_price: float,
        current_balance: float,
        kelly_fraction: float = 0.5,
    ) -> float:
        """
        Returns position size in USDC using half-Kelly formula.
        Always capped at MAX_POSITION_PCT of balance.
        """
        if market_price <= 0 or market_price >= 1:
            return 0.0
            
        payout      = 1.0 - market_price
        odds_ratio  = payout / market_price
        
        if odds_ratio == 0:
            return 0.0
            
        kelly_full  = win_rate - (1 - win_rate) / odds_ratio
        kelly_full  = max(kelly_full, 0.0)   # never negative
        kelly_sized = kelly_full * kelly_fraction * current_balance
        
        max_pos_pct = float(os.getenv('MAX_POSITION_PCT', 0.05))
        max_size    = current_balance * max_pos_pct
        return round(min(kelly_sized, max_size), 2)

    def should_trade(
        self,
        copy_signal: Optional[CopySignal],
        ev: float,
        win_rate: float,
    ) -> Tuple[bool, str]:
        """
        Returns (trade=True/False, reason_string).
        All conditions must pass.
        """
        if copy_signal is None:
            return False, "no copy signal — copy engine idle"
            
        min_win_rate = float(os.getenv('MIN_WIN_RATE', 0.80))
        if win_rate < min_win_rate:
            return False, f"win_rate {win_rate:.1%} below {min_win_rate:.0%} threshold"
            
        ev_min_threshold = float(os.getenv('EV_MIN_THRESHOLD', 0.05))
        if ev < ev_min_threshold:
            return False, f"EV {ev:.3f} below minimum threshold"
            
        if copy_signal.age_seconds > 90:
            return False, f"copy signal stale ({copy_signal.age_seconds:.0f}s)"
            
        return True, "all checks passed"

    async def send_telegram_alert(self, payload: dict) -> None:
        """
        Sends a rich Telegram notification for a BTC 5-minute window prediction.
        Never raises — logs and continues if Telegram is unreachable.
        """
        bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
        chat_id   = os.getenv('TELEGRAM_CHAT_ID')
        if not bot_token or not chat_id:
            logging.warning("Telegram not configured. Skipping alert.")
            return

        direction   = payload['direction']           # "UP" or "DOWN"
        confidence  = payload.get('confidence', 0.0)
        market_up   = payload.get('market_price', 0.5)  # CLOB Up-token price
        btc_open    = payload.get('btc_open', 0.0)
        btc_spot    = payload.get('btc_spot', 0.0)
        pct_change  = payload.get('pct_change', 0.0)
        edge        = payload.get('edge', 0.0)
        slug        = payload.get('market_slug', '')
        window_ts   = payload.get('window_start', 0)
        window_end  = window_ts + 300

        dir_emoji = "📈 UP" if direction == "UP" else "📉 DOWN"
        conf_bar  = "🟩" * round(confidence * 5) + "⬜" * (5 - round(confidence * 5))

        # Format window times in ET (UTC-4)
        def fmt_bt(ts):
            dt = datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(hours=6)
            return dt.strftime("%-I:%M%p").lower()

        window_str = f"{fmt_bt(window_ts)}–{fmt_bt(window_end)} BT"

        traded_status = "✅ ORDER PLACED" if payload.get('traded') else "👁 MONITORING"
        source = payload.get('source', '5m Predictor')

        text = (
            f"<b>⚡ BTC 5-MIN PREDICTION</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 Window: <b>{window_str}</b>\n"
            f"📊 Prediction: <b>{dir_emoji}</b>\n"
            f"🎯 Confidence: {conf_bar} <b>{confidence:.0%}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 BTC Open (price to beat): <code>${btc_open:,.2f}</code>\n"
            f"📡 BTC Now: <code>${btc_spot:,.2f}</code> (<b>{pct_change:+.2f}%</b>)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📉 Market Up price: <code>{market_up:.3f}</code>\n"
            f"📐 Edge: <code>{edge:+.3f}</code>\n"
            f"💵 Trade size: <code>${payload.get('size', 0):.2f} USDC</code>\n"
            f"Status: <b>{traded_status}</b>\n"
            f"Source: {source}\n"
        )
        if slug:
            text += f"\n<a href='https://polymarket.com/event/{slug}'>🔗 Open on Polymarket</a>"

        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": False,
                }) as r:
                    if r.status != 200:
                        logging.error(f"Telegram API error: {await r.text()}")
                    else:
                        logging.info(f"Telegram alert sent: {direction} | conf={confidence:.0%}")
        except Exception as e:
            logging.error(f"Failed to send Telegram alert: {e}")

    async def send_result_alert(self, condition_id: str, slug: str, predicted: str,
                                  won: bool, btc_open: float, btc_close: float,
                                  pnl: float) -> None:
        """Send a resolution result notification."""
        bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
        chat_id   = os.getenv('TELEGRAM_CHAT_ID')
        if not bot_token or not chat_id:
            return

        result_emoji = "🏆 CORRECT" if won else "❌ WRONG"
        pct = (btc_close - btc_open) / btc_open * 100 if btc_open else 0
        actual = "📈 UP" if btc_close >= btc_open else "📉 DOWN"
        pred_str = "📈 UP" if predicted == "UP" else "📉 DOWN"
        pnl_str  = f"{'+' if pnl >= 0 else ''}{pnl:.2f} USDC"

        text = (
            f"<b>RESULT: {result_emoji}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Predicted: <b>{pred_str}</b>  →  Actual: <b>{actual}</b>\n"
            f"Open: <code>${btc_open:,.2f}</code>  Close: <code>${btc_close:,.2f}</code> "
            f"(<b>{pct:+.2f}%</b>)\n"
            f"PnL: <b>{pnl_str}</b>\n"
        )
        if slug:
            text += f"\n<a href='https://polymarket.com/event/{slug}'>🔗 View market</a>"

        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        try:
            async with aiohttp.ClientSession() as s:
                await s.post(url, json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                })
        except Exception as e:
            logging.error(f"Failed to send result alert: {e}")

    async def run_window(self, window_time: datetime) -> None:
        """
        Orchestrates one complete 5-minute window cycle, tightly coupled to
        the btc-updown-5m-* market series.

        Timeline inside each 5-minute window:
          T-45 s  → scan: fetch market prices and build prediction
          T-15 s  → entry: send Telegram alert, optionally place trade
          T+310 s → resolve: fetch actual result, send result alert
        """
        scan_lead  = int(os.getenv('SCAN_LEAD_SECONDS', 45))
        entry_lead = int(os.getenv('ENTRY_LEAD_SECONDS', 15))
        auto_trade = os.getenv('AUTO_TRADE_ENABLED', 'true').lower() == 'true'
        alert_only = os.getenv('ALERT_ONLY_MODE', 'false').lower() == 'true'
        trading_mode = os.getenv('TRADING_MODE', 'paper')

    async def run_window(self, window_time: datetime) -> None:
        """
        Handles paper/live order placement for a window.
        NOTE: Telegram alerts are sent directly by signal_engine._window_tracker_loop
        30 s before each window opens — this method handles trade execution only.
        """
        auto_trade   = os.getenv('AUTO_TRADE_ENABLED', 'true').lower() == 'true'
        alert_only   = os.getenv('ALERT_ONLY_MODE', 'false').lower() == 'true'
        trading_mode = os.getenv('TRADING_MODE', 'paper')

        # Sleep until 10 s before window opens (time to place order)
        entry_time = window_time - timedelta(seconds=10)
        wait = (entry_time - datetime.now(timezone.utc)).total_seconds()
        if wait > 0:
            await asyncio.sleep(wait)

        if not auto_trade or alert_only:
            return

        # Pull the latest signal from signal_engine queue
        signal = None
        if self.signal_engine:
            try:
                signal = self.signal_engine.signal_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass

        if signal is None:
            return

        window_ts = int(window_time.timestamp())
        slug = f"btc-updown-5m-{window_ts}"
        market_price = signal.market_price
        size = self.compute_kelly_size(signal.confidence, market_price, self.balance)

        if size <= 0:
            return

        copy_signal = self.copy_engine.get_latest_signal() if self.copy_engine else None

        if trading_mode == 'paper' and self.paper_trader and copy_signal:
            self.paper_trader.execute(copy_signal, size, market_price)
        elif trading_mode == 'live' and self.order_executor and copy_signal:
            await self.order_executor.submit(copy_signal, size, market_price)

    async def fetch_5m_market(self, slug: str) -> Optional[dict]:
        """
        Fetch market data for a btc-updown-5m slug from CLOB.
        Returns dict with condition_id, up_price, down_price, accepting_orders.
        """
        trades_url = "https://data-api.polymarket.com/trades"
        clob_base  = os.getenv("POLYMARKET_API_BASE", "https://clob.polymarket.com")
        headers = {"User-Agent": "Mozilla/5.0"}
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(f"{trades_url}?limit=200") as resp:
                    if resp.status != 200:
                        return None
                    trades = await resp.json()
                cid = next((t["conditionId"] for t in trades if t.get("slug") == slug), None)
                if not cid:
                    return None
                async with session.get(f"{clob_base}/markets/{cid}") as mresp:
                    if mresp.status != 200:
                        return None
                    m = await mresp.json()
                tokens = m.get("tokens", [])
                return {
                    "condition_id":    cid,
                    "question":        m.get("question", slug),
                    "up_price":        next((float(t["price"]) for t in tokens if t["outcome"] == "Up"), 0.5),
                    "down_price":      next((float(t["price"]) for t in tokens if t["outcome"] == "Down"), 0.5),
                    "accepting_orders": m.get("accepting_orders", False),
                }
        except Exception as e:
            logging.error(f"fetch_5m_market error for {slug}: {e}")
            return None

    async def fetch_resolution(self, condition_id: str, predicted: str,
                                btc_close: float) -> Tuple[str, bool]:
        """
        Resolve the market outcome by checking the winning token on CLOB.
        Returns (actual_direction, prediction_correct).
        """
        if not condition_id:
            # Fallback: derive from spot price vs open
            return predicted, False

        clob_base = os.getenv("POLYMARKET_API_BASE", "https://clob.polymarket.com")
        headers = {"User-Agent": "Mozilla/5.0"}
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(f"{clob_base}/markets/{condition_id}") as resp:
                    if resp.status != 200:
                        return predicted, False
                    m = await resp.json()
            tokens = m.get("tokens", [])
            winner_tok = next((t for t in tokens if t.get("winner")), None)
            if winner_tok:
                actual = "UP" if winner_tok["outcome"] == "Up" else "DOWN"
                return actual, actual == predicted
        except Exception as e:
            logging.error(f"fetch_resolution error: {e}")
        return predicted, False

    async def fetch_market_price(self, market_id: str) -> float:
        """Fetch current Up-token price from CLOB for a given conditionId."""
        clob_base = os.getenv("POLYMARKET_API_BASE", "https://clob.polymarket.com")
        headers = {"User-Agent": "Mozilla/5.0"}
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(f"{clob_base}/markets/{market_id}") as resp:
                    if resp.status != 200:
                        return 0.5
                    m = await resp.json()
            tokens = m.get("tokens", [])
            return next((float(t["price"]) for t in tokens if t["outcome"] == "Up"), 0.5)
        except Exception:
            return 0.5
