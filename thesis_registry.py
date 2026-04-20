"""
thesis_registry.py — Canonical thesis record store for Thesis Lab (Build 1).

Ring 2 only — advisory shadow, never touches live execution.
Runs in the weekly review cadence, not the 5-minute cycle.

Importable with no env vars set: pure data module.
Zero imports from: bot.py, order_executor.py, risk_kernel.py
"""

from __future__ import annotations

import json
import logging
import random
import string
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────

_THESIS_LAB_DIR  = Path(__file__).parent / "data" / "thesis_lab"
_THESES_FILE     = _THESIS_LAB_DIR / "theses.json"
_QUARANTINE_FILE = _THESIS_LAB_DIR / "quarantine.jsonl"

# ─────────────────────────────────────────────────────────────────────────────
# Lifecycle states
# ─────────────────────────────────────────────────────────────────────────────

LIFECYCLE_STATES: list[str] = [
    "proposed",
    "researched",
    "active_tracking",
    "checkpoint_3m_complete",
    "checkpoint_6m_complete",
    "checkpoint_9m_complete",
    "checkpoint_12m_complete",
    "archived",
    "invalidated",
    "quarantine",
]

# Allowed forward transitions per state (permissive — human can override any via notes)
VALID_TRANSITIONS: dict[str, list[str]] = {
    "proposed":                ["researched", "invalidated", "quarantine"],
    "researched":              ["active_tracking", "invalidated", "quarantine"],
    "active_tracking":         ["checkpoint_3m_complete", "invalidated", "archived"],
    "checkpoint_3m_complete":  ["checkpoint_6m_complete", "invalidated", "archived"],
    "checkpoint_6m_complete":  ["checkpoint_9m_complete", "invalidated", "archived"],
    "checkpoint_9m_complete":  ["checkpoint_12m_complete", "invalidated", "archived"],
    "checkpoint_12m_complete": ["archived", "invalidated"],
    "archived":                [],
    "invalidated":             [],
    "quarantine":              ["researched", "proposed"],  # manual rescue path
}


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ThesisRecord:
    thesis_id: str
    source_type: str           # "manual" | "memo" | "batch_memo" | "imported"
    source_ref: str            # filename or description of source
    title: str
    date_opened: str           # ISO date YYYY-MM-DD
    status: str                # lifecycle state
    time_horizons: list[int]   # e.g. [3, 6, 9, 12] months
    narrative: str
    market_belief: str
    market_missing: str
    primary_bottleneck: str
    confirming_signals: list[str]
    countersignals: list[str]
    anchor_metrics: list[str]
    base_expression: dict      # {"instrument": "equity", "symbols": [...], "direction": "long"}
    alternate_expressions: list[dict]
    review_schedule: list[str] # ISO dates generated from time_horizons
    tags: list[str]
    archetype_candidates: list[str]
    notes: str
    schema_version: int = 1


@dataclass
class ThesisExpression:
    expression_id: str
    thesis_id: str
    label: str
    instrument_type: str   # "equity" | "option" | "etf" | "macro"
    symbols: list[str]
    weighting: str
    entry_rule: str
    exit_rule: str
    rotate_rule: str
    notes: str


# ─────────────────────────────────────────────────────────────────────────────
# ID generation
# ─────────────────────────────────────────────────────────────────────────────

def generate_thesis_id() -> str:
    ts     = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"thesis_{ts}_{suffix}"


# ─────────────────────────────────────────────────────────────────────────────
# Review schedule helper
# ─────────────────────────────────────────────────────────────────────────────

def build_review_schedule(date_opened: str, time_horizons: list[int]) -> list[str]:
    """Generate review dates from date_opened + time_horizons (months)."""
    try:
        base = datetime.strptime(date_opened, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        base = date.today()
    return [
        (base + timedelta(days=months * 30)).isoformat()
        for months in sorted(time_horizons)
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Persistence helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_dir() -> None:
    _THESIS_LAB_DIR.mkdir(parents=True, exist_ok=True)


def _load_raw() -> list[dict]:
    _ensure_dir()
    if not _THESES_FILE.exists():
        return []
    try:
        data = json.loads(_THESES_FILE.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_raw(records: list[dict]) -> None:
    _ensure_dir()
    tmp = _THESES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(records, indent=2))
    tmp.rename(_THESES_FILE)


def _dict_to_record(d: dict) -> ThesisRecord:
    return ThesisRecord(
        thesis_id=d.get("thesis_id", ""),
        source_type=d.get("source_type", ""),
        source_ref=d.get("source_ref", ""),
        title=d.get("title", ""),
        date_opened=d.get("date_opened", ""),
        status=d.get("status", "proposed"),
        time_horizons=d.get("time_horizons") or [],
        narrative=d.get("narrative", ""),
        market_belief=d.get("market_belief", ""),
        market_missing=d.get("market_missing", ""),
        primary_bottleneck=d.get("primary_bottleneck", ""),
        confirming_signals=d.get("confirming_signals") or [],
        countersignals=d.get("countersignals") or [],
        anchor_metrics=d.get("anchor_metrics") or [],
        base_expression=d.get("base_expression") or {},
        alternate_expressions=d.get("alternate_expressions") or [],
        review_schedule=d.get("review_schedule") or [],
        tags=d.get("tags") or [],
        archetype_candidates=d.get("archetype_candidates") or [],
        notes=d.get("notes", ""),
        schema_version=d.get("schema_version", 1),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Quarantine
# ─────────────────────────────────────────────────────────────────────────────

def write_quarantine(record_dict: dict, reason: str) -> None:
    """Append a failed/invalid record to quarantine.jsonl for human review."""
    _ensure_dir()
    entry = {
        "quarantined_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "record": record_dict,
    }
    with _QUARANTINE_FILE.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")
    log.info("[THESIS] Quarantined: %s", reason[:80])


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def create_thesis(record: ThesisRecord) -> str:
    """Persist a ThesisRecord. Returns thesis_id."""
    raw = _load_raw()
    raw.append(asdict(record))
    _save_raw(raw)
    log.info("[THESIS] Created %s: %.60s", record.thesis_id, record.title)
    return record.thesis_id


def get_thesis(thesis_id: str) -> Optional[ThesisRecord]:
    """Return ThesisRecord by ID, or None if not found."""
    for d in _load_raw():
        if d.get("thesis_id") == thesis_id:
            return _dict_to_record(d)
    return None


def list_theses(status: str = None) -> list[ThesisRecord]:
    """Return all ThesisRecords, optionally filtered by status."""
    records = [_dict_to_record(d) for d in _load_raw()]
    if status is not None:
        records = [r for r in records if r.status == status]
    return records


def update_thesis_status(thesis_id: str, new_status: str, notes: str = "") -> None:
    """
    Advance a thesis through the lifecycle.
    Logs WARNING for non-standard transitions but still applies the change
    (human override is intentional).
    Raises ValueError for unknown status values.
    Raises KeyError if thesis_id not found.
    """
    if new_status not in LIFECYCLE_STATES:
        raise ValueError(
            f"Unknown status: {new_status!r}. Valid states: {LIFECYCLE_STATES}"
        )

    raw = _load_raw()
    for d in raw:
        if d.get("thesis_id") != thesis_id:
            continue

        old_status = d.get("status", "")
        allowed    = VALID_TRANSITIONS.get(old_status, [])
        if new_status not in allowed:
            log.warning(
                "[THESIS] Non-standard transition %s → %s for %s",
                old_status, new_status, thesis_id,
            )

        d["status"] = new_status
        if notes:
            existing    = d.get("notes", "")
            ts          = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            d["notes"]  = f"{existing}\n[{ts}] {notes}".strip() if existing else f"[{ts}] {notes}"

        _save_raw(raw)
        log.info("[THESIS] %s: %s → %s", thesis_id, old_status, new_status)
        return

    raise KeyError(f"Thesis not found: {thesis_id!r}")


def load_all() -> list[ThesisRecord]:
    """Load all thesis records from storage."""
    return [_dict_to_record(d) for d in _load_raw()]
