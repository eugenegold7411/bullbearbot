"""
divergence_summarizer.py — Divergence incident summarizer (T2.6).

Clusters divergence incidents and produces root-cause summaries via single
Haiku call. Feature flag: enable_divergence_summarizer (default True).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import feature_flags
import model_tiering

log = logging.getLogger(__name__)

_INCIDENT_LOG = Path("data/analytics/incident_log.jsonl")

_SYSTEM_PROMPT = (
    "You are a trading system reliability analyst. Given a list of operational "
    "incidents, identify root-cause clusters and recommended actions. "
    "Respond ONLY with valid JSON. Keep responses concise — under 400 tokens."
)


def _load_incidents(days_back: int = 7) -> list[dict]:
    """Load divergence-type incidents from incident_log.jsonl."""
    try:
        if not _INCIDENT_LOG.exists():
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        results = []
        for line in _INCIDENT_LOG.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                ts_str = d.get("detected_at", "")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if ts < cutoff:
                            continue
                    except Exception:
                        pass
                results.append(d)
            except Exception:
                continue
        return results
    except Exception:
        return []


def _cluster_incidents(incidents: list[dict]) -> dict:
    """Group incidents by severity and type."""
    clusters: dict = {}
    for inc in incidents:
        key = f"{inc.get('severity','unknown')}:{inc.get('incident_type','unknown')}"
        if key not in clusters:
            clusters[key] = {"severity": inc.get("severity"), "incident_type": inc.get("incident_type"), "count": 0, "examples": []}
        clusters[key]["count"] += 1
        if len(clusters[key]["examples"]) < 2:
            clusters[key]["examples"].append(inc.get("description", "")[:100])
    return clusters


def _call_haiku(clusters: dict, model: str) -> dict:
    """Single Haiku call for root-cause narrative. Raises on failure."""
    import anthropic  # noqa: PLC0415

    import cost_attribution as _ca  # noqa: PLC0415

    cluster_list = list(clusters.values())
    prompt = (
        f"Analyze {len(cluster_list)} incident cluster(s):\n"
        + json.dumps(cluster_list, indent=2)[:1500]
        + '\n\nRespond with JSON: {"clusters":[{"type":"...","severity":"...","count":0,"root_cause":"...","recommendation":"..."}],"top_root_cause":"...","overall_recommendation":"..."}'
    )

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model,
        max_tokens=512,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    input_tokens = getattr(resp.usage, "input_tokens", None)
    output_tokens = getattr(resp.usage, "output_tokens", None)
    est_cost = None
    if input_tokens and output_tokens:
        est_cost = (input_tokens / 1_000_000 * 1.00) + (output_tokens / 1_000_000 * 5.00)

    _ca.log_spine_record(
        module_name="divergence_summarizer",
        layer_name="governance_review",
        ring="prod",
        model=model,
        purpose="incident_summary",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_usd=est_cost,
    )

    content = resp.content[0].text.strip()
    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
    return json.loads(content)


def summarize_divergence_incidents(
    days_back: int = 7,
    min_incidents: int = 2,
) -> Optional[dict]:
    """
    Clusters incidents and produces root-cause summary via Haiku.
    Returns None if fewer than min_incidents or on any failure.
    """
    if not feature_flags.is_enabled("enable_divergence_summarizer"):
        return None
    try:
        incidents = _load_incidents(days_back=days_back)
        if len(incidents) < min_incidents:
            return None

        clusters = _cluster_incidents(incidents)
        model = model_tiering.get_model_for_module("divergence_summarizer")

        try:
            llm_resp = _call_haiku(clusters, model)
        except Exception as _llm_exc:
            log.warning("[DIV_SUMM] LLM call failed: %s", _llm_exc)
            # Return structural summary without root-cause narrative
            return {
                "clusters": list(clusters.values()),
                "root_causes": [],
                "recommendations": [],
                "total_incidents": len(incidents),
                "model_used": model,
                "llm_error": str(_llm_exc),
            }

        return {
            "clusters": llm_resp.get("clusters", list(clusters.values())),
            "root_causes": [llm_resp.get("top_root_cause", "")],
            "recommendations": [llm_resp.get("overall_recommendation", "")],
            "total_incidents": len(incidents),
            "model_used": model,
        }

    except Exception as exc:  # noqa: BLE001
        log.warning("[DIV_SUMM] summarize_divergence_incidents failed: %s", exc)
        return None


def format_divergence_summary_for_review(days_back: int = 7) -> str:
    """
    Returns markdown summary for CTO (Agent 5) and Compliance (Agent 10).
    Returns '' on no incidents or error.
    """
    if not feature_flags.is_enabled("enable_divergence_summarizer"):
        return ""
    try:
        incidents = _load_incidents(days_back=days_back)
        if not incidents:
            return ""

        severity_counts: dict[str, int] = {}
        type_counts: dict[str, int] = {}
        for inc in incidents:
            sev = inc.get("severity", "unknown")
            itype = inc.get("incident_type", "unknown")
            severity_counts[sev] = severity_counts.get(sev, 0) + 1
            type_counts[itype] = type_counts.get(itype, 0) + 1

        lines = [f"### Divergence Incident Summary (last {days_back}d)", ""]
        lines.append(f"Total incidents: {len(incidents)}")
        lines.append("By severity: " + ", ".join(f"{k}={v}" for k, v in sorted(severity_counts.items())))
        lines.append("By type: " + ", ".join(f"{k}={v}" for k, v in sorted(type_counts.items(), key=lambda x: -x[1])[:5]))

        # Add LLM root-cause if we have enough
        if len(incidents) >= 2:
            summary = summarize_divergence_incidents(days_back=days_back)
            if summary:
                rc = (summary.get("root_causes") or [""])[0]
                rec = (summary.get("recommendations") or [""])[0]
                if rc:
                    lines.append(f"\nRoot cause: {rc}")
                if rec:
                    lines.append(f"Recommendation: {rec}")

        return "\n".join(lines)

    except Exception as exc:
        log.warning("[DIV_SUMM] format_divergence_summary_for_review failed: %s", exc)
        return ""
