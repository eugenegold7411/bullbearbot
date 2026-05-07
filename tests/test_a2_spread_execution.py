"""
tests/test_a2_spread_execution.py — A2 spread execution bug fixes (8 required tests).

Fix A: position intent — use BUY_TO_CLOSE / SELL_TO_CLOSE only when Alpaca already holds
       a matching position whose qty sign justifies it:
         SELL leg + existing LONG (qty > 0) → SELL_TO_CLOSE
         BUY  leg + existing SHORT (qty < 0) → BUY_TO_CLOSE
       Otherwise _TO_OPEN. The qty-naive check (any position → CLOSE) is wrong and
       triggers 42210000 when a BUY leg has an existing LONG (adding to position, not closing).

Fix B: TIF — all spreads now use GTC. Preflight max_age guard keeps fresh mleg orders alive
       for up to mleg_max_age_minutes (default 30) before cancelling and re-pricing.

Tests:
  Intent tests (Fix A):
  1. test_sell_to_close_when_long_exists
  2. test_buy_to_close_when_short_exists
  3. test_sell_to_open_no_existing
  4. test_buy_to_open_no_existing
  5. test_mixed_legs_correct_intents

  TIF tests (Fix B — submit side):
  6. test_mleg_order_uses_gtc

  Preflight max_age tests (Fix B — cancel side):
  7. test_preflight_cancels_stale_gtc_mleg
  8. test_preflight_keeps_fresh_gtc_mleg

  Regression:
  9.  test_credit_spread_uses_gtc
  10. test_buy_to_open_when_long_exists  (BUY leg + existing LONG → still BUY_TO_OPEN)
  11. test_sell_to_open_when_short_exists (SELL leg + existing SHORT → still SELL_TO_OPEN)
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

# ── Alpaca stubs ──────────────────────────────────────────────────────────────
# Each test that calls _submit_spread_mleg wraps its call in
# `with patch.dict(sys.modules, _make_alpaca_stubs())` so it is immune to all
# sys.modules contamination that happens at collection time (test_trim_exposure_clean.py,
# test_a2_decision_store.py, etc.).

class _PositionIntent:
    BUY_TO_OPEN   = "buy_to_open"
    BUY_TO_CLOSE  = "buy_to_close"
    SELL_TO_OPEN  = "sell_to_open"
    SELL_TO_CLOSE = "sell_to_close"


class _TimeInForce:
    DAY = "day"
    GTC = "gtc"


class _OrderClass:
    MLEG   = "mleg"
    SIMPLE = "simple"


class _MockOptionLegRequest:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class _MockLimitOrderRequest:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def _make_alpaca_stubs() -> dict:
    enums_mod = MagicMock()
    enums_mod.PositionIntent = _PositionIntent
    enums_mod.TimeInForce    = _TimeInForce
    enums_mod.OrderClass     = _OrderClass

    requests_mod = MagicMock()
    requests_mod.LimitOrderRequest  = _MockLimitOrderRequest
    requests_mod.OptionLegRequest   = _MockOptionLegRequest

    return {
        "alpaca":                 MagicMock(),
        "alpaca.trading":         MagicMock(),
        "alpaca.trading.enums":   enums_mod,
        "alpaca.trading.requests": requests_mod,
        "alpaca.trading.client":  MagicMock(),
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_spread_structure(
    strategy_name: str = "call_debit_spread",
    long_occ: str = "NVDA260619C00380000",
    short_occ: str = "NVDA260619C00385000",
    long_bid: float = 3.50, long_ask: float = 3.70,
    short_bid: float = 1.80, short_ask: float = 2.00,
    opened_at: str | None = None,
):
    from schemas import (
        OptionsLeg,
        OptionsStructure,
        OptionStrategy,
        StructureLifecycle,
        Tier,
    )
    strategy_map = {
        "call_debit_spread":  OptionStrategy.CALL_DEBIT_SPREAD,
        "call_credit_spread": OptionStrategy.CALL_CREDIT_SPREAD,
        "put_debit_spread":   OptionStrategy.PUT_DEBIT_SPREAD,
    }
    long_leg = OptionsLeg(
        occ_symbol=long_occ, underlying="NVDA", side="buy", qty=1,
        option_type="call", strike=380.0, expiration="2026-06-19",
        bid=long_bid, ask=long_ask,
    )
    short_leg = OptionsLeg(
        occ_symbol=short_occ, underlying="NVDA", side="sell", qty=1,
        option_type="call", strike=385.0, expiration="2026-06-19",
        bid=short_bid, ask=short_ask,
    )
    return OptionsStructure(
        structure_id="test-spread-001",
        underlying="NVDA",
        strategy=strategy_map[strategy_name],
        lifecycle=StructureLifecycle.PROPOSED,
        legs=[long_leg, short_leg],
        contracts=1,
        max_cost_usd=200.0,
        opened_at=opened_at or datetime.now(timezone.utc).isoformat(),
        catalyst="test",
        tier=Tier.DYNAMIC,
    )


def _mock_client(existing_positions: dict[str, float] | None = None) -> MagicMock:
    """Trading client mock with configurable existing positions (symbol → qty)."""
    client = MagicMock()
    positions = []
    for sym, qty in (existing_positions or {}).items():
        p = MagicMock()
        p.symbol = sym
        p.qty = qty
        positions.append(p)
    client.get_all_positions.return_value = positions
    fake_order = MagicMock()
    fake_order.id = "order-test-uuid"
    client.submit_order.return_value = fake_order
    return client


def _get_submitted_req(client: MagicMock):
    return client.submit_order.call_args[0][0]


def _intents_by_sym(req) -> dict:
    return {leg.symbol: leg.position_intent for leg in req.legs}


# ── Tests 1–5: Intent fix (Fix A) ─────────────────────────────────────────────

def test_sell_to_close_when_long_exists():
    """SELL leg + existing LONG (qty > 0) → SELL_TO_CLOSE to avoid 42210000."""
    long_occ  = "NVDA260619C00380000"
    short_occ = "NVDA260619C00385000"
    structure = _make_spread_structure(long_occ=long_occ, short_occ=short_occ)
    client    = _mock_client({short_occ: 10.0})  # sell leg has an existing LONG

    with patch.dict(sys.modules, _make_alpaca_stubs()):
        from options_executor import _submit_spread_mleg
        _submit_spread_mleg(structure, client, config={})

    intents = _intents_by_sym(_get_submitted_req(client))
    assert intents[short_occ] == _PositionIntent.SELL_TO_CLOSE, (
        f"SELL leg with existing LONG must use SELL_TO_CLOSE, got {intents[short_occ]!r}"
    )
    assert intents[long_occ] == _PositionIntent.BUY_TO_OPEN


def test_buy_to_close_when_short_exists():
    """BUY leg + existing SHORT (qty < 0) → BUY_TO_CLOSE to avoid 42210000."""
    long_occ  = "NVDA260619C00380000"
    short_occ = "NVDA260619C00385000"
    structure = _make_spread_structure(long_occ=long_occ, short_occ=short_occ)
    client    = _mock_client({long_occ: -10.0})  # buy leg has an existing SHORT

    with patch.dict(sys.modules, _make_alpaca_stubs()):
        from options_executor import _submit_spread_mleg
        _submit_spread_mleg(structure, client, config={})

    intents = _intents_by_sym(_get_submitted_req(client))
    assert intents[long_occ] == _PositionIntent.BUY_TO_CLOSE, (
        f"BUY leg with existing SHORT must use BUY_TO_CLOSE, got {intents[long_occ]!r}"
    )
    assert intents[short_occ] == _PositionIntent.SELL_TO_OPEN


def test_sell_to_open_no_existing():
    """SELL leg with no existing position → SELL_TO_OPEN."""
    short_occ = "NVDA260619C00385000"
    structure = _make_spread_structure()
    client    = _mock_client({})

    with patch.dict(sys.modules, _make_alpaca_stubs()):
        from options_executor import _submit_spread_mleg
        _submit_spread_mleg(structure, client, config={})

    intents = _intents_by_sym(_get_submitted_req(client))
    assert intents[short_occ] == _PositionIntent.SELL_TO_OPEN


def test_buy_to_open_no_existing():
    """BUY leg with no existing position → BUY_TO_OPEN."""
    long_occ  = "NVDA260619C00380000"
    structure = _make_spread_structure()
    client    = _mock_client({})

    with patch.dict(sys.modules, _make_alpaca_stubs()):
        from options_executor import _submit_spread_mleg
        _submit_spread_mleg(structure, client, config={})

    intents = _intents_by_sym(_get_submitted_req(client))
    assert intents[long_occ] == _PositionIntent.BUY_TO_OPEN


def test_mixed_legs_correct_intents():
    """Buy leg has existing SHORT → BUY_TO_CLOSE; sell leg has no position → SELL_TO_OPEN."""
    long_occ  = "NVDA260619C00380000"
    short_occ = "NVDA260619C00385000"
    structure = _make_spread_structure(long_occ=long_occ, short_occ=short_occ)
    client    = _mock_client({long_occ: -5.0})  # short position on buy leg only

    with patch.dict(sys.modules, _make_alpaca_stubs()):
        from options_executor import _submit_spread_mleg
        _submit_spread_mleg(structure, client, config={})

    intents = _intents_by_sym(_get_submitted_req(client))
    assert intents[long_occ]  == _PositionIntent.BUY_TO_CLOSE
    assert intents[short_occ] == _PositionIntent.SELL_TO_OPEN


# ── Test 6: TIF fix (Fix B submit side) ──────────────────────────────────────

def test_mleg_order_uses_gtc():
    """All spread orders must use GTC so preflight (not session close) controls expiry."""
    structure = _make_spread_structure("call_debit_spread")
    client    = _mock_client()

    with patch.dict(sys.modules, _make_alpaca_stubs()):
        from options_executor import _submit_spread_mleg
        _submit_spread_mleg(structure, client, config={})

    req = _get_submitted_req(client)
    assert req.time_in_force == _TimeInForce.GTC, (
        f"Expected GTC but got {req.time_in_force!r} — spread will expire after ~2 min"
    )


# ── Tests 7–8: Preflight max_age guard (Fix B cancel side) ───────────────────

def _make_submitted_structure(age_minutes: float, strategy_name: str = "call_debit_spread"):
    """Return a SUBMITTED spread structure with opened_at set age_minutes ago."""
    from schemas import StructureLifecycle
    opened_at = (datetime.now(timezone.utc) - timedelta(minutes=age_minutes)).isoformat()
    s = _make_spread_structure(strategy_name=strategy_name, opened_at=opened_at)
    s.lifecycle = StructureLifecycle.SUBMITTED
    s.order_ids = ["test-order-id-001"]
    return s


def test_preflight_cancels_stale_gtc_mleg():
    """GTC mleg order older than max_age_minutes must be cancelled and re-enter the pool."""
    from bot_options_stage0_preflight import _cancel_and_clear_unfilled_orders

    stale  = _make_submitted_structure(age_minutes=35.0)  # > default 30
    alpaca = MagicMock()
    alpaca.get_order_by_id.return_value = MagicMock(filled_qty=0)

    config = {"account2": {"mleg_max_age_minutes": 30.0}}

    with (
        patch("options_state.load_structures", return_value=[stale]),
        patch("options_state.save_structure"),
    ):
        count, underlyings = _cancel_and_clear_unfilled_orders(alpaca, config)

    assert count == 1, f"Expected 1 cancellation for stale mleg, got {count}"
    assert "NVDA" in underlyings
    alpaca.cancel_order_by_id.assert_called_once_with("test-order-id-001")


def test_preflight_keeps_fresh_gtc_mleg():
    """GTC mleg order younger than max_age_minutes must NOT be cancelled."""
    from bot_options_stage0_preflight import _cancel_and_clear_unfilled_orders

    fresh  = _make_submitted_structure(age_minutes=10.0)  # < default 30
    alpaca = MagicMock()
    alpaca.get_order_by_id.return_value = MagicMock(filled_qty=0)

    config = {"account2": {"mleg_max_age_minutes": 30.0}}

    with (
        patch("options_state.load_structures", return_value=[fresh]),
        patch("options_state.save_structure"),
    ):
        count, underlyings = _cancel_and_clear_unfilled_orders(alpaca, config)

    assert count == 0, f"Fresh mleg should NOT be cancelled, got count={count}"
    alpaca.cancel_order_by_id.assert_not_called()


# ── Regression tests ──────────────────────────────────────────────────────────

def test_credit_spread_uses_gtc():
    """Credit spread must use GTC after Fix B (no regression)."""
    structure = _make_spread_structure(
        "call_credit_spread",
        long_bid=0.20, long_ask=0.30,
        short_bid=1.80, short_ask=2.00,
    )
    client = _mock_client()

    with patch.dict(sys.modules, _make_alpaca_stubs()):
        from options_executor import _submit_spread_mleg
        _submit_spread_mleg(structure, client, config={})

    req = _get_submitted_req(client)
    assert req.time_in_force == _TimeInForce.GTC


def test_buy_to_open_when_long_exists():
    """BUY leg + existing LONG (qty > 0) → BUY_TO_OPEN (adding to long, not closing short)."""
    long_occ  = "NVDA260619C00380000"
    structure = _make_spread_structure(long_occ=long_occ)
    client    = _mock_client({long_occ: 10.0})  # existing LONG on buy leg

    with patch.dict(sys.modules, _make_alpaca_stubs()):
        from options_executor import _submit_spread_mleg
        _submit_spread_mleg(structure, client, config={})

    intents = _intents_by_sym(_get_submitted_req(client))
    assert intents[long_occ] == _PositionIntent.BUY_TO_OPEN, (
        f"BUY leg + existing LONG must use BUY_TO_OPEN (not CLOSE), got {intents[long_occ]!r}"
    )


def test_sell_to_open_when_short_exists():
    """SELL leg + existing SHORT (qty < 0) → SELL_TO_OPEN (adding to short, not closing long)."""
    short_occ = "NVDA260619C00385000"
    structure = _make_spread_structure(short_occ=short_occ)
    client    = _mock_client({short_occ: -10.0})  # existing SHORT on sell leg

    with patch.dict(sys.modules, _make_alpaca_stubs()):
        from options_executor import _submit_spread_mleg
        _submit_spread_mleg(structure, client, config={})

    intents = _intents_by_sym(_get_submitted_req(client))
    assert intents[short_occ] == _PositionIntent.SELL_TO_OPEN, (
        f"SELL leg + existing SHORT must use SELL_TO_OPEN (not CLOSE), got {intents[short_occ]!r}"
    )
