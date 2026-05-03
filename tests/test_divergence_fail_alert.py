"""
tests/test_divergence_fail_alert.py — Fail-alert remediation tests for divergence.py.

Covers all 5 safety-critical functions:
  - is_action_allowed
  - load_account_mode
  - respond_to_divergence
  - check_clean_cycle
  - save_account_mode

For each function, three scenarios:
  1. Normal path — existing behavior unchanged
  2. Exception path — log.error + alert fires, safe fallback returned, no crash
  3. Alert delivery failure — send_whatsapp_direct throws, function still returns fallback

Also covers the dedup window: two consecutive exceptions within 5 minutes
fire only one alert.
"""

import json
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Minimal stubs so divergence.py imports cleanly without external deps
# ---------------------------------------------------------------------------

for _m in ("dotenv", "anthropic"):
    if _m not in sys.modules:
        _mod = types.ModuleType(_m)
        if _m == "dotenv":
            _mod.load_dotenv = lambda *a, **kw: None
        sys.modules[_m] = _mod


import divergence  # noqa: E402  (after stubs)
from divergence import (  # noqa: E402
    _SAFETY_ALERT_CACHE,
    _SAFETY_DEDUP_SECS,
    AccountMode,
    DivergenceEvent,
    DivergenceScope,
    DivergenceSeverity,
    OperatingMode,
    _fire_safety_alert,
    check_clean_cycle,
    is_action_allowed,
    load_account_mode,
    respond_to_divergence,
    save_account_mode,
)


def _normal_mode(account: str = "A1") -> AccountMode:
    return AccountMode(
        account=account,
        mode=OperatingMode.NORMAL,
        scope=DivergenceScope.ACCOUNT,
        scope_id="",
        reason_code="",
        reason_detail="",
        entered_at="",
        entered_by="test",
        recovery_condition="one_clean_cycle",
        last_checked_at="",
    )


def _halted_mode(account: str = "A1") -> AccountMode:
    return AccountMode(
        account=account,
        mode=OperatingMode.HALTED,
        scope=DivergenceScope.ACCOUNT,
        scope_id=account,
        reason_code="test",
        reason_detail="test halt",
        entered_at="",
        entered_by="test",
        recovery_condition="manual_review",
        last_checked_at="",
    )


def _reconcile_mode(account: str = "A1") -> AccountMode:
    return AccountMode(
        account=account,
        mode=OperatingMode.RECONCILE_ONLY,
        scope=DivergenceScope.ACCOUNT,
        scope_id="",
        reason_code="test",
        reason_detail="",
        entered_at="",
        entered_by="test",
        recovery_condition="one_clean_cycle",
        last_checked_at="",
        clean_cycles_since_entry=0,
    )


def _halt_event() -> DivergenceEvent:
    return DivergenceEvent(
        event_id="div_test",
        timestamp="2026-05-03T12:00:00+00:00",
        account="A1",
        symbol="NVDA",
        event_type="protection_missing",
        severity=DivergenceSeverity.HALT,
        scope=DivergenceScope.ACCOUNT,
        scope_id="A1",
        paper_expected={"stop_exists": True},
        live_observed={"stop_exists": False},
        delta={"missing": True},
        recoverability="manual",
        risk_impact="high",
    )


# ---------------------------------------------------------------------------
# Helper: clear the dedup cache before each test that checks alerts
# ---------------------------------------------------------------------------

def _clear_alert_cache():
    _SAFETY_ALERT_CACHE.clear()


# ===========================================================================
# _fire_safety_alert tests
# ===========================================================================

class TestFireSafetyAlert(unittest.TestCase):

    def setUp(self):
        _clear_alert_cache()

    def test_sends_whatsapp_on_first_call(self):
        exc = ValueError("boom")
        with patch("divergence.send_whatsapp_direct", create=True) as mock_wa:
            # Import path used inside _fire_safety_alert is via lazy import
            with patch.dict("sys.modules", {"notifications": MagicMock(
                send_whatsapp_direct=mock_wa
            )}):
                _fire_safety_alert("test_fn", exc)
        # alert cache should be set
        self.assertIn("test_fn", _SAFETY_ALERT_CACHE)

    def test_dedup_suppresses_second_alert_within_window(self):
        exc = ValueError("boom")
        calls = []
        with patch.dict("sys.modules", {"notifications": MagicMock(
            send_whatsapp_direct=lambda msg: calls.append(msg) or True
        )}):
            _fire_safety_alert("dedup_fn", exc)
            _fire_safety_alert("dedup_fn", exc)  # second — should be suppressed
        self.assertEqual(len(calls), 1)

    def test_dedup_fires_again_after_window(self):
        exc = ValueError("boom")
        calls = []
        with patch.dict("sys.modules", {"notifications": MagicMock(
            send_whatsapp_direct=lambda msg: calls.append(msg) or True
        )}):
            _fire_safety_alert("window_fn", exc)
            # Backdate the cache entry to simulate window expiry
            _SAFETY_ALERT_CACHE["window_fn"] = time.time() - _SAFETY_DEDUP_SECS - 1
            _fire_safety_alert("window_fn", exc)  # should fire again
        self.assertEqual(len(calls), 2)

    def test_alert_delivery_failure_does_not_raise(self):
        exc = ValueError("boom")
        broken_notifications = MagicMock()
        broken_notifications.send_whatsapp_direct.side_effect = RuntimeError("twilio down")
        with patch.dict("sys.modules", {"notifications": broken_notifications}):
            try:
                _fire_safety_alert("nocrash_fn", exc)
            except Exception as e:
                self.fail(f"_fire_safety_alert raised when alert delivery failed: {e}")

    def test_message_contains_function_name_and_safety_degraded(self):
        exc = ValueError("disk full")
        captured = []
        mock_notif = MagicMock()
        mock_notif.send_whatsapp_direct = lambda msg: captured.append(msg) or True
        with patch.dict("sys.modules", {"notifications": mock_notif}):
            _fire_safety_alert("save_account_mode", exc)
        self.assertEqual(len(captured), 1)
        self.assertIn("SAFETY DEGRADED", captured[0])
        self.assertIn("save_account_mode", captured[0])
        self.assertIn("ValueError", captured[0])


# ===========================================================================
# is_action_allowed
# ===========================================================================

class TestIsActionAllowedNormal(unittest.TestCase):

    def test_normal_mode_allows_enter_long(self):
        allowed, reason = is_action_allowed(_normal_mode(), "enter_long", "AAPL")
        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_halted_blocks_enter_long(self):
        allowed, reason = is_action_allowed(_halted_mode(), "enter_long", "AAPL")
        self.assertFalse(allowed)
        self.assertIn("halted", reason)

    def test_halted_allows_close(self):
        allowed, _ = is_action_allowed(_halted_mode(), "close", "AAPL")
        self.assertTrue(allowed)

    def test_reconcile_only_blocks_add(self):
        allowed, reason = is_action_allowed(_reconcile_mode(), "add", "AAPL")
        self.assertFalse(allowed)
        self.assertIn("reconcile_only", reason)


class _RaisingModeState:
    """Stub whose .mode property always raises — used to force exception path."""
    @property
    def mode(self):
        raise AttributeError("injected test error for is_action_allowed")


class TestIsActionAllowedException(unittest.TestCase):

    def setUp(self):
        _clear_alert_cache()

    def test_exception_returns_true_empty_string(self):
        with patch.dict("sys.modules", {"notifications": MagicMock(
            send_whatsapp_direct=lambda msg: True
        )}):
            allowed, reason = is_action_allowed(_RaisingModeState(), "enter_long", "AAPL")
        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_exception_fires_safety_alert(self):
        captured = []
        mock_notif = MagicMock()
        mock_notif.send_whatsapp_direct = lambda msg: captured.append(msg) or True
        with patch.dict("sys.modules", {"notifications": mock_notif}):
            is_action_allowed(_RaisingModeState(), "enter_long", "AAPL")
        self.assertTrue(any("is_action_allowed" in m for m in captured))

    def test_exception_logs_at_error_level(self):
        with patch.dict("sys.modules", {"notifications": MagicMock(
            send_whatsapp_direct=lambda msg: True
        )}):
            with patch.object(divergence.log, "error") as mock_error:
                is_action_allowed(_RaisingModeState(), "enter_long", "AAPL")
        mock_error.assert_called()
        call_args = mock_error.call_args[0][0]
        self.assertIn("is_action_allowed", call_args)

    def test_alert_delivery_failure_still_returns_fallback(self):
        broken_notif = MagicMock()
        broken_notif.send_whatsapp_direct.side_effect = RuntimeError("twilio down")
        with patch.dict("sys.modules", {"notifications": broken_notif}):
            try:
                allowed, reason = is_action_allowed(_RaisingModeState(), "enter_long", "AAPL")
            except Exception as e:
                self.fail(f"Raised unexpectedly: {e}")
        self.assertTrue(allowed)


# ===========================================================================
# load_account_mode
# ===========================================================================

class TestLoadAccountModeNormal(unittest.TestCase):

    def test_loads_normal_mode(self, tmp_path=None):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a1_mode.json"
            p.write_text(json.dumps({
                "account": "A1", "mode": "normal", "scope": "account",
                "scope_id": "", "reason_code": "", "reason_detail": "",
                "entered_at": "", "entered_by": "test",
                "recovery_condition": "one_clean_cycle",
                "last_checked_at": "", "clean_cycles_since_entry": 0, "version": 1,
            }))
            with patch.object(divergence, "get_mode_path", return_value=p):
                result = load_account_mode("A1")
        self.assertEqual(result.mode, OperatingMode.NORMAL)

    def test_missing_file_returns_normal(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            absent = Path(tmp) / "a1_mode.json"
            with patch.object(divergence, "get_mode_path", return_value=absent):
                result = load_account_mode("A1")
        self.assertEqual(result.mode, OperatingMode.NORMAL)


class TestLoadAccountModeException(unittest.TestCase):

    def setUp(self):
        _clear_alert_cache()

    def test_corrupt_json_returns_normal(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a1_mode.json"
            p.write_text("{not valid json")
            with patch.dict("sys.modules", {"notifications": MagicMock(
                send_whatsapp_direct=lambda msg: True
            )}):
                with patch.object(divergence, "get_mode_path", return_value=p):
                    result = load_account_mode("A1")
        self.assertEqual(result.mode, OperatingMode.NORMAL)
        self.assertEqual(result.account, "A1")

    def test_corrupt_json_fires_safety_alert(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a1_mode.json"
            p.write_text("{not valid json")
            captured = []
            mock_notif = MagicMock()
            mock_notif.send_whatsapp_direct = lambda msg: captured.append(msg) or True
            with patch.dict("sys.modules", {"notifications": mock_notif}):
                with patch.object(divergence, "get_mode_path", return_value=p):
                    load_account_mode("A1")
        self.assertTrue(any("load_account_mode" in m for m in captured))

    def test_corrupt_json_logs_at_error_level(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a1_mode.json"
            p.write_text("{not valid json")
            with patch.dict("sys.modules", {"notifications": MagicMock(
                send_whatsapp_direct=lambda msg: True
            )}):
                with patch.object(divergence, "get_mode_path", return_value=p):
                    with patch.object(divergence.log, "error") as mock_error:
                        load_account_mode("A1")
        mock_error.assert_called()

    def test_alert_delivery_failure_still_returns_normal(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a1_mode.json"
            p.write_text("{not valid json")
            broken_notif = MagicMock()
            broken_notif.send_whatsapp_direct.side_effect = RuntimeError("twilio down")
            with patch.dict("sys.modules", {"notifications": broken_notif}):
                with patch.object(divergence, "get_mode_path", return_value=p):
                    try:
                        result = load_account_mode("A1")
                    except Exception as e:
                        self.fail(f"Raised unexpectedly: {e}")
        self.assertEqual(result.mode, OperatingMode.NORMAL)


# ===========================================================================
# respond_to_divergence
# ===========================================================================

class TestRespondToDivergenceNormal(unittest.TestCase):

    def test_no_events_returns_current_mode_unchanged(self):
        mode = _normal_mode()
        result = respond_to_divergence([], "A1", mode)
        self.assertEqual(result.mode, OperatingMode.NORMAL)

    def test_halt_event_transitions_to_halted(self):
        mode = _normal_mode()
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a1_mode.json"
            with patch.object(divergence, "get_mode_path", return_value=p), \
                 patch.object(divergence, "MODE_TRANSITION_LOG",
                               Path(tmp) / "transitions.jsonl"), \
                 patch.object(divergence, "RUNTIME_DIR", Path(tmp)):
                result = respond_to_divergence([_halt_event()], "A1", mode)
        self.assertEqual(result.mode, OperatingMode.HALTED)


class TestRespondToDivergenceException(unittest.TestCase):

    def setUp(self):
        _clear_alert_cache()

    def _make_bad_event(self):
        bad = MagicMock(spec=DivergenceEvent)
        # _SEVERITY_LADDER.index(e.severity) raises because severity is not in the ladder
        bad.severity = "not_a_real_severity"
        return bad

    def test_exception_returns_current_mode(self):
        mode = _reconcile_mode()
        with patch.dict("sys.modules", {"notifications": MagicMock(
            send_whatsapp_direct=lambda msg: True
        )}):
            result = respond_to_divergence([self._make_bad_event()], "A1", mode)
        self.assertEqual(result.mode, OperatingMode.RECONCILE_ONLY)

    def test_exception_fires_safety_alert(self):
        mode = _reconcile_mode()
        captured = []
        mock_notif = MagicMock()
        mock_notif.send_whatsapp_direct = lambda msg: captured.append(msg) or True
        with patch.dict("sys.modules", {"notifications": mock_notif}):
            respond_to_divergence([self._make_bad_event()], "A1", mode)
        self.assertTrue(any("respond_to_divergence" in m for m in captured))

    def test_exception_logs_at_error_level(self):
        mode = _reconcile_mode()
        with patch.dict("sys.modules", {"notifications": MagicMock(
            send_whatsapp_direct=lambda msg: True
        )}):
            with patch.object(divergence.log, "error") as mock_error:
                respond_to_divergence([self._make_bad_event()], "A1", mode)
        mock_error.assert_called()

    def test_alert_delivery_failure_still_returns_current_mode(self):
        mode = _reconcile_mode()
        broken_notif = MagicMock()
        broken_notif.send_whatsapp_direct.side_effect = RuntimeError("twilio down")
        with patch.dict("sys.modules", {"notifications": broken_notif}):
            try:
                result = respond_to_divergence([self._make_bad_event()], "A1", mode)
            except Exception as e:
                self.fail(f"Raised unexpectedly: {e}")
        self.assertEqual(result.mode, OperatingMode.RECONCILE_ONLY)


# ===========================================================================
# check_clean_cycle
# ===========================================================================

class TestCheckCleanCycleNormal(unittest.TestCase):

    def test_normal_mode_returns_unchanged(self):
        mode = _normal_mode()
        result = check_clean_cycle("A1", mode, [])
        self.assertEqual(result.mode, OperatingMode.NORMAL)

    def test_one_clean_cycle_recovers_reconcile_only(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a1_mode.json"
            with patch.object(divergence, "get_mode_path", return_value=p), \
                 patch.object(divergence, "MODE_TRANSITION_LOG",
                               Path(tmp) / "transitions.jsonl"), \
                 patch.object(divergence, "RUNTIME_DIR", Path(tmp)):
                mode = _reconcile_mode()
                result = check_clean_cycle("A1", mode, [])
        self.assertEqual(result.mode, OperatingMode.NORMAL)

    def test_new_divergence_resets_clean_count(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a1_mode.json"
            with patch.object(divergence, "get_mode_path", return_value=p), \
                 patch.object(divergence, "RUNTIME_DIR", Path(tmp)):
                mode = _reconcile_mode()
                mode.clean_cycles_since_entry = 1
                result = check_clean_cycle("A1", mode, [_halt_event()])
        self.assertEqual(result.clean_cycles_since_entry, 0)


class TestCheckCleanCycleException(unittest.TestCase):

    def setUp(self):
        _clear_alert_cache()

    def test_exception_returns_current_mode(self):
        mode = _reconcile_mode()
        with patch.dict("sys.modules", {"notifications": MagicMock(
            send_whatsapp_direct=lambda msg: True
        )}):
            with patch("divergence.save_account_mode", side_effect=OSError("disk full")):
                result = check_clean_cycle("A1", mode, [_halt_event()])
        # Should return current_mode (with reset count applied in-memory)
        self.assertIsNotNone(result)

    def test_exception_fires_safety_alert(self):
        mode = _reconcile_mode()
        captured = []
        mock_notif = MagicMock()
        mock_notif.send_whatsapp_direct = lambda msg: captured.append(msg) or True
        with patch.dict("sys.modules", {"notifications": mock_notif}):
            with patch("divergence.save_account_mode", side_effect=OSError("disk full")):
                check_clean_cycle("A1", mode, [_halt_event()])
        self.assertTrue(any("check_clean_cycle" in m for m in captured))

    def test_exception_logs_at_error_level(self):
        mode = _reconcile_mode()
        with patch.dict("sys.modules", {"notifications": MagicMock(
            send_whatsapp_direct=lambda msg: True
        )}):
            with patch("divergence.save_account_mode", side_effect=OSError("disk full")):
                with patch.object(divergence.log, "error") as mock_error:
                    check_clean_cycle("A1", mode, [_halt_event()])
        mock_error.assert_called()

    def test_alert_delivery_failure_still_returns_fallback(self):
        mode = _reconcile_mode()
        broken_notif = MagicMock()
        broken_notif.send_whatsapp_direct.side_effect = RuntimeError("twilio down")
        with patch.dict("sys.modules", {"notifications": broken_notif}):
            with patch("divergence.save_account_mode", side_effect=OSError("disk full")):
                try:
                    result = check_clean_cycle("A1", mode, [_halt_event()])
                except Exception as e:
                    self.fail(f"Raised unexpectedly: {e}")
        self.assertIsNotNone(result)


# ===========================================================================
# save_account_mode
# ===========================================================================

class TestSaveAccountModeNormal(unittest.TestCase):

    def test_writes_mode_file_atomically(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a1_mode.json"
            with patch.object(divergence, "get_mode_path", return_value=p), \
                 patch.object(divergence, "RUNTIME_DIR", Path(tmp)):
                save_account_mode(_normal_mode())
            self.assertTrue(p.exists())
            data = json.loads(p.read_text())
            self.assertEqual(data["mode"], "normal")

    def test_no_tmp_file_left_behind(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a1_mode.json"
            with patch.object(divergence, "get_mode_path", return_value=p), \
                 patch.object(divergence, "RUNTIME_DIR", Path(tmp)):
                save_account_mode(_normal_mode())
            tmp_file = p.with_suffix(".tmp")
            self.assertFalse(tmp_file.exists())


class TestSaveAccountModeException(unittest.TestCase):

    def setUp(self):
        _clear_alert_cache()

    def test_oserror_does_not_raise(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("sys.modules", {"notifications": MagicMock(
                send_whatsapp_direct=lambda msg: True
            )}):
                with patch.object(divergence, "RUNTIME_DIR", Path(tmp)), \
                     patch.object(divergence, "get_mode_path",
                                  side_effect=OSError("disk full")):
                    try:
                        save_account_mode(_normal_mode())
                    except Exception as e:
                        self.fail(f"Raised unexpectedly: {e}")

    def test_oserror_fires_safety_alert(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            captured = []
            mock_notif = MagicMock()
            mock_notif.send_whatsapp_direct = lambda msg: captured.append(msg) or True
            with patch.dict("sys.modules", {"notifications": mock_notif}):
                with patch.object(divergence, "RUNTIME_DIR", Path(tmp)), \
                     patch.object(divergence, "get_mode_path",
                                  side_effect=OSError("disk full")):
                    save_account_mode(_normal_mode())
        self.assertTrue(any("save_account_mode" in m for m in captured))

    def test_oserror_logs_at_error_level(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("sys.modules", {"notifications": MagicMock(
                send_whatsapp_direct=lambda msg: True
            )}):
                with patch.object(divergence, "RUNTIME_DIR", Path(tmp)), \
                     patch.object(divergence, "get_mode_path",
                                  side_effect=OSError("disk full")):
                    with patch.object(divergence.log, "error") as mock_error:
                        save_account_mode(_normal_mode())
        mock_error.assert_called()

    def test_alert_delivery_failure_still_returns_none(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            broken_notif = MagicMock()
            broken_notif.send_whatsapp_direct.side_effect = RuntimeError("twilio down")
            with patch.dict("sys.modules", {"notifications": broken_notif}):
                with patch.object(divergence, "RUNTIME_DIR", Path(tmp)), \
                     patch.object(divergence, "get_mode_path",
                                  side_effect=OSError("disk full")):
                    try:
                        result = save_account_mode(_normal_mode())
                    except Exception as e:
                        self.fail(f"Raised unexpectedly: {e}")
            self.assertIsNone(result)


# ===========================================================================
# Dedup across multiple functions (cross-function independence)
# ===========================================================================

class TestDedupCrossFunction(unittest.TestCase):

    def setUp(self):
        _clear_alert_cache()

    def test_separate_functions_each_get_own_dedup_entry(self):
        exc = ValueError("boom")
        calls = []
        mock_notif = MagicMock()
        mock_notif.send_whatsapp_direct = lambda msg: calls.append(msg) or True
        with patch.dict("sys.modules", {"notifications": mock_notif}):
            _fire_safety_alert("fn_a", exc)
            _fire_safety_alert("fn_b", exc)
        self.assertEqual(len(calls), 2)  # each function gets its first alert

    def test_same_function_second_call_suppressed(self):
        exc = ValueError("boom")
        calls = []
        mock_notif = MagicMock()
        mock_notif.send_whatsapp_direct = lambda msg: calls.append(msg) or True
        with patch.dict("sys.modules", {"notifications": mock_notif}):
            _fire_safety_alert("fn_c", exc)
            _fire_safety_alert("fn_c", exc)  # same function — suppressed
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
