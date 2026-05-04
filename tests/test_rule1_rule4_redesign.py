"""
tests/test_rule1_rule4_redesign.py — Earnings routing + RULE4 macro routing.

RULE1 has been removed (earnings_dte_blackout=0). Symbols near earnings now route
normally via IV environment rules (no blanket earnings block).

Covers:
  R1-01: eda=0 timing=unknown -> routes normally (RULE6: debit_call_spread)
  R1-02: eda=1 pre_market iv_rank=95 bullish -> credit_put_spread (RULE2_CREDIT)
  R1-03: eda=1 pre_market iv_rank=30 bullish -> debit_call_spread + straddle (RULE_EARNINGS)
  R1-04: eda=1 pre_market iv_rank=55 bullish -> debit_call_spread + straddle (RULE_EARNINGS)
  R1-05: eda=2 iv_rank=88 bullish -> credit_put_spread (RULE2_CREDIT)
  R1-06: eda=2 iv_rank=25 bullish -> debit_call_spread + straddle (RULE_EARNINGS)
  R1-07: eda=2 direction=neutral -> straddle (RULE_EARNINGS)
  R1-08: eda=4 iv_rank=100 -> RULE_EARNINGS_HIGH_IV fires first (disabled by default)
  R1-09: eda=-1 -> RULE_POST_EARNINGS fires
  R4-01: macro_flag=True iv_rank=72 neutral -> iron_condor or iron_butterfly in results
  R4-02: macro_flag=True iv_rank=88 bullish -> credit_put_spread
  R4-03: macro_flag=True iv_rank=25 bearish -> debit_put_spread
  R4-04: macro_flag=True iv_rank=50 neutral -> blocked
  R4-05: macro_event_routing_enabled=false macro_flag=True iv_rank=72 -> blocked (original)
  R4-06: macro_flag=False -> rule skipped entirely

Scenario traces:
  XOM: eda=1, timing=pre_market, iv_rank=95, direction=bullish -> credit_put_spread
  CVX: eda=1, timing=pre_market, iv_rank=89, direction=bullish -> credit_put_spread
  LLY: eda=0, iv_rank=7, direction=bullish -> routes normally (long_call/debit_call_spread)
  AAPL: eda=0, iv_rank=94, direction=neutral -> routes normally (credit_put_spread/credit_call_spread)
  PLTR: eda=4, iv_rank=100, direction=bullish -> RULE_EARNINGS_HIGH_IV (disabled by default)
  META: eda=-1, iv_rank=13, direction=bearish -> RULE_POST_EARNINGS or fall-through
  Powell: macro_flag=True, iv_rank=72, direction=neutral -> iron_condor/iron_butterfly
"""

import os
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock
from unittest.mock import patch

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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_pack(**overrides):
    from schemas import A2FeaturePack
    defaults = dict(
        symbol="XOM",
        a1_signal_score=72.0,
        a1_direction="bullish",
        trend_score=None,
        momentum_score=None,
        sector_alignment="energy",
        iv_rank=50.0,
        iv_environment="neutral",
        term_structure_slope=None,
        skew=None,
        expected_move_pct=3.5,
        flow_imbalance_30m=None,
        sweep_count=None,
        gex_regime=None,
        oi_concentration=None,
        earnings_days_away=None,
        macro_event_flag=False,
        premium_budget_usd=3000.0,
        liquidity_score=0.8,
        built_at="2026-04-30T10:00:00",
        data_sources=["yfinance"],
    )
    defaults.update(overrides)
    return A2FeaturePack(**defaults)


def _route(pack, *, timing=None, config_overrides=None):
    """
    Call _route_strategy with a fake earnings calendar that returns `timing`
    for the pack's symbol. Passes config dict from config_overrides if given.
    """
    from bot_options_stage2_structures import _route_strategy

    today = date.today()
    eda = pack.earnings_days_away or 0
    # Build a fake calendar entry for the symbol
    eda_date = date.fromordinal(today.toordinal() + eda)
    cal = {"calendar": []}
    if timing is not None:
        cal["calendar"].append({
            "symbol": pack.symbol,
            "earnings_date": eda_date.isoformat(),
            "timing": timing,
        })
    config = None
    if config_overrides is not None:
        config = {"a2_router": config_overrides}
    return _route_strategy(pack, config=config, earnings_calendar_data=cal)


def _route_no_cal(pack, config_overrides=None):
    """Call _route_strategy with empty calendar (timing=unknown)."""
    from bot_options_stage2_structures import _route_strategy
    config = None
    if config_overrides is not None:
        config = {"a2_router": config_overrides}
    return _route_strategy(pack, config=config, earnings_calendar_data={"calendar": []})


# ── Earnings routing tests (RULE1 removed) ────────────────────────────────────

class TestRule1SmartEarningsRouter(unittest.TestCase):

    def test_R1_01_eda0_unknown_timing_routes_normally(self):
        """R1-01: eda=0 -> RULE1 removed, routes via IV rules (RULE6: debit_call_spread)."""
        pack = _make_pack(symbol="LLY", earnings_days_away=0, iv_rank=7,
                          a1_direction="bullish")
        result = _route(pack, timing=None)
        self.assertEqual(result, ["debit_call_spread"],
                         f"Expected RULE6 debit_call_spread for eda=0 neutral IV, got {result}")

    def test_R1_01b_eda0_postmarket_routes_normally(self):
        """R1-01b: eda=0 timing=post_market -> RULE1 removed, routes via IV rules."""
        pack = _make_pack(symbol="LLY", earnings_days_away=0, iv_rank=7,
                          a1_direction="bullish")
        result = _route(pack, timing="post_market")
        self.assertEqual(result, ["debit_call_spread"],
                         f"Expected RULE6 debit_call_spread for eda=0 post_market, got {result}")

    def test_R1_01c_eda0_premarket_falls_through(self):
        """R1-01c: eda=0 timing=pre_market -> routes via IV rules (RULE1 removed)."""
        pack = _make_pack(symbol="LLY", earnings_days_away=0, iv_rank=7,
                          iv_environment="very_cheap", a1_direction="bullish",
                          macro_event_flag=False)
        result = _route(pack, timing="pre_market")
        # Normal routing for very_cheap + bullish: RULE5 -> long_call/debit_call_spread
        self.assertIn("debit_call_spread", result,
                      f"Expected RULE5 routing for eda=0 pre_market, got {result}")

    def test_R1_02_eda1_premarket_high_iv_bullish_credit(self):
        """R1-02: eda=1 pre_market iv_rank=95 bullish -> credit_put_spread."""
        pack = _make_pack(symbol="XOM", earnings_days_away=1, iv_rank=95,
                          iv_environment="very_expensive", a1_direction="bullish")
        result = _route(pack, timing="pre_market")
        self.assertEqual(result, ["credit_put_spread"],
                         f"Expected credit_put_spread for XOM eda=1 pre_market iv=95, got {result}")

    def test_R1_02b_eda1_premarket_high_iv_bearish_credit(self):
        """R1-02b: eda=1 pre_market iv_rank=89 bearish -> credit_call_spread (CVX case)."""
        pack = _make_pack(symbol="CVX", earnings_days_away=1, iv_rank=89,
                          iv_environment="very_expensive", a1_direction="bearish")
        result = _route(pack, timing="pre_market")
        self.assertEqual(result, ["credit_call_spread"],
                         f"Expected credit_call_spread for CVX eda=1 pre_market iv=89, got {result}")

    def test_R1_03_eda1_premarket_low_iv_bullish_debit(self):
        """R1-03: eda=1 iv_rank=30 bullish -> RULE_EARNINGS fires (debit_call_spread + straddle)."""
        pack = _make_pack(symbol="XOM", earnings_days_away=1, iv_rank=30,
                          iv_environment="cheap", a1_direction="bullish")
        result = _route(pack, timing="pre_market")
        self.assertEqual(result, ["debit_call_spread", "straddle"],
                         f"Expected RULE_EARNINGS debit+straddle for eda=1 iv=30, got {result}")

    def test_R1_04_eda1_premarket_middle_iv_routes(self):
        """R1-04: eda=1 iv_rank=55 bullish -> RULE_EARNINGS fires (debit_call_spread + straddle)."""
        pack = _make_pack(symbol="XOM", earnings_days_away=1, iv_rank=55,
                          iv_environment="neutral", a1_direction="bullish")
        result = _route(pack, timing="pre_market")
        self.assertEqual(result, ["debit_call_spread", "straddle"],
                         f"Expected RULE_EARNINGS for eda=1 iv=55 bullish, got {result}")

    def test_R1_04b_eda1_premarket_neutral_direction_credit(self):
        """R1-04b: eda=1 iv_rank=95 very_expensive neutral -> RULE2_CREDIT fires."""
        pack = _make_pack(symbol="XOM", earnings_days_away=1, iv_rank=95,
                          iv_environment="very_expensive", a1_direction="neutral")
        result = _route(pack, timing="pre_market")
        self.assertIn("credit_put_spread", result,
                      f"Expected RULE2_CREDIT for eda=1 very_expensive neutral, got {result}")

    def test_R1_05_eda2_high_iv_bullish_credit(self):
        """R1-05: eda=2 iv_rank=88 bullish -> credit_put_spread."""
        pack = _make_pack(symbol="XOM", earnings_days_away=2, iv_rank=88,
                          iv_environment="very_expensive", a1_direction="bullish")
        result = _route_no_cal(pack)
        self.assertEqual(result, ["credit_put_spread"],
                         f"Expected credit_put_spread for eda=2 iv=88, got {result}")

    def test_R1_06_eda2_low_iv_bullish_debit(self):
        """R1-06: eda=2 iv_rank=25 bullish -> RULE_EARNINGS fires (debit_call_spread + straddle)."""
        pack = _make_pack(symbol="XOM", earnings_days_away=2, iv_rank=25,
                          iv_environment="cheap", a1_direction="bullish")
        result = _route_no_cal(pack)
        self.assertEqual(result, ["debit_call_spread", "straddle"],
                         f"Expected RULE_EARNINGS debit+straddle for eda=2 iv=25, got {result}")

    def test_R1_07_eda2_neutral_direction_straddle(self):
        """R1-07: eda=2 direction=neutral -> RULE_EARNINGS fires (straddle only)."""
        pack = _make_pack(symbol="XOM", earnings_days_away=2, iv_rank=50,
                          iv_environment="neutral", a1_direction="neutral")
        result = _route_no_cal(pack)
        self.assertEqual(result, ["straddle"],
                         f"Expected RULE_EARNINGS straddle for eda=2 neutral, got {result}")

    def test_R1_08_eda4_high_iv_rule_earnings_high_iv_fires_first(self):
        """R1-08: eda=4 iv_rank=100 -> RULE_EARNINGS_HIGH_IV DTE window is [7,14], eda=4 misses it."""
        # eda=4 NOT in [7, 14] range → RULE_EARNINGS_HIGH_IV doesn't fire.
        # iv_env=very_expensive → RULE2_CREDIT fires → credit_put_spread.
        pack = _make_pack(symbol="PLTR", earnings_days_away=4, iv_rank=100,
                          iv_environment="very_expensive", a1_direction="bullish")
        result = _route_no_cal(pack,
                               config_overrides={
                                   "pre_earnings_credit_spread_enabled": True,
                                   "pre_earnings_iv_rank_min": 85,
                                   "pre_earnings_dte_min": 7,
                                   "pre_earnings_dte_max": 14,
                                   "earnings_dte_blackout": 2,
                               })
        # eda=4 not in [7,14] -> RULE_EARNINGS_HIGH_IV doesn't fire
        # iv_env=very_expensive -> RULE2_CREDIT fires -> credit_put_spread
        self.assertEqual(result, ["credit_put_spread"],
                         f"Expected RULE2_CREDIT for eda=4, got {result}")

    def test_R1_08b_eda7_high_iv_rule_earnings_high_iv_fires_first(self):
        """R1-08b: eda=7 iv_rank=100 -> RULE_EARNINGS_HIGH_IV fires (eda in [7,14] window)."""
        pack = _make_pack(symbol="PLTR", earnings_days_away=7, iv_rank=100,
                          iv_environment="very_expensive", a1_direction="bullish")
        result = _route_no_cal(pack,
                               config_overrides={
                                   "pre_earnings_credit_spread_enabled": True,
                                   "pre_earnings_iv_rank_min": 85,
                                   "pre_earnings_dte_min": 7,
                                   "pre_earnings_dte_max": 14,
                                   "earnings_dte_blackout": 2,
                               })
        # RULE_EARNINGS_HIGH_IV: eda=7 in [7,14], iv_rank=100 >= 85, bullish -> credit_put_spread
        self.assertEqual(result, ["credit_put_spread"],
                         f"Expected RULE_EARNINGS_HIGH_IV for eda=7 iv=100, got {result}")

    def test_R1_09_eda_negative_routes_to_post_earnings_not_rule1(self):
        """R1-09: eda=-1 -> RULE_POST_EARNINGS fires, not RULE1."""
        today = date.today()
        from datetime import timedelta
        yesterday = (today - timedelta(days=1)).isoformat()
        cal = {"calendar": [{
            "symbol": "META",
            "earnings_date": yesterday,
            "timing": "post_market",
        }]}
        pack = _make_pack(symbol="META", earnings_days_away=-1, iv_rank=80,
                          iv_environment="expensive", a1_direction="bearish",
                          liquidity_score=0.8)
        from bot_options_stage2_structures import _route_strategy
        # Patch _iv_already_crushed so the test is not sensitive to real IV history files
        # on the server — this test validates routing logic, not the crush-detection heuristic.
        with patch("bot_options_stage2_structures._iv_already_crushed", return_value=False):
            result = _route_strategy(pack, earnings_calendar_data=cal)
        # iv_rank=80 >= post_earnings_iv_rank_min default 75 -> RULE_POST_EARNINGS fires
        self.assertEqual(result, ["credit_call_spread"],
                         f"Expected RULE_POST_EARNINGS for eda=-1, got {result}")


# ── RULE4 tests ───────────────────────────────────────────────────────────────

class TestRule4MacroEventRouter(unittest.TestCase):

    def test_R4_01_macro_flag_elevated_iv_neutral_iron_structures(self):
        """R4-01: macro_flag=True iv_rank=72 neutral -> iron_condor or iron_butterfly."""
        pack = _make_pack(symbol="SPY", macro_event_flag=True, iv_rank=72,
                          iv_environment="expensive", a1_direction="neutral",
                          earnings_days_away=None)
        result = _route_no_cal(pack)
        self.assertTrue(
            any(s in result for s in ("iron_condor", "iron_butterfly")),
            f"Expected iron_condor or iron_butterfly for macro neutral iv=72, got {result}"
        )

    def test_R4_02_macro_flag_very_high_iv_bullish_credit(self):
        """R4-02: macro_flag=True iv_rank=88 bullish -> credit_put_spread."""
        pack = _make_pack(symbol="SPY", macro_event_flag=True, iv_rank=88,
                          iv_environment="very_expensive", a1_direction="bullish",
                          earnings_days_away=None)
        result = _route_no_cal(pack)
        self.assertEqual(result, ["credit_put_spread"],
                         f"Expected credit_put_spread for macro bullish iv=88, got {result}")

    def test_R4_03_macro_flag_low_iv_bearish_debit(self):
        """R4-03: macro_flag=True iv_rank=25 bearish -> debit_put_spread."""
        pack = _make_pack(symbol="SPY", macro_event_flag=True, iv_rank=25,
                          iv_environment="cheap", a1_direction="bearish",
                          earnings_days_away=None)
        result = _route_no_cal(pack)
        self.assertEqual(result, ["debit_put_spread"],
                         f"Expected debit_put_spread for macro bearish iv=25, got {result}")

    def test_R4_04_macro_flag_mid_iv_neutral_blocked(self):
        """R4-04: macro_flag=True iv_rank=50 neutral -> blocked (not elevated enough for condor)."""
        pack = _make_pack(symbol="SPY", macro_event_flag=True, iv_rank=50,
                          iv_environment="neutral", a1_direction="neutral",
                          earnings_days_away=None)
        result = _route_no_cal(pack)
        self.assertEqual(result, [],
                         f"Expected [] for macro neutral iv=50 (below condor_iv_min=70), got {result}")

    def test_R4_05_routing_disabled_reverts_to_original_block(self):
        """R4-05: macro_event_routing_enabled=false -> original block behavior for iv>gate."""
        pack = _make_pack(symbol="SPY", macro_event_flag=True, iv_rank=72,
                          iv_environment="expensive", a1_direction="neutral",
                          earnings_days_away=None)
        result = _route_no_cal(pack, config_overrides={
            "macro_event_routing_enabled": False,
            "macro_iv_gate_rank": 70,
        })
        self.assertEqual(result, [],
                         f"Expected [] when routing disabled and iv=72 > gate=70, got {result}")

    def test_R4_05b_routing_disabled_iv_below_gate_not_blocked(self):
        """R4-05b: macro_event_routing_enabled=false iv_rank=50 -> not blocked (below original gate)."""
        pack = _make_pack(symbol="SPY", macro_event_flag=True, iv_rank=50,
                          iv_environment="neutral", a1_direction="bullish",
                          earnings_days_away=None)
        result = _route_no_cal(pack, config_overrides={
            "macro_event_routing_enabled": False,
            "macro_iv_gate_rank": 60,
        })
        # iv=50 < gate=60 -> original RULE4 doesn't block, routing continues
        # RULE6: neutral + bullish -> debit_call_spread
        self.assertNotEqual(result, [],
                            f"Expected non-empty for macro routing disabled iv=50 < gate, got {result}")

    def test_R4_06_no_macro_flag_rule_skipped(self):
        """R4-06: macro_flag=False -> RULE4 not triggered, normal routing proceeds."""
        pack = _make_pack(symbol="SPY", macro_event_flag=False, iv_rank=72,
                          iv_environment="expensive", a1_direction="bullish",
                          earnings_days_away=None)
        result = _route_no_cal(pack)
        # No macro flag -> RULE4 skipped, falls through to normal routing
        # RULE7: expensive + bullish -> credit_put_spread or debit_call_spread
        self.assertNotEqual(result, [],
                            f"Expected non-empty when macro_flag=False, got {result}")


# ── Scenario trace tests ──────────────────────────────────────────────────────

class TestScenarioTraces(unittest.TestCase):
    """End-to-end scenario traces confirming expected routes."""

    def _make_calendar(self, symbol, eda, timing):
        today = date.today()
        from datetime import timedelta
        eda_date = (today + timedelta(days=eda)).isoformat()
        return {"calendar": [{"symbol": symbol, "earnings_date": eda_date, "timing": timing}]}

    def test_scenario_xom_eda1_premarket_iv95_bullish(self):
        """XOM: eda=1, timing=pre_market, iv_rank=95, direction=bullish -> credit_put_spread."""
        pack = _make_pack(symbol="XOM", earnings_days_away=1, iv_rank=95,
                          iv_environment="very_expensive", a1_direction="bullish")
        from bot_options_stage2_structures import _route_strategy
        result = _route_strategy(pack,
                                 earnings_calendar_data=self._make_calendar("XOM", 1, "pre_market"))
        self.assertEqual(result, ["credit_put_spread"], f"XOM scenario failed: {result}")

    def test_scenario_cvx_eda1_premarket_iv89_bullish(self):
        """CVX: eda=1, timing=pre_market, iv_rank=89, direction=bullish -> credit_put_spread."""
        pack = _make_pack(symbol="CVX", earnings_days_away=1, iv_rank=89,
                          iv_environment="very_expensive", a1_direction="bullish")
        from bot_options_stage2_structures import _route_strategy
        result = _route_strategy(pack,
                                 earnings_calendar_data=self._make_calendar("CVX", 1, "pre_market"))
        self.assertEqual(result, ["credit_put_spread"], f"CVX scenario failed: {result}")

    def test_scenario_lly_eda0_routes_normally(self):
        """LLY: eda=0, iv_rank=7, very_cheap, bullish -> RULE5 fires (long_call/debit_call_spread)."""
        pack = _make_pack(symbol="LLY", earnings_days_away=0, iv_rank=7,
                          iv_environment="very_cheap", a1_direction="bullish")
        from bot_options_stage2_structures import _route_strategy
        result = _route_strategy(pack,
                                 earnings_calendar_data=self._make_calendar("LLY", 0, "post_market"))
        self.assertIn("long_call", result, f"LLY scenario failed: expected RULE5, got {result}")
        self.assertIn("debit_call_spread", result, f"LLY scenario failed: {result}")

    def test_scenario_aapl_eda0_credit_routes(self):
        """AAPL: eda=0, iv_rank=94, very_expensive, neutral -> RULE2_CREDIT fires."""
        pack = _make_pack(symbol="AAPL", earnings_days_away=0, iv_rank=94,
                          iv_environment="very_expensive", a1_direction="neutral")
        from bot_options_stage2_structures import _route_strategy
        result = _route_strategy(pack,
                                 earnings_calendar_data=self._make_calendar("AAPL", 0, "post_market"))
        self.assertIn("credit_put_spread", result,
                      f"AAPL scenario failed: expected RULE2_CREDIT, got {result}")

    def test_scenario_pltr_eda4_iv100_rule_earnings_high_iv(self):
        """PLTR: eda=4, iv_rank=100 -> eda not in RULE_EARNINGS_HIGH_IV [7,14]; RULE2_CREDIT fires."""
        pack = _make_pack(symbol="PLTR", earnings_days_away=4, iv_rank=100,
                          iv_environment="very_expensive", a1_direction="bullish")
        from bot_options_stage2_structures import _route_strategy
        result = _route_strategy(pack,
                                 config={"a2_router": {
                                     "pre_earnings_credit_spread_enabled": True,
                                     "pre_earnings_iv_rank_min": 85,
                                     "pre_earnings_dte_min": 7,
                                     "pre_earnings_dte_max": 14,
                                     "earnings_dte_blackout": 2,
                                 }},
                                 earnings_calendar_data={"calendar": []})
        # eda=4 not in [7,14] -> RULE_EARNINGS_HIGH_IV doesn't fire
        # RULE2_CREDIT: very_expensive + bullish -> credit_put_spread
        self.assertEqual(result, ["credit_put_spread"],
                         f"PLTR eda=4 scenario failed: {result}")

    def test_scenario_meta_eda_negative(self):
        """META: eda=-1, iv_rank=13 -> RULE_POST_EARNINGS check (iv too low, falls through)."""
        from datetime import timedelta
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        pack = _make_pack(symbol="META", earnings_days_away=-1, iv_rank=13,
                          iv_environment="very_cheap", a1_direction="bearish",
                          liquidity_score=0.8)
        from bot_options_stage2_structures import _route_strategy
        # iv_rank=13 < post_earnings_iv_rank_min default 75 -> POST_EARNINGS won't fire
        # Falls through to RULE5: very_cheap + bearish -> long_put, debit_put_spread
        cal = {"calendar": [{"symbol": "META", "earnings_date": yesterday,
                             "timing": "post_market"}]}
        result = _route_strategy(pack, earnings_calendar_data=cal)
        # Should route to debit/long structures since IV is cheap
        self.assertTrue(len(result) > 0,
                        f"META eda=-1 low iv scenario should route somewhere, got {result}")

    def test_scenario_powell_macro_event_neutral_condor(self):
        """Powell: macro_flag=True, iv_rank=72, direction=neutral -> iron_condor/iron_butterfly."""
        pack = _make_pack(symbol="SPY", macro_event_flag=True, iv_rank=72,
                          iv_environment="expensive", a1_direction="neutral",
                          earnings_days_away=None)
        from bot_options_stage2_structures import _route_strategy
        result = _route_strategy(pack, earnings_calendar_data={"calendar": []})
        self.assertTrue(
            any(s in result for s in ("iron_condor", "iron_butterfly")),
            f"Powell scenario failed (expected iron structure): {result}"
        )


if __name__ == "__main__":
    unittest.main()
