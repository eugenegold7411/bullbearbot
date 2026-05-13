"""
tests/test_a2_rejection_log.py — A2 rejection feedback loop.

Tests:
  RJ-01  _classify_debate_rejection identifies high-IV keyword patterns
  RJ-02  _classify_debate_rejection returns None for non-IV rejections
  RJ-03  _write_rejection_log_entry appends valid JSONL entry
  RJ-04  _get_forced_credit_symbols returns symbol with 3+ entries
  RJ-05  _get_forced_credit_symbols does NOT return symbol with only 2 entries
  RJ-06  _get_forced_credit_symbols ignores entries older than lookback window
  RJ-07  _get_forced_credit_symbols returns empty frozenset when log is missing
  RJ-08  run_candidate_stage applies credit override for forced symbol (bullish)
  RJ-09  run_candidate_stage applies credit override for forced symbol (bearish)
  RJ-10  run_candidate_stage does NOT override symbol not in forced set
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _write_rejection_entries(log_path: Path, symbol: str, count: int,
                              code: str = "high_iv_debit_rejected",
                              age_hours: float = 0.0) -> None:
    """Write `count` rejection entries for `symbol` to log_path."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ts = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()
    with log_path.open("a") as f:
        for _ in range(count):
            f.write(json.dumps({
                "ts": ts,
                "symbol": symbol,
                "rejection_code": code,
                "iv_rank": 100.0,
                "structures": ["long_call", "debit_call_spread"],
                "reason_snippet": "IV rank 100 — buying premium is wrong premium side",
            }) + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# RJ-01 / RJ-02: _classify_debate_rejection
# ─────────────────────────────────────────────────────────────────────────────

def test_classify_iv_keywords_return_code():
    from bot_options_stage3_debate import _classify_debate_rejection
    iv_phrases = [
        "Both candidates are long premium structures with IV rank 100",
        "high IV environment prohibits buying premium",
        "long vega structures on AMAT entering at IV rank 100",
        "IV hierarchy rules prohibit debit structure",
        "wrong premium side for this iv environment",
    ]
    for phrase in iv_phrases:
        assert _classify_debate_rejection(phrase) == "high_iv_debit_rejected", phrase


def test_classify_non_iv_returns_none():
    from bot_options_stage3_debate import _classify_debate_rejection
    non_iv = [
        "Thesis invalidated — underlying dropped below support",
        "Stop loss hit — loss exceeds 50% of max risk",
        "Confidence below floor",
        "",
    ]
    for phrase in non_iv:
        assert _classify_debate_rejection(phrase) is None, phrase


# ─────────────────────────────────────────────────────────────────────────────
# RJ-03: _write_rejection_log_entry
# ─────────────────────────────────────────────────────────────────────────────

def test_write_rejection_log_entry_creates_valid_jsonl(tmp_path, monkeypatch):
    from bot_options_stage3_debate import _write_rejection_log_entry
    log_path = tmp_path / "rejection_log.jsonl"
    monkeypatch.setattr("bot_options_stage3_debate._REJECTION_LOG", log_path)

    _write_rejection_log_entry("AMAT", "high_iv_debit_rejected", 100.0,
                               ["long_call", "debit_call_spread"], "IV rank 100 test")

    lines = log_path.read_text().strip().split("\n")
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["symbol"] == "AMAT"
    assert entry["rejection_code"] == "high_iv_debit_rejected"
    assert entry["iv_rank"] == 100.0
    assert "long_call" in entry["structures"]
    assert len(entry["reason_snippet"]) <= 200


# ─────────────────────────────────────────────────────────────────────────────
# RJ-04 / RJ-05: _get_forced_credit_symbols threshold
# ─────────────────────────────────────────────────────────────────────────────

def test_get_forced_credit_with_3_entries(tmp_path, monkeypatch):
    from bot_options_stage1_candidates import _get_forced_credit_symbols
    log_path = tmp_path / "rejection_log.jsonl"
    monkeypatch.setattr("bot_options_stage1_candidates._REJECTION_LOG_PATH", log_path)
    _write_rejection_entries(log_path, "AMAT", 3)
    result = _get_forced_credit_symbols(threshold=3, hours=24)
    assert "AMAT" in result


def test_get_forced_credit_with_2_entries_not_returned(tmp_path, monkeypatch):
    from bot_options_stage1_candidates import _get_forced_credit_symbols
    log_path = tmp_path / "rejection_log.jsonl"
    monkeypatch.setattr("bot_options_stage1_candidates._REJECTION_LOG_PATH", log_path)
    _write_rejection_entries(log_path, "AMAT", 2)
    result = _get_forced_credit_symbols(threshold=3, hours=24)
    assert "AMAT" not in result


# ─────────────────────────────────────────────────────────────────────────────
# RJ-06: _get_forced_credit_symbols ignores stale entries
# ─────────────────────────────────────────────────────────────────────────────

def test_get_forced_credit_ignores_old_entries(tmp_path, monkeypatch):
    from bot_options_stage1_candidates import _get_forced_credit_symbols
    log_path = tmp_path / "rejection_log.jsonl"
    monkeypatch.setattr("bot_options_stage1_candidates._REJECTION_LOG_PATH", log_path)
    # Write 3 entries that are 25 hours old (outside the 24h window)
    _write_rejection_entries(log_path, "AMAT", 3, age_hours=25.0)
    result = _get_forced_credit_symbols(threshold=3, hours=24)
    assert "AMAT" not in result


# ─────────────────────────────────────────────────────────────────────────────
# RJ-07: _get_forced_credit_symbols — missing log
# ─────────────────────────────────────────────────────────────────────────────

def test_get_forced_credit_missing_log_returns_empty(tmp_path, monkeypatch):
    from bot_options_stage1_candidates import _get_forced_credit_symbols
    missing = tmp_path / "nonexistent.jsonl"
    monkeypatch.setattr("bot_options_stage1_candidates._REJECTION_LOG_PATH", missing)
    result = _get_forced_credit_symbols()
    assert result == frozenset()


# ─────────────────────────────────────────────────────────────────────────────
# RJ-08 / RJ-09 / RJ-10: run_candidate_stage routing override
# ─────────────────────────────────────────────────────────────────────────────

def _make_minimal_pack(symbol: str, direction: str, iv_rank: float = 100.0):
    pack = MagicMock()
    pack.symbol = symbol
    pack.a1_direction = direction
    pack.iv_rank = iv_rank
    pack.iv_environment = "very_expensive"
    pack.earnings_days_away = 1
    pack.liquidity_score = 0.50
    pack.a1_signal_score = 85.0
    pack.skew = None
    return pack


def _make_iv_summary(symbol: str, iv_rank: float = 100.0) -> dict:
    return {symbol: {"iv_rank": iv_rank, "iv_environment": "very_expensive"}}


@pytest.mark.parametrize("direction,expected_credit", [
    ("bullish", "credit_put_spread"),
    ("bearish", "credit_call_spread"),
])
def test_credit_override_fires_for_forced_symbol(direction, expected_credit,
                                                  tmp_path, monkeypatch):
    """After 3+ rejections, run_candidate_stage routes forced symbol to credit."""
    log_path = tmp_path / "rejection_log.jsonl"
    _write_rejection_entries(log_path, "AMAT", 3)
    monkeypatch.setattr("bot_options_stage1_candidates._REJECTION_LOG_PATH", log_path)

    pack = _make_minimal_pack("AMAT", direction)
    signal_scores = {
        "AMAT": {"conviction": "high", "score": 85, "direction": direction,
                 "primary_catalyst": "test", "price": 180.0}
    }
    iv_summaries = _make_iv_summary("AMAT")

    # Stub the external dependencies so only the routing logic is exercised
    monkeypatch.setattr(
        "bot_options_stage1_candidates._build_a2_feature_pack",
        lambda **kw: pack,
    )
    # _route_strategy / _compute_effective_direction / _quick_liquidity_check are
    # imported from bot_options_stage2_structures inside run_candidate_stage, so
    # patch them at the source module.
    import bot_options_stage2_structures as _s2
    monkeypatch.setattr(
        _s2, "_route_strategy",
        lambda p, config=None, options_regime=None: (
            ["long_call", "debit_call_spread"] if p.a1_direction == "bullish"
            else ["long_put", "debit_put_spread"]
        ),
    )
    monkeypatch.setattr(
        _s2, "_compute_effective_direction",
        lambda p, cfg: p.a1_direction,
    )

    fake_cset = MagicMock()
    fake_cset.surviving_candidates = []
    monkeypatch.setattr("bot_options_stage1_candidates.build_candidate_set",
                        lambda *a, **kw: fake_cset)
    monkeypatch.setattr(_s2, "_quick_liquidity_check", lambda *a, **kw: (True, ""))

    import options_data as _od_mod
    monkeypatch.setattr(_od_mod, "get_options_regime", lambda vix: {"regime": "normal"})
    monkeypatch.setattr(_od_mod, "fetch_options_chain", lambda sym: {})

    import options_universe_manager as _oum
    monkeypatch.setattr(_oum, "is_tradeable", lambda sym: True)

    import options_state as _os
    monkeypatch.setattr(_os, "load_structures", lambda: [])

    import options_position_manager as _opm
    monkeypatch.setattr(_opm, "get_close_recommended_symbols", lambda: set())

    import options_intelligence as _oi
    monkeypatch.setattr(_oi, "select_options_strategy", lambda **kw: None)

    from bot_options_stage1_candidates import run_candidate_stage
    _, _, allowed_by_sym, _ = run_candidate_stage(
        signal_scores=signal_scores,
        iv_summaries=iv_summaries,
        equity=100_000,
        vix=18.0,
        equity_symbols=["AMAT"],
        config={},
        buying_power=50_000,
    )

    assert "AMAT" in allowed_by_sym, "AMAT should have allowed structures"
    assert expected_credit in allowed_by_sym["AMAT"], (
        f"Expected {expected_credit} in {allowed_by_sym['AMAT']}"
    )
    assert "long_call" not in allowed_by_sym["AMAT"], (
        "debit structures should have been replaced by credit override"
    )


def test_no_override_for_symbol_not_in_forced_set(tmp_path, monkeypatch):
    """Symbol without rejection history keeps normal routing."""
    log_path = tmp_path / "empty_rejection_log.jsonl"
    log_path.touch()
    monkeypatch.setattr("bot_options_stage1_candidates._REJECTION_LOG_PATH", log_path)

    pack = _make_minimal_pack("NVDA", "bullish", iv_rank=45.0)
    signal_scores = {
        "NVDA": {"conviction": "high", "score": 80, "direction": "bullish",
                 "primary_catalyst": "test", "price": 900.0}
    }
    iv_summaries = {"NVDA": {"iv_rank": 45.0, "iv_environment": "neutral"}}

    monkeypatch.setattr(
        "bot_options_stage1_candidates._build_a2_feature_pack",
        lambda **kw: pack,
    )
    import bot_options_stage2_structures as _s2
    monkeypatch.setattr(
        _s2, "_route_strategy",
        lambda p, config=None, options_regime=None: ["debit_call_spread"],
    )
    monkeypatch.setattr(
        _s2, "_compute_effective_direction",
        lambda p, cfg: "bullish",
    )

    fake_cset = MagicMock()
    fake_cset.surviving_candidates = []
    monkeypatch.setattr("bot_options_stage1_candidates.build_candidate_set",
                        lambda *a, **kw: fake_cset)
    monkeypatch.setattr(_s2, "_quick_liquidity_check", lambda *a, **kw: (True, ""))

    import options_data as _od_mod
    monkeypatch.setattr(_od_mod, "get_options_regime", lambda vix: {"regime": "normal"})
    monkeypatch.setattr(_od_mod, "fetch_options_chain", lambda sym: {})

    import options_universe_manager as _oum
    monkeypatch.setattr(_oum, "is_tradeable", lambda sym: True)

    import options_state as _os
    monkeypatch.setattr(_os, "load_structures", lambda: [])

    import options_position_manager as _opm
    monkeypatch.setattr(_opm, "get_close_recommended_symbols", lambda: set())

    import options_intelligence as _oi
    monkeypatch.setattr(_oi, "select_options_strategy", lambda **kw: None)

    from bot_options_stage1_candidates import run_candidate_stage
    _, _, allowed_by_sym, _ = run_candidate_stage(
        signal_scores=signal_scores,
        iv_summaries=iv_summaries,
        equity=100_000,
        vix=18.0,
        equity_symbols=["NVDA"],
        config={},
        buying_power=50_000,
    )

    assert "NVDA" in allowed_by_sym
    # Normal routing should have debit_call_spread (no credit override)
    assert allowed_by_sym["NVDA"] == ["debit_call_spread"]
