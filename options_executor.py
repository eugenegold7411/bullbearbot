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
should_roll_structure(structure, close_reason, config)     → (bool, str)
execute_roll(structure, trading_client, roll_reason, config)
                                                           → OptionsStructure
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from options_state import save_structure
from schemas import (
    OptionsStructure,
    OptionStrategy,
    StructureLifecycle,
)

log = logging.getLogger(__name__)

# ── Phase 1 strategies ───────────────────────────────────────────────────────
_PHASE1_STRATEGIES: frozenset[OptionStrategy] = frozenset({
    OptionStrategy.SINGLE_CALL,
    OptionStrategy.SINGLE_PUT,
    OptionStrategy.SHORT_PUT,
    OptionStrategy.CALL_DEBIT_SPREAD,
    OptionStrategy.PUT_DEBIT_SPREAD,
    OptionStrategy.CALL_CREDIT_SPREAD,
    OptionStrategy.PUT_CREDIT_SPREAD,
    OptionStrategy.STRADDLE,
    OptionStrategy.STRANGLE,
    OptionStrategy.IRON_CONDOR,
    OptionStrategy.IRON_BUTTERFLY,
})

# Auditable execution log path (D13)
_LOG_PATH = Path("data/account2/positions/options_log.jsonl")


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
        3. Submit LimitOrderRequest(GTC) at mid price rounded to 2dp
        4. On success → lifecycle = SUBMITTED, leg.order_id = order.id
        5. On rejection → lifecycle = REJECTED, add_audit("rejected: {error}")

      call_debit_spread / put_debit_spread:
        Single atomic mleg order (OrderClass.MLEG, TimeInForce.DAY).
        limit_price = net debit rounded to nearest $0.05 tick, capped at 2dp.
        On success → lifecycle = SUBMITTED, single order_id on all legs.
        On rejection → lifecycle = REJECTED.

      call_credit_spread / put_credit_spread / iron_condor / iron_butterfly:
        Single atomic mleg order (OrderClass.MLEG, TimeInForce.GTC).
        limit_price = net credit × 0.90 (accept 10% less than mid to improve fill),
        rounded to nearest $0.05 tick, capped at 2dp.
        GTC so the order persists past the current session.
        On success → lifecycle = SUBMITTED, single order_id on all legs.
        On rejection → lifecycle = REJECTED.

    Phase 2/3 strategies:
      lifecycle = REJECTED, add_audit("strategy not yet supported for submission")

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
        return _submit_single_leg(structure, trading_client, config)
    else:
        return _submit_spread_mleg(structure, trading_client, config)


def _submit_single_leg(
    structure:      OptionsStructure,
    trading_client,
    config:         dict | None = None,
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

    a2_cfg = (config or {}).get("account2", config or {})
    aggression = float(a2_cfg.get("debit_fill_aggression", 0.0))
    if aggression > 0 and leg.ask is not None and leg.side == "buy":
        limit_price = round(_round_limit(mid + aggression * (float(leg.ask) - mid)), 2)
    else:
        limit_price = round(_round_limit(mid), 2)

    try:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest

        order_side = OrderSide.SELL if leg.side == "sell" else OrderSide.BUY
        req = LimitOrderRequest(
            symbol=occ_sym,
            qty=structure.contracts,
            side=order_side,
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
            f"single leg submitted: {occ_sym} side={order_side.value} qty={structure.contracts} "
            f"limit={limit_price:.2f} order_id={order_id}"
        )
        log.info("[EXECUTOR] %s single leg submitted: %s side=%s limit=%.2f order=%s",
                 structure.underlying, occ_sym, order_side.value, limit_price, order_id)

    except Exception as exc:
        err = str(exc)
        structure = _set_lifecycle(
            structure, StructureLifecycle.REJECTED, f"rejected: {err}"
        )
        log.warning("[EXECUTOR] %s single leg rejected: %s", structure.underlying, err)

    return structure


def _compute_net_mid(structure: OptionsStructure) -> Optional[float]:
    """
    Net mid price for a spread order.
    Buy legs add to cost; sell legs subtract (credit received).
    Returns positive for debit spreads, negative for credit spreads.
    Returns None if any leg has no usable mid price.
    """
    total = 0.0
    for leg in structure.legs:
        mid = _mid_for_leg(leg)
        if mid is None or mid <= 0:
            return None
        if leg.side == "buy":
            total += mid
        else:
            total -= mid
    return round(total, 4)


def _compute_net_ask(structure: OptionsStructure) -> Optional[float]:
    """Net ask for a debit spread: pay ask on buy legs, receive bid on sell legs.

    Returns None if any leg is missing bid or ask.
    """
    total = 0.0
    for leg in structure.legs:
        if leg.bid is None or leg.ask is None:
            return None
        if leg.side == "buy":
            total += float(leg.ask)
        else:
            total -= float(leg.bid)
    return round(total, 4)


_CREDIT_STRATEGIES: frozenset[OptionStrategy] = frozenset({
    OptionStrategy.CALL_CREDIT_SPREAD,
    OptionStrategy.PUT_CREDIT_SPREAD,
    OptionStrategy.SHORT_PUT,
    OptionStrategy.IRON_CONDOR,
    OptionStrategy.IRON_BUTTERFLY,
})

# Credit spread fill aggressiveness: accept this fraction of mid credit to improve fill rate.
# 0.90 = accept 10% less than mid, making the order more competitive at the cost of
# slightly lower credit received. Debit spreads are unaffected.
_CREDIT_FILL_FACTOR = 0.90


def _submit_spread_mleg(
    structure:      OptionsStructure,
    trading_client,
    config:         dict | None = None,
) -> OptionsStructure:
    """
    Submit a spread as a single atomic mleg order (OrderClass.MLEG).

    Debit spreads: limit_price = net debit at mid, TIF=DAY.
    Credit spreads: limit_price = net credit × 0.90 (more aggressive to get filled),
                    TIF=GTC so the order persists past the current session.

    limit_price is always rounded to nearest $0.05 tick and capped at 2 decimal places
    before submission to satisfy Alpaca's 42210000 "must be limited to 2 decimal places"
    requirement.

    Credit spreads with net credit below config account2.min_credit_usd are rejected
    before submission — sub-threshold credits don't justify the risk.

    A single order_id is assigned to all legs. lifecycle = SUBMITTED on success.
    """
    if config is None:
        config = {}

    if len(structure.legs) < 2:
        return _set_lifecycle(
            structure, StructureLifecycle.REJECTED,
            "spread requires at least 2 legs"
        )

    net_mid = _compute_net_mid(structure)
    if net_mid is None:
        return _set_lifecycle(
            structure, StructureLifecycle.REJECTED,
            "cannot compute net mid price for mleg order — leg bid/ask unavailable"
        )

    is_credit = structure.strategy in _CREDIT_STRATEGIES

    # min_credit_usd gate: reject sub-threshold credit structures before submission
    if is_credit and net_mid < 0:
        a2_cfg = config.get("account2", config)
        min_credit = float(a2_cfg.get("min_credit_usd", 0.15))
        credit_per_share = abs(net_mid)
        if credit_per_share < min_credit:
            return _set_lifecycle(
                structure, StructureLifecycle.REJECTED,
                f"credit ${credit_per_share:.3f}/share < min_credit_usd=${min_credit:.2f} — not submitted"
            )

    if is_credit and net_mid < 0:
        # For credit structures: accept slightly less than mid to improve fill probability.
        # net_mid is negative (credit received); scaling by _CREDIT_FILL_FACTOR reduces
        # the absolute credit we demand, making our limit more competitive.
        adjusted = net_mid * _CREDIT_FILL_FACTOR
    else:
        # For debit structures: move limit toward ask to improve fill probability.
        # debit_fill_aggression=0.0 → mid (unchanged); 1.0 → net_ask (pay ask/receive bid).
        a2_cfg = (config or {}).get("account2", config or {})
        aggression = float(a2_cfg.get("debit_fill_aggression", 0.0))
        if aggression > 0:
            net_ask = _compute_net_ask(structure)
            if net_ask is not None and net_ask > net_mid:
                adjusted = net_mid + aggression * (net_ask - net_mid)
            else:
                adjusted = net_mid
        else:
            adjusted = net_mid

    # Round to $0.05 tick, then enforce 2dp. Preserve debit/credit sign.
    abs_rounded = round(round(abs(adjusted) / 0.05) * 0.05, 2)
    abs_rounded = max(0.05, abs_rounded)
    limit_price = round(abs_rounded if adjusted >= 0 else -abs_rounded, 2)

    tif_day   = False  # GTC unless explicitly debit
    if not is_credit:
        tif_day = True  # debit spreads expire at close; re-priced next cycle if missed

    try:
        from alpaca.trading.enums import OrderClass, PositionIntent, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest

        tif = TimeInForce.DAY if tif_day else TimeInForce.GTC

        leg_requests = []
        for leg in structure.legs:
            occ_sym = leg.occ_symbol or build_occ_symbol(
                structure.underlying, structure.expiration, leg.option_type, leg.strike
            )
            intent = (
                PositionIntent.BUY_TO_OPEN if leg.side == "buy"
                else PositionIntent.SELL_TO_OPEN
            )
            leg_requests.append(OptionLegRequest(
                symbol=occ_sym,
                ratio_qty=1.0,
                position_intent=intent,
            ))

        req = LimitOrderRequest(
            qty=structure.contracts,
            order_class=OrderClass.MLEG,
            time_in_force=tif,
            limit_price=limit_price,
            legs=leg_requests,
        )
        order = trading_client.submit_order(req)
        order_id = str(order.id)

        for leg in structure.legs:
            leg.order_id = order_id
        structure.order_ids.append(order_id)
        structure = _set_lifecycle(structure, StructureLifecycle.SUBMITTED, None)
        tif_str = tif.value if hasattr(tif, "value") else str(tif)
        structure.add_audit(
            f"mleg submitted: {structure.underlying} {structure.strategy.value} "
            f"qty={structure.contracts} net_limit={limit_price:.2f} tif={tif_str} "
            f"order_id={order_id}"
        )
        log.info("[EXECUTOR] %s mleg submitted: net_limit=%.2f tif=%s order=%s",
                 structure.underlying, limit_price, tif_str, order_id)

    except Exception as exc:
        err = str(exc)
        structure = _set_lifecycle(
            structure, StructureLifecycle.REJECTED, f"mleg rejected: {err}"
        )
        log.warning("[EXECUTOR] %s mleg rejected: %s", structure.underlying, err)

    return structure


def _emergency_close_leg(trading_client, occ_symbol: str, qty: int) -> None:
    """Submit a market close for a single filled option leg."""
    try:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest
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
    # Stamp close audit fields (D13)
    structure.close_reason_code = reason
    structure.close_reason_detail = (
        f"{reason} via {method} at {datetime.now(timezone.utc).isoformat()}"
    )
    structure.initiated_by = "auto_rule"
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
                from alpaca.trading.enums import OrderSide, TimeInForce
                from alpaca.trading.requests import MarketOrderRequest
                close_side = OrderSide.SELL if leg.side == "buy" else OrderSide.BUY
                req = MarketOrderRequest(
                    symbol=occ_sym,
                    qty=close_qty,
                    side=close_side,
                    time_in_force=TimeInForce.DAY,
                )
            else:
                from alpaca.trading.enums import OrderSide, TimeInForce
                from alpaca.trading.requests import LimitOrderRequest
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

    _log_structure_event(structure, "close", reason)
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
    1.  lifecycle == CANCELLED → close broken structure (broken_structure)
    2.  not is_open() → no-op
    3.  force_close_structures list in config → close (manual_close)
    4.  DTE ≤ 2 days → close (expiry_approaching)
    4a. Time-stop (after DTE check, before P&L check):
        single legs close at 40% elapsed DTE; debit spreads at 50%.
        Credit spreads excluded — theta works in their favour.
    4b. IV crush: current IV < pre-event snap × (1 − threshold) → close.
        Only fires when account2.iv_monitoring.auto_close_on_crush is True.
    5.  Loss ≥ 50% of max_risk → close (stop_loss_hit)
    6.  Gain ≥ 80% of max_profit → close (target_profit_hit)

    Returns (False, "") if none apply.
    """
    # Rule 1: broken structure
    if structure.lifecycle == StructureLifecycle.CANCELLED:
        return True, "broken_structure"

    # Must be open to evaluate P&L / DTE
    if not structure.is_open():
        return False, ""

    # Rule 3: manual close list
    force_list = config.get("force_close_structures", [])
    if structure.structure_id in force_list or structure.underlying in force_list:
        return True, "manual_close"

    # Rule 4: DTE check
    if structure.expiration:
        try:
            exp_date = date.fromisoformat(structure.expiration)
            dte = (exp_date - date.today()).days
            if dte <= 2:
                return True, "expiry_approaching"
        except (ValueError, TypeError):
            pass

    # Rule 4a: time-stop (after DTE check, before P&L check)
    _SINGLE_LEG_STRATEGIES = frozenset({
        OptionStrategy.SINGLE_CALL, OptionStrategy.SINGLE_PUT,
    })
    _DEBIT_SPREAD_STRATEGIES = frozenset({
        OptionStrategy.CALL_DEBIT_SPREAD, OptionStrategy.PUT_DEBIT_SPREAD,
    })
    if structure.strategy in _SINGLE_LEG_STRATEGIES or structure.strategy in _DEBIT_SPREAD_STRATEGIES:
        if structure.expiration and structure.opened_at:
            try:
                exp_date     = date.fromisoformat(structure.expiration)
                opened_dt    = datetime.fromisoformat(structure.opened_at)
                opened_date  = opened_dt.date()
                total_dte    = (exp_date - opened_date).days
                elapsed_dte  = (date.today() - opened_date).days
                if total_dte > 0:
                    elapsed_pct = elapsed_dte / total_dte
                    threshold   = 0.40 if structure.strategy in _SINGLE_LEG_STRATEGIES else 0.50
                    if elapsed_pct >= threshold:
                        return True, f"time_stop: elapsed {elapsed_pct:.0%} of DTE"
            except (ValueError, TypeError):
                pass

    # Rule 4b: IV crush check (only when auto_close_on_crush enabled in config)
    try:
        from options_data import detect_iv_crush  # noqa: PLC0415
        _crush, _crush_reason = detect_iv_crush(structure.underlying, config)
        if _crush:
            return True, _crush_reason
    except Exception:
        pass

    # Rules 5 & 6: P&L check using current_prices
    net_debit  = structure.net_debit_per_contract()
    max_profit = structure.max_profit_usd

    if net_debit is not None and net_debit > 0:
        # Debit structure: current_value < net_debit means loss
        current_val = _estimate_current_value(structure, current_prices)
        if current_val is not None:
            max_risk    = net_debit * structure.contracts * 100
            current_pnl = current_val - (net_debit * structure.contracts * 100)

            if current_pnl <= -(max_risk * 0.50):
                return True, "stop_loss_hit"

            if max_profit and current_pnl >= (max_profit * 0.80):
                return True, "target_profit_hit"

    return False, ""


# ─────────────────────────────────────────────────────────────────────────────
# Roll logic
# ─────────────────────────────────────────────────────────────────────────────

def should_roll_structure(
    structure:    OptionsStructure,
    close_reason: str,
    config:       dict,
) -> tuple[bool, str]:
    """
    Determine whether a structure being closed should be rolled instead.

    Roll is considered when:
    - Close reason is a DTE/time trigger (expiry_approaching or time_stop*)
    - thesis_status is "intact" or "weakened" (not "invalidated")
    - VIX regime is not crisis (checked via config account2.vix_gates.crisis_halt)

    P&L exits (stop_loss_hit, target_profit_hit), manual_close, broken_structure,
    and iv_crush are NOT roll candidates — position must exit cleanly.

    Returns (should_roll: bool, roll_reason: str).
    """
    # Only DTE/time-based triggers qualify for roll
    _ROLL_ELIGIBLE = {"expiry_approaching", "time_stop"}
    eligible = any(
        close_reason == r or close_reason.startswith(r)
        for r in _ROLL_ELIGIBLE
    )
    if not eligible:
        return False, ""

    # Invalidated thesis — don't roll
    if structure.thesis_status == "invalidated":
        return False, ""

    # Crisis VIX regime — no new options positions (including rolls)
    a2_cfg     = config.get("account2", {})
    vix_gates  = a2_cfg.get("vix_gates", {})
    float(vix_gates.get("crisis_halt", 40))
    # VIX not directly available here; check config-level override flag if present
    if config.get("_vix_crisis_halt", False):
        return False, ""

    roll_reason = (
        f"roll_eligible: {close_reason} "
        f"thesis={structure.thesis_status} "
        f"strategy={structure.strategy.value}"
    )
    return True, roll_reason


def execute_roll(
    structure:      OptionsStructure,
    trading_client,
    roll_reason:    str,
    config:         dict,
) -> OptionsStructure:
    """
    Execute a roll by closing the current structure and recording roll intent.

    The replacement structure is NOT built here — it is created on the next
    bot_options.py cycle via the normal debate → build → submit pipeline.
    The next cycle picks up the roll intent from the closing structure's
    roll_group_id and roll_reason fields.

    Steps:
    1. Close the structure (limit close)
    2. Set roll_reason and roll_group_id on the structure
    3. Persist the updated structure via save_structure()

    Returns the updated (closing) structure.
    """
    import uuid  # noqa: PLC0415

    # Assign a roll group ID if this is the first hop in the chain
    if not structure.roll_group_id:
        structure.roll_group_id = str(uuid.uuid4())[:8]
    structure.roll_reason = roll_reason
    structure.add_audit(f"roll initiated: {roll_reason} group={structure.roll_group_id}")
    # Stamp roll audit fields (D13)
    _trigger = next(
        (p for p in roll_reason.replace("roll_eligible:", "").strip().split() if "=" not in p),
        "roll",
    )
    structure.roll_reason_code = _trigger
    structure.roll_reason_detail = roll_reason
    structure.initiated_by = "execute_roll"
    # Note: rolled_to_structure_id set by bot_options.py when replacement structure is created

    log.info(
        "[EXECUTOR] execute_roll %s (%s) group=%s reason=%s",
        structure.underlying, structure.structure_id,
        structure.roll_group_id, roll_reason,
    )

    # Close the current structure
    structure = close_structure(
        structure, trading_client, reason=f"roll: {roll_reason}", method="limit"
    )
    _log_structure_event(structure, "roll_initiated", roll_reason)

    # Persist with roll metadata so next cycle can read roll_group_id
    try:
        save_structure(structure)
    except Exception as exc:
        log.warning("[EXECUTOR] execute_roll save failed (non-fatal): %s", exc)

    return structure


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

def _log_structure_event(structure: OptionsStructure, event_type: str, detail: str = "") -> None:
    """Append a structure event to options_log.jsonl. Non-fatal."""
    try:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "structure_id": structure.structure_id,
            "underlying": structure.underlying,
            "strategy": structure.strategy.value,
            "lifecycle": structure.lifecycle.value,
            "close_reason_code": structure.close_reason_code,
            "roll_reason_code": structure.roll_reason_code,
            "initiated_by": structure.initiated_by,
            "detail": detail,
        }
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception as exc:
        log.debug("[EXECUTOR] _log_structure_event failed (non-fatal): %s", exc)


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
    """Round limit price to nearest $0.05 (standard options tick). Minimum $0.05.

    The inner round(..., 2) eliminates float artifacts from n * 0.05
    (e.g., 39 * 0.05 == 1.9500000000000002 in Python). Alpaca rejects
    limit prices with more than 2 decimal places.
    """
    rounded = round(round(price / 0.05) * 0.05, 2)
    return max(0.05, rounded)
