"""
test_memory_fixes.py — T-004, T-007, T-008 correctness tests.

Suite T004: decisions rolling window = 500 (not 20)
Suite T007: regime_score persisted in every decision record
Suite T008: strategy + sector tags in action records; by_sector/by_strategy buckets populated
"""

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── stubs ─────────────────────────────────────────────────────────────────────

def _ensure_trade_memory_stub() -> None:
    if "trade_memory" not in sys.modules:
        m = types.ModuleType("trade_memory")
        m.save_trade_memory = lambda *a, **kw: ""
        m.update_trade_outcome = lambda *a, **kw: None
        sys.modules["trade_memory"] = m


def _ensure_dotenv_stub() -> None:
    if "dotenv" not in sys.modules:
        m = types.ModuleType("dotenv")
        m.load_dotenv = lambda *a, **kw: None
        sys.modules["dotenv"] = m


def _import_memory():
    _ensure_dotenv_stub()
    _ensure_trade_memory_stub()
    if "memory" in sys.modules:
        return sys.modules["memory"]
    import memory as mem
    return mem


# ── T-004: window size ────────────────────────────────────────────────────────

class TestDecisionsWindow(unittest.TestCase):

    def test_max_decisions_constant_is_500(self):
        mem = _import_memory()
        self.assertEqual(mem._MAX_DECISIONS, 500)

    def test_window_trims_to_500_after_501_saves(self):
        mem = _import_memory()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            df  = tmp / "decisions.json"
            with (
                patch.object(mem, "MEMORY_DIR", tmp),
                patch.object(mem, "DECISIONS_FILE", df),
                patch.object(mem, "_get_active_strategy", return_value="hybrid"),
            ):
                for i in range(501):
                    mem.save_decision(
                        {"actions": [], "reasoning": f"r{i}", "regime_view": "neutral"},
                        "market",
                    )
            stored = json.loads(df.read_text())
            self.assertEqual(len(stored), 500)

    def test_window_retains_most_recent_entries(self):
        mem = _import_memory()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            df  = tmp / "decisions.json"
            with (
                patch.object(mem, "MEMORY_DIR", tmp),
                patch.object(mem, "DECISIONS_FILE", df),
                patch.object(mem, "_get_active_strategy", return_value="hybrid"),
            ):
                for i in range(510):
                    mem.save_decision(
                        {"actions": [], "reasoning": f"r{i}", "regime_view": "neutral"},
                        "market",
                    )
            stored = json.loads(df.read_text())
            self.assertEqual(stored[-1]["reasoning"], "r509")
            self.assertEqual(stored[0]["reasoning"], "r10")


# ── T-007: regime_score in decision record ────────────────────────────────────

class TestRegimeScorePersisted(unittest.TestCase):

    def setUp(self):
        self.mem = _import_memory()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.df  = self.tmp / "decisions.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _save_and_read(self, decision: dict) -> dict:
        with (
            patch.object(self.mem, "MEMORY_DIR", self.tmp),
            patch.object(self.mem, "DECISIONS_FILE", self.df),
            patch.object(self.mem, "_get_active_strategy", return_value="hybrid"),
        ):
            self.mem.save_decision(decision, "market")
        return json.loads(self.df.read_text())[-1]

    def test_regime_score_written_when_provided(self):
        rec = self._save_and_read({"actions": [], "regime_view": "neutral", "regime_score": 72})
        self.assertEqual(rec["regime_score"], 72)

    def test_regime_score_none_when_absent(self):
        rec = self._save_and_read({"actions": [], "regime_view": "neutral"})
        self.assertIn("regime_score", rec)
        self.assertIsNone(rec["regime_score"])

    def test_regime_score_zero_stored_correctly(self):
        rec = self._save_and_read({"actions": [], "regime_view": "risk_off", "regime_score": 0})
        self.assertEqual(rec["regime_score"], 0)

    def test_regime_score_100_stored_correctly(self):
        rec = self._save_and_read({"actions": [], "regime_view": "risk_on", "regime_score": 100})
        self.assertEqual(rec["regime_score"], 100)


# ── T-008: strategy + sector fields in action records ─────────────────────────

class TestActionTags(unittest.TestCase):

    def setUp(self):
        self.mem = _import_memory()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.df  = self.tmp / "decisions.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _save_actions(self, actions: list, strategy: str = "hybrid") -> list:
        with (
            patch.object(self.mem, "MEMORY_DIR", self.tmp),
            patch.object(self.mem, "DECISIONS_FILE", self.df),
            patch.object(self.mem, "_get_active_strategy", return_value=strategy),
        ):
            self.mem.save_decision({"actions": actions, "regime_view": "neutral"}, "market")
        return json.loads(self.df.read_text())[-1]["actions"]

    def test_known_symbol_gets_correct_sector(self):
        acts = self._save_actions([{"action": "buy", "symbol": "NVDA"}])
        self.assertEqual(acts[0]["sector"], "Technology")

    def test_unknown_symbol_returns_unknown(self):
        acts = self._save_actions([{"action": "buy", "symbol": "ZZZZ"}])
        self.assertEqual(acts[0]["sector"], "unknown")

    def test_crypto_symbol_slash_format_gets_crypto(self):
        acts = self._save_actions([{"action": "buy", "symbol": "BTC/USD"}])
        self.assertEqual(acts[0]["sector"], "Crypto")

    def test_crypto_symbol_alpaca_format_gets_crypto(self):
        acts = self._save_actions([{"action": "buy", "symbol": "BTCUSD"}])
        self.assertEqual(acts[0]["sector"], "Crypto")

    def test_strategy_field_written_from_active_strategy(self):
        acts = self._save_actions([{"action": "buy", "symbol": "GLD"}], strategy="momentum")
        self.assertEqual(acts[0]["strategy"], "momentum")

    def test_missing_symbol_key_returns_unknown_without_raising(self):
        acts = self._save_actions([{"action": "buy"}])
        self.assertEqual(acts[0]["sector"], "unknown")

    def test_multiple_actions_each_get_sector(self):
        acts = self._save_actions([
            {"action": "buy", "symbol": "NVDA"},
            {"action": "buy", "symbol": "XLE"},
            {"action": "buy", "symbol": "UNKNOWN99"},
        ])
        self.assertEqual(acts[0]["sector"], "Technology")
        self.assertEqual(acts[1]["sector"], "Energy")
        self.assertEqual(acts[2]["sector"], "unknown")


# ── T-008: performance buckets populated ─────────────────────────────────────

class TestPerformanceBuckets(unittest.TestCase):
    """update_outcomes_from_alpaca() must populate by_sector and by_strategy."""

    def setUp(self):
        self.mem = _import_memory()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.df  = self.tmp / "decisions.json"
        self.pf  = self.tmp / "performance.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _write_decision(self, symbol: str, sector: str, strategy: str,
                         stop_loss: float, take_profit: float) -> None:
        record = [{
            "ts": "2026-04-18T10:00:00+00:00",
            "session": "market",
            "regime": "neutral",
            "regime_score": 60,
            "n_actions": 1,
            "vector_id": "",
            "decision_id": "",
            "actions": [{
                "action": "buy", "symbol": symbol, "qty": 10,
                "stop_loss": stop_loss, "take_profit": take_profit,
                "tier": "core", "catalyst": "test", "sector_signal": None,
                "confidence": 0.8, "strategy": strategy, "sector": sector,
                "option_strategy": None, "expiration": None,
                "long_strike": None, "short_strike": None, "max_cost_usd": None,
                "outcome": None, "pnl": None,
            }],
        }]
        self.df.write_text(json.dumps(record))
        self.pf.write_text(json.dumps(self.mem._empty_perf()))

    def _run_update(self, symbol: str, fill_price: float) -> dict:
        # T-025: outcomes now require a SELL fill (exit fill), not a BUY fill.
        mock_order = MagicMock()
        mock_order.side = "sell"
        mock_order.filled_avg_price = str(fill_price)
        mock_order.filled_qty = "10"
        mock_order.symbol = symbol
        mock_order.id = "test-id"
        mock_order.status = "filled"

        with (
            patch.object(self.mem, "MEMORY_DIR", self.tmp),
            patch.object(self.mem, "DECISIONS_FILE", self.df),
            patch.object(self.mem, "PERF_FILE", self.pf),
            patch.dict(os.environ, {"ALPACA_API_KEY": "x", "ALPACA_SECRET_KEY": "y"}),
            patch("memory.TradingClient") as mock_tc,
            patch("memory.OrderSide") as mock_os,
            patch("memory.QueryOrderStatus") as mock_qs,
            patch("memory.trade_memory") as mock_tm,
        ):
            mock_os.SELL = "sell"
            mock_os.BUY  = "buy"
            mock_qs.CLOSED = "closed"
            mock_tc.return_value.get_orders.return_value = [mock_order]
            mock_tm.update_trade_outcome = MagicMock()
            self.mem.update_outcomes_from_alpaca()

        return json.loads(self.pf.read_text())

    def test_by_sector_populated_for_win(self):
        self._write_decision("NVDA", "Technology", "hybrid",
                             stop_loss=150.0, take_profit=200.0)
        perf = self._run_update("NVDA", fill_price=200.0)  # ≥ 200*0.99=198 → win
        self.assertIn("Technology", perf["by_sector"])
        self.assertEqual(perf["by_sector"]["Technology"]["wins"], 1)

    def test_by_strategy_populated_for_stock_win(self):
        self._write_decision("NVDA", "Technology", "hybrid",
                             stop_loss=150.0, take_profit=200.0)
        perf = self._run_update("NVDA", fill_price=200.0)
        self.assertIn("hybrid", perf["by_strategy"])
        self.assertEqual(perf["by_strategy"]["hybrid"]["wins"], 1)

    def test_by_sector_populated_for_loss(self):
        self._write_decision("XLE", "Energy", "momentum",
                             stop_loss=50.0, take_profit=70.0)
        perf = self._run_update("XLE", fill_price=50.0)  # ≤ 50*1.01=50.5 → loss
        self.assertIn("Energy", perf["by_sector"])
        self.assertEqual(perf["by_sector"]["Energy"]["losses"], 1)

    def test_sector_fallback_to_map_when_field_absent(self):
        """by_sector still works for old records that lack a sector field."""
        record = [{
            "ts": "2026-04-18T10:00:00+00:00",
            "session": "market",
            "regime": "neutral",
            "regime_score": None,
            "n_actions": 1,
            "vector_id": "",
            "decision_id": "",
            "actions": [{
                "action": "buy", "symbol": "GLD", "qty": 1,
                "stop_loss": 400.0, "take_profit": 500.0,
                "tier": "core", "catalyst": "test", "sector_signal": None,
                "confidence": 0.8,
                # No "strategy" or "sector" fields (old-format record)
                "option_strategy": None, "expiration": None,
                "long_strike": None, "short_strike": None, "max_cost_usd": None,
                "outcome": None, "pnl": None,
            }],
        }]
        self.df.write_text(json.dumps(record))
        self.pf.write_text(json.dumps(self.mem._empty_perf()))
        perf = self._run_update("GLD", fill_price=500.0)  # win
        self.assertIn("Commodities", perf["by_sector"])


if __name__ == "__main__":
    unittest.main()
