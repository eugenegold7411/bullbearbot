"""
Sprint 7 Phase B tests — S7-P, S7-D, S7-E, S7-F.

S7-P: Position cap headroom fix — existing position value subtracted before applying cap
S7-D: ChromaDB metadata backfill guard (read-path guard)
S7-E: Allocator output wired into Stage 3
S7-F: Trim score threshold reads from config instead of hardcoded
"""
import unittest
from unittest.mock import MagicMock


# ─────────────────────────────────────────────────────────────────────────────
# S7-P — Position Cap Headroom Fix
# ─────────────────────────────────────────────────────────────────────────────

class TestPositionCapHeadroom(unittest.TestCase):
    """size_position() subtracts existing position value before applying max_position_pct_equity."""

    @classmethod
    def setUpClass(cls):
        from risk_kernel import size_position
        from schemas import (
            AccountAction,
            BrokerSnapshot,
            Conviction,
            Direction,
            NormalizedPosition,
            Tier,
            TradeIdea,
        )
        cls.size_position = staticmethod(size_position)
        cls.BrokerSnapshot = BrokerSnapshot
        cls.NormalizedPosition = NormalizedPosition
        cls.TradeIdea = TradeIdea
        cls.AccountAction = AccountAction
        cls.Direction = Direction
        cls.Conviction = Conviction
        cls.Tier = Tier

    # equity=$100k, cap=15% → $15k hard ceiling per position
    _CONFIG = {
        "parameters": {"max_position_pct_equity": 0.15},
        "position_sizing": {
            "core_tier_pct": 0.15,
            "dynamic_tier_pct": 0.08,
            "intraday_tier_pct": 0.05,
        },
        "account2": {},
    }

    def _pos(self, symbol, market_value):
        return self.NormalizedPosition(
            symbol=symbol,
            alpaca_sym=symbol,
            qty=1.0,
            avg_entry_price=market_value,
            current_price=market_value,
            market_value=market_value,
            unrealized_pl=0.0,
            unrealized_plpc=0.0,
            is_crypto_pos=False,
        )

    def _snapshot(self, positions=None, equity=100_000.0):
        return self.BrokerSnapshot(
            equity=equity,
            cash=equity,
            buying_power=equity * 2,
            open_orders=[],
            positions=positions or [],
        )

    def _idea(self, symbol="AAPL"):
        return self.TradeIdea(
            symbol=symbol,
            action=self.AccountAction.BUY,
            direction=self.Direction.BULLISH,
            conviction=0.60,
            tier=self.Tier.CORE,
            catalyst="breakout",
        )

    def test_add_into_existing_position_reduces_budget(self):
        """ADD with $5k existing → cap headroom = $10k, not $15k."""
        snap = self._snapshot(positions=[self._pos("AAPL", 5_000.0)])
        result = self.size_position(
            self._idea("AAPL"), snap, self._CONFIG,
            current_price=100.0, vix=20.0,
        )
        self.assertIsInstance(result, tuple, "Expected (qty, val) tuple")
        qty, val = result
        # headroom = 15k - 5k = 10k → 100 shares at $100
        self.assertLessEqual(val, 10_000.0 + 0.01,
                             "Position value must not exceed $10k headroom")
        self.assertEqual(qty, 100)

    def test_add_where_existing_at_cap_is_blocked(self):
        """ADD with existing already at $15k cap → headroom=$0 → rejected."""
        snap = self._snapshot(positions=[self._pos("AAPL", 15_000.0)])
        result = self.size_position(
            self._idea("AAPL"), snap, self._CONFIG,
            current_price=100.0, vix=20.0,
        )
        self.assertIsInstance(result, str,
                              "Expected rejection string when existing position fills cap")
        self.assertIn("max_position_pct_equity", result)

    def test_add_where_existing_exceeds_cap_is_blocked(self):
        """ADD with existing $20k (above $15k cap) → headroom=0 → rejected."""
        snap = self._snapshot(positions=[self._pos("AAPL", 20_000.0)])
        result = self.size_position(
            self._idea("AAPL"), snap, self._CONFIG,
            current_price=100.0, vix=20.0,
        )
        self.assertIsInstance(result, str)
        self.assertIn("max_position_pct_equity", result)

    def test_zero_existing_unchanged_from_pre_fix(self):
        """With no existing position, behavior is identical to pre-S7-P: full $15k budget."""
        snap = self._snapshot(positions=[])
        result = self.size_position(
            self._idea("AAPL"), snap, self._CONFIG,
            current_price=100.0, vix=20.0,
        )
        self.assertIsInstance(result, tuple)
        qty, val = result
        # No existing position → full 15% = $15k → 150 shares
        self.assertEqual(qty, 150)
        self.assertAlmostEqual(val, 15_000.0, places=0)

    def test_different_symbol_position_not_counted(self):
        """Existing position in a different symbol does not reduce cap for AAPL."""
        snap = self._snapshot(positions=[self._pos("MSFT", 10_000.0)])
        result = self.size_position(
            self._idea("AAPL"), snap, self._CONFIG,
            current_price=100.0, vix=20.0,
        )
        self.assertIsInstance(result, tuple)
        qty, val = result
        # MSFT position doesn't affect AAPL cap → full $15k = 150 shares
        self.assertEqual(qty, 150)

    def test_existing_just_below_cap_gives_minimal_headroom(self):
        """Existing $14,900 → headroom $100 → 1 share at $100 allowed."""
        snap = self._snapshot(positions=[self._pos("AAPL", 14_900.0)])
        result = self.size_position(
            self._idea("AAPL"), snap, self._CONFIG,
            current_price=100.0, vix=20.0,
        )
        self.assertIsInstance(result, tuple)
        qty, val = result
        self.assertEqual(qty, 1)
        self.assertAlmostEqual(val, 100.0, places=0)


# ─────────────────────────────────────────────────────────────────────────────
# S7-F — Trim Score Threshold Config Read
# ─────────────────────────────────────────────────────────────────────────────

class TestTrimScoreThreshold(unittest.TestCase):
    """_decide_actions() reads trim_score_threshold from pa_cfg, not hardcoded 4."""

    @classmethod
    def setUpClass(cls):
        from portfolio_allocator import _PA_DEFAULTS, _decide_actions
        cls._decide_actions = staticmethod(_decide_actions)
        cls._PA_DEFAULTS = _PA_DEFAULTS

    def _incumbent(self, symbol="AAPL", thesis_score=5, market_value=10_000.0):
        return {
            "symbol":                 symbol,
            "thesis_score":          thesis_score,
            "thesis_score_normalized": thesis_score * 10,
            "market_value":          market_value,
            "account_pct":           10.0,
        }

    def _pa_cfg(self, trim_score_threshold=4):
        return {
            "replace_score_gap":             15.0,
            "trim_score_drop":               10.0,
            "trim_score_threshold":          trim_score_threshold,
            "weight_deadband":               0.02,
            "min_rebalance_notional":        500.0,
            "max_recommendations_per_cycle": 10,
            "same_symbol_daily_cooldown_enabled": False,
            "same_day_replace_block_hours":  6.0,
        }

    def _run(self, incumbents, pa_cfg=None):
        proposed, _ = self._decide_actions(
            incumbents=incumbents,
            candidates=[],
            pi_data={},
            cfg={},
            pa_cfg=pa_cfg or self._pa_cfg(),
            sizes={"available_for_new": 0},
            equity=100_000.0,
        )
        return proposed

    def test_score_at_threshold_fires_trim(self):
        """score == trim_score_threshold (4) → TRIM proposed."""
        proposed = self._run([self._incumbent(thesis_score=4, market_value=5_000.0)])
        actions = [a["action"] for a in proposed]
        self.assertIn("TRIM", actions)

    def test_score_above_threshold_no_trim(self):
        """score == 5 > threshold 4 → no TRIM."""
        proposed = self._run([self._incumbent(thesis_score=5)])
        actions = [a["action"] for a in proposed]
        self.assertNotIn("TRIM", actions)

    def test_config_override_raises_threshold(self):
        """trim_score_threshold=3 config → score=4 does NOT trigger TRIM."""
        proposed = self._run(
            [self._incumbent(thesis_score=4, market_value=5_000.0)],
            pa_cfg=self._pa_cfg(trim_score_threshold=3),
        )
        actions = [a["action"] for a in proposed]
        self.assertNotIn("TRIM", actions)

    def test_config_override_allows_trim_at_new_threshold(self):
        """trim_score_threshold=3 config → score=3 DOES trigger TRIM."""
        proposed = self._run(
            [self._incumbent(thesis_score=3, market_value=5_000.0)],
            pa_cfg=self._pa_cfg(trim_score_threshold=3),
        )
        actions = [a["action"] for a in proposed]
        self.assertIn("TRIM", actions)

    def test_default_threshold_is_4(self):
        """_PA_DEFAULTS has trim_score_threshold=4 (unchanged behavior after S7-F)."""
        self.assertEqual(self._PA_DEFAULTS["trim_score_threshold"], 4)


# ─────────────────────────────────────────────────────────────────────────────
# S7-D — ChromaDB Read-Path Guard
# ─────────────────────────────────────────────────────────────────────────────

class TestChromaDBReadPathGuard(unittest.TestCase):
    """format_retrieved_memories() normalizes legacy "?" regime sentinel."""

    @classmethod
    def setUpClass(cls):
        from trade_memory import format_retrieved_memories
        cls.format = staticmethod(format_retrieved_memories)

    def _scenario(self, regime="unknown", session="market", vix=20.0, outcome="pending"):
        return {
            "weighted_score": 0.9,
            "distance":       0.1,
            "document":       "session=market vix=20.0 regime=neutral actions: HOLD reasoning: test",
            "metadata": {
                "ts":      "2026-04-20T10:00:00",
                "session": session,
                "vix":     vix,
                "regime":  regime,
                "symbols": "AAPL",
                "outcome": outcome,
                "pnl":     0.0,
                "tier":    "short",
            },
        }

    def test_regime_question_mark_normalized_to_unknown(self):
        """Legacy regime="?" is displayed as "unknown", not as "?"."""
        out = self.format([self._scenario(regime="?")])
        self.assertNotIn("regime=?", out)
        self.assertIn("regime=unknown", out)

    def test_normal_regime_passes_through(self):
        """A real regime value is displayed unchanged."""
        out = self.format([self._scenario(regime="risk-off")])
        self.assertIn("regime=risk-off", out)

    def test_missing_regime_defaults_to_unknown(self):
        """Missing regime key → 'unknown' default (not '?')."""
        scenario = self._scenario()
        del scenario["metadata"]["regime"]
        out = self.format([scenario])
        self.assertIn("regime=unknown", out)

    def test_session_fallback_is_unknown_not_question_mark(self):
        """Missing session key → 'unknown' not '?' — don't assert unrecorded session."""
        scenario = self._scenario()
        del scenario["metadata"]["session"]
        out = self.format([scenario])
        self.assertIn("sess=unknown", out)

    def test_vix_zero_sentinel_displayed(self):
        """vix=0.0 sentinel renders correctly (not an error)."""
        out = self.format([self._scenario(vix=0.0)])
        self.assertIn("vix=0.0", out)

    def test_empty_scenarios_returns_no_results_message(self):
        """Empty list returns the no-results message."""
        out = self.format([])
        self.assertIn("no similar past scenarios", out.lower())


# ─────────────────────────────────────────────────────────────────────────────
# S7-E — Allocator Section Wire into Stage 3
# ─────────────────────────────────────────────────────────────────────────────

class TestAllocatorSectionWiring(unittest.TestCase):
    """format_allocator_section returns fallback header when output is None."""

    @classmethod
    def setUpClass(cls):
        from portfolio_allocator import format_allocator_section
        cls.format = staticmethod(format_allocator_section)

    def test_none_output_returns_fallback_header(self):
        """None → Option B: header + 'not available' message, not empty string."""
        out = self.format(None)
        self.assertIn("PORTFOLIO ALLOCATOR", out)
        self.assertIn("not available", out)
        self.assertTrue(out.strip(), "fallback must be non-empty so it reaches the prompt")

    def test_empty_dict_returns_fallback_header(self):
        """Empty dict → same fallback as None."""
        out = self.format({})
        self.assertIn("not available", out)

    def test_populated_output_renders_content(self):
        """When output is populated, renders real content (not fallback)."""
        out = self.format({
            "weakest_incumbent":   {"symbol": "XBI", "thesis_score": 3, "health": "weak"},
            "strongest_candidate": {"symbol": "NVDA", "signal_score": 78.0, "direction": "bullish"},
            "proposed_actions":    [{"action": "REPLACE", "symbol": "NVDA", "exit_symbol": "XBI",
                                      "score_gap": 45.0}],
            "suppressed_actions":  [],
        })
        self.assertIn("XBI", out)
        self.assertIn("NVDA", out)
        self.assertIn("REPLACE", out)
        self.assertNotIn("not available", out)

    _MINIMAL_MD = {
        "vix":              20.0,
        "vix_regime":       "normal",
        "session":          "market",
        "market_status":    "open",
        "time_et":          "10:00 ET",
        "minutes_since_open": 30,
    }

    def test_build_user_prompt_receives_allocator_section(self):
        """build_user_prompt() accepts allocator_section and injects it into FULL prompt."""
        from bot_stage3_decision import build_user_prompt
        from unittest.mock import MagicMock

        acct = MagicMock()
        acct.equity         = 100_000.0
        acct.cash           = 50_000.0
        acct.buying_power   = 100_000.0
        acct.daytrade_count = 0

        prompt = build_user_prompt(
            account=acct,
            positions=[],
            md=self._MINIMAL_MD,
            session_tier="market",
            session_instruments="stocks",
            recent_decisions="none",
            ticker_lessons="none",
            allocator_section="=== PORTFOLIO ALLOCATOR SHADOW (advisory only) ===\n  TRIM XBI",
        )
        self.assertIn("PORTFOLIO ALLOCATOR", prompt)
        self.assertIn("TRIM XBI", prompt)

    def test_build_user_prompt_no_allocator_uses_fallback_header(self):
        """When allocator_section is the fallback string, it still appears in FULL prompt."""
        from bot_stage3_decision import build_user_prompt
        from portfolio_allocator import format_allocator_section
        from unittest.mock import MagicMock

        acct = MagicMock()
        acct.equity         = 100_000.0
        acct.cash           = 50_000.0
        acct.buying_power   = 100_000.0
        acct.daytrade_count = 0

        section = format_allocator_section(None)  # "not available" fallback
        prompt = build_user_prompt(
            account=acct,
            positions=[],
            md=self._MINIMAL_MD,
            session_tier="market",
            session_instruments="stocks",
            recent_decisions="none",
            ticker_lessons="none",
            allocator_section=section,
        )
        self.assertIn("PORTFOLIO ALLOCATOR", prompt)
        self.assertIn("not available", prompt)
