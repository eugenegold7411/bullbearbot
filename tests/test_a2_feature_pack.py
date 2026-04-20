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


if __name__ == "__main__":
    unittest.main(verbosity=2)
