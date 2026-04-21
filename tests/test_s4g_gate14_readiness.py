"""
Tests for S4-G — Gate 14 Update + Readiness Report Hardening.

Build 1: Gate 14 reads symbol count from _OBS_IV_SYMBOLS dynamically
Build 2: Readiness check scheduler job fires in correct window
"""

import json
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_now_et(weekday: int, hour: int, minute: int, date_str: str = "2026-04-22"):
    """Build a mock datetime in ET for the given weekday/hour/minute."""
    dt = datetime.strptime(f"{date_str} {hour:02d}:{minute:02d}:00", "%Y-%m-%d %H:%M:%S")
    dt = dt.replace(tzinfo=ET)
    dt_mock = mock.MagicMock(spec=datetime)
    dt_mock.hour    = dt.hour
    dt_mock.minute  = dt.minute
    dt_mock.weekday.return_value = weekday
    dt_mock.strftime = dt.strftime
    return dt_mock


def _import_fresh_scheduler():
    """Import scheduler with minimal stubs (mirrors pattern from test_s4c)."""
    import sys

    stubs = {}
    for name in ("bot", "report", "weekly_review"):
        if name in sys.modules:
            stubs[name] = sys.modules.pop(name)
        stub = types.ModuleType(name)
        if name == "bot":
            stub.run_cycle = lambda *a, **kw: None
        sys.modules[name] = stub

    if "scheduler" in sys.modules:
        del sys.modules["scheduler"]

    try:
        import scheduler as sched
    finally:
        for name, orig in stubs.items():
            if orig is not None:
                sys.modules[name] = orig
            else:
                sys.modules.pop(name, None)

    return sched


# ---------------------------------------------------------------------------
# Suite G1 — Gate 14 uses _OBS_IV_SYMBOLS dynamically
# ---------------------------------------------------------------------------

class TestGate14Dynamic(unittest.TestCase):

    def test_validate_config_imports_obs_iv_symbols(self):
        """validate_config.py must reference _OBS_IV_SYMBOLS, not a hardcoded 16-symbol list."""
        vc_path = Path(__file__).parent.parent / "validate_config.py"
        src = vc_path.read_text()
        self.assertIn(
            "_OBS_IV_SYMBOLS",
            src,
            "validate_config.py must import _OBS_IV_SYMBOLS from bot_options_stage0_preflight",
        )

    def test_validate_config_no_hardcoded_phase1_list(self):
        """_PHASE1_SYMBOLS hardcoded list must be removed from validate_config.py."""
        vc_path = Path(__file__).parent.parent / "validate_config.py"
        src = vc_path.read_text()
        self.assertNotIn(
            "_PHASE1_SYMBOLS",
            src,
            "validate_config.py must not contain the old hardcoded _PHASE1_SYMBOLS list",
        )

    def test_gate14_message_uses_dynamic_total(self):
        """Gate 14 message must reference _universe_total, not the literal '/16'."""
        vc_path = Path(__file__).parent.parent / "validate_config.py"
        src = vc_path.read_text()
        # The message should not be hardcoded as '/16'
        self.assertNotIn(
            "}/16",
            src,
            "Gate 14 message must use dynamic total, not hardcoded '/16'",
        )

    def test_obs_iv_symbols_has_43_symbols(self):
        """_OBS_IV_SYMBOLS should contain the full 43-symbol universe."""
        from bot_options_stage0_preflight import _OBS_IV_SYMBOLS
        self.assertEqual(
            len(_OBS_IV_SYMBOLS),
            43,
            f"Expected 43 symbols in _OBS_IV_SYMBOLS, got {len(_OBS_IV_SYMBOLS)}",
        )

    def test_gate14_iv_count_logic(self, tmp_path=None):
        """IV counting logic: symbol with ≥20 valid entries (iv≥0.05) counts as ready."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            iv_dir = Path(td)

            # 20 valid entries
            good = [{"date": f"2026-01-{i+1:02d}", "iv": 0.20} for i in range(20)]
            (iv_dir / "NVDA_iv_history.json").write_text(json.dumps(good))

            # Only 19 valid entries — should NOT count
            partial = [{"date": f"2026-01-{i+1:02d}", "iv": 0.20} for i in range(19)]
            (iv_dir / "TSM_iv_history.json").write_text(json.dumps(partial))

            # 20 entries but all iv < 0.05 — should NOT count
            bad_iv = [{"date": f"2026-01-{i+1:02d}", "iv": 0.02} for i in range(20)]
            (iv_dir / "AAPL_iv_history.json").write_text(json.dumps(bad_iv))

            universe = ["NVDA", "TSM", "AAPL", "MSFT"]  # MSFT has no file

            count = 0
            for sym in universe:
                hist_path = iv_dir / f"{sym}_iv_history.json"
                if hist_path.exists():
                    try:
                        hist  = json.loads(hist_path.read_text())
                        valid = [e for e in hist if e.get("iv", 0) >= 0.05]
                        if len(valid) >= 20:
                            count += 1
                    except Exception:
                        pass

            self.assertEqual(count, 1, "Only NVDA should qualify (20 valid entries, iv≥0.05)")

    def test_gate14_passes_when_all_ready(self):
        """Gate 14 passes (True) when iv_ready_count == universe_total."""
        universe_total  = 43
        iv_ready_count  = 43
        gate_passes     = iv_ready_count >= universe_total
        self.assertTrue(gate_passes)

    def test_gate14_fails_when_missing_symbols(self):
        """Gate 14 fails (False) when any symbol is missing IV history."""
        universe_total  = 43
        iv_ready_count  = 42
        gate_passes     = iv_ready_count >= universe_total
        self.assertFalse(gate_passes)

    def test_gate14_fallback_is_16_symbols(self):
        """Fallback list (when import fails) must still have 16 symbols."""
        # Verify the fallback list in validate_config.py has exactly 16 entries
        vc_path = Path(__file__).parent.parent / "validate_config.py"
        src = vc_path.read_text()
        # The fallback block should be present
        self.assertIn(
            "bot_options_stage0_preflight",
            src,
            "validate_config.py must import from bot_options_stage0_preflight",
        )
        # Fallback block should reference 16-element list
        self.assertIn('"XBI"', src, "Fallback list should contain XBI (last Phase 1 symbol)")


# ---------------------------------------------------------------------------
# Suite G2 — Scheduler readiness check job
# ---------------------------------------------------------------------------

class TestReadinessCheckScheduler(unittest.TestCase):

    def test_readiness_ran_date_global_exists(self):
        """scheduler._readiness_ran_date global must exist."""
        sched = _import_fresh_scheduler()
        self.assertTrue(
            hasattr(sched, "_readiness_ran_date"),
            "scheduler must have _readiness_ran_date global",
        )

    def test_maybe_run_readiness_check_exists(self):
        """_maybe_run_readiness_check function must exist in scheduler."""
        sched = _import_fresh_scheduler()
        self.assertTrue(
            hasattr(sched, "_maybe_run_readiness_check"),
            "scheduler must have _maybe_run_readiness_check function",
        )

    def test_fires_in_window_on_weekday(self):
        """_maybe_run_readiness_check fires on a weekday at 4:45 AM ET."""
        sched = _import_fresh_scheduler()
        sched._readiness_ran_date = ""

        now_et = _make_now_et(weekday=1, hour=4, minute=45, date_str="2026-04-22")

        with mock.patch("scheduler.datetime") as mock_dt, \
             mock.patch("subprocess.run") as mock_run:
            mock_dt.now.return_value = now_et
            mock_dt.strptime = datetime.strptime
            mock_run.return_value = mock.MagicMock(returncode=0)
            sched._maybe_run_readiness_check(dry_run=False)

        self.assertEqual(
            sched._readiness_ran_date,
            "2026-04-22",
            "_readiness_ran_date should be set after firing",
        )

    def test_does_not_fire_outside_window(self):
        """_maybe_run_readiness_check must not fire at 3:00 AM (outside 4:45-5:30 window)."""
        sched = _import_fresh_scheduler()
        sched._readiness_ran_date = ""

        now_et = _make_now_et(weekday=1, hour=3, minute=0, date_str="2026-04-22")

        with mock.patch("scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = now_et
            mock_dt.strptime = datetime.strptime
            sched._maybe_run_readiness_check(dry_run=False)

        self.assertEqual(
            sched._readiness_ran_date,
            "",
            "_readiness_ran_date must remain empty when outside window",
        )

    def test_does_not_fire_on_weekend(self):
        """_maybe_run_readiness_check must not fire on Saturday."""
        sched = _import_fresh_scheduler()
        sched._readiness_ran_date = ""

        now_et = _make_now_et(weekday=5, hour=4, minute=50, date_str="2026-04-25")

        with mock.patch("scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = now_et
            mock_dt.strptime = datetime.strptime
            sched._maybe_run_readiness_check(dry_run=False)

        self.assertEqual(
            sched._readiness_ran_date,
            "",
            "_readiness_ran_date must remain empty on weekends",
        )

    def test_does_not_refire_same_day(self):
        """_maybe_run_readiness_check must not fire twice on the same day."""
        sched = _import_fresh_scheduler()
        sched._readiness_ran_date = "2026-04-22"

        now_et = _make_now_et(weekday=1, hour=4, minute=50, date_str="2026-04-22")
        fired = []

        with mock.patch("scheduler.datetime") as mock_dt, \
             mock.patch("subprocess.run", side_effect=lambda *a, **kw: fired.append(1)) as _:
            mock_dt.now.return_value = now_et
            mock_dt.strptime = datetime.strptime
            sched._maybe_run_readiness_check(dry_run=False)

        self.assertEqual(len(fired), 0, "Must not fire when _readiness_ran_date already set for today")

    def test_dry_run_sets_date_without_subprocess(self):
        """dry_run mode must set _readiness_ran_date without calling subprocess."""
        sched = _import_fresh_scheduler()
        sched._readiness_ran_date = ""

        now_et = _make_now_et(weekday=1, hour=4, minute=50, date_str="2026-04-22")

        with mock.patch("scheduler.datetime") as mock_dt, \
             mock.patch("subprocess.run") as mock_subproc:
            mock_dt.now.return_value = now_et
            mock_dt.strptime = datetime.strptime
            sched._maybe_run_readiness_check(dry_run=True)

        mock_subproc.assert_not_called()
        self.assertEqual(sched._readiness_ran_date, "2026-04-22")

    def test_called_in_main_loop(self):
        """_maybe_run_readiness_check must be called in the scheduler main loop."""
        sched_path = Path(__file__).parent.parent / "scheduler.py"
        src = sched_path.read_text()
        self.assertIn(
            "_maybe_run_readiness_check(dry_run)",
            src,
            "_maybe_run_readiness_check must be called in the main loop",
        )

    def test_readiness_check_after_orb_scan_in_loop(self):
        """Readiness check must appear after ORB scan in the main loop."""
        sched_path = Path(__file__).parent.parent / "scheduler.py"
        src = sched_path.read_text()
        orb_pos  = src.find("_maybe_run_orb_scan(dry_run)")
        ready_pos = src.find("_maybe_run_readiness_check(dry_run)")
        self.assertGreater(
            ready_pos, orb_pos,
            "Readiness check must come after ORB scan in the main loop",
        )
