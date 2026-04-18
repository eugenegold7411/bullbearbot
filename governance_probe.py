"""
governance_probe.py — Governance module availability probe.

Probes a fixed list of governance modules via stdlib importlib.
Writes data/governance/module_availability.json atomically.
Returns availability dict to caller. Never raises.

Called by weekly_review.py before Agent 6 final synthesis.
"""
from __future__ import annotations

import importlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_OUTPUT_DIR  = Path("data/governance")
_OUTPUT_FILE = _OUTPUT_DIR / "module_availability.json"

_GOVERNANCE_MODULES = [
    "model_tiering",
    "feature_flags",
    "cost_attribution",
    "abstention",
    "recommendation_store",
    "recommendation_resolver",
    "incident_schema",
    "hindsight",
    "semantic_labels",
    "versioning",
    "divergence",
    "preflight",
]


def probe_governance_modules() -> dict[str, bool]:
    """
    Attempt to import each governance module. Returns {module_name: available}.
    Never raises.
    """
    results: dict[str, bool] = {}
    for mod in _GOVERNANCE_MODULES:
        try:
            importlib.import_module(mod)
            results[mod] = True
        except Exception as exc:  # noqa: BLE001
            log.warning("[PROBE] %s unavailable: %s", mod, exc)
            results[mod] = False
    return results


def run_governance_probe() -> dict[str, bool]:
    """
    Probe all governance modules and write result atomically.
    Returns the availability dict. Never raises.
    """
    try:
        availability = probe_governance_modules()
        record = {
            "probed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "modules": availability,
            "available_count": sum(availability.values()),
            "total_count": len(availability),
        }
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _OUTPUT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2))
        os.replace(tmp, _OUTPUT_FILE)
        log.info(
            "[PROBE] Governance probe complete: %d/%d available",
            record["available_count"], record["total_count"],
        )
        return availability
    except Exception as exc:  # noqa: BLE001
        log.warning("[PROBE] run_governance_probe failed: %s", exc)
        return {}


def load_module_availability() -> Optional[dict]:
    """Load last probe result. Returns None if file absent or unreadable."""
    try:
        if not _OUTPUT_FILE.exists():
            return None
        return json.loads(_OUTPUT_FILE.read_text())
    except Exception:  # noqa: BLE001
        return None
