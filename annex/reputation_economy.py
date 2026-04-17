# ANNEX MODULE — lab ring, no prod pipeline imports
"""
annex/reputation_economy.py — Reputation economy experiment (T6.7).

Evaluation class: quality_positive_non_alpha

Analytics-first. NO LLM in v1. Numeric reputation derived from actual
outcomes. Neutral prior (0.5) below sample threshold. score_status="insufficient_sample"
is the correct initial state for most entities.

Storage: data/annex/reputation_economy/reputations.json
Feature flag: enable_reputation_economy (lab_flags, default False).
Promotion contract: promotion_contracts/reputation_economy_v1.md (DRAFT).

Annex sandbox contract:
- No imports from bot.py, scheduler.py, order_executor.py, risk_kernel.py
- No writes to decision objects, strategy_config, execution paths
- Kill-switchable via feature flag
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_ANNEX_DIR = Path("data/annex/reputation_economy")
_REPUTATIONS_FILE = _ANNEX_DIR / "reputations.json"

_OUTCOMES_LOG = Path("data/analytics/decision_outcomes.jsonl")
_CHECKSUMS_LOG = Path("data/analytics/thesis_checksums.jsonl")
_CATALYST_LOG = Path("data/analytics/catalyst_log.jsonl")
_REC_STORE = Path("data/reports/recommendation_store.json")


# ─────────────────────────────────────────────────────────────────────────────
# Dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ReputationRecord:
    schema_version: int = 1
    entity_id: str = ""
    entity_type: str = ""           # "catalyst" | "thesis" | "signal" | "module"
    last_updated: str = ""
    total_appearances: int = 0
    outcome_linked_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    inconclusive_count: int = 0
    win_rate: float = 0.0
    reputation_score: float = 0.5   # neutral prior
    score_status: str = "insufficient_sample"
    sample_threshold: int = 5
    trend: str = "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_enabled() -> bool:
    try:
        from feature_flags import is_enabled  # noqa: PLC0415
        return is_enabled("enable_reputation_economy")
    except Exception:
        return False


def _compute_score(rec: ReputationRecord) -> ReputationRecord:
    """Compute win_rate, reputation_score, score_status, trend in-place."""
    n = rec.outcome_linked_count
    if n < rec.sample_threshold:
        rec.win_rate = 0.0
        rec.reputation_score = 0.5
        rec.score_status = "insufficient_sample"
        rec.trend = "unknown"
        return rec

    rec.win_rate = rec.win_count / n if n > 0 else 0.0
    loss_rate = rec.loss_count / n if n > 0 else 0.0
    rec.reputation_score = round(rec.win_rate * 0.7 + (1 - loss_rate) * 0.3, 4)
    rec.score_status = "active"
    return rec


def _infer_trend(history: list[str]) -> str:
    """Compare last 3 vs prior 3 outcomes to detect trend."""
    if len(history) < 6:
        return "unknown"
    recent = history[-3:]
    prior = history[-6:-3]
    recent_wins = sum(1 for o in recent if o == "alpha_positive")
    prior_wins = sum(1 for o in prior if o == "alpha_positive")
    if recent_wins > prior_wins:
        return "improving"
    if recent_wins < prior_wins:
        return "declining"
    return "stable"


def _load_outcomes(days_back: int) -> list[dict]:
    try:
        if not _OUTCOMES_LOG.exists():
            return []
        from datetime import timedelta  # noqa: PLC0415
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        results = []
        with open(_OUTCOMES_LOG) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts = rec.get("logged_at", rec.get("created_at", ""))
                    if ts:
                        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if t < cutoff:
                            continue
                    results.append(rec)
                except Exception:
                    continue
        return results
    except Exception as exc:
        log.debug("[REPUTATION] _load_outcomes failed: %s", exc)
        return []


def _load_checksums(days_back: int) -> list[dict]:
    try:
        if not _CHECKSUMS_LOG.exists():
            return []
        from datetime import timedelta  # noqa: PLC0415
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        results = []
        with open(_CHECKSUMS_LOG) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts = rec.get("created_at", rec.get("stamped_at", ""))
                    if ts:
                        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if t < cutoff:
                            continue
                    results.append(rec)
                except Exception:
                    continue
        return results
    except Exception as exc:
        log.debug("[REPUTATION] _load_checksums failed: %s", exc)
        return []


def _load_catalyst_log(days_back: int) -> list[dict]:
    try:
        if not _CATALYST_LOG.exists():
            return []
        from datetime import timedelta  # noqa: PLC0415
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        results = []
        with open(_CATALYST_LOG) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts = rec.get("normalized_at", rec.get("created_at", ""))
                    if ts:
                        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if t < cutoff:
                            continue
                    results.append(rec)
                except Exception:
                    continue
        return results
    except Exception as exc:
        log.debug("[REPUTATION] _load_catalyst_log failed: %s", exc)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def rebuild_reputations(days_back: int = 365) -> dict:
    """
    Full rebuild from decision_outcomes.jsonl + thesis_checksums.jsonl +
    catalyst_log.jsonl + recommendation_store.json.
    Returns dict keyed by entity_id. Non-fatal, returns {} on error.
    """
    try:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        records: dict[str, ReputationRecord] = {}
        # Track outcome histories per entity for trend analysis
        histories: dict[str, list[str]] = {}

        def _upsert(entity_id: str, entity_type: str) -> ReputationRecord:
            if entity_id not in records:
                records[entity_id] = ReputationRecord(
                    entity_id=entity_id,
                    entity_type=entity_type,
                    last_updated=now,
                )
                histories[entity_id] = []
            return records[entity_id]

        # From decision_outcomes — link catalyst_type and thesis_type to outcomes
        for outcome in _load_outcomes(days_back):
            alpha = outcome.get("alpha_classification", "")
            if not alpha:
                continue

            # Catalyst type reputation
            cat_type = outcome.get("catalyst_type", "")
            if cat_type and cat_type not in ("", "unknown"):
                rec = _upsert(f"catalyst:{cat_type}", "catalyst")
                rec.total_appearances += 1
                rec.outcome_linked_count += 1
                histories[f"catalyst:{cat_type}"].append(alpha)
                if alpha == "alpha_positive":
                    rec.win_count += 1
                elif alpha == "alpha_negative":
                    rec.loss_count += 1
                else:
                    rec.inconclusive_count += 1

            # Thesis type reputation
            thesis = outcome.get("thesis_type", "")
            if thesis and thesis not in ("", "unknown"):
                rec = _upsert(f"thesis:{thesis}", "thesis")
                rec.total_appearances += 1
                rec.outcome_linked_count += 1
                histories[f"thesis:{thesis}"].append(alpha)
                if alpha == "alpha_positive":
                    rec.win_count += 1
                elif alpha == "alpha_negative":
                    rec.loss_count += 1
                else:
                    rec.inconclusive_count += 1

            # Module tags reputation
            for tag in outcome.get("module_tags", []):
                if tag:
                    rec = _upsert(f"module:{tag}", "module")
                    rec.total_appearances += 1
                    rec.outcome_linked_count += 1
                    histories[f"module:{tag}"].append(alpha)
                    if alpha == "alpha_positive":
                        rec.win_count += 1
                    elif alpha == "alpha_negative":
                        rec.loss_count += 1
                    else:
                        rec.inconclusive_count += 1

        # From catalyst_log — count appearances even without outcomes
        for cat_rec in _load_catalyst_log(days_back):
            cat_type = cat_rec.get("catalyst_type", "")
            if cat_type and cat_type not in ("", "unknown"):
                rec = _upsert(f"catalyst:{cat_type}", "catalyst")
                rec.total_appearances += 1

        # From thesis checksums — count appearances even without outcomes
        for checksum in _load_checksums(days_back):
            thesis = checksum.get("thesis_type", "")
            if thesis and thesis not in ("", "unknown"):
                rec = _upsert(f"thesis:{thesis}", "thesis")
                rec.total_appearances += 1

        # Compute scores and trends
        for entity_id, rec in records.items():
            _compute_score(rec)
            rec.trend = _infer_trend(histories.get(entity_id, []))
            rec.last_updated = now

        save_reputations(records)
        return records
    except Exception as exc:
        log.warning("[REPUTATION] rebuild_reputations failed: %s", exc)
        return {}


def get_reputation(entity_id: str) -> Optional[ReputationRecord]:
    """Reads current reputation store. Returns None if not found."""
    try:
        if not _REPUTATIONS_FILE.exists():
            return None
        data = json.loads(_REPUTATIONS_FILE.read_text())
        rec_dict = data.get(entity_id)
        if rec_dict is None:
            return None
        return ReputationRecord(**{
            k: v for k, v in rec_dict.items()
            if k in ReputationRecord.__dataclass_fields__
        })
    except Exception as exc:
        log.warning("[REPUTATION] get_reputation failed: %s", exc)
        return None


def get_top_reputations(
    entity_type: Optional[str] = None,
    top_n: int = 10,
    min_status: str = "active",
) -> list:
    """Returns top N by reputation_score."""
    try:
        if not _REPUTATIONS_FILE.exists():
            return []
        data = json.loads(_REPUTATIONS_FILE.read_text())
        recs = []
        for rec_dict in data.values():
            if entity_type and rec_dict.get("entity_type") != entity_type:
                continue
            if min_status == "active" and rec_dict.get("score_status") != "active":
                continue
            recs.append(rec_dict)
        recs.sort(key=lambda r: r.get("reputation_score", 0.0), reverse=True)
        return recs[:top_n]
    except Exception as exc:
        log.warning("[REPUTATION] get_top_reputations failed: %s", exc)
        return []


def get_bottom_reputations(
    entity_type: Optional[str] = None,
    top_n: int = 5,
) -> list:
    """Returns bottom N by reputation_score (active only)."""
    try:
        if not _REPUTATIONS_FILE.exists():
            return []
        data = json.loads(_REPUTATIONS_FILE.read_text())
        recs = [
            r for r in data.values()
            if r.get("score_status") == "active"
            and (entity_type is None or r.get("entity_type") == entity_type)
        ]
        recs.sort(key=lambda r: r.get("reputation_score", 1.0))
        return recs[:top_n]
    except Exception as exc:
        log.warning("[REPUTATION] get_bottom_reputations failed: %s", exc)
        return []


def save_reputations(records: dict) -> bool:
    """Atomic write to data/annex/reputation_economy/reputations.json."""
    try:
        _ANNEX_DIR.mkdir(parents=True, exist_ok=True)
        serializable = {
            eid: (asdict(rec) if hasattr(rec, "__dataclass_fields__") else rec)
            for eid, rec in records.items()
        }
        tmp = _REPUTATIONS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(serializable, indent=2))
        tmp.replace(_REPUTATIONS_FILE)
        return True
    except Exception as exc:
        log.warning("[REPUTATION] save_reputations failed: %s", exc)
        return False


def format_reputation_for_review() -> str:
    """Markdown showing top 5 and bottom 5 entities by reputation."""
    try:
        if not _REPUTATIONS_FILE.exists():
            return ""
        data = json.loads(_REPUTATIONS_FILE.read_text())
        all_recs = list(data.values())
        total = len(all_recs)
        insufficient = sum(1 for r in all_recs if r.get("score_status") == "insufficient_sample")

        if total == 0:
            return ""

        top5 = sorted(
            [r for r in all_recs if r.get("score_status") == "active"],
            key=lambda r: r.get("reputation_score", 0.0),
            reverse=True,
        )[:5]

        bottom5 = sorted(
            [r for r in all_recs if r.get("score_status") == "active"],
            key=lambda r: r.get("reputation_score", 1.0),
        )[:5]

        lines = [
            "## Reputation Economy\n",
            f"Total entities: {total} | Insufficient sample: {insufficient} | Active: {total - insufficient}\n",
        ]

        if top5:
            lines.append("**Top 5 by reputation:**")
            for r in top5:
                trend_sym = {"improving": "↑", "declining": "↓", "stable": "→"}.get(
                    r.get("trend", ""), "?"
                )
                lines.append(
                    f"  - {r['entity_id']}: {r.get('reputation_score', 0):.2f} "
                    f"(win_rate={r.get('win_rate', 0):.2f}, n={r.get('outcome_linked_count', 0)}) {trend_sym}"
                )

        if bottom5:
            lines.append("\n**Bottom 5 by reputation (active only):**")
            for r in bottom5:
                lines.append(
                    f"  - {r['entity_id']}: {r.get('reputation_score', 0):.2f} "
                    f"(win_rate={r.get('win_rate', 0):.2f}, n={r.get('outcome_linked_count', 0)})"
                )

        if insufficient > 0:
            lines.append(
                f"\n_Note: {insufficient} entities have insufficient sample "
                f"(< 5 outcomes) and show neutral prior score (0.50)._"
            )

        return "\n".join(lines)
    except Exception as exc:
        log.warning("[REPUTATION] format_reputation_for_review failed: %s", exc)
        return ""
