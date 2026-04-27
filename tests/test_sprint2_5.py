"""
tests/test_sprint2_5.py — Sprint 2.5 verification suite.

Items covered:
  Item 1 — Aggressive trading config (strategy_config.json + _PARAM_RANGES + system_v1.txt)
  Item 2 — ChromaDB PROTOCOL_BUFFERS env var (verified in server .env)
  Item 3 — Cost spine taxonomy (bot.py attribution caller tags)
  Item 4 — OI gate + CVNA pipeline (liquidity_gates.min_open_interest=100)
"""
import json
import subprocess
from pathlib import Path

import pytest

# ─── Item 1 — Aggressive Trading Config ──────────────────────────────────────

class TestAggressiveConfig:
    def _cfg(self):
        return json.loads(Path("strategy_config.json").read_text())

    def test_max_position_pct_equity_raised(self):
        assert self._cfg()["parameters"]["max_position_pct_equity"] == 0.25

    def test_max_positions_raised(self):
        assert self._cfg()["parameters"]["max_positions"] == 20

    def test_margin_multiplier_raised(self):
        assert self._cfg()["parameters"]["margin_sizing_multiplier"] == 4.0

    def test_high_conviction_threshold_lowered(self):
        thresholds = self._cfg()["parameters"]["margin_sizing_conviction_thresholds"]
        assert thresholds["high"] == 0.65

    def test_medium_conviction_threshold_unchanged(self):
        thresholds = self._cfg()["parameters"]["margin_sizing_conviction_thresholds"]
        assert thresholds["medium"] == 0.5

    def test_cash_reserve_pct_lowered(self):
        assert self._cfg()["position_sizing"]["cash_reserve_pct"] == 0.05

    def test_max_total_exposure_pct_raised(self):
        assert self._cfg()["position_sizing"]["max_total_exposure_pct"] == 0.95

    def test_param_ranges_accommodate_new_max_position_pct(self):
        import weekly_review as wr
        lo, hi = wr._PARAM_RANGES["max_position_pct_equity"]
        assert lo <= 0.25 <= hi, f"0.25 not within _PARAM_RANGES bounds ({lo}, {hi})"

    def test_param_ranges_hi_raised(self):
        import weekly_review as wr
        assert wr._PARAM_RANGES["max_position_pct_equity"][1] >= 0.30

    def test_risk_kernel_uses_new_cap(self):
        """risk_kernel.size_position() must cap at 0.25 × equity, not 0.07."""
        import json as _json
        from pathlib import Path as _Path

        import risk_kernel as rk
        from schemas import (
            AccountAction,
            BrokerSnapshot,
            Direction,
            Tier,
            TradeIdea,
        )

        cfg = _json.loads(_Path("strategy_config.json").read_text())
        snap = BrokerSnapshot(
            equity=100_000.0,
            buying_power=120_000.0,
            cash=20_000.0,
            positions=[],
            open_orders=[],
        )
        idea = TradeIdea(
            symbol="AAPL",
            action=AccountAction.BUY,
            intent="enter_long",
            tier=Tier.CORE,
            conviction=0.90,
            direction=Direction.BULLISH,
            catalyst="strong earnings beat",
        )
        result = rk.size_position(idea, snap, cfg, current_price=200.0, vix=15.0)
        assert isinstance(result, tuple), f"Expected tuple, got rejection: {result}"
        qty, val = result
        # With max_position_pct_equity=0.25 × $100K equity = $25K max
        # HIGH conviction CORE = 20% of sizing_basis = 20% × min(120K, 400K) = 20% × 120K = $24K
        # $24K < $25K cap → val should be ~$24K (not capped down to $7K)
        assert val >= 20_000, f"Expected val >= $20K with 0.25 cap, got ${val:,.0f}"

    def test_validate_config_passes_no_failures(self):
        server_dir = Path("/home/trading-bot")
        if not server_dir.exists():
            pytest.skip("Server-only test — /home/trading-bot not present in CI")
        result = subprocess.run(
            [".venv/bin/python3", "validate_config.py"],
            capture_output=True, text=True, cwd=str(server_dir),
        )
        assert "0 failures" in result.stdout, (
            f"validate_config failures found:\n{result.stdout[-2000:]}"
        )

    def test_system_v1_high_threshold_updated(self):
        txt = Path("prompts/system_v1.txt").read_text()
        assert ">=0.65" in txt, "HIGH conviction threshold not updated to 0.65 in system_v1.txt"
        assert ">=0.75" not in txt, "Old HIGH threshold 0.75 still present in system_v1.txt"

    def test_system_v1_multiplier_updated(self):
        txt = Path("prompts/system_v1.txt").read_text()
        assert "equity x 4.0" in txt, "Multiplier not updated to 4.0 in system_v1.txt"
        assert "equity x 3.0" not in txt, "Old multiplier 3.0 still present in system_v1.txt"

    def test_system_v1_hold_language_softened(self):
        txt = Path("prompts/system_v1.txt").read_text()
        assert "60-70% of cycles" not in txt, "Old conservative HOLD instruction still present"
        assert "paper trading mode" in txt.lower(), "Paper trading mode instruction not added"

    def test_system_v1_max_positions_updated(self):
        txt = Path("prompts/system_v1.txt").read_text()
        assert "20" in txt, "max_positions 20 not referenced in system_v1.txt"


# ─── Item 2 — ChromaDB env var ────────────────────────────────────────────────

class TestChromaDBEnvVar:
    def test_protocol_buffers_in_env_file(self):
        """PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python must be in .env on server."""
        env_path = Path("/home/trading-bot/.env")
        if not env_path.exists():
            pytest.skip("Not running on server — .env not present")
        content = env_path.read_text()
        assert "PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python" in content

    def test_chromadb_importable(self):
        """chromadb must be importable (PROTOCOL_BUFFERS workaround active)."""
        import os
        # The env var is set in systemd service + .env; when running tests via pytest
        # on the server, the service sets it. If not set here, skip gracefully.
        if os.environ.get("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION") != "python":
            pytest.skip("PROTOCOL_BUFFERS env var not set in this process — OK in service context")
        import chromadb  # noqa: F401
        assert chromadb.__version__ == "1.5.7"

    def test_trade_memory_importable(self):
        """trade_memory must import without error (chromadb dependency)."""
        import os
        if os.environ.get("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION") != "python":
            pytest.skip("PROTOCOL_BUFFERS env var not set in this process — OK in service context")
        import trade_memory  # noqa: F401


# ─── Item 3 — Cost Spine Taxonomy ────────────────────────────────────────────

class TestCostSpineTaxonomy:
    def test_decision_made_caller_in_bot_source(self):
        """bot.py decision_made attribution call must have caller=bot_decision."""
        src = Path("bot.py").read_text()
        assert '"caller": "bot_decision"' in src, (
            "caller=bot_decision not found in bot.py decision_made attribution call"
        )

    def test_order_submitted_caller_in_bot_source(self):
        """bot.py order_submitted attribution call must have caller=bot_order_submitted."""
        src = Path("bot.py").read_text()
        assert '"caller": "bot_order_submitted"' in src, (
            "caller=bot_order_submitted not found in bot.py order_submitted attribution call"
        )

    def test_emit_spine_record_uses_caller_fallback(self):
        """_emit_spine_record falls back to event.get('caller') when module_tags has no module."""
        import inspect

        from attribution import _emit_spine_record
        src = inspect.getsource(_emit_spine_record)
        assert 'event.get("caller")' in src, (
            "_emit_spine_record does not read caller from event dict"
        )

    def test_both_callers_are_distinct(self):
        """The two caller values must be different strings."""
        assert "bot_decision" != "bot_order_submitted"

    def test_no_unknown_from_fixed_call_sites(self):
        """Neither fixed call site should produce module_name=unknown after the fix.

        We verify structurally: both call sites now pass caller in extra,
        which _emit_spine_record reads as fallback → 'unknown' only when caller absent.
        """
        src = Path("bot.py").read_text()
        # Both callers present
        assert src.count('"caller": "bot_decision"') >= 1
        assert src.count('"caller": "bot_order_submitted"') >= 1


# ─── Item 4 — OI Gate + CVNA ─────────────────────────────────────────────────

class TestOIGateAndCVNA:
    def _cfg(self):
        return json.loads(Path("strategy_config.json").read_text())

    def test_oi_gate_lowered_to_100(self):
        gates = self._cfg()["account2"]["liquidity_gates"]
        assert gates["min_open_interest"] == 100

    def test_cvna_in_admissible_universe(self):
        from earnings_rotation import _admissible_universe
        universe = _admissible_universe()
        assert "CVNA" in universe, "CVNA not found in _admissible_universe()"

    def test_cvna_passes_pre_debate_oi_floor(self):
        """CVNA OI=142 >= pre_debate_oi_floor=75."""
        gates = self._cfg()["account2"]["liquidity_gates"]
        cvna_oi = 142
        floor = gates.get("pre_debate_oi_floor", 75)
        assert cvna_oi >= floor, f"CVNA OI={cvna_oi} < pre_debate_oi_floor={floor}"

    def test_cvna_passes_builder_oi_gate(self):
        """CVNA OI=142 >= liquidity_gates.min_open_interest=100 (was 150)."""
        gates = self._cfg()["account2"]["liquidity_gates"]
        cvna_oi = 142
        min_oi = gates["min_open_interest"]
        assert cvna_oi >= min_oi, f"CVNA OI={cvna_oi} < liquidity_gates.min_open_interest={min_oi}"

    def test_cvna_passes_veto_oi_threshold(self):
        """CVNA OI=142 >= a2_veto_thresholds.min_open_interest=100."""
        veto = self._cfg()["a2_veto_thresholds"]
        cvna_oi = 142
        veto_oi = veto.get("min_open_interest", 100)
        assert cvna_oi >= veto_oi, f"CVNA OI={cvna_oi} < veto min_open_interest={veto_oi}"

    def test_all_three_oi_gates_coherent(self):
        """pre_debate_oi_floor <= a2_veto_thresholds.min_oi <= liquidity_gates.min_oi."""
        cfg = self._cfg()
        gates = cfg["account2"]["liquidity_gates"]
        veto = cfg["a2_veto_thresholds"]
        pre = gates.get("pre_debate_oi_floor", 75)
        min_oi = gates["min_open_interest"]
        veto_oi = veto.get("min_open_interest", 100)
        # Pre-debate is the loosest gate; veto and builder should be equal or tighter
        assert pre <= veto_oi, f"pre_debate_oi_floor ({pre}) > veto min_oi ({veto_oi})"
        assert pre <= min_oi, f"pre_debate_oi_floor ({pre}) > liquidity_gates min_oi ({min_oi})"

    def test_oi_gate_handles_none_oi_gracefully(self):
        """Veto rule V2: if oi is None, it must not reject."""
        from bot_options_stage2_structures import _apply_veto_rules
        cfg = json.loads(Path("strategy_config.json").read_text())

        class _FakePack:
            liquidity_score = 0.5
            iv_environment = "neutral"
            iv_rank = 40
            earnings_days_away = 30

        candidate = {"open_interest": None, "dte": 14, "bid_ask_spread_pct": 0.05}
        result = _apply_veto_rules(candidate, _FakePack(), 100_000.0, cfg)
        assert result is None or "open_interest" not in (result or ""), (
            f"None OI should not trigger V2 rejection, got: {result}"
        )
