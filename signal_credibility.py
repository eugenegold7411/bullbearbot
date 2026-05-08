"""
signal_credibility.py — Credibility tracking for signal-source modules (S2 Fix 4).

Parallel to shadow_governance.py but distinct in purpose:

  shadow_governance.advisor_credibility.json
    → 6 shadow REVIEW modules (context_compiler, forensic_reviewer, …).
      Updated from HindsightRecord verdicts. Asks "is this advisor's
      retrospective judgment correct?"

  signal_credibility.signal_source_credibility.json   ← THIS MODULE
    → N signal SOURCE modules (insider_intelligence, macro_wire,
      reddit_sentiment, qualitative_context, …) keyed on the module_tags
      field of decision_outcomes records. Asks "when this data input
      was present in the decision, did the trade earn alpha?"

Both cohorts coexist; neither overwrites the other.

Storage: data/analytics/signal_source_credibility.json (atomic writes).

Sample thresholds:
    insufficient_sample : sample_count < 5
    provisional         : 5 <= sample_count < 10
    active              : sample_count >= 10

Verdict mapping from alpha_classification:
    alpha_positive  → confirmed     (delta = +1)
    alpha_negative  → refuted       (delta =  0)
    alpha_neutral   → inconclusive  (no calibration update; sample still counts)
    insufficient_sample / other → ignored (no update)
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_CREDIBILITY_PATH = Path("data/analytics/signal_source_credibility.json")
PROVISIONAL_THRESHOLD = 5
ACTIVE_THRESHOLD = 10

# Default cohort — derived from the 15-bool module_tags emitted by
# decision_outcomes.build_outcome_from_attribution. Keep in sync with that
# schema; new tags auto-initialize on first write.
_DEFAULT_SOURCES = [
    "regime_classifier",
    "signal_scorer",
    "scratchpad",
    "vector_memory",
    "macro_backdrop",
    "macro_wire",
    "morning_brief",
    "insider_intelligence",
    "reddit_sentiment",
    "earnings_intel",
    "portfolio_intelligence",
    "risk_kernel",
    "qualitative_context",
]


@dataclass
class SignalSourceCredibility:
    schema_version: int = 1
    source_name: str = ""
    contribution_score: float = 0.5
    sample_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    neutral_count: int = 0
    win_rate: Optional[float] = None
    avg_outcome_pct: Optional[float] = None
    score_status: str = "insufficient_sample"
    last_updated_at: str = ""
    score_provenance: dict = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# Storage helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_store() -> dict:
    try:
        if _CREDIBILITY_PATH.exists():
            return json.loads(_CREDIBILITY_PATH.read_text())
    except Exception as exc:  # noqa: BLE001
        log.warning("[SIGNAL_CRED] _load_store failed: %s", exc)
    return {}


def _save_store(store: dict) -> None:
    try:
        _CREDIBILITY_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CREDIBILITY_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(store, indent=2, sort_keys=True))
        tmp.rename(_CREDIBILITY_PATH)
    except Exception as exc:  # noqa: BLE001
        log.warning("[SIGNAL_CRED] _save_store failed: %s", exc)


def _record_from_dict(d: dict) -> SignalSourceCredibility:
    return SignalSourceCredibility(
        schema_version=int(d.get("schema_version", 1)),
        source_name=d.get("source_name", ""),
        contribution_score=float(d.get("contribution_score", 0.5)),
        sample_count=int(d.get("sample_count", 0)),
        win_count=int(d.get("win_count", 0)),
        loss_count=int(d.get("loss_count", 0)),
        neutral_count=int(d.get("neutral_count", 0)),
        win_rate=d.get("win_rate"),
        avg_outcome_pct=d.get("avg_outcome_pct"),
        score_status=d.get("score_status", "insufficient_sample"),
        last_updated_at=d.get("last_updated_at", ""),
        score_provenance=d.get("score_provenance", {}),
    )


def _status_for(sample_count: int) -> str:
    if sample_count >= ACTIVE_THRESHOLD:
        return "active"
    if sample_count >= PROVISIONAL_THRESHOLD:
        return "provisional"
    return "insufficient_sample"


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def initialize_source(source_name: str) -> SignalSourceCredibility:
    """Idempotent — returns existing record or creates a neutral-prior one."""
    try:
        store = _load_store()
        if source_name in store:
            return _record_from_dict(store[source_name])
        rec = SignalSourceCredibility(
            schema_version=1,
            source_name=source_name,
            last_updated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            score_provenance={"decision_ids": []},
        )
        store[source_name] = asdict(rec)
        _save_store(store)
        return rec
    except Exception as exc:  # noqa: BLE001
        log.warning("[SIGNAL_CRED] initialize_source(%s) failed: %s", source_name, exc)
        return SignalSourceCredibility(source_name=source_name)


def initialize_default_cohort() -> None:
    """Initialize the default signal-source cohort if missing. Idempotent."""
    for src in _DEFAULT_SOURCES:
        initialize_source(src)


def update_signal_credibility_from_outcome(
    source_name: str,
    classification: str,
    outcome_pct: Optional[float] = None,
    decision_id: Optional[str] = None,
) -> Optional[SignalSourceCredibility]:
    """
    Increment credibility for `source_name` from a single classified decision.

    `classification` is one of decision_outcomes.classify_alpha's labels:
      alpha_positive | alpha_negative | alpha_neutral | (anything else → no-op)

    Returns the updated record, or None on no-op / failure. Non-fatal.
    """
    if classification not in {"alpha_positive", "alpha_negative", "alpha_neutral"}:
        return None
    try:
        store = _load_store()
        if source_name not in store:
            initialize_source(source_name)
            store = _load_store()
        rec = _record_from_dict(store[source_name])

        rec.sample_count += 1
        if classification == "alpha_positive":
            rec.win_count += 1
            calibration_target = 1.0
        elif classification == "alpha_negative":
            rec.loss_count += 1
            calibration_target = 0.0
        else:
            rec.neutral_count += 1
            calibration_target = None  # neutrals don't pull the score

        # Win rate is wins over decisive (positive + negative) outcomes — neutrals
        # are sample-counting but not directional. avg_outcome_pct is a separate
        # rolling average over realized outcomes.
        decisive = rec.win_count + rec.loss_count
        rec.win_rate = round(rec.win_count / decisive, 4) if decisive > 0 else None

        if outcome_pct is not None:
            try:
                pct = float(outcome_pct)
                prev = rec.avg_outcome_pct if rec.avg_outcome_pct is not None else 0.0
                # Rolling average across all sample_count records (incl. neutrals
                # — outcome magnitude is informative even if direction is neutral).
                rec.avg_outcome_pct = round(
                    ((prev * (rec.sample_count - 1)) + pct) / rec.sample_count, 5
                )
            except (TypeError, ValueError):
                pass

        if calibration_target is not None:
            alpha = 1.0 / max(rec.sample_count, 1)
            rec.contribution_score = round(
                (1 - alpha) * rec.contribution_score + alpha * calibration_target, 4
            )

        rec.score_status = _status_for(rec.sample_count)
        rec.last_updated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        if decision_id:
            prov = rec.score_provenance.setdefault("decision_ids", [])
            if decision_id not in prov:
                prov.append(decision_id)

        store[source_name] = asdict(rec)
        _save_store(store)
        return rec
    except Exception as exc:  # noqa: BLE001
        log.warning("[SIGNAL_CRED] update for %s failed: %s", source_name, exc)
        return None


def update_from_module_tags(
    module_tags: dict,
    classification: str,
    outcome_pct: Optional[float] = None,
    decision_id: Optional[str] = None,
) -> int:
    """Apply update_signal_credibility_from_outcome for each truthy tag.

    Returns the number of sources updated. Non-fatal.
    """
    if not isinstance(module_tags, dict):
        return 0
    n_updated = 0
    for src, present in module_tags.items():
        if not present:
            continue
        rec = update_signal_credibility_from_outcome(
            source_name=src,
            classification=classification,
            outcome_pct=outcome_pct,
            decision_id=decision_id,
        )
        if rec is not None:
            n_updated += 1
    return n_updated


def get_credibility(source_name: str) -> Optional[SignalSourceCredibility]:
    try:
        store = _load_store()
        if source_name not in store:
            return None
        return _record_from_dict(store[source_name])
    except Exception as exc:  # noqa: BLE001
        log.warning("[SIGNAL_CRED] get_credibility failed: %s", exc)
        return None


def get_all_credibilities() -> list[SignalSourceCredibility]:
    """All records sorted by sample_count desc."""
    try:
        store = _load_store()
        recs = [_record_from_dict(v) for v in store.values()]
        return sorted(recs, key=lambda r: -r.sample_count)
    except Exception as exc:  # noqa: BLE001
        log.warning("[SIGNAL_CRED] get_all_credibilities failed: %s", exc)
        return []


def get_signal_source_win_rates() -> dict[str, dict]:
    """
    Return a {source_name: {win_rate, sample_count, score_status, ...}} dict
    suitable for injection into Agent 6's prompt.
    """
    out: dict[str, dict] = {}
    for rec in get_all_credibilities():
        out[rec.source_name] = {
            "win_rate":           rec.win_rate,
            "sample_count":       rec.sample_count,
            "score_status":       rec.score_status,
            "contribution_score": rec.contribution_score,
            "avg_outcome_pct":    rec.avg_outcome_pct,
        }
    return out
