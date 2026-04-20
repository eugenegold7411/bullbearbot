"""
tests/test_a2_integration.py — A2 decision pipeline integration tests (S3-D).

All tests run in CI (no ChromaDB, no live API calls, no Alpaca credentials).

Suites:
  TestGoldenFixtureIntegrity     — fixture files are well-formed and internally consistent
  TestDeterministicRoutingStage  — re-run Stage 2 routing/veto against golden fixture packs
  TestReplayHarness              — replay_decision() on each golden case produces match=True
  TestNoTradePathLogic           — deterministic no-trade outcomes (Stage 2 logic)
  TestHappyPathAuditTrail        — golden_005 trade fixture has complete audit trail
  TestPersistenceNoCycle         — persist_decision_record writes for every cycle
"""

import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

# ── Project root on path ──────────────────────────────────────────────────────

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ── Minimal stubs (installed before any project imports) ─────────────────────

def _stub(name: str, **attrs) -> types.ModuleType:
    if name not in sys.modules:
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m
    return sys.modules[name]

_stub("dotenv", load_dotenv=lambda *a, **kw: None)
_stub("log_setup", get_logger=lambda n: __import__("logging").getLogger(n))
_stub("anthropic")
for _ap in ("alpaca", "alpaca.trading", "alpaca.trading.client",
            "alpaca.trading.requests", "alpaca.trading.enums"):
    if _ap not in sys.modules:
        sys.modules[_ap] = mock.MagicMock()

# ── Golden fixture helpers ────────────────────────────────────────────────────

_GOLDEN_DIR = Path(__file__).parent / "a2_golden_cases"

_FIXTURE_NAMES = [
    "no_trade_no_signal_scores",
    "no_trade_earnings_blackout",
    "no_trade_all_vetoed_spread",
    "no_trade_debate_low_confidence",
    "trade_debit_call_spread",
    "no_trade_candidate_gen_failed",
]


def _load_fixture(name: str) -> dict:
    path = _GOLDEN_DIR / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _all_fixtures() -> list[tuple[str, dict]]:
    return [(name, _load_fixture(name)) for name in _FIXTURE_NAMES]


# ════════════════════════════════════════════════════════════════════════════
# Suite 1 — Golden fixture integrity
# ════════════════════════════════════════════════════════════════════════════

class TestGoldenFixtureIntegrity(unittest.TestCase):
    """Verify every fixture file is well-formed and internally consistent."""

    _REQUIRED_FIELDS = {
        "decision_id", "session_tier", "candidate_sets",
        "debate_input", "debate_output_raw", "debate_parsed",
        "selected_candidate", "execution_result", "no_trade_reason",
        "elapsed_seconds", "schema_version", "built_at",
    }

    def test_all_fixtures_exist(self):
        for name in _FIXTURE_NAMES:
            with self.subTest(name=name):
                self.assertTrue((_GOLDEN_DIR / f"{name}.json").exists(),
                                f"{name}.json not found in {_GOLDEN_DIR}")

    def test_all_fixtures_have_required_fields(self):
        for name, fixture in _all_fixtures():
            with self.subTest(name=name):
                missing = self._REQUIRED_FIELDS - set(fixture.keys())
                self.assertEqual(missing, set(),
                                 f"{name} missing fields: {missing}")

    def test_schema_version_is_one(self):
        for name, fixture in _all_fixtures():
            with self.subTest(name=name):
                self.assertEqual(fixture["schema_version"], 1)

    def test_no_trade_fixtures_have_no_selected_candidate(self):
        no_trade_names = [n for n in _FIXTURE_NAMES if n.startswith("no_trade")]
        for name in no_trade_names:
            fixture = _load_fixture(name)
            with self.subTest(name=name):
                self.assertIsNone(fixture["selected_candidate"])
                self.assertEqual(fixture["execution_result"], "no_trade")

    def test_trade_fixture_has_selected_candidate(self):
        fixture = _load_fixture("trade_debit_call_spread")
        self.assertIsNotNone(fixture["selected_candidate"])
        self.assertEqual(fixture["execution_result"], "submitted")

    def test_earnings_blackout_pack_has_earnings_days_away(self):
        fixture = _load_fixture("no_trade_earnings_blackout")
        cs = fixture["candidate_sets"][0]
        self.assertEqual(cs["pack"]["earnings_days_away"], 3)
        self.assertEqual(cs["router_rule_fired"], "RULE1")
        self.assertEqual(cs["allowed_structures"], [])

    def test_all_vetoed_fixture_all_vetoed_for_spread(self):
        fixture = _load_fixture("no_trade_all_vetoed_spread")
        cs = fixture["candidate_sets"][0]
        self.assertEqual(cs["surviving_candidates"], [])
        self.assertEqual(len(cs["vetoed_candidates"]), 2)
        for v in cs["vetoed_candidates"]:
            self.assertIn("bid_ask_spread_pct", v["reason"])

    def test_candidate_gen_failed_has_generation_errors(self):
        fixture = _load_fixture("no_trade_candidate_gen_failed")
        cs = fixture["candidate_sets"][0]
        self.assertGreater(len(cs["generation_errors"]), 0)
        self.assertEqual(cs["generated_candidates"], [])

    def test_debate_low_confidence_fixture_confidence_below_threshold(self):
        fixture = _load_fixture("no_trade_debate_low_confidence")
        self.assertIsNotNone(fixture["debate_parsed"])
        self.assertLess(fixture["debate_parsed"]["confidence"], 0.85)
        self.assertEqual(fixture["no_trade_reason"], "debate_low_confidence")

    def test_trade_fixture_confidence_above_threshold(self):
        fixture = _load_fixture("trade_debit_call_spread")
        self.assertGreaterEqual(fixture["debate_parsed"]["confidence"], 0.85)
        self.assertFalse(fixture["debate_parsed"]["reject"])


# ════════════════════════════════════════════════════════════════════════════
# Suite 2 — Deterministic routing stage
# ════════════════════════════════════════════════════════════════════════════

class TestDeterministicRoutingStage(unittest.TestCase):
    """
    Re-run Stage 2 routing + veto against golden fixture packs.
    These tests exercise real pipeline code with no live API calls.
    """

    def _pack_from_fixture(self, fixture_name: str, cs_index: int = 0):
        """Reconstruct A2FeaturePack from a fixture's candidate_set.pack dict."""
        from scripts.replay_a2_decision import _reconstruct_pack  # noqa: PLC0415
        fixture = _load_fixture(fixture_name)
        pack_dict = fixture["candidate_sets"][cs_index]["pack"]
        return _reconstruct_pack(pack_dict)

    def test_earnings_blackout_pack_routes_to_empty(self):
        from bot_options_stage2_structures import _route_strategy
        pack = self._pack_from_fixture("no_trade_earnings_blackout")
        self.assertIsNotNone(pack)
        result = _route_strategy(pack)
        self.assertEqual(result, [], "RULE1 should block earnings-near symbols")

    def test_cheap_iv_bullish_routes_to_long_and_debit(self):
        """NVDA cheap IV + bullish fires RULE5: allows long + debit structures."""
        from bot_options_stage2_structures import _route_strategy
        pack = self._pack_from_fixture("trade_debit_call_spread")
        self.assertIsNotNone(pack)
        result = _route_strategy(pack)
        self.assertIn("long_call", result)
        self.assertIn("debit_call_spread", result)

    def test_neutral_iv_bullish_routes_to_debit_only(self):
        """QQQ neutral IV + bullish fires RULE6: debit spreads only, no long premium."""
        from bot_options_stage2_structures import _route_strategy
        pack = self._pack_from_fixture("no_trade_debate_low_confidence")
        self.assertIsNotNone(pack)
        result = _route_strategy(pack)
        self.assertIn("debit_call_spread", result)
        self.assertNotIn("long_call", result, "RULE6 should not allow naked long premium")

    def test_all_vetoed_spread_candidates_fail_veto(self):
        """All SPY candidates in fixture 3 should fail bid_ask spread veto."""
        from bot_options_stage2_structures import _apply_veto_rules
        from scripts.replay_a2_decision import _reconstruct_pack
        fixture = _load_fixture("no_trade_all_vetoed_spread")
        cs = fixture["candidate_sets"][0]
        pack = _reconstruct_pack(cs["pack"])
        equity = pack.premium_budget_usd / 0.05

        for candidate in cs["generated_candidates"]:
            with self.subTest(candidate_id=candidate["candidate_id"]):
                reason = _apply_veto_rules(candidate, pack, equity)
                self.assertIsNotNone(reason, "candidate should be vetoed")
                self.assertIn("bid_ask_spread_pct", reason)

    def test_clean_trade_candidate_passes_veto(self):
        """NVDA golden candidate should pass all veto rules."""
        from bot_options_stage2_structures import _apply_veto_rules
        from scripts.replay_a2_decision import _reconstruct_pack
        fixture = _load_fixture("trade_debit_call_spread")
        cs = fixture["candidate_sets"][0]
        pack = _reconstruct_pack(cs["pack"])
        equity = pack.premium_budget_usd / 0.05

        for candidate in cs["generated_candidates"]:
            with self.subTest(candidate_id=candidate["candidate_id"]):
                reason = _apply_veto_rules(candidate, pack, equity)
                self.assertIsNone(reason, f"candidate should NOT be vetoed, got: {reason}")

    def test_cheap_iv_amd_routes_to_rule5(self):
        """AMD cheap IV + bullish fires RULE5."""
        from bot_options_stage2_structures import (
            _infer_router_rule_fired,
            _route_strategy,
        )
        pack = self._pack_from_fixture("no_trade_candidate_gen_failed")
        self.assertIsNotNone(pack)
        result   = _route_strategy(pack)
        rule     = _infer_router_rule_fired(pack, result)
        self.assertIn("long_call", result)
        self.assertEqual(rule, "RULE5")


# ════════════════════════════════════════════════════════════════════════════
# Suite 3 — Replay harness
# ════════════════════════════════════════════════════════════════════════════

class TestReplayHarness(unittest.TestCase):
    """
    Run replay_decision() on each golden fixture.
    Uses mock to inject the fixture dict instead of reading from disk.
    All cases should produce match=True (routing/veto is deterministic).
    """

    def _replay_fixture(self, fixture_name: str) -> dict:
        import a2_decision_store
        from scripts.replay_a2_decision import replay_decision

        fixture = _load_fixture(fixture_name)
        decision_id = fixture["decision_id"]

        with mock.patch.object(a2_decision_store, "get_decision_by_id",
                               return_value=fixture):
            return replay_decision(decision_id=decision_id, offline=True)

    def test_no_signal_scores_replay_match(self):
        result = self._replay_fixture("no_trade_no_signal_scores")
        self.assertTrue(result["match"])
        self.assertEqual(result["diff"], {})

    def test_earnings_blackout_replay_match(self):
        result = self._replay_fixture("no_trade_earnings_blackout")
        self.assertTrue(result["match"])
        self.assertEqual(result["diff"], {})

    def test_all_vetoed_spread_replay_match(self):
        result = self._replay_fixture("no_trade_all_vetoed_spread")
        self.assertTrue(result["match"])
        self.assertEqual(result["diff"], {})

    def test_debate_low_confidence_replay_match(self):
        result = self._replay_fixture("no_trade_debate_low_confidence")
        self.assertTrue(result["match"])
        self.assertEqual(result["diff"], {})

    def test_trade_debit_call_spread_replay_match(self):
        result = self._replay_fixture("trade_debit_call_spread")
        self.assertTrue(result["match"])
        self.assertEqual(result["diff"], {})

    def test_candidate_gen_failed_replay_match(self):
        result = self._replay_fixture("no_trade_candidate_gen_failed")
        self.assertTrue(result["match"])
        self.assertEqual(result["diff"], {})

    def test_replay_not_found_returns_match_false(self):
        import a2_decision_store
        from scripts.replay_a2_decision import replay_decision

        with mock.patch.object(a2_decision_store, "get_decision_by_id",
                               return_value=None):
            result = replay_decision(decision_id="does_not_exist", offline=True)

        self.assertFalse(result["match"])
        self.assertIsNone(result["original"])
        self.assertIn("error", result["diff"])

    def test_replay_preserves_original_no_trade_reason(self):
        """Replayed record keeps original no_trade_reason unchanged."""
        result = self._replay_fixture("no_trade_earnings_blackout")
        self.assertEqual(
            result["original"]["no_trade_reason"],
            result["replayed"]["no_trade_reason"],
        )

    def test_replay_adds_replay_note(self):
        result = self._replay_fixture("trade_debit_call_spread")
        self.assertIn("_replay_note", result["replayed"])
        self.assertIn("_replayed_at", result["replayed"])


# ════════════════════════════════════════════════════════════════════════════
# Suite 4 — No-trade path logic
# ════════════════════════════════════════════════════════════════════════════

class TestNoTradePathLogic(unittest.TestCase):
    """
    Deterministic no-trade detection using fixtures as test data.
    Tests the routing/veto stage behaviour that produces no-trade outcomes.
    """

    def test_no_candidate_sets_means_no_routing(self):
        """Fixture 1: empty candidate_sets → nothing to route."""
        fixture = _load_fixture("no_trade_no_signal_scores")
        self.assertEqual(fixture["candidate_sets"], [])
        self.assertEqual(fixture["no_trade_reason"], "no_signal_scores")

    def test_earnings_blackout_produces_empty_allowed_structures(self):
        """Fixture 2: RULE1 produces empty allowed_structures → no candidates."""
        fixture = _load_fixture("no_trade_earnings_blackout")
        cs = fixture["candidate_sets"][0]
        self.assertEqual(cs["allowed_structures"], [])
        self.assertEqual(cs["surviving_candidates"], [])

    def test_all_vetoed_means_no_surviving(self):
        """Fixture 3: all vetoed → surviving_candidates is empty."""
        fixture = _load_fixture("no_trade_all_vetoed_spread")
        cs = fixture["candidate_sets"][0]
        self.assertEqual(cs["surviving_candidates"], [])
        self.assertGreater(len(cs["vetoed_candidates"]), 0)

    def test_debate_ran_but_low_confidence_no_trade(self):
        """Fixture 4: debate has confidence < 0.85 → no_trade_reason set."""
        fixture = _load_fixture("no_trade_debate_low_confidence")
        self.assertIsNotNone(fixture["debate_parsed"])
        self.assertIsNotNone(fixture["debate_output_raw"])
        self.assertEqual(fixture["no_trade_reason"], "debate_low_confidence")

    def test_chain_failure_produces_generation_errors(self):
        """Fixture 6: chain fetch failure leaves generation_errors populated."""
        fixture = _load_fixture("no_trade_candidate_gen_failed")
        cs = fixture["candidate_sets"][0]
        self.assertEqual(cs["generated_candidates"], [])
        self.assertGreater(len(cs["generation_errors"]), 0)
        self.assertIn("ConnectionError", cs["generation_errors"][0])

    def test_run_candidate_stage_empty_signals_returns_empty(self):
        """run_candidate_stage with {} signal_scores produces no candidate_sets."""
        import bot_options_stage1_candidates as s1

        # Stub all external imports used inside run_candidate_stage
        _od_stub_m = mock.MagicMock()
        _od_stub_m.get_options_regime.return_value = "normal"
        _od_stub_m.fetch_options_chain.return_value = {}
        _oi_stub_m = mock.MagicMock()
        _oi_stub_m.select_options_strategy.return_value = None

        with mock.patch.dict(sys.modules, {
            "options_data": _od_stub_m,
            "options_intelligence": _oi_stub_m,
            "options_universe_manager": mock.MagicMock(),
        }):
            cs_list, proposals, allowed_by_sym, structs = s1.run_candidate_stage(
                signal_scores={},
                iv_summaries={},
                equity=100_000.0,
                vix=18.0,
                equity_symbols=["SPY", "NVDA"],
                config={},
            )

        self.assertEqual(cs_list, [])
        self.assertEqual(proposals, [])
        self.assertEqual(structs, [])


# ════════════════════════════════════════════════════════════════════════════
# Suite 5 — Happy path audit trail
# ════════════════════════════════════════════════════════════════════════════

class TestHappyPathAuditTrail(unittest.TestCase):
    """Verify trade fixture (golden_005) has a complete audit trail."""

    def setUp(self):
        self.fixture = _load_fixture("trade_debit_call_spread")

    def test_has_debate_input(self):
        self.assertIsNotNone(self.fixture["debate_input"])
        self.assertGreater(len(self.fixture["debate_input"]), 10)

    def test_has_raw_debate_output(self):
        self.assertIsNotNone(self.fixture["debate_output_raw"])
        self.assertIn("NVDA_dcs_001", self.fixture["debate_output_raw"])

    def test_has_parsed_debate(self):
        parsed = self.fixture["debate_parsed"]
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["selected_candidate_id"], "NVDA_dcs_001")
        self.assertFalse(parsed["reject"])
        self.assertGreaterEqual(parsed["confidence"], 0.85)

    def test_selected_candidate_matches_debate_selection(self):
        sel = self.fixture["selected_candidate"]
        parsed = self.fixture["debate_parsed"]
        self.assertIsNotNone(sel)
        self.assertEqual(sel["candidate_id"], parsed["selected_candidate_id"])

    def test_selected_candidate_has_geometry(self):
        sel = self.fixture["selected_candidate"]
        for field in ("debit", "max_loss", "max_gain", "breakeven", "delta", "dte"):
            with self.subTest(field=field):
                self.assertIn(field, sel)
                self.assertIsNotNone(sel[field])

    def test_pack_embedded_in_candidate_set(self):
        cs = self.fixture["candidate_sets"][0]
        pack = cs["pack"]
        self.assertEqual(pack["symbol"], "NVDA")
        self.assertEqual(pack["iv_environment"], "cheap")
        self.assertGreater(pack["liquidity_score"], 0.5)

    def test_surviving_candidate_passes_all_veto_rules(self):
        """Cross-check: surviving candidate from fixture 5 passes _apply_veto_rules."""
        from bot_options_stage2_structures import _apply_veto_rules
        from scripts.replay_a2_decision import _reconstruct_pack
        cs = self.fixture["candidate_sets"][0]
        pack = _reconstruct_pack(cs["pack"])
        equity = pack.premium_budget_usd / 0.05
        for c in cs["surviving_candidates"]:
            reason = _apply_veto_rules(c, pack, equity)
            self.assertIsNone(reason, f"should not be vetoed, got: {reason}")


# ════════════════════════════════════════════════════════════════════════════
# Suite 6 — Persistence for every cycle
# ════════════════════════════════════════════════════════════════════════════

class TestPersistenceNoCycle(unittest.TestCase):
    """persist_decision_record writes a file even for no-trade cycles."""

    def _make_no_trade_record(self, reason: str = "no_signal_scores"):
        from schemas import A2DecisionRecord
        return A2DecisionRecord(
            decision_id=f"test_persist_{reason}",
            session_tier="market",
            candidate_sets=[],
            debate_input=None,
            debate_output_raw=None,
            debate_parsed=None,
            selected_candidate=None,
            execution_result="no_trade",
            no_trade_reason=reason,
            elapsed_seconds=0.4,
        )

    def test_persist_writes_file_for_no_trade_cycle(self):
        import tempfile

        import bot_options_stage4_execution as s4

        rec = self._make_no_trade_record("no_signal_scores")
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(s4, "_DECISIONS_DIR", Path(tmpdir)):
                s4.persist_decision_record(rec)
            files = list(Path(tmpdir).glob("a2_dec_*.json"))
            self.assertEqual(len(files), 1)
            data = json.loads(files[0].read_text())
        self.assertEqual(data["no_trade_reason"], "no_signal_scores")
        self.assertEqual(data["execution_result"], "no_trade")

    def test_persist_covers_all_no_trade_reasons(self):
        """A record can be persisted for each taxonomy reason without error."""
        import tempfile

        import bot_options_stage4_execution as s4
        from schemas import NO_TRADE_REASONS

        for reason in NO_TRADE_REASONS:
            with self.subTest(reason=reason):
                rec = self._make_no_trade_record(reason)
                with tempfile.TemporaryDirectory() as tmpdir:
                    with mock.patch.object(s4, "_DECISIONS_DIR", Path(tmpdir)):
                        # Must not raise
                        s4.persist_decision_record(rec)
                    files = list(Path(tmpdir).glob("a2_dec_*.json"))
                    self.assertEqual(len(files), 1)


if __name__ == "__main__":
    unittest.main()
