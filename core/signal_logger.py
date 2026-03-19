"""
signal_logger.py
Appends every prediction and its resolved result to signal_log.jsonl.

Each line is a JSON object.  Two types:
  {"type": "prediction", "slug": ..., "window_start": ..., "window_end": ...,
   "direction": "UP"|"DOWN", "confidence": 0.0-1.0, "fair_value": ...,
   "source": ..., "btc_spot": ..., "market_price": ..., "edge": ...,
   "logged_at": <unix ts>}

  {"type": "result", "slug": ..., "window_start": ...,
   "actual": "UP"|"DOWN", "btc_open": ..., "btc_close": ...,
   "pct_change": ..., "logged_at": <unix ts>}

The Streamlit app joins them on `slug` to produce the full prediction vs actual table.
"""

import json
import os
import time
import logging
from typing import Optional

LOG_FILE = os.getenv("SIGNAL_LOG_FILE", "signal_log.jsonl")


def _append(record: dict):
    record["logged_at"] = time.time()
    try:
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        logging.error(f"signal_logger write error: {e}")


def log_prediction(
    slug: str,
    window_start: int,
    window_end: int,
    direction: str,
    confidence: float,
    fair_value: float,
    source: str,
    btc_spot: float,
    market_price: float,
    edge: float,
):
    _append({
        "type":         "prediction",
        "slug":         slug,
        "window_start": window_start,
        "window_end":   window_end,
        "direction":    direction,
        "confidence":   round(confidence, 4),
        "fair_value":   round(fair_value, 4),
        "source":       source,
        "btc_spot":     round(btc_spot, 2),
        "market_price": round(market_price, 4),
        "edge":         round(edge, 4),
    })


def log_result(
    slug: str,
    window_start: int,
    actual: str,
    btc_open: float,
    btc_close: float,
):
    pct = (btc_close - btc_open) / btc_open * 100 if btc_open else 0.0
    _append({
        "type":         "result",
        "slug":         slug,
        "window_start": window_start,
        "actual":       actual,
        "btc_open":     round(btc_open, 2),
        "btc_close":    round(btc_close, 2),
        "pct_change":   round(pct, 3),
    })
