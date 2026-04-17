# ANNEX MODULE — lab ring, no prod pipeline imports
"""
annex/self_image_tracker.py — Bot self-image tracker (T6.2).

Evaluation class: quality_positive_non_alpha

Tracks how the bot describes itself in its own reasoning vs what it actually does.
Builds a longitudinal profile of stated identity vs behavioral identity.
No LLM calls. Pure analytics over existing decision data.

Storage: data/annex/self_image_tracker/ — annex namespace only.
Feature flag: enable_self_image_tracker (lab_flags, default False).
Promotion contract: promotion_contracts/self_image_tracker_v1.md (DRAFT).

Annex sandbox contract:
- No imports from bot.py, scheduler.py, order_executor.py, risk_kernel.py
- No writes to decision objects, strategy_config, execution paths
- Outputs include confidence and/or abstention
- Kill-switchable via feature flag
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_ANNEX_DIR = Path("data/annex/self_image_tracker")
_SNAPSHOTS_LOG = _ANNEX_DIR / "snapshots.jsonl"
_DECISIONS_PATH = Path("memory/decisions.json")


# ─────────────────────────────────────────────────────────────────────────────
# Dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SelfImageProfile:
    schema_version: int = 1
    profile_id: str = ""
    snapshot_at: str = ""
    stated_regime_views: dict = field(default_factory=dict)
    stated_convictions: dict = field(default_factory=dict)
    stated_concerns: list = field(default_factory=list)
    actual_hold_rate: float = 0.0
    actual_avg_conviction: float = 0.0
    top_stated_catalysts: list = field(default_factory=list)
    self_description_phrases: list = field(default_factory=list)
    image_drift_flags: list = field(default_factory=list)
    evaluation_class: str = "quality_positive_non_alpha"


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_enabled() -> bool:
    try:
        from feature_flags import is_enabled  # noqa: PLC0415
        return is_enabled("enable_self_image_tracker")
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
        log.debug("[SIT] _load_decisions failed: %s", exc)
        return []


def _extract_phrases(text: str, min_len: int = 4) -> list:
    """Extract short recurring phrases from reasoning text."""
    words = re.findall(r'\b[a-zA-Z]{3,}\b', text.lower())
    two_grams = [f"{words[i]} {words[i+1]}" for i in range(len(words) - 1)]
    return [g for g in two_grams if len(g) >= min_len]


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def build_self_image_snapshot(days_back: int = 7) -> Optional[SelfImageProfile]:
    """
    Read memory/decisions.json for the past days_back days.
    Extract stated vs actual behavior patterns.
    Compare to previous snapshot if available (detects drift).
    Returns SelfImageProfile or None on error.
    """
    try:
        decisions = _load_decisions(days_back)
        if not decisions:
            return None

        # Stated regime views
        regime_counter: Counter = Counter()
        for d in decisions:
            view = d.get("regime_view", d.get("regime", "unknown"))
            if isinstance(view, dict):
                view = view.get("bias", "unknown")
            regime_counter[str(view)] += 1

        # Conviction distribution
        conviction_buckets: dict = {"low": 0, "medium": 0, "high": 0}
        conviction_vals = []
        for d in decisions:
            for idea in d.get("ideas", []):
                if not isinstance(idea, dict):
                    continue
                conv = idea.get("conviction", idea.get("confidence"))
                if conv is not None:
                    try:
                        c = float(conv)
                        conviction_vals.append(c)
                        if c < 0.5:
                            conviction_buckets["low"] += 1
                        elif c < 0.8:
                            conviction_buckets["medium"] += 1
                        else:
                            conviction_buckets["high"] += 1
                    except (ValueError, TypeError):
                        pass

        # Actual hold rate
        has_ideas = sum(1 for d in decisions if d.get("ideas"))
        hold_rate = 1.0 - (has_ideas / len(decisions)) if decisions else 0.0

        # Avg conviction on submitted ideas
        avg_conviction = sum(conviction_vals) / len(conviction_vals) if conviction_vals else 0.0

        # Top catalysts
        catalyst_counter: Counter = Counter()
        for d in decisions:
            for idea in d.get("ideas", []):
                if not isinstance(idea, dict):
                    continue
                cat = idea.get("catalyst_type", idea.get("catalyst", ""))
                if cat:
                    catalyst_counter[str(cat)] += 1
        top_catalysts = [c for c, _ in catalyst_counter.most_common(5)]

        # Top concerns
        concern_counter: Counter = Counter()
        for d in decisions:
            concerns = d.get("concerns", "")
            if isinstance(concerns, str) and concerns:
                for phrase in _extract_phrases(concerns):
                    concern_counter[phrase] += 1
            elif isinstance(concerns, list):
                for c in concerns:
                    if isinstance(c, str):
                        for phrase in _extract_phrases(c):
                            concern_counter[phrase] += 1
        top_concerns = [p for p, _ in concern_counter.most_common(5)]

        # Self-description phrases from reasoning
        phrase_counter: Counter = Counter()
        for d in decisions:
            reasoning = d.get("reasoning", "")
            if isinstance(reasoning, str) and reasoning:
                for phrase in _extract_phrases(reasoning):
                    phrase_counter[phrase] += 1
        top_phrases = [p for p, _ in phrase_counter.most_common(8)]

        # Drift detection vs previous snapshot
        drift_flags = []
        prev = get_latest_snapshot()
        if prev:
            prev_hold_rate = prev.actual_hold_rate
            prev_avg_conviction = prev.actual_avg_conviction
            if abs(hold_rate - prev_hold_rate) > 0.15:
                drift_flags.append(
                    f"hold_rate drifted {prev_hold_rate:.2f}→{hold_rate:.2f}"
                )
            if abs(avg_conviction - prev_avg_conviction) > 0.1:
                drift_flags.append(
                    f"avg_conviction drifted {prev_avg_conviction:.2f}→{avg_conviction:.2f}"
                )
            prev_top_catalyst = prev.top_stated_catalysts[0] if prev.top_stated_catalysts else ""
            new_top_catalyst = top_catalysts[0] if top_catalysts else ""
            if prev_top_catalyst and new_top_catalyst and prev_top_catalyst != new_top_catalyst:
                drift_flags.append(
                    f"top_catalyst changed: {prev_top_catalyst!r}→{new_top_catalyst!r}"
                )

        return SelfImageProfile(
            schema_version=1,
            profile_id=str(uuid.uuid4()),
            snapshot_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            stated_regime_views=dict(regime_counter),
            stated_convictions=conviction_buckets,
            stated_concerns=top_concerns,
            actual_hold_rate=round(hold_rate, 4),
            actual_avg_conviction=round(avg_conviction, 4),
            top_stated_catalysts=top_catalysts,
            self_description_phrases=top_phrases,
            image_drift_flags=drift_flags,
            evaluation_class="quality_positive_non_alpha",
        )
    except Exception as exc:
        log.warning("[SIT] build_self_image_snapshot failed: %s", exc)
        return None


def log_snapshot(profile: SelfImageProfile) -> Optional[str]:
    """Appends to data/annex/self_image_tracker/snapshots.jsonl. Returns profile_id or None."""
    try:
        _ANNEX_DIR.mkdir(parents=True, exist_ok=True)
        with open(_SNAPSHOTS_LOG, "a") as fh:
            fh.write(json.dumps(asdict(profile)) + "\n")
        return profile.profile_id
    except Exception as exc:
        log.warning("[SIT] log_snapshot failed: %s", exc)
        return None


def get_latest_snapshot() -> Optional[SelfImageProfile]:
    """Returns most recent snapshot. None if no snapshots yet."""
    try:
        if not _SNAPSHOTS_LOG.exists():
            return None
        last_line = None
        with open(_SNAPSHOTS_LOG) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    last_line = line
        if not last_line:
            return None
        d = json.loads(last_line)
        return SelfImageProfile(
            schema_version=d.get("schema_version", 1),
            profile_id=d.get("profile_id", ""),
            snapshot_at=d.get("snapshot_at", ""),
            stated_regime_views=d.get("stated_regime_views", {}),
            stated_convictions=d.get("stated_convictions", {}),
            stated_concerns=d.get("stated_concerns", []),
            actual_hold_rate=float(d.get("actual_hold_rate", 0.0)),
            actual_avg_conviction=float(d.get("actual_avg_conviction", 0.0)),
            top_stated_catalysts=d.get("top_stated_catalysts", []),
            self_description_phrases=d.get("self_description_phrases", []),
            image_drift_flags=d.get("image_drift_flags", []),
            evaluation_class=d.get("evaluation_class", "quality_positive_non_alpha"),
        )
    except Exception as exc:
        log.warning("[SIT] get_latest_snapshot failed: %s", exc)
        return None


def format_profile_for_review() -> str:
    """
    Returns markdown summary of latest snapshot.
    Shows: stated vs actual hold rate, conviction distribution,
    top concerns, any detected drifts.
    Returns "" on error or no snapshots.
    """
    try:
        profile = get_latest_snapshot()
        if not profile:
            return ""

        lines = [
            "## Self-Image Profile (latest snapshot)\n",
            f"Snapshot at: {profile.snapshot_at}",
            "",
            f"**Actual hold rate:** {profile.actual_hold_rate:.1%}",
            f"**Avg conviction (submitted trades):** {profile.actual_avg_conviction:.2f}",
            "",
            "**Stated conviction distribution:**",
            f"  - low: {profile.stated_convictions.get('low', 0)}",
            f"  - medium: {profile.stated_convictions.get('medium', 0)}",
            f"  - high: {profile.stated_convictions.get('high', 0)}",
            "",
            "**Top stated regime views:**",
        ]
        for regime, count in sorted(profile.stated_regime_views.items(), key=lambda x: -x[1])[:3]:
            lines.append(f"  - {regime}: {count}")

        if profile.top_stated_catalysts:
            lines.append("")
            lines.append(f"**Top catalysts:** {', '.join(profile.top_stated_catalysts[:5])}")

        if profile.stated_concerns:
            lines.append("")
            lines.append(f"**Top concern phrases:** {', '.join(profile.stated_concerns[:5])}")

        if profile.image_drift_flags:
            lines.append("")
            lines.append("**⚠ Drift flags:**")
            for flag in profile.image_drift_flags:
                lines.append(f"  - {flag}")

        return "\n".join(lines)
    except Exception as exc:
        log.warning("[SIT] format_profile_for_review failed: %s", exc)
        return ""
