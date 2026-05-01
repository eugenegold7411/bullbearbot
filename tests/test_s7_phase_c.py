"""
Sprint 7 Phase C tests — S7-G, S7-H, S7-I, S7-L, S8-cancel-replace.

S7-G: max_recommendations_per_cycle raised from 3 to 5
S7-H: ADD conviction gate in eligibility_check() — BUY on held symbol requires conviction >= add_conviction_gate
S7-I: Graduated TRIM severity — trim_pct scales with thesis_score weakness
S7-L: Conviction score appended to allocator ADD reason string
S8-cancel-replace: _trail_cancel_and_replace fallback in maybe_trail_stop for Alpaca 42210000
"""
import unittest

# ─────────────────────────────────────────────────────────────────────────────
# S7-G — Max Recommendations Cap raised to 5
# ─────────────────────────────────────────────────────────────────────────────

class TestMaxRecommendationsCap(unittest.TestCase):
    """max_recommendations_per_cycle=5: up to 5 non-HOLD recs allowed; 6th suppressed."""

    @classmethod
    def setUpClass(cls):
        from portfolio_allocator import _PA_DEFAULTS, _decide_actions
        cls._decide_actions = staticmethod(_decide_actions)
        cls._PA_DEFAULTS = _PA_DEFAULTS

    def _pa_cfg(self, max_recs=5):
        return {
            "replace_score_gap":             15.0,
            "trim_score_drop":               10.0,
            "trim_score_threshold":          4,
            "weight_deadband":               0.02,
            "min_rebalance_notional":        500.0,
            "max_recommendations_per_cycle": max_recs,
            "same_symbol_daily_cooldown_enabled": False,
            "same_day_replace_block_hours":  6.0,
        }

    def _incumbent(self, symbol, score, mv=5_000.0):
        return {
            "symbol":                  symbol,
            "thesis_score":            score,
            "thesis_score_normalized": score * 10,
            "market_value":            mv,
            "account_pct":             5.0,
        }

    def _run(self, incumbents, max_recs=5):
        proposed, suppressed = self._decide_actions(
            incumbents=incumbents,
            candidates=[],
            pi_data={},
            cfg={},
            pa_cfg=self._pa_cfg(max_recs),
            sizes={"available_for_new": 0},
            equity=100_000.0,
        )
        return proposed, suppressed

    def test_default_cap_is_5(self):
        """_PA_DEFAULTS has max_recommendations_per_cycle=5 after S7-G."""
        self.assertEqual(self._PA_DEFAULTS["max_recommendations_per_cycle"], 5)

    def test_five_recs_all_pass(self):
        """5 TRIM recommendations — all allowed, none suppressed."""
        incumbents = [self._incumbent(f"SYM{i}", score=3) for i in range(5)]
        proposed, suppressed = self._run(incumbents, max_recs=5)
        non_hold = [p for p in proposed if p["action"] != "HOLD"]
        self.assertEqual(len(non_hold), 5)
        self.assertEqual(len(suppressed), 0)

    def test_sixth_rec_suppressed(self):
        """6th non-HOLD recommendation is suppressed when cap=5."""
        incumbents = [self._incumbent(f"SYM{i}", score=3) for i in range(6)]
        proposed, suppressed = self._run(incumbents, max_recs=5)
        non_hold = [p for p in proposed if p["action"] != "HOLD"]
        self.assertEqual(len(non_hold), 5)
        self.assertEqual(len(suppressed), 1)
        self.assertIn("max_recommendations_per_cycle=5", suppressed[0]["suppression_reason"])

    def test_three_recs_unchanged(self):
        """3 TRIM recommendations — still all allowed (cap ≥ 3 never adds new blocks)."""
        incumbents = [self._incumbent(f"SYM{i}", score=3) for i in range(3)]
        proposed, suppressed = self._run(incumbents, max_recs=5)
        non_hold = [p for p in proposed if p["action"] != "HOLD"]
        self.assertEqual(len(non_hold), 3)
        self.assertEqual(len(suppressed), 0)

    def test_old_cap_3_still_works(self):
        """Caller explicitly passing max_recs=3 still suppresses the 4th."""
        incumbents = [self._incumbent(f"SYM{i}", score=3) for i in range(4)]
        proposed, suppressed = self._run(incumbents, max_recs=3)
        non_hold = [p for p in proposed if p["action"] != "HOLD"]
        self.assertEqual(len(non_hold), 3)
        self.assertEqual(len(suppressed), 1)


# ─────────────────────────────────────────────────────────────────────────────
# S7-H — ADD Conviction Gate
# ─────────────────────────────────────────────────────────────────────────────

class TestAddConvictionGate(unittest.TestCase):
    """eligibility_check() gates BUY on existing position below add_conviction_gate=0.65."""

    @classmethod
    def setUpClass(cls):
        from risk_kernel import eligibility_check
        from schemas import (
            AccountAction,
            BrokerSnapshot,
            Conviction,
            Direction,
            NormalizedPosition,
            Tier,
            TradeIdea,
        )
        cls.eligibility_check = staticmethod(eligibility_check)
        cls.BrokerSnapshot = BrokerSnapshot
        cls.NormalizedPosition = NormalizedPosition
        cls.TradeIdea = TradeIdea
        cls.AccountAction = AccountAction
        cls.Direction = Direction
        cls.Conviction = Conviction
        cls.Tier = Tier

    _CONFIG = {
        "parameters": {
            "max_positions": 15,
            "max_position_pct_equity": 0.15,
            "catalyst_tag_disallowed_values": ["", "none", "null", "no"],
            "add_conviction_gate": 0.65,
        },
        "position_sizing": {
            "core_tier_pct": 0.15,
        },
        "account2": {},
    }

    def _pos(self, symbol, qty=10.0, mv=5_000.0):
        return self.NormalizedPosition(
            symbol=symbol,
            alpaca_sym=symbol,
            qty=qty,
            avg_entry_price=mv / qty,
            current_price=mv / qty,
            market_value=mv,
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

    def _idea(self, symbol="AAPL", conviction=0.70):
        return self.TradeIdea(
            symbol=symbol,
            action=self.AccountAction.BUY,
            direction=self.Direction.BULLISH,
            conviction=conviction,
            tier=self.Tier.CORE,
            catalyst="breakout",
        )

    def test_add_at_gate_passes(self):
        """BUY on existing position at exactly conviction=0.65 → passes the gate."""
        snap = self._snapshot(positions=[self._pos("AAPL")])
        result = self.eligibility_check(self._idea("AAPL", conviction=0.65), snap, self._CONFIG)
        self.assertIsNone(result, f"Expected None (pass) but got: {result}")

    def test_add_above_gate_passes(self):
        """BUY on existing position at conviction=0.80 → passes."""
        snap = self._snapshot(positions=[self._pos("AAPL")])
        result = self.eligibility_check(self._idea("AAPL", conviction=0.80), snap, self._CONFIG)
        self.assertIsNone(result)

    def test_add_below_gate_rejected(self):
        """BUY on existing AAPL position at conviction=0.64 → rejected with correct message."""
        snap = self._snapshot(positions=[self._pos("AAPL")])
        result = self.eligibility_check(self._idea("AAPL", conviction=0.64), snap, self._CONFIG)
        self.assertIsInstance(result, str)
        self.assertIn("add to existing AAPL", result)
        self.assertIn("conviction >= 0.65", result)
        self.assertIn("got 0.64", result)

    def test_fresh_buy_no_existing_position_unaffected(self):
        """BUY into symbol with no existing position is not gated (not an ADD)."""
        snap = self._snapshot(positions=[])
        result = self.eligibility_check(self._idea("AAPL", conviction=0.50), snap, self._CONFIG)
        self.assertIsNone(result)

    def test_different_symbol_existing_not_counted(self):
        """Existing MSFT position does not affect AAPL BUY gate."""
        snap = self._snapshot(positions=[self._pos("MSFT")])
        result = self.eligibility_check(self._idea("AAPL", conviction=0.50), snap, self._CONFIG)
        self.assertIsNone(result)

    def test_sell_unaffected_by_gate(self):
        """SELL on existing position is never gated by the ADD conviction check."""
        from schemas import AccountAction
        idea = self.TradeIdea(
            symbol="AAPL",
            action=AccountAction.SELL,
            direction=self.Direction.BULLISH,
            conviction=0.30,
            tier=self.Tier.CORE,
            catalyst="exit",
        )
        snap = self._snapshot(positions=[self._pos("AAPL")])
        result = self.eligibility_check(idea, snap, self._CONFIG)
        self.assertIsNone(result)

    def test_config_default_0_65_used_when_key_absent(self):
        """When add_conviction_gate absent from config, default 0.65 applies."""
        cfg = {
            "parameters": {
                "max_positions": 15,
                "catalyst_tag_disallowed_values": ["", "none", "null", "no"],
                # no add_conviction_gate key
            },
            "position_sizing": {"core_tier_pct": 0.15},
            "account2": {},
        }
        snap = self._snapshot(positions=[self._pos("AAPL")])
        result = self.eligibility_check(self._idea("AAPL", conviction=0.64), snap, cfg)
        self.assertIsInstance(result, str)
        self.assertIn("conviction >= 0.65", result)


# ─────────────────────────────────────────────────────────────────────────────
# S7-I — Graduated TRIM Severity
# ─────────────────────────────────────────────────────────────────────────────

class TestGraduatedTrimSeverity(unittest.TestCase):
    """_trim_pct_for_score() maps thesis_score to correct trim fraction."""

    @classmethod
    def setUpClass(cls):
        from portfolio_allocator import (
            _PA_DEFAULTS,
            _decide_actions,
            _trim_pct_for_score,
        )
        cls._decide_actions = staticmethod(_decide_actions)
        cls._trim_pct_for_score = staticmethod(_trim_pct_for_score)
        cls._PA_DEFAULTS = _PA_DEFAULTS

    _SEVERITY = [
        {"score_max": 2, "trim_pct": 0.75},
        {"score_max": 4, "trim_pct": 0.50},
        {"score_max": 6, "trim_pct": 0.25},
    ]

    def _pa_cfg(self, trim_score_threshold=4, severity=None):
        base = {
            "replace_score_gap":             15.0,
            "trim_score_drop":               10.0,
            "trim_score_threshold":          trim_score_threshold,
            "weight_deadband":               0.02,
            "min_rebalance_notional":        500.0,
            "max_recommendations_per_cycle": 5,
            "same_symbol_daily_cooldown_enabled": False,
            "same_day_replace_block_hours":  6.0,
        }
        if severity is not None:
            base["trim_severity"] = severity
        return base

    def _incumbent(self, score, mv=10_000.0, symbol="AAPL"):
        return {
            "symbol":                  symbol,
            "thesis_score":            score,
            "thesis_score_normalized": score * 10,
            "market_value":            mv,
            "account_pct":             10.0,
        }

    # ── Unit tests for _trim_pct_for_score ────────────────────────────────────

    def test_score_1_yields_75_pct(self):
        self.assertAlmostEqual(
            self._trim_pct_for_score(1, {"trim_severity": self._SEVERITY}), 0.75
        )

    def test_score_2_yields_75_pct(self):
        self.assertAlmostEqual(
            self._trim_pct_for_score(2, {"trim_severity": self._SEVERITY}), 0.75
        )

    def test_score_3_yields_50_pct(self):
        self.assertAlmostEqual(
            self._trim_pct_for_score(3, {"trim_severity": self._SEVERITY}), 0.50
        )

    def test_score_4_yields_50_pct(self):
        self.assertAlmostEqual(
            self._trim_pct_for_score(4, {"trim_severity": self._SEVERITY}), 0.50
        )

    def test_score_5_yields_25_pct(self):
        self.assertAlmostEqual(
            self._trim_pct_for_score(5, {"trim_severity": self._SEVERITY}), 0.25
        )

    def test_score_6_yields_25_pct(self):
        self.assertAlmostEqual(
            self._trim_pct_for_score(6, {"trim_severity": self._SEVERITY}), 0.25
        )

    def test_score_7_no_match_returns_fallback(self):
        """Score above all tier maxes → fallback 25%."""
        self.assertAlmostEqual(
            self._trim_pct_for_score(7, {"trim_severity": self._SEVERITY}), 0.25
        )

    def test_absent_severity_key_returns_fallback(self):
        """trim_severity absent from pa_cfg → fallback 25%."""
        self.assertAlmostEqual(self._trim_pct_for_score(1, {}), 0.25)

    # ── Integration tests via _decide_actions ─────────────────────────────────

    def _run(self, score, mv=10_000.0, severity=None):
        proposed, _ = self._decide_actions(
            incumbents=[self._incumbent(score, mv)],
            candidates=[],
            pi_data={},
            cfg={},
            pa_cfg=self._pa_cfg(trim_score_threshold=4, severity=severity),
            sizes={"available_for_new": 0},
            equity=100_000.0,
        )
        trims = [p for p in proposed if p["action"] == "TRIM"]
        return trims

    def test_score_2_reason_shows_75_pct(self):
        """Score=2 with severity table → TRIM reason mentions 75%."""
        trims = self._run(2, severity=self._SEVERITY)
        self.assertEqual(len(trims), 1)
        self.assertIn("75%", trims[0]["reason"])

    def test_score_3_reason_shows_50_pct(self):
        trims = self._run(3, severity=self._SEVERITY)
        self.assertEqual(len(trims), 1)
        self.assertIn("50%", trims[0]["reason"])

    def test_score_4_reason_shows_50_pct(self):
        trims = self._run(4, severity=self._SEVERITY)
        self.assertEqual(len(trims), 1)
        self.assertIn("50%", trims[0]["reason"])

    def test_score_7_no_trim(self):
        """Score=7 is above trim_score_threshold=4 → no TRIM."""
        trims = self._run(7, severity=self._SEVERITY)
        self.assertEqual(len(trims), 0)

    def test_absent_severity_falls_back_to_25_pct(self):
        """No trim_severity in pa_cfg → TRIM reason shows 25%."""
        trims = self._run(3, severity=None)
        self.assertEqual(len(trims), 1)
        self.assertIn("25%", trims[0]["reason"])


# ─────────────────────────────────────────────────────────────────────────────
# S7-L — Conviction Note in ADD Reason
# ─────────────────────────────────────────────────────────────────────────────

class TestAddConvictionNote(unittest.TestCase):
    """_decide_actions() ADD reason contains conviction={norm/100:.2f} (S7-L)."""

    @classmethod
    def setUpClass(cls):
        from portfolio_allocator import _decide_actions
        cls._decide_actions = staticmethod(_decide_actions)

    def _pa_cfg(self):
        return {
            "replace_score_gap":             15.0,
            "trim_score_drop":               10.0,
            "trim_score_threshold":          4,
            "weight_deadband":               0.02,
            "min_rebalance_notional":        500.0,
            "max_recommendations_per_cycle": 5,
            "same_symbol_daily_cooldown_enabled": False,
            "same_day_replace_block_hours":  6.0,
        }

    def _run_add(self, score=8):
        norm = score * 10
        incumbent = {
            "symbol":                  "AAPL",
            "thesis_score":            score,
            "thesis_score_normalized": norm,
            "market_value":            5_000.0,
            "account_pct":             5.0,      # well below tier_max
        }
        proposed, _ = self._decide_actions(
            incumbents=[incumbent],
            candidates=[],
            pi_data={},
            cfg={},
            pa_cfg=self._pa_cfg(),
            # standard=8000 → _target_weights infers tier_max=0.08; acct_pct=0.05 < 0.06
            sizes={"standard": 8_000.0, "available_for_new": 10_000.0},
            equity=100_000.0,
        )
        adds = [p for p in proposed if p["action"] == "ADD"]
        return adds

    def test_add_reason_contains_conviction_score(self):
        """ADD reason contains 'conviction=0.80' for thesis_score=8."""
        adds = self._run_add(score=8)
        self.assertEqual(len(adds), 1)
        reason = adds[0]["reason"]
        self.assertIn("conviction=0.80", reason)

    def test_add_reason_format_matches_pattern(self):
        """ADD reason format: 'thesis_score=X/10 (conviction=Y.YY)'."""
        adds = self._run_add(score=9)
        self.assertEqual(len(adds), 1)
        reason = adds[0]["reason"]
        self.assertIn("thesis_score=9/10", reason)
        self.assertIn("conviction=0.90", reason)

    def test_add_conviction_at_boundary_score_7(self):
        """thesis_score=7 → conviction=0.70 in reason."""
        adds = self._run_add(score=7)
        self.assertEqual(len(adds), 1)
        self.assertIn("conviction=0.70", adds[0]["reason"])

    def test_trim_reason_unchanged(self):
        """TRIM reason does not contain 'conviction=' (S7-L only affects ADD)."""
        incumbent = {
            "symbol":                  "AAPL",
            "thesis_score":            3,
            "thesis_score_normalized": 30,
            "market_value":            10_000.0,
            "account_pct":             10.0,
        }
        proposed, _ = self._decide_actions(
            incumbents=[incumbent],
            candidates=[],
            pi_data={},
            cfg={},
            pa_cfg=self._pa_cfg(),
            sizes={"available_for_new": 0},
            equity=100_000.0,
        )
        trims = [p for p in proposed if p["action"] == "TRIM"]
        self.assertEqual(len(trims), 1)
        self.assertNotIn("conviction=", trims[0]["reason"])


# ─────────────────────────────────────────────────────────────────────────────
# S8 — _trail_cancel_and_replace: cancel-and-replace fallback for 42210000
# ─────────────────────────────────────────────────────────────────────────────

from unittest.mock import MagicMock, patch


def _make_position(symbol="GOOGL", qty=10, entry=340.0, current=355.0, unrealized=150.0):
    p = MagicMock()
    p.symbol           = symbol
    p.qty              = qty
    p.avg_entry_price  = entry
    p.current_price    = current
    p.unrealized_pl    = unrealized
    return p


def _em_cfg_base(**overrides):
    cfg = {
        "trail_stop_enabled":           True,
        "trail_trigger_r":              1.0,
        "trail_to_breakeven_plus_pct":  0.005,
        "refresh_if_stop_stale_pct":    0.15,
        "backstop_days":                7,
        "trail_replace_max_failures":   3,
    }
    cfg.update(overrides)
    return cfg


def _strategy_cfg(**exit_overrides):
    base = _em_cfg_base(**exit_overrides)
    return {"exit_management": base}


class TestTrailCancelAndReplace(unittest.TestCase):
    """S8: maybe_trail_stop uses cancel+resubmit as primary path — no replace_order calls."""

    def setUp(self):
        import exit_manager as em
        em._trail_replace_failures.clear()
        self._em = em

    # ── 1. Trail advance cancels existing stop then submits fresh stop ────────

    def test_trail_advance_cancels_then_resubmits(self):
        """cancel_order_by_id called first; submit_order places fresh stop (primary path)."""
        client = MagicMock()
        new_order = MagicMock(); new_order.id = "new-order-id"
        client.submit_order.return_value = new_order

        pos = _make_position()
        ei  = {"stop_price": 325.0, "stop_order_id": "old-oid", "stop_order_status": "accepted"}

        with patch.object(self._em, "get_active_exits", return_value={"GOOGL": ei}):
            result = self._em.maybe_trail_stop(pos, client, _strategy_cfg())

        client.cancel_order_by_id.assert_called_once_with("old-oid")
        client.submit_order.assert_called_once()
        client.replace_order_by_id.assert_not_called()
        self.assertTrue(result)

    # ── 2. submit_order failure increments counter after 3 attempts ──────────

    def test_non_42210000_error_uses_retry_path(self):
        """submit_order raises → counter incremented, trail returns False."""
        client = MagicMock()
        client.submit_order.side_effect = Exception("40010001: some other error")

        pos = _make_position()
        ei  = {"stop_price": 325.0, "stop_order_id": "old-oid", "stop_order_status": "new"}

        with patch("time.sleep"), \
             patch.object(self._em, "get_active_exits", return_value={"GOOGL": ei}):
            result = self._em.maybe_trail_stop(pos, client, _strategy_cfg())

        client.cancel_order_by_id.assert_called_once()
        self.assertFalse(result)
        self.assertEqual(self._em._trail_replace_failures.get("old-oid", 0), 1)

    # ── 3. Cancel succeeds → new stop placed at trail price ──────────────────

    def test_cancel_success_places_new_stop_at_trail_price(self):
        """After cancel, submit_order called with stop_price == new_stop (entry × 1.005)."""

        client = MagicMock()
        new_order = MagicMock(); new_order.id = "new-id"
        client.submit_order.return_value = new_order

        # entry=340, plus_pct=0.005 → new_stop = round(340 * 1.005, 2) = 341.70
        pos = _make_position(entry=340.0, current=355.0, unrealized=150.0)
        ei  = {"stop_price": 325.0, "stop_order_id": "oid-123", "stop_order_status": "accepted"}

        with patch.object(self._em, "get_active_exits", return_value={"GOOGL": ei}):
            result = self._em.maybe_trail_stop(pos, client, _strategy_cfg())

        self.assertTrue(result)
        submitted = client.submit_order.call_args[0][0]
        self.assertAlmostEqual(float(submitted.stop_price), 341.70, places=1)

    # ── 4. Cancel fails → failure counter incremented, no place attempt ───────

    def test_cancel_failure_increments_counter_no_place(self):
        """cancel_order_by_id raises → failure counter incremented, submit_order not called."""
        client = MagicMock()
        client.cancel_order_by_id.side_effect = Exception("cancel failed")

        pos = _make_position()
        ei  = {"stop_price": 325.0, "stop_order_id": "oid-cancel-fail", "stop_order_status": "accepted"}

        with patch.object(self._em, "get_active_exits", return_value={"GOOGL": ei}):
            result = self._em.maybe_trail_stop(pos, client, _strategy_cfg())

        self.assertFalse(result)
        client.submit_order.assert_not_called()
        self.assertEqual(self._em._trail_replace_failures.get("oid-cancel-fail", 0), 1)

    # ── 5. cancel+resubmit is unconditional — not config-gated ───────────────

    def test_cancel_resubmit_is_unconditional(self):
        """cancel_order_by_id is always called first; replace_order_by_id is never called."""
        client = MagicMock()
        new_order = MagicMock(); new_order.id = "new-id"
        client.submit_order.return_value = new_order

        pos = _make_position()
        ei  = {"stop_price": 325.0, "stop_order_id": "oid-unconditional", "stop_order_status": "accepted"}

        with patch("time.sleep"), \
             patch.object(self._em, "get_active_exits", return_value={"GOOGL": ei}):
            result = self._em.maybe_trail_stop(pos, client, _strategy_cfg())

        client.cancel_order_by_id.assert_called_once()
        client.replace_order_by_id.assert_not_called()
        self.assertTrue(result)

    # ── 6. Retry cap prevents indefinite attempts on a stuck order ────────────

    def test_retry_cap_abandons_trail_for_stuck_order(self):
        """After trail_replace_max_failures failures, trail is abandoned for this order_id."""
        client = MagicMock()

        oid = "oid-maxed"
        self._em._trail_replace_failures[oid] = 3   # already at cap

        pos = _make_position()
        ei  = {"stop_price": 325.0, "stop_order_id": oid, "stop_order_status": "accepted"}

        with patch.object(self._em, "get_active_exits", return_value={"GOOGL": ei}):
            result = self._em.maybe_trail_stop(pos, client, _strategy_cfg())

        client.cancel_order_by_id.assert_not_called()
        self.assertFalse(result)
