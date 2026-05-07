"""Tests for the three morning_brief intelligence-context fixes.

FIX 1 — generate_intelligence_brief: skip file write on API failure.
FIX 2 — load_intelligence_brief: detect error stub, return {}.
FIX 3 — load_sonnet_brief: detect error stub via paired full brief, return {}.
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


# ── Minimal stubs (same pattern as test_morning_brief_held_exempt.py) ─────────
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
import morning_brief as mb  # noqa: E402

# ── Fixtures ──────────────────────────────────────────────────────────────────

_ERROR_STUB_FULL = {
    "market_regime": {
        "regime": "caution",
        "score": 50,
        "confidence": "low",
        "vix": 20.0,
        "tone": "Intelligence brief generation failed: Error code: 400 - credit balance too low",
        "key_drivers": [],
        "todays_events": [],
    },
    "sector_snapshot": [],
    "high_conviction_longs": [],
    "high_conviction_bearish": [],
    "current_positions": {"a1_equity": [], "a2_options": []},
    "watch_list": [],
    "earnings_pipeline": [],
    "macro_wire_alerts": [],
    "insider_activity": {"high_conviction": [], "congressional": [], "form4_purchases": []},
    "avoid_list": [],
    "latest_updates": [],
}

_VALID_FULL = {
    "market_regime": {
        "regime": "risk_on",
        "score": 72,
        "confidence": "high",
        "vix": 18.5,
        "tone": "Tech-led rally with AI capex supercycle driving broad momentum.",
        "key_drivers": ["AI capex", "Fed pivot", "Iran ceasefire"],
        "todays_events": [],
    },
    "sector_snapshot": [{"sector": "Technology", "bias": "bullish", "score": 85}],
    "high_conviction_longs": [
        {"symbol": "NVDA", "thesis": "AI capex demand", "conviction": "HIGH",
         "entry_zone": "195-200", "stop": "185", "target": "230", "risk_note": "ER risk"},
    ],
    "high_conviction_bearish": [],
    "current_positions": {"a1_equity": [{"symbol": "NVDA", "unrealized_pct": 3.5}], "a2_options": []},
    "watch_list": [],
    "earnings_pipeline": [],
    "macro_wire_alerts": [],
    "insider_activity": {"high_conviction": [], "congressional": [], "form4_purchases": []},
    "avoid_list": [],
    "latest_updates": [],
}

_ERROR_SONNET = {
    "generated_at": "2026-05-06T16:30:30",
    "brief_type": "intraday_update",
    "conviction_state": "CONVICTION STATE [updated 12:30 PM ET]",
    "regime_line": "caution(50) VIX=20.0",
    "positions_line": "A1: none | A2: none",
    "avoid_line": "AVOID: none",
}

_VALID_SONNET = {
    "generated_at": "2026-05-06T10:25:00",
    "brief_type": "intraday_update",
    "conviction_state": "CONVICTION STATE [updated 10:25 AM ET]\nNVDA HIGH LONG 195-200",
    "regime_line": "risk_on(72) VIX=18.5 — AI rally",
    "positions_line": "A1: NVDA+3.5% AAPL+2.1% | A2: none",
    "avoid_line": "AVOID: PLTR CVS",
}


# ── FIX 2 Tests: load_intelligence_brief ──────────────────────────────────────

class TestLoadIntelligenceBriefErrorStub(unittest.TestCase):
    def test_returns_empty_for_error_stub(self):
        """load_intelligence_brief returns {} when the file has an error stub tone."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(_ERROR_STUB_FULL, f)
            fpath = Path(f.name)
        with patch.object(mb, "_FULL_BRIEF_FILE", fpath):
            result = mb.load_intelligence_brief()
        fpath.unlink()
        self.assertEqual(result, {}, "Error stub should be treated as missing brief")

    def test_returns_valid_brief_when_not_stub(self):
        """load_intelligence_brief returns the brief when tone has no error marker."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(_VALID_FULL, f)
            fpath = Path(f.name)
        with patch.object(mb, "_FULL_BRIEF_FILE", fpath):
            result = mb.load_intelligence_brief()
        fpath.unlink()
        self.assertIn("high_conviction_longs", result)
        self.assertEqual(len(result["high_conviction_longs"]), 1)

    def test_returns_empty_when_file_missing(self):
        nonexistent = Path("/tmp/no_such_brief_12345.json")
        with patch.object(mb, "_FULL_BRIEF_FILE", nonexistent):
            result = mb.load_intelligence_brief()
        self.assertEqual(result, {})


# ── FIX 3 Tests: load_sonnet_brief ────────────────────────────────────────────

class TestLoadSonnetBriefErrorStub(unittest.TestCase):
    def test_returns_empty_when_paired_full_brief_is_error_stub(self):
        """load_sonnet_brief returns {} when the paired full brief is an error stub."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as sf:
            json.dump(_ERROR_SONNET, sf)
            sonnet_path = Path(sf.name)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as ff:
            json.dump(_ERROR_STUB_FULL, ff)
            full_path = Path(ff.name)
        with patch.object(mb, "_SONNET_BRIEF_FILE", sonnet_path), \
             patch.object(mb, "_FULL_BRIEF_FILE", full_path):
            result = mb.load_sonnet_brief()
        sonnet_path.unlink()
        full_path.unlink()
        self.assertEqual(result, {},
                         "Sonnet brief paired with error stub should be suppressed")

    def test_returns_valid_sonnet_brief_when_full_brief_is_valid(self):
        """load_sonnet_brief returns the brief when full brief is not an error stub."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as sf:
            json.dump(_VALID_SONNET, sf)
            sonnet_path = Path(sf.name)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as ff:
            json.dump(_VALID_FULL, ff)
            full_path = Path(ff.name)
        with patch.object(mb, "_SONNET_BRIEF_FILE", sonnet_path), \
             patch.object(mb, "_FULL_BRIEF_FILE", full_path):
            result = mb.load_sonnet_brief()
        sonnet_path.unlink()
        full_path.unlink()
        self.assertIn("regime_line", result)
        # Positions line should contain actual position data (not just "none")
        self.assertIn("NVDA", result.get("positions_line", ""))

    def test_returns_empty_when_sonnet_file_missing(self):
        nonexistent = Path("/tmp/no_such_sonnet_12345.json")
        with patch.object(mb, "_SONNET_BRIEF_FILE", nonexistent):
            result = mb.load_sonnet_brief()
        self.assertEqual(result, {})


# ── FIX 1 Tests: generate_intelligence_brief does not write on failure ─────────

class TestGenerateIntelligenceBriefNoWriteOnFailure(unittest.TestCase):
    def test_save_not_called_on_api_failure(self):
        """_save_intelligence_briefs must NOT be called when Claude API raises."""
        with patch.object(mb, "_save_intelligence_briefs") as mock_save, \
             patch.object(mb, "_claude") as mock_client, \
             patch.object(mb, "_load_context", return_value="test context"), \
             patch.object(mb, "_load_prev_daily_brief", return_value=None):
            mock_client.messages.create.side_effect = Exception("Error code: 400 - credit balance too low")
            result = mb.generate_intelligence_brief()
        mock_save.assert_not_called()
        self.assertIn("Intelligence brief generation failed", result.get("market_regime", {}).get("tone", ""))

    def test_save_called_on_api_success(self):
        """_save_intelligence_briefs IS called when Claude returns valid JSON."""
        valid_response = json.dumps(_VALID_FULL)
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text=valid_response)]
        with patch.object(mb, "_save_intelligence_briefs") as mock_save, \
             patch.object(mb, "_claude") as mock_client, \
             patch.object(mb, "_load_context", return_value="test context"), \
             patch.object(mb, "_load_prev_daily_brief", return_value=None), \
             patch.object(mb, "_load_brief_bearish_max_score", return_value=40), \
             patch.object(mb, "_compute_brief_diff", return_value=[]):
            mock_client.messages.create.return_value = mock_msg
            mb.generate_intelligence_brief()
        mock_save.assert_called_once()


# ── FIX 2 Helper: _is_brief_error_stub ────────────────────────────────────────

class TestIsBriefErrorStub(unittest.TestCase):
    def test_detects_error_stub(self):
        self.assertTrue(mb._is_brief_error_stub(_ERROR_STUB_FULL))

    def test_passes_valid_brief(self):
        self.assertFalse(mb._is_brief_error_stub(_VALID_FULL))

    def test_passes_empty_brief(self):
        self.assertFalse(mb._is_brief_error_stub({}))

    def test_partial_match_fails(self):
        # "Intelligence brief generation" without "failed:" should NOT match
        partial = {"market_regime": {"tone": "Intelligence brief generation succeeded."}}
        self.assertFalse(mb._is_brief_error_stub(partial))


# ── Macro wire: confirmed working — verify live_cache.json is the source ──────

class TestMacroWireSource(unittest.TestCase):
    def test_build_macro_wire_reads_live_cache_not_macro_events(self):
        """build_macro_wire_section reads LIVE_CACHE, not macro_events.json."""
        import inspect
        mw = __import__("macro_wire", fromlist=["build_macro_wire_section", "LIVE_CACHE"])
        # LIVE_CACHE constant must point to live_cache.json
        self.assertIn("live_cache.json", str(mw.LIVE_CACHE))
        # The function should NOT read from macro_events.json
        wire_src = inspect.getsource(mw.build_macro_wire_section)
        self.assertNotIn("macro_events", wire_src)

    def test_macro_wire_section_no_qualifying_returns_fallback(self):
        """build_macro_wire_section returns fallback string when no qualifying articles."""
        from macro_wire import LIVE_CACHE, build_macro_wire_section
        empty_cache = json.dumps({"articles": []})
        with patch.object(
            LIVE_CACHE.__class__, "exists", return_value=True
        ), patch.object(
            LIVE_CACHE.__class__, "read_text", return_value=empty_cache
        ):
            with patch("macro_wire.LIVE_CACHE") as mock_lc:
                mock_lc.exists.return_value = True
                mock_lc.read_text.return_value = empty_cache
                result = build_macro_wire_section()
        self.assertIn("No significant macro headlines", result)


# ── Scratchpad: confirm graceful fallback when result is empty ─────────────────

class TestScratchpadFallback(unittest.TestCase):
    def test_scratchpad_unavailable_string_when_empty(self):
        """Stage 3 prompt injects fallback string when scratchpad_section is empty."""
        # bot_stage3_decision.py line 399:
        # scratchpad_section=scratchpad_section or "  (scratchpad unavailable this cycle)"
        scratchpad_section = ""
        injected = scratchpad_section or "  (scratchpad unavailable this cycle)"
        self.assertIn("scratchpad unavailable", injected)
        self.assertNotIn("Exception", injected)
        self.assertNotIn("Error", injected)

    def test_scratchpad_none_fallback(self):
        scratchpad_section = None
        injected = scratchpad_section or "  (scratchpad unavailable this cycle)"
        self.assertIn("scratchpad unavailable", injected)


if __name__ == "__main__":
    unittest.main()
