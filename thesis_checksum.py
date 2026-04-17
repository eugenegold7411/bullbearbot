"""
thesis_checksum.py — Trade-thesis checksum (T2.1).

Structured payload capturing thesis at entry time for forensic comparison.
Stored with the decision/outcome record.
Feature flag: enable_thesis_checksum.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import feature_flags
from semantic_labels import (
    CatalystFreshness,
    CatalystType,
    HorizonType,
    RegimeType,
    ThesisType,
)

log = logging.getLogger(__name__)

_CHECKSUM_PATH = Path("data/analytics/thesis_checksums.jsonl")

# ─────────────────────────────────────────────────────────────────────────────
# Derivation helpers
# ─────────────────────────────────────────────────────────────────────────────

def _infer_catalyst_type(raw_catalyst: str) -> str:
    """Best-effort mapping from raw catalyst string to CatalystType."""
    try:
        from catalyst_normalizer import _match_catalyst_type  # noqa: PLC0415
        ctype, _ = _match_catalyst_type(raw_catalyst)
        return ctype
    except Exception:
        return CatalystType.UNKNOWN.value


def _infer_thesis_type(idea: dict, catalyst_type: str) -> str:
    """Infer ThesisType from idea dict and catalyst type."""
    tier = str(idea.get("tier", "")).lower()
    catalyst_lower = str(idea.get("catalyst", "")).lower()

    if catalyst_type in (CatalystType.EARNINGS_BEAT.value, CatalystType.EARNINGS_MISS.value,
                         CatalystType.GUIDANCE_RAISE.value, CatalystType.GUIDANCE_CUT.value,
                         CatalystType.CORPORATE_ACTION.value):
        return ThesisType.CATALYST_SWING.value
    if catalyst_type in (CatalystType.MACRO_PRINT.value, CatalystType.FED_SIGNAL.value,
                         CatalystType.CITRINI_THESIS.value, CatalystType.GEOPOLITICAL.value,
                         CatalystType.POLICY_CHANGE.value):
        return ThesisType.MACRO_OVERLAY.value
    if catalyst_type == CatalystType.MOMENTUM_CONTINUATION.value:
        return ThesisType.MOMENTUM_CONTINUATION.value
    if catalyst_type == CatalystType.MEAN_REVERSION.value:
        return ThesisType.MEAN_REVERSION.value
    if catalyst_type == CatalystType.SECTOR_ROTATION.value:
        return ThesisType.SECTOR_ROTATION.value
    return ThesisType.UNKNOWN.value


def _infer_horizon(idea: dict, thesis_type: str) -> str:
    """Infer HorizonType from tier and thesis."""
    tier = str(idea.get("tier", "")).lower()
    if tier == "intraday":
        return HorizonType.INTRADAY.value
    if tier == "dynamic":
        return HorizonType.SWING.value
    if thesis_type in (ThesisType.MACRO_OVERLAY.value,):
        return HorizonType.MACRO.value
    if tier == "core":
        return HorizonType.POSITIONAL.value
    return HorizonType.SWING.value


def _infer_regime(regime_obj: dict) -> str:
    """Map regime_obj to RegimeType. Taxonomy regime labels are not the same as macro labels."""
    bias = str(regime_obj.get("bias", "")).lower()
    regime_score = int(regime_obj.get("regime_score", 50))
    vix = float(regime_obj.get("vix", 20))

    if vix > 35:
        return RegimeType.CRISIS.value
    if vix > 22:
        return RegimeType.VOLATILITY_SPIKE.value
    if bias in ("bullish", "risk_on"):
        return RegimeType.RISK_ON.value
    if bias in ("bearish", "risk_off", "defensive"):
        return RegimeType.RISK_OFF.value
    if regime_score < 30:
        return RegimeType.LOW_CONVICTION.value
    return RegimeType.UNKNOWN.value


def _build_invalidation_condition(catalyst_type: str, idea: dict) -> str:
    """Template-based invalidation condition sentence."""
    action = str(idea.get("action", "buy")).lower()
    direction = "reversal" if action in ("sell", "close") else "breakdown"
    catalyst = str(idea.get("catalyst", "thesis"))[:60]
    return f"Thesis invalidated if {catalyst[:50]} reverses or fails to materialize within hold horizon."


def _build_key_assumption(idea: dict) -> str:
    """First meaningful sentence from catalyst text as the key assumption."""
    catalyst = str(idea.get("catalyst", "")).strip()
    if not catalyst:
        return "Catalyst-driven price move continues in intended direction."
    # Use first 120 chars
    return catalyst[:120] if len(catalyst) <= 120 else catalyst[:117] + "..."


# ─────────────────────────────────────────────────────────────────────────────
# Dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ThesisChecksum:
    schema_version: int = 1
    checksum_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    decision_id: str = ""
    symbol: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    thesis_type: str = ThesisType.UNKNOWN.value
    catalyst_type: str = CatalystType.UNKNOWN.value
    catalyst_freshness: str = CatalystFreshness.FRESH.value
    intended_horizon: str = HorizonType.SWING.value
    invalidation_condition: str = ""
    key_assumption: str = ""
    hidden_dependency: Optional[str] = None
    regime_at_entry: Optional[str] = None
    signal_score_at_entry: Optional[float] = None
    raw_catalyst_text: str = ""

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "checksum_id": self.checksum_id,
            "decision_id": self.decision_id,
            "symbol": self.symbol,
            "created_at": self.created_at,
            "thesis_type": self.thesis_type,
            "catalyst_type": self.catalyst_type,
            "catalyst_freshness": self.catalyst_freshness,
            "intended_horizon": self.intended_horizon,
            "invalidation_condition": self.invalidation_condition,
            "key_assumption": self.key_assumption,
            "hidden_dependency": self.hidden_dependency,
            "regime_at_entry": self.regime_at_entry,
            "signal_score_at_entry": self.signal_score_at_entry,
            "raw_catalyst_text": self.raw_catalyst_text,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ThesisChecksum":
        return cls(
            schema_version=d.get("schema_version", 1),
            checksum_id=d.get("checksum_id", str(uuid.uuid4())),
            decision_id=d.get("decision_id", ""),
            symbol=d.get("symbol", ""),
            created_at=d.get("created_at", ""),
            thesis_type=d.get("thesis_type", ThesisType.UNKNOWN.value),
            catalyst_type=d.get("catalyst_type", CatalystType.UNKNOWN.value),
            catalyst_freshness=d.get("catalyst_freshness", CatalystFreshness.FRESH.value),
            intended_horizon=d.get("intended_horizon", HorizonType.SWING.value),
            invalidation_condition=d.get("invalidation_condition", ""),
            key_assumption=d.get("key_assumption", ""),
            hidden_dependency=d.get("hidden_dependency"),
            regime_at_entry=d.get("regime_at_entry"),
            signal_score_at_entry=d.get("signal_score_at_entry"),
            raw_catalyst_text=d.get("raw_catalyst_text", ""),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def build_checksum_from_decision(
    decision_id: str,
    symbol: str,
    idea: dict,
    regime_obj: dict,
    signal_scores: dict,
) -> Optional[ThesisChecksum]:
    """
    Constructs ThesisChecksum from submitted trade decision data.
    Returns None if decision_id or symbol missing. Non-fatal.
    """
    try:
        if not decision_id or not symbol:
            return None

        raw_catalyst = str(idea.get("catalyst", ""))
        catalyst_type = _infer_catalyst_type(raw_catalyst)
        thesis_type = _infer_thesis_type(idea, catalyst_type)
        horizon = _infer_horizon(idea, thesis_type)
        regime = _infer_regime(regime_obj) if regime_obj else RegimeType.UNKNOWN.value

        # Signal score lookup
        scored = (signal_scores or {}).get("scored_symbols", {})
        score_entry = scored.get(symbol) or scored.get(symbol.replace("/", ""))
        signal_score = None
        if isinstance(score_entry, dict):
            signal_score = score_entry.get("score")
        elif isinstance(score_entry, (int, float)):
            signal_score = float(score_entry)

        return ThesisChecksum(
            decision_id=decision_id,
            symbol=symbol,
            thesis_type=thesis_type,
            catalyst_type=catalyst_type,
            catalyst_freshness=CatalystFreshness.FRESH.value,
            intended_horizon=horizon,
            invalidation_condition=_build_invalidation_condition(catalyst_type, idea),
            key_assumption=_build_key_assumption(idea),
            hidden_dependency=None,
            regime_at_entry=regime,
            signal_score_at_entry=float(signal_score) if signal_score is not None else None,
            raw_catalyst_text=raw_catalyst,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("[CHECKSUM] build_checksum_from_decision failed: %s", exc)
        return None


def log_checksum(checksum: ThesisChecksum) -> Optional[str]:
    """Appends to thesis_checksums.jsonl. Returns checksum_id or None."""
    try:
        _CHECKSUM_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _CHECKSUM_PATH.open("a") as f:
            f.write(json.dumps(checksum.to_dict()) + "\n")
        return checksum.checksum_id
    except Exception as exc:
        log.warning("[CHECKSUM] log_checksum failed: %s", exc)
        return None


def get_checksum(decision_id: str) -> Optional[ThesisChecksum]:
    """Reads JSONL, finds first matching decision_id. Returns None if not found."""
    try:
        if not _CHECKSUM_PATH.exists():
            return None
        for line in _CHECKSUM_PATH.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if d.get("decision_id") == decision_id:
                    return ThesisChecksum.from_dict(d)
            except Exception:
                continue
        return None
    except Exception as exc:
        log.warning("[CHECKSUM] get_checksum failed: %s", exc)
        return None


def get_checksums(
    symbol: Optional[str] = None,
    thesis_type: Optional[str] = None,
    days_back: int = 90,
) -> list[ThesisChecksum]:
    """Filters by symbol and/or thesis_type. Returns [] on error."""
    try:
        if not _CHECKSUM_PATH.exists():
            return []
        from datetime import timedelta as _td  # noqa: PLC0415
        cutoff = datetime.now(timezone.utc) - _td(days=days_back)
        results = []
        for line in _CHECKSUM_PATH.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                ts_str = d.get("created_at", "")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if ts < cutoff:
                            continue
                    except Exception:
                        pass
                if symbol and d.get("symbol") != symbol:
                    continue
                if thesis_type and d.get("thesis_type") != thesis_type:
                    continue
                results.append(ThesisChecksum.from_dict(d))
            except Exception:
                continue
        return results
    except Exception as exc:
        log.warning("[CHECKSUM] get_checksums failed: %s", exc)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# versioning.py migration registration (T0.5)
# ─────────────────────────────────────────────────────────────────────────────

def _migrate_thesis_checksum_v0_to_v1(artifact: dict) -> dict:
    """v0→v1: add schema_version=1."""
    result = dict(artifact)
    result["schema_version"] = 1
    if "intended_horizon" not in result:
        result["intended_horizon"] = HorizonType.SWING.value
    return result


try:
    from versioning import register_migration as _rm  # noqa: PLC0415
    _rm("thesis_checksum", 0, _migrate_thesis_checksum_v0_to_v1)
except Exception as _ve:
    log.warning("[CHECKSUM] versioning registration failed (non-fatal): %s", _ve)
