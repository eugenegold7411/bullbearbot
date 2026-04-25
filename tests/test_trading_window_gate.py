"""
Tests for the hard trading-window gate (9:25 AM–4:15 PM ET, weekdays).

Covers:
  - is_claude_trading_window() in bot_stage3_decision (boundary + weekend +
    feature-flag override)
  - scheduler._maybe_run_options_close_check() off-hours behavior
  - scheduler._maybe_refresh_qualitative_context() — 2 AM slot removed,
    event-driven path gated to 4 AM–8 PM ET
"""
from __future__ import annotations

import importlib
import sys
import types
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

# Some test files in this suite (e.g. test_t022_t023_t025.py,
# test_weekly_review_agent6.py) install an empty stub `scheduler` module into
# sys.modules to short-circuit imports from weekly_review. If we run after
# them in the same pytest session that stub has no `datetime`,
# `_is_claude_trading_window`, etc. Force-reload to the real module here.
if isinstance(sys.modules.get("scheduler"), types.ModuleType) and not hasattr(
    sys.modules.get("scheduler"), "_is_claude_trading_window"
):
    del sys.modules["scheduler"]

import bot_stage3_decision  # noqa: E402
import scheduler  # noqa: E402

# If the import above re-resolved to the stub anyway (rare timing), reload.
if not hasattr(scheduler, "_is_claude_trading_window"):
    del sys.modules["scheduler"]
    scheduler = importlib.import_module("scheduler")

ET = ZoneInfo("America/New_York")


# ── is_claude_trading_window ──────────────────────────────────────────────────


class TestTradingWindowGate:
    def _gate(self, hour: int, minute: int, weekday: int = 0, cfg: dict | None = None) -> bool:
        # weekday=0 → Mon; pick a known Monday: 2026-04-20 = Mon
        base_date = {0: 20, 1: 21, 2: 22, 3: 23, 4: 24, 5: 25, 6: 26}[weekday]
        now_et = datetime(2026, 4, base_date, hour, minute, tzinfo=ET)
        return bot_stage3_decision.is_claude_trading_window(now_et=now_et, cfg=cfg)

    def test_market_hours_returns_true(self):
        assert self._gate(9, 30) is True
        assert self._gate(12, 0) is True
        assert self._gate(15, 59) is True

    def test_pre_open_at_925_returns_true(self):
        """9:25 AM ET weekday → inside window (warm-up start)."""
        assert self._gate(9, 25) is True

    def test_just_before_pre_open_returns_false(self):
        """9:24 AM ET weekday → outside window."""
        assert self._gate(9, 24) is False

    def test_exactly_at_cutoff_returns_true(self):
        """4:15 PM ET weekday → inside window (inclusive)."""
        assert self._gate(16, 15) is True

    def test_exactly_after_cutoff_returns_false(self):
        """4:16 PM ET weekday → outside window."""
        assert self._gate(16, 16) is False

    def test_post_close_returns_false(self):
        assert self._gate(17, 0) is False
        assert self._gate(20, 0) is False

    def test_overnight_returns_false(self):
        assert self._gate(2, 0) is False
        assert self._gate(0, 0) is False
        assert self._gate(7, 0) is False

    def test_weekend_returns_false(self):
        # Saturday noon, Sunday 10 AM
        assert self._gate(12, 0, weekday=5) is False
        assert self._gate(10, 0, weekday=6) is False

    def test_gate_respects_feature_flag_off(self):
        """With hard_gate_claude_to_trading_window=false, gate always returns True."""
        cfg = {"feature_flags": {"hard_gate_claude_to_trading_window": False}}
        # Even at 3 AM on a Sunday, gate returns True when disabled
        now_et = datetime(2026, 4, 26, 3, 0, tzinfo=ET)  # Sunday
        assert bot_stage3_decision.is_claude_trading_window(now_et=now_et, cfg=cfg) is True

    def test_gate_respects_custom_window(self):
        cfg = {
            "feature_flags": {
                "hard_gate_claude_to_trading_window": True,
                "trading_window_start_et": "10:00",
                "trading_window_end_et":   "15:00",
            }
        }
        now = datetime(2026, 4, 20, 9, 30, tzinfo=ET)  # Monday 9:30
        assert bot_stage3_decision.is_claude_trading_window(now_et=now, cfg=cfg) is False
        now = datetime(2026, 4, 20, 14, 59, tzinfo=ET)
        assert bot_stage3_decision.is_claude_trading_window(now_et=now, cfg=cfg) is True
        now = datetime(2026, 4, 20, 15, 1, tzinfo=ET)
        assert bot_stage3_decision.is_claude_trading_window(now_et=now, cfg=cfg) is False

    def test_scheduler_helper_matches(self):
        """scheduler._is_claude_trading_window should agree with the canonical helper."""
        now = datetime(2026, 4, 20, 14, 0, tzinfo=ET)  # Monday 2 PM
        assert scheduler._is_claude_trading_window(now_et=now) is True
        now = datetime(2026, 4, 20, 17, 0, tzinfo=ET)  # Monday 5 PM
        assert scheduler._is_claude_trading_window(now_et=now) is False


# ── A2 close-check off-hours job ──────────────────────────────────────────────


class TestA2CloseCheckOffHours:
    def test_close_check_runs_after_market_close(self):
        """5:00 PM ET weekday → off-hours close-check fires."""
        called = {"hit": False}

        def _fake_close_loop(client):
            called["hit"] = True

        # Monday 17:00 ET
        fake_now = datetime(2026, 4, 20, 17, 0, tzinfo=ET)
        with patch.object(scheduler, "datetime") as mock_dt, \
             patch("bot_options_stage4_execution.close_check_loop", _fake_close_loop), \
             patch("bot_options._get_alpaca", return_value=object()):
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            scheduler._maybe_run_options_close_check(dry_run=False)
        assert called["hit"] is True

    def test_close_check_skips_during_market_hours(self):
        """10:00 AM ET → no-op (A2 inline path handles it)."""
        called = {"hit": False}

        def _fake_close_loop(client):
            called["hit"] = True

        fake_now = datetime(2026, 4, 20, 10, 0, tzinfo=ET)
        with patch.object(scheduler, "datetime") as mock_dt, \
             patch("bot_options_stage4_execution.close_check_loop", _fake_close_loop), \
             patch("bot_options._get_alpaca", return_value=object()):
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            scheduler._maybe_run_options_close_check(dry_run=False)
        assert called["hit"] is False

    def test_close_check_skips_weekend(self):
        called = {"hit": False}

        def _fake_close_loop(client):
            called["hit"] = True

        fake_now = datetime(2026, 4, 25, 12, 0, tzinfo=ET)  # Saturday
        with patch.object(scheduler, "datetime") as mock_dt, \
             patch("bot_options_stage4_execution.close_check_loop", _fake_close_loop), \
             patch("bot_options._get_alpaca", return_value=object()):
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            scheduler._maybe_run_options_close_check(dry_run=False)
        assert called["hit"] is False

    def test_dry_run_is_noop(self):
        called = {"hit": False}

        def _fake_close_loop(client):
            called["hit"] = True

        fake_now = datetime(2026, 4, 20, 21, 0, tzinfo=ET)  # Monday 9 PM
        with patch.object(scheduler, "datetime") as mock_dt, \
             patch("bot_options_stage4_execution.close_check_loop", _fake_close_loop), \
             patch("bot_options._get_alpaca", return_value=object()):
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            scheduler._maybe_run_options_close_check(dry_run=True)
        assert called["hit"] is False


# ── L1 qualitative sweep gate ─────────────────────────────────────────────────


class TestL1SweepGate:
    def _run_at(self, hour: int, minute: int = 0, weekday_offset: int = 0,
                last_key: str = "", news_hash: str = "abc123",
                last_news: str = "older"):
        """Drive _maybe_refresh_qualitative_context with controlled mocks.
        Returns (fired, reason_substr) — fired is whether a thread was started.
        """
        base_date = {0: 20, 1: 21, 2: 22, 3: 23, 4: 24, 5: 25, 6: 26}[weekday_offset]
        fake_now = datetime(2026, 4, base_date, hour, minute, tzinfo=ET)

        # Reset module-level state
        scheduler._qualitative_sweep_running = False
        scheduler._last_qualitative_sweep_key = last_key
        scheduler._last_qualitative_news_hash = last_news

        captured: dict = {"fired": False, "reason": ""}

        def _fake_thread(*args, **kwargs):
            captured["fired"] = True
            class _T:
                def start(self_inner): pass
            return _T()

        # Stub the watchlist + market_data + news_hash helpers used internally
        class _WM:
            @staticmethod
            def get_active_watchlist():
                return {"all": [{"symbol": "SPY"}], "stocks": ["SPY"], "etfs": [], "crypto": []}

        class _MD:
            @staticmethod
            def fetch_all(*a, **k):
                return {"news_block": "x"}

        with patch.object(scheduler, "datetime") as mock_dt, \
             patch.dict("sys.modules", {"watchlist_manager": _WM, "market_data": _MD}), \
             patch("bot_stage1_5_qualitative.news_hash_fingerprint", return_value=news_hash), \
             patch("bot_stage1_5_qualitative.context_age_minutes", return_value=999.0), \
             patch.object(scheduler.threading, "Thread", side_effect=_fake_thread):
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            scheduler._maybe_refresh_qualitative_context(dry_run=False)

        return captured["fired"]

    def test_2am_slot_no_longer_fires(self):
        """2:00 AM ET weekday → no scheduled fire (slot removed). Event path is
        also blocked because 2 AM is outside the 4 AM–8 PM event window."""
        assert self._run_at(2, 30) is False

    def test_6am_slot_still_fires(self):
        """6:30 AM ET weekday → scheduled fire."""
        assert self._run_at(6, 30) is True

    def test_10am_slot_still_fires(self):
        assert self._run_at(10, 15) is True

    def test_event_driven_blocked_at_3am(self):
        """3:00 AM weekday with news change → blocked by event-window gate."""
        assert self._run_at(3, 0, news_hash="new", last_news="old") is False

    def test_event_driven_fires_at_2pm(self):
        """2:00 PM weekday with news change → event-driven fire."""
        # Use last_key="" so scheduled-slot check (no slot at 14:00) falls through
        # to event-driven path. age_min=999 ensures the >30 m gate passes.
        assert self._run_at(14, 0, news_hash="new", last_news="old") is True

    def test_event_driven_blocked_late_night(self):
        """9:00 PM weekday → past 8 PM window, blocked."""
        assert self._run_at(21, 0, news_hash="new", last_news="old") is False
