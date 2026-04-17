# ANNEX MODULE — lab ring, no prod pipeline imports
"""
annex/self_deception_detector.py — Self-deception detector (T6.1).

Evaluation class: quality_positive_non_alpha

Detects patterns where the bot's stated reasoning diverges from actual behavior.
Rule-based detection only — no LLM calls. No spine attribution needed.

Storage: data/annex/self_deception_detector/ — annex namespace only.
Feature flag: enable_self_deception_detector (lab_flags, default False).
Promotion contract: promotion_contracts/self_deception_detector_v1.md (DRAFT).

Annex sandbox contract:
- No imports from bot.py, scheduler.py, order_executor.py, risk_kernel.py
- No writes to decision objects, strategy_config, execution paths, or readiness artifacts
- Outputs include confidence and/or abstention
- Kill-switchable via feature flag
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_ANNEX_DIR = Path("data/annex/self_deception_detector")
_SIGNALS_LOG = _ANNEX_DIR / "signals.jsonl"
_DECISIONS_PATH = Path("memory/decisions.json")
_OUTCOMES_PATH = Path("data/analytics/decision_outcomes.jsonl")
_FORENSIC_PATH = Path("data/analytics/forensic_log.jsonl")
_TRADES_PATH = Path("logs/trades.jsonl")


# ─────────────────────────────────────────────────────────────────────────────
# Enums and dataclasses
# ─────────────────────────────────────────────────────────────────────────────

class DeceptionType(str, Enum):
    CONFIDENCE_OUTCOME_MISMATCH  = "confidence_outcome_mismatch"
    STATED_HOLD_ACTUAL_TRADE     = "stated_hold_actual_trade"
    CATALYST_FABRICATION         = "catalyst_fabrication"
    REGIME_CONTRADICTION         = "regime_contradiction"
    REPEATED_SAME_MISTAKE        = "repeated_same_mistake"
    THESIS_DRIFT                 = "thesis_drift"


@dataclass
class SelfDeceptionSignal:
    schema_version: int = 1
    signal_id: str = ""
    detected_at: str = ""
    deception_type: str = ""
    subject_decision_id: str = ""
    stated_reasoning: str = ""
    actual_behavior: str = ""
    divergence_description: str = ""
    confidence: float = 0.0
    evidence_decision_ids: list = field(default_factory=list)
    abstention: Optional[dict] = None
    evaluation_class: str = "quality_positive_non_alpha"


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_enabled() -> bool:
    try:
        from feature_flags import is_enabled  # noqa: PLC0415
        return is_enabled("enable_self_deception_detector")
    except Exception:
        return False


def _load_decisions(days_back: int) -> list:
    try:
        if not _DECISIONS_PATH.exists():
            return []
        raw = json.loads(_DECISIONS_PATH.read_text())
        decisions = raw if isinstance(raw, list) else raw.get("decisions", [])
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        result = []
        for d in decisions:
            ts = d.get("timestamp", d.get("created_at", ""))
            if ts:
                try:
                    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if t >= cutoff:
                        result.append(d)
                        continue
                except Exception:
                    pass
            result.append(d)
        return result[-500:]
    except Exception as exc:
        log.debug("[SDD] _load_decisions failed: %s", exc)
        return []


def _load_outcomes(days_back: int) -> list:
    try:
        if not _OUTCOMES_PATH.exists():
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        results = []
        with open(_OUTCOMES_PATH) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts_str = rec.get("logged_at", rec.get("created_at", ""))
                    if ts_str:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if ts >= cutoff:
                            results.append(rec)
                except Exception:
                    continue
        return results
    except Exception as exc:
        log.debug("[SDD] _load_outcomes failed: %s", exc)
        return []


def _load_forensics(days_back: int) -> list:
    try:
        if not _FORENSIC_PATH.exists():
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        results = []
        with open(_FORENSIC_PATH) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts_str = rec.get("created_at", "")
                    if ts_str:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if ts >= cutoff:
                            results.append(rec)
                except Exception:
                    continue
        return results
    except Exception as exc:
        log.debug("[SDD] _load_forensics failed: %s", exc)
        return []


def _make_signal(
    deception_type: str,
    decision_id: str,
    stated: str,
    actual: str,
    description: str,
    confidence: float,
    evidence_ids: Optional[list] = None,
) -> SelfDeceptionSignal:
    return SelfDeceptionSignal(
        schema_version=1,
        signal_id=str(uuid.uuid4()),
        detected_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        deception_type=deception_type,
        subject_decision_id=decision_id,
        stated_reasoning=stated[:300],
        actual_behavior=actual[:300],
        divergence_description=description[:200],
        confidence=confidence,
        evidence_decision_ids=evidence_ids or [],
        evaluation_class="quality_positive_non_alpha",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Detection rules
# ─────────────────────────────────────────────────────────────────────────────

def _detect_confidence_outcome_mismatch(outcomes: list) -> list:
    """High conviction (>0.8) but alpha_negative outcome."""
    signals = []
    try:
        for rec in outcomes:
            conviction = float(rec.get("conviction", rec.get("confidence", 0.0)) or 0.0)
            alpha_class = rec.get("alpha_classification", "")
            decision_id = rec.get("decision_id", "")
            if conviction > 0.8 and alpha_class == "alpha_negative":
                signals.append(_make_signal(
                    deception_type=DeceptionType.CONFIDENCE_OUTCOME_MISMATCH,
                    decision_id=decision_id,
                    stated=f"conviction={conviction:.2f}",
                    actual=f"alpha_classification={alpha_class}",
                    description="High conviction trade resulted in alpha_negative outcome",
                    confidence=0.75,
                    evidence_ids=[decision_id] if decision_id else [],
                ))
    except Exception as exc:
        log.debug("[SDD] _detect_confidence_outcome_mismatch failed: %s", exc)
    return signals


def _detect_catalyst_fabrication(decisions: list) -> list:
    """Decisions submitted with null/empty/placeholder catalyst."""
    signals = []
    _null_catalysts = {"no", "none", "null", "", "n/a", "na", "unknown"}
    try:
        for dec in decisions:
            ideas = dec.get("ideas", [])
            if not ideas:
                continue
            for idea in ideas:
                if not isinstance(idea, dict):
                    continue
                catalyst = str(idea.get("catalyst", "") or "").strip().lower()
                if catalyst in _null_catalysts and idea.get("intent", "").startswith("enter"):
                    decision_id = dec.get("decision_id", dec.get("id", ""))
                    signals.append(_make_signal(
                        deception_type=DeceptionType.CATALYST_FABRICATION,
                        decision_id=decision_id,
                        stated=f"catalyst={idea.get('catalyst', '')!r}",
                        actual="entry submitted without verifiable catalyst",
                        description="Entry idea has null/empty catalyst — catalyst_discipline violation",
                        confidence=0.85,
                        evidence_ids=[decision_id] if decision_id else [],
                    ))
    except Exception as exc:
        log.debug("[SDD] _detect_catalyst_fabrication failed: %s", exc)
    return signals


def _detect_regime_contradiction(decisions: list) -> list:
    """regime_view contradicts regime_score (e.g. risk_on but score < 30)."""
    signals = []
    try:
        for dec in decisions:
            regime_view = str(dec.get("regime_view", dec.get("regime", "")) or "").lower()
            regime_score = dec.get("regime_score", dec.get("regime", {}) if isinstance(dec.get("regime"), dict) else None)
            if isinstance(regime_score, dict):
                regime_score = regime_score.get("regime_score")
            if regime_score is None:
                continue
            try:
                score = float(regime_score)
            except (ValueError, TypeError):
                continue

            contradiction = False
            if "risk_on" in regime_view and score < 30:
                contradiction = True
            elif "risk_off" in regime_view and score > 70:
                contradiction = True
            elif "bullish" in regime_view and score < 30:
                contradiction = True
            elif "bearish" in regime_view and score > 70:
                contradiction = True

            if contradiction:
                decision_id = dec.get("decision_id", dec.get("id", ""))
                signals.append(_make_signal(
                    deception_type=DeceptionType.REGIME_CONTRADICTION,
                    decision_id=decision_id,
                    stated=f"regime_view={regime_view!r}",
                    actual=f"regime_score={score:.0f}",
                    description=f"Regime view '{regime_view}' contradicts score={score:.0f}",
                    confidence=0.7,
                    evidence_ids=[decision_id] if decision_id else [],
                ))
    except Exception as exc:
        log.debug("[SDD] _detect_regime_contradiction failed: %s", exc)
    return signals


def _detect_repeated_same_mistake(forensics: list) -> list:
    """Same pattern_tag in forensic records 3+ times with thesis_verdict=incorrect."""
    signals = []
    try:
        tag_counts: dict = {}
        tag_examples: dict = {}
        for rec in forensics:
            if rec.get("thesis_verdict") != "incorrect":
                continue
            for tag in rec.get("pattern_tags", []):
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
                tag_examples.setdefault(tag, []).append(rec.get("forensic_id", ""))

        for tag, count in tag_counts.items():
            if count >= 3:
                signals.append(_make_signal(
                    deception_type=DeceptionType.REPEATED_SAME_MISTAKE,
                    decision_id="",
                    stated=f"pattern_tag={tag!r}",
                    actual=f"appeared {count}x in forensic records with thesis_verdict=incorrect",
                    description=f"Pattern '{tag}' repeated {count} times with incorrect thesis verdict",
                    confidence=0.8,
                    evidence_ids=tag_examples.get(tag, [])[:5],
                ))
    except Exception as exc:
        log.debug("[SDD] _detect_repeated_same_mistake failed: %s", exc)
    return signals


def _detect_thesis_drift(decisions: list) -> list:
    """idea.direction contradicts thesis_type for same symbol within 24 hours."""
    signals = []
    try:
        from collections import defaultdict
        symbol_entries: dict = defaultdict(list)
        for dec in decisions:
            ts_str = dec.get("timestamp", dec.get("created_at", ""))
            decision_id = dec.get("decision_id", dec.get("id", ""))
            for idea in dec.get("ideas", []):
                if not isinstance(idea, dict):
                    continue
                symbol = idea.get("symbol", "")
                direction = str(idea.get("direction", idea.get("intent", "")) or "").lower()
                thesis = str(idea.get("thesis_type", "") or "")
                if symbol and direction and thesis:
                    symbol_entries[symbol].append({
                        "ts": ts_str,
                        "direction": direction,
                        "thesis_type": thesis,
                        "decision_id": decision_id,
                    })

        for symbol, entries in symbol_entries.items():
            if len(entries) < 2:
                continue
            entries_sorted = sorted(entries, key=lambda x: x.get("ts", ""))
            for i in range(len(entries_sorted) - 1):
                a = entries_sorted[i]
                b = entries_sorted[i + 1]
                # Check time delta <= 24h
                try:
                    ta = datetime.fromisoformat(a["ts"].replace("Z", "+00:00"))
                    tb = datetime.fromisoformat(b["ts"].replace("Z", "+00:00"))
                    if abs((tb - ta).total_seconds()) > 86400:
                        continue
                except Exception:
                    continue

                # Check if direction flipped with same thesis
                dir_a = a["direction"]
                dir_b = b["direction"]
                bullish = {"buy", "enter_long", "long"}
                bearish = {"sell", "enter_short", "short"}
                flipped = (dir_a in bullish and dir_b in bearish) or (dir_a in bearish and dir_b in bullish)
                if flipped and a["thesis_type"] == b["thesis_type"]:
                    signals.append(_make_signal(
                        deception_type=DeceptionType.THESIS_DRIFT,
                        decision_id=b["decision_id"],
                        stated=f"thesis_type={b['thesis_type']!r} direction={dir_b}",
                        actual=f"prior direction={dir_a} for same symbol+thesis within 24h",
                        description=f"{symbol}: direction flipped {dir_a}→{dir_b} with unchanged thesis",
                        confidence=0.65,
                        evidence_ids=[a["decision_id"], b["decision_id"]],
                    ))
    except Exception as exc:
        log.debug("[SDD] _detect_thesis_drift failed: %s", exc)
    return signals


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def detect_self_deception(days_back: int = 30) -> list:
    """
    Run all detection rules against available data.
    Returns list of SelfDeceptionSignals. [] if none or on error.
    Non-fatal.
    """
    if not _is_enabled():
        return []

    signals = []
    try:
        decisions = _load_decisions(days_back)
        outcomes = _load_outcomes(days_back)
        forensics = _load_forensics(days_back)

        signals.extend(_detect_confidence_outcome_mismatch(outcomes))
        signals.extend(_detect_catalyst_fabrication(decisions))
        signals.extend(_detect_regime_contradiction(decisions))
        signals.extend(_detect_repeated_same_mistake(forensics))
        signals.extend(_detect_thesis_drift(decisions))

        log.info("[SDD] detected %d signals from %d decisions / %d outcomes / %d forensics",
                 len(signals), len(decisions), len(outcomes), len(forensics))
    except Exception as exc:
        log.warning("[SDD] detect_self_deception failed: %s", exc)

    return signals


def log_signal(signal: SelfDeceptionSignal) -> Optional[str]:
    """Appends to data/annex/self_deception_detector/signals.jsonl. Returns signal_id or None."""
    try:
        _ANNEX_DIR.mkdir(parents=True, exist_ok=True)
        with open(_SIGNALS_LOG, "a") as fh:
            fh.write(json.dumps(asdict(signal)) + "\n")
        return signal.signal_id
    except Exception as exc:
        log.warning("[SDD] log_signal failed: %s", exc)
        return None


def format_signals_for_review(days_back: int = 30) -> str:
    """
    Returns markdown summary of detected signals for weekly review.
    Groups by deception_type, shows counts and examples.
    Returns "" on error or no signals.
    """
    try:
        signals = detect_self_deception(days_back=days_back)
        if not signals:
            return ""

        by_type: dict = {}
        for sig in signals:
            t = sig.deception_type
            by_type.setdefault(t, []).append(sig)

        lines = [
            f"## Self-Deception Signals ({days_back}d)\n",
            f"Total signals: {len(signals)}\n",
        ]
        for dtype, sigs in sorted(by_type.items(), key=lambda x: -len(x[1])):
            lines.append(f"**{dtype}** — {len(sigs)} instance(s)")
            for sig in sigs[:2]:
                lines.append(f"  - {sig.divergence_description} (confidence={sig.confidence:.2f})")
            lines.append("")

        return "\n".join(lines)
    except Exception as exc:
        log.warning("[SDD] format_signals_for_review failed: %s", exc)
        return ""
