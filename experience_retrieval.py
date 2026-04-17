"""
experience_retrieval.py — Experience library retrieval hooks (T2.8).

Advisory retrieval over the experience library. Never injects into production
execution. All results include provenance and confidence.
Feature flag: enable_experience_library. No LLM calls.
"""

from __future__ import annotations

import logging
from typing import Optional

import feature_flags

log = logging.getLogger(__name__)

# Relevance scoring weights
_SCORE_SYMBOL = 3
_SCORE_THESIS = 2
_SCORE_CATALYST = 2
_SCORE_REGIME = 1

# Optional fields for confidence calculation
_OPTIONAL_FIELDS = [
    "forensic_id", "checksum_id", "catalyst_id", "thesis_type", "catalyst_type",
    "regime_at_entry", "close_reason", "realized_pnl", "hold_duration_hours",
    "what_worked", "what_failed", "repair_marker", "alpha_classification",
]


def _confidence_from_record(record: object) -> float:
    """Confidence = fraction of optional fields populated."""
    filled = sum(
        1 for f in _OPTIONAL_FIELDS
        if getattr(record, f, None) is not None and getattr(record, f, None) != ""
    )
    return round(filled / len(_OPTIONAL_FIELDS), 2)


def _score_record(
    record: object,
    symbol: Optional[str],
    thesis_type: Optional[str],
    catalyst_type: Optional[str],
    regime: Optional[str],
) -> int:
    score = 0
    if symbol and getattr(record, "symbol", None) == symbol:
        score += _SCORE_SYMBOL
    if thesis_type and getattr(record, "thesis_type", None) == thesis_type:
        score += _SCORE_THESIS
    if catalyst_type and getattr(record, "catalyst_type", None) == catalyst_type:
        score += _SCORE_CATALYST
    if regime and getattr(record, "regime_at_entry", None) == regime:
        score += _SCORE_REGIME
    return score


def _to_result(record: object, score: int) -> Optional[dict]:
    """Build retrieval result dict with provenance. Returns None if no provenance."""
    experience_id = getattr(record, "experience_id", None)
    decision_id = getattr(record, "decision_id", None)
    if not experience_id or not decision_id:
        return None  # provenance required
    return {
        "experience_id": experience_id,
        "record_type": getattr(record, "record_type", ""),
        "symbol": getattr(record, "symbol", ""),
        "summary": getattr(record, "summary", ""),
        "score": score,
        "confidence": _confidence_from_record(record),
        "provenance": {
            "experience_id": experience_id,
            "decision_id": decision_id,
            "forensic_id": getattr(record, "forensic_id", None),
        },
        "thesis_type": getattr(record, "thesis_type", None),
        "catalyst_type": getattr(record, "catalyst_type", None),
        "realized_pnl": getattr(record, "realized_pnl", None),
        "pattern_tags": getattr(record, "pattern_tags", []),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def retrieve_similar_experiences(
    symbol: Optional[str] = None,
    thesis_type: Optional[str] = None,
    catalyst_type: Optional[str] = None,
    regime: Optional[str] = None,
    top_n: int = 3,
    days_back: int = 365,
) -> list[dict]:
    """
    Retrieves most relevant experience records by relevance scoring.
    All results include provenance and confidence.
    Returns [] on error or empty library.
    """
    if not feature_flags.is_enabled("enable_experience_library"):
        return []
    try:
        from experience_library import get_experiences  # noqa: PLC0415
        records = get_experiences(days_back=days_back, limit=100)
        if not records:
            return []

        scored = []
        for r in records:
            score = _score_record(r, symbol, thesis_type, catalyst_type, regime)
            if score == 0:
                continue
            result = _to_result(r, score)
            if result is not None:
                scored.append(result)

        scored.sort(key=lambda x: (-x["score"], -x["confidence"]))
        return scored[:top_n]

    except Exception as exc:
        log.warning("[EXP_RETRIEVAL] retrieve_similar_experiences failed: %s", exc)
        return []


def retrieve_repaired_failures(
    thesis_type: Optional[str] = None,
    catalyst_type: Optional[str] = None,
    top_n: int = 3,
) -> list[dict]:
    """
    Retrieves repaired_failure_case records specifically.
    Used by forensic reviewer to reference prior similar repairs.
    Returns [] on error.
    """
    if not feature_flags.is_enabled("enable_experience_library"):
        return []
    try:
        from experience_library import get_experiences  # noqa: PLC0415
        records = get_experiences(
            record_type="repaired_failure_case",
            thesis_type=thesis_type,
            catalyst_type=catalyst_type,
            days_back=365,
            limit=top_n * 3,
        )
        results = []
        for r in records:
            result = _to_result(r, score=0)
            if result is not None:
                results.append(result)
        return results[:top_n]
    except Exception as exc:
        log.warning("[EXP_RETRIEVAL] retrieve_repaired_failures failed: %s", exc)
        return []


def format_retrieval_for_review(
    symbol: Optional[str] = None,
    thesis_type: Optional[str] = None,
) -> str:
    """
    Returns markdown summary of top 3 prior relevant cases with provenance.
    Returns '' on error or no relevant cases.
    """
    if not feature_flags.is_enabled("enable_experience_library"):
        return ""
    try:
        results = retrieve_similar_experiences(
            symbol=symbol,
            thesis_type=thesis_type,
            top_n=3,
        )
        if not results:
            return ""
        lines = ["### Prior Experience Cases", ""]
        for i, r in enumerate(results, 1):
            pnl_str = f"pnl={r['realized_pnl']:+.2f}" if r.get("realized_pnl") is not None else "pnl=?"
            lines.append(
                f"{i}. **{r['record_type']}** {r['symbol']} — {r['summary'][:80]} "
                f"({pnl_str}, confidence={r['confidence']:.2f}, "
                f"provenance={r['provenance']['decision_id'][:12]}...)"
            )
        return "\n".join(lines)
    except Exception:
        return ""
