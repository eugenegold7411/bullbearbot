"""
tests/test_qualitative_sweep_parallel.py

Tests for the parallel two-batch qualitative sweep and priority-ordered
symbol selection (QS-01 through QS-08).
"""
from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure the project root is on sys.path
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_ctx_entry(sym: str, ts: str | None = None) -> dict:
    now = ts or datetime.now(timezone.utc).isoformat()
    return {
        "thesis_tags": ["test"],
        "macro_beta_stress": "low",
        "catalyst_active": None,
        "catalyst_expiry_date": None,
        "narrative": "test narrative",
        "refreshed_at": now,
    }


def _make_batch_response(symbols: list[str]) -> tuple[dict, dict]:
    """Simulate a successful _run_single_batch return value."""
    sym_ctx = {s: _make_ctx_entry(s) for s in symbols}
    regime_ctx = {"narrative": "test", "risk_on_catalysts": [], "risk_off_catalysts": []}
    return sym_ctx, regime_ctx


def _import_module():
    """Import bot_stage1_5_qualitative with minimal mocking."""
    # Stub out bot_clients if not available in test env
    if "bot_clients" not in sys.modules:
        bc = types.ModuleType("bot_clients")
        bc.MODEL_FAST = "claude-haiku-4-5-20251001"
        bc._get_claude = MagicMock()
        sys.modules["bot_clients"] = bc
    if "cost_tracker" not in sys.modules:
        ct = types.ModuleType("cost_tracker")
        ct.get_tracker = MagicMock(return_value=MagicMock())
        sys.modules["cost_tracker"] = ct
    if "cost_attribution" not in sys.modules:
        ca = types.ModuleType("cost_attribution")
        ca.log_claude_call_to_spine = MagicMock()
        sys.modules["cost_attribution"] = ca

    import importlib  # noqa: PLC0415, E402

    import bot_stage1_5_qualitative as m  # noqa: PLC0415, E402

    importlib.reload(m)
    return m


# ── QS-01: Priority ordering puts held positions first ───────────────────────

class TestQS01HeldPositionsFirst(unittest.TestCase):
    def setUp(self):
        self.mod = _import_module()

    def test_held_symbols_come_before_morning_brief(self):
        all_syms = ["NVDA", "GLD", "AMZN", "SPY", "QQQ"]

        with (
            patch.object(self.mod, "_get_a1_held_symbols", return_value=["GLD", "AMZN"]),
            patch.object(self.mod, "_get_a2_underlying_symbols", return_value=[]),
            patch.object(self.mod, "_get_morning_brief_symbols", return_value=["NVDA"]),
            patch.object(self.mod, "_get_signal_score_ranked_symbols", return_value=["SPY", "QQQ"]),
        ):
            result = self.mod.build_priority_ordered_symbols(all_syms)

        # GLD and AMZN must be the first two (held positions)
        self.assertEqual(result[0], "GLD")
        self.assertEqual(result[1], "AMZN")
        # NVDA (morning brief) comes after held positions
        self.assertIn("NVDA", result)
        self.assertLess(result.index("AMZN"), result.index("NVDA"))


# ── QS-02: Both batches run and merge into single output ─────────────────────

class TestQS02BothBatchesMerge(unittest.TestCase):
    def setUp(self):
        self.mod = _import_module()

    def test_merge_produces_combined_output(self):
        # 60 symbols → batch1=47, batch2=13
        all_syms = [f"SYM{i:02d}" for i in range(60)]

        def fake_batch(md, regime, syms, batch_num):
            return _make_batch_response(syms)

        with patch.object(self.mod, "build_priority_ordered_symbols", return_value=all_syms):
            with patch.object(self.mod, "_run_single_batch", side_effect=fake_batch):
                with patch.object(self.mod, "_atomic_write"):
                    result = self.mod.run_qualitative_sweep({}, {}, all_syms)

        self.assertIn("symbol_context", result)
        sym_ctx = result["symbol_context"]
        # All 60 symbols should appear
        for s in all_syms:
            self.assertIn(s, sym_ctx)


# ── QS-03: All input symbols have entries (or null) in merged output ─────────

class TestQS03AllSymbolsHaveEntries(unittest.TestCase):
    def setUp(self):
        self.mod = _import_module()

    def test_all_symbols_present_after_merge(self):
        # 20 symbols, one batch returns null for some
        all_syms = [f"T{i:02d}" for i in range(20)]

        def fake_batch(md, regime, syms, batch_num):
            ctx = {}
            for i, s in enumerate(syms):
                ctx[s] = None if i % 3 == 0 else _make_ctx_entry(s)
            return ctx, {}

        with (
            patch.object(self.mod, "_run_single_batch", side_effect=fake_batch),
            patch.object(self.mod, "_atomic_write"),
        ):
            result = self.mod.run_qualitative_sweep({}, {}, all_syms)

        sym_ctx = result.get("symbol_context", {})
        for s in [sym.upper() for sym in all_syms]:
            self.assertIn(s, sym_ctx)


# ── QS-04: No symbol appears twice in combined input_symbols ────────────────

class TestQS04NoDuplicatesInInputSymbols(unittest.TestCase):
    def setUp(self):
        self.mod = _import_module()

    def test_no_duplicates_in_input_symbols(self):
        # Provide duplicates in the raw input
        raw_syms = ["NVDA", "GLD", "NVDA", "AMZN", "GLD", "SPY"]

        with (
            patch.object(self.mod, "_run_single_batch", return_value=({}, {})),
            patch.object(self.mod, "_atomic_write"),
        ):
            result = self.mod.run_qualitative_sweep({}, {}, raw_syms)

        input_syms = result.get("input_symbols", [])
        self.assertEqual(len(input_syms), len(set(input_syms)))

    def test_build_priority_ordered_deduplicates(self):
        all_syms = ["NVDA", "GLD", "AMZN"]
        with (
            patch.object(self.mod, "_get_a1_held_symbols", return_value=["NVDA", "GLD"]),
            patch.object(self.mod, "_get_a2_underlying_symbols", return_value=["NVDA"]),  # duplicate
            patch.object(self.mod, "_get_morning_brief_symbols", return_value=["GLD"]),   # duplicate
            patch.object(self.mod, "_get_signal_score_ranked_symbols", return_value=[]),
        ):
            result = self.mod.build_priority_ordered_symbols(all_syms)

        self.assertEqual(len(result), len(set(result)))
        self.assertEqual(set(result), {"NVDA", "GLD", "AMZN"})


# ── QS-05: Batch sizes ≤47 and ≤46 ──────────────────────────────────────────

class TestQS05BatchSizes(unittest.TestCase):
    def setUp(self):
        self.mod = _import_module()

    def test_93_symbols_splits_47_46(self):
        all_syms = [f"S{i:03d}" for i in range(93)]
        captured = []

        def fake_batch(md, regime, syms, batch_num):
            captured.append((batch_num, syms))
            return _make_batch_response(syms)

        with (
            patch.object(self.mod, "_run_single_batch", side_effect=fake_batch),
            patch.object(self.mod, "_atomic_write"),
        ):
            self.mod.run_qualitative_sweep({}, {}, all_syms)

        self.assertEqual(len(captured), 2)
        b1_syms = next(syms for bn, syms in captured if bn == 1)
        b2_syms = next(syms for bn, syms in captured if bn == 2)
        self.assertLessEqual(len(b1_syms), 47)
        self.assertLessEqual(len(b2_syms), 46)

    def test_30_symbols_single_batch(self):
        """Fewer than 48 symbols: only batch1 fires, batch2 is empty."""
        all_syms = [f"S{i:02d}" for i in range(30)]
        captured = []

        def fake_batch(md, regime, syms, batch_num):
            captured.append((batch_num, len(syms)))
            return _make_batch_response(syms)

        with (
            patch.object(self.mod, "_run_single_batch", side_effect=fake_batch),
            patch.object(self.mod, "_atomic_write"),
        ):
            self.mod.run_qualitative_sweep({}, {}, all_syms)

        # Only batch1 should fire for ≤47 symbols
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][0], 1)


# ── QS-06: One batch fails, other results still saved ────────────────────────

class TestQS06OneBatchFailSaved(unittest.TestCase):
    def setUp(self):
        self.mod = _import_module()

    def test_batch1_fails_batch2_saves(self):
        all_syms = [f"X{i:02d}" for i in range(60)]
        saved_payload = {}

        def fake_batch(md, regime, syms, batch_num):
            if batch_num == 1:
                raise RuntimeError("simulated batch1 failure")
            return _make_batch_response(syms)

        def fake_write(path, payload):
            saved_payload.update(payload)

        with (
            patch.object(self.mod, "_run_single_batch", side_effect=fake_batch),
            patch.object(self.mod, "_atomic_write", side_effect=fake_write),
        ):
            result = self.mod.run_qualitative_sweep({}, {}, all_syms)

        # batch2 results should be in output
        self.assertNotEqual(result, {})
        self.assertIn("symbol_context", result)

    def test_batch2_fails_batch1_saves(self):
        all_syms = [f"Y{i:02d}" for i in range(60)]

        def fake_batch(md, regime, syms, batch_num):
            if batch_num == 2:
                return {}, {}  # empty dict = failure, no crash
            return _make_batch_response(syms)

        with (
            patch.object(self.mod, "_run_single_batch", side_effect=fake_batch),
            patch.object(self.mod, "_atomic_write"),
        ):
            result = self.mod.run_qualitative_sweep({}, {}, all_syms)

        # batch1 results should still be in output
        self.assertNotEqual(result, {})

    def test_both_batches_fail_returns_empty(self):
        all_syms = [f"Z{i:02d}" for i in range(60)]

        def fake_batch(md, regime, syms, batch_num):
            return {}, {}

        with (
            patch.object(self.mod, "_run_single_batch", side_effect=fake_batch),
        ):
            result = self.mod.run_qualitative_sweep({}, {}, all_syms)

        self.assertEqual(result, {})


# ── QS-07: input_symbols reflects all symbols from both batches ──────────────

class TestQS07InputSymbolsComplete(unittest.TestCase):
    def setUp(self):
        self.mod = _import_module()

    def test_input_symbols_contains_all_batched_symbols(self):
        all_syms = [f"M{i:02d}" for i in range(70)]

        def fake_batch(md, regime, syms, batch_num):
            return _make_batch_response(syms)

        with (
            patch.object(self.mod, "_run_single_batch", side_effect=fake_batch),
            patch.object(self.mod, "_atomic_write"),
        ):
            result = self.mod.run_qualitative_sweep({}, {}, all_syms)

        expected = sorted(set(s.upper() for s in all_syms))
        self.assertEqual(result.get("input_symbols"), expected)


# ── QS-08: Priority order: held → morning brief → signal scores ──────────────

class TestQS08PriorityOrder(unittest.TestCase):
    def setUp(self):
        self.mod = _import_module()

    def test_full_priority_ordering(self):
        all_syms = ["SPY", "QQQ", "GLD", "NVDA", "AMZN", "XBI", "MSFT", "TSM"]

        with (
            patch.object(self.mod, "_get_a1_held_symbols", return_value=["GLD", "AMZN"]),
            patch.object(self.mod, "_get_a2_underlying_symbols", return_value=["XBI"]),
            patch.object(self.mod, "_get_morning_brief_symbols", return_value=["NVDA"]),
            patch.object(self.mod, "_get_signal_score_ranked_symbols",
                         return_value=["MSFT", "SPY", "TSM", "QQQ"]),
        ):
            result = self.mod.build_priority_ordered_symbols(all_syms)

        # Held positions first
        self.assertEqual(result[0], "GLD")
        self.assertEqual(result[1], "AMZN")
        # A2 structures next
        self.assertEqual(result[2], "XBI")
        # Morning brief next
        self.assertEqual(result[3], "NVDA")
        # Signal scores next (in ranked order, filtered to those in all_syms)
        self.assertIn("MSFT", result[4:])
        self.assertIn("SPY", result)
        # All symbols present exactly once
        self.assertEqual(len(result), len(set(result)))
        self.assertEqual(set(result), set(s.upper() for s in all_syms))

    def test_held_positions_before_signal_scores(self):
        all_syms = ["NVDA", "GLD", "SPY"]
        with (
            patch.object(self.mod, "_get_a1_held_symbols", return_value=["GLD"]),
            patch.object(self.mod, "_get_a2_underlying_symbols", return_value=[]),
            patch.object(self.mod, "_get_morning_brief_symbols", return_value=[]),
            patch.object(self.mod, "_get_signal_score_ranked_symbols",
                         return_value=["NVDA", "SPY"]),
        ):
            result = self.mod.build_priority_ordered_symbols(all_syms)

        self.assertEqual(result[0], "GLD")
        self.assertLess(result.index("GLD"), result.index("NVDA"))

    def test_morning_brief_before_signal_scores(self):
        all_syms = ["SPY", "QQQ", "NVDA", "GLD"]
        with (
            patch.object(self.mod, "_get_a1_held_symbols", return_value=[]),
            patch.object(self.mod, "_get_a2_underlying_symbols", return_value=[]),
            patch.object(self.mod, "_get_morning_brief_symbols", return_value=["GLD"]),
            patch.object(self.mod, "_get_signal_score_ranked_symbols",
                         return_value=["SPY", "QQQ", "NVDA"]),
        ):
            result = self.mod.build_priority_ordered_symbols(all_syms)

        self.assertEqual(result[0], "GLD")
        self.assertLess(result.index("GLD"), result.index("SPY"))


if __name__ == "__main__":
    unittest.main()
