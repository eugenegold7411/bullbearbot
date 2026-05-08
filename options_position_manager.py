"""
options_position_manager.py — A2 position intelligence layer.

Runs between Stage 0 and Stage 1 of the A2 cycle. For every active options
structure: fetches per-leg greeks via Alpaca's option snapshot endpoint,
appends a snapshot to the structure's greek-history file, classifies drift
state (DELTA_ITM, DELTA_OTM, THETA_ACCELERATION, VEGA_COLLAPSE, SHORT_LEG_ITM,
NORMAL, INSUFFICIENT_DATA), and routes a recommended action.

Recommendations are written to data/options/position_intel_latest.json and
read by Stage 1 (skip close-recommended symbols), Stage 3 (inject greek
context into the debate), and Stage 4 (execute close on immediate
recommendations, evaluate upgrades).

Design rules:
  - This module RECOMMENDS only — execution stays in close_check_loop.
  - act_on_immediate_close defaults to false. Operator flips on via SSH after
    confirming intel JSON content for 2-3 cycles.
  - Graceful degradation everywhere: missing greeks → INSUFFICIENT_DATA.
  - The structure-upgrade evaluator (formerly in stage4) lives here.

Entry points:
  run(state, alpaca_client, config) -> dict
  get_recommendations(symbol=None, action_filter=None) -> list[PositionAction]
  get_close_recommended_symbols() -> set[str]
  get_upgrade_recommendation(symbol, structure_id) -> dict | None
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from log_setup import get_logger

log = get_logger(__name__)

# ── Storage paths ─────────────────────────────────────────────────────────────

_DATA_DIR     = Path(__file__).parent / "data" / "options"
_HISTORY_DIR  = _DATA_DIR / "greek_history"
_INTEL_PATH   = _DATA_DIR / "position_intel_latest.json"
_DECISIONS_DIR = Path(__file__).parent / "data" / "account2" / "decisions"

_SCHEMA_VERSION = 1

# ── Default config (overlayed by strategy_config.json["position_intel"]) ─────

_DEFAULTS = {
    "enabled":                    True,
    "delta_itm_threshold":        0.80,
    "delta_otm_threshold":        0.15,
    "theta_acceleration_factor":  2.0,
    "vega_collapse_factor":       0.30,
    "short_leg_itm_threshold":    0.70,
    "min_history_points":         3,
    "history_max_snapshots":      30,
    "act_on_immediate_close":     False,
}


def _cfg(config: dict, key: str):
    """Resolve a position_intel config key with the documented default."""
    pi = (config or {}).get("position_intel", {}) or {}
    if key in pi:
        return pi[key]
    return _DEFAULTS[key]


# ── Dataclasses ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GreekSnapshot:
    """Per-structure net greeks at a single point in time."""
    delta:            Optional[float]
    theta:            Optional[float]
    vega:             Optional[float]
    gamma:            Optional[float]
    underlying_price: Optional[float]
    timestamp:        str  # ISO-8601 UTC

    def to_dict(self) -> dict:
        return asdict(self)


class DriftState(str, Enum):
    NORMAL              = "NORMAL"
    DELTA_ITM           = "DELTA_ITM"
    DELTA_OTM           = "DELTA_OTM"
    THETA_ACCELERATION  = "THETA_ACCELERATION"
    VEGA_COLLAPSE       = "VEGA_COLLAPSE"
    SHORT_LEG_ITM       = "SHORT_LEG_ITM"
    INSUFFICIENT_DATA   = "INSUFFICIENT_DATA"


@dataclass
class PositionAction:
    """Recommendation emitted per structure."""
    action:       str   # CLOSE | CLOSE_SHORT_LEG | HOLD | UPGRADE_TO_SPREAD
    reason:       str
    symbol:       str
    structure_id: str
    urgency:      str   # immediate | next_cycle | monitor
    details:      dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# Action constants used elsewhere in the module — single source of truth.
ACTION_CLOSE             = "CLOSE"
ACTION_CLOSE_SHORT_LEG   = "CLOSE_SHORT_LEG"
ACTION_HOLD              = "HOLD"
ACTION_UPGRADE_TO_SPREAD = "UPGRADE_TO_SPREAD"


# ── Greek fetch ──────────────────────────────────────────────────────────────

def _fetch_greeks(structure) -> Optional[GreekSnapshot]:
    """
    Fetch per-leg greeks via options_data.fetch_option_greeks() and net them.
    Net greek = sum over legs of (sign × leg_greek), sign=+1 buy, −1 sell.

    Returns None if any leg returns no greeks (graceful degradation — the
    structure is treated as INSUFFICIENT_DATA for this cycle).
    """
    try:
        import options_data  # noqa: PLC0415
    except Exception as exc:
        log.debug("[PI] options_data import failed: %s", exc)
        return None

    legs = getattr(structure, "legs", None) or []
    if not legs:
        return None

    net_delta = 0.0
    net_theta = 0.0
    net_vega  = 0.0
    net_gamma = 0.0
    underlying_price: Optional[float] = None
    have_any = False

    for leg in legs:
        occ = getattr(leg, "occ_symbol", None)
        side = getattr(leg, "side", "buy")
        if not occ:
            return None
        greeks = options_data.fetch_option_greeks(occ)
        if not greeks:
            return None
        sign = 1.0 if side == "buy" else -1.0
        d = greeks.get("delta")
        t = greeks.get("theta")
        v = greeks.get("vega")
        g = greeks.get("gamma")
        # All four must be present to count this leg.
        if d is None or t is None or v is None or g is None:
            return None
        net_delta += sign * float(d)
        net_theta += sign * float(t)
        net_vega  += sign * float(v)
        net_gamma += sign * float(g)
        have_any = True
        # Underlying price: snapshot doesn't carry it directly — leave None.

    if not have_any:
        return None

    return GreekSnapshot(
        delta=round(net_delta, 4),
        theta=round(net_theta, 4),
        vega=round(net_vega, 4),
        gamma=round(net_gamma, 4),
        underlying_price=underlying_price,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def _short_leg_abs_delta(structure) -> Optional[float]:
    """
    For multi-leg structures with a short leg, return |delta| of the short leg.
    Returns None for single-leg structures or if greeks are unavailable.
    """
    try:
        import options_data  # noqa: PLC0415
    except Exception:
        return None
    legs = getattr(structure, "legs", None) or []
    short_legs = [l for l in legs if getattr(l, "side", "") == "sell"]
    if not short_legs:
        return None
    occ = getattr(short_legs[0], "occ_symbol", None)
    if not occ:
        return None
    g = options_data.fetch_option_greeks(occ)
    if not g or g.get("delta") is None:
        return None
    return abs(float(g["delta"]))


# ── Greek history ────────────────────────────────────────────────────────────

def _history_path(symbol: str, structure_id: str) -> Path:
    safe_sym = (symbol or "UNKNOWN").upper().replace("/", "_")
    safe_id  = (structure_id or "no_id").replace("/", "_")
    return _HISTORY_DIR / f"{safe_sym}_{safe_id}.json"


def _load_greek_history(symbol: str, structure_id: str) -> list[dict]:
    """Read a structure's snapshot history. Returns [] if missing/corrupt."""
    path = _history_path(symbol, structure_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        return []
    except Exception as exc:
        log.warning("[PI] greek history corrupt at %s: %s", path, exc)
        return []


def _append_greek_history(
    symbol: str, structure_id: str, snap: GreekSnapshot, max_keep: int,
) -> int:
    """Append snapshot and cap at max_keep (oldest-first eviction). Returns new length."""
    _HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = _history_path(symbol, structure_id)
    history = _load_greek_history(symbol, structure_id)
    history.append(snap.to_dict())
    if len(history) > max_keep:
        history = history[-max_keep:]
    try:
        path.write_text(json.dumps(history, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning("[PI] failed to write greek history %s: %s", path, exc)
    return len(history)


# ── Entry-greek resolution ───────────────────────────────────────────────────

def _resolve_entry_greeks(structure, current: GreekSnapshot) -> dict:
    """
    Determine the entry greeks for drift comparison.

    Order of preference:
      1. structure.entry_greeks dict (set by a prior cycle).
      2. structure.delta/theta/vega scalars (legacy entry capture).
      3. current snapshot (first observation — best we can do for legacy positions).

    Returns a dict {delta, theta, vega, gamma} (any value may be None).
    """
    eg = getattr(structure, "entry_greeks", None)
    if isinstance(eg, dict) and eg.get("delta") is not None:
        return {
            "delta": eg.get("delta"),
            "theta": eg.get("theta"),
            "vega":  eg.get("vega"),
            "gamma": eg.get("gamma"),
        }
    legacy_d = getattr(structure, "delta", None)
    legacy_t = getattr(structure, "theta", None)
    legacy_v = getattr(structure, "vega", None)
    if legacy_d is not None or legacy_t is not None or legacy_v is not None:
        return {
            "delta": legacy_d,
            "theta": legacy_t,
            "vega":  legacy_v,
            "gamma": None,
        }
    # First observation — use current as the baseline.
    return {
        "delta": current.delta,
        "theta": current.theta,
        "vega":  current.vega,
        "gamma": current.gamma,
    }


# ── Drift detection ──────────────────────────────────────────────────────────

def _strategy_str(structure) -> str:
    s = getattr(structure, "strategy", "")
    return s.value if hasattr(s, "value") else str(s)


def _is_spread(strategy: str) -> bool:
    return strategy.endswith("_debit_spread") or strategy.endswith("_credit_spread")


def _is_single(strategy: str) -> bool:
    return strategy in ("single_call", "single_put")


def _has_short_leg(structure) -> bool:
    """
    True if any leg is `sell`. Used independently of strategy to catch
    mis-labeled spreads (e.g. orphan_tracked structures whose strategy field
    was set to single_call by the reconcile path even though the underlying
    Alpaca position is a vertical spread).
    """
    return any(getattr(leg, "side", "") == "sell"
               for leg in (getattr(structure, "legs", None) or []))


def _detect_drift(
    current: GreekSnapshot,
    entry: dict,
    history: list[dict],
    structure_type: str,
    short_leg_abs_delta: Optional[float],
    config: dict,
) -> tuple[DriftState, str]:
    """
    Apply detection thresholds. Returns (DriftState, human-readable reason).

    INSUFFICIENT_DATA when len(history) < min_history_points OR current.delta
    is None (greeks unfetchable).

    SHORT_LEG_ITM fires whenever short_leg_abs_delta is supplied and exceeds
    the threshold — gated on leg structure (presence of a short leg), NOT on
    structure_type, so mis-labeled orphans (vertical spreads recorded as
    single_call by reconcile) still trigger.
    """
    if current is None or current.delta is None:
        return DriftState.INSUFFICIENT_DATA, "current greeks unavailable"

    min_pts = int(_cfg(config, "min_history_points"))
    if len(history) < min_pts:
        return (
            DriftState.INSUFFICIENT_DATA,
            f"history={len(history)} < min_history_points={min_pts}",
        )

    delta_itm_t = float(_cfg(config, "delta_itm_threshold"))
    delta_otm_t = float(_cfg(config, "delta_otm_threshold"))
    theta_acc_f = float(_cfg(config, "theta_acceleration_factor"))
    vega_col_f  = float(_cfg(config, "vega_collapse_factor"))
    short_itm_t = float(_cfg(config, "short_leg_itm_threshold"))

    # Short-leg ITM check is highest priority. Gated on leg structure (caller
    # supplies short_leg_abs_delta only when a short leg exists), not on
    # structure_type.
    if short_leg_abs_delta is not None and short_leg_abs_delta >= short_itm_t:
        return (
            DriftState.SHORT_LEG_ITM,
            f"short leg |delta|={short_leg_abs_delta:.2f} >= {short_itm_t:.2f}",
        )

    abs_delta = abs(current.delta)

    # DELTA_ITM — long single deep ITM (gamma small, little upside).
    if _is_single(structure_type) and abs_delta >= delta_itm_t:
        return (
            DriftState.DELTA_ITM,
            f"|net delta|={abs_delta:.2f} >= {delta_itm_t:.2f}",
        )

    # DELTA_OTM — premium nearly zero.
    if _is_single(structure_type) and abs_delta <= delta_otm_t:
        return (
            DriftState.DELTA_OTM,
            f"|net delta|={abs_delta:.2f} <= {delta_otm_t:.2f}",
        )

    # THETA_ACCELERATION — daily theta cost spiking.
    entry_theta = entry.get("theta")
    if entry_theta is not None and current.theta is not None:
        et = abs(float(entry_theta))
        ct = abs(float(current.theta))
        if et > 0 and ct >= theta_acc_f * et:
            return (
                DriftState.THETA_ACCELERATION,
                f"|current theta|={ct:.3f} >= {theta_acc_f:.1f}× entry={et:.3f}",
            )

    # VEGA_COLLAPSE — vol play complete.
    entry_vega = entry.get("vega")
    if entry_vega is not None and current.vega is not None:
        ev = abs(float(entry_vega))
        cv = abs(float(current.vega))
        if ev > 0 and cv <= vega_col_f * ev:
            return (
                DriftState.VEGA_COLLAPSE,
                f"|current vega|={cv:.3f} <= {vega_col_f:.2f}× entry={ev:.3f}",
            )

    return DriftState.NORMAL, "all thresholds clear"


# ── Migrated upgrade helpers ─────────────────────────────────────────────────

def _compute_dte(structure) -> Optional[int]:
    """Calendar days to expiry. Migrated from bot_options_stage4_execution.py."""
    try:
        exp_str = getattr(structure, "expiration", "") or ""
        if not exp_str:
            for leg in (getattr(structure, "legs", None) or []):
                if getattr(leg, "expiration", None):
                    exp_str = leg.expiration
                    break
        if not exp_str:
            return None
        return (date.fromisoformat(exp_str) - date.today()).days
    except Exception:
        return None


def _load_latest_debate_selected_candidate() -> Optional[dict]:
    """Read selected_candidate from the most recent A2 decision file."""
    try:
        files = sorted(_DECISIONS_DIR.glob("a2_dec_*.json"))
        if not files:
            return None
        data = json.loads(files[-1].read_text(encoding="utf-8"))
        return data.get("selected_candidate")
    except Exception as exc:
        log.debug("[PI] latest debate load failed: %s", exc)
        return None


def _build_upgrade_short_leg(structure, is_call: bool) -> Optional[str]:
    """OCC symbol for the upgrade hedge leg. Migrated verbatim from stage4."""
    try:
        ref_strike = getattr(structure, "long_strike", None)
        if ref_strike is None:
            for leg in (getattr(structure, "legs", None) or []):
                if getattr(leg, "strike", None):
                    ref_strike = float(leg.strike)
                    break
        if ref_strike is None:
            return None
        multiplier   = 1.05 if is_call else 0.95
        short_strike = round(ref_strike * multiplier / 0.5) * 0.5
        exp_str      = getattr(structure, "expiration", "") or ""
        if not exp_str:
            for leg in (getattr(structure, "legs", None) or []):
                if getattr(leg, "expiration", None):
                    exp_str = leg.expiration
                    break
        if not exp_str:
            return None
        exp_part   = exp_str.replace("-", "")[2:]
        opt_type   = "C" if is_call else "P"
        strike_int = int(round(short_strike * 1000))
        return f"{structure.underlying}{exp_part}{opt_type}{strike_int:08d}"
    except Exception:
        return None


def _check_upgrade_conditions(
    structure, debate_result: Optional[dict], config: dict,
) -> Optional[dict]:
    """
    Pure evaluation of the 6 upgrade conditions. NO side effects.
    Returns the original-shape upgrade dict (for shim compat) or None.
    """
    strat = _strategy_str(structure)
    if strat not in ("single_call", "single_put"):
        return None
    is_call = (strat == "single_call")

    if debate_result is None:
        return None
    debate_stype     = debate_result.get("structure_type", "")
    spread_for_dir   = "debit_call_spread" if is_call else "debit_put_spread"
    if debate_stype != spread_for_dir:
        return None

    upnl = getattr(structure, "pnl_unrealized", None)
    if upnl is None or upnl <= 0:
        return None

    dte = _compute_dte(structure)
    if dte is None or dte <= 7:
        return None

    last_attempt = getattr(structure, "last_upgrade_attempted", None)
    if last_attempt is not None:
        try:
            days_since = (date.today() - date.fromisoformat(last_attempt[:10])).days
            if days_since < 7:
                return None
        except Exception:
            pass

    if not config.get("structure_upgrade_enabled", False):
        return None

    short_leg_occ = _build_upgrade_short_leg(structure, is_call)
    if short_leg_occ is None:
        return None

    new_type = "call_debit_spread" if is_call else "put_debit_spread"
    return {
        "action":       "add_hedge_leg",
        "symbol":       short_leg_occ,
        "qty":          getattr(structure, "contracts", 1),
        "structure_id": getattr(structure, "structure_id", ""),
        "old_strategy": strat,
        "new_strategy": new_type,
    }


def _check_upgrade(
    structure, debate_result: Optional[dict], config: dict,
) -> Optional[dict]:
    """
    Migration-equivalent of stage4._evaluate_structure_upgrade.

    Preserves the original side effect: stamps structure.last_upgrade_attempted
    when conditions 1-5 pass (even if the feature flag is off, so the cap
    blocks re-evaluation for 7 days). Returns the upgrade dict iff condition 6
    (feature flag) also passes; otherwise None.
    """
    et = datetime.now(timezone.utc)

    strat = _strategy_str(structure)
    if strat not in ("single_call", "single_put"):
        return None
    is_call = (strat == "single_call")

    if debate_result is None:
        return None
    debate_stype     = debate_result.get("structure_type", "")
    spread_for_dir   = "debit_call_spread" if is_call else "debit_put_spread"
    if debate_stype != spread_for_dir:
        return None

    upnl = getattr(structure, "pnl_unrealized", None)
    if upnl is None or upnl <= 0:
        return None

    dte = _compute_dte(structure)
    if dte is None or dte <= 7:
        return None

    last_attempt = getattr(structure, "last_upgrade_attempted", None)
    if last_attempt is not None:
        try:
            days_since = (date.today() - date.fromisoformat(last_attempt[:10])).days
            if days_since < 7:
                return None
        except Exception:
            pass

    new_type = "call_debit_spread" if is_call else "put_debit_spread"
    log.info(
        "[UPGRADE] %s upgrade candidate: %s → %s  upnl=%.2f  dte=%d",
        getattr(structure, "underlying", "?"), strat, new_type, float(upnl), dte,
    )
    structure.last_upgrade_attempted = et.isoformat()

    if not config.get("structure_upgrade_enabled", False):
        return None

    short_leg_occ = _build_upgrade_short_leg(structure, is_call)
    if short_leg_occ is None:
        log.warning(
            "[UPGRADE] %s: could not build short leg OCC — skipping",
            getattr(structure, "underlying", "?"),
        )
        return None

    return {
        "action":       "add_hedge_leg",
        "symbol":       short_leg_occ,
        "qty":          getattr(structure, "contracts", 1),
        "structure_id": getattr(structure, "structure_id", ""),
        "old_strategy": strat,
        "new_strategy": new_type,
    }


# ── Action router ────────────────────────────────────────────────────────────

def _route_action(
    drift: DriftState,
    structure,
    debate_result: Optional[dict],
    config: dict,
) -> PositionAction:
    """Map drift state to a PositionAction. Pure function — no side effects."""
    sym  = getattr(structure, "underlying", "")
    sid  = getattr(structure, "structure_id", "")
    strat = _strategy_str(structure)
    dte   = _compute_dte(structure)

    # SHORT_LEG_ITM — distinct action name retained in the intel JSON; Stage 4
    # will map to a full-structure CLOSE in v1 (no leg-surgery executor yet).
    if drift == DriftState.SHORT_LEG_ITM:
        return PositionAction(
            action=ACTION_CLOSE_SHORT_LEG,
            reason="short leg deep ITM — harvest remaining value",
            symbol=sym, structure_id=sid, urgency="immediate",
            details={"strategy": strat, "dte": dte},
        )

    if drift == DriftState.DELTA_ITM:
        # Single-leg deep ITM → close (rolls deferred to a later session).
        return PositionAction(
            action=ACTION_CLOSE,
            reason="long single deep ITM — gamma small, lock in",
            symbol=sym, structure_id=sid, urgency="next_cycle",
            details={"strategy": strat, "dte": dte},
        )

    if drift == DriftState.DELTA_OTM:
        urg = "immediate" if (dte is not None and dte <= 14) else "monitor"
        action = ACTION_CLOSE if urg == "immediate" else ACTION_HOLD
        return PositionAction(
            action=action,
            reason="long single nearly worthless — theta will finish it",
            symbol=sym, structure_id=sid, urgency=urg,
            details={"strategy": strat, "dte": dte},
        )

    if drift == DriftState.THETA_ACCELERATION:
        urg = "immediate" if (dte is not None and dte <= 14) else "monitor"
        action = ACTION_CLOSE if urg == "immediate" else ACTION_HOLD
        return PositionAction(
            action=action,
            reason="theta cliff — daily decay accelerating",
            symbol=sym, structure_id=sid, urgency=urg,
            details={"strategy": strat, "dte": dte},
        )

    if drift == DriftState.VEGA_COLLAPSE:
        return PositionAction(
            action=ACTION_CLOSE,
            reason="vol play complete — vega collapsed",
            symbol=sym, structure_id=sid, urgency="next_cycle",
            details={"strategy": strat, "dte": dte},
        )

    # NORMAL / INSUFFICIENT_DATA — consider upgrade for single-leg longs.
    if drift == DriftState.NORMAL and _is_single(strat):
        upgrade = _check_upgrade_conditions(structure, debate_result, config)
        if upgrade is not None:
            return PositionAction(
                action=ACTION_UPGRADE_TO_SPREAD,
                reason="profitable single + debate prefers spread + DTE > 7",
                symbol=sym, structure_id=sid, urgency="next_cycle",
                details=upgrade,
            )

    return PositionAction(
        action=ACTION_HOLD,
        reason=("normal" if drift == DriftState.NORMAL else "bootstrap window"),
        symbol=sym, structure_id=sid, urgency="monitor",
        details={"strategy": strat, "dte": dte, "drift_state": drift.value},
    )


# ── Main entry point ─────────────────────────────────────────────────────────

def _ensure_dirs() -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def _empty_intel(reason: str) -> dict:
    return {
        "generated_at":        datetime.now(timezone.utc).isoformat(),
        "cycle_id":            f"a2_pi_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
        "schema_version":      _SCHEMA_VERSION,
        "positions":           {},
        "close_recommended":   [],
        "upgrade_recommended": [],
        "monitoring":          [],
        "disabled_reason":     reason,
    }


def _write_intel(intel: dict) -> None:
    _ensure_dirs()
    try:
        _INTEL_PATH.write_text(json.dumps(intel, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning("[PI] failed to write %s: %s", _INTEL_PATH, exc)


def run(state: Optional[dict], alpaca_client, config: Optional[dict]) -> dict:
    """
    Main entry. Iterate active structures, fetch greeks, update history,
    classify drift, route actions. Write position_intel_latest.json. Return
    the intel dict for in-process consumers.

    state         — optional (kept for forward-compat; unused in v1).
    alpaca_client — optional; reserved for future close-detection guards.
    config        — strategy_config dict (may be {}; defaults are baked in).
    """
    config = config or {}

    if not _cfg(config, "enabled"):
        intel = _empty_intel("position_intel.enabled=false")
        _write_intel(intel)
        log.info("[PI] disabled by config — empty intel written")
        return intel

    try:
        import options_state  # noqa: PLC0415
    except Exception as exc:
        log.warning("[PI] options_state unavailable: %s", exc)
        intel = _empty_intel(f"options_state import failed: {exc}")
        _write_intel(intel)
        return intel

    open_structs = []
    try:
        open_structs = options_state.get_open_structures() or []
    except Exception as exc:
        log.warning("[PI] could not load open structures: %s", exc)

    debate_result = _load_latest_debate_selected_candidate()
    max_keep = int(_cfg(config, "history_max_snapshots"))

    intel: dict = {
        "generated_at":        datetime.now(timezone.utc).isoformat(),
        "cycle_id":            f"a2_pi_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
        "schema_version":      _SCHEMA_VERSION,
        "positions":           {},
        "close_recommended":   [],
        "upgrade_recommended": [],
        "monitoring":          [],
    }

    counts = {"close": 0, "upgrade": 0, "monitor": 0, "insufficient": 0}
    for struct in open_structs:
        try:
            sym = getattr(struct, "underlying", "")
            sid = getattr(struct, "structure_id", "")
            strat = _strategy_str(struct)

            current = _fetch_greeks(struct)
            entry   = _resolve_entry_greeks(struct, current) if current else {}
            history = _load_greek_history(sym, sid)
            # Fetch short-leg delta whenever the structure has a short leg —
            # leg-structure based, not strategy-based, so mis-labeled orphans
            # still get evaluated for SHORT_LEG_ITM.
            short_d = _short_leg_abs_delta(struct) if _has_short_leg(struct) else None

            new_len = history and len(history) or 0
            if current is not None:
                new_len = _append_greek_history(sym, sid, current, max_keep)

            drift, reason = _detect_drift(
                current, entry, history, strat, short_d, config,
            )
            paction = _route_action(drift, struct, debate_result, config)
            paction.reason = f"{reason} → {paction.reason}" if drift != DriftState.NORMAL else paction.reason

            key = f"{sym}_{sid}"
            intel["positions"][key] = {
                "symbol":               sym,
                "structure_id":         sid,
                "structure_type":       strat,
                "drift_state":          drift.value,
                "action":               paction.action,
                "urgency":              paction.urgency,
                "reason":               paction.reason,
                "greek_snapshot":       (current.to_dict() if current else None),
                "entry_greeks":         entry or None,
                "greek_history_length": new_len,
                "details":              paction.details,
            }

            if paction.action in (ACTION_CLOSE, ACTION_CLOSE_SHORT_LEG):
                intel["close_recommended"].append(sym)
                counts["close"] += 1
            elif paction.action == ACTION_UPGRADE_TO_SPREAD:
                intel["upgrade_recommended"].append(sym)
                counts["upgrade"] += 1
            else:
                intel["monitoring"].append(sym)
                counts["monitor"] += 1

            if drift == DriftState.INSUFFICIENT_DATA:
                counts["insufficient"] += 1

            if drift != DriftState.NORMAL and drift != DriftState.INSUFFICIENT_DATA:
                log.info(
                    "[PI] %s/%s: %s %s → %s (%s)",
                    sym, sid[:8], drift.value, reason, paction.action, paction.urgency,
                )
        except Exception as exc:
            log.warning("[PI] error analyzing structure %s: %s",
                        getattr(struct, "structure_id", "?"), exc)
            continue

    # De-dupe symbols (a single underlying with multiple structures appears once).
    intel["close_recommended"]   = sorted(set(intel["close_recommended"]))
    intel["upgrade_recommended"] = sorted(set(intel["upgrade_recommended"]))
    intel["monitoring"]          = sorted(set(intel["monitoring"]))

    _write_intel(intel)
    log.info(
        "[PI] %d structures analyzed: %d close, %d upgrade, %d monitor (%d INSUFFICIENT_DATA)",
        len(open_structs), counts["close"], counts["upgrade"],
        counts["monitor"], counts["insufficient"],
    )
    return intel


# ── Public read API (file-based, decoupled from run()) ───────────────────────

def _load_intel() -> dict:
    """Read position_intel_latest.json. Returns {} if absent or unreadable."""
    if not _INTEL_PATH.exists():
        return {}
    try:
        return json.loads(_INTEL_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        log.debug("[PI] intel read failed: %s", exc)
        return {}


def get_recommendations(
    symbol: Optional[str] = None,
    action_filter: Optional[str] = None,
) -> list[PositionAction]:
    """Read intel file. Optionally filter by symbol and/or action."""
    intel = _load_intel()
    out: list[PositionAction] = []
    for _key, p in (intel.get("positions") or {}).items():
        if symbol is not None and p.get("symbol") != symbol:
            continue
        if action_filter is not None and p.get("action") != action_filter:
            continue
        out.append(PositionAction(
            action=p.get("action", ACTION_HOLD),
            reason=p.get("reason", ""),
            symbol=p.get("symbol", ""),
            structure_id=p.get("structure_id", ""),
            urgency=p.get("urgency", "monitor"),
            details=p.get("details") or {},
        ))
    return out


def get_close_recommended_symbols() -> set[str]:
    """Symbols flagged for close in the latest intel. Empty set if absent."""
    intel = _load_intel()
    return set(intel.get("close_recommended") or [])


def get_upgrade_recommendation(symbol: str, structure_id: str) -> Optional[dict]:
    """
    Stage-4 shim convenience: return the original-shape upgrade dict
    {action, symbol, qty, structure_id, old_strategy, new_strategy}, or None.
    """
    recs = get_recommendations(symbol=symbol, action_filter=ACTION_UPGRADE_TO_SPREAD)
    for r in recs:
        if r.structure_id == structure_id:
            d = dict(r.details or {})
            # Detail dict already carries the original shape from _check_upgrade_conditions.
            if d.get("action") == "add_hedge_leg":
                return d
    return None


def is_immediate_close_action_enabled(config: Optional[dict] = None) -> bool:
    """Stage-4 short-circuit gate. Defaults to false."""
    return bool(_cfg(config or {}, "act_on_immediate_close"))


__all__ = [
    "GreekSnapshot",
    "DriftState",
    "PositionAction",
    "ACTION_CLOSE",
    "ACTION_CLOSE_SHORT_LEG",
    "ACTION_HOLD",
    "ACTION_UPGRADE_TO_SPREAD",
    "run",
    "get_recommendations",
    "get_close_recommended_symbols",
    "get_upgrade_recommendation",
    "is_immediate_close_action_enabled",
    # Migrated helpers (re-exported for stage4 shim and tests).
    "_compute_dte",
    "_load_latest_debate_selected_candidate",
    "_build_upgrade_short_leg",
    "_check_upgrade",
    "_check_upgrade_conditions",
]
