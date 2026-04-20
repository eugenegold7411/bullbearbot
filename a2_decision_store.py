"""
a2_decision_store.py — Query interface over persisted A2 decision artifacts.

Reads from data/account2/decisions/a2_dec_YYYYMMDD_HHMMSS.json files written
by bot_options_stage4_execution.persist_decision_record().

Public API:
  load_decisions(date=None, limit=50) -> list[dict]
  get_daily_summary(date=None) -> dict
  get_decision_by_id(decision_id) -> Optional[dict]

All functions are non-fatal and importable with no env vars set.
"""

from __future__ import annotations

import json
from datetime import date as _date
from pathlib import Path
from typing import Optional

_DECISIONS_DIR = Path(__file__).parent / "data" / "account2" / "decisions"

_VETO_REASON_LABELS: dict[str, str] = {
    "bid_ask_spread_pct": "spread_too_wide",
    "open_interest":      "low_open_interest",
    "theta_decay_rate":   "theta_too_punitive",
    "max_loss":           "position_too_large",
    "dte":                "dte_too_near",
    "expected_value":     "negative_ev",
}


def _load_record(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _files_for_date(d: _date) -> list[Path]:
    prefix = f"a2_dec_{d.strftime('%Y%m%d')}_"
    return sorted(_DECISIONS_DIR.glob(f"{prefix}*.json"))


def _normalize_veto_reason(raw: str) -> str:
    """
    Convert raw veto reason strings like 'bid_ask_spread_pct=0.062>0.05'
    to a stable aggregation key like 'spread_too_wide'.
    """
    field = raw.split("=")[0] if "=" in raw else raw
    return _VETO_REASON_LABELS.get(field, field)


# ── Public API ────────────────────────────────────────────────────────────────

def load_decisions(date: str = None, limit: int = 50) -> list[dict]:
    """
    Load persisted A2 decision records for a given date.

    date=None means today. Format: "YYYY-MM-DD".
    Returns list of raw record dicts (oldest first), capped at limit.
    """
    if not _DECISIONS_DIR.exists():
        return []
    try:
        target = _date.fromisoformat(date) if date else _date.today()
        files = _files_for_date(target)[-limit:]
        records: list[dict] = []
        for f in files:
            rec = _load_record(f)
            if rec is not None:
                records.append(rec)
        return records
    except Exception:
        return []


def get_daily_summary(date: str = None) -> dict:
    """
    Aggregate stats for one trading day from persisted A2DecisionRecord files.

    Returns:
    {
        "date": "2026-04-21",
        "cycles_run": 45,
        "symbols_evaluated": 12,
        "candidates_generated": 8,
        "candidates_vetoed": 23,
        "veto_reasons": {"spread_too_wide": 12, "theta_too_punitive": 6, ...},
        "debate_runs": 3,
        "debate_rejects": 2,
        "debate_low_confidence": 1,
        "executions_attempted": 0,
        "executions_filled": 0,
        "no_trade_reasons": {"no_candidates_after_veto": 42, ...},
        "missing_data_failures": 2,
        "bootstrap_queue_additions": 0,
    }
    """
    try:
        target = _date.fromisoformat(date) if date else _date.today()
    except Exception:
        target = _date.today()

    records = load_decisions(date=target.isoformat(), limit=500)

    cycles_run           = len(records)
    symbols_evaluated    = 0
    candidates_generated = 0
    candidates_vetoed    = 0
    veto_reasons: dict[str, int] = {}
    debate_runs          = 0
    debate_rejects       = 0
    debate_low_confidence = 0
    executions_attempted = 0
    executions_filled    = 0
    no_trade_reasons: dict[str, int] = {}
    missing_data_failures = 0

    for rec in records:
        for cs in rec.get("candidate_sets", []):
            symbols_evaluated    += 1
            candidates_generated += len(cs.get("generated_candidates", []))
            for vetoed in cs.get("vetoed_candidates", []):
                candidates_vetoed += 1
                label = _normalize_veto_reason(vetoed.get("reason", "unknown"))
                veto_reasons[label] = veto_reasons.get(label, 0) + 1
            missing_data_failures += len(cs.get("generation_errors", []))

        parsed = rec.get("debate_parsed")
        if parsed is not None:
            debate_runs += 1
            if parsed.get("reject", False):
                debate_rejects += 1

        ntr = rec.get("no_trade_reason")
        if ntr:
            if ntr == "debate_low_confidence":
                debate_low_confidence += 1
            no_trade_reasons[ntr] = no_trade_reasons.get(ntr, 0) + 1

        exec_result = rec.get("execution_result")
        if exec_result and exec_result not in ("no_trade", None):
            executions_attempted += 1
            if exec_result == "submitted":
                executions_filled += 1

    return {
        "date":                   target.isoformat(),
        "cycles_run":             cycles_run,
        "symbols_evaluated":      symbols_evaluated,
        "candidates_generated":   candidates_generated,
        "candidates_vetoed":      candidates_vetoed,
        "veto_reasons":           veto_reasons,
        "debate_runs":            debate_runs,
        "debate_rejects":         debate_rejects,
        "debate_low_confidence":  debate_low_confidence,
        "executions_attempted":   executions_attempted,
        "executions_filled":      executions_filled,
        "no_trade_reasons":       no_trade_reasons,
        "missing_data_failures":  missing_data_failures,
        "bootstrap_queue_additions": 0,
    }


def get_decision_by_id(decision_id: str) -> Optional[dict]:
    """
    Load a specific A2 decision record by decision_id.
    Scans all persisted files, newest first.
    Returns the raw record dict, or None if not found.
    """
    if not _DECISIONS_DIR.exists():
        return None
    try:
        for path in sorted(_DECISIONS_DIR.glob("a2_dec_*.json"), reverse=True):
            rec = _load_record(path)
            if rec and rec.get("decision_id") == decision_id:
                return rec
    except Exception:
        pass
    return None
