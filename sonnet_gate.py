"""
sonnet_gate.py — Sonnet call gate for BullBearBot.

Controls whether the expensive Sonnet (Stage 3) call runs each cycle.
Gate is state-change based, not time-based.
Target: ~20-30% of market-hours cycles call Sonnet.

Public API
──────────
load_gate_state()  -> GateState
save_gate_state(state: GateState) -> None
should_run_sonnet(...) -> tuple[bool, list[TriggerReason], GateState]
should_use_compact_prompt(...) -> bool
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

GATE_STATE_PATH = Path("data/market/gate_state.json")

# ─────────────────────────────────────────────────────────────────────────────
# Trigger reasons
# ─────────────────────────────────────────────────────────────────────────────

class TriggerReason(str, Enum):
    NEW_CATALYST         = "new_catalyst"
    SIGNAL_THRESHOLD     = "signal_threshold"
    REGIME_CHANGE        = "regime_change"
    RISK_ANOMALY         = "risk_anomaly"
    POSITION_CHANGE      = "position_change"
    DEADLINE_APPROACHING = "deadline_approaching"
    SCHEDULED_WINDOW     = "scheduled_window"
    RECON_ANOMALY        = "recon_anomaly"
    COOLDOWN_EXPIRED     = "cooldown_expired"
    MAX_SKIP_EXCEEDED    = "max_skip_exceeded"
    HARD_OVERRIDE        = "hard_override"


# ─────────────────────────────────────────────────────────────────────────────
# Gate state — persisted across cycles
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class GateState:
    last_sonnet_call_utc: Optional[str]   # ISO timestamp; None = never called
    last_regime:          str             # last regime string seen
    last_top_symbol:      Optional[str]   # symbol with highest signal score
    last_top_score:       float           # its score
    last_exposure_pct:    float           # portfolio exposure as fraction (0.0–2.0)
    last_positions_hash:  str             # short hash of open positions
    last_catalyst_hash:   str             # short hash of breaking_news
    last_recon_anomaly:   bool            # True if last cycle had a recon anomaly
    consecutive_skips:    int             # cycles skipped since last Sonnet call
    total_calls_today:    int             # Sonnet calls today
    total_skips_today:    int             # skip cycles today
    date_str:             str             # "YYYY-MM-DD" for daily reset
    compact_calls_today:  int   = 0
    full_calls_today:     int   = 0
    skip_cycles_today:    int   = 0
    avg_compact_tokens:   float = 0.0
    avg_full_tokens:      float = 0.0


_GATE_DEFAULTS: dict = {
    "last_sonnet_call_utc": None,
    "last_regime":          "unknown",
    "last_top_symbol":      None,
    "last_top_score":       0.0,
    "last_exposure_pct":    0.0,
    "last_positions_hash":  "",
    "last_catalyst_hash":   "",
    "last_recon_anomaly":   False,
    "consecutive_skips":    0,
    "total_calls_today":    0,
    "total_skips_today":    0,
    "date_str":             "",
    "compact_calls_today":  0,
    "full_calls_today":     0,
    "skip_cycles_today":    0,
    "avg_compact_tokens":   0.0,
    "avg_full_tokens":      0.0,
}


def load_gate_state() -> GateState:
    """Load gate state from disk, returning fresh defaults if missing or corrupt."""
    if GATE_STATE_PATH.exists():
        try:
            data   = json.loads(GATE_STATE_PATH.read_text())
            merged = {**_GATE_DEFAULTS, **data}
            return GateState(**{k: merged[k] for k in _GATE_DEFAULTS})
        except Exception as exc:
            log.warning("[GATE] Failed to load gate state (%s) — using defaults", exc)
    return GateState(**_GATE_DEFAULTS)


def save_gate_state(state: GateState) -> None:
    """Persist gate state to disk (non-fatal)."""
    try:
        GATE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        GATE_STATE_PATH.write_text(json.dumps(asdict(state), indent=2))
    except Exception as exc:
        log.warning("[GATE] Failed to save gate state: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _hash8(text: str) -> str:
    """8-character MD5 prefix — lightweight fingerprint for change detection."""
    return hashlib.md5(text.encode()).hexdigest()[:8]


def _hash_positions(positions: list) -> str:
    """Stable hash of (symbol, qty) pairs — detects opens/closes/size changes."""
    if not positions:
        return "empty"
    try:
        items = sorted(
            [
                (str(getattr(p, "symbol", "")), float(getattr(p, "qty", 0)))
                for p in positions
            ],
            key=lambda x: x[0],
        )
        return _hash8(json.dumps(items))
    except Exception:
        return "error"


def _daily_reset_if_needed(state: GateState, today_str: str) -> GateState:
    """Reset per-day counters when the calendar date rolls over."""
    if state.date_str != today_str:
        state.total_calls_today   = 0
        state.total_skips_today   = 0
        state.compact_calls_today = 0
        state.full_calls_today    = 0
        state.skip_cycles_today   = 0
        state.avg_compact_tokens  = 0.0
        state.avg_full_tokens     = 0.0
        state.date_str            = today_str
    return state


def _minutes_since_last_call(state: GateState, now_utc: datetime) -> float:
    """Returns minutes since last Sonnet call, or 9999 if never called."""
    if not state.last_sonnet_call_utc:
        return 9999.0
    try:
        last = datetime.fromisoformat(state.last_sonnet_call_utc)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (now_utc - last).total_seconds() / 60.0
    except (ValueError, TypeError):
        return 9999.0


def _update_state_on_call(
    state:        GateState,
    now_utc:      datetime,
    regime:       str,
    top_symbol:   Optional[str],
    top_score:    float,
    exposure_pct: float,
    positions:    list,
    catalyst_hash: str,
) -> GateState:
    """Apply post-call state updates (called when Sonnet fires)."""
    state.last_sonnet_call_utc = now_utc.isoformat()
    state.last_regime          = regime
    state.last_top_symbol      = top_symbol
    state.last_top_score       = top_score
    state.last_exposure_pct    = exposure_pct
    state.last_positions_hash  = _hash_positions(positions)
    state.last_catalyst_hash   = catalyst_hash
    state.consecutive_skips    = 0
    state.total_calls_today   += 1
    return state


# ─────────────────────────────────────────────────────────────────────────────
# Core gate function
# ─────────────────────────────────────────────────────────────────────────────

def should_run_sonnet(
    session_tier:          str,
    regime:                str,       # pre-derived: "halt"/"caution"/"risk-on"/etc.
    vix:                   float,
    signal_scores:         dict,
    positions:             list,
    recon_diff,                       # Optional[ReconciliationDiff]; None = unavailable
    breaking_news:         str,
    time_bound_actions:    list,
    current_time_et:       datetime,
    gate_state:            GateState,
    config:                dict,
    equity:                float = 0.0,  # account equity for exposure delta calc
) -> tuple[bool, list[TriggerReason], GateState]:
    """
    Decide whether to call Sonnet this cycle.

    Returns (should_call, reasons, updated_state).
    - should_call=False → skip Sonnet; reconciliation + stop management still run.
    - reasons is an empty list when skipping.

    Gate only fires for market/extended sessions — overnight uses Haiku separately.
    """
    # Overnight is handled by a dedicated Haiku path; gate never applies there.
    if session_tier == "overnight":
        return True, [TriggerReason.HARD_OVERRIDE], gate_state

    gate_cfg = config.get("sonnet_gate", {})
    cooldown_minutes       = float(gate_cfg.get("cooldown_minutes",           15))
    max_consecutive_skips  = int(  gate_cfg.get("max_consecutive_skips",      12))
    signal_score_threshold = float(gate_cfg.get("signal_score_threshold",     15))
    exposure_change_thresh = float(gate_cfg.get("exposure_change_threshold", 0.05))
    deadline_warn_minutes  = float(gate_cfg.get("deadline_warning_minutes",   30))
    scheduled_windows      = gate_cfg.get("scheduled_windows", [])

    # Use current_time_et (caller-provided) so tests can simulate time
    now_utc   = current_time_et.astimezone(timezone.utc)
    today_str = current_time_et.strftime("%Y-%m-%d")
    gate_state = _daily_reset_if_needed(gate_state, today_str)

    # ── Pre-compute current signal state ────────────────────────────────────
    scored     = signal_scores.get("scored_symbols", {}) if isinstance(signal_scores, dict) else {}
    top_score  = 0.0
    top_symbol: Optional[str] = None
    if scored:
        top_sym_entry = max(
            scored.items(),
            key=lambda kv: float(kv[1].get("score", 0)) if isinstance(kv[1], dict) else 0.0,
        )
        top_symbol = top_sym_entry[0]
        top_score  = float(top_sym_entry[1].get("score", 0)) if isinstance(top_sym_entry[1], dict) else 0.0

    # Exposure fraction
    total_pos_val = sum(float(getattr(p, "market_value", 0) or 0) for p in positions)
    current_exposure_pct = (total_pos_val / equity) if equity > 0 else 0.0

    # Catalyst hash (first 500 chars of breaking news)
    current_catalyst_hash = _hash8((breaking_news or "")[:500])

    # ── STEP 1: Hard overrides (bypass cooldown) ─────────────────────────────
    hard_override_reasons: list[TriggerReason] = []

    # HO-1: Dangerous regime
    if regime == "halt" or (regime == "caution" and vix > 30):
        hard_override_reasons.append(TriggerReason.HARD_OVERRIDE)

    # HO-2: Reconciliation has CRITICAL actions (forced/deadline exits)
    if recon_diff is not None:
        _recon_actions = getattr(recon_diff, "actions", []) or []
        if any(getattr(a, "priority", "") == "CRITICAL" for a in _recon_actions):
            hard_override_reasons.append(TriggerReason.HARD_OVERRIDE)

    # HO-3: Too many consecutive skips — never go dark > max_consecutive_skips cycles
    if gate_state.consecutive_skips >= max_consecutive_skips:
        hard_override_reasons.append(TriggerReason.MAX_SKIP_EXCEEDED)

    if hard_override_reasons:
        gate_state = _update_state_on_call(
            gate_state, now_utc, regime, top_symbol, top_score,
            current_exposure_pct, positions, current_catalyst_hash,
        )
        log.info("[GATE] SONNET hard override: %s", [r.value for r in hard_override_reasons])
        return True, hard_override_reasons, gate_state

    # ── STEP 2: Cooldown check ───────────────────────────────────────────────
    minutes_since = _minutes_since_last_call(gate_state, now_utc)
    cooldown_active = minutes_since < cooldown_minutes

    if cooldown_active:
        gate_state.consecutive_skips += 1
        gate_state.total_skips_today += 1
        gate_state.skip_cycles_today += 1
        log.debug(
            "[GATE] SKIP cycle %d — cooldown active (%.1f/%.0f min)",
            gate_state.consecutive_skips, minutes_since, cooldown_minutes,
        )
        return False, [], gate_state

    # ── STEP 3: Trigger conditions (cooldown expired) ────────────────────────
    trigger_reasons: list[TriggerReason] = []

    # T-5: New catalyst
    if current_catalyst_hash != gate_state.last_catalyst_hash:
        trigger_reasons.append(TriggerReason.NEW_CATALYST)

    # T-6: Signal threshold
    score_delta = top_score - gate_state.last_top_score
    if score_delta >= signal_score_threshold:
        trigger_reasons.append(TriggerReason.SIGNAL_THRESHOLD)
    elif top_symbol and top_symbol != gate_state.last_top_symbol and top_score >= 65:
        trigger_reasons.append(TriggerReason.SIGNAL_THRESHOLD)

    # T-7: Regime change
    if regime != gate_state.last_regime:
        trigger_reasons.append(TriggerReason.REGIME_CHANGE)

    # T-8: Exposure anomaly
    if equity > 0:
        exposure_delta = abs(current_exposure_pct - gate_state.last_exposure_pct)
        if exposure_delta >= exposure_change_thresh:
            trigger_reasons.append(TriggerReason.RISK_ANOMALY)

    # T-9: Position change (open/close/resize)
    current_positions_hash = _hash_positions(positions)
    if current_positions_hash != gate_state.last_positions_hash:
        trigger_reasons.append(TriggerReason.POSITION_CHANGE)

    # T-10: Deadline approaching
    for tba in (time_bound_actions or []):
        exit_by = tba.get("exit_by") or tba.get("deadline")
        if not exit_by:
            continue
        try:
            dl_dt = datetime.fromisoformat(str(exit_by).replace("Z", "+00:00"))
            minutes_until = (dl_dt - now_utc).total_seconds() / 60.0
            if 0 < minutes_until <= deadline_warn_minutes:
                trigger_reasons.append(TriggerReason.DEADLINE_APPROACHING)
                break
        except (ValueError, TypeError, AttributeError):
            pass

    # T-11: Scheduled window (post-ORB, pre-close setup, pre-close execution)
    cur_min = current_time_et.hour * 60 + current_time_et.minute
    for win in scheduled_windows:
        try:
            sh, sm = map(int, win.get("start", "00:00").split(":"))
            eh, em = map(int, win.get("end",   "00:00").split(":"))
            if (sh * 60 + sm) <= cur_min <= (eh * 60 + em):
                trigger_reasons.append(TriggerReason.SCHEDULED_WINDOW)
                break
        except (ValueError, KeyError, AttributeError):
            pass

    # T-12: Recon anomaly (missing stops or orphaned duplicate orders)
    if recon_diff is not None:
        _missing = getattr(recon_diff, "missing_stops", []) or []
        _actions = getattr(recon_diff, "actions", []) or []
        _orphaned = any(getattr(a, "action_type", "") == "cancel_duplicate" for a in _actions)
        if _missing or _orphaned:
            trigger_reasons.append(TriggerReason.RECON_ANOMALY)

    # ── STEP 4: Decide ───────────────────────────────────────────────────────
    if trigger_reasons:
        # Append COOLDOWN_EXPIRED as companion reason (cooldown was satisfied)
        trigger_reasons.append(TriggerReason.COOLDOWN_EXPIRED)
        gate_state = _update_state_on_call(
            gate_state, now_utc, regime, top_symbol, top_score,
            current_exposure_pct, positions, current_catalyst_hash,
        )
        log.info("[GATE] SONNET triggered: %s", [r.value for r in trigger_reasons])
        return True, trigger_reasons, gate_state
    else:
        gate_state.consecutive_skips += 1
        gate_state.total_skips_today += 1
        gate_state.skip_cycles_today += 1
        log.debug(
            "[GATE] SKIP cycle %d — no material change detected",
            gate_state.consecutive_skips,
        )
        return False, [], gate_state


# ─────────────────────────────────────────────────────────────────────────────
# Compact vs full prompt selector
# ─────────────────────────────────────────────────────────────────────────────

def should_use_compact_prompt(
    reasons:       list[TriggerReason],
    positions:     list,
    signal_scores: dict,
    recon_diff,
) -> bool:
    """
    Returns True if the compact 6-block prompt should be used.
    Returns False if the full compressed prompt is required.

    Compact is the default for low-information cycles.
    Full is reserved for high-information cycles with actionable new state.
    """
    reason_set = set(reasons)

    # Hard override triggers → full context always
    if TriggerReason.HARD_OVERRIDE in reason_set:
        return False
    if TriggerReason.MAX_SKIP_EXCEEDED in reason_set:
        return False

    # New information triggers → full
    if TriggerReason.NEW_CATALYST in reason_set:
        return False
    if TriggerReason.REGIME_CHANGE in reason_set:
        return False
    if TriggerReason.DEADLINE_APPROACHING in reason_set:
        return False
    if TriggerReason.RECON_ANOMALY in reason_set:
        return False
    if TriggerReason.POSITION_CHANGE in reason_set:
        return False
    if TriggerReason.RISK_ANOMALY in reason_set:
        return False

    # Signal threshold: full if score >= 75
    if TriggerReason.SIGNAL_THRESHOLD in reason_set:
        scored = signal_scores.get("scored_symbols", {}) if isinstance(signal_scores, dict) else {}
        if scored:
            peak = max(
                (float(v.get("score", 0)) if isinstance(v, dict) else 0.0)
                for v in scored.values()
            )
            if peak >= 75:
                return False
        # score < 75 SIGNAL_THRESHOLD → still full (meaningful setup developing)
        return False

    # recon_diff has CRITICAL actions → full
    if recon_diff is not None:
        _actions = getattr(recon_diff, "actions", []) or []
        if any(getattr(a, "priority", "") == "CRITICAL" for a in _actions):
            return False

    # 3+ open positions → full (more exit decisions pending)
    if len(positions) >= 3:
        return False

    # Only COOLDOWN_EXPIRED / SCHEDULED_WINDOW triggered, < 3 positions → compact
    return True
