"""
tests/test_s8_phase_a.py — Sprint 8 Phase A + S8 Items 1/2/3 verification.

Item 1 (S8): format_positions_with_health() oversize bands use buying_power denominator.
Item 2 (S8): trim_severity score_max=5 (Option C — was 6).
Item 3 (S8): size-based TRIM gate in _decide_actions fires for score≥6 + oversized positions.
Prior items (Sprint prev): system_v1.txt earnings rule + earnings flag in thesis section.
"""
from __future__ import annotations

import json
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

    def test_eda_zero_shows_earnings_today_flag(self):
        """earnings_days_away=0 without timing → EARNINGS TODAY (safe default — upcoming)."""
        out = self._fmt([_make_ts(eda=0)])
        self.assertIn("EARNINGS TODAY", out)
        self.assertNotIn("CATALYST CONSUMED", out)

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
        self.assertIn("EARNINGS TODAY", out)
        self.assertIn("EARNINGS TOMORROW", out)


# ─────────────────────────────────────────────────────────────────────────────
# Item 1 (S8) — format_positions_with_health() uses buying_power denominator
# ─────────────────────────────────────────────────────────────────────────────

def _make_pos_mv(mv: float, symbol: str = "STNG") -> MagicMock:
    """Build a minimal mock position with an explicit market value."""
    p = MagicMock()
    p.symbol          = symbol
    p.qty             = 100.0
    p.avg_entry_price = mv / 100.0
    p.current_price   = mv / 100.0
    p.market_value    = mv
    p.unrealized_pl   = 0.0
    p.unrealized_plpc = 0.0
    return p


class TestOversizeFlagInPositionsHealth(unittest.TestCase):
    """Item 1 (S8): oversize bands use buying_power as denominator."""

    @classmethod
    def setUpClass(cls):
        import portfolio_intelligence as pi
        cls._fmt = staticmethod(pi.format_positions_with_health)

    def _render(self, bp_pct: float, equity: float = 100_000.0, buying_power: float = 120_000.0) -> str:
        """bp_pct is position market_value as % of buying_power."""
        mv  = (bp_pct / 100.0) * buying_power
        pos = _make_pos_mv(mv)
        return self._fmt([pos], equity, buying_power=buying_power)

    def test_above_25_fires_top_band(self):
        """26% of BP → top band: 'exceeds HIGH conviction core ceiling 25%'."""
        out = self._render(26.0)
        self.assertIn("OVERSIZE", out)
        self.assertIn("exceeds HIGH conviction core ceiling 25%", out)
        self.assertIn("of BP", out)
        self.assertIn("TRIM or close regardless of tier", out)

    def test_above_20_below_25_fires_core_band(self):
        """22% of BP → core band (>20%, ≤25%): 'exceeds standard core max 20%'."""
        out = self._render(22.0)
        self.assertIn("OVERSIZE", out)
        self.assertIn("exceeds standard core max 20%", out)
        self.assertIn("of BP", out)
        self.assertIn("confirm HIGH conviction core or TRIM", out)

    def test_above_15_below_20_fires_dynamic_band(self):
        """17% of BP → dynamic band (>15%, ≤20%): 'exceeds 15%'."""
        out = self._render(17.0)
        self.assertIn("OVERSIZE for dynamic/intraday tier", out)
        self.assertIn("exceeds 15%", out)
        self.assertIn("of BP", out)
        self.assertIn("TRIM or confirm core tier intended", out)

    def test_exactly_15_no_flag(self):
        """15.0% of BP → at dynamic tier max boundary, no flag fires."""
        out = self._render(15.0)
        self.assertNotIn("OVERSIZE", out)

    def test_below_15_no_flag(self):
        """5.0% of BP → well within any tier max, no flag."""
        out = self._render(5.0)
        self.assertNotIn("OVERSIZE", out)

    def test_exactly_25_fires_core_band_not_top(self):
        """25.0% of BP → core band (>20%, not strictly >25%)."""
        out = self._render(25.0)
        self.assertIn("exceeds standard core max 20%", out)
        self.assertNotIn("exceeds HIGH conviction core ceiling 25%", out)

    def test_16_pct_of_bp_fires_dynamic_band(self):
        """16% of BP → dynamic band (>15%, <20%)."""
        out = self._render(16.0)
        self.assertIn("OVERSIZE for dynamic/intraday tier", out)

    def test_buying_power_zero_falls_back_to_equity(self):
        """buying_power=0 → equity is denominator, no crash, flag still fires at 26%."""
        equity = 100_000.0
        mv     = 26_000.0   # 26% of equity → top band
        pos    = _make_pos_mv(mv)
        import portfolio_intelligence as pi
        out    = pi.format_positions_with_health([pos], equity, buying_power=0.0)
        self.assertIn("OVERSIZE", out)
        self.assertIn("exceeds HIGH conviction core ceiling 25%", out)

    def test_existing_health_fields_still_present(self):
        """Adding oversize flag does not remove existing health output."""
        out = self._render(26.0)
        self.assertIn("account_pct=", out)
        self.assertIn("drawdown=", out)
        self.assertIn("health=", out)

    def test_critical_drawdown_flag_still_fires(self):
        """CRITICAL health flag is not suppressed by oversize flag."""
        import portfolio_intelligence as pi
        with patch.object(pi, "compute_position_health") as mock_health:
            mock_health.return_value = {
                "health":       "CRITICAL",
                "account_pct":  25.0,
                "drawdown_pct": 12.5,
            }
            pos = _make_pos_mv(26_000.0)
            out = pi.format_positions_with_health([pos], 100_000.0, buying_power=100_000.0)
        self.assertIn("CRITICAL DRAWDOWN", out)
        self.assertIn("OVERSIZE", out)


# ─────────────────────────────────────────────────────────────────────────────
# Item 2 (S8) — trim_severity score_max=5 (Option C)
# ─────────────────────────────────────────────────────────────────────────────

class TestTrimSeverityItem2(unittest.TestCase):
    """score_max ceiling is 5 — score=5 maps to the 25% trim tier."""

    def _pa_cfg(self):
        import portfolio_allocator as pa
        cfg = dict(pa._PA_DEFAULTS)
        cfg["trim_severity"] = [
            {"score_max": 2, "trim_pct": 0.75},
            {"score_max": 4, "trim_pct": 0.50},
            {"score_max": 5, "trim_pct": 0.25},
        ]
        return cfg

    def test_score_5_routes_to_25pct(self):
        import portfolio_allocator as pa
        self.assertAlmostEqual(pa._trim_pct_for_score(5, self._pa_cfg()), 0.25)

    def test_score_4_routes_to_50pct(self):
        import portfolio_allocator as pa
        self.assertAlmostEqual(pa._trim_pct_for_score(4, self._pa_cfg()), 0.50)

    def test_score_2_routes_to_75pct(self):
        import portfolio_allocator as pa
        self.assertAlmostEqual(pa._trim_pct_for_score(2, self._pa_cfg()), 0.75)

    def test_score_6_above_all_tiers_uses_default(self):
        """score=6 exceeds all score_max entries → falls through to 0.25 default."""
        import portfolio_allocator as pa
        self.assertAlmostEqual(pa._trim_pct_for_score(6, self._pa_cfg()), 0.25)

    def test_strategy_config_has_score_max_5_not_6(self):
        """Confirm strategy_config.json trim_severity has score_max=5, not 6."""
        cfg_path = Path(__file__).parent.parent / "strategy_config.json"
        cfg  = json.loads(cfg_path.read_text())
        tiers = cfg["portfolio_allocator"]["trim_severity"]
        max_values = [int(t["score_max"]) for t in tiers]
        self.assertIn(5, max_values)
        self.assertNotIn(6, max_values)


# ─────────────────────────────────────────────────────────────────────────────
# Item 3 (S8) — size-based TRIM gate in _decide_actions
# ─────────────────────────────────────────────────────────────────────────────

def _make_incumbent(symbol: str = "STNG", score: int = 7, mv: float = 22_000.0) -> dict:
    return {
        "symbol":                  symbol,
        "market_value":            mv,
        "account_pct":             mv / 1000.0,   # rough; not used by size-TRIM path
        "thesis_score":            score,
        "thesis_score_normalized": score * 10,
        "health":                  "OK",
        "recommended_pi_action":   "hold",
        "override_flag":           None,
        "weakest_factor":          "",
    }


def _make_sizes(bp: float = 120_000.0) -> dict:
    return {
        "buying_power":    bp,
        "core":            15_000.0,
        "standard":        8_000.0,
        "available_for_new": 20_000.0,
        "max_exposure":    30_000.0,
    }


def _run_decide(
    incumbents,
    pa_overrides: dict | None = None,
    bp: float = 120_000.0,
) -> list[dict]:
    """Helper: run _decide_actions with sane defaults. Returns proposed_actions."""
    import portfolio_allocator as pa
    cfg    = {"time_bound_actions": []}
    pa_cfg = dict(pa._PA_DEFAULTS)
    pa_cfg["trim_severity"] = [
        {"score_max": 2, "trim_pct": 0.75},
        {"score_max": 4, "trim_pct": 0.50},
        {"score_max": 5, "trim_pct": 0.25},
    ]
    if pa_overrides:
        pa_cfg.update(pa_overrides)
    sizes   = _make_sizes(bp)
    pi_data: dict = {"correlation": {}}
    proposed, _ = pa._decide_actions(
        incumbents, [], pi_data, cfg, pa_cfg, sizes, 100_000.0
    )
    return proposed


class TestSizeTrimGateItem3(unittest.TestCase):
    """Item 3 (S8): size-based TRIM gate fires for score≥6 + oversized positions."""

    def test_size_trim_fires_when_equity_pct_exceeds_tier_plus_tolerance(self):
        """STNG (core, tier_max=20%) at 27K / total_capacity(bp=120K) = 22.5% > 22% → SIZE TRIM.

        total_capacity = current_exposure(0) + bp(120K) = 120K.
        cap_frac = 27K/120K = 22.5% > tier_max(20%) + tol(2%) = 22% → fires.
        """
        inc = _make_incumbent("STNG", score=7, mv=27_000.0)
        proposed = _run_decide([inc], bp=120_000.0)
        actions  = [p for p in proposed if p["action"] == "TRIM"]
        self.assertEqual(len(actions), 1)
        self.assertIn("SIZE TRIM", actions[0]["reason"])
        self.assertIn("total capacity", actions[0]["reason"])

    def test_size_trim_does_not_fire_within_tolerance(self):
        """STNG at 16.5K/100K equity = 16.5% — within 20% + 2% = 22% → no size TRIM."""
        inc = _make_incumbent("STNG", score=7, mv=16_500.0)
        proposed = _run_decide([inc], bp=120_000.0)
        trim_actions = [p for p in proposed if p["action"] == "TRIM"]
        self.assertEqual(len(trim_actions), 0)

    def test_low_score_uses_thesis_trim_not_size_trim(self):
        """score=4 (≤ trim_thresh=5) → thesis TRIM fires, not size TRIM."""
        inc = _make_incumbent("STNG", score=4, mv=23_000.0)
        proposed = _run_decide([inc], bp=120_000.0)
        trim_actions = [p for p in proposed if p["action"] == "TRIM"]
        self.assertEqual(len(trim_actions), 1)
        self.assertNotIn("SIZE TRIM", trim_actions[0]["reason"])
        self.assertIn("thesis_score=4", trim_actions[0]["reason"])

    def test_score_6_triggers_size_trim_not_thesis_trim(self):
        """score=6 is above trim_thresh (5) → size TRIM path (not thesis TRIM).

        MV=27K, total_capacity=120K → cap_frac=22.5% > 22% → SIZE TRIM, not thesis TRIM.
        """
        inc = _make_incumbent("STNG", score=6, mv=27_000.0)
        proposed = _run_decide([inc], bp=120_000.0)
        trim_actions = [p for p in proposed if p["action"] == "TRIM"]
        self.assertEqual(len(trim_actions), 1)
        self.assertIn("SIZE TRIM", trim_actions[0]["reason"])

    def test_size_trim_disabled_flag_suppresses_gate(self):
        """size_trim_enabled=False → oversized position with score=7 → HOLD, no size TRIM."""
        inc = _make_incumbent("STNG", score=7, mv=22_000.0)
        proposed = _run_decide([inc], pa_overrides={"size_trim_enabled": False}, bp=120_000.0)
        trim_actions = [p for p in proposed if p["action"] == "TRIM"]
        self.assertEqual(len(trim_actions), 0)

    def test_strategy_config_has_size_trim_enabled(self):
        """Confirm strategy_config.json has size_trim_enabled=true."""
        cfg_path = Path(__file__).parent.parent / "strategy_config.json"
        cfg = json.loads(cfg_path.read_text())
        pa_section = cfg["portfolio_allocator"]
        self.assertTrue(pa_section["size_trim_enabled"])
        self.assertIsInstance(pa_section["size_trim_tolerance_pct"], (int, float))
