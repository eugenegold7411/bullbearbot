"""
test_sprint1_stabilization.py — Sprint 1 stabilization tests.

Covers items 2, 3, 4, 5, and 7 from the Sprint 1 pre-trading stabilization plan.
All tests are offline-safe unless marked otherwise.
"""

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))


# ─────────────────────────────────────────────────────────────────────────────
# Item 2 — divergence.load_account_mode() enum case normalization
# ─────────────────────────────────────────────────────────────────────────────

def test_load_account_mode_handles_uppercase_stored_value(tmp_path, monkeypatch):
    """load_account_mode must handle 'NORMAL' stored as uppercase."""
    from divergence import OperatingMode, load_account_mode
    mode_file = tmp_path / "a1_mode.json"
    mode_file.write_text(json.dumps({
        "account": "A1",
        "mode": "NORMAL",
        "scope": "account",
        "scope_id": "",
        "reason_code": "",
        "reason_detail": "",
        "entered_at": "",
        "entered_by": "system",
        "recovery_condition": "one_clean_cycle",
        "last_checked_at": "",
    }))
    monkeypatch.setattr("divergence.RUNTIME_DIR", tmp_path)
    result = load_account_mode("A1")
    assert result.mode == OperatingMode.NORMAL


def test_load_account_mode_handles_lowercase_stored_value(tmp_path, monkeypatch):
    """load_account_mode must handle 'normal' stored as lowercase."""
    from divergence import OperatingMode, load_account_mode
    mode_file = tmp_path / "a1_mode.json"
    mode_file.write_text(json.dumps({
        "account": "A1",
        "mode": "normal",
        "scope": "account",
        "scope_id": "",
        "reason_code": "",
        "reason_detail": "",
        "entered_at": "",
        "entered_by": "system",
        "recovery_condition": "one_clean_cycle",
        "last_checked_at": "",
    }))
    monkeypatch.setattr("divergence.RUNTIME_DIR", tmp_path)
    result = load_account_mode("A1")
    assert result.mode == OperatingMode.NORMAL


def test_load_account_mode_handles_uppercase_scope(tmp_path, monkeypatch):
    """load_account_mode must handle uppercase 'ACCOUNT' in scope field."""
    from divergence import DivergenceScope, load_account_mode
    mode_file = tmp_path / "a1_mode.json"
    mode_file.write_text(json.dumps({
        "account": "A1",
        "mode": "normal",
        "scope": "ACCOUNT",
        "scope_id": "",
        "reason_code": "",
        "reason_detail": "",
        "entered_at": "",
        "entered_by": "system",
        "recovery_condition": "one_clean_cycle",
        "last_checked_at": "",
    }))
    monkeypatch.setattr("divergence.RUNTIME_DIR", tmp_path)
    result = load_account_mode("A1")
    assert result.scope == DivergenceScope.ACCOUNT


# ─────────────────────────────────────────────────────────────────────────────
# Item 3 — risk_kernel.size_position() single-name cap
# ─────────────────────────────────────────────────────────────────────────────

from risk_kernel import size_position
from schemas import (
    AccountAction,
    BrokerSnapshot,
    Direction,
    NormalizedPosition,
    Tier,
    TradeIdea,
)


def _snapshot(equity: float = 100_000.0, extra_exposure: float = 0.0) -> BrokerSnapshot:
    positions = []
    if extra_exposure > 0:
        positions.append(NormalizedPosition(
            symbol="SPY", alpaca_sym="SPY",
            qty=extra_exposure / 100.0,
            avg_entry_price=100.0, current_price=100.0,
            market_value=extra_exposure,
            unrealized_pl=0.0, unrealized_plpc=0.0,
            is_crypto_pos=False,
        ))
    return BrokerSnapshot(
        positions=positions,
        open_orders=[],
        equity=equity,
        cash=equity - extra_exposure,
        buying_power=equity,
    )


def _idea(
    symbol: str = "AAPL",
    tier: Tier = Tier.CORE,
    conviction: float = 0.60,
) -> TradeIdea:
    return TradeIdea(
        symbol=symbol,
        action=AccountAction.BUY,
        tier=tier,
        conviction=conviction,
        direction=Direction.BULLISH,
        catalyst="technical_breakout",
        intent="enter_long",
    )


def _cap_config(max_pct: float = 0.07) -> dict:
    """Config with max_position_pct_capacity set."""
    return {
        "parameters": {
            "max_positions": 15,
            "stop_loss_pct_core": 0.035,
            "stop_loss_pct_intraday": 0.018,
            "take_profit_multiple": 2.5,
            "catalyst_tag_required_for_entry": False,
            "session_gate_enforce": False,
            "max_position_pct_capacity": max_pct,
        },
        "position_sizing": {
            "core_tier_pct": 0.15,
            "dynamic_tier_pct": 0.08,
            "intraday_tier_pct": 0.05,
        },
        "time_bound_actions": [],
    }


def _no_cap_config() -> dict:
    """Config without max_position_pct_capacity (absent key)."""
    return {
        "parameters": {
            "max_positions": 15,
            "stop_loss_pct_core": 0.035,
            "stop_loss_pct_intraday": 0.018,
            "take_profit_multiple": 2.5,
            "catalyst_tag_required_for_entry": False,
            "session_gate_enforce": False,
        },
        "position_sizing": {
            "core_tier_pct": 0.15,
            "dynamic_tier_pct": 0.08,
            "intraday_tier_pct": 0.05,
        },
        "time_bound_actions": [],
    }


class TestMaxPositionCap:
    def test_position_capped_at_max_pct(self):
        """size_position must not exceed max_position_pct_capacity * total_capacity."""
        equity = 100_000.0
        # Core tier at MEDIUM conviction = 15% of $100K = $15K, but cap is 7% = $7K
        snap = _snapshot(equity=equity)
        config = _cap_config(max_pct=0.07)
        result = size_position(_idea(tier=Tier.CORE, conviction=0.60), snap, config,
                               current_price=10.0, vix=20.0)
        assert isinstance(result, tuple), f"Expected tuple, got rejection: {result}"
        qty, position_value = result
        cap_dollars = equity * 0.07
        assert position_value <= cap_dollars + 0.01, (
            f"Position value ${position_value:.0f} exceeds cap ${cap_dollars:.0f}"
        )

    def test_position_within_cap_unchanged(self):
        """size_position below cap is returned unchanged."""
        equity = 100_000.0
        # Intraday tier = 5% of $100K = $5K; cap is 20% ($20K) — cap should not fire
        snap = _snapshot(equity=equity)
        config = _cap_config(max_pct=0.20)
        result = size_position(_idea(tier=Tier.INTRADAY, conviction=0.60), snap, config,
                               current_price=10.0, vix=20.0)
        assert isinstance(result, tuple), f"Expected tuple, got rejection: {result}"
        qty, position_value = result
        # With cap at 20%, the 5% intraday sizing should come through untouched
        expected = equity * 0.05  # 5% intraday tier
        assert abs(position_value - expected) < 10.0, (
            f"Expected ~${expected:.0f}, got ${position_value:.0f} — cap should not have fired"
        )

    def test_cap_missing_from_config_does_not_crash(self):
        """If max_position_pct_capacity absent from config, existing logic runs unchanged."""
        snap = _snapshot(equity=100_000.0)
        config = _no_cap_config()
        result = size_position(_idea(tier=Tier.CORE, conviction=0.60), snap, config,
                               current_price=10.0, vix=20.0)
        assert isinstance(result, tuple), f"Expected tuple, got rejection: {result}"
        qty, position_value = result
        # Core at 15% = $15K
        assert abs(position_value - 15_000.0) < 10.0

    def test_cap_never_increases_size(self):
        """Cap is an upper bound only — never increases size."""
        equity = 100_000.0
        # Use a huge cap (50%) — sizing should not increase above normal 15%
        snap = _snapshot(equity=equity)
        config_no_cap = _no_cap_config()
        config_large_cap = _cap_config(max_pct=0.50)
        result_no_cap = size_position(_idea(tier=Tier.CORE, conviction=0.60), snap,
                                      config_no_cap, current_price=10.0, vix=20.0)
        result_with_cap = size_position(_idea(tier=Tier.CORE, conviction=0.60), snap,
                                        config_large_cap, current_price=10.0, vix=20.0)
        assert isinstance(result_no_cap, tuple)
        assert isinstance(result_with_cap, tuple)
        _, val_no_cap = result_no_cap
        _, val_with_cap = result_with_cap
        assert val_with_cap <= val_no_cap + 0.01, (
            f"Cap increased size: ${val_with_cap:.0f} > ${val_no_cap:.0f}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Item 4 — preflight._check_vix_halt() dict schema
# ─────────────────────────────────────────────────────────────────────────────

class TestVixHaltGate:
    def _make_snap(self, tmp_path, vix_value) -> Path:
        snap = tmp_path / "macro_snapshot.json"
        snap.write_text(json.dumps({"vix": vix_value, "fetched_at": "2026-04-26T00:00:00Z"}))
        return snap

    def _run_check(self, tmp_path, vix_value):
        """Run _check_vix_halt with a patched snap path."""
        import preflight
        snap = self._make_snap(tmp_path, vix_value)
        with mock.patch.object(preflight, "_check_vix_halt") as _m:
            # Call the real function via direct import
            pass
        # Import the actual private function for direct testing
        from preflight import _check_vix_halt as _fn
        _orig = preflight.Path("data/market/macro_snapshot.json")
        # Patch Path resolution by monkeypatching the snap_path used inside
        with mock.patch("preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = snap
            return _fn()

    def test_vix_dict_schema_parsed_correctly(self, tmp_path):
        """_check_vix_halt must parse {'price': N, 'chg_pct': M} without TypeError."""
        snap = self._make_snap(tmp_path, {"price": 40.5, "chg_pct": 3.2})
        with mock.patch("preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = snap
            from preflight import _check_vix_halt
            result = _check_vix_halt()
        # VIX=40.5 > 35 → soft fail
        assert not result.passed
        assert "40.5" in result.message

    def test_vix_scalar_schema_still_works(self, tmp_path):
        """_check_vix_halt must still work when vix is a plain float."""
        snap = self._make_snap(tmp_path, 18.5)
        with mock.patch("preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = snap
            from preflight import _check_vix_halt
            result = _check_vix_halt()
        assert result.passed
        assert "18.5" in result.message

    def test_vix_below_threshold_no_halt(self, tmp_path):
        """VIX dict with price < 35 must return passed=True."""
        snap = self._make_snap(tmp_path, {"price": 18.97, "chg_pct": -1.76})
        with mock.patch("preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = snap
            from preflight import _check_vix_halt
            result = _check_vix_halt()
        assert result.passed
        assert result.severity == "soft"

    def test_vix_missing_does_not_halt(self, tmp_path):
        """If macro_snapshot.json absent, _check_vix_halt must return passed=True."""
        _absent = tmp_path / "macro_snapshot.json"
        # Do NOT create the file
        with mock.patch("preflight.Path") as mock_path_cls:
            absent_mock = mock.MagicMock()
            absent_mock.exists.return_value = False
            mock_path_cls.return_value = absent_mock
            from preflight import _check_vix_halt
            result = _check_vix_halt()
        assert result.passed


# ─────────────────────────────────────────────────────────────────────────────
# Item 5 — bot.py attribution _dt NameError (overnight path)
# ─────────────────────────────────────────────────────────────────────────────

def test_bot_dt_import_available_on_overnight_path():
    """
    _dt must be importable and usable even when session_tier == 'overnight'.
    We verify that moving the import above the if/else means _dt is always defined.
    Tested by checking that 'from datetime import datetime as _dt' appears
    BEFORE the if session_tier == 'overnight': block in bot.py.
    """
    bot_py = Path(__file__).resolve().parent.parent / "bot.py"
    src = bot_py.read_text()
    dt_import_pos = src.find("from datetime import datetime as _dt")
    overnight_pos = src.find('if session_tier == "overnight"')
    assert dt_import_pos != -1, "_dt import not found in bot.py"
    assert overnight_pos != -1, "overnight branch not found in bot.py"
    assert dt_import_pos < overnight_pos, (
        f"_dt import (pos {dt_import_pos}) must appear BEFORE the overnight branch "
        f"(pos {overnight_pos}); attribution block will NameError on overnight cycles"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Item 7 — scheduler._maybe_reset_session_watchlist exception guard
# ─────────────────────────────────────────────────────────────────────────────

def test_maybe_reset_session_watchlist_guarded_against_exception(monkeypatch):
    """_maybe_reset_session_watchlist must not propagate exceptions to caller."""
    import scheduler

    # Force the time condition to trigger (8 PM ET = 20:00)
    fake_now = mock.MagicMock()
    fake_now.hour = 20
    fake_now.minute = 5
    fake_now.weekday.return_value = 0

    monkeypatch.setattr("scheduler._session_reset_done", None)
    monkeypatch.setattr("scheduler.datetime", mock.MagicMock(now=mock.MagicMock(return_value=fake_now)))
    monkeypatch.setattr("scheduler._today", lambda: "2026-04-28")

    # Make watchlist_manager.reset_session_tiers raise
    bad_wm = mock.MagicMock()
    bad_wm.reset_session_tiers.side_effect = RuntimeError("watchlist DB locked")
    monkeypatch.setitem(sys.modules, "watchlist_manager", bad_wm)

    # Must not raise
    try:
        scheduler._maybe_reset_session_watchlist()
    except Exception as exc:
        pytest.fail(f"_maybe_reset_session_watchlist propagated exception: {exc}")
