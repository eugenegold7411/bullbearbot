"""
tests/test_s7e_audit_hardening.py — S7-E audit script hardening tests.

Covers:
  Suite 1 — Stricter status: missing SMS delivery returns BROKEN
  Suite 2 — Incident log filters test accounts
  Suite 3 — A2 debate status check
"""

import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_BOT_DIR = Path(__file__).resolve().parent.parent
_AUDIT_PATH = _BOT_DIR / "scripts" / "feature_audit.py"


def _load_audit():
    """Import feature_audit fresh so module-level paths resolve correctly."""
    spec = importlib.util.spec_from_file_location("feature_audit", _AUDIT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Suite 1: Stricter SMS status ──────────────────────────────────────────────

class TestSmsDeliveryBroken(unittest.TestCase):
    """check_sms_delivery() must return BROKEN (not DEGRADED) when no delivery logged."""

    def setUp(self):
        self.audit = _load_audit()

    def test_no_sms_today_returns_broken(self):
        with mock.patch.object(self.audit, "_read_log_tail", return_value=[]):
            status, detail = self.audit.check_sms_delivery()
        self.assertEqual(status, "BROKEN")
        self.assertIn("no WhatsApp delivery", detail)

    def test_no_sms_today_not_degraded(self):
        with mock.patch.object(self.audit, "_read_log_tail", return_value=[]):
            status, _ = self.audit.check_sms_delivery()
        self.assertNotEqual(status, "DEGRADED")

    def test_sms_present_returns_ok(self):
        today = self.audit.TODAY_STR
        fake_lines = [f"{today} INFO WhatsApp sent to +1xxxxx"]
        with mock.patch.object(self.audit, "_read_log_tail", return_value=fake_lines):
            status, detail = self.audit.check_sms_delivery()
        self.assertEqual(status, "OK")

    def test_whatsapp_alert_sent_returns_ok(self):
        today = self.audit.TODAY_STR
        fake_lines = [f"{today} INFO WhatsApp alert sent"]
        with mock.patch.object(self.audit, "_read_log_tail", return_value=fake_lines):
            status, _ = self.audit.check_sms_delivery()
        self.assertEqual(status, "OK")

    def test_old_sms_sent_compat_returns_ok(self):
        today = self.audit.TODAY_STR
        fake_lines = [f"{today} INFO SMS sent"]
        with mock.patch.object(self.audit, "_read_log_tail", return_value=fake_lines):
            status, _ = self.audit.check_sms_delivery()
        self.assertEqual(status, "OK")


# ── Suite 2: Incident log test-account filtering ─────────────────────────────

class TestIncidentLogFiltering(unittest.TestCase):
    """check_incident_log() must exclude entries where account contains TEST."""

    def setUp(self):
        self.audit = _load_audit()
        self.today = self.audit.TODAY_STR

    def _make_incident(self, account: str, extra: str = "") -> str:
        return json.dumps({"account": account, "timestamp": f"{self.today}T10:00:00{extra}"})

    def test_test_accounts_excluded_from_today_count(self):
        lines = "\n".join([
            self._make_incident("A1_E2E_TEST"),
            self._make_incident("A1_TEST"),
            self._make_incident("real_account"),
        ])
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(lines)
            tmp = Path(f.name)
        try:
            with mock.patch.object(self.audit, "DATA", tmp.parent):
                # Patch path construction
                fake_path = tmp
                with mock.patch.object(
                    self.audit.Path, "__truediv__",
                    side_effect=lambda self_, other: fake_path if "incident_log" in str(other) else Path.__truediv__(self_, other)
                ):
                    pass
            # Direct path patch via DATA reassignment won't work cleanly for nested paths,
            # so patch read_text on the resolved path instead.
            with mock.patch("pathlib.Path.exists", return_value=True), \
                 mock.patch("pathlib.Path.read_text", return_value=lines):
                # Narrow the mock: only the incident_log path triggers this
                status, detail = self.audit.check_incident_log()
        finally:
            tmp.unlink(missing_ok=True)

        # With 2 TEST incidents and 1 real incident (today), should be DEGRADED with 1 real
        self.assertEqual(status, "DEGRADED")
        self.assertIn("1", detail)
        self.assertIn("2 test acct filtered", detail)

    def test_all_test_accounts_returns_ok_zero_today(self):
        lines = "\n".join([
            self._make_incident("A1_E2E_TEST"),
            self._make_incident("A1_TEST"),
        ])
        with mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("pathlib.Path.read_text", return_value=lines):
            status, detail = self.audit.check_incident_log()
        self.assertEqual(status, "OK")
        self.assertIn("0 today", detail)
        self.assertIn("2 test acct filtered", detail)

    def test_no_test_incidents_no_filter_note(self):
        # No incidents at all (empty file)
        with mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("pathlib.Path.read_text", return_value=""):
            status, detail = self.audit.check_incident_log()
        self.assertEqual(status, "OK")
        self.assertNotIn("filtered", detail)

    def test_real_incidents_today_show_correctly(self):
        lines = "\n".join([
            self._make_incident("account1"),
            self._make_incident("account2"),
            self._make_incident("A1_TEST"),  # should be excluded
        ])
        with mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("pathlib.Path.read_text", return_value=lines):
            status, detail = self.audit.check_incident_log()
        self.assertEqual(status, "DEGRADED")
        self.assertIn("2 real incident(s) today", detail)
        self.assertIn("1 test acct filtered", detail)

    def test_missing_file_returns_broken(self):
        with mock.patch("pathlib.Path.exists", return_value=False):
            status, detail = self.audit.check_incident_log()
        self.assertEqual(status, "BROKEN")
        self.assertIn("missing", detail)


# ── Suite 3: A2 debate status ─────────────────────────────────────────────────

class TestA2DebateStatus(unittest.TestCase):
    """check_a2_debate_status() reflects whether the debate has ever run.

    Production reads from data/account2/decisions/a2_dec_*.json — one file
    per decision, globbed from a real directory. These tests populate a
    real temp dir with per-decision JSON files and point the audit module's
    DATA constant at it for the duration of each test.
    """

    def setUp(self):
        self.audit = _load_audit()

    def _write_decision_files(self, dec_dir: Path, records: list[dict]) -> None:
        """Write each record as a separate a2_dec_*.json file in dec_dir."""
        dec_dir.mkdir(parents=True, exist_ok=True)
        for i, rec in enumerate(records):
            # Use sortable timestamps so glob() ordering is deterministic.
            fname = f"a2_dec_20260421_00{i:04d}00.json"
            (dec_dir / fname).write_text(json.dumps(rec))

    def _run_with_dir(self, tmp_root: Path, records: list[dict]) -> tuple[str, str]:
        """Set audit.DATA to a temp root containing account2/decisions/, then run."""
        dec_dir = tmp_root / "account2" / "decisions"
        self._write_decision_files(dec_dir, records)
        with mock.patch.object(self.audit, "DATA", tmp_root):
            return self.audit.check_a2_debate_status()

    def test_no_file_returns_degraded(self):
        # No decisions dir at all — production returns DEGRADED with "never run"
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(self.audit, "DATA", Path(tmp)):
                status, detail = self.audit.check_a2_debate_status()
        self.assertEqual(status, "DEGRADED")
        self.assertIn("never run", detail)

    def test_decisions_without_debate_input_returns_degraded(self):
        records = [{"symbol": "AAPL", "action": "HOLD", "debate_input": None}]
        with tempfile.TemporaryDirectory() as tmp:
            status, detail = self._run_with_dir(Path(tmp), records)
        self.assertEqual(status, "DEGRADED")
        self.assertIn("none with debate_input", detail)

    def test_decisions_with_debate_input_returns_ok(self):
        records = [
            {"symbol": "AAPL",
             "debate_input": {"bull": "...", "bear": "..."},
             "debate_parsed": {"reject": False, "confidence": 0.9,
                               "reasons": "PROCEED — strong setup"},
             "built_at": "2026-04-21T10:00:00Z"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            status, detail = self._run_with_dir(Path(tmp), records)
        self.assertEqual(status, "OK")
        self.assertIn("proceed", detail)   # production lower-cases "proceed"/"reject"
        self.assertIn("2026-04-21", detail)

    def test_debate_count_shown_in_detail(self):
        records = [
            {"symbol": "AAPL",
             "debate_input": {"bull": "x"},
             "debate_parsed": {"reject": False, "confidence": 0.85, "reasons": "ok"},
             "built_at": "2026-04-20T10:00:00Z"},
            {"symbol": "TSLA",
             "debate_input": {"bull": "y"},
             "debate_parsed": {"reject": True, "confidence": 0.5, "reasons": "veto"},
             "built_at": "2026-04-21T10:00:00Z"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            status, detail = self._run_with_dir(Path(tmp), records)
        self.assertEqual(status, "OK")
        # Production phrases the count as "N in last 50 files"
        self.assertIn("2 in last 50", detail)

    def test_dict_wrapper_format_supported(self):
        # Some legacy records carry the debate payload as a dict wrapper.
        # The audit only requires a non-null debate_input; non-list shapes
        # are still accepted as long as the field is present.
        records = [
            {"symbol": "SPY",
             "debate_input": {"x": 1},
             "debate_parsed": {"reject": False, "confidence": 0.8,
                               "reasons": "RESTRUCTURE — narrower spread"},
             "built_at": "2026-04-21T10:00:00Z"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            status, detail = self._run_with_dir(Path(tmp), records)
        self.assertEqual(status, "OK")
        self.assertIn("RESTRUCTURE", detail)

    def test_a2_debate_in_features_registry(self):
        names = [name for name, _ in self.audit.FEATURES]
        self.assertIn("A2 Debate Status", names)


if __name__ == "__main__":
    unittest.main()
