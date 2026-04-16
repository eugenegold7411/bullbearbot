"""
attribution.py — PnL attribution and module ROI tracking.

Tracks which modules were active each cycle, what triggered Sonnet,
and (eventually) which decisions led to profitable outcomes.

All public functions are non-fatal: exceptions are caught and logged
at WARNING level so attribution failures never crash the bot.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

ATTRIBUTION_LOG = Path("data/analytics/attribution_log.jsonl")


# ─────────────────────────────────────────────────────────────────────────────
# Decision ID
# ─────────────────────────────────────────────────────────────────────────────

def generate_decision_id(account: str, timestamp_str: str) -> str:
    """
    Format: dec_{account}_{YYYYMMDD}_{HHMMSS}
    e.g. dec_A1_20260416_093500

    timestamp_str should be in the form "YYYYMMDD_HHMMSS" or any string
    whose first 15 chars (after stripping separators) give YYYYMMDD_HHMMSS.
    """
    clean = (timestamp_str
             .replace("-", "")
             .replace(":", "")
             .replace(" ", "_"))
    return f"dec_{account}_{clean[:15]}"


# ─────────────────────────────────────────────────────────────────────────────
# Module tag builder
# ─────────────────────────────────────────────────────────────────────────────

def build_module_tags(
    session_tier: str,
    gate_reasons: list,
    used_compact: bool,
    gate_skipped: bool,
    scratchpad_result: dict,
    retrieved_memories,          # list[dict] or str — truthy check only
    macro_backdrop_str: str,
    macro_wire_str: str,
    morning_brief,               # dict or str — type-flexible
    insider_section: str,
    reddit_section: str,
    earnings_intel,              # dict or str — type-flexible
    recon_diff,                  # ReconciliationDiff or None
    positions: list,
) -> dict:
    """
    Build module_tags dict — what was active this cycle.
    All values are bool.

    Accepts both dict and str for morning_brief / earnings_intel
    so it works with bot.py's md-string layout without requiring
    a separate file load.
    """
    def _bool_flex(val) -> bool:
        """True if val is a non-empty dict with content OR a str longer than 50 chars."""
        if val is None:
            return False
        if isinstance(val, dict):
            return bool(val)
        return bool(val and len(str(val)) > 50)

    def _has_memories(val) -> bool:
        if isinstance(val, list):
            return len(val) > 0
        return bool(val and len(str(val)) > 20)

    return {
        "regime_classifier":      session_tier in ("market", "extended"),
        "signal_scorer":          session_tier in ("market", "extended"),
        "scratchpad":             bool(scratchpad_result and
                                      scratchpad_result.get("watching")),
        "vector_memory":          _has_memories(retrieved_memories),
        "macro_backdrop":         bool(macro_backdrop_str and
                                      len(macro_backdrop_str) > 50),
        "macro_wire":             bool(macro_wire_str and
                                      len(macro_wire_str) > 50),
        "morning_brief":          _bool_flex(morning_brief),
        "insider_intelligence":   bool(insider_section and
                                      len(insider_section) > 20),
        "reddit_sentiment":       bool(reddit_section and
                                      len(reddit_section) > 20),
        "earnings_intel":         _bool_flex(earnings_intel),
        "portfolio_intelligence": recon_diff is not None,
        "risk_kernel":            True,   # always active after Phase 1
        "sonnet_full":            not used_compact and not gate_skipped,
        "sonnet_compact":         used_compact and not gate_skipped,
        "sonnet_skipped":         gate_skipped,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Trigger flags builder
# ─────────────────────────────────────────────────────────────────────────────

def build_trigger_flags(gate_reasons: list) -> dict:
    """Build trigger_flags dict from gate TriggerReasons."""
    reason_values = set(
        r.value if hasattr(r, "value") else str(r)
        for r in gate_reasons
    )
    triggers = [
        "new_catalyst", "signal_threshold", "regime_change",
        "risk_anomaly", "position_change", "deadline_approaching",
        "scheduled_window", "recon_anomaly", "cooldown_expired",
        "max_skip_exceeded", "hard_override",
    ]
    return {t: (t in reason_values) for t in triggers}


# ─────────────────────────────────────────────────────────────────────────────
# Attribution ledger
# ─────────────────────────────────────────────────────────────────────────────

def log_attribution_event(
    event_type: str,
    decision_id: str,
    account: str,
    symbol: str,
    module_tags: dict,
    trigger_flags: dict,
    pnl_usd: Optional[float] = None,
    pnl_r: Optional[float] = None,
    exit_type: Optional[str] = None,
    trade_id: Optional[str] = None,
    structure_id: Optional[str] = None,
    extra: Optional[dict] = None,
) -> None:
    """
    Append one attribution event to the JSONL log.
    Non-fatal: all exceptions are caught and logged at WARNING.
    """
    try:
        ATTRIBUTION_LOG.parent.mkdir(parents=True, exist_ok=True)
        record: dict = {
            "event_id":      f"attr_{int(time.time() * 1000)}",
            "event_type":    event_type,
            "decision_id":   decision_id,
            "account":       account,
            "symbol":        symbol,
            "timestamp":     datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "module_tags":   module_tags,
            "trigger_flags": trigger_flags,
        }
        if pnl_usd is not None:
            record["pnl_usd"] = pnl_usd
        if pnl_r is not None:
            record["pnl_r"] = pnl_r
        if exit_type:
            record["exit_type"] = exit_type
        if trade_id:
            record["trade_id"] = trade_id
        if structure_id:
            record["structure_id"] = structure_id
        if extra:
            record.update(extra)
        with open(ATTRIBUTION_LOG, "a") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception as exc:  # noqa: BLE001
        log.warning("[ATTR] log_attribution_event failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Attribution summary (for weekly review Agent 1)
# ─────────────────────────────────────────────────────────────────────────────

def get_attribution_summary(days_back: int = 7) -> dict:
    """
    Read attribution_log.jsonl, produce summary dict.
    Returns an empty-but-valid summary if the file is missing or empty.
    """
    _empty: dict = {
        "total_events":      0,
        "total_decisions":   0,
        "total_trades":      0,
        "module_usage_pct":  {},
        "trigger_distribution": {},
        "gate_efficiency": {
            "skip_rate":    0,
            "compact_rate": 0,
            "full_rate":    0,
        },
        "note": "No attribution data yet",
    }

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    events: list[dict] = []

    try:
        if not ATTRIBUTION_LOG.exists():
            return _empty
        with open(ATTRIBUTION_LOG) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts_str = rec.get("timestamp", "")
                    if ts_str:
                        event_dt = datetime.fromisoformat(
                            ts_str.replace("Z", "+00:00")
                        )
                        if event_dt >= cutoff:
                            events.append(rec)
                except Exception:
                    pass
    except Exception:
        return _empty

    if not events:
        return _empty

    decisions = [e for e in events if e["event_type"] == "decision_made"]
    trades    = [e for e in events if e["event_type"] == "order_submitted"]

    if not decisions:
        return {**_empty, "total_events": len(events), "note": "Events present but no decisions yet"}

    # Module usage rates
    module_keys = list(decisions[0].get("module_tags", {}).keys())
    module_usage: dict = {}
    for key in module_keys:
        active = sum(1 for d in decisions if d.get("module_tags", {}).get(key))
        module_usage[key] = round(active / len(decisions), 3)

    # Trigger distribution
    trigger_keys = list(decisions[0].get("trigger_flags", {}).keys())
    trigger_dist: dict = {}
    for key in trigger_keys:
        trigger_dist[key] = sum(
            1 for d in decisions if d.get("trigger_flags", {}).get(key)
        )

    # Gate efficiency
    total   = len(decisions) or 1
    skipped = sum(1 for d in decisions if d.get("module_tags", {}).get("sonnet_skipped"))
    compact = sum(1 for d in decisions if d.get("module_tags", {}).get("sonnet_compact"))
    full    = sum(1 for d in decisions if d.get("module_tags", {}).get("sonnet_full"))

    return {
        "total_events":         len(events),
        "total_decisions":      len(decisions),
        "total_trades":         len(trades),
        "module_usage_pct":     module_usage,
        "trigger_distribution": trigger_dist,
        "gate_efficiency": {
            "skip_rate":    round(skipped / total, 3),
            "compact_rate": round(compact  / total, 3),
            "full_rate":    round(full     / total, 3),
        },
    }


def _emit_spine_record(event: dict, extra: dict) -> None:
    """Best-effort spine adapter. Non-fatal. Called from log_attribution_event()."""
    try:
        from cost_attribution import log_spine_record  # lazy import, avoids circular risk
        module_tags = event.get("module_tags") or {}
        log_spine_record(
            module_name=module_tags.get("module") or event.get("caller") or "unknown",
            layer_name=module_tags.get("layer") or "execution_control",
            ring=module_tags.get("ring") or "prod",
            model=extra.get("model") or "unknown",
            purpose=event.get("event_type") or "unknown",
            linked_subject_id=event.get("decision_id") or None,
            linked_subject_type="decision" if event.get("decision_id") else None,
            input_tokens=extra.get("input_tokens"),
            output_tokens=extra.get("output_tokens"),
            cached_tokens=extra.get("cached_tokens") or extra.get("cache_read_tokens"),
            estimated_cost_usd=extra.get("estimated_cost_usd"),
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("[T0.7] spine adapter failed: %s", e)
