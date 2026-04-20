"""
tests/test_a2_feature_pack.py — Suite 29: A2 decision core migration tests.

Covers:
  - A2FeaturePack construction
  - _route_strategy() for each rule condition
  - options_universe_manager.is_tradeable() queues symbol when IV history missing
  - options_universe_manager.run_bootstrap_queue() respects 5-symbol limit
  - options_universe_manager.initialize_universe_from_existing_iv_history() idempotent
"""

import json
import sys
import os
import tempfile
import unittest
from datetime import datetime, date, timezone, timedelta
from pathlib import Path
from unittest import mock

_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

os.chdir(_BOT_DIR)

# Stub third-party packages absent from local (non-venv) environments.
# Only packages that are truly unavailable here — local modules (preflight,
# log_setup, options_*) are on-disk and importable without side-effects.
import logging as _logging

_THIRD_PARTY_STUBS = {
    "dotenv":                   None,
    "anthropic":                None,
    "alpaca":                   None,
    "alpaca.trading":           None,
    "alpaca.trading.client":    None,
    "alpaca.trading.requests":  None,
    "alpaca.trading.enums":     None,
}

for _stub_name in _THIRD_PARTY_STUBS:
    if _stub_name not in sys.modules:
        _m = mock.MagicMock()
        if _stub_name == "dotenv":
            _m.load_dotenv = mock.MagicMock()
        sys.modules[_stub_name] = _m


# ════════════════════════════════════════════════════════════════════════════
# SUITE 29a — A2FeaturePack construction
# ════════════════════════════════════════════════════════════════════════════

class TestA2FeaturePackConstruction(unittest.TestCase):

    def _make_pack(self, **overrides):
        from schemas import A2FeaturePack
        defaults = dict(
            symbol="AAPL",
            a1_signal_score=72.0,
            a1_direction="bullish",
            trend_score=None,
            momentum_score=None,
            sector_alignment="tech",
            iv_rank=28.0,
            iv_environment="cheap",
            term_structure_slope=None,
            skew=None,
            expected_move_pct=5.2,
            flow_imbalance_30m=None,
            sweep_count=None,
            gex_regime=None,
            oi_concentration=None,
            earnings_days_away=None,
            macro_event_flag=False,
            premium_budget_usd=5000.0,
            liquidity_score=0.75,
            built_at=datetime.now(timezone.utc).isoformat(),
            data_sources=["signal_scores", "iv_history"],
        )
        defaults.update(overrides)
        return A2FeaturePack(**defaults)

    def test_construction_valid_inputs(self):
        pack = self._make_pack()
        self.assertEqual(pack.symbol, "AAPL")
        self.assertEqual(pack.iv_rank, 28.0)
        self.assertEqual(pack.iv_environment, "cheap")
        self.assertIsNone(pack.flow_imbalance_30m)
        self.assertIsNone(pack.sweep_count)
        self.assertIsNone(pack.gex_regime)
        self.assertIsNone(pack.oi_concentration)

    def test_uw_fields_all_none(self):
        """All UW-sourced fields must default to None (Phase 2)."""
        pack = self._make_pack()
        self.assertIsNone(pack.flow_imbalance_30m)
        self.assertIsNone(pack.sweep_count)
        self.assertIsNone(pack.gex_regime)
        self.assertIsNone(pack.oi_concentration)

    def test_data_sources_list(self):
        pack = self._make_pack(data_sources=["signal_scores", "iv_history", "options_chain"])
        self.assertIn("signal_scores", pack.data_sources)
        self.assertIn("iv_history", pack.data_sources)
        self.assertIn("options_chain", pack.data_sources)

    def test_earnings_days_away_optional(self):
        pack_no_earn = self._make_pack(earnings_days_away=None)
        self.assertIsNone(pack_no_earn.earnings_days_away)
        pack_earn = self._make_pack(earnings_days_away=3)
        self.assertEqual(pack_earn.earnings_days_away, 3)

    def test_macro_event_flag_bool(self):
        pack_macro = self._make_pack(macro_event_flag=True, iv_rank=65.0)
        self.assertTrue(pack_macro.macro_event_flag)


# ════════════════════════════════════════════════════════════════════════════
# SUITE 29b — _route_strategy() deterministic rules
# ════════════════════════════════════════════════════════════════════════════

class TestRouteStrategy(unittest.TestCase):

    def _make_pack(self, **overrides):
        from schemas import A2FeaturePack
        defaults = dict(
            symbol="NVDA",
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
            liquidity_score=0.75,
            built_at=datetime.now(timezone.utc).isoformat(),
            data_sources=["signal_scores", "iv_history"],
        )
        defaults.update(overrides)
        return A2FeaturePack(**defaults)

    def _route(self, **kw):
        from bot_options import _route_strategy
        return _route_strategy(self._make_pack(**kw))

    def test_rule1_earnings_blackout(self):
        """Earnings within 5 days → block."""
        self.assertEqual(self._route(earnings_days_away=5), [])
        self.assertEqual(self._route(earnings_days_away=1), [])
        self.assertEqual(self._route(earnings_days_away=0), [])

    def test_rule1_earnings_clear(self):
        """Earnings > 5 days → not blocked by rule 1."""
        result = self._route(earnings_days_away=6)
        self.assertNotEqual(result, [])

    def test_rule1_earnings_none(self):
        """earnings_days_away=None → not blocked by rule 1."""
        result = self._route(earnings_days_away=None)
        self.assertNotEqual(result, [])

    def test_rule2_very_expensive(self):
        """very_expensive IV → block."""
        self.assertEqual(self._route(iv_environment="very_expensive"), [])

    def test_rule3_low_liquidity(self):
        """Liquidity score < 0.3 → block."""
        self.assertEqual(self._route(liquidity_score=0.2), [])
        self.assertEqual(self._route(liquidity_score=0.0), [])

    def test_rule3_sufficient_liquidity(self):
        """Liquidity score >= 0.3 → not blocked by rule 3."""
        result = self._route(liquidity_score=0.3)
        self.assertNotEqual(result, [])

    def test_rule4_macro_elevated_iv(self):
        """Macro event flag + iv_rank > 60 → block."""
        self.assertEqual(self._route(macro_event_flag=True, iv_rank=61.0), [])
        self.assertEqual(self._route(macro_event_flag=True, iv_rank=80.0), [])

    def test_rule4_macro_low_iv_passes(self):
        """Macro event but iv_rank <= 60 → not blocked by rule 4."""
        result = self._route(macro_event_flag=True, iv_rank=60.0,
                             iv_environment="neutral")
        # rule 4 requires iv_rank > 60; 60 == 60 doesn't trigger
        self.assertNotEqual(result, [])

    def test_rule5_cheap_bullish(self):
        allowed = self._route(iv_environment="cheap", a1_direction="bullish")
        self.assertIn("long_call", allowed)
        self.assertIn("debit_call_spread", allowed)

    def test_rule5_very_cheap_bearish(self):
        allowed = self._route(iv_environment="very_cheap", a1_direction="bearish")
        self.assertIn("long_put", allowed)
        self.assertIn("debit_put_spread", allowed)

    def test_rule5_neutral_direction_blocked(self):
        """cheap IV + neutral direction → rule 5 doesn't fire → default block."""
        result = self._route(iv_environment="cheap", a1_direction="neutral")
        self.assertEqual(result, [])

    def test_rule6_neutral_iv_directional(self):
        allowed = self._route(iv_environment="neutral", a1_direction="bullish")
        self.assertIn("debit_call_spread", allowed)
        self.assertNotIn("long_call", allowed)

    def test_rule7_expensive_directional(self):
        allowed = self._route(iv_environment="expensive", a1_direction="bearish")
        self.assertIn("debit_put_spread", allowed)
        self.assertNotIn("long_put", allowed)

    def test_rule8_default_block(self):
        """No matching rule → empty list."""
        result = self._route(iv_environment="neutral", a1_direction="neutral")
        self.assertEqual(result, [])


# ════════════════════════════════════════════════════════════════════════════
# SUITE 29c — options_universe_manager
# ════════════════════════════════════════════════════════════════════════════

class TestOptionsUniverseManager(unittest.TestCase):
    """All tests use a temp directory to avoid polluting data/options/."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._patch_paths()

    def _patch_paths(self):
        """Redirect module-level path constants to temp dir."""
        import options_universe_manager as oum
        self._orig_data   = oum._DATA_DIR
        self._orig_univ   = oum._UNIVERSE_FILE
        self._orig_queue  = oum._BOOTSTRAP_QUEUE
        self._orig_iv_dir = oum._IV_DIR

        oum._DATA_DIR        = Path(self.tmpdir)
        oum._UNIVERSE_FILE   = Path(self.tmpdir) / "universe.json"
        oum._BOOTSTRAP_QUEUE = Path(self.tmpdir) / "iv_pending_bootstrap.json"
        oum._IV_DIR          = Path(self.tmpdir) / "iv_history"
        oum._IV_DIR.mkdir(exist_ok=True)

    def tearDown(self):
        import options_universe_manager as oum
        oum._DATA_DIR        = self._orig_data
        oum._UNIVERSE_FILE   = self._orig_univ
        oum._BOOTSTRAP_QUEUE = self._orig_queue
        oum._IV_DIR          = self._orig_iv_dir
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_iv_history(self, symbol: str, n_entries: int = 25):
        """Write synthetic IV history for symbol."""
        import options_universe_manager as oum
        hist = [
            {"date": (date.today() - timedelta(days=i)).isoformat(), "iv": 0.25 + i * 0.001}
            for i in range(n_entries)
        ]
        (oum._IV_DIR / f"{symbol}_iv_history.json").write_text(json.dumps(hist))

    # ── is_tradeable ─────────────────────────────────────────────────────────

    def test_is_tradeable_no_iv_history_queues_bootstrap(self):
        """Symbol with no IV history returns False and queues bootstrap."""
        from options_universe_manager import is_tradeable, _BOOTSTRAP_QUEUE
        result = is_tradeable("UNKNOWNSYM")
        self.assertFalse(result)
        queue = json.loads(_BOOTSTRAP_QUEUE.read_text())
        self.assertIn("UNKNOWNSYM", queue.get("pending", {}))

    def test_is_tradeable_with_iv_history_returns_true(self):
        """Symbol with sufficient IV history is tradeable."""
        from options_universe_manager import is_tradeable
        self._write_iv_history("SPY", 25)
        self.assertTrue(is_tradeable("SPY"))

    def test_is_tradeable_insufficient_history_returns_false(self):
        """Symbol with < 20 entries is not tradeable."""
        from options_universe_manager import is_tradeable
        self._write_iv_history("THINSTOCK", 10)
        self.assertFalse(is_tradeable("THINSTOCK"))

    def test_is_tradeable_in_universe_returns_true(self):
        """Symbol already in universe with bootstrap_complete returns True."""
        import options_universe_manager as oum
        self._write_iv_history("QQQ", 25)
        oum._UNIVERSE_FILE.write_text(json.dumps({
            "symbols": {"QQQ": {"bootstrap_complete": True, "added_at": "2026-01-01T00:00:00+00:00", "source": "test"}},
            "created_at": "2026-01-01T00:00:00+00:00",
        }))
        self.assertTrue(oum.is_tradeable("QQQ"))

    # ── run_bootstrap_queue ───────────────────────────────────────────────────

    def test_run_bootstrap_queue_respects_5_symbol_limit(self):
        """Only up to 5 symbols are processed per run."""
        import options_universe_manager as oum

        symbols = ["S1", "S2", "S3", "S4", "S5", "S6", "S7"]
        pending = {s: {"source": "test", "queued_at": "2026-01-01T00:00:00+00:00"} for s in symbols}
        oum._BOOTSTRAP_QUEUE.write_text(json.dumps({"pending": pending}))

        called_with: list[list] = []

        def _mock_seed(syms, **kw):
            called_with.append(list(syms))
            return {"seeded": list(syms), "skipped": [], "failed": []}

        _fake_seeder = mock.MagicMock()
        _fake_seeder.seed_iv_history = _mock_seed

        with mock.patch.dict("sys.modules", {"iv_history_seeder": _fake_seeder}):
            with mock.patch("options_universe_manager._has_sufficient_iv_history", return_value=True):
                result = oum.run_bootstrap_queue()

        self.assertTrue(len(called_with) > 0, "seed_iv_history was never called")
        total_processed = len(called_with[0])
        self.assertLessEqual(total_processed, oum._MAX_DAILY_BOOTSTRAPS)

    def test_run_bootstrap_queue_empty_queue(self):
        """Empty queue returns no-op result."""
        import options_universe_manager as oum
        oum._BOOTSTRAP_QUEUE.write_text(json.dumps({"pending": {}}))
        result = oum.run_bootstrap_queue()
        self.assertEqual(result["bootstrapped"], [])
        self.assertEqual(result["failed"], [])

    def test_run_bootstrap_queue_no_queue_file(self):
        """Missing queue file returns no-op result."""
        from options_universe_manager import run_bootstrap_queue
        result = run_bootstrap_queue()
        self.assertEqual(result["bootstrapped"], [])

    # ── initialize_universe_from_existing_iv_history ─────────────────────────

    def test_initialize_idempotent(self):
        """Calling initialize twice yields same universe (no duplicate entries)."""
        from options_universe_manager import initialize_universe_from_existing_iv_history, get_universe
        self._write_iv_history("AAPL", 25)
        self._write_iv_history("MSFT", 25)

        initialize_universe_from_existing_iv_history()
        uni1 = get_universe()
        count1 = len(uni1.get("symbols", {}))

        initialize_universe_from_existing_iv_history()
        uni2 = get_universe()
        count2 = len(uni2.get("symbols", {}))

        self.assertEqual(count1, count2)
        self.assertIn("AAPL", uni2["symbols"])
        self.assertIn("MSFT", uni2["symbols"])

    def test_initialize_only_includes_sufficient_history(self):
        """Symbols with < 20 entries are not added to the universe."""
        from options_universe_manager import initialize_universe_from_existing_iv_history, get_universe
        self._write_iv_history("GOOD", 25)
        self._write_iv_history("SPARSE", 5)

        initialize_universe_from_existing_iv_history()
        uni = get_universe()
        self.assertIn("GOOD", uni.get("symbols", {}))
        self.assertNotIn("SPARSE", uni.get("symbols", {}))

    def test_initialize_marks_bootstrap_complete(self):
        """All universe entries have bootstrap_complete=True."""
        from options_universe_manager import initialize_universe_from_existing_iv_history, get_universe
        self._write_iv_history("GLD", 25)

        initialize_universe_from_existing_iv_history()
        uni = get_universe()
        self.assertTrue(uni["symbols"]["GLD"]["bootstrap_complete"])
        self.assertEqual(uni["symbols"]["GLD"]["source"], "grandfathered")


# ════════════════════════════════════════════════════════════════════════════
# SUITE 29d — generate_candidate_structures
# ════════════════════════════════════════════════════════════════════════════

class TestGenerateCandidateStructures(unittest.TestCase):

    def _make_pack(self, **kw):
        from schemas import A2FeaturePack
        defaults = dict(
            symbol="NVDA", a1_signal_score=75.0, a1_direction="bullish",
            trend_score=None, momentum_score=None, sector_alignment="tech",
            iv_rank=30.0, iv_environment="cheap", term_structure_slope=None,
            skew=None, expected_move_pct=5.0, flow_imbalance_30m=None,
            sweep_count=None, gex_regime=None, oi_concentration=None,
            earnings_days_away=None, macro_event_flag=False,
            premium_budget_usd=5000.0, liquidity_score=0.75,
            built_at=datetime.now(timezone.utc).isoformat(),
            data_sources=["signal_scores", "iv_history"],
        )
        defaults.update(kw)
        return A2FeaturePack(**defaults)

    def _make_chain(self, symbol="NVDA", spot=100.0):
        exp = (date.today() + timedelta(days=14)).isoformat()
        atm  = {"strike": 100.0, "bid": 1.00, "ask": 1.20, "impliedVolatility": 0.30,
                "volume": 500, "openInterest": 1000, "delta": 0.50, "theta": -0.04}
        otm  = {"strike": 105.0, "bid": 0.50, "ask": 0.70, "impliedVolatility": 0.28,
                "volume": 300, "openInterest": 600, "delta": 0.30, "theta": -0.02}
        return {
            "symbol": symbol,
            "current_price": spot,
            "expirations": {exp: {"calls": [atm, otm], "puts": [atm, otm]}},
        }

    def test_candidate_has_required_schema_fields(self):
        from options_intelligence import generate_candidate_structures
        pack = self._make_pack()
        chain = self._make_chain()
        cands = generate_candidate_structures(
            pack=pack, allowed_structures=["long_call", "debit_call_spread"],
            equity=100_000.0, chain=chain,
        )
        self.assertGreater(len(cands), 0)
        required = [
            "candidate_id", "structure_type", "symbol", "expiry",
            "long_strike", "contracts", "debit", "max_loss", "breakeven", "dte",
        ]
        for field in required:
            self.assertIn(field, cands[0], f"Missing field: {field}")

    def test_empty_chain_returns_no_candidates(self):
        from options_intelligence import generate_candidate_structures
        pack = self._make_pack()
        cands = generate_candidate_structures(
            pack=pack, allowed_structures=["long_call"],
            equity=100_000.0, chain={},
        )
        self.assertEqual(cands, [])

    def test_unknown_structure_type_skipped(self):
        from options_intelligence import generate_candidate_structures
        pack = self._make_pack()
        chain = self._make_chain()
        cands = generate_candidate_structures(
            pack=pack, allowed_structures=["iron_condor"],
            equity=100_000.0, chain=chain,
        )
        self.assertEqual(cands, [])

    def test_structure_type_recorded_correctly(self):
        from options_intelligence import generate_candidate_structures
        pack = self._make_pack()
        chain = self._make_chain()
        cands = generate_candidate_structures(
            pack=pack, allowed_structures=["long_call"],
            equity=100_000.0, chain=chain,
        )
        if cands:
            self.assertEqual(cands[0]["structure_type"], "long_call")
            self.assertEqual(cands[0]["symbol"], "NVDA")

    def test_debit_spread_has_short_strike(self):
        from options_intelligence import generate_candidate_structures
        pack = self._make_pack()
        chain = self._make_chain()
        cands = generate_candidate_structures(
            pack=pack, allowed_structures=["debit_call_spread"],
            equity=100_000.0, chain=chain,
        )
        if cands:
            self.assertIsNotNone(cands[0]["short_strike"])

    def test_dte_positive(self):
        from options_intelligence import generate_candidate_structures
        pack = self._make_pack()
        chain = self._make_chain()
        cands = generate_candidate_structures(
            pack=pack, allowed_structures=["long_call"],
            equity=100_000.0, chain=chain,
        )
        if cands:
            self.assertGreater(cands[0]["dte"], 0)


# ════════════════════════════════════════════════════════════════════════════
# SUITE 29e — _apply_veto_rules
# ════════════════════════════════════════════════════════════════════════════

class TestApplyVetoRules(unittest.TestCase):

    def _make_pack(self):
        from schemas import A2FeaturePack
        return A2FeaturePack(
            symbol="NVDA", a1_signal_score=75.0, a1_direction="bullish",
            trend_score=None, momentum_score=None, sector_alignment="tech",
            iv_rank=30.0, iv_environment="cheap", term_structure_slope=None,
            skew=None, expected_move_pct=5.0, flow_imbalance_30m=None,
            sweep_count=None, gex_regime=None, oi_concentration=None,
            earnings_days_away=None, macro_event_flag=False,
            premium_budget_usd=5000.0, liquidity_score=0.75,
            built_at=datetime.now(timezone.utc).isoformat(),
            data_sources=["signal_scores"],
        )

    def _base(self, **kw):
        cand = {
            "candidate_id": "abc123", "structure_type": "long_call",
            "symbol": "NVDA", "expiry": "2026-05-04",
            "long_strike": 100.0, "short_strike": None, "contracts": 1,
            "debit": 1.50, "max_loss": 150.0, "max_gain": None,
            "breakeven": 101.50, "delta": 0.50, "theta": -0.05, "vega": 0.10,
            "probability_profit": 0.50, "expected_value": 50.0,
            "liquidity_score": 0.75, "bid_ask_spread_pct": 0.03,
            "open_interest": 500, "dte": 14,
        }
        cand.update(kw)
        return cand

    def test_passes_all_rules(self):
        from bot_options import _apply_veto_rules
        self.assertIsNone(_apply_veto_rules(self._base(), self._make_pack(), 100_000.0))

    def test_v1_wide_bid_ask_spread(self):
        from bot_options import _apply_veto_rules
        result = _apply_veto_rules(self._base(bid_ask_spread_pct=0.06), self._make_pack(), 100_000.0)
        self.assertIsNotNone(result)
        self.assertIn("bid_ask", result)

    def test_v1_exactly_at_limit_passes(self):
        from bot_options import _apply_veto_rules
        self.assertIsNone(
            _apply_veto_rules(self._base(bid_ask_spread_pct=0.05), self._make_pack(), 100_000.0)
        )

    def test_v2_low_open_interest(self):
        from bot_options import _apply_veto_rules
        result = _apply_veto_rules(self._base(open_interest=50), self._make_pack(), 100_000.0)
        self.assertIsNotNone(result)
        self.assertIn("open_interest", result)

    def test_v2_oi_none_skips_rule(self):
        from bot_options import _apply_veto_rules
        self.assertIsNone(
            _apply_veto_rules(self._base(open_interest=None), self._make_pack(), 100_000.0)
        )

    def test_v3_theta_decay_too_high(self):
        from bot_options import _apply_veto_rules
        # |theta|/debit = 0.10/1.0 = 0.10 > 0.05
        result = _apply_veto_rules(self._base(theta=-0.10, debit=1.0), self._make_pack(), 100_000.0)
        self.assertIsNotNone(result)
        self.assertIn("theta", result)

    def test_v3_theta_none_skips_rule(self):
        from bot_options import _apply_veto_rules
        self.assertIsNone(
            _apply_veto_rules(self._base(theta=None), self._make_pack(), 100_000.0)
        )

    def test_v4_max_loss_exceeds_equity_pct(self):
        from bot_options import _apply_veto_rules
        # max_loss=4000 > 100000*0.03=3000
        result = _apply_veto_rules(self._base(max_loss=4000.0), self._make_pack(), 100_000.0)
        self.assertIsNotNone(result)
        self.assertIn("max_loss", result)

    def test_v4_max_loss_at_limit_passes(self):
        from bot_options import _apply_veto_rules
        self.assertIsNone(
            _apply_veto_rules(self._base(max_loss=3000.0), self._make_pack(), 100_000.0)
        )

    def test_v5_dte_too_short(self):
        from bot_options import _apply_veto_rules
        result = _apply_veto_rules(self._base(dte=3), self._make_pack(), 100_000.0)
        self.assertIsNotNone(result)
        self.assertIn("dte", result)

    def test_v5_dte_exactly_5_passes(self):
        from bot_options import _apply_veto_rules
        self.assertIsNone(
            _apply_veto_rules(self._base(dte=5), self._make_pack(), 100_000.0)
        )

    def test_v6_negative_expected_value(self):
        from bot_options import _apply_veto_rules
        result = _apply_veto_rules(self._base(expected_value=-100.0), self._make_pack(), 100_000.0)
        self.assertIsNotNone(result)
        self.assertIn("expected_value", result)

    def test_v6_ev_none_skips_rule(self):
        from bot_options import _apply_veto_rules
        self.assertIsNone(
            _apply_veto_rules(self._base(expected_value=None), self._make_pack(), 100_000.0)
        )


# ════════════════════════════════════════════════════════════════════════════
# SUITE 29f — flow_imbalance_30m in A2FeaturePack
# ════════════════════════════════════════════════════════════════════════════

class TestFlowImbalance(unittest.TestCase):

    def _make_chain(self, call_vol: int, put_vol: int, spot: float = 100.0):
        exp = (date.today() + timedelta(days=14)).isoformat()
        call = {"strike": 100.0, "bid": 1.0, "ask": 1.2, "volume": call_vol,
                "openInterest": 500, "impliedVolatility": 0.30}
        put  = {"strike": 100.0, "bid": 1.0, "ask": 1.2, "volume": put_vol,
                "openInterest": 500, "impliedVolatility": 0.30}
        return {
            "symbol": "SPY", "current_price": spot,
            "expirations": {exp: {"calls": [call], "puts": [put]}},
        }

    def _make_iv(self):
        return {"iv_environment": "cheap", "iv_rank": 25.0, "current_iv": 0.25,
                "observation_mode": False, "history_days": 30}

    def _make_sig(self):
        return {"score": 70, "direction": "bullish", "conviction": "high",
                "tier": "core", "primary_catalyst": "test", "price": 100.0}

    def test_bullish_flow_positive(self):
        """More call volume than put → positive flow_imbalance_30m."""
        from bot_options import _build_a2_feature_pack
        chain = self._make_chain(call_vol=800, put_vol=200)
        pack = _build_a2_feature_pack(
            symbol="SPY",
            signal_scores={"SPY": self._make_sig()},
            iv_summaries={"SPY": self._make_iv()},
            equity=100_000.0, vix=18.0, chain=chain,
        )
        self.assertIsNotNone(pack)
        self.assertIsNotNone(pack.flow_imbalance_30m)
        self.assertGreater(pack.flow_imbalance_30m, 0)

    def test_bearish_flow_negative(self):
        """More put volume → negative flow_imbalance_30m."""
        from bot_options import _build_a2_feature_pack
        chain = self._make_chain(call_vol=200, put_vol=800)
        pack = _build_a2_feature_pack(
            symbol="SPY",
            signal_scores={"SPY": self._make_sig()},
            iv_summaries={"SPY": self._make_iv()},
            equity=100_000.0, vix=18.0, chain=chain,
        )
        self.assertIsNotNone(pack)
        self.assertIsNotNone(pack.flow_imbalance_30m)
        self.assertLess(pack.flow_imbalance_30m, 0)

    def test_no_chain_flow_none(self):
        """No chain → flow_imbalance_30m=None."""
        from bot_options import _build_a2_feature_pack
        pack = _build_a2_feature_pack(
            symbol="SPY",
            signal_scores={"SPY": self._make_sig()},
            iv_summaries={"SPY": self._make_iv()},
            equity=100_000.0, vix=18.0, chain=None,
        )
        self.assertIsNotNone(pack)
        self.assertIsNone(pack.flow_imbalance_30m)

    def test_equal_volumes_zero(self):
        """Equal call/put volumes → flow_imbalance_30m=0.0."""
        from bot_options import _build_a2_feature_pack
        chain = self._make_chain(call_vol=500, put_vol=500)
        pack = _build_a2_feature_pack(
            symbol="SPY",
            signal_scores={"SPY": self._make_sig()},
            iv_summaries={"SPY": self._make_iv()},
            equity=100_000.0, vix=18.0, chain=chain,
        )
        self.assertIsNotNone(pack)
        self.assertIsNotNone(pack.flow_imbalance_30m)
        self.assertAlmostEqual(pack.flow_imbalance_30m, 0.0)

    def test_flow_signals_in_data_sources_when_computed(self):
        """flow_signals added to data_sources when flow_imbalance computed."""
        from bot_options import _build_a2_feature_pack
        chain = self._make_chain(call_vol=700, put_vol=300)
        pack = _build_a2_feature_pack(
            symbol="SPY",
            signal_scores={"SPY": self._make_sig()},
            iv_summaries={"SPY": self._make_iv()},
            equity=100_000.0, vix=18.0, chain=chain,
        )
        self.assertIsNotNone(pack)
        if pack.flow_imbalance_30m is not None:
            self.assertIn("flow_signals", pack.data_sources)


# ════════════════════════════════════════════════════════════════════════════
# SUITE 29g — bounded debate response parsing
# ════════════════════════════════════════════════════════════════════════════

class TestBoundedDebateParsing(unittest.TestCase):

    def _valid_payload(self, **kw):
        base = {
            "selected_candidate_id": "abc123",
            "confidence": 0.91,
            "key_risks": ["market fragile"],
            "reasons": "Solid thesis, cheap IV, directional alignment.",
            "recommended_size_modifier": 1.0,
            "reject": False,
        }
        base.update(kw)
        return json.dumps(base)

    def test_valid_json_parsed_correctly(self):
        from bot_options import _parse_bounded_debate_response
        result = _parse_bounded_debate_response(self._valid_payload())
        self.assertFalse(result["reject"])
        self.assertEqual(result["selected_candidate_id"], "abc123")
        self.assertAlmostEqual(result["confidence"], 0.91)

    def test_markdown_json_fences_stripped(self):
        from bot_options import _parse_bounded_debate_response
        raw = (
            "```json\n"
            + self._valid_payload(selected_candidate_id="xyz99", confidence=0.88)
            + "\n```"
        )
        result = _parse_bounded_debate_response(raw)
        self.assertIsNotNone(result)
        self.assertEqual(result["selected_candidate_id"], "xyz99")
        self.assertAlmostEqual(result["confidence"], 0.88)

    def test_plain_code_fences_stripped(self):
        from bot_options import _parse_bounded_debate_response
        raw = (
            "```\n"
            + self._valid_payload(reject=True, selected_candidate_id=None,
                                  confidence=0.3)
            + "\n```"
        )
        result = _parse_bounded_debate_response(raw)
        self.assertTrue(result["reject"])

    def test_json_parse_failure_returns_reject_all(self):
        from bot_options import _parse_bounded_debate_response
        result = _parse_bounded_debate_response("I cannot select any candidate at this time.")
        self.assertTrue(result["reject"])
        self.assertIsNone(result["selected_candidate_id"])
        self.assertEqual(result["confidence"], 0.0)

    def test_empty_response_returns_reject_all(self):
        from bot_options import _parse_bounded_debate_response
        result = _parse_bounded_debate_response("")
        self.assertTrue(result["reject"])
        self.assertIsNone(result["selected_candidate_id"])

    def test_confidence_below_threshold_not_modified_by_parser(self):
        """Parser returns confidence as-is; gate logic is in run_options_cycle."""
        from bot_options import _parse_bounded_debate_response
        result = _parse_bounded_debate_response(
            self._valid_payload(confidence=0.72, reject=False)
        )
        self.assertFalse(result["reject"])
        self.assertAlmostEqual(result["confidence"], 0.72)

    def test_reject_true_payload_parsed(self):
        from bot_options import _parse_bounded_debate_response
        raw = json.dumps({
            "selected_candidate_id": None,
            "confidence": 0.40,
            "key_risks": ["bear case dominates"],
            "reasons": "IV too expensive for direction.",
            "recommended_size_modifier": 1.0,
            "reject": True,
        })
        result = _parse_bounded_debate_response(raw)
        self.assertTrue(result["reject"])
        self.assertIsNone(result["selected_candidate_id"])

    def test_json_embedded_in_prose_extracted(self):
        """JSON object embedded in surrounding text is extracted."""
        from bot_options import _parse_bounded_debate_response
        raw = ('After careful analysis, here is my decision:\n'
               + self._valid_payload(selected_candidate_id="deep1", confidence=0.87)
               + '\nEnd of response.')
        result = _parse_bounded_debate_response(raw)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("selected_candidate_id"), "deep1")

    def test_selected_candidate_structure_type_maps_to_option_strategy(self):
        """_STRATEGY_FROM_STRUCTURE maps all expected structure_type strings."""
        from bot_options import _STRATEGY_FROM_STRUCTURE
        from schemas import OptionStrategy
        expected = {
            "long_call":          OptionStrategy.SINGLE_CALL,
            "long_put":           OptionStrategy.SINGLE_PUT,
            "debit_call_spread":  OptionStrategy.CALL_DEBIT_SPREAD,
            "debit_put_spread":   OptionStrategy.PUT_DEBIT_SPREAD,
            "credit_call_spread": OptionStrategy.CALL_CREDIT_SPREAD,
            "credit_put_spread":  OptionStrategy.PUT_CREDIT_SPREAD,
        }
        for struct_name, expected_strategy in expected.items():
            self.assertEqual(
                _STRATEGY_FROM_STRUCTURE[struct_name], expected_strategy,
                f"Wrong mapping for {struct_name}",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
