"""
decision_outcomes.py — per-decision outcome tracking.

Joins attribution events (decision_made) with execution events (submitted from
trades.jsonl) and forward returns (backtest_latest.json) to produce a per-decision
outcome record.

Output: data/analytics/decision_outcomes.jsonl

Schema per record:
  decision_id       — e.g. "dec_A1_20260416_093500"
  account           — "A1"
  symbol            — canonical (e.g. "AAPL", "BTC/USD")
  timestamp         — UTC ISO-8601
  action            — "buy" | "sell" | "close" | "hold" | etc.
  tier              — "core" | "dynamic" | "intraday" | null
  confidence        — "high" | "medium" | "low" | null
  catalyst          — catalyst string | null
  session           — "market" | "extended" | "overnight" | null
  order_id          — Alpaca order_id | null
  entry_price       — fill price | null  ← gap: ExecutionResult.fill_price not yet
                       added to schemas.py — always None until that gap is closed
  stop_loss         — stop price | null
  take_profit       — take-profit price | null
  status            — "submitted" | "rejected_by_kernel" | "rejected_by_executor"
  reject_reason     — reason string when status != "submitted" | null
  module_tags       — {15-bool dict from attribution} | {}
  trigger_flags     — {11-bool dict from attribution} | {}
  return_1d         — fractional return at +1 trading day | null
  return_3d         — fractional return at +3 trading days | null
  return_5d         — fractional return at +5 trading days | null
  correct_1d        — True/False/null
  correct_3d        — True/False/null
  correct_5d        — True/False/null

Non-fatal everywhere: all public functions catch exceptions and return
empty-but-valid results so that a bad outcome log never affects execution.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

OUTCOMES_LOG   = Path("data/analytics/decision_outcomes.jsonl")
BACKTEST_CACHE = Path("data/reports/backtest_latest.json")


# ─────────────────────────────────────────────────────────────────────────────
# Data contract
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DecisionOutcomeRecord:
    """
    One per decision-symbol pair: an approved trade or a kernel/executor rejection.

    entry_price is always None until ExecutionResult.fill_price is added to
    schemas.ExecutionResult (known gap — see CLAUDE.md).
    """
    decision_id:   str
    account:       str
    symbol:        str
    timestamp:     str                    # UTC ISO-8601
    action:        str
    tier:          Optional[str]  = None
    confidence:    Optional[str]  = None  # "high" | "medium" | "low"
    catalyst:      Optional[str]  = None
    session:       Optional[str]  = None
    order_id:      Optional[str]  = None
    entry_price:   Optional[float] = None
    stop_loss:     Optional[float] = None
    take_profit:   Optional[float] = None
    status:        str             = "submitted"   # submitted | rejected_by_kernel | rejected_by_executor | blocked_by_mode
    reject_reason: Optional[str]  = None
    module_tags:   dict            = None   # type: ignore[assignment]
    trigger_flags: dict            = None   # type: ignore[assignment]
    return_1d:     Optional[float] = None
    return_3d:     Optional[float] = None
    return_5d:     Optional[float] = None
    correct_1d:    Optional[bool]  = None
    correct_3d:    Optional[bool]  = None
    correct_5d:    Optional[bool]  = None
    # T1.6 — alpha classification (alpha_measurement_framework_v1.0.0.md §9)
    alpha_classification:        Optional[str] = None
    alpha_classification_reason: Optional[str] = None
    alpha_classified_at:         Optional[str] = None

    def __post_init__(self) -> None:
        if self.module_tags is None:
            self.module_tags = {}
        if self.trigger_flags is None:
            self.trigger_flags = {}

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DecisionOutcomeRecord":
        return cls(
            decision_id=d.get("decision_id", ""),
            account=d.get("account", ""),
            symbol=d.get("symbol", ""),
            timestamp=d.get("timestamp", ""),
            action=d.get("action", ""),
            tier=d.get("tier"),
            confidence=d.get("confidence"),
            catalyst=d.get("catalyst"),
            session=d.get("session"),
            order_id=d.get("order_id"),
            entry_price=d.get("entry_price"),
            stop_loss=d.get("stop_loss"),
            take_profit=d.get("take_profit"),
            status=d.get("status", "submitted"),
            reject_reason=d.get("reject_reason"),
            module_tags=d.get("module_tags") or {},
            trigger_flags=d.get("trigger_flags") or {},
            return_1d=d.get("return_1d"),
            return_3d=d.get("return_3d"),
            return_5d=d.get("return_5d"),
            correct_1d=d.get("correct_1d"),
            correct_3d=d.get("correct_3d"),
            correct_5d=d.get("correct_5d"),
            alpha_classification=d.get("alpha_classification"),
            alpha_classification_reason=d.get("alpha_classification_reason"),
            alpha_classified_at=d.get("alpha_classified_at"),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Log writer
# ─────────────────────────────────────────────────────────────────────────────

def log_outcome_event(record: DecisionOutcomeRecord) -> None:
    """
    Append one DecisionOutcomeRecord to decision_outcomes.jsonl.
    Non-fatal: exceptions caught and logged at WARNING.
    """
    try:
        OUTCOMES_LOG.parent.mkdir(parents=True, exist_ok=True)
        d = record.to_dict()
        d["_logged_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with open(OUTCOMES_LOG, "a") as fh:
            fh.write(json.dumps(d) + "\n")
        try:
            from cost_attribution import _rotate_jsonl  # noqa: PLC0415
            _rotate_jsonl(OUTCOMES_LOG, max_lines=10_000)
        except Exception:  # noqa: BLE001
            pass
        log.info("[OUTCOMES] logged %s  %s  status=%s", record.decision_id, record.symbol, record.status)
    except Exception as exc:  # noqa: BLE001
        log.warning("[OUTCOMES] log_outcome_event failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Record builder
# ─────────────────────────────────────────────────────────────────────────────

def build_outcome_from_attribution(
    attribution_event: dict,
    execution_event: Optional[dict] = None,
) -> Optional["DecisionOutcomeRecord"]:
    """
    Build a DecisionOutcomeRecord from an attribution decision_made event and
    an optional matching execution event from trades.jsonl.

    attribution_event  — a dict from attribution_log.jsonl with event_type="decision_made"
    execution_event    — matching record from trades.jsonl (status="submitted" or
                         "rejected" from executor), or None for kernel rejections

    Returns None if the attribution_event is malformed.
    Non-fatal: exceptions caught and logged at WARNING.
    """
    try:
        decision_id = attribution_event.get("decision_id", "")
        if not decision_id:
            log.warning("[OUTCOMES] build_outcome_from_attribution: missing decision_id")
            return None

        account    = attribution_event.get("account", "A1")
        symbol     = attribution_event.get("symbol", "portfolio")
        timestamp  = attribution_event.get("timestamp",
                         datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
        module_tags   = attribution_event.get("module_tags", {})
        trigger_flags = attribution_event.get("trigger_flags", {})

        # If we have an execution event, pull its fields
        if execution_event is not None:
            ex_status = execution_event.get("status", "submitted")
            if ex_status == "submitted":
                status = "submitted"
                reject_reason = None
            elif ex_status == "rejected":
                status = "rejected_by_executor"
                reject_reason = execution_event.get("reason")
            else:
                status = ex_status
                reject_reason = execution_event.get("reason")

            # execution_event is a dict from trades.jsonl; read fill_price as a dict key
            entry_price = _maybe_float(execution_event.get("fill_price"))

            action     = execution_event.get("action", "")
            tier       = execution_event.get("tier")
            confidence = execution_event.get("confidence")
            catalyst   = execution_event.get("catalyst")
            session    = execution_event.get("session")
            order_id   = execution_event.get("order_id")
            stop_loss  = _maybe_float(execution_event.get("stop_loss"))
            take_profit = _maybe_float(execution_event.get("take_profit"))
            symbol_ex  = execution_event.get("symbol", symbol)
            # Prefer per-symbol execution record over portfolio-level attribution
            if symbol_ex and symbol_ex != "portfolio":
                symbol = symbol_ex
        else:
            # Kernel rejection path — no execution event
            status        = "rejected_by_kernel"
            reject_reason = None
            entry_price   = None
            action        = ""
            tier          = None
            confidence    = None
            catalyst      = None
            session       = None
            order_id      = None
            stop_loss     = None
            take_profit   = None

        return DecisionOutcomeRecord(
            decision_id=decision_id,
            account=account,
            symbol=symbol,
            timestamp=timestamp,
            action=action,
            tier=tier,
            confidence=confidence,
            catalyst=catalyst,
            session=session,
            order_id=order_id,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            status=status,
            reject_reason=reject_reason,
            module_tags=dict(module_tags),
            trigger_flags=dict(trigger_flags),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("[OUTCOMES] build_outcome_from_attribution failed: %s", exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Forward return backfill
# ─────────────────────────────────────────────────────────────────────────────

def backfill_forward_returns(days_back: int = 30) -> int:
    """
    Read decision_outcomes.jsonl and backfill forward returns for submitted
    trades whose return fields are still null.

    Joins on (symbol, decision_date) using data/reports/backtest_latest.json.
    Rewrites the JSONL in-place — appends updated versions for matched rows.

    Returns the count of records updated.
    Non-fatal: returns 0 on any failure.
    """
    try:
        if not OUTCOMES_LOG.exists():
            return 0
        if not BACKTEST_CACHE.exists():
            log.debug("[OUTCOMES] backfill_forward_returns: no backtest cache at %s", BACKTEST_CACHE)
            return 0

        # Load forward return index: {(symbol, date): result_dict}
        bt_index: dict[tuple[str, str], dict] = {}
        try:
            bt_data = json.loads(BACKTEST_CACHE.read_text())
            # T-025: backtest may have status=insufficient_data (< MIN_SIGNALS=5) with no results key
            bt_status = bt_data.get("status", "")
            if bt_status == "insufficient_data":
                n   = bt_data.get("n_signals", 0)
                req = bt_data.get("min_required", 5)
                log.info(
                    "[OUTCOMES] backfill: backtest insufficient_data "
                    "(%d signals < %d required) — no forward returns available",
                    n, req,
                )
                return 0
            for r in bt_data.get("results", []):
                sym  = r.get("symbol", "")
                dstr = r.get("decision_date", "")[:10]   # "YYYY-MM-DD"
                if sym and dstr:
                    bt_index[(sym, dstr)] = r
        except Exception as exc:  # noqa: BLE001
            log.warning("[OUTCOMES] backfill: failed to load backtest cache: %s", exc)
            return 0

        if not bt_index:
            return 0

        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        records: list[dict] = []
        updated = 0

        with open(OUTCOMES_LOG) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    records.append({})   # keep malformed lines as-is (empty placeholder)
                    continue

                # Only backfill submitted trades where returns are missing
                if (rec.get("status") == "submitted"
                        and rec.get("return_1d") is None
                        and rec.get("symbol")):
                    ts_str   = rec.get("timestamp", "")
                    date_str = ts_str[:10] if ts_str else ""
                    key      = (rec["symbol"], date_str)
                    bt_rec   = bt_index.get(key)
                    if bt_rec is not None:
                        # Check record is within days_back window
                        try:
                            rec_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                            if rec_dt >= cutoff:
                                rec["return_1d"]  = bt_rec.get("return_1d")
                                rec["return_3d"]  = bt_rec.get("return_3d")
                                rec["return_5d"]  = bt_rec.get("return_5d")
                                rec["correct_1d"] = bt_rec.get("correct_1d")
                                rec["correct_3d"] = bt_rec.get("correct_3d")
                                rec["correct_5d"] = bt_rec.get("correct_5d")
                                updated += 1
                                # Mirror resolved outcome to ChromaDB vector store
                                if (rec.get("correct_1d") is not None
                                        and rec.get("return_1d") is not None
                                        and rec.get("decision_id")):
                                    _update_chroma_outcome(
                                        rec["decision_id"],
                                        bool(rec["correct_1d"]),
                                        float(rec["return_1d"]),
                                    )
                        except Exception:
                            pass
                records.append(rec)

        if updated > 0:
            # Rewrite JSONL with updated records
            lines = []
            for rec in records:
                if rec:
                    try:
                        lines.append(json.dumps(rec))
                    except Exception:
                        pass
            OUTCOMES_LOG.write_text("\n".join(lines) + ("\n" if lines else ""))
            log.info("[OUTCOMES] backfill_forward_returns: updated %d records", updated)

        return updated
    except Exception as exc:  # noqa: BLE001
        log.warning("[OUTCOMES] backfill_forward_returns failed: %s", exc)
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# Summary aggregation
# ─────────────────────────────────────────────────────────────────────────────

def generate_outcomes_summary(days_back: int = 7) -> dict:
    """
    Aggregate decision_outcomes.jsonl over the last `days_back` days.

    Returns an empty-but-valid summary dict if the file is missing or empty.
    Non-fatal: exceptions caught and logged at WARNING.
    """
    _empty: dict = {
        "total_decisions":     0,
        "submitted":           0,
        "rejected_by_kernel":  0,
        "rejected_by_executor": 0,
        "blocked_by_mode":     0,
        "with_returns":        0,
        "win_rate_1d":         None,
        "win_rate_3d":         None,
        "win_rate_5d":         None,
        "avg_return_1d":       None,
        "avg_return_3d":       None,
        "avg_return_5d":       None,
        "top_catalysts":       [],
        "module_submit_rates": {},
        "note": "No outcome data yet",
    }

    try:
        if not OUTCOMES_LOG.exists():
            return _empty

        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        records: list[dict] = []

        with open(OUTCOMES_LOG) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts_str = rec.get("timestamp", "")
                    if ts_str:
                        rec_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if rec_dt >= cutoff:
                            records.append(rec)
                except Exception:
                    pass

        if not records:
            return _empty

        submitted   = [r for r in records if r.get("status") == "submitted"]
        k_rejected  = [r for r in records if r.get("status") == "rejected_by_kernel"]
        ex_rejected = [r for r in records if r.get("status") == "rejected_by_executor"]
        mode_blocked = [r for r in records if r.get("status") == "blocked_by_mode"]

        # Forward returns (submitted trades with data)
        with_returns = [r for r in submitted if r.get("return_1d") is not None]

        def _win_rate(recs: list[dict], field: str) -> Optional[float]:
            vals = [r.get(field) for r in recs if r.get(field) is not None]
            if not vals:
                return None
            wins = sum(1 for v in vals if v)
            return round(wins / len(vals), 3)

        def _avg_return(recs: list[dict], field: str) -> Optional[float]:
            vals = [r.get(field) for r in recs if r.get(field) is not None]
            if not vals:
                return None
            return round(sum(vals) / len(vals), 5)

        # Top catalysts (submitted trades only)
        from collections import Counter
        catalysts = [r.get("catalyst", "") for r in submitted if r.get("catalyst")]
        top_catalysts = [c for c, _ in Counter(catalysts).most_common(5)]

        # Module → submit rate (what % of decisions with this module active led to submit)
        module_submit: dict = {}
        if records:
            all_modules = set()
            for r in records:
                all_modules.update(r.get("module_tags", {}).keys())
            for mod in all_modules:
                active_recs = [r for r in records if r.get("module_tags", {}).get(mod)]
                if active_recs:
                    submitted_active = sum(
                        1 for r in active_recs if r.get("status") == "submitted"
                    )
                    module_submit[mod] = round(submitted_active / len(active_recs), 3)

        return {
            "total_decisions":      len(records),
            "submitted":            len(submitted),
            "rejected_by_kernel":   len(k_rejected),
            "rejected_by_executor": len(ex_rejected),
            "blocked_by_mode":      len(mode_blocked),
            "with_returns":         len(with_returns),
            "win_rate_1d":          _win_rate(with_returns, "correct_1d"),
            "win_rate_3d":          _win_rate(with_returns, "correct_3d"),
            "win_rate_5d":          _win_rate(with_returns, "correct_5d"),
            "avg_return_1d":        _avg_return(with_returns, "return_1d"),
            "avg_return_3d":        _avg_return(with_returns, "return_3d"),
            "avg_return_5d":        _avg_return(with_returns, "return_5d"),
            "top_catalysts":        top_catalysts,
            "module_submit_rates":  module_submit,
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("[OUTCOMES] generate_outcomes_summary failed: %s", exc)
        return _empty


# ─────────────────────────────────────────────────────────────────────────────
# Report formatter (for weekly review Agent 4)
# ─────────────────────────────────────────────────────────────────────────────

def format_outcomes_report(summary: dict) -> str:
    """
    Format a generate_outcomes_summary() result as a markdown section
    for weekly review Agent 4 input.

    Returns a compact string. Never raises.
    """
    try:
        if summary.get("total_decisions", 0) == 0:
            return "**Decision Outcomes:** No outcome data in reporting window.\n"

        lines = [
            "**Decision Outcomes (7-day)**",
            f"- Total decisions logged: {summary['total_decisions']}",
            f"- Submitted to Alpaca: {summary['submitted']}",
            f"- Rejected by risk kernel: {summary['rejected_by_kernel']}",
            f"- Rejected by executor: {summary['rejected_by_executor']}",
            f"- Blocked by mode/preflight: {summary.get('blocked_by_mode', 0)}",
            f"- With forward return data: {summary['with_returns']}",
        ]

        if summary.get("win_rate_1d") is not None:
            lines.append(
                f"- Win rate: +1d={summary['win_rate_1d']:.1%}  "
                f"+3d={summary.get('win_rate_3d', 0) or 0:.1%}  "
                f"+5d={summary.get('win_rate_5d', 0) or 0:.1%}"
            )
        if summary.get("avg_return_1d") is not None:
            lines.append(
                f"- Avg return: +1d={summary['avg_return_1d']:.3%}  "
                f"+3d={summary.get('avg_return_3d', 0) or 0:.3%}  "
                f"+5d={summary.get('avg_return_5d', 0) or 0:.3%}"
            )

        if summary.get("top_catalysts"):
            lines.append(f"- Top catalysts: {', '.join(summary['top_catalysts'])}")

        if summary.get("note"):
            lines.append(f"- Note: {summary['note']}")

        return "\n".join(lines) + "\n"
    except Exception as exc:  # noqa: BLE001
        log.warning("[OUTCOMES] format_outcomes_report failed: %s", exc)
        return "**Decision Outcomes:** Report formatting error.\n"


# ─────────────────────────────────────────────────────────────────────────────
# Alpha classification (alpha_measurement_framework_v1.0.0.md)
# ─────────────────────────────────────────────────────────────────────────────

def classify_alpha(record: DecisionOutcomeRecord) -> str:
    """
    Apply alpha_measurement_framework_v1.0.0.md §9 classification rules.

    Returns one of 7 classifications from ALPHA_CLASSIFICATIONS.
    Returns "insufficient_sample" when:
    - forward_return_1d is None (no outcome data yet)
    - record is less than 1 trading day old (< 24h)

    Never raises.
    """
    try:
        # No outcome data or too recent → insufficient sample
        if record.return_1d is None:
            return "insufficient_sample"

        # Age check: < 24h old → insufficient sample
        try:
            ts = datetime.fromisoformat(record.timestamp.replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
            if age_hours < 24:
                return "insufficient_sample"
        except Exception:
            return "insufficient_sample"

        # Only classify submitted trades (not rejections)
        if record.status != "submitted":
            return "quality_positive_non_alpha"

        # Alpha classification based on 1d forward return direction
        correct_1d = record.correct_1d
        return_1d = record.return_1d

        if correct_1d is True and return_1d is not None and return_1d > 0.003:
            return "alpha_positive"
        elif correct_1d is False and return_1d is not None and return_1d < -0.003:
            return "alpha_negative"
        elif correct_1d is not None:
            return "alpha_neutral"
        else:
            return "insufficient_sample"

    except Exception as exc:  # noqa: BLE001
        log.warning("[OUTCOMES] classify_alpha failed: %s", exc)
        return "insufficient_sample"


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _maybe_float(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# ChromaDB outcome bridge
# ─────────────────────────────────────────────────────────────────────────────

_DECISIONS_FILE = Path("memory/decisions.json")

def _build_decision_to_vector_lookup() -> dict[str, str]:
    """
    Read memory/decisions.json and return {decision_id: vector_id} mapping.

    Used by _update_chroma_outcome() to bridge attribution IDs to ChromaDB IDs.
    Returns {} on any failure — always non-fatal.
    """
    try:
        if not _DECISIONS_FILE.exists():
            return {}
        records = json.loads(_DECISIONS_FILE.read_text())
        return {
            r["decision_id"]: r["vector_id"]
            for r in records
            if r.get("decision_id") and r.get("vector_id")
        }
    except Exception as exc:
        log.debug("[OUTCOMES] _build_decision_to_vector_lookup failed: %s", exc)
        return {}


def _update_chroma_outcome(decision_id: str, correct: bool, return_val: float) -> None:
    """
    Update the ChromaDB vector record's outcome + pnl when a forward return is resolved.

    Looks up decision_id → vector_id in memory/decisions.json, then calls
    trade_memory.update_trade_outcome(). Non-fatal.
    """
    try:
        import trade_memory as tm  # noqa: PLC0415
        lookup = _build_decision_to_vector_lookup()
        vector_id = lookup.get(decision_id)
        if not vector_id:
            return
        outcome = "win" if correct else "loss"
        tm.update_trade_outcome(vector_id, outcome, float(return_val))
        log.debug(
            "[OUTCOMES] ChromaDB outcome updated: %s → %s outcome=%s pnl=%.4f",
            decision_id, vector_id, outcome, return_val,
        )
    except Exception as exc:
        log.debug("[OUTCOMES] _update_chroma_outcome failed (non-fatal): %s", exc)
