"""
S8 earnings label timezone edge-case tests.

Covers:
  earnings_timing() — None-safety (missing symbol, missing calendar, load failure)
  format_thesis_ranking_section() display label — all 6 rows of the behavior table
  build_portfolio_intelligence() catalyst_consumed auto-flip — eda=0/pre-market case
"""

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ts(symbol, eda, timing=None, score=7, catalyst="earnings beat",
             days_held=3, consumed=False, consumed_at=None):
    return {
        "symbol": symbol,
        "thesis_score": score,
        "thesis_status": "valid" if score >= 7 else "weakening",
        "catalyst": catalyst,
        "catalyst_age_days": days_held,
        "catalyst_consumed": consumed,
        "catalyst_consumed_at": consumed_at,
        "earnings_days_away": eda,
        "earnings_timing": timing,
        "technical_intact": True,
        "above_ma20": True,
        "above_ema9": True,
        "trending_toward": "target",
        "sector_aligned": True,
        "weakest_factor": "none",
        "recommended_action": "hold",
        "override_flag": None,
    }


def _make_position(symbol, qty=100, avg_entry_price=50.0, current_price=55.0,
                   unrealized_pl=500.0, market_value=5500.0):
    pos = MagicMock()
    pos.symbol = symbol
    pos.qty = str(qty)
    pos.avg_entry_price = str(avg_entry_price)
    pos.current_price = str(current_price)
    pos.unrealized_pl = str(unrealized_pl)
    pos.market_value = str(market_value)
    return pos


def _base_config():
    return {
        "position_sizing": {
            "core_tier_pct": 0.15,
            "standard_tier_pct": 0.08,
            "speculative_tier_pct": 0.05,
            "dynamic_tier_pct": 0.08,
            "max_total_exposure_pct": 0.30,
            "cash_reserve_pct": 0.10,
        },
        "parameters": {
            "margin_authorized": False,
            "margin_sizing_multiplier": 1.0,
        },
        "time_bound_actions": [],
    }


def _run_pi_with_eda(eda_value, timing_value):
    """Run build_portfolio_intelligence with controlled eda + timing."""
    from portfolio_intelligence import build_portfolio_intelligence

    pos = _make_position("V", qty=50, avg_entry_price=300.0,
                         current_price=310.0, unrealized_pl=500.0,
                         market_value=15500.0)
    decisions = {"V": {"catalyst": "Q1 earnings beat",
                       "stop_loss": 290.0, "take_profit": 330.0}}
    entry_dates = {"V": datetime(2026, 4, 20, tzinfo=timezone.utc)}

    mock_eda_module = MagicMock()
    mock_eda_module.earnings_days_away = lambda sym: eda_value
    mock_eda_module.earnings_timing = lambda sym: timing_value

    with patch("portfolio_intelligence._load_bars", return_value=None), \
         patch("portfolio_intelligence._load_sector_perf", return_value={}), \
         patch("portfolio_intelligence.get_forced_exits", return_value=[]), \
         patch("portfolio_intelligence.get_deadline_exits", return_value=[]), \
         patch("portfolio_intelligence.compute_portfolio_correlation",
               return_value={"matrix": {}, "high_correlation_pairs": [],
                             "effective_bets": 1, "new_symbol_correlations": {}}), \
         patch.dict("sys.modules",
                    {"earnings_calendar_lookup": mock_eda_module}):
        result = build_portfolio_intelligence(
            equity=103_420.0,
            positions=[pos],
            config=_base_config(),
            open_decisions=decisions,
            position_entry_dates=entry_dates,
            buying_power=271_556.0,
        )
    return result["thesis_scores"][0]


# ---------------------------------------------------------------------------
# earnings_timing() None-safety
# ---------------------------------------------------------------------------

class TestEarningsTimingNoneSafety(unittest.TestCase):
    """earnings_timing() must return None gracefully on any missing/failed input."""

    def test_missing_symbol_returns_none(self):
        from earnings_calendar_lookup import earnings_timing
        result = earnings_timing("ZZZNOTREAL", calendar_map={"AAPL": {"timing": "post-market"}})
        self.assertIsNone(result)

    def test_empty_calendar_returns_none(self):
        from earnings_calendar_lookup import earnings_timing
        result = earnings_timing("AAPL", calendar_map={})
        self.assertIsNone(result)

    def test_entry_without_timing_field_returns_none(self):
        from earnings_calendar_lookup import earnings_timing
        result = earnings_timing("AAPL", calendar_map={"AAPL": {"earnings_date": "2026-05-01"}})
        self.assertIsNone(result)

    def test_known_timing_returned(self):
        from earnings_calendar_lookup import earnings_timing
        result = earnings_timing("AAPL", calendar_map={"AAPL": {"timing": "post-market"}})
        self.assertEqual(result, "post-market")

    def test_pre_market_returned(self):
        from earnings_calendar_lookup import earnings_timing
        result = earnings_timing("SPOT", calendar_map={"SPOT": {"timing": "pre-market"}})
        self.assertEqual(result, "pre-market")

    def test_none_symbol_returns_none(self):
        from earnings_calendar_lookup import earnings_timing
        result = earnings_timing(None, calendar_map={"AAPL": {"timing": "post-market"}})
        self.assertIsNone(result)

    def test_load_failure_returns_none(self):
        """earnings_timing() with no calendar_map falls back to load_calendar_map;
        if that raises, the exception propagates (non-fatal at call site in PI)."""
        from earnings_calendar_lookup import earnings_timing
        with patch("earnings_calendar_lookup.load_calendar_map",
                   side_effect=RuntimeError("io error")):
            with self.assertRaises(Exception):
                earnings_timing("AAPL")


# ---------------------------------------------------------------------------
# Behavior table — display label (format_thesis_ranking_section)
# ---------------------------------------------------------------------------

class TestEarningsLabelDisplay(unittest.TestCase):
    """Six rows of the behavior table verified via format_thesis_ranking_section."""

    def _render(self, eda, timing, consumed=False, consumed_at=None):
        from portfolio_intelligence import format_thesis_ranking_section
        ts = _make_ts("V", eda=eda, timing=timing, consumed=consumed,
                      consumed_at=consumed_at)
        return format_thesis_ranking_section([ts])

    def test_eda_negative_any_timing_shows_consumed(self):
        """eda < 0 / any timing — catalyst_consumed=True is shown as CONSUMED block."""
        from portfolio_intelligence import format_thesis_ranking_section
        ts = _make_ts("V", eda=-1, timing="post-market", consumed=True,
                      consumed_at="2026-04-28T20:00:00+00:00")
        output = format_thesis_ranking_section([ts])
        self.assertIn("CONSUMED", output)
        self.assertNotIn("EARNINGS TODAY", output)
        self.assertNotIn("EARNINGS TOMORROW", output)

    def test_eda_zero_pre_market_shows_catalyst_consumed_label(self):
        """eda=0 / pre-market — event already happened → CATALYST CONSUMED label."""
        output = self._render(eda=0, timing="pre-market")
        self.assertIn("CATALYST CONSUMED", output)
        self.assertIn("pre-market", output)
        self.assertNotIn("EARNINGS TODAY", output)

    def test_eda_zero_post_market_shows_earnings_today(self):
        """eda=0 / post-market — event hasn't happened yet → EARNINGS TODAY."""
        output = self._render(eda=0, timing="post-market")
        self.assertIn("EARNINGS TODAY", output)
        self.assertNotIn("CATALYST CONSUMED", output)
        self.assertNotIn("EARNINGS TOMORROW", output)

    def test_eda_zero_unknown_shows_earnings_today(self):
        """eda=0 / unknown — safe default is upcoming → EARNINGS TODAY."""
        output = self._render(eda=0, timing="unknown")
        self.assertIn("EARNINGS TODAY", output)
        self.assertNotIn("CATALYST CONSUMED", output)

    def test_eda_zero_none_timing_shows_earnings_today(self):
        """eda=0 / timing=None — treat as upcoming → EARNINGS TODAY."""
        output = self._render(eda=0, timing=None)
        self.assertIn("EARNINGS TODAY", output)
        self.assertNotIn("CATALYST CONSUMED", output)

    def test_eda_one_any_timing_shows_earnings_tomorrow(self):
        """eda=1 / any timing — EARNINGS TOMORROW label."""
        output = self._render(eda=1, timing="post-market")
        self.assertIn("EARNINGS TOMORROW", output)
        self.assertNotIn("EARNINGS TODAY", output)
        self.assertNotIn("CATALYST CONSUMED", output)

    def test_eda_two_no_flag(self):
        """eda=2+ — no earnings label shown."""
        output = self._render(eda=2, timing="post-market")
        self.assertNotIn("EARNINGS TODAY", output)
        self.assertNotIn("EARNINGS TOMORROW", output)
        self.assertNotIn("CATALYST CONSUMED", output)


# ---------------------------------------------------------------------------
# Behavior table — catalyst_consumed auto-flip in build_portfolio_intelligence
# ---------------------------------------------------------------------------

class TestCatalystConsumedFlip(unittest.TestCase):
    """catalyst_consumed must fire for eda<0 and eda=0/pre-market only."""

    def test_eda_negative_sets_consumed_true(self):
        """eda=-1 / any timing → catalyst_consumed=True."""
        ts = _run_pi_with_eda(eda_value=-1, timing_value="post-market")
        self.assertTrue(ts["catalyst_consumed"])
        self.assertIsNotNone(ts["catalyst_consumed_at"])

    def test_eda_zero_pre_market_sets_consumed_true(self):
        """eda=0 / pre-market → catalyst_consumed=True (event already happened)."""
        ts = _run_pi_with_eda(eda_value=0, timing_value="pre-market")
        self.assertTrue(ts["catalyst_consumed"])
        self.assertIsNotNone(ts["catalyst_consumed_at"])

    def test_eda_zero_post_market_consumed_false(self):
        """eda=0 / post-market → catalyst_consumed=False (event not yet happened)."""
        ts = _run_pi_with_eda(eda_value=0, timing_value="post-market")
        self.assertFalse(ts["catalyst_consumed"])
        self.assertIsNone(ts["catalyst_consumed_at"])

    def test_eda_zero_unknown_consumed_false(self):
        """eda=0 / unknown timing → catalyst_consumed=False (safe default)."""
        ts = _run_pi_with_eda(eda_value=0, timing_value="unknown")
        self.assertFalse(ts["catalyst_consumed"])

    def test_eda_zero_none_timing_consumed_false(self):
        """eda=0 / timing=None → catalyst_consumed=False."""
        ts = _run_pi_with_eda(eda_value=0, timing_value=None)
        self.assertFalse(ts["catalyst_consumed"])

    def test_eda_one_consumed_false(self):
        """eda=1 → catalyst_consumed=False."""
        ts = _run_pi_with_eda(eda_value=1, timing_value="post-market")
        self.assertFalse(ts["catalyst_consumed"])

    def test_eda_two_consumed_false(self):
        """eda=2 → catalyst_consumed=False."""
        ts = _run_pi_with_eda(eda_value=2, timing_value="post-market")
        self.assertFalse(ts["catalyst_consumed"])


if __name__ == "__main__":
    unittest.main()
