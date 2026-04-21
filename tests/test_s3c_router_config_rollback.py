"""
tests/test_s3c_router_config_rollback.py — S3-C tests.

Covers:
  - Router reads thresholds from config (non-default values alter gate behaviour)
  - validate_no_trade_reason raises on unknown value
  - Rollback flag force_no_trade skips Stage 1 candidate generation
  - Rollback flag disable_candidate_generation skips Stage 1
  - Rollback flag disable_bounded_debate skips Stage 3 debate
  - Each rollback flag tested independently
  - Default config (all false) leaves pipeline unaffected
"""

import os
import sys  # noqa: F401 — also used in mock.patch.dict(sys.modules, ...)
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))
os.chdir(_BOT_DIR)

# Stub third-party packages absent from local (non-venv) environments.
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
        symbol="AAPL",
        a1_signal_score=72.0,
        a1_direction="bullish",
        trend_score=None,
        momentum_score=None,
        sector_alignment="technology",
        iv_rank=35.0,
        iv_environment="cheap",
        term_structure_slope=None,
        skew=None,
        expected_move_pct=4.5,
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


# ════════════════════════════════════════════════════════════════════════════
# Build 1 — Router reads thresholds from config
# ════════════════════════════════════════════════════════════════════════════

class TestRouterConfigThresholds(unittest.TestCase):

    def _route(self, pack, config=None):
        from bot_options_stage2_structures import _route_strategy
        return _route_strategy(pack, config=config)

    # ── earnings_dte_blackout ─────────────────────────────────────────────────

    def test_earnings_blackout_default_allows_beyond_5(self):
        # earnings_days_away=7, default blackout=5 → should not be blocked by Rule 1
        pack = _make_pack(earnings_days_away=7, iv_environment="cheap", a1_direction="bullish",
                          liquidity_score=0.7)
        result = self._route(pack)
        self.assertNotEqual(result, [], "7 days away should pass the default 5-day blackout")

    def test_earnings_blackout_config_extends_gate(self):
        # earnings_days_away=7, config blackout=10 → Rule 1 fires
        pack = _make_pack(earnings_days_away=7, iv_environment="cheap", a1_direction="bullish",
                          liquidity_score=0.7)
        config = {"a2_router": {"earnings_dte_blackout": 10}}
        result = self._route(pack, config=config)
        self.assertEqual(result, [], "7 days away should be blocked by 10-day config blackout")

    def test_earnings_blackout_default_blocks_within_5(self):
        pack = _make_pack(earnings_days_away=3, iv_environment="cheap", a1_direction="bullish",
                          liquidity_score=0.7)
        result = self._route(pack)
        self.assertEqual(result, [], "3 days away should be blocked by default 5-day blackout")

    # ── min_liquidity_score ───────────────────────────────────────────────────

    def test_liquidity_default_allows_above_0_3(self):
        pack = _make_pack(liquidity_score=0.4, iv_environment="cheap", a1_direction="bullish")
        result = self._route(pack)
        self.assertNotEqual(result, [], "liquidity=0.4 should pass the default 0.3 floor")

    def test_liquidity_config_raises_floor(self):
        # liquidity_score=0.4, config floor=0.5 → Rule 3 fires
        pack = _make_pack(liquidity_score=0.4, iv_environment="cheap", a1_direction="bullish")
        config = {"a2_router": {"min_liquidity_score": 0.5}}
        result = self._route(pack, config=config)
        self.assertEqual(result, [], "liquidity=0.4 should be blocked by config floor=0.5")

    def test_liquidity_config_lowers_floor(self):
        # liquidity_score=0.2, config floor=0.1 → not blocked by Rule 3
        pack = _make_pack(liquidity_score=0.2, iv_environment="cheap", a1_direction="bullish")
        config = {"a2_router": {"min_liquidity_score": 0.1}}
        result = self._route(pack, config=config)
        self.assertNotEqual(result, [], "liquidity=0.2 should pass config floor=0.1")

    # ── macro_iv_gate_rank ────────────────────────────────────────────────────

    def test_macro_iv_gate_default_blocks_above_60(self):
        pack = _make_pack(macro_event_flag=True, iv_rank=65.0, iv_environment="cheap",
                          a1_direction="bullish", liquidity_score=0.7)
        result = self._route(pack)
        self.assertEqual(result, [], "iv_rank=65 with macro event should be blocked by default gate=60")

    def test_macro_iv_gate_config_raises_threshold(self):
        # iv_rank=65, config gate=70 → not blocked
        pack = _make_pack(macro_event_flag=True, iv_rank=65.0, iv_environment="cheap",
                          a1_direction="bullish", liquidity_score=0.7)
        config = {"a2_router": {"macro_iv_gate_rank": 70}}
        result = self._route(pack, config=config)
        self.assertNotEqual(result, [], "iv_rank=65 should pass config gate=70")

    def test_macro_iv_gate_config_lowers_threshold(self):
        # iv_rank=55, config gate=50 → blocked
        pack = _make_pack(macro_event_flag=True, iv_rank=55.0, iv_environment="cheap",
                          a1_direction="bullish", liquidity_score=0.7)
        config = {"a2_router": {"macro_iv_gate_rank": 50}}
        result = self._route(pack, config=config)
        self.assertEqual(result, [], "iv_rank=55 should be blocked by config gate=50")

    # ── iv_env_blackout ───────────────────────────────────────────────────────

    def test_iv_env_blackout_default_routes_very_expensive_to_credit(self):
        # S7-VOL: very_expensive now routes to credit structures via RULE2_CREDIT
        pack = _make_pack(iv_environment="very_expensive", a1_direction="bullish",
                          liquidity_score=0.7)
        result = self._route(pack)
        self.assertEqual(result, ["credit_put_spread"],
                         "very_expensive + bullish should route to credit_put_spread")

    def test_iv_env_blackout_config_extends_to_expensive(self):
        # expensive also blocked when config adds it
        pack = _make_pack(iv_environment="expensive", a1_direction="bullish",
                          liquidity_score=0.7)
        config = {"a2_router": {"iv_env_blackout": ["very_expensive", "expensive"]}}
        result = self._route(pack, config=config)
        self.assertEqual(result, [], "expensive should be blocked when in config iv_env_blackout")

    def test_iv_env_blackout_config_empty_very_expensive_neutral(self):
        # S7-VOL: RULE2_CREDIT hardcoded — blackout config is irrelevant for very_expensive.
        # Neutral direction → both sides offered.
        pack = _make_pack(iv_environment="very_expensive", a1_direction="neutral",
                          liquidity_score=0.7)
        config = {"a2_router": {"iv_env_blackout": []}}
        result = self._route(pack, config=config)
        self.assertEqual(result, ["credit_put_spread", "credit_call_spread"])

    # ── Default behavior unchanged when config matches defaults ───────────────

    def test_default_config_matches_no_config_cheap_bullish(self):
        from bot_options_stage2_structures import _A2_ROUTER_DEFAULTS
        pack = _make_pack(iv_environment="cheap", a1_direction="bullish", liquidity_score=0.7)
        explicit_default_config = {"a2_router": dict(_A2_ROUTER_DEFAULTS)}
        result_none   = self._route(pack, config=None)
        result_default = self._route(pack, config=explicit_default_config)
        self.assertEqual(result_none, result_default,
                         "Explicit defaults must produce identical results to no config")

    def test_default_config_matches_no_config_blocked(self):
        from bot_options_stage2_structures import _A2_ROUTER_DEFAULTS
        pack = _make_pack(liquidity_score=0.1, iv_environment="cheap", a1_direction="bullish")
        explicit_default_config = {"a2_router": dict(_A2_ROUTER_DEFAULTS)}
        result_none    = self._route(pack, config=None)
        result_default = self._route(pack, config=explicit_default_config)
        self.assertEqual(result_none, [], "Below floor should block")
        self.assertEqual(result_none, result_default)


# ════════════════════════════════════════════════════════════════════════════
# Build 2 — validate_no_trade_reason
# ════════════════════════════════════════════════════════════════════════════

class TestValidateNoTradeReason(unittest.TestCase):

    def _validate(self, reason):
        from schemas import validate_no_trade_reason
        return validate_no_trade_reason(reason)

    def test_valid_reason_returns_it(self):
        result = self._validate("debate_low_confidence")
        self.assertEqual(result, "debate_low_confidence")

    def test_all_taxonomy_values_pass(self):
        from schemas import NO_TRADE_REASONS
        for reason in NO_TRADE_REASONS:
            result = self._validate(reason)
            self.assertEqual(result, reason, f"Taxonomy value {reason!r} should pass")

    def test_rollback_active_is_in_taxonomy(self):
        result = self._validate("rollback_active")
        self.assertEqual(result, "rollback_active")

    def test_unknown_reason_raises_value_error(self):
        with self.assertRaises(ValueError):
            self._validate("totally_made_up_reason")

    def test_empty_string_raises_value_error(self):
        with self.assertRaises(ValueError):
            self._validate("")

    def test_none_like_string_raises_value_error(self):
        with self.assertRaises(ValueError):
            self._validate("none")

    def test_error_message_lists_valid_values(self):
        try:
            self._validate("unknown_xyz")
            self.fail("Should have raised ValueError")
        except ValueError as exc:
            self.assertIn("NO_TRADE_REASONS", str(exc))


# ════════════════════════════════════════════════════════════════════════════
# Build 3 — Rollback flags
# ════════════════════════════════════════════════════════════════════════════

class TestRollbackFlagStage1(unittest.TestCase):
    """force_no_trade and disable_candidate_generation skip run_candidate_stage."""

    def _run_stage1(self, config):
        from bot_options_stage1_candidates import run_candidate_stage
        # Rollback check happens before any imports in the function body, so
        # no mocking needed for early-exit cases.
        result = run_candidate_stage(
            signal_scores={},
            iv_summaries={},
            equity=100_000.0,
            vix=18.0,
            equity_symbols=[],
            config=config,
        )
        return result

    def test_force_no_trade_returns_empty(self):
        config = {"a2_rollback": {"force_no_trade": True, "disable_candidate_generation": False,
                                  "disable_bounded_debate": False}}
        candidate_sets, proposals, allowed_by_sym, all_structs = self._run_stage1(config)
        self.assertEqual(candidate_sets, [])
        self.assertEqual(proposals, [])
        self.assertEqual(allowed_by_sym, {})
        self.assertEqual(all_structs, [])

    def test_disable_candidate_generation_returns_empty(self):
        config = {"a2_rollback": {"force_no_trade": False, "disable_candidate_generation": True,
                                  "disable_bounded_debate": False}}
        candidate_sets, proposals, allowed_by_sym, all_structs = self._run_stage1(config)
        self.assertEqual(candidate_sets, [])
        self.assertEqual(proposals, [])

    def test_force_no_trade_independent_of_disable_candidate_generation(self):
        # force_no_trade=True, disable_candidate_generation=False → still skips
        config = {"a2_rollback": {"force_no_trade": True, "disable_candidate_generation": False,
                                  "disable_bounded_debate": False}}
        result = self._run_stage1(config)
        self.assertEqual(result[0], [])

    def test_disable_candidate_generation_independent_of_force_no_trade(self):
        # disable_candidate_generation=True, force_no_trade=False → still skips
        config = {"a2_rollback": {"force_no_trade": False, "disable_candidate_generation": True,
                                  "disable_bounded_debate": False}}
        result = self._run_stage1(config)
        self.assertEqual(result[0], [])

    def test_both_false_does_not_skip(self):
        # Both flags false → stage runs normally; verify by checking it returns a 4-tuple
        # and does NOT return the early-exit empty tuple immediately.
        # We stub options_data in sys.modules to avoid needing the full venv.
        config = {"a2_rollback": {"force_no_trade": False, "disable_candidate_generation": False,
                                  "disable_bounded_debate": False}}
        _mock_od = mock.MagicMock()
        _mock_od.get_options_regime.return_value = "neutral"
        _mock_oi = mock.MagicMock()
        with mock.patch.dict(sys.modules, {
            "options_data": _mock_od,
            "options_intelligence": _mock_oi,
        }):
            from bot_options_stage1_candidates import run_candidate_stage
            run_candidate_stage(
                signal_scores={}, iv_summaries={}, equity=100_000.0,
                vix=18.0, equity_symbols=[], config=config,
            )
        _mock_od.get_options_regime.assert_called_once()

    def test_no_rollback_section_does_not_skip(self):
        # Missing a2_rollback key → no skip
        _mock_od = mock.MagicMock()
        _mock_od.get_options_regime.return_value = "neutral"
        _mock_oi = mock.MagicMock()
        with mock.patch.dict(sys.modules, {
            "options_data": _mock_od,
            "options_intelligence": _mock_oi,
        }):
            from bot_options_stage1_candidates import run_candidate_stage
            run_candidate_stage(
                signal_scores={}, iv_summaries={}, equity=100_000.0,
                vix=18.0, equity_symbols=[], config={},
            )
        _mock_od.get_options_regime.assert_called_once()


class TestRollbackFlagStage3(unittest.TestCase):
    """disable_bounded_debate and force_no_trade skip run_bounded_debate."""

    def _run_debate(self, config):
        from bot_options_stage3_debate import run_bounded_debate
        return run_bounded_debate(
            candidate_sets=[],
            candidates=[],
            candidate_structures=[],
            allowed_by_sym={},
            equity=100_000.0,
            vix=18.0,
            regime="risk_on",
            account1_summary="",
            obs_mode=False,
            session_tier="market",
            iv_summaries={},
            t_start=time.monotonic(),
            config=config,
        )

    def test_disable_bounded_debate_returns_rollback_record(self):
        config = {"a2_rollback": {"disable_bounded_debate": True, "force_no_trade": False,
                                  "disable_candidate_generation": False}}
        record = self._run_debate(config)
        self.assertEqual(record.no_trade_reason, "rollback_active")
        self.assertEqual(record.execution_result, "no_trade")
        self.assertIsNone(record.debate_input)
        self.assertIsNone(record.selected_candidate)

    def test_force_no_trade_in_stage3_returns_rollback_record(self):
        config = {"a2_rollback": {"force_no_trade": True, "disable_bounded_debate": False,
                                  "disable_candidate_generation": False}}
        record = self._run_debate(config)
        self.assertEqual(record.no_trade_reason, "rollback_active")
        self.assertEqual(record.execution_result, "no_trade")

    def test_disable_bounded_debate_independent_of_force_no_trade(self):
        # disable_bounded_debate=True, force_no_trade=False → still skips
        config = {"a2_rollback": {"disable_bounded_debate": True, "force_no_trade": False,
                                  "disable_candidate_generation": False}}
        record = self._run_debate(config)
        self.assertEqual(record.no_trade_reason, "rollback_active")

    def test_force_no_trade_independent_in_stage3(self):
        # force_no_trade=True, disable_bounded_debate=False → still skips
        config = {"a2_rollback": {"force_no_trade": True, "disable_bounded_debate": False,
                                  "disable_candidate_generation": False}}
        record = self._run_debate(config)
        self.assertEqual(record.no_trade_reason, "rollback_active")

    def test_all_false_does_not_skip_debate(self):
        # All flags false → debate proceeds (mock Claude so it doesn't actually call)
        config = {"a2_rollback": {"force_no_trade": False, "disable_bounded_debate": False,
                                  "disable_candidate_generation": False}}
        import bot_options_stage3_debate as s3
        with mock.patch.object(s3, "_get_claude") as _gc:
            _mock_client = mock.MagicMock()
            _gc.return_value = _mock_client
            _mock_resp = mock.MagicMock()
            _mock_resp.content = [mock.MagicMock(text='{"selected_candidate_id": null, "confidence": 0.5, "reject": true, "key_risks": [], "reasons": "no setup", "recommended_size_modifier": 1.0}')]
            _mock_resp.usage = mock.MagicMock(
                input_tokens=100, output_tokens=50,
                cache_read_input_tokens=0, cache_creation_input_tokens=0,
            )
            _mock_client.messages.create.return_value = _mock_resp
            record = self._run_debate(config)
        # rollback was NOT triggered — record was built from debate result
        self.assertNotEqual(record.no_trade_reason, "rollback_active")

    def test_rollback_record_is_a2_decision_record(self):
        from schemas import A2DecisionRecord
        config = {"a2_rollback": {"disable_bounded_debate": True, "force_no_trade": False,
                                  "disable_candidate_generation": False}}
        record = self._run_debate(config)
        self.assertIsInstance(record, A2DecisionRecord)

    def test_rollback_active_is_valid_taxonomy_value(self):
        from schemas import NO_TRADE_REASONS
        self.assertIn("rollback_active", NO_TRADE_REASONS)


class TestRollbackDefaultsInConfig(unittest.TestCase):
    """strategy_config.json rollback flags must all default false."""

    def test_config_file_has_a2_rollback(self):
        import json
        cfg_path = _BOT_DIR / "strategy_config.json"
        cfg = json.loads(cfg_path.read_text())
        self.assertIn("a2_rollback", cfg, "a2_rollback section must exist in strategy_config.json")

    def test_all_rollback_flags_default_false(self):
        import json
        cfg = json.loads((_BOT_DIR / "strategy_config.json").read_text())
        rb = cfg["a2_rollback"]
        self.assertFalse(rb["force_no_trade"], "force_no_trade must default false")
        self.assertFalse(rb["disable_bounded_debate"], "disable_bounded_debate must default false")
        self.assertFalse(rb["disable_candidate_generation"],
                         "disable_candidate_generation must default false")

    def test_config_file_has_a2_router(self):
        import json
        cfg = json.loads((_BOT_DIR / "strategy_config.json").read_text())
        self.assertIn("a2_router", cfg, "a2_router section must exist in strategy_config.json")
        router = cfg["a2_router"]
        self.assertIn("earnings_dte_blackout", router)
        self.assertIn("min_liquidity_score", router)
        self.assertIn("macro_iv_gate_rank", router)
        self.assertIn("iv_env_blackout", router)


if __name__ == "__main__":
    unittest.main()
