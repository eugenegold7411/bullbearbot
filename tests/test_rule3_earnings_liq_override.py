"""
tests/test_rule3_earnings_liq_override.py

Tests for RULE3 earnings liquidity override in _route_strategy().

Four cases:
  (a) AMAT-like: liq=0.11, eda=6, conviction=high → passes RULE3 (floor lowered to 0.08)
  (b) AMAT-like: liq=0.07, eda=6, conviction=high → blocked (below 0.08 floor)
  (c) AMAT-like: liq=0.11, eda=6, conviction=medium → standard floor applies, blocked (< 0.25)
  (d) Non-earnings symbol: liq=0.11, no earnings_conviction → blocked by standard floor
"""

import sys
from pathlib import Path
from types import SimpleNamespace

_REPO = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO))

import bot_options_stage2_structures as s2

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pack(
    liquidity_score: float,
    earnings_days_away: int | None,
    conviction_level: str | None,
    iv_environment: str = "neutral",
    iv_rank: float = 45.0,
    a1_direction: str = "bullish",
    a1_signal_score: float = 65.0,
    macro_event_flag: bool = False,
) -> object:
    """Return a minimal A2FeaturePack-compatible namespace for routing tests."""
    earnings_conviction = (
        {"conviction_level": conviction_level, "beat_rate": 1.0, "eda": earnings_days_away}
        if conviction_level is not None
        else None
    )
    return SimpleNamespace(
        symbol="TEST",
        a1_direction=a1_direction,
        a1_signal_score=a1_signal_score,
        iv_rank=iv_rank,
        iv_environment=iv_environment,
        term_structure_slope=None,
        skew=None,
        expected_move_pct=0.04,
        earnings_days_away=earnings_days_away,
        macro_event_flag=macro_event_flag,
        liquidity_score=liquidity_score,
        earnings_conviction=earnings_conviction,
        premium_budget_usd=500.0,
        trend_score=None,
        momentum_score=None,
        flow_imbalance_30m=None,
        sweep_count=None,
        gex_regime=None,
        oi_concentration=None,
    )


def _config_with_floor(std_floor: float = 0.25) -> dict:
    """Return a minimal strategy_config override with a known standard liq floor."""
    return {
        "a2_router": {
            "min_liquidity_score": std_floor,
            "earnings_liq_override_floor": 0.08,
            "earnings_liq_override_max_eda": 7,
        }
    }


# ---------------------------------------------------------------------------
# (a) High-conviction earnings, liq=0.11 → passes (floor lowered to 0.08)
# ---------------------------------------------------------------------------

def test_a_high_conviction_earnings_liq_passes_override_floor():
    pack = _make_pack(liquidity_score=0.11, earnings_days_away=6, conviction_level="high",
                      iv_environment="neutral")
    result = s2._route_strategy(pack, config=_config_with_floor(0.25))
    assert result != [], (
        "AMAT-like symbol (liq=0.11, eda=6, conviction=high) should pass RULE3 "
        "via earnings liquidity override (floor 0.08), but got []"
    )


# ---------------------------------------------------------------------------
# (b) High-conviction earnings, liq=0.07 → blocked (below 0.08 floor)
# ---------------------------------------------------------------------------

def test_b_high_conviction_earnings_liq_below_override_floor_blocked():
    pack = _make_pack(liquidity_score=0.07, earnings_days_away=6, conviction_level="high",
                      iv_environment="neutral")
    result = s2._route_strategy(pack, config=_config_with_floor(0.25))
    assert result == [], (
        "liq=0.07 < earnings_liq_override_floor=0.08 must still be blocked by RULE3"
    )


# ---------------------------------------------------------------------------
# (c) Medium-conviction earnings, liq=0.11 → standard floor applies, blocked
# ---------------------------------------------------------------------------

def test_c_medium_conviction_earnings_standard_floor_applies():
    pack = _make_pack(liquidity_score=0.11, earnings_days_away=6, conviction_level="medium",
                      iv_environment="neutral")
    result = s2._route_strategy(pack, config=_config_with_floor(0.25))
    assert result == [], (
        "conviction=medium must not trigger earnings liq override; "
        "standard floor 0.25 applies and liq=0.11 should be blocked"
    )


# ---------------------------------------------------------------------------
# (d) Non-earnings symbol, liq=0.11 → standard floor applies, blocked
# ---------------------------------------------------------------------------

def test_d_non_earnings_symbol_standard_floor_applies():
    pack = _make_pack(liquidity_score=0.11, earnings_days_away=None, conviction_level=None,
                      iv_environment="neutral")
    result = s2._route_strategy(pack, config=_config_with_floor(0.25))
    assert result == [], (
        "Non-earnings symbol with liq=0.11 must be blocked by standard RULE3 floor of 0.25"
    )


# ---------------------------------------------------------------------------
# Edge: eda=0 (event day) must not trigger override (override condition: 0 < eda <= max)
# ---------------------------------------------------------------------------

def test_e_event_day_eda_zero_no_override():
    pack = _make_pack(liquidity_score=0.11, earnings_days_away=0, conviction_level="high",
                      iv_environment="neutral")
    result = s2._route_strategy(pack, config=_config_with_floor(0.25))
    assert result == [], (
        "eda=0 (event day) must not trigger the earnings liquidity override"
    )
