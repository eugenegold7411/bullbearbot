# SHADOW MODULE — do not import from prod pipeline
"""
replay_debugger.py — Shadow replay fork engine (T4.7).

Allows forking A1 decisions and weekly reviews with alternate model tiers,
prompt versions, or module overrides — without touching production state.

All LLM calls attributed with:
  layer_name="shadow_analysis", ring="shadow", purpose="replay_fork"

Feature flag: enable_replay_fork_debugger (shadow_flags, default=False)
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_REPLAY_LOG = Path("data/analytics/replay_log.jsonl")


# ─────────────────────────────────────────────────────────────────────────────
# Enums and dataclasses
# ─────────────────────────────────────────────────────────────────────────────

class ReplayTarget(str, Enum):
    A1_DECISION        = "a1_decision"
    WEEKLY_REVIEW      = "weekly_review"
    A2_DEBATE          = "a2_debate"          # TODO — not yet implemented
    SIGNAL_SCORER      = "signal_scorer"      # TODO — not yet implemented
    REGIME_CLASSIFIER  = "regime_classifier"  # TODO — not yet implemented


@dataclass
class ForkConfig:
    """
    Specifies the axes along which to fork the replay.
    schema_version=1
    """
    schema_version: int = 1
    model_tier:       Optional[str]  = None   # e.g. "cheap" | "default" | "premium"
    prompt_version:   Optional[str]  = None   # e.g. "v2" (unused until prompt registry)
    module_overrides: dict           = field(default_factory=dict)
    fork_axes:        list[str]      = field(default_factory=list)
    label:            str            = ""


@dataclass
class ReplayResult:
    """
    Output of a single replay fork run.
    schema_version=1
    """
    schema_version:   int        = 1
    result_id:        str        = ""
    target_type:      str        = ""
    target_id:        str        = ""
    fork_config:      dict       = field(default_factory=dict)
    original_output:  Any        = None
    forked_output:    Any        = None
    diff_summary:     str        = ""
    layer_name:       str        = "shadow_analysis"
    ring:             str        = "shadow"
    purpose:          str        = "replay_fork"
    ran_at:           str        = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    model_used:       str        = ""
    input_tokens:     int        = 0
    output_tokens:    int        = 0
    error:            Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Guard: feature flag check
# ─────────────────────────────────────────────────────────────────────────────

def _is_enabled() -> bool:
    try:
        from feature_flags import is_enabled  # noqa: PLC0415
        return is_enabled("enable_replay_fork_debugger", default=False)
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def replay_a1_decision(
    decision_id: str,
    fork_config: ForkConfig,
) -> Optional[ReplayResult]:
    """
    Fork an existing A1 decision through a shadow Claude call.

    Loads the decision record from memory/decisions.json, re-runs it
    with the fork_config overrides, and returns a ReplayResult comparing
    original vs forked output.

    Returns None if feature flag disabled, decision not found, or any error.
    Non-fatal everywhere.
    """
    if not _is_enabled():
        log.debug("[REPLAY] enable_replay_fork_debugger is false — skipping")
        return None

    try:
        import uuid  # noqa: PLC0415
        result_id = f"replay_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

        # Load original decision
        decisions_path = Path("memory/decisions.json")
        if not decisions_path.exists():
            log.debug("[REPLAY] decisions.json not found — cannot replay %s", decision_id)
            return None

        decisions = json.loads(decisions_path.read_text())
        if not isinstance(decisions, list):
            decisions = [decisions]

        original = next(
            (d for d in decisions if d.get("decision_id") == decision_id),
            None,
        )
        if original is None:
            log.debug("[REPLAY] decision_id=%s not found in decisions.json", decision_id)
            return None

        # Resolve model
        model = _resolve_model(fork_config)

        # Build fork prompt from original context
        forked_output, tokens_in, tokens_out = _run_fork_call(
            context=original,
            fork_config=fork_config,
            model=model,
            task_hint="Replay this A1 trading decision with the same market context.",
        )

        diff = format_diff(original.get("reasoning", ""), forked_output.get("reasoning", ""))

        result = ReplayResult(
            result_id=result_id,
            target_type=ReplayTarget.A1_DECISION.value,
            target_id=decision_id,
            fork_config=asdict(fork_config),
            original_output={"reasoning": original.get("reasoning", "")},
            forked_output=forked_output,
            diff_summary=diff,
            model_used=model,
            input_tokens=tokens_in,
            output_tokens=tokens_out,
        )
        log.info("[REPLAY] a1_decision fork complete: %s model=%s", result_id, model)
        return result

    except Exception as exc:
        log.debug("[REPLAY] replay_a1_decision failed (non-fatal): %s", exc)
        return None


def replay_weekly_review(
    review_date: str,
    fork_config: ForkConfig,
    agent_number: int = 6,
) -> Optional[ReplayResult]:
    """
    Fork a weekly review agent's run — Agent 6 (Strategy Director) only.

    Loads the director memo for review_date from data/reports/,
    re-runs the analysis with fork_config overrides, and returns a ReplayResult.

    agent_number must be 6. Other agents return None with a TODO.
    Returns None if feature flag disabled or any error.
    """
    if not _is_enabled():
        log.debug("[REPLAY] enable_replay_fork_debugger is false — skipping")
        return None

    if agent_number != 6:
        log.debug("[REPLAY] replay_weekly_review: only Agent 6 supported (got %d) — TODO", agent_number)
        return None

    try:
        import uuid  # noqa: PLC0415
        result_id = f"replay_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

        # Load original memo
        memo_path = Path("data/reports") / f"weekly_review_{review_date}.md"
        if not memo_path.exists():
            log.debug("[REPLAY] No review found for date=%s at %s", review_date, memo_path)
            return None

        original_text = memo_path.read_text()
        model = _resolve_model(fork_config)

        forked_output, tokens_in, tokens_out = _run_fork_call(
            context={"memo_text": original_text[:3000]},
            fork_config=fork_config,
            model=model,
            task_hint=(
                "You are the Strategy Director (Agent 6). "
                "Given this weekly review memo, produce an alternative strategic assessment. "
                "Respond in JSON with keys: regime_view, key_changes, director_notes."
            ),
        )

        diff = format_diff(original_text[:500], str(forked_output))

        result = ReplayResult(
            result_id=result_id,
            target_type=ReplayTarget.WEEKLY_REVIEW.value,
            target_id=f"agent6_{review_date}",
            fork_config=asdict(fork_config),
            original_output={"memo_preview": original_text[:500]},
            forked_output=forked_output,
            diff_summary=diff,
            model_used=model,
            input_tokens=tokens_in,
            output_tokens=tokens_out,
        )
        log.info("[REPLAY] weekly_review fork complete: %s date=%s model=%s",
                 result_id, review_date, model)
        return result

    except Exception as exc:
        log.debug("[REPLAY] replay_weekly_review failed (non-fatal): %s", exc)
        return None


def log_replay_result(result: ReplayResult) -> Optional[str]:
    """
    Append a ReplayResult to data/analytics/replay_log.jsonl.
    Returns result_id or None on error. Non-fatal.
    """
    try:
        _REPLAY_LOG.parent.mkdir(parents=True, exist_ok=True)
        record = asdict(result)
        # Spine attribution
        try:
            from cost_attribution import log_spine_record  # noqa: PLC0415
            log_spine_record(
                layer_name="shadow_analysis",
                ring="shadow",
                purpose="replay_fork",
                model=result.model_used or "unknown",
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                extra={"result_id": result.result_id, "target_type": result.target_type},
            )
        except Exception:
            pass
        with _REPLAY_LOG.open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        return result.result_id
    except Exception as exc:
        log.debug("[REPLAY] log_replay_result failed (non-fatal): %s", exc)
        return None


def get_replay_results(
    target_type: str,
    target_id: str,
    days_back: int = 30,
) -> list[ReplayResult]:
    """
    Read replay_log.jsonl and return ReplayResult objects matching target_type/target_id.
    Non-fatal — returns [] on error.
    """
    try:
        if not _REPLAY_LOG.exists():
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        results = []
        with _REPLAY_LOG.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("target_type") != target_type:
                    continue
                if target_id and d.get("target_id") != target_id:
                    continue
                try:
                    ts = datetime.fromisoformat(d.get("ran_at", ""))
                    if ts < cutoff:
                        continue
                except (ValueError, TypeError):
                    pass
                try:
                    results.append(ReplayResult(
                        schema_version=d.get("schema_version", 1),
                        result_id=d.get("result_id", ""),
                        target_type=d.get("target_type", ""),
                        target_id=d.get("target_id", ""),
                        fork_config=d.get("fork_config", {}),
                        original_output=d.get("original_output"),
                        forked_output=d.get("forked_output"),
                        diff_summary=d.get("diff_summary", ""),
                        ran_at=d.get("ran_at", ""),
                        model_used=d.get("model_used", ""),
                        input_tokens=d.get("input_tokens", 0),
                        output_tokens=d.get("output_tokens", 0),
                        error=d.get("error"),
                    ))
                except Exception:
                    continue
        return results
    except Exception as exc:
        log.debug("[REPLAY] get_replay_results failed (non-fatal): %s", exc)
        return []


def format_diff(original: Any, forked: Any) -> str:
    """
    Produce a short human-readable diff string between original and forked outputs.
    Used for diff_summary in ReplayResult. Non-fatal.
    """
    try:
        o_str = str(original)[:300]
        f_str = str(forked)[:300]
        if o_str == f_str:
            return "no_diff"
        # Simple token-level overlap estimate
        o_words = set(o_str.lower().split())
        f_words = set(f_str.lower().split())
        overlap = len(o_words & f_words) / max(len(o_words | f_words), 1)
        return f"overlap={overlap:.0%} original_len={len(o_str)} forked_len={len(f_str)}"
    except Exception:
        return "diff_unavailable"


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_model(fork_config: ForkConfig) -> str:
    """Resolve the model string from fork_config, defaulting to Haiku (shadow=cheap)."""
    tier = (fork_config.model_tier or "cheap").lower()
    _tier_model_map = {
        "cheap":   "claude-haiku-4-5-20251001",
        "default": "claude-sonnet-4-6",
        "premium": "claude-opus-4-7",
    }
    return _tier_model_map.get(tier, "claude-haiku-4-5-20251001")


def _run_fork_call(
    context: dict,
    fork_config: ForkConfig,
    model: str,
    task_hint: str,
) -> tuple[Any, int, int]:
    """
    Run a shadow Claude call with the given context. Returns (output_dict, input_tokens, output_tokens).
    Non-fatal — returns ({}, 0, 0) on any failure.
    """
    try:
        import os  # noqa: PLC0415

        from anthropic import Anthropic  # noqa: PLC0415

        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            return {}, 0, 0

        client = Anthropic(api_key=api_key)

        context_text = json.dumps(context, indent=2, default=str)[:4000]
        fork_label = fork_config.label or f"fork/{fork_config.model_tier or 'cheap'}"

        user_msg = (
            f"[SHADOW REPLAY — {fork_label}]\n\n"
            f"Task: {task_hint}\n\n"
            f"Context:\n{context_text}\n\n"
            "Respond with valid JSON only."
        )

        resp = client.messages.create(
            model=model,
            max_tokens=1000,
            system=(
                "You are a shadow replay agent. Conduct an independent analysis of the provided "
                "context. Do not reference production decisions. Respond only with JSON."
            ),
            messages=[{"role": "user", "content": user_msg}],
        )

        raw = resp.content[0].text.strip() if resp.content else ""
        tokens_in = getattr(resp.usage, "input_tokens", 0)
        tokens_out = getattr(resp.usage, "output_tokens", 0)

        try:
            output = json.loads(raw)
        except json.JSONDecodeError:
            output = {"raw": raw[:500]}

        return output, tokens_in, tokens_out

    except Exception as exc:
        log.debug("[REPLAY] _run_fork_call failed (non-fatal): %s", exc)
        return {}, 0, 0
