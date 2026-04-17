# ANNEX MODULE — lab ring, no prod pipeline imports
"""
annex/consensus_hallucination_detector.py — Consensus hallucination detector (T6.4).

Evaluation class: quality_positive_non_alpha

Detects when all signals agree suspiciously — cases where apparent consensus
may be an artifact of correlated inputs rather than independent confirmation.
No LLM calls. Rule-based only.

Storage: data/annex/consensus_hallucination_detector/ — annex namespace only.
Feature flag: enable_consensus_hallucination_detector (lab_flags, default False).
Promotion contract: promotion_contracts/consensus_hallucination_detector_v1.md (DRAFT).

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
from enum import Enum
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_ANNEX_DIR = Path("data/annex/consensus_hallucination_detector")
_SIGNALS_LOG = _ANNEX_DIR / "signals.jsonl"
_DECISIONS_PATH = Path("memory/decisions.json")
_OUTCOMES_PATH = Path("data/analytics/decision_outcomes.jsonl")
_MORNING_BRIEF_PATH = Path("data/market/morning_brief.json")


# ─────────────────────────────────────────────────────────────────────────────
# Enums and dataclasses
# ─────────────────────────────────────────────────────────────────────────────

class HallucinationType(str, Enum):
    ALL_SIGNALS_AGREE_BAD_OUTCOME   = "all_signals_agree_bad_outcome"
    REGIME_SIGNAL_CORRELATION       = "regime_signal_correlation"
    MORNING_BRIEF_ECHO              = "morning_brief_echo"
    REDDIT_TECHNICAL_ALIGNMENT      = "reddit_technical_alignment"
    SINGLE_SOURCE_AMPLIFICATION     = "single_source_amplification"


@dataclass
class HallucinationSignal:
    schema_version: int = 1
    signal_id: str = ""
    detected_at: str = ""
    decision_id: str = ""
    hallucination_type: str = ""
    consensus_score: float = 0.0
    signal_sources: list = field(default_factory=list)
    outcome: Optional[str] = None
    description: str = ""
    confidence: float = 0.0
    abstention: Optional[dict] = None
    evaluation_class: str = "quality_positive_non_alpha"


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_enabled() -> bool:
    try:
        from feature_flags import is_enabled  # noqa: PLC0415
        return is_enabled("enable_consensus_hallucination_detector")
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
        log.debug("[CHD] _load_decisions failed: %s", exc)
        return []


def _load_outcomes(days_back: int) -> dict:
    """Returns dict mapping decision_id → outcome record."""
    out = {}
    try:
        if not _OUTCOMES_PATH.exists():
            return out
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
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
                        if ts < cutoff:
                            continue
                    did = rec.get("decision_id", "")
                    if did:
                        out[did] = rec
                except Exception:
                    continue
    except Exception as exc:
        log.debug("[CHD] _load_outcomes failed: %s", exc)
    return out


def _load_morning_brief_symbols() -> list:
    """Returns list of symbols mentioned in the latest morning brief."""
    try:
        if not _MORNING_BRIEF_PATH.exists():
            return []
        raw = json.loads(_MORNING_BRIEF_PATH.read_text())
        symbols = []
        for item in raw.get("trade_ideas", raw.get("ideas", [])):
            if isinstance(item, dict):
                sym = item.get("symbol", item.get("ticker", ""))
                if sym:
                    symbols.append(str(sym).upper())
        return symbols
    except Exception as exc:
        log.debug("[CHD] _load_morning_brief_symbols failed: %s", exc)
        return []


def _make_signal(
    hallucination_type: str,
    decision_id: str,
    consensus_score: float,
    sources: list,
    outcome: Optional[str],
    description: str,
    confidence: float,
) -> HallucinationSignal:
    return HallucinationSignal(
        schema_version=1,
        signal_id=str(uuid.uuid4()),
        detected_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        decision_id=decision_id,
        hallucination_type=hallucination_type,
        consensus_score=round(consensus_score, 3),
        signal_sources=sources,
        outcome=outcome,
        description=description[:200],
        confidence=confidence,
        evaluation_class="quality_positive_non_alpha",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Detection rules (no LLM)
# ─────────────────────────────────────────────────────────────────────────────

def _detect_all_signals_agree_bad_outcome(decisions: list, outcomes: dict) -> list:
    """Top 3 signal scores all > 0.7 AND outcome is alpha_negative."""
    signals = []
    try:
        for dec in decisions:
            decision_id = dec.get("decision_id", dec.get("id", ""))
            ideas = dec.get("ideas", [])
            if not ideas:
                continue

            convictions = []
            for idea in ideas:
                if not isinstance(idea, dict):
                    continue
                c = float(idea.get("conviction", idea.get("confidence", 0.0)) or 0.0)
                convictions.append(c)

            if len(convictions) < 1:
                continue
            top3 = sorted(convictions, reverse=True)[:3]
            if all(c > 0.7 for c in top3):
                outcome_rec = outcomes.get(decision_id, {})
                alpha_class = outcome_rec.get("alpha_classification", "")
                if alpha_class == "alpha_negative":
                    consensus_score = sum(top3) / len(top3)
                    signals.append(_make_signal(
                        hallucination_type=HallucinationType.ALL_SIGNALS_AGREE_BAD_OUTCOME,
                        decision_id=decision_id,
                        consensus_score=consensus_score,
                        sources=["signal_scores", "conviction"],
                        outcome=alpha_class,
                        description=f"All top signals high (avg {consensus_score:.2f}) but outcome=alpha_negative",
                        confidence=0.8,
                    ))
    except Exception as exc:
        log.debug("[CHD] _detect_all_signals_agree_bad_outcome failed: %s", exc)
    return signals


def _detect_morning_brief_echo(decisions: list) -> list:
    """Morning brief symbol, top signal, and regime bias all point the same direction."""
    signals = []
    try:
        brief_symbols = set(s.upper() for s in _load_morning_brief_symbols())
        if not brief_symbols:
            return signals

        for dec in decisions:
            decision_id = dec.get("decision_id", dec.get("id", ""))
            regime_view = str(dec.get("regime_view", dec.get("regime", "")) or "").lower()
            bullish_regime = any(w in regime_view for w in ("risk_on", "bullish", "risk_on"))
            if not bullish_regime:
                continue

            ideas = dec.get("ideas", [])
            if not ideas:
                continue

            # Find the top-conviction idea
            top_idea = max(
                (i for i in ideas if isinstance(i, dict)),
                key=lambda x: float(x.get("conviction", x.get("confidence", 0.0)) or 0.0),
                default=None,
            )
            if top_idea is None:
                continue

            sym = str(top_idea.get("symbol", "")).upper()
            direction = str(top_idea.get("direction", top_idea.get("intent", "")) or "").lower()
            is_bullish = any(w in direction for w in ("buy", "long", "enter_long"))

            if sym in brief_symbols and is_bullish and bullish_regime:
                signals.append(_make_signal(
                    hallucination_type=HallucinationType.MORNING_BRIEF_ECHO,
                    decision_id=decision_id,
                    consensus_score=0.85,
                    sources=["morning_brief", "signal_scorer", "regime"],
                    outcome=None,
                    description=f"{sym}: morning brief + signal score + regime all bullish — possible echo",
                    confidence=0.6,
                ))
    except Exception as exc:
        log.debug("[CHD] _detect_morning_brief_echo failed: %s", exc)
    return signals


def _detect_reddit_technical_alignment(decisions: list) -> list:
    """Reddit sentiment > +0.5 AND technical signal bullish AND regime bullish."""
    signals = []
    try:
        for dec in decisions:
            decision_id = dec.get("decision_id", dec.get("id", ""))
            reddit_sentiment = float(dec.get("reddit_sentiment", dec.get("social_sentiment", 0.0)) or 0.0)
            if reddit_sentiment <= 0.5:
                continue

            regime_view = str(dec.get("regime_view", dec.get("regime", "")) or "").lower()
            if not any(w in regime_view for w in ("risk_on", "bullish")):
                continue

            ideas = dec.get("ideas", [])
            has_technical = any(
                isinstance(i, dict) and
                str(i.get("catalyst", "") or "").lower() in ("technical_breakout", "momentum_continuation")
                for i in ideas
            )
            if has_technical:
                signals.append(_make_signal(
                    hallucination_type=HallucinationType.REDDIT_TECHNICAL_ALIGNMENT,
                    decision_id=decision_id,
                    consensus_score=0.9,
                    sources=["reddit_sentiment", "technical_signal", "regime"],
                    outcome=None,
                    description=f"Reddit sentiment {reddit_sentiment:.2f} + technical + bullish regime all align — suspicious",
                    confidence=0.65,
                ))
    except Exception as exc:
        log.debug("[CHD] _detect_reddit_technical_alignment failed: %s", exc)
    return signals


def _detect_single_source_amplification(decisions: list) -> list:
    """Same symbol in morning brief + top signal score + Claude's notes/concerns."""
    signals = []
    try:
        brief_symbols = set(s.upper() for s in _load_morning_brief_symbols())

        for dec in decisions:
            decision_id = dec.get("decision_id", dec.get("id", ""))
            ideas = dec.get("ideas", [])
            notes = str(dec.get("notes", dec.get("reasoning", "")) or "").lower()
            concerns = str(dec.get("concerns", "") or "")
            if isinstance(concerns, list):
                concerns = " ".join(str(c) for c in concerns)
            concerns = concerns.lower()

            for idea in ideas:
                if not isinstance(idea, dict):
                    continue
                sym = str(idea.get("symbol", "")).upper()
                if not sym or sym not in brief_symbols:
                    continue

                # Check if same symbol mentioned in notes AND concerns
                sym_lower = sym.lower()
                in_notes = sym_lower in notes
                in_concerns = sym_lower in concerns

                if in_notes and in_concerns:
                    signals.append(_make_signal(
                        hallucination_type=HallucinationType.SINGLE_SOURCE_AMPLIFICATION,
                        decision_id=decision_id,
                        consensus_score=0.8,
                        sources=["morning_brief", "signal_scorer", "claude_notes", "claude_concerns"],
                        outcome=None,
                        description=f"{sym}: appears in morning brief, signal, notes, and concerns — possible single-source amplification",
                        confidence=0.6,
                    ))
                    break  # one signal per decision
    except Exception as exc:
        log.debug("[CHD] _detect_single_source_amplification failed: %s", exc)
    return signals


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def detect_hallucinations(days_back: int = 30) -> list:
    """
    Run all detection rules. Returns list of HallucinationSignals. [] on error.
    Non-fatal.
    """
    if not _is_enabled():
        return []

    signals = []
    try:
        decisions = _load_decisions(days_back)
        outcomes = _load_outcomes(days_back)

        signals.extend(_detect_all_signals_agree_bad_outcome(decisions, outcomes))
        signals.extend(_detect_morning_brief_echo(decisions))
        signals.extend(_detect_reddit_technical_alignment(decisions))
        signals.extend(_detect_single_source_amplification(decisions))

        log.info("[CHD] detected %d hallucination signals from %d decisions",
                 len(signals), len(decisions))
    except Exception as exc:
        log.warning("[CHD] detect_hallucinations failed: %s", exc)

    return signals


def log_signal(signal: HallucinationSignal) -> Optional[str]:
    """Appends to data/annex/consensus_hallucination_detector/signals.jsonl. Returns signal_id or None."""
    try:
        _ANNEX_DIR.mkdir(parents=True, exist_ok=True)
        with open(_SIGNALS_LOG, "a") as fh:
            fh.write(json.dumps(asdict(signal)) + "\n")
        return signal.signal_id
    except Exception as exc:
        log.warning("[CHD] log_signal failed: %s", exc)
        return None


def format_hallucinations_for_review(days_back: int = 30) -> str:
    """
    Markdown summary grouped by hallucination_type.
    Returns "" on error or no signals.
    """
    try:
        signals = detect_hallucinations(days_back=days_back)
        if not signals:
            return ""

        by_type: dict = {}
        for sig in signals:
            by_type.setdefault(sig.hallucination_type, []).append(sig)

        lines = [
            f"## Consensus Hallucination Signals ({days_back}d)\n",
            f"Total signals: {len(signals)}\n",
        ]
        for htype, sigs in sorted(by_type.items(), key=lambda x: -len(x[1])):
            lines.append(f"**{htype}** — {len(sigs)} instance(s)")
            for sig in sigs[:2]:
                lines.append(f"  - {sig.description} (confidence={sig.confidence:.2f})")
            lines.append("")

        return "\n".join(lines)
    except Exception as exc:
        log.warning("[CHD] format_hallucinations_for_review failed: %s", exc)
        return ""
