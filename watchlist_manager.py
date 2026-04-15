"""
watchlist_manager.py — three-tier dynamic watchlist.

TIER 1  core      watchlist_core.json       permanent, never removed
TIER 2  dynamic   watchlist_dynamic.json    pre-market scan finds, reset 8 PM ET daily
TIER 3  intraday  watchlist_intraday.json   live promotions, reset 8 PM ET, max 10

Usage:
    from watchlist_manager import get_active_watchlist, promote_intraday, reset_session_tiers
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from log_setup import get_logger

log = get_logger(__name__)
ET  = ZoneInfo("America/New_York")

BASE         = Path(__file__).parent
CORE_FILE    = BASE / "watchlist_core.json"
DYNAMIC_FILE = BASE / "watchlist_dynamic.json"
INTRADAY_FILE= BASE / "watchlist_intraday.json"

MAX_DYNAMIC  = 8
MAX_INTRADAY = 10

# 8 PM ET in minutes-since-midnight
_RESET_MINUTE = 20 * 60   # 8:00 PM


# ── Low-level I/O ─────────────────────────────────────────────────────────────

def _load(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {"symbols": []}


def _save(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2))


# ── Core (read-only) ──────────────────────────────────────────────────────────

def get_core() -> list[dict]:
    return _load(CORE_FILE).get("symbols", [])


# ── Dynamic tier ──────────────────────────────────────────────────────────────

def get_dynamic() -> list[dict]:
    return _load(DYNAMIC_FILE).get("symbols", [])


def set_dynamic(candidates: list[dict]) -> None:
    """Replace dynamic tier (called by scanner). Enforces MAX_DYNAMIC."""
    trimmed = candidates[:MAX_DYNAMIC]
    _save(DYNAMIC_FILE, {
        "symbols":  trimmed,
        "reset_at": datetime.now(ET).isoformat(),
    })
    log.info("Dynamic watchlist updated: %d symbols", len(trimmed))
    for s in trimmed:
        log.info("  [DYNAMIC+] %s  reason=%s  catalyst=%s",
                 s.get("symbol"), s.get("reason", "?"), s.get("catalyst", "?"))


# ── Intraday tier ─────────────────────────────────────────────────────────────

def get_intraday() -> list[dict]:
    return _load(INTRADAY_FILE).get("symbols", [])


def promote_intraday(symbol: str, reason: str, *, force: bool = False) -> bool:
    """
    Add symbol to intraday watchlist if not already present.
    Returns True if newly added, False if already tracked or at cap.
    """
    symbol = symbol.upper()
    data   = _load(INTRADAY_FILE)
    syms   = data.get("symbols", [])

    # Skip if already in core or dynamic
    existing = {s["symbol"] for s in get_core()} | {s["symbol"] for s in get_dynamic()}
    if symbol in existing:
        return False

    # Skip if already intraday
    if any(s["symbol"] == symbol for s in syms):
        return False

    if not force and len(syms) >= MAX_INTRADAY:
        log.debug("Intraday cap reached (%d) — skipping %s", MAX_INTRADAY, symbol)
        return False

    entry = {
        "symbol":   symbol,
        "tier":     "intraday",
        "reason":   reason,
        "added_at": datetime.now(ET).isoformat(),
    }
    syms.append(entry)
    _save(INTRADAY_FILE, {"symbols": syms, "session_date": _today_et()})
    log.info("[INTRADAY+] %s  reason=%s", symbol, reason)
    return True


def demote_intraday(symbol: str, reason: str) -> None:
    symbol = symbol.upper()
    data   = _load(INTRADAY_FILE)
    before = len(data.get("symbols", []))
    data["symbols"] = [s for s in data.get("symbols", []) if s["symbol"] != symbol]
    if len(data["symbols"]) < before:
        _save(INTRADAY_FILE, data)
        log.info("[INTRADAY-] %s  reason=%s", symbol, reason)


def demote_dynamic(symbol: str, reason: str) -> None:
    symbol = symbol.upper()
    data   = _load(DYNAMIC_FILE)
    before = len(data.get("symbols", []))
    data["symbols"] = [s for s in data.get("symbols", []) if s["symbol"] != symbol]
    if len(data["symbols"]) < before:
        _save(DYNAMIC_FILE, data)
        log.info("[DYNAMIC-] %s  reason=%s", symbol, reason)


# ── Session reset ─────────────────────────────────────────────────────────────

def _today_et() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def reset_session_tiers() -> None:
    """Clears dynamic and intraday tiers. Called at 8 PM ET."""
    _save(DYNAMIC_FILE,  {"symbols": [], "reset_at": datetime.now(ET).isoformat()})
    _save(INTRADAY_FILE, {"symbols": [], "session_date": _today_et()})
    log.info("Session tiers reset (dynamic + intraday cleared)")


def maybe_reset_session_tiers() -> None:
    """Call each cycle — resets tiers if past 8 PM ET and not yet reset today."""
    now_et  = datetime.now(ET)
    now_min = now_et.hour * 60 + now_et.minute
    today   = _today_et()

    if now_min < _RESET_MINUTE:
        return

    intraday_data = _load(INTRADAY_FILE)
    last_session  = intraday_data.get("session_date", "")
    if last_session != today:
        reset_session_tiers()


# ── Stale intraday pruning ────────────────────────────────────────────────────

def prune_stale_intraday(max_age_hours: float = 4.0) -> None:
    """Remove intraday entries that have been sitting > max_age_hours with no trade."""
    data = _load(INTRADAY_FILE)
    now  = datetime.now(ET)
    kept = []
    for s in data.get("symbols", []):
        added_str = s.get("added_at", "")
        try:
            added = datetime.fromisoformat(added_str)
            age_h = (now - added).total_seconds() / 3600
            if age_h <= max_age_hours:
                kept.append(s)
            else:
                log.info("[INTRADAY-] %s  reason=stale_%.1fh", s["symbol"], age_h)
        except Exception:
            kept.append(s)   # keep if parse fails
    data["symbols"] = kept
    _save(INTRADAY_FILE, data)


# ── Feedback loop: auto-promote from signals ──────────────────────────────────

def run_feedback_loop(
    breaking_news_text: str = "",
    claude_rationale:   str = "",
    volume_spikes:      dict | None = None,  # {symbol: vol_ratio}
    price_movers:       dict | None = None,  # {symbol: pct_chg}
    news_mentions:      dict | None = None,  # {symbol: count_today}
) -> None:
    """
    Runs each cycle. Promotes symbols to intraday tier based on live signals.
    Prunes stale intraday entries.
    """
    maybe_reset_session_tiers()
    prune_stale_intraday()

    # Extract tickers from breaking news
    if breaking_news_text:
        _promote_from_text(breaking_news_text, "breaking_news")

    # Extract tickers Claude mentioned in rationale but aren't on watchlist
    if claude_rationale:
        _promote_from_text(claude_rationale, "claude_rationale")

    # Volume spikes > 5x
    if volume_spikes:
        for sym, ratio in volume_spikes.items():
            if ratio >= 5.0:
                promote_intraday(sym, f"volume_spike_{ratio:.1f}x")

    # Price moves > 3% in 30 min
    if price_movers:
        for sym, pct in price_movers.items():
            if abs(pct) >= 3.0:
                promote_intraday(sym, f"price_move_{pct:+.1f}pct")

    # News mention frequency >= 3
    if news_mentions:
        for sym, count in news_mentions.items():
            if count >= 3:
                promote_intraday(sym, f"news_frequency_{count}x")


# Regex: uppercase ticker-like tokens (2-5 chars, not common words)
_TICKER_RE  = re.compile(r'\b([A-Z]{2,5})\b')
_SKIP_WORDS = {
    "BUY", "SELL", "HOLD", "THE", "AND", "FOR", "ETF", "USD",
    "VIX", "RSI", "ATM", "PDT", "IPO", "CEO", "CFO", "SEC",
    "FDA", "DOD", "GDP", "CPI", "PCE", "FED", "MACD", "VWAP",
    "IV", "DTE", "OTM", "ITM", "P&L", "AI", "ML", "BTC", "ETH", "API", "ISSUE", "ALERT", "INFO", "WARN", "DATA", "FEED", "BOT", "CYCLE", "NOTE", "HALT", "MODE", "TRADE", "HOLD", "REST", "OHLCV", "VWAP", "MACD", "RSI", "ATH", "DTE", "UTC", "JSON", "CSV",
}

def _promote_from_text(text: str, reason: str) -> None:
    # Only promote symbols already in the core watchlist — prevents spurious
    # words from breaking news ever becoming tickers.
    core_symbols = {s["symbol"] for s in get_core()}
    already_intraday = {s["symbol"] for s in get_intraday()}
    candidates = set(_TICKER_RE.findall(text)) - already_intraday
    for sym in candidates:
        if sym in core_symbols:
            promote_intraday(sym, reason)


# ── Master merge ──────────────────────────────────────────────────────────────

def get_active_watchlist() -> dict:
    """
    Returns merged watchlist dict:
    {
      "all":    [list of all unique symbol dicts, highest tier wins],
      "stocks": [symbol strings, non-crypto],
      "etfs":   [symbol strings],
      "crypto": [symbol strings],
      "core":   [symbol dicts],
      "dynamic":[symbol dicts],
      "intraday":[symbol dicts],
      "by_sector": {sector: [symbol dicts]},
    }
    """
    core     = get_core()
    dynamic  = get_dynamic()
    intraday = get_intraday()

    # Merge: highest tier wins on duplicate symbol
    seen: dict[str, dict] = {}
    for s in intraday:
        seen[s["symbol"]] = s
    for s in dynamic:
        seen[s["symbol"]] = s          # dynamic overrides intraday
    for s in core:
        seen[s["symbol"]] = s          # core overrides all

    all_symbols = list(seen.values())

    stocks  = [s["symbol"] for s in all_symbols if s.get("type") == "stock"]
    etfs    = [s["symbol"] for s in all_symbols if s.get("type") == "etf"]
    crypto  = [s["symbol"] for s in all_symbols if s.get("type") == "crypto"]

    by_sector: dict[str, list] = {}
    for s in all_symbols:
        sec = s.get("sector", "other")
        by_sector.setdefault(sec, []).append(s)

    return {
        "all":       all_symbols,
        "stocks":    stocks,
        "etfs":      etfs,
        "crypto":    crypto,
        "core":      core,
        "dynamic":   dynamic,
        "intraday":  intraday,
        "by_sector": by_sector,
    }
