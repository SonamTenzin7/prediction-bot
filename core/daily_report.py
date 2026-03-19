"""
daily_report.py
Sends a comprehensive daily performance report to Telegram every day at midnight BT.
Covers: accuracy, confidence calibration, win streaks, signal sources, BTC summary.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

import aiohttp

BT_OFFSET = timedelta(hours=6)   # Bhutan Time = UTC+6


def _now_bt() -> datetime:
    return datetime.now(timezone.utc) + BT_OFFSET


def _load_log(path: str = "signal_log.jsonl") -> tuple[dict, dict]:
    """Return (preds, results) dicts keyed by slug."""
    preds, results = {}, {}
    if not os.path.exists(path):
        return preds, results
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            slug = r.get("slug", "")
            if r.get("type") == "prediction":
                preds[slug] = r
            elif r.get("type") == "result":
                results[slug] = r
    return preds, results


def _build_report(preds: dict, results: dict, period_label: str) -> str:
    """Build the Telegram HTML report string."""

    resolved_slugs = [s for s in preds if s in results]
    if not resolved_slugs:
        return f"<b>📊 Daily Report — {period_label}</b>\n\nNo resolved predictions yet."

    # ── Core metrics ──────────────────────────────────────────────────────────
    correct = wrong = 0
    confidences_correct = []
    confidences_wrong   = []
    up_correct = up_wrong = down_correct = down_wrong = 0
    source_counts: Dict[str, int] = {}
    source_correct: Dict[str, int] = {}
    streak = 0
    max_streak = 0
    cur_streak = 0

    for slug in sorted(resolved_slugs, key=lambda s: preds[s].get("window_start", 0)):
        p   = preds[slug]
        res = results[slug]
        pred_dir   = p.get("direction", "")
        actual_dir = res.get("actual", "")
        conf       = p.get("confidence", 0)
        src        = "🐋 Copy" if p.get("source", "").startswith("copy:") else "⚙️ Model"

        is_correct = pred_dir == actual_dir
        source_counts[src]  = source_counts.get(src, 0) + 1
        source_correct[src] = source_correct.get(src, 0) + (1 if is_correct else 0)

        if is_correct:
            correct += 1
            confidences_correct.append(conf)
            cur_streak += 1
            max_streak = max(max_streak, cur_streak)
            if pred_dir == "UP":   up_correct   += 1
            else:                  down_correct += 1
        else:
            wrong += 1
            confidences_wrong.append(conf)
            cur_streak = 0
            if pred_dir == "UP":  up_wrong   += 1
            else:                 down_wrong += 1

    streak = cur_streak   # current active streak at end of period
    total = correct + wrong
    accuracy = correct / total if total else 0
    avg_conf = (sum(confidences_correct) + sum(confidences_wrong)) / total if total else 0

    # Confidence buckets
    buckets = [(0.0, 0.3, "Low  (&lt;30%)"), (0.3, 0.6, "Med  (30-60%)"), (0.6, 1.01, "High (&gt;60%)")]
    bucket_lines = []
    for lo, hi, label in buckets:
        bc = bw = 0
        for slug in resolved_slugs:
            p   = preds[slug]
            res = results[slug]
            c   = p.get("confidence", 0)
            if lo <= c < hi:
                if p["direction"] == res.get("actual", ""):
                    bc += 1
                else:
                    bw += 1
        bt = bc + bw
        if bt:
            bar = "🟩" * round((bc / bt) * 5) + "⬜" * (5 - round((bc / bt) * 5))
            bucket_lines.append(f"  {label}: {bar} {bc}/{bt} ({bc/bt:.0%})")

    # BTC price summary
    open_prices  = [results[s]["btc_open"]  for s in resolved_slugs if results[s].get("btc_open")]
    close_prices = [results[s]["btc_close"] for s in resolved_slugs if results[s].get("btc_close")]
    if open_prices and close_prices:
        btc_start = open_prices[0]
        btc_end   = close_prices[-1]
        btc_pct   = (btc_end - btc_start) / btc_start * 100
        btc_line  = f"📉 BTC: <code>${btc_start:,.0f}</code> → <code>${btc_end:,.0f}</code> (<b>{btc_pct:+.2f}%</b>)"
    else:
        btc_line = ""

    # Accuracy bar
    acc_bar = "🟩" * round(accuracy * 5) + "⬜" * (5 - round(accuracy * 5))

    # ── Assemble message ──────────────────────────────────────────────────────
    lines = [
        f"<b>📊 Daily Bot Report — {period_label}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"<b>🎯 Accuracy</b>: {acc_bar} <b>{accuracy:.0%}</b>  ({correct}/{total} correct)",
        f"<b>📈 UP predictions</b>:   {up_correct}✅  {up_wrong}❌",
        f"<b>📉 DOWN predictions</b>: {down_correct}✅  {down_wrong}❌",
        f"<b>🔥 Current streak</b>: {streak} in a row",
        f"<b>🏆 Best streak</b>: {max_streak} in a row",
        f"<b>📐 Avg confidence</b>: {avg_conf:.0%}",
        "",
        "<b>🔍 Confidence Calibration</b>",
    ] + bucket_lines + [
        "",
        "<b>📡 Signal Source</b>",
    ]

    for src, count in source_counts.items():
        sc    = source_correct.get(src, 0)
        sline = f"  {src}: {sc}/{count} correct ({sc/count:.0%})"
        lines.append(sline)

    if btc_line:
        lines += ["", btc_line]

    # Performance verdict
    lines += ["", "━━━━━━━━━━━━━━━━━━━━━━━"]
    if accuracy >= 0.70:
        lines.append("🏆 <b>Excellent performance!</b> Model is well above coin-flip.")
    elif accuracy >= 0.55:
        lines.append("✅ <b>Good performance.</b> Beating the market.")
    elif accuracy >= 0.45:
        lines.append("⚠️ <b>Coin-flip zone.</b> Model needs more data or tuning.")
    else:
        lines.append("🚨 <b>Below coin-flip.</b> Check signal logic.")

    return "\n".join(lines)


async def _send_telegram(text: str):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id   = os.getenv("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        logging.warning("[DailyReport] Telegram not configured.")
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }) as r:
                if r.status == 200:
                    logging.info("[DailyReport] Daily report sent to Telegram ✅")
                else:
                    resp = await r.json()
                    logging.error(f"[DailyReport] Telegram error: {resp}")
    except Exception as e:
        logging.error(f"[DailyReport] Failed to send: {e}")


async def send_daily_report_now():
    """Send the daily report immediately (call on-demand or at midnight)."""
    preds, results = _load_log()
    now_bt  = _now_bt()
    label   = now_bt.strftime("%b %d, %Y")
    text    = _build_report(preds, results, label)
    await _send_telegram(text)


async def daily_report_loop():
    """
    Background loop — sends report once per day at midnight Bhutan Time (UTC+6 = UTC 18:00).
    On startup, also sends a report immediately so you always have fresh data.
    """
    logging.info("[DailyReport] Daily report scheduler started.")

    # Send one immediately on startup so you see data right away
    await asyncio.sleep(10)   # wait briefly for other engines to initialise
    await send_daily_report_now()

    while True:
        now_bt = _now_bt()
        # Next midnight BT
        next_midnight_bt = (now_bt + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        wait_seconds = (next_midnight_bt - now_bt).total_seconds()
        logging.info(
            f"[DailyReport] Next report in {wait_seconds/3600:.1f}h "
            f"(midnight BT = {next_midnight_bt.strftime('%Y-%m-%d 12:00 AM BT')})"
        )
        await asyncio.sleep(wait_seconds)
        await send_daily_report_now()
