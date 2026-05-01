"""
tests/test_s9_graduated_trail.py — Sprint 9 graduated trail-stop tests.

15 test cases covering gain_pct/stop_pct tier format:
  TC-01  Tier 1 fires at +3% gain → stop = entry × 1.01
  TC-02  Tier 2 fires at +5% gain → stop = entry × 1.03
  TC-03  Tier 3 fires at +10% gain → stop = entry × 1.07
  TC-04  Tier 4 fires at +15% gain → stop = entry × 1.12
  TC-05  Tier 5 fires at +20% gain → stop = entry × 1.17
  TC-06  Partial gain (+4%) picks tier 1, not tier 2
  TC-07  No tier fires below +3% → current_stop returned
  TC-08  Never-narrow: new_stop < current_stop → current_stop returned
  TC-09  MA real scenario: entry=$502.35, current=$529.87 → tier 2
  TC-10  V real scenario: existing stop better than tier target → no change
  TC-11  XLE real scenario: tier 1 fires and improves existing stop
  TC-12  trail_tiers=[] in config → legacy path used
  TC-13  Legacy profit_r format tiers → _graduated_trail_stop returns None → legacy path
  TC-14  new_stop >= current_price → safety cap → current_stop returned
  TC-15  entry_price=0 → returns current_stop immediately
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

from exit_manager import _graduated_trail_stop  # noqa: E402

# ---------------------------------------------------------------------------
# Shared tier list (mirrors strategy_config.json)
# ---------------------------------------------------------------------------

_TIERS = [
    {"gain_pct": 0.03, "stop_pct": 0.01},
    {"gain_pct": 0.05, "stop_pct": 0.03},
    {"gain_pct": 0.10, "stop_pct": 0.07},
    {"gain_pct": 0.15, "stop_pct": 0.12},
    {"gain_pct": 0.20, "stop_pct": 0.17},
]


def _cfg(tiers=None):
    return {
        "parameters": {"stop_loss_pct_core": 0.03},
        "exit_management": {
            "trail_stop_enabled": True,
            "trail_tiers": tiers if tiers is not None else _TIERS,
        },
    }


def _legacy_cfg(trigger_r=1.0, plus_pct=0.005):
    return {
        "parameters": {"stop_loss_pct_core": 0.035},
        "exit_management": {
            "trail_stop_enabled": True,
            "trail_trigger_r": trigger_r,
            "trail_to_breakeven_plus_pct": plus_pct,
            # trail_tiers intentionally absent → legacy path
        },
    }


def _pos(symbol="AAPL", entry=100.0, current=120.0, unrealized=200.0, qty=10):
    p = MagicMock()
    p.symbol = symbol
    p.avg_entry_price = str(entry)
    p.current_price = str(current)
    p.unrealized_pl = str(unrealized)
    p.qty = str(qty)
    return p


# ---------------------------------------------------------------------------
# TC-01: Tier 1 fires at +3% gain
# ---------------------------------------------------------------------------

class TestTC01Tier1:
    def test_tier1_at_3pct_gain(self):
        # current=103 → gain=3% → tier 1 (stop_pct=0.01) → stop=100×1.01=101.00
        result = _graduated_trail_stop(
            entry_price=100.0,
            current_price=103.0,
            current_stop=97.0,
            trail_tiers=_TIERS,
        )
        assert result == 101.00, f"Expected $101.00, got {result}"


# ---------------------------------------------------------------------------
# TC-02: Tier 2 fires at +5% gain
# ---------------------------------------------------------------------------

class TestTC02Tier2:
    def test_tier2_at_5pct_gain(self):
        # current=105 → gain=5% → tier 2 (stop_pct=0.03) → stop=100×1.03=103.00
        result = _graduated_trail_stop(
            entry_price=100.0,
            current_price=105.0,
            current_stop=97.0,
            trail_tiers=_TIERS,
        )
        assert result == 103.00, f"Expected $103.00, got {result}"


# ---------------------------------------------------------------------------
# TC-03: Tier 3 fires at +10% gain
# ---------------------------------------------------------------------------

class TestTC03Tier3:
    def test_tier3_at_10pct_gain(self):
        # current=111 → gain=11% → tier 3 (stop_pct=0.07) → stop=100×1.07=107.00
        # (using 111 not 110 to avoid floating-point edge: 100.0*1.10 == 110.00000000000001)
        result = _graduated_trail_stop(
            entry_price=100.0,
            current_price=111.0,
            current_stop=97.0,
            trail_tiers=_TIERS,
        )
        assert result == 107.00, f"Expected $107.00, got {result}"


# ---------------------------------------------------------------------------
# TC-04: Tier 4 fires at +15% gain
# ---------------------------------------------------------------------------

class TestTC04Tier4:
    def test_tier4_at_15pct_gain(self):
        # current=115 → gain=15% → tier 4 (stop_pct=0.12) → stop=100×1.12=112.00
        result = _graduated_trail_stop(
            entry_price=100.0,
            current_price=115.0,
            current_stop=97.0,
            trail_tiers=_TIERS,
        )
        assert result == 112.00, f"Expected $112.00, got {result}"


# ---------------------------------------------------------------------------
# TC-05: Tier 5 fires at +20% gain
# ---------------------------------------------------------------------------

class TestTC05Tier5:
    def test_tier5_at_20pct_gain(self):
        # current=120 → gain=20% → tier 5 (stop_pct=0.17) → stop=100×1.17=117.00
        result = _graduated_trail_stop(
            entry_price=100.0,
            current_price=120.0,
            current_stop=97.0,
            trail_tiers=_TIERS,
        )
        assert result == 117.00, f"Expected $117.00, got {result}"


# ---------------------------------------------------------------------------
# TC-06: +4% gain picks tier 1 (highest applicable), not tier 2
# ---------------------------------------------------------------------------

class TestTC06CorrectTierSelection:
    def test_4pct_gain_picks_tier1_not_tier2(self):
        # current=104 → gain=4% → qualifies for tier 1 (≥3%) but NOT tier 2 (≥5%)
        # → stop_pct=0.01 → stop=100×1.01=101.00
        result = _graduated_trail_stop(
            entry_price=100.0,
            current_price=104.0,
            current_stop=97.0,
            trail_tiers=_TIERS,
        )
        assert result == 101.00, f"Expected $101.00 (tier 1), got {result}"


# ---------------------------------------------------------------------------
# TC-07: No tier fires below +3% → current_stop returned
# ---------------------------------------------------------------------------

class TestTC07NoTierFires:
    def test_below_3pct_gain_returns_current_stop(self):
        # current=102.5 → gain=2.5% < 3% → no tier qualifies → current_stop returned
        result = _graduated_trail_stop(
            entry_price=100.0,
            current_price=102.5,
            current_stop=97.0,
            trail_tiers=_TIERS,
        )
        assert result == 97.0, f"Expected current_stop=97.0, got {result}"

    def test_exactly_at_threshold_qualifies(self):
        # current=103.0 → gain=3.0% → qualifies for tier 1
        result = _graduated_trail_stop(
            entry_price=100.0,
            current_price=103.0,
            current_stop=97.0,
            trail_tiers=_TIERS,
        )
        assert result == 101.00, f"Expected $101.00 at exactly 3% threshold, got {result}"


# ---------------------------------------------------------------------------
# TC-08: Never-narrow — new_stop < current_stop → current_stop returned
# ---------------------------------------------------------------------------

class TestTC08NeverNarrow:
    def test_new_stop_below_current_stop_returns_current(self):
        # entry=100, current=103 → tier 1 → new_stop=101.00
        # current_stop=102 (already better) → must return 102, not 101
        result = _graduated_trail_stop(
            entry_price=100.0,
            current_price=103.0,
            current_stop=102.0,
            trail_tiers=_TIERS,
        )
        assert result == 102.0, f"Expected current_stop=102.0 (never narrow), got {result}"

    def test_equal_stop_returns_current(self):
        # new_stop == current_stop → still return current (no improvement)
        result = _graduated_trail_stop(
            entry_price=100.0,
            current_price=103.0,
            current_stop=101.0,  # exactly what tier 1 would produce
            trail_tiers=_TIERS,
        )
        assert result == 101.0, f"Expected 101.0 (no improvement case), got {result}"


# ---------------------------------------------------------------------------
# TC-09: MA real scenario — entry=$502.35, current=$529.87 (+5.5%) → tier 2
# ---------------------------------------------------------------------------

class TestTC09MAScenario:
    def test_ma_tier2_fires(self):
        # gain = (529.87 - 502.35) / 502.35 = 5.48% → tier 2 (stop_pct=0.03)
        # new_stop = 502.35 × 1.03 = 517.4205 → rounds to 517.42
        result = _graduated_trail_stop(
            entry_price=502.35,
            current_price=529.87,
            current_stop=487.28,  # 502.35 × (1 - 0.03)
            trail_tiers=_TIERS,
        )
        assert result == 517.42, f"Expected $517.42 (MA tier 2), got {result}"


# ---------------------------------------------------------------------------
# TC-10: V real scenario — existing stop better than tier target → no change
# ---------------------------------------------------------------------------

class TestTC10VScenario:
    def test_v_existing_stop_beats_tier_target(self):
        # entry=$310.30, current=$337 → gain=8.6% → tier 2 (stop_pct=0.03)
        # new_stop = 310.30 × 1.03 = 319.609 → rounds to 319.61
        # current_stop=$333 > $319.61 → never-narrow → return $333
        result = _graduated_trail_stop(
            entry_price=310.30,
            current_price=337.0,
            current_stop=333.0,
            trail_tiers=_TIERS,
        )
        assert result == 333.0, f"Expected current_stop=333.0 (V stop better than tier), got {result}"


# ---------------------------------------------------------------------------
# TC-11: XLE real scenario — tier 1 fires and improves existing stop
# ---------------------------------------------------------------------------

class TestTC11XLEScenario:
    def test_xle_tier1_improves_stop(self):
        # entry=$56.73, current=$58.88 → gain=3.79% → tier 1 (stop_pct=0.01)
        # new_stop = 56.73 × 1.01 = 57.2973 → rounds to 57.30
        # current_stop=$57.01 < $57.30 → fires → return $57.30
        result = _graduated_trail_stop(
            entry_price=56.73,
            current_price=58.88,
            current_stop=57.01,
            trail_tiers=_TIERS,
        )
        assert result == 57.30, f"Expected $57.30 (XLE tier 1), got {result}"


# ---------------------------------------------------------------------------
# TC-12: trail_tiers=[] → legacy path used via maybe_trail_stop
# ---------------------------------------------------------------------------

class TestTC12EmptyTiersLegacyPath:
    def test_empty_tiers_routes_to_legacy(self):
        from exit_manager import maybe_trail_stop

        # entry=$100, stop=$97 → dist=$3, current=$104 → profit_r=4/3≈1.33x ≥ 1.0
        # legacy fires: new_stop = 100×1.005 = $100.50
        pos = _pos(entry=100.0, current=104.0, unrealized=400.0)
        ei = {"stop_price": 97.0, "stop_order_id": "ord-legacy", "stop_order_status": "open"}
        cfg = _legacy_cfg(trigger_r=1.0, plus_pct=0.005)  # no trail_tiers key

        alpaca_mock = MagicMock()

        with patch("time.sleep"):
            result = maybe_trail_stop(pos, alpaca_mock, cfg, exit_info=ei)
        assert result is True, "Expected legacy trail to fire when trail_tiers absent"
        req = alpaca_mock.submit_order.call_args[0][0]
        assert abs(req.stop_price - 100.50) < 0.01, (
            f"Expected legacy stop $100.50, got {req.stop_price}"
        )


# ---------------------------------------------------------------------------
# TC-13: Legacy profit_r format → _graduated_trail_stop returns None → legacy path
# ---------------------------------------------------------------------------

class TestTC13LegacyFormatDetection:
    def test_profit_r_format_returns_none(self):
        # Old profit_r/lock_pct format has no gain_pct key → function returns None
        old_tiers = [
            {"profit_r": 1.0, "lock_pct": 0.00},
            {"profit_r": 2.0, "lock_pct": 0.50},
        ]
        result = _graduated_trail_stop(
            entry_price=100.0,
            current_price=110.0,
            current_stop=97.0,
            trail_tiers=old_tiers,
        )
        assert result is None, (
            f"Expected None for legacy profit_r format (signals caller to use legacy path), got {result}"
        )

    def test_none_result_routes_to_legacy_in_maybe_trail_stop(self):
        from exit_manager import maybe_trail_stop

        # Config has old-format tiers → _graduated_trail_stop returns None →
        # routing sets trail_tiers=[] → legacy path fires
        old_tiers = [{"profit_r": 1.0, "lock_pct": 0.00}]
        cfg = {
            "parameters": {"stop_loss_pct_core": 0.03},
            "exit_management": {
                "trail_stop_enabled": True,
                "trail_tiers": old_tiers,
                "trail_trigger_r": 1.0,
                "trail_to_breakeven_plus_pct": 0.005,
            },
        }
        pos = _pos(entry=100.0, current=104.0, unrealized=400.0)
        ei = {"stop_price": 97.0, "stop_order_id": "ord-old", "stop_order_status": "open"}

        alpaca_mock = MagicMock()
        alpaca_mock.replace_order_by_id.return_value = MagicMock()

        result = maybe_trail_stop(pos, alpaca_mock, cfg, exit_info=ei)
        assert result is True, "Expected legacy fallback to fire for old profit_r format tiers"


# ---------------------------------------------------------------------------
# TC-14: new_stop >= current_price → safety cap → current_stop returned
# ---------------------------------------------------------------------------

class TestTC14SafetyCap:
    def test_new_stop_above_current_price_capped(self):
        # Custom tier with very high stop_pct: gain_pct=0.01 (fires at +1%), stop_pct=0.10
        # entry=100, current=101.5 → gain=1.5% → tier fires
        # new_stop = 100 × 1.10 = 110.0 > current=101.5 → safety cap → return current_stop=98
        aggressive_tiers = [{"gain_pct": 0.01, "stop_pct": 0.10}]
        result = _graduated_trail_stop(
            entry_price=100.0,
            current_price=101.5,
            current_stop=98.0,
            trail_tiers=aggressive_tiers,
        )
        assert result == 98.0, (
            f"Expected current_stop=98.0 (safety cap: new_stop≥current_price), got {result}"
        )


# ---------------------------------------------------------------------------
# TC-15: entry_price=0 → returns current_stop immediately
# ---------------------------------------------------------------------------

class TestTC15ZeroEntry:
    def test_zero_entry_price_returns_current_stop(self):
        result = _graduated_trail_stop(
            entry_price=0.0,
            current_price=105.0,
            current_stop=97.0,
            trail_tiers=_TIERS,
        )
        assert result == 97.0, f"Expected current_stop=97.0 for zero entry, got {result}"

    def test_negative_entry_price_returns_current_stop(self):
        result = _graduated_trail_stop(
            entry_price=-10.0,
            current_price=105.0,
            current_stop=97.0,
            trail_tiers=_TIERS,
        )
        assert result == 97.0, f"Expected current_stop=97.0 for negative entry, got {result}"
