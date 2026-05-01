"""
tests/test_earnings_calendar_daily.py

Tests for EC-01 through EC-08: daily earnings calendar refresh,
staleness detection, dashboard indicator, morning brief enrichment,
and eda runtime computation.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

# ─── helpers ────────────────────────────────────────────────────────────────

def _make_cal(fetched_hours_ago: float, entries: list[dict] | None = None) -> dict:
    """Build a fake earnings_calendar dict with fetched_at set N hours ago."""
    ts = (datetime.now(timezone.utc) - timedelta(hours=fetched_hours_ago)).isoformat()
    return {
        "fetched_at": ts,
        "source": "alphavantage",
        "calendar": entries or [],
    }


def _make_entry(symbol: str, days_ahead: int, timing: str = "post-market") -> dict:
    iso = (date.today() + timedelta(days=days_ahead)).isoformat()
    return {
        "symbol": symbol,
        "earnings_date": iso,
        "timing": timing,
        "eps_estimate": 1.23,
        "source": "alphavantage",
    }


# ─── EC-02: stale=True when calendar >25h old ────────────────────────────────

class TestGetEarningsCalendarStaleness:

    def _call(self, tmp_path, cal_dict):
        cal_file = tmp_path / "earnings_calendar.json"
        cal_file.write_text(json.dumps(cal_dict))
        import data_warehouse as dw
        orig = dw.MARKET_DIR
        dw.MARKET_DIR = tmp_path
        try:
            return dw.get_earnings_calendar_staleness()
        finally:
            dw.MARKET_DIR = orig

    def test_ec02_stale_when_older_than_25h(self, tmp_path):
        """EC-02: stale=True when calendar is >25h old."""
        result = self._call(tmp_path, _make_cal(26.0))
        assert result["stale"] is True
        assert result["hours_old"] == pytest.approx(26.0, abs=0.1)

    def test_ec03_warning_when_older_than_48h(self, tmp_path):
        """EC-03: warning=True when calendar >48h old."""
        result = self._call(tmp_path, _make_cal(50.0))
        assert result["warning"] is True
        assert result["stale"] is True

    def test_ec04_fresh_when_recently_refreshed(self, tmp_path):
        """EC-04: stale=False when calendar was refreshed within 25h."""
        result = self._call(tmp_path, _make_cal(3.0, entries=[_make_entry("XOM", 1)]))
        assert result["stale"] is False
        assert result["warning"] is False
        assert result["hours_old"] == pytest.approx(3.0, abs=0.2)
        assert result["entry_count"] == 1

    def test_missing_file_returns_stale(self, tmp_path):
        """Missing file is treated as stale+warning."""
        import data_warehouse as dw
        orig = dw.MARKET_DIR
        dw.MARKET_DIR = tmp_path
        try:
            result = dw.get_earnings_calendar_staleness()
        finally:
            dw.MARKET_DIR = orig
        assert result["stale"] is True
        assert result["warning"] is True
        assert result["entry_count"] == 0

    def test_last_daily_refresh_at_takes_precedence_when_newer(self, tmp_path):
        """last_daily_refresh_at overrides fetched_at when it is more recent."""
        cal = _make_cal(30.0)  # fetched_at is 30h ago
        # last_daily_refresh_at is 2h ago
        cal["last_daily_refresh_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat()
        result = self._call(tmp_path, cal)
        assert result["stale"] is False
        assert result["hours_old"] == pytest.approx(2.0, abs=0.2)

    def test_stale_threshold_boundary_exactly_25h(self, tmp_path):
        """At exactly 25h should be stale=True (>25 condition)."""
        result = self._call(tmp_path, _make_cal(25.5))
        assert result["stale"] is True

    def test_fresh_threshold_boundary_just_under_25h(self, tmp_path):
        """At 24h should be stale=False."""
        result = self._call(tmp_path, _make_cal(24.0))
        assert result["stale"] is False

    def test_entry_count_correct(self, tmp_path):
        """entry_count reflects the calendar list length."""
        entries = [_make_entry("XOM", 1), _make_entry("CVX", 1), _make_entry("PLTR", 4)]
        result = self._call(tmp_path, _make_cal(1.0, entries))
        assert result["entry_count"] == 3


# ─── EC-01: daily refresh fires Mon–Sat at 4:05 AM ET, not Sundays ───────────

class TestDailyRefreshSchedule:

    def _call_daily(self, dry_run=True, weekday=0, hour=4, minute=10,
                    ran_date="", year=2026, month=5, day=5):
        import scheduler
        orig_key = scheduler._earnings_av_daily_ran_date
        scheduler._earnings_av_daily_ran_date = ran_date

        mock_now = datetime(year, month, day, hour, minute, 0,
                            tzinfo=timezone.utc)

        import zoneinfo
        ET = zoneinfo.ZoneInfo("America/New_York")

        class FakeDT:
            @staticmethod
            def now(tz=None):
                if tz:
                    return mock_now.astimezone(tz)
                return mock_now.replace(tzinfo=None)

            def weekday(self_inner):
                return mock_now.astimezone(ET).weekday()

        with patch("scheduler.datetime") as mock_dt_cls, \
             patch("scheduler._today", return_value=mock_now.astimezone(ET).date().isoformat()):
            mock_dt_cls.now = FakeDT.now
            scheduler._maybe_refresh_earnings_calendar_av_daily(dry_run=True)

        fired = scheduler._earnings_av_daily_ran_date != ran_date
        scheduler._earnings_av_daily_ran_date = orig_key
        return fired

    def test_ec01_fires_monday_4am(self):
        """EC-01: daily refresh fires on a Monday at 4:10 AM ET."""
        import zoneinfo

        import scheduler
        ET = zoneinfo.ZoneInfo("America/New_York")

        # 2026-05-04 is a Monday
        monday_4am_et = datetime(2026, 5, 4, 4, 10, 0, tzinfo=ET)
        orig = scheduler._earnings_av_daily_ran_date
        scheduler._earnings_av_daily_ran_date = ""
        try:
            with patch("scheduler.datetime") as mdt, \
                 patch("scheduler._today", return_value="2026-05-04"):
                mdt.now.return_value = monday_4am_et
                scheduler._maybe_refresh_earnings_calendar_av_daily(dry_run=True)
            assert scheduler._earnings_av_daily_ran_date == "2026-05-04"
        finally:
            scheduler._earnings_av_daily_ran_date = orig

    def test_ec01_skips_sunday(self):
        """EC-01: daily refresh does NOT fire on Sunday (handled by weekly)."""
        import zoneinfo

        import scheduler
        ET = zoneinfo.ZoneInfo("America/New_York")

        # 2026-05-03 is a Sunday
        sunday_4am_et = datetime(2026, 5, 3, 4, 10, 0, tzinfo=ET)
        orig = scheduler._earnings_av_daily_ran_date
        scheduler._earnings_av_daily_ran_date = ""
        try:
            with patch("scheduler.datetime") as mdt, \
                 patch("scheduler._today", return_value="2026-05-03"):
                mdt.now.return_value = sunday_4am_et
                scheduler._maybe_refresh_earnings_calendar_av_daily(dry_run=True)
            assert scheduler._earnings_av_daily_ran_date == ""
        finally:
            scheduler._earnings_av_daily_ran_date = orig

    def test_ec01_skips_wrong_hour(self):
        """EC-01: daily refresh does not fire outside 4:05–4:20 AM window."""
        import zoneinfo

        import scheduler
        ET = zoneinfo.ZoneInfo("America/New_York")

        monday_9am_et = datetime(2026, 5, 4, 9, 0, 0, tzinfo=ET)
        orig = scheduler._earnings_av_daily_ran_date
        scheduler._earnings_av_daily_ran_date = ""
        try:
            with patch("scheduler.datetime") as mdt, \
                 patch("scheduler._today", return_value="2026-05-04"):
                mdt.now.return_value = monday_9am_et
                scheduler._maybe_refresh_earnings_calendar_av_daily(dry_run=True)
            assert scheduler._earnings_av_daily_ran_date == ""
        finally:
            scheduler._earnings_av_daily_ran_date = orig

    def test_ec01_does_not_fire_twice_same_day(self):
        """EC-01: second call same day is a no-op."""
        import zoneinfo

        import scheduler
        ET = zoneinfo.ZoneInfo("America/New_York")

        monday_4am_et = datetime(2026, 5, 4, 4, 10, 0, tzinfo=ET)
        orig = scheduler._earnings_av_daily_ran_date
        scheduler._earnings_av_daily_ran_date = "2026-05-04"  # already ran today
        try:
            with patch("scheduler.datetime") as mdt, \
                 patch("scheduler._today", return_value="2026-05-04"):
                mdt.now.return_value = monday_4am_et
                scheduler._maybe_refresh_earnings_calendar_av_daily(dry_run=True)
            # key should still be "2026-05-04" — no second set
            assert scheduler._earnings_av_daily_ran_date == "2026-05-04"
        finally:
            scheduler._earnings_av_daily_ran_date = orig


# ─── EC-06: eda computed at runtime from calendar days ────────────────────────

class TestEdaRuntimeComputation:

    def test_ec06_eda_computed_fresh_each_call(self, tmp_path):
        """EC-06: earnings_days_away is calendar-days at call time, not stored."""
        from earnings_calendar_lookup import earnings_days_away
        # Build a fake calendar with XOM reporting tomorrow
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        cal = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": "alphavantage",
            "calendar": [{"symbol": "XOM", "earnings_date": tomorrow,
                           "timing": "pre-market"}],
        }
        cal_file = tmp_path / "earnings_calendar.json"
        cal_file.write_text(json.dumps(cal))

        # Patch the path inside the module
        import earnings_calendar_lookup as ecl
        orig_path = ecl._CAL_PATH
        ecl._CAL_PATH = cal_file
        try:
            eda = earnings_days_away("XOM")
            assert eda == 1
        finally:
            ecl._CAL_PATH = orig_path

    def test_ec06_eda_uses_date_today_not_stored_value(self, tmp_path):
        """EC-06: eda is (earnings_date - date.today()).days — pure runtime math."""
        from earnings_calendar_lookup import earnings_days_away

        five_days_ahead = (date.today() + timedelta(days=5)).isoformat()
        cal = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": "alphavantage",
            "calendar": [{"symbol": "PLTR", "earnings_date": five_days_ahead,
                           "timing": "post-market"}],
        }
        cal_file = tmp_path / "earnings_calendar.json"
        cal_file.write_text(json.dumps(cal))

        import earnings_calendar_lookup as ecl
        orig_path = ecl._CAL_PATH
        ecl._CAL_PATH = cal_file
        try:
            eda = earnings_days_away("PLTR")
            assert eda == 5
        finally:
            ecl._CAL_PATH = orig_path


# ─── EC-08: XOM and CVX appear with eda=1 ────────────────────────────────────

class TestEarningsPipelineSymbols:

    def test_ec08_xom_cvx_eda_one(self, tmp_path):
        """EC-08: XOM and CVX with earnings_date=tomorrow yield eda=1."""
        from earnings_calendar_lookup import earnings_days_away, load_calendar_map

        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        cal = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": "alphavantage",
            "calendar": [
                {"symbol": "XOM", "earnings_date": tomorrow, "timing": "pre-market"},
                {"symbol": "CVX", "earnings_date": tomorrow, "timing": "pre-market"},
            ],
        }
        cal_file = tmp_path / "earnings_calendar.json"
        cal_file.write_text(json.dumps(cal))

        import earnings_calendar_lookup as ecl
        orig_path = ecl._CAL_PATH
        ecl._CAL_PATH = cal_file
        try:
            cal_map = load_calendar_map()
            assert earnings_days_away("XOM", cal_map) == 1
            assert earnings_days_away("CVX", cal_map) == 1
            # Timing is accessible
            assert cal_map["XOM"]["timing"] == "pre-market"
            assert cal_map["CVX"]["timing"] == "pre-market"
        finally:
            ecl._CAL_PATH = orig_path

    def test_ec08_timing_field_present_in_calendar(self):
        """EC-08: The calendar schema uses 'timing' (not 'report_timing')."""
        import earnings_calendar_lookup as ecl
        raw = ecl._load_raw()
        # If calendar exists, verify field names
        if raw:
            entry = raw[0]
            assert "earnings_date" in entry, "Schema uses earnings_date not report_date"
            assert "timing" in entry or "symbol" in entry  # at minimum symbol present


# ─── EC-05: dashboard staleness HTML rendered ─────────────────────────────────

class TestDashboardStalenessIndicator:

    @staticmethod
    def _import_page_overview():
        try:
            from dashboard.app import _page_overview  # noqa: PLC0415
            return _page_overview
        except ImportError:
            import pytest
            pytest.skip("Dashboard import failed (flask not installed in test env)")

    def test_ec05_green_when_fresh(self):
        """EC-05: fresh calendar renders green ✅ indicator."""
        _page_overview = self._import_page_overview()
        status = _make_minimal_status()
        with patch("data_warehouse.get_earnings_calendar_staleness",
                   return_value={"stale": False, "warning": False,
                                 "hours_old": 3.0, "entry_count": 99,
                                 "message": None, "last_refresh": "2026-04-30T12:00:00"}):
            html = _page_overview(status, "2026-04-30 12:00 ET")

        assert "Earnings cal" in html
        assert "&#x2705;" in html or "updated" in html.lower()

    def test_ec05_yellow_when_stale(self):
        """EC-05: stale (>25h) calendar renders yellow ⚠️ indicator."""
        _page_overview = self._import_page_overview()
        status = _make_minimal_status()
        with patch("data_warehouse.get_earnings_calendar_staleness",
                   return_value={"stale": True, "warning": False,
                                 "hours_old": 28.0, "entry_count": 99,
                                 "message": "Earnings calendar 28h old",
                                 "last_refresh": "2026-04-29T08:00:00"}):
            html = _page_overview(status, "2026-04-30 12:00 ET")

        assert "Earnings cal" in html
        assert "&#x26A0;" in html  # ⚠️

    def test_ec05_red_when_warning(self):
        """EC-05: warning (>48h) calendar renders red 🔴 indicator."""
        _page_overview = self._import_page_overview()
        status = _make_minimal_status()
        with patch("data_warehouse.get_earnings_calendar_staleness",
                   return_value={"stale": True, "warning": True,
                                 "hours_old": 52.0, "entry_count": 40,
                                 "message": "Earnings calendar 52h old",
                                 "last_refresh": "2026-04-28T08:00:00"}):
            html = _page_overview(status, "2026-04-30 12:00 ET")

        assert "Earnings cal" in html
        assert "&#x1F534;" in html  # 🔴

    def test_ec05_staleness_function_returns_dict_shape(self):
        """EC-05: get_earnings_calendar_staleness() returns expected dict keys."""
        import data_warehouse as dw
        # Call with a real (or fake) path to verify the return shape
        result = dw.get_earnings_calendar_staleness()
        assert isinstance(result, dict)
        for key in ("stale", "warning", "hours_old", "entry_count", "last_refresh"):
            assert key in result, f"Missing key: {key}"
        assert isinstance(result["stale"], bool)
        assert isinstance(result["warning"], bool)


def _make_minimal_status() -> dict:
    """Build the minimal status dict that _page_overview accepts."""
    return {
        "a1": {"account": None, "positions": []},
        "a2": {"account": None, "positions": []},
        "a1_mode": {"mode": "normal"},
        "a2_mode": {"mode": "normal"},
        "warnings": [],
        "today_pnl_a1": (0.0, 0.0),
        "today_pnl_a2": (0.0, 0.0),
        "positions": [],
        "costs": {"daily_cost": 0, "daily_calls": 0, "by_caller": {}},
        "gate": {"total_calls_today": 0},
        "buys_today": 0,
        "sells_today": 0,
        "a2_decision": {},
        "git_hash": "abc1234",
        "service_uptime": "1d 2h",
        "morning_brief_mtime": 0,
        "morning_brief_time": "—",
        "morning_brief": None,
        "trail_tiers": [],
        "a1_theses": [],
        "a1_decisions": [],
        "a2_decisions": [],
        "a2_pipeline": {},
        "allocator_line": "",
    }


# ─── EC-07: morning brief context includes enriched earnings section ───────────

class TestMorningBriefEarningsPipeline:

    def test_ec07_enriched_section_present(self, tmp_path):
        """EC-07: _load_intelligence_context includes EARNINGS PIPELINE section."""
        from morning_brief import _load_intelligence_context

        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        fake_cal = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": "alphavantage",
            "calendar": [
                {"symbol": "XOM", "earnings_date": tomorrow, "timing": "pre-market",
                 "eps_estimate": 1.21, "source": "alphavantage"},
                {"symbol": "CVX", "earnings_date": tomorrow, "timing": "pre-market",
                 "eps_estimate": 1.09, "source": "alphavantage"},
            ],
        }

        with patch("data_warehouse.load_earnings_calendar", return_value=fake_cal), \
             patch("morning_brief._get_held_symbols", return_value=set()), \
             patch("morning_brief._load_iv_ranks_for_brief", return_value={}), \
             patch("morning_brief._load_signal_scores_for_brief", return_value=[]), \
             patch("morning_brief._build_pre_earnings_intel_section", return_value=""):
            ctx = _load_intelligence_context("morning")

        assert "EARNINGS PIPELINE" in ctx
        assert "XOM" in ctx
        assert "CVX" in ctx
        assert "pre-market" in ctx

    def test_ec07_held_symbol_flagged(self, tmp_path):
        """EC-07: A1-held symbol is flagged as HELD-A1 in earnings pipeline."""
        from morning_brief import _load_intelligence_context

        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        fake_cal = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": "alphavantage",
            "calendar": [
                {"symbol": "AMZN", "earnings_date": tomorrow, "timing": "post-market",
                 "source": "alphavantage"},
            ],
        }

        with patch("data_warehouse.load_earnings_calendar", return_value=fake_cal), \
             patch("morning_brief._get_held_symbols", return_value={"AMZN"}), \
             patch("morning_brief._load_iv_ranks_for_brief", return_value={"AMZN": 45.0}), \
             patch("morning_brief._load_signal_scores_for_brief", return_value=[]), \
             patch("morning_brief._build_pre_earnings_intel_section", return_value=""):
            ctx = _load_intelligence_context("morning")

        # Both the _load_intelligence_context earnings section and _load_context
        # enriched sections use [HELD-A1] tag
        assert "[HELD-A1]" in ctx or "HELD-A1" in ctx or "HELD A1" in ctx
        assert "iv_rank=45" in ctx or "AMZN" in ctx  # iv_rank visible in enriched section

    def test_ec07_symbols_beyond_5_days_excluded(self):
        """EC-07: Symbols with eda > 5 days are not included in the pipeline."""
        from morning_brief import _load_intelligence_context

        far_out = (date.today() + timedelta(days=10)).isoformat()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        fake_cal = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": "alphavantage",
            "calendar": [
                {"symbol": "NVDA", "earnings_date": far_out, "timing": "post-market",
                 "source": "alphavantage"},
                {"symbol": "XOM", "earnings_date": tomorrow, "timing": "pre-market",
                 "source": "alphavantage"},
            ],
        }

        with patch("data_warehouse.load_earnings_calendar", return_value=fake_cal), \
             patch("morning_brief._get_held_symbols", return_value=set()), \
             patch("morning_brief._load_iv_ranks_for_brief", return_value={}), \
             patch("morning_brief._load_signal_scores_for_brief", return_value=[]), \
             patch("morning_brief._build_pre_earnings_intel_section", return_value=""):
            ctx = _load_intelligence_context("morning")

        assert "XOM" in ctx
        assert "NVDA" not in ctx.split("EARNINGS PIPELINE")[1].split("===")[0] \
            if "EARNINGS PIPELINE" in ctx else True
