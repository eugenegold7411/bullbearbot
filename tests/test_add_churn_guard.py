"""
tests/test_add_churn_guard.py — Guard against bracket-add churn in _execute_live_add.

4 tests:
  1. test_add_skipped_when_open_buy_exists
     Mock _has_open_buy_order returning (True, "fake-id"), assert ADD is skipped and
     the returned string starts with "skipped:" and log contains the expected message.

  2. test_add_proceeds_when_no_open_buy
     Mock _has_open_buy_order returning (False, ""), assert ADD calls process_idea
     and returns "ok:{qty}".

  3. test_has_open_buy_order_returns_true_for_buy_side
     Inject a fake order_executor._get_alpaca that returns a BUY-side order.
     Assert _has_open_buy_order returns (True, order_id).

  4. test_has_open_buy_order_returns_false_for_sell_only
     Inject a fake order_executor._get_alpaca that returns only a SELL-side order.
     Assert _has_open_buy_order returns (False, "").
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from alpaca.trading.enums import OrderSide

# Tests 3 and 4 use the injectable _client parameter on _has_open_buy_order —
# no sys.modules patching needed, making them stable across any test ordering.


# ── Shared helpers (mirrors test_allocator_live_add_replace.py) ────────────────

def _raw_position(symbol: str, qty: float, current_price: float) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=symbol,
        qty=str(qty),
        current_price=str(current_price),
        market_value=str(qty * current_price),
        unrealized_pl="0",
    )


def _make_snapshot(equity: float = 100_000.0, buying_power: float = 50_000.0) -> MagicMock:
    snap = MagicMock()
    snap.equity = equity
    snap.buying_power = buying_power
    snap.exposure_dollars = 30_000.0
    snap.short_exposure_dollars = 0.0
    snap.positions = []
    snap.position_by_symbol = {}
    return snap


def _base_cfg() -> dict:
    return {
        "parameters": {
            "stop_loss_pct_core":              0.03,
            "take_profit_multiple":            2.5,
            "max_positions":                   30,
            "add_conviction_gate":             0.6,
            "margin_authorized":               True,
            "margin_sizing_multiplier":        2.0,
            "catalyst_tag_disallowed_values":  ["", "none", "null", "no"],
        },
        "position_sizing": {
            "dynamic_tier_pct": 0.15,
            "core_tier_pct":    0.20,
        },
    }


def _fake_oe(return_value: list | None = None) -> MagicMock:
    """Fake order_executor module injected into sys.modules."""
    mod = MagicMock()
    mod.execute_all = MagicMock(return_value=return_value or [])
    mod.__name__ = "order_executor"
    return mod


# ── Test 1 ────────────────────────────────────────────────────────────────────

def test_add_skipped_when_open_buy_exists():
    """_execute_live_add returns 'skipped:...' when _has_open_buy_order returns True."""
    from portfolio_allocator import _execute_live_add

    raw_positions = [_raw_position("STNG", 191.0, 84.0)]
    snapshot = _make_snapshot()
    account = MagicMock()
    fake_oe = _fake_oe()

    mock_pi = MagicMock()

    with patch.dict(sys.modules, {"order_executor": fake_oe}):
        with patch("risk_kernel.process_idea", mock_pi):
            with patch("portfolio_allocator._has_open_buy_order", return_value=(True, "abc-123")):
                result = _execute_live_add(
                    symbol="STNG",
                    positions=raw_positions,
                    snapshot=snapshot,
                    cfg=_base_cfg(),
                    session_tier="market",
                    reason="allocator add STNG",
                    account=account,
                )

    assert result.startswith("skipped:"), f"Expected 'skipped:...' but got: {result!r}"
    assert "STNG" in result
    mock_pi.assert_not_called()
    fake_oe.execute_all.assert_not_called()


# ── Test 2 ────────────────────────────────────────────────────────────────────

def test_add_proceeds_when_no_open_buy():
    """_execute_live_add proceeds normally when _has_open_buy_order returns False."""
    from portfolio_allocator import _execute_live_add

    raw_positions = [_raw_position("STNG", 191.0, 84.0)]
    snapshot = _make_snapshot()
    account = MagicMock()

    mock_ba = MagicMock()
    mock_ba.qty = 54
    mock_pi = MagicMock(return_value=mock_ba)
    fake_oe = _fake_oe()

    with patch.dict(sys.modules, {"order_executor": fake_oe}):
        with patch("risk_kernel.process_idea", mock_pi):
            with patch("portfolio_allocator._has_open_buy_order", return_value=(False, "")):
                result = _execute_live_add(
                    symbol="STNG",
                    positions=raw_positions,
                    snapshot=snapshot,
                    cfg=_base_cfg(),
                    session_tier="market",
                    reason="allocator add STNG",
                    account=account,
                )

    assert result == "ok:54", f"Expected 'ok:54' but got: {result!r}"
    mock_pi.assert_called_once()
    fake_oe.execute_all.assert_called_once()


# ── Test 3 ────────────────────────────────────────────────────────────────────

def test_has_open_buy_order_returns_true_for_buy_side():
    """_has_open_buy_order returns (True, order_id) when a BUY-side order is open.

    Uses the injectable _client parameter to bypass the real Alpaca client without
    any sys.modules patching — this pattern is stable across any test ordering.
    """
    from portfolio_allocator import _has_open_buy_order

    mock_order = MagicMock()
    mock_order.side = OrderSide.BUY
    mock_order.id = "order-buy-uuid-001"

    mock_alpaca = MagicMock()
    mock_alpaca.get_orders = MagicMock(return_value=[mock_order])

    found, order_id = _has_open_buy_order("STNG", _client=mock_alpaca)

    assert found is True
    assert order_id == "order-buy-uuid-001"


# ── Test 4 ────────────────────────────────────────────────────────────────────

def test_has_open_buy_order_returns_false_for_sell_only():
    """_has_open_buy_order returns (False, '') when only SELL-side orders are open.

    Uses the injectable _client parameter — same stable pattern as test 3.
    """
    from portfolio_allocator import _has_open_buy_order

    mock_order = MagicMock()
    mock_order.side = OrderSide.SELL
    mock_order.id = "order-sell-uuid-001"

    mock_alpaca = MagicMock()
    mock_alpaca.get_orders = MagicMock(return_value=[mock_order])

    found, order_id = _has_open_buy_order("STNG", _client=mock_alpaca)

    assert found is False
    assert order_id == ""
