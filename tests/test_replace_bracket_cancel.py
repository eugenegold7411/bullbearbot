"""
tests/test_replace_bracket_cancel.py — REPLACE Phase A bracket-cancel pre-step.

8 tests covering portfolio_allocator._cancel_protective_orders and its wiring
into _execute_live_replace:

  1. cancel called before close (when bracket orders exist)
  2. close proceeds after a successful cancel-and-poll
  3. replace aborts (does not run Phase B) when CLOSE itself fails
  4. cancel not called when there are no bracket/OCO sell orders
  5. cancel logs '[REPLACE] {sym}: canceled N protective orders before close'
  6. config gate replace_cancel_brackets=False skips cancel entirely
  7. multiple bracket orders → all canceled in one pass
  8. end-to-end REPLACE: cancel → close fills → add fires → both phases ok

The order_executor module is shadow-injected into sys.modules so the lazy
`from order_executor import execute_all` and `from order_executor import
_get_alpaca` calls inside portfolio_allocator pick up the fakes. We set
`fake_oe._get_alpaca.return_value = fake_alpaca` so the real alpaca client is
never reached.
"""

from __future__ import annotations

import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

_ET = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def _freeze_market_hours():
    _am = datetime(2026, 5, 7, 10, 0, 0, tzinfo=_ET)
    with patch("risk_kernel._get_et_now", return_value=_am):
        yield


# ── shared helpers ────────────────────────────────────────────────────────────

def _raw_position(symbol: str, qty: float, current_price: float) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=symbol,
        qty=str(qty),
        current_price=str(current_price),
        market_value=str(qty * current_price),
        unrealized_pl="0",
    )


def _norm_pos(symbol: str, qty: float, market_value: float,
              avg_entry_price: float = 100.0) -> MagicMock:
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
    snap = MagicMock()
    snap.equity = equity
    snap.buying_power = buying_power
    snap.exposure_dollars = exposure_dollars
    snap.short_exposure_dollars = 0.0
    snap.positions = norm_positions or []
    snap.position_by_symbol = {p.symbol: p for p in (norm_positions or [])}
    return snap


def _base_cfg(extra_params: dict | None = None,
              extra_alloc: dict | None = None) -> dict:
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
    cfg = {
        "parameters":      params,
        "position_sizing": {
            "dynamic_tier_pct": 0.15,
            "core_tier_pct":    0.20,
        },
    }
    if extra_alloc:
        cfg["portfolio_allocator"] = extra_alloc
    return cfg


def _fake_order(
    side: str = "sell",
    order_type: str = "limit",
    order_class: str = "oco",
    order_id: str = "ord-1",
):
    """Plain object whose .side / .order_type / .order_class str() to alpaca-style enums."""
    return SimpleNamespace(
        id=order_id,
        side=f"OrderSide.{side.upper()}",
        order_type=f"OrderType.{order_type.upper()}",
        type=f"OrderType.{order_type.upper()}",
        order_class=f"OrderClass.{order_class.upper()}" if order_class else "",
        status="OPEN",
    )


def _fake_alpaca(initial_orders: list, *, persist_after_cancel: bool = False):
    """
    Fake Alpaca client.
    cancel_order_by_id() removes the order from the open set unless
    persist_after_cancel=True (used to simulate Alpaca paper PENDING_CANCEL
    persisting beyond the poll deadline).
    """
    state = {"orders": list(initial_orders)}
    cancel_calls: list[str] = []

    def get_orders(filter):  # noqa: A002 — alpaca SDK uses 'filter' kw
        return list(state["orders"])

    def cancel_order_by_id(order_id):
        cancel_calls.append(str(order_id))
        if not persist_after_cancel:
            state["orders"] = [o for o in state["orders"] if str(o.id) != str(order_id)]

    fake = MagicMock()
    fake.get_orders.side_effect         = get_orders
    fake.cancel_order_by_id.side_effect = cancel_order_by_id
    fake._cancel_calls = cancel_calls
    fake._state        = state
    return fake


def _fake_oe(fake_alpaca, return_value: list | None = None, side_effect=None) -> MagicMock:
    """Fake order_executor module to inject into sys.modules."""
    mod = MagicMock()
    mod.__name__ = "order_executor"
    if side_effect is not None:
        mod.execute_all.side_effect = side_effect
    else:
        mod.execute_all.return_value = return_value or []
    mod._get_alpaca.return_value = fake_alpaca
    return mod


# ── 1. cancel called before close ─────────────────────────────────────────────

def test_cancel_called_before_close():
    """A bracket-OCO sell on the exit symbol → cancel runs and CLOSE is then submitted."""
    from portfolio_allocator import _execute_live_replace

    positions = [_raw_position("ITA", 71.0, 50.0)]
    snapshot  = _make_snapshot()
    account   = MagicMock()

    close_result = MagicMock(); close_result.qty = 71
    buy_result   = MagicMock(); buy_result.qty   = 10
    mock_pi = MagicMock(side_effect=[close_result, buy_result])

    fake_alpaca = _fake_alpaca([_fake_order(order_id="oco-ITA-1")])
    fake_oe = _fake_oe(fake_alpaca)

    call_order: list[str] = []
    original_cancel = fake_alpaca.cancel_order_by_id.side_effect

    def trace_cancel(oid):
        call_order.append("cancel")
        original_cancel(oid)

    fake_alpaca.cancel_order_by_id.side_effect = trace_cancel
    fake_oe.execute_all.side_effect = lambda *a, **k: call_order.append("execute") or []

    with patch.dict(sys.modules, {"order_executor": fake_oe}):
        with patch("risk_kernel.process_idea", mock_pi):
            _execute_live_replace(
                exit_symbol="ITA", enter_symbol="DIS",
                positions=positions, snapshot=snapshot,
                cfg=_base_cfg(), session_tier="market",
                reason="replace ITA→DIS",
                enter_price=100.0, account=account,
            )

    assert "cancel" in call_order, "cancel was not called"
    assert "execute" in call_order, "execute_all was not called"
    assert call_order.index("cancel") < call_order.index("execute"), \
        "cancel must precede execute_all (CLOSE submission)"


# ── 2. close proceeds after cancel ────────────────────────────────────────────

def test_close_proceeds_after_cancel():
    """Cancel succeeds → execute_all called for Phase A CLOSE."""
    from portfolio_allocator import _execute_live_replace

    positions = [_raw_position("ITA", 71.0, 50.0)]
    snapshot  = _make_snapshot()
    account   = MagicMock()

    close_result = MagicMock(); close_result.qty = 71
    buy_result   = MagicMock(); buy_result.qty   = 10
    mock_pi = MagicMock(side_effect=[close_result, buy_result])

    fake_alpaca = _fake_alpaca([_fake_order(order_id="oco-ITA-1")])
    fake_oe = _fake_oe(fake_alpaca)

    with patch.dict(sys.modules, {"order_executor": fake_oe}):
        with patch("risk_kernel.process_idea", mock_pi):
            result = _execute_live_replace(
                exit_symbol="ITA", enter_symbol="DIS",
                positions=positions, snapshot=snapshot,
                cfg=_base_cfg(), session_tier="market",
                reason="replace ITA→DIS",
                enter_price=100.0, account=account,
            )

    assert result.startswith("ok:close=ITA"), f"unexpected result: {result}"
    assert fake_oe.execute_all.call_count >= 1


# ── 3. replace aborts if close fails ──────────────────────────────────────────

def test_replace_aborts_if_close_fails():
    """Cancel succeeds, CLOSE still rejected → Phase B not attempted."""
    from order_executor import ExecutionResult
    from portfolio_allocator import _execute_live_replace

    positions = [_raw_position("ITA", 71.0, 50.0)]
    snapshot  = _make_snapshot()
    account   = MagicMock()

    close_result = MagicMock(); close_result.qty = 71
    buy_result   = MagicMock(); buy_result.qty   = 10
    mock_pi = MagicMock(side_effect=[close_result, buy_result])

    rejected = ExecutionResult(symbol="ITA", action="close",
                               status="rejected", reason="still locked")
    fake_alpaca = _fake_alpaca([_fake_order(order_id="oco-ITA-1")])
    fake_oe = _fake_oe(fake_alpaca, return_value=[rejected])

    with patch.dict(sys.modules, {"order_executor": fake_oe}):
        with patch("risk_kernel.process_idea", mock_pi):
            result = _execute_live_replace(
                exit_symbol="ITA", enter_symbol="DIS",
                positions=positions, snapshot=snapshot,
                cfg=_base_cfg(), session_tier="market",
                reason="replace ITA→DIS",
                enter_price=100.0, account=account,
            )

    assert result.startswith("phase_a_failed:"), f"unexpected result: {result}"
    assert fake_oe.execute_all.call_count == 1, "Phase B must not run after Phase A failure"


# ── 4. no cancel when there are no brackets ───────────────────────────────────

def test_no_cancel_when_no_brackets():
    """No bracket/OCO/stop sells exist → no cancel calls; CLOSE still submitted."""
    from portfolio_allocator import _execute_live_replace

    positions = [_raw_position("ITA", 71.0, 50.0)]
    snapshot  = _make_snapshot()
    account   = MagicMock()

    close_result = MagicMock(); close_result.qty = 71
    buy_result   = MagicMock(); buy_result.qty   = 10
    mock_pi = MagicMock(side_effect=[close_result, buy_result])

    # Only an unrelated open BUY bracket primary — must NOT be canceled.
    fake_alpaca = _fake_alpaca([
        _fake_order(side="buy", order_type="market",
                    order_class="bracket", order_id="open-buy-1"),
    ])
    fake_oe = _fake_oe(fake_alpaca)

    with patch.dict(sys.modules, {"order_executor": fake_oe}):
        with patch("risk_kernel.process_idea", mock_pi):
            _execute_live_replace(
                exit_symbol="ITA", enter_symbol="DIS",
                positions=positions, snapshot=snapshot,
                cfg=_base_cfg(), session_tier="market",
                reason="replace ITA→DIS",
                enter_price=100.0, account=account,
            )

    fake_alpaca.cancel_order_by_id.assert_not_called()
    fake_oe.execute_all.assert_called()


# ── 5. cancel logs correctly ──────────────────────────────────────────────────

def test_cancel_logs_correctly(caplog):
    """[REPLACE] {sym}: canceled N protective orders before close — emitted at INFO."""
    import logging

    from portfolio_allocator import _cancel_protective_orders

    fake_alpaca = _fake_alpaca([
        _fake_order(order_id="oco-ITA-1"),
        _fake_order(order_id="oco-ITA-2"),
    ])
    fake_oe = _fake_oe(fake_alpaca)

    caplog.set_level(logging.INFO, logger="portfolio_allocator")
    with patch.dict(sys.modules, {"order_executor": fake_oe}):
        n = _cancel_protective_orders("ITA", poll_seconds=1.0)

    assert n == 2
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "[REPLACE] ITA: canceled 2 protective orders before close" in m
        for m in msgs
    ), f"missing expected log line. got: {msgs}"


# ── 6. config gate disables cancel ────────────────────────────────────────────

def test_config_gate_disables_cancel():
    """replace_cancel_brackets=False → cancel skipped (no Alpaca order calls at all)."""
    from portfolio_allocator import _execute_live_replace

    positions = [_raw_position("ITA", 71.0, 50.0)]
    snapshot  = _make_snapshot()
    account   = MagicMock()

    close_result = MagicMock(); close_result.qty = 71
    buy_result   = MagicMock(); buy_result.qty   = 10
    mock_pi = MagicMock(side_effect=[close_result, buy_result])

    fake_alpaca = _fake_alpaca([_fake_order(order_id="oco-ITA-1")])
    fake_oe = _fake_oe(fake_alpaca)

    cfg_off = _base_cfg(extra_alloc={"replace_cancel_brackets": False})

    with patch.dict(sys.modules, {"order_executor": fake_oe}):
        with patch("risk_kernel.process_idea", mock_pi):
            _execute_live_replace(
                exit_symbol="ITA", enter_symbol="DIS",
                positions=positions, snapshot=snapshot,
                cfg=cfg_off, session_tier="market",
                reason="replace ITA→DIS",
                enter_price=100.0, account=account,
            )

    fake_alpaca.cancel_order_by_id.assert_not_called()
    fake_alpaca.get_orders.assert_not_called()


# ── 7. multiple brackets all canceled ─────────────────────────────────────────

def test_multiple_brackets_all_canceled():
    """3 mixed protective sells → all 3 cancel calls submitted."""
    from portfolio_allocator import _cancel_protective_orders

    orders = [
        _fake_order(order_type="limit",  order_class="oco",     order_id="o1"),
        _fake_order(order_type="stop",   order_class="",        order_id="o2"),
        _fake_order(order_type="market", order_class="bracket", order_id="o3"),
    ]
    fake_alpaca = _fake_alpaca(orders)
    fake_oe = _fake_oe(fake_alpaca)

    with patch.dict(sys.modules, {"order_executor": fake_oe}):
        n = _cancel_protective_orders("ITA", poll_seconds=1.0)

    assert n == 3
    cancelled = sorted(fake_alpaca._cancel_calls)
    assert cancelled == ["o1", "o2", "o3"]


# ── 8. end-to-end REPLACE happy path ─────────────────────────────────────────

def test_replace_fires_end_to_end():
    """Full mock: cancel → close fills → add fires → 'ok:close=...+enter=...:qty'."""
    from portfolio_allocator import _execute_live_replace

    positions = [_raw_position("ITA", 71.0, 50.0)]
    snapshot  = _make_snapshot()
    account   = MagicMock()

    close_result = MagicMock(); close_result.qty = 71
    buy_result   = MagicMock(); buy_result.qty   = 12

    mock_pi = MagicMock(side_effect=[close_result, buy_result])

    fake_alpaca = _fake_alpaca([_fake_order(order_id="oco-ITA-1")])
    fake_oe = _fake_oe(fake_alpaca)

    with patch.dict(sys.modules, {"order_executor": fake_oe}):
        with patch("risk_kernel.process_idea", mock_pi):
            result = _execute_live_replace(
                exit_symbol="ITA", enter_symbol="DIS",
                positions=positions, snapshot=snapshot,
                cfg=_base_cfg(), session_tier="market",
                reason="replace ITA→DIS gap=20",
                enter_price=100.0, account=account,
            )

    assert result == "ok:close=ITA+enter=DIS:12"
    fake_alpaca.cancel_order_by_id.assert_called_once_with("oco-ITA-1")
    assert mock_pi.call_count == 2
    assert fake_oe.execute_all.call_count == 2
