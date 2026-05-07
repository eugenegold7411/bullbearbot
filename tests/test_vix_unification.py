"""
tests/test_vix_unification.py — VIX unification and staleness alerting tests.

Tests:
  1. bot_options imports get_vix_with_fallback from market_data (not local fetch)
  2. get_vix_with_fallback returns default and logs WARNING when live fetch fails
  3. get_vix_with_fallback returns file cache value when live fails + cache is fresh
  4. get_vix_with_fallback ignores stale file cache (> 30 min) and returns default
  5. Alert fires after 3 consecutive failures
  6. Alert not repeated on 4th consecutive failure (5-min dedup)
  7. Consecutive failure counter resets to 0 on success
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import market_data as md

# ── helpers ──────────────────────────────────────────────────────────────────

def _reset_state():
    """Reset module-level VIX state between tests."""
    md._vix_consecutive_failures = 0
    md._VIX_CACHE["value"] = None
    md._VIX_CACHE["ts"] = 0.0
    md._SAFETY_ALERT_CACHE.pop("vix_fetch_failure", None)


# ── Test 1: bot_options uses market_data.get_vix_with_fallback ────────────────

def test_a2_uses_market_data_vix():
    """bot_options.py must import get_vix_with_fallback from market_data, not a local fetch."""
    import inspect

    import bot_options
    src = inspect.getsource(bot_options)
    assert "get_vix_with_fallback" in src, \
        "bot_options.py must call get_vix_with_fallback from market_data"
    assert "yf.Ticker" not in src or "VIX" not in src.split("yf.Ticker")[1].split("\n")[0] \
        if "yf.Ticker" in src else True, \
        "bot_options.py must not do its own yfinance VIX fetch"
    # Confirm the import comes from market_data
    assert "from market_data import get_vix_with_fallback" in src, \
        "bot_options.py must import get_vix_with_fallback from market_data"


# ── Test 2: fallback to default when live fails ───────────────────────────────

def test_vix_fallback_on_live_failure(tmp_path, caplog):
    """get_vix() returns None → get_vix_with_fallback returns default and logs WARNING."""
    _reset_state()
    with (
        patch.object(md, "_VIX_FILE_CACHE", tmp_path / "vix_cache_absent.json"),
        patch("market_data.get_vix", return_value=None),
    ):
        import logging
        with caplog.at_level(logging.WARNING, logger="market_data"):
            result = md.get_vix_with_fallback(default=20.0)

    assert result == 20.0
    assert any("fallback" in r.message.lower() or "failed" in r.message.lower()
               for r in caplog.records), "Expected WARNING about fallback"


# ── Test 3: uses file cache when live fails and cache is fresh ────────────────

def test_vix_uses_cache_when_fresh(tmp_path):
    """When get_vix() fails, a file cache < 30min old is used."""
    _reset_state()
    cache_file = tmp_path / "vix_cache.json"
    cache_file.write_text(json.dumps({"vix": 18.5, "fetched_at": time.time()}))

    with (
        patch.object(md, "_VIX_FILE_CACHE", cache_file),
        patch("market_data.get_vix", return_value=None),
    ):
        result = md.get_vix_with_fallback(default=20.0)

    assert result == 18.5, f"expected 18.5 from fresh cache, got {result}"


# ── Test 4: ignores stale file cache (> 30 min) ───────────────────────────────

def test_vix_ignores_stale_cache(tmp_path):
    """File cache older than 30 min must be ignored; fallback default used."""
    _reset_state()
    cache_file = tmp_path / "vix_cache.json"
    cache_file.write_text(json.dumps({"vix": 18.5, "fetched_at": time.time() - 3000}))
    # Backdate the mtime to simulate staleness
    stale_mtime = time.time() - md._VIX_FILE_CACHE_MAX_AGE_S - 60
    import os
    os.utime(cache_file, (stale_mtime, stale_mtime))

    with (
        patch.object(md, "_VIX_FILE_CACHE", cache_file),
        patch("market_data.get_vix", return_value=None),
    ):
        result = md.get_vix_with_fallback(default=20.0)

    assert result == 20.0, f"expected default 20.0 for stale cache, got {result}"


# ── Test 5: alert fires after 3 consecutive failures ─────────────────────────

def test_vix_alert_on_consecutive_failures(tmp_path):
    """Alert must be fired (once) after _VIX_FAILURE_ALERT_THRESHOLD consecutive failures."""
    _reset_state()
    cache_file = tmp_path / "vix_cache_absent.json"
    sent = []

    with (
        patch.object(md, "_VIX_FILE_CACHE", cache_file),
        patch("market_data.get_vix", return_value=None),
        patch("market_data._SAFETY_DEDUP_SECS", 0),  # disable dedup for this test
        patch("market_data._maybe_fire_vix_alert", wraps=md._maybe_fire_vix_alert),
    ):
        # Patch send_whatsapp_direct inside notifications module
        with patch.dict(
            __import__("sys").modules,
            {"notifications": MagicMock(send_whatsapp_direct=lambda msg: sent.append(msg))},
        ):
            for _ in range(3):
                md.get_vix_with_fallback(default=20.0)

    assert len(sent) >= 1, "Expected at least one WhatsApp alert after 3 failures"
    assert "VIX DEGRADED" in sent[0] or "vix" in sent[0].lower()


# ── Test 6: alert NOT repeated on 4th failure (dedup) ────────────────────────

def test_vix_alert_not_repeated(tmp_path):
    """4th consecutive failure within 5-min window must NOT fire another alert."""
    _reset_state()
    cache_file = tmp_path / "vix_cache_absent.json"
    sent = []

    def fake_send(msg):
        sent.append(msg)

    with (
        patch.object(md, "_VIX_FILE_CACHE", cache_file),
        patch("market_data.get_vix", return_value=None),
    ):
        with patch.dict(
            __import__("sys").modules,
            {"notifications": MagicMock(send_whatsapp_direct=fake_send)},
        ):
            for _ in range(4):
                md.get_vix_with_fallback(default=20.0)

    # Alert must have fired at most once (dedup prevents re-fire within 5 min)
    assert len(sent) <= 1, f"Expected at most 1 alert, got {len(sent)}"


# ── Test 7: counter resets on success ────────────────────────────────────────

def test_vix_counter_resets_on_success(tmp_path):
    """After 2 failures then a successful get_vix(), counter must be 0."""
    _reset_state()
    cache_file = tmp_path / "vix_cache_absent.json"

    with (
        patch.object(md, "_VIX_FILE_CACHE", cache_file),
        patch("market_data.get_vix", return_value=None),
    ):
        md.get_vix_with_fallback(default=20.0)
        md.get_vix_with_fallback(default=20.0)

    assert md._vix_consecutive_failures == 2

    with patch("market_data.get_vix", return_value=17.5):
        result = md.get_vix_with_fallback(default=20.0)

    assert result == 17.5
    assert md._vix_consecutive_failures == 0, \
        f"Counter should be 0 after success, got {md._vix_consecutive_failures}"
