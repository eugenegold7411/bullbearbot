"""
tests/test_order_hygiene.py — Order hygiene and protection-gap tests.

6 tests:
  1. test_overshoot_detection              — total sell_qty > position qty → OVERSHOOT
  2. test_no_overshoot_when_balanced       — sell_qty == position qty → ok
  3. test_cancel_replace_guarded_high_risk — _stops_placed_this_cycle guard exists
  4. test_no_unprotected_position_after_trim_failure — trim sell fails → stop re-placed
  5. test_scenario_a_stop_failure_detected — buy fills, stop fails → divergence detects
  6. test_scenario_c_trim_failure_detected  — trim cancel-replace fails → safety alert
"""

from __future__ import annotations

import time
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_order(
    order_id: str,
    symbol: str,
    side: str,
    order_type: str = "stop",
    order_class: str = "",
    qty: float = 10.0,
    stop_price: float = 100.0,
    status: str = "accepted",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=order_id,
        symbol=symbol,
        side=side,
        order_type=order_type,
        order_class=order_class,
        qty=str(qty),
        stop_price=str(stop_price) if stop_price else None,
        status=status,
        legs=None,
    )


def _make_position(symbol: str, qty: float, price: float = 100.0) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=symbol,
        qty=str(qty),
        current_price=str(price),
        avg_entry_price=str(price * 0.95),
        market_value=str(qty * price),
        unrealized_pl="0",
        asset_class="us_equity",
    )


# ── Test 1: Overshoot detection ────────────────────────────────────────────────

def test_overshoot_detection():
    """
    When total sell order qty exceeds position qty, an OVERSHOOT is detected.
    This mirrors the Phase 1a diagnostic logic.
    """
    # Position: 10 shares of NVDA
    # Open sell orders: two sell orders totaling 15 shares (5 + 10) — OVERSHOOT
    positions = {"NVDA": 10}

    orders = [
        _make_order("ord-001", "NVDA", side="sell", qty=5.0),
        _make_order("ord-002", "NVDA", side="sell", qty=10.0),
    ]

    # Replicate the overshoot detection logic from Phase 1a
    sell_qty: dict[str, int] = defaultdict(int)
    for o in orders:
        if str(o.side).lower() in ("sell", "orderside.sell"):
            sell_qty[o.symbol] += int(float(o.qty))

    overshoots = []
    for sym, total_sell in sell_qty.items():
        pos = positions.get(sym, 0)
        if total_sell > pos:
            overshoots.append(sym)

    assert "NVDA" in overshoots, (
        f"OVERSHOOT should be detected for NVDA (sell={sell_qty['NVDA']}, pos={positions['NVDA']})"
    )


def test_no_overshoot_when_balanced():
    """
    When total sell order qty equals position qty exactly, no OVERSHOOT is detected.
    """
    positions = {"AAPL": 55}

    orders = [
        _make_order("ord-010", "AAPL", side="sell", qty=55.0, order_class="oco"),
    ]

    sell_qty: dict[str, int] = defaultdict(int)
    for o in orders:
        if str(o.side).lower() in ("sell", "orderside.sell"):
            sell_qty[o.symbol] += int(float(o.qty))

    overshoots = []
    for sym, total_sell in sell_qty.items():
        pos = positions.get(sym, 0)
        if total_sell > pos:
            overshoots.append(sym)

    assert "AAPL" not in overshoots, (
        f"No OVERSHOOT expected for AAPL (sell={sell_qty['AAPL']}, pos={positions['AAPL']})"
    )


# ── Test 3: Guard exists for high-risk cancel-replace ─────────────────────────

def test_cancel_replace_guarded_high_risk():
    """
    The _stops_placed_this_cycle guard in _sell_cancel_stop_and_sell() must
    raise RuntimeError("deferred:...") when exit_manager placed a stop < 60s ago.
    This confirms ab0eb93 is in place and wired.
    """
    import exit_manager
    import order_executor

    # Simulate exit_manager placing a stop 5 seconds ago
    exit_manager._stops_placed_this_cycle["CRWD"] = time.time() - 5

    # Mock Alpaca so no real API calls are made
    mock_alpaca = MagicMock()
    mock_alpaca.get_orders.return_value = []

    with patch("order_executor._get_alpaca", return_value=mock_alpaca):
        with pytest.raises(RuntimeError, match="deferred"):
            order_executor._sell_cancel_stop_and_sell(
                symbol="CRWD",
                sell_qty=10,
                positions=[_make_position("CRWD", qty=31)],
            )

    # Clean up
    exit_manager._stops_placed_this_cycle.pop("CRWD", None)


# ── Test 4: No unprotected position after trim sell failure ───────────────────

def test_no_unprotected_position_after_trim_failure():
    """
    When the market sell fails in _sell_cancel_stop_and_sell(), the stop must
    be re-placed (_replace_stop called) before the exception is re-raised,
    so the remaining position stays protected.
    """
    import exit_manager
    import order_executor

    # Ensure guard doesn't fire
    exit_manager._stops_placed_this_cycle.clear()

    mock_stop_order = MagicMock()
    mock_stop_order.stop_price = "192.54"
    mock_stop_order.id = "stop-oid-001"
    mock_stop_order.side = "sell"
    mock_stop_order.order_type = "stop"
    mock_stop_order.order_class = ""

    mock_alpaca = MagicMock()
    mock_alpaca.get_orders.return_value = [mock_stop_order]
    mock_alpaca.cancel_order_by_id.return_value = None

    # Sell fails — simulates Alpaca rejection
    mock_alpaca.submit_order.side_effect = Exception("Simulated sell failure")

    replace_stop_calls = []

    def _mock_replace_stop(symbol, qty, stop_price):
        replace_stop_calls.append((symbol, qty, stop_price))

    with patch("order_executor._get_alpaca", return_value=mock_alpaca):
        with patch("order_executor._replace_stop", side_effect=_mock_replace_stop):
            with pytest.raises(Exception, match="Simulated sell failure"):
                order_executor._sell_cancel_stop_and_sell(
                    symbol="NVDA",
                    sell_qty=10,
                    positions=[_make_position("NVDA", qty=76)],
                )

    # The stop must have been re-placed before the exception propagated
    assert len(replace_stop_calls) == 1, (
        f"_replace_stop should be called once on sell failure, got: {replace_stop_calls}"
    )
    assert replace_stop_calls[0][0] == "NVDA", "Should re-place stop for NVDA"


# ── Test 5: Scenario A — stop failure detected by divergence ──────────────────

def test_scenario_a_stop_failure_detected():
    """
    Scenario A: a buy fills and the bracket stop leg is missing.
    detect_protection_divergence() should detect an unprotected position
    (stop_missing or protection_missing) when no stop order exists.

    Notes:
    - market_value must be a float because divergence.py calls abs(pos.market_value)
    - divergence.py requires 2 consecutive misses before firing — we pre-seed
      _fill_seen and _protection_miss_cycles to simulate the 2nd cycle
    - divergence.py has an 90s startup grace — we patch _STARTUP_EPOCH to bypass it
    """
    import divergence as div
    from divergence import detect_protection_divergence

    position = SimpleNamespace(
        symbol="TSLA_HYGIENE_TEST",  # unique name to avoid cross-test pollution
        qty="20",
        current_price="250.0",
        avg_entry_price="245.0",
        market_value=5000.0,   # float — divergence.py does abs(pos.market_value)
        unrealized_pl="100.0",
    )

    # Pre-seed state: simulate the 2nd consecutive cycle with no stop
    sym = "TSLA_HYGIENE_TEST"
    div._fill_seen[sym] = time.time() - 120  # 2 min ago (past grace)
    div._protection_miss_cycles[sym] = 1     # already had miss 1/2 → fire on this call

    # Patch startup epoch so startup grace doesn't block the event
    old_startup = div._STARTUP_EPOCH
    div._STARTUP_EPOCH = time.time() - 200   # pretend we started 200s ago

    try:
        events = detect_protection_divergence(
            account="A1",
            positions=[position],
            open_orders=[],
            grace_seconds=0,  # no grace window
        )
    finally:
        div._STARTUP_EPOCH = old_startup
        div._fill_seen.pop(sym, None)
        div._protection_miss_cycles.pop(sym, None)

    event_types = [e.event_type for e in events]
    assert any(et in ("stop_missing", "protection_missing") for et in event_types), (
        f"Expected stop_missing or protection_missing when no stop exists. Got: {event_types}"
    )


# ── Test 6: Scenario C — trim cancel-replace failure → safety alert ───────────

def test_scenario_c_trim_failure_detected():
    """
    Scenario C: TRIM fires, stops canceled, sell succeeds, but OCO re-placement
    fails. _fire_safety_alert must be called with the 'oco_replace' key so the
    operator is notified. The standalone-stop fallback (_replace_stop) is then
    attempted.

    This test verifies the non-fatal fail path: the trim completes (sell executed),
    a safety alert fires, and _replace_stop is attempted as a last resort.
    """
    import exit_manager
    import order_executor

    # Ensure guard doesn't fire
    exit_manager._stops_placed_this_cycle.clear()

    # Build a stop order with a numeric stop_price so the OCO path is taken
    mock_stop_order = MagicMock()
    mock_stop_order.stop_price = "192.54"
    mock_stop_order.id = "stop-oid-002"
    mock_stop_order.side = "sell"
    mock_stop_order.order_type = "stop"
    mock_stop_order.order_class = ""
    mock_stop_order.legs = None

    sell_order_mock = MagicMock()
    sell_order_mock.filled_avg_price = "210.0"
    sell_order_mock.filled_qty = "10"
    sell_order_mock.filled_at = "2026-05-07T18:00:00Z"
    sell_order_mock.id = "sell-fill-001"

    mock_alpaca = MagicMock()
    mock_alpaca.get_orders.return_value = [mock_stop_order]
    mock_alpaca.cancel_order_by_id.return_value = None

    # Sell succeeds; OCO re-placement fails; standalone stop fallback succeeds
    mock_alpaca.submit_order.side_effect = [
        sell_order_mock,          # market sell succeeds
        Exception("OCO failed"),  # OCO re-placement fails → triggers safety alert
    ]

    safety_alerts = []

    def _mock_safety_alert(fn_name, exc):
        safety_alerts.append((fn_name, str(exc)))

    replace_stop_calls = []

    def _mock_replace_stop(symbol, qty, stop_price):
        replace_stop_calls.append((symbol, qty, stop_price))

    # Patch position_targets to return no TP price (simpler path: no OCO attempt)
    # This causes the code to go to _replace_stop directly
    # To trigger oco_replace path we need _tp_price, so return one
    import json
    tp_data = json.dumps({"MSFT": {"take_profit": 450.0}})

    with patch("order_executor._get_alpaca", return_value=mock_alpaca):
        with patch("order_executor._fire_safety_alert", side_effect=_mock_safety_alert):
            with patch("order_executor._replace_stop", side_effect=_mock_replace_stop):
                with patch("builtins.open", MagicMock()):
                    with patch("order_executor.Path") as mock_path_cls:
                        mock_p = MagicMock()
                        mock_p.exists.return_value = True
                        mock_p.read_text.return_value = tp_data
                        mock_path_cls.return_value = mock_p

                        order_executor._sell_cancel_stop_and_sell(
                            symbol="MSFT",
                            sell_qty=10,
                            positions=[_make_position("MSFT", qty=38, price=420.0)],
                        )

    # Safety alert must have fired for the OCO failure
    alert_names = [a[0] for a in safety_alerts]
    assert any("oco_replace" in name for name in alert_names), (
        f"Expected safety alert for oco_replace failure, got alerts: {safety_alerts}"
    )

    # Fallback _replace_stop must have been attempted
    assert len(replace_stop_calls) >= 1, (
        f"Expected _replace_stop fallback attempt, got: {replace_stop_calls}"
    )
