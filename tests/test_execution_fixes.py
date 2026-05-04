"""
test_execution_fixes.py — T-005, T-006, T-010, T-011 correctness tests.

Suite T005: regime label normalization (_normalize_regime_labels)
Suite T006: session=unknown BUY guard in execute_all()
Suite T010: per-symbol consecutive-rejection suppressor
Suite T011: crypto symbol format (BTC/USD → BTCUSD) in _submit_buy()
"""

import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# ── dependency stubs ──────────────────────────────────────────────────────────

def _ensure_dotenv_stub():
    if "dotenv" not in sys.modules:
        m = types.ModuleType("dotenv")
        m.load_dotenv = lambda *a, **kw: None
        sys.modules["dotenv"] = m


def _ensure_anthropic_stub():
    """Stub anthropic + bot_clients so bot_stage1_regime can be imported locally."""
    if "anthropic" not in sys.modules:
        sys.modules["anthropic"] = types.ModuleType("anthropic")
    if "bot_clients" not in sys.modules:
        bc = types.ModuleType("bot_clients")
        bc._get_claude = lambda: None
        bc.MODEL_FAST = "claude-haiku-4-5-20251001"
        sys.modules["bot_clients"] = bc
    if "log_setup" not in sys.modules:
        ls = types.ModuleType("log_setup")
        import logging
        ls.get_logger = lambda _: logging.getLogger("test")
        ls.log_trade = lambda _: None
        sys.modules["log_setup"] = ls
    if "cost_tracker" not in sys.modules:
        ct = types.ModuleType("cost_tracker")
        ct.get_tracker = lambda: None
        sys.modules["cost_tracker"] = ct


def _ensure_alpaca_stubs():
    """Minimal stubs so order_executor can be imported without real credentials."""
    for mod in (
        "alpaca", "alpaca.trading", "alpaca.trading.client",
        "alpaca.trading.requests", "alpaca.trading.enums",
        "alpaca.data", "alpaca.data.historical", "alpaca.data.requests",
        "alpaca.data.enums",
    ):
        if mod not in sys.modules:
            sys.modules[mod] = types.ModuleType(mod)

    # Enums — ensure required attribute values exist (conftest stubs may lack them)
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

    # Request classes — capture constructor kwargs so tests can inspect them
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

    # TradingClient stub
    client = sys.modules["alpaca.trading.client"]
    if not hasattr(client, "TradingClient"):
        class _TC:
            def __init__(self, **_kw): pass
        client.TradingClient = _TC


# ── Suite T005 — regime label normalization ───────────────────────────────────

class TestRegimeLabelNormalization(unittest.TestCase):
    """Suite T005 — _normalize_regime_labels() converts hyphens to underscores."""

    @classmethod
    def setUpClass(cls):
        _ensure_anthropic_stub()
        try:
            from bot_stage1_regime import _normalize_regime_labels
            cls.fn = staticmethod(_normalize_regime_labels)
        except ImportError as exc:
            raise unittest.SkipTest(f"bot_stage1_regime not importable: {exc}")

    def test_risk_on_hyphen_normalized(self):
        out = self.fn({"bias": "risk-on"})
        self.assertEqual(out["bias"], "risk_on")

    def test_risk_off_hyphen_normalized(self):
        out = self.fn({"bias": "risk-off"})
        self.assertEqual(out["bias"], "risk_off")

    def test_neutral_unchanged(self):
        out = self.fn({"bias": "neutral"})
        self.assertEqual(out["bias"], "neutral")

    def test_macro_regime_risk_off_normalized(self):
        out = self.fn({"macro_regime": "risk-off"})
        self.assertEqual(out["macro_regime"], "risk_off")

    def test_already_underscore_unchanged(self):
        out = self.fn({"bias": "risk_on", "macro_regime": "goldilocks"})
        self.assertEqual(out["bias"], "risk_on")
        self.assertEqual(out["macro_regime"], "goldilocks")

    def test_missing_fields_no_error(self):
        out = self.fn({})
        self.assertEqual(out, {})

    def test_mutates_and_returns_same_dict(self):
        d = {"bias": "risk-on"}
        out = self.fn(d)
        self.assertIs(out, d)
        self.assertEqual(d["bias"], "risk_on")

    def test_normal_bias_normalized_to_neutral(self):
        out = self.fn({"bias": "normal"})
        self.assertEqual(out["bias"], "neutral")

    def test_normal_upper_case_normalized(self):
        out = self.fn({"bias": "NORMAL"})
        self.assertEqual(out["bias"], "neutral")


# ── helpers shared by T006 / T010 ─────────────────────────────────────────────

class _FakeAccount:
    equity = "30000"
    buying_power = "60000"
    cash = "30000"


_BUY_GLD = {
    "action": "buy", "symbol": "GLD", "tier": "core",
    "qty": 5, "stop_loss": 425.0, "take_profit": 455.0,
    "catalyst": "safe-haven", "confidence": "medium",
}
_SELL_GLD = {
    "action": "sell", "symbol": "GLD", "tier": "core",
    "qty": 5,
}
_CLOSE_GLD = {
    "action": "close", "symbol": "GLD", "tier": "core",
}


def _run_execute_all(actions, session_tier, extra_patches=None):
    """Import order_executor and call execute_all with minimal environment."""
    _ensure_dotenv_stub()
    _ensure_alpaca_stubs()
    patches = {
        "order_executor._get_alpaca": MagicMock(return_value=MagicMock()),
        "order_executor.log_trade": MagicMock(),
        "order_executor._get_current_price": MagicMock(return_value=435.0),
    }
    if extra_patches:
        patches.update(extra_patches)
    with patch.multiple("order_executor", **{k.split(".")[-1]: v for k, v in patches.items()}):
        import order_executor as oe
        return oe.execute_all(
            actions,
            account=_FakeAccount(),
            positions=[],
            market_status="open",
            minutes_since_open=30,
            current_prices={"GLD": 435.0, "BTC/USD": 85000.0},
            session_tier=session_tier,
        )


# ── Suite T006 — session=unknown BUY guard ────────────────────────────────────

class TestSessionUnknownGuard(unittest.TestCase):
    """Suite T006 — BUY orders must be blocked when session_tier is 'unknown'."""

    @classmethod
    def setUpClass(cls):
        _ensure_dotenv_stub()
        _ensure_alpaca_stubs()
        try:
            import order_executor  # noqa: F401
        except ImportError as exc:
            raise unittest.SkipTest(f"order_executor not importable: {exc}")

    def test_buy_unknown_session_is_rejected(self):
        results = _run_execute_all([dict(_BUY_GLD)], session_tier="unknown")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "rejected")
        self.assertIn("session", results[0].reason.lower())

    def test_buy_market_session_not_blocked_by_guard(self):
        """Market session should reach validate_action, not be blocked by T-006."""
        results = _run_execute_all([dict(_BUY_GLD)], session_tier="market")
        # May be rejected by validate_action for other reasons, but not T-006
        if results:
            self.assertNotIn("session=unknown", results[0].reason)

    def test_sell_unknown_session_not_blocked(self):
        """SELL (exit) must never be blocked by the session guard."""
        results = _run_execute_all([dict(_SELL_GLD)], session_tier="unknown")
        # Should reach broker (which may error), but not be T-006 rejected
        if results:
            self.assertNotEqual(results[0].reason, "session=unknown: BUY order blocked (session not yet classified)")

    def test_close_unknown_session_not_blocked(self):
        """CLOSE (exit) must never be blocked by the session guard."""
        results = _run_execute_all([dict(_CLOSE_GLD)], session_tier="unknown")
        if results:
            self.assertNotEqual(results[0].reason, "session=unknown: BUY order blocked (session not yet classified)")

    def test_multiple_buys_all_blocked_when_unknown(self):
        actions = [dict(_BUY_GLD), dict(_BUY_GLD)]
        results = _run_execute_all(actions, session_tier="unknown")
        self.assertEqual(len(results), 2)
        for r in results:
            self.assertEqual(r.status, "rejected")
            self.assertIn("session", r.reason.lower())


# ── Suite T010 — consecutive rejection suppressor ────────────────────────────

class TestConsecutiveRejectionSuppressor(unittest.TestCase):
    """Suite T010 — after 10 consecutive BUY rejections, further entries suppressed."""

    @classmethod
    def setUpClass(cls):
        _ensure_dotenv_stub()
        _ensure_alpaca_stubs()
        try:
            import order_executor as oe
            cls.oe = oe
        except ImportError as exc:
            raise unittest.SkipTest(f"order_executor not importable: {exc}")

    def setUp(self):
        # Each test starts with a clean counter
        self.oe._consecutive_rejections.clear()

    def _validated_reject(self, symbol="GLD"):
        """Force a validate_action rejection (market closed path)."""
        _ensure_dotenv_stub()
        _ensure_alpaca_stubs()
        with patch.multiple(
            self.oe,
            _get_alpaca=MagicMock(return_value=MagicMock()),
            log_trade=MagicMock(),
            _get_current_price=MagicMock(return_value=435.0),
            validate_action=MagicMock(side_effect=ValueError("test-reject")),
        ):
            return self.oe.execute_all(
                [{"action": "buy", "symbol": symbol, "tier": "core",
                  "qty": 5, "stop_loss": 425.0, "take_profit": 455.0,
                  "catalyst": "", "confidence": "medium"}],
                account=_FakeAccount(),
                positions=[],
                market_status="closed",
                minutes_since_open=0,
                current_prices={symbol: 435.0},
                session_tier="market",
            )

    def test_counter_increments_on_rejection(self):
        self._validated_reject("GLD")
        self.assertEqual(self.oe._consecutive_rejections.get("GLD", 0), 1)

    def test_suppression_fires_at_10(self):
        self.oe._consecutive_rejections["GLD"] = 10
        results = _run_execute_all([dict(_BUY_GLD)], session_tier="market")
        self.assertEqual(results[0].status, "rejected")
        self.assertIn("suppressed", results[0].reason)

    def test_nine_rejections_not_yet_suppressed(self):
        self.oe._consecutive_rejections["GLD"] = 9
        # Should still reach validate_action (not pre-suppressed)
        with patch.multiple(
            self.oe,
            _get_alpaca=MagicMock(return_value=MagicMock()),
            log_trade=MagicMock(),
            _get_current_price=MagicMock(return_value=435.0),
            validate_action=MagicMock(side_effect=ValueError("market-closed")),
        ):
            results = self.oe.execute_all(
                [dict(_BUY_GLD)],
                account=_FakeAccount(),
                positions=[],
                market_status="closed",
                minutes_since_open=0,
                current_prices={"GLD": 435.0},
                session_tier="market",
            )
        self.assertNotIn("suppressed", results[0].reason)

    def test_sell_never_suppressed(self):
        """SELL must not be affected by the rejection counter regardless of count."""
        self.oe._consecutive_rejections["GLD"] = 100
        results = _run_execute_all([dict(_SELL_GLD)], session_tier="market")
        if results:
            self.assertNotIn("suppressed", results[0].reason)

    def test_close_never_suppressed(self):
        """CLOSE must not be affected by the rejection counter."""
        self.oe._consecutive_rejections["GLD"] = 100
        results = _run_execute_all([dict(_CLOSE_GLD)], session_tier="market")
        if results:
            self.assertNotIn("suppressed", results[0].reason)

    def test_different_symbols_independent(self):
        """Rejection counter is per-symbol."""
        self.oe._consecutive_rejections["GLD"] = 10
        # TSM counter is 0 — should not be suppressed by GLD's count
        action = dict(_BUY_GLD)
        action["symbol"] = "TSM"
        with patch.multiple(
            self.oe,
            _get_alpaca=MagicMock(return_value=MagicMock()),
            log_trade=MagicMock(),
            _get_current_price=MagicMock(return_value=175.0),
            validate_action=MagicMock(side_effect=ValueError("market-closed")),
        ):
            results = self.oe.execute_all(
                [action],
                account=_FakeAccount(),
                positions=[],
                market_status="closed",
                minutes_since_open=0,
                current_prices={"TSM": 175.0},
                session_tier="market",
            )
        self.assertNotIn("suppressed", results[0].reason)


# ── Suite T011 — crypto symbol format ────────────────────────────────────────

class TestCryptoSymbolFormat(unittest.TestCase):
    """Suite T011 — _submit_buy() must pass 'BTCUSD' not 'BTC/USD' to Alpaca."""

    @classmethod
    def setUpClass(cls):
        try:
            from schemas import alpaca_symbol
            cls.alpaca_symbol = staticmethod(alpaca_symbol)
        except ImportError as exc:
            raise unittest.SkipTest(f"schemas not importable: {exc}")

    def test_btc_slash_to_btcusd(self):
        self.assertEqual(self.alpaca_symbol("BTC/USD"), "BTCUSD")

    def test_eth_slash_to_ethusd(self):
        self.assertEqual(self.alpaca_symbol("ETH/USD"), "ETHUSD")

    def test_stock_symbol_unchanged(self):
        self.assertEqual(self.alpaca_symbol("GLD"), "GLD")

    def test_submit_buy_uses_alpaca_symbol_for_crypto(self):
        """_submit_buy crypto path must pass 'BTCUSD' (not 'BTC/USD') to MarketOrderRequest."""
        _ensure_dotenv_stub()
        _ensure_alpaca_stubs()
        import order_executor as oe

        mock_req_cls = MagicMock()
        mock_req_instance = MagicMock()
        mock_req_cls.return_value = mock_req_instance

        fake_order = MagicMock()
        fake_order.id = "test-order-id"
        fake_order.filled_avg_price = None
        fake_order.filled_qty = None
        fake_order.filled_at = None

        with patch.object(oe, "MarketOrderRequest", mock_req_cls), \
             patch.object(oe, "_get_alpaca") as mock_client:
            mock_client.return_value.submit_order.return_value = fake_order
            try:
                oe._submit_buy({
                    "symbol": "BTC/USD",
                    "stop_loss": 80000.0,
                    "take_profit": 100000.0,
                    "qty": 0.1,
                    "order_type": "market",
                    "limit_price": None,
                    "entry_price": 85000.0,
                })
            except Exception:
                pass

        self.assertTrue(mock_req_cls.called,
                        "_submit_buy must construct MarketOrderRequest for crypto")
        _, kwargs = mock_req_cls.call_args
        self.assertEqual(kwargs.get("symbol"), "BTCUSD",
                         f"Expected 'BTCUSD', got '{kwargs.get('symbol')}'")

    def test_submit_buy_stock_symbol_passed_as_is(self):
        """Stock symbols must not be transformed by alpaca_symbol in _submit_buy."""
        _ensure_dotenv_stub()
        _ensure_alpaca_stubs()
        import order_executor as oe

        mock_req_cls = MagicMock()
        mock_req_cls.return_value = MagicMock()

        fake_order = MagicMock()
        fake_order.id = "test-order-id"
        fake_order.filled_avg_price = None
        fake_order.filled_qty = None
        fake_order.filled_at = None

        with patch.object(oe, "MarketOrderRequest", mock_req_cls), \
             patch.object(oe, "StopLossRequest", MagicMock(return_value=MagicMock())), \
             patch.object(oe, "TakeProfitRequest", MagicMock(return_value=MagicMock())), \
             patch.object(oe, "_get_alpaca") as mock_client:
            mock_client.return_value.submit_order.return_value = fake_order
            try:
                oe._submit_buy({
                    "symbol": "GLD",
                    "stop_loss": 420.0,
                    "take_profit": 460.0,
                    "qty": 5,
                    "order_type": "market",
                    "limit_price": None,
                })
            except Exception:
                pass

        if mock_req_cls.called:
            _, kwargs = mock_req_cls.call_args
            self.assertEqual(kwargs.get("symbol"), "GLD")


if __name__ == "__main__":
    unittest.main()
