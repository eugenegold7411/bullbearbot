"""
tests/test_s4h_bars_pruning.py — S4-H tests.

Covers:
  Build 1 — _prune_bars() deletes old intraday CSVs, spares daily CSVs,
             skips malformed filenames, and is non-fatal in run_full_refresh()
"""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

# ── Third-party stubs ─────────────────────────────────────────────────────────

_STUBS = {
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
for _stub_name in _STUBS:
    if _stub_name not in sys.modules:
        sys.modules[_stub_name] = mock.MagicMock()
if hasattr(sys.modules.get("dotenv"), "load_dotenv"):
    sys.modules["dotenv"].load_dotenv = mock.MagicMock()

import data_warehouse as dw  # noqa: E402


def _make_intraday(bars_dir: Path, symbol: str, date_str: str) -> Path:
    f = bars_dir / f"{symbol}_intraday_{date_str}.csv"
    f.write_text("date,open,close\n")
    return f


def _make_daily(bars_dir: Path, symbol: str) -> Path:
    f = bars_dir / f"{symbol}_daily.csv"
    f.write_text("date,open,close\n")
    return f


# ═══════════════════════════════════════════════════════════════════════════════
# Suite P1 — old intraday files are deleted
# ═══════════════════════════════════════════════════════════════════════════════

class TestPruneDeletesOldIntraday(unittest.TestCase):

    def test_old_intraday_deleted(self):
        """Files older than keep_days must be deleted."""
        with tempfile.TemporaryDirectory() as tmp:
            bars_dir = Path(tmp)
            old_date = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d")
            old_file = _make_intraday(bars_dir, "AAPL", old_date)
            with mock.patch.object(dw, "BARS_DIR", bars_dir):
                dw._prune_bars(keep_days=30)
            self.assertFalse(old_file.exists(), "Old intraday file should have been deleted")

    def test_recent_intraday_kept(self):
        """Files within keep_days must NOT be deleted."""
        with tempfile.TemporaryDirectory() as tmp:
            bars_dir = Path(tmp)
            recent_date = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
            recent_file = _make_intraday(bars_dir, "AAPL", recent_date)
            with mock.patch.object(dw, "BARS_DIR", bars_dir):
                dw._prune_bars(keep_days=30)
            self.assertTrue(recent_file.exists(), "Recent intraday file must not be deleted")

    def test_within_keep_days_kept(self):
        """File from keep_days - 1 days ago must NOT be deleted."""
        with tempfile.TemporaryDirectory() as tmp:
            bars_dir = Path(tmp)
            safe_date = (datetime.now() - timedelta(days=29)).strftime("%Y-%m-%d")
            safe_file = _make_intraday(bars_dir, "MSFT", safe_date)
            with mock.patch.object(dw, "BARS_DIR", bars_dir):
                dw._prune_bars(keep_days=30)
            self.assertTrue(safe_file.exists(), "File within keep_days window must not be deleted")

    def test_multiple_symbols_old_all_deleted(self):
        """Multiple old intraday files across different symbols are all pruned."""
        with tempfile.TemporaryDirectory() as tmp:
            bars_dir = Path(tmp)
            old_date = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
            files = [
                _make_intraday(bars_dir, sym, old_date)
                for sym in ["NVDA", "GLD", "TSM"]
            ]
            with mock.patch.object(dw, "BARS_DIR", bars_dir):
                dw._prune_bars(keep_days=30)
            for f in files:
                self.assertFalse(f.exists(), f"{f.name} should have been deleted")


# ═══════════════════════════════════════════════════════════════════════════════
# Suite P2 — daily CSVs are never deleted
# ═══════════════════════════════════════════════════════════════════════════════

class TestPruneNeverDeletesDaily(unittest.TestCase):

    def test_daily_csv_untouched(self):
        """Daily CSVs must never be deleted regardless of age."""
        with tempfile.TemporaryDirectory() as tmp:
            bars_dir = Path(tmp)
            daily = _make_daily(bars_dir, "AAPL")
            with mock.patch.object(dw, "BARS_DIR", bars_dir):
                dw._prune_bars(keep_days=0)  # aggressive: keep_days=0 should still spare daily
            self.assertTrue(daily.exists(), "Daily CSV must never be deleted")

    def test_mix_daily_kept_old_intraday_deleted(self):
        """In a mixed directory, daily stays and old intraday goes."""
        with tempfile.TemporaryDirectory() as tmp:
            bars_dir = Path(tmp)
            daily = _make_daily(bars_dir, "GLD")
            old_date = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
            old_intraday = _make_intraday(bars_dir, "GLD", old_date)
            recent_date = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
            recent_intraday = _make_intraday(bars_dir, "GLD", recent_date)
            with mock.patch.object(dw, "BARS_DIR", bars_dir):
                dw._prune_bars(keep_days=30)
            self.assertTrue(daily.exists(), "Daily CSV must survive pruning")
            self.assertFalse(old_intraday.exists(), "Old intraday must be deleted")
            self.assertTrue(recent_intraday.exists(), "Recent intraday must survive")


# ═══════════════════════════════════════════════════════════════════════════════
# Suite P3 — malformed filenames skipped without error
# ═══════════════════════════════════════════════════════════════════════════════

class TestPruneMalformedFilenames(unittest.TestCase):

    def test_malformed_no_date_skipped(self):
        """Files matching *_intraday_*.csv but with unparseable dates are skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            bars_dir = Path(tmp)
            bad = bars_dir / "AAPL_intraday_not-a-date.csv"
            bad.write_text("x")
            with mock.patch.object(dw, "BARS_DIR", bars_dir):
                # Must not raise
                dw._prune_bars(keep_days=30)
            self.assertTrue(bad.exists(), "Malformed file must not be deleted")

    def test_malformed_no_underscore_skipped(self):
        """Files that don't split on '_intraday_' correctly are skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            bars_dir = Path(tmp)
            weird = bars_dir / "AAPL_intraday_.csv"
            weird.write_text("x")
            with mock.patch.object(dw, "BARS_DIR", bars_dir):
                dw._prune_bars(keep_days=30)
            self.assertTrue(weird.exists(), "Edge-case filename must not crash or be deleted")

    def test_mixed_good_and_bad_filenames(self):
        """Good old file is deleted; bad filename is silently skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            bars_dir = Path(tmp)
            old_date = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d")
            good = _make_intraday(bars_dir, "SPY", old_date)
            bad = bars_dir / "SPY_intraday_BADDATE.csv"
            bad.write_text("x")
            with mock.patch.object(dw, "BARS_DIR", bars_dir):
                dw._prune_bars(keep_days=30)
            self.assertFalse(good.exists(), "Old good file must be deleted")
            self.assertTrue(bad.exists(), "Bad filename must be skipped")


# ═══════════════════════════════════════════════════════════════════════════════
# Suite P4 — non-fatal in run_full_refresh
# ═══════════════════════════════════════════════════════════════════════════════

class TestPruneNonFatal(unittest.TestCase):

    def test_prune_exception_does_not_crash_run_full_refresh(self):
        """If _prune_bars raises, run_full_refresh must still complete."""
        mock_wm = mock.MagicMock()
        mock_wm.get_active_watchlist.return_value = {"stocks": [], "etfs": []}
        with mock.patch.object(dw, "wm", mock_wm), \
             mock.patch.object(dw, "refresh_bars"), \
             mock.patch.object(dw, "refresh_economic_calendar_finnhub"), \
             mock.patch.object(dw, "refresh_fundamentals"), \
             mock.patch.object(dw, "refresh_news"), \
             mock.patch.object(dw, "refresh_sector_performance"), \
             mock.patch.object(dw, "refresh_macro_snapshot"), \
             mock.patch.object(dw, "refresh_earnings_calendar"), \
             mock.patch.object(dw, "refresh_premarket_movers"), \
             mock.patch.object(dw, "refresh_global_indices"), \
             mock.patch.object(dw, "refresh_crypto_sentiment"), \
             mock.patch.object(dw, "_prune_bars", side_effect=RuntimeError("disk full")):
            # Must not raise
            dw.run_full_refresh()

    def test_prune_called_during_run_full_refresh(self):
        """_prune_bars must be invoked once per run_full_refresh."""
        mock_wm = mock.MagicMock()
        mock_wm.get_active_watchlist.return_value = {"stocks": [], "etfs": []}
        with mock.patch.object(dw, "wm", mock_wm), \
             mock.patch.object(dw, "refresh_bars"), \
             mock.patch.object(dw, "refresh_economic_calendar_finnhub"), \
             mock.patch.object(dw, "refresh_fundamentals"), \
             mock.patch.object(dw, "refresh_news"), \
             mock.patch.object(dw, "refresh_sector_performance"), \
             mock.patch.object(dw, "refresh_macro_snapshot"), \
             mock.patch.object(dw, "refresh_earnings_calendar"), \
             mock.patch.object(dw, "refresh_premarket_movers"), \
             mock.patch.object(dw, "refresh_global_indices"), \
             mock.patch.object(dw, "refresh_crypto_sentiment"), \
             mock.patch.object(dw, "_prune_bars") as mock_prune:
            dw.run_full_refresh()
        mock_prune.assert_called_once()

    def test_keep_days_default_is_30(self):
        """_prune_bars default keep_days must be 30."""
        import inspect
        sig = inspect.signature(dw._prune_bars)
        default = sig.parameters["keep_days"].default
        self.assertEqual(default, 30, "keep_days default must be 30")


if __name__ == "__main__":
    unittest.main()
