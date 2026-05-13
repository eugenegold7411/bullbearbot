"""
test_universe_guard.py — NEW-7: Stage 3 watchlist bypass prevention.

UG-01  New entry not in any watchlist → blocked, logged at WARNING
UG-02  New entry found in watchlist_core → allowed
UG-03  Symbol already held → allowed even if not in watchlist (ADD exempt)
UG-04  Watchlist load failure → fails open (no symbols blocked)
UG-05  buy action filtered from actions list when universe_guard blocks it
UG-06  Symbol found in watchlist_dynamic only → allowed
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

import bot_stage4_execution as _s4
from bot_stage4_execution import universe_guard


# ─── helpers ──────────────────────────────────────────────────────────────────

def _make_position(symbol: str):
    p = MagicMock()
    p.symbol = symbol
    return p


@pytest.fixture()
def wl_root(tmp_path, monkeypatch):
    """Point _WATCHLIST_ROOT at tmp_path and write empty watchlist stubs."""
    (tmp_path / "watchlist_core.json").write_text(json.dumps({}))
    (tmp_path / "watchlist_dynamic.json").write_text(json.dumps({}))
    monkeypatch.setattr(_s4, "_WATCHLIST_ROOT", tmp_path)
    return tmp_path


# ─── UG-01: symbol not in any watchlist is blocked ────────────────────────────

def test_ug01_unknown_symbol_blocked(wl_root):
    (wl_root / "watchlist_core.json").write_text(json.dumps({"tech": ["AAPL", "MSFT"]}))
    candidates = [{"action": "buy", "symbol": "BIDU"}]
    blocked = universe_guard(candidates, [])
    assert "BIDU" in blocked


# ─── UG-02: symbol found in watchlist_core → allowed ─────────────────────────

def test_ug02_watchlist_core_symbol_allowed(wl_root):
    (wl_root / "watchlist_core.json").write_text(json.dumps({"tech": ["AAPL", "MSFT"]}))
    candidates = [{"action": "buy", "symbol": "AAPL"}]
    blocked = universe_guard(candidates, [])
    assert "AAPL" not in blocked


# ─── UG-06: symbol found in watchlist_dynamic only → allowed ─────────────────

def test_ug06_watchlist_dynamic_symbol_allowed(wl_root):
    (wl_root / "watchlist_dynamic.json").write_text(
        json.dumps({"earnings": ["NVDA"], "dynamic": ["CRWD"]})
    )
    candidates = [{"action": "buy", "symbol": "NVDA"}]
    blocked = universe_guard(candidates, [])
    assert "NVDA" not in blocked


# ─── UG-03: already-held symbol exempt regardless of watchlist ────────────────

def test_ug03_held_symbol_exempt(wl_root):
    # watchlists are empty — BIDU would normally be blocked
    candidates = [{"action": "buy", "symbol": "BIDU"}]
    positions  = [_make_position("BIDU")]
    blocked = universe_guard(candidates, positions)
    assert "BIDU" not in blocked


# ─── UG-04: watchlist load failure → fails open ──────────────────────────────

def test_ug04_load_failure_fails_open(monkeypatch, tmp_path):
    """When the watchlist files don't exist the guard fails open."""
    # Point at empty dir (no watchlist files) — reads will raise FileNotFoundError
    empty_dir = tmp_path / "missing"
    monkeypatch.setattr(_s4, "_WATCHLIST_ROOT", empty_dir)
    candidates = [{"action": "buy", "symbol": "BIDU"}]
    # Should not raise, should return empty set (fail open)
    blocked = universe_guard(candidates, [])
    assert blocked == set()


# ─── UG-05: bot.py filtering pattern removes blocked buy from actions ─────────

def test_ug05_actions_filtered_correctly(wl_root):
    (wl_root / "watchlist_core.json").write_text(json.dumps({"tech": ["AAPL"]}))

    actions = [
        {"action": "buy",   "symbol": "BIDU"},   # blocked
        {"action": "buy",   "symbol": "AAPL"},   # allowed
        {"action": "close", "symbol": "BIDU"},   # non-buy — kept regardless
    ]
    remaining_buys = [a for a in actions if a.get("action") == "buy"]
    blocked = universe_guard(remaining_buys, [])

    filtered = [
        a for a in actions
        if not (a.get("action") == "buy" and a.get("symbol") in blocked)
    ]
    action_pairs = [(a["action"], a["symbol"]) for a in filtered]

    assert ("buy",   "BIDU")  not in action_pairs   # blocked buy removed
    assert ("buy",   "AAPL")  in action_pairs        # allowed buy kept
    assert ("close", "BIDU")  in action_pairs        # non-buy kept regardless
