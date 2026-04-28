"""
Sprint 7 — vol label, tier cap, and scratchpad gate tests.

V1-V4: market_data vol label uses d1_vol=X.Xx vs 20d; ivol=N/A emitted when no intraday data
T1-T5: risk_kernel.apply_tier_cap() caps Tier.CORE to Tier.DYNAMIC when signal score < 65
S1-S6: schemas.py TradeIdea override_scratchpad / override_reason fields parsed by validate_claude_decision
G1-G5: bot.py scratchpad gate rejects BUY on off-watching symbol without override_scratchpad
"""
from __future__ import annotations

import unittest

# ─────────────────────────────────────────────────────────────────────────────
# V — Vol label tests
# ─────────────────────────────────────────────────────────────────────────────

class TestVolLabel(unittest.TestCase):
    """Vol label source-code inspection — d1_vol label and ivol=N/A present in market_data.py."""

    @classmethod
    def setUpClass(cls):
        import os
        src_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "market_data.py")
        with open(src_path) as f:
            cls.src = f.read()

    # V1 — d1_vol label present in source (stock path)
    def test_v1_d1_vol_label_in_source(self):
        self.assertIn("d1_vol=", self.src)

    # V2 — vs 20d clarification in source
    def test_v2_vs_20d_in_source(self):
        self.assertIn("vs 20d", self.src)

    # V3 — ivol=N/A(no live data) present for explicit no-data case
    def test_v3_ivol_na_no_live_data_in_source(self):
        self.assertIn("ivol=N/A(no live data)", self.src)

    # V4 — old "Vol= label absent (regression guard)
    def test_v4_old_vol_label_absent(self):
        # Allow 'vol_ratio' variable usage but reject the old display string 'Vol=X.Xx avg'
        import re
        # Pattern: "Vol=" followed by digits — the old label format
        old_label = re.findall(r'"Vol=[\d.]+x', self.src)
        self.assertEqual(old_label, [], f"Old 'Vol=X.Xx' label still present: {old_label}")


# ─────────────────────────────────────────────────────────────────────────────
# T — Tier cap tests (risk_kernel.apply_tier_cap)
# ─────────────────────────────────────────────────────────────────────────────

class TestApplyTierCap(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from risk_kernel import apply_tier_cap
        from schemas import AccountAction, Direction, Tier, TradeIdea
        cls.apply_tier_cap = staticmethod(apply_tier_cap)
        cls.Tier = Tier
        cls.TradeIdea = TradeIdea
        cls.AccountAction = AccountAction
        cls.Direction = Direction

    def _idea(self, sym="STNG", tier="core", action="buy"):
        action_enum = {"buy": self.AccountAction.BUY, "hold": self.AccountAction.HOLD}.get(
            action, self.AccountAction.BUY
        )
        tier_enum = {"core": self.Tier.CORE, "dynamic": self.Tier.DYNAMIC}.get(
            tier, self.Tier.DYNAMIC
        )
        return self.TradeIdea(
            symbol=sym,
            action=action_enum,
            tier=tier_enum,
            conviction=0.70,
            direction=self.Direction.BULLISH,
            catalyst="breakout",
        )

    def _scores(self, sym, score=60, tier="dynamic"):
        return {"scored_symbols": {sym: {"score": score, "tier": tier}}}

    # T1 — BUY with score < 65 and CORE tier → capped to DYNAMIC
    def test_t1_core_buy_low_score_capped(self):
        ideas = [self._idea("STNG", tier="core", action="buy")]
        self.apply_tier_cap(ideas, self._scores("STNG", score=55))
        self.assertEqual(ideas[0].tier, self.Tier.DYNAMIC)

    # T2 — BUY with score >= 65 and CORE tier → unchanged
    def test_t2_core_buy_high_score_unchanged(self):
        ideas = [self._idea("NVDA", tier="core", action="buy")]
        self.apply_tier_cap(ideas, self._scores("NVDA", score=70))
        self.assertEqual(ideas[0].tier, self.Tier.CORE)

    # T3 — HOLD with CORE tier and low score → not capped (only BUY is subject)
    def test_t3_hold_not_capped(self):
        ideas = [self._idea("GLD", tier="core", action="hold")]
        self.apply_tier_cap(ideas, self._scores("GLD", score=40))
        self.assertEqual(ideas[0].tier, self.Tier.CORE)

    # T4 — Symbol absent from scores → tier unchanged
    def test_t4_missing_symbol_unchanged(self):
        ideas = [self._idea("XBI", tier="core", action="buy")]
        self.apply_tier_cap(ideas, {"scored_symbols": {}})
        self.assertEqual(ideas[0].tier, self.Tier.CORE)

    # T5 — Empty signal_scores_obj → no-op, no crash
    def test_t5_empty_scores_noop(self):
        ideas = [self._idea("AMZN", tier="core", action="buy")]
        self.apply_tier_cap(ideas, {})
        self.assertEqual(ideas[0].tier, self.Tier.CORE)

    # T6 — Score exactly 65.0 → NOT capped (threshold is strict <)
    def test_t6_score_exactly_65_not_capped(self):
        ideas = [self._idea("MSFT", tier="core", action="buy")]
        self.apply_tier_cap(ideas, self._scores("MSFT", score=65))
        self.assertEqual(ideas[0].tier, self.Tier.CORE)

    # T7 — Already DYNAMIC tier → no change (idempotent)
    def test_t7_already_dynamic_noop(self):
        ideas = [self._idea("PLTR", tier="dynamic", action="buy")]
        self.apply_tier_cap(ideas, self._scores("PLTR", score=50))
        self.assertEqual(ideas[0].tier, self.Tier.DYNAMIC)


# ─────────────────────────────────────────────────────────────────────────────
# S — Schema tests for override_scratchpad / override_reason fields
# ─────────────────────────────────────────────────────────────────────────────

class TestTradeIdeaOverrideFields(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from schemas import (
            AccountAction,
            Direction,
            Tier,
            TradeIdea,
            validate_claude_decision,
        )
        cls.TradeIdea = TradeIdea
        cls.AccountAction = AccountAction
        cls.Tier = Tier
        cls.Direction = Direction
        cls.validate_claude_decision = staticmethod(validate_claude_decision)

    def _raw_idea(self, **kwargs):
        base = {
            "intent": "enter_long",
            "symbol": "NVDA",
            "conviction": 0.70,
            "tier_preference": "core",
            "catalyst": "breakout",
            "direction": "bullish",
        }
        base.update(kwargs)
        return base

    # S1 — TradeIdea has override_scratchpad defaulting to False
    def test_s1_default_override_scratchpad_false(self):
        idea = self.TradeIdea(
            symbol="NVDA",
            action=self.AccountAction.BUY,
            tier=self.Tier.CORE,
            conviction=0.7,
            direction=self.Direction.BULLISH,
            catalyst="breakout",
        )
        self.assertFalse(idea.override_scratchpad)
        self.assertEqual(idea.override_reason, "")

    # S2 — validate_claude_decision parses override_scratchpad=True
    def test_s2_parse_override_scratchpad_true(self):
        data = {"ideas": [self._raw_idea(override_scratchpad=True, override_reason="strong catalyst")]}
        dec = self.validate_claude_decision(data)
        self.assertEqual(len(dec.ideas), 1)
        self.assertTrue(dec.ideas[0].override_scratchpad)
        self.assertEqual(dec.ideas[0].override_reason, "strong catalyst")

    # S3 — validate_claude_decision with override_scratchpad absent → defaults to False
    def test_s3_missing_override_defaults_false(self):
        data = {"ideas": [self._raw_idea()]}
        dec = self.validate_claude_decision(data)
        self.assertFalse(dec.ideas[0].override_scratchpad)
        self.assertEqual(dec.ideas[0].override_reason, "")

    # S4 — override_scratchpad=False explicitly parsed correctly
    def test_s4_explicit_false_preserved(self):
        data = {"ideas": [self._raw_idea(override_scratchpad=False, override_reason="")]}
        dec = self.validate_claude_decision(data)
        self.assertFalse(dec.ideas[0].override_scratchpad)


# ─────────────────────────────────────────────────────────────────────────────
# G — Scratchpad gate logic tests (unit-level, no bot.py import)
# ─────────────────────────────────────────────────────────────────────────────

def _run_scratchpad_gate(ideas, watching_syms):
    """Replicate the exact gate logic from bot.py for unit testing."""
    watching = set(watching_syms)
    filtered = []
    rejected = []
    if watching:
        for idea in ideas:
            sym = idea.symbol
            if (
                idea.action.value in ("buy", "reallocate")
                and sym not in watching
                and not idea.override_scratchpad
            ):
                rejected.append(sym)
            else:
                filtered.append(idea)
    else:
        filtered = list(ideas)
    return filtered, rejected


class TestScratchpadGate(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from schemas import AccountAction, Direction, Tier, TradeIdea
        cls.TradeIdea = TradeIdea
        cls.AccountAction = AccountAction
        cls.Direction = Direction
        cls.Tier = Tier

    def _idea(self, sym, action="buy", override=False, reason=""):
        action_enum = {
            "buy": self.AccountAction.BUY,
            "hold": self.AccountAction.HOLD,
            "reallocate": self.AccountAction.REALLOCATE,
        }.get(action, self.AccountAction.BUY)
        return self.TradeIdea(
            symbol=sym,
            action=action_enum,
            tier=self.Tier.DYNAMIC,
            conviction=0.70,
            direction=self.Direction.BULLISH,
            catalyst="breakout",
            override_scratchpad=override,
            override_reason=reason,
        )

    # G1 — BUY on watching symbol passes
    def test_g1_buy_watching_symbol_passes(self):
        ideas = [self._idea("NVDA")]
        filtered, rejected = _run_scratchpad_gate(ideas, ["NVDA", "GLD"])
        self.assertEqual(len(filtered), 1)
        self.assertEqual(rejected, [])

    # G2 — BUY on off-watching symbol without override → rejected
    def test_g2_buy_off_watching_rejected(self):
        ideas = [self._idea("STNG")]
        filtered, rejected = _run_scratchpad_gate(ideas, ["NVDA", "GLD"])
        self.assertEqual(filtered, [])
        self.assertIn("STNG", rejected)

    # G3 — BUY on off-watching symbol WITH override_scratchpad=True → passes
    def test_g3_override_scratchpad_passes(self):
        ideas = [self._idea("STNG", override=True, reason="Citrini thesis intact")]
        filtered, rejected = _run_scratchpad_gate(ideas, ["NVDA", "GLD"])
        self.assertEqual(len(filtered), 1)
        self.assertEqual(rejected, [])

    # G4 — HOLD on off-watching symbol → always passes (gate only applies to BUY/REALLOCATE)
    def test_g4_hold_off_watching_passes(self):
        ideas = [self._idea("STNG", action="hold")]
        filtered, rejected = _run_scratchpad_gate(ideas, ["NVDA"])
        self.assertEqual(len(filtered), 1)
        self.assertEqual(rejected, [])

    # G5 — Empty watching list → gate is disabled, all ideas pass
    def test_g5_empty_watching_gate_disabled(self):
        ideas = [self._idea("STNG"), self._idea("FRO")]
        filtered, rejected = _run_scratchpad_gate(ideas, [])
        self.assertEqual(len(filtered), 2)
        self.assertEqual(rejected, [])

    # G6 — REALLOCATE on off-watching entry symbol → rejected
    def test_g6_reallocate_off_watching_rejected(self):
        ideas = [self._idea("STNG", action="reallocate")]
        filtered, rejected = _run_scratchpad_gate(ideas, ["NVDA"])
        self.assertEqual(filtered, [])
        self.assertIn("STNG", rejected)

    # G7 — Mixed ideas: one watching, one not → only off-watching BUY rejected
    def test_g7_mixed_ideas_selective_rejection(self):
        ideas = [self._idea("NVDA"), self._idea("STNG")]
        filtered, rejected = _run_scratchpad_gate(ideas, ["NVDA"])
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].symbol, "NVDA")
        self.assertIn("STNG", rejected)


if __name__ == "__main__":
    unittest.main()
