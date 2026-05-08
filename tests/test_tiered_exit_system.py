"""
test_tiered_exit_system.py — Tests for the A2 tiered exit system.

Layers under test:
- Layer 1: profit exits (target, profit-lock retrace, IV expansion take-profit)
- Layer 2: loss exits (stop-loss, theta-burn, near-expiry salvage)
- Layer 3: time exits (DTE checks)
- Per-structure close targets for orphan_tracked structures
- peak_pnl tracking in close_check_loop
- Layer ordering (time → profit → loss)
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_structure(
    *,
    underlying:    str = "AAPL",
    expiration:    str | None = None,   # YYYY-MM-DD; default = 30 days out
    contracts:     int = 1,
    filled_price:  float = 1.10,
    pnl_unrealized: float | None = None,
    peak_pnl:      float | None = None,
    max_profit_usd: float | None = 200.0,
    iv_rank:       float | None = None,
    theta:         float | None = None,
    lifecycle:     str = "fully_filled",
    close_profit_target_pct: float | None = None,
    close_max_loss_pct: float | None = None,
    close_time_stop_pct_dte: float | None = None,
):
    from schemas import (
        OptionsLeg,
        OptionsStructure,
        OptionStrategy,
        StructureLifecycle,
        Tier,
    )
    if expiration is None:
        expiration = (date.today() + timedelta(days=30)).isoformat()
    lc_map = {
        "fully_filled":    StructureLifecycle.FULLY_FILLED,
        "orphan_tracked":  StructureLifecycle.ORPHAN_TRACKED,
    }
    leg = OptionsLeg(
        occ_symbol=f"{underlying}260620C00150000",
        underlying=underlying,
        side="buy",
        qty=1,
        option_type="call",
        strike=150.0,
        expiration=expiration,
        filled_price=filled_price,
    )
    return OptionsStructure(
        structure_id=f"test-{underlying}-{lifecycle}",
        underlying=underlying,
        strategy=OptionStrategy.SINGLE_CALL,
        lifecycle=lc_map[lifecycle],
        legs=[leg],
        contracts=contracts,
        max_cost_usd=filled_price * contracts * 100,
        opened_at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
        catalyst="test",
        tier=Tier.CORE,
        debit_paid=filled_price,
        max_profit_usd=max_profit_usd,
        expiration=expiration,
        pnl_unrealized=pnl_unrealized,
        peak_pnl=peak_pnl,
        iv_rank=iv_rank,
        theta=theta,
        close_profit_target_pct=close_profit_target_pct,
        close_max_loss_pct=close_max_loss_pct,
        close_time_stop_pct_dte=close_time_stop_pct_dte,
    )


# ── TEST 1 — Profit lock fires when peak retraces ─────────────────────────────

def test_profit_lock_retrace_fires():
    """Layer 1d: peak ≥ 60% of max_profit, retrace to 30% → profit_lock_retrace."""
    import options_executor

    # max_profit=100 → peak threshold = 60, retrace threshold = 30
    struct = _make_structure(
        max_profit_usd=100.0,
        pnl_unrealized=25.0,   # retraced below 30
        peak_pnl=85.0,         # peak above 60
        expiration=(date.today() + timedelta(days=30)).isoformat(),
    )

    should_close, reason = options_executor.should_close_structure(
        struct, current_prices={}, config={}, current_time=None,
    )

    assert should_close is True
    assert reason == "profit_lock_retrace"


# ── TEST 2 — DTE ≤ 2 forces close regardless of P&L ───────────────────────────

def test_dte_under_3_forces_close_regardless_of_pnl():
    """Layer 3a: DTE ≤ 2 → expiry_approaching, even with small profit."""
    import options_executor

    struct = _make_structure(
        expiration=(date.today() + timedelta(days=2)).isoformat(),
        pnl_unrealized=10.0,   # small profit but irrelevant
    )

    should_close, reason = options_executor.should_close_structure(
        struct, current_prices={}, config={}, current_time=None,
    )

    assert should_close is True
    assert reason == "expiry_approaching"


# ── TEST 3 — Theta-aware exit for losing position with high theta ─────────────

def test_theta_burn_underwater_fires():
    """Layer 2c: theta cost > $50/day AND loss ≥ 20% of max_loss → close."""
    import options_executor

    # theta=-0.60, contracts=10 → daily_theta = 0.60 × 100 × 10 = $600/day (>$50)
    # filled_price=1.00, contracts=10 → max_loss = 1.00 × 10 × 100 = $1000
    # 25% loss = -$250 → satisfies ≥ 20% of max_loss (-$200)
    struct = _make_structure(
        theta=-0.60,
        contracts=10,
        filled_price=1.00,
        pnl_unrealized=-250.0,
        max_profit_usd=300.0,
        expiration=(date.today() + timedelta(days=20)).isoformat(),
    )

    should_close, reason = options_executor.should_close_structure(
        struct, current_prices={}, config={}, current_time=None,
    )

    assert should_close is True
    assert reason == "theta_burn_underwater"


# ── TEST 4 — IV expansion take-profit fires when IV up ≥ 20% ──────────────────

def test_iv_expansion_take_profit_fires():
    """Layer 1c: ≥ 40% of max_profit AND current IV rank ≥ entry × 1.20 → close."""
    import options_executor

    # iv_rank_at_entry=30; current=40 → 40 ≥ 30 × 1.20 = 36 ✓
    # max_profit=100; pnl=45 → ≥ 40% of max_profit ✓
    struct = _make_structure(
        max_profit_usd=100.0,
        pnl_unrealized=45.0,
        iv_rank=30.0,
        expiration=(date.today() + timedelta(days=30)).isoformat(),
    )

    with patch("options_executor._current_iv_rank", return_value=40.0):
        should_close, reason = options_executor.should_close_structure(
            struct, current_prices={}, config={}, current_time=None,
        )

    assert should_close is True
    assert reason == "iv_expansion_take_profit"


# ── TEST 5 — Orphan structure gets close targets at creation ──────────────────

def test_orphan_structure_has_close_targets():
    """Orphan reconciler stamps per-structure close targets on creation."""
    import bot_options_stage0_preflight as preflight
    import options_state as _os

    # Mock Alpaca position object
    class _FakePos:
        def __init__(self, sym, qty, avg, pl):
            self.symbol = sym
            self.qty = qty
            self.avg_entry_price = avg
            self.unrealized_pl = pl

    # OCC-formatted symbol so the regex matches
    pos = _FakePos("XPEV260515P00010000", 4, 0.50, -50.0)

    saved: list = []
    with patch.object(_os, "save_structure", side_effect=lambda s: saved.append(s)):
        created = preflight._reconcile_orphan_positions(
            live_opts=[pos],
            tracked_occs=set(),
            existing_structures=[],
        )

    assert "XPEV" in created
    assert len(saved) == 1
    s = saved[0]
    assert s.lifecycle.value == "orphan_tracked"
    assert s.close_profit_target_pct == 0.50
    assert s.close_max_loss_pct == 0.35
    assert s.close_time_stop_pct_dte == 0.50


# ── TEST 6 — Peak P&L updates correctly across cycles ─────────────────────────

def test_peak_pnl_updates_correctly():
    """close_check_loop monotonically tracks peak across pnl progression."""
    import bot_options_stage4_execution as m
    import options_state

    # Use a structure NOT near expiry so should_close_structure won't fire
    struct = _make_structure(
        underlying="AAPL",
        expiration=(date.today() + timedelta(days=30)).isoformat(),
        max_profit_usd=500.0,
    )

    # Sequence of pnl values across 4 cycles: 20, 60, 80, 50
    sequence = iter([20.0, 60.0, 80.0, 50.0])

    saved_states: list = []

    def _save(s):
        saved_states.append((s.pnl_unrealized, s.peak_pnl))

    def _fake_pnl(_struct, _prices):
        return next(sequence)

    # Provide an Alpaca position matching the structure's leg OCC symbol so
    # the position-gone guard does NOT close the structure on cycle 1.
    _live_pos = MagicMock()
    _live_pos.symbol = "AAPL260620C00150000"
    alpaca = MagicMock()
    alpaca.get_all_positions.return_value = [_live_pos]

    with (
        patch("options_state.get_open_structures", return_value=[struct]),
        patch("options_state.load_structures", return_value=[struct]),
        patch.object(m, "_sync_submitted_lifecycles"),
        patch.object(m, "_update_fill_prices"),
        patch.object(m, "_fetch_close_check_prices", return_value={}),
        patch.object(m, "_compute_pnl_unrealized", side_effect=_fake_pnl),
        patch("options_executor.should_close_structure", return_value=(False, "")),
        patch.object(options_state, "save_structure", side_effect=_save),
    ):
        # Patch the position-gone guard's age check by giving a stable opened_at
        # Already > 10 min old: opened_at is 2026-04-15.
        for _ in range(4):
            m.close_check_loop(alpaca)

    # After cycle 3 (pnl=80) peak should be 80; after cycle 4 (pnl=50) still 80.
    final_peak = struct.peak_pnl
    assert final_peak == 80.0, f"expected peak=80, got {final_peak}"
    assert struct.pnl_unrealized == 50.0


# ── TEST 7 — Layer ordering: time before profit before loss ───────────────────

def test_layer_order_time_checks_first():
    """
    Time exit must fire BEFORE profit/loss exits when both apply.
    Construct: DTE=2 + huge profit → expiry_approaching wins (Layer 3 > Layer 1).
    """
    import options_executor

    struct = _make_structure(
        expiration=(date.today() + timedelta(days=2)).isoformat(),
        max_profit_usd=100.0,
        pnl_unrealized=95.0,   # would also trigger profit_target_pct_hit (95%)
    )

    should_close, reason = options_executor.should_close_structure(
        struct, current_prices={}, config={}, current_time=None,
    )

    assert should_close is True
    assert reason == "expiry_approaching", (
        f"expected time-exit to win over profit-exit; got {reason}"
    )


def test_layer_order_loss_cut_near_expiry():
    """
    Layer 3b: DTE ≤ 4 AND pnl < 0 → loss_cut_near_expiry.
    Fires BEFORE max_loss_exit fallback.
    """
    import options_executor

    struct = _make_structure(
        expiration=(date.today() + timedelta(days=4)).isoformat(),
        pnl_unrealized=-10.0,   # tiny loss but DTE close
        max_profit_usd=100.0,
    )

    should_close, reason = options_executor.should_close_structure(
        struct, current_prices={}, config={}, current_time=None,
    )

    assert should_close is True
    assert reason == "loss_cut_near_expiry"
