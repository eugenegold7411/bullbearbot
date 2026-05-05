"""
tests/test_cancel_storm_guard.py — Per-cycle cap + inter-order delay for stop refreshes.

Verifies that run_exit_manager() limits stop-refresh burst size and spaces
cancel+resubmit pairs, preventing the May 4 cancel storm from recurring.

May 4 scenario: 8 positions, all with stale/tp_missing stops, produced
24+ Alpaca API calls per 6-minute cycle. With max_stop_refreshes_per_cycle=3
and stop_refresh_delay_seconds=0.5, the max burst is 3 pairs over 1.5 seconds.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, call, patch

import pytest


# ── stubs ─────────────────────────────────────────────────────────────────────

def _ensure_stubs():
    for name in ("dotenv",):
        if name not in sys.modules:
            m = types.ModuleType(name)
            m.load_dotenv = lambda *a, **kw: None
            sys.modules[name] = m

_ensure_stubs()


# ── helpers ───────────────────────────────────────────────────────────────────

def _pos(sym: str, qty: float = 100.0, current: float = 100.0, stale_stop: float = 80.0):
    """Build a MagicMock position. Default stop=$80 on price=$100 → 20% stale > 15% threshold."""
    p = MagicMock()
    p.symbol = sym
    p.qty = str(qty)
    p.current_price = str(current)
    p.avg_entry_price = str(current - 5.0)
    p.unrealized_pl = str(qty * 0.5)
    return p


def _exits_stale(syms: list[str], stop: float = 80.0, price: float = 100.0) -> dict:
    """Build exits dict where every position has a stale stop (20% below price)."""
    return {
        s: {
            "status": "partial",
            "stop_price": stop,
            "target_price": None,
            "stop_order_id": f"stop-{s}",
            "stop_order_status": "accepted",
            "target_order_id": None,
        }
        for s in syms
    }


def _exits_unprotected(syms: list[str]) -> dict:
    return {
        s: {
            "status": "unprotected",
            "stop_price": None,
            "target_price": None,
            "stop_order_id": None,
            "stop_order_status": None,
            "target_order_id": None,
        }
        for s in syms
    }


def _cfg(max_refreshes: int = 3, delay: float = 0.5) -> dict:
    return {
        "exit_management": {
            "trail_stop_enabled": False,
            "refresh_if_stop_stale_pct": 0.15,
            "max_stop_refreshes_per_cycle": max_refreshes,
            "stop_refresh_delay_seconds": delay,
        }
    }


# ── Test 1 — Per-cycle cap enforced ──────────────────────────────────────────

def test_cycle_cap_limits_refreshes_to_max():
    """
    8 positions all with stale stops → only max_stop_refreshes_per_cycle (3)
    call refresh_exits_for_position; remaining 5 are deferred with a log message.
    """
    import exit_manager as em

    syms = [f"SYM{i}" for i in range(8)]
    positions = [_pos(s) for s in syms]
    exits = _exits_stale(syms)

    refresh_calls: list[str] = []

    def fake_refresh(pos, alpaca, cfg, conviction="medium", exit_info=None):
        refresh_calls.append(pos.symbol)
        return True   # always "did work"

    with patch.object(em, "get_active_exits", return_value=exits), \
         patch.object(em, "_load_position_targets", return_value={}), \
         patch.object(em, "maybe_trail_stop", return_value=False), \
         patch.object(em, "refresh_exits_for_position", side_effect=fake_refresh), \
         patch("time.sleep"):

        actions = em.run_exit_manager(positions, MagicMock(), _cfg(max_refreshes=3))

    assert len(refresh_calls) == 3, (
        f"Expected exactly 3 refresh calls (cap=3), got {len(refresh_calls)}: {refresh_calls}"
    )
    assert len(actions) == 3, f"Expected 3 actions taken, got {len(actions)}"


# ── Test 2 — Inter-order delay fires between each pair ────────────────────────

def test_inter_order_delay_fires_between_pairs():
    """
    3 positions needing refresh → time.sleep called 2 times (before positions 2 and 3,
    not before position 1). Delay value matches stop_refresh_delay_seconds config.
    """
    import exit_manager as em

    syms = ["A", "B", "C"]
    positions = [_pos(s) for s in syms]
    exits = _exits_stale(syms)

    with patch.object(em, "get_active_exits", return_value=exits), \
         patch.object(em, "_load_position_targets", return_value={}), \
         patch.object(em, "maybe_trail_stop", return_value=False), \
         patch.object(em, "refresh_exits_for_position", return_value=True), \
         patch("exit_manager.time") as mock_time:

        em.run_exit_manager(positions, MagicMock(), _cfg(max_refreshes=5, delay=0.75))

    sleep_calls = [c.args[0] for c in mock_time.sleep.call_args_list]
    assert len(sleep_calls) == 2, (
        f"Expected 2 inter-refresh delays (for positions 2 and 3), got {len(sleep_calls)}: {sleep_calls}"
    )
    assert all(v == 0.75 for v in sleep_calls), (
        f"Each delay should be 0.75s (configured value), got: {sleep_calls}"
    )


# ── Test 3 — Config key max_stop_refreshes_per_cycle=1 ───────────────────────

def test_max_refreshes_config_one():
    """max_stop_refreshes_per_cycle=1 → exactly 1 refresh fires regardless of position count."""
    import exit_manager as em

    syms = ["X", "Y", "Z", "W"]
    positions = [_pos(s) for s in syms]
    exits = _exits_stale(syms)

    calls: list[str] = []

    def fake_refresh(pos, alpaca, cfg, conviction="medium", exit_info=None):
        calls.append(pos.symbol)
        return True

    with patch.object(em, "get_active_exits", return_value=exits), \
         patch.object(em, "_load_position_targets", return_value={}), \
         patch.object(em, "maybe_trail_stop", return_value=False), \
         patch.object(em, "refresh_exits_for_position", side_effect=fake_refresh), \
         patch("time.sleep"):

        em.run_exit_manager(positions, MagicMock(), _cfg(max_refreshes=1))

    assert len(calls) == 1, f"Expected 1 refresh with max=1, got {len(calls)}: {calls}"


# ── Test 4 — Safe defaults when config keys absent ────────────────────────────

def test_safe_defaults_when_config_absent():
    """Empty exit_management config → defaults to cap=3, delay=0.5s without error."""
    import exit_manager as em

    syms = [f"D{i}" for i in range(5)]
    positions = [_pos(s) for s in syms]
    exits = _exits_stale(syms)

    calls: list[str] = []

    def fake_refresh(pos, alpaca, cfg, conviction="medium", exit_info=None):
        calls.append(pos.symbol)
        return True

    with patch.object(em, "get_active_exits", return_value=exits), \
         patch.object(em, "_load_position_targets", return_value={}), \
         patch.object(em, "maybe_trail_stop", return_value=False), \
         patch.object(em, "refresh_exits_for_position", side_effect=fake_refresh), \
         patch("time.sleep"):

        # Empty config — relies entirely on defaults
        em.run_exit_manager(positions, MagicMock(), {})

    assert len(calls) == 3, (
        f"Default cap should be 3 — got {len(calls)}: {calls}"
    )


# ── Test 5 — Single position refresh unaffected ───────────────────────────────

def test_single_position_refresh_happy_path():
    """Single stale position → refreshes normally, no delay, no cap issue."""
    import exit_manager as em

    pos = _pos("AAPL")
    exits = _exits_stale(["AAPL"])

    refreshed: list[bool] = []

    def fake_refresh(p, alpaca, cfg, conviction="medium", exit_info=None):
        refreshed.append(True)
        return True

    with patch.object(em, "get_active_exits", return_value=exits), \
         patch.object(em, "_load_position_targets", return_value={}), \
         patch.object(em, "maybe_trail_stop", return_value=False), \
         patch.object(em, "refresh_exits_for_position", side_effect=fake_refresh), \
         patch("exit_manager.time") as mock_time:

        actions = em.run_exit_manager([pos], MagicMock(), _cfg())

    assert refreshed == [True], "Single position should refresh successfully"
    assert len(actions) == 1
    # No delay for the first (and only) refresh
    assert mock_time.sleep.call_count == 0, (
        "No inter-refresh delay should fire for the first refresh"
    )


# ── Test 6 — Protected positions NOT in refresh path ─────────────────────────

def test_protected_positions_skip_refresh():
    """
    Positions with status='protected' are already fine — cap counts only
    positions that actually need work.
    5 protected + 3 stale → exactly 3 stale get refreshed (cap=3 not hit by protected).
    """
    import exit_manager as em

    protected_syms = [f"P{i}" for i in range(5)]
    stale_syms     = [f"S{i}" for i in range(3)]
    all_syms       = protected_syms + stale_syms
    positions      = [_pos(s) for s in all_syms]

    exits: dict = {}
    for s in protected_syms:
        exits[s] = {
            "status": "protected",
            "stop_price": 90.0,
            "target_price": 115.0,
            "stop_order_id": f"stop-{s}",
            "stop_order_status": "accepted",
            "target_order_id": f"tp-{s}",
        }
    exits.update(_exits_stale(stale_syms))

    refresh_calls: list[str] = []

    def fake_refresh(p, alpaca, cfg, conviction="medium", exit_info=None):
        refresh_calls.append(p.symbol)
        return True

    with patch.object(em, "get_active_exits", return_value=exits), \
         patch.object(em, "_load_position_targets", return_value={}), \
         patch.object(em, "maybe_trail_stop", return_value=False), \
         patch.object(em, "refresh_exits_for_position", side_effect=fake_refresh), \
         patch("time.sleep"):

        actions = em.run_exit_manager(positions, MagicMock(), _cfg(max_refreshes=3))

    assert set(refresh_calls) == set(stale_syms), (
        f"Only stale positions should refresh — got: {refresh_calls}"
    )
    assert len(refresh_calls) == 3


# ── Test 7 — Unprotected positions bypass cap ─────────────────────────────────

def test_unprotected_bypasses_cap():
    """
    Unprotected (no stop at all) is safety-critical — must fire even when
    cap is already reached by non-urgent refreshes.
    2 stale positions (hit cap=2) + 1 unprotected → all 3 fire.
    """
    import exit_manager as em

    stale_syms       = ["S1", "S2"]
    unprotected_syms = ["U1"]
    all_syms         = stale_syms + unprotected_syms
    positions        = [_pos(s) for s in all_syms]

    exits: dict = {}
    exits.update(_exits_stale(stale_syms))
    exits.update(_exits_unprotected(unprotected_syms))

    refresh_calls: list[str] = []

    def fake_refresh(p, alpaca, cfg, conviction="medium", exit_info=None):
        refresh_calls.append(p.symbol)
        return True

    with patch.object(em, "get_active_exits", return_value=exits), \
         patch.object(em, "_load_position_targets", return_value={}), \
         patch.object(em, "maybe_trail_stop", return_value=False), \
         patch.object(em, "refresh_exits_for_position", side_effect=fake_refresh), \
         patch("time.sleep"):

        actions = em.run_exit_manager(positions, MagicMock(), _cfg(max_refreshes=2))

    assert "U1" in refresh_calls, "Unprotected position must always refresh regardless of cap"
    assert len(refresh_calls) == 3, (
        f"Both stale (cap=2) + 1 unprotected = 3 total — got {len(refresh_calls)}: {refresh_calls}"
    )
