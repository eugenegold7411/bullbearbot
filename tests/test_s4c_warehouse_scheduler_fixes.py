"""
tests/test_s4c_warehouse_scheduler_fixes.py — S4-C tests.

Covers:
  Build 1 — refresh_fundamentals skips ETF symbols (no quoteSummary 404s)
  Build 2 — VX=F removed; ^VIX present in _GLOBAL_INDICES
  Build 3 — refresh_economic_calendar_finnhub writes placeholder when no key
  Build 4 — scheduler retry guards only set date/slot on success
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
    # chromadb deliberately omitted: stubbing it with MagicMock poisons
    # trade_memory's lazy init for the rest of the pytest session and breaks
    # test_scratchpad_memory.py. trade_memory has graceful degradation if
    # chromadb is genuinely absent.
}
for _stub_name, _stub_val in _THIRD_PARTY_STUBS.items():
    if _stub_name not in sys.modules:
        _m = mock.MagicMock()
        if _stub_name == "dotenv":
            _m.load_dotenv = mock.MagicMock()
        sys.modules[_stub_name] = _m


# ═══════════════════════════════════════════════════════════════════════════════
# Build 1 — ETF fundamentals skip
# ═══════════════════════════════════════════════════════════════════════════════

class TestRefreshFundamentalsEtfSkip(unittest.TestCase):
    """refresh_fundamentals() must skip ETF symbols without calling yf.Ticker."""

    def _import_dw(self, etf_watchlist=None):
        """Import data_warehouse with mocked watchlist_manager."""
        saved = sys.modules.pop("data_warehouse", None)
        mock_wm = mock.MagicMock()
        mock_wm.get_active_watchlist.return_value = {
            "stocks": ["NVDA", "GS", "JPM"],
            "etfs": etf_watchlist if etf_watchlist is not None
                    else ["XLE", "USO", "GLD", "XLF", "XBI"],
        }
        with mock.patch.dict(sys.modules, {"watchlist_manager": mock_wm}):
            import data_warehouse as dw
        if saved is not None:
            sys.modules["data_warehouse"] = saved
        return dw, mock_wm

    def test_etf_symbols_not_passed_to_yfinance(self):
        """ETF symbols from watchlist must never reach yf.Ticker.info."""
        import data_warehouse as dw
        mock_wm = mock.MagicMock()
        mock_wm.get_active_watchlist.return_value = {
            "stocks": ["NVDA", "GS"],
            "etfs": ["XLE", "GLD", "XBI"],
        }
        called_symbols = []
        mock_ticker = mock.MagicMock()
        mock_ticker.info = {"marketCap": 1_000_000}

        with mock.patch.object(dw, "wm", mock_wm), \
             mock.patch("data_warehouse.yf") as mock_yf, \
             mock.patch("data_warehouse.FUND_DIR") as mock_dir:
            mock_dir.mkdir = mock.MagicMock()
            mock_yf.Ticker.side_effect = lambda sym: (
                called_symbols.append(sym) or mock_ticker
            )
            mock_ticker_instance = mock.MagicMock()
            mock_ticker_instance.info = {"marketCap": 1_000_000}
            mock_yf.Ticker.return_value = mock_ticker_instance

            with mock.patch("data_warehouse._save_json"):
                dw.refresh_fundamentals(["NVDA", "GS", "XLE", "GLD", "XBI"])

        for sym in ["XLE", "GLD", "XBI"]:
            self.assertNotIn(
                sym, [c.args[0] for c in mock_yf.Ticker.call_args_list],
                f"ETF {sym} should not be passed to yf.Ticker",
            )

    def test_equity_symbols_still_fetched(self):
        """Non-ETF symbols must still reach yf.Ticker."""
        import data_warehouse as dw
        mock_wm = mock.MagicMock()
        mock_wm.get_active_watchlist.return_value = {
            "stocks": [], "etfs": ["XLE", "GLD"],
        }
        mock_ticker_instance = mock.MagicMock()
        mock_ticker_instance.info = {"marketCap": 500_000_000}

        with mock.patch.object(dw, "wm", mock_wm), \
             mock.patch("data_warehouse.yf") as mock_yf, \
             mock.patch("data_warehouse.FUND_DIR") as mock_dir, \
             mock.patch("data_warehouse._save_json"):
            mock_dir.mkdir = mock.MagicMock()
            mock_yf.Ticker.return_value = mock_ticker_instance
            dw.refresh_fundamentals(["NVDA", "GS", "XLE"])

        called = [c.args[0] for c in mock_yf.Ticker.call_args_list]
        self.assertIn("NVDA", called)
        self.assertIn("GS", called)
        self.assertNotIn("XLE", called)

    def test_fallback_etf_set_used_when_watchlist_unavailable(self):
        """When watchlist_manager raises, fallback ETF set must still gate out ETFs."""
        import data_warehouse as dw
        mock_wm = mock.MagicMock()
        mock_wm.get_active_watchlist.side_effect = RuntimeError("unavailable")
        mock_ticker_instance = mock.MagicMock()
        mock_ticker_instance.info = {}

        with mock.patch.object(dw, "wm", mock_wm), \
             mock.patch("data_warehouse.yf") as mock_yf, \
             mock.patch("data_warehouse.FUND_DIR") as mock_dir, \
             mock.patch("data_warehouse._save_json"):
            mock_dir.mkdir = mock.MagicMock()
            mock_yf.Ticker.return_value = mock_ticker_instance
            dw.refresh_fundamentals(["NVDA", "XLE", "GLD"])

        called = [c.args[0] for c in mock_yf.Ticker.call_args_list]
        self.assertIn("NVDA", called)
        self.assertNotIn("XLE", called)
        self.assertNotIn("GLD", called)

    def test_all_etf_input_logs_zero_equity_symbols(self):
        """A list of only ETF symbols produces 0 equity symbols and no yf.Ticker calls."""
        import data_warehouse as dw
        mock_wm = mock.MagicMock()
        mock_wm.get_active_watchlist.return_value = {
            "stocks": [], "etfs": ["XLE", "USO", "GLD", "XLF"],
        }
        with mock.patch.object(dw, "wm", mock_wm), \
             mock.patch("data_warehouse.yf") as mock_yf, \
             mock.patch("data_warehouse.FUND_DIR") as mock_dir, \
             mock.patch("data_warehouse._save_json"):
            mock_dir.mkdir = mock.MagicMock()
            dw.refresh_fundamentals(["XLE", "USO", "GLD", "XLF"])

        mock_yf.Ticker.assert_not_called()

    def test_get_etf_symbols_returns_frozenset(self):
        """_get_etf_symbols must always return a frozenset."""
        import data_warehouse as dw
        result = dw._get_etf_symbols()
        self.assertIsInstance(result, frozenset)
        self.assertGreater(len(result), 0)

    def test_fallback_etf_set_contains_expected_symbols(self):
        """Fallback set must contain the known 404-prone ETF symbols."""
        import data_warehouse as dw
        for sym in ("XLE", "USO", "GLD", "SLV", "XLF", "XRT", "ITA", "XBI", "EWJ", "FXI"):
            self.assertIn(sym, dw._ETF_SYMBOLS_FALLBACK,
                          f"{sym} missing from _ETF_SYMBOLS_FALLBACK")


# ═══════════════════════════════════════════════════════════════════════════════
# Build 2 — VX=F removed / ^VIX present
# ═══════════════════════════════════════════════════════════════════════════════

class TestGlobalIndicesVixFix(unittest.TestCase):
    """_GLOBAL_INDICES must use ^VIX (CBOE) not VX=F (delisted futures)."""

    def test_vxf_not_in_global_indices_values(self):
        import data_warehouse as dw
        self.assertNotIn("VX=F", dw._GLOBAL_INDICES.values(),
                         "VX=F is delisted — must not appear in _GLOBAL_INDICES")

    def test_vix_present_in_global_indices_values(self):
        import data_warehouse as dw
        self.assertIn("^VIX", dw._GLOBAL_INDICES.values(),
                      "^VIX must be present in _GLOBAL_INDICES")

    def test_global_indices_key_for_vix(self):
        """The key for ^VIX should be labelled 'VIX' not 'VIX Fut'."""
        import data_warehouse as dw
        ticker_to_name = {v: k for k, v in dw._GLOBAL_INDICES.items()}
        self.assertEqual(ticker_to_name.get("^VIX"), "VIX")

    def test_global_indices_still_has_13_entries(self):
        """Replacing VX=F with ^VIX should not change the total count."""
        import data_warehouse as dw
        self.assertEqual(len(dw._GLOBAL_INDICES), 13)

    def test_refresh_global_indices_skips_vxf(self):
        """refresh_global_indices must not attempt to fetch VX=F."""
        import data_warehouse as dw
        tickers_fetched = []
        mock_history = mock.MagicMock()
        mock_history.empty = False
        mock_history.__len__ = mock.MagicMock(return_value=2)
        mock_history.__getitem__ = mock.MagicMock(return_value=mock.MagicMock(
            iloc=mock.MagicMock(__getitem__=mock.MagicMock(return_value=100.0))
        ))

        def _mock_ticker(sym):
            tickers_fetched.append(sym)
            t = mock.MagicMock()
            t.history.return_value = mock_history
            return t

        with mock.patch("data_warehouse.yf") as mock_yf, \
             mock.patch("data_warehouse._save_json"), \
             mock.patch("data_warehouse._archive"), \
             mock.patch("data_warehouse.MARKET_DIR") as mock_mdir:
            mock_mdir.mkdir = mock.MagicMock()
            mock_yf.Ticker.side_effect = _mock_ticker
            dw.refresh_global_indices()

        self.assertNotIn("VX=F", tickers_fetched)
        self.assertIn("^VIX", tickers_fetched)


# ═══════════════════════════════════════════════════════════════════════════════
# Build 3 — Economic calendar no-key placeholder
# ═══════════════════════════════════════════════════════════════════════════════

class TestEconomicCalendarNoKey(unittest.TestCase):
    """Without FINNHUB_API_KEY, refresh_economic_calendar_finnhub writes a placeholder."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def _run_no_key(self):
        import data_warehouse as dw
        mock_market_dir = Path(self._tmpdir)
        with mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch.object(dw, "MARKET_DIR", mock_market_dir), \
             mock.patch.dict(os.environ, {"FINNHUB_API_KEY": ""}):
            # Ensure key is absent
            env_copy = {k: v for k, v in os.environ.items() if k != "FINNHUB_API_KEY"}
            with mock.patch.dict(os.environ, env_copy, clear=True):
                dw.refresh_economic_calendar_finnhub()

    def test_writes_file_when_no_key(self):
        """A placeholder file must be written even when FINNHUB_API_KEY is absent."""
        import data_warehouse as dw
        written_data = {}

        def _capture_save(path, data):
            written_data["path"] = str(path)
            written_data["data"] = data

        with mock.patch("data_warehouse.os.getenv", return_value=None), \
             mock.patch("data_warehouse._save_json", side_effect=_capture_save), \
             mock.patch("data_warehouse.MARKET_DIR") as mock_mdir:
            mock_mdir.__truediv__ = lambda self, other: Path(self._tmpdir) / other
            mock_mdir.mkdir = mock.MagicMock()
            dw.refresh_economic_calendar_finnhub()

        self.assertIn("data", written_data, "No file written when FINNHUB_API_KEY absent")

    def test_placeholder_has_empty_events(self):
        """Placeholder must have events=[] so build_economic_calendar_section degrades cleanly."""
        import data_warehouse as dw
        written_data = {}

        def _capture_save(path, data):
            written_data["data"] = data

        with mock.patch("data_warehouse.os.getenv", return_value=None), \
             mock.patch("data_warehouse._save_json", side_effect=_capture_save), \
             mock.patch("data_warehouse.MARKET_DIR") as mock_mdir:
            mock_mdir.__truediv__ = lambda self, other: Path(self._tmpdir) / other
            mock_mdir.mkdir = mock.MagicMock()
            dw.refresh_economic_calendar_finnhub()

        self.assertEqual(written_data["data"]["events"], [])
        self.assertIsNone(written_data["data"]["next_high_impact"])

    def test_placeholder_has_fetched_at(self):
        """Placeholder must have a current fetched_at so cache age checks pass."""
        import data_warehouse as dw
        written_data = {}

        def _capture_save(path, data):
            written_data["data"] = data

        with mock.patch("data_warehouse.os.getenv", return_value=None), \
             mock.patch("data_warehouse._save_json", side_effect=_capture_save), \
             mock.patch("data_warehouse.MARKET_DIR") as mock_mdir:
            mock_mdir.__truediv__ = lambda self, other: Path(self._tmpdir) / other
            mock_mdir.mkdir = mock.MagicMock()
            dw.refresh_economic_calendar_finnhub()

        self.assertIn("fetched_at", written_data["data"])
        self.assertIn("_source", written_data["data"])
        self.assertEqual(written_data["data"]["_source"], "no_key_placeholder")

    def test_no_http_call_when_no_key(self):
        """No HTTP request should be made when FINNHUB_API_KEY is absent."""
        import data_warehouse as dw

        with mock.patch("data_warehouse.os.getenv", return_value=None), \
             mock.patch("data_warehouse._save_json"), \
             mock.patch("data_warehouse.MARKET_DIR") as mock_mdir:
            mock_mdir.__truediv__ = lambda self, other: Path(self._tmpdir) / other
            mock_mdir.mkdir = mock.MagicMock()
            # requests would raise if called — patching to detect
            with mock.patch.dict(sys.modules, {"requests": mock.MagicMock()}) as _:
                import requests as mock_requests
                dw.refresh_economic_calendar_finnhub()
                mock_requests.get.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# Build 4 — Scheduler retry guards
# ═══════════════════════════════════════════════════════════════════════════════

import types as _types

_MISSING = object()


def _import_fresh_scheduler() -> "scheduler":  # noqa: F821
    """Force a clean import of the real scheduler module with minimal stubs.

    Follows the same pattern as test_t012_t013_t020._import_scheduler():
    pop any stub from sys.modules, inject bot/report/weekly_review, import,
    then restore original sys.modules state for those keys.
    """
    _bot_stub = _types.ModuleType("bot")
    _bot_stub.run_cycle = lambda *a, **kw: None
    _wr_stub = _types.ModuleType("weekly_review")
    _wr_stub.run_review = lambda *a, **kw: ""
    _rpt_stub = _types.ModuleType("report")
    _rpt_stub.send_report_email = mock.MagicMock()
    _rpt_stub.send_alert_email  = mock.MagicMock()
    _rpt_stub._get_account      = lambda: None
    _rpt_stub._get_positions    = lambda: []

    _scoped = {
        "bot":           _bot_stub,
        "weekly_review": _wr_stub,
        "report":        _rpt_stub,
    }
    _saved = {k: sys.modules.get(k, _MISSING) for k in _scoped}

    sys.modules.pop("scheduler", None)
    for k, v in _scoped.items():
        sys.modules[k] = v
    try:
        import scheduler as sched
    finally:
        for k, saved in _saved.items():
            if saved is _MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = saved

    return sched


def _make_now_et(weekday: int, hour: int, minute: int, date_str: str):
    """Build a mock datetime that behaves like datetime.now(ET)."""
    now = mock.MagicMock()
    now.weekday.return_value = weekday
    now.hour   = hour
    now.minute = minute
    now.strftime.return_value = date_str
    return now


class TestSchedulerRetryGuards(unittest.TestCase):
    """Scheduler guards must only lock the date/slot when the underlying job succeeds."""

    # ── _maybe_run_premarket_jobs ─────────────────────────────────────────────

    def test_premarket_date_not_set_on_warehouse_failure(self):
        """_maybe_run_premarket_jobs must NOT set _premarket_ran_date if warehouse raises."""
        sched = _import_fresh_scheduler()
        sched._premarket_ran_date = None

        mock_dw = mock.MagicMock()
        mock_dw.run_full_refresh.side_effect = RuntimeError("yfinance 429")
        mock_scanner = mock.MagicMock()

        now_et = _make_now_et(weekday=1, hour=4, minute=10, date_str="2026-04-22")

        with mock.patch("scheduler.datetime") as mock_dt, \
             mock.patch.dict(sys.modules, {"data_warehouse": mock_dw, "scanner": mock_scanner}):
            mock_dt.now.return_value = now_et
            sched._maybe_run_premarket_jobs(dry_run=False)

        self.assertIsNone(
            sched._premarket_ran_date,
            "_premarket_ran_date must remain None after warehouse failure",
        )

    def test_premarket_date_set_on_warehouse_success(self):
        """_maybe_run_premarket_jobs must set _premarket_ran_date on warehouse success."""
        sched = _import_fresh_scheduler()
        sched._premarket_ran_date = None

        mock_dw = mock.MagicMock()
        mock_dw.run_full_refresh.return_value = None
        mock_scanner = mock.MagicMock()

        now_et = _make_now_et(weekday=1, hour=4, minute=10, date_str="2026-04-22")

        with mock.patch("scheduler.datetime") as mock_dt, \
             mock.patch.dict(sys.modules, {"data_warehouse": mock_dw, "scanner": mock_scanner}):
            mock_dt.now.return_value = now_et
            sched._maybe_run_premarket_jobs(dry_run=False)

        self.assertEqual(sched._premarket_ran_date, "2026-04-22")

    def test_premarket_date_set_on_dry_run(self):
        """dry_run must set _premarket_ran_date (no actual work to fail)."""
        sched = _import_fresh_scheduler()
        sched._premarket_ran_date = None

        now_et = _make_now_et(weekday=1, hour=4, minute=10, date_str="2026-04-22")

        with mock.patch("scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = now_et
            sched._maybe_run_premarket_jobs(dry_run=True)

        self.assertEqual(sched._premarket_ran_date, "2026-04-22")

    # ── _maybe_refresh_global_indices ─────────────────────────────────────────

    def test_global_indices_slot_not_set_on_failure(self):
        """_maybe_refresh_global_indices must not lock slot if refresh raises."""
        sched = _import_fresh_scheduler()
        sched._global_indices_refresh_key = None

        mock_dw = mock.MagicMock()
        mock_dw.refresh_global_indices.side_effect = RuntimeError("HTTP 404")

        now_et = _make_now_et(weekday=1, hour=4, minute=20, date_str="2026-04-22-04")

        with mock.patch("scheduler.datetime") as mock_dt, \
             mock.patch.dict(sys.modules, {"data_warehouse": mock_dw}):
            mock_dt.now.return_value = now_et
            sched._maybe_refresh_global_indices(dry_run=False)

        self.assertIsNone(
            sched._global_indices_refresh_key,
            "_global_indices_refresh_key must remain None after failure",
        )

    def test_global_indices_slot_set_on_success(self):
        """_maybe_refresh_global_indices must lock slot after successful refresh."""
        sched = _import_fresh_scheduler()
        sched._global_indices_refresh_key = None

        mock_dw = mock.MagicMock()
        mock_dw.refresh_global_indices.return_value = None

        now_et = _make_now_et(weekday=1, hour=4, minute=20, date_str="2026-04-22-04")

        with mock.patch("scheduler.datetime") as mock_dt, \
             mock.patch.dict(sys.modules, {"data_warehouse": mock_dw}):
            mock_dt.now.return_value = now_et
            sched._maybe_refresh_global_indices(dry_run=False)

        self.assertEqual(sched._global_indices_refresh_key, "2026-04-22-04")

    # ── _maybe_refresh_economic_calendar ─────────────────────────────────────

    def test_econ_slot_not_set_on_failure(self):
        """_maybe_refresh_economic_calendar must not lock slot if refresh raises."""
        sched = _import_fresh_scheduler()
        sched._econ_calendar_refresh_key = None

        mock_dw = mock.MagicMock()
        mock_dw.MARKET_DIR = Path(tempfile.mkdtemp())
        mock_dw.refresh_economic_calendar_finnhub.side_effect = RuntimeError("boom")

        now_et = _make_now_et(weekday=1, hour=8, minute=37, date_str="2026-04-22")

        with mock.patch("scheduler.datetime") as mock_dt, \
             mock.patch.dict(sys.modules, {"data_warehouse": mock_dw}):
            mock_dt.now.return_value = now_et
            sched._maybe_refresh_economic_calendar(dry_run=False)

        self.assertIsNone(
            sched._econ_calendar_refresh_key,
            "_econ_calendar_refresh_key must remain None after failure",
        )

    def test_econ_slot_set_on_success(self):
        """_maybe_refresh_economic_calendar must lock slot after successful refresh."""
        sched = _import_fresh_scheduler()
        sched._econ_calendar_refresh_key = None

        mock_dw = mock.MagicMock()
        tmp_cal = Path(tempfile.mkdtemp()) / "economic_calendar.json"
        tmp_cal.write_text(json.dumps({"events": [], "next_high_impact": None}))
        mock_dw.MARKET_DIR = tmp_cal.parent
        mock_dw.refresh_economic_calendar_finnhub.return_value = None

        now_et = _make_now_et(weekday=1, hour=8, minute=37, date_str="2026-04-22")

        with mock.patch("scheduler.datetime") as mock_dt, \
             mock.patch.dict(sys.modules, {"data_warehouse": mock_dw}):
            mock_dt.now.return_value = now_et
            sched._maybe_refresh_economic_calendar(dry_run=False)

        self.assertEqual(sched._econ_calendar_refresh_key, "2026-04-22-0835")

    def test_econ_slot_set_on_dry_run(self):
        """dry_run must lock slot (nothing to fail)."""
        sched = _import_fresh_scheduler()
        sched._econ_calendar_refresh_key = None

        now_et = _make_now_et(weekday=1, hour=8, minute=37, date_str="2026-04-22")

        with mock.patch("scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = now_et
            sched._maybe_refresh_economic_calendar(dry_run=True)

        self.assertEqual(sched._econ_calendar_refresh_key, "2026-04-22-0835")


if __name__ == "__main__":
    unittest.main()
