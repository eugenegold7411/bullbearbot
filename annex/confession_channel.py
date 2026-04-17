# ANNEX MODULE — lab ring, no prod pipeline imports
"""
annex/confession_channel.py — Confession channel prototype (T6.15).

Evaluation class: quality_positive_non_alpha

Structured hypothesis artifact. Never open-ended prose. Can abstain entirely.
All confessions are hypotheses — never treated as verified facts.
is_hypothesis is always True.

Storage: data/annex/confession_channel/ — annex namespace only.
Feature flag: enable_confession_channel (lab_flags, default False).
Promotion contract: promotion_contracts/confession_channel_v1.md (DRAFT).

Annex sandbox contract:
- No imports from bot.py, scheduler.py, order_executor.py, risk_kernel.py
- No writes to decision objects, strategy_config, execution paths
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

_ANNEX_DIR = Path("data/annex/confession_channel")
_CONFESSIONS_LOG = _ANNEX_DIR / "confessions.jsonl"

_CONFESSION_SYSTEM = (
    "You are generating a structured confession artifact for a trading bot. "
    "This is a hypothesis, not a verified fact. "
    "Confess only what is supported by the evidence provided. "
    "Abstain if nothing genuine can be confessed. "
    "Never invent evidence not present in the case data. "
    "Output structured JSON only."
)

_EVIDENCE_STRENGTH_VALUES = {"weak", "moderate", "strong"}


# ─────────────────────────────────────────────────────────────────────────────
# Enums and dataclasses
# ─────────────────────────────────────────────────────────────────────────────

class ConfessionType(str, Enum):
    UNCERTAINTY        = "uncertainty"
    RULE_CONFLICT      = "rule_conflict"
    SUPPRESSED_INTENT  = "suppressed_intent"
    POSSIBLE_SHORTCUT  = "possible_shortcut"


@dataclass
class ConfessionRecord:
    schema_version: int = 1
    confession_id: str = ""
    confessed_at: str = ""
    module_name: str = ""
    case_id: str = ""
    confession_type: str = ""
    claim: str = ""
    evidence_strength: str = "weak"
    confidence: float = 0.0
    would_have_done: Optional[str] = None
    why_not_done: Optional[str] = None
    possible_shortcut: Optional[str] = None
    abstain_reason: Optional[str] = None
    abstention: Optional[dict] = None
    is_hypothesis: bool = True
    evaluation_class: str = "quality_positive_non_alpha"
    model_used: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_enabled() -> bool:
    try:
        from feature_flags import is_enabled  # noqa: PLC0415
        return is_enabled("enable_confession_channel")
    except Exception:
        return False


def _call_confession_llm(
    module_name: str,
    case_id: str,
    case_data: dict,
    confession_type: Optional[str],
) -> Optional[ConfessionRecord]:
    try:
        model = model_tiering.get_model_for_module("confession_channel")
        import anthropic  # noqa: PLC0415
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))

        valid_types = [t.value for t in ConfessionType]
        type_instruction = (
            f'confession_type must be one of: {valid_types}'
            if confession_type is None
            else f'confession_type: "{confession_type}"'
        )

        case_summary = json.dumps(case_data, indent=2)[:900]
        user_msg = (
            f"Module: {module_name}\n"
            f"Case ID: {case_id}\n\n"
            f"Case data:\n{case_summary}\n\n"
            "Generate a structured confession. "
            f"{type_instruction}\n\n"
            "Return JSON:\n"
            "{\n"
            '  "confession_type": "<type>",\n'
            '  "claim": "<one sentence — what is being confessed>",\n'
            '  "evidence_strength": "weak" | "moderate" | "strong",\n'
            '  "confidence": 0.0-1.0,\n'
            '  "would_have_done": "<optional — what it would have preferred to do>",\n'
            '  "why_not_done": "<optional — what prevented it>",\n'
            '  "possible_shortcut": "<optional — if a cognitive shortcut may have been taken>"\n'
            "}\n\n"
            "IMPORTANT: Only confess what is supported by the case data. "
            'If nothing genuine can be confessed, return {"abstention": {"reason": "..."}}.'
        )

        response = client.messages.create(
            model=model,
            max_tokens=350,
            system=_CONFESSION_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )

        raw = response.content[0].text if response.content else "{}"
        input_tok = response.usage.input_tokens
        output_tok = response.usage.output_tokens
        cost = (input_tok * 1.0 + output_tok * 5.0) / 1_000_000

        try:
            import cost_attribution as _ca  # noqa: PLC0415
            _ca.log_spine_record(
                module_name="confession_channel",
                layer_name="annex_experiment",
                ring="lab",
                model=model,
                purpose="confession",
                linked_subject_id=case_id,
                linked_subject_type="case",
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

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        if parsed.get("abstention"):
            return ConfessionRecord(
                schema_version=1,
                confession_id=str(uuid.uuid4()),
                confessed_at=now,
                module_name=module_name,
                case_id=case_id,
                abstention=parsed["abstention"],
                abstain_reason=parsed["abstention"].get("reason", ""),
                confidence=0.0,
                is_hypothesis=True,
                model_used=model,
            )

        raw_type = str(parsed.get("confession_type", "uncertainty")).lower()
        valid_vals = {t.value for t in ConfessionType}
        confession_type_out = raw_type if raw_type in valid_vals else ConfessionType.UNCERTAINTY.value

        evidence_str = str(parsed.get("evidence_strength", "weak")).lower()
        if evidence_str not in _EVIDENCE_STRENGTH_VALUES:
            evidence_str = "weak"

        return ConfessionRecord(
            schema_version=1,
            confession_id=str(uuid.uuid4()),
            confessed_at=now,
            module_name=module_name,
            case_id=case_id,
            confession_type=confession_type_out,
            claim=str(parsed.get("claim", ""))[:400],
            evidence_strength=evidence_str,
            confidence=float(parsed.get("confidence", 0.3)),
            would_have_done=str(parsed.get("would_have_done", "") or "")[:200] or None,
            why_not_done=str(parsed.get("why_not_done", "") or "")[:200] or None,
            possible_shortcut=str(parsed.get("possible_shortcut", "") or "")[:200] or None,
            is_hypothesis=True,
            model_used=model,
        )
    except Exception as exc:
        log.warning("[CONFESS] _call_confession_llm failed for %s/%s: %s", module_name, case_id, exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def generate_confession(
    module_name: str,
    case_id: str,
    case_data: dict,
    confession_type: Optional[str] = None,
) -> Optional[ConfessionRecord]:
    """
    Makes one Haiku call to generate a structured confession.
    If LLM cannot produce a genuine confession: returns abstaining record.
    Never generates a confession not supported by case_data evidence.
    Non-fatal. Returns None on hard failure.
    """
    try:
        if not _is_enabled():
            return None

        # Validate confession_type if provided
        if confession_type is not None:
            valid_vals = {t.value for t in ConfessionType}
            if confession_type not in valid_vals:
                log.warning("[CONFESS] invalid confession_type=%r, using None", confession_type)
                confession_type = None

        # Require non-empty case_data
        if not case_data:
            return ConfessionRecord(
                schema_version=1,
                confession_id=str(uuid.uuid4()),
                confessed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                module_name=module_name,
                case_id=case_id,
                abstention={"reason": "no case data provided — cannot generate confession"},
                abstain_reason="no case data provided",
                confidence=0.0,
                is_hypothesis=True,
            )

        return _call_confession_llm(module_name, case_id, case_data, confession_type)
    except Exception as exc:
        log.warning("[CONFESS] generate_confession failed for %s/%s: %s", module_name, case_id, exc)
        return None


def log_confession(record: ConfessionRecord) -> Optional[str]:
    """Appends to data/annex/confession_channel/confessions.jsonl. Returns confession_id or None."""
    try:
        _ANNEX_DIR.mkdir(parents=True, exist_ok=True)
        with open(_CONFESSIONS_LOG, "a") as fh:
            fh.write(json.dumps(asdict(record)) + "\n")
        return record.confession_id
    except Exception as exc:
        log.warning("[CONFESS] log_confession failed: %s", exc)
        return None


def get_confessions(
    module_name: Optional[str] = None,
    confession_type: Optional[str] = None,
    days_back: int = 30,
) -> list:
    """Reads JSONL. Filters by module_name, confession_type, date. Returns [] on error."""
    results = []
    try:
        if not _CONFESSIONS_LOG.exists():
            return results
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        with open(_CONFESSIONS_LOG) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts_str = rec.get("confessed_at", "")
                    if ts_str:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if ts < cutoff:
                            continue
                    if module_name and rec.get("module_name") != module_name:
                        continue
                    if confession_type and rec.get("confession_type") != confession_type:
                        continue
                    results.append(rec)
                except Exception:
                    continue
    except Exception as exc:
        log.warning("[CONFESS] get_confessions failed: %s", exc)
    return results


def format_confessions_for_review(days_back: int = 30) -> str:
    """
    Markdown summary grouped by confession_type.
    Includes hypothesis disclaimer.
    Returns "" on error or no confessions.
    """
    try:
        confessions = get_confessions(days_back=days_back)
        if not confessions:
            return ""

        non_abstained = [c for c in confessions if not c.get("abstention")]
        if not non_abstained:
            return f"## Confession Channel ({days_back}d)\nAll {len(confessions)} confession(s) abstained."

        by_type: dict = {}
        for c in non_abstained:
            t = c.get("confession_type", "unknown")
            by_type.setdefault(t, []).append(c)

        lines = [
            f"## Confession Channel ({days_back}d)\n",
            "> **HYPOTHESIS** — these are structured hypotheses, not verified facts.\n",
            f"Total: {len(confessions)} ({len(confessions) - len(non_abstained)} abstained)\n",
        ]
        for ctype, recs in sorted(by_type.items()):
            lines.append(f"**{ctype}** — {len(recs)} instance(s)")
            for rec in recs[:2]:
                strength = rec.get("evidence_strength", "weak")
                conf = rec.get("confidence", 0.0)
                claim = rec.get("claim", "")[:120]
                lines.append(f"  - [{strength}] {claim} (confidence={conf:.2f})")
            lines.append("")

        return "\n".join(lines)
    except Exception as exc:
        log.warning("[CONFESS] format_confessions_for_review failed: %s", exc)
        return ""
