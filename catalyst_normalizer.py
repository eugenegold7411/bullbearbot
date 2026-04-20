"""
catalyst_normalizer.py — Structured catalyst normalizer (T2.2).

Rule-based normalization of raw catalyst strings to CatalystObject.
No LLM calls. No spine attribution. Raw text always preserved.
Feature flag: enable_thesis_checksum (shared with T2.1).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from semantic_labels import CatalystFreshness, CatalystType

log = logging.getLogger(__name__)

_CATALYST_LOG = Path("data/analytics/catalyst_log.jsonl")

# ─────────────────────────────────────────────────────────────────────────────
# Keyword matching rules (taxonomy_v1.0.0.md DIMENSION 1)
# ─────────────────────────────────────────────────────────────────────────────

_CATALYST_KEYWORDS: dict[str, list[str]] = {
    CatalystType.EARNINGS_BEAT.value:        ["beat", "beat consensus", "earnings beat", "above expectations", "outperform"],
    CatalystType.EARNINGS_MISS.value:        ["miss", "earnings miss", "below expectations", "disappointment", "missed"],
    CatalystType.GUIDANCE_RAISE.value:       ["guidance raise", "raised guidance", "raised outlook", "raised forecast", "raised full year"],
    CatalystType.GUIDANCE_CUT.value:         ["guidance cut", "cut guidance", "lowered guidance", "reduced guidance", "lowered outlook"],
    CatalystType.MACRO_PRINT.value:          ["cpi", "ppi", "nfp", "jobs report", "gdp", "inflation", "economic data", "macro data", "pce", "ism"],
    CatalystType.FED_SIGNAL.value:           ["fed", "fomc", "powell", "rate cut", "rate hike", "rate decision", "minutes", "federal reserve", "dovish", "hawkish"],
    CatalystType.GEOPOLITICAL.value:         ["war", "sanctions", "military", "geopolitical", "conflict", "iran", "russia", "ukraine", "middle east", "diplomatic"],
    CatalystType.POLICY_CHANGE.value:        ["tariff", "executive order", "regulation", "policy", "legislation", "tax", "deregulation", "government action"],
    CatalystType.INSIDER_BUY.value:          ["insider buy", "form 4", "c-suite", "ceo buy", "cfo buy", "insider purchase"],
    CatalystType.CONGRESSIONAL_BUY.value:    ["congressional", "congress", "senate", "pelosi", "politician", "disclosure"],
    CatalystType.ANALYST_REVISION.value:     ["analyst", "upgrade", "downgrade", "target", "initiation", "raise target", "cut target", "rating"],
    CatalystType.CORPORATE_ACTION.value:     ["m&a", "merger", "acquisition", "buyback", "spinoff", "contract", "settlement", "deal", "tender offer"],
    CatalystType.TECHNICAL_BREAKOUT.value:   ["breakout", "break out", "broke out", "resistance", "support", "technical", "level", "ath", "all-time high"],
    CatalystType.MOMENTUM_CONTINUATION.value: ["momentum", "continuation", "trend", "trending", "follow through", "higher low"],
    CatalystType.MEAN_REVERSION.value:       ["mean reversion", "oversold", "overbought", "revert", "bounce", "stretched", "extreme"],
    CatalystType.SECTOR_ROTATION.value:      ["rotation", "sector", "flows", "capital rotation", "rotating into", "rotating out"],
    CatalystType.SOCIAL_SENTIMENT.value:     ["reddit", "wsb", "wallstreetbets", "social", "mentions", "viral", "trending"],
    CatalystType.CITRINI_THESIS.value:       ["citrini", "citrini thesis", "macro overlay"],
}

# Proper nouns / named events that indicate is_named=True
_NAMED_EVENT_PATTERNS = [
    "earnings", "q1", "q2", "q3", "q4", "fomc", "fed", "cpi", "ppi", "nfp", "gdp",
    "acquisition", "merger", "ipo", "spinoff", "buyback",
    "powell", "russia", "iran", "ukraine", "china",
]


# ─────────────────────────────────────────────────────────────────────────────
# Dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CatalystObject:
    schema_version: int = 1
    catalyst_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    decision_id: str = ""
    symbol: str = ""
    normalized_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    raw_text: str = ""
    catalyst_type: str = CatalystType.UNKNOWN.value
    catalyst_freshness: str = CatalystFreshness.FRESH.value
    is_named: bool = False
    is_verifiable: bool = False
    confidence: float = 0.1
    abstention: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "catalyst_id": self.catalyst_id,
            "decision_id": self.decision_id,
            "symbol": self.symbol,
            "normalized_at": self.normalized_at,
            "raw_text": self.raw_text,
            "catalyst_type": self.catalyst_type,
            "catalyst_freshness": self.catalyst_freshness,
            "is_named": self.is_named,
            "is_verifiable": self.is_verifiable,
            "confidence": self.confidence,
            "abstention": self.abstention,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CatalystObject":
        return cls(
            schema_version=d.get("schema_version", 1),
            catalyst_id=d.get("catalyst_id", str(uuid.uuid4())),
            decision_id=d.get("decision_id", ""),
            symbol=d.get("symbol", ""),
            normalized_at=d.get("normalized_at", ""),
            raw_text=d.get("raw_text", ""),
            catalyst_type=d.get("catalyst_type", CatalystType.UNKNOWN.value),
            catalyst_freshness=d.get("catalyst_freshness", CatalystFreshness.FRESH.value),
            is_named=d.get("is_named", False),
            is_verifiable=d.get("is_verifiable", False),
            confidence=d.get("confidence", 0.1),
            abstention=d.get("abstention"),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Normalization logic
# ─────────────────────────────────────────────────────────────────────────────

def _load_disallowed_values() -> list[str]:
    try:
        cfg = json.loads(Path("strategy_config.json").read_text())
        vals = cfg.get("parameters", {}).get("catalyst_tag_disallowed_values", [])
        return [str(v).lower() for v in vals if v is not None]
    except Exception:
        return ["no", "none", "null", ""]


def _match_catalyst_type(text: str) -> tuple[str, float]:
    """Returns (catalyst_type_value, confidence). Exact=0.9, partial=0.6, none=0.1."""
    lower = text.lower()
    best_type = CatalystType.UNKNOWN.value
    best_score = 0.0

    for ctype, keywords in _CATALYST_KEYWORDS.items():
        for kw in keywords:
            if kw in lower:
                # Exact multi-word match scores higher
                score = 0.9 if len(kw.split()) >= 2 else 0.6
                if score > best_score:
                    best_score = score
                    best_type = ctype
                    break

    confidence = best_score if best_score > 0.0 else 0.1
    return best_type, confidence


def _is_named(text: str) -> bool:
    lower = text.lower()
    return any(p in lower for p in _NAMED_EVENT_PATTERNS)


def normalize_catalyst(
    raw_text: str,
    decision_id: str,
    symbol: str,
) -> CatalystObject:
    """
    Rule-based catalyst normalization. Never raises.
    Returns CatalystObject with abstention set if raw_text is empty/disallowed.
    """
    try:
        obj = CatalystObject(
            decision_id=decision_id,
            symbol=symbol,
            raw_text=raw_text,
        )

        disallowed = _load_disallowed_values()
        text_stripped = (raw_text or "").strip()
        text_lower = text_stripped.lower()

        if not text_stripped or text_lower in disallowed:
            try:
                from abstention import abstain as _abstain  # noqa: PLC0415
                _ab = _abstain(
                    reason="catalyst text is empty or disallowed",
                    module_name="catalyst_normalizer",
                    unknown=True,
                )
                obj.abstention = {
                    "abstain": _ab.abstain,
                    "reason": _ab.abstention_reason,
                    "unknown": _ab.unknown,
                }
            except Exception:
                obj.abstention = {"abstain": True, "reason": "empty or disallowed", "unknown": True}
            obj.catalyst_type = CatalystType.UNKNOWN.value
            obj.confidence = 0.0
            obj.is_named = False
            obj.is_verifiable = False
            return obj

        catalyst_type, confidence = _match_catalyst_type(text_stripped)
        named = _is_named(text_stripped)
        verifiable = (catalyst_type != CatalystType.UNKNOWN.value) and named

        obj.catalyst_type = catalyst_type
        obj.confidence = confidence
        obj.is_named = named
        obj.is_verifiable = verifiable
        # Default freshness — caller can override
        obj.catalyst_freshness = CatalystFreshness.FRESH.value
        return obj

    except Exception as exc:  # noqa: BLE001
        log.warning("[CATALYST] normalize_catalyst failed: %s", exc)
        return CatalystObject(
            decision_id=decision_id,
            symbol=symbol,
            raw_text=raw_text or "",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Storage
# ─────────────────────────────────────────────────────────────────────────────

def log_catalyst(obj: CatalystObject) -> Optional[str]:
    """Appends to catalyst_log.jsonl. Returns catalyst_id or None."""
    try:
        _CATALYST_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _CATALYST_LOG.open("a") as f:
            f.write(json.dumps(obj.to_dict()) + "\n")
        return obj.catalyst_id
    except Exception as exc:
        log.warning("[CATALYST] log_catalyst failed: %s", exc)
        return None


def get_catalyst(decision_id: str) -> Optional[CatalystObject]:
    """Reads log, finds first matching decision_id. Returns None if not found."""
    try:
        if not _CATALYST_LOG.exists():
            return None
        for line in _CATALYST_LOG.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if d.get("decision_id") == decision_id:
                    return CatalystObject.from_dict(d)
            except Exception:
                continue
        return None
    except Exception as exc:
        log.warning("[CATALYST] get_catalyst failed: %s", exc)
        return None
