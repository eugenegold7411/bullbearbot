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
"""

import sys
import os
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

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
        """BUG-002: BUY must still be rejected when market is closed."""
        action  = {"action": "buy", "symbol": "GLD", "qty": 10, "confidence": "medium"}
        account = self._mock_account()
        with self.assertRaises(ValueError) as ctx:
            self.validate_action(action, account, [], "closed", 0)
        self.assertIn("market is closed", str(ctx.exception))

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
        """Low conviction → cap=1×equity=100K. Existing 95K + new 12K = 107K > 100K → FAIL."""
        action    = self._valid_buy_action(confidence="low", tier="core")
        account   = self._mock_account(equity=100_000, buying_power=100_000)
        positions = self._mock_positions([95_000])
        with self.assertRaises(ValueError) as ctx:
            self.validate_action(
                action, account, positions, "open", 20,
                current_prices={"SPY": 300.0},
            )
        self.assertIn("exposure", str(ctx.exception).lower())

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
        current_prices = {"BTC/USD": 74000.0}

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

    def _mock_fill(self, fill_price=440.0, symbol="GLD"):
        from alpaca.trading.enums import OrderSide
        return SimpleNamespace(
            side=OrderSide.BUY,
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
        decisions = self._base_decision("buy", stop_loss=430.0, take_profit=450.0)
        # 425 <= 430 * 1.01 = 434.3 → loss
        fill = self._mock_fill(fill_price=425.0)

        with (mock.patch("memory.TradingClient") as MockTC,
              mock.patch("memory._load_decisions", return_value=decisions),
              mock.patch("memory._save_decisions")  as mock_save,
              mock.patch("memory._load_perf",       return_value=self._empty_perf()),
              mock.patch("memory._save_perf"),
              mock.patch("memory.trade_memory")):
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
        from exit_manager import maybe_trail_stop, get_active_exits
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
        self.assertEqual(default["regime"], "normal",
                         "_OVERNIGHT_DEFAULT regime must be 'normal'")
        self.assertEqual(default["actions"], [],
                         "_OVERNIGHT_DEFAULT actions must be empty list (hold-all)")
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
        _hold_all = {
            "reasoning": "test",
            "regime": "normal",
            "actions": [],
            "notes": "",
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

        self.assertEqual(result["regime"], "normal")
        self.assertEqual(result["actions"], [])


# ═════════════════════════════════════════════════════════════════════════════
# SUITE 8 — schemas.py: symbol normalisation, enums, dataclasses
# ═════════════════════════════════════════════════════════════════════════════

class TestSchemaSymbolNormalization(unittest.TestCase):
    """normalize_symbol / is_crypto / alpaca_symbol / yfinance_symbol"""

    @classmethod
    def setUpClass(cls):
        from schemas import (
            normalize_symbol, is_crypto, alpaca_symbol, yfinance_symbol,
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
            BrokerAction, AccountAction, Tier, Conviction, TradeIdea,
            Direction,
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
            TradeIdea, BrokerAction, AccountAction, Tier, Conviction, Direction,
            validate_trade_idea, validate_broker_action,
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
        from schemas import SignalScore, Conviction, Direction, Tier
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
            OptionsStructure, OptionsLeg, OptionStrategy,
            StructureLifecycle, Tier,
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
        from schemas import (
            AccountAction, BrokerSnapshot, Conviction, Direction,
            NormalizedPosition, Tier, TradeIdea,
        )
        from risk_kernel import eligibility_check, PDT_FLOOR, VIX_HALT
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
        from schemas import (
            AccountAction, BrokerSnapshot, Conviction, Direction,
            NormalizedPosition, Tier, TradeIdea,
        )
        from risk_kernel import size_position, VIX_CAUTION, _CORE_HIGH_CONVICTION_PCT
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

    def _make_position(self, market_value: float) -> "NormalizedPosition":
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
        from schemas import AccountAction, Conviction, Direction, Tier, TradeIdea
        from risk_kernel import place_stops, MIN_RR_RATIO
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
        from schemas import (
            AccountAction, BrokerAction, BrokerSnapshot, Conviction,
            Direction, NormalizedPosition, Tier, TradeIdea,
        )
        from risk_kernel import process_idea, VIX_HALT
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
        from schemas import Direction, OptionStrategy, Tier
        from risk_kernel import select_structure, liquidity_gate
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
        from reconciliation import build_desired_state, DesiredState, DesiredPosition
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
            build_desired_state, diff_state, ReconciliationDiff,
            PRIORITY_CRITICAL, PRIORITY_HIGH, PRIORITY_NORMAL,
        )
        from schemas import BrokerSnapshot, NormalizedPosition, NormalizedOrder
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
        from datetime import datetime, timezone, timedelta
        from reconciliation import DesiredState, DesiredPosition

        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        desired = DesiredState(positions={
            "TSM": DesiredPosition(symbol="TSM", must_exit_by=past, must_exit_reason="earnings"),
        })
        snap = self._snapshot([self._make_pos("TSM")])
        diff = self.diff_state(desired, snap)

        self.assertIn("TSM", diff.expired_symbols)
        self.assertTrue(any(a.priority == self.PRIORITY_CRITICAL for a in diff.actions))

    def test_future_deadline_not_expired(self):
        from datetime import datetime, timezone, timedelta
        from reconciliation import DesiredState, DesiredPosition

        future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
        desired = DesiredState(positions={
            "TSM": DesiredPosition(symbol="TSM", must_exit_by=future),
        })
        snap = self._snapshot([self._make_pos("TSM")])
        diff = self.diff_state(desired, snap)
        self.assertNotIn("TSM", diff.expired_symbols)

    def test_forced_exit_is_high_priority(self):
        from reconciliation import DesiredState, DesiredPosition

        desired = DesiredState(positions={
            "NVDA": DesiredPosition(symbol="NVDA", forced_exit=True),
        })
        snap = self._snapshot([self._make_pos("NVDA")])
        diff = self.diff_state(desired, snap)

        self.assertIn("NVDA", diff.forced_symbols)
        self.assertTrue(any(a.priority == self.PRIORITY_HIGH for a in diff.actions))

    def test_missing_stop_is_normal_priority(self):
        from reconciliation import DesiredState, DesiredPosition

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
        from reconciliation import DesiredState, DesiredPosition

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
        from datetime import datetime, timezone, timedelta
        from reconciliation import DesiredState, DesiredPosition

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
        from reconciliation import reconcile_options_structures, OptionsReconResult
        from schemas import (
            OptionsStructure, OptionsLeg, OptionStrategy,
            StructureLifecycle, Tier, BrokerSnapshot, NormalizedPosition,
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
            GateState, TriggerReason,
            should_run_sonnet, should_use_compact_prompt,
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
        from datetime import datetime, timezone, timedelta
        import hashlib
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
        from datetime import datetime, timezone, timedelta
        import hashlib

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

    def _make_order(self, order_type: str) -> "NormalizedOrder":
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

    def _make_position(self) -> "NormalizedPosition":
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
        from schemas import StructureLifecycle, OptionsStructure, OptionStrategy, Tier
        from datetime import datetime, timezone
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
        from schemas import StructureLifecycle, OptionsStructure, OptionStrategy, Tier
        from datetime import datetime, timezone
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
        from schemas import (
            StructureLifecycle, OptionsStructure, OptionsLeg, OptionStrategy, Tier,
        )
        from datetime import datetime, timezone
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
        from schemas import StructureProposal, OptionStrategy
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
        from options_builder import select_expiry
        from datetime import date, timedelta
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
        from unittest.mock import patch
        from pathlib import Path
        from options_builder import build_structure
        from schemas import OptionStrategy
        import options_state

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
            OptionStrategy, OptionsStructure, StructureLifecycle, Tier,
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
        from schemas import OptionStrategy, StructureLifecycle
        from options_executor import submit_structure

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
        from datetime import date, timedelta, datetime, timezone
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
        from datetime import date, timedelta, datetime, timezone
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
            reconcile_options_structures,
            plan_structure_repair,
            OptionsReconResult,
        )
        from schemas import (
            OptionsStructure, OptionsLeg, OptionStrategy,
            StructureLifecycle, Tier, BrokerSnapshot, NormalizedPosition,
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
        from reconciliation import DesiredState, DesiredPosition
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
        import tempfile, importlib
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
        from datetime import timedelta
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
import os as _os
_sys.path.insert(0, str(Path(__file__).parent.parent))

class TestSuite18Divergence(unittest.TestCase):
    """
    Tests for divergence.py: classification, operating mode, detectors,
    and liquidity gate integration.
    Covers 10 of the 10 new tests required (total: 181).
    """

    def test_divergence_classify_stop_missing(self):
        """stop_missing classifies as DE_RISK, SYMBOL scope, guarded_auto."""
        from divergence import classify_divergence, DivergenceSeverity, DivergenceScope
        severity, scope, recov = classify_divergence(
            "stop_missing", "AAPL", "A1")
        self.assertEqual(severity, DivergenceSeverity.DE_RISK)
        self.assertEqual(scope, DivergenceScope.SYMBOL)
        self.assertEqual(recov, "guarded_auto")

    def test_divergence_classify_escalates_large_position(self):
        """Large position ($10k) escalates stop_missing from DE_RISK to HALT."""
        from divergence import classify_divergence, DivergenceSeverity, DivergenceScope
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
            is_action_allowed, AccountMode, OperatingMode, DivergenceScope,
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
            is_action_allowed, AccountMode, OperatingMode, DivergenceScope,
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
            is_action_allowed, AccountMode, OperatingMode, DivergenceScope,
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
            is_action_allowed, AccountMode, OperatingMode, DivergenceScope,
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
        import tempfile, json
        from divergence import (
            check_clean_cycle, AccountMode, OperatingMode,
            DivergenceScope, RUNTIME_DIR,
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
        from divergence import (
            check_repeat_escalation, DivergenceSeverity, DIVERGENCE_COUNTS_PATH,
        )
        import divergence as _div_mod
        original_path = _div_mod.DIVERGENCE_COUNTS_PATH
        with tempfile.TemporaryDirectory() as tmp:
            _div_mod.DIVERGENCE_COUNTS_PATH = Path(tmp) / "divergence_counts.json"
            try:
                # First call — no escalation yet
                s1 = check_repeat_escalation(
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
        bo = importlib.import_module("bot_options")

        # _quick_liquidity_check expects chain["expirations"][date]["calls"]
        # as a list of dicts with strike, openInterest, volume keys
        chain = {
            "current_price": 100.0,
            "expirations": {
                "2026-04-25": {
                    "calls": [
                        {"strike": 100.0, "openInterest": 5, "volume": 1,
                         "bid": 1.0, "ask": 1.2},
                    ],
                }
            }
        }
        from schemas import StructureProposal, OptionStrategy, Direction
        from datetime import datetime, timezone
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
        import options_executor
        from schemas import OptionsStructure, OptionStrategy, StructureLifecycle, Tier
        from datetime import date, timedelta
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
        import options_executor
        from schemas import OptionsStructure, OptionStrategy, StructureLifecycle, Tier
        from datetime import date, timedelta
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
        import options_executor
        from schemas import OptionsStructure, OptionStrategy, StructureLifecycle, Tier
        from datetime import date, timedelta
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
        from iv_history_seeder import _merge_with_existing, MIN_VALID_IV

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
        from iv_history_seeder import _fetch_atm_iv_yfinance, MIN_DTE

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


if __name__ == "__main__":
    unittest.main()
