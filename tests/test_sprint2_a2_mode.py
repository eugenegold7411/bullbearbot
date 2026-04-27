"""
tests/test_sprint2_a2_mode.py — Sprint 2 A2 mode file initialization tests.

Root cause: data/runtime/a2_mode.json is gitignored and excluded from rsync.
It is only created by divergence events (wired for A1 only) or operator action.
On a fresh server or after reboot the file is absent, causing preflight to block
A2 with reconcile_only indefinitely.

Fix: _ensure_account_modes_initialized() in scheduler.py creates both a1_mode.json
and a2_mode.json with NORMAL mode at scheduler startup if they are absent.

Suites:
  Build 1 — _ensure_account_modes_initialized creates missing files
  Build 2 — _ensure_account_modes_initialized does not overwrite existing files
  Build 3 — preflight returns reconcile_only when a2_mode.json is absent
  Build 4 — preflight returns go when a2_mode.json is present with NORMAL mode
  Build 5 — _ensure_account_modes_initialized is non-fatal on import error
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))
os.chdir(_BOT_DIR)

# ── Third-party stubs ─────────────────────────────────────────────────────────

_THIRD_PARTY_STUBS = {
    "dotenv":                          None,
    "anthropic":                       None,
    "alpaca":                          None,
    "alpaca.trading":                  None,
    "alpaca.trading.client":           None,
    "alpaca.trading.requests":         None,
    "alpaca.trading.enums":            None,
    "alpaca.data":                     None,
    "alpaca.data.enums":               None,
    "alpaca.data.historical":          None,
    "alpaca.data.historical.news":     None,
    "alpaca.data.requests":            None,
    "alpaca.data.timeframe":           None,
    "pandas":                          None,
    "yfinance":                        None,
}
for _stub_name, _stub_val in _THIRD_PARTY_STUBS.items():
    if _stub_name not in sys.modules:
        _m = mock.MagicMock()
        if _stub_name == "dotenv":
            _m.load_dotenv = mock.MagicMock()
        sys.modules[_stub_name] = _m


# ═══════════════════════════════════════════════════════════════════════════════
# Build 1 — _ensure_account_modes_initialized creates missing files
# ═══════════════════════════════════════════════════════════════════════════════

class TestEnsureAccountModesCreate(unittest.TestCase):
    """_ensure_account_modes_initialized must create mode files if absent."""

    def _run_init(self, runtime_dir: Path) -> None:
        """Run _ensure_account_modes_initialized with runtime dir redirected to tmp."""
        import divergence as div
        import scheduler
        with mock.patch.object(div, "RUNTIME_DIR", runtime_dir), \
             mock.patch("scheduler.datetime") as mock_dt:
            mock_dt.now.return_value.isoformat.return_value = "2026-04-27T00:00:00+00:00"
            mock_dt.now.return_value = mock.MagicMock()
            mock_dt.now.return_value.isoformat.return_value = "2026-04-27T00:00:00+00:00"
            # Patch get_mode_path to use our temp dir
            original_get_mode_path = div.get_mode_path
            with mock.patch.object(div, "get_mode_path",
                                   lambda a: runtime_dir / f"{a.lower()}_mode.json"):
                scheduler._ensure_account_modes_initialized()

    def test_creates_a2_mode_when_absent(self):
        """Must create a2_mode.json with NORMAL mode when absent."""
        with tempfile.TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp)
            self._run_init(runtime_dir)
            a2_path = runtime_dir / "a2_mode.json"
            self.assertTrue(a2_path.exists(), "a2_mode.json must be created")
            data = json.loads(a2_path.read_text())
            self.assertEqual(data["mode"], "normal")
            self.assertEqual(data["account"], "A2")
            self.assertEqual(data["entered_by"], "system_init")

    def test_creates_a1_mode_when_absent(self):
        """Must create a1_mode.json with NORMAL mode when absent."""
        with tempfile.TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp)
            self._run_init(runtime_dir)
            a1_path = runtime_dir / "a1_mode.json"
            self.assertTrue(a1_path.exists(), "a1_mode.json must be created")
            data = json.loads(a1_path.read_text())
            self.assertEqual(data["mode"], "normal")
            self.assertEqual(data["account"], "A1")
            self.assertEqual(data["entered_by"], "system_init")

    def test_creates_both_when_both_absent(self):
        """Must create both files when both are absent."""
        with tempfile.TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp)
            self._run_init(runtime_dir)
            self.assertTrue((runtime_dir / "a1_mode.json").exists())
            self.assertTrue((runtime_dir / "a2_mode.json").exists())

    def test_created_files_contain_expected_fields(self):
        """Created mode files must have all required AccountMode fields."""
        with tempfile.TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp)
            self._run_init(runtime_dir)
            for account, fname in (("A1", "a1_mode.json"), ("A2", "a2_mode.json")):
                data = json.loads((runtime_dir / fname).read_text())
                self.assertIn("account", data)
                self.assertIn("mode", data)
                self.assertIn("scope", data)
                self.assertIn("reason_detail", data)
                self.assertIn("entered_by", data)
                self.assertIn("recovery_condition", data)
                self.assertIn("version", data)
                self.assertEqual(data["version"], 1)


# ═══════════════════════════════════════════════════════════════════════════════
# Build 2 — _ensure_account_modes_initialized does not overwrite existing files
# ═══════════════════════════════════════════════════════════════════════════════

class TestEnsureAccountModesNoOverwrite(unittest.TestCase):
    """_ensure_account_modes_initialized must not overwrite existing mode files."""

    def _run_init(self, runtime_dir: Path) -> None:
        import divergence as div
        import scheduler
        with mock.patch.object(div, "get_mode_path",
                               lambda a: runtime_dir / f"{a.lower()}_mode.json"):
            scheduler._ensure_account_modes_initialized()

    def _write_mode(self, path: Path, account: str, mode: str) -> None:
        path.write_text(json.dumps({
            "account": account,
            "mode": mode,
            "scope": "account",
            "scope_id": "",
            "reason_code": "test",
            "reason_detail": "pre-existing state",
            "entered_at": "2026-04-27T00:00:00+00:00",
            "entered_by": "operator",
            "recovery_condition": "one_clean_cycle",
            "last_checked_at": "2026-04-27T00:00:00+00:00",
            "clean_cycles_since_entry": 0,
            "version": 1,
        }))

    def test_does_not_overwrite_a2_reconcile_only(self):
        """Must not overwrite a2_mode.json that already has reconcile_only."""
        with tempfile.TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp)
            self._write_mode(runtime_dir / "a2_mode.json", "A2", "reconcile_only")
            self._write_mode(runtime_dir / "a1_mode.json", "A1", "normal")
            self._run_init(runtime_dir)
            data = json.loads((runtime_dir / "a2_mode.json").read_text())
            self.assertEqual(data["mode"], "reconcile_only",
                             "existing reconcile_only mode must not be overwritten with normal")

    def test_does_not_overwrite_a1_halted(self):
        """Must not overwrite a1_mode.json that already has halted mode."""
        with tempfile.TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp)
            self._write_mode(runtime_dir / "a1_mode.json", "A1", "halted")
            self._write_mode(runtime_dir / "a2_mode.json", "A2", "normal")
            self._run_init(runtime_dir)
            data = json.loads((runtime_dir / "a1_mode.json").read_text())
            self.assertEqual(data["mode"], "halted",
                             "existing halted mode must not be overwritten with normal")

    def test_creates_absent_when_other_present(self):
        """Must create a2_mode.json when absent even if a1_mode.json is present."""
        with tempfile.TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp)
            self._write_mode(runtime_dir / "a1_mode.json", "A1", "normal")
            # a2_mode.json deliberately absent
            self._run_init(runtime_dir)
            self.assertTrue((runtime_dir / "a2_mode.json").exists(),
                            "a2_mode.json must be created when absent even if a1 is present")
            data = json.loads((runtime_dir / "a2_mode.json").read_text())
            self.assertEqual(data["mode"], "normal")


# ═══════════════════════════════════════════════════════════════════════════════
# Build 3 — preflight returns reconcile_only when a2_mode.json is absent
# ═══════════════════════════════════════════════════════════════════════════════

class TestPreflightBlocksWhenA2ModeAbsent(unittest.TestCase):
    """Preflight must return reconcile_only when a2_mode.json is absent."""

    def test_a2_preflight_reconcile_only_when_file_absent(self):
        """_check_operating_mode('a2') must return reconcile_only hint when file absent."""
        with tempfile.TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp)
            import preflight
            a2_path = runtime_dir / "a2_mode.json"
            # Confirm file does not exist
            self.assertFalse(a2_path.exists())

            with mock.patch.object(preflight, "_A2_MODE", a2_path):
                result = preflight._check_operating_mode("a2")

            self.assertFalse(result.passed)
            self.assertEqual(result.verdict_hint, "reconcile_only")
            self.assertIn("absent", result.message)

    def test_run_preflight_reconcile_only_for_a2_when_file_absent(self):
        """run_preflight(account_id='a2') must return reconcile_only verdict when file absent."""
        with tempfile.TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp)
            import preflight
            a2_path = runtime_dir / "a2_mode.json"

            with mock.patch.object(preflight, "_A2_MODE", a2_path), \
                 mock.patch.object(preflight, "_LOG_DIR", Path(tmp)), \
                 mock.patch.object(preflight, "_LOG_FILE", Path(tmp) / "preflight_log.jsonl"), \
                 mock.patch.object(preflight, "_A1_MODE",
                                   Path(tmp) / "a1_mode.json"):
                # Create valid a1_mode so only a2 blocks
                (Path(tmp) / "a1_mode.json").write_text(
                    json.dumps({"mode": "normal", "account": "A1"})
                )
                result = preflight.run_preflight(
                    caller="run_options_cycle",
                    account_id="a2",
                )

            self.assertEqual(result.verdict, "reconcile_only")
            self.assertTrue(any("operating_mode_a2" in b for b in result.blockers))


# ═══════════════════════════════════════════════════════════════════════════════
# Build 4 — preflight returns go when a2_mode.json is present with NORMAL mode
# ═══════════════════════════════════════════════════════════════════════════════

class TestPreflightGoWhenA2ModeNormal(unittest.TestCase):
    """Preflight must pass the a2 mode check when file exists with NORMAL mode."""

    def test_a2_operating_mode_passes_when_normal(self):
        """_check_operating_mode('a2') must pass when a2_mode.json contains NORMAL."""
        with tempfile.TemporaryDirectory() as tmp:
            a2_path = Path(tmp) / "a2_mode.json"
            a2_path.write_text(json.dumps({"mode": "normal", "account": "A2"}))

            import preflight
            with mock.patch.object(preflight, "_A2_MODE", a2_path):
                result = preflight._check_operating_mode("a2")

            self.assertTrue(result.passed)
            self.assertIsNone(result.verdict_hint)


# ═══════════════════════════════════════════════════════════════════════════════
# Build 5 — _ensure_account_modes_initialized is non-fatal
# ═══════════════════════════════════════════════════════════════════════════════

class TestEnsureAccountModesNonFatal(unittest.TestCase):
    """_ensure_account_modes_initialized must not raise on any failure."""

    def test_non_fatal_when_divergence_import_fails(self):
        """Must swallow exceptions — never propagate to scheduler startup."""
        import scheduler
        with mock.patch.dict(sys.modules, {"divergence": None}):
            try:
                scheduler._ensure_account_modes_initialized()
            except Exception as exc:
                self.fail(
                    f"_ensure_account_modes_initialized raised unexpectedly: {exc}"
                )

    def test_non_fatal_when_save_raises(self):
        """Must not raise when save_account_mode throws."""
        import divergence as div
        import scheduler
        with tempfile.TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp)
            with mock.patch.object(div, "get_mode_path",
                                   lambda a: runtime_dir / f"{a.lower()}_mode.json"), \
                 mock.patch.object(div, "save_account_mode",
                                   side_effect=OSError("disk full")):
                try:
                    scheduler._ensure_account_modes_initialized()
                except Exception as exc:
                    self.fail(
                        f"_ensure_account_modes_initialized raised unexpectedly: {exc}"
                    )


if __name__ == "__main__":
    unittest.main()
