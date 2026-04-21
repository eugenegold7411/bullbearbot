"""bot_options.py — Account 2 options bot (thin orchestrator). Wires stage 0-4."""
import json
import os
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from log_setup import get_logger

load_dotenv()
log = get_logger("bot_options")
ET = ZoneInfo("America/New_York")
_A2_DIR = Path(__file__).parent / "data" / "account2"


def _ensure_dirs() -> None:
    for d in [_A2_DIR, _A2_DIR / "trade_memory", _A2_DIR / "costs", _A2_DIR / "positions"]:
        d.mkdir(parents=True, exist_ok=True)


# Lazy-init clients — test_import_safety.py asserts both are None at import time
_alpaca = None
_claude = None


def _get_alpaca():
    global _alpaca
    if _alpaca is None:
        from alpaca.trading.client import TradingClient  # noqa: PLC0415
        api_key = os.getenv("ALPACA_API_KEY_OPTIONS")
        secret  = os.getenv("ALPACA_SECRET_KEY_OPTIONS")
        base    = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
        if not api_key or not secret:
            raise EnvironmentError("ALPACA_API_KEY_OPTIONS / ALPACA_SECRET_KEY_OPTIONS not set")
        _alpaca = TradingClient(api_key=api_key, secret_key=secret, paper=("paper" in base))
    return _alpaca


# Backward-compat re-exports — tests import these from bot_options
from bot_options_stage0_preflight import (  # noqa: E402
    _OBS_IV_SYMBOLS,  # noqa: F401
    _OBS_MODE_DAYS,  # noqa: F401
    _OBS_MODE_FILE,  # noqa: F401
    _OBS_SCHEMA_VERSION,  # noqa: F401
    _check_and_update_iv_ready,  # noqa: F401
    _get_obs_mode_state,
    _is_trading_day,  # noqa: F401
    _update_obs_mode_state,
    is_observation_mode,  # noqa: F401
)
from bot_options_stage1_candidates import _build_a2_feature_pack  # noqa: F401
from bot_options_stage2_structures import (  # noqa: F401
    _STRATEGY_FROM_STRUCTURE,  # noqa: F401
    _apply_veto_rules,  # noqa: F401
    _quick_liquidity_check,  # noqa: F401
    _route_strategy,  # noqa: F401
)
from bot_options_stage3_debate import _parse_bounded_debate_response  # noqa: F401


def _persist_early_exit(session_tier: str, t_start: float, no_trade_reason: str) -> None:
    """Persist a minimal A2DecisionRecord for cycles that exit before Stage 3."""
    try:
        from bot_options_stage4_execution import (
            persist_decision_record,  # noqa: PLC0415
        )
        from schemas import A2DecisionRecord  # noqa: PLC0415
        rec = A2DecisionRecord(
            decision_id="",
            session_tier=session_tier,
            candidate_sets=[],
            debate_input=None,
            debate_output_raw=None,
            debate_parsed=None,
            selected_candidate=None,
            execution_result="no_trade",
            no_trade_reason=no_trade_reason,
            elapsed_seconds=time.monotonic() - t_start,
        )
        persist_decision_record(rec)
    except Exception as _exc:
        log.debug("[OPTS] _persist_early_exit failed (non-fatal): %s", _exc)


def run_options_cycle(session_tier: str = "market", next_cycle_time: str = "?") -> None:
    """Run one Account 2 options cycle."""
    _ensure_dirs()
    t_start = time.monotonic()
    log.info("── [OPTS] Cycle start  session=%s ─────────────────────────", session_tier)

    from bot_options_stage0_preflight import run_a2_preflight  # noqa: PLC0415
    pf = run_a2_preflight(session_tier, _get_alpaca())
    if pf.halt:
        _persist_early_exit(session_tier, t_start, "preflight_halt")
        return
    equity = pf.equity
    pf_allow_live_orders = pf.pf_allow_live_orders
    pf_allow_new_entries = pf.pf_allow_new_entries
    a2_mode = pf.a2_mode

    obs_state = _get_obs_mode_state()
    obs_mode  = _update_obs_mode_state(obs_state)
    vix = 20.0; regime = "normal"  # noqa: E702
    try:
        _vc = Path(__file__).parent / "data" / "market" / "vix_cache.json"
        if _vc.exists() and (time.time() - _vc.stat().st_mtime) < 600:
            vix = float(json.loads(_vc.read_text()).get("vix", vix))
    except Exception:
        pass

    from bot_options_stage1_candidates import (  # noqa: PLC0415
        _get_core_equity_symbols,
        _get_iv_summaries_for_symbols,
        _load_account1_last_decision,
        _summarize_account1_for_prompt,
        load_a1_signals,
        run_candidate_stage,
    )
    from bot_options_stage3_debate import _load_strategy_config  # noqa: PLC0415
    from bot_options_stage4_execution import save_legacy_decision  # noqa: PLC0415

    a1_decision = _load_account1_last_decision()
    regime = a1_decision.get("regime", regime) if isinstance(a1_decision, dict) else regime
    account1_summary = _summarize_account1_for_prompt(a1_decision)
    signal_scores = load_a1_signals()
    if not signal_scores:
        log.info("[OPTS] No signal scores from Account 1 — skipping debate (no catalyst context)")
        save_legacy_decision({"reasoning": "no_account1_signals", "regime": regime,
                              "actions": [], "observation_mode": obs_mode})
        _persist_early_exit(session_tier, t_start, "no_signal_scores")
        return

    iv_summaries = _get_iv_summaries_for_symbols(list(signal_scores.keys())[:20])
    log.info("[OPTS] IV summaries: %d symbols, %d in obs mode", len(iv_summaries),
             sum(1 for iv in iv_summaries.values() if iv.get("observation_mode", True)))

    try:
        import options_universe_manager as _oum  # noqa: PLC0415
        _uni = Path(__file__).parent / "data" / "options" / "universe.json"
        if not _uni.exists():
            log.info("[OPTS] universe.json absent — initializing from IV history")
            _oum.initialize_universe_from_existing_iv_history()
    except Exception as _ue:
        log.debug("[OPTS] universe init failed (non-fatal): %s", _ue)

    config = _load_strategy_config()
    candidate_sets, candidates, allowed_by_sym, candidate_structures = run_candidate_stage(
        signal_scores=signal_scores, iv_summaries=iv_summaries,
        equity=equity, vix=vix, equity_symbols=_get_core_equity_symbols(), config=config,
    )
    log.info("[OPTS] Candidates: %d  candidate_structures: %d  VIX=%.1f  regime=%s",
             len(candidates), len(candidate_structures), vix, regime)

    if not candidates and not obs_mode:
        log.info("[OPTS] No candidates after filtering — holding")
        save_legacy_decision({"reasoning": "no_candidates_after_filter", "regime": regime,
                              "actions": [], "observation_mode": obs_mode})
        _persist_early_exit(session_tier, t_start, "no_candidates_after_veto")
        return

    from bot_options_stage3_debate import run_bounded_debate  # noqa: PLC0415
    decision_record = run_bounded_debate(
        candidate_sets=candidate_sets, candidates=candidates,
        candidate_structures=candidate_structures, allowed_by_sym=allowed_by_sym,
        equity=equity, vix=vix, regime=regime, account1_summary=account1_summary,
        obs_mode=obs_mode, session_tier=session_tier, iv_summaries=iv_summaries,
        t_start=t_start, config=config,
    )

    from bot_options_stage4_execution import (  # noqa: PLC0415
        close_check_loop,
        persist_decision_record,
        submit_selected_candidate,
    )
    submit_selected_candidate(
        decision_record=decision_record, alpaca_client=_get_alpaca(),
        candidates=candidates, candidate_structures=candidate_structures,
        iv_summaries=iv_summaries, equity=equity,
        pf_allow_new_entries=pf_allow_new_entries, pf_allow_live_orders=pf_allow_live_orders,
        obs_mode=obs_mode, a2_mode=a2_mode,
    )
    persist_decision_record(decision_record)
    close_check_loop(_get_alpaca())
    elapsed = time.monotonic() - t_start
    log.info("── [OPTS] Cycle done  %.1fs  obs=%s  executed=%s ─────────────",
             elapsed, obs_mode, decision_record.execution_result)


if __name__ == "__main__":
    import sys
    run_options_cycle(session_tier=sys.argv[1] if len(sys.argv) > 1 else "market")
