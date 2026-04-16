"""
incident_schema.py — Shared incident record for divergence + A2 lifecycle (T1.5).

One schema, two use cases. Append-only JSONL at data/analytics/incident_log.jsonl.

Feature flag: enable_schema_migrations gates incident logging.
If False, log_incident() is a no-op returning None.
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

_INCIDENT_PATH = Path("data/analytics/incident_log.jsonl")


@dataclass
class IncidentRecord:
    schema_version: int = 1
    incident_id: str = ""
    incident_type: str = ""         # from IncidentType enum in semantic_labels.py
    account: str = ""               # "account1" | "account2"
    severity: str = ""              # "info" | "warning" | "critical" | "halt"
    detected_at: str = ""           # ISO8601 UTC
    resolved_at: Optional[str] = None
    subject_id: Optional[str] = None
    subject_type: Optional[str] = None
    description: str = ""
    root_cause: Optional[str] = None
    resolution: Optional[str] = None
    linked_divergence_event: Optional[str] = None
    linked_structure_id: Optional[str] = None
    metadata: Optional[dict] = None


def build_incident(
    incident_type: str,
    account: str,
    severity: str,
    description: str,
    **kwargs,
) -> IncidentRecord:
    """Convenience constructor. Auto-generates incident_id and detected_at."""
    return IncidentRecord(
        schema_version=1,
        incident_id=str(uuid.uuid4()),
        incident_type=incident_type,
        account=account,
        severity=severity,
        detected_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        description=description,
        subject_id=kwargs.get("subject_id"),
        subject_type=kwargs.get("subject_type"),
        root_cause=kwargs.get("root_cause"),
        resolution=kwargs.get("resolution"),
        resolved_at=kwargs.get("resolved_at"),
        linked_divergence_event=kwargs.get("linked_divergence_event"),
        linked_structure_id=kwargs.get("linked_structure_id"),
        metadata=kwargs.get("metadata"),
    )


def log_incident(record: IncidentRecord) -> Optional[str]:
    """
    Append to incident JSONL. Returns incident_id on success, None on failure.
    Non-fatal. No-op if enable_schema_migrations flag is False.
    """
    try:
        from feature_flags import is_enabled  # noqa: PLC0415
        if not is_enabled("enable_schema_migrations"):
            return None
        _INCIDENT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_INCIDENT_PATH, "a") as fh:
            fh.write(json.dumps(asdict(record)) + "\n")
        return record.incident_id
    except Exception as exc:  # noqa: BLE001
        log.warning("[INCIDENT] log_incident failed: %s", exc)
        return None


def get_incidents(
    incident_type: Optional[str] = None,
    account: Optional[str] = None,
    severity: Optional[str] = None,
    days_back: int = 7,
    resolved: Optional[bool] = None,
) -> list[dict]:
    """
    Filter incidents by any combination of fields. Returns [] on error.
    resolved=True → only resolved; resolved=False → only unresolved; None → both.
    """
    try:
        if not _INCIDENT_PATH.exists():
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        results: list[dict] = []
        with open(_INCIDENT_PATH) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts_str = rec.get("detected_at", "")
                    if ts_str:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if ts < cutoff:
                            continue
                    if incident_type and rec.get("incident_type") != incident_type:
                        continue
                    if account and rec.get("account") != account:
                        continue
                    if severity and rec.get("severity") != severity:
                        continue
                    if resolved is True and rec.get("resolved_at") is None:
                        continue
                    if resolved is False and rec.get("resolved_at") is not None:
                        continue
                    results.append(rec)
                except Exception:
                    pass
        return results
    except Exception as exc:  # noqa: BLE001
        log.warning("[INCIDENT] get_incidents failed: %s", exc)
        return []
