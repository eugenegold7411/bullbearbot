"""
model_tiering.py — Canonical model tier definitions and escalation policy (T1.8).

Governance substrate — defines policy, does not execute.
Feature flag: enable_model_tiering (in feature_flags). When False,
get_model_for_module() returns the default tier model string unchanged.

CACHE INVALIDATION WARNING (document, do not enforce in code):
Every tier escalation mid-session causes a cache miss on the new model,
paying full cache-write cost. If escalation triggers fire frequently,
cache economics degrade significantly.
Measure: compare cache_hit_input_tokens before/after enabling escalation.
This is logged in the spine — review weekly via format_spine_summary_for_review().
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import cost_attribution

log = logging.getLogger(__name__)

# CACHE INVALIDATION WARNING: see module docstring.
CACHE_INVALIDATION_WARNING = (
    "Tier escalation causes cache miss on new model. "
    "Monitor cache_hit_input_tokens in spine summary before widening escalation triggers."
)

# Per annex sandbox contract addendum:
# No annex module may use premium tier without a numeric evaluation contract on file.
ANNEX_PREMIUM_REQUIRES_EVAL_CONTRACT = True


# ─────────────────────────────────────────────────────────────────────────────
# Tier definitions
# ─────────────────────────────────────────────────────────────────────────────

class ModelTier(str, Enum):
    CHEAP   = "cheap"    # claude-haiku-4-5-20251001
    DEFAULT = "default"  # claude-sonnet-4-6
    PREMIUM = "premium"  # claude-opus-4-7


TIER_MODELS: dict[ModelTier, str] = {
    ModelTier.CHEAP:   "claude-haiku-4-5-20251001",
    ModelTier.DEFAULT: "claude-sonnet-4-6",
    ModelTier.PREMIUM: "claude-opus-4-7",
}


class BudgetClass(str, Enum):
    NEGLIGIBLE    = "negligible"    # < $0.01/day
    LOW           = "low"           # $0.01 – $0.10/day
    MEDIUM        = "medium"        # $0.10 – $1.00/day
    EXPERIMENTAL  = "experimental"  # unknown / being measured


# ─────────────────────────────────────────────────────────────────────────────
# Module tier declarations
# ─────────────────────────────────────────────────────────────────────────────

MODULE_TIER_DECLARATIONS: dict[str, dict] = {
    "regime_classifier":              {"tier": ModelTier.CHEAP,   "ring": "prod",   "budget_class": BudgetClass.LOW},
    "signal_scorer":                  {"tier": ModelTier.CHEAP,   "ring": "prod",   "budget_class": BudgetClass.LOW},
    "scratchpad":                     {"tier": ModelTier.CHEAP,   "ring": "prod",   "budget_class": BudgetClass.LOW},
    "macro_wire_classifier":          {"tier": ModelTier.CHEAP,   "ring": "prod",   "budget_class": BudgetClass.LOW},
    "main_decision":                  {"tier": ModelTier.DEFAULT, "ring": "prod",   "budget_class": BudgetClass.MEDIUM},
    "weekly_review_agent_1":          {"tier": ModelTier.DEFAULT, "ring": "prod",   "budget_class": BudgetClass.LOW},
    "weekly_review_agent_2":          {"tier": ModelTier.DEFAULT, "ring": "prod",   "budget_class": BudgetClass.LOW},
    "weekly_review_agent_3":          {"tier": ModelTier.DEFAULT, "ring": "prod",   "budget_class": BudgetClass.LOW},
    "weekly_review_agent_4":          {"tier": ModelTier.DEFAULT, "ring": "prod",   "budget_class": BudgetClass.LOW},
    "weekly_review_agent_5_cto":      {"tier": ModelTier.DEFAULT, "ring": "prod",   "budget_class": BudgetClass.LOW},
    "weekly_review_agent_6_director": {"tier": ModelTier.DEFAULT, "ring": "prod",   "budget_class": BudgetClass.LOW},
    "weekly_review_agent_11_narrative": {"tier": ModelTier.CHEAP, "ring": "prod",   "budget_class": BudgetClass.NEGLIGIBLE},
    "options_debate":                 {"tier": ModelTier.DEFAULT, "ring": "prod",   "budget_class": BudgetClass.LOW},
    "context_compiler":               {"tier": ModelTier.CHEAP,   "ring": "shadow", "budget_class": BudgetClass.EXPERIMENTAL},
    "morning_brief":                  {"tier": ModelTier.CHEAP,   "ring": "prod",   "budget_class": BudgetClass.LOW},
}


# ─────────────────────────────────────────────────────────────────────────────
# Escalation predicates
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class EscalationContext:
    """Input to escalation predicate evaluation."""
    top_signal_scores: list        # top 3 signal scores this cycle
    regime_score: int               # 0-100 from regime classifier
    regime_bias: str                # "bullish" | "bearish" | "neutral"
    open_position_count: int
    signals_conflict: bool          # top signal direction != regime bias
    catalyst_count: int
    deadline_approaching: bool
    vix_level: float


def should_escalate_to_premium(ctx: EscalationContext) -> tuple[bool, str]:
    """
    Computable escalation predicates — no subjective assessment.
    Returns (should_escalate, reason).

    Escalation triggers (ANY of):
    - signals_conflict AND all top-3 scores within 10 points (ambiguous environment)
    - catalyst_count >= 3 AND signals_conflict (multiple competing catalysts)
    - open_position_count >= 5 AND regime_score < 30 (many positions in choppy regime)
    - deadline_approaching AND signals_conflict (forced decision under ambiguity)
    - vix_level > 35 (crisis regime)
    """
    try:
        scores = ctx.top_signal_scores or []
        if ctx.signals_conflict and len(scores) >= 3:
            score_range = max(scores[:3]) - min(scores[:3])
            if score_range <= 10:
                return True, "ambiguous_signal_environment"

        if ctx.catalyst_count >= 3 and ctx.signals_conflict:
            return True, "multiple_competing_catalysts_with_conflict"

        if ctx.open_position_count >= 5 and ctx.regime_score < 30:
            return True, "many_positions_in_defensive_regime"

        if ctx.deadline_approaching and ctx.signals_conflict:
            return True, "forced_decision_under_ambiguity"

        if ctx.vix_level > 35:
            return True, "crisis_regime_vix_above_35"

        return False, ""
    except Exception as exc:  # noqa: BLE001
        log.warning("[TIERING] should_escalate_to_premium failed: %s", exc)
        return False, ""


def cheap_tier_abstained_escalate(abstention: Optional[object]) -> bool:
    """
    Policy: does a cheap-tier abstention warrant escalation to default tier?

    Returns True ONLY if:
    - abstention is not None AND abstain=True
    - evidence_present=True (cheap tier couldn't resolve it — worth escalating)
    - unknown=False (unknown inputs don't benefit from escalation)

    Returns False if evidence_present=False — unknown inputs must NOT
    auto-escalate (cost leak prevention).
    """
    try:
        if abstention is None:
            return False
        if isinstance(abstention, dict):
            abstain = abstention.get("abstain", False)
            evidence_present = abstention.get("evidence_present", False)
            unknown = abstention.get("unknown", False)
        else:
            abstain = getattr(abstention, "abstain", False)
            evidence_present = getattr(abstention, "evidence_present", False)
            unknown = getattr(abstention, "unknown", False)
        return bool(abstain and evidence_present and not unknown)
    except Exception:  # noqa: BLE001
        return False


def annex_may_use_premium(module_name: str, eval_contract_on_file: bool) -> bool:
    """
    Returns True only if eval_contract_on_file is True.
    Persuasiveness and coherence are NOT evidence of promotion-worthiness.
    """
    return eval_contract_on_file


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def get_module_tier(module_name: str) -> ModelTier:
    """
    Returns declared tier. Returns ModelTier.DEFAULT if unknown.
    Logs WARNING if module_name not in MODULE_TIER_DECLARATIONS.
    """
    decl = MODULE_TIER_DECLARATIONS.get(module_name)
    if decl is None:
        log.warning("[TIERING] Module %r not in MODULE_TIER_DECLARATIONS — defaulting to DEFAULT", module_name)
        return ModelTier.DEFAULT
    return decl["tier"]


def get_model_for_module(
    module_name: str,
    ctx: Optional[EscalationContext] = None,
) -> str:
    """
    Return canonical model string for module.
    If ctx provided and should_escalate_to_premium fires AND flag is enabled:
    - escalates DEFAULT modules to PREMIUM (never CHEAP → PREMIUM directly)
    - logs escalation to spine
    Returns TIER_MODELS[tier] otherwise.
    """
    try:
        from feature_flags import is_enabled  # noqa: PLC0415
        tier = get_module_tier(module_name)
        base_model = TIER_MODELS[tier]

        if ctx is not None and is_enabled("enable_model_tiering"):
            escalate, reason = should_escalate_to_premium(ctx)
            if escalate and tier == ModelTier.DEFAULT:
                premium_model = TIER_MODELS[ModelTier.PREMIUM]
                try:
                    cost_attribution.log_spine_record(
                        module_name=module_name,
                        layer_name="execution_control",
                        ring="prod",
                        model=premium_model,
                        purpose="tier_escalation",
                    )
                except Exception:
                    pass
                log.info("[TIERING] Escalated %s → PREMIUM (%s)", module_name, reason)
                return premium_model

        return base_model
    except Exception as exc:  # noqa: BLE001
        log.warning("[TIERING] get_model_for_module failed: %s", exc)
        return TIER_MODELS.get(ModelTier.DEFAULT, "claude-sonnet-4-6")


def format_tier_summary_for_review() -> str:
    """
    Return markdown table of all declared modules with tier, ring, budget_class.
    For weekly review CFO/CTO sections.
    """
    try:
        lines = [
            "### Module Tier Declarations",
            "",
            "| Module | Tier | Ring | Budget Class |",
            "|--------|------|------|-------------|",
        ]
        for mod, decl in sorted(MODULE_TIER_DECLARATIONS.items()):
            tier = decl["tier"].value if hasattr(decl["tier"], "value") else str(decl["tier"])
            ring = decl["ring"]
            budget = decl["budget_class"].value if hasattr(decl["budget_class"], "value") else str(decl["budget_class"])
            lines.append(f"| {mod} | {tier} | {ring} | {budget} |")
        lines.append(f"\n{CACHE_INVALIDATION_WARNING}")
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001
        log.warning("[TIERING] format_tier_summary_for_review failed: %s", exc)
        return ""
