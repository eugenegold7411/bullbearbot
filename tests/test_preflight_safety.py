"""
test_preflight_safety.py — P0 safety: DESYNC abort (T-003) and PID lockfile (T-002).

Suite A: run_preflight_desync_check() — T-003 DESYNC override
Suite B: _check_pid_lock()            — T-002 PID lockfile
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

# ── helpers ───────────────────────────────────────────────────────────────────

def _write_mode_file(directory: Path, mode: str) -> Path:
    """Write a minimal a1_mode.json with the given mode string."""
    path = directory / "a1_mode.json"
    path.write_text(json.dumps({
        "account": "A1",
        "mode": mode,
        "scope": "ACCOUNT",
        "scope_id": "A1",
        "reason": "",
        "reason_detail": "",
        "entered_at": "",
        "entered_by": "system",
        "recovery_condition": "one_clean_cycle",
        "last_checked_at": "",
        "version": 1,
    }))
    return path


# ── Suite A — DESYNC override (T-003) ────────────────────────────────────────

class TestDesyncOverride(unittest.TestCase):
    """Suite A — run_preflight_desync_check() gates cycles on fresh mode read."""

    @classmethod
    def setUpClass(cls):
        try:
            from preflight import run_preflight_desync_check
            cls.desync_check = staticmethod(run_preflight_desync_check)
        except ImportError as exc:
            raise unittest.SkipTest(f"preflight not importable: {exc}")

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_desync_override_fires_when_preflight_go_but_mode_halted(self):
        """DESYNC abort: preflight=go, a1_mode=HALTED → must return False (abort)."""
        mode_path = _write_mode_file(self.tmp, "HALTED")
        result = self.desync_check(mode_path=mode_path, preflight_verdict="go")
        self.assertFalse(
            result,
            "DESYNC check must return False (abort) when mode file says HALTED",
        )

    def test_desync_override_does_not_fire_when_both_normal(self):
        """No DESYNC: preflight=go, a1_mode=NORMAL → must return True (proceed)."""
        mode_path = _write_mode_file(self.tmp, "NORMAL")
        result = self.desync_check(mode_path=mode_path, preflight_verdict="go")
        self.assertTrue(
            result,
            "DESYNC check must return True (proceed) when mode file says NORMAL",
        )

    def test_desync_override_fires_for_risk_containment(self):
        """DESYNC abort: preflight=go_degraded, a1_mode=RISK_CONTAINMENT → abort."""
        mode_path = _write_mode_file(self.tmp, "RISK_CONTAINMENT")
        result = self.desync_check(mode_path=mode_path, preflight_verdict="go_degraded")
        self.assertFalse(
            result,
            "DESYNC check must abort when mode is RISK_CONTAINMENT",
        )

    def test_desync_override_fires_for_reconcile_only(self):
        """DESYNC abort: a1_mode=RECONCILE_ONLY → abort."""
        mode_path = _write_mode_file(self.tmp, "RECONCILE_ONLY")
        result = self.desync_check(mode_path=mode_path, preflight_verdict="go")
        self.assertFalse(
            result,
            "DESYNC check must abort when mode is RECONCILE_ONLY",
        )

    def test_desync_absent_mode_file_proceeds(self):
        """No mode file → no mode constraint; cycle may proceed."""
        absent_path = self.tmp / "a1_mode.json"  # does not exist
        result = self.desync_check(mode_path=absent_path, preflight_verdict="go")
        self.assertTrue(
            result,
            "DESYNC check must return True (proceed) when mode file is absent",
        )

    def test_desync_corrupt_mode_file_proceeds_with_caution(self):
        """Corrupt mode file (non-fatal) → proceeds rather than blocking the cycle."""
        bad_path = self.tmp / "a1_mode.json"
        bad_path.write_text("{not valid json")
        result = self.desync_check(mode_path=bad_path, preflight_verdict="go")
        self.assertTrue(
            result,
            "DESYNC check must not hard-block the cycle on unreadable mode file",
        )


# ── Suite B — PID lockfile (T-002) ───────────────────────────────────────────

class TestPidLockfile(unittest.TestCase):
    """Suite B — _check_pid_lock() prevents duplicate scheduler instances."""

    @classmethod
    def setUpClass(cls):
        # Ensure dotenv stub is present before scheduler is imported
        # (conftest.py does not stub dotenv, but scheduler → bot → dotenv)
        import sys  # noqa: E401
        import types
        if "dotenv" not in sys.modules:
            _m = types.ModuleType("dotenv")
            _m.load_dotenv = lambda *a, **kw: None  # type: ignore[attr-defined]
            sys.modules["dotenv"] = _m
        try:
            from scheduler import _check_pid_lock
            cls._check_pid_lock = staticmethod(_check_pid_lock)
        except ImportError as exc:
            raise unittest.SkipTest(f"scheduler not importable: {exc}")

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.pid_path = Path(self._tmpdir.name) / "scheduler.pid"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_stale_lockfile_cleaned_up_and_startup_proceeds(self):
        """Stale lockfile (dead PID) is silently removed; startup proceeds."""
        # PID 99999999 is virtually guaranteed to not exist
        self.pid_path.write_text("99999999")
        # Must not raise SystemExit
        self._check_pid_lock(pid_path=self.pid_path)
        self.assertFalse(
            self.pid_path.exists(),
            "Stale lockfile must be removed after cleanup",
        )

    def test_live_pid_raises_system_exit(self):
        """Lockfile holding the current process PID → SystemExit (refuse to dual-start)."""
        self.pid_path.write_text(str(os.getpid()))  # current process is definitely alive
        with self.assertRaises(SystemExit) as ctx:
            self._check_pid_lock(pid_path=self.pid_path)
        self.assertEqual(ctx.exception.code, 1, "SystemExit code must be 1")

    def test_no_lockfile_proceeds_normally(self):
        """No lockfile present → _check_pid_lock returns without raising."""
        # pid_path does not exist
        self._check_pid_lock(pid_path=self.pid_path)  # must not raise

    def test_unreadable_lockfile_treated_as_stale(self):
        """Non-integer lockfile content → treated as stale (removed, proceed)."""
        self.pid_path.write_text("not-a-pid")
        self._check_pid_lock(pid_path=self.pid_path)  # must not raise
        self.assertFalse(
            self.pid_path.exists(),
            "Unreadable lockfile must be cleaned up",
        )


if __name__ == "__main__":
    unittest.main()
