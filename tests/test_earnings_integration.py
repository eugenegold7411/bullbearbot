"""
tests/test_earnings_integration.py — A2 Earnings Integration (FIX-A through FIX-D)

a) FIX-A: AMAT OI=37 + eda=6 + conviction=high → OI floor lowered to 25, passes gate
b) FIX-A: AMAT OI=24 + eda=6 + conviction=high → OI < 25 floor, vetoed
c) FIX-A: AMAT OI=37 + eda=6 + conviction=low → standard OI floor (100), vetoed
d) FIX-B: Stage 3 debate prompt includes earnings_days_to_event for eda != None
e) FIX-B: Stage 3 debate prompt includes WEIGHT hint for pre-event high conviction
f) FIX-C: post_event eda=-1 fresh_catalyst_override=False → returns []
g) FIX-C: post_event eda=-1 fresh_catalyst_override=True → RULE_POST_EARNINGS fires
h) FIX-C: event_day eda=0 → returns [] regardless of override
i) FIX-D: [POST-EARNINGS] tag appears in A1 signal output for eda < 0
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))
os.chdir(_BOT_DIR)

_THIRD_PARTY_STUBS = {
    "dotenv":                  None,
    "anthropic":               None,
    "alpaca":                  None,
    "alpaca.trading":          None,
    "alpaca.trading.client":   None,
    "alpaca.trading.requests": None,
    "alpaca.trading.enums":    None,
}
for _stub_name in _THIRD_PARTY_STUBS:
    if _stub_name not in sys.modules:
        _m = MagicMock()
        if _stub_name == "dotenv":
            _m.load_dotenv = MagicMock()
        sys.modules[_stub_name] = _m


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_pack(**overrides):
    from schemas import A2FeaturePack
    defaults = dict(
        symbol="AMAT",
        a1_signal_score=72.0,
        a1_direction="bullish",
        trend_score=None,
        momentum_score=None,
        sector_alignment="tech",
        iv_rank=93.0,
        iv_environment="very_expensive",
        term_structure_slope=None,
        skew=None,
        expected_move_pct=4.0,
        flow_imbalance_30m=None,
        sweep_count=None,
        gex_regime=None,
        oi_concentration=None,
        earnings_days_away=6,
        macro_event_flag=False,
        premium_budget_usd=3000.0,
        liquidity_score=0.8,
        built_at="2026-05-01T10:00:00",
        data_sources=["yfinance"],
        earnings_conviction=None,
    )
    defaults.update(overrides)
    return A2FeaturePack(**defaults)


def _make_candidate(oi, **extra):
    c = {
        "symbol": "AMAT",
        "open_interest": oi,
        "bid_ask_spread": 0.02,
        "theta_decay_score": 0.5,
        "max_loss_pct": 0.1,
        "dte": 7,
        "expected_value": 0.1,
        "structure": "credit_put_spread",
    }
    c.update(extra)
    return c


def _veto(pack, candidate, config=None):
    from bot_options_stage2_structures import _apply_veto_rules
    return _apply_veto_rules(candidate, pack, equity=50_000.0, config=config)


def _route(pack, *, config_overrides=None):
    from bot_options_stage2_structures import _route_strategy
    config = {"a2_router": config_overrides} if config_overrides else None
    return _route_strategy(pack, config=config, earnings_calendar_data={"calendar": []})


# ── FIX-A: OI floor lowered when earnings conviction high + pre-event ─────────

class TestFixAOIEarningsOverride(unittest.TestCase):

    def test_a_amat_oi37_eda6_conviction_high_passes_gate(self):
        """a) AMAT OI=37, eda=6, conviction=high → OI floor lowered to 25, passes."""
        pack = _make_pack(
            earnings_days_away=6,
            earnings_conviction={"eda": 6, "conviction_level": "high"},
        )
        candidate = _make_candidate(oi=37)
        result = _veto(pack, candidate)
        self.assertIsNone(result,
                          f"Expected no veto (OI=37 >= lowered floor=25), got: {result!r}")

    def test_b_amat_oi24_eda6_conviction_high_vetoed(self):
        """b) AMAT OI=24, eda=6, conviction=high → OI < 25 floor, still vetoed."""
        pack = _make_pack(
            earnings_days_away=6,
            earnings_conviction={"eda": 6, "conviction_level": "high"},
        )
        candidate = _make_candidate(oi=24)
        result = _veto(pack, candidate)
        self.assertIsNotNone(result,
                             "Expected veto (OI=24 < floor=25), but got None")
        self.assertIn("open_interest", result)

    def test_c_amat_oi37_eda6_conviction_low_vetoed(self):
        """c) AMAT OI=37, eda=6, conviction=low → standard floor (100), vetoed."""
        pack = _make_pack(
            earnings_days_away=6,
            earnings_conviction={"eda": 6, "conviction_level": "low"},
        )
        candidate = _make_candidate(oi=37)
        result = _veto(pack, candidate)
        self.assertIsNotNone(result,
                             "Expected veto (OI=37 < standard floor=100), but got None")
        self.assertIn("open_interest", result)


# ── FIX-B: Stage 3 earnings fields in debate prompt ───────────────────────────

class TestFixBStage3EarningsFields(unittest.TestCase):

    def _earnings_line(self, eda, conviction_level=None):
        from bot_options_stage3_debate import _fmt_earnings_line
        return _fmt_earnings_line(eda, conviction_level)

    def test_d_earnings_days_to_event_in_prompt(self):
        """d) earnings_line includes earnings_days_to_event label when eda is set."""
        line = self._earnings_line(eda=5, conviction_level="high")
        self.assertIn("earnings_days_to_event=5", line,
                      f"Expected earnings_days_to_event in line, got:\n{line!r}")

    def test_e_weight_hint_for_pre_event_high_conviction(self):
        """e) earnings_line includes WEIGHT hint for pre-event + high conviction."""
        line = self._earnings_line(eda=3, conviction_level="high")
        self.assertIn("WEIGHT", line,
                      f"Expected WEIGHT hint for pre-event high conviction:\n{line!r}")
        self.assertIn("premium buying", line.lower(),
                      f"Expected premium buying mention in WEIGHT hint:\n{line!r}")

    def test_d_no_earnings_line_when_eda_none(self):
        """earnings_line is empty string when eda=None."""
        line = self._earnings_line(eda=None, conviction_level=None)
        self.assertEqual(line, "",
                         f"Should return '' when eda=None, got:\n{line!r}")


# ── FIX-C: Timing-aware router ────────────────────────────────────────────────

class TestFixCTimingRouter(unittest.TestCase):

    def _route_post_event(self, eda, override=False):
        pack = _make_pack(
            symbol="NVDA",
            earnings_days_away=eda,
            iv_rank=80.0,
            iv_environment="expensive",
            a1_direction="bullish",
        )
        with patch("bot_options_stage2_structures._iv_already_crushed", return_value=False):
            return _route(pack, config_overrides={"fresh_catalyst_override": override})

    def test_f_post_event_eda_neg1_override_false_suppressed(self):
        """f) eda=-1, fresh_catalyst_override=False → BIAS-2: suppression removed; RULE_POST_EARNINGS fires."""
        result = self._route_post_event(eda=-1, override=False)
        self.assertNotEqual(result, [],
                            f"Expected RULE_POST_EARNINGS for eda=-1 (BIAS-2 removed suppression), got {result}")
        # iv_rank=80 >= post_earnings_iv_rank_min=75 → credit spread
        self.assertTrue(
            any(s in result for s in ("credit_put_spread", "credit_call_spread")),
            f"Expected credit spread from RULE_POST_EARNINGS, got {result}",
        )

    def test_g_post_event_eda_neg1_override_true_fires(self):
        """g) eda=-1, fresh_catalyst_override=True → RULE_POST_EARNINGS fires."""
        result = self._route_post_event(eda=-1, override=True)
        self.assertNotEqual(result, [],
                            f"Expected RULE_POST_EARNINGS for eda=-1 override=True, got {result}")
        # iv_rank=80 >= post_earnings_iv_rank_min=75 → credit spread
        self.assertTrue(
            any(s in result for s in ("credit_put_spread", "credit_call_spread")),
            f"Expected credit spread from RULE_POST_EARNINGS, got {result}",
        )

    def test_h_event_day_eda0_returns_empty(self):
        """h) eda=0 event_day → returns [] regardless of fresh_catalyst_override."""
        for override in (False, True):
            pack = _make_pack(
                symbol="NVDA",
                earnings_days_away=0,
                iv_rank=80.0,
                iv_environment="expensive",
                a1_direction="bullish",
            )
            result = _route(pack, config_overrides={"fresh_catalyst_override": override})
            self.assertEqual(result, [],
                             f"Expected [] for eda=0 (event_day), override={override}, got {result}")


# ── FIX-D: [POST-EARNINGS] tag in A1 signal output ───────────────────────────

class TestFixDPostEarningsTag(unittest.TestCase):

    def _format_signals(self, scored_symbols):
        from bot_stage2_signal import format_signal_scores
        return format_signal_scores({"scored_symbols": scored_symbols})

    def test_i_post_earnings_tag_for_negative_eda(self):
        """i) Symbol with eda < 0 gets [POST-EARNINGS] tag in formatted output."""
        signals = {
            "NVDA": {
                "score": 82,
                "conviction": "high",
                "primary_catalyst": "earnings beat",
                "earnings_days_away": -1,
                "signals": ["momentum"],
            },
            "AAPL": {
                "score": 65,
                "conviction": "medium",
                "primary_catalyst": "product launch",
                "earnings_days_away": None,
                "signals": ["breakout"],
            },
        }
        output = self._format_signals(signals)
        self.assertIn("[POST-EARNINGS]", output,
                      f"Expected [POST-EARNINGS] tag for eda=-1 symbol:\n{output}")
        nvda_line = next(
            (l for l in output.splitlines() if "NVDA" in l), ""
        )
        self.assertIn("[POST-EARNINGS]", nvda_line,
                      f"[POST-EARNINGS] should be on NVDA line, got:\n{nvda_line}")
        aapl_line = next(
            (l for l in output.splitlines() if "AAPL" in l), ""
        )
        self.assertNotIn("[POST-EARNINGS]", aapl_line,
                         f"AAPL (eda=None) should NOT have [POST-EARNINGS]:\n{aapl_line}")


if __name__ == "__main__":
    unittest.main()
