# ANNEX MODULE — lab ring, no prod pipeline imports
"""
annex/theory_of_failure.py — Theory of failure generator (T6.5).

Evaluation class: quality_positive_non_alpha

Generates 2-4 competing failure theories for a given subject (forensic record,
recommendation, or incident). Single Haiku call per subject. Abstains if no
outcome data is available.

Storage: data/annex/theory_of_failure/ — annex namespace only.
Feature flag: enable_theory_of_failure (lab_flags, default False).
Promotion contract: promotion_contracts/theory_of_failure_v1.md (DRAFT).

Annex sandbox contract:
- No imports from bot.py, scheduler.py, order_executor.py, risk_kernel.py
- No writes to decision objects, strategy_config, execution paths, or readiness artifacts
- Outputs include confidence and/or abstention
- Kill-switchable via feature flag
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import model_tiering

log = logging.getLogger(__name__)

_ANNEX_DIR = Path("data/annex/theory_of_failure")
_THEORIES_LOG = _ANNEX_DIR / "theories.jsonl"
_FORENSIC_PATH = Path("data/analytics/forensic_log.jsonl")
_OUTCOMES_PATH = Path("data/analytics/decision_outcomes.jsonl")

_THEORY_SYSTEM = (
    "You are a rigorous post-mortem analyst for a systematic trading bot. "
    "Generate competing failure theories for why a trade or decision went wrong. "
    "Be specific, evidence-based, and include a testable prediction for each theory. "
    "Abstain if you cannot generate meaningful theories from the provided data. "
    "Respond ONLY in JSON."
)


# ─────────────────────────────────────────────────────────────────────────────
# Enums and dataclasses
# ─────────────────────────────────────────────────────────────────────────────

class TheoryType(str, Enum):
    BAD_ENTRY_TIMING        = "bad_entry_timing"
    WRONG_THESIS_TYPE       = "wrong_thesis_type"
    CATALYST_STALE          = "catalyst_stale"
    REGIME_MISMATCH         = "regime_mismatch"
    HIDDEN_DEPENDENCY       = "hidden_dependency"
    STOP_TOO_TIGHT          = "stop_too_tight"
    STOP_TOO_LOOSE          = "stop_too_loose"
    MANAGEMENT_DRIFT        = "management_drift"
    OVERCONFIDENCE          = "overconfidence"
    SIGNAL_CONFLICT_IGNORED = "signal_conflict_ignored"
    UNKNOWN                 = "unknown"


@dataclass
class TheoryEntry:
    theory_id: str = ""
    theory_type: str = ""
    description: str = ""
    supporting_evidence: str = ""
    confidence: float = 0.0
    testable_prediction: str = ""


@dataclass
class FailureTheory:
    schema_version: int = 1
    failure_theory_id: str = ""
    subject_id: str = ""
    subject_type: str = ""
    generated_at: str = ""
    theories: list = field(default_factory=list)
    dominant_theory: str = ""
    confidence: float = 0.0
    testable_prediction: str = ""
    abstention: Optional[dict] = None
    evaluation_class: str = "quality_positive_non_alpha"
    model_used: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_enabled() -> bool:
    try:
        from feature_flags import is_enabled  # noqa: PLC0415
        return is_enabled("enable_theory_of_failure")
    except Exception:
        return False


def _load_forensic_record(subject_id: str) -> Optional[dict]:
    try:
        if not _FORENSIC_PATH.exists():
            return None
        with open(_FORENSIC_PATH) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("forensic_id") == subject_id:
                        return rec
                except Exception:
                    continue
        return None
    except Exception as exc:
        log.debug("[TOF] _load_forensic_record failed: %s", exc)
        return None


def _load_outcome_record(subject_id: str) -> Optional[dict]:
    try:
        if not _OUTCOMES_PATH.exists():
            return None
        with open(_OUTCOMES_PATH) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("decision_id") == subject_id or rec.get("outcome_id") == subject_id:
                        return rec
                except Exception:
                    continue
        return None
    except Exception as exc:
        log.debug("[TOF] _load_outcome_record failed: %s", exc)
        return None


def _has_outcome_data(subject_id: str, subject_type: str, record: Optional[dict]) -> bool:
    """Check if subject has outcome data sufficient for theory generation."""
    if record is None:
        return False
    if subject_type == "forensic_record":
        verdict = record.get("thesis_verdict", "")
        return bool(verdict and verdict != "pending")
    if subject_type == "recommendation":
        verdict = record.get("verdict", "")
        return bool(verdict and verdict != "pending")
    # For other types, presence of the record is sufficient
    return True


def _call_theory_llm(subject_id: str, subject_type: str, record_summary: str) -> Optional[FailureTheory]:
    try:
        model = model_tiering.get_model_for_module("theory_of_failure")
        import anthropic  # noqa: PLC0415
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))

        valid_types = [t.value for t in TheoryType]
        user_msg = (
            f"Subject ID: {subject_id}\n"
            f"Subject type: {subject_type}\n\n"
            f"Record:\n{record_summary[:1400]}\n\n"
            "Generate 2-4 competing failure theories explaining what went wrong.\n"
            f"Valid theory_type values: {valid_types}\n\n"
            "Return JSON with this structure:\n"
            "{\n"
            '  "theories": [\n'
            "    {\n"
            '      "theory_type": "<one of the valid types>",\n'
            '      "description": "<what went wrong and why>",\n'
            '      "supporting_evidence": "<specific evidence from the record>",\n'
            '      "confidence": 0.0-1.0,\n'
            '      "testable_prediction": "<how to verify this theory>"\n'
            "    }\n"
            "  ],\n"
            '  "dominant_theory": "<theory_type of the most likely theory>",\n'
            '  "confidence": 0.0-1.0,\n'
            '  "testable_prediction": "<how to test the dominant theory>"\n'
            "}\n"
            'If you cannot generate meaningful theories, return {"abstention": {"reason": "..."}}.'
        )

        response = client.messages.create(
            model=model,
            max_tokens=600,
            system=_THEORY_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )

        raw = response.content[0].text if response.content else "{}"
        input_tok = response.usage.input_tokens
        output_tok = response.usage.output_tokens
        cost = (input_tok * 1.0 + output_tok * 5.0) / 1_000_000

        try:
            import cost_attribution as _ca  # noqa: PLC0415
            _ca.log_spine_record(
                module_name="theory_of_failure",
                layer_name="annex_experiment",
                ring="lab",
                model=model,
                purpose="failure_theory",
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

        if parsed.get("abstention"):
            return FailureTheory(
                schema_version=1,
                failure_theory_id=str(uuid.uuid4()),
                subject_id=subject_id,
                subject_type=subject_type,
                generated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                abstention=parsed["abstention"],
                confidence=0.0,
                model_used=model,
            )

        theories = []
        for i, t in enumerate(parsed.get("theories", [])[:4]):
            if not isinstance(t, dict):
                continue
            raw_type = str(t.get("theory_type", "unknown")).lower()
            valid_vals = {e.value for e in TheoryType}
            theory_type = raw_type if raw_type in valid_vals else TheoryType.UNKNOWN.value
            theories.append(TheoryEntry(
                theory_id=str(uuid.uuid4()),
                theory_type=theory_type,
                description=str(t.get("description", ""))[:400],
                supporting_evidence=str(t.get("supporting_evidence", ""))[:300],
                confidence=float(t.get("confidence", 0.5)),
                testable_prediction=str(t.get("testable_prediction", ""))[:300],
            ))

        return FailureTheory(
            schema_version=1,
            failure_theory_id=str(uuid.uuid4()),
            subject_id=subject_id,
            subject_type=subject_type,
            generated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            theories=[asdict(t) for t in theories],
            dominant_theory=str(parsed.get("dominant_theory", ""))[:50],
            confidence=float(parsed.get("confidence", 0.5)),
            testable_prediction=str(parsed.get("testable_prediction", ""))[:300],
            model_used=model,
        )
    except Exception as exc:
        log.warning("[TOF] _call_theory_llm failed for %s: %s", subject_id, exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def generate_failure_theories(
    subject_id: str,
    subject_type: str,
) -> Optional[FailureTheory]:
    """
    Generate 2-4 competing failure theories for the given subject.
    Makes one Haiku call. Abstains if no outcome data available.
    Returns FailureTheory or None on error.
    """
    try:
        if not _is_enabled():
            return None

        record = None
        if subject_type == "forensic_record":
            record = _load_forensic_record(subject_id)
        elif subject_type == "recommendation":
            try:
                from recommendation_store import get_recommendation  # noqa: PLC0415
                rec = get_recommendation(subject_id)
                if rec is not None:
                    record = rec.to_dict() if hasattr(rec, "to_dict") else (rec if isinstance(rec, dict) else None)
            except Exception:
                pass
        else:
            record = _load_outcome_record(subject_id)

        if not _has_outcome_data(subject_id, subject_type, record):
            return FailureTheory(
                schema_version=1,
                failure_theory_id=str(uuid.uuid4()),
                subject_id=subject_id,
                subject_type=subject_type,
                generated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                abstention={"reason": "no outcome data available — cannot generate failure theories"},
                confidence=0.0,
            )

        summary = json.dumps(record, indent=2)[:1400] if record else f"subject_id={subject_id}"
        return _call_theory_llm(subject_id, subject_type, summary)
    except Exception as exc:
        log.warning("[TOF] generate_failure_theories failed for %s: %s", subject_id, exc)
        return None


def log_theory(theory: FailureTheory) -> Optional[str]:
    """Appends to data/annex/theory_of_failure/theories.jsonl. Returns failure_theory_id or None."""
    try:
        _ANNEX_DIR.mkdir(parents=True, exist_ok=True)
        with open(_THEORIES_LOG, "a") as fh:
            fh.write(json.dumps(asdict(theory)) + "\n")
        return theory.failure_theory_id
    except Exception as exc:
        log.warning("[TOF] log_theory failed: %s", exc)
        return None


def get_theories(
    subject_type: Optional[str] = None,
    days_back: int = 30,
) -> list:
    """Reads JSONL, filters by subject_type and date. Returns list of dicts. [] on error."""
    results = []
    try:
        if not _THEORIES_LOG.exists():
            return results
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        with open(_THEORIES_LOG) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts_str = rec.get("generated_at", "")
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
        log.warning("[TOF] get_theories failed: %s", exc)
    return results


def format_theories_for_review(days_back: int = 30) -> str:
    """
    Returns markdown summary of recent failure theories.
    Groups by dominant_theory type, shows counts and testable predictions.
    Returns "" on error or no theories.
    """
    try:
        theories = get_theories(days_back=days_back)
        if not theories:
            return ""

        non_abstained = [t for t in theories if not t.get("abstention")]
        if not non_abstained:
            return f"## Failure Theories ({days_back}d)\nAll {len(theories)} subject(s) abstained."

        from collections import Counter
        dominant_counts: Counter = Counter()
        for t in non_abstained:
            dominant_counts[t.get("dominant_theory", "unknown")] += 1

        lines = [
            f"## Failure Theories ({days_back}d)\n",
            f"Total: {len(theories)} ({len(theories) - len(non_abstained)} abstained)\n",
            "**Dominant theory distribution:**",
        ]
        for theory_type, count in dominant_counts.most_common():
            lines.append(f"  - {theory_type}: {count}")

        recent = sorted(non_abstained, key=lambda x: x.get("generated_at", ""), reverse=True)[:3]
        if recent:
            lines.append("")
            lines.append("**Recent examples:**")
            for t in recent:
                lines.append(f"  - [{t.get('subject_type', '?')}] dominant={t.get('dominant_theory', '?')} "
                              f"confidence={t.get('confidence', 0):.2f}")
                pred = t.get("testable_prediction", "")
                if pred:
                    lines.append(f"    prediction: {pred[:120]}")

        return "\n".join(lines)
    except Exception as exc:
        log.warning("[TOF] format_theories_for_review failed: %s", exc)
        return ""
