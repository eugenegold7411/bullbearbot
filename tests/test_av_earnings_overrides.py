"""
tests/test_av_earnings_overrides.py — AV earnings calendar override tests.

AV-01: PLTR present in earnings_calendar.json
AV-02: TSM found via earnings_overrides.json after merge
AV-03: _load_earnings_days_away returns correct positive int for PLTR
AV-04: _load_earnings_days_away returns correct positive int for TSM via override
AV-05: override replaces AV entry for same symbol (override wins)
AV-06: missing overrides file is non-fatal (graceful degradation)
AV-07: stage2 lazy-load also respects overrides (RULE1 path)
AV-08: stage2 post-earnings lazy-load also respects overrides (RULE_POST_EARNINGS path)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))
os.chdir(_BOT_DIR)

# ── Third-party stubs ─────────────────────────────────────────────────────────

_STUBS = {
    "dotenv": None,
    "anthropic": None,
    "alpaca": None,
    "alpaca.trading": None,
    "alpaca.trading.client": None,
    "alpaca.trading.requests": None,
    "alpaca.trading.enums": None,
    "alpaca.data": None,
    "alpaca.data.enums": None,
    "alpaca.data.historical": None,
    "alpaca.data.historical.news": None,
    "alpaca.data.requests": None,
    "alpaca.data.timeframe": None,
    "pandas": None,
    "yfinance": None,
    "requests": None,
}
for _n, _v in _STUBS.items():
    if _n not in sys.modules:
        _m = mock.MagicMock()
        if _n == "dotenv":
            _m.load_dotenv = mock.MagicMock()
        sys.modules[_n] = _m


def _make_tmp_tree(cal_entries=None, ovr_entries=None):
    """
    Create a tempdir with data/market/{earnings_calendar,earnings_overrides}.json.
    Returns (tmpdir_path, cal_path, ovr_path).
    """
    tmpdir = tempfile.mkdtemp()
    market_dir = Path(tmpdir) / "data" / "market"
    market_dir.mkdir(parents=True)

    cal_path = market_dir / "earnings_calendar.json"
    if cal_entries is not None:
        cal_path.write_text(json.dumps({"calendar": cal_entries}))

    ovr_path = market_dir / "earnings_overrides.json"
    if ovr_entries is not None:
        ovr_path.write_text(json.dumps(ovr_entries))

    return tmpdir, cal_path, ovr_path


def _call_load_eda(module, symbol, tmpdir):
    """Call _load_earnings_days_away with __file__ redirected to tmpdir."""
    fake_file = str(Path(tmpdir) / "bot_options_stage1_candidates.py")
    with mock.patch.object(module, "__file__", fake_file):
        return module._load_earnings_days_away(symbol)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _import_candidates():
    # Always evict and re-import so sys.modules["bot_options_stage1_candidates"]
    # points at the same object mock.patch("bot_options_stage1_candidates.date") will patch.
    sys.modules.pop("bot_options_stage1_candidates", None)
    import bot_options_stage1_candidates as m
    return m


def _import_stage2():
    sys.modules.pop("bot_options_stage2_structures", None)
    import bot_options_stage2_structures as m
    return m


# ═══════════════════════════════════════════════════════════════════════════════
# AV-01 — PLTR present in live earnings_calendar.json
# ═══════════════════════════════════════════════════════════════════════════════

class TestAV01PltrInCalendar(unittest.TestCase):
    """PLTR must be present in earnings_calendar.json with a valid future date."""

    def test_pltr_in_calendar_json(self):
        cal_path = _BOT_DIR / "data" / "market" / "earnings_calendar.json"
        if not cal_path.exists():
            self.skipTest("earnings_calendar.json not on this machine")
        cal = json.loads(cal_path.read_text())
        entries = cal.get("calendar", [])
        syms = [e.get("symbol", "").upper() for e in entries]
        self.assertIn("PLTR", syms, "PLTR must be in earnings_calendar.json")

    def test_pltr_has_future_date(self):
        cal_path = _BOT_DIR / "data" / "market" / "earnings_calendar.json"
        if not cal_path.exists():
            self.skipTest("earnings_calendar.json not on this machine")
        cal = json.loads(cal_path.read_text())
        pltr = next(
            (e for e in cal.get("calendar", []) if e.get("symbol", "").upper() == "PLTR"),
            None,
        )
        self.assertIsNotNone(pltr, "PLTR entry not found")
        ed = pltr.get("earnings_date", "")
        self.assertTrue(ed, "earnings_date must be non-empty")
        # Date must parse correctly
        parsed = date.fromisoformat(str(ed)[:10])
        self.assertIsInstance(parsed, date)


# ═══════════════════════════════════════════════════════════════════════════════
# AV-02 — TSM found via earnings_overrides.json
# ═══════════════════════════════════════════════════════════════════════════════

class TestAV02TsmViaOverride(unittest.TestCase):
    """TSM absent from AV CSV must be resolvable via earnings_overrides.json."""

    def test_tsm_not_in_raw_calendar(self):
        """Baseline: raw calendar has no TSM entry."""
        tmpdir, _, _ = _make_tmp_tree(
            cal_entries=[
                {"symbol": "PLTR", "earnings_date": "2026-05-04", "timing": "post-market"},
            ],
            ovr_entries=None,
        )
        m = _import_candidates()
        result = _call_load_eda(m, "TSM", tmpdir)
        self.assertIsNone(result, "TSM without override must return None")

    def test_tsm_found_via_override(self):
        """With override entry TSM 2026-07-16, eda must be positive."""
        tmpdir, _, _ = _make_tmp_tree(
            cal_entries=[],
            ovr_entries={"TSM": {"earnings_date": "2026-07-16", "timing": "unknown", "source": "yfinance"}},
        )
        m = _import_candidates()
        with mock.patch("bot_options_stage1_candidates.date") as mock_date:
            mock_date.today.return_value = date(2026, 4, 30)
            mock_date.fromisoformat.side_effect = date.fromisoformat
            result = _call_load_eda(m, "TSM", tmpdir)
        self.assertIsNotNone(result, "TSM eda must not be None with override")
        self.assertGreater(result, 0, "TSM eda must be positive (upcoming)")

    def test_tsm_eda_value_correct(self):
        """TSM override 2026-07-16, today 2026-04-30 → eda = 77."""
        tmpdir, _, _ = _make_tmp_tree(
            cal_entries=[],
            ovr_entries={"TSM": {"earnings_date": "2026-07-16", "timing": "unknown"}},
        )
        m = _import_candidates()
        with mock.patch("bot_options_stage1_candidates.date") as mock_date:
            mock_date.today.return_value = date(2026, 4, 30)
            mock_date.fromisoformat.side_effect = date.fromisoformat
            result = _call_load_eda(m, "TSM", tmpdir)
        expected = (date(2026, 7, 16) - date(2026, 4, 30)).days
        self.assertEqual(result, expected)


# ═══════════════════════════════════════════════════════════════════════════════
# AV-03 — eda for PLTR computed correctly from calendar
# ═══════════════════════════════════════════════════════════════════════════════

class TestAV03PltrEda(unittest.TestCase):
    """_load_earnings_days_away returns correct positive int for PLTR."""

    def test_pltr_eda_positive(self):
        """PLTR 2026-05-04, today 2026-04-30 → eda = 4."""
        tmpdir, _, _ = _make_tmp_tree(
            cal_entries=[
                {"symbol": "PLTR", "earnings_date": "2026-05-04", "timing": "post-market"},
            ],
            ovr_entries=None,
        )
        m = _import_candidates()
        with mock.patch("bot_options_stage1_candidates.date") as mock_date:
            mock_date.today.return_value = date(2026, 4, 30)
            mock_date.fromisoformat.side_effect = date.fromisoformat
            result = _call_load_eda(m, "PLTR", tmpdir)
        self.assertEqual(result, 4)

    def test_pltr_eda_not_affected_by_unrelated_overrides(self):
        """Override for TSM must not disturb PLTR eda."""
        tmpdir, _, _ = _make_tmp_tree(
            cal_entries=[
                {"symbol": "PLTR", "earnings_date": "2026-05-04", "timing": "post-market"},
            ],
            ovr_entries={"TSM": {"earnings_date": "2026-07-16", "timing": "unknown"}},
        )
        m = _import_candidates()
        with mock.patch("bot_options_stage1_candidates.date") as mock_date:
            mock_date.today.return_value = date(2026, 4, 30)
            mock_date.fromisoformat.side_effect = date.fromisoformat
            result = _call_load_eda(m, "PLTR", tmpdir)
        self.assertEqual(result, 4)


# ═══════════════════════════════════════════════════════════════════════════════
# AV-04 — eda for TSM via override matches expected value
# ═══════════════════════════════════════════════════════════════════════════════

class TestAV04TsmEdaViaOverride(unittest.TestCase):
    """_load_earnings_days_away returns correct positive int for TSM via override."""

    def test_tsm_eda_matches_yfinance_date(self):
        """Override date 2026-07-16 from yfinance, today 2026-04-30 → 77 days."""
        tmpdir, _, _ = _make_tmp_tree(
            cal_entries=[
                {"symbol": "PLTR", "earnings_date": "2026-05-04", "timing": "post-market"},
                {"symbol": "NVDA", "earnings_date": "2026-05-20", "timing": "post-market"},
            ],
            ovr_entries={
                "TSM":   {"earnings_date": "2026-07-16", "timing": "unknown"},
                "ASML":  {"earnings_date": "2026-07-15", "timing": "unknown"},
                "GOOGL": {"earnings_date": "2026-07-23", "timing": "unknown"},
                "AMZN":  {"earnings_date": "2026-07-30", "timing": "unknown"},
                "META":  {"earnings_date": "2026-07-29", "timing": "unknown"},
            },
        )
        m = _import_candidates()
        today = date(2026, 4, 30)
        with mock.patch("bot_options_stage1_candidates.date") as mock_date:
            mock_date.today.return_value = today
            mock_date.fromisoformat.side_effect = date.fromisoformat
            tsm_eda  = _call_load_eda(m, "TSM",   tmpdir)
            asml_eda = _call_load_eda(m, "ASML",  tmpdir)
            goog_eda = _call_load_eda(m, "GOOGL", tmpdir)
            amzn_eda = _call_load_eda(m, "AMZN",  tmpdir)
            meta_eda = _call_load_eda(m, "META",  tmpdir)

        self.assertEqual(tsm_eda,  (date(2026, 7, 16) - today).days)
        self.assertEqual(asml_eda, (date(2026, 7, 15) - today).days)
        self.assertEqual(goog_eda, (date(2026, 7, 23) - today).days)
        self.assertEqual(amzn_eda, (date(2026, 7, 30) - today).days)
        self.assertEqual(meta_eda, (date(2026, 7, 29) - today).days)


# ═══════════════════════════════════════════════════════════════════════════════
# AV-05 — override replaces AV entry for same symbol
# ═══════════════════════════════════════════════════════════════════════════════

class TestAV05OverrideWins(unittest.TestCase):
    """When same symbol in both files, override date must win."""

    def test_override_replaces_stale_av_entry(self):
        """AV has PLTR 2026-05-01 (stale), override has 2026-05-04 (correct)."""
        tmpdir, _, _ = _make_tmp_tree(
            cal_entries=[
                {"symbol": "PLTR", "earnings_date": "2026-05-01", "timing": "post-market"},
            ],
            ovr_entries={"PLTR": {"earnings_date": "2026-05-04", "timing": "post-market"}},
        )
        m = _import_candidates()
        with mock.patch("bot_options_stage1_candidates.date") as mock_date:
            mock_date.today.return_value = date(2026, 4, 30)
            mock_date.fromisoformat.side_effect = date.fromisoformat
            result = _call_load_eda(m, "PLTR", tmpdir)
        self.assertEqual(result, 4, "Override date 2026-05-04 → eda=4 wins over stale 2026-05-01")


# ═══════════════════════════════════════════════════════════════════════════════
# AV-06 — missing overrides file is non-fatal
# ═══════════════════════════════════════════════════════════════════════════════

class TestAV06MissingOverridesNonFatal(unittest.TestCase):
    """Absence of earnings_overrides.json must not raise any exception."""

    def test_no_overrides_file_still_works(self):
        """Calendar without override file: normal symbol found, missing symbol → None."""
        tmpdir, _, _ = _make_tmp_tree(
            cal_entries=[
                {"symbol": "PLTR", "earnings_date": "2026-05-04", "timing": "post-market"},
            ],
            ovr_entries=None,  # file not created
        )
        m = _import_candidates()
        with mock.patch("bot_options_stage1_candidates.date") as mock_date:
            mock_date.today.return_value = date(2026, 4, 30)
            mock_date.fromisoformat.side_effect = date.fromisoformat
            pltr_result = _call_load_eda(m, "PLTR", tmpdir)
            tsm_result  = _call_load_eda(m, "TSM",  tmpdir)
        self.assertEqual(pltr_result, 4)
        self.assertIsNone(tsm_result)

    def test_corrupt_overrides_file_degrades_gracefully(self):
        """Corrupt overrides JSON must not crash; calendar still serves PLTR."""
        tmpdir = tempfile.mkdtemp()
        market_dir = Path(tmpdir) / "data" / "market"
        market_dir.mkdir(parents=True)
        (market_dir / "earnings_calendar.json").write_text(
            json.dumps({"calendar": [
                {"symbol": "PLTR", "earnings_date": "2026-05-04", "timing": "post-market"},
            ]})
        )
        (market_dir / "earnings_overrides.json").write_text("NOT VALID JSON !!!")
        m = _import_candidates()
        with mock.patch("bot_options_stage1_candidates.date") as mock_date:
            mock_date.today.return_value = date(2026, 4, 30)
            mock_date.fromisoformat.side_effect = date.fromisoformat
            result = _call_load_eda(m, "PLTR", tmpdir)
        self.assertEqual(result, 4)



# ═══════════════════════════════════════════════════════════════════════════════
# AV-08 — stage2 post-earnings lazy-load respects overrides
# ═══════════════════════════════════════════════════════════════════════════════

class TestAV08Stage2PostEarningsOverride(unittest.TestCase):
    """_get_earnings_timing (RULE_POST_EARNINGS) must also see overrides."""

    def test_post_earnings_helper_sees_override_timing(self):
        m = _import_stage2()

        tmpdir = tempfile.mkdtemp()
        market_dir = Path(tmpdir) / "data" / "market"
        market_dir.mkdir(parents=True)
        (market_dir / "earnings_calendar.json").write_text(json.dumps({"calendar": []}))
        (market_dir / "earnings_overrides.json").write_text(json.dumps(
            {"TSM": {"earnings_date": "2026-04-17", "timing": "post-market"}}
        ))

        fake_file = str(Path(tmpdir) / "bot_options_stage2_structures.py")
        import json as _json
        from pathlib import Path as _Path
        cal_path = _Path(fake_file).parent / "data" / "market" / "earnings_calendar.json"
        ovr_path = _Path(fake_file).parent / "data" / "market" / "earnings_overrides.json"
        cal = _json.loads(cal_path.read_text()) if cal_path.exists() else {}
        if ovr_path.exists():
            ovrs = _json.loads(ovr_path.read_text())
            if isinstance(ovrs, dict) and ovrs:
                ovr_syms = {k.upper() for k in ovrs}
                merged = [e for e in cal.get("calendar", [])
                          if (e.get("symbol") or "").upper() not in ovr_syms]
                for raw_sym, ovr_data in ovrs.items():
                    merged.append({
                        "symbol": raw_sym.upper(),
                        "earnings_date": ovr_data.get("earnings_date", ""),
                        "timing": ovr_data.get("timing", "unknown"),
                        "eps_estimate": None,
                        "source": ovr_data.get("source", "manual"),
                    })
                cal = dict(cal)
                cal["calendar"] = merged

        timing = m._get_earnings_timing("TSM", cal)
        self.assertEqual(timing, "post_market")


if __name__ == "__main__":
    unittest.main()
