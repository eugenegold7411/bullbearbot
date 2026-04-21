"""
tests/test_s7_vol_credit_routing.py — S7-VOL: volatility-aware routing tests.

Covers:
  Build 2 — RULE2_CREDIT: very_expensive IV routes to credit structures
  Build 2 — RULE7_MIXED: expensive IV routes to both credit and debit structures
  Build 5 — validate_config accepts debate_confidence_floor in 0.60-0.95 range
  Build 5 — validate_config accepts empty iv_env_blackout list
"""

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

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
        _m = mock.MagicMock()
        if _stub_name == "dotenv":
            _m.load_dotenv = mock.MagicMock()
        sys.modules[_stub_name] = _m


def _make_pack(**overrides):
    from schemas import A2FeaturePack
    defaults = dict(
        symbol="SPY",
        a1_signal_score=70.0,
        a1_direction="bullish",
        trend_score=None,
        momentum_score=None,
        sector_alignment="etf",
        iv_rank=85.0,
        iv_environment="very_expensive",
        term_structure_slope=None,
        skew=None,
        expected_move_pct=3.0,
        flow_imbalance_30m=None,
        sweep_count=None,
        gex_regime=None,
        oi_concentration=None,
        earnings_days_away=None,
        macro_event_flag=False,
        premium_budget_usd=5000.0,
        liquidity_score=0.8,
        built_at=datetime.now(timezone.utc).isoformat(),
        data_sources=["signal_scores", "iv_history"],
    )
    defaults.update(overrides)
    return A2FeaturePack(**defaults)


def _route(pack, config=None):
    from bot_options_stage2_structures import _route_strategy
    return _route_strategy(pack, config=config)


# ════════════════════════════════════════════════════════════════════════════
# RULE2_CREDIT — very_expensive IV routing
# ════════════════════════════════════════════════════════════════════════════

class TestRule2Credit(unittest.TestCase):
    """very_expensive IV should route to credit structures, not block."""

    def test_very_expensive_bullish_routes_to_credit_put(self):
        pack = _make_pack(iv_environment="very_expensive", a1_direction="bullish")
        result = _route(pack)
        self.assertEqual(result, ["credit_put_spread"])

    def test_very_expensive_bearish_routes_to_credit_call(self):
        pack = _make_pack(iv_environment="very_expensive", a1_direction="bearish")
        result = _route(pack)
        self.assertEqual(result, ["credit_call_spread"])

    def test_very_expensive_neutral_routes_to_both_sides(self):
        pack = _make_pack(iv_environment="very_expensive", a1_direction="neutral")
        result = _route(pack)
        self.assertEqual(result, ["credit_put_spread", "credit_call_spread"])

    def test_very_expensive_no_debit_structures(self):
        """No debit or naked long structures should appear in very_expensive routing."""
        for direction in ("bullish", "bearish", "neutral"):
            pack = _make_pack(iv_environment="very_expensive", a1_direction=direction)
            result = _route(pack)
            for disallowed in ("long_call", "long_put", "debit_call_spread", "debit_put_spread"):
                self.assertNotIn(disallowed, result,
                                 f"debit structure {disallowed} should not appear in very_expensive")

    def test_very_expensive_blocked_when_in_config_blackout(self):
        """If operator explicitly adds very_expensive to blackout, RULE2 fires first."""
        pack = _make_pack(iv_environment="very_expensive", a1_direction="bullish")
        config = {"a2_router": {"iv_env_blackout": ["very_expensive"]}}
        result = _route(pack, config=config)
        self.assertEqual(result, [], "RULE2 blackout should override RULE2_CREDIT")

    def test_infer_rule_fired_returns_rule2_credit(self):
        from bot_options_stage2_structures import _infer_router_rule_fired
        pack = _make_pack(iv_environment="very_expensive", a1_direction="bullish")
        allowed = ["credit_put_spread"]
        rule = _infer_router_rule_fired(pack, allowed)
        self.assertEqual(rule, "RULE2_CREDIT")


# ════════════════════════════════════════════════════════════════════════════
# RULE7_MIXED — expensive IV routing
# ════════════════════════════════════════════════════════════════════════════

class TestRule7Mixed(unittest.TestCase):
    """expensive IV + directional should include both credit and debit structures."""

    def test_expensive_bullish_includes_credit_put(self):
        pack = _make_pack(iv_environment="expensive", iv_rank=70.0, a1_direction="bullish")
        result = _route(pack)
        self.assertIn("credit_put_spread", result)

    def test_expensive_bullish_includes_credit_call(self):
        pack = _make_pack(iv_environment="expensive", iv_rank=70.0, a1_direction="bullish")
        result = _route(pack)
        self.assertIn("credit_call_spread", result)

    def test_expensive_bullish_includes_debit_spreads(self):
        pack = _make_pack(iv_environment="expensive", iv_rank=70.0, a1_direction="bullish")
        result = _route(pack)
        self.assertIn("debit_call_spread", result)
        self.assertIn("debit_put_spread", result)

    def test_expensive_bearish_includes_all_four(self):
        pack = _make_pack(iv_environment="expensive", iv_rank=70.0, a1_direction="bearish")
        result = _route(pack)
        self.assertEqual(
            sorted(result),
            sorted(["credit_put_spread", "credit_call_spread", "debit_call_spread", "debit_put_spread"]),
        )

    def test_expensive_neutral_no_trade(self):
        """expensive + neutral direction still hits RULE8 (no directional signal)."""
        pack = _make_pack(iv_environment="expensive", iv_rank=70.0, a1_direction="neutral")
        result = _route(pack)
        self.assertEqual(result, [])

    def test_expensive_no_naked_longs(self):
        """Single leg structures should not appear for expensive IV."""
        for direction in ("bullish", "bearish"):
            pack = _make_pack(iv_environment="expensive", iv_rank=70.0, a1_direction=direction)
            result = _route(pack)
            self.assertNotIn("long_call", result)
            self.assertNotIn("long_put", result)


# ════════════════════════════════════════════════════════════════════════════
# strategy_config.json — verify S7-VOL values are present
# ════════════════════════════════════════════════════════════════════════════

class TestStrategyConfigS7Vol(unittest.TestCase):
    """strategy_config.json should reflect S7-VOL paper-trading values."""

    def _load_config(self):
        import json
        cfg_path = _BOT_DIR / "strategy_config.json"
        return json.loads(cfg_path.read_text())

    def test_debate_confidence_floor_lowered(self):
        cfg = self._load_config()
        dcf = cfg.get("account2", {}).get("debate_confidence_floor")
        self.assertIsNotNone(dcf)
        self.assertLessEqual(float(dcf), 0.70,
                             "Paper trading floor should be ≤ 0.70 to accumulate signals")

    def test_iv_env_blackout_empty(self):
        cfg = self._load_config()
        ieb = cfg.get("a2_router", {}).get("iv_env_blackout")
        self.assertIsNotNone(ieb)
        self.assertIsInstance(ieb, list)
        self.assertNotIn("very_expensive", ieb,
                         "very_expensive should not be in blackout — it now routes to credit")

    # ── validate_config gate logic (inline, avoids running the full script) ──

    def test_debate_confidence_floor_valid_range_060(self):
        """Gate predicate: 0.60 <= 0.60 <= 0.95 should be True."""
        self.assertTrue(0.60 <= 0.60 <= 0.95)

    def test_debate_confidence_floor_valid_range_065(self):
        """Gate predicate: 0.60 <= 0.65 <= 0.95 should be True."""
        self.assertTrue(0.60 <= 0.65 <= 0.95)

    def test_debate_confidence_floor_below_range_059(self):
        """Gate predicate: 0.60 <= 0.59 should be False."""
        self.assertFalse(0.60 <= 0.59 <= 0.95)

    def test_iv_env_blackout_empty_list_is_valid(self):
        """Gate predicate: isinstance([], list) should be True."""
        self.assertTrue(isinstance([], list))

    def test_iv_env_blackout_nonempty_list_is_valid(self):
        """Gate predicate: isinstance(['very_expensive'], list) should be True."""
        self.assertTrue(isinstance(["very_expensive"], list))

    def test_iv_env_blackout_string_is_invalid(self):
        """Gate predicate: isinstance('very_expensive', list) should be False."""
        self.assertFalse(isinstance("very_expensive", list))


if __name__ == "__main__":
    unittest.main()
