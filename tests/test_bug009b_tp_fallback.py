"""
tests/test_bug009b_tp_fallback.py — 5 failing tests proving BUG-009b.

BUG-009b: Alpaca paper trading silently voids BOTH the stop-loss AND
take-profit child legs of bracket orders on OCA collision.  BUG-009
(commit eefa3e0) added a stop fallback in _submit_buy().  There is NO
equivalent fallback for the take-profit leg, and refresh_exits_for_position()
does not repair "partial" status (stop present, no TP).

All 5 tests FAIL on current code — they are pre-implementation proof of the
bug, not regression guards.  Do not implement the fix until the diagnosis has
been reviewed and approved.

NOTE on assertions: the conftest stubs register all Alpaca request classes as
_KwargsRequest (a single class that copies all kwargs as attributes), so class-
name checks are meaningless.  Tests instead identify order TYPE by shape:
  - GTC limit sell with limit_price + side=SELL + no order_class → TP fallback
  - GTC stop with stop_price + side=SELL + no order_class       → stop fallback
  - DAY order with order_class=BRACKET + side=BUY               → bracket entry
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


# ── order-shape helpers ───────────────────────────────────────────────────────

def _is_tp_sell(req) -> bool:
    """
    Identify a standalone GTC limit sell — the expected TP fallback shape.

    Conftest stubs all request classes as _KwargsRequest, so class-name checks
    are unreliable.  Distinguish by the combination of attributes that uniquely
    identify a standalone TP order vs. a bracket entry or a stop order:
      - limit_price present (not a stop)
      - side == SELL        (protective, not an entry)
      - time_in_force == GTC (standalone; brackets use DAY)
      - no order_class      (brackets carry BRACKET)
    """
    from alpaca.trading.enums import OrderSide, TimeInForce
    return (
        hasattr(req, "limit_price")
        and getattr(req, "side", None) == OrderSide.SELL
        and getattr(req, "time_in_force", None) == TimeInForce.GTC
        and not hasattr(req, "order_class")
    )


# ── helpers ───────────────────────────────────────────────────────────────────

def _filled_bracket_order(fill_price=200.0, fill_qty=10):
    o = MagicMock()
    o.id = "bracket-order-id"
    o.filled_avg_price = str(fill_price)
    o.filled_qty = str(fill_qty)
    o.filled_at = "2026-05-01T10:00:00Z"
    return o


def _stop_order_mock():
    """Open stop order — satisfies the existing _stop_active check."""
    from alpaca.trading.enums import OrderSide
    o = MagicMock()
    o.id = "existing-stop-id"
    o.side = OrderSide.SELL   # == "sell" under the stub
    o.order_type = "stop"
    o.type = "stop"
    return o


def _buy_action(symbol="NVDA", qty=10, stop_loss=192.54, take_profit=213.36):
    return {
        "symbol":      symbol,
        "qty":         qty,
        "stop_loss":   stop_loss,
        "take_profit": take_profit,
        "order_type":  "market",   # forces MarketOrderRequest for bracket (not LimitOrderRequest)
    }


def _run_submit_buy(action, open_orders, side_effects=None):
    """Call _submit_buy() directly under full stub context."""
    _ensure_stubs()
    import order_executor as oe

    if side_effects is None:
        side_effects = [_filled_bracket_order(), MagicMock(id="fallback-order")]

    client = MagicMock()
    client.submit_order.side_effect = side_effects
    client.get_orders.return_value = open_orders

    with patch("order_executor._get_alpaca", return_value=client), \
         patch("order_executor.log_trade"), \
         patch("time.sleep"):
        oe._submit_buy(action)

    return client


# ── Test 1: TP fallback placed when stop active, no limit sell ────────────────

def test_tp_fallback_placed_when_stop_present_no_limit_sell():
    """
    After a confirmed fill, if open_orders has a stop but NO limit sell,
    _submit_buy() must place a standalone GTC limit sell at the take_profit price.

    BUG-009b: FAILS — _submit_buy() has no _tp_active check and no standalone
    TP placement block.  Only the bracket MarketOrderRequest is submitted.
    """
    action = _buy_action(symbol="NVDA", qty=10, stop_loss=192.54, take_profit=213.36)
    open_orders = [_stop_order_mock()]  # stop present, NO limit sell

    client = _run_submit_buy(action, open_orders)
    submitted = [c.args[0] for c in client.submit_order.call_args_list]

    assert any(_is_tp_sell(r) for r in submitted), (
        "Expected a standalone GTC limit sell (TP fallback) after a confirmed fill "
        "where a stop is active but no take-profit limit sell exists. "
        f"Submitted orders: {[vars(r) for r in submitted]}. "
        "BUG-009b: _submit_buy() has no _tp_active check or standalone TP placement."
    )


# ── Test 2: TP placed at exact action['take_profit'] price ────────────────────

def test_tp_fallback_price_matches_action_take_profit():
    """
    The standalone TP limit sell must be placed at exactly action['take_profit'],
    not a recomputed or default-derived value.

    BUG-009b: FAILS — no GTC limit sell is submitted at all.
    """
    tp_price = 213.29
    action = _buy_action(symbol="NVDA", qty=10, stop_loss=192.54, take_profit=tp_price)
    open_orders = [_stop_order_mock()]

    client = _run_submit_buy(action, open_orders)
    submitted = [c.args[0] for c in client.submit_order.call_args_list]

    tp_orders = [r for r in submitted if _is_tp_sell(r)]
    assert len(tp_orders) >= 1, (
        "No standalone GTC limit sell submitted — BUG-009b: TP fallback is missing."
    )
    placed_price = getattr(tp_orders[0], "limit_price", None)
    assert placed_price is not None and abs(placed_price - tp_price) < 0.01, (
        f"TP limit_price={placed_price!r} does not match action take_profit={tp_price}. "
        "The fallback must use the value from the action dict, not a recomputed price."
    )


# ── Test 3: both legs voided — stop fallback fires, TP fallback should too ────

def test_tp_placed_alongside_stop_when_both_legs_voided():
    """
    When open_orders is empty after fill (both stop AND TP child legs were
    silently voided by Alpaca OCA collision), _submit_buy() must:
      1. Place a standalone StopOrderRequest (existing BUG-009 fix — already works)
      2. Place a standalone GTC limit sell for the TP (BUG-009b — missing)

    BUG-009b: FAILS — the stop fallback fires and places a stop, but there is
    no TP fallback.  The position is left without a take-profit order.
    """
    action = _buy_action(symbol="NVDA", qty=10, stop_loss=192.54, take_profit=213.36)
    open_orders = []  # both legs voided

    side_effects = [
        _filled_bracket_order(),
        MagicMock(id="standalone-stop-id"),   # stop fallback
        MagicMock(id="standalone-tp-id"),     # tp fallback (expected but absent)
    ]
    client = _run_submit_buy(action, open_orders, side_effects=side_effects)
    submitted = [c.args[0] for c in client.submit_order.call_args_list]

    assert any(_is_tp_sell(r) for r in submitted), (
        "Expected a standalone GTC limit sell (TP fallback) alongside the stop fallback "
        "when both bracket legs are silently voided. "
        f"Submitted orders: {[vars(r) for r in submitted]}. "
        "BUG-009b: the stop fallback fires (BUG-009 fix) but there is no TP fallback."
    )


# ── Test 4: refresh_exits_for_position returns True for partial status ─────────

def test_refresh_exits_returns_true_for_partial_status():
    """
    When a position has status='partial' (stop present, no take-profit),
    refresh_exits_for_position() must detect it and return True after placing a TP.

    BUG-009b secondary gap: exit_manager.py line 440 reads
        if not (is_unprotected or is_stale): return False
    'partial' does not set is_unprotected (requires 'unprotected', 'unknown', or
    'tp_only') and is not stale — so the function returns False immediately
    without placing a TP.

    FAILS on current code.
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
    client.submit_order.return_value = MagicMock(id="tp-repair-id")

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
        "Expected True — should detect the missing TP and place a GTC limit sell. "
        "BUG-009b: line 440 returns False immediately for 'partial' status."
    )


# ── Test 5: refresh_exits places GTC limit sell for partial status ─────────────

def test_refresh_exits_places_limit_sell_for_partial_status():
    """
    When status='partial', refresh_exits_for_position() must submit a GTC limit
    sell (take-profit) order WITHOUT cancelling the existing stop.

    The stop is healthy — only the TP is missing.  The fix must add the TP in
    isolation, not route through the full stop-refresh path.

    BUG-009b: FAILS — the function returns at line 440 before calling
    submit_order or cancel_order_by_id at all.
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
    client.submit_order.return_value = MagicMock(id="tp-repair-id")

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
    assert any(_is_tp_sell(r) for r in submitted), (
        "Expected a standalone GTC limit sell for TP repair when status='partial', "
        f"but submit_order was called with: {[vars(r) for r in submitted]}. "
        "BUG-009b: refresh_exits_for_position() returns before submitting any order."
    )

    # The existing stop is healthy — it must NOT be cancelled.
    assert client.cancel_order_by_id.call_count == 0, (
        f"cancel_order_by_id called {client.cancel_order_by_id.call_count} time(s) "
        "for status='partial'. The stop is healthy; only the TP needs to be added."
    )
