"""
recommendation_resolver.py — Recommendation outcome resolver (T2.4).

Resolves pending recommendations in the recommendation store by comparing
against subsequent outcomes. Produces verdict updates and HindsightRecords.
Feature flag: enable_recommendation_memory.
No LLM calls.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import feature_flags

log = logging.getLogger(__name__)


def resolve_pending_recommendations(
    days_back: int = 90,
    min_age_days: int = 7,
) -> list[dict]:
    """
    Fetches all pending recommendations and attempts to resolve them.
    Returns list of resolution summaries. Returns [] on error.
    """
    if not feature_flags.is_enabled("enable_recommendation_memory"):
        return []
    try:
        import decision_outcomes as _do  # noqa: PLC0415
        from recommendation_store import (  # noqa: PLC0415
            get_recommendations,
        )

        pending = get_recommendations(verdict="pending", limit=200)
        if not pending:
            return []

        outcomes_summary = _do.generate_outcomes_summary(days_back=days_back)
        now = datetime.now(timezone.utc)
        min_age_cutoff = now - timedelta(days=min_age_days)

        summaries = []
        for rec in pending:
            try:
                result = _resolve_single(rec, outcomes_summary, min_age_cutoff, now)
                if result:
                    summaries.append(result)
            except Exception as _exc:
                log.debug("[RESOLVER] failed to resolve rec %s: %s", getattr(rec, "rec_id", "?"), _exc)

        return summaries

    except Exception as exc:  # noqa: BLE001
        log.warning("[RESOLVER] resolve_pending_recommendations failed: %s", exc)
        return []


def _resolve_single(rec: object, outcomes_summary: dict, min_age_cutoff: datetime, now: datetime) -> Optional[dict]:
    """
    Attempts to resolve a single recommendation. Returns summary dict or None.
    Abstains (leaves pending) if: too young, no expected_direction, or no outcome evidence.
    """
    from recommendation_store import update_verdict  # noqa: PLC0415

    rec_id = getattr(rec, "rec_id", None) or (rec.get("rec_id") if isinstance(rec, dict) else None)
    if not rec_id:
        return None

    # Check minimum age
    created_at_str = getattr(rec, "created_at", None) or (rec.get("created_at") if isinstance(rec, dict) else None)
    if created_at_str:
        try:
            created_at = datetime.fromisoformat(str(created_at_str).replace("Z", "+00:00"))
            if created_at > min_age_cutoff:
                return None  # too young — abstain, leave pending
        except Exception:
            pass

    expected_direction = getattr(rec, "expected_direction", None) or (rec.get("expected_direction") if isinstance(rec, dict) else None)
    if not expected_direction:
        return None  # no direction set — cannot resolve

    getattr(rec, "target_metric", None) or (rec.get("target_metric") if isinstance(rec, dict) else None)
    text = getattr(rec, "text", None) or (rec.get("text") if isinstance(rec, dict) else "")

    # Evidence: look at outcomes_summary for metric movement
    verdict = "neutral"
    resolution_evidence = "No matching outcome data found."

    submitted_count = outcomes_summary.get("submitted_count", 0)
    avg_return = outcomes_summary.get("avg_return_1d")
    outcomes_summary.get("win_rate_1d")

    if submitted_count > 0 and avg_return is not None:
        if expected_direction == "up" and float(avg_return) > 0.003:
            verdict = "verified"
            resolution_evidence = f"avg_return_1d={avg_return:.4f} supports upward thesis"
        elif expected_direction == "down" and float(avg_return) < -0.003:
            verdict = "verified"
            resolution_evidence = f"avg_return_1d={avg_return:.4f} supports downward thesis"
        elif expected_direction == "up" and float(avg_return) < -0.003:
            verdict = "falsified"
            resolution_evidence = f"avg_return_1d={avg_return:.4f} contradicts upward thesis"
        elif expected_direction == "down" and float(avg_return) > 0.003:
            verdict = "falsified"
            resolution_evidence = f"avg_return_1d={avg_return:.4f} contradicts downward thesis"
        else:
            verdict = "neutral"
            resolution_evidence = f"avg_return_1d={avg_return:.4f} inconclusive"
    else:
        return None  # insufficient evidence — leave pending

    # Write verdict
    update_verdict(
        rec_id=rec_id,
        verdict=verdict,
        resolution_evidence=resolution_evidence,
    )

    # Create HindsightRecord
    hindsight_id = None
    try:
        from hindsight import (  # noqa: PLC0415
            build_hindsight_record,
            log_hindsight_record,
        )
        hs = build_hindsight_record(
            subject_id=rec_id,
            subject_type="recommendation",
            verdict=verdict,
            evidence_summary=resolution_evidence,
            module_name="recommendation_resolver",
        )
        hindsight_id = log_hindsight_record(hs)
    except Exception as _hs_exc:
        log.debug("[RESOLVER] hindsight link failed: %s", _hs_exc)

    return {
        "rec_id": rec_id,
        "verdict": verdict,
        "resolution_evidence": resolution_evidence,
        "hindsight_id": hindsight_id,
        "text_preview": str(text)[:80] if text else "",
    }


def resolve_single_recommendation(rec_id: str) -> Optional[dict]:
    """Resolves one recommendation by rec_id. Returns summary dict or None."""
    if not feature_flags.is_enabled("enable_recommendation_memory"):
        return None
    try:
        from recommendation_store import get_recommendation  # noqa: PLC0415
        rec = get_recommendation(rec_id)
        if rec is None:
            return None
        import decision_outcomes as _do  # noqa: PLC0415
        outcomes_summary = _do.generate_outcomes_summary(days_back=90)
        now = datetime.now(timezone.utc)
        min_age_cutoff = now - timedelta(days=7)
        return _resolve_single(rec, outcomes_summary, min_age_cutoff, now)
    except Exception as exc:
        log.warning("[RESOLVER] resolve_single_recommendation failed: %s", exc)
        return None


def format_resolution_summary_for_review(days_back: int = 30) -> str:
    """
    Returns markdown summary of recent resolutions for weekly review.
    Returns '' on error or no data.
    """
    if not feature_flags.is_enabled("enable_recommendation_memory"):
        return ""
    try:
        from recommendation_store import get_recommendations  # noqa: PLC0415
        all_recs = get_recommendations(limit=200)
        if not all_recs:
            return ""

        from datetime import timedelta as _td  # noqa: PLC0415
        cutoff = datetime.now(timezone.utc) - _td(days=days_back)
        verdict_counts: dict[str, int] = {}
        verified: list = []
        falsified: list = []

        for rec in all_recs:
            resolved_at = getattr(rec, "resolved_at", None) or (rec.get("resolved_at") if isinstance(rec, dict) else None)
            verdict = getattr(rec, "verdict", None) or (rec.get("verdict") if isinstance(rec, dict) else "pending")
            if not resolved_at or verdict == "pending":
                continue
            try:
                ts = datetime.fromisoformat(str(resolved_at).replace("Z", "+00:00"))
                if ts < cutoff:
                    continue
            except Exception:
                continue
            verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
            text = str(getattr(rec, "text", "") or "")[:80]
            if verdict == "verified":
                verified.append(text)
            elif verdict == "falsified":
                falsified.append(text)

        if not verdict_counts:
            return ""

        total = sum(verdict_counts.values())
        lines = [f"### Recommendation Resolution Summary (last {days_back}d)", ""]
        lines.append(f"Total resolved: {total}")
        for v, c in sorted(verdict_counts.items(), key=lambda x: -x[1]):
            lines.append(f"- {v}: {c}")
        if verified:
            lines.append(f"\nTop verified: {verified[0]}")
        if falsified:
            lines.append(f"Top falsified: {falsified[0]}")
        return "\n".join(lines)

    except Exception as exc:
        log.warning("[RESOLVER] format_resolution_summary_for_review failed: %s", exc)
        return ""
