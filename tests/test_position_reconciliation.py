"""
test_position_reconciliation.py — Short position handling correctness.

Suite A: NormalizedPosition.side + BrokerSnapshot.held_symbols
Suite B: _has_stop_order() side-awareness (long vs short)
Suite C: reconciliation diff_state() + execute_reconciliation_plan() for shorts
"""

import sys
import types
import unittest
from typing import Optional


# ── dotenv stub (needed by exit_manager and scheduler imports) ─────────────────
def _ensure_dotenv_stub():
    if "dotenv" not in sys.modules:
        _m = types.ModuleType("dotenv")
        _m.load_dotenv = lambda *a, **kw: None
        sys.modules["dotenv"] = _m


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_position(symbol: str, qty: float):
    """Minimal duck-type object accepted by NormalizedPosition.from_alpaca_position()."""
    class FakePos:
        pass
    pos = FakePos()
    pos.symbol = symbol
    pos.qty = qty
    pos.avg_entry_price = 150.0
    pos.current_price = 148.0
    pos.market_value = qty * 148.0
    pos.unrealized_pl = (148.0 - 150.0) * qty
    pos.unrealized_plpc = -0.013
    return pos


def _make_order(symbol: str, side: str, order_type: str,
                stop_price: Optional[float] = None):
    class FakeOrder:
        pass
    o = FakeOrder()
    o.symbol = symbol
    o.side = side
    o.type = order_type
    o.order_type = order_type
    o.stop_price = stop_price
    o.limit_price = None
    return o


class _NullClient:
    """Truthy mock Alpaca client — raises if any broker call is actually attempted."""
    def submit_order(self, *a, **kw):
        raise RuntimeError("_NullClient: submit_order must not be called in these tests")
    def get_orders(self, *a, **kw):
        return []
    def cancel_order_by_id(self, *a, **kw):
        raise RuntimeError("_NullClient: cancel_order_by_id must not be called in these tests")


# ── Suite A — NormalizedPosition.side + BrokerSnapshot.held_symbols ──────────

class TestNormalizedPositionSide(unittest.TestCase):
    """Suite A — short/long side detection via qty sign."""

    @classmethod
    def setUpClass(cls):
        try:
            from schemas import BrokerSnapshot, NormalizedPosition
            cls.NP = NormalizedPosition
            cls.BS = BrokerSnapshot
        except ImportError as exc:
            raise unittest.SkipTest(f"schemas not importable: {exc}")

    def _pos(self, symbol: str, qty: float):
        return self.NP.from_alpaca_position(_make_position(symbol, qty))

    def test_long_position_side_is_long(self):
        pos = self._pos("GLD", 34.0)
        self.assertEqual(pos.side, "long")

    def test_short_position_side_is_short(self):
        pos = self._pos("TSM", -26.0)
        self.assertEqual(pos.side, "short")

    def test_held_symbols_includes_long(self):
        pos = self._pos("GLD", 34.0)
        snap = self.BS(positions=[pos], open_orders=[], equity=100000, cash=80000,
                       buying_power=180000)
        self.assertIn("GLD", snap.held_symbols)

    def test_held_symbols_includes_short(self):
        """Short positions (negative qty) must appear in held_symbols."""
        pos = self._pos("TSM", -26.0)
        snap = self.BS(positions=[pos], open_orders=[], equity=100000, cash=80000,
                       buying_power=180000)
        self.assertIn("TSM", snap.held_symbols,
                      "held_symbols must include short positions (qty != 0)")

    def test_held_symbols_excludes_zero_qty(self):
        pos = self._pos("SPY", 0.0)
        snap = self.BS(positions=[pos], open_orders=[], equity=100000, cash=80000,
                       buying_power=180000)
        self.assertNotIn("SPY", snap.held_symbols)


# ── Suite B — _has_stop_order() side-awareness ────────────────────────────────

class TestHasStopOrder(unittest.TestCase):
    """Suite B — sell-stop protects long; buy-stop protects short."""

    @classmethod
    def setUpClass(cls):
        _ensure_dotenv_stub()
        try:
            from exit_manager import _has_stop_order
            cls._has_stop_order = staticmethod(_has_stop_order)
        except ImportError as exc:
            raise unittest.SkipTest(f"exit_manager not importable: {exc}")

    def test_sell_stop_protects_long(self):
        orders = [_make_order("TSM", "sell", "stop", stop_price=148.0)]
        self.assertTrue(self._has_stop_order("TSM", orders, is_short=False))

    def test_sell_stop_does_not_protect_short(self):
        """A sell-stop is not protection for a short position."""
        orders = [_make_order("TSM", "sell", "stop", stop_price=148.0)]
        self.assertFalse(self._has_stop_order("TSM", orders, is_short=True),
                         "sell-stop must NOT be counted as protection for a short position")

    def test_buy_stop_protects_short(self):
        """A buy-stop (cover-on-rise) IS protection for a short position."""
        orders = [_make_order("TSM", "buy", "stop", stop_price=165.0)]
        self.assertTrue(self._has_stop_order("TSM", orders, is_short=True),
                        "buy-stop must be counted as protection for a short position")

    def test_buy_stop_does_not_protect_long(self):
        """A buy-stop is not protection for a long position."""
        orders = [_make_order("TSM", "buy", "stop", stop_price=165.0)]
        self.assertFalse(self._has_stop_order("TSM", orders, is_short=False),
                         "buy-stop must NOT be counted as protection for a long position")

    def test_no_orders_returns_false(self):
        self.assertFalse(self._has_stop_order("TSM", []))

    def test_stop_limit_buy_protects_short(self):
        orders = [_make_order("TSM", "buy", "stop_limit", stop_price=165.0)]
        self.assertTrue(self._has_stop_order("TSM", orders, is_short=True))

    def test_default_is_not_short(self):
        """Default is_short=False: sell-stop is found for long."""
        orders = [_make_order("TSM", "sell", "stop")]
        self.assertTrue(self._has_stop_order("TSM", orders))


# ── Suite C — reconciliation short-position guards ────────────────────────────

class TestReconciliationShortGuards(unittest.TestCase):
    """Suite C — diff_state and execute plan correctly handle short positions."""

    @classmethod
    def setUpClass(cls):
        _ensure_dotenv_stub()
        try:
            from reconciliation import (
                ReconciliationAction,
                build_desired_state,
                diff_state,
                execute_reconciliation_plan,
            )
            from schemas import BrokerSnapshot, NormalizedOrder, NormalizedPosition
            cls.NP = NormalizedPosition
            cls.NO = NormalizedOrder
            cls.BS = BrokerSnapshot
            cls.diff_state = staticmethod(diff_state)
            cls.build_desired_state = staticmethod(build_desired_state)
            cls.execute_reconciliation_plan = staticmethod(execute_reconciliation_plan)
            cls.ReconciliationAction = ReconciliationAction
        except ImportError as exc:
            raise unittest.SkipTest(f"reconciliation not importable: {exc}")

    def _make_norm_pos(self, symbol: str, qty: float):
        return self.NP.from_alpaca_position(_make_position(symbol, qty))

    def test_short_with_buy_stop_not_in_missing_stops(self):
        """A short position with a buy-stop order must not be flagged missing_stops."""
        short_pos = self._make_norm_pos("TSM", -26.0)
        buy_stop = self.NO(
            order_id="o1", symbol="TSM", alpaca_sym="TSM",
            side="buy", order_type="stop", qty=26.0, filled_qty=0.0,
            stop_price=165.0, limit_price=None, status="open",
        )
        snap = self.BS(positions=[short_pos], open_orders=[buy_stop],
                       equity=100000, cash=80000, buying_power=180000)
        desired = self.build_desired_state([short_pos], {})
        diff = self.diff_state(desired, snap)
        self.assertNotIn("TSM", diff.missing_stops,
                         "short with buy-stop must not appear in missing_stops")

    def test_short_without_stop_is_in_missing_stops(self):
        """A short position with no stop at all must be flagged missing_stops."""
        short_pos = self._make_norm_pos("TSM", -26.0)
        snap = self.BS(positions=[short_pos], open_orders=[],
                       equity=100000, cash=80000, buying_power=180000)
        desired = self.build_desired_state([short_pos], {})
        diff = self.diff_state(desired, snap)
        self.assertIn("TSM", diff.missing_stops,
                      "short without any stop must appear in missing_stops")

    def test_execute_plan_skips_close_all_for_short(self):
        """execute_reconciliation_plan must skip close_all when qty is negative."""
        action = self.ReconciliationAction(
            priority="CRITICAL",
            action_type="close_all",
            symbol="TSM",
            reason="test",
            qty=-26.0,
        )
        results = self.execute_reconciliation_plan([action], alpaca_client=_NullClient())
        self.assertTrue(
            any("SKIPPED" in r and "TSM" in r for r in results),
            f"expected SKIPPED log for short close_all, got: {results}",
        )

    def test_execute_plan_skips_deadline_exit_for_short(self):
        """execute_reconciliation_plan must skip deadline_exit_market for short positions."""
        action = self.ReconciliationAction(
            priority="CRITICAL",
            action_type="deadline_exit_market",
            symbol="TSM",
            reason="expired_deadline",
            qty=-26.0,
        )
        results = self.execute_reconciliation_plan([action], alpaca_client=_NullClient())
        self.assertTrue(
            any("SKIPPED" in r and "TSM" in r for r in results),
            f"expected SKIPPED log for short deadline_exit_market, got: {results}",
        )

    def test_execute_plan_long_close_calls_client(self):
        """execute_reconciliation_plan with positive qty attempts a real broker call."""
        action = self.ReconciliationAction(
            priority="HIGH",
            action_type="close_all",
            symbol="GLD",
            reason="test",
            qty=34.0,
        )
        # _NullClient raises on submit_order — expect an error result, not SKIPPED
        results = self.execute_reconciliation_plan([action], alpaca_client=_NullClient())
        self.assertFalse(
            any("SKIPPED" in r and "GLD" in r for r in results),
            "long close_all must NOT produce a SKIPPED message",
        )
        self.assertTrue(
            any("ERROR" in r and "GLD" in r for r in results),
            f"long close_all must produce an ERROR result (no real client), got: {results}",
        )


if __name__ == "__main__":
    unittest.main()
