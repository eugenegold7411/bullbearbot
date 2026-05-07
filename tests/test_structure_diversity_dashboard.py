"""tests/test_structure_diversity_dashboard.py — Structure Eligibility Matrix (7 tests).

1. test_eligibility_endpoint_returns_data        — GET /api/a2_eligibility → 200, list
2. test_bullish_cheap_iv_shows_call_structures   — bullish + cheap IV → long_call eligible
3. test_neutral_direction_shows_condor           — neutral + iv_rank≥50 → iron_condor eligible
4. test_elevated_vix_strips_single_legs          — VIX elevated → long_call NOT eligible
5. test_high_vix_only_credits                   — VIX high → only credit structures eligible
6. test_held_structure_marked                   — open structure → held field populated
7. test_missing_signal_handled_gracefully        — missing direction key → no crash, neutral
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

os.environ.setdefault("DASHBOARD_USER", "admin")
os.environ.setdefault("DASHBOARD_PASSWORD", "bullbearbot")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("ALPACA_API_KEY", "test-key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret")
os.environ.setdefault("ALPACA_API_KEY_OPTIONS", "test-key")
os.environ.setdefault("ALPACA_SECRET_KEY_OPTIONS", "test-secret")

_STUB_MODULES = {
    "alpaca": MagicMock(),
    "alpaca.trading": MagicMock(),
    "alpaca.trading.client": MagicMock(),
    "alpaca.trading.requests": MagicMock(),
    "alpaca.trading.enums": MagicMock(),
    "alpaca.data": MagicMock(),
    "alpaca.data.historical": MagicMock(),
    "alpaca.data.requests": MagicMock(),
    "chromadb": MagicMock(),
    "twilio": MagicMock(),
    "twilio.rest": MagicMock(),
    "sendgrid": MagicMock(),
}

_FLASK_AVAILABLE = importlib.util.find_spec("flask") is not None

_DASH = None
if _FLASK_AVAILABLE:
    with patch.dict("sys.modules", _STUB_MODULES):
        import dashboard.app as _DASH  # type: ignore[assignment]

_REAL_READ_TEXT = Path.read_text


def _make_file_mock(file_map: dict) -> object:
    """Return a Path.read_text replacement that falls through to real FS for unknown paths."""
    def _mock(self_path, *args, **kwargs):
        p = str(self_path)
        for pattern, content in file_map.items():
            if pattern in p:
                return content
        return _REAL_READ_TEXT(self_path, *args, **kwargs)
    return _mock


def _make_iv_history(current_iv: float, low_iv: float = 0.10, high_iv: float = 0.50,
                     n: int = 25) -> list[dict]:
    step = (high_iv - low_iv) / max(n - 1, 1)
    history = [{"date": f"2026-{i+1:02d}-01",
                "iv": round(low_iv + step * i, 4), "source": "test"}
               for i in range(n)]
    # Override last entry to be current_iv so iv_rank is predictable
    history[-1]["iv"] = current_iv
    return history


@unittest.skipUnless(_FLASK_AVAILABLE, "Flask not installed")
class TestEligibilityEndpoint(unittest.TestCase):
    """Test 1: /api/a2_eligibility returns 200 with a non-empty list."""

    def test_eligibility_endpoint_returns_data(self):
        mock_rows = [
            {
                "symbol": "AAPL",
                "iv_rank": 30.0, "iv_env": "cheap",
                "direction": "bullish", "score": 65.0,
                "vix": 17.5, "vix_regime": "normal",
                "eligible": ["long_call", "debit_call_spread"],
                "blocked": [],
                "held": "",
            }
        ]
        with patch.object(_DASH, "_a2_eligibility_data", return_value=mock_rows):
            client = _DASH.app.test_client()
            resp = client.get("/api/a2_eligibility")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIsInstance(data, list)
        self.assertGreater(len(data), 0)
        row = data[0]
        self.assertIn("symbol", row)
        self.assertIn("eligible", row)
        self.assertIn("blocked", row)
        self.assertIn("held", row)
        self.assertIn("vix_regime", row)


@unittest.skipUnless(_FLASK_AVAILABLE, "Flask not installed")
class TestRoutingLogic(unittest.TestCase):
    """Tests 2-5: _route_eligible_local routing rules (no file I/O)."""

    def test_bullish_cheap_iv_shows_call_structures(self):
        """Test 2: bullish + cheap IV → long_call and debit_call_spread eligible."""
        eligible, blocked = _DASH._route_eligible_local("bullish", "cheap", 25.0, "normal")
        self.assertIn("long_call", eligible)
        self.assertIn("debit_call_spread", eligible)
        self.assertNotIn("long_put", eligible)
        self.assertNotIn("iron_condor", eligible)

    def test_neutral_direction_shows_condor(self):
        """Test 3: neutral + iv_rank≥50 → iron_condor eligible."""
        eligible, blocked = _DASH._route_eligible_local("neutral", "neutral", 55.0, "normal")
        self.assertIn("iron_condor", eligible)
        self.assertNotIn("long_call", eligible)
        self.assertNotIn("long_put", eligible)

    def test_elevated_vix_strips_single_legs(self):
        """Test 4: VIX elevated → long_call stripped; debit_call_spread survives."""
        eligible, blocked = _DASH._route_eligible_local("bullish", "cheap", 25.0, "elevated")
        self.assertNotIn("long_call", eligible)
        self.assertIn("long_call", blocked)
        self.assertIn("elevated", blocked["long_call"])
        self.assertIn("debit_call_spread", eligible)

    def test_high_vix_only_credits(self):
        """Test 5: VIX high → only credit/iron/short_put structures eligible."""
        eligible, blocked = _DASH._route_eligible_local("bullish", "cheap", 25.0, "high")
        for s in eligible:
            self.assertTrue(
                "credit" in s or "iron" in s or s == "short_put",
                f"Non-credit structure eligible under VIX high: {s}",
            )
        self.assertIn("long_call", blocked)
        self.assertIn("debit_call_spread", blocked)


@unittest.skipUnless(_FLASK_AVAILABLE, "Flask not installed")
class TestEligibilityData(unittest.TestCase):
    """Tests 6-7: _a2_eligibility_data with controlled file reads."""

    def _run_eligibility(self, signal_data, vix_data, structs_data, iv_history=None):
        """Run _a2_eligibility_data with patched file reads and iv_rank_local."""
        iv_history = iv_history or _make_iv_history(0.25)
        file_map = {
            "signal_scores": json.dumps(signal_data),
            "vix_cache": json.dumps(vix_data),
            "structures.json": json.dumps(structs_data),
        }
        with patch.object(Path, "read_text", _make_file_mock(file_map)), \
             patch.object(_DASH, "_iv_rank_local", return_value=25.0), \
             patch.object(Path, "exists", return_value=True):
            return _DASH._a2_eligibility_data()

    def test_held_structure_marked(self):
        """Test 6: symbol with open lifecycle → held field populated."""
        signal_data = {"scored_symbols": {"AAPL": {"direction": "bullish", "score": 65.0}}}
        vix_data = {"vix": 17.5}
        structs_data = [{
            "symbol": "AAPL", "lifecycle": "open",
            "structure_type": "debit_call_spread",
        }]
        rows = self._run_eligibility(signal_data, vix_data, structs_data)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "AAPL")
        self.assertEqual(rows[0]["held"], "debit_call_spread")

    def test_missing_signal_handled_gracefully(self):
        """Test 7: symbol with empty signal dict → no crash, direction=neutral, held=''."""
        signal_data = {"scored_symbols": {"TSLA": {}}}
        vix_data = {"vix": 17.5}
        rows = self._run_eligibility(signal_data, vix_data, [])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["symbol"], "TSLA")
        self.assertEqual(row["direction"], "neutral")
        self.assertEqual(row["held"], "")
        self.assertIsInstance(row["eligible"], list)
        self.assertIsInstance(row["blocked"], list)


if __name__ == "__main__":
    unittest.main()
