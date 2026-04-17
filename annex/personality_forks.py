# ANNEX MODULE — lab ring, no prod pipeline imports
"""
annex/personality_forks.py — Personality fork panel (T6.3).

Evaluation class: quality_positive_non_alpha

Five cognitive-style personas (distinct from ghost advisors — these represent
different reasoning styles, not trading philosophies). Rule-based pre-filter
avoids LLM calls when the case clearly violates a fork's constraints.

Storage: data/annex/personality_forks/ — annex namespace only.
Feature flag: enable_personality_forks (lab_flags, default False).
Promotion contract: promotion_contracts/personality_forks_v1.md (DRAFT).

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
from pathlib import Path
from typing import Optional

import model_tiering

log = logging.getLogger(__name__)

_ANNEX_DIR = Path("data/annex/personality_forks")
_OPINIONS_LOG = _ANNEX_DIR / "opinions.jsonl"

_FORK_SYSTEM = (
    "You are a trading analyst with a specific cognitive style. "
    "Evaluate the case through ONLY your cognitive lens — stay rigidly in character. "
    "Return structured JSON only. Abstain if you cannot apply your style to this case."
)


# ─────────────────────────────────────────────────────────────────────────────
# Fork persona definitions
# ─────────────────────────────────────────────────────────────────────────────

PERSONALITY_FORK_PERSONAS: dict = {
    "paranoid": {
        "name": "paranoid",
        "description": "Assumes adverse selection on every signal. Questions every catalyst. Defaults to cash unless evidence is overwhelming.",
        "cognitive_style": "skeptical, high-bar for entry, never chases",
        "signal_discount_factor": 0.6,
        "veto_threshold": 0.85,
        "preferred_regime": "risk_off or caution",
        "default_action": "hold",
    },
    "opportunist": {
        "name": "opportunist",
        "description": "Maximizes exposure when signals align. Acts quickly on catalysts. Tolerates higher drawdown for bigger gains.",
        "cognitive_style": "aggressive entry, high tolerance for volatility",
        "signal_boost_factor": 1.2,
        "veto_threshold": 0.45,
        "preferred_regime": "risk_on",
        "default_action": "enter if any signal > 0.5",
    },
    "minimalist": {
        "name": "minimalist",
        "description": "Holds maximum 2 positions at once. Prefers extreme clarity. Ignores any signal that isn't in the top decile.",
        "cognitive_style": "concentrated, patient, ignores noise",
        "max_positions": 2,
        "signal_floor": 0.75,
        "preferred_regime": "any",
        "default_action": "hold until top signal is clear winner",
    },
    "anti_crowding": {
        "name": "anti_crowding",
        "description": "Avoids whatever everyone else is doing. Fades consensus signals. Looks for contrarian setups.",
        "cognitive_style": "contrarian, fades momentum, seeks asymmetry",
        "crowding_veto": True,
        "consensus_discount": 0.5,
        "preferred_regime": "caution or risk_off",
        "default_action": "fade the consensus",
    },
    "catalyst_purist": {
        "name": "catalyst_purist",
        "description": "Only acts on named, verifiable, fresh catalysts. Never trades on technicals alone. No catalyst = no trade.",
        "cognitive_style": "event-driven, ignores price action without news",
        "catalyst_required": True,
        "max_catalyst_age_hours": 2,
        "preferred_regime": "any",
        "default_action": "hold if no fresh named catalyst",
    },
}

_NULL_CATALYSTS = {"no", "none", "null", "", "n/a", "na", "unknown"}


# ─────────────────────────────────────────────────────────────────────────────
# Dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ForkOpinion:
    schema_version: int = 1
    opinion_id: str = ""
    fork_name: str = ""
    case_id: str = ""
    case_type: str = ""
    generated_at: str = ""
    agrees_with_prod: bool = False
    would_action: str = ""
    conviction_adjustment: float = 0.0
    primary_reason: str = ""
    cognitive_conflict: str = ""
    confidence: float = 0.0
    abstention: Optional[dict] = None
    evaluation_class: str = "quality_positive_non_alpha"
    model_used: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_enabled() -> bool:
    try:
        from feature_flags import is_enabled  # noqa: PLC0415
        return is_enabled("enable_personality_forks")
    except Exception:
        return False


def _rule_based_abstain(fork_name: str, case_data: dict) -> Optional[str]:
    """
    Returns an abstention reason string if rules clearly indicate this fork
    cannot/should not evaluate the case. Returns None if LLM call should proceed.
    """
    persona = PERSONALITY_FORK_PERSONAS.get(fork_name, {})

    if fork_name == "paranoid":
        # Never vetoes outright — always opinionated, even if skeptically
        return None

    if fork_name == "opportunist":
        # Abstains on risk_off regime cases (outside domain)
        regime = str(case_data.get("regime_view", case_data.get("regime", ""))).lower()
        if "risk_off" in regime or "crisis" in regime:
            return "opportunist style does not apply in risk_off/crisis regime"
        return None

    if fork_name == "minimalist":
        # Abstains if position count context unavailable or irrelevant
        return None

    if fork_name == "anti_crowding":
        # Abstains if no signal consensus data to fade
        ideas = case_data.get("ideas", [])
        if not ideas and not case_data.get("regime_score"):
            return "no consensus signal data available to apply contrarian analysis"
        return None

    if fork_name == "catalyst_purist":
        # Abstains if there's a fresh named catalyst (no conflict to evaluate)
        # OR if we can determine there's no catalyst at all (rule-based veto)
        ideas = case_data.get("ideas", [])
        for idea in ideas:
            if not isinstance(idea, dict):
                continue
            catalyst = str(idea.get("catalyst", "") or "").strip().lower()
            if catalyst and catalyst not in _NULL_CATALYSTS:
                # Fresh catalyst present — purist has no objection, but we still
                # want to evaluate whether catalyst is within max_catalyst_age_hours
                return None
        # No fresh named catalyst found — rule-based abstain (would-be veto)
        return None  # still proceed to LLM for nuanced evaluation

    return None


def _paranoid_conviction_adjustment(conviction: float) -> float:
    """Apply paranoid's signal_discount_factor to conviction."""
    return round(conviction * PERSONALITY_FORK_PERSONAS["paranoid"]["signal_discount_factor"], 3)


def _call_fork_llm(fork_name: str, case_id: str, case_type: str, case_data: dict) -> Optional[ForkOpinion]:
    try:
        persona = PERSONALITY_FORK_PERSONAS[fork_name]
        model = model_tiering.get_model_for_module("personality_forks")
        import anthropic  # noqa: PLC0415
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))

        system_msg = (
            f"{_FORK_SYSTEM}\n\n"
            f"Your cognitive style: {persona['cognitive_style']}\n"
            f"Your description: {persona['description']}\n"
            f"Your default action: {persona['default_action']}"
        )

        case_summary = json.dumps(case_data, indent=2)[:1000]
        user_msg = (
            f"Case ID: {case_id}\n"
            f"Case type: {case_type}\n\n"
            f"Case data:\n{case_summary}\n\n"
            "Evaluate this case from your cognitive style. Return JSON:\n"
            "{\n"
            '  "agrees_with_prod": true/false,\n'
            '  "would_action": "enter" | "hold" | "exit" | "abstain",\n'
            '  "conviction_adjustment": <float -0.5 to +0.5 — how you would shift the conviction>,\n'
            '  "primary_reason": "<one sentence from your cognitive style>",\n'
            '  "cognitive_conflict": "<what in this case conflicts with your style>",\n'
            '  "confidence": 0.0-1.0\n'
            "}\n"
            'If this case truly cannot be evaluated by your style, return {"abstention": {"reason": "..."}}.'
        )

        response = client.messages.create(
            model=model,
            max_tokens=300,
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
                module_name="personality_forks",
                layer_name="annex_experiment",
                ring="lab",
                model=model,
                purpose="fork_opinion",
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
            return ForkOpinion(
                schema_version=1,
                opinion_id=str(uuid.uuid4()),
                fork_name=fork_name,
                case_id=case_id,
                case_type=case_type,
                generated_at=now,
                abstention=parsed["abstention"],
                confidence=0.0,
                model_used=model,
            )

        return ForkOpinion(
            schema_version=1,
            opinion_id=str(uuid.uuid4()),
            fork_name=fork_name,
            case_id=case_id,
            case_type=case_type,
            generated_at=now,
            agrees_with_prod=bool(parsed.get("agrees_with_prod", False)),
            would_action=str(parsed.get("would_action", "hold"))[:30],
            conviction_adjustment=float(parsed.get("conviction_adjustment", 0.0)),
            primary_reason=str(parsed.get("primary_reason", ""))[:300],
            cognitive_conflict=str(parsed.get("cognitive_conflict", ""))[:300],
            confidence=float(parsed.get("confidence", 0.5)),
            model_used=model,
        )
    except Exception as exc:
        log.warning("[FORKS] _call_fork_llm failed for %s/%s: %s", fork_name, case_id, exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Rule-based pre-filtering for specific forks
# ─────────────────────────────────────────────────────────────────────────────

def _apply_paranoid_rules(case_id: str, case_type: str, case_data: dict) -> Optional[ForkOpinion]:
    """
    Paranoid pre-filter: if best conviction < veto_threshold (0.85) after
    applying signal_discount_factor, paranoid would veto without LLM call.
    """
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    persona = PERSONALITY_FORK_PERSONAS["paranoid"]
    discount = persona["signal_discount_factor"]
    veto_thresh = persona["veto_threshold"]

    ideas = case_data.get("ideas", [])
    max_conviction = 0.0
    for idea in ideas:
        if isinstance(idea, dict):
            c = float(idea.get("conviction", idea.get("confidence", 0.0)) or 0.0)
            max_conviction = max(max_conviction, c)

    adjusted = max_conviction * discount
    if max_conviction > 0 and adjusted < veto_thresh:
        # Conviction after discount is below threshold — rule-based veto
        return ForkOpinion(
            schema_version=1,
            opinion_id=str(uuid.uuid4()),
            fork_name="paranoid",
            case_id=case_id,
            case_type=case_type,
            generated_at=now,
            agrees_with_prod=False,
            would_action="hold",
            conviction_adjustment=round(adjusted - max_conviction, 3),
            primary_reason=(
                f"Conviction {max_conviction:.2f} after paranoid discount "
                f"({discount}x) = {adjusted:.2f} — below veto threshold {veto_thresh}"
            ),
            cognitive_conflict="Any entry below discounted threshold is adverse selection risk",
            confidence=0.75,
            evaluation_class="quality_positive_non_alpha",
        )
    return None  # proceed to LLM


def _apply_catalyst_purist_rules(case_id: str, case_type: str, case_data: dict) -> Optional[ForkOpinion]:
    """
    Catalyst purist pre-filter: if all entry ideas have null/empty catalysts,
    purist vetoes without LLM call.
    """
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    ideas = case_data.get("ideas", [])
    entry_ideas = [
        i for i in ideas
        if isinstance(i, dict) and str(i.get("intent", "")).startswith("enter")
    ]
    if not entry_ideas:
        return None  # no entries — nothing to veto, proceed to LLM

    all_null = all(
        str(i.get("catalyst", "") or "").strip().lower() in _NULL_CATALYSTS
        for i in entry_ideas
    )
    if all_null:
        return ForkOpinion(
            schema_version=1,
            opinion_id=str(uuid.uuid4()),
            fork_name="catalyst_purist",
            case_id=case_id,
            case_type=case_type,
            generated_at=now,
            agrees_with_prod=False,
            would_action="hold",
            conviction_adjustment=-0.5,
            primary_reason="No named verifiable catalyst — catalyst_purist cannot enter without one",
            cognitive_conflict="Entry submitted with null/empty catalyst violates catalyst_required rule",
            confidence=0.9,
            evaluation_class="quality_positive_non_alpha",
        )
    return None  # has catalyst — proceed to LLM


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def get_fork_names() -> list:
    """Returns list of all fork persona names."""
    return list(PERSONALITY_FORK_PERSONAS.keys())


def get_fork_opinion(
    fork_name: str,
    case_id: str,
    case_type: str,
    case_data: dict,
) -> Optional[ForkOpinion]:
    """
    Apply fork persona to case. Rule-based pre-filter first; LLM only if needed.
    Returns ForkOpinion or None on hard failure.
    """
    try:
        if not _is_enabled():
            return None

        if fork_name not in PERSONALITY_FORK_PERSONAS:
            log.warning("[FORKS] unknown fork_name=%r", fork_name)
            return None

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        # Rule-based abstention check
        abstain_reason = _rule_based_abstain(fork_name, case_data)
        if abstain_reason:
            return ForkOpinion(
                schema_version=1,
                opinion_id=str(uuid.uuid4()),
                fork_name=fork_name,
                case_id=case_id,
                case_type=case_type,
                generated_at=now,
                abstention={"reason": abstain_reason},
                confidence=0.0,
            )

        # Fork-specific rule-based pre-filters (before LLM call)
        if fork_name == "paranoid":
            rule_result = _apply_paranoid_rules(case_id, case_type, case_data)
            if rule_result is not None:
                return rule_result

        if fork_name == "catalyst_purist":
            rule_result = _apply_catalyst_purist_rules(case_id, case_type, case_data)
            if rule_result is not None:
                return rule_result

        return _call_fork_llm(fork_name, case_id, case_type, case_data)
    except Exception as exc:
        log.warning("[FORKS] get_fork_opinion failed for %s/%s: %s", fork_name, case_id, exc)
        return None


def get_all_fork_opinions(
    case_id: str,
    case_type: str,
    case_data: dict,
) -> list:
    """
    Get opinions from all 5 fork personas. Returns list (may include abstentions).
    Non-fatal.
    """
    opinions = []
    if not _is_enabled():
        return opinions
    for fork_name in PERSONALITY_FORK_PERSONAS:
        try:
            opinion = get_fork_opinion(fork_name, case_id, case_type, case_data)
            if opinion is not None:
                opinions.append(opinion)
        except Exception as exc:
            log.warning("[FORKS] get_all_fork_opinions failed for %s: %s", fork_name, exc)
    return opinions


def log_opinion(opinion: ForkOpinion) -> Optional[str]:
    """Appends to data/annex/personality_forks/opinions.jsonl. Returns opinion_id or None."""
    try:
        _ANNEX_DIR.mkdir(parents=True, exist_ok=True)
        with open(_OPINIONS_LOG, "a") as fh:
            fh.write(json.dumps(asdict(opinion)) + "\n")
        return opinion.opinion_id
    except Exception as exc:
        log.warning("[FORKS] log_opinion failed: %s", exc)
        return None


def format_fork_divergence_for_review(
    case_id: str,
    case_type: str,
    case_data: dict,
) -> str:
    """
    Returns markdown showing where forks agree/disagree with prod and each other.
    Returns "" on error or all abstentions.
    """
    try:
        if not _is_enabled():
            return ""

        opinions = get_all_fork_opinions(case_id, case_type, case_data)
        if not opinions:
            return ""

        active = [o for o in opinions if not o.abstention]
        if not active:
            return f"## Fork Panel — {case_id}\nAll forks abstained."

        agrees = sum(1 for o in active if o.agrees_with_prod)
        disagrees = len(active) - agrees
        abstained = len(opinions) - len(active)

        lines = [
            f"## Fork Divergence — {case_id} ({case_type})\n",
            f"Agrees: {agrees} | Disagrees: {disagrees} | Abstained: {abstained}",
            "",
            "| Fork | Action | Conv Adj | Agrees | Confidence |",
            "|------|--------|----------|--------|------------|",
        ]

        for fork_name in PERSONALITY_FORK_PERSONAS:
            o_map = {o.fork_name: o for o in opinions}
            o = o_map.get(fork_name)
            if o is None:
                continue
            if o.abstention:
                lines.append(f"| {fork_name} | — | — | abstain | — |")
            else:
                agrees_str = "✅" if o.agrees_with_prod else "❌"
                adj_str = f"{o.conviction_adjustment:+.2f}"
                lines.append(
                    f"| {fork_name} | {o.would_action} | {adj_str} | {agrees_str} | {o.confidence:.2f} |"
                )

        if active:
            lines.append("")
            lines.append("**Primary reasons:**")
            for o in active:
                if o.primary_reason:
                    lines.append(f"  - **{o.fork_name}:** {o.primary_reason[:120]}")

        return "\n".join(lines)
    except Exception as exc:
        log.warning("[FORKS] format_fork_divergence_for_review failed: %s", exc)
        return ""
