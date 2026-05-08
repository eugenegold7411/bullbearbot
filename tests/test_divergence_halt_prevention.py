"""
tests/test_divergence_halt_prevention.py

Tests for the _pending_close_syms fix in divergence.py.

Root cause: when _sell_cancel_stop_and_sell cancels an OCO and submits a market
sell while the market is closed, the position persists with no stop and the
pending sell sits as status=accepted. divergence.py's stop_map only recognised
stop/stop_limit/trailing_stop/oco/bracket orders — a plain market sell was
invisible, causing repeated protection_missing HALT events overnight.

Fix: after building stop_map, collect symbols where pending SELL qty ≥ 95% of
position qty and skip the protection check for those positions.

Also tests: exit_manager OCO repair returns False gracefully when stop_price=None.
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

import divergence as div
from divergence import detect_protection_divergence

# ── helpers ───────────────────────────────────────────────────────────────────

def _pos(symbol: str, qty: float = 100.0, market_value: float = 10000.0):
    p = MagicMock()
    p.symbol = symbol
    p.qty = str(qty)
    p.market_value = market_value
    return p


def _order(
    symbol: str,
    side: str = "sell",
    order_type: str = "stop",
    order_class: str = "simple",
    status: str = "accepted",
    qty: float = 100.0,
    stop_price: float | None = 95.0,
    order_id: str = "ord-001",
):
    o = MagicMock()
    o.symbol = symbol
    o.side = side
    o.order_type = order_type
    o.order_class = order_class
    o.status = status
    o.qty = str(qty)
    o.stop_price = stop_price
    o.id = order_id
    return o


class _Base(unittest.TestCase):
    """Clear module-level divergence state and bypass startup/grace windows."""

    def setUp(self):
        div._fill_seen.clear()
        div._protection_miss_cycles.clear()
        self._startup_patch = patch.object(div, "_STARTUP_EPOCH", time.time() - 10_000)
        self._startup_patch.start()

    def tearDown(self):
        self._startup_patch.stop()
        div._fill_seen.clear()
        div._protection_miss_cycles.clear()


# ── BUG fix: pending close suppresses halt ────────────────────────────────────

class TestPendingCloseSupressesHalt(_Base):

    def test_market_sell_pending_no_halt(self):
        """STNG case: accepted market sell covers full qty → protection_missing suppressed."""
        pos = _pos("STNG", qty=188.0, market_value=5500.0)
        sell = _order("STNG", side="sell", order_type="market", qty=188.0, stop_price=None)
        events = detect_protection_divergence("paper", [pos], [sell], grace_seconds=0)
        self.assertEqual(events, [])

    def test_95pct_threshold_suppresses_halt(self):
        """Sell covers exactly 95% of position qty → treated as full close."""
        pos = _pos("XLE", qty=100.0, market_value=6000.0)
        sell = _order("XLE", side="sell", order_type="market", qty=95.0, stop_price=None)
        events = detect_protection_divergence("paper", [pos], [sell], grace_seconds=0)
        self.assertEqual(events, [])

    def test_94pct_qty_fires_halt(self):
        """Sell covers only 94% → below threshold → protection_missing fires."""
        pos = _pos("XLE", qty=100.0, market_value=6000.0)
        sell = _order("XLE", side="sell", order_type="market", qty=94.0, stop_price=None)
        div._fill_seen["XLE"] = time.time() - 1000
        div._protection_miss_cycles["XLE"] = 1
        events = detect_protection_divergence("paper", [pos], [sell], grace_seconds=0)
        self.assertTrue(any(e.event_type in ("protection_missing", "stop_missing") for e in events))

    def test_canceled_sell_not_counted_as_protection(self):
        """Canceled sell must not count — halt should still fire."""
        pos = _pos("GOOGL", qty=50.0, market_value=10000.0)
        canceled = _order("GOOGL", side="sell", order_type="market", qty=50.0,
                          status="canceled", stop_price=None)
        div._fill_seen["GOOGL"] = time.time() - 1000
        div._protection_miss_cycles["GOOGL"] = 1
        events = detect_protection_divergence("paper", [pos], [canceled], grace_seconds=0)
        self.assertTrue(any(e.event_type in ("protection_missing", "stop_missing") for e in events))

    def test_pending_close_clears_stale_miss_counter(self):
        """Pre-existing miss counter must be reset when pending close is detected."""
        div._fill_seen["MA"] = time.time() - 1000
        div._protection_miss_cycles["MA"] = 1
        pos = _pos("MA", qty=91.0, market_value=48000.0)
        sell = _order("MA", side="sell", order_type="market", qty=91.0, stop_price=None)
        events = detect_protection_divergence("paper", [pos], [sell], grace_seconds=0)
        self.assertEqual(events, [])
        self.assertNotIn("MA", div._protection_miss_cycles)
        self.assertNotIn("MA", div._fill_seen)


# ── Normal stop-exists and unprotected paths ──────────────────────────────────

class TestStopMapNormalPaths(_Base):

    def test_no_halt_when_position_has_stop_order(self):
        """Position with a live stop order → no events, debug log path taken."""
        pos = _pos("AAPL", qty=50.0, market_value=10000.0)
        stop = _order("AAPL", side="sell", order_type="stop", status="new",
                      qty=50.0, stop_price=180.0)
        events = detect_protection_divergence("paper", [pos], [stop], grace_seconds=0)
        self.assertEqual(events, [])

    def test_halt_fires_when_genuinely_unprotected(self):
        """No orders at all → protection_missing fires after miss counter reaches 2."""
        pos = _pos("TSLA", qty=20.0, market_value=5000.0)
        div._fill_seen["TSLA"] = time.time() - 1000
        div._protection_miss_cycles["TSLA"] = 1
        events = detect_protection_divergence("paper", [pos], [], grace_seconds=0)
        self.assertTrue(any(e.event_type in ("protection_missing", "stop_missing") for e in events))

    def test_pending_close_wins_over_buy_side_orders(self):
        """Buy-side order alone would fire halt — but pending sell covering full qty suppresses it."""
        pos = _pos("NVDA", qty=30.0, market_value=15000.0)
        buy = _order("NVDA", side="buy", order_type="market", qty=30.0)
        sell = _order("NVDA", side="sell", order_type="market", qty=30.0,
                      stop_price=None, order_id="close-ord")
        events = detect_protection_divergence("paper", [pos], [buy, sell], grace_seconds=0)
        self.assertEqual(events, [])


# ── exit_manager: OCO repair skips when stop_price=None ──────────────────────

class TestExitManagerOcoRepairStopNone(unittest.TestCase):

    def test_returns_false_without_crash_when_stop_none(self):
        """refresh_exits_for_position must return False, not raise TypeError, when stop_price=None."""
        import exit_manager as em

        pos = MagicMock()
        pos.symbol = "STNG"
        pos.qty = "188"
        pos.current_price = "29.50"

        live_pos = MagicMock()
        live_pos.current_price = "29.50"
        live_pos.qty = "188"

        alpaca = MagicMock()
        alpaca.get_open_position.return_value = live_pos

        strategy_config: dict = {}  # _em_config uses _DEFAULT_CFG fallbacks

        exit_info = {
            "status": "partial",     # is_tp_missing = True
            "stop_price": None,      # stop_at will be None → crashes without the fix
            "stop_order_id": "close-ord-001",
        }

        with patch.object(em, "generate_exit_plan", return_value={"take_profit": 35.0}):
            result = em.refresh_exits_for_position(
                pos, alpaca, strategy_config,
                conviction="medium", exit_info=exit_info,
            )

        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
