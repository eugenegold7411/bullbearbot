"""
tests/test_s10_phase3_short_put.py — Sprint 10 Phase 3: Short Put tests.

Tests:
  SP-01  _build_short_put() returns valid candidate with OTM put at delta ~0.275
  SP-02  _build_short_put() returns None when no OTM puts available
  SP-03  _build_short_put() returns None when premium < min_premium floor
  SP-04  _build_short_put() returns None when no expiry in DTE range
  SP-05  _build_short_put() DTE range is 21–45 (not 5–21)
  SP-06  RULE_SHORT_PUT fires — iv_rank=55, neutral env, bullish direction
  SP-07  RULE_SHORT_PUT fires — direction=neutral with iv_rank=60
  SP-08  RULE_SHORT_PUT does NOT fire — iv_rank too low (< 50)
  SP-09  RULE_SHORT_PUT does NOT fire — direction=bearish
  SP-10  RULE_SHORT_PUT does NOT fire — iv_env=cheap (even if iv_rank=50)
  SP-11  RULE_SHORT_PUT does NOT fire — earnings within blackout (eda=1)
  SP-12  _submit_single_leg() uses OrderSide.SELL when leg.side == "sell"
  SP-13  _submit_single_leg() uses OrderSide.BUY when leg.side == "buy"
  SP-14  _infer_router_rule_fired() returns "RULE_SHORT_PUT" for short_put allowed
  SP-15  strategy_config.json has all required short_put keys
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

# ── Helpers ────────────────────────────────────────────────────────────────────

def _today_plus(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def _make_put(strike: float, delta: float = -0.275, bid: float = 1.20,
               ask: float = 1.40, oi: int = 500, vol: int = 50) -> dict:
    return {
        "strike": strike,
        "delta": delta,
        "bid": bid,
        "ask": ask,
        "openInterest": oi,
        "volume": vol,
        "theta": -0.03,
        "vega": 0.12,
    }


def _chain_with_puts(spot: float = 200.0, dte_days: int = 30,
                      puts: list | None = None) -> dict:
    """Return a minimal chain dict with one expiry."""
    expiry = _today_plus(dte_days)
    if puts is None:
        puts = [
            _make_put(spot * 0.97, delta=-0.275),
            _make_put(spot * 0.95, delta=-0.20),
            _make_put(spot * 0.90, delta=-0.15),
            _make_put(spot * 1.00, delta=-0.50),   # ATM (not OTM)
        ]
    return {
        "current_price": spot,
        "expirations": {
            expiry: {"puts": puts, "calls": []},
        },
    }


@dataclass
class _FakePack:
    """Minimal A2FeaturePack substitute for router tests."""
    symbol: str = "AAPL"
    iv_rank: float = 55.0
    iv_environment: str = "neutral"
    a1_direction: str = "bullish"
    a1_signal_score: float = 60.0
    earnings_days_away: Optional[int] = None
    liquidity_score: float = 0.7
    macro_event_flag: bool = False
    premium_budget_usd: float = 5000.0


# ── SP-01: _build_short_put returns valid candidate ────────────────────────────

class TestBuildShortPut:
    def _call(self, chain, equity=100_000.0, config=None):
        from options_intelligence import _build_short_put
        pack = _FakePack()
        return _build_short_put(pack, chain, equity, config)

    def test_SP01_returns_valid_candidate(self):
        chain = _chain_with_puts(spot=200.0, dte_days=30)
        result = self._call(chain)
        assert result is not None
        assert result["structure_type"] == "short_put"
        assert result["leg_side"] == "sell"
        assert result["debit"] < 0          # credit = negative debit
        assert result["max_gain"] > 0
        assert result["max_loss"] > 0
        assert result["breakeven"] < 200.0  # below spot
        assert result["dte"] >= 21

    def test_SP02_no_otm_puts_returns_none(self):
        """No puts below spot → None."""
        chain = _chain_with_puts(spot=200.0, dte_days=30,
                                  puts=[_make_put(210.0, delta=-0.55)])  # ITM put
        result = self._call(chain)
        assert result is None

    def test_SP03_premium_below_floor_returns_none(self):
        """Put with mid < $0.50/share → below $50/contract floor → None."""
        low_prem_put = _make_put(190.0, delta=-0.275, bid=0.05, ask=0.07)
        chain = _chain_with_puts(spot=200.0, dte_days=30, puts=[low_prem_put])
        result = self._call(chain)
        assert result is None

    def test_SP04_no_expiry_in_range_returns_none(self):
        """Expiry with only 5 DTE (below 21-day floor) → None."""
        chain = _chain_with_puts(spot=200.0, dte_days=5)
        result = self._call(chain)
        assert result is None

    def test_SP05_dte_range_is_21_to_45(self):
        """DTE of selected expiry must be in [21, 45]."""
        chain = _chain_with_puts(spot=200.0, dte_days=30)
        result = self._call(chain)
        assert result is not None
        assert 21 <= result["dte"] <= 45

    def test_SP_strike_is_below_spot(self):
        """Selected strike must be OTM (below spot)."""
        chain = _chain_with_puts(spot=200.0, dte_days=30)
        result = self._call(chain)
        assert result is not None
        assert result["long_strike"] < 200.0

    def test_SP_max_loss_is_stop_bounded(self):
        """max_loss = stop_multiple * premium (not worst-case strike value)."""
        chain = _chain_with_puts(spot=200.0, dte_days=30)
        result = self._call(chain)
        assert result is not None
        mid_per_share = abs(result["debit"])
        contracts = result["contracts"]
        expected_max_loss = 2.0 * mid_per_share * contracts * 100
        assert abs(result["max_loss"] - expected_max_loss) < 1.0

    def test_SP_ev_positive_at_target_delta(self):
        """Expected value should be positive for delta ~0.275 with 2x stop."""
        chain = _chain_with_puts(spot=200.0, dte_days=30)
        result = self._call(chain)
        assert result is not None
        # EV = max_gain * pp - max_loss * (1-pp); pp ≈ 0.725 with stop_mult=2.0
        if result["expected_value"] is not None:
            assert result["expected_value"] > 0, (
                f"EV={result['expected_value']} should be positive for short put at delta ~0.275"
            )


# ── SP-06..SP-11: RULE_SHORT_PUT routing ──────────────────────────────────────

class TestRuleShortPut:
    def _route(self, pack_kwargs=None, config=None):
        from bot_options_stage2_structures import _route_strategy
        pack = _FakePack(**(pack_kwargs or {}))
        return _route_strategy(pack, config=config, earnings_calendar_data={})

    def test_SP06_fires_bullish_neutral_iv(self):
        """iv_rank=55, neutral env, bullish dir → ['short_put']."""
        result = self._route({"iv_rank": 55.0, "iv_environment": "neutral",
                               "a1_direction": "bullish", "a1_signal_score": 65.0})
        assert result == ["short_put"]

    def test_SP07_fires_neutral_direction(self):
        """direction=neutral + iv_rank=60 → ['short_put']."""
        result = self._route({"iv_rank": 60.0, "iv_environment": "neutral",
                               "a1_direction": "neutral", "a1_signal_score": 55.0})
        assert result == ["short_put"]

    def test_SP08_does_not_fire_iv_rank_too_low(self):
        """iv_rank=30 < 50 → RULE_SHORT_PUT skipped."""
        result = self._route({"iv_rank": 30.0, "iv_environment": "neutral",
                               "a1_direction": "bullish", "a1_signal_score": 70.0})
        # Should fall through to RULE6 (neutral + directional) or RULE8
        assert "short_put" not in result

    def test_SP09_does_not_fire_bearish(self):
        """direction=bearish → RULE_SHORT_PUT skipped."""
        result = self._route({"iv_rank": 60.0, "iv_environment": "neutral",
                               "a1_direction": "bearish", "a1_signal_score": 65.0})
        assert "short_put" not in result

    def test_SP10_does_not_fire_cheap_iv(self):
        """iv_env=cheap → RULE_SHORT_PUT blocked (cheap IV → buy premium)."""
        result = self._route({"iv_rank": 52.0, "iv_environment": "cheap",
                               "a1_direction": "bullish", "a1_signal_score": 65.0})
        assert "short_put" not in result

    def test_SP11_does_not_fire_earnings_blackout(self):
        """eda=1 smart router intercepts before SHORT_PUT; routes to debit spread."""
        result = self._route({"iv_rank": 60.0, "iv_environment": "neutral",
                               "a1_direction": "bullish", "a1_signal_score": 70.0,
                               "earnings_days_away": 1})
        # RULE1: eda=1, unknown timing → treated as eda=2 → iv_rank=60 < 85 → debit_call_spread
        assert "short_put" not in result   # SHORT_PUT does not fire (RULE1 intercepts first)

    def test_SP_fires_for_expensive_iv(self):
        """iv_env=expensive + iv_rank=65 + bullish → RULE_SHORT_PUT fires."""
        result = self._route({"iv_rank": 65.0, "iv_environment": "expensive",
                               "a1_direction": "bullish", "a1_signal_score": 70.0})
        assert result == ["short_put"]

    def test_SP_does_not_fire_low_conviction(self):
        """a1_signal_score=25 → conviction=low → RULE_SHORT_PUT skipped."""
        result = self._route({"iv_rank": 60.0, "iv_environment": "neutral",
                               "a1_direction": "bullish", "a1_signal_score": 25.0})
        assert "short_put" not in result


# ── SP-12..SP-13: executor sell/buy side ──────────────────────────────────────

class TestSubmitSingleLegSide:
    def _make_structure(self, side: str):
        from schemas import (
            OptionsLeg,
            OptionsStructure,
            OptionStrategy,
            StructureLifecycle,
            Tier,
        )
        leg = OptionsLeg(
            occ_symbol="AAPL260530P00190000",
            underlying="AAPL",
            side=side,
            qty=1,
            option_type="put",
            strike=190.0,
            expiration="2026-05-30",
            bid=1.50,
            ask=1.70,
        )
        s = OptionsStructure(
            structure_id="test-001",
            underlying="AAPL",
            strategy=OptionStrategy.SHORT_PUT if side == "sell" else OptionStrategy.SINGLE_PUT,
            lifecycle=StructureLifecycle.PROPOSED,
            legs=[leg],
            contracts=1,
            max_cost_usd=160.0,
            opened_at="2026-05-01T00:00:00Z",
            catalyst="test",
            tier=Tier.CORE,
        )
        return s

    def test_SP12_sell_side_uses_orderside_sell(self):
        """leg.side='sell' → OrderSide.SELL submitted."""
        from options_executor import _submit_single_leg
        captured = {}

        class _FakeClient:
            def submit_order(self_, req):
                captured["req"] = req
                m = MagicMock()
                m.id = "ord-001"
                return m

        structure = self._make_structure("sell")
        _submit_single_leg(structure, _FakeClient())
        req = captured.get("req")
        assert req is not None
        from alpaca.trading.enums import OrderSide
        assert req.side == OrderSide.SELL

    def test_SP13_buy_side_uses_orderside_buy(self):
        """leg.side='buy' → OrderSide.BUY submitted (existing behavior preserved)."""
        from options_executor import _submit_single_leg
        captured = {}

        class _FakeClient:
            def submit_order(self_, req):
                captured["req"] = req
                m = MagicMock()
                m.id = "ord-002"
                return m

        structure = self._make_structure("buy")
        _submit_single_leg(structure, _FakeClient())
        req = captured.get("req")
        assert req is not None
        from alpaca.trading.enums import OrderSide
        assert req.side == OrderSide.BUY


# ── SP-14: infer_router_rule_fired ────────────────────────────────────────────

class TestInferRouterRuleFired:
    def test_SP14_returns_rule_short_put(self):
        from bot_options_stage2_structures import _infer_router_rule_fired
        pack = _FakePack(iv_rank=60.0, iv_environment="neutral", a1_direction="bullish")
        assert _infer_router_rule_fired(pack, ["short_put"]) == "RULE_SHORT_PUT"

    def test_SP14b_does_not_return_rule6_when_short_put_in_allowed(self):
        """RULE_SHORT_PUT takes priority over RULE6 in infer logic."""
        from bot_options_stage2_structures import _infer_router_rule_fired
        pack = _FakePack(iv_rank=60.0, iv_environment="neutral", a1_direction="bullish")
        # Even if debit spreads also in list, short_put detected first
        result = _infer_router_rule_fired(pack, ["short_put", "debit_call_spread"])
        assert result == "RULE_SHORT_PUT"


# ── SP-15: strategy_config.json has all required keys ─────────────────────────

class TestStrategyConfigShortPut:
    def _load_config(self):
        cfg_path = Path(__file__).parent.parent / "strategy_config.json"
        return json.loads(cfg_path.read_text())

    def test_SP15_a2_router_has_short_put_iv_rank_min(self):
        cfg = self._load_config()
        assert "short_put_iv_rank_min" in cfg.get("a2_router", {})
        assert cfg["a2_router"]["short_put_iv_rank_min"] == 50

    def test_SP15b_account2_has_delta_target(self):
        cfg = self._load_config()
        a2 = cfg.get("account2", {})
        assert "short_put_delta_target" in a2
        assert a2["short_put_delta_target"] == 0.275

    def test_SP15c_account2_has_delta_tolerance(self):
        cfg = self._load_config()
        a2 = cfg.get("account2", {})
        assert "short_put_delta_tolerance" in a2
        assert a2["short_put_delta_tolerance"] == 0.05

    def test_SP15d_account2_has_min_premium(self):
        cfg = self._load_config()
        a2 = cfg.get("account2", {})
        assert "short_put_min_premium_usd" in a2
        assert a2["short_put_min_premium_usd"] == 50

    def test_SP15e_account2_has_stop_loss_multiple(self):
        cfg = self._load_config()
        a2 = cfg.get("account2", {})
        assert "short_put_stop_loss_multiple" in a2
        assert a2["short_put_stop_loss_multiple"] == 2.0
