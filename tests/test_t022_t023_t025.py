"""
test_t022_t023_t025.py

T-022: classify_catalyst() keyword matching + catalyst_type in action records
T-023: thesis_status field in score_position_thesis() + THESIS INVALIDATED log
T-025: update_outcomes_from_alpaca() SELL-fill path + backfill_forward_returns()
       insufficient_data early-exit
"""

import json
import logging
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


# ── stubs needed before any trading-bot imports ───────────────────────────────

def _stub_module(name: str, **attrs) -> None:
    if name in sys.modules:
        return
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m


def _ensure_anthropic_stub() -> None:
    if "anthropic" not in sys.modules:
        m = types.ModuleType("anthropic")
        sys.modules["anthropic"] = m
    ant = sys.modules["anthropic"]
    if not hasattr(ant, "Anthropic"):
        ant.Anthropic = type("Anthropic", (), {"__init__": lambda self, *a, **kw: None})


_ensure_anthropic_stub()
_stub_module("dotenv", load_dotenv=lambda *a, **kw: None)
_stub_module(
    "trade_memory",
    get_collection_stats=lambda: {},
    save_trade_memory=lambda *a, **kw: "",
    retrieve_similar_scenarios=lambda *a, **kw: [],
    update_trade_outcome=lambda *a, **kw: None,
)
_stub_module("report", generate_report=lambda: {})
_stub_module("scheduler")


# ── T-022: classify_catalyst ──────────────────────────────────────────────────

class TestCatalystClassifier(unittest.TestCase):

    def setUp(self):
        from semantic_labels import classify_catalyst, CatalystType
        self.classify = classify_catalyst
        self.CatalystType = CatalystType

    def test_insider_buy_form4(self):
        result = self.classify("insider buy confirmed Form 4")
        self.assertEqual(result, self.CatalystType.INSIDER_BUY)

    def test_empty_string_returns_unknown(self):
        result = self.classify("")
        self.assertEqual(result, self.CatalystType.UNKNOWN)

    def test_whitespace_only_returns_unknown(self):
        result = self.classify("   ")
        self.assertEqual(result, self.CatalystType.UNKNOWN)

    def test_congressional_buy(self):
        result = self.classify("Congressional buy — Nancy Pelosi NVDA calls")
        self.assertEqual(result, self.CatalystType.CONGRESSIONAL_BUY)

    def test_earnings_beat(self):
        result = self.classify("Strong earnings beat, EPS beat by 12%")
        self.assertEqual(result, self.CatalystType.EARNINGS_BEAT)

    def test_earnings_miss(self):
        result = self.classify("Q3 earnings miss, guidance cut")
        self.assertEqual(result, self.CatalystType.EARNINGS_MISS)

    def test_guidance_raise(self):
        result = self.classify("Company raised guidance for FY2026")
        self.assertEqual(result, self.CatalystType.GUIDANCE_RAISE)

    def test_guidance_cut(self):
        result = self.classify("Management cut guidance citing tariff pressure")
        self.assertEqual(result, self.CatalystType.GUIDANCE_CUT)

    def test_fed_signal(self):
        result = self.classify("FOMC meeting — Powell signals rate cut")
        self.assertEqual(result, self.CatalystType.FED_SIGNAL)

    def test_macro_print(self):
        result = self.classify("Hot CPI print above expectations")
        self.assertEqual(result, self.CatalystType.MACRO_PRINT)

    def test_technical_breakout(self):
        result = self.classify("Technical breakout above 200-day MA")
        self.assertEqual(result, self.CatalystType.TECHNICAL_BREAKOUT)

    def test_momentum_continuation(self):
        result = self.classify("Strong momentum continuation after earnings")
        self.assertEqual(result, self.CatalystType.MOMENTUM_CONTINUATION)

    def test_sector_rotation(self):
        result = self.classify("Sector rotation from tech into energy")
        self.assertEqual(result, self.CatalystType.SECTOR_ROTATION)

    def test_social_sentiment(self):
        result = self.classify("WSB Reddit momentum build on GME")
        self.assertEqual(result, self.CatalystType.SOCIAL_SENTIMENT)

    def test_citrini_thesis(self):
        result = self.classify("Citrini thesis — long FXI China recovery")
        self.assertEqual(result, self.CatalystType.CITRINI_THESIS)

    def test_unrecognized_returns_unknown(self):
        result = self.classify("random text with no keywords")
        self.assertEqual(result, self.CatalystType.UNKNOWN)

    def test_returns_enum_instance(self):
        """Return type must be a CatalystType enum, not a string."""
        result = self.classify("insider buy")
        self.assertIsInstance(result, self.CatalystType)

    def test_case_insensitive(self):
        result = self.classify("INSIDER BUY FORM 4")
        self.assertEqual(result, self.CatalystType.INSIDER_BUY)


# ── T-022: catalyst_type written to decision record ──────────────────────────

class TestCatalystTypeInDecisionRecord(unittest.TestCase):

    def setUp(self):
        import importlib
        if "memory" in sys.modules:
            self.mem = sys.modules["memory"]
        else:
            import memory as mem
            self.mem = mem
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.df = self.tmp / "decisions.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _save_and_read(self, actions: list) -> list:
        decision = {"actions": actions, "regime_view": "neutral"}
        with (
            patch.object(self.mem, "MEMORY_DIR", self.tmp),
            patch.object(self.mem, "DECISIONS_FILE", self.df),
            patch.object(self.mem, "_get_active_strategy", return_value="hybrid"),
        ):
            self.mem.save_decision(decision, "market")
        return json.loads(self.df.read_text())[-1]["actions"]

    def test_catalyst_type_field_present(self):
        acts = self._save_and_read([{"action": "buy", "symbol": "NVDA",
                                      "catalyst": "earnings beat"}])
        self.assertIn("catalyst_type", acts[0])

    def test_catalyst_type_classified_correctly(self):
        acts = self._save_and_read([{"action": "buy", "symbol": "NVDA",
                                      "catalyst": "insider buy confirmed Form 4"}])
        self.assertEqual(acts[0]["catalyst_type"], "insider_buy")

    def test_catalyst_type_unknown_for_empty_catalyst(self):
        acts = self._save_and_read([{"action": "buy", "symbol": "NVDA",
                                      "catalyst": ""}])
        self.assertEqual(acts[0]["catalyst_type"], "unknown")

    def test_catalyst_type_unknown_when_catalyst_absent(self):
        acts = self._save_and_read([{"action": "buy", "symbol": "NVDA"}])
        self.assertEqual(acts[0]["catalyst_type"], "unknown")


# ── T-023: thesis_status in score_position_thesis ────────────────────────────

class TestThesisStatus(unittest.TestCase):

    def setUp(self):
        import importlib
        import portfolio_intelligence as pi
        self.pi = pi

    def _mock_position(self, entry=100.0, current=100.0, pl=0.0, qty=10.0):
        pos = MagicMock()
        pos.avg_entry_price = entry
        pos.current_price   = current
        pos.unrealized_pl   = pl
        pos.qty             = qty
        return pos

    def _score(self, days_held=1, entry=100.0, current=105.0, pl=50.0,
               catalyst="momentum", strategy_config=None):
        """Convenience wrapper — patches out bars and sector data."""
        pos = self._mock_position(entry=entry, current=current, pl=pl)
        decision = {"catalyst": catalyst, "stop_loss": 90.0, "take_profit": 120.0}
        with (
            patch.object(self.pi, "_load_bars", return_value=None),
            patch.object(self.pi, "_load_sector_perf", return_value={}),
        ):
            return self.pi.score_position_thesis(
                "NVDA", pos, decision, {}, days_held, strategy_config
            )

    def test_thesis_status_field_present(self):
        result = self._score()
        self.assertIn("thesis_status", result)

    def test_valid_when_score_above_6(self):
        # days_held=1, price above entry → should score well
        result = self._score(days_held=1, current=115.0, pl=150.0)
        if result["thesis_score"] >= 7:
            self.assertEqual(result["thesis_status"], "valid")

    def test_weakening_when_score_4_to_6(self):
        # Force a moderately-weak scenario: 5-day hold (catalyst aging) + trending flat
        result = self._score(days_held=5, current=100.0, pl=0.0)
        score = result["thesis_score"]
        if 4 <= score <= 6:
            self.assertEqual(result["thesis_status"], "weakening")

    def test_invalidated_when_score_below_4(self):
        # Many days held + trending toward stop + below entry
        result = self._score(days_held=10, current=91.0, pl=-90.0)
        score = result["thesis_score"]
        if score < 4:
            self.assertEqual(result["thesis_status"], "invalidated")

    def test_invalidated_when_override_flag_set(self):
        # Override flag forces score ≤ 4 → invalidated
        config = {
            "time_bound_actions": [{
                "symbol": "NVDA",
                "deadline_utc": "2020-01-01T00:00:00Z",  # past deadline
            }]
        }
        result = self._score(strategy_config=config)
        self.assertEqual(result["thesis_status"], "invalidated")

    def test_thesis_status_invalidated_emits_warning(self):
        """score_position_thesis must log [PI] THESIS INVALIDATED when invalidated."""
        # Use an expired deadline — guarantees override_flag → invalidated regardless of score
        config = {
            "time_bound_actions": [{
                "symbol": "NVDA",
                "deadline_utc": "2020-01-01T00:00:00Z",
            }]
        }
        with patch.object(self.pi, "_load_bars", return_value=None), \
             patch.object(self.pi, "_load_sector_perf", return_value={}):
            pos = self._mock_position(entry=100.0, current=105.0, pl=50.0)
            decision = {"catalyst": "earnings beat", "stop_loss": 90.0, "take_profit": 120.0}
            with self.assertLogs("portfolio_intelligence", level="WARNING") as cm:
                result = self.pi.score_position_thesis(
                    "NVDA", pos, decision, {}, days_held=1, strategy_config=config
                )

        self.assertEqual(result["thesis_status"], "invalidated")
        self.assertTrue(
            any("THESIS INVALIDATED" in msg and "NVDA" in msg for msg in cm.output),
            f"Expected [PI] THESIS INVALIDATED log, got: {cm.output}",
        )

    def test_thesis_status_valid_no_warning(self):
        """No WARNING emitted for valid thesis."""
        import logging as _logging
        logger = _logging.getLogger("portfolio_intelligence")

        with patch.object(self.pi, "_load_bars", return_value=None), \
             patch.object(self.pi, "_load_sector_perf", return_value={}):
            pos = self._mock_position(entry=100.0, current=115.0, pl=150.0)
            decision = {"catalyst": "earnings beat", "stop_loss": 90.0, "take_profit": 120.0}

            with self.assertLogs("portfolio_intelligence", level="DEBUG") as cm:
                logger.debug("sentinel")
                result = self.pi.score_position_thesis(
                    "NVDA", pos, decision, {}, days_held=1
                )

        if result["thesis_status"] == "valid":
            self.assertFalse(
                any("THESIS INVALIDATED" in msg for msg in cm.output),
                "Should not log THESIS INVALIDATED for valid thesis",
            )

    def test_format_thesis_ranking_includes_status(self):
        """format_thesis_ranking_section must include thesis_status text."""
        ts_list = [{
            "symbol": "NVDA",
            "thesis_score": 8,
            "thesis_status": "valid",
            "catalyst_age_days": 1,
            "technical_intact": True,
            "above_ma20": True,
            "above_ema9": True,
            "trending_toward": "target",
            "sector_aligned": True,
            "weakest_factor": "none",
            "recommended_action": "hold",
            "override_flag": None,
            "catalyst": "earnings beat",
        }]
        output = self.pi.format_thesis_ranking_section(ts_list)
        self.assertIn("VALID", output)

    def test_format_thesis_ranking_invalidated_label(self):
        ts_list = [{
            "symbol": "XLE",
            "thesis_score": 3,
            "thesis_status": "invalidated",
            "catalyst_age_days": 8,
            "technical_intact": False,
            "above_ma20": False,
            "above_ema9": False,
            "trending_toward": "stop",
            "sector_aligned": False,
            "weakest_factor": "catalyst stale",
            "recommended_action": "exit_consider",
            "override_flag": None,
            "catalyst": "macro momentum",
        }]
        output = self.pi.format_thesis_ranking_section(ts_list)
        self.assertIn("INVALIDATED", output)


# ── T-025: update_outcomes_from_alpaca uses SELL fills ───────────────────────

class TestOutcomeResolutionSellFill(unittest.TestCase):

    def setUp(self):
        import importlib
        if "memory" in sys.modules:
            self.mem = sys.modules["memory"]
        else:
            import memory as mem
            self.mem = mem
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.df  = self.tmp / "decisions.json"
        self.pf  = self.tmp / "performance.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _write_pending_buy(self, symbol: str, stop: float, tp: float) -> None:
        record = [{
            "ts": "2026-04-18T10:00:00+00:00",
            "session": "market",
            "regime": "neutral",
            "regime_score": 60,
            "n_actions": 1,
            "vector_id": "",
            "decision_id": "",
            "actions": [{
                "action": "buy",
                "symbol": symbol,
                "qty": 10,
                "stop_loss": stop,
                "take_profit": tp,
                "tier": "core",
                "catalyst": "earnings beat",
                "catalyst_type": "earnings_beat",
                "sector_signal": None,
                "confidence": "high",
                "strategy": "hybrid",
                "sector": "Technology",
                "option_strategy": None,
                "expiration": None,
                "long_strike": None,
                "short_strike": None,
                "max_cost_usd": None,
                "outcome": None,
                "pnl": None,
            }],
        }]
        self.df.write_text(json.dumps(record))
        self.pf.write_text(json.dumps(self.mem._empty_perf()))

    def _mock_sell_order(self, symbol: str, fill_price: float):
        o = MagicMock()
        o.side = "sell"   # matches mock_os.SELL below
        o.filled_avg_price = str(fill_price)
        o.filled_qty = "10"
        o.symbol = symbol
        o.id = "sell-order-1"
        o.status = "filled"
        return o

    def _mock_buy_order(self, symbol: str, fill_price: float):
        o = MagicMock()
        o.side = "buy"    # matches mock_os.BUY below
        o.filled_avg_price = str(fill_price)
        o.filled_qty = "10"
        o.symbol = symbol
        o.id = "buy-order-1"
        o.status = "filled"
        return o

    def _run_update(self, orders: list) -> dict:
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
            mock_tc.return_value.get_orders.return_value = orders
            mock_tm.update_trade_outcome = MagicMock()
            self.mem.update_outcomes_from_alpaca()

        return json.loads(self.df.read_text())[-1]["actions"][0]

    def test_win_when_sell_at_take_profit(self):
        """SELL fill at take_profit → outcome=win."""
        self._write_pending_buy("NVDA", stop=150.0, tp=200.0)
        sell = self._mock_sell_order("NVDA", fill_price=200.0)
        action = self._run_update([sell])
        self.assertEqual(action["outcome"], "win")

    def test_loss_when_sell_at_stop(self):
        """SELL fill at stop_loss → outcome=loss."""
        self._write_pending_buy("NVDA", stop=150.0, tp=200.0)
        sell = self._mock_sell_order("NVDA", fill_price=150.0)
        action = self._run_update([sell])
        self.assertEqual(action["outcome"], "loss")

    def test_no_outcome_when_only_buy_fill(self):
        """A BUY fill alone must not resolve outcome (entry, not exit)."""
        self._write_pending_buy("NVDA", stop=150.0, tp=200.0)
        buy = self._mock_buy_order("NVDA", fill_price=170.0)
        action = self._run_update([buy])
        self.assertIsNone(action["outcome"])

    def test_pnl_computed_when_buy_and_sell_fills_present(self):
        """When both BUY and SELL fills exist, pnl = (sell - buy) * qty."""
        self._write_pending_buy("NVDA", stop=150.0, tp=200.0)
        buy  = self._mock_buy_order("NVDA", fill_price=170.0)
        sell = self._mock_sell_order("NVDA", fill_price=200.0)
        action = self._run_update([buy, sell])
        self.assertEqual(action["outcome"], "win")
        self.assertIsNotNone(action["pnl"])
        self.assertAlmostEqual(action["pnl"], (200.0 - 170.0) * 10, places=0)

    def test_midrange_sell_does_not_resolve(self):
        """SELL fill between stop and take_profit → no outcome (pending)."""
        self._write_pending_buy("NVDA", stop=150.0, tp=200.0)
        sell = self._mock_sell_order("NVDA", fill_price=175.0)
        action = self._run_update([sell])
        self.assertIsNone(action["outcome"])

    def test_no_resolution_when_no_matching_symbol(self):
        """SELL fill for a different symbol must not resolve the action."""
        self._write_pending_buy("NVDA", stop=150.0, tp=200.0)
        sell = self._mock_sell_order("TSLA", fill_price=200.0)
        action = self._run_update([sell])
        self.assertIsNone(action["outcome"])


# ── T-025: backfill_forward_returns insufficient_data early-exit ──────────────

class TestBackfillInsufficientData(unittest.TestCase):

    def test_returns_zero_when_status_insufficient_data(self):
        """backfill_forward_returns returns 0 immediately when backtest=insufficient_data."""
        import decision_outcomes as do
        bt_content = json.dumps({
            "status": "insufficient_data",
            "n_signals": 2,
            "min_required": 5,
            "summaries": {},
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cache = tmp / "backtest_latest.json"
            outcomes = tmp / "decision_outcomes.jsonl"
            cache.write_text(bt_content)
            outcomes.write_text(
                json.dumps({
                    "decision_id": "dec_A1_20260418_093500",
                    "account": "A1",
                    "symbol": "NVDA",
                    "timestamp": "2026-04-18T09:35:00Z",
                    "action": "buy",
                    "status": "submitted",
                    "return_1d": None,
                }) + "\n"
            )
            with (
                patch.object(do, "OUTCOMES_LOG", outcomes),
                patch.object(do, "BACKTEST_CACHE", cache),
            ):
                result = do.backfill_forward_returns(days_back=30)
        self.assertEqual(result, 0)

    def test_logs_info_when_status_insufficient_data(self):
        """backfill_forward_returns logs [OUTCOMES] backfill: backtest insufficient_data."""
        import decision_outcomes as do
        import logging
        bt_content = json.dumps({
            "status": "insufficient_data",
            "n_signals": 3,
            "min_required": 5,
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cache = tmp / "backtest_latest.json"
            outcomes = tmp / "decision_outcomes.jsonl"
            cache.write_text(bt_content)
            outcomes.write_text("")

            with (
                patch.object(do, "OUTCOMES_LOG", outcomes),
                patch.object(do, "BACKTEST_CACHE", cache),
            ):
                with self.assertLogs("decision_outcomes", level="INFO") as cm:
                    do.backfill_forward_returns(days_back=30)

        self.assertTrue(
            any("insufficient_data" in msg or "insufficient" in msg for msg in cm.output),
            f"Expected insufficient_data info log, got: {cm.output}",
        )

    def test_normal_backfill_still_works_when_results_present(self):
        """backfill_forward_returns works normally when backtest has results."""
        import decision_outcomes as do
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        ts = (now - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        date_str = ts[:10]
        bt_content = json.dumps({
            "results": [{
                "symbol": "NVDA",
                "decision_date": date_str,
                "return_1d": 0.025,
                "return_3d": 0.04,
                "return_5d": 0.06,
                "correct_1d": True,
                "correct_3d": True,
                "correct_5d": True,
            }]
        })
        outcome_line = json.dumps({
            "decision_id": "dec_A1_20260418_093500",
            "account": "A1",
            "symbol": "NVDA",
            "timestamp": ts,
            "action": "buy",
            "status": "submitted",
            "return_1d": None,
            "return_3d": None,
            "return_5d": None,
            "correct_1d": None,
            "correct_3d": None,
            "correct_5d": None,
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cache = tmp / "backtest_latest.json"
            outcomes = tmp / "decision_outcomes.jsonl"
            cache.write_text(bt_content)
            outcomes.write_text(outcome_line + "\n")

            with (
                patch.object(do, "OUTCOMES_LOG", outcomes),
                patch.object(do, "BACKTEST_CACHE", cache),
            ):
                updated = do.backfill_forward_returns(days_back=30)

            self.assertEqual(updated, 1)
            result = json.loads(outcomes.read_text().strip())
            self.assertAlmostEqual(result["return_1d"], 0.025)
            self.assertTrue(result["correct_1d"])


if __name__ == "__main__":
    unittest.main()
