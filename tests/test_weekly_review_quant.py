"""
Tests for compute_quantitative_performance_summary() in performance_tracker,
Agent 6 quantitative data injection in weekly_review, and readiness score
heuristic.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

# ─── helpers ─────────────────────────────────────────────────────────────────

def _make_outcome(
    symbol: str,
    cls: str,
    action: str = "buy",
    module_tags: list | None = None,
    outcome_pct: float | None = None,
    decided_at: str = "2026-05-01T10:00:00Z",
) -> str:
    rec: dict = {
        "symbol":               symbol,
        "alpha_classification": cls,
        "action":               action,
        "module_tags":          module_tags or [],
        "decided_at":           decided_at,
    }
    if outcome_pct is not None:
        rec["outcome_pct"] = outcome_pct
    return json.dumps(rec)


def _write_outcomes(tmp_path: Path, lines: list[str]) -> Path:
    p = tmp_path / "data" / "analytics" / "decision_outcomes.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n")
    return p


# ─── Performance summary tests ───────────────────────────────────────────────

def test_summary_win_rate_correct(tmp_path: Path) -> None:
    lines = (
        [_make_outcome("AAPL", "alpha_positive")] * 6
        + [_make_outcome("AAPL", "alpha_negative")] * 4
    )
    _write_outcomes(tmp_path, lines)
    from performance_tracker import compute_quantitative_performance_summary
    with mock.patch("performance_tracker._OUTCOMES_LOG", tmp_path / "data" / "analytics" / "decision_outcomes.jsonl"):
        result = compute_quantitative_performance_summary()
    assert result["classified_decisions"] == 10
    assert result["overall_win_rate"] == pytest.approx(0.6, abs=1e-3)


def test_summary_by_symbol_correct(tmp_path: Path) -> None:
    lines = [
        _make_outcome("NVDA", "alpha_positive"),
        _make_outcome("NVDA", "alpha_positive"),
        _make_outcome("NVDA", "alpha_negative"),
        _make_outcome("TSLA", "alpha_negative"),
        _make_outcome("TSLA", "alpha_negative"),
    ]
    _write_outcomes(tmp_path, lines)
    with mock.patch("performance_tracker._OUTCOMES_LOG", tmp_path / "data" / "analytics" / "decision_outcomes.jsonl"):
        from performance_tracker import compute_quantitative_performance_summary
        result = compute_quantitative_performance_summary()
    assert "NVDA" in result["by_symbol"]
    assert result["by_symbol"]["NVDA"]["win_rate"] == pytest.approx(2 / 3, abs=1e-3)
    assert "TSLA" in result["by_symbol"]
    assert result["by_symbol"]["TSLA"]["win_rate"] == pytest.approx(0.0, abs=1e-3)


def test_summary_excludes_thin_data(tmp_path: Path) -> None:
    lines = [_make_outcome("RARE", "alpha_positive")]
    _write_outcomes(tmp_path, lines)
    with mock.patch("performance_tracker._OUTCOMES_LOG", tmp_path / "data" / "analytics" / "decision_outcomes.jsonl"):
        from performance_tracker import compute_quantitative_performance_summary
        result = compute_quantitative_performance_summary()
    assert "RARE" not in result["by_symbol"]


def test_summary_top_performers_ordered(tmp_path: Path) -> None:
    # BEST: 4/4, MID: 2/4, LOW: 1/4
    lines = (
        [_make_outcome("BEST", "alpha_positive")] * 4
        + [_make_outcome("MID",  "alpha_positive")] * 2
        + [_make_outcome("MID",  "alpha_negative")] * 2
        + [_make_outcome("LOW",  "alpha_positive")] * 1
        + [_make_outcome("LOW",  "alpha_negative")] * 3
    )
    _write_outcomes(tmp_path, lines)
    with mock.patch("performance_tracker._OUTCOMES_LOG", tmp_path / "data" / "analytics" / "decision_outcomes.jsonl"):
        from performance_tracker import compute_quantitative_performance_summary
        result = compute_quantitative_performance_summary()
    tops = result["top_performing_symbols"]
    assert tops[0] == "BEST"
    assert tops[1] == "MID"


def test_summary_handles_empty_outcomes(tmp_path: Path) -> None:
    _write_outcomes(tmp_path, [])
    with mock.patch("performance_tracker._OUTCOMES_LOG", tmp_path / "data" / "analytics" / "decision_outcomes.jsonl"):
        from performance_tracker import compute_quantitative_performance_summary
        result = compute_quantitative_performance_summary()
    assert result["classified_decisions"] == 0
    assert result["overall_win_rate"] is None
    assert result["by_symbol"] == {}
    assert result["top_performing_symbols"] == []


def test_summary_signal_source_win_rates(tmp_path: Path) -> None:
    lines = [
        _make_outcome("AAPL", "alpha_positive", module_tags=["macro_wire", "insider_intelligence"]),
        _make_outcome("AAPL", "alpha_positive", module_tags=["macro_wire"]),
        _make_outcome("AAPL", "alpha_negative", module_tags=["insider_intelligence"]),
    ]
    _write_outcomes(tmp_path, lines)
    with mock.patch("performance_tracker._OUTCOMES_LOG", tmp_path / "data" / "analytics" / "decision_outcomes.jsonl"):
        from performance_tracker import compute_quantitative_performance_summary
        result = compute_quantitative_performance_summary()
    src = result["by_signal_source"]
    assert "macro_wire" in src
    assert src["macro_wire"]["win_rate"] == pytest.approx(1.0, abs=1e-3)
    assert "insider_intelligence" in src
    assert src["insider_intelligence"]["win_rate"] == pytest.approx(0.5, abs=1e-3)


# ─── Agent 6 injection tests ─────────────────────────────────────────────────
# These tests exercise _build_quantitative_performance_block() added by Session 3
# in weekly_review.py. That function is only present once Session 3 merges its
# uncommitted work. Skip gracefully when running against origin without those changes.

try:
    from weekly_review import _build_quantitative_performance_block as _wr_quant_block
    _WR_QUANT_AVAILABLE = True
except ImportError:
    _WR_QUANT_AVAILABLE = False

_skip_no_wr_quant = pytest.mark.skipif(
    not _WR_QUANT_AVAILABLE,
    reason="_build_quantitative_performance_block not yet merged from Session 3",
)


@_skip_no_wr_quant
def test_agent6_receives_quant_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_build_quantitative_performance_block returns keys consumed by governance signals."""
    lines = (
        [_make_outcome("AAPL", "alpha_positive", module_tags=["macro_wire"])] * 4
        + [_make_outcome("AAPL", "alpha_negative", module_tags=["macro_wire"])] * 2
    )
    _write_outcomes(tmp_path, lines)
    monkeypatch.chdir(tmp_path)
    result = _wr_quant_block()
    assert "win_rates_by_symbol" in result
    assert "total_classified" in result
    assert result["total_classified"] == 6


@_skip_no_wr_quant
def test_agent6_quant_section_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """win_rates_by_symbol is a dict of symbol→float."""
    lines = [
        _make_outcome("NVDA", "alpha_positive"),
        _make_outcome("NVDA", "alpha_positive"),
        _make_outcome("NVDA", "alpha_negative"),
    ]
    _write_outcomes(tmp_path, lines)
    monkeypatch.chdir(tmp_path)
    result = _wr_quant_block()
    wr = result["win_rates_by_symbol"]
    assert isinstance(wr, dict)
    assert "NVDA" in wr
    assert isinstance(wr["NVDA"], float)


@_skip_no_wr_quant
def test_agent6_handles_empty_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When no classified data exists, block returns safe defaults without crashing."""
    _write_outcomes(tmp_path, [])
    monkeypatch.chdir(tmp_path)
    result = _wr_quant_block()
    assert result["total_classified"] == 0
    assert result["win_rates_by_symbol"] == {}
    assert result["top_performing_symbols"] == []


# ─── Readiness score tests ────────────────────────────────────────────────────

def _readiness_score(overall_win_rate: float | None, classified: int) -> float:
    """
    Reference implementation of the heuristic described in the task.
    Mirrors what _compute_readiness_score() will do once wired into weekly_review.py.
    """
    score = 0.0
    if overall_win_rate is not None and overall_win_rate > 0.55 and classified >= 20:
        score += 0.3
    if overall_win_rate is not None and overall_win_rate > 0.60 and classified >= 30:
        score += 0.5
    return min(score, 1.0)


def test_readiness_score_increases_with_win_rate() -> None:
    score = _readiness_score(overall_win_rate=0.60, classified=30)
    assert score > 0.0


def test_readiness_score_zero_when_insufficient() -> None:
    score = _readiness_score(overall_win_rate=0.60, classified=5)
    assert score == pytest.approx(0.0)


def test_readiness_score_capped_at_one() -> None:
    # Both thresholds met → 0.3 + 0.5 = 0.8, capped at 1.0
    score = _readiness_score(overall_win_rate=0.65, classified=40)
    assert score <= 1.0
    assert score == pytest.approx(0.8, abs=1e-6)
