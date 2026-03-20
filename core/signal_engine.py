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

        Model v5 — built on the three principles from math.md:

        1. EXPECTED VALUE (math.md §1-2):
           Market is almost always priced at exactly 0.500 (stdev=0.011 from 86 windows).
           True base rate from data: DOWN=55.8%, UP=44.2%.
           EV = fair_value - 0.500.  Any model that beats 50% has positive EV here.
           fair_value is computed from the vote-weighted posterior, not an arbitrary formula.

        2. KELLY CRITERION (math.md §3):
           kelly_fraction = (p - q) / (1 - q)  where p=fair_value, q=market_price=0.5
           Simplified at q=0.5: kelly = 2*(p - 0.5).
           We express this as 'confidence' so downstream bet sizing can scale correctly.
           Kelly is only positive when p > 0.5, so SKIP is still valid for p≤0.5.

        3. BAYESIAN UPDATING (math.md §4):
           Start with prior p(DOWN)=0.558, p(UP)=0.442 (from observed base rates).
           Each independent signal updates the posterior via likelihood ratio.
           Signals with observed accuracy A give L = A/(1-A).
           Posterior after k signals = prior * product(L_i) / normalisation.
           This keeps estimates current and prevents indicator over-counting.

        Signal set (each genuinely independent):
          S1: Micro-momentum (3-candle vs prior 3) — momentum continuation
          S2: Volume-weighted candle direction — institutional order flow proxy
          S3: 10-candle trend position — breakout/breakdown context
          S4: RSI extreme — exhaustion / continuation at extremes
        ATR gate: skip when range ≤0.02% of spot (pure noise — zero edge).
        """
        import time as _time
        spot = self.latest_price

        # ── Copy signal (always highest priority) ─────────────────────────────
        copy_signal   = None
        copy_win_rate = 0.0
        if self.copy_engine:
            latest = self.copy_engine.get_latest_signal()
            if latest:
                slug_ts   = int(slug.split("-")[-1])
                signal_ts = int(latest.market_slug.split("-")[-1]) if latest.market_slug else 0
                slug_match = abs(slug_ts - signal_ts) <= 600   # within 2 windows
                age_ok     = latest.age_seconds < 590          # full window tolerance
                if slug_match and age_ok:
                    copy_signal   = latest
                    copy_win_rate = self.copy_engine._get_wallet_win_rate(latest.wallet)
                    logging.info(
                        f"[Predict] ✅ Using COPY signal: {latest.direction} from "
                        f"{latest.wallet[:10]}... (win_rate={copy_win_rate:.0%}, age={latest.age_seconds:.0f}s)"
                    )
                else:
                    logging.debug(
                        f"[Predict] Copy signal skipped: slug_match={slug_match} "
                        f"age={latest.age_seconds:.0f}s (need <590s)"
                    )

        # ── Build candle list (cap to 30 most recent) ─────────────────────────
        all_candles = list(self.candles)
        if self._current_candle is not None:
            all_candles.append(self._current_candle)
        all_candles = all_candles[-30:]
        closes  = [c["close"] for c in all_candles]
        volumes = [c.get("volume", 0.0) for c in all_candles]
        n       = len(closes)

        # ── Defaults ──────────────────────────────────────────────────────────
        model_direction  = "SKIP"
        model_fair_value = 0.50
        model_confidence = 0.00
        atr_pct          = 999.0

        if n >= 10:
            price_std = float(np.std(closes[-10:])) if np.std(closes[-10:]) > 0 else spot * 0.0001

            # ── ATR gate ──────────────────────────────────────────────────────
            recent_range = max(closes[-5:]) - min(closes[-5:])
            atr_pct      = recent_range / spot * 100
            if atr_pct <= 0.02:
                logging.info(f"[Predict] ATR gate: range={atr_pct:.4f}% — dead market, skip")
            else:
                # ── Bayesian posterior: start from base-rate prior ─────────────
                # p(DOWN)=0.558, p(UP)=0.442 from 86 observed windows.
                # Each signal multiplies by its likelihood ratio L = acc / (1-acc).
                # Accuracy estimates: S1≈55%, S2≈57%, S3≈54%, S4≈56% (conservative).
                prior_down = 0.558
                prior_up   = 0.442
                lr_down = 1.0   # accumulated likelihood ratio favouring DOWN
                lr_up   = 1.0   # accumulated likelihood ratio favouring UP
                signals_fired = 0

                # ── S1: Micro-momentum (3-candle slope) ───────────────────────
                if n >= 6:
                    recent3   = float(np.mean(closes[-3:]))
                    prior3    = float(np.mean(closes[-6:-3]))
                    mom_slope = (recent3 - prior3) / price_std
                    # Lowered threshold to 0.10 (was 0.20) — BTC 1-min moves are
                    # small relative to 10-candle std, so ±0.20 almost never fires.
                    # Accuracy ~55% → L = 0.55/0.45 ≈ 1.22
                    if mom_slope > 0.10:
                        lr_up   *= 1.22;  signals_fired += 1
                    elif mom_slope < -0.10:
                        lr_down *= 1.22;  signals_fired += 1

                # ── S2: Volume-weighted candle direction ───────────────────────
                # Use only the last 10 candles for avg_vol (not all 30) to avoid
                # mixing REST-seeded candles (which have different volume scale)
                # with live-tick candles and inflating the baseline.
                if n >= 10 and sum(volumes[-10:]) > 0:
                    avg_vol   = float(np.mean(volumes[-10:]))
                    last_vol  = volumes[-1]
                    last_open = all_candles[-1].get("open", closes[-1])
                    if avg_vol > 0 and last_vol > avg_vol * 1.2:   # 20% spike (was 30%)
                        # Accuracy ~57% → L = 0.57/0.43 ≈ 1.33
                        if closes[-1] > last_open:
                            lr_up   *= 1.33;  signals_fired += 1
                        elif closes[-1] < last_open:
                            lr_down *= 1.33;  signals_fired += 1

                # ── S3: 10-candle trend position (breakout) ───────────────────
                if n >= 10:
                    hi10  = max(closes[-10:])
                    lo10  = min(closes[-10:])
                    range10 = hi10 - lo10 + 1e-9
                    pos   = (spot - (hi10 + lo10) / 2.0) / range10  # −0.5 to +0.5
                    # Accuracy ~54% → L = 0.54/0.46 ≈ 1.17
                    if pos > 0.25:
                        lr_up   *= 1.17;  signals_fired += 1
                    elif pos < -0.25:
                        lr_down *= 1.17;  signals_fired += 1

                # ── S4: Wilder RSI extreme (momentum exhaustion / continuation) ─
                if n >= 16:
                    rsi = self._compute_rsi_wilder(closes, period=14)
                    # RSI>60 → trend continuation UP; RSI<40 → continuation DOWN
                    # Accuracy ~56% → L = 0.56/0.44 ≈ 1.27
                    if rsi > 60:
                        lr_up   *= 1.27;  signals_fired += 1
                    elif rsi < 40:
                        lr_down *= 1.27;  signals_fired += 1

                # ── Bayesian posterior ────────────────────────────────────────
                post_down = prior_down * lr_down
                post_up   = prior_up   * lr_up
                norm      = post_down + post_up
                p_down    = post_down / norm
                p_up      = post_up   / norm

                # ── EV / Kelly sizing ─────────────────────────────────────────
                # Market price ≈ 0.500, so Kelly = 2*(p - 0.5)
                # Always require ≥2 independent signals before betting.
                # A single signal barely moves the posterior off the prior — not enough
                # edge to justify a prediction. 2 signals = meaningful update.
                # Scaler: kelly * 4.0 (up from 2.5) so that the weakest 2-signal
                # combo (p≈0.56) maps to ~24% confidence instead of ~15%.
                # Floor: minimum 25% confidence when ≥2 signals agree on direction,
                # since the posterior already guarantees p > 0.55 in those cases.
                q           = 0.500   # market price (empirically flat at 0.500)
                min_signals = 2       # always need 2+ signals

                if p_down > p_up and p_down > 0.50 and signals_fired >= min_signals:
                    model_direction  = "DOWN"
                    model_fair_value = round(p_down, 3)
                    kelly            = 2.0 * (p_down - q)
                    raw_conf         = min(kelly * 4.0, 0.80)
                    model_confidence = max(raw_conf, 0.25)   # floor at 25%

                elif p_up > p_down and p_up > 0.50 and signals_fired >= min_signals:
                    model_direction  = "UP"
                    model_fair_value = round(p_up, 3)
                    kelly            = 2.0 * (p_up - q)
                    raw_conf         = min(kelly * 4.0, 0.80)
                    model_confidence = max(raw_conf, 0.25)   # floor at 25%

                else:
                    # No positive EV — skip this window
                    model_direction  = "SKIP"
                    model_fair_value = 0.50

                logging.info(
                    f"[Predict] p_down={p_down:.3f} p_up={p_up:.3f} "
                    f"signals={signals_fired} → {model_direction} "
                    f"conf={model_confidence:.0%} atr={atr_pct:.4f}% n={n}"
                )

        # ── Fetch market from CLOB ─────────────────────────────────────────────
        market_info       = await self._fetch_5m_market(slug)
        market_up_price   = market_info.get("up_price",   0.5) if market_info else 0.5
        market_down_price = 1.0 - market_up_price
        condition_id      = market_info.get("condition_id", "") if market_info else ""
        accepting         = market_info.get("accepting_orders", False) if market_info else False

        # ── Final direction (copy signal overrides everything) ─────────────────
        if copy_signal:
            direction  = copy_signal.direction
            fair_value = copy_signal.raw_price   # whale's actual price paid
            confidence = max(copy_win_rate, model_confidence)
            source     = f"copy:{copy_signal.wallet[:6]}... ({copy_win_rate:.0%} WR)"
        else:
            if model_direction == "SKIP":
                logging.info(f"[Predict] No positive EV for {slug} — skipping.")
                return

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
            f"atr={atr_pct:.4f}% candles={n} | {slug}"
        )

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

    def _compute_rsi_wilder(self, prices: List[float], period: int = 14) -> float:
        """
        Wilder-smoothed RSI — the correct implementation used by TradingView
        and most professional platforms. Fixes the naive avg-gain version which
        overstates RSI on brief one-sided runs and understates momentum exhaustion.
        """
        if len(prices) < period + 2:
            return 50.0
        deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
        gains  = [max(d, 0.0) for d in deltas]
        losses = [max(-d, 0.0) for d in deltas]
        # Seed with simple average over first `period` bars
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        # Wilder smooth over remaining bars
        for g, l in zip(gains[period:], losses[period:]):
            avg_gain = (avg_gain * (period - 1) + g) / period
            avg_loss = (avg_loss * (period - 1) + l) / period
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
                f"📋 Source: <b>Technical model v5</b> (EV + Kelly + Bayes)\n"
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
