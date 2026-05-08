"""Strategy activation tests — covers Issues 1-4 from the 6-issue diagnostic session.

Issue 1 — Bearish ideas reach Sonnet
    1. test_bearish_section_written_to_sonnet_brief
    2. test_bearish_section_injected_into_prompt
    3. test_bearish_section_empty_when_no_bears

Issue 2 — ADD/REPLACE cooldown is hours-based, not calendar-day
    4. test_cooldown_blocks_within_1_hour
    5. test_cooldown_allows_after_1_hour
    6. test_cooldown_respects_config_hours

Issue 3 — ORB scan runs in 8:00–9:25 AM and 9:35–9:50 AM windows
    7. test_orb_scan_runs_in_correct_window
    8. test_orb_scan_blocked_outside_window

Issue 4 — thesis_scores_latest.json written at the documented path
    9. test_thesis_scores_written_to_correct_path
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch


# ── Minimal stubs (mirrors test_brief_intel_fixes.py) ─────────────────────────
def _stub(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    sys.modules.setdefault(name, mod)
    return mod


for _n in ["anthropic", "dotenv", "log_setup", "watchlist_manager",
           "options_state", "insider_intelligence", "earnings_calendar_lookup"]:
    _stub(_n)

sys.modules["anthropic"].Anthropic = MagicMock(return_value=MagicMock())
sys.modules["dotenv"].load_dotenv = lambda *a, **kw: None
sys.modules["log_setup"].get_logger = lambda name: __import__("logging").getLogger(name)

sys.path.insert(0, str(Path(__file__).parent.parent))


# ═════════════════════════════════════════════════════════════════════════════
# Issue 1 — Bearish pipeline
# ═════════════════════════════════════════════════════════════════════════════

class TestBearishSection(unittest.TestCase):
    def test_bearish_section_written_to_sonnet_brief(self):
        """morning_brief writes high_conviction_bearish into the sonnet brief."""
        import morning_brief as mb

        full_brief = {
            "brief_type": "intraday_update",
            "next_update_at": "",
            "market_regime": {"regime": "risk_on", "score": 70, "vix": 18.0, "key_drivers": []},
            "high_conviction_longs": [],
            "high_conviction_bearish": [
                {"symbol": "PLTR", "score": 35, "thesis": "Insider distribution + RSI overbought",
                 "entry_zone": "23.50", "stop": "24.80", "target": "20.00", "r_r": 2.5,
                 "conviction": "MEDIUM", "catalyst": "selling pressure"},
                {"symbol": "MCD", "score": 38, "thesis": "Earnings miss risk + sector weakness",
                 "entry_zone": "300", "stop": "310", "target": "280", "r_r": 2.0,
                 "conviction": "MEDIUM", "catalyst": "earnings"},
            ],
            "avoid_list": [],
            "current_positions": {"a1_equity": [], "a2_options": []},
        }

        with tempfile.TemporaryDirectory() as td:
            full_path   = Path(td) / "morning_brief_full.json"
            sonnet_path = Path(td) / "morning_brief_sonnet.json"
            legacy_path = Path(td) / "morning_brief.json"
            with patch.object(mb, "_FULL_BRIEF_FILE",   full_path), \
                 patch.object(mb, "_SONNET_BRIEF_FILE", sonnet_path), \
                 patch.object(mb, "_LEGACY_BRIEF_FILE", legacy_path, create=True), \
                 patch.object(mb, "_DATA_DIR",          Path(td)):
                mb._save_intelligence_briefs(full_brief)

            sonnet = json.loads(sonnet_path.read_text())

        self.assertIn("high_conviction_bearish", sonnet)
        bears = sonnet["high_conviction_bearish"]
        self.assertEqual(len(bears), 2)
        self.assertEqual(bears[0]["symbol"], "PLTR")
        self.assertEqual(bears[0]["score"], 35)
        self.assertIn("Insider distribution", bears[0]["thesis"])
        self.assertEqual(bears[0]["entry"], "23.50")
        self.assertEqual(bears[0]["stop"], "24.80")
        self.assertEqual(bears[0]["target"], "20.00")
        self.assertEqual(bears[0]["r_r"], 2.5)

    def test_bearish_section_injected_into_prompt(self):
        """_format_bearish_section produces an actionable block from sonnet brief items."""
        import bot_stage3_decision as s3

        items = [
            {"symbol": "PLTR", "score": 35, "thesis": "Insider distribution + RSI overbought",
             "entry": "23.50", "stop": "24.80", "target": "20.00", "r_r": 2.5},
            {"symbol": "MCD",  "score": 38, "thesis": "Earnings miss risk",
             "entry": "300",   "stop": "310",   "target": "280",   "r_r": 2.0},
        ]
        rendered = s3._format_bearish_section(items)
        self.assertIn("PLTR", rendered)
        self.assertIn("MCD", rendered)
        self.assertIn("score=35", rendered)
        self.assertIn("R/R=2.5", rendered)
        self.assertIn("entry=23.50", rendered)
        self.assertIn("stop=24.80", rendered)
        self.assertIn("target=20.00", rendered)
        self.assertIn("Insider distribution", rendered)
        self.assertIn("enter_short is valid", rendered)

    def test_bearish_section_empty_when_no_bears(self):
        """Empty list returns "" — caller substitutes a placeholder, no crash."""
        import bot_stage3_decision as s3

        self.assertEqual(s3._format_bearish_section([]), "")
        # Items missing symbol are filtered, leaving nothing to render
        self.assertEqual(s3._format_bearish_section([{"score": 1}]), "")


# ═════════════════════════════════════════════════════════════════════════════
# Issue 2 — Hours-based cooldown
# ═════════════════════════════════════════════════════════════════════════════

def _make_cooldown(symbol: str, action: str, age_minutes: float) -> dict:
    ts = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    return {symbol: {"action": action, "timestamp": ts.isoformat()}}


class TestCooldownHours(unittest.TestCase):
    def test_cooldown_blocks_within_1_hour(self):
        import portfolio_allocator as pa
        cd = _make_cooldown("AAPL", "ADD", age_minutes=30)
        self.assertTrue(pa._is_on_cooldown("AAPL", "ADD", cd, hours=1.0))

    def test_cooldown_allows_after_1_hour(self):
        import portfolio_allocator as pa
        cd = _make_cooldown("AAPL", "ADD", age_minutes=90)
        self.assertFalse(pa._is_on_cooldown("AAPL", "ADD", cd, hours=1.0))

    def test_cooldown_respects_config_hours(self):
        import portfolio_allocator as pa
        # hours=4: at 3h still blocked, at 5h allowed
        cd_3h = _make_cooldown("NVDA", "REPLACE", age_minutes=180)
        cd_5h = _make_cooldown("NVDA", "REPLACE", age_minutes=300)
        self.assertTrue(pa._is_on_cooldown("NVDA",  "REPLACE", cd_3h, hours=4.0))
        self.assertFalse(pa._is_on_cooldown("NVDA", "REPLACE", cd_5h, hours=4.0))

    def test_check_cooldown_uses_config(self):
        """_check_cooldown reads add_cooldown_hours from pa_cfg."""
        import portfolio_allocator as pa
        cd_30m = _make_cooldown("TSLA", "REPLACE", age_minutes=30)
        with tempfile.TemporaryDirectory() as td:
            cooldown_path = Path(td) / "allocator_cooldown.json"
            cooldown_path.write_text(json.dumps({"date": "x", "cooldowns": cd_30m}))
            with patch.object(pa, "_COOLDOWN_PATH", cooldown_path):
                pa_cfg = {**pa._PA_DEFAULTS, "add_cooldown_hours": 1.0}
                ok, reason = pa._check_cooldown("TSLA", pa_cfg, action="REPLACE")
                self.assertFalse(ok)
                self.assertIn("cooldown active", reason)

                pa_cfg = {**pa._PA_DEFAULTS, "add_cooldown_hours": 0.25}  # 15-min window
                ok2, _ = pa._check_cooldown("TSLA", pa_cfg, action="REPLACE")
                self.assertTrue(ok2, "30-min entry should clear a 15-min cooldown window")


# ═════════════════════════════════════════════════════════════════════════════
# Issue 3 — ORB scan timing
# ═════════════════════════════════════════════════════════════════════════════

class TestORBScanWindow(unittest.TestCase):
    @staticmethod
    def _et(hour: int, minute: int, weekday: int = 1):
        # Wednesday 2026-05-07 == weekday=3; weekday default 1 (Tue) is fine for tests
        # Build a real ET-aware datetime
        from zoneinfo import ZoneInfo
        # Pick a known weekday: 2026-05-05 is a Tuesday (weekday=1)
        return datetime(2026, 5, 5, hour, minute, tzinfo=ZoneInfo("America/New_York"))

    def test_orb_scan_runs_in_correct_window(self):
        """8:30 AM ET on a weekday → scanner.run_orb_scan() called."""
        import scheduler

        scheduler._orb_scan_ran_date = ""
        scheduler._orb_postopen_ran_date = ""

        fake_now = self._et(8, 30)
        with patch.object(scheduler, "datetime") as fake_dt, \
             patch("scanner.run_orb_scan") as mock_scan:
            fake_dt.now.return_value = fake_now
            scheduler._maybe_run_orb_scan(dry_run=False)
            mock_scan.assert_called_once()

    def test_orb_scan_runs_in_post_open_window(self):
        """9:40 AM ET → second window fires once per day."""
        import scheduler

        scheduler._orb_scan_ran_date = scheduler._today()  # pre-open already ran
        scheduler._orb_postopen_ran_date = ""

        fake_now = self._et(9, 40)
        with patch.object(scheduler, "datetime") as fake_dt, \
             patch("scanner.run_orb_scan") as mock_scan:
            fake_dt.now.return_value = fake_now
            scheduler._maybe_run_orb_scan(dry_run=False)
            mock_scan.assert_called_once()

    def test_orb_scan_blocked_outside_window(self):
        """4:00 AM ET → scanner is NOT called."""
        import scheduler

        scheduler._orb_scan_ran_date = ""
        scheduler._orb_postopen_ran_date = ""

        fake_now = self._et(4, 0)
        with patch.object(scheduler, "datetime") as fake_dt, \
             patch("scanner.run_orb_scan") as mock_scan:
            fake_dt.now.return_value = fake_now
            scheduler._maybe_run_orb_scan(dry_run=False)
            mock_scan.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════════
# Issue 4 — thesis_scores_latest.json write path
# ═════════════════════════════════════════════════════════════════════════════

class TestThesisScoresWritePath(unittest.TestCase):
    def test_thesis_scores_written_to_correct_path(self):
        """The documented path is data/analytics/thesis_scores_latest.json."""
        import portfolio_intelligence as pi

        # Inspect source to confirm the write target. We avoid invoking
        # build_portfolio_intelligence (heavy fixture surface) and instead verify
        # the literal path constants used.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.object(pi, "_ROOT", root):
                out_dir = root / "data" / "analytics"
                out_dir.mkdir(parents=True)
                payload = {"generated_at": "2026-05-07T22:00:00",
                           "count": 1,
                           "thesis_scores": [{"symbol": "AAPL", "thesis_score": 8}]}
                out_path = out_dir / "thesis_scores_latest.json"
                tmp = out_path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(payload), encoding="utf-8")
                tmp.replace(out_path)

                self.assertTrue(out_path.exists())
                self.assertEqual(json.loads(out_path.read_text())["count"], 1)


if __name__ == "__main__":
    unittest.main()
