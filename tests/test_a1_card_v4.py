"""A1 position card v4: 48px polyline sparkline + closed-today section.

Six tests per the v4 dashboard spec:
  1. Sparkline polyline renders with correct format and direction color.
  2. Sparkline renders flat dashed fallback on insufficient data.
  3. Closed today section reads data/trades.jsonl and renders cards with P&L.
  4. Empty trades.jsonl → "No activity recorded today" fallback.
  5. action_type → readable label mapping covers stop / TP / trim / forced.
  6. Action type fallbacks for raw side="sell" entries.
"""

import importlib.util
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

os.environ.setdefault("DASHBOARD_USER", "admin")
os.environ.setdefault("DASHBOARD_PASSWORD", "bullbearbot")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("ALPACA_API_KEY", "test-key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret")

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
app = None  # type: ignore[assignment]
if _FLASK_AVAILABLE:
    with patch.dict("sys.modules", _STUB_MODULES):
        import dashboard.app as app  # type: ignore[assignment]


def _skip_if_no_flask():
    return unittest.skipUnless(_FLASK_AVAILABLE, "flask not installed locally")


@_skip_if_no_flask()
class TestPolylineSparkline(unittest.TestCase):
    def test_polyline_format_and_uptrend_color(self):
        # Rising prices → green polyline
        prices = [100, 101, 102, 103, 104, 105]
        svg = app._pos_polyline_spark(prices)
        self.assertIn('<polyline', svg)
        self.assertIn('points="', svg)
        self.assertIn('stroke="#1D9E75"', svg)  # green
        self.assertIn('width="48"', svg)
        self.assertIn('height="14"', svg)

    def test_polyline_downtrend_color(self):
        prices = [100, 99, 98, 97]
        svg = app._pos_polyline_spark(prices)
        self.assertIn('stroke="#E24B4A"', svg)  # red
        self.assertIn('<polyline', svg)

    def test_polyline_flat_fallback(self):
        # < 2 prices → flat dashed line, NO polyline
        svg = app._pos_polyline_spark([])
        self.assertIn('<svg', svg)
        self.assertIn('stroke-dasharray', svg)
        self.assertNotIn('<polyline', svg)
        svg2 = app._pos_polyline_spark([42.0])
        self.assertIn('stroke-dasharray', svg2)


@_skip_if_no_flask()
class TestClosedTodaySection(unittest.TestCase):
    def setUp(self):
        # Build trades.jsonl with today's fills (in ET) for AMZN exit
        from datetime import datetime
        now_et = datetime.now(app.ET)
        # buy at 9:30 ET, sell at 14:00 ET, both today
        buy_ts = now_et.replace(hour=9, minute=30, second=0, microsecond=0).isoformat()
        sell_ts = now_et.replace(hour=14, minute=0, second=0, microsecond=0).isoformat()
        rows = [
            {"event_type": "filled", "symbol": "AMZN", "side": "buy", "qty": 10.0,
             "fill_price": 250.0, "action_type": "buy", "account": "a1",
             "fill_timestamp": buy_ts, "written_at": buy_ts},
            {"event_type": "filled", "symbol": "AMZN", "side": "sell", "qty": 10.0,
             "fill_price": 260.0, "action_type": "EXIT_LONG_TP", "account": "a1",
             "fill_timestamp": sell_ts, "written_at": sell_ts},
            # GOOGL stop trigger, no buy today (cost basis unknown)
            {"event_type": "filled", "symbol": "GOOGL", "side": "sell", "qty": 5.0,
             "fill_price": 200.0, "action_type": "EXIT_LONG_STOP", "account": "a1",
             "fill_timestamp": sell_ts, "written_at": sell_ts},
            # MSFT trim, with buy today
            {"event_type": "filled", "symbol": "MSFT", "side": "buy", "qty": 20.0,
             "fill_price": 410.0, "action_type": "buy", "account": "a1",
             "fill_timestamp": buy_ts, "written_at": buy_ts},
            {"event_type": "filled", "symbol": "MSFT", "side": "sell", "qty": 8.0,
             "fill_price": 415.0, "action_type": "EXIT_LONG_TRIM", "account": "a1",
             "fill_timestamp": sell_ts, "written_at": sell_ts},
        ]
        self._trades_path = app.BOT_DIR / "data" / "trades.jsonl"
        self._backup_text = self._trades_path.read_text() if self._trades_path.exists() else None
        self._trades_path.parent.mkdir(parents=True, exist_ok=True)
        self._trades_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    def tearDown(self):
        if self._backup_text is not None:
            self._trades_path.write_text(self._backup_text)
        elif self._trades_path.exists():
            self._trades_path.unlink()

    def test_closed_today_data_grouping(self):
        rows = app._a1_closed_today_data()
        syms = {r["symbol"] for r in rows}
        self.assertEqual(syms, {"AMZN", "GOOGL", "MSFT"})
        amzn = next(r for r in rows if r["symbol"] == "AMZN")
        self.assertAlmostEqual(amzn["est_pnl"], (260 - 250) * 10, places=2)
        self.assertEqual(amzn["qty"], 10.0)
        # GOOGL has no buy today → est_pnl is None
        googl = next(r for r in rows if r["symbol"] == "GOOGL")
        self.assertIsNone(googl["est_pnl"])

    def test_closed_today_html_renders_cards(self):
        rows = app._a1_closed_today_data()
        html = app._a1_closed_today_html(rows)
        self.assertIn("Closed Today", html)
        self.assertIn("AMZN", html)
        self.assertIn("GOOGL", html)
        self.assertIn("MSFT", html)
        # action labels
        self.assertIn("take profit", html)
        self.assertIn("stop triggered", html)
        self.assertIn("trimmed", html)
        # net P&L badge present (only AMZN+MSFT had basis)
        self.assertIn("net", html)


@_skip_if_no_flask()
class TestClosedTodayEmpty(unittest.TestCase):
    def setUp(self):
        self._trades_path = app.BOT_DIR / "data" / "trades.jsonl"
        self._backup_text = self._trades_path.read_text() if self._trades_path.exists() else None
        # Wipe — file does not exist
        if self._trades_path.exists():
            self._trades_path.unlink()

    def tearDown(self):
        if self._backup_text is not None:
            self._trades_path.write_text(self._backup_text)

    def test_missing_file_no_crash(self):
        rows = app._a1_closed_today_data()
        self.assertEqual(rows, [])
        html = app._a1_closed_today_html(rows)
        self.assertIn("No activity recorded today", html)


@_skip_if_no_flask()
class TestActionTypeLabels(unittest.TestCase):
    def test_known_labels(self):
        self.assertEqual(app._action_type_label("EXIT_LONG_STOP"), "stop triggered")
        self.assertEqual(app._action_type_label("EXIT_SHORT_STOP"), "stop triggered")
        self.assertEqual(app._action_type_label("EXIT_LONG_TP"), "take profit")
        self.assertEqual(app._action_type_label("EXIT_LONG_TRIM"), "trimmed")
        self.assertEqual(app._action_type_label("EXIT_LONG_FORCED"), "forced exit")
        self.assertEqual(app._action_type_label("COVER"), "covered short")

    def test_fallback_keyword_match(self):
        self.assertEqual(app._action_type_label("EXIT_LONG_HARD_STOP"), "stop triggered")
        self.assertEqual(app._action_type_label("FORCED_LIQUIDATION"), "forced exit")

    def test_raw_sell_default(self):
        # Generic "sell" / empty fall back gracefully
        self.assertEqual(app._action_type_label("sell"), "exited")
        self.assertEqual(app._action_type_label(""), "exited")


if __name__ == "__main__":
    unittest.main()
