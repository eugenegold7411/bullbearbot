"""
Tests for thesis score discrimination — verifies that score_position_thesis()
produces a meaningful spread across 1-9 rather than clustering at 8-9.

Root cause: base score was 8 (pre-saturated) and technical above-one-MA gave
the same +1 as above-both (biased upward). Both bugs are fixed here.
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import portfolio_intelligence as pi

# ── helpers ───────────────────────────────────────────────────────────────────

def _pos(symbol, entry, current, unrealized_pl, qty=10):
    """Minimal Alpaca position mock."""
    return SimpleNamespace(
        symbol=symbol,
        avg_entry_price=str(entry),
        current_price=str(current),
        unrealized_pl=str(unrealized_pl),
        market_value=str(current * qty),
        qty=str(qty),
    )


def _score(
    symbol="TEST",
    entry=100.0,
    current=100.0,
    unrealized_pl=0.0,
    qty=10,
    days_held=0,
    stop_loss=None,
    take_profit=None,
    catalyst="",
    bars_df=None,
    sector_perf=None,
):
    """Call score_position_thesis with controlled mocks."""
    pos = _pos(symbol, entry, current, unrealized_pl, qty)
    decision = {"catalyst": catalyst}
    if stop_loss is not None:
        decision["stop_loss"] = str(stop_loss)
    if take_profit is not None:
        decision["take_profit"] = str(take_profit)

    with patch("portfolio_intelligence._load_bars", return_value=bars_df), \
         patch("portfolio_intelligence._load_sector_perf", return_value=sector_perf or {}):
        result = pi.score_position_thesis(
            symbol=symbol,
            position=pos,
            original_decision=decision,
            current_md={},
            days_held=days_held,
        )
    return result["thesis_score"]


# ── 1. Underwater position approaching stop → score ≤ 5 ──────────────────────

def test_underwater_position_scores_low():
    """Position well below entry and 65% of the way to stop → low score."""
    # Entry 100, stop 85, target 125. Current 90: pct_to_stop = 1-(5/15)=0.67 > 0.5 → -2.
    # base=6, catalyst_age=8d→-2, below_both_MAs→-1, pnl_momentum→-2, sector_down→-1 = 0 → clamped 1
    import pandas as pd

    # Bars with closes well below both MAs (MA20 ~105, EMA9 ~103)
    closes = [105.0] * 19 + [90.0]  # last bar = 90, well below MA of ~105
    df = pd.DataFrame({"close": closes, "date": range(20)})

    score = _score(
        entry=100.0, current=90.0, unrealized_pl=-100.0,
        days_held=8,
        stop_loss=85.0, take_profit=125.0,
        sector_perf={"technology": {"day_chg": -0.5}},
        bars_df=df,
    )
    assert score <= 5, f"Expected ≤ 5 for underwater position, got {score}"


# ── 2. Healthy position, thesis intact, flat momentum → 5 ≤ score ≤ 7 ────────

def test_healthy_position_scores_mid():
    """Position slightly above entry, above both MAs, 3 days old, no sector data → mid score."""
    import pandas as pd

    # Bars with closes below current (price above MA)
    closes = [98.0] * 20
    df = pd.DataFrame({"close": closes, "date": range(20)})

    # pct_to_target = (103-100)/(130-100) = 0.1 → not > 0.1 → flat (0)
    score = _score(
        entry=100.0, current=103.0, unrealized_pl=30.0,
        days_held=3,
        stop_loss=92.0, take_profit=130.0,
        bars_df=df,
    )
    assert 5 <= score <= 7, f"Expected 5-7 for healthy mid position, got {score}"


# ── 3. Strong position, confirmed thesis, strong momentum → score ≥ 7 ─────────

def test_strong_position_scores_high():
    """Fresh position, above both MAs, trending toward target, sector aligned → high score."""
    import pandas as pd

    # Bars with closes below current (price above MA)
    closes = [98.0] * 20
    df = pd.DataFrame({"close": closes, "date": range(20)})

    # pct_to_target = (112-100)/(130-100) = 0.4 > 0.1 → +1
    score = _score(
        entry=100.0, current=112.0, unrealized_pl=120.0,
        days_held=0,
        stop_loss=90.0, take_profit=130.0,
        sector_perf={"technology": {"day_chg": 1.2}},
        bars_df=df,
    )
    assert score >= 7, f"Expected ≥ 7 for strong confirmed position, got {score}"


# ── 4. Score never exceeds 9 (ceiling) ────────────────────────────────────────

def test_score_never_exceeds_9():
    """All inputs maximized — score must be clamped to 9."""
    import pandas as pd

    closes = [95.0] * 20
    df = pd.DataFrame({"close": closes, "date": range(20)})

    score = _score(
        entry=100.0, current=115.0, unrealized_pl=150.0,
        days_held=0,
        stop_loss=88.0, take_profit=130.0,
        sector_perf={"technology": {"day_chg": 2.5}},
        bars_df=df,
    )
    assert score <= 9, f"Score exceeded ceiling: {score}"


# ── 5. Score never below 1 (floor) ────────────────────────────────────────────

def test_score_never_below_1():
    """All inputs minimized — score must be clamped to 1."""
    import pandas as pd

    # High closes so current price (60) is well below MA (~100) and EMA9
    closes = [100.0] * 20
    df = pd.DataFrame({"close": closes, "date": range(20)})

    # Below entry, near stop: pct_to_stop = 1 - (60-50)/(100-50) = 1-0.2 = 0.8 > 0.5 → -2
    score = _score(
        entry=100.0, current=60.0, unrealized_pl=-400.0,
        days_held=30,
        stop_loss=50.0, take_profit=150.0,
        sector_perf={"technology": {"day_chg": -2.0}},
        catalyst="earnings beat",   # earnings > 2 days → -1 time decay
        bars_df=df,
    )
    assert score >= 1, f"Score fell below floor: {score}"


# ── 6. thesis_scores_latest.json is written after scoring run ─────────────────

def test_scores_latest_json_written(tmp_path, monkeypatch):
    """build_portfolio_intelligence() must write thesis_scores_latest.json."""
    monkeypatch.setattr(pi, "_ROOT", tmp_path)

    pos = _pos("AAPL", 150.0, 155.0, 50.0, 1)
    with patch("portfolio_intelligence._load_bars", return_value=None), \
         patch("portfolio_intelligence._load_sector_perf", return_value={}), \
         patch("portfolio_intelligence.compute_portfolio_correlation",
               return_value={"matrix": {}, "high_correlation_pairs": [],
                             "effective_bets": 1, "new_symbol_correlations": {}}):
        pi.build_portfolio_intelligence(
            equity=100_000.0,
            positions=[pos],
            config={},
            open_decisions={"AAPL": {}},
            position_entry_dates={"AAPL": datetime.now(timezone.utc)},
        )

    out_path = tmp_path / "data" / "analytics" / "thesis_scores_latest.json"
    assert out_path.exists(), "thesis_scores_latest.json was not written"
    data = json.loads(out_path.read_text())
    symbols = [ts["symbol"] for ts in data["thesis_scores"]]
    assert "AAPL" in symbols, f"Expected AAPL in scores, got: {symbols}"


# ── 7. Portfolio with varied health → stddev of scores > 1.0 ─────────────────

def test_portfolio_has_score_variance():
    """Five positions spanning health states must have score stddev > 1.0."""
    import pandas as pd

    # Bars where MA20 ≈ 100 (all closes = 100)
    high_closes = [100.0] * 20     # current above → above MAs
    low_closes  = [100.0] * 20     # current below 100 → below MAs

    df_up   = pd.DataFrame({"close": high_closes, "date": range(20)})
    df_down = pd.DataFrame({"close": low_closes,  "date": range(20)})

    cases = [
        # (entry, current, unreal_pl, days, stop, target, sector_perf, bars)
        # 1. Excellent: fresh, above both, to target, sector aligned → ~9
        (100, 112, 120, 0,  88, 130, {"tech": {"day_chg": 1.5}}, df_up),
        # 2. Good: 2 days, above both, trending to target, neutral sector → ~8
        (100, 108, 80,  2,  88, 130, {},                          df_up),
        # 3. Average: 5 days, above both, flat, neutral → ~5
        (100, 101, 10,  5,  88, 130, {},                          df_up),
        # 4. Weak: 8 days, above one MA only, approaching stop (<50%), sector down → ~3
        (100, 93,  -70, 8,  85, 130, {"tech": {"day_chg": -1.0}}, df_up),
        # 5. Critical: 10+ days, below both MAs, >50% to stop, sector down → ~1
        (100, 88,  -120, 10, 85, 130, {"tech": {"day_chg": -2.0}}, df_down),
    ]

    scores = []
    for entry, current, unreal_pl, days, stop, target, sector, bars in cases:
        s = _score(
            entry=float(entry), current=float(current), unrealized_pl=float(unreal_pl),
            days_held=days,
            stop_loss=float(stop), take_profit=float(target),
            sector_perf=sector, bars_df=bars,
        )
        scores.append(s)

    std = statistics.stdev(scores)
    assert std > 1.0, (
        f"Expected score stddev > 1.0 for varied portfolio, got {std:.2f}. "
        f"Scores: {scores}"
    )
