"""
cost_attribution.py — Canonical v2 cost attribution spine (T0.7).

Append-only JSONL log at data/analytics/cost_attribution_spine.jsonl.
All new v2 modules call this directly.
Existing modules are wired via an adapter in attribution.py.

Non-fatal everywhere: exceptions are caught and logged at WARNING.
# TODO(T0.6): refactor _is_spine_enabled() to use feature_flags.is_enabled()
#             once Batch 2 load-order audit confirms no circular import risk.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_SPINE_PATH = Path("data/analytics/cost_attribution_spine.jsonl")
# Test-override flag: set to False in tests to suppress spine writes without
# touching strategy_config.json.  Production code must not mutate this.
_SPINE_ENABLED: bool = True

VALID_LAYER_NAMES: frozenset[str] = frozenset({
    "execution_control",
    "semantic_normalization",
    "context_compiler",
    "learning_evaluation",
    "governance_review",
    "shadow_analysis",
    "annex_experiment",
})


# ─────────────────────────────────────────────────────────────────────────────
# Spine record schema
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SpineRecord:
    schema_version: int
    call_id: str
    ts: str
    module_name: str
    layer_name: str
    ring: str
    model: str
    purpose: str
    linked_subject_id: Optional[str]
    linked_subject_type: Optional[str]
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    cached_tokens: Optional[int]
    estimated_cost_usd: Optional[float]


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_spine_enabled() -> bool:
    """
    Read enable_cost_attribution_spine from strategy_config.json on every call.
    Returns False on any read/parse failure so flag toggles take effect immediately
    without requiring a process restart.
    Also respects the module-level _SPINE_ENABLED override for test isolation.
    """
    if not _SPINE_ENABLED:
        return False
    try:
        config = json.loads(Path("strategy_config.json").read_text())
        return bool(
            config.get("feature_flags", {}).get("enable_cost_attribution_spine", False)
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("[SPINE] _is_spine_enabled failed to read config: %s", exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def _rotate_jsonl(path: Path, max_lines: int = 10_000) -> None:
    """Keep only the last max_lines entries. Non-fatal."""
    try:
        lines = path.read_text().splitlines()
        if len(lines) > max_lines:
            path.write_text("\n".join(lines[-max_lines:]) + "\n")
    except Exception:
        pass


def log_spine_record(
    module_name: str,
    layer_name: str,
    ring: str,
    model: str,
    purpose: str,
    *,
    linked_subject_id: Optional[str] = None,
    linked_subject_type: Optional[str] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    cached_tokens: Optional[int] = None,
    estimated_cost_usd: Optional[float] = None,
    call_id: Optional[str] = None,
) -> Optional[str]:
    """
    Append one JSON line to the cost attribution spine JSONL.
    Returns call_id on success, None on failure or when flag is disabled.
    MUST be non-fatal — any exception caught, logged at WARNING.
    """
    try:
        if not _is_spine_enabled():
            return None

        if layer_name not in VALID_LAYER_NAMES:
            log.warning(
                "[SPINE] Unknown layer_name %r — writing record anyway", layer_name
            )

        cid = call_id or str(uuid.uuid4())
        record = SpineRecord(
            schema_version=1,
            call_id=cid,
            ts=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            module_name=module_name,
            layer_name=layer_name,
            ring=ring,
            model=model,
            purpose=purpose,
            linked_subject_id=linked_subject_id,
            linked_subject_type=linked_subject_type,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            estimated_cost_usd=estimated_cost_usd,
        )
        _SPINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_SPINE_PATH, "a") as fh:
            fh.write(json.dumps(asdict(record)) + "\n")
        _rotate_jsonl(_SPINE_PATH)
        return cid

    except Exception as exc:  # noqa: BLE001
        log.warning("[SPINE] log_spine_record failed: %s", exc)
        return None


def get_spine_summary(
    days_back: int = 7,
    group_by: str = "module_name",
) -> dict:
    """
    Read spine JSONL, group by dimension, return aggregated costs.
    Returns {} on any error (non-fatal).
    group_by: "module_name" | "layer_name" | "ring" | "model"
    """
    try:
        if not _SPINE_PATH.exists():
            return {}
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        result: dict = {}
        with open(_SPINE_PATH) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts_str = rec.get("ts", "")
                    if ts_str:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if ts < cutoff:
                            continue
                    key = rec.get(group_by, "unknown")
                    if key not in result:
                        result[key] = {
                            "total_cost_usd": 0.0,
                            "total_calls": 0,
                            "total_input_tokens": 0,
                            "total_output_tokens": 0,
                        }
                    bucket = result[key]
                    bucket["total_calls"] += 1
                    bucket["total_cost_usd"] += float(
                        rec.get("estimated_cost_usd") or 0.0
                    )
                    bucket["total_input_tokens"] += int(rec.get("input_tokens") or 0)
                    bucket["total_output_tokens"] += int(rec.get("output_tokens") or 0)
                except Exception:
                    pass
        return result
    except Exception as exc:  # noqa: BLE001
        log.warning("[SPINE] get_spine_summary failed: %s", exc)
        return {}


_CALL_SITE_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6":         {"input": 3.00,  "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-haiku-4-5-20251001": {"input": 1.00,  "output":  5.00, "cache_read": 0.10, "cache_write": 1.25},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00, "cache_read": 1.50, "cache_write": 18.75},
}


def log_claude_call_to_spine(
    module_name: str,
    model: str,
    purpose: str,
    usage,
    *,
    linked_subject_id: Optional[str] = None,
) -> Optional[str]:
    """
    Extract token counts from a Claude API response usage object and append a
    spine record.  Call this at each Claude API call site immediately after the
    response is received.  Non-fatal — any exception caught and logged.
    """
    try:
        in_tok  = getattr(usage, "input_tokens",               0) or 0
        out_tok = getattr(usage, "output_tokens",              0) or 0
        cache_r = getattr(usage, "cache_read_input_tokens",    0) or 0
        cache_w = getattr(usage, "cache_creation_input_tokens", 0) or 0
        rates   = _CALL_SITE_PRICING.get(model, _CALL_SITE_PRICING["claude-sonnet-4-6"])
        regular = max(0, in_tok - cache_r - cache_w)
        cost_usd = (
            regular  * rates["input"]
            + out_tok * rates["output"]
            + cache_r * rates["cache_read"]
            + cache_w * rates["cache_write"]
        ) / 1_000_000
        return log_spine_record(
            module_name=module_name,
            layer_name="execution_control",
            ring="prod",
            model=model,
            purpose=purpose,
            linked_subject_id=linked_subject_id,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cached_tokens=cache_r,
            estimated_cost_usd=cost_usd,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("[SPINE] log_claude_call_to_spine failed: %s", exc)
        return None


def format_spine_summary_for_review(days_back: int = 7) -> str:
    """
    Return a markdown-formatted cost summary for weekly review injection.
    Shows: cost by ring, cost by layer, top 5 modules by cost, total spend.
    Returns empty string on error.
    """
    try:
        by_module = get_spine_summary(days_back=days_back, group_by="module_name")
        by_layer = get_spine_summary(days_back=days_back, group_by="layer_name")
        by_ring = get_spine_summary(days_back=days_back, group_by="ring")

        if not by_module and not by_layer and not by_ring:
            return ""

        total = sum(v["total_cost_usd"] for v in by_module.values())

        lines = [
            f"## Cost Attribution Spine — last {days_back}d",
            f"**Total spend:** ${total:.4f}",
            "",
            "### By Ring",
        ]
        for ring, stats in sorted(by_ring.items()):
            lines.append(
                f"- {ring}: ${stats['total_cost_usd']:.4f} "
                f"({stats['total_calls']} calls)"
            )

        lines += ["", "### By Layer"]
        for layer, stats in sorted(by_layer.items()):
            lines.append(
                f"- {layer}: ${stats['total_cost_usd']:.4f} "
                f"({stats['total_calls']} calls)"
            )

        lines += ["", "### Top 5 Modules by Cost"]
        top5 = sorted(
            by_module.items(),
            key=lambda kv: kv[1]["total_cost_usd"],
            reverse=True,
        )[:5]
        for mod, stats in top5:
            lines.append(
                f"- {mod}: ${stats['total_cost_usd']:.4f} "
                f"({stats['total_calls']} calls)"
            )

        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001
        log.warning("[SPINE] format_spine_summary_for_review failed: %s", exc)
        return ""
