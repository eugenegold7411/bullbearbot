"""Tests for runtime file persistence fixes.

TEST 1 — regime_cache.json written after classify_regime()
TEST 2 — regime_cache.json write failure does not crash classify_regime()
TEST 3 — signal_weights_live.json written after weekly_review save
TEST 4 — ensure_signal_weights_live() creates file from defaults when missing
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


# ── Minimal stubs ─────────────────────────────────────────────────────────────
def _stub(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    sys.modules.setdefault(name, mod)
    return mod


for _n in [
    "anthropic",
    "dotenv",
    "log_setup",
    "bot_clients",
    "cost_tracker",
    "macro_wire",
    "macro_intelligence",
    "scheduler",
    "watchlist_manager",
]:
    _stub(_n)

sys.modules["dotenv"].load_dotenv = lambda *a, **kw: None
sys.modules["log_setup"].get_logger = lambda name: __import__("logging").getLogger(name)
sys.modules["log_setup"].log_trade = lambda *a, **kw: None
sys.modules["bot_clients"].MODEL_FAST = "claude-haiku-4-5-20251001"
sys.modules["bot_clients"]._get_claude = MagicMock()
sys.modules["cost_tracker"].get_tracker = MagicMock(return_value=MagicMock())

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── TEST 1 & 2 — regime_cache.json ────────────────────────────────────────────

class TestRegimeCache(unittest.TestCase):

    def _make_mock_response(self) -> MagicMock:
        resp = MagicMock()
        resp.content = [MagicMock(text='{"regime_score": 65, "bias": "risk_on", '
                                       '"session_theme": "tech-led rally", '
                                       '"constraints": [], "high_impact_warning": null, '
                                       '"orb_context": "Not in ORB window", '
                                       '"confidence": "high", "macro_regime": "goldilocks", '
                                       '"commodity_trend": "neutral", "dollar_trend": "neutral", '
                                       '"credit_stress": "normal"}')]
        resp.usage = MagicMock(cache_read_input_tokens=0)
        return resp

    def test_regime_cache_written_after_classify(self):
        """classify_regime() writes regime_cache.json with required fields."""
        import bot_stage1_regime as br

        mock_client = MagicMock()
        mock_client.messages.create.return_value = self._make_mock_response()

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "regime_cache.json"
            with patch.object(br, "_get_claude", return_value=mock_client), \
                 patch.object(br, "_REGIME_CACHE_PATH", cache_path):
                result = br.classify_regime(
                    md={"vix": 17.0, "time_et": "10:30", "vix_regime": "low"},
                    calendar={"events": []},
                )

            self.assertTrue(cache_path.exists(), "regime_cache.json was not written")
            cached = json.loads(cache_path.read_text())
            self.assertEqual(cached["regime_score"], 65)
            self.assertEqual(cached["bias"], "risk_on")
            self.assertIn("generated_at", cached)
            self.assertIn("vix", cached)
            # result is returned correctly even when writing
            self.assertEqual(result["regime_score"], 65)

    def test_regime_cache_write_failure_does_not_crash(self):
        """classify_regime() completes successfully even if cache write raises OSError."""
        import bot_stage1_regime as br

        mock_client = MagicMock()
        mock_client.messages.create.return_value = self._make_mock_response()

        with patch.object(br, "_get_claude", return_value=mock_client), \
             patch("bot_stage1_regime.os.replace", side_effect=OSError("disk full")):
            mock_file = MagicMock()
            mock_file.name = "/tmp/fake.tmp"
            with patch("bot_stage1_regime.tempfile.NamedTemporaryFile", return_value=mock_file):
                result = br.classify_regime(
                    md={"vix": 17.0, "time_et": "10:30", "vix_regime": "low"},
                    calendar={"events": []},
                )

        # The call must still return a valid regime dict
        self.assertIn("regime_score", result)
        self.assertIn("bias", result)


# ── TEST 3 & 4 — signal_weights_live.json ─────────────────────────────────────

class TestSignalWeightsLive(unittest.TestCase):

    def test_signal_weights_live_written_after_save(self):
        """_write_signal_weights_live() writes the file with required fields."""
        import weekly_review as wr

        weights = {
            "momentum_weight": 0.35,
            "mean_reversion_weight": 0.20,
            "news_sentiment_weight": 0.30,
            "cross_sector_weight": 0.15,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            live_path = Path(tmpdir) / "signal_weights_live.json"
            with patch.object(wr, "_SIGNAL_WEIGHTS_LIVE", live_path):
                wr._write_signal_weights_live(weights, source="test")

            self.assertTrue(live_path.exists(), "signal_weights_live.json was not written")
            data = json.loads(live_path.read_text())
            self.assertEqual(data["signal_source_weights"], weights)
            self.assertEqual(data["source"], "test")
            self.assertIn("updated_at", data)

    def test_ensure_signal_weights_live_creates_from_defaults(self):
        """ensure_signal_weights_live() creates the file when it is missing."""
        import weekly_review as wr

        default_config = {
            "signal_source_weights": {
                "momentum_weight": 0.35,
                "news_sentiment_weight": 0.30,
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            live_path = Path(tmpdir) / "signal_weights_live.json"
            self.assertFalse(live_path.exists())

            with patch.object(wr, "_SIGNAL_WEIGHTS_LIVE", live_path):
                wr.ensure_signal_weights_live(config=default_config)

            self.assertTrue(live_path.exists(), "file should have been created from defaults")
            data = json.loads(live_path.read_text())
            self.assertEqual(data["signal_source_weights"]["momentum_weight"], 0.35)
            self.assertEqual(data["source"], "startup_default")

    def test_ensure_signal_weights_live_noop_when_exists(self):
        """ensure_signal_weights_live() does not overwrite an existing file."""
        import weekly_review as wr

        existing = {"signal_source_weights": {"old_weight": 0.99}, "source": "existing"}
        with tempfile.TemporaryDirectory() as tmpdir:
            live_path = Path(tmpdir) / "signal_weights_live.json"
            live_path.write_text(json.dumps(existing))

            with patch.object(wr, "_SIGNAL_WEIGHTS_LIVE", live_path):
                wr.ensure_signal_weights_live(config={"signal_source_weights": {"new_weight": 0.01}})

            # Should still have old content
            data = json.loads(live_path.read_text())
            self.assertIn("old_weight", data.get("signal_source_weights", {}))


if __name__ == "__main__":
    unittest.main()
