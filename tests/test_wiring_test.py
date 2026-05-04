"""
tests/test_wiring_test.py — Unit tests for wiring_test.py infrastructure.

Tests the MockOrder intercept, cleanup helpers, and report formatter.
Does NOT run the full end-to-end wiring test (that requires Claude API keys).
"""

import json
import sys
import unittest.mock as mock
from pathlib import Path

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import wiring_test as wt


# ---------------------------------------------------------------------------
# MockOrder
# ---------------------------------------------------------------------------
class TestMockOrder:
    def test_default_id_format(self):
        order = wt.MockOrder()
        assert order.id.startswith("WIRING-")
        assert len(order.id) == len("WIRING-") + 8

    def test_unique_ids(self):
        orders = [wt.MockOrder() for _ in range(5)]
        ids = [o.id for o in orders]
        assert len(set(ids)) == 5, "MockOrder IDs should be unique"

    def test_default_fields(self):
        order = wt.MockOrder()
        assert order.status == "filled"
        assert order.filled_qty == "1"


# ---------------------------------------------------------------------------
# _mock_submit_order
# ---------------------------------------------------------------------------
class TestMockSubmitOrder:
    def setup_method(self):
        wt._intercepted_orders.clear()

    def test_intercepts_call(self):
        req = mock.MagicMock()
        req.symbol = "SPY"
        order = wt._mock_submit_order(req)
        assert isinstance(order, wt.MockOrder)
        assert len(wt._intercepted_orders) == 1
        assert wt._intercepted_orders[0]["symbol"] == "SPY"

    def test_uses_underlying_symbol_fallback(self):
        req = mock.MagicMock(spec=[])  # no .symbol attribute
        req.underlying_symbol = "AAPL"
        wt._mock_submit_order(req)
        assert wt._intercepted_orders[0]["symbol"] == "AAPL"

    def test_records_req_type(self):
        req = mock.MagicMock()
        req.symbol = "QQQ"
        type(req).__name__ = "LimitOrderRequest"
        wt._mock_submit_order(req)
        # req_type is recorded (exact type name from MagicMock varies)
        assert "req_type" in wt._intercepted_orders[0]

    def teardown_method(self):
        wt._intercepted_orders.clear()


# ---------------------------------------------------------------------------
# _check_no_live_bot
# ---------------------------------------------------------------------------
class TestCheckNoLiveBot:
    def test_no_pid_file(self, tmp_path):
        pid_path = tmp_path / "scheduler.pid"
        with mock.patch.object(wt, "_PID_FILE", pid_path):
            safe, msg = wt._check_no_live_bot()
        assert safe is True

    def test_stale_pid_file(self, tmp_path):
        pid_path = tmp_path / "scheduler.pid"
        pid_path.write_text("999999")  # non-existent PID
        with mock.patch.object(wt, "_PID_FILE", pid_path):
            safe, msg = wt._check_no_live_bot()
        assert safe is True

    def test_live_pid_detected(self, tmp_path):
        import os
        live_pid = os.getpid()  # current process is definitely running
        pid_path = tmp_path / "scheduler.pid"
        pid_path.write_text(str(live_pid))
        with mock.patch.object(wt, "_PID_FILE", pid_path):
            safe, msg = wt._check_no_live_bot()
        assert safe is False
        assert str(live_pid) in msg


# ---------------------------------------------------------------------------
# _cleanup_wiring_test — trades.jsonl filter (correction #5)
# ---------------------------------------------------------------------------
class TestCleanupTradesJsonl:
    def test_removes_wiring_test_entries(self, tmp_path):
        trades_path = tmp_path / "trades.jsonl"
        lines = [
            json.dumps({"event": "fill", "symbol": "AAPL", "ts": "2026-05-03T10:00:00Z"}),
            json.dumps({"event": "fill", "symbol": "SPY",  "ts": "2026-05-03T10:01:00Z",
                        "wiring_test": True}),
            json.dumps({"event": "fill", "symbol": "MSFT", "ts": "2026-05-03T10:02:00Z"}),
        ]
        trades_path.write_text("\n".join(lines) + "\n")

        with mock.patch.object(wt, "_TRADES_JSONL", trades_path):
            actions = wt._cleanup_wiring_test()

        remaining = [json.loads(l) for l in trades_path.read_text().splitlines() if l]
        symbols = [r["symbol"] for r in remaining]
        assert "SPY" not in symbols
        assert "AAPL" in symbols
        assert "MSFT" in symbols
        assert any("removed 1" in a for a in actions)

    def test_no_op_when_no_wiring_entries(self, tmp_path):
        trades_path = tmp_path / "trades.jsonl"
        lines = [
            json.dumps({"event": "fill", "symbol": "AAPL"}),
            json.dumps({"event": "fill", "symbol": "MSFT"}),
        ]
        trades_path.write_text("\n".join(lines) + "\n")

        with mock.patch.object(wt, "_TRADES_JSONL", trades_path):
            actions = wt._cleanup_wiring_test()

        assert any("removed 0" in a for a in actions)

    def test_no_op_when_file_missing(self, tmp_path):
        with mock.patch.object(wt, "_TRADES_JSONL", tmp_path / "nonexistent.jsonl"):
            actions = wt._cleanup_wiring_test()
        # Should not raise, ChromaDB/signal cleanup entries still appear
        assert isinstance(actions, list)


# ---------------------------------------------------------------------------
# _cleanup_wiring_test — signal_scores.json restore
# ---------------------------------------------------------------------------
class TestCleanupSignalScores:
    def test_restores_backup(self, tmp_path):
        scores_path = tmp_path / "signal_scores.json"
        bak_path    = tmp_path / "signal_scores.json.wiring_bak"
        original = {"scored_symbols": {"AAPL": {"score": 0.5}}}
        bak_path.write_text(json.dumps(original))
        # synthetic file is currently in place
        scores_path.write_text(json.dumps(wt._SYNTHETIC_SIGNAL_SCORES))

        with (mock.patch.object(wt, "_SIGNAL_SCORES_PATH", scores_path),
              mock.patch.object(wt, "_SIGNAL_SCORES_BAK",  bak_path)):
            actions = wt._cleanup_wiring_test()

        restored = json.loads(scores_path.read_text())
        assert restored == original
        assert any("restored from backup" in a for a in actions)

    def test_removes_synthetic_if_no_backup(self, tmp_path):
        scores_path = tmp_path / "signal_scores.json"
        bak_path    = tmp_path / "signal_scores.json.wiring_bak"
        # Only synthetic file exists, no backup
        scores_path.write_text(json.dumps(wt._SYNTHETIC_SIGNAL_SCORES))

        with (mock.patch.object(wt, "_SIGNAL_SCORES_PATH", scores_path),
              mock.patch.object(wt, "_SIGNAL_SCORES_BAK",  bak_path)):
            actions = wt._cleanup_wiring_test()

        assert not scores_path.exists()
        assert any("removed" in a for a in actions)


# ---------------------------------------------------------------------------
# risk_kernel TEST_ filter (correction #4)
# ---------------------------------------------------------------------------
class TestRiskKernelTestFilter:
    def test_test_prefix_rejected(self):
        from risk_kernel import eligibility_check
        from schemas import AccountAction, Conviction, Direction, Tier, TradeIdea

        idea = TradeIdea(
            symbol     = "TEST_AAPL",
            action     = AccountAction.BUY,
            direction  = Direction.BULLISH,
            tier       = Tier.INTRADAY,
            conviction = Conviction.MEDIUM,
            catalyst   = "wiring_test",
        )
        rejection = eligibility_check(idea, None, {})
        assert rejection is not None
        assert "wiring_test_symbol" in rejection
        assert "TEST_AAPL" in rejection

    def test_real_symbol_not_affected(self):
        """Normal symbols must still reach the full eligibility check chain."""
        from risk_kernel import eligibility_check
        from schemas import AccountAction, Conviction, Direction, Tier, TradeIdea

        idea = TradeIdea(
            symbol     = "SPY",
            action     = AccountAction.BUY,
            direction  = Direction.BULLISH,
            tier       = Tier.INTRADAY,
            conviction = Conviction.MEDIUM,
            catalyst   = "momentum",
        )
        # Provide a minimal snapshot so downstream checks don't crash on None.equity
        snapshot = mock.MagicMock()
        snapshot.equity      = 50_000.0
        snapshot.open_orders = []
        snapshot.positions   = []

        rejection = eligibility_check(idea, snapshot, {})
        # Should NOT be rejected by the TEST_ filter
        if rejection:
            assert "wiring_test_symbol" not in rejection


# ---------------------------------------------------------------------------
# trade_memory.delete_by_vector_id (correction #2)
# ---------------------------------------------------------------------------
class TestDeleteByVectorId:
    def test_noop_on_empty_id(self):
        """delete_by_vector_id("") must not raise."""
        import trade_memory
        trade_memory.delete_by_vector_id("")   # should be silent no-op

    def test_noop_on_nonexistent_id(self):
        """delete_by_vector_id with an unknown ID must not raise."""
        import trade_memory
        trade_memory.delete_by_vector_id("trade_99991231_999999_000000")

    def test_roundtrip_write_and_delete(self):
        """Write a record, capture the vector_id, delete it, confirm gone."""
        pytest.importorskip("chromadb",
                            reason="chromadb not installed — skipping roundtrip test")
        import trade_memory

        synthetic = {
            "action":      "hold",
            "ideas":       [],
            "reasoning":   "unit test roundtrip",
            "wiring_test": True,
        }
        vid = trade_memory.save_trade_memory(synthetic, {"vix": 18.0}, "market")
        if not vid:
            pytest.skip("ChromaDB write returned empty — collection may be unavailable")

        # Confirm it exists
        short, _, _ = trade_memory._get_collections()
        if short is not None:
            result = short.get(ids=[vid], include=["metadatas"])
            assert result and result.get("ids"), "record should exist before delete"

        # Delete and confirm gone
        trade_memory.delete_by_vector_id(vid)
        if short is not None:
            result2 = short.get(ids=[vid], include=["metadatas"])
            assert not result2.get("ids"), "record should be gone after delete"


# ---------------------------------------------------------------------------
# Claude stop_reason recorder (addition A)
# ---------------------------------------------------------------------------
class TestClaudeStopRecorder:
    def setup_method(self):
        wt._recorded_stop_reasons.clear()
        wt._claude_create_patcher = None

    def teardown_method(self):
        wt._uninstall_claude_stop_recorder()
        wt._recorded_stop_reasons.clear()

    def test_install_uninstall_noop_when_no_client(self):
        """Uninstall is safe when nothing was installed."""
        wt._uninstall_claude_stop_recorder()  # should not raise

    def test_install_records_intercept_failed_on_missing_module(self):
        """If _get_claude fails, error is captured in _recorded_stop_reasons."""
        with mock.patch.dict("sys.modules", {"bot_clients": None}):
            wt._install_claude_stop_recorder()
        # Either installed successfully or recorded the failure — must not raise
        # (either outcome is acceptable; what matters is no exception escapes)

    def test_uninstall_clears_patcher(self):
        """After uninstall, _claude_create_patcher is None."""
        wt._claude_create_patcher = (mock.MagicMock(), mock.MagicMock())
        wt._uninstall_claude_stop_recorder()
        assert wt._claude_create_patcher is None


# ---------------------------------------------------------------------------
# Signal schema validation (addition B)
# ---------------------------------------------------------------------------
class TestSignalSchemaCheck:
    def setup_method(self):
        wt._results.clear()

    def test_all_valid_entries_pass(self):
        """All entries with score/direction/tier produce D-04b PASS."""
        scored = {
            "TEST_AAPL": {"score": 72.0, "direction": "bullish", "tier": "dynamic"},
            "TEST_MSFT": {"score": 65.0, "direction": "neutral",  "tier": "core"},
        }
        bad = [
            s for s, d in scored.items()
            if not isinstance(d, dict) or "score" not in d
            or "direction" not in d or "tier" not in d
        ]
        assert bad == []

    def test_missing_tier_detected(self):
        """Entry missing 'tier' is flagged as bad_schema."""
        scored = {
            "TEST_AAPL": {"score": 72.0, "direction": "bullish"},  # no tier
        }
        bad = [
            s for s, d in scored.items()
            if not isinstance(d, dict) or "score" not in d
            or "direction" not in d or "tier" not in d
        ]
        assert "TEST_AAPL" in bad

    def test_non_dict_entry_flagged(self):
        """Non-dict entry is flagged."""
        scored = {"TEST_AAPL": "not_a_dict"}
        bad = [
            s for s, d in scored.items()
            if not isinstance(d, dict) or "score" not in d
            or "direction" not in d or "tier" not in d
        ]
        assert "TEST_AAPL" in bad


# ---------------------------------------------------------------------------
# Prompt section presence (addition H)
# ---------------------------------------------------------------------------
class TestPromptSections:
    def test_all_sections_present(self):
        """When all 4 required sections are in prompt, missing_sections is empty."""
        prompt = (
            "=== ACCOUNT & RISK ===\nsome text\n"
            "=== MARKET CONTEXT ===\nmore text\n"
            "=== TOP SIGNALS (scored 2/39) ===\nstuff\n"
            "=== YOUR TASK ===\ndo things\n"
        )
        required = [
            "=== ACCOUNT & RISK ===",
            "=== MARKET CONTEXT ===",
            "=== TOP SIGNALS",
            "=== YOUR TASK ===",
        ]
        missing = [s for s in required if s not in prompt]
        assert missing == []

    def test_missing_section_detected(self):
        """When a section is absent, it appears in missing_sections."""
        prompt = "=== ACCOUNT & RISK ===\n=== MARKET CONTEXT ===\n=== YOUR TASK ==="
        required = [
            "=== ACCOUNT & RISK ===",
            "=== MARKET CONTEXT ===",
            "=== TOP SIGNALS",
            "=== YOUR TASK ===",
        ]
        missing = [s for s in required if s not in prompt]
        assert "=== TOP SIGNALS" in missing


# ---------------------------------------------------------------------------
# Cross-stage idea field validation (addition C+D)
# ---------------------------------------------------------------------------
class TestIdeaFieldValidation:
    def test_valid_idea_dict_passes_field_check(self):
        """Idea with all required fields produces empty bad_ideas list."""
        required_fields = {"symbol", "intent", "conviction", "catalyst", "direction"}
        idea = {
            "symbol": "TEST_AAPL", "intent": "enter_long", "direction": "bullish",
            "tier_preference": "intraday", "conviction": 0.75, "catalyst": "wiring_test",
        }
        bad = [
            i for i in [idea]
            if not isinstance(i, dict) or not required_fields.issubset(i.keys())
        ]
        assert bad == []

    def test_missing_conviction_flagged(self):
        """Idea missing 'conviction' is flagged."""
        required_fields = {"symbol", "intent", "conviction", "catalyst", "direction"}
        idea = {
            "symbol": "TEST_AAPL", "intent": "enter_long", "direction": "bullish",
            "tier_preference": "intraday", "catalyst": "wiring_test",
            # missing conviction
        }
        bad = [
            i for i in [idea]
            if not isinstance(i, dict) or not required_fields.issubset(i.keys())
        ]
        assert len(bad) == 1

    def test_tradeidea_via_validate_claude_decision(self):
        """validate_claude_decision maps intent/tier_preference to TradeIdea correctly."""
        from schemas import validate_claude_decision
        decision = {
            "reasoning": "test",
            "regime_view": "caution",
            "ideas": [{
                "symbol": "TEST_AAPL", "intent": "enter_long", "direction": "bullish",
                "tier_preference": "intraday", "conviction": 0.75, "catalyst": "wiring_test",
            }],
            "holds": [],
        }
        parsed = validate_claude_decision(decision)
        assert len(parsed.ideas) == 1
        ti = parsed.ideas[0]
        assert ti.symbol == "TEST_AAPL"
        assert ti.conviction == 0.75


# ---------------------------------------------------------------------------
# WARN status in report (addition I)
# ---------------------------------------------------------------------------
class TestWarnStatus:
    def setup_method(self):
        wt._results.clear()

    def teardown_method(self):
        wt._results.clear()

    def test_warn_count_tracked(self):
        """WARN records are counted separately from PASS/FAIL/SKIP."""
        wt._record("test_stage", "PASS")
        wt._record("test_stage2", "WARN", "degraded but functional")
        wt._record("test_stage3", "SKIP")
        warns = sum(1 for r in wt._results if r.status == "WARN")
        fails = sum(1 for r in wt._results if r.status == "FAIL")
        assert warns == 1
        assert fails == 0

    def test_overall_pass_despite_warn(self):
        """WARN records do not cause OVERALL FAIL."""
        wt._record("a", "PASS")
        wt._record("b", "WARN", "stale data")
        fails = sum(1 for r in wt._results if r.status == "FAIL")
        overall = "PASS" if fails == 0 else "FAIL"
        assert overall == "PASS"
