"""Tests for crypto mean-reversion signal layer (bot_stage2_signal.py)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import bot_stage2_signal
from bot_stage2_signal import (
    _compute_4h_drop,
    _maybe_append_crypto_reversion_signal,
)

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
    # closes: 100 → 99 → 98 → 97  =>  drop = (97 - 100) / 100 * 100 = -3.0%
    mock_hist = pd.DataFrame({"Close": [100.0, 99.0, 98.0, 97.0]})
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = mock_hist

    with patch("yfinance.Ticker", return_value=mock_ticker):
        result = _compute_4h_drop("BTC-USD")

    assert result == pytest.approx(-3.0, abs=0.01)
