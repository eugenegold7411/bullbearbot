"""
S8 Allocator Shadow Hardening tests.

Task 1 — Error artifact written on run_allocator_shadow() exception.
Task 2 — REALLOCATE plumbing: order_executor passes correct args to execute_reallocate.
Task 3 — trim_score_threshold=5 aligns allocator with system_v1.txt "4–5/10: TRIM".
"""

import ast
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_cfg(trim_thresh=5):
    return {
        "portfolio_allocator": {
            "enable_shadow":     True,
            "enable_live":       False,
            "trim_score_threshold": trim_thresh,
            "trim_severity": [
                {"score_max": 2, "trim_pct": 0.75},
                {"score_max": 4, "trim_pct": 0.50},
                {"score_max": 6, "trim_pct": 0.25},
            ],
            "min_rebalance_notional": 100,
            "replace_score_gap":  15,
            "weight_deadband":    0.02,
            "max_recommendations_per_cycle": 5,
            "same_symbol_daily_cooldown_enabled": False,
            "same_day_replace_block_hours": 6,
        }
    }


def _fake_position(symbol, market_value=5000.0, thesis_score=5, qty=100):
    from unittest.mock import MagicMock
    pos = MagicMock()
    pos.symbol = symbol
    pos.qty = str(qty)
    pos.market_value = str(market_value)
    pos.current_price = str(market_value / qty)
    return pos


def _pi_data_for(symbol, thesis_score):
    return {
        "thesis_scores": [{
            "symbol": symbol,
            "thesis_score": thesis_score,
            "thesis_status": "valid" if thesis_score >= 7 else "weakening",
            "catalyst": "test",
            "catalyst_age_days": 2,
            "catalyst_consumed": False,
            "catalyst_consumed_at": None,
            "earnings_days_away": None,
            "technical_intact": True,
            "above_ma20": True,
            "above_ema9": True,
            "trending_toward": "target",
            "sector_aligned": True,
            "weakest_factor": "none",
            "recommended_action": "hold",
            "override_flag": None,
        }],
        "sizes": {
            "available_for_new": 50000,
            "max_exposure": 30000,
            "core_size": 15000,
            "dynamic_size": 8000,
        },
        "forced_exits": [],
        "deadline_exits": [],
    }


# ---------------------------------------------------------------------------
# Task 1 — Error artifact written on exception
# ---------------------------------------------------------------------------

class TestErrorArtifactOnException(unittest.TestCase):
    """run_allocator_shadow() must write an error artifact when it raises."""

    def _run_with_forced_exception(self, exc_type=RuntimeError, msg="injected failure"):
        """Trigger an exception inside run_allocator_shadow and return (result, artifact_lines)."""
        import portfolio_allocator as pa

        with tempfile.TemporaryDirectory() as td:
            artifact_path = Path(td) / "shadow.jsonl"
            registry_path = Path(td) / "registry.json"

            with patch.object(pa, "_ARTIFACT_PATH", artifact_path), \
                 patch.object(pa, "_REGISTRY_JSON_PATH", registry_path), \
                 patch.object(pa, "_rank_incumbents",
                              side_effect=exc_type(msg)):
                result = pa.run_allocator_shadow(
                    pi_data={},
                    positions=[],
                    cfg=_base_cfg(),
                    equity=100_000.0,
                )

            lines = []
            if artifact_path.exists():
                lines = [l for l in artifact_path.read_text().splitlines() if l.strip()]

        return result, lines

    def test_error_artifact_written_on_exception(self):
        """An exception inside run_allocator_shadow writes status=error to JSONL."""
        result, lines = self._run_with_forced_exception()

        self.assertIsNone(result, "Must return None on exception")
        self.assertEqual(len(lines), 1, "Exactly one error artifact must be written")

        artifact = json.loads(lines[0])
        self.assertEqual(artifact["status"], "error")
        self.assertIn("injected failure", artifact["error"])
        self.assertIn("ts", artifact)

    def test_error_artifact_has_schema_version(self):
        """Error artifact includes schema_version field."""
        import portfolio_allocator as pa

        _, lines = self._run_with_forced_exception(ValueError, "boom")
        artifact = json.loads(lines[0])
        self.assertIn("schema_version", artifact)
        self.assertEqual(artifact["schema_version"], pa.SCHEMA_VERSION)

    def test_success_path_still_writes_normal_artifact(self):
        """Successful run still writes a non-error artifact."""
        import portfolio_allocator as pa

        with tempfile.TemporaryDirectory() as td:
            artifact_path = Path(td) / "shadow.jsonl"
            registry_path = Path(td) / "registry.json"

            with patch.object(pa, "_ARTIFACT_PATH", artifact_path), \
                 patch.object(pa, "_REGISTRY_JSON_PATH", registry_path), \
                 patch.object(pa, "_load_candidates", return_value=[]):
                pa.run_allocator_shadow(
                    pi_data={},
                    positions=[],
                    cfg=_base_cfg(),
                    equity=100_000.0,
                )

            lines = [l for l in artifact_path.read_text().splitlines() if l.strip()]

        self.assertEqual(len(lines), 1)
        artifact = json.loads(lines[0])
        self.assertNotEqual(artifact.get("status"), "error",
                            "Success path must not write error status")

    def test_multiple_exceptions_write_multiple_error_artifacts(self):
        """Each failed shadow call writes exactly one error artifact."""
        import portfolio_allocator as pa

        with tempfile.TemporaryDirectory() as td:
            artifact_path = Path(td) / "shadow.jsonl"
            registry_path = Path(td) / "registry.json"

            for _ in range(3):
                with patch.object(pa, "_ARTIFACT_PATH", artifact_path), \
                     patch.object(pa, "_REGISTRY_JSON_PATH", registry_path), \
                     patch.object(pa, "_rank_incumbents",
                                  side_effect=RuntimeError("repeated failure")):
                    pa.run_allocator_shadow(
                        pi_data={}, positions=[], cfg=_base_cfg(), equity=100_000.0
                    )

            lines = [l for l in artifact_path.read_text().splitlines() if l.strip()]

        self.assertEqual(len(lines), 3,
                         "Three failed calls must write three error artifacts")
        for line in lines:
            self.assertEqual(json.loads(line)["status"], "error")


# ---------------------------------------------------------------------------
# Task 2 — REALLOCATE plumbing: AST source inspection
# ---------------------------------------------------------------------------

class TestReallocatePlumbing(unittest.TestCase):
    """execute_reallocate must be called with (exit_symbol, action, alpaca_client)."""

    def _get_reallocate_branch_src(self):
        import order_executor
        src = inspect.getsource(order_executor.execute_all)
        return src

    def test_execute_reallocate_first_arg_is_action_get_exit_symbol(self):
        """execute_reallocate must receive action.get('exit_symbol') as first arg, not the dict."""
        src = self._get_reallocate_branch_src()
        tree = ast.parse(src)

        reallocate_calls = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name == "execute_reallocate":
                reallocate_calls.append(node)

        self.assertTrue(
            len(reallocate_calls) > 0,
            "execute_reallocate call must exist in execute_all source",
        )

        call = reallocate_calls[0]
        args = call.args

        self.assertGreaterEqual(len(args), 1, "Must pass at least one argument")
        first_arg = args[0]

        # First arg must NOT be a bare Name("action") — it must be a Call on action
        is_bare_action = isinstance(first_arg, ast.Name) and first_arg.id == "action"
        self.assertFalse(
            is_bare_action,
            "First arg must not be the bare 'action' dict — must be action.get('exit_symbol')",
        )

    def test_execute_reallocate_first_arg_uses_get_exit_symbol(self):
        """First arg must be action.get('exit_symbol') call."""
        src = self._get_reallocate_branch_src()
        tree = ast.parse(src)

        found_correct_call = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name != "execute_reallocate":
                continue

            args = node.args
            if not args:
                continue
            first_arg = args[0]

            # Looking for action.get('exit_symbol') — that's a Call node
            # with func=Attribute(value=Name('action'), attr='get')
            if isinstance(first_arg, ast.Call):
                fa_func = first_arg.func
                if (isinstance(fa_func, ast.Attribute) and
                        fa_func.attr == "get" and
                        isinstance(fa_func.value, ast.Name) and
                        fa_func.value.id == "action"):
                    call_args = first_arg.args
                    if (call_args and
                            isinstance(call_args[0], ast.Constant) and
                            call_args[0].value == "exit_symbol"):
                        found_correct_call = True

        self.assertTrue(
            found_correct_call,
            "execute_reallocate must be called with action.get('exit_symbol') as first arg",
        )

    def test_execute_reallocate_receives_three_args(self):
        """execute_reallocate must receive exactly three positional arguments."""
        src = self._get_reallocate_branch_src()
        tree = ast.parse(src)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name != "execute_reallocate":
                continue

            self.assertEqual(
                len(node.args), 3,
                "execute_reallocate must receive 3 positional args: exit_symbol, action, alpaca_client",
            )
            return

        self.fail("execute_reallocate call not found in execute_all")


# ---------------------------------------------------------------------------
# Task 3 — trim_score_threshold=5 alignment
# ---------------------------------------------------------------------------

class TestTrimScoreThreshold(unittest.TestCase):
    """trim_score_threshold=5 means score<=5 fires TRIM, score=6 does not."""

    def _run_shadow_trim_check(self, thesis_score, cfg=None):
        """Run one shadow cycle with a single position and return recommendations."""
        import portfolio_allocator as pa

        cfg = cfg or _base_cfg(trim_thresh=5)
        pos = _fake_position("XYZ", market_value=6000.0,
                             thesis_score=thesis_score, qty=100)
        pi = _pi_data_for("XYZ", thesis_score)

        with tempfile.TemporaryDirectory() as td:
            with patch.object(pa, "_ARTIFACT_PATH", Path(td) / "s.jsonl"), \
                 patch.object(pa, "_REGISTRY_JSON_PATH", Path(td) / "r.json"), \
                 patch.object(pa, "_load_candidates", return_value=[]):
                artifact = pa.run_allocator_shadow(
                    pi_data=pi,
                    positions=[pos],
                    cfg=cfg,
                    equity=100_000.0,
                )

        return artifact.get("proposed_actions", []) if artifact else []

    def test_score_5_triggers_trim(self):
        """thesis_score=5 must produce TRIM when trim_score_threshold=5."""
        recs = self._run_shadow_trim_check(thesis_score=5)
        trim_recs = [r for r in recs if r["action"] == "TRIM"]
        self.assertTrue(len(trim_recs) > 0,
                        f"score=5 must trigger TRIM with threshold=5; recs={recs}")

    def test_score_4_triggers_trim(self):
        """thesis_score=4 must still trigger TRIM."""
        recs = self._run_shadow_trim_check(thesis_score=4)
        trim_recs = [r for r in recs if r["action"] == "TRIM"]
        self.assertTrue(len(trim_recs) > 0,
                        "score=4 must trigger TRIM with threshold=5")

    def test_score_6_does_not_trigger_trim(self):
        """thesis_score=6 must NOT trigger TRIM with threshold=5."""
        recs = self._run_shadow_trim_check(thesis_score=6)
        trim_recs = [r for r in recs if r["action"] == "TRIM"]
        self.assertEqual(len(trim_recs), 0,
                         "score=6 must NOT trigger TRIM when threshold=5")

    def test_score_7_does_not_trigger_trim(self):
        """thesis_score=7 must produce HOLD, not TRIM."""
        recs = self._run_shadow_trim_check(thesis_score=7)
        trim_recs = [r for r in recs if r["action"] == "TRIM"]
        self.assertEqual(len(trim_recs), 0,
                         "score=7 must not trigger TRIM")

    def test_explicit_threshold_4_prevents_trim_at_score_5(self):
        """Explicit trim_score_threshold=4 in config must keep score=5 as HOLD."""
        cfg = _base_cfg(trim_thresh=4)
        recs = self._run_shadow_trim_check(thesis_score=5, cfg=cfg)
        trim_recs = [r for r in recs if r["action"] == "TRIM"]
        self.assertEqual(len(trim_recs), 0,
                         "With threshold=4, score=5 must NOT trigger TRIM")

    def test_strategy_config_json_has_threshold_5(self):
        """strategy_config.json must declare trim_score_threshold=5."""
        config_path = Path(__file__).parent.parent / "strategy_config.json"
        config = json.loads(config_path.read_text())
        pa_cfg = config.get("portfolio_allocator", {})
        self.assertIn("trim_score_threshold", pa_cfg,
                      "trim_score_threshold must be explicit in strategy_config.json")
        self.assertEqual(pa_cfg["trim_score_threshold"], 5,
                         "trim_score_threshold must be 5 in strategy_config.json")

    def test_pa_defaults_updated_to_5(self):
        """portfolio_allocator._PA_DEFAULTS must use threshold=5."""
        import portfolio_allocator as pa
        self.assertEqual(pa._PA_DEFAULTS["trim_score_threshold"], 5,
                         "_PA_DEFAULTS must be updated to 5 to match strategy_config.json")


if __name__ == "__main__":
    unittest.main()
