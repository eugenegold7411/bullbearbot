"""
test_reallocate_handler.py — order_executor REALLOCATE action handler tests.

Tests validate:
  1. reallocate → execute_reallocate called (close + open submitted)
  2. reallocate → no "unknown action" rejection
  3. reallocate blocked when session_tier == "unknown" (T-006)
  4. reallocate success → log_trade called with correct fields
  5. close fails → entry NOT submitted (exit_failed propagated)
"""

import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# ── shared dependency stubs ────────────────────────────────────────────────────

def _ensure_dotenv_stub():
    if "dotenv" not in sys.modules:
        m = types.ModuleType("dotenv")
        m.load_dotenv = lambda *a, **kw: None
        sys.modules["dotenv"] = m


def _ensure_log_setup_stub():
    if "log_setup" not in sys.modules:
        import logging
        ls = types.ModuleType("log_setup")
        ls.get_logger = lambda _: logging.getLogger("test")
        ls.log_trade = lambda _: None
        sys.modules["log_setup"] = ls
    if "cost_tracker" not in sys.modules:
        ct = types.ModuleType("cost_tracker")
        ct.get_tracker = lambda: None
        sys.modules["cost_tracker"] = ct


def _ensure_alpaca_stubs():
    for mod in (
        "alpaca", "alpaca.trading", "alpaca.trading.client",
        "alpaca.trading.requests", "alpaca.trading.enums",
        "alpaca.data", "alpaca.data.historical", "alpaca.data.requests",
        "alpaca.data.enums",
    ):
        if mod not in sys.modules:
            sys.modules[mod] = types.ModuleType(mod)

    enums = sys.modules["alpaca.trading.enums"]
    _ENUM_ATTRS = {
        "BUY": "buy", "SELL": "sell", "DAY": "day", "GTC": "gtc",
        "BRACKET": "bracket", "ACTIVE": "active", "CALL": "call", "PUT": "put",
    }
    for _enum_name in ("OrderSide", "TimeInForce", "OrderClass", "AssetStatus",
                       "ContractType", "ExerciseStyle"):
        if not hasattr(enums, _enum_name):
            _c = type(_enum_name, (), {})
            setattr(enums, _enum_name, _c)
        _enum_cls = getattr(enums, _enum_name)
        for _attr, _val in _ENUM_ATTRS.items():
            if not hasattr(_enum_cls, _attr):
                setattr(_enum_cls, _attr, _val)

    reqs = sys.modules["alpaca.trading.requests"]
    for cls_name in (
        "MarketOrderRequest", "LimitOrderRequest", "StopOrderRequest",
        "StopLossRequest", "TakeProfitRequest", "ClosePositionRequest",
        "GetOptionContractsRequest", "GetOrdersRequest",
    ):
        if not hasattr(reqs, cls_name):
            def _mk(name):
                class _Req:
                    def __init__(self, **kwargs):
                        self._kwargs = kwargs
                _Req.__name__ = name
                return _Req
            setattr(reqs, cls_name, _mk(cls_name))

    client = sys.modules["alpaca.trading.client"]
    if not hasattr(client, "TradingClient"):
        class _TC:
            def __init__(self, **_kw): pass
        client.TradingClient = _TC


def _ensure_portfolio_intelligence_stub():
    """Stub portfolio_intelligence so the lazy import inside the handler resolves."""
    if "portfolio_intelligence" not in sys.modules:
        pi = types.ModuleType("portfolio_intelligence")
        pi.execute_reallocate = MagicMock(return_value={
            "status": "submitted",
            "exit_order_id": "exit-111",
            "entry_order_id": "entry-222",
            "reason": "exited GS, entered ARM",
        })
        sys.modules["portfolio_intelligence"] = pi


def _ensure_all_stubs():
    _ensure_dotenv_stub()
    _ensure_log_setup_stub()
    _ensure_alpaca_stubs()
    _ensure_portfolio_intelligence_stub()
    for mod in ("anthropic", "bot_clients", "schemas"):
        if mod not in sys.modules:
            sys.modules[mod] = types.ModuleType(mod)
    # schemas needs BrokerAction with to_dict
    schemas = sys.modules["schemas"]
    if not hasattr(schemas, "BrokerAction"):
        class _BA:
            def to_dict(self): return {}
        schemas.BrokerAction = _BA


# ── test fixtures ──────────────────────────────────────────────────────────────

class _FakeAccount:
    equity = "30000"
    buying_power = "60000"
    cash = "30000"


_REALLOCATE_GS_ARM = {
    "action":       "reallocate",
    "symbol":       "ARM",
    "exit_symbol":  "GS",
    "entry_symbol": "ARM",
    "qty":          168,
    "stop_loss":    214.23,
    "take_profit":  235.83,
    "tier":         "core",
    "catalyst":     "Barclays PT raise ARM $250",
    "confidence":   "high",
}

_REALLOCATE_RESULT_OK = {
    "status":        "submitted",
    "exit_order_id": "exit-111",
    "entry_order_id": "entry-222",
    "reason":        "exited GS, entered ARM",
}

_REALLOCATE_RESULT_EXIT_FAILED = {
    "status":        "exit_failed",
    "exit_order_id": None,
    "entry_order_id": None,
    "reason":        "position not found: GS",
}


def _run_execute_all(actions, session_tier="market", reallocate_return=None,
                     extra_patches=None):
    """Run execute_all with minimal mocked environment."""
    _ensure_all_stubs()

    pi = sys.modules["portfolio_intelligence"]
    pi.execute_reallocate = MagicMock(
        return_value=reallocate_return or _REALLOCATE_RESULT_OK
    )

    mock_log_trade = MagicMock()
    patches = {
        "_get_alpaca":        MagicMock(return_value=MagicMock()),
        "log_trade":          mock_log_trade,
        "_get_current_price": MagicMock(return_value=215.0),
    }
    if extra_patches:
        patches.update(extra_patches)

    with patch.multiple("order_executor", **patches):
        import order_executor as oe
        results = oe.execute_all(
            actions,
            account=_FakeAccount(),
            positions=[],
            market_status="open",
            minutes_since_open=30,
            current_prices={"GS": 180.0, "ARM": 215.0},
            session_tier=session_tier,
        )
    return results, mock_log_trade


# ── Suite: REALLOCATE handler ──────────────────────────────────────────────────

class TestReallocateHandler(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _ensure_all_stubs()
        try:
            import order_executor  # noqa: F401
        except ImportError as exc:
            raise unittest.SkipTest(f"order_executor not importable: {exc}")

    # 1 — happy path: execute_reallocate is called; result status is "submitted"
    def test_reallocate_executes_close_then_open(self):
        results, _ = _run_execute_all(
            [dict(_REALLOCATE_GS_ARM)],
            reallocate_return=_REALLOCATE_RESULT_OK,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "submitted",
                         f"Expected submitted, got: {results[0]}")
        # Confirm execute_reallocate was actually called
        pi = sys.modules["portfolio_intelligence"]
        pi.execute_reallocate.assert_called_once()
        call_args = pi.execute_reallocate.call_args
        # First positional arg is the exit symbol
        self.assertEqual(call_args[0][0], "GS")

    # 2 — no silent drop: "unknown action" must not appear in any result reason
    def test_reallocate_not_silent_drop(self):
        results, _ = _run_execute_all(
            [dict(_REALLOCATE_GS_ARM)],
            reallocate_return=_REALLOCATE_RESULT_OK,
        )
        for r in results:
            self.assertNotIn(
                "unknown action", (r.reason or "").lower(),
                f"reallocate was silently dropped: {r}",
            )

    # 3 — T-006: reallocate must be blocked when session_tier == "unknown"
    def test_reallocate_respects_session_tier(self):
        results, _ = _run_execute_all(
            [dict(_REALLOCATE_GS_ARM)],
            session_tier="unknown",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "rejected",
                         f"Expected rejected for unknown session, got: {results[0]}")
        self.assertIn("session", results[0].reason.lower(),
                      f"Reason should mention session: {results[0].reason}")

    # 4 — log_trade called on success with reallocate action field
    def test_reallocate_logs_correctly(self):
        _, mock_log_trade = _run_execute_all(
            [dict(_REALLOCATE_GS_ARM)],
            reallocate_return=_REALLOCATE_RESULT_OK,
        )
        mock_log_trade.assert_called()
        logged = mock_log_trade.call_args[0][0]
        self.assertEqual(logged.get("action"), "reallocate")
        self.assertEqual(logged.get("symbol"), "GS")      # exit_symbol logged
        self.assertEqual(logged.get("entry_symbol"), "ARM")

    # 5 — close fails → open never submitted; result carries exit_failed status
    def test_reallocate_aborts_if_close_fails(self):
        results, _ = _run_execute_all(
            [dict(_REALLOCATE_GS_ARM)],
            reallocate_return=_REALLOCATE_RESULT_EXIT_FAILED,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "exit_failed",
                         f"Expected exit_failed propagated, got: {results[0]}")
        # execute_reallocate was called once — it handles the abort internally
        pi = sys.modules["portfolio_intelligence"]
        pi.execute_reallocate.assert_called_once()


# ── Suite: validate_action for reallocate ─────────────────────────────────────

class TestValidateActionReallocate(unittest.TestCase):
    """Unit tests for the validate_action() reallocate branch."""

    @classmethod
    def setUpClass(cls):
        _ensure_all_stubs()
        try:
            from order_executor import validate_action
            cls.va = staticmethod(validate_action)
        except ImportError as exc:
            raise unittest.SkipTest(f"order_executor not importable: {exc}")

    def _acc(self):
        return _FakeAccount()

    def _valid(self):
        return {
            "action": "reallocate", "symbol": "ARM",
            "exit_symbol": "GS", "entry_symbol": "ARM",
            "qty": 168, "stop_loss": 214.23, "take_profit": 235.83,
        }

    def test_valid_reallocate_passes(self):
        # Must not raise
        self.va(self._valid(), self._acc(), [], "open", 30)

    def test_missing_exit_symbol_raises(self):
        a = self._valid()
        del a["exit_symbol"]
        with self.assertRaises(ValueError, msg="Should raise without exit_symbol"):
            self.va(a, self._acc(), [], "open", 30)

    def test_missing_entry_symbol_raises(self):
        a = self._valid()
        del a["entry_symbol"]
        del a["symbol"]
        with self.assertRaises(ValueError, msg="Should raise without entry_symbol"):
            self.va(a, self._acc(), [], "open", 30)

    def test_zero_qty_raises(self):
        a = {**self._valid(), "qty": 0}
        with self.assertRaises(ValueError):
            self.va(a, self._acc(), [], "open", 30)

    def test_missing_stop_loss_raises(self):
        a = self._valid()
        del a["stop_loss"]
        with self.assertRaises(ValueError):
            self.va(a, self._acc(), [], "open", 30)

    def test_missing_take_profit_raises(self):
        a = self._valid()
        del a["take_profit"]
        with self.assertRaises(ValueError):
            self.va(a, self._acc(), [], "open", 30)


if __name__ == "__main__":
    unittest.main()
