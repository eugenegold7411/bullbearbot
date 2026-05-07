"""
test_structure_upgrade.py — Tests for _evaluate_structure_upgrade.

Covers:
1. Profitable single_call with matching debate → upgrade action returned
2. Losing position → blocked
3. Short DTE → blocked
4. Feature flag disabled → blocked (no action)
5. Already a spread → blocked
6. Frequency cap not yet expired → blocked
7. Frequency cap expired → allowed
8. Log line emitted when candidate identified
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_structure(
    *,
    underlying:              str = "AAPL",
    strategy:                str = "single_call",
    pnl_unrealized:          float | None = 150.0,
    expiration:              str | None = None,
    long_strike:             float = 180.0,
    contracts:               int = 1,
    last_upgrade_attempted:  str | None = None,
):
    from schemas import (
        OptionsLeg,
        OptionsStructure,
        OptionStrategy,
        StructureLifecycle,
        Tier,
    )
    if expiration is None:
        expiration = (date.today() + timedelta(days=20)).isoformat()

    strat_map = {
        "single_call":       OptionStrategy.SINGLE_CALL,
        "single_put":        OptionStrategy.SINGLE_PUT,
        "call_debit_spread": OptionStrategy.CALL_DEBIT_SPREAD,
        "put_debit_spread":  OptionStrategy.PUT_DEBIT_SPREAD,
    }
    opt_type = "call" if "call" in strategy else "put"
    leg = OptionsLeg(
        occ_symbol=f"{underlying}260620{'C' if opt_type=='call' else 'P'}{int(long_strike*1000):08d}",
        underlying=underlying,
        side="buy",
        qty=1,
        option_type=opt_type,
        strike=long_strike,
        expiration=expiration,
        filled_price=2.00,
    )
    return OptionsStructure(
        structure_id=f"test-{underlying}-upgrade",
        underlying=underlying,
        strategy=strat_map.get(strategy, OptionStrategy.SINGLE_CALL),
        lifecycle=StructureLifecycle.FULLY_FILLED,
        legs=[leg],
        contracts=contracts,
        max_cost_usd=200.0,
        opened_at=(datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),
        catalyst="test catalyst",
        tier=Tier.CORE,
        long_strike=long_strike,
        expiration=expiration,
        pnl_unrealized=pnl_unrealized,
        last_upgrade_attempted=last_upgrade_attempted,
    )


def _call_spread_debate() -> dict:
    return {"structure_type": "debit_call_spread", "symbol": "SPY", "contracts": 5}


def _put_spread_debate() -> dict:
    return {"structure_type": "debit_put_spread", "symbol": "SPY", "contracts": 5}


def _enabled_config() -> dict:
    return {"structure_upgrade_enabled": True}


def _disabled_config() -> dict:
    return {"structure_upgrade_enabled": False}


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_upgrade_fires_for_profitable_single_call():
    """Profitable long_call + debate recommends spread + all conditions met → upgrade action."""
    from bot_options_stage4_execution import _evaluate_structure_upgrade

    struct = _make_structure(strategy="single_call", pnl_unrealized=200.0)
    result = _evaluate_structure_upgrade(struct, _call_spread_debate(), _enabled_config())

    assert result is not None
    assert result["action"] == "add_hedge_leg"
    assert result["old_strategy"] == "single_call"
    assert result["new_strategy"] == "call_debit_spread"
    assert result["qty"] == 1
    assert "AAPL" in result["symbol"]
    assert "C" in result["symbol"]


def test_upgrade_blocked_for_losing_position():
    """pnl_unrealized < 0 → no upgrade (condition 3)."""
    from bot_options_stage4_execution import _evaluate_structure_upgrade

    struct = _make_structure(strategy="single_call", pnl_unrealized=-50.0)
    result = _evaluate_structure_upgrade(struct, _call_spread_debate(), _enabled_config())

    assert result is None


def test_upgrade_blocked_for_short_dte():
    """DTE ≤ 7 → no upgrade (condition 4)."""
    from bot_options_stage4_execution import _evaluate_structure_upgrade

    exp = (date.today() + timedelta(days=5)).isoformat()
    struct = _make_structure(strategy="single_call", pnl_unrealized=100.0, expiration=exp)
    result = _evaluate_structure_upgrade(struct, _call_spread_debate(), _enabled_config())

    assert result is None


def test_upgrade_blocked_when_disabled():
    """structure_upgrade_enabled=False → no upgrade returned (condition 6)."""
    from bot_options_stage4_execution import _evaluate_structure_upgrade

    struct = _make_structure(strategy="single_call", pnl_unrealized=100.0)
    result = _evaluate_structure_upgrade(struct, _call_spread_debate(), _disabled_config())

    assert result is None


def test_upgrade_blocked_for_spread():
    """structure_type already a spread → no upgrade (condition 1)."""
    from bot_options_stage4_execution import _evaluate_structure_upgrade

    struct = _make_structure(strategy="call_debit_spread", pnl_unrealized=100.0)
    result = _evaluate_structure_upgrade(struct, _call_spread_debate(), _enabled_config())

    assert result is None


def test_upgrade_frequency_cap_active():
    """last_upgrade_attempted 3 days ago → blocked (condition 5: < 7 days)."""
    from bot_options_stage4_execution import _evaluate_structure_upgrade

    last_ts = (date.today() - timedelta(days=3)).isoformat()
    struct = _make_structure(strategy="single_call", pnl_unrealized=100.0,
                             last_upgrade_attempted=last_ts)
    result = _evaluate_structure_upgrade(struct, _call_spread_debate(), _enabled_config())

    assert result is None


def test_upgrade_frequency_cap_expired():
    """last_upgrade_attempted 8 days ago → allowed (condition 5: ≥ 7 days)."""
    from bot_options_stage4_execution import _evaluate_structure_upgrade

    last_ts = (date.today() - timedelta(days=8)).isoformat()
    struct = _make_structure(strategy="single_call", pnl_unrealized=100.0,
                             last_upgrade_attempted=last_ts)
    result = _evaluate_structure_upgrade(struct, _call_spread_debate(), _enabled_config())

    assert result is not None
    assert result["action"] == "add_hedge_leg"


def test_upgrade_log_line():
    """When upgrade candidate is identified, [UPGRADE] log line is emitted."""
    from bot_options_stage4_execution import _evaluate_structure_upgrade

    struct = _make_structure(strategy="single_call", pnl_unrealized=150.0)

    with patch("bot_options_stage4_execution.log") as mock_log:
        _evaluate_structure_upgrade(struct, _call_spread_debate(), _enabled_config())

    logged_msgs = [str(call) for call in mock_log.info.call_args_list]
    assert any("[UPGRADE]" in msg for msg in logged_msgs), (
        f"Expected [UPGRADE] log line, got: {logged_msgs}"
    )
