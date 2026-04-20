"""
tests/test_backtest_runner.py — Unit tests for backtest_runner.py fixes.

Covers:
    Suite A — _rule_based_actions (deterministic path, Fix 1)
    Suite B — _run_strategy LLM gate (Fix 1)
    Suite C — _run_strategy_director sample gate (Fix 3)
    Suite D — _write_strategy_config is a no-op (Fix 2)
    Suite E — run_backtest does not call _write_strategy_config (Fix 2)
"""

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── helpers ───────────────────────────────────────────────────────────────────

def _make_snapshot(
    rsi: float = 60.0,
    vol_ratio: float = 3.5,
    close: float = 100.0,
    ma20: float | None = 90.0,
    pct_vs_ma20: float | None = 11.1,
    intermarket: list[str] | None = None,
    sym: str = "AAPL",
    tier: str = "core",
) -> dict:
    return {
        "sym_indicators": {
            sym: {
                "close":       close,
                "ma20":        ma20,
                "rsi":         rsi,
                "vol_ratio":   vol_ratio,
                "pct_vs_ma20": pct_vs_ma20,
                "sector":      "technology",
                "type":        "stock",
                "tier":        tier,
            }
        },
        "intermarket_signals": intermarket or [],
    }


# ── Suite A — _rule_based_actions ─────────────────────────────────────────────

class TestRuleBasedActions:
    """_rule_based_actions must be deterministic and strategy-specific."""

    def setup_method(self):
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from backtest_runner import _rule_based_actions
        self._fn = _rule_based_actions

    def test_momentum_buys_on_signal(self):
        snap = _make_snapshot(rsi=60.0, vol_ratio=3.5, close=100.0, ma20=90.0)
        actions = self._fn(snap, "momentum", 30_000)
        assert len(actions) == 1
        assert actions[0]["action"] == "buy"
        assert actions[0]["symbol"] == "AAPL"

    def test_momentum_no_buy_low_volume(self):
        snap = _make_snapshot(rsi=60.0, vol_ratio=1.5, close=100.0, ma20=90.0)
        actions = self._fn(snap, "momentum", 30_000)
        assert actions == []

    def test_momentum_no_buy_rsi_too_high(self):
        snap = _make_snapshot(rsi=75.0, vol_ratio=4.0, close=100.0, ma20=90.0)
        actions = self._fn(snap, "momentum", 30_000)
        assert actions == []

    def test_momentum_no_buy_below_ma20(self):
        snap = _make_snapshot(rsi=60.0, vol_ratio=3.5, close=80.0, ma20=90.0)
        actions = self._fn(snap, "momentum", 30_000)
        assert actions == []

    def test_mean_reversion_buys_oversold(self):
        snap = _make_snapshot(rsi=25.0, vol_ratio=1.0, close=80.0, ma20=90.0,
                               pct_vs_ma20=-11.0)
        actions = self._fn(snap, "mean_reversion", 30_000)
        assert len(actions) == 1
        assert actions[0]["action"] == "buy"

    def test_mean_reversion_no_buy_not_oversold(self):
        snap = _make_snapshot(rsi=45.0, vol_ratio=1.0, pct_vs_ma20=-2.0)
        actions = self._fn(snap, "mean_reversion", 30_000)
        assert actions == []

    def test_news_sentiment_always_holds(self):
        snap = _make_snapshot(rsi=60.0, vol_ratio=5.0)
        actions = self._fn(snap, "news_sentiment", 30_000)
        assert actions == []

    def test_cross_sector_buys_on_bull_intermarket(self):
        snap = _make_snapshot(
            intermarket=["Gold rising — risk-off, consider TLT/GLD long"],
        )
        actions = self._fn(snap, "cross_sector", 30_000)
        assert len(actions) == 1

    def test_cross_sector_no_buy_without_intermarket(self):
        snap = _make_snapshot(intermarket=[])
        actions = self._fn(snap, "cross_sector", 30_000)
        assert actions == []

    def test_hybrid_requires_two_signals(self):
        # momentum + cross_sector = 2 → should buy
        snap = _make_snapshot(
            rsi=60.0, vol_ratio=3.5, close=100.0, ma20=90.0,
            intermarket=["Energy sector strong — oil tailwind"],
        )
        actions = self._fn(snap, "hybrid", 30_000)
        assert len(actions) == 1

    def test_hybrid_no_buy_single_signal(self):
        # only momentum signal; no intermarket, no mean-reversion
        snap = _make_snapshot(rsi=60.0, vol_ratio=3.5, close=100.0, ma20=90.0,
                               intermarket=[])
        actions = self._fn(snap, "hybrid", 30_000)
        assert actions == []

    def test_skips_crypto_symbols(self):
        snap = {
            "sym_indicators": {
                "BTC/USD": {
                    "close": 80_000, "ma20": 70_000, "rsi": 60.0,
                    "vol_ratio": 4.0, "pct_vs_ma20": 14.0,
                    "sector": "crypto", "type": "crypto", "tier": "core",
                }
            },
            "intermarket_signals": [],
        }
        actions = self._fn(snap, "momentum", 30_000)
        assert actions == []

    def test_qty_is_tier_sized(self):
        snap = _make_snapshot(rsi=60.0, vol_ratio=3.5, close=100.0, ma20=90.0, tier="core")
        actions = self._fn(snap, "momentum", 30_000)
        assert len(actions) == 1
        # core = 15% of 30_000 = 4_500 / 100 = 45 shares
        assert actions[0]["qty"] == 45

    def test_determinism_identical_inputs(self):
        snap = _make_snapshot(rsi=60.0, vol_ratio=3.5, close=100.0, ma20=90.0)
        from backtest_runner import _rule_based_actions
        r1 = _rule_based_actions(snap, "momentum", 30_000)
        r2 = _rule_based_actions(snap, "momentum", 30_000)
        assert r1 == r2

    def test_max_three_actions(self):
        """At most 3 actions returned even with many qualifying symbols."""
        indicators = {}
        for i in range(10):
            indicators[f"SYM{i}"] = {
                "close": 100.0, "ma20": 90.0, "rsi": 60.0,
                "vol_ratio": 4.0, "pct_vs_ma20": 11.0,
                "sector": "tech", "type": "stock", "tier": "core",
            }
        snap = {"sym_indicators": indicators, "intermarket_signals": []}
        from backtest_runner import _rule_based_actions
        actions = _rule_based_actions(snap, "momentum", 100_000)
        assert len(actions) <= 3


# ── Suite B — _run_strategy LLM gate ─────────────────────────────────────────

class TestRunStrategyLLMGate:
    """_run_strategy must use the rule-based path when the flag is off."""

    def setup_method(self):
        sys.path.insert(0, str(Path(__file__).parent.parent))

    def _minimal_bars(self) -> dict:
        return {
            "AAPL": [
                {"date": f"2026-01-{i+1:02d}", "open": 100+i, "high": 105+i,
                 "low": 99+i, "close": 101+i, "volume": 1_000_000}
                for i in range(60)
            ]
        }

    def _core_symbols(self) -> list:
        return [{"symbol": "AAPL", "sector": "technology",
                 "type": "stock", "tier": "core"}]

    def test_flag_off_does_not_call_claude(self):
        import backtest_runner as br

        # Inject a stub feature_flags into sys.modules so the inline
        # `from feature_flags import is_enabled` picks up our stub.
        stub_ff = types.ModuleType("feature_flags")
        stub_ff.is_enabled = lambda flag, default=False: False

        original_claude = br.claude
        mock_claude = MagicMock()
        br.claude = mock_claude

        orig_ff = sys.modules.get("feature_flags")
        sys.modules["feature_flags"] = stub_ff
        try:
            br._run_strategy(
                strategy_name="momentum",
                system_prompt=br.STRATEGY_PROMPTS["momentum"],
                all_bars=self._minimal_bars(),
                core_symbols=self._core_symbols(),
                trade_dates=["2026-01-31"],
            )
        finally:
            br.claude = original_claude
            if orig_ff is None:
                sys.modules.pop("feature_flags", None)
            else:
                sys.modules["feature_flags"] = orig_ff

        mock_claude.messages.create.assert_not_called()

    def test_flag_on_calls_claude(self):
        import backtest_runner as br

        stub_ff = types.ModuleType("feature_flags")
        stub_ff.is_enabled = lambda flag, default=False: flag == "backtest_llm_enabled"

        original_claude = br.claude
        mock_claude = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='{"actions": [], "rationale": "no setup"}')]
        mock_response.usage = MagicMock(
            cache_read_input_tokens=0, cache_creation_input_tokens=0
        )
        mock_claude.messages.create.return_value = mock_response
        br.claude = mock_claude

        orig_ff = sys.modules.get("feature_flags")
        sys.modules["feature_flags"] = stub_ff
        try:
            br._run_strategy(
                strategy_name="momentum",
                system_prompt=br.STRATEGY_PROMPTS["momentum"],
                all_bars=self._minimal_bars(),
                core_symbols=self._core_symbols(),
                trade_dates=["2026-01-31"],
            )
        finally:
            br.claude = original_claude
            if orig_ff is None:
                sys.modules.pop("feature_flags", None)
            else:
                sys.modules["feature_flags"] = orig_ff

        assert mock_claude.messages.create.called


# ── Suite C — _run_strategy_director sample gate ──────────────────────────────

class TestStrategyDirectorSampleGate:
    """_run_strategy_director must return early when n_closed < minimum."""

    def setup_method(self):
        sys.path.insert(0, str(Path(__file__).parent.parent))

    def _call_director(
        self, n_closed: int, min_required: int = 30,
        tmp_path: Path | None = None,
    ) -> dict:
        import tempfile

        import backtest_runner as br

        perf = {"totals": {"trades": n_closed}}
        cfg  = {"parameters": {"backtest_minimum_sample_before_recalibration": min_required}}

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            mem_dir = td_path / "memory"
            mem_dir.mkdir()
            (mem_dir / "performance.json").write_text(json.dumps(perf))
            (td_path / "strategy_config.json").write_text(json.dumps(cfg))

            orig_base   = br.BASE_DIR
            orig_config = br.CONFIG_FILE
            br.BASE_DIR    = td_path
            br.CONFIG_FILE = td_path / "strategy_config.json"
            try:
                result = br._run_strategy_director({"hybrid": {"sharpe": 1.0, "return_pct": 5.0}})
            finally:
                br.BASE_DIR    = orig_base
                br.CONFIG_FILE = orig_config

        return result

    def test_below_minimum_returns_insufficient_sample(self):
        result = self._call_director(n_closed=5, min_required=30)
        assert result.get("insufficient_sample") is True
        assert result["n_closed_trades"] == 5
        assert result["min_required"] == 30
        assert result["winner"] == ""
        assert result["parameter_adjustments"] == {}

    def test_below_minimum_no_claude_call(self):
        import backtest_runner as br
        orig_claude = br.claude
        mock_claude = MagicMock()
        br.claude = mock_claude
        try:
            self._call_director(n_closed=2, min_required=30)
        finally:
            br.claude = orig_claude
        mock_claude.messages.create.assert_not_called()

    def test_at_minimum_proceeds(self):
        import tempfile

        import backtest_runner as br

        perf = {"totals": {"trades": 30}}
        cfg  = {"parameters": {"backtest_minimum_sample_before_recalibration": 30}}

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            (td_path / "memory").mkdir()
            (td_path / "memory" / "performance.json").write_text(json.dumps(perf))
            (td_path / "strategy_config.json").write_text(json.dumps(cfg))

            orig_base, orig_config = br.BASE_DIR, br.CONFIG_FILE
            br.BASE_DIR    = td_path
            br.CONFIG_FILE = td_path / "strategy_config.json"
            orig_claude = br.claude
            mock_claude = MagicMock()
            mock_response = MagicMock()
            mock_response.content = [MagicMock(text=json.dumps({
                "winner": "hybrid",
                "rationale": "test",
                "parameter_adjustments": {},
            }))]
            mock_claude.messages.create.return_value = mock_response
            br.claude = mock_claude

            try:
                result = br._run_strategy_director(
                    {"hybrid": {"sharpe": 1.0, "return_pct": 5.0}}
                )
            finally:
                br.BASE_DIR    = orig_base
                br.CONFIG_FILE = orig_config
                br.claude = orig_claude

        assert result.get("insufficient_sample") is not True
        assert mock_claude.messages.create.called

    def test_above_minimum_proceeds(self):
        # at 31 with min=30, should proceed (assert insufficient_sample not set)
        import tempfile

        import backtest_runner as br

        perf = {"totals": {"trades": 31}}
        cfg  = {"parameters": {"backtest_minimum_sample_before_recalibration": 30}}

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            (td_path / "memory").mkdir()
            (td_path / "memory" / "performance.json").write_text(json.dumps(perf))
            (td_path / "strategy_config.json").write_text(json.dumps(cfg))
            orig_base, orig_config = br.BASE_DIR, br.CONFIG_FILE
            br.BASE_DIR    = td_path
            br.CONFIG_FILE = td_path / "strategy_config.json"
            orig_claude = br.claude
            mock_claude = MagicMock()
            mock_response = MagicMock()
            mock_response.content = [MagicMock(text='{"winner":"hybrid","rationale":"ok","parameter_adjustments":{}}')]
            mock_claude.messages.create.return_value = mock_response
            br.claude = mock_claude
            try:
                result = br._run_strategy_director(
                    {"hybrid": {"sharpe": 1.0, "return_pct": 5.0}}
                )
            finally:
                br.BASE_DIR    = orig_base
                br.CONFIG_FILE = orig_config
                br.claude = orig_claude

        assert result.get("insufficient_sample") is not True

    def test_missing_performance_file_defaults_to_zero(self):
        import tempfile

        import backtest_runner as br

        cfg = {"parameters": {"backtest_minimum_sample_before_recalibration": 30}}
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            (td_path / "strategy_config.json").write_text(json.dumps(cfg))
            orig_base, orig_config = br.BASE_DIR, br.CONFIG_FILE
            br.BASE_DIR    = td_path
            br.CONFIG_FILE = td_path / "strategy_config.json"
            try:
                result = br._run_strategy_director({"hybrid": {}})
            finally:
                br.BASE_DIR    = orig_base
                br.CONFIG_FILE = orig_config

        assert result.get("insufficient_sample") is True
        assert result["n_closed_trades"] == 0


# ── Suite D — _write_strategy_config is a no-op ───────────────────────────────

class TestWriteStrategyConfigNoOp:
    """_write_strategy_config must not write to strategy_config.json."""

    def setup_method(self):
        sys.path.insert(0, str(Path(__file__).parent.parent))

    def test_does_not_modify_config_file(self, tmp_path):
        import backtest_runner as br

        config = {"active_strategy": "hybrid", "parameters": {"stop_loss_pct_core": 0.035}}
        cfg_path = tmp_path / "strategy_config.json"
        cfg_path.write_text(json.dumps(config))
        original_content = cfg_path.read_text()

        orig_config = br.CONFIG_FILE
        br.CONFIG_FILE = cfg_path
        try:
            br._write_strategy_config(
                {"winner": "momentum", "parameter_adjustments": {}},
                {"momentum": {"return_pct": 5.0}},
            )
        finally:
            br.CONFIG_FILE = orig_config

        assert cfg_path.read_text() == original_content

    def test_returns_none(self):
        import backtest_runner as br
        result = br._write_strategy_config(
            {"winner": "momentum", "parameter_adjustments": {}},
            {},
        )
        assert result is None


# ── Suite E — run_backtest does not write config ───────────────────────────────

class TestRunBacktestNoConfigWrite:
    """run_backtest must not write to strategy_config.json."""

    def test_config_file_unchanged_after_run_backtest(self, tmp_path):

        import backtest_runner as br

        config = {"active_strategy": "hybrid", "parameters": {}}
        cfg_path = tmp_path / "strategy_config.json"
        cfg_path.write_text(json.dumps(config))
        original_mtime = cfg_path.stat().st_mtime

        orig_config = br.CONFIG_FILE
        br.CONFIG_FILE = cfg_path

        # Minimal mock to short-circuit run_backtest without touching the FS
        with patch.object(br, "_load_all_bars", return_value={}):
            try:
                result = br.run_backtest()
            finally:
                br.CONFIG_FILE = orig_config

        # run_backtest returns early when all_bars is empty — config untouched
        assert cfg_path.stat().st_mtime == original_mtime
        assert result == {}
