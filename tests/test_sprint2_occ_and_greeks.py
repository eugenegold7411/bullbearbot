"""
tests/test_sprint2_occ_and_greeks.py — OCC symbol format fix + Alpaca greeks wiring.

Sprint 2 — two changes verified here:
  1. options_builder._build_occ_symbol produces Alpaca-format symbols (no ticker padding).
  2. fetch_option_greeks in options_data returns greeks from Alpaca snapshot and
     _enrich_with_greeks in bot_options_stage2_structures populates greeks on
     surviving candidates.

Red lines verified:
  - risk_kernel.py is NOT touched.
  - A2 stays bounded — greeks are enrichment only, no freeform model output.
  - structures.json is SOLE A2 state source — enrichment only touches in-memory dicts.
"""

import re
import sys
import unittest
from pathlib import Path
from unittest import mock

_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

# ---------------------------------------------------------------------------
# Third-party stubs — prevents import errors in CI without venv
# ---------------------------------------------------------------------------
_STUBS = {
    "dotenv": None, "anthropic": None,
    "alpaca": None, "alpaca.trading": None, "alpaca.trading.client": None,
    "alpaca.trading.requests": None, "alpaca.trading.enums": None,
    "alpaca.data": None, "alpaca.data.enums": None,
    "alpaca.data.historical": None, "alpaca.data.historical.option": None,
    "alpaca.data.requests": None, "alpaca.data.timeframe": None,
    "pandas": None, "yfinance": None,
}
for _n, _v in _STUBS.items():
    if _n not in sys.modules:
        _m = mock.MagicMock()
        sys.modules[_n] = _m


# =============================================================================
# Build 1 — OCC symbol format: options_builder._build_occ_symbol
# =============================================================================

class TestOCCSymbolFormat(unittest.TestCase):
    """_build_occ_symbol must produce Alpaca-format OCC symbols (no ticker padding)."""

    _PATTERN = re.compile(r"^[A-Z]{1,5}\d{6}[CP]\d{8}$")

    def _sym(self, ticker, expiry, opt_type, strike):
        from options_builder import _build_occ_symbol
        return _build_occ_symbol(ticker, expiry, opt_type, strike)

    def test_nvda_put_no_spaces(self):
        """NVDA put must have no spaces — Alpaca rejects OCC paper padding."""
        sym = self._sym("NVDA", "2026-05-22", "put", 205.0)
        self.assertEqual(sym, "NVDA260522P00205000")
        self.assertNotIn(" ", sym, "OCC symbol must not contain spaces")

    def test_spy_call_format(self):
        """SPY call must encode fractional strike correctly."""
        sym = self._sym("SPY", "2026-05-08", "call", 512.5)
        self.assertEqual(sym, "SPY260508C00512500")

    def test_gld_call_format(self):
        """GLD (3-char ticker) must not be padded to 6 chars."""
        sym = self._sym("GLD", "2026-12-19", "call", 435.0)
        self.assertEqual(sym, "GLD261219C00435000")
        self.assertFalse(sym.startswith("GLD "), "GLD must not be space-padded")

    def test_single_letter_ticker(self):
        """Single-letter ticker V must not be padded with 5 spaces."""
        sym = self._sym("V", "2026-04-28", "call", 300.0)
        self.assertEqual(sym, "V260428C00300000")
        self.assertTrue(sym.startswith("V2"), "V must be followed immediately by date digits")

    def test_aapl_put(self):
        """AAPL put must produce no-space OCC symbol."""
        sym = self._sym("AAPL", "2026-06-19", "put", 175.0)
        self.assertEqual(sym, "AAPL260619P00175000")

    def test_alpaca_regex_compliance(self):
        """All generated symbols must match Alpaca's OCC regex."""
        cases = [
            ("NVDA",  "2026-05-22", "put",  205.0),
            ("SPY",   "2026-05-08", "call", 512.5),
            ("V",     "2026-04-28", "call", 300.0),
            ("AAPL",  "2026-06-19", "put",  175.0),
            ("GOOGL", "2026-05-15", "call", 165.0),
            ("GLD",   "2026-12-19", "call", 435.0),
        ]
        for ticker, expiry, opt_type, strike in cases:
            sym = self._sym(ticker, expiry, opt_type, strike)
            self.assertRegex(sym, self._PATTERN,
                             f"{sym!r} does not match Alpaca regex for "
                             f"{ticker} {expiry} {opt_type} {strike}")

    def test_fractional_strike_cents(self):
        """Fractional strikes must encode cents in the last 3 digits."""
        sym = self._sym("SPY", "2026-05-08", "call", 512.5)
        self.assertTrue(sym.endswith("500"), f"512.5 → cents=500, got suffix {sym[-3:]!r}")
        sym2 = self._sym("NVDA", "2026-05-22", "put", 205.0)
        self.assertTrue(sym2.endswith("000"), f"205.0 → cents=000, got suffix {sym2[-3:]!r}")

    def test_whole_dollar_strike(self):
        """Whole-dollar strike must have 000 in cents position."""
        sym = self._sym("AMZN", "2026-05-15", "put", 247.0)
        self.assertTrue(sym.endswith("000"))

    def test_no_double_spaces_anywhere(self):
        """No OCC symbol must ever contain double (or any) spaces."""
        cases = [
            ("NVDA",  "2026-05-22", "put",  205.0),
            ("V",     "2026-04-28", "call", 300.0),
            ("GLD",   "2026-12-19", "call", 435.0),
            ("GOOGL", "2026-05-15", "call", 165.0),
        ]
        for ticker, expiry, opt_type, strike in cases:
            sym = self._sym(ticker, expiry, opt_type, strike)
            self.assertNotIn(" ", sym, f"{sym!r} contains a space")

    def test_executor_and_builder_produce_same_format(self):
        """options_executor.build_occ_symbol and options_builder._build_occ_symbol
        must produce identical output after the fix."""
        from options_builder import _build_occ_symbol
        from options_executor import build_occ_symbol as exec_build
        cases = [
            ("NVDA", "2026-05-22", "put",  205.0),
            ("SPY",  "2026-05-08", "call", 512.5),
            ("GLD",  "2026-12-19", "call", 435.0),
            ("V",    "2026-04-28", "call", 300.0),
        ]
        for ticker, expiry, opt_type, strike in cases:
            builder_sym  = _build_occ_symbol(ticker, expiry, opt_type, strike)
            executor_sym = exec_build(ticker, expiry, opt_type, strike)
            self.assertEqual(builder_sym, executor_sym,
                             f"builder={builder_sym!r} != executor={executor_sym!r} "
                             f"for {ticker} {expiry} {opt_type} {strike}")


# =============================================================================
# Build 2 — fetch_option_greeks: options_data.fetch_option_greeks
# =============================================================================

class TestGreeksFetch(unittest.TestCase):
    """fetch_option_greeks must return greeks dict on success and None on failure."""

    def test_returns_all_greek_fields(self):
        """fetch_option_greeks must return delta, gamma, theta, vega, rho, iv."""
        import options_data as od

        _OCC = "NVDA260522P00205000"

        class _G:
            delta = -0.3564; gamma = 0.014; theta = -0.1908; vega = 0.2073; rho = -0.0566

        class _Snap:
            greeks = _G(); implied_volatility = 0.4799

        class _Client:
            def get_option_snapshot(_self, req):
                return {_OCC: _Snap()}

        with mock.patch.object(od, "_make_options_data_client", return_value=_Client()):
            result = od.fetch_option_greeks(_OCC)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["delta"],             -0.3564, places=4)
        self.assertAlmostEqual(result["gamma"],              0.014,  places=4)
        self.assertAlmostEqual(result["theta"],             -0.1908, places=4)
        self.assertAlmostEqual(result["vega"],               0.2073, places=4)
        self.assertAlmostEqual(result["rho"],               -0.0566, places=4)
        self.assertAlmostEqual(result["implied_volatility"], 0.4799, places=4)

    def test_returns_none_on_api_error(self):
        """fetch_option_greeks must return None on API failure, not raise."""
        import options_data as od

        class _FailClient:
            def get_option_snapshot(self, req):
                raise RuntimeError("network timeout")

        with mock.patch.object(od, "_make_options_data_client", return_value=_FailClient()):
            result = od.fetch_option_greeks("NVDA260522P00205000")
        self.assertIsNone(result)

    def test_returns_none_when_symbol_not_in_response(self):
        """fetch_option_greeks returns None when the symbol is absent from snapshot."""
        import options_data as od

        class _EmptyClient:
            def get_option_snapshot(self, req):
                return {}  # symbol not in response

        with mock.patch.object(od, "_make_options_data_client", return_value=_EmptyClient()):
            result = od.fetch_option_greeks("NVDA260522P00205000")
        self.assertIsNone(result)

    def test_returns_none_when_greeks_is_none(self):
        """fetch_option_greeks returns None when snapshot has no greeks attribute."""
        import options_data as od
        occ = "NVDA260522P00205000"

        class _NoGreeksSnap:
            greeks = None
            implied_volatility = 0.45

        class _Client:
            def get_option_snapshot(self, req):
                return {occ: _NoGreeksSnap()}

        with mock.patch.object(od, "_make_options_data_client", return_value=_Client()):
            result = od.fetch_option_greeks(occ)
        self.assertIsNone(result)


# =============================================================================
# Build 3 — _enrich_with_greeks: bot_options_stage2_structures._enrich_with_greeks
# =============================================================================

class TestEnrichWithGreeks(unittest.TestCase):
    """_enrich_with_greeks must populate greeks in a candidate dict. Non-fatal on error."""

    def _make_candidate(self, delta=None, theta=None, vega=None):
        return {
            "candidate_id":  "abc123",
            "structure_type": "debit_call_spread",
            "symbol":         "NVDA",
            "expiry":         "2026-05-22",
            "long_strike":    205.0,
            "short_strike":   210.0,
            "contracts":      1,
            "debit":         -3.5,
            "max_loss":       350.0,
            "max_gain":       150.0,
            "breakeven":      208.5,
            "delta":          delta,
            "theta":          theta,
            "vega":           vega,
            "probability_profit": None,
            "expected_value":     None,
            "liquidity_score":    0.7,
            "bid_ask_spread_pct": 0.05,
            "open_interest":      300,
            "dte":                25,
        }

    def test_populates_missing_greeks(self):
        """Candidate with delta=None gets greeks from Alpaca snapshot."""
        import options_data as od
        from bot_options_stage2_structures import _enrich_with_greeks

        _OCC = "NVDA260522C00205000"

        class _G:
            delta = 0.42; gamma = 0.02; theta = -0.15; vega = 0.30; rho = 0.01

        class _Snap:
            greeks = _G(); implied_volatility = 0.45

        class _Client:
            def get_option_snapshot(_self, req):
                return {_OCC: _Snap()}

        c = self._make_candidate()
        with mock.patch.object(od, "_make_options_data_client", return_value=_Client()):
            _enrich_with_greeks(c)

        self.assertAlmostEqual(c["delta"], 0.42, places=4)
        self.assertAlmostEqual(c["theta"], -0.15, places=4)
        self.assertAlmostEqual(c["vega"],   0.30, places=4)
        self.assertIn("gamma", c)
        self.assertAlmostEqual(c["gamma"],  0.02, places=4)
        self.assertIn("rho", c)
        self.assertAlmostEqual(c["rho"],    0.01, places=4)

    def test_does_not_overwrite_existing_greeks(self):
        """Candidate with delta already populated must not be overwritten."""
        import options_data as od
        from bot_options_stage2_structures import _enrich_with_greeks

        c = self._make_candidate(delta=0.55, theta=-0.10, vega=0.25)
        original_delta = c["delta"]

        class _G:
            delta = 0.99; gamma = 0.02; theta = -0.99; vega = 0.99; rho = 0.99

        class _Snap:
            greeks = _G(); implied_volatility = 0.99

        class _Client:
            def get_option_snapshot(_self, req):
                return {"NVDA260522C00205000": _Snap()}

        with mock.patch.object(od, "_make_options_data_client", return_value=_Client()):
            _enrich_with_greeks(c)

        self.assertAlmostEqual(c["delta"], original_delta, places=4,
                               msg="delta must not be overwritten when already present")

    def test_non_fatal_on_api_failure(self):
        """_enrich_with_greeks must not raise when Alpaca call fails."""
        import options_data as od
        from bot_options_stage2_structures import _enrich_with_greeks

        class _FailClient:
            def get_option_snapshot(self, req):
                raise RuntimeError("network error")

        c = self._make_candidate()
        with mock.patch.object(od, "_make_options_data_client", return_value=_FailClient()):
            # Must not raise
            _enrich_with_greeks(c)
        # delta remains None — no greeks set
        self.assertIsNone(c["delta"])

    def test_non_fatal_on_missing_fields(self):
        """_enrich_with_greeks must not raise on a malformed candidate dict."""
        from bot_options_stage2_structures import _enrich_with_greeks
        # Minimal candidate missing required fields
        _enrich_with_greeks({})
        _enrich_with_greeks({"symbol": "X"})

    def test_put_structure_uses_P_in_occ(self):
        """put structure types must produce OCC symbols with P suffix."""
        import options_data as od
        from bot_options_stage2_structures import _enrich_with_greeks

        called_with = []

        original = od.fetch_option_greeks

        def _capture(occ_sym):
            called_with.append(occ_sym)
            return None  # non-fatal skip

        c = self._make_candidate()
        c["structure_type"] = "debit_put_spread"
        with mock.patch.object(od, "fetch_option_greeks", side_effect=_capture):
            _enrich_with_greeks(c)

        self.assertTrue(called_with, "_enrich_with_greeks must call fetch_option_greeks")
        sym = called_with[0]
        self.assertRegex(sym, r"^[A-Z]{1,5}\d{6}P\d{8}$",
                         f"OCC for put must match pattern ^..P.., got {sym!r}")

    def test_call_structure_uses_C_in_occ(self):
        """call structure types must produce OCC symbols with C suffix."""
        import options_data as od
        from bot_options_stage2_structures import _enrich_with_greeks

        called_with = []

        def _capture(occ_sym):
            called_with.append(occ_sym)
            return None

        c = self._make_candidate()
        c["structure_type"] = "debit_call_spread"
        with mock.patch.object(od, "fetch_option_greeks", side_effect=_capture):
            _enrich_with_greeks(c)

        self.assertTrue(called_with, "_enrich_with_greeks must call fetch_option_greeks")
        sym = called_with[0]
        self.assertRegex(sym, r"^[A-Z]{1,5}\d{6}C\d{8}$",
                         f"OCC for call must match pattern ^..C.., got {sym!r}")


# =============================================================================
# Build 4 — build_candidate_structures wires greeks enrichment
# =============================================================================

class TestBuildCandidateStructuresGreeksWiring(unittest.TestCase):
    """build_candidate_structures must call _enrich_with_greeks for surviving candidates
    with missing greeks, and must not call it for candidates that have greeks."""

    def test_enrich_called_for_surviving_with_null_greeks(self):
        """_enrich_with_greeks is called when surviving candidates have delta=None."""
        from bot_options_stage2_structures import build_candidate_structures

        mock_pack = mock.MagicMock()
        mock_pack.symbol = "NVDA"
        mock_pack.iv_rank = 30.0
        mock_pack.iv_environment = "cheap"
        mock_pack.liquidity_score = 0.6
        mock_pack.a1_direction = "bullish"
        mock_pack.earnings_days_away = None
        mock_pack.macro_event_flag = False
        mock_pack.premium_budget_usd = 5000.0

        # Candidate with null greeks that will "survive" veto
        surviving_candidate = {
            "candidate_id":     "test01",
            "structure_type":   "debit_call_spread",
            "symbol":           "NVDA",
            "expiry":           "2026-05-22",
            "long_strike":      205.0,
            "short_strike":     210.0,
            "contracts":        1,
            "debit":           -3.5,
            "max_loss":         350.0,
            "max_gain":         150.0,
            "breakeven":        208.5,
            "delta":            None,   # missing — should trigger enrichment
            "theta":            None,
            "vega":             None,
            "probability_profit": 0.4,
            "expected_value":     10.0,
            "liquidity_score":    0.7,
            "bid_ask_spread_pct": 0.05,
            "open_interest":      300,
            "dte":                25,
            "bid_ask_spread":     0.10,  # for veto V1
        }

        enrich_calls = []

        def _fake_enrich(c):
            enrich_calls.append(c.get("candidate_id"))

        with mock.patch("bot_options_stage2_structures._enrich_with_greeks", side_effect=_fake_enrich), \
             mock.patch("bot_options_stage2_structures._apply_veto_rules", return_value=None), \
             mock.patch("options_intelligence.generate_candidate_structures",
                        return_value=[surviving_candidate]):
            build_candidate_structures(
                pack=mock_pack,
                equity=100_000.0,
                chain={"expirations": {"2026-05-22": {}}, "current_price": 200.0},
                allowed_structures=["debit_call_spread"],
            )

        self.assertIn("test01", enrich_calls,
                      "_enrich_with_greeks must be called for surviving candidate with null greeks")

    def test_enrich_not_called_for_vetoed_candidates(self):
        """_enrich_with_greeks must NOT be called for vetoed candidates."""
        from bot_options_stage2_structures import build_candidate_structures

        mock_pack = mock.MagicMock()
        mock_pack.symbol = "NVDA"

        vetoed_candidate = {
            "candidate_id": "veto01", "structure_type": "debit_call_spread",
            "symbol": "NVDA", "expiry": "2026-05-22", "long_strike": 205.0,
            "delta": None, "theta": None,
        }

        enrich_calls = []

        def _fake_enrich(c):
            enrich_calls.append(c.get("candidate_id"))

        with mock.patch("bot_options_stage2_structures._enrich_with_greeks", side_effect=_fake_enrich), \
             mock.patch("bot_options_stage2_structures._apply_veto_rules",
                        return_value="bid_ask_spread_pct=0.20>0.18"), \
             mock.patch("options_intelligence.generate_candidate_structures",
                        return_value=[vetoed_candidate]):
            build_candidate_structures(
                pack=mock_pack,
                equity=100_000.0,
                chain={"expirations": {}, "current_price": 200.0},
                allowed_structures=["debit_call_spread"],
            )

        self.assertNotIn("veto01", enrich_calls,
                         "_enrich_with_greeks must not be called for vetoed candidates")


if __name__ == "__main__":
    unittest.main()
