"""
tests/test_protection_layer.py — Protection layer guards: divergence side-filter (Issue 4)
and within-cycle race deferred (Issue 3).

7 tests:

Issue 4 — divergence.py stop_map side-filter:
  1. test_stop_map_excludes_buy_bracket_for_long_position
     BUY-side bracket for a LONG position must NOT appear in stop_map
  2. test_stop_map_includes_sell_oco_for_long_position
     SELL-side OCO for a LONG position must appear in stop_map
  3. test_stop_map_excludes_sell_order_for_short_position
     SELL-side bracket/OCO for a SHORT position must NOT appear in stop_map
  4. test_no_duplicate_exit_for_unfilled_buy_bracket
     LONG position with one SELL-OCO stop + one unfilled BUY-bracket → no duplicate_exit event

Issue 3 — order_executor deferred cancel-replace:
  5. test_trim_deferred_when_stop_placed_this_cycle
     _stops_placed_this_cycle has entry < 60s ago → RuntimeError("deferred:...")
  6. test_trim_proceeds_when_stop_placed_prior_cycle
     entry > 60s ago → no deferred, cancel-replace proceeds normally
  7. test_trim_proceeds_when_no_stop_placed
     empty _stops_placed_this_cycle → no deferred, normal path
"""

from __future__ import annotations

import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_order(
    order_id: str,
    symbol: str,
    side: str,           # "buy" or "sell"
    order_class: str,    # "bracket", "oco", "simple", ""
    order_type: str = "limit",
    status: str = "accepted",
    qty: float = 10.0,
    stop_price: float = 0.0,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=order_id,
        symbol=symbol,
        side=side,
        order_class=order_class,
        order_type=order_type,
        status=status,
        qty=str(qty),
        stop_price=str(stop_price) if stop_price else None,
    )


def _make_position(symbol: str, qty: float, current_price: float = 100.0) -> SimpleNamespace:
    mv = qty * current_price
    return SimpleNamespace(
        symbol=symbol,
        qty=str(qty),
        current_price=str(current_price),
        market_value=str(mv),
        unrealized_pl="0",
    )


# ── Issue 4 tests ─────────────────────────────────────────────────────────────

def test_stop_map_excludes_buy_bracket_for_long_position():
    """BUY-side bracket for a LONG position is an entry order — must not be in stop_map."""
    from divergence import detect_protection_divergence

    position = _make_position("CRWD", qty=34.0)
    # Unfilled ADD bracket buy — should NOT count as a stop
    buy_bracket = _make_order(
        "buy-bracket-001", "CRWD", side="buy", order_class="bracket", qty=33.0,
    )

    events = detect_protection_divergence(
        account="A1",
        positions=[position],
        open_orders=[buy_bracket],
        grace_seconds=0,
    )

    # The BUY bracket should be filtered out → position appears unprotected
    # (protection_missing or stop_missing), NOT duplicate_exit.
    event_types = [e.event_type for e in events]
    assert "duplicate_exit" not in event_types, (
        f"duplicate_exit should not fire for a BUY bracket on a LONG position, got: {event_types}"
    )


def test_stop_map_includes_sell_oco_for_long_position():
    """SELL-side OCO for a LONG position is a real exit stop and must be in stop_map."""
    from divergence import detect_protection_divergence

    position = _make_position("CRWD", qty=34.0)
    sell_oco = _make_order(
        "sell-oco-001", "CRWD", side="sell", order_class="oco", qty=34.0,
    )

    events = detect_protection_divergence(
        account="A1",
        positions=[position],
        open_orders=[sell_oco],
        grace_seconds=0,
    )

    # SELL-side OCO should protect the position → no protection_missing event
    event_types = [e.event_type for e in events]
    assert "protection_missing" not in event_types, (
        f"SELL-side OCO should protect position, but got events: {event_types}"
    )
    assert "stop_missing" not in event_types, (
        f"SELL-side OCO should protect position, but got events: {event_types}"
    )


def test_stop_map_excludes_sell_order_for_short_position():
    """SELL-side bracket/OCO for a SHORT position is an entry order — must not be in stop_map."""
    from divergence import detect_protection_divergence

    position = _make_position("TSLA", qty=-10.0)  # SHORT position
    # A SELL-side OCO for a short position would be an entry (shorting more), not an exit
    sell_order = _make_order(
        "sell-entry-001", "TSLA", side="sell", order_class="bracket", qty=10.0,
    )

    events = detect_protection_divergence(
        account="A1",
        positions=[position],
        open_orders=[sell_order],
        grace_seconds=0,
    )

    # SELL order on a SHORT position is not an exit stop → not in stop_map → not duplicate_exit
    event_types = [e.event_type for e in events]
    assert "duplicate_exit" not in event_types, (
        f"SELL bracket on SHORT position must not be counted as a stop: {event_types}"
    )


def test_no_duplicate_exit_for_unfilled_buy_bracket():
    """LONG position with one SELL-OCO stop + one unfilled BUY-bracket must NOT fire duplicate_exit.

    This is the exact CRWD scenario from 2026-05-06 23:43 UTC.
    """
    from divergence import detect_protection_divergence

    position = _make_position("CRWD", qty=34.0)
    sell_oco  = _make_order("oco-stop-001",  "CRWD", side="sell", order_class="oco",     qty=34.0)
    buy_bkt   = _make_order("buy-bkt-001",   "CRWD", side="buy",  order_class="bracket", qty=33.0)

    events = detect_protection_divergence(
        account="A1",
        positions=[position],
        open_orders=[sell_oco, buy_bkt],
        grace_seconds=0,
    )

    event_types = [e.event_type for e in events]
    assert "duplicate_exit" not in event_types, (
        f"BUY bracket must not trigger duplicate_exit alongside SELL OCO, got: {event_types}"
    )
    assert "protection_missing" not in event_types, (
        f"SELL OCO should protect position, but got: {event_types}"
    )
    assert "stop_missing" not in event_types


# ── Issue 3 tests ─────────────────────────────────────────────────────────────

def _base_positions_for_trim() -> list:
    return [_make_position("DIS", qty=254.0, current_price=117.0)]


def test_trim_deferred_when_stop_placed_this_cycle():
    """_sell_cancel_stop_and_sell raises RuntimeError('deferred:...') when exit_manager
    placed a stop for the symbol within the last 60 seconds."""
    import exit_manager
    from order_executor import _sell_cancel_stop_and_sell

    # Simulate exit_manager having placed a stop 5 seconds ago
    exit_manager._stops_placed_this_cycle["DIS"] = time.time() - 5.0

    try:
        with pytest.raises(RuntimeError, match="deferred: stop placed this cycle"):
            _sell_cancel_stop_and_sell("DIS", 109, _base_positions_for_trim())
    finally:
        exit_manager._stops_placed_this_cycle.pop("DIS", None)


def test_trim_proceeds_when_stop_placed_prior_cycle():
    """_sell_cancel_stop_and_sell proceeds normally when the last exit_manager stop placement
    was more than 60 seconds ago."""
    import exit_manager
    from order_executor import _sell_cancel_stop_and_sell

    # Simulate a stop that was placed 120 seconds ago (prior cycle)
    exit_manager._stops_placed_this_cycle["DIS"] = time.time() - 120.0

    mock_alpaca = MagicMock()
    # get_orders returns empty list → no stop to cancel
    mock_alpaca.get_orders.return_value = []
    # _submit_sell raises to short-circuit the actual sell
    with patch("order_executor._get_alpaca", return_value=mock_alpaca):
        with patch("order_executor._submit_sell", side_effect=RuntimeError("test-abort")):
            with pytest.raises(RuntimeError, match="test-abort"):
                _sell_cancel_stop_and_sell("DIS", 109, _base_positions_for_trim())

    # Should have reached get_orders (i.e., the guard did NOT fire)
    mock_alpaca.get_orders.assert_called_once()

    exit_manager._stops_placed_this_cycle.pop("DIS", None)


def test_trim_proceeds_when_no_stop_placed():
    """_sell_cancel_stop_and_sell proceeds normally when _stops_placed_this_cycle is empty."""
    import exit_manager
    from order_executor import _sell_cancel_stop_and_sell

    exit_manager._stops_placed_this_cycle.clear()

    mock_alpaca = MagicMock()
    mock_alpaca.get_orders.return_value = []
    with patch("order_executor._get_alpaca", return_value=mock_alpaca):
        with patch("order_executor._submit_sell", side_effect=RuntimeError("test-abort")):
            with pytest.raises(RuntimeError, match="test-abort"):
                _sell_cancel_stop_and_sell("DIS", 109, _base_positions_for_trim())

    mock_alpaca.get_orders.assert_called_once()
