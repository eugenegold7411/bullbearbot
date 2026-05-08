"""QW5 / #40 — alpha context injection into A2 debate prompt."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import bot_options_stage3_debate as debate  # noqa: E402
import decision_outcomes  # noqa: E402


def _make_record(symbol: str, classification: str, outcome_pct: float = 0.01) -> dict:
    return {
        "decision_id":          f"dec_{symbol}",
        "account":              "A1",
        "symbol":               symbol,
        "timestamp":            "2026-04-01T10:00:00+00:00",
        "action":               "buy",
        "alpha_classification": classification,
        "outcome_pct":          outcome_pct,
    }


def _write_outcomes(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


@pytest.fixture
def isolate_outcomes(tmp_path, monkeypatch) -> Path:
    p = tmp_path / "decision_outcomes.jsonl"
    monkeypatch.setattr(decision_outcomes, "OUTCOMES_LOG", p)
    return p


def test_alpha_summary_correct(isolate_outcomes):
    _write_outcomes(isolate_outcomes, [
        _make_record("AAPL", "alpha_positive",  0.05),
        _make_record("AAPL", "alpha_positive",  0.02),
        _make_record("AAPL", "alpha_negative", -0.03),
        _make_record("AAPL", "alpha_neutral",   0.00),
    ])
    s = decision_outcomes.get_alpha_summary("AAPL")
    assert s is not None
    assert s["n"] == 4
    assert s["win_rate"] == 0.5  # 2 wins / 4 records
    assert abs(s["avg_outcome_pct"] - 0.01) < 1e-3
    assert s["classifications"]["alpha_positive"] == 2
    assert s["classifications"]["alpha_negative"] == 1
    # last_outcomes is most-recent-first (the final row was alpha_neutral)
    assert s["last_outcomes"][0] == "alpha_neutral"
    assert s["last_n_outcomes"][-1] == "alpha_neutral"


def test_alpha_summary_min_sample(isolate_outcomes):
    """n=1 < _ALPHA_MIN_SAMPLE (2) → returns None."""
    _write_outcomes(isolate_outcomes, [
        _make_record("XYZ", "alpha_positive",  0.05),
    ])
    assert decision_outcomes.get_alpha_summary("XYZ") is None


def test_debate_prompt_includes_alpha(isolate_outcomes):
    """n=2 (at threshold — STNG case) → injection with thin-sample marker."""
    _write_outcomes(isolate_outcomes, [
        _make_record("STNG", "alpha_positive",  0.05),
        _make_record("STNG", "alpha_negative", -0.03),
    ])
    out = debate._format_alpha_summary_for_debate("STNG")
    assert "HISTORICAL ALPHA — STNG" in out
    assert "Win rate" in out
    assert "Avg outcome" in out
    # n=2 < _ALPHA_THIN_THRESHOLD (5) → thin marker present
    assert "thin sample" in out


def test_debate_prompt_excludes_thin_alpha(isolate_outcomes):
    """n=1 (below min=2) → no injection (empty string)."""
    _write_outcomes(isolate_outcomes, [
        _make_record("XYZ", "alpha_positive", 0.05),
    ])
    out = debate._format_alpha_summary_for_debate("XYZ")
    assert out == ""
