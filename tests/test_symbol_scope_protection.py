"""
tests/test_symbol_scope_protection.py

Symbol-scoped protection_missing response.

Replaces account-scope HALT for single/pair-symbol unprotected cases with
exit_manager.attempt_repair_or_sell. Account-scope HALT remains the response
for 3+ simultaneously unprotected symbols (systemic failure).

Two layers under test:
  1. divergence.detect_protection_divergence — counts unprotected post-grace
     and routes 1–2 to symbol-scoped repair, 3+ to legacy HALT.
  2. exit_manager.attempt_repair_or_sell — repair → market sell → flag for
     open → CRITICAL alert ladder.
"""

from __future__ import annotations

import json
import time
import unittest
from unittest.mock import MagicMock, patch

import divergence as div
import exit_manager as em
from divergence import detect_protection_divergence

# ── helpers ────────────────────────────────────────────────────────────────

def _pos(symbol: str, qty: float = 100.0, market_value: float = 10000.0):
    p = MagicMock()
    p.symbol = symbol
    p.qty = str(qty)
    p.market_value = market_value
    p.current_price = "100.0"
    return p


def _order(
    symbol: str,
    side: str = "sell",
    order_type: str = "stop",
    order_class: str = "simple",
    status: str = "accepted",
    qty: float = 100.0,
    stop_price: float | None = 95.0,
    order_id: str = "ord-001",
):
    o = MagicMock()
    o.symbol = symbol
    o.side = side
    o.order_type = order_type
    o.order_class = order_class
    o.status = status
    o.qty = str(qty)
    o.stop_price = stop_price
    o.id = order_id
    return o


def _seed_miss(sym: str) -> None:
    """Pre-seed grace + miss-cycle state so the next call enters firing."""
    div._fill_seen[sym] = time.time() - 1000
    div._protection_miss_cycles[sym] = 1


class _Base(unittest.TestCase):
    """Clear module-level divergence state and bypass startup grace."""

    def setUp(self):
        div._fill_seen.clear()
        div._protection_miss_cycles.clear()
        self._startup_patch = patch.object(
            div, "_STARTUP_EPOCH", time.time() - 10_000
        )
        self._startup_patch.start()

    def tearDown(self):
        self._startup_patch.stop()
        div._fill_seen.clear()
        div._protection_miss_cycles.clear()


# ── Symbol-scoped halt tests (divergence-side dispatch) ───────────────────

class TestSymbolScopedDispatch(_Base):

    def test_single_unprotected_no_halt(self):
        """1 unprotected symbol → no HALT events, repair_or_sell called once."""
        pos = _pos("STNG", qty=188.0, market_value=5500.0)
        _seed_miss("STNG")
        alpaca = MagicMock()
        with patch.object(em, "attempt_repair_or_sell") as mock_ros:
            events = detect_protection_divergence(
                account="A1",
                positions=[pos],
                open_orders=[],
                grace_seconds=0,
                alpaca_client=alpaca,
                strategy_config={"exit_management": {}},
                market_is_open=True,
            )
        self.assertEqual(events, [])
        self.assertEqual(mock_ros.call_count, 1)
        self.assertEqual(mock_ros.call_args.kwargs["symbol"], "STNG")

    def test_two_unprotected_no_halt(self):
        """2 unprotected → no HALT events, repair_or_sell called twice."""
        p1 = _pos("STNG", qty=100.0, market_value=5500.0)
        p2 = _pos("GOOGL", qty=50.0, market_value=10000.0)
        _seed_miss("STNG")
        _seed_miss("GOOGL")
        alpaca = MagicMock()
        with patch.object(em, "attempt_repair_or_sell") as mock_ros:
            events = detect_protection_divergence(
                account="A1",
                positions=[p1, p2],
                open_orders=[],
                grace_seconds=0,
                alpaca_client=alpaca,
                strategy_config={"exit_management": {}},
                market_is_open=True,
            )
        self.assertEqual(events, [])
        self.assertEqual(mock_ros.call_count, 2)
        called_syms = {c.kwargs["symbol"] for c in mock_ros.call_args_list}
        self.assertEqual(called_syms, {"STNG", "GOOGL"})

    def test_three_unprotected_halts(self):
        """3 unprotected → legacy HALT path: events emitted, no repair_or_sell."""
        p1 = _pos("STNG", qty=100.0, market_value=5500.0)
        p2 = _pos("GOOGL", qty=50.0, market_value=10000.0)
        p3 = _pos("AAPL", qty=10.0, market_value=2500.0)
        _seed_miss("STNG")
        _seed_miss("GOOGL")
        _seed_miss("AAPL")
        alpaca = MagicMock()
        with patch.object(em, "attempt_repair_or_sell") as mock_ros:
            events = detect_protection_divergence(
                account="A1",
                positions=[p1, p2, p3],
                open_orders=[],
                grace_seconds=0,
                alpaca_client=alpaca,
                strategy_config={"exit_management": {}},
                market_is_open=True,
            )
        self.assertEqual(mock_ros.call_count, 0)
        protect_events = [
            e for e in events
            if e.event_type in ("protection_missing", "stop_missing")
        ]
        self.assertEqual(len(protect_events), 3)
        emitted = {e.symbol for e in protect_events}
        self.assertEqual(emitted, {"STNG", "GOOGL", "AAPL"})

    def test_zero_unprotected_continues(self):
        """All protected → no events, repair_or_sell never called."""
        p = _pos("AAPL", qty=50.0, market_value=10000.0)
        stop = _order(
            "AAPL", side="sell", order_type="stop", status="new",
            qty=50.0, stop_price=180.0,
        )
        alpaca = MagicMock()
        with patch.object(em, "attempt_repair_or_sell") as mock_ros:
            events = detect_protection_divergence(
                account="A1",
                positions=[p],
                open_orders=[stop],
                grace_seconds=0,
                alpaca_client=alpaca,
                strategy_config={"exit_management": {}},
                market_is_open=True,
            )
        self.assertEqual(events, [])
        self.assertEqual(mock_ros.call_count, 0)

    def test_pending_close_counts_as_protected(self):
        """Symbol with pending full-qty SELL never enters unprotected list."""
        p = _pos("STNG", qty=188.0, market_value=5500.0)
        sell = _order(
            "STNG", side="sell", order_type="market",
            qty=188.0, stop_price=None,
        )
        alpaca = MagicMock()
        with patch.object(em, "attempt_repair_or_sell") as mock_ros:
            events = detect_protection_divergence(
                account="A1",
                positions=[p],
                open_orders=[sell],
                grace_seconds=0,
                alpaca_client=alpaca,
                strategy_config={"exit_management": {}},
                market_is_open=True,
            )
        self.assertEqual(events, [])
        self.assertEqual(mock_ros.call_count, 0)


# ── Repair/sell fallback tests (exit_manager-side) ────────────────────────

class TestAttemptRepairOrSell(unittest.TestCase):

    def setUp(self):
        # Re-route the pending-sells JSON to a temp path per test so the
        # production runtime file is never touched.
        import tempfile
        self._tmpdir = tempfile.mkdtemp(prefix="psells_")
        self._path_patch = patch.object(
            em,
            "_PENDING_PROTECTION_SELLS_PATH",
            __import__("pathlib").Path(self._tmpdir) / "pending.json",
        )
        self._path_patch.start()

    def tearDown(self):
        self._path_patch.stop()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _read_file(self) -> dict:
        p = em._PENDING_PROTECTION_SELLS_PATH
        if not p.exists():
            return {}
        return json.loads(p.read_text())

    def test_repair_succeeds_no_sell(self):
        """Repair returns True → no submit_order, no file write."""
        pos = MagicMock()
        pos.symbol = "STNG"
        pos.qty = "100"
        alpaca = MagicMock()
        with patch.object(em, "refresh_exits_for_position", return_value=True):
            result = em.attempt_repair_or_sell(
                symbol="STNG",
                position=pos,
                alpaca_client=alpaca,
                strategy_config={},
                market_is_open=True,
            )
        self.assertTrue(result)
        self.assertEqual(alpaca.submit_order.call_count, 0)
        self.assertEqual(self._read_file(), {})

    def test_repair_fails_market_open_places_sell(self):
        """Repair False + market open → submit_order(MarketOrderRequest) called."""
        pos = MagicMock()
        pos.symbol = "STNG"
        pos.qty = "188"
        alpaca = MagicMock()
        sell_response = MagicMock()
        sell_response.id = "fallback-sell-1"
        alpaca.submit_order.return_value = sell_response
        with patch.object(em, "refresh_exits_for_position", return_value=False):
            result = em.attempt_repair_or_sell(
                symbol="STNG",
                position=pos,
                alpaca_client=alpaca,
                strategy_config={},
                market_is_open=True,
            )
        self.assertFalse(result)
        self.assertEqual(alpaca.submit_order.call_count, 1)
        from alpaca.trading.requests import MarketOrderRequest
        sent = alpaca.submit_order.call_args.args[0]
        self.assertIsInstance(sent, MarketOrderRequest)
        self.assertEqual(sent.symbol, "STNG")
        self.assertEqual(sent.qty, 188.0)
        data = self._read_file()
        self.assertIn("STNG", data)
        self.assertEqual(data["STNG"]["market_status"], "sell_placed")
        self.assertEqual(data["STNG"]["reason"], "repair_failed")
        self.assertEqual(data["STNG"]["qty"], 188.0)

    def test_repair_fails_market_closed_flags(self):
        """Repair False + market closed → no submit_order, file flagged pending_open."""
        pos = MagicMock()
        pos.symbol = "GOOGL"
        pos.qty = "50"
        alpaca = MagicMock()
        with patch.object(em, "refresh_exits_for_position", return_value=False):
            result = em.attempt_repair_or_sell(
                symbol="GOOGL",
                position=pos,
                alpaca_client=alpaca,
                strategy_config={},
                market_is_open=False,
            )
        self.assertFalse(result)
        self.assertEqual(alpaca.submit_order.call_count, 0)
        data = self._read_file()
        self.assertIn("GOOGL", data)
        self.assertEqual(data["GOOGL"]["market_status"], "pending_open")
        self.assertEqual(
            data["GOOGL"]["reason"], "repair_failed_market_closed"
        )
        self.assertEqual(data["GOOGL"]["qty"], 50.0)

    def test_both_fail_fires_alert(self):
        """Repair False + submit_order raises → CRITICAL log + safety alert + hard_failure flag."""
        pos = MagicMock()
        pos.symbol = "STNG"
        pos.qty = "100"
        alpaca = MagicMock()
        alpaca.submit_order.side_effect = RuntimeError("alpaca rejected")
        with patch.object(em, "refresh_exits_for_position", return_value=False), \
             patch.object(em, "_fire_safety_alert") as mock_alert:
            result = em.attempt_repair_or_sell(
                symbol="STNG",
                position=pos,
                alpaca_client=alpaca,
                strategy_config={},
                market_is_open=True,
            )
        self.assertFalse(result)
        self.assertEqual(mock_alert.call_count, 1)
        alert_name = mock_alert.call_args.args[0]
        self.assertIn("repair_and_sell_failed_STNG", alert_name)
        data = self._read_file()
        self.assertEqual(data["STNG"]["market_status"], "hard_failure")
        self.assertEqual(data["STNG"]["reason"], "repair_and_sell_failed")

    def test_pending_sells_json_written_correctly(self):
        """File schema: dict keyed by symbol; entries carry qty/reason/flagged_at/market_status."""
        pos = MagicMock()
        pos.symbol = "AAPL"
        pos.qty = "25"
        alpaca = MagicMock()
        with patch.object(em, "refresh_exits_for_position", return_value=False):
            em.attempt_repair_or_sell(
                symbol="AAPL",
                position=pos,
                alpaca_client=alpaca,
                strategy_config={},
                market_is_open=False,
            )
        data = self._read_file()
        self.assertIn("AAPL", data)
        entry = data["AAPL"]
        for k in ("qty", "reason", "flagged_at", "market_status"):
            self.assertIn(k, entry)
        self.assertEqual(entry["qty"], 25.0)
        # flagged_at is an ISO-8601 timestamp
        self.assertIn("T", entry["flagged_at"])


if __name__ == "__main__":
    unittest.main()
