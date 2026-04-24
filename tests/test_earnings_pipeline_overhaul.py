"""
tests/test_earnings_pipeline_overhaul.py

Tests for the earnings pipeline overhaul (Changes 1-7):
  - Change 1: Alpha Vantage earnings calendar builder + yfinance confirm
  - Change 2: watchlist order (core-first) + _MAX_SCORED=80/_BATCH_SIZE=20
              + CORE_SYMBOLS regenerated from JSON
  - Change 3: pending_rotation purge + $3B mkt-cap floor at promotion
              + sector inference + Finnhub removal
  - Change 4: seed_iv_history min_open_interest param + aggregate ATM±5%
              band + fast-track threshold=500
  - Change 5: assert_core_coverage in earnings_calendar_lookup
  - Change 6: RULE_EARNINGS direction-split routing
  - Change 7: cull extracted to _cull_post_earnings_symbols()
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════════════════
# Change 1 — Alpha Vantage earnings calendar
# ═══════════════════════════════════════════════════════════════════════════

AV_SAMPLE_CSV = (
    "symbol,name,reportDate,fiscalDateEnding,estimate,currency,timeOfTheDay\n"
    "GOOGL,ALPHABET INC,2026-04-29,2026-03-31,2.63,USD,post-market\n"
    "AAPL,APPLE INC,2026-04-30,2026-03-31,1.91,USD,post-market\n"
    "V,VISA INC,2026-04-28,2026-03-31,3.09,USD,post-market\n"
    "MA,MASTERCARD INC,2026-04-30,2026-03-31,4.40,USD,pre-market\n"
    "QCOM,QUALCOMM INC,2026-04-29,2026-03-31,1.89,USD,post-market\n"
    # Off-universe micro-cap — should be filtered out
    "BAH,BOOZ ALLEN HAMILTON,2026-05-22,2026-03-31,1.35,USD,pre-market\n"
    # Non-tracked symbol — should be filtered out
    "NOTHERE,NOT TRACKED INC,2026-05-01,2026-03-31,0.10,USD,\n"
)


class TestAVEarningsCalendar:
    """refresh_earnings_calendar_av() exactly-one-call, filtered, merged."""

    def _mock_av_response(self, csv_text: str = AV_SAMPLE_CSV):
        mock_resp = MagicMock()
        mock_resp.text = csv_text
        mock_resp.raise_for_status = MagicMock()
        mock_resp.status_code = 200
        return mock_resp

    def _run_av(self, tmp_path, monkeypatch, universe=None, csv_text=AV_SAMPLE_CSV,
                existing=None):
        import data_warehouse as dw

        market_dir = tmp_path / "market"
        market_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(dw, "MARKET_DIR", market_dir)

        if existing is not None:
            (market_dir / "earnings_calendar.json").write_text(json.dumps(existing))

        # Universe defaults to a large-cap subset
        default_universe = universe if universe is not None else {
            "GOOGL", "AAPL", "V", "MA", "QCOM", "MSFT", "META", "AMZN",
        }
        monkeypatch.setattr(dw, "_get_tracked_universe", lambda: default_universe)

        # Core-stock set used for invariant — default to same core subset
        mock_core = [
            {"symbol": "GOOGL", "type": "stock"},
            {"symbol": "AAPL",  "type": "stock"},
            {"symbol": "MSFT",  "type": "stock"},
        ]
        mock_wm = MagicMock()
        mock_wm.get_core.return_value = mock_core
        monkeypatch.setattr(dw, "wm", mock_wm)

        # Mock AV HTTP call
        mock_get = MagicMock(return_value=self._mock_av_response(csv_text))
        monkeypatch.setattr("requests.get", mock_get)

        # No real Alpaca in tests
        monkeypatch.setattr("os.getenv", lambda k, *a: {
            "ALPHA_VANTAGE_API_KEY": "TEST_KEY",
        }.get(k, ""))

        result = dw.refresh_earnings_calendar_av()
        return result, mock_get

    def test_filters_to_tracked_universe_only(self, tmp_path, monkeypatch):
        result, mock_get = self._run_av(tmp_path, monkeypatch)
        syms = {e["symbol"] for e in result.get("calendar", [])}
        assert "BAH" not in syms       # off-universe filtered
        assert "NOTHERE" not in syms   # not tracked
        assert "GOOGL" in syms
        assert "V" in syms
        assert "MA" in syms

    def test_exactly_one_api_call(self, tmp_path, monkeypatch):
        _, mock_get = self._run_av(tmp_path, monkeypatch)
        assert mock_get.call_count == 1, (
            f"AV endpoint called {mock_get.call_count} times; must be exactly 1"
        )

    def test_entry_shape(self, tmp_path, monkeypatch):
        result, _ = self._run_av(tmp_path, monkeypatch)
        googl = next(e for e in result["calendar"] if e["symbol"] == "GOOGL")
        assert googl["earnings_date"] == "2026-04-29"
        assert googl["timing"] == "post-market"
        assert googl["eps_estimate"] == 2.63
        assert googl["source"] == "alphavantage"
        assert "source_confirmed_at" in googl

    def test_core_invariant_force_add(self, tmp_path, monkeypatch):
        """Core stock present in raw AV data but filtered out must be force-added."""
        import data_warehouse as dw

        # Universe deliberately excludes GOOGL, but core includes it
        self._run_av(
            tmp_path, monkeypatch,
            universe={"AAPL", "V", "MA"},  # no GOOGL
        )
        saved = json.loads((tmp_path / "market" / "earnings_calendar.json").read_text())
        syms = {e["symbol"] for e in saved["calendar"]}
        # GOOGL is in core and in raw AV — must be force-added even if universe excluded it
        assert "GOOGL" in syms, "Core invariant violation: GOOGL missing"

    def test_merge_drops_past_dates(self, tmp_path, monkeypatch):
        existing = {
            "calendar": [
                {"symbol": "OLD1", "earnings_date": "2020-01-01"},
            ],
        }
        result, _ = self._run_av(tmp_path, monkeypatch, existing=existing)
        syms = {e["symbol"] for e in result["calendar"]}
        assert "OLD1" not in syms, "Past-date entry should be aged out"

    def test_merge_preserves_prior_date_on_reschedule(self, tmp_path, monkeypatch):
        existing = {
            "calendar": [
                {
                    "symbol": "V",
                    "earnings_date": "2026-04-29",  # stored date
                    "source": "alphavantage",
                },
            ],
        }
        result, _ = self._run_av(tmp_path, monkeypatch, existing=existing)
        v = next(e for e in result["calendar"] if e["symbol"] == "V")
        # New date from CSV is 2026-04-28 — reschedule detected
        assert v["earnings_date"] == "2026-04-28"
        assert v.get("prior_reported_date") == "2026-04-29"

    def test_empty_csv_returns_empty_dict(self, tmp_path, monkeypatch):
        result, _ = self._run_av(tmp_path, monkeypatch,
                                  csv_text="symbol,reportDate\n")
        # Empty CSV → empty dict (error path)
        assert result == {}

    def test_missing_api_key_returns_empty(self, tmp_path, monkeypatch):
        import data_warehouse as dw
        monkeypatch.setattr("os.getenv", lambda k, *a: "" if k == "ALPHA_VANTAGE_API_KEY" else None)
        result = dw.refresh_earnings_calendar_av()
        assert result == {}


class TestYfinanceConfirm:
    """refresh_earnings_calendar_yfinance_confirm — per-symbol date confirmation."""

    def test_updates_when_date_differs(self, tmp_path, monkeypatch):
        import data_warehouse as dw

        market_dir = tmp_path / "market"
        market_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(dw, "MARKET_DIR", market_dir)

        # Seed with an existing stored date
        (market_dir / "earnings_calendar.json").write_text(json.dumps({
            "calendar": [
                {"symbol": "V", "earnings_date": "2026-04-29", "timing": "post-market"},
            ],
        }))

        # yfinance reports a different date → reschedule
        mock_ticker = MagicMock()
        mock_ticker.calendar = {"Earnings Date": [date(2026, 4, 28)]}
        monkeypatch.setattr("yfinance.Ticker", lambda s: mock_ticker)

        result = dw.refresh_earnings_calendar_yfinance_confirm(["V"])
        assert "V" in result["updated"]

        saved = json.loads((market_dir / "earnings_calendar.json").read_text())
        v = next(e for e in saved["calendar"] if e["symbol"] == "V")
        assert v["earnings_date"] == "2026-04-28"
        assert v.get("prior_reported_date") == "2026-04-29"

    def test_unchanged_when_date_matches(self, tmp_path, monkeypatch):
        import data_warehouse as dw

        market_dir = tmp_path / "market"
        market_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(dw, "MARKET_DIR", market_dir)

        (market_dir / "earnings_calendar.json").write_text(json.dumps({
            "calendar": [
                {"symbol": "V", "earnings_date": "2026-04-28"},
            ],
        }))

        mock_ticker = MagicMock()
        mock_ticker.calendar = {"Earnings Date": [date(2026, 4, 28)]}
        monkeypatch.setattr("yfinance.Ticker", lambda s: mock_ticker)

        result = dw.refresh_earnings_calendar_yfinance_confirm(["V"])
        assert "V" in result["unchanged"]


# ═══════════════════════════════════════════════════════════════════════════
# Change 2 — watchlist order + scoring cap + CORE_SYMBOLS
# ═══════════════════════════════════════════════════════════════════════════

class TestWatchlistOrder:
    """Core symbols must precede rotation in watchlist iteration."""

    def test_core_before_rotation(self, tmp_path, monkeypatch):
        import watchlist_manager as wm

        # Arrange: rotation written first (old bug reproduced), core from JSON
        rotation_file = tmp_path / "rotation.json"
        rotation_file.write_text(json.dumps({
            "symbols": [
                {"symbol": "ROT1", "type": "stock", "sector": "unknown"},
                {"symbol": "ROT2", "type": "stock", "sector": "unknown"},
            ],
        }))
        monkeypatch.setattr(wm, "ROTATION_FILE",  rotation_file)
        monkeypatch.setattr(wm, "DYNAMIC_FILE",   tmp_path / "dyn.json")
        monkeypatch.setattr(wm, "INTRADAY_FILE",  tmp_path / "intr.json")

        wl = wm.get_active_watchlist()
        stocks = wl["stocks"]

        # Every core stock must appear before any rotation stock in the list.
        core_stock_set = {e["symbol"] for e in wm.get_core() if e.get("type") == "stock"}
        first_rotation_idx = None
        last_core_idx = -1
        for i, sym in enumerate(stocks):
            if sym in core_stock_set:
                last_core_idx = i
            elif sym in ("ROT1", "ROT2") and first_rotation_idx is None:
                first_rotation_idx = i
        assert first_rotation_idx is None or last_core_idx < first_rotation_idx


class TestScoringCaps:
    """Scoring cap and batch size invariants after the 3-layer overhaul.

    Current architecture:
      - `_BATCH_SIZE = 10` for the L3 Haiku synthesis layer (new public
        entry point `score_signals_layered`). L3 carries L1+L2 context
        per symbol, so each symbol's input block is 2-3× larger than the
        legacy single-layer scorer's — batch size is halved to keep each
        call within ~1.5K input tokens.
      - `_LEGACY_BATCH_SIZE = 20` is retained for the fallback
        `score_signals()` path.
      - `_MAX_SCORED = 80` inline in both `score_signals_layered` and
        the legacy `score_signals` — assert via source inspection.
    """

    def test_batch_size_is_10_for_L3(self):
        import bot_stage2_signal as s
        assert s._BATCH_SIZE == 10

    def test_legacy_batch_size_preserved_at_20(self):
        import bot_stage2_signal as s
        assert getattr(s, "_LEGACY_BATCH_SIZE", None) == 20

    def test_max_scored_is_80(self):
        """Inline constant set inside both score_signals_layered and
        the legacy score_signals — verify via source inspection."""
        import inspect

        import bot_stage2_signal as s
        src_layered = inspect.getsource(s.score_signals_layered)
        assert "_MAX_SCORED = 80" in src_layered
        src_legacy = inspect.getsource(s.score_signals)
        assert "_MAX_SCORED = 80" in src_legacy


class TestCoreSymbolsFromJson:
    """CORE_SYMBOLS regenerated from watchlist_core.json at import."""

    def test_core_symbols_matches_json(self):
        from watchlist_manager import CORE_SYMBOLS, CORE_FILE
        raw = json.loads(CORE_FILE.read_text())
        expected = {e["symbol"].upper() for e in raw["symbols"] if e.get("symbol")}
        assert CORE_SYMBOLS == expected

    def test_drift_symbols_absent(self):
        """AMD, AVGO, TSLA, PANW, HD, SNOW were hardcoded drift — must be absent."""
        from watchlist_manager import CORE_SYMBOLS
        drift = {"AMD", "AVGO", "TSLA", "PANW", "HD", "SNOW"}
        assert not (drift & CORE_SYMBOLS)


# ═══════════════════════════════════════════════════════════════════════════
# Change 3 — rotation admission cleanup
# ═══════════════════════════════════════════════════════════════════════════

class TestPendingRotationPurge:
    """Stale off-universe symbols in pending_rotation.json must be purged
    on next run_earnings_rotation() invocation."""

    def test_purge_removes_off_universe_symbols(self, tmp_path, monkeypatch):
        import earnings_rotation as er

        # Pre-seed pending with a mix of on- and off-universe symbols
        pending_path = tmp_path / "pending.json"
        pending_path.write_text(json.dumps({
            "symbols": [
                {"symbol": "NFLX",    "earnings_date": "2026-05-10",
                 "dte_at_add": 20, "source": "yfinance", "added_at": "2026-04-10"},
                {"symbol": "BAH",     "earnings_date": "2026-05-22",
                 "dte_at_add": 30, "source": "finnhub",  "added_at": "2026-04-22"},
                {"symbol": "NOTHERE", "earnings_date": "2026-05-15",
                 "dte_at_add": 25, "source": "finnhub",  "added_at": "2026-04-22"},
            ],
        }))
        monkeypatch.setattr(er, "_PENDING_PATH", pending_path)

        monkeypatch.setattr(er, "_fetch_yfinance_earnings",
                           lambda t, lookforward=30: [])
        monkeypatch.setattr(er, "get_rotation", lambda: [])
        monkeypatch.setattr(er, "get_active_watchlist",
                           lambda: {"all": [{"symbol": "GOOGL", "type": "stock"}]})
        monkeypatch.setattr(er, "_REPORTS_DIR", tmp_path / "reports")

        er.run_earnings_rotation(config=None)

        remaining = json.loads(pending_path.read_text())["symbols"]
        syms = {s["symbol"] for s in remaining}
        assert "NFLX" in syms, "NFLX is in _EXTRA_UNIVERSE — must survive purge"
        assert "BAH" not in syms, "BAH is off-universe — must be purged"
        assert "NOTHERE" not in syms, "NOTHERE is off-universe — must be purged"


class TestMktCapFloor:
    """$3B mkt-cap floor at promotion-time. Core symbols bypass."""

    def test_under_floor_blocked(self, monkeypatch):
        import earnings_rotation as er

        mock_info = MagicMock()
        mock_info.market_cap = 500_000_000  # $500M — below $3B
        mock_ticker = MagicMock()
        mock_ticker.fast_info = mock_info
        monkeypatch.setattr("yfinance.Ticker", lambda s: mock_ticker)

        assert er._passes_mkt_cap_floor("MICRO") is False

    def test_above_floor_passes(self, monkeypatch):
        import earnings_rotation as er

        mock_info = MagicMock()
        mock_info.market_cap = 50_000_000_000  # $50B
        mock_ticker = MagicMock()
        mock_ticker.fast_info = mock_info
        monkeypatch.setattr("yfinance.Ticker", lambda s: mock_ticker)

        assert er._passes_mkt_cap_floor("LARGE") is True

    def test_missing_cap_fails_open(self, monkeypatch):
        import earnings_rotation as er

        mock_info = MagicMock()
        mock_info.market_cap = None
        mock_ticker = MagicMock()
        mock_ticker.fast_info = mock_info
        mock_ticker.fast_info.get = MagicMock(return_value=None)
        monkeypatch.setattr("yfinance.Ticker", lambda s: mock_ticker)

        assert er._passes_mkt_cap_floor("UNKNOWN") is True


class TestSectorInference:
    """_infer_sector replaces hardcoded 'technology' label."""

    def test_falls_back_to_unknown(self, monkeypatch):
        import earnings_rotation as er

        # Force portfolio_intelligence import to return empty map
        import sys
        import types
        stub = types.ModuleType("portfolio_intelligence")
        stub._SYMBOL_SECTOR = {}
        monkeypatch.setitem(sys.modules, "portfolio_intelligence", stub)

        assert er._infer_sector("UNMAPPED") == "unknown"

    def test_reads_symbol_sector_map(self, monkeypatch):
        import earnings_rotation as er

        import sys
        import types
        stub = types.ModuleType("portfolio_intelligence")
        stub._SYMBOL_SECTOR = {"AAPL": "technology", "V": "financials"}
        monkeypatch.setitem(sys.modules, "portfolio_intelligence", stub)

        assert er._infer_sector("AAPL") == "technology"
        assert er._infer_sector("V") == "financials"


class TestFinnhubRemoved:
    """_fetch_finnhub_earnings must no longer exist."""

    def test_finnhub_fn_removed(self):
        import earnings_rotation as er
        assert not hasattr(er, "_fetch_finnhub_earnings")


# ═══════════════════════════════════════════════════════════════════════════
# Change 4 — IV fast-track OI fixes
# ═══════════════════════════════════════════════════════════════════════════

class TestSeedIVHistoryMinOI:
    """seed_iv_history accepts min_open_interest and threads it through."""

    def test_seed_iv_history_signature(self):
        import inspect

        import iv_history_seeder as s
        sig = inspect.signature(s.seed_iv_history)
        assert "min_open_interest" in sig.parameters
        assert sig.parameters["min_open_interest"].default is None

    def test_fetch_atm_iv_signature(self):
        import inspect

        import iv_history_seeder as s
        sig = inspect.signature(s._fetch_atm_iv_yfinance)
        assert "min_open_interest" in sig.parameters


class TestAggregateATMOI:
    """_fetch_atm_iv_yfinance sums OI across ATM ±5% band, not single strike."""

    def _mock_chain(self, calls: list[dict], puts: list[dict], spot: float):
        chain = MagicMock()
        if calls:
            import pandas as pd
            chain.calls = pd.DataFrame(calls)
        else:
            chain.calls = MagicMock(empty=True)
        if puts:
            import pandas as pd
            chain.puts = pd.DataFrame(puts)
        else:
            chain.puts = MagicMock(empty=True)
        return chain

    def test_aggregates_across_band_passes(self, monkeypatch):
        """Single-strike OI=30 but aggregate band OI=300 → passes min_open_interest=50."""
        import iv_history_seeder as s

        spot = 100.0
        # 5 strikes in ±5% band; each with OI=60 → aggregate=600 across 10 (calls+puts)
        calls = [
            {"strike": 95.0,  "openInterest": 60, "impliedVolatility": 0.30},
            {"strike": 97.5,  "openInterest": 60, "impliedVolatility": 0.30},
            {"strike": 100.0, "openInterest": 60, "impliedVolatility": 0.30},
            {"strike": 102.5, "openInterest": 60, "impliedVolatility": 0.30},
            {"strike": 105.0, "openInterest": 60, "impliedVolatility": 0.30},
        ]
        puts = [
            {"strike": 95.0,  "openInterest": 60, "impliedVolatility": 0.32},
            {"strike": 97.5,  "openInterest": 60, "impliedVolatility": 0.32},
            {"strike": 100.0, "openInterest": 60, "impliedVolatility": 0.32},
            {"strike": 102.5, "openInterest": 60, "impliedVolatility": 0.32},
            {"strike": 105.0, "openInterest": 60, "impliedVolatility": 0.32},
        ]
        expiry = (date.today() + timedelta(days=14)).isoformat()

        mock_ticker = MagicMock()
        mock_ticker.options = [expiry]
        mock_ticker.fast_info.last_price = spot
        mock_ticker.option_chain = MagicMock(return_value=self._mock_chain(calls, puts, spot))
        monkeypatch.setattr("yfinance.Ticker", lambda sym: mock_ticker)

        iv, used_exp, meta = s._fetch_atm_iv_yfinance("TESTSYM", min_open_interest=50)
        assert iv is not None
        assert meta.get("aggregate_oi") == 600
        assert meta.get("n_strikes") == 10
        assert meta.get("min_oi_used") == 50

    def test_aggregates_block_when_below_threshold(self, monkeypatch):
        """Aggregate OI=100 fails min_open_interest=500."""
        import iv_history_seeder as s

        spot = 100.0
        calls = [
            {"strike": 100.0, "openInterest": 50, "impliedVolatility": 0.30},
        ]
        puts = [
            {"strike": 100.0, "openInterest": 50, "impliedVolatility": 0.32},
        ]
        expiry = (date.today() + timedelta(days=14)).isoformat()
        mock_ticker = MagicMock()
        mock_ticker.options = [expiry]
        mock_ticker.fast_info.last_price = spot
        mock_ticker.option_chain = MagicMock(return_value=self._mock_chain(calls, puts, spot))
        monkeypatch.setattr("yfinance.Ticker", lambda sym: mock_ticker)

        iv, used_exp, meta = s._fetch_atm_iv_yfinance("THIN", min_open_interest=500)
        assert iv is None
        assert "oi_too_low" in meta.get("error", "")


class TestFasttrackUsesThreshold500:
    """earnings_iv_fasttrack passes min_open_interest=500 to seed_iv_history."""

    def test_fasttrack_passes_500(self, tmp_path, monkeypatch):
        import options_universe_manager as oum

        iv_dir = tmp_path / "iv_history"
        iv_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(oum, "_DATA_DIR",        tmp_path)
        monkeypatch.setattr(oum, "_UNIVERSE_FILE",   tmp_path / "universe.json")
        monkeypatch.setattr(oum, "_BOOTSTRAP_QUEUE", tmp_path / "queue.json")
        monkeypatch.setattr(oum, "_IV_DIR",          iv_dir)

        captured: list[dict] = []

        def fake_seed(symbols, target_days=25, dry_run=False, min_open_interest=None):
            captured.append({"min_oi": min_open_interest})
            # Write a passing history so _has_sufficient_iv_history returns True
            for s in symbols:
                entries = [{"date": f"2026-03-{i+1:02d}", "iv": 0.25} for i in range(25)]
                (iv_dir / f"{s}_iv_history.json").write_text(json.dumps(entries))
            return {
                "seeded": list(symbols), "skipped": [], "failed": [],
                "entries_added": 25, "quality_summary": {s: "A" for s in symbols},
            }

        with patch("iv_history_seeder.seed_iv_history", side_effect=fake_seed):
            ok = oum.earnings_iv_fasttrack("V", date(2026, 4, 28))

        assert ok is True
        assert len(captured) == 1
        assert captured[0]["min_oi"] == 500


# ═══════════════════════════════════════════════════════════════════════════
# Change 5 — core earnings coverage
# ═══════════════════════════════════════════════════════════════════════════

class TestAssertCoreCoverage:
    """assert_core_coverage warns about missing core symbols."""

    def test_all_present_returns_empty(self, monkeypatch):
        import earnings_calendar_lookup as ecl

        mock_core = [
            {"symbol": "AAPL", "type": "stock"},
            {"symbol": "MSFT", "type": "stock"},
        ]
        mock_wm = MagicMock()
        mock_wm.get_core.return_value = mock_core
        import sys
        monkeypatch.setitem(sys.modules, "watchlist_manager", mock_wm)

        missing = ecl.assert_core_coverage({"AAPL": 5, "MSFT": 3})
        assert missing == []

    def test_missing_core_returned(self, monkeypatch):
        import earnings_calendar_lookup as ecl

        mock_core = [
            {"symbol": "AAPL", "type": "stock"},
            {"symbol": "MSFT", "type": "stock"},
            {"symbol": "GOOGL", "type": "stock"},
        ]
        mock_wm = MagicMock()
        mock_wm.get_core.return_value = mock_core
        import sys
        monkeypatch.setitem(sys.modules, "watchlist_manager", mock_wm)

        missing = ecl.assert_core_coverage({"AAPL": 5})
        assert set(missing) == {"MSFT", "GOOGL"}

    def test_etfs_excluded(self, monkeypatch):
        import earnings_calendar_lookup as ecl

        mock_core = [
            {"symbol": "AAPL", "type": "stock"},
            {"symbol": "SPY",  "type": "etf"},
        ]
        mock_wm = MagicMock()
        mock_wm.get_core.return_value = mock_core
        import sys
        monkeypatch.setitem(sys.modules, "watchlist_manager", mock_wm)

        # SPY is etf — should not be checked
        missing = ecl.assert_core_coverage({"AAPL": 5})
        assert missing == []


# ═══════════════════════════════════════════════════════════════════════════
# Change 6 — RULE_EARNINGS direction-split
# ═══════════════════════════════════════════════════════════════════════════

class TestRuleEarningsDirectional:
    """_route_strategy RULE_EARNINGS respects a1_direction."""

    def _pack(self, direction: str, dte: int = 8, iv_rank: float = 40.0):
        """Minimal A2FeaturePack-like object for routing tests."""
        pack = MagicMock()
        pack.symbol = "TESTSYM"
        pack.earnings_days_away = dte
        pack.a1_direction = direction
        pack.iv_rank = iv_rank
        pack.iv_environment = "neutral"
        pack.liquidity_score = 0.8
        pack.macro_event_flag = False
        return pack

    def test_bullish_returns_debit_call_and_straddle(self):
        from bot_options_stage2_structures import _route_strategy
        pack = self._pack("bullish")
        allowed = _route_strategy(pack)
        assert "debit_call_spread" in allowed
        assert "straddle" in allowed

    def test_bearish_returns_debit_put_and_straddle(self):
        from bot_options_stage2_structures import _route_strategy
        pack = self._pack("bearish")
        allowed = _route_strategy(pack)
        assert "debit_put_spread" in allowed
        assert "straddle" in allowed

    def test_neutral_returns_straddle_only(self):
        from bot_options_stage2_structures import _route_strategy
        pack = self._pack("neutral")
        allowed = _route_strategy(pack)
        assert allowed == ["straddle"]

    def test_blackout_earnings_dte_le_5_returns_empty(self):
        from bot_options_stage2_structures import _route_strategy
        pack = self._pack("bullish", dte=3)
        allowed = _route_strategy(pack)
        assert allowed == []

    def test_elevated_iv_rank_skips_earnings_rule(self):
        """iv_rank >= 70 bypasses RULE_EARNINGS — different rule fires."""
        from bot_options_stage2_structures import _route_strategy
        pack = self._pack("bullish", dte=8, iv_rank=75.0)
        allowed = _route_strategy(pack)
        # With iv_rank=75 and iv_env=neutral and dir=bullish, RULE6 fires
        # → ["debit_call_spread", "debit_put_spread"] (no straddle).
        # RULE_EARNINGS would have returned ["debit_call_spread", "straddle"].
        assert "straddle" not in allowed


# ═══════════════════════════════════════════════════════════════════════════
# Change 7 — cull extracted to 2 AM scheduler path
# ═══════════════════════════════════════════════════════════════════════════

class TestCullExtracted:
    """_cull_post_earnings_symbols callable independently; run_earnings_rotation no longer culls."""

    def test_cull_helper_exists(self):
        import earnings_rotation as er
        assert callable(er._cull_post_earnings_symbols)

    def test_cull_removes_past_symbols(self, tmp_path, monkeypatch):
        import earnings_rotation as er
        import watchlist_manager as wm

        rotation_file = tmp_path / "rot.json"
        rotation_file.write_text(json.dumps({
            "symbols": [
                {
                    "symbol":                  "EXPIRED",
                    "added_by":                "earnings_rotation",
                    "post_earnings_cull_after": (date.today() - timedelta(days=2)).isoformat(),
                    "type": "stock", "tier": "dynamic", "sector": "unknown",
                    "earnings_date": "2026-01-01", "earnings_rotation_added_at": "2026-01-01",
                    "added_on": "2026-01-01",
                },
                {
                    "symbol":                  "STILL_FRESH",
                    "added_by":                "earnings_rotation",
                    "post_earnings_cull_after": (date.today() + timedelta(days=5)).isoformat(),
                    "type": "stock", "tier": "dynamic", "sector": "unknown",
                    "earnings_date": "2026-05-15", "earnings_rotation_added_at": "2026-04-01",
                    "added_on": "2026-04-01",
                },
            ],
        }))
        monkeypatch.setattr(wm, "ROTATION_FILE", rotation_file)
        # Clear CORE protection so "EXPIRED" isn't kept artificially
        monkeypatch.setattr(er, "CORE_SYMBOLS", frozenset({"AAPL"}))

        culled = er._cull_post_earnings_symbols()
        assert any(c["symbol"] == "EXPIRED" for c in culled)
        after = wm.get_rotation()
        assert not any(s["symbol"] == "EXPIRED" for s in after)
        assert any(s["symbol"] == "STILL_FRESH" for s in after)

    def test_run_earnings_rotation_no_longer_culls(self, tmp_path, monkeypatch):
        """Sanity: run_earnings_rotation returns culled=0 even with stale entries."""
        import earnings_rotation as er
        import watchlist_manager as wm

        rotation_file = tmp_path / "rot.json"
        rotation_file.write_text(json.dumps({
            "symbols": [
                {
                    "symbol":                  "OLD",
                    "added_by":                "earnings_rotation",
                    "post_earnings_cull_after": (date.today() - timedelta(days=10)).isoformat(),
                    "type": "stock", "tier": "dynamic", "sector": "unknown",
                    "earnings_date": "2026-01-01", "earnings_rotation_added_at": "2026-01-01",
                    "added_on": "2026-01-01",
                },
            ],
        }))
        monkeypatch.setattr(wm, "ROTATION_FILE", rotation_file)
        monkeypatch.setattr(er, "_PENDING_PATH", tmp_path / "pending.json")
        monkeypatch.setattr(er, "_REPORTS_DIR",  tmp_path / "reports")
        monkeypatch.setattr(er, "_fetch_yfinance_earnings", lambda t, lookforward=30: [])
        monkeypatch.setattr(er, "CORE_SYMBOLS", frozenset({"AAPL"}))

        result = er.run_earnings_rotation(config=None)
        assert result["culled"] == 0

    def test_scheduler_cull_window_is_2_AM(self):
        """_maybe_cull_post_earnings should only run between 2:00 and 3:00 AM ET."""
        import inspect

        import scheduler
        src = inspect.getsource(scheduler._maybe_cull_post_earnings)
        # Window check: 2 * 60 ≤ now_min ≤ 3 * 60
        assert "2 * 60" in src and "3 * 60" in src, (
            "Cull window must be 2:00-3:00 AM (2 * 60 minutes)"
        )
