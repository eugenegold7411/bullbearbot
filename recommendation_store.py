"""
recommendation_store.py — Persistent store for recommendation records (T1.3).

Keyed by stable rec_id. Backed by a single JSON file with versioned schema.
Atomic writes (write to .tmp, rename).

Feature flag: enable_recommendation_memory gates all writes.
Reads always work regardless of flag.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import versioning

log = logging.getLogger(__name__)

_STORE_PATH = Path("data/reports/recommendation_store.json")
_ARTIFACT_TYPE = "recommendation_store"
_CURRENT_VERSION = 1


# ─────────────────────────────────────────────────────────────────────────────
# Schema versioning
# ─────────────────────────────────────────────────────────────────────────────

def _migrate_rec_store_v0_to_v1(artifact: dict) -> dict:
    """v0→v1: add schema_version=1 to any unversioned store."""
    result = dict(artifact)
    result["schema_version"] = 1
    return result


versioning.register_migration(_ARTIFACT_TYPE, 0, _migrate_rec_store_v0_to_v1)


# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RecommendationRecord:
    schema_version: int = 1
    rec_id: str = ""
    week_str: str = ""
    created_at: str = ""
    source_module: str = ""
    recommendation_text: str = ""
    target_metric: Optional[str] = None
    expected_direction: Optional[str] = None    # "improve" | "degrade" | "neutral"
    verdict: str = "pending"                    # "pending" | "verified" | "falsified" | "neutral"
    resolved_at: Optional[str] = None
    resolution_evidence: Optional[str] = None
    linked_hindsight_id: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RecommendationRecord":
        return cls(
            schema_version=d.get("schema_version", 1),
            rec_id=d.get("rec_id", ""),
            week_str=d.get("week_str", ""),
            created_at=d.get("created_at", ""),
            source_module=d.get("source_module", ""),
            recommendation_text=d.get("recommendation_text", ""),
            target_metric=d.get("target_metric"),
            expected_direction=d.get("expected_direction"),
            verdict=d.get("verdict", "pending"),
            resolved_at=d.get("resolved_at"),
            resolution_evidence=d.get("resolution_evidence"),
            linked_hindsight_id=d.get("linked_hindsight_id"),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Internal store I/O
# ─────────────────────────────────────────────────────────────────────────────

def _load_store() -> dict:
    """Load recommendation store JSON. Returns {'schema_version': 1, 'records': {}} on error."""
    try:
        if not _STORE_PATH.exists():
            return {"schema_version": _CURRENT_VERSION, "records": {}}
        raw = json.loads(_STORE_PATH.read_text())
        # Migrate if needed
        found = versioning.detect_version(raw)
        if found < _CURRENT_VERSION:
            raw = versioning.load_with_compat(
                _STORE_PATH,
                current_version=_CURRENT_VERSION,
                migrations=versioning._MIGRATIONS,
            )
        return raw
    except Exception as exc:  # noqa: BLE001
        log.warning("[RECSTORE] _load_store failed: %s", exc)
        return {"schema_version": _CURRENT_VERSION, "records": {}}


def _save_store(store: dict) -> bool:
    """Atomic write of store dict. Returns True on success."""
    try:
        _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STORE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(store, indent=2))
        tmp.rename(_STORE_PATH)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("[RECSTORE] _save_store failed: %s", exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def save_recommendation(record: RecommendationRecord) -> bool:
    """
    Create or update a recommendation record. Atomic write.
    Gated by enable_recommendation_memory flag.
    Returns True on success. Non-fatal.
    """
    try:
        from feature_flags import is_enabled  # noqa: PLC0415
        if not is_enabled("enable_recommendation_memory"):
            return False
        if not record.rec_id:
            log.warning("[RECSTORE] save_recommendation: rec_id is empty")
            return False
        store = _load_store()
        records_dict = store.get("records", {})
        records_dict[record.rec_id] = record.to_dict()
        store["records"] = records_dict
        store["schema_version"] = _CURRENT_VERSION
        return _save_store(store)
    except Exception as exc:  # noqa: BLE001
        log.warning("[RECSTORE] save_recommendation failed: %s", exc)
        return False


def get_recommendation(rec_id: str) -> Optional[RecommendationRecord]:
    """Returns None if not found or on error. Non-fatal. Flag-independent."""
    try:
        store = _load_store()
        raw = store.get("records", {}).get(rec_id)
        if raw is None:
            return None
        return RecommendationRecord.from_dict(raw)
    except Exception as exc:  # noqa: BLE001
        log.warning("[RECSTORE] get_recommendation failed: %s", exc)
        return None


def get_recommendations(
    verdict: Optional[str] = None,
    week_str: Optional[str] = None,
    limit: int = 50,
) -> list[RecommendationRecord]:
    """
    Return list filtered by verdict and/or week_str. [] on error.
    Flag-independent (reads always work).
    """
    try:
        store = _load_store()
        results: list[RecommendationRecord] = []
        for raw in store.get("records", {}).values():
            rec = RecommendationRecord.from_dict(raw)
            if verdict and rec.verdict != verdict:
                continue
            if week_str and rec.week_str != week_str:
                continue
            results.append(rec)
            if len(results) >= limit:
                break
        return results
    except Exception as exc:  # noqa: BLE001
        log.warning("[RECSTORE] get_recommendations failed: %s", exc)
        return []


def update_verdict(
    rec_id: str,
    verdict: str,
    resolution_evidence: str,
    linked_hindsight_id: Optional[str] = None,
) -> bool:
    """
    Update verdict, resolved_at, resolution_evidence on existing record.
    Non-destructive — only touches those fields.
    Returns False if rec_id not found. Gated by flag.
    """
    try:
        from feature_flags import is_enabled  # noqa: PLC0415
        if not is_enabled("enable_recommendation_memory"):
            return False
        store = _load_store()
        records_dict = store.get("records", {})
        if rec_id not in records_dict:
            log.warning("[RECSTORE] update_verdict: rec_id %r not found", rec_id)
            return False
        rec = records_dict[rec_id]
        rec["verdict"] = verdict
        rec["resolved_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        rec["resolution_evidence"] = resolution_evidence
        if linked_hindsight_id is not None:
            rec["linked_hindsight_id"] = linked_hindsight_id
        records_dict[rec_id] = rec
        store["records"] = records_dict
        return _save_store(store)
    except Exception as exc:  # noqa: BLE001
        log.warning("[RECSTORE] update_verdict failed: %s", exc)
        return False
