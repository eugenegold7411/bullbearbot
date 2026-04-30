"""
tests/test_bearish_routing.py — BR: direction-aware routing for RULE5/RULE6/RULE7.

Validates that bearish signals receive put-side structures only, bullish signals
receive call-side structures only, and neutral falls through to the full list.

Tests:
  BR-01: RULE5 bullish  → ["long_call", "debit_call_spread"]
  BR-02: RULE5 bearish  → ["long_put",  "debit_put_spread"]
  BR-03: RULE5 neutral  → all four
  BR-04: RULE6 bullish  → ["debit_call_spread"]
  BR-05: RULE6 bearish  → ["debit_put_spread"]
  BR-06: RULE7 bullish  → ["credit_put_spread", "debit_call_spread"]
  BR-07: RULE7 bearish  → ["credit_call_spread", "debit_put_spread"]
  BR-08: Regression — existing routing tests still pass (spot-checks)
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
        symbol="TEST",
        a1_signal_score=70.0,
        a1_direction="bullish",
        trend_score=None,
        momentum_score=None,
        sector_alignment="technology",
        iv_rank=20.0,
        iv_environment="cheap",
        term_structure_slope=None,
        skew=None,
        expected_move_pct=4.0,
        flow_imbalance_30m=None,
        sweep_count=None,
        gex_regime=None,
        oi_concentration=None,
        earnings_days_away=None,
        macro_event_flag=False,
        premium_budget_usd=5000.0,
        liquidity_score=0.80,
        built_at=datetime.now(timezone.utc).isoformat(),
        data_sources=["signal_scores", "iv_history"],
    )
    defaults.update(overrides)
    return A2FeaturePack(**defaults)


def _route(pack, config=None):
    from bot_options_stage2_structures import _route_strategy
    return _route_strategy(pack, config=config)


# ════════════════════════════════════════════════════════════════════════════
# RULE5 — cheap IV, direction-aware
# ════════════════════════════════════════════════════════════════════════════

class TestRule5DirectionAware(unittest.TestCase):

    def test_BR01_rule5_bullish_call_side_only(self):
        """BR-01: RULE5 bullish → call-side structures only."""
        pack = _make_pack(iv_environment="cheap", iv_rank=20.0, a1_direction="bullish")
        result = _route(pack)
        self.assertEqual(sorted(result), sorted(["long_call", "debit_call_spread"]))

    def test_BR01_rule5_bullish_no_put_structures(self):
        """BR-01: RULE5 bullish must not include put-side structures."""
        pack = _make_pack(iv_environment="cheap", iv_rank=20.0, a1_direction="bullish")
        result = _route(pack)
        self.assertNotIn("long_put", result)
        self.assertNotIn("debit_put_spread", result)

    def test_BR02_rule5_bearish_put_side_only(self):
        """BR-02: RULE5 bearish → put-side structures only."""
        pack = _make_pack(iv_environment="cheap", iv_rank=20.0, a1_direction="bearish")
        result = _route(pack)
        self.assertEqual(sorted(result), sorted(["long_put", "debit_put_spread"]))

    def test_BR02_rule5_bearish_no_call_structures(self):
        """BR-02: RULE5 bearish must not include call-side structures."""
        pack = _make_pack(iv_environment="cheap", iv_rank=20.0, a1_direction="bearish")
        result = _route(pack)
        self.assertNotIn("long_call", result)
        self.assertNotIn("debit_call_spread", result)

    def test_BR03_rule5_neutral_no_trade(self):
        """BR-03: RULE5 outer guard requires non-neutral direction — neutral+cheap → RULE8 (no structures)."""
        pack = _make_pack(iv_environment="cheap", iv_rank=20.0, a1_direction="neutral",
                          a1_signal_score=50.0)
        result = _route(pack)
        self.assertEqual(result, [])

    def test_BR01_very_cheap_bullish_call_side_only(self):
        """BR-01 variant: very_cheap IV + bullish also filtered to call side."""
        pack = _make_pack(iv_environment="very_cheap", iv_rank=5.0, a1_direction="bullish")
        result = _route(pack)
        self.assertEqual(sorted(result), sorted(["long_call", "debit_call_spread"]))

    def test_BR02_very_cheap_bearish_put_side_only(self):
        """BR-02 variant: very_cheap IV + bearish also filtered to put side."""
        pack = _make_pack(iv_environment="very_cheap", iv_rank=5.0, a1_direction="bearish")
        result = _route(pack)
        self.assertEqual(sorted(result), sorted(["long_put", "debit_put_spread"]))


# ════════════════════════════════════════════════════════════════════════════
# RULE6 — neutral IV, direction-aware
# ════════════════════════════════════════════════════════════════════════════

class TestRule6DirectionAware(unittest.TestCase):

    def test_BR04_rule6_bullish_call_spread_only(self):
        """BR-04: RULE6 bullish → debit_call_spread only."""
        pack = _make_pack(iv_environment="neutral", iv_rank=40.0, a1_direction="bullish",
                          a1_signal_score=30.0)
        result = _route(pack)
        self.assertEqual(result, ["debit_call_spread"])

    def test_BR04_rule6_bullish_no_put_spread(self):
        """BR-04: RULE6 bullish must not include debit_put_spread."""
        pack = _make_pack(iv_environment="neutral", iv_rank=40.0, a1_direction="bullish",
                          a1_signal_score=30.0)
        result = _route(pack)
        self.assertNotIn("debit_put_spread", result)
        self.assertNotIn("long_put", result)

    def test_BR05_rule6_bearish_put_spread_only(self):
        """BR-05: RULE6 bearish → debit_put_spread only."""
        pack = _make_pack(iv_environment="neutral", iv_rank=40.0, a1_direction="bearish",
                          a1_signal_score=30.0)
        result = _route(pack)
        self.assertEqual(result, ["debit_put_spread"])

    def test_BR05_rule6_bearish_no_call_spread(self):
        """BR-05: RULE6 bearish must not include debit_call_spread."""
        pack = _make_pack(iv_environment="neutral", iv_rank=40.0, a1_direction="bearish",
                          a1_signal_score=30.0)
        result = _route(pack)
        self.assertNotIn("debit_call_spread", result)
        self.assertNotIn("long_call", result)


# ════════════════════════════════════════════════════════════════════════════
# RULE7 — expensive IV, direction-aware
# ════════════════════════════════════════════════════════════════════════════

class TestRule7DirectionAware(unittest.TestCase):
    """RULE7 fires for expensive IV + low conviction or low iv_rank (RULE_SHORT_PUT skipped)."""

    def _make_rule7_pack(self, direction: str) -> object:
        # iv_rank=35 < 50 → RULE_SHORT_PUT threshold not met → falls through to RULE7
        return _make_pack(
            iv_environment="expensive",
            iv_rank=35.0,
            a1_direction=direction,
            a1_signal_score=70.0,
        )

    def test_BR06_rule7_bullish_credit_put_and_debit_call(self):
        """BR-06: RULE7 bullish → ["credit_put_spread", "debit_call_spread"]."""
        pack = self._make_rule7_pack("bullish")
        result = _route(pack)
        self.assertEqual(sorted(result), sorted(["credit_put_spread", "debit_call_spread"]))

    def test_BR06_rule7_bullish_no_bearish_structures(self):
        """BR-06: RULE7 bullish must not include put-biased structures."""
        pack = self._make_rule7_pack("bullish")
        result = _route(pack)
        self.assertNotIn("credit_call_spread", result)
        self.assertNotIn("debit_put_spread", result)

    def test_BR07_rule7_bearish_credit_call_and_debit_put(self):
        """BR-07: RULE7 bearish → ["credit_call_spread", "debit_put_spread"]."""
        pack = self._make_rule7_pack("bearish")
        result = _route(pack)
        self.assertEqual(sorted(result), sorted(["credit_call_spread", "debit_put_spread"]))

    def test_BR07_rule7_bearish_no_bullish_structures(self):
        """BR-07: RULE7 bearish must not include call-biased structures."""
        pack = self._make_rule7_pack("bearish")
        result = _route(pack)
        self.assertNotIn("credit_put_spread", result)
        self.assertNotIn("debit_call_spread", result)


# ════════════════════════════════════════════════════════════════════════════
# BR-08 — Regression spot-checks (existing routing unchanged)
# ════════════════════════════════════════════════════════════════════════════

class TestBR08Regressions(unittest.TestCase):
    """Spot-check that rules outside RULE5/6/7 are unaffected."""

    def test_very_expensive_bullish_still_routes_to_credit_put(self):
        """RULE2_CREDIT: very_expensive + bullish → credit_put_spread."""
        pack = _make_pack(iv_environment="very_expensive", iv_rank=90.0, a1_direction="bullish")
        result = _route(pack)
        self.assertEqual(result, ["credit_put_spread"])

    def test_very_expensive_bearish_still_routes_to_credit_call(self):
        """RULE2_CREDIT: very_expensive + bearish → credit_call_spread."""
        pack = _make_pack(iv_environment="very_expensive", iv_rank=90.0, a1_direction="bearish")
        result = _route(pack)
        self.assertEqual(result, ["credit_call_spread"])

    def test_earnings_blackout_still_blocks(self):
        """RULE1: earnings ≤ blackout → empty list."""
        pack = _make_pack(iv_environment="cheap", iv_rank=20.0, a1_direction="bullish",
                          earnings_days_away=1)
        result = _route(pack)
        self.assertEqual(result, [])

    def test_rule5_bearish_not_in_cheap_bullish_result(self):
        """Bearish traces for cheap+bullish: no put structures in result."""
        pack = _make_pack(iv_environment="cheap", iv_rank=22.0, a1_direction="bullish",
                          symbol="META")
        result = _route(pack)
        self.assertNotIn("long_put", result)
        self.assertNotIn("debit_put_spread", result)

    def test_rule6_bearish_traces_meta(self):
        """META neutral IV + bearish → debit_put_spread only."""
        pack = _make_pack(iv_environment="neutral", iv_rank=40.0, a1_direction="bearish",
                          symbol="META", a1_signal_score=30.0)
        result = _route(pack)
        self.assertEqual(result, ["debit_put_spread"])

    def test_rule5_bullish_traces_tsm(self):
        """TSM cheap IV + bullish → call-side only."""
        pack = _make_pack(iv_environment="very_cheap", iv_rank=4.0, a1_direction="bullish",
                          symbol="TSM")
        result = _route(pack)
        self.assertEqual(sorted(result), sorted(["long_call", "debit_call_spread"]))

    def test_rule5_neutral_traces_spy(self):
        """SPY cheap IV + neutral → RULE8 (no trade; outer guard blocks neutral from RULE5)."""
        pack = _make_pack(iv_environment="very_cheap", iv_rank=0.0, a1_direction="neutral",
                          symbol="SPY", a1_signal_score=50.0)
        result = _route(pack)
        self.assertEqual(result, [])
