"""TZ-01 through TZ-06 — ET_OFFSET → ZoneInfo migration tests for dashboard/app.py.

Verifies that _now_et(), _is_market_hours(), and the ZoneInfo bar-fetch UTC
conversion pattern behave correctly in both EDT (summer) and EST (winter).
"""

import importlib
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

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

_DASH = None
if _FLASK_AVAILABLE:
    with patch.dict("sys.modules", _STUB_MODULES):
        import dashboard.app as _DASH  # type: ignore[assignment]

# Fixed UTC instants used across tests.
# Both are 14:00 UTC on a Monday → 10:00 AM ET, well inside market hours.
_SUMMER_UTC = datetime(2026, 7, 7, 14, 0, 0, tzinfo=timezone.utc)   # EDT −4 → 10:00 AM
_WINTER_UTC = datetime(2027, 1, 4, 15, 0, 0, tzinfo=timezone.utc)   # EST −5 → 10:00 AM

_ET = ZoneInfo("America/New_York")


def _make_fake_now(base_utc: datetime):
    """Return a side_effect for datetime.now that handles optional tz kwarg."""
    def _fake(tz=None):
        return base_utc.astimezone(tz) if tz is not None else base_utc
    return _fake


@unittest.skipUnless(_FLASK_AVAILABLE, "Flask not installed")
class TestNowEt(unittest.TestCase):
    """TZ-01, TZ-02 — _now_et() returns correct ET wall-clock time."""

    def _call(self, base_utc):
        with patch("dashboard.app.datetime") as mock_dt:
            mock_dt.now.side_effect = _make_fake_now(base_utc)
            mock_dt.fromisoformat = datetime.fromisoformat
            return _DASH._now_et()

    def test_summer_et_time(self):
        """TZ-01: 14:00 UTC in July = 10:00 AM EDT."""
        result = self._call(_SUMMER_UTC)
        self.assertIn("10:00:00 AM ET", result)

    def test_winter_et_time(self):
        """TZ-02: 15:00 UTC in January = 10:00 AM EST."""
        result = self._call(_WINTER_UTC)
        self.assertIn("10:00:00 AM ET", result)


@unittest.skipUnless(_FLASK_AVAILABLE, "Flask not installed")
class TestIsMarketHours(unittest.TestCase):
    """TZ-03, TZ-04 — _is_market_hours() returns True at 10:00 AM ET on a weekday."""

    def _call(self, base_utc):
        with patch("dashboard.app.datetime") as mock_dt:
            mock_dt.now.side_effect = _make_fake_now(base_utc)
            mock_dt.fromisoformat = datetime.fromisoformat
            return _DASH._is_market_hours()

    def test_summer_open(self):
        """TZ-03: Monday 10:00 AM EDT is market hours."""
        self.assertTrue(self._call(_SUMMER_UTC))

    def test_winter_open(self):
        """TZ-04: Monday 10:00 AM EST is market hours."""
        self.assertTrue(self._call(_WINTER_UTC))


class TestZoneInfoMarketOpenUtc(unittest.TestCase):
    """TZ-05, TZ-06 — ZoneInfo conversion gives correct market-open UTC in each season.

    These tests exercise the exact pattern used in the bar-fetch window code:
        market_open_et = now_et.replace(hour=9, minute=30, ...)
        market_open_utc = market_open_et.astimezone(timezone.utc)

    They do not depend on Flask being installed.
    """

    def _compute(self, base_utc):
        now_et = base_utc.astimezone(_ET)
        market_open_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        return market_open_et.astimezone(timezone.utc)

    def test_summer_market_open_utc(self):
        """TZ-05: EDT (−4) market open 9:30 ET = 13:30 UTC."""
        result = self._compute(_SUMMER_UTC)
        self.assertEqual(result.hour, 13)
        self.assertEqual(result.minute, 30)

    def test_winter_market_open_utc(self):
        """TZ-06: EST (−5) market open 9:30 ET = 14:30 UTC."""
        result = self._compute(_WINTER_UTC)
        self.assertEqual(result.hour, 14)
        self.assertEqual(result.minute, 30)


if __name__ == "__main__":
    unittest.main()
