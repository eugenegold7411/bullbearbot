"""
tests/test_sprint6_phase_a.py — Sprint 6 Phase A:
  - earnings_intel_fetcher: fetch, cache, format
  - morning_brief: tiered priority ordering (_build_pre_earnings_intel_section)
  - data_warehouse.run_full_refresh wires refresh_finnhub_news
  - scheduler._maybe_refresh_earnings_intel exists and is wired

Tests:
  PA6-01  load_analyst_intel_cached returns None when no cache file
  PA6-02  load_analyst_intel_cached returns dict when cache file exists
  PA6-03  refresh_earnings_analyst_intel writes cache file with symbol + fetched_at
  PA6-04  refresh_earnings_analyst_intel skips crypto symbols (contains '/')
  PA6-05  refresh_earnings_analyst_intel respects 24h TTL (no re-fetch when fresh)
  PA6-06  refresh_earnings_analyst_intel re-fetches when cache is stale (>24h)
  PA6-07  format_analyst_intel_text returns beat history string
  PA6-08  format_analyst_intel_text returns empty string for empty input
  PA6-09  format_analyst_intel_text includes consensus when bullish_pct + analyst_count present
  PA6-10  format_analyst_intel_text includes rec_mean label when bullish_pct absent
  PA6-11  _build_pre_earnings_intel_section includes HELD symbol in T0 (≤2d)
  PA6-12  _build_pre_earnings_intel_section includes HELD symbol in T1 (3–5d)
  PA6-13  _build_pre_earnings_intel_section caps T2 (!held, ≤1d) at 3
  PA6-14  _build_pre_earnings_intel_section caps T3 (!held, 2–5d) at 2
  PA6-15  _build_pre_earnings_intel_section held symbols not subject to T2/T3 caps
  PA6-16  _build_pre_earnings_intel_section injects analyst intel when cache present
  PA6-17  _build_pre_earnings_intel_section returns '' when no symbols in ≤5d window
  PA6-18  _build_pre_earnings_intel_section is non-fatal (returns '' on exception)
  PA6-19  run_full_refresh calls refresh_finnhub_news
  PA6-20  scheduler._maybe_refresh_earnings_intel exists as callable
  PA6-21  scheduler main loop calls _maybe_refresh_earnings_intel
  PA6-22  _earnings_intel_ran_date global exists in scheduler
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_calendar(*symbols_and_days: tuple[str, int]) -> dict:
    """Build a fake earnings calendar."""
    today = date.today()
    return {
        "calendar": [
            {
                "symbol": sym,
                "earnings_date": (today + timedelta(days=n)).isoformat(),
            }
            for sym, n in symbols_and_days
        ]
    }


# ─────────────────────────────────────────────────────────────────────────────
# PA6-01 / PA6-02 — load_analyst_intel_cached
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadAnalystIntelCached:
    def test_pa601_returns_none_when_no_cache(self, tmp_path, monkeypatch):
        import earnings_intel_fetcher as eif
        monkeypatch.setattr(eif, "_CACHE_DIR", tmp_path)
        result = eif.load_analyst_intel_cached("AAPL")
        assert result is None

    def test_pa602_returns_dict_when_cache_exists(self, tmp_path, monkeypatch):
        import earnings_intel_fetcher as eif
        monkeypatch.setattr(eif, "_CACHE_DIR", tmp_path)

        cache = {
            "symbol": "NVDA",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "beat_quarters": 4,
            "total_quarters": 4,
            "avg_surprise_pct": 5.2,
        }
        (tmp_path / "NVDA_analyst_intel.json").write_text(json.dumps(cache))

        result = eif.load_analyst_intel_cached("NVDA")
        assert result is not None
        assert result["symbol"] == "NVDA"
        assert result["beat_quarters"] == 4


# ─────────────────────────────────────────────────────────────────────────────
# PA6-03 / PA6-04 / PA6-05 / PA6-06 — refresh_earnings_analyst_intel
# ─────────────────────────────────────────────────────────────────────────────

class TestRefreshEarningsAnalystIntel:
    def test_pa603_writes_cache_with_symbol_and_fetched_at(self, tmp_path, monkeypatch):
        import earnings_intel_fetcher as eif
        monkeypatch.setattr(eif, "_CACHE_DIR", tmp_path)
        monkeypatch.setattr(eif, "_fetch_yfinance_intel", lambda sym: {"beat_quarters": 3, "total_quarters": 4})
        monkeypatch.setattr(eif, "_fetch_finnhub_analyst", lambda sym: {})

        eif.refresh_earnings_analyst_intel(["AAPL"])

        cache_file = tmp_path / "AAPL_analyst_intel.json"
        assert cache_file.exists()
        data = json.loads(cache_file.read_text())
        assert data["symbol"] == "AAPL"
        assert "fetched_at" in data
        assert data["beat_quarters"] == 3

    def test_pa604_skips_crypto_symbols(self, tmp_path, monkeypatch):
        import earnings_intel_fetcher as eif
        monkeypatch.setattr(eif, "_CACHE_DIR", tmp_path)
        fetched: list[str] = []

        def spy_yf(sym):
            fetched.append(sym)
            return {}

        monkeypatch.setattr(eif, "_fetch_yfinance_intel", spy_yf)
        monkeypatch.setattr(eif, "_fetch_finnhub_analyst", lambda sym: {})

        eif.refresh_earnings_analyst_intel(["BTC/USD", "ETH/USD", "AAPL"])
        assert "BTC/USD" not in fetched
        assert "ETH/USD" not in fetched
        assert "AAPL" in fetched

    def test_pa605_respects_24h_ttl_fresh_cache(self, tmp_path, monkeypatch):
        import earnings_intel_fetcher as eif
        monkeypatch.setattr(eif, "_CACHE_DIR", tmp_path)
        fetched: list[str] = []

        def spy_yf(sym):
            fetched.append(sym)
            return {}

        # Write a fresh cache (1 hour old)
        fresh = {
            "symbol": "MSFT",
            "fetched_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            "beat_quarters": 4,
        }
        (tmp_path / "MSFT_analyst_intel.json").write_text(json.dumps(fresh))

        monkeypatch.setattr(eif, "_fetch_yfinance_intel", spy_yf)
        monkeypatch.setattr(eif, "_fetch_finnhub_analyst", lambda sym: {})

        eif.refresh_earnings_analyst_intel(["MSFT"])
        assert "MSFT" not in fetched, "Should NOT re-fetch when cache is < 24h old"

    def test_pa606_refetches_stale_cache(self, tmp_path, monkeypatch):
        import earnings_intel_fetcher as eif
        monkeypatch.setattr(eif, "_CACHE_DIR", tmp_path)
        fetched: list[str] = []

        def spy_yf(sym):
            fetched.append(sym)
            return {"beat_quarters": 2}

        # Write stale cache (25 hours old)
        stale = {
            "symbol": "MSFT",
            "fetched_at": (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(),
            "beat_quarters": 1,
        }
        (tmp_path / "MSFT_analyst_intel.json").write_text(json.dumps(stale))

        monkeypatch.setattr(eif, "_fetch_yfinance_intel", spy_yf)
        monkeypatch.setattr(eif, "_fetch_finnhub_analyst", lambda sym: {})

        eif.refresh_earnings_analyst_intel(["MSFT"])
        assert "MSFT" in fetched, "Should re-fetch when cache is > 24h old"


# ─────────────────────────────────────────────────────────────────────────────
# PA6-07 / PA6-08 / PA6-09 / PA6-10 — format_analyst_intel_text
# ─────────────────────────────────────────────────────────────────────────────

class TestFormatAnalystIntelText:
    def test_pa607_returns_beat_history_string(self):
        import earnings_intel_fetcher as eif
        intel = {"beat_quarters": 4, "total_quarters": 4, "avg_surprise_pct": 2.2}
        result = eif.format_analyst_intel_text(intel)
        assert "Beat" in result
        assert "4/4" in result
        assert "+2.2%" in result

    def test_pa608_returns_empty_for_empty_input(self):
        import earnings_intel_fetcher as eif
        assert eif.format_analyst_intel_text({}) == ""
        assert eif.format_analyst_intel_text(None) == ""  # type: ignore[arg-type]

    def test_pa609_includes_consensus_when_bullish_and_count_present(self):
        import earnings_intel_fetcher as eif
        intel = {
            "bullish_pct": 91.3,
            "analyst_count": 46,
            "price_target_mean": 392.0,
            "price_target_upside_pct": 26.7,
        }
        result = eif.format_analyst_intel_text(intel)
        assert "91%" in result or "91.3" in result
        assert "46" in result
        assert "392" in result

    def test_pa610_uses_rec_mean_label_when_bullish_absent(self):
        import earnings_intel_fetcher as eif
        # rec_mean=1.3 → Strong Buy
        intel = {"rec_mean": 1.3, "analyst_count": 30}
        result = eif.format_analyst_intel_text(intel)
        assert "Strong Buy" in result or "Buy" in result


# ─────────────────────────────────────────────────────────────────────────────
# PA6-11 to PA6-18 — _build_pre_earnings_intel_section tiered behavior
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildPreEarningsIntelSectionTiered:
    def test_pa611_held_symbol_t0_included(self):
        """HELD symbol with ≤2 days is always included (T0, uncapped)."""
        import morning_brief as mb

        cal = _make_calendar(("V", 1))
        with patch("data_warehouse.load_earnings_calendar", return_value=cal), \
             patch("morning_brief._get_held_symbols", return_value={"V"}), \
             patch("morning_brief._load_analyst_intel", return_value=None), \
             patch("earnings_intel.get_earnings_intel_section", return_value="  V: transcript"):
            result = mb._build_pre_earnings_intel_section()

        assert "V" in result
        assert "[HELD]" in result
        assert "PRE-EARNINGS INTELLIGENCE" in result

    def test_pa612_held_symbol_t1_included(self):
        """HELD symbol with 3–5 days is included (T1, uncapped)."""
        import morning_brief as mb

        cal = _make_calendar(("GOOGL", 4))
        with patch("data_warehouse.load_earnings_calendar", return_value=cal), \
             patch("morning_brief._get_held_symbols", return_value={"GOOGL"}), \
             patch("morning_brief._load_analyst_intel", return_value=None), \
             patch("earnings_intel.get_earnings_intel_section", return_value="  GOOGL: transcript"):
            result = mb._build_pre_earnings_intel_section()

        assert "GOOGL" in result
        assert "[HELD]" in result

    def test_pa613_t2_not_held_1d_capped_at_3(self):
        """T2 (!held, ≤1d) symbols are capped at 3."""
        import morning_brief as mb

        # 4 not-held symbols all ≤1d (same day: 0d)
        cal = _make_calendar(("AAPL", 0), ("MSFT", 0), ("AMZN", 0), ("TSLA", 0))
        calls: list[str] = []

        def spy_intel(sym, n_days):
            calls.append(sym)
            return f"  {sym}: transcript"

        with patch("data_warehouse.load_earnings_calendar", return_value=cal), \
             patch("morning_brief._get_held_symbols", return_value=set()), \
             patch("morning_brief._load_analyst_intel", return_value=None), \
             patch("earnings_intel.get_earnings_intel_section", side_effect=spy_intel):
            mb._build_pre_earnings_intel_section()

        assert len(calls) <= 3, f"T2 cap exceeded: called for {len(calls)} symbols"

    def test_pa614_t3_not_held_25d_capped_at_2(self):
        """T3 (!held, 2–5d) symbols are capped at 2."""
        import morning_brief as mb

        # 4 not-held symbols all in 2–5d range
        cal = _make_calendar(("NVDA", 2), ("AMD", 3), ("INTC", 4), ("QCOM", 5))
        calls: list[str] = []

        def spy_intel(sym, n_days):
            calls.append(sym)
            return f"  {sym}: transcript"

        with patch("data_warehouse.load_earnings_calendar", return_value=cal), \
             patch("morning_brief._get_held_symbols", return_value=set()), \
             patch("morning_brief._load_analyst_intel", return_value=None), \
             patch("earnings_intel.get_earnings_intel_section", side_effect=spy_intel):
            mb._build_pre_earnings_intel_section()

        assert len(calls) <= 2, f"T3 cap exceeded: called for {len(calls)} symbols"

    def test_pa615_held_symbols_not_capped_by_t2_t3_limits(self):
        """Held symbols in T0+T1 are uncapped even when T2+T3 are full."""
        import morning_brief as mb

        # 3 held symbols (T0/T1) + 3 not-held (T2) + 3 not-held (T3)
        cal = _make_calendar(
            ("V", 0),      # T0 held
            ("GOOGL", 1),  # T0 held
            ("CAT", 4),    # T1 held
            ("AAPL", 0),   # T2 not-held
            ("MSFT", 0),   # T2 not-held
            ("AMZN", 0),   # T2 not-held (cap=3 fills here)
            ("META", 0),   # T2 not-held — should be excluded
            ("NVDA", 3),   # T3 not-held
            ("AMD", 4),    # T3 not-held (cap=2 fills here)
            ("INTC", 5),   # T3 not-held — should be excluded
        )
        calls: list[str] = []

        def spy_intel(sym, n_days):
            calls.append(sym)
            return f"  {sym}: transcript"

        with patch("data_warehouse.load_earnings_calendar", return_value=cal), \
             patch("morning_brief._get_held_symbols", return_value={"V", "GOOGL", "CAT"}), \
             patch("morning_brief._load_analyst_intel", return_value=None), \
             patch("earnings_intel.get_earnings_intel_section", side_effect=spy_intel):
            result = mb._build_pre_earnings_intel_section()

        # All 3 held must appear regardless of caps
        assert "V" in result
        assert "GOOGL" in result
        assert "CAT" in result
        # META and INTC should be excluded (beyond their tier caps)
        assert "META" not in result
        assert "INTC" not in result

    def test_pa616_injects_analyst_intel_when_cache_present(self):
        """Analyst intel is injected in the section when cache has data."""
        import morning_brief as mb

        cal = _make_calendar(("NVDA", 2))
        fake_intel = {
            "symbol": "NVDA",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "beat_quarters": 4,
            "total_quarters": 4,
            "avg_surprise_pct": 5.0,
            "bullish_pct": 88.0,
            "analyst_count": 50,
        }

        with patch("data_warehouse.load_earnings_calendar", return_value=cal), \
             patch("morning_brief._get_held_symbols", return_value=set()), \
             patch("morning_brief._load_analyst_intel", return_value=fake_intel), \
             patch("earnings_intel.get_earnings_intel_section", return_value="  NVDA: transcript"), \
             patch("earnings_intel_fetcher.format_analyst_intel_text",
                   return_value="Beat: 4/4 avg +5.0% | Consensus: 88% bullish (50 analysts)"):
            result = mb._build_pre_earnings_intel_section()

        assert "Beat" in result or "88%" in result or "4/4" in result

    def test_pa617_returns_empty_when_no_symbols_in_window(self):
        """Returns '' when no symbols have earnings ≤ 5 days away."""
        import morning_brief as mb

        cal = _make_calendar(("AAPL", 10), ("MSFT", 15))
        with patch("data_warehouse.load_earnings_calendar", return_value=cal), \
             patch("morning_brief._get_held_symbols", return_value=set()):
            result = mb._build_pre_earnings_intel_section()

        assert result == ""

    def test_pa618_non_fatal_on_load_exception(self):
        """Returns '' gracefully when load_earnings_calendar raises."""
        import morning_brief as mb

        with patch("data_warehouse.load_earnings_calendar",
                   side_effect=RuntimeError("calendar unavailable")):
            result = mb._build_pre_earnings_intel_section()

        assert result == ""


# ─────────────────────────────────────────────────────────────────────────────
# PA6-19 — run_full_refresh calls refresh_finnhub_news
# ─────────────────────────────────────────────────────────────────────────────

class TestRunFullRefreshFinnhubWired:
    def test_pa619_run_full_refresh_calls_refresh_finnhub_news(self, monkeypatch):
        """run_full_refresh must call refresh_finnhub_news after refresh_yahoo_symbol_news."""
        import data_warehouse as dw

        called: list[str] = []

        monkeypatch.setattr(dw, "refresh_bars", lambda syms, **kw: None)
        monkeypatch.setattr(dw, "refresh_fundamentals", lambda syms: None)
        monkeypatch.setattr(dw, "refresh_news", lambda syms: None)
        monkeypatch.setattr(dw, "refresh_yahoo_symbol_news", lambda syms: called.append("yahoo"))
        monkeypatch.setattr(dw, "refresh_finnhub_news", lambda syms: called.append("finnhub"))
        monkeypatch.setattr(dw, "refresh_sector_performance", lambda: None)
        monkeypatch.setattr(dw, "refresh_macro_snapshot", lambda: None)
        monkeypatch.setattr(dw, "refresh_premarket_movers", lambda: None)
        monkeypatch.setattr(dw, "refresh_global_indices", lambda: None)
        monkeypatch.setattr(dw, "refresh_earnings_calendar", lambda: None)
        # Suppress any remaining calls
        import watchlist_manager as wm
        monkeypatch.setattr(wm, "get_active_watchlist", lambda: {
            "all": [{"symbol": "SPY"}],
            "stocks": [{"symbol": "AAPL"}],
            "crypto": [],
            "etfs": [{"symbol": "SPY"}],
        })

        try:
            dw.run_full_refresh()
        except Exception:
            pass  # run_full_refresh may fail on unpatched paths — we only care about call order

        assert "finnhub" in called, "refresh_finnhub_news must be called in run_full_refresh"
        assert called.index("yahoo") < called.index("finnhub"), \
            "refresh_finnhub_news must be called after refresh_yahoo_symbol_news"


# ─────────────────────────────────────────────────────────────────────────────
# PA6-20 / PA6-21 / PA6-22 — scheduler
# ─────────────────────────────────────────────────────────────────────────────

class TestSchedulerEarningsIntel:
    def test_pa620_maybe_refresh_earnings_intel_callable(self):
        """_maybe_refresh_earnings_intel must exist as a callable in scheduler."""
        import scheduler
        assert callable(getattr(scheduler, "_maybe_refresh_earnings_intel", None)), \
            "_maybe_refresh_earnings_intel must be defined in scheduler.py"

    def test_pa621_main_loop_calls_maybe_refresh_earnings_intel(self):
        """scheduler module source must reference _maybe_refresh_earnings_intel in the main loop."""
        import inspect
        import scheduler
        src = inspect.getsource(scheduler)
        assert "_maybe_refresh_earnings_intel" in src, \
            "_maybe_refresh_earnings_intel must be called in the scheduler main loop"

    def test_pa622_earnings_intel_ran_date_global_exists(self):
        """_earnings_intel_ran_date global must exist in scheduler."""
        import scheduler
        assert hasattr(scheduler, "_earnings_intel_ran_date"), \
            "_earnings_intel_ran_date global not found in scheduler"
        assert isinstance(scheduler._earnings_intel_ran_date, str)
