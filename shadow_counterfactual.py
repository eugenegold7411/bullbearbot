"""
shadow_counterfactual.py — Advisory counterfactual verdict computation.

Reads rejected_by_risk_kernel events from the append-only shadow log,
cross-references decision_outcomes.jsonl for forward-return data, and
assigns one of three verdicts to each eligible event:

    right_to_reject  — rejection was correct (trade would have lost)
    wrong_to_reject  — rejection was wrong (trade would have profited)
    neutral          — trade had no meaningful forward return

Verdicts are written to a PARALLEL file, never mutating the shadow log.
Advisory only — no gating behavior, no module retirement.
Meaningful only when cumulative_accuracy n >= 50.

Parallel verdict log: data/logs/shadow_counterfactual_verdicts.jsonl
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_SHADOW_LOG    = Path("data/analytics/near_miss_log.jsonl")
_VERDICT_LOG   = Path("data/logs/shadow_counterfactual_verdicts.jsonl")
_OUTCOMES_LOG  = Path("data/analytics/decision_outcomes.jsonl")
_MIN_AGE_DAYS  = 5          # events must be at least 5 days old to have a verdict
_ADVISORY_N    = 50         # note advisory-only until this many cumulative verdicts
_ALPHA_THRESH  = 0.003      # return magnitude to call right/wrong (0.3%)


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_already_verdicted() -> set[str]:
    """Return set of event keys (ts+symbol) already in the verdict log."""
    verdicted: set[str] = set()
    try:
        if not _VERDICT_LOG.exists():
            return verdicted
        for line in _VERDICT_LOG.read_text(errors="replace").splitlines():
            try:
                rec = json.loads(line)
                key = rec.get("_event_key", "")
                if key:
                    verdicted.add(key)
            except Exception:
                continue
    except Exception as exc:  # noqa: BLE001
        log.warning("[CF] _load_already_verdicted failed: %s", exc)
    return verdicted


def _event_key(event: dict) -> str:
    return f"{event.get('ts', '')}|{event.get('symbol', '')}"


def _load_outcome_returns() -> dict[tuple[str, str], dict]:
    """
    Build index {(symbol, date_str): outcome_record} from decision_outcomes.jsonl.
    Uses return_1d / correct_1d from submitted records only.
    """
    index: dict[tuple[str, str], dict] = {}
    try:
        if not _OUTCOMES_LOG.exists():
            return index
        for line in _OUTCOMES_LOG.read_text(errors="replace").splitlines():
            try:
                rec = json.loads(line)
                if rec.get("status") != "submitted":
                    continue
                sym  = rec.get("symbol", "")
                ts   = rec.get("timestamp", "")
                date = ts[:10] if ts else ""
                if sym and date and rec.get("return_1d") is not None:
                    index[(sym, date)] = rec
            except Exception:
                continue
    except Exception as exc:  # noqa: BLE001
        log.warning("[CF] _load_outcome_returns failed: %s", exc)
    return index


def _assign_verdict(event: dict, outcome: Optional[dict]) -> Optional[str]:
    """
    Assign right_to_reject / wrong_to_reject / neutral given the shadow event
    and the best available outcome record for that symbol on that date.

    Returns None if we cannot assign a verdict (no outcome data, no direction).
    """
    if outcome is None:
        return None

    return_1d = outcome.get("return_1d")
    correct_1d = outcome.get("correct_1d")
    if return_1d is None:
        return None

    # Determine what direction the rejected signal wanted
    details = event.get("details") or {}
    direction = str(details.get("direction", "") or "").lower()
    # Fallback: infer from action in details
    if not direction:
        action = str(details.get("action", "") or "").lower()
        if action in ("buy", "long"):
            direction = "long"
        elif action in ("sell", "short"):
            direction = "short"

    if not direction:
        # No direction info — cannot classify
        return None

    abs_return = abs(return_1d)
    if abs_return < _ALPHA_THRESH:
        return "neutral"

    # right_to_reject: rejection prevented a loss
    # wrong_to_reject: rejection missed a profit
    if direction == "long":
        if return_1d > _ALPHA_THRESH:
            return "wrong_to_reject"   # trade would have gone up — wrong to block it
        else:
            return "right_to_reject"   # trade would have gone down — good to block it
    elif direction == "short":
        if return_1d < -_ALPHA_THRESH:
            return "wrong_to_reject"   # trade would have gone down (short wins) — wrong to block
        else:
            return "right_to_reject"   # trade would have gone up (short loses) — good to block
    return "neutral"


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def compute_verdicts() -> dict:
    """
    Main entry point. Scans eligible shadow events, assigns verdicts, appends
    to the parallel verdict log.

    Returns summary dict:
        verdicted_new       — new verdicts written this run
        right               — cumulative right_to_reject count
        wrong               — cumulative wrong_to_reject count
        neutral             — cumulative neutral count
        cumulative_accuracy — right / (right + wrong) if > 0, else None
        advisory            — True when cumulative total < _ADVISORY_N
        note                — human-readable advisory note

    Non-fatal: returns an empty-but-valid summary on any error.
    """
    _empty = {
        "verdicted_new": 0,
        "right": 0,
        "wrong": 0,
        "neutral": 0,
        "cumulative_accuracy": None,
        "advisory": True,
        "note": "No data yet",
    }

    try:
        if not _SHADOW_LOG.exists():
            return {**_empty, "note": "shadow log absent"}

        cutoff_age  = (datetime.now(timezone.utc) - timedelta(days=_MIN_AGE_DAYS)).isoformat()
        already     = _load_already_verdicted()
        outcome_idx = _load_outcome_returns()

        eligible: list[dict] = []
        for line in _SHADOW_LOG.read_text(errors="replace").splitlines():
            try:
                ev = json.loads(line)
                if ev.get("event_type") != "rejected_by_risk_kernel":
                    continue
                if ev.get("ts", "") >= cutoff_age:
                    continue  # too recent
                if _event_key(ev) in already:
                    continue  # already verdicted
                eligible.append(ev)
            except Exception:
                continue

        new_records: list[dict] = []
        for ev in eligible:
            sym    = ev.get("symbol", "")
            ts     = ev.get("ts", "")
            date   = ts[:10] if ts else ""
            # Try decision_id match first, then (symbol, date) fallback
            outcome = None
            did = ev.get("decision_id", "")
            if did:
                for rec in (outcome_idx.values()):
                    if rec.get("decision_id") == did:
                        outcome = rec
                        break
            if outcome is None and sym and date:
                outcome = outcome_idx.get((sym, date))

            verdict = _assign_verdict(ev, outcome)
            if verdict is None:
                continue  # skip unevaluable events

            new_records.append({
                "ts":            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "_event_key":    _event_key(ev),
                "event_ts":      ts,
                "symbol":        sym,
                "decision_id":   ev.get("decision_id", ""),
                "session":       ev.get("session", ""),
                "verdict":       verdict,
                "return_1d":     (outcome or {}).get("return_1d"),
                "direction":     str((ev.get("details") or {}).get("direction", "")),
                "reject_reason": str((ev.get("details") or {}).get("reject_reason", "")),
            })

        if new_records:
            _VERDICT_LOG.parent.mkdir(parents=True, exist_ok=True)
            with _VERDICT_LOG.open("a") as fh:
                for r in new_records:
                    fh.write(json.dumps(r) + "\n")
            log.info("[CF] wrote %d new counterfactual verdicts", len(new_records))

        # Load cumulative totals from full verdict log
        right = wrong = neutral = 0
        try:
            if _VERDICT_LOG.exists():
                for line in _VERDICT_LOG.read_text(errors="replace").splitlines():
                    try:
                        r = json.loads(line)
                        v = r.get("verdict", "")
                        if v == "right_to_reject":
                            right += 1
                        elif v == "wrong_to_reject":
                            wrong += 1
                        elif v == "neutral":
                            neutral += 1
                    except Exception:
                        continue
        except Exception:
            pass

        total_decisive = right + wrong
        accuracy = round(right / total_decisive, 3) if total_decisive > 0 else None
        total_all = right + wrong + neutral
        advisory = total_all < _ADVISORY_N

        note = (
            f"Advisory only — n={total_all} < {_ADVISORY_N} required for signal validity"
            if advisory else
            f"n={total_all} verdicts; accuracy={accuracy:.1%} (right/{total_decisive} decisive)"
        )

        return {
            "verdicted_new":       len(new_records),
            "right":               right,
            "wrong":               wrong,
            "neutral":             neutral,
            "cumulative_accuracy": accuracy,
            "advisory":            advisory,
            "note":                note,
        }

    except Exception as exc:  # noqa: BLE001
        log.warning("[CF] compute_verdicts failed: %s", exc)
        return {**_empty, "note": f"compute_verdicts error: {exc}"}
