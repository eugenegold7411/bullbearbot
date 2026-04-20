"""
test_risk_kernel_place_stops.py — Tests for risk_kernel.place_stops().

Covers:
  - Default stop/target from config (stop_loss_pct_core=3.5%, take_profit_multiple=2.5)
  - Advisory stop pct respected and capped
  - Advisory target_r respected and MIN_RR_RATIO floor enforced
  - Hard stop ceilings: stocks core=4%, intraday=2%, crypto core=8%
  - Invalid price rejection

All tests are offline-safe.
"""


from risk_kernel import MIN_RR_RATIO, place_stops
from schemas import AccountAction, Direction, Tier, TradeIdea

# ── Helpers ───────────────────────────────────────────────────────────────────

def _idea(
    symbol: str = "AAPL",
    tier: Tier = Tier.CORE,
    advisory_stop_pct: float | None = None,
    advisory_target_r: float | None = None,
) -> TradeIdea:
    return TradeIdea(
        symbol=symbol,
        action=AccountAction.BUY,
        tier=tier,
        conviction=0.80,
        direction=Direction.BULLISH,
        catalyst="test_catalyst",
        intent="enter_long",
        advisory_stop_pct=advisory_stop_pct,
        advisory_target_r=advisory_target_r,
    )


# ── Basic output shape ────────────────────────────────────────────────────────

class TestStopPlacement:
    def test_returns_stop_below_entry_and_target_above(self, kernel_config):
        result = place_stops(_idea(), current_price=100.0, config=kernel_config)
        assert isinstance(result, tuple), f"Expected (stop, target) tuple, got: {result!r}"
        stop, target = result
        assert stop < 100.0
        assert target > 100.0

    def test_stop_and_target_scale_with_price(self, kernel_config):
        stop_100, target_100 = place_stops(_idea(), current_price=100.0, config=kernel_config)
        stop_200, target_200 = place_stops(_idea(), current_price=200.0, config=kernel_config)
        # Distances should scale proportionally
        assert stop_200 < 200.0
        assert target_200 > 200.0
        dist_100 = 100.0 - stop_100
        dist_200 = 200.0 - stop_200
        assert abs(dist_200 / dist_100 - 2.0) < 0.1

    def test_default_config_stop_is_used_when_no_advisory(self, kernel_config):
        # No advisory hint → config stop_loss_pct_core = 0.035 (3.5%)
        # max_stop for core stock = 4%; min(3.5%, 4%) = 3.5%
        stop, _ = place_stops(_idea(), current_price=100.0, config=kernel_config)
        stop_pct = (100.0 - stop) / 100.0
        assert abs(stop_pct - 0.035) < 0.001


# ── Advisory hints ────────────────────────────────────────────────────────────

class TestAdvisoryHints:
    def test_advisory_stop_pct_respected_when_within_cap(self, kernel_config):
        # 2% < 4% cap for core stocks → accepted as-is
        stop, _ = place_stops(_idea(advisory_stop_pct=0.02), current_price=100.0, config=kernel_config)
        assert abs(stop - 98.0) < 0.01

    def test_advisory_target_r_respected_when_above_min_rr(self, kernel_config):
        # advisory_target_r=3.0 > MIN_RR_RATIO=2.0 → accepted as-is
        stop, target = place_stops(
            _idea(advisory_stop_pct=0.02, advisory_target_r=3.0),
            current_price=100.0, config=kernel_config,
        )
        stop_dist = 100.0 - stop
        actual_rr = (target - 100.0) / stop_dist
        assert abs(actual_rr - 3.0) < 0.01

    def test_min_rr_ratio_floor_enforced(self, kernel_config):
        # advisory_target_r=1.0 < MIN_RR_RATIO=2.0 → bumped up to 2.0
        stop, target = place_stops(
            _idea(advisory_stop_pct=0.02, advisory_target_r=1.0),
            current_price=100.0, config=kernel_config,
        )
        stop_dist = 100.0 - stop
        actual_rr = (target - 100.0) / stop_dist
        assert actual_rr >= MIN_RR_RATIO - 0.001


# ── Hard stop ceilings ────────────────────────────────────────────────────────

class TestStopCeilings:
    def test_core_stock_stop_capped_at_4pct(self, kernel_config):
        # Request 10% advisory stop → capped to 4% ceiling for core stocks
        stop, _ = place_stops(
            _idea(tier=Tier.CORE, advisory_stop_pct=0.10),
            current_price=100.0, config=kernel_config,
        )
        stop_pct = (100.0 - stop) / 100.0
        assert stop_pct <= 0.04 + 1e-9

    def test_intraday_stop_capped_at_2pct(self, kernel_config):
        # Request 10% advisory stop → capped to 2% ceiling for intraday
        stop, _ = place_stops(
            _idea(tier=Tier.INTRADAY, advisory_stop_pct=0.10),
            current_price=100.0, config=kernel_config,
        )
        stop_pct = (100.0 - stop) / 100.0
        assert stop_pct <= 0.02 + 1e-9

    def test_crypto_core_stop_capped_at_8pct(self, kernel_config):
        # Request 15% advisory stop → capped to 8% ceiling for core crypto
        stop, _ = place_stops(
            _idea(symbol="BTC/USD", tier=Tier.CORE, advisory_stop_pct=0.15),
            current_price=50_000.0, config=kernel_config,
        )
        stop_pct = (50_000.0 - stop) / 50_000.0
        assert stop_pct <= 0.08 + 1e-9


# ── Invalid input rejection ───────────────────────────────────────────────────

class TestInvalidInput:
    def test_zero_price_returns_rejection_string(self, kernel_config):
        result = place_stops(_idea(), current_price=0.0, config=kernel_config)
        assert isinstance(result, str)
        assert "unavailable" in result.lower() or "cannot" in result.lower()

    def test_none_price_returns_rejection_string(self, kernel_config):
        result = place_stops(_idea(), current_price=None, config=kernel_config)  # type: ignore[arg-type]
        assert isinstance(result, str)
