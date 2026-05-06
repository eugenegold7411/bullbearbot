"""
tests/test_debate_existing_position_context.py

Tests for _build_existing_position_context() and related debate context injection.

TEST 1 — No existing positions → clean "No existing positions" message
TEST 2 — Tracked structure present → shows structure type, legs, P&L
TEST 3 — Orphan position (in Alpaca, not in structures) → shows UNTRACKED label
TEST 4 — Mixed (tracked + orphan) → both shown with net P&L summary
TEST 5 — Alpaca fetch fails → graceful fallback message; debate still runs
TEST 6 — Alpaca fetch is cached per cycle (get_all_positions called once, not per-candidate)
"""

import json
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

_STUBS = {
    "dotenv":                      None,
    "anthropic":                   None,
    "alpaca":                      None,
    "alpaca.trading":              None,
    "alpaca.trading.client":       None,
    "alpaca.trading.requests":     None,
    "alpaca.trading.enums":        None,
    "alpaca.data":                 None,
    "alpaca.data.historical":      None,
    "alpaca.data.historical.news": None,
    "alpaca.data.requests":        None,
    "alpaca.data.timeframe":       None,
    "alpaca.data.enums":           None,
    "pandas":                      None,
    "yfinance":                    None,
}
for _name in _STUBS:
    if _name not in sys.modules:
        sys.modules[_name] = mock.MagicMock()


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_leg(occ_symbol, side="buy", qty=20, filled_price=3.20):
    """Return a minimal OptionsLeg-like object."""
    return SimpleNamespace(
        occ_symbol=occ_symbol,
        underlying=occ_symbol[:5].rstrip("0123456789"),
        side=side,
        qty=qty,
        filled_price=filled_price,
        option_type="call",
        strike=385.0,
        expiration="2026-06-20",
    )


def _make_structure(underlying, occ_syms, lifecycle="fully_filled",
                    strategy="call_debit_spread", pnl=-1100.0):
    """Return a minimal OptionsStructure-like object."""
    legs = []
    for i, (occ, side) in enumerate(occ_syms):
        legs.append(_make_leg(occ, side=side, qty=20))

    lc_mock = SimpleNamespace(value=lifecycle)
    st_mock = SimpleNamespace(value=strategy)

    return SimpleNamespace(
        underlying=underlying,
        lifecycle=lc_mock,
        strategy=st_mock,
        legs=legs,
        opened_at="2026-05-01T14:00:00+00:00",
        pnl_unrealized=pnl,
        is_open=lambda: lifecycle in ("fully_filled", "partially_filled"),
    )


def _make_alpaca_position(occ_symbol, qty=5, avg_entry=1.40,
                          unrealized_pl=210.0, unrealized_plpc=0.30):
    """Return a minimal Alpaca Position-like object."""
    return SimpleNamespace(
        symbol=occ_symbol,
        qty=str(qty),
        avg_entry_price=str(avg_entry),
        unrealized_pl=str(unrealized_pl),
        unrealized_plpc=str(unrealized_plpc),
    )


# ── Test helpers ──────────────────────────────────────────────────────────────

def _ctx(underlying, all_structures, alpaca_positions):
    from bot_options_stage3_debate import _build_existing_position_context
    return _build_existing_position_context(underlying, all_structures, alpaca_positions)


# ── Suite 1-5: Unit tests for _build_existing_position_context ────────────────

class TestBuildExistingPositionContext(unittest.TestCase):

    def test_1_no_positions_clean_message(self):
        """No tracked structures + empty Alpaca list → clean 'No existing positions' line."""
        result = _ctx("GOOGL", [], [])
        self.assertEqual(result, "No existing positions on GOOGL.")

    def test_2_tracked_structure_shown_in_context(self):
        """Fully-filled tracked structure → structure type, legs, P&L appear in context."""
        struct = _make_structure(
            "GOOGL",
            occ_syms=[
                ("GOOGL260605C00385000", "buy"),
                ("GOOGL260605C00390000", "sell"),
            ],
            lifecycle="fully_filled",
            strategy="call_debit_spread",
            pnl=-1100.0,
        )
        result = _ctx("GOOGL", [struct], [])

        self.assertIn("EXISTING POSITIONS ON GOOGL", result)
        self.assertIn("[TRACKED]", result)
        self.assertIn("call_debit_spread", result)
        self.assertIn("GOOGL260605C00385000", result)
        self.assertIn("GOOGL260605C00390000", result)
        self.assertIn("$-1,100.00", result)
        self.assertIn("fully_filled", result)

    def test_3_orphan_position_shown_untracked(self):
        """Alpaca position not covered by any structure leg → UNTRACKED label."""
        # No tracked structures for GOOGL
        orphan = _make_alpaca_position(
            "GOOGL260601P00370000", qty=-5, avg_entry=1.40,
            unrealized_pl=-340.0, unrealized_plpc=-0.123,
        )
        result = _ctx("GOOGL", [], [orphan])

        self.assertIn("EXISTING POSITIONS ON GOOGL", result)
        self.assertIn("[UNTRACKED]", result)
        self.assertIn("GOOGL260601P00370000", result)
        self.assertNotIn("[TRACKED]", result)

    def test_4_mixed_tracked_and_orphan_net_summary(self):
        """Tracked structure + orphan → both appear; net P&L summary is correct."""
        struct = _make_structure(
            "GOOGL",
            occ_syms=[
                ("GOOGL260605C00385000", "buy"),
                ("GOOGL260605C00390000", "sell"),
            ],
            pnl=-1100.0,
        )
        orphan = _make_alpaca_position(
            "GOOGL260601P00370000", qty=5, avg_entry=1.40,
            unrealized_pl=210.0, unrealized_plpc=0.30,
        )
        result = _ctx("GOOGL", [struct], [orphan])

        self.assertIn("[TRACKED]", result)
        self.assertIn("[UNTRACKED]", result)
        self.assertIn("GOOGL260601P00370000", result)
        # Net: -1100 + 210 = -890
        self.assertIn("NET:", result)
        self.assertIn("$-890.00", result)
        self.assertIn("2 tracked leg(s)", result)
        self.assertIn("1 untracked position(s)", result)

    def test_5_alpaca_fetch_fails_graceful_fallback(self):
        """alpaca_positions=None → fallback message; no crash."""
        result = _ctx("GOOGL", [], None)
        self.assertIn("Alpaca fetch failed", result)
        self.assertIn("GOOGL", result)
        # Should not raise

    def test_5b_alpaca_fetch_fails_with_tracked_structure(self):
        """alpaca_positions=None + existing tracked structure → structure shown + fetch note."""
        struct = _make_structure(
            "GOOGL",
            occ_syms=[("GOOGL260605C00385000", "buy")],
            pnl=-500.0,
        )
        result = _ctx("GOOGL", [struct], None)
        self.assertIn("[TRACKED]", result)
        self.assertIn("Alpaca fetch failed", result)

    def test_orphan_detection_skips_other_underlyings(self):
        """Alpaca positions for a different underlying are not flagged as orphans."""
        aapl_pos = _make_alpaca_position("AAPL260605C00200000", qty=10)
        result = _ctx("GOOGL", [], [aapl_pos])
        self.assertEqual(result, "No existing positions on GOOGL.")

    def test_tracked_occ_not_flagged_as_orphan(self):
        """A position whose OCC symbol appears in a tracked leg is not an orphan."""
        struct = _make_structure(
            "GOOGL",
            occ_syms=[("GOOGL260605C00385000", "buy")],
            pnl=0.0,
        )
        # Same OCC symbol also appears in Alpaca
        tracked_pos = _make_alpaca_position("GOOGL260605C00385000", qty=20)
        result = _ctx("GOOGL", [struct], [tracked_pos])
        # Should not show UNTRACKED for this position
        self.assertNotIn("[UNTRACKED]", result)
        self.assertIn("[TRACKED]", result)

    def test_submitted_structure_counts_as_active(self):
        """Submitted structure (not yet filled) still appears in context."""
        struct = _make_structure(
            "NVDA",
            occ_syms=[("NVDA260620C00120000", "buy")],
            lifecycle="submitted",
            pnl=None,
        )
        result = _ctx("NVDA", [struct], [])
        self.assertIn("[TRACKED]", result)
        self.assertIn("submitted", result)

    def test_closed_structure_not_shown_as_active(self):
        """Closed structure does not appear in tracked block (not active lifecycle)."""
        struct = _make_structure(
            "GOOGL",
            occ_syms=[("GOOGL260605C00385000", "buy")],
            lifecycle="closed",
            pnl=-200.0,
        )
        result = _ctx("GOOGL", [struct], [])
        # No active structures, no orphans → clean message
        self.assertEqual(result, "No existing positions on GOOGL.")


# ── Suite 6: Caching — get_all_positions called once per debate cycle ─────────

class TestAlpacaFetchCachedPerCycle(unittest.TestCase):
    """
    Verify get_all_positions() is called exactly once in run_bounded_debate(),
    regardless of how many candidate symbols are debated.
    """

    def _run_bounded_debate_with_mock_client(self, n_candidates=3):
        from bot_options_stage3_debate import run_bounded_debate
        from schemas import A2CandidateSet, A2FeaturePack

        fake_raw = json.dumps({
            "selected_candidate_id": None,
            "confidence": 0.6,
            "reject": True,
            "key_risks": [],
            "reasons": "test",
            "recommended_size_modifier": 1.0,
        })
        fake_usage = mock.MagicMock()
        fake_usage.input_tokens = 100
        fake_usage.output_tokens = 50
        fake_usage.cache_read_input_tokens = 0
        fake_usage.cache_creation_input_tokens = 0

        fake_resp = mock.MagicMock()
        fake_resp.content = [mock.MagicMock(text=fake_raw)]
        fake_resp.usage = fake_usage

        mock_claude = mock.MagicMock()
        mock_claude.messages.create.return_value = fake_resp

        mock_alpaca = mock.MagicMock()
        mock_alpaca.get_all_positions.return_value = []

        syms = [f"SYM{i}" for i in range(n_candidates)]
        candidate_structures = [
            {
                "candidate_id": f"cid{i}",
                "symbol": sym,
                "structure_type": "call_debit_spread",
                "expiry": "2026-06-20",
                "long_strike": 100.0,
                "debit": 2.0,
                "max_loss": 200.0,
                "dte": 30,
                "open_interest": 500,
            }
            for i, sym in enumerate(syms)
        ]

        pack = A2FeaturePack(
            symbol="SYM0",
            a1_signal_score=75.0,
            a1_direction="bullish",
            trend_score=None,
            momentum_score=None,
            sector_alignment="tech",
            iv_rank=30.0,
            iv_environment="cheap",
            term_structure_slope=None,
            skew=None,
            expected_move_pct=5.0,
            flow_imbalance_30m=None,
            sweep_count=None,
            gex_regime=None,
            oi_concentration=None,
            earnings_days_away=None,
            macro_event_flag=False,
            premium_budget_usd=5000.0,
            liquidity_score=0.7,
            built_at=datetime.now(timezone.utc).isoformat(),
            data_sources=["signal_scores"],
        )
        candidate_set = A2CandidateSet(
            symbol="SYM0",
            pack=pack,
            allowed_structures=["call_debit_spread"],
            router_rule_fired="RULE5",
            generated_candidates=[],
            vetoed_candidates=[],
            surviving_candidates=candidate_structures[:1],
            generation_errors=[],
            built_at=datetime.now(timezone.utc).isoformat(),
        )

        with mock.patch("bot_options_stage3_debate._get_claude", return_value=mock_claude), \
             mock.patch("bot_options_stage3_debate._load_opts_system", return_value="sys"), \
             mock.patch("bot_options_stage3_debate._log_claude_cost"), \
             mock.patch("bot_options_stage3_debate._load_strategy_config", return_value={}), \
             mock.patch("bot_options_stage3_debate.load_structures", return_value=[], create=True), \
             mock.patch("options_state.load_structures", return_value=[], create=True):
            run_bounded_debate(
                candidate_sets=[candidate_set],
                candidates=[],
                candidate_structures=candidate_structures,
                allowed_by_sym={sym: ["call_debit_spread"] for sym in syms},
                equity=100_000.0,
                vix=20.0,
                regime="normal",
                account1_summary="A1: no active trades",
                obs_mode=False,
                session_tier="market",
                iv_summaries={sym: {"iv_rank": 30, "iv_environment": "cheap"}
                              for sym in syms},
                t_start=time.monotonic(),
                config={},
                alpaca_client=mock_alpaca,
            )

        return mock_alpaca

    def test_6_get_all_positions_called_exactly_once_for_three_candidates(self):
        """get_all_positions must be called exactly once even with 3 candidates."""
        mock_alpaca = self._run_bounded_debate_with_mock_client(n_candidates=3)
        self.assertEqual(
            mock_alpaca.get_all_positions.call_count, 1,
            "get_all_positions should be fetched once per cycle, not once per candidate",
        )


if __name__ == "__main__":
    unittest.main()
