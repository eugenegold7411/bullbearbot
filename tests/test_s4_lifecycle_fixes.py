"""
tests/test_s4_lifecycle_fixes.py

Tests for three lifecycle fixes:
  Fix 1 — order_executor_options: save_structure called with SUBMITTED structure
           after submit_structure() returns.
  Fix 2 — bot_options_stage4_execution._sync_submitted_lifecycles: transitions
           SUBMITTED → CANCELLED / FULLY_FILLED / PARTIALLY_FILLED by order status.
  Fix 3 — reconciliation.reconcile_options_structures: cancelled_orders populated
           for SUBMITTED structures whose order_ids are absent from open_orders.
"""
from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch

# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_structure(lifecycle="submitted", order_ids=None, occ="XLF260515C00052000",
                    underlying="XLF", strategy_val="single_call"):
    """Build a minimal OptionsStructure-like object."""
    import uuid

    from schemas import (
        OptionsLeg,
        OptionsStructure,
        OptionStrategy,
        StructureLifecycle,
        Tier,
    )
    lc = StructureLifecycle(lifecycle)
    leg = OptionsLeg(
        occ_symbol=occ,
        underlying=underlying,
        side="buy",
        qty=1,
        option_type="call",
        strike=52.0,
        expiration="2026-05-15",
        bid=0.78,
        ask=0.82,
        mid=0.80,
    )
    s = OptionsStructure(
        structure_id=str(uuid.uuid4()),
        underlying=underlying,
        strategy=OptionStrategy(strategy_val),
        lifecycle=lc,
        legs=[leg],
        contracts=10,
        max_cost_usd=800.0,
        opened_at="2026-04-29T19:19:22+00:00",
        catalyst="",
        tier=Tier.CORE,
    )
    s.order_ids = list(order_ids or [])
    return s


def _make_snapshot(positions=None, open_orders=None):
    """Build a minimal BrokerSnapshot-like object."""
    from schemas import BrokerSnapshot
    snap = BrokerSnapshot(
        positions=list(positions or []),
        open_orders=list(open_orders or []),
        equity=100_000.0,
        cash=100_000.0,
        buying_power=100_000.0,
    )
    return snap


def _mock_open_order(order_id: str):
    """Return a minimal NormalizedOrder-like mock with given order_id."""
    o = MagicMock()
    o.order_id = order_id
    o.id = order_id
    return o


# ─────────────────────────────────────────────────────────────────────────────
# Fix 1: order_executor_options saves SUBMITTED structure after submission
# ─────────────────────────────────────────────────────────────────────────────

class TestFix1SaveStructureAfterSubmit(unittest.TestCase):
    """Fix 1: submit_options_order() must persist the updated (SUBMITTED) structure."""

    def _run_submit(self, submitted_lifecycle="submitted"):
        """
        Run submit_options_order with a mocked options_executor, options_state,
        and _get_options_client. Returns (saved_structures, result).
        """
        structure = _make_structure(lifecycle="proposed", order_ids=[])

        # Build a 'filled' structure as options_executor would return it
        filled = _make_structure(lifecycle=submitted_lifecycle,
                                 order_ids=["order-abc-123"])
        filled.legs[0].order_id = "order-abc-123"

        mock_executor = MagicMock()
        mock_executor.submit_structure.return_value = filled

        saved_structures = []
        mock_os = MagicMock()
        mock_os.save_structure.side_effect = lambda s: saved_structures.append(s)

        mock_client = MagicMock()
        mock_acct = MagicMock()
        mock_acct.daytrading_buying_power = 100_000.0
        mock_acct.options_buying_power = 100_000.0
        mock_client.get_account.return_value = mock_acct

        import order_executor_options as oeo
        with patch.dict(sys.modules, {
            "options_executor": mock_executor,
            "options_state": mock_os,
        }), patch.object(oeo, "_get_options_client", return_value=mock_client):
            result = oeo.submit_options_order(structure, equity=100_000.0,
                                              observation_mode=False)

        return saved_structures, result

    def test_save_structure_called_after_submit(self):
        """save_structure must be called at least once with the SUBMITTED structure."""
        saved, _ = self._run_submit()
        self.assertGreater(len(saved), 0, "save_structure was never called")

    def test_saved_structure_has_submitted_lifecycle(self):
        """The structure passed to save_structure must have lifecycle=submitted."""
        saved, _ = self._run_submit()
        from schemas import StructureLifecycle
        submitted_saves = [
            s for s in saved
            if (s.lifecycle == StructureLifecycle.SUBMITTED
                or str(s.lifecycle) == "submitted"
                or (hasattr(s.lifecycle, "value") and s.lifecycle.value == "submitted"))
        ]
        self.assertGreater(len(submitted_saves), 0,
                           f"No SUBMITTED structure was saved; got {[str(s.lifecycle) for s in saved]}")

    def test_saved_structure_has_order_ids(self):
        """The saved structure must carry the Alpaca order_id."""
        saved, _ = self._run_submit()
        saves_with_order = [s for s in saved if s.order_ids]
        self.assertGreater(len(saves_with_order), 0,
                           "Saved structure has empty order_ids")

    def test_result_status_is_submitted(self):
        """OptionsExecutionResult.status must still be 'submitted'."""
        _, result = self._run_submit()
        self.assertEqual(result.status, "submitted")

    def test_save_not_called_in_observation_mode(self):
        """In observation mode no order is submitted and save_structure is not called."""
        structure = _make_structure(lifecycle="proposed", order_ids=[])
        mock_os = MagicMock()
        import order_executor_options as oeo
        with patch.dict(sys.modules, {"options_state": mock_os,
                                      "options_executor": MagicMock()}):
            oeo.submit_options_order(structure, equity=100_000.0, observation_mode=True)
        # Fix 1 save must not fire in obs mode (no submit_structure call)
        mock_os.save_structure.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Fix 2: _sync_submitted_lifecycles transitions by order status
# ─────────────────────────────────────────────────────────────────────────────

class TestFix2SyncSubmittedLifecycles(unittest.TestCase):
    """Fix 2: _sync_submitted_lifecycles handles cancel, fill, partial, and open."""

    def _run_sync(self, alpaca_order_status: str, lifecycle="submitted",
                  order_ids=None):
        """
        Run _sync_submitted_lifecycles with one structure and one Alpaca order status.
        Returns the saved structures list.
        """
        from bot_options_stage4_execution import _sync_submitted_lifecycles
        structure = _make_structure(lifecycle=lifecycle,
                                    order_ids=order_ids or ["order-001"])

        mock_order = MagicMock()
        mock_order.status = alpaca_order_status

        mock_client = MagicMock()
        mock_client.get_order_by_id.return_value = mock_order

        saved = []
        mock_os = MagicMock()
        mock_os.save_structure.side_effect = lambda s: saved.append(s)

        with patch.dict(sys.modules, {"options_state": mock_os}):
            _sync_submitted_lifecycles([structure], mock_client)

        return structure, saved

    def test_cancelled_order_transitions_to_cancelled(self):
        """Alpaca status 'cancelled' → structure lifecycle becomes CANCELLED."""
        from schemas import StructureLifecycle
        struct, saved = self._run_sync("cancelled")
        self.assertEqual(struct.lifecycle, StructureLifecycle.CANCELLED)
        self.assertEqual(len(saved), 1)

    def test_orderstatus_enum_string_cancelled(self):
        """Alpaca enum 'OrderStatus.cancelled' (dot-form) also transitions correctly."""
        from schemas import StructureLifecycle
        struct, saved = self._run_sync("OrderStatus.cancelled")
        self.assertEqual(struct.lifecycle, StructureLifecycle.CANCELLED)

    def test_expired_order_transitions_to_cancelled(self):
        """Alpaca status 'expired' → CANCELLED."""
        from schemas import StructureLifecycle
        struct, saved = self._run_sync("expired")
        self.assertEqual(struct.lifecycle, StructureLifecycle.CANCELLED)

    def test_done_for_day_transitions_to_cancelled(self):
        """DAY order that expired at close → CANCELLED."""
        from schemas import StructureLifecycle
        struct, saved = self._run_sync("done_for_day")
        self.assertEqual(struct.lifecycle, StructureLifecycle.CANCELLED)

    def test_filled_order_transitions_to_fully_filled(self):
        """Alpaca status 'filled' → FULLY_FILLED."""
        from schemas import StructureLifecycle
        struct, saved = self._run_sync("filled")
        self.assertEqual(struct.lifecycle, StructureLifecycle.FULLY_FILLED)
        self.assertEqual(len(saved), 1)

    def test_partially_filled_transitions_to_partially_filled(self):
        """Alpaca status 'partially_filled' → PARTIALLY_FILLED."""
        from schemas import StructureLifecycle
        struct, saved = self._run_sync("partially_filled")
        self.assertEqual(struct.lifecycle, StructureLifecycle.PARTIALLY_FILLED)
        self.assertEqual(len(saved), 1)

    def test_new_order_no_transition(self):
        """Alpaca status 'new' (still pending) → no lifecycle change, no save."""
        from schemas import StructureLifecycle
        struct, saved = self._run_sync("new")
        self.assertEqual(struct.lifecycle, StructureLifecycle.SUBMITTED)
        self.assertEqual(len(saved), 0)

    def test_accepted_order_no_transition(self):
        """Alpaca status 'accepted' → no transition."""
        from schemas import StructureLifecycle
        struct, saved = self._run_sync("accepted")
        self.assertEqual(struct.lifecycle, StructureLifecycle.SUBMITTED)

    def test_non_submitted_structure_skipped(self):
        """Structures in other lifecycles are not touched."""
        from bot_options_stage4_execution import _sync_submitted_lifecycles
        for lc in ("proposed", "fully_filled", "cancelled", "rejected"):
            struct = _make_structure(lifecycle=lc, order_ids=["order-001"])
            mock_client = MagicMock()
            with patch.dict(sys.modules, {"options_state": MagicMock()}):
                _sync_submitted_lifecycles([struct], mock_client)
            mock_client.get_order_by_id.assert_not_called()
            mock_client.reset_mock()

    def test_submitted_with_no_order_ids_skipped(self):
        """SUBMITTED structure with empty order_ids is skipped — no Alpaca call."""
        from bot_options_stage4_execution import _sync_submitted_lifecycles
        struct = _make_structure(lifecycle="submitted", order_ids=[])
        mock_client = MagicMock()
        with patch.dict(sys.modules, {"options_state": MagicMock()}):
            _sync_submitted_lifecycles([struct], mock_client)
        mock_client.get_order_by_id.assert_not_called()

    def test_alpaca_exception_non_fatal(self):
        """Alpaca API error must not raise — structure lifecycle unchanged."""
        from bot_options_stage4_execution import _sync_submitted_lifecycles
        from schemas import StructureLifecycle
        struct = _make_structure(lifecycle="submitted", order_ids=["order-001"])
        mock_client = MagicMock()
        mock_client.get_order_by_id.side_effect = RuntimeError("network error")
        with patch.dict(sys.modules, {"options_state": MagicMock()}):
            _sync_submitted_lifecycles([struct], mock_client)  # must not raise
        self.assertEqual(struct.lifecycle, StructureLifecycle.SUBMITTED)

    def test_cancelled_audit_log_updated(self):
        """A cancellation must add an entry to the structure's audit_log."""
        struct, _ = self._run_sync("cancelled")
        audit_msgs = [e["msg"] if isinstance(e, dict) else str(e)
                      for e in struct.audit_log]
        self.assertTrue(any("cancelled" in m for m in audit_msgs),
                        f"No cancellation entry in audit_log: {audit_msgs}")


# ─────────────────────────────────────────────────────────────────────────────
# Fix 3: reconcile_options_structures populates cancelled_orders
# ─────────────────────────────────────────────────────────────────────────────

class TestFix3CancelledOrdersInRecon(unittest.TestCase):
    """Fix 3: reconcile_options_structures detects dead SUBMITTED orders."""

    def _run_recon(self, structures, snapshot):
        from reconciliation import reconcile_options_structures
        return reconcile_options_structures(
            structures=structures,
            snapshot=snapshot,
            current_time="2026-04-29T20:00:00+00:00",
            config={},
        )

    def test_submitted_structure_dead_order_in_cancelled_orders(self):
        """SUBMITTED structure whose order_id is not in open_orders → cancelled_orders."""
        struct = _make_structure(lifecycle="submitted", order_ids=["order-dead"])
        snapshot = _make_snapshot(positions=[], open_orders=[])
        result = self._run_recon([struct], snapshot)
        self.assertIn(struct.structure_id, result.cancelled_orders)

    def test_submitted_structure_live_order_not_in_cancelled_orders(self):
        """SUBMITTED structure whose order_id IS in open_orders → not in cancelled_orders."""
        struct = _make_structure(lifecycle="submitted", order_ids=["order-live"])
        live_order = _mock_open_order("order-live")
        snapshot = _make_snapshot(positions=[], open_orders=[live_order])
        result = self._run_recon([struct], snapshot)
        self.assertNotIn(struct.structure_id, result.cancelled_orders)

    def test_non_submitted_structure_not_in_cancelled_orders(self):
        """PROPOSED/FULLY_FILLED/CANCELLED structures are never added to cancelled_orders."""
        for lc in ("proposed", "fully_filled", "cancelled", "rejected"):
            struct = _make_structure(lifecycle=lc, order_ids=["order-x"])
            snapshot = _make_snapshot(positions=[], open_orders=[])
            result = self._run_recon([struct], snapshot)
            self.assertNotIn(struct.structure_id, result.cancelled_orders,
                             f"lifecycle={lc} should not appear in cancelled_orders")

    def test_submitted_with_no_order_ids_not_in_cancelled_orders(self):
        """SUBMITTED with empty order_ids — no order to check, not flagged."""
        struct = _make_structure(lifecycle="submitted", order_ids=[])
        snapshot = _make_snapshot(positions=[], open_orders=[])
        result = self._run_recon([struct], snapshot)
        self.assertNotIn(struct.structure_id, result.cancelled_orders)

    def test_submitted_structure_filled_occ_in_positions_not_cancelled(self):
        """If OCC symbol is present in positions, order filled — not cancelled."""
        from schemas import NormalizedPosition
        struct = _make_structure(lifecycle="submitted", order_ids=["order-filled"],
                                 occ="XLF260515C00052000")
        pos = NormalizedPosition(
            symbol="XLF260515C00052000",
            alpaca_sym="XLF260515C00052000",
            qty=10,
            avg_entry_price=0.80,
            current_price=0.82,
            market_value=820.0,
            unrealized_pl=20.0,
            unrealized_plpc=0.025,
            is_crypto_pos=False,
        )
        snapshot = _make_snapshot(positions=[pos], open_orders=[])
        result = self._run_recon([struct], snapshot)
        self.assertNotIn(struct.structure_id, result.cancelled_orders)

    def test_cancelled_orders_field_exists_on_result(self):
        """OptionsReconResult always has a cancelled_orders attribute."""
        from reconciliation import OptionsReconResult
        r = OptionsReconResult()
        self.assertTrue(hasattr(r, "cancelled_orders"))
        self.assertIsInstance(r.cancelled_orders, list)

    def test_multiple_structures_only_dead_ones_flagged(self):
        """Two SUBMITTED structures: one live order, one dead — only dead is flagged."""
        struct_live = _make_structure(lifecycle="submitted", order_ids=["order-live"],
                                      occ="XLF260515C00052000", underlying="XLF")
        struct_dead = _make_structure(lifecycle="submitted", order_ids=["order-dead"],
                                      occ="NVDA260522C00215000", underlying="NVDA",
                                      strategy_val="call_credit_spread")
        live_order = _mock_open_order("order-live")
        snapshot = _make_snapshot(positions=[], open_orders=[live_order])
        result = self._run_recon([struct_live, struct_dead], snapshot)
        self.assertNotIn(struct_live.structure_id, result.cancelled_orders)
        self.assertIn(struct_dead.structure_id, result.cancelled_orders)


if __name__ == "__main__":
    unittest.main()
