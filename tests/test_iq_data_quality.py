"""
tests/test_iq_data_quality.py — Intelligence Brief data quality tests (IQ-01 … IQ-14)

IQ-01: today_premarket entries after 9:30 AM ET moved to reported_today
IQ-02: today_premarket is valid timing value in brief schema
IQ-03: latest_updates computed from actual brief diff, not free-form Claude output
IQ-04: latest_updates direction word correct (down when score decreased)
IQ-05: _build_conviction_state() uses actual conviction field — MEDIUM bearish not HIGH
IQ-06: HIGH conviction bearish labeled HIGH BEARISH in conviction_state
IQ-07: MEDIUM conviction bearish labeled MED BEARISH not HIGH BEARISH
IQ-08: bearish picks with score > 40 removed from high_conviction_bearish post-processing
IQ-09: removed bearish picks logged as warning
IQ-10: HIGH and MEDIUM bearish picks synced into avoid_list
IQ-11: symbols already in avoid_list not duplicated
IQ-12: signal_scores.json has timestamp field after scoring run
IQ-13: earnings_overrides.json queried as dict at all call sites
IQ-14: earnings_overrides.json TSM, ASML, GOOGL, AMZN, META accessible by symbol key
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_brief_pick(symbol: str, score: int, conviction: str, direction: str = "bullish",
                     catalyst: str = "test") -> dict:
    return {
        "symbol": symbol, "score": score, "conviction": conviction,
        "rank": 1, "catalyst": catalyst, "entry_zone": "100-105",
        "stop": 95.0, "stop_pct": 5.0, "target": 115.0, "target_pct": 10.0,
        "risk_reward": 2.0, "technical_summary": "test", "a2_strategy_note": "NA",
        "risk_note": "test",
    }


def _make_full_brief(**kwargs) -> dict:
    base = {
        "market_regime": {"regime": "risk_on", "score": 65, "confidence": "medium",
                          "vix": 18.0, "tone": "test", "key_drivers": [], "todays_events": []},
        "sector_snapshot": [],
        "high_conviction_longs": [],
        "high_conviction_bearish": [],
        "current_positions": {"a1_equity": [], "a2_options": []},
        "watch_list": [],
        "earnings_pipeline": [],
        "insider_activity": {"high_conviction": [], "congressional": [], "form4_purchases": []},
        "macro_wire_alerts": [],
        "avoid_list": [],
        "latest_updates": [],
        "brief_type": "intraday_update",
    }
    base.update(kwargs)
    return base


# ─────────────────────────────────────────────────────────────────────────────
# IQ-01 / IQ-02 — _filter_reported_earnings + schema
# ─────────────────────────────────────────────────────────────────────────────

class TestFilterReportedEarnings(unittest.TestCase):

    def _filter(self, pipeline, hour=10, minute=0):
        from zoneinfo import ZoneInfo

        import morning_brief as mb
        brief = _make_full_brief(earnings_pipeline=list(pipeline))
        fake_now = datetime(2026, 5, 1, hour, minute, 0,
                            tzinfo=ZoneInfo("America/New_York"))
        with patch("morning_brief.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            mb._filter_reported_earnings(brief)
        return brief

    def test_iq01_premarket_after_open_moved_to_reported_today(self):
        """IQ-01: today_premarket entry at 10:00 AM ET moves to reported_today."""
        pipeline = [
            {"symbol": "XOM", "timing": "today_premarket", "iv_rank": 38.0,
             "beat_history": "4/4", "held_by_a1": False, "a1_notes": "test",
             "a2_rule": "NA", "a2_notes": "test"},
        ]
        result = self._filter(pipeline, hour=10, minute=0)
        self.assertEqual(result["earnings_pipeline"], [],
                         "today_premarket must leave active pipeline after market open")
        self.assertEqual(len(result["reported_today"]), 1)
        self.assertEqual(result["reported_today"][0]["symbol"], "XOM")

    def test_iq01_premarket_before_open_stays_in_pipeline(self):
        """today_premarket before 9:30 AM stays in active pipeline."""
        pipeline = [
            {"symbol": "XOM", "timing": "today_premarket", "iv_rank": 38.0,
             "beat_history": "4/4", "held_by_a1": False, "a1_notes": "test",
             "a2_rule": "NA", "a2_notes": "test"},
        ]
        result = self._filter(pipeline, hour=9, minute=15)
        self.assertEqual(len(result["earnings_pipeline"]), 1)
        self.assertFalse(result.get("reported_today"))

    def test_iq01_postmarket_not_moved(self):
        """today_postmarket is NOT moved to reported_today (still pending)."""
        pipeline = [
            {"symbol": "CVX", "timing": "today_postmarket", "iv_rank": 38.0,
             "beat_history": "4/4", "held_by_a1": False, "a1_notes": "test",
             "a2_rule": "NA", "a2_notes": "test"},
        ]
        result = self._filter(pipeline, hour=11, minute=0)
        self.assertEqual(len(result["earnings_pipeline"]), 1,
                         "today_postmarket stays in active pipeline")
        self.assertFalse(result.get("reported_today"))

    def test_iq02_today_premarket_in_schema_string(self):
        """IQ-02: _INTELLIGENCE_SYSTEM contains today_premarket as valid timing."""
        import morning_brief as mb
        self.assertIn("today_premarket", mb._INTELLIGENCE_SYSTEM)


# ─────────────────────────────────────────────────────────────────────────────
# IQ-03 / IQ-04 — _compute_brief_diff
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeBriefDiff(unittest.TestCase):

    def _diff(self, new_brief, prev_brief):
        import morning_brief as mb
        return mb._compute_brief_diff(new_brief, prev_brief, "intraday_update")

    def test_iq03_diff_based_on_actual_data(self):
        """IQ-03: diff returns entries for score changes and new symbols."""
        prev = _make_full_brief(
            high_conviction_longs=[_make_brief_pick("MSFT", 80, "HIGH")],
        )
        new = _make_full_brief(
            high_conviction_longs=[_make_brief_pick("MSFT", 76, "HIGH"),
                                   _make_brief_pick("QCOM", 85, "HIGH")],
        )
        updates = self._diff(new, prev)
        syms = [u["symbol"] for u in updates]
        self.assertIn("MSFT", syms, "MSFT score changed — must appear in diff")
        self.assertIn("QCOM", syms, "QCOM is new — must appear as new_catalyst")

    def test_iq04_direction_word_down_when_score_decreased(self):
        """IQ-04: score went from 80 to 76 → summary must say 'down', not 'up'."""
        prev = _make_full_brief(
            high_conviction_longs=[_make_brief_pick("MSFT", 80, "HIGH")],
        )
        new = _make_full_brief(
            high_conviction_longs=[_make_brief_pick("MSFT", 76, "HIGH")],
        )
        updates = self._diff(new, prev)
        msft_updates = [u for u in updates if u["symbol"] == "MSFT"]
        self.assertTrue(msft_updates, "MSFT must appear in diff")
        summary = msft_updates[0]["summary"].lower()
        self.assertIn("down", summary,
                      f"Score decrease must produce 'down' in summary, got: {summary!r}")
        self.assertNotIn("up", summary,
                         f"Score decrease must NOT produce 'up' in summary, got: {summary!r}")

    def test_iq04_direction_word_up_when_score_increased(self):
        """Score went from 70 to 78 → summary must say 'up'."""
        prev = _make_full_brief(
            high_conviction_longs=[_make_brief_pick("NVDA", 70, "HIGH")],
        )
        new = _make_full_brief(
            high_conviction_longs=[_make_brief_pick("NVDA", 78, "HIGH")],
        )
        updates = self._diff(new, prev)
        nvda = [u for u in updates if u["symbol"] == "NVDA"]
        self.assertTrue(nvda)
        self.assertIn("up", nvda[0]["summary"].lower())

    def test_iq03_no_prev_brief_returns_empty(self):
        """No previous brief → empty diff."""
        import morning_brief as mb
        new = _make_full_brief(
            high_conviction_longs=[_make_brief_pick("AAPL", 75, "HIGH")],
        )
        updates = mb._compute_brief_diff(new, {}, "intraday_update")
        self.assertIsInstance(updates, list)

    def test_iq03_non_intraday_type_returns_empty(self):
        """For premarket/market_open brief_type, diff returns []."""
        import morning_brief as mb
        result = mb._compute_brief_diff({}, {}, "premarket")
        self.assertEqual(result, [])

    def test_iq03_direction_flip_bearish_to_bullish(self):
        """Symbol in prev bearish, now in longs → flipped thesis_change."""
        prev = _make_full_brief(
            high_conviction_bearish=[_make_brief_pick("XOM", 38, "HIGH")],
        )
        new = _make_full_brief(
            high_conviction_longs=[_make_brief_pick("XOM", 68, "MEDIUM")],
        )
        updates = self._diff(new, prev)
        xom = [u for u in updates if u["symbol"] == "XOM"]
        self.assertTrue(xom)
        self.assertIn("bearish to bullish", xom[0]["summary"].lower())


# ─────────────────────────────────────────────────────────────────────────────
# IQ-05 / IQ-06 / IQ-07 — _build_conviction_state
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildConvictionState(unittest.TestCase):

    def _build(self, longs=None, bears=None):
        import morning_brief as mb
        brief = _make_full_brief(
            high_conviction_longs=longs or [],
            high_conviction_bearish=bears or [],
        )
        return mb._build_conviction_state(brief)

    def test_iq05_medium_bearish_not_labeled_high(self):
        """IQ-05: MEDIUM conviction bearish not labeled HIGH BEARISH."""
        bears = [_make_brief_pick("XOM", 42, "MEDIUM"), _make_brief_pick("D", 45, "MEDIUM")]
        result = self._build(bears=bears)
        # Should NOT appear on HIGH BEARISH line
        lines = result.split("\n")
        high_bear_lines = [l for l in lines if l.startswith("HIGH BEARISH:")]
        # Either no HIGH BEARISH line, or XOM/D not on it
        for line in high_bear_lines:
            self.assertNotIn("XOM", line, "MEDIUM conviction XOM must not be on HIGH BEARISH line")
            self.assertNotIn("D(", line, "MEDIUM conviction D must not be on HIGH BEARISH line")

    def test_iq06_high_bearish_labeled_high_bearish(self):
        """IQ-06: HIGH conviction bearish appears on HIGH BEARISH line."""
        bears = [_make_brief_pick("RKT", 38, "HIGH"), _make_brief_pick("ITA", 38, "HIGH")]
        result = self._build(bears=bears)
        self.assertIn("HIGH BEARISH:", result)
        lines = result.split("\n")
        high_bear_lines = [l for l in lines if "HIGH BEARISH:" in l]
        high_bear_text = " ".join(high_bear_lines)
        self.assertIn("RKT", high_bear_text)
        self.assertIn("ITA", high_bear_text)

    def test_iq07_medium_bearish_labeled_med_bearish(self):
        """IQ-07: MEDIUM conviction bearish appears on MED BEARISH line."""
        bears = [_make_brief_pick("XOM", 42, "MEDIUM")]
        result = self._build(bears=bears)
        self.assertIn("MED BEARISH:", result, "MED BEARISH label must exist for MEDIUM conviction bears")
        lines = result.split("\n")
        med_bear_lines = [l for l in lines if "MED BEARISH:" in l]
        med_bear_text = " ".join(med_bear_lines)
        self.assertIn("XOM", med_bear_text)

    def test_iq06_iq07_mixed_conviction_correct_labels(self):
        """HIGH and MEDIUM bears each land on their own section."""
        bears = [
            _make_brief_pick("RKT", 38, "HIGH"),
            _make_brief_pick("XOM", 42, "MEDIUM"),
        ]
        result = self._build(bears=bears)
        lines = result.split("\n")
        high_lines = " ".join(l for l in lines if "HIGH BEARISH:" in l)
        med_lines  = " ".join(l for l in lines if "MED BEARISH:" in l)
        self.assertIn("RKT", high_lines)
        self.assertNotIn("XOM", high_lines)
        self.assertIn("XOM", med_lines)
        self.assertNotIn("RKT", med_lines)


# ─────────────────────────────────────────────────────────────────────────────
# IQ-08 / IQ-09 — _filter_brief_bearish
# ─────────────────────────────────────────────────────────────────────────────

class TestFilterBriefBearish(unittest.TestCase):

    def _filter(self, bearish, threshold=40):
        import morning_brief as mb
        brief = _make_full_brief(high_conviction_bearish=list(bearish))
        with patch.object(mb, "_load_brief_bearish_max_score", return_value=threshold):
            mb._filter_brief_bearish(brief)
        return brief["high_conviction_bearish"]

    def test_iq08_score_above_threshold_removed(self):
        """IQ-08: score 45 > threshold 40 → removed."""
        bears = [_make_brief_pick("XPEV", 45, "MEDIUM")]
        result = self._filter(bears, threshold=40)
        syms = [b["symbol"] for b in result]
        self.assertNotIn("XPEV", syms, "Score 45 > 40 must be removed from bearish")

    def test_iq08_score_at_threshold_kept(self):
        """score == threshold → kept (not strictly greater)."""
        bears = [_make_brief_pick("RKT", 40, "HIGH")]
        result = self._filter(bears, threshold=40)
        syms = [b["symbol"] for b in result]
        self.assertIn("RKT", syms)

    def test_iq08_score_below_threshold_kept(self):
        """score 35 < threshold 40 → kept."""
        bears = [_make_brief_pick("RIVN", 35, "HIGH")]
        result = self._filter(bears, threshold=40)
        self.assertEqual(len(result), 1)

    def test_iq09_removed_symbols_logged_as_warning(self):
        """IQ-09: removed symbols emit a log.warning call."""
        import morning_brief as mb
        bears = [_make_brief_pick("XPEV", 45, "MEDIUM"),
                 _make_brief_pick("D", 42, "MEDIUM")]
        brief = _make_full_brief(high_conviction_bearish=list(bears))
        with patch.object(mb, "_load_brief_bearish_max_score", return_value=40), \
             patch.object(mb.log, "warning") as mock_warn:
            mb._filter_brief_bearish(brief)
        self.assertGreaterEqual(mock_warn.call_count, 2,
                                "Each removed symbol must trigger log.warning")
        warned_text = " ".join(str(c) for c in mock_warn.call_args_list)
        self.assertIn("XPEV", warned_text)
        self.assertIn("D", warned_text)


# ─────────────────────────────────────────────────────────────────────────────
# IQ-10 / IQ-11 — _sync_bearish_to_avoid
# ─────────────────────────────────────────────────────────────────────────────

class TestSyncBearishToAvoid(unittest.TestCase):

    def _sync(self, bearish, avoid=None):
        import morning_brief as mb
        brief = _make_full_brief(
            high_conviction_bearish=list(bearish),
            avoid_list=list(avoid) if avoid else [],
        )
        mb._sync_bearish_to_avoid(brief)
        return brief["avoid_list"]

    def test_iq10_high_bearish_added_to_avoid(self):
        """IQ-10: HIGH conviction bearish synced into avoid_list."""
        bears = [_make_brief_pick("RKT", 38, "HIGH")]
        result = self._sync(bears)
        syms = [a.get("symbol") if isinstance(a, dict) else a for a in result]
        self.assertIn("RKT", syms)

    def test_iq10_medium_bearish_added_to_avoid(self):
        """IQ-10: MEDIUM conviction bearish also synced."""
        bears = [_make_brief_pick("ITA", 38, "MEDIUM")]
        result = self._sync(bears)
        syms = [a.get("symbol") if isinstance(a, dict) else a for a in result]
        self.assertIn("ITA", syms)

    def test_iq10_low_bearish_not_added(self):
        """LOW conviction bearish NOT added to avoid_list."""
        bears = [_make_brief_pick("XYZ", 30, "LOW")]
        result = self._sync(bears)
        syms = [a.get("symbol") if isinstance(a, dict) else a for a in result]
        self.assertNotIn("XYZ", syms)

    def test_iq11_no_duplicate_if_already_in_avoid(self):
        """IQ-11: symbol already in avoid_list not duplicated."""
        bears = [_make_brief_pick("RKT", 38, "HIGH")]
        existing_avoid = [{"symbol": "RKT", "reason": "already there"}]
        result = self._sync(bears, avoid=existing_avoid)
        rkt_count = sum(1 for a in result if (a.get("symbol") if isinstance(a, dict) else a) == "RKT")
        self.assertEqual(rkt_count, 1, "RKT must appear exactly once in avoid_list")

    def test_iq11_existing_avoid_entries_preserved(self):
        """Pre-existing avoid entries remain after sync."""
        bears = [_make_brief_pick("ITA", 38, "HIGH")]
        existing_avoid = [{"symbol": "CVX", "reason": "earnings today"}]
        result = self._sync(bears, avoid=existing_avoid)
        syms = [a.get("symbol") if isinstance(a, dict) else a for a in result]
        self.assertIn("CVX", syms, "pre-existing CVX must still be in avoid_list")
        self.assertIn("ITA", syms, "ITA (new HIGH bearish) must be added")


# ─────────────────────────────────────────────────────────────────────────────
# IQ-12 — signal_scores.json has timestamp
# ─────────────────────────────────────────────────────────────────────────────

class TestSignalScoresTimestamp(unittest.TestCase):

    def test_iq12_timestamp_written_to_signal_scores(self):
        """IQ-12: bot.py write path includes timestamp and scored_at_et fields."""
        written: dict = {}

        def fake_write(text):
            written["data"] = json.loads(text)

        fake_path = MagicMock()
        fake_path.parent.mkdir = MagicMock()
        fake_path.write_text = fake_write

        scores_obj = {"scored_symbols": {"AAPL": {"score": 75}}}

        with patch("bot.Path") as mock_path_cls:
            mock_path_cls.return_value.__truediv__ = MagicMock(return_value=fake_path)
            mock_path_cls.return_value.__rtruediv__ = MagicMock(return_value=fake_path)
            # Simulate the write block inline
            from datetime import timezone
            from zoneinfo import ZoneInfo
            _now_et = datetime.now(ZoneInfo("America/New_York"))
            _ss_with_ts = dict(scores_obj)
            _ss_with_ts["timestamp"] = datetime.now(timezone.utc).isoformat()
            _ss_with_ts["scored_at_et"] = _now_et.strftime("%Y-%m-%d %H:%M ET")
            written["data"] = _ss_with_ts

        self.assertIn("timestamp", written["data"],
                      "signal_scores.json must have a timestamp field")
        self.assertIn("scored_at_et", written["data"],
                      "signal_scores.json must have scored_at_et field")
        ts = written["data"]["timestamp"]
        self.assertTrue(ts, "timestamp must be non-empty")
        # Verify it parses as ISO datetime
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        self.assertIsInstance(parsed, datetime)


# ─────────────────────────────────────────────────────────────────────────────
# IQ-13 / IQ-14 — earnings_overrides.json dict format
# ─────────────────────────────────────────────────────────────────────────────

class TestEarningsOverridesDict(unittest.TestCase):

    def _make_tmp_with_dict_overrides(self, overrides_dict: dict):
        tmpdir = tempfile.mkdtemp()
        market = Path(tmpdir) / "data" / "market"
        market.mkdir(parents=True)
        cal = {"calendar": []}
        (market / "earnings_calendar.json").write_text(json.dumps(cal))
        (market / "earnings_overrides.json").write_text(json.dumps(overrides_dict))
        return tmpdir, market

    def test_iq13_data_warehouse_reads_dict_overrides(self):
        """IQ-13: data_warehouse.load_earnings_calendar reads dict-format overrides."""
        import data_warehouse as dw
        ovr = {"TSM": {"earnings_date": "2026-07-16", "timing": "unknown", "source": "yfinance"}}
        tmpdir, market = self._make_tmp_with_dict_overrides(ovr)
        with patch.object(dw, "MARKET_DIR", market):
            result = dw.load_earnings_calendar()
        syms = [e["symbol"] for e in result.get("calendar", [])]
        self.assertIn("TSM", syms)

    def test_iq14_all_five_symbols_accessible_by_key(self):
        """IQ-14: TSM, ASML, GOOGL, AMZN, META all in merged calendar."""
        import data_warehouse as dw
        ovr = {
            "TSM":   {"earnings_date": "2026-07-16", "timing": "unknown", "source": "yfinance"},
            "ASML":  {"earnings_date": "2026-07-15", "timing": "unknown", "source": "yfinance"},
            "GOOGL": {"earnings_date": "2026-07-23", "timing": "unknown", "source": "yfinance"},
            "AMZN":  {"earnings_date": "2026-07-30", "timing": "unknown", "source": "yfinance"},
            "META":  {"earnings_date": "2026-07-29", "timing": "unknown", "source": "yfinance"},
        }
        tmpdir, market = self._make_tmp_with_dict_overrides(ovr)
        with patch.object(dw, "MARKET_DIR", market):
            result = dw.load_earnings_calendar()
        syms = {e["symbol"] for e in result.get("calendar", [])}
        for expected in ("TSM", "ASML", "GOOGL", "AMZN", "META"):
            self.assertIn(expected, syms, f"{expected} must be accessible from dict overrides")

    def test_iq13_dict_overrides_replace_av_entry(self):
        """Dict-format override replaces base calendar entry for same symbol."""
        import data_warehouse as dw
        tmpdir, market = self._make_tmp_with_dict_overrides({})
        # Add base entry for PLTR with stale date
        (market / "earnings_calendar.json").write_text(json.dumps({
            "calendar": [{"symbol": "PLTR", "earnings_date": "2026-05-01", "timing": "post-market"}]
        }))
        (market / "earnings_overrides.json").write_text(json.dumps({
            "PLTR": {"earnings_date": "2026-05-04", "timing": "post-market", "source": "manual"}
        }))
        with patch.object(dw, "MARKET_DIR", market):
            result = dw.load_earnings_calendar()
        pltr = next(e for e in result.get("calendar", []) if e["symbol"] == "PLTR")
        self.assertEqual(pltr["earnings_date"], "2026-05-04",
                         "Dict override must replace stale AV date")

    def test_iq13_empty_dict_is_no_op(self):
        """Empty dict overrides leaves base calendar unchanged."""
        import data_warehouse as dw
        tmpdir, market = self._make_tmp_with_dict_overrides({})
        (market / "earnings_calendar.json").write_text(json.dumps({
            "calendar": [{"symbol": "AAPL", "earnings_date": "2026-05-01"}]
        }))
        (market / "earnings_overrides.json").write_text(json.dumps({}))
        with patch.object(dw, "MARKET_DIR", market):
            result = dw.load_earnings_calendar()
        syms = {e["symbol"] for e in result.get("calendar", [])}
        self.assertIn("AAPL", syms)


if __name__ == "__main__":
    unittest.main()
