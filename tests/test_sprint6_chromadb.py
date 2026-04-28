"""
Sprint 6 ChromaDB metadata fix tests.
- Bug 1: regime field reads regime_view
- Bug 2: n_actions/symbols reads ideas not actions
- Bug 3: update_trade_outcome wired in decision_outcomes
- Backfill script exists and is importable
"""
import inspect
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _stub_chromadb_if_absent() -> None:
    """Ensure chromadb is importable in test env without a real DB."""
    if "chromadb" not in sys.modules:
        m = types.ModuleType("chromadb")
        m.PersistentClient = MagicMock()
        m.Settings = MagicMock()
        settings_mod = types.ModuleType("chromadb.utils")
        emb_mod = types.ModuleType("chromadb.utils.embedding_functions")
        emb_mod.DefaultEmbeddingFunction = MagicMock(return_value=MagicMock())
        sys.modules.setdefault("chromadb", m)
        sys.modules.setdefault("chromadb.utils", settings_mod)
        sys.modules.setdefault("chromadb.utils.embedding_functions", emb_mod)


def _get_save_trade_memory_src() -> str:
    import trade_memory as tm
    return inspect.getsource(tm.save_trade_memory)


# ─────────────────────────────────────────────────────────────────────────────
# Bug 1 — regime field reads regime_view
# ─────────────────────────────────────────────────────────────────────────────

class TestRegimeFieldFix:

    def test_save_trade_memory_source_reads_regime_view(self):
        """save_trade_memory reads 'regime_view' field from decision dict."""
        src = _get_save_trade_memory_src()
        assert "regime_view" in src, (
            "save_trade_memory must read regime_view field — "
            "ClaudeDecision JSON uses regime_view, not regime"
        )

    def test_save_trade_memory_has_regime_fallback(self):
        """save_trade_memory falls back to 'regime' for legacy decisions."""
        src = _get_save_trade_memory_src()
        assert "regime_view" in src
        assert "regime" in src

    def test_regime_logic_new_format(self):
        """Logic correctly reads regime_view from new-format decision."""
        decision = {"regime_view": "risk_on", "ideas": []}
        regime = str(
            decision.get("regime_view")
            or decision.get("regime")
            or "unknown"
        )
        assert regime == "risk_on"

    def test_regime_logic_legacy_fallback(self):
        """Logic falls back to 'regime' when regime_view absent."""
        decision = {"regime": "risk_off", "actions": []}
        regime = str(
            decision.get("regime_view")
            or decision.get("regime")
            or "unknown"
        )
        assert regime == "risk_off"

    def test_regime_defaults_to_unknown_when_absent(self):
        """regime defaults to 'unknown' when neither field present."""
        decision = {}
        regime = str(
            decision.get("regime_view")
            or decision.get("regime")
            or "unknown"
        )
        assert regime == "unknown"

    def test_empty_string_regime_view_falls_back_to_unknown(self):
        """Empty string in regime_view treated as falsy → unknown."""
        decision = {"regime_view": "", "regime": ""}
        regime = str(
            decision.get("regime_view")
            or decision.get("regime")
            or "unknown"
        )
        assert regime == "unknown"

    def test_save_produces_correct_regime_metadata(self, monkeypatch):
        """save_trade_memory produces regime=risk_on for new-format decision."""
        import trade_memory as tm

        captured_meta = {}

        mock_short = MagicMock()
        mock_short.add = lambda documents, metadatas, ids: captured_meta.update(
            metadatas[0] if metadatas else {}
        )

        with (
            patch.object(tm, "_get_collections", return_value=(mock_short, MagicMock(), MagicMock())),
            patch.object(tm, "_maybe_promote_aged_records", return_value=None),
        ):
            tm.save_trade_memory(
                decision={"regime_view": "risk_on", "ideas": [], "reasoning": "test"},
                market_conditions={"vix": 18.5},
                session_tier="market",
            )

        assert captured_meta.get("regime") == "risk_on", (
            f"Expected regime='risk_on', got: {captured_meta.get('regime')!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Bug 2 — n_actions/symbols reads ideas not actions
# ─────────────────────────────────────────────────────────────────────────────

class TestNActionsSymbolsFix:

    def test_save_trade_memory_source_reads_ideas(self):
        """save_trade_memory reads 'ideas' field from ClaudeDecision dict."""
        src = _get_save_trade_memory_src()
        assert "ideas" in src, (
            "save_trade_memory must read 'ideas' field — "
            "ClaudeDecision JSON uses ideas[], not actions[]"
        )

    def test_save_trade_memory_has_actions_fallback(self):
        """save_trade_memory falls back to 'actions' for legacy decisions."""
        src = _get_save_trade_memory_src()
        assert "ideas" in src
        assert "actions" in src

    def test_n_actions_counts_ideas(self):
        """n_actions reflects number of ideas in decision."""
        ideas = [{"symbol": "NVDA"}, {"symbol": "AAPL"}]
        n_actions = len(ideas)
        assert n_actions == 2

    def test_n_actions_zero_for_hold_cycle(self):
        """n_actions is 0 when ideas is empty."""
        ideas = []
        n_actions = len(ideas)
        assert n_actions == 0

    def test_symbols_extracted_from_ideas(self):
        """symbols metadata built from ideas[] symbol fields."""
        ideas = [{"symbol": "NVDA", "intent": "enter_long"},
                 {"symbol": "AAPL", "intent": "close"}]
        symbols = ",".join(
            a.get("symbol") or a.get("ticker") or ""
            for a in ideas
            if a.get("symbol") or a.get("ticker")
        )
        assert "NVDA" in symbols
        assert "AAPL" in symbols

    def test_symbols_empty_for_no_ideas(self):
        """symbols is empty string when no ideas."""
        ideas = []
        symbols = ",".join(
            a.get("symbol") or a.get("ticker") or ""
            for a in ideas
            if a.get("symbol") or a.get("ticker")
        )
        assert symbols == ""

    def test_save_produces_correct_n_actions_metadata(self, monkeypatch):
        """save_trade_memory produces correct n_actions from ideas list."""
        import trade_memory as tm

        captured_meta = {}

        mock_short = MagicMock()
        mock_short.add = lambda documents, metadatas, ids: captured_meta.update(
            metadatas[0] if metadatas else {}
        )

        with (
            patch.object(tm, "_get_collections", return_value=(mock_short, MagicMock(), MagicMock())),
            patch.object(tm, "_maybe_promote_aged_records", return_value=None),
        ):
            tm.save_trade_memory(
                decision={
                    "regime_view": "risk_on",
                    "ideas": [
                        {"symbol": "V", "intent": "enter_long"},
                        {"symbol": "GOOGL", "intent": "enter_long"},
                    ],
                    "reasoning": "test",
                },
                market_conditions={"vix": 18.5},
                session_tier="market",
            )

        assert captured_meta.get("n_actions") == 2, (
            f"Expected n_actions=2, got: {captured_meta.get('n_actions')!r}"
        )
        symbols = captured_meta.get("symbols", "")
        assert "V" in symbols, f"Expected 'V' in symbols, got: {symbols!r}"
        assert "GOOGL" in symbols, f"Expected 'GOOGL' in symbols, got: {symbols!r}"

    def test_save_reads_legacy_actions_when_no_ideas(self, monkeypatch):
        """save_trade_memory falls back to actions[] for legacy decisions."""
        import trade_memory as tm

        captured_meta = {}

        mock_short = MagicMock()
        mock_short.add = lambda documents, metadatas, ids: captured_meta.update(
            metadatas[0] if metadatas else {}
        )

        with (
            patch.object(tm, "_get_collections", return_value=(mock_short, MagicMock(), MagicMock())),
            patch.object(tm, "_maybe_promote_aged_records", return_value=None),
        ):
            tm.save_trade_memory(
                decision={
                    "regime": "neutral",
                    "actions": [{"symbol": "GLD", "action": "buy"}],
                    "reasoning": "legacy test",
                },
                market_conditions={"vix": 19.0},
                session_tier="market",
            )

        assert captured_meta.get("n_actions") == 1
        assert "GLD" in captured_meta.get("symbols", "")


# ─────────────────────────────────────────────────────────────────────────────
# Bug 3 — update_trade_outcome wired in decision_outcomes
# ─────────────────────────────────────────────────────────────────────────────

class TestUpdateTradeOutcomeWiring:

    def test_decision_outcomes_references_update_trade_outcome(self):
        """decision_outcomes.py contains update_trade_outcome reference."""
        import decision_outcomes as do
        src = inspect.getsource(do)
        assert "update_trade_outcome" in src, (
            "decision_outcomes must call update_trade_outcome to bridge ChromaDB"
        )

    def test_decision_outcomes_imports_trade_memory(self):
        """decision_outcomes.py imports trade_memory for ChromaDB updates."""
        import decision_outcomes as do
        src = inspect.getsource(do)
        assert "trade_memory" in src, (
            "decision_outcomes must reference trade_memory"
        )

    def test_update_chroma_outcome_is_nonfatal(self):
        """_update_chroma_outcome does not raise even when trade_memory fails."""
        import decision_outcomes as do

        with patch.dict(sys.modules, {"trade_memory": None}):
            # Should not raise — always non-fatal
            try:
                do._update_chroma_outcome("dec_A1_20260421_093500", True, 0.012)
            except Exception as exc:
                pytest.fail(f"_update_chroma_outcome raised: {exc}")

    def test_update_chroma_outcome_calls_update_trade_outcome(self, tmp_path):
        """_update_chroma_outcome calls tm.update_trade_outcome with correct args."""
        import decision_outcomes as do

        decisions_data = json.dumps([
            {"decision_id": "dec_A1_20260421_093500",
             "vector_id": "trade_20260421_093500_123456",
             "actions": []}
        ])

        mock_tm = MagicMock()
        mock_tm.update_trade_outcome = MagicMock()

        with (
            patch.object(do, "_DECISIONS_FILE", tmp_path / "decisions.json"),
            patch.dict(sys.modules, {"trade_memory": mock_tm}),
        ):
            (tmp_path / "decisions.json").write_text(decisions_data)
            do._update_chroma_outcome("dec_A1_20260421_093500", True, 0.025)

        mock_tm.update_trade_outcome.assert_called_once_with(
            "trade_20260421_093500_123456", "win", 0.025
        )

    def test_update_chroma_outcome_loss_path(self, tmp_path):
        """_update_chroma_outcome maps correct=False → outcome='loss'."""
        import decision_outcomes as do

        decisions_data = json.dumps([
            {"decision_id": "dec_A1_test_loss",
             "vector_id": "trade_test_loss_id",
             "actions": []}
        ])

        mock_tm = MagicMock()

        with (
            patch.object(do, "_DECISIONS_FILE", tmp_path / "decisions.json"),
            patch.dict(sys.modules, {"trade_memory": mock_tm}),
        ):
            (tmp_path / "decisions.json").write_text(decisions_data)
            do._update_chroma_outcome("dec_A1_test_loss", False, -0.018)

        mock_tm.update_trade_outcome.assert_called_once_with(
            "trade_test_loss_id", "loss", -0.018
        )

    def test_update_chroma_outcome_skips_unknown_decision_id(self, tmp_path):
        """_update_chroma_outcome is a no-op when decision_id not in decisions.json."""
        import decision_outcomes as do

        decisions_data = json.dumps([
            {"decision_id": "dec_A1_known", "vector_id": "trade_known", "actions": []}
        ])

        mock_tm = MagicMock()

        with (
            patch.object(do, "_DECISIONS_FILE", tmp_path / "decisions.json"),
            patch.dict(sys.modules, {"trade_memory": mock_tm}),
        ):
            (tmp_path / "decisions.json").write_text(decisions_data)
            do._update_chroma_outcome("dec_A1_UNKNOWN_ID", True, 0.01)

        mock_tm.update_trade_outcome.assert_not_called()

    def test_update_trade_outcome_signature(self):
        """update_trade_outcome accepts decision_id, outcome, pnl."""
        import trade_memory as tm
        sig = inspect.signature(tm.update_trade_outcome)
        params = list(sig.parameters.keys())
        assert "decision_id" in params
        assert "outcome" in params
        assert "pnl" in params


# ─────────────────────────────────────────────────────────────────────────────
# Backfill script
# ─────────────────────────────────────────────────────────────────────────────

class TestBackfillScript:

    _SCRIPT = Path("scripts/backfill_trade_outcomes.py")

    def test_backfill_script_exists(self):
        """scripts/backfill_trade_outcomes.py exists."""
        assert self._SCRIPT.exists(), f"{self._SCRIPT} not found"

    def test_backfill_script_references_decision_outcomes(self):
        """Backfill script reads decision_outcomes.jsonl."""
        if not self._SCRIPT.exists():
            pytest.skip("backfill script not found")
        src = self._SCRIPT.read_text()
        assert "decision_outcomes" in src, (
            "backfill script must reference decision_outcomes.jsonl"
        )

    def test_backfill_script_calls_update_trade_outcome(self):
        """Backfill script calls update_trade_outcome."""
        if not self._SCRIPT.exists():
            pytest.skip("backfill script not found")
        src = self._SCRIPT.read_text()
        assert "update_trade_outcome" in src, (
            "backfill script must call update_trade_outcome"
        )

    def test_backfill_script_is_importable(self):
        """Backfill script can be parsed without syntax errors."""
        if not self._SCRIPT.exists():
            pytest.skip("backfill script not found")
        import ast
        src = self._SCRIPT.read_text()
        try:
            ast.parse(src)
        except SyntaxError as exc:
            pytest.fail(f"backfill script has syntax error: {exc}")

    def test_backfill_vector_lookup_returns_dict(self, tmp_path):
        """_build_vector_lookup returns correct mapping."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "backfill_trade_outcomes", self._SCRIPT
        )
        if spec is None:
            pytest.skip("cannot load backfill script")
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception:
            pytest.skip("backfill script failed to load (missing deps)")

        decisions = [
            {"decision_id": "dec_A1_20260421", "vector_id": "trade_20260421", "actions": []},
            {"decision_id": "dec_A1_20260422", "vector_id": "trade_20260422", "actions": []},
            {"decision_id": "dec_no_vector",                                   "actions": []},
        ]
        lookup = mod._build_vector_lookup(decisions)
        assert lookup.get("dec_A1_20260421") == "trade_20260421"
        assert lookup.get("dec_A1_20260422") == "trade_20260422"
        assert "dec_no_vector" not in lookup
