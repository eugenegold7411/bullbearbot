# ANNEX MODULE — lab ring, no prod pipeline imports
"""
annex/thesis_ecology.py — Thesis Ecology Engine skeleton (T6.9).

Evaluation class: exploratory — no alpha claim
Status: Data accumulation skeleton. score_status="insufficient_sample" always.
No modeling yet. Records co-occurrence of thesis types in active portfolio.

Storage: data/annex/thesis_ecology/snapshots.jsonl
Feature flag: enable_thesis_ecology (lab_flags, default False).
Promotion contract: promotion_contracts/thesis_ecology_v1.md (DRAFT).

Annex sandbox contract:
- No imports from bot.py, scheduler.py, order_executor.py, risk_kernel.py
- No writes to decision objects, strategy_config, execution paths
- Kill-switchable via feature flag
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

_ANNEX_DIR = Path("data/annex/thesis_ecology")
_SNAPSHOTS_LOG = _ANNEX_DIR / "snapshots.jsonl"


# ─────────────────────────────────────────────────────────────────────────────
# Dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ThesisEcologyRecord:
    schema_version: int = 1
    record_id: str = ""
    recorded_at: str = ""
    thesis_types_active: list = field(default_factory=list)
    thesis_pair: Optional[list] = None     # [str, str] — if exactly 2 active
    same_symbol_different_thesis: bool = False
    competing_direction: bool = False
    outcome_decision_ids: list = field(default_factory=list)
    notes: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_enabled() -> bool:
    try:
        from feature_flags import is_enabled  # noqa: PLC0415
        return is_enabled("enable_thesis_ecology")
    except Exception:
        return False


def _extract_thesis_types(active_positions: list, thesis_checksums: list) -> list:
    """Extract distinct thesis_type values from positions or checksums."""
    types = set()
    # From positions
    for pos in active_positions:
        if isinstance(pos, dict):
            t = pos.get("thesis_type", pos.get("thesis", ""))
            if t and t not in ("", "unknown"):
                types.add(str(t))
    # From checksums — more authoritative
    checksum_by_symbol: dict = {}
    for cs in thesis_checksums:
        if isinstance(cs, dict):
            sym = cs.get("symbol", "")
            t = cs.get("thesis_type", "")
            v = cs.get("thesis_verdict", cs.get("verdict", "pending"))
            if sym and t and v != "closed":
                checksum_by_symbol[sym] = t
    types.update(checksum_by_symbol.values())
    return sorted(types)


def _detect_symbol_collisions(active_positions: list, thesis_checksums: list) -> tuple[bool, bool]:
    """
    Returns (same_symbol_different_thesis, competing_direction).
    """
    symbol_theses: dict = {}
    for pos in active_positions:
        if not isinstance(pos, dict):
            continue
        sym = pos.get("symbol", "")
        t = pos.get("thesis_type", pos.get("thesis", ""))
        direction = str(pos.get("direction", pos.get("side", "")) or "").lower()
        if sym:
            symbol_theses.setdefault(sym, []).append({"thesis": t, "direction": direction})

    # Also check checksums
    for cs in thesis_checksums:
        if not isinstance(cs, dict):
            continue
        sym = cs.get("symbol", "")
        t = cs.get("thesis_type", "")
        if sym and t and cs.get("thesis_verdict", "pending") != "closed":
            symbol_theses.setdefault(sym, []).append({"thesis": t, "direction": ""})

    same_sym_diff = False
    competing = False
    for sym, entries in symbol_theses.items():
        theses = [e["thesis"] for e in entries if e["thesis"]]
        if len(set(theses)) > 1:
            same_sym_diff = True
        directions = [e["direction"] for e in entries if e["direction"]]
        if "long" in directions and any(d in ("short", "bearish", "put") for d in directions):
            competing = True

    return same_sym_diff, competing


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def record_ecology_snapshot(
    active_positions: list,
    thesis_checksums: list,
) -> Optional[ThesisEcologyRecord]:
    """
    Builds snapshot from current portfolio state.
    Appends to data/annex/thesis_ecology/snapshots.jsonl.
    Non-fatal.
    """
    try:
        if not _is_enabled():
            return None

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        thesis_types = _extract_thesis_types(active_positions, thesis_checksums)
        same_sym, competing = _detect_symbol_collisions(active_positions, thesis_checksums)

        pair = None
        if len(thesis_types) == 2:
            pair = thesis_types[:2]

        rec = ThesisEcologyRecord(
            schema_version=1,
            record_id=str(uuid.uuid4()),
            recorded_at=now,
            thesis_types_active=thesis_types,
            thesis_pair=pair,
            same_symbol_different_thesis=same_sym,
            competing_direction=competing,
            outcome_decision_ids=[],
        )

        _ANNEX_DIR.mkdir(parents=True, exist_ok=True)
        with open(_SNAPSHOTS_LOG, "a") as fh:
            fh.write(json.dumps(asdict(rec)) + "\n")
        return rec
    except Exception as exc:
        log.warning("[ECOLOGY] record_ecology_snapshot failed: %s", exc)
        return None


def get_snapshots(days_back: int = 30) -> list:
    """Reads JSONL. Returns [] on error."""
    results = []
    try:
        if not _SNAPSHOTS_LOG.exists():
            return results
        from datetime import timedelta  # noqa: PLC0415
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        with open(_SNAPSHOTS_LOG) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts = rec.get("recorded_at", "")
                    if ts:
                        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if t < cutoff:
                            continue
                    results.append(rec)
                except Exception:
                    continue
    except Exception as exc:
        log.warning("[ECOLOGY] get_snapshots failed: %s", exc)
    return results


def get_cooccurrence_counts(days_back: int = 90) -> dict:
    """
    Returns {(thesis_a, thesis_b): count} for all observed pairs.
    Returns {} on error or insufficient data.
    """
    try:
        snapshots = get_snapshots(days_back=days_back)
        counts: dict = {}
        for snap in snapshots:
            types = snap.get("thesis_types_active", [])
            if len(types) < 2:
                continue
            sorted_types = sorted(types)
            for i in range(len(sorted_types)):
                for j in range(i + 1, len(sorted_types)):
                    pair = (sorted_types[i], sorted_types[j])
                    counts[str(pair)] = counts.get(str(pair), 0) + 1
        return counts
    except Exception as exc:
        log.warning("[ECOLOGY] get_cooccurrence_counts failed: %s", exc)
        return {}


def format_ecology_for_review() -> str:
    """
    Brief summary with SKELETON notice.
    Returns "" on error.
    """
    try:
        snapshots = get_snapshots(days_back=30)
        if not snapshots:
            return ""

        counts = get_cooccurrence_counts(days_back=30)
        competition_events = sum(1 for s in snapshots if s.get("competing_direction"))

        lines = [
            "## Thesis Ecology (30d) — SKELETON — insufficient data for modeling\n",
            f"Snapshots recorded: {len(snapshots)}",
            f"Competition events (opposing direction): {competition_events}",
        ]

        if counts:
            top_pair = max(counts.items(), key=lambda x: x[1])
            lines.append(f"Most common pair: {top_pair[0]} ({top_pair[1]} occurrences)")

        return "\n".join(lines)
    except Exception as exc:
        log.warning("[ECOLOGY] format_ecology_for_review failed: %s", exc)
        return ""
