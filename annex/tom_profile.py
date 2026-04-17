# ANNEX MODULE — lab ring, no prod pipeline imports
"""
annex/tom_profile.py — Theory of Mind profile experiment (T6.16).

Evaluation class: exploratory — hypothesis only, not truth.

Builds a structural model of how the bot "thinks" about market participants,
based on its own decision patterns. Explicitly labeled as hypothesis throughout.
Annex-only, never influences prod.

Minimum 50 decisions required for non-abstaining profile.

Storage: data/annex/tom_profile/ — annex namespace only.
Feature flag: enable_tom_profile (lab_flags, default False).
Promotion contract: promotion_contracts/tom_profile_v1.md (DRAFT).

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
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import model_tiering

log = logging.getLogger(__name__)

_ANNEX_DIR = Path("data/annex/tom_profile")
_PROFILES_LOG = _ANNEX_DIR / "profiles.jsonl"
_DECISIONS_PATH = Path("memory/decisions.json")

_TOM_MIN_DECISIONS = 50

_TOM_SYSTEM = (
    "You are analyzing a trading bot's decision patterns to build a theory-of-mind model. "
    "This is explicitly a hypothesis, not truth. "
    "Base ALL claims on the provided decision data. "
    "Do not invent behavior not present in the data. "
    "If data is insufficient, say so. "
    "Output JSON only."
)


# ─────────────────────────────────────────────────────────────────────────────
# Dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ToMProfile:
    schema_version: int = 1
    profile_id: str = ""
    generated_at: str = ""
    days_analyzed: int = 0
    decision_count: int = 0
    inferred_retail_model: str = ""
    inferred_institutional_model: str = ""
    inferred_market_maker_model: str = ""
    signal_trust_ranking: list = field(default_factory=list)
    catalyst_type_preferences: list = field(default_factory=list)
    regime_action_map: dict = field(default_factory=dict)
    confidence: float = 0.0
    sample_adequacy: str = "insufficient"
    is_hypothesis: bool = True
    evaluation_class: str = "exploratory"
    model_used: str = ""
    abstention: Optional[dict] = None


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_enabled() -> bool:
    try:
        from feature_flags import is_enabled  # noqa: PLC0415
        return is_enabled("enable_tom_profile")
    except Exception:
        return False


def _load_decisions(days_back: int) -> list:
    try:
        if not _DECISIONS_PATH.exists():
            return []
        raw = json.loads(_DECISIONS_PATH.read_text())
        decisions = raw if isinstance(raw, list) else raw.get("decisions", [])
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        result = []
        for d in decisions:
            ts = d.get("timestamp", d.get("created_at", ""))
            if ts:
                try:
                    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if t >= cutoff:
                        result.append(d)
                        continue
                except Exception:
                    pass
            result.append(d)
        return result[-500:]
    except Exception as exc:
        log.debug("[TOM] _load_decisions failed: %s", exc)
        return []


def _extract_patterns(decisions: list) -> dict:
    """Extract behavioral patterns from decisions for the ToM prompt."""
    catalyst_counter: Counter = Counter()
    regime_action: dict = {}
    signal_types: Counter = Counter()

    for dec in decisions:
        regime = str(dec.get("regime_view", dec.get("regime", "unknown")) or "unknown")
        ideas = dec.get("ideas", [])
        if ideas:
            actions = [str(i.get("intent", i.get("direction", "")) or "") for i in ideas if isinstance(i, dict)]
            most_common = max(set(actions), key=actions.count) if actions else "hold"
        else:
            most_common = "hold"

        if regime not in regime_action:
            regime_action[regime] = Counter()
        regime_action[regime][most_common] += 1

        for idea in ideas:
            if not isinstance(idea, dict):
                continue
            cat = str(idea.get("catalyst_type", idea.get("catalyst", "")) or "")
            if cat:
                catalyst_counter[cat] += 1
            notes = str(dec.get("notes", "") or "").lower()
            # Infer signal types from notes keywords
            for sig_type in ("momentum", "technical", "macro", "earnings", "insider", "social"):
                if sig_type in notes:
                    signal_types[sig_type] += 1

    # Convert regime_action to most common action per regime
    regime_action_map = {
        regime: counts.most_common(1)[0][0] if counts else "hold"
        for regime, counts in regime_action.items()
    }

    return {
        "top_catalysts": [c for c, _ in catalyst_counter.most_common(5)],
        "regime_action_map": regime_action_map,
        "signal_trust_ranking": [s for s, _ in signal_types.most_common(5)],
    }


def _call_tom_llm(decisions: list, patterns: dict, days_back: int) -> Optional[ToMProfile]:
    try:
        model = model_tiering.get_model_for_module("tom_profile")
        import anthropic  # noqa: PLC0415
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))

        # Summarize decisions for prompt — don't send all 50+
        sample_decisions = decisions[-10:]  # last 10 for recency
        sample_str = json.dumps(sample_decisions, indent=2)[:600]
        patterns_str = json.dumps(patterns, indent=2)

        user_msg = (
            f"Analyzing {len(decisions)} decisions over {days_back} days.\n\n"
            f"Behavioral patterns extracted:\n{patterns_str}\n\n"
            f"Sample of recent decisions (last 10):\n{sample_str}\n\n"
            "Build a theory-of-mind profile. Return JSON:\n"
            "{\n"
            '  "inferred_retail_model": "<one sentence: how this bot models retail behavior>",\n'
            '  "inferred_institutional_model": "<one sentence: institutional>",\n'
            '  "inferred_market_maker_model": "<one sentence: market makers>",\n'
            '  "signal_trust_ranking": ["<signal type 1>", "<signal type 2>", ...],\n'
            '  "catalyst_type_preferences": ["<catalyst 1>", "<catalyst 2>", ...],\n'
            '  "regime_action_map": {"<regime>": "<most common action>"},\n'
            '  "confidence": 0.0-1.0\n'
            "}\n\n"
            "HYPOTHESIS REQUIREMENT: All claims must be grounded in the provided data. "
            'If insufficient data for any field, use "insufficient data" as the value. '
            'If overall data is insufficient, return {"abstention": {"reason": "..."}}.'
        )

        response = client.messages.create(
            model=model,
            max_tokens=500,
            system=_TOM_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )

        raw = response.content[0].text if response.content else "{}"
        input_tok = response.usage.input_tokens
        output_tok = response.usage.output_tokens
        cost = (input_tok * 1.0 + output_tok * 5.0) / 1_000_000

        try:
            import cost_attribution as _ca  # noqa: PLC0415
            _ca.log_spine_record(
                module_name="tom_profile",
                layer_name="annex_experiment",
                ring="lab",
                model=model,
                purpose="tom_profile",
                linked_subject_id="",
                linked_subject_type="tom_analysis",
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
            return ToMProfile(
                schema_version=1,
                profile_id=str(uuid.uuid4()),
                generated_at=now,
                days_analyzed=days_back,
                decision_count=len(decisions),
                abstention=parsed["abstention"],
                sample_adequacy="marginal",
                confidence=0.0,
                is_hypothesis=True,
                model_used=model,
            )

        adequacy = "adequate" if len(decisions) >= 100 else "marginal"

        return ToMProfile(
            schema_version=1,
            profile_id=str(uuid.uuid4()),
            generated_at=now,
            days_analyzed=days_back,
            decision_count=len(decisions),
            inferred_retail_model=str(parsed.get("inferred_retail_model", ""))[:300],
            inferred_institutional_model=str(parsed.get("inferred_institutional_model", ""))[:300],
            inferred_market_maker_model=str(parsed.get("inferred_market_maker_model", ""))[:300],
            signal_trust_ranking=list(parsed.get("signal_trust_ranking", [])) or patterns["signal_trust_ranking"],
            catalyst_type_preferences=list(parsed.get("catalyst_type_preferences", [])) or patterns["top_catalysts"],
            regime_action_map=dict(parsed.get("regime_action_map", {})) or patterns["regime_action_map"],
            confidence=float(parsed.get("confidence", 0.4)),
            sample_adequacy=adequacy,
            is_hypothesis=True,
            model_used=model,
        )
    except Exception as exc:
        log.warning("[TOM] _call_tom_llm failed: %s", exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def build_tom_profile(days_back: int = 30) -> Optional[ToMProfile]:
    """
    Reads memory/decisions.json for past days_back days.
    If fewer than 50 decisions: returns abstaining profile.
    Otherwise: makes one Haiku call to synthesize ToM profile.
    Returns ToMProfile or None. Non-fatal.
    """
    try:
        if not _is_enabled():
            return None

        decisions = _load_decisions(days_back)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        if len(decisions) < _TOM_MIN_DECISIONS:
            return ToMProfile(
                schema_version=1,
                profile_id=str(uuid.uuid4()),
                generated_at=now,
                days_analyzed=days_back,
                decision_count=len(decisions),
                abstention={
                    "reason": (
                        f"insufficient sample: {len(decisions)} decisions < "
                        f"minimum {_TOM_MIN_DECISIONS}"
                    )
                },
                sample_adequacy="insufficient",
                confidence=0.0,
                is_hypothesis=True,
            )

        patterns = _extract_patterns(decisions)
        return _call_tom_llm(decisions, patterns, days_back)
    except Exception as exc:
        log.warning("[TOM] build_tom_profile failed: %s", exc)
        return None


def log_profile(profile: ToMProfile) -> Optional[str]:
    """Appends to data/annex/tom_profile/profiles.jsonl. Returns profile_id or None."""
    try:
        _ANNEX_DIR.mkdir(parents=True, exist_ok=True)
        with open(_PROFILES_LOG, "a") as fh:
            fh.write(json.dumps(asdict(profile)) + "\n")
        return profile.profile_id
    except Exception as exc:
        log.warning("[TOM] log_profile failed: %s", exc)
        return None


def get_latest_profile() -> Optional[ToMProfile]:
    """Returns most recent non-abstaining profile. None if none exist."""
    try:
        if not _PROFILES_LOG.exists():
            return None
        best = None
        with open(_PROFILES_LOG) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    if not d.get("abstention"):
                        best = d
                except Exception:
                    continue
        if best is None:
            return None
        return ToMProfile(
            schema_version=best.get("schema_version", 1),
            profile_id=best.get("profile_id", ""),
            generated_at=best.get("generated_at", ""),
            days_analyzed=best.get("days_analyzed", 0),
            decision_count=best.get("decision_count", 0),
            inferred_retail_model=best.get("inferred_retail_model", ""),
            inferred_institutional_model=best.get("inferred_institutional_model", ""),
            inferred_market_maker_model=best.get("inferred_market_maker_model", ""),
            signal_trust_ranking=best.get("signal_trust_ranking", []),
            catalyst_type_preferences=best.get("catalyst_type_preferences", []),
            regime_action_map=best.get("regime_action_map", {}),
            confidence=float(best.get("confidence", 0.0)),
            sample_adequacy=best.get("sample_adequacy", "insufficient"),
            is_hypothesis=True,
            evaluation_class=best.get("evaluation_class", "exploratory"),
            model_used=best.get("model_used", ""),
        )
    except Exception as exc:
        log.warning("[TOM] get_latest_profile failed: %s", exc)
        return None


def format_profile_for_review() -> str:
    """
    Returns markdown summary of latest non-abstaining profile.
    Includes HYPOTHESIS disclaimer.
    Returns "" on error or no adequate profiles.
    """
    try:
        profile = get_latest_profile()
        if not profile:
            return ""

        lines = [
            "## Theory of Mind Profile (latest)\n",
            "> **HYPOTHESIS — not verified.** Based on bot decision patterns only.\n",
            f"Generated: {profile.generated_at}",
            f"Decisions analyzed: {profile.decision_count} ({profile.days_analyzed}d)",
            f"Sample adequacy: {profile.sample_adequacy}",
            f"Confidence: {profile.confidence:.2f}",
            "",
        ]

        if profile.inferred_retail_model:
            lines.append(f"**Retail model:** {profile.inferred_retail_model}")
        if profile.inferred_institutional_model:
            lines.append(f"**Institutional model:** {profile.inferred_institutional_model}")
        if profile.inferred_market_maker_model:
            lines.append(f"**Market maker model:** {profile.inferred_market_maker_model}")

        if profile.signal_trust_ranking:
            lines.append(f"\n**Signal trust ranking:** {', '.join(profile.signal_trust_ranking[:5])}")

        if profile.catalyst_type_preferences:
            lines.append(f"**Catalyst preferences:** {', '.join(profile.catalyst_type_preferences[:5])}")

        if profile.regime_action_map:
            lines.append("\n**Regime → action map:**")
            for regime, action in list(profile.regime_action_map.items())[:5]:
                lines.append(f"  - {regime}: {action}")

        return "\n".join(lines)
    except Exception as exc:
        log.warning("[TOM] format_profile_for_review failed: %s", exc)
        return ""
