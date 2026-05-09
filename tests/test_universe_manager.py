"""
tests/test_universe_manager.py — universe_manager.py unit tests.

Tests (a)–(h) per spec:
  a. _is_us_listed: true for clean symbols, false for OTC/ADR patterns
  b. _get_market_cap: returns cached value from fundamentals JSON
  c. fetch_earnings_rotation_candidates: empty AV response returns []
  d. fetch_earnings_rotation_candidates: filters to DTE <= ROTATION_ENTRY_DTE
  e. fetch_earnings_rotation_candidates: fail-closed — excludes symbol with None market cap
  f. sync_dynamic_watchlist: adds new candidate to watchlist_dynamic.json
  g. sync_dynamic_watchlist: removes expired earnings entries (dte < -3), keeps manual entries
  h. get_universe_snapshot: reads core + dynamic files, returns expected structure
"""

from __future__ import annotations

import json
import sys
import types
from datetime import date, timedelta
from unittest.mock import patch

# ── Stub yfinance before any import touches it ─────────────────────────────
_yf_stub = types.ModuleType("yfinance")
sys.modules.setdefault("yfinance", _yf_stub)

import universe_manager as um  # noqa: E402

# ── (a) _is_us_listed ──────────────────────────────────────────────────────

def test_is_us_listed_accepts_clean_symbols():
    assert um._is_us_listed("AAPL") is True
    assert um._is_us_listed("MSFT") is True
    assert um._is_us_listed("CRWD") is True
    assert um._is_us_listed("META") is True


def test_is_us_listed_rejects_foreign_otc():
    assert um._is_us_listed("ALIZF") is False   # len>4 ends in F
    assert um._is_us_listed("NSRGY") is False   # len>4 ends in Y
    assert um._is_us_listed("ASMLF") is False
    assert um._is_us_listed("NSRGF") is False
    assert um._is_us_listed("BRK.B") is False   # dot
    assert um._is_us_listed("BF-B")  is False   # dash


# ── (b) _get_market_cap: returns fundamentals cache ───────────────────────

def test_get_market_cap_reads_cache(tmp_path):
    fund_dir = tmp_path / "fundamentals"
    fund_dir.mkdir()
    (fund_dir / "AAPL.json").write_text(json.dumps({"market_cap": 3_000_000_000_000}))

    with patch.object(um, "_FUND_DIR", fund_dir):
        cap = um._get_market_cap("AAPL")

    assert cap == 3_000_000_000_000.0


# ── (c) fetch_earnings_rotation_candidates: empty AV → [] ─────────────────

def test_fetch_candidates_empty_av():
    with patch.object(um, "_fetch_av_raw", return_value=[]):
        result = um.fetch_earnings_rotation_candidates("dummy_key")
    assert result == []


# ── (d) fetch_earnings_rotation_candidates: DTE filter ────────────────────

def test_fetch_candidates_dte_filter():
    today = date(2026, 5, 8)
    entries = [
        {"symbol": "AAPL", "earnings_date": today + timedelta(days=5),  "timing": "post_market"},
        {"symbol": "MSFT", "earnings_date": today + timedelta(days=30), "timing": "pre_market"},
    ]

    def mock_cap(sym):
        return 3_000_000_000_000.0

    with (
        patch.object(um, "_fetch_av_raw", return_value=entries),
        patch.object(um, "_get_market_cap", side_effect=mock_cap),
    ):
        result = um.fetch_earnings_rotation_candidates("key", sim_date=today)

    syms = {r["symbol"] for r in result}
    assert "AAPL" in syms       # DTE=5, within 21
    assert "MSFT" not in syms   # DTE=30, beyond window


# ── (e) fetch_earnings_rotation_candidates: fail-closed on None market cap ─

def test_fetch_candidates_fail_closed_no_market_cap():
    today = date(2026, 5, 8)
    entries = [
        {"symbol": "AAPL", "earnings_date": today + timedelta(days=3), "timing": "post_market"},
        {"symbol": "SMCI", "earnings_date": today + timedelta(days=4), "timing": "unknown"},
    ]

    def mock_cap(sym):
        return 3_000_000_000_000.0 if sym == "AAPL" else None

    with (
        patch.object(um, "_fetch_av_raw", return_value=entries),
        patch.object(um, "_get_market_cap", side_effect=mock_cap),
    ):
        result = um.fetch_earnings_rotation_candidates("key", sim_date=today)

    syms = {r["symbol"] for r in result}
    assert "AAPL" in syms
    assert "SMCI" not in syms   # None cap → excluded (fail-closed)


# ── (f) sync_dynamic_watchlist: adds new candidates ───────────────────────

def test_sync_adds_new_candidate(tmp_path):
    dynamic_path = tmp_path / "watchlist_dynamic.json"
    dynamic_path.write_text(json.dumps({"symbols": [], "reset_at": ""}))

    today = date(2026, 5, 8)
    candidates = [
        {
            "symbol":        "NVDA",
            "earnings_date": today + timedelta(days=7),
            "dte":           7,
            "timing":        "post_market",
            "market_cap_b":  2800.0,
        }
    ]

    with (
        patch.object(um, "_DYNAMIC_PATH", dynamic_path),
        patch.object(um, "fetch_earnings_rotation_candidates", return_value=candidates),
    ):
        summary = um.sync_dynamic_watchlist("key", sim_date=today)

    assert len(summary["added"]) == 1
    assert summary["added"][0]["symbol"] == "NVDA"

    saved = json.loads(dynamic_path.read_text())
    saved_syms = {e["symbol"] for e in saved["symbols"]}
    assert "NVDA" in saved_syms
    assert saved["symbols"][0]["tier"] == "earnings"


# ── (g) sync_dynamic_watchlist: removes expired, keeps manual ─────────────

def test_sync_removes_expired_keeps_manual(tmp_path):
    today = date(2026, 5, 8)
    expired_date = (today + timedelta(days=-5)).isoformat()   # dte = -5
    active_date  = (today + timedelta(days=3)).isoformat()    # dte =  3

    initial = {
        "symbols": [
            {"symbol": "EXPIRED", "tier": "earnings", "earnings_date": expired_date,
             "sector": "tech", "type": "stock"},
            {"symbol": "ACTIVE",  "tier": "earnings", "earnings_date": active_date,
             "sector": "tech", "type": "stock"},
            {"symbol": "MANUAL",  "tier": "core",     "earnings_date": "",
             "sector": "tech", "type": "stock"},
        ],
        "reset_at": "",
    }
    dynamic_path = tmp_path / "watchlist_dynamic.json"
    dynamic_path.write_text(json.dumps(initial))

    with (
        patch.object(um, "_DYNAMIC_PATH", dynamic_path),
        patch.object(um, "fetch_earnings_rotation_candidates", return_value=[]),
    ):
        summary = um.sync_dynamic_watchlist("key", sim_date=today)

    removed_syms = {r["symbol"] for r in summary["removed"]}
    assert "EXPIRED" in removed_syms

    saved = json.loads(dynamic_path.read_text())
    saved_syms = {e["symbol"] for e in saved["symbols"]}
    assert "EXPIRED" not in saved_syms
    assert "ACTIVE"  in saved_syms
    assert "MANUAL"  in saved_syms   # tier != "earnings" — never removed


# ── (h) get_universe_snapshot: reads core + dynamic ───────────────────────

def test_get_universe_snapshot(tmp_path):
    core_data = {
        "symbols": [
            {"symbol": "SPY", "tier": "core"},
            {"symbol": "QQQ", "tier": "core"},
        ]
    }
    today = date(2026, 5, 8)
    dynamic_data = {
        "symbols": [
            {
                "symbol": "NVDA", "tier": "earnings",
                "earnings_date": (today + timedelta(days=5)).isoformat(),
                "timing": "post_market", "market_cap_b": 2800.0,
            },
            {"symbol": "AAPL", "tier": "custom"},
        ],
        "reset_at": "",
    }

    core_path    = tmp_path / "watchlist_core.json"
    dynamic_path = tmp_path / "watchlist_dynamic.json"
    core_path.write_text(json.dumps(core_data))
    dynamic_path.write_text(json.dumps(dynamic_data))

    with (
        patch.object(um, "_CORE_PATH",    core_path),
        patch.object(um, "_DYNAMIC_PATH", dynamic_path),
    ):
        snap = um.get_universe_snapshot(sim_date=today)

    assert "SPY"  in snap["core_symbols"]
    assert "QQQ"  in snap["core_symbols"]
    assert any(e["symbol"] == "NVDA" for e in snap["dynamic_earnings"])
    assert "AAPL" in snap["dynamic_manual"]
    assert set(snap["all_symbols"]) == {"SPY", "QQQ", "NVDA", "AAPL"}
