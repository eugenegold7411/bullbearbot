"""
shadow_lane.py — counterfactual decision logging (shadow lane).

Records what would have happened with rejected/near-miss signals.
Completely non-fatal: every function has a top-level try/except.
Zero execution side effects — logs decisions only, never triggers orders.

Event types:
    approved_trade              — signal passed risk kernel, order submitted
    rejected_by_risk_kernel     — risk_kernel.process_idea() returned a string rejection
    rejected_by_policy          — blocked by policy gate before kernel (PDT, session, etc.)
    below_threshold_near_miss   — signal scored but below min_confidence threshold
    interesting_but_not_actioned — scored high, gate fired but Sonnet chose not to act
    timing_miss                 — valid signal but wrong session for the instrument
    structure_rejected          — options structure failed debate or liquidity gate
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from log_setup import get_logger

log = get_logger(__name__)

BASE_DIR      = Path(__file__).parent
NEAR_MISS_LOG = BASE_DIR / "data" / "analytics" / "near_miss_log.jsonl"

SHADOW_EVENT_TYPES = {
    "approved_trade",
    "rejected_by_risk_kernel",
    "rejected_by_policy",
    "below_threshold_near_miss",
    "interesting_but_not_actioned",
    "timing_miss",
    "structure_rejected",
}


def log_shadow_event(
    event_type: str,
    symbol: str,
    details: Optional[dict] = None,
    decision_id: str = "",
    session: str = "",
) -> None:
    """
    Append one shadow event record to near_miss_log.jsonl.

    Completely non-fatal — any failure is logged at DEBUG and swallowed.
    The log file and parent directory are created on first write.

    Args:
        event_type:  One of SHADOW_EVENT_TYPES.
        symbol:      Ticker symbol (e.g. "AAPL", "BTC/USD").
        details:     Free-form dict with signal score, rejection reason, etc.
        decision_id: Attribution decision ID (may be "" at kernel time — known limitation;
                     fix in future by moving ID generation earlier in run_cycle()).
        session:     Market session string ("market", "extended", "overnight").
    """
    try:
        if event_type not in SHADOW_EVENT_TYPES:
            log.debug("[SHADOW] Unknown event_type=%r — skipping", event_type)
            return

        record = {
            "ts":          datetime.now(timezone.utc).isoformat(),
            "event_type":  event_type,
            "symbol":      symbol,
            "decision_id": decision_id,
            "session":     session,
            "details":     details or {},
        }

        NEAR_MISS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with NEAR_MISS_LOG.open("a") as fh:
            fh.write(json.dumps(record) + "\n")

        log.debug(
            "[SHADOW] %s %s decision_id=%r",
            event_type, symbol, decision_id or "(pending)",
        )

    except Exception as exc:
        log.debug("[SHADOW] log_shadow_event failed (non-fatal): %s", exc)


def get_shadow_stats(lookback_days: int = 7) -> dict:
    """
    Read near_miss_log.jsonl and compute aggregate stats for the last N days.

    Returns {} on any failure — completely non-fatal.

    Returns dict with keys:
        status             "ok" | "no_log"
        lookback_days
        events             total events in window
        by_type            {event_type: count}
        top_symbols        [{symbol, count}] top 10 by frequency
        approved_trades    int
        kernel_rejections  int
        policy_rejections  int
        near_misses        int
    """
    try:
        if not NEAR_MISS_LOG.exists():
            return {"status": "no_log", "events": 0}

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=lookback_days)
        ).isoformat()

        events: list[dict] = []
        with NEAR_MISS_LOG.open() as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("ts", "") >= cutoff:
                        events.append(rec)
                except json.JSONDecodeError:
                    continue

        by_type:   dict[str, int] = {}
        by_symbol: dict[str, int] = {}

        for ev in events:
            et  = ev.get("event_type", "unknown")
            sym = ev.get("symbol",     "unknown")
            by_type[et]   = by_type.get(et,   0) + 1
            by_symbol[sym] = by_symbol.get(sym, 0) + 1

        top_rejected = sorted(by_symbol.items(), key=lambda x: -x[1])[:10]

        return {
            "status":           "ok",
            "lookback_days":    lookback_days,
            "events":           len(events),
            "by_type":          by_type,
            "top_symbols":      [{"symbol": s, "count": c} for s, c in top_rejected],
            "approved_trades":  by_type.get("approved_trade",         0),
            "kernel_rejections": by_type.get("rejected_by_risk_kernel", 0),
            "policy_rejections": by_type.get("rejected_by_policy",      0),
            "near_misses":      by_type.get("below_threshold_near_miss", 0),
        }

    except Exception as exc:
        log.debug("[SHADOW] get_shadow_stats failed (non-fatal): %s", exc)
        return {}
