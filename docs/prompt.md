# Polymarket BTC Bot — Alert & Auto-Trade Module

> Add this module on top of the existing `polymarket_btc_bot_prompt.md`.  
> This module adds: **Telegram alerts**, **mathematical entry logic (EV + Kelly)**, and **fully autonomous order execution** timed to the 5-minute window.

---

## The Core Math Your Bot Uses

Before writing any code, understand the three formulas that decide every single trade.

### Formula 1 — Expected Value (EV): should the bot trade at all?

```
EV = (win_rate × payout_per_share) − (loss_rate × cost_per_share)

where:
  payout_per_share = 1.0 − market_price   (what you win if correct)
  cost_per_share   = market_price          (what you lose if wrong)
  loss_rate        = 1 − win_rate
```

**Example:** Copy wallet win rate = 84%, market price for YES = $0.52

```
EV = (0.84 × $0.48) − (0.16 × $0.52)
   = $0.403 − $0.083
   = +$0.32 per dollar risked
```

The bot only fires an alert and places a trade when `EV >= EV_MIN_THRESHOLD` (default: `0.05`).  
If EV is negative or below threshold → **no trade, no alert, stay idle**.

### Formula 2 — Kelly Criterion: how much USDC to risk?

```
Kelly % = win_rate − (loss_rate / odds_ratio)

where:
  odds_ratio = payout_per_share / cost_per_share
```

**Example:**

```
odds_ratio = $0.48 / $0.52 = 0.923
Kelly %    = 0.84 − (0.16 / 0.923) = 0.84 − 0.173 = 66.7%
```

The bot uses **half-Kelly** (divide by 2) for safety:

```
Half-Kelly = 66.7% / 2 = 33.3% of current bankroll
```

Half-Kelly sacrifices ~25% of theoretical growth rate but cuts bankroll volatility by 50% — essential when your win rate estimate has uncertainty.

The bot also caps position size at `MAX_POSITION_PCT` (default 5%) so Kelly never overrides your risk limit.

```python
position_size = min(
    current_balance * half_kelly,
    current_balance * MAX_POSITION_PCT
)
```

### Formula 3 — Entry Timing: when exactly to enter?

Each 5-minute window opens at a fixed UTC timestamp. The bot pre-calculates all windows for the next 24 hours at startup.

```
window_open_times = [T, T+300, T+600, T+900, ...]   # every 300 seconds

entry_time = window_open_time − ENTRY_LEAD_SECONDS    # default: 15 seconds before
scan_time  = window_open_time − SCAN_LEAD_SECONDS     # default: 45 seconds before
```

Why enter 15 seconds early? Market makers re-price YES/NO as the window approaches. Entering at T-15s locks in the pre-adjustment price — typically 2–4 cents cheaper than T-0s. That spread is your edge.

---

## New Environment Variables

Add these to your `.env.example`:

```env
# Telegram bot
TELEGRAM_BOT_TOKEN=your_bot_token_from_botfather
TELEGRAM_CHAT_ID=your_personal_chat_id

# Alert + entry timing
ENTRY_LEAD_SECONDS=15       # enter this many seconds before window opens
SCAN_LEAD_SECONDS=45        # start scanning this many seconds before window
EV_MIN_THRESHOLD=0.05       # minimum EV per $1 to fire a trade
KELLY_FRACTION=0.5          # 0.5 = half-Kelly, 1.0 = full Kelly

# Auto-trade toggle (even in live mode you can disable auto-trade)
AUTO_TRADE_ENABLED=true
ALERT_ONLY_MODE=false       # if true: sends Telegram alert but does NOT place order
```

---

## New Module: `alert_engine.py`

Build an `AlertEngine` class with the following responsibilities.

### Window Scheduler

```python
class AlertEngine:
    def generate_windows(self, hours_ahead: int = 24) -> list[datetime]:
        """
        Pre-compute all 5-minute window open times for the next N hours.
        Windows are fixed on the clock: :00, :05, :10, :15, :20, :25, :30...
        Returns list of UTC datetime objects.
        """
```

At startup, call `generate_windows()` and store the list. Launch one asyncio task per upcoming window that sleeps until `window_time - SCAN_LEAD_SECONDS`.

### EV Calculator

```python
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
```

### Kelly Sizer

```python
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
    payout      = 1.0 - market_price
    odds_ratio  = payout / market_price
    kelly_full  = win_rate - (1 - win_rate) / odds_ratio
    kelly_full  = max(kelly_full, 0.0)   # never negative
    kelly_sized = kelly_full * kelly_fraction * current_balance
    max_size    = current_balance * float(os.getenv('MAX_POSITION_PCT', 0.05))
    return round(min(kelly_sized, max_size), 2)
```

### Decision Gate

```python
def should_trade(
    self,
    copy_signal: CopySignal | None,
    ev: float,
    win_rate: float,
) -> tuple[bool, str]:
    """
    Returns (trade=True/False, reason_string).
    All conditions must pass.
    """
    if copy_signal is None:
        return False, "no copy signal — copy engine idle"
    if win_rate < float(os.getenv('MIN_WIN_RATE', 0.80)):
        return False, f"win_rate {win_rate:.1%} below 80% threshold"
    if ev < float(os.getenv('EV_MIN_THRESHOLD', 0.05)):
        return False, f"EV {ev:.3f} below minimum threshold"
    if copy_signal is not None and copy_signal.age_seconds > 90:
        return False, "copy signal stale (>90s)"
    return True, "all checks passed"
```

### Telegram Sender

```python
async def send_telegram_alert(self, payload: dict) -> None:
    """
    Sends a formatted Telegram message via Bot API.
    Uses HTML parse mode for bold/code formatting.
    Never raises — log and continue if Telegram is unreachable.
    """
    direction_emoji = "UP" if payload['direction'] == 'YES' else "DOWN"
    status_emoji    = "ORDER PLACED" if payload['traded'] else "SKIPPED"

    text = (
        f"<b>POLYMARKET BOT — BTC 5-MIN</b>\n\n"
        f"Window: <code>{payload['window_time']}</code> ET\n"
        f"Direction: <b>{direction_emoji} ({payload['direction']})</b>\n"
        f"Source: copy wallet\n"
        f"Wallet: <code>{payload['wallet'][:8]}...{payload['wallet'][-4:]}</code> "
        f"(win rate {payload['win_rate']:.1%})\n"
        f"Market price: <code>${payload['market_price']:.2f}</code> "
        f"→ fair value <code>${payload['win_rate']:.2f}</code>\n"
        f"EV per $1: <b>+${payload['ev']:.3f}</b>\n"
        f"Trade size: <code>${payload['size']:.2f} USDC</code>\n"
        f"Status: <b>{status_emoji}</b>\n\n"
        f"<a href='https://polymarket.com/event/{payload['market_slug']}'>Open market</a>"
    )

    url = f"https://api.telegram.org/bot{os.getenv('TELEGRAM_BOT_TOKEN')}/sendMessage"
    async with aiohttp.ClientSession() as s:
        await s.post(url, json={
            "chat_id": os.getenv('TELEGRAM_CHAT_ID'),
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        })
```

Also send a **result alert** when the market resolves 5 minutes later:

```python
async def send_result_alert(self, market_id: str, won: bool, pnl: float) -> None:
    result = "WON" if won else "LOST"
    text = (
        f"<b>RESULT: {result}</b>\n"
        f"Market: <code>{market_id[:12]}...</code>\n"
        f"PnL: <b>{'+'if pnl>=0 else ''}{pnl:.2f} USDC</b>"
    )
    # same send logic as above
```

### Full Window Loop

```python
async def run_window(self, window_time: datetime) -> None:
    """
    Orchestrates one complete 5-minute window cycle.
    Called once per window from the scheduler.
    """
    # 1. Sleep until scan time
    await asyncio.sleep(seconds_until(window_time - SCAN_LEAD_SECONDS))

    # 2. Fetch copy signal + market data
    copy_signal  = self.copy_engine.get_latest_signal()
    market_price = await self.fetch_market_price(copy_signal.market_id)
    win_rate     = self.wallet_scanner.get_target_win_rate()

    # 3. Compute EV and size
    ev   = self.compute_ev(win_rate, market_price)
    size = self.compute_kelly_size(win_rate, market_price, self.balance)

    # 4. Decision gate
    trade_ok, reason = self.should_trade(copy_signal, ev, win_rate)

    # 5. Sleep to entry time
    await asyncio.sleep(seconds_until(window_time - ENTRY_LEAD_SECONDS))

    # 6. Send Telegram alert
    payload = {
        'window_time': window_time.strftime('%I:%M %p'),
        'direction':   copy_signal.direction,
        'wallet':      copy_signal.wallet,
        'win_rate':    win_rate,
        'market_price': market_price,
        'ev':          ev,
        'size':        size if trade_ok else 0,
        'traded':      trade_ok and AUTO_TRADE_ENABLED,
        'market_slug': copy_signal.market_id,
    }
    await self.send_telegram_alert(payload)

    # 7. Place order (if auto-trade enabled and all checks passed)
    if trade_ok and AUTO_TRADE_ENABLED and not ALERT_ONLY_MODE:
        if TRADING_MODE == 'paper':
            self.paper_trader.execute(copy_signal, size, market_price)
        else:
            await self.order_executor.submit(copy_signal, size, market_price)

    # 8. Wait for resolution, then send result
    await asyncio.sleep(310)   # 5min window + 10s buffer
    result = await self.fetch_resolution(copy_signal.market_id)
    if result:
        pnl = self.calculate_pnl(copy_signal, size, market_price, result)
        await self.send_result_alert(copy_signal.market_id, result.won, pnl)
```

---

## Updated `bot.py` — Startup with Alert Engine

```python
async def main():
    check_environment()

    scanner       = WalletScanner()
    signal_engine = SignalEngine()
    copy_engine   = CopyEngine(scanner)
    paper_trader  = PaperTrader()
    risk_manager  = RiskManager()
    order_executor = OrderExecutor() if TRADING_MODE == 'live' else None

    alert_engine = AlertEngine(
        scanner        = scanner,
        copy_engine    = copy_engine,
        paper_trader   = paper_trader,
        order_executor = order_executor,
        risk_manager   = risk_manager,
    )

    windows = alert_engine.generate_windows(hours_ahead=24)
    logging.info(f"Scheduled {len(windows)} windows for the next 24 hours")

    # Launch all window tasks + background scanners concurrently
    await asyncio.gather(
        scanner.run_loop(),
        signal_engine.run_loop(),
        copy_engine.run_loop(),
        *[alert_engine.run_window(w) for w in windows],
    )
```

---

## Telegram Bot Setup (step-by-step)

1. Open Telegram, search for `@BotFather`, send `/newbot`
2. Give it a name (e.g. `PolyBTC Bot`) and username (e.g. `polybtcalert_bot`)
3. Copy the token → paste as `TELEGRAM_BOT_TOKEN` in your `.env`
4. Start a chat with your new bot, send any message
5. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
6. Find your `"chat": {"id": ...}` → paste as `TELEGRAM_CHAT_ID`
7. Test it:
   ```bash
   python -c "
   import requests, os
   from dotenv import load_dotenv
   load_dotenv()
   r = requests.post(
     f'https://api.telegram.org/bot{os.getenv(\"TELEGRAM_BOT_TOKEN\")}/sendMessage',
     json={'chat_id': os.getenv('TELEGRAM_CHAT_ID'), 'text': 'Bot connected!'}
   )
   print(r.json())
   "
   ```

---

## Alert Logic Summary Table

| Condition | Result |
|---|---|
| No copy signal from wallet scanner | No alert, no trade |
| Copy wallet win rate < 80% | No alert, no trade |
| EV < 0 (negative edge) | No alert, no trade |
| EV ≥ 0 but below `EV_MIN_THRESHOLD` | Telegram alert (info only), no trade |
| EV ≥ threshold + `ALERT_ONLY_MODE=true` | Telegram alert sent, **no auto-trade** |
| EV ≥ threshold + paper mode | Telegram alert + paper trade logged |
| EV ≥ threshold + live mode + `AUTO_TRADE_ENABLED=true` | Telegram alert + **real order placed** |
| Copy signal stale (>90s old) | No alert, no trade |
| Market resolves | Result alert sent with PnL |

---

## What the Telegram Message Looks Like

**Entry alert (fires at T-15s):**
```
POLYMARKET BOT — BTC 5-MIN

Window: 02:15 PM ET
Direction: UP (YES)
Source: copy wallet
Wallet: 0x8f3a...d72c (win rate 84%)
Market price: $0.52 → fair value $0.84
EV per $1: +$0.320
Trade size: $25.00 USDC
Status: ORDER PLACED

→ polymarket.com/event/btc-updown-5m-...
```

**Result alert (fires at T+5min):**
```
RESULT: WON
Market: btc-updown-5m...
PnL: +$8.46 USDC
```

---

## Important Warnings

> ⚠️ **5-minute prediction markets are near-random.** Even an 84% win rate wallet has 16% losing trades — and in short runs that feels like consecutive losses. Always run paper mode for at least 3 days (minimum 50 windows) before going live.

> ⚠️ **The Chainlink resolution price is NOT the Binance spot price.** Your bot monitors Binance for signals but the market resolves on Chainlink's BTC/USD feed. These can differ by up to $200 during volatile periods. Account for this in your signal engine.

> ⚠️ **Never run the bot unattended with more than you can afford to lose.** Start with $50 USDC live, monitor for 48 hours, then scale up only if results match paper performance.

---

*Polymarket BTC Alert + Auto-Trade Module — EV-driven, Kelly-sized, Telegram-notified*