"""
tests/test_short_selling_execution.py — Session 2 of 3: short selling execution wiring.

Covers:
  - _submit_short(): market SELL entry + OCO (stop+TP) when take_profit provided
  - _submit_short(): standalone BUY stop when take_profit absent
  - _submit_short(): OCO fallback to standalone stop on broker failure
  - _submit_short(): position_targets.json written for short positions
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
        "PositionIntent":   {"BUY_TO_CLOSE": "buy_to_close", "SELL_TO_CLOSE": "sell_to_close"},
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


def _short_action(symbol="NVDA", qty=10, stop_loss=103.50, take_profit=None):
    a = {"symbol": symbol, "qty": qty, "stop_loss": stop_loss, "action": "short_sell"}
    if take_profit is not None:
        a["take_profit"] = take_profit
    return a


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


# ── Test 6: _submit_short places OCO when take_profit provided ────────────────

def test_submit_short_places_oco_with_take_profit(tmp_path, monkeypatch):
    """_submit_short() with take_profit in action → OCO (not standalone stop) is placed."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "runtime").mkdir(parents=True)

    _ensure_stubs()
    from alpaca.trading.enums import OrderClass, OrderSide

    import order_executor as oe

    fill     = _filled_order(fill_price=100.0, fill_qty=10)
    oco_ord  = MagicMock(id="oco-order-id")
    client   = _make_client([fill, oco_ord])

    with patch("order_executor._get_alpaca", return_value=client), \
         patch("order_executor.log_trade"), \
         patch("time.sleep"):
        oe._submit_short(_short_action(stop_loss=103.50, take_profit=93.50))

    assert client.submit_order.call_count == 2, (
        f"Expected 2 calls (entry + OCO), got {client.submit_order.call_count}"
    )
    second_req = client.submit_order.call_args_list[1].args[0]
    assert getattr(second_req, "side", None) == OrderSide.BUY
    assert getattr(second_req, "order_class", None) == OrderClass.OCO


# ── Test 7: _submit_short OCO fallback to standalone stop on failure ──────────

def test_submit_short_oco_fallback_on_failure(tmp_path, monkeypatch):
    """When OCO submission fails, _submit_short falls back to standalone protective stop."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "runtime").mkdir(parents=True)

    _ensure_stubs()
    from alpaca.trading.enums import OrderSide

    import order_executor as oe

    fill     = _filled_order(fill_price=100.0, fill_qty=10)
    stop_ord = MagicMock(id="stop-fallback-id")

    def _side_effect(req):
        if getattr(req, "order_class", None) is not None:
            raise RuntimeError("OCO not supported")
        return stop_ord if hasattr(req, "stop_price") else fill

    client = MagicMock()
    client.submit_order.side_effect = [fill, RuntimeError("OCO not supported"), stop_ord]
    client.get_orders.return_value = []

    with patch("order_executor._get_alpaca", return_value=client), \
         patch("order_executor.log_trade"), \
         patch("time.sleep"):
        oe._submit_short(_short_action(stop_loss=103.50, take_profit=93.50))

    assert client.submit_order.call_count == 3, (
        f"Expected 3 calls (entry + OCO fail + standalone stop), got {client.submit_order.call_count}"
    )
    stop_req = client.submit_order.call_args_list[2].args[0]
    assert getattr(stop_req, "side", None) == OrderSide.BUY
    placed_price = getattr(stop_req, "stop_price", None)
    assert placed_price is not None and abs(placed_price - 103.50) < 0.01


# ── Test 8: TP limit price is below entry price ───────────────────────────────

def test_submit_short_tp_price_below_entry(tmp_path, monkeypatch):
    """The TP limit price placed in the OCO must be below the fill price (profit target for short)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "runtime").mkdir(parents=True)

    _ensure_stubs()

    import order_executor as oe

    fill_price = 100.0
    fill    = _filled_order(fill_price=fill_price, fill_qty=10)
    oco_ord = MagicMock(id="oco-id")
    client  = _make_client([fill, oco_ord])

    with patch("order_executor._get_alpaca", return_value=client), \
         patch("order_executor.log_trade"), \
         patch("time.sleep"):
        oe._submit_short(_short_action(stop_loss=103.50, take_profit=93.50))

    oco_req  = client.submit_order.call_args_list[1].args[0]
    tp_req   = getattr(oco_req, "take_profit", None)
    tp_price = getattr(tp_req, "limit_price", None) if tp_req else None
    assert tp_price is not None and tp_price < fill_price, (
        f"Expected TP limit price < entry {fill_price}, got {tp_price}"
    )


# ── Test 9: position_targets.json written for short entry ────────────────────

def test_submit_short_writes_position_targets(tmp_path, monkeypatch):
    """_submit_short() with take_profit writes position_targets.json with side='short'."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "runtime").mkdir(parents=True)

    _ensure_stubs()

    import json

    import order_executor as oe

    fill    = _filled_order(fill_price=100.0, fill_qty=10)
    oco_ord = MagicMock(id="oco-id")
    client  = _make_client([fill, oco_ord])

    with patch("order_executor._get_alpaca", return_value=client), \
         patch("order_executor.log_trade"), \
         patch("time.sleep"):
        oe._submit_short(_short_action(symbol="NVDA", stop_loss=103.50, take_profit=93.50))

    targets_file = tmp_path / "data" / "runtime" / "position_targets.json"
    assert targets_file.exists(), "position_targets.json was not written"
    data = json.loads(targets_file.read_text())
    assert "NVDA" in data
    entry = data["NVDA"]
    assert entry["side"] == "short"
    assert abs(entry["take_profit"] - 93.50) < 0.01
    assert abs(entry["stop_loss"] - 103.50) < 0.01
    assert entry["take_profit"] < entry["stop_loss"], (
        "Short TP must be below stop_loss"
    )


# ── Test 10: _submit_short without take_profit → stop-only (no OCO) ──────────

def test_submit_short_stop_only_when_no_take_profit():
    """_submit_short() without take_profit in action → standalone stop, no OCO, call_count=2."""
    _ensure_stubs()
    from alpaca.trading.enums import OrderSide

    import order_executor as oe

    fill     = _filled_order(fill_price=100.0, fill_qty=10)
    stop_ord = MagicMock(id="stop-only-id")
    client   = _make_client([fill, stop_ord])

    with patch("order_executor._get_alpaca", return_value=client), \
         patch("time.sleep"):
        oe._submit_short(_short_action(stop_loss=103.50))  # no take_profit

    assert client.submit_order.call_count == 2, (
        f"Expected 2 calls (entry + stop), got {client.submit_order.call_count}"
    )
    stop_req = client.submit_order.call_args_list[1].args[0]
    assert getattr(stop_req, "side", None) == OrderSide.BUY
    assert getattr(stop_req, "stop_price", None) is not None
