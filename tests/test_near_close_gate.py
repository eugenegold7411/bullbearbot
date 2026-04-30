"""
test_near_close_gate.py — Hard kernel gate: block DYNAMIC/INTRADAY buys after 15:55 ET.

NC-01: BUY DYNAMIC at 15:54 ET → allowed
NC-02: BUY DYNAMIC at 15:55 ET → blocked (near_close_gate)
NC-03: BUY DYNAMIC at 15:59 ET → blocked
NC-04: BUY CORE at 15:55 ET   → allowed (CORE exempt)
NC-05: BUY CORE at 15:59 ET   → allowed (CORE exempt)
NC-06: SELL at 15:55 ET        → allowed (exits never blocked)
NC-07: BUY DYNAMIC extended session at 15:55 ET → allowed (only market session)
NC-08: A2 preflight at 15:49 ET → not near_close_gate halted
NC-09: A2 preflight at 15:50 ET → halted with near_close_gate
NC-10: timezone check fails    → allowed (non-fatal, both kernel and A2)

All tests are offline-safe (no Alpaca / Claude / network calls).
"""

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import risk_kernel
from risk_kernel import eligibility_check
from schemas import (
    AccountAction,
    BrokerSnapshot,
    Direction,
    Tier,
    TradeIdea,
)

ET = ZoneInfo("America/New_York")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _et(hour: int, minute: int) -> datetime:
    return datetime(2026, 4, 14, hour, minute, 0, tzinfo=ET)


def _snapshot(equity: float = 100_000.0) -> BrokerSnapshot:
    return BrokerSnapshot(
        positions=[],
        open_orders=[],
        equity=equity,
        cash=equity * 0.8,
        buying_power=equity,
    )


def _idea(
    action: AccountAction = AccountAction.BUY,
    tier: Tier = Tier.DYNAMIC,
    symbol: str = "NVDA",
) -> TradeIdea:
    return TradeIdea(
        symbol=symbol,
        action=action,
        tier=tier,
        conviction=0.80,
        direction=Direction.BULLISH,
        catalyst="momentum_breakout",
        intent="enter_long",
    )


# kernel_config fixture is defined in conftest.py (session-scoped)


# ── Kernel near-close gate (NC-01 to NC-07, NC-10) ───────────────────────────

class TestKernelNearCloseGate:

    def test_nc01_dynamic_before_gate_allowed(self, kernel_config):
        """NC-01: BUY DYNAMIC at 15:54 ET → allowed."""
        with patch.object(risk_kernel, "_get_et_now", return_value=_et(15, 54)):
            result = eligibility_check(
                _idea(tier=Tier.DYNAMIC),
                _snapshot(),
                kernel_config,
                session_tier="market",
                current_time_utc="2026-04-14T19:54:00+00:00",
            )
        assert result is None, f"expected allowed, got: {result}"

    def test_nc02_dynamic_at_gate_blocked(self, kernel_config):
        """NC-02: BUY DYNAMIC at 15:55 ET → blocked."""
        with patch.object(risk_kernel, "_get_et_now", return_value=_et(15, 55)):
            result = eligibility_check(
                _idea(tier=Tier.DYNAMIC),
                _snapshot(),
                kernel_config,
                session_tier="market",
                current_time_utc="2026-04-14T19:55:00+00:00",
            )
        assert result is not None
        assert "near_close_gate" in result
        assert "dynamic" in result

    def test_nc03_dynamic_at_1559_blocked(self, kernel_config):
        """NC-03: BUY DYNAMIC at 15:59 ET → blocked."""
        with patch.object(risk_kernel, "_get_et_now", return_value=_et(15, 59)):
            result = eligibility_check(
                _idea(tier=Tier.DYNAMIC),
                _snapshot(),
                kernel_config,
                session_tier="market",
                current_time_utc="2026-04-14T19:59:00+00:00",
            )
        assert result is not None
        assert "near_close_gate" in result

    def test_nc04_core_at_1555_allowed(self, kernel_config):
        """NC-04: BUY CORE at 15:55 ET → allowed (CORE exempt)."""
        with patch.object(risk_kernel, "_get_et_now", return_value=_et(15, 55)):
            result = eligibility_check(
                _idea(tier=Tier.CORE),
                _snapshot(),
                kernel_config,
                session_tier="market",
                current_time_utc="2026-04-14T19:55:00+00:00",
            )
        assert result is None, f"CORE should be exempt at 15:55, got: {result}"

    def test_nc05_core_at_1559_allowed(self, kernel_config):
        """NC-05: BUY CORE at 15:59 ET → allowed (CORE exempt)."""
        with patch.object(risk_kernel, "_get_et_now", return_value=_et(15, 59)):
            result = eligibility_check(
                _idea(tier=Tier.CORE),
                _snapshot(),
                kernel_config,
                session_tier="market",
                current_time_utc="2026-04-14T19:59:00+00:00",
            )
        assert result is None, f"CORE should be exempt at 15:59, got: {result}"

    def test_nc06_sell_at_1555_allowed(self, kernel_config):
        """NC-06: SELL at 15:55 ET → allowed (exits never blocked)."""
        with patch.object(risk_kernel, "_get_et_now", return_value=_et(15, 55)):
            result = eligibility_check(
                _idea(action=AccountAction.SELL, tier=Tier.DYNAMIC),
                _snapshot(),
                kernel_config,
                session_tier="market",
                current_time_utc="2026-04-14T19:55:00+00:00",
            )
        assert result is None, f"SELL should never be blocked by near_close_gate, got: {result}"

    def test_nc07_dynamic_extended_session_allowed(self, kernel_config):
        """NC-07: BUY DYNAMIC in extended session at 15:55 ET → allowed (market session only)."""
        with patch.object(risk_kernel, "_get_et_now", return_value=_et(15, 55)):
            result = eligibility_check(
                _idea(symbol="BTC/USD", tier=Tier.DYNAMIC),
                _snapshot(),
                kernel_config,
                session_tier="extended",
                current_time_utc="2026-04-14T19:55:00+00:00",
            )
        # Gate only fires in market session; extended session may be rejected
        # for other reasons (stock/ETF gate) but NOT near_close_gate.
        assert result is None or "near_close_gate" not in result

    def test_nc10_kernel_timezone_failure_allows_trade(self, kernel_config):
        """NC-10: timezone check raises → allowed (non-fatal)."""
        with patch.object(risk_kernel, "_get_et_now", side_effect=Exception("tz_unavailable")):
            result = eligibility_check(
                _idea(tier=Tier.DYNAMIC),
                _snapshot(),
                kernel_config,
                session_tier="market",
                current_time_utc="2026-04-14T19:55:00+00:00",
            )
        assert result is None, f"timezone failure should be non-fatal, got: {result}"

    def test_intraday_at_1555_blocked(self, kernel_config):
        """INTRADAY tier also blocked after 15:55 ET (not just DYNAMIC)."""
        with patch.object(risk_kernel, "_get_et_now", return_value=_et(15, 55)):
            result = eligibility_check(
                _idea(tier=Tier.INTRADAY),
                _snapshot(),
                kernel_config,
                session_tier="market",
                current_time_utc="2026-04-14T19:55:00+00:00",
            )
        assert result is not None
        assert "near_close_gate" in result
        assert "intraday" in result


# ── A2 preflight near-close gate (NC-08, NC-09, NC-10 preflight) ─────────────

class TestA2PreflightNearCloseGate:

    def test_nc08_before_gate_not_near_close_halted(self):
        """NC-08: A2 preflight at 15:49 ET → halt_reason is NOT near_close_gate."""
        import bot_options_stage0_preflight as _pf

        with patch.object(_pf, "_get_et_now", return_value=_et(15, 49)):
            # Pass None as alpaca_client — near-close gate doesn't fire,
            # so function proceeds to account fetch which fails with account_fetch_failed,
            # proving the near-close gate did not halt the cycle.
            result = _pf.run_a2_preflight("market", None)

        assert "near_close_gate" not in result.halt_reason

    def test_nc09_at_gate_halted(self):
        """NC-09: A2 preflight at 15:50 ET → halted with near_close_gate."""
        import bot_options_stage0_preflight as _pf

        with patch.object(_pf, "_get_et_now", return_value=_et(15, 50)):
            # Function returns before alpaca is called, so None is safe here.
            result = _pf.run_a2_preflight("market", None)

        assert result.halt is True
        assert "near_close_gate" in result.halt_reason

    def test_nc09_at_1559_also_halted(self):
        """15:59 ET is also within the gate window."""
        import bot_options_stage0_preflight as _pf

        with patch.object(_pf, "_get_et_now", return_value=_et(15, 59)):
            result = _pf.run_a2_preflight("market", None)

        assert result.halt is True
        assert "near_close_gate" in result.halt_reason

    def test_nc10_preflight_timezone_failure_proceeds(self):
        """NC-10: A2 preflight timezone check raises → gate is non-fatal, cycle continues."""
        import bot_options_stage0_preflight as _pf

        with patch.object(_pf, "_get_et_now", side_effect=Exception("tz_unavailable")):
            # Gate silently passes; proceeds to alpaca call which fails → account_fetch_failed.
            result = _pf.run_a2_preflight("market", None)

        assert "near_close_gate" not in result.halt_reason
