"""
tests/test_dashboard_data.py — Dashboard data display fixes (Issues 13, 14, 16).

7 tests:

Issue 13 — stop/TP from position_targets.json:
  1. test_stop_from_position_targets — stop_loss in targets.json → stop field populated
  2. test_tp_from_position_targets — take_profit in targets.json → tp field populated
  3. test_both_stop_tp_from_targets — both fields present → both populated correctly
  4. test_fallback_stop_from_order_map — symbol not in targets → falls back to _stop_map

Issue 14 — allocator canvas JS paren balance:
  5. test_alloc_chart_js_paren_balanced — _alloc_chart_html produces JS with paren depth 0

Issue 16 — equity curve JS paren balance + None handling:
  6. test_equity_chart_js_paren_balanced — _equity_curve_html produces JS with paren depth 0
  7. test_equity_curve_zeros_replaced_with_none — zero equity values become None in dataset
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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


def _paren_depth(js: str) -> int:
    return sum(1 if c == "(" else (-1 if c == ")" else 0) for c in js)


def _brace_depth(js: str) -> int:
    return sum(1 if c == "{" else (-1 if c == "}" else 0) for c in js)


# ── Issue 13 tests ─────────────────────────────────────────────────────────────

class TestPositionTargetsStopTp(unittest.TestCase):
    """Tests 1-4: stop/TP sourced from position_targets.json."""

    def _make_position(self, symbol="AAPL", qty=10.0, price=150.0):
        mv = qty * price
        return SimpleNamespace(
            symbol=symbol,
            qty=str(qty),
            avg_entry_price=str(price),
            current_price=str(price),
            market_value=str(mv),
            unrealized_pl="0",
            unrealized_plpc="0",
        )

    def _make_account(self, equity=100000.0, bp=50000.0):
        return SimpleNamespace(equity=str(equity), buying_power=str(bp))

    def _run_build_status(self, targets_dict: dict, orders=None):
        """Call _build_status with mocked Alpaca and a specific targets file."""
        if _DASH is None:
            self.skipTest("Flask not installed")

        position = self._make_position("AAPL")
        account = self._make_account()

        a1_data = {
            "ok": True, "account": account,
            "positions": [position], "orders": orders or [],
            "buys_today": 0, "sells_today": 0, "recent_orders": [],
        }
        a2_data = {
            "ok": True, "account": account, "positions": [], "recent_orders": [],
        }

        targets_json = json.dumps(targets_dict)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write(targets_json)
            targets_path = f.name

        def _read_text_mock(self, **kw):
            if "position_targets" in str(self):
                return targets_json
            return "{}"

        _patches = [
            patch.object(_DASH, "_alpaca_a1", return_value=a1_data),
            patch.object(_DASH, "_alpaca_a2", return_value=a2_data),
            patch.object(_DASH, "_last_decision", return_value={}),
            patch.object(_DASH, "_todays_trades", return_value=[]),
            patch.object(_DASH, "_recent_errors", return_value=[]),
            patch.object(_DASH, "_morning_brief", return_value={}),
            patch.object(_DASH, "_morning_brief_time", return_value=""),
            patch.object(_DASH, "_morning_brief_mtime_float", return_value=0.0),
            patch.object(_DASH, "_intelligence_brief_full", return_value={}),
            patch.object(_DASH, "_last_n_a1_decisions", return_value=[]),
            patch.object(_DASH, "_last_n_a2_decisions", return_value=[]),
            patch.object(_DASH, "_a2_last_cycle", return_value={}),
            patch.object(_DASH, "_a2_structures", return_value=[]),
            patch.object(_DASH, "_earnings_flags", return_value={}),
            patch.object(_DASH, "_allocator_shadow_compact", return_value=""),
            patch.object(_DASH, "_allocator_chart_data", return_value={}),
            patch.object(_DASH, "_equity_curve_data", return_value={}),
            patch.object(_DASH, "_intraday_bars_a1", return_value={"bars": {}, "label": "today"}),
            patch.object(_DASH, "_load_perf_summary", return_value={}),
            patch.object(_DASH, "_today_pnl_a1", return_value=0.0),
            patch.object(_DASH, "_today_pnl_a2", return_value=0.0),
            patch.object(_DASH, "_spark_a1_equity", return_value=([], [])),
            patch.object(_DASH, "_spark_a2_portfolio_daily", return_value=([], [])),
            patch.object(_DASH, "_spark_claude_cost", return_value=[]),
            patch.object(_DASH, "_a2_pipeline_today", return_value={}),
            patch.object(_DASH, "_build_a2_position_cards", return_value=""),
            patch.object(_DASH, "_a1_top_theses", return_value=[]),
            patch.object(_DASH, "_a2_top_theses", return_value=[]),
            patch.object(_DASH, "_qualitative_context", return_value={}),
            patch.object(_DASH, "_git_hash", return_value="abc1234"),
            patch.object(_DASH, "_service_uptime", return_value="1h"),
            patch.object(Path, "read_text", new=_read_text_mock),
        ]
        try:
            with contextlib.ExitStack() as stack:
                for p in _patches:
                    stack.enter_context(p)
                status = _DASH._build_status()
            return status["positions"]
        finally:
            os.unlink(targets_path)

    def test_stop_from_position_targets(self):
        """Test 1: stop_loss in position_targets.json → stop field is populated."""
        if _DASH is None:
            self.skipTest("Flask not installed")
        targets = {"AAPL": {"symbol": "AAPL", "stop_loss": 140.0, "take_profit": None}}
        positions = self._run_build_status(targets)
        aapl = next((p for p in positions if p["symbol"] == "AAPL"), None)
        self.assertIsNotNone(aapl)
        self.assertEqual(aapl["stop"], 140.0)

    def test_tp_from_position_targets(self):
        """Test 2: take_profit in position_targets.json → tp field is populated."""
        if _DASH is None:
            self.skipTest("Flask not installed")
        targets = {"AAPL": {"symbol": "AAPL", "stop_loss": None, "take_profit": 170.0}}
        positions = self._run_build_status(targets)
        aapl = next((p for p in positions if p["symbol"] == "AAPL"), None)
        self.assertIsNotNone(aapl)
        self.assertEqual(aapl["tp"], 170.0)

    def test_both_stop_tp_from_targets(self):
        """Test 3: both stop_loss and take_profit in targets → both populated correctly."""
        if _DASH is None:
            self.skipTest("Flask not installed")
        targets = {"AAPL": {"symbol": "AAPL", "stop_loss": 138.0, "take_profit": 172.0}}
        positions = self._run_build_status(targets)
        aapl = next((p for p in positions if p["symbol"] == "AAPL"), None)
        self.assertIsNotNone(aapl)
        self.assertEqual(aapl["stop"], 138.0)
        self.assertEqual(aapl["tp"], 172.0)

    def test_fallback_stop_from_order_map(self):
        """Test 4: symbol not in position_targets → falls back to _stop_map from orders."""
        if _DASH is None:
            self.skipTest("Flask not installed")
        # Empty targets — no entry for AAPL
        targets = {}
        # Provide a stop order that _stop_map CAN parse
        stop_order = SimpleNamespace(
            side="sell", type="stop", symbol="AAPL",
            stop_price="142.0", order_type="stop", order_class="simple",
        )
        positions = self._run_build_status(targets, orders=[stop_order])
        aapl = next((p for p in positions if p["symbol"] == "AAPL"), None)
        self.assertIsNotNone(aapl)
        self.assertEqual(aapl["stop"], 142.0)


# ── Issue 14 test ─────────────────────────────────────────────────────────────

@unittest.skipUnless(_FLASK_AVAILABLE, "Flask not installed")
class TestAllocChartJs(unittest.TestCase):
    """Test 5: allocator chart JS has balanced parens after missing ) fix."""

    def test_alloc_chart_js_paren_balanced(self):
        """_alloc_chart_html must produce a <script> block with balanced parens and braces."""
        data = {
            "symbols": ["AAPL", "MSFT", "NVDA"],
            "current_pct": {"AAPL": 5.0, "MSFT": 4.0, "NVDA": 3.0},
            "target_pct": {"AAPL": 15.0, "MSFT": 15.0, "NVDA": 15.0},
            "actions": {"AAPL": "HOLD", "MSFT": "ADD", "NVDA": "TRIM"},
            "ts_label": "2:00 PM ET",
        }
        html = _DASH._alloc_chart_html(data)
        self.assertIn("allocChart", html)
        # Capture the full <script> block so paren/brace counts include addEventListener
        m = re.search(r"<script>.*?</script>", html, re.DOTALL)
        self.assertIsNotNone(m, "script block not found in alloc HTML")
        js = m.group(0)
        self.assertIn("new Chart", js, "Chart call not found in alloc script")
        self.assertEqual(_paren_depth(js), 0, "Paren imbalance in alloc chart JS")
        self.assertEqual(_brace_depth(js), 0, "Brace imbalance in alloc chart JS")


# ── Issue 16 tests ─────────────────────────────────────────────────────────────

@unittest.skipUnless(_FLASK_AVAILABLE, "Flask not installed")
class TestEquityChartJs(unittest.TestCase):
    """Tests 6-7: equity curve chart JS balance and None handling."""

    def _make_equity_data(self, a2_start_zero=True):
        if a2_start_zero:
            return {
                "dates": ["Apr 14", "Apr 15", "Apr 16"],
                "a1": [100000.0, 101000.0, 102000.0],
                "a2": [None, None, 100000.0],
                "combined": [100000.0, 101000.0, 202000.0],
            }
        return {
            "dates": ["May 1", "May 2"],
            "a1": [105000.0, 106000.0],
            "a2": [100000.0, 101000.0],
            "combined": [205000.0, 207000.0],
        }

    def test_equity_chart_js_paren_balanced(self):
        """Test 6: _equity_curve_html must produce a <script> block with balanced parens/braces."""
        html = _DASH._equity_curve_html(self._make_equity_data())
        self.assertIn("equityChart", html)
        m = re.search(r"<script>.*?</script>", html, re.DOTALL)
        self.assertIsNotNone(m, "script block not found in equity HTML")
        js = m.group(0)
        self.assertIn("new Chart", js, "Chart call not found in equity script")
        self.assertEqual(_paren_depth(js), 0, "Paren imbalance in equity chart JS")
        self.assertEqual(_brace_depth(js), 0, "Brace imbalance in equity chart JS")

    def test_equity_curve_zeros_replaced_with_none(self):
        """Test 7: zero equity values become None (Chart.js skips null gaps)."""
        rows = {
            "Apr 14": {"a1": 100000.0, "a2": 0.0, "ts": 1000},
            "Apr 15": {"a1": 101000.0, "a2": 0.0, "ts": 1001},
            "Apr 16": {"a1": 102000.0, "a2": 100000.0, "ts": 1002},
        }
        ordered = sorted(rows.items(), key=lambda x: x[1]["ts"])
        a2_vals = [v["a2"] if v["a2"] > 0 else None for _, v in ordered]
        self.assertIsNone(a2_vals[0], "Zero A2 equity should become None")
        self.assertIsNone(a2_vals[1], "Zero A2 equity should become None")
        self.assertEqual(a2_vals[2], 100000.0, "Non-zero A2 equity should be preserved")
