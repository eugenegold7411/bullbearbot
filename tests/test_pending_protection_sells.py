"""QW2 / #52 — scheduler picks up pending protection sells at 9:30 AM ET."""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import scheduler  # noqa: E402

ET = ZoneInfo("America/New_York")


@pytest.fixture
def isolate_pending_sells(tmp_path, monkeypatch):
    """Redirect _PENDING_PROTECTION_SELLS_PATH and reset the run-date sentinel."""
    p = tmp_path / "pending_protection_sells.json"
    monkeypatch.setattr(scheduler, "_PENDING_PROTECTION_SELLS_PATH", p)
    monkeypatch.setattr(scheduler, "_pending_protection_sells_run_date", "")
    return p


def _freeze_time(monkeypatch, when: datetime) -> None:
    """Patch scheduler.datetime so .now(tz) returns `when`."""
    real_dt = datetime

    class _FrozenDateTime(real_dt):
        @classmethod
        def now(cls, tz=None):
            return when if tz is None else when.astimezone(tz)

    monkeypatch.setattr(scheduler, "datetime", _FrozenDateTime)


def _at_open() -> datetime:
    # Tuesday 2026-05-12, 9:30 AM ET
    return datetime(2026, 5, 12, 9, 30, tzinfo=ET)


def _at_8am() -> datetime:
    return datetime(2026, 5, 12, 8, 0, tzinfo=ET)


def test_pending_sells_executed_at_open(isolate_pending_sells, monkeypatch):
    isolate_pending_sells.write_text(json.dumps({
        "AAPL": {
            "qty":            100,
            "market_status":  "pending_open",
            "reason":         "repair_failed_market_closed",
            "flagged_at":     "2026-05-11T19:00:00",
        }
    }))

    mock_order = MagicMock()
    mock_order.id = "order-123"
    mock_client = MagicMock()
    mock_client.submit_order.return_value = mock_order

    monkeypatch.setattr(scheduler.bot, "_get_alpaca", lambda: mock_client)
    _freeze_time(monkeypatch, _at_open())

    scheduler._maybe_execute_pending_protection_sells(dry_run=False)

    assert mock_client.submit_order.called
    data = json.loads(isolate_pending_sells.read_text())
    assert data["AAPL"]["market_status"] == "sell_placed"
    assert data["AAPL"]["order_id"] == "order-123"


def test_pending_sells_skipped_outside_window(isolate_pending_sells, monkeypatch):
    isolate_pending_sells.write_text(json.dumps({
        "AAPL": {"qty": 100, "market_status": "pending_open"}
    }))

    mock_client = MagicMock()
    monkeypatch.setattr(scheduler.bot, "_get_alpaca", lambda: mock_client)
    _freeze_time(monkeypatch, _at_8am())

    scheduler._maybe_execute_pending_protection_sells(dry_run=False)

    assert not mock_client.submit_order.called
    # File untouched
    data = json.loads(isolate_pending_sells.read_text())
    assert data["AAPL"]["market_status"] == "pending_open"


def test_pending_sells_file_missing(isolate_pending_sells, monkeypatch):
    """No file → silent no-op, no Alpaca interaction, no crash."""
    assert not isolate_pending_sells.exists()

    mock_client = MagicMock()
    monkeypatch.setattr(scheduler.bot, "_get_alpaca", lambda: mock_client)
    _freeze_time(monkeypatch, _at_open())

    scheduler._maybe_execute_pending_protection_sells(dry_run=False)

    assert not mock_client.submit_order.called


def test_pending_sells_already_placed_skipped(isolate_pending_sells, monkeypatch):
    """Entries with status sell_placed must NOT be re-executed."""
    isolate_pending_sells.write_text(json.dumps({
        "AAPL": {
            "qty":           100,
            "market_status": "sell_placed",
            "order_id":      "old-order",
        }
    }))

    mock_client = MagicMock()
    monkeypatch.setattr(scheduler.bot, "_get_alpaca", lambda: mock_client)
    _freeze_time(monkeypatch, _at_open())

    scheduler._maybe_execute_pending_protection_sells(dry_run=False)

    assert not mock_client.submit_order.called
    data = json.loads(isolate_pending_sells.read_text())
    assert data["AAPL"]["order_id"] == "old-order"
