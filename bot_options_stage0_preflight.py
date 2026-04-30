"""
bot_options_stage0_preflight.py — A2 Stage 0: preflight, eligibility, reconciliation.

Public API:
  run_a2_preflight(session_tier, alpaca_client) -> A2PreflightResult
  _get_obs_mode_state() -> dict
  _update_obs_mode_state(state) -> bool
  is_observation_mode() -> bool
  _check_and_update_iv_ready(state) -> dict

Responsibilities:
  - Session gate (market/pre_market only)
  - Account eligibility check (equity floor)
  - Preflight verdict
  - A2 operating mode check
  - Options structure reconciliation
  - Observation mode tracking
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _get_et_now():
    """Return current datetime in US/Eastern. Extracted for testability."""
    from zoneinfo import ZoneInfo  # noqa: PLC0415
    return datetime.now(ZoneInfo("America/New_York"))
from pathlib import Path
from typing import Any

from log_setup import get_logger

log = get_logger(__name__)

_EQUITY_FLOOR = 25_000.0

_A2_DIR = Path(__file__).parent / "data" / "account2"

# Observation mode: first 20 trading days while IV history builds
_OBS_MODE_DAYS      = 20
_OBS_MODE_FILE      = _A2_DIR / "obs_mode_state.json"
_OBS_SCHEMA_VERSION = 2
# Full A2 optionable universe — used for IV readiness checks.
# Crypto (BTC/USD, ETH/USD) excluded — no options available.
# Symbols without IV history are bootstrapped automatically by the 4 AM job.
_OBS_IV_SYMBOLS = [
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
    # Legacy bootstrap symbols (original A2 Phase 1, not in watchlist_core)
    "AAPL", "META", "GOOGL", "AMD",
]


# ── Observation mode tracking ─────────────────────────────────────────────────

def _get_obs_mode_state() -> dict:
    """Load or initialize observation mode tracking state."""
    if _OBS_MODE_FILE.exists():
        try:
            return json.loads(_OBS_MODE_FILE.read_text())
        except Exception:
            pass
    return {
        "version": _OBS_SCHEMA_VERSION,
        "trading_days_observed": 0,
        "first_seen_date": None,
        "observation_complete": False,
        "iv_history_ready": False,
        "iv_ready_symbols": {},
    }


def _is_trading_day(iso_date: str) -> bool:
    """
    Return True if iso_date (YYYY-MM-DD) is a NYSE trading day.
    Excludes weekends. Excludes a fixed set of US market holidays.
    """
    from datetime import date  # noqa: PLC0415
    d = date.fromisoformat(iso_date)
    if d.weekday() >= 5:
        return False
    _fixed = {(1, 1), (7, 4), (12, 25)}
    if (d.month, d.day) in _fixed:
        return False
    import calendar as _cal  # noqa: PLC0415
    def _nth_weekday(year, month, weekday, n):
        """n-th occurrence (1-based) of weekday in month."""
        first = date(year, month, 1)
        delta = (weekday - first.weekday()) % 7
        return date(year, month, 1 + delta + (n - 1) * 7)
    def _last_monday(year, month):
        last = date(year, month, _cal.monthrange(year, month)[1])
        return last - __import__("datetime").timedelta(days=(last.weekday()) % 7)
    floating = {
        _nth_weekday(d.year, 1, 0, 3),
        _nth_weekday(d.year, 2, 0, 3),
        _last_monday(d.year, 5),
        _nth_weekday(d.year, 9, 0, 1),
        _nth_weekday(d.year, 11, 3, 4),
    }
    return d not in floating


def _update_obs_mode_state(state: dict) -> bool:
    """
    Update observation mode counter. Increment trading_days_observed only on
    NYSE trading days (no weekends, no US market holidays).
    Returns True if still in observation mode.
    """
    from datetime import date  # noqa: PLC0415
    today = date.today().isoformat()

    if state.get("observation_complete"):
        if state.get("version", 1) < _OBS_SCHEMA_VERSION:
            state = _check_and_update_iv_ready(state)
            state["version"] = _OBS_SCHEMA_VERSION
            try:
                _OBS_MODE_FILE.write_text(json.dumps(state, indent=2))
                log.info("[OPTS] obs_mode_state.json migrated to v%d", _OBS_SCHEMA_VERSION)
            except Exception:
                pass
        return False

    if state.get("first_seen_date") is None:
        state["first_seen_date"] = today

    if state.get("last_counted_date") != today and _is_trading_day(today):
        state["trading_days_observed"] = state.get("trading_days_observed", 0) + 1
        state["last_counted_date"] = today
    elif not _is_trading_day(today):
        log.debug("[OPTS] Observation mode: %s is not a trading day — not counting", today)

    days = state["trading_days_observed"]
    log.info("[OPTS] Observation mode: %d/%d trading days", days, _OBS_MODE_DAYS)

    if days >= _OBS_MODE_DAYS:
        state["observation_complete"] = True
        state["version"] = _OBS_SCHEMA_VERSION
        state = _check_and_update_iv_ready(state)
        log.info("[OPTS] Observation mode COMPLETE — Account 2 now live trading")

    try:
        _OBS_MODE_FILE.write_text(json.dumps(state, indent=2))
    except Exception:
        pass

    return not state.get("observation_complete", False)


def is_observation_mode() -> bool:
    """Quick check: is Account 2 still in observation mode?"""
    state = _get_obs_mode_state()
    return not state.get("observation_complete", False)


def _any_leg_has_fill(structure, alpaca_client) -> bool:
    """Return True if any Alpaca order for this structure has filled_qty > 0."""
    for order_id in (structure.order_ids or []):
        try:
            order = alpaca_client.get_order_by_id(order_id)
            if float(getattr(order, "filled_qty", 0) or 0) > 0:
                return True
        except Exception:
            pass
    return False


def _cancel_and_clear_unfilled_orders(
    alpaca_client,
    config: dict,
) -> int:
    """
    For every SUBMITTED structure with zero filled qty:
      1. Cancel all Alpaca orders for that structure
      2. Set lifecycle to CANCELLED so the symbol re-enters the candidate pool

    This ensures A2 always re-prices on fresh mid values each cycle rather
    than leaving stale limit orders open indefinitely (GTC single legs) or
    waiting for DAY spread orders to expire silently at close.

    Gated by account2.auto_cancel_unfilled_orders (default True).
    Returns count of structures cancelled. Non-fatal per structure.
    """
    if not config.get("account2", {}).get("auto_cancel_unfilled_orders", True):
        return 0

    import options_state as _os  # noqa: PLC0415
    from schemas import StructureLifecycle  # noqa: PLC0415

    all_structs = _os.load_structures()
    cancelled = 0

    for s in all_structs:
        try:
            if s.lifecycle != StructureLifecycle.SUBMITTED:
                continue
            if not s.order_ids:
                continue
            if _any_leg_has_fill(s, alpaca_client):
                continue  # partial or full fill — do not cancel

            for order_id in s.order_ids:
                try:
                    alpaca_client.cancel_order_by_id(order_id)
                    log.info(
                        "[PREFLIGHT] Cancelled unfilled order %s for %s (%s)",
                        order_id[:8], s.underlying, s.strategy.value,
                    )
                except Exception as _ce:
                    log.debug(
                        "[PREFLIGHT] Cancel order %s failed (non-fatal): %s",
                        order_id[:8], _ce,
                    )

            s.lifecycle = StructureLifecycle.CANCELLED
            s.add_audit(
                "auto-cancelled: unfilled order — resubmitting with fresh pricing next cycle"
            )
            _os.save_structure(s)
            cancelled += 1
        except Exception as _e:
            log.debug("[PREFLIGHT] _cancel_and_clear_unfilled_orders skip (non-fatal): %s", _e)

    if cancelled:
        log.info(
            "[PREFLIGHT] Cancelled %d unfilled order(s) — symbols re-enter candidate pool",
            cancelled,
        )
    return cancelled


def _is_duplicate_submission(symbol: str, structures: list) -> bool:
    """
    Return True if a new structure for this symbol should be blocked.

    Blocks only when a SUBMITTED structure exists (in-flight, not yet cancelled).
    CANCELLED and FULLY_FILLED structures never block re-entry.
    After _cancel_and_clear_unfilled_orders() runs, any remaining SUBMITTED
    structure has at least a partial fill — blocking is correct.
    """
    from schemas import StructureLifecycle  # noqa: PLC0415
    return any(
        s.underlying == symbol and s.lifecycle == StructureLifecycle.SUBMITTED
        for s in structures
    )


def _cleanup_stale_proposed_structures(
    structures: list,
    max_age_hours: float = 2.0,
) -> int:
    """
    Cancel PROPOSED structures older than max_age_hours with empty order_ids.

    These are proposals that were never submitted — either pre-fix artifacts
    from a cycle that failed before reaching the executor, or proposals that
    expired without being acted upon.

    Rules:
      - Only touches PROPOSED lifecycle structures
      - Only touches structures with empty order_ids (non-empty = may be in-flight)
      - Only touches structures older than max_age_hours
      - Non-fatal per structure — one bad entry never blocks the rest

    Returns count of structures cancelled.
    """
    import options_state as _os  # noqa: PLC0415
    from schemas import StructureLifecycle  # noqa: PLC0415

    cancelled = 0
    now = datetime.now(timezone.utc)

    for s in structures:
        try:
            if s.lifecycle != StructureLifecycle.PROPOSED:
                continue
            if s.order_ids:
                continue  # has order_ids — may be in-flight, do not touch

            try:
                opened = datetime.fromisoformat(s.opened_at.replace("Z", "+00:00"))
                if opened.tzinfo is None:
                    opened = opened.replace(tzinfo=timezone.utc)
                age_hours = (now - opened).total_seconds() / 3600
            except Exception:
                age_hours = max_age_hours + 1.0  # unparseable timestamp → treat as stale

            if age_hours <= max_age_hours:
                continue

            s.lifecycle = StructureLifecycle.CANCELLED
            s.add_audit(
                f"auto-cancelled: stale proposed, age={age_hours:.1f}h > {max_age_hours}h, "
                "no order_ids — never submitted"
            )
            _os.save_structure(s)
            log.info(
                "[PREFLIGHT] Cancelled stale PROPOSED %s (%s) age=%.1fh",
                s.structure_id[:8], s.underlying, age_hours,
            )
            cancelled += 1
        except Exception as _e:
            log.debug("[PREFLIGHT] _cleanup_stale_proposed skip (non-fatal): %s", _e)

    return cancelled


def _check_and_update_iv_ready(state: dict) -> dict:
    """
    Check IV history readiness for all core A2 symbols via options_data.
    Writes iv_history_ready + iv_ready_symbols into state dict (in-place).
    Never modifies observation_complete. Non-fatal.
    Returns the mutated state dict.

    NOTE: iv_ready_symbols in obs_mode_state.json is a legacy field.
    It was authoritative during observation mode (first 20 trading days) when
    individual symbols could be not-yet-seeded. Once observation_complete=true,
    this dict is no longer checked — all 43 symbols are assumed IV-ready and
    the bot proceeds unconditionally. The on-disk snapshot may show only the
    original 16 symbols (pre-S4-A expansion); this is stale but harmless.
    """
    try:
        import options_data  # noqa: PLC0415
        result = options_data.check_iv_history_ready(_OBS_IV_SYMBOLS)
        state["iv_history_ready"] = result["all_ready"]
        state["iv_ready_symbols"] = result["symbol_ready"]
        log.info("[OPTS] IV history check: %d/%d symbols ready",
                 result["ready_count"], result["total_count"])
    except Exception as exc:  # noqa: BLE001
        log.warning("[OPTS] _check_and_update_iv_ready failed (non-fatal): %s", exc)
        state.setdefault("iv_history_ready", False)
        state.setdefault("iv_ready_symbols", {})
    return state


@dataclass
class A2PreflightResult:
    """Result from run_a2_preflight. halt=True means the cycle must abort."""
    halt: bool = False
    halt_reason: str = ""
    equity: float = 0.0
    cash: float = 0.0
    buying_power: float = 0.0
    pf_allow_live_orders: bool = True
    pf_allow_new_entries: bool = True
    a2_mode: Any = None
    pending_underlyings: frozenset = field(default_factory=frozenset)


def _build_a2_broker_snapshot(alpaca_client):
    """
    Build a BrokerSnapshot from Account 2's current live state.

    Fetches positions and open orders from the A2 Alpaca account.
    Returns a BrokerSnapshot with normalised positions and orders.
    Non-fatal — returns an empty snapshot on any error so reconciliation
    can degrade gracefully rather than blocking the cycle.
    """
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    from schemas import BrokerSnapshot, NormalizedOrder, NormalizedPosition

    norm_positions: list = []
    norm_orders: list = []
    equity = buying_power = cash = 0.0

    try:
        account = alpaca_client.get_account()
        equity       = float(account.equity)
        cash         = float(account.cash)
        buying_power = float(account.buying_power)
    except Exception as exc:
        log.warning("[OPTS_RECON] snapshot: failed to fetch account: %s", exc)

    try:
        positions = alpaca_client.get_all_positions()
        norm_positions = [NormalizedPosition.from_alpaca_position(p) for p in positions]
    except Exception as exc:
        log.warning("[OPTS_RECON] snapshot: failed to fetch positions: %s", exc)

    try:
        orders = alpaca_client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN)
        )
        norm_orders = [NormalizedOrder.from_alpaca_order(o) for o in orders]
    except Exception as exc:
        log.warning("[OPTS_RECON] snapshot: failed to fetch orders: %s", exc)

    return BrokerSnapshot(
        positions=norm_positions,
        open_orders=norm_orders,
        equity=equity,
        cash=cash,
        buying_power=buying_power,
    )


def run_a2_preflight(
    session_tier: str,
    alpaca_client,
) -> A2PreflightResult:
    """
    Run A2 preflight checks. Returns A2PreflightResult with halt=True if cycle
    should abort. Handles: session gate, equity floor, preflight verdict,
    A2 operating mode, and options structure reconciliation.

    Note: observation mode tracking (_update_obs_mode_state) is handled by
    the orchestrator in bot_options.py — those helpers are tested with
    mock.patch("bot_options.*") so they must stay there.
    """
    result = A2PreflightResult()

    # Session gate — options only trade during market hours
    if session_tier not in ("market", "pre_market"):
        log.info("[OPTS] Session=%s — options cycle skipped (market hours only)", session_tier)
        return A2PreflightResult(halt=True, halt_reason="session_not_market")

    # Near-close gate: no new options structures after 15:50 ET.
    # Options need time to fill; returning halt=True skips new proposals.
    # Non-fatal: proceeds normally if timezone check raises.
    try:
        _et = _get_et_now()
        if _et.hour == 15 and _et.minute >= 50:
            log.info("[PREFLIGHT] near_close_gate: blocking new structures after 15:50 ET")
            result.halt = True
            result.halt_reason = "near_close_gate: no new options structures after 15:50 ET"
            return result
    except Exception:
        pass  # non-fatal — proceed if timezone check fails

    # Account equity check
    try:
        account = alpaca_client.get_account()
        result.equity        = float(account.equity)
        result.cash          = float(account.cash)
        result.buying_power  = float(account.buying_power)
        log.info("[OPTS] Account 2: equity=$%s  cash=$%s  buying_power=$%s",
                 f"{result.equity:,.0f}", f"{result.cash:,.0f}",
                 f"{result.buying_power:,.0f}")
    except Exception as exc:
        log.error("[OPTS] Cannot fetch Account 2 status: %s — skipping cycle", exc)
        return A2PreflightResult(halt=True, halt_reason="account_fetch_failed")

    if result.equity < _EQUITY_FLOOR:
        log.warning("[OPTS] Account 2 equity $%.0f below floor $%.0f — halting",
                    result.equity, _EQUITY_FLOOR)
        return A2PreflightResult(halt=True, halt_reason="equity_below_floor",
                                 equity=result.equity, cash=result.cash)

    # Preflight gate
    try:
        import preflight as _preflight  # noqa: PLC0415
        _pf_result = _preflight.run_preflight(
            caller="run_options_cycle",
            session_tier=session_tier,
            equity=result.equity,
            account_id="a2",
        )
        if _pf_result.verdict == "halt":
            log.error("[PREFLIGHT] verdict=halt — aborting options cycle  blockers=%s",
                      _pf_result.blockers)
            return A2PreflightResult(halt=True, halt_reason="preflight_halt",
                                     equity=result.equity, cash=result.cash)
        elif _pf_result.verdict == "reconcile_only":
            log.warning("[PREFLIGHT] verdict=reconcile_only — new A2 entries blocked  blockers=%s",
                        _pf_result.blockers)
            result.pf_allow_new_entries = False
        elif _pf_result.verdict == "shadow_only":
            log.warning("[PREFLIGHT] verdict=shadow_only — A2 live orders suppressed")
            result.pf_allow_live_orders = False
        elif _pf_result.verdict == "go_degraded":
            log.warning("[PREFLIGHT] verdict=go_degraded  warnings=%s", _pf_result.warnings)
    except Exception as _pf_exc:
        log.error("[PREFLIGHT] unexpected exception (proceeding with caution): %s", _pf_exc)

    # A2 operating mode (non-fatal)
    try:
        from divergence import OperatingMode, load_account_mode  # noqa: PLC0415
        result.a2_mode = load_account_mode("A2")
        if result.a2_mode.mode != OperatingMode.NORMAL:
            log.warning("[DIV] A2 mode=%s scope=%s/%s",
                        result.a2_mode.mode.value,
                        result.a2_mode.scope.value,
                        result.a2_mode.scope_id)
    except Exception as _div_exc:
        log.warning("[DIV] A2 mode load failed (non-fatal): %s", _div_exc)

    # Load config once for all preflight cleanup steps
    _cfg_path = Path(__file__).parent / "strategy_config.json"
    _s_cfg: dict = {}
    try:
        _s_cfg = json.loads(_cfg_path.read_text()) if _cfg_path.exists() else {}
    except Exception:
        pass

    # Stale PROPOSED structure cleanup (before reconciliation)
    try:
        import options_state as _oss  # noqa: PLC0415
        _max_age = float(_s_cfg.get("account2", {}).get("stale_cleanup_max_age_hours", 2.0))
        _all_structs = _oss.load_structures()
        _n_cleaned = _cleanup_stale_proposed_structures(_all_structs, _max_age)
        if _n_cleaned:
            log.info("[PREFLIGHT] Cancelled %d stale PROPOSED structure(s)", _n_cleaned)
    except Exception as _cleanup_err:
        log.debug("[PREFLIGHT] stale PROPOSED cleanup failed (non-fatal): %s", _cleanup_err)

    # Cancel unfilled SUBMITTED orders and reset lifecycle so symbols re-enter pool.
    # Runs before reconciliation so the pending_underlyings guard sees fresh state.
    try:
        _n_cancelled = _cancel_and_clear_unfilled_orders(alpaca_client, _s_cfg)
    except Exception as _cancel_err:
        log.debug("[PREFLIGHT] _cancel_and_clear_unfilled_orders failed (non-fatal): %s", _cancel_err)

    # Options structure reconciliation (before new proposals)
    try:
        import options_state  # noqa: PLC0415
        from reconciliation import (  # noqa: PLC0415
            execute_reconciliation_plan,
            plan_structure_repair,
            reconcile_options_structures,
        )
        _open_structs = options_state.get_open_structures()
        if _open_structs:
            _recon_snapshot = _build_a2_broker_snapshot(alpaca_client)
            _struct_diff = reconcile_options_structures(
                structures=_open_structs,
                snapshot=_recon_snapshot,
                current_time=datetime.now(timezone.utc).isoformat(),
                config={},
            )
            if any([
                _struct_diff.broken,
                _struct_diff.expiring_soon,
                _struct_diff.needs_close,
                _struct_diff.orphaned_legs,
            ]):
                _repair_plan = plan_structure_repair(
                    diff=_struct_diff,
                    structures=_open_structs,
                    snapshot=_recon_snapshot,
                    config={},
                )
                log.info(
                    "[OPTS_RECON] %d broken, %d expiring, "
                    "%d needs_close, %d orphaned — %d repair action(s)",
                    len(_struct_diff.broken),
                    len(_struct_diff.expiring_soon),
                    len(_struct_diff.needs_close),
                    len(_struct_diff.orphaned_legs),
                    len(_repair_plan),
                )
                execute_reconciliation_plan(
                    plan=_repair_plan,
                    trading_client=alpaca_client,
                    account_id="account2",
                    dry_run=False,
                )
            else:
                log.debug("[OPTS_RECON] %d open structures — all intact", len(_open_structs))
        else:
            log.debug("[OPTS_RECON] No open structures — skipping reconciliation")

        # Pending in-flight guard: after _cancel_and_clear_unfilled_orders() has run,
        # any remaining SUBMITTED structure has at least a partial fill from a prior
        # cycle. Block re-submission for those underlyings to avoid double-positioning.
        # Uses load_structures() (all lifecycle states) — get_open_structures() returns
        # only FULLY_FILLED/PARTIALLY_FILLED and would never see SUBMITTED.
        try:
            from schemas import StructureLifecycle  # noqa: PLC0415
            _all_for_guard = options_state.load_structures()
            _submitted = [
                s for s in _all_for_guard
                if s.lifecycle == StructureLifecycle.SUBMITTED
            ]
            if _submitted:
                result.pending_underlyings = frozenset(
                    s.underlying for s in _submitted
                )
                log.info("[OPTS] Pending in-flight orders — skip new candidates for: %s",
                         ", ".join(sorted(result.pending_underlyings)))
        except Exception as _pe:
            log.debug("[OPTS] pending_underlyings check failed (non-fatal): %s", _pe)

    except Exception as _recon_err:
        log.warning("[OPTS_RECON] Failed (non-fatal): %s", _recon_err)

    # Structure count gate — runs after reconciliation so expired/closed structures
    # are already removed from the open-structures list before counting.
    # Suppresses new entries (not the full cycle) so close-check loop still runs.
    try:
        import options_state as _oss_gate  # noqa: PLC0415
        _cfg_path_gate = Path(__file__).parent / "strategy_config.json"
        _cfg_gate = json.loads(_cfg_path_gate.read_text()) if _cfg_path_gate.exists() else {}
        _max_pos = int(_cfg_gate.get("account2", {}).get("max_open_positions", 8))
        _open_count = len(_oss_gate.get_open_structures())
        if _open_count >= _max_pos:
            log.info(
                "[PREFLIGHT] max_open_positions reached (%d/%d) — new entries suppressed",
                _open_count, _max_pos,
            )
            result.pf_allow_new_entries = False
    except Exception as _cnt_err:
        log.debug("[PREFLIGHT] structure count gate failed (non-fatal): %s", _cnt_err)

    return result
