"""
thesis_evaluator.py — Qualitative review layer for Thesis Lab (TL-2b).

Two-phase design:
  Phase 1 — Stub generation: always deterministic, no AI.
  Phase 2 — AI enrichment: Claude Haiku, gated behind enable_thesis_ai_ingestion flag.

Ring 2 only — advisory shadow, never touches live execution.
Weekly cadence only — not called from the 5-minute cycle.

Zero imports from: bot.py, order_executor.py, risk_kernel.py
"""

from __future__ import annotations

import json
import logging
import os
import random
import string
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_HAIKU_MODEL = "claude-haiku-4-5-20251001"

# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────

_THESIS_LAB_DIR = Path(__file__).parent / "data" / "thesis_lab"
_REVIEWS_FILE   = _THESIS_LAB_DIR / "reviews.jsonl"

# ─────────────────────────────────────────────────────────────────────────────
# Data class
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ThesisReview:
    review_id: str                           # "review_YYYYMMDD_HHMMSS_xxxx"
    thesis_id: str
    checkpoint_month: int                    # 3, 6, 9, or 12
    reviewed_at: str                         # ISO datetime
    # ── deterministic (always populated by stub) ──
    roi_at_checkpoint: Optional[float]       # None if data unavailable
    max_drawdown_at_checkpoint: float
    is_profitable: Optional[bool]            # None when inconclusive or pending
    data_quality: str                        # "full" | "partial" | "insufficient"
    final_verdict: str                       # "profitable" | "loss" | "inconclusive" | "pending"
    # ── AI fields (empty until enrich_review_with_ai succeeds) ──
    thesis_accuracy_score: Optional[float]   # 0.0–1.0
    market_translation_score: Optional[float]
    countersignal_score: Optional[float]
    recommended_action: str                  # "hold" | "exit" | "add" | "monitor" | ""
    summary: str                             # free-text AI evaluation
    ai_enriched: bool                        # True only after successful AI enrichment
    schema_version: int = 1


# ─────────────────────────────────────────────────────────────────────────────
# Feature flag
# ─────────────────────────────────────────────────────────────────────────────

def _ai_enrichment_enabled() -> bool:
    try:
        from feature_flags import is_enabled
        return is_enabled("enable_thesis_ai_ingestion", default=False)
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Claude client — lazy init so module is importable without ANTHROPIC_API_KEY
# ─────────────────────────────────────────────────────────────────────────────

_client = None


def _get_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    return _client


# ─────────────────────────────────────────────────────────────────────────────
# ID generation
# ─────────────────────────────────────────────────────────────────────────────

def _generate_review_id() -> str:
    ts     = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"review_{ts}_{suffix}"


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 — Stub generation (always runs, fully deterministic)
# ─────────────────────────────────────────────────────────────────────────────

_CHECKPOINT_ROI_FIELD: dict[int, str] = {
    3: "roi_3m", 6: "roi_6m", 9: "roi_9m", 12: "roi_12m",
}


def generate_review_stub(thesis, backtest, checkpoint_month: int) -> ThesisReview:
    """
    Build a deterministic ThesisReview from a ThesisRecord and ThesisBacktestResult.
    Never calls Claude. Always returns a usable stub regardless of data availability.

    checkpoint_month: 3, 6, 9, or 12
    """
    roi_field = _CHECKPOINT_ROI_FIELD.get(checkpoint_month, "roi_3m")
    roi       = getattr(backtest, roi_field, None)

    verdict      = getattr(backtest, "final_verdict", "pending")
    data_quality = getattr(backtest, "data_quality", "insufficient")
    max_dd       = getattr(backtest, "max_drawdown", 0.0)

    if roi is None:
        is_profitable = None
    elif roi > 0.01:
        is_profitable = True
    elif roi < -0.01:
        is_profitable = False
    else:
        is_profitable = None  # within ±1% noise zone — inconclusive

    return ThesisReview(
        review_id=_generate_review_id(),
        thesis_id=getattr(thesis, "thesis_id", ""),
        checkpoint_month=checkpoint_month,
        reviewed_at=datetime.now(timezone.utc).isoformat(),
        roi_at_checkpoint=roi,
        max_drawdown_at_checkpoint=max_dd,
        is_profitable=is_profitable,
        data_quality=data_quality,
        final_verdict=verdict,
        thesis_accuracy_score=None,
        market_translation_score=None,
        countersignal_score=None,
        recommended_action="",
        summary="",
        ai_enriched=False,
        schema_version=1,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — AI enrichment (gated, non-fatal)
# ─────────────────────────────────────────────────────────────────────────────

_EVAL_SYSTEM = (
    "You are a thesis performance evaluator. Analyze investment thesis outcomes based on "
    "quantitative backtest results. Return valid JSON only — no prose, no markdown, no code blocks."
)

_EVAL_PROMPT = """\
Evaluate this investment thesis given its backtest performance.

Thesis:
  Title: {title}
  Narrative: {narrative}
  Market belief: {market_belief}
  Primary bottleneck: {primary_bottleneck}
  Confirming signals: {confirming_signals}
  Countersignals: {countersignals}

Backtest results (checkpoint {checkpoint_month}m):
  ROI at checkpoint: {roi_at_checkpoint}
  Max drawdown: {max_drawdown}
  Data quality: {data_quality}
  Verdict: {final_verdict}

Score each dimension 0.0-1.0 and provide a recommended action. Return a JSON object:
{{
  "thesis_accuracy_score": 0.0,
  "market_translation_score": 0.0,
  "countersignal_score": 0.0,
  "recommended_action": "hold",
  "summary": "brief evaluation in 2-3 sentences"
}}

Rules:
- thesis_accuracy_score: how well the thesis narrative matched actual price behavior (1.0 = exactly right)
- market_translation_score: how well the market belief captured real conditions (1.0 = fully correct)
- countersignal_score: how accurately the countersignals predicted risk (1.0 = countersignals were correct)
- recommended_action must be one of: hold, exit, add, monitor
- If data_quality is insufficient or verdict is pending, set all scores to 0.0, recommended_action to "monitor"
- Do NOT reference current market prices or conditions — evaluation must be reproducible from backtest data only
- Return ONLY the JSON object"""


def enrich_review_with_ai(review: ThesisReview, thesis) -> ThesisReview:
    """
    Call Claude Haiku to add qualitative scores and summary to a ThesisReview stub.
    Non-fatal: returns the unchanged stub on any error.
    Gated behind enable_thesis_ai_ingestion feature flag.
    Does NOT inject current market context — evaluation is reproducible from backtest data only.
    """
    if not _ai_enrichment_enabled():
        log.debug("[THESIS_EVAL] AI enrichment disabled (enable_thesis_ai_ingestion=false)")
        return review

    try:
        _get_client()
        roi_str = f"{review.roi_at_checkpoint:.2%}" if review.roi_at_checkpoint is not None else "N/A"

        prompt = _EVAL_PROMPT.format(
            title=getattr(thesis, "title", ""),
            narrative=getattr(thesis, "narrative", ""),
            market_belief=getattr(thesis, "market_belief", ""),
            primary_bottleneck=getattr(thesis, "primary_bottleneck", ""),
            confirming_signals=", ".join(getattr(thesis, "confirming_signals", []) or []),
            countersignals=", ".join(getattr(thesis, "countersignals", []) or []),
            checkpoint_month=review.checkpoint_month,
            roi_at_checkpoint=roi_str,
            max_drawdown=f"{review.max_drawdown_at_checkpoint:.2%}",
            data_quality=review.data_quality,
            final_verdict=review.final_verdict,
        )

        response = _get_client().messages.create(
            model=_HAIKU_MODEL,
            max_tokens=512,
            system=_EVAL_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            end   = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
            raw   = "\n".join(lines[1:end])

        data = json.loads(raw)

        review.thesis_accuracy_score    = float(data.get("thesis_accuracy_score") or 0.0)
        review.market_translation_score = float(data.get("market_translation_score") or 0.0)
        review.countersignal_score      = float(data.get("countersignal_score") or 0.0)

        action                  = data.get("recommended_action", "monitor")
        review.recommended_action = action if action in ("hold", "exit", "add", "monitor") else "monitor"
        review.summary          = data.get("summary", "")
        review.ai_enriched      = True

    except Exception as exc:
        log.warning("[THESIS_EVAL] AI enrichment failed for %s: %s", review.thesis_id, exc)

    return review


# ─────────────────────────────────────────────────────────────────────────────
# Storage
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_dir() -> None:
    _THESIS_LAB_DIR.mkdir(parents=True, exist_ok=True)


def append_review(review: ThesisReview) -> None:
    """Append one review to reviews.jsonl (JSONL, one record per line)."""
    _ensure_dir()
    entry = {"saved_at": datetime.now(timezone.utc).isoformat(), **asdict(review)}
    with _REVIEWS_FILE.open("a") as fh:
        fh.write(json.dumps(entry, default=str) + "\n")


def load_reviews(thesis_id: str = None) -> list[dict]:
    """Load all reviews from reviews.jsonl, optionally filtered by thesis_id."""
    _ensure_dir()
    if not _REVIEWS_FILE.exists():
        return []
    results = []
    try:
        with _REVIEWS_FILE.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    if thesis_id is None or r.get("thesis_id") == thesis_id:
                        results.append(r)
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Status update logic
# ─────────────────────────────────────────────────────────────────────────────

_CHECKPOINT_STATUS: dict[int, str] = {
    3:  "checkpoint_3m_complete",
    6:  "checkpoint_6m_complete",
    9:  "checkpoint_9m_complete",
    12: "checkpoint_12m_complete",
}


def _update_thesis_status_for_checkpoint(thesis, review: ThesisReview) -> None:
    """
    Advance thesis lifecycle state based on a review stub.

    Rules (applied in order):
    1. Any stub generation: "researched" → "active_tracking"
    2. If roi_at_checkpoint is not None (data available):
       advance to checkpoint_Nm_complete if the transition is valid from the
       effective post-step-1 status.

    For a researched thesis with data, two hops are applied atomically here:
    researched → active_tracking → checkpoint_Nm_complete.
    """
    from thesis_registry import VALID_TRANSITIONS, update_thesis_status

    current_status = getattr(thesis, "status", "")
    target_status  = None

    if current_status == "researched":
        target_status = "active_tracking"

    if review.roi_at_checkpoint is not None:
        checkpoint_target = _CHECKPOINT_STATUS.get(review.checkpoint_month)
        if checkpoint_target:
            effective = target_status or current_status
            if checkpoint_target in VALID_TRANSITIONS.get(effective, []):
                target_status = checkpoint_target

    if not target_status or target_status == current_status:
        return

    try:
        if (current_status == "researched"
                and target_status == _CHECKPOINT_STATUS.get(review.checkpoint_month)):
            # Two-hop: researched → active_tracking → checkpoint_Nm_complete
            update_thesis_status(thesis.thesis_id, "active_tracking",
                                 notes="auto: checkpoint review started")
            update_thesis_status(thesis.thesis_id, target_status,
                                 notes=f"auto: checkpoint_{review.checkpoint_month}m data available")
        else:
            notes = (
                f"auto: checkpoint_{review.checkpoint_month}m data available"
                if target_status.startswith("checkpoint")
                else "auto: checkpoint review started"
            )
            update_thesis_status(thesis.thesis_id, target_status, notes=notes)

    except Exception as exc:
        log.warning("[THESIS_EVAL] Status update failed for %s: %s", thesis.thesis_id, exc)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def run_checkpoint_reviews(checkpoint_month: int = 3, force: bool = False) -> list[ThesisReview]:
    """
    Orchestrate checkpoint reviews for all trackable theses.

    1. Loads all non-quarantined theses in pre-complete states.
    2. For each thesis, loads the most recent backtest result.
    3. Generates a deterministic stub (always, regardless of data availability).
    4. Updates thesis status in the registry (not gated on AI).
    5. If enable_thesis_ai_ingestion is enabled (or force=True), enriches with Claude Haiku.
    6. Appends each review to data/thesis_lab/reviews.jsonl.

    Returns the list of ThesisReview objects generated.
    Non-fatal per thesis — logs WARNING and continues on individual failures.
    """
    from thesis_backtest import ThesisBacktestResult, load_backtest_results
    from thesis_registry import list_theses

    trackable_statuses = (
        "researched",
        "active_tracking",
        "checkpoint_3m_complete",
        "checkpoint_6m_complete",
        "checkpoint_9m_complete",
    )

    all_theses: list = []
    for s in trackable_statuses:
        all_theses.extend(list_theses(status=s))

    log.info(
        "[THESIS_EVAL] Running checkpoint_%dm reviews for %d theses",
        checkpoint_month, len(all_theses),
    )

    reviews: list[ThesisReview] = []

    for thesis in all_theses:
        if getattr(thesis, "status", "") == "quarantine":
            continue
        try:
            bt_records = load_backtest_results(thesis_id=thesis.thesis_id)
            if not bt_records:
                log.debug("[THESIS_EVAL] No backtest found for %s — skipping", thesis.thesis_id)
                continue

            bt_raw = bt_records[-1]
            bt = ThesisBacktestResult(
                thesis_id           = bt_raw.get("thesis_id", ""),
                expression_id       = bt_raw.get("expression_id", "base"),
                mode                = bt_raw.get("mode", "base"),
                entry_date          = bt_raw.get("entry_date", ""),
                checkpoints         = bt_raw.get("checkpoints", {}),
                roi_3m              = bt_raw.get("roi_3m"),
                roi_6m              = bt_raw.get("roi_6m"),
                roi_9m              = bt_raw.get("roi_9m"),
                roi_12m             = bt_raw.get("roi_12m"),
                max_drawdown        = bt_raw.get("max_drawdown", 0.0),
                final_verdict       = bt_raw.get("final_verdict", "pending"),
                data_quality        = bt_raw.get("data_quality", "insufficient"),
                missing_checkpoints = bt_raw.get("missing_checkpoints", []),
                schema_version      = bt_raw.get("schema_version", 1),
            )

            # Phase 1: deterministic stub — always runs
            review = generate_review_stub(thesis, bt, checkpoint_month)

            # Registry status update — not gated on AI
            _update_thesis_status_for_checkpoint(thesis, review)

            # Phase 2: AI enrichment — gated
            if force or _ai_enrichment_enabled():
                review = enrich_review_with_ai(review, thesis)

            append_review(review)
            reviews.append(review)

            log.info(
                "[THESIS_EVAL] %s | cp=%dm verdict=%s dq=%s ai=%s",
                thesis.thesis_id[:30], checkpoint_month,
                review.final_verdict, review.data_quality, review.ai_enriched,
            )

        except Exception as exc:
            log.warning("[THESIS_EVAL] Review failed for %s: %s",
                        getattr(thesis, "thesis_id", "?"), exc)

    log.info("[THESIS_EVAL] Done: %d reviews generated", len(reviews))
    return reviews
