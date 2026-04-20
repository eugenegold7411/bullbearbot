# SHADOW MODULE — do not import from prod pipeline
"""
outcome_critic.py — Sparse-outcome critic prototype (T4.9).

Shadow evaluator that scores recommendation quality and forensic records
from a single critical perspective. Participates in shadow governance only.

LLM: single Haiku call per score, prompt under 600 tokens.
Cost attribution: module_name="outcome_critic", layer_name="shadow_analysis",
  ring="shadow", purpose="critic_score".
Feature flag: enable_outcome_critic (shadow_flags, default False).
Promotion contract: promotion_contracts/outcome_critic_v1.md (DRAFT).
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import model_tiering

log = logging.getLogger(__name__)

_CRITIC_LOG = Path("data/analytics/critic_scores.jsonl")
_FORENSIC_LOG = Path("data/analytics/forensic_log.jsonl")

_CRITIC_SYSTEM = (
    "You are a rigorous evaluator of trading decisions. "
    "Score the provided record on the dimensions given. Be critical. "
    "Abstain if you cannot fairly evaluate. "
    "Respond ONLY in JSON."
)


# ─────────────────────────────────────────────────────────────────────────────
# Dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CriticScore:
    schema_version: int = 1
    score_id: str = ""
    subject_id: str = ""
    subject_type: str = ""
    scored_at: str = ""
    overall_score: float = 0.5
    reasoning_quality: float = 0.5
    evidence_quality: float = 0.5
    prediction_specificity: float = 0.5
    abstention_appropriateness: float = 0.5
    critic_notes: str = ""
    confidence: float = 0.5
    abstention: Optional[dict] = None
    model_used: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_enabled() -> bool:
    try:
        from feature_flags import is_enabled  # noqa: PLC0415
        return is_enabled("enable_outcome_critic")
    except Exception:
        return False


def _call_critic(subject_id: str, subject_type: str, record_summary: str) -> Optional[CriticScore]:
    """Make one Haiku call to score the provided record. Returns CriticScore or None."""
    try:
        model = model_tiering.get_model_for_module("outcome_critic")
        import anthropic  # noqa: PLC0415
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))

        user_msg = (
            f"Subject ID: {subject_id}\n"
            f"Subject type: {subject_type}\n\n"
            f"Record:\n{record_summary[:1500]}\n\n"
            "Score this record on these dimensions (0.0-1.0 each):\n"
            "- reasoning_quality: was the reasoning sound?\n"
            "- evidence_quality: was evidence specific and verifiable?\n"
            "- prediction_specificity: was the prediction concrete enough to evaluate?\n"
            "- abstention_appropriateness: if it abstained, was that right? (0.5 if N/A)\n"
            "- overall_score: weighted average\n"
            "- confidence: your confidence in this scoring\n"
            "- critic_notes: one paragraph max\n\n"
            'If you cannot evaluate fairly, set abstention to {"reason": "your reason"}.\n'
            "Output JSON only."
        )

        response = client.messages.create(
            model=model,
            max_tokens=400,
            system=_CRITIC_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )

        raw = response.content[0].text if response.content else "{}"
        input_tok = response.usage.input_tokens
        output_tok = response.usage.output_tokens
        cost = (input_tok * 1.0 + output_tok * 5.0) / 1_000_000

        try:
            import cost_attribution as _ca  # noqa: PLC0415
            _ca.log_spine_record(
                module_name="outcome_critic",
                layer_name="shadow_analysis",
                ring="shadow",
                model=model,
                purpose="critic_score",
                linked_subject_id=subject_id,
                linked_subject_type=subject_type,
                input_tokens=input_tok,
                output_tokens=output_tok,
                estimated_cost_usd=round(cost, 6),
            )
        except Exception:
            pass

        try:
            parsed = json.loads(raw)
        except Exception:
            import re
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            parsed = json.loads(m.group()) if m else {}

        return CriticScore(
            schema_version=1,
            score_id=str(uuid.uuid4()),
            subject_id=subject_id,
            subject_type=subject_type,
            scored_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            overall_score=float(parsed.get("overall_score", 0.5)),
            reasoning_quality=float(parsed.get("reasoning_quality", 0.5)),
            evidence_quality=float(parsed.get("evidence_quality", 0.5)),
            prediction_specificity=float(parsed.get("prediction_specificity", 0.5)),
            abstention_appropriateness=float(parsed.get("abstention_appropriateness", 0.5)),
            critic_notes=str(parsed.get("critic_notes", ""))[:500],
            confidence=float(parsed.get("confidence", 0.5)),
            abstention=parsed.get("abstention"),
            model_used=model,
        )
    except Exception as exc:
        log.warning("[CRITIC] _call_critic failed for %s: %s", subject_id, exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def score_recommendation(rec_id: str) -> Optional[CriticScore]:
    """
    Load recommendation from recommendation_store; make one Haiku call to evaluate it.
    Returns None on any failure. Abstains if verdict="pending".
    """
    try:
        if not _is_enabled():
            return None

        from recommendation_store import get_recommendation  # noqa: PLC0415
        rec = get_recommendation(rec_id)
        if rec is None:
            log.debug("[CRITIC] recommendation %s not found", rec_id)
            return None

        rec_dict = rec.to_dict() if hasattr(rec, "to_dict") else (rec if isinstance(rec, dict) else {})
        verdict = rec_dict.get("verdict", "pending")
        if verdict == "pending":
            return CriticScore(
                schema_version=1,
                score_id=str(uuid.uuid4()),
                subject_id=rec_id,
                subject_type="recommendation",
                scored_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                abstention={"reason": "verdict=pending — cannot evaluate without outcome"},
                confidence=0.0,
            )

        summary = json.dumps(rec_dict, indent=2)[:1200]
        score = _call_critic(rec_id, "recommendation", summary)
        if score:
            _append_score(score)
        return score
    except Exception as exc:
        log.warning("[CRITIC] score_recommendation failed for %s: %s", rec_id, exc)
        return None


def score_forensic_record(forensic_id: str) -> Optional[CriticScore]:
    """
    Load forensic record from forensic_log.jsonl; make one Haiku call to evaluate it.
    Returns None on any failure.
    """
    try:
        if not _is_enabled():
            return None

        rec = None
        if _FORENSIC_LOG.exists():
            with open(_FORENSIC_LOG) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                        if r.get("forensic_id") == forensic_id:
                            rec = r
                    except Exception:
                        continue

        if rec is None:
            log.debug("[CRITIC] forensic record %s not found", forensic_id)
            return None

        summary = json.dumps(rec, indent=2)[:1200]
        score = _call_critic(forensic_id, "forensic_record", summary)
        if score:
            _append_score(score)
        return score
    except Exception as exc:
        log.warning("[CRITIC] score_forensic_record failed for %s: %s", forensic_id, exc)
        return None


def _append_score(score: CriticScore) -> None:
    try:
        _CRITIC_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_CRITIC_LOG, "a") as fh:
            fh.write(json.dumps(asdict(score)) + "\n")
    except Exception as exc:
        log.warning("[CRITIC] _append_score failed: %s", exc)


def get_critic_scores(
    subject_type: Optional[str] = None,
    days_back: int = 30,
) -> list:
    """Reads JSONL, filters, returns list of CriticScore dicts. Returns [] on error."""
    results = []
    try:
        if not _CRITIC_LOG.exists():
            return results
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        with open(_CRITIC_LOG) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts_str = rec.get("scored_at", "")
                    if ts_str:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if ts < cutoff:
                            continue
                    if subject_type and rec.get("subject_type") != subject_type:
                        continue
                    results.append(rec)
                except Exception:
                    continue
    except Exception as exc:
        log.warning("[CRITIC] get_critic_scores failed: %s", exc)
    return results


def format_critic_summary_for_review(days_back: int = 30) -> str:
    """
    Returns markdown summary of recent critic scores.
    Shows: avg overall_score, avg reasoning_quality, avg evidence_quality.
    Returns "" on error or no scores.
    """
    try:
        scores = get_critic_scores(days_back=days_back)
        if not scores:
            return ""

        non_abstained = [s for s in scores if not s.get("abstention")]
        if not non_abstained:
            return f"## Critic Scores ({days_back}d)\nAll {len(scores)} scored records resulted in abstention."

        avg_overall = sum(s.get("overall_score", 0.5) for s in non_abstained) / len(non_abstained)
        avg_reasoning = sum(s.get("reasoning_quality", 0.5) for s in non_abstained) / len(non_abstained)
        avg_evidence = sum(s.get("evidence_quality", 0.5) for s in non_abstained) / len(non_abstained)

        lines = [
            f"## Critic Scores ({days_back}d)\n",
            f"Total scored: {len(scores)} ({len(scores) - len(non_abstained)} abstained)",
            f"Avg overall: {avg_overall:.2f}",
            f"Avg reasoning quality: {avg_reasoning:.2f}",
            f"Avg evidence quality: {avg_evidence:.2f}",
        ]

        by_type: dict = {}
        for s in non_abstained:
            t = s.get("subject_type", "unknown")
            by_type.setdefault(t, []).append(s.get("overall_score", 0.5))

        lines.append("")
        lines.append("**By subject type:**")
        for t, vals in sorted(by_type.items()):
            lines.append(f"  - {t}: {sum(vals)/len(vals):.2f} avg ({len(vals)} records)")

        return "\n".join(lines)
    except Exception as exc:
        log.warning("[CRITIC] format_critic_summary_for_review failed: %s", exc)
        return ""
