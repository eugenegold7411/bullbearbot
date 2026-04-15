"""
log_setup.py — configures logging for the trading bot.

Two outputs:
  1. logs/bot.log      — rotating application log (human-readable, 5 MB × 5 files)
  2. logs/trades.jsonl — trade journal (one JSON line per order decision)

Usage:
  from log_setup import get_logger, log_trade

  log = get_logger(__name__)
  log.info("something happened")
  log_trade({...})          # appends one line to trades.jsonl
"""

import json
import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path

LOGS_DIR   = Path(__file__).parent / "logs"
APP_LOG    = LOGS_DIR / "bot.log"
TRADE_LOG  = LOGS_DIR / "trades.jsonl"

_configured = False


def _configure() -> None:
    global _configured
    if _configured:
        return

    LOGS_DIR.mkdir(exist_ok=True)

    fmt = logging.Formatter(
        fmt="%(asctime)s  %(levelname)-8s  %(name)-20s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── Rotating file handler ─────────────────────────────────────────────────
    file_handler = logging.handlers.RotatingFileHandler(
        APP_LOG, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)

    # ── Console handler ───────────────────────────────────────────────────────
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    console_handler.setLevel(logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Remove any pre-existing handlers before adding ours.
    # Third-party libraries (e.g. alpaca-py) call logging.basicConfig() on
    # import, which silently adds a StreamHandler to the root logger before
    # _configure() runs. That causes every log line to appear twice. Clearing
    # here guarantees exactly one FileHandler and one StreamHandler.
    for _h in root.handlers[:]:
        root.removeHandler(_h)

    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # Silence chatty third-party loggers
    for noisy in ("urllib3", "httpcore", "httpx", "yfinance", "peewee",
                  "anthropic._base_client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Returns a logger for the given module name."""
    _configure()
    return logging.getLogger(name)


def log_trade(record: dict) -> None:
    """
    Appends one JSON line to trades.jsonl.
    Always stamps with a UTC ISO timestamp if not already present.
    """
    _configure()
    LOGS_DIR.mkdir(exist_ok=True)
    record.setdefault("ts", datetime.now(timezone.utc).isoformat())
    with open(TRADE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
