"""
bot_options_stage4_execution.py — A2 Stage 4: execution and persistence.

Public API:
  submit_selected_candidate(decision_record, alpaca_client, candidates,
                             candidate_structures, iv_summaries, equity,
                             pf_allow_new_entries, pf_allow_live_orders,
                             obs_mode, a2_mode) -> str
  close_check_loop(alpaca_client) -> None
  persist_decision_record(decision_record) -> None
  save_legacy_decision(cycle_result) -> None  (backward compat)

Responsibilities:
  - Execute the selected candidate (bounded path)
  - Execute legacy free-form actions
  - Close-check and roll evaluation for open structures
  - Persist A2DecisionRecord to data/account2/decisions/
  - Legacy decisions_account2.json log (backward compat)
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any  # Any used in submit_selected_candidate signature
from zoneinfo import ZoneInfo

from log_setup import get_logger

log = get_logger(__name__)

ET = ZoneInfo("America/New_York")

_A2_DIR        = Path(__file__).parent / "data" / "account2"
_DECISION_LOG  = _A2_DIR / "trade_memory" / "decisions_account2.json"
_DECISIONS_DIR = _A2_DIR / "decisions"

from bot_options_stage2_structures import _STRATEGY_FROM_STRUCTURE


def _get_strategy_map() -> dict:
    return _STRATEGY_FROM_STRUCTURE


# ── Open positions ─────────────────────────────────────────────────────────────

def _get_open_options_positions(alpaca_client) -> list:
    """Get open options positions from Account 2."""
    try:
        positions = alpaca_client.get_all_positions()
        # Options positions have symbols like AAPL230120C00150000
        opts = [p for p in positions if len(getattr(p, "symbol", "")) > 10
                and any(c in getattr(p, "symbol", "") for c in ("C", "P"))]
        return opts
    except Exception as exc:
        log.warning("[OPTS] Could not fetch Account 2 positions: %s", exc)
        return []


def _check_expiring_positions(positions: list, alpaca_client) -> list[str]:
    """
    Check for options positions expiring within 5 days.
    Returns list of symbols that should be reviewed for close.
    """
    warn_symbols = []
    today = date.today()

    for pos in positions:
        sym = getattr(pos, "symbol", "")
        if len(sym) < 15:
            continue
        try:
            # OCC format: AAPL230120C00150000 — extract YYMMDD (positions 4-10 from root)
            # Find the first digit after the underlying letters
            i = 0
            while i < len(sym) and not sym[i].isdigit():
                i += 1
            if i >= len(sym):
                continue
            date_str = sym[i:i+6]  # YYMMDD
            exp_date = date(2000 + int(date_str[:2]), int(date_str[2:4]), int(date_str[4:6]))
            dte = (exp_date - today).days
            if dte <= 5:
                log.warning("[OPTS] %s: expires in %d days — consider closing", sym, dte)
                warn_symbols.append(sym)
        except Exception:
            continue
    return warn_symbols


# ── Execution ─────────────────────────────────────────────────────────────────

def submit_selected_candidate(
    decision_record,
    alpaca_client,
    candidates: list,
    candidate_structures: list[dict],
    iv_summaries: dict,
    equity: float,
    pf_allow_new_entries: bool,
    pf_allow_live_orders: bool,
    obs_mode: bool,
    a2_mode: Any,
) -> str:
    """
    Execute the candidate selected by the debate (or legacy free-form path).
    Updates decision_record.execution_result in place.
    Returns execution_result string: "submitted"|"rejected"|"no_trade"|"error".
    """
    import options_builder  # noqa: PLC0415
    import options_data  # noqa: PLC0415
    import options_state  # noqa: PLC0415
    import order_executor_options as oe_opts  # noqa: PLC0415

    strategy_map = _get_strategy_map()
    debate_result = decision_record.debate_parsed or {}
    execution_results: list[dict] = []

    if not pf_allow_new_entries:
        log.warning("[PREFLIGHT] New A2 entries suppressed by preflight (reconcile_only)")

    # ── A2-3b bounded execution path ─────────────────────────────────────────
    if candidate_structures and "selected_candidate_id" in debate_result:
        _reject   = debate_result.get("reject", True)
        _sel_id   = debate_result.get("selected_candidate_id")
        _conf     = float(debate_result.get("confidence", 0.0))
        _size_mod = float(debate_result.get("recommended_size_modifier", 1.0))

        if _reject or not _sel_id:
            log.info("[OPTS] Bounded debate: reject — %s",
                     debate_result.get("reasons", "")[:100])
            decision_record.execution_result = "no_trade"
            decision_record.no_trade_reason = "debate_rejected_all"
            return "no_trade"

        if _conf < 0.85:
            log.info("[OPTS] Bounded debate: confidence=%.2f < 0.85 — holding", _conf)
            decision_record.execution_result = "no_trade"
            decision_record.no_trade_reason = "debate_low_confidence"
            return "no_trade"

        selected_cand = next(
            (c for c in candidate_structures if c.get("candidate_id") == _sel_id), None
        )
        if selected_cand is None:
            log.warning("[OPTS] Bounded debate selected_candidate_id=%s not found — holding", _sel_id)
            decision_record.execution_result = "no_trade"
            decision_record.no_trade_reason = "debate_rejected_all"
            return "no_trade"

        sym = selected_cand["symbol"]
        log.info("[OPTS] Bounded selection: %s  %s  conf=%.2f  size_mod=%.1f",
                 sym, selected_cand.get("structure_type", "?"), _conf, _size_mod)

        if pf_allow_new_entries:
            # Mode gate
            if a2_mode is not None:
                try:
                    from divergence import is_action_allowed  # noqa: PLC0415
                    _a2_allowed, _a2_reason = is_action_allowed(a2_mode, "enter_long", sym)
                    if not _a2_allowed:
                        log.warning("[DIV] A2 BLOCKED %s — %s", sym, _a2_reason)
                        decision_record.execution_result = "no_trade"
                        decision_record.no_trade_reason = "execution_rejected"
                        return "no_trade"
                except Exception as _dge:
                    log.debug("[DIV] A2 mode gate failed (non-fatal): %s", _dge)

            strategy_enum = strategy_map.get(selected_cand.get("structure_type", ""))
            if strategy_enum is None:
                log.warning("[OPTS] Unknown structure_type=%s — holding",
                            selected_cand.get("structure_type"))
                decision_record.execution_result = "no_trade"
                decision_record.no_trade_reason = "execution_rejected"
                return "no_trade"

            proposal = next((c for c in candidates if c.symbol == sym), None)
            direction_val = proposal.direction if proposal else selected_cand.get("a1_direction", "bullish")
            iv_rank_val   = iv_summaries.get(sym, {}).get("iv_rank", 50.0) or 50.0
            max_loss_usd  = selected_cand.get("max_loss", equity * 0.03) * _size_mod

            try:
                _chain = options_data.fetch_options_chain(sym)
                structure, build_err = options_builder.build_structure(
                    symbol=sym,
                    strategy=strategy_enum,
                    direction=direction_val,
                    conviction=_conf,
                    iv_rank=iv_rank_val,
                    max_cost_usd=max_loss_usd,
                    chain=_chain,
                    equity=equity,
                    config={},
                )
            except Exception as _be:
                log.error("[OPTS] %s: chain/build failed: %s", sym, _be)
                decision_record.execution_result = "error"
                decision_record.no_trade_reason = "execution_error"
                return "error"

            if structure is None:
                log.warning("[OPTS] %s: build_structure rejected — %s", sym, build_err)
                decision_record.execution_result = "rejected"
                decision_record.no_trade_reason = "execution_rejected"
                return "rejected"

            options_state.save_structure(structure)
            _effective_obs = obs_mode or (not pf_allow_live_orders)
            if not pf_allow_live_orders:
                log.warning("[PREFLIGHT] shadow_only — suppressing live A2 submission for %s", sym)
            result = oe_opts.submit_options_order(structure, equity, _effective_obs)
            execution_results.append(result.to_dict())
            log.info("[OPTS] %s %s  status=%s%s",
                     sym, structure.strategy.value, result.status,
                     f"  structure_id={result.structure_id}" if result.structure_id else "")

            exec_status = result.status if result.status else "submitted"
            decision_record.execution_result = exec_status
            _log_attribution(decision_record, execution_results)
            return exec_status

        decision_record.execution_result = "no_trade"
        decision_record.no_trade_reason = "execution_rejected"
        return "no_trade"

    # ── Legacy free-form execution path ──────────────────────────────────────
    for action in debate_result.get("actions", []) if pf_allow_new_entries else []:
        if action.get("action") == "hold":
            log.info("[OPTS] HOLD %s — %s",
                     action.get("symbol", "?"), action.get("reason", ""))
            execution_results.append({
                "action": "hold",
                "symbol": action.get("symbol", ""),
                "status": "hold",
                "reason": action.get("reason", ""),
                "observation_mode": obs_mode,
            })
            continue

        sym = action.get("symbol", "")
        if not sym:
            continue

        proposal = next((c for c in candidates if c.symbol == sym), None)
        if proposal is None:
            log.warning("[OPTS] %s: no matching proposal found in candidates", sym)
            continue

        if a2_mode is not None:
            try:
                from divergence import is_action_allowed  # noqa: PLC0415
                _a2_allowed, _a2_reason = is_action_allowed(
                    a2_mode, "enter_long", action.get("symbol", "")
                )
                if not _a2_allowed:
                    log.warning("[DIV] A2 BLOCKED %s — %s",
                                action.get("symbol", ""), _a2_reason)
                    continue
            except Exception as _div_gate_exc:
                log.debug("[DIV] A2 mode gate failed (non-fatal): %s", _div_gate_exc)

        try:
            chain = options_data.fetch_options_chain(sym)
            structure, build_err = options_builder.build_structure(
                symbol=proposal.symbol,
                strategy=proposal.strategy,
                direction=proposal.direction,
                conviction=proposal.conviction,
                iv_rank=proposal.iv_rank,
                max_cost_usd=action.get("max_cost_usd", proposal.max_cost_usd),
                chain=chain,
                equity=equity,
                config={},
            )
        except Exception as exc:
            log.error("[OPTS] %s: chain/build failed: %s", sym, exc)
            execution_results.append({
                "action": "error", "symbol": sym,
                "status": "error", "reason": str(exc),
            })
            continue

        if structure is None:
            log.warning("[OPTS] %s: build_structure rejected — %s", sym, build_err)
            execution_results.append({
                "action": "rejected", "symbol": sym,
                "status": "rejected", "reason": build_err or "build_failed",
            })
            continue

        options_state.save_structure(structure)
        _effective_obs = obs_mode or (not pf_allow_live_orders)
        if not pf_allow_live_orders:
            log.warning("[PREFLIGHT] shadow_only — suppressing live A2 submission for %s", sym)
        result = oe_opts.submit_options_order(structure, equity, _effective_obs)
        execution_results.append(result.to_dict())
        log.info("[OPTS] %s %s  status=%s%s",
                 sym, structure.strategy.value, result.status,
                 f"  structure_id={result.structure_id}" if result.structure_id else "")

    if execution_results:
        any_submitted = any(
            r.get("status") in ("submitted", "observation") for r in execution_results
        )
        exec_status = "submitted" if any_submitted else "no_trade"
    else:
        exec_status = "no_trade"

    decision_record.execution_result = exec_status
    _log_attribution(decision_record, execution_results)
    return exec_status


def _log_attribution(decision_record, execution_results: list[dict]) -> None:
    """Log order_submitted attribution events. Non-fatal."""
    try:
        from attribution import log_attribution_event  # noqa: PLC0415
        _a2_tags = {"debate_layer": True, "risk_kernel": True, "sonnet_full": True}
        for _er in execution_results:
            if _er.get("status") in ("submitted", "observation") and _er.get("structure_id"):
                log_attribution_event(
                    event_type="order_submitted",
                    decision_id=decision_record.decision_id,
                    account="A2",
                    symbol=_er.get("underlying", ""),
                    module_tags=_a2_tags,
                    trigger_flags={},
                    structure_id=_er.get("structure_id"),
                )
    except Exception as _exc:
        log.debug("[OPTS] Attribution failed (non-fatal): %s", _exc)


# ── Close-check loop ──────────────────────────────────────────────────────────

def close_check_loop(alpaca_client) -> None:
    """
    Check all open structures for close or roll conditions.
    Non-fatal — logs errors but never raises.
    """
    import options_executor  # noqa: PLC0415
    import options_state  # noqa: PLC0415

    try:
        _strategy_cfg = _load_strategy_config()
        open_structs  = options_state.get_open_structures()
        if open_structs:
            for struct in open_structs:
                should_close, close_reason = options_executor.should_close_structure(
                    struct, current_prices={}, config=_strategy_cfg,
                    current_time=None,
                )
                if should_close:
                    # Check for roll opportunity before plain close
                    should_roll, roll_reason = options_executor.should_roll_structure(
                        struct, close_reason, _strategy_cfg
                    )
                    if should_roll:
                        log.info("[OPTS] Rolling %s (%s): %s",
                                 struct.underlying, struct.structure_id, roll_reason)
                        options_executor.execute_roll(
                            struct, alpaca_client, roll_reason, _strategy_cfg
                        )
                    else:
                        log.info("[OPTS] Closing %s (%s): %s",
                                 struct.underlying, struct.structure_id, close_reason)
                        options_executor.close_structure(
                            struct, alpaca_client, reason=close_reason, method="limit"
                        )
    except Exception as exc:
        log.warning("[OPTS] Close-check loop error: %s", exc)


def _load_strategy_config() -> dict:
    """Load strategy_config.json. Returns {} on failure — non-fatal."""
    try:
        path = Path(__file__).parent / "strategy_config.json"
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as _exc:
        log.debug("[OPTS] _load_strategy_config failed (non-fatal): %s", _exc)
        return {}


# ── Persistence ───────────────────────────────────────────────────────────────

def persist_decision_record(decision_record) -> None:
    """
    Save A2DecisionRecord to data/account2/decisions/a2_dec_YYYYMMDD_HHMMSS.json.
    Keeps last 500 decision files (deletes oldest when over limit).
    Called for every cycle — trade or no-trade.
    """
    try:
        _DECISIONS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(ET).strftime("%Y%m%d_%H%M%S")
        filename = f"a2_dec_{ts}.json"
        dest = _DECISIONS_DIR / filename

        # Ensure decision_id is present and in the correct a2_dec_YYYYMMDD_HHMMSS format.
        if not decision_record.decision_id or not decision_record.decision_id.startswith("a2_dec_"):
            decision_record.decision_id = f"a2_dec_{ts}"

        # Serialize — A2DecisionRecord contains A2CandidateSet which contains A2FeaturePack;
        # use a custom serializer to handle dataclasses and non-JSON-native types.
        from dataclasses import asdict as _asdict  # noqa: PLC0415
        try:
            record_dict = _asdict(decision_record)
        except Exception:
            # Fallback if asdict fails (e.g., nested non-dataclass objects).
            # ALL debate fields must be present here — omissions cause silent data loss.
            record_dict = {
                "decision_id":       decision_record.decision_id,
                "session_tier":      decision_record.session_tier,
                "debate_input":      decision_record.debate_input,
                "debate_output_raw": decision_record.debate_output_raw,
                "debate_parsed":     decision_record.debate_parsed,
                "selected_candidate": decision_record.selected_candidate,
                "execution_result":  decision_record.execution_result,
                "no_trade_reason":   decision_record.no_trade_reason,
                "elapsed_seconds":   decision_record.elapsed_seconds,
                "schema_version":    decision_record.schema_version,
                "code_version":      decision_record.code_version,
                "built_at":          decision_record.built_at,
            }

        dest.write_text(json.dumps(record_dict, default=str, indent=2))

        # Prune to last 500 decision files
        all_files = sorted(_DECISIONS_DIR.glob("a2_dec_*.json"))
        if len(all_files) > 500:
            for old_file in all_files[:-500]:
                try:
                    old_file.unlink()
                except Exception:
                    pass

    except Exception as exc:
        log.warning("[OPTS] persist_decision_record failed (non-fatal): %s", exc)


def save_legacy_decision(cycle_result: dict) -> None:
    """
    Append cycle decision to Account 2 decision log (decisions_account2.json).
    Kept for backward compatibility with existing log parsers.
    """
    try:
        cycle_result["timestamp"] = datetime.now(ET).isoformat()
        history: list = []
        if _DECISION_LOG.exists():
            try:
                history = json.loads(_DECISION_LOG.read_text())
                if not isinstance(history, list):
                    history = [history]
            except Exception:
                history = []

        history.append(cycle_result)
        # Keep last 500 decisions
        if len(history) > 500:
            history = history[-500:]
        _DECISION_LOG.write_text(json.dumps(history, indent=2))
    except Exception as exc:
        log.debug("[OPTS] Decision log write failed: %s", exc)
