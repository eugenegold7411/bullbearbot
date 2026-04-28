"""
divergence.py — Live vs paper divergence tracking, classification,
and operating mode management for BullBearBot.

This module is completely non-fatal. Every public function wraps
failures in try/except and logs warnings. A crash here must never
propagate to the caller.

Sections:
  1. Data models (enums, dataclasses, EVENT_TYPES)
  2. Event log (log_divergence_event, generate_event_id)
  3. Operating mode state (load/save/transition)
  4. Divergence classifier (classify_divergence)
  5. Repeat escalation tracker (check_repeat_escalation)
  6. Mode enforcement (is_action_allowed)
  7. Fill divergence detector (detect_fill_divergence)
  8. Protection divergence detector (detect_protection_divergence)
  9. Mode response engine (respond_to_divergence)
  10. Clean cycle checker (check_clean_cycle)
  11. Summary for weekly review (get_divergence_summary)
"""

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Section 1 — Data models
# ---------------------------------------------------------------------------

class DivergenceSeverity(str, Enum):
    INFO = "info"
    RECONCILE = "reconcile"
    DE_RISK = "de_risk"
    HALT = "halt"


class DivergenceScope(str, Enum):
    ORDER = "order"
    SYMBOL = "symbol"
    STRUCTURE = "structure"
    ACCOUNT = "account"
    GLOBAL = "global"


class OperatingMode(str, Enum):
    NORMAL = "normal"
    RECONCILE_ONLY = "reconcile_only"
    RISK_CONTAINMENT = "risk_containment"
    HALTED = "halted"


@dataclass
class DivergenceEvent:
    event_id: str
    timestamp: str
    account: str                    # "A1" | "A2"
    symbol: str
    event_type: str                 # see EVENT_TYPES below
    severity: DivergenceSeverity
    scope: DivergenceScope
    scope_id: str                   # symbol, structure_id, etc.
    paper_expected: dict            # what bot thought would happen
    live_observed: dict             # what actually happened
    delta: dict                     # difference
    recoverability: str             # "auto" | "guarded_auto" | "manual"
    risk_impact: str                # "none" | "low" | "medium" | "high"
    repaired: bool = False
    repair_attempt_count: int = 0
    decision_id: Optional[str] = None
    trade_id: Optional[str] = None
    structure_id: Optional[str] = None


# Event types
EVENT_TYPES = [
    # Order level
    "order_rejected",
    "order_partial_fill",
    "fill_price_drift",      # fill worse than expected by threshold
    "fill_timing_lag",       # fill took much longer than expected
    "order_shape_mismatch",  # qty/type/TIF different from intended
    # Position level
    "stop_missing",          # position open, no stop order
    "stop_wrong_price",      # stop exists but at wrong price
    "target_missing",        # expected target not found
    "duplicate_exit",        # multiple stop/target orders
    "position_unexpected",   # position exists with no matching decision
    "exposure_mismatch",     # actual exposure != expected exposure
    # Options structure level
    "structure_partial_fill",   # one leg filled, one missing
    "structure_broken",         # leg mismatch / orphaned leg
    "structure_near_expiry",    # DTE <= 2, action needed
    "structure_close_failed",   # close order rejected/failed
    # Account level
    "cash_mismatch",         # actual cash != expected cash
    "buying_power_mismatch",
    "position_count_mismatch",
    # Protection level (always high risk_impact)
    "deadline_exit_failed",  # deadline exit order not confirmed
    "protection_missing",    # meaningful position with no stop
]

# ---------------------------------------------------------------------------
# Section 2 — Event log
# ---------------------------------------------------------------------------

DIVERGENCE_LOG = Path("data/analytics/divergence_log.jsonl")


def generate_event_id() -> str:
    return f"div_{int(time.time() * 1000)}"


def log_divergence_event(event: DivergenceEvent) -> None:
    """Append divergence event to log. Non-fatal."""
    try:
        DIVERGENCE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(DIVERGENCE_LOG, "a") as f:
            f.write(json.dumps({
                "event_id": event.event_id,
                "timestamp": event.timestamp,
                "account": event.account,
                "symbol": event.symbol,
                "event_type": event.event_type,
                "severity": event.severity.value,
                "scope": event.scope.value,
                "scope_id": event.scope_id,
                "paper_expected": event.paper_expected,
                "live_observed": event.live_observed,
                "delta": event.delta,
                "recoverability": event.recoverability,
                "risk_impact": event.risk_impact,
                "repaired": event.repaired,
                "repair_attempt_count": event.repair_attempt_count,
                "decision_id": event.decision_id,
                "trade_id": event.trade_id,
                "structure_id": event.structure_id,
            }) + "\n")
        try:
            from cost_attribution import _rotate_jsonl  # noqa: PLC0415
            _rotate_jsonl(DIVERGENCE_LOG)
        except Exception:
            pass
    except Exception as e:
        log.warning("[DIV] log_divergence_event failed: %s", e)

    # T1.5 incident wiring — best-effort, non-fatal
    _INCIDENT_SEVERITIES = {"reconcile", "de_risk", "halt"}
    if event.severity.value in _INCIDENT_SEVERITIES:
        try:
            from incident_schema import build_incident, log_incident  # noqa: PLC0415
            severity_map = {"reconcile": "warning", "de_risk": "critical", "halt": "halt"}
            inc = build_incident(
                incident_type=event.event_type,
                account=event.account,
                severity=severity_map.get(event.severity.value, "warning"),
                description=(
                    f"{event.event_type} on {event.symbol or 'account'} "
                    f"scope={event.scope.value}"
                ),
                subject_id=event.decision_id or event.structure_id or event.symbol,
                subject_type="decision" if event.decision_id else (
                    "structure" if event.structure_id else "symbol"
                ),
                linked_divergence_event=event.event_id,
                linked_structure_id=event.structure_id,
            )
            log_incident(inc)
        except Exception as _inc_exc:
            log.warning("[DIV] incident wiring failed (non-fatal): %s", _inc_exc)


# ---------------------------------------------------------------------------
# Section 3 — Operating mode state
# ---------------------------------------------------------------------------

RUNTIME_DIR = Path("data/runtime")
MODE_TRANSITION_LOG = RUNTIME_DIR / "mode_transitions.jsonl"
DIVERGENCE_COUNTS_PATH = RUNTIME_DIR / "divergence_counts.json"


def get_mode_path(account: str) -> Path:
    return RUNTIME_DIR / f"{account.lower()}_mode.json"


@dataclass
class AccountMode:
    account: str
    mode: OperatingMode
    scope: DivergenceScope
    scope_id: str                   # which symbol/structure frozen
    reason_code: str
    reason_detail: str
    entered_at: str
    entered_by: str                 # "reconciliation"|"divergence"|"manual"
    recovery_condition: str         # "one_clean_cycle"|"two_clean_cycles"
    last_checked_at: str
    clean_cycles_since_entry: int = 0
    version: int = 1


def load_account_mode(account: str) -> AccountMode:
    """Load current mode. Returns NORMAL if file missing. Non-fatal."""
    path = get_mode_path(account)
    try:
        if path.exists():
            d = json.loads(path.read_text())
            return AccountMode(
                account=d["account"],
                # T1-4: normalize case before enum lookup — prevents silent HALTED→NORMAL
                # fallback when JSON stores uppercase (e.g. manual edits).
                # TODO (post-paper-trading hardening): tighten to raise on unknown mode
                # instead of silently falling back to NORMAL via the outer except.
                mode=OperatingMode(str(d["mode"]).lower()),
                scope=DivergenceScope(str(d["scope"]).lower()),
                scope_id=d.get("scope_id", ""),
                reason_code=d.get("reason_code", ""),
                reason_detail=d.get("reason_detail", ""),
                entered_at=d.get("entered_at", ""),
                entered_by=d.get("entered_by", ""),
                recovery_condition=d.get("recovery_condition",
                                         "one_clean_cycle"),
                last_checked_at=d.get("last_checked_at", ""),
                clean_cycles_since_entry=d.get(
                    "clean_cycles_since_entry", 0),
                version=d.get("version", 1),
            )
    except Exception as e:
        log.warning("[DIV] load_account_mode failed: %s", e)
    return AccountMode(
        account=account,
        mode=OperatingMode.NORMAL,
        scope=DivergenceScope.ACCOUNT,
        scope_id="",
        reason_code="",
        reason_detail="",
        entered_at="",
        entered_by="system",
        recovery_condition="one_clean_cycle",
        last_checked_at=datetime.now(timezone.utc).isoformat(),
    )


def save_account_mode(mode_state: AccountMode) -> None:
    """Save mode atomically. Non-fatal."""
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        path = get_mode_path(mode_state.account)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "account": mode_state.account,
            "mode": mode_state.mode.value,
            "scope": mode_state.scope.value,
            "scope_id": mode_state.scope_id,
            "reason_code": mode_state.reason_code,
            "reason_detail": mode_state.reason_detail,
            "entered_at": mode_state.entered_at,
            "entered_by": mode_state.entered_by,
            "recovery_condition": mode_state.recovery_condition,
            "last_checked_at": mode_state.last_checked_at,
            "clean_cycles_since_entry":
                mode_state.clean_cycles_since_entry,
            "version": mode_state.version,
        }, indent=2))
        tmp.rename(path)
    except Exception as e:
        log.warning("[DIV] save_account_mode failed: %s", e)


def transition_mode(
    account: str,
    new_mode: OperatingMode,
    scope: DivergenceScope,
    scope_id: str,
    reason_code: str,
    reason_detail: str,
    entered_by: str,
    recovery_condition: str = "one_clean_cycle",
    trigger_event_id: Optional[str] = None,
) -> AccountMode:
    """
    Transition account to new operating mode.
    Logs transition. Returns new AccountMode. Non-fatal.
    """
    old_mode = load_account_mode(account)
    now = datetime.now(timezone.utc).isoformat()

    new_state = AccountMode(
        account=account,
        mode=new_mode,
        scope=scope,
        scope_id=scope_id,
        reason_code=reason_code,
        reason_detail=reason_detail,
        entered_at=now,
        entered_by=entered_by,
        recovery_condition=recovery_condition,
        last_checked_at=now,
    )
    save_account_mode(new_state)

    # Log transition
    try:
        MODE_TRANSITION_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(MODE_TRANSITION_LOG, "a") as f:
            f.write(json.dumps({
                "timestamp": now,
                "account": account,
                "old_mode": old_mode.mode.value,
                "new_mode": new_mode.value,
                "scope": scope.value,
                "scope_id": scope_id,
                "reason_code": reason_code,
                "reason_detail": reason_detail,
                "trigger_event_id": trigger_event_id,
            }) + "\n")
    except Exception as e:
        log.warning("[DIV] mode transition log failed: %s", e)

    if new_mode.value != old_mode.mode.value:
        log.warning(
            "[DIV] MODE TRANSITION %s: %s → %s scope=%s/%s reason=%s",
            account, old_mode.mode.value, new_mode.value,
            scope.value, scope_id, reason_code,
        )
    return new_state


# ---------------------------------------------------------------------------
# Section 4 — Divergence classifier
# ---------------------------------------------------------------------------

_SEVERITY_LADDER = [
    DivergenceSeverity.INFO,
    DivergenceSeverity.RECONCILE,
    DivergenceSeverity.DE_RISK,
    DivergenceSeverity.HALT,
]

_BASE_CLASSIFICATION = {
    # Info level
    "fill_price_drift":        (DivergenceSeverity.INFO,
                                DivergenceScope.ORDER, "auto"),
    "fill_timing_lag":         (DivergenceSeverity.INFO,
                                DivergenceScope.ORDER, "auto"),
    "order_shape_mismatch":    (DivergenceSeverity.RECONCILE,
                                DivergenceScope.ORDER, "guarded_auto"),
    # Reconcile level
    "duplicate_exit":          (DivergenceSeverity.RECONCILE,
                                DivergenceScope.SYMBOL, "auto"),
    "target_missing":          (DivergenceSeverity.RECONCILE,
                                DivergenceScope.SYMBOL, "auto"),
    "stop_wrong_price":        (DivergenceSeverity.RECONCILE,
                                DivergenceScope.SYMBOL, "auto"),
    "structure_partial_fill":  (DivergenceSeverity.RECONCILE,
                                DivergenceScope.STRUCTURE, "guarded_auto"),
    # De-risk level
    "stop_missing":            (DivergenceSeverity.DE_RISK,
                                DivergenceScope.SYMBOL, "guarded_auto"),
    "order_rejected":          (DivergenceSeverity.DE_RISK,
                                DivergenceScope.SYMBOL, "auto"),
    "order_partial_fill":      (DivergenceSeverity.RECONCILE,
                                DivergenceScope.ORDER, "auto"),
    "exposure_mismatch":       (DivergenceSeverity.DE_RISK,
                                DivergenceScope.ACCOUNT, "guarded_auto"),
    "structure_broken":        (DivergenceSeverity.DE_RISK,
                                DivergenceScope.STRUCTURE, "guarded_auto"),
    "structure_near_expiry":   (DivergenceSeverity.DE_RISK,
                                DivergenceScope.STRUCTURE, "auto"),
    # Halt level
    "protection_missing":      (DivergenceSeverity.HALT,
                                DivergenceScope.SYMBOL, "manual"),
    "deadline_exit_failed":    (DivergenceSeverity.HALT,
                                DivergenceScope.SYMBOL, "manual"),
    "position_unexpected":     (DivergenceSeverity.HALT,
                                DivergenceScope.ACCOUNT, "manual"),
    "cash_mismatch":           (DivergenceSeverity.HALT,
                                DivergenceScope.ACCOUNT, "manual"),
    "structure_close_failed":  (DivergenceSeverity.DE_RISK,
                                DivergenceScope.STRUCTURE, "guarded_auto"),
}


def classify_divergence(
    event_type: str,
    symbol: str,
    account: str,
    position_size_usd: float = 0,
    vix: float = 20,
    dte: Optional[int] = None,
    is_short_premium: bool = False,
) -> tuple[DivergenceSeverity, DivergenceScope, str]:
    """
    Classify divergence event into severity, scope, recoverability.
    Returns (severity, scope, recoverability).

    Risk-adjusts based on position size, VIX, DTE, short premium.
    Non-fatal.
    """
    try:
        severity, scope, recoverability = _BASE_CLASSIFICATION.get(
            event_type,
            (DivergenceSeverity.INFO, DivergenceScope.ORDER, "auto"),
        )

        # Risk adjustments — escalate severity
        large_position = position_size_usd > 5000
        stressed_market = vix > 25
        near_expiry = dte is not None and dte <= 2

        if large_position or stressed_market or near_expiry or is_short_premium:
            idx = _SEVERITY_LADDER.index(severity)
            if idx < len(_SEVERITY_LADDER) - 1:
                severity = _SEVERITY_LADDER[idx + 1]
            if recoverability == "auto":
                recoverability = "guarded_auto"

        return severity, scope, recoverability
    except Exception as e:
        log.warning("[DIV] classify_divergence failed: %s", e)
        return DivergenceSeverity.INFO, DivergenceScope.ORDER, "auto"


# ---------------------------------------------------------------------------
# Section 5 — Repeat escalation tracker
# ---------------------------------------------------------------------------

def check_repeat_escalation(
    account: str,
    event_type: str,
    scope_id: str,
    current_severity: DivergenceSeverity,
    window_cycles: int = 3,
) -> DivergenceSeverity:
    """
    Check if this event type on this scope_id has repeated enough
    to warrant automatic severity upgrade.

    Thresholds:
    - 2 same-class repeats on same object in 3 cycles → upgrade one level
    - 1 repeat of protection/exposure divergence after repair → DE_RISK min

    Returns upgraded severity (or same if no escalation). Non-fatal.
    """
    try:
        counts: dict = {}
        if DIVERGENCE_COUNTS_PATH.exists():
            counts = json.loads(DIVERGENCE_COUNTS_PATH.read_text())

        key = f"{account}:{event_type}:{scope_id}"
        entry = counts.get(key, {"count": 0, "cycles": [],
                                  "failed_repairs": 0})

        # Add current cycle (5-min cycle approximation)
        now_cycle = int(time.time() // 300)
        entry["cycles"] = [c for c in entry["cycles"]
                           if now_cycle - c <= window_cycles]
        entry["cycles"].append(now_cycle)
        entry["count"] = len(entry["cycles"])
        counts[key] = entry

        # Save
        DIVERGENCE_COUNTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        DIVERGENCE_COUNTS_PATH.write_text(json.dumps(counts, indent=2))

        # Check escalation thresholds
        idx = _SEVERITY_LADDER.index(current_severity)

        protection_types = {
            "stop_missing", "protection_missing",
            "deadline_exit_failed", "exposure_mismatch",
        }

        if entry["count"] >= 2:
            if current_severity == DivergenceSeverity.INFO:
                current_severity = DivergenceSeverity.RECONCILE
            elif idx < len(_SEVERITY_LADDER) - 1:
                current_severity = _SEVERITY_LADDER[idx + 1]

        if (event_type in protection_types
                and entry["count"] >= 1
                and entry.get("was_repaired")):
            current_severity = max(
                current_severity, DivergenceSeverity.DE_RISK,
                key=lambda s: _SEVERITY_LADDER.index(s),
            )

    except Exception as e:
        log.warning("[DIV] check_repeat_escalation failed: %s", e)

    return current_severity


# ---------------------------------------------------------------------------
# Section 6 — Mode enforcement
# ---------------------------------------------------------------------------

_BLOCKED_IN_CONTAINMENT = {"enter_long", "enter_short", "add", "reallocate"}
_ALLOWED_ALWAYS = {"close", "reduce", "stop_update", "recon",
                   "cancel", "deadline_exit"}


def is_action_allowed(
    mode_state: AccountMode,
    action_intent: str,
    symbol: str,
) -> tuple[bool, str]:
    """
    Check if an action is allowed given current operating mode.
    Returns (allowed, reason).

    NORMAL: all actions allowed
    RECONCILE_ONLY: only recon, close, reduce, stop_update
    RISK_CONTAINMENT: depends on scope
    HALTED: only deterministic close/stop/recon actions
    Non-fatal.
    """
    try:
        mode = mode_state.mode
        scope = mode_state.scope
        scope_id = mode_state.scope_id

        if mode == OperatingMode.NORMAL:
            return True, ""

        if action_intent in _ALLOWED_ALWAYS:
            return True, ""

        if mode == OperatingMode.RECONCILE_ONLY:
            if action_intent in _BLOCKED_IN_CONTAINMENT:
                return False, "mode=reconcile_only — new entries blocked"

        if mode == OperatingMode.RISK_CONTAINMENT:
            if action_intent in _BLOCKED_IN_CONTAINMENT:
                if scope == DivergenceScope.ACCOUNT:
                    return False, (
                        "mode=risk_containment scope=account "
                        "— new entries blocked"
                    )
                if scope == DivergenceScope.SYMBOL and symbol == scope_id:
                    return False, (
                        f"mode=risk_containment scope=symbol:{scope_id} "
                        f"— new {symbol} entries blocked"
                    )
                # Different symbol — allowed
                return True, ""

        if mode == OperatingMode.HALTED:
            if action_intent in _BLOCKED_IN_CONTAINMENT:
                return False, "mode=halted — all new entries blocked"

        return True, ""
    except Exception as e:
        log.warning("[DIV] is_action_allowed failed: %s", e)
        return True, ""  # fail open — never block on error


# ---------------------------------------------------------------------------
# Section 7 — Fill divergence detector
# ---------------------------------------------------------------------------

def detect_fill_divergence(
    symbol: str,
    account: str,
    intended_price: float,
    actual_fill_price: float,
    intended_qty: float,
    actual_qty: float,
    order_type: str,
    bid: Optional[float] = None,
    ask: Optional[float] = None,
    decision_id: Optional[str] = None,
    trade_id: Optional[str] = None,
) -> Optional[DivergenceEvent]:
    """
    Detect fill price/qty divergence after order execution.
    Returns first DivergenceEvent if divergence found, None if clean.

    Thresholds:
    - fill_price_drift: actual vs intended > 0.5%
    - order_partial_fill: actual_qty < intended_qty
    Non-fatal.
    """
    try:
        events: list[DivergenceEvent] = []
        mid = ((bid + ask) / 2) if bid and ask else intended_price

        # Price drift check
        if intended_price > 0:
            drift_pct = abs(actual_fill_price - intended_price) / intended_price
            if drift_pct > 0.005:  # 0.5% threshold
                severity, scope, recoverability = classify_divergence(
                    "fill_price_drift", symbol, account)
                events.append(DivergenceEvent(
                    event_id=generate_event_id(),
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    account=account,
                    symbol=symbol,
                    event_type="fill_price_drift",
                    severity=severity,
                    scope=scope,
                    scope_id=symbol,
                    paper_expected={"price": intended_price, "mid": mid},
                    live_observed={"price": actual_fill_price},
                    delta={"price_drift_pct": round(drift_pct * 100, 3)},
                    recoverability=recoverability,
                    risk_impact="low" if drift_pct < 0.01 else "medium",
                    decision_id=decision_id,
                    trade_id=trade_id,
                ))

        # Partial fill check
        if actual_qty < intended_qty:
            severity, scope, recoverability = classify_divergence(
                "order_partial_fill", symbol, account)
            events.append(DivergenceEvent(
                event_id=generate_event_id(),
                timestamp=datetime.now(timezone.utc).isoformat(),
                account=account,
                symbol=symbol,
                event_type="order_partial_fill",
                severity=severity,
                scope=scope,
                scope_id=symbol,
                paper_expected={"qty": intended_qty},
                live_observed={"qty": actual_qty},
                delta={"qty_shortfall": intended_qty - actual_qty},
                recoverability=recoverability,
                risk_impact="low",
                decision_id=decision_id,
                trade_id=trade_id,
            ))

        for event in events:
            log_divergence_event(event)

        return events[0] if events else None
    except Exception as e:
        log.warning("[DIV] detect_fill_divergence failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Section 8 — Protection divergence detector
# ---------------------------------------------------------------------------

def detect_protection_divergence(
    account: str,
    positions: list,       # list of NormalizedPosition
    open_orders: list,     # list of NormalizedOrder
    vix: float = 20,
) -> list[DivergenceEvent]:
    """
    Scan all positions for missing/wrong stops.
    Returns list of DivergenceEvents.

    Checks:
    1. Position with no stop → stop_missing or protection_missing (size)
    2. Position with duplicate stop orders → duplicate_exit
    Non-fatal.
    """
    events: list[DivergenceEvent] = []
    try:
        now = datetime.now(timezone.utc).isoformat()

        # Build stop order map
        stop_map: dict[str, list] = {}
        for o in open_orders:
            raw_type = str(getattr(o, "order_type", "")).lower()
            order_type = raw_type.split(".")[-1]
            if order_type in ("stop", "stop_limit", "trailing_stop"):
                # PENDING_REPLACE = in-flight replacement, not an independent stop.
                # Counting it causes false duplicate_exit events when the trail-stop
                # replace() call fails and exit_manager places a new stop alongside it.
                raw_status = str(getattr(o, "status", "")).lower().split(".")[-1]
                if raw_status == "pending_replace":
                    continue
                sym = o.symbol
                if sym not in stop_map:
                    stop_map[sym] = []
                stop_map[sym].append(o)

        for pos in positions:
            sym = pos.symbol
            size_usd = abs(pos.market_value)
            pos_stops = stop_map.get(sym, [])

            if not pos_stops:
                # No stop at all
                event_type = (
                    "protection_missing" if size_usd > 2000
                    else "stop_missing"
                )
                severity, scope, recoverability = classify_divergence(
                    event_type, sym, account,
                    position_size_usd=size_usd, vix=vix,
                )
                severity = check_repeat_escalation(
                    account, event_type, sym, severity)
                evt = DivergenceEvent(
                    event_id=generate_event_id(),
                    timestamp=now,
                    account=account,
                    symbol=sym,
                    event_type=event_type,
                    severity=severity,
                    scope=scope,
                    scope_id=sym,
                    paper_expected={"stop_exists": True},
                    live_observed={"stop_exists": False},
                    delta={"missing": True},
                    recoverability=recoverability,
                    risk_impact="high" if size_usd > 2000 else "medium",
                )
                log_divergence_event(evt)
                events.append(evt)

            elif len(pos_stops) > 1:
                # Multiple stops — only flag as duplicate if total stop qty
                # over-covers the position. Split-lot stops (e.g. two orders for
                # different tranches whose qty sums to the position qty) are valid.
                total_stop_qty = sum(
                    float(getattr(s, "qty", 0) or 0) for s in pos_stops
                )
                position_qty = float(getattr(pos, "qty", 0) or 0)
                if total_stop_qty <= position_qty:
                    # Stops partition the position — not a duplicate
                    pass
                else:
                    # Genuine over-coverage
                    severity, scope, recoverability = classify_divergence(
                        "duplicate_exit", sym, account,
                        position_size_usd=size_usd,
                    )
                    evt = DivergenceEvent(
                        event_id=generate_event_id(),
                        timestamp=now,
                        account=account,
                        symbol=sym,
                        event_type="duplicate_exit",
                        severity=severity,
                        scope=scope,
                        scope_id=sym,
                        paper_expected={"stop_count": 1},
                        live_observed={"stop_count": len(pos_stops)},
                        delta={"extra_stops": len(pos_stops) - 1},
                        recoverability=recoverability,
                        risk_impact="low",
                    )
                    log_divergence_event(evt)
                    events.append(evt)

    except Exception as e:
        log.warning("[DIV] detect_protection_divergence failed: %s", e)

    return events


# ---------------------------------------------------------------------------
# Section 9 — Mode response engine
# ---------------------------------------------------------------------------

def respond_to_divergence(
    events: list[DivergenceEvent],
    account: str,
    current_mode: AccountMode,
) -> AccountMode:
    """
    Given a list of divergence events, determine appropriate mode response.
    Returns updated AccountMode.

    Response ladder:
    - INFO only → stay NORMAL
    - Any RECONCILE → RECONCILE_ONLY (if currently NORMAL)
    - Any DE_RISK → RISK_CONTAINMENT at narrowest scope
    - Any HALT → HALTED at account scope
    Non-fatal.
    """
    try:
        if not events:
            return current_mode

        max_event = max(
            events,
            key=lambda e: _SEVERITY_LADDER.index(e.severity),
        )
        mode = current_mode.mode

        if max_event.severity == DivergenceSeverity.HALT:
            if mode != OperatingMode.HALTED:
                return transition_mode(
                    account=account,
                    new_mode=OperatingMode.HALTED,
                    scope=DivergenceScope.ACCOUNT,
                    scope_id=account,
                    reason_code=max_event.event_type,
                    reason_detail=str(max_event.delta),
                    entered_by="divergence_engine",
                    recovery_condition="manual_review",
                    trigger_event_id=max_event.event_id,
                )

        elif max_event.severity == DivergenceSeverity.DE_RISK:
            if mode in (OperatingMode.NORMAL, OperatingMode.RECONCILE_ONLY):
                scope = max_event.scope
                scope_id = max_event.scope_id
                recovery = (
                    "one_clean_cycle"
                    if scope != DivergenceScope.ACCOUNT
                    else "two_clean_cycles"
                )
                return transition_mode(
                    account=account,
                    new_mode=OperatingMode.RISK_CONTAINMENT,
                    scope=scope,
                    scope_id=scope_id,
                    reason_code=max_event.event_type,
                    reason_detail=str(max_event.delta),
                    entered_by="divergence_engine",
                    recovery_condition=recovery,
                    trigger_event_id=max_event.event_id,
                )

        elif max_event.severity == DivergenceSeverity.RECONCILE:
            if mode == OperatingMode.NORMAL:
                return transition_mode(
                    account=account,
                    new_mode=OperatingMode.RECONCILE_ONLY,
                    scope=max_event.scope,
                    scope_id=max_event.scope_id,
                    reason_code=max_event.event_type,
                    reason_detail=str(max_event.delta),
                    entered_by="divergence_engine",
                    recovery_condition="one_clean_cycle",
                    trigger_event_id=max_event.event_id,
                )

    except Exception as e:
        log.warning("[DIV] respond_to_divergence failed: %s", e)

    return current_mode


# ---------------------------------------------------------------------------
# Section 10 — Clean cycle checker
# ---------------------------------------------------------------------------

def check_clean_cycle(
    account: str,
    current_mode: AccountMode,
    divergence_events_this_cycle: list[DivergenceEvent],
) -> AccountMode:
    """
    If current cycle had no divergence events, increment clean cycle count.
    When recovery_condition is met, transition back to NORMAL.
    Non-fatal.
    """
    try:
        if current_mode.mode == OperatingMode.NORMAL:
            return current_mode

        has_new_divergence = len(divergence_events_this_cycle) > 0

        if not has_new_divergence:
            current_mode.clean_cycles_since_entry += 1
            current_mode.last_checked_at = (
                datetime.now(timezone.utc).isoformat()
            )

            recovery_met = False
            if current_mode.recovery_condition == "one_clean_cycle":
                recovery_met = current_mode.clean_cycles_since_entry >= 1
            elif current_mode.recovery_condition == "two_clean_cycles":
                recovery_met = current_mode.clean_cycles_since_entry >= 2
            elif current_mode.recovery_condition == "manual_review":
                recovery_met = False  # never auto-recover

            if recovery_met:
                return transition_mode(
                    account=account,
                    new_mode=OperatingMode.NORMAL,
                    scope=DivergenceScope.ACCOUNT,
                    scope_id="",
                    reason_code="clean_recovery",
                    reason_detail=(
                        f"clean_cycles="
                        f"{current_mode.clean_cycles_since_entry}"
                    ),
                    entered_by="clean_cycle_checker",
                    recovery_condition="one_clean_cycle",
                )

            save_account_mode(current_mode)
        else:
            # Reset clean cycle count on new divergence
            current_mode.clean_cycles_since_entry = 0
            save_account_mode(current_mode)

    except Exception as e:
        log.warning("[DIV] check_clean_cycle failed: %s", e)

    return current_mode


# ---------------------------------------------------------------------------
# Section 11 — Summary for weekly review
# ---------------------------------------------------------------------------

def get_divergence_summary(days_back: int = 7) -> dict:
    """
    Read divergence_log.jsonl, produce weekly review summary.
    Non-fatal.
    """
    from datetime import timedelta
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

        events: list[dict] = []
        if DIVERGENCE_LOG.exists():
            with open(DIVERGENCE_LOG) as f:
                for line in f:
                    try:
                        r = json.loads(line.strip())
                        ts = r.get("timestamp", "")
                        if ts:
                            dt = datetime.fromisoformat(
                                ts.replace("Z", "+00:00"))
                            if dt >= cutoff:
                                events.append(r)
                    except Exception:
                        pass

        if not events:
            return {"total_events": 0, "note": "No divergence data yet"}

        by_type: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        by_account: dict[str, int] = {}
        for e in events:
            k = e.get("event_type", "?")
            by_type[k] = by_type.get(k, 0) + 1
            s = e.get("severity", "?")
            by_severity[s] = by_severity.get(s, 0) + 1
            a = e.get("account", "?")
            by_account[a] = by_account.get(a, 0) + 1

        return {
            "total_events": len(events),
            "by_type": by_type,
            "by_severity": by_severity,
            "by_account": by_account,
            "halt_events": sum(
                1 for e in events if e.get("severity") == "halt"),
            "de_risk_events": sum(
                1 for e in events if e.get("severity") == "de_risk"),
        }
    except Exception as e:
        log.warning("[DIV] get_divergence_summary failed: %s", e)
        return {"total_events": 0, "note": f"error: {e}"}
