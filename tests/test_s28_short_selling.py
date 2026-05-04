"""
A1 Short Selling — Session 1 of 3.

Tests for schemas.py + risk_kernel.py changes:
  1. AccountAction.SHORT_SELL / COVER enum values
  2. Intent map: enter_short → SHORT_SELL; cover → COVER
  3. BrokerSnapshot.short_exposure_dollars property
  4. eligibility_check: max_short_exposure_pct=0 → disabled
  5. process_idea SHORT_SELL: approved when pct > 0
  6. process_idea COVER: closes short; rejects long position
  7. place_stops side="short": stop above entry, target below
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))
os.chdir(_BOT_DIR)

_STUBS = [
    "dotenv", "anthropic",
    "alpaca", "alpaca.trading", "alpaca.trading.client",
    "alpaca.trading.requests", "alpaca.trading.enums",
    "chromadb", "sendgrid", "sendgrid.helpers", "sendgrid.helpers.mail",
]
for _s in _STUBS:
    if _s not in sys.modules:
        sys.modules[_s] = mock.MagicMock()

from risk_kernel import place_stops, process_idea  # noqa: E402
from schemas import (  # noqa: E402
    AccountAction,
    BrokerAction,
    BrokerSnapshot,
    Direction,
    NormalizedPosition,
    Tier,
    TradeIdea,
    validate_claude_decision,
)

# ── Shared helpers ─────────────────────────────────────────────────────────────

def _long_pos(symbol: str, qty: float, price: float) -> NormalizedPosition:
    return NormalizedPosition(
        symbol=symbol, alpaca_sym=symbol,
        qty=qty, avg_entry_price=price, current_price=price,
        market_value=qty * price,
        unrealized_pl=0.0, unrealized_plpc=0.0,
        is_crypto_pos=False,
    )


def _short_pos(symbol: str, qty: float, price: float) -> NormalizedPosition:
    """Simulate an open short: negative qty, negative market_value."""
    return NormalizedPosition(
        symbol=symbol, alpaca_sym=symbol,
        qty=-qty, avg_entry_price=price, current_price=price,
        market_value=-qty * price,
        unrealized_pl=0.0, unrealized_plpc=0.0,
        is_crypto_pos=False,
    )


def _snap(positions: list, buying_power: float, equity: float) -> BrokerSnapshot:
    return BrokerSnapshot(
        positions=positions, open_orders=[],
        equity=equity,
        cash=buying_power,
        buying_power=buying_power,
    )


def _short_idea(symbol: str = "NVDA", catalyst: str = "bearish breakdown") -> TradeIdea:
    return TradeIdea(
        symbol=symbol,
        action=AccountAction.SHORT_SELL,
        intent="enter_short",
        tier=Tier.DYNAMIC,
        conviction=0.70,
        direction=Direction.BEARISH,
        catalyst=catalyst,
    )


def _cover_idea(symbol: str = "NVDA") -> TradeIdea:
    return TradeIdea(
        symbol=symbol,
        action=AccountAction.COVER,
        intent="cover",
        tier=Tier.DYNAMIC,
        conviction=0.60,
        direction=Direction.BEARISH,
        catalyst="cover on target",
    )


def _cfg_short_enabled(max_short_pct: float = 0.20) -> dict:
    return {"parameters": {
        "max_short_exposure_pct": max_short_pct,
        "stop_loss_pct_core": 0.035,
        "take_profit_multiple": 2.5,
    }}


def _cfg_short_disabled() -> dict:
    return {"parameters": {"max_short_exposure_pct": 0.00}}


# ── 1. Intent map: enter_short → SHORT_SELL ───────────────────────────────────

class TestIntentMapping(unittest.TestCase):

    def test_enter_short_intent_maps_to_short_sell(self):
        """validate_claude_decision must map intent='enter_short' → SHORT_SELL."""
        decision = validate_claude_decision({"ideas": [{
            "symbol": "NVDA",
            "intent": "enter_short",
            "tier": "dynamic",
            "conviction": 0.70,
            "direction": "bearish",
            "catalyst": "bearish channel breakdown",
        }]})
        self.assertEqual(len(decision.ideas), 1)
        self.assertEqual(decision.ideas[0].action, AccountAction.SHORT_SELL)

    def test_cover_intent_maps_to_cover(self):
        """validate_claude_decision must map intent='cover' → COVER."""
        decision = validate_claude_decision({"ideas": [{
            "symbol": "NVDA",
            "intent": "cover",
            "tier": "dynamic",
            "conviction": 0.55,
            "direction": "bearish",
            "catalyst": "cover at target",
        }]})
        self.assertEqual(len(decision.ideas), 1)
        self.assertEqual(decision.ideas[0].action, AccountAction.COVER)


# ── 2. BrokerSnapshot.short_exposure_dollars ──────────────────────────────────

class TestShortExposureDollars(unittest.TestCase):

    def test_short_exposure_dollars_property(self):
        """short_exposure_dollars sums abs(market_value) for qty < 0 only."""
        positions = [
            _long_pos("AAPL", 10, 200.0),   # +$2,000 long
            _short_pos("NVDA", 5, 100.0),   # −$500 short (market_value = -500)
            _short_pos("TSLA", 2, 250.0),   # −$500 short (market_value = -500)
        ]
        snap = _snap(positions, buying_power=50_000.0, equity=100_000.0)
        self.assertAlmostEqual(snap.short_exposure_dollars, 1000.0)
        self.assertAlmostEqual(snap.long_exposure_dollars, 2000.0)


# ── 3. Short selling disabled when max_short_exposure_pct=0 ───────────────────

class TestShortSellDisabled(unittest.TestCase):

    def test_short_sell_disabled_when_pct_zero(self):
        """process_idea must reject SHORT_SELL when max_short_exposure_pct=0."""
        snap = _snap([], buying_power=80_000.0, equity=100_000.0)
        result = process_idea(
            _short_idea("NVDA"),
            snap,
            None,
            _cfg_short_disabled(),
            current_price=500.0,
        )
        self.assertIsInstance(result, str)
        self.assertIn("short_selling_disabled", result)


# ── 4. Short selling approved when pct > 0 ────────────────────────────────────

class TestShortSellApproved(unittest.TestCase):

    def test_short_sell_approved_when_pct_enabled(self):
        """process_idea must return BrokerAction(SHORT_SELL) when enabled."""
        snap = _snap([], buying_power=80_000.0, equity=100_000.0)
        result = process_idea(
            _short_idea("NVDA"),
            snap,
            None,
            _cfg_short_enabled(0.20),
            current_price=500.0,
        )
        self.assertIsInstance(result, BrokerAction,
                              f"expected BrokerAction, got: {result}")
        self.assertEqual(result.action, AccountAction.SHORT_SELL)
        self.assertGreater(result.qty, 0)
        # Short stop must be above entry; take_profit must be below
        self.assertGreater(result.stop_loss, 500.0)
        self.assertLess(result.take_profit, 500.0)


# ── 5. COVER closes a short position ──────────────────────────────────────────

class TestCover(unittest.TestCase):

    def test_cover_closes_short_position(self):
        """COVER on a short position returns BrokerAction(COVER) with full qty."""
        short = _short_pos("NVDA", 10, 500.0)
        snap = _snap([short], buying_power=95_000.0, equity=100_000.0)
        result = process_idea(
            _cover_idea("NVDA"),
            snap,
            None,
            _cfg_short_enabled(0.20),
            current_price=500.0,
        )
        self.assertIsInstance(result, BrokerAction,
                              f"expected BrokerAction, got: {result}")
        self.assertEqual(result.action, AccountAction.COVER)
        self.assertEqual(result.qty, 10.0)

    def test_cover_rejects_on_long_position(self):
        """COVER must reject if the position is long, not short."""
        long = _long_pos("NVDA", 10, 500.0)
        snap = _snap([long], buying_power=95_000.0, equity=100_000.0)
        result = process_idea(
            _cover_idea("NVDA"),
            snap,
            None,
            _cfg_short_enabled(0.20),
            current_price=500.0,
        )
        self.assertIsInstance(result, str)
        self.assertIn("long", result.lower())

    def test_cover_rejects_on_no_position(self):
        """COVER with no position must reject."""
        snap = _snap([], buying_power=100_000.0, equity=100_000.0)
        result = process_idea(
            _cover_idea("NVDA"),
            snap,
            None,
            _cfg_short_enabled(0.20),
            current_price=500.0,
        )
        self.assertIsInstance(result, str)
        self.assertIn("no open position", result)


# ── 6. place_stops side="short" ───────────────────────────────────────────────

class TestPlaceStopsShort(unittest.TestCase):

    def test_place_stops_short_inverted_prices(self):
        """side='short': stop_loss > entry, take_profit < entry."""
        idea = TradeIdea(
            symbol="NVDA",
            action=AccountAction.SHORT_SELL,
            intent="enter_short",
            tier=Tier.DYNAMIC,
            conviction=0.70,
            direction=Direction.BEARISH,
            catalyst="test",
        )
        cfg = {"parameters": {
            "stop_loss_pct_core": 0.04,
            "take_profit_multiple": 2.5,
        }}
        result = place_stops(idea, 500.0, cfg, side="short")
        self.assertIsInstance(result, tuple,
                              f"expected (stop, target), got: {result}")
        stop_loss, take_profit = result
        self.assertGreater(stop_loss, 500.0,
                           f"short stop_loss {stop_loss} must be above entry 500")
        self.assertLess(take_profit, 500.0,
                        f"short take_profit {take_profit} must be below entry 500")

    def test_place_stops_long_unchanged(self):
        """side='long' (default): stop_loss < entry, take_profit > entry."""
        idea = TradeIdea(
            symbol="AAPL",
            action=AccountAction.BUY,
            intent="enter_long",
            tier=Tier.CORE,
            conviction=0.70,
            direction=Direction.BULLISH,
            catalyst="test",
        )
        cfg = {"parameters": {
            "stop_loss_pct_core": 0.035,
            "take_profit_multiple": 2.5,
        }}
        result = place_stops(idea, 200.0, cfg)
        self.assertIsInstance(result, tuple)
        stop_loss, take_profit = result
        self.assertLess(stop_loss, 200.0)
        self.assertGreater(take_profit, 200.0)


if __name__ == "__main__":
    unittest.main()
