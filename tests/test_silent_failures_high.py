"""
tests/test_silent_failures_high.py

22 test cases for HIGH severity silent failure remediation (#9–#17):
  SF09  options_state.save_structure       — OSError alert + dedup
  SF10  trade_memory.save_trade_memory     — exception alert + returns ""
  SF11  order_executor._check_pending_fills — stuck eviction + alert
  SF12  exit_manager TP submission         — 40310000 filter + returns True
  SF13  reconciliation A1/A2 excepts       — alerts on failure, continues
  SF14  divergence transition_mode_audit_log — alert fires, mode unaffected
  SF15  options_executor close_structure   — leg alert + market-close CANCELLED
  SF16  options_executor._emergency_close_leg — alert fires
  SF17  attribution.log_attribution_event  — outer alert; spine-only no-alert
"""

import sys
import time
import types
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Install notifications stub so _fire_safety_alert lazy import succeeds
# ---------------------------------------------------------------------------
if "notifications" not in sys.modules:
    _notif = types.ModuleType("notifications")
    _notif.send_whatsapp_direct = lambda msg: True  # type: ignore[attr-defined]
    sys.modules["notifications"] = _notif

# ---------------------------------------------------------------------------
# Import target modules (conftest stubs already installed by pytest)
# ---------------------------------------------------------------------------
import attribution  # noqa: E402
import divergence  # noqa: E402
import exit_manager  # noqa: E402
import options_executor  # noqa: E402
import options_state  # noqa: E402
import order_executor  # noqa: E402
import reconciliation  # noqa: E402
import trade_memory  # noqa: E402
from schemas import (  # noqa: E402
    OptionsLeg,
    OptionsStructure,
    OptionStrategy,
    StructureLifecycle,
    Tier,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _clear_all_caches() -> None:
    for mod in (
        options_state, trade_memory, order_executor, exit_manager,
        reconciliation, options_executor, attribution, divergence,
    ):
        if hasattr(mod, "_SAFETY_ALERT_CACHE"):
            mod._SAFETY_ALERT_CACHE.clear()


def _make_leg(
    occ: str,
    side: str = "buy",
    filled_price: float | None = 1.0,
) -> OptionsLeg:
    return OptionsLeg(
        occ_symbol=occ,
        underlying="SPY",
        side=side,
        qty=1,
        option_type="call",
        strike=400.0,
        expiration="2026-06-20",
        filled_price=filled_price,
    )


def _make_single_call() -> OptionsStructure:
    return OptionsStructure(
        structure_id="test-single-001",
        underlying="SPY",
        strategy=OptionStrategy.SINGLE_CALL,
        lifecycle=StructureLifecycle.FULLY_FILLED,
        legs=[_make_leg("SPY230620C00400000", side="buy", filled_price=1.50)],
        contracts=1,
        max_cost_usd=150.0,
        opened_at="2026-05-03T14:00:00Z",
        catalyst="test",
        tier=Tier.DYNAMIC,
    )


def _make_credit_spread() -> OptionsStructure:
    """Two-leg call credit spread: short call (sell) + long call hedge (buy)."""
    return OptionsStructure(
        structure_id="test-credit-001",
        underlying="SPY",
        strategy=OptionStrategy.CALL_CREDIT_SPREAD,
        lifecycle=StructureLifecycle.FULLY_FILLED,
        legs=[
            _make_leg("SPY230620C00400000", side="sell", filled_price=2.50),
            _make_leg("SPY230620C00410000", side="buy",  filled_price=1.20),
        ],
        contracts=1,
        max_cost_usd=130.0,
        opened_at="2026-05-03T14:00:00Z",
        catalyst="test",
        tier=Tier.DYNAMIC,
    )


# ===========================================================================
# SF09 — options_state.save_structure
# ===========================================================================

class TestSF09SaveStructure(unittest.TestCase):

    def setUp(self) -> None:
        _clear_all_caches()

    def test_sf09_normal_no_alert(self) -> None:
        with patch("options_state._write_atomic"), \
             patch("options_state._load_raw", return_value=[]), \
             patch("options_state._ensure_dir"), \
             patch("notifications.send_whatsapp_direct") as mock_wa:
            options_state.save_structure(_make_single_call())
        mock_wa.assert_not_called()

    def test_sf09_oserror_fires_alert_and_reraises(self) -> None:
        with patch("options_state._write_atomic", side_effect=OSError("disk full")), \
             patch("options_state._load_raw", return_value=[]), \
             patch("options_state._ensure_dir"), \
             patch("notifications.send_whatsapp_direct") as mock_wa:
            with self.assertRaises(OSError):
                options_state.save_structure(_make_single_call())
        mock_wa.assert_called_once()
        msg = mock_wa.call_args[0][0]
        assert "options_state.save_structure" in msg

    def test_sf09_dedup_fires_only_once(self) -> None:
        with patch("options_state._write_atomic", side_effect=OSError("disk full")), \
             patch("options_state._load_raw", return_value=[]), \
             patch("options_state._ensure_dir"), \
             patch("notifications.send_whatsapp_direct") as mock_wa:
            for _ in range(2):
                with self.assertRaises(OSError):
                    options_state.save_structure(_make_single_call())
        mock_wa.assert_called_once()


# ===========================================================================
# SF10 — trade_memory.save_trade_memory
# ===========================================================================

class TestSF10SaveTradeMemory(unittest.TestCase):

    def setUp(self) -> None:
        _clear_all_caches()

    def test_sf10_normal_no_alert(self) -> None:
        # chromadb is intentionally not stubbed — _get_collections returns (None, None, None)
        # → early return before the exception block; no alert should fire
        with patch("notifications.send_whatsapp_direct") as mock_wa:
            result = trade_memory.save_trade_memory({}, {}, "standard")
        assert result == ""
        mock_wa.assert_not_called()

    def test_sf10_exception_fires_alert_returns_empty(self) -> None:
        short = MagicMock()
        short.add.side_effect = RuntimeError("vector store error")
        with patch("trade_memory._get_collections", return_value=(short, None, None)), \
             patch("trade_memory._maybe_promote_aged_records"), \
             patch("notifications.send_whatsapp_direct") as mock_wa:
            result = trade_memory.save_trade_memory({}, {}, "standard")
        assert result == ""
        mock_wa.assert_called_once()
        assert "trade_memory.save_trade_memory" in mock_wa.call_args[0][0]

    def test_sf10_notification_failure_still_returns_empty(self) -> None:
        short = MagicMock()
        short.add.side_effect = RuntimeError("vector store error")
        with patch("trade_memory._get_collections", return_value=(short, None, None)), \
             patch("trade_memory._maybe_promote_aged_records"), \
             patch("notifications.send_whatsapp_direct", side_effect=RuntimeError("wa down")):
            result = trade_memory.save_trade_memory({}, {}, "standard")
        assert result == ""


# ===========================================================================
# SF11 — order_executor._check_pending_fills
# ===========================================================================

class TestSF11PendingFills(unittest.TestCase):

    def setUp(self) -> None:
        _clear_all_caches()
        order_executor._pending_fill_checks.clear()

    def tearDown(self) -> None:
        order_executor._pending_fill_checks.clear()

    def _register(self, oid: str, age_secs: float) -> None:
        order_executor._pending_fill_checks[oid] = {
            "symbol":        "AAPL",
            "action":        "buy",
            "qty":           10,
            "alert_deferred": False,
            "registered_at": time.time() - age_secs,
        }

    def test_sf11_stuck_order_evicted_after_10min(self) -> None:
        self._register("OID-001", 601)
        with patch("order_executor._get_alpaca") as mock_alpaca, \
             patch("notifications.send_whatsapp_direct"):
            mock_alpaca.return_value.get_order_by_id.side_effect = RuntimeError("timeout")
            order_executor._check_pending_fills()
        assert "OID-001" not in order_executor._pending_fill_checks

    def test_sf11_not_evicted_before_10min(self) -> None:
        self._register("OID-002", 60)
        with patch("order_executor._get_alpaca") as mock_alpaca, \
             patch("notifications.send_whatsapp_direct"):
            mock_alpaca.return_value.get_order_by_id.side_effect = RuntimeError("timeout")
            order_executor._check_pending_fills()
        assert "OID-002" in order_executor._pending_fill_checks

    def test_sf11_alert_fires_on_exception(self) -> None:
        self._register("OID-003", 60)
        with patch("order_executor._get_alpaca") as mock_alpaca, \
             patch("notifications.send_whatsapp_direct") as mock_wa:
            mock_alpaca.return_value.get_order_by_id.side_effect = RuntimeError("timeout")
            order_executor._check_pending_fills()
        mock_wa.assert_called_once()
        assert "order_executor._check_pending_fills" in mock_wa.call_args[0][0]


# ===========================================================================
# SF12 — exit_manager TP submission
# ===========================================================================

class TestSF12TpSubmission(unittest.TestCase):

    def setUp(self) -> None:
        _clear_all_caches()

    def _run_tp_path(self, tp_exc_msg: str) -> bool:
        """
        Drive _refresh_exits_locked to the TP submission block with a stop that
        succeeds and a TP that raises the given message.  Returns the bool result.
        """
        # Ensure OrderClass.OCO is defined on whatever stub is in sys.modules so
        # the OCO fast path is taken consistently across isolated and full-suite runs.
        _enums = sys.modules.get("alpaca.trading.enums")
        if _enums and hasattr(_enums, "OrderClass"):
            if not hasattr(_enums.OrderClass, "OCO"):
                setattr(_enums.OrderClass, "OCO", "oco")

        mock_client = MagicMock()
        stop_order  = MagicMock(id="stop-001")
        # _refresh_exits_locked now tries OCO first (submit_order call #1), falls
        # back on failure, then places standalone stop (#2) and standalone TP (#3).
        mock_client.submit_order.side_effect = [
            Exception("OCO rejected in test"),  # OCO fast path fails → fallback
            stop_order,                          # standalone stop succeeds
            Exception(tp_exc_msg),               # standalone TP raises
        ]

        ei     = {"status": "unprotected", "stop_price": None}
        em_cfg = {"refresh_if_stop_stale_pct": 0.05}

        with patch("exit_manager.generate_exit_plan",
                   return_value={"stop_loss": 390.0, "take_profit": 420.0}), \
             patch("exit_manager.log_trade"), \
             patch("exit_manager._is_crypto", return_value=False):
            return exit_manager._refresh_exits_locked(
                MagicMock(qty="10"),
                mock_client,
                {},
                "medium",
                ei,
                "SPY",
                10.0,
                400.0,
                em_cfg,
            )

    def test_sf12_non_40310000_fires_alert(self) -> None:
        with patch("notifications.send_whatsapp_direct") as mock_wa:
            self._run_tp_path("generic broker error")
        mock_wa.assert_called()
        assert "refresh_exits_tp_submission" in mock_wa.call_args[0][0]

    def test_sf12_40310000_no_alert(self) -> None:
        with patch("notifications.send_whatsapp_direct") as mock_wa:
            self._run_tp_path("error 40310000 OCA lock")
        mock_wa.assert_not_called()

    def test_sf12_returns_true_when_tp_fails(self) -> None:
        with patch("notifications.send_whatsapp_direct"):
            result = self._run_tp_path("generic broker error")
        assert result is True


# ===========================================================================
# SF13 — reconciliation A1/A2
# ===========================================================================

class TestSF13Reconciliation(unittest.TestCase):

    def setUp(self) -> None:
        _clear_all_caches()

    def test_sf13_a1_fires_alert_on_failure(self) -> None:
        action = reconciliation.ReconciliationAction(
            priority="NORMAL", symbol="AAPL", action_type="close_all",
            reason="test", qty=10.0,
        )
        with patch("reconciliation._close_position", side_effect=RuntimeError("broker error")), \
             patch("notifications.send_whatsapp_direct") as mock_wa:
            results = reconciliation.execute_reconciliation_plan(
                [action], alpaca_client=MagicMock()
            )
        assert any("ERROR" in r for r in results)
        mock_wa.assert_called()
        assert "reconciliation_close_all" in mock_wa.call_args[0][0]

    def test_sf13_a2_fires_alert_on_failure(self) -> None:
        action: dict = {
            "action": "close_structure",
            "structure_id": "sid-001",
            "symbol": "SPY",
            "method": "limit",
            "reason": "test",
        }
        with patch("reconciliation._opts_close_structure",
                   side_effect=RuntimeError("opts error")), \
             patch("notifications.send_whatsapp_direct") as mock_wa:
            results = reconciliation.execute_reconciliation_plan(
                [action], trading_client=MagicMock(), account_id="account2"
            )
        assert any("ERROR" in r for r in results)
        mock_wa.assert_called()
        assert "reconciliation_close_structure" in mock_wa.call_args[0][0]

    def test_sf13_continues_after_first_failure(self) -> None:
        call_count = [0]

        def fail_first(*_a, **_kw) -> None:
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("first action fails")

        actions = [
            reconciliation.ReconciliationAction(
                priority="NORMAL", symbol="AAPL", action_type="close_all",
                reason="test", qty=10.0,
            ),
            reconciliation.ReconciliationAction(
                priority="NORMAL", symbol="MSFT", action_type="close_all",
                reason="test", qty=5.0,
            ),
        ]
        with patch("reconciliation._close_position", side_effect=fail_first), \
             patch("notifications.send_whatsapp_direct"):
            reconciliation.execute_reconciliation_plan(
                actions, alpaca_client=MagicMock()
            )
        assert call_count[0] == 2  # both actions attempted despite first failure


# ===========================================================================
# SF14 — divergence transition_mode_audit_log
# ===========================================================================

class TestSF14TransitionModeAuditLog(unittest.TestCase):

    def setUp(self) -> None:
        _clear_all_caches()

    def _call_transition(self) -> object:
        return divergence.transition_mode(
            account="A1",
            new_mode=divergence.OperatingMode.RISK_CONTAINMENT,
            scope=divergence.DivergenceScope.ACCOUNT,
            scope_id="A1",
            reason_code="test_reason",
            reason_detail="test detail",
            entered_by="test",
        )

    def test_sf14_audit_log_failure_fires_alert(self) -> None:
        mock_old = MagicMock()
        mock_old.mode.value = "normal"
        with patch("divergence.load_account_mode", return_value=mock_old), \
             patch("divergence.save_account_mode"), \
             patch("builtins.open", side_effect=OSError("no disk space")), \
             patch("notifications.send_whatsapp_direct") as mock_wa:
            self._call_transition()
        mock_wa.assert_called()
        assert "transition_mode_audit_log" in mock_wa.call_args[0][0]

    def test_sf14_mode_state_unaffected_by_audit_failure(self) -> None:
        saved: list = []
        mock_old = MagicMock()
        mock_old.mode.value = "normal"
        with patch("divergence.load_account_mode", return_value=mock_old), \
             patch("divergence.save_account_mode", side_effect=saved.append), \
             patch("builtins.open", side_effect=OSError("no disk space")), \
             patch("notifications.send_whatsapp_direct"):
            result = self._call_transition()
        # save_account_mode was called once — mode state was written before audit failure
        assert len(saved) == 1
        assert result is not None
        assert result.mode == divergence.OperatingMode.RISK_CONTAINMENT


# ===========================================================================
# SF15 — options_executor close_structure lifecycle + leg alert
# ===========================================================================

class TestSF15CloseStructure(unittest.TestCase):

    def setUp(self) -> None:
        _clear_all_caches()

    def test_sf15_leg_failure_fires_alert(self) -> None:
        mock_client = MagicMock()
        mock_client.submit_order.side_effect = RuntimeError("alpaca error")
        with patch("notifications.send_whatsapp_direct") as mock_wa:
            options_executor.close_structure(
                _make_single_call(), mock_client, reason="test", method="market"
            )
        mock_wa.assert_called()
        assert "close_structure_leg_failed" in mock_wa.call_args[0][0]

    def test_sf15_market_close_cancelled_when_partial_credit_spread(self) -> None:
        """
        Call credit spread (2 filled legs): first leg submits, second raises.
        Market close with partial submission must yield CANCELLED, not CLOSED.
        """
        structure = _make_credit_spread()
        mock_client = MagicMock()
        call_count  = [0]

        def submit_first_only(_req: object) -> MagicMock:
            call_count[0] += 1
            if call_count[0] == 1:
                return MagicMock(id="ord-001")
            raise RuntimeError("second leg failed")

        mock_client.submit_order.side_effect = submit_first_only
        with patch("notifications.send_whatsapp_direct"):
            result = options_executor.close_structure(
                structure, mock_client, reason="eod_close", method="market"
            )
        assert result.lifecycle == StructureLifecycle.CANCELLED

    def test_sf15_market_close_closed_when_all_submitted(self) -> None:
        structure = _make_credit_spread()
        mock_client = MagicMock()
        mock_client.submit_order.side_effect = [MagicMock(id="o1"), MagicMock(id="o2")]
        with patch("notifications.send_whatsapp_direct"):
            result = options_executor.close_structure(
                structure, mock_client, reason="eod_close", method="market"
            )
        assert result.lifecycle == StructureLifecycle.CLOSED


# ===========================================================================
# SF16 — options_executor._emergency_close_leg
# ===========================================================================

class TestSF16EmergencyClose(unittest.TestCase):

    def setUp(self) -> None:
        _clear_all_caches()

    def test_sf16_alert_fires_on_failure(self) -> None:
        mock_client = MagicMock()
        mock_client.submit_order.side_effect = RuntimeError("emergency close failed")
        with patch("notifications.send_whatsapp_direct") as mock_wa:
            options_executor._emergency_close_leg(mock_client, "SPY230620C00400000", 1)
        mock_wa.assert_called_once()
        assert "emergency_close_leg_failed" in mock_wa.call_args[0][0]


# ===========================================================================
# SF17 — attribution.log_attribution_event
# ===========================================================================

class TestSF17Attribution(unittest.TestCase):

    def setUp(self) -> None:
        _clear_all_caches()

    def _call_attr(self) -> None:
        attribution.log_attribution_event(
            event_type="test",
            decision_id="d-001",
            account="A1",
            symbol="AAPL",
            module_tags={"signal": "test"},
            trigger_flags={"sonnet": False},
        )

    def test_sf17_outer_failure_fires_alert(self) -> None:
        # Patch open to raise so the write fails → outer except fires alert
        with patch("builtins.open", side_effect=OSError("disk full")), \
             patch("notifications.send_whatsapp_direct") as mock_wa:
            self._call_attr()
        mock_wa.assert_called_once()
        assert "log_attribution_event" in mock_wa.call_args[0][0]

    def test_sf17_spine_failure_does_not_fire_outer_alert(self) -> None:
        # Spine emit raises inside the inner try/except → no outer alert
        with patch("attribution._emit_spine_record", side_effect=RuntimeError("spine error")), \
             patch("notifications.send_whatsapp_direct") as mock_wa:
            # Let the write itself succeed (real or mocked file)
            try:
                self._call_attr()
            except Exception:
                pass  # file write may fail in test env — we care only about alerts
        mock_wa.assert_not_called()


if __name__ == "__main__":
    unittest.main()
