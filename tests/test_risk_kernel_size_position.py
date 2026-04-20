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

    def test_high_conviction_core_bumps_to_20pct(self, kernel_config):
        # conviction >= 0.75 AND tier == CORE → _CORE_HIGH_CONVICTION_PCT = 20%
        qty, value = size_position(
            _idea(tier=Tier.CORE, conviction=0.80), _snapshot(), kernel_config,
            current_price=100.0,
        )
        # 20% of 100K = 20,000; at $100 → 200 shares
        assert qty == 200
        assert abs(value - 20_000.0) < 1.0

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
        # MEDIUM conviction (0.60): effective cap ≈ equity (min of 1.5x equity, bp)
        # With equity=100K and existing exposure=90K → headroom=10K
        # CORE budget=15K → capped to 10K → 100 shares at $100
        snap = _snapshot(equity=100_000.0, extra_exposure=90_000.0)
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
