# SHADOW MODULE — do not import from prod pipeline
"""
shadow_governance.py — Credibility-weighted shadow advisor governance (T4.8).

Tracks credibility scores for shadow advisor modules. Cold-start scaffold —
most advisors show insufficient_sample initially. No strong weighting until
minimum sample threshold met.

Storage: data/analytics/advisor_credibility.json (atomic writes).
Feature flag: enable_shadow_governance (shadow_flags, default False).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_CREDIBILITY_PATH = Path("data/analytics/advisor_credibility.json")
CREDIBILITY_MIN_SAMPLE = 5

_INITIAL_ADVISORS = [
    "context_compiler",
    "semantic_router",
    "replay_debugger",
    "forensic_reviewer",
    "divergence_summarizer",
    "anti_pattern_miner",
]


# ─────────────────────────────────────────────────────────────────────────────
# Dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AdvisorCredibility:
    schema_version: int = 1
    advisor_name: str = ""
    contribution_score: float = 0.5
    calibration_score: float = 0.5
    recent_usefulness_score: float = 0.5
    abstention_quality_score: float = 0.5
    sample_count: int = 0
    score_status: str = "insufficient_sample"
    last_updated_at: str = ""
    score_provenance: dict = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_enabled() -> bool:
    try:
        from feature_flags import is_enabled  # noqa: PLC0415
        return is_enabled("enable_shadow_governance")
    except Exception:
        return False


def _load_store() -> dict:
    try:
        if _CREDIBILITY_PATH.exists():
            return json.loads(_CREDIBILITY_PATH.read_text())
    except Exception as exc:
        log.warning("[GOVERNANCE] _load_store failed: %s", exc)
    return {}


def _save_store(store: dict) -> None:
    try:
        _CREDIBILITY_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CREDIBILITY_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(store, indent=2))
        tmp.rename(_CREDIBILITY_PATH)
    except Exception as exc:
        log.warning("[GOVERNANCE] _save_store failed: %s", exc)


def _record_from_dict(d: dict) -> AdvisorCredibility:
    return AdvisorCredibility(
        schema_version=d.get("schema_version", 1),
        advisor_name=d.get("advisor_name", ""),
        contribution_score=float(d.get("contribution_score", 0.5)),
        calibration_score=float(d.get("calibration_score", 0.5)),
        recent_usefulness_score=float(d.get("recent_usefulness_score", 0.5)),
        abstention_quality_score=float(d.get("abstention_quality_score", 0.5)),
        sample_count=int(d.get("sample_count", 0)),
        score_status=d.get("score_status", "insufficient_sample"),
        last_updated_at=d.get("last_updated_at", ""),
        score_provenance=d.get("score_provenance", {}),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def initialize_advisor(advisor_name: str) -> AdvisorCredibility:
    """
    Creates a neutral-prior credibility record. Idempotent — returns existing if present.
    score_status = "insufficient_sample", all scores = 0.5, sample_count = 0.
    """
    try:
        store = _load_store()
        if advisor_name in store:
            return _record_from_dict(store[advisor_name])

        record = AdvisorCredibility(
            schema_version=1,
            advisor_name=advisor_name,
            contribution_score=0.5,
            calibration_score=0.5,
            recent_usefulness_score=0.5,
            abstention_quality_score=0.5,
            sample_count=0,
            score_status="insufficient_sample",
            last_updated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            score_provenance={"hindsight_ids": [], "outcome_ids": []},
        )
        store[advisor_name] = asdict(record)
        _save_store(store)
        log.debug("[GOVERNANCE] initialized advisor: %s", advisor_name)
        return record
    except Exception as exc:
        log.warning("[GOVERNANCE] initialize_advisor failed for %s: %s", advisor_name, exc)
        return AdvisorCredibility(advisor_name=advisor_name)


def update_credibility_from_hindsight(
    advisor_name: str,
    hindsight_record: dict,
) -> Optional[AdvisorCredibility]:
    """
    Update credibility scores based on a resolved HindsightRecord.
    Only updates if record.verdict != "pending".
    Increments sample_count. Flips score_status to "active" when >= threshold.
    Non-fatal.
    """
    try:
        verdict = hindsight_record.get("verdict", "pending")
        if verdict == "pending":
            return None

        store = _load_store()
        if advisor_name not in store:
            initialize_advisor(advisor_name)
            store = _load_store()

        rec = _record_from_dict(store[advisor_name])
        rec.sample_count += 1

        # Update calibration score: rolling average toward 1.0 if confirmed, 0.0 if refuted
        confidence = float(hindsight_record.get("confidence", 0.5))
        if verdict == "confirmed":
            calibration_delta = confidence
        elif verdict == "refuted":
            calibration_delta = 1.0 - confidence
        else:
            calibration_delta = 0.5

        alpha = 1.0 / max(rec.sample_count, 1)
        rec.calibration_score = round((1 - alpha) * rec.calibration_score + alpha * calibration_delta, 4)

        # Update abstention quality: if abstention was present, score based on appropriateness
        abstention = hindsight_record.get("abstention")
        if abstention:
            abst_alpha = 1.0 / max(rec.sample_count, 1)
            rec.abstention_quality_score = round((1 - abst_alpha) * rec.abstention_quality_score + abst_alpha * 0.8, 4)

        # Update score_status
        if rec.sample_count >= CREDIBILITY_MIN_SAMPLE:
            rec.score_status = "active"

        rec.last_updated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        # Track provenance
        record_id = hindsight_record.get("record_id", "")
        if record_id:
            prov = rec.score_provenance.setdefault("hindsight_ids", [])
            if record_id not in prov:
                prov.append(record_id)

        store[advisor_name] = asdict(rec)
        _save_store(store)
        return rec
    except Exception as exc:
        log.warning("[GOVERNANCE] update_credibility_from_hindsight failed for %s: %s", advisor_name, exc)
        return None


def get_credibility(advisor_name: str) -> Optional[AdvisorCredibility]:
    """Returns current credibility for advisor. None if not initialized."""
    try:
        store = _load_store()
        if advisor_name not in store:
            return None
        return _record_from_dict(store[advisor_name])
    except Exception as exc:
        log.warning("[GOVERNANCE] get_credibility failed: %s", exc)
        return None


def get_all_credibilities() -> list:
    """Returns all advisor records sorted by sample_count desc."""
    try:
        store = _load_store()
        records = [_record_from_dict(v) for v in store.values()]
        return sorted(records, key=lambda r: -r.sample_count)
    except Exception as exc:
        log.warning("[GOVERNANCE] get_all_credibilities failed: %s", exc)
        return []


def get_weighted_summary(advisor_outputs: dict) -> dict:
    """
    Given outputs from multiple advisors, produces a credibility-weighted summary.
    Advisors with insufficient_sample get weight=0.5 (neutral).
    Active advisors get weight proportional to calibration_score.
    Returns {advisor_name: weight, "summary": str, "dominant_advisor": str}.
    Non-fatal. Returns {} on error.
    """
    try:
        if not advisor_outputs:
            return {}

        weights: dict = {}
        for advisor_name in advisor_outputs:
            cred = get_credibility(advisor_name)
            if cred is None or cred.score_status == "insufficient_sample":
                weights[advisor_name] = 0.5
            else:
                weights[advisor_name] = max(0.1, cred.calibration_score)

        dominant = max(weights, key=lambda k: weights[k])
        total_weight = sum(weights.values())
        normalized = {k: round(v / total_weight, 4) for k, v in weights.items()} if total_weight > 0 else weights

        return {
            **normalized,
            "summary": f"Weighted by calibration scores. {len(advisor_outputs)} advisors.",
            "dominant_advisor": dominant,
        }
    except Exception as exc:
        log.warning("[GOVERNANCE] get_weighted_summary failed: %s", exc)
        return {}


def format_credibility_for_review() -> str:
    """
    Returns markdown table of all advisor credibility scores.
    Shows: advisor, status, calibration, abstention_quality, sample_count.
    Returns "" on error or no advisors.
    """
    try:
        records = get_all_credibilities()
        if not records:
            return ""

        lines = [
            "## Advisor Credibility Scores\n",
            "| Advisor | Status | Calibration | Abstention Quality | Samples |",
            "|---------|--------|-------------|-------------------|---------|",
        ]
        for rec in records:
            lines.append(
                f"| {rec.advisor_name} | {rec.score_status} | "
                f"{rec.calibration_score:.2f} | {rec.abstention_quality_score:.2f} | "
                f"{rec.sample_count} |"
            )
        return "\n".join(lines)
    except Exception as exc:
        log.warning("[GOVERNANCE] format_credibility_for_review failed: %s", exc)
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Bootstrap on import
# ─────────────────────────────────────────────────────────────────────────────

def _bootstrap_advisors() -> None:
    """Initialize all required advisors (idempotent)."""
    try:
        for name in _INITIAL_ADVISORS:
            initialize_advisor(name)
    except Exception as exc:
        log.debug("[GOVERNANCE] bootstrap failed (non-fatal): %s", exc)


_bootstrap_advisors()
