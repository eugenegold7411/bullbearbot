"""
tests/test_s8_phase_b.py — Sprint 8 Phase B verification.

Item 6: _scalar() normaliser in trade_memory.py guards all metadata reads.
Item 4: detect_protection_divergence() grace window for new fills.
Item 5: maybe_trail_stop() PENDING_REPLACE skip + consecutive-failure cap.
"""
from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

# ─────────────────────────────────────────────────────────────────────────────
# Item 6 — ChromaDB metadata _scalar() normaliser
# ─────────────────────────────────────────────────────────────────────────────

class TestScalarHelper(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import trade_memory as tm
        cls._scalar = staticmethod(tm._scalar)

    def test_passthrough_int(self):
        self.assertEqual(self._scalar(42, 0), 42)

    def test_passthrough_float(self):
        self.assertAlmostEqual(self._scalar(3.14, 0.0), 3.14)

    def test_passthrough_str(self):
        self.assertEqual(self._scalar("risk_on", "unknown"), "risk_on")

    def test_list_returns_first_element(self):
        self.assertEqual(self._scalar([42], 0), 42)

    def test_list_float_returns_first_element(self):
        self.assertAlmostEqual(self._scalar([18.5], 0.0), 18.5)

    def test_empty_list_returns_default(self):
        self.assertEqual(self._scalar([], 99), 99)

    def test_none_passthrough(self):
        self.assertIsNone(self._scalar(None, 0))

    def test_nested_list_returns_outer_first(self):
        # Only one level of list unwrapping expected
        result = self._scalar([[1, 2], 3], None)
        self.assertEqual(result, [1, 2])


class TestFormatRetrievedMemoriesTypeGuard(unittest.TestCase):
    """format_retrieved_memories() must not crash when ChromaDB list-wraps scalars."""

    @classmethod
    def setUpClass(cls):
        import trade_memory as tm
        cls._fmt = staticmethod(tm.format_retrieved_memories)

    def _make_scenario(self, **meta_overrides):
        meta = {
            "tier":    "short",
            "ts":      "2026-04-28T10:00:00+00:00",
            "session": "market",
            "vix":     18.5,
            "regime":  "risk_on",
            "symbols": "AAPL,NVDA",
            "outcome": "pending",
            "pnl":     0.0,
        }
        meta.update(meta_overrides)
        return [{"metadata": meta, "document": "reasoning: test", "distance": 0.1,
                 "weighted_score": 0.54}]

    def test_vix_as_list_does_not_crash(self):
        out = self._fmt(self._make_scenario(vix=[18.5]))
        self.assertIn("vix=18.5", out)

    def test_pnl_as_list_non_pending(self):
        out = self._fmt(self._make_scenario(pnl=[123.0], outcome="win"))
        self.assertIn("P&L=", out)

    def test_session_as_list(self):
        out = self._fmt(self._make_scenario(session=["market"]))
        self.assertIn("sess=market", out)

    def test_tier_as_list_short(self):
        # tier="short" → no tag displayed; as list same result
        out = self._fmt(self._make_scenario(tier=["short"]))
        self.assertNotIn("[medium]", out)
        self.assertNotIn("[long]", out)

    def test_tier_as_list_medium(self):
        out = self._fmt(self._make_scenario(tier=["medium"]))
        self.assertIn("[medium]", out)

    def test_outcome_as_list_pending(self):
        out = self._fmt(self._make_scenario(outcome=["pending"]))
        self.assertNotIn("P&L=", out)

    def test_regime_legacy_question_mark_in_list(self):
        out = self._fmt(self._make_scenario(regime=["?"]))
        self.assertIn("regime=unknown", out)

    def test_all_scalar_baseline_unchanged(self):
        """Verify baseline (no lists) still produces valid output."""
        out = self._fmt(self._make_scenario())
        self.assertIn("vix=18.5", out)
        self.assertIn("sess=market", out)


# ─────────────────────────────────────────────────────────────────────────────
# Item 4 — detect_protection_divergence() grace window
# ─────────────────────────────────────────────────────────────────────────────

def _make_position(symbol="AAPL", market_value=10_000.0, qty=100.0):
    pos = MagicMock()
    pos.symbol       = symbol
    pos.market_value = market_value
    pos.qty          = qty
    return pos


def _make_stop_order(symbol="AAPL", status="accepted"):
    o = MagicMock()
    o.symbol     = symbol
    o.order_type = "stop"
    o.status     = status
    o.qty        = 100.0
    return o


class TestProtectionDivergenceGraceWindow(unittest.TestCase):

    def setUp(self):
        import divergence
        # Reset module-level grace-window state before each test so tests
        # don't bleed into each other.
        divergence._fill_seen.clear()

    def _call(self, positions, open_orders, grace_seconds=120.0):
        import divergence
        with patch.object(divergence, "log_divergence_event"):
            with patch.object(divergence, "check_repeat_escalation",
                              side_effect=lambda acct, et, sym, sev: sev):
                return divergence.detect_protection_divergence(
                    account="A1",
                    positions=positions,
                    open_orders=open_orders,
                    vix=18.0,
                    grace_seconds=grace_seconds,
                )

    def test_first_cycle_no_event_grace_started(self):
        """First detection of no stop → no event, grace window started."""
        events = self._call([_make_position("AAPL")], open_orders=[])
        self.assertEqual(events, [])

    def test_second_cycle_within_grace_still_no_event(self):
        """Second cycle within grace window → still no event."""
        import divergence
        divergence._fill_seen["AAPL"] = time.time() - 10  # 10s elapsed, grace=120s
        events = self._call([_make_position("AAPL")], open_orders=[],
                            grace_seconds=120.0)
        self.assertEqual(events, [])

    def test_grace_expired_fires_event(self):
        """Grace window expired → protection_missing event fires."""
        import divergence
        divergence._fill_seen["AAPL"] = time.time() - 200  # 200s > 120s grace
        events = self._call([_make_position("AAPL")], open_orders=[],
                            grace_seconds=120.0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "protection_missing")

    def test_zero_grace_fires_immediately(self):
        """grace_seconds=0 → first cycle with no stop fires immediately."""
        events = self._call([_make_position("AAPL")], open_orders=[],
                            grace_seconds=0.0)
        self.assertEqual(len(events), 1)

    def test_fill_seen_cleared_when_position_closes(self):
        """Position closing (disappearing from positions list) clears _fill_seen."""
        import divergence
        divergence._fill_seen["AAPL"] = time.time() - 10
        # Call with no positions — AAPL has closed
        self._call(positions=[], open_orders=[])
        self.assertNotIn("AAPL", divergence._fill_seen)

    def test_fill_seen_cleared_when_stop_found(self):
        """When a stop is detected for a symbol, _fill_seen is cleared."""
        import divergence
        divergence._fill_seen["AAPL"] = time.time() - 10
        stop_order = _make_stop_order("AAPL", status="accepted")
        self._call([_make_position("AAPL")], open_orders=[stop_order])
        self.assertNotIn("AAPL", divergence._fill_seen)

    def test_reopen_same_symbol_gets_fresh_grace(self):
        """Close + reopen of same symbol → fresh grace window, not stale."""
        import divergence
        # Simulate: AAPL was gracing, now it closes (cleared), then reopens
        divergence._fill_seen["AAPL"] = time.time() - 200  # would have fired
        # Position close cycle
        self._call(positions=[], open_orders=[])
        self.assertNotIn("AAPL", divergence._fill_seen)
        # Position reopen cycle — should start fresh grace, no event
        events = self._call([_make_position("AAPL")], open_orders=[], grace_seconds=120.0)
        self.assertEqual(events, [])
        self.assertIn("AAPL", divergence._fill_seen)

    def test_pending_replace_stop_not_double_counted(self):
        """PENDING_REPLACE stop is excluded from stop_map — existing behaviour unchanged."""
        import divergence
        pending_order = _make_stop_order("AAPL", status="pending_replace")
        # With grace_seconds=0 and PENDING_REPLACE filtered out → event fires
        # (no actual stop visible)
        divergence._fill_seen["AAPL"] = time.time() - 200
        events = self._call([_make_position("AAPL")], open_orders=[pending_order],
                            grace_seconds=0.0)
        self.assertEqual(len(events), 1)

    def test_duplicate_exit_still_fires(self):
        """Two real stop orders (overcoverage) still produce duplicate_exit event."""
        s1 = _make_stop_order("AAPL", status="accepted")
        s1.qty = 200.0  # overcoverage
        s2 = _make_stop_order("AAPL", status="accepted")
        s2.qty = 200.0
        events = self._call([_make_position("AAPL", qty=100.0)],
                            open_orders=[s1, s2])
        self.assertTrue(any(e.event_type == "duplicate_exit" for e in events))


# ─────────────────────────────────────────────────────────────────────────────
# Item 5 — maybe_trail_stop() PENDING_REPLACE skip + failure cap
# ─────────────────────────────────────name────────────────────────────────────

class TestMaybeTrailStopPendingReplace(unittest.TestCase):

    def setUp(self):
        import exit_manager
        exit_manager._trail_replace_failures.clear()

    def _make_position(self, entry=100.0, current=115.0, unreal=1500.0):
        pos = MagicMock()
        pos.symbol           = "AAPL"
        pos.avg_entry_price  = entry
        pos.current_price    = current
        pos.unrealized_pl    = unreal
        return pos

    def _make_ei(self, stop_price=95.0, stop_oid="ord123", stop_status=None):
        return {
            "stop_price":        stop_price,
            "stop_order_id":     stop_oid,
            "stop_order_status": stop_status,
            "target_price":      None,
            "status":            "partial",
        }

    def _cfg(self, max_failures=3):
        return {
            "exit_management": {
                "trail_stop_enabled":          True,
                "trail_trigger_r":             1.0,
                "trail_to_breakeven_plus_pct": 0.005,
                "trail_replace_max_failures":  max_failures,
            }
        }

    def test_pending_replace_returns_false_no_api_call(self):
        """stop_order_status=pending_replace → returns False without calling replace."""
        import exit_manager
        client = MagicMock()
        ei = self._make_ei(stop_status="pending_replace")
        result = exit_manager.maybe_trail_stop(
            self._make_position(), client, self._cfg(), exit_info=ei
        )
        self.assertFalse(result)
        client.replace_order_by_id.assert_not_called()

    def test_normal_status_attempts_replace(self):
        """stop_order_status=accepted → replace is attempted."""
        import exit_manager
        client = MagicMock()
        ei = self._make_ei(stop_status="accepted")
        exit_manager.maybe_trail_stop(
            self._make_position(), client, self._cfg(), exit_info=ei
        )
        client.replace_order_by_id.assert_called_once()

    def test_failure_cap_stops_after_n(self):
        """After max_failures consecutive failures, replace is not attempted."""
        import exit_manager
        client = MagicMock()
        client.replace_order_by_id.side_effect = RuntimeError("API error")
        ei = self._make_ei(stop_status="accepted", stop_oid="ord456")

        # Exhaust the cap
        for _ in range(3):
            exit_manager.maybe_trail_stop(
                self._make_position(), client, self._cfg(max_failures=3), exit_info=ei
            )
        self.assertEqual(exit_manager._trail_replace_failures.get("ord456"), 3)

        # Next call should not attempt replace
        client.replace_order_by_id.reset_mock()
        result = exit_manager.maybe_trail_stop(
            self._make_position(), client, self._cfg(max_failures=3), exit_info=ei
        )
        self.assertFalse(result)
        client.replace_order_by_id.assert_not_called()

    def test_failure_counter_increments_on_each_failure(self):
        import exit_manager
        client = MagicMock()
        client.replace_order_by_id.side_effect = RuntimeError("nope")
        ei = self._make_ei(stop_status="accepted", stop_oid="ord789")

        exit_manager.maybe_trail_stop(
            self._make_position(), client, self._cfg(), exit_info=ei
        )
        self.assertEqual(exit_manager._trail_replace_failures.get("ord789"), 1)

        exit_manager.maybe_trail_stop(
            self._make_position(), client, self._cfg(), exit_info=ei
        )
        self.assertEqual(exit_manager._trail_replace_failures.get("ord789"), 2)

    def test_success_clears_failure_counter(self):
        """Successful replace clears the failure counter for that order."""
        import exit_manager
        client = MagicMock()
        ei = self._make_ei(stop_status="accepted", stop_oid="ord111")

        # Seed a partial failure count
        exit_manager._trail_replace_failures["ord111"] = 2

        result = exit_manager.maybe_trail_stop(
            self._make_position(), client, self._cfg(), exit_info=ei
        )
        self.assertTrue(result)
        self.assertNotIn("ord111", exit_manager._trail_replace_failures)

    def test_new_order_id_starts_fresh_counter(self):
        """Different order IDs have independent failure counters."""
        import exit_manager
        client = MagicMock()
        client.replace_order_by_id.side_effect = RuntimeError("fail")

        ei1 = self._make_ei(stop_status="accepted", stop_oid="ordA")
        ei2 = self._make_ei(stop_status="accepted", stop_oid="ordB")

        exit_manager.maybe_trail_stop(
            self._make_position(), client, self._cfg(), exit_info=ei1
        )
        exit_manager.maybe_trail_stop(
            self._make_position(), client, self._cfg(), exit_info=ei2
        )

        self.assertEqual(exit_manager._trail_replace_failures.get("ordA"), 1)
        self.assertEqual(exit_manager._trail_replace_failures.get("ordB"), 1)


class TestGetActiveExitsStopOrderStatus(unittest.TestCase):
    """get_active_exits() must include stop_order_status in result dict."""

    def _make_stop_order(self, symbol, status_str, stop_price=95.0):
        o = MagicMock()
        o.symbol      = symbol
        o.type        = "stop"
        o.side        = "sell"
        o.status      = status_str
        o.stop_price  = stop_price
        o.limit_price = None
        o.id          = "ord_stop_001"
        return o

    def _make_pos(self, symbol="AAPL", qty=100, price=100.0):
        p = MagicMock()
        p.symbol        = symbol
        p.qty           = qty
        p.current_price = price
        return p

    def test_stop_order_status_present_in_result(self):
        import exit_manager
        client = MagicMock()
        stop_order = self._make_stop_order("AAPL", "accepted")
        client.get_orders.return_value = [stop_order]

        result = exit_manager.get_active_exits([self._make_pos()], client)
        self.assertIn("stop_order_status", result.get("AAPL", {}))
        self.assertEqual(result["AAPL"]["stop_order_status"], "accepted")

    def test_pending_replace_status_captured(self):
        import exit_manager
        client = MagicMock()
        stop_order = self._make_stop_order("AAPL", "pending_replace")
        client.get_orders.return_value = [stop_order]

        result = exit_manager.get_active_exits([self._make_pos()], client)
        self.assertEqual(result["AAPL"]["stop_order_status"], "pending_replace")

    def test_no_stop_order_status_is_none(self):
        import exit_manager
        client = MagicMock()
        client.get_orders.return_value = []

        result = exit_manager.get_active_exits([self._make_pos()], client)
        # No stop found → stop_order_status should be None
        self.assertIsNone(result.get("AAPL", {}).get("stop_order_status"))
