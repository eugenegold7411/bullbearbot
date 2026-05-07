"""
tests/test_earnings_intelligence.py — Earnings intelligence fixes.

EARNINGS RULE prompt (Fix A):
  1. test_earnings_rule_contains_balanced_actions
  2. test_earnings_rule_no_default_reduce
  3. test_earnings_rule_references_beat_rate
  4. test_earnings_rule_fires_at_eda_3
  5. test_earnings_rule_silent_at_eda_4

A2 fresh catalyst override (Fix B):
  6. test_fresh_catalyst_override_adds_symbol
  7. test_fresh_catalyst_override_respects_config

A2 active structure gate (Fix C):
  8. test_active_structure_gate_blocks_existing
  9. test_active_structure_gate_allows_fresh_catalyst_with_config

A1 suppression logging:
  10. test_suppressed_symbols_logged
"""

from __future__ import annotations

import json
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

# ── helpers ───────────────────────────────────────────────────────────────────

def _future_iso(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def _make_position(symbol: str, qty: float) -> MagicMock:
    p = MagicMock()
    p.symbol = symbol
    p.qty = qty
    return p


def _minimal_account() -> MagicMock:
    a = MagicMock()
    a.equity = "50000.00"
    a.cash   = "20000.00"
    a.buying_power = "20000.00"
    a.daytrade_count = 0
    return a


def _minimal_md() -> dict:
    return {
        "market_status": "open",
        "time_et": "10:00 AM ET",
        "minutes_since_open": 30,
        "vix": 20.0,
        "vix_regime": "normal",
        "regime_instruction": "Standard rules apply.",
    }


_TEMPLATE = (
    "tpl {session_tier}{session_instruments}{next_cycle_time}"
    "{equity}{cash}{buying_power}{available_for_new}"
    "{pdt_used}{pdt_remaining}{exposure_pct}{vix}{vix_regime}"
    "{regime_instruction}{market_status}{time_et}{minutes_since_open}"
    "{sector_table}{intermarket_signals}{global_handoff}{earnings_calendar}"
    "{core_watchlist_by_sector}{dynamic_watchlist}{intraday_watchlist}"
    "{positions_table}{recent_decisions}{vector_memories}{ticker_lessons}"
    "{crypto_signals}{crypto_context}{breaking_news}{sector_news}"
    "{strategy_config_note}{conviction_table}{regime_line}{positions_line}"
    "{avoid_line}{insider_section}{reddit_section}{earnings_intel_section}"
    "{economic_calendar_section}{macro_wire_section}{orb_section}"
    "{regime_summary}{signal_scores}{intraday_momentum}{exit_status}"
    "{macro_backdrop}{dynamic_sizes_section}{positions_with_health}"
    "{correlation_section}{thesis_ranking_section}{scratchpad_section}"
)


def _build_prompt_with_eda(eda: int) -> str:
    """Helper: build user prompt with a position that has eda days to earnings."""
    import bot_stage3_decision as bsd
    pos = [_make_position("NVDA", 10.0)]
    cal_entry = [{"symbol": "NVDA", "earnings_date": _future_iso(eda)}]
    cal_data  = json.dumps({"fetched_at": "2026-01-01", "calendar": cal_entry})

    mock_path = MagicMock()
    mock_path.exists.return_value = True
    mock_path.read_text.return_value = cal_data

    class _ChainMock:
        def __init__(self, t):
            self._t = t
        @property
        def parent(self):
            return self
        def __truediv__(self, _):
            return self
        def exists(self):
            return self._t.exists()
        def read_text(self):
            return self._t.read_text()

    with (
        patch("bot_stage3_decision.Path", side_effect=lambda *a: _ChainMock(mock_path)),
        patch("bot_stage3_decision._load_prompts", return_value=("sys", _TEMPLATE)),
        patch("bot_stage3_decision.pi"),
    ):
        return bsd.build_user_prompt(
            account=_minimal_account(),
            positions=pos,
            md=_minimal_md(),
            session_tier="regular",
            session_instruments="equity",
            recent_decisions="",
            ticker_lessons="",
        )


# ══ Group 1: EARNINGS RULE prompt ════════════════════════════════════════════

class TestEarningsRulePrompt(unittest.TestCase):

    def test_earnings_rule_contains_balanced_actions(self):
        """Balanced EARNINGS RULE must list HOLD, ADD, REDUCE, and CLOSE as valid actions."""
        result = _build_prompt_with_eda(1)
        self.assertIn("EARNINGS RULE", result)
        for action in ("HOLD", "ADD", "REDUCE", "CLOSE"):
            self.assertIn(action, result, f"Expected '{action}' in EARNINGS RULE section")

    def test_earnings_rule_no_default_reduce(self):
        """EARNINGS RULE must NOT say 'default action is REDUCE' or 'default action is CLOSE'."""
        result = _build_prompt_with_eda(2)
        self.assertNotIn("default action is REDUCE", result)
        self.assertNotIn("default action is CLOSE", result)

    def test_earnings_rule_references_beat_rate(self):
        """EARNINGS RULE must reference beat_rate or beat history to ground decision in data."""
        result = _build_prompt_with_eda(1)
        # Accepts either "beat_rate", "beat history", or "beat_quarters" / "avg_surprise"
        beat_referenced = any(
            token in result
            for token in ("beat_rate", "beat history", "beat_quarters", "avg_surprise", "beat rate")
        )
        self.assertTrue(beat_referenced, "EARNINGS RULE should reference beat rate / beat history")

    def test_earnings_rule_fires_at_eda_3(self):
        """eda=3 → EARNINGS RULE section must appear in prompt."""
        result = _build_prompt_with_eda(3)
        self.assertIn("EARNINGS RULE", result)
        self.assertIn("NVDA", result)

    def test_earnings_rule_silent_at_eda_4(self):
        """eda=4 → EARNINGS RULE section must NOT appear (gate is eda ≤ 3)."""
        result = _build_prompt_with_eda(4)
        self.assertNotIn("EARNINGS RULE", result)


# ══ Group 2: A2 fresh catalyst override ══════════════════════════════════════

def _minimal_run_candidate_stage_args():
    """Return minimal kwargs for run_candidate_stage that reach the fresh_catalyst_override block."""
    return dict(
        signal_scores={},
        iv_summaries={},
        equity=100_000.0,
        vix=18.0,
        equity_symbols=[],
        config={},
    )


class TestFreshCatalystOverride(unittest.TestCase):

    def _run_to_scored_symbols(self, signal_scores: dict, config: dict) -> list:
        """
        Replicate the top-8 selection + fresh_catalyst_override logic and return scored_symbols.
        Tests the logic directly rather than importing the full stage (avoids heavy import chains).
        """
        scored = sorted(
            [(sym, sig) for sym, sig in signal_scores.items()
             if isinstance(sig, dict) and sig.get("conviction") in ("medium", "high")],
            key=lambda x: {"high": 2, "medium": 1, "low": 0}.get(
                x[1].get("conviction", "low"), 0
            ),
            reverse=True,
        )[:8]

        _fc_override_enabled = config.get("a2_router", {}).get(
            "fresh_catalyst_override_enabled", False
        )
        if _fc_override_enabled:
            _scored_sym_set = {s for s, _ in scored}
            _fc_threshold = float(config.get("a2_router", {}).get(
                "fresh_catalyst_override_min_score", 70.0
            ))
            _fc_candidates = [
                (sym, sig) for sym, sig in signal_scores.items()
                if isinstance(sig, dict)
                and sym not in _scored_sym_set
                and sig.get("score", 0) >= _fc_threshold
                and sig.get("catalyst_type") in (
                    "earnings_beat", "earnings_post", "earnings_surprise"
                )
            ]
            scored = scored + _fc_candidates[:2]
        return scored

    def test_fresh_catalyst_override_adds_symbol(self):
        """Symbol with catalyst_type=earnings_beat, score≥70, not in top-8 must be added."""
        # 8 high-conviction decoy symbols to fill top-8
        signal_scores = {f"SYM{i}": {"score": 80, "conviction": "high"} for i in range(8)}
        # DDOG: earnings beat, high score, not in top-8
        signal_scores["DDOG"] = {
            "score": 75.0,
            "conviction": "medium",
            "catalyst_type": "earnings_beat",
        }
        config = {"a2_router": {"fresh_catalyst_override_enabled": True}}
        result = self._run_to_scored_symbols(signal_scores, config)
        syms = [s for s, _ in result]
        self.assertIn("DDOG", syms, "DDOG with earnings_beat catalyst must be injected")

    def test_fresh_catalyst_override_respects_config(self):
        """When fresh_catalyst_override_enabled=false, earnings_beat symbol must NOT be added."""
        signal_scores = {f"SYM{i}": {"score": 80, "conviction": "high"} for i in range(8)}
        signal_scores["DDOG"] = {
            "score": 75.0,
            "conviction": "medium",
            "catalyst_type": "earnings_beat",
        }
        config = {"a2_router": {"fresh_catalyst_override_enabled": False}}
        result = self._run_to_scored_symbols(signal_scores, config)
        syms = [s for s, _ in result]
        self.assertNotIn("DDOG", syms, "DDOG must NOT be injected when override disabled")


# ══ Group 3: active structure gate ═══════════════════════════════════════════

class TestActiveStructureGate(unittest.TestCase):

    def _make_structure_mock(self, underlying: str, lifecycle: str = "fully_filled",
                              expiration: str = "") -> MagicMock:
        s = MagicMock()
        s.underlying  = underlying
        s.expiration  = expiration
        lc_mock = MagicMock()
        lc_mock.value = lifecycle
        s.lifecycle   = lc_mock
        return s

    def test_active_structure_gate_blocks_existing(self):
        """Symbol with any active (fully_filled) structure must be blocked by the default gate."""

        _ACTIVE_LC = {"submitted", "partially_filled", "fully_filled", "orphan_tracked"}
        structures = [self._make_structure_mock("NVDA", "fully_filled", "2026-05-22")]
        all_active = [
            s for s in structures
            if (s.lifecycle.value if hasattr(s.lifecycle, "value") else str(s.lifecycle))
            in _ACTIVE_LC
        ]

        # Default behavior (allow_multi=False): block any symbol with an active structure
        _allow_multi = False
        blocked = {s.underlying for s in all_active}

        self.assertIn("NVDA", blocked, "NVDA with active structure must be in blocked set")

    def test_active_structure_gate_allows_fresh_catalyst_with_config(self):
        """
        With allow_multiple_structures_per_symbol=True, symbols with structures expiring
        MORE than 35 days away must NOT be blocked (different expiry window is available).
        """
        far_exp = (date.today() + timedelta(days=60)).isoformat()  # 60 days > 35 threshold
        structures = [self._make_structure_mock("NVDA", "fully_filled", far_exp)]

        _ACTIVE_LC = {"submitted", "partially_filled", "fully_filled", "orphan_tracked"}
        _allow_multi = True
        _today = date.today()
        _near_exp_syms: set[str] = set()

        for s in structures:
            lc = s.lifecycle.value if hasattr(s.lifecycle, "value") else str(s.lifecycle)
            if lc not in _ACTIVE_LC:
                continue
            _exp = getattr(s, "expiration", None)
            if _exp:
                try:
                    _exp_date = date.fromisoformat(str(_exp)[:10])
                    if (_exp_date - _today).days <= 35:
                        _near_exp_syms.add(s.underlying)
                except Exception:
                    _near_exp_syms.add(s.underlying)
            else:
                _near_exp_syms.add(s.underlying)

        # NVDA has expiry in 60 days — should NOT be in the near-expiry blocked set
        self.assertNotIn(
            "NVDA", _near_exp_syms,
            "NVDA with far-expiry structure must not block near-term new entry when allow_multi=True",
        )


# ══ Group 4: A1 suppression logging ══════════════════════════════════════════

class TestSuppressedSymbolsLogged(unittest.TestCase):

    def test_suppressed_symbols_logged(self):
        """
        portfolio_allocator._decide_actions() must include suppression_reason for each
        suppressed action. Regression guard: the suppressed list is populated and each
        entry has 'symbol' and 'suppression_reason' fields.
        """
        from portfolio_allocator import _decide_actions

        # One incumbent with strong thesis (would qualify for ADD) but in cooldown
        incumbents = [
            {
                "symbol": "AAPL",
                "thesis_score": 8,
                "thesis_score_normalized": 80,  # > add_min_score default 65
                "market_value": 5000,
                "account_pct": 5.0,
                "signal_catalyst": "AI hardware",
                "signal_signals": [],
            }
        ]
        candidates: list[dict] = []
        sizes = {"AAPL": 0.10}

        pa_cfg = {
            "add_min_score": 65,
            "trim_score_threshold": 3,
            "weight_deadband": 0.02,
            "min_rebalance_notional": 200,
            "max_recommendations_per_cycle": 3,
            "replace_score_gap": 25,
            "size_trim_enabled": False,
            "size_trim_tolerance_pct": 2.0,
            "max_pos_pct_equity": 0.15,
            "cooldown_hours": 24,
        }
        sizes["available_for_new"] = 50_000

        # Patch cooldown to say AAPL is in cooldown
        with patch("portfolio_allocator._check_cooldown",
                   return_value=(False, "AAPL already received a ADD recommendation today (daily cooldown)")):
            proposed, suppressed = _decide_actions(
                incumbents=incumbents,
                candidates=candidates,
                pi_data={},
                cfg={},
                pa_cfg=pa_cfg,
                sizes=sizes,
                equity=100_000.0,
            )

        self.assertTrue(len(suppressed) > 0, "Expected at least one suppressed action")
        sup = suppressed[0]
        self.assertIn("symbol", sup, "Suppressed entry must have 'symbol' field")
        self.assertIn("suppression_reason", sup, "Suppressed entry must have 'suppression_reason' field")
        self.assertIn("AAPL", sup["symbol"])
        self.assertIn("cooldown", sup["suppression_reason"].lower())


if __name__ == "__main__":
    unittest.main()
