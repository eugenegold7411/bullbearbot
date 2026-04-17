"""
experience_library.py — Experience library (T2.7).

Stores closed trades as experience records: success, failure, repaired failure.
Feature flag: enable_experience_library.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import feature_flags

log = logging.getLogger(__name__)

_EXPERIENCE_LOG = Path("data/analytics/experience_library.jsonl")

_VALID_RECORD_TYPES = {"success_case", "failure_case", "repaired_failure_case"}


# ─────────────────────────────────────────────────────────────────────────────
# Dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ExperienceRecord:
    schema_version: int = 1
    experience_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    record_type: str = "failure_case"
    symbol: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    decision_id: str = ""
    forensic_id: Optional[str] = None
    checksum_id: Optional[str] = None
    catalyst_id: Optional[str] = None
    thesis_type: Optional[str] = None
    catalyst_type: Optional[str] = None
    regime_at_entry: Optional[str] = None
    close_reason: Optional[str] = None
    realized_pnl: Optional[float] = None
    hold_duration_hours: Optional[float] = None
    what_worked: Optional[str] = None
    what_failed: Optional[str] = None
    repair_marker: Optional[str] = None
    alpha_classification: Optional[str] = None
    pattern_tags: list = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "experience_id": self.experience_id,
            "record_type": self.record_type,
            "symbol": self.symbol,
            "created_at": self.created_at,
            "decision_id": self.decision_id,
            "forensic_id": self.forensic_id,
            "checksum_id": self.checksum_id,
            "catalyst_id": self.catalyst_id,
            "thesis_type": self.thesis_type,
            "catalyst_type": self.catalyst_type,
            "regime_at_entry": self.regime_at_entry,
            "close_reason": self.close_reason,
            "realized_pnl": self.realized_pnl,
            "hold_duration_hours": self.hold_duration_hours,
            "what_worked": self.what_worked,
            "what_failed": self.what_failed,
            "repair_marker": self.repair_marker,
            "alpha_classification": self.alpha_classification,
            "pattern_tags": self.pattern_tags,
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ExperienceRecord":
        return cls(
            schema_version=d.get("schema_version", 1),
            experience_id=d.get("experience_id", str(uuid.uuid4())),
            record_type=d.get("record_type", "failure_case"),
            symbol=d.get("symbol", ""),
            created_at=d.get("created_at", ""),
            decision_id=d.get("decision_id", ""),
            forensic_id=d.get("forensic_id"),
            checksum_id=d.get("checksum_id"),
            catalyst_id=d.get("catalyst_id"),
            thesis_type=d.get("thesis_type"),
            catalyst_type=d.get("catalyst_type"),
            regime_at_entry=d.get("regime_at_entry"),
            close_reason=d.get("close_reason"),
            realized_pnl=d.get("realized_pnl"),
            hold_duration_hours=d.get("hold_duration_hours"),
            what_worked=d.get("what_worked"),
            what_failed=d.get("what_failed"),
            repair_marker=d.get("repair_marker"),
            alpha_classification=d.get("alpha_classification"),
            pattern_tags=d.get("pattern_tags", []),
            summary=d.get("summary", ""),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def save_experience(record: ExperienceRecord) -> Optional[str]:
    """
    Validates and appends to experience_library.jsonl.
    Returns experience_id or None.
    Raises ValueError if record_type=repaired_failure_case and repair_marker empty.
    """
    if record.record_type == "repaired_failure_case":
        if not record.repair_marker or not str(record.repair_marker).strip():
            raise ValueError(
                "repaired_failure_case requires a non-empty repair_marker describing the repair"
            )

    if record.record_type not in _VALID_RECORD_TYPES:
        log.warning("[EXP] unknown record_type %r — saving anyway", record.record_type)

    if not feature_flags.is_enabled("enable_experience_library"):
        return None

    try:
        _EXPERIENCE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _EXPERIENCE_LOG.open("a") as f:
            f.write(json.dumps(record.to_dict()) + "\n")
        return record.experience_id
    except Exception as exc:
        log.warning("[EXP] save_experience failed: %s", exc)
        return None


def get_experiences(
    symbol: Optional[str] = None,
    record_type: Optional[str] = None,
    thesis_type: Optional[str] = None,
    catalyst_type: Optional[str] = None,
    regime: Optional[str] = None,
    close_reason: Optional[str] = None,
    days_back: int = 365,
    limit: int = 20,
) -> list[ExperienceRecord]:
    """Flexible query over experience library. Returns [] on error."""
    try:
        if not _EXPERIENCE_LOG.exists():
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        results = []
        for line in _EXPERIENCE_LOG.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                ts_str = d.get("created_at", "")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if ts < cutoff:
                            continue
                    except Exception:
                        pass
                if symbol and d.get("symbol") != symbol:
                    continue
                if record_type and d.get("record_type") != record_type:
                    continue
                if thesis_type and d.get("thesis_type") != thesis_type:
                    continue
                if catalyst_type and d.get("catalyst_type") != catalyst_type:
                    continue
                if regime and d.get("regime_at_entry") != regime:
                    continue
                if close_reason and d.get("close_reason") != close_reason:
                    continue
                results.append(ExperienceRecord.from_dict(d))
            except Exception:
                continue
        # newest first
        results.sort(key=lambda r: r.created_at, reverse=True)
        return results[:limit]
    except Exception as exc:
        log.warning("[EXP] get_experiences failed: %s", exc)
        return []


def build_experience_from_forensic(
    forensic: object,
    record_type: Optional[str] = None,
    repair_marker: Optional[str] = None,
) -> ExperienceRecord:
    """
    Convenience constructor from ForensicRecord.
    Auto-determines record_type from thesis_verdict if not provided.
    repair_marker required if record_type=repaired_failure_case.
    """
    tv = getattr(forensic, "thesis_verdict", "inconclusive")
    if record_type is None:
        if tv == "correct":
            record_type = "success_case"
        elif tv in ("incorrect", "partial"):
            record_type = "failure_case"
        else:
            record_type = "failure_case"

    symbol = getattr(forensic, "symbol", "")
    decision_id = getattr(forensic, "decision_id", "")

    # Pull thesis/catalyst from linked checksum
    thesis_type = None
    catalyst_type = None
    checksum_id = getattr(forensic, "checksum_id", None)
    catalyst_id = None
    try:
        if checksum_id:
            from thesis_checksum import get_checksum as _gc  # noqa: PLC0415
            cs = _gc(decision_id)
            if cs:
                thesis_type = cs.thesis_type
                catalyst_type = cs.catalyst_type
    except Exception:
        pass

    summary = (
        f"{record_type.replace('_', ' ').title()} — {symbol}: "
        f"{'profit' if (getattr(forensic, 'realized_pnl', None) or 0) > 0 else 'loss'}, "
        f"thesis={tv}"
    )

    return ExperienceRecord(
        record_type=record_type,
        symbol=symbol,
        decision_id=decision_id,
        forensic_id=getattr(forensic, "forensic_id", None),
        checksum_id=checksum_id,
        catalyst_id=catalyst_id,
        thesis_type=thesis_type,
        catalyst_type=catalyst_type,
        regime_at_entry=None,
        close_reason=None,
        realized_pnl=getattr(forensic, "realized_pnl", None),
        hold_duration_hours=getattr(forensic, "hold_duration_hours", None),
        what_worked=getattr(forensic, "what_worked", None),
        what_failed=getattr(forensic, "what_failed", None),
        repair_marker=repair_marker,
        alpha_classification=getattr(forensic, "alpha_classification", None),
        pattern_tags=list(getattr(forensic, "pattern_tags", [])),
        summary=summary,
    )


def format_experience_summary_for_review(days_back: int = 30) -> str:
    """
    Returns markdown summary for Agent 2 (Risk Manager). Returns '' on error/no data.
    """
    if not feature_flags.is_enabled("enable_experience_library"):
        return ""
    try:
        records = get_experiences(days_back=days_back)
        if not records:
            return ""
        type_counts: dict[str, int] = {}
        for r in records:
            type_counts[r.record_type] = type_counts.get(r.record_type, 0) + 1
        lines = [f"### Experience Library Summary (last {days_back}d)", ""]
        lines.append(f"Total records: {len(records)}")
        for rt, cnt in sorted(type_counts.items()):
            lines.append(f"- {rt}: {cnt}")

        repairs = [r for r in records if r.record_type == "repaired_failure_case" and r.repair_marker]
        if repairs:
            lines.append(f"\nTop repaired failure: {repairs[0].summary[:100]}")
        return "\n".join(lines)
    except Exception:
        return ""
