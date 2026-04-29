"""
tests/test_a2_stage_contracts.py — A2 stage contract tests (S3-A).

Covers:
  - Smoke: each stage module imports with no env vars
  - A2CandidateSet construction and roundtrip
  - A2DecisionRecord construction and roundtrip
  - no_trade_reason values match NO_TRADE_REASONS taxonomy
  - _STRATEGY_FROM_STRUCTURE keys and values
  - _parse_bounded_debate_response (stage 3 re-export)
  - _apply_veto_rules and _route_strategy available from bot_options (backward compat)
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

def _make_feature_pack(**overrides):
    from schemas import A2FeaturePack
    defaults = dict(
        symbol="AAPL",
        a1_signal_score=72.0,
        a1_direction="bullish",
        trend_score=None,
        momentum_score=None,
        sector_alignment="technology",
        iv_rank=35.0,
        iv_environment="neutral",
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
# Smoke tests — each stage module independently importable with no env vars
# ════════════════════════════════════════════════════════════════════════════

class TestStageModuleSmokeImports(unittest.TestCase):

    def test_stage0_importable(self):
        import bot_options_stage0_preflight as s0
        self.assertTrue(hasattr(s0, "run_a2_preflight"))
        self.assertTrue(hasattr(s0, "A2PreflightResult"))

    def test_stage1_importable(self):
        import bot_options_stage1_candidates as s1
        self.assertTrue(hasattr(s1, "load_a1_signals"))
        self.assertTrue(hasattr(s1, "run_candidate_stage"))

    def test_stage2_importable(self):
        import bot_options_stage2_structures as s2
        self.assertTrue(hasattr(s2, "_route_strategy"))
        self.assertTrue(hasattr(s2, "_apply_veto_rules"))
        self.assertTrue(hasattr(s2, "build_candidate_structures"))
        self.assertTrue(hasattr(s2, "_STRATEGY_FROM_STRUCTURE"))

    def test_stage3_importable(self):
        import bot_options_stage3_debate as s3
        self.assertTrue(hasattr(s3, "_parse_bounded_debate_response"))
        self.assertTrue(hasattr(s3, "run_bounded_debate"))

    def test_stage4_importable(self):
        import bot_options_stage4_execution as s4
        self.assertTrue(hasattr(s4, "submit_selected_candidate"))
        self.assertTrue(hasattr(s4, "close_check_loop"))
        self.assertTrue(hasattr(s4, "persist_decision_record"))


# ════════════════════════════════════════════════════════════════════════════
# A2CandidateSet construction and roundtrip
# ════════════════════════════════════════════════════════════════════════════

class TestA2CandidateSetConstruction(unittest.TestCase):

    def _make_set(self, **overrides):
        from schemas import A2CandidateSet
        pack = _make_feature_pack()
        defaults = dict(
            symbol="AAPL",
            pack=pack,
            allowed_structures=["debit_call_spread", "long_call"],
            router_rule_fired="RULE5",
            generated_candidates=[{"candidate_id": "C001", "structure_type": "debit_call_spread"}],
            vetoed_candidates=[],
            surviving_candidates=[{"candidate_id": "C001", "structure_type": "debit_call_spread"}],
            generation_errors=[],
            built_at=datetime.now(timezone.utc).isoformat(),
        )
        defaults.update(overrides)
        return A2CandidateSet(**defaults)

    def test_construction_succeeds(self):
        cset = self._make_set()
        self.assertEqual(cset.symbol, "AAPL")
        self.assertEqual(cset.router_rule_fired, "RULE5")
        self.assertEqual(len(cset.surviving_candidates), 1)

    def test_empty_surviving_candidates(self):
        cset = self._make_set(surviving_candidates=[], generated_candidates=[])
        self.assertEqual(len(cset.surviving_candidates), 0)

    def test_vetoed_candidates_tracked(self):
        vetoed = [{"candidate_id": "C002", "reason": "dte=3<5"}]
        cset = self._make_set(vetoed_candidates=vetoed)
        self.assertEqual(cset.vetoed_candidates[0]["reason"], "dte=3<5")

    def test_generation_errors_tracked(self):
        errors = ["chain fetch failed: timeout"]
        cset = self._make_set(generation_errors=errors)
        self.assertIn("timeout", cset.generation_errors[0])

    def test_pack_embedded(self):
        cset = self._make_set()
        self.assertEqual(cset.pack.symbol, "AAPL")
        self.assertAlmostEqual(cset.pack.iv_rank, 35.0)

    def test_json_roundtrip(self):
        from dataclasses import asdict
        cset = self._make_set()
        d = asdict(cset)
        serialized = json.dumps(d, default=str)
        restored = json.loads(serialized)
        self.assertEqual(restored["symbol"], "AAPL")
        self.assertEqual(restored["router_rule_fired"], "RULE5")
        self.assertEqual(restored["pack"]["iv_environment"], "neutral")


# ════════════════════════════════════════════════════════════════════════════
# A2DecisionRecord construction and roundtrip
# ════════════════════════════════════════════════════════════════════════════

class TestA2DecisionRecordConstruction(unittest.TestCase):

    def _make_record(self, **overrides):
        from schemas import A2DecisionRecord
        defaults = dict(
            decision_id="a2_dec_20260420_120000_0001",
            session_tier="market",
            candidate_sets=[],
            debate_input="some prompt",
            debate_output_raw='{"selected_candidate_id": "C001", "confidence": 0.90}',
            debate_parsed={"selected_candidate_id": "C001", "confidence": 0.90, "reject": False},
            selected_candidate={"candidate_id": "C001", "structure_type": "debit_call_spread"},
            execution_result="submitted",
            no_trade_reason=None,
            elapsed_seconds=4.7,
        )
        defaults.update(overrides)
        return A2DecisionRecord(**defaults)

    def test_construction_succeeds(self):
        rec = self._make_record()
        self.assertEqual(rec.decision_id, "a2_dec_20260420_120000_0001")
        self.assertEqual(rec.execution_result, "submitted")
        self.assertIsNone(rec.no_trade_reason)

    def test_schema_version_default(self):
        rec = self._make_record()
        self.assertEqual(rec.schema_version, 1)

    def test_built_at_is_iso_string(self):
        rec = self._make_record()
        # Should parse without error
        datetime.fromisoformat(rec.built_at)

    def test_code_version_is_string_or_none(self):
        rec = self._make_record()
        self.assertIn(type(rec.code_version), (str, type(None)))

    def test_no_trade_record(self):
        rec = self._make_record(
            execution_result="no_trade",
            no_trade_reason="debate_low_confidence",
            selected_candidate=None,
        )
        self.assertEqual(rec.execution_result, "no_trade")
        self.assertEqual(rec.no_trade_reason, "debate_low_confidence")

    def test_json_roundtrip(self):
        from dataclasses import asdict
        rec = self._make_record()
        d = asdict(rec)
        serialized = json.dumps(d, default=str)
        restored = json.loads(serialized)
        self.assertEqual(restored["decision_id"], rec.decision_id)
        self.assertEqual(restored["schema_version"], 1)
        self.assertEqual(restored["execution_result"], "submitted")

    def test_missing_fields_tolerated_on_load(self):
        # Schema versioning rule: old records missing fields must default to None, never raise
        from schemas import A2DecisionRecord
        # Simulate old record with missing optional fields by constructing with defaults
        rec = A2DecisionRecord(
            decision_id="old_rec",
            session_tier="market",
            candidate_sets=[],
            debate_input=None,
            debate_output_raw=None,
            debate_parsed=None,
            selected_candidate=None,
            execution_result=None,
            no_trade_reason=None,
            elapsed_seconds=0.0,
        )
        self.assertIsNone(rec.execution_result)
        self.assertEqual(rec.schema_version, 1)


# ════════════════════════════════════════════════════════════════════════════
# NO_TRADE_REASONS taxonomy
# ════════════════════════════════════════════════════════════════════════════

class TestNoTradeReasonsTaxonomy(unittest.TestCase):

    EXPECTED_REASONS = [
        "no_signal_scores",
        "no_candidates_after_router",
        "no_candidates_after_veto",
        "debate_low_confidence",
        "debate_parse_failed",
        "debate_rejected_all",
        "execution_rejected",
        "execution_error",
        "preflight_halt",
        "session_not_market",
        "obs_mode_active",
    ]

    def test_all_expected_reasons_present(self):
        from schemas import NO_TRADE_REASONS
        for reason in self.EXPECTED_REASONS:
            self.assertIn(reason, NO_TRADE_REASONS,
                          f"'{reason}' missing from NO_TRADE_REASONS")

    def test_no_duplicates(self):
        from schemas import NO_TRADE_REASONS
        self.assertEqual(len(NO_TRADE_REASONS), len(set(NO_TRADE_REASONS)),
                         "NO_TRADE_REASONS contains duplicate entries")

    def test_all_are_strings(self):
        from schemas import NO_TRADE_REASONS
        for r in NO_TRADE_REASONS:
            self.assertIsInstance(r, str, f"Entry {r!r} is not a string")

    def test_no_trade_reason_field_accepts_taxonomy_values(self):
        from schemas import NO_TRADE_REASONS, A2DecisionRecord
        for reason in NO_TRADE_REASONS:
            rec = A2DecisionRecord(
                decision_id="test",
                session_tier="market",
                candidate_sets=[],
                debate_input=None,
                debate_output_raw=None,
                debate_parsed=None,
                selected_candidate=None,
                execution_result="no_trade",
                no_trade_reason=reason,
                elapsed_seconds=0.0,
            )
            self.assertEqual(rec.no_trade_reason, reason)


# ════════════════════════════════════════════════════════════════════════════
# _STRATEGY_FROM_STRUCTURE — eagerly populated, backward compat
# ════════════════════════════════════════════════════════════════════════════

class TestStrategyFromStructureMap(unittest.TestCase):

    EXPECTED_KEYS = [
        "long_call", "long_put",
        "debit_call_spread", "debit_put_spread",
        "credit_call_spread", "credit_put_spread",
    ]

    def test_all_expected_keys_present(self):
        from bot_options import _STRATEGY_FROM_STRUCTURE
        for key in self.EXPECTED_KEYS:
            self.assertIn(key, _STRATEGY_FROM_STRUCTURE, f"'{key}' missing")

    def test_values_are_option_strategy_enums(self):
        from bot_options import _STRATEGY_FROM_STRUCTURE
        from schemas import OptionStrategy
        for key, val in _STRATEGY_FROM_STRUCTURE.items():
            self.assertIsInstance(val, OptionStrategy,
                                  f"_STRATEGY_FROM_STRUCTURE['{key}'] is not OptionStrategy")

    def test_long_call_maps_to_single_call(self):
        from bot_options import _STRATEGY_FROM_STRUCTURE
        from schemas import OptionStrategy
        self.assertEqual(_STRATEGY_FROM_STRUCTURE["long_call"], OptionStrategy.SINGLE_CALL)

    def test_debit_call_spread_maps_correctly(self):
        from bot_options import _STRATEGY_FROM_STRUCTURE
        from schemas import OptionStrategy
        self.assertEqual(_STRATEGY_FROM_STRUCTURE["debit_call_spread"],
                         OptionStrategy.CALL_DEBIT_SPREAD)


# ════════════════════════════════════════════════════════════════════════════
# Backward-compat re-exports — functions accessible from bot_options
# ════════════════════════════════════════════════════════════════════════════

class TestBotOptionsBackwardCompatExports(unittest.TestCase):

    def test_apply_veto_rules_accessible(self):
        from bot_options import _apply_veto_rules
        self.assertTrue(callable(_apply_veto_rules))

    def test_route_strategy_accessible(self):
        from bot_options import _route_strategy
        self.assertTrue(callable(_route_strategy))

    def test_parse_bounded_debate_response_accessible(self):
        from bot_options import _parse_bounded_debate_response
        self.assertTrue(callable(_parse_bounded_debate_response))

    def test_build_a2_feature_pack_accessible(self):
        from bot_options import _build_a2_feature_pack
        self.assertTrue(callable(_build_a2_feature_pack))

    def test_quick_liquidity_check_accessible(self):
        from bot_options_stage2_structures import _quick_liquidity_check
        self.assertTrue(callable(_quick_liquidity_check))

    def test_strategy_from_structure_has_correct_count(self):
        from bot_options import _STRATEGY_FROM_STRUCTURE
        self.assertEqual(len(_STRATEGY_FROM_STRUCTURE), 8)  # straddle + strangle added in Phase 2


# ════════════════════════════════════════════════════════════════════════════
# _parse_bounded_debate_response (via stage 3)
# ════════════════════════════════════════════════════════════════════════════

class TestParseBoundedDebateResponse(unittest.TestCase):

    def _parse(self, raw):
        from bot_options_stage3_debate import _parse_bounded_debate_response
        return _parse_bounded_debate_response(raw)

    def test_empty_returns_reject_all(self):
        result = self._parse("")
        self.assertTrue(result["reject"])
        self.assertIsNone(result["selected_candidate_id"])

    def test_valid_json_parsed(self):
        payload = json.dumps({
            "selected_candidate_id": "C001",
            "confidence": 0.92,
            "reject": False,
            "key_risks": [],
            "reasons": "good setup",
            "recommended_size_modifier": 1.0,
        })
        result = self._parse(payload)
        self.assertEqual(result["selected_candidate_id"], "C001")
        self.assertAlmostEqual(result["confidence"], 0.92)
        self.assertFalse(result["reject"])

    def test_markdown_fences_stripped(self):
        payload = '```json\n{"selected_candidate_id": "C002", "confidence": 0.87, "reject": false, "key_risks": [], "reasons": "ok", "recommended_size_modifier": 1.0}\n```'
        result = self._parse(payload)
        self.assertEqual(result["selected_candidate_id"], "C002")

    def test_reject_true_payload(self):
        payload = json.dumps({
            "selected_candidate_id": None,
            "confidence": 0.5,
            "reject": True,
            "key_risks": ["high IV"],
            "reasons": "thesis weak",
            "recommended_size_modifier": 1.0,
        })
        result = self._parse(payload)
        self.assertTrue(result["reject"])

    def test_json_parse_failure_returns_reject_all(self):
        result = self._parse("not json at all {{")
        self.assertTrue(result["reject"])
        self.assertEqual(result["reasons"], "json_parse_failed")


# ════════════════════════════════════════════════════════════════════════════
# persist_decision_record — writes file and prunes to 500
# ════════════════════════════════════════════════════════════════════════════

class TestPersistDecisionRecord(unittest.TestCase):

    def _make_record(self, decision_id: str = "a2_dec_test_persist_001"):
        # Note: persist_decision_record rewrites any decision_id that doesn't
        # start with "a2_dec_" into the canonical "a2_dec_YYYYMMDD_HHMMSS"
        # form, because every decision file MUST have a canonical ID for the
        # glob / index / dedup logic to work. Tests must supply an ID already
        # in canonical form when they want to verify it is preserved.
        from schemas import A2DecisionRecord
        return A2DecisionRecord(
            decision_id=decision_id,
            session_tier="market",
            candidate_sets=[],
            debate_input=None,
            debate_output_raw=None,
            debate_parsed=None,
            selected_candidate=None,
            execution_result="no_trade",
            no_trade_reason="no_signal_scores",
            elapsed_seconds=1.2,
        )

    def test_persist_writes_json_file(self):
        import tempfile

        import bot_options_stage4_execution as s4
        from bot_options_stage4_execution import persist_decision_record

        rec = self._make_record()
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(s4, "_DECISIONS_DIR", Path(tmpdir)):
                persist_decision_record(rec)
            files = list(Path(tmpdir).glob("a2_dec_*.json"))
            self.assertEqual(len(files), 1)
            data = json.loads(files[0].read_text())
            # Canonical "a2_dec_..." IDs are preserved verbatim.
            self.assertEqual(data["decision_id"], "a2_dec_test_persist_001")
            self.assertEqual(data["execution_result"], "no_trade")
            self.assertEqual(data["schema_version"], 1)

    def test_persist_rewrites_non_canonical_decision_id(self):
        """A decision_id without the 'a2_dec_' prefix must be rewritten to canonical form."""
        import tempfile

        import bot_options_stage4_execution as s4
        from bot_options_stage4_execution import persist_decision_record

        rec = self._make_record(decision_id="legacy_freeform_id")
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(s4, "_DECISIONS_DIR", Path(tmpdir)):
                persist_decision_record(rec)
            files = list(Path(tmpdir).glob("a2_dec_*.json"))
            self.assertEqual(len(files), 1)
            data = json.loads(files[0].read_text())
            self.assertTrue(data["decision_id"].startswith("a2_dec_"),
                            f"non-canonical ID was not rewritten: {data['decision_id']!r}")
            # The rewritten ID is the canonical timestamp form, never the original.
            self.assertNotEqual(data["decision_id"], "legacy_freeform_id")

    def test_persist_prunes_oldest_when_over_500(self):
        import tempfile

        import bot_options_stage4_execution as s4
        from bot_options_stage4_execution import persist_decision_record

        rec = self._make_record()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            # Pre-populate 502 dummy files
            for i in range(502):
                (tmppath / f"a2_dec_20260101_00{i:04d}00.json").write_text(
                    json.dumps({"decision_id": f"old_{i}"})
                )
            with mock.patch.object(s4, "_DECISIONS_DIR", tmppath):
                persist_decision_record(rec)
            files = list(tmppath.glob("a2_dec_*.json"))
        # 502 + 1 new = 503, pruned to 500
        self.assertEqual(len(files), 500)

    def test_persist_nonfatal_on_write_error(self):
        import bot_options_stage4_execution as s4
        from bot_options_stage4_execution import persist_decision_record

        rec = self._make_record()
        # Point to an unwritable path — should not raise
        with mock.patch.object(s4, "_DECISIONS_DIR", Path("/nonexistent/path/that/cannot/exist")):
            try:
                persist_decision_record(rec)
            except Exception as exc:
                self.fail(f"persist_decision_record raised unexpectedly: {exc}")


if __name__ == "__main__":
    unittest.main()
