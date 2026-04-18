"""
Tests for T-018, T-019, T-021 data freshness and fill confirmation fixes.
"""

import json
import logging
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch


# ─── T-018: global_indices.json staleness WARNING ───────────────────────────

def _make_indices_data(age_hours: float) -> dict:
    """Build a minimal global_indices dict with fetched_at set to age_hours ago."""
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    fetched_at = datetime.now(ET) - timedelta(hours=age_hours)
    return {
        "fetched_at": fetched_at.isoformat(),
        "session_status": {"asia": "open", "europe": "closed", "us": "closed"},
        "indices": {
            "^N225": {"name": "Nikkei 225", "ticker": "^N225",
                      "last_price": 38000.0, "chg_pct": 0.5, "prev_price": 37800.0},
        },
    }


def test_stale_global_indices_overnight_logs_warning(caplog):
    """Stale global_indices (> 6h) during overnight session must log a WARNING."""
    import market_data

    stale_data = _make_indices_data(age_hours=8.0)

    with patch.object(market_data.dw, "load_global_indices", return_value=stale_data):
        with caplog.at_level(logging.WARNING, logger="market_data"):
            result = market_data._build_global_session_handoff(session_tier="overnight")

    assert any("stale" in r.message.lower() for r in caplog.records), (
        "Expected WARNING about stale global_indices not found in logs"
    )
    assert "WARNING" in result, (
        "Expected staleness warning text injected into prompt section"
    )


def test_fresh_global_indices_overnight_no_warning(caplog):
    """Fresh global_indices (< 6h) during overnight must NOT log a stale warning."""
    import market_data

    fresh_data = _make_indices_data(age_hours=2.0)

    with patch.object(market_data.dw, "load_global_indices", return_value=fresh_data):
        with caplog.at_level(logging.WARNING, logger="market_data"):
            result = market_data._build_global_session_handoff(session_tier="overnight")

    stale_warnings = [r for r in caplog.records if "stale" in r.message.lower()]
    assert not stale_warnings, f"Unexpected stale warning: {stale_warnings}"
    assert "WARNING" not in result


def test_stale_global_indices_market_session_no_warning(caplog):
    """Stale global_indices during market session should NOT trigger the overnight warning."""
    import market_data

    stale_data = _make_indices_data(age_hours=8.0)

    with patch.object(market_data.dw, "load_global_indices", return_value=stale_data):
        with caplog.at_level(logging.WARNING, logger="market_data"):
            result = market_data._build_global_session_handoff(session_tier="market")

    stale_warnings = [r for r in caplog.records if "stale" in r.message.lower()]
    assert not stale_warnings, "Staleness warning should not fire for market session"


# ─── T-019: morning_brief.json staleness gate ────────────────────────────────

def _make_brief(age_hours: float) -> dict:
    """Build a minimal morning_brief dict with generated_at set to age_hours ago."""
    gen_dt = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    return {
        "market_tone": "bullish",
        "key_themes": ["AI", "earnings"],
        "conviction_picks": [],
        "avoid_today": [],
        "brief_summary": "Markets looking strong.",
        "generated_at": gen_dt.isoformat(),
    }


def test_stale_morning_brief_logs_warning_and_returns_placeholder(caplog):
    """Brief older than 24h must log WARNING and return the placeholder string."""
    import morning_brief

    stale_brief = _make_brief(age_hours=26.0)

    with patch.object(morning_brief, "load_morning_brief", return_value=stale_brief):
        with caplog.at_level(logging.WARNING, logger="morning_brief"):
            result = morning_brief.format_morning_brief_section()

    assert any("stale" in r.message.lower() for r in caplog.records), (
        "Expected WARNING about stale morning brief not found"
    )
    assert "morning brief unavailable" in result.lower(), (
        f"Expected placeholder string, got: {result!r}"
    )
    assert "Markets looking strong" not in result, (
        "Stale content must not be injected into the prompt"
    )


def test_fresh_morning_brief_returns_content(caplog):
    """Brief younger than 24h must be returned normally, no staleness warning."""
    import morning_brief

    fresh_brief = _make_brief(age_hours=4.0)

    with patch.object(morning_brief, "load_morning_brief", return_value=fresh_brief):
        with caplog.at_level(logging.WARNING, logger="morning_brief"):
            result = morning_brief.format_morning_brief_section()

    stale_warns = [r for r in caplog.records if "stale" in r.message.lower()]
    assert not stale_warns, f"Unexpected stale warning: {stale_warns}"
    assert "Markets looking strong" in result


# ─── T-021: fill confirmation events written to trades.jsonl ─────────────────

def test_fill_confirmation_written_to_trades_jsonl(tmp_path, caplog, monkeypatch):
    """
    After a submitted order is polled and found FILLED, a 'filled' event
    must be appended to logs/trades.jsonl with the required fields.
    """
    import order_executor

    # Ensure the module-level dict is clean for this test
    order_executor._pending_fill_checks.clear()

    oid = "test-order-id-filled-001"
    order_executor._pending_fill_checks[oid] = {
        "symbol": "NVDA",
        "action": "buy",
        "qty": 5,
    }

    # Build a mock Alpaca order object with filled status
    mock_order = MagicMock()
    mock_order.status = "filled"
    mock_order.filled_avg_price = 882.50
    mock_order.filled_qty = 5.0
    mock_order.filled_at = "2026-04-18T09:35:00+00:00"

    mock_alpaca = MagicMock()
    mock_alpaca.get_order_by_id.return_value = mock_order

    # Capture log_trade calls
    trade_records: list[dict] = []

    def fake_log_trade(record: dict) -> None:
        trade_records.append(record)

    monkeypatch.setattr(order_executor, "_alpaca", mock_alpaca)
    monkeypatch.setattr(order_executor, "log_trade", fake_log_trade)

    with caplog.at_level(logging.INFO, logger="order_executor"):
        order_executor._check_pending_fills()

    # Order should be removed from pending
    assert oid not in order_executor._pending_fill_checks

    # log_trade should have been called with a filled event
    assert len(trade_records) == 1, f"Expected 1 trade record, got {len(trade_records)}"
    rec = trade_records[0]
    assert rec["event_type"] == "filled"
    assert rec["order_id"] == oid
    assert rec["symbol"] == "NVDA"
    assert rec["fill_price"] == 882.50
    assert rec["fill_qty"] == 5.0

    # INFO log should mention FILLED
    filled_logs = [r for r in caplog.records if "FILLED" in r.message]
    assert filled_logs, "Expected [EXECUTOR] FILLED log message"


def test_cancelled_order_written_to_trades_jsonl(monkeypatch):
    """Cancelled orders must produce a 'cancelled' event in log_trade."""
    import order_executor

    order_executor._pending_fill_checks.clear()

    oid = "test-order-id-cancelled-002"
    order_executor._pending_fill_checks[oid] = {
        "symbol": "TSM",
        "action": "buy",
        "qty": 10,
    }

    mock_order = MagicMock()
    mock_order.status = "canceled"
    mock_order.filled_avg_price = None
    mock_order.filled_qty = None
    mock_order.filled_at = None

    mock_alpaca = MagicMock()
    mock_alpaca.get_order_by_id.return_value = mock_order

    trade_records: list[dict] = []

    def fake_log_trade(record: dict) -> None:
        trade_records.append(record)

    monkeypatch.setattr(order_executor, "_alpaca", mock_alpaca)
    monkeypatch.setattr(order_executor, "log_trade", fake_log_trade)

    order_executor._check_pending_fills()

    assert oid not in order_executor._pending_fill_checks
    assert len(trade_records) == 1
    rec = trade_records[0]
    assert rec["event_type"] == "cancelled"
    assert rec["order_id"] == oid
    assert rec["symbol"] == "TSM"


def test_fill_check_non_fatal_on_api_error(monkeypatch):
    """API failure during fill check must not raise — order stays pending for next cycle."""
    import order_executor

    order_executor._pending_fill_checks.clear()

    oid = "test-order-id-error-003"
    order_executor._pending_fill_checks[oid] = {
        "symbol": "GLD",
        "action": "buy",
        "qty": 2,
    }

    mock_alpaca = MagicMock()
    mock_alpaca.get_order_by_id.side_effect = RuntimeError("Alpaca API timeout")

    monkeypatch.setattr(order_executor, "_alpaca", mock_alpaca)

    # Must not raise
    order_executor._check_pending_fills()

    # Order stays in pending (will retry next cycle)
    assert oid in order_executor._pending_fill_checks
