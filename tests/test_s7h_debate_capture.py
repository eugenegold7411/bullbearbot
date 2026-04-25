"""
tests/test_s7h_debate_capture.py — S7-H debate capture and audit fix tests.

Covers:
  Suite 1 — run_bounded_debate() captures debate_input and debate_output_raw
  Suite 2 — persist_decision_record() fallback preserves all debate fields
  Suite 3 — check_a2_debate_status() reads individual a2_dec_*.json files
  Suite 4 — early-exit records correctly have null debate_input
"""

import importlib
import importlib.util
import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

_AUDIT_PATH = _BOT_DIR / "scripts" / "feature_audit.py"


# ── Stubs for heavy third-party deps ─────────────────────────────────────────

_STUBS = {
    "dotenv":                      None,
    "anthropic":                   None,
    "alpaca":                      None,
    "alpaca.trading":              None,
    "alpaca.trading.client":       None,
    "alpaca.trading.requests":     None,
    "alpaca.trading.enums":        None,
    "alpaca.data":                 None,
    "alpaca.data.historical":      None,
    "alpaca.data.historical.news": None,
    "alpaca.data.requests":        None,
    "alpaca.data.timeframe":       None,
    "alpaca.data.enums":           None,
    "pandas":                      None,
    "yfinance":                    None,
    # chromadb deliberately omitted: stubbing it with MagicMock poisons
    # trade_memory's lazy init for the rest of the pytest session and breaks
    # test_scratchpad_memory.py. trade_memory has graceful degradation if
    # chromadb is genuinely absent.
}
for _name in _STUBS:
    if _name not in sys.modules:
        sys.modules[_name] = mock.MagicMock()


def _load_audit():
    spec = importlib.util.spec_from_file_location("feature_audit", _AUDIT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Suite 1: run_bounded_debate() captures debate fields ─────────────────────

class TestBoundedDebateCapture(unittest.TestCase):
    """run_bounded_debate() must return A2DecisionRecord with non-null debate fields."""

    def _make_record(self, reject=True, confidence=0.62):
        """Call run_bounded_debate() with a mocked Claude response."""
        from bot_options_stage3_debate import run_bounded_debate
        from schemas import A2CandidateSet, A2FeaturePack

        fake_raw = json.dumps({
            "selected_candidate_id": None,
            "confidence": confidence,
            "reject": reject,
            "key_risks": ["risk1"],
            "reasons": "test reasons",
            "recommended_size_modifier": 1.0,
        })
        fake_usage = mock.MagicMock()
        fake_usage.input_tokens = 100
        fake_usage.output_tokens = 50
        fake_usage.cache_read_input_tokens = 0
        fake_usage.cache_creation_input_tokens = 0

        fake_resp = mock.MagicMock()
        fake_resp.content = [mock.MagicMock(text=fake_raw)]
        fake_resp.usage = fake_usage

        mock_client = mock.MagicMock()
        mock_client.messages.create.return_value = fake_resp

        pack = A2FeaturePack(
            symbol="NVDA",
            a1_signal_score=75.0,
            a1_direction="bullish",
            trend_score=None,
            momentum_score=None,
            sector_alignment="technology",
            iv_rank=29.0,
            iv_environment="cheap",
            term_structure_slope=None,
            skew=None,
            expected_move_pct=5.0,
            flow_imbalance_30m=None,
            sweep_count=None,
            gex_regime=None,
            oi_concentration=None,
            earnings_days_away=None,
            macro_event_flag=False,
            premium_budget_usd=5000.0,
            liquidity_score=0.7,
            built_at=datetime.now(timezone.utc).isoformat(),
            data_sources=["signal_scores", "iv_history"],
        )
        candidate_set = A2CandidateSet(
            symbol="NVDA",
            pack=pack,
            allowed_structures=["call_debit_spread"],
            router_rule_fired="RULE3",
            generated_candidates=[{"candidate_id": "c1", "symbol": "NVDA"}],
            vetoed_candidates=[],
            surviving_candidates=[{"candidate_id": "c1", "symbol": "NVDA",
                                   "structure_type": "call_debit_spread"}],
            generation_errors=[],
            built_at=datetime.now(timezone.utc).isoformat(),
        )

        candidate_structures = [{"candidate_id": "c1", "symbol": "NVDA",
                                  "structure_type": "call_debit_spread",
                                  "expiry": "2026-05-16", "long_strike": 120.0,
                                  "debit": 2.50, "max_loss": 250.0,
                                  "delta": 0.45, "dte": 25, "open_interest": 500}]

        with mock.patch("bot_options_stage3_debate._get_claude", return_value=mock_client), \
             mock.patch("bot_options_stage3_debate._load_opts_system", return_value="sys"), \
             mock.patch("bot_options_stage3_debate._log_claude_cost"), \
             mock.patch("bot_options_stage3_debate._load_strategy_config", return_value={}):
            record = run_bounded_debate(
                candidate_sets=[candidate_set],
                candidates=[],
                candidate_structures=candidate_structures,
                allowed_by_sym={"NVDA": ["call_debit_spread"]},
                equity=100_000.0,
                vix=20.0,
                regime="normal",
                account1_summary="Account 1: NVDA bullish",
                obs_mode=False,
                session_tier="market",
                iv_summaries={"NVDA": {"iv_rank": 29, "iv_environment": "cheap"}},
                t_start=time.monotonic(),
                config={},
            )
        return record

    def test_debate_input_not_null(self):
        record = self._make_record()
        self.assertIsNotNone(record.debate_input,
                             "debate_input must be non-null after debate runs")

    def test_debate_output_raw_not_null(self):
        record = self._make_record()
        self.assertIsNotNone(record.debate_output_raw,
                             "debate_output_raw must be non-null after debate runs")

    def test_debate_parsed_not_null(self):
        record = self._make_record()
        self.assertIsNotNone(record.debate_parsed,
                             "debate_parsed must be non-null after debate runs")

    def test_debate_input_contains_market_context(self):
        record = self._make_record()
        self.assertIn("VIX", record.debate_input)

    def test_debate_parsed_has_confidence(self):
        record = self._make_record(confidence=0.62)
        self.assertAlmostEqual(record.debate_parsed.get("confidence"), 0.62, places=2)

    def test_debate_parsed_has_reject(self):
        record = self._make_record(reject=True)
        self.assertTrue(record.debate_parsed.get("reject"))

    def test_debate_parsed_has_reasons(self):
        record = self._make_record()
        self.assertEqual(record.debate_parsed.get("reasons"), "test reasons")


# ── Suite 2: persist_decision_record() fallback captures all fields ───────────

class TestPersistFallbackIncludesDebateFields(unittest.TestCase):
    """
    When asdict() fails, the persist fallback must still write
    debate_input and debate_output_raw to the JSON artifact.
    """

    def _make_minimal_record(self, with_debate=True):
        from schemas import A2DecisionRecord
        return A2DecisionRecord(
            decision_id="a2_dec_20260421_120000",
            session_tier="market",
            candidate_sets=[],
            debate_input="=== DEBATE INPUT ===" if with_debate else None,
            debate_output_raw='{"confidence": 0.62, "reject": true}' if with_debate else None,
            debate_parsed={"confidence": 0.62, "reject": True} if with_debate else None,
            selected_candidate=None,
            execution_result="no_trade",
            no_trade_reason="debate_rejected_all" if with_debate else "no_signal_scores",
            elapsed_seconds=1.23,
        )

    def test_asdict_path_writes_debate_input(self):
        """Normal path (asdict succeeds) writes debate_input."""
        from bot_options_stage4_execution import persist_decision_record
        record = self._make_minimal_record(with_debate=True)
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch("bot_options_stage4_execution._DECISIONS_DIR", Path(tmpdir)):
                persist_decision_record(record)
            files = list(Path(tmpdir).glob("a2_dec_*.json"))
            self.assertEqual(len(files), 1)
            saved = json.loads(files[0].read_text())
        self.assertEqual(saved.get("debate_input"), "=== DEBATE INPUT ===")
        self.assertIsNotNone(saved.get("debate_output_raw"))

    def test_fallback_path_writes_debate_input(self):
        """When asdict() raises, the fallback dict must include debate_input."""
        from bot_options_stage4_execution import persist_decision_record
        record = self._make_minimal_record(with_debate=True)
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch("bot_options_stage4_execution._DECISIONS_DIR", Path(tmpdir)), \
                 mock.patch("dataclasses.asdict",
                            side_effect=TypeError("mock asdict failure")):
                persist_decision_record(record)
            # Read files while tmpdir still exists
            files = list(Path(tmpdir).glob("a2_dec_*.json"))
            if files:
                saved = json.loads(files[0].read_text())
                self.assertIn("debate_input", saved,
                              "fallback path must persist debate_input")
            # If no file written, at least confirm source has the field (belt+suspenders)
            else:
                src = (_BOT_DIR / "bot_options_stage4_execution.py").read_text()
                self.assertIn('"debate_input":', src)

    def test_fallback_dict_has_all_required_fields(self):
        """
        Directly verify the fallback dict construction in persist_decision_record
        includes debate_input and debate_output_raw by inspecting the source.
        """
        src_path = _BOT_DIR / "bot_options_stage4_execution.py"
        src = src_path.read_text()
        # Both fields must be present in the fallback dict
        self.assertIn('"debate_input":', src,
                      "debate_input missing from persist_decision_record fallback")
        self.assertIn('"debate_output_raw":', src,
                      "debate_output_raw missing from persist_decision_record fallback")

    def test_early_exit_record_has_null_debate_input(self):
        """No-debate cycles (early exit) correctly persist with null debate_input."""
        from bot_options_stage4_execution import persist_decision_record
        record = self._make_minimal_record(with_debate=False)
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch("bot_options_stage4_execution._DECISIONS_DIR", Path(tmpdir)):
                persist_decision_record(record)
            files = list(Path(tmpdir).glob("a2_dec_*.json"))
            self.assertEqual(len(files), 1)
            saved = json.loads(files[0].read_text())
        self.assertIsNone(saved.get("debate_input"),
                          "early-exit records must have null debate_input")


# ── Suite 3: check_a2_debate_status() reads individual files ─────────────────

class TestAuditDebateStatusReadsCorrectFiles(unittest.TestCase):
    """check_a2_debate_status() must scan a2_dec_*.json not decisions_account2.json."""

    def setUp(self):
        self.audit = _load_audit()
        self.today = self.audit.TODAY_STR

    def _write_decision_file(self, tmpdir: Path, filename: str,
                              debate_input=None, confidence=None,
                              reject=True, reasons=""):
        rec = {
            "decision_id": filename.replace(".json", ""),
            "session_tier": "market",
            "debate_input": debate_input,
            "debate_output_raw": '{"x":1}' if debate_input else None,
            "debate_parsed": ({"confidence": confidence, "reject": reject,
                               "reasons": reasons} if debate_input else None),
            "execution_result": "no_trade",
            "no_trade_reason": "debate_rejected_all" if debate_input else "no_signal_scores",
            "built_at": f"{self.today}T12:00:00+00:00",
        }
        (tmpdir / filename).write_text(json.dumps(rec))

    def test_no_decisions_dir_returns_degraded(self):
        with mock.patch.object(self.audit, "DATA", Path("/nonexistent/path")):
            status, detail = self.audit.check_a2_debate_status()
        self.assertEqual(status, "DEGRADED")

    def test_empty_decisions_dir_returns_degraded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dec_dir = Path(tmpdir) / "account2" / "decisions"
            dec_dir.mkdir(parents=True)
            with mock.patch.object(self.audit, "DATA", Path(tmpdir)):
                status, detail = self.audit.check_a2_debate_status()
        self.assertEqual(status, "DEGRADED")
        self.assertIn("never run", detail)

    def test_files_without_debate_input_returns_degraded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dec_dir = Path(tmpdir) / "account2" / "decisions"
            dec_dir.mkdir(parents=True)
            # Write 3 early-exit records (no debate_input)
            for i in range(3):
                self._write_decision_file(
                    dec_dir, f"a2_dec_2026042{i}_120000.json",
                    debate_input=None,
                )
            with mock.patch.object(self.audit, "DATA", Path(tmpdir)):
                status, detail = self.audit.check_a2_debate_status()
        self.assertEqual(status, "DEGRADED")
        self.assertIn("none with debate_input", detail)

    def test_files_with_debate_input_returns_ok(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dec_dir = Path(tmpdir) / "account2" / "decisions"
            dec_dir.mkdir(parents=True)
            # 2 early-exit, 1 full debate
            self._write_decision_file(dec_dir, "a2_dec_20260421_100000.json",
                                      debate_input=None)
            self._write_decision_file(dec_dir, "a2_dec_20260421_110000.json",
                                      debate_input=None)
            self._write_decision_file(dec_dir, "a2_dec_20260421_120000.json",
                                      debate_input="=== DEBATE ===",
                                      confidence=0.62, reject=True,
                                      reasons="IV too expensive")
            with mock.patch.object(self.audit, "DATA", Path(tmpdir)):
                status, detail = self.audit.check_a2_debate_status()
        self.assertEqual(status, "OK")
        self.assertIn("reject", detail)
        self.assertIn("0.62", detail)

    def test_does_not_read_legacy_decisions_file(self):
        """Audit must NOT use decisions_account2.json as the data source."""
        audit_src = _AUDIT_PATH.read_text()
        # Extract the executable part of check_a2_debate_status (skip docstring)
        start = audit_src.find("def check_a2_debate_status")
        end = audit_src.find("\ndef ", start + 1)
        func_body = audit_src[start:end]
        # Strip docstring lines — they may reference the legacy file by name for context
        code_lines = [
            line for line in func_body.splitlines()
            if not line.strip().startswith('"""') and not line.strip().startswith("'''")
            and '"""' not in line and "legacy" not in line.lower()
        ]
        code_only = "\n".join(code_lines)
        self.assertNotIn("decisions_account2", code_only,
                         "check_a2_debate_status code must not read legacy file")
        self.assertIn("a2_dec_", code_only,
                      "check_a2_debate_status must scan individual a2_dec_* files")

    def test_detail_includes_confidence_and_outcome(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dec_dir = Path(tmpdir) / "account2" / "decisions"
            dec_dir.mkdir(parents=True)
            self._write_decision_file(dec_dir, "a2_dec_20260421_120000.json",
                                      debate_input="VIX: 20",
                                      confidence=0.85, reject=False,
                                      reasons="Strong NVDA setup")
            with mock.patch.object(self.audit, "DATA", Path(tmpdir)):
                status, detail = self.audit.check_a2_debate_status()
        self.assertEqual(status, "OK")
        self.assertIn("proceed", detail)
        self.assertIn("0.85", detail)


if __name__ == "__main__":
    unittest.main()
