"""
reconciliation.py — Desired-state diff engine for Account 1.

Replaces the ad-hoc forced_exits / deadline_exits / backstop-seeding blocks
in bot.py with a structured desired→actual comparison.

Public API
──────────
build_desired_state(positions, config, now_utc) -> DesiredState
  Construct what we *want* the portfolio to look like based on current config
  and open positions.

diff_state(desired, snapshot) -> ReconciliationDiff
  Compare desired state against live broker state. Returns a prioritised list
  of reconciliation actions.

plan_reconciliation(diff) -> list[ReconciliationAction]
  Priority-ordered list of actions:
    CRITICAL  — deadline-expired mandatory exits (e.g. TSM before earnings)
    HIGH      — forced exits for CRITICAL-health positions
    NORMAL    — stale/orphaned/unprotected positions (stops missing or wrong)

execute_reconciliation_plan(plan, alpaca_client) -> list[str]
  Execute each action against the live Alpaca API. Returns log of results.

run_account1_reconciliation(positions, snapshot, config, alpaca_client, regime)
  -> list[str]
  One-shot helper called from bot.py run_cycle(). Runs build→diff→plan→execute.
  Skips execution if regime == "halt".

Options helpers (Account 2)
──────────────────────────
StructureDiff           — diff between a desired OptionsStructure and live state
reconcile_options_structures(desired, actual_orders) -> list[StructureDiff]
  Identify structures that should exist but don't (missing), or exist but
  shouldn't (orphaned), or are partially filled (broken).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from alpaca.trading.requests import GetOrdersRequest

from exit_manager import _has_stop_order
from schemas import (
    BrokerSnapshot,
    NormalizedPosition,
    OptionsStructure,
    normalize_symbol,
)

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Data types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DesiredPosition:
    """
    What we intend to hold for one symbol.

    symbol          — canonical internal format (BTC/USD for crypto)
    must_exit_by    — ISO-8601 UTC timestamp; None means no deadline
    must_exit_reason — human-readable reason for exit deadline
    max_size_pct    — max allowed position size as fraction of equity
    forced_exit     — True → portfolio intelligence flagged CRITICAL health
    """
    symbol:           str
    must_exit_by:     Optional[str] = None
    must_exit_reason: Optional[str] = None
    max_size_pct:     float = 0.20
    forced_exit:      bool = False


@dataclass
class DesiredState:
    """
    Complete desired portfolio state.

    positions       — desired positions keyed by canonical symbol
    seeded_from     — ISO-8601 UTC timestamp when this was built
    """
    positions:   dict[str, DesiredPosition]
    seeded_from: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# Priority levels for reconciliation actions
PRIORITY_CRITICAL = "CRITICAL"
PRIORITY_HIGH     = "HIGH"
PRIORITY_NORMAL   = "NORMAL"


@dataclass
class ReconciliationAction:
    """
    A single reconciliation action to execute.

    priority    — CRITICAL / HIGH / NORMAL
    action_type — "close_all" | "close_half" | "refresh_stop" | "cancel_duplicate"
    symbol      — canonical symbol to act on
    reason      — human-readable explanation
    qty         — number of shares/units to act on (0 = close all)
    """
    priority:    str
    action_type: str
    symbol:      str
    reason:      str
    qty:         float = 0.0


@dataclass
class ReconciliationDiff:
    """
    Result of comparing desired state against live broker state.

    actions         — ordered list of ReconciliationActions (CRITICAL first)
    expired_symbols — symbols that hit their exit deadline
    forced_symbols  — symbols flagged for forced exit by portfolio intelligence
    missing_stops   — symbols with an open position but no stop order
    """
    actions:          list[ReconciliationAction]
    expired_symbols:  list[str]
    forced_symbols:   list[str]
    missing_stops:    list[str]


# ─────────────────────────────────────────────────────────────────────────────
# Options diff types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StructureDiff:
    """
    Diff result for a single options structure (legacy per-structure format).

    structure_id — internal ID
    status       — "missing" | "orphaned" | "broken" | "ok"
    description  — human-readable detail
    """
    structure_id: str
    status:       str
    description:  str


@dataclass
class OptionsReconResult:
    """
    Composite reconciliation result for Account 2 options structures.
    Returned by reconcile_options_structures().

    intact        — structure_ids where all leg OCC symbols are in broker positions
    broken        — structure_ids where only some leg OCC symbols are present
    expiring_soon — structure_ids with DTE ≤ 2
    needs_close   — structure_ids where should_close_structure() returns True
    orphaned_legs — OCC symbols in broker positions with no matching structure leg
    close_reasons — structure_id → close reason string (for plan_structure_repair)
    orphaned_qtys — OCC symbol → qty (for plan_structure_repair)
    """
    intact:        list[str]       = field(default_factory=list)
    broken:        list[str]       = field(default_factory=list)
    expiring_soon: list[str]       = field(default_factory=list)
    needs_close:   list[str]       = field(default_factory=list)
    orphaned_legs: list[str]       = field(default_factory=list)
    close_reasons: dict[str, str]  = field(default_factory=dict)
    orphaned_qtys: dict[str, int]  = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Build desired state
# ─────────────────────────────────────────────────────────────────────────────

def build_desired_state(
    positions:  list[NormalizedPosition],
    config:     dict,
    now_utc:    Optional[datetime] = None,
) -> DesiredState:
    """
    Construct desired portfolio state from config + open positions.

    Reads `time_bound_actions` from config to populate exit deadlines.
    Reads `forced_exits` and `deadline_exits` from portfolio_intelligence
    output if present in config.

    Args:
        positions  — current open positions (from broker)
        config     — parsed strategy_config.json
        now_utc    — current time (defaults to utcnow); injectable for testing
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    desired: dict[str, DesiredPosition] = {}

    # ── Seed from open positions ──────────────────────────────────────────────
    for pos in positions:
        sym = normalize_symbol(pos.symbol)
        desired[sym] = DesiredPosition(symbol=sym)

    # ── Apply time_bound_actions ──────────────────────────────────────────────
    for tba in config.get("time_bound_actions", []):
        raw_sym  = tba.get("symbol", "")
        sym      = normalize_symbol(raw_sym)
        deadline = tba.get("exit_by") or tba.get("deadline")
        reason   = tba.get("reason", "time_bound_exit")

        if not sym or not deadline:
            log.debug("[RECON] Skipping malformed time_bound_action: %s", tba)
            continue

        if sym not in desired:
            # Position may not be open yet — pre-register anyway
            desired[sym] = DesiredPosition(symbol=sym)

        desired[sym].must_exit_by     = deadline
        desired[sym].must_exit_reason = reason

    # ── Apply portfolio_intelligence forced exits ─────────────────────────────
    # pi_data may be embedded in config under a "pi_data" key when called
    # from run_account1_reconciliation; otherwise no forced exit info.
    pi_data = config.get("_pi_data", {})
    for fe in pi_data.get("forced_exits", []):
        sym = normalize_symbol(fe.get("symbol", ""))
        if sym:
            if sym not in desired:
                desired[sym] = DesiredPosition(symbol=sym)
            desired[sym].forced_exit = True
            desired[sym].must_exit_reason = (
                fe.get("reason", "portfolio_health_critical")
            )

    return DesiredState(positions=desired)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Diff desired vs actual
# ─────────────────────────────────────────────────────────────────────────────

def diff_state(
    desired:  DesiredState,
    snapshot: BrokerSnapshot,
    now_utc:  Optional[datetime] = None,
) -> ReconciliationDiff:
    """
    Compare desired state to live broker snapshot.

    Returns a ReconciliationDiff with prioritised actions:
      CRITICAL — expired deadlines (must close before event)
      HIGH     — forced exits (portfolio health critical)
      NORMAL   — missing stops, stale stops, orphaned positions

    Args:
        desired   — from build_desired_state()
        snapshot  — live BrokerSnapshot
        now_utc   — injectable for testing
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    actions:         list[ReconciliationAction] = []
    expired_symbols: list[str] = []
    forced_symbols:  list[str] = []
    missing_stops:   list[str] = []

    pos_by_sym   = snapshot.position_by_symbol
    orders_by_sym = snapshot.orders_by_symbol

    for sym, dp in desired.positions.items():
        # ── CRITICAL: deadline expired ────────────────────────────────────────
        if dp.must_exit_by:
            try:
                deadline_dt = datetime.fromisoformat(dp.must_exit_by)
                if deadline_dt.tzinfo is None:
                    deadline_dt = deadline_dt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                log.warning(
                    "[RECON] Unparseable deadline for %s: %s", sym, dp.must_exit_by
                )
                deadline_dt = None

            if deadline_dt and now_utc >= deadline_dt:
                if sym in pos_by_sym:
                    expired_symbols.append(sym)
                    actions.append(ReconciliationAction(
                        priority=PRIORITY_CRITICAL,
                        action_type="deadline_exit_market",
                        symbol=sym,
                        reason=(
                            dp.must_exit_reason
                            or f"deadline_expired: {dp.must_exit_by}"
                        ),
                        qty=pos_by_sym[sym].qty,
                    ))
                    continue  # skip lower-priority checks for this symbol

        # ── HIGH: forced exit ─────────────────────────────────────────────────
        if dp.forced_exit and sym in pos_by_sym:
            forced_symbols.append(sym)
            actions.append(ReconciliationAction(
                priority=PRIORITY_HIGH,
                action_type="close_half",
                symbol=sym,
                reason=dp.must_exit_reason or "portfolio_health_critical",
                qty=round(pos_by_sym[sym].qty / 2, 6),
            ))
            continue  # still check stops after forced-half exit below if needed

        # ── NORMAL: stop order audit ──────────────────────────────────────────
        # Uses _has_stop_order() from exit_manager so both modules share the
        # same stop-detection logic (handles NormalizedOrder and raw Alpaca
        # objects; normalises enum prefixes; includes trailing_stop).
        if sym in pos_by_sym:
            sym_orders = orders_by_sym.get(sym, [])
            is_short = pos_by_sym[sym].qty < 0
            has_stop = _has_stop_order(sym, sym_orders, is_short=is_short)
            if not has_stop:
                missing_stops.append(sym)
                actions.append(ReconciliationAction(
                    priority=PRIORITY_NORMAL,
                    action_type="refresh_stop",
                    symbol=sym,
                    reason="position_unprotected: no stop order found",
                    qty=pos_by_sym[sym].qty,
                ))

    # ── NORMAL: orphaned duplicate orders ────────────────────────────────────
    for sym, orders in orders_by_sym.items():
        stop_orders = [o for o in orders if o.order_type in ("stop", "stop_limit")]
        if len(stop_orders) > 1:
            actions.append(ReconciliationAction(
                priority=PRIORITY_NORMAL,
                action_type="cancel_duplicate",
                symbol=sym,
                reason=(
                    f"duplicate_stops: {len(stop_orders)} stop orders found "
                    f"for {sym}"
                ),
            ))

    # Sort: CRITICAL first, then HIGH, then NORMAL
    _priority_order = {PRIORITY_CRITICAL: 0, PRIORITY_HIGH: 1, PRIORITY_NORMAL: 2}
    actions.sort(key=lambda a: _priority_order.get(a.priority, 9))

    return ReconciliationDiff(
        actions=actions,
        expired_symbols=expired_symbols,
        forced_symbols=forced_symbols,
        missing_stops=missing_stops,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Plan reconciliation
# ─────────────────────────────────────────────────────────────────────────────

def plan_reconciliation(diff: ReconciliationDiff) -> list[ReconciliationAction]:
    """
    Return the priority-ordered action plan from a diff.

    Currently just re-exposes diff.actions, but provides a seam for
    future throttling, deduplication, or approval-mode filtering.
    """
    return list(diff.actions)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Execute reconciliation plan
# ─────────────────────────────────────────────────────────────────────────────

def execute_reconciliation_plan(
    plan,
    alpaca_client=None,
    config=None,
    positions_map=None,
    *,
    trading_client=None,
    account_id: str = "account1",
    dry_run: bool = False,
) -> list[str]:
    """
    Execute each reconciliation action via the Alpaca client.

    Accepts both Account 1 positional call:
        execute_reconciliation_plan(plan, alpaca_client, config, positions_map)
    and Account 2 keyword call:
        execute_reconciliation_plan(plan=..., trading_client=...,
                                    account_id="account2", dry_run=False)

    Plan items may be ReconciliationAction instances (A1) or dicts (A2).

    A2 options action types handled:
      "close_broken_leg"  — close the filled leg of a partially-broken structure
      "close_expiring"    — close a structure approaching expiry (DTE ≤ 2)
      "close_structure"   — close a full structure (stop/target hit)
      "close_orphaned_leg"— close an orphaned OCC position with no matching structure

    Returns:
        List of log strings describing what was done (success or failure).
    """
    effective_client = trading_client or alpaca_client
    if effective_client is None:
        log.warning("[RECON] execute_reconciliation_plan called with no client — skipping")
        return ["[RECON] ERROR no trading client provided"]

    results: list[str] = []

    for action in plan:
        # Determine if this is an A1 ReconciliationAction or an A2 dict action
        if isinstance(action, ReconciliationAction):
            # ── A1 equity/ETF/crypto actions ─────────────────────────────────
            sym   = action.symbol
            atype = action.action_type
            try:
                if dry_run:
                    results.append(f"[RECON] DRY_RUN {atype} {sym}")
                    continue

                # Short positions (negative qty) require a buy-to-cover order, not a sell.
                # Skip all close/exit actions to avoid submitting wrong-side orders;
                # operator must handle short positions manually.
                if action.qty < 0 and atype in ("deadline_exit_market", "close_all", "close_half"):
                    log.warning(
                        "[RECON] Skipping %s for SHORT position %s qty=%.0f — "
                        "manual intervention required",
                        atype, sym, action.qty,
                    )
                    results.append(
                        f"[RECON] SKIPPED {atype} for SHORT {sym} qty={action.qty:.0f}"
                    )
                    continue

                if atype == "deadline_exit_market":
                    _execute_deadline_exit(
                        effective_client, sym, action.qty, results, action.reason
                    )

                elif atype == "close_all":
                    _close_position(effective_client, sym, action.qty, results, action.reason)

                elif atype == "close_half":
                    _close_position(effective_client, sym, action.qty, results, action.reason)

                elif atype == "cancel_duplicate":
                    _cancel_duplicate_stops(effective_client, sym, results)

                elif atype == "refresh_stop":
                    log.info(
                        "[RECON] refresh_stop for %s — delegating to exit_manager", sym
                    )
                    results.append(
                        f"[RECON] refresh_stop delegated to exit_manager: {sym}"
                    )

                else:
                    log.warning("[RECON] Unknown action_type '%s' for %s", atype, sym)
                    results.append(f"[RECON] Unknown action {atype} for {sym}")

            except Exception as exc:  # noqa: BLE001
                log.error("[RECON] %s failed for %s: %s", atype, sym, exc)
                results.append(f"[RECON] ERROR {atype} {sym}: {exc}")

        elif isinstance(action, dict):
            # ── A2 options repair actions ─────────────────────────────────────
            atype = action.get("action", "")
            sid   = action.get("structure_id", "")
            sym   = action.get("symbol", "")

            try:
                if dry_run:
                    results.append(f"[OPTS_RECON] DRY_RUN {atype} {sid or sym}")
                    continue

                if atype == "close_broken_leg":
                    _opts_close_broken_leg(effective_client, action, results)

                elif atype == "close_expiring":
                    _opts_close_structure(
                        effective_client, action, results,
                        method=action.get("method", "limit"),
                        reason="expiry_approaching",
                    )

                elif atype == "close_structure":
                    _opts_close_structure(
                        effective_client, action, results,
                        method="limit",
                        reason=action.get("reason", "recon_close"),
                    )

                elif atype == "close_orphaned_leg":
                    _opts_close_orphaned_leg(effective_client, action, results)

                else:
                    log.warning("[OPTS_RECON] Unknown action '%s'", atype)
                    results.append(f"[OPTS_RECON] Unknown action {atype}")

            except Exception as exc:  # noqa: BLE001
                log.error("[OPTS_RECON] %s failed for %s: %s", atype, sid or sym, exc)
                results.append(f"[OPTS_RECON] ERROR {atype} {sid or sym}: {exc}")

        else:
            log.warning("[RECON] Unrecognised plan item type: %s", type(action))

    return results


def _opts_close_broken_leg(
    trading_client,
    action: dict,
    results: list[str],
) -> None:
    """Close the surviving filled leg of a broken options structure."""
    from options_executor import close_structure
    from options_state import load_structures

    structure_id = action.get("structure_id", "")
    structs = {s.structure_id: s for s in load_structures()}
    struct = structs.get(structure_id)
    if struct is None:
        results.append(f"[OPTS_RECON] close_broken_leg: structure {structure_id} not found")
        return

    updated = close_structure(
        struct, trading_client, reason="broken_leg_recon", method="limit"
    )
    from options_state import save_structure
    save_structure(updated)
    log.warning("[OPTS_RECON] close_broken_leg: %s → %s", structure_id, updated.lifecycle.value)
    results.append(f"[OPTS_RECON] CLOSED broken leg for {structure_id}")


def _opts_close_structure(
    trading_client,
    action: dict,
    results: list[str],
    method: str,
    reason: str,
) -> None:
    """Close a full options structure (expiring or stop/target hit)."""
    from options_executor import close_structure
    from options_state import load_structures, save_structure

    structure_id = action.get("structure_id", "")
    structs = {s.structure_id: s for s in load_structures()}
    struct = structs.get(structure_id)
    if struct is None:
        results.append(f"[OPTS_RECON] close_structure: {structure_id} not found")
        return

    updated = close_structure(struct, trading_client, reason=reason, method=method)
    save_structure(updated)
    log.warning("[OPTS_RECON] close_structure: %s reason=%s → %s",
                structure_id, reason, updated.lifecycle.value)
    results.append(f"[OPTS_RECON] CLOSED {structure_id} reason={reason}")


def _opts_close_orphaned_leg(
    trading_client,
    action: dict,
    results: list[str],
) -> None:
    """Close an orphaned options position (OCC symbol in broker with no matching structure)."""
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    occ_symbol = action.get("occ_symbol", action.get("symbol", ""))
    qty        = int(action.get("qty", 1))
    if not occ_symbol:
        results.append("[OPTS_RECON] close_orphaned_leg: no occ_symbol in action")
        return

    req = MarketOrderRequest(
        symbol=occ_symbol,
        qty=qty,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
    )
    order = trading_client.submit_order(req)
    log.warning("[OPTS_RECON] close_orphaned_leg: %s qty=%d order=%s",
                occ_symbol, qty, order.id)
    results.append(f"[OPTS_RECON] CLOSED orphaned leg {occ_symbol} qty={qty} order={order.id}")


def _close_position(
    alpaca_client,
    symbol: str,
    qty: float,
    results: list[str],
    reason: str,
) -> None:
    """Submit a market sell for `qty` of `symbol`."""
    from alpaca.trading.enums import OrderSide, TimeInForce  # noqa: PLC0415
    from alpaca.trading.requests import MarketOrderRequest  # noqa: PLC0415

    from schemas import alpaca_symbol, is_crypto  # noqa: PLC0415

    alpaca_sym = alpaca_symbol(symbol)
    tif = TimeInForce.GTC if is_crypto(symbol) else TimeInForce.DAY
    req = MarketOrderRequest(
        symbol=alpaca_sym,
        qty=qty,
        side=OrderSide.SELL,
        time_in_force=tif,
    )
    order = alpaca_client.submit_order(req)
    log.warning(
        "[RECON] close_position: %s qty=%.6f reason=%s order_id=%s",
        symbol, qty, reason, order.id,
    )
    results.append(
        f"[RECON] CLOSED {symbol} qty={qty} reason={reason} order={order.id}"
    )


def _execute_deadline_exit(
    trading_client,
    symbol: str,
    qty: float,
    results: list[str],
    audit_reason: str,
) -> None:
    """
    Cancel all open orders for symbol, then submit a market sell.
    Used for CRITICAL deadline exits (e.g. earnings binary events).
    Cancelling first avoids Alpaca OCA share-lock conflicts.
    Non-fatal: appends result description to `results`.
    """
    from alpaca.trading.enums import (  # noqa: PLC0415
        OrderSide,
        QueryOrderStatus,
        TimeInForce,
    )
    from alpaca.trading.requests import (  # noqa: PLC0415
        GetOrdersRequest,
        MarketOrderRequest,
    )

    from schemas import alpaca_symbol, is_crypto  # noqa: PLC0415

    if trading_client is None:
        results.append(f"[RECON] DRY_RUN deadline_exit_market {symbol} qty={qty}")
        return

    # Step 1: cancel all open orders for this symbol
    cancelled: list[str] = []
    try:
        open_orders = trading_client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN)
        )
        for o in open_orders:
            if str(o.symbol) == alpaca_symbol(symbol):
                try:
                    trading_client.cancel_order_by_id(o.id)
                    cancelled.append(str(o.id))
                    log.info("[RECON] Cancelled order %s for deadline exit %s", o.id, symbol)
                except Exception as ce:
                    log.warning("[RECON] Cancel failed %s: %s", o.id, ce)
    except Exception as e:
        log.warning("[RECON] Fetch open orders failed for deadline exit %s: %s", symbol, e)

    # Step 2: submit market sell
    try:
        alpaca_sym = alpaca_symbol(symbol)
        tif = TimeInForce.GTC if is_crypto(symbol) else TimeInForce.DAY
        order = trading_client.submit_order(
            MarketOrderRequest(
                symbol=alpaca_sym,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=tif,
            )
        )
        log.info(
            "[RECON] DEADLINE EXIT MARKET %s qty=%.6f order_id=%s cancelled_orders=%s — %s",
            symbol, qty, order.id, cancelled, audit_reason,
        )
        results.append(
            f"[RECON] DEADLINE EXIT MARKET {symbol} qty={qty} "
            f"order_id={order.id} cancelled={cancelled} reason={audit_reason}"
        )
    except Exception as e:
        log.error("[RECON] DEADLINE EXIT FAILED %s: %s", symbol, e)
        results.append(f"[RECON] DEADLINE EXIT ERROR {symbol}: {e}")


def _cancel_duplicate_stops(
    alpaca_client,
    symbol: str,
    results: list[str],
) -> None:
    """Cancel all but the most-recent stop order for `symbol`."""
    orders = alpaca_client.get_orders(GetOrdersRequest(status="open", symbols=[symbol]))
    stop_orders = sorted(
        [o for o in orders if str(o.type).lower().split(".")[-1] in ("stop", "stop_limit")],
        key=lambda o: o.created_at,
        reverse=True,
    )
    # Keep the newest; cancel the rest
    for old in stop_orders[1:]:
        alpaca_client.cancel_order_by_id(str(old.id))
        log.info("[RECON] Cancelled duplicate stop %s for %s", old.id, symbol)
        results.append(f"[RECON] CANCELLED duplicate stop {old.id} for {symbol}")


# ─────────────────────────────────────────────────────────────────────────────
# 5. Backstop seeding
# ─────────────────────────────────────────────────────────────────────────────

def seed_backstop(
    symbol:      str,
    config_path: Path,
    max_hold_days: int = 5,
) -> None:
    """
    Ensure a time_bound_action exists for `symbol` in strategy_config.json.

    Called after a new BUY executes. Sets an exit deadline at max_hold_days
    calendar days from now. Does nothing if an entry already exists.

    Args:
        symbol        — canonical symbol (BTC/USD, NVDA, etc.)
        config_path   — path to strategy_config.json
        max_hold_days — calendar days until backstop exit fires
    """
    from zoneinfo import ZoneInfo as _ZI  # noqa: PLC0415

    try:
        cfg = json.loads(config_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        log.warning("[RECON] Could not load config for backstop seeding: %s", config_path)
        return

    tba = cfg.setdefault("time_bound_actions", [])
    if any(normalize_symbol(t.get("symbol", "")) == symbol for t in tba):
        log.debug("[RECON] Backstop already exists for %s", symbol)
        return

    et = _ZI("America/New_York")
    now_et   = datetime.now(et)
    deadline = (now_et.replace(hour=15, minute=45, second=0, microsecond=0)
                + __import__("datetime").timedelta(days=max_hold_days))
    deadline_utc = deadline.astimezone(timezone.utc).isoformat()

    tba.append({
        "symbol":    symbol,
        "exit_by":   deadline_utc,
        "reason":    f"backstop_exit: max_hold_{max_hold_days}d",
    })
    config_path.write_text(json.dumps(cfg, indent=2))
    log.info("[RECON] Seeded backstop for %s → exits by %s", symbol, deadline_utc)


def remove_backstop(symbol: str, config_path: Path) -> None:
    """
    Remove the time_bound_action entry for `symbol`.

    Called after a position closes successfully so the backstop doesn't
    fire on the next cycle for a symbol we no longer hold.
    """
    try:
        cfg = json.loads(config_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return

    before = len(cfg.get("time_bound_actions", []))
    cfg["time_bound_actions"] = [
        t for t in cfg.get("time_bound_actions", [])
        if normalize_symbol(t.get("symbol", "")) != symbol
    ]
    after = len(cfg.get("time_bound_actions", []))
    if before != after:
        config_path.write_text(json.dumps(cfg, indent=2))
        log.info("[RECON] Removed backstop for %s", symbol)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Top-level one-shot helper
# ─────────────────────────────────────────────────────────────────────────────

def run_account1_reconciliation(
    positions:     list[NormalizedPosition],
    snapshot:      BrokerSnapshot,
    config:        dict,
    alpaca_client,
    regime:        str = "market",
    pi_data:       Optional[dict] = None,
    now_utc:       Optional[datetime] = None,
) -> tuple[list[str], Optional[ReconciliationDiff]]:
    """
    Full reconciliation pass for Account 1.  Called from bot.py run_cycle().

    build_desired_state → diff_state → plan_reconciliation → execute

    Skips execution (returns ([], diff)) if regime == "halt".

    Args:
        positions     — current open positions list
        snapshot      — live BrokerSnapshot
        config        — parsed strategy_config.json
        alpaca_client — live Alpaca TradingClient
        regime        — current session regime string
        pi_data       — optional portfolio_intelligence output dict
        now_utc       — injectable for testing (defaults to utcnow)

    Returns:
        (log_lines, diff) — log_lines is the list of human-readable action strings;
        diff is the ReconciliationDiff for gate and monitoring use.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    # Embed pi_data into config under private key for build_desired_state
    effective_config = dict(config)
    if pi_data:
        effective_config["_pi_data"] = pi_data

    desired = build_desired_state(positions, effective_config, now_utc)
    diff    = diff_state(desired, snapshot, now_utc)
    plan    = plan_reconciliation(diff)

    if not plan:
        log.debug("[RECON] No reconciliation actions needed")
        return [], diff

    log.info(
        "[RECON] %d action(s): %d CRITICAL, %d HIGH, %d NORMAL",
        len(plan),
        sum(1 for a in plan if a.priority == PRIORITY_CRITICAL),
        sum(1 for a in plan if a.priority == PRIORITY_HIGH),
        sum(1 for a in plan if a.priority == PRIORITY_NORMAL),
    )

    if regime == "halt":
        log.warning("[RECON] regime=halt — skipping execution of %d action(s)", len(plan))
        return [f"[RECON] SKIPPED (halt): {a.action_type} {a.symbol}" for a in plan], diff

    positions_map = {normalize_symbol(p.symbol): p for p in positions}

    return execute_reconciliation_plan(
        plan, alpaca_client, config, positions_map,
    ), diff


# ─────────────────────────────────────────────────────────────────────────────
# 7. Options structure reconciliation (Account 2)
# ─────────────────────────────────────────────────────────────────────────────

def reconcile_options_structures(
    structures:   list[OptionsStructure],
    snapshot:     BrokerSnapshot,
    current_time: str,
    config:       dict,
) -> OptionsReconResult:
    """
    Full reconciliation of Account 2 options structures against live broker state.

    Checks (for each open structure):
      INTACT       — all leg OCC symbols present in snapshot positions
      BROKEN       — only some leg OCC symbols present (partial fill)
      EXPIRING SOON — structure.expiration DTE ≤ 2 days
      NEEDS CLOSE  — options_executor.should_close_structure() returns True

    Also checks (across all broker positions):
      ORPHANED LEG — OCC position in broker with no matching structure leg

    Args:
        structures   — list of OptionsStructure objects (open structures)
        snapshot     — live BrokerSnapshot for Account 2
        current_time — ISO-8601 UTC string (current cycle time)
        config       — parsed strategy_config.json

    Returns:
        OptionsReconResult with categorised lists and auxiliary metadata.
    """
    from options_executor import should_close_structure  # local import avoids circular

    result = OptionsReconResult()
    today  = date.today()

    # Index broker positions by symbol (OCC symbols for options)
    snapshot_syms = {p.symbol for p in snapshot.positions}
    # Also index by alpaca_sym in case normalisation differs
    snapshot_syms |= {p.alpaca_sym for p in snapshot.positions}

    # Collect all known OCC symbols across all structures (for orphan check)
    all_known_occs: set[str] = set()

    for struct in structures:
        if not struct.is_open():
            continue

        occ_syms = [leg.occ_symbol for leg in struct.legs if leg.occ_symbol]
        all_known_occs.update(occ_syms)

        # ── INTACT / BROKEN check ─────────────────────────────────────────────
        if occ_syms:
            present = [sym for sym in occ_syms if sym in snapshot_syms]
            if len(present) == len(occ_syms):
                result.intact.append(struct.structure_id)
            elif len(present) > 0:
                result.broken.append(struct.structure_id)
            # else: no legs visible in broker — structure not yet filled / pending

        # ── EXPIRING SOON check ───────────────────────────────────────────────
        if struct.expiration:
            try:
                exp_date = date.fromisoformat(struct.expiration)
                dte = (exp_date - today).days
                if dte <= 2:
                    result.expiring_soon.append(struct.structure_id)
            except (ValueError, TypeError):
                pass

        # ── NEEDS CLOSE check ─────────────────────────────────────────────────
        try:
            should_close, close_reason = should_close_structure(
                struct,
                current_prices={},
                config=config,
                current_time=current_time,
            )
            if should_close:
                result.needs_close.append(struct.structure_id)
                result.close_reasons[struct.structure_id] = close_reason
        except Exception as exc:
            log.debug("[OPTS_RECON] should_close check failed for %s: %s",
                      struct.structure_id, exc)

    # ── ORPHANED LEG check ────────────────────────────────────────────────────
    for pos in snapshot.positions:
        sym = pos.symbol or pos.alpaca_sym
        # Options symbols are longer than 10 chars and contain C or P (OCC format)
        if len(sym) > 10 and any(c in sym for c in ("C", "P")):
            if sym not in all_known_occs:
                result.orphaned_legs.append(sym)
                try:
                    result.orphaned_qtys[sym] = int(abs(pos.qty))
                except (TypeError, ValueError):
                    result.orphaned_qtys[sym] = 1

    return result


def plan_structure_repair(
    diff:       OptionsReconResult,
    structures: list[OptionsStructure],
    snapshot:   BrokerSnapshot,
    config:     dict,
) -> list[dict]:
    """
    Build a priority-ordered list of repair actions from an OptionsReconResult.

    Priority order: broken > expiring_soon > needs_close > orphaned_legs

    Action types produced:
      "close_broken_leg"   — close surviving filled leg of a broken structure
      "close_expiring"     — close structure approaching expiry (limit, 30-min timeout)
      "close_structure"    — close structure where stop/target hit
      "close_orphaned_leg" — close orphaned OCC position in broker

    Args:
        diff       — from reconcile_options_structures()
        structures — open structures list (for symbol lookup)
        snapshot   — live BrokerSnapshot
        config     — strategy config dict

    Returns:
        List of action dicts, highest priority first.
    """
    plan: list[dict] = []

    # Index structures by structure_id for quick lookup
    struct_by_id = {s.structure_id: s for s in structures}

    # ── 1. BROKEN (highest priority) ─────────────────────────────────────────
    for sid in diff.broken:
        struct = struct_by_id.get(sid)
        plan.append({
            "action":       "close_broken_leg",
            "structure_id": sid,
            "symbol":       struct.underlying if struct else "",
        })

    # ── 2. EXPIRING SOON ──────────────────────────────────────────────────────
    for sid in diff.expiring_soon:
        if sid in diff.broken:
            continue  # already handled above
        struct = struct_by_id.get(sid)
        plan.append({
            "action":          "close_expiring",
            "structure_id":    sid,
            "symbol":          struct.underlying if struct else "",
            "method":          "limit",
            "timeout_minutes": 30,
        })

    # ── 3. NEEDS CLOSE ────────────────────────────────────────────────────────
    for sid in diff.needs_close:
        if sid in diff.broken or sid in diff.expiring_soon:
            continue  # already handled by a higher-priority action
        struct = struct_by_id.get(sid)
        plan.append({
            "action":       "close_structure",
            "structure_id": sid,
            "symbol":       struct.underlying if struct else "",
            "reason":       diff.close_reasons.get(sid, "recon_close"),
        })

    # ── 4. ORPHANED LEGS (lowest priority) ───────────────────────────────────
    for occ_sym in diff.orphaned_legs:
        plan.append({
            "action":     "close_orphaned_leg",
            "occ_symbol": occ_sym,
            "symbol":     occ_sym,
            "qty":        diff.orphaned_qtys.get(occ_sym, 1),
        })

    return plan
