"""
tests/test_silent_failures_medium.py

29 tests for MEDIUM and LOW severity silent failure remediation (#18–#29):

  SF18  bot_stage0_precycle — pi/recon block fires alert
  SF19  bot_stage0_precycle — divergence init failure fires alert
  SF20  scheduler — _ensure_account_modes_initialized fires alert
  SF21  scheduler — _maybe_run_health_checks fires alert
  SF23  market_data — get_market_clock fires alert, returns fallback
  SF24  market_data — stock bars fetch fires alert
  SF25  data_warehouse — bars save (was debug) fires alert
  SF26  data_warehouse — bars batch fetch fires alert
  SF29  bot_stage2_signal — score_signals fires alert, returns {}
  SF27  data_warehouse — earnings write logs at error level (LOW)
  SF28  scheduler — watchlist reset logs at error level (LOW)
"""

import inspect
import sys
import time
import types
import unittest
from unittest.mock import ANY, MagicMock, patch

# ---------------------------------------------------------------------------
# Install notifications stub before any production module import.
# Must include all symbols imported by bot.py (pulled in via scheduler).
# ---------------------------------------------------------------------------
if "notifications" not in sys.modules:
    _notif = types.ModuleType("notifications")
    _notif.send_whatsapp_direct = lambda msg: True  # type: ignore[attr-defined]
    _notif.build_order_email_html = lambda *a, **kw: "<html></html>"  # type: ignore[attr-defined]
    sys.modules["notifications"] = _notif
else:
    _notif = sys.modules["notifications"]
    if not hasattr(_notif, "build_order_email_html"):
        _notif.build_order_email_html = lambda *a, **kw: "<html></html>"  # type: ignore[attr-defined]
    if not hasattr(_notif, "send_whatsapp_direct"):
        _notif.send_whatsapp_direct = lambda msg: True  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------

def _clear_cache(mod) -> None:
    if hasattr(mod, "_SAFETY_ALERT_CACHE"):
        mod._SAFETY_ALERT_CACHE.clear()


def _capture_whatsapp(mod):
    """Context manager: intercept send_whatsapp_direct, return captured list."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        sent = []
        orig = sys.modules["notifications"].send_whatsapp_direct
        sys.modules["notifications"].send_whatsapp_direct = lambda msg: sent.append(msg)
        try:
            yield sent
        finally:
            sys.modules["notifications"].send_whatsapp_direct = orig
            _clear_cache(mod)

    return _ctx()


# ═══════════════════════════════════════════════════════════════════════════════
# SF18 — bot_stage0_precycle: pi/recon block
# ═══════════════════════════════════════════════════════════════════════════════

class TestSF18PiReconBlock(unittest.TestCase):

    def setUp(self):
        import bot_stage0_precycle
        _clear_cache(bot_stage0_precycle)

    def test_alert_fires_on_pi_failure(self):
        import bot_stage0_precycle
        with _capture_whatsapp(bot_stage0_precycle) as sent:
            bot_stage0_precycle._fire_safety_alert(
                "run_precycle_pi_recon", RuntimeError("pi build failed")
            )
        self.assertEqual(len(sent), 1)
        self.assertIn("run_precycle_pi_recon", sent[0])
        self.assertIn("SAFETY DEGRADED", sent[0])

    def test_dedup_suppresses_second_alert(self):
        import bot_stage0_precycle
        bot_stage0_precycle._SAFETY_ALERT_CACHE["run_precycle_pi_recon"] = time.time()
        with _capture_whatsapp(bot_stage0_precycle) as sent:
            bot_stage0_precycle._fire_safety_alert(
                "run_precycle_pi_recon", RuntimeError("still broken")
            )
        self.assertEqual(len(sent), 0)

    def test_notification_failure_does_not_raise(self):
        import bot_stage0_precycle
        with patch.object(
            sys.modules["notifications"], "send_whatsapp_direct",
            side_effect=RuntimeError("WA down"),
        ):
            bot_stage0_precycle._fire_safety_alert(
                "run_precycle_pi_recon", RuntimeError("pi fail")
            )


# ═══════════════════════════════════════════════════════════════════════════════
# SF19 — bot_stage0_precycle: divergence init failure
# ═══════════════════════════════════════════════════════════════════════════════

class TestSF19DivergenceInit(unittest.TestCase):

    def setUp(self):
        import bot_stage0_precycle
        _clear_cache(bot_stage0_precycle)

    def test_alert_fires_on_divergence_init_failure(self):
        import bot_stage0_precycle
        with _capture_whatsapp(bot_stage0_precycle) as sent:
            bot_stage0_precycle._fire_safety_alert(
                "run_precycle_divergence_init", ImportError("divergence import failed")
            )
        self.assertEqual(len(sent), 1)
        self.assertIn("run_precycle_divergence_init", sent[0])

    def test_dedup_suppresses_second_alert(self):
        import bot_stage0_precycle
        bot_stage0_precycle._SAFETY_ALERT_CACHE["run_precycle_divergence_init"] = time.time()
        with _capture_whatsapp(bot_stage0_precycle) as sent:
            bot_stage0_precycle._fire_safety_alert(
                "run_precycle_divergence_init", ImportError("still failing")
            )
        self.assertEqual(len(sent), 0)

    def test_alert_fires_again_after_dedup_window(self):
        import bot_stage0_precycle
        bot_stage0_precycle._SAFETY_ALERT_CACHE["run_precycle_divergence_init"] = (
            time.time() - 360  # 6 min ago — past the 300s window
        )
        with _capture_whatsapp(bot_stage0_precycle) as sent:
            bot_stage0_precycle._fire_safety_alert(
                "run_precycle_divergence_init", ImportError("failing again")
            )
        self.assertEqual(len(sent), 1)


# ═══════════════════════════════════════════════════════════════════════════════
# SF20 — scheduler: _ensure_account_modes_initialized
# ═══════════════════════════════════════════════════════════════════════════════

class TestSF20ModeInit(unittest.TestCase):

    def setUp(self):
        import scheduler
        _clear_cache(scheduler)

    def test_alert_fires_on_init_failure(self):
        import scheduler
        with _capture_whatsapp(scheduler) as sent:
            scheduler._fire_safety_alert(
                "_ensure_account_modes_initialized", RuntimeError("divergence unavailable")
            )
        self.assertEqual(len(sent), 1)
        self.assertIn("_ensure_account_modes_initialized", sent[0])

    def test_dedup_suppresses_second_alert(self):
        import scheduler
        scheduler._SAFETY_ALERT_CACHE["_ensure_account_modes_initialized"] = time.time()
        with _capture_whatsapp(scheduler) as sent:
            scheduler._fire_safety_alert(
                "_ensure_account_modes_initialized", RuntimeError("still broken")
            )
        self.assertEqual(len(sent), 0)

    def test_notification_failure_does_not_raise(self):
        import scheduler
        with patch.object(
            sys.modules["notifications"], "send_whatsapp_direct",
            side_effect=RuntimeError("WA down"),
        ):
            scheduler._fire_safety_alert(
                "_ensure_account_modes_initialized", RuntimeError("init failed")
            )


# ═══════════════════════════════════════════════════════════════════════════════
# SF21 — scheduler: _maybe_run_health_checks
# ═══════════════════════════════════════════════════════════════════════════════

class TestSF21HealthCheckWrapper(unittest.TestCase):

    def setUp(self):
        import scheduler
        _clear_cache(scheduler)

    def test_alert_fires_on_health_check_exception(self):
        import scheduler
        with _capture_whatsapp(scheduler) as sent:
            scheduler._fire_safety_alert(
                "_maybe_run_health_checks", RuntimeError("health_monitor unavailable")
            )
        self.assertEqual(len(sent), 1)
        self.assertIn("_maybe_run_health_checks", sent[0])
        self.assertIn("SAFETY DEGRADED", sent[0])

    def test_dedup_suppresses_second_alert(self):
        import scheduler
        scheduler._SAFETY_ALERT_CACHE["_maybe_run_health_checks"] = time.time()
        with _capture_whatsapp(scheduler) as sent:
            scheduler._fire_safety_alert(
                "_maybe_run_health_checks", RuntimeError("still broken")
            )
        self.assertEqual(len(sent), 0)

    def test_two_different_fns_fire_independently(self):
        import scheduler
        _clear_cache(scheduler)
        with _capture_whatsapp(scheduler) as sent:
            scheduler._fire_safety_alert("_maybe_run_health_checks", RuntimeError("a"))
            scheduler._fire_safety_alert("_ensure_account_modes_initialized", RuntimeError("b"))
        self.assertEqual(len(sent), 2)


# ═══════════════════════════════════════════════════════════════════════════════
# SF23 — market_data: get_market_clock
# ═══════════════════════════════════════════════════════════════════════════════

class TestSF23MarketClock(unittest.TestCase):

    def setUp(self):
        import market_data
        _clear_cache(market_data)

    def test_alert_fires_on_clock_failure(self):
        import market_data
        with patch.object(market_data, "_fire_safety_alert") as mock_alert:
            with patch.object(
                market_data, "_get_trading_client",
                side_effect=RuntimeError("Alpaca down"),
            ):
                result = market_data.get_market_clock()
        mock_alert.assert_called_once_with("get_market_clock", ANY)
        self.assertFalse(result["is_open"])

    def test_fallback_dict_has_required_keys(self):
        import market_data
        with patch.object(
            market_data, "_get_trading_client",
            side_effect=RuntimeError("timeout"),
        ):
            result = market_data.get_market_clock()
        for key in ("is_open", "status", "time_et", "minutes_since_open"):
            self.assertIn(key, result)

    def test_dedup_suppresses_whatsapp_send(self):
        import market_data
        market_data._SAFETY_ALERT_CACHE["get_market_clock"] = time.time()
        with _capture_whatsapp(market_data) as sent:
            with patch.object(
                market_data, "_get_trading_client",
                side_effect=RuntimeError("still down"),
            ):
                market_data.get_market_clock()
        self.assertEqual(len(sent), 0)


# ═══════════════════════════════════════════════════════════════════════════════
# SF24 — market_data: stock bars fetch
# ═══════════════════════════════════════════════════════════════════════════════

class TestSF24StockBarsFetch(unittest.TestCase):

    def setUp(self):
        import market_data
        _clear_cache(market_data)

    def test_alert_fires_on_bars_fetch_error(self):
        import market_data
        mock_client = MagicMock()
        mock_client.get_stock_bars.side_effect = RuntimeError("bars fetch boom")
        mock_client.get_stock_latest_trade.return_value = {}
        with patch.object(market_data, "_get_data_client", return_value=mock_client):
            with patch.object(market_data, "_fire_safety_alert") as mock_alert:
                with patch.object(market_data, "dw",
                                  MagicMock(load_bars_cached=lambda s: None)):
                    market_data.get_stock_signals(["AAPL"], use_cache=False)
        mock_alert.assert_any_call("get_stock_data_bars_fetch", ANY)

    def test_dedup_suppresses_whatsapp_send(self):
        import market_data
        market_data._SAFETY_ALERT_CACHE["get_stock_data_bars_fetch"] = time.time()
        with _capture_whatsapp(market_data) as sent:
            market_data._fire_safety_alert(
                "get_stock_data_bars_fetch", RuntimeError("bars still failing")
            )
        self.assertEqual(len(sent), 0)

    def test_function_completes_without_raising_on_bars_failure(self):
        import market_data
        mock_client = MagicMock()
        mock_client.get_stock_bars.side_effect = RuntimeError("bars boom")
        mock_client.get_stock_latest_trade.return_value = {}
        with patch.object(market_data, "_get_data_client", return_value=mock_client):
            with patch.object(market_data, "_fire_safety_alert"):
                with patch.object(market_data, "dw",
                                  MagicMock(load_bars_cached=lambda s: None)):
                    # Must not raise
                    market_data.get_stock_signals(["AAPL"], use_cache=False)


# ═══════════════════════════════════════════════════════════════════════════════
# SF25 — data_warehouse: bars save failure (was log.debug — now log.error + alert)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSF25BarsSave(unittest.TestCase):

    def setUp(self):
        import data_warehouse
        _clear_cache(data_warehouse)

    def test_alert_fires_on_bars_save_failure(self):
        import data_warehouse
        with _capture_whatsapp(data_warehouse) as sent:
            data_warehouse._fire_safety_alert(
                "refresh_bars_save", OSError("disk full")
            )
        self.assertEqual(len(sent), 1)
        self.assertIn("refresh_bars_save", sent[0])

    def test_dedup_suppresses_second_save_alert(self):
        import data_warehouse
        data_warehouse._SAFETY_ALERT_CACHE["refresh_bars_save"] = time.time()
        with _capture_whatsapp(data_warehouse) as sent:
            data_warehouse._fire_safety_alert(
                "refresh_bars_save", OSError("disk full again")
            )
        self.assertEqual(len(sent), 0)

    def test_bars_save_uses_log_error_not_debug(self):
        """Verify the bars-save except block now calls log.error (was log.debug)."""
        import data_warehouse
        source = inspect.getsource(data_warehouse.refresh_bars)
        self.assertIn('log.error("Bars save failed', source)
        self.assertNotIn('log.debug("Bars save failed', source)


# ═══════════════════════════════════════════════════════════════════════════════
# SF26 — data_warehouse: bars batch fetch failure
# ═══════════════════════════════════════════════════════════════════════════════

class TestSF26BarsBatchFetch(unittest.TestCase):

    def setUp(self):
        import data_warehouse
        _clear_cache(data_warehouse)

    def test_alert_fires_on_batch_fetch_failure(self):
        import data_warehouse
        mock_client = MagicMock()
        mock_client.get_stock_bars.side_effect = RuntimeError("Alpaca API error")
        with patch.object(data_warehouse, "_get_data_client", return_value=mock_client):
            with patch.object(data_warehouse, "_fire_safety_alert") as mock_alert:
                data_warehouse.refresh_bars(["AAPL"])
        mock_alert.assert_any_call("refresh_bars_fetch", ANY)

    def test_dedup_suppresses_second_batch_alert(self):
        import data_warehouse
        data_warehouse._SAFETY_ALERT_CACHE["refresh_bars_fetch"] = time.time()
        with _capture_whatsapp(data_warehouse) as sent:
            data_warehouse._fire_safety_alert(
                "refresh_bars_fetch", RuntimeError("api still down")
            )
        self.assertEqual(len(sent), 0)

    def test_function_completes_without_raising(self):
        import data_warehouse
        mock_client = MagicMock()
        mock_client.get_stock_bars.side_effect = RuntimeError("boom")
        with patch.object(data_warehouse, "_get_data_client", return_value=mock_client):
            with patch.object(data_warehouse, "_fire_safety_alert"):
                data_warehouse.refresh_bars(["AAPL"])  # must not raise


# ═══════════════════════════════════════════════════════════════════════════════
# SF29 — bot_stage2_signal: score_signals top-level failure
# ═══════════════════════════════════════════════════════════════════════════════

class TestSF29SignalScorer(unittest.TestCase):

    def setUp(self):
        import bot_stage2_signal
        _clear_cache(bot_stage2_signal)

    def test_alert_fires_on_outer_scorer_failure(self):
        """Trigger the outer except by passing an md object that raises on .get()."""
        import bot_stage2_signal

        class _BrokenMd:
            def get(self, key, default=None):
                raise RuntimeError("md corrupted: cannot iterate")

        with patch.object(bot_stage2_signal, "_fire_safety_alert") as mock_alert:
            result = bot_stage2_signal.score_signals(["AAPL"], {}, _BrokenMd(), [])
        self.assertEqual(result, {})
        mock_alert.assert_called_once_with("score_signals", ANY)

    def test_dedup_suppresses_second_scorer_alert(self):
        import bot_stage2_signal
        bot_stage2_signal._SAFETY_ALERT_CACHE["score_signals"] = time.time()
        with _capture_whatsapp(bot_stage2_signal) as sent:
            bot_stage2_signal._fire_safety_alert(
                "score_signals", RuntimeError("still failing")
            )
        self.assertEqual(len(sent), 0)

    def test_returns_empty_dict_on_outer_failure(self):
        import bot_stage2_signal

        class _BrokenMd:
            def get(self, key, default=None):
                raise RuntimeError("md broken")

        with patch.object(bot_stage2_signal, "_fire_safety_alert"):
            result = bot_stage2_signal.score_signals(["MSFT"], {}, _BrokenMd(), [])
        self.assertIsInstance(result, dict)
        self.assertEqual(result, {})


# ═══════════════════════════════════════════════════════════════════════════════
# SF27 — data_warehouse: earnings write (LOW — log.error only, no alert)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSF27EarningsWrite(unittest.TestCase):

    def test_earnings_write_uses_log_error_not_warning(self):
        """Verify the write-failure except block now uses log.error (was log.warning)."""
        import data_warehouse
        source = inspect.getsource(data_warehouse.refresh_earnings_calendar_av)
        self.assertIn('log.error("[EARNINGS_AV] write failed', source)
        self.assertNotIn('log.warning("[EARNINGS_AV] write failed', source)


# ═══════════════════════════════════════════════════════════════════════════════
# SF28 — scheduler: session watchlist reset (LOW — log.error only, no alert)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSF28WatchlistReset(unittest.TestCase):

    def test_watchlist_reset_uses_log_error_not_warning(self):
        """Verify the reset-failure except block now uses log.error (was log.warning)."""
        import scheduler
        source = inspect.getsource(scheduler._maybe_reset_session_watchlist)
        self.assertIn("log.error", source)
        self.assertNotIn('log.warning("Session watchlist reset failed', source)


if __name__ == "__main__":
    unittest.main()
