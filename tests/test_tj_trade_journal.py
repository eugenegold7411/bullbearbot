"""
tests/test_tj_trade_journal.py — Trade Journal unit tests.

TJ-01  build_closed_trades([], []) returns []
TJ-02  Single BUY→SELL pair produces one trade with all required fields
TJ-03  BUY with no matching SELL is excluded (open position)
TJ-04  SELL with no matching BUY is excluded (orphaned sell)
TJ-05  P&L and pnl_pct calculated correctly (entry 100, exit 110, qty 10)
TJ-06  BUG-OCA-001 flag applied: AMZN entered 2026-04-13 to 2026-04-15
TJ-07  BUG-OCA-001 flag NOT applied: AMZN entered after 2026-04-15
TJ-08  BUG-OCA-001 flag NOT applied: SPY entered 2026-04-14 (symbol not in affects_symbols)
TJ-09  BUG-DENOM-001 applies to all symbols (empty affects_symbols means all)
TJ-10  Decision enrichment: catalyst and reasoning attached when decision matches
TJ-11  Decision enrichment: graceful when no decision found (None fields)
TJ-12  build_bug_fix_log() returns >= 3 bugs; HIGH severity before MEDIUM
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

# Stub third-party imports so trade_journal imports cleanly in CI
import unittest
for _mod in ["dotenv", "alpaca", "alpaca.trading", "alpaca.trading.client",
             "alpaca.trading.requests", "alpaca.trading.enums"]:
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

from trade_journal import (
    KNOWN_BUG_PERIODS,
    build_bug_fix_log,
    build_closed_trades,
    _apply_bug_flags,
    _find_entry_decision,
    _parse_orders,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _order(
    symbol: str,
    side: str,
    price: str,
    qty: str = "10",
    filled_at: str = "2026-04-20T14:00:00+00:00",
    status: str = "filled",
) -> dict:
    return {
        "id": f"ord-{symbol}-{side}",
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "filled_qty": qty,
        "filled_avg_price": price,
        "status": status,
        "filled_at": filled_at,
    }


def _decision(
    ts: str,
    symbol: str,
    catalyst: str = "earnings_beat",
    reasoning: str = "strong thesis",
    decision_id: str = "dec-001",
) -> dict:
    return {
        "ts": ts,
        "session": "market",
        "regime": "risk_on",
        "regime_score": 65,
        "reasoning": reasoning,
        "decision_id": decision_id,
        "actions": [
            {
                "action": "buy",
                "symbol": symbol,
                "catalyst": catalyst,
                "catalyst_type": "earnings_beat",
                "tier": "core",
                "confidence": 0.80,
            }
        ],
    }


# ── TJ-01 ─────────────────────────────────────────────────────────────────────

class TestTJ01EmptyInput(unittest.TestCase):
    def test_empty_orders_returns_empty_list(self):
        result = build_closed_trades(orders=[], decisions=[])
        self.assertEqual(result, [])


# ── TJ-02 ─────────────────────────────────────────────────────────────────────

class TestTJ02SinglePair(unittest.TestCase):
    def test_single_pair_produces_one_trade_with_required_fields(self):
        orders = [
            _order("AAPL", "buy", "150.00", filled_at="2026-04-18T10:00:00+00:00"),
            _order("AAPL", "sell", "160.00", filled_at="2026-04-25T15:00:00+00:00"),
        ]
        result = build_closed_trades(orders=orders, decisions=[])
        self.assertEqual(len(result), 1)
        t = result[0]
        required = [
            "symbol", "entry_price", "exit_price", "qty", "pnl", "pnl_pct",
            "outcome", "entry_time", "exit_time", "holding_days",
            "tier", "catalyst", "reasoning", "decision_id", "bug_flags",
        ]
        for field in required:
            self.assertIn(field, t, f"Missing field: {field}")
        self.assertEqual(t["symbol"], "AAPL")
        self.assertEqual(t["entry_price"], 150.0)
        self.assertEqual(t["exit_price"], 160.0)


# ── TJ-03 ─────────────────────────────────────────────────────────────────────

class TestTJ03UnmatchedBuy(unittest.TestCase):
    def test_buy_with_no_sell_excluded(self):
        orders = [_order("MSFT", "buy", "400.00")]
        result = build_closed_trades(orders=orders, decisions=[])
        self.assertEqual(result, [])


# ── TJ-04 ─────────────────────────────────────────────────────────────────────

class TestTJ04OrphanedSell(unittest.TestCase):
    def test_sell_with_no_buy_excluded(self):
        orders = [_order("NVDA", "sell", "900.00")]
        result = build_closed_trades(orders=orders, decisions=[])
        self.assertEqual(result, [])


# ── TJ-05 ─────────────────────────────────────────────────────────────────────

class TestTJ05PnLCalculation(unittest.TestCase):
    def test_pnl_and_pct_correct(self):
        orders = [
            _order("SPY", "buy", "100.00", qty="10", filled_at="2026-04-18T10:00:00+00:00"),
            _order("SPY", "sell", "110.00", qty="10", filled_at="2026-04-25T15:00:00+00:00"),
        ]
        result = build_closed_trades(orders=orders, decisions=[])
        self.assertEqual(len(result), 1)
        t = result[0]
        self.assertAlmostEqual(t["pnl"], 100.0)  # 10 * (110 - 100)
        self.assertAlmostEqual(t["pnl_pct"], 10.0)
        self.assertEqual(t["outcome"], "win")

    def test_losing_trade_outcome_is_loss(self):
        orders = [
            _order("XLE", "buy", "100.00", qty="5", filled_at="2026-04-18T10:00:00+00:00"),
            _order("XLE", "sell", "90.00", qty="5", filled_at="2026-04-20T15:00:00+00:00"),
        ]
        result = build_closed_trades(orders=orders, decisions=[])
        self.assertEqual(len(result), 1)
        t = result[0]
        self.assertAlmostEqual(t["pnl"], -50.0)  # 5 * (90 - 100)
        self.assertEqual(t["outcome"], "loss")


# ── TJ-06 ─────────────────────────────────────────────────────────────────────

class TestTJ06BugFlagApplied(unittest.TestCase):
    def test_amzn_in_bug_oca_period_flagged(self):
        # AMZN entered 2026-04-13 (bot launch), sold 2026-04-16
        orders = [
            _order("AMZN", "buy", "250.00", filled_at="2026-04-13T10:00:00+00:00"),
            _order("AMZN", "sell", "255.00", filled_at="2026-04-16T15:00:00+00:00"),
        ]
        result = build_closed_trades(orders=orders, decisions=[])
        self.assertEqual(len(result), 1)
        self.assertIn("BUG-OCA-001", result[0]["bug_flags"])


# ── TJ-07 ─────────────────────────────────────────────────────────────────────

class TestTJ07BugFlagNotAppliedAfterPeriod(unittest.TestCase):
    def test_amzn_entered_after_bug_period_not_flagged(self):
        # AMZN entered 2026-04-20 — well after BUG-OCA-001 fix (2026-04-15)
        orders = [
            _order("AMZN", "buy", "260.00", filled_at="2026-04-20T10:00:00+00:00"),
            _order("AMZN", "sell", "264.00", filled_at="2026-04-30T15:00:00+00:00"),
        ]
        result = build_closed_trades(orders=orders, decisions=[])
        self.assertEqual(len(result), 1)
        self.assertNotIn("BUG-OCA-001", result[0]["bug_flags"])


# ── TJ-08 ─────────────────────────────────────────────────────────────────────

class TestTJ08BugFlagSymbolFilter(unittest.TestCase):
    def test_spy_not_flagged_by_oca_bug_during_bug_period(self):
        # SPY entered during BUG-OCA-001 window but SPY not in affects_symbols
        orders = [
            _order("SPY", "buy", "500.00", filled_at="2026-04-14T10:00:00+00:00"),
            _order("SPY", "sell", "510.00", filled_at="2026-04-16T15:00:00+00:00"),
        ]
        result = build_closed_trades(orders=orders, decisions=[])
        self.assertEqual(len(result), 1)
        self.assertNotIn("BUG-OCA-001", result[0]["bug_flags"])


# ── TJ-09 ─────────────────────────────────────────────────────────────────────

class TestTJ09DenomBugAllSymbols(unittest.TestCase):
    def test_denom_bug_applies_to_any_symbol_in_window(self):
        # BUG-DENOM-001 has affects_symbols=[] meaning all symbols
        flags = _apply_bug_flags(
            "QQQ",
            entry_time=__import__("datetime").datetime(2026, 4, 20, 10, 0, 0,
                tzinfo=__import__("datetime").timezone.utc),
            exit_time=__import__("datetime").datetime(2026, 4, 28, 15, 0, 0,
                tzinfo=__import__("datetime").timezone.utc),
        )
        self.assertIn("BUG-DENOM-001", flags)

    def test_denom_bug_applies_regardless_of_symbol(self):
        from datetime import datetime, timezone
        for sym in ("AAPL", "GLD", "XBI", "BTC/USD"):
            flags = _apply_bug_flags(
                sym,
                entry_time=datetime(2026, 4, 20, 10, 0, 0, tzinfo=timezone.utc),
                exit_time=datetime(2026, 4, 28, 15, 0, 0, tzinfo=timezone.utc),
            )
            self.assertIn("BUG-DENOM-001", flags, f"{sym} should have DENOM flag")


# ── TJ-10 ─────────────────────────────────────────────────────────────────────

class TestTJ10DecisionEnrichment(unittest.TestCase):
    def test_catalyst_and_reasoning_attached_when_decision_matches(self):
        orders = [
            _order("AAPL", "buy", "180.00", filled_at="2026-04-22T14:05:00+00:00"),
            _order("AAPL", "sell", "190.00", filled_at="2026-04-28T15:00:00+00:00"),
        ]
        decisions = [
            _decision(
                ts="2026-04-22T13:58:00+00:00",  # 7 minutes before fill — within 30 min window
                symbol="AAPL",
                catalyst="AAPL earnings beat Q1 2026",
                reasoning="Strong tech narrative, AI monetization beat expectations.",
                decision_id="dec-aapl-001",
            )
        ]
        result = build_closed_trades(orders=orders, decisions=decisions)
        self.assertEqual(len(result), 1)
        t = result[0]
        self.assertEqual(t["catalyst"], "AAPL earnings beat Q1 2026")
        self.assertIn("AI monetization", t["reasoning"])
        self.assertEqual(t["decision_id"], "dec-aapl-001")
        self.assertEqual(t["tier"], "core")


# ── TJ-11 ─────────────────────────────────────────────────────────────────────

class TestTJ11DecisionMissGraceful(unittest.TestCase):
    def test_no_matching_decision_returns_none_fields(self):
        orders = [
            _order("GS", "buy", "500.00", filled_at="2026-04-22T14:00:00+00:00"),
            _order("GS", "sell", "510.00", filled_at="2026-04-25T15:00:00+00:00"),
        ]
        decisions = [
            # Decision for a different symbol — should not match
            _decision(ts="2026-04-22T13:58:00+00:00", symbol="JPM")
        ]
        result = build_closed_trades(orders=orders, decisions=decisions)
        self.assertEqual(len(result), 1)
        t = result[0]
        self.assertIsNone(t["catalyst"])
        self.assertIsNone(t["reasoning"])
        self.assertIsNone(t["decision_id"])

    def test_decision_too_old_not_matched(self):
        # Decision is 2 hours before fill — outside 30-minute window
        orders = [
            _order("MSFT", "buy", "400.00", filled_at="2026-04-22T14:00:00+00:00"),
            _order("MSFT", "sell", "410.00", filled_at="2026-04-25T15:00:00+00:00"),
        ]
        decisions = [
            _decision(ts="2026-04-22T12:00:00+00:00", symbol="MSFT")  # 2h before
        ]
        result = build_closed_trades(orders=orders, decisions=decisions)
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0]["catalyst"])


# ── TJ-12 ─────────────────────────────────────────────────────────────────────

class TestTJ12BugFixLog(unittest.TestCase):
    def test_returns_at_least_three_bugs(self):
        log = build_bug_fix_log()
        self.assertGreaterEqual(len(log), 3)

    def test_all_bugs_have_required_fields(self):
        log = build_bug_fix_log()
        required = ["id", "title", "description", "severity", "start", "end",
                    "trading_impact", "resolution"]
        for bug in log:
            for field in required:
                self.assertIn(field, bug, f"Bug {bug.get('id')} missing field: {field}")

    def test_high_severity_before_medium(self):
        log = build_bug_fix_log()
        severities = [b["severity"] for b in log]
        last_high = max((i for i, s in enumerate(severities) if s == "HIGH"), default=-1)
        first_medium = min((i for i, s in enumerate(severities) if s == "MEDIUM"), default=9999)
        self.assertLess(last_high, first_medium, "All HIGH entries should precede MEDIUM entries")

    def test_oca_and_denom_bugs_present(self):
        ids = {b["id"] for b in build_bug_fix_log()}
        self.assertIn("BUG-OCA-001", ids)
        self.assertIn("BUG-DENOM-001", ids)

    def test_known_bug_periods_constant_is_list(self):
        self.assertIsInstance(KNOWN_BUG_PERIODS, list)
        self.assertGreater(len(KNOWN_BUG_PERIODS), 0)


if __name__ == "__main__":
    unittest.main()
