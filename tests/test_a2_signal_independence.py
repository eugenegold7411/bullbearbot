"""
tests/test_a2_signal_independence.py

Fix 1: pnl_unrealized-based stop/target exits in should_close_structure
Fix 2: IV skew direction override in _route_strategy
Fix 3: VIX regime-aware routing + caution score gate
"""

import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_leg(side="buy", filled_price=9.25, option_type="call",
               strike=417.5, expiration_days=15):
    from schemas import OptionsLeg
    expiration = (date.today() + timedelta(days=expiration_days)).isoformat()
    return OptionsLeg(
        occ_symbol  = "MSFT260515C00417500",
        underlying  = "MSFT",
        side        = side,
        qty         = 1,
        option_type = option_type,
        strike      = strike,
        expiration  = expiration,
        filled_price= filled_price,
    )


def _make_structure(strategy_name="single_call", contracts=4, legs=None,
                    pnl_unrealized=None, max_profit_usd=None,
                    expiration_days=15):
    from schemas import (  # noqa: E402
        OptionsStructure,
        OptionStrategy,
        StructureLifecycle,
        Tier,
    )
    strategy_map = {
        "single_call":       OptionStrategy.SINGLE_CALL,
        "call_debit_spread": OptionStrategy.CALL_DEBIT_SPREAD,
    }
    expiration = (date.today() + timedelta(days=expiration_days)).isoformat()
    struct = OptionsStructure(
        structure_id  = "test-fix1-001",
        underlying    = "MSFT",
        strategy      = strategy_map[strategy_name],
        lifecycle     = StructureLifecycle.FULLY_FILLED,
        legs          = legs or [],
        contracts     = contracts,
        max_cost_usd  = 5000.0,
        opened_at     = datetime.now(timezone.utc).isoformat(),
        catalyst      = "test",
        tier          = Tier.CORE,
        expiration    = expiration,
        max_profit_usd= max_profit_usd,
        pnl_unrealized= pnl_unrealized,
    )
    return struct


def _make_pack(**overrides):
    from schemas import A2FeaturePack
    defaults = dict(
        symbol               = "AAPL",
        a1_signal_score      = 72.0,
        a1_direction         = "bullish",
        trend_score          = None,
        momentum_score       = None,
        sector_alignment     = "technology",
        iv_rank              = 35.0,
        iv_environment       = "cheap",
        term_structure_slope = None,
        skew                 = None,
        expected_move_pct    = 4.5,
        flow_imbalance_30m   = None,
        sweep_count          = None,
        gex_regime           = None,
        oi_concentration     = None,
        earnings_days_away   = None,
        macro_event_flag     = False,
        premium_budget_usd   = 5000.0,
        liquidity_score      = 0.7,
        built_at             = datetime.now(timezone.utc).isoformat(),
        data_sources         = ["signal_scores", "iv_history"],
    )
    defaults.update(overrides)
    return A2FeaturePack(**defaults)


_NORMAL_REGIME = {"regime": "normal", "allowed_strategies": None, "size_multiplier": 1.0}
_ELEVATED_REGIME = {"regime": "elevated", "allowed_strategies": ["debit_spread", "credit_spread"],
                    "size_multiplier": 0.5}
_HIGH_REGIME = {"regime": "high", "allowed_strategies": ["credit_spread"], "size_multiplier": 0.25}


# ════════════════════════════════════════════════════════════════════════════
# Fix 1 — pnl_unrealized-based stop/target
# ════════════════════════════════════════════════════════════════════════════

class TestMaxLossExit(unittest.TestCase):

    def _close(self, struct, config=None):
        from options_executor import should_close_structure
        return should_close_structure(
            struct,
            current_prices={},
            config=config or {},
            current_time=None,
        )

    def test_max_loss_exit_triggers_at_threshold(self):
        """Fix 1: pnl_unrealized = -(cost_basis × 0.50) → max_loss_exit"""
        # cost_basis = 9.25 × 4 × 100 = $3,700; 50% = $1,850
        legs = [_make_leg(side="buy", filled_price=9.25, expiration_days=15)]
        struct = _make_structure(
            strategy_name  = "single_call",
            contracts      = 4,
            legs           = legs,
            pnl_unrealized = -1850.0,
            expiration_days= 15,
        )
        should_close, reason = self._close(struct)
        self.assertTrue(should_close, f"Expected close; got ({should_close}, {reason!r})")
        self.assertEqual(reason, "max_loss_exit")

    def test_max_loss_exit_below_threshold_does_not_trigger(self):
        """pnl_unrealized = -(cost_basis × 0.49) → should NOT trigger max_loss_exit"""
        legs = [_make_leg(side="buy", filled_price=9.25, expiration_days=15)]
        struct = _make_structure(
            strategy_name  = "single_call",
            contracts      = 4,
            legs           = legs,
            pnl_unrealized = -1813.0,  # < 50% of 3700
            expiration_days= 15,
        )
        should_close, reason = self._close(struct)
        # Should not close for max_loss — may close for time_stop but not max_loss_exit
        if should_close:
            self.assertNotEqual(reason, "max_loss_exit",
                                "49% loss should not trigger max_loss_exit")

    def test_profit_target_exit_triggers(self):
        """Fix 1: pnl_unrealized = max_profit × 0.75 → profit_target_pct_hit
        Also covers zero-net-debit spreads (both legs at same fill price)."""
        # Spread where both legs filled at 3.40 → net_debit=0, old check never fired
        buy_leg  = _make_leg(side="buy",  filled_price=3.40, strike=415.0, expiration_days=18)
        sell_leg = _make_leg(side="sell", filled_price=3.40, strike=420.0, expiration_days=18)
        struct = _make_structure(
            strategy_name  = "call_debit_spread",
            contracts      = 10,
            legs           = [buy_leg, sell_leg],
            max_profit_usd = 2025.0,
            pnl_unrealized = 1520.0,  # > 2025 × 0.75 = 1518.75
            expiration_days= 18,
        )
        should_close, reason = self._close(struct)
        self.assertTrue(should_close, f"Expected close; got ({should_close}, {reason!r})")
        self.assertEqual(reason, "profit_target_pct_hit")

    def test_profit_target_configurable(self):
        """account2.profit_target_pct=0.90 raises the bar; 75% pnl should NOT trigger"""
        buy_leg  = _make_leg(side="buy",  filled_price=3.40, strike=415.0, expiration_days=18)
        sell_leg = _make_leg(side="sell", filled_price=3.40, strike=420.0, expiration_days=18)
        struct = _make_structure(
            strategy_name  = "call_debit_spread",
            contracts      = 10,
            legs           = [buy_leg, sell_leg],
            max_profit_usd = 2025.0,
            pnl_unrealized = 1520.0,
            expiration_days= 18,
        )
        config = {"account2": {"profit_target_pct": 0.90}}
        should_close, reason = self._close(struct, config=config)
        if should_close:
            self.assertNotEqual(reason, "profit_target_pct_hit",
                                "75% pnl should not hit 90% target threshold")

    def test_force_close_structures_in_account2_subdict(self):
        """force_close_structures under account2 key triggers manual_close"""
        legs = [_make_leg(side="buy", filled_price=9.25, expiration_days=15)]
        struct = _make_structure(
            strategy_name  = "single_call",
            contracts      = 1,
            legs           = legs,
            expiration_days= 15,
        )
        # Use prefix matching — full ID is "test-fix1-001"
        config = {"account2": {"force_close_structures": ["test-fix1"]}}
        should_close, reason = self._close(struct, config=config)
        self.assertTrue(should_close)
        self.assertEqual(reason, "manual_close")


# ════════════════════════════════════════════════════════════════════════════
# Fix 2 — IV skew direction override
# ════════════════════════════════════════════════════════════════════════════

class TestIVSkewOverride(unittest.TestCase):

    def _route(self, pack, options_regime=None):
        from bot_options_stage2_structures import _route_strategy
        return _route_strategy(pack, options_regime=options_regime)

    def test_iv_skew_overrides_bullish_to_neutral(self):
        """Fix 2: skew=1.35 (> 1.30 neutral threshold) + a1_direction=bullish → neutral
        RULE_IRON fires for neutral + iv_rank=55 → iron_condor, not debit_call_spread"""
        pack = _make_pack(
            a1_direction    = "bullish",
            iv_rank         = 55.0,
            iv_environment  = "neutral",
            skew            = 1.35,
            liquidity_score = 0.7,
        )
        result = self._route(pack, options_regime=_NORMAL_REGIME)
        self.assertNotIn("debit_call_spread", result,
                         f"Bearish skew override should block debit calls; got {result}")
        self.assertTrue(
            any("iron" in s for s in result) or result == [],
            f"Expected iron condor or no trade after neutral skew override; got {result}",
        )

    def test_iv_skew_below_threshold_does_not_override(self):
        """skew=1.20 (< 1.30 threshold) + bullish → direction stays bullish"""
        pack = _make_pack(
            a1_direction    = "bullish",
            iv_rank         = 30.0,
            iv_environment  = "cheap",
            skew            = 1.20,
            liquidity_score = 0.7,
        )
        result = self._route(pack, options_regime=_NORMAL_REGIME)
        # RULE5 should fire: debit allowed
        self.assertIn("debit_call_spread", result,
                      f"Skew 1.20 below threshold should not override direction; got {result}")

    def test_bearish_skew_routes_bearish(self):
        """Fix 2 + Test 6: skew=1.55 (>= 1.50 bearish threshold) → effective_dir=bearish
        RULE5 cheap IV + bearish → put structures"""
        pack = _make_pack(
            a1_direction    = "bullish",
            iv_rank         = 30.0,
            iv_environment  = "cheap",
            skew            = 1.55,
            liquidity_score = 0.7,
        )
        result = self._route(pack, options_regime=_NORMAL_REGIME)
        self.assertIn("debit_put_spread", result,
                      f"Strong bearish skew should route to put structures; got {result}")
        self.assertNotIn("debit_call_spread", result,
                         f"Strong bearish skew should not include call debit; got {result}")

    def test_skew_none_does_not_affect_routing(self):
        """skew=None → no override, direction stays as a1_direction"""
        pack = _make_pack(
            a1_direction    = "bullish",
            iv_rank         = 30.0,
            iv_environment  = "cheap",
            skew            = None,
            liquidity_score = 0.7,
        )
        result = self._route(pack, options_regime=_NORMAL_REGIME)
        self.assertIn("debit_call_spread", result,
                      f"None skew should not change bullish routing; got {result}")


# ════════════════════════════════════════════════════════════════════════════
# Fix 3 — VIX regime-aware routing + caution score gate
# ════════════════════════════════════════════════════════════════════════════

class TestVIXRegimeFilter(unittest.TestCase):

    def _route(self, pack, options_regime=None):
        from bot_options_stage2_structures import _route_strategy
        return _route_strategy(pack, options_regime=options_regime)

    def test_high_vix_blocks_debit_call_spread(self):
        """Fix 3 + Test 4: VIX high regime → debit_call_spread removed from RULE7 result.
        Use iv_rank=35 to avoid RULE_IRON / RULE_SHORT_PUT (both need iv_rank >= 50)."""
        pack = _make_pack(
            a1_direction    = "bullish",
            iv_rank         = 35.0,
            iv_environment  = "expensive",
            skew            = None,
            liquidity_score = 0.7,
        )
        result = self._route(pack, options_regime=_HIGH_REGIME)
        # RULE7 would normally return ["credit_put_spread", "debit_call_spread"]
        # VIX high filter keeps only credit → ["credit_put_spread"]
        self.assertNotIn("debit_call_spread", result,
                         f"VIX high should block debit structures; got {result}")
        self.assertIn("credit_put_spread", result,
                      f"VIX high should retain credit structures; got {result}")

    def test_high_vix_rule5_cheap_iv_blocks_all_debit(self):
        """VIX high + cheap IV + bullish → RULE5 fires then filter removes all debit → []"""
        pack = _make_pack(
            a1_direction    = "bullish",
            iv_rank         = 30.0,
            iv_environment  = "cheap",
            skew            = None,
            liquidity_score = 0.7,
        )
        result = self._route(pack, options_regime=_HIGH_REGIME)
        # RULE5: ["long_call", "debit_call_spread"] → all debit → filtered to [] → no trade
        self.assertEqual(result, [],
                         f"VIX high + cheap IV = no credit options available → no trade; got {result}")

    def test_elevated_vix_removes_single_legs(self):
        """VIX elevated regime removes long_call/long_put from RULE5 result"""
        pack = _make_pack(
            a1_direction    = "bullish",
            iv_rank         = 30.0,
            iv_environment  = "cheap",
            skew            = None,
            liquidity_score = 0.7,
        )
        result = self._route(pack, options_regime=_ELEVATED_REGIME)
        # RULE5: ["long_call", "debit_call_spread"] → elevated removes long_call
        self.assertNotIn("long_call", result,
                         f"VIX elevated should remove single legs; got {result}")
        self.assertIn("debit_call_spread", result,
                      f"VIX elevated should keep debit spreads; got {result}")

    def test_crisis_vix_blocks_all_routes(self):
        """VIX crisis regime → early exit, no new positions"""
        pack = _make_pack(
            a1_direction    = "bullish",
            iv_rank         = 30.0,
            iv_environment  = "cheap",
            liquidity_score = 0.7,
        )
        crisis_regime = {"regime": "crisis", "allowed_strategies": [], "size_multiplier": 0.0}
        result = self._route(pack, options_regime=crisis_regime)
        self.assertEqual(result, [], f"Crisis VIX should block all routing; got {result}")

    def test_caution_regime_blocks_debit_entry(self):
        """Fix 3 + Test 5: a1_signal_score=40 (<50) + bullish → debit blocked
        RULE5 fires but caution filter removes debit → []"""
        pack = _make_pack(
            a1_signal_score = 40.0,
            a1_direction    = "bullish",
            iv_rank         = 30.0,
            iv_environment  = "cheap",
            skew            = None,
            liquidity_score = 0.7,
        )
        result = self._route(pack, options_regime=_NORMAL_REGIME)
        self.assertEqual(result, [],
                         f"Low-score caution should block all directional debit; got {result}")

    def test_caution_score_above_threshold_allows_debit(self):
        """a1_signal_score=55 (>= 50 threshold) → caution gate does not fire"""
        pack = _make_pack(
            a1_signal_score = 55.0,
            a1_direction    = "bullish",
            iv_rank         = 30.0,
            iv_environment  = "cheap",
            skew            = None,
            liquidity_score = 0.7,
        )
        result = self._route(pack, options_regime=_NORMAL_REGIME)
        self.assertIn("debit_call_spread", result,
                      f"Score 55 >= 50 should allow debit; got {result}")

    def test_normal_vix_no_regime_filter(self):
        """Normal VIX regime → routing unchanged from default behavior"""
        pack = _make_pack(
            a1_signal_score = 72.0,
            a1_direction    = "bullish",
            iv_rank         = 55.0,
            iv_environment  = "expensive",
            skew            = None,
            liquidity_score = 0.7,
        )
        result_with_regime   = self._route(pack, options_regime=_NORMAL_REGIME)
        result_without_regime = self._route(pack, options_regime=None)
        self.assertEqual(result_with_regime, result_without_regime,
                         "Normal regime should not change routing")


if __name__ == "__main__":
    unittest.main()
