"""
universe_manager.py

Single source of truth for the BullBearBot symbol universe.
Replaces _EXTRA_TRACKED_UNIVERSE (data_warehouse.py) and
_EXTRA_UNIVERSE (earnings_rotation.py).

Rotation window:  DTE 0–21  → ACTIVE
Cooling window:   DTE -3–-1 → post-earnings, still tracked
Exit threshold:   DTE < -3  → remove from dynamic list
Market cap floor: $100B     → fail-closed (None = excluded)
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────
_ROOT         = Path(__file__).parent
_CORE_PATH    = _ROOT / "watchlist_core.json"
_DYNAMIC_PATH = _ROOT / "watchlist_dynamic.json"
_FUND_DIR     = _ROOT / "data" / "fundamentals"

# ── Constants ──────────────────────────────────────────────────────────────────
ROTATION_ENTRY_DTE = 21     # rotate in when DTE <= 21
ROTATION_EXIT_DTE  = -3     # rotate out when DTE < -3 (strictly less than)
MIN_MARKET_CAP_B   = 100.0  # $100B minimum

_AV_URL = (
    "https://www.alphavantage.co/query"
    "?function=EARNINGS_CALENDAR&horizon=3month&apikey={api_key}"
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_us_listed(symbol: str) -> bool:
    """
    Heuristic filter for foreign OTC pink sheet ADRs.
    Returns False for symbols that are almost certainly not US-exchange listed.
    """
    if len(symbol) > 4 and symbol[-1] in ("F", "Y"):
        return False
    if "." in symbol or "-" in symbol:
        return False
    return True


def _get_market_cap(symbol: str) -> float | None:
    """Returns market cap in USD or None. Checks fundamentals cache first."""
    fund = _FUND_DIR / f"{symbol}.json"
    if fund.exists():
        try:
            cap = json.loads(fund.read_text()).get("market_cap")
            if cap is not None:
                return float(cap)
        except Exception:
            pass

    try:
        import yfinance as yf  # noqa: PLC0415
        mc = yf.Ticker(symbol).fast_info.market_cap
        return float(mc) if mc else None
    except Exception:
        return None


def _fetch_av_raw(api_key: str) -> list[dict] | None:
    """Fetch fresh AV earnings calendar.

    Returns:
      list[dict]  — success (may be empty if no future entries)
      None        — AV error: HTTP failure, rate-limit JSON, or error-CSV response
    """
    url = _AV_URL.format(api_key=api_key)
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        body = resp.text or ""
    except Exception as exc:
        log.error("[UNIVERSE] AV fetch failed: %s", exc)
        return None

    if body.lstrip().startswith("{"):
        try:
            msg  = json.loads(body)
            note = msg.get("Note") or msg.get("Information") or msg.get("Error Message") or body[:200]
        except Exception:
            note = body[:200]
        log.error("[UNIVERSE] AV returned JSON (rate limit / bad key?): %s", note)
        return None

    today   = date.today()
    results: list[dict] = []
    rows_seen     = 0
    date_parse_ok = 0
    try:
        for row in csv.DictReader(io.StringIO(body)):
            rows_seen += 1
            sym      = (row.get("symbol") or "").strip().upper()
            raw_date = (row.get("reportDate") or "").strip()
            if not sym or not raw_date:
                continue
            try:
                ed = date.fromisoformat(raw_date[:10])
                date_parse_ok += 1
            except ValueError:
                continue
            if ed < today:
                continue
            raw_timing = (row.get("reportTime") or row.get("timeOfTheDay") or "").strip().lower()
            if "before" in raw_timing or "pre" in raw_timing:
                timing = "pre_market"
            elif "after" in raw_timing or "post" in raw_timing:
                timing = "post_market"
            else:
                timing = "unknown"
            results.append({"symbol": sym, "earnings_date": ed, "timing": timing})
    except Exception as exc:
        log.error("[UNIVERSE] AV CSV parse failed: %s", exc)
        return None

    # AV encodes errors as a CSV row (e.g. "E,r,r,o,r, ,M") — detect by
    # checking whether the body had rows but none had a parseable reportDate.
    if rows_seen > 0 and date_parse_ok == 0:
        log.error(
            "[UNIVERSE] AV CSV had %d rows with no parseable dates — "
            "treating as error response (key invalid or rate-limited)",
            rows_seen,
        )
        return None

    return results


# ── Public API ─────────────────────────────────────────────────────────────────

def fetch_earnings_rotation_candidates(
    api_key: str,
    sim_date: date | None = None,
) -> list[dict] | None:
    """
    Pulls fresh AV earnings calendar, filters to US-listed $100B+ symbols
    with earnings within the next ROTATION_ENTRY_DTE days.

    Returns:
      list[dict]  — success, sorted by dte ascending (may be empty)
      None        — AV fetch failed (propagated from _fetch_av_raw)
    """
    today = sim_date or date.today()

    raw = _fetch_av_raw(api_key)
    if raw is None:
        return None
    if not raw:
        return []

    # Keep earliest upcoming date per US-listed symbol
    by_sym: dict[str, dict] = {}
    for e in raw:
        sym = e["symbol"]
        ed  = e["earnings_date"]
        if ed < today:
            continue
        if not _is_us_listed(sym):
            continue
        if sym not in by_sym or ed < by_sym[sym]["earnings_date"]:
            by_sym[sym] = e

    # Filter to DTE <= ROTATION_ENTRY_DTE
    in_window: list[dict] = []
    for sym, e in by_sym.items():
        dte = (e["earnings_date"] - today).days
        if dte <= ROTATION_ENTRY_DTE:
            in_window.append({
                "symbol":        sym,
                "earnings_date": e["earnings_date"],
                "dte":           dte,
                "timing":        e["timing"],
            })

    # Market cap filter (fail-closed)
    result: list[dict] = []
    for c in in_window:
        sym = c["symbol"]
        cap = _get_market_cap(sym)
        if cap is None:
            log.debug("[UNIVERSE] %s: no market cap — excluded (fail-closed)", sym)
            continue
        cap_b = cap / 1e9
        if cap_b < MIN_MARKET_CAP_B:
            continue
        result.append({
            "symbol":        sym,
            "earnings_date": c["earnings_date"],
            "dte":           c["dte"],
            "timing":        c["timing"],
            "market_cap_b":  round(cap_b, 2),
        })

    result.sort(key=lambda x: x["dte"])
    log.info(
        "[UNIVERSE] candidates: %d AV total, %d in window, %d passed $%.0fB filter",
        len(raw), len(in_window), len(result), MIN_MARKET_CAP_B,
    )
    return result


def sync_dynamic_watchlist(
    api_key: str,
    sim_date: date | None = None,
) -> dict:
    """
    Main entry point — called by scheduler at startup and 9:05 AM ET.

    1. Fetches earnings rotation candidates from AV
    2. Adds new candidates to watchlist_dynamic.json (tier="earnings")
    3. Removes expired tier=earnings entries (dte < ROTATION_EXIT_DTE)
    4. Never removes entries where tier != "earnings" (manually-added symbols)
    5. Returns summary dict

    Returns:
    {
        date, added, removed, active_earnings,
        manually_managed, total_dynamic
    }
    """
    today = sim_date or date.today()

    # Load current watchlist_dynamic.json
    try:
        wl_data: dict = json.loads(_DYNAMIC_PATH.read_text())
    except Exception:
        wl_data = {"symbols": [], "reset_at": ""}

    symbols_list: list[dict] = wl_data.get("symbols", [])
    if not isinstance(symbols_list, list):
        symbols_list = []

    existing: dict[str, dict] = {
        e["symbol"]: e
        for e in symbols_list
        if isinstance(e, dict) and e.get("symbol")
    }

    # Fetch rotation candidates
    candidates = fetch_earnings_rotation_candidates(api_key, sim_date=today)

    # AV fetch failed — leave existing watchlist completely intact
    if candidates is None:
        log.warning("[UNIVERSE] AV fetch failed — preserving existing watchlist unchanged")
        return {
            "date":             today.isoformat(),
            "added":            [],
            "removed":          [],
            "active_earnings":  [],
            "manually_managed": [],
            "total_dynamic":    len(symbols_list),
        }

    # Add new candidates
    added: list[dict] = []
    for c in candidates:
        sym = c["symbol"]
        if sym in existing:
            continue
        entry: dict = {
            "symbol":        sym,
            "sector":        "unknown",
            "type":          "stock",
            "tier":          "earnings",
            "earnings_date": c["earnings_date"].isoformat(),
            "market_cap_b":  c["market_cap_b"],
            "timing":        c["timing"],
        }
        symbols_list.append(entry)
        existing[sym] = entry
        added.append({
            "symbol":        sym,
            "earnings_date": c["earnings_date"].isoformat(),
            "dte":           c["dte"],
            "timing":        c["timing"],
        })
        log.info(
            "[UNIVERSE] auto-added %s (eda=%d, mc=$%.0fB, timing=%s)",
            sym, c["dte"], c["market_cap_b"], c["timing"],
        )

    # Remove expired earnings entries; keep everything else
    removed: list[dict] = []
    new_symbols: list[dict] = []
    for entry in symbols_list:
        sym  = (entry.get("symbol") or "").upper()
        tier = entry.get("tier", "")

        if tier != "earnings":
            new_symbols.append(entry)
            continue

        # Calculate current DTE from stored earnings_date
        ed_str = entry.get("earnings_date", "")
        dte: int | None = None
        if ed_str:
            try:
                ed  = date.fromisoformat(str(ed_str)[:10])
                dte = (ed - today).days
            except (ValueError, TypeError):
                pass

        if dte is not None and dte < ROTATION_EXIT_DTE:
            removed.append({"symbol": sym, "reason": f"post-earnings (dte={dte})"})
            log.info("[UNIVERSE] removed %s (eda=%d, post-earnings)", sym, dte)
        else:
            new_symbols.append(entry)

    # Write updated file atomically
    wl_data["symbols"]  = new_symbols
    wl_data["reset_at"] = datetime.now(timezone.utc).astimezone().isoformat()
    tmp = _DYNAMIC_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(wl_data, indent=2, default=str))
    tmp.replace(_DYNAMIC_PATH)

    # Build summary
    active_earnings: list[dict] = []
    for e in new_symbols:
        if e.get("tier") != "earnings":
            continue
        sym    = (e.get("symbol") or "").upper()
        ed_str = e.get("earnings_date", "")
        dte    = None
        if ed_str:
            try:
                ed  = date.fromisoformat(str(ed_str)[:10])
                dte = (ed - today).days
            except (ValueError, TypeError):
                pass
        active_earnings.append({
            "symbol":  sym,
            "dte":     dte,
            "timing":  e.get("timing", ""),
        })
    active_earnings.sort(key=lambda x: (x["dte"] is None, x["dte"] or 0))

    manually_managed = [
        (e.get("symbol") or "")
        for e in new_symbols
        if e.get("tier") != "earnings"
    ]

    summary = {
        "date":             today.isoformat(),
        "added":            added,
        "removed":          removed,
        "active_earnings":  active_earnings,
        "manually_managed": manually_managed,
        "total_dynamic":    len(new_symbols),
    }

    log.info(
        "[UNIVERSE] sync complete: +%d added, -%d removed, "
        "%d earnings active, %d manual",
        len(added), len(removed), len(active_earnings), len(manually_managed),
    )
    return summary


def get_universe_snapshot(sim_date: date | None = None) -> dict:
    """
    Returns a snapshot of the full symbol universe.
    Reads watchlist_core.json and watchlist_dynamic.json only — no API calls.

    Returns:
    {
        date, core_symbols, dynamic_earnings,
        dynamic_manual, all_symbols
    }
    """
    today = sim_date or date.today()

    core_symbols: list[str] = []
    try:
        data = json.loads(_CORE_PATH.read_text())
        for e in data.get("symbols", []):
            sym = (e.get("symbol") or "").upper()
            if sym and "/" not in sym:
                core_symbols.append(sym)
    except Exception as exc:
        log.debug("[UNIVERSE] core watchlist load failed: %s", exc)

    dynamic_earnings: list[dict] = []
    dynamic_manual:   list[str]  = []
    try:
        wl_data = json.loads(_DYNAMIC_PATH.read_text())
        for e in wl_data.get("symbols", []):
            sym  = (e.get("symbol") or "").upper()
            tier = e.get("tier", "")
            if not sym:
                continue
            if tier == "earnings":
                ed_str = e.get("earnings_date", "")
                dte: int | None = None
                if ed_str:
                    try:
                        ed  = date.fromisoformat(str(ed_str)[:10])
                        dte = (ed - today).days
                    except (ValueError, TypeError):
                        pass
                dynamic_earnings.append({
                    "symbol":        sym,
                    "earnings_date": ed_str or "",
                    "dte":           dte,
                    "timing":        e.get("timing", ""),
                    "market_cap_b":  e.get("market_cap_b"),
                })
            else:
                dynamic_manual.append(sym)
    except Exception as exc:
        log.debug("[UNIVERSE] dynamic watchlist load failed: %s", exc)

    all_syms = sorted(
        set(core_symbols)
        | {e["symbol"] for e in dynamic_earnings}
        | set(dynamic_manual)
    )

    return {
        "date":             today.isoformat(),
        "core_symbols":     core_symbols,
        "dynamic_earnings": dynamic_earnings,
        "dynamic_manual":   dynamic_manual,
        "all_symbols":      all_syms,
    }
