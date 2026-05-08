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
import time
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

_SAFETY_DEDUP_SECS: float = 300.0
_SAFETY_ALERT_CACHE: dict[str, float] = {}
_MAX_CLOSE_ATTEMPTS: int = 5

# Cached subclasses — built lazily so Alpaca imports stay deferred.
_CLOSE_REQ_CLASSES: "tuple | None" = None


def _get_close_request_classes() -> tuple:
    """
    Return (_OptionLimitCloseRequest, _OptionMarketCloseRequest) — subclasses of
    the Alpaca SDK request classes that carry position_effect="closing" through
    to the API payload.  Built once and cached; Alpaca imports are deferred so
    they stay compatible with test stubs.

    Falls back to the base classes directly when stubs are lambdas or otherwise
    non-subclassable (e.g., in tests that patch alpaca.trading.requests).
    """
    global _CLOSE_REQ_CLASSES
    if _CLOSE_REQ_CLASSES is not None:
        return _CLOSE_REQ_CLASSES
    from alpaca.trading.requests import (  # noqa: PLC0415
        LimitOrderRequest,
        MarketOrderRequest,
    )
    try:
        from pydantic import ConfigDict  # noqa: PLC0415

        class _OptionLimitCloseRequest(LimitOrderRequest):
            model_config = ConfigDict(extra="allow", validate_assignment=True)

        class _OptionMarketCloseRequest(MarketOrderRequest):
            model_config = ConfigDict(extra="allow", validate_assignment=True)

        _CLOSE_REQ_CLASSES = (_OptionLimitCloseRequest, _OptionMarketCloseRequest)
    except Exception:
        # Base classes are stubs/lambdas (test environment) — use them directly.
        # position_effect="closing" will be accepted as a kwarg and ignored by stubs.
        _CLOSE_REQ_CLASSES = (LimitOrderRequest, MarketOrderRequest)
    return _CLOSE_REQ_CLASSES


def _fetch_live_close_price(occ_symbol: str) -> "Optional[float]":
    """
    Fetch current bid price for a single option contract via Alpaca snapshot.
    Used to price close limit orders at current market, not avg entry cost.
    Falls back to last trade price when bid is unavailable.
    Non-fatal: returns None on any failure so callers fall back to _mid_for_leg.
    """
    try:
        from alpaca.data.requests import OptionSnapshotRequest  # noqa: PLC0415

        import options_data  # noqa: PLC0415
        client = options_data._make_options_data_client()
        req = OptionSnapshotRequest(symbol_or_symbols=occ_symbol)
        snap = client.get_option_snapshot(req)
        if not snap or occ_symbol not in snap:
            return None
        data = snap[occ_symbol]
        quote = getattr(data, "latest_quote", None)
        if quote is not None:
            bid = getattr(quote, "bid_price", None)
            if bid is not None and float(bid) > 0:
                return float(bid)
        trade = getattr(data, "latest_trade", None)
        if trade is not None:
            price = getattr(trade, "price", None)
            if price is not None and float(price) > 0:
                return float(price)
        return None
    except Exception as exc:
        log.debug("[EXECUTOR] live close price unavailable for %s: %s", occ_symbol, exc)
        return None


def _fire_safety_alert(fn_name: str, exc: Exception) -> None:
    try:
        now = time.time()
        if now - _SAFETY_ALERT_CACHE.get(fn_name, 0) < _SAFETY_DEDUP_SECS:
            return
        _SAFETY_ALERT_CACHE[fn_name] = now
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        msg = (
            f"[SAFETY DEGRADED] options_executor.{fn_name} threw: "
            f"{type(exc).__name__}: {exc}. "
            f"Fallback active — manual review required. {ts}"
        )
        try:
            from notifications import send_whatsapp_direct  # noqa: PLC0415
            send_whatsapp_direct(msg)
        except Exception:
            pass
    except Exception:
        pass


def _cancel_holding_orders(trading_client, occ_sym: str, exc: Exception) -> bool:
    """
    If exc is an Alpaca 40310000 held_for_orders error, cancel the related orders
    so the qty is released for a close attempt. Returns True if any cancel was attempted.
    """
    try:
        import uuid  # noqa: PLC0415
        exc_str = str(exc)
        if "held_for_orders" not in exc_str or "related_orders" not in exc_str:
            return False
        # Parse the JSON body out of the exception string.
        _body_start = exc_str.find("{")
        if _body_start == -1:
            return False
        _data = json.loads(exc_str[_body_start:])
        _related = _data.get("related_orders") or []
        if not _related:
            return False
        _canceled = 0
        for _oid in _related:
            try:
                trading_client.cancel_order_by_id(uuid.UUID(str(_oid)))
                log.info(
                    "[EXECUTOR] canceled holding order %s for %s to release qty",
                    _oid, occ_sym,
                )
                _canceled += 1
            except Exception as _cex:
                log.warning(
                    "[EXECUTOR] could not cancel holding order %s for %s: %s",
                    _oid, occ_sym, _cex,
                )
        return _canceled > 0
    except Exception as _pex:
        log.warning("[EXECUTOR] _cancel_holding_orders parse error: %s", _pex)
        return False


def _fire_manual_review_alert(
    symbol: str, sid_short: str, attempts: int, positions_str: str
) -> None:
    try:
        from notifications import send_whatsapp_direct  # noqa: PLC0415
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        msg = (
            f"[A2 MANUAL REVIEW] {symbol} ({sid_short}): close failed {attempts} times"
            f" — positions still open: {positions_str}."
            f" Marked manual_review_required. {ts}"
        )
        send_whatsapp_direct(msg)
    except Exception:
        pass


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

    All spreads use TIF=GTC. Preflight cancels stale orders before resubmit,
    so debit spreads need time across multiple cycles to fill.
    Credit spreads: limit_price = net credit × 0.90 (more aggressive to get filled).

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

    try:
        from alpaca.trading.enums import OrderClass, PositionIntent, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest

        # GTC for all mleg orders — preflight cancels stale orders before resubmit,
        # so spreads need multiple cycles to fill rather than expiring after ~2 min.
        tif = TimeInForce.GTC

        # Fetch existing positions to determine correct position intent per leg.
        # Alpaca 42210000 fires when intent doesn't match the inferred position state:
        #   SELL leg + existing LONG (qty > 0) must use SELL_TO_CLOSE
        #   BUY  leg + existing SHORT (qty < 0) must use BUY_TO_CLOSE
        # All other cases use the _TO_OPEN variant.
        try:
            existing_pos = {str(p.symbol): float(p.qty) for p in trading_client.get_all_positions()}
        except Exception:
            existing_pos = {}

        leg_requests = []
        for leg in structure.legs:
            occ_sym = leg.occ_symbol or build_occ_symbol(
                structure.underlying, structure.expiration, leg.option_type, leg.strike
            )
            pos_qty = existing_pos.get(occ_sym, 0.0)
            if leg.side == "buy":
                intent = PositionIntent.BUY_TO_CLOSE if pos_qty < 0 else PositionIntent.BUY_TO_OPEN
            else:
                intent = PositionIntent.SELL_TO_CLOSE if pos_qty > 0 else PositionIntent.SELL_TO_OPEN
            log.info(
                "[A2_EXEC] %s intent=%s existing_position=%s (qty=%.0f)",
                occ_sym, getattr(intent, "value", str(intent)), pos_qty != 0.0, pos_qty,
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
        from alpaca.trading.enums import OrderSide, TimeInForce  # noqa: PLC0415
        _, _MktCloseReq = _get_close_request_classes()
        req = _MktCloseReq(
            symbol=occ_symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            position_effect="closing",
        )
        trading_client.submit_order(req)
        log.info("[EXECUTOR] emergency close submitted for %s qty=%d", occ_symbol, qty)
    except Exception as exc:
        log.error("[EXECUTOR] emergency close FAILED for %s: %s", occ_symbol, exc)
        _fire_safety_alert("emergency_close_leg_failed", exc)


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

def _close_spread_mleg(
    structure:      OptionsStructure,
    trading_client,
) -> str:
    """
    Close a multi-leg spread as a single atomic MLEG order.

    Submits SELL_TO_CLOSE (for buy-side legs) and BUY_TO_CLOSE (for sell-side legs)
    in one instruction so Alpaca never sees an uncovered short between two orders.
    limit_price = net mid at current prices (falls back to filled prices); minimum $0.05.

    Returns the order_id string on success. Raises on any Alpaca error so the
    caller can fall back to per-leg submission.
    """
    from alpaca.trading.enums import OrderClass, PositionIntent, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest

    filled_legs = [leg for leg in structure.legs if leg.filled_price is not None]
    leg_requests = []
    for leg in filled_legs:
        intent = (
            PositionIntent.SELL_TO_CLOSE if leg.side == "buy"
            else PositionIntent.BUY_TO_CLOSE
        )
        leg_requests.append(OptionLegRequest(
            symbol=leg.occ_symbol,
            ratio_qty=1.0,
            position_intent=intent,
        ))

    net_mid = _compute_net_mid(structure)
    if net_mid is None:
        # Fall back to fill prices as a conservative estimate
        net_mid = sum(
            float(leg.filled_price) * (1.0 if leg.side == "buy" else -1.0)
            for leg in filled_legs
            if leg.filled_price is not None
        )
    limit_price = max(0.05, _round_limit(abs(net_mid) if net_mid else 0.05))

    req = LimitOrderRequest(
        qty=structure.contracts,
        order_class=OrderClass.MLEG,
        time_in_force=TimeInForce.GTC,
        limit_price=limit_price,
        legs=leg_requests,
    )
    order = trading_client.submit_order(req)
    return str(order.id)


def _compute_realized_pnl_estimate(
    structure: OptionsStructure,
    current_prices: dict | None,
) -> "float | None":
    """Best-effort realized P&L at close time (entry fills vs current mids)."""
    if current_prices:
        try:
            total = 0.0
            for leg in structure.legs:
                entry   = leg.filled_price
                current = current_prices.get(leg.occ_symbol)
                if entry is None or current is None:
                    return None
                sign    = 1.0 if leg.side == "buy" else -1.0
                total  += sign * (current - entry) * leg.qty * structure.contracts * 100
            return round(total, 2)
        except Exception:
            pass
    if structure.pnl_unrealized is not None:
        return structure.pnl_unrealized
    return None


def close_structure(
    structure:       OptionsStructure,
    trading_client,
    reason:          str,
    method:          str = "limit",   # "limit" | "market"
    timeout_minutes: int = 30,
    current_prices:  dict | None = None,
) -> OptionsStructure:
    """
    Close all open legs of a structure.

    For each leg with a non-None filled_price (i.e. was filled):
      - method="limit":  submit closing order at current mid price, GTC
      - method="market": submit market close, DAY

    For multi-leg spreads (≥ 2 filled legs with mixed buy/sell sides) and
    method="limit", closes atomically via a single MLEG order to prevent
    Alpaca 40310000 (uncovered short) errors that arise from sequential
    per-leg submission where the long is closed before the short.

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

    # Atomic MLEG close for spreads: submit both legs simultaneously to avoid
    # the uncovered-short window that triggers Alpaca 40310000 when legs are
    # closed sequentially (long closed first → short becomes naked).
    _is_spread = (
        method == "limit"
        and len(filled_legs) >= 2
        and any(l.side == "buy" for l in filled_legs)
        and any(l.side == "sell" for l in filled_legs)
    )
    if _is_spread:
        try:
            order_id = _close_spread_mleg(structure, trading_client)
            structure.order_ids.append(order_id)
            structure.add_audit(f"mleg close submitted: order_id={order_id}")
            log.info("[EXECUTOR] %s mleg close submitted: reason=%s order=%s",
                     structure.underlying, reason, order_id)
            structure.realized_pnl = _compute_realized_pnl_estimate(structure, current_prices)
            structure.closed_at = datetime.now(timezone.utc).isoformat()
            structure = _set_lifecycle(
                structure, StructureLifecycle.CLOSED, f"mleg close submitted: {reason}"
            )
            _log_structure_event(structure, "close", reason)
            return structure
        except Exception as _mleg_exc:
            log.warning(
                "[EXECUTOR] %s MLEG close failed (%s) — falling back to per-leg",
                structure.underlying, _mleg_exc,
            )
            _fire_safety_alert("close_structure_mleg_failed", _mleg_exc)

    # Per-leg fallback: sort sell-side (BUY_TO_CLOSE) first so the short leg is
    # closed before the long, avoiding a naked-short window on MLEG fallback paths.
    all_submitted = True
    filled_legs_ordered = sorted(filled_legs, key=lambda leg: 0 if leg.side == "sell" else 1)

    for leg in filled_legs_ordered:
        occ_sym   = leg.occ_symbol
        close_qty = structure.contracts

        try:
            from alpaca.trading.enums import (  # noqa: PLC0415
                OrderSide,
                PositionIntent,
                TimeInForce,
            )
            _LimitCloseReq, _MktCloseReq = _get_close_request_classes()
            close_side   = OrderSide.SELL if leg.side == "buy" else OrderSide.BUY
            close_intent = (
                PositionIntent.SELL_TO_CLOSE if leg.side == "buy"
                else PositionIntent.BUY_TO_CLOSE
            )
            if method == "market":
                req = _MktCloseReq(
                    symbol=occ_sym,
                    qty=close_qty,
                    side=close_side,
                    position_intent=close_intent,
                    time_in_force=TimeInForce.DAY,
                    position_effect="closing",
                )
            else:
                live_price = _fetch_live_close_price(occ_sym)
                if live_price is not None:
                    log.info("[EXECUTOR] %s close limit=%.4f (live bid)", occ_sym, live_price)
                    limit_price = _round_limit(live_price)
                else:
                    mid = _mid_for_leg(leg)
                    limit_price = _round_limit(mid) if mid and mid > 0 else 0.05
                req = _LimitCloseReq(
                    symbol=occ_sym,
                    qty=close_qty,
                    side=close_side,
                    position_intent=close_intent,
                    time_in_force=TimeInForce.GTC,
                    limit_price=limit_price,
                    position_effect="closing",
                )

            order = trading_client.submit_order(req)
            order_id = str(order.id)
            structure.order_ids.append(order_id)
            structure.add_audit(f"close leg {occ_sym} submitted: order_id={order_id}")
            log.info("[EXECUTOR] close leg %s %s order=%s", occ_sym, method, order_id)

        except Exception as exc:
            # Attempt bracket cancel + single retry for held_for_orders errors.
            _cancel_attempted = _cancel_holding_orders(trading_client, occ_sym, exc)
            if _cancel_attempted:
                try:
                    order = trading_client.submit_order(req)
                    order_id = str(order.id)
                    structure.order_ids.append(order_id)
                    structure.add_audit(
                        f"close leg {occ_sym} submitted after bracket cancel: order_id={order_id}"
                    )
                    log.info(
                        "[EXECUTOR] close leg %s %s order=%s (after bracket cancel)",
                        occ_sym, method, order_id,
                    )
                    continue
                except Exception as _retry_exc:
                    log.error(
                        "[EXECUTOR] close %s retry after bracket cancel also failed: %s",
                        occ_sym, _retry_exc,
                    )
                    exc = _retry_exc
            all_submitted = False
            structure.add_audit(f"close leg {occ_sym} FAILED: {exc}")
            log.error("[EXECUTOR] close %s failed: %s", occ_sym, exc)

    if all_submitted:
        structure.realized_pnl = _compute_realized_pnl_estimate(structure, current_prices)
        structure.closed_at = datetime.now(timezone.utc).isoformat()
        close_label = "market" if method == "market" else "limit"
        structure = _set_lifecycle(
            structure, StructureLifecycle.CLOSED,
            f"{close_label} close submitted: {reason}",
        )
    else:
        # One or more per-leg close orders failed.
        # Verify via Alpaca whether the positions are actually gone before marking closed.
        # This prevents orphaning: a failed close must not be recorded as successful.
        _leg_occs = {leg.occ_symbol for leg in structure.legs if leg.occ_symbol}
        _still_open = True
        try:
            _live_syms = {str(p.symbol) for p in trading_client.get_all_positions()}
            _still_open = bool(_leg_occs & _live_syms)
        except Exception as _vex:
            log.warning(
                "[EXECUTOR] %s: position verify failed after close failure — "
                "assuming still open: %s",
                structure.underlying, _vex,
            )

        if not _still_open:
            # Position gone from Alpaca — expired or closed externally.
            structure.realized_pnl = _compute_realized_pnl_estimate(structure, current_prices)
            structure.closed_at = datetime.now(timezone.utc).isoformat()
            structure = _set_lifecycle(
                structure, StructureLifecycle.CLOSED,
                f"close verified: position absent from Alpaca: {reason}",
            )
            log.info(
                "[EXECUTOR] %s: position absent from Alpaca after failed close order"
                " — marked closed",
                structure.underlying,
            )
        else:
            # Close FAILED and position still live — do NOT mark closed or set closed_at.
            structure.close_attempt_count += 1
            _positions_str = ", ".join(sorted(_leg_occs))
            log.error(
                "[EXECUTOR] %s (%s) close FAILED — position still live in Alpaca "
                "after %d attempt(s). Lifecycle unchanged at %s.",
                structure.underlying, structure.structure_id[:8],
                structure.close_attempt_count, structure.lifecycle.value,
            )
            if structure.close_attempt_count == 1:
                # First failure — notify once.
                _fire_safety_alert(
                    f"close_stuck_{structure.structure_id[:8]}",
                    Exception(
                        f"A2 {structure.underlying} ({structure.structure_id[:8]}) "
                        f"close failed on first attempt — positions still open: {_positions_str}"
                    ),
                )
            elif structure.close_attempt_count >= _MAX_CLOSE_ATTEMPTS:
                # Max attempts reached — escalate and stop retrying.
                structure = _set_lifecycle(
                    structure, StructureLifecycle.MANUAL_REVIEW_REQUIRED,
                    f"close failed {structure.close_attempt_count} times — manual review required",
                )
                log.error(
                    "[EXECUTOR] %s: max close attempts (%d) reached — marking manual_review_required",
                    structure.underlying, structure.close_attempt_count,
                )
                _fire_manual_review_alert(
                    structure.underlying,
                    structure.structure_id[:8],
                    structure.close_attempt_count,
                    _positions_str,
                )

    _log_structure_event(structure, "close", reason)
    return structure


# ─────────────────────────────────────────────────────────────────────────────
# Tiered exit system helpers
# ─────────────────────────────────────────────────────────────────────────────

def _compute_dte(structure: OptionsStructure) -> Optional[int]:
    """Days-to-expiry for the structure's expiration date. None if unparseable."""
    if not structure.expiration:
        return None
    try:
        return (date.fromisoformat(structure.expiration) - date.today()).days
    except (ValueError, TypeError):
        return None


def _max_loss_usd_estimate(structure: OptionsStructure) -> Optional[float]:
    """
    Best-available max-loss proxy in USD for the structure.

    For debit positions: sum of buy-leg fills × contracts × 100.
    Falls back to max_cost_usd when fills are missing (e.g. orphan recovery).
    """
    _buy_cost = sum(
        float(leg.filled_price) * structure.contracts * 100
        for leg in structure.legs
        if leg.side == "buy" and leg.filled_price is not None
    )
    if _buy_cost > 0:
        return _buy_cost
    if structure.max_cost_usd:
        try:
            return float(structure.max_cost_usd)
        except (TypeError, ValueError):
            return None
    return None


def _resolve_close_targets(structure: OptionsStructure, config: dict) -> dict:
    """
    Effective close thresholds for this structure.

    Per-structure overrides on OptionsStructure take precedence over config.
    Used so orphan_tracked structures can carry tighter exit rules.
    """
    a2 = config.get("account2", {}) if isinstance(config, dict) else {}
    return {
        "profit_target_pct": (
            structure.close_profit_target_pct
            if structure.close_profit_target_pct is not None
            else float(a2.get("profit_target_pct", 0.75))
        ),
        "max_loss_pct": (
            structure.close_max_loss_pct
            if structure.close_max_loss_pct is not None
            else float(a2.get("max_loss_exit_pct", 0.50))
        ),
        "time_stop_pct_dte": (
            structure.close_time_stop_pct_dte
            if structure.close_time_stop_pct_dte is not None
            else None
        ),
    }


def _current_iv_rank(symbol: str) -> Optional[float]:
    """Best-effort current IV rank for the underlying. None on any failure."""
    try:
        from options_data import compute_iv_rank  # noqa: PLC0415
        return compute_iv_rank(symbol)
    except Exception:
        return None


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

    Tiered exit system (checked in order):

    Pre-checks
        1.  lifecycle == CANCELLED → broken_structure
        2.  not is_open() → no-op
        3.  force_close_structures list → manual_close

    Layer 3 — Time exits (highest priority; theta deadline doesn't negotiate)
        L3a. DTE ≤ 2                          → expiry_approaching
        L3b. DTE ≤ 4 AND pnl < 0              → loss_cut_near_expiry
        L3c. DTE ≤ 6 AND loss ≥ 25% of max    → time_stop_loss_near_expiry
        L3d. Existing elapsed-DTE % rule (single legs 40%, debit spreads 50%)

    L4b. IV crush (config-gated)              → iv_crush_*

    Layer 1 — Profit exits
        L1d. peak ≥ 60% gain AND retraced to 30% → profit_lock_retrace
        L1b. DTE ≤ 6 AND pnl ≥ 50% of max_profit → profit_target_50_near_expiry
        L1c. IV up ≥ 20% from entry AND pnl ≥ 40% of max_profit → iv_expansion_take_profit
        L1a. pnl ≥ 80% of max_profit (current_prices path) → target_profit_hit
        L1a'. pnl_unrealized ≥ profit_target_pct of max_profit → profit_target_pct_hit

    Layer 2 — Loss exits
        L2a. Loss ≥ 50% of max_risk (current_prices path) → stop_loss_hit
        L2c. Theta cost > $50/day AND loss ≥ 20% of max_loss → theta_burn_underwater
        L2a'. pnl_unrealized ≤ -max_loss_pct of buy_cost → max_loss_exit

    Per-structure overrides via OptionsStructure.close_profit_target_pct /
    close_max_loss_pct apply to L1a'/L2a' (used for orphan_tracked).
    """
    # Rule 1: broken structure — but skip if already sent through close_structure()
    # (closed_at set means a close attempt was made; don't re-enter the close loop)
    if structure.lifecycle == StructureLifecycle.CANCELLED and not structure.closed_at:
        return True, "broken_structure"

    # Must be open to evaluate P&L / DTE
    if not structure.is_open():
        return False, ""

    # Rule 3: manual close list (check both top-level and account2 sub-dict)
    force_list = list(config.get("force_close_structures", []))
    force_list.extend(config.get("account2", {}).get("force_close_structures", []))
    if (structure.structure_id in force_list
            or structure.underlying in force_list
            or any(structure.structure_id.startswith(fid) for fid in force_list if fid)):
        return True, "manual_close"

    # ── Derived values reused across layers ──
    dte         = _compute_dte(structure)
    max_loss    = _max_loss_usd_estimate(structure)
    max_profit  = structure.max_profit_usd
    pnl         = structure.pnl_unrealized
    targets     = _resolve_close_targets(structure, config)

    # ── LAYER 3: TIME EXITS (highest priority) ──
    # L3a: DTE ≤ 2 (existing rule, preserved as expiry_approaching)
    if dte is not None and dte <= 2:
        return True, "expiry_approaching"
    # L3b: DTE ≤ 4 AND not profitable → close (salvage)
    if dte is not None and dte <= 4 and pnl is not None and pnl < 0:
        return True, "loss_cut_near_expiry"
    # L3c: DTE ≤ 6 AND loss ≥ 25% of max_loss → close (preserve capital)
    if (dte is not None and dte <= 6
            and pnl is not None and max_loss
            and pnl <= -(max_loss * 0.25)):
        return True, "time_stop_loss_near_expiry"

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

    # ── LAYER 1: PROFIT EXITS ──
    # L1d: profit-lock retrace — peak ≥ 60% gain that has retraced to 30% locks
    # in partial gain rather than giving back the move.
    if (max_profit and pnl is not None
            and structure.peak_pnl is not None
            and structure.peak_pnl >= max_profit * 0.60
            and pnl <= max_profit * 0.30):
        return True, "profit_lock_retrace"
    # L1b: 50% profit AND DTE ≤ 6 → take it before theta accelerates
    if (max_profit and pnl is not None and dte is not None
            and dte <= 6 and pnl >= max_profit * 0.50):
        return True, "profit_target_50_near_expiry"
    # L1c: IV expansion ≥ 20% from entry AND ≥ 40% of max_profit → vega tailwind
    # banked before mean reversion.  Skips silently if either IV reading absent.
    if (max_profit and pnl is not None and pnl >= max_profit * 0.40
            and structure.iv_rank is not None and structure.iv_rank > 0):
        _cur_iv = _current_iv_rank(structure.underlying)
        if _cur_iv is not None and _cur_iv >= structure.iv_rank * 1.20:
            return True, "iv_expansion_take_profit"

    # Rules 5 & 6: P&L check using current_prices
    net_debit  = structure.net_debit_per_contract()

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

    # ── LAYER 2c: THETA-BURN UNDERWATER ──
    # If theta is consuming more than $50/day AND position is ≥ 20% underwater
    # of max_loss, the daily decay alone outweighs reasonable recovery odds.
    if (structure.theta is not None and pnl is not None and max_loss):
        _daily_theta_cost = abs(float(structure.theta)) * 100 * structure.contracts
        if _daily_theta_cost > 50.0 and pnl <= -(max_loss * 0.20):
            return True, "theta_burn_underwater"

    # Rule 5c: pnl_unrealized-based stop/target — fires when current_prices unavailable
    # (reconciliation always passes {}) or when net_debit is zero (equal-fill spreads).
    # Uses per-structure overrides via _resolve_close_targets so orphan_tracked
    # structures can carry tighter exit thresholds.
    _max_loss_pct = float(targets["max_loss_pct"])
    _profit_tgt   = float(targets["profit_target_pct"])
    if structure.pnl_unrealized is not None:
        _buy_cost = sum(
            float(leg.filled_price) * structure.contracts * 100
            for leg in structure.legs
            if leg.side == "buy" and leg.filled_price is not None
        )
        if _buy_cost > 0 and structure.pnl_unrealized <= -(_buy_cost * _max_loss_pct):
            return True, "max_loss_exit"
        if max_profit and structure.pnl_unrealized >= (max_profit * _profit_tgt):
            return True, "profit_target_pct_hit"

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
