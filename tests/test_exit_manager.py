"""
tests/test_exit_manager.py — Trail stop path regression tests.

Verifies that maybe_trail_stop() uses cancel+resubmit as the SOLE path
(no replace_order_by_id) and handles stuck order states correctly.

These tests were written AFTER the fix landed in commit 46cec75, so they
PASS — they are regression guards, not TDD probes for an open bug.

Corresponding fix commit: 46cec75
  "feat(exit-manager): rewrite trail stop as cancel+resubmit; remove chromadb CI job"
"""
import sys
import types
from unittest.mock import MagicMock, patch

# ── stubs ─────────────────────────────────────────────────────────────────────

def _ensure_stubs():
    if "dotenv" not in sys.modules:
        m = types.ModuleType("dotenv")
        m.load_dotenv = lambda *a, **kw: None
        sys.modules["dotenv"] = m

_ensure_stubs()


# ── helpers ───────────────────────────────────────────────────────────────────

def _position(entry=100.0, current=105.0, unreal=50.0, sym="AAPL", qty=100):
    p = MagicMock()
    p.symbol = sym
    p.avg_entry_price = str(entry)
    p.current_price = str(current)
    p.unrealized_pl = str(unreal)
    p.qty = str(qty)
    return p


def _ei(stop_price=95.0, stop_oid="stop-oid-001", stop_status="accepted"):
    """
    Build an exit_info dict suitable for direct injection into maybe_trail_stop().
    stop_price=95 with entry=100, current=105 → stop_dist=5, profit_r=1.0 → trail fires.
    """
    return {
        "stop_price":        stop_price,
        "stop_order_id":     stop_oid,
        "stop_order_status": stop_status,
    }


def _cfg(**overrides):
    """Minimal strategy_config. No trail_tiers → uses legacy trigger path."""
    em_cfg = {
        "trail_stop_enabled":          True,
        "trail_trigger_r":             1.0,
        "trail_to_breakeven_plus_pct": 0.005,
        "trail_replace_max_failures":  3,
    }
    em_cfg.update(overrides)
    return {"exit_management": em_cfg}


# ── Test 1: replace_order_by_id is never called ───────────────────────────────

def test_trail_advance_never_calls_replace_order():
    """
    A profitable position at exactly trigger_r should advance via cancel+resubmit.
    replace_order_by_id must never be called — not even as a fallback.

    entry=100, stop=95, current=105 → profit_r=1.0 == trigger_r → trail fires.
    """
    import exit_manager as em

    client = MagicMock()
    new_order = MagicMock()
    new_order.id = "new-stop-id-after-trail"
    client.submit_order.return_value = new_order

    em._trail_replace_failures.clear()

    with patch("time.sleep"):
        result = em.maybe_trail_stop(
            position=_position(entry=100.0, current=105.0, unreal=50.0),
            alpaca_client=client,
            strategy_config=_cfg(),
            exit_info=_ei(stop_price=95.0, stop_oid="stop-oid-001"),
        )

    assert result is True, "Trail should succeed"
    assert client.replace_order_by_id.call_count == 0, (
        "replace_order_by_id must never be called — cancel+resubmit is the sole trail path"
    )
    client.cancel_order_by_id.assert_called_once_with("stop-oid-001")
    assert client.submit_order.call_count == 1, "Exactly one new stop submitted"


# ── Test 2: pending_replace stop skips trail without touching Alpaca ──────────

def test_trail_advance_handles_pending_replace_state():
    """
    When the existing stop is mid-replace (status='pending_replace'), maybe_trail_stop()
    must return False immediately without calling cancel or submit.

    This is the GOOGL incident scenario: replace_order was called by the OLD code,
    Alpaca accepted but put the order in pending_replace, and every subsequent cycle
    re-attempted, producing a 42210000 cascade. The fix skips the trail when the stop
    is in pending_replace rather than issuing another API call on the stuck order.
    """
    import exit_manager as em

    class NoCallClient:
        def cancel_order_by_id(self, oid):
            raise AssertionError(f"cancel_order_by_id must NOT be called when stop is pending_replace (oid={oid})")

        def submit_order(self, req):
            raise AssertionError("submit_order must NOT be called when stop is pending_replace")

        def replace_order_by_id(self, *a, **kw):
            raise AssertionError("replace_order_by_id must NOT be called")

    em._trail_replace_failures.clear()

    result = em.maybe_trail_stop(
        position=_position(entry=100.0, current=105.0, unreal=50.0),
        alpaca_client=NoCallClient(),
        strategy_config=_cfg(),
        exit_info=_ei(stop_price=95.0, stop_oid="stuck-oid", stop_status="pending_replace"),
    )

    assert result is False, (
        "Should return False when stop is pending_replace — not retry on a stuck order"
    )


# ── Test 3: pending_cancel stop fails gracefully ──────────────────────────────

def test_trail_advance_handles_pending_cancel_state():
    """
    When the existing stop is in pending_cancel (being cancelled by some other path),
    maybe_trail_stop() should:
      - attempt cancel_order_by_id (the current code does not special-case pending_cancel)
      - receive an exception (can't cancel a pending_cancel order)
      - increment the failure counter
      - return False without submitting a new stop

    This is graceful degradation: the next cycle's refresh_exits_for_position() will
    detect the unprotected position and place a fresh stop.
    """
    import exit_manager as em

    submitted = []

    class PendingCancelClient:
        def cancel_order_by_id(self, oid):
            raise RuntimeError("40310000: order in pending_cancel state")

        def submit_order(self, req):
            submitted.append(req)
            return MagicMock(id="should-not-reach")

        def replace_order_by_id(self, *a, **kw):
            raise AssertionError("replace_order_by_id must NOT be called")

    oid = "pending-cancel-oid"
    em._trail_replace_failures.clear()

    result = em.maybe_trail_stop(
        position=_position(entry=100.0, current=105.0, unreal=50.0),
        alpaca_client=PendingCancelClient(),
        strategy_config=_cfg(),
        exit_info=_ei(stop_price=95.0, stop_oid=oid, stop_status="pending_cancel"),
    )

    assert result is False, "Cancel failure on pending_cancel stop should return False"
    assert len(submitted) == 0, "submit_order must NOT be called when cancel fails"
    assert em._trail_replace_failures.get(oid, 0) == 1, (
        "Failure counter should be incremented on cancel failure"
    )


# ── Test 4: cancel failure is logged and original stop stays in place ─────────

def test_trail_cancel_fails_gracefully():
    """
    When cancel_order_by_id raises, the trail must:
      - NOT submit a new stop (which would create a duplicate)
      - increment the failure counter for this stop_oid
      - return False

    After max_failures consecutive cancel failures, the trail is abandoned for this
    order_id so the failure doesn't repeat every cycle indefinitely.
    """
    import exit_manager as em

    submitted = []
    cancelled = []

    class FailCancelClient:
        def cancel_order_by_id(self, oid):
            cancelled.append(oid)
            raise RuntimeError("Alpaca error: cannot cancel order in current state")

        def submit_order(self, req):
            submitted.append(req)
            return MagicMock(id="orphaned-stop")

        def replace_order_by_id(self, *a, **kw):
            raise AssertionError("replace_order_by_id must NOT be called")

    oid = "oid-cancel-will-fail"
    em._trail_replace_failures.clear()

    with patch("time.sleep"):
        result = em.maybe_trail_stop(
            position=_position(entry=100.0, current=105.0, unreal=50.0),
            alpaca_client=FailCancelClient(),
            strategy_config=_cfg(),
            exit_info=_ei(stop_price=95.0, stop_oid=oid),
        )

    assert result is False, "Cancel failure must return False"
    assert len(submitted) == 0, (
        "submit_order must NOT be called after cancel fails — "
        "submitting without cancelling creates a duplicate stop that triggers 40310000"
    )
    assert len(cancelled) == 1, "cancel_order_by_id should have been attempted once"
    assert em._trail_replace_failures.get(oid, 0) == 1, (
        "Failure counter should be incremented so repeated cancel failures "
        "are abandoned after trail_replace_max_failures cycles"
    )
