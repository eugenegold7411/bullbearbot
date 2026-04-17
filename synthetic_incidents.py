"""
synthetic_incidents.py — Synthetic incident replay generator (T5.5).

Generates synthetic incidents from known failure mode templates for testing
the full T5 incident pipeline without waiting for real A2 failures.
Emits replay candidates for replay_debugger.py.

No LLM calls. Pure data construction. No spine attribution.
Storage: data/annex/synthetic_incidents/ — annex namespace only.
Synthetic incidents NEVER written to data/analytics/incident_log.jsonl.

Feature flag: enable_synthetic_incidents (lab_flags, default False).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_ANNEX_DIR = Path("data/annex/synthetic_incidents")


# ─────────────────────────────────────────────────────────────────────────────
# Templates
# ─────────────────────────────────────────────────────────────────────────────

SYNTHETIC_INCIDENT_TEMPLATES: dict = {
    "stop_missing": {
        "incident_type": "protection_missing",
        "severity": "warning",
        "description": "Position held without active stop-loss order",
        "account": "account1",
        "suggested_axes": ["model_tier", "prompt_version"],
    },
    "spread_leg2_failed": {
        "incident_type": "a2_spread_abort",
        "severity": "warning",
        "description": "Spread leg 1 filled, leg 2 submission failed",
        "account": "account2",
        "suggested_axes": ["module_overrides"],
    },
    "reconcile_mismatch": {
        "incident_type": "fill_divergence",
        "severity": "warning",
        "description": "Broker position differs from internal state",
        "account": "account1",
        "suggested_axes": ["model_tier"],
    },
    "close_no_fill": {
        "incident_type": "a2_close_failure",
        "severity": "warning",
        "description": "Close order submitted, no fill confirmed",
        "account": "account2",
        "suggested_axes": ["module_overrides", "prompt_version"],
    },
    "stale_structure_state": {
        "incident_type": "a2_orphaned_leg",
        "severity": "critical",
        "description": "Structure state stale vs broker — orphaned leg suspected",
        "account": "account2",
        "suggested_axes": ["model_tier", "module_overrides"],
    },
    "protection_repeated_escalation": {
        "incident_type": "protection_missing",
        "severity": "critical",
        "description": "Same protection gap detected 3+ consecutive cycles",
        "account": "account1",
        "suggested_axes": ["model_tier", "prompt_version", "module_overrides"],
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def generate_synthetic_incident(
    template_name: str,
    symbol: str = "TEST",
    metadata: Optional[dict] = None,
):
    """
    Builds a synthetic IncidentRecord from the named template.
    Does NOT log to incident_log.jsonl — caller decides whether to persist.
    Raises ValueError if template_name not in SYNTHETIC_INCIDENT_TEMPLATES.
    """
    if template_name not in SYNTHETIC_INCIDENT_TEMPLATES:
        raise ValueError(
            f"Unknown template: {template_name!r}. "
            f"Valid templates: {list(SYNTHETIC_INCIDENT_TEMPLATES)}"
        )

    from incident_schema import build_incident  # noqa: PLC0415
    template = SYNTHETIC_INCIDENT_TEMPLATES[template_name]

    extra_meta = {
        "synthetic": True,
        "template": template_name,
        "symbol": symbol,
        **(metadata or {}),
    }

    incident = build_incident(
        incident_type=template["incident_type"],
        account=template["account"],
        severity=template["severity"],
        description=template["description"],
        subject_id=symbol,
        subject_type="synthetic",
        metadata=extra_meta,
    )
    return incident


def generate_replay_candidates(
    template_names: Optional[list] = None,
    count_per_template: int = 1,
) -> list:
    """
    Generates synthetic incidents wrapped as replay candidate dicts.
    Each candidate: {incident, fork_config, suggested_axes}.
    Non-fatal.
    """
    results = []
    try:
        from replay_debugger import ForkConfig  # noqa: PLC0415

        names = template_names or list(SYNTHETIC_INCIDENT_TEMPLATES.keys())
        for template_name in names:
            for _ in range(max(1, count_per_template)):
                try:
                    incident = generate_synthetic_incident(template_name)
                    template = SYNTHETIC_INCIDENT_TEMPLATES[template_name]
                    axes = template.get("suggested_axes", ["model_tier"])

                    fork_cfg = ForkConfig(
                        schema_version=1,
                        model_tier="cheap",
                        prompt_version="v1",
                        fork_axes=axes,
                        label=f"synthetic_{template_name}",
                    )
                    results.append({
                        "incident": incident,
                        "fork_config": fork_cfg,
                        "suggested_axes": axes,
                        "template_name": template_name,
                    })
                except Exception as exc:
                    log.warning("[SYNTHETIC] generate_replay_candidates template=%s: %s", template_name, exc)
    except Exception as exc:
        log.warning("[SYNTHETIC] generate_replay_candidates failed: %s", exc)
    return results


def run_synthetic_replay_suite(template_names: Optional[list] = None) -> list:
    """
    Dry-run: for each template, generates a synthetic incident and validates the
    pipeline accepts it without crashing. No LLM calls.
    Returns list of {template, status: "ok"|"error", error_message}.
    Non-fatal.
    """
    results = []
    names = template_names or list(SYNTHETIC_INCIDENT_TEMPLATES.keys())

    for template_name in names:
        try:
            incident = generate_synthetic_incident(template_name, symbol="REPLAY_TEST")

            # Validate required fields
            assert incident.incident_id, "incident_id missing"
            assert incident.incident_type, "incident_type missing"
            assert incident.account, "account missing"
            assert incident.severity, "severity missing"
            assert incident.detected_at, "detected_at missing"
            assert incident.metadata and incident.metadata.get("synthetic") is True

            # Validate it's an IncidentRecord with schema_version
            assert incident.schema_version == 1

            results.append({
                "template": template_name,
                "status": "ok",
                "error_message": None,
                "incident_id": incident.incident_id,
            })
            log.debug("[SYNTHETIC] template=%s ok incident_id=%s", template_name, incident.incident_id)
        except Exception as exc:
            results.append({
                "template": template_name,
                "status": "error",
                "error_message": str(exc),
                "incident_id": None,
            })
            log.warning("[SYNTHETIC] template=%s FAILED: %s", template_name, exc)

    return results


def format_synthetic_suite_results(results: list) -> str:
    """Returns markdown table of run_synthetic_replay_suite() results."""
    if not results:
        return "## Synthetic Incident Suite\nNo results."

    ok = sum(1 for r in results if r.get("status") == "ok")
    lines = [
        f"## Synthetic Incident Suite Results\n",
        f"Passed: {ok}/{len(results)}\n",
        "| Template | Status | Error |",
        "|----------|--------|-------|",
    ]
    for r in results:
        status = r.get("status", "?")
        error = r.get("error_message") or "—"
        lines.append(f"| {r.get('template', '?')} | {status} | {error} |")
    return "\n".join(lines)
