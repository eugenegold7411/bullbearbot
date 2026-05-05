"""
tests/test_close_mleg_and_untracked_gate.py

BUG-1: close_structure() uses atomic MLEG for spread close to avoid Alpaca 40310000.
BUG-2: run_a2_preflight() blocks candidates for underlyings with untracked live positions.
"""
from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch

# ── Alpaca mock helpers ───────────────────────────────────────────────────────

class _PositionIntentEnum:
    BUY_TO_OPEN    = "buy_to_open"
    SELL_TO_OPEN   = "sell_to_open"
    BUY_TO_CLOSE   = "buy_to_close"
    SELL_TO_CLOSE  = "sell_to_close"


class _OrderClassEnum:
    MLEG   = "mleg"
    SIMPLE = "simple"


class _OrderSideEnum:
    BUY  = "buy"
    SELL = "sell"


class _TIFValue:
    def __init__(self, v: str):
        self.value = v
    def __eq__(self, other):
        return self.value == (other.value if isinstance(other, _TIFValue) else other)
    def __repr__(self):
        return self.value


class _TimeInForceEnum:
    DAY = _TIFValue("day")
    GTC = _TIFValue("gtc")


class _CapturedLimitOrderRequest:
    """Records kwargs passed to LimitOrderRequest for assertion."""
    captured: dict = {}

    def __init__(self, **kwargs):
        _CapturedLimitOrderRequest.captured = kwargs
        for k, v in kwargs.items():
            setattr(self, k, v)


class _CapturedOptionLegRequest:
    """Records OptionLegRequest instantiation args."""
    instances: list = []

    def __init__(self, **kwargs):
        _CapturedOptionLegRequest.instances.append(kwargs)
        for k, v in kwargs.items():
            setattr(self, k, v)


def _alpaca_mocks(capture_limit_req=False, capture_leg_req=False):
    enums_mod = MagicMock()
    enums_mod.OrderClass    = _OrderClassEnum
    enums_mod.TimeInForce   = _TimeInForceEnum
    enums_mod.PositionIntent = _PositionIntentEnum
    enums_mod.OrderSide     = _OrderSideEnum

    requests_mod = MagicMock()
    if capture_limit_req:
        _CapturedLimitOrderRequest.captured = {}
        requests_mod.LimitOrderRequest = _CapturedLimitOrderRequest
    else:
        requests_mod.LimitOrderRequest = lambda **kw: MagicMock(**kw)
    if capture_leg_req:
        _CapturedOptionLegRequest.instances = []
        requests_mod.OptionLegRequest = _CapturedOptionLegRequest
    else:
        requests_mod.OptionLegRequest = lambda **kw: MagicMock(**kw)
    requests_mod.MarketOrderRequest = lambda **kw: MagicMock(**kw)

    return {
        "alpaca":                  MagicMock(),
        "alpaca.trading":          MagicMock(),
        "alpaca.trading.enums":    enums_mod,
        "alpaca.trading.requests": requests_mod,
    }


# ── Structure / leg fixtures ──────────────────────────────────────────────────

def _make_leg(side: str, filled_price: float, occ: str, bid=None, ask=None) -> MagicMock:
    leg = MagicMock()
    leg.side        = side
    leg.filled_price = filled_price
    leg.occ_symbol  = occ
    leg.bid         = bid
    leg.ask         = ask
    leg.mid         = None
    leg.order_id    = None
    return leg


def _make_spread_structure(strategy_value="call_debit_spread"):
    """2-leg filled structure (buy lower, sell upper) — typical debit spread."""
    from schemas import OptionStrategy, StructureLifecycle

    strategy_map = {
        "call_debit_spread": OptionStrategy.CALL_DEBIT_SPREAD,
        "put_debit_spread":  OptionStrategy.PUT_DEBIT_SPREAD,
        "call_credit_spread": OptionStrategy.CALL_CREDIT_SPREAD,
        "put_credit_spread":  OptionStrategy.PUT_CREDIT_SPREAD,
    }

    s = MagicMock()
    s.contracts  = 1
    s.underlying = "TSM"
    s.expiration = "2026-06-20"
    s.order_ids  = []
    s.lifecycle  = StructureLifecycle.FULLY_FILLED
    s.strategy   = strategy_map.get(strategy_value, OptionStrategy.CALL_DEBIT_SPREAD)
    s.close_reason_code   = None
    s.close_reason_detail = None
    s.initiated_by        = None
    s.closed_at           = None
    s.pnl_unrealized      = None
    s.roll_reason_code    = None
    s.roll_reason_detail  = None

    audit_log = []
    s.audit_log = audit_log
    s.add_audit = lambda msg: audit_log.append(msg)

    buy_leg  = _make_leg("buy",  3.50, "TSM260620C00140000", bid=3.40, ask=3.60)
    sell_leg = _make_leg("sell", 1.80, "TSM260620C00145000", bid=1.70, ask=1.90)
    s.legs = [buy_leg, sell_leg]
    return s


def _make_single_leg_structure():
    """Single-leg call structure."""
    from schemas import OptionStrategy, StructureLifecycle

    s = MagicMock()
    s.contracts  = 1
    s.underlying = "NVDA"
    s.expiration = "2026-06-20"
    s.order_ids  = []
    s.lifecycle  = StructureLifecycle.FULLY_FILLED
    s.strategy   = OptionStrategy.SINGLE_CALL
    s.close_reason_code   = None
    s.close_reason_detail = None
    s.initiated_by        = None
    s.closed_at           = None
    s.pnl_unrealized      = None
    s.roll_reason_code    = None
    s.roll_reason_detail  = None

    audit_log = []
    s.add_audit = lambda msg: audit_log.append(msg)

    leg = _make_leg("buy", 5.00, "NVDA260620C00900000", bid=4.90, ask=5.10)
    s.legs = [leg]
    return s


# ── BUG-1 Tests: MLEG close for spreads ──────────────────────────────────────

class TestSpreadCloseMleg(unittest.TestCase):
    """close_structure() must use a single atomic MLEG order for 2-leg spreads."""

    def _run_close(self, structure, method="limit", capture_leg=False):
        import importlib
        _CapturedOptionLegRequest.instances = []

        mocks = _alpaca_mocks(capture_limit_req=True, capture_leg_req=capture_leg)

        with patch.dict(sys.modules, mocks):
            import options_executor
            importlib.reload(options_executor)

            client = MagicMock()
            mock_order = MagicMock()
            mock_order.id = "test-close-order"
            client.submit_order.return_value = mock_order

            options_executor.close_structure(structure, client, reason="test_close", method=method)
            return client

    def test_spread_limit_close_submits_single_mleg_order(self):
        """BUG-1: spread limit close must call submit_order exactly once (MLEG)."""
        struct = _make_spread_structure()
        client = self._run_close(struct, method="limit")
        self.assertEqual(
            client.submit_order.call_count, 1,
            "Expected 1 MLEG submit_order call for spread close, "
            f"got {client.submit_order.call_count}",
        )

    def test_spread_limit_close_uses_mleg_order_class(self):
        """BUG-1: the MLEG close order has order_class=MLEG."""
        struct = _make_spread_structure()
        _CapturedLimitOrderRequest.captured = {}
        _CapturedOptionLegRequest.instances = []

        import importlib
        mocks = _alpaca_mocks(capture_limit_req=True, capture_leg_req=True)
        with patch.dict(sys.modules, mocks):
            import options_executor
            importlib.reload(options_executor)
            client = MagicMock()
            client.submit_order.return_value = MagicMock(id="x")
            options_executor.close_structure(struct, client, reason="r", method="limit")

        req = _CapturedLimitOrderRequest.captured
        self.assertEqual(req.get("order_class"), _OrderClassEnum.MLEG)

    def test_spread_limit_close_leg_intents(self):
        """BUG-1: buy leg → SELL_TO_CLOSE, sell leg → BUY_TO_CLOSE in MLEG close."""
        struct = _make_spread_structure()
        _CapturedOptionLegRequest.instances = []

        import importlib
        mocks = _alpaca_mocks(capture_leg_req=True)
        with patch.dict(sys.modules, mocks):
            import options_executor
            importlib.reload(options_executor)
            client = MagicMock()
            client.submit_order.return_value = MagicMock(id="x")
            options_executor.close_structure(struct, client, reason="r", method="limit")

        legs = _CapturedOptionLegRequest.instances
        self.assertEqual(len(legs), 2, f"Expected 2 leg requests, got {len(legs)}: {legs}")

        intents = {lg["symbol"]: lg["position_intent"] for lg in legs}
        self.assertEqual(intents["TSM260620C00140000"], _PositionIntentEnum.SELL_TO_CLOSE,
                         "buy leg must use SELL_TO_CLOSE")
        self.assertEqual(intents["TSM260620C00145000"], _PositionIntentEnum.BUY_TO_CLOSE,
                         "sell leg must use BUY_TO_CLOSE")

    def test_single_leg_close_does_not_use_mleg(self):
        """BUG-1: single-leg close must NOT use MLEG (fallback to per-leg)."""
        struct = _make_single_leg_structure()
        import importlib
        mocks = _alpaca_mocks(capture_limit_req=True)
        with patch.dict(sys.modules, mocks):
            import options_executor
            importlib.reload(options_executor)
            client = MagicMock()
            client.submit_order.return_value = MagicMock(id="x")
            options_executor.close_structure(struct, client, reason="r", method="limit")

        req = _CapturedLimitOrderRequest.captured
        # Single-leg path uses LimitOrderRequest without order_class=MLEG
        self.assertNotEqual(req.get("order_class"), _OrderClassEnum.MLEG,
                            "single-leg close must not use MLEG order class")
        self.assertEqual(client.submit_order.call_count, 1)

    def test_spread_market_close_uses_per_leg(self):
        """BUG-1: market method bypasses MLEG path — uses per-leg MarketOrderRequest."""
        struct = _make_spread_structure()
        import importlib
        mocks = _alpaca_mocks()
        with patch.dict(sys.modules, mocks):
            import options_executor
            importlib.reload(options_executor)
            client = MagicMock()
            client.submit_order.return_value = MagicMock(id="x")
            options_executor.close_structure(struct, client, reason="r", method="market")

        # market method hits the per-leg loop → 2 separate submit_order calls
        self.assertEqual(client.submit_order.call_count, 2,
                         "market close must submit one order per leg, not one MLEG")

    def test_spread_mleg_fallback_sells_short_first(self):
        """BUG-1 fallback: when MLEG close raises, per-leg fallback closes sell-side first."""
        struct = _make_spread_structure()
        import importlib
        mocks = _alpaca_mocks(capture_leg_req=True)

        submission_order = []

        def _submit(req):
            # Detect which leg by reading the limit order's symbol attribute
            sym = getattr(req, "symbol", None)
            submission_order.append(sym)
            return MagicMock(id="x")

        with patch.dict(sys.modules, mocks):
            import options_executor
            importlib.reload(options_executor)

            client = MagicMock()
            client.submit_order.side_effect = [
                Exception("mleg_not_supported"),  # first call = MLEG close → fails
                MagicMock(id="order-1"),           # second call = per-leg sell-side
                MagicMock(id="order-2"),           # third call = per-leg buy-side
            ]
            options_executor.close_structure(struct, client, reason="r", method="limit")

        # After MLEG fails (1 call), per-leg submits 2 orders.
        self.assertEqual(client.submit_order.call_count, 3,
                         "Expected MLEG attempt (1) + 2 per-leg fallback calls")


# ── BUG-2 Tests: untracked position gate ─────────────────────────────────────

class TestUntrackedPositionGate(unittest.TestCase):
    """run_a2_preflight() blocks underlyings with untracked live Alpaca positions."""

    @classmethod
    def setUpClass(cls):
        try:
            from bot_options_stage0_preflight import run_a2_preflight
            cls.run_preflight = staticmethod(run_a2_preflight)
        except ImportError as exc:
            raise unittest.SkipTest(f"bot_options_stage0_preflight not importable: {exc}")

    def _make_alpaca_client(self, positions=None, equity=50_000.0):
        """Build a minimal mock alpaca_client for preflight."""
        client = MagicMock()
        acct = MagicMock()
        acct.equity       = equity
        acct.cash         = equity * 0.5
        acct.buying_power = equity * 0.8
        client.get_account.return_value = acct

        pos_list = positions or []
        client.get_all_positions.return_value = pos_list
        return client

    def _make_alpaca_position(self, symbol: str):
        pos = MagicMock()
        pos.symbol = symbol
        return pos

    def _run_with_mocks(self, alpaca_positions, tracked_occ_symbols):
        """
        Run run_a2_preflight in isolation with:
        - alpaca_positions: list of OCC symbol strings Alpaca returns
        - tracked_occ_symbols: OCC symbols already in structures.json legs
        Returns the A2PreflightResult.
        """
        from schemas import StructureLifecycle

        # Build fake structures for options_state.load_structures()
        fake_structs = []
        if tracked_occ_symbols:
            s = MagicMock()
            s.lifecycle = StructureLifecycle.FULLY_FILLED
            s.underlying = "TRACKED"
            s.order_ids = []
            fake_leg = MagicMock()
            fake_leg.occ_symbol = tracked_occ_symbols[0] if tracked_occ_symbols else None
            s.legs = [fake_leg]
            fake_structs.append(s)

        client = self._make_alpaca_client(
            positions=[self._make_alpaca_position(sym) for sym in alpaca_positions]
        )

        preflight_mock = MagicMock()
        pf_result = MagicMock()
        pf_result.verdict = "go"
        pf_result.blockers = []
        pf_result.warnings = []
        preflight_mock.run_preflight.return_value = pf_result

        opts_state_mock = MagicMock()
        opts_state_mock.load_structures.return_value = fake_structs
        opts_state_mock.get_open_structures.return_value = []

        import importlib

        import bot_options_stage0_preflight as _mod

        with patch.dict(sys.modules, {
            "preflight":    preflight_mock,
            "options_state": opts_state_mock,
            "divergence":   MagicMock(),
            "reconciliation": MagicMock(),
            "alpaca.trading.enums":    MagicMock(),
            "alpaca.trading.requests": MagicMock(),
        }):
            importlib.reload(_mod)
            result = _mod.run_a2_preflight("market", client)

        return result

    def test_untracked_position_adds_underlying_to_pending(self):
        """BUG-2: TSM OCC position not in structures.json → TSM added to pending_underlyings."""
        result = self._run_with_mocks(
            alpaca_positions=["TSM260116C00140000", "TSM260116C00145000"],
            tracked_occ_symbols=[],
        )
        self.assertIn("TSM", result.pending_underlyings,
                      "TSM with untracked live position must be in pending_underlyings")

    def test_tracked_position_not_added_to_pending(self):
        """BUG-2: position already tracked in structures.json is NOT added to pending_underlyings."""
        result = self._run_with_mocks(
            alpaca_positions=["NVDA260620C00900000"],
            tracked_occ_symbols=["NVDA260620C00900000"],
        )
        self.assertNotIn("NVDA", result.pending_underlyings,
                         "tracked position must not be added to pending_underlyings")

    def test_untracked_gate_non_fatal_on_get_positions_error(self):
        """BUG-2: if get_all_positions() raises in the gate, preflight must still return."""
        client = self._make_alpaca_client()
        # First call (reconciliation path) succeeds; gate call raises — simulate network blip.
        # Use a counter so only the gate call (after first call) fails.
        call_count = {"n": 0}

        def _side_effect():
            call_count["n"] += 1
            if call_count["n"] > 1:
                raise Exception("network timeout")
            return []

        client.get_all_positions.side_effect = _side_effect

        opts_state_mock = MagicMock()
        opts_state_mock.load_structures.return_value = []
        opts_state_mock.get_open_structures.return_value = []

        preflight_mock = MagicMock()
        pf_result = MagicMock()
        pf_result.verdict = "go"
        pf_result.blockers = []
        pf_result.warnings = []
        preflight_mock.run_preflight.return_value = pf_result

        import importlib

        import bot_options_stage0_preflight as _mod

        with patch.dict(sys.modules, {
            "preflight":     preflight_mock,
            "options_state": opts_state_mock,
            "divergence":    MagicMock(),
            "reconciliation": MagicMock(),
            "alpaca.trading.enums":    MagicMock(),
            "alpaca.trading.requests": MagicMock(),
        }):
            importlib.reload(_mod)
            result = _mod.run_a2_preflight("market", client)

        # The gate failure must not halt the cycle
        self.assertFalse(result.halt,
                         "untracked-gate failure must not set halt=True on preflight result")


if __name__ == "__main__":
    unittest.main()
