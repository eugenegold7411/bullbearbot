# ANNEX MODULE — lab ring, no prod pipeline imports
"""
annex/ghost_advisors.py — Ghost advisor panel (T6.8).

Evaluation class: quality_positive_non_alpha

5 fixed ghost personas each giving one opinion on a case. One Haiku call per ghost.
Not wired into live cycles — called only from replay_debugger and weekly review
post-forensic section.

Storage: data/annex/ghost_advisors/ — annex namespace only.
Feature flag: enable_ghost_advisors (lab_flags, default False).
Promotion contract: promotion_contracts/ghost_advisors_v1.md (DRAFT).

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
from pathlib import Path
from typing import Optional

import model_tiering

log = logging.getLogger(__name__)

_ANNEX_DIR = Path("data/annex/ghost_advisors")
_OPINIONS_LOG = _ANNEX_DIR / "opinions.jsonl"

_GHOST_SYSTEM = (
    "You are a trading advisor with a specific investment philosophy. "
    "Stay rigidly in character — your philosophy is your only lens. "
    "Be honest about when a case is outside your domain. "
    "Respond ONLY in JSON."
)


# ─────────────────────────────────────────────────────────────────────────────
# Ghost personas
# ─────────────────────────────────────────────────────────────────────────────

GHOST_PERSONAS: dict = {
    "momentum_first": {
        "name": "momentum_first",
        "display_name": "The Momentum Trader",
        "philosophy": (
            "Trends persist until they don't. Ride what is working. "
            "Enter when price and volume confirm direction. "
            "Cut losses fast when momentum reverses. "
            "Never fight a strong trend. Catalysts don't matter — price action is the signal."
        ),
        "outside_domain_triggers": ["mean_reversion", "fundamental", "value", "macro_thesis"],
    },
    "macro_first": {
        "name": "macro_first",
        "display_name": "The Macro Strategist",
        "philosophy": (
            "Macro regime is everything. Rate cycles, dollar strength, credit spreads, and "
            "geopolitical flows determine which sectors and assets win. "
            "Individual stock selection matters far less than being in the right asset class "
            "at the right time in the cycle. Use equities to express macro views."
        ),
        "outside_domain_triggers": ["earnings_play", "technical", "intraday", "scalp"],
    },
    "mean_reversion_bias": {
        "name": "mean_reversion_bias",
        "display_name": "The Mean Reversion Trader",
        "philosophy": (
            "Prices always revert to fair value. Overreactions create opportunity. "
            "Buy when fear is highest and fundamentals haven't changed. "
            "Fade extended moves. "
            "Never chase — wait for the rubber band to snap back."
        ),
        "outside_domain_triggers": ["momentum", "trend_following", "breakout"],
    },
    "risk_minimizer": {
        "name": "risk_minimizer",
        "display_name": "The Risk Minimizer",
        "philosophy": (
            "Capital preservation is the first job. A portfolio that doesn't blow up "
            "survives to compound. Size small on uncertainty. "
            "Only take asymmetric bets where downside is defined and limited. "
            "The best trade is often no trade. Question every entry."
        ),
        "outside_domain_triggers": [],  # evaluates everything through a risk lens
    },
    "event_driven_only": {
        "name": "event_driven_only",
        "display_name": "The Event Trader",
        "philosophy": (
            "Only trade around knowable events: earnings, FDA decisions, economic releases, "
            "M&A announcements, short squeezes. The edge is in the asymmetry before the event. "
            "Exit when the event resolves. "
            "No thesis, no position. If there's no catalyst, there's no trade."
        ),
        "outside_domain_triggers": ["technical", "macro_backdrop", "trend", "momentum"],
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class GhostOpinion:
    schema_version: int = 1
    opinion_id: str = ""
    ghost_name: str = ""
    case_id: str = ""
    case_type: str = ""
    generated_at: str = ""
    agrees_with_prod: Optional[bool] = None
    would_action: str = ""
    would_sizing: str = ""
    key_concern: str = ""
    missed_risk_flag: bool = False
    missed_opportunity_flag: bool = False
    confidence: float = 0.0
    reasoning: str = ""
    abstention: Optional[dict] = None
    evaluation_class: str = "quality_positive_non_alpha"
    model_used: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_enabled() -> bool:
    try:
        from feature_flags import is_enabled  # noqa: PLC0415
        return is_enabled("enable_ghost_advisors")
    except Exception:
        return False


def _is_outside_domain(ghost_name: str, case_data: dict) -> bool:
    """Check if the case is clearly outside this ghost's domain."""
    persona = GHOST_PERSONAS.get(ghost_name, {})
    triggers = persona.get("outside_domain_triggers", [])
    if not triggers:
        return False
    case_text = json.dumps(case_data).lower()
    return any(t.lower() in case_text for t in triggers)


def _call_ghost_llm(ghost_name: str, case_id: str, case_type: str, case_data: dict) -> Optional[GhostOpinion]:
    try:
        persona = GHOST_PERSONAS[ghost_name]
        model = model_tiering.get_model_for_module("ghost_advisors")
        import anthropic  # noqa: PLC0415
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))

        system_msg = (
            f"{_GHOST_SYSTEM}\n\n"
            f"Your philosophy: {persona['philosophy']}"
        )

        case_summary = json.dumps(case_data, indent=2)[:1200]
        user_msg = (
            f"Case ID: {case_id}\n"
            f"Case type: {case_type}\n\n"
            f"Case data:\n{case_summary}\n\n"
            "Evaluate this case from your philosophy. Return JSON:\n"
            "{\n"
            '  "agrees_with_prod": true/false/null (null=abstain),\n'
            '  "would_action": "<what you would do: buy/sell/hold/pass/abstain>",\n'
            '  "would_sizing": "<full/half/quarter/none>",\n'
            '  "key_concern": "<one sentence — biggest concern from your philosophy>",\n'
            '  "missed_risk_flag": true/false,\n'
            '  "missed_opportunity_flag": true/false,\n'
            '  "confidence": 0.0-1.0,\n'
            '  "reasoning": "<1-2 sentences from your philosophy>"\n'
            "}\n"
            'If this case is outside your philosophy or you cannot evaluate it, '
            'return {"abstention": {"reason": "..."}} instead.'
        )

        response = client.messages.create(
            model=model,
            max_tokens=350,
            system=system_msg,
            messages=[{"role": "user", "content": user_msg}],
        )

        raw = response.content[0].text if response.content else "{}"
        input_tok = response.usage.input_tokens
        output_tok = response.usage.output_tokens
        cost = (input_tok * 1.0 + output_tok * 5.0) / 1_000_000

        try:
            import cost_attribution as _ca  # noqa: PLC0415
            _ca.log_spine_record(
                module_name="ghost_advisors",
                layer_name="annex_experiment",
                ring="lab",
                model=model,
                purpose="ghost_opinion",
                linked_subject_id=case_id,
                linked_subject_type=case_type,
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
            return GhostOpinion(
                schema_version=1,
                opinion_id=str(uuid.uuid4()),
                ghost_name=ghost_name,
                case_id=case_id,
                case_type=case_type,
                generated_at=now,
                abstention=parsed["abstention"],
                confidence=0.0,
                model_used=model,
            )

        agrees = parsed.get("agrees_with_prod")
        if agrees is not None:
            try:
                agrees = bool(agrees)
            except Exception:
                agrees = None

        return GhostOpinion(
            schema_version=1,
            opinion_id=str(uuid.uuid4()),
            ghost_name=ghost_name,
            case_id=case_id,
            case_type=case_type,
            generated_at=now,
            agrees_with_prod=agrees,
            would_action=str(parsed.get("would_action", ""))[:50],
            would_sizing=str(parsed.get("would_sizing", ""))[:30],
            key_concern=str(parsed.get("key_concern", ""))[:300],
            missed_risk_flag=bool(parsed.get("missed_risk_flag", False)),
            missed_opportunity_flag=bool(parsed.get("missed_opportunity_flag", False)),
            confidence=float(parsed.get("confidence", 0.5)),
            reasoning=str(parsed.get("reasoning", ""))[:400],
            model_used=model,
        )
    except Exception as exc:
        log.warning("[GHOST] _call_ghost_llm failed for %s/%s: %s", ghost_name, case_id, exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def get_ghost_names() -> list:
    """Returns list of all ghost persona names."""
    return list(GHOST_PERSONAS.keys())


def get_ghost_opinion(
    ghost_name: str,
    case_id: str,
    case_type: str,
    case_data: dict,
) -> Optional[GhostOpinion]:
    """
    Get one ghost's opinion on a case. Makes one Haiku call.
    Abstains if case is outside the ghost's philosophy or on error.
    Returns GhostOpinion or None on hard failure.
    """
    try:
        if not _is_enabled():
            return None

        if ghost_name not in GHOST_PERSONAS:
            log.warning("[GHOST] unknown ghost_name=%r", ghost_name)
            return None

        # Fast-path abstention for out-of-domain cases
        if _is_outside_domain(ghost_name, case_data):
            persona = GHOST_PERSONAS[ghost_name]
            return GhostOpinion(
                schema_version=1,
                opinion_id=str(uuid.uuid4()),
                ghost_name=ghost_name,
                case_id=case_id,
                case_type=case_type,
                generated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                abstention={"reason": f"case outside {persona['display_name']} philosophy domain"},
                confidence=0.0,
            )

        return _call_ghost_llm(ghost_name, case_id, case_type, case_data)
    except Exception as exc:
        log.warning("[GHOST] get_ghost_opinion failed for %s/%s: %s", ghost_name, case_id, exc)
        return None


def get_all_ghost_opinions(
    case_id: str,
    case_type: str,
    case_data: dict,
) -> list:
    """
    Get opinions from all 5 ghost personas for the given case.
    Returns list of GhostOpinion (may include abstentions). Non-fatal.
    """
    opinions = []
    if not _is_enabled():
        return opinions
    for ghost_name in GHOST_PERSONAS:
        try:
            opinion = get_ghost_opinion(ghost_name, case_id, case_type, case_data)
            if opinion is not None:
                opinions.append(opinion)
        except Exception as exc:
            log.warning("[GHOST] get_all_ghost_opinions failed for %s: %s", ghost_name, exc)
    return opinions


def log_opinion(opinion: GhostOpinion) -> Optional[str]:
    """Appends to data/annex/ghost_advisors/opinions.jsonl. Returns opinion_id or None."""
    try:
        _ANNEX_DIR.mkdir(parents=True, exist_ok=True)
        with open(_OPINIONS_LOG, "a") as fh:
            fh.write(json.dumps(asdict(opinion)) + "\n")
        return opinion.opinion_id
    except Exception as exc:
        log.warning("[GHOST] log_opinion failed: %s", exc)
        return None


def get_opinions(
    ghost_name: Optional[str] = None,
    case_id: Optional[str] = None,
    days_back: int = 30,
) -> list:
    """Reads JSONL, filters by ghost_name/case_id/date. Returns list of dicts. [] on error."""
    results = []
    try:
        if not _OPINIONS_LOG.exists():
            return results
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        with open(_OPINIONS_LOG) as fh:
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
                    if ghost_name and rec.get("ghost_name") != ghost_name:
                        continue
                    if case_id and rec.get("case_id") != case_id:
                        continue
                    results.append(rec)
                except Exception:
                    continue
    except Exception as exc:
        log.warning("[GHOST] get_opinions failed: %s", exc)
    return results


def format_ghost_consensus_for_review(case_id: str, case_type: str, case_data: dict) -> str:
    """
    Calls get_all_ghost_opinions(), returns markdown consensus table.
    Shows: agrees/disagrees/abstains, missed_risk flags, missed_opportunity flags.
    Returns "" on error or all abstentions.
    """
    try:
        if not _is_enabled():
            return ""

        opinions = get_all_ghost_opinions(case_id, case_type, case_data)
        if not opinions:
            return ""

        active = [o for o in opinions if not o.abstention]
        if not active:
            return f"## Ghost Panel — {case_id}\nAll ghosts abstained."

        agrees_count = sum(1 for o in active if o.agrees_with_prod is True)
        disagrees_count = sum(1 for o in active if o.agrees_with_prod is False)
        abstain_count = len(opinions) - len(active)
        risk_flags = sum(1 for o in active if o.missed_risk_flag)
        opp_flags = sum(1 for o in active if o.missed_opportunity_flag)

        lines = [
            f"## Ghost Advisor Panel — {case_id} ({case_type})\n",
            f"Agrees with prod: {agrees_count} | Disagrees: {disagrees_count} | Abstained: {abstain_count}",
            f"Missed risk flags: {risk_flags} | Missed opportunity flags: {opp_flags}",
            "",
            "| Ghost | Action | Sizing | Agrees | Risk | Opp | Confidence |",
            "|-------|--------|--------|--------|------|-----|------------|",
        ]

        persona_order = list(GHOST_PERSONAS.keys())
        opinion_map = {o.ghost_name: o for o in opinions}
        for ghost_name in persona_order:
            o = opinion_map.get(ghost_name)
            if o is None:
                continue
            if o.abstention:
                reason = o.abstention.get("reason", "")[:40]
                lines.append(f"| {ghost_name} | — | — | abstain ({reason}) | — | — | — |")
            else:
                agrees_str = "✅" if o.agrees_with_prod else ("❌" if o.agrees_with_prod is False else "—")
                risk_str = "⚠" if o.missed_risk_flag else "—"
                opp_str = "💡" if o.missed_opportunity_flag else "—"
                lines.append(
                    f"| {ghost_name} | {o.would_action} | {o.would_sizing} | "
                    f"{agrees_str} | {risk_str} | {opp_str} | {o.confidence:.2f} |"
                )

        if active:
            lines.append("")
            lines.append("**Key concerns:**")
            for o in active:
                if o.key_concern:
                    lines.append(f"  - **{o.ghost_name}:** {o.key_concern[:120]}")

        return "\n".join(lines)
    except Exception as exc:
        log.warning("[GHOST] format_ghost_consensus_for_review failed: %s", exc)
        return ""
