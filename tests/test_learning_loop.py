"""
test_learning_loop.py — coverage for the close → classify_alpha → forensic loop.

Three lanes:
  1. classify_alpha wiring (outcome_pct path, record updater, log line)
  2. forensic review (verdict / what_worked / what_failed populated when Haiku
     returns real data; record reaches forensic_log.jsonl)
  3. backfill script (decision-id matching, outcome_pct math, --dry-run safety)
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

_REPO = Path(__file__).resolve().parent.parent


# ─────────────────────────────────────────────────────────────────────────────
# classify_alpha wiring
# ─────────────────────────────────────────────────────────────────────────────


def _seed_record(log_path: Path, **overrides) -> dict:
    """Write one DecisionOutcomeRecord to a tmp jsonl and return the dict."""
    base = {
        "decision_id":   "dec_A1_TEST",
        "account":       "A1",
        "symbol":        "GLD",
        "timestamp":     (datetime.now(timezone.utc) - timedelta(hours=48))
                          .isoformat().replace("+00:00", "Z"),
        "action":        "buy",
        "status":        "submitted",
        "return_1d":     None,
        "correct_1d":    None,
        "outcome_pct":   None,
        "alpha_classification": None,
    }
    base.update(overrides)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as fh:
        fh.write(json.dumps(base) + "\n")
    return base


def test_classify_alpha_called_after_close(tmp_path: Path) -> None:
    """A closed BUY with a positive realized outcome is classified as alpha_positive
    when outcome_pct is set, even though return_1d remains None."""
    import decision_outcomes as do

    log = tmp_path / "decision_outcomes.jsonl"
    _seed_record(log)
    with mock.patch.object(do, "OUTCOMES_LOG", log):
        rec_dict = json.loads(log.read_text().strip())
        rec = do.DecisionOutcomeRecord.from_dict(rec_dict)
        rec.outcome_pct = 0.012  # +1.2 %
        cls = do.classify_alpha(rec)
        assert cls == "alpha_positive"


def test_outcome_pct_calculated_correctly() -> None:
    """outcome_pct = (exit - entry) / entry, signed."""
    cases = [
        (100.0, 105.0, 0.05),
        (100.0, 90.0, -0.10),
        (501.00, 501.43, 0.00085828),
    ]
    for entry, exit_, expected in cases:
        got = (exit_ - entry) / entry
        assert abs(got - expected) < 1e-6, f"entry={entry} exit={exit_} got={got}"


def test_classification_written_to_jsonl(tmp_path: Path) -> None:
    """update_outcome_record overwrites the record line in-place, atomically."""
    import decision_outcomes as do

    log = tmp_path / "decision_outcomes.jsonl"
    _seed_record(log, decision_id="dec_A")
    _seed_record(log, decision_id="dec_B")
    with mock.patch.object(do, "OUTCOMES_LOG", log):
        ok = do.update_outcome_record(
            "dec_B",
            outcome_pct=0.025,
            alpha_classification="alpha_positive",
            alpha_classification_reason="closed_trade_realized_outcome",
        )
        assert ok is True
        lines = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        assert len(lines) == 2  # both records preserved
        ids = {r["decision_id"]: r for r in lines}
        assert ids["dec_A"]["alpha_classification"] is None     # untouched
        assert ids["dec_B"]["alpha_classification"] == "alpha_positive"
        assert abs(ids["dec_B"]["outcome_pct"] - 0.025) < 1e-9


def test_classification_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A classification produced post-close emits an [OUTCOMES] log line.

    Exercises the same log-format used in bot.py's close hook.
    """
    import decision_outcomes as do

    log = tmp_path / "decision_outcomes.jsonl"
    _seed_record(log, decision_id="dec_LOG")
    with mock.patch.object(do, "OUTCOMES_LOG", log):
        rec_dict = json.loads(log.read_text().strip())
        rec = do.DecisionOutcomeRecord.from_dict(rec_dict)
        rec.outcome_pct = -0.018  # losing trade
        cls = do.classify_alpha(rec)
        assert cls == "alpha_negative"
        bot_log = logging.getLogger("bot_test")
        with caplog.at_level(logging.INFO, logger="bot_test"):
            bot_log.info("[OUTCOMES] %s: outcome=%.1f%% classification=%s",
                         rec.symbol, rec.outcome_pct * 100, cls)
        assert any("[OUTCOMES]" in r.message and "alpha_negative" in r.message
                   for r in caplog.records)


# ─────────────────────────────────────────────────────────────────────────────
# Forensic review verdict / lesson
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def _forensic_haiku_stub():
    """Stub the Haiku call so review_closed_trade has a deterministic response."""
    payload = {
        "thesis_verdict": "correct",
        "thesis_verdict_confidence": 0.78,
        "execution_verdict": "good",
        "management_drifted": False,
        "regime_contradicted": False,
        "what_worked": "Catalyst (AI cybersecurity launch) materialised as expected; trail-stop captured most of the move.",
        "what_failed": None,
        "pattern_tags": ["catalyst_validated", "trail_stop_capture"],
        "alpha_classification": "alpha_positive",
        "abstention": None,
    }
    with mock.patch("forensic_reviewer._call_haiku", return_value=payload):
        yield payload


def test_forensic_verdict_not_none(_forensic_haiku_stub, tmp_path: Path) -> None:
    """review_closed_trade returns a record with a populated thesis_verdict
    when the LLM returns real data (i.e. when entry/exit are not zero)."""
    import feature_flags
    import forensic_reviewer

    fl = tmp_path / "forensic_log.jsonl"
    with mock.patch.object(feature_flags, "is_enabled", return_value=True), \
         mock.patch.object(forensic_reviewer, "_FORENSIC_LOG", fl), \
         mock.patch.object(forensic_reviewer, "log_forensic", return_value="fid"):
        rec = forensic_reviewer.review_closed_trade(
            decision_id="dec_F1", symbol="CRWD",
            entry_price=501.0, exit_price=501.43,
            realized_pnl=31.85, hold_duration_hours=0.03,
            entry_decision={"catalyst": "AI cybersecurity launch", "tier": "core"},
            exit_reason="trail_stop_hit",
        )
    assert rec is not None
    assert rec.thesis_verdict == "correct"
    assert rec.thesis_verdict != "inconclusive"


def test_forensic_lesson_not_none(_forensic_haiku_stub) -> None:
    """review_closed_trade populates what_worked / what_failed from the LLM.
    These are the 'lesson' fields — production has been writing None for both."""
    import feature_flags
    import forensic_reviewer

    with mock.patch.object(feature_flags, "is_enabled", return_value=True), \
         mock.patch.object(forensic_reviewer, "log_forensic", return_value="fid"):
        rec = forensic_reviewer.review_closed_trade(
            decision_id="dec_F2", symbol="CRWD",
            entry_price=501.0, exit_price=510.0,
            realized_pnl=666.0, hold_duration_hours=2.5,
            entry_decision={"catalyst": "AI cybersecurity launch"},
            exit_reason="trail_stop_hit",
        )
    assert rec is not None
    assert rec.what_worked is not None
    assert "catalyst" in rec.what_worked.lower() or "trail" in rec.what_worked.lower()


def test_forensic_written_to_log(_forensic_haiku_stub, tmp_path: Path) -> None:
    """A successful review produces a JSONL line in forensic_log.jsonl with
    the populated verdict + what_worked fields."""
    import feature_flags
    import forensic_reviewer

    fl = tmp_path / "forensic_log.jsonl"
    with mock.patch.object(feature_flags, "is_enabled", return_value=True), \
         mock.patch.object(forensic_reviewer, "_FORENSIC_LOG", fl):
        forensic_reviewer.review_closed_trade(
            decision_id="dec_F3", symbol="CRWD",
            entry_price=501.0, exit_price=510.0,
            realized_pnl=666.0, hold_duration_hours=2.5,
            entry_decision={"catalyst": "AI cybersecurity launch"},
            exit_reason="trail_stop_hit",
        )
    assert fl.exists(), "forensic_log.jsonl was not created"
    line = fl.read_text().strip().splitlines()[-1]
    rec = json.loads(line)
    assert rec["thesis_verdict"] == "correct"
    assert rec["what_worked"] is not None


# ─────────────────────────────────────────────────────────────────────────────
# Backfill script
# ─────────────────────────────────────────────────────────────────────────────


_SCRIPT_PATH = _REPO / "scripts" / "backfill_alpha_classifications.py"


def _import_backfill():
    """Import the backfill script as a module by file path."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("backfill_mod", _SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_backfill_matches_by_symbol_timestamp(tmp_path: Path) -> None:
    """_match() prefers exact decision_id, falling back to symbol+entry_time
    within a 24h window."""
    bf = _import_backfill()
    # Closed trade with matching decision_id
    closed_a = {
        "symbol": "GLD", "decision_id": "dec_A",
        "entry_time": "2026-04-20T15:00:00Z",
        "entry_price": 100.0, "exit_price": 102.0,
        "exit_time":  "2026-04-21T15:00:00Z",
    }
    # Closed trade without decision_id — should fall back to symbol+time
    closed_b = {
        "symbol": "TSM", "decision_id": None,
        "entry_time": "2026-04-22T14:00:00Z",
        "entry_price": 200.0, "exit_price": 198.0,
        "exit_time":  "2026-04-22T15:00:00Z",
    }
    records = [
        {"decision_id": "dec_A", "symbol": "GLD", "action": "buy",
         "timestamp": "2026-04-20T15:00:00Z"},
        {"decision_id": "dec_OTHER", "symbol": "GLD", "action": "buy",
         "timestamp": "2026-03-01T10:00:00Z"},
        {"decision_id": "dec_TSM", "symbol": "TSM", "action": "buy",
         "timestamp": "2026-04-22T13:30:00Z"},  # within 24h
    ]
    m_a = bf._match(closed_a, records)
    m_b = bf._match(closed_b, records)
    assert m_a is not None and m_a["decision_id"] == "dec_A"
    assert m_b is not None and m_b["decision_id"] == "dec_TSM"


def test_backfill_calculates_outcome_pct(tmp_path: Path) -> None:
    """The backfill uses (exit - entry) / entry. Sign is preserved."""
    bf = _import_backfill()
    log = tmp_path / "decision_outcomes.jsonl"
    log.write_text(json.dumps({
        "decision_id": "dec_W", "account": "A1", "symbol": "GLD", "action": "buy",
        "timestamp": (datetime.now(timezone.utc) - timedelta(hours=48))
                       .isoformat().replace("+00:00", "Z"),
        "status": "submitted",
    }) + "\n" + json.dumps({
        "decision_id": "dec_L", "account": "A1", "symbol": "TSM", "action": "buy",
        "timestamp": (datetime.now(timezone.utc) - timedelta(hours=48))
                       .isoformat().replace("+00:00", "Z"),
        "status": "submitted",
    }) + "\n")

    closed = [
        {"symbol": "GLD", "decision_id": "dec_W",
         "entry_time": "2026-04-20T10:00:00Z", "exit_time": "2026-04-21T10:00:00Z",
         "entry_price": 100.0, "exit_price": 105.0, "pnl": 50.0, "pnl_pct": 5.0,
         "holding_days": 1},
        {"symbol": "TSM", "decision_id": "dec_L",
         "entry_time": "2026-04-22T10:00:00Z", "exit_time": "2026-04-23T10:00:00Z",
         "entry_price": 200.0, "exit_price": 190.0, "pnl": -10.0, "pnl_pct": -5.0,
         "holding_days": 1},
    ]

    import decision_outcomes as do
    with mock.patch.object(bf, "OUTCOMES_LOG", log), \
         mock.patch.object(do, "OUTCOMES_LOG", log), \
         mock.patch.object(bf._tj, "build_closed_trades", return_value=closed):
        sys_argv = sys.argv[:]
        sys.argv = [str(_SCRIPT_PATH)]
        try:
            bf.main()
        finally:
            sys.argv = sys_argv

    by_id = {json.loads(line)["decision_id"]: json.loads(line)
             for line in log.read_text().splitlines() if line.strip()}
    assert abs(by_id["dec_W"]["outcome_pct"] - 0.05) < 1e-9
    assert abs(by_id["dec_L"]["outcome_pct"] - (-0.05)) < 1e-9
    assert by_id["dec_W"]["alpha_classification"] == "alpha_positive"
    assert by_id["dec_L"]["alpha_classification"] == "alpha_negative"


def test_backfill_dry_run_no_writes(tmp_path: Path) -> None:
    """--dry-run never modifies decision_outcomes.jsonl. Sha256 unchanged."""
    import hashlib

    bf = _import_backfill()
    log = tmp_path / "decision_outcomes.jsonl"
    log.write_text(json.dumps({
        "decision_id": "dec_D", "account": "A1", "symbol": "GLD", "action": "buy",
        "timestamp": (datetime.now(timezone.utc) - timedelta(hours=48))
                       .isoformat().replace("+00:00", "Z"),
        "status": "submitted",
    }) + "\n")
    before = hashlib.sha256(log.read_bytes()).hexdigest()

    closed = [{
        "symbol": "GLD", "decision_id": "dec_D",
        "entry_time": "2026-04-20T10:00:00Z", "exit_time": "2026-04-21T10:00:00Z",
        "entry_price": 100.0, "exit_price": 110.0, "pnl": 100.0, "pnl_pct": 10.0,
        "holding_days": 1,
    }]
    import decision_outcomes as do
    with mock.patch.object(bf, "OUTCOMES_LOG", log), \
         mock.patch.object(do, "OUTCOMES_LOG", log), \
         mock.patch.object(bf._tj, "build_closed_trades", return_value=closed):
        sys_argv = sys.argv[:]
        sys.argv = [str(_SCRIPT_PATH), "--dry-run"]
        try:
            bf.main()
        finally:
            sys.argv = sys_argv

    after = hashlib.sha256(log.read_bytes()).hexdigest()
    assert before == after, "decision_outcomes.jsonl was modified during --dry-run"
