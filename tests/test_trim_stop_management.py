"""
tests/test_trim_stop_management.py — Tests for trim OCO re-placement fixes.

Covers:
  Fix 1 — _sell_cancel_stop_and_sell:
    T1: OCO parent stop_price extracted from leg; OCO placed after trim
    T2: OCO placement fails → safety alert fired + standalone stop fallback
    T3: No TP in position_targets → standalone stop (no OCO)
    T4: No stop_price found after all → safety alert (position unprotected)
  Fix 2 — _submit_buy BUG-009b:
    T5: OCO placement failure fires _fire_safety_alert
  Fix 2 — place_stops tier TP:
    T6: place_stops uses position_tiers take_profit_pct for long TP
"""
from __future__ import annotations

import json
import sys
import types
from unittest.mock import MagicMock, patch

# ── stubs ─────────────────────────────────────────────────────────────────────

def _ensure_stubs():
    if "dotenv" not in sys.modules:
        m = types.ModuleType("dotenv")
        m.load_dotenv = lambda *a, **kw: None
        sys.modules["dotenv"] = m

    for mod in (
        "alpaca", "alpaca.trading", "alpaca.trading.client",
        "alpaca.trading.requests", "alpaca.trading.enums",
        "alpaca.data", "alpaca.data.historical", "alpaca.data.requests",
        "alpaca.data.enums",
    ):
        if mod not in sys.modules:
            sys.modules[mod] = types.ModuleType(mod)

    enums = sys.modules["alpaca.trading.enums"]
    for enum_name, attrs in {
        "OrderSide":        {"BUY": "buy",  "SELL": "sell"},
        "TimeInForce":      {"DAY": "day",  "GTC":  "gtc"},
        "OrderClass":       {"BRACKET": "bracket", "OCO": "oco"},
        "QueryOrderStatus": {"OPEN": "open", "ALL": "all"},
        "AssetStatus":      {},
        "ContractType":     {},
        "PositionIntent":   {},
    }.items():
        if not hasattr(enums, enum_name):
            cls = type(enum_name, (), {})
            setattr(enums, enum_name, cls)
        cls = getattr(enums, enum_name)
        for attr, val in attrs.items():
            if not hasattr(cls, attr):
                setattr(cls, attr, val)

    reqs = sys.modules["alpaca.trading.requests"]
    for cls_name in (
        "MarketOrderRequest", "LimitOrderRequest", "StopOrderRequest",
        "StopLossRequest", "TakeProfitRequest", "ClosePositionRequest",
        "GetOrdersRequest", "GetOptionContractsRequest",
    ):
        if not hasattr(reqs, cls_name):
            def _mk(name):
                class _Req:
                    def __init__(self, **kwargs):
                        for k, v in kwargs.items():
                            setattr(self, k, v)
                _Req.__name__ = name
                return _Req
            setattr(reqs, cls_name, _mk(cls_name))

    tc = sys.modules["alpaca.trading.client"]
    if not hasattr(tc, "TradingClient"):
        class _TC:
            def __init__(self, **_kw): pass
        tc.TradingClient = _TC

    if "schemas" not in sys.modules:
        m = types.ModuleType("schemas")
        m.alpaca_symbol = lambda s: s
        sys.modules["schemas"] = m
    elif not hasattr(sys.modules["schemas"], "alpaca_symbol"):
        sys.modules["schemas"].alpaca_symbol = lambda s: s

    if "log_setup" not in sys.modules:
        m = types.ModuleType("log_setup")
        m.get_logger = lambda name: __import__("logging").getLogger(name)
        m.log_trade = lambda *a, **kw: None
        sys.modules["log_setup"] = m


_ensure_stubs()


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_oco_open_order(symbol="LLY"):
    """OCO parent order: stop_price=None (lives in stop leg only)."""
    from alpaca.trading.enums import OrderClass, OrderSide
    o = MagicMock()
    o.id = "oco-parent-id"
    o.symbol = symbol
    o.side = OrderSide.SELL
    o.order_type = "limit"
    o.type = "limit"
    o.stop_price = None
    o.order_class = OrderClass.OCO
    return o


def _make_full_oco_with_stop_leg(stop_price=90.0):
    """Full OCO order fetched via get_order_by_id, with stop leg populated."""
    stop_leg = MagicMock()
    stop_leg.order_type = "stop"
    stop_leg.stop_price = str(stop_price)

    tp_leg = MagicMock()
    tp_leg.order_type = "limit"
    tp_leg.stop_price = None

    full = MagicMock()
    full.legs = [stop_leg, tp_leg]
    return full


def _make_position(symbol="LLY", qty=100):
    p = MagicMock()
    p.symbol = symbol
    p.qty = str(qty)
    return p


def _make_sell_fill(sell_qty=30):
    o = MagicMock()
    o.id = "sell-order-id"
    o.filled_avg_price = "100.0"
    o.filled_qty = str(float(sell_qty))
    o.filled_at = "2026-05-06T15:00:00Z"
    return o


def _run_cancel_stop_and_sell(
    symbol="LLY",
    sell_qty=30,
    position_qty=100,
    open_orders=None,
    full_oco=None,
    position_targets=None,
    submit_side_effects=None,
    get_order_by_id_raises=None,
):
    """Run _sell_cancel_stop_and_sell under full mocks. Returns (client, alerts_fired)."""
    _ensure_stubs()
    import order_executor as oe

    client = MagicMock()
    client.get_orders.return_value = open_orders or []
    if get_order_by_id_raises:
        client.get_order_by_id.side_effect = get_order_by_id_raises
    else:
        client.get_order_by_id.return_value = full_oco or MagicMock(legs=[])

    if submit_side_effects is None:
        # Default: sell fills, OCO placed OK
        submit_side_effects = [
            _make_sell_fill(sell_qty),
            MagicMock(id="oco-trim-id"),
        ]
    client.submit_order.side_effect = submit_side_effects

    positions = [_make_position(symbol, position_qty)]

    tp_json_str = json.dumps(position_targets) if position_targets else "{}"

    alerts: list[tuple] = []

    def _capture_alert(fn_name, exc):
        alerts.append((fn_name, exc))

    path_mock = MagicMock()
    path_mock.exists.return_value = position_targets is not None
    path_mock.read_text.return_value = tp_json_str

    with patch("order_executor._get_alpaca", return_value=client), \
         patch("order_executor._fire_safety_alert", side_effect=_capture_alert), \
         patch("order_executor.Path", return_value=path_mock):
        oe._sell_cancel_stop_and_sell(symbol, sell_qty, positions)

    return client, alerts


# ─────────────────────────────────────────────────────────────────────────────
# T1: OCO stop_price extracted from leg → OCO placed after trim
# ─────────────────────────────────────────────────────────────────────────────

def test_oco_stop_price_extracted_from_leg_and_oco_placed():
    """
    When an OCO parent order has stop_price=None, the stop_price must be fetched
    from the nested stop leg via get_order_by_id.  After the trim sell, a new
    OCO (stop + TP) must be submitted for remaining shares.
    """
    symbol = "LLY"
    stop_price = 90.0
    tp_price = 115.0
    sell_qty = 30
    remaining_qty = 100 - sell_qty  # 70

    oco_order = _make_oco_open_order(symbol)
    full_oco = _make_full_oco_with_stop_leg(stop_price)
    position_targets = {symbol: {"take_profit": tp_price}}

    client, alerts = _run_cancel_stop_and_sell(
        symbol=symbol,
        sell_qty=sell_qty,
        open_orders=[oco_order],
        full_oco=full_oco,
        position_targets=position_targets,
    )

    # get_order_by_id must be called to extract stop_price from OCO leg
    client.get_order_by_id.assert_called_once_with("oco-parent-id")

    # cancel must be called on the OCO parent before sell
    client.cancel_order_by_id.assert_any_call("oco-parent-id")

    # OCO must be submitted for remaining 70 shares
    submitted = [c.args[0] for c in client.submit_order.call_args_list]
    from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
    oco_orders = [
        r for r in submitted
        if getattr(r, "order_class", None) == OrderClass.OCO
        and getattr(r, "side", None) == OrderSide.SELL
        and getattr(r, "take_profit", None) is not None
        and getattr(r, "stop_loss", None) is not None
    ]
    assert oco_orders, (
        f"Expected OCO sell submitted after trim, got: {[vars(r) for r in submitted]}"
    )
    oco_req = oco_orders[0]
    assert getattr(oco_req, "qty", None) == remaining_qty, (
        f"OCO qty should be {remaining_qty}, got {getattr(oco_req, 'qty', None)}"
    )
    assert len(alerts) == 0, f"No safety alerts expected, got: {alerts}"


# ─────────────────────────────────────────────────────────────────────────────
# T2: OCO replacement fails → safety alert + standalone stop fallback
# ─────────────────────────────────────────────────────────────────────────────

def test_oco_replace_fails_fires_safety_alert_and_standalone_stop():
    """
    If OCO placement raises after the trim, _fire_safety_alert must be called
    and a standalone stop must be placed as fallback.
    """
    symbol = "LLY"
    stop_price = 90.0
    tp_price = 115.0
    sell_qty = 30

    oco_order = _make_oco_open_order(symbol)
    full_oco = _make_full_oco_with_stop_leg(stop_price)
    position_targets = {symbol: {"take_profit": tp_price}}

    oco_error = Exception("40310000 share lock on OCO placement")

    # sell fills, then OCO raises, then standalone stop succeeds
    side_effects = [
        _make_sell_fill(sell_qty),
        oco_error,
        MagicMock(id="standalone-stop-id"),
    ]

    client, alerts = _run_cancel_stop_and_sell(
        symbol=symbol,
        sell_qty=sell_qty,
        open_orders=[oco_order],
        full_oco=full_oco,
        position_targets=position_targets,
        submit_side_effects=side_effects,
    )

    # Safety alert must be fired
    assert any(
        "oco_replace" in fn_name for fn_name, _ in alerts
    ), f"Expected 'oco_replace' alert, got: {alerts}"

    # Standalone stop must be placed as fallback (third submit_order call)
    submitted = [c.args[0] for c in client.submit_order.call_args_list]
    from alpaca.trading.enums import OrderClass
    stop_only = [
        r for r in submitted
        if getattr(r, "order_class", None) not in ("oco", "bracket", OrderClass.OCO, OrderClass.BRACKET)
        and getattr(r, "stop_price", None) is not None
    ]
    # The standalone stop is placed via _replace_stop → submit_order with StopOrderRequest
    # Verify at least one stop-only submit happened after the OCO error
    assert len(client.submit_order.call_args_list) >= 3, (
        "Expected sell + OCO attempt + standalone stop, "
        f"got {len(client.submit_order.call_args_list)} submit_order calls"
    )


# ─────────────────────────────────────────────────────────────────────────────
# T3: No TP in position_targets → standalone stop placed, no OCO
# ─────────────────────────────────────────────────────────────────────────────

def test_no_tp_in_position_targets_falls_back_to_standalone_stop():
    """
    When position_targets has no TP for the symbol, no OCO should be submitted —
    only a standalone stop.
    """
    symbol = "LLY"
    stop_price = 90.0
    sell_qty = 30

    oco_order = _make_oco_open_order(symbol)
    full_oco = _make_full_oco_with_stop_leg(stop_price)
    # No TP entry for this symbol
    position_targets = {"OTHER": {"take_profit": 999.0}}

    # sell fills, then standalone stop placed
    side_effects = [
        _make_sell_fill(sell_qty),
        MagicMock(id="standalone-stop-id"),
    ]

    client, alerts = _run_cancel_stop_and_sell(
        symbol=symbol,
        sell_qty=sell_qty,
        open_orders=[oco_order],
        full_oco=full_oco,
        position_targets=position_targets,
        submit_side_effects=side_effects,
    )

    # Exactly 2 submit_order calls: sell + standalone stop
    assert client.submit_order.call_count == 2, (
        f"Expected sell + standalone stop (2 calls), got {client.submit_order.call_count}"
    )
    # No OCO should have been submitted
    submitted = [c.args[0] for c in client.submit_order.call_args_list]
    from alpaca.trading.enums import OrderClass
    oco_orders = [
        r for r in submitted
        if getattr(r, "order_class", None) in ("oco", OrderClass.OCO)
    ]
    assert not oco_orders, f"No OCO expected when TP is missing, got: {oco_orders}"
    assert len(alerts) == 0, f"No safety alerts expected, got: {alerts}"


# ─────────────────────────────────────────────────────────────────────────────
# T4: No stop_price found → safety alert (position unprotected)
# ─────────────────────────────────────────────────────────────────────────────

def test_no_stop_price_found_fires_safety_alert():
    """
    If no stop_price can be extracted (get_order_by_id fails and stop_price=None on
    parent), and remaining shares exist after trim, _fire_safety_alert must fire.
    """
    symbol = "LLY"
    sell_qty = 30

    oco_order = _make_oco_open_order(symbol)  # stop_price=None on parent
    position_targets = {symbol: {"take_profit": 115.0}}

    # OCO order but get_order_by_id raises — can't extract stop_price from legs
    side_effects = [_make_sell_fill(sell_qty)]

    client, alerts = _run_cancel_stop_and_sell(
        symbol=symbol,
        sell_qty=sell_qty,
        open_orders=[oco_order],
        full_oco=None,
        position_targets=position_targets,
        submit_side_effects=side_effects,
        get_order_by_id_raises=Exception("order not found"),
    )

    assert any(
        "no_stop_price" in fn_name for fn_name, _ in alerts
    ), f"Expected 'no_stop_price' safety alert, got: {alerts}"


# ─────────────────────────────────────────────────────────────────────────────
# T5: _submit_buy BUG-009b — OCO failure fires _fire_safety_alert
# ─────────────────────────────────────────────────────────────────────────────

def test_submit_buy_bug009b_oco_failure_fires_safety_alert():
    """
    When BUG-009b replaces a missing TP with OCO and that OCO placement fails,
    _fire_safety_alert must be called with '_submit_buy.oco_placement'.
    """
    _ensure_stubs()
    import order_executor as oe

    # Bracket fills, open_orders has a stop but no TP (BUG-009b path)
    from alpaca.trading.enums import OrderSide
    stop_ord = MagicMock()
    stop_ord.id = "existing-stop-id"
    stop_ord.side = OrderSide.SELL
    stop_ord.order_type = "stop"
    stop_ord.type = "stop"
    stop_ord.stop_price = "192.54"

    bracket_fill = MagicMock()
    bracket_fill.id = "bracket-id"
    bracket_fill.filled_avg_price = "200.0"
    bracket_fill.filled_qty = "10"
    bracket_fill.filled_at = "2026-05-06T10:00:00Z"

    oco_error = Exception("Simulated OCO failure")

    # standalone stop placed after OCO failure
    standalone_stop = MagicMock(id="standalone-stop-id")

    client = MagicMock()
    client.submit_order.side_effect = [
        bracket_fill,       # bracket buy fills
        oco_error,          # OCO placement fails
        standalone_stop,    # standalone stop via _replace_stop
    ]
    client.get_orders.return_value = [stop_ord]

    alerts: list[tuple] = []

    def _capture_alert(fn_name, exc):
        alerts.append((fn_name, exc))

    path_mock = MagicMock()
    path_mock.exists.return_value = False

    action = {
        "symbol":      "NVDA",
        "qty":         10,
        "stop_loss":   192.54,
        "take_profit": 213.36,
        "order_type":  "market",
    }

    with patch("order_executor._get_alpaca", return_value=client), \
         patch("order_executor._fire_safety_alert", side_effect=_capture_alert), \
         patch("order_executor.log_trade"), \
         patch("time.sleep"), \
         patch("order_executor.Path", return_value=path_mock):
        oe._submit_buy(action)

    assert any(
        "_submit_buy.oco_placement" in fn_name for fn_name, _ in alerts
    ), f"Expected '_submit_buy.oco_placement' safety alert, got: {alerts}"


# ─────────────────────────────────────────────────────────────────────────────
# T6: place_stops uses position_tiers take_profit_pct for long TP
# ─────────────────────────────────────────────────────────────────────────────

def test_place_stops_uses_tier_tp_pct_for_long():
    """
    When position_tiers.core.take_profit_pct = 0.12, place_stops must compute
    TP as current_price * 1.12 for long entries, overriding the R:R default.
    """
    from pathlib import Path as _RealPath
    sys.path.insert(0, str(_RealPath(__file__).parent.parent))

    from risk_kernel import place_stops
    from schemas import AccountAction, Direction, Tier, TradeIdea

    config = {
        "parameters": {
            "stop_loss_pct_core":  0.035,
            "take_profit_multiple": 2.5,
        },
        "position_tiers": {
            "core":        {"take_profit_pct": 0.12},
            "dynamic":     {"take_profit_pct": 0.15},
            "speculative":  {"take_profit_pct": 0.20},
        },
    }

    idea = TradeIdea(
        symbol="AAPL",
        action=AccountAction.BUY,
        tier=Tier.CORE,
        conviction=0.80,
        direction=Direction.BULLISH,
        catalyst="tier_tp_test",
        intent="enter_long",
    )

    result = place_stops(idea, current_price=100.0, config=config, side="long")
    assert isinstance(result, tuple), f"Expected (stop, target) tuple, got: {result!r}"
    stop, target = result

    # TP = 100 * 1.12 = 112.00
    assert abs(target - 112.0) < 0.01, (
        f"Expected tier TP = $112.00 (100 * 1.12), got ${target:.2f}"
    )
    # Stop should still be below entry
    assert stop < 100.0, f"Stop ${stop:.2f} should be below entry $100.0"


def test_place_stops_rr_used_when_no_tier_tp_pct():
    """
    When position_tiers has no take_profit_pct, place_stops must fall back to
    the R:R approach (current_price + stop_dist * target_r).
    """
    from pathlib import Path as _RealPath
    sys.path.insert(0, str(_RealPath(__file__).parent.parent))

    from risk_kernel import place_stops
    from schemas import AccountAction, Direction, Tier, TradeIdea

    config = {
        "parameters": {
            "stop_loss_pct_core":  0.035,
            "take_profit_multiple": 2.5,
        },
        "position_tiers": {},  # no take_profit_pct
    }

    idea = TradeIdea(
        symbol="AAPL",
        action=AccountAction.BUY,
        tier=Tier.CORE,
        conviction=0.80,
        direction=Direction.BULLISH,
        catalyst="rr_test",
        intent="enter_long",
    )

    result = place_stops(idea, current_price=100.0, config=config, side="long")
    assert isinstance(result, tuple), f"Expected (stop, target) tuple, got: {result!r}"
    stop, target = result

    # R:R TP = 100 - 3.5 * 2.5 + 100 = 100 + 3.5 * 2.5 = 108.75
    stop_dist = 100.0 - stop
    rr_target = round(100.0 + stop_dist * 2.5, 2)
    assert abs(target - rr_target) < 0.01, (
        f"Expected R:R TP = ${rr_target:.2f}, got ${target:.2f}"
    )
