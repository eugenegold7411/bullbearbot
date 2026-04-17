# SHADOW MODULE — do not import from prod pipeline
"""
semantic_router.py — Deterministic cycle routing (T3.6).

Shadow-ring routing engine. Applies deterministic rules to recommend FULL vs COMPACT
prompt cycles, then compares against sonnet_gate's actual decision and logs divergences.

No LLM calls — zero cost per cycle. Cost attribution: purpose="routing_decision", ring="shadow",
cost_usd=0.0.

Feature flag: enable_semantic_router_shadow (shadow_flags, default false).
Wire: bot.py calls route_cycle() at market cycle start after gate decision is made.

CRITICAL CONSTRAINT: sonnet_gate.py is the live production routing authority.
This module MUST NOT be imported by sonnet_gate.py. Shadow only.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_ROUTER_LOG = Path("data/analytics/router_decisions.jsonl")
_GATE_STATE_PATH = Path("data/market/gate_state.json")


# ─────────────────────────────────────────────────────────────────────────────
# Dataclasses
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RouterContext:
    """Input snapshot for one routing evaluation."""
    schema_version: int = 1
    cycle_id: str = ""
    session_tier: str = "market"
    regime_score: int = 50
    signals_conflict: bool = False
    catalyst_count: int = 0
    vix_level: float = 0.0
    has_breaking_news: bool = False
    deadline_approaching: bool = False
    top_signal_delta: float = 0.0
    open_position_count: int = 0
    sonnet_gate_last_decision: str = ""
    sonnet_gate_last_mode: str = ""


@dataclass
class RouterDecision:
    """Result of one routing evaluation."""
    schema_version: int = 1
    decision_id: str = ""
    cycle_id: str = ""
    decided_at: str = ""
    recommended_mode: str = ""
    reason: str = ""
    diverged_from_gate: bool = False
    divergence_reason: str = ""
    gate_mode: str = ""
    context_snapshot: dict = field(default_factory=dict)
    layer_name: str = "semantic_router"
    ring: str = "shadow"
    purpose: str = "routing_decision"
    cost_usd: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_enabled() -> bool:
    try:
        from feature_flags import is_enabled  # noqa: PLC0415
        return is_enabled("enable_semantic_router_shadow")
    except Exception:
        return False


def _load_gate_state() -> dict:
    try:
        if _GATE_STATE_PATH.exists():
            return json.loads(_GATE_STATE_PATH.read_text())
    except Exception:
        pass
    return {}


def _apply_routing_rules(ctx: RouterContext) -> tuple[str, str]:
    """
    Apply deterministic routing rules. Returns (recommended_mode, reason).
    FULL triggers: breaking news, deadline, conflict+catalysts, extreme regime, high VIX.
    COMPACT: calm baseline.
    """
    if ctx.has_breaking_news:
        return "FULL", "has_breaking_news"
    if ctx.deadline_approaching:
        return "FULL", "deadline_approaching"
    if ctx.signals_conflict and ctx.catalyst_count >= 2:
        return "FULL", "signals_conflict_with_multiple_catalysts"
    if ctx.regime_score < 25:
        return "FULL", "extreme_bearish_regime"
    if ctx.regime_score > 75:
        return "FULL", "extreme_bullish_regime"
    if ctx.catalyst_count >= 3:
        return "FULL", "three_or_more_catalysts"
    if ctx.vix_level > 30:
        return "FULL", "elevated_vix"
    if ctx.top_signal_delta > 30:
        return "FULL", "high_signal_spread"
    if ctx.open_position_count >= 5 and ctx.regime_score < 35:
        return "FULL", "many_positions_in_defensive_regime"
    return "COMPACT", "calm_baseline"


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def route_cycle(context: RouterContext) -> RouterDecision:
    """
    Evaluate routing rules deterministically; compare to gate's last decision.
    Returns RouterDecision. Never raises.
    """
    try:
        recommended_mode, reason = _apply_routing_rules(context)

        gate_state = _load_gate_state()
        gate_mode = gate_state.get("last_mode", "") or ""
        if not gate_mode and gate_state.get("last_trigger"):
            gate_mode = "FULL"

        diverged = bool(gate_mode and gate_mode != recommended_mode)
        divergence_reason = (
            f"router={recommended_mode} gate={gate_mode}" if diverged else ""
        )

        return RouterDecision(
            schema_version=1,
            decision_id=str(uuid.uuid4()),
            cycle_id=context.cycle_id,
            decided_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            recommended_mode=recommended_mode,
            reason=reason,
            diverged_from_gate=diverged,
            divergence_reason=divergence_reason,
            gate_mode=gate_mode,
            context_snapshot={
                "regime_score": context.regime_score,
                "signals_conflict": context.signals_conflict,
                "catalyst_count": context.catalyst_count,
                "vix_level": context.vix_level,
                "has_breaking_news": context.has_breaking_news,
                "deadline_approaching": context.deadline_approaching,
                "top_signal_delta": context.top_signal_delta,
                "open_position_count": context.open_position_count,
                "session_tier": context.session_tier,
            },
            layer_name="semantic_router",
            ring="shadow",
            purpose="routing_decision",
            cost_usd=0.0,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("[ROUTER] route_cycle failed: %s", exc)
        return RouterDecision(
            decision_id=str(uuid.uuid4()),
            cycle_id=getattr(context, "cycle_id", ""),
            decided_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            recommended_mode="COMPACT",
            reason="error_fallback",
        )


def log_router_decision(decision: RouterDecision) -> None:
    """
    Append RouterDecision to router_decisions.jsonl and log zero-cost spine record.
    Never raises.
    """
    try:
        _ROUTER_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_ROUTER_LOG, "a") as fh:
            fh.write(json.dumps(asdict(decision)) + "\n")

        try:
            import cost_attribution as _ca  # noqa: PLC0415
            _ca.log_spine_record(
                module_name="semantic_router",
                layer_name="semantic_router",
                ring="shadow",
                model="none",
                purpose="routing_decision",
                linked_subject_id=decision.cycle_id,
                linked_subject_type="cycle",
                input_tokens=0,
                output_tokens=0,
                estimated_cost_usd=0.0,
            )
        except Exception:
            pass

        if decision.diverged_from_gate:
            log.info(
                "[ROUTER] DIVERGENCE cycle=%s router=%s gate=%s reason=%s",
                decision.cycle_id,
                decision.recommended_mode,
                decision.gate_mode,
                decision.divergence_reason,
            )
        else:
            log.debug(
                "[ROUTER] cycle=%s mode=%s reason=%s",
                decision.cycle_id,
                decision.recommended_mode,
                decision.reason,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("[ROUTER] log_router_decision failed: %s", exc)


def get_router_decisions(days_back: int = 7) -> list:
    """
    Read router_decisions.jsonl; return list of RouterDecision dicts.
    Returns [] on any error.
    """
    results = []
    try:
        if not _ROUTER_LOG.exists():
            return results
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        with open(_ROUTER_LOG) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts_str = rec.get("decided_at", "")
                    if ts_str:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if ts < cutoff:
                            continue
                    results.append(rec)
                except Exception:
                    continue
    except Exception as exc:  # noqa: BLE001
        log.warning("[ROUTER] get_router_decisions failed: %s", exc)
    return results


def format_router_summary_for_review(days_back: int = 7) -> str:
    """
    Format a markdown summary of routing decisions for the weekly CTO review.
    Includes divergence rate, mode distribution, top divergence reasons.
    """
    try:
        decisions = get_router_decisions(days_back=days_back)
        if not decisions:
            return f"## Semantic Router Summary ({days_back}d)\nNo decisions logged yet."

        total = len(decisions)
        diverged = sum(1 for d in decisions if d.get("diverged_from_gate", False))
        divergence_rate = diverged / total if total > 0 else 0.0

        mode_counts: dict = {}
        for d in decisions:
            mode = d.get("recommended_mode", "UNKNOWN")
            mode_counts[mode] = mode_counts.get(mode, 0) + 1

        reason_counts: dict = {}
        for d in decisions:
            if d.get("diverged_from_gate"):
                reason = d.get("divergence_reason", "unknown")
                reason_counts[reason] = reason_counts.get(reason, 0) + 1

        lines = [
            f"## Semantic Router Summary ({days_back}d)",
            "",
            f"Total decisions: {total}",
            f"Divergence rate: {divergence_rate:.1%} ({diverged}/{total})",
            "",
            "**Mode distribution:**",
        ]
        for mode, count in sorted(mode_counts.items(), key=lambda x: -x[1]):
            lines.append(f"  - {mode}: {count} ({count / total:.0%})")

        if reason_counts:
            lines.append("")
            lines.append("**Divergence reasons:**")
            for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
                lines.append(f"  - {reason}: {count}")

        pass_fail = "PASS" if divergence_rate < 0.10 else "REVIEW"
        lines.append("")
        lines.append(f"**Promotion gate:** divergence <10% → {pass_fail}")

        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001
        log.warning("[ROUTER] format_router_summary_for_review failed: %s", exc)
        return "Semantic Router Summary: (unavailable)"


def run_shadow_routing(
    cycle_id: str,
    regime_score: int = 50,
    signals_conflict: bool = False,
    catalyst_count: int = 0,
    vix_level: float = 0.0,
    has_breaking_news: bool = False,
    deadline_approaching: bool = False,
    top_signal_delta: float = 0.0,
    open_position_count: int = 0,
    session_tier: str = "market",
) -> Optional[RouterDecision]:
    """
    Convenience wrapper: build RouterContext, call route_cycle(), log, return decision.
    Returns None when flag is disabled. Never raises.
    """
    try:
        if not _is_enabled():
            return None
        ctx = RouterContext(
            cycle_id=cycle_id,
            session_tier=session_tier,
            regime_score=regime_score,
            signals_conflict=signals_conflict,
            catalyst_count=catalyst_count,
            vix_level=vix_level,
            has_breaking_news=has_breaking_news,
            deadline_approaching=deadline_approaching,
            top_signal_delta=top_signal_delta,
            open_position_count=open_position_count,
        )
        decision = route_cycle(ctx)
        log_router_decision(decision)
        return decision
    except Exception as exc:  # noqa: BLE001
        log.warning("[ROUTER] run_shadow_routing failed: %s", exc)
        return None
