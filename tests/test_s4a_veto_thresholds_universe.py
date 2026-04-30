"""
tests/test_s4a_veto_thresholds_universe.py — S4-A tests.

Covers:
  Build 1 — _apply_veto_rules reads thresholds from config (not hardcoded)
  Build 2 — validate_config passes with new a2_veto_thresholds values
  Build 3 — expanded universe contains expected symbols
  Build 4 — strategy_config.json has a2_veto_thresholds with correct fields
  Build 5 — default veto thresholds unchanged when config absent
"""

import json
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))
os.chdir(_BOT_DIR)

_THIRD_PARTY_STUBS = {
    "dotenv":                  None,
    "anthropic":               None,
    "alpaca":                  None,
    "alpaca.trading":          None,
    "alpaca.trading.client":   None,
    "alpaca.trading.requests": None,
    "alpaca.trading.enums":    None,
}
for _stub_name in _THIRD_PARTY_STUBS:
    if _stub_name not in sys.modules:
        _m = mock.MagicMock()
        if _stub_name == "dotenv":
            _m.load_dotenv = mock.MagicMock()
        sys.modules[_stub_name] = _m


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_pack(**overrides):
    from schemas import A2FeaturePack
    defaults = dict(
        symbol="GS",
        a1_signal_score=72.0,
        a1_direction="bullish",
        trend_score=None,
        momentum_score=None,
        sector_alignment="financials",
        iv_rank=30.0,
        iv_environment="cheap",
        term_structure_slope=None,
        skew=None,
        expected_move_pct=3.5,
        flow_imbalance_30m=None,
        sweep_count=None,
        gex_regime=None,
        oi_concentration=None,
        earnings_days_away=None,
        macro_event_flag=False,
        premium_budget_usd=5000.0,
        liquidity_score=0.7,
        built_at=datetime.now(timezone.utc).isoformat(),
        data_sources=["signal_scores", "iv_history"],
    )
    defaults.update(overrides)
    return A2FeaturePack(**defaults)


def _veto(candidate, equity=100_000.0, config=None):
    from bot_options_stage2_structures import _apply_veto_rules
    return _apply_veto_rules(candidate, _make_pack(), equity, config=config)


# ════════════════════════════════════════════════════════════════════════════
# Build 1 — _apply_veto_rules reads thresholds from config
# ════════════════════════════════════════════════════════════════════════════

class TestVetoRulesConfigDriven(unittest.TestCase):

    # ── V1: bid_ask_spread_pct ────────────────────────────────────────────────

    def test_v1_default_blocks_at_0_05(self):
        # Spread = 0.06 > 0.05 default → veto
        result = _veto({"bid_ask_spread_pct": 0.06})
        self.assertIsNotNone(result)
        self.assertIn("bid_ask_spread_pct", result)

    def test_v1_default_passes_below_0_05(self):
        result = _veto({"bid_ask_spread_pct": 0.04})
        self.assertIsNone(result)

    def test_v1_config_widened_to_0_15_passes_gs(self):
        # GS observed at 9.1% — must pass with widened threshold
        config = {"a2_veto_thresholds": {"max_bid_ask_spread_pct": 0.15}}
        result = _veto({"bid_ask_spread_pct": 0.091}, config=config)
        self.assertIsNone(result, "GS 9.1% spread should pass with threshold=0.15")

    def test_v1_config_widened_to_0_15_passes_tsm(self):
        # TSM observed at 11.8%
        config = {"a2_veto_thresholds": {"max_bid_ask_spread_pct": 0.15}}
        result = _veto({"bid_ask_spread_pct": 0.118}, config=config)
        self.assertIsNone(result, "TSM 11.8% spread should pass with threshold=0.15")

    def test_v1_config_widened_to_0_15_passes_amzn(self):
        # AMZN observed at 5.6%
        config = {"a2_veto_thresholds": {"max_bid_ask_spread_pct": 0.15}}
        result = _veto({"bid_ask_spread_pct": 0.056}, config=config)
        self.assertIsNone(result, "AMZN 5.6% spread should pass with threshold=0.15")

    def test_v1_config_widened_to_0_15_still_blocks_xlf(self):
        # XLF observed at 33.7% — must still be blocked even with wider threshold
        config = {"a2_veto_thresholds": {"max_bid_ask_spread_pct": 0.15}}
        result = _veto({"bid_ask_spread_pct": 0.337}, config=config)
        self.assertIsNotNone(result, "XLF 33.7% spread should still be blocked at threshold=0.15")

    def test_v1_config_0_15_matches_strategy_config_json(self):
        # Value widened to 0.18 in S5-CONFIG-ACTIVITY "Busier, Same-ish Risk" pass
        cfg = json.loads((_BOT_DIR / "strategy_config.json").read_text())
        vt = cfg.get("a2_veto_thresholds", {})
        self.assertAlmostEqual(float(vt.get("max_bid_ask_spread_pct", 0)), 0.18,
                               msg="strategy_config.json max_bid_ask_spread_pct should be 0.18")

    def test_v1_config_tightened_blocks_previously_passing(self):
        # Tighter config → lower spread blocked
        config = {"a2_veto_thresholds": {"max_bid_ask_spread_pct": 0.03}}
        result = _veto({"bid_ask_spread_pct": 0.04}, config=config)
        self.assertIsNotNone(result, "0.04 spread should be blocked with threshold=0.03")

    # ── V2: open_interest ─────────────────────────────────────────────────────

    def test_v2_default_blocks_below_50(self):
        # Default OI floor lowered from 100 to 50 (Sprint 10 Phase 6).
        result = _veto({"open_interest": 49})
        self.assertIsNotNone(result)
        self.assertIn("open_interest", result)

    def test_v2_default_passes_at_50(self):
        # OI exactly at the new default floor passes (< not <=).
        result = _veto({"open_interest": 50})
        self.assertIsNone(result)

    def test_v2_config_raises_floor(self):
        config = {"a2_veto_thresholds": {"min_open_interest": 500}}
        result = _veto({"open_interest": 300}, config=config)
        self.assertIsNotNone(result, "OI=300 should be blocked with floor=500")

    def test_v2_config_lowers_floor(self):
        config = {"a2_veto_thresholds": {"min_open_interest": 50}}
        result = _veto({"open_interest": 75}, config=config)
        self.assertIsNone(result, "OI=75 should pass with floor=50")

    # ── V3: theta_decay_rate ──────────────────────────────────────────────────

    def test_v3_default_blocks_above_0_05(self):
        result = _veto({"theta": -0.10, "debit": 1.50})
        self.assertIsNotNone(result)
        self.assertIn("theta_decay_rate", result)

    def test_v3_default_passes_at_threshold(self):
        # rate = 0.05 / 1.0 = 0.05 exactly → passes (not strictly greater)
        result = _veto({"theta": -0.05, "debit": 1.0})
        self.assertIsNone(result)

    # ── V5: DTE ───────────────────────────────────────────────────────────────

    def test_v5_default_blocks_below_5(self):
        result = _veto({"dte": 3})
        self.assertIsNotNone(result)
        self.assertIn("dte", result)

    def test_v5_default_passes_at_5(self):
        result = _veto({"dte": 5})
        self.assertIsNone(result)

    def test_v5_config_tightened(self):
        config = {"a2_veto_thresholds": {"min_dte": 7}}
        result = _veto({"dte": 6}, config=config)
        self.assertIsNotNone(result, "DTE=6 should be blocked with min_dte=7")

    # ── V6: expected_value ────────────────────────────────────────────────────

    def test_v6_blocks_negative_ev(self):
        result = _veto({"expected_value": -0.10})
        self.assertIsNotNone(result)
        self.assertIn("expected_value", result)

    def test_v6_passes_zero_ev(self):
        result = _veto({"expected_value": 0.0})
        self.assertIsNone(result)

    # ── No fields present ─────────────────────────────────────────────────────

    def test_empty_candidate_passes_all_rules(self):
        # No fields = no data to veto on
        result = _veto({})
        self.assertIsNone(result)

    # ── Config vs no-config parity ────────────────────────────────────────────

    def test_default_config_identical_to_no_config(self):
        from bot_options_stage2_structures import _A2_VETO_DEFAULTS
        explicit_config = {"a2_veto_thresholds": dict(_A2_VETO_DEFAULTS)}
        cand = {"bid_ask_spread_pct": 0.06, "open_interest": 50, "dte": 3}
        result_none   = _veto(cand, config=None)
        result_default = _veto(cand, config=explicit_config)
        self.assertEqual(result_none, result_default,
                         "Explicit defaults must produce identical results to no config")


# ════════════════════════════════════════════════════════════════════════════
# Build 2 — strategy_config.json a2_veto_thresholds section
# ════════════════════════════════════════════════════════════════════════════

class TestStrategyConfigVetoThresholds(unittest.TestCase):

    def _cfg(self):
        return json.loads((_BOT_DIR / "strategy_config.json").read_text())

    def test_a2_veto_thresholds_section_present(self):
        cfg = self._cfg()
        self.assertIn("a2_veto_thresholds", cfg)

    def test_required_keys_present(self):
        vt = self._cfg()["a2_veto_thresholds"]
        for key in ("max_bid_ask_spread_pct", "min_open_interest",
                    "max_theta_decay_pct", "min_dte", "min_expected_value"):
            self.assertIn(key, vt, f"Required key {key!r} missing from a2_veto_thresholds")

    def test_tuned_comment_present(self):
        vt = self._cfg()["a2_veto_thresholds"]
        self.assertIn("_tuned", vt, "_tuned rationale comment must be present")

    def test_spread_threshold_is_0_18(self):
        # Widened from 0.15 to 0.18 in S5-CONFIG-ACTIVITY "Busier, Same-ish Risk" pass
        vt = self._cfg()["a2_veto_thresholds"]
        self.assertAlmostEqual(float(vt["max_bid_ask_spread_pct"]), 0.18)

    def test_oi_floor_unchanged_at_100(self):
        vt = self._cfg()["a2_veto_thresholds"]
        self.assertEqual(int(vt["min_open_interest"]), 100)

    def test_theta_unchanged_at_0_05(self):
        vt = self._cfg()["a2_veto_thresholds"]
        self.assertAlmostEqual(float(vt["max_theta_decay_pct"]), 0.05)

    def test_min_dte_unchanged_at_5(self):
        vt = self._cfg()["a2_veto_thresholds"]
        self.assertEqual(int(vt["min_dte"]), 5)


# ════════════════════════════════════════════════════════════════════════════
# Build 3 — expanded universe
# ════════════════════════════════════════════════════════════════════════════

class TestExpandedUniverse(unittest.TestCase):

    def _fallback_symbols(self) -> list[str]:
        """Get the fallback universe without importing watchlist_manager."""
        _mock_wm = mock.MagicMock()
        _mock_wm.get_active_watchlist.side_effect = ImportError("stub")
        with mock.patch.dict(sys.modules, {"watchlist_manager": _mock_wm}):
            # Force re-import to pick up stub
            if "bot_options_stage1_candidates" in sys.modules:
                del sys.modules["bot_options_stage1_candidates"]
            from bot_options_stage1_candidates import _get_core_equity_symbols
            try:
                return _get_core_equity_symbols()
            except Exception:
                pass
        return []

    def test_fallback_exceeds_16_symbols(self):
        # When watchlist_manager works (or fails), we care about the fallback size
        # Test the module constant directly
        from bot_options_stage0_preflight import _OBS_IV_SYMBOLS
        self.assertGreater(len(_OBS_IV_SYMBOLS), 16,
                           "Expanded universe must have more than the original 16 symbols")

    def test_obs_iv_symbols_contains_watchlist_core_names(self):
        from bot_options_stage0_preflight import _OBS_IV_SYMBOLS
        expected = ["NVDA", "TSM", "MSFT", "PLTR", "JPM", "GS", "XLF",
                    "AMZN", "WMT", "LMT", "RTX", "XBI", "JNJ", "SPY",
                    "QQQ", "IWM", "TLT", "FRO", "STNG", "GLD", "EWJ", "FXI"]
        for sym in expected:
            self.assertIn(sym, _OBS_IV_SYMBOLS, f"{sym} should be in expanded universe")

    def test_obs_iv_symbols_excludes_crypto(self):
        from bot_options_stage0_preflight import _OBS_IV_SYMBOLS
        self.assertNotIn("BTC/USD", _OBS_IV_SYMBOLS, "Crypto BTC/USD must not be in A2 universe")
        self.assertNotIn("ETH/USD", _OBS_IV_SYMBOLS, "Crypto ETH/USD must not be in A2 universe")

    def test_obs_iv_symbols_contains_citrini_additions(self):
        # Citrini-driven additions from watchlist_core: EWM, ECH, RKT, BE, COPX
        from bot_options_stage0_preflight import _OBS_IV_SYMBOLS
        for sym in ("EWM", "ECH", "RKT", "BE", "COPX"):
            self.assertIn(sym, _OBS_IV_SYMBOLS, f"Citrini symbol {sym} should be in expanded universe")

    def test_obs_iv_symbols_no_duplicates(self):
        from bot_options_stage0_preflight import _OBS_IV_SYMBOLS
        self.assertEqual(len(_OBS_IV_SYMBOLS), len(set(_OBS_IV_SYMBOLS)),
                         "_OBS_IV_SYMBOLS must not contain duplicates")

    def test_fallback_in_stage1_excludes_crypto(self):
        # Check that crypto symbols are not present in the returned lists
        # by mocking watchlist_manager to force the fallback path.
        # Temporarily remove cached module to force fallback path on reimport
        _saved = sys.modules.pop("bot_options_stage1_candidates", None)
        _mock_wm = mock.MagicMock()
        _mock_wm.get_active_watchlist.side_effect = Exception("force fallback")
        with mock.patch.dict(sys.modules, {"watchlist_manager": _mock_wm}):
            import bot_options_stage1_candidates as s1_fresh
            result = s1_fresh._get_core_equity_symbols()
        if _saved is not None:
            sys.modules["bot_options_stage1_candidates"] = _saved
        self.assertNotIn("BTC/USD", result, "BTC/USD must not appear in fallback equity list")
        self.assertNotIn("ETH/USD", result, "ETH/USD must not appear in fallback equity list")

    def test_fallback_in_stage1_contains_expanded_symbols(self):
        import inspect

        import bot_options_stage1_candidates as s1
        src = inspect.getsource(s1._get_core_equity_symbols)
        for sym in ("JPM", "GS", "LMT", "RTX", "EWM", "ECH", "RKT", "BE"):
            self.assertIn(sym, src, f"Expanded symbol {sym} should be in stage1 fallback")

    def test_universe_size_at_least_39(self):
        from bot_options_stage0_preflight import _OBS_IV_SYMBOLS
        self.assertGreaterEqual(len(_OBS_IV_SYMBOLS), 39,
                                "Universe should cover at least 39 optionable symbols")


# ════════════════════════════════════════════════════════════════════════════
# Build 4 — _get_veto_config helper
# ════════════════════════════════════════════════════════════════════════════

class TestGetVetoConfig(unittest.TestCase):

    def _get(self, config=None):
        from bot_options_stage2_structures import _get_veto_config
        return _get_veto_config(config)

    def test_none_config_returns_defaults(self):
        from bot_options_stage2_structures import _A2_VETO_DEFAULTS
        result = self._get(None)
        for k, v in _A2_VETO_DEFAULTS.items():
            self.assertEqual(result[k], v)

    def test_empty_config_returns_defaults(self):
        from bot_options_stage2_structures import _A2_VETO_DEFAULTS
        result = self._get({})
        for k, v in _A2_VETO_DEFAULTS.items():
            self.assertEqual(result[k], v)

    def test_partial_config_merges_with_defaults(self):
        config = {"a2_veto_thresholds": {"max_bid_ask_spread_pct": 0.20}}
        result = self._get(config)
        self.assertAlmostEqual(result["max_bid_ask_spread_pct"], 0.20)
        self.assertEqual(result["min_open_interest"], 50)   # new default (P6)

    def test_full_config_overrides_all_defaults(self):
        config = {"a2_veto_thresholds": {
            "max_bid_ask_spread_pct": 0.20,
            "min_open_interest":      200,
            "max_theta_decay_pct":    0.08,
            "min_dte":                7,
            "min_expected_value":     0.10,
        }}
        result = self._get(config)
        self.assertAlmostEqual(result["max_bid_ask_spread_pct"], 0.20)
        self.assertEqual(result["min_open_interest"], 200)
        self.assertAlmostEqual(result["max_theta_decay_pct"], 0.08)
        self.assertEqual(result["min_dte"], 7)
        self.assertAlmostEqual(result["min_expected_value"], 0.10)


# ════════════════════════════════════════════════════════════════════════════
# Build 5 — validate_config.py passes with new a2_veto_thresholds
# ════════════════════════════════════════════════════════════════════════════

class TestValidateConfigVetoThresholds(unittest.TestCase):
    """
    Smoke-test that validate_config.py's a2_veto_thresholds gate runs without FAIL
    against the current strategy_config.json.
    """

    def test_validate_config_passes_with_current_config(self):
        cfg = json.loads((_BOT_DIR / "strategy_config.json").read_text())
        vt = cfg.get("a2_veto_thresholds", {})
        self.assertIsNotNone(vt, "a2_veto_thresholds must be present")

        spread = float(vt.get("max_bid_ask_spread_pct", 0))
        oi     = int(vt.get("min_open_interest", 0))
        theta  = float(vt.get("max_theta_decay_pct", 0))
        dte    = int(vt.get("min_dte", 0))

        self.assertTrue(0.01 <= spread <= 0.50, f"spread={spread} out of valid range")
        self.assertTrue(10 <= oi <= 10000, f"oi={oi} out of valid range")
        self.assertTrue(0.001 <= theta <= 0.50, f"theta={theta} out of valid range")
        self.assertTrue(1 <= dte <= 30, f"dte={dte} out of valid range")

    def test_spread_passes_new_threshold(self):
        # The widened threshold should be in the acceptable range for validate_config
        cfg = json.loads((_BOT_DIR / "strategy_config.json").read_text())
        spread = float(cfg["a2_veto_thresholds"]["max_bid_ask_spread_pct"])
        self.assertGreater(spread, 0.05, "Widened threshold should be > v1 default 0.05")
        self.assertLessEqual(spread, 0.50, "Should not exceed 50% (nonsensical)")


if __name__ == "__main__":
    unittest.main()
