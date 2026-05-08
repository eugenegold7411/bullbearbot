"""S2 learning loop — alpha summary, A2/A1 prompt injection, signal credibility,
weekly review quantitative wiring (19 tests)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import bot_options_stage3_debate as debate  # noqa: E402
import bot_stage2_signal as signal_mod  # noqa: E402
import decision_outcomes  # noqa: E402
import signal_credibility  # noqa: E402
import weekly_review  # noqa: E402

# ─── fixtures ────────────────────────────────────────────────────────────────

def _record(symbol: str, classification: str, outcome_pct: float = 0.01,
            action: str = "buy") -> dict:
    return {
        "decision_id":          f"dec_{symbol}_{classification}_{outcome_pct}",
        "account":              "A1",
        "symbol":               symbol,
        "timestamp":            "2026-04-01T10:00:00+00:00",
        "action":               action,
        "alpha_classification": classification,
        "outcome_pct":          outcome_pct,
        "module_tags":          {
            "signal_scorer":        True,
            "macro_wire":           True,
            "insider_intelligence": False,
        },
    }


def _write_outcomes(path: Path, recs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")


@pytest.fixture
def isolate_outcomes(tmp_path, monkeypatch) -> Path:
    p = tmp_path / "decision_outcomes.jsonl"
    monkeypatch.setattr(decision_outcomes, "OUTCOMES_LOG", p)
    return p


@pytest.fixture
def isolate_credibility(tmp_path, monkeypatch) -> Path:
    p = tmp_path / "signal_source_credibility.json"
    monkeypatch.setattr(signal_credibility, "_CREDIBILITY_PATH", p)
    return p


# ─── alpha summary tests (1-5) ───────────────────────────────────────────────

def test_alpha_summary_correct_win_rate(isolate_outcomes):
    """7 positives, 3 negatives → 70% WR."""
    recs = [_record("NVDA", "alpha_positive",  0.05) for _ in range(7)]
    recs += [_record("NVDA", "alpha_negative", -0.03) for _ in range(3)]
    _write_outcomes(isolate_outcomes, recs)
    s = decision_outcomes.get_alpha_summary("NVDA")
    assert s is not None
    assert s["n"] == 10
    assert s["win_rate"] == 0.7


def test_alpha_summary_returns_none_when_thin(isolate_outcomes):
    """1 record (below min=2) → None."""
    _write_outcomes(isolate_outcomes, [
        _record("X", "alpha_positive", 0.05),
    ])
    assert decision_outcomes.get_alpha_summary("X") is None


def test_alpha_summary_min_threshold_records(isolate_outcomes):
    """Exactly 2 records (at min=2 threshold) → returns data."""
    _write_outcomes(isolate_outcomes, [
        _record("X", "alpha_positive",  0.05),
        _record("X", "alpha_negative", -0.03),
    ])
    s = decision_outcomes.get_alpha_summary("X")
    assert s is not None
    assert s["n"] == 2
    assert s["symbol"] == "X"


def test_alpha_summary_last_outcomes_order(isolate_outcomes):
    """last_outcomes is most-recent-first; last_n_outcomes is oldest→newest."""
    _write_outcomes(isolate_outcomes, [
        _record("X", "alpha_positive",  0.05),
        _record("X", "alpha_negative", -0.03),
        _record("X", "alpha_neutral",   0.00),
    ])
    s = decision_outcomes.get_alpha_summary("X")
    assert s["last_outcomes"][0] == "alpha_neutral"   # most recent first
    assert s["last_outcomes"][-1] == "alpha_positive"  # oldest of the 3
    assert s["last_n_outcomes"][-1] == "alpha_neutral"  # full window, newest at end


def test_alpha_summary_handles_missing_symbol(isolate_outcomes):
    """Unknown symbol → None."""
    _write_outcomes(isolate_outcomes, [
        _record("AAPL", "alpha_positive",  0.05),
        _record("AAPL", "alpha_negative", -0.03),
        _record("AAPL", "alpha_neutral",   0.00),
    ])
    assert decision_outcomes.get_alpha_summary("ZZZ_NEVER_TRADED") is None


# ─── A2 debate injection tests (6-9) ─────────────────────────────────────────

def test_debate_prompt_includes_alpha_when_available(isolate_outcomes):
    _write_outcomes(isolate_outcomes, [
        _record("AAPL", "alpha_positive",  0.05),
        _record("AAPL", "alpha_negative", -0.03),
        _record("AAPL", "alpha_neutral",   0.00),
    ])
    out = debate._format_alpha_summary_for_debate("AAPL")
    assert "HISTORICAL ALPHA" in out
    assert "AAPL" in out
    assert "Win rate" in out


def test_debate_prompt_excludes_when_thin(isolate_outcomes):
    """1 record (below min=2) → debate gets empty string, no inject."""
    _write_outcomes(isolate_outcomes, [
        _record("X", "alpha_positive",  0.05),
    ])
    assert debate._format_alpha_summary_for_debate("X") == ""


def test_debate_alpha_format_correct(isolate_outcomes):
    """Win rate and emojis match the data."""
    _write_outcomes(isolate_outcomes, [
        _record("AAPL", "alpha_positive",  0.05),
        _record("AAPL", "alpha_positive",  0.04),
        _record("AAPL", "alpha_negative", -0.03),
    ])
    out = debate._format_alpha_summary_for_debate("AAPL")
    assert "67%" in out         # 2/3 positive among 3 records
    assert "✅" in out
    assert "❌" in out


def test_debate_alpha_handles_none_gracefully(monkeypatch):
    """If get_alpha_summary raises, debate format must not crash."""
    def _boom(symbol):
        raise RuntimeError("synthetic")
    monkeypatch.setattr(decision_outcomes, "get_alpha_summary", _boom)
    out = debate._format_alpha_summary_for_debate("AAPL")
    assert out == ""


# ─── A1 signal block tests (10-12) ───────────────────────────────────────────

def test_signal_line_includes_alpha_annotation(isolate_outcomes):
    _write_outcomes(isolate_outcomes, [
        _record("NVDA", "alpha_positive",  0.05),
        _record("NVDA", "alpha_positive",  0.04),
        _record("NVDA", "alpha_negative", -0.02),
    ])
    formatted = signal_mod.format_signal_scores({
        "scored_symbols": {
            "NVDA": {"score": 88, "conviction": "high", "primary_catalyst": "AI capex"},
        },
    })
    assert "[alpha:" in formatted
    assert "NVDA" in formatted


def test_signal_line_unchanged_when_thin(isolate_outcomes):
    """0 records on a fresh symbol → no [alpha:…] annotation appended."""
    _write_outcomes(isolate_outcomes, [
        _record("OTHER_SYM", "alpha_positive", 0.05),
    ])
    formatted = signal_mod.format_signal_scores({
        "scored_symbols": {
            "AAPL": {"score": 70, "conviction": "medium", "primary_catalyst": "earnings"},
        },
    })
    assert "[alpha:" not in formatted


def test_signal_alpha_format_correct(isolate_outcomes):
    """5 records, 3 positive → 60% WR / 5t annotation."""
    _write_outcomes(isolate_outcomes, [
        _record("MSFT", "alpha_positive",  0.05),
        _record("MSFT", "alpha_positive",  0.04),
        _record("MSFT", "alpha_positive",  0.03),
        _record("MSFT", "alpha_negative", -0.02),
        _record("MSFT", "alpha_negative", -0.01),
    ])
    formatted = signal_mod.format_signal_scores({
        "scored_symbols": {
            "MSFT": {"score": 80, "conviction": "high"},
        },
    })
    assert "[alpha: 60% WR / 5t]" in formatted


# ─── signal-source credibility tests (13-16) ─────────────────────────────────

def test_credibility_updated_after_classify(isolate_credibility):
    """alpha_positive outcome → contribution_score moves toward 1.0."""
    rec0 = signal_credibility.update_signal_credibility_from_outcome(
        "macro_wire", "alpha_positive", outcome_pct=0.04, decision_id="d1",
    )
    assert rec0 is not None
    assert rec0.contribution_score > 0.5  # rolling average pulled toward 1.0


def test_credibility_sample_count_increments(isolate_credibility):
    for i in range(3):
        signal_credibility.update_signal_credibility_from_outcome(
            "insider_intelligence", "alpha_neutral", outcome_pct=0.0,
            decision_id=f"d{i}",
        )
    rec = signal_credibility.get_credibility("insider_intelligence")
    assert rec.sample_count == 3


def test_credibility_provisional_at_5_samples(isolate_credibility):
    for i in range(5):
        signal_credibility.update_signal_credibility_from_outcome(
            "macro_wire", "alpha_positive", outcome_pct=0.02, decision_id=f"d{i}",
        )
    rec = signal_credibility.get_credibility("macro_wire")
    assert rec.score_status == "provisional"


def test_credibility_active_at_threshold(isolate_credibility):
    for i in range(10):
        signal_credibility.update_signal_credibility_from_outcome(
            "macro_wire", "alpha_positive", outcome_pct=0.02, decision_id=f"d{i}",
        )
    rec = signal_credibility.get_credibility("macro_wire")
    assert rec.score_status == "active"


# ─── weekly review tests (17-19) ─────────────────────────────────────────────

def test_weekly_review_receives_win_rates(monkeypatch, tmp_path):
    """Classified decisions feed _build_quantitative_performance_block."""
    outcomes_path = tmp_path / "decision_outcomes.jsonl"
    _write_outcomes(outcomes_path, [
        _record("NVDA", "alpha_positive",  0.05),
        _record("NVDA", "alpha_positive",  0.04),
        _record("NVDA", "alpha_negative", -0.02),
        _record("DIS",  "alpha_neutral",   0.00),
        _record("DIS",  "alpha_neutral",   0.00),
        _record("DIS",  "alpha_negative", -0.02),
    ])
    monkeypatch.chdir(tmp_path)
    Path("data/analytics").mkdir(parents=True, exist_ok=True)
    Path("data/analytics/decision_outcomes.jsonl").write_text(outcomes_path.read_text())
    block = weekly_review._build_quantitative_performance_block()
    assert "NVDA" in block["win_rates_by_symbol"]
    assert block["win_rates_by_symbol"]["NVDA"] > 0.5
    assert block["total_classified"] == 6


def test_weekly_review_identifies_top_performers(monkeypatch, tmp_path):
    """Symbol with high win rate & n>=3 lands in top_performing_symbols."""
    outcomes_path = tmp_path / "decision_outcomes.jsonl"
    _write_outcomes(outcomes_path, [
        _record("NVDA", "alpha_positive",  0.05),
        _record("NVDA", "alpha_positive",  0.04),
        _record("NVDA", "alpha_positive",  0.06),
        _record("NVDA", "alpha_negative", -0.02),
        _record("BAD",  "alpha_negative", -0.05),
        _record("BAD",  "alpha_negative", -0.04),
        _record("BAD",  "alpha_negative", -0.06),
    ])
    monkeypatch.chdir(tmp_path)
    Path("data/analytics").mkdir(parents=True, exist_ok=True)
    Path("data/analytics/decision_outcomes.jsonl").write_text(outcomes_path.read_text())
    block = weekly_review._build_quantitative_performance_block()
    assert "NVDA" in block["top_performing_symbols"]
    assert "BAD"  in block["underperforming_symbols"]


def test_weekly_review_quantitative_section_format(monkeypatch, tmp_path):
    """Block contains the required keys with correct types."""
    outcomes_path = tmp_path / "decision_outcomes.jsonl"
    _write_outcomes(outcomes_path, [
        _record("X", "alpha_positive",  0.02),
        _record("X", "alpha_positive",  0.01),
        _record("X", "alpha_negative", -0.01),
    ])
    monkeypatch.chdir(tmp_path)
    Path("data/analytics").mkdir(parents=True, exist_ok=True)
    Path("data/analytics/decision_outcomes.jsonl").write_text(outcomes_path.read_text())
    block = weekly_review._build_quantitative_performance_block()
    for key in (
        "win_rates_by_symbol",
        "classification_distribution",
        "top_performing_symbols",
        "underperforming_symbols",
        "signal_source_win_rates",
        "total_classified",
    ):
        assert key in block, f"missing key: {key}"
    assert isinstance(block["win_rates_by_symbol"], dict)
    assert isinstance(block["top_performing_symbols"], list)
    assert isinstance(block["total_classified"], int)
