# ANNEX MODULE — lab ring, no prod pipeline imports
"""
annex/internal_parliament.py — Internal Parliament experiment (T6.6).

Evaluation class: quality_positive_non_alpha

Router-triggered deliberative session. 4 fixed delegates (bull_advocate,
bear_advocate, risk_auditor, synthesis_chair). Parliament only convenes on
high-complexity trigger conditions — NOT every cycle. Observational only;
output never modifies the actual decision pipeline.

Storage: data/annex/internal_parliament/sessions.jsonl
Feature flag: enable_internal_parliament (lab_flags, default False).
Promotion contract: promotion_contracts/internal_parliament_v1.md (DRAFT).

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import model_tiering

log = logging.getLogger(__name__)

_ANNEX_DIR = Path("data/annex/internal_parliament")
_SESSIONS_LOG = _ANNEX_DIR / "sessions.jsonl"

PARLIAMENT_DELEGATES = {
    "bull_advocate": {
        "mandate": "Make the strongest possible case for the most bullish action available.",
        "bias": "bullish",
        "veto_power": False,
    },
    "bear_advocate": {
        "mandate": "Make the strongest possible case for the most bearish or defensive action.",
        "bias": "bearish",
        "veto_power": False,
    },
    "risk_auditor": {
        "mandate": "Identify every risk, hidden dependency, and failure mode. No advocacy.",
        "bias": "neutral",
        "veto_power": True,
    },
    "synthesis_chair": {
        "mandate": "Synthesize the debate. Produce a structured verdict and dissent summary.",
        "bias": "neutral",
        "veto_power": False,
    },
}

_VALID_VERDICTS = {"proceed", "hold", "reduce", "exit", "abstain"}

_PARLIAMENT_SYSTEM = (
    "You are a delegate in an internal deliberative parliament for a trading bot. "
    "Your role is defined strictly by your mandate. "
    "Base ALL arguments on the provided context data. "
    "Do not invent information not present in the context. "
    "Be concise and structured. Output text only (no JSON)."
)

_SYNTHESIS_SYSTEM = (
    "You are the synthesis chair of an internal deliberative parliament for a trading bot. "
    "You receive outputs from three other delegates and must synthesize their arguments "
    "into a structured verdict. "
    "Output JSON only with fields: verdict, confidence, minority_veto_issued, "
    "minority_veto_reason, dissent_summary."
)


# ─────────────────────────────────────────────────────────────────────────────
# Dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ParliamentSession:
    schema_version: int = 1
    session_id: str = ""
    convened_at: str = ""
    trigger_reason: str = ""
    cycle_id: str = ""
    context_summary: str = ""
    delegate_outputs: dict = field(default_factory=dict)
    synthesis_verdict: str = "abstain"
    synthesis_confidence: float = 0.0
    minority_veto_issued: bool = False
    minority_veto_reason: Optional[str] = None
    dissent_summary: Optional[str] = None
    abstention: Optional[dict] = None
    evaluation_class: str = "quality_positive_non_alpha"
    model_used: str = ""
    total_cost_usd: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_enabled() -> bool:
    try:
        from feature_flags import is_enabled  # noqa: PLC0415
        return is_enabled("enable_internal_parliament")
    except Exception:
        return False


def _extract_router_fields(router_decision: dict | None) -> dict:
    """Pull relevant fields from RouterDecision (dict form) or context_snapshot."""
    if router_decision is None:
        return {}
    ctx = router_decision.get("context_snapshot", {})
    return {
        "signals_conflict": bool(
            router_decision.get("signals_conflict",
            ctx.get("signals_conflict", False))
        ),
        "catalyst_count": int(
            router_decision.get("catalyst_count",
            ctx.get("catalyst_count", 0)) or 0
        ),
        "regime_score": int(
            router_decision.get("regime_score",
            ctx.get("regime_score", 50)) or 50
        ),
        "open_position_count": int(
            router_decision.get("open_position_count",
            ctx.get("open_position_count", 0)) or 0
        ),
        "route_uncertain": bool(
            router_decision.get("diverged_from_gate",
            ctx.get("diverged_from_gate", False))
        ),
    }


def _call_delegate_llm(
    delegate_name: str,
    mandate: str,
    context_str: str,
    prior_outputs: str,
    model: str,
) -> tuple[str, float]:
    """Call one delegate. Returns (output_text, cost_usd)."""
    import anthropic  # noqa: PLC0415
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))

    user_msg = (
        f"Your mandate: {mandate}\n\n"
        f"Context:\n{context_str[:800]}\n"
    )
    if prior_outputs:
        user_msg += f"\nPrior delegate arguments:\n{prior_outputs[:600]}\n"
    user_msg += "\nProvide your analysis in 3-5 sentences, strictly per your mandate."

    response = client.messages.create(
        model=model,
        max_tokens=350,
        system=_PARLIAMENT_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = response.content[0].text if response.content else ""
    in_tok = response.usage.input_tokens
    out_tok = response.usage.output_tokens
    cost = (in_tok * 1.0 + out_tok * 5.0) / 1_000_000
    return text[:800], cost


def _call_synthesis_llm(
    delegate_outputs: dict,
    context_str: str,
    model: str,
) -> tuple[dict, float]:
    """Synthesis chair call. Returns (parsed_dict, cost_usd)."""
    import anthropic  # noqa: PLC0415
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))

    delegate_text = "\n".join(
        f"[{name}]: {output}"
        for name, output in delegate_outputs.items()
    )
    user_msg = (
        f"Context summary:\n{context_str[:400]}\n\n"
        f"Delegate arguments:\n{delegate_text[:1000]}\n\n"
        "Synthesize into JSON:\n"
        "{\n"
        '  "verdict": "proceed"|"hold"|"reduce"|"exit"|"abstain",\n'
        '  "confidence": 0.0-1.0,\n'
        '  "minority_veto_issued": true|false,\n'
        '  "minority_veto_reason": "<one sentence or null>",\n'
        '  "dissent_summary": "<one sentence or null>"\n'
        "}"
    )

    response = client.messages.create(
        model=model,
        max_tokens=400,
        system=_SYNTHESIS_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = response.content[0].text if response.content else "{}"
    in_tok = response.usage.input_tokens
    out_tok = response.usage.output_tokens
    cost = (in_tok * 1.0 + out_tok * 5.0) / 1_000_000

    try:
        parsed = json.loads(raw)
    except Exception:
        import re
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        parsed = json.loads(m.group()) if m else {}

    return parsed, cost


def _log_spine(module_name: str, model: str, in_tok: int, out_tok: int,
               cost: float, purpose: str) -> None:
    try:
        import cost_attribution as _ca  # noqa: PLC0415
        _ca.log_spine_record(
            module_name=module_name,
            layer_name="annex_experiment",
            ring="lab",
            model=model,
            purpose=purpose,
            input_tokens=in_tok,
            output_tokens=out_tok,
            estimated_cost_usd=round(cost, 6),
        )
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def should_convene(router_decision: dict | None) -> tuple[bool, str]:
    """
    Check trigger conditions against RouterDecision fields.
    Returns (True, reason) if parliament should convene, (False, "") otherwise.
    Non-fatal.
    """
    try:
        f = _extract_router_fields(router_decision)
        sc = f.get("signals_conflict", False)
        cc = f.get("catalyst_count", 0)
        rs = f.get("regime_score", 50)
        opc = f.get("open_position_count", 0)
        ru = f.get("route_uncertain", False)

        if sc and cc >= 2:
            return True, "signals_conflict_with_multiple_catalysts"
        if rs < 25:
            return True, "extreme_bearish_regime"
        if rs > 80:
            return True, "extreme_bullish_regime"
        if opc >= 4 and sc:
            return True, "many_positions_with_signal_conflict"
        if ru and opc >= 2:
            return True, "uncertain_routing_with_open_positions"
        return False, ""
    except Exception as exc:
        log.debug("[PARLIAMENT] should_convene failed: %s", exc)
        return False, ""


def convene_parliament(
    cycle_id: str,
    context: dict,
    router_decision: dict | None = None,
) -> Optional[ParliamentSession]:
    """
    Checks should_convene() — returns None if conditions not met.
    Makes 4 Haiku calls (one per delegate) sequentially.
    Synthesis chair receives all three other outputs.
    Non-fatal.
    """
    try:
        if not _is_enabled():
            return None

        convene, reason = should_convene(router_decision)
        if not convene:
            return None

        model = model_tiering.get_model_for_module("internal_parliament")
        session_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        # Build context summary
        regime = str(context.get("regime_view", context.get("regime", "unknown")) or "unknown")
        positions = context.get("positions", [])
        signals = context.get("signals", context.get("signal_scores", []))
        ctx_summary = (
            f"Regime: {regime} | Positions: {len(positions) if isinstance(positions, list) else '?'} | "
            f"Signals: {len(signals) if isinstance(signals, list) else '?'}"
        )
        context_str = json.dumps({
            k: v for k, v in context.items()
            if k in ("regime_view", "regime", "positions", "signals",
                     "catalyst_context", "notes", "concerns", "signal_scores")
        }, indent=2)[:1000]

        delegate_outputs: dict = {}
        total_cost = 0.0

        # Calls for bull, bear, risk_auditor
        for name in ("bull_advocate", "bear_advocate", "risk_auditor"):
            mandate = PARLIAMENT_DELEGATES[name]["mandate"]
            prior = "\n".join(f"[{n}]: {o}" for n, o in delegate_outputs.items())
            try:
                output, cost = _call_delegate_llm(name, mandate, context_str, prior, model)
                delegate_outputs[name] = output
                total_cost += cost
                _log_spine("internal_parliament", model, 0, 0, cost, f"parliament_delegate_{name}")
            except Exception as exc:
                log.debug("[PARLIAMENT] delegate %s failed: %s", name, exc)
                delegate_outputs[name] = f"(unavailable: {type(exc).__name__})"

        # Synthesis chair call
        synthesis_data: dict = {}
        try:
            synthesis_data, cost = _call_synthesis_llm(delegate_outputs, context_str, model)
            total_cost += cost
            _log_spine("internal_parliament", model, 0, 0, cost, "parliament_synthesis")
        except Exception as exc:
            log.debug("[PARLIAMENT] synthesis failed: %s", exc)

        verdict = str(synthesis_data.get("verdict", "abstain")).lower()
        if verdict not in _VALID_VERDICTS:
            verdict = "abstain"

        veto_issued = bool(synthesis_data.get("minority_veto_issued", False))
        veto_reason = str(synthesis_data.get("minority_veto_reason") or "")[:200] or None
        dissent = str(synthesis_data.get("dissent_summary") or "")[:200] or None

        session = ParliamentSession(
            schema_version=1,
            session_id=session_id,
            convened_at=now,
            trigger_reason=reason,
            cycle_id=cycle_id,
            context_summary=ctx_summary,
            delegate_outputs=delegate_outputs,
            synthesis_verdict=verdict,
            synthesis_confidence=float(synthesis_data.get("confidence", 0.5)),
            minority_veto_issued=veto_issued,
            minority_veto_reason=veto_reason,
            dissent_summary=dissent,
            model_used=model,
            total_cost_usd=round(total_cost, 6),
        )
        log_session(session)
        log.info("[PARLIAMENT] Session %s convened (trigger=%s, verdict=%s)",
                 session_id[:8], reason, verdict)
        return session
    except Exception as exc:
        log.warning("[PARLIAMENT] convene_parliament failed: %s", exc)
        return None


def log_session(session: ParliamentSession) -> Optional[str]:
    """Appends to data/annex/internal_parliament/sessions.jsonl. Returns session_id or None."""
    try:
        _ANNEX_DIR.mkdir(parents=True, exist_ok=True)
        with open(_SESSIONS_LOG, "a") as fh:
            fh.write(json.dumps(asdict(session)) + "\n")
        return session.session_id
    except Exception as exc:
        log.warning("[PARLIAMENT] log_session failed: %s", exc)
        return None


def get_sessions(days_back: int = 30) -> list:
    """Reads JSONL. Returns [] on error."""
    results = []
    try:
        if not _SESSIONS_LOG.exists():
            return results
        from datetime import timedelta  # noqa: PLC0415
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        with open(_SESSIONS_LOG) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts = rec.get("convened_at", "")
                    if ts:
                        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if t < cutoff:
                            continue
                    results.append(rec)
                except Exception:
                    continue
    except Exception as exc:
        log.warning("[PARLIAMENT] get_sessions failed: %s", exc)
    return results


def format_session_for_review(session) -> str:
    """Returns markdown summary of one session."""
    try:
        d = asdict(session) if hasattr(session, "__dataclass_fields__") else session
        veto = " ⚠️ MINORITY VETO" if d.get("minority_veto_issued") else ""
        lines = [
            f"**Session {str(d.get('session_id',''))[:8]}** — {d.get('convened_at','')}",
            f"Trigger: {d.get('trigger_reason','')}",
            f"Verdict: **{d.get('synthesis_verdict','')}** (conf={d.get('synthesis_confidence',0):.2f}){veto}",
        ]
        if d.get("minority_veto_reason"):
            lines.append(f"Veto reason: {d['minority_veto_reason']}")
        if d.get("dissent_summary"):
            lines.append(f"Dissent: {d['dissent_summary']}")
        return "\n".join(lines)
    except Exception:
        return ""


def format_parliament_summary_for_review(days_back: int = 7) -> str:
    """Weekly summary: session count, verdict distribution, veto rate."""
    try:
        sessions = get_sessions(days_back=days_back)
        if not sessions:
            return ""
        total = len(sessions)
        verdict_counts: dict = {}
        veto_count = 0
        for s in sessions:
            v = s.get("synthesis_verdict", "unknown")
            verdict_counts[v] = verdict_counts.get(v, 0) + 1
            if s.get("minority_veto_issued"):
                veto_count += 1

        lines = [
            f"## Internal Parliament ({days_back}d)\n",
            f"Sessions: {total} | Veto rate: {veto_count}/{total}",
            "",
            "**Verdict distribution:**",
        ]
        for verdict, count in sorted(verdict_counts.items(), key=lambda x: -x[1]):
            lines.append(f"  - {verdict}: {count}")

        # Show latest session
        last = sessions[-1]
        lines.append(f"\n**Latest:** {format_session_for_review(last)}")
        return "\n".join(lines)
    except Exception as exc:
        log.warning("[PARLIAMENT] format_parliament_summary_for_review failed: %s", exc)
        return ""
