"""
tests/test_sprint2_followup.py — Sprint 2 follow-up: test artifact cleanup,
stale TBA removal, and OCC format regression suite.

Suites:
  F1  — DTBP tests write no artifacts to production options_log.jsonl
  F2  — remove_backstop wired into order_executor sell/close path
  F3  — OCC symbol format regression: puts, calls, all structure types
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))


# =============================================================================
# F1 — DTBP tests must not write to production options_log.jsonl
# =============================================================================

class TestDTBPTestsDoNotContaminateProductionLog(unittest.TestCase):
    """The 3 DTBP tests in test_sprint2_items.py must use tmp_path for logs."""

    def _get_dtbp_test_source(self) -> str:
        src = Path(__file__).parent / "test_sprint2_items.py"
        return src.read_text()

    def test_dtbp_zero_test_monkeypatches_log_path(self):
        """test_dtbp_zero_guard_skips_submission must monkeypatch _LOG_PATH."""
        src = self._get_dtbp_test_source()
        # Verify tmp_path is wired into the test
        self.assertIn(
            "monkeypatch.setattr(oe, \"_LOG_PATH\"",
            src,
            "DTBP tests must monkeypatch _LOG_PATH to prevent production log writes",
        )

    def test_dtbp_tests_use_tmp_path(self):
        """All 3 DTBP tests must accept tmp_path fixture."""
        src = self._get_dtbp_test_source()
        # All three function signatures should include tmp_path
        self.assertIn("def test_dtbp_zero_guard_skips_submission(tmp_path", src)
        self.assertIn("def test_dtbp_nonzero_proceeds_normally(tmp_path", src)
        self.assertIn("def test_dtbp_check_failure_fails_open(tmp_path", src)

    def test_dtbp_zero_writes_to_tmp_not_production(self, tmp_path=None):
        """When DTBP=0 falls through to submission, log goes to tmp not production path."""
        import sys
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp_log = Path(td) / "options_log.jsonl"
            from datetime import datetime, timezone

            import order_executor_options as oe
            from schemas import (
                OptionsLeg,
                OptionsStructure,
                OptionStrategy,
                StructureLifecycle,
                Tier,
            )

            leg = OptionsLeg(
                occ_symbol="GLD261219C00435000",
                underlying="GLD", side="buy", qty=1,
                option_type="call", strike=435.0, expiration="2026-12-19",
            )
            structure = OptionsStructure(
                structure_id="followup-test-001",
                underlying="GLD",
                strategy=OptionStrategy.SINGLE_CALL,
                lifecycle=StructureLifecycle.PROPOSED,
                legs=[leg], contracts=1, max_cost_usd=500.0,
                opened_at=datetime.now(timezone.utc).isoformat(),
                catalyst="test", tier=Tier.CORE,
            )

            mock_account = MagicMock()
            mock_account.daytrading_buying_power = "0"
            mock_account.options_buying_power = "100000"
            mock_account.id = "test-acct-followup"
            mock_client = MagicMock()
            mock_client.get_account.return_value = mock_account

            # Mock submit_structure so we don't make real Alpaca calls
            mock_filled = OptionsStructure(
                structure_id="followup-test-001",
                underlying="GLD",
                strategy=OptionStrategy.SINGLE_CALL,
                lifecycle=StructureLifecycle.SUBMITTED,
                legs=[leg], contracts=1, max_cost_usd=500.0,
                opened_at=datetime.now(timezone.utc).isoformat(),
                catalyst="test", tier=Tier.CORE,
            )
            mock_filled.order_ids = ["order-followup-001"]

            mock_executor = MagicMock()
            mock_executor.submit_structure.return_value = mock_filled

            original_log_path = oe._LOG_PATH
            try:
                oe._LOG_PATH = tmp_log
                with mock.patch.object(oe, "_get_options_client", return_value=mock_client), \
                     mock.patch.dict(sys.modules, {"options_executor": mock_executor,
                                                   "options_state": MagicMock()}):
                    result = oe.submit_options_order(structure, equity=50000.0)
            finally:
                oe._LOG_PATH = original_log_path

            # New behavior: DTBP=0 + OBP>0 falls through — status is submitted not dtbp_zero
            self.assertNotEqual(result.status, "dtbp_zero",
                                "dtbp_zero status must not be returned after fallback fix")
            # Log entry must go to tmp, not production
            self.assertTrue(tmp_log.exists(), "Execution log entry should be in tmp log")
            entries = [json.loads(line) for line in tmp_log.read_text().splitlines() if line.strip()]
            self.assertGreaterEqual(len(entries), 1, "At least one log entry expected in tmp log")
            self.assertIn("followup-test-001", entries[0]["structure_id"])


# =============================================================================
# F2 — remove_backstop wired into order_executor sell/close path
# =============================================================================

class TestRemoveBackstopWiredInExecutor(unittest.TestCase):
    """order_executor.execute_all must call remove_backstop on sell/close."""

    def _run_execute_sell(self, act: str, monkeypatch_fn=None):
        """Helper: run execute_all with a sell or close action, intercept remove_backstop."""
        from unittest.mock import MagicMock, patch

        import order_executor as oe

        action = {
            "symbol": "XLE",
            "action": act,
            "qty": 100,
            "tier": "core",
        }

        mock_account = MagicMock()
        mock_account.equity = "105000"
        mock_account.cash = "20000"
        mock_account.buying_power = "105000"
        mock_account.pattern_day_trader = False
        mock_account.options_approved_level = 2
        mock_account.daytrade_count = 0
        mock_account.daytrading_buying_power = "50000"

        remove_backstop_calls = []

        def fake_rb(symbol, cfg_path):
            remove_backstop_calls.append(symbol)

        with patch.object(oe, "_get_alpaca") as mock_alpaca, \
             patch("reconciliation.remove_backstop", side_effect=fake_rb), \
             patch("order_executor.validate_action", return_value=None), \
             patch("order_executor.log_trade"):

            if act == "close":
                mock_alpaca.return_value.close_position.return_value = MagicMock(id="oid-close-1")
            else:
                mock_order = MagicMock()
                mock_order.id = "oid-sell-1"
                mock_order.filled_avg_price = "57.50"
                mock_order.filled_qty = "100"
                mock_order.filled_at = "2026-04-27T18:00:00Z"
                mock_alpaca.return_value.submit_order.return_value = mock_order

            results = oe.execute_all(
                actions=[action],
                account=mock_account,
                positions=[],
                market_status="open",
                minutes_since_open=60,
            )

        return results, remove_backstop_calls

    def test_remove_backstop_called_on_sell(self):
        """execute_all calls remove_backstop when act=sell succeeds."""
        results, calls = self._run_execute_sell("sell")
        self.assertIn("XLE", calls, "remove_backstop should be called for sell action")

    def test_remove_backstop_called_on_close(self):
        """execute_all calls remove_backstop when act=close succeeds."""
        results, calls = self._run_execute_sell("close")
        self.assertIn("XLE", calls, "remove_backstop should be called for close action")

    def test_remove_backstop_not_called_on_buy(self):
        """execute_all does NOT call remove_backstop for buy actions."""
        import order_executor as oe
        action = {"symbol": "NVDA", "action": "buy", "qty": 5, "tier": "core"}

        mock_account = MagicMock()
        mock_account.equity = "105000"
        mock_account.cash = "50000"
        mock_account.buying_power = "105000"
        mock_account.pattern_day_trader = False
        mock_account.options_approved_level = 2
        mock_account.daytrade_count = 0
        mock_account.daytrading_buying_power = "50000"

        remove_backstop_calls = []

        def fake_rb(symbol, cfg_path):
            remove_backstop_calls.append(symbol)

        with patch.object(oe, "_get_alpaca") as mock_alpaca, \
             patch("reconciliation.remove_backstop", side_effect=fake_rb), \
             patch("order_executor.validate_action", return_value=None):

            mock_order = MagicMock()
            mock_order.id = "oid-buy-1"
            mock_order.filled_avg_price = None
            mock_order.filled_qty = None
            mock_order.filled_at = None
            mock_alpaca.return_value.submit_order.return_value = mock_order

            oe.execute_all(
                actions=[action], account=mock_account, positions=[],
                market_status="open", minutes_since_open=60,
            )

        self.assertNotIn("NVDA", remove_backstop_calls,
                         "remove_backstop should NOT be called for buy")

    def test_remove_backstop_failure_is_non_fatal(self):
        """remove_backstop error on sell does not break execute_all result."""
        import order_executor as oe
        action = {"symbol": "XLE", "action": "sell", "qty": 100, "tier": "core"}

        mock_account = MagicMock()
        mock_account.equity = "105000"
        mock_account.cash = "20000"
        mock_account.buying_power = "105000"
        mock_account.pattern_day_trader = False
        mock_account.options_approved_level = 2
        mock_account.daytrade_count = 0
        mock_account.daytrading_buying_power = "50000"

        def exploding_rb(symbol, cfg_path):
            raise RuntimeError("strategy_config.json not found")

        with patch.object(oe, "_get_alpaca") as mock_alpaca, \
             patch("reconciliation.remove_backstop", side_effect=exploding_rb), \
             patch("order_executor.validate_action", return_value=None), \
             patch("order_executor.log_trade"):

            mock_order = MagicMock()
            mock_order.id = "oid-sell-nonfatal"
            mock_order.filled_avg_price = "57.50"
            mock_order.filled_qty = "100"
            mock_order.filled_at = "2026-04-27T18:00:00Z"
            mock_alpaca.return_value.submit_order.return_value = mock_order

            results = oe.execute_all(
                actions=[action], account=mock_account, positions=[],
                market_status="open", minutes_since_open=60,
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "submitted",
                         "remove_backstop failure must not affect execution result")


# =============================================================================
# F3 — OCC symbol format regression: both builders, all structure types
# =============================================================================

class TestOCCSymbolFormatRegression(unittest.TestCase):
    """
    Regression suite for OCC symbol format after the 450975d no-padding fix.

    Context: options_builder._build_occ_symbol previously used .ljust(6)[:6]
    to pad tickers to 6 chars (producing "NVDA  260522P00205000"). The fix
    removed padding. These tests prevent regression. The "NVDA  260522P00205000"
    seen in Alpaca error logs was Alpaca's own error-message display format, not
    generated by our code — verified 2026-04-27 by running both builders live.
    """

    _PATTERN = __import__("re").compile(r"^[A-Z]{1,5}\d{6}[CP]\d{8}$")

    def _check(self, sym: str, expected: str):
        self.assertEqual(sym, expected, f"OCC mismatch: got {sym!r}, want {expected!r}")
        self.assertNotIn(" ", sym, f"OCC symbol must not contain spaces: {sym!r}")
        self.assertTrue(
            self._PATTERN.match(sym),
            f"OCC symbol {sym!r} does not match Alpaca pattern"
        )

    # ── options_executor.build_occ_symbol ─────────────────────────────────────

    def test_executor_put_no_spaces(self):
        from options_executor import build_occ_symbol
        self._check(build_occ_symbol("NVDA", "2026-05-22", "put", 205.0),
                    "NVDA260522P00205000")

    def test_executor_call_no_spaces(self):
        from options_executor import build_occ_symbol
        self._check(build_occ_symbol("NVDA", "2026-05-22", "call", 205.0),
                    "NVDA260522C00205000")

    def test_executor_5char_ticker_put(self):
        """TSM (4 chars like NVDA) and SPY (3 chars) must not be padded."""
        from options_executor import build_occ_symbol
        self._check(build_occ_symbol("TSM", "2026-05-08", "put", 160.0),
                    "TSM260508P00160000")

    def test_executor_single_char_ticker_put(self):
        from options_executor import build_occ_symbol
        self._check(build_occ_symbol("V", "2026-04-28", "put", 300.0),
                    "V260428P00300000")

    def test_executor_3char_ticker_put(self):
        from options_executor import build_occ_symbol
        self._check(build_occ_symbol("SPY", "2026-05-08", "put", 500.0),
                    "SPY260508P00500000")

    # ── options_builder._build_occ_symbol ─────────────────────────────────────

    def test_builder_put_no_spaces(self):
        from options_builder import _build_occ_symbol
        self._check(_build_occ_symbol("NVDA", "2026-05-22", "put", 205.0),
                    "NVDA260522P00205000")

    def test_builder_call_no_spaces(self):
        from options_builder import _build_occ_symbol
        self._check(_build_occ_symbol("NVDA", "2026-05-22", "call", 205.0),
                    "NVDA260522C00205000")

    def test_builder_tsm_put_no_spaces(self):
        from options_builder import _build_occ_symbol
        self._check(_build_occ_symbol("TSM", "2026-05-08", "put", 160.0),
                    "TSM260508P00160000")

    def test_builder_put_option_type_alias(self):
        """option_type='put' must produce P, not default to something else."""
        from options_builder import _build_occ_symbol
        sym = _build_occ_symbol("GLD", "2026-12-19", "put", 435.0)
        self.assertIn("P", sym, "put option_type must produce P suffix")
        self.assertNotIn("C", sym)

    # ── build_legs (spread legs contain OCC symbols) ──────────────────────────

    def test_put_spread_legs_have_no_spaces(self):
        """build_legs for a put spread must produce OCC symbols with no spaces."""
        from options_builder import build_legs
        from schemas import OptionStrategy
        strikes_data = {
            "option_type":    "put",
            "long_strike_price":  205.0,
            "short_strike_price": 210.0,
            "long_leg_data": {
                "bid": 2.0, "ask": 2.2,
                "openInterest": 500, "volume": 200, "delta": -0.35,
            },
            "short_leg_data": {
                "bid": 3.0, "ask": 3.2,
                "openInterest": 400, "volume": 150, "delta": -0.45,
            },
        }
        legs = build_legs("NVDA", OptionStrategy.PUT_CREDIT_SPREAD, "2026-05-22", strikes_data)
        self.assertEqual(len(legs), 2)
        for leg in legs:
            self.assertNotIn(" ", leg.occ_symbol,
                             f"Spread leg OCC must not have spaces: {leg.occ_symbol!r}")
            self.assertTrue(self._PATTERN.match(leg.occ_symbol),
                            f"Leg OCC {leg.occ_symbol!r} does not match Alpaca pattern")

    def test_call_spread_legs_have_no_spaces(self):
        """build_legs for a call spread must produce OCC symbols with no spaces."""
        from options_builder import build_legs
        from schemas import OptionStrategy
        strikes_data = {
            "option_type":    "call",
            "long_strike_price":  205.0,
            "short_strike_price": 210.0,
            "long_leg_data": {
                "bid": 2.0, "ask": 2.2,
                "openInterest": 500, "volume": 200, "delta": 0.40,
            },
            "short_leg_data": {
                "bid": 3.0, "ask": 3.2,
                "openInterest": 400, "volume": 150, "delta": 0.35,
            },
        }
        legs = build_legs("NVDA", OptionStrategy.CALL_DEBIT_SPREAD, "2026-05-22", strikes_data)
        self.assertEqual(len(legs), 2)
        for leg in legs:
            self.assertNotIn(" ", leg.occ_symbol,
                             f"Spread leg OCC must not have spaces: {leg.occ_symbol!r}")

    def test_both_builders_produce_identical_symbols(self):
        """options_executor.build_occ_symbol and options_builder._build_occ_symbol agree."""
        from options_builder import _build_occ_symbol as builder_build
        from options_executor import build_occ_symbol as exec_build

        cases = [
            ("NVDA", "2026-05-22", "put",  205.0),
            ("NVDA", "2026-05-22", "call", 205.0),
            ("TSM",  "2026-05-08", "put",  160.0),
            ("GLD",  "2026-12-19", "call", 435.0),
            ("V",    "2026-04-28", "put",  300.0),
            ("AMZN", "2026-06-20", "put",  195.0),
        ]
        for ticker, expiry, opt_type, strike in cases:
            exec_sym    = exec_build(ticker, expiry, opt_type, strike)
            builder_sym = builder_build(ticker, expiry, opt_type, strike)
            self.assertEqual(exec_sym, builder_sym,
                             f"Builders diverge for {ticker} {opt_type}: "
                             f"executor={exec_sym!r} builder={builder_sym!r}")


if __name__ == "__main__":
    unittest.main()
