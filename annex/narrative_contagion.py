# ANNEX MODULE — lab ring, no prod pipeline imports
"""
annex/narrative_contagion.py — Narrative Contagion Simulator skeleton (T6.10).

Evaluation class: exploratory — no alpha claim
Status: Data accumulation skeleton. No simulation yet.
Records potential narrative propagation events for future analysis.

Storage: data/annex/narrative_contagion/events.jsonl
Feature flag: enable_narrative_contagion (lab_flags, default False).
Promotion contract: promotion_contracts/narrative_contagion_v1.md (DRAFT).

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

_ANNEX_DIR = Path("data/annex/narrative_contagion")
_EVENTS_LOG = _ANNEX_DIR / "events.jsonl"


# ─────────────────────────────────────────────────────────────────────────────
# Dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ContagionEvent:
    schema_version: int = 1
    event_id: str = ""
    recorded_at: str = ""
    source_symbol: str = ""
    source_catalyst_type: str = ""
    propagation_candidates: list = field(default_factory=list)
    observed_propagation: list = field(default_factory=list)
    lag_hours: Optional[float] = None
    narrative_tag: str = ""
    notes: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_enabled() -> bool:
    try:
        from feature_flags import is_enabled  # noqa: PLC0415
        return is_enabled("enable_narrative_contagion")
    except Exception:
        return False


def _find_sector_peers(source_symbol: str, sector_map: dict) -> list[str]:
    """Return symbols in same sector as source_symbol."""
    source_sector = None
    for sym, sector in sector_map.items():
        if sym.upper() == source_symbol.upper():
            source_sector = sector
            break
    if source_sector is None:
        return []
    return [
        sym for sym, sector in sector_map.items()
        if sector == source_sector and sym.upper() != source_symbol.upper()
    ]


def _find_propagated(candidates: list, signal_scores: dict, threshold: float = 15.0) -> list[str]:
    """Return candidates that have signal scores above threshold in current cycle."""
    propagated = []
    for sym in candidates:
        score_entry = signal_scores.get(sym, signal_scores.get(sym.upper(), {}))
        if isinstance(score_entry, dict):
            score = float(score_entry.get("score", score_entry.get("final_score", 0)) or 0)
        else:
            score = float(score_entry or 0)
        if score >= threshold:
            propagated.append(sym)
    return propagated


def _make_narrative_tag(source_symbol: str, catalyst_type: str) -> str:
    return f"{source_symbol}:{catalyst_type}"


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def record_contagion_candidate(
    source_symbol: str,
    catalyst_type: str,
    signal_scores: dict,
    sector_map: dict,
) -> Optional[ContagionEvent]:
    """
    Records a potential contagion event for later analysis.
    Appends to data/annex/narrative_contagion/events.jsonl.
    Non-fatal.
    """
    try:
        if not _is_enabled():
            return None

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        candidates = _find_sector_peers(source_symbol, sector_map)
        propagated = _find_propagated(candidates, signal_scores)

        event = ContagionEvent(
            schema_version=1,
            event_id=str(uuid.uuid4()),
            recorded_at=now,
            source_symbol=source_symbol.upper(),
            source_catalyst_type=str(catalyst_type or "unknown"),
            propagation_candidates=candidates,
            observed_propagation=propagated,
            lag_hours=None,  # filled later when propagation timestamps are available
            narrative_tag=_make_narrative_tag(source_symbol, str(catalyst_type or "unknown")),
        )

        _ANNEX_DIR.mkdir(parents=True, exist_ok=True)
        with open(_EVENTS_LOG, "a") as fh:
            fh.write(json.dumps(asdict(event)) + "\n")
        return event
    except Exception as exc:
        log.warning("[CONTAGION] record_contagion_candidate failed: %s", exc)
        return None


def get_events(days_back: int = 30) -> list:
    """Reads JSONL. Returns [] on error."""
    results = []
    try:
        if not _EVENTS_LOG.exists():
            return results
        from datetime import timedelta  # noqa: PLC0415
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        with open(_EVENTS_LOG) as fh:
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
        log.warning("[CONTAGION] get_events failed: %s", exc)
    return results


def get_propagation_patterns(days_back: int = 90) -> dict:
    """
    Returns {narrative_tag: {avg_lag_hours, propagation_rate, count}}.
    Returns {} on error or insufficient data.
    """
    try:
        events = get_events(days_back=days_back)
        if not events:
            return {}

        patterns: dict = {}
        for event in events:
            tag = event.get("narrative_tag", "")
            if not tag:
                continue
            if tag not in patterns:
                patterns[tag] = {
                    "count": 0,
                    "propagation_events": 0,
                    "lag_hours_sum": 0.0,
                    "lag_count": 0,
                }
            p = patterns[tag]
            p["count"] += 1
            candidates = event.get("propagation_candidates", [])
            observed = event.get("observed_propagation", [])
            if candidates:
                p["propagation_events"] += len(observed)
            lag = event.get("lag_hours")
            if lag is not None:
                p["lag_hours_sum"] += float(lag)
                p["lag_count"] += 1

        result = {}
        for tag, p in patterns.items():
            total_candidates = p["count"] * 3  # rough denominator estimate
            result[tag] = {
                "avg_lag_hours": (p["lag_hours_sum"] / p["lag_count"]) if p["lag_count"] else None,
                "propagation_rate": p["propagation_events"] / max(total_candidates, 1),
                "count": p["count"],
            }
        return result
    except Exception as exc:
        log.warning("[CONTAGION] get_propagation_patterns failed: %s", exc)
        return {}


def format_contagion_for_review() -> str:
    """
    Brief summary with SKELETON notice.
    Returns "" on error.
    """
    try:
        events = get_events(days_back=30)
        if not events:
            return ""

        patterns = get_propagation_patterns(days_back=30)
        with_propagation = sum(1 for e in events if e.get("observed_propagation"))

        lines = [
            "## Narrative Contagion (30d) — SKELETON — insufficient data for simulation\n",
            f"Events recorded: {len(events)} | With observed propagation: {with_propagation}",
        ]

        if patterns:
            top = max(patterns.items(), key=lambda x: x[1]["count"])
            lines.append(f"Most active narrative: {top[0]} ({top[1]['count']} events)")

        return "\n".join(lines)
    except Exception as exc:
        log.warning("[CONTAGION] format_contagion_for_review failed: %s", exc)
        return ""
