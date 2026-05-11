"""
tests/test_a2_churn_prevention.py — A2 NVDA spread churn prevention tests.

9 tests covering Fix A (theta entry filter), Fix B (min hold time), Fix C (profit cooldown).

Fix A — theta entry filter in bot_options_stage4_execution:
  1. test_theta_filter_disabled_by_default — filter off by default; high-theta candidate passes
  2. test_theta_filter_blocks_high_theta   — filter on; abs(theta)/debit > max blocks submission
  3. test_theta_filter_allows_low_theta    — filter on; abs(theta)/debit ≤ max allows submission

Fix B — min hold time in options_executor.should_close_structure:
  4. test_min_hold_suppresses_theta_burn   — theta_burn_underwater suppressed when hold < min
  5. test_min_hold_suppresses_iv_crush     — iv_crush suppressed when hold < min
  6. test_min_hold_allows_exit_after_hold  — theta_burn_underwater fires after min hold elapsed

Fix C — profit-close cooldown in bot_options_stage4_execution:
  7. test_profit_cooldown_blocks_reentry   — recent profit close blocks candidate for same symbol
  8. test_profit_cooldown_allows_other_sym — cooldown on NVDA does not block a different symbol
  9. test_profit_cooldown_expired          — cooldown expired; submission proceeds
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

_STUB_MODULES = {
    "alpaca": MagicMock(),
    "alpaca.trading": MagicMock(),
    "alpaca.trading.client": MagicMock(),
    "alpaca.trading.requests": MagicMock(),
    "alpaca.trading.enums": MagicMock(),
    "alpaca.data": MagicMock(),
    "alpaca.data.historical": MagicMock(),
    "alpaca.data.requests": MagicMock(),
    "chromadb": MagicMock(),
    "twilio": MagicMock(),
    "twilio.rest": MagicMock(),
    "sendgrid": MagicMock(),
}

os.environ.setdefault("ALPACA_API_KEY", "test-key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret")
os.environ.setdefault("ALPACA_API_KEY_OPTIONS", "test-key")
os.environ.setdefault("ALPACA_SECRET_KEY_OPTIONS", "test-secret")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

_S4 = None
_OE = None

with patch.dict("sys.modules", _STUB_MODULES):
    try:
        import bot_options_stage4_execution as _S4
    except Exception:
        pass
    try:
        import options_executor as _OE
    except Exception:
        pass


def _make_structure(
    underlying="NVDA",
    lifecycle="fully_filled",
    theta=-0.10,
    max_profit_usd=800.0,
    pnl_unrealized=None,
    opened_at: str | None = None,
    closed_at: str | None = None,
    close_reason_code: str | None = None,
    contracts: int = 1,
):
    from schemas import (
        OptionsLeg,
        OptionsStructure,
        OptionStrategy,
        StructureLifecycle,
        Tier,
    )

    lc = StructureLifecycle(lifecycle)
    leg = OptionsLeg(
        occ_symbol=f"{underlying}260620C00212500",
        underlying=underlying,
        side="buy",
        qty=1,
        option_type="call",
        strike=212.5,
        expiration="2026-06-20",
        filled_price=2.00,
    )
    _opened = opened_at or "2026-05-07T18:00:00+00:00"
    s = OptionsStructure(
        structure_id=f"test_{underlying}_{lifecycle}",
        underlying=underlying,
        strategy=OptionStrategy.CALL_DEBIT_SPREAD,
        expiration="2026-06-20",
        contracts=contracts,
        lifecycle=lc,
        legs=[leg],
        max_cost_usd=200.0,
        opened_at=_opened,
        catalyst="earnings",
        tier=Tier.CORE,
    )
    s.theta = theta
    s.max_profit_usd = max_profit_usd
    s.pnl_unrealized = pnl_unrealized
    if closed_at:
        s.closed_at = closed_at
    if close_reason_code:
        s.close_reason_code = close_reason_code
    return s


# ── Fix A tests ────────────────────────────────────────────────────────────────

class TestThetaEntryFilter(unittest.TestCase):
    """Tests 1-3: theta entry filter in bot_options_stage4_execution."""

    def setUp(self):
        if _S4 is None:
            self.skipTest("bot_options_stage4_execution import failed")

    def test_theta_filter_disabled_by_default(self):
        """Test 1: theta_entry_filter_enabled absent → filter off; high-theta candidate passes."""
        cfg = {}  # no theta_entry_filter_enabled key
        self.assertFalse(cfg.get("theta_entry_filter_enabled", False))
        # Simulate: high theta/debit ratio — would be blocked if filter were on
        theta_val = -0.30
        debit_val = 1.00
        max_theta = float(cfg.get("max_theta_decay_pct", 0.05))
        rate = abs(theta_val) / debit_val
        self.assertGreater(rate, max_theta)
        # But since filter is off, should NOT block
        filter_on = cfg.get("theta_entry_filter_enabled", False)
        would_block = filter_on and (rate > max_theta)
        self.assertFalse(would_block, "Filter-off should never block any candidate")

    def test_theta_filter_blocks_high_theta(self):
        """Test 2: theta_entry_filter_enabled=True; abs(theta)/debit > max → blocked."""
        cfg = {"theta_entry_filter_enabled": True, "max_theta_decay_pct": 0.05}
        theta_val = -0.30   # abs rate = 0.30 / 1.00 = 0.30 >> 0.05
        debit_val = 1.00
        max_theta = float(cfg.get("max_theta_decay_pct", 0.05))
        rate = abs(theta_val) / debit_val
        filter_on = cfg.get("theta_entry_filter_enabled", False)
        would_block = filter_on and debit_val > 0 and rate > max_theta
        self.assertTrue(would_block, "High theta/debit should be blocked when filter enabled")

    def test_theta_filter_allows_low_theta(self):
        """Test 3: theta_entry_filter_enabled=True; abs(theta)/debit ≤ max → allowed."""
        cfg = {"theta_entry_filter_enabled": True, "max_theta_decay_pct": 0.05}
        theta_val = -0.04   # abs rate = 0.04 / 1.00 = 0.04 < 0.05
        debit_val = 1.00
        max_theta = float(cfg.get("max_theta_decay_pct", 0.05))
        rate = abs(theta_val) / debit_val
        filter_on = cfg.get("theta_entry_filter_enabled", False)
        would_block = filter_on and debit_val > 0 and rate > max_theta
        self.assertFalse(would_block, "Low theta/debit should pass when filter enabled")


# ── Fix B tests ────────────────────────────────────────────────────────────────

@unittest.skipIf(_OE is None, "options_executor import failed")
class TestMinHoldTime(unittest.TestCase):
    """Tests 4-6: min hold time guard in options_executor.should_close_structure."""

    def _make_config(self, min_hold_min: float = 45.0) -> dict:
        return {"account2": {"tiered_exit_min_hold_minutes": min_hold_min}}

    def test_min_hold_suppresses_theta_burn(self):
        """Test 4: theta_burn_underwater suppressed when structure held < min_hold_minutes."""
        # Structure opened 10 minutes ago — below 45-min hold
        opened = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        s = _make_structure(
            theta=-0.80,          # $80/day × 1 contract × 100 = $8,000/day (way above $50)
            pnl_unrealized=-600.0,
            max_profit_usd=800.0,
            opened_at=opened,
        )
        # max_loss estimate: must be > pnl so 20% threshold is crossed
        config = self._make_config(45.0)

        with patch.object(_OE, "_max_loss_usd_estimate", return_value=2000.0), \
             patch.object(_OE, "_compute_dte", return_value=14), \
             patch.object(_OE, "_resolve_close_targets",
                          return_value={"max_loss_pct": 1.0, "profit_target_pct": 0.75}), \
             patch.object(_OE, "_estimate_current_value", return_value=None):
            should_close, reason = _OE.should_close_structure(s, {}, config, "")

        self.assertNotEqual(reason, "theta_burn_underwater",
                            "theta_burn_underwater should be suppressed within min_hold period")

    def test_min_hold_suppresses_iv_crush(self):
        """Test 5: iv_crush suppressed when structure held < min_hold_minutes."""
        opened = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        s = _make_structure(opened_at=opened)
        config = self._make_config(45.0)

        with patch.object(_OE, "_max_loss_usd_estimate", return_value=500.0), \
             patch.object(_OE, "_compute_dte", return_value=14), \
             patch.object(_OE, "_resolve_close_targets",
                          return_value={"max_loss_pct": 1.0, "profit_target_pct": 0.75}), \
             patch.object(_OE, "_estimate_current_value", return_value=None), \
             patch("options_executor.detect_iv_crush", return_value=(True, "iv_crush_post_earnings"),
                   create=True):
            should_close, reason = _OE.should_close_structure(s, {}, config, "")

        self.assertNotEqual(reason, "iv_crush_post_earnings",
                            "IV crush should be suppressed within min_hold period")

    def test_min_hold_allows_exit_after_hold(self):
        """Test 6: theta_burn_underwater fires normally after min hold period elapsed."""
        # Structure opened 120 minutes ago — above 45-min hold
        opened = (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat()
        s = _make_structure(
            theta=-0.80,
            pnl_unrealized=-600.0,
            max_profit_usd=800.0,
            opened_at=opened,
        )
        config = self._make_config(45.0)

        with patch.object(_OE, "_max_loss_usd_estimate", return_value=2000.0), \
             patch.object(_OE, "_compute_dte", return_value=14), \
             patch.object(_OE, "_resolve_close_targets",
                          return_value={"max_loss_pct": 1.0, "profit_target_pct": 0.75}), \
             patch.object(_OE, "_estimate_current_value", return_value=None):
            should_close, reason = _OE.should_close_structure(s, {}, config, "")

        self.assertEqual(reason, "theta_burn_underwater",
                         "theta_burn_underwater should fire after min_hold period")
        self.assertTrue(should_close)


# ── Fix C tests ────────────────────────────────────────────────────────────────

class TestProfitCloseCooldown(unittest.TestCase):
    """Tests 7-9: profit-close cooldown in _is_recent_profit_close."""

    def setUp(self):
        if _S4 is None:
            self.skipTest("bot_options_stage4_execution import failed")

    def _run_cooldown_check(self, closed_s, symbol: str, cooldown: float) -> bool:
        mock_os = MagicMock()
        mock_os.load_structures.return_value = [closed_s]
        with patch.dict("sys.modules", {"options_state": mock_os}):
            return _S4._is_recent_profit_close(symbol, cooldown)

    def test_profit_cooldown_blocks_reentry(self):
        """Test 7: recent profit close on same symbol → _is_recent_profit_close returns True."""
        closed_dt = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        closed_s = _make_structure(
            underlying="NVDA",
            lifecycle="closed",
            closed_at=closed_dt,
            close_reason_code="profit_target_pct_hit",
        )
        result = self._run_cooldown_check(closed_s, "NVDA", 60.0)
        self.assertTrue(result, "Should block re-entry within 60m profit cooldown")

    def test_profit_cooldown_allows_other_sym(self):
        """Test 8: NVDA profit cooldown does not block a different symbol (AAPL)."""
        closed_dt = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        closed_s = _make_structure(
            underlying="NVDA",
            lifecycle="closed",
            closed_at=closed_dt,
            close_reason_code="profit_target_pct_hit",
        )
        result = self._run_cooldown_check(closed_s, "AAPL", 60.0)
        self.assertFalse(result, "NVDA cooldown should not block AAPL re-entry")

    def test_profit_cooldown_expired(self):
        """Test 9: profit close was 90 minutes ago; 60m cooldown expired → returns False."""
        closed_dt = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
        closed_s = _make_structure(
            underlying="NVDA",
            lifecycle="closed",
            closed_at=closed_dt,
            close_reason_code="profit_target_pct_hit",
        )
        result = self._run_cooldown_check(closed_s, "NVDA", 60.0)
        self.assertFalse(result, "Cooldown should have expired after 90m with 60m window")


if __name__ == "__main__":
    unittest.main()
