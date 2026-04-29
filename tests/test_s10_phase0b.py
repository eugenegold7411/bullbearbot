"""
tests/test_s10_phase0b.py — Sprint 10 Phase 0B tests

Fix 1: _cleanup_stale_proposed_structures()
  - PROPOSED > 2h, no order_ids → cancelled
  - PROPOSED < 2h → not cancelled
  - PROPOSED with order_ids → not cancelled (may be in-flight)
  - SUBMITTED > 2h → not touched (different lifecycle handler)
  - FULLY_FILLED → not touched

Fix 2: _EARNINGS_EXEMPT_SYMBOLS + _load_earnings_days_away()
  - ETF/fund symbol → None (exempt, no file lookup)
  - Equity in calendar → correct days_away
  - Non-exempt equity not in calendar → None
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

# ── helpers ───────────────────────────────────────────────────────────────────

def _make_structure(lifecycle_str: str, order_ids: list, age_hours: float):
    """Build a minimal OptionsStructure with the given lifecycle and age."""
    from schemas import (
        OptionsLeg,
        OptionsStructure,
        OptionStrategy,
        StructureLifecycle,
        Tier,
    )
    lc = StructureLifecycle(lifecycle_str)
    opened_at = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()
    leg = OptionsLeg(
        occ_symbol="AAPL260515C00200000",
        underlying="AAPL",
        side="buy",
        qty=1,
        option_type="call",
        strike=200.0,
        expiration="2026-05-15",
    )
    s = OptionsStructure(
        structure_id=f"test-{lifecycle_str}-{int(age_hours)}h",
        underlying="AAPL",
        strategy=OptionStrategy.SINGLE_CALL,
        lifecycle=lc,
        legs=[leg],
        contracts=1,
        max_cost_usd=300.0,
        opened_at=opened_at,
        catalyst="test",
        tier=Tier.CORE,
        order_ids=list(order_ids),
    )
    return s


# ── Fix 1: stale PROPOSED cleanup ─────────────────────────────────────────────

class TestCleanupStaleProposed(unittest.TestCase):

    def _run_cleanup(self, structures, max_age_hours=2.0):
        """Run cleanup with save_structure mocked out."""
        from bot_options_stage0_preflight import _cleanup_stale_proposed_structures
        with patch("options_state.save_structure"):
            return _cleanup_stale_proposed_structures(structures, max_age_hours)

    def test_stale_proposed_no_orderids_cancelled(self):
        """PROPOSED, age 3h, empty order_ids → cancelled (count=1)."""
        s = _make_structure("proposed", [], age_hours=3.0)
        count = self._run_cleanup([s])
        self.assertEqual(count, 1)
        from schemas import StructureLifecycle
        self.assertEqual(s.lifecycle, StructureLifecycle.CANCELLED)

    def test_fresh_proposed_not_cancelled(self):
        """PROPOSED, age 1h → not cancelled (count=0)."""
        s = _make_structure("proposed", [], age_hours=1.0)
        count = self._run_cleanup([s])
        self.assertEqual(count, 0)
        from schemas import StructureLifecycle
        self.assertEqual(s.lifecycle, StructureLifecycle.PROPOSED)

    def test_proposed_with_orderids_not_cancelled(self):
        """PROPOSED with order_ids → not cancelled (may be in-flight)."""
        s = _make_structure("proposed", ["some-order-id"], age_hours=5.0)
        count = self._run_cleanup([s])
        self.assertEqual(count, 0)
        from schemas import StructureLifecycle
        self.assertEqual(s.lifecycle, StructureLifecycle.PROPOSED)

    def test_submitted_not_touched(self):
        """SUBMITTED, age 3h → not touched (handled by _sync_submitted_lifecycles)."""
        s = _make_structure("submitted", ["some-order-id"], age_hours=3.0)
        count = self._run_cleanup([s])
        self.assertEqual(count, 0)
        from schemas import StructureLifecycle
        self.assertEqual(s.lifecycle, StructureLifecycle.SUBMITTED)

    def test_fully_filled_not_touched(self):
        """FULLY_FILLED → not touched."""
        s = _make_structure("fully_filled", ["some-order-id"], age_hours=48.0)
        count = self._run_cleanup([s])
        self.assertEqual(count, 0)
        from schemas import StructureLifecycle
        self.assertEqual(s.lifecycle, StructureLifecycle.FULLY_FILLED)

    def test_audit_log_entry_added_on_cancel(self):
        """Cancelled structure gets an audit log entry explaining why."""
        s = _make_structure("proposed", [], age_hours=3.0)
        self._run_cleanup([s])
        self.assertTrue(s.audit_log, "audit_log should be non-empty after cancel")
        last_msg = s.audit_log[-1]["msg"]
        self.assertIn("auto-cancelled", last_msg)
        self.assertIn("stale proposed", last_msg)

    def test_empty_structures_list(self):
        """Empty input → count=0, no errors."""
        count = self._run_cleanup([])
        self.assertEqual(count, 0)

    def test_mixed_structures_only_stale_proposed_cancelled(self):
        """Mixed list — only the one stale PROPOSED with no order_ids is cancelled."""
        stale   = _make_structure("proposed",     [],               age_hours=3.0)
        fresh   = _make_structure("proposed",     [],               age_hours=1.0)
        inflight = _make_structure("proposed",    ["ord-123"],      age_hours=3.0)
        subm    = _make_structure("submitted",    ["ord-456"],      age_hours=3.0)
        filled  = _make_structure("fully_filled", ["ord-789"],      age_hours=48.0)

        count = self._run_cleanup([stale, fresh, inflight, subm, filled])
        self.assertEqual(count, 1)

        from schemas import StructureLifecycle
        self.assertEqual(stale.lifecycle,    StructureLifecycle.CANCELLED)
        self.assertEqual(fresh.lifecycle,    StructureLifecycle.PROPOSED)
        self.assertEqual(inflight.lifecycle, StructureLifecycle.PROPOSED)
        self.assertEqual(subm.lifecycle,     StructureLifecycle.SUBMITTED)
        self.assertEqual(filled.lifecycle,   StructureLifecycle.FULLY_FILLED)


# ── Fix 2: earnings exempt symbols ────────────────────────────────────────────

class TestEarningsExemptSymbols(unittest.TestCase):

    def test_etf_returns_none_without_file_lookup(self):
        """ETF symbol (SPY) → None immediately, no calendar file read."""
        from bot_options_stage1_candidates import _load_earnings_days_away
        # Patch Path.exists to raise so any file lookup would crash — exempt
        # symbols must never reach the file.
        with patch("pathlib.Path.exists", side_effect=AssertionError("should not read file")):
            result = _load_earnings_days_away("SPY")
        self.assertIsNone(result)

    def test_all_19_etfs_are_exempt(self):
        """All 19 ETF/fund symbols are in _EARNINGS_EXEMPT_SYMBOLS."""
        from bot_options_stage1_candidates import _EARNINGS_EXEMPT_SYMBOLS
        expected = {
            "COPX", "ECH", "EEM", "EWJ", "EWM", "FXI",
            "GLD", "ITA", "IWM", "QQQ", "SLV", "SPY",
            "TLT", "USO", "VXX", "XBI", "XLE", "XLF", "XRT",
        }
        self.assertEqual(_EARNINGS_EXEMPT_SYMBOLS, expected)

    def test_equity_in_calendar_returns_days_away(self):
        """Non-exempt equity present in calendar → correct days_away."""
        from datetime import date

        from bot_options_stage1_candidates import _load_earnings_days_away
        future = (date.today() + timedelta(days=5)).isoformat()
        cal_data = json.dumps({
            "calendar": [{"symbol": "AAPL", "earnings_date": future, "timing": "post-market"}]
        })
        with patch("bot_options_stage1_candidates.Path") as MockPath:
            instance = MagicMock()
            MockPath.return_value = instance
            cal_mock = instance.parent.__truediv__.return_value.__truediv__.return_value.__truediv__.return_value
            cal_mock.exists.return_value = True
            cal_mock.read_text.return_value = cal_data
            result = _load_earnings_days_away("AAPL")
        self.assertEqual(result, 5)

    def test_non_exempt_equity_not_in_calendar_returns_none(self):
        """Non-exempt equity absent from calendar (e.g. TSM) → None."""
        from bot_options_stage1_candidates import _load_earnings_days_away
        cal_data = json.dumps({"calendar": []})
        with patch("bot_options_stage1_candidates.Path") as MockPath:
            instance = MagicMock()
            MockPath.return_value = instance
            instance.parent.__truediv__.return_value.__truediv__.return_value.__truediv__.return_value.exists.return_value = True
            instance.parent.__truediv__.return_value.__truediv__.return_value.__truediv__.return_value.read_text.return_value = cal_data
            result = _load_earnings_days_away("TSM")
        self.assertIsNone(result)

    def test_case_insensitive_exempt_lookup(self):
        """Exempt check is case-insensitive."""
        from bot_options_stage1_candidates import _load_earnings_days_away
        with patch("pathlib.Path.exists", side_effect=AssertionError("should not read file")):
            self.assertIsNone(_load_earnings_days_away("spy"))
            self.assertIsNone(_load_earnings_days_away("Spy"))
            self.assertIsNone(_load_earnings_days_away("SPY"))


if __name__ == "__main__":
    unittest.main()
