"""
tests/test_swtp_shorts.py — SW-TP (software take-profit) behaviour for short positions.

Verifies:
  1. SW-TP fires for short when price drops to/below target
  2. SW-TP does NOT fire for short when price has not reached target
  3. SW-TP close for short submits BUY (not SELL)
  4. Existing long SW-TP behaviour is unaffected
"""
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
        "PositionIntent":   {"BUY_TO_CLOSE": "buy_to_close"},
        "AssetStatus":      {"ACTIVE": "active"},
        "ContractType":     {"CALL": "call", "PUT": "put"},
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


# ── helpers ───────────────────────────────────────────────────────────────────

def _short_position(sym="NVDA", qty=-10, current_price=90.0):
    p = MagicMock()
    p.symbol = sym
    p.qty = str(qty)
    p.current_price = str(current_price)
    p.avg_entry_price = "100.0"
    return p


def _long_position(sym="AAPL", qty=10, current_price=115.0):
    p = MagicMock()
    p.symbol = sym
    p.qty = str(qty)
    p.current_price = str(current_price)
    p.avg_entry_price = "100.0"
    return p


def _minimal_cfg():
    return {
        "exit_management": {
            "trail_stop_enabled": False,
            "max_stop_refreshes_per_cycle": 0,
        },
    }


def _alpaca_client():
    c = MagicMock()
    c.submit_order.return_value = MagicMock(id="sw-tp-order-id")
    return c


# ── Test 1: SW-TP fires for short when price has dropped to target ────────────

def test_swtp_fires_for_short_at_target(tmp_path, monkeypatch):
    """Short SW-TP fires when current_price <= take_profit (price has fallen to target)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "runtime").mkdir(parents=True)

    targets_file = tmp_path / "data" / "runtime" / "position_targets.json"
    targets_file.write_text(json.dumps({
        "NVDA": {
            "symbol":      "NVDA",
            "take_profit": 90.0,
            "stop_loss":   103.50,
            "side":        "short",
        }
    }))

    import exit_manager as em

    pos    = _short_position(sym="NVDA", qty=-10, current_price=90.0)  # exactly at target
    client = _alpaca_client()

    with patch.object(em, "get_active_exits", return_value={"NVDA": {"status": "partial"}}):
        actions = em.run_exit_manager([pos], client, _minimal_cfg())

    assert any(a["action"] == "sw_tp_close" and a["symbol"] == "NVDA" for a in actions), (
        f"Expected sw_tp_close for NVDA, got: {actions}"
    )
    close_req = client.submit_order.call_args_list[0].args[0]
    from alpaca.trading.enums import OrderSide
    assert getattr(close_req, "side", None) == OrderSide.BUY, (
        f"Short SW-TP close must be BUY (buy-to-cover), got {getattr(close_req, 'side', None)!r}"
    )


# ── Test 2: SW-TP does NOT fire for short when price has not reached target ───

def test_swtp_does_not_fire_for_short_above_target(tmp_path, monkeypatch):
    """Short SW-TP does NOT fire when current_price is still above take_profit."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "runtime").mkdir(parents=True)

    targets_file = tmp_path / "data" / "runtime" / "position_targets.json"
    targets_file.write_text(json.dumps({
        "NVDA": {
            "symbol":      "NVDA",
            "take_profit": 90.0,
            "stop_loss":   103.50,
            "side":        "short",
        }
    }))

    import exit_manager as em

    pos    = _short_position(sym="NVDA", qty=-10, current_price=95.0)  # above target — not triggered
    client = _alpaca_client()

    with patch.object(em, "get_active_exits", return_value={"NVDA": {"status": "partial"}}):
        actions = em.run_exit_manager([pos], client, _minimal_cfg())

    assert not any(a["action"] == "sw_tp_close" for a in actions), (
        f"Short SW-TP must NOT fire when price={95.0} > target={90.0}: {actions}"
    )
    client.submit_order.assert_not_called()


# ── Test 3: long SW-TP still uses SELL close ─────────────────────────────────

def test_swtp_long_uses_sell_close(tmp_path, monkeypatch):
    """Long SW-TP close must still use SELL (unchanged by short extension)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "runtime").mkdir(parents=True)

    targets_file = tmp_path / "data" / "runtime" / "position_targets.json"
    targets_file.write_text(json.dumps({
        "AAPL": {
            "symbol":      "AAPL",
            "take_profit": 115.0,
            "stop_loss":   95.0,
        }
    }))

    import exit_manager as em

    pos    = _long_position(sym="AAPL", qty=10, current_price=115.0)  # at target
    client = _alpaca_client()

    with patch.object(em, "get_active_exits", return_value={"AAPL": {"status": "partial"}}):
        actions = em.run_exit_manager([pos], client, _minimal_cfg())

    assert any(a["action"] == "sw_tp_close" and a["symbol"] == "AAPL" for a in actions), (
        f"Expected sw_tp_close for AAPL, got: {actions}"
    )
    close_req = client.submit_order.call_args_list[0].args[0]
    from alpaca.trading.enums import OrderSide
    assert getattr(close_req, "side", None) == OrderSide.SELL, (
        f"Long SW-TP close must be SELL, got {getattr(close_req, 'side', None)!r}"
    )


# ── Test 4: long SW-TP does NOT fire when price below long target ─────────────

def test_swtp_long_does_not_fire_below_target(tmp_path, monkeypatch):
    """Long SW-TP does NOT fire when current_price < take_profit."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "runtime").mkdir(parents=True)

    targets_file = tmp_path / "data" / "runtime" / "position_targets.json"
    targets_file.write_text(json.dumps({
        "AAPL": {
            "symbol":      "AAPL",
            "take_profit": 115.0,
            "stop_loss":   95.0,
        }
    }))

    import exit_manager as em

    pos    = _long_position(sym="AAPL", qty=10, current_price=110.0)  # below target
    client = _alpaca_client()

    with patch.object(em, "get_active_exits", return_value={"AAPL": {"status": "partial"}}):
        actions = em.run_exit_manager([pos], client, _minimal_cfg())

    assert not any(a["action"] == "sw_tp_close" for a in actions), (
        f"Long SW-TP must NOT fire when price={110.0} < target={115.0}: {actions}"
    )
    client.submit_order.assert_not_called()
