"""
earnings_rotation.py — earnings-driven rotation tier maintenance.

Pipeline:
  4 AM ET (weekday) — run_earnings_rotation():
    1. Fetch upcoming earnings (next 30 days) via yfinance for tracked universe.
    2. Filter: must pass _passes_mkt_cap_floor ($3B floor, fail-open).
    3. Purge off-universe symbols from pending_rotation.json.
    4. Promote eligible symbols via watchlist_manager.add_rotation_symbol().
    5. Queue new symbols for A2 IV fast-track via earnings_iv_fasttrack().
    6. Write report to data/reports/earnings_rotation_YYYY-MM-DD.json.

  2 AM ET (weekday) — _cull_post_earnings_symbols():
    Removes rotation symbols past their post_earnings_cull_after.
    Core symbols are never culled.

The cull is intentionally extracted from run_earnings_rotation — the
earnings-rotation 4 AM run no longer culls. This separation lets the
2 AM cull run independently of any AV/yfinance availability.

NO Finnhub. NO yfinance writing the canonical calendar. AV-tagged data
in earnings_calendar.json is the canonical earnings source; yfinance is
a per-symbol date confirmation/discovery tool only.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from watchlist_manager import (
    CORE_SYMBOLS,
    add_rotation_symbol,
    get_active_watchlist,
    get_core,
    get_rotation,
    remove_rotation_symbol,
)

log = logging.getLogger(__name__)

_BASE         = Path(__file__).parent
_PENDING_PATH = _BASE / "data" / "market" / "pending_rotation.json"
_REPORTS_DIR  = _BASE / "data" / "reports"

# Universe extension beyond watchlist — symbols admissible for rotation
_EXTRA_UNIVERSE: frozenset[str] = frozenset({
    "NFLX", "CRM", "ORCL", "ADBE", "NOW", "WDAY", "ZM",
    "V", "MA", "PYPL", "SQ", "AFRM", "UPST", "SOFI", "HOOD",
    "BAC", "C", "WFC", "GE", "CAT", "DE", "BA", "UNH",
    "UBER", "LYFT", "ABNB", "DASH", "COIN", "MSTR", "SHOP",
    "DDOG", "NET", "CRWD", "OKTA", "ZS", "TEAM", "MDB", "ESTC",
    "U", "RBLX", "ARM", "SMCI", "MRVL", "QCOM", "MU", "INTC", "TXN",
    "AMAT", "KLAC", "LRCX", "ONTO", "ENTG", "SNAP", "SPOT",
    "RDFN", "Z", "OPEN", "CVNA", "RIVN", "LCID", "NIO", "XPEV", "LI",
    "BIDU", "JD", "PDD", "BABA", "SE", "GRAB", "GOTO", "TSLA",
    "AAPL", "META", "GOOGL", "AMD",
})


# ── Pending rotation I/O ──────────────────────────────────────────────────────

def _load_pending() -> list[dict]:
    if not _PENDING_PATH.exists():
        return []
    try:
        d = json.loads(_PENDING_PATH.read_text())
        return d.get("symbols", []) if isinstance(d, dict) else []
    except Exception:
        return []


def _save_pending(symbols: list[dict]) -> None:
    try:
        _PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PENDING_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({
            "symbols":    symbols,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2))
        tmp.replace(_PENDING_PATH)
    except Exception as exc:
        log.debug("[ROTATION] _save_pending failed (non-fatal): %s", exc)


# ── Universe membership ───────────────────────────────────────────────────────

def _admissible_universe() -> set[str]:
    """Symbols admissible for rotation: watchlist + _EXTRA_UNIVERSE."""
    syms: set[str] = set(_EXTRA_UNIVERSE)
    try:
        wl = get_active_watchlist()
        for entry in wl.get("all", []):
            sym = (entry.get("symbol") or "").upper() if isinstance(entry, dict) else ""
            if sym and "/" not in sym:
                syms.add(sym)
    except Exception:
        pass
    syms |= set(CORE_SYMBOLS)
    return syms


# ── Sector inference ──────────────────────────────────────────────────────────

def _infer_sector(symbol: str) -> str:
    """Read sector from portfolio_intelligence._SYMBOL_SECTOR. Fallback 'unknown'."""
    try:
        # Lazy import — tests monkeypatch sys.modules["portfolio_intelligence"]
        import portfolio_intelligence as pi  # noqa: PLC0415
        m = getattr(pi, "_SYMBOL_SECTOR", {}) or {}
        return m.get(symbol.upper(), m.get(symbol, "unknown")) or "unknown"
    except Exception:
        return "unknown"


# ── Market-cap floor ──────────────────────────────────────────────────────────

def _passes_mkt_cap_floor(symbol: str, floor_usd: float = 3_000_000_000.0) -> bool:
    """
    True if symbol's market cap >= floor_usd.
    FAIL-OPEN: missing / unparseable cap data returns True (never blocks).
    """
    try:
        import yfinance as yf  # noqa: PLC0415
        info = yf.Ticker(symbol).fast_info
        cap = getattr(info, "market_cap", None)
        if cap is None:
            try:
                cap = info.get("market_cap")
            except Exception:
                cap = None
        if cap is None:
            return True   # fail-open
        return float(cap) >= floor_usd
    except Exception:
        return True       # fail-open


# ── yfinance earnings discovery (rotation candidates only) ───────────────────

def _fetch_yfinance_earnings(tickers: list[str], lookforward: int = 30) -> list[dict]:
    """
    Per-symbol yfinance .calendar lookups, dict API.
    Returns [{"symbol", "earnings_date" (date), "source": "yfinance"}] for
    symbols whose earnings fall within (today, today + lookforward].
    Non-fatal per-symbol.
    """
    import yfinance as yf  # noqa: PLC0415
    today  = date.today()
    horizon = today + timedelta(days=lookforward)
    out: list[dict] = []
    for sym in tickers:
        sym_u = (sym or "").upper()
        if not sym_u or "/" in sym_u:
            continue
        try:
            cal = yf.Ticker(sym_u).calendar
            if not cal:
                continue
            dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
            if not dates:
                continue
            d = dates[0]
            if hasattr(d, "date"):
                d = d.date()
            if not (today < d <= horizon):
                continue
            out.append({"symbol": sym_u, "earnings_date": d, "source": "yfinance"})
        except Exception as exc:
            log.debug("[ROTATION] yfinance %s failed (non-fatal): %s", sym_u, exc)
    return out


# ── Cull (extracted to 2 AM scheduler path) ───────────────────────────────────

def _cull_post_earnings_symbols() -> list[dict]:
    """
    Remove rotation symbols where post_earnings_cull_after < today.
    Core symbols are never removed regardless of cull_after.
    Returns list of culled entries: [{"symbol", "reason"}, ...].
    Non-fatal — never raises.
    """
    today = date.today()
    culled: list[dict] = []
    try:
        for s in get_rotation():
            sym = (s.get("symbol") or "").upper()
            if not sym or sym in CORE_SYMBOLS:
                continue
            cull_after = s.get("post_earnings_cull_after") or ""
            if not cull_after:
                continue
            try:
                cd = date.fromisoformat(str(cull_after)[:10])
            except Exception:
                continue
            if cd < today:
                if remove_rotation_symbol(sym):
                    culled.append({"symbol": sym, "reason": "post_earnings_cull",
                                   "cull_after": str(cull_after)})
    except Exception as exc:
        log.warning("[ROTATION] cull error (non-fatal): %s", exc)
    if culled:
        log.info("[ROTATION] culled %d post-earnings symbols: %s",
                 len(culled), [c["symbol"] for c in culled])
    return culled


# ── Main entry point ──────────────────────────────────────────────────────────

def run_earnings_rotation(config: Optional[dict] = None) -> dict:
    """
    Daily rotation run. Idempotent. Never raises.

    Steps:
      1. Discover earnings candidates (next 30 days) via yfinance for the
         admissible universe.
      2. Purge pending_rotation entries that are off-universe.
      3. For each candidate not already in core/rotation:
           - require _passes_mkt_cap_floor() (fail-open)
           - add to rotation tier with post_earnings_cull_after = earnings + 2 days
           - call options_universe_manager.earnings_iv_fasttrack() (best-effort)
      4. Write report to data/reports/earnings_rotation_YYYY-MM-DD.json.

    Returns:
      {"added": [str], "culled": 0 (always — cull is separate), "watchlist_size_after": int}
    """
    today = date.today()
    cfg = (config or {}).get("earnings_rotation", {}) if isinstance(config, dict) else {}
    lookforward    = int(cfg.get("lookforward_days", 30))
    post_hold_days = int(cfg.get("post_earnings_hold_days", 2))
    max_new        = int(cfg.get("max_new_symbols_per_day", 10))

    universe = _admissible_universe()
    in_watchlist = {(s.get("symbol") or "").upper() for s in get_rotation()} \
                 | {(s.get("symbol") or "").upper() for s in get_core()}

    # ── Step 1: discover ──────────────────────────────────────────────────────
    candidates: list[dict] = []
    try:
        candidates = _fetch_yfinance_earnings(sorted(universe), lookforward=lookforward)
    except Exception as exc:
        log.warning("[ROTATION] discovery failed (non-fatal): %s", exc)

    # ── Step 2: purge pending of off-universe entries ─────────────────────────
    pending = _load_pending()
    purged_pending: list[dict] = []
    kept_pending:   list[dict] = []
    for p in pending:
        sym = (p.get("symbol") or "").upper()
        if not sym or sym not in universe:
            purged_pending.append(p)
        else:
            kept_pending.append(p)
    if purged_pending:
        _save_pending(kept_pending)
        log.info("[ROTATION] purged %d off-universe pending: %s",
                 len(purged_pending), [p.get("symbol") for p in purged_pending])
    else:
        # Still write back to ensure file is current/well-formed
        _save_pending(kept_pending)

    # ── Step 3: promotion ─────────────────────────────────────────────────────
    added: list[str] = []
    skipped_low_cap: list[str] = []
    new_count = 0
    for cand in sorted(candidates, key=lambda c: c["earnings_date"]):
        if new_count >= max_new:
            break
        sym = cand["symbol"]
        if sym in in_watchlist or sym in CORE_SYMBOLS:
            continue
        if not _passes_mkt_cap_floor(sym):
            skipped_low_cap.append(sym)
            continue
        ed = cand["earnings_date"]
        cull_after = (ed + timedelta(days=post_hold_days)).isoformat()
        sector = _infer_sector(sym)
        ok = add_rotation_symbol(
            symbol=sym,
            sector=sector,
            cull_after=cull_after,
            source="earnings_rotation",
            earnings_date=ed.isoformat(),
        )
        if ok:
            added.append(sym)
            new_count += 1
            # Best-effort IV fast-track
            try:
                from options_universe_manager import (
                    earnings_iv_fasttrack,  # noqa: PLC0415
                )
                earnings_iv_fasttrack(sym, ed)
            except Exception as exc:
                log.debug("[ROTATION] iv_fasttrack(%s) failed (non-fatal): %s", sym, exc)

    # ── Step 4: write report ──────────────────────────────────────────────────
    rotation_size = len(get_rotation())
    report = {
        "date":                  today.isoformat(),
        "added":                 added,
        "skipped_low_cap":       skipped_low_cap,
        "purged_pending":        [p.get("symbol") for p in purged_pending],
        "watchlist_size_after":  rotation_size,
    }
    try:
        _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        path = _REPORTS_DIR / f"earnings_rotation_{today.isoformat()}.json"
        path.write_text(json.dumps(report, indent=2))
    except Exception as exc:
        log.warning("[ROTATION] report write failed (non-fatal): %s", exc)

    log.info("[ROTATION] run complete added=%d size_after=%d",
             len(added), rotation_size)

    # `culled` is always 0 in this function — cull is owned by 2 AM job.
    return {"added": added, "culled": 0, "watchlist_size_after": rotation_size}
