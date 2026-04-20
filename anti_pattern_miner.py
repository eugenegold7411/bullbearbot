"""
anti_pattern_miner.py — Memory anti-pattern miner (T2.5).

Mines forensic records to surface repeated failure patterns.
Enforces minimum-sample threshold before surfacing any pattern.
No LLM calls. Feature flag: enable_thesis_checksum.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import feature_flags

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AntiPattern:
    schema_version: int = 1
    pattern_id: str = ""
    pattern_tags: list = field(default_factory=list)
    description: str = ""
    occurrence_count: int = 0
    loss_rate: float = 0.0
    avg_pnl: float = 0.0
    first_seen: str = ""
    last_seen: str = ""
    sample_decision_ids: list = field(default_factory=list)
    confidence: float = 0.0
    abstention: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "pattern_id": self.pattern_id,
            "pattern_tags": self.pattern_tags,
            "description": self.description,
            "occurrence_count": self.occurrence_count,
            "loss_rate": self.loss_rate,
            "avg_pnl": self.avg_pnl,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "sample_decision_ids": self.sample_decision_ids,
            "confidence": self.confidence,
            "abstention": self.abstention,
        }


def _pattern_id(tags: list) -> str:
    """Stable hash of sorted tag list."""
    key = ",".join(sorted(str(t) for t in tags))
    return hashlib.md5(key.encode()).hexdigest()[:12]  # noqa: S324


def _confidence(n: int) -> float:
    """Confidence from sample size: 0.3@3, 0.6@10, 0.9@30+."""
    if n >= 30:
        return 0.9
    if n >= 10:
        return 0.6
    if n >= 3:
        return 0.3
    return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def mine_anti_patterns(
    days_back: int = 90,
    min_occurrences: int = 3,
    top_n: int = 5,
) -> list[AntiPattern]:
    """
    Reads forensic_log.jsonl, groups by pattern_tags for failed/poor trades.
    Enforces min_occurrences threshold with abstention below threshold.
    Returns top_n patterns. Returns [] on error or insufficient data.
    """
    if not feature_flags.is_enabled("enable_thesis_checksum"):
        return []
    try:
        forensic_log = Path("data/analytics/forensic_log.jsonl")
        if not forensic_log.exists():
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        # Groups: tags_key → list of matching records
        groups: dict[str, list[dict]] = {}

        for line in forensic_log.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                # Only failed/poor outcomes
                tv = d.get("thesis_verdict", "")
                ev = d.get("execution_verdict", "")
                if tv not in ("incorrect",) and ev not in ("poor",):
                    continue
                ts_str = d.get("created_at", "")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if ts < cutoff:
                            continue
                    except Exception:
                        pass
                tags = d.get("pattern_tags", [])
                if not tags:
                    tags = ["untagged"]
                key = ",".join(sorted(str(t) for t in tags))
                if key not in groups:
                    groups[key] = []
                groups[key].append(d)
            except Exception:
                continue

        patterns = []
        for key, records in groups.items():
            tags_list = key.split(",")
            n = len(records)
            pnls = [r.get("realized_pnl") for r in records if r.get("realized_pnl") is not None]
            avg_pnl = sum(pnls) / len(pnls) if pnls else 0.0
            loss_count = sum(1 for p in pnls if p < 0)
            loss_rate = loss_count / len(pnls) if pnls else 0.0

            timestamps = []
            for r in records:
                ts_s = r.get("created_at", "")
                if ts_s:
                    try:
                        timestamps.append(datetime.fromisoformat(ts_s.replace("Z", "+00:00")))
                    except Exception:
                        pass

            first_seen = min(timestamps).isoformat().replace("+00:00", "Z") if timestamps else ""
            last_seen = max(timestamps).isoformat().replace("+00:00", "Z") if timestamps else ""
            sample_ids = [r.get("decision_id", "") for r in records[:3]]

            if n < min_occurrences:
                try:
                    from abstention import abstain as _abstain  # noqa: PLC0415
                    _ab = _abstain(
                        reason=f"insufficient sample: {n} occurrences < {min_occurrences} required",
                        module_name="anti_pattern_miner",
                        evidence_present=True,
                    )
                except Exception:
                    pass
                # Sub-threshold patterns not surfaced as findings
                continue

            p = AntiPattern(
                pattern_id=_pattern_id(tags_list),
                pattern_tags=tags_list,
                description=f"Pattern: {' + '.join(tags_list[:3])} — {n} failure(s), loss_rate={loss_rate:.0%}",
                occurrence_count=n,
                loss_rate=loss_rate,
                avg_pnl=avg_pnl,
                first_seen=first_seen,
                last_seen=last_seen,
                sample_decision_ids=sample_ids,
                confidence=_confidence(n),
                abstention=None,
            )
            patterns.append(p)

        # Sort by occurrence_count desc, return top_n
        patterns.sort(key=lambda x: -x.occurrence_count)
        return patterns[:top_n]

    except Exception as exc:  # noqa: BLE001
        log.warning("[ANTI_PATTERN] mine_anti_patterns failed: %s", exc)
        return []


def format_anti_patterns_for_review(days_back: int = 90) -> str:
    """
    Returns markdown summary for weekly review injection (Agent 1).
    Returns '' on error or no data.
    """
    if not feature_flags.is_enabled("enable_thesis_checksum"):
        return ""
    try:
        patterns = mine_anti_patterns(days_back=days_back)
        if not patterns:
            return ""
        lines = [f"### Anti-Pattern Analysis (last {days_back}d)", ""]
        for p in patterns[:5]:
            lines.append(
                f"- **{p.description}** — {p.occurrence_count}x, "
                f"loss_rate={p.loss_rate:.0%}, avg_pnl={p.avg_pnl:+.2f}"
            )
        return "\n".join(lines)
    except Exception:
        return ""
