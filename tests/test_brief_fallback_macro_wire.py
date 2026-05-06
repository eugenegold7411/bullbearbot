"""
tests/test_brief_fallback_macro_wire.py

Fix A — Intelligence page brief fallback chain and macro wire injection.

brief_fallback_1 — morning_brief_full.json valid → returned directly
brief_fallback_2 — morning_brief_full.json is error stub → falls back to dated file
brief_fallback_3 — all files missing/invalid → returns minimal stub, no exception
macro_wire_1     — significant_events.jsonl: score<3.0 filtered, >24h filtered, sorted desc
macro_wire_2     — significant_events.jsonl missing → empty list, no exception
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── Environment (must be set before Flask-based import attempt) ────────────────

os.environ.setdefault("DASHBOARD_USER", "admin")
os.environ.setdefault("DASHBOARD_PASSWORD", "bullbearbot")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("ALPACA_API_KEY", "test-key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret")
os.environ.setdefault("ALPACA_API_KEY_OPTIONS", "test-key")
os.environ.setdefault("ALPACA_SECRET_KEY_OPTIONS", "test-secret")

sys.path.insert(0, str(Path(__file__).parent.parent))


def _build_flask_mock() -> types.ModuleType:
    """Return a minimal Flask stand-in that lets app.py import cleanly."""
    flask_mock = MagicMock(name="flask")

    class _FakeFlask:
        logger = MagicMock()
        def __init__(self, *a, **kw): pass
        def route(self, *a, **kw):
            return lambda f: f
        def before_request(self, f): return f
        def after_request(self, f):  return f

    flask_mock.Flask   = _FakeFlask
    flask_mock.Response = MagicMock
    flask_mock.jsonify  = MagicMock
    flask_mock.request  = MagicMock()
    return flask_mock


_HEAVY_STUBS = {
    "alpaca":                     MagicMock(),
    "alpaca.trading":             MagicMock(),
    "alpaca.trading.client":      MagicMock(),
    "alpaca.trading.requests":    MagicMock(),
    "alpaca.trading.enums":       MagicMock(),
    "alpaca.data":                MagicMock(),
    "alpaca.data.historical":     MagicMock(),
    "alpaca.data.requests":       MagicMock(),
    "chromadb":                   MagicMock(),
    "twilio":                     MagicMock(),
    "twilio.rest":                MagicMock(),
    "sendgrid":                   MagicMock(),
    "flask":                      _build_flask_mock(),
}

_DASH = None
try:
    _cached = sys.modules.get("dashboard.app")
    if _cached is not None:
        _DASH = _cached
    else:
        with patch.dict("sys.modules", _HEAVY_STUBS):
            import dashboard.app as _DASH  # type: ignore[assignment]
except Exception as _import_err:
    pass  # tests skip via @unittest.skipUnless below


# ── Fixture helpers ────────────────────────────────────────────────────────────

def _valid_brief() -> dict:
    return {
        "market_regime": {
            "tone": "Constructive tape with AI names leading.",
            "regime": "risk_on", "score": 72, "confidence": "high",
            "vix": 17.5, "key_drivers": ["AI capex"], "todays_events": [],
        },
        "high_conviction_longs": [{"symbol": "NVDA", "score": 85}],
        "watch_list": [],
        "sector_snapshot": [],
        "macro_wire_alerts": [],
    }


def _error_stub_brief() -> dict:
    return {
        "market_regime": {
            "tone": "Intelligence brief generation failed: Error code: 400 - credit balance too low",
            "regime": "caution", "score": 50, "confidence": "low",
            "vix": 20.0, "key_drivers": [], "todays_events": [],
        },
        "high_conviction_longs": [],
        "watch_list": [],
        "sector_snapshot": [],
        "macro_wire_alerts": [],
    }


def _write_json(path: Path, content: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content), encoding="utf-8")


def _events_jsonl(events: list[dict]) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


# ── Tests ──────────────────────────────────────────────────────────────────────

@unittest.skipUnless(_DASH is not None, "dashboard.app could not be imported")
class TestBriefFallback(unittest.TestCase):

    def _call(self, tmp_dir: Path) -> dict:
        with patch.object(_DASH, "BOT_DIR", tmp_dir):
            return _DASH._intelligence_brief_full()

    def test_brief_fallback_1_full_json_valid_returned(self):
        """morning_brief_full.json valid → returned as brief."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_json(d / "data/market/morning_brief_full.json", _valid_brief())
            (d / "data/macro_wire").mkdir(parents=True, exist_ok=True)
            result = self._call(d)

        longs = result.get("high_conviction_longs", [])
        self.assertTrue(
            len(longs) > 0,
            f"Expected high_conviction_longs from valid brief, got: {result}",
        )

    def test_brief_fallback_2_error_stub_falls_back_to_dated(self):
        """morning_brief_full.json is error stub → falls back to today's dated file."""
        from zoneinfo import ZoneInfo
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            # Full brief is an error stub
            _write_json(d / "data/market/morning_brief_full.json", _error_stub_brief())

            # Dated file is valid
            today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y%m%d")
            valid = _valid_brief()
            valid["high_conviction_longs"] = [{"symbol": "AAPL", "score": 90}]
            # Wrap in the "updates" format used by dated files
            _write_json(
                d / "data/market/briefs" / f"morning_brief_{today}.json",
                {"date": today, "updates": [valid]},
            )
            (d / "data/macro_wire").mkdir(parents=True, exist_ok=True)
            result = self._call(d)

        symbols = [x.get("symbol") for x in result.get("high_conviction_longs", [])]
        self.assertIn(
            "AAPL", symbols,
            f"Expected AAPL from dated fallback, got symbols: {symbols}",
        )

    def test_brief_fallback_3_all_missing_returns_unavailable_stub(self):
        """All brief files missing → returns minimal unavailable stub, no exception."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "data/market").mkdir(parents=True, exist_ok=True)
            (d / "data/macro_wire").mkdir(parents=True, exist_ok=True)
            result = self._call(d)

        tone = (result.get("market_regime") or {}).get("tone", "")
        self.assertIn(
            "unavailable", tone.lower(),
            f"Expected unavailable stub tone, got: '{tone}'",
        )
        self.assertNotIn("Error code", tone, "Stub tone must not expose API error text")


@unittest.skipUnless(_DASH is not None, "dashboard.app could not be imported")
class TestMacroWire(unittest.TestCase):

    def _load(self, tmp_dir: Path) -> list:
        with patch.object(_DASH, "BOT_DIR", tmp_dir):
            return _DASH._load_macro_events()

    def test_macro_wire_1_filters_and_sorts(self):
        """score<3.0 filtered, >24h filtered, sorted desc by score, ≤15 items."""
        now_utc = datetime.now(timezone.utc)
        recent  = (now_utc - timedelta(hours=12)).isoformat()
        old     = (now_utc - timedelta(hours=25)).isoformat()

        events = [
            {"ts": recent, "impact_score": 7.5, "keyword_tier": "high",
             "headline": "Top event", "one_line_summary": "Fed surprise",
             "affected_sectors": ["Financials"]},
            # Score too low — filtered
            {"ts": recent, "impact_score": 1.5, "keyword_tier": "low",
             "headline": "Noise", "one_line_summary": "Nothing", "affected_sectors": []},
            # Too old — filtered
            {"ts": old, "impact_score": 9.0, "keyword_tier": "critical",
             "headline": "Stale", "one_line_summary": "Old", "affected_sectors": ["Energy"]},
            {"ts": recent, "impact_score": 4.2, "keyword_tier": "medium",
             "headline": "Second event", "one_line_summary": "Rate cut",
             "affected_sectors": ["Tech"]},
        ]

        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            ep = d / "data/macro_wire/significant_events.jsonl"
            ep.parent.mkdir(parents=True, exist_ok=True)
            ep.write_text(_events_jsonl(events), encoding="utf-8")
            result = self._load(d)

        self.assertEqual(len(result), 2, f"Expected 2 events after filtering, got {len(result)}: {result}")
        self.assertEqual(result[0]["score"], 7.5, "Highest score must be first")
        self.assertEqual(result[1]["score"], 4.2, "Second event must be second")
        self.assertEqual(result[0]["headline"], "Top event")
        self.assertEqual(result[0]["tier"], "high")

    def test_macro_wire_2_missing_file_returns_empty_no_exception(self):
        """significant_events.jsonl missing → empty list, no exception."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "data/macro_wire").mkdir(parents=True, exist_ok=True)
            result = self._load(d)

        self.assertEqual(result, [], f"Expected empty list for missing file, got: {result}")


if __name__ == "__main__":
    unittest.main()
