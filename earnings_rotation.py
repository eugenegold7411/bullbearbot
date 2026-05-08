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

import watchlist_manager as wm
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
_CAL_PATH     = _BASE / "data" / "market" / "earnings_calendar.json"
_FUND_DIR     = _BASE / "data" / "fundamentals"

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
    # Consumer / blue-chip names (S8-C Fix A1 — keeps in sync with data_warehouse)
    "SBUX", "COST", "DIS", "MCD", "HD", "LOW", "TGT", "NKE", "PG", "KO",
    "PEP", "T", "VZ", "CVS", "MDT", "ABT", "NEE", "DUK", "SO", "D",
    "AMT", "PLD", "EQIX", "PSA", "O",
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
    Returns True if symbol's market cap >= floor_usd.
    FAIL-OPEN: missing/unparseable cap data returns True (never blocks).

    Source priority:
      1. data/fundamentals/{SYM}.json:market_cap  (refreshed daily at 4 AM by
         data_warehouse.refresh_fundamentals — runs strictly before this job).
      2. yfinance.Ticker.fast_info.market_cap     (fallback for cache miss only).
      3. True (fail-open).
    """
    # 1. Fundamentals cache
    fund_path = _FUND_DIR / f"{symbol}.json"
    if fund_path.exists():
        try:
            cap = json.loads(fund_path.read_text()).get("market_cap")
            if cap is not None:
                return float(cap) >= floor_usd
        except Exception:
            pass

    # 2. Live yfinance fallback — only for symbols not yet in cache
    try:
        import yfinance as yf  # noqa: PLC0415
        info = yf.Ticker(symbol).fast_info
        cap = getattr(info, "market_cap", None)
        if cap is None:
            try:
                cap = info.get("market_cap")
            except Exception:
                cap = None
        if cap is not None:
            return float(cap) >= floor_usd
    except Exception:
        pass

    # 3. Fail-open
    log.debug("[ROTATION] market_cap unavailable for %s — failing open", symbol)
    return True


# ── Earnings candidate discovery (AV calendar — no yfinance) ────────────────

def _fetch_earnings_candidates(
    tickers: list[str], lookforward: int = 30
) -> list[dict]:
    """
    Returns list of {symbol, earnings_date, source} for symbols with
    earnings within the next `lookforward` days.

    Source: AV earnings calendar (data/market/earnings_calendar.json),
    refreshed weekly at Sunday 5 AM ET by data_warehouse.refresh_earnings_calendar_av.
    No yfinance calls. The function name is retained for test-seam
    compatibility; `tickers` is used only as an allow-list filter.
    """
    if not _CAL_PATH.exists():
        log.warning("[ROTATION] AV calendar not found at %s — no candidates", _CAL_PATH)
        return []

    try:
        raw = json.loads(_CAL_PATH.read_text())
        entries = raw.get("calendar", [])
    except Exception as exc:
        log.warning("[ROTATION] Failed to read AV calendar: %s", exc)
        return []

    today  = date.today()
    cutoff = today + timedelta(days=lookforward)
    ticker_set = {(t or "").upper() for t in tickers if t}

    candidates: list[dict] = []
    for e in entries:
        sym = (e.get("symbol") or "").upper()
        ed_str = str(e.get("earnings_date") or "")
        if not sym or not ed_str:
            continue
        if sym not in ticker_set:
            continue
        try:
            ed = date.fromisoformat(ed_str[:10])
        except ValueError:
            continue
        if today <= ed <= cutoff:
            candidates.append({
                "symbol":        sym,
                "earnings_date": ed_str[:10],
                "source":        "alphavantage",
            })

    log.info(
        "[ROTATION] AV calendar -> %d candidates within %d days (source=alphavantage)",
        len(candidates), lookforward,
    )
    return candidates


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


# ── Short-horizon expansion ───────────────────────────────────────────────────

def expand_watchlist_for_upcoming_earnings(
    days_ahead: int = 5,
    config: Optional[dict] = None,
) -> list[str]:
    """
    Add _EXTRA_UNIVERSE symbols with earnings within *days_ahead* calendar days
    to the rotation tier so they appear in signal scoring before the event.

    Designed to catch blue-chip / consumer names (SBUX, DIS, MCD …) that are
    not on the core watchlist but have imminent earnings worth tracking.

    TTL: post_earnings_cull_after = earnings_date + post_earnings_hold_days
    (defaults to 2).  _cull_post_earnings_symbols() removes them at 2 AM after
    the cull date passes — no manual cleanup required.

    Non-fatal — returns [] on any failure.
    """
    try:
        from data_warehouse import load_earnings_calendar  # noqa: PLC0415
        cal_data = load_earnings_calendar()
        entries = cal_data.get("calendar", []) if isinstance(cal_data, dict) else []
    except Exception as exc:
        log.warning("[ROTATION] expand_watchlist: calendar load failed: %s", exc)
        return []

    today  = date.today()
    cutoff = today + timedelta(days=days_ahead)

    in_watchlist: set[str] = (
        {(s.get("symbol") or "").upper() for s in get_rotation()}
        | {(s.get("symbol") or "").upper() for s in get_core()}
    )

    cfg = (config or {}).get("earnings_rotation", {}) if isinstance(config, dict) else {}
    post_hold_days = int(cfg.get("post_earnings_hold_days", 2))

    added: list[str] = []
    for e in entries:
        sym = (e.get("symbol") or "").upper()
        if not sym or sym in in_watchlist or "/" in sym:
            continue
        if sym not in _EXTRA_UNIVERSE:
            continue
        ed_str = str(e.get("earnings_date", ""))[:10]
        if not ed_str:
            continue
        try:
            ed = date.fromisoformat(ed_str)
        except ValueError:
            continue
        if not (today <= ed <= cutoff):
            continue
        cull_after = (ed + timedelta(days=post_hold_days)).isoformat()
        sector = _infer_sector(sym)
        ok = add_rotation_symbol(
            symbol=sym,
            sector=sector,
            cull_after=cull_after,
            source="earnings_expand",
            earnings_date=ed.isoformat(),
        )
        if ok:
            added.append(sym)
            log.info("[ROTATION] expand_watchlist: added %s (earnings %s, cull %s)",
                     sym, ed_str, cull_after)

    if added:
        log.info("[ROTATION] expand_watchlist: %d new symbol(s): %s", len(added), added)
    return added


# ── Main entry point ──────────────────────────────────────────────────────────

def run_earnings_rotation(config: Optional[dict] = None) -> dict:
    """
    Daily rotation run. Idempotent. Never raises.

    Steps:
      1. Discover earnings candidates (next 30 days) from the AV calendar
         (data/market/earnings_calendar.json) for the admissible universe.
         Drop ETFs explicitly — they have no earnings.
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
        candidates = _fetch_earnings_candidates(sorted(universe), lookforward=lookforward)
    except Exception as exc:
        log.warning("[ROTATION] discovery failed (non-fatal): %s", exc)

    # Drop ETFs — they don't report earnings and must never enter rotation.
    etf_syms = {
        (s if isinstance(s, str) else s.get("symbol", "")).upper()
        for s in wm.get_active_watchlist().get("etfs", [])
    }
    pre_filter = len(candidates)
    candidates = [c for c in candidates if c["symbol"] not in etf_syms]
    if pre_filter != len(candidates):
        log.info(
            "[ROTATION] ETF filter removed %d candidate(s)",
            pre_filter - len(candidates),
        )

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
        ed_raw = cand["earnings_date"]
        # _fetch_earnings_candidates emits ISO strings; tolerate date objects too
        # (e.g. legacy mocks) by normalising both shapes.
        if hasattr(ed_raw, "isoformat"):
            ed_date = ed_raw
        else:
            try:
                ed_date = date.fromisoformat(str(ed_raw)[:10])
            except ValueError:
                continue
        cull_after = (ed_date + timedelta(days=post_hold_days)).isoformat()
        sector = _infer_sector(sym)
        ok = add_rotation_symbol(
            symbol=sym,
            sector=sector,
            cull_after=cull_after,
            source="earnings_rotation",
            earnings_date=ed_date.isoformat(),
        )
        if ok:
            added.append(sym)
            new_count += 1
            # Best-effort IV fast-track
            try:
                from options_universe_manager import (
                    earnings_iv_fasttrack,  # noqa: PLC0415
                )
                earnings_iv_fasttrack(sym, ed_date)
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


# ── Earnings conviction assembler (v2) ───────────────────────────────────────

_INTEL_DIR = _BASE / "data" / "earnings_intel"
_IV_HIST_DIR = _BASE / "data" / "options" / "iv_history"
_CONVICTIONS_PATH = _BASE / "data" / "market" / "earnings_convictions.json"


def _ec_derive_direction(intel: dict) -> tuple[str, float]:
    """Return (direction, confidence) from analyst intel."""
    bullish_pct = intel.get("bullish_pct")
    rec_mean = intel.get("rec_mean")

    if bullish_pct is not None:
        if bullish_pct >= 80:
            return "bullish", min(0.90, bullish_pct / 100.0)
        if bullish_pct <= 40:
            return "bearish", min(0.90, (100.0 - bullish_pct) / 100.0)
        # 40 < bullish_pct < 80 — neutral
        return "neutral", 0.50

    if rec_mean is not None:
        if rec_mean <= 2.0:
            conf = max(0.55, (5.0 - rec_mean) / 4.0 * 0.90)
            return "bullish", conf
        if rec_mean >= 3.5:
            conf = max(0.55, (rec_mean - 1.0) / 4.0 * 0.90)
            return "bearish", conf
        return "neutral", 0.50

    return "neutral", 0.50


def _ec_consensus_label(intel: dict) -> Optional[str]:
    rec_mean = intel.get("rec_mean")
    bullish_pct = intel.get("bullish_pct")
    if rec_mean is not None:
        if rec_mean <= 1.5:
            return "strong_buy"
        if rec_mean <= 2.5:
            return "buy"
        if rec_mean <= 3.5:
            return "hold"
        if rec_mean <= 4.5:
            return "sell"
        return "strong_sell"
    if bullish_pct is not None:
        if bullish_pct >= 80:
            return "buy"
        if bullish_pct >= 60:
            return "hold"
        return "sell"
    return None


def _ec_iv_trajectory(iv_history: list) -> tuple[str, Optional[float]]:
    """Return (trajectory, current_iv_rank) from iv history entries."""
    recent = [e for e in iv_history[-7:] if e.get("iv_rank") is not None]
    if not recent:
        return "unknown", None
    current = recent[-1].get("iv_rank")
    if len(recent) < 3:
        return "flat", current
    mid = len(recent) // 2
    first_avg = sum(e.get("iv_rank", 0) for e in recent[:mid]) / mid
    second_avg = sum(e.get("iv_rank", 0) for e in recent[mid:]) / (len(recent) - mid)
    if second_avg - first_avg > 5:
        return "ramping", current
    if current is not None and current > 60:
        return "elevated", current
    return "flat", current


def _ec_vol_conviction(iv_trajectory: str, iv_rank: Optional[float]) -> str:
    if iv_rank is not None:
        if iv_rank < 40:
            return "buy_vol"
        if iv_rank > 60:
            return "sell_vol"
        return "neutral_vol"
    if iv_trajectory in ("ramping", "elevated"):
        return "sell_vol"
    return "neutral_vol"


def _ec_conviction_matrix(
    direction: str, vol_conviction: str, confidence: float
) -> tuple[str, Optional[str], Optional[str]]:
    """Return (recommended_structure, a1_signal, a2_structure)."""
    if direction == "bullish":
        if vol_conviction == "sell_vol":
            return "bull_put_spread", "enter_long", "bull_put_spread"
        if confidence >= 0.75:
            return "long_call", "enter_long", "long_call"
        return "debit_call_spread", "enter_long", "debit_call_spread"
    if direction == "bearish":
        if vol_conviction == "sell_vol":
            return "bear_call_spread", "enter_short", "bear_call_spread"
        if confidence >= 0.75:
            return "long_put", "enter_short", "long_put"
        return "debit_put_spread", "enter_short", "debit_put_spread"
    # neutral
    if vol_conviction == "sell_vol":
        return "iron_condor", None, "iron_condor"
    return "straddle", None, "straddle"


def assemble_earnings_conviction(
    symbol: str, eda: int, timing: str, config: Optional[dict] = None
) -> dict:
    """
    Assemble a pre-earnings conviction record for a single symbol.

    Returns a dict with direction, phase, beat_rate, analyst_consensus,
    IV analysis, conviction_score, a1_signal, a2_structure, and recommended_structure.
    Non-fatal — missing data files return a conviction with None fields rather than crashing.
    """
    # Load analyst intel (cache file, no network)
    intel: dict = {}
    intel_path = _INTEL_DIR / f"{symbol}_analyst_intel.json"
    if intel_path.exists():
        try:
            intel = json.loads(intel_path.read_text())
        except Exception as exc:
            log.debug("[EARNINGS] %s analyst_intel read failed: %s", symbol, exc)

    # Load IV history
    iv_history: list = []
    iv_path = _IV_HIST_DIR / f"{symbol}_iv_history.json"
    if iv_path.exists():
        try:
            raw = json.loads(iv_path.read_text())
            iv_history = raw if isinstance(raw, list) else raw.get("history", [])
        except Exception as exc:
            log.debug("[EARNINGS] %s iv_history read failed: %s", symbol, exc)

    # Phase
    if eda > 7:
        phase = "watch"
    elif eda >= 3:
        phase = "active"
    else:
        phase = "transition"

    direction, confidence = _ec_derive_direction(intel)
    consensus = _ec_consensus_label(intel)

    beat_q = intel.get("beat_quarters")
    total_q = intel.get("total_quarters")
    beat_rate: Optional[float] = round(beat_q / total_q, 3) if (beat_q is not None and total_q) else None

    pt_upside_pct = intel.get("price_target_upside_pct")
    price_target_upside: Optional[float] = round(pt_upside_pct / 100.0, 3) if pt_upside_pct is not None else None

    iv_trajectory, iv_rank = _ec_iv_trajectory(iv_history)
    vol_conv = _ec_vol_conviction(iv_trajectory, iv_rank)

    # Crowded consensus: overwhelmingly bullish + expensive IV
    bullish_pct_val = intel.get("bullish_pct") or 0.0
    crowded = bool(bullish_pct_val >= 90 and iv_rank is not None and iv_rank > 60)

    base = {
        "symbol": symbol,
        "eda": eda,
        "timing": timing,
        "phase": phase,
        "direction": direction,
        "direction_confidence": round(confidence, 3),
        "beat_rate": beat_rate,
        "avg_eps_surprise": intel.get("avg_surprise_pct"),
        "analyst_consensus": consensus,
        "price_target_upside": price_target_upside,
        "iv_rank": iv_rank,
        "iv_trajectory": iv_trajectory,
        "vol_conviction": vol_conv,
        "crowded_consensus_flag": crowded,
    }

    if phase == "watch":
        return {
            **base,
            "recommended_structure": None,
            "conviction_score": 0.0,
            "conviction_level": "skip",
            "a1_signal": None,
            "a2_structure": None,
            "notes": f"Watch phase — eda={eda}, data gathering only",
        }

    # Conviction assembly for active / transition
    structure, a1_signal, a2_structure = _ec_conviction_matrix(direction, vol_conv, confidence)

    if crowded:
        a1_signal = None
        notes = (
            f"Crowded consensus (bullish_pct={bullish_pct_val:.0f}%, IV elevated) — downgraded"
        )
    else:
        notes = f"eda={eda}, {direction} ({confidence:.0%} confidence), IV={iv_trajectory}"

    # Beat rate suppresses confidence for poor beat histories
    if beat_rate is not None and beat_rate == 0.0 and direction == "bullish":
        confidence = confidence * 0.6
        notes += " | beat_rate=0 suppresses confidence"

    score_parts = [confidence]
    if beat_rate is not None:
        score_parts.append(beat_rate)
    if bullish_pct_val:
        score_parts.append(bullish_pct_val / 100.0)
    conviction_score = sum(score_parts) / len(score_parts)
    if crowded:
        conviction_score *= 0.6

    conviction_score = round(conviction_score, 3)

    if conviction_score >= 0.75:
        conviction_level = "high"
    elif conviction_score >= 0.55:
        conviction_level = "medium"
    elif conviction_score >= 0.35:
        conviction_level = "low"
    else:
        conviction_level = "skip"

    return {
        **base,
        "recommended_structure": structure,
        "conviction_score": conviction_score,
        "conviction_level": conviction_level,
        "a1_signal": a1_signal,
        "a2_structure": a2_structure,
        "notes": notes,
    }


def scan_earnings_candidates(lookforward: int = 14, config: Optional[dict] = None) -> list:
    """
    Scan earnings_calendar.json for upcoming symbols and assemble conviction records.

    Writes results to data/market/earnings_convictions.json.
    Returns list of conviction dicts sorted by conviction_score descending.
    Non-fatal — returns [] on calendar read failure.
    """
    if not _CAL_PATH.exists():
        log.warning("[EARNINGS] earnings_calendar.json not found — conviction scan skipped")
        return []
    try:
        cal_data = json.loads(_CAL_PATH.read_text())
    except Exception as exc:
        log.warning("[EARNINGS] calendar read failed: %s", exc)
        return []

    today = date.today()
    candidates = []
    active_count = 0

    for entry in cal_data.get("calendar", []):
        sym = (entry.get("symbol") or "").upper()
        if not sym or "/" in sym:
            continue
        ed_raw = str(entry.get("earnings_date", ""))[:10]
        try:
            ed = date.fromisoformat(ed_raw)
        except ValueError:
            continue
        eda = (ed - today).days
        if not (0 <= eda <= lookforward):
            continue
        timing = str(entry.get("timing") or "unknown")
        conviction = assemble_earnings_conviction(sym, eda, timing, config)
        candidates.append(conviction)
        if conviction["phase"] in ("active", "transition"):
            active_count += 1

    candidates.sort(key=lambda c: c["conviction_score"], reverse=True)

    try:
        _CONVICTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CONVICTIONS_PATH.write_text(json.dumps(
            {"generated_at": datetime.now(timezone.utc).isoformat(), "candidates": candidates},
            indent=2,
        ))
    except Exception as exc:
        log.warning("[EARNINGS] earnings_convictions.json write failed: %s", exc)

    active_syms = [c["symbol"] for c in candidates if c["phase"] in ("active", "transition")]
    log.info(
        "[EARNINGS] %d candidates scanned, %d in active phase, top: %s",
        len(candidates), active_count, active_syms[:5],
    )
    return candidates
