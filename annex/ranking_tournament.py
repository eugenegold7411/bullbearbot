# ANNEX MODULE — lab ring, no prod pipeline imports
"""
annex/ranking_tournament.py — Annex ranking tournament engine (T6.13).

Evaluation class: exploratory — no alpha claim, no quality claim yet.
This is infrastructure for future evaluation.

Pairwise comparison engine. No Elo yet. Records rubric scores and evidence.
Stays entirely in annex namespace. No LLM calls.

Storage: data/annex/ranking_tournament/ — annex namespace only.
Feature flag: enable_annex_ranking_tournament (lab_flags, default False).
Promotion contract: promotion_contracts/ranking_tournament_v1.md (DRAFT).

Annex sandbox contract:
- No imports from bot.py, scheduler.py, order_executor.py, risk_kernel.py
- No writes to decision objects, strategy_config, execution paths
- Outputs include confidence and/or abstention
- Kill-switchable via feature flag
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_ANNEX_DIR = Path("data/annex/ranking_tournament")
_COMPARISONS_LOG = _ANNEX_DIR / "comparisons.jsonl"

# Rubric weights for winner determination
_WEIGHT_SPECIFICITY = 0.4
_WEIGHT_TESTABILITY = 0.35
_WEIGHT_CALIBRATION = 0.25

# Minimum comparisons per source before appearing in leaderboard
_MIN_COMPARISONS_FOR_LEADERBOARD = 3


# ─────────────────────────────────────────────────────────────────────────────
# Dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PairwiseComparison:
    schema_version: int = 1
    comparison_id: str = ""
    compared_at: str = ""
    artifact_type: str = ""
    artifact_a_id: str = ""
    artifact_b_id: str = ""
    artifact_a_source: str = ""
    artifact_b_source: str = ""
    case_id: str = ""
    # Rubric scores (0.0–1.0 each)
    a_specificity: float = 0.0
    b_specificity: float = 0.0
    a_testability: float = 0.0
    b_testability: float = 0.0
    a_calibration: float = 0.0
    b_calibration: float = 0.0
    # Result
    winner: str = ""
    winner_source: str = ""
    win_reason: str = ""
    confidence: float = 0.0
    later_evidence_supported: Optional[bool] = None
    abstention: Optional[dict] = None
    evaluation_class: str = "exploratory"


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_enabled() -> bool:
    try:
        from feature_flags import is_enabled  # noqa: PLC0415
        return is_enabled("enable_annex_ranking_tournament")
    except Exception:
        return False


def _score_specificity(artifact: dict) -> float:
    """
    How specific and concrete is this artifact's output?
    Scores based on: length of key text fields, presence of numbers/percentages,
    named entities, non-generic language.
    """
    try:
        score = 0.0
        # Collect all textual content
        text_fields = []
        for key in ("description", "divergence_description", "testable_prediction",
                    "primary_reason", "key_concern", "reasoning", "claim",
                    "dominant_theory", "theories"):
            val = artifact.get(key, "")
            if isinstance(val, list):
                text_fields.extend(str(v) for v in val if v)
            elif val:
                text_fields.append(str(val))

        combined = " ".join(text_fields)
        if not combined:
            return 0.1

        word_count = len(combined.split())
        # Base score from content density
        if word_count >= 30:
            score += 0.3
        elif word_count >= 15:
            score += 0.2
        else:
            score += 0.1

        # Numeric specificity (numbers, %, decimals)
        import re
        numbers = re.findall(r'\b\d+\.?\d*%?\b', combined)
        if len(numbers) >= 3:
            score += 0.3
        elif len(numbers) >= 1:
            score += 0.15

        # Named entities (capitalized words that look like tickers or names)
        named = re.findall(r'\b[A-Z]{2,5}\b', combined)
        if len(named) >= 2:
            score += 0.2
        elif len(named) >= 1:
            score += 0.1

        # Confidence field present and non-trivial
        conf = float(artifact.get("confidence", 0.5) or 0.5)
        if 0.3 <= conf <= 0.85:
            score += 0.1  # neither trivially low nor suspiciously high

        # Abstention is specific in its own way (honest abstention > vague answer)
        if artifact.get("abstention"):
            score = max(score, 0.3)

        return min(1.0, score)
    except Exception:
        return 0.0


def _score_testability(artifact: dict) -> float:
    """
    Does this artifact make a testable prediction?
    """
    try:
        score = 0.0
        # Direct testable_prediction field
        tp = str(artifact.get("testable_prediction", "") or "")
        if tp and len(tp.split()) >= 5:
            score += 0.5
        elif tp:
            score += 0.25

        # Theories list with individual testable_predictions
        theories = artifact.get("theories", [])
        if isinstance(theories, list) and theories:
            has_any_tp = any(
                isinstance(t, dict) and t.get("testable_prediction")
                for t in theories
            )
            if has_any_tp:
                score += 0.3

        # Would-have-done fields imply counterfactual testability
        if artifact.get("would_have_done") or artifact.get("would_action"):
            score += 0.15

        # Supporting evidence implies the prediction is grounded
        if artifact.get("supporting_evidence") and len(str(artifact["supporting_evidence"])) > 20:
            score += 0.1

        return min(1.0, score)
    except Exception:
        return 0.0


def _score_calibration(artifact: dict) -> float:
    """
    Does confidence match evidence strength?
    Penalizes trivial confidence (0.5 exactly) and overconfidence (0.95+) without strong evidence.
    """
    try:
        conf = float(artifact.get("confidence", 0.5) or 0.5)

        # Trivial confidence — just defaulted
        if conf == 0.5:
            return 0.3

        # Overconfident without abstention
        if conf >= 0.95 and not artifact.get("abstention"):
            has_evidence = any(
                artifact.get(k) for k in
                ("supporting_evidence", "evidence_decision_ids", "signal_sources", "theories")
            )
            if not has_evidence:
                return 0.2

        # Honest abstention with reason is well-calibrated
        if artifact.get("abstention") and artifact["abstention"].get("reason"):
            return 0.7

        # Good calibration range
        if 0.55 <= conf <= 0.85:
            return 0.8
        elif 0.4 <= conf <= 0.9:
            return 0.6
        else:
            return 0.4
    except Exception:
        return 0.3


def _extract_artifact_id(artifact: dict) -> str:
    """Try common ID field names."""
    for key in ("signal_id", "opinion_id", "failure_theory_id", "comparison_id",
                "confession_id", "profile_id", "rec_id", "forensic_id", "id"):
        if artifact.get(key):
            return str(artifact[key])
    return str(uuid.uuid4())


def _extract_source(artifact: dict) -> str:
    """Try to extract producing module name."""
    for key in ("module_name", "ghost_name", "fork_name", "deception_type",
                "hallucination_type", "confession_type", "source"):
        if artifact.get(key):
            return str(artifact[key])
    return "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def compare_pair(
    artifact_a: dict,
    artifact_b: dict,
    artifact_type: str,
    case_id: str,
) -> Optional[PairwiseComparison]:
    """
    Apply rubric to score both artifacts. Determines winner by weighted sum.
    No LLM calls. Returns PairwiseComparison or None on error. Non-fatal.
    """
    try:
        a_spec = _score_specificity(artifact_a)
        b_spec = _score_specificity(artifact_b)
        a_test = _score_testability(artifact_a)
        b_test = _score_testability(artifact_b)
        a_cal = _score_calibration(artifact_a)
        b_cal = _score_calibration(artifact_b)

        a_total = (a_spec * _WEIGHT_SPECIFICITY +
                   a_test * _WEIGHT_TESTABILITY +
                   a_cal  * _WEIGHT_CALIBRATION)
        b_total = (b_spec * _WEIGHT_SPECIFICITY +
                   b_test * _WEIGHT_TESTABILITY +
                   b_cal  * _WEIGHT_CALIBRATION)

        delta = abs(a_total - b_total)
        if delta < 0.05:
            winner = "tie"
            winner_source = ""
            win_reason = f"Scores too close (A={a_total:.2f}, B={b_total:.2f})"
            confidence = 0.4
        elif a_total > b_total:
            winner = "A"
            winner_source = _extract_source(artifact_a)
            win_reason = (
                f"A wins: spec={a_spec:.2f} test={a_test:.2f} cal={a_cal:.2f} "
                f"vs B: spec={b_spec:.2f} test={b_test:.2f} cal={b_cal:.2f}"
            )
            confidence = min(0.9, 0.5 + delta * 2)
        else:
            winner = "B"
            winner_source = _extract_source(artifact_b)
            win_reason = (
                f"B wins: spec={b_spec:.2f} test={b_test:.2f} cal={b_cal:.2f} "
                f"vs A: spec={a_spec:.2f} test={a_test:.2f} cal={a_cal:.2f}"
            )
            confidence = min(0.9, 0.5 + delta * 2)

        return PairwiseComparison(
            schema_version=1,
            comparison_id=str(uuid.uuid4()),
            compared_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            artifact_type=artifact_type,
            artifact_a_id=_extract_artifact_id(artifact_a),
            artifact_b_id=_extract_artifact_id(artifact_b),
            artifact_a_source=_extract_source(artifact_a),
            artifact_b_source=_extract_source(artifact_b),
            case_id=case_id,
            a_specificity=round(a_spec, 3),
            b_specificity=round(b_spec, 3),
            a_testability=round(a_test, 3),
            b_testability=round(b_test, 3),
            a_calibration=round(a_cal, 3),
            b_calibration=round(b_cal, 3),
            winner=winner,
            winner_source=winner_source,
            win_reason=win_reason[:200],
            confidence=round(confidence, 3),
        )
    except Exception as exc:
        log.warning("[TOURNAMENT] compare_pair failed: %s", exc)
        return None


def run_tournament(
    artifacts: list,
    artifact_type: str,
    case_id: str,
) -> list:
    """
    Round-robin pairwise comparisons across all artifacts for the same case.
    Returns all comparison records. [] on error.
    """
    results = []
    try:
        if not _is_enabled():
            return results
        n = len(artifacts)
        if n < 2:
            return results
        for i in range(n):
            for j in range(i + 1, n):
                comp = compare_pair(artifacts[i], artifacts[j], artifact_type, case_id)
                if comp is not None:
                    results.append(comp)
    except Exception as exc:
        log.warning("[TOURNAMENT] run_tournament failed: %s", exc)
    return results


def log_comparison(comparison: PairwiseComparison) -> Optional[str]:
    """Appends to data/annex/ranking_tournament/comparisons.jsonl. Returns comparison_id or None."""
    try:
        _ANNEX_DIR.mkdir(parents=True, exist_ok=True)
        with open(_COMPARISONS_LOG, "a") as fh:
            fh.write(json.dumps(asdict(comparison)) + "\n")
        return comparison.comparison_id
    except Exception as exc:
        log.warning("[TOURNAMENT] log_comparison failed: %s", exc)
        return None


def get_leaderboard(
    artifact_type: Optional[str] = None,
    days_back: int = 30,
) -> list:
    """
    Tally wins/losses/ties per source module from comparison log.
    Only includes sources with >= _MIN_COMPARISONS_FOR_LEADERBOARD comparisons.
    Returns sorted list: [{source, wins, losses, ties, win_rate}]. [] on error.
    """
    try:
        if not _COMPARISONS_LOG.exists():
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        tally: dict = {}  # source → {wins, losses, ties}

        with open(_COMPARISONS_LOG) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts_str = rec.get("compared_at", "")
                    if ts_str:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if ts < cutoff:
                            continue
                    if artifact_type and rec.get("artifact_type") != artifact_type:
                        continue

                    winner = rec.get("winner", "")
                    src_a = rec.get("artifact_a_source", "unknown")
                    src_b = rec.get("artifact_b_source", "unknown")

                    for src in [src_a, src_b]:
                        if src not in tally:
                            tally[src] = {"wins": 0, "losses": 0, "ties": 0}

                    if winner == "A":
                        tally[src_a]["wins"] += 1
                        tally[src_b]["losses"] += 1
                    elif winner == "B":
                        tally[src_b]["wins"] += 1
                        tally[src_a]["losses"] += 1
                    elif winner == "tie":
                        tally[src_a]["ties"] += 1
                        tally[src_b]["ties"] += 1
                except Exception:
                    continue

        results = []
        for src, counts in tally.items():
            total = counts["wins"] + counts["losses"] + counts["ties"]
            if total < _MIN_COMPARISONS_FOR_LEADERBOARD:
                continue
            win_rate = counts["wins"] / total if total > 0 else 0.0
            results.append({
                "source": src,
                "wins": counts["wins"],
                "losses": counts["losses"],
                "ties": counts["ties"],
                "total": total,
                "win_rate": round(win_rate, 3),
            })

        return sorted(results, key=lambda x: -x["win_rate"])
    except Exception as exc:
        log.warning("[TOURNAMENT] get_leaderboard failed: %s", exc)
        return []


def format_leaderboard_for_review(days_back: int = 30) -> str:
    """Returns markdown leaderboard table. Returns "" on error or no data."""
    try:
        board = get_leaderboard(days_back=days_back)
        if not board:
            return ""

        lines = [
            f"## Annex Ranking Tournament Leaderboard ({days_back}d)\n",
            "| Rank | Source | Wins | Losses | Ties | Win Rate |",
            "|------|--------|------|--------|------|----------|",
        ]
        for i, entry in enumerate(board, 1):
            lines.append(
                f"| {i} | {entry['source']} | {entry['wins']} | "
                f"{entry['losses']} | {entry['ties']} | {entry['win_rate']:.1%} |"
            )

        return "\n".join(lines)
    except Exception as exc:
        log.warning("[TOURNAMENT] format_leaderboard_for_review failed: %s", exc)
        return ""
