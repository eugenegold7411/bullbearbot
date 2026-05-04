"""
tests/test_bug009b_tp_fallback.py — BUG-009b OCO fix verification tests.

BUG-009b root cause: Alpaca's OCA share-lock prevents adding a standalone TP
when a standalone stop already holds all shares (error 40310000).

Fix: cancel the existing stop, resubmit as an OCO pair (stop + TP in the same
OCA group).  Alpaca allows stop + TP to coexist inside one OCA group on the
same shares.  This applies to both paths:
  - order_executor._submit_buy(): TP leg voided after bracket fills
  - exit_manager.refresh_exits_for_position(): status='partial' each cycle

NOTE on assertions: the conftest stubs register all Alpaca request classes as
_KwargsRequest (a single class that copies all kwargs as attributes), so class-
name checks are meaningless.  Tests identify order TYPE by shape:
  - order_class==OCO + side=SELL + limit_price + stop_loss → OCO repair
  - order_class==BRACKET + side=BUY                        → bracket entry
"""
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


# ── order-shape helpers ───────────────────────────────────────────────────────

def _is_oco_sell(req) -> bool:
    """Identify an OCO sell — the correct BUG-009b repair shape.

    An OCO order carries:
      - order_class == OrderClass.OCO
      - side == SELL
      - time_in_force == GTC
      - limit_price present (take-profit leg)
      - stop_loss present (stop leg)
    """
    from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
    return (
        getattr(req, "order_class", None) == OrderClass.OCO
        and getattr(req, "side",        None) == OrderSide.SELL
        and getattr(req, "time_in_force", None) == TimeInForce.GTC
        and getattr(req, "limit_price", None) is not None
        and getattr(req, "stop_loss",   None) is not None
    )


# ── helpers ───────────────────────────────────────────────────────────────────

def _filled_bracket_order(fill_price=200.0, fill_qty=10):
    o = MagicMock()
    o.id = "bracket-order-id"
    o.filled_avg_price = str(fill_price)
    o.filled_qty = str(fill_qty)
    o.filled_at = "2026-05-01T10:00:00Z"
    return o


def _stop_order_mock(stop_price=192.54):
    """Open stop order — satisfies the _stop_active check."""
    from alpaca.trading.enums import OrderSide
    o = MagicMock()
    o.id = "existing-stop-id"
    o.side = OrderSide.SELL
    o.order_type = "stop"
    o.type = "stop"
    o.stop_price = stop_price
    return o


def _buy_action(symbol="NVDA", qty=10, stop_loss=192.54, take_profit=213.36):
    return {
        "symbol":      symbol,
        "qty":         qty,
        "stop_loss":   stop_loss,
        "take_profit": take_profit,
        "order_type":  "market",
    }


def _run_submit_buy(action, open_orders, side_effects=None):
    """Call _submit_buy() directly under full stub context."""
    _ensure_stubs()
    import order_executor as oe

    if side_effects is None:
        side_effects = [_filled_bracket_order(), MagicMock(id="oco-id")]

    client = MagicMock()
    client.submit_order.side_effect = side_effects
    client.get_orders.return_value = open_orders

    _path_mock = MagicMock()
    with patch("order_executor._get_alpaca", return_value=client), \
         patch("order_executor.log_trade"), \
         patch("time.sleep"), \
         patch("pathlib.Path", return_value=_path_mock):
        oe._submit_buy(action)

    return client


# ── Test 1: OCO placed when bracket stop active, TP missing ──────────────────

def test_oco_placed_when_stop_active_no_tp():
    """
    After a confirmed fill, if open_orders has a stop but NO limit sell,
    _submit_buy() must cancel the existing stop and submit an OCO order.

    OCO fix: standalone TP is impossible (stop holds all shares, error 40310000).
    Cancelling stop + resubmitting as OCO puts both legs in one OCA group,
    which Alpaca allows.
    """
    action = _buy_action(symbol="NVDA", qty=10, stop_loss=192.54, take_profit=213.36)
    open_orders = [_stop_order_mock()]

    side_effects = [
        _filled_bracket_order(),       # bracket buy fills
        MagicMock(id="oco-order-id"),  # OCO submission
    ]
    client = _run_submit_buy(action, open_orders, side_effects=side_effects)
    submitted = [c.args[0] for c in client.submit_order.call_args_list]

    assert any(_is_oco_sell(r) for r in submitted), (
        "Expected an OCO sell order (order_class=OCO, limit_price=TP, stop_loss=stop) "
        "when stop is active but TP is missing. "
        f"Submitted orders: {[vars(r) for r in submitted]}."
    )
    client.cancel_order_by_id.assert_called_once_with("existing-stop-id")


# ── Test 2: OCO TP leg price matches action['take_profit'] ───────────────────

def test_oco_tp_price_matches_action_take_profit():
    """The OCO limit_price (TP leg) must equal action['take_profit'] exactly."""
    tp_price = 213.29
    action = _buy_action(symbol="NVDA", qty=10, stop_loss=192.54, take_profit=tp_price)
    open_orders = [_stop_order_mock()]

    side_effects = [_filled_bracket_order(), MagicMock(id="oco-order-id")]
    client = _run_submit_buy(action, open_orders, side_effects=side_effects)
    submitted = [c.args[0] for c in client.submit_order.call_args_list]

    oco_orders = [r for r in submitted if _is_oco_sell(r)]
    assert len(oco_orders) >= 1, "No OCO sell submitted — BUG-009b fix not wired."
    placed_price = getattr(oco_orders[0], "limit_price", None)
    assert placed_price is not None and abs(placed_price - tp_price) < 0.01, (
        f"OCO limit_price={placed_price!r} does not match action take_profit={tp_price}."
    )


# ── Test 3: both legs voided — stop fallback fires, then OCO replaces it ─────

def test_oco_placed_when_both_bracket_legs_voided():
    """
    When open_orders is empty after fill (both bracket legs voided), _submit_buy()
    must:
      1. Place a standalone StopOrderRequest (existing BUG-009 fix)
      2. Cancel that standalone stop
      3. Resubmit as OCO so stop + TP coexist in one OCA group
    """
    action = _buy_action(symbol="NVDA", qty=10, stop_loss=192.54, take_profit=213.36)
    open_orders = []

    side_effects = [
        _filled_bracket_order(),              # bracket buy fills
        MagicMock(id="standalone-stop-id"),   # standalone stop fallback
        MagicMock(id="oco-order-id"),         # OCO replaces standalone stop
    ]
    client = _run_submit_buy(action, open_orders, side_effects=side_effects)
    submitted = [c.args[0] for c in client.submit_order.call_args_list]

    assert any(_is_oco_sell(r) for r in submitted), (
        "Expected an OCO sell after standalone stop + OCO upgrade path. "
        f"Submitted orders: {[vars(r) for r in submitted]}."
    )
    client.cancel_order_by_id.assert_called_once_with("standalone-stop-id")


# ── Test 4: refresh_exits returns True for partial status ─────────────────────

def test_refresh_exits_returns_true_for_partial_status():
    """
    refresh_exits_for_position() must return True for status='partial'
    (stop present, TP voided) after completing OCO repair.
    """
    _ensure_stubs()
    import exit_manager as em

    position = MagicMock()
    position.symbol          = "NVDA"
    position.avg_entry_price = "198.49"
    position.current_price   = "199.38"
    position.unrealized_pl   = "8.90"
    position.qty             = "10"

    client = MagicMock()
    client.submit_order.return_value = MagicMock(id="oco-repair-id")

    exit_info = {
        "status":            "partial",
        "stop_price":        192.54,
        "stop_order_id":     "existing-stop-id",
        "stop_order_status": "accepted",
        "target_price":      None,
        "target_order_id":   None,
    }
    strategy_config = {
        "exit_management": {
            "stop_loss_pct":               0.03,
            "take_profit_multiple":        2.5,
            "trail_stop_enabled":          False,
            "trail_trigger_r":             1.0,
            "refresh_if_stop_stale_pct":   0.20,
            "trail_to_breakeven_plus_pct": 0.005,
        }
    }

    with patch("time.sleep"):
        result = em.refresh_exits_for_position(
            position=position,
            alpaca_client=client,
            strategy_config=strategy_config,
            exit_info=exit_info,
        )

    assert result is True, (
        f"refresh_exits_for_position() returned {result!r} for status='partial'. "
        "Expected True — OCO repair should succeed and return True."
    )


# ── Test 5: refresh_exits submits OCO for partial status ──────────────────────

def test_refresh_exits_submits_oco_for_partial_status():
    """
    For status='partial', refresh_exits_for_position() must:
      1. Cancel the existing stop (releases Alpaca share-lock)
      2. Submit an OCO order (stop + TP in one OCA group)

    Old behavior was: try standalone limit TP → always blocked by 40310000.
    New behavior: cancel stop, resubmit as OCO.
    """
    _ensure_stubs()
    import exit_manager as em

    position = MagicMock()
    position.symbol          = "NVDA"
    position.avg_entry_price = "198.49"
    position.current_price   = "199.38"
    position.unrealized_pl   = "8.90"
    position.qty             = "10"

    client = MagicMock()
    client.submit_order.return_value = MagicMock(id="oco-repair-id")

    exit_info = {
        "status":            "partial",
        "stop_price":        192.54,
        "stop_order_id":     "existing-stop-id",
        "stop_order_status": "accepted",
        "target_price":      None,
        "target_order_id":   None,
    }
    strategy_config = {
        "exit_management": {
            "stop_loss_pct":               0.03,
            "take_profit_multiple":        2.5,
            "trail_stop_enabled":          False,
            "trail_trigger_r":             1.0,
            "refresh_if_stop_stale_pct":   0.20,
            "trail_to_breakeven_plus_pct": 0.005,
        }
    }

    with patch("time.sleep"):
        em.refresh_exits_for_position(
            position=position,
            alpaca_client=client,
            strategy_config=strategy_config,
            exit_info=exit_info,
        )

    submitted = [c.args[0] for c in client.submit_order.call_args_list]
    assert any(_is_oco_sell(r) for r in submitted), (
        "Expected an OCO sell (order_class=OCO, limit_price=TP, stop_loss) for "
        f"status='partial', but submit_order was called with: "
        f"{[vars(r) for r in submitted]}."
    )
    # The existing stop must be cancelled before OCO is submitted.
    client.cancel_order_by_id.assert_called_once_with("existing-stop-id")
