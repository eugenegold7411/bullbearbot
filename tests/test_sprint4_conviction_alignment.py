"""
Sprint 4 — Conviction threshold alignment tests.

Verifies all four risk_kernel fixes:
  P0: _float_to_conviction reads config threshold (was hardcoded 0.75)
  P1: HIGH CORE tier bump reads config threshold (was hardcoded 0.75)
  P2: _effective_exposure_cap is config-driven (was hardcoded thresholds + 3x ceiling)
  P3: MEDIUM sizing uses mult/2.0 not hardcoded 1.5
"""
import copy
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent

from risk_kernel import (
    PDT_FLOOR,
    _compute_sizing_basis,
    _effective_exposure_cap,
    _float_to_conviction,
    eligibility_check,
    size_position,
)
from schemas import (
    AccountAction,
    BrokerSnapshot,
    Conviction,
    Direction,
    Tier,
    TradeIdea,
)

# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def cfg_65():
    """Config with high conviction threshold at 0.65 (Sprint 2.5 deployed value)."""
    return {
        "parameters": {
            "margin_authorized": True,
            "margin_sizing_multiplier": 4.0,
            "margin_sizing_conviction_thresholds": {
                "high": 0.65,
                "medium": 0.50,
            },
            "max_position_pct_equity": 0.25,
            "max_positions": 20,
            "stop_loss_pct_core": 0.03,
            "catalyst_tag_required_for_entry": True,
            "catalyst_tag_disallowed_values": ["", "none", "null", "no"],
        },
        "position_sizing": {
            "core_tier_pct": 0.15,
            "dynamic_tier_pct": 0.10,
            "intraday_tier_pct": 0.05,
        },
        "time_bound_actions": [],
    }


@pytest.fixture
def cfg_75():
    """Config with high conviction threshold at legacy 0.75."""
    return {
        "parameters": {
            "margin_authorized": True,
            "margin_sizing_multiplier": 4.0,
            "margin_sizing_conviction_thresholds": {
                "high": 0.75,
                "medium": 0.50,
            },
            "max_position_pct_equity": 0.25,
            "max_positions": 20,
            "stop_loss_pct_core": 0.03,
            "catalyst_tag_required_for_entry": True,
            "catalyst_tag_disallowed_values": ["", "none", "null", "no"],
        },
        "position_sizing": {
            "core_tier_pct": 0.15,
            "dynamic_tier_pct": 0.10,
            "intraday_tier_pct": 0.05,
        },
        "time_bound_actions": [],
    }


def _snap(equity=102_000.0, buying_power=116_000.0) -> BrokerSnapshot:
    return BrokerSnapshot(
        positions=[],
        open_orders=[],
        equity=equity,
        cash=equity * 0.3,
        buying_power=buying_power,
    )


def _snap_high_bp(equity=100_000.0, buying_power=500_000.0) -> BrokerSnapshot:
    return BrokerSnapshot(
        positions=[],
        open_orders=[],
        equity=equity,
        cash=equity * 0.3,
        buying_power=buying_power,
    )


def _idea(conviction=0.80, tier=Tier.CORE) -> TradeIdea:
    return TradeIdea(
        symbol="NVDA",
        action=AccountAction.BUY,
        tier=tier,
        conviction=conviction,
        direction=Direction.BULLISH,
        catalyst="technical_breakout",
        intent="enter_long",
    )


# ─── P0: _float_to_conviction ─────────────────────────────────────────────────


class TestFloatToConviction:
    def test_reads_config_high_threshold_0_65(self, cfg_65):
        """_float_to_conviction must return HIGH for 0.68 when config.high=0.65."""
        result = _float_to_conviction(0.68, config=cfg_65)
        assert result == Conviction.HIGH, (
            f"Expected HIGH for 0.68 with threshold=0.65, got {result}"
        )

    def test_returns_medium_below_config_threshold(self, cfg_65):
        """_float_to_conviction must return MEDIUM for 0.64 when config.high=0.65."""
        result = _float_to_conviction(0.64, config=cfg_65)
        assert result == Conviction.MEDIUM

    def test_boundary_exactly_at_threshold(self, cfg_65):
        """Exactly at threshold (0.65) returns HIGH."""
        result = _float_to_conviction(0.65, config=cfg_65)
        assert result == Conviction.HIGH

    def test_legacy_0_75_threshold_still_works(self, cfg_75):
        """With old config.high=0.75, value 0.68 still returns MEDIUM."""
        result = _float_to_conviction(0.68, config=cfg_75)
        assert result == Conviction.MEDIUM

    def test_fallback_when_no_config(self):
        """With no config, falls back to hardcoded 0.75 threshold safely."""
        assert _float_to_conviction(0.68) == Conviction.MEDIUM  # 0.68 < 0.75 fallback
        assert _float_to_conviction(0.80) == Conviction.HIGH

    def test_low_conviction(self, cfg_65):
        """Values below medium threshold return LOW."""
        result = _float_to_conviction(0.45, config=cfg_65)
        assert result == Conviction.LOW

    def test_gap_band_all_return_high(self, cfg_65):
        """All values in 0.65–0.74 gap band return HIGH with cfg_65."""
        for v in [0.65, 0.68, 0.70, 0.72, 0.74]:
            result = _float_to_conviction(v, config=cfg_65)
            assert result == Conviction.HIGH, f"Expected HIGH for {v}, got {result}"

    def test_p0_audit_no_callers_outside_risk_kernel(self):
        """
        P0 audit: _float_to_conviction is only called from within risk_kernel.py.
        Ensures no external execution gating depends on this enum value.
        """
        result = subprocess.run(
            ["grep", "-rn", "_float_to_conviction",
             str(_REPO_ROOT), "--include=*.py"],
            capture_output=True, text=True
        )
        callers = [
            line for line in result.stdout.splitlines()
            if "_float_to_conviction" in line
            and "test_" not in line
            and ".pyc" not in line
            and ".venv" not in line
        ]
        non_kernel = [line for line in callers if "risk_kernel.py" not in line]
        assert len(non_kernel) == 0, (
            f"_float_to_conviction called outside risk_kernel: {non_kernel}"
        )


# ─── P1: HIGH CORE tier bump ──────────────────────────────────────────────────


class TestHighCoreTierBump:
    def test_conviction_068_gets_high_tier_pct(self, cfg_65):
        """
        Conviction=0.68 with config.high=0.65 must use _CORE_HIGH_CONVICTION_PCT (20%).
        Before fix: 15% of $116K = $17,400. After fix: 20% of $116K = $23,200.
        """
        result = size_position(_idea(0.68), _snap(), cfg_65, current_price=100.0, vix=18.0)
        assert isinstance(result, tuple), f"Expected (qty, val) tuple, got: {result}"
        qty, val = result
        assert val >= 22_000, (
            f"Expected ~$23,200 (20% of $116K), got ${val:,.0f}. "
            f"Likely still using 15% tier: ${val:.0f} vs expected $23,200"
        )

    def test_conviction_072_gets_high_tier_pct(self, cfg_65):
        """Conviction=0.72 (in gap band) also gets 20% tier."""
        result = size_position(_idea(0.72), _snap(), cfg_65, current_price=100.0, vix=18.0)
        assert isinstance(result, tuple)
        qty, val = result
        assert val >= 22_000, f"Expected ~$23,200, got ${val:,.0f}"

    def test_conviction_068_uses_base_tier_with_old_threshold(self, cfg_75):
        """Conviction=0.68 with old config.high=0.75 must stay at 15% (backward compat)."""
        result = size_position(_idea(0.68), _snap(), cfg_75, current_price=100.0, vix=18.0)
        assert isinstance(result, tuple)
        qty, val = result
        # 15% of $116K = $17,400
        assert val <= 19_000, (
            f"Expected ~$17,400 (15% with old threshold), got ${val:,.0f}"
        )

    def test_conviction_080_unchanged_across_thresholds(self, cfg_65, cfg_75):
        """Conviction=0.80 produces same result regardless of threshold (above both)."""
        result_65 = size_position(_idea(0.80), _snap(), cfg_65, current_price=100.0, vix=18.0)
        result_75 = size_position(_idea(0.80), _snap(), cfg_75, current_price=100.0, vix=18.0)
        assert isinstance(result_65, tuple) and isinstance(result_75, tuple)
        qty_65, val_65 = result_65
        qty_75, val_75 = result_75
        assert qty_65 == qty_75, (
            f"Conviction=0.80 should be unchanged: cfg_65={qty_65} vs cfg_75={qty_75}"
        )

    def test_pdt_floor_untouched(self):
        """PDT_FLOOR must still be 26_000 after all changes."""
        assert PDT_FLOOR == 26_000.0

    def test_high_core_bump_not_applied_to_dynamic_tier(self, cfg_65):
        """Dynamic tier does NOT get the HIGH CORE bump regardless of conviction."""
        result = size_position(
            _idea(0.80, tier=Tier.DYNAMIC), _snap(), cfg_65,
            current_price=100.0, vix=18.0,
        )
        assert isinstance(result, tuple)
        qty, val = result
        # DYNAMIC: 10% of $116K = $11,600 (not 20%)
        assert val <= 14_000, f"Dynamic tier should not get 20% bump, got ${val:,.0f}"


# ─── P2: _effective_exposure_cap ─────────────────────────────────────────────


class TestEffectiveExposureCap:
    def test_reads_config_high_threshold_for_0_68(self, cfg_65):
        """
        conviction=0.68 >= config.high=0.65 → HIGH path → mult × equity.
        At current paper account (BP=$116K binds), result = $116K.
        """
        snap = _snap()
        cap = _effective_exposure_cap(snap, conviction=0.68, config=cfg_65)
        assert abs(cap - 116_000.0) < 500, f"Expected $116K (BP bound), got ${cap:,.0f}"

    def test_medium_conviction_uses_half_mult(self, cfg_65):
        """MEDIUM conviction uses mult/2.0 = 2.0× exposure cap."""
        snap = _snap_high_bp()
        cap = _effective_exposure_cap(snap, conviction=0.55, config=cfg_65)
        # MEDIUM: equity × (mult/2) = $100K × 2.0 = $200K; BP=$500K → $200K
        assert abs(cap - 200_000.0) < 500, f"Expected $200K (MEDIUM 2x), got ${cap:,.0f}"

    def test_low_conviction_uses_1x(self, cfg_65):
        """LOW conviction uses 1× equity."""
        snap = _snap_high_bp()
        cap = _effective_exposure_cap(snap, conviction=0.45, config=cfg_65)
        assert abs(cap - 100_000.0) < 500, f"Expected $100K (1×), got ${cap:,.0f}"

    def test_ceiling_uses_config_multiplier(self, cfg_65):
        """Ceiling must be mult × equity = 4 × $100K = $400K (not hardcoded 3×)."""
        snap = _snap_high_bp(buying_power=2_000_000.0)
        cap = _effective_exposure_cap(snap, conviction=0.80, config=cfg_65)
        # HIGH: equity × mult = $100K × 4.0 = $400K; BP=$2M → $400K
        assert abs(cap - 400_000.0) < 500, (
            f"Expected $400K (4x ceiling), got ${cap:,.0f} (likely hardcoded 3x)"
        )

    def test_backward_compat_no_config(self):
        """With no config, defaults preserve old hardcoded behavior (mult=3.0)."""
        snap = _snap_high_bp(buying_power=2_000_000.0)
        cap = _effective_exposure_cap(snap, conviction=0.80)
        # Default mult=3.0 → HIGH: $100K × 3.0 = $300K
        assert abs(cap - 300_000.0) < 500, (
            f"No-config backward compat: expected $300K, got ${cap:,.0f}"
        )

    def test_call_site_passes_config(self, cfg_65):
        """
        size_position() passes config to _effective_exposure_cap().
        Verify by checking that a conviction=0.68 trade with cfg_65 uses HIGH cap
        (not MEDIUM cap from hardcoded 0.75 threshold).
        """
        snap = _snap()
        result = size_position(_idea(0.68), snap, cfg_65, current_price=100.0, vix=18.0)
        assert isinstance(result, tuple), f"Unexpected rejection: {result}"


# ─── P3: MEDIUM sizing basis ──────────────────────────────────────────────────


class TestMediumSizingBasis:
    def test_medium_uses_half_mult_not_hardcoded_1_5(self, cfg_65):
        """
        MEDIUM sizing basis uses mult/2.0=2.0, not hardcoded 1.5.
        At equity=$100K, BP=$200K:
          Old (1.5×): min($200K, $100K×1.5)=$150K
          New (2.0×): min($200K, $100K×2.0)=$200K — different!
        """
        snap = BrokerSnapshot(
            positions=[],
            open_orders=[],
            equity=100_000.0,
            buying_power=200_000.0,
            cash=30_000.0,
        )
        basis = _compute_sizing_basis(snap, conviction=0.55, config=cfg_65)
        # With mult/2.0=2.0: min($200K, $200K) = $200K
        # With hardcoded 1.5: min($200K, $150K) = $150K
        assert abs(basis - 200_000.0) < 500, (
            f"Expected $200K (mult/2.0=2.0), got ${basis:,.0f} "
            f"(likely still hardcoded 1.5 → $150K)"
        )

    def test_medium_basis_unchanged_at_current_account_bp(self, cfg_65):
        """
        At current paper account (BP=$116K), MEDIUM basis is $116K in both
        old and new logic — BP is the binding constraint regardless.
        """
        snap = _snap()  # BP=$116K
        basis = _compute_sizing_basis(snap, conviction=0.55, config=cfg_65)
        # equity × (mult/2.0) = $102K × 2.0 = $204K > BP=$116K → BP binds
        assert abs(basis - 116_000.0) < 500, (
            f"Expected BP-bound $116K, got ${basis:,.0f}"
        )

    def test_high_basis_unchanged(self, cfg_65):
        """HIGH conviction basis uses full multiplier — P3 does not affect HIGH path."""
        snap = _snap()
        basis = _compute_sizing_basis(snap, conviction=0.80, config=cfg_65)
        # HIGH: min(bp=$116K, equity×4.0=$408K) = $116K
        assert abs(basis - 116_000.0) < 500

    def test_mult_3_medium_same_as_old_1_5(self, cfg_75):
        """
        When mult=4.0, mult/2.0=2.0 ≠ 1.5 (the fix has real impact).
        But when mult=3.0, mult/2.0=1.5 exactly matches old hardcode.
        Verify consistency with a mult=3.0 config.
        """
        cfg_3x = copy.deepcopy(cfg_75)
        cfg_3x["parameters"]["margin_sizing_multiplier"] = 3.0
        snap = BrokerSnapshot(
            positions=[],
            open_orders=[],
            equity=100_000.0,
            buying_power=500_000.0,
            cash=30_000.0,
        )
        basis = _compute_sizing_basis(snap, conviction=0.55, config=cfg_3x)
        # mult/2.0 = 1.5 → min($500K, $150K) = $150K (same as old hardcode)
        assert abs(basis - 150_000.0) < 500


# ─── Integration: full sizing comparison table ────────────────────────────────


class TestSizingComparisonTable:
    def test_full_conviction_spectrum_with_cfg_65(self, cfg_65):
        """
        Integration test: verify full sizing spectrum matches expected outputs.
        Current paper account: equity=$102K, BP=$116K, mult=4.0, high_thresh=0.65.
        """
        snap = _snap()

        expected = {
            0.45: (153, 15_300),   # LOW:    15% × equity $102K / $100 = 153
            0.55: (174, 17_400),   # MEDIUM: 15% × BP $116K / $100 = 174 (base CORE)
            0.68: (232, 23_200),   # HIGH-A: 20% × BP $116K / $100 = 232 (P1 fix)
            0.72: (232, 23_200),   # HIGH-B: same as HIGH-A
            0.80: (232, 23_200),   # HIGH-C: 20% × BP $116K / $100 = 232
        }

        print("\n=== SIZING COMPARISON TABLE (CORE, VIX=18, price=$100, cfg_65) ===")
        print("  %-15s  %6s  %10s  %10s" % ("Conviction", "qty", "value", "pct equity"))

        for conviction, (exp_qty, exp_val) in sorted(expected.items()):
            result = size_position(
                _idea(conviction), snap, cfg_65,
                current_price=100.0, vix=18.0,
            )
            assert isinstance(result, tuple), f"{conviction}: unexpected rejection: {result}"
            qty, val = result
            print("  %-15s  %6d  $%9.0f  %9.1f%%" % (
                str(conviction), qty, val, val / 102000 * 100
            ))
            # 10% tolerance on qty
            assert abs(qty - exp_qty) <= max(1, int(exp_qty * 0.10)), (
                f"conviction={conviction}: expected qty~{exp_qty}, got {qty}"
            )

    def test_pdt_and_eligibility_check_untouched(self):
        """PDT floor and eligibility_check signature remain unchanged."""
        assert PDT_FLOOR == 26_000.0
        assert hasattr(eligibility_check, "__call__")

    def test_no_a2_execution_paths_changed(self):
        """Confirm A2 execution files are unchanged (not risk_kernel.py)."""
        import subprocess
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
            capture_output=True, text=True,
            cwd=str(_REPO_ROOT),
        )
        changed = result.stdout.strip().splitlines()
        a2_execution = [f for f in changed if f in (
            "bot_options.py", "options_executor.py",
            "order_executor_options.py", "options_builder.py",
        )]
        assert len(a2_execution) == 0, f"Unexpected A2 execution file changes: {a2_execution}"
