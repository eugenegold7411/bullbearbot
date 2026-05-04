"""
test_risk_kernel_size_position.py — Tests for risk_kernel.size_position().

Covers:
  - Tier percentages (core 15%, dynamic 8%, intraday 5%)
  - High-conviction core bump to 20%
  - VIX scaling (50% reduction at VIX >= 25)
  - Exposure headroom capping
  - No-headroom rejection
  - Crypto fractional qty vs stock integer qty

All tests are offline-safe.
"""


from risk_kernel import VIX_CAUTION, size_position
from schemas import (
    AccountAction,
    BrokerSnapshot,
    Direction,
    NormalizedPosition,
    Tier,
    TradeIdea,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _snapshot(
    equity: float = 100_000.0,
    extra_exposure: float = 0.0,
) -> BrokerSnapshot:
    """Clean snapshot with optional pre-existing exposure via a fake position."""
    positions = []
    if extra_exposure > 0:
        positions.append(NormalizedPosition(
            symbol="SPY", alpaca_sym="SPY",
            qty=extra_exposure / 100.0,
            avg_entry_price=100.0, current_price=100.0,
            market_value=extra_exposure,
            unrealized_pl=0.0, unrealized_plpc=0.0,
            is_crypto_pos=False,
        ))
    return BrokerSnapshot(
        positions=positions,
        open_orders=[],
        equity=equity,
        cash=equity - extra_exposure,
        buying_power=equity,
    )


def _idea(
    symbol: str = "AAPL",
    tier: Tier = Tier.CORE,
    conviction: float = 0.80,
) -> TradeIdea:
    return TradeIdea(
        symbol=symbol,
        action=AccountAction.BUY,
        tier=tier,
        conviction=conviction,
        direction=Direction.BULLISH,
        catalyst="technical_breakout",
        intent="enter_long",
    )


# ── Tier sizing percentages ───────────────────────────────────────────────────

class TestTierSizing:
    def test_core_tier_allocates_15pct(self, kernel_config):
        # conviction=0.60 (MEDIUM) stays at 15%, not the 20% high-conv bump
        qty, value = size_position(
            _idea(tier=Tier.CORE, conviction=0.60), _snapshot(), kernel_config,
            current_price=100.0,
        )
        # 15% of 100K = 15,000; at $100 → 150 shares
        assert qty == 150
        assert abs(value - 15_000.0) < 1.0

    def test_dynamic_tier_allocates_8pct(self, kernel_config):
        qty, value = size_position(
            _idea(tier=Tier.DYNAMIC, conviction=0.60), _snapshot(), kernel_config,
            current_price=100.0,
        )
        # 8% of 100K = 8,000; at $100 → 80 shares
        assert qty == 80
        assert abs(value - 8_000.0) < 1.0

    def test_intraday_tier_allocates_5pct(self, kernel_config):
        qty, value = size_position(
            _idea(tier=Tier.INTRADAY, conviction=0.60), _snapshot(), kernel_config,
            current_price=100.0,
        )
        # 5% of 100K = 5,000; at $100 → 50 shares
        assert qty == 50
        assert abs(value - 5_000.0) < 1.0

    def test_high_conviction_core_bumps_to_25pct(self, kernel_config):
        # conviction >= 0.75 AND tier == CORE → _CORE_HIGH_CONVICTION_PCT = 25%
        qty, value = size_position(
            _idea(tier=Tier.CORE, conviction=0.80), _snapshot(), kernel_config,
            current_price=100.0,
        )
        # 25% of 100K = 25,000; at $100 → 250 shares
        assert qty == 250
        assert abs(value - 25_000.0) < 1.0

    def test_medium_conviction_core_stays_at_15pct(self, kernel_config):
        # conviction=0.60 is MEDIUM — no high-conv bump
        qty, value = size_position(
            _idea(tier=Tier.CORE, conviction=0.60), _snapshot(), kernel_config,
            current_price=100.0,
        )
        assert abs(value - 15_000.0) < 1.0

    def test_conviction_boundary_74pct_stays_at_15pct(self, kernel_config):
        # conviction=0.74 is just below the 0.75 HIGH threshold
        qty, value = size_position(
            _idea(tier=Tier.CORE, conviction=0.74), _snapshot(), kernel_config,
            current_price=100.0,
        )
        assert abs(value - 15_000.0) < 1.0


# ── VIX scaling ───────────────────────────────────────────────────────────────

class TestVIXScaling:
    def test_vix_at_caution_halves_position(self, kernel_config):
        # VIX >= 25 → size_mult = 0.5
        qty_normal, _ = size_position(
            _idea(conviction=0.60), _snapshot(), kernel_config,
            current_price=100.0, vix=20.0,
        )
        qty_scaled, _ = size_position(
            _idea(conviction=0.60), _snapshot(), kernel_config,
            current_price=100.0, vix=VIX_CAUTION,
        )
        # Scaled should be roughly half
        assert qty_scaled == qty_normal // 2

    def test_vix_below_caution_no_scaling(self, kernel_config):
        qty_20, _ = size_position(
            _idea(conviction=0.60), _snapshot(), kernel_config,
            current_price=100.0, vix=20.0,
        )
        qty_24, _ = size_position(
            _idea(conviction=0.60), _snapshot(), kernel_config,
            current_price=100.0, vix=24.9,
        )
        assert qty_20 == qty_24

    def test_vix_above_caution_halves_position(self, kernel_config):
        qty_normal, _ = size_position(
            _idea(conviction=0.60), _snapshot(), kernel_config,
            current_price=100.0, vix=20.0,
        )
        qty_high_vix, _ = size_position(
            _idea(conviction=0.60), _snapshot(), kernel_config,
            current_price=100.0, vix=30.0,
        )
        assert qty_high_vix < qty_normal


# ── Exposure headroom ─────────────────────────────────────────────────────────

class TestExposureHeadroom:
    def test_headroom_caps_position_below_tier_budget(self, kernel_config):
        # MEDIUM conviction (0.60): effective cap = equity × (mult/2) = 100K × 1.5 = 150K
        # With existing exposure=140K → headroom=10K < CORE budget=15K
        # → capped to 10K → 100 shares at $100
        snap = _snapshot(equity=100_000.0, extra_exposure=140_000.0)
        result = size_position(
            _idea(conviction=0.60), snap, kernel_config, current_price=100.0,
        )
        assert isinstance(result, tuple)
        qty, value = result
        assert value <= 11_000.0  # well below the uncapped 15K budget

    def test_no_headroom_returns_rejection_string(self, kernel_config):
        # LOW conviction (0.30): effective cap = equity * 1.0 = 100K
        # Fill exposure to 100K → headroom = 0 → rejection
        snap = _snapshot(equity=100_000.0, extra_exposure=100_000.0)
        result = size_position(
            _idea(conviction=0.30), snap, kernel_config, current_price=100.0,
        )
        assert isinstance(result, str)
        assert "headroom" in result.lower()


# ── Qty rounding ──────────────────────────────────────────────────────────────

class TestQtyRounding:
    def test_stock_qty_is_integer(self, kernel_config):
        qty, _ = size_position(
            _idea(symbol="AAPL", tier=Tier.CORE, conviction=0.60), _snapshot(),
            kernel_config, current_price=150.0,
        )
        assert qty == int(qty)
        assert qty >= 1

    def test_crypto_qty_is_fractional(self, kernel_config):
        # 15% of 100K = 15K budget; at $9,999 → raw_qty ≈ 1.5002 → round(, 6)
        # Budget ($15K) > price ($9,999): passes the budget-check gate.
        # Crypto branch returns round(raw_qty, 6) giving fractional precision.
        qty, _ = size_position(
            _idea(symbol="BTC/USD", tier=Tier.CORE, conviction=0.60), _snapshot(),
            kernel_config, current_price=9_999.0,
        )
        assert isinstance(qty, float)
        assert qty != int(qty)  # has decimal precision (e.g. 1.500150...)

    def test_zero_price_returns_rejection_string(self, kernel_config):
        result = size_position(
            _idea(), _snapshot(), kernel_config, current_price=0.0,
        )
        assert isinstance(result, str)


# ── BP-aware margin sizing (Thing 1) ──────────────────────────────────────────

import copy

from risk_kernel import _effective_exposure_cap


def _margin_snapshot(
    equity: float = 100_000.0,
    buying_power: float = 300_000.0,
    extra_exposure: float = 0.0,
) -> BrokerSnapshot:
    """Snapshot simulating a 3x margin account: BP > equity."""
    positions = []
    if extra_exposure > 0:
        positions.append(NormalizedPosition(
            symbol="SPY", alpaca_sym="SPY",
            qty=extra_exposure / 100.0,
            avg_entry_price=100.0, current_price=100.0,
            market_value=extra_exposure,
            unrealized_pl=0.0, unrealized_plpc=0.0,
            is_crypto_pos=False,
        ))
    return BrokerSnapshot(
        positions=positions,
        open_orders=[],
        equity=equity,
        cash=max(0.0, equity - extra_exposure),
        buying_power=buying_power,
    )


def _margin_config(kernel_config: dict, *, margin_authorized: bool = True,
                   multiplier: float = 3.0) -> dict:
    """Deep-copy kernel_config and inject Thing 1 margin parameters."""
    cfg = copy.deepcopy(kernel_config)
    cfg["parameters"]["margin_authorized"] = margin_authorized
    cfg["parameters"]["margin_sizing_multiplier"] = multiplier
    cfg["parameters"]["margin_sizing_conviction_thresholds"] = {
        "high": 0.75, "medium": 0.50,
    }
    return cfg


class TestBPAwareSizing:
    def test_high_conviction_margin_3x_basis(self, kernel_config):
        # HIGH (0.80) + margin_authorized + 3.0 multiplier
        # sizing_basis = min(BP=300K, equity*3=300K) = 300K
        # core HIGH bump 25% → 75K; at $100 → 750 shares
        cfg = _margin_config(kernel_config, multiplier=3.0)
        qty, value = size_position(
            _idea(tier=Tier.CORE, conviction=0.80), _margin_snapshot(),
            cfg, current_price=100.0,
        )
        assert abs(value - 75_000.0) < 1.0
        assert qty == 750

    def test_medium_conviction_margin_capped_at_15x(self, kernel_config):
        # MEDIUM (0.60): sizing_basis = min(BP=300K, equity * min(3, 1.5)=150K) = 150K
        # core 15% → 22,500
        cfg = _margin_config(kernel_config, multiplier=3.0)
        qty, value = size_position(
            _idea(tier=Tier.CORE, conviction=0.60), _margin_snapshot(),
            cfg, current_price=100.0,
        )
        assert abs(value - 22_500.0) < 1.0

    def test_low_conviction_no_margin(self, kernel_config):
        # LOW (0.30): sizing_basis = equity = 100K
        # core 15% → 15,000 (same as cash account)
        cfg = _margin_config(kernel_config, multiplier=3.0)
        qty, value = size_position(
            _idea(tier=Tier.CORE, conviction=0.30), _margin_snapshot(),
            cfg, current_price=100.0,
        )
        assert abs(value - 15_000.0) < 1.0

    def test_margin_authorized_false_disables_basis(self, kernel_config):
        # margin_authorized=False, HIGH conviction:
        # sizing_basis = equity → core HIGH bump (25%) = 25,000
        cfg = _margin_config(kernel_config, margin_authorized=False, multiplier=3.0)
        qty, value = size_position(
            _idea(tier=Tier.CORE, conviction=0.80), _margin_snapshot(),
            cfg, current_price=100.0,
        )
        assert abs(value - 25_000.0) < 1.0


class TestBPAwareEligibility:
    def test_high_conviction_existing_position_passes(self, kernel_config):
        # Eligibility ceiling moved out of eligibility_check by sprint refactor.
        # This test confirms eligibility now passes for any non-PDT/non-VIX/non-session
        # condition — a HIGH conviction BUY with no blocking conditions returns None.
        from risk_kernel import eligibility_check
        cfg = _margin_config(kernel_config, multiplier=3.0)
        snap = _margin_snapshot()
        idea = _idea(symbol="AAPL", tier=Tier.CORE, conviction=0.80)
        result = eligibility_check(idea, snap, cfg, session_tier="market", vix=20.0)
        assert result is None, f"expected pass, got rejection: {result}"


class TestBPAwareExposureHeadroom:
    def test_high_conv_3x_cap_with_180k_existing_exposure(self, kernel_config):
        # HIGH conviction (0.80), equity=100K, BP=300K, pre-existing 180K exposure.
        # Old aggregate cap was equity*2=200K → headroom $20K.
        # New hard ceiling: equity*3=300K. Conviction ladder still gives 2.0× for HIGH
        # but the OUTER cap is min(2.0×equity, 3.0×equity_ceiling, bp) = 200K.
        # Headroom = 200K - 180K = 20K > 0.
        snap = _margin_snapshot(extra_exposure=180_000.0)
        cap = _effective_exposure_cap(snap, conviction=0.80)
        # Cap should be at least 200K (HIGH gives 2.0× equity), bound by min(equity*3=300K, bp=300K)
        assert cap >= 200_000.0
        assert cap - 180_000.0 > 0


class TestBPAwareDynamicSizesAllocator:
    def test_compute_dynamic_sizes_exposes_bp_aware_caps(self):
        """
        compute_dynamic_sizes() should expose conviction-tiered caps so Sonnet
        sees BP-aware sizing. With margin_authorized=true and multiplier=3.0,
        a HIGH-conviction core slot should be 25% of min(bp, equity*3).
        """
        from portfolio_intelligence import compute_dynamic_sizes
        cfg = {
            "position_sizing": {
                "core_tier_pct":         0.15,
                "dynamic_tier_pct":      0.08,
                "max_total_exposure_pct": 0.30,
                "cash_reserve_pct":      0.10,
            },
            "parameters": {
                "margin_authorized":         True,
                "margin_sizing_multiplier":  3.0,
                "margin_sizing_conviction_thresholds": {"high": 0.75, "medium": 0.50},
            },
        }
        sizes = compute_dynamic_sizes(
            equity=100_000.0, config=cfg,
            current_exposure_dollars=0.0, buying_power=300_000.0,
        )
        # sizing_basis_high = min(300K, 300K) = 300K → core HIGH (25%) = 75K
        assert abs(sizes["core_high"] - 75_000.0) < 1.0
        # sizing_basis_med = min(300K, 150K) = 150K → core 15% = 22.5K
        assert abs(sizes["core_med"] - 22_500.0) < 1.0
        # sizing_basis_low = equity = 100K → core 15% = 15K
        assert abs(sizes["core_low"] - 15_000.0) < 1.0
        # cap_high should equal sizing_basis_high
        assert abs(sizes["cap_high"] - 300_000.0) < 1.0

    def test_compute_dynamic_sizes_margin_disabled_falls_back_to_equity(self):
        from portfolio_intelligence import compute_dynamic_sizes
        cfg = {
            "position_sizing": {"core_tier_pct": 0.15, "dynamic_tier_pct": 0.08},
            "parameters": {
                "margin_authorized":        False,
                "margin_sizing_multiplier": 3.0,
            },
        }
        sizes = compute_dynamic_sizes(
            equity=100_000.0, config=cfg,
            current_exposure_dollars=0.0, buying_power=300_000.0,
        )
        # margin disabled → all sizing bases = equity → core HIGH = 25% × 100K = 25K
        assert abs(sizes["core_high"] - 25_000.0) < 1.0
        assert abs(sizes["core_med"]  - 15_000.0) < 1.0
        assert abs(sizes["core_low"]  - 15_000.0) < 1.0
