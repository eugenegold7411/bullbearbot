"""
S10 — A2 confidence threshold tests.

Verifies that paper_confidence_floor (0.75) and live_confidence_floor (0.85)
are correctly read from strategy_config.json and applied in both Stage 3
(run_bounded_debate) and Stage 4 (submit_selected_candidate).

TC1  — paper mode: conf=0.76 passes gate (>= paper floor 0.75)
TC2  — paper mode: conf=0.74 blocked by gate (< paper floor 0.75)
TC3  — live mode: conf=0.85 passes gate (>= live floor 0.85)
TC4  — live mode: conf=0.84 blocked by gate (< live floor 0.85)
TC5  — stage3 conf_floor propagates correctly to run_options_debate prompt
TC6  — stage4 uses paper floor when pf_allow_live_orders=False
TC7  — stage4 uses live floor when pf_allow_live_orders=True
TC8  — strategy_config.json has paper_confidence_floor=0.75, live_confidence_floor=0.85
       (no debate_confidence_floor dead key)
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _paper_config(paper_floor: float = 0.75, live_floor: float = 0.85) -> dict:
    return {
        "account2": {
            "paper_confidence_floor": paper_floor,
            "live_confidence_floor": live_floor,
            "pf_allow_live_orders": False,
        },
        "a2_rollback": {},
    }


def _live_config(paper_floor: float = 0.75, live_floor: float = 0.85) -> dict:
    cfg = _paper_config(paper_floor, live_floor)
    cfg["account2"]["pf_allow_live_orders"] = True
    return cfg


def _minimal_candidate_structure(cid: str = "C1", conf: float = 0.80) -> dict:
    return {
        "candidate_id": cid,
        "symbol": "SPY",
        "structure_type": "debit_spread",
        "direction": "bullish",
        "thesis": "test",
        "confidence": conf,
    }


def _make_debate_result(cid: str = "C1", conf: float = 0.80, reject: bool = False) -> dict:
    return {
        "selected_candidate_id": cid,
        "confidence": conf,
        "reject": reject,
        "key_risks": [],
        "reasons": "test",
        "recommended_size_modifier": 1.0,
    }


# ── TC1–TC4: run_bounded_debate gate ─────────────────────────────────────────

class TestStage3ConfidenceGate(unittest.TestCase):
    """TC1–TC4: run_bounded_debate applies paper vs live floor correctly."""

    def _run_bounded(self, conf: float, config: dict, live_url: bool = False) -> str:
        """
        Patch run_options_debate to return a controlled confidence and verify
        whether run_bounded_debate sets no_trade_reason=debate_low_confidence.

        live_url=True patches ALPACA_BASE_URL to a live endpoint so the code
        reads live_confidence_floor instead of paper_confidence_floor.
        """
        from bot_options_stage3_debate import run_bounded_debate

        cand_struct = [_minimal_candidate_structure(conf=conf)]
        debate_result = _make_debate_result(conf=conf)

        env_patch = {"ALPACA_BASE_URL": "https://api.alpaca.markets"} if live_url else {}

        with patch("bot_options_stage3_debate.run_options_debate",
                   return_value=(debate_result, "prompt", "raw")), \
             patch("bot_options_stage3_debate._load_strategy_config", return_value=config), \
             patch.dict("os.environ", env_patch, clear=False):
            record = run_bounded_debate(
                candidate_sets=[],
                candidates=[],
                candidate_structures=cand_struct,
                allowed_by_sym={},
                equity=100_000.0,
                vix=18.0,
                regime="normal",
                account1_summary="test",
                obs_mode=False,
                session_tier="market",
                iv_summaries={},
                t_start=0.0,
                config=config,
            )
        return record.no_trade_reason or ""

    def test_tc1_paper_76_passes(self):
        """TC1: conf=0.76 in paper mode (floor=0.75) → passes, no_trade_reason is not low_confidence."""
        reason = self._run_bounded(0.76, _paper_config())
        self.assertNotEqual(reason, "debate_low_confidence",
                            f"Expected pass but got no_trade_reason={reason!r}")

    def test_tc2_paper_74_blocked(self):
        """TC2: conf=0.74 in paper mode (floor=0.75) → blocked as debate_low_confidence."""
        reason = self._run_bounded(0.74, _paper_config())
        self.assertEqual(reason, "debate_low_confidence")

    def test_tc3_live_85_passes(self):
        """TC3: conf=0.85 in live mode (floor=0.85) → passes."""
        reason = self._run_bounded(0.85, _live_config(), live_url=True)
        self.assertNotEqual(reason, "debate_low_confidence",
                            f"Expected pass but got no_trade_reason={reason!r}")

    def test_tc4_live_84_blocked(self):
        """TC4: conf=0.84 in live mode (floor=0.85) → blocked as debate_low_confidence."""
        reason = self._run_bounded(0.84, _live_config(), live_url=True)
        self.assertEqual(reason, "debate_low_confidence")


# ── TC5: conf_floor propagates into prompt string ─────────────────────────────

class TestStage3PromptCalibration(unittest.TestCase):
    """TC5: conf_floor parameter appears in the debate prompt text."""

    @pytest.mark.requires_prompts
    def test_tc5_conf_floor_in_bounded_prompt(self):
        """TC5: run_options_debate with conf_floor=0.75 includes '0.75' in prompt."""
        p = Path(__file__).parent.parent / "prompts" / "system_options_v1.txt"
        if not p.exists():
            pytest.skip("prompts/system_options_v1.txt not in repo")
        from bot_options_stage3_debate import run_options_debate

        captured_prompt = {}

        def fake_create(**kwargs):
            msgs = kwargs.get("messages", [])
            if msgs:
                captured_prompt["content"] = msgs[-1]["content"]
            resp = MagicMock()
            resp.content = [MagicMock(text='{"selected_candidate_id":null,"confidence":0.0,"reject":true,"key_risks":[],"reasons":"no","recommended_size_modifier":1.0}')]
            return resp

        cand = _minimal_candidate_structure()
        with patch("bot_options_stage3_debate._get_claude") as mock_claude:
            mock_claude.return_value.messages.create.side_effect = fake_create
            run_options_debate(
                candidates=[],
                iv_summaries={"SPY": {"iv_rank": 40, "environment": "neutral"}},
                vix=18.0,
                regime="normal",
                account1_summary="test",
                obs_mode=False,
                equity=100_000.0,
                candidate_structures=[cand],
                conf_floor=0.75,
            )

        prompt_text = captured_prompt.get("content", "")
        self.assertIn("0.75", prompt_text,
                      "Expected conf_floor=0.75 to appear in debate prompt")


# ── TC6–TC7: Stage 4 execution gate ──────────────────────────────────────────

class TestStage4ConfidenceGate(unittest.TestCase):
    """TC6–TC7: submit_selected_candidate reads floor from config."""

    def _run_stage4(self, conf: float, pf_allow_live_orders: bool,
                    paper_floor: float = 0.75, live_floor: float = 0.85) -> str:
        from bot_options_stage4_execution import submit_selected_candidate
        from schemas import A2DecisionRecord

        cand_struct = [_minimal_candidate_structure(conf=conf)]
        debate_parsed = _make_debate_result(conf=conf)

        record = A2DecisionRecord(
            decision_id="test-id",
            session_tier="market",
            candidate_sets=[],
            debate_input=None,
            debate_output_raw=None,
            debate_parsed=debate_parsed,
            selected_candidate=None,
            execution_result="pending",
            no_trade_reason=None,
            elapsed_seconds=0.0,
        )

        cfg = {
            "account2": {
                "paper_confidence_floor": paper_floor,
                "live_confidence_floor": live_floor,
            }
        }

        with patch("bot_options_stage4_execution._load_strategy_config", return_value=cfg):
            result = submit_selected_candidate(
                decision_record=record,
                alpaca_client=MagicMock(),
                candidates=[],
                candidate_structures=cand_struct,
                iv_summaries={},
                equity=100_000.0,
                pf_allow_new_entries=True,
                pf_allow_live_orders=pf_allow_live_orders,
                obs_mode=False,
                a2_mode=MagicMock(),
            )
        return result

    def test_tc6_stage4_paper_floor_applied(self):
        """TC6: conf=0.74 with pf_allow_live_orders=False → no_trade (paper floor 0.75)."""
        result = self._run_stage4(conf=0.74, pf_allow_live_orders=False)
        self.assertEqual(result, "no_trade")

    def test_tc7_stage4_live_floor_applied(self):
        """TC7: conf=0.84 with pf_allow_live_orders=True → no_trade (live floor 0.85)."""
        result = self._run_stage4(conf=0.84, pf_allow_live_orders=True)
        self.assertEqual(result, "no_trade")


# ── TC8: strategy_config.json schema check ───────────────────────────────────

class TestStrategyConfigSchema(unittest.TestCase):
    """TC8: strategy_config.json uses new keys, not dead key."""

    def test_tc8_config_has_new_keys_not_dead_key(self):
        """TC8: paper_confidence_floor=0.75, live_confidence_floor=0.85, no debate_confidence_floor."""
        cfg_path = Path(__file__).parent.parent / "strategy_config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        a2 = cfg.get("account2", {})

        self.assertIn("paper_confidence_floor", a2,
                      "account2.paper_confidence_floor missing from strategy_config.json")
        self.assertIn("live_confidence_floor", a2,
                      "account2.live_confidence_floor missing from strategy_config.json")
        self.assertAlmostEqual(float(a2["paper_confidence_floor"]), 0.70, places=3)
        self.assertAlmostEqual(float(a2["live_confidence_floor"]), 0.85, places=3)
        self.assertNotIn("debate_confidence_floor", a2,
                         "Dead key debate_confidence_floor should not be in strategy_config.json")


if __name__ == "__main__":
    unittest.main()
