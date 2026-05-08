"""Tests for crypto mean-reversion signal layer (bot_stage2_signal.py)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import bot_stage2_signal
import crypto_reversion_state as crs_module
from bot_stage2_signal import (
    _compute_4h_drop,
    _maybe_append_crypto_reversion_signal,
    format_signal_scores,
)
from crypto_reversion_state import CryptoReversionState

_CFG_ENABLED = {
    "crypto_reversion": {
        "crypto_reversion_enabled": True,
        "btc": {
            "symbol": "BTC/USD",
            "drop_threshold_pct": 1.0,
            "stop_pct": 1.5,
            "target_reversion_pct": 100,
            "max_hold_hours": 8,
            "sizing_multiplier": 0.8,
        },
        "eth": {
            "symbol": "ETH/USD",
            "drop_threshold_pct": 0.5,
            "stop_pct": 1.5,
            "target_reversion_pct": 200,
            "max_hold_hours": 8,
            "sizing_multiplier": 0.8,
        },
    }
}
_CFG_DISABLED = {
    "crypto_reversion": {
        **_CFG_ENABLED["crypto_reversion"],
        "crypto_reversion_enabled": False,
    }
}


@pytest.fixture(autouse=True)
def clear_cache():
    bot_stage2_signal._CRYPTO_DROP_CACHE.clear()
    yield
    bot_stage2_signal._CRYPTO_DROP_CACHE.clear()


@pytest.fixture(autouse=True)
def redirect_state_file(tmp_path, monkeypatch):
    """Redirect CryptoReversionState storage to a temp file — no real state touched."""
    state_file = tmp_path / "crypto_reversion_state.json"
    monkeypatch.setattr(crs_module, "_STATE_PATH", state_file)
    yield state_file


def test_btc_trigger_fires_at_threshold():
    scored = {}
    with patch("bot_stage2_signal._compute_4h_drop", return_value=-1.1):
        _maybe_append_crypto_reversion_signal(scored, _CFG_ENABLED)
    assert "BTC/USD" in scored


def test_btc_no_trigger_below_threshold():
    scored = {}
    with patch("bot_stage2_signal._compute_4h_drop", return_value=-0.8):
        _maybe_append_crypto_reversion_signal(scored, _CFG_ENABLED)
    assert "BTC/USD" not in scored


def test_eth_trigger_fires_at_threshold():
    scored = {}
    with patch("bot_stage2_signal._compute_4h_drop", return_value=-0.6):
        _maybe_append_crypto_reversion_signal(scored, _CFG_ENABLED)
    assert "ETH/USD" in scored


def test_eth_no_trigger_below_threshold():
    scored = {}
    with patch("bot_stage2_signal._compute_4h_drop", return_value=-0.3):
        _maybe_append_crypto_reversion_signal(scored, _CFG_ENABLED)
    assert "ETH/USD" not in scored


def test_drop_compute_returns_none_on_failure():
    with patch("yfinance.Ticker", side_effect=RuntimeError("network")):
        result = _compute_4h_drop("BTC-USD")
    assert result is None


def test_disabled_by_config():
    scored = {}
    with patch("bot_stage2_signal._compute_4h_drop", return_value=-5.0):
        _maybe_append_crypto_reversion_signal(scored, _CFG_DISABLED)
    assert "BTC/USD" not in scored
    assert "ETH/USD" not in scored


def test_signal_format_correct():
    scored = {}
    with patch("bot_stage2_signal._compute_4h_drop", return_value=-1.2):
        _maybe_append_crypto_reversion_signal(scored, _CFG_ENABLED)
    sig = scored["BTC/USD"]
    assert sig["score"] == 72
    assert sig["direction"] == "bullish"
    assert "crypto_reversion_setup" in sig["catalyst"]
    assert "BTC/USD" in sig["catalyst"]


def test_stop_pct_in_signal():
    scored = {}
    with patch("bot_stage2_signal._compute_4h_drop", return_value=-1.2):
        _maybe_append_crypto_reversion_signal(scored, _CFG_ENABLED)
    assert scored["BTC/USD"]["stop_pct"] == 1.5
    assert scored["ETH/USD"]["stop_pct"] == 1.5


def test_sizing_multiplier_in_signal():
    scored = {}
    with patch("bot_stage2_signal._compute_4h_drop", return_value=-1.2):
        _maybe_append_crypto_reversion_signal(scored, _CFG_ENABLED)
    assert scored["BTC/USD"]["sizing_multiplier"] == 0.8
    assert scored["ETH/USD"]["sizing_multiplier"] == 0.8


def test_both_crypto_evaluated():
    calls: list[str] = []

    def fake_drop(sym: str) -> float:
        calls.append(sym)
        return -2.0

    scored = {}
    with patch("bot_stage2_signal._compute_4h_drop", side_effect=fake_drop):
        _maybe_append_crypto_reversion_signal(scored, _CFG_ENABLED)

    assert "BTC-USD" in calls
    assert "ETH-USD" in calls
    assert "BTC/USD" in scored
    assert "ETH/USD" in scored


def test_cache_prevents_duplicate_fetch():
    mock_hist = pd.DataFrame({"Close": [100.0, 99.0, 98.0, 97.0]})
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = mock_hist

    with patch("yfinance.Ticker", return_value=mock_ticker) as mock_ctor:
        _compute_4h_drop("BTC-USD")
        _compute_4h_drop("BTC-USD")

    assert mock_ctor.call_count == 1


def test_4h_drop_calculation_correct():
    # closes: 100 -> 99 -> 98 -> 97  =>  drop = (97 - 100) / 100 * 100 = -3.0%
    mock_hist = pd.DataFrame({"Close": [100.0, 99.0, 98.0, 97.0]})
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = mock_hist

    with patch("yfinance.Ticker", return_value=mock_ticker):
        result = _compute_4h_drop("BTC-USD")

    assert result == pytest.approx(-3.0, abs=0.01)


# ═════════════════════════════════════════════════════════════════════════════
# VAL-spec tests for CryptoReversionState and halt tracking
# ═════════════════════════════════════════════════════════════════════════════

# VAL-spec a) ETH 0.51% drop -> signal fires at 0.8x sizing
def test_eth_051_drop_fires():
    scored = {}
    with patch("bot_stage2_signal._compute_4h_drop", return_value=-0.51):
        _maybe_append_crypto_reversion_signal(scored, _CFG_ENABLED)
    assert "ETH/USD" in scored
    assert scored["ETH/USD"]["sizing_multiplier"] == 0.8


# VAL-spec b) BTC 0.99% drop -> no signal (below 1.0% threshold)
def test_btc_099_no_signal():
    scored = {}
    with patch("bot_stage2_signal._compute_4h_drop", return_value=-0.99):
        _maybe_append_crypto_reversion_signal(scored, _CFG_ENABLED)
    assert "BTC/USD" not in scored


# VAL-spec c) BTC 1.01% drop -> signal fires
def test_btc_101_fires():
    scored = {}
    with patch("bot_stage2_signal._compute_4h_drop", return_value=-1.01):
        _maybe_append_crypto_reversion_signal(scored, _CFG_ENABLED)
    assert "BTC/USD" in scored
    assert scored["BTC/USD"]["score"] == 72


# VAL-spec d) 3 combined losses -> is_halted() = True
def test_three_losses_halt():
    state = CryptoReversionState()
    state.record_loss("BTC")
    state.record_loss("ETH")
    state.record_loss("BTC")
    assert state.is_halted() is True


# VAL-spec d cont.) signals return [] when halted
def test_halted_signals_empty():
    state = CryptoReversionState()
    state.record_loss("BTC")
    state.record_loss("ETH")
    state.record_loss("BTC")
    assert state.is_halted() is True

    scored = {}
    with patch("bot_stage2_signal._compute_4h_drop", return_value=-5.0):
        _maybe_append_crypto_reversion_signal(scored, _CFG_ENABLED)
    assert scored == {}


# VAL-spec e) State persists and reloads correctly from JSON
def test_state_persistence():
    s1 = CryptoReversionState()
    s1.record_loss("BTC")
    s1.record_loss("ETH")

    s2 = CryptoReversionState()
    assert s2.get_state()["combined_losses"] == 2
    assert s2.get_state()["btc_losses"] == 1
    assert s2.get_state()["eth_losses"] == 1
    assert s2.is_halted() is False


# VAL-spec f) Halted state: signal suppressed with log line
def test_halt_log_line(caplog):
    import logging
    state = CryptoReversionState()
    state.record_loss("BTC")
    state.record_loss("ETH")
    state.record_loss("BTC")

    scored = {}
    with caplog.at_level(logging.INFO, logger="bot_stage2_signal"), \
         patch("bot_stage2_signal._compute_4h_drop", return_value=-5.0):
        _maybe_append_crypto_reversion_signal(scored, _CFG_ENABLED)

    assert scored == {}
    assert any("halted" in r.message for r in caplog.records)


def test_clean_default_no_file(redirect_state_file):
    assert not redirect_state_file.exists()
    state = CryptoReversionState()
    assert state.get_state() == {
        "btc_losses": 0, "eth_losses": 0, "combined_losses": 0, "cross_symbol_halt": False
    }


def test_win_resets_symbol_count():
    state = CryptoReversionState()
    state.record_loss("BTC")
    state.record_loss("BTC")
    assert state.get_state()["btc_losses"] == 2
    state.record_win("BTC")
    assert state.get_state()["btc_losses"] == 0
    assert state.get_state()["combined_losses"] == 2  # combined not reset by win


def test_two_losses_no_halt():
    state = CryptoReversionState()
    state.record_loss("BTC")
    state.record_loss("ETH")
    assert state.is_halted() is False


def test_format_signal_scores_shows_crypto_mr():
    scores = {
        "scored_symbols": {
            "ETH/USD": {
                "score": 72, "conviction": "medium",
                "primary_catalyst": "crypto_reversion_setup: ETH/USD dropped -0.6% in 4h",
                "catalyst_type": "mean_reversion",
                "signals": ["crypto_mean_reversion"],
                "drop_pct": -0.6,
                "stop_pct": 1.5,
            }
        }
    }
    result = format_signal_scores(scores)
    assert "[CRYPTO-MR]" in result
    assert "ETH/USD" in result
    assert "enter_long required" in result


def test_format_signal_scores_equity_not_cut_off_by_crypto():
    """Crypto MR entries shown first; equity still gets up to 10 slots."""
    equity_syms = {f"SYM{i:02d}": {"score": 70, "conviction": "medium",
                                     "primary_catalyst": f"catalyst {i}",
                                     "signals": [], "orb_candidate": False}
                   for i in range(12)}
    scores = {
        "scored_symbols": {
            "ETH/USD": {"score": 72, "conviction": "medium",
                        "primary_catalyst": "crypto_reversion_setup: ...",
                        "catalyst_type": "mean_reversion",
                        "signals": [], "drop_pct": -0.6, "stop_pct": 1.5},
            **equity_syms,
        }
    }
    result = format_signal_scores(scores)
    assert "[CRYPTO-MR]" in result
    assert "ETH/USD" in result
