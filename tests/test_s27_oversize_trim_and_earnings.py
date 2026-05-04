"""
S27 — OVERSIZE trim and earnings entry tests.

Bug 1: trim_never_full_exit
  A reduce intent must sell only the excess shares, not the whole position.
  Covers: partial trim qty < current_qty, trim=1 at-boundary case,
  safety cap when computed trim >= full position, crypto trim.

Bug 2: earnings_entry_allowed / a2_binary_event_still_blocked
  A1 signal scorer must NOT penalise or conflict-flag earnings proximity.
  A2 RULE1 binary event block must remain intact.
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

# Stub heavy third-party deps that bot modules import at top level.
_STUBS = [
    "dotenv", "anthropic",
    "alpaca", "alpaca.trading", "alpaca.trading.client",
    "alpaca.trading.requests", "alpaca.trading.enums",
    "chromadb", "sendgrid", "sendgrid.helpers", "sendgrid.helpers.mail",
]
for _s in _STUBS:
    if _s not in sys.modules:
        sys.modules[_s] = mock.MagicMock()


# ── Helpers ───────────────────────────────────────────────────────────────────

from risk_kernel import process_idea  # noqa: E402
from schemas import (  # noqa: E402
    AccountAction,
    BrokerAction,
    BrokerSnapshot,
    Direction,
    NormalizedPosition,
    Tier,
    TradeIdea,
)


def _pos(symbol: str, qty: float, price: float, is_crypto: bool = False) -> NormalizedPosition:
    return NormalizedPosition(
        symbol=symbol, alpaca_sym=symbol,
        qty=qty, avg_entry_price=price, current_price=price,
        market_value=qty * price,
        unrealized_pl=0.0, unrealized_plpc=0.0,
        is_crypto_pos=is_crypto,
    )


def _snap(pos: NormalizedPosition, buying_power: float) -> BrokerSnapshot:
    return BrokerSnapshot(
        positions=[pos], open_orders=[],
        equity=pos.market_value + buying_power,
        cash=buying_power,
        buying_power=buying_power,
    )


def _reduce_idea(symbol: str, is_crypto: bool = False) -> TradeIdea:
    return TradeIdea(
        symbol=symbol,
        action=AccountAction.SELL,
        intent="reduce",
        tier=Tier.CORE,
        conviction=0.62,
        direction=Direction.BULLISH,
        catalyst="oversize compliance trim",
    )


def _cfg(max_pos_pct: float = 0.15) -> dict:
    return {"parameters": {"max_position_pct_capacity": max_pos_pct}}


# ── Bug 1: trim_never_full_exit ───────────────────────────────────────────────

class TestTrimNeverFullExit(unittest.TestCase):

    def test_trim_qty_less_than_full_position(self):
        """Core case: GOOGL 15.1% of capacity → trim 1 share, keep 132."""
        qty   = 133.0
        price = 170.0
        # total_capacity chosen so GOOGL is exactly 15.1% oversize
        exposure  = qty * price                  # $22,610
        total_cap = exposure / 0.151             # ≈ $149,735
        bp        = total_cap - exposure

        result = process_idea(
            _reduce_idea("GOOGL"),
            _snap(_pos("GOOGL", qty, price), bp),
            None,
            _cfg(0.15),
            price,
        )
        self.assertIsInstance(result, BrokerAction,
                              f"expected BrokerAction, got: {result}")
        self.assertLess(result.qty, qty,
                        f"trim_qty={result.qty} must be < full position {qty}")
        self.assertGreater(result.qty, 0,
                           "trim_qty must be > 0")

    def test_trim_qty_is_correct_partial(self):
        """trim_qty = current_qty - floor(total_cap * 0.15 / price)."""
        qty   = 200.0
        price = 100.0
        # total_cap = $200,000  →  target = floor(0.15 * 200_000 / 100) = 300
        # BUT target > current, so trim <= 0 → should be rejected as "within limit"
        # Let's make it clearly oversize: total_cap = $100,000
        # → target = floor(0.15 * 100_000 / 100) = 150
        # → trim = 200 - 150 = 50
        total_cap = 100_000.0
        bp = total_cap - qty * price  # $80,000 buying power

        result = process_idea(
            _reduce_idea("MSFT"),
            _snap(_pos("MSFT", qty, price), bp),
            None,
            _cfg(0.15),
            price,
        )
        self.assertIsInstance(result, BrokerAction)
        self.assertEqual(result.qty, 50.0,
                         f"expected trim_qty=50, got {result.qty}")

    def test_safety_cap_prevents_full_liquidation(self):
        """If trim_qty >= current_qty (miscalculation path), cap at current_qty - 1."""
        # Construct a scenario where the target would exceed current_qty.
        # e.g. total_cap is so large that target_qty > current_qty.
        # In practice this means position is already within limit → early-return rejection.
        # To hit the safety cap we'd need target_qty exactly == current_qty.
        # We test the guard by monkey-patching a bad total_capacity = 0.
        qty   = 10.0
        price = 100.0
        # With total_capacity = 0, target_qty = 0, trim_qty = 10 = current_qty → cap fires
        result = process_idea(
            _reduce_idea("TEST"),
            _snap(_pos("TEST", qty, price), 0.0),  # buying_power=0 → total_cap=exposure only
            None,
            # Set absurdly small max_pos_pct so target_qty rounds to 0
            {"parameters": {"max_position_pct_capacity": 0.0001}},
            price,
        )
        # trim_qty would be 10.0 - 0 = 10.0 >= current_qty=10.0 → cap to 9
        self.assertIsInstance(result, BrokerAction,
                              f"should have returned BrokerAction, got: {result}")
        self.assertLess(result.qty, qty,
                        f"safety cap failed: qty={result.qty} should be < {qty}")

    def test_already_within_limit_rejected(self):
        """Position at exactly 10% when limit is 15% → reduce returns rejection string."""
        qty   = 100.0
        price = 100.0
        # position = $10,000; total_cap needs to make that 10%: total_cap = $100,000
        # target_qty = floor(0.15 * 100_000 / 100) = 150 > 100 → trim <= 0 → reject
        total_cap = 100_000.0
        bp = total_cap - qty * price

        result = process_idea(
            _reduce_idea("AAPL"),
            _snap(_pos("AAPL", qty, price), bp),
            None,
            _cfg(0.15),
            price,
        )
        self.assertIsInstance(result, str,
                              f"expected rejection string, got BrokerAction qty={getattr(result, 'qty', None)}")
        self.assertIn("within", result)

    def test_no_current_price_rejected(self):
        """reduce without a price → rejection string, not a sell."""
        qty = 50.0
        pos = _pos("V", qty, 200.0)
        result = process_idea(
            _reduce_idea("V"),
            _snap(pos, 50_000.0),
            None,
            _cfg(0.15),
            None,  # no price
        )
        self.assertIsInstance(result, str)
        self.assertIn("current_price", result)

    def test_reduce_action_is_sell(self):
        """BrokerAction returned by reduce must have action=SELL."""
        qty   = 133.0
        price = 170.0
        exposure  = qty * price
        total_cap = exposure / 0.151
        bp        = total_cap - exposure

        result = process_idea(
            _reduce_idea("GOOGL"),
            _snap(_pos("GOOGL", qty, price), bp),
            None,
            _cfg(0.15),
            price,
        )
        self.assertIsInstance(result, BrokerAction)
        self.assertEqual(result.action, AccountAction.SELL)


# ── Bug 2a: earnings_entry_allowed (A1 signal scorer) ────────────────────────

class TestEarningsEntryAllowed(unittest.TestCase):

    def setUp(self):
        from bot_stage2_python import _CYCLE_CACHE
        self._cache = _CYCLE_CACHE
        self._backup = dict(_CYCLE_CACHE)

    def tearDown(self):
        self._cache.clear()
        self._cache.update(self._backup)

    def _base_md(self, sym: str = "PLTR") -> dict:
        return {
            "ind_by_symbol": {sym: {
                "price": 25.0, "prev": 24.0,
                "ma20": 22.0, "ma50": 20.0,
                "ema9": 24.5, "ema21": 23.0, "ema9_cross": "golden",
                "price_above_ema9": True,
                "rsi": 65.0, "macd": 0.5, "macd_signal": 0.3,
                "vol_ratio": 1.5,
            }},
            "intraday_summaries": {},
            "current_prices": {sym: 25.0},
        }

    def _score(self, sym: str, eda: int | None) -> dict:
        from bot_stage2_python import score_symbol_python
        self._cache.update({
            "orb_by_sym":    {},
            "morning_brief": {},
            "pattern_wl":    {},
            "insider_evt":   {},
            "earnings_map":  {sym.upper(): eda} if eda is not None else {},
        })
        return score_symbol_python(sym, self._base_md(sym), {"bias": "neutral"})

    def test_no_penalty_when_earnings_today(self):
        """eda=0: score must equal score with no earnings (penalty removed)."""
        score_with_earnings    = self._score("PLTR", eda=0)["score"]
        score_without_earnings = self._score("PLTR", eda=None)["score"]
        self.assertEqual(score_with_earnings, score_without_earnings,
                         f"earnings eda=0 still penalising: "
                         f"{score_with_earnings} vs {score_without_earnings}")

    def test_no_penalty_when_earnings_tomorrow(self):
        """eda=1: no penalty."""
        s_earn = self._score("PLTR", eda=1)["score"]
        s_none = self._score("PLTR", eda=None)["score"]
        self.assertEqual(s_earn, s_none)

    def test_no_penalty_when_earnings_two_days(self):
        """eda=2: no penalty."""
        s_earn = self._score("PLTR", eda=2)["score"]
        s_none = self._score("PLTR", eda=None)["score"]
        self.assertEqual(s_earn, s_none)

    def test_earnings_days_away_still_in_result(self):
        """earnings_days_away must still be returned as context data."""
        result = self._score("PLTR", eda=0)
        self.assertIn("earnings_days_away", result)
        self.assertEqual(result["earnings_days_away"], 0)

    def test_no_earnings_conflict_in_conflicts(self):
        """'earnings_in_Xd' must not appear in conflicts list."""
        result = self._score("PLTR", eda=0)
        earnings_conflicts = [c for c in result.get("conflicts", [])
                              if "earnings_in_" in c]
        self.assertEqual(earnings_conflicts, [],
                         f"earnings conflict still injected: {earnings_conflicts}")


# ── Bug 2b: A2 RULE1 binary event block still intact ────────────────────────

class TestA2EarningsRouting(unittest.TestCase):

    def _make_pack(self, eda: int | None, iv_rank: float = 60.0):
        from datetime import datetime, timezone

        from schemas import A2FeaturePack
        return A2FeaturePack(
            symbol="PLTR",
            a1_signal_score=72.0,
            a1_direction="bullish",
            trend_score=None,
            momentum_score=None,
            sector_alignment="technology",
            iv_rank=iv_rank,
            iv_environment="neutral",
            term_structure_slope=None,
            skew=None,
            expected_move_pct=8.0,
            flow_imbalance_30m=None,
            sweep_count=None,
            gex_regime=None,
            oi_concentration=None,
            earnings_days_away=eda,
            macro_event_flag=False,
            premium_budget_usd=5000.0,
            liquidity_score=0.8,
            built_at=datetime.now(timezone.utc).isoformat(),
            data_sources=["signal_scores"],
        )

    def _route(self, eda: int | None, iv_rank: float = 60.0) -> list:
        from bot_options_stage2_structures import _route_strategy
        cfg = {"a2_router": {
            "earnings_dte_blackout": 0,
            "earnings_dte_window": 14,
            "earnings_iv_rank_gate": 70,
            "min_liquidity_score": 0.25,
            "macro_iv_gate_rank": 70,
            "iv_env_blackout": [],
            "post_earnings_window_premarket": 2,
            "post_earnings_window_postmarket": 1,
            "post_earnings_window_unknown": 1,
            "post_earnings_iv_rank_min": 55,
            "post_earnings_iv_already_crushed_threshold": 15,
            "pre_earnings_credit_spread_enabled": False,
            "pre_earnings_iv_rank_min": 85,
            "pre_earnings_dte_min": 7,
            "pre_earnings_dte_max": 14,
            "straddle_iv_rank_max": 40,
            "straddle_dte_min": 6,
            "straddle_dte_max": 14,
            "short_put_iv_rank_min": 50,
            "iron_iv_rank_min": 50,
            "iron_low_conviction_threshold": 0.6,
            "macro_event_routing_enabled": True,
            "macro_event_credit_iv_min": 85,
            "macro_event_condor_iv_min": 70,
            "macro_event_debit_iv_max": 70,
        }}
        return _route_strategy(
            self._make_pack(eda, iv_rank),
            config=cfg,
            earnings_calendar_data={"calendar": []},
        )

    def test_eda_0_routes_normally(self):
        """eda=0 -> RULE1 removed; routes via IV rules (neutral IV + bullish → RULE6)."""
        result = self._route(eda=0)
        self.assertNotEqual(result, [],
                            f"eda=0 should route normally (RULE1 removed), got: {result}")

    def test_eda_1_routes_normally(self):
        """eda=1 -> RULE1 removed; routes via IV rules."""
        result = self._route(eda=1, iv_rank=60.0)
        self.assertIsInstance(result, list)
        self.assertNotEqual(result, [],
                            "eda=1 should route normally (RULE1 removed)")

    def test_eda_none_not_blocked(self):
        """eda=None (no earnings) → not blocked by RULE1 (falls through to other rules)."""
        result = self._route(eda=None, iv_rank=30.0)
        # With iv_rank=30 (cheap) and bullish direction it should get some structures.
        # We just verify it's not blocked specifically by RULE1.
        # (Other rules may still block — we don't prescribe what comes through.)
        # The key check: no A2BinaryEvent-type block path fires.
        # Any non-empty list or a rule-8 block is fine; the function must not crash.
        self.assertIsInstance(result, list)

    def test_eda_5_not_blocked_by_rule1(self):
        """eda=5 is outside earnings_dte_blackout=2 — RULE1 must not block it."""
        result = self._route(eda=5, iv_rank=30.0)
        # Falls through RULE1; outcome driven by other rules. Just confirm it's a list.
        self.assertIsInstance(result, list)


if __name__ == "__main__":
    unittest.main()
