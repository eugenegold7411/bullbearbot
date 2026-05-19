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
import os
from datetime import date, datetime
from pathlib import Path
from typing import (  # Any used in submit_selected_candidate; Optional for upgrade helpers
    Any,
    Optional,
)
from zoneinfo import ZoneInfo

from log_setup import get_logger

log = get_logger(__name__)

ET = ZoneInfo("America/New_York")

_A2_DIR        = Path(__file__).parent / "data" / "account2"
_DECISION_LOG  = _A2_DIR / "trade_memory" / "decisions_account2.json"
_DECISIONS_DIR = _A2_DIR / "decisions"
_A2_TRADES_LOG = _A2_DIR / "trades.jsonl"


def _write_a2_trade(record: dict) -> None:
    """Append a structured A2 trade record to data/account2/trades.jsonl. Never raises."""
    import fcntl as _fcntl  # noqa: PLC0415
    try:
        _A2_TRADES_LOG.parent.mkdir(parents=True, exist_ok=True)
        record.setdefault("written_at", datetime.now(ET).isoformat())
        record.setdefault("account", "a2")
        with _A2_TRADES_LOG.open("a", encoding="utf-8") as _fh:
            _fcntl.flock(_fh, _fcntl.LOCK_EX)
            try:
                _fh.write(json.dumps(record) + "\n")
            finally:
                _fcntl.flock(_fh, _fcntl.LOCK_UN)
    except Exception as _exc:
        log.warning("[OPTS] _write_a2_trade failed: %s", _exc)


def _validate_a2_trades_log() -> None:
    """Validate last line of A2 trades log; rename if corrupt."""
    try:
        if not _A2_TRADES_LOG.exists() or _A2_TRADES_LOG.stat().st_size == 0:
            return
        lines = _A2_TRADES_LOG.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            line = line.strip()
            if line:
                json.loads(line)
                return
    except (ValueError, json.JSONDecodeError):
        _ts = datetime.now(ET).strftime("%Y%m%d%H%M%S")
        _corrupt = _A2_TRADES_LOG.with_suffix(f".corrupt.{_ts}")
        try:
            _A2_TRADES_LOG.rename(_corrupt)
            log.warning("[OPTS] A2 trades log corrupt — renamed to %s", _corrupt.name)
        except Exception as _re:
            log.warning("[OPTS] could not rename corrupt A2 trades log: %s", _re)
    except Exception as _exc:
        log.debug("[OPTS] _validate_a2_trades_log non-fatal: %s", _exc)


_validate_a2_trades_log()

from bot_options_stage2_structures import _STRATEGY_FROM_STRUCTURE

_STRATEGY_ALIASES: dict[str, str] = {
    # debate output name  : OptionStrategy enum value
    "long_call":           "single_call",
    "long_put":            "single_put",
    "debit_call_spread":   "call_debit_spread",
    "debit_put_spread":    "put_debit_spread",
    "credit_call_spread":  "call_credit_spread",
    "credit_put_spread":   "put_credit_spread",
}

log.debug("[EXEC] Strategy aliases loaded: %s", list(_STRATEGY_ALIASES.keys()))


def _normalize_strategy_name(name: str) -> str:
    """Resolve debate strategy names to canonical OptionStrategy enum values."""
    return _STRATEGY_ALIASES.get(name, name)


def _get_strategy_map() -> dict:
    from schemas import OptionStrategy as _OS  # noqa: PLC0415
    _base = dict(_STRATEGY_FROM_STRUCTURE)
    for _enum in _OS:
        _base.setdefault(_enum.value, _enum)
    return _base


# ── Debate snapshot builder ───────────────────────────────────────────────────

def _build_debate_snapshot(debate_result: dict, decision_id: str) -> dict:
    """Build the debate dict persisted to OptionsStructure.debate."""
    return {
        "confidence":                debate_result.get("confidence"),
        "key_risks":                 debate_result.get("key_risks", []),
        "reasons":                   debate_result.get("reasons", ""),
        "reject":                    debate_result.get("reject", False),
        "selected_candidate_id":     debate_result.get("selected_candidate_id"),
        "recommended_size_modifier": debate_result.get("recommended_size_modifier", 1.0),
        "ran_at":                    datetime.now(ET).isoformat(),
        "decision_id":               decision_id,
    }


# ── Profit-close cooldown guard (Fix C) ───────────────────────────────────────

_PROFIT_CLOSE_REASONS = frozenset({
    "profit_target_pct_hit",
    "target_profit_hit",
    "profit_lock_retrace",
    "profit_target_50_near_expiry",
    "iv_expansion_take_profit",
})

_LOSS_CLOSE_REASONS = frozenset({
    "stop_loss_hit",
    "max_loss_exit",
    "time_stop",
    "friday_eod_forced_close",
    "theta_burn_underwater",
    "loss_cut_near_expiry",
    "time_stop_loss_near_expiry",
    "conviction_degradation",
    "manual_emergency_close",
    "position_not_in_alpaca",
})


def _is_recent_profit_close(symbol: str, cooldown_minutes: float) -> bool:
    """Returns True if a profit-exit close on `symbol` occurred within cooldown_minutes."""
    try:
        import options_state  # noqa: PLC0415
        now = datetime.now(ET)
        for s in options_state.load_structures():
            if s.underlying != symbol:
                continue
            lc = s.lifecycle.value if hasattr(s.lifecycle, "value") else str(s.lifecycle)
            if lc != "closed":
                continue
            if s.close_reason_code not in _PROFIT_CLOSE_REASONS:
                continue
            if not s.closed_at:
                continue
            try:
                closed_dt = datetime.fromisoformat(s.closed_at)
                if closed_dt.tzinfo is None:
                    closed_dt = closed_dt.replace(tzinfo=ET)
                elapsed = (now - closed_dt).total_seconds() / 60
                if elapsed < cooldown_minutes:
                    log.info(
                        "[OPTS] %s: recent profit close %.0fm ago (reason=%s cooldown=%.0fm)",
                        symbol, elapsed, s.close_reason_code, cooldown_minutes,
                    )
                    return True
            except (ValueError, TypeError):
                continue
    except Exception as _exc:
        log.debug("[OPTS] profit_cooldown check failed (non-fatal): %s", _exc)
    return False


def _is_recent_loss_close(
    symbol: str, cooldown_minutes: float, strategy_type: str | None = None
) -> bool:
    """Returns True if a loss-exit close on `symbol` (same strategy_type when given)
    occurred within cooldown_minutes. Different strategy = separate cooldown bucket."""
    try:
        import options_state  # noqa: PLC0415
        now = datetime.now(ET)
        for s in options_state.load_structures():
            if s.underlying != symbol:
                continue
            lc = s.lifecycle.value if hasattr(s.lifecycle, "value") else str(s.lifecycle)
            if lc != "closed":
                continue
            if s.close_reason_code not in _LOSS_CLOSE_REASONS:
                continue
            if strategy_type is not None:
                s_strat = s.strategy.value if hasattr(s.strategy, "value") else str(s.strategy)
                if s_strat != strategy_type:
                    continue
            if not s.closed_at:
                continue
            try:
                closed_dt = datetime.fromisoformat(s.closed_at)
                if closed_dt.tzinfo is None:
                    closed_dt = closed_dt.replace(tzinfo=ET)
                elapsed = (now - closed_dt).total_seconds() / 60
                if elapsed < cooldown_minutes:
                    log.info(
                        "[OPTS] %s/%s: recent loss close %.0fm ago (reason=%s cooldown=%.0fm)",
                        symbol, strategy_type or "any", elapsed,
                        s.close_reason_code, cooldown_minutes,
                    )
                    return True
            except (ValueError, TypeError):
                continue
    except Exception as _exc:
        log.debug("[OPTS] loss_cooldown check failed (non-fatal): %s", _exc)
    return False


# ── Duplicate-submission guard (T2-1) ─────────────────────────────────────────

def _is_duplicate_submission(symbol: str, legs: list) -> bool:
    """
    Returns True if a structure for the same underlying with any matching leg
    OCC symbol already exists in submitted, partially_filled, fully_filled, or
    closing state. Prevents duplicate submissions and re-entry while a close is
    pending Alpaca confirmation.
    """
    try:
        import options_state  # noqa: PLC0415
        _ACTIVE = {"submitted", "partially_filled", "fully_filled", "closing"}
        new_occs = {
            leg.occ_symbol
            for leg in legs
            if getattr(leg, "occ_symbol", None)
        }
        if not new_occs:
            return False
        for s in options_state.load_structures():
            if s.underlying != symbol:
                continue
            if (s.lifecycle.value if hasattr(s.lifecycle, "value") else str(s.lifecycle)) not in _ACTIVE:
                continue
            existing_occs = {
                leg.occ_symbol
                for leg in s.legs
                if getattr(leg, "occ_symbol", None)
            }
            if new_occs & existing_occs:
                log.warning(
                    "[OPTS] DUPLICATE_SUBMIT blocked: %s already has active structure "
                    "with overlapping OCC symbols %s (structure_id=%s lifecycle=%s)",
                    symbol, new_occs & existing_occs, s.structure_id,
                    s.lifecycle.value if hasattr(s.lifecycle, "value") else s.lifecycle,
                )
                return True
    except Exception as _exc:
        log.debug("[OPTS] Duplicate check failed (non-fatal): %s", _exc)
    return False


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


# ── Execution guards ──────────────────────────────────────────────────────────

def _validate_execution_matches_debate(
    selected_candidate: dict,
    structure_to_execute,
    strategy_map: dict,
) -> bool:
    """
    Confirm the structure being executed matches what the debate selected.
    Returns False and fires CRITICAL log + WhatsApp if strategies differ.
    Normalizes debate strategy names to canonical enum values before comparing,
    so "debit_call_spread" and "call_debit_spread" are treated as equivalent.
    """
    raw_type   = selected_candidate.get("structure_type", "")
    normalized = _normalize_strategy_name(raw_type)
    expected_enum = strategy_map.get(normalized)
    if expected_enum is None:
        log.warning(
            "[EXEC] _validate: unknown strategy type '%s' (normalized='%s') "
            "— skipping mismatch check",
            raw_type, normalized,
        )
        return True
    if structure_to_execute.strategy != expected_enum:
        _debate_strat = raw_type or "unknown"
        _exec_strat = (
            structure_to_execute.strategy.value
            if hasattr(structure_to_execute.strategy, "value")
            else str(structure_to_execute.strategy)
        )
        log.critical(
            "[EXEC] STRATEGY MISMATCH — debate selected %s but execution is %s. "
            "BLOCKING order submission.",
            _debate_strat, _exec_strat,
        )
        try:
            from notifications import send_whatsapp_direct  # noqa: PLC0415
            send_whatsapp_direct(
                f"[A2 BLOCKED] Strategy mismatch: debate approved "
                f"{_debate_strat} but {_exec_strat} attempted. "
                f"Trade blocked."
            )
        except Exception as _wa_exc:
            log.debug("[EXEC] WhatsApp alert failed: %s", _wa_exc)
        return False
    return True


def _audit_open_structures_for_mismatch() -> list[dict]:
    """
    FIX-D: Check all active structures for execution_fallback or strategy mismatch.
    For pre-fix structures, cross-references decision files via debate.decision_id.
    Returns a list of mismatch records and logs each one.
    """
    try:
        import options_state  # noqa: PLC0415
        _sm = _get_strategy_map()
        mismatches: list[dict] = []
        _ACTIVE = {"submitted", "partially_filled", "fully_filled"}

        for s in options_state.load_structures():
            lc = s.lifecycle.value if hasattr(s.lifecycle, "value") else str(s.lifecycle)
            if lc not in _ACTIVE:
                continue
            actual = s.strategy.value if hasattr(s.strategy, "value") else str(s.strategy)
            intended = getattr(s, "intended_structure", None)

            if intended is not None:
                # Normalize: intended is structure_type ("long_call"), actual is enum value
                # ("single_call"). Use strategy_map to compare apples to apples.
                intended_enum = _sm.get(intended)
                intended_val = intended_enum.value if intended_enum else intended
                if intended_val != actual:
                    rec: dict = {
                        "structure_id":       s.structure_id,
                        "symbol":             s.underlying,
                        "intended_structure": intended,
                        "actual_strategy":    actual,
                        "lifecycle":          lc,
                        "risk_authorized":    getattr(s, "risk_authorized", None),
                        "risk_executed":      getattr(s, "risk_executed", None),
                    }
                    mismatches.append(rec)
                    log.warning(
                        "[EXEC] FIX-D MISMATCH: %s (%s) intended=%s actual=%s",
                        s.underlying, s.structure_id[:8], intended, actual,
                    )
                continue

            # Pre-fix structures: cross-reference via decision file
            debate = getattr(s, "debate", {}) or {}
            decision_id = debate.get("decision_id")
            if not decision_id:
                continue
            try:
                dec_path = _DECISIONS_DIR / f"{decision_id}.json"
                if not dec_path.exists():
                    continue
                dec = json.loads(dec_path.read_text(encoding="utf-8"))
                sc = dec.get("selected_candidate") or {}
                intended_type = sc.get("structure_type", "")
                if not intended_type:
                    continue
                intended_enum = _sm.get(intended_type)
                if intended_enum and intended_enum.value != actual:
                    rec = {
                        "structure_id":       s.structure_id,
                        "symbol":             s.underlying,
                        "intended_structure": intended_type,
                        "actual_strategy":    actual,
                        "lifecycle":          lc,
                        "source":             "decision_file",
                    }
                    mismatches.append(rec)
                    log.warning(
                        "[EXEC] FIX-D MISMATCH (via decision file): %s (%s) "
                        "intended=%s actual=%s",
                        s.underlying, s.structure_id[:8], intended_type, actual,
                    )
            except Exception as _de:
                log.debug("[EXEC] FIX-D decision check failed for %s: %s", s.structure_id, _de)

        if not mismatches:
            log.info("[EXEC] FIX-D audit complete — no mismatches in active structures")
        else:
            log.critical("[EXEC] FIX-D audit found %d mismatch(es): %s",
                         len(mismatches), [m["symbol"] for m in mismatches])
        return mismatches
    except Exception as _exc:
        log.warning("[EXEC] _audit_open_structures_for_mismatch failed: %s", _exc)
        return []


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

    # Load confidence floor from Alpaca base URL (paper vs live account)
    _cfg = _load_strategy_config()
    _a2_cfg = _cfg.get("account2", {})
    _base_url = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    _is_paper = "paper-api.alpaca.markets" in _base_url.lower()
    _conf_floor = float(_a2_cfg.get(
        "paper_confidence_floor" if _is_paper else "live_confidence_floor",
        0.75 if _is_paper else 0.85,
    ))

    # ── A2-3b bounded execution path ─────────────────────────────────────────
    if candidate_structures and "selected_candidate_id" in debate_result:
        _reject   = debate_result.get("reject", True)
        _sel_id   = debate_result.get("selected_candidate_id")
        _conf     = float(debate_result.get("confidence", 0.0))
        _size_mod = float(debate_result.get("recommended_size_modifier", 1.0))

        # Hard rule: high-conviction trades get 1.5x sizing regardless of Claude's modifier.
        # Confidence ≥ 0.85 is the threshold where edge is considered well-established.
        if _conf >= 0.85 and _size_mod < 1.5:
            log.info("[OPTS] High-conviction override: conf=%.2f → size_mod 1.0→1.5", _conf)
            _size_mod = 1.5

        if _reject or not _sel_id:
            log.info("[OPTS] Bounded debate: reject — %s",
                     debate_result.get("reasons", "")[:100])
            decision_record.execution_result = "no_trade"
            decision_record.no_trade_reason = "debate_rejected_all"
            return "no_trade"

        if _conf < _conf_floor:
            log.info("[OPTS] Bounded debate: confidence=%.2f < %.2f — holding", _conf, _conf_floor)
            decision_record.execution_result = "no_trade"
            decision_record.no_trade_reason = "debate_low_confidence"
            return "no_trade"

        # Debate winner first; remaining candidates in original order as fallback.
        _winner = next(
            (c for c in candidate_structures if c.get("candidate_id") == _sel_id), None
        )
        if _winner is None:
            log.warning("[OPTS] Bounded debate selected_candidate_id=%s not found — holding", _sel_id)
            decision_record.execution_result = "no_trade"
            decision_record.no_trade_reason = "debate_rejected_all"
            return "no_trade"

        _winner_type = _winner.get("structure_type", "unknown")

        _fallback_pool = [_winner] + [
            c for c in candidate_structures if c.get("candidate_id") != _sel_id
        ]
        _MAX_BUILD_ATTEMPTS = 3

        if pf_allow_new_entries:
            for _attempt, _cand in enumerate(_fallback_pool[:_MAX_BUILD_ATTEMPTS], 1):
                sym = _cand["symbol"]
                _cand_type = _cand.get("structure_type", "unknown")
                if _attempt == 1:
                    log.info("[OPTS] Bounded selection: %s  %s  conf=%.2f  size_mod=%.1f",
                             sym, _cand_type, _conf, _size_mod)
                else:
                    log.info("[OPTS] Build fallback attempt %d/%d: %s  %s",
                             _attempt, _MAX_BUILD_ATTEMPTS, sym, _cand_type)
                    # FIX-A: abort if fallback strategy differs from debate winner
                    if _normalize_strategy_name(_cand_type) != _normalize_strategy_name(_winner_type):
                        _risk_auth = _winner.get("max_loss", 0.0)
                        _risk_fall = _cand.get("max_loss", 0.0)
                        log.critical(
                            "[EXEC] %s: spread short leg failed — ABORTING trade. "
                            "Debate approved %s only, %s was explicitly rejected. "
                            "original=%s attempted=%s risk_delta=$%.0f",
                            sym, _winner_type, _cand_type,
                            _winner_type, _cand_type, _risk_fall - _risk_auth,
                        )
                        try:
                            from notifications import (
                                send_whatsapp_direct,  # noqa: PLC0415
                            )
                            send_whatsapp_direct(
                                f"[A2 TRADE ABORTED] {sym}: spread leg failed, "
                                f"no fallback executed. Manual review needed."
                            )
                        except Exception as _wa_exc:
                            log.debug("[EXEC] WhatsApp alert failed: %s", _wa_exc)
                        decision_record.execution_result = "rejected"
                        decision_record.no_trade_reason = "spread_leg_failed_no_fallback"
                        return "rejected"

                # Mode gate — hard stop (not candidate-specific)
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

                # Loss-close cooldown — block re-entry after a loss exit (same strategy only)
                _loss_cooldown = float(_a2_cfg.get("loss_close_cooldown_minutes", 0))
                if _loss_cooldown > 0 and _is_recent_loss_close(sym, _loss_cooldown, _cand_type):
                    log.warning(
                        "[OPTS] %s/%s: skipping — within %.0fm loss-close cooldown",
                        sym, _cand_type, _loss_cooldown,
                    )
                    continue

                strategy_enum = strategy_map.get(_cand.get("structure_type", ""))
                if strategy_enum is None:
                    log.warning("[OPTS] Unknown structure_type=%s — skipping %s",
                                _cand.get("structure_type"), sym)
                    continue

                proposal = next((c for c in candidates if c.symbol == sym), None)
                direction_val = proposal.direction if proposal else _cand.get("a1_direction", "bullish")
                if proposal is not None:
                    iv_rank_val = proposal.iv_rank
                else:
                    _iv = iv_summaries.get(sym, {}).get("iv_rank")
                    if _iv is None:
                        log.warning("[OPTS] %s: iv_rank absent — skipping", sym)
                        continue
                    iv_rank_val = float(_iv)
                max_loss_usd = _cand.get("max_loss", equity * 0.03) * _size_mod

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
                        config=_a2_cfg,
                    )
                except Exception as _be:
                    log.error("[OPTS] %s: chain/build failed: %s", sym, _be)
                    continue

                if structure is None:
                    log.warning("[OPTS] %s: build_structure rejected (attempt %d/%d) — %s",
                                sym, _attempt, _MAX_BUILD_ATTEMPTS, build_err)
                    continue

                # FIX-C: validate structure matches what the debate actually selected
                if not _validate_execution_matches_debate(_winner, structure, strategy_map):
                    decision_record.execution_result = "rejected"
                    decision_record.no_trade_reason = "spread_leg_failed_no_fallback"
                    return "rejected"

                # Per-symbol submission lock — block duplicates (T2-1)
                if _is_duplicate_submission(sym, structure.legs):
                    decision_record.execution_result = "no_trade"
                    decision_record.no_trade_reason = "duplicate_submission_blocked"
                    return "no_trade"

                # FIX-B: stamp execution audit trail fields
                _risk_auth = _winner.get("max_loss", 0.0) * _size_mod
                _risk_exec = _cand.get("max_loss", 0.0) * _size_mod
                structure.intended_structure = _winner_type
                structure.execution_fallback = (_attempt > 1)
                structure.fallback_reason = (
                    f"winner_build_failed_attempt_{_attempt - 1}" if _attempt > 1 else None
                )
                structure.risk_authorized = _risk_auth
                structure.risk_executed = _risk_exec

                # FIX-B: block if fallback somehow executes with higher risk
                if structure.execution_fallback and _risk_exec > _risk_auth:
                    log.critical(
                        "[EXEC] %s: execution_fallback=True and risk_executed=$%.0f "
                        "> risk_authorized=$%.0f — BLOCKING order submission.",
                        sym, _risk_exec, _risk_auth,
                    )
                    try:
                        from notifications import send_whatsapp_direct  # noqa: PLC0415
                        send_whatsapp_direct(
                            f"[A2 CRITICAL] {sym}: fallback risk ${_risk_exec:.0f} "
                            f"exceeds authorized ${_risk_auth:.0f}. Trade blocked."
                        )
                    except Exception as _wa_exc:
                        log.debug("[EXEC] WhatsApp alert failed: %s", _wa_exc)
                    decision_record.execution_result = "rejected"
                    decision_record.no_trade_reason = "spread_leg_failed_no_fallback"
                    return "rejected"

                structure.debate = _build_debate_snapshot(
                    debate_result if _attempt == 1 else {}, decision_record.decision_id
                )
                structure.delta = _cand.get("delta")
                structure.theta = _cand.get("theta")
                structure.vega  = _cand.get("vega")

                # Fix A: theta entry filter (belt-and-suspenders over V3 stage-2 veto)
                if _a2_cfg.get("theta_entry_filter_enabled", False):
                    _theta_val = structure.theta
                    _debit_val = _cand.get("debit") or structure.net_debit_per_contract()
                    _max_theta = float(_a2_cfg.get("max_theta_decay_pct", 0.05))
                    if (_theta_val is not None and _debit_val and _debit_val > 0
                            and abs(_theta_val) / _debit_val > _max_theta):
                        log.warning(
                            "[OPTS] %s: theta entry filter blocked — rate=%.3f > %.3f",
                            sym, abs(_theta_val) / _debit_val, _max_theta,
                        )
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

                exec_status = result.status if result.status else "submitted"
                decision_record.execution_result = exec_status
                _log_attribution(decision_record, execution_results)
                return exec_status

            log.warning("[OPTS] All %d build attempts exhausted — holding", _MAX_BUILD_ATTEMPTS)
            decision_record.execution_result = "rejected"
            decision_record.no_trade_reason = "execution_rejected"
            return "rejected"

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
                config=_a2_cfg,
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

        structure.debate = _build_debate_snapshot(
            decision_record.debate_parsed or {}, decision_record.decision_id
        )
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


# ── Fill-price ingestion ──────────────────────────────────────────────────────

def _update_fill_prices(structures: list, trading_client) -> bool:
    """
    For structures with legs that have an order_id but null filled_price,
    fetch the fill data from Alpaca and update in place. Saves each updated
    structure atomically via options_state. Returns True if any updates were made.

    Targets SUBMITTED, PARTIALLY_FILLED, and FULLY_FILLED lifecycles —
    close_structure() gates on filled_price so populating this field
    enables proper cost-basis tracking and P&L computation.
    """
    import options_state  # noqa: PLC0415

    _ELIGIBLE = {"submitted", "partially_filled", "fully_filled"}
    updated_any = False
    for s in structures:
        lc = s.lifecycle.value if hasattr(s.lifecycle, "value") else str(s.lifecycle)
        if lc not in _ELIGIBLE:
            continue
        structure_updated = False
        for leg in s.legs:
            if leg.order_id and leg.filled_price is None:
                try:
                    order = trading_client.get_order_by_id(leg.order_id)
                    # For mleg orders, order.filled_avg_price is the NET spread
                    # price, not an individual leg price. Match this leg's OCC
                    # symbol against order.legs[] to get the per-leg fill instead.
                    order_legs = getattr(order, "legs", None) or []
                    matched = next(
                        (ol for ol in order_legs
                         if str(getattr(ol, "symbol", "")) == leg.occ_symbol),
                        None,
                    )
                    if matched is not None:
                        fap  = getattr(matched, "filled_avg_price", None) \
                               or getattr(order, "filled_avg_price", None)
                        fqty = getattr(matched, "filled_qty", None) \
                               or getattr(order, "filled_qty", None)
                    else:
                        # Single-leg order — parent filled_avg_price is correct.
                        fap  = getattr(order, "filled_avg_price", None)
                        fqty = getattr(order, "filled_qty", None)
                    if fap is not None:
                        leg.filled_price = float(fap)
                        if fqty is not None:
                            leg.filled_qty = float(fqty)
                        structure_updated = True
                        log.info(
                            "[FILL] %s leg %s: filled_price=%.4f filled_qty=%s",
                            s.underlying, leg.order_id, leg.filled_price, leg.filled_qty,
                        )
                except Exception as _exc:
                    log.debug("[FILL] fetch failed for order_id=%s: %s", leg.order_id, _exc)
        if structure_updated:
            # Backfill debit_paid once all leg fills are available.
            if s.debit_paid is None:
                _dp = s.net_debit_per_contract()
                if _dp is not None:
                    s.debit_paid = _dp
                    log.info(
                        "[FILL] %s: debit_paid backfilled=%.4f from leg fills",
                        s.underlying, _dp,
                    )
            try:
                options_state.save_structure(s)
                updated_any = True
            except Exception as _se:
                log.debug("[FILL] save_structure failed for %s: %s", s.structure_id, _se)
            # Fill quality check: if net debit fill > pre-entry mid × 1.15, fire
            # a CRITICAL alert. Catches debit_fill_aggression overshoot at entry.
            # TODO(DASHBOARD) surface fill quality violations in safety panel
            try:
                _mids = {lg.occ_symbol: lg.mid for lg in s.legs if lg.mid is not None}
                _net_mid = sum(
                    (1.0 if lg.side == "buy" else -1.0) * _mids[lg.occ_symbol]
                    for lg in s.legs if lg.occ_symbol in _mids
                )
                _net_fill = sum(
                    (1.0 if lg.side == "buy" else -1.0) * (lg.filled_price or 0.0)
                    for lg in s.legs
                )
                if _net_mid > 0 and _net_fill > _net_mid * 1.15:
                    _overpay_pct = (_net_fill / _net_mid - 1) * 100
                    log.critical(
                        "[FILL_QUALITY] %s %s: net fill $%.2f vs mid $%.2f "
                        "(+%.0f%% above mid) — check debit_fill_aggression",
                        s.underlying, getattr(s.strategy, "value", s.strategy),
                        _net_fill, _net_mid, _overpay_pct,
                    )
                    try:
                        from notifications import send_whatsapp_direct  # noqa: PLC0415
                        send_whatsapp_direct(
                            f"FILL QUALITY ALERT: {s.underlying} debit "
                            f"${_net_fill:.2f} vs mid ${_net_mid:.2f} "
                            f"(+{_overpay_pct:.0f}% above mid)"
                        )
                    except Exception:
                        pass
            except Exception:
                pass
    return updated_any


# ── Submitted-order lifecycle sync ───────────────────────────────────────────

def _sync_submitted_lifecycles(structures: list, trading_client) -> None:
    """
    For every SUBMITTED structure, query Alpaca for its order status and
    transition lifecycle accordingly. Non-fatal. Saves each mutated structure.

    Transitions:
      cancelled / expired / done_for_day / stopped / suspended → CANCELLED
      filled                                                    → FULLY_FILLED
      partially_filled                                          → PARTIALLY_FILLED
      new / accepted / pending_new / held / (still open)       → no change
    """
    import options_state  # noqa: PLC0415
    from schemas import StructureLifecycle  # noqa: PLC0415

    _CANCEL_STATUSES  = {"cancelled", "expired", "done_for_day", "stopped", "suspended"}
    _FILL_STATUSES    = {"filled"}
    _PARTIAL_STATUSES = {"partially_filled"}

    for s in structures:
        lc = s.lifecycle.value if hasattr(s.lifecycle, "value") else str(s.lifecycle)
        if lc != "submitted":
            continue
        if not s.order_ids:
            continue
        try:
            order = trading_client.get_order_by_id(s.order_ids[0])
            raw_status = str(order.status).lower()
            # Normalise "orderstatus.cancelled" → "cancelled"
            status = raw_status.split(".")[-1]

            if status in _CANCEL_STATUSES:
                s.add_audit(
                    f"order {s.order_ids[0]} status={status} — lifecycle → cancelled"
                )
                s.lifecycle = StructureLifecycle.CANCELLED
                log.info(
                    "[FILL] %s (%s): order %s → %s, lifecycle=cancelled",
                    s.underlying, s.structure_id, s.order_ids[0], status,
                )
            elif status in _FILL_STATUSES:
                s.add_audit(
                    f"order {s.order_ids[0]} filled — lifecycle → fully_filled"
                )
                s.lifecycle = StructureLifecycle.FULLY_FILLED
                log.info(
                    "[FILL] %s (%s): order %s filled, lifecycle=fully_filled",
                    s.underlying, s.structure_id, s.order_ids[0],
                )
                _write_a2_trade({
                    "event_type":   "entry_filled",
                    "structure_id": s.structure_id,
                    "symbol":       s.underlying,
                    "strategy":     str(getattr(s.strategy, "value", s.strategy)),
                    "order_id":     s.order_ids[0] if s.order_ids else "",
                    "legs":         [
                        {"occ_symbol": leg.occ_symbol, "side": leg.side,
                         "filled_price": leg.filled_price}
                        for leg in getattr(s, "legs", [])
                    ],
                    "contracts":    getattr(s, "contracts", None),
                    "expiration":   getattr(s, "expiration", None),
                })
            elif status in _PARTIAL_STATUSES:
                s.add_audit(
                    f"order {s.order_ids[0]} partially_filled — lifecycle → partially_filled"
                )
                s.lifecycle = StructureLifecycle.PARTIALLY_FILLED
                log.info(
                    "[FILL] %s (%s): order %s partially_filled, lifecycle=partially_filled",
                    s.underlying, s.structure_id, s.order_ids[0],
                )
            else:
                continue  # still open — no transition needed

            try:
                options_state.save_structure(s)
            except Exception as _se:
                log.debug("[FILL] save_structure failed for %s: %s", s.structure_id, _se)

        except Exception as exc:
            log.debug("[FILL] _sync_submitted_lifecycles %s: %s", s.structure_id, exc)


def _sync_closing_lifecycles(structures: list, trading_client) -> None:
    """
    For every CLOSING structure, query Alpaca for its close order status and
    promote or revert the lifecycle accordingly. Non-fatal. Saves each mutated
    structure.

    Transitions:
      close order filled                                   → CLOSED
      close order cancelled / expired / rejected           → FULLY_FILLED
        (close failed — position still live, retry on next cycle)
      close order still pending (new/accepted/pending_new) → no change (stays CLOSING)

    This runs at the TOP of close_check_loop(), before orphan detection in
    preflight, so CLOSING structures keep their OCC symbols in _tracked_occs
    for the full cycle in which the close order is pending.
    """
    import options_state  # noqa: PLC0415
    from schemas import StructureLifecycle  # noqa: PLC0415

    _FILL_STATUSES   = {"filled"}
    _REVERT_STATUSES = {"cancelled", "expired", "done_for_day", "stopped", "suspended", "rejected"}

    for s in structures:
        lc = s.lifecycle.value if hasattr(s.lifecycle, "value") else str(s.lifecycle)
        if lc != "closing":
            continue
        if not s.close_order_ids:
            continue
        close_order_id = s.close_order_ids[-1]
        try:
            order = trading_client.get_order_by_id(close_order_id)
            raw_status = str(order.status).lower()
            status = raw_status.split(".")[-1]

            if status in _FILL_STATUSES:
                s.add_audit(
                    f"close order {close_order_id} filled — lifecycle → closed"
                )
                s.lifecycle = StructureLifecycle.CLOSED
                log.info(
                    "[CLOSE_SYNC] %s (%s): close order %s filled → closed",
                    s.underlying, s.structure_id, close_order_id,
                )
                _write_a2_trade({
                    "event_type":    "close_submitted",
                    "structure_id":  s.structure_id,
                    "symbol":        s.underlying,
                    "strategy":      str(getattr(s.strategy, "value", s.strategy)),
                    "close_reason":  s.close_reason_code,
                    "lifecycle":     "closed",
                    "pnl_unrealized": s.realized_pnl,
                    "closed_at":     s.closed_at,
                    "account":       "a2",
                })
            elif status in _REVERT_STATUSES:
                s.add_audit(
                    f"close order {close_order_id} status={status} — reverting to fully_filled"
                )
                s.lifecycle = StructureLifecycle.FULLY_FILLED
                s.close_attempt_count = getattr(s, "close_attempt_count", 0) + 1
                log.warning(
                    "[CLOSE_SYNC] %s (%s): close order %s → %s — reverting to fully_filled"
                    " (attempt %d)",
                    s.underlying, s.structure_id, close_order_id, status,
                    s.close_attempt_count,
                )
            else:
                log.debug(
                    "[CLOSE_SYNC] %s (%s): close order %s status=%s — still pending",
                    s.underlying, s.structure_id, close_order_id, status,
                )
                continue  # still pending — leave as CLOSING

            try:
                options_state.save_structure(s)
            except Exception as _se:
                log.debug("[CLOSE_SYNC] save_structure failed for %s: %s", s.structure_id, _se)

        except Exception as exc:
            log.debug("[CLOSE_SYNC] _sync_closing_lifecycles %s: %s", s.structure_id, exc)


# ── Close-check loop ──────────────────────────────────────────────────────────

def _fetch_close_check_prices(open_structs: list) -> dict:
    """
    Build {occ_symbol: mid_price} for all legs across open structures.

    Fetches once per unique underlying using fetch_options_chain().
    Only active during market hours (9:30 AM – 4:00 PM ET, Mon–Fri).
    Returns {} outside market hours — options chains are unavailable then.
    Non-fatal per underlying: if fetch fails, that symbol contributes no prices
    (existing DTE and time-stop rules in should_close_structure still fire).
    """
    now_et = datetime.now(ET)
    if now_et.weekday() >= 5:
        return {}
    market_open  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
    if not (market_open <= now_et < market_close):
        return {}

    import options_data  # noqa: PLC0415

    prices: dict[str, float] = {}
    fetched: set[str] = set()

    # Group by underlying so each chain is fetched at most once.
    by_underlying: dict[str, list] = {}
    for struct in open_structs:
        by_underlying.setdefault(struct.underlying, []).append(struct)

    for underlying, structs in by_underlying.items():
        try:
            chain = options_data.fetch_options_chain(underlying)
            expirations = chain.get("expirations", {})
            for struct in structs:
                for leg in struct.legs:
                    if leg.occ_symbol in prices:
                        continue
                    option_key = "calls" if leg.option_type == "call" else "puts"
                    contracts = expirations.get(leg.expiration, {}).get(option_key, [])
                    for contract in contracts:
                        if abs(float(contract.get("strike", 0)) - leg.strike) < 0.01:
                            bid = float(contract.get("bid") or 0)
                            ask = float(contract.get("ask") or 0)
                            if bid > 0 and ask > 0:
                                prices[leg.occ_symbol] = round((bid + ask) / 2, 4)
                            break
            fetched.add(underlying)
        except Exception as exc:
            log.debug("[CLOSE_CHECK] price fetch for %s failed (non-fatal): %s",
                      underlying, exc)

    log.info("[CLOSE_CHECK] fetched %d option prices for %d underlyings",
             len(prices), len(fetched))
    return prices


def _compute_pnl_unrealized(struct, current_prices: dict) -> float | None:
    """
    Compute unrealized PnL for a structure from current mid prices vs fill prices.
    Returns None if any leg is missing fill or current price data.
    """
    try:
        total = 0.0
        for leg in struct.legs:
            entry = leg.filled_price
            current = current_prices.get(leg.occ_symbol)
            if entry is None or current is None:
                return None
            # Buy legs: profit when price rises; sell legs: profit when price falls.
            sign = 1.0 if leg.side == "buy" else -1.0
            total += sign * (current - entry) * leg.qty * struct.contracts * 100
        return round(total, 2)
    except Exception:
        return None


# ── Upgrade helpers: migrated to options_position_manager.py ─────────────────
# Stage 4 retains the public names as thin shims for backward compatibility.
# The real implementations (with identical logic and side effects) live in
# options_position_manager. A future session removes these shims.

def _compute_dte(structure) -> Optional[int]:
    """Shim — see options_position_manager._compute_dte."""
    from options_position_manager import _compute_dte as _impl  # noqa: PLC0415
    return _impl(structure)


def _load_latest_debate_selected_candidate() -> Optional[dict]:
    """Shim — see options_position_manager._load_latest_debate_selected_candidate."""
    from options_position_manager import (  # noqa: PLC0415
        _load_latest_debate_selected_candidate as _impl,
    )
    return _impl()


def _build_upgrade_short_leg(structure, is_call: bool) -> Optional[str]:
    """Shim — see options_position_manager._build_upgrade_short_leg."""
    from options_position_manager import (
        _build_upgrade_short_leg as _impl,  # noqa: PLC0415
    )
    return _impl(structure, is_call)


def _evaluate_structure_upgrade(
    structure,
    debate_result: Optional[dict],
    config: dict,
) -> Optional[dict]:
    """
    Shim — delegates to options_position_manager._check_upgrade.

    Same signature, same return shape, same side effect (stamps
    structure.last_upgrade_attempted when conditions 1-5 pass).
    """
    from options_position_manager import _check_upgrade as _impl  # noqa: PLC0415
    return _impl(structure, debate_result, config)


def close_check_loop(alpaca_client) -> None:
    """
    Check all open structures for close or roll conditions.
    Non-fatal — logs errors but never raises.
    """
    import options_executor  # noqa: PLC0415
    import options_state  # noqa: PLC0415
    from schemas import StructureLifecycle  # noqa: PLC0415

    try:
        _strategy_cfg = _load_strategy_config()
        open_structs  = options_state.get_open_structures()
        # Backfill fill prices for any submitted/filled structures missing them.
        _all_structs = options_state.load_structures()
        _sync_submitted_lifecycles(_all_structs, alpaca_client)
        _sync_closing_lifecycles(_all_structs, alpaca_client)
        _update_fill_prices(_all_structs, alpaca_client)

        # Include MANUAL_REVIEW_REQUIRED structures that have filled legs.
        # These positions are live in Alpaca; close failed max times but the
        # position still exists and needs to be monitored and closed.
        _mrr_structs = [
            s for s in _all_structs
            if s.lifecycle == StructureLifecycle.MANUAL_REVIEW_REQUIRED
            and any(leg.filled_price is not None for leg in s.legs)
        ]
        if _mrr_structs:
            log.info(
                "[CLOSE_CHECK] %d manual_review_required structure(s) with fills added to monitoring: %s",
                len(_mrr_structs), [s.structure_id[:8] for s in _mrr_structs],
            )
            open_structs = list(open_structs) + _mrr_structs

        # Fix 3: detect positions gone from Alpaca (manually closed / expired).
        # Fetch all A2 positions once and check each open struct's OCC symbols.
        _alpaca_syms: set[str] | None = None
        try:
            _alpaca_syms = {str(p.symbol) for p in alpaca_client.get_all_positions()}
        except Exception as _pe:
            log.debug("[CLOSE_CHECK] position fetch failed (non-fatal): %s", _pe)

        if open_structs:
            _now_utc = datetime.now(ET)
            _current_prices = _fetch_close_check_prices(open_structs)
            _latest_debate_result = _load_latest_debate_selected_candidate()

            # Friday EOD forced close: single_call/single_put held over the weekend
            # decay through theta and gap through stops without triggering exit logic
            # (prices return {} on weekends). Force close by 3:30 PM ET on Fridays.
            _NAKED_STRATEGIES = frozenset({"single_call", "single_put"})
            _is_friday_eod = (
                _now_utc.weekday() == 4          # Friday
                and _now_utc.hour == 15
                and _now_utc.minute >= 30
            )
            if _is_friday_eod:
                for _fri_struct in list(open_structs):
                    _strat_val = getattr(_fri_struct.strategy, "value", str(_fri_struct.strategy))
                    if _strat_val in _NAKED_STRATEGIES:
                        log.warning(
                            "[CLOSE_CHECK] Friday EOD forced close: %s (%s) strategy=%s",
                            _fri_struct.underlying, _fri_struct.structure_id, _strat_val,
                        )
                        _fri_closed = options_executor.close_structure(
                            _fri_struct, alpaca_client,
                            reason="friday_eod_forced_close", method="limit",
                            current_prices=_current_prices,
                        )
                        options_state.save_structure(_fri_closed)
                        _write_a2_trade({
                            "event_type":    "close_submitted",
                            "structure_id":  _fri_closed.structure_id,
                            "symbol":        _fri_closed.underlying,
                            "strategy":      _strat_val,
                            "close_reason":  "friday_eod_forced_close",
                            "lifecycle":     str(getattr(_fri_closed.lifecycle, "value", _fri_closed.lifecycle)),
                            "pnl_unrealized": getattr(_fri_closed, "pnl_unrealized", None),
                            "closed_at":     getattr(_fri_closed, "closed_at", None),
                        })
                        open_structs = [s for s in open_structs if s.structure_id != _fri_struct.structure_id]

            _PROFIT_TARGET_REASONS = frozenset({
                "profit_target_pct_hit",
                "profit_target_50_near_expiry",
                "target_profit_hit",
                "iv_expansion_take_profit",
                "profit_lock_retrace",
                "cost_basis_profit_target",
            })
            _MIN_PROFIT_HOLD_MIN = 15  # minutes before any profit-target exit is allowed

            for struct in list(open_structs):
                # Fix 3: position-gone guard (skip structures opened < 10 min ago).
                if _alpaca_syms is not None:
                    try:
                        _age_min = (
                            _now_utc
                            - datetime.fromisoformat(struct.opened_at.replace("Z", "+00:00"))
                              .astimezone(ET)
                        ).total_seconds() / 60
                    except Exception:
                        _age_min = 9999.0
                    _leg_occs = {leg.occ_symbol for leg in struct.legs if leg.occ_symbol}
                    if _leg_occs and _age_min > 10 and not any(
                        occ in _alpaca_syms for occ in _leg_occs
                    ):
                        log.info(
                            "[CLOSE_CHECK] %s (%s): no Alpaca position found — lifecycle→closed",
                            struct.underlying, struct.structure_id,
                        )
                        struct.realized_pnl = struct.pnl_unrealized
                        struct.lifecycle = StructureLifecycle.CLOSED
                        struct.closed_at = datetime.now(ET).isoformat()
                        struct.close_reason_code = "position_not_in_alpaca"
                        struct.close_reason_detail = (
                            f"auto-closed: position absent from Alpaca at "
                            f"{datetime.now(ET).isoformat()}"
                        )
                        struct.add_audit(
                            "auto-closed: no matching Alpaca position found in close_check_loop"
                        )
                        options_state.save_structure(struct)
                        continue

                # Fix 2: update pnl_unrealized snapshot.
                _pnl = _compute_pnl_unrealized(struct, _current_prices)
                _save_needed = False
                if _pnl is not None and struct.pnl_unrealized != _pnl:
                    struct.pnl_unrealized = _pnl
                    _save_needed = True

                # Tiered-exit Layer 1d: track peak P&L for profit-lock retrace.
                # Only update on positive moves so the peak monotonically rises.
                _eval_pnl = struct.pnl_unrealized
                if _eval_pnl is not None and _eval_pnl > 0:
                    if struct.peak_pnl is None or _eval_pnl > struct.peak_pnl:
                        struct.peak_pnl = float(_eval_pnl)
                        struct.peak_pnl_at = datetime.now(ET).isoformat()
                        _save_needed = True

                if _save_needed:
                    options_state.save_structure(struct)

                should_close, close_reason = options_executor.should_close_structure(
                    struct, current_prices=_current_prices, config=_strategy_cfg,
                    current_time=None,
                )
                # Minimum hold guard: phantom PnL from corrupted fill-price
                # storage can immediately fire profit targets right after entry.
                # Block all profit-target exits for the first _MIN_PROFIT_HOLD_MIN
                # minutes — stop-loss, DTE, and time-stops are never suppressed.
                if should_close and any(
                    close_reason.startswith(pr) for pr in _PROFIT_TARGET_REASONS
                ):
                    try:
                        _hold_age_min = (
                            _now_utc
                            - datetime.fromisoformat(
                                struct.opened_at.replace("Z", "+00:00")
                            ).astimezone(ET)
                        ).total_seconds() / 60
                    except Exception:
                        _hold_age_min = 9999.0
                    if _hold_age_min < _MIN_PROFIT_HOLD_MIN:
                        log.info(
                            "[CLOSE_CHECK] %s: profit-target '%s' suppressed "
                            "— hold guard (age=%.1f min < %d min)",
                            struct.underlying, close_reason,
                            _hold_age_min, _MIN_PROFIT_HOLD_MIN,
                        )
                        should_close = False
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
                        _closed = options_executor.close_structure(
                            struct, alpaca_client, reason=close_reason, method="limit",
                            current_prices=_current_prices,
                        )
                        options_state.save_structure(_closed)
                        _write_a2_trade({
                            "event_type":    "close_submitted",
                            "structure_id":  _closed.structure_id,
                            "symbol":        _closed.underlying,
                            "strategy":      str(getattr(_closed.strategy, "value", _closed.strategy)),
                            "close_reason":  close_reason,
                            "lifecycle":     str(getattr(_closed.lifecycle, "value", _closed.lifecycle)),
                            "pnl_unrealized": getattr(_closed, "pnl_unrealized", None),
                            "closed_at":     getattr(_closed, "closed_at", None),
                        })
                else:
                    # Position-intel close: act on CLOSE recommendations written
                    # by run() earlier this cycle. Handles VEGA_COLLAPSE,
                    # DELTA_ITM, SHORT_LEG_ITM and other PI-detected signals.
                    # Bug: previously gated behind an operator flag
                    # (default False) + urgency==immediate filter — both
                    # conditions silently dropped every PI CLOSE recommendation.
                    # Fix: act on any ACTION_CLOSE / ACTION_CLOSE_SHORT_LEG.
                    _intel_acted = False
                    try:
                        import options_position_manager as _opm  # noqa: PLC0415
                        _recs = _opm.get_recommendations(symbol=struct.underlying)
                        for _rec in _recs:
                            if _rec.structure_id != struct.structure_id:
                                continue
                            if _rec.action not in (
                                _opm.ACTION_CLOSE, _opm.ACTION_CLOSE_SHORT_LEG,
                            ):
                                continue
                            _close_reason = f"position_intel:{_rec.action.lower()}:{_rec.reason[:60]}"
                            log.info(
                                "[CLOSE_CHECK] %s (%s): position intel close [%s] \u2014 %s",
                                struct.underlying, struct.structure_id,
                                _rec.urgency, _rec.reason,
                            )
                            _closed = options_executor.close_structure(
                                struct, alpaca_client, reason=_close_reason,
                                method="limit", current_prices=_current_prices,
                            )
                            options_state.save_structure(_closed)
                            _write_a2_trade({
                                "event_type":    "close_submitted",
                                "structure_id":  _closed.structure_id,
                                "symbol":        _closed.underlying,
                                "strategy":      str(getattr(_closed.strategy, "value", _closed.strategy)),
                                "close_reason":  _close_reason,
                                "lifecycle":     str(getattr(_closed.lifecycle, "value", _closed.lifecycle)),
                                "pnl_unrealized": getattr(_closed, "pnl_unrealized", None),
                                "closed_at":     getattr(_closed, "closed_at", None),
                            })
                            _intel_acted = True
                            break
                    except Exception as _pi_exc:
                        log.debug("[CLOSE_CHECK] position_intel act-check failed: %s", _pi_exc)

                    if _intel_acted:
                        continue

                    # Structure is not closing — evaluate upgrade opportunity.
                    _prev_ts = struct.last_upgrade_attempted
                    _upgrade = _evaluate_structure_upgrade(
                        struct, _latest_debate_result, _strategy_cfg,
                    )
                    if struct.last_upgrade_attempted != _prev_ts:
                        options_state.save_structure(struct)
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
