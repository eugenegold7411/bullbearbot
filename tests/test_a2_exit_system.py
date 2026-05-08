"""
tests/test_a2_exit_system.py — A2 exit system: L1e, L1f, SPREAD_NARROWING, DELTA_ITM urgency.

Covers:
  a. L1e fires when pnl >= buy_cost × profit_multiple
  b. L1e does NOT fire below threshold
  c. L1f fires when peak >= buy_cost × 0.50 AND pnl <= peak × 0.50
  d. L1f does NOT fire when peak below buy_cost threshold
  e. L1f does NOT fire when no retrace has occurred
  f. DELTA_ITM urgency is "immediate" in _route_action()
  g. SPREAD_NARROWING fires when abs_delta < 0.10 for spread strategy
  h. SPREAD_NARROWING does NOT fire for single_call strategy
  i. close_check_loop calls close_structure when intel urgency=immediate + gate enabled
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_structure(
    strategy: str = "single_call",
    pnl: float | None = None,
    peak_pnl: float | None = None,
    filled_price: float = 5.0,
    contracts: int = 2,
    expiration: str = "2026-07-18",
    opened_at: str = "2026-05-01T10:00:00+00:00",
    lifecycle: str = "fully_filled",
    structure_id: str = "test-sid-001",
    underlying: str = "AAPL",
) -> MagicMock:
    from schemas import OptionStrategy, StructureLifecycle

    strat_map = {
        "single_call": OptionStrategy.SINGLE_CALL,
        "single_put":  OptionStrategy.SINGLE_PUT,
        "call_debit_spread": OptionStrategy.CALL_DEBIT_SPREAD,
        "put_debit_spread":  OptionStrategy.PUT_DEBIT_SPREAD,
    }
    lc_map = {
        "fully_filled": StructureLifecycle.FULLY_FILLED,
        "open": StructureLifecycle.FULLY_FILLED,
        "cancelled": StructureLifecycle.CANCELLED,
    }

    leg = SimpleNamespace(
        side="buy",
        filled_price=str(filled_price),
        occ_symbol="AAPL260718C00200000",
        option_type="call",
        strike=200.0,
        qty=contracts,
        expiration=expiration,
        bid=None, ask=None, mid=None,
        delta=None, open_interest=None, volume=None,
        underlying=underlying,
        order_id=None,
    )

    s = MagicMock()
    s.structure_id     = structure_id
    s.underlying       = underlying
    s.strategy         = strat_map.get(strategy, OptionStrategy.SINGLE_CALL)
    s.lifecycle        = lc_map.get(lifecycle, StructureLifecycle.FULLY_FILLED)
    s.legs             = [leg]
    s.contracts        = contracts
    s.pnl_unrealized   = pnl
    s.peak_pnl         = peak_pnl
    s.peak_pnl_at      = None
    s.max_profit_usd   = None   # intentionally None (single calls / unlimited)
    s.max_cost_usd     = filled_price * contracts * 100
    s.expiration       = expiration
    s.opened_at        = opened_at
    s.iv_rank          = None
    s.theta            = None
    s.is_open.return_value = True
    s.net_debit_per_contract.return_value = filled_price
    return s


def _cfg(profit_multiple: float = 1.0, peak_min: float = 0.50,
         peak_retrace: float = 0.50, max_loss_pct: float = 0.35) -> dict:
    return {
        "account2": {
            "profit_multiple_target": profit_multiple,
            "peak_retrace_min_pct":   peak_min,
            "peak_retrace_threshold": peak_retrace,
            "max_loss_exit_pct":      max_loss_pct,
            "force_close_structures": [],
        },
        "force_close_structures": [],
    }


# ── Test a — L1e fires ────────────────────────────────────────────────────────

def test_l1e_fires_at_profit_multiple():
    """L1e: pnl >= buy_cost × 1.0 → cost_basis_profit_target."""
    import options_executor as oe

    # buy_cost = 5.0 × 2 contracts × 100 = $1,000
    # pnl = $1,000 → 100% of buy_cost → L1e fires
    struct = _make_structure(pnl=1_000.0, filled_price=5.0, contracts=2)
    close, reason = oe.should_close_structure(struct, {}, _cfg(profit_multiple=1.0), None)

    assert close is True
    assert reason.startswith("cost_basis_profit_target")


# ── Test b — L1e does NOT fire below threshold ────────────────────────────────

def test_l1e_does_not_fire_below_threshold():
    """L1e: pnl = 90% of buy_cost → does NOT fire at profit_multiple=1.0."""
    import options_executor as oe

    # buy_cost = $1,000; pnl = $900 (90%) — below 1.0×
    struct = _make_structure(pnl=900.0, filled_price=5.0, contracts=2)
    close, reason = oe.should_close_structure(struct, {}, _cfg(profit_multiple=1.0), None)

    assert close is False or not reason.startswith("cost_basis_profit_target"), (
        f"L1e must not fire at 90% of buy_cost, got reason={reason!r}"
    )


# ── Test c — L1f fires on peak retrace ───────────────────────────────────────

def test_l1f_fires_on_peak_retrace():
    """L1f: peak=$700 (70% of $1k cost), pnl=$300 (43% of peak → retraced 57%) → fires."""
    import options_executor as oe

    # buy_cost = $1,000; peak = $700 (>= 50% min); pnl = $300 (<= peak × 0.50 = $350)
    struct = _make_structure(pnl=300.0, peak_pnl=700.0, filled_price=5.0, contracts=2)
    close, reason = oe.should_close_structure(
        struct, {}, _cfg(profit_multiple=2.0, peak_min=0.50, peak_retrace=0.50), None
    )

    assert close is True
    assert reason.startswith("peak_retrace_lock")


# ── Test d — L1f does NOT fire when peak below buy_cost threshold ─────────────

def test_l1f_does_not_fire_when_peak_below_minimum():
    """L1f: peak=$400 (40% of $1k cost) — below 50% min → does NOT fire."""
    import options_executor as oe

    struct = _make_structure(pnl=150.0, peak_pnl=400.0, filled_price=5.0, contracts=2)
    close, reason = oe.should_close_structure(
        struct, {}, _cfg(profit_multiple=2.0, peak_min=0.50, peak_retrace=0.50), None
    )

    assert not (close and reason.startswith("peak_retrace_lock")), (
        f"L1f must not fire when peak=$400 < 50% of buy_cost=$1000"
    )


# ── Test e — L1f does NOT fire when no retrace ────────────────────────────────

def test_l1f_does_not_fire_when_no_retrace():
    """L1f: peak=$700, pnl=$690 — barely below peak, no significant retrace."""
    import options_executor as oe

    # peak=$700 (>= 50% of $1k) but pnl=$690 > peak × 0.50 = $350 → no retrace
    struct = _make_structure(pnl=690.0, peak_pnl=700.0, filled_price=5.0, contracts=2)
    close, reason = oe.should_close_structure(
        struct, {}, _cfg(profit_multiple=2.0, peak_min=0.50, peak_retrace=0.50), None
    )

    assert not (close and reason.startswith("peak_retrace_lock")), (
        "L1f must not fire when pnl is still near peak"
    )


# ── Test f — DELTA_ITM urgency is immediate ───────────────────────────────────

def test_delta_itm_urgency_is_immediate():
    """DELTA_ITM → _route_action() must produce urgency='immediate'."""
    from options_position_manager import DriftState, _route_action

    struct = MagicMock()
    struct.underlying  = "TSLA"
    struct.structure_id = "tsla-sid"
    struct.strategy    = MagicMock()
    struct.strategy.value = "single_call"
    struct.expiration  = "2026-07-18"
    struct.opened_at   = "2026-05-01T10:00:00+00:00"

    action = _route_action(DriftState.DELTA_ITM, struct, {}, {})

    assert action.urgency == "immediate", (
        f"DELTA_ITM urgency must be 'immediate', got {action.urgency!r}"
    )
    assert action.action == "CLOSE"


# ── Test g — SPREAD_NARROWING fires for spread ────────────────────────────────

def test_spread_narrowing_fires_for_spread():
    """SPREAD_NARROWING: abs_delta=0.07 < 0.10 for call_debit_spread → fires."""
    from options_position_manager import DriftState, GreekSnapshot, _detect_drift

    current = GreekSnapshot(
        delta=0.07, theta=-0.01, vega=0.05, gamma=0.01,
        underlying_price=300.0, timestamp="2026-05-08T15:00:00+00:00",
    )
    entry   = {"delta": 0.35, "theta": -0.02, "vega": 0.10, "gamma": 0.02}
    # 3+ history points to satisfy min_history_points
    history = [entry, entry, entry]

    state, reason = _detect_drift(
        current, entry, history, "call_debit_spread", None, {}
    )

    assert state == DriftState.SPREAD_NARROWING, (
        f"Expected SPREAD_NARROWING, got {state!r}: {reason}"
    )


# ── Test h — SPREAD_NARROWING does NOT fire for single_call ──────────────────

def test_spread_narrowing_does_not_fire_for_single_call():
    """SPREAD_NARROWING must not fire for single_call with low abs_delta."""
    from options_position_manager import DriftState, GreekSnapshot, _detect_drift

    current = GreekSnapshot(
        delta=0.07, theta=-0.01, vega=0.05, gamma=0.01,
        underlying_price=300.0, timestamp="2026-05-08T15:00:00+00:00",
    )
    entry   = {"delta": 0.35, "theta": -0.02, "vega": 0.10, "gamma": 0.02}
    history = [entry, entry, entry]

    state, reason = _detect_drift(
        current, entry, history, "single_call", None, {}
    )

    assert state != DriftState.SPREAD_NARROWING, (
        f"SPREAD_NARROWING must not fire for single_call, got {state!r}"
    )
    # Low delta for single_call → DELTA_OTM (not SPREAD_NARROWING)
    assert state == DriftState.DELTA_OTM, (
        f"single_call with delta=0.07 should be DELTA_OTM, got {state!r}"
    )


# ── Test i — close_check_loop acts on urgency=immediate ───────────────────────

def test_close_check_loop_acts_on_immediate_intel(tmp_path, monkeypatch):
    """close_check_loop calls close_structure when intel urgency=immediate + gate enabled."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "options").mkdir(parents=True)
    (tmp_path / "data" / "account2" / "positions").mkdir(parents=True)
    (tmp_path / "data" / "account2" / "decisions").mkdir(parents=True)
    (tmp_path / "data" / "runtime").mkdir(parents=True)
    (tmp_path / "strategy_config.json").write_text(json.dumps({
        "account2": {"force_close_structures": []},
        "force_close_structures": [],
        "position_intel": {"act_on_immediate_close": True, "spread_narrowing_threshold": 0.10},
    }))
    (tmp_path / "data" / "account2" / "positions" / "structures.json").write_text("[]")

    # Write a minimal intel file with one immediate-close recommendation
    intel = {
        "generated_at": "2026-05-08T15:00:00+00:00",
        "cycle_id": "test",
        "schema_version": 1,
        "positions": {},
        "close_recommended": [
            {
                "symbol": "AAPL", "structure_id": "aapl-sid",
                "action": "CLOSE", "urgency": "immediate",
                "reason": "test immediate close", "drift_state": "DELTA_ITM",
            }
        ],
        "upgrade_recommended": [],
        "monitoring": [],
    }
    (tmp_path / "data" / "options" / "position_intel_latest.json").write_text(
        json.dumps(intel)
    )

    import options_executor as oe
    import options_position_manager as _opm

    struct = _make_structure(
        underlying="AAPL", structure_id="aapl-sid",
        pnl=500.0, filled_price=5.0, contracts=2,
    )
    struct.is_open.return_value = True

    mock_close = MagicMock(return_value=struct)
    mock_alpaca = MagicMock()
    mock_alpaca.get_all_positions.return_value = [
        SimpleNamespace(symbol="AAPL260718C00200000")
    ]

    with patch.object(oe, "should_close_structure", return_value=(False, "")):
        with patch.object(oe, "close_structure", mock_close):
            with patch("options_state.get_open_structures", return_value=[struct]):
                with patch("options_state.load_structures", return_value=[struct]):
                    with patch("options_state.save_structure"):
                        with patch.object(_opm, "is_immediate_close_action_enabled",
                                          return_value=True):
                            with patch.object(_opm, "get_recommendations") as mock_recs:
                                from options_position_manager import PositionAction
                                mock_recs.return_value = [
                                    PositionAction(
                                        action="CLOSE",
                                        reason="test immediate close",
                                        symbol="AAPL",
                                        structure_id="aapl-sid",
                                        urgency="immediate",
                                    )
                                ]
                                import bot_options_stage4_execution as s4
                                with patch.object(s4, "_sync_submitted_lifecycles"):
                                    with patch.object(s4, "_update_fill_prices"):
                                        with patch.object(s4, "_load_strategy_config",
                                                          return_value=json.loads(
                                                              (tmp_path / "strategy_config.json")
                                                              .read_text()
                                                          )):
                                            with patch.object(s4, "_write_a2_trade"):
                                                s4.close_check_loop(mock_alpaca)

    mock_close.assert_called_once(), (
        "close_structure must be called when intel urgency=immediate and gate enabled"
    )
