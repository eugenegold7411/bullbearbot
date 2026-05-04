"""
Tests for regime label normalisation — forward fix (_normalize_regime_labels)
and backfill script (backfill_regime.py).
"""

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bot_stage1_regime import _normalize_regime_labels

_SCRIPT = Path(__file__).parent.parent / "data" / "scripts" / "backfill_regime.py"


def _load_backfill_mod():
    spec = importlib.util.spec_from_file_location("backfill_regime", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_backfill(records: list, dry_run: bool = False) -> tuple[list, str]:
    """Run process_file() against a temp file; return (result_records, stdout)."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        dp = td_path / "decisions.json"
        dp.write_text(json.dumps(records, indent=2))

        mod = _load_backfill_mod()
        buf = io.StringIO()
        with redirect_stdout(buf):
            mod.process_file(dp, dry_run=dry_run)

        return json.loads(dp.read_text()), buf.getvalue()


# ── _normalize_regime_labels ──────────────────────────────────────────────────

class TestNormalizeRegimeLabels(unittest.TestCase):

    def test_normal_bias_normalized_to_neutral(self):
        self.assertEqual(_normalize_regime_labels({"bias": "normal"})["bias"], "neutral")

    def test_normal_uppercase_normalized(self):
        self.assertEqual(_normalize_regime_labels({"bias": "NORMAL"})["bias"], "neutral")

    def test_neutral_unchanged(self):
        self.assertEqual(_normalize_regime_labels({"bias": "neutral"})["bias"], "neutral")

    def test_risk_on_unchanged(self):
        self.assertEqual(_normalize_regime_labels({"bias": "risk_on"})["bias"], "risk_on")

    def test_risk_on_hyphen_normalized(self):
        self.assertEqual(_normalize_regime_labels({"bias": "risk-on"})["bias"], "risk_on")

    def test_risk_off_hyphen_normalized(self):
        self.assertEqual(_normalize_regime_labels({"bias": "risk-off"})["bias"], "risk_off")

    def test_macro_regime_normal_normalized(self):
        out = _normalize_regime_labels({"macro_regime": "normal"})
        self.assertEqual(out["macro_regime"], "neutral")

    def test_missing_fields_no_error(self):
        self.assertEqual(_normalize_regime_labels({}), {})

    def test_non_string_fields_skipped(self):
        out = _normalize_regime_labels({"bias": None})
        self.assertIsNone(out["bias"])


# ── backfill_regime.py ────────────────────────────────────────────────────────

class TestBackfillRegimeScript(unittest.TestCase):

    def test_normal_becomes_neutral(self):
        records = [
            {"decision_id": "d1", "regime": "normal"},
            {"decision_id": "d2", "regime": "neutral"},
        ]
        result, _ = _run_backfill(records)
        self.assertEqual(result[0]["regime"], "neutral")
        self.assertEqual(result[1]["regime"], "neutral")

    def test_risk_on_hyphen_normalized(self):
        records = [{"decision_id": "d1", "regime": "risk-on"}]
        result, _ = _run_backfill(records)
        self.assertEqual(result[0]["regime"], "risk_on")

    def test_risk_on_underscore_unchanged(self):
        records = [{"decision_id": "d1", "regime": "risk_on"}]
        result, _ = _run_backfill(records)
        self.assertEqual(result[0]["regime"], "risk_on")

    def test_caution_and_halt_untouched(self):
        records = [
            {"decision_id": "d1", "regime": "caution"},
            {"decision_id": "d2", "regime": "halt"},
        ]
        result, _ = _run_backfill(records)
        self.assertEqual(result[0]["regime"], "caution")
        self.assertEqual(result[1]["regime"], "halt")

    def test_idempotent(self):
        records = [{"decision_id": "d1", "regime": "normal"}]
        result1, _ = _run_backfill(records)
        result2, _ = _run_backfill(result1)
        self.assertEqual(result1[0]["regime"], "neutral")
        self.assertEqual(result2[0]["regime"], "neutral")

    def test_dry_run_does_not_write(self):
        records = [{"decision_id": "d1", "regime": "normal"}]
        result, out = _run_backfill(records, dry_run=True)
        self.assertEqual(result[0]["regime"], "normal")   # file unchanged
        self.assertIn("DRY RUN", out)

    def test_missing_file_skipped_without_crash(self):
        mod = _load_backfill_mod()
        buf = io.StringIO()
        with redirect_stdout(buf):
            mod.process_file(Path("/nonexistent/path.json"), dry_run=True)
        self.assertIn("SKIP", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
