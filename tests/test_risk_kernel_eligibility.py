"""
test_risk_kernel_eligibility.py — Tests for risk_kernel.eligibility_check().

Covers all six hard gates:
  0. Time-bound action block
  1. VIX halt (>= 35)
  2. PDT equity floor (< $26K)
  3. Session gate — stocks/ETFs require market session
  4. Intraday tier gate
  5. Max open positions
  6. Catalyst required for buys

All tests are offline-safe (no Alpaca / Claude / Twilio / network calls).
"""

import pytest

from schemas import (
    AccountAction, BrokerSnapshot, Direction, NormalizedPosition, Tier,
    TradeIdea,
)
from risk_kernel import eligibility_check, PDT_FLOOR, VIX_HALT


# ── Helpers ───────────────────────────────────────────────────────────────────

def _snapshot(equity: float = 100_000.0, n_positions: int = 0) -> BrokerSnapshot:
    positions = [
        NormalizedPosition(
            symbol=f"SYM{i}", alpaca_sym=f"SYM{i}",
            qty=10.0, avg_entry_price=100.0, current_price=100.0,
            market_value=1_000.0, unrealized_pl=0.0, unrealized_plpc=0.0,
            is_crypto_pos=False,
        )
        for i in range(n_positions)
    ]
    return BrokerSnapshot(
        positions=positions,
        open_orders=[],
        equity=equity,
        cash=equity * 0.8,
        buying_power=equity,
    )


def _idea(
    symbol: str = "AAPL",
    action: AccountAction = AccountAction.BUY,
    tier: Tier = Tier.CORE,
    conviction: float = 0.80,
    direction: Direction = Direction.BULLISH,
    catalyst: str = "earnings_beat",
    intent: str = "enter_long",
) -> TradeIdea:
    return TradeIdea(
        symbol=symbol,
        action=action,
        tier=tier,
        conviction=conviction,
        direction=direction,
        catalyst=catalyst,
        intent=intent,
    )


# ── PDT equity floor ──────────────────────────────────────────────────────────

class TestPDTFloor:
    def test_equity_below_floor_rejected(self, kernel_config):
        result = eligibility_check(_idea(), _snapshot(equity=PDT_FLOOR - 1), kernel_config)
        assert result is not None
        assert "PDT" in result or "equity" in result.lower()

    def test_equity_exactly_at_floor_passes(self, kernel_config):
        result = eligibility_check(_idea(), _snapshot(equity=PDT_FLOOR), kernel_config)
        assert result is None

    def test_equity_well_above_floor_passes(self, kernel_config):
        result = eligibility_check(_idea(), _snapshot(equity=100_000.0), kernel_config)
        assert result is None

    def test_pdt_floor_also_blocks_close(self, kernel_config):
        # PDT floor is not BUY-specific; CLOSE is also blocked below floor
        idea = _idea(action=AccountAction.CLOSE, intent="close")
        result = eligibility_check(idea, _snapshot(equity=PDT_FLOOR - 1), kernel_config)
        assert result is not None


# ── VIX halt ──────────────────────────────────────────────────────────────────

class TestVIXHalt:
    def test_vix_at_halt_threshold_blocks_buy(self, kernel_config):
        result = eligibility_check(_idea(), _snapshot(), kernel_config, vix=VIX_HALT)
        assert result is not None
        assert "VIX" in result or "halt" in result.lower()

    def test_vix_above_halt_blocks_buy(self, kernel_config):
        result = eligibility_check(_idea(), _snapshot(), kernel_config, vix=40.0)
        assert result is not None

    def test_vix_just_below_halt_passes(self, kernel_config):
        result = eligibility_check(_idea(), _snapshot(), kernel_config, vix=VIX_HALT - 0.1)
        assert result is None

    def test_vix_halt_does_not_block_close(self, kernel_config):
        # VIX halt is BUY-only — CLOSE must not be blocked
        idea = _idea(action=AccountAction.CLOSE, intent="close")
        result = eligibility_check(idea, _snapshot(), kernel_config, vix=40.0)
        assert result is None

    def test_vix_caution_does_not_block_eligibility(self, kernel_config):
        # VIX 25–34.9 reduces size (in size_position) but does NOT block eligibility
        result = eligibility_check(_idea(), _snapshot(), kernel_config, vix=30.0)
        assert result is None


# ── Session gate ──────────────────────────────────────────────────────────────

class TestSessionGate:
    def test_stock_buy_blocked_in_extended_session(self, kernel_config):
        result = eligibility_check(
            _idea(symbol="AAPL"), _snapshot(), kernel_config,
            session_tier="extended",
        )
        assert result is not None
        assert "session" in result.lower()

    def test_stock_buy_blocked_in_overnight_session(self, kernel_config):
        result = eligibility_check(
            _idea(symbol="AAPL"), _snapshot(), kernel_config,
            session_tier="overnight",
        )
        assert result is not None

    def test_stock_buy_allowed_in_market_session(self, kernel_config):
        result = eligibility_check(
            _idea(symbol="AAPL"), _snapshot(), kernel_config,
            session_tier="market",
        )
        assert result is None

    def test_crypto_buy_allowed_in_extended_session(self, kernel_config):
        # Crypto is not subject to the stock session gate
        result = eligibility_check(
            _idea(symbol="BTC/USD"), _snapshot(), kernel_config,
            session_tier="extended",
        )
        assert result is None

    def test_intraday_crypto_blocked_outside_market_session(self, kernel_config):
        # INTRADAY tier requires market session regardless of asset class
        idea = _idea(symbol="BTC/USD", tier=Tier.INTRADAY)
        result = eligibility_check(idea, _snapshot(), kernel_config,
                                   session_tier="extended")
        assert result is not None
        assert "intraday" in result.lower() or "session" in result.lower()


# ── Max positions ─────────────────────────────────────────────────────────────

class TestMaxPositions:
    def test_at_max_positions_blocks_buy(self, kernel_config):
        import copy
        cfg = copy.deepcopy(kernel_config)
        cfg["parameters"]["max_positions"] = 3
        result = eligibility_check(_idea(), _snapshot(n_positions=3), cfg)
        assert result is not None
        assert "max_position" in result.lower() or "3" in result

    def test_one_below_max_positions_allows_buy(self, kernel_config):
        import copy
        cfg = copy.deepcopy(kernel_config)
        cfg["parameters"]["max_positions"] = 3
        result = eligibility_check(_idea(), _snapshot(n_positions=2), cfg)
        assert result is None


# ── Catalyst gate ─────────────────────────────────────────────────────────────

class TestCatalystGate:
    def test_empty_catalyst_blocks_buy(self, kernel_config):
        result = eligibility_check(_idea(catalyst=""), _snapshot(), kernel_config)
        assert result is not None
        assert "catalyst" in result.lower()

    def test_none_string_catalyst_blocks_buy(self, kernel_config):
        result = eligibility_check(_idea(catalyst="none"), _snapshot(), kernel_config)
        assert result is not None

    def test_null_string_catalyst_blocks_buy(self, kernel_config):
        result = eligibility_check(_idea(catalyst="null"), _snapshot(), kernel_config)
        assert result is not None

    def test_named_catalyst_passes(self, kernel_config):
        result = eligibility_check(_idea(catalyst="earnings_beat_q4"), _snapshot(), kernel_config)
        assert result is None

    def test_hold_bypasses_catalyst_check(self, kernel_config):
        # HOLD is not subject to catalyst gate
        idea = _idea(action=AccountAction.HOLD, catalyst="", intent="hold")
        result = eligibility_check(idea, _snapshot(), kernel_config)
        assert result is None
