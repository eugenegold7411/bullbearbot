"""
tests/test_s8_phase_c.py — Sprint 8 Phase C verification.

Fix A1: _EXTRA_TRACKED_UNIVERSE expanded with consumer blue-chips.
Fix A2: load_earnings_calendar() merges earnings_overrides.json.
Fix B:  expand_watchlist_for_upcoming_earnings() short-horizon expansion.
Fix C:  build_insider_intelligence_section() surfaces [SIGNALS OUTSIDE WATCHLIST].
"""
from __future__ import annotations

import json
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

# ─────────────────────────────────────────────────────────────────────────────
# Fix A1 — _EXTRA_TRACKED_UNIVERSE consumer blue-chips
# ─────────────────────────────────────────────────────────────────────────────

class TestExtraTrackedUniverseA1(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import data_warehouse as dw
        cls.universe = dw._EXTRA_TRACKED_UNIVERSE

    def test_sbux_present(self):
        self.assertIn("SBUX", self.universe)

    def test_cost_present(self):
        self.assertIn("COST", self.universe)

    def test_dis_present(self):
        self.assertIn("DIS", self.universe)

    def test_mcd_present(self):
        self.assertIn("MCD", self.universe)

    def test_hd_present(self):
        self.assertIn("HD", self.universe)

    def test_ko_present(self):
        self.assertIn("KO", self.universe)

    def test_nke_present(self):
        self.assertIn("NKE", self.universe)

    def test_all_consumer_additions(self):
        expected = {
            "SBUX", "COST", "DIS", "MCD", "HD", "LOW", "TGT", "NKE",
            "PG", "KO", "PEP", "T", "VZ", "CVS", "MDT", "ABT",
            "NEE", "DUK", "SO", "D", "AMT", "PLD", "EQIX", "PSA", "O",
        }
        missing = expected - self.universe
        self.assertEqual(missing, set(), f"Missing from _EXTRA_TRACKED_UNIVERSE: {missing}")

    def test_prior_names_still_present(self):
        # Regression: original names must not be removed
        for sym in ("AAPL", "META", "GOOGL", "TSLA", "V", "MA", "BAC", "QCOM"):
            self.assertIn(sym, self.universe)


# ─────────────────────────────────────────────────────────────────────────────
# Fix A2 — load_earnings_calendar() merges overrides
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadEarningsCalendarOverrides(unittest.TestCase):

    def _write_cal(self, tmp_dir: Path, entries: list) -> Path:
        cal_path = tmp_dir / "earnings_calendar.json"
        cal_path.write_text(json.dumps({"source": "alphavantage", "calendar": entries}))
        return cal_path

    def _write_overrides(self, tmp_dir: Path, overrides: list) -> Path:
        ov_path = tmp_dir / "earnings_overrides.json"
        ov_path.write_text(json.dumps(overrides))
        return ov_path

    def test_no_overrides_file_returns_base(self, tmp_path=None):
        import tempfile

        import data_warehouse as dw
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._write_cal(tmp, [{"symbol": "AAPL", "earnings_date": "2026-05-01"}])
            with patch.object(dw, "MARKET_DIR", tmp):
                result = dw.load_earnings_calendar()
            syms = {e["symbol"] for e in result.get("calendar", [])}
            self.assertIn("AAPL", syms)

    def test_override_replaces_av_entry(self):
        import tempfile

        import data_warehouse as dw
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._write_cal(tmp, [
                {"symbol": "SBUX", "earnings_date": "2099-01-01", "timing": "unknown"},
                {"symbol": "AAPL", "earnings_date": "2026-05-01"},
            ])
            self._write_overrides(tmp, [
                {"symbol": "SBUX", "earnings_date": "2026-04-29",
                 "timing": "after-hours", "source": "manual"},
            ])
            with patch.object(dw, "MARKET_DIR", tmp):
                result = dw.load_earnings_calendar()
            cal = result.get("calendar", [])
            sbux = next(e for e in cal if e["symbol"] == "SBUX")
            self.assertEqual(sbux["earnings_date"], "2026-04-29")
            self.assertEqual(sbux["timing"], "after-hours")
            self.assertEqual(sbux["source"], "manual")
            # AAPL must survive
            syms = {e["symbol"] for e in cal}
            self.assertIn("AAPL", syms)

    def test_override_adds_new_symbol(self):
        import tempfile

        import data_warehouse as dw
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._write_cal(tmp, [{"symbol": "AAPL", "earnings_date": "2026-05-01"}])
            self._write_overrides(tmp, [
                {"symbol": "SBUX", "earnings_date": "2026-04-29", "source": "manual"},
            ])
            with patch.object(dw, "MARKET_DIR", tmp):
                result = dw.load_earnings_calendar()
            syms = {e["symbol"] for e in result.get("calendar", [])}
            self.assertIn("SBUX", syms)
            self.assertIn("AAPL", syms)

    def test_malformed_overrides_file_non_fatal(self):
        import tempfile

        import data_warehouse as dw
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._write_cal(tmp, [{"symbol": "AAPL", "earnings_date": "2026-05-01"}])
            (tmp / "earnings_overrides.json").write_text("NOT JSON {{{{")
            with patch.object(dw, "MARKET_DIR", tmp):
                result = dw.load_earnings_calendar()
            # Should still return base calendar
            syms = {e["symbol"] for e in result.get("calendar", [])}
            self.assertIn("AAPL", syms)

    def test_empty_overrides_list_is_no_op(self):
        import tempfile

        import data_warehouse as dw
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._write_cal(tmp, [{"symbol": "AAPL", "earnings_date": "2026-05-01"}])
            self._write_overrides(tmp, [])
            with patch.object(dw, "MARKET_DIR", tmp):
                result = dw.load_earnings_calendar()
            syms = {e["symbol"] for e in result.get("calendar", [])}
            self.assertIn("AAPL", syms)

    def test_no_calendar_file_returns_overrides_only(self):
        import tempfile

        import data_warehouse as dw
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            # No earnings_calendar.json
            self._write_overrides(tmp, [
                {"symbol": "SBUX", "earnings_date": "2026-04-29", "source": "manual"},
            ])
            with patch.object(dw, "MARKET_DIR", tmp):
                result = dw.load_earnings_calendar()
            syms = {e["symbol"] for e in result.get("calendar", [])}
            self.assertIn("SBUX", syms)


# ─────────────────────────────────────────────────────────────────────────────
# Fix B — expand_watchlist_for_upcoming_earnings()
# ─────────────────────────────────────────────────────────────────────────────

def _make_cal_with_entry(symbol: str, days_from_now: int) -> dict:
    ed = (date.today() + timedelta(days=days_from_now)).isoformat()
    return {"source": "alphavantage", "calendar": [
        {"symbol": symbol, "earnings_date": ed, "timing": "post-market"},
    ]}


class TestExpandWatchlistUpcomingEarnings(unittest.TestCase):

    def setUp(self):
        import earnings_rotation as er
        self._er = er

    def _run(self, cal: dict, days_ahead: int = 5, in_watchlist: set | None = None):
        in_wl = in_watchlist or set()
        mock_core = [{"symbol": s} for s in in_wl]
        # load_earnings_calendar is a local import inside the function, so patch
        # at the data_warehouse module level so the local import picks up the mock.
        with patch.object(self._er, "add_rotation_symbol", return_value=True) as mock_add, \
             patch.object(self._er, "get_rotation", return_value=[]), \
             patch.object(self._er, "get_core", return_value=mock_core), \
             patch.object(self._er, "_infer_sector", return_value="consumer"), \
             patch("data_warehouse.load_earnings_calendar", return_value=cal):
            result = self._er.expand_watchlist_for_upcoming_earnings(days_ahead=days_ahead)
        return result, mock_add

    def test_adds_extra_universe_symbol_within_window(self):
        cal = _make_cal_with_entry("SBUX", days_from_now=3)
        added, mock_add = self._run(cal, days_ahead=5)
        self.assertIn("SBUX", added)
        mock_add.assert_called_once()

    def test_skips_symbol_outside_days_window(self):
        cal = _make_cal_with_entry("SBUX", days_from_now=10)
        added, _ = self._run(cal, days_ahead=5)
        self.assertNotIn("SBUX", added)

    def test_skips_symbol_already_in_watchlist(self):
        cal = _make_cal_with_entry("SBUX", days_from_now=2)
        added, mock_add = self._run(cal, days_ahead=5, in_watchlist={"SBUX"})
        self.assertEqual(added, [])
        mock_add.assert_not_called()

    def test_skips_symbol_not_in_extra_universe(self):
        # NOUNKNOWN is not a real extra-universe symbol
        cal = _make_cal_with_entry("NOUNKNOWN", days_from_now=1)
        added, _ = self._run(cal, days_ahead=5)
        self.assertNotIn("NOUNKNOWN", added)

    def test_cull_after_set_to_earnings_plus_2(self):
        cal = _make_cal_with_entry("SBUX", days_from_now=2)
        ed = (date.today() + timedelta(days=2))
        expected_cull = (ed + timedelta(days=2)).isoformat()
        _, mock_add = self._run(cal, days_ahead=5)
        call_kwargs = mock_add.call_args
        self.assertEqual(call_kwargs.kwargs.get("cull_after"), expected_cull)

    def test_empty_calendar_returns_empty(self):
        added, _ = self._run({"calendar": []}, days_ahead=5)
        self.assertEqual(added, [])

    def test_non_fatal_on_calendar_load_failure(self):
        with patch("data_warehouse.load_earnings_calendar", side_effect=RuntimeError("fail")):
            result = self._er.expand_watchlist_for_upcoming_earnings()
        self.assertEqual(result, [])

    def test_zero_days_ahead_skips_all(self):
        cal = _make_cal_with_entry("SBUX", days_from_now=0)
        # days_from_now=0 → today; today <= ed <= today is still valid; edge check
        added, _ = self._run(cal, days_ahead=0)
        # today == ed and today <= ed <= today+0, so should be added
        self.assertIn("SBUX", added)

    def test_source_tag_is_earnings_expand(self):
        cal = _make_cal_with_entry("COST", days_from_now=1)
        _, mock_add = self._run(cal, days_ahead=5)
        call_kwargs = mock_add.call_args
        self.assertEqual(call_kwargs.kwargs.get("source"), "earnings_expand")


# ─────────────────────────────────────────────────────────────────────────────
# Fix C — [SIGNALS OUTSIDE WATCHLIST] section
# ─────────────────────────────────────────────────────────────────────────────

def _make_congress_file(tmp_dir: Path, trades: list) -> Path:
    path = tmp_dir / "insider" / "congressional_trades.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "fetched_at": "2026-04-28T12:00:00+00:00",
        "trades": trades,
    }))
    return path


def _make_trade(ticker, action, days_since=5):
    return {
        "ticker":          ticker,
        "action":          action,
        "politician":      "Rep. Test",
        "amount_range":    "$15K–$50K",
        "filing_date":     "2026-04-20",
        "days_since_trade": days_since,
    }


class TestBuildOutsideWatchlistSection(unittest.TestCase):

    def setUp(self):
        import insider_intelligence as ii
        self._ii = ii

    def _run(self, trades: list, watchlist_syms: set, tmp_dir: Path):
        congress_path = _make_congress_file(tmp_dir, trades)
        with patch.object(self._ii, "_CONGRESS_FILE", congress_path):
            return self._ii._build_outside_watchlist_section(watchlist_syms)

    def test_buy_outside_watchlist_surfaced(self, tmp_path=None):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            trades = [_make_trade("SBUX", "buy", days_since=10)]
            lines = self._run(trades, {"AAPL", "MSFT"}, tmp)
            self.assertTrue(any("SBUX" in l for l in lines))
            self.assertTrue(any("BUY" in l for l in lines))

    def test_sell_within_30_days_surfaced(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            trades = [_make_trade("SBUX", "sell", days_since=20)]
            lines = self._run(trades, {"AAPL"}, tmp)
            self.assertTrue(any("SBUX" in l for l in lines))

    def test_sell_older_than_30_days_excluded(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            trades = [_make_trade("SBUX", "sell", days_since=45)]
            lines = self._run(trades, {"AAPL"}, tmp)
            self.assertFalse(any("SBUX" in l for l in lines))

    def test_watchlist_symbol_excluded(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            trades = [_make_trade("AAPL", "buy", days_since=5)]
            lines = self._run(trades, {"AAPL"}, tmp)
            self.assertFalse(any("AAPL" in l for l in lines))

    def test_max_10_symbols_cap(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            trades = [_make_trade(f"SYM{i}", "buy", days_since=i+1) for i in range(20)]
            lines = self._run(trades, set(), tmp)
            self.assertLessEqual(len(lines), 10)

    def test_empty_cache_returns_empty(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            lines = self._run([], {"AAPL"}, tmp)
            self.assertEqual(lines, [])

    def test_missing_cache_returns_empty(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "insider" / "congressional_trades.json"
            with patch.object(self._ii, "_CONGRESS_FILE", missing):
                lines = self._ii._build_outside_watchlist_section({"AAPL"})
            self.assertEqual(lines, [])

    def test_buy_any_age_included(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            trades = [_make_trade("DIS", "buy", days_since=90)]
            lines = self._run(trades, set(), tmp)
            self.assertTrue(any("DIS" in l for l in lines))


class TestBuildInsiderSectionOutsideWatchlistIntegration(unittest.TestCase):
    """build_insider_intelligence_section() emits [SIGNALS OUTSIDE WATCHLIST] when relevant."""

    def setUp(self):
        import insider_intelligence as ii
        self._ii = ii

    def test_section_header_present_when_outside_trades_exist(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            trades = [_make_trade("SBUX", "buy", days_since=5)]
            congress_path = _make_congress_file(tmp, trades)
            # Patch so watchlist query returns nothing, outside trades fire
            with patch.object(self._ii, "fetch_congressional_trades", return_value=[]), \
                 patch.object(self._ii, "fetch_form4_insider_trades", return_value=[]), \
                 patch.object(self._ii, "_CONGRESS_FILE", congress_path):
                out = self._ii.build_insider_intelligence_section(["AAPL", "MSFT"])
            self.assertIn("[SIGNALS OUTSIDE WATCHLIST]", out)
            self.assertIn("SBUX", out)

    def test_no_header_when_all_trades_on_watchlist(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            trades = [_make_trade("AAPL", "buy", days_since=5)]
            congress_path = _make_congress_file(tmp, trades)
            with patch.object(self._ii, "fetch_congressional_trades", return_value=[]), \
                 patch.object(self._ii, "fetch_form4_insider_trades", return_value=[]), \
                 patch.object(self._ii, "_CONGRESS_FILE", congress_path):
                out = self._ii.build_insider_intelligence_section(["AAPL"])
            self.assertNotIn("[SIGNALS OUTSIDE WATCHLIST]", out)


if __name__ == "__main__":
    unittest.main()
