# ANNEX MODULE — lab ring, no prod pipeline imports
"""
annex/dream_mode.py — Dream Mode experiment (T6.12).

Evaluation class: exploratory — no alpha claim

On-demand only. No automatic schedule. Single Sonnet call with maximum
creative latitude. Human-curated harvesting only. Harvested ideas NEVER
automatically persist to prod memory, strategy_config, or any other prod
artifact. Human-only curation gate.

Uses DEFAULT tier (Sonnet, not Haiku) — the one place where we deliberately
use the stronger model for unconstrained hypothesis generation.

Storage: data/annex/dream_mode/sessions.jsonl — append-only
Feature flag: enable_dream_mode (lab_flags, default False).
Promotion contract: promotion_contracts/dream_mode_v1.md (DRAFT).

Annex sandbox contract:
- No imports from bot.py, scheduler.py, order_executor.py, risk_kernel.py
- No writes to decision objects, strategy_config, execution paths
- Harvested ideas NEVER auto-persist to prod
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

_ANNEX_DIR = Path("data/annex/dream_mode")
_SESSIONS_LOG = _ANNEX_DIR / "sessions.jsonl"

_DREAM_SYSTEM = (
    "You are an unconstrained hypothesis generator for a trading bot. "
    "Given the context provided, generate your most interesting, speculative, "
    "and unconventional hypotheses about market dynamics, bot behavior, "
    "strategy improvements, or anything else that seems worth exploring. "
    "Do not constrain yourself to what is safe or conventional. "
    "Label everything as speculation. Output freeform text."
)

_VALID_STATUSES = {"raw", "harvested", "discarded"}


# ─────────────────────────────────────────────────────────────────────────────
# Dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DreamSession:
    schema_version: int = 1
    session_id: str = ""
    dreamed_at: str = ""
    prompt_context: str = ""
    raw_output: str = ""
    harvested_ideas: list = field(default_factory=list)
    harvest_status: str = "raw"         # "raw" | "harvested" | "discarded"
    harvest_notes: str = ""
    is_hypothesis: bool = True
    evaluation_class: str = "exploratory — no alpha claim"
    model_used: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_enabled() -> bool:
    try:
        from feature_flags import is_enabled  # noqa: PLC0415
        return is_enabled("enable_dream_mode")
    except Exception:
        return False


def _rewrite_session(session_id: str, updates: dict) -> bool:
    """Rewrite a specific session record in the JSONL log."""
    try:
        if not _SESSIONS_LOG.exists():
            return False
        lines = _SESSIONS_LOG.read_text().splitlines()
        updated = False
        new_lines = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get("session_id") == session_id:
                    rec.update(updates)
                    updated = True
                new_lines.append(json.dumps(rec))
            except Exception:
                new_lines.append(line)
        if updated:
            _SESSIONS_LOG.write_text("\n".join(new_lines) + "\n")
        return updated
    except Exception as exc:
        log.warning("[DREAM] _rewrite_session failed: %s", exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def run_dream_session(
    context_summary: str,
    free_prompt: str = "",
    max_tokens: int = 2000,
) -> Optional[DreamSession]:
    """
    Makes one Sonnet call with maximum creative latitude.
    Logs session with harvest_status="raw".
    Returns DreamSession or None. Non-fatal.
    """
    try:
        if not _is_enabled():
            return None

        model = model_tiering.get_model_for_module("dream_mode")
        session_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        import anthropic  # noqa: PLC0415
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))

        user_msg = f"Context:\n{context_summary[:1200]}"
        if free_prompt:
            user_msg += f"\n\nAdditional direction: {free_prompt[:400]}"

        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=_DREAM_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )

        raw_output = response.content[0].text if response.content else ""
        in_tok = response.usage.input_tokens
        out_tok = response.usage.output_tokens
        cost = (in_tok * 3.0 + out_tok * 15.0) / 1_000_000  # Sonnet pricing

        try:
            import cost_attribution as _ca  # noqa: PLC0415
            _ca.log_spine_record(
                module_name="dream_mode",
                layer_name="annex_experiment",
                ring="lab",
                model=model,
                purpose="dream_session",
                linked_subject_id=session_id,
                linked_subject_type="dream_session",
                input_tokens=in_tok,
                output_tokens=out_tok,
                estimated_cost_usd=round(cost, 6),
            )
        except Exception:
            pass

        session = DreamSession(
            schema_version=1,
            session_id=session_id,
            dreamed_at=now,
            prompt_context=context_summary[:500],
            raw_output=raw_output,
            harvested_ideas=[],
            harvest_status="raw",
            harvest_notes="",
            is_hypothesis=True,
            model_used=model,
        )

        _ANNEX_DIR.mkdir(parents=True, exist_ok=True)
        with open(_SESSIONS_LOG, "a") as fh:
            fh.write(json.dumps(asdict(session)) + "\n")

        log.info("[DREAM] Session %s created (raw_output=%d chars)", session_id[:8], len(raw_output))
        return session
    except Exception as exc:
        log.warning("[DREAM] run_dream_session failed: %s", exc)
        return None


def mark_harvested(
    session_id: str,
    harvested_ideas: list,
    harvest_notes: str,
) -> bool:
    """
    Updates an existing session record with human-curated harvest.
    Sets harvest_status="harvested". Atomic write. Returns True on success.
    """
    try:
        return _rewrite_session(session_id, {
            "harvest_status": "harvested",
            "harvested_ideas": harvested_ideas,
            "harvest_notes": harvest_notes,
        })
    except Exception as exc:
        log.warning("[DREAM] mark_harvested failed: %s", exc)
        return False


def mark_discarded(session_id: str) -> bool:
    """Sets harvest_status="discarded". Non-fatal."""
    try:
        return _rewrite_session(session_id, {"harvest_status": "discarded"})
    except Exception as exc:
        log.warning("[DREAM] mark_discarded failed: %s", exc)
        return False


def get_sessions(
    harvest_status: Optional[str] = None,
    limit: int = 10,
) -> list:
    """Reads sessions. Filters by harvest_status. Returns [] on error."""
    results = []
    try:
        if not _SESSIONS_LOG.exists():
            return results
        with open(_SESSIONS_LOG) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if harvest_status and rec.get("harvest_status") != harvest_status:
                        continue
                    results.append(rec)
                except Exception:
                    continue
        return results[-limit:]
    except Exception as exc:
        log.warning("[DREAM] get_sessions failed: %s", exc)
        return []


def format_dream_summary_for_review() -> str:
    """
    Brief summary: sessions run, harvested count, recent harvested ideas.
    Returns "" on error or no sessions.
    """
    try:
        all_sessions = get_sessions(limit=100)
        if not all_sessions:
            return ""

        total = len(all_sessions)
        harvested = [s for s in all_sessions if s.get("harvest_status") == "harvested"]
        discarded = sum(1 for s in all_sessions if s.get("harvest_status") == "discarded")

        lines = [
            "## Dream Mode Sessions\n",
            f"Total: {total} | Harvested: {len(harvested)} | Discarded: {discarded} | Raw: {total - len(harvested) - discarded}",
        ]

        if harvested:
            lines.append("\n**Recent harvested ideas:**")
            for session in harvested[-3:]:
                ideas = session.get("harvested_ideas", [])
                notes = session.get("harvest_notes", "")
                lines.append(f"  Session {str(session.get('session_id',''))[:8]} ({session.get('dreamed_at','')[:10]}):")
                for idea in ideas[:3]:
                    lines.append(f"    - {str(idea)[:120]}")
                if notes:
                    lines.append(f"    Notes: {notes[:100]}")

        return "\n".join(lines)
    except Exception as exc:
        log.warning("[DREAM] format_dream_summary_for_review failed: %s", exc)
        return ""
