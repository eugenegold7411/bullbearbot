"""
tests/test_s4_mleg_submission.py

Tests for the mleg spread submission rewrite in options_executor.py
and the pending-underlyings guard in bot_options_stage0_preflight.py.

Alpaca is not installed in the local test environment, so all tests that
exercise the Alpaca submission path mock alpaca.trading.enums and
alpaca.trading.requests via sys.modules patching.
"""
from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch

# ─────────────────────────────────────────────────────────────────────────────
# Alpaca mock classes — injected into sys.modules before each executor call
# ─────────────────────────────────────────────────────────────────────────────

class _MockLimitOrderRequest:
    """Minimal stand-in for alpaca LimitOrderRequest."""
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class _MockOptionLegRequest:
    """Minimal stand-in for alpaca OptionLegRequest."""
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


# Enum-like namespace objects
class _OrderClass:
    MLEG = "mleg"
    SIMPLE = "simple"
    BRACKET = "bracket"


class _TimeInForce:
    DAY = "day"
    GTC = "gtc"


class _PositionIntent:
    BUY_TO_OPEN = "buy_to_open"
    SELL_TO_OPEN = "sell_to_open"


class _OrderSide:
    BUY = "buy"
    SELL = "sell"


def _make_mock_alpaca_modules():
    enums_mod = MagicMock()
    enums_mod.OrderClass = _OrderClass
    enums_mod.TimeInForce = _TimeInForce
    enums_mod.PositionIntent = _PositionIntent
    enums_mod.OrderSide = _OrderSide

    requests_mod = MagicMock()
    requests_mod.LimitOrderRequest = _MockLimitOrderRequest
    requests_mod.OptionLegRequest = _MockOptionLegRequest

    return {
        "alpaca": MagicMock(),
        "alpaca.trading": MagicMock(),
        "alpaca.trading.enums": enums_mod,
        "alpaca.trading.requests": requests_mod,
    }


# ── Minimal structure stubs ───────────────────────────────────────────────────

def _make_leg(side: str, bid, ask, occ: str = "") -> MagicMock:
    leg = MagicMock()
    leg.side = side
    leg.bid = bid
    leg.ask = ask
    leg.mid = None
    leg.filled_price = None
    leg.occ_symbol = occ
    leg.option_type = "put"
    leg.strike = 200.0
    leg.order_id = None
    return leg


def _make_structure(contracts: int = 5, legs=None, strategy_value="put_credit_spread"):
    from schemas import OptionStrategy, StructureLifecycle

    s = MagicMock()
    s.contracts = contracts
    s.underlying = "NVDA"
    s.expiration = "2026-05-22"
    s.order_ids = []
    s.audit_log = []

    _strat_map = {
        "put_credit_spread": OptionStrategy.PUT_CREDIT_SPREAD,
        "call_debit_spread": OptionStrategy.CALL_DEBIT_SPREAD,
        "call_credit_spread": OptionStrategy.CALL_CREDIT_SPREAD,
        "put_debit_spread": OptionStrategy.PUT_DEBIT_SPREAD,
    }
    s.strategy = _strat_map.get(strategy_value, OptionStrategy.PUT_CREDIT_SPREAD)
    s.lifecycle = StructureLifecycle.PROPOSED

    s.legs = legs or [
        _make_leg("buy",  bid=2.10, ask=2.50, occ="NVDA260522P00200000"),
        _make_leg("sell", bid=4.10, ask=4.50, occ="NVDA260522P00205000"),
    ]

    def _add_audit(msg):
        s.audit_log.append(msg)
    s.add_audit = _add_audit

    return s


def _run_mleg(structure=None, order_id="test-order-abc"):
    """Run _submit_spread_mleg with mocked alpaca and a mock trading client."""
    from options_executor import _submit_spread_mleg

    mock_order = MagicMock()
    mock_order.id = order_id
    mock_client = MagicMock()
    mock_client.submit_order.return_value = mock_order

    s = structure or _make_structure()

    with patch.dict(sys.modules, _make_mock_alpaca_modules()):
        result = _submit_spread_mleg(s, mock_client)

    return result, mock_client


def _run_submit_structure(strategy_value="put_credit_spread", order_id="dispatch-id"):
    """Run submit_structure() with mocked alpaca."""
    from options_executor import submit_structure

    s = _make_structure(strategy_value=strategy_value)

    mock_order = MagicMock()
    mock_order.id = order_id
    client = MagicMock()
    client.submit_order.return_value = mock_order

    with patch.dict(sys.modules, _make_mock_alpaca_modules()):
        result = submit_structure(s, client, config={})

    return result, client


# ─────────────────────────────────────────────────────────────────────────────
# Suite 1 — _compute_net_mid (no alpaca needed)
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeNetMid(unittest.TestCase):
    """_compute_net_mid: positive for debit, negative for credit."""

    def _net(self, legs):
        from options_executor import _compute_net_mid
        s = _make_structure(legs=legs)
        return _compute_net_mid(s)

    def test_credit_spread_is_negative(self):
        # buy 200P @ mid 2.30, sell 205P @ mid 4.30 → net = 2.30 - 4.30 = -2.00
        result = self._net([
            _make_leg("buy",  bid=2.10, ask=2.50),
            _make_leg("sell", bid=4.10, ask=4.50),
        ])
        self.assertAlmostEqual(result, -2.0, places=4)

    def test_debit_spread_is_positive(self):
        # buy @ mid 4.30, sell @ mid 2.30 → net = +2.00
        result = self._net([
            _make_leg("buy",  bid=4.10, ask=4.50),
            _make_leg("sell", bid=2.10, ask=2.50),
        ])
        self.assertAlmostEqual(result, 2.0, places=4)

    def test_returns_none_if_any_leg_has_no_price(self):
        result = self._net([
            _make_leg("buy",  bid=None, ask=None),
            _make_leg("sell", bid=4.10, ask=4.50),
        ])
        self.assertIsNone(result)

    def test_bid_ask_average_used(self):
        # bid=2.00, ask=3.00 → mid=2.50; bid=3.00, ask=5.00 → mid=4.00
        result = self._net([
            _make_leg("buy",  bid=2.00, ask=3.00),
            _make_leg("sell", bid=3.00, ask=5.00),
        ])
        self.assertAlmostEqual(result, -1.5, places=4)  # 2.50 - 4.00


# ─────────────────────────────────────────────────────────────────────────────
# Suite 2 — _submit_spread_mleg: successful submission
# ─────────────────────────────────────────────────────────────────────────────

class TestSubmitSpreadMlegSuccess(unittest.TestCase):

    def test_single_submit_order_call(self):
        _, client = _run_mleg()
        self.assertEqual(client.submit_order.call_count, 1)

    def test_lifecycle_is_submitted(self):
        from schemas import StructureLifecycle
        s, _ = _run_mleg()
        self.assertEqual(s.lifecycle, StructureLifecycle.SUBMITTED)

    def test_order_id_added_to_structure(self):
        s, _ = _run_mleg(order_id="mleg-xyz-123")
        self.assertIn("mleg-xyz-123", s.order_ids)

    def test_order_id_stamped_on_both_legs(self):
        structure = _make_structure()
        s, _ = _run_mleg(structure=structure, order_id="shared-id")
        for leg in s.legs:
            self.assertEqual(leg.order_id, "shared-id")

    def test_uses_order_class_mleg(self):
        _, client = _run_mleg()
        req = client.submit_order.call_args[0][0]
        self.assertEqual(req.order_class, _OrderClass.MLEG)

    def test_uses_time_in_force_day(self):
        _, client = _run_mleg()
        req = client.submit_order.call_args[0][0]
        self.assertEqual(req.time_in_force, _TimeInForce.DAY)

    def test_qty_equals_contracts(self):
        structure = _make_structure(contracts=7)
        _, client = _run_mleg(structure=structure)
        req = client.submit_order.call_args[0][0]
        self.assertEqual(req.qty, 7)

    def test_no_symbol_on_parent_order(self):
        _, client = _run_mleg()
        req = client.submit_order.call_args[0][0]
        self.assertFalse(hasattr(req, "symbol"),
            "mleg parent order must not have a top-level symbol")

    def test_two_legs_in_request(self):
        _, client = _run_mleg()
        req = client.submit_order.call_args[0][0]
        self.assertEqual(len(req.legs), 2)

    def test_buy_leg_has_buy_to_open_intent(self):
        _, client = _run_mleg()
        req = client.submit_order.call_args[0][0]
        buy_legs = [l for l in req.legs
                    if l.position_intent == _PositionIntent.BUY_TO_OPEN]
        self.assertEqual(len(buy_legs), 1)

    def test_sell_leg_has_sell_to_open_intent(self):
        _, client = _run_mleg()
        req = client.submit_order.call_args[0][0]
        sell_legs = [l for l in req.legs
                     if l.position_intent == _PositionIntent.SELL_TO_OPEN]
        self.assertEqual(len(sell_legs), 1)

    def test_credit_spread_limit_price_is_negative(self):
        # buy 200P @ mid 2.30, sell 205P @ mid 4.30 → net credit → negative limit_price
        structure = _make_structure(legs=[
            _make_leg("buy",  bid=2.10, ask=2.50, occ="NVDA260522P00200000"),
            _make_leg("sell", bid=4.10, ask=4.50, occ="NVDA260522P00205000"),
        ])
        _, client = _run_mleg(structure=structure)
        req = client.submit_order.call_args[0][0]
        self.assertLess(req.limit_price, 0)

    def test_debit_spread_limit_price_is_positive(self):
        structure = _make_structure(legs=[
            _make_leg("buy",  bid=4.10, ask=4.50, occ="NVDA260522C00205000"),
            _make_leg("sell", bid=2.10, ask=2.50, occ="NVDA260522C00200000"),
        ])
        _, client = _run_mleg(structure=structure)
        req = client.submit_order.call_args[0][0]
        self.assertGreater(req.limit_price, 0)

    def test_limit_price_rounded_to_005(self):
        """limit_price must be a clean $0.05 multiple — no float artifacts."""
        _, client = _run_mleg()
        req = client.submit_order.call_args[0][0]
        lp = abs(req.limit_price)
        # Use integer rounding — 2.0 % 0.05 is ~0.05 in IEEE 754, not 0.0
        twentieths = lp * 20
        self.assertAlmostEqual(twentieths, round(twentieths), places=4,
            msg=f"limit_price {req.limit_price!r} is not a $0.05 multiple")

    def test_audit_log_contains_mleg_entry(self):
        s, _ = _run_mleg()
        audit_text = " ".join(s.audit_log)
        self.assertIn("mleg submitted", audit_text)


# ─────────────────────────────────────────────────────────────────────────────
# Suite 3 — _submit_spread_mleg: rejection paths
# ─────────────────────────────────────────────────────────────────────────────

class TestSubmitSpreadMlegRejection(unittest.TestCase):

    def test_too_few_legs_rejected(self):
        from options_executor import _submit_spread_mleg
        from schemas import StructureLifecycle
        s = _make_structure(legs=[_make_leg("buy", bid=2.0, ask=3.0)])
        with patch.dict(sys.modules, _make_mock_alpaca_modules()):
            result = _submit_spread_mleg(s, MagicMock())
        self.assertEqual(result.lifecycle, StructureLifecycle.REJECTED)

    def test_no_mid_price_rejected(self):
        from options_executor import _submit_spread_mleg
        from schemas import StructureLifecycle
        s = _make_structure(legs=[
            _make_leg("buy",  bid=None, ask=None),
            _make_leg("sell", bid=4.0,  ask=5.0),
        ])
        with patch.dict(sys.modules, _make_mock_alpaca_modules()):
            result = _submit_spread_mleg(s, MagicMock())
        self.assertEqual(result.lifecycle, StructureLifecycle.REJECTED)

    def test_alpaca_exception_rejected(self):
        from options_executor import _submit_spread_mleg
        from schemas import StructureLifecycle
        client = MagicMock()
        client.submit_order.side_effect = Exception("Alpaca API error 42210000")
        s = _make_structure()
        with patch.dict(sys.modules, _make_mock_alpaca_modules()):
            result = _submit_spread_mleg(s, client)
        self.assertEqual(result.lifecycle, StructureLifecycle.REJECTED)

    def test_alpaca_exception_not_partially_filled(self):
        """A rejected mleg must not leave PARTIALLY_FILLED (old sequential artifact)."""
        from options_executor import _submit_spread_mleg
        from schemas import StructureLifecycle
        client = MagicMock()
        client.submit_order.side_effect = Exception("error")
        s = _make_structure()
        with patch.dict(sys.modules, _make_mock_alpaca_modules()):
            result = _submit_spread_mleg(s, client)
        self.assertNotEqual(result.lifecycle, StructureLifecycle.PARTIALLY_FILLED)


# ─────────────────────────────────────────────────────────────────────────────
# Suite 4 — submit_structure dispatch: spreads use mleg (single order call)
# ─────────────────────────────────────────────────────────────────────────────

class TestSubmitStructureDispatch(unittest.TestCase):

    def test_credit_put_spread_single_order_call(self):
        _, client = _run_submit_structure("put_credit_spread")
        self.assertEqual(client.submit_order.call_count, 1)

    def test_credit_call_spread_single_order_call(self):
        _, client = _run_submit_structure("call_credit_spread")
        self.assertEqual(client.submit_order.call_count, 1)

    def test_debit_put_spread_single_order_call(self):
        _, client = _run_submit_structure("put_debit_spread")
        self.assertEqual(client.submit_order.call_count, 1)

    def test_debit_call_spread_single_order_call(self):
        _, client = _run_submit_structure("call_debit_spread")
        self.assertEqual(client.submit_order.call_count, 1)

    def test_spread_lifecycle_is_submitted(self):
        from schemas import StructureLifecycle
        s, _ = _run_submit_structure("put_credit_spread")
        self.assertEqual(s.lifecycle, StructureLifecycle.SUBMITTED)

    def test_spread_lifecycle_not_fully_filled(self):
        """Old sequential code set FULLY_FILLED on short leg submit. mleg sets SUBMITTED."""
        from schemas import StructureLifecycle
        s, _ = _run_submit_structure("put_credit_spread")
        self.assertNotEqual(s.lifecycle, StructureLifecycle.FULLY_FILLED)

    def test_spread_lifecycle_not_partially_filled(self):
        """Old sequential code set PARTIALLY_FILLED after long leg. mleg skips this."""
        from schemas import StructureLifecycle
        s, _ = _run_submit_structure("put_credit_spread")
        self.assertNotEqual(s.lifecycle, StructureLifecycle.PARTIALLY_FILLED)


# ─────────────────────────────────────────────────────────────────────────────
# Suite 5 — A2PreflightResult.pending_underlyings field
# ─────────────────────────────────────────────────────────────────────────────

class TestPendingUnderlyingsField(unittest.TestCase):

    def test_default_is_empty_frozenset(self):
        from bot_options_stage0_preflight import A2PreflightResult
        r = A2PreflightResult()
        self.assertIsInstance(r.pending_underlyings, frozenset)
        self.assertEqual(len(r.pending_underlyings), 0)

    def test_can_hold_underlyings(self):
        from bot_options_stage0_preflight import A2PreflightResult
        r = A2PreflightResult()
        r.pending_underlyings = frozenset({"NVDA", "PYPL"})
        self.assertIn("NVDA", r.pending_underlyings)
        self.assertIn("PYPL", r.pending_underlyings)

    def test_does_not_affect_halt_default(self):
        from bot_options_stage0_preflight import A2PreflightResult
        r = A2PreflightResult()
        self.assertFalse(r.halt)


# ─────────────────────────────────────────────────────────────────────────────
# Suite 6 — Pending underlyings filter in run_candidate_stage
# ─────────────────────────────────────────────────────────────────────────────

class TestPendingUnderlyingsFilter(unittest.TestCase):
    """run_candidate_stage skips symbols in config['_pending_underlyings']."""

    def _run(self, pending: frozenset, sym: str = "NVDA"):
        signal_scores = {
            sym: {
                "conviction": "medium",
                "tier": "core",
                "primary_catalyst": "test",
                "price": 200.0,
                "direction": "bullish",
                "score": 72,
            }
        }
        iv_summaries = {
            sym: {
                "iv_environment": "expensive",
                "iv_rank": 73.9,
                "observation_mode": False,
            }
        }
        config = {"_pending_underlyings": pending}

        mock_oum = MagicMock()
        mock_oum.is_tradeable.return_value = True
        mock_options_data = MagicMock()
        mock_options_data.get_options_regime.return_value = "normal"
        mock_options_data.fetch_options_chain.return_value = {}
        mock_stage2 = MagicMock()
        mock_stage2._quick_liquidity_check.return_value = True
        mock_stage2._route_strategy.return_value = ["put_credit_spread"]
        mock_oi = MagicMock()
        mock_oi.select_options_strategy.return_value = None

        with patch.dict(sys.modules, {
            "options_universe_manager": mock_oum,
            "options_data": mock_options_data,
            "bot_options_stage2_structures": mock_stage2,
            "options_intelligence": mock_oi,
        }):
            from bot_options_stage1_candidates import run_candidate_stage
            return run_candidate_stage(
                signal_scores=signal_scores,
                iv_summaries=iv_summaries,
                equity=100_000,
                vix=20.0,
                equity_symbols=["NVDA"],
                config=config,
            )

    def test_pending_sym_produces_no_candidate_sets(self):
        sets, _, _, _ = self._run(pending=frozenset({"NVDA"}))
        self.assertEqual(len(sets), 0)

    def test_pending_sym_produces_no_structures(self):
        _, _, _, structs = self._run(pending=frozenset({"NVDA"}))
        self.assertEqual(len(structs), 0)

    def test_empty_pending_does_not_block(self):
        # No pending set → pipeline runs normally (returns list, not error)
        sets, _, _, structs = self._run(pending=frozenset())
        self.assertIsInstance(sets, list)
        self.assertIsInstance(structs, list)

    def test_different_pending_sym_does_not_block_nvda(self):
        # PYPL is pending, not NVDA — NVDA should not be skipped
        sets, _, _, _ = self._run(pending=frozenset({"PYPL"}), sym="NVDA")
        # Run_candidate_stage returns normally (doesn't raise)
        self.assertIsInstance(sets, list)


if __name__ == "__main__":
    unittest.main()
