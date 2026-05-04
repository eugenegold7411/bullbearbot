"""
Tests for data/scripts/backfill_regime.py.
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

_SCRIPT = Path(__file__).parent.parent / "data" / "scripts" / "backfill_regime.py"


def _load_mod():
    spec = importlib.util.spec_from_file_location("backfill_regime", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_file(records: list, dry_run: bool = False):
    """Write records to a temp file, call process_file(), return (records_on_disk, stdout)."""
    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        import json as _j
        _j.dump(records, f, indent=2)
        path = Path(f.name)

    mod = _load_mod()
    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.process_file(path, dry_run=dry_run)

    result = json.loads(path.read_text())
    path.unlink(missing_ok=True)
    return result, buf.getvalue()


class TestBackfillRegime(unittest.TestCase):

    def test_normal_records_updated(self):
        records = [
            {"decision_id": "d1", "regime": "normal"},
            {"decision_id": "d2", "regime": "neutral"},
            {"decision_id": "d3", "regime": "risk_on"},
        ]
        result, _ = _run_file(records)
        self.assertEqual(result[0]["regime"], "neutral")
        self.assertEqual(result[1]["regime"], "neutral")
        self.assertEqual(result[2]["regime"], "risk_on")

    def test_risk_on_hyphen_normalized(self):
        records = [{"decision_id": "d1", "regime": "risk-on"}]
        result, _ = _run_file(records)
        self.assertEqual(result[0]["regime"], "risk_on")

    def test_idempotent(self):
        records = [{"decision_id": "d1", "regime": "normal"}]
        result1, _ = _run_file(records)
        result2, _ = _run_file(result1)
        self.assertEqual(result1[0]["regime"], "neutral")
        self.assertEqual(result2[0]["regime"], "neutral")

    def test_risk_on_records_untouched(self):
        records = [{"decision_id": "d1", "regime": "risk_on"}]
        result, _ = _run_file(records)
        self.assertEqual(result[0]["regime"], "risk_on")

    def test_caution_and_halt_untouched(self):
        records = [
            {"decision_id": "d1", "regime": "caution"},
            {"decision_id": "d2", "regime": "halt"},
        ]
        result, _ = _run_file(records)
        self.assertEqual(result[0]["regime"], "caution")
        self.assertEqual(result[1]["regime"], "halt")

    def test_dry_run_does_not_write(self):
        records = [{"decision_id": "d1", "regime": "normal"}]
        original = json.dumps(records, indent=2)

        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            f.write(original)
            path = Path(f.name)

        mod = _load_mod()
        buf = io.StringIO()
        with redirect_stdout(buf):
            mod.process_file(path, dry_run=True)

        self.assertEqual(path.read_text(), original)
        self.assertIn("DRY RUN", buf.getvalue())
        path.unlink(missing_ok=True)

    def test_missing_file_skipped_gracefully(self):
        mod = _load_mod()
        buf = io.StringIO()
        with redirect_stdout(buf):
            mod.process_file(Path("/tmp/nonexistent_file_99999.json"), dry_run=False)
        self.assertIn("SKIP", buf.getvalue())
