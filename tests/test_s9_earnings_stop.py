"""
S9 — Earnings-aware stop floor tests (exit_manager.py Item 5).

Tests verify that maybe_trail_stop() optionally widens the trailing stop to an
IV-based floor when earnings are imminent (eda <= eda_trigger), ensuring the
position is not stopped out by the earnings-day volatility swing.

Seven test cases as specified:
  EA1 — eda=1, IV=0.20, floor > stop_price → stop widened to earnings_floor
  EA2 — eda=1, IV unavailable → normal trail stop unchanged
  EA3 — eda=0 (earnings today), IV=0.25 → stop widened using IV
  EA4 — eda=-1 (post-earnings) → normal trail stop (0 <= -1 is False)
  EA5 — eda=5 (> eda_trigger=1) → normal trail stop
  EA6 — earnings_aware_stop_enabled: false → earnings path skipped entirely
  EA7 — IV=0.01 (trivial) → iv_floor=0.05 clamps expected_move; stop = entry * 0.95

Note on condition: The spec proposed `earnings_floor > new_stop` which is
logically impossible (floor = entry*(1-iv) is always below entry, while
new_stop = entry*(1+plus_pct) is above entry). Implemented as
`earnings_floor > stop_price` which matches the test description
"stop widened to $280 if $280 > current_stop".
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_position(sym="GOOGL", entry=350.0, current=430.0, stop_dist=80.0, qty=10):
    """
    Build a mock position whose profit_r = (current-entry)/stop_dist.

    With entry=350, current=430, stop_dist=80: profit_r = 80/80 = 1.0 = trigger.
    """
    pos = MagicMock()
    pos.symbol = sym
    pos.avg_entry_price = str(entry)
    pos.current_price = str(current)
    pos.unrealized_pl = str((current - entry) * qty)
    pos.qty = str(qty)
    return pos


def _make_exit_info(stop_price=270.0, stop_oid="oid-001"):
    return {
        "stop_price": stop_price,
        "stop_order_id": stop_oid,
        "stop_order_status": "accepted",
        "target_price": None,
    }


def _em_cfg(**overrides) -> dict:
    base = {
        "exit_management": {
            "trail_stop_enabled": True,
            "trail_trigger_r": 1.0,
            "trail_to_breakeven_plus_pct": 0.005,
            "trail_cancel_replace_enabled": False,
            "trail_replace_max_failures": 3,
            "earnings_aware_stop_enabled": True,
            "earnings_stop_eda_trigger": 1,
            "earnings_stop_iv_floor_pct": 0.05,
        }
    }
    base["exit_management"].update(overrides)
    return base


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEarningsAwareStopFloor(unittest.TestCase):
    """EA1–EA7: earnings-aware stop floor in maybe_trail_stop()."""

    def _run(self, pos, exit_info, cfg, eda_return, iv_return):
        """
        Run maybe_trail_stop() with _get_eda and _get_latest_iv patched.
        Returns the stop_price passed to replace_order_by_id (or None if not called).
        """

        from exit_manager import maybe_trail_stop

        alpaca = MagicMock()

        with patch("exit_manager._get_eda", return_value=eda_return), \
             patch("exit_manager._get_latest_iv", return_value=iv_return):
            maybe_trail_stop(pos, alpaca, cfg, exit_info=exit_info)

        if alpaca.replace_order_by_id.called:
            _, kwargs_or_args = alpaca.replace_order_by_id.call_args
            # positional: (order_id, ReplaceOrderRequest(...))
            args = alpaca.replace_order_by_id.call_args[0]
            req = args[1]
            return req.stop_price
        return None

    def test_ea1_stop_widened_when_floor_above_current_stop(self):
        """EA1: eda=1, IV=0.20, entry=$350, stop=$270 → floor=$280 > $270 → widened to $280."""
        pos = _make_position(entry=350.0, current=430.0, stop_dist=80.0)
        ei  = _make_exit_info(stop_price=270.0)
        cfg = _em_cfg()

        placed = self._run(pos, ei, cfg, eda_return=1, iv_return=0.20)

        # floor = 350 * (1 - max(0.20, 0.05)) = 350 * 0.80 = 280.0
        self.assertIsNotNone(placed, "Expected a stop-replace order to be placed")
        self.assertAlmostEqual(placed, 280.0, places=1)

    def test_ea2_iv_unavailable_normal_trail(self):
        """EA2: eda=1, IV=None (no file) → normal trail stop at entry*(1+0.005)."""
        pos = _make_position(entry=350.0, current=430.0, stop_dist=80.0)
        ei  = _make_exit_info(stop_price=270.0)
        cfg = _em_cfg()

        placed = self._run(pos, ei, cfg, eda_return=1, iv_return=None)

        # Normal trail target = 350 * 1.005 = 351.75
        self.assertIsNotNone(placed)
        self.assertAlmostEqual(placed, 351.75, places=1)

    def test_ea3_eda_zero_earnings_today_widens(self):
        """EA3: eda=0 (earnings today), IV=0.25 → floor = entry*(1-0.25) = entry*0.75."""
        entry = 400.0
        stop  = 295.0  # below floor of 400*0.75=300; stop_dist=105
        # current=510 so profit_r=(510-400)/105=1.047 >= trigger_r=1.0
        pos = _make_position(entry=entry, current=510.0, stop_dist=105.0)
        ei  = _make_exit_info(stop_price=stop)
        cfg = _em_cfg()

        placed = self._run(pos, ei, cfg, eda_return=0, iv_return=0.25)

        # floor = 400 * (1 - max(0.25, 0.05)) = 400 * 0.75 = 300.0
        self.assertIsNotNone(placed)
        self.assertAlmostEqual(placed, 300.0, places=1)

    def test_ea4_post_earnings_normal_trail(self):
        """EA4: eda=-1 (post-earnings) → 0 <= -1 is False → normal trail."""
        pos = _make_position(entry=350.0, current=430.0, stop_dist=80.0)
        ei  = _make_exit_info(stop_price=270.0)
        cfg = _em_cfg()

        placed = self._run(pos, ei, cfg, eda_return=-1, iv_return=0.20)

        # Normal trail target = 350 * 1.005 = 351.75
        self.assertIsNotNone(placed)
        self.assertAlmostEqual(placed, 351.75, places=1)

    def test_ea5_eda_beyond_trigger_normal_trail(self):
        """EA5: eda=5 > eda_trigger=1 → earnings path skipped → normal trail."""
        pos = _make_position(entry=350.0, current=430.0, stop_dist=80.0)
        ei  = _make_exit_info(stop_price=270.0)
        cfg = _em_cfg(earnings_stop_eda_trigger=1)

        placed = self._run(pos, ei, cfg, eda_return=5, iv_return=0.20)

        self.assertIsNotNone(placed)
        self.assertAlmostEqual(placed, 351.75, places=1)

    def test_ea6_feature_disabled_normal_trail(self):
        """EA6: earnings_aware_stop_enabled=false → entire earnings block skipped."""
        pos = _make_position(entry=350.0, current=430.0, stop_dist=80.0)
        ei  = _make_exit_info(stop_price=270.0)
        cfg = _em_cfg(earnings_aware_stop_enabled=False)

        placed = self._run(pos, ei, cfg, eda_return=1, iv_return=0.20)

        # earnings block never fires; normal trail at 351.75
        self.assertIsNotNone(placed)
        self.assertAlmostEqual(placed, 351.75, places=1)

    def test_ea7_trivial_iv_clamped_by_floor(self):
        """EA7: IV=0.01 < iv_floor=0.05 → expected_move clamped to 0.05 → stop = entry*0.95."""
        entry = 350.0
        stop  = 325.0  # below floor = 350*0.95 = 332.50
        pos = _make_position(entry=entry, current=430.0, stop_dist=80.0)
        ei  = _make_exit_info(stop_price=stop)
        cfg = _em_cfg(earnings_stop_iv_floor_pct=0.05)

        placed = self._run(pos, ei, cfg, eda_return=1, iv_return=0.01)

        # floor = 350 * (1 - max(0.01, 0.05)) = 350 * 0.95 = 332.50
        self.assertIsNotNone(placed)
        self.assertAlmostEqual(placed, 332.50, places=1)


if __name__ == "__main__":
    unittest.main()
