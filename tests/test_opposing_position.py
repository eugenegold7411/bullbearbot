"""
tests/test_opposing_position.py

Fix C — opposing position gate in risk_kernel.eligibility_check() (check 7.5).

opposing_position_1 — ENTER_LONG blocked when short position exists for same symbol
opposing_position_2 — ENTER_LONG allowed when only long positions exist for same symbol
opposing_position_3 — ENTER_SHORT blocked when long position exists for same symbol
opposing_position_4 — ENTER_SHORT blocked when pending BUY order exists for same symbol
opposing_position_5 — ENTER_SHORT allowed when no opposing position or pending order
"""
from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, __import__("pathlib").Path(__file__).parent.parent.as_posix())

# ── Minimal stubs for alpaca (risk_kernel imports from schemas, not alpaca directly) ─

from schemas import (  # noqa: E402
    AccountAction,
    BrokerSnapshot,
    Direction,
    NormalizedOrder,
    NormalizedPosition,
    Tier,
    TradeIdea,
)
import risk_kernel  # noqa: E402


# ── Fixture builders ───────────────────────────────────────────────────────────

def _make_idea(
    symbol: str = "AAPL",
    action: AccountAction = AccountAction.BUY,
    conviction: float = 0.80,
    tier: Tier = Tier.CORE,
    direction: Direction = Direction.BULLISH,
    catalyst: str = "earnings_beat",
) -> TradeIdea:
    return TradeIdea(
        symbol=symbol,
        action=action,
        conviction=conviction,
        tier=tier,
        direction=direction,
        catalyst=catalyst,
        intent="enter",
    )


def _make_position(symbol: str, qty: float) -> NormalizedPosition:
    """qty > 0 → long; qty < 0 → short."""
    return NormalizedPosition(
        symbol=symbol,
        alpaca_sym=symbol,
        qty=qty,
        avg_entry_price=100.0,
        current_price=105.0,
        market_value=abs(qty) * 105.0,
        unrealized_pl=abs(qty) * 5.0,
        unrealized_plpc=0.05,
        is_crypto_pos=False,
    )


def _make_order(symbol: str, side: str) -> NormalizedOrder:
    return NormalizedOrder(
        order_id="ord-test-001",
        symbol=symbol,
        alpaca_sym=symbol,
        side=side,
        order_type="market",
        qty=10.0,
        filled_qty=0.0,
        stop_price=None,
        limit_price=None,
        status="open",
    )


def _make_snapshot(
    positions: list[NormalizedPosition] | None = None,
    open_orders: list[NormalizedOrder] | None = None,
    equity: float = 100_000.0,
) -> BrokerSnapshot:
    return BrokerSnapshot(
        positions=positions or [],
        open_orders=open_orders or [],
        equity=equity,
        cash=equity,
        buying_power=equity * 2,
    )


def _base_config() -> dict:
    return {
        "parameters": {
            "max_positions": 15,
            "add_conviction_gate": 0.65,
            "blocked_symbols": [],
            "catalyst_tag_disallowed_values": ["", "none", "null", "no"],
            "vix_elevated_threshold": 25,
            "vix_cautious_threshold": 30,
            "vix_stressed_threshold": 40,
            "vix_stressed_conviction_floor": 0.75,
            "vix_calm_threshold": 20,
            "max_short_exposure_pct": 0.25,
        }
    }


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestOpposingPositionGate(unittest.TestCase):

    def test_opposing_position_1_enter_long_blocked_when_short_exists(self):
        """ENTER_LONG blocked when a short position exists for the same symbol."""
        idea     = _make_idea(symbol="AAPL", action=AccountAction.BUY)
        snapshot = _make_snapshot(positions=[_make_position("AAPL", qty=-10)])

        result = risk_kernel.eligibility_check(idea, snapshot, _base_config(), vix=18.0)

        self.assertIsNotNone(result, "Expected rejection; got None (allowed)")
        self.assertIn("opposing_position", result)
        self.assertIn("AAPL", result)

    def test_opposing_position_2_enter_long_allowed_with_only_longs(self):
        """ENTER_LONG allowed when existing AAPL position is long (ADD path)."""
        # Uses a high conviction to pass check 7 (ADD gate)
        idea     = _make_idea(symbol="AAPL", action=AccountAction.BUY, conviction=0.90)
        snapshot = _make_snapshot(positions=[_make_position("AAPL", qty=10)])

        result = risk_kernel.eligibility_check(idea, snapshot, _base_config(), vix=18.0)

        self.assertIsNone(
            result,
            f"Expected eligible (long ADD), got rejection: {result}",
        )

    def test_opposing_position_3_enter_short_blocked_when_long_exists(self):
        """ENTER_SHORT blocked when a long position exists for the same symbol."""
        idea     = _make_idea(symbol="MSFT", action=AccountAction.SHORT_SELL, catalyst="trend_reversal")
        snapshot = _make_snapshot(positions=[_make_position("MSFT", qty=50)])

        result = risk_kernel.eligibility_check(idea, snapshot, _base_config(), vix=18.0)

        self.assertIsNotNone(result, "Expected rejection; got None (allowed)")
        self.assertIn("opposing_position", result)
        self.assertIn("MSFT", result)

    def test_opposing_position_4_enter_short_blocked_when_pending_buy(self):
        """ENTER_SHORT blocked when a pending BUY order exists for same symbol."""
        idea     = _make_idea(symbol="TSLA", action=AccountAction.SHORT_SELL, catalyst="earnings_miss")
        # No position — order only
        snapshot = _make_snapshot(
            positions=[],
            open_orders=[_make_order("TSLA", side="buy")],
        )

        result = risk_kernel.eligibility_check(idea, snapshot, _base_config(), vix=18.0)

        self.assertIsNotNone(result, "Expected rejection due to pending BUY; got None")
        self.assertIn("opposing_position", result)
        self.assertIn("TSLA", result)

    def test_opposing_position_5_enter_short_allowed_with_no_conflict(self):
        """ENTER_SHORT allowed when no long position and no pending BUY exist."""
        idea     = _make_idea(symbol="SPY", action=AccountAction.SHORT_SELL, catalyst="macro_deterioration")
        snapshot = _make_snapshot(
            positions=[_make_position("QQQ", qty=-5)],  # short in different symbol — OK
            open_orders=[_make_order("IWM", side="sell")],  # sell order on different symbol
        )

        result = risk_kernel.eligibility_check(idea, snapshot, _base_config(), vix=18.0)

        self.assertIsNone(
            result,
            f"Expected eligible short, got rejection: {result}",
        )


if __name__ == "__main__":
    unittest.main()
