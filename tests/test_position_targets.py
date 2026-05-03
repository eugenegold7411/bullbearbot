"""
tests/test_position_targets.py — Tests for BUG-009b SW-TP fix (position_targets.json).

Covers:
  1-4: order_executor._submit_buy() writes position_targets.json at bracket submission
  5-7: exit_manager helper functions (_load_position_targets, _remove_position_target)
  8-10: exit_manager.run_exit_manager() SW-TP check
"""
import json
import sys
import types
from unittest.mock import MagicMock, patch

# ── stubs (same pattern as test_bug009b_tp_fallback.py) ──────────────────────

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


# ── order_executor helpers ────────────────────────────────────────────────────

def _filled_order(fill_price=200.0, fill_qty=10, order_id="bracket-order-id"):
    o = MagicMock()
    o.id = order_id
    o.filled_avg_price = str(fill_price)
    o.filled_qty = str(fill_qty)
    o.filled_at = "2026-05-01T10:00:00Z"
    return o


def _stop_order():
    from alpaca.trading.enums import OrderSide
    o = MagicMock()
    o.id = "existing-stop-id"
    o.side = OrderSide.SELL
    o.order_type = "stop"
    o.type = "stop"
    return o


def _buy_action(symbol="NVDA", qty=10, stop_loss=192.54, take_profit=213.36):
    return {
        "symbol":      symbol,
        "qty":         qty,
        "stop_loss":   stop_loss,
        "take_profit": take_profit,
        "order_type":  "market",
    }


def _run_submit_buy(action, mock_path_inst):
    """Call _submit_buy() with Path I/O mocked via patch('pathlib.Path')."""
    _ensure_stubs()
    import order_executor as oe

    client = MagicMock()
    # First call: bracket order fill. Second: standalone TP fallback (BUG-009b).
    client.submit_order.side_effect = [_filled_order(), MagicMock(id="tp-fallback-id")]
    client.get_orders.return_value = [_stop_order()]  # stop present — stops stop-fallback

    with patch("order_executor._get_alpaca", return_value=client), \
         patch("order_executor.log_trade"), \
         patch("time.sleep"), \
         patch("pathlib.Path", return_value=mock_path_inst):
        oe._submit_buy(action)

    return client


# ── exit_manager helpers ──────────────────────────────────────────────────────

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


def _mock_position(symbol="NVDA", qty="10", current_price="213.40",
                   avg_entry_price="198.49", unrealized_pl="148.80"):
    p = MagicMock()
    p.symbol = symbol
    p.qty = qty
    p.current_price = current_price
    p.avg_entry_price = avg_entry_price
    p.unrealized_pl = unrealized_pl
    return p


# ── Tests 1-4: order_executor writes position_targets.json ───────────────────

def test_executor_writes_position_targets_on_bracket_submit():
    """_submit_buy() calls write_text on position_targets.json after a bracket fill."""
    mp = MagicMock()
    mp.exists.return_value = False
    _run_submit_buy(_buy_action(), mp)
    assert mp.write_text.called, (
        "Expected write_text to be called on position_targets.json after bracket fill. "
        "The SW-TP fix requires the intended TP to be persisted at submission time."
    )


def test_executor_writes_correct_tp_to_position_targets():
    """The written JSON contains the correct symbol and take_profit value."""
    tp_price = 213.36
    mp = MagicMock()
    mp.exists.return_value = False
    _run_submit_buy(_buy_action(take_profit=tp_price), mp)

    assert mp.write_text.called, "write_text not called on position_targets.json"
    written_json = mp.write_text.call_args[0][0]
    data = json.loads(written_json)
    assert "NVDA" in data, f"Symbol 'NVDA' not in written data: {list(data.keys())}"
    assert abs(data["NVDA"]["take_profit"] - tp_price) < 0.01, (
        f"take_profit={data['NVDA']['take_profit']!r} does not match action tp={tp_price}"
    )


def test_executor_writes_stop_loss_qty_and_order_id():
    """The written JSON entry includes stop_loss, qty, and order_id alongside take_profit."""
    mp = MagicMock()
    mp.exists.return_value = False
    _run_submit_buy(_buy_action(stop_loss=192.54, take_profit=213.36, qty=10), mp)

    written_json = mp.write_text.call_args[0][0]
    data = json.loads(written_json)
    entry = data.get("NVDA", {})
    assert "stop_loss" in entry, f"stop_loss missing from entry: {entry}"
    assert "qty" in entry,       f"qty missing from entry: {entry}"
    assert "order_id" in entry,  f"order_id missing from entry: {entry}"


def test_executor_position_targets_write_failure_is_nonfatal():
    """A write_text failure must not propagate — _submit_buy() must return normally."""
    mp = MagicMock()
    mp.exists.return_value = False
    mp.write_text.side_effect = OSError("disk full")
    # Must not raise even though the write blew up
    _run_submit_buy(_buy_action(), mp)


# ── Tests 5-7: exit_manager helper functions ─────────────────────────────────

def test_load_position_targets_returns_dict_from_file(tmp_path):
    """_load_position_targets() parses position_targets.json and returns a dict."""
    _ensure_stubs()
    import exit_manager as em

    targets_file = tmp_path / "position_targets.json"
    targets_file.write_text(json.dumps({"NVDA": {"take_profit": 213.36}}))

    with patch.object(em, "_TARGETS_PATH", targets_file):
        result = em._load_position_targets()

    assert result == {"NVDA": {"take_profit": 213.36}}


def test_load_position_targets_returns_empty_when_file_missing(tmp_path):
    """_load_position_targets() returns {} when position_targets.json does not exist."""
    _ensure_stubs()
    import exit_manager as em

    missing = tmp_path / "position_targets.json"  # never written — does not exist
    with patch.object(em, "_TARGETS_PATH", missing):
        result = em._load_position_targets()

    assert result == {}


def test_remove_position_target_deletes_symbol_entry(tmp_path):
    """_remove_position_target() removes the symbol key and rewrites the file."""
    _ensure_stubs()
    import exit_manager as em

    targets_file = tmp_path / "position_targets.json"
    targets_file.write_text(json.dumps({
        "NVDA": {"take_profit": 213.36},
        "SPY":  {"take_profit": 768.67},
    }))

    with patch.object(em, "_TARGETS_PATH", targets_file):
        em._remove_position_target("NVDA")
        remaining = json.loads(targets_file.read_text())

    assert "NVDA" not in remaining, f"NVDA still present after removal: {remaining}"
    assert "SPY" in remaining,      f"SPY unexpectedly removed: {remaining}"


# ── Tests 8-10: run_exit_manager() SW-TP check ───────────────────────────────

def test_sw_tp_fires_when_current_price_at_target():
    """run_exit_manager() submits a SELL market close when current_price >= target * 0.999."""
    _ensure_stubs()
    from alpaca.trading.enums import OrderSide

    import exit_manager as em

    position = _mock_position(symbol="NVDA", qty="10", current_price="213.40")
    client = MagicMock()
    client.submit_order.return_value = MagicMock(id="sw-tp-close")

    targets = {"NVDA": {"take_profit": 213.36}}  # 213.40 >= 213.36 * 0.999 = 213.15 → fires

    with patch("exit_manager._load_position_targets", return_value=targets), \
         patch("exit_manager.get_active_exits", return_value={"NVDA": {"status": "partial", "stop_price": 192.54}}), \
         patch("exit_manager._remove_position_target"), \
         patch("exit_manager.refresh_exits_for_position", return_value=False), \
         patch("exit_manager.maybe_trail_stop", return_value=False), \
         patch("time.sleep"):
        actions = em.run_exit_manager([position], client, _strategy_config())

    assert client.submit_order.called, (
        "Expected submit_order to be called when current_price (213.40) >= target*0.999 (213.15). "
        "SW-TP check did not fire."
    )
    submitted = [c.args[0] for c in client.submit_order.call_args_list]
    sell_orders = [r for r in submitted if getattr(r, "side", None) == OrderSide.SELL]
    assert sell_orders, f"No SELL order submitted. All submitted: {[vars(r) for r in submitted]}"

    sw_tp_actions = [a for a in actions if a.get("action") == "sw_tp_close"]
    assert sw_tp_actions, f"sw_tp_close not recorded in actions: {actions}"


def test_sw_tp_skips_when_current_price_below_target():
    """run_exit_manager() does NOT submit a market close when price is below target."""
    _ensure_stubs()
    import exit_manager as em

    position = _mock_position(symbol="NVDA", qty="10", current_price="210.00")
    client = MagicMock()

    targets = {"NVDA": {"take_profit": 213.36}}  # 213.36 * 0.999 = 213.15 > 210.00 → skip

    with patch("exit_manager._load_position_targets", return_value=targets), \
         patch("exit_manager.get_active_exits", return_value={"NVDA": {"status": "partial", "stop_price": 192.54}}), \
         patch("exit_manager._remove_position_target"), \
         patch("exit_manager.refresh_exits_for_position", return_value=False), \
         patch("exit_manager.maybe_trail_stop", return_value=False), \
         patch("time.sleep"):
        actions = em.run_exit_manager([position], client, _strategy_config())

    sw_tp_actions = [a for a in actions if a.get("action") == "sw_tp_close"]
    assert not sw_tp_actions, (
        f"SW-TP should not fire when price (210.00) < target*0.999 (213.15). "
        f"sw_tp_close found in: {actions}"
    )


def test_sw_tp_skips_for_symbol_not_in_targets():
    """run_exit_manager() does not fire SW-TP when symbol is absent from position_targets.json."""
    _ensure_stubs()
    import exit_manager as em

    position = _mock_position(symbol="CAT", qty="2", current_price="956.39")
    client = MagicMock()

    with patch("exit_manager._load_position_targets", return_value={}), \
         patch("exit_manager.get_active_exits", return_value={"CAT": {"status": "partial", "stop_price": 866.39}}), \
         patch("exit_manager.refresh_exits_for_position", return_value=False), \
         patch("exit_manager.maybe_trail_stop", return_value=False), \
         patch("time.sleep"):
        actions = em.run_exit_manager([position], client, _strategy_config())

    sw_tp_actions = [a for a in actions if a.get("action") == "sw_tp_close"]
    assert not sw_tp_actions, (
        f"SW-TP should not fire for symbol absent from position_targets.json: {actions}"
    )
