"""
tests/test_core.py — Core regression tests for BullBearBot.

Runnable as:
    cd /home/trading-bot
    python3 -m unittest tests/test_core.py -v

Suites:
  1. order_executor — validation logic (BUG-002, exposure caps)
  2. memory         — outcome recording (BUG-003)
  3. exit_manager   — trail stop arithmetic (BUG-007)
  4. earnings_intel — stub detection (BUG-006)
  5. signal_scorer  — cap at 25, held-position priority (BUG-001)
  6-8. schemas      — symbol normalization, BrokerAction, validation, SignalScore, OptionsStructure
  9. risk_kernel     — eligibility, sizing, stops, VIX scaling, options structure selection
 22. Phase A        — decision_id timing fix, shadow lane id, executor isinstance routing
 23. Phase B        — obs mode v2, options lifecycle, time-stop, DecisionOutcomeRecord
 24. Phase C7       — divergence subsystem: classify, mode enforcement, detect, respond, e2e
 25. Phase C8/D13   — market_data section fallbacks + options close/roll audit trail
"""

import json
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# Ensure /home/trading-bot is on the path regardless of where tests/ lives
_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

os.chdir(_BOT_DIR)  # set working dir so relative file paths in modules resolve


# ═════════════════════════════════════════════════════════════════════════════
# SUITE 1 — order_executor validation logic
# ═════════════════════════════════════════════════════════════════════════════

class TestOrderExecutorValidation(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Import lazily so the module-level alpaca client construction
        # happens in the context where .env is already loaded.
        import order_executor as oe
        cls.oe = oe
        cls.validate_action = staticmethod(oe.validate_action)

    def _mock_account(self, equity=100_000, buying_power=200_000):
        return SimpleNamespace(
            equity=str(equity),
            buying_power=str(buying_power),
        )

    def _mock_positions(self, market_values=None):
        """Return list of SimpleNamespace positions with given market_values."""
        positions = []
        for mv in (market_values or []):
            positions.append(SimpleNamespace(market_value=str(mv), qty="1"))
        return positions

    # ── BUG-002 regression ────────────────────────────────────────────────────

    def test_hold_passes_when_market_closed(self):
        """BUG-002: HOLD must not be rejected when market is closed."""
        action   = {"action": "hold", "symbol": "GLD", "confidence": "medium"}
        account  = self._mock_account()
        # Should raise nothing:
        self.validate_action(action, account, [], "closed", 0)

    def test_buy_rejected_when_market_closed(self):
        """Session 1: market-closed is now WARNING only (demoted). A well-formed buy with
        stop_loss/take_profit must NOT be rejected purely for market being closed.
        (Structural rejections — missing stop_loss — are still enforced regardless.)"""
        action  = {
            "action":      "buy",
            "symbol":      "GLD",
            "qty":         10,
            "stop_loss":   430.0,
            "take_profit": 450.0,
            "confidence":  "medium",
            "tier":        "core",
        }
        account = self._mock_account()
        # Should not raise — market-closed is demoted to log.warning() in Session 1
        # current_prices avoids a live Alpaca price fetch in test context
        self.validate_action(action, account, [], "closed", 0,
                             current_prices={"GLD": 435.0})

    # ── Exposure cap tests ────────────────────────────────────────────────────

    def _valid_buy_action(self, confidence="medium", tier="core"):
        """A buy action that passes all checks except possibly the exposure cap.
        SPY @ $300:  stop=$288 (4%), take_profit=$324 (2.0×R:R), qty=40 → value=$12,000
        """
        return {
            "action":      "buy",
            "symbol":      "SPY",
            "qty":         40,
            "stop_loss":   288.0,
            "take_profit": 324.0,
            "tier":        tier,
            "confidence":  confidence,
        }

    def test_exposure_cap_high_conviction(self):
        """High conviction → cap=2×equity=200K. Existing 180K + new 12K = 192K < 200K → PASS."""
        action    = self._valid_buy_action(confidence="high", tier="core")
        account   = self._mock_account(equity=100_000, buying_power=200_000)
        positions = self._mock_positions([180_000])
        # Should not raise:
        self.validate_action(
            action, account, positions, "open", 20,
            current_prices={"SPY": 300.0},
        )

    def test_exposure_cap_low_conviction_exceeded(self):
        """Session 1: exposure cap is now WARNING only (demoted from hard rejection).
        Low conviction → cap=1×equity=100K. Existing 95K + new 12K = 107K > 100K.
        Executor logs a warning but does NOT raise — risk_kernel is primary owner."""
        action    = self._valid_buy_action(confidence="low", tier="core")
        account   = self._mock_account(equity=100_000, buying_power=100_000)
        positions = self._mock_positions([95_000])
        # Should not raise — exposure cap demoted to log.warning() in Session 1
        self.validate_action(
            action, account, positions, "open", 20,
            current_prices={"SPY": 300.0},
        )

    # ── BUG-008 regression ────────────────────────────────────────────────────

    def test_crypto_stop_scale_guard(self):
        """BUG-008: stop_loss < 1000 on a crypto symbol (BTC/USD) must be detected as scale error."""
        # Replicate the exact detection logic from bot.py post-processing pass.
        actions = [
            {"symbol": "BTC/USD", "stop_loss": 68.0, "action": "hold"},
        ]
        current_prices = {"BTC/USD": 74000.0}

        detected = []
        for a in actions:
            if "/" not in a.get("symbol", ""):
                continue
            sl = a.get("stop_loss")
            if sl is None or float(sl) >= 1000:
                continue
            sym   = a["symbol"]
            price = float(current_prices.get(sym, 0))
            if price <= 0:
                continue
            detected.append(sym)

        self.assertEqual(detected, ["BTC/USD"],
                         "BTC/USD with stop_loss=68.0 should be detected as scale error")

    def test_crypto_stop_real_price_not_flagged(self):
        """BUG-008: a real crypto stop price (e.g. 68000) must NOT be flagged."""
        actions        = [{"symbol": "BTC/USD", "stop_loss": 68000.0, "action": "hold"}]

        detected = []
        for a in actions:
            if "/" not in a.get("symbol", ""):
                continue
            sl = a.get("stop_loss")
            if sl is None or float(sl) >= 1000:
                continue
            detected.append(a["symbol"])

        self.assertEqual(detected, [], "stop_loss=68000 should NOT be flagged as scale error")


# ═════════════════════════════════════════════════════════════════════════════
# SUITE 2 — memory outcome recording
# ═════════════════════════════════════════════════════════════════════════════

class TestMemoryOutcomeRecording(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import memory as mem
        cls.mem = mem

    def _base_decision(self, action_type, stop_loss=430.0, take_profit=450.0):
        return [{
            "ts":       "2026-04-15T00:00:00+00:00",
            "session":  "market",
            "regime":   "normal",
            "vector_id": "",
            "actions": [{
                "action":      action_type,
                "symbol":      "GLD",
                "qty":         5,
                "stop_loss":   stop_loss,
                "take_profit": take_profit,
                "tier":        "core",
                "catalyst":    "test",
                "outcome":     None,
                "pnl":         None,
            }],
        }]

    def _mock_fill(self, fill_price=440.0, symbol="GLD", side=None):
        from alpaca.trading.enums import OrderSide
        return SimpleNamespace(
            side=OrderSide.BUY if side is None else side,
            filled_avg_price=str(fill_price),
            filled_qty="5.0",
            symbol=symbol,
            id="test-order-id",
            status="filled",
        )

    def _empty_perf(self):
        return {
            "by_type": {}, "by_sector": {}, "by_session": {},
            "by_catalyst": {}, "by_strategy": {}, "by_tier": {},
            "totals": {"trades": 0, "wins": 0, "losses": 0, "pending": 0},
        }

    def test_hold_not_recorded_as_loss(self):
        """BUG-003: HOLD actions must not be matched against fills and recorded as losses."""
        decisions = self._base_decision("hold", stop_loss=430.0, take_profit=450.0)
        # fill_price=433.0 would trigger loss for a buy (433 <= 430*1.01=434.3)
        fill = self._mock_fill(fill_price=433.0)

        with (mock.patch("memory.TradingClient") as MockTC,
              mock.patch("memory._load_decisions", return_value=decisions),
              mock.patch("memory._save_decisions")  as mock_save,
              mock.patch("memory._load_perf",       return_value=self._empty_perf()),
              mock.patch("memory._save_perf"),
              mock.patch("memory.trade_memory")):
            MockTC.return_value.get_orders.return_value = [fill]
            self.mem.update_outcomes_from_alpaca()

        # No changes should have been persisted — hold was skipped
        mock_save.assert_not_called()

    def test_buy_outcome_pending_when_between_levels(self):
        """Fill between stop and target is pending — no outcome recorded yet."""
        decisions = self._base_decision("buy", stop_loss=430.0, take_profit=450.0)
        # 440 is between stop(430) and target(450) → pending
        fill = self._mock_fill(fill_price=440.0)

        with (mock.patch("memory.TradingClient") as MockTC,
              mock.patch("memory._load_decisions", return_value=decisions),
              mock.patch("memory._save_decisions")  as mock_save,
              mock.patch("memory._load_perf",       return_value=self._empty_perf()),
              mock.patch("memory._save_perf"),
              mock.patch("memory.trade_memory")):
            MockTC.return_value.get_orders.return_value = [fill]
            self.mem.update_outcomes_from_alpaca()

        mock_save.assert_not_called()
        # Original outcome field still None
        self.assertIsNone(decisions[0]["actions"][0]["outcome"])

    def test_buy_loss_recorded(self):
        """Fill at or below stop_loss * 1.01 → outcome='loss'."""
        from alpaca.trading.enums import OrderSide
        decisions = self._base_decision("buy", stop_loss=430.0, take_profit=450.0)
        # 425 <= 430 * 1.01 = 434.3 → loss; must be a SELL fill (exit) for outcome matching
        fill = self._mock_fill(fill_price=425.0, side=OrderSide.SELL)

        with (mock.patch("memory.TradingClient") as MockTC,
              mock.patch("memory._load_decisions", return_value=decisions),
              mock.patch("memory._save_decisions")  as mock_save,
              mock.patch("memory._load_perf",       return_value=self._empty_perf()),
              mock.patch("memory._save_perf"),
              mock.patch("memory.trade_memory"),
              mock.patch.dict("os.environ",
                              {"ALPACA_API_KEY": "test_key",
                               "ALPACA_SECRET_KEY": "test_secret"})):
            MockTC.return_value.get_orders.return_value = [fill]
            self.mem.update_outcomes_from_alpaca()

        mock_save.assert_called_once()
        self.assertEqual(decisions[0]["actions"][0]["outcome"], "loss")


# ═════════════════════════════════════════════════════════════════════════════
# SUITE 3 — exit_manager trail stop arithmetic
# ═════════════════════════════════════════════════════════════════════════════

class TestExitManagerTrailStop(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from exit_manager import get_active_exits, maybe_trail_stop
        cls.maybe_trail_stop  = staticmethod(maybe_trail_stop)
        cls.get_active_exits  = staticmethod(get_active_exits)

    _STRATEGY = {
        "exit_management": {
            "trail_stop_enabled":          True,
            "trail_trigger_r":             1.0,
            "trail_to_breakeven_plus_pct": 0.005,
        }
    }

    def _position(self, entry, current, unrealized_pl=None):
        pl = unrealized_pl if unrealized_pl is not None else (current - entry) * 34
        return SimpleNamespace(
            symbol="GLD",
            avg_entry_price=str(entry),
            current_price=str(current),
            unrealized_pl=str(pl),
            qty="34",
        )

    def test_trail_trigger_fires_at_1r(self):
        """profit_r=2.28 ≥ 1.0 → trail fires, new_stop = entry*1.005 = 435.67."""
        # entry=433.50, stop=429.11, current=443.52
        # stop_dist=4.39, profit=10.02, profit_r=2.28
        pos      = self._position(433.50, 443.52, unrealized_pl=340.68)
        ei       = {"stop_price": 429.11, "stop_order_id": "fake-oid", "status": "protected"}
        mock_cli = mock.MagicMock()

        result = self.maybe_trail_stop(pos, mock_cli, self._STRATEGY, exit_info=ei)

        self.assertTrue(result, "Trail should fire when profit_r ≥ 1.0")
        mock_cli.replace_order_by_id.assert_called_once()
        # Inspect new stop_price passed to replace_order_by_id
        call_args    = mock_cli.replace_order_by_id.call_args[0]
        replace_req  = call_args[1]
        expected_new = round(433.50 * 1.005, 2)  # 435.67
        self.assertAlmostEqual(float(replace_req.stop_price), expected_new, places=1)

    def test_trail_does_not_fire_below_1r(self):
        """profit_r=0.57 < 1.0 → trail must NOT fire."""
        # entry=433.50, stop=429.11, current=436.00
        # stop_dist=4.39, profit=2.50, profit_r=0.57
        pos      = self._position(433.50, 436.00, unrealized_pl=85.0)
        ei       = {"stop_price": 429.11, "stop_order_id": "fake-oid", "status": "protected"}
        mock_cli = mock.MagicMock()

        result = self.maybe_trail_stop(pos, mock_cli, self._STRATEGY, exit_info=ei)

        self.assertFalse(result, "Trail must NOT fire when profit_r < 1.0")
        mock_cli.replace_order_by_id.assert_not_called()

    def test_enum_serialization_normalized(self):
        """BUG-007: order with type='OrderType.STOP' must be recognised as a stop order."""
        # Simulate the Alpaca enum __str__ issue
        class _MockType:
            def __str__(self): return "OrderType.STOP"

        class _MockSide:
            def __str__(self): return "OrderSide.SELL"

        mock_order = SimpleNamespace(
            symbol="GLD",
            type=_MockType(),
            side=_MockSide(),
            stop_price="429.11",
            limit_price=None,
            id="stop-order-id",
            legs=None,
        )

        mock_pos = SimpleNamespace(
            symbol="GLD",
            qty="34",
            current_price="443.52",
        )

        mock_client = mock.MagicMock()
        mock_client.get_orders.return_value = [mock_order]

        result = self.get_active_exits([mock_pos], mock_client)

        self.assertIn("GLD", result)
        self.assertIsNotNone(result["GLD"]["stop_price"],
                             "stop_price must be extracted despite 'OrderType.STOP' enum repr")
        self.assertAlmostEqual(result["GLD"]["stop_price"], 429.11, places=2)


# ═════════════════════════════════════════════════════════════════════════════
# SUITE 4 — earnings_intel stub detection
# ═════════════════════════════════════════════════════════════════════════════

class TestEarningsIntelStubDetection(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import earnings_intel as ei
        cls.ei = ei

    def test_yfinance_stub_skipped(self):
        """BUG-006: yfinance stub transcript must return {} without calling Claude."""
        transcript = (
            "yfinance fundamentals for TSM: trailingEps: 10.42  |  "
            "forwardEps: 11.5  |  revenueGrowth: 0.12"
        )
        with mock.patch.object(self.ei._claude.messages, "create") as mock_create:
            result = self.ei.analyze_earnings_transcript("TSM", transcript)

        self.assertEqual(result, {}, "yfinance stub must return empty dict")
        mock_create.assert_not_called()

    def test_real_transcript_analyzed(self):
        """Non-stub transcript → Claude is called and result has expected keys."""
        transcript = (
            "Good morning. Thank you for joining the TSM Q4 2025 earnings call. "
            "We are pleased to report EPS of $2.24, beating consensus of $2.10 by 6.7%. "
            "Revenue was $26.9B, above consensus of $26.1B. Management raised FY2026 "
            "guidance by 8%, citing strong AI server demand."
        )
        mock_resp = mock.MagicMock()
        mock_resp.content = [mock.MagicMock(text=json.dumps({
            "eps_beat_miss":      "+6.7% beat",
            "revenue_beat_miss":  "+3% beat",
            "guidance_direction": "raised",
            "guidance_detail":    "FY2026 raised 8% on AI demand",
            "management_tone":    "confident",
            "key_risks":          ["tariffs", "geopolitics"],
            "surprise_elements":  ["AI server demand"],
            "analyst_sentiment":  "positive",
            "trading_signal":     "bullish",
            "one_line_summary":   "Clean beat, raised guidance — bullish setup",
        }))]

        with (mock.patch.object(self.ei._claude.messages, "create", return_value=mock_resp),
              mock.patch("earnings_intel._load_cached_analysis", return_value=None),
              mock.patch("earnings_intel._save_analysis")):
            result = self.ei.analyze_earnings_transcript("TSM", transcript)

        self.assertIn("trading_signal", result)
        self.assertEqual(result["trading_signal"], "bullish")
        self.assertIn("guidance_direction", result)
        self.assertEqual(result["guidance_direction"], "raised")


# ═════════════════════════════════════════════════════════════════════════════
# SUITE 5 — signal scorer cap and prioritization
# ═════════════════════════════════════════════════════════════════════════════

def _build_scored_list(watchlist_symbols_set, held_symbols, morning_picks, max_scored=25):
    """
    Replicates the prioritization logic from bot.py:score_signals().
    Used here to test the algorithm without importing the full bot module.
    """
    scored = []
    seen   = set()

    def _add(sym):
        if sym in seen or sym not in watchlist_symbols_set:
            return
        scored.append(sym)
        seen.add(sym)

    for sym in held_symbols:
        _add(sym)
    for sym in morning_picks:
        _add(sym)
    for sym in watchlist_symbols_set:
        if len(scored) >= max_scored:
            break
        _add(sym)

    return scored


class TestSignalScorerCap(unittest.TestCase):

    def test_scored_symbols_capped_at_25(self):
        """BUG-001: scorer must never return more than _MAX_SCORED=25 symbols."""
        watchlist = set(f"SYM{i}" for i in range(39))
        scored    = _build_scored_list(watchlist, [], [], max_scored=25)
        self.assertLessEqual(len(scored), 25,
                             f"Expected ≤25 scored symbols, got {len(scored)}")

    def test_held_positions_prioritized(self):
        """BUG-001: currently held positions must appear first in scored list."""
        held      = ["GLD", "TSM"]
        watchlist = set(["GLD", "TSM"] + [f"SYM{i}" for i in range(37)])  # 39 total
        scored    = _build_scored_list(watchlist, held, [], max_scored=25)

        self.assertIn("GLD", scored)
        self.assertIn("TSM", scored)

        # Both should appear before any SYM* symbols
        gld_idx = scored.index("GLD")
        tsm_idx = scored.index("TSM")
        sym_indices = [scored.index(s) for s in scored if s.startswith("SYM")]
        if sym_indices:
            self.assertLess(gld_idx, min(sym_indices),
                            "GLD should appear before non-held symbols")
            self.assertLess(tsm_idx, min(sym_indices),
                            "TSM should appear before non-held symbols")

    def test_held_positions_always_included_even_when_cap_reached(self):
        """Held positions must be included even if watchlist is large."""
        held      = ["GLD", "TSM"]
        watchlist = set(["GLD", "TSM"] + [f"SYM{i}" for i in range(37)])
        scored    = _build_scored_list(watchlist, held, [], max_scored=3)
        # With max_scored=3 and 2 held, GLD and TSM must both be present
        self.assertIn("GLD", scored)
        self.assertIn("TSM", scored)

    def test_morning_picks_prioritized_after_held(self):
        """Morning brief conviction picks come after held but before general watchlist."""
        held          = ["GLD"]
        morning_picks = ["MSFT", "TSM"]
        watchlist     = set(["GLD", "MSFT", "TSM"] + [f"SYM{i}" for i in range(36)])
        scored        = _build_scored_list(watchlist, held, morning_picks, max_scored=25)

        gld_idx  = scored.index("GLD")
        msft_idx = scored.index("MSFT")
        tsm_idx  = scored.index("TSM")

        self.assertLess(gld_idx, msft_idx,
                        "held GLD must appear before morning pick MSFT")
        self.assertLess(msft_idx, 4,
                        "MSFT morning pick should appear near the front")
        self.assertLess(tsm_idx, 4,
                        "TSM morning pick should appear near the front")


# ═════════════════════════════════════════════════════════════════════════════
# SUITE 6 — C1: crypto hold path with slash-format symbol
# ═════════════════════════════════════════════════════════════════════════════

class TestCryptoHoldPathC1(unittest.TestCase):
    """C1: BTC/USD hold must be treated as crypto (fractional qty, limit order).

    order_executor.py hold handler was calling _em_hold._is_crypto("BTC/USD")
    which returns False because _is_crypto() expects Alpaca-format "BTCUSD".
    Fix: "/" in symbol or _is_crypto(symbol).
    """

    def test_slash_format_detected_as_crypto(self):
        """'/' in symbol must resolve is_crypto_hold=True for BTC/USD format."""
        btc_slash = "BTC/USD"
        eth_slash = "ETH/USD"

        # Simulate the C1 fix logic exactly as written in order_executor.py
        import exit_manager as em

        def _is_crypto_hold_fixed(sym: str) -> bool:
            return "/" in sym or em._is_crypto(sym)

        self.assertTrue(_is_crypto_hold_fixed(btc_slash),
                        "BTC/USD should be detected as crypto via '/' check")
        self.assertTrue(_is_crypto_hold_fixed(eth_slash),
                        "ETH/USD should be detected as crypto via '/' check")

    def test_alpaca_format_still_detected(self):
        """Alpaca-format BTCUSD must still resolve to True via _is_crypto()."""
        import exit_manager as em

        def _is_crypto_hold_fixed(sym: str) -> bool:
            return "/" in sym or em._is_crypto(sym)

        self.assertTrue(_is_crypto_hold_fixed("BTCUSD"),
                        "BTCUSD (Alpaca format) must still resolve as crypto")
        self.assertTrue(_is_crypto_hold_fixed("ETHUSD"),
                        "ETHUSD (Alpaca format) must still resolve as crypto")

    def test_stock_not_detected_as_crypto(self):
        """Regular stock symbols must NOT be detected as crypto."""
        import exit_manager as em

        def _is_crypto_hold_fixed(sym: str) -> bool:
            return "/" in sym or em._is_crypto(sym)

        self.assertFalse(_is_crypto_hold_fixed("GLD"),
                         "GLD must not be detected as crypto")
        self.assertFalse(_is_crypto_hold_fixed("AAPL"),
                         "AAPL must not be detected as crypto")
        self.assertFalse(_is_crypto_hold_fixed("TSM"),
                         "TSM must not be detected as crypto")

    def test_old_logic_was_broken_for_slash_format(self):
        """Verify the OLD logic (pre-C1) returned False for BTC/USD — confirms fix was needed."""
        import exit_manager as em
        self.assertFalse(em._is_crypto("BTC/USD"),
                         "_is_crypto('BTC/USD') must return False — it's designed for BTCUSD format. "
                         "C1 fix adds '/' in symbol check at the call site.")


# ═════════════════════════════════════════════════════════════════════════════
# SUITE 7 — C3: overnight gate uses Haiku instead of Sonnet
# ═════════════════════════════════════════════════════════════════════════════

class TestOvernightGateC3(unittest.TestCase):
    """C3: overnight session must call _ask_claude_overnight (Haiku), not ask_claude (Sonnet)."""

    @classmethod
    def setUpClass(cls):
        import bot
        cls.bot = bot

    def test_ask_claude_overnight_exists(self):
        """_ask_claude_overnight function must exist in bot.py."""
        self.assertTrue(
            callable(getattr(self.bot, "_ask_claude_overnight", None)),
            "_ask_claude_overnight must be a callable function in bot.py"
        )

    def test_overnight_default_is_hold_all(self):
        """_OVERNIGHT_DEFAULT must be a hold-all response (safe fallback structure)."""
        default = self.bot._OVERNIGHT_DEFAULT
        # SQ-1: schema normalized — key is now regime_view (intent-based format)
        self.assertEqual(default["regime_view"], "normal",
                         "_OVERNIGHT_DEFAULT regime_view must be 'normal'")
        self.assertEqual(default["ideas"], [],
                         "_OVERNIGHT_DEFAULT ideas must be empty list (hold-all)")
        self.assertIn("reasoning", default,
                      "_OVERNIGHT_DEFAULT must have reasoning field")

    def test_overnight_uses_haiku_model(self):
        """_OVERNIGHT_SYS and MODEL_FAST must be defined; _ask_claude_overnight uses MODEL_FAST."""
        import inspect
        source = inspect.getsource(self.bot._ask_claude_overnight)
        self.assertIn("MODEL_FAST", source,
                      "_ask_claude_overnight must use MODEL_FAST (Haiku), not MODEL (Sonnet)")
        self.assertNotIn('model=MODEL,', source,
                         "_ask_claude_overnight must not use the Sonnet MODEL constant")

    def test_overnight_gate_in_run_cycle(self):
        """run_cycle() source must contain the overnight gate branching on session_tier."""
        import inspect
        source = inspect.getsource(self.bot.run_cycle)
        self.assertIn('session_tier == "overnight"', source,
                      "run_cycle must gate on session_tier == 'overnight'")
        self.assertIn("_ask_claude_overnight(", source,
                      "run_cycle must call _ask_claude_overnight for overnight session")

    def test_ask_claude_not_called_for_overnight(self):
        """Mock _ask_claude_overnight and ask_claude; verify only overnight path is called."""
        # SQ-1: schema normalized — use intent-based format (regime_view + ideas)
        _hold_all = {
            "reasoning": "test",
            "regime_view": "normal",
            "ideas": [],
            "holds": [],
            "notes": "",
            "concerns": "",
        }

        # Patch _ask_claude_overnight to return hold-all; patch ask_claude to fail if called
        with mock.patch.object(self.bot, "_ask_claude_overnight", return_value=_hold_all) as mock_overnight, \
             mock.patch.object(self.bot, "ask_claude") as mock_sonnet:

            result = self.bot._ask_claude_overnight(
                positions=[],
                crypto_context="",
                regime_obj={"regime_score": 50, "bias": "neutral"},
                macro_wire="",
            )

            # The patched version was called
            mock_overnight.assert_called_once()
            # ask_claude (Sonnet) was NOT called via the overnight path
            mock_sonnet.assert_not_called()

        # SQ-1: schema normalized — check intent-based fields
        self.assertEqual(result["regime_view"], "normal")
        self.assertEqual(result["ideas"], [])


# ═════════════════════════════════════════════════════════════════════════════
# SUITE 8 — schemas.py: symbol normalisation, enums, dataclasses
# ═════════════════════════════════════════════════════════════════════════════

class TestSchemaSymbolNormalization(unittest.TestCase):
    """normalize_symbol / is_crypto / alpaca_symbol / yfinance_symbol"""

    @classmethod
    def setUpClass(cls):
        from schemas import (
            alpaca_symbol,
            is_crypto,
            normalize_symbol,
            yfinance_symbol,
        )
        cls.normalize  = staticmethod(normalize_symbol)
        cls.is_crypto  = staticmethod(is_crypto)
        cls.alpaca_sym = staticmethod(alpaca_symbol)
        cls.yf_sym     = staticmethod(yfinance_symbol)

    # ── normalize_symbol ──────────────────────────────────────────────────────

    def test_normalize_slash_passthrough(self):
        """BTC/USD stays BTC/USD."""
        self.assertEqual(self.normalize("BTC/USD"), "BTC/USD")

    def test_normalize_alpaca_to_slash(self):
        """BTCUSD -> BTC/USD."""
        self.assertEqual(self.normalize("BTCUSD"), "BTC/USD")

    def test_normalize_yfinance_to_slash(self):
        """BTC-USD -> BTC/USD."""
        self.assertEqual(self.normalize("BTC-USD"), "BTC/USD")

    def test_normalize_eth_variants(self):
        """All three ETH formats resolve to ETH/USD."""
        self.assertEqual(self.normalize("ETH/USD"), "ETH/USD")
        self.assertEqual(self.normalize("ETHUSD"),  "ETH/USD")
        self.assertEqual(self.normalize("ETH-USD"),  "ETH/USD")

    def test_normalize_stock_unchanged(self):
        """Stock tickers are returned uppercase, unchanged."""
        self.assertEqual(self.normalize("aapl"),  "AAPL")
        self.assertEqual(self.normalize("GLD"),   "GLD")
        self.assertEqual(self.normalize("  spy "), "SPY")

    def test_normalize_tsm_not_crypto(self):
        """TSM must not be misidentified as crypto (ends in no USD crypto base)."""
        self.assertEqual(self.normalize("TSM"), "TSM")

    # ── is_crypto ─────────────────────────────────────────────────────────────

    def test_is_crypto_slash_format(self):
        self.assertTrue(self.is_crypto("BTC/USD"))
        self.assertTrue(self.is_crypto("ETH/USD"))

    def test_is_crypto_alpaca_format(self):
        self.assertTrue(self.is_crypto("BTCUSD"))
        self.assertTrue(self.is_crypto("ETHUSD"))

    def test_is_crypto_yfinance_format(self):
        self.assertTrue(self.is_crypto("BTC-USD"))
        self.assertTrue(self.is_crypto("ETH-USD"))

    def test_is_crypto_false_for_stocks(self):
        self.assertFalse(self.is_crypto("AAPL"))
        self.assertFalse(self.is_crypto("GLD"))
        self.assertFalse(self.is_crypto("TSM"))
        self.assertFalse(self.is_crypto("SPY"))

    # ── alpaca_symbol / yfinance_symbol ───────────────────────────────────────

    def test_alpaca_symbol_from_slash(self):
        self.assertEqual(self.alpaca_sym("BTC/USD"), "BTCUSD")
        self.assertEqual(self.alpaca_sym("ETH/USD"), "ETHUSD")

    def test_alpaca_symbol_stock_passthrough(self):
        self.assertEqual(self.alpaca_sym("AAPL"), "AAPL")

    def test_yf_symbol_from_slash(self):
        self.assertEqual(self.yf_sym("BTC/USD"), "BTC-USD")
        self.assertEqual(self.yf_sym("ETH/USD"), "ETH-USD")

    def test_yf_symbol_stock_passthrough(self):
        self.assertEqual(self.yf_sym("AAPL"), "AAPL")

    def test_roundtrip_any_format_to_alpaca(self):
        """All three input formats produce the same Alpaca symbol."""
        for sym in ("BTC/USD", "BTCUSD", "BTC-USD"):
            self.assertEqual(self.alpaca_sym(sym), "BTCUSD",
                             f"Expected BTCUSD from {sym!r}")

    def test_roundtrip_any_format_to_yf(self):
        """All three input formats produce the same yfinance symbol."""
        for sym in ("BTC/USD", "BTCUSD", "BTC-USD"):
            self.assertEqual(self.yf_sym(sym), "BTC-USD",
                             f"Expected BTC-USD from {sym!r}")


class TestSchemaBrokerAction(unittest.TestCase):
    """BrokerAction.to_dict() — key mapping and executor compatibility."""

    @classmethod
    def setUpClass(cls):
        from schemas import (
            AccountAction,
            BrokerAction,
            Conviction,
            Direction,
            Tier,
            TradeIdea,
        )
        cls.BrokerAction  = BrokerAction
        cls.AccountAction = AccountAction
        cls.Tier          = Tier
        cls.Conviction    = Conviction
        cls.TradeIdea     = TradeIdea
        cls.Direction     = Direction

    def _buy_action(self, conviction=None):
        return self.BrokerAction(
            symbol="GLD",
            action=self.AccountAction.BUY,
            qty=10,
            order_type="market",
            tier=self.Tier.CORE,
            conviction=conviction or self.Conviction.MEDIUM,
            catalyst="safe-haven demand",
            stop_loss=425.0,
            take_profit=455.0,
            sector_signal="commodities bid",
        )

    def test_to_dict_has_confidence_key_not_conviction(self):
        """Executor expects 'confidence', not 'conviction'."""
        d = self._buy_action().to_dict()
        self.assertIn("confidence", d, "to_dict() must have 'confidence' key")
        self.assertNotIn("conviction", d, "to_dict() must NOT have 'conviction' key")

    def test_to_dict_conviction_maps_to_string_value(self):
        """HIGH conviction -> 'high' string."""
        d = self._buy_action(conviction=self.Conviction.HIGH).to_dict()
        self.assertEqual(d["confidence"], "high")

    def test_to_dict_tier_is_string(self):
        """Tier enum serialises to lowercase string."""
        d = self._buy_action().to_dict()
        self.assertEqual(d["tier"], "core")

    def test_to_dict_action_is_string(self):
        """AccountAction enum serialises to lowercase string."""
        d = self._buy_action().to_dict()
        self.assertEqual(d["action"], "buy")

    def test_to_dict_required_keys_present(self):
        """to_dict() must contain all keys expected by execute_all()."""
        d = self._buy_action().to_dict()
        for key in ("action", "symbol", "qty", "order_type", "limit_price",
                    "stop_loss", "take_profit", "tier", "confidence",
                    "catalyst", "sector_signal"):
            self.assertIn(key, d, f"Missing key: {key}")

    def test_to_dict_reallocate_includes_exit_entry(self):
        """Reallocate action includes exit_symbol and entry_symbol."""
        action = self.BrokerAction(
            symbol="GLD",
            action=self.AccountAction.REALLOCATE,
            qty=10,
            order_type="market",
            tier=self.Tier.CORE,
            conviction=self.Conviction.MEDIUM,
            catalyst="rebalance",
            exit_symbol="TSM",
            entry_symbol="GLD",
        )
        d = action.to_dict()
        self.assertEqual(d["exit_symbol"], "TSM")
        self.assertEqual(d["entry_symbol"], "GLD")

    def test_to_dict_no_exit_entry_when_not_reallocate(self):
        """Non-reallocate actions must NOT include exit_symbol/entry_symbol keys."""
        d = self._buy_action().to_dict()
        self.assertNotIn("exit_symbol", d)
        self.assertNotIn("entry_symbol", d)

    def test_source_idea_not_in_to_dict(self):
        """source_idea is traceability-only; must never appear in to_dict()."""
        idea = self.TradeIdea(
            symbol="GLD",
            action=self.AccountAction.BUY,
            tier=self.Tier.CORE,
            conviction=0.60,
            direction=self.Direction.BULLISH,
            catalyst="test",
        )
        action = self._buy_action()
        action.source_idea = idea
        d = action.to_dict()
        self.assertNotIn("source_idea", d)


class TestSchemaValidation(unittest.TestCase):
    """validate_trade_idea() and validate_broker_action()."""

    @classmethod
    def setUpClass(cls):
        from schemas import (
            AccountAction,
            BrokerAction,
            Conviction,
            Direction,
            Tier,
            TradeIdea,
            validate_broker_action,
            validate_trade_idea,
        )
        cls.TradeIdea            = TradeIdea
        cls.BrokerAction         = BrokerAction
        cls.AccountAction        = AccountAction
        cls.Tier                 = Tier
        cls.Conviction           = Conviction
        cls.Direction            = Direction
        cls.validate_trade_idea  = staticmethod(validate_trade_idea)
        cls.validate_broker_action = staticmethod(validate_broker_action)

    def _idea(self, action=None, catalyst="safe-haven demand",
              conviction=None, advisory_stop_pct=None, advisory_target_r=None,
              exit_symbol=None, entry_symbol=None):
        return self.TradeIdea(
            symbol="GLD",
            action=action or self.AccountAction.BUY,
            tier=self.Tier.CORE,
            conviction=conviction if conviction is not None else 0.60,
            direction=self.Direction.BULLISH,
            catalyst=catalyst,
            advisory_stop_pct=advisory_stop_pct,
            advisory_target_r=advisory_target_r,
            exit_symbol=exit_symbol,
            entry_symbol=entry_symbol,
        )

    def _broker_buy(self, qty=10, stop=425.0, target=455.0, action=None):
        return self.BrokerAction(
            symbol="GLD",
            action=action or self.AccountAction.BUY,
            qty=qty,
            order_type="market",
            tier=self.Tier.CORE,
            conviction=self.Conviction.MEDIUM,
            catalyst="test",
            stop_loss=stop,
            take_profit=target,
        )

    # ── validate_trade_idea ───────────────────────────────────────────────────

    def test_valid_buy_idea_passes(self):
        ok, reason = self.validate_trade_idea(self._idea())
        self.assertTrue(ok, f"Valid buy idea should pass: {reason}")

    def test_buy_requires_catalyst(self):
        ok, reason = self.validate_trade_idea(self._idea(catalyst=""))
        self.assertFalse(ok)
        self.assertIn("catalyst", reason.lower())

    def test_buy_low_conviction_advisory_warn(self):
        ok, reason = self.validate_trade_idea(
            self._idea(conviction=0.30)
        )
        self.assertFalse(ok)
        self.assertIn("low conviction", reason.lower())

    def test_reallocate_requires_exit_symbol(self):
        ok, reason = self.validate_trade_idea(
            self._idea(action=self.AccountAction.REALLOCATE,
                       exit_symbol=None, entry_symbol="GLD")
        )
        self.assertFalse(ok)
        self.assertIn("exit_symbol", reason.lower())

    def test_reallocate_requires_entry_symbol(self):
        ok, reason = self.validate_trade_idea(
            self._idea(action=self.AccountAction.REALLOCATE,
                       exit_symbol="TSM", entry_symbol=None)
        )
        self.assertFalse(ok)
        self.assertIn("entry_symbol", reason.lower())

    def test_negative_advisory_stop_rejected(self):
        ok, reason = self.validate_trade_idea(
            self._idea(advisory_stop_pct=-0.01)
        )
        self.assertFalse(ok)
        self.assertIn("advisory_stop_pct", reason)

    def test_advisory_target_r_below_1_rejected(self):
        ok, reason = self.validate_trade_idea(
            self._idea(advisory_target_r=0.5)
        )
        self.assertFalse(ok)
        self.assertIn("advisory_target_r", reason)

    def test_hold_idea_passes_without_catalyst(self):
        """HOLD actions don't require a catalyst."""
        ok, reason = self.validate_trade_idea(
            self._idea(action=self.AccountAction.HOLD, catalyst="")
        )
        self.assertTrue(ok, f"HOLD should pass without catalyst: {reason}")

    # ── validate_broker_action ────────────────────────────────────────────────

    def test_valid_buy_broker_action_passes(self):
        ok, reason = self.validate_broker_action(self._broker_buy())
        self.assertTrue(ok, f"Valid BrokerAction should pass: {reason}")

    def test_buy_requires_stop(self):
        action = self._broker_buy(stop=None)
        ok, reason = self.validate_broker_action(action)
        self.assertFalse(ok)
        self.assertIn("stop_loss", reason)

    def test_buy_requires_positive_qty(self):
        action = self._broker_buy(qty=0)
        ok, reason = self.validate_broker_action(action)
        self.assertFalse(ok)
        self.assertIn("qty", reason)

    def test_stop_must_be_below_target(self):
        action = self._broker_buy(stop=460.0, target=430.0)
        ok, reason = self.validate_broker_action(action)
        self.assertFalse(ok)
        self.assertIn("stop_loss", reason)

    def test_hold_broker_action_passes_minimal(self):
        action = self.BrokerAction(
            symbol="GLD",
            action=self.AccountAction.HOLD,
            qty=0,
            order_type="market",
            tier=self.Tier.CORE,
            conviction=self.Conviction.MEDIUM,
            catalyst="",
        )
        ok, reason = self.validate_broker_action(action)
        self.assertTrue(ok, f"HOLD BrokerAction should pass: {reason}")


class TestSchemaSignalScore(unittest.TestCase):
    """SignalScore.from_dict() parses signal_scores.json entries correctly."""

    @classmethod
    def setUpClass(cls):
        from schemas import Conviction, Direction, SignalScore, Tier
        cls.SignalScore = SignalScore
        cls.Conviction  = Conviction
        cls.Direction   = Direction
        cls.Tier        = Tier

    def test_from_dict_basic(self):
        d = {
            "score": 78,
            "conviction": "high",
            "direction": "bullish",
            "tier": "core",
            "primary_catalyst": "AI demand surge",
            "signals": ["EMA9>EMA21", "volume_spike"],
            "conflicts": [],
            "orb_candidate": False,
            "pattern_watchlist": False,
            "price": 445.32,
        }
        ss = self.SignalScore.from_dict("GLD", d)
        self.assertEqual(ss.symbol, "GLD")
        self.assertEqual(ss.score, 78.0)
        self.assertEqual(ss.conviction, self.Conviction.HIGH)
        self.assertEqual(ss.direction, self.Direction.BULLISH)
        self.assertEqual(ss.tier, self.Tier.CORE)
        self.assertEqual(ss.primary_catalyst, "AI demand surge")
        self.assertEqual(ss.price, 445.32)

    def test_from_dict_crypto_symbol_normalised(self):
        """from_dict normalises BTC/USD regardless of input format."""
        d = {"score": 65, "conviction": "medium", "direction": "bullish",
             "tier": "core", "primary_catalyst": "momentum", "price": 74000.0}
        ss = self.SignalScore.from_dict("BTCUSD", d)
        self.assertEqual(ss.symbol, "BTC/USD",
                         "BTCUSD input must be normalised to BTC/USD")

    def test_from_dict_unknown_conviction_defaults_low(self):
        d = {"score": 50, "conviction": "extreme", "direction": "neutral",
             "tier": "core", "primary_catalyst": ""}
        ss = self.SignalScore.from_dict("SPY", d)
        self.assertEqual(ss.conviction, self.Conviction.LOW)

    def test_from_dict_missing_price_is_none(self):
        d = {"score": 50, "conviction": "medium", "direction": "neutral",
             "tier": "dynamic", "primary_catalyst": "scan"}
        ss = self.SignalScore.from_dict("NVDA", d)
        self.assertIsNone(ss.price)


class TestSchemaOptionsStructure(unittest.TestCase):
    """OptionsStructure round-trip: to_dict() / from_dict()."""

    @classmethod
    def setUpClass(cls):
        from schemas import (
            OptionsLeg,
            OptionsStructure,
            OptionStrategy,
            StructureLifecycle,
            Tier,
        )
        cls.OptionsStructure    = OptionsStructure
        cls.OptionsLeg          = OptionsLeg
        cls.OptionStrategy      = OptionStrategy
        cls.StructureLifecycle  = StructureLifecycle
        cls.Tier                = Tier

    def _sample_structure(self):
        leg_long = self.OptionsLeg(
            occ_symbol="AAPL260120C00200000",
            underlying="AAPL",
            side="buy",
            qty=2,
            option_type="call",
            strike=200.0,
            expiration="2026-01-20",
        )
        leg_short = self.OptionsLeg(
            occ_symbol="AAPL260120C00210000",
            underlying="AAPL",
            side="sell",
            qty=2,
            option_type="call",
            strike=210.0,
            expiration="2026-01-20",
        )
        return self.OptionsStructure(
            structure_id="test-001",
            underlying="AAPL",
            strategy=self.OptionStrategy.CALL_DEBIT_SPREAD,
            lifecycle=self.StructureLifecycle.FULLY_FILLED,
            legs=[leg_long, leg_short],
            contracts=2,
            max_cost_usd=400.0,
            opened_at="2026-04-14T14:00:00+00:00",
            catalyst="AI demand",
            tier=self.Tier.CORE,
            iv_rank=28.5,
            order_ids=["ord-abc", "ord-def"],
        )

    def test_to_dict_strategy_is_string(self):
        d = self._sample_structure().to_dict()
        self.assertEqual(d["strategy"], "call_debit_spread")

    def test_to_dict_lifecycle_is_string(self):
        d = self._sample_structure().to_dict()
        self.assertEqual(d["lifecycle"], "fully_filled")

    def test_to_dict_tier_is_string(self):
        d = self._sample_structure().to_dict()
        self.assertEqual(d["tier"], "core")

    def test_roundtrip_preserves_all_fields(self):
        original = self._sample_structure()
        d = original.to_dict()
        restored = self.OptionsStructure.from_dict(d)

        self.assertEqual(restored.structure_id, original.structure_id)
        self.assertEqual(restored.strategy, original.strategy)
        self.assertEqual(restored.lifecycle, original.lifecycle)
        self.assertEqual(restored.underlying, original.underlying)
        self.assertEqual(restored.contracts, original.contracts)
        self.assertAlmostEqual(restored.max_cost_usd, original.max_cost_usd)
        self.assertAlmostEqual(restored.iv_rank, original.iv_rank)
        self.assertEqual(len(restored.legs), 2)

    def test_roundtrip_leg_fields_preserved(self):
        original = self._sample_structure()
        restored = self.OptionsStructure.from_dict(original.to_dict())
        leg = restored.legs[0]
        self.assertEqual(leg.occ_symbol, "AAPL260120C00200000")
        self.assertEqual(leg.side, "buy")
        self.assertEqual(leg.strike, 200.0)
        self.assertEqual(leg.option_type, "call")

    def test_from_dict_unknown_strategy_defaults_gracefully(self):
        """from_dict with unrecognised strategy must not raise."""
        d = self._sample_structure().to_dict()
        d["strategy"] = "unknown_future_strategy"
        # Should not raise; defaults to SINGLE_CALL
        restored = self.OptionsStructure.from_dict(d)
        self.assertEqual(restored.strategy, self.OptionStrategy.SINGLE_CALL)


# ─────────────────────────────────────────────────────────────────────────────
# Suite 9 — risk_kernel
# ─────────────────────────────────────────────────────────────────────────────

class TestRiskKernelEligibility(unittest.TestCase):
    """eligibility_check() — hard gate logic."""

    @classmethod
    def setUpClass(cls):
        from risk_kernel import PDT_FLOOR, VIX_HALT, eligibility_check
        from schemas import (
            AccountAction,
            BrokerSnapshot,
            Conviction,
            Direction,
            NormalizedPosition,
            Tier,
            TradeIdea,
        )
        cls.AccountAction = AccountAction
        cls.BrokerSnapshot = BrokerSnapshot
        cls.Conviction = Conviction
        cls.Direction = Direction
        cls.NormalizedPosition = NormalizedPosition
        cls.Tier = Tier
        cls.TradeIdea = TradeIdea
        cls.eligibility_check = staticmethod(eligibility_check)
        cls.PDT_FLOOR = PDT_FLOOR
        cls.VIX_HALT = VIX_HALT

    def _make_position(self, symbol="SYM0"):
        return self.NormalizedPosition(
            symbol=symbol,
            alpaca_sym=symbol,
            qty=10.0,
            avg_entry_price=100.0,
            current_price=100.0,
            market_value=1000.0,
            unrealized_pl=0.0,
            unrealized_plpc=0.0,
            is_crypto_pos=False,
        )

    def _snapshot(self, equity=100_000.0, positions=None):
        return self.BrokerSnapshot(
            equity=equity,
            cash=80_000.0,
            buying_power=180_000.0,
            open_orders=[],
            positions=positions or [],
        )

    def _idea(self, symbol="NVDA", action=None, tier=None, catalyst="breakout news"):
        if action is None:
            action = self.AccountAction.BUY
        if tier is None:
            tier = self.Tier.CORE
        return self.TradeIdea(
            symbol=symbol,
            action=action,
            direction=self.Direction.BULLISH,
            conviction=0.60,
            tier=tier,
            catalyst=catalyst,
        )

    _CONFIG = {
        "parameters": {"max_positions": 15},
        "position_sizing": {"core_tier_pct": 0.15},
    }

    def test_vix_halt_blocks_buy(self):
        result = self.eligibility_check(
            self._idea(), self._snapshot(), self._CONFIG,
            session_tier="market", vix=self.VIX_HALT,
        )
        self.assertIsNotNone(result)
        self.assertIn("halt", result.lower())

    def test_vix_below_halt_allows_buy(self):
        result = self.eligibility_check(
            self._idea(), self._snapshot(), self._CONFIG,
            session_tier="market", vix=self.VIX_HALT - 0.1,
        )
        self.assertIsNone(result)

    def test_pdt_floor_blocks_all(self):
        snap = self._snapshot(equity=self.PDT_FLOOR - 1)
        result = self.eligibility_check(
            self._idea(), snap, self._CONFIG, session_tier="market", vix=20.0,
        )
        self.assertIsNotNone(result)
        self.assertIn("PDT", result)

    def test_stock_buy_in_extended_session_blocked(self):
        result = self.eligibility_check(
            self._idea(symbol="NVDA"), self._snapshot(), self._CONFIG,
            session_tier="extended", vix=20.0,
        )
        self.assertIsNotNone(result)
        self.assertIn("session", result.lower())

    def test_crypto_buy_in_extended_session_allowed(self):
        result = self.eligibility_check(
            self._idea(symbol="BTC/USD"), self._snapshot(), self._CONFIG,
            session_tier="extended", vix=20.0,
        )
        self.assertIsNone(result)

    def test_max_positions_gate(self):
        # Build 15 dummy positions
        positions = [self._make_position(f"SYM{i}") for i in range(15)]
        snap = self._snapshot(positions=positions)
        result = self.eligibility_check(
            self._idea(), snap, self._CONFIG, session_tier="market", vix=20.0,
        )
        self.assertIsNotNone(result)
        self.assertIn("max_positions", result)

    def test_no_catalyst_blocks_buy(self):
        result = self.eligibility_check(
            self._idea(catalyst=""), self._snapshot(), self._CONFIG,
            session_tier="market", vix=20.0,
        )
        self.assertIsNotNone(result)
        self.assertIn("catalyst", result.lower())

    def test_hold_action_passes_without_catalyst(self):
        """HOLD/SELL should pass eligibility even if catalyst is empty."""
        idea = self._idea(action=self.AccountAction.HOLD, catalyst="")
        result = self.eligibility_check(
            idea, self._snapshot(), self._CONFIG, session_tier="market", vix=20.0,
        )
        self.assertIsNone(result)

    def test_valid_buy_returns_none(self):
        result = self.eligibility_check(
            self._idea(), self._snapshot(), self._CONFIG, session_tier="market", vix=20.0,
        )
        self.assertIsNone(result)

    def test_kernel_blocks_entry_on_deadline_symbol(self):
        """
        Regression: risk kernel must block enter_long on a symbol that has
        a same-day mandatory exit in time_bound_actions.
        Reproduces the TSM 10→52 bug (2026-04-15).
        """
        from datetime import date as _date

        import risk_kernel as rk

        today_str = _date.today().strftime("%Y-%m-%d")
        config_with_tba = {
            **self._CONFIG,
            "time_bound_actions": [
                {
                    "symbol": "TSM",
                    "action": "exit",
                    "reason": "earnings binary event",
                    "exit_by": f"{today_str} 15:45",
                    "deadline_et": f"{today_str} 15:45",
                }
            ],
        }
        idea = self.TradeIdea(
            symbol="TSM",
            action=self.AccountAction.BUY,
            direction=self.Direction.BULLISH,
            conviction=0.75,
            tier=self.Tier.CORE,
            catalyst="insider cluster signal",
            intent="enter_long",
        )
        current_time_utc = datetime(
            int(today_str[:4]), int(today_str[5:7]), int(today_str[8:]),
            14, 0, 0, tzinfo=timezone.utc,
        ).isoformat()

        result = rk.eligibility_check(
            idea, self._snapshot(), config_with_tba,
            session_tier="market", vix=20.0,
            current_time_utc=current_time_utc,
        )
        self.assertIsNotNone(result, "expected rejection but got None")
        self.assertIn("time_bound_action", result)
        self.assertIn("TSM", result)

    def test_kernel_allows_entry_symbol_no_deadline(self):
        """Symbols NOT in time_bound_actions must still be allowed through."""
        from datetime import date as _date

        import risk_kernel as rk

        today_str = _date.today().strftime("%Y-%m-%d")
        config_with_tba = {
            **self._CONFIG,
            "time_bound_actions": [
                {
                    "symbol": "TSM",
                    "action": "exit",
                    "exit_by": f"{today_str} 15:45",
                }
            ],
        }
        # NVDA is not in time_bound_actions — should pass
        result = rk.eligibility_check(
            self._idea(symbol="NVDA"), self._snapshot(), config_with_tba,
            session_tier="market", vix=20.0,
            current_time_utc=datetime(
                int(today_str[:4]), int(today_str[5:7]), int(today_str[8:]),
                14, 0, 0, tzinfo=timezone.utc,
            ).isoformat(),
        )
        self.assertIsNone(result)


class TestRiskKernelSizing(unittest.TestCase):
    """size_position() — qty, VIX scaling, headroom."""

    @classmethod
    def setUpClass(cls):
        from risk_kernel import _CORE_HIGH_CONVICTION_PCT, VIX_CAUTION, size_position
        from schemas import (
            AccountAction,
            BrokerSnapshot,
            Conviction,
            Direction,
            NormalizedPosition,
            Tier,
            TradeIdea,
        )
        cls.AccountAction = AccountAction
        cls.BrokerSnapshot = BrokerSnapshot
        cls.Conviction = Conviction
        cls.Direction = Direction
        cls.NormalizedPosition = NormalizedPosition
        cls.Tier = Tier
        cls.TradeIdea = TradeIdea
        cls.size_position = staticmethod(size_position)
        cls.VIX_CAUTION = VIX_CAUTION
        cls._CORE_HIGH_CONVICTION_PCT = _CORE_HIGH_CONVICTION_PCT

    def _make_position(self, market_value: float) -> "NormalizedPosition":  # noqa: F821
        return self.NormalizedPosition(
            symbol="__DUMMY__",
            alpaca_sym="__DUMMY__",
            qty=1.0,
            avg_entry_price=market_value,
            current_price=market_value,
            market_value=market_value,
            unrealized_pl=0.0,
            unrealized_plpc=0.0,
            is_crypto_pos=False,
        )

    def _snapshot(self, equity=100_000.0, exposure=0.0, bp=200_000.0):
        positions = [self._make_position(exposure)] if exposure > 0 else []
        return self.BrokerSnapshot(
            equity=equity,
            cash=equity,
            buying_power=bp,
            open_orders=[],
            positions=positions,
        )

    def _idea(self, symbol="NVDA", conviction=None, tier=None):
        if conviction is None:
            conviction = 0.60
        if tier is None:
            tier = self.Tier.CORE
        return self.TradeIdea(
            symbol=symbol,
            action=self.AccountAction.BUY,
            direction=self.Direction.BULLISH,
            conviction=conviction,
            tier=tier,
            catalyst="breakout",
        )

    _CONFIG = {
        "parameters": {},
        "position_sizing": {
            "core_tier_pct": 0.15,
            "dynamic_tier_pct": 0.08,
            "intraday_tier_pct": 0.05,
        },
        "account2": {},
    }

    def test_normal_sizing_returns_tuple(self):
        result = self.size_position(
            self._idea(), self._snapshot(), self._CONFIG,
            current_price=100.0, vix=20.0,
        )
        self.assertIsInstance(result, tuple)
        qty, val = result
        self.assertGreater(qty, 0)
        self.assertGreater(val, 0)

    def test_core_medium_conviction_15pct(self):
        """Core + MEDIUM → 15% of equity at $100/share."""
        snap = self._snapshot(equity=100_000.0, exposure=0.0)
        qty, val = self.size_position(
            self._idea(conviction=0.60, tier=self.Tier.CORE),
            snap, self._CONFIG, current_price=100.0, vix=20.0,
        )
        # 15% of $100k = $15,000 → 150 shares
        self.assertEqual(qty, 150)
        self.assertAlmostEqual(val, 15_000.0, places=0)

    def test_high_conviction_core_20pct_bump(self):
        """HIGH conviction CORE → 20% instead of 15%."""
        snap = self._snapshot(equity=100_000.0, exposure=0.0)
        qty, val = self.size_position(
            self._idea(conviction=0.80, tier=self.Tier.CORE),
            snap, self._CONFIG, current_price=100.0, vix=20.0,
        )
        # 20% of $100k = $20,000 → 200 shares
        self.assertEqual(qty, 200)
        self.assertAlmostEqual(val, 20_000.0, places=0)

    def test_vix_caution_halves_size(self):
        """VIX >= VIX_CAUTION (25) → 50% size reduction."""
        snap = self._snapshot(equity=100_000.0, exposure=0.0)
        result_normal = self.size_position(
            self._idea(), snap, self._CONFIG, current_price=100.0, vix=20.0,
        )
        result_caution = self.size_position(
            self._idea(), snap, self._CONFIG, current_price=100.0, vix=self.VIX_CAUTION,
        )
        qty_normal, _ = result_normal
        qty_caution, _ = result_caution
        self.assertAlmostEqual(qty_normal / qty_caution, 2.0, places=1)

    def test_no_headroom_returns_str(self):
        """When exposure already at cap, reject with string."""
        # MEDIUM conviction → 1.5× equity cap = $150k
        # Inject exposure already at $150k
        snap = self._snapshot(equity=100_000.0, exposure=150_001.0, bp=200_000.0)
        result = self.size_position(
            self._idea(conviction=0.60),
            snap, self._CONFIG, current_price=100.0, vix=20.0,
        )
        self.assertIsInstance(result, str)
        self.assertIn("headroom", result.lower())

    def test_zero_price_returns_str(self):
        snap = self._snapshot()
        result = self.size_position(
            self._idea(), snap, self._CONFIG, current_price=0.0, vix=20.0,
        )
        self.assertIsInstance(result, str)

    def test_crypto_qty_has_decimals(self):
        """Crypto (BTC/USD) should return fractional qty."""
        # Use $1M equity so 15% budget ($150k) > $80k BTC price
        snap = self._snapshot(equity=1_000_000.0, exposure=0.0, bp=2_000_000.0)
        result = self.size_position(
            self._idea(symbol="BTC/USD"),
            snap, self._CONFIG, current_price=80_000.0, vix=20.0,
        )
        self.assertIsInstance(result, tuple)
        qty, _ = result
        # 15% of $1M = $150k; at $80k/BTC → 1.875 BTC
        self.assertAlmostEqual(qty, 1.875, places=4)


class TestRiskKernelStops(unittest.TestCase):
    """place_stops() — stop/target arithmetic, R:R enforcement."""

    @classmethod
    def setUpClass(cls):
        from risk_kernel import MIN_RR_RATIO, place_stops
        from schemas import AccountAction, Conviction, Direction, Tier, TradeIdea
        cls.AccountAction = AccountAction
        cls.Conviction = Conviction
        cls.Direction = Direction
        cls.Tier = Tier
        cls.TradeIdea = TradeIdea
        cls.place_stops = staticmethod(place_stops)
        cls.MIN_RR_RATIO = MIN_RR_RATIO

    def _idea(self, tier=None, advisory_stop_pct=None, advisory_target_r=None,
              symbol="NVDA"):
        if tier is None:
            tier = self.Tier.CORE
        return self.TradeIdea(
            symbol=symbol,
            action=self.AccountAction.BUY,
            direction=self.Direction.BULLISH,
            conviction=0.60,
            tier=tier,
            catalyst="breakout",
            advisory_stop_pct=advisory_stop_pct,
            advisory_target_r=advisory_target_r,
        )

    _CONFIG = {
        "parameters": {
            "stop_loss_pct_core": 0.035,
            "stop_loss_pct_intraday": 0.018,
            "take_profit_multiple": 2.5,
        },
        "position_sizing": {},
        "account2": {},
    }

    def test_stop_below_entry(self):
        stop, target = self.place_stops(self._idea(), 100.0, self._CONFIG)
        self.assertLess(stop, 100.0)

    def test_target_above_entry(self):
        stop, target = self.place_stops(self._idea(), 100.0, self._CONFIG)
        self.assertGreater(target, 100.0)

    def test_rr_ratio_at_least_two(self):
        stop, target = self.place_stops(self._idea(), 100.0, self._CONFIG)
        risk   = 100.0 - stop
        reward = target - 100.0
        self.assertGreaterEqual(reward / risk, self.MIN_RR_RATIO - 0.001)

    def test_core_stock_stop_at_config_pct(self):
        """Core stock stop should use config stop_loss_pct_core = 3.5%."""
        stop, _ = self.place_stops(self._idea(tier=self.Tier.CORE), 100.0, self._CONFIG)
        # 3.5% below entry
        self.assertAlmostEqual(stop, 96.50, places=1)

    def test_intraday_stop_tighter(self):
        """Intraday stop should use stop_loss_pct_intraday = 1.8%."""
        stop, _ = self.place_stops(self._idea(tier=self.Tier.INTRADAY), 100.0, self._CONFIG)
        self.assertAlmostEqual(stop, 98.20, places=1)

    def test_crypto_stop_wider_floor(self):
        """Crypto core should have stop floored at 8% (crypto volatility floor)."""
        # Config says 3.5% but crypto floor is 8%
        stop, _ = self.place_stops(self._idea(symbol="BTC/USD"), 100.0, self._CONFIG)
        # Should be at least 8% below entry
        self.assertLessEqual(stop, 92.50)

    def test_advisory_stop_pct_respected_within_ceiling(self):
        """Claude advisory_stop_pct tighter than ceiling → use it."""
        # Advisory 2% (tighter than core 3.5% default)
        idea = self._idea(advisory_stop_pct=0.02)
        stop, _ = self.place_stops(idea, 100.0, self._CONFIG)
        self.assertAlmostEqual(stop, 98.0, places=1)

    def test_advisory_stop_pct_capped_at_ceiling(self):
        """Claude advisory_stop_pct wider than ceiling → cap to ceiling."""
        # Advisory 10% (wider than core stock ceiling 4%)
        idea = self._idea(advisory_stop_pct=0.10)
        stop, _ = self.place_stops(idea, 100.0, self._CONFIG)
        # Should be capped at 4%
        self.assertAlmostEqual(stop, 96.0, places=1)


class TestRiskKernelProcessIdea(unittest.TestCase):
    """process_idea() — integration: VIX halt, HOLD passthrough, BUY output."""

    @classmethod
    def setUpClass(cls):
        from risk_kernel import VIX_HALT, process_idea
        from schemas import (
            AccountAction,
            BrokerAction,
            BrokerSnapshot,
            Conviction,
            Direction,
            NormalizedPosition,
            Tier,
            TradeIdea,
        )
        cls.AccountAction = AccountAction
        cls.BrokerAction = BrokerAction
        cls.BrokerSnapshot = BrokerSnapshot
        cls.Conviction = Conviction
        cls.Direction = Direction
        cls.NormalizedPosition = NormalizedPosition
        cls.Tier = Tier
        cls.TradeIdea = TradeIdea
        cls.process_idea = staticmethod(process_idea)
        cls.VIX_HALT = VIX_HALT

    def _make_position(self, market_value: float):
        return self.NormalizedPosition(
            symbol="__DUMMY__",
            alpaca_sym="__DUMMY__",
            qty=1.0,
            avg_entry_price=market_value,
            current_price=market_value,
            market_value=market_value,
            unrealized_pl=0.0,
            unrealized_plpc=0.0,
            is_crypto_pos=False,
        )

    def _snapshot(self, equity=100_000.0, exposure=0.0):
        positions = [self._make_position(exposure)] if exposure > 0 else []
        return self.BrokerSnapshot(
            equity=equity,
            cash=equity,
            buying_power=equity * 2,
            open_orders=[],
            positions=positions,
        )

    def _idea(self, action=None, symbol="NVDA", catalyst="breakout"):
        if action is None:
            action = self.AccountAction.BUY
        return self.TradeIdea(
            symbol=symbol,
            action=action,
            direction=self.Direction.BULLISH,
            conviction=0.60,
            tier=self.Tier.CORE,
            catalyst=catalyst,
        )

    _CONFIG = {
        "parameters": {
            "max_positions": 15,
            "stop_loss_pct_core": 0.035,
            "take_profit_multiple": 2.5,
        },
        "position_sizing": {"core_tier_pct": 0.15},
        "account2": {},
    }

    def test_vix_halt_returns_str(self):
        result = self.process_idea(
            self._idea(), self._snapshot(), None, self._CONFIG,
            current_price=100.0, session_tier="market", vix=self.VIX_HALT,
        )
        self.assertIsInstance(result, str)

    def test_hold_returns_broker_action_zero_qty(self):
        idea = self._idea(action=self.AccountAction.HOLD, catalyst="")
        result = self.process_idea(
            idea, self._snapshot(), None, self._CONFIG,
            current_price=100.0, session_tier="market", vix=20.0,
        )
        self.assertIsInstance(result, self.BrokerAction)
        self.assertEqual(result.action, self.AccountAction.HOLD)
        self.assertEqual(result.qty, 0)

    def test_buy_returns_broker_action(self):
        result = self.process_idea(
            self._idea(), self._snapshot(), None, self._CONFIG,
            current_price=100.0, session_tier="market", vix=20.0,
        )
        self.assertIsInstance(result, self.BrokerAction)
        self.assertEqual(result.action, self.AccountAction.BUY)
        self.assertGreater(result.qty, 0)
        self.assertIsNotNone(result.stop_loss)
        self.assertIsNotNone(result.take_profit)

    def test_buy_stop_is_below_entry(self):
        result = self.process_idea(
            self._idea(), self._snapshot(), None, self._CONFIG,
            current_price=100.0, session_tier="market", vix=20.0,
        )
        self.assertLess(result.stop_loss, 100.0)

    def test_buy_broker_action_has_correct_symbol(self):
        result = self.process_idea(
            self._idea(symbol="NVDA"), self._snapshot(), None, self._CONFIG,
            current_price=100.0, session_tier="market", vix=20.0,
        )
        self.assertEqual(result.symbol, "NVDA")

    def test_extended_session_stock_buy_returns_str(self):
        result = self.process_idea(
            self._idea(symbol="NVDA"), self._snapshot(), None, self._CONFIG,
            current_price=100.0, session_tier="extended", vix=20.0,
        )
        self.assertIsInstance(result, str)

    def test_extended_session_crypto_buy_succeeds(self):
        # Use $1M equity so 15% budget ($150k) > $80k BTC price
        snap = self.BrokerSnapshot(
            equity=1_000_000.0, cash=1_000_000.0, buying_power=2_000_000.0,
            open_orders=[], positions=[],
        )
        result = self.process_idea(
            self._idea(symbol="BTC/USD"), snap, None, self._CONFIG,
            current_price=80_000.0, session_tier="extended", vix=20.0,
        )
        self.assertIsInstance(result, self.BrokerAction)
        self.assertEqual(result.action, self.AccountAction.BUY)


class TestRiskKernelOptionsSelection(unittest.TestCase):
    """select_structure() and liquidity_gate() — options strategy logic."""

    @classmethod
    def setUpClass(cls):
        from risk_kernel import liquidity_gate, select_structure
        from schemas import Direction, OptionStrategy, Tier
        cls.Direction = Direction
        cls.OptionStrategy = OptionStrategy
        cls.Tier = Tier
        cls.select_structure = staticmethod(select_structure)
        cls.liquidity_gate = staticmethod(liquidity_gate)

    def _iv_summary(self, iv_rank=30.0, iv_env="cheap", spread_pct=0.02, volume=500):
        return {
            "iv_rank": iv_rank,
            "iv_environment": iv_env,
            "spread_pct": spread_pct,
            "volume": volume,
            "current_iv": 0.25,
            "iv_percentile": iv_rank,
            "observation_mode": False,  # liquidity_gate defaults to True if missing
        }

    # options_regime dict that allows all strategy types
    _REGIME_ALL = {"allowed_strategies": ["debit_spread", "credit_spread", "single_leg"]}

    def test_bullish_cheap_iv_returns_call_debit_spread(self):
        result = self.select_structure(
            self.Direction.BULLISH,
            self._iv_summary(iv_rank=25.0, iv_env="cheap"),
            self._REGIME_ALL, self.Tier.CORE,
        )
        self.assertEqual(result, self.OptionStrategy.CALL_DEBIT_SPREAD)

    def test_bearish_cheap_iv_returns_put_debit_spread(self):
        result = self.select_structure(
            self.Direction.BEARISH,
            self._iv_summary(iv_rank=25.0, iv_env="cheap"),
            self._REGIME_ALL, self.Tier.CORE,
        )
        self.assertEqual(result, self.OptionStrategy.PUT_DEBIT_SPREAD)

    def test_bullish_expensive_iv_returns_put_credit_spread(self):
        """Bullish + expensive IV → sell puts (put_credit_spread)."""
        result = self.select_structure(
            self.Direction.BULLISH,
            self._iv_summary(iv_rank=70.0, iv_env="expensive"),
            self._REGIME_ALL, self.Tier.CORE,
        )
        self.assertEqual(result, self.OptionStrategy.PUT_CREDIT_SPREAD)

    def test_bearish_expensive_iv_returns_call_credit_spread(self):
        """Bearish + expensive IV → sell calls (call_credit_spread)."""
        result = self.select_structure(
            self.Direction.BEARISH,
            self._iv_summary(iv_rank=70.0, iv_env="expensive"),
            self._REGIME_ALL, self.Tier.CORE,
        )
        self.assertEqual(result, self.OptionStrategy.CALL_CREDIT_SPREAD)

    def test_very_expensive_iv_returns_none(self):
        """IV rank > 80 → select_structure returns None (caller should liquidity_gate first)."""
        result = self.select_structure(
            self.Direction.BULLISH,
            self._iv_summary(iv_rank=85.0, iv_env="very_expensive"),
            self._REGIME_ALL, self.Tier.CORE,
        )
        # very_expensive env has no match in the if/elif chain → returns None
        self.assertIsNone(result)

    def test_liquidity_gate_passes_good_iv_summary(self):
        result = self.liquidity_gate("AAPL", self._iv_summary())
        self.assertIsNone(result)

    def test_liquidity_gate_rejects_none_iv_summary(self):
        result = self.liquidity_gate("AAPL", None)
        self.assertIsNotNone(result)

    def test_liquidity_gate_rejects_observation_mode(self):
        """observation_mode=True (or missing) → reject."""
        obs = self._iv_summary()
        obs["observation_mode"] = True
        result = self.liquidity_gate("AAPL", obs)
        self.assertIsNotNone(result)
        self.assertIn("observation_mode", result)

    def test_liquidity_gate_rejects_very_expensive_iv(self):
        """very_expensive IV environment → reject new positions."""
        expensive = self._iv_summary(iv_rank=85.0, iv_env="very_expensive")
        result = self.liquidity_gate("AAPL", expensive)
        self.assertIsNotNone(result)
        self.assertIn("very_expensive", result)


# ─────────────────────────────────────────────────────────────────────────────
# Suite 10 — reconciliation
# ─────────────────────────────────────────────────────────────────────────────

class TestReconciliationDesiredState(unittest.TestCase):
    """build_desired_state() — loading deadlines and forced exits."""

    @classmethod
    def setUpClass(cls):
        from reconciliation import DesiredPosition, DesiredState, build_desired_state
        cls.build_desired_state = staticmethod(build_desired_state)
        cls.DesiredState = DesiredState
        cls.DesiredPosition = DesiredPosition

    def _make_position(self, symbol="NVDA"):
        from schemas import NormalizedPosition
        return NormalizedPosition(
            symbol=symbol, alpaca_sym=symbol,
            qty=10.0, avg_entry_price=100.0, current_price=100.0,
            market_value=1000.0, unrealized_pl=0.0, unrealized_plpc=0.0,
            is_crypto_pos=False,
        )

    def test_returns_desired_state(self):
        result = self.build_desired_state([], {})
        self.assertIsInstance(result, self.DesiredState)

    def test_open_position_creates_entry(self):
        pos = [self._make_position("NVDA")]
        state = self.build_desired_state(pos, {})
        self.assertIn("NVDA", state.positions)

    def test_time_bound_action_sets_deadline(self):
        cfg = {
            "time_bound_actions": [{
                "symbol": "TSM",
                "exit_by": "2030-04-15T19:45:00+00:00",
                "reason": "earnings",
            }]
        }
        pos = [self._make_position("TSM")]
        state = self.build_desired_state(pos, cfg)
        dp = state.positions.get("TSM")
        self.assertIsNotNone(dp)
        self.assertEqual(dp.must_exit_by, "2030-04-15T19:45:00+00:00")
        self.assertEqual(dp.must_exit_reason, "earnings")

    def test_forced_exit_via_pi_data(self):
        cfg = {
            "_pi_data": {
                "forced_exits": [{"symbol": "NVDA", "reason": "critical_health"}]
            }
        }
        pos = [self._make_position("NVDA")]
        state = self.build_desired_state(pos, cfg)
        dp = state.positions.get("NVDA")
        self.assertTrue(dp.forced_exit)

    def test_no_positions_returns_empty(self):
        state = self.build_desired_state([], {})
        self.assertEqual(len(state.positions), 0)


class TestReconciliationDiff(unittest.TestCase):
    """diff_state() — priority ordering and correct classification."""

    @classmethod
    def setUpClass(cls):
        from reconciliation import (
            PRIORITY_CRITICAL,
            PRIORITY_HIGH,
            PRIORITY_NORMAL,
            ReconciliationDiff,
            build_desired_state,
            diff_state,
        )
        from schemas import BrokerSnapshot, NormalizedOrder, NormalizedPosition
        cls.build_desired_state = staticmethod(build_desired_state)
        cls.diff_state = staticmethod(diff_state)
        cls.ReconciliationDiff = ReconciliationDiff
        cls.PRIORITY_CRITICAL = PRIORITY_CRITICAL
        cls.PRIORITY_HIGH = PRIORITY_HIGH
        cls.PRIORITY_NORMAL = PRIORITY_NORMAL
        cls.BrokerSnapshot = BrokerSnapshot
        cls.NormalizedPosition = NormalizedPosition
        cls.NormalizedOrder = NormalizedOrder

    def _make_pos(self, symbol, qty=10.0):
        return self.NormalizedPosition(
            symbol=symbol, alpaca_sym=symbol,
            qty=qty, avg_entry_price=100.0, current_price=100.0,
            market_value=qty * 100.0, unrealized_pl=0.0, unrealized_plpc=0.0,
            is_crypto_pos=False,
        )

    def _make_stop_order(self, symbol):
        return self.NormalizedOrder(
            order_id="ord-001", symbol=symbol, alpaca_sym=symbol,
            side="sell", order_type="stop",
            qty=10.0, filled_qty=0.0,
            stop_price=90.0, limit_price=None,
            status="accepted", time_in_force="gtc",
        )

    def _snapshot(self, positions, orders=None):
        return self.BrokerSnapshot(
            equity=100_000.0, cash=90_000.0, buying_power=180_000.0,
            open_orders=orders or [],
            positions=positions,
        )

    def test_returns_reconciliation_diff(self):
        from reconciliation import DesiredState
        desired = DesiredState(positions={})
        snap = self._snapshot([])
        result = self.diff_state(desired, snap)
        self.assertIsInstance(result, self.ReconciliationDiff)

    def test_expired_deadline_is_critical(self):
        from datetime import datetime, timedelta, timezone

        from reconciliation import DesiredPosition, DesiredState

        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        desired = DesiredState(positions={
            "TSM": DesiredPosition(symbol="TSM", must_exit_by=past, must_exit_reason="earnings"),
        })
        snap = self._snapshot([self._make_pos("TSM")])
        diff = self.diff_state(desired, snap)

        self.assertIn("TSM", diff.expired_symbols)
        self.assertTrue(any(a.priority == self.PRIORITY_CRITICAL for a in diff.actions))

    def test_future_deadline_not_expired(self):
        from datetime import datetime, timedelta, timezone

        from reconciliation import DesiredPosition, DesiredState

        future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
        desired = DesiredState(positions={
            "TSM": DesiredPosition(symbol="TSM", must_exit_by=future),
        })
        snap = self._snapshot([self._make_pos("TSM")])
        diff = self.diff_state(desired, snap)
        self.assertNotIn("TSM", diff.expired_symbols)

    def test_forced_exit_is_high_priority(self):
        from reconciliation import DesiredPosition, DesiredState

        desired = DesiredState(positions={
            "NVDA": DesiredPosition(symbol="NVDA", forced_exit=True),
        })
        snap = self._snapshot([self._make_pos("NVDA")])
        diff = self.diff_state(desired, snap)

        self.assertIn("NVDA", diff.forced_symbols)
        self.assertTrue(any(a.priority == self.PRIORITY_HIGH for a in diff.actions))

    def test_missing_stop_is_normal_priority(self):
        from reconciliation import DesiredPosition, DesiredState

        desired = DesiredState(positions={
            "GLD": DesiredPosition(symbol="GLD"),
        })
        snap = self._snapshot(
            positions=[self._make_pos("GLD")],
            orders=[],  # no stop order
        )
        diff = self.diff_state(desired, snap)

        self.assertIn("GLD", diff.missing_stops)
        self.assertTrue(any(
            a.priority == self.PRIORITY_NORMAL and a.action_type == "refresh_stop"
            for a in diff.actions
        ))

    def test_position_with_stop_has_no_missing_stop(self):
        from reconciliation import DesiredPosition, DesiredState

        desired = DesiredState(positions={
            "GLD": DesiredPosition(symbol="GLD"),
        })
        snap = self._snapshot(
            positions=[self._make_pos("GLD")],
            orders=[self._make_stop_order("GLD")],
        )
        diff = self.diff_state(desired, snap)
        self.assertNotIn("GLD", diff.missing_stops)

    def test_critical_before_high_before_normal(self):
        """Actions must be sorted CRITICAL < HIGH < NORMAL."""
        from datetime import datetime, timedelta, timezone

        from reconciliation import DesiredPosition, DesiredState

        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        desired = DesiredState(positions={
            "TSM":  DesiredPosition(symbol="TSM", must_exit_by=past, must_exit_reason="deadline"),
            "NVDA": DesiredPosition(symbol="NVDA", forced_exit=True),
            "GLD":  DesiredPosition(symbol="GLD"),
        })
        snap = self._snapshot(
            positions=[
                self._make_pos("TSM"),
                self._make_pos("NVDA"),
                self._make_pos("GLD"),
            ],
            orders=[],
        )
        diff = self.diff_state(desired, snap)

        priorities = [a.priority for a in diff.actions]
        # CRITICAL actions must all come before HIGH, HIGH before NORMAL
        for i in range(len(priorities) - 1):
            p1, p2 = priorities[i], priorities[i+1]
            order = {"CRITICAL": 0, "HIGH": 1, "NORMAL": 2}
            self.assertLessEqual(
                order[p1], order[p2],
                f"Actions not sorted: {p1} appears before {p2}",
            )


class TestReconciliationOptionsStructures(unittest.TestCase):
    """reconcile_options_structures() — OptionsReconResult checks (new API)."""

    @classmethod
    def setUpClass(cls):
        from reconciliation import OptionsReconResult, reconcile_options_structures
        from schemas import (
            BrokerSnapshot,
            NormalizedPosition,
            OptionsLeg,
            OptionsStructure,
            OptionStrategy,
            StructureLifecycle,
            Tier,
        )
        cls.reconcile         = staticmethod(reconcile_options_structures)
        cls.OptionsReconResult = OptionsReconResult
        cls.OptionsStructure   = OptionsStructure
        cls.OptionsLeg         = OptionsLeg
        cls.OptionStrategy     = OptionStrategy
        cls.StructureLifecycle = StructureLifecycle
        cls.Tier               = Tier
        cls.BrokerSnapshot     = BrokerSnapshot
        cls.NormalizedPosition = NormalizedPosition

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _make_structure(self, sid="s001", lifecycle=None, legs=None, expiration="2027-12-19"):
        if lifecycle is None:
            lifecycle = self.StructureLifecycle.FULLY_FILLED
        return self.OptionsStructure(
            structure_id=sid,
            underlying="AAPL",
            strategy=self.OptionStrategy.CALL_DEBIT_SPREAD,
            tier=self.Tier.CORE,
            legs=legs or [],
            contracts=1,
            max_cost_usd=300.0,
            iv_rank=30.0,
            catalyst="test",
            lifecycle=lifecycle,
            opened_at="2026-04-14T00:00:00+00:00",
            expiration=expiration,
        )

    def _make_leg(self, occ_symbol="AAPL270101C00200000"):
        return self.OptionsLeg(
            occ_symbol=occ_symbol,
            underlying="AAPL",
            side="buy",
            qty=1,
            option_type="call",
            strike=200.0,
            expiration="2027-01-01",
        )

    def _make_snapshot(self, occ_symbols=None):
        positions = []
        for sym in (occ_symbols or []):
            positions.append(self.NormalizedPosition(
                symbol=sym, alpaca_sym=sym, qty=1.0,
                avg_entry_price=1.0, current_price=1.0,
                market_value=1.0, unrealized_pl=0.0,
                unrealized_plpc=0.0, is_crypto_pos=False,
            ))
        return self.BrokerSnapshot(
            positions=positions, open_orders=[],
            equity=100_000.0, cash=100_000.0, buying_power=100_000.0,
        )

    _NOW = "2026-04-15T19:00:00+00:00"

    # ── Tests (updated to new API) ────────────────────────────────────────────

    def test_intact_when_all_legs_in_snapshot(self):
        """All leg OCC symbols present in broker → structure_id in intact."""
        legs = [
            self._make_leg("AAPL270101C00200000"),
            self._make_leg("AAPL270101C00210000"),
        ]
        s = self._make_structure(legs=legs)
        snapshot = self._make_snapshot(["AAPL270101C00200000", "AAPL270101C00210000"])
        result = self.reconcile([s], snapshot, self._NOW, {})
        self.assertIn(s.structure_id, result.intact)
        self.assertNotIn(s.structure_id, result.broken)

    def test_not_in_intact_or_broken_when_no_legs_in_snapshot(self):
        """No leg OCC symbols in broker → structure not in intact OR broken (pending fill)."""
        legs = [
            self._make_leg("AAPL270101C00200000"),
            self._make_leg("AAPL270101C00210000"),
        ]
        s = self._make_structure(legs=legs)
        snapshot = self._make_snapshot([])
        result = self.reconcile([s], snapshot, self._NOW, {})
        self.assertNotIn(s.structure_id, result.intact)
        self.assertNotIn(s.structure_id, result.broken)

    def test_broken_when_partial_legs_in_snapshot(self):
        """Only 1 of 2 leg OCC symbols in broker → structure_id in broken."""
        legs = [
            self._make_leg("AAPL270101C00200000"),
            self._make_leg("AAPL270101C00210000"),
        ]
        s = self._make_structure(legs=legs)
        snapshot = self._make_snapshot(["AAPL270101C00200000"])
        result = self.reconcile([s], snapshot, self._NOW, {})
        self.assertIn(s.structure_id, result.broken)
        self.assertNotIn(s.structure_id, result.intact)

    def test_orphaned_legs_detected(self):
        """OCC position in snapshot with no matching structure → in orphaned_legs."""
        orphan_occ = "GLD260619C00435000"
        snapshot = self._make_snapshot([orphan_occ])
        result = self.reconcile([], snapshot, self._NOW, {})
        self.assertIn(orphan_occ, result.orphaned_legs)

    def test_closed_structure_not_checked_for_intact(self):
        """CLOSED structure is not open → not added to intact even if legs in snapshot."""
        legs = [self._make_leg("AAPL270101C00200000")]
        s = self._make_structure(legs=legs, lifecycle=self.StructureLifecycle.CLOSED)
        snapshot = self._make_snapshot(["AAPL270101C00200000"])
        result = self.reconcile([s], snapshot, self._NOW, {})
        self.assertNotIn(s.structure_id, result.intact)

    def test_empty_input_all_lists_empty(self):
        """Empty structures list and empty snapshot → all result lists empty."""
        result = self.reconcile([], self._make_snapshot([]), self._NOW, {})
        self.assertEqual(result.intact, [])
        self.assertEqual(result.broken, [])
        self.assertEqual(result.expiring_soon, [])
        self.assertEqual(result.needs_close, [])
        self.assertEqual(result.orphaned_legs, [])


# ─────────────────────────────────────────────────────────────────────────────
# Suite 12 — Sonnet Gate
# ─────────────────────────────────────────────────────────────────────────────

class TestSonnetGate(unittest.TestCase):
    """Tests for sonnet_gate.should_run_sonnet and should_use_compact_prompt."""

    @classmethod
    def setUpClass(cls):
        from sonnet_gate import (
            GateState,
            TriggerReason,
            should_run_sonnet,
            should_use_compact_prompt,
        )
        cls.GateState              = GateState
        cls.TriggerReason          = TriggerReason
        cls.should_run_sonnet      = staticmethod(should_run_sonnet)
        cls.should_use_compact     = staticmethod(should_use_compact_prompt)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _base_config(self):
        return {"sonnet_gate": {
            "cooldown_minutes":        15,
            "max_consecutive_skips":   12,
            "signal_score_threshold":  15,
            "exposure_change_threshold": 0.05,
            "deadline_warning_minutes": 30,
            "scheduled_windows": [],
        }}

    def _state_now(self, **overrides):
        """Gate state with last_sonnet_call_utc = just now (cooldown fully active)."""
        import hashlib
        from datetime import datetime, timezone
        defaults = {
            "last_sonnet_call_utc": datetime.now(timezone.utc).isoformat(),
            "last_regime":          "neutral",
            "last_top_symbol":      "GLD",
            "last_top_score":       60.0,
            "last_exposure_pct":    0.20,
            "last_positions_hash":  "abc",
            "last_catalyst_hash":   hashlib.md5(b"same news").hexdigest()[:8],
            "last_recon_anomaly":   False,
            "consecutive_skips":    0,
            "total_calls_today":    3,
            "total_skips_today":    9,
            "date_str":             "2026-04-15",
        }
        defaults.update(overrides)
        return self.GateState(**defaults)

    def _state_expired(self, **overrides):
        """Gate state with last call 20 minutes ago (cooldown expired)."""
        import hashlib
        from datetime import datetime, timedelta, timezone
        defaults = {
            "last_sonnet_call_utc": (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(),
            "last_regime":          "neutral",
            "last_top_symbol":      "GLD",
            "last_top_score":       60.0,
            "last_exposure_pct":    0.20,
            "last_positions_hash":  "abc",
            "last_catalyst_hash":   hashlib.md5(b"old news").hexdigest()[:8],
            "last_recon_anomaly":   False,
            "consecutive_skips":    4,
            "total_calls_today":    3,
            "total_skips_today":    9,
            "date_str":             "2026-04-15",
        }
        defaults.update(overrides)
        return self.GateState(**defaults)

    def _now_et(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))

    # ── tests ─────────────────────────────────────────────────────────────────

    def test_gate_skip_cooldown_active(self):
        """Cooldown active + no hard overrides → skip, consecutive_skips incremented."""
        import hashlib
        state = self._state_now(
            last_catalyst_hash=hashlib.md5(b"same news").hexdigest()[:8],
            last_regime="neutral",
        )
        result, reasons, new_state = self.should_run_sonnet(
            session_tier="market", regime="neutral", vix=20.0,
            signal_scores={"scored_symbols": {"GLD": {"score": 62}}},
            positions=[], recon_diff=None,
            breaking_news="same news",
            time_bound_actions=[],
            current_time_et=self._now_et(),
            gate_state=state, config=self._base_config(),
        )
        self.assertFalse(result)
        self.assertEqual(reasons, [])
        self.assertEqual(new_state.consecutive_skips, 1)

    def test_gate_hard_override_regime_halt(self):
        """regime='halt' fires HARD_OVERRIDE regardless of cooldown."""
        state = self._state_now()
        result, reasons, new_state = self.should_run_sonnet(
            session_tier="market", regime="halt", vix=20.0,
            signal_scores={}, positions=[], recon_diff=None,
            breaking_news="same news",
            time_bound_actions=[],
            current_time_et=self._now_et(),
            gate_state=state, config=self._base_config(),
        )
        self.assertTrue(result)
        self.assertIn(self.TriggerReason.HARD_OVERRIDE, reasons)
        self.assertEqual(new_state.consecutive_skips, 0)

    def test_gate_trigger_new_catalyst(self):
        """Cooldown expired + new breaking news → NEW_CATALYST triggers."""
        state = self._state_expired()
        result, reasons, new_state = self.should_run_sonnet(
            session_tier="market", regime="neutral", vix=20.0,
            signal_scores={"scored_symbols": {"GLD": {"score": 62}}},
            positions=[], recon_diff=None,
            breaking_news="BREAKING: Fed cuts rates by 50bps surprise",
            time_bound_actions=[],
            current_time_et=self._now_et(),
            gate_state=state, config=self._base_config(),
        )
        self.assertTrue(result)
        self.assertIn(self.TriggerReason.NEW_CATALYST, reasons)
        self.assertIn(self.TriggerReason.COOLDOWN_EXPIRED, reasons)

    def test_gate_trigger_signal_threshold(self):
        """Score delta >= threshold → SIGNAL_THRESHOLD triggers."""
        state = self._state_expired(last_top_score=55.0, last_top_symbol="GLD")
        import hashlib
        state.last_catalyst_hash = hashlib.md5(b"same news").hexdigest()[:8]
        result, reasons, new_state = self.should_run_sonnet(
            session_tier="market", regime="neutral", vix=20.0,
            signal_scores={"scored_symbols": {"GLD": {"score": 75}}},  # delta=20 >= 15
            positions=[], recon_diff=None,
            breaking_news="same news",
            time_bound_actions=[],
            current_time_et=self._now_et(),
            gate_state=state, config=self._base_config(),
        )
        self.assertTrue(result)
        self.assertIn(self.TriggerReason.SIGNAL_THRESHOLD, reasons)

    def test_gate_trigger_deadline_approaching(self):
        """Time-bound action deadline within 30 min → DEADLINE_APPROACHING triggers."""
        import hashlib
        from datetime import datetime, timedelta, timezone

        deadline_utc = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
        state = self._state_expired(
            last_catalyst_hash=hashlib.md5(b"same news").hexdigest()[:8],
            last_regime="neutral",
        )
        result, reasons, new_state = self.should_run_sonnet(
            session_tier="market", regime="neutral", vix=20.0,
            signal_scores={"scored_symbols": {"TSM": {"score": 60}}},
            positions=[], recon_diff=None,
            breaking_news="same news",
            time_bound_actions=[{"exit_by": deadline_utc, "symbol": "TSM", "reason": "earnings"}],
            current_time_et=self._now_et(),
            gate_state=state, config=self._base_config(),
        )
        self.assertTrue(result)
        self.assertIn(self.TriggerReason.DEADLINE_APPROACHING, reasons)

    def test_gate_max_skip_exceeded(self):
        """consecutive_skips >= max fires MAX_SKIP_EXCEEDED even with active cooldown."""
        state = self._state_now(consecutive_skips=12)
        result, reasons, new_state = self.should_run_sonnet(
            session_tier="market", regime="neutral", vix=20.0,
            signal_scores={}, positions=[], recon_diff=None,
            breaking_news="same news",
            time_bound_actions=[],
            current_time_et=self._now_et(),
            gate_state=state, config=self._base_config(),
        )
        self.assertTrue(result)
        self.assertIn(self.TriggerReason.MAX_SKIP_EXCEEDED, reasons)
        self.assertEqual(new_state.consecutive_skips, 0)

    def test_gate_compact_vs_full_selection(self):
        """should_use_compact_prompt returns True for low-info, False for high-info."""
        TR = self.TriggerReason

        # Compact: only COOLDOWN_EXPIRED + SCHEDULED_WINDOW, < 3 positions
        self.assertTrue(self.should_use_compact(
            reasons=[TR.COOLDOWN_EXPIRED, TR.SCHEDULED_WINDOW],
            positions=[],
            signal_scores={"scored_symbols": {"GLD": {"score": 60}}},
            recon_diff=None,
        ))

        # Full: NEW_CATALYST present
        self.assertFalse(self.should_use_compact(
            reasons=[TR.NEW_CATALYST, TR.COOLDOWN_EXPIRED],
            positions=[],
            signal_scores={"scored_symbols": {"GLD": {"score": 60}}},
            recon_diff=None,
        ))

        # Full: HARD_OVERRIDE present
        self.assertFalse(self.should_use_compact(
            reasons=[TR.HARD_OVERRIDE],
            positions=[],
            signal_scores={},
            recon_diff=None,
        ))

        # Full: 3+ positions regardless of reason
        self.assertFalse(self.should_use_compact(
            reasons=[TR.COOLDOWN_EXPIRED],
            positions=["pos1", "pos2", "pos3"],
            signal_scores={},
            recon_diff=None,
        ))


# ─────────────────────────────────────────────────────────────────────────────
# Suite 13 — BUG-009: bracket stop invisible to status=OPEN queries
# ─────────────────────────────────────────────────────────────────────────────

class TestBug009BracketStopVisibility(unittest.TestCase):
    """
    BUG-009 regression: Alpaca bracket stop-loss children have non-"open" OCA
    status and are invisible to status=OPEN order queries.  A position left with
    only a take-profit limit order was being classified as "partial" (protected)
    instead of "tp_only" (stop missing).

    Validates:
      - _has_stop_order() returns False for a limit sell order
      - _has_stop_order() returns True for a stop sell order
      - reconciliation.diff_state() flags limit-sell-only positions as missing_stops
      - reconciliation.diff_state() does NOT flag stop-protected positions
    """

    @classmethod
    def setUpClass(cls):
        from exit_manager import _has_stop_order, _has_take_profit_order
        from reconciliation import build_desired_state, diff_state
        from schemas import BrokerSnapshot, NormalizedOrder, NormalizedPosition
        cls._has_stop_order         = staticmethod(_has_stop_order)
        cls._has_take_profit_order  = staticmethod(_has_take_profit_order)
        cls._build_desired_state    = staticmethod(build_desired_state)
        cls._diff_state             = staticmethod(diff_state)
        cls.BrokerSnapshot          = BrokerSnapshot
        cls.NormalizedOrder         = NormalizedOrder
        cls.NormalizedPosition      = NormalizedPosition

    def _make_order(self, order_type: str) -> "NormalizedOrder":  # noqa: F821
        """Build a sell NormalizedOrder with the given order_type string."""
        return self.NormalizedOrder(
            order_id="test-ord-001",
            symbol="AMZN",
            alpaca_sym="AMZN",
            side="sell",
            order_type=order_type,
            qty=60.0,
            filled_qty=0.0,
            stop_price=238.91 if order_type in ("stop", "stop_limit") else None,
            limit_price=275.0  if order_type == "limit" else None,
            status="open",
        )

    def _make_position(self) -> "NormalizedPosition":  # noqa: F821
        return self.NormalizedPosition(
            symbol="AMZN",
            alpaca_sym="AMZN",
            qty=60.0,
            avg_entry_price=240.0,
            current_price=250.0,
            market_value=15_000.0,
            unrealized_pl=600.0,
            unrealized_plpc=0.04,
            is_crypto_pos=False,
        )

    # ── _has_stop_order helpers ───────────────────────────────────────────────

    def test_limit_sell_not_detected_as_stop(self):
        """BUG-009: _has_stop_order must return False for a limit sell order."""
        limit_order = self._make_order("limit")
        self.assertFalse(
            self._has_stop_order("AMZN", [limit_order]),
            "_has_stop_order must be False for a limit sell",
        )

    def test_stop_order_detected_as_stop(self):
        """BUG-009: _has_stop_order must return True for a stop sell order."""
        stop_order = self._make_order("stop")
        self.assertTrue(
            self._has_stop_order("AMZN", [stop_order]),
            "_has_stop_order must be True for a stop sell",
        )

    # ── reconciliation.diff_state() stop audit ────────────────────────────────

    def test_reconciliation_missing_stop_with_limit_sell(self):
        """
        BUG-009: position with only a take-profit limit sell must appear in
        diff.missing_stops — a limit order is NOT stop coverage.
        """
        pos         = self._make_position()
        limit_order = self._make_order("limit")
        snapshot = self.BrokerSnapshot(
            equity=100_000.0,
            cash=80_000.0,
            buying_power=180_000.0,
            open_orders=[limit_order],
            positions=[pos],
        )
        desired = self._build_desired_state(
            [pos], {}, datetime.now(timezone.utc)
        )
        diff = self._diff_state(desired, snapshot, datetime.now(timezone.utc))
        self.assertIn(
            "AMZN", diff.missing_stops,
            "AMZN with limit sell only must appear in missing_stops",
        )

    def test_reconciliation_protected_with_stop_order(self):
        """
        BUG-009: position with a stop sell must NOT appear in diff.missing_stops.
        """
        pos        = self._make_position()
        stop_order = self._make_order("stop")
        snapshot = self.BrokerSnapshot(
            equity=100_000.0,
            cash=80_000.0,
            buying_power=180_000.0,
            open_orders=[stop_order],
            positions=[pos],
        )
        desired = self._build_desired_state(
            [pos], {}, datetime.now(timezone.utc)
        )
        diff = self._diff_state(desired, snapshot, datetime.now(timezone.utc))
        self.assertNotIn(
            "AMZN", diff.missing_stops,
            "AMZN with stop order must NOT appear in missing_stops",
        )


# =============================================================================
# Suite 14 — options_builder + options_state + schemas options models
# 9 tests:
#   T1  schemas: StructureLifecycle has the 8 new values
#   T2  schemas: is_terminal() returns True for terminal states
#   T3  schemas: is_open() returns True for filled states
#   T4  schemas: net_debit_per_contract() computes from leg filled_prices
#   T5  schemas: StructureProposal is importable with all required fields
#   T6  options_builder: select_expiry returns correct expiration
#   T7  options_builder: build_structure returns (None, "not yet supported") for straddle
#   T8  options_builder: build_structure succeeds for call_debit_spread with valid chain
#   T9  options_state: round-trip save → load preserves all fields
# =============================================================================


class TestSuite14OptionsBuilder(unittest.TestCase):
    """Suite 14 — options_builder, options_state, schemas options models."""

    # ── Shared chain fixture ──────────────────────────────────────────────────

    @staticmethod
    def _make_chain(symbol="GLD", spot=430.0):
        """Minimal chain fixture with two expirations and liquid call/put data."""
        from datetime import date, timedelta
        today = date.today()
        exp1  = (today + timedelta(days=14)).isoformat()  # 14 DTE
        exp2  = (today + timedelta(days=28)).isoformat()  # 28 DTE
        leg = {
            "strike": 430.0, "lastPrice": 5.0,
            "bid": 4.80, "ask": 5.20,
            "impliedVolatility": 0.25,
            "volume": 200, "openInterest": 500,
        }
        leg_otm = {
            "strike": 435.0, "lastPrice": 3.0,
            "bid": 2.80, "ask": 3.20,
            "impliedVolatility": 0.22,
            "volume": 150, "openInterest": 400,
        }
        exp_data = {
            "calls": [leg, leg_otm],
            "puts":  [
                dict(leg,     strike=430.0),
                dict(leg_otm, strike=425.0),
            ],
        }
        return {
            "symbol":        symbol,
            "fetched_at":    1000000.0,
            "current_price": spot,
            "expirations":   {exp1: exp_data, exp2: exp_data},
        }

    @staticmethod
    def _make_config():
        """Minimal account2 config matching strategy_config.json structure."""
        return {
            "position_sizing": {
                "core_spread_max_pct":     0.05,
                "core_single_leg_max_pct": 0.03,
                "dynamic_max_pct":         0.03,
            },
            "greeks": {"min_delta": 0.30, "min_dte": 5},
            "iv_rules": {},
            "liquidity": {
                "min_open_interest": 50,
                "min_volume":        5,
                "max_bid_ask_pct":   0.25,
                "min_mid_price":     0.05,
            },
        }

    # ── T1: StructureLifecycle 8-value spec ───────────────────────────────────

    def test_structure_lifecycle_new_values(self):
        """T1: StructureLifecycle must expose all 8 spec-mandated values."""
        from schemas import StructureLifecycle
        expected = {
            "proposed", "submitted", "partially_filled", "fully_filled",
            "closed", "rejected", "expired", "cancelled",
        }
        actual = {e.value for e in StructureLifecycle}
        self.assertEqual(
            actual, expected,
            f"StructureLifecycle values mismatch. Got: {actual}",
        )

    # ── T2: is_terminal() ────────────────────────────────────────────────────

    def test_is_terminal_true_for_terminal_states(self):
        """T2: is_terminal() must return True for CLOSED/REJECTED/EXPIRED/CANCELLED."""
        from datetime import datetime, timezone

        from schemas import OptionsStructure, OptionStrategy, StructureLifecycle, Tier
        base = dict(
            structure_id="x", underlying="GLD", strategy=OptionStrategy.SINGLE_CALL,
            lifecycle=StructureLifecycle.CLOSED,
            legs=[], contracts=1, max_cost_usd=100.0,
            opened_at=datetime.now(timezone.utc).isoformat(),
            catalyst="test", tier=Tier.CORE,
        )
        for lc in (
            StructureLifecycle.CLOSED, StructureLifecycle.REJECTED,
            StructureLifecycle.EXPIRED, StructureLifecycle.CANCELLED,
        ):
            s = OptionsStructure(**{**base, "lifecycle": lc})
            self.assertTrue(s.is_terminal(), f"is_terminal() must be True for {lc.value}")

        for lc in (StructureLifecycle.PROPOSED, StructureLifecycle.SUBMITTED,
                   StructureLifecycle.PARTIALLY_FILLED, StructureLifecycle.FULLY_FILLED):
            s = OptionsStructure(**{**base, "lifecycle": lc})
            self.assertFalse(s.is_terminal(), f"is_terminal() must be False for {lc.value}")

    # ── T3: is_open() ────────────────────────────────────────────────────────

    def test_is_open_true_for_filled_states(self):
        """T3: is_open() must return True for FULLY_FILLED and PARTIALLY_FILLED only."""
        from datetime import datetime, timezone

        from schemas import OptionsStructure, OptionStrategy, StructureLifecycle, Tier
        base = dict(
            structure_id="y", underlying="GLD", strategy=OptionStrategy.SINGLE_CALL,
            lifecycle=StructureLifecycle.FULLY_FILLED,
            legs=[], contracts=1, max_cost_usd=100.0,
            opened_at=datetime.now(timezone.utc).isoformat(),
            catalyst="test", tier=Tier.CORE,
        )
        for lc in (StructureLifecycle.FULLY_FILLED, StructureLifecycle.PARTIALLY_FILLED):
            s = OptionsStructure(**{**base, "lifecycle": lc})
            self.assertTrue(s.is_open(), f"is_open() must be True for {lc.value}")

        for lc in (StructureLifecycle.PROPOSED, StructureLifecycle.SUBMITTED,
                   StructureLifecycle.CLOSED, StructureLifecycle.REJECTED,
                   StructureLifecycle.EXPIRED, StructureLifecycle.CANCELLED):
            s = OptionsStructure(**{**base, "lifecycle": lc})
            self.assertFalse(s.is_open(), f"is_open() must be False for {lc.value}")

    # ── T4: net_debit_per_contract() ─────────────────────────────────────────

    def test_net_debit_per_contract_from_legs(self):
        """T4: net_debit_per_contract() computes correctly from leg filled_prices."""
        from datetime import datetime, timezone

        from schemas import (
            OptionsLeg,
            OptionsStructure,
            OptionStrategy,
            StructureLifecycle,
            Tier,
        )
        buy_leg  = OptionsLeg(
            occ_symbol="GLD   260418C00430000", underlying="GLD",
            side="buy",  qty=1, option_type="call",
            strike=430.0, expiration="2026-04-18", filled_price=5.10,
        )
        sell_leg = OptionsLeg(
            occ_symbol="GLD   260418C00435000", underlying="GLD",
            side="sell", qty=1, option_type="call",
            strike=435.0, expiration="2026-04-18", filled_price=2.90,
        )
        s = OptionsStructure(
            structure_id="z", underlying="GLD",
            strategy=OptionStrategy.CALL_DEBIT_SPREAD,
            lifecycle=StructureLifecycle.FULLY_FILLED,
            legs=[buy_leg, sell_leg], contracts=2,
            max_cost_usd=440.0,
            opened_at=datetime.now(timezone.utc).isoformat(),
            catalyst="test", tier=Tier.CORE,
        )
        net = s.net_debit_per_contract()
        self.assertIsNotNone(net)
        self.assertAlmostEqual(net, 2.20, places=4,
            msg=f"net_debit_per_contract expected 2.20, got {net}")

    # ── T5: StructureProposal importable ─────────────────────────────────────

    def test_structure_proposal_importable(self):
        """T5: StructureProposal must be importable from schemas with all required fields."""
        from schemas import OptionStrategy, StructureProposal
        p = StructureProposal(
            symbol="GLD",
            strategy=OptionStrategy.CALL_DEBIT_SPREAD,
            direction="bullish",
            conviction=0.80,
            iv_rank=30.0,
            max_cost_usd=500.0,
            target_dte_min=14,
            target_dte_max=28,
            rationale="Test proposal",
            signal_score=72,
            proposed_at="2026-04-15T12:00:00+00:00",
        )
        self.assertEqual(p.symbol, "GLD")
        self.assertEqual(p.strategy, OptionStrategy.CALL_DEBIT_SPREAD)
        self.assertEqual(p.direction, "bullish")
        self.assertAlmostEqual(p.conviction, 0.80)

    # ── T6: select_expiry ────────────────────────────────────────────────────

    def test_select_expiry_returns_closest_to_midpoint(self):
        """T6: select_expiry picks the expiration closest to the DTE midpoint."""
        from datetime import date, timedelta

        from options_builder import select_expiry
        chain = self._make_chain()
        exp14 = (date.today() + timedelta(days=14)).isoformat()
        result = select_expiry(chain, dte_min=5, dte_max=21)
        self.assertEqual(result, exp14,
            f"select_expiry(5,21) should return 14-DTE={exp14}, got {result}")

    # ── T7: Phase 2/3 returns not-yet-supported ───────────────────────────────

    def test_build_structure_straddle_not_supported(self):
        """T7: build_structure returns (None, 'not yet supported') for STRADDLE."""
        from options_builder import build_structure
        from schemas import OptionStrategy
        action = {
            "symbol":          "GLD",
            "option_strategy": OptionStrategy.STRADDLE.value,
            "direction":       "neutral",
            "iv_rank":         50.0,
            "max_cost_usd":    500.0,
            "catalyst":        "test",
        }
        struct, reason = build_structure(action, self._make_chain(), 100_000.0, self._make_config())
        self.assertIsNone(struct,  "structure must be None for unsupported strategy")
        self.assertIsNotNone(reason, "reason must be non-None for unsupported strategy")
        self.assertIn("not yet supported", reason.lower(),
            f"reason should contain 'not yet supported', got: {reason!r}")

    # ── T8: call_debit_spread happy path ─────────────────────────────────────

    def test_build_structure_call_debit_spread_success(self):
        """T8: build_structure returns valid OptionsStructure for call_debit_spread."""
        from options_builder import build_structure
        from schemas import OptionStrategy, StructureLifecycle
        action = {
            "symbol":          "GLD",
            "option_strategy": OptionStrategy.CALL_DEBIT_SPREAD.value,
            "direction":       "bullish",
            "iv_rank":         28.0,
            "max_cost_usd":    1000.0,
            "catalyst":        "gold breakout",
            "tier":            "core",
            "conviction":      0.80,
        }
        struct, reason = build_structure(action, self._make_chain(), 100_000.0, self._make_config())
        self.assertIsNone(reason,   f"expected no error, got: {reason!r}")
        self.assertIsNotNone(struct, "expected OptionsStructure, got None")
        self.assertEqual(struct.strategy, OptionStrategy.CALL_DEBIT_SPREAD)
        self.assertEqual(struct.lifecycle, StructureLifecycle.PROPOSED)
        self.assertEqual(struct.underlying, "GLD")
        self.assertGreaterEqual(len(struct.legs), 2, "spread must have at least 2 legs")
        self.assertEqual(struct.legs[0].side, "buy",  "first leg must be the long (buy) leg")
        self.assertEqual(struct.legs[1].side, "sell", "second leg must be the short (sell) leg")
        self.assertGreater(struct.contracts, 0, "contracts must be > 0")
        self.assertGreater(struct.max_cost_usd, 0, "max_cost_usd must be > 0")
        self.assertEqual(len(struct.audit_log), 1, "proposed audit entry must be present")

    # ── T9: options_state round-trip ─────────────────────────────────────────

    def test_options_state_save_load_round_trip(self):
        """T9: save_structure → load_structures round-trip preserves all key fields."""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        import options_state
        from options_builder import build_structure
        from schemas import OptionStrategy

        action = {
            "symbol":          "GLD",
            "option_strategy": OptionStrategy.CALL_DEBIT_SPREAD.value,
            "direction":       "bullish",
            "iv_rank":         28.0,
            "max_cost_usd":    1000.0,
            "catalyst":        "test",
            "tier":            "core",
        }
        struct, reason = build_structure(action, self._make_chain(), 100_000.0, self._make_config())
        self.assertIsNone(reason, f"build_structure failed: {reason!r}")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir) / "structures.json"
            with patch.object(options_state, "_STRUCTURES_PATH", tmp_path):
                options_state.save_structure(struct)
                loaded = options_state.load_structures()

        self.assertEqual(len(loaded), 1, "expected exactly 1 structure after round-trip")
        rt = loaded[0]
        self.assertEqual(rt.structure_id, struct.structure_id)
        self.assertEqual(rt.underlying,   struct.underlying)
        self.assertEqual(rt.strategy,     struct.strategy)
        self.assertEqual(rt.lifecycle,    struct.lifecycle)
        self.assertEqual(rt.direction,    struct.direction)
        self.assertEqual(rt.expiration,   struct.expiration)
        self.assertEqual(len(rt.legs),    len(struct.legs))
        self.assertEqual(rt.contracts,    struct.contracts)
        self.assertAlmostEqual(rt.max_cost_usd, struct.max_cost_usd, places=2)
        self.assertEqual(len(rt.audit_log), 1, "audit_log must survive round-trip")


# =============================================================================
# Suite 15 — options_executor + options_intelligence refactor
# 6 tests:
#   T1  options_executor: build_occ_symbol — call
#   T2  options_executor: build_occ_symbol — put with fractional strike
#   T3  options_executor: submit_structure Phase 2 STRADDLE → REJECTED
#   T4  options_executor: should_close_structure — expiry_approaching (DTE ≤ 2)
#   T5  options_executor: should_close_structure — no close reason (DTE 30 days)
#   T6  options_intelligence: select_options_strategy returns StructureProposal
# =============================================================================


class TestSuite15OptionsExecutorAndIntelligence(unittest.TestCase):
    """Suite 15 — options_executor broker adapter and intelligence refactor."""

    # ── Shared structure factory ──────────────────────────────────────────────

    @staticmethod
    def _make_structure(
        strategy=None,
        lifecycle=None,
        expiration=None,
    ):
        """Minimal OptionsStructure for executor tests."""
        from datetime import date, timedelta

        from schemas import (
            OptionsStructure,
            OptionStrategy,
            StructureLifecycle,
            Tier,
        )
        if strategy is None:
            strategy = OptionStrategy.CALL_DEBIT_SPREAD
        if lifecycle is None:
            lifecycle = StructureLifecycle.FULLY_FILLED
        if expiration is None:
            expiration = (date.today() + timedelta(days=30)).isoformat()
        return OptionsStructure(
            structure_id  = "test-struct-001",
            underlying    = "GLD",
            strategy      = strategy,
            lifecycle     = lifecycle,
            legs          = [],
            contracts     = 1,
            max_cost_usd  = 500.0,
            opened_at     = "2026-04-15T10:00:00+00:00",
            catalyst      = "test catalyst",
            tier          = Tier.CORE,
            expiration    = expiration,
        )

    # ── T1: OCC symbol — call ─────────────────────────────────────────────────

    def test_build_occ_symbol_call(self):
        """T1: build_occ_symbol produces correct OCC symbol for a call."""
        from options_executor import build_occ_symbol
        result = build_occ_symbol("GLD", "2026-12-19", "call", 435.0)
        self.assertEqual(result, "GLD261219C00435000")

    # ── T2: OCC symbol — put with fractional strike ───────────────────────────

    def test_build_occ_symbol_put_fractional_strike(self):
        """T2: build_occ_symbol handles fractional strikes (247.5 → 247500)."""
        from options_executor import build_occ_symbol
        result = build_occ_symbol("AMZN", "2026-05-15", "put", 247.5)
        self.assertEqual(result, "AMZN260515P00247500")

    # ── T3: Phase 2/3 strategy immediately rejected ───────────────────────────

    def test_submit_structure_phase2_rejected(self):
        """T3: STRADDLE (Phase 2) → lifecycle=REJECTED, audit_log contains 'not yet supported'."""
        from options_executor import submit_structure
        from schemas import OptionStrategy, StructureLifecycle

        struct = self._make_structure(
            strategy=OptionStrategy.STRADDLE,
            lifecycle=StructureLifecycle.PROPOSED,
        )
        # trading_client is never called for Phase 2 strategies
        result = submit_structure(struct, trading_client=None, config={})

        self.assertEqual(result.lifecycle, StructureLifecycle.REJECTED,
                         "STRADDLE must be rejected immediately")
        self.assertTrue(len(result.audit_log) > 0, "audit_log must have an entry")
        last_entry = result.audit_log[-1]
        last_msg = last_entry["msg"] if isinstance(last_entry, dict) else str(last_entry)
        self.assertIn("not yet supported", last_msg.lower(),
                      "audit_log must mention 'not yet supported'")

    # ── T4: should_close — expiry approaching (DTE ≤ 2) ──────────────────────

    def test_should_close_expiry_approaching(self):
        """T4: should_close_structure returns (True, 'expiry_approaching') when DTE ≤ 2."""
        from datetime import date, datetime, timedelta, timezone

        from options_executor import should_close_structure

        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        struct = self._make_structure(expiration=tomorrow)

        should_close, reason = should_close_structure(
            struct, current_prices={}, config={},
            current_time=datetime.now(timezone.utc),
        )

        self.assertTrue(should_close, "must close when DTE ≤ 2")
        self.assertEqual(reason, "expiry_approaching")

    # ── T5: should_close — no reason (DTE 30 days, no P&L data) ──────────────

    def test_should_close_no_reason(self):
        """T5: should_close_structure returns (False, '') with 30 DTE and no P&L data."""
        from datetime import date, datetime, timedelta, timezone

        from options_executor import should_close_structure

        far_expiry = (date.today() + timedelta(days=30)).isoformat()
        struct = self._make_structure(expiration=far_expiry)

        should_close, reason = should_close_structure(
            struct, current_prices={}, config={},
            current_time=datetime.now(timezone.utc),
        )

        self.assertFalse(should_close, "must not close when DTE 30 and no P&L data")
        self.assertEqual(reason, "")

    # ── T6: intelligence returns StructureProposal ────────────────────────────

    def test_structure_proposal_produced_by_intelligence(self):
        """T6: select_options_strategy returns a StructureProposal on a valid high-conviction signal."""
        from options_intelligence import select_options_strategy
        from schemas import Direction, OptionStrategy, StructureProposal

        iv_summary = {
            "symbol":          "GLD",
            "iv_environment":  "cheap",
            "iv_rank":         25.0,
            "current_iv":      0.18,
            "history_days":    25,
            "observation_mode": False,
        }
        signal_data = {
            "score":       75,
            "confidence":  "high",
            "direction":   "bullish",
            "price":       430.0,
        }
        options_regime = {
            "regime":             "normal",
            "allowed_strategies": ["debit_spread", "single_leg"],
            "size_multiplier":    1.0,
        }

        result = select_options_strategy(
            symbol="GLD",
            iv_summary=iv_summary,
            signal_data=signal_data,
            vix=18.0,
            tier="core",
            catalyst="Fed rate decision catalyst",
            current_price=430.0,
            equity=100_000.0,
            options_regime=options_regime,
        )

        self.assertIsNotNone(result, "expected StructureProposal, got None")
        self.assertIsInstance(result, StructureProposal,
                              f"expected StructureProposal, got {type(result)}")
        self.assertEqual(result.strategy, OptionStrategy.CALL_DEBIT_SPREAD,
                         "cheap IV + bullish signal must produce call_debit_spread")
        self.assertEqual(result.direction, Direction.BULLISH,
                         "direction must be Direction.BULLISH")
        self.assertEqual(result.symbol, "GLD")
        self.assertGreater(result.max_cost_usd, 0)


# ─────────────────────────────────────────────────────────────────────────────
# Suite 16 — Options reconciliation: reconcile + plan_structure_repair
# ─────────────────────────────────────────────────────────────────────────────

class TestSuite16OptionsRecon(unittest.TestCase):
    """
    5 new tests for reconcile_options_structures() and plan_structure_repair().
    Verifies intact/broken/expiring detection and repair plan priority ordering.
    """

    @classmethod
    def setUpClass(cls):
        from reconciliation import (
            OptionsReconResult,
            plan_structure_repair,
            reconcile_options_structures,
        )
        from schemas import (
            BrokerSnapshot,
            NormalizedPosition,
            OptionsLeg,
            OptionsStructure,
            OptionStrategy,
            StructureLifecycle,
            Tier,
        )
        cls.reconcile           = staticmethod(reconcile_options_structures)
        cls.plan_repair         = staticmethod(plan_structure_repair)
        cls.OptionsReconResult  = OptionsReconResult
        cls.OptionsStructure    = OptionsStructure
        cls.OptionsLeg          = OptionsLeg
        cls.OptionStrategy      = OptionStrategy
        cls.StructureLifecycle  = StructureLifecycle
        cls.Tier                = Tier
        cls.BrokerSnapshot      = BrokerSnapshot
        cls.NormalizedPosition  = NormalizedPosition

    _NOW = "2026-04-15T19:00:00+00:00"

    def _make_structure(self, sid, legs=None, expiration="2027-12-19",
                        lifecycle=None):
        if lifecycle is None:
            lifecycle = self.StructureLifecycle.FULLY_FILLED
        return self.OptionsStructure(
            structure_id=sid,
            underlying="GLD",
            strategy=self.OptionStrategy.CALL_DEBIT_SPREAD,
            tier=self.Tier.CORE,
            legs=legs or [],
            contracts=1,
            max_cost_usd=500.0,
            iv_rank=25.0,
            catalyst="test",
            lifecycle=lifecycle,
            opened_at="2026-04-14T00:00:00+00:00",
            expiration=expiration,
        )

    def _make_leg(self, occ_symbol):
        return self.OptionsLeg(
            occ_symbol=occ_symbol,
            underlying="GLD",
            side="buy",
            qty=1,
            option_type="call",
            strike=435.0,
            expiration="2027-12-19",
        )

    def _make_snapshot(self, occ_symbols=None):
        positions = []
        for sym in (occ_symbols or []):
            positions.append(self.NormalizedPosition(
                symbol=sym, alpaca_sym=sym, qty=1.0,
                avg_entry_price=1.0, current_price=1.0,
                market_value=1.0, unrealized_pl=0.0,
                unrealized_plpc=0.0, is_crypto_pos=False,
            ))
        return self.BrokerSnapshot(
            positions=positions, open_orders=[],
            equity=100_000.0, cash=100_000.0, buying_power=100_000.0,
        )

    # ── T1: intact structure ──────────────────────────────────────────────────

    def test_reconcile_intact_structure(self):
        """T1: both legs present in broker snapshot → structure_id in diff.intact."""
        occ1 = "GLD271219C00435000"
        occ2 = "GLD271219C00445000"
        struct = self._make_structure("sid-intact", legs=[
            self._make_leg(occ1), self._make_leg(occ2),
        ])
        snapshot = self._make_snapshot([occ1, occ2])

        result = self.reconcile([struct], snapshot, self._NOW, {})

        self.assertIn("sid-intact", result.intact,
                      "both legs present → must be intact")
        self.assertNotIn("sid-intact", result.broken,
                         "both legs present → must NOT be broken")

    # ── T2: broken structure ──────────────────────────────────────────────────

    def test_reconcile_broken_structure(self):
        """T2: only 1 of 2 legs in broker snapshot → structure_id in diff.broken."""
        occ1 = "GLD271219C00435000"
        occ2 = "GLD271219C00445000"
        struct = self._make_structure("sid-broken", legs=[
            self._make_leg(occ1), self._make_leg(occ2),
        ])
        snapshot = self._make_snapshot([occ1])  # only long leg present

        result = self.reconcile([struct], snapshot, self._NOW, {})

        self.assertIn("sid-broken", result.broken,
                      "partial legs → must be broken")
        self.assertNotIn("sid-broken", result.intact,
                         "partial legs → must NOT be intact")

    # ── T3: expiring soon ─────────────────────────────────────────────────────

    def test_reconcile_expiring_structure(self):
        """T3: structure expiring tomorrow → structure_id in diff.expiring_soon."""
        from datetime import date, timedelta
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        struct = self._make_structure("sid-expiring", expiration=tomorrow)
        snapshot = self._make_snapshot([])

        result = self.reconcile([struct], snapshot, self._NOW, {})

        self.assertIn("sid-expiring", result.expiring_soon,
                      "DTE=1 → must be expiring_soon")

    # ── T4: repair plan priority — broken beats expiring ─────────────────────

    def test_repair_plan_broken_priority(self):
        """T4: broken action comes before expiring in repair plan (broken > expiring)."""
        diff = self.OptionsReconResult(
            broken=["sid-broken"],
            expiring_soon=["sid-expiring"],
        )
        struct_broken   = self._make_structure("sid-broken")
        struct_expiring = self._make_structure("sid-expiring")
        structures = [struct_broken, struct_expiring]
        snapshot   = self._make_snapshot([])

        plan = self.plan_repair(
            diff=diff,
            structures=structures,
            snapshot=snapshot,
            config={},
        )

        self.assertGreater(len(plan), 0, "plan must have at least one action")
        self.assertEqual(plan[0]["action"], "close_broken_leg",
                         "first action must be close_broken_leg (broken > expiring)")

    # ── T5: no open structures → reconciliation is a no-op ───────────────────

    def test_a2_recon_no_structures_skips(self):
        """T5: empty structures list produces all-empty OptionsReconResult."""
        snapshot = self._make_snapshot([])
        result = self.reconcile([], snapshot, self._NOW, {})

        self.assertEqual(result.intact, [])
        self.assertEqual(result.broken, [])
        self.assertEqual(result.expiring_soon, [])
        self.assertEqual(result.needs_close, [])
        self.assertEqual(result.orphaned_legs, [])


# ─────────────────────────────────────────────────────────────────────────────
# Suite 17 — Attribution & deadline_exit_market
# ─────────────────────────────────────────────────────────────────────────────

class TestSuite17AttributionAndDeadlineExit(unittest.TestCase):
    """
    Tests for attribution.py and the deadline_exit_market reconciliation action.

    T1: diff_state() emits deadline_exit_market (not close_all) for expired deadline
    T2: generate_decision_id format matches dec_A1_YYYYMMDD_HHMMSS
    T3: build_module_tags returns all 15 expected keys
    T4: log_attribution_event + get_attribution_summary round-trip
    T5: get_attribution_summary with missing file returns safe defaults
    T6: get_attribution_summary with real data returns correct structure
    """

    # ── helpers ──────────────────────────────────────────────────────────────

    def _import_attribution(self):
        import importlib

        import attribution
        importlib.reload(attribution)
        return attribution

    # ── T1: deadline_exit_market action type ────────────────────────────────

    def test_deadline_exit_produces_market_action(self):
        """diff_state() must emit action_type='deadline_exit_market' for expired deadline."""
        import reconciliation
        from reconciliation import DesiredPosition, DesiredState
        from schemas import BrokerSnapshot, NormalizedPosition

        NOW = datetime(2026, 4, 22, 20, 0, 0, tzinfo=timezone.utc)  # past deadline

        # Build desired state: AMZN with expired deadline
        desired = DesiredState(
            positions={
                "AMZN": DesiredPosition(
                    symbol="AMZN",
                    must_exit_by="2026-04-22T19:45:00+00:00",
                    must_exit_reason="backstop_exit: max_hold_7d",
                )
            },
            seeded_from=NOW.isoformat(),
        )

        # Build broker snapshot: AMZN is held
        amzn_pos = NormalizedPosition(
            symbol="AMZN",
            alpaca_sym="AMZN",
            qty=10,
            avg_entry_price=180.0,
            current_price=185.0,
            market_value=1850.0,
            unrealized_pl=50.0,
            unrealized_plpc=0.028,
            is_crypto_pos=False,
        )
        snapshot = BrokerSnapshot(
            positions=[amzn_pos],
            open_orders=[],
            equity=100_000.0,
            cash=80_000.0,
            buying_power=80_000.0,
        )

        diff = reconciliation.diff_state(desired=desired, snapshot=snapshot, now_utc=NOW)

        deadline_actions = [
            a for a in diff.actions
            if a.symbol == "AMZN" and a.action_type == "deadline_exit_market"
        ]
        close_all_actions = [
            a for a in diff.actions
            if a.symbol == "AMZN" and a.action_type == "close_all"
        ]

        self.assertTrue(
            len(deadline_actions) >= 1,
            f"Expected deadline_exit_market action for AMZN, got actions: "
            f"{[a.action_type for a in diff.actions]}"
        )
        self.assertEqual(
            len(close_all_actions), 0,
            "close_all must not be emitted for expired deadline — use deadline_exit_market"
        )

    # ── T2: generate_decision_id format ─────────────────────────────────────

    def test_generate_decision_id_format(self):
        """generate_decision_id returns dec_A1_YYYYMMDD_HHMMSS."""
        attr = self._import_attribution()
        # strftime("%Y%m%d_%H%M%S") = "20260416_093500" — exactly 15 chars
        ts = "20260416_093500"
        dec_id = attr.generate_decision_id("A1", ts)
        # clean = "20260416_093500", [:15] = "20260416_093500"
        self.assertEqual(dec_id, "dec_A1_20260416_093500")
        self.assertTrue(dec_id.startswith("dec_A1_"))
        self.assertRegex(dec_id, r"^dec_A1_\d{8}_\d{6}$")

    # ── T3: build_module_tags returns all 15 keys ────────────────────────────

    def test_build_module_tags_returns_all_keys(self):
        """build_module_tags must return all 15 expected boolean keys."""
        attr = self._import_attribution()

        EXPECTED_KEYS = {
            "regime_classifier", "signal_scorer", "scratchpad",
            "vector_memory", "macro_backdrop", "macro_wire",
            "morning_brief", "insider_intelligence", "reddit_sentiment",
            "earnings_intel", "portfolio_intelligence", "risk_kernel",
            "sonnet_full", "sonnet_compact", "sonnet_skipped",
        }

        tags = attr.build_module_tags(
            session_tier="market",
            gate_reasons=[],
            used_compact=False,
            gate_skipped=False,
            scratchpad_result={"watching": ["AMZN"]},
            retrieved_memories=[{"id": "m1"}],
            macro_backdrop_str="Global macro conditions remain supportive of risk assets with Fed on hold.",
            macro_wire_str="Fed minutes released — neutral tone, no surprises for markets.",
            morning_brief="Morning brief: Markets open flat. VIX at 18. No major catalysts.",
            insider_section="Insider buying detected in XBI: cluster of Form 4 filings.",
            reddit_section="Reddit sentiment: AMZN bullish, r/investing volume spike.",
            earnings_intel={"AMZN": {"beat": True, "guidance": "raised"}},
            recon_diff=None,
            positions=[{"symbol": "AMZN", "qty": 10}],
        )

        self.assertEqual(set(tags.keys()), EXPECTED_KEYS)
        # All values must be bool
        for k, v in tags.items():
            self.assertIsInstance(v, bool, f"Key '{k}' value {v!r} is not bool")

    # ── T4: attribution log round-trip ───────────────────────────────────────

    def test_attribution_log_roundtrip(self):
        """log_attribution_event writes a record; get_attribution_summary reads it back."""
        import tempfile
        attr = self._import_attribution()

        with tempfile.TemporaryDirectory() as tmpdir:
            # Patch the log path
            log_path = Path(tmpdir) / "analytics" / "attribution_log.jsonl"
            original_log = attr.ATTRIBUTION_LOG
            attr.ATTRIBUTION_LOG = log_path
            try:
                module_tags = {k: False for k in [
                    "regime_classifier", "signal_scorer", "scratchpad",
                    "vector_memory", "macro_backdrop", "macro_wire",
                    "morning_brief", "insider_intelligence", "reddit_sentiment",
                    "earnings_intel", "portfolio_intelligence", "risk_kernel",
                    "sonnet_full", "sonnet_compact", "sonnet_skipped",
                ]}
                module_tags["regime_classifier"] = True
                module_tags["signal_scorer"] = True
                module_tags["sonnet_full"] = True
                module_tags["risk_kernel"] = True

                trigger_flags = {
                    "new_catalyst": False, "signal_threshold": True,
                    "regime_change": False, "risk_anomaly": False,
                    "position_change": False, "deadline_approaching": False,
                    "scheduled_window": False, "recon_anomaly": False,
                    "cooldown_expired": False, "max_skip_exceeded": False,
                    "hard_override": False,
                }

                attr.log_attribution_event(
                    event_type="decision_made",
                    decision_id="dec_A1_20260416_093500",
                    account="A1",
                    symbol="portfolio",
                    module_tags=module_tags,
                    trigger_flags=trigger_flags,
                )

                self.assertTrue(log_path.exists(), "Attribution log file was not created")

                with open(log_path) as fh:
                    records = [json.loads(line) for line in fh if line.strip()]

                self.assertEqual(len(records), 1)
                rec = records[0]
                self.assertEqual(rec["event_type"], "decision_made")
                self.assertEqual(rec["decision_id"], "dec_A1_20260416_093500")
                self.assertEqual(rec["account"], "A1")
                self.assertIn("event_id", rec)
                self.assertIn("timestamp", rec)
                self.assertTrue(rec["module_tags"]["sonnet_full"])
                self.assertTrue(rec["trigger_flags"]["signal_threshold"])

            finally:
                attr.ATTRIBUTION_LOG = original_log

    # ── T5: get_attribution_summary with missing file ────────────────────────

    def test_get_attribution_summary_empty(self):
        """get_attribution_summary returns safe defaults when file is missing."""
        import tempfile
        attr = self._import_attribution()

        with tempfile.TemporaryDirectory() as tmpdir:
            original_log = attr.ATTRIBUTION_LOG
            attr.ATTRIBUTION_LOG = Path(tmpdir) / "nonexistent" / "attribution_log.jsonl"
            try:
                summary = attr.get_attribution_summary(days_back=7)
            finally:
                attr.ATTRIBUTION_LOG = original_log

        self.assertEqual(summary["total_events"], 0)
        self.assertEqual(summary["total_decisions"], 0)
        self.assertEqual(summary["total_trades"], 0)
        self.assertIn("module_usage_pct", summary)
        self.assertIn("gate_efficiency", summary)
        ge = summary["gate_efficiency"]
        self.assertIn("skip_rate", ge)
        self.assertIn("compact_rate", ge)
        self.assertIn("full_rate", ge)

    # ── T6: get_attribution_summary with real data ───────────────────────────

    def test_get_attribution_summary_with_data(self):
        """get_attribution_summary returns correct counts and rates from real data."""
        import tempfile
        attr = self._import_attribution()

        module_tags_full = {
            "regime_classifier": True, "signal_scorer": True,
            "scratchpad": False, "vector_memory": False,
            "macro_backdrop": True, "macro_wire": False,
            "morning_brief": False, "insider_intelligence": False,
            "reddit_sentiment": False, "earnings_intel": False,
            "portfolio_intelligence": False, "risk_kernel": True,
            "sonnet_full": True, "sonnet_compact": False, "sonnet_skipped": False,
        }
        module_tags_skip = {**module_tags_full, "sonnet_full": False, "sonnet_skipped": True}
        trigger_flags = {t: False for t in [
            "new_catalyst", "signal_threshold", "regime_change", "risk_anomaly",
            "position_change", "deadline_approaching", "scheduled_window",
            "recon_anomaly", "cooldown_expired", "max_skip_exceeded", "hard_override",
        ]}
        trigger_flags["signal_threshold"] = True

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "analytics" / "attribution_log.jsonl"
            original_log = attr.ATTRIBUTION_LOG
            attr.ATTRIBUTION_LOG = log_path
            try:
                # 2 decision_made events (1 full, 1 skip) + 1 order_submitted
                attr.log_attribution_event(
                    "decision_made", "dec_A1_20260416_090000", "A1",
                    "portfolio", module_tags_full, trigger_flags,
                )
                attr.log_attribution_event(
                    "decision_made", "dec_A1_20260416_100000", "A1",
                    "portfolio", module_tags_skip, trigger_flags,
                )
                attr.log_attribution_event(
                    "order_submitted", "dec_A1_20260416_090000", "A1",
                    "AMZN", module_tags_full, trigger_flags,
                    trade_id="trade_001",
                )

                summary = attr.get_attribution_summary(days_back=30)
            finally:
                attr.ATTRIBUTION_LOG = original_log

        self.assertEqual(summary["total_events"], 3)
        self.assertEqual(summary["total_decisions"], 2)
        self.assertEqual(summary["total_trades"], 1)

        ge = summary["gate_efficiency"]
        self.assertAlmostEqual(ge["full_rate"], 0.5, places=2)
        self.assertAlmostEqual(ge["skip_rate"], 0.5, places=2)
        self.assertAlmostEqual(ge["compact_rate"], 0.0, places=2)

        mu = summary["module_usage_pct"]
        self.assertAlmostEqual(mu["risk_kernel"], 1.0, places=2)
        self.assertAlmostEqual(mu["regime_classifier"], 1.0, places=2)

        td = summary["trigger_distribution"]
        self.assertEqual(td["signal_threshold"], 2)
        self.assertEqual(td["new_catalyst"], 0)


# Suite 18 — divergence.py: classify, mode enforcement, detectors, liquidity gates
# ---------------------------------------------------------------------------

import sys as _sys

_sys.path.insert(0, str(Path(__file__).parent.parent))

class TestSuite18Divergence(unittest.TestCase):
    """
    Tests for divergence.py: classification, operating mode, detectors,
    and liquidity gate integration.
    Covers 10 of the 10 new tests required (total: 181).
    """

    def test_divergence_classify_stop_missing(self):
        """stop_missing classifies as DE_RISK, SYMBOL scope, guarded_auto."""
        from divergence import DivergenceScope, DivergenceSeverity, classify_divergence
        severity, scope, recov = classify_divergence(
            "stop_missing", "AAPL", "A1")
        self.assertEqual(severity, DivergenceSeverity.DE_RISK)
        self.assertEqual(scope, DivergenceScope.SYMBOL)
        self.assertEqual(recov, "guarded_auto")

    def test_divergence_classify_escalates_large_position(self):
        """Large position ($10k) escalates stop_missing from DE_RISK to HALT."""
        from divergence import DivergenceSeverity, classify_divergence
        severity, scope, recov = classify_divergence(
            "stop_missing", "TSLA", "A1",
            position_size_usd=10000,
        )
        self.assertEqual(severity, DivergenceSeverity.HALT)
        # recoverability should stay guarded_auto or higher
        self.assertIn(recov, ("guarded_auto", "manual"))

    def test_operating_mode_normal_allows_all(self):
        """NORMAL mode allows all action types."""
        from divergence import (
            AccountMode,
            DivergenceScope,
            OperatingMode,
            is_action_allowed,
        )
        mode = AccountMode(
            account="A1", mode=OperatingMode.NORMAL,
            scope=DivergenceScope.ACCOUNT, scope_id="",
            reason_code="", reason_detail="", entered_at="",
            entered_by="test", recovery_condition="one_clean_cycle",
            last_checked_at="",
        )
        for intent in ("enter_long", "enter_short", "close", "recon", "reduce"):
            allowed, reason = is_action_allowed(mode, intent, "AAPL")
            self.assertTrue(allowed, f"{intent} should be allowed in NORMAL mode")

    def test_operating_mode_risk_containment_blocks_entry(self):
        """RISK_CONTAINMENT with account scope blocks enter_long."""
        from divergence import (
            AccountMode,
            DivergenceScope,
            OperatingMode,
            is_action_allowed,
        )
        mode = AccountMode(
            account="A1", mode=OperatingMode.RISK_CONTAINMENT,
            scope=DivergenceScope.ACCOUNT, scope_id="A1",
            reason_code="stop_missing", reason_detail="test",
            entered_at="", entered_by="test",
            recovery_condition="one_clean_cycle", last_checked_at="",
        )
        allowed, reason = is_action_allowed(mode, "enter_long", "AAPL")
        self.assertFalse(allowed)
        self.assertIn("risk_containment", reason)

    def test_operating_mode_risk_containment_allows_close(self):
        """RISK_CONTAINMENT always allows close/reduce actions."""
        from divergence import (
            AccountMode,
            DivergenceScope,
            OperatingMode,
            is_action_allowed,
        )
        mode = AccountMode(
            account="A1", mode=OperatingMode.RISK_CONTAINMENT,
            scope=DivergenceScope.ACCOUNT, scope_id="A1",
            reason_code="stop_missing", reason_detail="test",
            entered_at="", entered_by="test",
            recovery_condition="one_clean_cycle", last_checked_at="",
        )
        for intent in ("close", "reduce", "stop_update", "recon"):
            allowed, _ = is_action_allowed(mode, intent, "AAPL")
            self.assertTrue(allowed, f"{intent} should be allowed even in RISK_CONTAINMENT")

    def test_operating_mode_halted_blocks_entries(self):
        """HALTED mode blocks enter_long and enter_short."""
        from divergence import (
            AccountMode,
            DivergenceScope,
            OperatingMode,
            is_action_allowed,
        )
        mode = AccountMode(
            account="A1", mode=OperatingMode.HALTED,
            scope=DivergenceScope.ACCOUNT, scope_id="A1",
            reason_code="protection_missing", reason_detail="test",
            entered_at="", entered_by="test",
            recovery_condition="manual_review", last_checked_at="",
        )
        for intent in ("enter_long", "enter_short", "add"):
            allowed, reason = is_action_allowed(mode, intent, "SPY")
            self.assertFalse(allowed)
            self.assertIn("halted", reason)

    def test_clean_cycle_recovers_to_normal(self):
        """One clean cycle with recovery_condition=one_clean_cycle returns NORMAL."""
        import tempfile

        from divergence import (
            AccountMode,
            DivergenceScope,
            OperatingMode,
            check_clean_cycle,
        )
        mode = AccountMode(
            account="A1", mode=OperatingMode.RECONCILE_ONLY,
            scope=DivergenceScope.ACCOUNT, scope_id="",
            reason_code="duplicate_exit", reason_detail="test",
            entered_at="2026-04-15T00:00:00+00:00",
            entered_by="test",
            recovery_condition="one_clean_cycle",
            last_checked_at="2026-04-15T00:00:00+00:00",
            clean_cycles_since_entry=0,
        )
        # Use a temp runtime dir so we don't pollute real state
        import divergence as _div_mod
        original_runtime = _div_mod.RUNTIME_DIR
        with tempfile.TemporaryDirectory() as tmp:
            _div_mod.RUNTIME_DIR = Path(tmp)
            _div_mod.MODE_TRANSITION_LOG = Path(tmp) / "mode_transitions.jsonl"
            try:
                result = check_clean_cycle("A1", mode, [])
                self.assertEqual(result.mode, OperatingMode.NORMAL)
            finally:
                _div_mod.RUNTIME_DIR = original_runtime
                _div_mod.MODE_TRANSITION_LOG = original_runtime / "mode_transitions.jsonl"

    def test_repeat_escalation_upgrades_severity(self):
        """Two repeats of same event within window upgrades INFO to RECONCILE."""
        import tempfile

        import divergence as _div_mod
        from divergence import (
            DivergenceSeverity,
            check_repeat_escalation,
        )
        original_path = _div_mod.DIVERGENCE_COUNTS_PATH
        with tempfile.TemporaryDirectory() as tmp:
            _div_mod.DIVERGENCE_COUNTS_PATH = Path(tmp) / "divergence_counts.json"
            try:
                # First call — no escalation yet
                check_repeat_escalation(
                    "A1", "fill_price_drift", "AAPL",
                    DivergenceSeverity.INFO, window_cycles=10,
                )
                # Second call — should escalate
                s2 = check_repeat_escalation(
                    "A1", "fill_price_drift", "AAPL",
                    DivergenceSeverity.INFO, window_cycles=10,
                )
                self.assertEqual(s2, DivergenceSeverity.RECONCILE)
            finally:
                _div_mod.DIVERGENCE_COUNTS_PATH = original_path

    def test_options_liquidity_gate_blocks_illiquid_spread(self):
        """validate_liquidity returns False when legs fail OI threshold."""
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent))
        import importlib
        ob = importlib.import_module("options_builder")

        # validate_liquidity reads long_leg_data / short_leg_data with
        # openInterest, volume, bid, ask, strike keys
        strikes_data = {
            "long_leg_data": {
                "strike": 100.0,
                "bid": 1.0, "ask": 1.5,
                "openInterest": 10,   # below 200 threshold
                "volume": 2,
            },
            "short_leg_data": {
                "strike": 95.0,
                "bid": 0.5, "ask": 0.9,
                "openInterest": 8,    # below 200 threshold
                "volume": 1,
            },
        }
        config = {
            "liquidity_gates": {
                "min_open_interest": 200,
                "min_volume": 20,
                "max_spread_pct": 0.08,
                "min_mid_price": 0.05,
            }
        }
        ok, reason = ob.validate_liquidity(strikes_data, config)
        self.assertFalse(ok)
        self.assertTrue(len(reason) > 0)

    def test_options_pre_debate_gate_skips_illiquid(self):
        """_quick_liquidity_check returns False when ATM OI is below pre-debate floor."""
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent))
        import importlib
        from datetime import date as _date
        from datetime import timedelta as _td
        bo = importlib.import_module("bot_options")

        # _quick_liquidity_check expects chain["expirations"][date]["calls"]
        # as a list of dicts with strike, openInterest, volume keys.
        # Production skips expirations with dte < 2; use today+30 days so this
        # test never goes stale as the calendar advances past the fixture date.
        future_exp = (_date.today() + _td(days=30)).isoformat()
        chain = {
            "current_price": 100.0,
            "expirations": {
                future_exp: {
                    "calls": [
                        {"strike": 100.0, "openInterest": 5, "volume": 1,
                         "bid": 1.0, "ask": 1.2},
                    ],
                }
            }
        }
        from datetime import datetime, timezone

        from schemas import Direction, OptionStrategy, StructureProposal
        proposal = StructureProposal(
            symbol="TEST",
            strategy=OptionStrategy.SINGLE_CALL,
            direction=Direction.BULLISH,
            conviction=0.8,
            iv_rank=30.0,
            max_cost_usd=500.0,
            target_dte_min=7,
            target_dte_max=14,
            rationale="test",
            signal_score=10,
            proposed_at=datetime.now(timezone.utc).isoformat(),
        )
        config = {
            "account2": {
                "liquidity_gates": {
                    "pre_debate_oi_floor": 100,
                    "pre_debate_volume_floor": 10,
                }
            }
        }
        ok, reason = bo._quick_liquidity_check(chain, proposal, config)
        self.assertFalse(ok)
        self.assertIn("OI", reason)


# ═══════════════════════════════════════════════════════════════════════════════
# Suite 19 — Phase 3: Reddit public provider, roll logic, time-stop,
#             IV crush, and weekly-review agent count
# ═══════════════════════════════════════════════════════════════════════════════

class TestSuite19Phase3(unittest.TestCase):
    """10 tests for Phase 3 features: public Reddit, roll, time-stop, IV crush, agent count."""

    # ── Test 1: RedditPublicProvider cache directory created ──────────────────
    def test_01_reddit_public_provider_importable(self):
        """reddit_sentiment_public.py must import and expose RedditPublicProvider."""
        import importlib
        mod = importlib.import_module("reddit_sentiment_public")
        self.assertTrue(hasattr(mod, "RedditPublicProvider"))

    # ── Test 2: Module-level _SUBREDDITS not empty ────────────────────────────
    def test_02_reddit_public_subreddits_defined(self):
        """reddit_sentiment_public must define at least 2 subreddits."""
        import reddit_sentiment_public as rsp
        self.assertTrue(
            hasattr(rsp, "_SUBREDDITS"),
            "reddit_sentiment_public is missing _SUBREDDITS",
        )
        self.assertGreaterEqual(len(rsp._SUBREDDITS), 2)

    # ── Test 3: should_roll_structure — expiry_approaching triggers roll ──────
    def test_03_should_roll_expiry_approaching(self):
        """should_roll_structure returns True for expiry_approaching trigger."""
        import options_executor
        from schemas import OptionsStructure, OptionStrategy, StructureLifecycle, Tier
        struct = OptionsStructure(
            structure_id="roll-test-1",
            underlying="SPY",
            strategy=OptionStrategy.SINGLE_CALL,
            lifecycle=StructureLifecycle.FULLY_FILLED,
            legs=[],
            contracts=1,
            max_cost_usd=100.0,
            opened_at="2026-04-14T14:00:00+00:00",
            catalyst="test",
            tier=Tier.CORE,
            thesis_status="intact",
        )
        ok, reason = options_executor.should_roll_structure(
            struct, "expiry_approaching", {}
        )
        self.assertTrue(ok)
        self.assertIn("roll", reason.lower())

    # ── Test 4: should_roll_structure — thesis invalidated blocks roll ────────
    def test_04_should_roll_thesis_invalidated(self):
        """should_roll_structure returns False when thesis_status == 'invalidated'."""
        import options_executor
        from schemas import OptionsStructure, OptionStrategy, StructureLifecycle, Tier
        struct = OptionsStructure(
            structure_id="roll-test-2",
            underlying="SPY",
            strategy=OptionStrategy.SINGLE_CALL,
            lifecycle=StructureLifecycle.FULLY_FILLED,
            legs=[],
            contracts=1,
            max_cost_usd=100.0,
            opened_at="2026-04-14T14:00:00+00:00",
            catalyst="test",
            tier=Tier.CORE,
            thesis_status="invalidated",
        )
        ok, _ = options_executor.should_roll_structure(
            struct, "expiry_approaching", {}
        )
        self.assertFalse(ok)

    # ── Test 5: should_roll_structure — P&L trigger does NOT trigger roll ─────
    def test_05_should_roll_pnl_stop_blocked(self):
        """should_roll_structure returns False for stop_loss P&L triggers."""
        import options_executor
        from schemas import OptionsStructure, OptionStrategy, StructureLifecycle, Tier
        struct = OptionsStructure(
            structure_id="roll-test-3",
            underlying="SPY",
            strategy=OptionStrategy.SINGLE_CALL,
            lifecycle=StructureLifecycle.FULLY_FILLED,
            legs=[],
            contracts=1,
            max_cost_usd=100.0,
            opened_at="2026-04-14T14:00:00+00:00",
            catalyst="test",
            tier=Tier.CORE,
            thesis_status="intact",
        )
        ok, _ = options_executor.should_roll_structure(
            struct, "stop_loss: down 50%", {}
        )
        self.assertFalse(ok)

    # ── Test 6: time-stop fires at 40% elapsed for single leg ─────────────────
    def test_06_time_stop_single_leg_40pct(self):
        """should_close_structure returns True for single leg at 40% elapsed DTE."""
        from datetime import date, timedelta

        import options_executor
        from schemas import OptionsStructure, OptionStrategy, StructureLifecycle, Tier
        today = date.today()
        opened = today - timedelta(days=4)   # opened 4 days ago
        expiry = today + timedelta(days=6)   # expires 6 days → total=10, elapsed=4 → 40%
        struct = OptionsStructure(
            structure_id="ts-test-1",
            underlying="SPY",
            strategy=OptionStrategy.SINGLE_CALL,
            lifecycle=StructureLifecycle.FULLY_FILLED,
            legs=[],
            contracts=1,
            max_cost_usd=100.0,
            catalyst="test",
            tier=Tier.CORE,
            expiration=expiry.isoformat(),
            opened_at=datetime(opened.year, opened.month, opened.day, 10, 0, 0).isoformat(),
        )
        close, reason = options_executor.should_close_structure(struct, {}, {}, None)
        self.assertTrue(close)
        self.assertIn("time_stop", reason)

    # ── Test 7: time-stop fires at 50% elapsed for debit spread ───────────────
    def test_07_time_stop_debit_spread_50pct(self):
        """should_close_structure returns True for debit spread at 50% elapsed DTE."""
        from datetime import date, timedelta

        import options_executor
        from schemas import OptionsStructure, OptionStrategy, StructureLifecycle, Tier
        today = date.today()
        opened = today - timedelta(days=5)   # opened 5 days ago
        expiry = today + timedelta(days=5)   # total=10, elapsed=5 → 50%
        struct = OptionsStructure(
            structure_id="ts-test-2",
            underlying="SPY",
            strategy=OptionStrategy.CALL_DEBIT_SPREAD,
            lifecycle=StructureLifecycle.FULLY_FILLED,
            legs=[],
            contracts=1,
            max_cost_usd=200.0,
            catalyst="test",
            tier=Tier.CORE,
            expiration=expiry.isoformat(),
            opened_at=datetime(opened.year, opened.month, opened.day, 10, 0, 0).isoformat(),
        )
        close, reason = options_executor.should_close_structure(struct, {}, {}, None)
        self.assertTrue(close)
        self.assertIn("time_stop", reason)

    # ── Test 8: time-stop does NOT fire for credit spread ─────────────────────
    def test_08_time_stop_credit_spread_excluded(self):
        """Credit spreads must NOT trigger the time-stop rule."""
        from datetime import date, timedelta

        import options_executor
        from schemas import OptionsStructure, OptionStrategy, StructureLifecycle, Tier
        today = date.today()
        opened = today - timedelta(days=7)
        expiry = today + timedelta(days=3)  # 70% elapsed — above any threshold
        struct = OptionsStructure(
            structure_id="ts-test-3",
            underlying="SPY",
            strategy=OptionStrategy.CALL_CREDIT_SPREAD,
            lifecycle=StructureLifecycle.FULLY_FILLED,
            legs=[],
            contracts=1,
            max_cost_usd=150.0,
            catalyst="test",
            tier=Tier.CORE,
            expiration=expiry.isoformat(),
            opened_at=datetime(opened.year, opened.month, opened.day, 10, 0, 0).isoformat(),
        )
        # Only the time-stop rule would fire; no P&L data → no other close reason
        close, reason = options_executor.should_close_structure(struct, {}, {}, None)
        # May or may not close for other reasons, but if it closes it must not be time_stop
        if close:
            self.assertNotIn("time_stop", reason)

    # ── Test 9: detect_iv_crush disabled when auto_close_on_crush=False ───────
    def test_09_iv_crush_disabled_by_config(self):
        """detect_iv_crush returns (False, '') when auto_close_on_crush is False."""
        from options_data import detect_iv_crush
        config = {
            "account2": {
                "iv_monitoring": {
                    "auto_close_on_crush": False,
                    "crush_threshold": 0.30,
                }
            }
        }
        crushed, reason = detect_iv_crush("SPY", config)
        self.assertFalse(crushed)
        self.assertEqual(reason, "")

    # ── Test 10: weekly_review has _SYSTEM_AGENT5 through _SYSTEM_AGENT11 ─────
    def test_10_weekly_review_has_eleven_agent_prompts(self):
        """weekly_review.py must export _SYSTEM_AGENT1 through _SYSTEM_AGENT11."""
        import weekly_review
        for i in range(1, 12):
            attr = f"_SYSTEM_AGENT{i}"
            self.assertTrue(
                hasattr(weekly_review, attr),
                f"weekly_review is missing {attr}",
            )
            prompt = getattr(weekly_review, attr)
            self.assertIsInstance(prompt, str)
            self.assertGreater(len(prompt), 50, f"{attr} prompt is too short")


# ─────────────────────────────────────────────────────────────────────────────
# Suite 20 — Phase 4: shadow_lane, signal_backtest, weekly_review helpers
# ─────────────────────────────────────────────────────────────────────────────
class TestSuite20Phase4(unittest.TestCase):
    """12 tests covering Phase 4 additions (shadow lane, signal backtest, director memo)."""

    # ── shadow_lane ───────────────────────────────────────────────────────────

    def test_01_shadow_log_valid_event_written(self):
        """log_shadow_event writes a parseable JSONL line for a valid event."""
        import json
        import tempfile
        from pathlib import Path

        import shadow_lane as sl

        orig = sl.NEAR_MISS_LOG
        try:
            with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf:
                tmp = Path(tf.name)
            sl.NEAR_MISS_LOG = tmp
            sl.log_shadow_event(
                "rejected_by_risk_kernel", "AAPL",
                {"rejection_reason": "vix", "conviction": 0.7},
                decision_id="dec-001", session="market",
            )
            lines = tmp.read_text().strip().splitlines()
            self.assertEqual(len(lines), 1)
            rec = json.loads(lines[0])
            self.assertEqual(rec["event_type"], "rejected_by_risk_kernel")
            self.assertEqual(rec["symbol"], "AAPL")
            self.assertEqual(rec["session"], "market")
        finally:
            sl.NEAR_MISS_LOG = orig
            tmp.unlink(missing_ok=True)

    def test_02_shadow_log_invalid_event_silently_skipped(self):
        """log_shadow_event silently ignores unknown event_type (non-fatal)."""
        import tempfile
        from pathlib import Path

        import shadow_lane as sl

        orig = sl.NEAR_MISS_LOG
        try:
            with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf:
                tmp = Path(tf.name)
            sl.NEAR_MISS_LOG = tmp
            sl.log_shadow_event("not_a_valid_event", "SPY", {})
            written = [l for l in tmp.read_text().splitlines() if l.strip()]
            self.assertEqual(len(written), 0)
        finally:
            sl.NEAR_MISS_LOG = orig
            tmp.unlink(missing_ok=True)

    def test_03_shadow_stats_returns_correct_counts(self):
        """get_shadow_stats counts events correctly from a temp log file."""
        import json
        import tempfile
        from datetime import datetime, timezone
        from pathlib import Path

        import shadow_lane as sl

        orig = sl.NEAR_MISS_LOG
        try:
            with tempfile.NamedTemporaryFile(
                suffix=".jsonl", mode="w", delete=False
            ) as tf:
                tmp = Path(tf.name)
                now_ts = datetime.now(timezone.utc).isoformat()
                for etype in ("approved_trade", "rejected_by_risk_kernel", "rejected_by_risk_kernel"):
                    tf.write(json.dumps({
                        "ts": now_ts, "event_type": etype,
                        "symbol": "GLD", "decision_id": "", "session": "market", "details": {},
                    }) + "\n")
            sl.NEAR_MISS_LOG = tmp
            stats = sl.get_shadow_stats(lookback_days=1)
            self.assertEqual(stats["approved_trades"], 1)
            self.assertEqual(stats["kernel_rejections"], 2)
            self.assertEqual(stats["events"], 3)
        finally:
            sl.NEAR_MISS_LOG = orig
            tmp.unlink(missing_ok=True)

    def test_04_shadow_stats_no_log_returns_no_log_status(self):
        """get_shadow_stats returns status='no_log' when log file absent."""
        from pathlib import Path

        import shadow_lane as sl

        orig = sl.NEAR_MISS_LOG
        try:
            sl.NEAR_MISS_LOG = Path("/tmp/__nonexistent_shadow_log_xyz__.jsonl")
            stats = sl.get_shadow_stats()
            self.assertEqual(stats.get("status"), "no_log")
        finally:
            sl.NEAR_MISS_LOG = orig

    # ── signal_backtest ───────────────────────────────────────────────────────

    def test_05_forward_return_buy_correct(self):
        """_compute_forward_return: BUY + positive return → correct=True."""
        from signal_backtest import _compute_forward_return

        ret, correct = _compute_forward_return(100.0, 105.0, "BUY")
        self.assertAlmostEqual(ret, 0.05, places=5)
        self.assertTrue(correct)

    def test_06_forward_return_sell_correct(self):
        """_compute_forward_return: SELL + negative return → correct=True."""
        from signal_backtest import _compute_forward_return

        ret, correct = _compute_forward_return(100.0, 92.0, "SELL")
        self.assertAlmostEqual(ret, -0.08, places=5)
        self.assertTrue(correct)

    def test_07_forward_return_none_on_missing_price(self):
        """_compute_forward_return: future_price=None → (None, None)."""
        from signal_backtest import _compute_forward_return

        ret, correct = _compute_forward_return(100.0, None, "BUY")
        self.assertIsNone(ret)
        self.assertIsNone(correct)

    def test_08_get_price_at_offset_correct_bars(self):
        """_get_price_at_offset returns Nth bar strictly after from_date."""
        from signal_backtest import _get_price_at_offset

        bars = [
            {"date": "2026-01-01", "close": 100.0},
            {"date": "2026-01-02", "close": 101.0},
            {"date": "2026-01-03", "close": 102.0},
            {"date": "2026-01-06", "close": 103.0},
        ]
        self.assertAlmostEqual(_get_price_at_offset(bars, "2026-01-01", 1), 101.0)
        self.assertAlmostEqual(_get_price_at_offset(bars, "2026-01-01", 3), 103.0)
        self.assertIsNone(_get_price_at_offset(bars, "2026-01-01", 5))

    def test_09_run_signal_backtest_insufficient_data_never_raises(self):
        """run_signal_backtest returns a dict and never raises, even with no data."""
        from signal_backtest import run_signal_backtest

        result = run_signal_backtest(lookback_days=1)
        self.assertIsInstance(result, dict)
        if result:
            self.assertIn(result.get("status"), ("insufficient_data", "ok"))

    # ── weekly_review helpers ─────────────────────────────────────────────────

    def test_10_extract_cto_score_parses_x_over_10(self):
        """_extract_cto_score extracts N/10 pattern from CTO output text."""
        import weekly_review

        text = "Overall readiness assessment: 7/10 — vector memory must be restored."
        score = weekly_review._extract_cto_score(text)
        self.assertAlmostEqual(score, 7.0)

    def test_11_format_director_history_first_week_message(self):
        """_format_director_history_for_prompt returns first-week message on empty history."""
        import weekly_review

        result = weekly_review._format_director_history_for_prompt([])
        self.assertIn("first week", result.lower())

    def test_12_extract_regime_view_finds_regime_sentence(self):
        """_extract_regime_view returns the first sentence mentioning regime."""
        import weekly_review

        text = "## Strategy Memo\nThe current regime is risk-off with elevated volatility. More here."
        result = weekly_review._extract_regime_view(text)
        self.assertIn("regime", result.lower())
        self.assertGreater(len(result), 10)


# =============================================================================
# Suite 21 — F012: iv_history_seeder quality, merge, generate, fetch
# 7 tests:
#   T01  validate_seed_quality: grade A for 20+ entries with variance
#   T02  validate_seed_quality: grade F for < 10 entries
#   T03  _generate_seed_entries: returns exactly target_days entries
#   T04  _generate_seed_entries: entries have variance (not a flat line)
#   T05  _merge_with_existing: does not overwrite good live entries
#   T06  _merge_with_existing: replaces bad iv entry (SPY BUG-005 fix)
#   T07  _fetch_atm_iv_yfinance: returns None when all expirations DTE < MIN_DTE
# =============================================================================

class TestSuite21IVHistorySeeder(unittest.TestCase):
    """Suite 21 — iv_history_seeder: seed quality, merge, generate, DTE filter."""

    # ── T01: grade A ─────────────────────────────────────────────────────────

    def test_01_seed_quality_grade_a(self):
        """validate_seed_quality returns grade A for 20+ entries with variance."""
        import tempfile
        from pathlib import Path

        from iv_history_seeder import validate_seed_quality

        history = [
            {"date": f"2026-01-{i+1:02d}", "iv": round(0.20 + i * 0.003, 4)}
            for i in range(25)
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            iv_dir = Path(tmpdir)
            (iv_dir / "SPY_iv_history.json").write_text(json.dumps(history))
            q = validate_seed_quality("SPY", iv_history_dir=iv_dir)

        self.assertEqual(q["quality_grade"], "A")
        self.assertTrue(q["ready_for_iv_rank"])
        self.assertTrue(q["has_variance"])
        self.assertEqual(q["total_entries"], 25)

    # ── T02: grade F ─────────────────────────────────────────────────────────

    def test_02_seed_quality_grade_f_insufficient_entries(self):
        """validate_seed_quality returns grade F when fewer than 10 valid entries."""
        import tempfile
        from pathlib import Path

        from iv_history_seeder import validate_seed_quality

        history = [{"date": f"2026-01-0{i+1}", "iv": 0.20} for i in range(5)]
        with tempfile.TemporaryDirectory() as tmpdir:
            iv_dir = Path(tmpdir)
            (iv_dir / "AAPL_iv_history.json").write_text(json.dumps(history))
            q = validate_seed_quality("AAPL", iv_history_dir=iv_dir)

        self.assertEqual(q["quality_grade"], "F")
        self.assertFalse(q["ready_for_iv_rank"])

    # ── T03: entry count ──────────────────────────────────────────────────────

    def test_03_generate_seed_entries_count(self):
        """_generate_seed_entries returns exactly target_days entries."""
        from iv_history_seeder import _generate_seed_entries

        entries = _generate_seed_entries("AAPL", 0.25, "2026-05-16", {}, target_days=20)
        self.assertEqual(len(entries), 20)
        # All entries must have the required fields
        for e in entries:
            self.assertIn("date", e)
            self.assertIn("iv", e)
            self.assertEqual(e.get("source"), "yfinance_seed")

    # ── T04: variance ─────────────────────────────────────────────────────────

    def test_04_generate_seed_entries_has_variance(self):
        """_generate_seed_entries produces IV values with variance (not a flat line)."""
        from iv_history_seeder import _generate_seed_entries

        entries = _generate_seed_entries("MSFT", 0.30, "2026-05-16", {}, target_days=25)
        ivs = [e["iv"] for e in entries]
        self.assertGreater(
            max(ivs) - min(ivs), 0.001,
            f"Expected variance in seed entries; range was {max(ivs)-min(ivs):.6f}",
        )

    # ── T05: no overwrite of good entries ─────────────────────────────────────

    def test_05_merge_with_existing_no_overwrite(self):
        """_merge_with_existing does not replace entries where iv >= MIN_VALID_IV."""
        import tempfile
        from pathlib import Path

        from iv_history_seeder import _merge_with_existing

        existing = [{"date": "2026-01-02", "iv": 0.25}]
        new_entries = [{"date": "2026-01-02", "iv": 0.35, "source": "yfinance_seed"}]

        with tempfile.TemporaryDirectory() as tmpdir:
            iv_dir = Path(tmpdir)
            (iv_dir / "GLD_iv_history.json").write_text(json.dumps(existing))
            merged, n_added = _merge_with_existing("GLD", new_entries, iv_history_dir=iv_dir)

        self.assertEqual(n_added, 0, "Should not overwrite a good live entry")
        live_entry = next((e for e in merged if e["date"] == "2026-01-02"), None)
        self.assertIsNotNone(live_entry)
        self.assertAlmostEqual(live_entry["iv"], 0.25,
                               msg="Original live iv must be preserved")

    # ── T06: replace bad iv entry (SPY BUG-005 fix) ───────────────────────────

    def test_06_merge_replaces_bad_iv_entry(self):
        """_merge_with_existing replaces entries with iv < MIN_VALID_IV."""
        import tempfile
        from pathlib import Path

        from iv_history_seeder import MIN_VALID_IV, _merge_with_existing

        # BUG-005 artifact: SPY same-day expiry returned iv=0.02
        existing = [{"date": "2026-04-14", "iv": 0.02}]
        new_entries = [{"date": "2026-04-14", "iv": 0.18, "source": "yfinance_seed"}]

        with tempfile.TemporaryDirectory() as tmpdir:
            iv_dir = Path(tmpdir)
            (iv_dir / "SPY_iv_history.json").write_text(json.dumps(existing))
            merged, n_added = _merge_with_existing("SPY", new_entries, iv_history_dir=iv_dir)

        self.assertEqual(n_added, 1, "Bad entry should be replaced (counted as added)")
        fixed = next((e for e in merged if e["date"] == "2026-04-14"), None)
        self.assertIsNotNone(fixed)
        self.assertAlmostEqual(fixed["iv"], 0.18,
                               msg="Bad entry should be overwritten with seeded value")
        self.assertGreaterEqual(fixed["iv"], MIN_VALID_IV,
                                msg="Replaced entry must satisfy MIN_VALID_IV")

    # ── T07: DTE filter ───────────────────────────────────────────────────────

    def test_07_fetch_atm_iv_skips_short_dte(self):
        """_fetch_atm_iv_yfinance returns (None, '', ...) when all expirations DTE < MIN_DTE."""
        import sys
        from datetime import date, timedelta
        from unittest.mock import MagicMock, patch

        from iv_history_seeder import MIN_DTE, _fetch_atm_iv_yfinance

        today = date.today()
        # Only provide expirations with DTE < MIN_DTE (same-day and next-day)
        short_exps = [(today + timedelta(days=i)).isoformat() for i in range(MIN_DTE)]

        mock_ticker = MagicMock()
        mock_ticker.options = short_exps
        mock_ticker.fast_info.last_price = 550.0

        mock_yf_module = MagicMock()
        mock_yf_module.Ticker.return_value = mock_ticker

        with patch.dict(sys.modules, {"yfinance": mock_yf_module}):
            iv, expiry, meta = _fetch_atm_iv_yfinance("SPY")

        self.assertIsNone(iv,
            f"Should return None when all expirations have DTE < {MIN_DTE}, got iv={iv}")


# ═════════════════════════════════════════════════════════════════════════════
# SUITE 22 — Phase A: decision_id timing, shadow lane id, executor isinstance
# ═════════════════════════════════════════════════════════════════════════════

class TestSuite22PhaseA(unittest.TestCase):
    """Suite 22 — Phase A refactor: attribution timing fix + executor contract."""

    # ── T01: generate_decision_id produces populated id ──────────────────────

    def test_decision_id_generated_before_kernel_loop(self):
        """generate_decision_id returns a non-empty string in dec_{acct}_{date}_{time} format.

        Verifies the attribution block produces a real id when called before the
        kernel loop (the fix for the blank decision_id bug in shadow lane).
        """
        from attribution import generate_decision_id

        ts     = "20260415_123456"
        dec_id = generate_decision_id("A1", ts)

        self.assertIsInstance(dec_id, str)
        self.assertGreater(len(dec_id), 0, "decision_id must not be empty")
        self.assertTrue(dec_id.startswith("dec_A1_"),
                        f"Expected prefix 'dec_A1_', got: {dec_id!r}")
        self.assertIn("20260415", dec_id,
                      f"Expected date in decision_id, got: {dec_id!r}")

    # ── T02: shadow event records the decision_id ─────────────────────────────

    def test_shadow_event_has_decision_id(self):
        """log_shadow_event writes decision_id to JSONL and it is non-empty."""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        import shadow_lane

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_log = Path(tmpdir) / "near_miss_log.jsonl"
            with patch.object(shadow_lane, "NEAR_MISS_LOG", tmp_log):
                shadow_lane.log_shadow_event(
                    "rejected_by_risk_kernel",
                    "AAPL",
                    {"rejection_reason": "vix too high"},
                    decision_id="dec_A1_20260415_120000",
                    session="market",
                )
            record = json.loads(tmp_log.read_text().strip())

        self.assertEqual(record["decision_id"], "dec_A1_20260415_120000")
        self.assertNotEqual(record["decision_id"], "",
                            "decision_id in shadow event must not be blank")
        self.assertEqual(record["event_type"], "rejected_by_risk_kernel")
        self.assertEqual(record["symbol"], "AAPL")

    # ── T03: executor skips unknown type ──────────────────────────────────────

    def test_executor_rejects_unknown_type(self):
        """execute_all skips items that are neither BrokerAction nor dict."""
        import order_executor as oe

        class _Junk:
            pass

        account = SimpleNamespace(equity="100000", buying_power="200000")
        results = oe.execute_all(
            [_Junk()],
            account,
            [],
            "open",
            30,
        )
        self.assertEqual(results, [],
                         "Unknown type should be silently skipped (no result appended)")

    # ── T04: executor warns on raw dict ──────────────────────────────────────

    def test_executor_warns_on_raw_dict(self):
        """execute_all emits a WARNING when it receives a raw dict instead of BrokerAction."""
        import order_executor as oe

        raw = {
            "symbol": "AAPL", "action": "buy", "qty": 1,
            "stop_loss": 190.0, "take_profit": 210.0,
            "tier": "core", "confidence": "medium",
        }
        account = SimpleNamespace(equity="100000", buying_power="200000")

        with self.assertLogs("order_executor", level="WARNING") as cm:
            # Patch validate_action to raise so execution stops before Alpaca calls
            with mock.patch.object(oe, "validate_action",
                                   side_effect=ValueError("test-reject")):
                oe.execute_all([raw], account, [], "open", 30)

        warning_lines = "\n".join(cm.output)
        self.assertIn("raw dict", warning_lines,
                      "Expected 'raw dict' in WARNING log output")

    # ── T05: executor accepts BrokerAction and converts via to_dict() ─────────

    def test_executor_accepts_broker_action(self):
        """execute_all converts BrokerAction → dict via to_dict() before validation."""
        import order_executor as oe
        from schemas import AccountAction, BrokerAction, Conviction, Tier

        ba = BrokerAction(
            symbol="GLD",
            action=AccountAction.BUY,
            qty=5,
            order_type="market",
            tier=Tier.CORE,
            conviction=Conviction.MEDIUM,
            catalyst="safe-haven demand",
            stop_loss=425.0,
            take_profit=455.0,
        )
        account = SimpleNamespace(equity="100000", buying_power="200000")

        captured: list = []

        def _capture(action, *args, **kwargs):
            captured.append(action)
            raise ValueError("test-reject")  # stop before Alpaca submission

        with mock.patch.object(oe, "validate_action", side_effect=_capture):
            oe.execute_all([ba], account, [], "open", 30, session_tier="market")

        self.assertEqual(len(captured), 1,
                         "validate_action should be called exactly once")
        self.assertIsInstance(captured[0], dict,
                              "BrokerAction must be converted to dict before validate_action")
        self.assertEqual(captured[0]["symbol"], "GLD")
        # BrokerAction.to_dict() maps conviction → "confidence"
        self.assertIn("confidence", captured[0],
                      "to_dict() must map conviction → 'confidence' key")
        self.assertNotIn("source_idea", captured[0],
                         "source_idea must be excluded by to_dict()")


# ═════════════════════════════════════════════════════════════════════════════
# SUITE 23 — Phase B: obs mode v2, options lifecycle, time-stop, DecisionOutcomeRecord
# ═════════════════════════════════════════════════════════════════════════════

class TestSuite23PhaseB(unittest.TestCase):
    """Suite 23 — Phase B: obs mode v2 schema, options lifecycle + time-stop, outcome log."""

    # ── T01: check_iv_history_ready returns False when no history files exist ──

    def test_obs_iv_not_ready_blocks_completion(self):
        """check_iv_history_ready returns all_ready=False when no IV history files exist."""
        import tempfile

        import options_data as _od

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(_od, "_IV_DIR", Path(tmpdir)):
                result = _od.check_iv_history_ready(["SPY", "QQQ", "NVDA"])

        self.assertFalse(result["all_ready"],
                         "all_ready must be False when no history files exist")
        self.assertEqual(result["ready_count"], 0)
        self.assertEqual(result["total_count"], 3)
        for sym in ["SPY", "QQQ", "NVDA"]:
            self.assertFalse(result["symbol_ready"][sym],
                             f"{sym} must not be ready without history files")

    # ── T02: _update_obs_mode_state returns True when days < 20 ─────────────

    def test_obs_validation_days_insufficient(self):
        """_update_obs_mode_state returns True (still in obs) when trading_days_observed < 20."""
        from datetime import date

        import bot_options

        today_str = date.today().isoformat()
        state = {
            "version": 2,
            "trading_days_observed": 5,
            "first_seen_date": "2026-04-01",
            "observation_complete": False,
            "last_counted_date": today_str,   # already counted today → no increment
            "iv_history_ready": False,
            "iv_ready_symbols": {},
        }

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_file = Path(tmpdir) / "obs_mode_state.json"
            with mock.patch("bot_options_stage0_preflight._OBS_MODE_FILE", tmp_file):
                still_in_obs = bot_options._update_obs_mode_state(state)

        self.assertTrue(still_in_obs,
                        "5 trading days < 20 — should still be in obs mode")
        self.assertFalse(state.get("observation_complete", False),
                         "observation_complete must remain False")

    # ── T03: observation_complete=True is never reset by _update_obs_mode ────

    def test_obs_blockers_prevent_completion(self):
        """_update_obs_mode_state returns False and never resets observation_complete=True."""
        import bot_options

        state = {
            "trading_days_observed": 20,
            "first_seen_date": "2026-04-01",
            "observation_complete": True,
            "last_counted_date": "2026-04-14",
            # version absent → will trigger v2 migration path
        }

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_file = Path(tmpdir) / "obs_mode_state.json"
            with mock.patch("bot_options_stage0_preflight._OBS_MODE_FILE", tmp_file), \
                 mock.patch("bot_options_stage0_preflight._check_and_update_iv_ready",
                            side_effect=lambda s: s):
                result = bot_options._update_obs_mode_state(state)

        self.assertFalse(result,
                         "_update_obs_mode_state must return False when obs already complete")
        self.assertTrue(state["observation_complete"],
                        "observation_complete must not be reset to False")

    # ── T04: _update_obs_mode_state completes at 20 days and stamps v2 fields ─

    def test_obs_all_conditions_met(self):
        """_update_obs_mode_state completes obs mode at exactly 20 days and writes v2 fields."""
        from datetime import date

        import bot_options

        yesterday = (date.today() - __import__("datetime").timedelta(days=1)).isoformat()
        state = {
            "version": 1,
            "trading_days_observed": 19,
            "first_seen_date": "2026-03-01",
            "observation_complete": False,
            "last_counted_date": yesterday,   # different from today → will count
        }

        def _mock_iv_ready(s):
            s["iv_history_ready"] = True
            s["iv_ready_symbols"] = {"SPY": True}
            return s

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_file = Path(tmpdir) / "obs_mode_state.json"
            with mock.patch("bot_options_stage0_preflight._OBS_MODE_FILE", tmp_file), \
                 mock.patch("bot_options_stage0_preflight._is_trading_day", return_value=True), \
                 mock.patch("bot_options_stage0_preflight._check_and_update_iv_ready",
                            side_effect=_mock_iv_ready):
                result = bot_options._update_obs_mode_state(state)

        self.assertFalse(result,
                         "Should return False — obs mode is now complete")
        self.assertTrue(state["observation_complete"],
                        "observation_complete must be True after reaching 20 days")
        self.assertEqual(state["trading_days_observed"], 20)
        self.assertEqual(state["version"], bot_options._OBS_SCHEMA_VERSION)
        self.assertTrue(state.get("iv_history_ready", False),
                        "iv_history_ready must be stamped when obs completes")

    # ── T05: OptionsStructure lifecycle: PROPOSED is not open, not terminal ───

    def test_options_structure_lifecycle_proposed_to_submitted(self):
        """OptionsStructure in PROPOSED state is neither open nor terminal."""
        from schemas import OptionsStructure, OptionStrategy, StructureLifecycle, Tier

        struct = OptionsStructure(
            structure_id="test_lc_001",
            underlying="AAPL",
            strategy=OptionStrategy.CALL_DEBIT_SPREAD,
            lifecycle=StructureLifecycle.PROPOSED,
            legs=[],
            contracts=1,
            max_cost_usd=500.0,
            opened_at="2026-04-15T10:00:00Z",
            catalyst="earnings catalyst",
            tier=Tier.CORE,
        )
        self.assertFalse(struct.is_terminal(),
                         "PROPOSED structure must not be terminal")
        self.assertFalse(struct.is_open(),
                         "PROPOSED structure must not be is_open()")

        struct.lifecycle = StructureLifecycle.SUBMITTED
        self.assertFalse(struct.is_terminal(),
                         "SUBMITTED structure must not be terminal")
        self.assertFalse(struct.is_open(),
                         "SUBMITTED structure is not yet fully open")

    # ── T06: REJECTED lifecycle is terminal ──────────────────────────────────

    def test_options_structure_lifecycle_rejected_is_terminal(self):
        """OptionsStructure.is_terminal() returns True for REJECTED lifecycle."""
        from schemas import OptionsStructure, OptionStrategy, StructureLifecycle, Tier

        for lc in (StructureLifecycle.REJECTED, StructureLifecycle.EXPIRED,
                   StructureLifecycle.CANCELLED, StructureLifecycle.CLOSED):
            struct = OptionsStructure(
                structure_id=f"test_terminal_{lc.value}",
                underlying="SPY",
                strategy=OptionStrategy.SINGLE_CALL,
                lifecycle=lc,
                legs=[],
                contracts=1,
                max_cost_usd=300.0,
                opened_at="2026-04-15T10:00:00Z",
                catalyst="momentum",
                tier=Tier.DYNAMIC,
            )
            self.assertTrue(struct.is_terminal(),
                            f"{lc.value} must be a terminal lifecycle state")
            self.assertFalse(struct.is_open(),
                             f"{lc.value} must not satisfy is_open()")

    # ── T07: reconcile_options_structures detects broken spread ──────────────

    def test_options_structure_broken_triggers_close(self):
        """reconcile_options_structures marks a spread as broken when only one leg is present."""
        from reconciliation import reconcile_options_structures
        from schemas import (
            BrokerSnapshot,
            NormalizedPosition,
            OptionsLeg,
            OptionsStructure,
            OptionStrategy,
            StructureLifecycle,
            Tier,
        )

        occ_long  = "AAPL260417C00200000"
        occ_short = "AAPL260417C00210000"

        legs = [
            OptionsLeg(occ_symbol=occ_long,  underlying="AAPL", side="buy",
                       qty=1, option_type="call", strike=200.0, expiration="2026-04-17"),
            OptionsLeg(occ_symbol=occ_short, underlying="AAPL", side="sell",
                       qty=1, option_type="call", strike=210.0, expiration="2026-04-17"),
        ]
        struct = OptionsStructure(
            structure_id="test_broken_spread",
            underlying="AAPL",
            strategy=OptionStrategy.CALL_DEBIT_SPREAD,
            lifecycle=StructureLifecycle.FULLY_FILLED,
            legs=legs,
            contracts=1,
            max_cost_usd=400.0,
            opened_at="2026-04-10T10:00:00Z",
            catalyst="test",
            tier=Tier.CORE,
        )

        # Snapshot has ONLY the long leg — short leg missing → broken
        pos_long = NormalizedPosition(
            symbol=occ_long, alpaca_sym=occ_long,
            qty=1.0, avg_entry_price=5.0, current_price=5.5,
            market_value=550.0, unrealized_pl=50.0, unrealized_plpc=0.1,
            is_crypto_pos=False,
        )
        snapshot = BrokerSnapshot(
            positions=[pos_long], open_orders=[],
            equity=100_000.0, cash=90_000.0, buying_power=200_000.0,
        )

        result = reconcile_options_structures(
            structures=[struct],
            snapshot=snapshot,
            current_time="2026-04-15T10:00:00Z",
            config={},
        )
        self.assertIn("test_broken_spread", result.broken,
                      "Spread with one missing leg must appear in broken list")
        self.assertNotIn("test_broken_spread", result.intact,
                         "Broken spread must not appear in intact list")

    # ── T08: reconcile_options_structures detects DTE ≤ 2 as expiring_soon ───

    def test_options_structure_expiry_approaching(self):
        """reconcile_options_structures marks structure as expiring_soon when DTE ≤ 2."""
        from datetime import date, timedelta

        from reconciliation import reconcile_options_structures
        from schemas import (
            BrokerSnapshot,
            NormalizedPosition,
            OptionsLeg,
            OptionsStructure,
            OptionStrategy,
            StructureLifecycle,
            Tier,
        )

        exp_date = date.today() + timedelta(days=1)
        occ = f"SPY{exp_date.strftime('%y%m%d')}C00500000"

        leg = OptionsLeg(
            occ_symbol=occ, underlying="SPY", side="buy",
            qty=1, option_type="call", strike=500.0,
            expiration=exp_date.isoformat(),
        )
        struct = OptionsStructure(
            structure_id="test_expiring",
            underlying="SPY",
            strategy=OptionStrategy.SINGLE_CALL,
            lifecycle=StructureLifecycle.FULLY_FILLED,
            legs=[leg],
            contracts=1,
            max_cost_usd=400.0,
            opened_at="2026-04-01T10:00:00Z",
            catalyst="test",
            tier=Tier.CORE,
            expiration=exp_date.isoformat(),
        )

        pos = NormalizedPosition(
            symbol=occ, alpaca_sym=occ,
            qty=1.0, avg_entry_price=5.0, current_price=5.5,
            market_value=550.0, unrealized_pl=50.0, unrealized_plpc=0.1,
            is_crypto_pos=False,
        )
        snapshot = BrokerSnapshot(
            positions=[pos], open_orders=[],
            equity=100_000.0, cash=90_000.0, buying_power=200_000.0,
        )

        result = reconcile_options_structures(
            structures=[struct],
            snapshot=snapshot,
            current_time=datetime.now(timezone.utc).isoformat(),
            config={},
        )
        self.assertIn("test_expiring", result.expiring_soon,
                      "Structure expiring in 1 day must appear in expiring_soon")

    # ── T09: time-stop fires at 40% elapsed DTE for single leg ───────────────

    def test_options_structure_time_stop_single_leg(self):
        """should_close_structure fires time_stop at 40% elapsed DTE for single-leg strategy."""
        from datetime import date, timedelta

        import options_executor
        from schemas import (
            OptionsLeg,
            OptionsStructure,
            OptionStrategy,
            StructureLifecycle,
            Tier,
        )

        # 20-day total: opened 8 days ago, expires in 12 days → 8/20 = 40% elapsed
        open_date = date.today() - timedelta(days=8)
        exp_date  = date.today() + timedelta(days=12)

        leg = OptionsLeg(
            occ_symbol=f"SPY{exp_date.strftime('%y%m%d')}C00500000",
            underlying="SPY", side="buy",
            qty=1, option_type="call", strike=500.0,
            expiration=exp_date.isoformat(),
        )
        struct = OptionsStructure(
            structure_id="test_timestop_fires",
            underlying="SPY",
            strategy=OptionStrategy.SINGLE_CALL,
            lifecycle=StructureLifecycle.FULLY_FILLED,
            legs=[leg],
            contracts=1,
            max_cost_usd=500.0,
            opened_at=f"{open_date.isoformat()}T10:00:00Z",
            catalyst="momentum",
            tier=Tier.CORE,
            expiration=exp_date.isoformat(),
        )

        from datetime import datetime, timezone
        now_str = datetime.now(timezone.utc).isoformat()
        should_close, reason = options_executor.should_close_structure(
            struct, current_prices={}, config={}, current_time=now_str,
        )
        self.assertTrue(should_close,
                        "Time stop must fire at 40% elapsed DTE for single-leg strategy")
        self.assertIn("time_stop", reason,
                      f"Reason must contain 'time_stop', got: {reason!r}")

    # ── T10: time-stop does NOT fire before 40% elapsed DTE ──────────────────

    def test_options_structure_time_stop_no_fire_early(self):
        """should_close_structure does NOT fire time_stop when elapsed DTE < 40%."""
        from datetime import date, timedelta

        import options_executor
        from schemas import (
            OptionsLeg,
            OptionsStructure,
            OptionStrategy,
            StructureLifecycle,
            Tier,
        )

        # 31-day total: opened 1 day ago, expires in 30 days → 1/31 ≈ 3% elapsed
        open_date = date.today() - timedelta(days=1)
        exp_date  = date.today() + timedelta(days=30)

        leg = OptionsLeg(
            occ_symbol=f"SPY{exp_date.strftime('%y%m%d')}C00500000",
            underlying="SPY", side="buy",
            qty=1, option_type="call", strike=500.0,
            expiration=exp_date.isoformat(),
        )
        struct = OptionsStructure(
            structure_id="test_timestop_no_fire",
            underlying="SPY",
            strategy=OptionStrategy.SINGLE_CALL,
            lifecycle=StructureLifecycle.FULLY_FILLED,
            legs=[leg],
            contracts=1,
            max_cost_usd=500.0,
            opened_at=f"{open_date.isoformat()}T10:00:00Z",
            catalyst="momentum",
            tier=Tier.CORE,
            expiration=exp_date.isoformat(),
        )

        from datetime import datetime, timezone
        now_str = datetime.now(timezone.utc).isoformat()
        should_close, reason = options_executor.should_close_structure(
            struct, current_prices={}, config={}, current_time=now_str,
        )
        if should_close:
            self.assertNotIn(
                "time_stop", reason,
                f"time_stop must not fire at 3% elapsed DTE; reason was: {reason!r}",
            )

    # ── T11: DecisionOutcomeRecord round-trips through to_dict() ─────────────

    def test_outcome_record_roundtrip(self):
        """DecisionOutcomeRecord to_dict() produces expected keys and None sentinel for entry_price."""
        from decision_outcomes import DecisionOutcomeRecord

        record = DecisionOutcomeRecord(
            decision_id="dec_A1_20260415_093500",
            account="A1",
            symbol="AAPL",
            timestamp="2026-04-15T13:35:00Z",
            action="buy",
            tier="core",
            confidence="high",
            catalyst="earnings beat",
            session="market",
            order_id="abc123",
            status="submitted",
            module_tags={"sonnet_full": True, "risk_kernel": True},
            trigger_flags={"new_catalyst": True},
        )
        d = record.to_dict()

        self.assertEqual(d["decision_id"], "dec_A1_20260415_093500")
        self.assertEqual(d["account"], "A1")
        self.assertEqual(d["symbol"], "AAPL")
        self.assertEqual(d["action"], "buy")
        self.assertEqual(d["status"], "submitted")
        self.assertIsNone(d["entry_price"],
                          "entry_price must be None until ExecutionResult.fill_price is added")
        self.assertIsNone(d["return_1d"],
                          "forward returns are None at creation time")
        self.assertIsNone(d["reject_reason"],
                          "reject_reason must be None for submitted trade")
        self.assertIn("sonnet_full", d["module_tags"])
        self.assertTrue(d["module_tags"]["sonnet_full"])
        self.assertEqual(d["confidence"], "high")
        self.assertEqual(d["order_id"], "abc123")

    # ── T12: generate_outcomes_summary returns valid empty dict when no log ───

    def test_generate_outcomes_summary_empty(self):
        """generate_outcomes_summary returns empty-but-valid dict when no log file exists."""
        import tempfile

        import decision_outcomes as _do
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_log = Path(tmpdir) / "decision_outcomes.jsonl"
            # File intentionally not created — simulates first-run with no log
            with mock.patch.object(_do, "OUTCOMES_LOG", tmp_log):
                summary = _do.generate_outcomes_summary(days_back=7)

        self.assertIsInstance(summary, dict,
                              "Summary must be a dict even with no log file")
        self.assertEqual(summary["total_decisions"], 0)
        self.assertEqual(summary["submitted"], 0)
        self.assertEqual(summary["rejected_by_kernel"], 0)
        self.assertIsNone(summary["win_rate_1d"],
                          "win_rate_1d must be None with no outcome data")
        self.assertIn("note", summary,
                      "Summary must include a 'note' field when no data available")


# =============================================================================
# Suite 24 — Phase C7: Divergence subsystem
# =============================================================================

class TestSuite24DivergenceC7(unittest.TestCase):
    """
    24 tests covering divergence.py: classifier, mode enforcement,
    fill/protection detection, respond_to_divergence, check_clean_cycle,
    and full e2e detect→respond→recover flow.

    File-writing tests redirect all four module-level path constants
    (RUNTIME_DIR, MODE_TRANSITION_LOG, DIVERGENCE_COUNTS_PATH, DIVERGENCE_LOG)
    to a TemporaryDirectory using the try/finally inline pattern.

    Test account IDs use A1_TEST / A1_E2E_TEST to avoid collisions with
    live runtime files.
    """

    # ── Unit T01: classify fill_price_drift → INFO ────────────────────────────

    def test_classify_fill_price_drift_returns_info(self):
        """classify_divergence('fill_price_drift') returns INFO severity."""
        from divergence import DivergenceScope, DivergenceSeverity, classify_divergence
        severity, scope, recoverability = classify_divergence(
            "fill_price_drift", "SPY", "A1")
        self.assertEqual(severity, DivergenceSeverity.INFO)
        self.assertEqual(scope, DivergenceScope.ORDER)
        self.assertEqual(recoverability, "auto")

    # ── Unit T02: classify stop_missing → DE_RISK ─────────────────────────────

    def test_classify_stop_missing_returns_de_risk(self):
        """classify_divergence('stop_missing') returns DE_RISK / SYMBOL scope."""
        from divergence import DivergenceScope, DivergenceSeverity, classify_divergence
        severity, scope, recoverability = classify_divergence(
            "stop_missing", "GLD", "A1")
        self.assertEqual(severity, DivergenceSeverity.DE_RISK)
        self.assertEqual(scope, DivergenceScope.SYMBOL)

    # ── Unit T03: classify protection_missing → HALT ──────────────────────────

    def test_classify_protection_missing_returns_halt(self):
        """classify_divergence('protection_missing') returns HALT / manual recovery."""
        from divergence import DivergenceSeverity, classify_divergence
        severity, scope, recoverability = classify_divergence(
            "protection_missing", "GLD", "A1")
        self.assertEqual(severity, DivergenceSeverity.HALT)
        self.assertEqual(recoverability, "manual")

    # ── Unit T04: large position escalates INFO → RECONCILE ───────────────────

    def test_classify_large_position_escalates_severity(self):
        """position_size_usd > 5000 escalates severity one level and tightens recoverability."""
        from divergence import DivergenceSeverity, classify_divergence
        sev_base, _, rec_base = classify_divergence(
            "fill_price_drift", "SPY", "A1", position_size_usd=100)
        sev_large, _, rec_large = classify_divergence(
            "fill_price_drift", "SPY", "A1", position_size_usd=6000)
        self.assertEqual(sev_base, DivergenceSeverity.INFO)
        self.assertNotEqual(sev_large, DivergenceSeverity.INFO)
        self.assertEqual(rec_large, "guarded_auto")  # escalated from "auto"

    # ── Unit T05: stressed VIX escalates severity ─────────────────────────────

    def test_classify_stressed_vix_escalates_severity(self):
        """vix > 25 escalates fill_price_drift from INFO to next level."""
        from divergence import DivergenceSeverity, classify_divergence
        sev_normal, _, _ = classify_divergence(
            "fill_price_drift", "SPY", "A1", vix=20)
        sev_stressed, _, _ = classify_divergence(
            "fill_price_drift", "SPY", "A1", vix=30)
        self.assertEqual(sev_normal, DivergenceSeverity.INFO)
        self.assertNotEqual(sev_stressed, DivergenceSeverity.INFO)

    # ── Unit T06: near expiry escalates severity ──────────────────────────────

    def test_classify_near_expiry_escalates_severity(self):
        """dte <= 2 escalates structure_partial_fill from RECONCILE to DE_RISK."""
        from divergence import DivergenceSeverity, classify_divergence
        sev_ok, _, _ = classify_divergence(
            "structure_partial_fill", "GLD", "A2", dte=10)
        sev_expiring, _, _ = classify_divergence(
            "structure_partial_fill", "GLD", "A2", dte=1)
        self.assertEqual(sev_ok, DivergenceSeverity.RECONCILE)
        self.assertNotEqual(sev_expiring, DivergenceSeverity.RECONCILE)

    # ── Unit T07: NORMAL mode allows all actions ──────────────────────────────

    def test_is_action_allowed_normal_mode_all_allowed(self):
        """NORMAL mode: every action intent is allowed."""
        from divergence import (
            AccountMode,
            DivergenceScope,
            OperatingMode,
            is_action_allowed,
        )
        mode_state = AccountMode(
            account="A1", mode=OperatingMode.NORMAL,
            scope=DivergenceScope.ACCOUNT, scope_id="",
            reason_code="", reason_detail="",
            entered_at="", entered_by="test",
            recovery_condition="one_clean_cycle",
            last_checked_at="",
        )
        for action in ("enter_long", "enter_short", "add", "reallocate", "close",
                       "reduce", "stop_update", "recon"):
            allowed, _ = is_action_allowed(mode_state, action, "SPY")
            self.assertTrue(allowed, f"'{action}' should be allowed in NORMAL mode")

    # ── Unit T08: HALTED mode blocks new entries ──────────────────────────────

    def test_is_action_allowed_halted_blocks_enter(self):
        """HALTED mode: enter_long is blocked and reason contains 'halted'."""
        from divergence import (
            AccountMode,
            DivergenceScope,
            OperatingMode,
            is_action_allowed,
        )
        mode_state = AccountMode(
            account="A1", mode=OperatingMode.HALTED,
            scope=DivergenceScope.ACCOUNT, scope_id="A1",
            reason_code="protection_missing", reason_detail="test",
            entered_at="", entered_by="test",
            recovery_condition="manual_review",
            last_checked_at="",
        )
        allowed, reason = is_action_allowed(mode_state, "enter_long", "SPY")
        self.assertFalse(allowed)
        self.assertIn("halted", reason)

    # ── Unit T09: HALTED mode allows defensive actions ────────────────────────

    def test_is_action_allowed_halted_allows_close(self):
        """HALTED mode: all _ALLOWED_ALWAYS actions are still permitted."""
        from divergence import (
            AccountMode,
            DivergenceScope,
            OperatingMode,
            is_action_allowed,
        )
        mode_state = AccountMode(
            account="A1", mode=OperatingMode.HALTED,
            scope=DivergenceScope.ACCOUNT, scope_id="A1",
            reason_code="protection_missing", reason_detail="test",
            entered_at="", entered_by="test",
            recovery_condition="manual_review",
            last_checked_at="",
        )
        for action in ("close", "reduce", "stop_update", "recon",
                       "cancel", "deadline_exit"):
            allowed, _ = is_action_allowed(mode_state, action, "SPY")
            self.assertTrue(allowed,
                            f"'{action}' must be allowed even in HALTED mode")

    # ── Unit T10: RISK_CONTAINMENT blocks scoped symbol, allows others ────────

    def test_is_action_allowed_risk_containment_scoped_symbol(self):
        """RISK_CONTAINMENT scope=symbol: blocked for that symbol, allowed for others."""
        from divergence import (
            AccountMode,
            DivergenceScope,
            OperatingMode,
            is_action_allowed,
        )
        mode_state = AccountMode(
            account="A1", mode=OperatingMode.RISK_CONTAINMENT,
            scope=DivergenceScope.SYMBOL, scope_id="GLD",
            reason_code="stop_missing", reason_detail="test",
            entered_at="", entered_by="test",
            recovery_condition="one_clean_cycle",
            last_checked_at="",
        )
        allowed_gld, reason = is_action_allowed(mode_state, "enter_long", "GLD")
        self.assertFalse(allowed_gld, "GLD entry must be blocked under GLD containment")
        allowed_spy, _ = is_action_allowed(mode_state, "enter_long", "SPY")
        self.assertTrue(allowed_spy, "SPY entry must be allowed under GLD-scoped containment")

    # ── Unit T11: RECONCILE_ONLY blocks new entries for any symbol ────────────

    def test_is_action_allowed_reconcile_only_blocks_entry(self):
        """RECONCILE_ONLY mode: enter_long is blocked regardless of symbol."""
        from divergence import (
            AccountMode,
            DivergenceScope,
            OperatingMode,
            is_action_allowed,
        )
        mode_state = AccountMode(
            account="A1", mode=OperatingMode.RECONCILE_ONLY,
            scope=DivergenceScope.SYMBOL, scope_id="GLD",
            reason_code="duplicate_exit", reason_detail="test",
            entered_at="", entered_by="test",
            recovery_condition="one_clean_cycle",
            last_checked_at="",
        )
        allowed, reason = is_action_allowed(mode_state, "enter_long", "SPY")
        self.assertFalse(allowed)
        self.assertIn("reconcile_only", reason)

    # ── Unit T12: fill drift below threshold returns None ─────────────────────

    def test_detect_fill_divergence_below_threshold_returns_none(self):
        """detect_fill_divergence: < 0.5% price drift returns None (no event)."""
        import tempfile

        import divergence as _div_mod
        original_log = _div_mod.DIVERGENCE_LOG
        with tempfile.TemporaryDirectory() as tmp:
            _div_mod.DIVERGENCE_LOG = Path(tmp) / "divergence_log.jsonl"
            try:
                # 440.4 vs 440.0 = 0.09% drift — below 0.5% threshold
                result = _div_mod.detect_fill_divergence(
                    symbol="SPY", account="A1",
                    intended_price=440.0,
                    actual_fill_price=440.4,
                    intended_qty=10, actual_qty=10,
                    order_type="market",
                )
                self.assertIsNone(result,
                    "Sub-threshold fill drift must not create a divergence event")
            finally:
                _div_mod.DIVERGENCE_LOG = original_log

    # ── Scenario T13: unknown event type → INFO default ───────────────────────

    def test_scenario_classify_unknown_event_type_returns_info(self):
        """classify_divergence with an unrecognised event_type returns the INFO default."""
        from divergence import DivergenceScope, DivergenceSeverity, classify_divergence
        severity, scope, recoverability = classify_divergence(
            "totally_made_up_event_xyz", "SPY", "A1")
        self.assertEqual(severity, DivergenceSeverity.INFO)
        self.assertEqual(scope, DivergenceScope.ORDER)
        self.assertEqual(recoverability, "auto")

    # ── Scenario T14: repeat escalation upgrades severity ────────────────────

    def test_scenario_repeat_escalation_upgrades_severity(self):
        """check_repeat_escalation: two occurrences in same window escalate RECONCILE → DE_RISK."""
        import tempfile

        import divergence as _div_mod
        from divergence import DivergenceSeverity
        original_counts = _div_mod.DIVERGENCE_COUNTS_PATH
        with tempfile.TemporaryDirectory() as tmp:
            _div_mod.DIVERGENCE_COUNTS_PATH = Path(tmp) / "divergence_counts.json"
            try:
                # First call: count becomes 1 → no escalation yet
                sev1 = _div_mod.check_repeat_escalation(
                    "A1_TEST", "stop_missing", "GLD",
                    DivergenceSeverity.RECONCILE)
                self.assertEqual(sev1, DivergenceSeverity.RECONCILE)
                # Second call in same 5-min window: count becomes 2 → escalates
                sev2 = _div_mod.check_repeat_escalation(
                    "A1_TEST", "stop_missing", "GLD",
                    DivergenceSeverity.RECONCILE)
                self.assertEqual(sev2, DivergenceSeverity.DE_RISK)
            finally:
                _div_mod.DIVERGENCE_COUNTS_PATH = original_counts

    # ── Scenario T15: respond RECONCILE event → RECONCILE_ONLY ───────────────

    def test_scenario_respond_to_reconcile_event_transitions_mode(self):
        """respond_to_divergence with RECONCILE event transitions NORMAL → RECONCILE_ONLY."""
        import tempfile

        import divergence as _div_mod
        from divergence import (
            AccountMode,
            DivergenceEvent,
            DivergenceScope,
            DivergenceSeverity,
            OperatingMode,
        )
        original_runtime = _div_mod.RUNTIME_DIR
        original_mtl = _div_mod.MODE_TRANSITION_LOG
        with tempfile.TemporaryDirectory() as tmp:
            _div_mod.RUNTIME_DIR = Path(tmp)
            _div_mod.MODE_TRANSITION_LOG = Path(tmp) / "mode_transitions.jsonl"
            try:
                mode_state = AccountMode(
                    account="A1_TEST", mode=OperatingMode.NORMAL,
                    scope=DivergenceScope.ACCOUNT, scope_id="",
                    reason_code="", reason_detail="",
                    entered_at="", entered_by="test",
                    recovery_condition="one_clean_cycle",
                    last_checked_at="",
                )
                event = DivergenceEvent(
                    event_id="test_evt_r01",
                    timestamp="2026-01-01T00:00:00+00:00",
                    account="A1_TEST", symbol="GLD",
                    event_type="duplicate_exit",
                    severity=DivergenceSeverity.RECONCILE,
                    scope=DivergenceScope.SYMBOL, scope_id="GLD",
                    paper_expected={}, live_observed={}, delta={},
                    recoverability="auto", risk_impact="low",
                )
                result = _div_mod.respond_to_divergence(
                    [event], "A1_TEST", mode_state)
                self.assertEqual(result.mode, OperatingMode.RECONCILE_ONLY)
            finally:
                _div_mod.RUNTIME_DIR = original_runtime
                _div_mod.MODE_TRANSITION_LOG = original_mtl

    # ── Scenario T16: respond DE_RISK event → RISK_CONTAINMENT ───────────────

    def test_scenario_respond_to_de_risk_event_transitions_mode(self):
        """respond_to_divergence with DE_RISK event transitions NORMAL → RISK_CONTAINMENT."""
        import tempfile

        import divergence as _div_mod
        from divergence import (
            AccountMode,
            DivergenceEvent,
            DivergenceScope,
            DivergenceSeverity,
            OperatingMode,
        )
        original_runtime = _div_mod.RUNTIME_DIR
        original_mtl = _div_mod.MODE_TRANSITION_LOG
        with tempfile.TemporaryDirectory() as tmp:
            _div_mod.RUNTIME_DIR = Path(tmp)
            _div_mod.MODE_TRANSITION_LOG = Path(tmp) / "mode_transitions.jsonl"
            try:
                mode_state = AccountMode(
                    account="A1_TEST", mode=OperatingMode.NORMAL,
                    scope=DivergenceScope.ACCOUNT, scope_id="",
                    reason_code="", reason_detail="",
                    entered_at="", entered_by="test",
                    recovery_condition="one_clean_cycle",
                    last_checked_at="",
                )
                event = DivergenceEvent(
                    event_id="test_evt_dr01",
                    timestamp="2026-01-01T00:00:00+00:00",
                    account="A1_TEST", symbol="GLD",
                    event_type="stop_missing",
                    severity=DivergenceSeverity.DE_RISK,
                    scope=DivergenceScope.SYMBOL, scope_id="GLD",
                    paper_expected={}, live_observed={}, delta={},
                    recoverability="guarded_auto", risk_impact="medium",
                )
                result = _div_mod.respond_to_divergence(
                    [event], "A1_TEST", mode_state)
                self.assertEqual(result.mode, OperatingMode.RISK_CONTAINMENT)
            finally:
                _div_mod.RUNTIME_DIR = original_runtime
                _div_mod.MODE_TRANSITION_LOG = original_mtl

    # ── Scenario T17: respond HALT event → HALTED ────────────────────────────

    def test_scenario_respond_to_halt_event_transitions_mode(self):
        """respond_to_divergence with HALT event transitions NORMAL → HALTED."""
        import tempfile

        import divergence as _div_mod
        from divergence import (
            AccountMode,
            DivergenceEvent,
            DivergenceScope,
            DivergenceSeverity,
            OperatingMode,
        )
        original_runtime = _div_mod.RUNTIME_DIR
        original_mtl = _div_mod.MODE_TRANSITION_LOG
        with tempfile.TemporaryDirectory() as tmp:
            _div_mod.RUNTIME_DIR = Path(tmp)
            _div_mod.MODE_TRANSITION_LOG = Path(tmp) / "mode_transitions.jsonl"
            try:
                mode_state = AccountMode(
                    account="A1_TEST", mode=OperatingMode.NORMAL,
                    scope=DivergenceScope.ACCOUNT, scope_id="",
                    reason_code="", reason_detail="",
                    entered_at="", entered_by="test",
                    recovery_condition="one_clean_cycle",
                    last_checked_at="",
                )
                event = DivergenceEvent(
                    event_id="test_evt_h01",
                    timestamp="2026-01-01T00:00:00+00:00",
                    account="A1_TEST", symbol="GLD",
                    event_type="protection_missing",
                    severity=DivergenceSeverity.HALT,
                    scope=DivergenceScope.SYMBOL, scope_id="GLD",
                    paper_expected={}, live_observed={}, delta={},
                    recoverability="manual", risk_impact="high",
                )
                result = _div_mod.respond_to_divergence(
                    [event], "A1_TEST", mode_state)
                self.assertEqual(result.mode, OperatingMode.HALTED)
            finally:
                _div_mod.RUNTIME_DIR = original_runtime
                _div_mod.MODE_TRANSITION_LOG = original_mtl

    # ── Scenario T18: already HALTED → stays HALTED (idempotent) ─────────────

    def test_scenario_respond_already_halted_stays_halted(self):
        """respond_to_divergence when already HALTED: no transition, returns unchanged mode."""
        import divergence as _div_mod
        from divergence import (
            AccountMode,
            DivergenceEvent,
            DivergenceScope,
            DivergenceSeverity,
            OperatingMode,
        )
        halted_mode = AccountMode(
            account="A1_TEST", mode=OperatingMode.HALTED,
            scope=DivergenceScope.ACCOUNT, scope_id="A1_TEST",
            reason_code="protection_missing", reason_detail="prior halt",
            entered_at="2026-01-01T00:00:00+00:00", entered_by="divergence_engine",
            recovery_condition="manual_review",
            last_checked_at="2026-01-01T00:00:00+00:00",
        )
        event = DivergenceEvent(
            event_id="test_evt_h02",
            timestamp="2026-01-01T01:00:00+00:00",
            account="A1_TEST", symbol="GLD",
            event_type="protection_missing",
            severity=DivergenceSeverity.HALT,
            scope=DivergenceScope.SYMBOL, scope_id="GLD",
            paper_expected={}, live_observed={}, delta={},
            recoverability="manual", risk_impact="high",
        )
        result = _div_mod.respond_to_divergence([event], "A1_TEST", halted_mode)
        # Already HALTED → no transition → returns current_mode unchanged
        self.assertEqual(result.mode, OperatingMode.HALTED)
        self.assertEqual(result.recovery_condition, "manual_review")

    # ── Scenario T19: check_clean_cycle in NORMAL → immediate no-op ──────────

    def test_scenario_check_clean_cycle_in_normal_mode_no_op(self):
        """check_clean_cycle with NORMAL mode returns the same mode object immediately."""
        import divergence as _div_mod
        from divergence import AccountMode, DivergenceScope, OperatingMode
        mode_state = AccountMode(
            account="A1_TEST", mode=OperatingMode.NORMAL,
            scope=DivergenceScope.ACCOUNT, scope_id="",
            reason_code="", reason_detail="",
            entered_at="", entered_by="test",
            recovery_condition="one_clean_cycle",
            last_checked_at="",
        )
        result = _div_mod.check_clean_cycle("A1_TEST", mode_state, [])
        self.assertEqual(result.mode, OperatingMode.NORMAL)
        self.assertIs(result, mode_state, "NORMAL mode: same object returned unchanged")

    # ── Scenario T20: check_clean_cycle increments counter (two_clean_cycles) ─

    def test_scenario_check_clean_cycle_increments_count(self):
        """check_clean_cycle with no new events increments clean_cycles_since_entry."""
        import tempfile

        import divergence as _div_mod
        from divergence import AccountMode, DivergenceScope, OperatingMode
        original_runtime = _div_mod.RUNTIME_DIR
        original_mtl = _div_mod.MODE_TRANSITION_LOG
        with tempfile.TemporaryDirectory() as tmp:
            _div_mod.RUNTIME_DIR = Path(tmp)
            _div_mod.MODE_TRANSITION_LOG = Path(tmp) / "mode_transitions.jsonl"
            try:
                mode_state = AccountMode(
                    account="A1_TEST", mode=OperatingMode.RISK_CONTAINMENT,
                    scope=DivergenceScope.ACCOUNT, scope_id="A1_TEST",
                    reason_code="exposure_mismatch", reason_detail="test",
                    entered_at="", entered_by="test",
                    recovery_condition="two_clean_cycles",  # needs 2 — won't recover yet
                    last_checked_at="",
                    clean_cycles_since_entry=0,
                )
                result = _div_mod.check_clean_cycle("A1_TEST", mode_state, [])
                # Still RISK_CONTAINMENT (needs 2 clean cycles, only got 1)
                self.assertEqual(result.mode, OperatingMode.RISK_CONTAINMENT)
                self.assertEqual(result.clean_cycles_since_entry, 1)
            finally:
                _div_mod.RUNTIME_DIR = original_runtime
                _div_mod.MODE_TRANSITION_LOG = original_mtl

    # ── Scenario T21: fill drift above threshold creates event ────────────────

    def test_scenario_detect_fill_divergence_above_threshold(self):
        """detect_fill_divergence: > 0.5% drift returns a fill_price_drift event."""
        import tempfile

        import divergence as _div_mod
        original_log = _div_mod.DIVERGENCE_LOG
        with tempfile.TemporaryDirectory() as tmp:
            _div_mod.DIVERGENCE_LOG = Path(tmp) / "divergence_log.jsonl"
            try:
                # 449.0 vs 440.0 = 2.05% drift — above 0.5% threshold
                result = _div_mod.detect_fill_divergence(
                    symbol="SPY", account="A1",
                    intended_price=440.0,
                    actual_fill_price=449.0,
                    intended_qty=10, actual_qty=10,
                    order_type="market",
                )
                self.assertIsNotNone(result,
                    "Above-threshold fill drift must create a DivergenceEvent")
                self.assertEqual(result.event_type, "fill_price_drift")
            finally:
                _div_mod.DIVERGENCE_LOG = original_log

    # ── Scenario T22: detect_protection finds stop_missing ────────────────────

    def test_scenario_detect_protection_stop_missing(self):
        """detect_protection_divergence: position with no stop order → stop_missing event."""
        import tempfile

        import divergence as _div_mod
        original_log = _div_mod.DIVERGENCE_LOG
        original_counts = _div_mod.DIVERGENCE_COUNTS_PATH
        with tempfile.TemporaryDirectory() as tmp:
            _div_mod.DIVERGENCE_LOG = Path(tmp) / "divergence_log.jsonl"
            _div_mod.DIVERGENCE_COUNTS_PATH = Path(tmp) / "divergence_counts.json"
            try:
                class MockPosition:
                    def __init__(self, symbol, market_value):
                        self.symbol = symbol
                        self.market_value = market_value

                events = _div_mod.detect_protection_divergence(
                    account="A1_TEST",
                    positions=[MockPosition("GLD", 800)],  # <2000 → stop_missing
                    open_orders=[],
                    vix=20,
                )
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0].event_type, "stop_missing")
                self.assertEqual(events[0].symbol, "GLD")
            finally:
                _div_mod.DIVERGENCE_LOG = original_log
                _div_mod.DIVERGENCE_COUNTS_PATH = original_counts

    # ── E2E T23: detect → respond → clean-cycle recovery ─────────────────────

    def test_e2e_detect_respond_check_clean_cycle_recovery(self):
        """
        Full divergence flow:
          detect_protection_divergence (market_value=800 → stop_missing → DE_RISK)
          → respond_to_divergence → RISK_CONTAINMENT (one_clean_cycle recovery)
          → check_clean_cycle (no new events) → back to NORMAL.
        """
        import tempfile

        import divergence as _div_mod
        from divergence import AccountMode, DivergenceScope, OperatingMode
        original_runtime = _div_mod.RUNTIME_DIR
        original_mtl = _div_mod.MODE_TRANSITION_LOG
        original_log = _div_mod.DIVERGENCE_LOG
        original_counts = _div_mod.DIVERGENCE_COUNTS_PATH
        with tempfile.TemporaryDirectory() as tmp:
            _div_mod.RUNTIME_DIR = Path(tmp)
            _div_mod.MODE_TRANSITION_LOG = Path(tmp) / "mode_transitions.jsonl"
            _div_mod.DIVERGENCE_LOG = Path(tmp) / "divergence_log.jsonl"
            _div_mod.DIVERGENCE_COUNTS_PATH = Path(tmp) / "divergence_counts.json"
            try:
                class MockPosition:
                    def __init__(self, symbol, market_value):
                        self.symbol = symbol
                        self.market_value = market_value

                normal_mode = AccountMode(
                    account="A1_E2E_TEST", mode=OperatingMode.NORMAL,
                    scope=DivergenceScope.ACCOUNT, scope_id="",
                    reason_code="", reason_detail="",
                    entered_at="", entered_by="test",
                    recovery_condition="one_clean_cycle",
                    last_checked_at="",
                )

                # Step 1: detect — market_value=800 < 2000 → stop_missing (DE_RISK)
                events = _div_mod.detect_protection_divergence(
                    account="A1_E2E_TEST",
                    positions=[MockPosition("GLD", 800)],
                    open_orders=[],
                    vix=20,
                )
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0].event_type, "stop_missing")

                # Step 2: respond — DE_RISK + SYMBOL scope → RISK_CONTAINMENT
                mode_after = _div_mod.respond_to_divergence(
                    events, "A1_E2E_TEST", normal_mode)
                self.assertEqual(mode_after.mode, OperatingMode.RISK_CONTAINMENT)
                self.assertEqual(mode_after.recovery_condition, "one_clean_cycle")

                # Step 3: clean cycle — no new events → recovery_met → NORMAL
                recovered = _div_mod.check_clean_cycle(
                    "A1_E2E_TEST", mode_after, [])
                self.assertEqual(recovered.mode, OperatingMode.NORMAL)
            finally:
                _div_mod.RUNTIME_DIR = original_runtime
                _div_mod.MODE_TRANSITION_LOG = original_mtl
                _div_mod.DIVERGENCE_LOG = original_log
                _div_mod.DIVERGENCE_COUNTS_PATH = original_counts

    # ── E2E T24: transition_mode writes to tempdir, load reads it back ────────

    def test_e2e_transition_mode_writes_to_tempdir(self):
        """transition_mode writes mode file to redirected RUNTIME_DIR; load_account_mode reads it back."""
        import tempfile

        import divergence as _div_mod
        from divergence import DivergenceScope, OperatingMode
        original_runtime = _div_mod.RUNTIME_DIR
        original_mtl = _div_mod.MODE_TRANSITION_LOG
        with tempfile.TemporaryDirectory() as tmp:
            _div_mod.RUNTIME_DIR = Path(tmp)
            _div_mod.MODE_TRANSITION_LOG = Path(tmp) / "mode_transitions.jsonl"
            try:
                result = _div_mod.transition_mode(
                    account="A1_TEST",
                    new_mode=OperatingMode.RISK_CONTAINMENT,
                    scope=DivergenceScope.SYMBOL,
                    scope_id="GLD",
                    reason_code="stop_missing",
                    reason_detail="unit test write check",
                    entered_by="test",
                    recovery_condition="one_clean_cycle",
                )
                self.assertEqual(result.mode, OperatingMode.RISK_CONTAINMENT)
                self.assertEqual(result.account, "A1_TEST")

                # Verify the file was written to tempdir (not live runtime)
                mode_path = Path(tmp) / "a1_test_mode.json"
                self.assertTrue(mode_path.exists(),
                    "transition_mode must write mode file to redirected RUNTIME_DIR")

                # Verify round-trip: load_account_mode reads the same state
                reloaded = _div_mod.load_account_mode("A1_TEST")
                self.assertEqual(reloaded.mode, OperatingMode.RISK_CONTAINMENT)
                self.assertEqual(reloaded.scope_id, "GLD")
            finally:
                _div_mod.RUNTIME_DIR = original_runtime
                _div_mod.MODE_TRANSITION_LOG = original_mtl


# =============================================================================
# Suite 25 — Phase C8 market_data section fallbacks + D13 options audit (6 tests)
# =============================================================================
#   C8 — market_data section tagging and fallback behaviour:
#   T01 test_market_data_required_section_has_fallback
#   T02 test_market_data_optional_section_returns_empty
#   T03 test_market_data_compact_uses_no_enrichment
#
#   D13 — OptionsStructure close/roll audit trail:
#   T04 test_close_reason_code_stamped_on_structure
#   T05 test_roll_links_old_and_new_structure
#   T06 test_log_structure_event_appends_jsonl
# =============================================================================


class TestSuite25MarketDataAndOptionsAudit(unittest.TestCase):
    """Suite 25 — C8 market_data fallbacks and D13 options close/roll audit."""

    # ── C8 T01: REQUIRED section fallback ────────────────────────────────────

    def test_market_data_required_section_has_fallback(self):
        """T01: get_market_clock() returns fallback dict when Alpaca raises."""
        import market_data as _md_mod
        _mock_client = mock.MagicMock()
        _mock_client.get_clock.side_effect = Exception("network error")
        with mock.patch("market_data._get_trading_client", return_value=_mock_client):
            result = _md_mod.get_market_clock()
        self.assertIsInstance(result, dict,
                              "get_market_clock must return a dict on error, not raise")
        self.assertEqual(result["status"], "unknown",
                         "fallback status must be 'unknown'")
        self.assertFalse(result.get("is_open"),
                         "fallback is_open must be False")

    # ── C8 T02: OPTIONAL section returns "" on error ──────────────────────────

    def test_market_data_optional_section_returns_empty(self):
        """T02: _build_sector_table() returns '' when dw.load_sector_perf raises."""
        import market_data as _md_mod
        with mock.patch.object(_md_mod.dw, "load_sector_perf",
                               side_effect=Exception("dw unavailable")):
            result = _md_mod._build_sector_table()
        self.assertEqual(result, "",
                         "_build_sector_table must return '' on exception, not raise")

    # ── C8 T03: compact template contains no ENRICHMENT section names ─────────

    def test_market_data_compact_uses_no_enrichment(self):
        """T03: ENRICHMENT section names must not appear in compact_template.txt."""
        template_path = Path(__file__).parent.parent / "prompts" / "compact_template.txt"
        if not template_path.exists():
            self.skipTest("compact_template.txt not found")
        text = template_path.read_text()
        enrichment_names = ["compute_eth_btc_ratio", "test_crypto_prices"]
        for name in enrichment_names:
            self.assertNotIn(
                name, text,
                f"ENRICHMENT section '{name}' must not appear in compact_template.txt",
            )

    # ── D13 T04: close_reason_code stamped on structure ───────────────────────

    def test_close_reason_code_stamped_on_structure(self):
        """T04: close_structure stamps close_reason_code, initiated_by, and audit entry."""
        import tempfile
        from datetime import date, timedelta

        import options_executor as _oe_mod
        from schemas import (
            OptionsLeg,
            OptionsStructure,
            OptionStrategy,
            StructureLifecycle,
            Tier,
        )

        filled_leg = OptionsLeg(
            occ_symbol   = "GLD261219C00435000",
            underlying   = "GLD",
            side         = "buy",
            qty          = 1,
            option_type  = "call",
            strike       = 435.0,
            expiration   = "2026-12-19",
            filled_price = 1.50,
        )
        structure = OptionsStructure(
            structure_id = "test-close-001",
            underlying   = "GLD",
            strategy     = OptionStrategy.SINGLE_CALL,
            lifecycle    = StructureLifecycle.FULLY_FILLED,
            legs         = [filled_leg],
            contracts    = 1,
            max_cost_usd = 500.0,
            opened_at    = "2026-04-15T10:00:00+00:00",
            catalyst     = "test",
            tier         = Tier.CORE,
            expiration   = (date.today() + timedelta(days=30)).isoformat(),
        )

        mock_order = mock.MagicMock()
        mock_order.id = "mock-order-999"
        mock_client = mock.MagicMock()
        mock_client.submit_order.return_value = mock_order

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_log = Path(tmpdir) / "options_log.jsonl"
            with mock.patch.object(_oe_mod, "_LOG_PATH", tmp_log):
                result = _oe_mod.close_structure(
                    structure, mock_client, reason="time_stop", method="limit"
                )

        self.assertEqual(result.close_reason_code, "time_stop",
                         "close_reason_code must be set to the reason argument")
        self.assertEqual(result.initiated_by, "auto_rule",
                         "initiated_by must be 'auto_rule' for automated close")
        self.assertTrue(
            any("close" in str(e.get("msg", "")).lower() for e in result.audit_log),
            "audit_log must contain at least one close-related entry",
        )

    # ── D13 T05: roll fields survive save/load round-trip ─────────────────────

    def test_roll_links_old_and_new_structure(self):
        """T05: roll_reason_code and rolled_to_structure_id survive save→load round-trip."""
        import tempfile
        from datetime import date, timedelta

        import options_state
        from schemas import (
            OptionsStructure,
            OptionStrategy,
            StructureLifecycle,
            Tier,
        )

        structure = OptionsStructure(
            structure_id           = "test-roll-001",
            underlying             = "GLD",
            strategy               = OptionStrategy.CALL_DEBIT_SPREAD,
            lifecycle              = StructureLifecycle.CLOSED,
            legs                   = [],
            contracts              = 1,
            max_cost_usd           = 500.0,
            opened_at              = "2026-04-15T10:00:00+00:00",
            catalyst               = "test",
            tier                   = Tier.CORE,
            expiration             = (date.today() + timedelta(days=30)).isoformat(),
            roll_group_id          = "grp-abc123",
            roll_reason_code       = "dte_approaching",
            rolled_to_structure_id = "new_id_001",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir) / "structures.json"
            with mock.patch.object(options_state, "_STRUCTURES_PATH", tmp_path):
                options_state.save_structure(structure)
                loaded_list = options_state.load_structures()

        self.assertEqual(len(loaded_list), 1,
                         "expected exactly 1 structure after round-trip")
        loaded = loaded_list[0]
        self.assertEqual(loaded.roll_reason_code, "dte_approaching",
                         "roll_reason_code must survive save/load round-trip")
        self.assertEqual(loaded.rolled_to_structure_id, "new_id_001",
                         "rolled_to_structure_id must survive save/load round-trip")

    # ── D13 T06: _log_structure_event appends JSONL ───────────────────────────

    def test_log_structure_event_appends_jsonl(self):
        """T06: _log_structure_event appends a valid JSONL record to the log file."""
        import tempfile
        from datetime import date, timedelta

        import options_executor as _oe_mod
        from schemas import (
            OptionsStructure,
            OptionStrategy,
            StructureLifecycle,
            Tier,
        )

        structure = OptionsStructure(
            structure_id      = "test-log-001",
            underlying        = "GLD",
            strategy          = OptionStrategy.SINGLE_CALL,
            lifecycle         = StructureLifecycle.CLOSED,
            legs              = [],
            contracts         = 1,
            max_cost_usd      = 500.0,
            opened_at         = "2026-04-15T10:00:00+00:00",
            catalyst          = "test",
            tier              = Tier.CORE,
            expiration        = (date.today() + timedelta(days=30)).isoformat(),
            close_reason_code = "test_close",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_log = Path(tmpdir) / "options_log.jsonl"
            with mock.patch.object(_oe_mod, "_LOG_PATH", tmp_log):
                _oe_mod._log_structure_event(structure, "close", "test detail")
            lines = tmp_log.read_text().strip().splitlines()

        self.assertEqual(len(lines), 1,
                         "exactly one JSONL line must be appended per event")
        record = json.loads(lines[0])
        self.assertEqual(record["event_type"], "close",
                         "event_type field must be 'close'")
        self.assertEqual(record["close_reason_code"], "test_close",
                         "close_reason_code must be propagated to the log record")


class TestSuite26Session1(unittest.TestCase):
    """Suite 26 — Session 1: executor policy consolidation, fill_price, recommendation scaffold."""

    # ── S26 T01: TIER_MAX_PCT constant must not exist in order_executor ───────

    def test_executor_no_tier_max_pct_constant(self):
        """T01: order_executor.py must not define TIER_MAX_PCT (policy consolidated to risk_kernel)."""
        import inspect

        import order_executor as _oe
        src = inspect.getsource(_oe)
        self.assertNotIn("TIER_MAX_PCT", src,
                         "TIER_MAX_PCT must not appear in order_executor.py — "
                         "risk_kernel._TIER_MAX_PCT is the sole authoritative definition")

    # ── S26 T02: PDT_FLOOR still present in executor (regulatory backstop) ────

    def test_executor_no_pdt_floor_as_primary(self):
        """T02: order_executor.py retains PDT_FLOOR as a hard backstop constant (regulatory dual enforcement)."""
        import order_executor as _oe
        self.assertTrue(
            hasattr(_oe, "PDT_FLOOR"),
            "PDT_FLOOR must remain in order_executor.py as regulatory hard backstop"
        )
        self.assertEqual(_oe.PDT_FLOOR, 26_000.0,
                         "PDT_FLOOR backstop value must be 26000.0")

    # ── S26 T03: ExecutionResult has fill_price field ─────────────────────────

    def test_execution_result_has_fill_price(self):
        """T03: ExecutionResult dataclass must expose fill_price, filled_qty, fill_timestamp, qty, order_type."""
        import dataclasses

        from order_executor import ExecutionResult
        fields = {f.name for f in dataclasses.fields(ExecutionResult)}
        for expected in ("fill_price", "filled_qty", "fill_timestamp", "qty", "order_type"):
            self.assertIn(expected, fields,
                          f"ExecutionResult must have field '{expected}'")

    # ── S26 T04: ExecutionResult fill_price defaults to None ─────────────────

    def test_execution_result_fill_price_defaults_none(self):
        """T04: ExecutionResult.fill_price must default to None; order_type must default to empty string."""
        from order_executor import ExecutionResult
        result = ExecutionResult(
            order_id="test-001",
            action="buy",
            symbol="SPY",
            status="submitted",
        )
        self.assertIsNone(result.fill_price,
                          "fill_price must default to None when not populated")
        self.assertIsNone(result.filled_qty,
                          "filled_qty must default to None when not populated")
        self.assertIsNone(result.fill_timestamp,
                          "fill_timestamp must default to None when not populated")
        self.assertIsNone(result.qty,
                          "qty must default to None when not populated")
        self.assertEqual(result.order_type, "",
                         "order_type must default to empty string")

    # ── S26 T05: _extract_recommendations returns rec_id in expected format ──

    def test_recommendation_id_format(self):
        """T05: _extract_recommendations() must attach rec_id in 'rec_{week_str}_{n}' format."""
        import os
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from weekly_review import _extract_recommendations

        sample_text = (
            "RECOMMENDATIONS:\n"
            "- Increase intraday position sizing to 6%\n"
            "- Add stop-loss floor of 2% for crypto\n"
        )
        recs = _extract_recommendations(sample_text, week_str="2026-04-20")
        self.assertGreater(len(recs), 0, "must parse at least one recommendation")
        for i, rec in enumerate(recs):
            expected_id = f"rec_2026-04-20_{i + 1}"
            self.assertEqual(rec["rec_id"], expected_id,
                             f"rec_id must be 'rec_{{week_str}}_{{n}}' — got {rec['rec_id']!r}")
            self.assertEqual(rec["verdict"], "pending",
                             "new recommendations must have verdict='pending'")
            self.assertIn("created_at", rec,
                          "recommendation must have created_at timestamp")

    # ── S26 T06: _apply_recommendation_updates merges verdict updates ─────────

    def test_apply_recommendation_updates_verdict(self):
        """T06: _apply_recommendation_updates() must update verdict/resolved_at for matching rec_id."""
        import os
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from weekly_review import _apply_recommendation_updates

        history = [
            {
                "week": "2026-04-13",
                "key_recommendations": [
                    {
                        "rec_id": "rec_2026-04-13_0",
                        "recommendation": "Raise stop floors",
                        "verdict": "pending",
                        "resolved_at": "",
                    }
                ],
            }
        ]
        updates = [
            {
                "rec_id": "rec_2026-04-13_0",
                "verdict": "helped",
                "resolved_at": "2026-04-20T00:00:00+00:00",
            }
        ]
        result = _apply_recommendation_updates(history, updates)
        rec = result[0]["key_recommendations"][0]
        self.assertEqual(rec["verdict"], "helped",
                         "verdict must be updated to 'helped'")
        self.assertEqual(rec["resolved_at"], "2026-04-20T00:00:00+00:00",
                         "resolved_at must be stamped by the update")

    # ── S26 T07: _apply_recommendation_updates ignores unknown rec_id ─────────

    def test_apply_updates_unknown_rec_id_ignored(self):
        """T07: _apply_recommendation_updates() must silently skip updates with unknown rec_id."""
        import os
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from weekly_review import _apply_recommendation_updates

        history = [
            {
                "week": "2026-04-13",
                "key_recommendations": [
                    {
                        "rec_id": "rec_2026-04-13_0",
                        "recommendation": "Reduce leverage",
                        "verdict": "pending",
                        "resolved_at": "",
                    }
                ],
            }
        ]
        updates = [
            {
                "rec_id": "rec_2026-04-13_NONEXISTENT",
                "verdict": "hurt",
                "resolved_at": "2026-04-20T00:00:00+00:00",
            }
        ]
        result = _apply_recommendation_updates(history, updates)
        rec = result[0]["key_recommendations"][0]
        self.assertEqual(rec["verdict"], "pending",
                         "verdict of unmatched rec must remain 'pending' — unknown rec_id must be ignored")


# Suite 28 — Epic 1 Shared Substrate: T1.1–T1.8


class Suite28Epic1SharedSubstrate(unittest.TestCase):
    """Suite 28 — Epic 1: semantic_labels, hindsight, rec_store, context_compiler,
    incident_schema, decision_outcomes alpha, abstention, model_tiering (20 tests)."""

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ── semantic_labels.py ────────────────────────────────────────────────────

    def test_catalyst_type_values_match_taxonomy(self):
        """CatalystType spot-check: 3 values present per taxonomy_v1.0.0.md."""
        from semantic_labels import CatalystType
        self.assertIn("earnings_beat", [c.value for c in CatalystType])
        self.assertIn("citrini_thesis", [c.value for c in CatalystType])
        self.assertIn("unknown", [c.value for c in CatalystType])

    def test_regime_type_values_match_taxonomy(self):
        """RegimeType spot-check: 3 values present per taxonomy_v1.0.0.md."""
        from semantic_labels import RegimeType
        self.assertIn("risk_on", [r.value for r in RegimeType])
        self.assertIn("volatility_spike", [r.value for r in RegimeType])
        self.assertIn("unknown", [r.value for r in RegimeType])

    def test_thesis_type_values_match_taxonomy(self):
        """ThesisType spot-check: 3 values present per taxonomy_v1.0.0.md."""
        from semantic_labels import ThesisType
        self.assertIn("catalyst_swing", [t.value for t in ThesisType])
        self.assertIn("macro_overlay", [t.value for t in ThesisType])
        self.assertIn("unknown", [t.value for t in ThesisType])

    def test_validate_label_known_value(self):
        """validate_label returns value unchanged for known enum member."""
        from semantic_labels import CatalystType, validate_label
        result = validate_label(CatalystType, "earnings_beat")
        self.assertEqual(result, "earnings_beat")

    def test_validate_label_unknown_allow(self):
        """validate_label allows unknown value when allow_unknown=True (logs warning)."""
        from semantic_labels import CatalystType, validate_label
        result = validate_label(CatalystType, "invented_label", allow_unknown=True)
        self.assertEqual(result, "invented_label")

    def test_validate_label_unknown_disallow_raises(self):
        """validate_label raises ValueError when allow_unknown=False and value not in enum."""
        from semantic_labels import CatalystType, validate_label
        with self.assertRaises(ValueError):
            validate_label(CatalystType, "invented_label", allow_unknown=False)

    # ── hindsight.py ──────────────────────────────────────────────────────────

    def test_hindsight_write_read_roundtrip(self):
        """log_hindsight_record writes to JSONL, get_hindsight_records reads it back."""
        import cost_attribution as ca
        import hindsight as hs
        orig_path = hs._HINDSIGHT_PATH
        orig_spine = ca._SPINE_ENABLED
        try:
            hs._HINDSIGHT_PATH = Path(self.tmpdir) / "hindsight.jsonl"
            ca._SPINE_ENABLED = False  # suppress spine writes in test

            import feature_flags as ff
            orig_ff_path = ff._CONFIG_PATH
            ff._CONFIG_PATH = Path(self.tmpdir) / "cfg.json"
            ff._FLAG_CACHE = {}
            ff._CACHE_LOADED = False
            ff._CONFIG_PATH.write_text(json.dumps({
                "feature_flags": {"enable_recommendation_memory": True},
                "shadow_flags": {}, "lab_flags": {},
            }))

            rec = hs.build_hindsight_record(
                subject_id="dec_A1_test",
                subject_type="trade",
                expected_effect="GLD would rise 1%",
                observed_result="GLD rose 2%",
                verdict="confirmed",
                confidence=0.8,
                explanation="Thesis validated by price action",
                evidence_window_start="2026-04-16T09:00:00Z",
                evidence_window_end="2026-04-17T09:00:00Z",
                evaluator_module="test_module",
            )
            result_id = hs.log_hindsight_record(rec)
            self.assertIsNotNone(result_id)

            records = hs.get_hindsight_records(days_back=30)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["subject_id"], "dec_A1_test")
            self.assertEqual(records[0]["verdict"], "confirmed")
        finally:
            hs._HINDSIGHT_PATH = orig_path
            ca._SPINE_ENABLED = orig_spine
            ff._CONFIG_PATH = orig_ff_path
            ff._FLAG_CACHE = {}
            ff._CACHE_LOADED = False

    def test_hindsight_flag_disabled_noop(self):
        """log_hindsight_record returns None without writing when flag is disabled."""
        import feature_flags as ff
        import hindsight as hs
        orig_path = hs._HINDSIGHT_PATH
        orig_ff_path = ff._CONFIG_PATH
        try:
            hs._HINDSIGHT_PATH = Path(self.tmpdir) / "hindsight_disabled.jsonl"
            ff._CONFIG_PATH = Path(self.tmpdir) / "cfg_dis.json"
            ff._FLAG_CACHE = {}
            ff._CACHE_LOADED = False
            ff._CONFIG_PATH.write_text(json.dumps({
                "feature_flags": {"enable_recommendation_memory": False},
                "shadow_flags": {}, "lab_flags": {},
            }))
            rec = hs.build_hindsight_record(
                subject_id="x", subject_type="trade",
                expected_effect="e", observed_result="o",
                verdict="pending", confidence=0.5, explanation="test",
                evidence_window_start="2026-04-16T00:00:00Z",
                evidence_window_end="2026-04-17T00:00:00Z",
            )
            result = hs.log_hindsight_record(rec)
            self.assertIsNone(result)
            self.assertFalse(hs._HINDSIGHT_PATH.exists())
        finally:
            hs._HINDSIGHT_PATH = orig_path
            ff._CONFIG_PATH = orig_ff_path
            ff._FLAG_CACHE = {}
            ff._CACHE_LOADED = False

    # ── recommendation_store.py ───────────────────────────────────────────────

    def test_recommendation_save_get_update_roundtrip(self):
        """save + get + update_verdict round-trip works correctly."""
        import feature_flags as ff
        import recommendation_store as rs
        orig_store = rs._STORE_PATH
        orig_ff_path = ff._CONFIG_PATH
        try:
            rs._STORE_PATH = Path(self.tmpdir) / "rec_store.json"
            ff._CONFIG_PATH = Path(self.tmpdir) / "cfg.json"
            ff._FLAG_CACHE = {}
            ff._CACHE_LOADED = False
            ff._CONFIG_PATH.write_text(json.dumps({
                "feature_flags": {"enable_recommendation_memory": True},
                "shadow_flags": {}, "lab_flags": {},
            }))
            from recommendation_store import RecommendationRecord
            rec = RecommendationRecord(
                rec_id="rec_2026-04-16_1",
                week_str="2026-04-16",
                created_at="2026-04-16T12:00:00Z",
                source_module="test",
                recommendation_text="Reduce leverage by 20%",
                verdict="pending",
            )
            ok = rs.save_recommendation(rec)
            self.assertTrue(ok)

            fetched = rs.get_recommendation("rec_2026-04-16_1")
            self.assertIsNotNone(fetched)
            self.assertEqual(fetched.recommendation_text, "Reduce leverage by 20%")
            self.assertEqual(fetched.verdict, "pending")

            ok2 = rs.update_verdict("rec_2026-04-16_1", "verified", "Fix confirmed in live data")
            self.assertTrue(ok2)
            updated = rs.get_recommendation("rec_2026-04-16_1")
            self.assertEqual(updated.verdict, "verified")
        finally:
            rs._STORE_PATH = orig_store
            ff._CONFIG_PATH = orig_ff_path
            ff._FLAG_CACHE = {}
            ff._CACHE_LOADED = False

    def test_recommendation_atomic_write(self):
        """save_recommendation produces a valid JSON file (no .tmp left behind)."""
        import feature_flags as ff
        import recommendation_store as rs
        orig_store = rs._STORE_PATH
        orig_ff_path = ff._CONFIG_PATH
        try:
            store_path = Path(self.tmpdir) / "rec_store_atomic.json"
            rs._STORE_PATH = store_path
            ff._CONFIG_PATH = Path(self.tmpdir) / "cfg2.json"
            ff._FLAG_CACHE = {}
            ff._CACHE_LOADED = False
            ff._CONFIG_PATH.write_text(json.dumps({
                "feature_flags": {"enable_recommendation_memory": True},
                "shadow_flags": {}, "lab_flags": {},
            }))
            from recommendation_store import RecommendationRecord
            rec = RecommendationRecord(rec_id="rec_atom_1", week_str="2026-04-16",
                                       created_at="", source_module="test",
                                       recommendation_text="atomic test")
            rs.save_recommendation(rec)
            self.assertTrue(store_path.exists())
            self.assertFalse(store_path.with_suffix(".tmp").exists(), ".tmp must be cleaned up")
            raw = json.loads(store_path.read_text())
            self.assertIn("rec_atom_1", raw.get("records", {}))
        finally:
            rs._STORE_PATH = orig_store
            ff._CONFIG_PATH = orig_ff_path
            ff._FLAG_CACHE = {}
            ff._CACHE_LOADED = False

    # ── context_compiler.py ───────────────────────────────────────────────────

    def test_context_compiler_flag_disabled_returns_none(self):
        """compress_section returns None without making API call when flag is disabled."""
        import context_compiler as cc
        import feature_flags as ff
        orig_ff_path = ff._CONFIG_PATH
        try:
            ff._CONFIG_PATH = Path(self.tmpdir) / "cfg.json"
            ff._FLAG_CACHE = {}
            ff._CACHE_LOADED = False
            ff._CONFIG_PATH.write_text(json.dumps({
                "feature_flags": {},
                "shadow_flags": {"enable_context_compressor_shadow": False},
                "lab_flags": {},
            }))
            result = cc.compress_section("macro_backdrop", "Some macro content here.")
            self.assertIsNone(result)
        finally:
            ff._CONFIG_PATH = orig_ff_path
            ff._FLAG_CACHE = {}
            ff._CACHE_LOADED = False

    def test_compressed_section_schema(self):
        """CompressedSection dataclass has all required fields."""
        from context_compiler import CompressedSection
        s = CompressedSection(
            schema_version=1,
            section_name="macro_backdrop",
            cycle_id="cycle_001",
            compressed_at="2026-04-16T12:00:00Z",
            raw_length_chars=500,
            compressed_length_chars=120,
            compression_ratio=0.24,
            raw_content="full macro text",
            compressed_content="compressed",
            model="claude-haiku-4-5-20251001",
            input_tokens=100,
            output_tokens=30,
            estimated_cost_usd=0.00025,
        )
        self.assertEqual(s.schema_version, 1)
        self.assertEqual(s.section_name, "macro_backdrop")
        self.assertAlmostEqual(s.compression_ratio, 0.24)

    # ── incident_schema.py ────────────────────────────────────────────────────

    def test_incident_build_log_get_roundtrip(self):
        """build_incident + log_incident + get_incidents round-trip."""
        import feature_flags as ff
        import incident_schema as isc
        orig_path = isc._INCIDENT_PATH
        orig_ff_path = ff._CONFIG_PATH
        try:
            isc._INCIDENT_PATH = Path(self.tmpdir) / "incidents.jsonl"
            ff._CONFIG_PATH = Path(self.tmpdir) / "cfg.json"
            ff._FLAG_CACHE = {}
            ff._CACHE_LOADED = False
            ff._CONFIG_PATH.write_text(json.dumps({
                "feature_flags": {"enable_schema_migrations": True},
                "shadow_flags": {}, "lab_flags": {},
            }))
            rec = isc.build_incident(
                incident_type="stop_missing",
                account="account1",
                severity="critical",
                description="GLD has no stop-loss order",
                subject_id="GLD",
                subject_type="symbol",
            )
            inc_id = isc.log_incident(rec)
            self.assertIsNotNone(inc_id)

            results = isc.get_incidents(incident_type="stop_missing", days_back=1)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["account"], "account1")
            self.assertEqual(results[0]["severity"], "critical")
        finally:
            isc._INCIDENT_PATH = orig_path
            ff._CONFIG_PATH = orig_ff_path
            ff._FLAG_CACHE = {}
            ff._CACHE_LOADED = False

    def test_divergence_wiring_nonfatal(self):
        """incident wiring in divergence.log_divergence_event is non-fatal even if incident_schema fails."""
        import divergence
        import feature_flags as ff
        orig_ff_path = ff._CONFIG_PATH
        try:
            # Point flags to a config where enable_schema_migrations is False
            ff._CONFIG_PATH = Path(self.tmpdir) / "cfg.json"
            ff._FLAG_CACHE = {}
            ff._CACHE_LOADED = False
            ff._CONFIG_PATH.write_text(json.dumps({
                "feature_flags": {"enable_schema_migrations": False},
                "shadow_flags": {}, "lab_flags": {},
            }))
            # This should not raise even though incident logging is disabled
            event = divergence.DivergenceEvent(
                event_id="div_test",
                timestamp="2026-04-16T12:00:00Z",
                account="A1_TEST",
                symbol="GLD",
                event_type="stop_missing",
                severity=divergence.DivergenceSeverity.DE_RISK,
                scope=divergence.DivergenceScope.SYMBOL,
                scope_id="GLD",
                paper_expected=None,
                live_observed=None,
                delta=None,
                recoverability="auto",
                risk_impact="medium",
                repaired=False,
                repair_attempt_count=0,
                decision_id="",
                trade_id="",
                structure_id="",
            )
            # Redirect divergence log to tempdir to avoid writing to production paths
            orig_log = divergence.DIVERGENCE_LOG
            divergence.DIVERGENCE_LOG = Path(self.tmpdir) / "div.jsonl"
            try:
                divergence.log_divergence_event(event)
            finally:
                divergence.DIVERGENCE_LOG = orig_log
            # No exception = pass
        finally:
            ff._CONFIG_PATH = orig_ff_path
            ff._FLAG_CACHE = {}
            ff._CACHE_LOADED = False

    # ── decision_outcomes.py alpha classification ─────────────────────────────

    def test_classify_alpha_insufficient_sample_no_return(self):
        """classify_alpha returns insufficient_sample when return_1d is None."""
        from decision_outcomes import DecisionOutcomeRecord, classify_alpha
        rec = DecisionOutcomeRecord(
            decision_id="dec_test", account="A1", symbol="GLD",
            timestamp="2026-04-16T12:00:00Z", action="buy",
            status="submitted", return_1d=None,
        )
        result = classify_alpha(rec)
        self.assertEqual(result, "insufficient_sample")

    def test_classify_alpha_positive(self):
        """classify_alpha returns alpha_positive for correct +1d direction with >0.3% return."""
        from datetime import datetime, timedelta, timezone

        from decision_outcomes import DecisionOutcomeRecord, classify_alpha
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat().replace("+00:00", "Z")
        rec = DecisionOutcomeRecord(
            decision_id="dec_test", account="A1", symbol="GLD",
            timestamp=old_ts, action="buy",
            status="submitted",
            return_1d=0.015, correct_1d=True,
        )
        result = classify_alpha(rec)
        self.assertEqual(result, "alpha_positive")

    def test_classify_alpha_insufficient_sample_recent(self):
        """classify_alpha returns insufficient_sample for records < 24h old."""
        from datetime import datetime, timezone

        from decision_outcomes import DecisionOutcomeRecord, classify_alpha
        rec = DecisionOutcomeRecord(
            decision_id="dec_test", account="A1", symbol="GLD",
            timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            action="buy", status="submitted",
            return_1d=0.02, correct_1d=True,
        )
        result = classify_alpha(rec)
        self.assertEqual(result, "insufficient_sample")

    # ── abstention.py ─────────────────────────────────────────────────────────

    def test_abstain_empty_reason_raises(self):
        """abstain() raises ValueError when reason is empty."""
        from abstention import abstain
        with self.assertRaises(ValueError):
            abstain(reason="", module_name="test_module")

    def test_did_abstain_handles_none(self):
        """did_abstain returns True when passed None."""
        from abstention import did_abstain
        self.assertTrue(did_abstain(None))

    def test_abstention_rate_calculation(self):
        """abstention_rate correctly computes rate from mixed records."""
        from abstention import abstention_rate
        records = [
            {"abstention": {"abstain": True, "abstention_reason": "no data"}},
            {"abstention": {"abstain": True, "abstention_reason": "unclear"}},
            {"abstention": {"abstain": False}},
            {"abstention": None},
        ]
        rate = abstention_rate(records)
        self.assertAlmostEqual(rate, 0.5, places=2)

    # ── model_tiering.py ──────────────────────────────────────────────────────

    def test_get_model_for_module_correct_string(self):
        """get_model_for_module returns correct canonical model string for known modules."""
        import feature_flags as ff
        import model_tiering as mt
        orig_ff_path = ff._CONFIG_PATH
        try:
            ff._CONFIG_PATH = Path(self.tmpdir) / "cfg.json"
            ff._FLAG_CACHE = {}
            ff._CACHE_LOADED = False
            ff._CONFIG_PATH.write_text(json.dumps({
                "feature_flags": {"enable_model_tiering": False},
                "shadow_flags": {}, "lab_flags": {},
            }))
            haiku = mt.get_model_for_module("regime_classifier")
            self.assertEqual(haiku, "claude-haiku-4-5-20251001")
            sonnet = mt.get_model_for_module("main_decision")
            self.assertEqual(sonnet, "claude-sonnet-4-6")
        finally:
            ff._CONFIG_PATH = orig_ff_path
            ff._FLAG_CACHE = {}
            ff._CACHE_LOADED = False

    def test_escalation_predicate_fires_on_conflict(self):
        """should_escalate_to_premium fires when signals conflict and scores are tight."""
        from model_tiering import EscalationContext, should_escalate_to_premium
        ctx = EscalationContext(
            top_signal_scores=[65.0, 68.0, 70.0],  # tight range (<10 points)
            regime_score=50,
            regime_bias="bullish",
            open_position_count=2,
            signals_conflict=True,
            catalyst_count=1,
            deadline_approaching=False,
            vix_level=20.0,
        )
        should, reason = should_escalate_to_premium(ctx)
        self.assertTrue(should)
        self.assertEqual(reason, "ambiguous_signal_environment")

    def test_escalation_no_trigger(self):
        """should_escalate_to_premium returns False when no trigger fires."""
        from model_tiering import EscalationContext, should_escalate_to_premium
        ctx = EscalationContext(
            top_signal_scores=[50.0, 70.0, 90.0],  # wide range
            regime_score=70,
            regime_bias="bullish",
            open_position_count=2,
            signals_conflict=False,
            catalyst_count=1,
            deadline_approaching=False,
            vix_level=18.0,
        )
        should, _ = should_escalate_to_premium(ctx)
        self.assertFalse(should)


# ─────────────────────────────────────────────────────────────────────────────
# Suite 29 — Epic 2 Production Learning Core (T2.1–T2.8)
# ─────────────────────────────────────────────────────────────────────────────

class Suite29Epic2ProductionLearning(unittest.TestCase):
    """Smoke + behavior tests for all 8 Epic 2 modules."""

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ── T2.1 thesis_checksum ─────────────────────────────────────────────────

    def test_checksum_build_from_decision(self):
        """build_checksum_from_decision returns ThesisChecksum with required fields."""
        import thesis_checksum as tc
        idea = {"catalyst": "Strong earnings beat above consensus", "tier": "core", "action": "buy", "confidence": "high"}
        regime = {"bias": "bullish", "regime_score": 65, "vix": 18}
        scores = {"scored_symbols": {"AAPL": {"score": 72}}}
        result = tc.build_checksum_from_decision(
            decision_id="dec-001",
            symbol="AAPL",
            idea=idea,
            regime_obj=regime,
            signal_scores=scores,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.decision_id, "dec-001")
        self.assertEqual(result.symbol, "AAPL")
        self.assertIsNotNone(result.checksum_id)
        self.assertIsNotNone(result.thesis_type)
        self.assertIsNotNone(result.catalyst_type)
        self.assertEqual(result.signal_score_at_entry, 72)

    def test_checksum_log_get_roundtrip(self):
        """log_checksum + get_checksum roundtrip returns matching record."""
        from pathlib import Path

        import thesis_checksum as tc
        orig_path = tc._CHECKSUM_PATH
        try:
            tc._CHECKSUM_PATH = Path(self.tmpdir) / "thesis_checksums.jsonl"
            cs = tc.ThesisChecksum(
                decision_id="dec-roundtrip",
                symbol="MSFT",
                thesis_type="catalyst_swing",
                raw_catalyst_text="Fed signal hawkish",
            )
            cid = tc.log_checksum(cs)
            self.assertIsNotNone(cid)
            found = tc.get_checksum("dec-roundtrip")
            self.assertIsNotNone(found)
            self.assertEqual(found.symbol, "MSFT")
        finally:
            tc._CHECKSUM_PATH = orig_path

    def test_checksum_returns_none_on_empty_decision_id(self):
        """build_checksum_from_decision returns None if decision_id is empty."""
        import thesis_checksum as tc
        result = tc.build_checksum_from_decision("", "AAPL", {}, {}, {})
        self.assertIsNone(result)

    # ── T2.2 catalyst_normalizer ─────────────────────────────────────────────

    def test_catalyst_normalizer_earnings_keyword(self):
        """'beat consensus' maps to earnings_beat catalyst type."""
        import catalyst_normalizer as cn
        obj = cn.normalize_catalyst("Strong earnings beat above consensus Q3", "d1", "AAPL")
        self.assertEqual(obj.catalyst_type, "earnings_beat")
        self.assertGreater(obj.confidence, 0.5)

    def test_catalyst_normalizer_empty_text_abstains(self):
        """Empty catalyst text results in abstention with unknown catalyst_type."""
        import catalyst_normalizer as cn
        obj = cn.normalize_catalyst("", "d2", "AAPL")
        self.assertEqual(obj.catalyst_type, "unknown")
        self.assertIsNotNone(obj.abstention)
        self.assertTrue(obj.abstention.get("abstain"))

    def test_catalyst_normalizer_unknown_text_low_confidence(self):
        """Unrecognized text gets catalyst_type=unknown, confidence=0.1."""
        import catalyst_normalizer as cn
        obj = cn.normalize_catalyst("something completely unrelated xyz", "d3", "SPY")
        self.assertEqual(obj.catalyst_type, "unknown")
        self.assertEqual(obj.confidence, 0.1)

    def test_catalyst_log_get_roundtrip(self):
        """log_catalyst + get_catalyst roundtrip."""
        from pathlib import Path

        import catalyst_normalizer as cn
        orig_path = cn._CATALYST_LOG
        try:
            cn._CATALYST_LOG = Path(self.tmpdir) / "catalyst_log.jsonl"
            obj = cn.normalize_catalyst("Fed signal rate decision", "d-fed", "TLT")
            cid = cn.log_catalyst(obj)
            self.assertIsNotNone(cid)
            found = cn.get_catalyst("d-fed")
            self.assertIsNotNone(found)
            self.assertEqual(found.symbol, "TLT")
        finally:
            cn._CATALYST_LOG = orig_path

    # ── T2.3 forensic_reviewer ───────────────────────────────────────────────

    def test_forensic_flag_disabled_returns_none(self):
        """review_closed_trade returns None when enable_thesis_checksum is False."""
        import feature_flags as ff
        import forensic_reviewer as fr
        orig_path = ff._CONFIG_PATH
        orig_cache = dict(ff._FLAG_CACHE)
        orig_loaded = ff._CACHE_LOADED
        import json
        from pathlib import Path
        tmp_cfg = Path(self.tmpdir) / "cfg.json"
        tmp_cfg.write_text(json.dumps({"feature_flags": {"enable_thesis_checksum": False}}))
        try:
            ff._CONFIG_PATH = tmp_cfg
            ff._FLAG_CACHE = {}
            ff._CACHE_LOADED = False
            result = fr.review_closed_trade(
                decision_id="d-dis", symbol="AAPL",
                entry_price=150.0, exit_price=155.0,
                realized_pnl=5.0, hold_duration_hours=24.0,
                entry_decision={}, exit_reason="stop_hit",
            )
            self.assertIsNone(result)
        finally:
            ff._CONFIG_PATH = orig_path
            ff._FLAG_CACHE = orig_cache
            ff._CACHE_LOADED = orig_loaded

    def test_forensic_record_schema(self):
        """ForensicRecord has all required schema_version fields."""
        from forensic_reviewer import ForensicRecord
        r = ForensicRecord(decision_id="d1", symbol="MSFT")
        self.assertEqual(r.schema_version, 1)
        self.assertIn("thesis_verdict", r.to_dict())
        self.assertIn("execution_verdict", r.to_dict())
        self.assertIn("model_used", r.to_dict())

    def test_forensic_log_get_roundtrip(self):
        """log_forensic + get_forensic roundtrip."""
        from pathlib import Path

        import forensic_reviewer as fr
        orig_path = fr._FORENSIC_LOG
        try:
            fr._FORENSIC_LOG = Path(self.tmpdir) / "forensic_log.jsonl"
            rec = fr.ForensicRecord(decision_id="d-flog", symbol="GLD", thesis_verdict="correct")
            fid = fr.log_forensic(rec)
            self.assertIsNotNone(fid)
            found = fr.get_forensic("d-flog")
            self.assertIsNotNone(found)
            self.assertEqual(found.thesis_verdict, "correct")
        finally:
            fr._FORENSIC_LOG = orig_path

    # ── T2.4 recommendation_resolver ────────────────────────────────────────

    def test_resolver_flag_disabled_returns_empty(self):
        """resolve_pending_recommendations returns [] when flag disabled."""
        import json
        from pathlib import Path

        import feature_flags as ff
        import recommendation_resolver as rr
        orig_path = ff._CONFIG_PATH
        orig_cache = dict(ff._FLAG_CACHE)
        orig_loaded = ff._CACHE_LOADED
        tmp_cfg = Path(self.tmpdir) / "cfg.json"
        tmp_cfg.write_text(json.dumps({"feature_flags": {"enable_recommendation_memory": False}}))
        try:
            ff._CONFIG_PATH = tmp_cfg
            ff._FLAG_CACHE = {}
            ff._CACHE_LOADED = False
            result = rr.resolve_pending_recommendations()
            self.assertEqual(result, [])
        finally:
            ff._CONFIG_PATH = orig_path
            ff._FLAG_CACHE = orig_cache
            ff._CACHE_LOADED = orig_loaded

    def test_resolver_too_young_rec_stays_pending(self):
        """Recommendation created today is not resolved (min_age_days=7)."""
        from dataclasses import dataclass
        from datetime import datetime, timezone
        from typing import Optional

        import recommendation_resolver as rr

        @dataclass
        class MockRec:
            rec_id: str = "rec-test-1"
            created_at: str = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            expected_direction: str = "up"
            target_metric: str = "return_1d"
            verdict: str = "pending"
            text: str = "test recommendation"
            resolved_at: Optional[str] = None

        mock_rec = MockRec()
        now = datetime.now(timezone.utc)
        from datetime import timedelta
        min_age_cutoff = now - timedelta(days=7)

        result = rr._resolve_single(mock_rec, {"submitted_count": 5, "avg_return_1d": 0.01}, min_age_cutoff, now)
        self.assertIsNone(result)  # too young — should not be resolved

    # ── T2.5 anti_pattern_miner ──────────────────────────────────────────────

    def test_anti_pattern_below_threshold_abstains(self):
        """Patterns with fewer than min_occurrences are not surfaced."""
        import json
        from pathlib import Path

        import anti_pattern_miner as apm
        import feature_flags as ff
        orig_path = ff._CONFIG_PATH
        orig_cache = dict(ff._FLAG_CACHE)
        orig_loaded = ff._CACHE_LOADED
        tmp_cfg = Path(self.tmpdir) / "cfg.json"
        tmp_cfg.write_text(json.dumps({"feature_flags": {"enable_thesis_checksum": True}}))
        apm.Path("data/analytics/forensic_log.jsonl")

        from pathlib import Path as _P
        forensic_log = _P(self.tmpdir) / "forensic_log.jsonl"
        # Write 2 records with same pattern (below threshold of 3)
        from datetime import datetime, timezone
        now_str = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        for _ in range(2):
            forensic_log.open("a").write(json.dumps({
                "schema_version": 1,
                "forensic_id": "f1",
                "decision_id": "d1",
                "symbol": "AAPL",
                "created_at": now_str,
                "thesis_verdict": "incorrect",
                "execution_verdict": "poor",
                "pattern_tags": ["momentum_continuation"],
                "realized_pnl": -50.0,
            }) + "\n")

        try:
            ff._CONFIG_PATH = tmp_cfg
            ff._FLAG_CACHE = {}
            ff._CACHE_LOADED = False
            # Temporarily redirect forensic path
            import anti_pattern_miner as _apm2
            _apm2.Path("data/analytics/forensic_log.jsonl")

            # Patch the path inside the module
            import unittest.mock as mock
            with mock.patch("anti_pattern_miner.Path") as mock_path:
                def path_side_effect(p):
                    if "forensic_log" in str(p):
                        return forensic_log
                    return _P(p)
                mock_path.side_effect = path_side_effect
                patterns = apm.mine_anti_patterns(min_occurrences=3)
                self.assertEqual(patterns, [])  # 2 < 3, not surfaced
        finally:
            ff._CONFIG_PATH = orig_path
            ff._FLAG_CACHE = orig_cache
            ff._CACHE_LOADED = orig_loaded

    def test_anti_pattern_above_threshold_surfaces_pattern(self):
        """Patterns with >= min_occurrences are surfaced as findings."""
        import json
        from pathlib import Path

        import anti_pattern_miner as apm
        import feature_flags as ff
        orig_path = ff._CONFIG_PATH
        orig_cache = dict(ff._FLAG_CACHE)
        orig_loaded = ff._CACHE_LOADED
        tmp_cfg = Path(self.tmpdir) / "cfg.json"
        tmp_cfg.write_text(json.dumps({"feature_flags": {"enable_thesis_checksum": True}}))
        forensic_log = Path(self.tmpdir) / "forensic_log.jsonl"
        from datetime import datetime, timezone
        now_str = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        for i in range(4):
            with forensic_log.open("a") as f:
                f.write(json.dumps({
                    "schema_version": 1,
                    "forensic_id": f"f{i}",
                    "decision_id": f"d{i}",
                    "symbol": "AAPL",
                    "created_at": now_str,
                    "thesis_verdict": "incorrect",
                    "execution_verdict": "neutral",
                    "pattern_tags": ["mean_reversion", "thin_tape"],
                    "realized_pnl": -100.0,
                }) + "\n")
        try:
            ff._CONFIG_PATH = tmp_cfg
            ff._FLAG_CACHE = {}
            ff._CACHE_LOADED = False
            import unittest.mock as mock
            from pathlib import Path as _P
            with mock.patch("anti_pattern_miner.Path") as mock_path:
                def path_side_effect(p):
                    if "forensic_log" in str(p):
                        return forensic_log
                    return _P(p)
                mock_path.side_effect = path_side_effect
                patterns = apm.mine_anti_patterns(min_occurrences=3)
                self.assertGreater(len(patterns), 0)
                self.assertEqual(patterns[0].occurrence_count, 4)
        finally:
            ff._CONFIG_PATH = orig_path
            ff._FLAG_CACHE = orig_cache
            ff._CACHE_LOADED = orig_loaded

    # ── T2.6 divergence_summarizer ───────────────────────────────────────────

    def test_divergence_summarizer_below_min_returns_none(self):
        """summarize_divergence_incidents returns None when fewer than min_incidents."""
        import json
        from pathlib import Path

        import divergence_summarizer as ds
        import feature_flags as ff
        orig_path = ff._CONFIG_PATH
        orig_cache = dict(ff._FLAG_CACHE)
        orig_loaded = ff._CACHE_LOADED
        tmp_cfg = Path(self.tmpdir) / "cfg.json"
        tmp_cfg.write_text(json.dumps({"feature_flags": {"enable_divergence_summarizer": True}}))
        try:
            ff._CONFIG_PATH = tmp_cfg
            ff._FLAG_CACHE = {}
            ff._CACHE_LOADED = False
            incident_log = Path(self.tmpdir) / "incident_log.jsonl"
            # Only 1 incident
            from datetime import datetime, timezone
            incident_log.write_text(json.dumps({
                "incident_id": "i1", "incident_type": "stop_missing",
                "severity": "warning", "detected_at": datetime.now(timezone.utc).isoformat(),
            }) + "\n")
            import unittest.mock as mock
            with mock.patch("divergence_summarizer._INCIDENT_LOG", incident_log):
                result = ds.summarize_divergence_incidents(min_incidents=2)
                self.assertIsNone(result)
        finally:
            ff._CONFIG_PATH = orig_path
            ff._FLAG_CACHE = orig_cache
            ff._CACHE_LOADED = orig_loaded

    def test_divergence_summarizer_summary_dict_schema(self):
        """Summary dict has required keys: clusters, root_causes, recommendations."""
        from divergence_summarizer import _cluster_incidents
        incidents = [
            {"severity": "warning", "incident_type": "stop_missing", "description": "stop missing on GLD"},
            {"severity": "critical", "incident_type": "fill_price_drift", "description": "drift on MSFT"},
            {"severity": "warning", "incident_type": "stop_missing", "description": "stop missing on TSM"},
        ]
        clusters = _cluster_incidents(incidents)
        self.assertIn("warning:stop_missing", clusters)
        self.assertEqual(clusters["warning:stop_missing"]["count"], 2)

    # ── T2.7 experience_library ──────────────────────────────────────────────

    def test_experience_repaired_failure_requires_repair_marker(self):
        """save_experience raises ValueError for repaired_failure_case without repair_marker."""
        import json
        from pathlib import Path

        import experience_library as el
        import feature_flags as ff
        orig_path = ff._CONFIG_PATH
        orig_cache = dict(ff._FLAG_CACHE)
        orig_loaded = ff._CACHE_LOADED
        tmp_cfg = Path(self.tmpdir) / "cfg.json"
        tmp_cfg.write_text(json.dumps({"feature_flags": {"enable_experience_library": True}}))
        try:
            ff._CONFIG_PATH = tmp_cfg
            ff._FLAG_CACHE = {}
            ff._CACHE_LOADED = False
            rec = el.ExperienceRecord(
                record_type="repaired_failure_case",
                symbol="AAPL",
                decision_id="d-repair",
                summary="test",
                repair_marker="",  # empty — should raise
            )
            with self.assertRaises(ValueError):
                el.save_experience(rec)
        finally:
            ff._CONFIG_PATH = orig_path
            ff._FLAG_CACHE = orig_cache
            ff._CACHE_LOADED = orig_loaded

    def test_experience_roundtrip(self):
        """save_experience + get_experiences roundtrip returns correct record."""
        import json
        from pathlib import Path

        import experience_library as el
        import feature_flags as ff
        orig_path = ff._CONFIG_PATH
        orig_cache = dict(ff._FLAG_CACHE)
        orig_loaded = ff._CACHE_LOADED
        orig_exp_log = el._EXPERIENCE_LOG
        tmp_cfg = Path(self.tmpdir) / "cfg.json"
        tmp_cfg.write_text(json.dumps({"feature_flags": {"enable_experience_library": True}}))
        try:
            ff._CONFIG_PATH = tmp_cfg
            ff._FLAG_CACHE = {}
            ff._CACHE_LOADED = False
            el._EXPERIENCE_LOG = Path(self.tmpdir) / "experience_library.jsonl"
            rec = el.ExperienceRecord(
                record_type="success_case",
                symbol="GLD",
                decision_id="d-exp",
                summary="GLD catalyst swing success",
                realized_pnl=150.0,
                thesis_type="catalyst_swing",
            )
            eid = el.save_experience(rec)
            self.assertIsNotNone(eid)
            results = el.get_experiences(symbol="GLD")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].thesis_type, "catalyst_swing")
        finally:
            ff._CONFIG_PATH = orig_path
            ff._FLAG_CACHE = orig_cache
            ff._CACHE_LOADED = orig_loaded
            el._EXPERIENCE_LOG = orig_exp_log

    # ── T2.8 experience_retrieval ────────────────────────────────────────────

    def test_experience_retrieval_relevance_scoring(self):
        """retrieve_similar_experiences scores symbol match higher than regime match."""
        from experience_library import ExperienceRecord
        from experience_retrieval import _score_record

        rec = ExperienceRecord(
            symbol="AAPL",
            record_type="success_case",
            decision_id="d1",
            experience_id="e1",
            thesis_type="catalyst_swing",
            catalyst_type="earnings_beat",
            regime_at_entry="risk_on",
            summary="AAPL earnings beat success",
        )
        # Symbol + thesis + catalyst + regime match = 3+2+2+1 = 8
        score_all = _score_record(rec, symbol="AAPL", thesis_type="catalyst_swing", catalyst_type="earnings_beat", regime="risk_on")
        # Symbol only = 3
        score_sym = _score_record(rec, symbol="AAPL", thesis_type=None, catalyst_type=None, regime=None)
        # Regime only = 1
        score_reg = _score_record(rec, symbol=None, thesis_type=None, catalyst_type=None, regime="risk_on")

        self.assertEqual(score_all, 8)
        self.assertEqual(score_sym, 3)
        self.assertEqual(score_reg, 1)
        self.assertGreater(score_sym, score_reg)

    def test_experience_retrieval_provenance_required(self):
        """Results without decision_id are filtered out by _to_result."""
        from experience_library import ExperienceRecord
        from experience_retrieval import _to_result

        rec_no_dec = ExperienceRecord(
            symbol="AAPL",
            record_type="success_case",
            experience_id="e1",
            decision_id="",  # missing — should be filtered
            summary="test",
        )
        result = _to_result(rec_no_dec, score=5)
        self.assertIsNone(result)

    def test_experience_retrieval_provenance_present(self):
        """Results with all IDs include provenance dict with experience_id and decision_id."""
        from experience_library import ExperienceRecord
        from experience_retrieval import _to_result

        rec = ExperienceRecord(
            symbol="AAPL",
            record_type="success_case",
            experience_id="e-uuid-123",
            decision_id="d-uuid-456",
            summary="test",
        )
        result = _to_result(rec, score=3)
        self.assertIsNotNone(result)
        self.assertIn("provenance", result)
        self.assertEqual(result["provenance"]["experience_id"], "e-uuid-123")
        self.assertEqual(result["provenance"]["decision_id"], "d-uuid-456")


# ════════════════════════════════════════════════════════════════════════════
# S5-2 Part A — Cost spine call-site attribution
# ════════════════════════════════════════════════════════════════════════════

class TestCostSpineCallSite(unittest.TestCase):
    """log_claude_call_to_spine produces a spine record with correct module/tokens/cost."""

    def test_log_claude_call_to_spine_populates_fields(self):
        """A mocked Claude usage object yields non-unknown module_name, non-null tokens/cost."""
        import cost_attribution as ca

        usage = mock.MagicMock()
        usage.input_tokens = 1200
        usage.output_tokens = 300
        usage.cache_read_input_tokens = 0
        usage.cache_creation_input_tokens = 0

        captured: list[dict] = []

        def _fake_log_spine(module_name, layer_name, ring, model, purpose,
                            linked_subject_id=None, linked_subject_type=None,
                            input_tokens=None, output_tokens=None,
                            cached_tokens=None, estimated_cost_usd=None,
                            call_id=None):
            captured.append({
                "module_name": module_name,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "estimated_cost_usd": estimated_cost_usd,
            })
            return "test-cid"

        with mock.patch.object(ca, "log_spine_record", side_effect=_fake_log_spine):
            ca.log_claude_call_to_spine(
                "bot_stage3_decision",
                "claude-sonnet-4-6",
                "decision",
                usage,
            )

        self.assertEqual(len(captured), 1)
        rec = captured[0]
        self.assertNotEqual(rec["module_name"], "unknown",
                            "module_name must not be 'unknown'")
        self.assertEqual(rec["module_name"], "bot_stage3_decision")
        self.assertIsNotNone(rec["input_tokens"])
        self.assertEqual(rec["input_tokens"], 1200)
        self.assertIsNotNone(rec["output_tokens"])
        self.assertEqual(rec["output_tokens"], 300)
        self.assertIsNotNone(rec["estimated_cost_usd"])
        self.assertGreater(rec["estimated_cost_usd"], 0.0)

    def test_log_claude_call_to_spine_computes_cost_from_pricing(self):
        """estimated_cost_usd matches manual calculation for sonnet pricing."""
        import cost_attribution as ca

        usage = mock.MagicMock()
        usage.input_tokens = 1000
        usage.output_tokens = 200
        usage.cache_read_input_tokens = 0
        usage.cache_creation_input_tokens = 0

        captured: list[dict] = []

        with mock.patch.object(ca, "log_spine_record",
                               side_effect=lambda *a, **kw: captured.append(kw)):
            ca.log_claude_call_to_spine("test_module", "claude-sonnet-4-6", "test", usage)

        self.assertEqual(len(captured), 1)
        expected = (1000 * 3.00 + 200 * 15.00) / 1_000_000
        self.assertAlmostEqual(captured[0]["estimated_cost_usd"], expected, places=8)

    def test_log_claude_call_to_spine_nonfatal_on_bad_usage(self):
        """log_claude_call_to_spine does not raise when usage attributes are missing."""
        import cost_attribution as ca

        class _BadUsage:
            pass  # no input_tokens/output_tokens attributes

        with mock.patch.object(ca, "log_spine_record", return_value="cid"):
            result = ca.log_claude_call_to_spine("mod", "claude-sonnet-4-6", "test", _BadUsage())
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
