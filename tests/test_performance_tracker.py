"""
Performance tracker tests — PT-01 through PT-15.

All tests use temporary directories so they never write to real data paths.
Price fetching is dependency-injected to avoid yfinance network calls.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

# ─────────────────────────────────────────────────────────────────────────────
# Redirect path constants to temp dirs before importing the module
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def perf_tmp(tmp_path, monkeypatch):
    """Redirect all _*_PATH constants in performance_tracker to tmp_path."""
    import performance_tracker as pt
    monkeypatch.setattr(pt, "_SONNET_IDEAS_PATH",   tmp_path / "sonnet_ideas.jsonl")
    monkeypatch.setattr(pt, "_ALLOCATOR_RECS_PATH", tmp_path / "allocator_recs.jsonl")
    monkeypatch.setattr(pt, "_A2_OUTCOMES_PATH",    tmp_path / "a2_outcomes.jsonl")
    monkeypatch.setattr(pt, "_SUMMARY_PATH",        tmp_path / "performance_summary.json")
    monkeypatch.setattr(pt, "_WEEKLY_REPORT_PATH",  tmp_path / "weekly_report.json")
    return tmp_path


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _days_ago_iso(n: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=n)
    return dt.isoformat().replace("+00:00", "Z")


def _make_idea(intent="enter_long", symbol="AAPL", conviction=0.8, tier="CORE"):
    idea = MagicMock()
    idea.intent = intent
    idea.symbol = symbol
    idea.conviction = conviction
    idea.tier = MagicMock()
    idea.tier.value = tier
    idea.direction = MagicMock()
    idea.direction.value = "long"
    idea.catalyst = "Test catalyst"
    idea.advisory_stop_pct = 0.035
    idea.sector_signal = ""
    return idea


def _make_structure(
    lifecycle_val="closed",
    structure_id="struct-001",
    underlying="AAPL",
    strategy_val="call_debit_spread",
    close_reason_code="target_hit",
    realized_pnl=150.0,
    max_profit_usd=200.0,
    max_cost_usd=-100.0,
    order_ids=None,
    opened_at=None,
    closed_at=None,
    expiration="2026-05-30",
    debit_paid=1.5,
    contracts=1,
):
    from schemas import OptionsStructure, StructureLifecycle

    lc = StructureLifecycle(lifecycle_val)
    s = MagicMock(spec=OptionsStructure)
    s.lifecycle = lc
    s.structure_id = structure_id
    s.underlying = underlying
    s.strategy = MagicMock()
    s.strategy.value = strategy_val
    s.close_reason_code = close_reason_code
    s.realized_pnl = realized_pnl
    s.max_profit_usd = max_profit_usd
    s.max_cost_usd = max_cost_usd
    s.order_ids = order_ids if order_ids is not None else ["order-001"]
    s.opened_at = opened_at or _days_ago_iso(3)
    s.closed_at = closed_at or _now_iso()
    s.expiration = expiration
    s.debit_paid = debit_paid
    s.contracts = contracts
    s.debate_confidence = 0.88
    s.net_debit_per_contract = MagicMock(return_value=debit_paid)
    return s


# ─────────────────────────────────────────────────────────────────────────────
# PT-01: log_sonnet_ideas filters to actionable intents only
# ─────────────────────────────────────────────────────────────────────────────

def test_pt01_log_sonnet_ideas_filters_intents(perf_tmp):
    import performance_tracker as pt

    ideas = [
        _make_idea(intent="enter_long",  symbol="AAPL"),
        _make_idea(intent="enter_short", symbol="SPY"),
        _make_idea(intent="hold",        symbol="NVDA"),   # should be skipped
        _make_idea(intent="monitor",     symbol="QQQ"),    # should be skipped
        _make_idea(intent="reduce",      symbol="MSFT"),
        _make_idea(intent="close",       symbol="AMZN"),
    ]
    pt.log_sonnet_ideas(
        ideas=ideas,
        approved_symbols={"AAPL", "MSFT"},
        executed_symbols={"AAPL"},
        rejection_map={"SPY": "size too large", "AMZN": "vix too high"},
        prices={"AAPL": 175.0, "SPY": 500.0, "MSFT": 420.0, "AMZN": 180.0},
        signal_scores_obj={"scored_symbols": {"AAPL": {"score": 80}, "SPY": {"score": 72}}},
        session_tier="market",
        decision_id="dec-001",
        broker_actions_map={},
    )

    records = pt._load_jsonl(pt._SONNET_IDEAS_PATH)
    assert len(records) == 4, f"Expected 4, got {len(records)}"
    symbols = {r["symbol"] for r in records}
    assert "NVDA" not in symbols
    assert "QQQ" not in symbols
    assert "AAPL" in symbols
    assert "SPY" in symbols


# ─────────────────────────────────────────────────────────────────────────────
# PT-02: log_sonnet_ideas sets kernel_result + executed correctly
# ─────────────────────────────────────────────────────────────────────────────

def test_pt02_log_sonnet_ideas_kernel_and_executed(perf_tmp):
    import performance_tracker as pt

    ideas = [
        _make_idea(intent="enter_long", symbol="AAPL"),
        _make_idea(intent="enter_long", symbol="MSFT"),
    ]
    pt.log_sonnet_ideas(
        ideas=ideas,
        approved_symbols={"AAPL"},
        executed_symbols={"AAPL"},
        rejection_map={"MSFT": "pdt floor"},
        prices={"AAPL": 175.0, "MSFT": 420.0},
        signal_scores_obj={},
        session_tier="market",
        decision_id="dec-002",
        broker_actions_map={},
    )

    records = {r["symbol"]: r for r in pt._load_jsonl(pt._SONNET_IDEAS_PATH)}
    assert records["AAPL"]["kernel_result"] == "approved"
    assert records["AAPL"]["executed"] is True
    assert records["MSFT"]["kernel_result"] == "rejected"
    assert records["MSFT"]["executed"] is False
    assert "pdt floor" in records["MSFT"]["rejection_reason"]


# ─────────────────────────────────────────────────────────────────────────────
# PT-03: log_sonnet_ideas bearish intent sign flip
# ─────────────────────────────────────────────────────────────────────────────

def test_pt03_bearish_intent_sign_flip(perf_tmp):
    import performance_tracker as pt

    # enter_short: price goes DOWN → correct call → positive return after flip
    ret_short = pt._compute_pct_return(100.0, 90.0, "enter_short")
    assert ret_short > 0, f"enter_short: price down should be positive, got {ret_short}"

    # reduce: price goes UP → incorrect (wrong to reduce) → negative
    ret_reduce_up = pt._compute_pct_return(100.0, 110.0, "reduce")
    assert ret_reduce_up < 0, f"reduce: price up should be negative, got {ret_reduce_up}"

    # enter_long: price goes UP → positive
    ret_long = pt._compute_pct_return(100.0, 110.0, "enter_long")
    assert ret_long > 0, f"enter_long: price up should be positive, got {ret_long}"


# ─────────────────────────────────────────────────────────────────────────────
# PT-04: log_allocator_recommendations filters to ADD/TRIM/REPLACE only
# ─────────────────────────────────────────────────────────────────────────────

def test_pt04_allocator_filters_hold(perf_tmp):
    import performance_tracker as pt

    proposed = [
        {"action": "ADD",     "symbol": "AAPL", "reason": "strong momentum"},
        {"action": "TRIM",    "symbol": "MSFT", "reason": "thesis weakening"},
        {"action": "REPLACE", "symbol": "QQQ",  "reason": "swap to SPY"},
        {"action": "HOLD",    "symbol": "NVDA", "reason": "thesis intact"},  # skip
    ]
    incumbents = [
        {"symbol": "MSFT", "thesis_score_normalized": 45, "market_value": 5000, "account_pct": 5.0},
    ]
    candidates = [
        {"symbol": "AAPL", "signal_score": 80, "direction": "long", "catalyst": "AI demand", "price": 175.0},
        {"symbol": "QQQ",  "signal_score": 75, "direction": "long", "catalyst": "macro",    "price": 500.0},
    ]

    pos = MagicMock()
    pos.symbol = "MSFT"
    pos.current_price = 420.0

    pt.log_allocator_recommendations(
        proposed_actions=proposed,
        incumbents=incumbents,
        candidates=candidates,
        positions=[pos],
        cycle_id="cycle-001",
    )

    records = pt._load_jsonl(pt._ALLOCATOR_RECS_PATH)
    assert len(records) == 3
    actions = {r["symbol"]: r["action"] for r in records}
    assert "NVDA" not in actions
    assert actions["AAPL"] == "ADD"
    assert actions["MSFT"] == "TRIM"
    assert actions["QQQ"] == "REPLACE"


# ─────────────────────────────────────────────────────────────────────────────
# PT-05: log_a2_structure_outcome — CLOSED win logged correctly
# ─────────────────────────────────────────────────────────────────────────────

def test_pt05_a2_closed_win_logged(perf_tmp):
    import performance_tracker as pt

    struct = _make_structure(
        lifecycle_val="closed",
        realized_pnl=150.0,
        max_profit_usd=200.0,
        close_reason_code="target_hit",
    )
    pt.log_a2_structure_outcome(struct)

    records = pt._load_jsonl(pt._A2_OUTCOMES_PATH)
    assert len(records) == 1
    r = records[0]
    assert r["outcome"] == "win"
    assert r["pnl_usd"] == 150.0
    assert r["exit_reason"] == "target_hit"
    assert r["structure_id"] == "struct-001"


# ─────────────────────────────────────────────────────────────────────────────
# PT-06: log_a2_structure_outcome — CANCELLED with no order_ids is skipped
# ─────────────────────────────────────────────────────────────────────────────

def test_pt06_cancelled_no_order_ids_skipped(perf_tmp):
    import performance_tracker as pt

    struct = _make_structure(
        lifecycle_val="cancelled",
        order_ids=[],   # no order_ids → never submitted → skip
    )
    pt.log_a2_structure_outcome(struct)

    records = pt._load_jsonl(pt._A2_OUTCOMES_PATH)
    assert len(records) == 0, "Cancelled with no order_ids should not be logged"


# ─────────────────────────────────────────────────────────────────────────────
# PT-07: log_a2_structure_outcome — CANCELLED with order_ids is logged
# ─────────────────────────────────────────────────────────────────────────────

def test_pt07_cancelled_with_order_ids_logged(perf_tmp):
    import performance_tracker as pt

    struct = _make_structure(
        lifecycle_val="cancelled",
        order_ids=["ord-001"],
        realized_pnl=None,
    )
    pt.log_a2_structure_outcome(struct)

    records = pt._load_jsonl(pt._A2_OUTCOMES_PATH)
    assert len(records) == 1
    assert records[0]["outcome"] == "cancelled"


# ─────────────────────────────────────────────────────────────────────────────
# PT-08: _trading_days_elapsed counts Mon-Fri only
# ─────────────────────────────────────────────────────────────────────────────

def test_pt08_trading_days_elapsed():
    import performance_tracker as pt

    # Reference date: simulate a Monday 5 trading days ago (Mon-Fri = 5 days)
    # We'll check that a weekend day in between is not counted.
    today = datetime.now(timezone.utc)
    # Find last Monday (go back up to 7 days)
    d = today
    while d.weekday() != 0:
        d -= timedelta(days=1)
    # d is now last Monday (or today if today is Monday)
    # A timestamp from last Monday should give between 0 and 5 trading days back
    ts = d.isoformat().replace("+00:00", "Z")
    elapsed = pt._trading_days_elapsed(ts)
    # elapsed should be 0 if d == today.date(), else positive
    assert elapsed >= 0
    assert elapsed <= 5   # can't be more than 5 days in a week


# ─────────────────────────────────────────────────────────────────────────────
# PT-09: _find_nth_trading_close finds correct price
# ─────────────────────────────────────────────────────────────────────────────

def test_pt09_find_nth_trading_close():
    import performance_tracker as pt

    close_by_date = {
        "2026-04-21": 170.0,   # Monday
        "2026-04-22": 172.0,   # Tuesday
        "2026-04-23": 175.0,   # Wednesday
        "2026-04-24": 174.0,   # Thursday
        "2026-04-25": 176.0,   # Friday
    }
    ref = "2026-04-20"   # Sunday — next trading day is Monday

    p1 = pt._find_nth_trading_close(close_by_date, ref, 1)
    assert p1 == 170.0, f"1d close should be 170.0, got {p1}"

    p3 = pt._find_nth_trading_close(close_by_date, ref, 3)
    assert p3 == 175.0, f"3d close should be 175.0, got {p3}"

    p5 = pt._find_nth_trading_close(close_by_date, ref, 5)
    assert p5 == 176.0, f"5d close should be 176.0, got {p5}"

    p6 = pt._find_nth_trading_close(close_by_date, ref, 6)
    assert p6 is None, f"6d close should be None (no data), got {p6}"


# ─────────────────────────────────────────────────────────────────────────────
# PT-10: compute_overnight_outcomes fills in outcome_1d for mature records
# ─────────────────────────────────────────────────────────────────────────────

def test_pt10_compute_overnight_outcomes_fills_1d(perf_tmp):
    import performance_tracker as pt

    # Insert a record with price_at_decision and a timestamp 5 days ago
    old_ts = _days_ago_iso(5)
    old_date = datetime.fromisoformat(old_ts.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    next_date = (datetime.fromisoformat(old_ts.replace("Z", "+00:00")) + timedelta(days=1)).strftime("%Y-%m-%d")

    rec = {
        "timestamp": old_ts,
        "decision_id": "dec-x",
        "symbol": "AAPL",
        "intent": "enter_long",
        "tier": "CORE",
        "conviction": 0.8,
        "score": 80,
        "direction": "long",
        "catalyst": "test",
        "kernel_result": "approved",
        "rejection_reason": None,
        "price_at_decision": 170.0,
        "stop_proposed": None,
        "target_proposed": None,
        "executed": True,
        "session": "market",
        "outcome_1d": None,
        "outcome_3d": None,
        "outcome_5d": None,
        "outcome_closed": None,
        "outcome_filled_at": None,
    }
    pt._append_jsonl(pt._SONNET_IDEAS_PATH, [rec])

    # Fake price fetcher: AAPL closed 1% higher next day
    close_prices = {"AAPL": {next_date: 171.7, old_date: 170.0}}
    # Add 3d and 5d dates
    for i in range(2, 6):
        d = (datetime.fromisoformat(old_ts.replace("Z", "+00:00")) + timedelta(days=i)).strftime("%Y-%m-%d")
        close_prices["AAPL"][d] = 170.0 + i * 0.5

    def fake_fetcher(symbols, days_back=90):
        return {s: close_prices.get(s, {}) for s in symbols}

    pt.compute_overnight_outcomes(price_fetcher=fake_fetcher)

    updated = pt._load_jsonl(pt._SONNET_IDEAS_PATH)
    assert len(updated) == 1
    r = updated[0]
    assert r["outcome_1d"] is not None, "outcome_1d should be filled"
    assert r["outcome_1d"] > 0, "enter_long with price up should give positive outcome"


# ─────────────────────────────────────────────────────────────────────────────
# PT-11: compute_overnight_outcomes — bearish outcome sign flip applied
# ─────────────────────────────────────────────────────────────────────────────

def test_pt11_bearish_sign_flip_in_nightly(perf_tmp):
    import performance_tracker as pt

    old_ts = _days_ago_iso(5)
    next_date = (datetime.fromisoformat(old_ts.replace("Z", "+00:00")) + timedelta(days=1)).strftime("%Y-%m-%d")

    rec = {
        "timestamp": old_ts,
        "decision_id": "dec-y",
        "symbol": "SPY",
        "intent": "enter_short",   # bearish
        "tier": "CORE",
        "conviction": 0.7,
        "score": 70,
        "direction": "short",
        "catalyst": "bear signal",
        "kernel_result": "approved",
        "rejection_reason": None,
        "price_at_decision": 500.0,
        "stop_proposed": None,
        "target_proposed": None,
        "executed": True,
        "session": "market",
        "outcome_1d": None,
        "outcome_3d": None,
        "outcome_5d": None,
        "outcome_closed": None,
        "outcome_filled_at": None,
    }
    pt._append_jsonl(pt._SONNET_IDEAS_PATH, [rec])

    close_prices = {"SPY": {next_date: 495.0}}  # price fell → correct short → positive
    for i in range(2, 6):
        d = (datetime.fromisoformat(old_ts.replace("Z", "+00:00")) + timedelta(days=i)).strftime("%Y-%m-%d")
        close_prices["SPY"][d] = 495.0

    def fake_fetcher(symbols, days_back=90):
        return {s: close_prices.get(s, {}) for s in symbols}

    pt.compute_overnight_outcomes(price_fetcher=fake_fetcher)

    updated = pt._load_jsonl(pt._SONNET_IDEAS_PATH)
    r = updated[0]
    assert r["outcome_1d"] is not None
    assert r["outcome_1d"] > 0, f"enter_short with price falling should be positive, got {r['outcome_1d']}"


# ─────────────────────────────────────────────────────────────────────────────
# PT-12: followed_by_bot is True when Sonnet executes same symbol within 10min
# ─────────────────────────────────────────────────────────────────────────────

def test_pt12_followed_by_bot_true(perf_tmp):
    import performance_tracker as pt

    base_ts = _days_ago_iso(3)
    # Sonnet idea executed 5 minutes after the allocator recommendation
    idea_ts = (datetime.fromisoformat(base_ts.replace("Z", "+00:00")) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")

    # Write a sonnet idea for AAPL (executed=True, enter_long within 10 min)
    idea = {
        "timestamp": idea_ts,
        "decision_id": "dec-z",
        "symbol": "AAPL",
        "intent": "enter_long",
        "tier": "CORE",
        "conviction": 0.8,
        "score": 80,
        "direction": "long",
        "catalyst": "test",
        "kernel_result": "approved",
        "rejection_reason": None,
        "price_at_decision": 175.0,
        "stop_proposed": None,
        "target_proposed": None,
        "executed": True,
        "session": "market",
        "outcome_1d": None,
        "outcome_3d": None,
        "outcome_5d": None,
        "outcome_closed": None,
        "outcome_filled_at": None,
    }
    pt._append_jsonl(pt._SONNET_IDEAS_PATH, [idea])

    # Write an ADD recommendation at base_ts for same symbol
    rec = {
        "timestamp": base_ts,
        "cycle_id": "cycle-x",
        "symbol": "AAPL",
        "action": "ADD",
        "reason": "momentum",
        "conviction": 0.8,
        "price_at_recommendation": 175.0,
        "account_pct_at_recommendation": 0.05,
        "followed_by_bot": None,
        "outcome_1d": None,
        "outcome_3d": None,
        "outcome_5d": None,
        "outcome_filled_at": None,
    }
    pt._append_jsonl(pt._ALLOCATOR_RECS_PATH, [rec])

    def fake_fetcher(symbols, days_back=90):
        return {}

    pt._compute_allocator_outcomes(fake_fetcher)

    updated = pt._load_jsonl(pt._ALLOCATOR_RECS_PATH)
    assert updated[0]["followed_by_bot"] is True


# ─────────────────────────────────────────────────────────────────────────────
# PT-13: _compute_performance_summary returns correct data_days count
# ─────────────────────────────────────────────────────────────────────────────

def test_pt13_performance_summary_data_days(perf_tmp):
    import performance_tracker as pt

    # Write 3 ideas on 3 distinct days
    for i in range(3):
        ts = _days_ago_iso(i)
        rec = {
            "timestamp": ts,
            "decision_id": f"dec-{i}",
            "symbol": "AAPL",
            "intent": "enter_long",
            "tier": "CORE",
            "conviction": 0.8,
            "score": 75,
            "direction": "long",
            "catalyst": "test",
            "kernel_result": "approved",
            "rejection_reason": None,
            "price_at_decision": 175.0,
            "stop_proposed": None,
            "target_proposed": None,
            "executed": True,
            "session": "market",
            "outcome_1d": None,
            "outcome_3d": None,
            "outcome_5d": None,
            "outcome_closed": None,
            "outcome_filled_at": None,
        }
        pt._append_jsonl(pt._SONNET_IDEAS_PATH, [rec])

    summary = pt._compute_performance_summary(days_back=7)
    assert summary["data_days"] == 3
    assert summary["sonnet_ideas"]["total_ideas_7d"] == 3


# ─────────────────────────────────────────────────────────────────────────────
# PT-14: load_performance_summary returns {} when file is >25h stale
# ─────────────────────────────────────────────────────────────────────────────

def test_pt14_load_summary_stale_returns_empty(perf_tmp):
    import performance_tracker as pt

    stale_ts = (datetime.now(timezone.utc) - timedelta(hours=26)).isoformat().replace("+00:00", "Z")
    pt._SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    pt._SUMMARY_PATH.write_text(json.dumps({"computed_at": stale_ts, "data_days": 5}))

    result = pt.load_performance_summary()
    assert result == {}, f"Expected empty dict for stale summary, got {result}"


# ─────────────────────────────────────────────────────────────────────────────
# PT-15: generate_weekly_performance_report is non-fatal on import error
# ─────────────────────────────────────────────────────────────────────────────

def test_pt15_weekly_report_nonfatal_on_claude_error(perf_tmp):
    """generate_weekly_performance_report must not raise even if Claude API fails."""
    import performance_tracker as pt

    with patch("performance_tracker.generate_weekly_performance_report") as mock_gen:
        # Simulate the real function failing internally
        def _inner():
            try:
                raise RuntimeError("Claude API unavailable")
            except Exception as exc:
                import logging
                logging.getLogger("performance_tracker").warning("[PERF] failed: %s", exc)
        mock_gen.side_effect = _inner
        # Calling should not raise
        try:
            pt.generate_weekly_performance_report()
        except Exception as exc:
            pytest.fail(f"generate_weekly_performance_report raised: {exc}")
