"""
test_weekly_review_agent6.py — T-009 Agent 6 JSON extraction and validation tests.

Suite: JsonExtraction     — _extract_json_block multi-strategy extraction
Suite: Agent6Validation   — _extract_and_validate_agent6_json type coercion + rejection
Suite: WhitelistGate      — non-whitelisted keys are never merged into config
"""

import json
import sys
import types
import unittest

# ── stubs needed before importing weekly_review ──────────────────────────────

def _stub_module(name: str, **attrs) -> None:
    """Insert a bare module stub into sys.modules only if not already present."""
    if name in sys.modules:
        return
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m


def _ensure_anthropic_stub() -> None:
    """Ensure anthropic and anthropic.Anthropic exist (stub if not installed)."""
    if "anthropic" not in sys.modules:
        m = types.ModuleType("anthropic")
        sys.modules["anthropic"] = m
    ant = sys.modules["anthropic"]
    if not hasattr(ant, "Anthropic"):
        ant.Anthropic = type("Anthropic", (), {"__init__": lambda self, *a, **kw: None})


_ensure_anthropic_stub()
_stub_module("dotenv", load_dotenv=lambda *a, **kw: None)
_stub_module(
    "trade_memory",
    get_collection_stats=lambda: {},
    save_trade_memory=lambda *a, **kw: "",
    retrieve_similar_scenarios=lambda *a, **kw: [],
    update_trade_outcome=lambda *a, **kw: None,
)
_stub_module("report", generate_report=lambda: {})
# Do NOT stub "memory" here — the real memory.py must remain importable for
# test_memory_fixes.py and other tests that run in the same pytest session.
# weekly_review.py imports memory at module level but the functions under test
# (_extract_json_block, _extract_and_validate_agent6_json) do not use it.
_stub_module("scheduler")

# Load weekly_review as an isolated module — stop at first exception but keep
# whatever functions were defined before the error.
import importlib.util
import os as _os

_wr_path = _os.path.join(_os.path.dirname(__file__), "..", "weekly_review.py")
_spec    = importlib.util.spec_from_file_location("weekly_review_test", _wr_path)
_wr_mod  = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(_wr_mod)
except Exception:
    pass  # module-level code may fail on missing env; functions we need are already defined

_extract_json_block             = getattr(_wr_mod, "_extract_json_block", None)
_extract_and_validate_agent6_json = getattr(_wr_mod, "_extract_and_validate_agent6_json", None)
_NUMERIC_PARAM_FIELDS           = getattr(_wr_mod, "_NUMERIC_PARAM_FIELDS", frozenset())

FUNCTIONS_AVAILABLE = _extract_json_block is not None and _extract_and_validate_agent6_json is not None


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_valid_agent6_response(memo: str = "## STRATEGY DIRECTOR WEEKLY MEMO\n\nSome analysis.") -> str:
    """Build a realistic Agent 6 response with a fenced JSON block."""
    payload = {
        "active_strategy": "hybrid",
        "parameter_adjustments": {
            "stop_loss_pct_core": 0.035,
            "take_profit_multiple": 2.5,
            "max_positions": 15,
        },
        "watchlist_updates": {},
        "signal_weights_recommended": {
            "congressional": "medium",
            "macro_wire": "high",
        },
        "director_notes": {
            "active_context": "Focus on commodities this week.",
            "expiry": "2026-04-25",
            "priority": "normal",
        },
        "recommendations": [
            {"text": "Reduce stop_loss_pct_core to 0.03", "target_metric": "win_rate", "priority": "high"}
        ],
    }
    return f"{memo}\n\n```json\n{json.dumps(payload, indent=2)}\n```\n"


# ── Suite: JSON extraction ────────────────────────────────────────────────────

@unittest.skipUnless(FUNCTIONS_AVAILABLE, "weekly_review functions not importable")
class TestJsonExtraction(unittest.TestCase):

    def test_extracts_fenced_json_block(self):
        text = 'Some prose.\n\n```json\n{"key": "value", "n": 42}\n```\n'
        result = _extract_json_block(text)
        self.assertIsNotNone(result)
        self.assertEqual(result["key"], "value")
        self.assertEqual(result["n"], 42)

    def test_extracts_raw_json_no_fences(self):
        text = 'Preamble text.\n{"active_strategy": "hybrid", "count": 3}'
        result = _extract_json_block(text)
        self.assertIsNotNone(result)
        self.assertEqual(result["active_strategy"], "hybrid")

    def test_extracts_json_when_prose_has_stray_braces(self):
        """Prose { ... } before the real JSON must not block extraction."""
        text = (
            "The bot configures {various parameters} each week.\n\n"
            '```json\n{"active_strategy": "momentum"}\n```'
        )
        result = _extract_json_block(text)
        self.assertIsNotNone(result)
        self.assertEqual(result["active_strategy"], "momentum")

    def test_extracts_json_bare_when_prose_brace_is_invalid_json(self):
        """Bare {prose} is invalid JSON; extraction should skip it and find the real object."""
        text = 'See {this} example.\n{"real": true}'
        result = _extract_json_block(text)
        self.assertIsNotNone(result)
        self.assertEqual(result["real"], True)

    def test_truncated_json_returns_none(self):
        text = '```json\n{"active_strategy": "hybrid", "parameter_adjustments": {'
        result = _extract_json_block(text)
        self.assertIsNone(result)

    def test_empty_string_returns_none(self):
        result = _extract_json_block("")
        self.assertIsNone(result)

    def test_plain_text_no_json_returns_none(self):
        result = _extract_json_block("This is a strategy memo with no JSON at all.")
        self.assertIsNone(result)

    def test_full_agent6_response_extracts_correctly(self):
        text = _make_valid_agent6_response()
        result = _extract_json_block(text)
        self.assertIsNotNone(result)
        self.assertEqual(result["active_strategy"], "hybrid")
        self.assertIn("parameter_adjustments", result)

    def test_extracts_correct_block_when_recommendation_updates_appears_first(self):
        """
        Agent 6 responses contain both a recommendation_updates block AND a parameter
        block. The extractor must return the parameter block, not recommendation_updates.
        """
        # recommendation_updates block appears first (as it might in a real response)
        rec_updates_block = json.dumps({"recommendation_updates": [
            {"rec_id": "rec_20260418_1", "verdict": "helped"}
        ]})
        param_block = json.dumps({
            "active_strategy": "hybrid",
            "parameter_adjustments": {"stop_loss_pct_core": 0.035},
            "director_notes": {"active_context": "...", "expiry": "2026-04-25", "priority": "normal"},
        })
        text = (
            "## STRATEGY DIRECTOR WEEKLY MEMO\n\nAnalysis text.\n\n"
            f"```json\n{rec_updates_block}\n```\n\n"
            "More analysis.\n\n"
            f"```json\n{param_block}\n```\n"
        )
        result = _extract_and_validate_agent6_json(text, "test-two-blocks")
        self.assertIsNotNone(result)
        self.assertEqual(result["active_strategy"], "hybrid")
        self.assertIn("parameter_adjustments", result)
        self.assertNotIn("recommendation_updates", result)


# ── Suite: Agent 6 validation ─────────────────────────────────────────────────

@unittest.skipUnless(FUNCTIONS_AVAILABLE, "weekly_review functions not importable")
class TestAgent6Validation(unittest.TestCase):

    def test_valid_response_returns_dict(self):
        text = _make_valid_agent6_response()
        result = _extract_and_validate_agent6_json(text, "test-agent6")
        self.assertIsNotNone(result)
        self.assertIsInstance(result, dict)

    def test_numeric_string_coerced_to_float(self):
        """stop_loss_pct_core returned as "0.035" string should be coerced to 0.035."""
        payload = {
            "active_strategy": "hybrid",
            "parameter_adjustments": {
                "stop_loss_pct_core": "0.035",
                "take_profit_multiple": "2.5",
            },
        }
        text = f"Memo\n\n```json\n{json.dumps(payload)}\n```"
        result = _extract_and_validate_agent6_json(text, "test-coerce")
        self.assertIsNotNone(result)
        adj = result["parameter_adjustments"]
        self.assertIsInstance(adj["stop_loss_pct_core"], float)
        self.assertAlmostEqual(adj["stop_loss_pct_core"], 0.035)
        self.assertIsInstance(adj["take_profit_multiple"], float)

    def test_non_coercible_string_dropped(self):
        """stop_loss_pct_core = "aggressive" should be dropped, not raise."""
        payload = {
            "active_strategy": "hybrid",
            "parameter_adjustments": {
                "stop_loss_pct_core": "aggressive",
                "max_positions": 15,
            },
        }
        text = f"Memo\n\n```json\n{json.dumps(payload)}\n```"
        result = _extract_and_validate_agent6_json(text, "test-drop")
        self.assertIsNotNone(result)
        adj = result["parameter_adjustments"]
        self.assertNotIn("stop_loss_pct_core", adj)
        self.assertEqual(adj["max_positions"], 15)

    def test_parse_failure_logs_warning_with_excerpt(self):
        """Truncated JSON logs WARNING with raw response excerpt."""
        truncated = "## STRATEGY MEMO\n\nAnalysis...\n\n```json\n{unterminated"
        # Logger name is the module's __name__, which equals the spec name used to load it
        _logger_name = _wr_mod.__name__
        with self.assertLogs(_logger_name, level="WARNING") as cm:
            result = _extract_and_validate_agent6_json(truncated, "test-truncated")
        self.assertIsNone(result)
        self.assertTrue(
            any("500 chars" in msg or "excerpt" in msg or "parse failed" in msg for msg in cm.output),
            f"Expected warning with excerpt in: {cm.output}",
        )

    def test_empty_response_logs_warning(self):
        _logger_name = _wr_mod.__name__
        with self.assertLogs(_logger_name, level="WARNING") as cm:
            result = _extract_and_validate_agent6_json("", "test-empty")
        self.assertIsNone(result)
        self.assertTrue(any("parse failed" in msg for msg in cm.output))

    def test_non_numeric_field_not_in_numeric_set_passes_through(self):
        """String fields like min_confidence_threshold pass through unchanged."""
        payload = {
            "active_strategy": "hybrid",
            "parameter_adjustments": {
                "min_confidence_threshold": "medium",
                "sector_rotation_bias": "neutral",
            },
        }
        text = f"```json\n{json.dumps(payload)}\n```"
        result = _extract_and_validate_agent6_json(text, "test-strings")
        self.assertIsNotNone(result)
        adj = result["parameter_adjustments"]
        self.assertEqual(adj["min_confidence_threshold"], "medium")
        self.assertEqual(adj["sector_rotation_bias"], "neutral")


# ── Suite: whitelist gate (preserved from existing logic) ─────────────────────

@unittest.skipUnless(FUNCTIONS_AVAILABLE, "weekly_review functions not importable")
class TestWhitelistGate(unittest.TestCase):
    """
    The whitelist gate (only config["parameters"] keys may be updated) lives in
    the run_review() body. Test it indirectly by verifying _extract_and_validate
    returns unknown keys so the caller can reject them.
    """

    def test_unknown_key_returned_by_extractor(self):
        """_extract_and_validate returns unknown keys unchanged so caller's whitelist can filter them."""
        payload = {
            "active_strategy": "hybrid",
            "parameter_adjustments": {
                "stop_loss_pct_core": 0.035,
                "invented_new_key": 99.9,
            },
        }
        text = f"```json\n{json.dumps(payload)}\n```"
        result = _extract_and_validate_agent6_json(text, "test-whitelist")
        self.assertIsNotNone(result)
        adj = result["parameter_adjustments"]
        # Both keys present — whitelist filtering happens at the call site, not here
        self.assertIn("stop_loss_pct_core", adj)
        self.assertIn("invented_new_key", adj)

    def test_whitelist_filtering_in_isolation(self):
        """Simulate the whitelist gate: only known keys may be written."""
        known_keys = {"stop_loss_pct_core", "take_profit_multiple", "max_positions"}
        proposed = {
            "stop_loss_pct_core": 0.03,
            "take_profit_multiple": 2.5,
            "invented_param": 42,
        }
        accepted = {k: v for k, v in proposed.items() if k in known_keys}
        rejected = [k for k in proposed if k not in known_keys]
        self.assertIn("stop_loss_pct_core", accepted)
        self.assertIn("take_profit_multiple", accepted)
        self.assertNotIn("invented_param", accepted)
        self.assertEqual(rejected, ["invented_param"])

    def test_wrong_type_rejected_from_numeric_set(self):
        """A list value for a numeric field must be dropped, not raise."""
        payload = {
            "active_strategy": "hybrid",
            "parameter_adjustments": {
                "stop_loss_pct_core": [0.03, 0.04],  # list — invalid
                "max_positions": 15,
            },
        }
        text = f"```json\n{json.dumps(payload)}\n```"
        result = _extract_and_validate_agent6_json(text, "test-list-type")
        self.assertIsNotNone(result)
        adj = result["parameter_adjustments"]
        self.assertNotIn("stop_loss_pct_core", adj)
        self.assertEqual(adj["max_positions"], 15)


_merge_blocked_symbols = getattr(_wr_mod, "_merge_blocked_symbols", None)
_MERGE_AVAILABLE = _merge_blocked_symbols is not None


# ── Suite: blocked_symbols guard ──────────────────────────────────────────────

@unittest.skipUnless(_MERGE_AVAILABLE, "weekly_review._merge_blocked_symbols not importable")
class TestBlockedSymbolsGuard(unittest.TestCase):

    def test_identical_proposed_no_removal(self):
        merged, removed = _merge_blocked_symbols(["QCOM"], ["QCOM"])
        self.assertEqual(merged, ["QCOM"])
        self.assertEqual(removed, [])

    def test_empty_proposed_preserves_existing(self):
        """Agent 6 sets blocked_symbols: [] → QCOM must survive."""
        merged, removed = _merge_blocked_symbols(["QCOM"], [])
        self.assertEqual(merged, ["QCOM"])
        self.assertEqual(removed, ["QCOM"])

    def test_partial_removal_preserves_all_existing(self):
        """Agent 6 omits AAPL → both AAPL and QCOM survive."""
        merged, removed = _merge_blocked_symbols(["QCOM", "AAPL"], ["QCOM"])
        self.assertIn("QCOM", merged)
        self.assertIn("AAPL", merged)
        self.assertEqual(removed, ["AAPL"])

    def test_append_new_symbol_allowed(self):
        """Agent 6 adds MSFT → QCOM + MSFT both present; no removal."""
        merged, removed = _merge_blocked_symbols(["QCOM"], ["QCOM", "MSFT"])
        self.assertIn("QCOM", merged)
        self.assertIn("MSFT", merged)
        self.assertEqual(removed, [])

    def test_non_list_proposed_treated_as_empty(self):
        """Agent 6 returns None or a non-list → existing symbols preserved."""
        merged, removed = _merge_blocked_symbols(["QCOM"], None)
        self.assertEqual(merged, ["QCOM"])
        self.assertEqual(removed, ["QCOM"])

    def test_non_list_existing_treated_as_empty(self):
        """Corrupt existing value → proposed symbols adopted without crash."""
        merged, removed = _merge_blocked_symbols(None, ["QCOM"])
        self.assertIn("QCOM", merged)
        self.assertEqual(removed, [])


if __name__ == "__main__":
    unittest.main()
