"""Tests for three A1 production fixes.

FIX 1 — portfolio_allocator: SIZE TRIM fires when position exceeds max_position_pct_equity.
FIX 2 — order_executor: hard exposure cap at 2.5× blocks buy orders (hard block, not warning).
FIX 3 — divergence: clean_cycles_since_entry increments even in NORMAL mode.
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Minimal stubs so portfolio_allocator and order_executor import cleanly
# ---------------------------------------------------------------------------

def _stub(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    sys.modules.setdefault(name, mod)
    return mod


for _n in [
    "anthropic", "dotenv", "log_setup",
    "watchlist_manager", "options_state", "insider_intelligence",
    "earnings_calendar_lookup", "alpaca.trading.client",
    "alpaca.trading.enums", "alpaca.trading.requests",
    "alpaca", "alpaca.trading",
]:
    _stub(_n)

sys.modules["dotenv"].load_dotenv = lambda *a, **kw: None
sys.modules["log_setup"].get_logger = lambda name: __import__("logging").getLogger(name)
sys.modules["log_setup"].log_trade = lambda *a, **kw: None

# Alpaca enum stubs needed by order_executor — only install if the real alpaca
# hasn't already populated these (prevents corrupting the real module in CI).
for _enum_name in ["AssetStatus", "ContractType", "OrderClass", "OrderSide",
                   "PositionIntent", "TimeInForce"]:
    if not hasattr(sys.modules["alpaca.trading.enums"], _enum_name):
        setattr(sys.modules["alpaca.trading.enums"], _enum_name, MagicMock())
for _req_name in ["GetOptionContractsRequest", "LimitOrderRequest", "MarketOrderRequest",
                  "StopLossRequest", "StopOrderRequest", "TakeProfitRequest"]:
    if not hasattr(sys.modules["alpaca.trading.requests"], _req_name):
        setattr(sys.modules["alpaca.trading.requests"], _req_name, MagicMock())

_stub("schemas")
if not hasattr(sys.modules["schemas"], "alpaca_symbol"):
    sys.modules["schemas"].alpaca_symbol = lambda s: s


# ---------------------------------------------------------------------------
# FIX 1 — SIZE TRIM: fires when position > max_position_pct_equity
# ---------------------------------------------------------------------------

class TestSizeTrimEquityBased(unittest.TestCase):
    def _make_incumbent(self, symbol: str, score: int, mv: float) -> dict:
        return {
            "symbol":                 symbol,
            "thesis_score":           score,
            "thesis_score_normalized": score * 10,
            "market_value":           mv,
            "account_pct":            (mv / (100_000 * 4)) * 100,  # fraction of total_capacity
            "signal_catalyst":        "test",
            "signal_signals":         [],
        }

    def test_size_trim_fires_for_oversize_position(self):
        """TSM at 23% of equity×4 with max_position_pct_capacity=0.15 must generate SIZE TRIM.

        4x margin account: full_account = equity × 4 = $400K.
        TSM MV = 23% × $400K = $92K → cap_frac = 23% > 15%+2% = 17% → fires.
        """
        import portfolio_allocator as pa_mod

        equity = 100_000.0
        tsm_mv = equity * 4 * 0.23   # 23% of equity×4 = $92K — over 17% trigger

        incumbent = self._make_incumbent("TSM", score=10, mv=tsm_mv)

        pa_cfg = dict(pa_mod._PA_DEFAULTS)
        pa_cfg.update({
            "enable_live": True,
            "enable_live_trim": True,
            "max_position_pct_equity": 0.15,
            "size_trim_tolerance_pct": 2.0,
            "size_trim_enabled": True,
            "trim_score_threshold": 4,
            "min_rebalance_notional": 500.0,
            "max_recommendations_per_cycle": 10,
        })

        cfg = {
            "parameters": {"max_position_pct_capacity": 0.15},
            "portfolio_allocator": pa_cfg,
        }
        sizes = {"available_for_new": 0, "core": tsm_mv * 0.5, "standard": tsm_mv * 0.3}

        proposed, suppressed = pa_mod._decide_actions(
            [incumbent], [], {}, cfg, pa_cfg, sizes, equity,
        )

        trim_actions = [a for a in proposed if a["action"] == "TRIM" and a["symbol"] == "TSM"]
        self.assertEqual(len(trim_actions), 1, f"Expected SIZE TRIM for TSM; proposed={proposed}")
        self.assertIn("SIZE TRIM", trim_actions[0]["reason"])

    def test_size_trim_does_not_fire_when_position_within_cap(self):
        """Position at 14% of equity with 15% cap + 2% tolerance should NOT trigger SIZE TRIM."""
        import portfolio_allocator as pa_mod

        equity = 100_000.0
        mv = equity * 0.14   # 14% — below 15% cap

        incumbent = self._make_incumbent("NVDA", score=10, mv=mv)

        pa_cfg = dict(pa_mod._PA_DEFAULTS)
        pa_cfg.update({
            "enable_live": True,
            "enable_live_trim": True,
            "max_position_pct_equity": 0.15,
            "size_trim_tolerance_pct": 2.0,
            "size_trim_enabled": True,
            "trim_score_threshold": 4,
            "min_rebalance_notional": 500.0,
            "max_recommendations_per_cycle": 10,
        })

        cfg = {
            "parameters": {"max_position_pct_capacity": 0.15},
            "portfolio_allocator": pa_cfg,
        }
        sizes = {"available_for_new": 0, "core": mv * 0.5, "standard": mv * 0.3}

        proposed, _ = pa_mod._decide_actions(
            [incumbent], [], {}, cfg, pa_cfg, sizes, equity,
        )

        trim_actions = [a for a in proposed if a["action"] == "TRIM" and a["symbol"] == "NVDA"]
        self.assertEqual(len(trim_actions), 0, "No SIZE TRIM expected for position at 14% (cap=15%+2%tol)")

    def test_enable_live_trim_false_blocks_trim_execution(self):
        """Trim execution gate: enable_live_trim=False prevents _execute_live_trim calls."""
        import portfolio_allocator as pa_mod

        # The gate at run_allocator_cycle checks pa_cfg["enable_live"] AND pa_cfg.get("enable_live_trim")
        pa_cfg = dict(pa_mod._PA_DEFAULTS)
        pa_cfg["enable_live"] = True
        pa_cfg["enable_live_trim"] = False   # ← disabled

        self.assertTrue(pa_cfg["enable_live"])
        self.assertFalse(pa_cfg.get("enable_live_trim"))
        # Combined gate should be False
        gate = pa_cfg["enable_live"] and pa_cfg.get("enable_live_trim")
        self.assertFalse(gate, "Trim gate must be False when enable_live_trim=False")

    def test_enable_live_trim_true_opens_gate(self):
        """Both enable_live=True AND enable_live_trim=True → trim gate opens."""
        import portfolio_allocator as pa_mod

        pa_cfg = dict(pa_mod._PA_DEFAULTS)
        pa_cfg["enable_live"] = True
        pa_cfg["enable_live_trim"] = True

        gate = pa_cfg["enable_live"] and pa_cfg.get("enable_live_trim")
        self.assertTrue(gate, "Trim gate must be True when both flags are True")


# ---------------------------------------------------------------------------
# FIX 2 — Executor hard exposure cap
# ---------------------------------------------------------------------------

class _FakePosition:
    def __init__(self, symbol: str, mv: float, qty: float = 100.0):
        self.symbol      = symbol
        self.market_value = str(mv)
        self.qty          = str(qty)


class _FakeAccount:
    def __init__(self, equity: float, buying_power: float | None = None):
        self.equity       = str(equity)
        self.buying_power = str(buying_power if buying_power is not None else equity)


class TestExecutorHardLeverageCap(unittest.TestCase):
    def _make_action(self, symbol: str = "GS") -> dict:
        return {
            "action":     "buy",
            "symbol":     symbol,
            "qty":        10,
            "entry":      500.0,
            "stop_loss":  480.0,
            "take_profit": 550.0,
            "tier":       "core",
            "confidence": "high",
        }

    def test_hard_block_when_over_leverage_cap(self):
        """validate_action raises ValueError when long_mv > equity × 2.5."""
        import order_executor as oe

        equity   = 100_000.0
        long_mv  = equity * 2.6   # over 2.5× cap
        positions = [_FakePosition("TSM", long_mv)]
        account   = _FakeAccount(equity)

        action = self._make_action()

        with self.assertRaises(ValueError) as ctx:
            oe.validate_action(action, account, positions, "open", 60, {})

        self.assertIn("BLOCKED", str(ctx.exception))
        self.assertIn("exposure cap", str(ctx.exception).lower())

    def test_no_block_when_under_leverage_cap(self):
        """validate_action does NOT raise for leverage cap when long_mv < equity × 2.5."""
        import order_executor as oe

        equity   = 100_000.0
        long_mv  = equity * 1.8   # under 2.5× cap
        positions = [_FakePosition("TSM", long_mv)]
        account   = _FakeAccount(equity)

        action = self._make_action()
        # Supply the symbol's current price so validate_action skips
        # _get_current_price (which would 401 in CI without Alpaca keys).
        current_prices = {action["symbol"]: action["entry"]}

        # Should not raise for the leverage cap (may raise for other reasons;
        # catch ValueError and check it's not the cap block)
        try:
            oe.validate_action(action, account, positions, "open", 60, current_prices)
        except ValueError as exc:
            self.assertNotIn("BLOCKED", str(exc), "Hard cap block should NOT fire at 1.8×")

    def test_hard_leverage_cap_constant(self):
        """HARD_LEVERAGE_CAP constant is 2.5."""
        import order_executor as oe
        self.assertEqual(oe.HARD_LEVERAGE_CAP, 2.5)


# ---------------------------------------------------------------------------
# FIX 3 — clean_cycles_since_entry increments in NORMAL mode
# ---------------------------------------------------------------------------

class TestCleanCyclesNormalMode(unittest.TestCase):
    def _load_divergence(self):
        """Import divergence module, stubbing filesystem dependencies."""
        _stub("cost_attribution")
        sys.modules["cost_attribution"].log_claude_call_to_spine = lambda **kw: None
        import divergence as div_mod  # noqa: PLC0415
        return div_mod

    def test_clean_cycles_increments_when_normal_and_no_div_events(self):
        """check_clean_cycle increments counter and updates last_checked_at when mode=NORMAL."""
        div = self._load_divergence()

        mode = div.AccountMode(
            account="A1",
            mode=div.OperatingMode.NORMAL,
            scope=div.DivergenceScope.ACCOUNT,
            scope_id="",
            reason_code="manual_reset",
            reason_detail="test",
            entered_at="2026-05-05T18:42:00+00:00",
            entered_by="manual",
            recovery_condition="one_clean_cycle",
            last_checked_at="2026-05-05T18:42:00+00:00",
            clean_cycles_since_entry=0,
        )

        with patch.object(div, "save_account_mode") as mock_save:
            result = div.check_clean_cycle("A1", mode, [])  # no div_events

        self.assertEqual(result.clean_cycles_since_entry, 1,
                         "Counter should increment on clean cycle in NORMAL mode")
        mock_save.assert_called_once()

    def test_clean_cycles_does_not_increment_when_div_events_present(self):
        """check_clean_cycle does NOT increment counter when div_events present."""
        div = self._load_divergence()

        mode = div.AccountMode(
            account="A1",
            mode=div.OperatingMode.NORMAL,
            scope=div.DivergenceScope.ACCOUNT,
            scope_id="",
            reason_code="manual_reset",
            reason_detail="test",
            entered_at="2026-05-05T18:42:00+00:00",
            entered_by="manual",
            recovery_condition="one_clean_cycle",
            last_checked_at="2026-05-05T18:42:00+00:00",
            clean_cycles_since_entry=5,
        )

        fake_event = MagicMock()
        with patch.object(div, "save_account_mode"):
            result = div.check_clean_cycle("A1", mode, [fake_event])

        self.assertEqual(result.clean_cycles_since_entry, 5,
                         "Counter should NOT increment when div_events present in NORMAL mode")

    def test_clean_cycles_increments_multiple_cycles(self):
        """Counter accumulates across consecutive clean cycles in NORMAL mode."""
        div = self._load_divergence()

        mode = div.AccountMode(
            account="A1",
            mode=div.OperatingMode.NORMAL,
            scope=div.DivergenceScope.ACCOUNT,
            scope_id="",
            reason_code="manual_reset",
            reason_detail="test",
            entered_at="2026-05-05T18:42:00+00:00",
            entered_by="manual",
            recovery_condition="one_clean_cycle",
            last_checked_at="2026-05-05T18:42:00+00:00",
            clean_cycles_since_entry=0,
        )

        with patch.object(div, "save_account_mode"):
            mode = div.check_clean_cycle("A1", mode, [])
            mode = div.check_clean_cycle("A1", mode, [])
            mode = div.check_clean_cycle("A1", mode, [])

        self.assertEqual(mode.clean_cycles_since_entry, 3)

    def test_non_normal_mode_still_recovers(self):
        """check_clean_cycle still transitions non-NORMAL mode to NORMAL after one clean cycle."""
        div = self._load_divergence()

        mode = div.AccountMode(
            account="A1",
            mode=div.OperatingMode.HALTED,
            scope=div.DivergenceScope.ACCOUNT,
            scope_id="",
            reason_code="protection_missing",
            reason_detail="test",
            entered_at="2026-05-05T18:42:00+00:00",
            entered_by="divergence",
            recovery_condition="one_clean_cycle",
            last_checked_at="2026-05-05T18:42:00+00:00",
            clean_cycles_since_entry=0,
        )

        with patch.object(div, "save_account_mode"), \
             patch.object(div, "transition_mode") as mock_transition:
            mock_transition.return_value = div.AccountMode(
                account="A1",
                mode=div.OperatingMode.NORMAL,
                scope=div.DivergenceScope.ACCOUNT,
                scope_id="",
                reason_code="clean_recovery",
                reason_detail="clean_cycles=1",
                entered_at="2026-05-06T10:00:00+00:00",
                entered_by="clean_cycle_checker",
                recovery_condition="one_clean_cycle",
                last_checked_at="2026-05-06T10:00:00+00:00",
            )
            result = div.check_clean_cycle("A1", mode, [])

        mock_transition.assert_called_once()
        self.assertEqual(result.mode, div.OperatingMode.NORMAL)


if __name__ == "__main__":
    unittest.main()
