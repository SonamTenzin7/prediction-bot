import asyncio
import os
import json
import logging
import websockets
import aiohttp
import re
import numpy as np
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict
from core.signal_logger import log_prediction, log_result

@dataclass
class BTCSignal:
    source: str = "signal_engine"
    market_id: str = ""
    market_question: str = ""
    market_slug: str = ""
    direction: str = ""         # "UP" or "DOWN"
    confidence: float = 0.0     # 0.0 - 1.0
    fair_value: float = 0.0     # model probability for the predicted direction
    market_price: float = 0.0   # current CLOB Up-token price
    edge: float = 0.0           # fair_value - market_price (positive = value bet)
    btc_open: float = 0.0       # BTC price at window open (price-to-beat)
    btc_spot: float = 0.0       # BTC/USDT at signal time
    pct_change: float = 0.0     # (btc_spot - btc_open) / btc_open * 100
    window_start: int = 0       # unix timestamp of window open
    window_end: int = 0         # unix timestamp of window close
    timestamp: float = 0.0

# -- 5-minute window helpers --------------------------------------------------

def current_window_start() -> int:
    import time
    return (int(time.time()) // 300) * 300

def next_window_start() -> int:
    return current_window_start() + 300

def window_slug(ts: int) -> str:
    return f"btc-updown-5m-{ts}"


class SignalEngine:
    def __init__(self, copy_engine=None):
        self.price_history = deque(maxlen=500)          # raw ticks (kept for btc_spot)
        self.candles: List[Dict] = []                   # 1-min OHLCV candles (up to 120)
        self._candle_max = 120                          # 2 hours of 1-min candles
        self._current_candle: Optional[Dict] = None     # candle being built from ticks
        self._candle_interval = 60                      # seconds per candle
        self.binance_ws_url = os.getenv("BINANCE_WS", "wss://stream.binance.com:9443/ws/btcusdt@trade")
        self.signal_queue = asyncio.Queue()
        self.latest_price = 0.0
        self.active_markets = {}
        self.cooldowns = {}
        self.copy_engine = copy_engine

        self._window_open_price: float = 0.0
        self._window_start_ts: int = 0
        self._last_signal_window: int = 0

    async def run_loop(self):
        asyncio.create_task(self.market_discovery_loop())
        asyncio.create_task(self._window_tracker_loop())

        # Seed candle history from Binance REST API so we have signal immediately
        await self._seed_candles_from_rest()

        while True:
            try:
                async with websockets.connect(self.binance_ws_url) as ws:
                    logging.info("Connected to Binance WebSocket")
                    while True:
                        msg = await ws.recv()
                        data = json.loads(msg)
                        price = float(data['p'])
                        ts    = float(data['T']) / 1000.0  # trade timestamp (seconds)
                        self.latest_price = price
                        self.price_history.append(price)
                        self._ingest_tick(price, ts)
                        await self.process_price_tick()
            except Exception as e:
                logging.error(f"Binance WS error: {e}. Retrying in 5 seconds...")
                await asyncio.sleep(5)

    # -- Candle aggregation ----------------------------------------------------

    async def _seed_candles_from_rest(self):
        """Fetch the last 120 one-minute candles from Binance REST so the model
        has meaningful history from the very first prediction."""
        url = "https://api.binance.com/api/v3/klines"
        params = {"symbol": "BTCUSDT", "interval": "1m", "limit": self._candle_max}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params) as resp:
                    if resp.status != 200:
                        logging.warning(f"Binance klines API error: {resp.status}")
                        return
                    raw = await resp.json()
            for k in raw:
                candle = {
                    "open":   float(k[1]),
                    "high":   float(k[2]),
                    "low":    float(k[3]),
                    "close":  float(k[4]),
                    "volume": float(k[5]),
                    "ts":     int(k[0]) // 1000,  # open time in seconds
                }
                self.candles.append(candle)
            # Trim to max
            if len(self.candles) > self._candle_max:
                self.candles = self.candles[-self._candle_max:]
            logging.info(f"Seeded {len(self.candles)} 1-min candles from Binance REST")
        except Exception as e:
            logging.error(f"Failed to seed candles: {e}")

    def _ingest_tick(self, price: float, ts: float):
        """Aggregate raw ticks into 1-minute OHLCV candles in real time."""
        candle_ts = int(ts) // self._candle_interval * self._candle_interval

        if self._current_candle is None or self._current_candle["ts"] != candle_ts:
            # Finalize the previous candle and start a new one
            if self._current_candle is not None:
                self.candles.append(self._current_candle)
                if len(self.candles) > self._candle_max:
                    self.candles = self.candles[-self._candle_max:]
            self._current_candle = {
                "open": price, "high": price, "low": price,
                "close": price, "volume": 0.0, "ts": candle_ts,
            }
        else:
            c = self._current_candle
            c["high"]  = max(c["high"], price)
            c["low"]   = min(c["low"], price)
            c["close"] = price

    # -- Window tracking -------------------------------------------------------

    async def _window_tracker_loop(self):
        """
        Drives every 5-minute window cycle:
          1. Sleep until ALERT_LEAD_SECONDS before window open.
          2. Predict -> send Telegram alert.
          3. Wait for window open -> snapshot btc_open.
          4. Wait for window close+8s -> send result.
          5. Advance next_ws by +300 and repeat.

        FIX: next_ws is carried forward explicitly (next_ws += 300 at the end)
        so a window is NEVER repeated even if steps 2-4 overrun by a few seconds.
        """
        import time as _time

        alert_lead = int(os.getenv("ALERT_LEAD_SECONDS", 30))

        # Compute the first upcoming window at startup.
        # Always recalculate here so startup delays don't leave us on a stale window.
        def _next_future_window() -> int:
            t = _time.time()
            ws = ((int(t) // 300) + 1) * 300
            # If we're already past the alert firing point for that window, skip it
            if ws - alert_lead <= t:
                ws += 300
            return ws

        next_ws = _next_future_window()
        logging.info(f"Window tracker starting — first window: {window_slug(next_ws)}")

        while True:
            # Step 1: sleep until alert_lead seconds before window opens
            sleep_until = next_ws - alert_lead
            wait = sleep_until - _time.time()
            if wait > 0:
                await asyncio.sleep(wait)

            if self.latest_price == 0:
                logging.warning("No BTC price yet -- skipping window prediction.")
                next_ws += 300
                await asyncio.sleep(max(0, next_ws - alert_lead - _time.time()))
                continue

            slug = window_slug(next_ws)
            logging.info(f"Preparing prediction for {slug} (opens in ~{max(0, int(next_ws - _time.time()))}s)")

            # Step 2: predict and alert
            await self._predict_and_alert(next_ws, slug)

            # Step 3: wait for window open, snapshot price
            wait_open = next_ws - _time.time()
            if wait_open > 0:
                await asyncio.sleep(wait_open)

            self._window_start_ts   = next_ws
            self._window_open_price = self.latest_price
            logging.info(f"Window {slug} OPENED | price-to-beat: ${self._window_open_price:,.2f}")

            # Step 4: schedule result check as a background task
            # This fires after the window closes (+8s settlement) WITHOUT
            # blocking the window tracker loop — so the next window is never skipped.
            result_open = self._window_open_price
            result_ws   = next_ws
            result_slug = slug
            asyncio.create_task(self._delayed_result(result_ws, result_slug, result_open))

            # Advance to the next window -- never re-run the same window.
            # Also skip any windows that are already in the past (e.g. if
            # result processing ran long and crossed a boundary).
            next_ws += 300
            now = _time.time()
            if next_ws <= now:
                # Fast-forward to the actual next future window on the UTC grid
                skipped = int((now - next_ws) // 300) + 1
                next_ws += skipped * 300
                logging.warning(
                    f"Skipped {skipped} stale window(s) — "
                    f"jumping to {window_slug(next_ws)}"
                )

    async def _predict_and_alert(self, window_ts: int, slug: str):
        """
        Build a prediction for the upcoming 5-minute window and send Telegram.
        Priority: copy signal from top wallet -> technical model fallback.
        """
        import time as _time
        spot = self.latest_price

        # Check for a fresh copy signal on this specific window
        copy_signal = None
        copy_win_rate = 0.0
        if self.copy_engine:
            latest = self.copy_engine.get_latest_signal()
            if latest and latest.market_slug == slug and latest.age_seconds < 270:
                copy_signal = latest
                copy_win_rate = self.copy_engine._get_wallet_win_rate(latest.wallet)
                logging.info(
                    f"[Predict] Using COPY signal: {latest.direction} from "
                    f"{latest.wallet[:10]}... (win_rate={copy_win_rate:.0%})"
                )

        # Technical model — uses 1-minute candle closes for meaningful signals
        # Include the in-progress candle for the most up-to-date reading
        all_candles = list(self.candles)
        if self._current_candle is not None:
            all_candles.append(self._current_candle)
        closes  = [c["close"] for c in all_candles]
        volumes = [c.get("volume", 0) for c in all_candles]
        n = len(closes)

        ema_z    = 0.0
        mom_z    = 0.0
        rsi      = 50.0
        rev_z    = 0.0   # mean-reversion signal
        vwap_z   = 0.0

        if n >= 26:
            price_std = float(np.std(closes[-30:])) if n >= 30 else float(np.std(closes))
            if price_std == 0:
                price_std = spot * 0.0001

            ema_fast = self.compute_ema(closes, 12)
            ema_slow = self.compute_ema(closes, 26)
            ema_z    = (ema_fast - ema_slow) / price_std

            # Momentum: last 3 candles vs prior 3 (short look-back for 5-min windows)
            if n >= 6:
                recent_mean = float(np.mean(closes[-3:]))
                prior_mean  = float(np.mean(closes[-6:-3]))
                mom_z = (recent_mean - prior_mean) / price_std

            # RSI on candle closes
            rsi   = self._compute_rsi(closes, period=14)
            rsi_z = (rsi - 50.0) / 15.0

            # Mean-reversion: price deviation from 20-candle SMA
            # When price is stretched far from its mean, it tends to snap back
            if n >= 20:
                sma20   = float(np.mean(closes[-20:]))
                rev_z   = -((spot - sma20) / price_std)  # negative: stretched UP → predict DOWN

            # VWAP deviation
            if n >= 10:
                recent_c = all_candles[-10:]
                total_vol = sum(c.get("volume", 0) for c in recent_c)
                if total_vol > 0:
                    vwap = sum(c["close"] * c.get("volume", 0) for c in recent_c) / total_vol
                    vwap_z = -((spot - vwap) / price_std)  # negative: above VWAP → lean DOWN

            # Regime detection: is the market trending or choppy?
            # Use 5-candle range relative to std to determine signal weights
            if n >= 5:
                recent_range = max(closes[-5:]) - min(closes[-5:])
                is_trending  = recent_range > price_std * 0.5
            else:
                is_trending = False

            if is_trending:
                # Trending: weight momentum and EMA more, reversion less
                combined_z = (ema_z * 0.35) + (mom_z * 0.35) + (rsi_z * 0.15) + (rev_z * 0.10) + (vwap_z * 0.05)
            else:
                # Choppy: weight mean-reversion more, momentum less
                combined_z = (rev_z * 0.35) + (vwap_z * 0.25) + (rsi_z * 0.25) + (ema_z * 0.10) + (mom_z * 0.05)

        elif n >= 10:
            price_std = float(np.std(closes)) if n > 1 else spot * 0.0001
            if price_std == 0:
                price_std = spot * 0.0001
            ema_fast = self.compute_ema(closes, 5)
            ema_slow = self.compute_ema(closes, 10)
            ema_z    = (ema_fast - ema_slow) / price_std
            rsi      = self._compute_rsi(closes, period=min(14, n - 1))
            rsi_z    = (rsi - 50.0) / 15.0
            combined_z = (ema_z * 0.50) + (rsi_z * 0.50)
        else:
            combined_z = 0.0

        sigmoid_k     = float(os.getenv("SIGMOID_K", 1.5))
        model_prob_up = float(1 / (1 + np.exp(-combined_z * sigmoid_k)))

        if model_prob_up >= 0.5:
            model_direction  = "UP"
            model_fair_value = model_prob_up
        else:
            model_direction  = "DOWN"
            model_fair_value = 1.0 - model_prob_up

        # Confidence: distance from coin-flip, scaled so a strong signal → 80-95%
        model_confidence = min(abs(model_fair_value - 0.5) * 2.5, 1.0)

        # Fetch market from CLOB
        market_info       = await self._fetch_5m_market(slug)
        market_up_price   = market_info.get("up_price",   0.5)  if market_info else 0.5
        market_down_price = 1.0 - market_up_price
        condition_id      = market_info.get("condition_id", "") if market_info else ""
        accepting         = market_info.get("accepting_orders", False) if market_info else False

        # Decide final direction
        if copy_signal:
            direction  = copy_signal.direction
            fair_value = model_fair_value
            confidence = max(copy_win_rate, model_confidence)
            source     = f"copy:{copy_signal.wallet[:6]}... ({copy_win_rate:.0%} WR)"
        else:
            direction  = model_direction
            fair_value = model_fair_value
            confidence = model_confidence
            source     = "technical model"

        market_price_dir = market_up_price if direction == "UP" else market_down_price
        edge             = fair_value - market_price_dir

        signal = BTCSignal(
            source=source,
            market_id=condition_id,
            market_question=market_info.get("question", slug) if market_info else slug,
            market_slug=slug,
            direction=direction,
            confidence=confidence,
            fair_value=fair_value,
            market_price=market_price_dir,
            edge=edge,
            btc_open=spot,
            btc_spot=spot,
            pct_change=0.0,
            window_start=window_ts,
            window_end=window_ts + 300,
            timestamp=_time.time(),
        )
        while not self.signal_queue.empty():
            try:
                self.signal_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        await self.signal_queue.put(signal)

        logging.info(
            f"[Predict] {direction} | conf={confidence:.0%} | source={source} | "
            f"ema_z={ema_z:.3f} mom_z={mom_z:.3f} rev_z={rev_z:.3f} "
            f"rsi={rsi:.1f} combined_z={combined_z:.3f} "
            f"trending={is_trending if n>=26 else 'n/a'} candles={n} | {slug}"
        )

        # Persist prediction so Streamlit dashboard can show it
        log_prediction(
            slug=slug,
            window_start=window_ts,
            window_end=window_ts + 300,
            direction=direction,
            confidence=confidence,
            fair_value=fair_value,
            source=source,
            btc_spot=spot,
            market_price=market_price_dir,
            edge=edge,
        )

        await self._telegram_alert(signal, accepting, copy_signal=copy_signal, copy_win_rate=copy_win_rate)

    def _compute_rsi(self, prices: List[float], period: int = 14) -> float:
        if len(prices) < period + 1:
            return 50.0
        deltas   = [prices[i] - prices[i - 1] for i in range(-period, 0)]
        gains    = [d for d in deltas if d > 0]
        losses   = [-d for d in deltas if d < 0]
        avg_gain = sum(gains) / period if gains else 0.0
        avg_loss = sum(losses) / period if losses else 0.0
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    async def _delayed_result(self, window_ts: int, slug: str, btc_open: float):
        """Background task: wait for window close + 8s, then log the result."""
        import time as _time
        window_close = window_ts + 300
        wait = window_close - _time.time() + 8
        if wait > 0:
            await asyncio.sleep(wait)
        btc_close = self.latest_price
        await self._send_result(window_ts, slug, btc_open, btc_close)

    async def _send_result(self, window_ts: int, slug: str,
                           btc_open: float = 0.0, btc_close: float = 0.0):
        """
        Determine the window result and log it.
        Primary: BTC price comparison (matches Chainlink resolution exactly).
        Secondary: CLOB winner token (single attempt, non-blocking).
        """
        # Use passed-in prices; fall back to instance vars if not provided
        if btc_open == 0.0:
            btc_open = self._window_open_price
        if btc_close == 0.0:
            btc_close = self.latest_price

        # Primary resolution: price comparison (same as Chainlink oracle)
        if btc_open > 0:
            actual = "UP" if btc_close >= btc_open else "DOWN"
            logging.info(
                f"[Result] {slug}: open=${btc_open:,.2f} close=${btc_close:,.2f} → {actual}"
            )
        else:
            # Try one CLOB lookup as fallback
            actual = None
            market_info = await self._fetch_5m_market(slug)
            if market_info:
                cid = market_info.get("condition_id", "")
                if cid:
                    clob_base = os.getenv("POLYMARKET_API_BASE", "https://clob.polymarket.com")
                    try:
                        async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as session:
                            async with session.get(f"{clob_base}/markets/{cid}") as resp:
                                if resp.status == 200:
                                    m = await resp.json()
                                    tokens = m.get("tokens", [])
                                    winner = next((t for t in tokens if t.get("winner")), None)
                                    if winner:
                                        actual = "UP" if winner["outcome"] == "Up" else "DOWN"
                    except Exception as e:
                        logging.error(f"[Result] CLOB fallback error for {slug}: {e}")

            if actual is None:
                logging.warning(f"[Result] {slug}: no open price and CLOB unresolved — skipping")
                return

        # Persist result for Streamlit dashboard
        log_result(
            slug=slug,
            window_start=window_ts,
            actual=actual,
            btc_open=btc_open,
            btc_close=btc_close,
        )

        await self._telegram_result(slug, actual, btc_open, btc_close)

    async def _telegram_alert(self, signal: 'BTCSignal', accepting: bool,
                              copy_signal=None, copy_win_rate: float = 0.0):
        import time as _time
        bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
        chat_id   = os.getenv('TELEGRAM_CHAT_ID')
        if not bot_token or not chat_id:
            logging.warning("Telegram not configured -- no alert sent.")
            return

        ws = signal.window_start
        we = signal.window_end

        def fmt_bt_full(ts: int) -> str:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(hours=6)
            return dt.strftime("%I:%M:%S %p").lstrip("0")   # e.g. 2:30:00 PM

        def fmt_bt_short(ts: int) -> str:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(hours=6)
            return dt.strftime("%I:%M %p").lstrip("0")      # e.g. 2:35 PM

        open_time_str  = fmt_bt_full(ws)
        close_time_str = fmt_bt_short(we)

        secs_left = max(0, int(ws - _time.time()))
        mins_left = secs_left // 60
        sec_part  = secs_left % 60
        countdown = f"{mins_left}m {sec_part:02d}s" if mins_left > 0 else f"{sec_part}s"

        direction  = signal.direction
        confidence = signal.confidence
        dir_emoji  = "📈 BUY UP" if direction == "UP" else "📉 BUY DOWN"
        conf_bar   = "🟩" * round(confidence * 5) + "⬜" * (5 - round(confidence * 5))
        status     = "✅ ACCEPTING ORDERS" if accepting else "⚠️ No orders yet"

        if copy_signal:
            wallet_short = copy_signal.wallet[:6] + "..." + copy_signal.wallet[-4:]
            header = "<b>🐋 WHALE COPY -- BTC 5-MIN</b>"
            source_line = (
                f"📋 Source: <b>Wallet copy</b> ({copy_win_rate:.0%} win rate)\n"
                f"👛 Wallet: <code>{wallet_short}</code>\n"
                f"💵 Whale bet: <code>${copy_signal.raw_size:.2f} USDC @ {copy_signal.raw_price:.3f}</code>\n"
            )
        else:
            header = "<b>⚡ BTC 5-MIN -- TECHNICAL SIGNAL</b>"
            source_line = (
                f"📋 Source: <b>Technical model</b> (EMA + Mom + RevMean + RSI)\n"
                f"📊 Candles: <b>{len(self.candles)} × 1-min</b>\n"
            )

        text = (
            f"{header}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🟢 Opens:    <b>{open_time_str} BT</b>\n"
            f"🔴 Closes:   <b>{close_time_str} BT</b>\n"
            f"⏳ Starts in: <b>{countdown}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Prediction: <b>{dir_emoji}</b>\n"
            f"🎯 Confidence: {conf_bar} <b>{confidence:.0%}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{source_line}"
            f"📡 BTC now: <code>${signal.btc_spot:,.2f}</code>\n"
            f"📐 Model edge: <code>{signal.edge:+.3f}</code>\n"
            f"💹 Market Up price: <code>{signal.market_price:.3f}</code>\n"
            f"Market: {status}\n"
        )
        if signal.market_slug:
            text += f"\n<a href='https://polymarket.com/event/{signal.market_slug}'>🔗 Open on Polymarket</a>"

        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": False,
                }) as r:
                    resp = await r.json()
                    if r.status == 200:
                        src = "COPY" if copy_signal else "MODEL"
                        logging.info(
                            f"✅ Telegram alert sent [{src}]: {direction} | "
                            f"conf={confidence:.0%} | opens={open_time_str} BT | {signal.market_slug}"
                        )
                    else:
                        logging.error(f"Telegram error: {resp}")
        except Exception as e:
            logging.error(f"Failed to send Telegram alert: {e}")

    async def _telegram_result(self, slug: str, actual: str, btc_open: float, btc_close: float):
        bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
        chat_id   = os.getenv('TELEGRAM_CHAT_ID')
        if not bot_token or not chat_id:
            return
        pct = (btc_close - btc_open) / btc_open * 100 if btc_open else 0
        actual_emoji = "📈 UP" if actual == "UP" else "📉 DOWN"
        text = (
            f"<b>✅ RESULT: {actual_emoji}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Open: <code>${btc_open:,.2f}</code>  →  Close: <code>${btc_close:,.2f}</code>\n"
            f"Change: <b>{pct:+.2f}%</b>\n"
        )
        if slug:
            text += f"\n<a href='https://polymarket.com/event/{slug}'>🔗 View market</a>"
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        try:
            async with aiohttp.ClientSession() as s:
                await s.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
        except Exception as e:
            logging.error(f"Failed to send result alert: {e}")

    async def _fetch_5m_market(self, slug: str) -> Optional[Dict]:
        trades_url = "https://data-api.polymarket.com/trades"
        clob_base  = os.getenv("POLYMARKET_API_BASE", "https://clob.polymarket.com")
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(f"{trades_url}?limit=200") as resp:
                    if resp.status != 200:
                        return None
                    trades = await resp.json()
                cid = next((t["conditionId"] for t in trades if t.get("slug") == slug), None)
                if not cid:
                    logging.debug(f"No trades found yet for {slug}")
                    return None
                async with session.get(f"{clob_base}/markets/{cid}") as mresp:
                    if mresp.status != 200:
                        return None
                    m = await mresp.json()
                tokens     = m.get("tokens", [])
                up_price   = next((float(t["price"]) for t in tokens if t["outcome"] == "Up"),   0.5)
                down_price = next((float(t["price"]) for t in tokens if t["outcome"] == "Down"), 0.5)
                return {
                    "condition_id":     cid,
                    "question":         m.get("question", slug),
                    "up_price":         up_price,
                    "down_price":       down_price,
                    "accepting_orders": m.get("accepting_orders", False),
                }
        except Exception as e:
            logging.error(f"Failed to fetch 5m market {slug}: {e}")
            return None

    async def market_discovery_loop(self):
        trades_url = "https://data-api.polymarket.com/trades"
        clob_base  = os.getenv("POLYMARKET_API_BASE", "https://clob.polymarket.com")
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        while True:
            try:
                async with aiohttp.ClientSession(headers=headers) as session:
                    async with session.get(f"{trades_url}?limit=500") as resp:
                        if resp.status != 200:
                            logging.error(f"Trades API error: {resp.status}")
                            await asyncio.sleep(60)
                            continue
                        trades = await resp.json()
                    btc_condition_ids = {
                        t["conditionId"]
                        for t in trades
                        if re.search(r"bitcoin|btc", t.get("title", ""), re.I)
                        and t.get("conditionId")
                    }
                    btc_markets = {}
                    for cid in btc_condition_ids:
                        try:
                            async with session.get(f"{clob_base}/markets/{cid}") as mresp:
                                if mresp.status != 200:
                                    continue
                                m = await mresp.json()
                            if not m.get("accepting_orders"):
                                continue
                            tokens    = m.get("tokens", [])
                            mid_price = 0.5
                            for tok in tokens:
                                if tok.get("outcome", "").lower() in ("up", "yes"):
                                    mid_price = float(tok.get("price", 0.5))
                                    break
                            btc_markets[cid] = {
                                "question":     m.get("question", ""),
                                "slug":         m.get("market_slug", cid),
                                "strike_price": self.parse_strike(m.get("question", "")),
                                "mid_price":    mid_price,
                                "tokens":       tokens,
                            }
                        except Exception as e:
                            logging.debug(f"Failed to fetch market {cid}: {e}")
                    self.active_markets = btc_markets
                    logging.info(f"Updated {len(btc_markets)} active BTC markets.")
            except Exception as e:
                logging.error(f"Market discovery error: {e}")
            await asyncio.sleep(60)

    def parse_strike(self, description: str) -> Optional[float]:
        try:
            match = re.search(r'\$(\d{1,3}(?:,\d{3})*(?:\.\d+)?)', description)
            if match:
                return float(match.group(1).replace(',', ''))
        except Exception:
            pass
        return None

    async def process_price_tick(self):
        if len(self.price_history) < 50:
            return
        if not self.active_markets:
            return
        prices    = list(self.price_history)
        ema_fast  = self.compute_ema(prices, 10)
        ema_slow  = self.compute_ema(prices, 50)
        price_std = float(np.std(prices[-60:])) if len(prices) >= 60 else float(np.std(prices))
        for market_id, market_data in list(self.active_markets.items()):
            await self.check_market_signal(market_id, market_data, ema_fast, ema_slow, price_std)

    def compute_ema(self, prices: List[float], window: int) -> float:
        alpha = 2 / (window + 1)
        ema = prices[0]
        for price in prices[1:]:
            ema = (price * alpha) + (ema * (1 - alpha))
        return ema

    async def check_market_signal(self, market_id: str, market_data: Dict,
                                  ema_fast: float, ema_slow: float, price_std: float):
        now = datetime.now(timezone.utc).timestamp()
        if market_id in self.cooldowns and now - self.cooldowns[market_id] < 1800:
            return
        strike_price     = market_data.get('strike_price')
        market_mid_price = market_data.get('mid_price', 0.5)
        edge_threshold   = float(os.getenv('SIGNAL_EDGE_THRESHOLD', 0.045))
        direction        = ""
        if strike_price is not None and price_std > 0:
            sigmoid_k = float(os.getenv('SIGMOID_K', 1.8))
            z_score   = (self.latest_price - strike_price) / price_std
            raw_prob  = 1 / (1 + np.exp(-z_score * sigmoid_k))
            edge      = raw_prob - market_mid_price
            if edge > edge_threshold and ema_fast > ema_slow:
                direction = "YES"
            elif edge < -edge_threshold and ema_fast < ema_slow:
                direction = "NO"
            fair_value = raw_prob
        else:
            ema_diff = (ema_fast - ema_slow) / max(price_std, 1)
            if ema_diff > 0.5 and market_mid_price < (0.5 - edge_threshold):
                direction  = "YES"
                fair_value = 0.5 + edge_threshold
            elif ema_diff < -0.5 and market_mid_price > (0.5 + edge_threshold):
                direction  = "NO"
                fair_value = 0.5 - edge_threshold
            else:
                return
            edge = fair_value - market_mid_price
        if direction:
            signal = BTCSignal(
                market_id=market_id,
                market_question=market_data.get('question', ''),
                direction=direction,
                fair_value=fair_value,
                market_price=market_mid_price,
                edge=edge,
                btc_spot=self.latest_price,
                timestamp=now,
            )
            await self.signal_queue.put(signal)
            self.cooldowns[market_id] = now
            logging.info(f"Signal emitted: {direction} on {market_data.get('slug', market_id)} (Edge: {edge:.3f})")

    async def update_markets(self, markets: Dict):
        self.active_markets = markets

    def get_latest_price(self) -> float:
        return self.latest_price

    def get_window_open_price(self) -> float:
        return self._window_open_price

    def get_current_window_ts(self) -> int:
        return self._window_start_ts
