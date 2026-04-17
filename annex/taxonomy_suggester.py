# ANNEX MODULE — lab ring, no prod pipeline imports
"""
annex/taxonomy_suggester.py — Taxonomy Suggestion Engine skeleton (T6.17).

Evaluation class: quality_positive_non_alpha
Status: Governance support skeleton. No auto-generation in v1.
Insufficient labeled data for meaningful suggestions currently.
is_auto_generated=False always in v1.

Reads catalyst_log.jsonl and thesis_checksums.jsonl for usage stats.
submit_suggestion() is for human-initiated submissions only.
Schema owner review required before any changes to taxonomy_v1.0.0.md.

Storage: data/annex/taxonomy_suggester/suggestions.jsonl
Feature flag: enable_taxonomy_suggester (lab_flags, default False).
Promotion contract: promotion_contracts/taxonomy_suggester_v1.md (DRAFT).

Annex sandbox contract:
- No imports from bot.py, scheduler.py, order_executor.py, risk_kernel.py
- No writes to decision objects, strategy_config, execution paths
- No writes to taxonomy_v1.0.0.md — suggestions only
- Kill-switchable via feature flag
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_ANNEX_DIR = Path("data/annex/taxonomy_suggester")
_SUGGESTIONS_LOG = _ANNEX_DIR / "suggestions.jsonl"

_CATALYST_LOG = Path("data/analytics/catalyst_log.jsonl")
_CHECKSUMS_LOG = Path("data/analytics/thesis_checksums.jsonl")


# ─────────────────────────────────────────────────────────────────────────────
# Dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TaxonomySuggestion:
    schema_version: int = 1
    suggestion_id: str = ""
    suggested_at: str = ""
    suggestion_type: str = ""       # "new_label" | "merge" | "split" | "deprecate"
    target_enum: str = ""
    proposed_value: str = ""
    evidence_count: int = 0
    evidence_decision_ids: list = field(default_factory=list)
    justification: str = ""
    status: str = "pending"         # "pending" | "accepted" | "rejected"
    schema_owner_review: str = ""
    is_auto_generated: bool = False  # always False in v1


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_enabled() -> bool:
    try:
        from feature_flags import is_enabled  # noqa: PLC0415
        return is_enabled("enable_taxonomy_suggester")
    except Exception:
        return False


def _load_jsonl_days(path: Path, days_back: int) -> list[dict]:
    try:
        if not path.exists():
            return []
        from datetime import timedelta  # noqa: PLC0415
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        results = []
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts = rec.get("normalized_at", rec.get("stamped_at",
                         rec.get("created_at", rec.get("recorded_at", ""))))
                    if ts:
                        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if t < cutoff:
                            continue
                    results.append(rec)
                except Exception:
                    continue
        return results
    except Exception as exc:
        log.debug("[TAXONOMY] _load_jsonl_days failed for %s: %s", path, exc)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def get_label_usage_stats(days_back: int = 90) -> dict:
    """
    Returns {enum_name: {label_value: count}} usage frequency.
    Reads thesis_checksums.jsonl and catalyst_log.jsonl.
    Returns {} on error.
    """
    try:
        stats: dict = {"catalyst_type": {}, "thesis_type": {}}

        for rec in _load_jsonl_days(_CATALYST_LOG, days_back):
            cat = rec.get("catalyst_type", "")
            if cat:
                stats["catalyst_type"][cat] = stats["catalyst_type"].get(cat, 0) + 1

        for rec in _load_jsonl_days(_CHECKSUMS_LOG, days_back):
            thesis = rec.get("thesis_type", "")
            if thesis:
                stats["thesis_type"][thesis] = stats["thesis_type"].get(thesis, 0) + 1

        return stats
    except Exception as exc:
        log.warning("[TAXONOMY] get_label_usage_stats failed: %s", exc)
        return {}


def get_unknown_catalyst_patterns(days_back: int = 90) -> list:
    """
    Reads catalyst_log.jsonl for records with catalyst_type="unknown".
    Extracts most common raw_text patterns.
    Returns [{pattern, count, example_raw_texts}].
    """
    try:
        records = _load_jsonl_days(_CATALYST_LOG, days_back)
        unknown_recs = [
            r for r in records
            if r.get("catalyst_type", "") in ("unknown", "", None)
        ]
        if not unknown_recs:
            return []

        raw_texts = [str(r.get("raw_text", "") or "") for r in unknown_recs if r.get("raw_text")]

        # Extract lowercase 2-3 word phrases as pattern candidates
        phrase_counter: Counter = Counter()
        for text in raw_texts:
            text_lower = text.lower()
            words = re.findall(r'\b[a-z]{3,}\b', text_lower)
            for i in range(len(words) - 1):
                phrase = f"{words[i]} {words[i+1]}"
                phrase_counter[phrase] += 1

        # Build result: top 10 patterns with examples
        results = []
        for phrase, count in phrase_counter.most_common(10):
            examples = [t[:80] for t in raw_texts if phrase in t.lower()][:3]
            results.append({
                "pattern": phrase,
                "count": count,
                "example_raw_texts": examples,
            })
        return results
    except Exception as exc:
        log.warning("[TAXONOMY] get_unknown_catalyst_patterns failed: %s", exc)
        return []


def submit_suggestion(suggestion: TaxonomySuggestion) -> Optional[str]:
    """
    Appends to data/annex/taxonomy_suggester/suggestions.jsonl.
    Returns suggestion_id or None.
    Note: is_auto_generated must be False in v1.
    """
    try:
        suggestion.is_auto_generated = False  # enforce v1 invariant
        if not suggestion.suggestion_id:
            suggestion.suggestion_id = str(uuid.uuid4())
        if not suggestion.suggested_at:
            suggestion.suggested_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        _ANNEX_DIR.mkdir(parents=True, exist_ok=True)
        with open(_SUGGESTIONS_LOG, "a") as fh:
            fh.write(json.dumps(asdict(suggestion)) + "\n")
        return suggestion.suggestion_id
    except Exception as exc:
        log.warning("[TAXONOMY] submit_suggestion failed: %s", exc)
        return None


def get_suggestions(status: Optional[str] = None) -> list:
    """Reads JSONL. Filters by status. Returns [] on error."""
    results = []
    try:
        if not _SUGGESTIONS_LOG.exists():
            return results
        with open(_SUGGESTIONS_LOG) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if status and rec.get("status") != status:
                        continue
                    results.append(rec)
                except Exception:
                    continue
    except Exception as exc:
        log.warning("[TAXONOMY] get_suggestions failed: %s", exc)
    return results


def format_usage_report_for_review() -> str:
    """
    Markdown report: label usage counts, unknown catalyst patterns.
    Always includes schema owner review notice.
    Returns "" on error or insufficient data.
    """
    try:
        stats = get_label_usage_stats(days_back=90)
        unknown_patterns = get_unknown_catalyst_patterns(days_back=90)

        cat_stats = stats.get("catalyst_type", {})
        thesis_stats = stats.get("thesis_type", {})

        if not cat_stats and not thesis_stats:
            return ""

        lines = [
            "## Taxonomy Usage Report (90d)\n",
            "> Human schema owner review required before any taxonomy changes.\n",
        ]

        if cat_stats:
            lines.append("**catalyst_type usage:**")
            for label, count in sorted(cat_stats.items(), key=lambda x: -x[1])[:10]:
                lines.append(f"  - {label}: {count}")

        if thesis_stats:
            lines.append("\n**thesis_type usage:**")
            for label, count in sorted(thesis_stats.items(), key=lambda x: -x[1])[:10]:
                lines.append(f"  - {label}: {count}")

        if unknown_patterns:
            lines.append(f"\n**Unknown catalyst raw_text patterns (top {len(unknown_patterns)}):**")
            for p in unknown_patterns[:5]:
                lines.append(
                    f"  - \"{p['pattern']}\" ({p['count']}x) — "
                    f"e.g. \"{p['example_raw_texts'][0] if p['example_raw_texts'] else ''}\""
                )

        pending = get_suggestions(status="pending")
        if pending:
            lines.append(f"\n**Pending suggestions:** {len(pending)}")

        return "\n".join(lines)
    except Exception as exc:
        log.warning("[TAXONOMY] format_usage_report_for_review failed: %s", exc)
        return ""
