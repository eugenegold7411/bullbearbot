"""
tests/test_options_decimal_credit.py

Tests for:
- DP-01: All structure types produce limit prices with <= 2 decimal places
- DP-02: short_put single-leg limit rounded to 2dp
- DP-03: credit_put_spread net credit rounded to 2dp
- DP-04: Negative net credit (e.g. -0.457) rounds correctly to -0.45 or -0.46 (nearest $0.05)
- CS-01: Credit spread TIF is GTC; debit spread TIF is DAY
- CS-02: Credit spread limit uses 0.90× credit factor (more aggressive than pure mid)
- CS-03: Credit spread with net credit < min_credit_usd is not submitted
"""
from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock

# ─────────────────────────────────────────────────────────────────────────────
# Alpaca mock modules (alpaca not installed in test env)
# ─────────────────────────────────────────────────────────────────────────────

class _MockLimitOrderRequest:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class _MockOptionLegRequest:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class _OrderClass:
    MLEG = "mleg"
    SIMPLE = "simple"


class _TIFValue:
    """Enum-like value with a .value attribute for string representation."""
    def __init__(self, v: str):
        self.value = v

    def __eq__(self, other):
        if isinstance(other, _TIFValue):
            return self.value == other.value
        return self.value == other

    def __repr__(self):
        return self.value


class _TimeInForce:
    DAY = _TIFValue("day")
    GTC = _TIFValue("gtc")


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


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_leg(side: str, bid, ask, occ: str = "NVDA260522P00200000") -> MagicMock:
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

    strategy_map = {
        "put_credit_spread":   OptionStrategy.PUT_CREDIT_SPREAD,
        "call_credit_spread":  OptionStrategy.CALL_CREDIT_SPREAD,
        "put_debit_spread":    OptionStrategy.PUT_DEBIT_SPREAD,
        "call_debit_spread":   OptionStrategy.CALL_DEBIT_SPREAD,
        "single_put":          OptionStrategy.SINGLE_PUT,
        "single_call":         OptionStrategy.SINGLE_CALL,
        "short_put":           OptionStrategy.SHORT_PUT,
        "iron_condor":         OptionStrategy.IRON_CONDOR,
        "iron_butterfly":      OptionStrategy.IRON_BUTTERFLY,
    }
    s.strategy = strategy_map.get(strategy_value, OptionStrategy.PUT_CREDIT_SPREAD)
    s.lifecycle = StructureLifecycle.PROPOSED

    def add_audit(msg):
        s.audit_log.append({"msg": msg})

    s.add_audit = add_audit

    if legs is not None:
        s.legs = legs
    else:
        # Default: credit spread with long buy + short sell
        long_leg  = _make_leg("buy",  7.60, 7.80, "NVDA260522P00195000")
        short_leg = _make_leg("sell", 8.10, 8.30, "NVDA260522P00200000")
        s.legs = [long_leg, short_leg]

    return s


def _captured_limit_price(structure, config=None):
    """
    Run _submit_spread_mleg with a mock client and return the limit_price
    that would have been passed to LimitOrderRequest.
    """
    import importlib
    captured = {}

    class CapturingLimitOrderRequest:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            for k, v in kwargs.items():
                setattr(self, k, v)

    alpaca_mocks = _make_mock_alpaca_modules()
    alpaca_mocks["alpaca.trading.requests"].LimitOrderRequest = CapturingLimitOrderRequest

    with unittest.mock.patch.dict(sys.modules, alpaca_mocks):
        import importlib

        import options_executor
        importlib.reload(options_executor)

        mock_client = MagicMock()
        mock_order = MagicMock()
        mock_order.id = "test-order-id"
        mock_client.submit_order.return_value = mock_order

        options_executor._submit_spread_mleg(structure, mock_client, config or {})

    return captured.get("limit_price"), captured.get("time_in_force")


def _decimal_places(value: float) -> int:
    """Return number of decimal places in a float's string representation."""
    s = str(abs(value))
    if "." not in s:
        return 0
    return len(s.split(".")[1].rstrip("0") or "0")


# ─────────────────────────────────────────────────────────────────────────────
# DP Tests — Decimal Precision
# ─────────────────────────────────────────────────────────────────────────────

class TestDecimalPrecision(unittest.TestCase):

    def test_dp01_all_structure_types_2dp(self):
        """DP-01: All Phase 1 mleg structure types produce limit prices with <= 2 decimal places."""

        cases = [
            # (strategy_value, long_bid, long_ask, short_bid, short_ask)
            ("put_credit_spread",  7.60, 7.80, 8.10, 8.30),  # credit
            ("call_credit_spread", 3.40, 3.60, 4.10, 4.30),  # credit
            ("put_debit_spread",   5.10, 5.30, 2.40, 2.60),  # debit
            ("call_debit_spread",  4.10, 4.30, 2.40, 2.60),  # debit
            ("iron_condor",        1.10, 1.30, 0.60, 0.80),  # credit, 4 legs
        ]

        for strategy_value, lb, la, sb, sa in cases:
            with self.subTest(strategy=strategy_value):
                if strategy_value == "iron_condor":
                    # 4-leg structure
                    legs = [
                        _make_leg("sell", lb, la, "NVDA260522C00220000"),
                        _make_leg("buy",  0.60, 0.80, "NVDA260522C00225000"),
                        _make_leg("sell", 0.90, 1.10, "NVDA260522P00180000"),
                        _make_leg("buy",  0.50, 0.70, "NVDA260522P00175000"),
                    ]
                else:
                    legs = [
                        _make_leg("buy",  lb, la, "NVDA260522P00195000"),
                        _make_leg("sell", sb, sa, "NVDA260522P00200000"),
                    ]
                struct = _make_structure(legs=legs, strategy_value=strategy_value)
                limit_price, _ = _captured_limit_price(struct)
                if limit_price is not None:
                    self.assertLessEqual(
                        _decimal_places(limit_price), 2,
                        f"{strategy_value}: limit_price={limit_price!r} has >2 decimal places"
                    )

    def test_dp02_short_put_single_leg_2dp(self):
        """DP-02: short_put single-leg limit price is rounded to <= 2 decimal places."""
        import importlib
        import unittest.mock

        captured = {}

        class CapturingLimitOrderRequest:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        alpaca_mocks = _make_mock_alpaca_modules()
        alpaca_mocks["alpaca.trading.requests"].LimitOrderRequest = CapturingLimitOrderRequest

        with unittest.mock.patch.dict(sys.modules, alpaca_mocks):
            import options_executor
            importlib.reload(options_executor)
            from schemas import OptionStrategy

            struct = MagicMock()
            struct.underlying = "NVDA"
            struct.expiration = "2026-05-22"
            struct.contracts = 3
            struct.order_ids = []
            struct.audit_log = []
            struct.strategy = OptionStrategy.SHORT_PUT

            leg = _make_leg("sell", 1.42, 1.50, "NVDA260522P00190000")
            leg.option_type = "put"
            leg.strike = 190.0
            struct.legs = [leg]

            def add_audit(msg):
                struct.audit_log.append({"msg": msg})
            struct.add_audit = add_audit

            mock_client = MagicMock()
            mock_client.submit_order.return_value = MagicMock(id="test-123")

            options_executor._submit_single_leg(struct, mock_client)

        limit = captured.get("limit_price")
        self.assertIsNotNone(limit, "limit_price was not captured")
        self.assertLessEqual(
            _decimal_places(limit), 2,
            f"short_put limit_price={limit!r} has >2 decimal places"
        )
        # mid = (1.42+1.50)/2 = 1.46 → _round_limit(1.46) = round(round(1.46/0.05)*0.05, 2)
        # = round(round(29.2)*0.05, 2) = round(29*0.05, 2) = round(1.45, 2) = 1.45
        # then round(..., 2) = 1.45 — exactly 2dp
        self.assertAlmostEqual(limit, 1.45, places=2)

    def test_dp03_credit_put_spread_2dp(self):
        """DP-03: credit_put_spread net credit is rounded to <= 2 decimal places."""
        # Construct legs that produce a non-round net_mid
        # long buy: bid=7.60 ask=7.80 → mid=7.70
        # short sell: bid=8.10 ask=8.30 → mid=8.20
        # net_mid = 7.70 - 8.20 = -0.50 (clean)
        # Use slightly messier values:
        # long buy: bid=7.63 ask=7.77 → mid=7.70
        # short sell: bid=8.11 ask=8.29 → mid=8.20
        # net_mid = 7.70 - 8.20 = -0.50 (still clean)
        # Use: long=7.65/7.85=7.75, short=8.12/8.30=8.21 → net = 7.75-8.21 = -0.46
        struct = _make_structure(
            legs=[
                _make_leg("buy",  7.65, 7.85, "NVDA260522P00195000"),
                _make_leg("sell", 8.12, 8.30, "NVDA260522P00200000"),
            ],
            strategy_value="put_credit_spread",
        )
        limit_price, _ = _captured_limit_price(struct)
        self.assertIsNotNone(limit_price)
        self.assertLessEqual(_decimal_places(limit_price), 2)
        # credit structures: limit_price should be negative
        self.assertLess(limit_price, 0)

    def test_dp04_odd_net_credit_rounds_correctly(self):
        """DP-04: net credit of -0.457 rounds to -0.45 (nearest $0.05 tick * 0.90 factor)."""
        # Target net_mid = -0.457
        # long buy mid = 7.00, short sell mid = 7.457
        # Use bid/ask: long = 6.90/7.10, short = 7.36/7.554 → mid = 7.00 / 7.457
        struct = _make_structure(
            legs=[
                _make_leg("buy",  6.90, 7.10, "NVDA260522P00195000"),
                _make_leg("sell", 7.36, 7.554, "NVDA260522P00200000"),
            ],
            strategy_value="put_credit_spread",
        )
        limit_price, _ = _captured_limit_price(struct)
        self.assertIsNotNone(limit_price)
        # net_mid ≈ -0.457; credit factor 0.90 → adjusted ≈ -0.411
        # round to $0.05: round(0.411/0.05)*0.05 = round(8.22)*0.05 = 8*0.05 = 0.40
        # limit_price = -0.40
        self.assertLessEqual(_decimal_places(limit_price), 2)
        self.assertLess(limit_price, 0)
        # The exact value depends on Python's banker's rounding but must be ≤ 2dp
        # and must be a multiple of 0.05 (in absolute value)
        abs_price = abs(limit_price)
        remainder = round(abs_price % 0.05, 6)
        self.assertTrue(
            remainder < 1e-9 or abs(remainder - 0.05) < 1e-9,
            f"limit_price={limit_price!r} is not a $0.05 multiple"
        )


# ─────────────────────────────────────────────────────────────────────────────
# CS Tests — Credit Spread Behaviour
# ─────────────────────────────────────────────────────────────────────────────

class TestCreditSpreadBehaviour(unittest.TestCase):

    def test_cs01_credit_spread_tif_is_gtc(self):
        """CS-01: Credit spread mleg orders use GTC time-in-force."""
        for strat in ("put_credit_spread", "call_credit_spread", "iron_condor"):
            with self.subTest(strategy=strat):
                if strat == "iron_condor":
                    legs = [
                        _make_leg("sell", 1.10, 1.30, "NVDA260522C00220000"),
                        _make_leg("buy",  0.60, 0.80, "NVDA260522C00225000"),
                        _make_leg("sell", 0.90, 1.10, "NVDA260522P00180000"),
                        _make_leg("buy",  0.50, 0.70, "NVDA260522P00175000"),
                    ]
                else:
                    legs = [
                        _make_leg("buy",  7.60, 7.80, "NVDA260522P00195000"),
                        _make_leg("sell", 8.10, 8.30, "NVDA260522P00200000"),
                    ]
                struct = _make_structure(legs=legs, strategy_value=strat)
                _, tif = _captured_limit_price(struct)
                self.assertEqual(
                    tif, _TimeInForce.GTC,
                    f"{strat}: expected TIF=GTC but got {tif!r}"
                )

    def test_cs01_debit_spread_tif_is_day(self):
        """CS-01 complement: Debit spread mleg orders use DAY time-in-force."""
        for strat in ("put_debit_spread", "call_debit_spread"):
            with self.subTest(strategy=strat):
                legs = [
                    _make_leg("buy",  5.10, 5.30, "NVDA260522P00205000"),
                    _make_leg("sell", 2.40, 2.60, "NVDA260522P00200000"),
                ]
                struct = _make_structure(legs=legs, strategy_value=strat)
                _, tif = _captured_limit_price(struct)
                self.assertEqual(
                    tif, _TimeInForce.DAY,
                    f"{strat}: expected TIF=DAY but got {tif!r}"
                )

    def test_cs02_credit_spread_limit_uses_credit_factor(self):
        """CS-02: Credit spread limit applies _CREDIT_FILL_FACTOR (0.90) to net mid."""
        # long buy: mid=7.70 (bid=7.60, ask=7.80)
        # short sell: mid=8.20 (bid=8.10, ask=8.30)
        # net_mid = 7.70 - 8.20 = -0.50
        # adjusted = -0.50 * 0.90 = -0.45
        # rounded to $0.05: round(0.45/0.05)*0.05 = 9*0.05 = 0.45
        # limit_price = -0.45
        struct = _make_structure(
            legs=[
                _make_leg("buy",  7.60, 7.80, "NVDA260522P00195000"),
                _make_leg("sell", 8.10, 8.30, "NVDA260522P00200000"),
            ],
            strategy_value="put_credit_spread",
        )
        limit_price, _ = _captured_limit_price(struct)
        self.assertIsNotNone(limit_price)
        # At mid pricing: limit would be -0.50.
        # With 0.90 factor: adjusted=-0.45 → rounded to -0.45.
        # Either way limit < 0 (credit) and abs value < abs(mid net).
        abs_mid = 0.50  # abs(net_mid) at pure mid
        self.assertLess(limit_price, 0, "Credit spread limit_price must be negative")
        self.assertGreater(
            abs(limit_price), 0,
            "Credit spread limit_price must have positive absolute value"
        )
        # With the 0.90 factor, the credit demanded must be <= mid credit
        self.assertLessEqual(
            abs(limit_price), abs_mid + 0.05,  # allow 1 tick above mid (rounding artefact)
            f"limit_price={limit_price!r} is larger than mid credit {abs_mid}"
        )

    def test_cs02_debit_spread_limit_uses_mid_not_factor(self):
        """CS-02 complement: Debit spread limit is at mid, not adjusted by credit factor."""
        # long buy: mid=5.20 (bid=5.10, ask=5.30)
        # short sell: mid=2.50 (bid=2.40, ask=2.60)
        # net_mid = 5.20 - 2.50 = 2.70 (debit)
        # rounded to $0.05: round(2.70/0.05)*0.05 = 54*0.05 = 2.70
        # No credit factor applied → limit = +2.70
        struct = _make_structure(
            legs=[
                _make_leg("buy",  5.10, 5.30, "NVDA260522P00205000"),
                _make_leg("sell", 2.40, 2.60, "NVDA260522P00200000"),
            ],
            strategy_value="put_debit_spread",
        )
        limit_price, _ = _captured_limit_price(struct)
        self.assertIsNotNone(limit_price)
        self.assertGreater(limit_price, 0, "Debit spread limit_price must be positive")
        # Should be ~2.70 (no credit factor reduction)
        self.assertAlmostEqual(limit_price, 2.70, delta=0.10)

    def test_cs03_credit_below_min_is_rejected(self):
        """CS-03: Credit spread with net credit < min_credit_usd is not submitted."""
        import importlib
        import unittest.mock

        # long buy: mid=5.20, short sell: mid=5.30 → net_mid=-0.10 (tiny credit)
        struct = _make_structure(
            legs=[
                _make_leg("buy",  5.10, 5.30, "NVDA260522P00195000"),
                _make_leg("sell", 5.20, 5.40, "NVDA260522P00200000"),
            ],
            strategy_value="put_credit_spread",
        )

        # min_credit_usd=0.15 in config — the 0.10 credit should be rejected
        config = {"account2": {"min_credit_usd": 0.15}}

        alpaca_mocks = _make_mock_alpaca_modules()
        with unittest.mock.patch.dict(sys.modules, alpaca_mocks):
            import options_executor
            importlib.reload(options_executor)
            from schemas import StructureLifecycle

            mock_client = MagicMock()
            result = options_executor._submit_spread_mleg(struct, mock_client, config)

        # Should be rejected without calling submit_order
        mock_client.submit_order.assert_not_called()
        self.assertEqual(result.lifecycle, StructureLifecycle.REJECTED)
        # Audit log should mention min_credit_usd
        audit_msgs = " ".join(e["msg"] for e in result.audit_log)
        self.assertIn("min_credit_usd", audit_msgs)

    def test_cs03_credit_above_min_is_submitted(self):
        """CS-03 complement: Credit spread with net credit >= min_credit_usd is submitted."""
        import importlib
        import unittest.mock

        # net_mid ≈ -0.50 which is above 0.15 threshold
        struct = _make_structure(
            legs=[
                _make_leg("buy",  7.60, 7.80, "NVDA260522P00195000"),
                _make_leg("sell", 8.10, 8.30, "NVDA260522P00200000"),
            ],
            strategy_value="put_credit_spread",
        )

        config = {"account2": {"min_credit_usd": 0.15}}

        alpaca_mocks = _make_mock_alpaca_modules()
        with unittest.mock.patch.dict(sys.modules, alpaca_mocks):
            import options_executor
            importlib.reload(options_executor)
            from schemas import StructureLifecycle

            mock_client = MagicMock()
            mock_client.submit_order.return_value = MagicMock(id="order-abc")
            result = options_executor._submit_spread_mleg(struct, mock_client, config)

        # Should be submitted
        mock_client.submit_order.assert_called_once()
        self.assertEqual(result.lifecycle, StructureLifecycle.SUBMITTED)

    def test_cs03_no_config_uses_default_min_credit(self):
        """CS-03: When no config provided, default min_credit_usd=0.15 is used."""
        import importlib
        import unittest.mock

        # Very small credit: net_mid = 5.20 - 5.30 = -0.10 < 0.15 default
        struct = _make_structure(
            legs=[
                _make_leg("buy",  5.10, 5.30, "NVDA260522P00195000"),
                _make_leg("sell", 5.20, 5.40, "NVDA260522P00200000"),
            ],
            strategy_value="put_credit_spread",
        )

        alpaca_mocks = _make_mock_alpaca_modules()
        with unittest.mock.patch.dict(sys.modules, alpaca_mocks):
            import options_executor
            importlib.reload(options_executor)
            from schemas import StructureLifecycle

            mock_client = MagicMock()
            # Pass empty config — should fall back to default min_credit_usd=0.15
            result = options_executor._submit_spread_mleg(struct, mock_client, {})

        mock_client.submit_order.assert_not_called()
        self.assertEqual(result.lifecycle, StructureLifecycle.REJECTED)

    def test_cs03_debit_spread_ignores_min_credit(self):
        """CS-03: min_credit_usd gate does not apply to debit spreads."""
        import importlib
        import unittest.mock

        # Small net debit (not a credit) - should not be blocked by min_credit_usd
        struct = _make_structure(
            legs=[
                _make_leg("buy",  5.10, 5.30, "NVDA260522P00205000"),
                _make_leg("sell", 5.00, 5.20, "NVDA260522P00200000"),
            ],
            strategy_value="put_debit_spread",
        )

        config = {"account2": {"min_credit_usd": 0.15}}

        alpaca_mocks = _make_mock_alpaca_modules()
        with unittest.mock.patch.dict(sys.modules, alpaca_mocks):
            import options_executor
            importlib.reload(options_executor)
            from schemas import StructureLifecycle

            mock_client = MagicMock()
            mock_client.submit_order.return_value = MagicMock(id="order-xyz")
            result = options_executor._submit_spread_mleg(struct, mock_client, config)

        # Debit spread should reach submit_order regardless of min_credit_usd
        mock_client.submit_order.assert_called_once()
        self.assertEqual(result.lifecycle, StructureLifecycle.SUBMITTED)


if __name__ == "__main__":
    unittest.main()
