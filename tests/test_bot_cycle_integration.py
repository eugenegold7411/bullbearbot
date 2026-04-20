"""
test_bot_cycle_integration.py — Integration tests for the bot.py split.

Verifies:
  1. Import graph — all stage modules importable; no circular deps.
  2. Stage delegation — run_cycle() calls the expected stage functions.
  3. PreCycleState shape — run_precycle returns the right dataclass.
  4. Log sequence — cycle emits substrings matching golden_logs/cycle_baseline.log.
  5. None propagation — run_precycle returning None aborts run_cycle.
  6. Re-exports — bot.ask_claude and bot._ask_claude_overnight are reachable.
"""

import importlib
import inspect
import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _golden_lines() -> list[str]:
    """Return non-comment, non-empty lines from cycle_baseline.log."""
    log_path = Path(__file__).parent / "golden_logs" / "cycle_baseline.log"
    lines = []
    for raw in log_path.read_text().splitlines():
        stripped = raw.strip()
        if stripped and not stripped.startswith("#"):
            lines.append(stripped)
    return lines


# ---------------------------------------------------------------------------
# 1. Import graph
# ---------------------------------------------------------------------------

class TestImportGraph:
    def test_bot_clients_importable(self):
        mod = importlib.import_module("bot_clients")
        assert hasattr(mod, "_get_alpaca")
        assert hasattr(mod, "_get_claude")
        assert hasattr(mod, "MODEL")
        assert hasattr(mod, "MODEL_FAST")

    def test_stage0_importable(self):
        mod = importlib.import_module("bot_stage0_precycle")
        assert hasattr(mod, "run_precycle")
        assert hasattr(mod, "PreCycleState")

    def test_stage1_importable(self):
        mod = importlib.import_module("bot_stage1_regime")
        assert hasattr(mod, "classify_regime")
        assert hasattr(mod, "format_regime_summary")

    def test_stage2_importable(self):
        mod = importlib.import_module("bot_stage2_signal")
        assert hasattr(mod, "score_signals")
        assert hasattr(mod, "format_signal_scores")

    def test_stage2_5_importable(self):
        mod = importlib.import_module("bot_stage2_5_scratchpad")
        assert hasattr(mod, "run_scratchpad_stage")

    def test_stage3_importable(self):
        mod = importlib.import_module("bot_stage3_decision")
        assert hasattr(mod, "ask_claude")
        assert hasattr(mod, "_ask_claude_overnight")
        assert hasattr(mod, "build_user_prompt")
        assert hasattr(mod, "build_compact_prompt")

    def test_stage4_importable(self):
        mod = importlib.import_module("bot_stage4_execution")
        assert hasattr(mod, "debate_trade")
        assert hasattr(mod, "fundamental_check")

    def test_bot_importable(self):
        mod = importlib.import_module("bot")
        assert hasattr(mod, "run_cycle")

    def test_no_circular_import(self):
        """Importing all stage modules in dependency order must not raise."""
        for name in [
            "bot_clients",
            "bot_stage1_regime",
            "bot_stage2_signal",
            "bot_stage2_5_scratchpad",
            "bot_stage4_execution",
            "bot_stage3_decision",
            "bot_stage0_precycle",
            "bot",
        ]:
            importlib.import_module(name)  # would raise on circular import


# ---------------------------------------------------------------------------
# 2. Stage delegation — run_cycle calls expected stage functions
# ---------------------------------------------------------------------------

class TestStageDelegation:
    """
    Verify that run_cycle() delegates to each stage function.
    We patch the stage functions at the bot module level (where they're imported)
    so we can confirm delegation without running actual API calls.
    """

    def _make_precycle_state(self) -> Any:
        from bot_stage0_precycle import PreCycleState
        md: dict = {
            "market_status": "open",
            "vix": 18.5,
            "time_et": "10:00",
            "vix_regime": "normal",
            "current_prices": {},
            "breaking_news": "",
            "macro_wire_section": "",
            "minutes_since_open": 30,
            "crypto_signals": "",
            "morning_brief_section": "",
            "insider_section": "",
            "reddit_section": "",
            "earnings_intel_section": "",
            "eth_btc": {},
        }
        return PreCycleState(
            account=MagicMock(equity="100000", cash="80000", buying_power="180000"),
            positions=[],
            equity=100_000.0,
            cash=80_000.0,
            buying_power_float=180_000.0,
            long_val=0.0,
            exposure=0.0,
            allow_live_orders=True,
            allow_new_entries=True,
            pf_result=MagicMock(verdict="go", blockers=[], warnings=[]),
            wl={"stocks": [], "etfs": [], "crypto": []},
            symbols_stock=[],
            symbols_crypto=[],
            md=md,
            crypto_context="",
            cfg={},
            recent_decisions="",
            ticker_lessons="",
            vector_memories="",
            similar_scenarios=[],
            strategy_config_note="",
            pi_data={},
            recon_log=[],
            recon_diff=None,
            snapshot=None,
            a1_mode=None,
            div_events=[],
            exit_status_str="",
        )

    def test_run_cycle_calls_run_precycle(self):
        import bot
        fake_state = self._make_precycle_state()
        gate_state = MagicMock()
        gate_state.consecutive_skips = 0

        with (
            patch.object(bot, "run_precycle", return_value=fake_state) as mock_pre,
            patch.object(bot, "_check_drawdown", return_value=False),
            patch.object(bot, "classify_regime", return_value={}),
            patch.object(bot, "score_signals", return_value={}),
            patch.object(bot, "run_scratchpad_stage", return_value={}),
            patch.object(bot, "ask_claude", return_value={"reasoning": "test", "regime_view": "neutral", "ideas": [], "holds": [], "notes": "", "concerns": ""}),
            patch("bot._gate.load_gate_state", return_value=gate_state),
            patch("bot._gate.should_run_sonnet", return_value=(False, [], gate_state)),
            patch("bot._gate.save_gate_state"),
            patch("bot.trade_memory.save_trade_memory", return_value=None),
            patch("bot.mem.save_decision"),
            patch("bot.wm.run_feedback_loop"),
            patch("bot.log_trade"),
        ):
            bot.run_cycle(session_tier="market")
            mock_pre.assert_called_once()

    def test_run_cycle_calls_classify_regime_for_market(self):
        import bot
        fake_state = self._make_precycle_state()
        gate_state = MagicMock()
        gate_state.consecutive_skips = 0

        with (
            patch.object(bot, "run_precycle", return_value=fake_state),
            patch.object(bot, "_check_drawdown", return_value=False),
            patch.object(bot, "classify_regime", return_value={}) as mock_regime,
            patch.object(bot, "score_signals", return_value={}),
            patch.object(bot, "run_scratchpad_stage", return_value={}),
            patch("bot._gate.load_gate_state", return_value=gate_state),
            patch("bot._gate.should_run_sonnet", return_value=(False, [], gate_state)),
            patch("bot._gate.save_gate_state"),
            patch("bot.trade_memory.save_trade_memory", return_value=None),
            patch("bot.mem.save_decision"),
            patch("bot.wm.run_feedback_loop"),
            patch("bot.log_trade"),
        ):
            bot.run_cycle(session_tier="market")
            mock_regime.assert_called_once()

    def test_run_cycle_skips_classify_regime_for_overnight(self):
        import bot
        fake_state = self._make_precycle_state()

        with (
            patch.object(bot, "run_precycle", return_value=fake_state),
            patch.object(bot, "_check_drawdown", return_value=False),
            patch.object(bot, "classify_regime", return_value={}) as mock_regime,
            patch.object(bot, "score_signals", return_value={}),
            patch.object(bot, "_ask_claude_overnight", return_value={"reasoning": "overnight", "regime": "normal", "actions": [], "notes": ""}),
            patch("bot.trade_memory.save_trade_memory", return_value=None),
            patch("bot.mem.save_decision"),
            patch("bot.wm.run_feedback_loop"),
            patch("bot.log_trade"),
        ):
            bot.run_cycle(session_tier="overnight")
            mock_regime.assert_not_called()

    def test_run_cycle_halts_on_none_precycle(self):
        import bot
        with (
            patch.object(bot, "run_precycle", return_value=None),
            patch.object(bot, "_check_drawdown") as mock_dd,
        ):
            bot.run_cycle(session_tier="market")
            mock_dd.assert_not_called()

    def test_drawdown_halt_prevents_stage1(self):
        import bot
        fake_state = self._make_precycle_state()
        with (
            patch.object(bot, "run_precycle", return_value=fake_state),
            patch.object(bot, "_check_drawdown", return_value=True),
            patch.object(bot, "classify_regime") as mock_regime,
        ):
            bot.run_cycle(session_tier="market")
            mock_regime.assert_not_called()


# ---------------------------------------------------------------------------
# 3. PreCycleState shape
# ---------------------------------------------------------------------------

class TestPreCycleStateShape:
    REQUIRED_FIELDS = [
        "account", "positions", "equity", "cash", "buying_power_float",
        "long_val", "exposure", "allow_live_orders", "allow_new_entries",
        "pf_result", "wl", "symbols_stock", "symbols_crypto", "md",
        "crypto_context", "cfg", "recent_decisions", "ticker_lessons",
        "vector_memories", "similar_scenarios", "strategy_config_note",
        "pi_data", "recon_log", "recon_diff", "snapshot",
        "a1_mode", "div_events", "exit_status_str",
    ]

    def test_dataclass_has_all_fields(self):
        import dataclasses

        from bot_stage0_precycle import PreCycleState
        field_names = {f.name for f in dataclasses.fields(PreCycleState)}
        for name in self.REQUIRED_FIELDS:
            assert name in field_names, f"PreCycleState missing field: {name}"

    def test_div_events_default_empty_list(self):
        import dataclasses

        from bot_stage0_precycle import PreCycleState
        fields = {f.name: f for f in dataclasses.fields(PreCycleState)}
        assert fields["div_events"].default_factory is list  # type: ignore[attr-defined]

    def test_exit_status_str_default(self):
        import dataclasses

        from bot_stage0_precycle import PreCycleState
        fields = {f.name: f for f in dataclasses.fields(PreCycleState)}
        assert fields["exit_status_str"].default == "  (unavailable)"


# ---------------------------------------------------------------------------
# 4. Re-exports — test_core.py compat
# ---------------------------------------------------------------------------

class TestReExports:
    def test_bot_has_ask_claude(self):
        import bot
        assert callable(bot.ask_claude)

    def test_bot_has_ask_claude_overnight(self):
        import bot
        assert callable(bot._ask_claude_overnight)

    def test_ask_claude_source_contains_model(self):
        """inspect.getsource follows the re-export to bot_stage3_decision where MODEL is used."""
        import bot
        src = inspect.getsource(bot._ask_claude_overnight)
        assert "MODEL" in src or "claude" in src.lower()

    def test_ask_claude_overnight_callable_from_stage3(self):
        from bot_stage3_decision import _ask_claude_overnight
        assert callable(_ask_claude_overnight)


# ---------------------------------------------------------------------------
# 5. Policy gate — PDT_FLOOR comment present
# ---------------------------------------------------------------------------

class TestPolicyGateComment:
    def test_pdt_floor_comment_in_debate_trade(self):
        import bot_stage4_execution
        src = inspect.getsource(bot_stage4_execution.debate_trade)
        assert "PDT_FLOOR" in src, "debate_trade must carry PDT_FLOOR comment"
        assert "policy_leakage_findings" in src, "must reference policy_leakage_findings.md"

    def test_debate_trade_skips_at_pdt_floor(self):
        from bot_stage4_execution import debate_trade
        result = debate_trade(
            action={"action": "buy", "symbol": "NVDA", "catalyst": "test", "confidence": "high"},
            md={"vix": 18, "vix_regime": "normal", "market_status": "open",
                "breaking_news": "", "intermarket_signals": ""},
            equity=26_000.0,
            session_tier="market",
        )
        assert result["proceed"] is True

    def test_debate_trade_runs_above_pdt_floor(self):
        """Above the floor + correct session + high confidence → debate fires (3 API calls)."""
        from bot_stage4_execution import debate_trade
        fake_resp = MagicMock()
        fake_resp.content = [MagicMock(text="bullet\n- point")]
        synth_resp = MagicMock()
        synth_resp.content = [MagicMock(text='{"proceed":true,"veto_reason":"","synthesis":"ok","conviction_adjustment":"maintain"}')]

        with patch("bot_stage4_execution._get_claude") as mock_claude:
            mock_claude.return_value.messages.create.side_effect = [
                fake_resp,   # bull
                fake_resp,   # bear
                synth_resp,  # synthesis
            ]
            result = debate_trade(
                action={"action": "buy", "symbol": "NVDA", "catalyst": "test", "confidence": "high"},
                md={"vix": 18, "vix_regime": "normal", "market_status": "open",
                    "breaking_news": "", "intermarket_signals": ""},
                equity=50_000.0,
                session_tier="market",
            )
        assert result["proceed"] is True
        assert mock_claude.return_value.messages.create.call_count == 3


# ---------------------------------------------------------------------------
# 6. Golden log sequence
# ---------------------------------------------------------------------------

class TestGoldenLogSequence:
    def test_golden_log_file_exists(self):
        path = Path(__file__).parent / "golden_logs" / "cycle_baseline.log"
        assert path.exists(), "golden_logs/cycle_baseline.log must exist"

    def test_golden_log_has_required_substrings(self):
        lines = _golden_lines()
        assert len(lines) >= 5, "baseline log must have at least 5 non-comment lines"
        required = {"Account", "[SCHEMA]", "── Cycle done in"}
        for req in required:
            assert any(req in line for line in lines), (
                f"golden log missing required substring: {req!r}"
            )

    def test_run_cycle_emits_golden_substrings(self, caplog):
        """
        Run a fully mocked cycle and verify the log output contains
        a representative subset of golden-log substrings.
        """
        import bot
        from bot_stage0_precycle import PreCycleState

        md: dict = {
            "market_status": "open", "vix": 18.5, "time_et": "10:00",
            "vix_regime": "normal", "current_prices": {}, "breaking_news": "",
            "macro_wire_section": "", "minutes_since_open": 30,
            "crypto_signals": "", "morning_brief_section": "",
            "insider_section": "", "reddit_section": "", "earnings_intel_section": "",
            "eth_btc": {},
        }
        fake_state = PreCycleState(
            account=MagicMock(equity="100000", cash="80000", buying_power="180000"),
            positions=[], equity=100_000.0, cash=80_000.0,
            buying_power_float=180_000.0, long_val=0.0, exposure=0.0,
            allow_live_orders=True, allow_new_entries=True,
            pf_result=MagicMock(verdict="go", blockers=[], warnings=[]),
            wl={"stocks": [], "etfs": [], "crypto": []},
            symbols_stock=[], symbols_crypto=[], md=md,
            crypto_context="", cfg={}, recent_decisions="",
            ticker_lessons="", vector_memories="", similar_scenarios=[],
            strategy_config_note="", pi_data={}, recon_log=[],
            recon_diff=None, snapshot=None, a1_mode=None,
            div_events=[], exit_status_str="",
        )
        gate_state = MagicMock()
        gate_state.consecutive_skips = 0

        with caplog.at_level(logging.INFO, logger="bot"):
            with (
                patch.object(bot, "run_precycle", return_value=fake_state),
                patch.object(bot, "_check_drawdown", return_value=False),
                patch.object(bot, "classify_regime", return_value={"regime_score": 60, "bias": "neutral", "session_theme": "test", "constraints": [], "confidence": "medium"}),
                patch.object(bot, "score_signals", return_value={"top_3": ["SPY"], "elevated_caution": [], "reasoning": "ok", "scored_symbols": {}}),
                patch.object(bot, "run_scratchpad_stage", return_value={}),
                patch("bot._gate.load_gate_state", return_value=gate_state),
                patch("bot._gate.should_run_sonnet", return_value=(False, [], gate_state)),
                patch("bot._gate.save_gate_state"),
                patch("bot.trade_memory.save_trade_memory", return_value=None),
                patch("bot.mem.save_decision"),
                patch("bot.wm.run_feedback_loop"),
                patch("bot.log_trade"),
            ):
                bot.run_cycle(session_tier="market")

        all_log = "\n".join(r.message for r in caplog.records)
        # Check a representative subset from the golden log
        for substring in ["[SCHEMA]", "── Cycle done in"]:
            assert substring in all_log, (
                f"run_cycle log did not contain expected substring: {substring!r}\n"
                f"Captured log:\n{all_log[:1000]}"
            )
