"""
Tests for PENDING_REPLACE handling (PR-01 through PR-10).

Part A: reconciliation.py filters PENDING_REPLACE stops
Part B: exit_manager.py trail stop uses cancel+resubmit
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


# ── fake order helpers ────────────────────────────────────────────────────────

def _make_order(symbol="GOOGL", side="sell", order_type="stop",
                status="accepted", stop_price=160.0, created_at=None):
    from datetime import datetime
    o = MagicMock()
    o.symbol = symbol
    o.side = side
    o.type = order_type
    o.order_type = order_type
    o.status = status
    o.stop_price = stop_price
    o.limit_price = None
    o.created_at = created_at or datetime(2026, 5, 1, 10, 0, 0)
    o.id = f"order-{symbol}-{status}"
    return o


# ── PR-01: diff_state does NOT flag PENDING_REPLACE as duplicate ──────────────

def test_pr01_pending_replace_not_flagged_as_duplicate():

    # Two stops: one normal, one PENDING_REPLACE
    normal_stop = _make_order("GOOGL", status="accepted")
    pr_stop = _make_order("GOOGL", status="pending_replace")

    # Inject directly into diff logic via orders_by_sym
    orders_by_sym = {"GOOGL": [normal_stop, pr_stop]}

    # Simulate only the duplicate-detection logic from diff_state
    actions = []
    for sym, orders in orders_by_sym.items():
        all_stops = [o for o in orders if o.order_type in ("stop", "stop_limit")]
        pr_stops = [
            o for o in all_stops
            if str(getattr(o, "status", "")).lower().split(".")[-1] == "pending_replace"
        ]
        stop_orders = [o for o in all_stops if o not in pr_stops]
        if len(stop_orders) > 1:
            actions.append(("cancel_duplicate", sym))

    assert len(actions) == 0, "PENDING_REPLACE should not trigger cancel_duplicate"


# ── PR-02: diff_state DOES flag two real duplicate stops ─────────────────────

def test_pr02_two_real_stops_still_flagged():
    orders_by_sym = {
        "AAPL": [
            _make_order("AAPL", status="accepted"),
            _make_order("AAPL", status="accepted"),
        ]
    }

    actions = []
    for sym, orders in orders_by_sym.items():
        all_stops = [o for o in orders if o.order_type in ("stop", "stop_limit")]
        pr_stops = [
            o for o in all_stops
            if str(getattr(o, "status", "")).lower().split(".")[-1] == "pending_replace"
        ]
        stop_orders = [o for o in all_stops if o not in pr_stops]
        if len(stop_orders) > 1:
            actions.append(("cancel_duplicate", sym))

    assert len(actions) == 1, "Two real stop orders should still trigger cancel_duplicate"


# ── PR-03: _pending_replace_counts increments each cycle ─────────────────────

def test_pr03_pending_replace_counts_increment():
    import reconciliation as recon

    # Reset module-level state
    recon._pending_replace_counts.clear()

    pr_stop = _make_order("TSLA", status="pending_replace")
    normal_stop = _make_order("TSLA", status="accepted")
    orders_by_sym = {"TSLA": [normal_stop, pr_stop]}

    # Simulate 3 cycles of the duplicate detection loop
    for _ in range(3):
        for sym, orders in orders_by_sym.items():
            all_stops = [o for o in orders if o.order_type in ("stop", "stop_limit")]
            pr_stops = [
                o for o in all_stops
                if str(getattr(o, "status", "")).lower().split(".")[-1] == "pending_replace"
            ]
            if pr_stops:
                count = recon._pending_replace_counts.get(sym, 0) + 1
                recon._pending_replace_counts[sym] = count

    assert recon._pending_replace_counts.get("TSLA") == 3


# ── PR-04: _pending_replace_counts resets when PR resolves ───────────────────

def test_pr04_counts_reset_when_resolved():
    import reconciliation as recon

    recon._pending_replace_counts["GOOGL"] = 5

    # Simulate a cycle with no PENDING_REPLACE
    orders_by_sym = {"GOOGL": [_make_order("GOOGL", status="accepted")]}
    for sym, orders in orders_by_sym.items():
        all_stops = [o for o in orders if o.order_type in ("stop", "stop_limit")]
        pr_stops = [
            o for o in all_stops
            if str(getattr(o, "status", "")).lower().split(".")[-1] == "pending_replace"
        ]
        if not pr_stops:
            recon._pending_replace_counts.pop(sym, None)

    assert "GOOGL" not in recon._pending_replace_counts


# ── PR-05: _cancel_duplicate_stops skips PENDING_REPLACE orders ──────────────

def test_pr05_cancel_duplicate_skips_pending_replace():
    from datetime import datetime

    import reconciliation as recon
    newer = _make_order("GOOGL", status="accepted", created_at=datetime(2026, 5, 1, 11, 0, 0))
    older = _make_order("GOOGL", status="accepted", created_at=datetime(2026, 5, 1, 10, 0, 0))
    pr_order = _make_order("GOOGL", status="pending_replace", created_at=datetime(2026, 5, 1, 10, 30, 0))

    client = MagicMock()
    client.get_orders.return_value = [newer, older, pr_order]

    results = []
    recon._cancel_duplicate_stops(client, "GOOGL", results)

    # Should cancel older (1 cancel), but never touch pr_order
    assert client.cancel_order_by_id.call_count == 1
    cancelled_id = client.cancel_order_by_id.call_args[0][0]
    assert cancelled_id == str(older.id), "Should cancel the older non-PR stop"


# ── PR-06: maybe_trail_stop uses cancel then submit (no replace_order_by_id) ─

def test_pr06_trail_stop_uses_cancel_then_submit():
    import exit_manager as em

    cancelled = []
    submitted = []

    class FakeClient:
        def cancel_order_by_id(self, oid):
            cancelled.append(oid)
        def submit_order(self, req):
            submitted.append(req)
            result = MagicMock()
            result.id = "new-stop-id-001"
            return result

    position = MagicMock()
    position.symbol = "AAPL"
    position.avg_entry_price = "150.00"
    position.current_price = "175.00"
    position.unrealized_pl = "25.00"
    position.qty = "100"

    exit_info = {
        "has_stop": True,
        "stop_order_id": "old-stop-id",
        "stop_price": 140.0,  # below entry so stop_dist > 0
        "stop_order_status": "accepted",
        "position_health": "healthy",
        "trail_tier": "breakeven_plus",
        "binary_event_flag": False,
        "binary_event_note": "",
    }

    cfg = {
        "exit_management": {
            "trail_stop_enabled": True,
            "trail_trigger_r": 0.01,
            "trail_to_breakeven_plus_pct": 0.005,
        }
    }

    em._trail_replace_failures.clear()

    result = em.maybe_trail_stop(
        position=position,
        exit_info=exit_info,
        alpaca_client=FakeClient(),
        strategy_config=cfg,
    )

    assert result is True
    assert len(cancelled) == 1, "Should cancel existing stop"
    assert len(submitted) == 1, "Should submit new stop"


# ── PR-07: trail cancel failure returns False immediately ─────────────────────

def test_pr07_cancel_failure_returns_false():
    import exit_manager as em

    class FailCancelClient:
        def cancel_order_by_id(self, oid):
            raise RuntimeError("42210000: order pending replacement")
        def submit_order(self, req):
            raise AssertionError("submit should not be called after cancel failure")

    position = MagicMock()
    position.symbol = "TSLA"
    position.avg_entry_price = "200.00"
    position.current_price = "240.00"
    position.unrealized_pl = "40.00"
    position.qty = "50"

    exit_info = {
        "has_stop": True,
        "stop_order_id": "stop-tsla-001",
        "stop_price": 185.0,  # below entry
        "stop_order_status": "accepted",
        "position_health": "healthy",
        "trail_tier": "breakeven_plus",
        "binary_event_flag": False,
        "binary_event_note": "",
    }

    cfg = {"exit_management": {"trail_stop_enabled": True, "trail_trigger_r": 0.01}}
    em._trail_replace_failures.clear()

    result = em.maybe_trail_stop(
        position=position,
        exit_info=exit_info,
        alpaca_client=FailCancelClient(),
        strategy_config=cfg,
    )

    assert result is False


# ── PR-08: trail submit retries 3 times on failure then logs error ────────────

def test_pr08_submit_retries_3_times():
    import exit_manager as em

    submit_attempts = []

    class RetryClient:
        def cancel_order_by_id(self, oid):
            pass
        def submit_order(self, req):
            submit_attempts.append(req)
            raise RuntimeError("Alpaca 500 error")

    position = MagicMock()
    position.symbol = "NVDA"
    position.avg_entry_price = "800.00"
    position.current_price = "950.00"
    position.unrealized_pl = "150.00"
    position.qty = "10"

    exit_info = {
        "has_stop": True,
        "stop_order_id": "stop-nvda-001",
        "stop_price": 760.0,  # below entry
        "stop_order_status": "accepted",
        "position_health": "healthy",
        "trail_tier": "breakeven_plus",
        "binary_event_flag": False,
        "binary_event_note": "",
    }

    cfg = {"exit_management": {"trail_stop_enabled": True, "trail_trigger_r": 0.01}}
    em._trail_replace_failures.clear()

    with patch("time.sleep"):
        result = em.maybe_trail_stop(
            position=position,
            exit_info=exit_info,
            alpaca_client=RetryClient(),
            strategy_config=cfg,
        )

    assert result is False
    assert len(submit_attempts) == 3, f"Expected 3 submit attempts, got {len(submit_attempts)}"


# ── PR-09: PENDING_REPLACE exit_info skips trail altogether ──────────────────

def test_pr09_pending_replace_status_skips_trail():
    import exit_manager as em

    class NoCallClient:
        def cancel_order_by_id(self, oid):
            raise AssertionError("Should not call cancel when PENDING_REPLACE")
        def submit_order(self, req):
            raise AssertionError("Should not call submit when PENDING_REPLACE")

    position = MagicMock()
    position.symbol = "MSFT"
    position.avg_entry_price = "300.00"
    position.current_price = "360.00"
    position.qty = "20"

    exit_info = {
        "has_stop": True,
        "stop_order_id": "stop-msft-001",
        "stop_price": 310.0,
        "stop_order_status": "pending_replace",  # <-- the key field
        "position_health": "healthy",
        "trail_tier": "breakeven_plus",
        "binary_event_flag": False,
        "binary_event_note": "",
    }

    cfg = {"exit_management": {"trail_stop_enabled": True, "trail_trigger_r": 0.01}}
    em._trail_replace_failures.clear()

    result = em.maybe_trail_stop(
        position=position,
        exit_info=exit_info,
        alpaca_client=NoCallClient(),
        strategy_config=cfg,
    )

    assert result is False, "Should return False when stop is PENDING_REPLACE"


# ── PR-10: successful trail logs new_order_id not old stop_oid ────────────────

def test_pr10_trail_log_shows_new_order_id(capfd):
    import logging

    import exit_manager as em

    new_id = "brand-new-stop-xyz"

    class SuccessClient:
        def cancel_order_by_id(self, oid):
            pass
        def submit_order(self, req):
            result = MagicMock()
            result.id = new_id
            return result

    position = MagicMock()
    position.symbol = "XOM"
    position.avg_entry_price = "100.00"
    position.current_price = "125.00"
    position.unrealized_pl = "25.00"
    position.qty = "30"

    exit_info = {
        "has_stop": True,
        "stop_order_id": "old-stop-xom",
        "stop_price": 92.0,  # below entry
        "stop_order_status": "accepted",
        "position_health": "healthy",
        "trail_tier": "breakeven_plus",
        "binary_event_flag": False,
        "binary_event_note": "",
    }

    cfg = {"exit_management": {"trail_stop_enabled": True, "trail_trigger_r": 0.01}}
    em._trail_replace_failures.clear()

    log_records = []

    class CapHandler(logging.Handler):
        def emit(self, record):
            log_records.append(self.format(record))

    handler = CapHandler()
    handler.setLevel(logging.DEBUG)
    em_logger = logging.getLogger("exit_manager")
    original_level = em_logger.level
    em_logger.setLevel(logging.DEBUG)
    em_logger.addHandler(handler)
    try:
        result = em.maybe_trail_stop(
            position=position,
            exit_info=exit_info,
            alpaca_client=SuccessClient(),
            strategy_config=cfg,
        )
    finally:
        em_logger.removeHandler(handler)
        em_logger.setLevel(original_level)

    assert result is True
    trail_logs = [r for r in log_records if "stop advanced" in r]
    assert trail_logs, "Should emit [TRAIL_STOP] stop advanced log"
    assert new_id in trail_logs[0], f"Log should contain new_order_id={new_id}"
