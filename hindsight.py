"""
hindsight.py — Shared evaluation record schema for post-hoc assessment (T1.2).

One record type, multiple consumers: trades, recommendations, near-misses, incidents.
Append-only JSONL at data/analytics/hindsight_log.jsonl.

Feature flag: enable_recommendation_memory gates logging.
If False, log_hindsight_record() is a no-op returning None.

Cost attribution: logs spine record on write (no tokens — admin event).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_HINDSIGHT_PATH = Path("data/analytics/hindsight_log.jsonl")


# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class HindsightRecord:
    schema_version: int
    record_id: str
    subject_id: str
    subject_type: str               # "trade" | "recommendation" | "near_miss" | "incident"
    created_at: str
    evidence_window_start: str
    evidence_window_end: str
    expected_effect: str
    observed_result: str
    verdict: str                    # "confirmed" | "refuted" | "inconclusive" | "pending"
    confidence: float               # 0.0 – 1.0
    explanation: str
    catalyst_label: Optional[str]
    thesis_label: Optional[str]
    regime_label: Optional[str]
    model_tier: Optional[str]
    evaluator_module: str
    abstention: Optional[dict] = None   # AbstentionRecord if evaluator abstained


# ─────────────────────────────────────────────────────────────────────────────
# Convenience constructor
# ─────────────────────────────────────────────────────────────────────────────

def build_hindsight_record(
    subject_id: str,
    subject_type: str,
    expected_effect: str,
    observed_result: str,
    verdict: str,
    confidence: float,
    explanation: str,
    evidence_window_start: str,
    evidence_window_end: str,
    **kwargs,
) -> HindsightRecord:
    """Convenience constructor. Auto-generates record_id and created_at."""
    return HindsightRecord(
        schema_version=1,
        record_id=str(uuid.uuid4()),
        subject_id=subject_id,
        subject_type=subject_type,
        created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        evidence_window_start=evidence_window_start,
        evidence_window_end=evidence_window_end,
        expected_effect=expected_effect,
        observed_result=observed_result,
        verdict=verdict,
        confidence=confidence,
        explanation=explanation,
        catalyst_label=kwargs.get("catalyst_label"),
        thesis_label=kwargs.get("thesis_label"),
        regime_label=kwargs.get("regime_label"),
        model_tier=kwargs.get("model_tier"),
        evaluator_module=kwargs.get("evaluator_module", "unknown"),
        abstention=kwargs.get("abstention"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def log_hindsight_record(record: HindsightRecord) -> Optional[str]:
    """
    Append to hindsight JSONL. Returns record_id on success, None on failure.
    Non-fatal. No-op if enable_recommendation_memory flag is False.
    """
    try:
        from feature_flags import is_enabled  # noqa: PLC0415
        if not is_enabled("enable_recommendation_memory"):
            return None

        _HINDSIGHT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_HINDSIGHT_PATH, "a") as fh:
            fh.write(json.dumps(asdict(record)) + "\n")

        # Cost attribution spine — admin event, no tokens
        try:
            import cost_attribution as _ca  # noqa: PLC0415
            _ca.log_spine_record(
                module_name=record.evaluator_module,
                layer_name="learning_evaluation",
                ring="prod",
                model=record.model_tier or "unknown",
                purpose="hindsight_logged",
                linked_subject_id=record.subject_id,
                linked_subject_type=record.subject_type,
            )
        except Exception:
            pass

        return record.record_id
    except Exception as exc:  # noqa: BLE001
        log.warning("[HINDSIGHT] log_hindsight_record failed: %s", exc)
        return None


def get_hindsight_records(
    subject_type: Optional[str] = None,
    verdict: Optional[str] = None,
    days_back: int = 30,
) -> list[dict]:
    """
    Read hindsight JSONL, filter by subject_type and/or verdict and date window.
    Returns [] on any error.
    """
    try:
        if not _HINDSIGHT_PATH.exists():
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        results: list[dict] = []
        with open(_HINDSIGHT_PATH) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts_str = rec.get("created_at", "")
                    if ts_str:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if ts < cutoff:
                            continue
                    if subject_type and rec.get("subject_type") != subject_type:
                        continue
                    if verdict and rec.get("verdict") != verdict:
                        continue
                    results.append(rec)
                except Exception:
                    pass
        return results
    except Exception as exc:  # noqa: BLE001
        log.warning("[HINDSIGHT] get_hindsight_records failed: %s", exc)
        return []


def format_hindsight_summary_for_review(days_back: int = 30) -> str:
    """
    Return a brief hindsight summary for Agent 4 injection.
    Shows verdict counts. Returns empty string on error or no data.
    """
    try:
        records = get_hindsight_records(days_back=days_back)
        if not records:
            return ""
        from collections import Counter
        verdicts = Counter(r.get("verdict", "unknown") for r in records)
        lines = [
            f"### Hindsight Evaluations — last {days_back}d",
            f"Total records: {len(records)}",
        ]
        for v, count in sorted(verdicts.items()):
            lines.append(f"- {v}: {count}")
        return "\n".join(lines) + "\n"
    except Exception as exc:  # noqa: BLE001
        log.warning("[HINDSIGHT] format_hindsight_summary_for_review failed: %s", exc)
        return ""
