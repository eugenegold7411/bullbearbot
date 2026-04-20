"""
tests/test_thesis_lab.py — Thesis Lab subsystem tests (Build 1 + 2).

Suites:
  29 — thesis_registry: record creation, retrieval, lifecycle transitions
  30 — thesis_research: parse_thesis_from_text with mock Claude response
  31 — thesis_research: ingest_citrini_corpus record count and field quality
"""

import json
import os
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

os.chdir(_BOT_DIR)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_record(**overrides):
    """Return a minimal valid ThesisRecord for testing."""
    import thesis_registry as tr
    defaults = dict(
        thesis_id="thesis_20260420_120000_abcd",
        source_type="manual",
        source_ref="test",
        title="Long IBIT Bitcoin Rebound",
        date_opened="2026-04-20",
        status="proposed",
        time_horizons=[3, 6],
        narrative="Bitcoin is staging a catch-up rebound.",
        market_belief="BTC is undervalued relative to macro tailwinds.",
        market_missing="Market is ignoring ETF inflow momentum.",
        primary_bottleneck="Iran diplomatic resolution removing risk premium.",
        confirming_signals=["ETF inflow acceleration", "hash rate ATH"],
        countersignals=["Rising DXY", "Risk-off macro shift"],
        anchor_metrics=["IBIT weekly inflows", "BTC dominance"],
        base_expression={"instrument": "etf", "symbols": ["IBIT"], "direction": "long"},
        alternate_expressions=[],
        review_schedule=["2026-07-20", "2026-10-20"],
        tags=["crypto", "macro"],
        archetype_candidates=[],
        notes="",
        schema_version=1,
    )
    defaults.update(overrides)
    return tr.ThesisRecord(**defaults)


def _make_claude_response(extracted: dict) -> SimpleNamespace:
    """Build a mock Anthropic API response containing the given JSON dict."""
    text    = json.dumps(extracted)
    content = SimpleNamespace(text=text)
    return SimpleNamespace(content=[content])


# ─────────────────────────────────────────────────────────────────────────────
# SUITE 29 — thesis_registry
# ─────────────────────────────────────────────────────────────────────────────

class TestThesisRegistry(unittest.TestCase):

    def setUp(self):
        """Redirect all registry I/O to a tmp dir for each test."""
        import thesis_registry as tr
        self.tr = tr
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self.tmp.name)

        self.dir_patch  = mock.patch.object(tr, "_THESIS_LAB_DIR",  self.tmp_dir)
        self.file_patch = mock.patch.object(tr, "_THESES_FILE",      self.tmp_dir / "theses.json")
        self.quar_patch = mock.patch.object(tr, "_QUARANTINE_FILE",  self.tmp_dir / "quarantine.jsonl")

        self.dir_patch.start()
        self.file_patch.start()
        self.quar_patch.start()

    def tearDown(self):
        self.dir_patch.stop()
        self.file_patch.stop()
        self.quar_patch.stop()
        self.tmp.cleanup()

    # T29.1 — create + get roundtrip
    def test_create_and_get_roundtrip(self):
        record     = _make_record()
        thesis_id  = self.tr.create_thesis(record)
        self.assertEqual(thesis_id, record.thesis_id)

        retrieved = self.tr.get_thesis(thesis_id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.title, record.title)
        self.assertEqual(retrieved.status, "proposed")
        self.assertEqual(retrieved.base_expression["symbols"], ["IBIT"])

    # T29.2 — get missing returns None
    def test_get_missing_returns_none(self):
        result = self.tr.get_thesis("thesis_nonexistent_xxxx")
        self.assertIsNone(result)

    # T29.3 — list_theses unfiltered
    def test_list_theses_unfiltered(self):
        self.tr.create_thesis(_make_record(thesis_id="thesis_1", title="A"))
        self.tr.create_thesis(_make_record(thesis_id="thesis_2", title="B"))
        all_records = self.tr.list_theses()
        self.assertEqual(len(all_records), 2)

    # T29.4 — list_theses filtered by status
    def test_list_theses_filtered(self):
        self.tr.create_thesis(_make_record(thesis_id="thesis_a", status="proposed"))
        self.tr.create_thesis(_make_record(thesis_id="thesis_b", status="researched"))
        proposed = self.tr.list_theses(status="proposed")
        self.assertEqual(len(proposed), 1)
        self.assertEqual(proposed[0].status, "proposed")

    # T29.5 — update_thesis_status standard transition
    def test_update_status_standard_transition(self):
        record = _make_record()
        self.tr.create_thesis(record)
        self.tr.update_thesis_status(record.thesis_id, "researched")
        updated = self.tr.get_thesis(record.thesis_id)
        self.assertEqual(updated.status, "researched")

    # T29.6 — update_thesis_status with notes appended
    def test_update_status_appends_notes(self):
        record = _make_record(notes="")
        self.tr.create_thesis(record)
        self.tr.update_thesis_status(record.thesis_id, "researched", notes="confirmed signals")
        updated = self.tr.get_thesis(record.thesis_id)
        self.assertIn("confirmed signals", updated.notes)

    # T29.7 — non-standard transition emits warning but still applies
    def test_update_status_nonstandard_transition_warns_and_applies(self):
        record = _make_record(status="archived")
        self.tr.create_thesis(record)
        with self.assertLogs("thesis_registry", level="WARNING"):
            self.tr.update_thesis_status(record.thesis_id, "proposed")
        updated = self.tr.get_thesis(record.thesis_id)
        self.assertEqual(updated.status, "proposed")

    # T29.8 — unknown status raises ValueError
    def test_update_status_invalid_raises(self):
        record = _make_record()
        self.tr.create_thesis(record)
        with self.assertRaises(ValueError):
            self.tr.update_thesis_status(record.thesis_id, "totally_unknown_status")

    # T29.9 — missing thesis raises KeyError
    def test_update_status_missing_thesis_raises(self):
        with self.assertRaises(KeyError):
            self.tr.update_thesis_status("thesis_does_not_exist", "researched")

    # T29.10 — load_all returns all records
    def test_load_all(self):
        for i in range(3):
            self.tr.create_thesis(_make_record(thesis_id=f"thesis_{i}", title=f"Title {i}"))
        all_records = self.tr.load_all()
        self.assertEqual(len(all_records), 3)

    # T29.11 — schema_version field preserved
    def test_schema_version_preserved(self):
        record = _make_record()
        self.tr.create_thesis(record)
        retrieved = self.tr.get_thesis(record.thesis_id)
        self.assertEqual(retrieved.schema_version, 1)

    # T29.12 — write_quarantine produces valid JSONL
    def test_write_quarantine(self):
        self.tr.write_quarantine(
            record_dict={"thesis_id": "t1", "title": "Test"},
            reason="missing primary_bottleneck",
        )
        qfile = self.tmp_dir / "quarantine.jsonl"
        self.assertTrue(qfile.exists())
        lines = [json.loads(l) for l in qfile.read_text().strip().splitlines()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["reason"], "missing primary_bottleneck")
        self.assertIn("quarantined_at", lines[0])

    # T29.13 — full lifecycle progression
    def test_full_lifecycle_progression(self):
        record = _make_record()
        self.tr.create_thesis(record)
        for status in [
            "researched", "active_tracking", "checkpoint_3m_complete",
            "checkpoint_6m_complete", "checkpoint_9m_complete",
            "checkpoint_12m_complete", "archived",
        ]:
            self.tr.update_thesis_status(record.thesis_id, status)
        final = self.tr.get_thesis(record.thesis_id)
        self.assertEqual(final.status, "archived")

    # T29.14 — atomic write via .tmp rename
    def test_atomic_write(self):
        record = _make_record()
        self.tr.create_thesis(record)
        # Verify no leftover .tmp file
        tmp_file = self.tmp_dir / "theses.tmp"
        self.assertFalse(tmp_file.exists())
        self.assertTrue((self.tmp_dir / "theses.json").exists())


# ─────────────────────────────────────────────────────────────────────────────
# SUITE 30 — parse_thesis_from_text with mock Claude
# ─────────────────────────────────────────────────────────────────────────────

_MOCK_EXTRACTED = {
    "title": "Long IBIT Bitcoin ETF Rebound Play",
    "narrative": "Bitcoin is staging a catch-up rebound in Q1 2026 following abatement of negative reflexivity from MSTR and ETF outflows. The ETF is expected to retrace to prior highs.",
    "market_belief": "BTC is undervalued relative to macro tailwinds and ETF inflow momentum is resuming.",
    "market_missing": "Market is ignoring the supply shock from ETF net inflows exceeding miner output.",
    "primary_bottleneck": "Diplomatic resolution with Iran reducing geopolitical risk premium in risk assets.",
    "confirming_signals": ["IBIT weekly inflow acceleration", "BTC dominance rising"],
    "countersignals": ["Rising DXY", "Fed pause extending"],
    "anchor_metrics": ["IBIT weekly inflows (BTC)", "BTC 200d MA holding"],
    "base_expression": {"instrument": "etf", "symbols": ["IBIT"], "direction": "long"},
    "alternate_expressions": [],
    "tags": ["crypto", "macro", "etf"],
    "time_horizons": [3, 6, 9, 12],
}


class TestParseThesisFromText(unittest.TestCase):

    def setUp(self):
        import thesis_registry as tr
        import thesis_research as trs
        self.tr  = tr
        self.trs = trs
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self.tmp.name)

        self.dir_patch  = mock.patch.object(tr, "_THESIS_LAB_DIR", self.tmp_dir)
        self.file_patch = mock.patch.object(tr, "_THESES_FILE",     self.tmp_dir / "theses.json")
        self.quar_patch = mock.patch.object(tr, "_QUARANTINE_FILE", self.tmp_dir / "quarantine.jsonl")
        self.dir_patch.start()
        self.file_patch.start()
        self.quar_patch.start()

    def tearDown(self):
        self.dir_patch.stop()
        self.file_patch.stop()
        self.quar_patch.stop()
        self.tmp.cleanup()

    def _mock_client(self, extracted: dict = None):
        resp = _make_claude_response(extracted or _MOCK_EXTRACTED)
        client = mock.MagicMock()
        client.messages.create.return_value = resp
        return client

    # T30.1 — successful parse returns ThesisRecord in proposed status
    def test_parse_returns_proposed_record(self):
        with mock.patch.object(self.trs, "_get_client", return_value=self._mock_client()):
            record = self.trs.parse_thesis_from_text("IBIT long thesis text", "test.txt")
        self.assertEqual(record.status, "proposed")
        self.assertEqual(record.source_type, "memo")
        self.assertEqual(record.title, _MOCK_EXTRACTED["title"])
        self.assertEqual(record.base_expression["symbols"], ["IBIT"])
        self.assertEqual(record.base_expression["direction"], "long")

    # T30.2 — confirming_signals and countersignals are lists
    def test_parse_lists_are_populated(self):
        with mock.patch.object(self.trs, "_get_client", return_value=self._mock_client()):
            record = self.trs.parse_thesis_from_text("text", "test.txt")
        self.assertIsInstance(record.confirming_signals, list)
        self.assertIsInstance(record.countersignals, list)
        self.assertGreater(len(record.confirming_signals), 0)

    # T30.3 — review_schedule generated from time_horizons
    def test_parse_review_schedule_generated(self):
        with mock.patch.object(self.trs, "_get_client", return_value=self._mock_client()):
            record = self.trs.parse_thesis_from_text("text", "test.txt")
        self.assertEqual(len(record.review_schedule), len(_MOCK_EXTRACTED["time_horizons"]))

    # T30.4 — markdown fence stripped if present
    def test_parse_strips_markdown_fences(self):
        fenced = f"```json\n{json.dumps(_MOCK_EXTRACTED)}\n```"
        resp   = SimpleNamespace(content=[SimpleNamespace(text=fenced)])
        client = mock.MagicMock()
        client.messages.create.return_value = resp
        with mock.patch.object(self.trs, "_get_client", return_value=client):
            record = self.trs.parse_thesis_from_text("text", "test.txt")
        self.assertEqual(record.title, _MOCK_EXTRACTED["title"])

    # T30.5 — raises on invalid JSON from Claude
    def test_parse_raises_on_invalid_json(self):
        resp   = SimpleNamespace(content=[SimpleNamespace(text="not valid json")])
        client = mock.MagicMock()
        client.messages.create.return_value = resp
        with mock.patch.object(self.trs, "_get_client", return_value=client):
            with self.assertRaises(Exception):
                self.trs.parse_thesis_from_text("text", "test.txt")

    # T30.6 — schema_version defaults to 1
    def test_parse_schema_version(self):
        with mock.patch.object(self.trs, "_get_client", return_value=self._mock_client()):
            record = self.trs.parse_thesis_from_text("text", "test.txt")
        self.assertEqual(record.schema_version, 1)

    # T30.7 — missing primary_bottleneck caught by _validate_extracted
    def test_validate_quarantines_missing_bottleneck(self):
        bad = dict(_MOCK_EXTRACTED, primary_bottleneck="")
        reason = self.trs._validate_extracted(bad)
        self.assertIsNotNone(reason)
        self.assertIn("primary_bottleneck", reason)

    # T30.8 — ambiguous base_expression symbols quarantined
    def test_validate_quarantines_empty_symbols(self):
        bad = dict(_MOCK_EXTRACTED)
        bad["base_expression"] = {"instrument": "equity", "symbols": [""], "direction": "long"}
        reason = self.trs._validate_extracted(bad)
        self.assertIsNotNone(reason)
        self.assertIn("symbols", reason)

    # T30.9 — invalid direction quarantined
    def test_validate_quarantines_bad_direction(self):
        bad = dict(_MOCK_EXTRACTED)
        bad["base_expression"] = {"instrument": "equity", "symbols": ["AAPL"], "direction": "sideways"}
        reason = self.trs._validate_extracted(bad)
        self.assertIsNotNone(reason)

    # T30.10 — valid record returns None from _validate_extracted
    def test_validate_passes_valid_record(self):
        reason = self.trs._validate_extracted(_MOCK_EXTRACTED)
        self.assertIsNone(reason)


# ─────────────────────────────────────────────────────────────────────────────
# SUITE 31 — ingest_citrini_corpus
# ─────────────────────────────────────────────────────────────────────────────

_CITRINI_FIXTURE = {
    "active_trades": [
        {
            "symbol": "IBIT",
            "direction": "long",
            "thesis_summary": "Bitcoin ETF catch-up rebound with ETF inflows resuming.",
            "entry_notes": "Exited Long IBIT/Short QQQ for ~8% gain and flipped long IBIT.",
            "active": True,
        },
        {
            "symbol": "EWM",
            "direction": "long",
            "thesis_summary": "Malaysia ASEAN onshoring and data center buildout.",
            "entry_notes": "Favorite ex-US equity index pick.",
            "active": True,
        },
        {
            "symbol": "FXI",
            "direction": "long",
            "thesis_summary": "China corporate recovery driven by anti-involution push.",
            "entry_notes": "Long FXI March 20-delta calls.",
            "active": True,
        },
    ],
    "watchlist_themes": [
        {
            "theme": "Housing affordability beneficiaries",
            "symbols": ["RKT", "HD"],
            "rationale": "Trump mortgage rate push benefits home names.",
        },
    ],
}


class TestIngestCitriniCorpus(unittest.TestCase):

    def setUp(self):
        import thesis_registry as tr
        import thesis_research as trs
        self.tr  = tr
        self.trs = trs
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self.tmp.name)

        # Write fixture corpus to tmp dir
        self.corpus_path = self.tmp_dir / "citrini_test.json"
        self.corpus_path.write_text(json.dumps(_CITRINI_FIXTURE))

        self.dir_patch  = mock.patch.object(tr, "_THESIS_LAB_DIR", self.tmp_dir)
        self.file_patch = mock.patch.object(tr, "_THESES_FILE",     self.tmp_dir / "theses.json")
        self.quar_patch = mock.patch.object(tr, "_QUARANTINE_FILE", self.tmp_dir / "quarantine.jsonl")
        self.dir_patch.start()
        self.file_patch.start()
        self.quar_patch.start()

    def tearDown(self):
        self.dir_patch.stop()
        self.file_patch.stop()
        self.quar_patch.stop()
        self.tmp.cleanup()

    def _mock_parse_batch(self, items, source_ref):
        """Return one researched ThesisRecord per item (mocks Claude success)."""
        results = []
        for item in items:
            symbol    = item.get("symbol", item.get("theme", "TEST"))
            direction = item.get("direction", "long")
            syms      = item.get("symbols", [symbol] if symbol else [symbol])
            results.append(self.tr.ThesisRecord(
                thesis_id=self.tr.generate_thesis_id(),
                source_type="batch_memo",
                source_ref=source_ref,
                title=f"Long {symbol}",
                date_opened="2026-04-20",
                status="researched",
                time_horizons=[3, 6, 9, 12],
                narrative=item.get("thesis_summary", item.get("rationale", "test narrative")),
                market_belief="Market is mispricing this asset.",
                market_missing="Consensus is ignoring structural tailwinds.",
                primary_bottleneck="Macro reversal or policy change.",
                confirming_signals=["Signal A", "Signal B"],
                countersignals=["Counter A"],
                anchor_metrics=["Metric A"],
                base_expression={"instrument": "etf", "symbols": syms, "direction": direction},
                alternate_expressions=[],
                review_schedule=["2026-07-20", "2026-10-20", "2027-01-20", "2027-04-20"],
                tags=["test"],
                archetype_candidates=[],
                notes="",
                schema_version=1,
            ))
        return results

    # T31.1 — correct number of records ingested
    def test_ingestion_count(self):
        with mock.patch.object(self.trs, "parse_thesis_batch", side_effect=self._mock_parse_batch):
            ingested = self.trs.ingest_citrini_corpus(str(self.corpus_path))
        expected = len(_CITRINI_FIXTURE["active_trades"]) + len(_CITRINI_FIXTURE["watchlist_themes"])
        self.assertEqual(len(ingested), expected)

    # T31.2 — all ingested records exist in registry
    def test_all_records_in_registry(self):
        with mock.patch.object(self.trs, "parse_thesis_batch", side_effect=self._mock_parse_batch):
            ingested = self.trs.ingest_citrini_corpus(str(self.corpus_path))
        for tid in ingested:
            record = self.tr.get_thesis(tid)
            self.assertIsNotNone(record, f"Record {tid} missing from registry")

    # T31.3 — all ingested records have status='researched'
    def test_all_records_have_researched_status(self):
        with mock.patch.object(self.trs, "parse_thesis_batch", side_effect=self._mock_parse_batch):
            ingested = self.trs.ingest_citrini_corpus(str(self.corpus_path))
        for tid in ingested:
            record = self.tr.get_thesis(tid)
            self.assertEqual(record.status, "researched", f"{tid} has wrong status")

    # T31.4 — all required fields present and non-empty
    def test_required_fields_present(self):
        required_str = ["title", "narrative", "market_belief", "market_missing", "primary_bottleneck"]
        required_list = ["confirming_signals", "countersignals", "anchor_metrics", "review_schedule"]
        with mock.patch.object(self.trs, "parse_thesis_batch", side_effect=self._mock_parse_batch):
            ingested = self.trs.ingest_citrini_corpus(str(self.corpus_path))
        self.assertGreater(len(ingested), 0)
        for tid in ingested:
            record = self.tr.get_thesis(tid)
            d = asdict(record)
            for field in required_str:
                self.assertTrue(d[field].strip(), f"{tid}.{field} is empty")
            for field in required_list:
                self.assertIsInstance(d[field], list, f"{tid}.{field} is not a list")
                self.assertGreater(len(d[field]), 0, f"{tid}.{field} is empty list")

    # T31.5 — base_expression has correct structure
    def test_base_expression_structure(self):
        with mock.patch.object(self.trs, "parse_thesis_batch", side_effect=self._mock_parse_batch):
            ingested = self.trs.ingest_citrini_corpus(str(self.corpus_path))
        for tid in ingested:
            record = self.tr.get_thesis(tid)
            expr   = record.base_expression
            self.assertIn("instrument", expr)
            self.assertIn("symbols",   expr)
            self.assertIn("direction", expr)
            self.assertIn(expr["direction"], ("long", "short"))
            self.assertIsInstance(expr["symbols"], list)
            self.assertGreater(len(expr["symbols"]), 0)

    # T31.6 — quarantine items written to quarantine.jsonl and NOT in registry
    def test_quarantine_items_not_in_registry(self):
        def _mock_with_one_quarantine(items, source_ref):
            results = self._mock_parse_batch(items, source_ref)
            # Force first record to quarantine status
            if results:
                results[0].status = "quarantine"
                results[0].notes  = "Test quarantine"
            return results

        with mock.patch.object(self.trs, "parse_thesis_batch", side_effect=_mock_with_one_quarantine):
            ingested = self.trs.ingest_citrini_corpus(str(self.corpus_path))

        total_items = len(_CITRINI_FIXTURE["active_trades"]) + len(_CITRINI_FIXTURE["watchlist_themes"])
        self.assertEqual(len(ingested), total_items - 1)

        qfile = self.tmp_dir / "quarantine.jsonl"
        self.assertTrue(qfile.exists())
        lines = [json.loads(l) for l in qfile.read_text().strip().splitlines()]
        self.assertEqual(len(lines), 1)

    # T31.7 — file not found raises
    def test_corpus_not_found_raises(self):
        with self.assertRaises(FileNotFoundError):
            self.trs.ingest_citrini_corpus("/nonexistent/citrini.json")

    # T31.8 — schema_version=1 on all ingested records
    def test_schema_version_on_all_records(self):
        with mock.patch.object(self.trs, "parse_thesis_batch", side_effect=self._mock_parse_batch):
            ingested = self.trs.ingest_citrini_corpus(str(self.corpus_path))
        for tid in ingested:
            record = self.tr.get_thesis(tid)
            self.assertEqual(record.schema_version, 1)


# ─────────────────────────────────────────────────────────────────────────────
# SUITE 32 — import safety
# ─────────────────────────────────────────────────────────────────────────────

class TestThesisLabImportSafety(unittest.TestCase):

    def test_registry_importable_without_env_vars(self):
        """thesis_registry must import cleanly with no env vars set."""
        import thesis_registry  # noqa: F401 — already imported, just asserts no exception

    def test_registry_has_no_bot_imports(self):
        """thesis_registry must not contain import statements for execution modules."""
        import thesis_registry
        import inspect
        lines = inspect.getsource(thesis_registry).splitlines()
        import_lines = [l for l in lines if l.strip().startswith(("import ", "from "))]
        import_text = "\n".join(import_lines)
        for forbidden in ("bot", "order_executor", "risk_kernel"):
            self.assertNotIn(forbidden, import_text,
                             f"thesis_registry has import of forbidden module: {forbidden!r}")

    def test_research_importable(self):
        """thesis_research must import cleanly (anthropic client is lazy)."""
        import thesis_research  # noqa: F401

    def test_research_has_no_bot_imports(self):
        """thesis_research must not contain import statements for execution modules."""
        import thesis_research
        import inspect
        lines = inspect.getsource(thesis_research).splitlines()
        import_lines = [l for l in lines if l.strip().startswith(("import ", "from "))]
        import_text = "\n".join(import_lines)
        for forbidden in ("bot", "order_executor", "risk_kernel"):
            self.assertNotIn(forbidden, import_text,
                             f"thesis_research has import of forbidden module: {forbidden!r}")

    def test_generate_thesis_id_format(self):
        import thesis_registry as tr
        tid = tr.generate_thesis_id()
        self.assertTrue(tid.startswith("thesis_"))
        parts = tid.split("_")
        # thesis_YYYYMMDD_HHMMSS_XXXX → 4 parts
        self.assertEqual(len(parts), 4)
        self.assertEqual(len(parts[1]), 8)   # YYYYMMDD
        self.assertEqual(len(parts[2]), 6)   # HHMMSS
        self.assertEqual(len(parts[3]), 4)   # random suffix


if __name__ == "__main__":
    unittest.main(verbosity=2)
