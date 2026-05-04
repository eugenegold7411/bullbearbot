"""
tests/test_short_selling_execution.py — Session 2 of 3: short selling execution wiring.

Covers:
  - _submit_short(): market SELL entry + protective BUY stop
  - _submit_cover(): market BUY to close short
  - execute_all() dispatch for short_sell and cover actions
"""
import sys
import types
from unittest.mock import MagicMock, patch

# ── stubs ──────────────────────────────────────────────────────────────────────

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
        "GetOrdersRequest",
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


_ensure_stubs()


# ── helpers ────────────────────────────────────────────────────────────────────

def _filled_order(order_id="short-order-id", fill_price=100.0, fill_qty=10):
    o = MagicMock()
    o.id = order_id
    o.filled_avg_price = str(fill_price)
    o.filled_qty = str(fill_qty)
    o.filled_at = "2026-05-04T14:00:00Z"
    return o


def _short_action(symbol="NVDA", qty=10, stop_loss=103.50):
    return {"symbol": symbol, "qty": qty, "stop_loss": stop_loss, "action": "short_sell"}


def _cover_action(symbol="NVDA", qty=10):
    return {"symbol": symbol, "qty": qty, "action": "cover"}


def _make_client(side_effects):
    client = MagicMock()
    client.submit_order.side_effect = side_effects
    client.get_orders.return_value = []
    return client


# ── Test 1: _submit_short places a SELL market order ──────────────────────────

def test_submit_short_submits_sell_order():
    """_submit_short() must submit a market SELL as the first order."""
    _ensure_stubs()
    from alpaca.trading.enums import OrderSide

    import order_executor as oe

    fill = _filled_order()
    stop = MagicMock(id="stop-order-id")
    client = _make_client([fill, stop])

    with patch("order_executor._get_alpaca", return_value=client), \
         patch("time.sleep"):
        oe._submit_short(_short_action())

    first_req = client.submit_order.call_args_list[0].args[0]
    assert getattr(first_req, "side", None) == OrderSide.SELL, (
        f"Expected first submit_order side=SELL, got {getattr(first_req, 'side', None)!r}"
    )
    assert getattr(first_req, "qty", None) == 10


# ── Test 2: _submit_short places a protective BUY stop ────────────────────────

def test_submit_short_places_buy_stop():
    """After a short fill, _submit_short() must place a protective BUY stop at stop_loss price."""
    _ensure_stubs()
    from alpaca.trading.enums import OrderSide

    import order_executor as oe

    fill = _filled_order(fill_price=100.0, fill_qty=10)
    stop = MagicMock(id="stop-order-id")
    client = _make_client([fill, stop])

    with patch("order_executor._get_alpaca", return_value=client), \
         patch("time.sleep"):
        oe._submit_short(_short_action(stop_loss=103.50))

    assert client.submit_order.call_count == 2, (
        f"Expected 2 submit_order calls (entry + stop), got {client.submit_order.call_count}"
    )
    stop_req = client.submit_order.call_args_list[1].args[0]
    assert getattr(stop_req, "side", None) == OrderSide.BUY, (
        f"Expected protective stop side=BUY, got {getattr(stop_req, 'side', None)!r}"
    )
    placed_price = getattr(stop_req, "stop_price", None)
    assert placed_price is not None and abs(placed_price - 103.50) < 0.01, (
        f"Expected stop_price=103.50, got {placed_price!r}"
    )


# ── Test 3: _submit_cover submits a BUY market order ─────────────────────────

def test_submit_cover_submits_buy_order():
    """_submit_cover() must submit a market BUY order."""
    _ensure_stubs()
    from alpaca.trading.enums import OrderSide

    import order_executor as oe

    fill = _filled_order(order_id="cover-order-id", fill_price=95.0)
    client = _make_client([fill])

    with patch("order_executor._get_alpaca", return_value=client), \
         patch("time.sleep"):
        oe._submit_cover(_cover_action())

    assert client.submit_order.call_count == 1
    req = client.submit_order.call_args_list[0].args[0]
    assert getattr(req, "side", None) == OrderSide.BUY, (
        f"Expected cover side=BUY, got {getattr(req, 'side', None)!r}"
    )
    assert getattr(req, "qty", None) == 10


# ── Test 4: execute_all routes short_sell to _submit_short ───────────────────

def test_execute_all_routes_short_sell():
    """execute_all() with action='short_sell' must result in a SELL market order."""
    _ensure_stubs()
    from alpaca.trading.enums import OrderSide

    import order_executor as oe

    fill = _filled_order()
    stop = MagicMock(id="stop-order-id")
    client = _make_client([fill, stop])

    account = MagicMock()
    account.equity = "100000"
    account.buying_power = "100000"

    action = {
        "symbol": "NVDA", "qty": 5, "stop_loss": 103.50,
        "action": "short_sell", "tier": "core",
        "catalyst": "test", "confidence": "medium",
    }

    with patch("order_executor._get_alpaca", return_value=client), \
         patch("order_executor.log_trade"), \
         patch("order_executor._check_pending_fills"), \
         patch("time.sleep"):
        results = oe.execute_all(
            actions=[action],
            account=account,
            positions=[],
            market_status="open",
            minutes_since_open=30,
            session_tier="market",
        )

    assert results, "execute_all() returned no results"
    assert results[0].status != "rejected", (
        f"short_sell was rejected: {results[0].reason}"
    )
    submitted_sides = [
        getattr(c.args[0], "side", None)
        for c in client.submit_order.call_args_list
    ]
    assert OrderSide.SELL in submitted_sides, (
        f"Expected a SELL order in submitted calls, got sides: {submitted_sides}"
    )


# ── Test 5: execute_all routes cover to _submit_cover ────────────────────────

def test_execute_all_routes_cover():
    """execute_all() with action='cover' must result in a BUY market order."""
    _ensure_stubs()
    from alpaca.trading.enums import OrderSide

    import order_executor as oe

    fill = _filled_order(order_id="cover-id", fill_price=95.0)
    client = _make_client([fill])

    account = MagicMock()
    account.equity = "100000"
    account.buying_power = "100000"

    action = {
        "symbol": "NVDA", "qty": 5,
        "action": "cover", "tier": "core",
        "catalyst": "test", "confidence": "medium",
    }

    with patch("order_executor._get_alpaca", return_value=client), \
         patch("order_executor.log_trade"), \
         patch("order_executor._check_pending_fills"), \
         patch("time.sleep"):
        results = oe.execute_all(
            actions=[action],
            account=account,
            positions=[],
            market_status="open",
            minutes_since_open=30,
            session_tier="market",
        )

    assert results, "execute_all() returned no results"
    assert results[0].status != "rejected", (
        f"cover was rejected: {results[0].reason}"
    )
    submitted_sides = [
        getattr(c.args[0], "side", None)
        for c in client.submit_order.call_args_list
    ]
    assert OrderSide.BUY in submitted_sides, (
        f"Expected a BUY order in submitted calls, got sides: {submitted_sides}"
    )
