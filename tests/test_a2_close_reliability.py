"""
test_a2_close_reliability.py — Tests for A2 close retry backoff and manual_review escalation.

Covers:
1. test_max_attempts_marks_manual_review
2. test_safety_alert_fires_once_not_every_attempt
3. test_close_attempts_counter_increments
4. test_position_effect_close_on_long_call
5. test_bracket_canceled_before_close
6. test_manual_review_stops_retry
7. test_close_order_construction — position_effect + live bid price (Fix A + Fix B)
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_structure(
    lifecycle: str = "orphan_tracked",
    underlying: str = "DIS",
    occ_symbol: str = "DIS260522C00110000",
    close_attempt_count: int = 0,
):
    from schemas import (
        OptionsLeg,
        OptionsStructure,
        OptionStrategy,
        StructureLifecycle,
        Tier,
    )
    lc_map = {
        "orphan_tracked": StructureLifecycle.ORPHAN_TRACKED,
        "fully_filled":   StructureLifecycle.FULLY_FILLED,
        "manual_review_required": StructureLifecycle.MANUAL_REVIEW_REQUIRED,
    }
    leg = OptionsLeg(
        occ_symbol=occ_symbol,
        underlying=underlying,
        side="buy",
        qty=1,
        option_type="call",
        strike=110.0,
        expiration="2026-05-22",
        bid=0.50,
        ask=0.70,
        filled_price=1.00,
    )
    struct = OptionsStructure(
        structure_id=f"test-orphan-{underlying}",
        underlying=underlying,
        strategy=OptionStrategy.SINGLE_CALL,
        lifecycle=lc_map[lifecycle],
        legs=[leg],
        contracts=1,
        max_cost_usd=100.0,
        opened_at="2026-05-01T10:00:00+00:00",
        catalyst="test",
        tier=Tier.CORE,
        debit_paid=1.00,
        max_profit_usd=None,
        close_attempt_count=close_attempt_count,
    )
    return struct


def _make_trading_client(submit_raises=None, positions=None):
    """Return a mock TradingClient that fails close submissions."""
    client = MagicMock()
    if submit_raises is not None:
        client.submit_order.side_effect = submit_raises
    else:
        client.submit_order.return_value = MagicMock(id="order-123")
    if positions is not None:
        client.get_all_positions.return_value = positions
    else:
        # Default: position still live
        pos = MagicMock()
        pos.symbol = "DIS260522C00110000"
        client.get_all_positions.return_value = [pos]
    return client


# ── Test 1: max attempts marks manual_review_required ────────────────────────

class TestMaxAttemptsMarksManualReview:
    def test_fifth_failure_sets_lifecycle(self):
        """After _MAX_CLOSE_ATTEMPTS failures, lifecycle is set to manual_review_required."""
        import options_executor
        from schemas import StructureLifecycle

        struct = _make_structure(close_attempt_count=options_executor._MAX_CLOSE_ATTEMPTS - 1)
        client = _make_trading_client(submit_raises=Exception("close failed"))

        with patch("options_executor._fire_manual_review_alert"):
            result = options_executor.close_structure(struct, client, reason="test_max")

        assert result.lifecycle == StructureLifecycle.MANUAL_REVIEW_REQUIRED

    def test_before_max_lifecycle_unchanged(self):
        """Before reaching max attempts, lifecycle stays orphan_tracked."""
        import options_executor
        from schemas import StructureLifecycle

        struct = _make_structure(close_attempt_count=1)
        client = _make_trading_client(submit_raises=Exception("close failed"))

        with patch("options_executor._fire_manual_review_alert"):
            with patch("options_executor._fire_safety_alert"):
                result = options_executor.close_structure(struct, client, reason="test_below_max")

        assert result.lifecycle == StructureLifecycle.ORPHAN_TRACKED


# ── Test 2: safety alert fires once, not every attempt ───────────────────────

class TestSafetyAlertFiresOnce:
    def test_alert_fires_on_attempt_1_and_max_only(self):
        """
        SAFETY DEGRADED alert fires on attempt 1 (count becomes 1).
        [A2 MANUAL REVIEW] fires when count reaches MAX.
        Attempts 2 through MAX-1 fire nothing.
        """
        import options_executor

        # Position still live so we count a failure
        pos = MagicMock()
        pos.symbol = "DIS260522C00110000"

        safety_alert_calls = []
        manual_alert_calls = []

        def record_safety(*a, **kw):
            safety_alert_calls.append(a)

        def record_manual(*a, **kw):
            manual_alert_calls.append(a)

        # Simulate attempts 1 through MAX sequentially
        for attempt_before in range(options_executor._MAX_CLOSE_ATTEMPTS):
            struct = _make_structure(close_attempt_count=attempt_before)
            client = _make_trading_client(submit_raises=Exception("fail"))

            with patch("options_executor._fire_safety_alert", side_effect=record_safety):
                with patch("options_executor._fire_manual_review_alert", side_effect=record_manual):
                    options_executor.close_structure(struct, client, reason="test_alert_rate")

        # Only attempt 1 fires SAFETY DEGRADED
        assert len(safety_alert_calls) == 1
        # Only attempt MAX fires MANUAL REVIEW
        assert len(manual_alert_calls) == 1


# ── Test 3: close_attempts counter increments ─────────────────────────────────

class TestCloseAttemptsCounterIncrements:
    def test_counter_increments_on_each_failure(self):
        """Each failed close increments close_attempt_count by 1."""
        import options_executor

        start_attempts = 1
        struct = _make_structure(close_attempt_count=start_attempts)
        client = _make_trading_client(submit_raises=Exception("fail"))

        with patch("options_executor._fire_safety_alert"):
            with patch("options_executor._fire_manual_review_alert"):
                result = options_executor.close_structure(struct, client, reason="test_counter")

        assert result.close_attempt_count == start_attempts + 1

    def test_counter_not_incremented_on_success(self):
        """Successful close does not increment close_attempt_count."""
        import options_executor

        struct = _make_structure(close_attempt_count=0)
        client = _make_trading_client()  # submit succeeds

        result = options_executor.close_structure(struct, client, reason="test_success")

        assert result.close_attempt_count == 0


# ── Test 4: position_effect close on long call ────────────────────────────────

class TestPositionEffectCloseOnLongCall:
    def test_sell_to_close_used_for_buy_side_leg(self):
        """Long call leg (side=buy) produces SELL order with SELL_TO_CLOSE position_intent."""
        from alpaca.trading.enums import OrderSide, PositionIntent

        import options_executor

        struct = _make_structure(close_attempt_count=0)
        submitted_req = {}

        def capture_order(req):
            submitted_req["req"] = req
            return MagicMock(id="order-xyz")

        client = MagicMock()
        client.submit_order.side_effect = capture_order
        client.get_all_positions.return_value = []

        options_executor.close_structure(struct, client, reason="test_position_intent")

        req = submitted_req.get("req")
        assert req is not None, "No order was submitted"
        assert req.side == OrderSide.SELL
        assert req.position_intent == PositionIntent.SELL_TO_CLOSE


# ── Test 5: bracket canceled before close ────────────────────────────────────

class TestBracketCanceledBeforeClose:
    def test_held_for_orders_triggers_bracket_cancel_and_retry(self):
        """
        When close fails with held_for_orders + related_orders, the holding order is
        canceled and the close is retried once.
        """
        import uuid  # noqa: PLC0415

        import options_executor

        holding_order_id = "b5ccc3d1-ef8b-4f3e-8e3d-57eb5521741a"
        error_body = json.dumps({
            "available": "0",
            "code": 40310000,
            "existing_qty": "10",
            "held_for_orders": "10",
            "message": "insufficient qty available for order (requested: 10, available: 0)",
            "related_orders": [holding_order_id],
            "symbol": "DIS260522C00110000",
        })
        held_exc = Exception(error_body)

        call_count = {"n": 0}
        retry_order = MagicMock(id="retry-order-456")

        def submit_side_effect(req):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise held_exc
            return retry_order

        client = MagicMock()
        client.submit_order.side_effect = submit_side_effect
        client.get_all_positions.return_value = []

        struct = _make_structure(close_attempt_count=0)

        with patch("options_executor._fire_safety_alert"):
            result = options_executor.close_structure(struct, client, reason="test_bracket_cancel")

        # cancel_order_by_id was called with the holding order UUID
        client.cancel_order_by_id.assert_called_once_with(uuid.UUID(holding_order_id))
        # submit_order was called twice (first fail + retry)
        assert client.submit_order.call_count == 2
        # Structure ends up CLOSING (close order submitted; Alpaca fill not yet confirmed).
        # _sync_closing_lifecycles will promote to CLOSED once the fill arrives.
        from schemas import StructureLifecycle
        assert result.lifecycle == StructureLifecycle.CLOSING


# ── Test 6: manual_review_required stops retry ───────────────────────────────

class TestManualReviewStopsRetry:
    def test_manual_review_lifecycle_not_in_is_open(self):
        """MANUAL_REVIEW_REQUIRED lifecycle → is_open() == False → no further closes."""
        struct = _make_structure(lifecycle="manual_review_required")
        assert not struct.is_open()

    def test_should_close_returns_false_for_manual_review(self):
        """should_close_structure returns (False, '') for MANUAL_REVIEW_REQUIRED."""
        import options_executor

        struct = _make_structure(lifecycle="manual_review_required")
        should_close, reason = options_executor.should_close_structure(
            struct,
            current_prices={"DIS": 109.0},
            config={},
            current_time=None,
        )
        assert should_close is False
        assert reason == ""


# ── Test 7: close order construction — Fix A (position_effect) + Fix B (live bid) ──

def _make_spread_struct_two_legs():
    """2-leg spread: buy C140 + sell C145 (both filled)."""
    from schemas import (
        OptionsLeg,
        OptionsStructure,
        OptionStrategy,
        StructureLifecycle,
        Tier,
    )
    buy_leg = OptionsLeg(
        occ_symbol="TSM260620C00140000", underlying="TSM", side="buy", qty=1,
        option_type="call", strike=140.0, expiration="2026-06-20",
        filled_price=2.50, bid=2.10, ask=2.90,
    )
    sell_leg = OptionsLeg(
        occ_symbol="TSM260620C00145000", underlying="TSM", side="sell", qty=1,
        option_type="call", strike=145.0, expiration="2026-06-20",
        filled_price=1.20, bid=0.90, ask=1.50,
    )
    return OptionsStructure(
        structure_id="test-spread-tsm",
        underlying="TSM",
        strategy=OptionStrategy.CALL_DEBIT_SPREAD,
        lifecycle=StructureLifecycle.FULLY_FILLED,
        legs=[buy_leg, sell_leg],
        contracts=1,
        max_cost_usd=200.0,
        opened_at="2026-05-01T10:00:00+00:00",
        catalyst="test",
        tier=Tier.CORE,
        debit_paid=1.30,
        max_profit_usd=None,
    )


class TestCloseOrderConstruction:
    """Fix A: position_effect="closing" on all per-leg close requests.
    Fix B: close limit price comes from live bid, not avg_entry_price."""

    def test_long_call_close_has_position_effect_closing(self):
        """Fix A: single-leg long call close must carry position_effect='closing'."""
        import options_executor

        struct = _make_structure()
        submitted_req = {}

        def capture(req):
            submitted_req["req"] = req
            return MagicMock(id="ord-a")

        client = MagicMock()
        client.submit_order.side_effect = capture
        client.get_all_positions.return_value = []

        with patch("options_executor._fetch_live_close_price", return_value=None):
            options_executor.close_structure(struct, client, reason="test_position_effect")

        req = submitted_req.get("req")
        assert req is not None, "No order was submitted"
        assert getattr(req, "position_effect", None) == "closing", (
            f"Expected position_effect='closing', got {getattr(req, 'position_effect', '<missing>')}"
        )

    def test_close_limit_price_uses_live_bid_not_avg_entry(self):
        """Fix B: close limit price should be the live bid, not avg_entry_price."""
        import options_executor

        # leg has stale bid=0.50 and filled_price (avg entry) = 1.00
        # live bid from the snapshot = 0.35  ← should win
        struct = _make_structure()  # leg.filled_price=1.00, leg.bid=0.50
        submitted_req = {}

        def capture(req):
            submitted_req["req"] = req
            return MagicMock(id="ord-b")

        client = MagicMock()
        client.submit_order.side_effect = capture
        client.get_all_positions.return_value = []

        live_bid = 0.35
        with patch("options_executor._fetch_live_close_price", return_value=live_bid):
            options_executor.close_structure(struct, client, reason="test_live_bid", method="limit")

        req = submitted_req.get("req")
        assert req is not None, "No order was submitted"
        # Price should be live bid rounded to $0.05 tick = 0.35
        from options_executor import _round_limit
        expected = _round_limit(live_bid)
        assert abs(req.limit_price - expected) < 0.001, (
            f"Expected limit_price≈{expected} (live bid), got {req.limit_price}"
        )
        # Must NOT be avg_entry_price (1.00) or stale leg.bid (0.50)
        assert req.limit_price < 0.50, (
            f"limit_price {req.limit_price} looks like stale leg data — expected {expected}"
        )

    def test_spread_per_leg_fallback_also_has_position_effect(self):
        """Fix A: per-leg fallback for a spread close also carries position_effect='closing'."""
        import options_executor

        struct = _make_spread_struct_two_legs()
        submitted_reqs = []

        def capture(req):
            submitted_reqs.append(req)
            return MagicMock(id=f"ord-{len(submitted_reqs)}")

        client = MagicMock()
        client.submit_order.side_effect = capture
        client.get_all_positions.return_value = []

        # Force MLEG to fail so per-leg fallback runs
        with patch("options_executor._close_spread_mleg", side_effect=Exception("mleg-fail")):
            with patch("options_executor._fetch_live_close_price", return_value=None):
                options_executor.close_structure(struct, client, reason="test_spread_pe", method="limit")

        assert len(submitted_reqs) == 2, f"Expected 2 per-leg orders, got {len(submitted_reqs)}"
        for req in submitted_reqs:
            assert getattr(req, "position_effect", None) == "closing", (
                f"Spread per-leg req missing position_effect='closing': {req}"
            )
