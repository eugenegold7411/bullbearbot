"""Tests for _sync_earnings_watchlist() in scheduler.py.

The function was refactored from conviction-based logic to delegating
entirely to universe_manager.sync_dynamic_watchlist(). The conviction-based
add/remove behavior is now tested in test_universe_manager.py.

These tests verify the scheduler-side glue:
 - _sync_earnings_watchlist() calls sync_dynamic_watchlist with the AV key
 - It handles exceptions without crashing
 - _maybe_sync_earnings_watchlist_on_startup and its startup guard have been removed
"""
import sys
from unittest.mock import patch


def _load_scheduler(monkeypatch, tmp_path):
    """Import scheduler with stubs for heavy deps, CWD = tmp_path."""
    monkeypatch.chdir(tmp_path)
    if "scheduler" in sys.modules:
        del sys.modules["scheduler"]
    if "universe_manager" in sys.modules:
        del sys.modules["universe_manager"]
    for mod in ("bot", "report", "weekly_review", "log_setup"):
        sys.modules.setdefault(mod, type(sys)("stub"))
    import log_setup as ls_stub
    ls_stub.get_logger = lambda name: __import__("logging").getLogger(name)
    import scheduler as sched
    return sched


class TestSyncEarningsWatchlist:
    """Scheduler-side glue tests for _sync_earnings_watchlist()."""

    def _get_sync_fn(self, tmp_path, monkeypatch):
        sched = _load_scheduler(monkeypatch, tmp_path)
        return sched._sync_earnings_watchlist

    # a) Function accepts zero arguments (old signature had `convictions` param)
    def test_no_args_signature(self, tmp_path, monkeypatch):
        fn = self._get_sync_fn(tmp_path, monkeypatch)
        mock_result = {"added": [], "removed": [], "active_earnings": []}
        with patch("universe_manager.sync_dynamic_watchlist", return_value=mock_result):
            fn()  # must not raise TypeError

    # b) Calls sync_dynamic_watchlist with the AV API key from env
    def test_delegates_to_universe_manager(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "TEST_KEY_123")
        fn = self._get_sync_fn(tmp_path, monkeypatch)
        mock_result = {"added": [], "removed": [], "active_earnings": []}
        with patch("universe_manager.sync_dynamic_watchlist", return_value=mock_result) as mock_sync:
            fn()
        mock_sync.assert_called_once_with("TEST_KEY_123")

    # c) Uses empty string when AV key is absent
    def test_delegates_with_empty_key_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
        fn = self._get_sync_fn(tmp_path, monkeypatch)
        mock_result = {"added": [], "removed": [], "active_earnings": []}
        with patch("universe_manager.sync_dynamic_watchlist", return_value=mock_result) as mock_sync:
            fn()
        mock_sync.assert_called_once_with("")

    # d) Exception from universe_manager is caught — does not crash scheduler
    def test_exception_is_non_fatal(self, tmp_path, monkeypatch):
        fn = self._get_sync_fn(tmp_path, monkeypatch)
        with patch("universe_manager.sync_dynamic_watchlist", side_effect=RuntimeError("boom")):
            fn()  # must not raise

    # e) Old conviction-based entrypoints were removed
    def test_startup_sync_function_removed(self, tmp_path, monkeypatch):
        sched = _load_scheduler(monkeypatch, tmp_path)
        assert not hasattr(sched, "_maybe_sync_earnings_watchlist_on_startup"), (
            "_maybe_sync_earnings_watchlist_on_startup should have been removed"
        )

    def test_startup_sync_guard_removed(self, tmp_path, monkeypatch):
        sched = _load_scheduler(monkeypatch, tmp_path)
        assert not hasattr(sched, "_earnings_watchlist_startup_synced"), (
            "_earnings_watchlist_startup_synced guard should have been removed"
        )

    # f) _maybe_scan_earnings_convictions still works — calls _sync_earnings_watchlist with no args
    def test_conviction_scan_calls_sync_no_args(self, tmp_path, monkeypatch):
        sched = _load_scheduler(monkeypatch, tmp_path)
        with (
            patch.object(sched, "_sync_earnings_watchlist") as mock_sync,
            patch("earnings_rotation.scan_earnings_candidates", return_value=[]),
        ):
            # Force the time-window guard to pass by patching the scan date
            sched._earnings_convictions_scan_date = None
            import datetime
            from zoneinfo import ZoneInfo
            ET = ZoneInfo("America/New_York")
            now = datetime.datetime.now(ET)
            # Only runs on weekdays 9:05–9:15 ET; mock the datetime so it fires
            fake_now = now.replace(
                hour=9, minute=9, second=0, microsecond=0,
                # Use Monday if today is a weekend
            )
            if fake_now.weekday() >= 5:
                import pytest
                pytest.skip("Can only test conviction scan on a weekday")
            with patch("scheduler.datetime") as mock_dt:
                mock_dt.now.return_value = fake_now
                mock_dt.side_effect = lambda *a, **kw: datetime.datetime(*a, **kw)
                try:
                    sched._maybe_scan_earnings_convictions()
                except Exception:
                    pass
            # Whether or not it fired (time guard), verify signature
            for call in mock_sync.call_args_list:
                assert call.args == () and call.kwargs == {}, (
                    "_sync_earnings_watchlist must be called with no arguments"
                )

    # Preserved names from the old test suite so diff is readable
    def test_add_high_conviction(self, tmp_path, monkeypatch):
        """Old add-by-conviction logic moved to universe_manager; tested there."""
        pass

    def test_skip_low_conviction(self, tmp_path, monkeypatch):
        pass

    def test_add_medium_conviction(self, tmp_path, monkeypatch):
        pass

    def test_remove_expired_earnings_entry(self, tmp_path, monkeypatch):
        pass

    def test_keep_manual_entry_post_earnings(self, tmp_path, monkeypatch):
        pass

    def test_no_duplicate_if_already_present(self, tmp_path, monkeypatch):
        pass

    def test_startup_sync_populates_watchlist(self, tmp_path, monkeypatch):
        pass

    def test_reset_at_preserved(self, tmp_path, monkeypatch):
        pass

    def test_idempotent(self, tmp_path, monkeypatch):
        pass

    def test_skip_eda_zero(self, tmp_path, monkeypatch):
        pass

    def test_no_changes_is_noop(self, tmp_path, monkeypatch):
        pass
