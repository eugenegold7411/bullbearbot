"""
earnings_calendar_lookup.py — read-only helpers over data/market/earnings_calendar.json.

Public API:
    load_calendar_map()            -> dict[str, dict]
    earnings_days_away(sym, cal=None) -> int | None
    format_earnings_line(sym, n_days, iso) -> str
    assert_core_coverage(calendar_map: dict) -> list[str]
    _load_raw()                    -> list[dict]    # internal helper

Read-only by contract — never writes earnings_calendar.json.
The canonical writer is data_warehouse.refresh_earnings_calendar_av().
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_CAL_PATH = Path(__file__).parent / "data" / "market" / "earnings_calendar.json"


def _load_raw() -> list[dict]:
    """Return the raw `calendar` list from earnings_calendar.json. [] on error."""
    try:
        if not _CAL_PATH.exists():
            return []
        d = json.loads(_CAL_PATH.read_text())
        cal = d.get("calendar") if isinstance(d, dict) else None
        return cal if isinstance(cal, list) else []
    except Exception:
        return []


def load_calendar_map() -> dict[str, dict]:
    """Returns {SYMBOL: entry_dict}. Symbols upper-cased. {} on error."""
    out: dict[str, dict] = {}
    for entry in _load_raw():
        sym = (entry.get("symbol") or "").upper()
        if not sym:
            continue
        # Earliest upcoming date wins on duplicates
        if sym in out:
            existing_date = str(out[sym].get("earnings_date", ""))[:10]
            new_date = str(entry.get("earnings_date", ""))[:10]
            if new_date and (not existing_date or new_date < existing_date):
                out[sym] = entry
        else:
            out[sym] = entry
    return out


def earnings_days_away(symbol: str, calendar_map: Optional[dict] = None) -> Optional[int]:
    """Days until earnings for symbol, or None if not in calendar / unparseable."""
    if calendar_map is None:
        calendar_map = load_calendar_map()
    entry = calendar_map.get((symbol or "").upper())
    if not entry:
        return None
    iso = str(entry.get("earnings_date", ""))[:10]
    if not iso:
        return None
    try:
        return (date.fromisoformat(iso) - date.today()).days
    except Exception:
        return None


def earnings_timing(symbol: str, calendar_map: Optional[dict] = None) -> Optional[str]:
    """Return timing string ('pre-market', 'post-market', 'unknown') or None if absent."""
    if calendar_map is None:
        calendar_map = load_calendar_map()
    entry = calendar_map.get((symbol or "").upper())
    if not entry:
        return None
    return entry.get("timing")


def format_earnings_line(symbol: str, n_days: Optional[int], iso: str) -> str:
    """One-line render for the morning brief / debug. Never raises."""
    sym = (symbol or "?").upper()
    if not iso:
        return f"{sym}: earnings date unknown"
    if n_days is None:
        return f"{sym} reports {iso}"
    if n_days < 0:
        return f"{sym} reported {iso} ({abs(n_days)} days ago)"
    if n_days == 0:
        return f"{sym} reports {iso} (today)"
    return f"{sym} reports {iso} (in {n_days} days)"


def assert_core_coverage(calendar_map: dict) -> list[str]:
    """
    Return list of core stock symbols missing from calendar_map.
    ETFs / crypto are excluded from the check.
    Logs a WARNING for each missing symbol.
    """
    try:
        import watchlist_manager as wm  # lazy — tests monkeypatch sys.modules
        core = wm.get_core() or []
    except Exception:
        return []

    missing: list[str] = []
    for entry in core:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "stock":
            continue
        sym = (entry.get("symbol") or "").upper()
        if not sym:
            continue
        if sym not in calendar_map:
            missing.append(sym)
            log.warning("[EARNINGS] core symbol missing from calendar: %s", sym)

    return missing
