"""
tests/test_s8_phase_a.py — Sprint 8 Phase A verification.

Item 1: system_v1.txt EARNINGS / BINARY EVENT EXPOSURE RULE inserted correctly.
Item 2: format_thesis_ranking_section() appends earnings flags from earnings_days_away.
Item 3: format_positions_with_health() appends three-band oversize flag.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ─────────────────────────────────────────────────────────────────────────────
# Item 1 — system_v1.txt prompt structure
# ─────────────────────────────────────────────────────────────────────────────

class TestSystemPromptEarningsExposureRule(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        p = Path(__file__).parent.parent / "prompts" / "system_v1.txt"
        cls._txt = p.read_text()

    def test_new_section_present(self):
        self.assertIn("EARNINGS / BINARY EVENT EXPOSURE RULE", self._txt)

    def test_new_section_between_correct_anchors(self):
        earnings_pos     = self._txt.index("EARNINGS INTELLIGENCE")
        new_section_pos  = self._txt.index("EARNINGS / BINARY EVENT EXPOSURE RULE")
        post_earnings_pos = self._txt.index("POST-EARNINGS / CATALYST CONSUMED RULE")
        self.assertLess(earnings_pos, new_section_pos)
        self.assertLess(new_section_pos, post_earnings_pos)

    def test_full_size_carry_condition_present(self):
        self.assertIn("thesis score is 8/10+", self._txt)

    def test_binary_risk_sizing_framing_present(self):
        self.assertIn("binary-risk sizing regimes", self._txt)

    def test_historical_beat_rate_caution_present(self):
        self.assertIn("historical beat rate alone is not enough", self._txt)

    def test_existing_earnings_intelligence_section_intact(self):
        self.assertIn("EARNINGS INTELLIGENCE", self._txt)
        self.assertIn("earnings_pending", self._txt)

    def test_post_earnings_section_intact(self):
        self.assertIn("POST-EARNINGS / CATALYST CONSUMED RULE", self._txt)
        self.assertIn("The prior thesis is consumed", self._txt)


# ─────────────────────────────────────────────────────────────────────────────
# Item 2 — earnings flag in format_thesis_ranking_section()
# ─────────────────────────────────────────────────────────────────────────────

def _make_ts(symbol="AAPL", score=8, eda=None):
    return {
        "symbol":              symbol,
        "thesis_score":        score,
        "thesis_status":       "valid",
        "catalyst_age_days":   1,
        "technical_intact":    True,
        "above_ma20":          True,
        "above_ema9":          True,
        "trending_toward":     "target",
        "sector_aligned":      True,
        "weakest_factor":      "none",
        "recommended_action":  "hold",
        "override_flag":       None,
        "catalyst":            "strong momentum",
        "earnings_days_away":  eda,
    }


class TestEarningsFlagInThesisSection(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import portfolio_intelligence as pi
        cls._fmt = staticmethod(pi.format_thesis_ranking_section)

    def test_eda_zero_shows_consumed_flag(self):
        """earnings_days_away=0 → CATALYST CONSUMED flag appears."""
        out = self._fmt([_make_ts(eda=0)])
        self.assertIn("CATALYST CONSUMED", out)
        self.assertIn("re-evaluate fresh", out)

    def test_eda_one_shows_tomorrow_flag(self):
        """earnings_days_away=1 → EARNINGS TOMORROW flag appears."""
        out = self._fmt([_make_ts(eda=1)])
        self.assertIn("EARNINGS TOMORROW", out)
        self.assertIn("binary event exposure rule", out)

    def test_eda_two_shows_no_flag(self):
        """earnings_days_away=2 → no earnings flag."""
        out = self._fmt([_make_ts(eda=2)])
        self.assertNotIn("CATALYST CONSUMED", out)
        self.assertNotIn("EARNINGS TOMORROW", out)

    def test_eda_none_shows_no_flag(self):
        """earnings_days_away=None → no earnings flag (symbol not in calendar)."""
        out = self._fmt([_make_ts(eda=None)])
        self.assertNotIn("CATALYST CONSUMED", out)
        self.assertNotIn("EARNINGS TOMORROW", out)

    def test_eda_negative_shows_no_flag(self):
        """earnings_days_away=-1 (past earnings) → no earnings flag."""
        out = self._fmt([_make_ts(eda=-1)])
        self.assertNotIn("CATALYST CONSUMED", out)
        self.assertNotIn("EARNINGS TOMORROW", out)

    def test_non_earnings_fields_still_present(self):
        """Adding earnings flag does not break existing output lines."""
        out = self._fmt([_make_ts(eda=0)])
        self.assertIn("score: 8/10", out)
        self.assertIn("STRONG", out)
        self.assertIn("Action: HOLD", out)

    def test_eda_zero_and_eda_one_in_same_section(self):
        """Both flags render correctly when multiple positions."""
        out = self._fmt([_make_ts("AAPL", eda=0), _make_ts("NVDA", eda=1)])
        self.assertIn("CATALYST CONSUMED", out)
        self.assertIn("EARNINGS TOMORROW", out)


# ─────────────────────────────────────────────────────────────────────────────
# Item 3 — oversize flag in format_positions_with_health()
# ─────────────────────────────────────────────────────────────────────────────

def _make_pos(symbol="STNG", acct_pct_target=None, equity=100_000.0):
    """Build a minimal mock position object."""
    # market_value drives account_pct in compute_position_health
    market_value = (acct_pct_target / 100.0) * equity if acct_pct_target else 5_000.0
    p = MagicMock()
    p.symbol          = symbol
    p.qty             = 100.0
    p.avg_entry_price = market_value / 100.0
    p.current_price   = market_value / 100.0
    p.market_value    = market_value
    p.unrealized_pl   = 0.0
    p.unrealized_plpc = 0.0
    return p


class TestOversizeFlagInPositionsHealth(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import portfolio_intelligence as pi
        cls._fmt = staticmethod(pi.format_positions_with_health)

    def _render(self, acct_pct, equity=100_000.0):
        pos = _make_pos(acct_pct_target=acct_pct, equity=equity)
        return self._fmt([pos], equity)

    def test_above_20_fires_top_band(self):
        """24.7% → top band: 'exceeds max tier ceiling 20%'."""
        out = self._render(24.7)
        self.assertIn("OVERSIZE", out)
        self.assertIn("exceeds max tier ceiling 20%", out)
        self.assertIn("TRIM or close regardless of tier", out)

    def test_above_15_below_20_fires_core_band(self):
        """17.0% → core band: 'exceeds standard core max 15%'."""
        out = self._render(17.0)
        self.assertIn("OVERSIZE", out)
        self.assertIn("exceeds standard core max 15%", out)
        self.assertIn("confirm HIGH conviction core or TRIM", out)

    def test_above_8_below_15_fires_dynamic_band(self):
        """10.0% → dynamic band: 'exceeds 8%'."""
        out = self._render(10.0)
        self.assertIn("OVERSIZE for dynamic/intraday tier", out)
        self.assertIn("exceeds 8%", out)
        self.assertIn("TRIM or confirm core tier intended", out)

    def test_exactly_8_no_flag(self):
        """8.0% → at dynamic tier max, no flag fires."""
        out = self._render(8.0)
        self.assertNotIn("OVERSIZE", out)

    def test_below_8_no_flag(self):
        """5.0% → well within dynamic tier max, no flag."""
        out = self._render(5.0)
        self.assertNotIn("OVERSIZE", out)

    def test_exactly_20_fires_core_band_not_top(self):
        """20.0% → core band (> 15%, not > 20%)."""
        out = self._render(20.0)
        self.assertIn("exceeds standard core max 15%", out)
        self.assertNotIn("exceeds max tier ceiling 20%", out)

    def test_existing_health_fields_still_present(self):
        """Adding oversize flag does not remove existing health output."""
        out = self._render(24.7)
        self.assertIn("account_pct=", out)
        self.assertIn("drawdown=", out)
        self.assertIn("health=", out)

    def test_critical_drawdown_flag_still_fires(self):
        """CRITICAL health flag is not suppressed by oversize flag."""
        import portfolio_intelligence as pi
        with patch.object(pi, "compute_position_health") as mock_health:
            mock_health.return_value = {
                "health":      "CRITICAL",
                "account_pct": 25.0,
                "drawdown_pct": 12.5,
            }
            pos = _make_pos(acct_pct_target=25.0)
            out = pi.format_positions_with_health([pos], 100_000.0)
        self.assertIn("CRITICAL DRAWDOWN", out)
        self.assertIn("OVERSIZE", out)
