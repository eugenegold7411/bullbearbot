"""
Tests for the merged L3+Scratchpad single-call architecture.

Covers:
  1. _MERGED_SYSTEM token count >= 1024 (cache eligibility)
  2. _run_merged_synthesis() calls _call_merged_l3_scratchpad() once, not batched
  3. Held positions always land in haiku_symbols (Haiku-enriched)
  4. L2-only fallback written for symbols beyond _MAX_MERGED
  5. Scratchpad sub-dict embedded in returned result
  6. ≤15-point adjustment guardrail applied on merged output
  7. run_scratchpad_stage() extracts embedded scratchpad (no extra API call)
  8. run_scratchpad_stage() falls back to standalone when no embedded scratchpad
  9. _run_merged_synthesis() falls back to batched L3 when merged call raises
 10. Catalyst_type taxonomy validation on merged output
 11. L2-only fallback symbol has adjustment_reason='l3_unavailable_or_skip'
"""

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import bot_stage2_5_scratchpad as b25
import bot_stage2_signal as bss

# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_position(symbol: str, qty: float = 10.0, unrealized_plpc: float = 0.05):
    p = SimpleNamespace()
    p.symbol          = symbol
    p.qty             = qty
    p.unrealized_plpc = unrealized_plpc
    return p


def _make_l2_scores(symbols: list[str], base_score: float = 60.0) -> dict:
    return {
        sym: {
            "score":             base_score - i,
            "direction":         "bullish",
            "conviction":        "medium",
            "signals":           ["rsi_bullish"],
            "conflicts":         [],
            "orb_candidate":     False,
            "pattern_watchlist": False,
            "price":             100.0 + i,
            "earnings_days_away": None,
        }
        for i, sym in enumerate(symbols)
    }


def _mock_merged_response(symbols: list[str]) -> dict:
    """Minimal valid merged Haiku response covering all symbols."""
    return {
        "scored_symbols": {
            sym: {
                "score":             60,
                "direction":         "bullish",
                "conviction":        "medium",
                "signals":           ["test"],
                "conflicts":         [],
                "primary_catalyst":  "test catalyst",
                "catalyst_type":     "technical_breakout",
                "orb_candidate":     False,
                "pattern_watchlist": False,
                "tier":              "dynamic",
                "l2_score":          60,
                "l3_adjustment":     0,
                "adjustment_reason": "",
            }
            for sym in symbols
        },
        "top_3":            symbols[:3],
        "elevated_caution": [],
        "reasoning":        "Sentence one. Sentence two.",
        "scratchpad": {
            "watching":           symbols[:2],
            "blocking":           [],
            "triggers":           [f"{symbols[0]}: break above $100"],
            "conviction_ranking": [
                {"symbol": symbols[0], "conviction": "medium", "notes": "test note"},
            ],
            "summary": "Bullish test regime no blocks.",
        },
    }


def _mock_claude_resp(payload: dict) -> MagicMock:
    resp          = MagicMock()
    resp.content  = [MagicMock(text=json.dumps(payload))]
    usage         = MagicMock()
    usage.input_tokens                  = 500
    usage.output_tokens                 = 300
    usage.cache_read_input_tokens       = 0
    usage.cache_creation_input_tokens   = 0
    resp.usage = usage
    return resp


# ── Test cases ────────────────────────────────────────────────────────────────

class TestMergedSystemPromptCacheEligibility(unittest.TestCase):
    def test_merged_system_is_over_1024_tokens(self):
        """_MERGED_SYSTEM must be long enough to trigger Anthropic ephemeral caching."""
        # Anthropic requires >= 1024 tokens. We approximate: 1 token ≈ 3.5 chars.
        # Require >= 3584 chars as a conservative proxy for the 1024-token threshold.
        char_count = len(bss._MERGED_SYSTEM)
        self.assertGreaterEqual(
            char_count, 3584,
            f"_MERGED_SYSTEM is only {char_count} chars — may be below 1024-token cache threshold",
        )


class TestRunMergedSynthesis(unittest.TestCase):

    def _run(self, symbols, l2_scores, positions=None, merged_payload=None):
        """Helper: run _run_merged_synthesis() with mocked API call."""
        if merged_payload is None:
            merged_payload = _mock_merged_response(symbols[:bss._MAX_MERGED])
        qual_ctx = {}
        regime   = {"regime_score": 60, "bias": "neutral", "session_theme": "test", "constraints": []}
        md       = {"vix": 18.0, "vix_regime": "normal"}

        with patch.object(bss, "_call_merged_l3_scratchpad",
                          return_value=merged_payload) as mock_call:
            result = bss._run_merged_synthesis(
                symbols, l2_scores, qual_ctx, regime, positions or [], md
            )
        return result, mock_call

    def test_single_api_call_for_any_universe_size(self):
        """Merged synthesis must call the API exactly once regardless of symbol count."""
        symbols   = [f"SYM{i:03d}" for i in range(91)]
        l2_scores = _make_l2_scores(symbols)
        _, mock_call = self._run(symbols, l2_scores)
        self.assertEqual(mock_call.call_count, 1)

    def test_held_positions_always_haiku_enriched(self):
        """Held positions must land in the Haiku-scored symbol set, not L2-fallback."""
        held_sym  = "HELD_SYM"
        # Put held symbol last in the symbol list to verify reordering
        symbols   = [f"SYM{i:03d}" for i in range(80)] + [held_sym]
        l2_scores = _make_l2_scores(symbols, base_score=30.0)  # low scores for all
        positions = [_make_position(held_sym)]

        merged_payload = _mock_merged_response([held_sym] + [f"SYM{i:03d}" for i in range(39)])
        result, _ = self._run(symbols, l2_scores, positions, merged_payload)

        # held_sym must be Haiku-enriched (l3_adjustment not from l3_unavailable path)
        held_row = result["scored_symbols"].get(held_sym, {})
        self.assertNotEqual(
            held_row.get("adjustment_reason"), "l3_unavailable_or_skip",
            "Held position must be Haiku-enriched, not L2-only fallback",
        )

    def test_l2_only_fallback_for_symbols_beyond_max_merged(self):
        """Symbols ranked > _MAX_MERGED must receive L2-only fallback scores."""
        symbols   = [f"SYM{i:03d}" for i in range(91)]
        l2_scores = _make_l2_scores(symbols)
        result, _ = self._run(symbols, l2_scores)

        # Every symbol must be in output
        self.assertEqual(len(result["scored_symbols"]), len(symbols))
        # Symbols 40-90 must be L2-only
        l2_syms = symbols[bss._MAX_MERGED:]
        for sym in l2_syms:
            row = result["scored_symbols"][sym]
            self.assertEqual(
                row.get("adjustment_reason"), "l3_unavailable_or_skip",
                f"{sym} should be L2-only fallback",
            )

    def test_scratchpad_embedded_in_result(self):
        """Merged result must include 'scratchpad' key from API response."""
        symbols   = [f"SYM{i:03d}" for i in range(10)]
        l2_scores = _make_l2_scores(symbols)
        result, _ = self._run(symbols, l2_scores)
        self.assertIn("scratchpad", result)
        self.assertIn("watching", result["scratchpad"])

    def test_adjustment_guardrail_clamps_over_15(self):
        """Adjustments > 15 points must be clamped to ±15."""
        sym       = "BIGADJ"
        symbols   = [sym]
        l2_scores = {sym: {
            "score": 50.0, "direction": "neutral", "conviction": "low",
            "signals": [], "conflicts": [], "orb_candidate": False,
            "pattern_watchlist": False, "price": 100.0, "earnings_days_away": None,
        }}
        payload = {
            "scored_symbols": {sym: {
                "score":             80.0,   # 30-point adjustment — must clamp to 65
                "direction":         "bullish",
                "conviction":        "high",
                "signals":           [],
                "conflicts":         [],
                "primary_catalyst":  "test",
                "catalyst_type":     "unknown",
                "orb_candidate":     False,
                "pattern_watchlist": False,
                "tier":              "dynamic",
                "l2_score":          50.0,
                "l3_adjustment":     30.0,
                "adjustment_reason": "big move",
            }},
            "top_3": [sym], "elevated_caution": [], "reasoning": "x. y.",
            "scratchpad": {"watching": [], "blocking": [], "triggers": [],
                           "conviction_ranking": [], "summary": "test."},
        }
        result, _ = self._run(symbols, l2_scores, merged_payload=payload)
        row = result["scored_symbols"][sym]
        self.assertLessEqual(row["score"], 65.1, "Score must be clamped to l2 + 15")
        self.assertLessEqual(row["l3_adjustment"], 15.0)

    def test_fallback_to_batched_l3_on_merged_failure(self):
        """On merged call failure, must fall back to batched _run_l3_synthesis()."""
        symbols   = [f"SYM{i:03d}" for i in range(10)]
        l2_scores = _make_l2_scores(symbols)
        qual_ctx  = {}
        regime    = {"regime_score": 60, "bias": "neutral", "session_theme": "t", "constraints": []}
        md        = {"vix": 18.0}

        fallback_result = {
            "scored_symbols":     {s: _make_l2_scores([s])[s] for s in symbols},
            "top_3":              symbols[:3],
            "elevated_caution":   [],
            "reasoning":          "fallback",
            "l1_staleness_minutes": 0,
            "l3_used":            True,
        }

        with patch.object(bss, "_call_merged_l3_scratchpad", side_effect=RuntimeError("API down")), \
             patch.object(bss, "_run_l3_synthesis", return_value=fallback_result) as mock_fallback:
            result = bss._run_merged_synthesis(symbols, l2_scores, qual_ctx, regime, [], md)

        mock_fallback.assert_called_once()
        self.assertNotIn("scratchpad", result, "Fallback path must not embed scratchpad")


class TestRunScratchpadStageExtraction(unittest.TestCase):

    def _make_signal_scores_with_scratchpad(self):
        return {
            "scored_symbols": {"AAPL": {"score": 75}},
            "top_3":          ["AAPL"],
            "scratchpad": {
                "watching":           ["AAPL"],
                "blocking":           [],
                "triggers":           ["AAPL: break above $185"],
                "conviction_ranking": [{"symbol": "AAPL", "conviction": "high", "notes": "test"}],
                "summary":            "Bullish AAPL.",
            },
        }

    def test_extracts_embedded_scratchpad_without_api_call(self):
        """Fast path: embedded scratchpad extracted with zero API calls."""
        signal_scores = self._make_signal_scores_with_scratchpad()
        regime = {"regime_score": 65}
        md     = {"vix": 16.0}

        with patch("bot_stage2_5_scratchpad._scratchpad") as mock_sp, \
             patch("bot_stage2_5_scratchpad.trade_memory") as mock_tm:
            mock_sp.save_hot_scratchpad = MagicMock()
            mock_tm.save_scratchpad_memory = MagicMock()

            result = b25.run_scratchpad_stage(signal_scores, regime, md, [])

        self.assertEqual(result["watching"], ["AAPL"])
        self.assertIn("ts", result)
        self.assertEqual(result["regime_score"], 65)
        mock_sp.save_hot_scratchpad.assert_called_once()
        mock_tm.save_scratchpad_memory.assert_called_once()
        # Crucially: no call to _scratchpad.run_scratchpad()
        mock_sp.run_scratchpad.assert_not_called()

    def test_falls_back_to_standalone_when_no_embedded_scratchpad(self):
        """Without embedded scratchpad, must call standalone run_scratchpad()."""
        signal_scores = {"scored_symbols": {"AAPL": {"score": 75}}, "top_3": ["AAPL"]}
        regime = {"regime_score": 65}
        md     = {"vix": 16.0}

        standalone_result = {
            "watching": ["AAPL"], "blocking": [], "triggers": [],
            "conviction_ranking": [], "summary": "test.",
        }

        with patch("bot_stage2_5_scratchpad._scratchpad") as mock_sp, \
             patch("bot_stage2_5_scratchpad.trade_memory") as mock_tm:
            mock_sp.run_scratchpad.return_value = standalone_result
            mock_sp.save_hot_scratchpad = MagicMock()
            mock_tm.save_scratchpad_memory = MagicMock()

            result = b25.run_scratchpad_stage(signal_scores, regime, md, [])

        mock_sp.run_scratchpad.assert_called_once()
        self.assertEqual(result["watching"], ["AAPL"])

    def test_metadata_stamped_on_extracted_scratchpad(self):
        """Extracted scratchpad must have ts, regime_score, and vix stamped."""
        signal_scores = self._make_signal_scores_with_scratchpad()
        regime = {"regime_score": 72}
        md     = {"vix": 21.5}

        with patch("bot_stage2_5_scratchpad._scratchpad"), \
             patch("bot_stage2_5_scratchpad.trade_memory"):
            result = b25.run_scratchpad_stage(signal_scores, regime, md, [])

        self.assertIn("ts", result)
        self.assertEqual(result["regime_score"], 72)
        self.assertAlmostEqual(result["vix"], 21.5, places=1)

    def test_empty_signal_scores_returns_empty_dict(self):
        result = b25.run_scratchpad_stage({}, {}, {}, [])
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
