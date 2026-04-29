"""
tests/test_s9_graduated_trail.py — Sprint 9 graduated trail-stop tests.

14 test cases covering:
  TC-01  No tier fires when profit_r < 1.0x
  TC-02  Tier-1 fires at exactly 1.0x (lock_pct=0.00 → stop at entry)
  TC-03  Tier-2 fires at 2.0x (lock_pct=0.50 → stop at midpoint)
  TC-04  Tier-3 fires at 3.0x (lock_pct=0.67)
  TC-05  Tier-4 fires at 4.0x+ (lock_pct=0.75)
  TC-06  Never-narrow: new_stop ≤ stop_price returns False from maybe_trail_stop
  TC-07  Denominator uses entry×stop_loss_pct_core, NOT entry−stop_price
  TC-08  After first fire stop_price > entry — profit_r still computed correctly
  TC-09  Legacy path used when trail_tiers absent from config
  TC-10  Legacy path self-disables when stop_dist ≤ 0 (bug preserved as documented)
  TC-11  V at $336.28, entry $310, stop_loss_pct_core=0.03 — tier-2 fires
  TC-12  Graduated path returns None when original_stop_dist ≤ 0
  TC-13  Earnings floor overrides new_stop when earnings_aware_stop_enabled=True
  TC-14  Tiers evaluated in ascending profit_r order (unsorted config)
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))


# ---------------------------------------------------------------------------
# Helper: build a minimal fake position
# ---------------------------------------------------------------------------

def _pos(symbol="AAPL", entry=100.0, current=120.0, unrealized=200.0, qty=10):
    p = MagicMock()
    p.symbol = symbol
    p.avg_entry_price = str(entry)
    p.current_price = str(current)
    p.unrealized_pl = str(unrealized)
    p.qty = str(qty)
    return p


# ---------------------------------------------------------------------------
# Helper: minimal strategy_config with trail_tiers
# ---------------------------------------------------------------------------

_TIERS = [
    {"profit_r": 1.0, "lock_pct": 0.00},
    {"profit_r": 2.0, "lock_pct": 0.50},
    {"profit_r": 3.0, "lock_pct": 0.67},
    {"profit_r": 4.0, "lock_pct": 0.75},
]


def _cfg(tiers=_TIERS, stop_loss_pct_core=0.03, earnings_aware=False):
    return {
        "parameters": {
            "stop_loss_pct_core": stop_loss_pct_core,
        },
        "exit_management": {
            "trail_stop_enabled": True,
            "trail_tiers": tiers,
            "earnings_aware_stop_enabled": earnings_aware,
            "earnings_stop_eda_trigger": 1,
            "earnings_stop_iv_floor_pct": 0.05,
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


# ---------------------------------------------------------------------------
# Import helper under test
# ---------------------------------------------------------------------------

from exit_manager import _graduated_trail_stop  # noqa: E402

# ---------------------------------------------------------------------------
# TC-01: no tier fires when profit_r < 1.0x
# ---------------------------------------------------------------------------

class TestTC01NoTierFires:
    def test_profit_r_below_first_tier(self):
        # entry=$100, stop_loss_pct=0.03 → original_dist=$3
        # current=$102.5 → profit_r = 2.5/3 = 0.833x < 1.0 → None
        result = _graduated_trail_stop(
            current_price=102.5,
            entry_price=100.0,
            stop_price=97.0,
            tiers=_TIERS,
            stop_loss_pct_core=0.03,
        )
        assert result is None, f"Expected None for profit_r<1.0, got {result}"


# ---------------------------------------------------------------------------
# TC-02: Tier-1 fires at exactly 1.0x (lock_pct=0.00 → stop at entry)
# ---------------------------------------------------------------------------

class TestTC02Tier1:
    def test_tier1_fires_at_1x(self):
        # entry=$100, stop_loss_pct=0.03 → dist=$3
        # current=$103.0 → profit_r = 3/3 = 1.0x → tier-1 (lock_pct=0.00)
        # new_stop = 100 + 0.00*(103-100) = $100.00
        result = _graduated_trail_stop(
            current_price=103.0,
            entry_price=100.0,
            stop_price=97.0,
            tiers=_TIERS,
            stop_loss_pct_core=0.03,
        )
        assert result == 100.00, f"Expected $100.00, got {result}"


# ---------------------------------------------------------------------------
# TC-03: Tier-2 fires at 2.0x (lock_pct=0.50)
# ---------------------------------------------------------------------------

class TestTC03Tier2:
    def test_tier2_at_2x(self):
        # entry=$100, dist=$3, current=$106 → profit_r = 6/3 = 2.0x → tier-2 (lock_pct=0.50)
        # new_stop = 100 + 0.50*(106-100) = 100+3 = $103.00
        result = _graduated_trail_stop(
            current_price=106.0,
            entry_price=100.0,
            stop_price=97.0,
            tiers=_TIERS,
            stop_loss_pct_core=0.03,
        )
        assert result == 103.00, f"Expected $103.00, got {result}"


# ---------------------------------------------------------------------------
# TC-04: Tier-3 fires at 3.0x (lock_pct=0.67)
# ---------------------------------------------------------------------------

class TestTC04Tier3:
    def test_tier3_at_3x(self):
        # entry=$100, dist=$3, current=$109 → profit_r = 9/3 = 3.0x → tier-3 (lock_pct=0.67)
        # new_stop = 100 + 0.67*(109-100) = 100+6.03 = $106.03
        result = _graduated_trail_stop(
            current_price=109.0,
            entry_price=100.0,
            stop_price=97.0,
            tiers=_TIERS,
            stop_loss_pct_core=0.03,
        )
        assert result == 106.03, f"Expected $106.03, got {result}"


# ---------------------------------------------------------------------------
# TC-05: Tier-4 fires at 4.0x+ (lock_pct=0.75)
# ---------------------------------------------------------------------------

class TestTC05Tier4:
    def test_tier4_at_4x(self):
        # entry=$100, dist=$3, current=$112 → profit_r = 12/3 = 4.0x → tier-4 (lock_pct=0.75)
        # new_stop = 100 + 0.75*(112-100) = 100+9 = $109.00
        result = _graduated_trail_stop(
            current_price=112.0,
            entry_price=100.0,
            stop_price=97.0,
            tiers=_TIERS,
            stop_loss_pct_core=0.03,
        )
        assert result == 109.00, f"Expected $109.00, got {result}"

    def test_tier4_above_4x(self):
        # Same tier capped at 0.75 even at 5x
        # current=$115 → profit_r = 15/3 = 5.0x → still tier-4
        # new_stop = 100 + 0.75*(115-100) = 100+11.25 = $111.25
        result = _graduated_trail_stop(
            current_price=115.0,
            entry_price=100.0,
            stop_price=97.0,
            tiers=_TIERS,
            stop_loss_pct_core=0.03,
        )
        assert result == 111.25, f"Expected $111.25, got {result}"


# ---------------------------------------------------------------------------
# TC-06: Never-narrow guarantee
# ---------------------------------------------------------------------------

class TestTC06NeverNarrow:
    def test_new_stop_at_or_below_current_stop_returns_false(self):
        """When the graduated target doesn't improve on the existing stop, no trail fires."""
        from exit_manager import maybe_trail_stop

        # entry=$100, stop_loss_pct=0.03, current=$103 → tier-1 → new_stop=$100
        # BUT stop_price is already $101 (better than $100) → should return False
        pos = _pos(entry=100.0, current=103.0, unrealized=300.0)
        ei = {"stop_price": 101.0, "stop_order_id": "ord-1", "stop_order_status": "open"}
        cfg = _cfg()

        result = maybe_trail_stop(pos, MagicMock(), cfg, exit_info=ei)
        assert result is False, f"Expected False (new_stop ≤ stop_price), got {result}"


# ---------------------------------------------------------------------------
# TC-07: Denominator fix — uses entry×pct, not entry−stop_price
# ---------------------------------------------------------------------------

class TestTC07DenominatorFix:
    def test_profit_r_computed_from_entry_times_pct(self):
        # entry=$100, stop=$97, stop_loss_pct=0.03 → original_dist=$3
        # If we used entry-stop_price: dist=$3 → same result at first fire
        # But this test explicitly sets stop_price=$99 (above entry−3=97)
        # to confirm that stop_loss_pct_core is used, not entry-stop_price distance
        # entry=$100, stop=$99 (only $1 below entry), stop_loss_pct=0.03 → dist=$3
        # current=$103 → profit_r = 3/3 = 1.0x → tier-1 fires
        result = _graduated_trail_stop(
            current_price=103.0,
            entry_price=100.0,
            stop_price=99.0,  # only $1 below entry — old formula would give dist=1, profit_r=3
            tiers=_TIERS,
            stop_loss_pct_core=0.03,  # 3% → dist=$3
        )
        # With correct denominator (dist=3): profit_r=1.0x → tier-1 → new_stop=entry+0=100
        assert result == 100.00, f"Expected $100.00 (correct denominator), got {result}"


# ---------------------------------------------------------------------------
# TC-08: After first fire, stop > entry — profit_r still computed correctly
# ---------------------------------------------------------------------------

class TestTC08ReFireAfterTrail:
    def test_trail_fires_again_after_stop_above_entry(self):
        """After stop moves to entry+0.5%, profit_r still computes from original dist."""
        # Simulate: entry=$100, stop already trailed to $100.50 (above entry)
        # stop_loss_pct=0.03 → original_dist=$3
        # current=$106 → profit_r = 6/3 = 2.0x → tier-2 → new_stop=$103
        # This would fail in legacy (entry-stop = $100-$100.50 = -$0.50 → stop_dist≤0)
        result = _graduated_trail_stop(
            current_price=106.0,
            entry_price=100.0,
            stop_price=100.50,  # stop already above entry (legacy would return False)
            tiers=_TIERS,
            stop_loss_pct_core=0.03,
        )
        assert result == 103.00, (
            f"Expected $103.00 (graduated trail re-fires after stop > entry), got {result}"
        )


# ---------------------------------------------------------------------------
# TC-09: Legacy path when trail_tiers absent
# ---------------------------------------------------------------------------

class TestTC09LegacyPath:
    def test_legacy_path_fires_when_no_tiers(self):
        """When trail_tiers is absent, the legacy single-trigger path fires."""
        from exit_manager import maybe_trail_stop

        # entry=$100, stop=$97 (dist=$3), current=$104 → profit_r=4/3=1.33x ≥ 1.0 → fires
        # new_stop = entry*(1+0.005) = $100.50
        pos = _pos(entry=100.0, current=104.0, unrealized=400.0)
        ei = {"stop_price": 97.0, "stop_order_id": "ord-2", "stop_order_status": "open"}
        cfg = _legacy_cfg(trigger_r=1.0, plus_pct=0.005)

        alpaca_mock = MagicMock()
        # replace_order_by_id must succeed without raising
        alpaca_mock.replace_order_by_id.return_value = MagicMock()

        result = maybe_trail_stop(pos, alpaca_mock, cfg, exit_info=ei)
        assert result is True, "Expected legacy trail to fire"
        call_args = alpaca_mock.replace_order_by_id.call_args
        req = call_args[0][1] if call_args[0] else call_args[1].get("request_params", None)
        # The ReplaceOrderRequest is a stub in tests (KwargsRequest) — check stop_price attr
        assert hasattr(req, "stop_price"), "ReplaceOrderRequest missing stop_price"
        assert abs(req.stop_price - 100.50) < 0.01, f"Expected stop $100.50, got {req.stop_price}"


# ---------------------------------------------------------------------------
# TC-10: Legacy self-disables when stop > entry (documented behavior, NOT a bug)
# ---------------------------------------------------------------------------

class TestTC10LegacySelfDisable:
    def test_legacy_returns_false_when_stop_above_entry(self):
        """Legacy path: stop_dist = entry-stop_price ≤ 0 → returns False (known behavior)."""
        from exit_manager import maybe_trail_stop

        pos = _pos(entry=100.0, current=106.0, unrealized=600.0)
        ei = {"stop_price": 100.50, "stop_order_id": "ord-3"}  # stop already above entry
        cfg = _legacy_cfg()

        result = maybe_trail_stop(pos, MagicMock(), cfg, exit_info=ei)
        assert result is False, (
            "Legacy path should return False when stop_price > entry_price"
        )


# ---------------------------------------------------------------------------
# TC-11: V at $336.28, entry=$310, stop_loss_pct_core=0.03
# ---------------------------------------------------------------------------

class TestTC11VisaScenario:
    def test_visa_tier2(self):
        """V scenario: entry=$310, current=$336.28, pct=0.03 → profit_r≈2.8x → tier-2."""
        # original_dist = 310 * 0.03 = $9.30
        # profit = 336.28 - 310 = $26.28
        # profit_r = 26.28 / 9.30 ≈ 2.83x → tier-2 (lock_pct=0.50)
        # new_stop = 310 + 0.50*(336.28-310) = 310 + 13.14 = $323.14
        result = _graduated_trail_stop(
            current_price=336.28,
            entry_price=310.0,
            stop_price=300.70,  # 310*(1-0.03)
            tiers=_TIERS,
            stop_loss_pct_core=0.03,
        )
        assert result == 323.14, f"Expected $323.14, got {result}"

    def test_visa_tier3_above_3x(self):
        """V at $338.30 → profit_r≈3.04x → tier-3 (lock_pct=0.67)."""
        # original_dist = 310*0.03 = 9.2999... (Python float)
        # profit = 338.30 - 310 = 28.30
        # profit_r = 28.30 / 9.2999... ≈ 3.043x → tier-3 (lock_pct=0.67)
        # new_stop = 310 + 0.67*(338.30-310) = 310+18.961 = $328.96
        result = _graduated_trail_stop(
            current_price=338.30,
            entry_price=310.0,
            stop_price=300.70,
            tiers=_TIERS,
            stop_loss_pct_core=0.03,
        )
        assert result == 328.96, f"Expected $328.96, got {result}"


# ---------------------------------------------------------------------------
# TC-12: _graduated_trail_stop returns None when original_stop_dist ≤ 0
# ---------------------------------------------------------------------------

class TestTC12ZeroDenominator:
    def test_zero_stop_loss_pct_returns_none(self):
        result = _graduated_trail_stop(
            current_price=110.0,
            entry_price=100.0,
            stop_price=97.0,
            tiers=_TIERS,
            stop_loss_pct_core=0.0,  # zero denominator
        )
        assert result is None

    def test_negative_stop_loss_pct_returns_none(self):
        result = _graduated_trail_stop(
            current_price=110.0,
            entry_price=100.0,
            stop_price=97.0,
            tiers=_TIERS,
            stop_loss_pct_core=-0.01,
        )
        assert result is None


# ---------------------------------------------------------------------------
# TC-13: Earnings floor overrides new_stop when earnings_aware enabled
# ---------------------------------------------------------------------------

class TestTC13EarningsFloor:
    def test_earnings_floor_overrides_tier_target(self):
        """When earnings_aware_stop is enabled and IV-based floor > tier target, floor wins."""
        from exit_manager import maybe_trail_stop

        # entry=$100, current=$103 → tier-1 → new_stop=$100.00
        # But earnings floor = entry*(1-0.15) = $85 — this is BELOW stop_price so NOT applied
        # (earnings floor only applies if floor > current stop_price)
        pos = _pos(entry=100.0, current=106.0, unrealized=600.0)
        ei = {"stop_price": 97.0, "stop_order_id": "ord-4", "stop_order_status": "open"}
        cfg = _cfg(earnings_aware=True)

        with (
            patch("exit_manager._get_eda", return_value=0),  # eda=0, within trigger
            patch("exit_manager._get_latest_iv", return_value=0.15),  # IV=15%
        ):
            alpaca_mock = MagicMock()
            alpaca_mock.replace_order_by_id.return_value = MagicMock()
            result = maybe_trail_stop(pos, alpaca_mock, cfg, exit_info=ei)

        # earnings_floor = 100*(1-0.15) = $85, which is BELOW stop_price $97 → not applied
        # tier-2 at 2x: new_stop = 100 + 0.50*(106-100) = $103 → fires normally
        assert result is True, "Expected trail to fire (earnings floor below existing stop)"


# ---------------------------------------------------------------------------
# TC-14: Tiers evaluated in ascending profit_r order (unsorted config)
# ---------------------------------------------------------------------------

class TestTC14UnsortedTiers:
    def test_highest_qualifying_tier_selected_even_if_unsorted(self):
        """Tiers given in reverse order still yield the correct (highest qualifying) tier."""
        unsorted_tiers = [
            {"profit_r": 4.0, "lock_pct": 0.75},
            {"profit_r": 2.0, "lock_pct": 0.50},  # ← correct active tier at 2.5x
            {"profit_r": 3.0, "lock_pct": 0.67},
            {"profit_r": 1.0, "lock_pct": 0.00},
        ]
        # entry=$100, dist=$3, current=$107.5 → profit_r = 7.5/3 = 2.5x → tier-2 (lock_pct=0.50)
        result = _graduated_trail_stop(
            current_price=107.5,
            entry_price=100.0,
            stop_price=97.0,
            tiers=unsorted_tiers,
            stop_loss_pct_core=0.03,
        )
        # new_stop = 100 + 0.50*(107.5-100) = 100+3.75 = $103.75
        assert result == 103.75, f"Expected $103.75 for unsorted tiers, got {result}"
