"""
bot_options_stage1_candidates.py — A2 Stage 1: candidate generation.

Public API:
  load_a1_signals() -> dict
  run_candidate_stage(signal_scores, iv_summaries, equity, vix,
                      equity_symbols, config)
      -> (list[A2CandidateSet], list[StructureProposal], dict, list[dict])

Responsibilities:
  - Load A1 signal scores and IV summaries
  - Build A2FeaturePack per symbol
  - Call Stage 2 routing + veto
  - Run options_intelligence.select_options_strategy
  - Assemble A2CandidateSet per symbol
"""

from __future__ import annotations

import math
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from log_setup import get_logger

log = get_logger(__name__)


# ── A1 signal loading ─────────────────────────────────────────────────────────

def load_a1_signals() -> dict:
    """
    Load Account 1's signal scores from the most recent score file.
    Falls back to empty dict if not available.
    """
    try:
        # Account 1 may have written signal scores to data/market/signal_scores.json
        path = Path(__file__).parent / "data" / "market" / "signal_scores.json"
        if path.exists() and (time.time() - path.stat().st_mtime) < 600:  # fresh < 10 min
            import json  # noqa: PLC0415
            data = json.loads(path.read_text())
            # score_signals() returns {"scored_symbols": {...}, "top_3": [...], ...}
            # Extract the flat symbol→score dict from the nested structure.
            return data.get("scored_symbols", data) if isinstance(data, dict) else {}
    except Exception as exc:
        log.debug("[OPTS] Could not load Account 1 signal scores: %s", exc)
    return {}


def _get_core_equity_symbols() -> list[str]:
    """
    Get equity (non-crypto) symbols from core watchlist.
    Options only trade on equities; crypto symbols are excluded.
    """
    try:
        import watchlist_manager as wm  # noqa: PLC0415
        wl = wm.get_active_watchlist()
        return wl.get("stocks", []) + wl.get("etfs", [])
    except Exception as exc:
        log.debug("[OPTS] Could not load watchlist: %s", exc)
        # Fallback — full optionable universe from watchlist_core.json (non-crypto)
        # plus legacy symbols from original A2 bootstrap that are outside watchlist_core.
        # Crypto (BTC/USD, ETH/USD) excluded — options not available.
        # Symbols without IV history will be queued for bootstrap by is_tradeable().
        return [
            # Technology
            "NVDA", "TSM", "MSFT", "CRWV", "PLTR", "ASML",
            # Energy
            "XLE", "XOM", "CVX", "USO",
            # Commodities
            "GLD", "SLV", "COPX",
            # Financials
            "JPM", "GS", "XLF",
            # Consumer
            "AMZN", "WMT", "XRT",
            # Defense
            "LMT", "RTX", "ITA",
            # Biotech / Health
            "XBI", "JNJ", "LLY",
            # International
            "EWJ", "FXI", "EEM", "EWM", "ECH",
            # Macro
            "SPY", "QQQ", "IWM", "TLT", "VXX",
            # Shipping / Housing / Utilities
            "FRO", "STNG", "RKT", "BE",
            # Legacy bootstrap symbols (from original A2 Phase 1, not in watchlist_core)
            "AAPL", "META", "GOOGL", "AMD",
        ]


# ── IV summaries ──────────────────────────────────────────────────────────────

def _get_iv_summaries_for_symbols(symbols: list[str]) -> dict:
    """
    Return IV summaries for a list of symbols.
    Uses cached chains (refreshed in 4 AM block) + on-demand fetch if cache miss.
    Non-fatal per symbol.
    """
    import options_data  # noqa: PLC0415
    summaries = {}
    for sym in symbols:
        try:
            chain = options_data.fetch_options_chain(sym)  # uses cache
            summary = options_data.get_iv_summary(sym, chain=chain)
            summaries[sym] = summary
        except Exception as exc:
            log.debug("[OPTS] IV summary failed for %s: %s", sym, exc)
            summaries[sym] = {
                "symbol": sym, "iv_environment": "unknown",
                "observation_mode": True, "history_days": 0,
            }
    return summaries


# ── Account 1 context ─────────────────────────────────────────────────────────

def _load_account1_last_decision() -> dict:
    """
    Read Account 1's last decision from memory/decisions.json.
    Returns {} if not available. Non-fatal.
    """
    import json  # noqa: PLC0415
    try:
        path = Path(__file__).parent / "data" / "trade_memory" / "decisions.json"
        if not path.exists():
            # Try alternate path
            path = Path(__file__).parent / "memory" / "decisions.json"
        if path.exists():
            data = json.loads(path.read_text())
            # Could be a list (JSONL-style) or a dict
            if isinstance(data, list) and data:
                return data[-1]  # most recent
            if isinstance(data, dict):
                return data
    except Exception as exc:
        log.debug("[OPTS] Could not load Account 1 decisions: %s", exc)
    return {}


def _summarize_account1_for_prompt(decision: dict) -> str:
    """Format Account 1's last decision as a compact prompt section."""
    if not decision:
        return "  Account 1: no recent decisions available."

    regime = decision.get("regime", "unknown")
    actions = decision.get("actions", [])
    reasoning = decision.get("reasoning", "")
    ts = decision.get("timestamp", "")[:16] if decision.get("timestamp") else ""

    lines = [f"  Account 1 last cycle [{ts}]:"]
    lines.append(f"    Regime: {regime}")
    if reasoning:
        lines.append(f"    Read: {reasoning[:150]}")

    open_syms = [a.get("symbol") for a in actions if a.get("action") not in ("hold",) and a.get("symbol")]
    if open_syms:
        lines.append(f"    Active trades: {', '.join(open_syms[:8])}")

    return "\n".join(lines)


# ── Earnings context ──────────────────────────────────────────────────────────

# ETFs and funds that have no earnings reports. Alpha Vantage's earnings
# calendar covers US equities only; these symbols always return None —
# that is correct and expected. The router's earnings rules safely skip
# when earnings_days_away is None.
#
# ASML, TSM, GOOGL, AMZN, META are absent from AV's US earnings calendar (foreign
# ADRs or symbols AV periodically omits).  They are covered by
# data/market/earnings_overrides.json, which _load_earnings_days_away() merges
# at call-time so override entries are indistinguishable from AV entries.
_EARNINGS_EXEMPT_SYMBOLS: frozenset[str] = frozenset({
    "COPX", "ECH", "EEM", "EWJ", "EWM", "FXI",
    "GLD", "ITA", "IWM", "QQQ", "SLV", "SPY",
    "TLT", "USO", "VXX", "XBI", "XLE", "XLF", "XRT",
})


def _load_earnings_days_away(symbol: str) -> Optional[int]:
    """
    Return days until the nearest upcoming earnings event (positive int), or the
    days since the most recent past earnings (negative int), or None if unknown.

    - Returns None for symbols in _EARNINGS_EXEMPT_SYMBOLS (ETFs / funds)
    - Returns positive int: days until upcoming earnings (existing behavior)
    - Returns negative int: days since most recent past earnings (new — enables
      RULE_POST_EARNINGS to detect the post-earnings IV crush window)
    - Returns None: no entries found in the AV calendar
    """
    import json  # noqa: PLC0415
    if symbol.upper() in _EARNINGS_EXEMPT_SYMBOLS:
        log.debug("[EARNINGS] %s — ETF/fund, no earnings expected (exempt)", symbol)
        return None
    try:
        cal_path = Path(__file__).parent / "data" / "market" / "earnings_calendar.json"
        ovr_path = Path(__file__).parent / "data" / "market" / "earnings_overrides.json"
        if not cal_path.exists():
            return None
        cal = json.loads(cal_path.read_text())
        if ovr_path.exists():
            try:
                ovrs = json.loads(ovr_path.read_text())
                if isinstance(ovrs, dict) and ovrs:
                    ovr_syms = {k.upper() for k in ovrs}
                    entries = [e for e in cal.get("calendar", [])
                               if (e.get("symbol") or "").upper() not in ovr_syms]
                    for raw_sym, ovr_data in ovrs.items():
                        entries.append({
                            "symbol": raw_sym.upper(),
                            "earnings_date": ovr_data.get("earnings_date", ""),
                            "timing": ovr_data.get("timing", "unknown"),
                            "eps_estimate": None,
                            "source": ovr_data.get("source", "manual"),
                        })
                    cal = dict(cal)
                    cal["calendar"] = entries
            except Exception:
                pass
        today = date.today()
        min_future: Optional[int] = None   # smallest non-negative (upcoming)
        max_past:   Optional[int] = None   # largest negative (most recent past, closest to 0)
        for entry in cal.get("calendar", []):
            if entry.get("symbol", "").upper() != symbol.upper():
                continue
            raw = entry.get("earnings_date", "")
            if not raw:
                continue
            try:
                days = (date.fromisoformat(str(raw)[:10]) - today).days
                if days >= 0:
                    min_future = days if min_future is None else min(min_future, days)
                else:
                    max_past = days if max_past is None else max(max_past, days)
            except Exception:
                continue
        if min_future is not None:
            return min_future
        if max_past is not None:
            log.debug("[EARNINGS] %s — no upcoming earnings; most recent past %d days ago",
                      symbol, abs(max_past))
            return max_past
        log.debug("[EARNINGS] %s — no entry in AV calendar", symbol)
        return None
    except Exception:
        return None


# ── Feature pack ──────────────────────────────────────────────────────────────

def _build_a2_feature_pack(
    symbol: str,
    signal_scores: dict,
    iv_summaries: dict,
    equity: float,
    vix: float,
    chain: dict | None = None,
) -> Optional[object]:
    """
    Build a normalized A2FeaturePack from available data.
    Returns None if required data is missing (IV summary required).
    Flow/GEX fields are all None until UW is integrated (Phase 2).
    """
    from schemas import A2FeaturePack  # noqa: PLC0415

    sig = signal_scores.get(symbol, {})
    iv  = iv_summaries.get(symbol, {})

    if not iv or iv.get("iv_environment", "unknown") == "unknown":
        log.debug("[OPTS] A2FeaturePack: %s — missing IV summary, skipping", symbol)
        return None

    iv_rank = iv.get("iv_rank")
    if iv_rank is None:
        log.debug("[OPTS] A2FeaturePack: %s — iv_rank=None, skipping", symbol)
        return None

    a1_direction = str(sig.get("direction", "neutral")).lower()
    if a1_direction not in ("bullish", "bearish", "neutral"):
        a1_direction = "neutral"

    # expected_move_pct: IV × sqrt(30/252) expressed as percentage
    current_iv = iv.get("current_iv") or 0.0
    expected_move_pct = round(current_iv * math.sqrt(30 / 252) * 100, 2) if current_iv > 0 else 0.0

    # Liquidity score from chain data (0-1); default 0.5 when chain unavailable
    liquidity_score = 0.5
    if chain and chain.get("expirations") and chain.get("current_price"):
        try:
            spot = float(chain["current_price"])
            exp_data = next(
                (v for k, v in sorted(chain["expirations"].items())
                 if (date.fromisoformat(k) - date.today()).days >= 2),
                None,
            )
            if exp_data:
                opts = exp_data.get("calls", []) or exp_data.get("puts", [])
                if opts:
                    atm = min(opts, key=lambda o: abs(float(o.get("strike", 0)) - spot))
                    oi  = int(atm.get("openInterest", 0) or 0)
                    vol = int(atm.get("volume", 0) or 0)
                    bid = float(atm.get("bid", 0) or 0)
                    ask = float(atm.get("ask", 0) or 0)
                    mid = (bid + ask) / 2 if (bid + ask) > 0 else 0
                    spread_pct = (ask - bid) / mid if mid > 0 else 1.0
                    oi_score      = min(1.0, oi / 500)
                    vol_score     = min(1.0, vol / 50)
                    spread_score  = max(0.0, 1.0 - spread_pct / 0.10)
                    liquidity_score = round((oi_score + vol_score + spread_score) / 3, 3)
        except Exception as _liq_exc:
            log.debug("[OPTS] A2FeaturePack: %s liquidity calc failed: %s", symbol, _liq_exc)

    # flow_imbalance_30m: (call_vol - put_vol) / (call_vol + put_vol) across ATM contracts
    flow_imbalance_30m = None
    if chain and chain.get("expirations") and chain.get("current_price"):
        try:
            _fi_spot = float(chain["current_price"])
            _fi_exp = next(
                (v for k, v in sorted(chain["expirations"].items())
                 if (date.fromisoformat(k) - date.today()).days >= 2),
                None,
            )
            if _fi_exp:
                _atm_band = _fi_spot * 0.05
                _call_vol = sum(
                    int(o.get("volume") or 0)
                    for o in _fi_exp.get("calls", [])
                    if abs(float(o.get("strike", 0)) - _fi_spot) <= _atm_band
                )
                _put_vol = sum(
                    int(o.get("volume") or 0)
                    for o in _fi_exp.get("puts", [])
                    if abs(float(o.get("strike", 0)) - _fi_spot) <= _atm_band
                )
                _total_vol = _call_vol + _put_vol
                if _total_vol > 0:
                    flow_imbalance_30m = round((_call_vol - _put_vol) / _total_vol, 4)
        except Exception as _fi_exc:
            log.debug("[OPTS] A2FeaturePack: %s flow_imbalance calc failed: %s", symbol, _fi_exc)

    data_sources = ["signal_scores", "iv_history"]
    if chain:
        data_sources.append("options_chain")
    if flow_imbalance_30m is not None:
        data_sources.append("flow_signals")

    pack = A2FeaturePack(
        symbol               = symbol,
        a1_signal_score      = float(sig.get("score", 0)),
        a1_direction         = a1_direction,
        trend_score          = None,
        momentum_score       = None,
        sector_alignment     = str(sig.get("sector_signal", sig.get("sector", ""))),
        iv_rank              = float(iv_rank),
        iv_environment       = str(iv.get("iv_environment", "unknown")),
        term_structure_slope = None,
        skew                 = None,
        expected_move_pct    = expected_move_pct,
        flow_imbalance_30m   = flow_imbalance_30m,
        sweep_count          = None,
        gex_regime           = None,
        oi_concentration     = None,
        earnings_days_away   = _load_earnings_days_away(symbol),
        macro_event_flag     = False,
        premium_budget_usd   = round(equity * 0.05, 2),
        liquidity_score      = liquidity_score,
        built_at             = datetime.now(timezone.utc).isoformat(),
        data_sources         = data_sources,
    )
    log.debug("[OPTS] A2FeaturePack built for %s: iv_rank=%.1f env=%s dir=%s earn=%s liq=%.2f",
              symbol, pack.iv_rank, pack.iv_environment, pack.a1_direction,
              pack.earnings_days_away, pack.liquidity_score)
    return pack


# ── Candidate set assembly ────────────────────────────────────────────────────

def build_candidate_set(
    symbol: str,
    pack,
    equity: float,
    chain: dict,
    allowed_structures: list[str],
    config: dict | None = None,
    buying_power: float = 0.0,
) -> object:
    """
    Build an A2CandidateSet for a single symbol using Stage 2 routing and veto.
    Returns an A2CandidateSet.
    """
    from bot_options_stage2_structures import (  # noqa: PLC0415
        _infer_router_rule_fired,
        build_candidate_structures,
    )
    from schemas import A2CandidateSet  # noqa: PLC0415

    generation_errors: list[str] = []
    generated, vetoed, surviving = [], [], []

    try:
        generated, vetoed, surviving = build_candidate_structures(
            pack=pack,
            equity=equity,
            chain=chain,
            allowed_structures=allowed_structures,
            config=config,
            buying_power=buying_power,
        )
    except Exception as exc:
        generation_errors.append(str(exc))
        log.debug("[OPTS] %s: build_candidate_set error (non-fatal): %s", symbol, exc)

    rule_fired = _infer_router_rule_fired(pack, allowed_structures, config=config)

    return A2CandidateSet(
        symbol=symbol,
        pack=pack,
        allowed_structures=allowed_structures,
        router_rule_fired=rule_fired,
        generated_candidates=generated,
        vetoed_candidates=vetoed,
        surviving_candidates=surviving,
        generation_errors=generation_errors,
        built_at=datetime.now(timezone.utc).isoformat(),
    )


# ── Main stage function ───────────────────────────────────────────────────────

def run_candidate_stage(
    signal_scores: dict,
    iv_summaries: dict,
    equity: float,
    vix: float,
    equity_symbols: list[str],
    config: dict | None = None,
    buying_power: float = 0.0,
) -> tuple[list, list, dict, list[dict]]:
    """
    Build A2CandidateSets and StructureProposals for all qualified symbols.

    Returns:
      (candidate_sets, proposals, allowed_by_sym, all_candidate_structures)
      - candidate_sets:           list[A2CandidateSet]
      - proposals:                list[StructureProposal] (for legacy debate path)
      - allowed_by_sym:           dict[str, list[str]]
      - all_candidate_structures: list[dict] (flat surviving candidates)
    """
    if config is None:
        config = {}

    # Rollback flag check — both force_no_trade and disable_candidate_generation
    # result in an empty return from this stage. Checked before any imports so
    # the function exits cleanly even in test environments without venv packages.
    _rollback = config.get("a2_rollback", {})
    if _rollback.get("force_no_trade") or _rollback.get("disable_candidate_generation"):
        _flag = "force_no_trade" if _rollback.get("force_no_trade") else "disable_candidate_generation"
        log.warning("[OPTS] Rollback flag active: %s — skipping candidate generation", _flag)
        return [], [], {}, []

    import options_data  # noqa: PLC0415
    import options_intelligence  # noqa: PLC0415
    from bot_options_stage2_structures import (  # noqa: PLC0415
        _quick_liquidity_check,
        _route_strategy,
    )

    options_regime = options_data.get_options_regime(vix)

    candidate_sets: list = []
    proposals: list      = []
    allowed_by_sym: dict = {}
    all_candidate_structs: list[dict] = []

    # Filter to symbols that have a meaningful signal.
    scored_symbols = sorted(
        [(sym, sig) for sym, sig in signal_scores.items()
         if isinstance(sig, dict) and sig.get("conviction") in ("medium", "high")],
        key=lambda x: {"high": 2, "medium": 1, "low": 0}.get(x[1].get("conviction", "low"), 0),
        reverse=True
    )[:8]  # top 8 candidates only

    # Symbols with a pending mleg DAY order from a prior cycle — skip re-submission.
    _pending_underlyings = config.get("_pending_underlyings", frozenset())

    for sym, sig_data in scored_symbols:
        if sym in _pending_underlyings:
            log.info("[OPTS] %s: skipping — mleg order pending from prior cycle", sym)
            continue
        iv_summary = iv_summaries.get(sym, {
            "symbol": sym, "iv_environment": "unknown", "observation_mode": True
        })

        # Universe tradeable gate
        try:
            import options_universe_manager as _oum  # noqa: PLC0415
            if not _oum.is_tradeable(sym):
                log.debug("[OPTS] %s: not in tradeable universe — queued for bootstrap", sym)
                continue
        except Exception as _ue:
            log.debug("[OPTS] %s: universe check failed (non-fatal): %s", sym, _ue)

        tier          = sig_data.get("tier", "core")
        catalyst      = sig_data.get("primary_catalyst", "no specific catalyst")
        current_price = sig_data.get("price", 0)

        if current_price <= 0:
            log.debug("[OPTS] %s: no price data — skipping", sym)
            continue

        # Fetch chain once — used by both liquidity check and A2FeaturePack
        chain: dict = {}
        try:
            chain = options_data.fetch_options_chain(sym) or {}
        except Exception as _ce:
            log.debug("[OPTS] %s: chain fetch failed (non-fatal): %s", sym, _ce)

        # Build A2FeaturePack
        pack = _build_a2_feature_pack(
            symbol=sym,
            signal_scores=signal_scores,
            iv_summaries=iv_summaries,
            equity=equity,
            vix=vix,
            chain=chain or None,
        )

        # Deterministic strategy router — gates debate before AI sees this candidate
        if pack is not None:
            allowed = _route_strategy(pack, config=config)
            if not allowed:
                log.debug("[OPTS] %s: routing gate blocked — no allowed structures", sym)
                # Build candidate set (with empty surviving) to track the routing decision
                cset = build_candidate_set(sym, pack, equity, chain, allowed,
                                           config=config, buying_power=buying_power)
                candidate_sets.append(cset)
                continue
            allowed_by_sym[sym] = allowed

            # Build A2CandidateSet (includes routing + veto results via Stage 2)
            cset = build_candidate_set(sym, pack, equity, chain, allowed,
                                       config=config, buying_power=buying_power)
            candidate_sets.append(cset)

            if not cset.surviving_candidates:
                continue

            all_candidate_structs.extend(cset.surviving_candidates)

            # Enrich surviving candidates with A1 signal context for debate prompt rendering
            _a1_dir = pack.a1_direction if pack is not None else "unknown"
            for _c in cset.surviving_candidates:
                _c["a1_direction"]        = _a1_dir
                _c["a1_conviction"]       = sig_data.get("conviction", "unknown")
                _c["a1_score"]            = int(sig_data.get("score", 0))
                _c["a1_primary_catalyst"] = sig_data.get("primary_catalyst", "")

        # Build StructureProposal (for debate + legacy path)
        proposal = options_intelligence.select_options_strategy(
            symbol=sym,
            iv_summary=iv_summary,
            signal_data=sig_data,
            vix=vix,
            tier=tier,
            catalyst=catalyst,
            current_price=current_price,
            equity=equity,
            options_regime=options_regime,
            buying_power=buying_power,
        )
        if proposal is None:
            continue

        # Pre-debate liquidity pre-screen (loose thresholds — 50% of full gate)
        if chain:
            try:
                liq_ok, liq_reason = _quick_liquidity_check(chain, proposal, config)
                if not liq_ok:
                    log.debug("[OPTS] %s: pre-debate liquidity fail — %s", sym, liq_reason)
                    continue
            except Exception as _liq_err:
                log.debug("[OPTS] %s: pre-debate liquidity check error (passing): %s", sym, _liq_err)

        proposals.append(proposal)

    return candidate_sets, proposals, allowed_by_sym, all_candidate_structs
