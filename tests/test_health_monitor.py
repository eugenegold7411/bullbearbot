"""Tests for health_monitor.py — 7 runtime health checks."""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytz

sys.path.insert(0, str(Path(__file__).parent.parent))

import health_monitor
from health_monitor import (
    CheckResult,
    _check_a1_churn,
    _check_a1_cycle,
    _check_a2_cycle,
    _check_a2_fill_rate,
    _check_a2_stuck_structures,
    _check_equity_drawdown,
    _check_modes,
    _dispatch_alert,
    _mark_alerted,
    _should_alert,
    get_health_status,
    run_health_checks,
)

_ET = pytz.timezone("America/New_York")


def _market_time(hour=10, minute=0) -> datetime:
    return datetime(2026, 5, 5, hour, minute, 0, tzinfo=_ET)


def _outside_time() -> datetime:
    return datetime(2026, 5, 5, 7, 0, 0, tzinfo=_ET)


def _ts(delta_min: float = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(minutes=delta_min)
    return dt.isoformat()


def _mock_data_dir(content: str | None = None, exists: bool = True) -> MagicMock:
    """Return a mock for _DATA_DIR where any 2-level path chain returns a controlled leaf."""
    leaf = MagicMock()
    leaf.exists.return_value = exists
    if content is not None:
        leaf.read_text.return_value = content
    mock_dir = MagicMock()
    mock_dir.__truediv__.return_value.__truediv__.return_value = leaf
    return mock_dir


def _mock_data_dir_3(content: str | None = None, exists: bool = True) -> MagicMock:
    """Return a mock for _DATA_DIR where any 3-level path chain returns a controlled leaf."""
    leaf = MagicMock()
    leaf.exists.return_value = exists
    if content is not None:
        leaf.read_text.return_value = content
    mock_dir = MagicMock()
    mock_dir.__truediv__.return_value.__truediv__.return_value.__truediv__.return_value = leaf
    return mock_dir


# ---------------------------------------------------------------------------
# Check 1 — A1 cycle freshness
# ---------------------------------------------------------------------------

class TestA1Cycle(unittest.TestCase):
    def test_stale_cycle_is_critical(self):
        records = [{"ts": _ts(20), "actions": []}]
        with patch("health_monitor._DATA_DIR", _mock_data_dir(json.dumps(records))):
            r = _check_a1_cycle(_market_time())
        self.assertFalse(r.ok)
        self.assertEqual(r.severity, "CRITICAL")

    def test_fresh_cycle_is_ok(self):
        records = [{"ts": _ts(5), "actions": []}]
        with patch("health_monitor._DATA_DIR", _mock_data_dir(json.dumps(records))):
            r = _check_a1_cycle(_market_time())
        self.assertTrue(r.ok)

    def test_outside_market_hours_ok(self):
        r = _check_a1_cycle(_outside_time())
        self.assertTrue(r.ok)
        self.assertIn("outside", r.message)

    def test_empty_decisions_is_critical(self):
        with patch("health_monitor._DATA_DIR", _mock_data_dir(json.dumps([]))):
            r = _check_a1_cycle(_market_time())
        self.assertFalse(r.ok)
        self.assertEqual(r.severity, "CRITICAL")

    def test_missing_file_is_critical(self):
        with patch("health_monitor._DATA_DIR", _mock_data_dir(exists=False)):
            r = _check_a1_cycle(_market_time())
        self.assertFalse(r.ok)
        self.assertEqual(r.severity, "CRITICAL")


# ---------------------------------------------------------------------------
# Check 2 — A2 cycle freshness
# ---------------------------------------------------------------------------

class TestA2Cycle(unittest.TestCase):
    def test_stale_a2_is_critical(self):
        lines = [json.dumps({"caller": "run_options_cycle", "checked_at": _ts(20)})]
        with patch("health_monitor._DATA_DIR", _mock_data_dir("\n".join(lines))):
            r = _check_a2_cycle(_market_time())
        self.assertFalse(r.ok)
        self.assertEqual(r.severity, "CRITICAL")

    def test_fresh_a2_is_ok(self):
        lines = [json.dumps({"caller": "run_options_cycle", "checked_at": _ts(3)})]
        with patch("health_monitor._DATA_DIR", _mock_data_dir("\n".join(lines))):
            r = _check_a2_cycle(_market_time())
        self.assertTrue(r.ok)

    def test_no_a2_entries_is_critical(self):
        lines = [json.dumps({"caller": "something_else", "checked_at": _ts(2)})]
        with patch("health_monitor._DATA_DIR", _mock_data_dir("\n".join(lines))):
            r = _check_a2_cycle(_market_time())
        self.assertFalse(r.ok)
        self.assertEqual(r.severity, "CRITICAL")

    def test_outside_market_hours_ok(self):
        r = _check_a2_cycle(_outside_time())
        self.assertTrue(r.ok)


# ---------------------------------------------------------------------------
# Check 3 — A2 fill rate
# ---------------------------------------------------------------------------

class TestA2FillRate(unittest.TestCase):
    def _make_order(self, status_str: str):
        from alpaca.trading.enums import OrderStatus
        o = MagicMock()
        o.status = OrderStatus(status_str)
        return o

    def test_many_submitted_zero_filled_is_critical(self):
        orders = [self._make_order("new")] * 5
        with patch.dict("os.environ", {"ALPACA_API_KEY_OPTIONS": "k", "ALPACA_SECRET_KEY_OPTIONS": "s"}):
            with patch("alpaca.trading.client.TradingClient") as mock_tc:
                mock_tc.return_value.get_orders.return_value = orders
                r = _check_a2_fill_rate(_market_time())
        self.assertFalse(r.ok)
        self.assertEqual(r.severity, "CRITICAL")

    def test_some_filled_is_ok(self):
        from alpaca.trading.enums import OrderStatus
        orders = [MagicMock(status=OrderStatus.NEW)] * 2 + [MagicMock(status=OrderStatus.FILLED)] * 3
        with patch.dict("os.environ", {"ALPACA_API_KEY_OPTIONS": "k", "ALPACA_SECRET_KEY_OPTIONS": "s"}):
            with patch("alpaca.trading.client.TradingClient") as mock_tc:
                mock_tc.return_value.get_orders.return_value = orders
                r = _check_a2_fill_rate(_market_time())
        self.assertTrue(r.ok)

    def test_below_threshold_submitted_is_ok(self):
        from alpaca.trading.enums import OrderStatus
        orders = [MagicMock(status=OrderStatus.NEW)] * 2
        with patch.dict("os.environ", {"ALPACA_API_KEY_OPTIONS": "k", "ALPACA_SECRET_KEY_OPTIONS": "s"}):
            with patch("alpaca.trading.client.TradingClient") as mock_tc:
                mock_tc.return_value.get_orders.return_value = orders
                r = _check_a2_fill_rate(_market_time())
        self.assertTrue(r.ok)

    def test_no_creds_skips(self):
        with patch.dict("os.environ", {}, clear=True):
            r = _check_a2_fill_rate(_market_time())
        self.assertTrue(r.ok)
        self.assertIn("not configured", r.message)

    def test_outside_market_hours_ok(self):
        r = _check_a2_fill_rate(_outside_time())
        self.assertTrue(r.ok)


# ---------------------------------------------------------------------------
# Check 4 — A1 churn
# ---------------------------------------------------------------------------

class TestA1Churn(unittest.TestCase):
    def _run(self, records, now_et=None):
        with patch("health_monitor._DATA_DIR", _mock_data_dir(json.dumps(records))):
            return _check_a1_churn(now_et or _market_time())

    def test_churn_detected(self):
        records = [
            {"ts": _ts(10), "actions": [{"action": "buy", "symbol": "AAPL"}]},
            {"ts": _ts(8), "actions": [{"action": "sell", "symbol": "AAPL"}]},
        ]
        r = self._run(records)
        self.assertFalse(r.ok)
        self.assertEqual(r.severity, "CRITICAL")
        self.assertIn("AAPL", r.message)

    def test_buy_only_no_churn(self):
        records = [{"ts": _ts(i), "actions": [{"action": "buy", "symbol": "MSFT"}]}
                   for i in range(1, 7)]
        r = self._run(records)
        self.assertTrue(r.ok)

    def test_different_symbols_no_churn(self):
        records = [
            {"ts": _ts(5), "actions": [{"action": "buy", "symbol": "AAPL"}]},
            {"ts": _ts(3), "actions": [{"action": "sell", "symbol": "MSFT"}]},
        ]
        r = self._run(records)
        self.assertTrue(r.ok)

    def test_outside_market_hours_ok(self):
        r = self._run([], now_et=_outside_time())
        self.assertTrue(r.ok)


# ---------------------------------------------------------------------------
# Check 5 — Operating modes
# ---------------------------------------------------------------------------

class TestModes(unittest.TestCase):
    def test_a1_halted_is_critical(self):
        from divergence import OperatingMode
        mock_a1 = MagicMock()
        mock_a2 = MagicMock()
        mock_a1.mode = OperatingMode.HALTED
        mock_a2.mode = OperatingMode.NORMAL

        with patch("divergence.load_account_mode", side_effect=lambda acc: mock_a1 if acc == "A1" else mock_a2):
            r = _check_modes()
        self.assertFalse(r.ok)
        self.assertEqual(r.severity, "CRITICAL")
        self.assertIn("A1", r.message)

    def test_a2_halted_is_critical(self):
        from divergence import OperatingMode
        mock_a1 = MagicMock()
        mock_a2 = MagicMock()
        mock_a1.mode = OperatingMode.NORMAL
        mock_a2.mode = OperatingMode.HALTED

        with patch("divergence.load_account_mode", side_effect=lambda acc: mock_a1 if acc == "A1" else mock_a2):
            r = _check_modes()
        self.assertFalse(r.ok)
        self.assertIn("A2", r.message)

    def test_both_normal_is_ok(self):
        from divergence import OperatingMode
        mock_a1 = MagicMock()
        mock_a2 = MagicMock()
        mock_a1.mode = OperatingMode.NORMAL
        mock_a2.mode = OperatingMode.NORMAL

        with patch("divergence.load_account_mode", side_effect=lambda acc: mock_a1 if acc == "A1" else mock_a2):
            r = _check_modes()
        self.assertTrue(r.ok)

    def test_runs_outside_market_hours(self):
        """_check_modes has no market-hours gate."""
        from divergence import OperatingMode
        mock_a1 = MagicMock()
        mock_a2 = MagicMock()
        mock_a1.mode = OperatingMode.HALTED
        mock_a2.mode = OperatingMode.NORMAL

        with patch("divergence.load_account_mode", side_effect=lambda acc: mock_a1 if acc == "A1" else mock_a2):
            r = _check_modes()
        self.assertFalse(r.ok)


# ---------------------------------------------------------------------------
# Check 6 — Equity drawdown
# ---------------------------------------------------------------------------

class TestEquityDrawdown(unittest.TestCase):
    def _run_with_equity(self, equity: float, last_equity: float):
        mock_account = MagicMock()
        mock_account.equity = str(equity)
        mock_account.last_equity = str(last_equity)
        with patch.dict("os.environ", {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"}):
            with patch("alpaca.trading.client.TradingClient") as mock_tc:
                mock_tc.return_value.get_account.return_value = mock_account
                return _check_equity_drawdown(_market_time())

    def test_down_4pct_is_critical(self):
        r = self._run_with_equity(96000, 100000)
        self.assertFalse(r.ok)
        self.assertEqual(r.severity, "CRITICAL")

    def test_down_2pct_is_ok(self):
        r = self._run_with_equity(98000, 100000)
        self.assertTrue(r.ok)

    def test_flat_is_ok(self):
        r = self._run_with_equity(100000, 100000)
        self.assertTrue(r.ok)

    def test_outside_market_hours_ok(self):
        r = _check_equity_drawdown(_outside_time())
        self.assertTrue(r.ok)


# ---------------------------------------------------------------------------
# Check 7 — Stuck structures
# ---------------------------------------------------------------------------

class TestStuckStructures(unittest.TestCase):
    def _run(self, records):
        with patch("health_monitor._DATA_DIR", _mock_data_dir_3(json.dumps(records))):
            return _check_a2_stuck_structures(_market_time())

    def test_stuck_submitted_is_warning(self):
        opened = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        records = [{"structure_id": "s1", "lifecycle": "SUBMITTED", "opened_at": opened}]
        r = self._run(records)
        self.assertFalse(r.ok)
        self.assertEqual(r.severity, "WARNING")
        self.assertIn("s1", r.message)

    def test_fresh_submitted_is_ok(self):
        opened = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        records = [{"structure_id": "s2", "lifecycle": "SUBMITTED", "opened_at": opened}]
        r = self._run(records)
        self.assertTrue(r.ok)

    def test_fully_filled_not_stuck(self):
        opened = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        records = [{"structure_id": "s3", "lifecycle": "FULLY_FILLED", "opened_at": opened}]
        r = self._run(records)
        self.assertTrue(r.ok)

    def test_no_file_is_ok(self):
        with patch("health_monitor._DATA_DIR", _mock_data_dir_3(exists=False)):
            r = _check_a2_stuck_structures(_market_time())
        self.assertTrue(r.ok)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

class TestDeduplication(unittest.TestCase):
    def test_within_30min_no_alert(self):
        state = {}
        _mark_alerted(state, "test_check")
        self.assertFalse(_should_alert(state, "test_check"))

    def test_after_30min_should_alert(self):
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
        state = {"alert_test_check": old_ts}
        self.assertTrue(_should_alert(state, "alert_test_check"))

    def test_new_key_should_alert(self):
        self.assertTrue(_should_alert({}, "brand_new_check"))

    def test_dispatch_suppressed_within_window(self):
        state = {}
        _mark_alerted(state, "alert_a1_cycle")
        result = CheckResult(name="a1_cycle", ok=False, severity="CRITICAL", message="test")
        with patch.object(health_monitor, "_send_whatsapp") as mock_wa:
            with patch.object(health_monitor, "_send_email") as mock_email:
                _dispatch_alert(result, state, dry_run=False)
                mock_wa.assert_not_called()
                mock_email.assert_not_called()


# ---------------------------------------------------------------------------
# Alert dispatch routing
# ---------------------------------------------------------------------------

class TestDispatchRouting(unittest.TestCase):
    def test_critical_sends_both(self):
        state = {}
        result = CheckResult(name="a1_cycle", ok=False, severity="CRITICAL", message="stale")
        with patch.object(health_monitor, "_send_whatsapp") as mock_wa:
            with patch.object(health_monitor, "_send_email") as mock_email:
                _dispatch_alert(result, state, dry_run=False)
                mock_wa.assert_called_once()
                mock_email.assert_called_once()

    def test_warning_sends_whatsapp_only(self):
        state = {}
        result = CheckResult(name="a2_stuck_structures", ok=False, severity="WARNING", message="stuck")
        with patch.object(health_monitor, "_send_whatsapp") as mock_wa:
            with patch.object(health_monitor, "_send_email") as mock_email:
                _dispatch_alert(result, state, dry_run=False)
                mock_wa.assert_called_once()
                mock_email.assert_not_called()

    def test_dry_run_no_sends(self):
        state = {}
        result = CheckResult(name="a1_cycle", ok=False, severity="CRITICAL", message="stale")
        with patch.object(health_monitor, "_send_whatsapp") as mock_wa:
            with patch.object(health_monitor, "_send_email") as mock_email:
                _dispatch_alert(result, state, dry_run=True)
                mock_wa.assert_not_called()
                mock_email.assert_not_called()


# ---------------------------------------------------------------------------
# run_health_checks / get_health_status
# ---------------------------------------------------------------------------

class TestRunHealthChecks(unittest.TestCase):
    def test_dry_run_returns_list(self):
        ok_result = CheckResult(name="a1_cycle", ok=True, severity="OK", message="ok")
        with patch.object(health_monitor, "_run_all_checks", return_value=[ok_result]):
            with patch.object(health_monitor, "_load_state", return_value={}):
                with patch.object(health_monitor, "_save_state"):
                    results = run_health_checks(dry_run=True)
        self.assertIsInstance(results, list)
        self.assertEqual(len(results), 1)

    def test_failing_check_triggers_dispatch(self):
        failing = CheckResult(name="a1_cycle", ok=False, severity="CRITICAL", message="stale")
        with patch.object(health_monitor, "_run_all_checks", return_value=[failing]):
            with patch.object(health_monitor, "_load_state", return_value={}):
                with patch.object(health_monitor, "_save_state"):
                    with patch.object(health_monitor, "_dispatch_alert") as mock_disp:
                        run_health_checks(dry_run=True)
                        mock_disp.assert_called_once()

    def test_get_health_status_all_ok(self):
        ok_result = CheckResult(name="a1_cycle", ok=True, severity="OK", message="ok")
        with patch.object(health_monitor, "_run_all_checks", return_value=[ok_result]):
            status = get_health_status()
        self.assertIn("all_ok", status)
        self.assertIn("checks", status)
        self.assertIn("checked_at", status)
        self.assertTrue(status["all_ok"])

    def test_get_health_status_failing(self):
        failing = CheckResult(name="a1_cycle", ok=False, severity="CRITICAL", message="down")
        with patch.object(health_monitor, "_run_all_checks", return_value=[failing]):
            status = get_health_status()
        self.assertFalse(status["all_ok"])
        self.assertEqual(status["checks"][0]["severity"], "CRITICAL")


if __name__ == "__main__":
    unittest.main()
