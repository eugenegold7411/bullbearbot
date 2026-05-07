"""
tests/test_thesis_score_clamp.py — Thesis score saturation fix.

TEST 1 — Heuristic clamp ceiling is 9, never 10 (10 reserved for explicit override).
TEST 2 — Strong-rally stack (+tech +pnl +sector) caps at 9.
TEST 3 — Persistent loser (below MAs, trending stop, sector down) drops below 7.
TEST 4 — build_portfolio_intelligence writes thesis_scores_latest.json atomically.
TEST 5 — Greppable [PI] log line fires per-position.
TEST 6 — Override flag forces score ≤ 4 (preserves existing behavior).
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch


def _mock_position(symbol: str, qty: float, entry: float, current: float,
                   unreal: float = 0.0):
    p = MagicMock()
    p.symbol            = symbol
    p.qty               = str(qty)
    p.avg_entry_price   = str(entry)
    p.current_price     = str(current)
    p.unrealized_pl     = str(unreal)
    p.market_value      = str(qty * current)
    return p


class TestThesisClampAt9(unittest.TestCase):

    def test_strong_rally_clamps_at_9(self):
        """Stack: base 8 + tech +1 + pnl +1 + sector +1 = 11 → clamp to 9."""
        import portfolio_intelligence as pi

        pos = _mock_position("AAPL", qty=100, entry=200.0, current=215.0, unreal=1500.0)

        with patch.object(pi, "_load_bars", return_value=None), \
             patch.object(pi, "_load_sector_perf",
                          return_value={"technology": {"day_chg": 1.5}}), \
             patch.object(pi, "_get_symbol_sector", return_value="technology"):
            result = pi.score_position_thesis(
                symbol="AAPL", position=pos,
                original_decision={"catalyst": "momentum", "stop_loss": 190.0,
                                   "take_profit": 230.0},
                current_md={}, days_held=2, strategy_config={},
            )
        self.assertLessEqual(result["thesis_score"], 9,
                             "Heuristic must cap at 9 — 10 reserved for override")
        self.assertGreaterEqual(result["thesis_score"], 7,
                                "Healthy winner should still score 7+")

    def test_clamp_ceiling_is_9_for_extreme_stack(self):
        """Even if every factor lands positively, score never exceeds 9."""
        import portfolio_intelligence as pi

        pos = _mock_position("NVDA", qty=10, entry=100.0, current=150.0, unreal=500.0)
        bars_df = MagicMock()
        bars_df.empty = False
        bars_df.columns = ["close"]
        # Make MA20 and EMA9 both below current price (technical_intact)
        closes = MagicMock()
        closes.rolling.return_value.mean.return_value.iloc = [50.0]
        closes.ewm.return_value.mean.return_value.iloc = [60.0]
        closes.__len__ = lambda s: 30
        bars_df.__getitem__ = lambda s, k: closes

        with patch.object(pi, "_load_bars", return_value=bars_df), \
             patch.object(pi, "_load_sector_perf",
                          return_value={"technology": {"day_chg": 5.0}}), \
             patch.object(pi, "_get_symbol_sector", return_value="technology"):
            result = pi.score_position_thesis(
                symbol="NVDA", position=pos,
                original_decision={"catalyst": "momentum", "stop_loss": 90.0,
                                   "take_profit": 200.0},
                current_md={}, days_held=1, strategy_config={},
            )
        self.assertEqual(result["thesis_score"], 9,
                         "Max heuristic stack must clamp to 9 exactly")


class TestThesisDiscriminatesLoser(unittest.TestCase):

    def test_persistent_loser_scores_low(self):
        """Below MAs + trending stop + sector down + earnings stale → ≤ 5."""
        import portfolio_intelligence as pi

        pos = _mock_position("LOSE", qty=100, entry=100.0, current=92.0, unreal=-800.0)
        bars_df = MagicMock()
        bars_df.empty = False
        bars_df.columns = ["close"]
        closes = MagicMock()
        # Both MA20 and EMA9 above current — below both
        closes.rolling.return_value.mean.return_value.iloc = [105.0]
        closes.ewm.return_value.mean.return_value.iloc = [102.0]
        closes.__len__ = lambda s: 30
        bars_df.__getitem__ = lambda s, k: closes

        with patch.object(pi, "_load_bars", return_value=bars_df), \
             patch.object(pi, "_load_sector_perf",
                          return_value={"technology": {"day_chg": -2.0}}), \
             patch.object(pi, "_get_symbol_sector", return_value="technology"):
            result = pi.score_position_thesis(
                symbol="LOSE", position=pos,
                original_decision={"catalyst": "earnings", "stop_loss": 90.0,
                                   "take_profit": 120.0},
                current_md={}, days_held=10, strategy_config={},
            )
        self.assertLessEqual(result["thesis_score"], 5,
                             "Persistent loser must score ≤ 5 to enable rotation")
        self.assertIn(result["recommended_action"], ("reduce", "exit_consider"))


class TestThesisLogLineFires(unittest.TestCase):

    def test_greppable_log_format(self):
        """[PI] {symbol}: score=N/10 ... log line fires for every position."""
        import portfolio_intelligence as pi

        pos = _mock_position("AAPL", qty=10, entry=100.0, current=105.0, unreal=50.0)

        with self.assertLogs("portfolio_intelligence", level="INFO") as captured:
            with patch.object(pi, "_load_bars", return_value=None), \
                 patch.object(pi, "_load_sector_perf", return_value={}), \
                 patch.object(pi, "_get_symbol_sector", return_value=None):
                pi.score_position_thesis(
                    symbol="AAPL", position=pos,
                    original_decision={"stop_loss": 95.0, "take_profit": 120.0},
                    current_md={}, days_held=1, strategy_config={},
                )
        joined = "\n".join(captured.output)
        self.assertIn("[PI] AAPL:", joined)
        self.assertIn("score=", joined)
        self.assertIn("catalyst_age=", joined)
        self.assertIn("tech=", joined)
        self.assertIn("trend=", joined)
        self.assertIn("pnl=", joined)


class TestOverridePreserved(unittest.TestCase):

    def test_override_flag_caps_at_4(self):
        """Override (target hit / deadline passed) still forces score ≤ 4."""
        import portfolio_intelligence as pi

        pos = _mock_position("FOO", qty=10, entry=100.0, current=125.0, unreal=250.0)
        cfg = {
            "time_bound_actions": [
                {"symbol": "FOO", "target_price": 120.0,
                 "deadline_utc": "2026-01-01T00:00:00Z"},
            ]
        }
        with patch.object(pi, "_load_bars", return_value=None), \
             patch.object(pi, "_load_sector_perf", return_value={}), \
             patch.object(pi, "_get_symbol_sector", return_value=None):
            result = pi.score_position_thesis(
                symbol="FOO", position=pos,
                original_decision={"stop_loss": 95.0, "take_profit": 130.0},
                current_md={}, days_held=1, strategy_config=cfg,
            )
        self.assertLessEqual(result["thesis_score"], 4)
        self.assertEqual(result["recommended_action"], "exit_consider")


class TestBuildPortfolioIntelligencePersists(unittest.TestCase):

    def test_thesis_scores_latest_json_written(self):
        """build_portfolio_intelligence writes thesis_scores_latest.json on each call."""
        import portfolio_intelligence as pi

        pos = _mock_position("AAPL", qty=10, entry=100.0, current=105.0, unreal=50.0)

        with patch.object(pi, "_load_bars", return_value=None), \
             patch.object(pi, "_load_sector_perf", return_value={}), \
             patch.object(pi, "_get_symbol_sector", return_value=None), \
             patch.object(pi, "compute_dynamic_sizes",
                          return_value={"tier_caps": {}}), \
             patch.object(pi, "compute_position_health",
                          return_value={"flag": "HEALTHY"}), \
             patch.object(pi, "get_forced_exits", return_value=[]), \
             patch.object(pi, "get_deadline_exits", return_value=[]):
            result = pi.build_portfolio_intelligence(
                equity=100000.0,
                positions=[pos],
                config={},
            )
        out_path = pi._ROOT / "data" / "analytics" / "thesis_scores_latest.json"
        self.assertTrue(out_path.exists(), f"Expected file at {out_path}")
        payload = json.loads(out_path.read_text())
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["thesis_scores"][0]["symbol"], "AAPL")
        self.assertIn("generated_at", payload)
        # In-memory result also returned
        self.assertEqual(len(result["thesis_scores"]), 1)

    def test_summary_log_fires(self):
        """build_portfolio_intelligence logs avg/min/max summary."""
        import portfolio_intelligence as pi

        pos1 = _mock_position("AAPL", qty=10, entry=100.0, current=105.0, unreal=50.0)
        pos2 = _mock_position("MSFT", qty=10, entry=200.0, current=210.0, unreal=100.0)

        with self.assertLogs("portfolio_intelligence", level="INFO") as captured:
            with patch.object(pi, "_load_bars", return_value=None), \
                 patch.object(pi, "_load_sector_perf", return_value={}), \
                 patch.object(pi, "_get_symbol_sector", return_value=None), \
                 patch.object(pi, "compute_dynamic_sizes",
                              return_value={"tier_caps": {}}), \
                 patch.object(pi, "compute_position_health",
                              return_value={"flag": "HEALTHY"}), \
                 patch.object(pi, "get_forced_exits", return_value=[]), \
                 patch.object(pi, "get_deadline_exits", return_value=[]):
                pi.build_portfolio_intelligence(
                    equity=100000.0,
                    positions=[pos1, pos2],
                    config={},
                )
        joined = "\n".join(captured.output)
        self.assertIn("Thesis scoring complete", joined)
        self.assertIn("2 positions", joined)
        self.assertIn("avg=", joined)


if __name__ == "__main__":
    unittest.main()
