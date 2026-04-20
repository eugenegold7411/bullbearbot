"""
Isolated test for scratchpad.py — Part 1 verification.

Tests:
  1. run_scratchpad() returns correct keys on valid input (mocked Haiku)
  2. run_scratchpad() degrades to {} on Haiku failure
  3. save_hot_scratchpad() creates and appends to hot memory file
  4. hot memory rolling window trims to _HOT_MEMORY_MAX
  5. get_recent_scratchpads() returns newest-first, respects n cap
  6. format_scratchpad_section() produces expected text for full scratchpad
  7. format_scratchpad_section() returns fallback for empty input
  8. format_hot_memory_section() formats multiple entries without crash
  9. ts / regime_score / vix fields are stamped by run_scratchpad()
 10. run_scratchpad() handles empty signal_scores gracefully
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# ── resolve project root ──────────────────────────────────────────────────────
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import scratchpad as sp

# ── shared fixtures ───────────────────────────────────────────────────────────
_SIGNAL_SCORES = {
    "scored_symbols": {
        "AAPL": {"score": 82, "conviction": "high",   "primary_catalyst": "earnings beat"},
        "NVDA": {"score": 71, "conviction": "medium",  "primary_catalyst": "AI demand"},
        "GLD":  {"score": 45, "conviction": "low",    "primary_catalyst": "safe haven"},
    },
    "top_3":           ["AAPL", "NVDA", "GLD"],
    "elevated_caution": [],
    "reasoning":       "Strong tech momentum.",
}

_REGIME = {
    "regime_score": 65,
    "bias":         "bullish",
    "session_theme": "momentum continuation",
    "constraints":  [],
}

_MARKET_CONDITIONS = {
    "vix":        18.4,
    "vix_regime": "moderate",
}

_MOCK_SP_RESPONSE = {
    "watching":  ["AAPL", "NVDA"],
    "blocking":  ["GLD: no catalyst today"],
    "triggers":  ["AAPL: break above $185", "NVDA: volume > 50M"],
    "conviction_ranking": [
        {"symbol": "AAPL", "conviction": "high",   "notes": "earnings catalyst strong"},
        {"symbol": "NVDA", "conviction": "medium", "notes": "AI theme intact"},
    ],
    "summary": "Bullish tech bias — lean into AAPL and NVDA.",
}


def _mock_claude_response(payload: dict):
    """Build a mock Anthropic messages.create() response returning payload as JSON."""
    resp = MagicMock()
    resp.content = [MagicMock(text=json.dumps(payload))]
    usage = MagicMock()
    usage.input_tokens    = 300
    usage.output_tokens   = 120
    usage.cache_read_input_tokens  = 0
    usage.cache_creation_input_tokens = 0
    resp.usage = usage
    return resp


class TestRunScratchpad(unittest.TestCase):

    def _run_with_mock(self, signal_scores=None, regime=None, md=None, positions=None):
        """Helper: run_scratchpad() with a mocked Haiku response."""
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_claude_response(_MOCK_SP_RESPONSE)
        with patch.object(sp, "_claude", mock_client):
            return sp.run_scratchpad(
                signal_scores    = signal_scores if signal_scores is not None else _SIGNAL_SCORES,
                regime           = regime        if regime        is not None else _REGIME,
                market_conditions= md            if md            is not None else _MARKET_CONDITIONS,
                positions        = positions,
            )

    def test_01_returns_correct_keys(self):
        """run_scratchpad() returns all expected keys."""
        result = self._run_with_mock()
        for key in ("watching", "blocking", "triggers", "conviction_ranking", "summary",
                    "ts", "regime_score", "vix"):
            self.assertIn(key, result, f"Missing key: {key}")

    def test_02_watching_list(self):
        """watching field matches mock response."""
        result = self._run_with_mock()
        self.assertEqual(result["watching"], ["AAPL", "NVDA"])

    def test_03_blocking_list(self):
        """blocking field populated."""
        result = self._run_with_mock()
        self.assertIn("GLD: no catalyst today", result["blocking"])

    def test_04_conviction_ranking(self):
        """conviction_ranking has correct structure."""
        result = self._run_with_mock()
        ranking = result["conviction_ranking"]
        self.assertEqual(ranking[0]["symbol"], "AAPL")
        self.assertEqual(ranking[0]["conviction"], "high")

    def test_05_ts_stamped(self):
        """ts field is ISO 8601 UTC string starting with '20'."""
        result = self._run_with_mock()
        self.assertTrue(result["ts"].startswith("20"), f"Bad ts: {result['ts']}")

    def test_06_regime_score_stamped(self):
        """regime_score is stamped from regime input."""
        result = self._run_with_mock()
        self.assertEqual(result["regime_score"], 65)

    def test_07_vix_stamped(self):
        """vix is stamped from market_conditions input."""
        result = self._run_with_mock()
        self.assertAlmostEqual(result["vix"], 18.4)

    def test_08_degrades_on_api_failure(self):
        """run_scratchpad() returns {} when Haiku raises an exception."""
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = RuntimeError("API down")
        with patch.object(sp, "_claude", mock_client):
            result = sp.run_scratchpad(_SIGNAL_SCORES, _REGIME, _MARKET_CONDITIONS)
        self.assertEqual(result, {})

    def test_09_empty_signal_scores(self):
        """run_scratchpad() handles empty signal_scores without crashing."""
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_claude_response({
            "watching": [], "blocking": [], "triggers": [],
            "conviction_ranking": [],
            "summary": "Insufficient signal data — holding current positions.",
        })
        with patch.object(sp, "_claude", mock_client):
            result = sp.run_scratchpad({}, _REGIME, _MARKET_CONDITIONS)
        self.assertIn("summary", result)
        self.assertIsInstance(result.get("watching"), list)

    def test_10_positions_included_in_user_content(self):
        """Held positions appear in the content sent to Haiku."""
        pos = SimpleNamespace(symbol="TSLA", qty="10", unrealized_plpc="0.03")
        captured = []
        mock_client = MagicMock()
        def capture_call(**kwargs):
            captured.append(kwargs.get("messages", []))
            return _mock_claude_response(_MOCK_SP_RESPONSE)
        mock_client.messages.create.side_effect = capture_call
        with patch.object(sp, "_claude", mock_client):
            sp.run_scratchpad(_SIGNAL_SCORES, _REGIME, _MARKET_CONDITIONS, positions=[pos])
        self.assertTrue(captured, "No Haiku call was made")
        user_msg = captured[0][0]["content"]
        self.assertIn("TSLA", user_msg)


class TestHotMemory(unittest.TestCase):

    def setUp(self):
        """Redirect hot memory to a temp directory for each test."""
        self._tmpdir = tempfile.mkdtemp()
        self._orig_path = sp._HOT_MEMORY_PATH
        sp._HOT_MEMORY_PATH = Path(self._tmpdir) / "test_hot_scratchpads.json"

    def tearDown(self):
        sp._HOT_MEMORY_PATH = self._orig_path

    def _make_sp(self, idx: int) -> dict:
        return {
            "watching": [f"SYM{idx}"],
            "blocking": [],
            "triggers": [],
            "conviction_ranking": [],
            "summary": f"Cycle {idx}",
            "ts":           f"2026-04-14T{idx:02d}:00:00+00:00",
            "regime_score": 50,
            "vix":          15.0,
        }

    def test_11_save_creates_file(self):
        """save_hot_scratchpad() creates the file on first call."""
        sp.save_hot_scratchpad(self._make_sp(1))
        self.assertTrue(sp._HOT_MEMORY_PATH.exists())

    def test_12_save_appends(self):
        """save_hot_scratchpad() appends new entries."""
        sp.save_hot_scratchpad(self._make_sp(1))
        sp.save_hot_scratchpad(self._make_sp(2))
        data = json.loads(sp._HOT_MEMORY_PATH.read_text())
        self.assertEqual(len(data), 2)

    def test_13_rolling_window_trims(self):
        """Rolling window never exceeds _HOT_MEMORY_MAX."""
        for i in range(sp._HOT_MEMORY_MAX + 5):
            sp.save_hot_scratchpad(self._make_sp(i))
        data = json.loads(sp._HOT_MEMORY_PATH.read_text())
        self.assertEqual(len(data), sp._HOT_MEMORY_MAX)

    def test_14_oldest_dropped_when_window_full(self):
        """After overflow, the oldest entries are dropped."""
        for i in range(sp._HOT_MEMORY_MAX + 3):
            sp.save_hot_scratchpad(self._make_sp(i))
        data = json.loads(sp._HOT_MEMORY_PATH.read_text())
        # Most recent _HOT_MEMORY_MAX entries should remain
        # Last entry should be cycle (_HOT_MEMORY_MAX + 2)
        self.assertIn(f"SYM{sp._HOT_MEMORY_MAX + 2}", data[-1].get("watching", []))

    def test_15_get_recent_newest_first(self):
        """get_recent_scratchpads() returns newest-first."""
        for i in range(5):
            sp.save_hot_scratchpad(self._make_sp(i))
        recent = sp.get_recent_scratchpads(3)
        self.assertEqual(len(recent), 3)
        # Most recent is index 4
        self.assertIn("SYM4", recent[0]["watching"])

    def test_16_get_recent_respects_n(self):
        """get_recent_scratchpads(n) returns at most n entries."""
        for i in range(10):
            sp.save_hot_scratchpad(self._make_sp(i))
        self.assertEqual(len(sp.get_recent_scratchpads(2)), 2)

    def test_17_save_noop_on_empty(self):
        """save_hot_scratchpad({}) is a no-op — does not create file."""
        sp.save_hot_scratchpad({})
        self.assertFalse(sp._HOT_MEMORY_PATH.exists())


class TestFormatting(unittest.TestCase):

    _SP = {
        "watching":  ["AAPL", "NVDA"],
        "blocking":  ["VIX elevated — reduce size"],
        "triggers":  ["AAPL: close above $185"],
        "conviction_ranking": [
            {"symbol": "AAPL", "conviction": "high",   "notes": "strong catalyst"},
            {"symbol": "NVDA", "conviction": "medium", "notes": "AI theme"},
        ],
        "summary": "Bullish lean, size cautiously.",
        "ts": "2026-04-14T10:00:00+00:00",
        "regime_score": 65,
        "vix": 18.4,
    }

    def test_18_format_scratchpad_contains_key_fields(self):
        """format_scratchpad_section() includes summary, watching, blocking, triggers, ranking."""
        out = sp.format_scratchpad_section(self._SP)
        self.assertIn("Bullish lean", out)
        self.assertIn("AAPL", out)
        self.assertIn("VIX elevated", out)
        self.assertIn("AAPL: close above $185", out)
        self.assertIn("high", out)

    def test_19_format_scratchpad_empty_fallback(self):
        """format_scratchpad_section({}) returns non-empty fallback string."""
        out = sp.format_scratchpad_section({})
        self.assertIsInstance(out, str)
        self.assertTrue(len(out) > 5)
        self.assertNotIn("AAPL", out)

    def test_20_format_hot_memory_section_no_crash(self):
        """format_hot_memory_section() returns string even with no hot memory."""
        with tempfile.TemporaryDirectory() as tmp:
            orig = sp._HOT_MEMORY_PATH
            sp._HOT_MEMORY_PATH = Path(tmp) / "empty_hot.json"
            out = sp.format_hot_memory_section(3)
            sp._HOT_MEMORY_PATH = orig
        self.assertIsInstance(out, str)
        self.assertTrue(len(out) > 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
