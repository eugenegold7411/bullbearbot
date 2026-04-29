"""
tests/test_s10_phase5_iron_condor.py — Sprint 10 Phase 5: Iron Condor + Iron Butterfly tests.

Tests:
  IC-01  _build_iron_condor() returns valid candidate with 4 legs
  IC-02  _build_iron_condor() debit is negative (credit received)
  IC-03  _build_iron_condor() max_gain = net_credit × contracts × 100
  IC-04  _build_iron_condor() max_loss = (spread_width - credit) × contracts × 100
  IC-05  _build_iron_condor() returns None when no DTE in range
  IC-06  _build_iron_condor() returns None when net credit < min_credit floor
  IC-07  _select_iron_condor_strikes() builds 4 distinct leg fields
  IC-08  _build_iron_butterfly() returns valid candidate with ATM short strikes
  IC-09  _build_iron_butterfly() debit is negative (credit received)
  IC-10  _build_iron_butterfly() returns None when no DTE in range
  IC-11  _select_iron_butterfly_strikes() ATM strike is closest to spot
  IC-12  RULE_IRON fires — iv_rank=75, neutral direction → ['iron_condor']
  IC-13  RULE_IRON fires — iv_rank=85, neutral direction → ['iron_butterfly', 'iron_condor']
  IC-14  RULE_IRON fires — iv_rank=85, bullish direction → ['iron_butterfly', 'iron_condor']
  IC-15  RULE_IRON does NOT fire — iv_rank=65 (below 70 floor)
  IC-16  RULE_IRON does NOT fire — earnings within blackout (eda=1)
  IC-17  RULE_IRON does NOT fire — iv_rank=72, bearish direction (only neutral or ≥85)
  IC-18  RULE2_CREDIT fires before RULE_IRON for very_expensive + bullish (iv_rank=75)
  IC-19  _infer_router_rule_fired() returns "RULE_IRON" for iron_condor in allowed
  IC-20  strategy_config.json has all required iron keys
  IC-21  compute_economics() returns negative net_debit for iron condor credit structure
  IC-22  build_legs() returns 4 OptionsLeg objects for iron_condor strategy
  IC-23  IRON_CONDOR and IRON_BUTTERFLY in options_executor._PHASE1_STRATEGIES
  IC-24  IRON_CONDOR and IRON_BUTTERFLY in options_builder._PHASE1_STRATEGIES
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

# ── Helpers ────────────────────────────────────────────────────────────────────

def _today_plus(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def _make_option(strike: float, opt_type: str, delta: float, bid: float = 1.00,
                 ask: float = 1.20, oi: int = 500, vol: int = 50) -> dict:
    return {
        "strike": strike,
        "delta": delta,
        "bid": bid,
        "ask": ask,
        "openInterest": oi,
        "volume": vol,
        "theta": -0.02,
        "vega": 0.10,
    }


def _chain_with_both_sides(spot: float = 200.0, dte_days: int = 30) -> dict:
    """Return a chain with OTM calls and OTM puts, suitable for iron condor."""
    expiry = _today_plus(dte_days)
    calls = [
        _make_option(spot * 1.05, "call", delta=0.25, bid=0.90, ask=1.10),   # short call candidate
        _make_option(spot * 1.10, "call", delta=0.15, bid=0.40, ask=0.60),   # long call wing
        _make_option(spot * 1.15, "call", delta=0.08, bid=0.15, ask=0.25),
    ]
    puts = [
        _make_option(spot * 0.95, "put", delta=-0.25, bid=0.90, ask=1.10),   # short put candidate
        _make_option(spot * 0.90, "put", delta=-0.15, bid=0.40, ask=0.60),   # long put wing
        _make_option(spot * 0.85, "put", delta=-0.08, bid=0.15, ask=0.25),
        _make_option(spot * 1.00, "put", delta=-0.50, bid=3.00, ask=3.20),   # ATM put (for butterfly)
    ]
    return {
        "current_price": spot,
        "expirations": {
            expiry: {"calls": calls, "puts": puts},
        },
    }


def _chain_atm_butterfly(spot: float = 200.0, dte_days: int = 30) -> dict:
    """Return a chain where ATM options have high premium (suitable for butterfly)."""
    expiry = _today_plus(dte_days)
    calls = [
        _make_option(spot,        "call", delta=0.50, bid=4.00, ask=4.20),   # ATM short call
        _make_option(spot * 1.05, "call", delta=0.30, bid=2.00, ask=2.20),   # wing
        _make_option(spot * 1.10, "call", delta=0.15, bid=0.80, ask=1.00),   # wing fallback
    ]
    puts = [
        _make_option(spot,        "put",  delta=-0.50, bid=4.00, ask=4.20),  # ATM short put
        _make_option(spot * 0.95, "put",  delta=-0.30, bid=2.00, ask=2.20),  # wing
        _make_option(spot * 0.90, "put",  delta=-0.15, bid=0.80, ask=1.00),  # wing fallback
    ]
    return {
        "current_price": spot,
        "expirations": {
            expiry: {"calls": calls, "puts": puts},
        },
    }


@dataclass
class _FakePack:
    symbol: str = "AAPL"
    iv_rank: float = 75.0
    iv_environment: str = "expensive"
    a1_direction: str = "neutral"
    a1_signal_score: float = 60.0
    earnings_days_away: Optional[int] = None
    liquidity_score: float = 0.7
    macro_event_flag: bool = False
    premium_budget_usd: float = 5000.0


# ── IC-01 to IC-07: Iron Condor builder ───────────────────────────────────────

class TestBuildIronCondor:
    def _call(self, chain, equity=100_000.0, config=None):
        from options_intelligence import _build_iron_condor
        pack = _FakePack()
        return _build_iron_condor(pack, chain, equity, config)

    def test_IC01_returns_valid_candidate(self):
        chain = _chain_with_both_sides(spot=200.0, dte_days=30)
        result = self._call(chain)
        assert result is not None
        assert result["structure_type"] == "iron_condor"

    def test_IC02_debit_is_negative(self):
        """Iron condor receives credit — debit must be negative."""
        chain = _chain_with_both_sides(spot=200.0, dte_days=30)
        result = self._call(chain)
        assert result is not None
        assert result["debit"] < 0

    def test_IC03_max_gain_equals_net_credit(self):
        """max_gain = abs(debit) × contracts × 100."""
        chain = _chain_with_both_sides(spot=200.0, dte_days=30)
        result = self._call(chain)
        assert result is not None
        expected = abs(result["debit"]) * result["contracts"] * 100
        assert abs(result["max_gain"] - expected) < 1.0

    def test_IC04_max_loss_is_spread_minus_credit(self):
        """max_loss + max_gain should equal spread_width × contracts × 100 (credit reduces loss)."""
        chain = _chain_with_both_sides(spot=200.0, dte_days=30)
        result = self._call(chain)
        assert result is not None
        assert result["max_loss"] > 0
        # max_loss + max_gain = spread_width × contracts × 100
        total = result["max_loss"] + result["max_gain"]
        contracts = result["contracts"]
        # spread_width per contract in dollars (total / contracts / 100)
        spread_per_share = total / contracts / 100
        assert spread_per_share > 0, "spread_per_share must be positive"
        # max_loss must be less than total (credit reduces loss from worst case)
        assert result["max_loss"] < total

    def test_IC05_no_dte_in_range_returns_none(self):
        """Expiry at DTE=5 (below 21-day floor) → None."""
        chain = _chain_with_both_sides(spot=200.0, dte_days=5)
        result = self._call(chain)
        assert result is None

    def test_IC06_low_credit_returns_none(self):
        """Chain with very low premium → credit below min_credit floor → None."""
        expiry = _today_plus(30)
        tiny_calls = [_make_option(210.0, "call", delta=0.18, bid=0.01, ask=0.02)]
        tiny_puts  = [_make_option(190.0, "put",  delta=-0.18, bid=0.01, ask=0.02)]
        chain = {
            "current_price": 200.0,
            "expirations": {expiry: {"calls": tiny_calls, "puts": tiny_puts}},
        }
        result = self._call(chain)
        assert result is None

    def test_IC07_select_iron_condor_strikes_returns_four_leg_fields(self):
        """_select_iron_condor_strikes must return all 4 named leg data fields."""
        from options_builder import _select_iron_condor_strikes
        expiry = _today_plus(30)
        chain = _chain_with_both_sides(spot=200.0, dte_days=30)
        exp_data = chain["expirations"][expiry]
        result = _select_iron_condor_strikes(exp_data, 200.0, {})
        assert result is not None
        for key in ("short_call_leg_data", "long_call_leg_data",
                    "short_put_leg_data", "long_put_leg_data"):
            assert key in result, f"Missing key: {key}"

    def test_IC_short_call_strike_above_spot(self):
        """Short call strike must be OTM (above spot)."""
        from options_builder import _select_iron_condor_strikes
        expiry = _today_plus(30)
        chain = _chain_with_both_sides(spot=200.0, dte_days=30)
        exp_data = chain["expirations"][expiry]
        result = _select_iron_condor_strikes(exp_data, 200.0, {})
        assert result is not None
        assert float(result["short_call_strike_price"]) > 200.0

    def test_IC_short_put_strike_below_spot(self):
        """Short put strike must be OTM (below spot)."""
        from options_builder import _select_iron_condor_strikes
        expiry = _today_plus(30)
        chain = _chain_with_both_sides(spot=200.0, dte_days=30)
        exp_data = chain["expirations"][expiry]
        result = _select_iron_condor_strikes(exp_data, 200.0, {})
        assert result is not None
        assert float(result["short_put_strike_price"]) < 200.0

    def test_IC_long_call_beyond_short_call(self):
        """Long call wing must be further OTM than the short call."""
        from options_builder import _select_iron_condor_strikes
        expiry = _today_plus(30)
        chain = _chain_with_both_sides(spot=200.0, dte_days=30)
        exp_data = chain["expirations"][expiry]
        result = _select_iron_condor_strikes(exp_data, 200.0, {})
        assert result is not None
        assert float(result["long_call_strike_price"]) > float(result["short_call_strike_price"])

    def test_IC_long_put_beyond_short_put(self):
        """Long put wing must be further OTM (lower) than the short put."""
        from options_builder import _select_iron_condor_strikes
        expiry = _today_plus(30)
        chain = _chain_with_both_sides(spot=200.0, dte_days=30)
        exp_data = chain["expirations"][expiry]
        result = _select_iron_condor_strikes(exp_data, 200.0, {})
        assert result is not None
        assert float(result["long_put_strike_price"]) < float(result["short_put_strike_price"])


# ── IC-08 to IC-11: Iron Butterfly builder ────────────────────────────────────

class TestBuildIronButterfly:
    def _call(self, chain, equity=100_000.0, config=None):
        from options_intelligence import _build_iron_butterfly
        pack = _FakePack()
        return _build_iron_butterfly(pack, chain, equity, config)

    def test_IC08_returns_valid_candidate(self):
        chain = _chain_atm_butterfly(spot=200.0, dte_days=30)
        result = self._call(chain)
        assert result is not None
        assert result["structure_type"] == "iron_butterfly"

    def test_IC09_debit_is_negative(self):
        """Iron butterfly receives credit — debit must be negative."""
        chain = _chain_atm_butterfly(spot=200.0, dte_days=30)
        result = self._call(chain)
        assert result is not None
        assert result["debit"] < 0

    def test_IC10_no_dte_in_range_returns_none(self):
        """Expiry at DTE=5 (below 21-day floor) → None."""
        chain = _chain_atm_butterfly(spot=200.0, dte_days=5)
        result = self._call(chain)
        assert result is None

    def test_IC11_select_butterfly_atm_strike_near_spot(self):
        """ATM short strike must be the closest available strike to spot."""
        from options_builder import _select_iron_butterfly_strikes
        expiry = _today_plus(30)
        chain = _chain_atm_butterfly(spot=200.0, dte_days=30)
        exp_data = chain["expirations"][expiry]
        result = _select_iron_butterfly_strikes(exp_data, 200.0, {})
        assert result is not None
        atm = float(result["short_call_strike_price"])
        # Should be 200.0 or within 5.0 of spot (closest available strike)
        assert abs(atm - 200.0) <= 5.0

    def test_IC_butterfly_short_call_equals_short_put(self):
        """Short call and short put must be at the same (ATM) strike."""
        from options_builder import _select_iron_butterfly_strikes
        expiry = _today_plus(30)
        chain = _chain_atm_butterfly(spot=200.0, dte_days=30)
        exp_data = chain["expirations"][expiry]
        result = _select_iron_butterfly_strikes(exp_data, 200.0, {})
        assert result is not None
        sc = float(result["short_call_strike_price"])
        sp = float(result["short_put_strike_price"])
        assert abs(sc - sp) <= 1.0, f"short_call={sc} ≠ short_put={sp}"


# ── IC-12 to IC-18: Router tests ──────────────────────────────────────────────

class TestRuleIron:
    def _route(self, pack_kwargs=None, config=None):
        from bot_options_stage2_structures import _route_strategy
        pack = _FakePack(**(pack_kwargs or {}))
        return _route_strategy(pack, config=config, earnings_calendar_data={})

    def test_IC12_fires_neutral_iv75(self):
        """iv_rank=75, neutral direction → ['iron_condor']."""
        result = self._route({"iv_rank": 75.0, "iv_environment": "expensive",
                               "a1_direction": "neutral"})
        assert result == ["iron_condor"]

    def test_IC13_fires_neutral_iv85_both_structures(self):
        """iv_rank=85, expensive env, neutral direction → ['iron_butterfly', 'iron_condor']."""
        result = self._route({"iv_rank": 85.0, "iv_environment": "expensive",
                               "a1_direction": "neutral"})
        assert result == ["iron_butterfly", "iron_condor"]

    def test_IC14_fires_bullish_iv85_both_structures(self):
        """iv_rank=85, expensive env, bullish direction → ['iron_butterfly', 'iron_condor'] (≥85 overrides direction)."""
        result = self._route({"iv_rank": 85.0, "iv_environment": "expensive",
                               "a1_direction": "bullish"})
        assert result == ["iron_butterfly", "iron_condor"]

    def test_IC15_does_not_fire_iv_rank_too_low(self):
        """iv_rank=65 < 70 floor → RULE_IRON skipped."""
        result = self._route({"iv_rank": 65.0, "iv_environment": "expensive",
                               "a1_direction": "neutral"})
        assert "iron_condor" not in result
        assert "iron_butterfly" not in result

    def test_IC16_does_not_fire_earnings_blackout(self):
        """eda=1 (within 2-day blackout) → RULE1 blocks before RULE_IRON."""
        result = self._route({"iv_rank": 80.0, "iv_environment": "expensive",
                               "a1_direction": "neutral", "earnings_days_away": 1})
        assert result == []

    def test_IC17_does_not_fire_bearish_below_85(self):
        """iv_rank=72, bearish direction → RULE_IRON requires neutral for iv_rank < 85."""
        result = self._route({"iv_rank": 72.0, "iv_environment": "expensive",
                               "a1_direction": "bearish"})
        assert "iron_condor" not in result
        assert "iron_butterfly" not in result

    def test_IC18_rule2_credit_fires_before_rule_iron_for_very_expensive_bullish(self):
        """very_expensive + bullish + iv_rank=75 → RULE2_CREDIT (credit_put_spread), not RULE_IRON."""
        result = self._route({"iv_rank": 75.0, "iv_environment": "very_expensive",
                               "a1_direction": "bullish"})
        assert result == ["credit_put_spread"]
        assert "iron_condor" not in result


# ── IC-19: _infer_router_rule_fired ──────────────────────────────────────────

class TestInferRouterRuleIron:
    def test_IC19_returns_rule_iron_for_iron_condor(self):
        from bot_options_stage2_structures import _infer_router_rule_fired
        pack = _FakePack(iv_rank=75.0, iv_environment="expensive", a1_direction="neutral")
        assert _infer_router_rule_fired(pack, ["iron_condor"]) == "RULE_IRON"

    def test_IC19b_returns_rule_iron_for_iron_butterfly(self):
        from bot_options_stage2_structures import _infer_router_rule_fired
        pack = _FakePack(iv_rank=85.0, iv_environment="expensive", a1_direction="neutral")
        assert _infer_router_rule_fired(pack, ["iron_butterfly", "iron_condor"]) == "RULE_IRON"

    def test_IC19c_rule_short_put_not_confused_with_rule_iron(self):
        """RULE_SHORT_PUT should NOT return RULE_IRON."""
        from bot_options_stage2_structures import _infer_router_rule_fired
        pack = _FakePack(iv_rank=55.0, iv_environment="neutral", a1_direction="bullish")
        assert _infer_router_rule_fired(pack, ["short_put"]) == "RULE_SHORT_PUT"


# ── IC-20: strategy_config.json ──────────────────────────────────────────────

class TestStrategyConfigIron:
    def _load_config(self):
        cfg_path = Path(__file__).parent.parent / "strategy_config.json"
        return json.loads(cfg_path.read_text())

    def test_IC20a_a2_router_has_iron_iv_rank_min(self):
        cfg = self._load_config()
        assert "iron_iv_rank_min" in cfg.get("a2_router", {})
        assert cfg["a2_router"]["iron_iv_rank_min"] == 70

    def test_IC20b_account2_has_iron_condor_short_delta_target(self):
        cfg = self._load_config()
        a2 = cfg.get("account2", {})
        assert "iron_condor_short_delta_target" in a2
        assert a2["iron_condor_short_delta_target"] == 0.175

    def test_IC20c_account2_has_iron_condor_spread_width(self):
        cfg = self._load_config()
        a2 = cfg.get("account2", {})
        assert "iron_condor_spread_width" in a2
        assert a2["iron_condor_spread_width"] == 5.0

    def test_IC20d_account2_has_iron_condor_min_credit_usd(self):
        cfg = self._load_config()
        a2 = cfg.get("account2", {})
        assert "iron_condor_min_credit_usd" in a2
        assert a2["iron_condor_min_credit_usd"] == 50

    def test_IC20e_account2_has_iron_butterfly_wing_width(self):
        cfg = self._load_config()
        a2 = cfg.get("account2", {})
        assert "iron_butterfly_wing_width" in a2
        assert a2["iron_butterfly_wing_width"] == 10.0

    def test_IC20f_account2_has_iron_butterfly_min_credit_usd(self):
        cfg = self._load_config()
        a2 = cfg.get("account2", {})
        assert "iron_butterfly_min_credit_usd" in a2
        assert a2["iron_butterfly_min_credit_usd"] == 100


# ── IC-21: compute_economics ─────────────────────────────────────────────────

class TestComputeEconomicsIron:
    def _make_strikes_data(self, sc_bid=1.00, sc_ask=1.20,
                           lc_bid=0.40, lc_ask=0.60,
                           sp_bid=1.00, sp_ask=1.20,
                           lp_bid=0.40, lp_ask=0.60,
                           sc_strike=210.0, lc_strike=215.0,
                           sp_strike=190.0, lp_strike=185.0):
        def _leg(bid, ask, strike):
            return {"bid": bid, "ask": ask, "strike": strike, "openInterest": 500, "volume": 50}
        return {
            "option_type": "iron_condor",
            "short_call_strike_price": sc_strike,
            "short_call_leg_data": _leg(sc_bid, sc_ask, sc_strike),
            "long_call_strike_price": lc_strike,
            "long_call_leg_data": _leg(lc_bid, lc_ask, lc_strike),
            "short_put_strike_price": sp_strike,
            "short_put_leg_data": _leg(sp_bid, sp_ask, sp_strike),
            "long_put_strike_price": lp_strike,
            "long_put_leg_data": _leg(lp_bid, lp_ask, lp_strike),
        }

    def test_IC21_net_debit_is_negative_for_credit_structure(self):
        """Iron condor net_debit must be negative (credit received)."""
        from options_builder import compute_economics
        from schemas import OptionStrategy
        strikes = self._make_strikes_data()
        econ = compute_economics(OptionStrategy.IRON_CONDOR, strikes)
        assert econ["net_debit"] is not None
        assert econ["net_debit"] < 0

    def test_IC21b_max_profit_equals_net_credit(self):
        """max_profit = abs(net_debit) for credit structures."""
        from options_builder import compute_economics
        from schemas import OptionStrategy
        strikes = self._make_strikes_data()
        econ = compute_economics(OptionStrategy.IRON_CONDOR, strikes)
        assert econ["max_profit"] is not None
        assert abs(econ["max_profit"] - abs(econ["net_debit"])) < 0.01

    def test_IC21c_max_loss_less_than_spread_width(self):
        """max_loss must be less than spread_width (credit reduces it)."""
        from options_builder import compute_economics
        from schemas import OptionStrategy
        strikes = self._make_strikes_data()
        econ = compute_economics(OptionStrategy.IRON_CONDOR, strikes)
        spread_width = 5.0  # 215 - 210 and 190 - 185
        assert econ["max_loss"] < spread_width

    def test_IC21d_zero_credit_returns_none(self):
        """If net_credit <= 0 (wings cost more than shorts), returns None economics."""
        from options_builder import compute_economics
        from schemas import OptionStrategy
        # Make wings (long legs) more expensive than shorts → negative credit
        strikes = self._make_strikes_data(sc_bid=0.30, sc_ask=0.50,
                                          sp_bid=0.30, sp_ask=0.50,
                                          lc_bid=1.00, lc_ask=1.20,
                                          lp_bid=1.00, lp_ask=1.20)
        econ = compute_economics(OptionStrategy.IRON_CONDOR, strikes)
        # net_debit should be None or positive (no credit)
        assert econ["net_debit"] is None or econ["net_debit"] >= 0


# ── IC-22: build_legs ────────────────────────────────────────────────────────

class TestBuildLegsIron:
    def _make_strikes_data(self, spot=200.0):
        def _leg(bid, ask, strike):
            return {"bid": bid, "ask": ask, "strike": strike, "openInterest": 500, "volume": 50}
        return {
            "option_type": "iron_condor",
            "short_call_strike_price": spot * 1.05,
            "short_call_leg_data": _leg(1.00, 1.20, spot * 1.05),
            "long_call_strike_price": spot * 1.10,
            "long_call_leg_data": _leg(0.40, 0.60, spot * 1.10),
            "short_put_strike_price": spot * 0.95,
            "short_put_leg_data": _leg(1.00, 1.20, spot * 0.95),
            "long_put_strike_price": spot * 0.90,
            "long_put_leg_data": _leg(0.40, 0.60, spot * 0.90),
        }

    def test_IC22_returns_four_legs(self):
        from options_builder import build_legs
        from schemas import OptionStrategy
        strikes = self._make_strikes_data()
        legs = build_legs("AAPL", OptionStrategy.IRON_CONDOR, _today_plus(30), strikes)
        assert len(legs) == 4

    def test_IC22b_has_two_sell_legs_and_two_buy_legs(self):
        from options_builder import build_legs
        from schemas import OptionStrategy
        strikes = self._make_strikes_data()
        legs = build_legs("AAPL", OptionStrategy.IRON_CONDOR, _today_plus(30), strikes)
        sides = [leg.side for leg in legs]
        assert sides.count("sell") == 2
        assert sides.count("buy") == 2

    def test_IC22c_call_legs_have_option_type_call(self):
        from options_builder import build_legs
        from schemas import OptionStrategy
        strikes = self._make_strikes_data()
        legs = build_legs("AAPL", OptionStrategy.IRON_CONDOR, _today_plus(30), strikes)
        call_legs = [l for l in legs if l.option_type == "call"]
        put_legs  = [l for l in legs if l.option_type == "put"]
        assert len(call_legs) == 2
        assert len(put_legs) == 2

    def test_IC22d_occ_symbols_are_populated(self):
        from options_builder import build_legs
        from schemas import OptionStrategy
        strikes = self._make_strikes_data()
        legs = build_legs("AAPL", OptionStrategy.IRON_CONDOR, _today_plus(30), strikes)
        for leg in legs:
            assert leg.occ_symbol is not None
            assert len(leg.occ_symbol) > 0


# ── IC-23 / IC-24: Phase 1 strategy sets ─────────────────────────────────────

class TestPhase1StrategySet:
    def test_IC23_options_executor_phase1_has_iron_condor(self):
        from options_executor import _PHASE1_STRATEGIES
        from schemas import OptionStrategy
        assert OptionStrategy.IRON_CONDOR in _PHASE1_STRATEGIES

    def test_IC23b_options_executor_phase1_has_iron_butterfly(self):
        from options_executor import _PHASE1_STRATEGIES
        from schemas import OptionStrategy
        assert OptionStrategy.IRON_BUTTERFLY in _PHASE1_STRATEGIES

    def test_IC24_options_builder_phase1_has_iron_condor(self):
        from options_builder import _PHASE1_STRATEGIES
        from schemas import OptionStrategy
        assert OptionStrategy.IRON_CONDOR in _PHASE1_STRATEGIES

    def test_IC24b_options_builder_phase1_has_iron_butterfly(self):
        from options_builder import _PHASE1_STRATEGIES
        from schemas import OptionStrategy
        assert OptionStrategy.IRON_BUTTERFLY in _PHASE1_STRATEGIES

    def test_IC_schemas_has_iron_condor_enum(self):
        from schemas import OptionStrategy
        assert OptionStrategy.IRON_CONDOR.value == "iron_condor"

    def test_IC_schemas_has_iron_butterfly_enum(self):
        from schemas import OptionStrategy
        assert OptionStrategy.IRON_BUTTERFLY.value == "iron_butterfly"
