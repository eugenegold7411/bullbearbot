"""
tests/test_allocator_live_add_replace.py — Stage 2 live ADD + REPLACE execution paths.

10 tests covering:
  1. ADD happy path — process_idea + execute_all called, "ok:..." returned
  2. ADD price not found in positions — returns error string, never executes
  3. ADD conviction gate exactly at threshold (0.6 < 0.6 = False → passes)
  4. ADD session_tier != market — eligibility rejects DYNAMIC BUY
  5. ADD VIX stressed (>=30) — DYNAMIC long entry blocked
  6. REPLACE happy path — Phase A close + Phase B buy both fire
  7. REPLACE Phase A process_idea rejects — phase_a_rejected, Phase B never fires
  8. REPLACE enter_price=0 — Phase A fires, Phase B gracefully skipped
  9. REPLACE Phase B process_idea rejects — Phase A succeeded, phase_b_rejected string
 10. REPLACE Phase A execute_all throws — phase_a_execute_error, Phase B never fires

NOTE: order_executor imports from alpaca.trading.enums at module level, which
fails on the local dev machine (PositionIntent not available in local Alpaca SDK).
All tests that touch _execute_live_add / _execute_live_replace inject a fake
order_executor into sys.modules via patch.dict so the local `from order_executor
import execute_all` inside those functions resolves without triggering the real
module-level import.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# ── Shared helpers ─────────────────────────────────────────────────────────────

def _raw_position(symbol: str, qty: float, current_price: float) -> SimpleNamespace:
    """Alpaca-like raw position object (as returned by get_all_positions())."""
    return SimpleNamespace(
        symbol=symbol,
        qty=str(qty),
        current_price=str(current_price),
        market_value=str(qty * current_price),
        unrealized_pl="0",
    )


def _norm_pos(symbol: str, qty: float, market_value: float,
              avg_entry_price: float = 100.0) -> MagicMock:
    """NormalizedPosition-like mock for BrokerSnapshot.positions."""
    p = MagicMock()
    p.symbol = symbol
    p.qty = qty
    p.market_value = market_value
    p.avg_entry_price = avg_entry_price
    return p


def _make_snapshot(
    equity: float = 100_000.0,
    buying_power: float = 50_000.0,
    exposure_dollars: float = 30_000.0,
    norm_positions: list | None = None,
) -> MagicMock:
    """BrokerSnapshot mock with the attributes process_idea and size_position read."""
    snap = MagicMock()
    snap.equity = equity
    snap.buying_power = buying_power
    snap.exposure_dollars = exposure_dollars
    snap.short_exposure_dollars = 0.0
    snap.positions = norm_positions or []
    snap.position_by_symbol = {p.symbol: p for p in (norm_positions or [])}
    return snap


def _base_cfg(extra_params: dict | None = None) -> dict:
    params = {
        "stop_loss_pct_core":              0.03,
        "take_profit_multiple":            2.5,
        "max_positions":                   30,
        "add_conviction_gate":             0.6,
        "margin_authorized":               True,
        "margin_sizing_multiplier":        2.0,
        "catalyst_tag_disallowed_values":  ["", "none", "null", "no"],
    }
    if extra_params:
        params.update(extra_params)
    return {
        "parameters":      params,
        "position_sizing": {
            "dynamic_tier_pct": 0.15,
            "core_tier_pct":    0.20,
        },
    }


def _fake_oe(
    return_value: list | None = None,
    side_effect=None,
) -> MagicMock:
    """Build a fake order_executor module object to inject into sys.modules."""
    mod = MagicMock()
    if side_effect is not None:
        mod.execute_all = MagicMock(side_effect=side_effect)
    else:
        mod.execute_all = MagicMock(return_value=return_value or [])
    mod.__name__ = "order_executor"
    return mod


# ── ADD tests ─────────────────────────────────────────────────────────────────

def test_add_happy_path():
    """ADD: process_idea + execute_all called with correct args, returns 'ok:{qty}'."""
    from portfolio_allocator import _execute_live_add

    raw_positions = [_raw_position("NVDA", 10.0, 150.0)]
    norm_pos = _norm_pos("NVDA", 10.0, 1500.0)
    snapshot  = _make_snapshot(norm_positions=[norm_pos])
    account   = MagicMock()

    mock_ba = MagicMock()
    mock_ba.qty = 3

    mock_pi = MagicMock(return_value=mock_ba)
    fake_oe  = _fake_oe()

    with patch.dict(sys.modules, {"order_executor": fake_oe}):
        with patch("risk_kernel.process_idea", mock_pi):
            result = _execute_live_add(
                symbol="NVDA",
                positions=raw_positions,
                snapshot=snapshot,
                cfg=_base_cfg(),
                session_tier="market",
                reason="thesis_score=8/10 — strong conviction",
                vix=18.0,
                account=account,
                market_status="open",
                minutes_since_open=60,
            )

    assert result == "ok:3"
    assert mock_pi.call_count == 1
    from schemas import AccountAction, Tier
    idea_arg = mock_pi.call_args.kwargs["idea"]
    assert idea_arg.action == AccountAction.BUY
    assert idea_arg.tier   == Tier.DYNAMIC
    assert abs(idea_arg.conviction - 0.6) < 1e-9
    assert "NVDA" in idea_arg.symbol
    fake_oe.execute_all.assert_called_once()


def test_add_price_not_found_returns_error():
    """ADD: symbol not in positions → returns error string, execute_all never called."""
    from portfolio_allocator import _execute_live_add

    raw_positions = [_raw_position("AAPL", 5.0, 200.0)]  # MSFT not in list
    snapshot = _make_snapshot()
    account  = MagicMock()

    mock_pi = MagicMock()
    fake_oe  = _fake_oe()

    with patch.dict(sys.modules, {"order_executor": fake_oe}):
        with patch("risk_kernel.process_idea", mock_pi):
            result = _execute_live_add(
                symbol="MSFT",
                positions=raw_positions,
                snapshot=snapshot,
                cfg=_base_cfg(),
                session_tier="market",
                reason="strong signal",
                account=account,
            )

    assert result == "no current_price for MSFT"
    mock_pi.assert_not_called()
    fake_oe.execute_all.assert_not_called()


def test_add_conviction_gate_exactly_at_threshold_passes():
    """
    ADD: conviction=0.6, add_conviction_gate=0.6.
    Gate check is `conviction < gate` (strict less-than): 0.6 < 0.6 = False → passes.
    Real risk_kernel runs; execute_all mocked.
    """
    from portfolio_allocator import _execute_live_add

    raw_positions = [_raw_position("AMD", 20.0, 100.0)]
    norm_pos = _norm_pos("AMD", 20.0, 2000.0)
    snapshot  = _make_snapshot(
        equity=100_000.0,
        buying_power=70_000.0,
        exposure_dollars=30_000.0,
        norm_positions=[norm_pos],
    )
    account = MagicMock()
    cfg = _base_cfg({"add_conviction_gate": 0.6})

    fake_oe = _fake_oe()

    with patch.dict(sys.modules, {"order_executor": fake_oe}):
        # Real risk_kernel — kernel must approve the trade (conviction=0.6 passes gate)
        result = _execute_live_add(
            symbol="AMD",
            positions=raw_positions,
            snapshot=snapshot,
            cfg=cfg,
            session_tier="market",
            reason="add to strong position",
            account=account,
            vix=20.0,
        )

    # Kernel approved → execute_all was called → function returned "ok:{qty}"
    assert result.startswith("ok:"), f"Unexpected result: {result!r}"
    fake_oe.execute_all.assert_called_once()


def test_add_session_tier_not_market_rejects():
    """
    ADD: session_tier='pre_market' → eligibility_check rejects DYNAMIC stock BUY.
    Real risk_kernel runs; result is a rejection string.
    """
    from portfolio_allocator import _execute_live_add

    raw_positions = [_raw_position("TSLA", 10.0, 200.0)]
    norm_pos = _norm_pos("TSLA", 10.0, 2000.0)
    snapshot  = _make_snapshot(norm_positions=[norm_pos])
    account   = MagicMock()

    fake_oe = _fake_oe()

    with patch.dict(sys.modules, {"order_executor": fake_oe}):
        result = _execute_live_add(
            symbol="TSLA",
            positions=raw_positions,
            snapshot=snapshot,
            cfg=_base_cfg(),
            session_tier="pre_market",   # not "market"
            reason="strong add signal",
            account=account,
            vix=20.0,
        )

    # Risk kernel session gate fires
    assert "session" in result.lower() or "market" in result.lower(), (
        f"Expected session rejection, got: {result!r}"
    )
    fake_oe.execute_all.assert_not_called()


def test_add_vix_stressed_blocks_dynamic():
    """
    ADD: VIX=32 (stressed regime ≥30) → DYNAMIC tier long entry blocked by risk_kernel.
    Real risk_kernel runs; result is a rejection string.
    """
    from portfolio_allocator import _execute_live_add

    raw_positions = [_raw_position("META", 5.0, 500.0)]
    norm_pos = _norm_pos("META", 5.0, 2500.0)
    snapshot  = _make_snapshot(norm_positions=[norm_pos])
    account   = MagicMock()

    fake_oe = _fake_oe()

    with patch.dict(sys.modules, {"order_executor": fake_oe}):
        result = _execute_live_add(
            symbol="META",
            positions=raw_positions,
            snapshot=snapshot,
            cfg=_base_cfg(),
            session_tier="market",
            reason="strong thesis",
            account=account,
            vix=32.0,  # stressed: VIX >= 30, DYNAMIC blocked
        )

    # Risk kernel VIX gate: "VIX 32.0 >= 30 — stressed regime, dynamic long entries blocked"
    assert "stressed" in result.lower() or "dynamic" in result.lower() or "VIX" in result, (
        f"Expected VIX stress rejection, got: {result!r}"
    )
    fake_oe.execute_all.assert_not_called()


# ── REPLACE tests ─────────────────────────────────────────────────────────────

def test_replace_happy_path_both_phases():
    """REPLACE: Phase A closes exit_symbol, Phase B enters enter_symbol."""
    from portfolio_allocator import _execute_live_replace

    positions = [_raw_position("INTC", 50.0, 40.0)]
    snapshot  = _make_snapshot()
    account   = MagicMock()

    close_result = MagicMock()
    close_result.qty = 50

    buy_result = MagicMock()
    buy_result.qty = 8

    mock_pi = MagicMock(side_effect=[close_result, buy_result])
    fake_oe  = _fake_oe()

    with patch.dict(sys.modules, {"order_executor": fake_oe}):
        with patch("risk_kernel.process_idea", mock_pi):
            result = _execute_live_replace(
                exit_symbol="INTC",
                enter_symbol="NVDA",
                positions=positions,
                snapshot=snapshot,
                cfg=_base_cfg(),
                session_tier="market",
                reason="candidate gap=20 exceeds threshold",
                enter_price=150.0,
                vix=18.0,
                account=account,
            )

    assert result == "ok:close=INTC+enter=NVDA:8"
    assert mock_pi.call_count == 2
    assert fake_oe.execute_all.call_count == 2


def test_replace_phase_a_process_idea_rejects():
    """REPLACE: Phase A process_idea rejects → phase_a_rejected, Phase B never fires."""
    from portfolio_allocator import _execute_live_replace

    positions = [_raw_position("INTC", 50.0, 40.0)]
    snapshot  = _make_snapshot()
    account   = MagicMock()

    mock_pi = MagicMock(return_value="no open position for INTC to close")
    fake_oe  = _fake_oe()

    with patch.dict(sys.modules, {"order_executor": fake_oe}):
        with patch("risk_kernel.process_idea", mock_pi):
            result = _execute_live_replace(
                exit_symbol="INTC",
                enter_symbol="NVDA",
                positions=positions,
                snapshot=snapshot,
                cfg=_base_cfg(),
                session_tier="market",
                reason="replace INTC with NVDA",
                enter_price=150.0,
                account=account,
            )

    assert result.startswith("phase_a_rejected:")
    assert "no open position" in result
    mock_pi.assert_called_once()     # only Phase A's process_idea called
    fake_oe.execute_all.assert_not_called()


def test_replace_enter_price_zero_phase_b_skipped():
    """
    REPLACE: enter_price=0 → Phase A fires, Phase B gracefully skipped.
    Returns "ok:close={exit};phase_b_no_price:{enter}".
    """
    from portfolio_allocator import _execute_live_replace

    positions = [_raw_position("IBM", 20.0, 150.0)]
    snapshot  = _make_snapshot()
    account   = MagicMock()

    close_result = MagicMock()
    close_result.qty = 20

    mock_pi = MagicMock(return_value=close_result)
    fake_oe  = _fake_oe()

    with patch.dict(sys.modules, {"order_executor": fake_oe}):
        with patch("risk_kernel.process_idea", mock_pi):
            result = _execute_live_replace(
                exit_symbol="IBM",
                enter_symbol="MSFT",
                positions=positions,
                snapshot=snapshot,
                cfg=_base_cfg(),
                session_tier="market",
                reason="replace IBM with MSFT",
                enter_price=0,   # no price available
                account=account,
            )

    assert "ok:close=IBM" in result
    assert "phase_b_no_price" in result
    assert "MSFT" in result
    mock_pi.assert_called_once()        # only Phase A's process_idea called
    fake_oe.execute_all.assert_called_once()  # only Phase A's execute_all called


def test_replace_phase_b_process_idea_rejects():
    """
    REPLACE: Phase B process_idea rejects enter side.
    Phase A already succeeded; returns "ok:close={exit};phase_b_rejected:{reason}".
    """
    from portfolio_allocator import _execute_live_replace

    positions = [_raw_position("GE", 100.0, 10.0)]
    snapshot  = _make_snapshot()
    account   = MagicMock()

    close_result = MagicMock()
    close_result.qty = 100

    mock_pi = MagicMock(side_effect=[close_result, "max_positions=30 reached"])
    fake_oe  = _fake_oe()

    with patch.dict(sys.modules, {"order_executor": fake_oe}):
        with patch("risk_kernel.process_idea", mock_pi):
            result = _execute_live_replace(
                exit_symbol="GE",
                enter_symbol="AMD",
                positions=positions,
                snapshot=snapshot,
                cfg=_base_cfg(),
                session_tier="market",
                reason="replace GE with AMD",
                enter_price=100.0,
                account=account,
            )

    assert "ok:close=GE" in result
    assert "phase_b_rejected" in result
    assert "max_positions" in result
    assert mock_pi.call_count == 2       # both phases' process_idea called
    fake_oe.execute_all.assert_called_once()  # only Phase A's execute_all called


def test_replace_phase_a_execute_error_blocks_phase_b():
    """
    REPLACE: Phase A execute_all raises → phase_a_execute_error returned,
    Phase B's execute_all never called.
    """
    from portfolio_allocator import _execute_live_replace

    positions = [_raw_position("F", 200.0, 12.0)]
    snapshot  = _make_snapshot()
    account   = MagicMock()

    close_result = MagicMock()
    close_result.qty = 200

    buy_result = MagicMock()
    buy_result.qty = 10

    call_count = {"n": 0}

    def fake_ea(actions, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("Alpaca connection timeout")
        return []

    mock_pi = MagicMock(side_effect=[close_result, buy_result])
    fake_oe  = _fake_oe(side_effect=fake_ea)

    with patch.dict(sys.modules, {"order_executor": fake_oe}):
        with patch("risk_kernel.process_idea", mock_pi):
            result = _execute_live_replace(
                exit_symbol="F",
                enter_symbol="GM",
                positions=positions,
                snapshot=snapshot,
                cfg=_base_cfg(),
                session_tier="market",
                reason="replace F with GM",
                enter_price=40.0,
                account=account,
            )

    assert result.startswith("phase_a_execute_error:")
    assert "Alpaca connection timeout" in result
    assert call_count["n"] == 1   # Phase B execute_all never called
