"""
tests/test_direction_mandate_routing.py — Direction mandate routing tests.

Verifies that _route_strategy() NEVER proposes wrong-direction structures
for any combination of IV rank and A1 signal direction.

DM-01 to DM-15: NEVER assertions (wrong-direction must never appear)
DM-16 to DM-30: CORRECT assertions (right-direction appears for each IV bucket)
DM-31 to DM-35: Straddle/strangle neutral-only assertions
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

_BOT_DIR = os.path.join(os.path.dirname(__file__), "..")
if _BOT_DIR not in sys.path:
    sys.path.insert(0, _BOT_DIR)

from bot_options_stage2_structures import (
    _BEARISH_STRUCTURES,
    _BULLISH_STRUCTURES,
    _NEUTRAL_STRUCTURES,
    _route_strategy,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _pack(
    iv_rank: float,
    iv_environment: str,
    direction: str,
    score: float = 70.0,
    eda: int | None = None,
    skew: float | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        symbol="TEST",
        iv_rank=iv_rank,
        iv_environment=iv_environment,
        a1_direction=direction,
        a1_signal_score=score,
        a1_conviction=score / 100.0,
        earnings_days_away=eda,
        macro_event_flag=False,
        liquidity_score=0.80,
        skew=skew,
    )


_BULLISH_NEVER = ["long_put", "debit_put_spread", "credit_call_spread",
                  "straddle", "strangle", "iron_condor", "iron_butterfly"]
_BEARISH_NEVER = ["long_call", "debit_call_spread", "credit_put_spread", "short_put",
                  "straddle", "strangle", "iron_condor", "iron_butterfly"]


# ── DM-01..DM-05: Bullish signal — wrong-direction structures NEVER appear ────

class TestBullishNeverWrongDirection:
    """For every IV bucket, bullish signal must never produce bearish or neutral structures."""

    @pytest.mark.parametrize("iv_rank,iv_env", [
        (10.0, "very_cheap"),
        (25.0, "cheap"),
        (40.0, "neutral"),
        (65.0, "expensive"),
        (85.0, "very_expensive"),
    ])
    def test_bullish_never_put_structures(self, iv_rank, iv_env):
        """DM-01..05: Bullish + any IV → never long_put, debit_put_spread, credit_call_spread."""
        result = _route_strategy(_pack(iv_rank=iv_rank, iv_environment=iv_env,
                                       direction="bullish"))
        for wrong in ["long_put", "debit_put_spread", "credit_call_spread"]:
            assert wrong not in result, (
                f"bullish iv_rank={iv_rank} iv_env={iv_env} should never produce {wrong!r}, "
                f"got {result}"
            )

    @pytest.mark.parametrize("iv_rank,iv_env", [
        (10.0, "very_cheap"),
        (25.0, "cheap"),
        (40.0, "neutral"),
        (65.0, "expensive"),
        (85.0, "very_expensive"),
    ])
    def test_bullish_never_neutral_structures(self, iv_rank, iv_env):
        """DM-01..05: Bullish + any IV → never straddle, strangle, iron_condor, iron_butterfly."""
        result = _route_strategy(_pack(iv_rank=iv_rank, iv_environment=iv_env,
                                       direction="bullish"))
        for wrong in ["straddle", "strangle", "iron_condor", "iron_butterfly"]:
            assert wrong not in result, (
                f"bullish iv_rank={iv_rank} iv_env={iv_env} should never produce {wrong!r}, "
                f"got {result}"
            )


# ── DM-06..DM-10: Bearish signal — wrong-direction structures NEVER appear ────

class TestBearishNeverWrongDirection:
    """For every IV bucket, bearish signal must never produce bullish or neutral structures."""

    @pytest.mark.parametrize("iv_rank,iv_env", [
        (10.0, "very_cheap"),
        (25.0, "cheap"),
        (40.0, "neutral"),
        (65.0, "expensive"),
    ])
    def test_bearish_never_call_structures(self, iv_rank, iv_env):
        """DM-06..09: Bearish + any IV → never long_call, debit_call_spread, credit_put_spread, short_put."""
        result = _route_strategy(_pack(iv_rank=iv_rank, iv_environment=iv_env,
                                       direction="bearish"))
        for wrong in ["long_call", "debit_call_spread", "credit_put_spread", "short_put"]:
            assert wrong not in result, (
                f"bearish iv_rank={iv_rank} iv_env={iv_env} should never produce {wrong!r}, "
                f"got {result}"
            )

    @pytest.mark.parametrize("iv_rank,iv_env", [
        (10.0, "very_cheap"),
        (25.0, "cheap"),
        (40.0, "neutral"),
        (65.0, "expensive"),
    ])
    def test_bearish_never_neutral_structures(self, iv_rank, iv_env):
        """DM-06..09: Bearish + any IV → never straddle, strangle, iron_condor, iron_butterfly."""
        result = _route_strategy(_pack(iv_rank=iv_rank, iv_environment=iv_env,
                                       direction="bearish"))
        for wrong in ["straddle", "strangle", "iron_condor", "iron_butterfly"]:
            assert wrong not in result, (
                f"bearish iv_rank={iv_rank} iv_env={iv_env} should never produce {wrong!r}, "
                f"got {result}"
            )


# ── DM-11..DM-15: Correct-direction routing for each IV bucket (bullish) ──────

class TestBullishCorrectRouting:
    """Verify correct bullish structure is proposed at each IV rank tier."""

    def test_dm11_bullish_very_cheap_iv15(self):
        """DM-11: Bullish + very_cheap IV → long_call or debit_call_spread."""
        result = _route_strategy(_pack(10.0, "very_cheap", "bullish"))
        assert result, "Expected non-empty result for bullish + very_cheap"
        assert any(s in _BULLISH_STRUCTURES for s in result), (
            f"No bullish structure in {result}"
        )
        assert any(s in ["long_call", "debit_call_spread"] for s in result), (
            f"Expected call structure for bullish very_cheap, got {result}"
        )

    def test_dm12_bullish_cheap_iv25(self):
        """DM-12: Bullish + cheap IV → debit_call_spread or long_call."""
        result = _route_strategy(_pack(25.0, "cheap", "bullish"))
        assert any(s in ["long_call", "debit_call_spread"] for s in result), (
            f"Expected call structure for bullish cheap, got {result}"
        )

    def test_dm13_bullish_neutral_iv40(self):
        """DM-13: Bullish + neutral IV → debit_call_spread."""
        result = _route_strategy(_pack(40.0, "neutral", "bullish"))
        assert "debit_call_spread" in result, (
            f"Expected debit_call_spread for bullish neutral IV, got {result}"
        )

    def test_dm14_bullish_expensive_iv65(self):
        """DM-14: Bullish + expensive IV → credit_put_spread or debit_call_spread or short_put."""
        result = _route_strategy(_pack(65.0, "expensive", "bullish"))
        assert result, "Expected non-empty result for bullish + expensive"
        assert any(s in _BULLISH_STRUCTURES for s in result), (
            f"No bullish structure in {result}"
        )

    def test_dm15_bullish_very_expensive_iv85(self):
        """DM-15: Bullish + very_expensive IV → credit_put_spread."""
        result = _route_strategy(_pack(85.0, "very_expensive", "bullish"))
        assert "credit_put_spread" in result, (
            f"Expected credit_put_spread for bullish very_expensive, got {result}"
        )


# ── DM-16..DM-20: Correct-direction routing for each IV bucket (bearish) ──────

class TestBearishCorrectRouting:
    """Verify correct bearish structure is proposed at each IV rank tier."""

    def test_dm16_bearish_very_cheap_iv15(self):
        """DM-16: Bearish + very_cheap IV → long_put or debit_put_spread."""
        result = _route_strategy(_pack(10.0, "very_cheap", "bearish"))
        assert any(s in ["long_put", "debit_put_spread"] for s in result), (
            f"Expected put structure for bearish very_cheap, got {result}"
        )

    def test_dm17_bearish_cheap_iv25(self):
        """DM-17: Bearish + cheap IV → long_put or debit_put_spread."""
        result = _route_strategy(_pack(25.0, "cheap", "bearish"))
        assert any(s in ["long_put", "debit_put_spread"] for s in result), (
            f"Expected put structure for bearish cheap, got {result}"
        )

    def test_dm18_bearish_neutral_iv40(self):
        """DM-18: Bearish + neutral IV → debit_put_spread."""
        result = _route_strategy(_pack(40.0, "neutral", "bearish"))
        assert "debit_put_spread" in result, (
            f"Expected debit_put_spread for bearish neutral IV, got {result}"
        )

    def test_dm19_bearish_expensive_iv65(self):
        """DM-19: Bearish + expensive IV → credit_call_spread or debit_put_spread."""
        result = _route_strategy(_pack(65.0, "expensive", "bearish"))
        assert result, "Expected non-empty result for bearish + expensive"
        assert any(s in _BEARISH_STRUCTURES for s in result), (
            f"No bearish structure in {result}"
        )

    def test_dm20_bearish_very_expensive_iv85(self):
        """DM-20: Bearish + very_expensive IV → credit_call_spread."""
        result = _route_strategy(_pack(85.0, "very_expensive", "bearish"))
        assert "credit_call_spread" in result, (
            f"Expected credit_call_spread for bearish very_expensive, got {result}"
        )


# ── DM-21..DM-25: Straddle/strangle/condor only for neutral ──────────────────

class TestNeutralOnlyStructures:
    """Straddle, strangle, iron_condor, iron_butterfly must never appear for directional signals."""

    @pytest.mark.parametrize("direction", ["bullish", "bearish"])
    @pytest.mark.parametrize("eda", [7, 10, 14])  # within straddle window
    def test_dm21_straddle_never_for_directional_with_earnings(self, direction, eda):
        """DM-21: Directional signal + earnings in straddle window → straddle/strangle never proposed."""
        result = _route_strategy(
            _pack(20.0, "cheap", direction, eda=eda)
        )
        assert "straddle" not in result, (
            f"{direction} + eda={eda} cheap IV should not produce straddle, got {result}"
        )
        assert "strangle" not in result, (
            f"{direction} + eda={eda} cheap IV should not produce strangle, got {result}"
        )

    def test_dm22_neutral_cheap_earnings_window_gets_straddle(self):
        """DM-22: Neutral + cheap IV + earnings 6-14 days → straddle/strangle (correct)."""
        result = _route_strategy(_pack(20.0, "cheap", "neutral", eda=10))
        assert any(s in result for s in ["straddle", "strangle"]), (
            f"Expected straddle/strangle for neutral + cheap + eda=10, got {result}"
        )

    @pytest.mark.parametrize("direction", ["bullish", "bearish"])
    def test_dm23_iron_condor_for_directional_high_iv(self, direction):
        """DM-23: Directional + iv_rank=80 + expensive → iron_condor IS proposed (BIAS-4).
        IV overrides direction when rank >= 75; neutral guard allows iron structure through."""
        result = _route_strategy(
            _pack(80.0, "expensive", direction, score=75.0)
        )
        assert "iron_condor" in result, (
            f"{direction} + iv_rank=80 should produce iron_condor (BIAS-4), got {result}"
        )

    @pytest.mark.parametrize("direction", ["bullish", "bearish"])
    def test_dm24_iron_condor_never_for_directional_iv85(self, direction):
        """DM-24: Directional + iv_rank=85 (formerly triggered old RULE_IRON bug) → no iron structures."""
        result = _route_strategy(
            _pack(85.0, "very_expensive", direction, score=70.0)
        )
        assert "iron_condor" not in result, (
            f"iv_rank=85 {direction} must not produce iron_condor, got {result}"
        )
        assert "iron_butterfly" not in result, (
            f"iv_rank=85 {direction} must not produce iron_butterfly, got {result}"
        )

    def test_dm25_neutral_elevated_iv_gets_iron_condor(self):
        """DM-25: Neutral + elevated IV → iron_condor (correct neutral structure)."""
        result = _route_strategy(_pack(65.0, "expensive", "neutral"))
        assert "iron_condor" in result, (
            f"Expected iron_condor for neutral + expensive IV, got {result}"
        )


# ── DM-26..DM-30: BIDU/MSFT/TSLA regression cases ────────────────────────────

class TestLiveBugRegressions:
    """Regression tests for the specific symptoms seen 2026-05-06."""

    def test_dm26_bidu_bullish_earnings12_cheap_iv_no_straddle(self):
        """DM-26: BIDU symptom — bullish + eda=12 + iv_rank=0 → NO straddle (was the primary bug)."""
        result = _route_strategy(_pack(0.0, "very_cheap", "bullish", eda=12))
        assert "straddle" not in result, f"BIDU regression: straddle must not appear, got {result}"
        assert "strangle" not in result, f"BIDU regression: strangle must not appear, got {result}"

    def test_dm27_bidu_bullish_earnings12_cheap_iv_gets_call_spread(self):
        """DM-27: BIDU bullish + eda=12 + very_cheap IV → falls through to RULE_EARNINGS → debit_call_spread."""
        result = _route_strategy(_pack(0.0, "very_cheap", "bullish", eda=12))
        assert "debit_call_spread" in result, (
            f"BIDU bullish+eda=12 should produce debit_call_spread, got {result}"
        )

    def test_dm28_nvda_bullish_earnings14_iv40_no_straddle(self):
        """DM-28: NVDA symptom — bullish + eda=14 + iv_rank=39.9 → no straddle/strangle."""
        result = _route_strategy(_pack(39.9, "cheap", "bullish", eda=14))
        assert "straddle" not in result, f"NVDA regression: straddle must not appear, got {result}"
        assert "strangle" not in result, f"NVDA regression: strangle must not appear, got {result}"

    def test_dm29_msft_bullish_high_skew_gets_bearish_structure(self):
        """DM-29: MSFT symptom — bullish but skew ≥ 1.50 overrides to bearish → bearish structure."""
        result = _route_strategy(_pack(20.0, "cheap", "bullish", skew=2.27))
        assert result, "Expected non-empty result for skew-overridden bearish"
        # After skew override, effective_dir=bearish → only bearish structures
        for s in result:
            assert s in _BEARISH_STRUCTURES, (
                f"Skew-overridden bearish must only have bearish structures, got {s!r} in {result}"
            )

    def test_dm30_all_results_are_direction_aligned(self):
        """DM-30: Full matrix spot-check — every non-empty result contains only aligned structures."""
        cases = [
            (10.0,  "very_cheap",    "bullish",  _BULLISH_STRUCTURES),
            (25.0,  "cheap",         "bullish",  _BULLISH_STRUCTURES),
            (40.0,  "neutral",       "bullish",  _BULLISH_STRUCTURES),
            (65.0,  "expensive",     "bullish",  _BULLISH_STRUCTURES),
            (85.0,  "very_expensive","bullish",  _BULLISH_STRUCTURES),
            (10.0,  "very_cheap",    "bearish",  _BEARISH_STRUCTURES),
            (25.0,  "cheap",         "bearish",  _BEARISH_STRUCTURES),
            (40.0,  "neutral",       "bearish",  _BEARISH_STRUCTURES),
            (65.0,  "expensive",     "bearish",  _BEARISH_STRUCTURES),
            (65.0,  "expensive",     "neutral",  _NEUTRAL_STRUCTURES),
        ]
        for iv_rank, iv_env, direction, ok_set in cases:
            result = _route_strategy(_pack(iv_rank, iv_env, direction))
            for s in result:
                assert s in ok_set, (
                    f"iv_rank={iv_rank} iv_env={iv_env} direction={direction}: "
                    f"structure {s!r} not in expected set {ok_set}, got {result}"
                )
