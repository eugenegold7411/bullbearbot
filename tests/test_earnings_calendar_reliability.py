"""
tests/test_earnings_calendar_reliability.py — Issue 32: earnings calendar reliability.

data_warehouse:
  1. test_earnings_calendar_path_constant
  2. test_staleness_fresh_file
  3. test_staleness_stale_file
  4. test_staleness_missing_file
  5. test_staleness_no_timestamp

scheduler:
  6. test_daily_refresh_failure_fires_alert
  7. test_staleness_check_retries_on_stale
  8. test_staleness_check_no_retry_when_fresh
  9. test_staleness_check_alert_when_retry_fails
  10. test_staleness_check_date_guard_prevents_double_run
"""

from __future__ import annotations

import json
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

# ── helpers ───────────────────────────────────────────────────────────────────

def _iso_hours_ago(h: float) -> str:
    from datetime import timedelta
    dt = datetime.now(timezone.utc) - timedelta(hours=h)
    return dt.isoformat()


def _write_calendar(directory: Path, fetched_iso: str, entries: list | None = None) -> None:
    (directory / "earnings_calendar.json").write_text(
        json.dumps({"fetched_at": fetched_iso, "calendar": entries or []})
    )


_MISSING = object()


def _import_fresh_scheduler():
    """Load a clean scheduler module with minimal stubs."""
    bot_stub = types.ModuleType("bot")
    bot_stub.run_cycle = lambda *a, **kw: None
    wr_stub = types.ModuleType("weekly_review")
    wr_stub.run_review = lambda *a, **kw: ""
    rpt_stub = types.ModuleType("report")
    rpt_stub.send_report_email = mock.MagicMock()
    rpt_stub.send_alert_email = mock.MagicMock()
    rpt_stub._get_account = lambda: None
    rpt_stub._get_positions = lambda: []

    scoped = {"bot": bot_stub, "weekly_review": wr_stub, "report": rpt_stub}
    saved = {k: sys.modules.get(k, _MISSING) for k in scoped}

    sys.modules.pop("scheduler", None)
    for k, v in scoped.items():
        sys.modules[k] = v
    try:
        import scheduler as sched
    finally:
        for k, s in saved.items():
            if s is _MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = s
    return sched


def _make_now_et(weekday: int, hour: int, minute: int, date_str: str):
    now = mock.MagicMock()
    now.weekday.return_value = weekday
    now.hour = hour
    now.minute = minute
    now.strftime.return_value = date_str
    return now


# ══ Group 1: data_warehouse constant and staleness ════════════════════════════

class TestEarningsCalendarPathConstant(unittest.TestCase):
    def test_earnings_calendar_path_constant(self):
        """EARNINGS_CALENDAR_PATH must be a Path resolving to data/market/earnings_calendar.json."""
        import data_warehouse as dw
        path = dw.EARNINGS_CALENDAR_PATH
        self.assertIsInstance(path, Path)
        self.assertTrue(
            str(path).endswith("data/market/earnings_calendar.json"),
            f"Expected path ending in data/market/earnings_calendar.json, got {path}",
        )


class TestGetEarningsCalendarStaleness(unittest.TestCase):
    def _patched_dw(self, market_dir: Path):
        return mock.patch("data_warehouse.MARKET_DIR", market_dir)

    def test_staleness_fresh_file(self):
        """File refreshed 5h ago → stale=False, hours_old<25."""
        import tempfile

        import data_warehouse as dw
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _write_calendar(tmp_path, _iso_hours_ago(5))
            with self._patched_dw(tmp_path):
                result = dw.get_earnings_calendar_staleness()
        self.assertFalse(result["stale"], f"5h-old calendar must not be stale, got {result}")
        self.assertIsNotNone(result["hours_old"])
        self.assertLess(result["hours_old"], 25)

    def test_staleness_stale_file(self):
        """File refreshed 30h ago → stale=True, hours_old≥25."""
        import tempfile

        import data_warehouse as dw
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _write_calendar(tmp_path, _iso_hours_ago(30))
            with self._patched_dw(tmp_path):
                result = dw.get_earnings_calendar_staleness()
        self.assertTrue(result["stale"], f"30h-old calendar must be stale, got {result}")
        self.assertGreaterEqual(result["hours_old"], 25)

    def test_staleness_missing_file(self):
        """No calendar file → stale=True, hours_old=None, message contains 'missing'."""
        import tempfile

        import data_warehouse as dw
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Do not write any file
            with self._patched_dw(tmp_path):
                result = dw.get_earnings_calendar_staleness()
        self.assertTrue(result["stale"])
        self.assertIsNone(result["hours_old"])
        self.assertIn("missing", result["message"].lower())

    def test_staleness_no_timestamp(self):
        """File with no fetched_at or last_daily_refresh_at → stale=True, hours_old=None."""
        import tempfile

        import data_warehouse as dw
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "earnings_calendar.json").write_text(
                json.dumps({"calendar": []})  # no fetched_at key
            )
            with self._patched_dw(tmp_path):
                result = dw.get_earnings_calendar_staleness()
        self.assertTrue(result["stale"])
        self.assertIsNone(result["hours_old"])


# ══ Group 2: scheduler refresh + alert wiring ════════════════════════════════

class TestSchedulerEarningsReliability(unittest.TestCase):

    def test_daily_refresh_failure_fires_alert(self):
        """_maybe_refresh_earnings_calendar_av_daily: failure must call _fire_safety_alert."""
        sched = _import_fresh_scheduler()
        sched._earnings_av_daily_ran_date = None

        mock_dw = mock.MagicMock()
        mock_dw.refresh_earnings_calendar_av.side_effect = RuntimeError("AV 429")

        now_et = _make_now_et(weekday=1, hour=4, minute=10, date_str="2026-05-07")

        with mock.patch("scheduler.datetime") as mock_dt, \
             mock.patch.dict(sys.modules, {"data_warehouse": mock_dw}), \
             mock.patch.object(sched, "_fire_safety_alert") as mock_alert:
            mock_dt.now.return_value = now_et
            sched._maybe_refresh_earnings_calendar_av_daily(dry_run=False)

        mock_alert.assert_called_once()
        call_args = mock_alert.call_args[0]
        self.assertEqual(call_args[0], "_maybe_refresh_earnings_calendar_av_daily")
        self.assertIsInstance(call_args[1], RuntimeError)

    def test_staleness_check_retries_on_stale(self):
        """_maybe_check_earnings_calendar_staleness: stale calendar triggers refresh call."""
        sched = _import_fresh_scheduler()
        sched._earnings_stale_check_date = None

        mock_dw = mock.MagicMock()
        mock_dw.get_earnings_calendar_staleness.return_value = {
            "stale": True, "hours_old": 30.5, "warning": False,
            "message": "30h old", "entry_count": 3, "last_refresh": "2026-05-06T05:00:00",
        }
        mock_dw.refresh_earnings_calendar_av.return_value = {"calendar": []}

        now_et = _make_now_et(weekday=2, hour=8, minute=30, date_str="2026-05-07")

        with mock.patch("scheduler.datetime") as mock_dt, \
             mock.patch.dict(sys.modules, {"data_warehouse": mock_dw}):
            mock_dt.now.return_value = now_et
            sched._maybe_check_earnings_calendar_staleness(dry_run=False)

        mock_dw.refresh_earnings_calendar_av.assert_called_once()

    def test_staleness_check_no_retry_when_fresh(self):
        """_maybe_check_earnings_calendar_staleness: fresh calendar must NOT trigger refresh."""
        sched = _import_fresh_scheduler()
        sched._earnings_stale_check_date = None

        mock_dw = mock.MagicMock()
        mock_dw.get_earnings_calendar_staleness.return_value = {
            "stale": False, "hours_old": 6.0, "warning": False,
            "message": None, "entry_count": 12, "last_refresh": "2026-05-07T04:05:00",
        }

        now_et = _make_now_et(weekday=2, hour=8, minute=30, date_str="2026-05-07")

        with mock.patch("scheduler.datetime") as mock_dt, \
             mock.patch.dict(sys.modules, {"data_warehouse": mock_dw}):
            mock_dt.now.return_value = now_et
            sched._maybe_check_earnings_calendar_staleness(dry_run=False)

        mock_dw.refresh_earnings_calendar_av.assert_not_called()

    def test_staleness_check_alert_when_retry_fails(self):
        """_maybe_check_earnings_calendar_staleness: retry failure must fire alert."""
        sched = _import_fresh_scheduler()
        sched._earnings_stale_check_date = None

        mock_dw = mock.MagicMock()
        mock_dw.get_earnings_calendar_staleness.return_value = {
            "stale": True, "hours_old": 50.0, "warning": True,
            "message": "50h old", "entry_count": 0, "last_refresh": None,
        }
        mock_dw.refresh_earnings_calendar_av.side_effect = RuntimeError("AV timeout")

        now_et = _make_now_et(weekday=2, hour=8, minute=30, date_str="2026-05-07")

        with mock.patch("scheduler.datetime") as mock_dt, \
             mock.patch.dict(sys.modules, {"data_warehouse": mock_dw}), \
             mock.patch.object(sched, "_fire_safety_alert") as mock_alert:
            mock_dt.now.return_value = now_et
            sched._maybe_check_earnings_calendar_staleness(dry_run=False)

        mock_alert.assert_called_once()
        call_args = mock_alert.call_args[0]
        self.assertEqual(call_args[0], "_maybe_check_earnings_calendar_staleness")

    def test_staleness_check_date_guard_prevents_double_run(self):
        """_maybe_check_earnings_calendar_staleness: already-ran date must prevent re-execution."""
        sched = _import_fresh_scheduler()
        sched._earnings_stale_check_date = "2026-05-07"  # already ran today

        mock_dw = mock.MagicMock()

        now_et = _make_now_et(weekday=2, hour=8, minute=30, date_str="2026-05-07")

        with mock.patch("scheduler.datetime") as mock_dt, \
             mock.patch.dict(sys.modules, {"data_warehouse": mock_dw}):
            mock_dt.now.return_value = now_et
            sched._maybe_check_earnings_calendar_staleness(dry_run=False)

        mock_dw.get_earnings_calendar_staleness.assert_not_called()
        mock_dw.refresh_earnings_calendar_av.assert_not_called()


if __name__ == "__main__":
    unittest.main()
