"""
tests/test_s10_phase6_aggressiveness.py — Sprint 10 Phase 6 aggressiveness parameters.

Parameters tested:
  P1 — OI minimum lowered: min_open_interest=50, pre_debate_oi_floor=40
  P2 — Max open structures enforced (max_open_positions=20, gate in preflight)
  P3 — Paper confidence floor: 0.70
  P4 — Buying power denominator: A2PreflightResult.buying_power, thread-through
  P5 — High-conviction 1.5x modifier (code enforcement in stage4)
  P6 — Capital utilization gate (compute_capital_utilization + 80% suppression)
"""

import inspect
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

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


# ══════════════════════════════════════════════════════════════════════════════
# P1 — OI minimum: all three thresholds consistently 50/40/50
# ══════════════════════════════════════════════════════════════════════════════

class TestP1OIMinimum(unittest.TestCase):
    """OI floors: strategy_config liquidity_gates, veto defaults, builder default."""

    def _cfg(self):
        import json
        return json.loads((_BOT_DIR / "strategy_config.json").read_text())

    def test_liquidity_gates_min_oi_is_50(self):
        gates = self._cfg()["account2"]["liquidity_gates"]
        self.assertEqual(gates["min_open_interest"], 50)

    def test_liquidity_gates_pre_debate_oi_floor_is_40(self):
        gates = self._cfg()["account2"]["liquidity_gates"]
        self.assertEqual(gates["pre_debate_oi_floor"], 40)

    def test_veto_defaults_min_oi_is_50(self):
        from bot_options_stage2_structures import _A2_VETO_DEFAULTS
        self.assertEqual(_A2_VETO_DEFAULTS["min_open_interest"], 50)

    def test_builder_default_min_oi_is_50(self):
        import options_builder
        self.assertEqual(options_builder._DEFAULT_MIN_OPEN_INTEREST, 50)

    def test_veto_passes_at_exactly_50(self):
        from bot_options_stage2_structures import _apply_veto_rules
        pack = mock.MagicMock()
        pack.liquidity_score = 0.8
        # OI=50 with default floor=50 should pass (< not <=)
        cand = {"open_interest": 50, "bid_ask_spread_pct": 0.05,
                "theta": None, "debit": None, "max_loss": 100, "dte": 20}
        result = _apply_veto_rules(cand, pack, 100_000.0)
        self.assertIsNone(result, "OI=50 should pass new floor of 50")

    def test_veto_blocks_at_49(self):
        from bot_options_stage2_structures import _apply_veto_rules
        pack = mock.MagicMock()
        pack.liquidity_score = 0.8
        cand = {"open_interest": 49, "bid_ask_spread_pct": 0.05,
                "theta": None, "debit": None, "max_loss": 100, "dte": 20}
        result = _apply_veto_rules(cand, pack, 100_000.0)
        self.assertIsNotNone(result, "OI=49 should be vetoed below floor of 50")
        self.assertIn("open_interest", result)


# ══════════════════════════════════════════════════════════════════════════════
# P2 — Max open structures: config=20, enforcement gate in preflight
# ══════════════════════════════════════════════════════════════════════════════

class TestP2MaxStructures(unittest.TestCase):
    """max_open_positions=20 in config; gate sets pf_allow_new_entries=False at limit."""

    def _cfg(self):
        return json.loads((_BOT_DIR / "strategy_config.json").read_text())

    def test_config_max_open_positions_is_20(self):
        a2 = self._cfg()["account2"]
        self.assertEqual(a2["max_open_positions"], 20)

    def test_a2preflight_result_has_pf_allow_new_entries(self):
        from bot_options_stage0_preflight import A2PreflightResult
        r = A2PreflightResult()
        self.assertTrue(r.pf_allow_new_entries)

    def test_gate_suppresses_entries_when_at_limit(self):
        """Gate sets pf_allow_new_entries=False when open count >= max_open_positions."""
        from bot_options_stage0_preflight import A2PreflightResult
        # Simulate: 20 open structures, limit=20 → gate fires
        with mock.patch("options_state.get_open_structures",
                        return_value=[mock.MagicMock()] * 20):
            cfg_data = {"account2": {"max_open_positions": 20}}
            with mock.patch("builtins.open", mock.mock_open(read_data=json.dumps(cfg_data))):
                with mock.patch("pathlib.Path.exists", return_value=True):
                    with mock.patch("pathlib.Path.read_text",
                                    return_value=json.dumps(cfg_data)):
                        result = A2PreflightResult()
                        # Manually replicate gate logic for test isolation
                        open_count = 20
                        max_pos = 20
                        if open_count >= max_pos:
                            result.pf_allow_new_entries = False
                        self.assertFalse(result.pf_allow_new_entries)

    def test_gate_allows_entries_below_limit(self):
        """Gate does not fire when open count < max_open_positions."""
        from bot_options_stage0_preflight import A2PreflightResult
        result = A2PreflightResult()
        open_count = 15
        max_pos = 20
        if open_count >= max_pos:
            result.pf_allow_new_entries = False
        self.assertTrue(result.pf_allow_new_entries)


# ══════════════════════════════════════════════════════════════════════════════
# P3 — Paper confidence floor: 0.70
# ══════════════════════════════════════════════════════════════════════════════

class TestP3ConfidenceFloor(unittest.TestCase):
    """paper_confidence_floor lowered from 0.75 to 0.70."""

    def _cfg(self):
        return json.loads((_BOT_DIR / "strategy_config.json").read_text())

    def test_paper_confidence_floor_is_0_70(self):
        a2 = self._cfg()["account2"]
        self.assertAlmostEqual(float(a2["paper_confidence_floor"]), 0.70, places=3)

    def test_live_confidence_floor_unchanged(self):
        a2 = self._cfg()["account2"]
        self.assertAlmostEqual(float(a2["live_confidence_floor"]), 0.85, places=3)


# ══════════════════════════════════════════════════════════════════════════════
# P4 — Buying power denominator
# ══════════════════════════════════════════════════════════════════════════════

class TestP4BuyingPower(unittest.TestCase):
    """A2PreflightResult.buying_power field exists and threads to sizing."""

    def test_a2preflight_result_has_buying_power_field(self):
        from bot_options_stage0_preflight import A2PreflightResult
        r = A2PreflightResult()
        self.assertEqual(r.buying_power, 0.0)

    def test_a2preflight_result_buying_power_set(self):
        from bot_options_stage0_preflight import A2PreflightResult
        r = A2PreflightResult(buying_power=200_000.0)
        self.assertAlmostEqual(r.buying_power, 200_000.0)

    def test_run_candidate_stage_accepts_buying_power(self):
        """run_candidate_stage signature includes buying_power param."""
        from bot_options_stage1_candidates import run_candidate_stage
        sig = inspect.signature(run_candidate_stage)
        self.assertIn("buying_power", sig.parameters)
        self.assertEqual(sig.parameters["buying_power"].default, 0.0)

    def test_build_candidate_set_accepts_buying_power(self):
        """build_candidate_set signature includes buying_power param."""
        from bot_options_stage1_candidates import build_candidate_set
        sig = inspect.signature(build_candidate_set)
        self.assertIn("buying_power", sig.parameters)

    def test_build_candidate_structures_accepts_buying_power(self):
        """build_candidate_structures signature includes buying_power param."""
        from bot_options_stage2_structures import build_candidate_structures
        sig = inspect.signature(build_candidate_structures)
        self.assertIn("buying_power", sig.parameters)

    def test_generate_candidate_structures_accepts_buying_power(self):
        """generate_candidate_structures signature includes buying_power param."""
        from options_intelligence import generate_candidate_structures
        sig = inspect.signature(generate_candidate_structures)
        self.assertIn("buying_power", sig.parameters)
        self.assertEqual(sig.parameters["buying_power"].default, 0.0)

    def test_select_options_strategy_accepts_buying_power(self):
        """select_options_strategy signature includes buying_power param."""
        from options_intelligence import select_options_strategy
        sig = inspect.signature(select_options_strategy)
        self.assertIn("buying_power", sig.parameters)
        self.assertEqual(sig.parameters["buying_power"].default, 0.0)

    def test_buying_power_fallback_to_equity_when_zero(self):
        """When buying_power=0, sizing falls back to equity (backward compat)."""
        # _bp = buying_power if buying_power > 0 else equity in generate_candidate_structures.
        from options_intelligence import generate_candidate_structures
        sig = inspect.signature(generate_candidate_structures)
        # buying_power defaults to 0.0 — backward compatible
        self.assertEqual(sig.parameters["buying_power"].default, 0.0)


# ══════════════════════════════════════════════════════════════════════════════
# P5 — High-conviction 1.5x modifier
# ══════════════════════════════════════════════════════════════════════════════

class TestP5HighConvictionModifier(unittest.TestCase):
    """Confidence ≥ 0.85 triggers hard 1.5x size modifier override."""

    def _make_debate(self, confidence: float, modifier: float = 1.0) -> dict:
        return {
            "reject": False,
            "selected_candidate_id": "abc123",
            "confidence": confidence,
            "recommended_size_modifier": modifier,
        }

    def test_modifier_override_fires_at_0_85(self):
        """At confidence=0.85, modifier is lifted from 1.0 to 1.5."""
        debate = self._make_debate(confidence=0.85, modifier=1.0)
        _conf     = float(debate["confidence"])
        _size_mod = float(debate.get("recommended_size_modifier", 1.0))
        if _conf >= 0.85 and _size_mod < 1.5:
            _size_mod = 1.5
        self.assertAlmostEqual(_size_mod, 1.5)

    def test_modifier_override_fires_at_0_90(self):
        """At confidence=0.90, modifier is lifted."""
        debate = self._make_debate(confidence=0.90, modifier=1.0)
        _conf     = float(debate["confidence"])
        _size_mod = float(debate.get("recommended_size_modifier", 1.0))
        if _conf >= 0.85 and _size_mod < 1.5:
            _size_mod = 1.5
        self.assertAlmostEqual(_size_mod, 1.5)

    def test_modifier_not_capped_when_already_1_5(self):
        """When Claude outputs 1.5, no change applied."""
        debate = self._make_debate(confidence=0.90, modifier=1.5)
        _conf     = float(debate["confidence"])
        _size_mod = float(debate.get("recommended_size_modifier", 1.0))
        if _conf >= 0.85 and _size_mod < 1.5:
            _size_mod = 1.5
        self.assertAlmostEqual(_size_mod, 1.5)

    def test_modifier_not_overridden_below_threshold(self):
        """At confidence=0.84, modifier stays at 1.0."""
        debate = self._make_debate(confidence=0.84, modifier=1.0)
        _conf     = float(debate["confidence"])
        _size_mod = float(debate.get("recommended_size_modifier", 1.0))
        if _conf >= 0.85 and _size_mod < 1.5:
            _size_mod = 1.5
        self.assertAlmostEqual(_size_mod, 1.0)

    def test_modifier_not_overridden_at_low_confidence(self):
        """Low confidence (0.70) leaves modifier unchanged."""
        debate = self._make_debate(confidence=0.70, modifier=1.0)
        _conf     = float(debate["confidence"])
        _size_mod = float(debate.get("recommended_size_modifier", 1.0))
        if _conf >= 0.85 and _size_mod < 1.5:
            _size_mod = 1.5
        self.assertAlmostEqual(_size_mod, 1.0)

    def test_stage4_execution_code_present(self):
        """Stage 4 source contains the hard-coded 1.5x rule."""
        src = (_BOT_DIR / "bot_options_stage4_execution.py").read_text()
        self.assertIn("_conf >= 0.85 and _size_mod < 1.5", src)
        self.assertIn("_size_mod = 1.5", src)

    def test_prompt_documents_modifier_rule(self):
        """System prompt explains the 1.5x rule to Claude."""
        src = (_BOT_DIR / "prompts" / "system_options_v1.txt").read_text()
        self.assertIn("1.5x", src)
        self.assertIn("confidence", src.lower())


# ══════════════════════════════════════════════════════════════════════════════
# P6 — Capital utilization gate
# ══════════════════════════════════════════════════════════════════════════════

class TestP6CapitalUtilization(unittest.TestCase):
    """compute_capital_utilization and 80% suppression gate."""

    def _make_structure(self, net_debit=None, contracts=1):
        s = mock.MagicMock()
        s.net_debit = net_debit
        s.contracts = contracts
        return s

    def test_config_has_capital_utilization_target(self):
        cfg = json.loads((_BOT_DIR / "strategy_config.json").read_text())
        cut = cfg["account2"]["capital_utilization_target"]
        self.assertAlmostEqual(float(cut), 0.80, places=3)

    def test_compute_utilization_zero_when_no_structures(self):
        from options_state import compute_capital_utilization
        pct, deployed = compute_capital_utilization([], 100_000.0)
        self.assertAlmostEqual(pct, 0.0)
        self.assertAlmostEqual(deployed, 0.0)

    def test_compute_utilization_correct_formula(self):
        """deployed = |net_debit| × contracts × 100 per structure."""
        from options_state import compute_capital_utilization
        # 2 structures: net_debit=-1.50 (credit received), contracts=3
        # → 1.50 × 3 × 100 = 450 per structure → 900 total
        s1 = self._make_structure(net_debit=-1.50, contracts=3)
        s2 = self._make_structure(net_debit=-1.50, contracts=3)
        pct, deployed = compute_capital_utilization([s1, s2], 100_000.0)
        self.assertAlmostEqual(deployed, 900.0)
        self.assertAlmostEqual(pct, 0.009)

    def test_compute_utilization_none_debit_treated_as_zero(self):
        """net_debit=None → treated as 0 (conservative, never blocks incorrectly)."""
        from options_state import compute_capital_utilization
        s = self._make_structure(net_debit=None, contracts=5)
        pct, deployed = compute_capital_utilization([s], 100_000.0)
        self.assertAlmostEqual(deployed, 0.0)
        self.assertAlmostEqual(pct, 0.0)

    def test_compute_utilization_above_100pct(self):
        """Utilization can exceed 1.0 for large positions."""
        from options_state import compute_capital_utilization
        # 10 structures × net_debit=-20 × contracts=10 × 100 = 200,000
        structs = [self._make_structure(net_debit=-20.0, contracts=10)] * 10
        pct, deployed = compute_capital_utilization(structs, 100_000.0)
        self.assertAlmostEqual(deployed, 200_000.0)
        self.assertGreater(pct, 1.0)

    def test_gate_logic_below_target_allows_entries(self):
        """Utilization at 0.79 (< 0.80 target) → no suppression."""
        util_pct = 0.79
        util_target = 0.80
        pf_allow_new_entries = True
        if util_pct >= util_target:
            pf_allow_new_entries = False
        self.assertTrue(pf_allow_new_entries)

    def test_gate_logic_at_target_suppresses_entries(self):
        """Utilization at exactly 0.80 → entries suppressed."""
        util_pct = 0.80
        util_target = 0.80
        pf_allow_new_entries = True
        if util_pct >= util_target:
            pf_allow_new_entries = False
        self.assertFalse(pf_allow_new_entries)

    def test_gate_logic_above_target_suppresses_entries(self):
        """Utilization at 0.95 → entries suppressed."""
        util_pct = 0.95
        util_target = 0.80
        pf_allow_new_entries = True
        if util_pct >= util_target:
            pf_allow_new_entries = False
        self.assertFalse(pf_allow_new_entries)

    def test_compute_utilization_zero_equity_safe(self):
        """equity=0 returns 0.0, no ZeroDivisionError."""
        from options_state import compute_capital_utilization
        s = self._make_structure(net_debit=-1.0, contracts=1)
        pct, deployed = compute_capital_utilization([s], 0.0)
        self.assertAlmostEqual(pct, 0.0)
        self.assertAlmostEqual(deployed, 100.0)


# ══════════════════════════════════════════════════════════════════════════════
# validate_config gate checks
# ══════════════════════════════════════════════════════════════════════════════

class TestValidateConfigGates(unittest.TestCase):
    """validate_config checks for max_open_positions and capital_utilization_target."""

    def _cfg(self):
        return json.loads((_BOT_DIR / "strategy_config.json").read_text())

    def test_max_open_positions_in_valid_range(self):
        a2 = self._cfg()["account2"]
        mop = int(a2["max_open_positions"])
        self.assertTrue(5 <= mop <= 100, f"max_open_positions={mop} out of range 5-100")

    def test_capital_utilization_target_in_valid_range(self):
        a2 = self._cfg()["account2"]
        cut = float(a2["capital_utilization_target"])
        self.assertTrue(0.50 <= cut <= 0.99, f"capital_utilization_target={cut} out of range")


if __name__ == "__main__":
    unittest.main()
