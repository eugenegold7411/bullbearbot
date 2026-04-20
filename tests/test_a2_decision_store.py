"""
tests/test_a2_decision_store.py — Tests for a2_decision_store query layer.

Suites:
  TestLoadDecisions         — load_decisions() basic behaviour
  TestGetDailySummary       — aggregation logic over mock decision files
  TestVetoReasonAggregation — veto reason normalization and counting
  TestGetDecisionById       — look-up by decision_id
  TestReportA2Section       — report.py A2 health section degrades gracefully
"""

import json
import sys
import types
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

# ── Stubs for report.py's heavy dependencies (installed before first import) ──

def _stub(name: str, **attrs) -> types.ModuleType:
    if name not in sys.modules:
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m
    return sys.modules[name]


_stub("dotenv", load_dotenv=lambda *a, **kw: None)
_stub("log_setup", get_logger=lambda name: __import__("logging").getLogger(name))
_stub("alpaca.trading.enums",
      QueryOrderStatus=types.SimpleNamespace(CLOSED="closed"),
      OrderSide=types.SimpleNamespace(BUY="buy", SELL="sell"),
      AssetStatus=object, ContractType=object, ExerciseStyle=object,
      OrderClass=object, TimeInForce=object)

class _FakeTC:
    def __init__(self, *a, **kw): pass
    def get_account(self): return None
    def get_orders(self, *a, **kw): return []
    def get_portfolio_history(self, *a, **kw):
        return types.SimpleNamespace(timestamp=[], equity=[], profit_loss=[],
                                     profit_loss_pct=[])
    def get_all_positions(self): return []

_stub("alpaca.trading.client", TradingClient=_FakeTC)
_stub("alpaca.trading.requests",
      GetOrdersRequest=object, GetPortfolioHistoryRequest=object,
      ClosePositionRequest=object, LimitOrderRequest=object,
      MarketOrderRequest=object, StopLossRequest=object,
      StopOrderRequest=object, TakeProfitRequest=object,
      GetOptionContractsRequest=object)
_stub("alpaca", trading=types.SimpleNamespace())
_stub("alpaca.trading", client=object, requests=object, enums=object)
_stub("trade_memory",
      get_collection_stats=lambda: {},
      save_trade_memory=lambda *a, **kw: "",
      retrieve_similar_scenarios=lambda *a, **kw: [])


def _make_record(
    decision_id: str = "test_001",
    session_tier: str = "market",
    candidate_sets: list = None,
    debate_parsed: dict = None,
    execution_result: str = "no_trade",
    no_trade_reason: str = None,
) -> dict:
    """Build a minimal serialized A2DecisionRecord dict."""
    return {
        "decision_id": decision_id,
        "session_tier": session_tier,
        "candidate_sets": candidate_sets or [],
        "debate_input": None,
        "debate_output_raw": None,
        "debate_parsed": debate_parsed,
        "selected_candidate": None,
        "execution_result": execution_result,
        "no_trade_reason": no_trade_reason,
        "elapsed_seconds": 1.5,
        "schema_version": 1,
        "code_version": None,
        "built_at": "2026-04-21T12:00:00+00:00",
    }


def _make_candidate_set(
    symbol: str = "AAPL",
    generated: int = 2,
    vetoed_reasons: list[str] = None,
    generation_errors: list[str] = None,
) -> dict:
    vetoed = [
        {"candidate_id": f"{symbol}_{i}", "reason": r}
        for i, r in enumerate(vetoed_reasons or [])
    ]
    surviving = [
        {"candidate_id": f"{symbol}_ok_{i}"}
        for i in range(generated - len(vetoed_reasons or []))
        if generated - len(vetoed_reasons or []) > 0
    ]
    return {
        "symbol": symbol,
        "pack": {},
        "allowed_structures": ["debit_call_spread"],
        "router_rule_fired": "RULE5",
        "generated_candidates": [{}] * generated,
        "vetoed_candidates": vetoed,
        "surviving_candidates": surviving,
        "generation_errors": generation_errors or [],
        "built_at": "2026-04-21T12:00:00+00:00",
    }


class TestLoadDecisions(unittest.TestCase):
    def test_returns_empty_list_when_dir_absent(self):
        import a2_decision_store as ds
        with mock.patch.object(ds, "_DECISIONS_DIR", Path("/nonexistent/path")):
            result = ds.load_decisions(date="2026-04-21")
        self.assertEqual(result, [])

    def test_loads_records_for_date(self):
        import tempfile

        import a2_decision_store as ds

        rec1 = _make_record("d001")
        rec2 = _make_record("d002")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            (tmppath / "a2_dec_20260421_100000.json").write_text(json.dumps(rec1))
            (tmppath / "a2_dec_20260421_110000.json").write_text(json.dumps(rec2))
            # Different date — should not appear
            (tmppath / "a2_dec_20260420_090000.json").write_text(json.dumps(_make_record("d003")))
            with mock.patch.object(ds, "_DECISIONS_DIR", tmppath):
                result = ds.load_decisions(date="2026-04-21")
        self.assertEqual(len(result), 2)
        ids = {r["decision_id"] for r in result}
        self.assertIn("d001", ids)
        self.assertIn("d002", ids)
        self.assertNotIn("d003", ids)

    def test_limit_is_respected(self):
        import tempfile

        import a2_decision_store as ds

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            for i in range(10):
                (tmppath / f"a2_dec_20260421_{i:06d}.json").write_text(
                    json.dumps(_make_record(f"d{i:03d}"))
                )
            with mock.patch.object(ds, "_DECISIONS_DIR", tmppath):
                result = ds.load_decisions(date="2026-04-21", limit=5)
        self.assertEqual(len(result), 5)

    def test_skips_corrupt_files(self):
        import tempfile

        import a2_decision_store as ds

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            (tmppath / "a2_dec_20260421_100000.json").write_text("NOT JSON {{{")
            (tmppath / "a2_dec_20260421_110000.json").write_text(json.dumps(_make_record("d002")))
            with mock.patch.object(ds, "_DECISIONS_DIR", tmppath):
                result = ds.load_decisions(date="2026-04-21")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["decision_id"], "d002")

    def test_defaults_to_today(self):
        import a2_decision_store as ds
        date.today().strftime("%Y%m%d")
        with mock.patch.object(ds, "_DECISIONS_DIR", Path("/nonexistent")):
            # just ensure it doesn't raise
            result = ds.load_decisions()
        self.assertEqual(result, [])


class TestGetDailySummary(unittest.TestCase):
    def _write_records(self, tmppath: Path, records: list[dict]) -> None:
        for i, rec in enumerate(records):
            fname = f"a2_dec_20260421_{i:06d}.json"
            (tmppath / fname).write_text(json.dumps(rec))

    def test_empty_dir_returns_zeroed_summary(self):
        import a2_decision_store as ds
        with mock.patch.object(ds, "_DECISIONS_DIR", Path("/nonexistent")):
            summary = ds.get_daily_summary(date="2026-04-21")
        self.assertEqual(summary["date"], "2026-04-21")
        self.assertEqual(summary["cycles_run"], 0)
        self.assertEqual(summary["candidates_generated"], 0)

    def test_cycles_run_count(self):
        import tempfile

        import a2_decision_store as ds

        records = [_make_record(f"d{i}") for i in range(7)]
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            self._write_records(tmppath, records)
            with mock.patch.object(ds, "_DECISIONS_DIR", tmppath):
                summary = ds.get_daily_summary(date="2026-04-21")
        self.assertEqual(summary["cycles_run"], 7)

    def test_symbols_evaluated_aggregation(self):
        import tempfile

        import a2_decision_store as ds

        cs1 = _make_candidate_set("AAPL", generated=2, vetoed_reasons=[])
        cs2 = _make_candidate_set("MSFT", generated=3, vetoed_reasons=[])
        # One cycle with 2 symbols, one cycle with 1 symbol
        rec1 = _make_record("d1", candidate_sets=[cs1, cs2])
        rec2 = _make_record("d2", candidate_sets=[cs1])
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            self._write_records(tmppath, [rec1, rec2])
            with mock.patch.object(ds, "_DECISIONS_DIR", tmppath):
                summary = ds.get_daily_summary(date="2026-04-21")
        self.assertEqual(summary["symbols_evaluated"], 3)
        self.assertEqual(summary["candidates_generated"], 7)  # 2+3+2

    def test_candidates_vetoed_aggregation(self):
        import tempfile

        import a2_decision_store as ds

        cs = _make_candidate_set(
            "SPY", generated=4,
            vetoed_reasons=[
                "bid_ask_spread_pct=0.08>0.05",
                "bid_ask_spread_pct=0.07>0.05",
                "dte=3<5",
            ]
        )
        rec = _make_record("d1", candidate_sets=[cs])
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            self._write_records(tmppath, [rec])
            with mock.patch.object(ds, "_DECISIONS_DIR", tmppath):
                summary = ds.get_daily_summary(date="2026-04-21")
        self.assertEqual(summary["candidates_vetoed"], 3)

    def test_debate_runs_and_rejects(self):
        import tempfile

        import a2_decision_store as ds

        rec_reject = _make_record(
            "d1",
            debate_parsed={"reject": True, "selected_candidate_id": None, "confidence": 0.4},
            no_trade_reason="debate_rejected_all",
        )
        rec_accept = _make_record(
            "d2",
            debate_parsed={"reject": False, "selected_candidate_id": "AAPL_001", "confidence": 0.9},
            execution_result="submitted",
        )
        rec_no_debate = _make_record("d3", debate_parsed=None, no_trade_reason="no_signal_scores")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            self._write_records(tmppath, [rec_reject, rec_accept, rec_no_debate])
            with mock.patch.object(ds, "_DECISIONS_DIR", tmppath):
                summary = ds.get_daily_summary(date="2026-04-21")
        self.assertEqual(summary["debate_runs"], 2)
        self.assertEqual(summary["debate_rejects"], 1)
        self.assertEqual(summary["executions_filled"], 1)

    def test_low_confidence_counted(self):
        import tempfile

        import a2_decision_store as ds

        rec = _make_record(
            "d1",
            debate_parsed={"reject": False, "confidence": 0.7, "selected_candidate_id": "AAPL_1"},
            no_trade_reason="debate_low_confidence",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            self._write_records(tmppath, [rec])
            with mock.patch.object(ds, "_DECISIONS_DIR", tmppath):
                summary = ds.get_daily_summary(date="2026-04-21")
        self.assertEqual(summary["debate_low_confidence"], 1)
        self.assertIn("debate_low_confidence", summary["no_trade_reasons"])

    def test_no_trade_reasons_aggregation(self):
        import tempfile

        import a2_decision_store as ds

        records = [
            _make_record(f"d{i}", no_trade_reason="no_candidates_after_veto")
            for i in range(5)
        ] + [
            _make_record(f"e{i}", no_trade_reason="no_signal_scores")
            for i in range(2)
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            self._write_records(tmppath, records)
            with mock.patch.object(ds, "_DECISIONS_DIR", tmppath):
                summary = ds.get_daily_summary(date="2026-04-21")
        self.assertEqual(summary["no_trade_reasons"]["no_candidates_after_veto"], 5)
        self.assertEqual(summary["no_trade_reasons"]["no_signal_scores"], 2)

    def test_missing_data_failures_from_generation_errors(self):
        import tempfile

        import a2_decision_store as ds

        cs = _make_candidate_set("NVDA", generated=1, generation_errors=["chain_fetch_failed"])
        rec = _make_record("d1", candidate_sets=[cs])
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            self._write_records(tmppath, [rec])
            with mock.patch.object(ds, "_DECISIONS_DIR", tmppath):
                summary = ds.get_daily_summary(date="2026-04-21")
        self.assertEqual(summary["missing_data_failures"], 1)

    def test_summary_always_returns_all_keys(self):
        import a2_decision_store as ds
        expected_keys = {
            "date", "cycles_run", "symbols_evaluated", "candidates_generated",
            "candidates_vetoed", "veto_reasons", "debate_runs", "debate_rejects",
            "debate_low_confidence", "executions_attempted", "executions_filled",
            "no_trade_reasons", "missing_data_failures", "bootstrap_queue_additions",
        }
        with mock.patch.object(ds, "_DECISIONS_DIR", Path("/nonexistent")):
            summary = ds.get_daily_summary(date="2026-04-21")
        self.assertEqual(set(summary.keys()), expected_keys)


class TestVetoReasonAggregation(unittest.TestCase):
    def _run_with_veto_reasons(self, reasons: list[str]) -> dict:
        import tempfile

        import a2_decision_store as ds

        cs = _make_candidate_set("SPY", generated=len(reasons), vetoed_reasons=reasons)
        rec = _make_record("d1", candidate_sets=[cs])
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            (tmppath / "a2_dec_20260421_000000.json").write_text(json.dumps(rec))
            with mock.patch.object(ds, "_DECISIONS_DIR", tmppath):
                return ds.get_daily_summary(date="2026-04-21")

    def test_bid_ask_spread_normalized(self):
        summary = self._run_with_veto_reasons(["bid_ask_spread_pct=0.08>0.05"])
        self.assertIn("spread_too_wide", summary["veto_reasons"])
        self.assertEqual(summary["veto_reasons"]["spread_too_wide"], 1)

    def test_dte_normalized(self):
        summary = self._run_with_veto_reasons(["dte=3<5"])
        self.assertIn("dte_too_near", summary["veto_reasons"])

    def test_theta_normalized(self):
        summary = self._run_with_veto_reasons(["theta_decay_rate=0.06>0.05"])
        self.assertIn("theta_too_punitive", summary["veto_reasons"])

    def test_open_interest_normalized(self):
        summary = self._run_with_veto_reasons(["open_interest=50<100"])
        self.assertIn("low_open_interest", summary["veto_reasons"])

    def test_multiple_same_reason_summed(self):
        summary = self._run_with_veto_reasons([
            "bid_ask_spread_pct=0.06>0.05",
            "bid_ask_spread_pct=0.09>0.05",
            "bid_ask_spread_pct=0.11>0.05",
        ])
        self.assertEqual(summary["veto_reasons"]["spread_too_wide"], 3)

    def test_unknown_reason_kept_as_is(self):
        summary = self._run_with_veto_reasons(["some_custom_veto"])
        self.assertIn("some_custom_veto", summary["veto_reasons"])


class TestGetDecisionById(unittest.TestCase):
    def test_returns_none_when_dir_absent(self):
        import a2_decision_store as ds
        with mock.patch.object(ds, "_DECISIONS_DIR", Path("/nonexistent")):
            result = ds.get_decision_by_id("test_001")
        self.assertIsNone(result)

    def test_finds_record_by_id(self):
        import tempfile

        import a2_decision_store as ds

        rec = _make_record("unique_id_xyz")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            (tmppath / "a2_dec_20260421_100000.json").write_text(json.dumps(rec))
            with mock.patch.object(ds, "_DECISIONS_DIR", tmppath):
                result = ds.get_decision_by_id("unique_id_xyz")
        self.assertIsNotNone(result)
        self.assertEqual(result["decision_id"], "unique_id_xyz")

    def test_returns_none_for_unknown_id(self):
        import tempfile

        import a2_decision_store as ds

        rec = _make_record("known_id")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            (tmppath / "a2_dec_20260421_100000.json").write_text(json.dumps(rec))
            with mock.patch.object(ds, "_DECISIONS_DIR", tmppath):
                result = ds.get_decision_by_id("does_not_exist")
        self.assertIsNone(result)


class TestReportA2Section(unittest.TestCase):
    """Tests for the A2 health section in report.py."""

    def test_a2_health_html_empty_summary(self):
        from report import _a2_health_html
        html = _a2_health_html({})
        self.assertIn("A2 data unavailable", html)

    def test_a2_health_html_zero_cycles(self):
        from report import _a2_health_html
        summary = {
            "date": "2026-04-21",
            "cycles_run": 0,
            "symbols_evaluated": 0,
            "candidates_generated": 0,
            "candidates_vetoed": 0,
            "veto_reasons": {},
            "debate_runs": 0,
            "debate_rejects": 0,
            "debate_low_confidence": 0,
            "executions_attempted": 0,
            "executions_filled": 0,
            "no_trade_reasons": {},
            "missing_data_failures": 0,
            "bootstrap_queue_additions": 0,
        }
        html = _a2_health_html(summary)
        self.assertIn("A2 data unavailable", html)

    def test_a2_health_html_with_data(self):
        from report import _a2_health_html
        summary = {
            "date": "2026-04-21",
            "cycles_run": 45,
            "symbols_evaluated": 12,
            "candidates_generated": 8,
            "candidates_vetoed": 23,
            "veto_reasons": {"spread_too_wide": 15, "theta_too_punitive": 8},
            "debate_runs": 3,
            "debate_rejects": 2,
            "debate_low_confidence": 1,
            "executions_attempted": 0,
            "executions_filled": 0,
            "no_trade_reasons": {"no_candidates_after_veto": 42},
            "missing_data_failures": 2,
            "bootstrap_queue_additions": 0,
        }
        html = _a2_health_html(summary)
        self.assertNotIn("unavailable", html)
        self.assertIn("45", html)
        self.assertIn("spread_too_wide", html)
        self.assertIn("no_candidates_after_veto", html)

    def test_load_a2_daily_summary_nonfatal_on_import_error(self):
        """_load_a2_daily_summary must return {} if a2_decision_store raises."""
        from datetime import date

        from report import _load_a2_daily_summary
        with mock.patch.dict("sys.modules", {"a2_decision_store": None}):
            result = _load_a2_daily_summary(date.today())
        # Should return {} on any failure
        self.assertIsInstance(result, dict)

    def test_generate_report_includes_a2_activity_key(self):
        """generate_report() output dict must always contain 'a2_activity'."""
        import report
        mock_account = mock.MagicMock()
        mock_account.equity    = "100000"
        mock_account.cash      = "100000"
        mock_account.last_equity = "100000"

        with mock.patch.object(report, "_get_account", return_value=mock_account), \
             mock.patch.object(report, "_get_portfolio_history", return_value=[]), \
             mock.patch.object(report, "_get_closed_orders", return_value=[]), \
             mock.patch.object(report, "_get_positions", return_value=[]), \
             mock.patch("memory.get_ticker_stats", return_value={}), \
             mock.patch("memory.get_ticker_lessons", return_value=""), \
             mock.patch("portfolio_intelligence.build_portfolio_intelligence", return_value={}), \
             mock.patch.object(report, "_load_a2_daily_summary", return_value={}):
            result = report.generate_report()
        self.assertIn("a2_activity", result)


if __name__ == "__main__":
    unittest.main()
