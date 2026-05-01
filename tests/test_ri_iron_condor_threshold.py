"""
test_ri_iron_condor_threshold.py — Tests for the expanded RULE_IRON in
bot_options_stage2_structures._route_strategy().

RI-01: iv_rank=52, direction=neutral → iron_condor, iron_butterfly
RI-02: iv_rank=52, direction=bullish, conviction=0.55 → iron_condor (low conviction)
RI-03: iv_rank=52, direction=bullish, conviction=0.70 → NOT iron condor (goes to RULE5/6/7)
RI-04: iv_rank=45, direction=neutral → NOT iron condor (below threshold)
RI-05: iron_iv_rank_min config key respected
RI-06: iron_low_conviction_threshold config key respected
"""

from types import SimpleNamespace

import pytest
from bot_options_stage2_structures import _route_strategy


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pack(
    iv_rank: float = 52.0,
    iv_environment: str = "neutral",
    a1_direction: str = "neutral",
    a1_conviction: float = 0.80,
    a1_signal_score: float = 50.0,
    symbol: str = "NVDA",
    earnings_days_away: int = 20,
    earnings_timing=None,
    liquidity_score: float = 0.95,
    macro_event_flag: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        iv_rank=iv_rank,
        iv_environment=iv_environment,
        a1_direction=a1_direction,
        a1_conviction=a1_conviction,
        a1_signal_score=a1_signal_score,
        symbol=symbol,
        earnings_days_away=earnings_days_away,
        earnings_timing=earnings_timing,
        liquidity_score=liquidity_score,
        macro_event_flag=macro_event_flag,
    )


def _config(iron_iv_rank_min: float = 50, iron_low_conviction_threshold: float = 0.60) -> dict:
    return {
        "a2_router": {
            "earnings_dte_blackout": 2,
            "min_liquidity_score": 0.10,
            "macro_iv_gate_rank": 70,
            "iv_env_blackout": [],
            "iron_iv_rank_min": iron_iv_rank_min,
            "iron_low_conviction_threshold": iron_low_conviction_threshold,
            "short_put_iv_rank_min": 50,
            "macro_event_routing_enabled": False,
            "earnings_dte_1_premarket_enabled": False,
            "earnings_dte_2_enabled": False,
            "pre_earnings_credit_spread_enabled": False,
        }
    }


# ── RI-01: iv_rank=52, neutral → iron structures ─────────────────────────────

class TestRI01NeutralIron:
    """iv_rank=52, neutral direction → iron_condor (and/or iron_butterfly)."""

    def test_neutral_direction_routes_to_iron_condor(self):
        result = _route_strategy(_pack(iv_rank=52.0, a1_direction="neutral"), config=_config())
        assert "iron_condor" in result

    def test_neutral_direction_does_not_route_to_debit(self):
        result = _route_strategy(_pack(iv_rank=52.0, a1_direction="neutral"), config=_config())
        assert "debit_call_spread" not in result
        assert "debit_put_spread" not in result


# ── RI-02: iv_rank=52, bullish, low conviction → iron_condor ─────────────────

class TestRI02LowConvictionDirectional:
    """iv_rank=52, direction=bullish, conviction=0.55 → iron_condor."""

    def test_bullish_low_conviction_routes_to_iron_condor(self):
        result = _route_strategy(
            _pack(iv_rank=52.0, a1_direction="bullish", a1_conviction=0.55),
            config=_config(),
        )
        assert "iron_condor" in result

    def test_bearish_low_conviction_routes_to_iron_condor(self):
        result = _route_strategy(
            _pack(iv_rank=52.0, a1_direction="bearish", a1_conviction=0.50),
            config=_config(),
        )
        assert "iron_condor" in result

    def test_conviction_exactly_at_floor_not_routed_to_iron(self):
        # conviction == threshold → NOT low conviction (< not <=)
        result = _route_strategy(
            _pack(iv_rank=52.0, a1_direction="bullish", a1_conviction=0.60),
            config=_config(),
        )
        # At the boundary (0.60 == threshold), not low conviction → skip RULE_IRON
        assert "iron_condor" not in result


# ── RI-03: iv_rank=52, bullish, conviction=0.70 → NOT iron condor ─────────────

class TestRI03HighConvictionDirectional:
    """iv_rank=52, bullish, conviction=0.70 → falls through to RULE5/6/7, not RULE_IRON."""

    def test_high_conviction_bullish_not_iron(self):
        result = _route_strategy(
            _pack(iv_rank=52.0, iv_environment="neutral", a1_direction="bullish", a1_conviction=0.70),
            config=_config(),
        )
        assert "iron_condor" not in result
        assert "iron_butterfly" not in result

    def test_high_conviction_bearish_not_iron(self):
        result = _route_strategy(
            _pack(iv_rank=52.0, iv_environment="neutral", a1_direction="bearish", a1_conviction=0.75),
            config=_config(),
        )
        assert "iron_condor" not in result
        assert "iron_butterfly" not in result


# ── RI-04: iv_rank=45, neutral → NOT iron condor (below threshold) ─────────────

class TestRI04BelowThreshold:
    """iv_rank=45 (below 50) → RULE_IRON does not fire even for neutral direction."""

    def test_below_threshold_neutral_not_iron(self):
        result = _route_strategy(
            _pack(iv_rank=45.0, a1_direction="neutral"),
            config=_config(iron_iv_rank_min=50),
        )
        assert "iron_condor" not in result
        assert "iron_butterfly" not in result

    def test_below_threshold_low_conviction_not_iron(self):
        result = _route_strategy(
            _pack(iv_rank=45.0, a1_direction="bullish", a1_conviction=0.40),
            config=_config(iron_iv_rank_min=50),
        )
        assert "iron_condor" not in result


# ── RI-05: iron_iv_rank_min config key respected ──────────────────────────────

class TestRI05ConfigIronMin:
    """iron_iv_rank_min in config controls the threshold — not hardcoded."""

    def test_iron_fires_at_custom_threshold(self):
        # Raise threshold to 60 — iv_rank=55 should NOT route to iron
        result = _route_strategy(
            _pack(iv_rank=55.0, a1_direction="neutral"),
            config=_config(iron_iv_rank_min=60),
        )
        assert "iron_condor" not in result

    def test_iron_fires_at_configured_50(self):
        # Default threshold of 50 — iv_rank=51 should route to iron for neutral
        result = _route_strategy(
            _pack(iv_rank=51.0, a1_direction="neutral"),
            config=_config(iron_iv_rank_min=50),
        )
        assert "iron_condor" in result

    def test_iron_does_not_fire_just_below_configured_threshold(self):
        result = _route_strategy(
            _pack(iv_rank=49.9, a1_direction="neutral"),
            config=_config(iron_iv_rank_min=50),
        )
        assert "iron_condor" not in result


# ── RI-06: iron_low_conviction_threshold config key respected ─────────────────

class TestRI06ConfigConvictionThreshold:
    """iron_low_conviction_threshold in config controls the low-conv boundary."""

    def test_custom_high_threshold_catches_more_cases(self):
        # Raise threshold to 0.75 — conviction=0.70 should now be "low" → iron
        result = _route_strategy(
            _pack(iv_rank=55.0, a1_direction="bullish", a1_conviction=0.70),
            config=_config(iron_iv_rank_min=50, iron_low_conviction_threshold=0.75),
        )
        assert "iron_condor" in result

    def test_custom_low_threshold_catches_fewer_cases(self):
        # Lower threshold to 0.40 — conviction=0.55 is now NOT low → falls through
        result = _route_strategy(
            _pack(iv_rank=55.0, iv_environment="neutral", a1_direction="bullish", a1_conviction=0.55),
            config=_config(iron_iv_rank_min=50, iron_low_conviction_threshold=0.40),
        )
        assert "iron_condor" not in result

    def test_missing_conviction_treated_as_low(self):
        # Pack without a1_conviction attribute → getattr returns None → float(None or 0) = 0 → < threshold
        pack = SimpleNamespace(
            iv_rank=55.0,
            iv_environment="neutral",
            a1_direction="bullish",
            # no a1_conviction attribute
            a1_signal_score=50.0,
            symbol="AAPL",
            earnings_days_away=20,
            earnings_timing=None,
            liquidity_score=0.95,
            macro_event_flag=False,
        )
        result = _route_strategy(pack, config=_config())
        assert "iron_condor" in result
