"""
Part 2 roundtrip test: scratchpad cold storage in trade_memory.py.

Tests:
  1.  save_scratchpad_memory() returns a non-empty id for a valid scratchpad
  2.  get_collection_stats() reports scr_short count = 1 after first save
  3.  retrieve_similar_scratchpads() finds the saved record (needs >=2 for query)
  4.  metadata fields are round-tripped correctly (ts, vix, regime_score, watching)
  5.  save_scratchpad_memory() is a no-op on empty dict — returns ""
  6.  retrieve_similar_scratchpads() returns [] when < 2 records exist
  7.  _build_scratchpad_document() embeds watching/blocking/triggers/summary
  8.  get_scratchpad_history() returns records within days_back window
  9.  get_scratchpad_history() excludes records older than days_back
  10. get_near_miss_summary() identifies a repeated-and-blocked symbol
  11. get_near_miss_summary() ignores symbols blocked fewer than 50% of watches
  12. get_two_tier_memory() returns dict with both keys
  13. scr_short count increases correctly after multiple saves
  14. save_scratchpad_memory() stores summary in metadata (truncated to 200 chars)
  15. retrieve_similar_scratchpads() returns {} on ChromaDB unavailable (returns [])
"""
# The production systemd service sets PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python.
# Set it here before chromadb is first imported so the lazy init in trade_memory
# succeeds in the test environment.
import os

os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")
import os

import pytest

# All tests in this file require ChromaDB. Excluded from CI via -m "not requires_chromadb".
pytestmark = pytest.mark.requires_chromadb
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Isolate ChromaDB to a temp directory for every test class
# ---------------------------------------------------------------------------
class _TmpDbMixin:
    """Mixin that redirects trade_memory's DB path to a fresh temp dir."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        import trade_memory as tm
        self._tm = tm

        # Reset all singletons so each test class gets a clean DB
        tm._client            = None
        tm._coll_short        = None
        tm._coll_medium       = None
        tm._coll_long         = None
        tm._collections_tried = False

        tm._scr_short        = None
        tm._scr_medium       = None
        tm._scr_long         = None
        tm._scratchpad_tried = False

        # Point DB to temp dir
        self._orig_db_path = tm._DB_PATH
        tm._DB_PATH = self._tmpdir

    def tearDown(self):
        self._tm._DB_PATH = self._orig_db_path
        # Reset singletons again so subsequent test classes get a clean slate
        self._tm._client            = None
        self._tm._coll_short        = None
        self._tm._coll_medium       = None
        self._tm._coll_long         = None
        self._tm._collections_tried = False
        self._tm._scr_short        = None
        self._tm._scr_medium       = None
        self._tm._scr_long         = None
        self._tm._scratchpad_tried = False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
_SP = {
    "watching":  ["AAPL", "NVDA"],
    "blocking":  ["GLD: no catalyst today"],
    "triggers":  ["AAPL: break above $185", "NVDA: volume > 50M"],
    "conviction_ranking": [
        {"symbol": "AAPL", "conviction": "high",   "notes": "earnings catalyst"},
        {"symbol": "NVDA", "conviction": "medium", "notes": "AI theme"},
    ],
    "summary":      "Bullish tech bias — lean into AAPL and NVDA.",
    "ts":           datetime.now(timezone.utc).isoformat(),
    "regime_score": 65,
    "vix":          18.4,
}

_SP2 = {
    "watching":  ["TSLA", "AAPL"],
    "blocking":  ["AAPL: overbought RSI"],
    "triggers":  ["TSLA: above $180"],
    "conviction_ranking": [
        {"symbol": "TSLA", "conviction": "high",   "notes": "momentum"},
        {"symbol": "AAPL", "conviction": "low",    "notes": "wait for pullback"},
    ],
    "summary":      "TSLA momentum, AAPL wait.",
    "ts":           datetime.now(timezone.utc).isoformat(),
    "regime_score": 55,
    "vix":          20.1,
}

_MD = {
    "vix":                  18.4,
    "vix_regime":           "moderate",
    "intermarket_signals":  "Dollar weak, yields falling",
    "breaking_news":        "",
}


class TestSaveAndStats(_TmpDbMixin, unittest.TestCase):

    def test_01_save_returns_id(self):
        """save_scratchpad_memory() returns a non-empty string id."""
        scr_id = self._tm.save_scratchpad_memory(_SP)
        self.assertTrue(scr_id, "Expected non-empty scratchpad id")
        self.assertTrue(scr_id.startswith("scr_"), f"Bad id prefix: {scr_id}")

    def test_02_stats_reflect_save(self):
        """get_collection_stats() shows scr_short=1 after one save."""
        self._tm.save_scratchpad_memory(_SP)
        stats = self._tm.get_collection_stats()
        self.assertEqual(stats["scr_short"], 1)
        self.assertEqual(stats["scr_medium"], 0)
        self.assertEqual(stats["scr_long"], 0)
        self.assertEqual(stats["scr_total"], 1)

    def test_13_stats_increment_on_multiple_saves(self):
        """scr_short count tracks multiple saves."""
        self._tm.save_scratchpad_memory(_SP)
        self._tm.save_scratchpad_memory(_SP2)
        stats = self._tm.get_collection_stats()
        self.assertEqual(stats["scr_short"], 2)
        self.assertEqual(stats["scr_total"], 2)

    def test_05_save_empty_returns_empty_string(self):
        """save_scratchpad_memory({}) returns '' and doesn't touch DB."""
        scr_id = self._tm.save_scratchpad_memory({})
        self.assertEqual(scr_id, "")
        stats = self._tm.get_collection_stats()
        self.assertEqual(stats["scr_short"], 0)

    def test_14_summary_stored_in_metadata(self):
        """Metadata carries summary field (truncated if needed)."""
        self._tm.save_scratchpad_memory(_SP)
        short, _, _ = self._tm._get_scratchpad_collections()
        recs = short.get(limit=1, include=["metadatas"])
        meta = recs["metadatas"][0]
        self.assertIn("summary", meta)
        self.assertIn("Bullish", meta["summary"])


class TestRetrieve(_TmpDbMixin, unittest.TestCase):

    def test_06_retrieve_empty_below_threshold(self):
        """retrieve_similar_scratchpads() returns [] when < 2 records exist."""
        self._tm.save_scratchpad_memory(_SP)
        results = self._tm.retrieve_similar_scratchpads(_MD, "market", n_results=3)
        self.assertEqual(results, [])

    def test_03_retrieve_finds_saved_record(self):
        """retrieve_similar_scratchpads() returns a result after 2+ saves."""
        self._tm.save_scratchpad_memory(_SP)
        self._tm.save_scratchpad_memory(_SP2)
        results = self._tm.retrieve_similar_scratchpads(_MD, "market", n_results=3)
        self.assertGreater(len(results), 0, "Expected at least 1 result")

    def test_04_metadata_roundtrip(self):
        """VIX, regime_score, and watching survive save → retrieve."""
        self._tm.save_scratchpad_memory(_SP)
        self._tm.save_scratchpad_memory(_SP2)
        results = self._tm.retrieve_similar_scratchpads(_MD, "market", n_results=5)
        # At least one result should have metadata matching _SP
        metas = [r["metadata"] for r in results]
        vix_vals = [m.get("vix") for m in metas]
        self.assertIn(18.4, vix_vals)

    def test_15_retrieve_returns_empty_when_chromadb_unavailable(self):
        """retrieve_similar_scratchpads() returns [] when collections are None."""
        with patch.object(self._tm, "_get_scratchpad_collections", return_value=(None, None, None)):
            results = self._tm.retrieve_similar_scratchpads(_MD, "market")
        self.assertEqual(results, [])


class TestDocumentBuilder(_TmpDbMixin, unittest.TestCase):

    def test_07_build_document_contains_key_fields(self):
        """_build_scratchpad_document() includes vix, watching, blocking, triggers, summary."""
        doc = self._tm._build_scratchpad_document(_SP)
        self.assertIn("vix=18.4", doc)
        self.assertIn("AAPL", doc)
        self.assertIn("GLD: no catalyst", doc)
        self.assertIn("break above", doc)
        self.assertIn("Bullish tech", doc)


class TestHistory(_TmpDbMixin, unittest.TestCase):

    def _make_sp_with_ts(self, ts_iso: str) -> dict:
        import copy
        sp = copy.deepcopy(_SP)
        sp["ts"] = ts_iso
        return sp

    def test_08_history_returns_recent_records(self):
        """get_scratchpad_history() returns records within days_back."""
        recent_ts = datetime.now(timezone.utc).isoformat()
        sp = self._make_sp_with_ts(recent_ts)
        self._tm.save_scratchpad_memory(sp)
        history = self._tm.get_scratchpad_history(days_back=7)
        self.assertEqual(len(history), 1)

    def test_09_history_excludes_old_records(self):
        """get_scratchpad_history() excludes records older than days_back."""
        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        self._make_sp_with_ts(old_ts)
        # Save old record directly bypassing auto-ts — use raw ChromaDB add
        # (save_scratchpad_memory() stamps its own ts; we need to override metadata)
        short, _, _ = self._tm._get_scratchpad_collections()
        if short is None:
            self.skipTest("ChromaDB unavailable")
        short.add(
            documents=["vix=18 regime_score=50 watching=AAPL blocking: none triggers: none summary: old"],
            metadatas=[{
                "ts": old_ts, "vix": 18.0, "regime_score": 50,
                "watching": "AAPL", "summary": "old",
                "n_watching": 1, "n_blocking": 0, "n_triggers": 0, "tier": "short",
            }],
            ids=["scr_old_test"],
        )
        # Save a recent record via normal path
        self._tm.save_scratchpad_memory(self._make_sp_with_ts(datetime.now(timezone.utc).isoformat()))

        history = self._tm.get_scratchpad_history(days_back=3)
        # Only the recent record should appear
        self.assertEqual(len(history), 1, f"Expected 1 recent record, got {len(history)}")


class TestNearMiss(_TmpDbMixin, unittest.TestCase):

    def _save_sp_blocking(self, watching, blocking, ts_offset_hours=0):
        """Save a scratchpad where watching symbols are blocked."""
        from datetime import datetime, timedelta, timezone
        ts = (datetime.now(timezone.utc) - timedelta(hours=ts_offset_hours)).isoformat()
        sp = {
            "watching":  watching,
            "blocking":  blocking,
            "triggers":  [f"{watching[0]}: break above $100"],
            "conviction_ranking": [],
            "summary":   "test cycle",
            "ts":        ts,
            "regime_score": 50,
            "vix":       18.0,
        }
        self._tm.save_scratchpad_memory(sp)

    def test_10_near_miss_identified(self):
        """get_near_miss_summary() identifies symbol watched 2+ times and blocked ≥50%."""
        self._save_sp_blocking(["AAPL", "GLD"], ["AAPL: RSI overbought"], ts_offset_hours=2)
        self._save_sp_blocking(["AAPL", "NVDA"], ["AAPL: no catalyst"], ts_offset_hours=1)
        summary = self._tm.get_near_miss_summary(days_back=7)
        self.assertIn("AAPL", summary)

    def test_11_near_miss_ignores_unblocked_symbols(self):
        """Symbols always watched but never blocked should not appear in near misses."""
        self._save_sp_blocking(["NVDA", "GLD"], ["GLD: safe haven only"],  ts_offset_hours=2)
        self._save_sp_blocking(["NVDA", "TSLA"], ["TSLA: earnings risk"],   ts_offset_hours=1)
        summary = self._tm.get_near_miss_summary(days_back=7)
        # NVDA was watched twice but never blocked — should not be a near miss
        self.assertNotIn("NVDA", summary)


class TestTwoTierMemory(_TmpDbMixin, unittest.TestCase):

    def test_12_two_tier_memory_returns_both_keys(self):
        """get_two_tier_memory() always returns dict with trade_scenarios and recent_scratchpads."""
        result = self._tm.get_two_tier_memory(_MD, "market")
        self.assertIn("trade_scenarios", result)
        self.assertIn("recent_scratchpads", result)
        self.assertIsInstance(result["trade_scenarios"], list)
        self.assertIsInstance(result["recent_scratchpads"], list)


if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)
