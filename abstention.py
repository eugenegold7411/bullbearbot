"""
abstention.py — Universal abstention/uncertainty contract (T1.7).

Any shadow or lab module that cannot reach a conclusion uses this contract
rather than forcing a non-null output.

Feature flag: enable_abstention_contract gates whether weekly review surfaces
abstention metrics. The contract itself (AbstentionRecord, abstain(), did_abstain())
is always functional regardless of flag state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class AbstentionRecord:
    """Universal abstention record. Use abstain() convenience constructor."""
    schema_version: int = 1
    abstain: bool = False
    unknown: bool = False           # input data is unknown/missing
    inconclusive: bool = False      # evidence present but insufficient
    confidence: float = 0.0
    evidence_present: bool = False  # some evidence exists but weak
    abstention_reason: str = ""     # REQUIRED when abstain=True
    module_name: str = ""
    created_at: str = ""            # ISO8601 UTC


def abstain(
    reason: str,
    module_name: str,
    unknown: bool = False,
    inconclusive: bool = False,
    evidence_present: bool = False,
) -> AbstentionRecord:
    """
    Convenience constructor for an abstaining record.
    reason must be non-empty — abstaining without a reason is forbidden.
    Raises ValueError if reason is empty string.
    """
    if not reason or not reason.strip():
        raise ValueError(
            "abstention_reason must be non-empty. "
            "Abstaining without a reason is forbidden by the abstention contract."
        )
    return AbstentionRecord(
        schema_version=1,
        abstain=True,
        unknown=unknown,
        inconclusive=inconclusive,
        confidence=0.0,
        evidence_present=evidence_present,
        abstention_reason=reason.strip(),
        module_name=module_name,
        created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )


def did_abstain(record: "AbstentionRecord | dict | None") -> bool:
    """
    Safe check for abstention. Returns True if record is None or abstain is True.
    Never raises.
    """
    try:
        if record is None:
            return True
        if isinstance(record, dict):
            return bool(record.get("abstain", False))
        return bool(getattr(record, "abstain", False))
    except Exception:  # noqa: BLE001
        return True


def validate_abstention(record: AbstentionRecord) -> list[str]:
    """
    Validate an AbstentionRecord. Returns list of error strings.
    Empty list = valid.

    Errors checked:
    - empty reason when abstaining
    - confidence > 0 when abstaining (confidence must be 0.0 when abstaining)
    - abstain=False with no positive outcome signals
    """
    errors: list[str] = []
    try:
        if record.abstain and not record.abstention_reason.strip():
            errors.append("abstention_reason is empty but abstain=True")
        if record.abstain and record.confidence > 0.0:
            errors.append(
                f"confidence={record.confidence} but abstain=True "
                "(confidence must be 0.0 when abstaining)"
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("[ABSTENTION] validate_abstention failed: %s", exc)
    return errors


def list_modules(records: list[dict]) -> list[str]:
    """
    Return sorted list of distinct module_name values seen in the hindsight records.
    Reads from records[*]["abstention"]["module_name"]. Non-fatal.
    """
    try:
        names: set[str] = set()
        for r in records:
            ab = r.get("abstention") or {}
            if isinstance(ab, dict):
                mn = ab.get("module_name", "")
                if mn:
                    names.add(mn)
        return sorted(names)
    except Exception as exc:  # noqa: BLE001
        log.warning("[ABSTENTION] list_modules failed: %s", exc)
        return []


def abstention_rate(
    records: list[dict],
    module_name: Optional[str] = None,
) -> float:
    """
    Compute abstention rate from a list of output records that may contain
    abstention fields. Filters by module_name if provided.
    Returns 0.0 on empty input. Non-fatal.
    """
    try:
        if not records:
            return 0.0
        filtered = records
        if module_name is not None:
            filtered = [
                r for r in records
                if (r.get("abstention", {}) or {}).get("module_name") == module_name
            ]
        if not filtered:
            return 0.0
        abstained = sum(
            1 for r in filtered
            if (r.get("abstention") or {}).get("abstain", False)
        )
        return round(abstained / len(filtered), 4)
    except Exception as exc:  # noqa: BLE001
        log.warning("[ABSTENTION] abstention_rate failed: %s", exc)
        return 0.0
