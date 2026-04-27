"""
tests/test_pdt_removal.py — PDT day-trade count enforcement removal.

Verifies that the 3-day-trade rolling limit is NOT enforced as a prompt
constraint for accounts with equity above the $25K PDT threshold.

The PDT equity FLOOR check (equity < $26K → halt all trading) is a
separate mechanism in risk_kernel.py and is NOT affected by these changes.
It must remain intact. These tests do NOT touch risk_kernel.

What was removed:
  - bot_stage3_decision.build_compact_prompt(): no longer appends
    "PDT: 0 day trades remaining — no new stock/ETF entries" to clines
  - prompts/system_v1.txt: PDT day-trade count is now described as N/A above $25K
  - prompts/user_template_v1.txt: removed the "{pdt_remaining}/3" display
  - prompts/compact_template.txt: removed the PDT remaining display
  - strategy_config.json: max_day_trades_rolling_5day set to 999 (exempt sentinel)

What was NOT changed:
  - risk_kernel.py PDT_FLOOR = 26_000 equity floor (halt if equity < $26K)
  - order_executor.py PDT_FLOOR check (backstop)
  - preflight.py _check_pdt_floor() (equity floor check)
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))
os.chdir(_BOT_DIR)

_THIRD_PARTY_STUBS = {
    "dotenv": None, "anthropic": None,
    "alpaca": None, "alpaca.trading": None, "alpaca.trading.client": None,
    "alpaca.trading.requests": None, "alpaca.trading.enums": None,
    "alpaca.data": None, "alpaca.data.enums": None,
    "alpaca.data.historical": None, "alpaca.data.historical.news": None,
    "alpaca.data.requests": None, "alpaca.data.timeframe": None,
    "pandas": None, "yfinance": None,
}
for _n, _v in _THIRD_PARTY_STUBS.items():
    if _n not in sys.modules:
        _m = mock.MagicMock()
        if _n == "dotenv":
            _m.load_dotenv = mock.MagicMock()
        sys.modules[_n] = _m


def _make_account(equity=102_000.0, cash=20_000.0, buying_power=120_000.0,
                  daytrade_count=4):
    acct = mock.MagicMock()
    acct.equity = equity
    acct.cash = cash
    acct.buying_power = buying_power
    acct.daytrade_count = daytrade_count
    return acct


def _make_md():
    return {
        "vix": 18.0, "vix_regime": "normal",
        "regime_instruction": "Standard rules apply.",
        "market_status": "open", "time_et": "10:00 AM ET",
        "minutes_since_open": 30,
        "sector_table": "", "intermarket_signals": "",
        "global_handoff": "", "earnings_calendar": "",
        "core_by_sector": "", "dynamic_section": "", "intraday_section": "",
        "breaking_news": "", "sector_news": "", "morning_brief_section": "",
        "insider_section": "", "reddit_section": "", "earnings_intel_section": "",
        "economic_calendar_section": "", "macro_wire_section": "",
        "orb_section": "", "crypto_signals": "",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Build 1 — compact prompt no longer adds PDT constraint to clines
# ═══════════════════════════════════════════════════════════════════════════════

class TestCompactPromptNoPDTConstraint(unittest.TestCase):
    """build_compact_prompt must not include PDT day-trade constraint in constraints_block."""

    def _build(self, daytrade_count=4, vix=18.0):
        from bot_stage3_decision import build_compact_prompt
        acct = _make_account(daytrade_count=daytrade_count)
        md   = _make_md()
        md["vix"] = vix
        regime_obj = {"bias": "bullish", "regime_score": 65, "constraints": []}
        signal_obj = {"scored_symbols": {}}
        return build_compact_prompt(
            account=acct,
            positions=[],
            md=md,
            session_tier="market",
            regime_obj=regime_obj,
            signal_scores_obj=signal_obj,
            time_bound_actions=[],
            pi_data={"drawdown_pct": 0.0},
        )

    def test_no_pdt_constraint_when_daytrade_count_exceeds_3(self):
        """Compact prompt must not block entries via 'PDT: 0 day trades remaining'."""
        prompt = self._build(daytrade_count=4)
        self.assertNotIn("PDT: 0 day trades remaining", prompt,
                         "PDT day-trade constraint must not appear in compact prompt for PDT-exempt account")

    def test_no_pdt_constraint_when_daytrade_count_is_3(self):
        """Compact prompt must not block entries even when daytrade_count == 3."""
        prompt = self._build(daytrade_count=3)
        self.assertNotIn("PDT: 0 day trades remaining", prompt)

    def test_no_pdt_day_trade_block_in_any_output(self):
        """The phrase 'no new stock/ETF entries' for PDT must never appear in compact prompt."""
        prompt = self._build(daytrade_count=10)
        self.assertNotIn("no new stock/ETF entries", prompt)

    def test_vix_constraint_still_present_when_high(self):
        """VIX >= 35 constraint must still appear (unrelated to PDT fix)."""
        prompt = self._build(daytrade_count=0, vix=36.0)
        self.assertIn("HALT", prompt)


# ═══════════════════════════════════════════════════════════════════════════════
# Build 2 — system prompt does not restrict entries based on daytrade count
# ═══════════════════════════════════════════════════════════════════════════════

class TestSystemPromptPDTInstruction(unittest.TestCase):
    """system_v1.txt must state PDT day-trade limit is N/A above $25K equity."""

    def test_system_prompt_pdt_exempt_language_present(self):
        """System prompt must explain PDT-exempt status for accounts above $25K."""
        system_txt = (Path(_BOT_DIR) / "prompts" / "system_v1.txt").read_text()
        self.assertIn("PDT-exempt", system_txt,
                      "system_v1.txt must contain 'PDT-exempt' for accounts above $25K")

    def test_system_prompt_no_never_open_if_pdt_zero(self):
        """System prompt must NOT contain the old 'PDT remaining = 0' blocking rule."""
        system_txt = (Path(_BOT_DIR) / "prompts" / "system_v1.txt").read_text()
        self.assertNotIn("if PDT remaining = 0", system_txt,
                         "Old PDT blocking rule must be removed from system prompt")

    def test_pdt_floor_still_in_system_prompt(self):
        """PDT equity floor ($26K halt) must remain in system prompt."""
        system_txt = (Path(_BOT_DIR) / "prompts" / "system_v1.txt").read_text()
        self.assertIn("$26,000", system_txt,
                      "PDT equity floor ($26K halt) must remain in system prompt")


# ═══════════════════════════════════════════════════════════════════════════════
# Build 3 — user_template_v1.txt and compact_template.txt do not show X/3 limit
# ═══════════════════════════════════════════════════════════════════════════════

class TestTemplatesPDTDisplay(unittest.TestCase):
    """Templates must not display 'pdt_remaining/3' which implied a hard cap."""

    def test_user_template_no_pdt_remaining_over_3(self):
        """user_template_v1.txt must not contain 'PDT Remaining' with '/3' cap."""
        tmpl = (Path(_BOT_DIR) / "prompts" / "user_template_v1.txt").read_text()
        self.assertNotIn("PDT Remaining   : {pdt_remaining}/3", tmpl)

    def test_compact_template_no_pdt_remaining_over_3(self):
        """compact_template.txt must not contain 'PDT remaining: {pdt_remaining}/3'."""
        tmpl = (Path(_BOT_DIR) / "prompts" / "compact_template.txt").read_text()
        self.assertNotIn("PDT remaining: {pdt_remaining}/3", tmpl)

    def test_compact_template_still_has_buying_power(self):
        """compact_template.txt must still contain buying power display."""
        tmpl = (Path(_BOT_DIR) / "prompts" / "compact_template.txt").read_text()
        self.assertIn("buying_power", tmpl)


# ═══════════════════════════════════════════════════════════════════════════════
# Build 4 — strategy_config.json uses PDT-exempt sentinel
# ═══════════════════════════════════════════════════════════════════════════════

class TestStrategyConfigPDTExempt(unittest.TestCase):
    """strategy_config.json must use 999 as PDT-exempt sentinel."""

    def test_max_day_trades_rolling_5day_is_999(self):
        """Live config must use PDT-exempt sentinel value 999."""
        cfg_path = Path(_BOT_DIR) / "strategy_config.json"
        if not cfg_path.exists():
            self.skipTest("strategy_config.json not found")
        cfg = json.loads(cfg_path.read_text())
        mdt = cfg.get("parameters", {}).get("max_day_trades_rolling_5day")
        self.assertIsNotNone(mdt, "max_day_trades_rolling_5day must be present")
        self.assertEqual(int(mdt), 999,
                         "PDT-exempt account must use sentinel value 999")


# ═══════════════════════════════════════════════════════════════════════════════
# Build 5 — PDT equity floor in risk_kernel.py is unchanged
# ═══════════════════════════════════════════════════════════════════════════════

class TestRiskKernelPDTFloorIntact(unittest.TestCase):
    """risk_kernel.py PDT_FLOOR must remain at 26_000 — this fix must not touch it."""

    def test_pdt_floor_constant_is_26000(self):
        """PDT_FLOOR must equal 26_000 (equity halt floor — unrelated to day-trade count)."""
        import risk_kernel as rk
        self.assertEqual(rk.PDT_FLOOR, 26_000.0,
                         "PDT equity floor must remain at $26,000")

    def test_eligibility_check_rejects_below_floor(self):
        """risk_kernel.eligibility_check must reject entries when equity < PDT_FLOOR."""
        import risk_kernel as rk
        from schemas import (
            AccountAction,
            BrokerSnapshot,
            Direction,
            Tier,
            TradeIdea,
        )

        low_equity_snapshot = BrokerSnapshot(
            equity=25_000.0,
            cash=25_000.0,
            buying_power=25_000.0,
            positions=[],
            open_orders=[],
        )
        idea = TradeIdea(
            symbol="NVDA",
            action=AccountAction.BUY,
            tier=Tier.CORE,
            conviction=0.80,
            direction=Direction.BULLISH,
            catalyst="earnings beat",
        )
        result = rk.eligibility_check(idea, low_equity_snapshot, {})
        self.assertIsNotNone(result, "Should be rejected — equity below PDT floor")
        self.assertIn("PDT", result)

    def test_eligibility_check_approves_above_floor(self):
        """risk_kernel.eligibility_check must NOT reject entries based on equity being above floor."""
        import risk_kernel as rk
        from schemas import (
            AccountAction,
            BrokerSnapshot,
            Direction,
            Tier,
            TradeIdea,
        )

        high_equity_snapshot = BrokerSnapshot(
            equity=102_000.0,
            cash=20_000.0,
            buying_power=120_000.0,
            positions=[],
            open_orders=[],
        )
        idea = TradeIdea(
            symbol="NVDA",
            action=AccountAction.BUY,
            tier=Tier.CORE,
            conviction=0.80,
            direction=Direction.BULLISH,
            catalyst="strong momentum breakout",
        )
        result = rk.eligibility_check(idea, high_equity_snapshot, {})
        # Should not be rejected for PDT floor reasons
        if result is not None:
            self.assertNotIn("PDT floor", result,
                             "High-equity account must not be rejected by PDT floor check")


if __name__ == "__main__":
    unittest.main()
