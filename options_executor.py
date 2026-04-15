"""
options_executor.py — Pure Alpaca broker adapter for Account 2 options.

No sizing. No strategy selection. No economics computation.
Input:  OptionsStructure in PROPOSED state.
Output: OptionsStructure with updated lifecycle + order_ids.

Persistence is delegated to options_state.py.

Public API
----------
build_occ_symbol(underlying, expiry, option_type, strike) → str
submit_structure(structure, trading_client, config)        → OptionsStructure
close_structure(structure, trading_client, reason, method, timeout_minutes)
                                                           → OptionsStructure
should_close_structure(structure, current_prices, config, current_time)
                                                           → (bool, str)
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from typing import Optional

from schemas import (
    Direction,
    OptionStrategy,
    OptionsStructure,
    StructureLifecycle,
)
from options_state import save_structure

log = logging.getLogger(__name__)

# ── Phase 1 strategies eligible for sequential submission ────────────────────
_PHASE1_STRATEGIES: frozenset[OptionStrategy] = frozenset({
    OptionStrategy.SINGLE_CALL,
    OptionStrategy.SINGLE_PUT,
    OptionStrategy.CALL_DEBIT_SPREAD,
    OptionStrategy.PUT_DEBIT_SPREAD,
    OptionStrategy.CALL_CREDIT_SPREAD,
    OptionStrategy.PUT_CREDIT_SPREAD,
})

# Poll config for spread leg fill confirmation
_POLL_ATTEMPTS = 3
_POLL_INTERVAL = 2.0   # seconds between poll attempts


# ─────────────────────────────────────────────────────────────────────────────
# OCC symbol builder
# ─────────────────────────────────────────────────────────────────────────────

def build_occ_symbol(
    underlying:  str,
    expiry:      str,        # "YYYY-MM-DD"
    option_type: str,        # "call" | "put"
    strike:      float,
) -> str:
    """
    Build OCC option symbol.

    Format: {underlying}{YY}{MM}{DD}{C/P}{strike_8digit}
    Strike: multiply by 1000, zero-pad to 8 digits.

    Examples:
      GLD,  2026-12-19, call, 435.0  → "GLD261219C00435000"
      AMZN, 2026-05-15, put,  247.5  → "AMZN260515P00247500"
    """
    ticker   = underlying.replace("/", "").upper()
    date_obj = date.fromisoformat(expiry)
    date_str = date_obj.strftime("%y%m%d")
    cp       = "C" if option_type.lower().startswith("c") else "P"
    strike_i = int(round(strike * 1000))
    return f"{ticker}{date_str}{cp}{strike_i:08d}"


# ─────────────────────────────────────────────────────────────────────────────
# submit_structure
# ─────────────────────────────────────────────────────────────────────────────

def submit_structure(
    structure:      OptionsStructure,
    trading_client,
    config:         dict,
) -> OptionsStructure:
    """
    Submit all legs to Alpaca. Never raises — all errors captured in lifecycle.

    Phase 1 strategies:
      single_call / single_put:
        1. Build OCC symbol from leg data
        2. Compute mid = (bid + ask) / 2 from leg; fall back to leg.mid
        3. Submit LimitOrderRequest(GTC) at mid price
        4. On success → lifecycle = SUBMITTED, leg.order_id = order.id
        5. On rejection → lifecycle = REJECTED, add_audit("rejected: {error}")

      call_debit_spread / put_debit_spread /
      call_credit_spread / put_credit_spread:
        1. Submit long leg first (limit at mid, GTC)
        2. Record long leg order_id; lifecycle = PARTIALLY_FILLED
        3. Poll for long leg fill (max _POLL_ATTEMPTS × _POLL_INTERVAL)
        4. If filled → submit short leg (limit at mid, GTC)
        5. If both fill → lifecycle = FULLY_FILLED
        6. If long fills but short fails → cancel or close long, lifecycle = CANCELLED
        7. If long never fills → cancel long, lifecycle = REJECTED

    Phase 2/3 strategies:
      lifecycle = REJECTED, add_audit("strategy not yet supported for submission")

    All submissions: LimitOrderRequest, time_in_force=GTC,
                     order_class=SIMPLE, extended_hours=False,
                     qty = structure.contracts (integer)

    Returns updated OptionsStructure (does NOT save — caller decides).
    """
    strategy = structure.strategy

    if strategy not in _PHASE1_STRATEGIES:
        structure = _set_lifecycle(
            structure, StructureLifecycle.REJECTED,
            f"strategy {strategy.value} not yet supported for submission"
        )
        return structure

    is_single = strategy in (OptionStrategy.SINGLE_CALL, OptionStrategy.SINGLE_PUT)

    if is_single:
        return _submit_single_leg(structure, trading_client)
    else:
        return _submit_spread_sequential(structure, trading_client)


def _submit_single_leg(
    structure:      OptionsStructure,
    trading_client,
) -> OptionsStructure:
    """Submit a single-leg option (call or put)."""
    if not structure.legs:
        return _set_lifecycle(
            structure, StructureLifecycle.REJECTED, "no legs defined"
        )

    leg      = structure.legs[0]
    occ_sym  = leg.occ_symbol or build_occ_symbol(
        structure.underlying, structure.expiration, leg.option_type, leg.strike
    )
    mid      = _mid_for_leg(leg)
    if mid is None or mid <= 0:
        return _set_lifecycle(
            structure, StructureLifecycle.REJECTED,
            f"cannot compute mid price for {occ_sym} (bid={leg.bid}, ask={leg.ask})"
        )

    limit_price = _round_limit(mid)

    try:
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        req = LimitOrderRequest(
            symbol=occ_sym,
            qty=structure.contracts,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
            limit_price=limit_price,
        )
        order = trading_client.submit_order(req)
        order_id = str(order.id)

        # Update leg in place (dataclasses are mutable)
        leg.order_id = order_id

        structure = _set_lifecycle(structure, StructureLifecycle.SUBMITTED, None)
        structure.order_ids.append(order_id)
        structure.add_audit(
            f"single leg submitted: {occ_sym} qty={structure.contracts} "
            f"limit={limit_price:.2f} order_id={order_id}"
        )
        log.info("[EXECUTOR] %s single leg submitted: %s limit=%.2f order=%s",
                 structure.underlying, occ_sym, limit_price, order_id)

    except Exception as exc:
        err = str(exc)
        structure = _set_lifecycle(
            structure, StructureLifecycle.REJECTED, f"rejected: {err}"
        )
        log.warning("[EXECUTOR] %s single leg rejected: %s", structure.underlying, err)

    return structure


def _submit_spread_sequential(
    structure:      OptionsStructure,
    trading_client,
) -> OptionsStructure:
    """
    Submit a spread by sequential leg submission: long first, poll, then short.

    Aborts cleanly if either step fails.
    """
    if len(structure.legs) < 2:
        return _set_lifecycle(
            structure, StructureLifecycle.REJECTED,
            "spread requires at least 2 legs"
        )

    long_leg  = structure.legs[0]   # long leg always first per ordering rule
    short_leg = structure.legs[1]

    # ── Step 1: Submit long leg ───────────────────────────────────────────────
    long_occ = long_leg.occ_symbol or build_occ_symbol(
        structure.underlying, structure.expiration, long_leg.option_type, long_leg.strike
    )
    long_mid = _mid_for_leg(long_leg)
    if long_mid is None or long_mid <= 0:
        return _set_lifecycle(
            structure, StructureLifecycle.REJECTED,
            f"cannot compute mid for long leg {long_occ}"
        )
    long_limit = _round_limit(long_mid)

    try:
        from alpaca.trading.requests import LimitOrderRequest, GetOrderByIdRequest
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus

        long_req = LimitOrderRequest(
            symbol=long_occ,
            qty=structure.contracts,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
            limit_price=long_limit,
        )
        long_order = trading_client.submit_order(long_req)
        long_order_id = str(long_order.id)
        long_leg.order_id = long_order_id
        structure.order_ids.append(long_order_id)
        structure = _set_lifecycle(structure, StructureLifecycle.PARTIALLY_FILLED, None)
        structure.add_audit(
            f"long leg submitted: {long_occ} qty={structure.contracts} "
            f"limit={long_limit:.2f} order_id={long_order_id}"
        )
        log.info("[EXECUTOR] %s long leg submitted: %s limit=%.2f order=%s",
                 structure.underlying, long_occ, long_limit, long_order_id)

    except Exception as exc:
        err = str(exc)
        structure = _set_lifecycle(
            structure, StructureLifecycle.REJECTED,
            f"long leg rejected: {err}"
        )
        log.warning("[EXECUTOR] %s long leg rejected: %s", structure.underlying, err)
        return structure

    # ── Step 2: Poll for long leg fill ────────────────────────────────────────
    long_filled = False
    for attempt in range(_POLL_ATTEMPTS):
        time.sleep(_POLL_INTERVAL)
        try:
            order_status = trading_client.get_order_by_id(long_order_id)
            status_val = str(getattr(order_status, "status", "")).lower()
            if "fill" in status_val:
                long_filled = True
                log.info("[EXECUTOR] %s long leg filled (attempt %d/%d)",
                         structure.underlying, attempt + 1, _POLL_ATTEMPTS)
                break
            log.debug("[EXECUTOR] %s long leg status=%s (attempt %d/%d)",
                      structure.underlying, status_val, attempt + 1, _POLL_ATTEMPTS)
        except Exception as exc:
            log.debug("[EXECUTOR] %s poll attempt %d failed: %s",
                      structure.underlying, attempt + 1, exc)

    if not long_filled:
        # Cancel long leg, mark rejected
        try:
            trading_client.cancel_order_by_id(long_order_id)
            structure.add_audit(f"long leg cancelled (no fill after {_POLL_ATTEMPTS} polls)")
        except Exception as exc:
            structure.add_audit(f"long leg cancel failed: {exc}")
        structure = _set_lifecycle(
            structure, StructureLifecycle.REJECTED,
            f"long leg not filled after {_POLL_ATTEMPTS} attempts — spread aborted"
        )
        log.warning("[EXECUTOR] %s spread aborted: long leg not filled", structure.underlying)
        return structure

    # ── Step 3: Submit short leg ──────────────────────────────────────────────
    short_occ = short_leg.occ_symbol or build_occ_symbol(
        structure.underlying, structure.expiration, short_leg.option_type, short_leg.strike
    )
    short_mid = _mid_for_leg(short_leg)
    if short_mid is None or short_mid <= 0:
        # Can't price short leg — close long and abort
        _emergency_close_leg(trading_client, long_occ, structure.contracts)
        structure.add_audit(
            f"short leg price unavailable — long leg closed, spread aborted"
        )
        structure = _set_lifecycle(structure, StructureLifecycle.CANCELLED,
            "short leg mid price unavailable; long leg emergency-closed")
        return structure

    short_limit = _round_limit(short_mid)

    try:
        short_req = LimitOrderRequest(
            symbol=short_occ,
            qty=structure.contracts,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            limit_price=short_limit,
        )
        short_order = trading_client.submit_order(short_req)
        short_order_id = str(short_order.id)
        short_leg.order_id = short_order_id
        structure.order_ids.append(short_order_id)
        structure = _set_lifecycle(structure, StructureLifecycle.FULLY_FILLED, None)
        structure.add_audit(
            f"short leg submitted: {short_occ} qty={structure.contracts} "
            f"limit={short_limit:.2f} order_id={short_order_id}"
        )
        log.info("[EXECUTOR] %s short leg submitted: %s limit=%.2f order=%s — spread FULLY_FILLED",
                 structure.underlying, short_occ, short_limit, short_order_id)

    except Exception as exc:
        err = str(exc)
        # Long is filled, short failed — emergency close long leg
        _emergency_close_leg(trading_client, long_occ, structure.contracts)
        structure.add_audit(
            f"short leg rejected ({err}) — long leg emergency-closed; spread aborted"
        )
        structure = _set_lifecycle(structure, StructureLifecycle.CANCELLED,
            f"spread aborted: short leg failed after long fill: {err}")
        _send_spread_abort_sms(structure)
        log.error("[EXECUTOR] %s SPREAD ABORTED: short leg failed, long closed: %s",
                  structure.underlying, err)

    return structure


def _emergency_close_leg(trading_client, occ_symbol: str, qty: int) -> None:
    """Submit a market close for a single filled option leg."""
    try:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        req = MarketOrderRequest(
            symbol=occ_symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        trading_client.submit_order(req)
        log.info("[EXECUTOR] emergency close submitted for %s qty=%d", occ_symbol, qty)
    except Exception as exc:
        log.error("[EXECUTOR] emergency close FAILED for %s: %s", occ_symbol, exc)


def _send_spread_abort_sms(structure: OptionsStructure) -> None:
    """Non-fatal SMS alert when a spread aborts after long fill."""
    try:
        import os
        from twilio.rest import Client
        sid   = os.getenv("TWILIO_ACCOUNT_SID")
        token = os.getenv("TWILIO_AUTH_TOKEN")
        from_ = os.getenv("TWILIO_FROM_NUMBER")
        to    = os.getenv("TWILIO_TO_NUMBER")
        if not all([sid, token, from_, to]):
            return
        client = Client(sid, token)
        client.messages.create(
            body=(
                f"⚠ A2 SPREAD ABORTED: {structure.underlying} "
                f"{structure.strategy.value} — short leg failed after long fill. "
                f"Long leg emergency-closed. Check positions."
            ),
            from_=from_,
            to=to,
        )
    except Exception as exc:
        log.debug("[EXECUTOR] SMS alert failed (non-fatal): %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# close_structure
# ─────────────────────────────────────────────────────────────────────────────

def close_structure(
    structure:       OptionsStructure,
    trading_client,
    reason:          str,
    method:          str = "limit",   # "limit" | "market"
    timeout_minutes: int = 30,
) -> OptionsStructure:
    """
    Close all open legs of a structure.

    For each leg with a non-None filled_price (i.e. was filled):
      - method="limit":  submit closing order at current mid price, GTC
      - method="market": submit market close, DAY

    After submitting closes:
      - lifecycle = CLOSING
      - closed_at = now ISO
      - add_audit(reason)

    If method="market": lifecycle = CLOSED immediately (fill presumed).
    If method="limit":  stays CLOSING — reconciliation will confirm fills.
    """
    filled_legs = [leg for leg in structure.legs if leg.filled_price is not None]
    if not filled_legs:
        # No confirmed fills — nothing to close
        structure = _set_lifecycle(structure, StructureLifecycle.CANCELLED, f"close: {reason}")
        structure.closed_at = datetime.now(timezone.utc).isoformat()
        return structure

    structure.add_audit(f"close initiated: reason={reason} method={method}")
    all_submitted = True

    for leg in filled_legs:
        occ_sym   = leg.occ_symbol
        close_qty = structure.contracts

        try:
            if method == "market":
                from alpaca.trading.requests import MarketOrderRequest
                from alpaca.trading.enums import OrderSide, TimeInForce
                close_side = OrderSide.SELL if leg.side == "buy" else OrderSide.BUY
                req = MarketOrderRequest(
                    symbol=occ_sym,
                    qty=close_qty,
                    side=close_side,
                    time_in_force=TimeInForce.DAY,
                )
            else:
                from alpaca.trading.requests import LimitOrderRequest
                from alpaca.trading.enums import OrderSide, TimeInForce
                close_side = OrderSide.SELL if leg.side == "buy" else OrderSide.BUY
                mid = _mid_for_leg(leg)
                limit_price = _round_limit(mid) if mid and mid > 0 else 0.05
                req = LimitOrderRequest(
                    symbol=occ_sym,
                    qty=close_qty,
                    side=close_side,
                    time_in_force=TimeInForce.GTC,
                    limit_price=limit_price,
                )

            order = trading_client.submit_order(req)
            order_id = str(order.id)
            structure.order_ids.append(order_id)
            structure.add_audit(f"close leg {occ_sym} submitted: order_id={order_id}")
            log.info("[EXECUTOR] close leg %s %s order=%s", occ_sym, method, order_id)

        except Exception as exc:
            all_submitted = False
            structure.add_audit(f"close leg {occ_sym} FAILED: {exc}")
            log.error("[EXECUTOR] close %s failed: %s", occ_sym, exc)

    structure.closed_at = datetime.now(timezone.utc).isoformat()

    if method == "market":
        structure = _set_lifecycle(structure, StructureLifecycle.CLOSED,
                                   f"market close submitted: {reason}")
    else:
        structure = _set_lifecycle(structure, StructureLifecycle.CLOSED
                                   if all_submitted else StructureLifecycle.CANCELLED,
                                   f"limit close submitted: {reason}")

    return structure


# ─────────────────────────────────────────────────────────────────────────────
# should_close_structure
# ─────────────────────────────────────────────────────────────────────────────

def should_close_structure(
    structure:     OptionsStructure,
    current_prices: dict,
    config:        dict,
    current_time:  str,
) -> tuple[bool, str]:
    """
    Determine if a structure should be closed. Returns (should_close, reason).

    Rules (checked in order):
    1. DTE ≤ 2 days → close (expiry_approaching)
    2. Loss ≥ 50% of max_risk → close (stop_loss_hit)
    3. Gain ≥ 80% of max_profit → close (target_profit_hit)
    4. lifecycle == CANCELLED → close broken structure (broken_structure)
    5. force_close_structures list in config → close (manual_close)

    Returns (False, "") if none apply.
    """
    # Rule 4: broken structure
    if structure.lifecycle == StructureLifecycle.CANCELLED:
        return True, "broken_structure"

    # Must be open to evaluate P&L / DTE
    if not structure.is_open():
        return False, ""

    # Rule 5: manual close list
    force_list = config.get("force_close_structures", [])
    if structure.structure_id in force_list or structure.underlying in force_list:
        return True, "manual_close"

    # Rule 1: DTE check
    if structure.expiration:
        try:
            exp_date = date.fromisoformat(structure.expiration)
            dte = (exp_date - date.today()).days
            if dte <= 2:
                return True, "expiry_approaching"
        except (ValueError, TypeError):
            pass

    # Rules 2 & 3: P&L check using current_prices
    net_debit = structure.net_debit_per_contract()
    max_profit = structure.max_profit_usd

    if net_debit is not None and net_debit > 0:
        # Debit structure: current_value < net_debit means loss
        current_val = _estimate_current_value(structure, current_prices)
        if current_val is not None:
            max_risk = net_debit * structure.contracts * 100
            current_pnl = (current_val - (net_debit * structure.contracts * 100))

            if current_pnl <= -(max_risk * 0.50):
                return True, "stop_loss_hit"

            if max_profit and current_pnl >= (max_profit * 0.80):
                return True, "target_profit_hit"

    return False, ""


def _estimate_current_value(structure: OptionsStructure, current_prices: dict) -> Optional[float]:
    """
    Estimate current market value of the structure from current_prices dict.
    current_prices: {occ_symbol: float (mid price)} or {underlying: float (spot)}.
    Returns total value in USD or None if unavailable.
    """
    total = 0.0
    for leg in structure.legs:
        price = current_prices.get(leg.occ_symbol) or current_prices.get(leg.underlying)
        if price is None:
            return None
        if leg.side == "buy":
            total += price * structure.contracts * 100
        else:
            total -= price * structure.contracts * 100
    return total


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _set_lifecycle(
    structure: OptionsStructure,
    lifecycle: StructureLifecycle,
    audit_msg: Optional[str],
) -> OptionsStructure:
    """Set lifecycle and optionally add an audit entry."""
    structure.lifecycle = lifecycle
    if audit_msg:
        structure.add_audit(audit_msg)
    return structure


def _mid_for_leg(leg) -> Optional[float]:
    """Compute mid price for a leg. Uses bid/ask if available, then mid, then filled_price."""
    if leg.bid is not None and leg.ask is not None:
        b, a = float(leg.bid), float(leg.ask)
        if b > 0 or a > 0:
            return (b + a) / 2.0
    if leg.mid is not None and float(leg.mid) > 0:
        return float(leg.mid)
    if leg.filled_price is not None and float(leg.filled_price) > 0:
        return float(leg.filled_price)
    return None


def _round_limit(price: float) -> float:
    """Round limit price to nearest $0.05 (standard options tick). Minimum $0.05."""
    rounded = round(price / 0.05) * 0.05
    return max(0.05, rounded)
