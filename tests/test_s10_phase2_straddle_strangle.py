"""
tests/test_s10_phase2_straddle_strangle.py — Sprint 10 Phase 2 tests

Long Straddle + Long Strangle — builder, router, executor end-to-end.

Builder tests (ST-01 to ST-08):
  ATM strike selection, straddle/strangle construction, budget rejection,
  OI/spread failures, missing expiry, delta fallback.

Router tests (ST-09 to ST-16):
  RULE_STRADDLE_STRANGLE fires on correct conditions, boundary tests.

Executor tests (ST-17 to ST-20):
  mleg limit_price is positive, _compute_net_mid buy-buy, mleg 2-leg submission.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

# ── Alpaca mock helpers (same pattern as test_s4_mleg_submission.py) ──────────

class _MockLimitOrderRequest:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class _MockOptionLegRequest:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class _OrderClass:
    MLEG = "mleg"


class _TIFValue:
    """Enum-like with a .value attribute so code using tif.value doesn't crash."""
    def __init__(self, v: str):
        self.value = v

    def __eq__(self, other):
        if isinstance(other, _TIFValue):
            return self.value == other.value
        return self.value == other

    def __repr__(self):
        return self.value


class _TimeInForce:
    DAY = _TIFValue("day")
    GTC = _TIFValue("gtc")


class _PositionIntent:
    BUY_TO_OPEN = "buy_to_open"
    SELL_TO_OPEN = "sell_to_open"


class _OrderSide:
    BUY = "buy"
    SELL = "sell"


def _alpaca_modules():
    enums_mod = MagicMock()
    enums_mod.OrderClass = _OrderClass
    enums_mod.TimeInForce = _TimeInForce
    enums_mod.PositionIntent = _PositionIntent
    enums_mod.OrderSide = _OrderSide
    requests_mod = MagicMock()
    requests_mod.LimitOrderRequest = _MockLimitOrderRequest
    requests_mod.OptionLegRequest = _MockOptionLegRequest
    return {
        "alpaca": MagicMock(),
        "alpaca.trading": MagicMock(),
        "alpaca.trading.enums": enums_mod,
        "alpaca.trading.requests": requests_mod,
    }


# ── shared chain fixture factory ──────────────────────────────────────────────

def _make_chain(
    spot: float = 200.0,
    dte: int = 21,
    call_bid: float = 3.0,
    call_ask: float = 3.40,
    call_delta: float = 0.50,
    call_oi: int = 500,
    put_bid: float = 2.90,
    put_ask: float = 3.30,
    put_delta: float = -0.50,
    put_oi: int = 480,
    otm_call_strike: float = 210.0,
    otm_call_bid: float = 1.60,
    otm_call_ask: float = 1.80,
    otm_call_delta: float = 0.30,
    otm_call_oi: int = 300,
    otm_put_strike: float = 190.0,
    otm_put_bid: float = 1.50,
    otm_put_ask: float = 1.70,
    otm_put_delta: float = -0.30,
    otm_put_oi: int = 280,
    volume: int = 50,
) -> dict:
    exp = (date.today() + timedelta(days=dte)).isoformat()
    return {
        "symbol": "AAPL",
        "current_price": spot,
        "expirations": {
            exp: {
                "calls": [
                    {
                        "strike": spot,
                        "bid": call_bid, "ask": call_ask,
                        "delta": call_delta,
                        "openInterest": call_oi, "volume": volume,
                    },
                    {
                        "strike": otm_call_strike,
                        "bid": otm_call_bid, "ask": otm_call_ask,
                        "delta": otm_call_delta,
                        "openInterest": otm_call_oi, "volume": volume,
                    },
                ],
                "puts": [
                    {
                        "strike": spot,
                        "bid": put_bid, "ask": put_ask,
                        "delta": put_delta,
                        "openInterest": put_oi, "volume": volume,
                    },
                    {
                        "strike": otm_put_strike,
                        "bid": otm_put_bid, "ask": otm_put_ask,
                        "delta": otm_put_delta,
                        "openInterest": otm_put_oi, "volume": volume,
                    },
                ],
            }
        },
    }


def _make_pack(
    iv_rank: float = 30.0,
    eda: int | None = 8,
    direction: str = "bullish",
    iv_env: str = "cheap",
    liquidity_score: float = 0.8,
    macro_event: bool = False,
    premium_budget: float = 3000.0,
):
    """Build a minimal A2FeaturePack-like object."""
    pack = MagicMock()
    pack.symbol = "AAPL"
    pack.iv_rank = iv_rank
    pack.earnings_days_away = eda
    pack.a1_direction = direction
    pack.iv_environment = iv_env
    pack.liquidity_score = liquidity_score
    pack.macro_event_flag = macro_event
    pack.premium_budget_usd = premium_budget
    return pack


# ── Builder tests ─────────────────────────────────────────────────────────────

class TestStraddleStrangleBuilder(unittest.TestCase):

    def setUp(self):
        self.chain = _make_chain()
        self.config = {
            "account2": {
                "position_sizing": {"core_single_leg_max_pct": 0.03},
                "liquidity_gates": {
                    "min_open_interest": 200,
                    "min_volume": 20,
                    "max_spread_pct": 0.15,
                    "min_mid_price": 0.05,
                },
                "greeks": {"min_delta": 0.20, "min_dte": 5},
            }
        }

    def test_st01_atm_strike_selection(self):
        """ST-01: ATM strike = strike closest to current_price."""
        from options_builder import _select_straddle_strikes, select_expiry

        exp = select_expiry(self.chain, 5, 28)
        exp_data = self.chain["expirations"][exp]
        result = _select_straddle_strikes(exp_data, spot=200.0, min_delta=0.20)
        self.assertIsNotNone(result)
        self.assertEqual(result["call_strike_price"], 200.0)
        self.assertEqual(result["put_strike_price"], 200.0)

    def test_st02_straddle_both_legs_same_strike(self):
        """ST-02: Straddle builds both legs at the same ATM strike."""
        from options_builder import build_structure
        from schemas import OptionStrategy, StructureLifecycle

        struct, reason = build_structure(
            symbol="AAPL",
            strategy=OptionStrategy.STRADDLE,
            direction="neutral",
            max_cost_usd=5000.0,
            chain=self.chain,
            equity=100_000.0,
            config=self.config["account2"],
        )
        self.assertIsNone(reason, f"Expected success, got: {reason}")
        self.assertIsNotNone(struct)
        self.assertEqual(len(struct.legs), 2)
        call_leg = next(l for l in struct.legs if l.option_type == "call")
        put_leg  = next(l for l in struct.legs if l.option_type == "put")
        self.assertEqual(call_leg.strike, put_leg.strike)
        self.assertEqual(call_leg.side, "buy")
        self.assertEqual(put_leg.side, "buy")
        self.assertEqual(struct.lifecycle, StructureLifecycle.PROPOSED)

    def test_st03_strangle_call_above_put_below(self):
        """ST-03: Strangle call > spot, put < spot, both sides are buys."""
        from options_builder import build_structure
        from schemas import OptionStrategy

        struct, reason = build_structure(
            symbol="AAPL",
            strategy=OptionStrategy.STRANGLE,
            direction="neutral",
            max_cost_usd=5000.0,
            chain=self.chain,
            equity=100_000.0,
            config=self.config["account2"],
        )
        self.assertIsNone(reason, f"Expected success, got: {reason}")
        self.assertIsNotNone(struct)
        self.assertEqual(len(struct.legs), 2)
        call_leg = next(l for l in struct.legs if l.option_type == "call")
        put_leg  = next(l for l in struct.legs if l.option_type == "put")
        spot = 200.0
        self.assertGreater(call_leg.strike, spot)
        self.assertLess(put_leg.strike, spot)
        self.assertEqual(call_leg.side, "buy")
        self.assertEqual(put_leg.side, "buy")

    def test_st04_total_debit_exceeds_budget_rejected(self):
        """ST-04: If total_debit × 100 > max_cost → 0 contracts → rejected."""
        from options_builder import build_structure
        from schemas import OptionStrategy

        # call_mid=3.20, put_mid=3.10 → total=6.30/contract = $630/contract
        # max_cost_usd=100 → 0 contracts
        struct, reason = build_structure(
            symbol="AAPL",
            strategy=OptionStrategy.STRADDLE,
            direction="neutral",
            max_cost_usd=100.0,
            chain=self.chain,
            equity=100_000.0,
            config=self.config["account2"],
        )
        self.assertIsNone(struct)
        self.assertIsNotNone(reason)
        self.assertIn("0 contracts", reason)

    def test_st05_oi_below_minimum_call_leg_rejected(self):
        """ST-05: OI below minimum on call leg → liquidity check fails → rejected."""
        from options_builder import build_structure
        from schemas import OptionStrategy

        chain = _make_chain(call_oi=50)  # below 200 threshold
        # Straddle is not a single leg so liquidity fail → reject
        struct, reason = build_structure(
            symbol="AAPL",
            strategy=OptionStrategy.STRADDLE,
            direction="neutral",
            max_cost_usd=5000.0,
            chain=chain,
            equity=100_000.0,
            config=self.config["account2"],
        )
        self.assertIsNone(struct)
        self.assertIsNotNone(reason)
        self.assertIn("liquidity", reason.lower())

    def test_st06_spread_too_wide_put_leg_rejected(self):
        """ST-06: Bid/ask spread too wide on put leg → rejected."""
        from options_builder import build_structure
        from schemas import OptionStrategy

        # put bid=1.0, ask=4.0 → spread = 3.0/2.5=120% >> 15% threshold
        chain = _make_chain(put_bid=1.0, put_ask=4.0)
        struct, reason = build_structure(
            symbol="AAPL",
            strategy=OptionStrategy.STRADDLE,
            direction="neutral",
            max_cost_usd=5000.0,
            chain=chain,
            equity=100_000.0,
            config=self.config["account2"],
        )
        self.assertIsNone(struct)
        self.assertIsNotNone(reason)

    def test_st07_no_valid_expiry_in_range_returns_none(self):
        """ST-07: No expiry in builder DTE range → build_structure returns reason."""
        from options_builder import build_structure
        from schemas import OptionStrategy

        # Chain only has expiry 3 days out — below dte_min=5 used by build_structure
        chain = _make_chain(dte=3)
        struct, reason = build_structure(
            symbol="AAPL",
            strategy=OptionStrategy.STRADDLE,
            direction="neutral",
            max_cost_usd=5000.0,
            chain=chain,
            equity=100_000.0,
            config=self.config["account2"],
        )
        self.assertIsNone(struct)
        self.assertIsNotNone(reason)

    def test_st08_no_delta_data_falls_back_to_strike_selection(self):
        """ST-08: Chain has no delta fields → falls back to closest-to-spot strike."""
        from options_builder import _select_straddle_strikes, select_expiry

        chain = _make_chain()
        exp = select_expiry(chain, 5, 28)
        exp_data = chain["expirations"][exp]
        # Remove delta from all options
        for opt in exp_data.get("calls", []) + exp_data.get("puts", []):
            opt.pop("delta", None)

        result = _select_straddle_strikes(exp_data, spot=200.0, min_delta=0.30)
        self.assertIsNotNone(result, "Should fall back to strike-based selection")
        self.assertIsNotNone(result["call_strike_price"])
        self.assertIsNotNone(result["put_strike_price"])


# ── Router tests ──────────────────────────────────────────────────────────────

class TestRuleStraddleStrangle(unittest.TestCase):

    def _route(self, iv_rank, eda, direction="bullish", iv_env="cheap",
               liquidity=0.8, macro=False, config=None):
        from bot_options_stage2_structures import _route_strategy
        pack = _make_pack(
            iv_rank=iv_rank, eda=eda, direction=direction,
            iv_env=iv_env, liquidity_score=liquidity, macro_event=macro,
        )
        return _route_strategy(pack, config=config)

    def test_st09_cheap_iv_eda_8_fires_straddle_strangle(self):
        """ST-09: iv_rank=35, eda=8 → RULE_STRADDLE_STRANGLE → ['straddle', 'strangle']."""
        result = self._route(iv_rank=35, eda=8)
        self.assertEqual(result, ["straddle", "strangle"])

    def test_st10_iv_too_high_does_not_fire(self):
        """ST-10: iv_rank=45 (above 40 threshold) → RULE_STRADDLE_STRANGLE does not fire."""
        result = self._route(iv_rank=45, eda=8)
        # RULE_STRADDLE_STRANGLE requires iv_rank < 40; iv=45 falls through to RULE_EARNINGS
        self.assertNotEqual(result, ["straddle", "strangle"],
                            "RULE_STRADDLE_STRANGLE should not fire when iv_rank >= straddle_iv_rank_max")

    def test_st11_eda_within_blackout_does_not_fire(self):
        """ST-11: eda=3 (within earnings_dte_blackout=2 when eda<=2; but eda=3 > 2)."""
        # Blackout is eda <= 2, so eda=3 is NOT in blackout.
        # But straddle_dte_min=6, so eda=3 < 6 → below straddle window → no fire.
        result = self._route(iv_rank=35, eda=3)
        self.assertNotEqual(result, ["straddle", "strangle"])

    def test_st11b_eda_at_blackout_boundary_blocked(self):
        """ST-11b: eda=2 at blackout boundary → RULE1 smart router fires.
        neutral direction → blocked; directional → routed to debit/credit spread."""
        # Neutral direction → RULE1 blocks (no directional thesis)
        result_neutral = self._route(iv_rank=35, eda=2, direction="neutral")
        self.assertEqual(result_neutral, [])
        # Bullish direction + cheap iv → routed to debit_call_spread
        result_bullish = self._route(iv_rank=35, eda=2, direction="bullish")
        self.assertIn("debit_call_spread", result_bullish)

    def test_st12_eda_outside_window_does_not_fire(self):
        """ST-12: eda=16 (outside straddle_dte_max=14) → rule does not fire."""
        result = self._route(iv_rank=35, eda=16)
        self.assertNotEqual(result, ["straddle", "strangle"])

    def test_st13_eda_none_does_not_fire(self):
        """ST-13: eda=None → no earnings date → rule does not fire."""
        result = self._route(iv_rank=35, eda=None)
        self.assertNotEqual(result, ["straddle", "strangle"])

    def test_st14_eda_6_boundary_fires(self):
        """ST-14: eda=6 (straddle_dte_min boundary) → fires."""
        result = self._route(iv_rank=35, eda=6)
        self.assertEqual(result, ["straddle", "strangle"])

    def test_st15_eda_14_boundary_fires(self):
        """ST-15: eda=14 (straddle_dte_max boundary) → fires."""
        result = self._route(iv_rank=35, eda=14)
        self.assertEqual(result, ["straddle", "strangle"])

    def test_st16_eda_15_outside_boundary_does_not_fire(self):
        """ST-16: eda=15 (just outside straddle_dte_max=14) → rule does not fire."""
        result = self._route(iv_rank=35, eda=15)
        self.assertNotEqual(result, ["straddle", "strangle"])

    def test_rule_uses_config_overrides(self):
        """Custom config thresholds are respected (straddle_iv_rank_max=50)."""
        config = {
            "a2_router": {
                "straddle_iv_rank_max": 50,
                "straddle_dte_min": 6,
                "straddle_dte_max": 14,
                "earnings_dte_blackout": 2,
            }
        }
        result = self._route(iv_rank=45, eda=8, config=config)
        self.assertEqual(result, ["straddle", "strangle"])


# ── Executor tests ────────────────────────────────────────────────────────────

def _make_straddle_structure(call_bid=3.0, call_ask=3.40, put_bid=2.90, put_ask=3.30):
    from schemas import (
        OptionsLeg,
        OptionsStructure,
        OptionStrategy,
        StructureLifecycle,
        Tier,
    )
    call_leg = OptionsLeg(
        occ_symbol="AAPL260515C00200000",
        underlying="AAPL",
        side="buy",
        qty=1,
        option_type="call",
        strike=200.0,
        expiration="2026-05-15",
        bid=call_bid,
        ask=call_ask,
        mid=(call_bid + call_ask) / 2,
    )
    put_leg = OptionsLeg(
        occ_symbol="AAPL260515P00200000",
        underlying="AAPL",
        side="buy",
        qty=1,
        option_type="put",
        strike=200.0,
        expiration="2026-05-15",
        bid=put_bid,
        ask=put_ask,
        mid=(put_bid + put_ask) / 2,
    )
    return OptionsStructure(
        structure_id="test-straddle-001",
        underlying="AAPL",
        strategy=OptionStrategy.STRADDLE,
        lifecycle=StructureLifecycle.PROPOSED,
        legs=[call_leg, put_leg],
        contracts=2,
        max_cost_usd=1300.0,
        opened_at="2026-04-29T00:00:00+00:00",
        catalyst="earnings",
        tier=Tier.CORE,
        order_ids=[],
        expiration="2026-05-15",
    )


def _make_strangle_structure():
    from schemas import (
        OptionsLeg,
        OptionsStructure,
        OptionStrategy,
        StructureLifecycle,
        Tier,
    )
    call_leg = OptionsLeg(
        occ_symbol="AAPL260515C00210000",
        underlying="AAPL",
        side="buy",
        qty=1,
        option_type="call",
        strike=210.0,
        expiration="2026-05-15",
        bid=1.50, ask=1.80, mid=1.65,
    )
    put_leg = OptionsLeg(
        occ_symbol="AAPL260515P00190000",
        underlying="AAPL",
        side="buy",
        qty=1,
        option_type="put",
        strike=190.0,
        expiration="2026-05-15",
        bid=1.40, ask=1.70, mid=1.55,
    )
    return OptionsStructure(
        structure_id="test-strangle-001",
        underlying="AAPL",
        strategy=OptionStrategy.STRANGLE,
        lifecycle=StructureLifecycle.PROPOSED,
        legs=[call_leg, put_leg],
        contracts=2,
        max_cost_usd=640.0,
        opened_at="2026-04-29T00:00:00+00:00",
        catalyst="earnings",
        tier=Tier.CORE,
        order_ids=[],
        expiration="2026-05-15",
    )


class TestStraddleStrangleExecutor(unittest.TestCase):

    def test_st17_straddle_mleg_limit_price_is_positive(self):
        """ST-17: Straddle mleg has positive limit_price (total debit, not credit)."""
        from schemas import StructureLifecycle

        with patch.dict(sys.modules, _alpaca_modules()):
            from options_executor import _submit_spread_mleg

            struct = _make_straddle_structure()
            mock_client = MagicMock()
            mock_order = MagicMock()
            mock_order.id = "order-straddle-001"
            mock_client.submit_order.return_value = mock_order

            result = _submit_spread_mleg(struct, mock_client)
            self.assertEqual(result.lifecycle, StructureLifecycle.SUBMITTED)

            call_args = mock_client.submit_order.call_args[0][0]
            self.assertGreater(call_args.limit_price, 0,
                               "Straddle limit_price must be positive (total debit)")

    def test_st18_compute_net_mid_buy_buy_returns_positive(self):
        """ST-18: _compute_net_mid() for buy-buy structure (straddle) returns positive."""
        from options_executor import _compute_net_mid

        struct = _make_straddle_structure(call_bid=3.0, call_ask=3.40, put_bid=2.90, put_ask=3.30)
        net = _compute_net_mid(struct)
        # call_mid=3.20, put_mid=3.10 → total=6.30
        self.assertIsNotNone(net)
        self.assertGreater(net, 0)
        self.assertAlmostEqual(net, 6.30, places=2)

    def test_st19_strangle_mleg_is_single_atomic_order_with_2_legs(self):
        """ST-19: Strangle submitted as single mleg order with exactly 2 legs."""
        from schemas import StructureLifecycle

        with patch.dict(sys.modules, _alpaca_modules()):
            from options_executor import _submit_spread_mleg

            struct = _make_strangle_structure()
            mock_client = MagicMock()
            mock_order = MagicMock()
            mock_order.id = "order-strangle-001"
            mock_client.submit_order.return_value = mock_order

            result = _submit_spread_mleg(struct, mock_client)
            self.assertEqual(result.lifecycle, StructureLifecycle.SUBMITTED)
            mock_client.submit_order.assert_called_once()
            req = mock_client.submit_order.call_args[0][0]
            self.assertEqual(len(req.legs), 2)

    def test_st20_occ_symbols_correctly_formatted_in_mleg(self):
        """ST-20: Both OCC symbols in mleg request match expected Alpaca format."""
        with patch.dict(sys.modules, _alpaca_modules()):
            from options_executor import _submit_spread_mleg

            struct = _make_straddle_structure()
            mock_client = MagicMock()
            mock_order = MagicMock()
            mock_order.id = "order-occ-001"
            mock_client.submit_order.return_value = mock_order

            _submit_spread_mleg(struct, mock_client)
            req = mock_client.submit_order.call_args[0][0]
            occ_symbols = [leg.symbol for leg in req.legs]
            self.assertIn("AAPL260515C00200000", occ_symbols)
            self.assertIn("AAPL260515P00200000", occ_symbols)

    def test_strangle_in_phase1_strategies(self):
        """STRANGLE is in _PHASE1_STRATEGIES so submit_structure routes correctly."""
        from options_executor import _PHASE1_STRATEGIES
        from schemas import OptionStrategy

        self.assertIn(OptionStrategy.STRANGLE, _PHASE1_STRATEGIES)
        self.assertIn(OptionStrategy.STRADDLE, _PHASE1_STRATEGIES)

    def test_strangle_not_rejected_as_unsupported(self):
        """submit_structure does not REJECT straddle/strangle as unsupported."""
        from schemas import StructureLifecycle

        with patch.dict(sys.modules, _alpaca_modules()):
            from options_executor import submit_structure

            struct = _make_straddle_structure()
            mock_client = MagicMock()
            mock_order = MagicMock()
            mock_order.id = "order-submit-001"
            mock_client.submit_order.return_value = mock_order

            result = submit_structure(struct, mock_client, config={})
            self.assertNotEqual(result.lifecycle, StructureLifecycle.REJECTED,
                                "Straddle should not be rejected as unsupported")


if __name__ == "__main__":
    unittest.main()
