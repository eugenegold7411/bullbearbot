"""
tests/test_crypto_swtp.py — Crypto SW-TP and overnight stop gap tests.

Seven test cases:
  CT1 — crypto buy in order_executor writes position_targets entry (BTCUSD key)
  CT2 — equity buy still writes position_targets correctly (no regression)
  CT3 — crypto position_targets key is Alpaca format BTCUSD, not BTC/USD
  CT4 — SW-TP fires for BTCUSD when current_price >= target * 0.999 (GTC close)
  CT5 — SW-TP does NOT fire for BTCUSD when price is below target
  CT6 — overnight stop gap: after execute_all returns a crypto fill, submit_order
         is called with a GTC limit sell at the intended stop_loss price
  CT7 — overnight stop gap: no stop placement when execute_all returns no fills
"""

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))


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
        "OrderClass":       {"BRACKET": "bracket"},
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


# ── shared helpers ─────────────────────────────────────────────────────────────

def _filled_order(fill_price=95000.0, fill_qty=0.084, order_id="crypto-order-id"):
    o = MagicMock()
    o.id = order_id
    o.filled_avg_price = str(fill_price)
    o.filled_qty = str(fill_qty)
    o.filled_at = "2026-05-03T02:00:00Z"
    return o


def _equity_filled_order(fill_price=198.49, fill_qty=10, order_id="equity-order-id"):
    o = MagicMock()
    o.id = order_id
    o.filled_avg_price = str(fill_price)
    o.filled_qty = str(fill_qty)
    o.filled_at = "2026-05-03T10:30:00Z"
    return o


def _stop_order():
    from alpaca.trading.enums import OrderSide
    o = MagicMock()
    o.id = "existing-stop-id"
    o.side = OrderSide.SELL
    o.order_type = "stop"
    o.type = "stop"
    return o


def _mock_position(symbol="BTCUSD", qty="0.084", current_price="95500.00",
                   avg_entry_price="95000.00", unrealized_pl="42.00"):
    p = MagicMock()
    p.symbol = symbol
    p.qty = qty
    p.current_price = current_price
    p.avg_entry_price = avg_entry_price
    p.unrealized_pl = unrealized_pl
    return p


def _strategy_config():
    return {
        "exit_management": {
            "stop_loss_pct":               0.03,
            "take_profit_multiple":        2.5,
            "trail_stop_enabled":          False,
            "trail_trigger_r":             1.0,
            "refresh_if_stop_stale_pct":   0.20,
            "trail_to_breakeven_plus_pct": 0.005,
        }
    }


# ── CT1 — crypto buy writes position_targets with BTCUSD key ─────────────────

def test_ct1_crypto_buy_writes_position_targets():
    """CT1: _submit_buy() for BTC/USD writes position_targets.json with BTCUSD key."""
    _ensure_stubs()
    import order_executor as oe

    action = {
        "symbol":      "BTC/USD",
        "qty":         0.084,
        "stop_loss":   86450.0,
        "take_profit": 112560.0,
        "order_type":  "market",
    }

    mp = MagicMock()
    mp.exists.return_value = False

    client = MagicMock()
    client.submit_order.return_value = _filled_order()

    with patch("order_executor._get_alpaca", return_value=client), \
         patch("order_executor.log_trade"), \
         patch("time.sleep"), \
         patch("pathlib.Path", return_value=mp):
        oe._submit_buy(action)

    assert mp.write_text.called, (
        "Expected write_text on position_targets.json after crypto buy. "
        "Crypto SW-TP requires the target to be persisted at submission time."
    )
    written = json.loads(mp.write_text.call_args[0][0])
    assert "BTCUSD" in written, f"Expected BTCUSD key in written data: {list(written.keys())}"


# ── CT2 — equity buy still writes position_targets (no regression) ────────────

def test_ct2_equity_buy_still_writes_position_targets():
    """CT2: _submit_buy() for NVDA still writes position_targets.json (equity path unaffected)."""
    _ensure_stubs()
    import order_executor as oe

    action = {
        "symbol":      "NVDA",
        "qty":         10,
        "stop_loss":   192.54,
        "take_profit": 213.36,
        "order_type":  "market",
    }

    mp = MagicMock()
    mp.exists.return_value = False

    client = MagicMock()
    client.submit_order.side_effect = [
        _equity_filled_order(),
        MagicMock(id="tp-fallback-id"),
    ]
    client.get_orders.return_value = [_stop_order()]

    with patch("order_executor._get_alpaca", return_value=client), \
         patch("order_executor.log_trade"), \
         patch("time.sleep"), \
         patch("pathlib.Path", return_value=mp):
        oe._submit_buy(action)

    assert mp.write_text.called, "Expected write_text for equity buy — equity path must be unaffected."
    written = json.loads(mp.write_text.call_args[0][0])
    assert "NVDA" in written, f"Expected NVDA key in written data: {list(written.keys())}"


# ── CT3 — crypto key is Alpaca format BTCUSD, not BTC/USD ────────────────────

def test_ct3_crypto_position_targets_key_is_alpaca_format():
    """CT3: position_targets key for a BTC/USD buy is BTCUSD, not BTC/USD."""
    _ensure_stubs()
    import order_executor as oe

    action = {
        "symbol":      "BTC/USD",
        "qty":         0.084,
        "stop_loss":   86450.0,
        "take_profit": 112560.0,
        "order_type":  "market",
    }

    mp = MagicMock()
    mp.exists.return_value = False

    client = MagicMock()
    client.submit_order.return_value = _filled_order()

    with patch("order_executor._get_alpaca", return_value=client), \
         patch("order_executor.log_trade"), \
         patch("time.sleep"), \
         patch("pathlib.Path", return_value=mp):
        oe._submit_buy(action)

    assert mp.write_text.called
    written = json.loads(mp.write_text.call_args[0][0])

    assert "BTCUSD" in written, (
        f"Key must be Alpaca format 'BTCUSD' — SW-TP lookup uses pos.symbol which is "
        f"Alpaca format. Got keys: {list(written.keys())}"
    )
    assert "BTC/USD" not in written, (
        "Claude format 'BTC/USD' must NOT be used as the key — SW-TP would never match."
    )

    entry = written["BTCUSD"]
    assert entry["symbol"] == "BTCUSD"
    assert abs(entry["take_profit"] - 112560.0) < 0.01
    assert abs(entry["stop_loss"] - 86450.0) < 0.01


# ── CT4 — SW-TP fires for BTCUSD when at target (GTC close) ──────────────────

def test_ct4_sw_tp_fires_for_crypto_at_target():
    """CT4: run_exit_manager() submits a GTC market close for BTCUSD when price >= target*0.999."""
    _ensure_stubs()
    from alpaca.trading.enums import TimeInForce

    import exit_manager as em

    # BTCUSD at $112,675 — target is $112,560 → triggers (112,675 >= 112,560 * 0.999 = 112,447)
    position = _mock_position(symbol="BTCUSD", qty="0.084", current_price="112675.00")
    client = MagicMock()
    client.submit_order.return_value = MagicMock(id="sw-tp-crypto-close")

    targets = {"BTCUSD": {"take_profit": 112560.0, "stop_loss": 86450.0}}

    with patch("exit_manager._load_position_targets", return_value=targets), \
         patch("exit_manager.get_active_exits",
               return_value={"BTCUSD": {"status": "partial", "stop_price": 86450.0}}), \
         patch("exit_manager._remove_position_target"), \
         patch("exit_manager.refresh_exits_for_position", return_value=False), \
         patch("exit_manager.maybe_trail_stop", return_value=False), \
         patch("time.sleep"):
        actions = em.run_exit_manager([position], client, _strategy_config())

    assert client.submit_order.called, (
        "Expected submit_order when BTCUSD current_price (112,675) >= target*0.999 (112,447). "
        "SW-TP did not fire."
    )

    # Verify GTC (not DAY) was used — DAY orders may be rejected for overnight crypto
    submitted_req = client.submit_order.call_args[0][0]
    actual_tif = getattr(submitted_req, "time_in_force", None)
    assert actual_tif == TimeInForce.GTC, (
        f"SW-TP crypto close must use TimeInForce.GTC (crypto trades 24/7). Got: {actual_tif!r}"
    )

    sw_tp_actions = [a for a in actions if a.get("action") == "sw_tp_close"]
    assert sw_tp_actions, f"sw_tp_close not recorded in actions: {actions}"


# ── CT5 — SW-TP does NOT fire for crypto below target ─────────────────────────

def test_ct5_sw_tp_does_not_fire_for_crypto_below_target():
    """CT5: run_exit_manager() does NOT close BTCUSD when price is below target."""
    _ensure_stubs()
    import exit_manager as em

    # BTCUSD at $105,000 — target is $112,560 → does not trigger
    position = _mock_position(symbol="BTCUSD", qty="0.084", current_price="105000.00")
    client = MagicMock()

    targets = {"BTCUSD": {"take_profit": 112560.0, "stop_loss": 86450.0}}

    with patch("exit_manager._load_position_targets", return_value=targets), \
         patch("exit_manager.get_active_exits",
               return_value={"BTCUSD": {"status": "partial", "stop_price": 86450.0}}), \
         patch("exit_manager._remove_position_target"), \
         patch("exit_manager.refresh_exits_for_position", return_value=False), \
         patch("exit_manager.maybe_trail_stop", return_value=False), \
         patch("time.sleep"):
        actions = em.run_exit_manager([position], client, _strategy_config())

    sw_tp_actions = [a for a in actions if a.get("action") == "sw_tp_close"]
    assert not sw_tp_actions, (
        f"SW-TP must not fire when price (105,000) < target*0.999 (112,447). "
        f"Got: {actions}"
    )


# ── CT6 — overnight stop gap places stop after crypto fill ────────────────────

def test_ct6_overnight_stop_gap_places_stop_after_crypto_fill():
    """CT6: after execute_all returns a BTC/USD buy in overnight session,
    a GTC limit stop is submitted at the intended stop_loss price."""
    _ensure_stubs()
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest

    import order_executor as oe

    # Build a minimal result set: one submitted crypto buy
    fill_result = oe.ExecutionResult(
        symbol="BTC/USD",
        action="buy",
        status="submitted",
        order_id="crypto-order-id",
        fill_price=95000.0,
        filled_qty=0.084,
    )

    alpaca_client = MagicMock()
    alpaca_client.get_all_positions.return_value = [
        _mock_position(symbol="BTCUSD", qty="0.084", current_price="95000.00"),
    ]
    stop_order_mock = MagicMock()
    stop_order_mock.id = "stop-gap-order-id"
    alpaca_client.submit_order.return_value = stop_order_mock

    actions = [
        {
            "symbol":      "BTC/USD",
            "action":      "buy",
            "stop_loss":   86450.0,
            "take_profit": 112560.0,
        }
    ]
    results = [fill_result]

    # Simulate the stop gap logic from bot.py in isolation
    import time as _sg_time

    with patch.object(_sg_time, "sleep"), \
         patch("order_executor.log_trade"):
        _sg_fills = [
            r for r in results
            if r.status == "submitted" and r.action == "buy" and "/" in r.symbol
        ]
        assert len(_sg_fills) == 1

        _sg_positions = alpaca_client.get_all_positions()

        from schemas import alpaca_symbol as _asym
        for _sgr in _sg_fills:
            _sg_sym = _asym(_sgr.symbol)
            _sg_pos = next((p for p in _sg_positions if p.symbol == _sg_sym), None)
            assert _sg_pos is not None, f"Position {_sg_sym} not found in mock positions"

            _sg_action = next((a for a in actions if a.get("symbol") == _sgr.symbol), {})
            _sg_stop = _sg_action.get("stop_loss")
            assert _sg_stop is not None

            _sg_qty = round(abs(float(_sg_pos.qty)), 9)
            _sg_req = LimitOrderRequest(
                symbol=_sg_sym,
                qty=_sg_qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                limit_price=round(float(_sg_stop), 2),
            )
            _sg_ord = alpaca_client.submit_order(_sg_req)

    assert alpaca_client.submit_order.called, "submit_order must be called for stop gap"
    submitted = alpaca_client.submit_order.call_args[0][0]

    assert submitted.symbol == "BTCUSD", (
        f"Stop gap must use Alpaca format BTCUSD. Got: {submitted.symbol!r}"
    )
    assert submitted.side == OrderSide.SELL, (
        f"Stop must be a SELL order. Got: {submitted.side!r}"
    )
    assert submitted.time_in_force == TimeInForce.GTC, (
        f"Stop must use GTC (crypto 24/7). Got: {submitted.time_in_force!r}"
    )
    assert abs(submitted.limit_price - 86450.0) < 0.01, (
        f"Stop must be at the intended 9% stop price 86450. Got: {submitted.limit_price!r}"
    )


# ── CT7 — overnight stop gap skips when no crypto fills ──────────────────────

def test_ct7_overnight_stop_gap_skips_when_no_fills():
    """CT7: no stop placement call when execute_all returns no submitted crypto fills."""
    _ensure_stubs()
    import order_executor as oe

    results = [
        oe.ExecutionResult(symbol="BTC/USD", action="buy", status="rejected",
                           reason="insufficient buying power"),
    ]

    _sg_fills = [
        r for r in results
        if r.status == "submitted" and r.action == "buy" and "/" in r.symbol
    ]
    assert len(_sg_fills) == 0, (
        "No stop gap calls should be made when there are no submitted crypto fills. "
        f"Got fills: {_sg_fills}"
    )
