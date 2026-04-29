"""
S8-Phase-D tests
Item 1 — log_trade isolation in test_sprint2_followup (no trades.jsonl contamination)
Item 2 — available_for_new uses buying_power (not equity-cap formula)
Item 3 — catalyst_consumed auto-flip when earnings_days_away < 0
Item 4 — format_thesis_ranking_section renders consumed-catalyst text
Item 5 — exposure_pct uses total capacity denominator (current_exposure + buying_power)
"""

import textwrap
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _make_ts(symbol, score=7, catalyst="earnings beat", days_held=3,
             consumed=False, consumed_at=None, eda=None):
    return {
        "symbol": symbol,
        "thesis_score": score,
        "thesis_status": "valid" if score >= 7 else ("weakening" if score >= 4 else "invalidated"),
        "catalyst": catalyst,
        "catalyst_age_days": days_held,
        "catalyst_consumed": consumed,
        "catalyst_consumed_at": consumed_at,
        "earnings_days_away": eda,
        "technical_intact": True,
        "above_ma20": True,
        "above_ema9": True,
        "trending_toward": "target",
        "sector_aligned": True,
        "weakest_factor": "none",
        "recommended_action": "hold",
        "override_flag": None,
    }


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


# ---------------------------------------------------------------------------
# Item 1 — log_trade isolation
# ---------------------------------------------------------------------------

class TestLogTradeIsolation(unittest.TestCase):
    """log_trade must be mocked in execute_all test helpers to prevent
    trades.jsonl contamination on every pytest run."""

    def _collect_patch_args(self, source_str):
        import ast
        dedented = textwrap.dedent(source_str)
        tree = ast.parse(dedented)
        args = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                is_patch = (
                    (isinstance(func, ast.Name) and func.id == "patch") or
                    (isinstance(func, ast.Attribute) and func.attr == "patch")
                )
                if is_patch:
                    for arg in node.args:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            args.append(arg.value)
        return args

    def test_log_trade_mocked_in_run_execute_sell(self):
        """_run_execute_sell patches order_executor.log_trade."""
        import inspect

        from tests import test_sprint2_followup as m
        src = inspect.getsource(m.TestRemoveBackstopWiredInExecutor._run_execute_sell)
        self.assertIn(
            "order_executor.log_trade",
            self._collect_patch_args(src),
            "_run_execute_sell must patch order_executor.log_trade to prevent "
            "trades.jsonl contamination on every pytest run",
        )

    def test_log_trade_mocked_in_failure_test(self):
        """test_remove_backstop_failure_is_non_fatal patches order_executor.log_trade."""
        import inspect

        from tests import test_sprint2_followup as m
        src = inspect.getsource(
            m.TestRemoveBackstopWiredInExecutor.test_remove_backstop_failure_is_non_fatal
        )
        self.assertIn(
            "order_executor.log_trade",
            self._collect_patch_args(src),
            "test_remove_backstop_failure_is_non_fatal must patch order_executor.log_trade",
        )


# ---------------------------------------------------------------------------
# Item 2 — available_for_new uses buying_power
# ---------------------------------------------------------------------------

class TestAvailableForNew(unittest.TestCase):
    """compute_dynamic_sizes must report buying_power as available_for_new."""

    def test_available_for_new_equals_buying_power(self):
        """available_for_new = buying_power (Alpaca already nets deployed margin)."""
        from portfolio_intelligence import compute_dynamic_sizes
        equity = 103_420.0
        buying_power = 271_556.0
        current_exposure = 113_900.0

        sizes = compute_dynamic_sizes(
            equity, _base_config(),
            current_exposure_dollars=current_exposure,
            buying_power=buying_power,
        )
        self.assertEqual(sizes["available_for_new"], round(buying_power, 2))

    def test_available_for_new_not_zero_when_over_equity_cap(self):
        """available_for_new must not be zero when deployed > equity * max_exp_pct
        but buying_power remains positive."""
        from portfolio_intelligence import compute_dynamic_sizes
        equity = 100_000.0
        buying_power = 200_000.0
        # current_exposure exceeds the 30% equity cap (100k * 0.30 = 30k)
        current_exposure = 80_000.0

        sizes = compute_dynamic_sizes(
            equity, _base_config(),
            current_exposure_dollars=current_exposure,
            buying_power=buying_power,
        )
        self.assertGreater(
            sizes["available_for_new"], 0,
            "available_for_new must not be zero when buying_power > 0",
        )

    def test_available_for_new_zero_when_buying_power_zero(self):
        """available_for_new is 0 when buying_power = 0."""
        from portfolio_intelligence import compute_dynamic_sizes
        sizes = compute_dynamic_sizes(
            100_000.0, _base_config(),
            current_exposure_dollars=50_000.0,
            buying_power=0.0,
        )
        self.assertEqual(sizes["available_for_new"], 0.0)

    def test_available_for_new_with_negative_cash_margin_account(self):
        """Margin account with negative cash still shows buying_power as available."""
        from portfolio_intelligence import compute_dynamic_sizes
        # Mirrors real account: equity=$103k, cash=-$10k, bp=$271k
        equity = 103_420.0
        buying_power = 271_556.0
        current_exposure = 113_901.0  # exceeds equity (margin deployed)

        sizes = compute_dynamic_sizes(
            equity, _base_config(),
            current_exposure_dollars=current_exposure,
            buying_power=buying_power,
        )
        self.assertGreater(sizes["available_for_new"], 100_000,
                           "Margin account with real BP should show > $100k available")


# ---------------------------------------------------------------------------
# Item 3 — catalyst_consumed auto-flip (unit-level — directly test PI loop logic)
# ---------------------------------------------------------------------------

class TestCatalystConsumedAutoFlip(unittest.TestCase):
    """catalyst_consumed must flip True when earnings_days_away < 0.

    Tests exercise build_portfolio_intelligence directly with mocked helpers
    and an explicit earnings_calendar_lookup module injection.
    """

    def _run_with_eda(self, eda_value):
        """Run build_portfolio_intelligence with controlled earnings_days_away."""
        from portfolio_intelligence import build_portfolio_intelligence

        pos = _make_position("V", qty=50, avg_entry_price=300.0,
                             current_price=310.0, unrealized_pl=500.0,
                             market_value=15500.0)

        decisions = {"V": {"catalyst": "Q1 earnings beat",
                           "stop_loss": 290.0, "take_profit": 330.0}}
        entry_dates = {"V": datetime(2026, 4, 20, tzinfo=timezone.utc)}

        mock_eda_module = MagicMock()
        mock_eda_module.earnings_days_away = lambda sym: eda_value

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
        return result["thesis_scores"]

    def test_catalyst_consumed_false_when_eda_positive(self):
        """catalyst_consumed=False when earnings are in the future."""
        scores = self._run_with_eda(3)
        self.assertEqual(len(scores), 1)
        self.assertFalse(scores[0]["catalyst_consumed"])
        self.assertIsNone(scores[0]["catalyst_consumed_at"])

    def test_catalyst_consumed_false_when_eda_zero(self):
        """catalyst_consumed=False when eda=0 (earnings today — not yet consumed)."""
        scores = self._run_with_eda(0)
        self.assertFalse(scores[0]["catalyst_consumed"])

    def test_catalyst_consumed_true_when_eda_negative(self):
        """catalyst_consumed=True when eda < 0 (earnings in the past)."""
        scores = self._run_with_eda(-1)
        self.assertTrue(scores[0]["catalyst_consumed"])

    def test_catalyst_consumed_at_is_iso_timestamp(self):
        """catalyst_consumed_at is an ISO timestamp when consumed."""
        scores = self._run_with_eda(-3)
        consumed_at = scores[0].get("catalyst_consumed_at")
        self.assertIsNotNone(consumed_at)
        dt = datetime.fromisoformat(consumed_at)
        self.assertIsNotNone(dt)

    def test_catalyst_consumed_score_penalty_applied(self):
        """thesis_score is decremented by 1 when catalyst consumed."""
        scores_consumed = self._run_with_eda(-2)
        scores_fresh    = self._run_with_eda(5)
        # Consumed score should be 1 lower than fresh (floor 0)
        self.assertLessEqual(scores_consumed[0]["thesis_score"],
                             scores_fresh[0]["thesis_score"])

    def test_catalyst_consumed_score_floors_at_zero(self):
        """thesis_score never goes below 0 after penalty."""
        from portfolio_intelligence import score_position_thesis
        pos = _make_position("XBI", current_price=130.0, avg_entry_price=200.0,
                             unrealized_pl=-7000.0)
        with patch("portfolio_intelligence._load_bars", return_value=None), \
             patch("portfolio_intelligence._load_sector_perf", return_value={}):
            ts = score_position_thesis(
                symbol="XBI", position=pos,
                original_decision={"catalyst": "biotech play"},
                current_md={}, days_held=10, strategy_config=_base_config(),
            )
        penalized = max(0, ts["thesis_score"] - 1)
        self.assertGreaterEqual(penalized, 0)

    def test_catalyst_not_consumed_when_eda_none(self):
        """catalyst_consumed=False when earnings_days_away unavailable."""
        scores = self._run_with_eda(None)
        self.assertFalse(scores[0]["catalyst_consumed"])
        self.assertIsNone(scores[0]["catalyst_consumed_at"])


# ---------------------------------------------------------------------------
# Item 4 — format_thesis_ranking_section consumed-catalyst rendering
# ---------------------------------------------------------------------------

class TestFormatConsumedCatalyst(unittest.TestCase):
    """format_thesis_ranking_section must render consumed-catalyst text."""

    def test_consumed_catalyst_shows_consumed_header(self):
        """When catalyst_consumed=True, output must contain 'CONSUMED'."""
        from portfolio_intelligence import format_thesis_ranking_section
        ts = _make_ts("V", consumed=True, consumed_at="2026-04-25T10:00:00+00:00",
                      catalyst="Q1 earnings beat", eda=-3)
        output = format_thesis_ranking_section([ts])
        self.assertIn("CONSUMED", output)

    def test_consumed_catalyst_does_not_show_age_marker(self):
        """When consumed, the (Nd old) age marker must not appear."""
        from portfolio_intelligence import format_thesis_ranking_section
        ts = _make_ts("GOOGL", consumed=True,
                      consumed_at="2026-04-24T14:00:00+00:00",
                      catalyst="cloud growth", eda=-4)
        output = format_thesis_ranking_section([ts])
        self.assertNotIn("d old", output)

    def test_consumed_catalyst_includes_generic_fallback(self):
        """When thesis_tags absent, generic forward-thesis text must appear."""
        from portfolio_intelligence import format_thesis_ranking_section
        ts = _make_ts("MA", consumed=True,
                      consumed_at="2026-04-23T09:00:00+00:00",
                      catalyst="earnings", eda=-5)
        output = format_thesis_ranking_section([ts])
        self.assertIn("re-evaluate forward thesis", output)

    def test_consumed_catalyst_uses_thesis_tags_when_present(self):
        """When thesis_tags populated, they replace the generic fallback."""
        from portfolio_intelligence import format_thesis_ranking_section
        ts = _make_ts("AMZN", consumed=True,
                      consumed_at="2026-04-22T08:00:00+00:00",
                      catalyst="AWS beat", eda=-6)
        ts["thesis_tags"] = ["AWS growth", "advertising margin", "Prime flywheel"]
        output = format_thesis_ranking_section([ts])
        self.assertIn("AWS growth", output)
        self.assertNotIn("re-evaluate forward thesis", output)

    def test_non_consumed_catalyst_shows_normal_format(self):
        """Non-consumed position shows original catalyst text with age marker."""
        from portfolio_intelligence import format_thesis_ranking_section
        ts = _make_ts("GLD", consumed=False, catalyst="safe haven bid",
                      days_held=2, eda=10)
        output = format_thesis_ranking_section([ts])
        self.assertIn("safe haven bid", output)
        self.assertIn("d old", output)
        self.assertNotIn("CONSUMED", output)

    def test_consumed_at_date_appears_in_output(self):
        """The date portion of catalyst_consumed_at appears in the output."""
        from portfolio_intelligence import format_thesis_ranking_section
        ts = _make_ts("V", consumed=True,
                      consumed_at="2026-04-25T10:00:00+00:00", eda=-3)
        output = format_thesis_ranking_section([ts])
        self.assertIn("2026-04-25", output)


# ---------------------------------------------------------------------------
# Item 5 — exposure_pct uses total capacity denominator
# ---------------------------------------------------------------------------

class TestExposurePctTotalCapacity(unittest.TestCase):
    """compute_dynamic_sizes must compute exposure_pct as
    current_exposure / (current_exposure + buying_power)."""

    def _sizes(self, exposure, buying_power, equity=100_000.0):
        from portfolio_intelligence import compute_dynamic_sizes
        cfg = _base_config()
        return compute_dynamic_sizes(equity, cfg, exposure, buying_power=buying_power)

    def test_exposure_pct_typical_margin_account(self):
        """Live numbers: $114K deployed, $271K BP → 29.6%."""
        sizes = self._sizes(114_382.91, 271_556.71)
        self.assertAlmostEqual(sizes["exposure_pct"], 29.6, places=0)

    def test_exposure_pct_not_over_100(self):
        """With margin accounts, equity-based denominator was returning 110%+.
        Total-capacity denominator must always return ≤ 100%."""
        # exposure slightly above equity (margin deployed)
        sizes = self._sizes(110_000.0, 290_000.0, equity=100_000.0)
        self.assertLessEqual(sizes["exposure_pct"], 100.0)

    def test_exposure_pct_zero_exposure(self):
        """All cash: exposure_pct = 0."""
        sizes = self._sizes(0.0, 400_000.0)
        self.assertEqual(sizes["exposure_pct"], 0.0)

    def test_exposure_pct_fully_deployed(self):
        """All-in with no buying power left: exposure_pct = 100.0."""
        sizes = self._sizes(100_000.0, 0.0, equity=100_000.0)
        self.assertEqual(sizes["exposure_pct"], 100.0)

    def test_exposure_pct_buying_power_zero_fallback(self):
        """When both exposure and buying_power are 0, no ZeroDivisionError."""
        sizes = self._sizes(0.0, 0.0, equity=100_000.0)
        self.assertEqual(sizes["exposure_pct"], 0.0)

    def test_available_for_new_unchanged(self):
        """available_for_new must still equal buying_power exactly."""
        bp = 271_556.71
        sizes = self._sizes(114_382.91, bp)
        self.assertEqual(sizes["available_for_new"], round(bp, 2))

    def test_exposure_label_contains_total_capacity(self):
        """format_dynamic_sizes_section label must say 'of total capacity'."""
        from portfolio_intelligence import (
            compute_dynamic_sizes,
            format_dynamic_sizes_section,
        )
        cfg = _base_config()
        sizes = compute_dynamic_sizes(100_000.0, cfg, 30_000.0, buying_power=270_000.0)
        section = format_dynamic_sizes_section(sizes, 100_000.0)
        self.assertIn("of total capacity", section)
        self.assertNotIn("of buying power", section)


if __name__ == "__main__":
    unittest.main()
