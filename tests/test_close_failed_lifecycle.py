"""
tests/test_close_failed_lifecycle.py

Orphan-position fix: close_structure() must not mark a structure CLOSED/CANCELLED
when the close order fails and Alpaca positions are still live.

Four tests:
  1. Successful per-leg close → lifecycle=CLOSED, closed_at set.
  2. All per-leg closes fail, position still in Alpaca → lifecycle unchanged (FULLY_FILLED),
     close_attempt_count incremented, closed_at stays None.
  3. close_attempt_count ≥ 3 after failure → _fire_safety_alert called with stuck key.
  4. close_check_loop saves the return value of close_structure (secondary bug fix).
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# ── Alpaca mock helpers (same pattern as test_close_mleg_and_untracked_gate) ──

class _PositionIntentEnum:
    BUY_TO_OPEN    = "buy_to_open"
    SELL_TO_OPEN   = "sell_to_open"
    BUY_TO_CLOSE   = "buy_to_close"
    SELL_TO_CLOSE  = "sell_to_close"


class _OrderClassEnum:
    MLEG   = "mleg"
    SIMPLE = "simple"


class _OrderSideEnum:
    BUY  = "buy"
    SELL = "sell"


class _TIFValue:
    def __init__(self, v: str):
        self.value = v
    def __eq__(self, other):
        return self.value == (other.value if isinstance(other, _TIFValue) else other)
    def __repr__(self):
        return self.value


class _TimeInForceEnum:
    DAY = _TIFValue("day")
    GTC = _TIFValue("gtc")


def _alpaca_mocks():
    enums_mod = MagicMock()
    enums_mod.OrderClass     = _OrderClassEnum
    enums_mod.TimeInForce    = _TimeInForceEnum
    enums_mod.PositionIntent = _PositionIntentEnum
    enums_mod.OrderSide      = _OrderSideEnum

    requests_mod = MagicMock()
    requests_mod.LimitOrderRequest  = lambda **kw: MagicMock(**kw)
    requests_mod.MarketOrderRequest = lambda **kw: MagicMock(**kw)
    requests_mod.OptionLegRequest   = lambda **kw: MagicMock(**kw)

    return {
        "alpaca":                  MagicMock(),
        "alpaca.trading":          MagicMock(),
        "alpaca.trading.enums":    enums_mod,
        "alpaca.trading.requests": requests_mod,
    }


# ── Structure fixture ─────────────────────────────────────────────────────────

def _make_single_leg_close_struct(close_attempt_count: int = 0):
    """Single-leg FULLY_FILLED structure with all fields required by close_structure()."""
    from schemas import OptionStrategy, StructureLifecycle

    s = MagicMock()
    s.contracts           = 1
    s.underlying          = "NVDA"
    s.expiration          = "2026-06-20"
    s.order_ids           = []
    s.lifecycle           = StructureLifecycle.FULLY_FILLED
    s.strategy            = OptionStrategy.SINGLE_CALL
    s.structure_id        = "test-1234-5678-abcd-ef01"
    s.close_attempt_count = close_attempt_count
    s.close_reason_code   = None
    s.close_reason_detail = None
    s.initiated_by        = None
    s.closed_at           = None
    s.pnl_unrealized      = None
    s.roll_reason_code    = None
    s.roll_reason_detail  = None

    audit_log = []
    s.audit_log = audit_log
    s.add_audit = lambda msg: audit_log.append(msg)

    leg = MagicMock()
    leg.side         = "buy"
    leg.filled_price = 5.00
    leg.occ_symbol   = "NVDA260620C00900000"
    leg.bid          = 4.90
    leg.ask          = 5.10
    leg.mid          = None
    leg.order_id     = None
    s.legs = [leg]
    return s


# ── Tests 1–3: close_structure() lifecycle behaviour ─────────────────────────

class TestCloseFailedLifecycle(unittest.TestCase):

    def test_successful_close_marks_lifecycle_closed(self):
        """Close with all legs succeeding → lifecycle=CLOSED, closed_at set."""
        import importlib

        with patch.dict(sys.modules, _alpaca_mocks()):
            import options_executor
            importlib.reload(options_executor)

            struct = _make_single_leg_close_struct()
            client = MagicMock()
            client.submit_order.return_value = MagicMock(id="order-ok")

            result = options_executor.close_structure(
                struct, client, reason="stop_hit", method="limit"
            )

        from schemas import StructureLifecycle
        self.assertEqual(
            result.lifecycle, StructureLifecycle.CLOSED,
            f"Expected CLOSED after successful close, got {result.lifecycle}",
        )
        self.assertIsNotNone(
            result.closed_at,
            "closed_at must be set when close succeeds",
        )

    def test_failed_close_leaves_lifecycle_unchanged(self):
        """Per-leg close fails + position still in Alpaca → lifecycle FULLY_FILLED, count++."""
        import importlib

        with patch.dict(sys.modules, _alpaca_mocks()):
            import options_executor
            importlib.reload(options_executor)

            struct = _make_single_leg_close_struct(close_attempt_count=0)
            client = MagicMock()
            client.submit_order.side_effect = Exception("42210000 position intent mismatch")

            # Position still live in Alpaca
            mock_pos = MagicMock()
            mock_pos.symbol = "NVDA260620C00900000"
            client.get_all_positions.return_value = [mock_pos]

            result = options_executor.close_structure(
                struct, client, reason="stop_hit", method="limit"
            )

        from schemas import StructureLifecycle
        self.assertEqual(
            result.lifecycle, StructureLifecycle.FULLY_FILLED,
            "Lifecycle must stay FULLY_FILLED when close fails with live position",
        )
        self.assertIsNone(
            result.closed_at,
            "closed_at must not be set when close fails",
        )
        self.assertEqual(
            result.close_attempt_count, 1,
            "close_attempt_count must increment on each failed close",
        )

    def test_failed_close_at_threshold_fires_safety_alert(self):
        """close_attempt_count ≥ 3 after failure → _fire_safety_alert called with stuck key."""
        import importlib

        with patch.dict(sys.modules, _alpaca_mocks()):
            import options_executor
            importlib.reload(options_executor)

            struct = _make_single_leg_close_struct(close_attempt_count=3)
            client = MagicMock()
            client.submit_order.side_effect = Exception("network_error")
            mock_pos = MagicMock()
            mock_pos.symbol = "NVDA260620C00900000"
            client.get_all_positions.return_value = [mock_pos]

            with patch.object(options_executor, "_fire_safety_alert") as mock_alert:
                options_executor.close_structure(
                    struct, client, reason="stop_hit", method="limit"
                )

            stuck_calls = [
                c for c in mock_alert.call_args_list
                if c.args and "close_stuck" in str(c.args[0])
            ]
            self.assertTrue(
                len(stuck_calls) > 0,
                f"Expected _fire_safety_alert with 'close_stuck' key at count ≥ 3; "
                f"actual calls: {mock_alert.call_args_list}",
            )


# ── Test 4: close_check_loop saves after close_structure ─────────────────────

class TestCloseCheckLoopSaves(unittest.TestCase):
    """close_check_loop must persist the structure returned by close_structure."""

    def test_close_check_loop_saves_structure_after_close(self):
        from schemas import OptionStrategy, StructureLifecycle

        # Sentinel: uniquely identifies the object close_structure returns.
        sentinel = MagicMock(name="closed_structure_sentinel")

        # --- fake open structure ---
        struct = MagicMock()
        struct.underlying          = "NVDA"
        struct.structure_id        = "test-dead-beef-0001"
        struct.lifecycle           = StructureLifecycle.FULLY_FILLED
        struct.strategy            = OptionStrategy.SINGLE_CALL
        struct.order_ids           = []
        struct.opened_at           = datetime.now(ET).isoformat()  # < 10 min → guard skipped
        struct.pnl_unrealized      = None
        struct.close_attempt_count = 0
        struct.close_reason_code   = None
        struct.close_reason_detail = None
        struct.initiated_by        = None
        struct.closed_at           = None

        audit_log = []
        struct.audit_log = audit_log
        struct.add_audit = lambda msg: audit_log.append(msg)

        leg = MagicMock()
        leg.side         = "buy"
        leg.filled_price = 5.00
        leg.occ_symbol   = "NVDA260620C00900000"
        leg.order_id     = None
        struct.legs = [leg]

        # --- mocked dependencies ---
        mock_state    = MagicMock()
        mock_executor = MagicMock()

        mock_state.get_open_structures.return_value = [struct]
        mock_state.load_structures.return_value     = [struct]

        mock_executor.should_close_structure.return_value = (True, "test_stop_hit")
        mock_executor.should_roll_structure.return_value  = (False, None)
        mock_executor.close_structure.return_value        = sentinel

        alpaca_client = MagicMock()
        alpaca_client.get_all_positions.return_value = []

        sys_patch = {
            "options_state":    mock_state,
            "options_executor": mock_executor,
            "options_data":     MagicMock(),
        }

        with patch.dict(sys.modules, sys_patch):
            import bot_options_stage4_execution as mod
            mod.close_check_loop(alpaca_client)

        # Verify close_structure was called
        mock_executor.close_structure.assert_called_once()

        # Verify save_structure was called with the sentinel returned by close_structure
        save_calls = mock_state.save_structure.call_args_list
        self.assertTrue(
            any(c.args[0] is sentinel for c in save_calls),
            f"save_structure must be called with the return value of close_structure; "
            f"actual calls: {save_calls}",
        )


if __name__ == "__main__":
    unittest.main()
