"""
semantic_labels.py — Single canonical module for all label enums used across v2.
All other modules import from here. No duplicated enums anywhere else.

Labels align exactly with docs/taxonomy_v1.0.0.md (LOCKED v1.0.0).
Do not add or rename values without a taxonomy version bump.

SEMANTIC_LABELS_VERSION tracks this module's schema version.
"""

from __future__ import annotations

import logging
from enum import Enum

log = logging.getLogger(__name__)

SEMANTIC_LABELS_VERSION = 1


# ─────────────────────────────────────────────────────────────────────────────
# Enums — values must match taxonomy_v1.0.0.md exactly
# ─────────────────────────────────────────────────────────────────────────────

class CatalystType(str, Enum):
    """DIMENSION 1 — catalyst_type (taxonomy_v1.0.0.md)"""
    EARNINGS_BEAT          = "earnings_beat"
    EARNINGS_MISS          = "earnings_miss"
    GUIDANCE_RAISE         = "guidance_raise"
    GUIDANCE_CUT           = "guidance_cut"
    MACRO_PRINT            = "macro_print"
    FED_SIGNAL             = "fed_signal"
    GEOPOLITICAL           = "geopolitical"
    POLICY_CHANGE          = "policy_change"
    INSIDER_BUY            = "insider_buy"
    CONGRESSIONAL_BUY      = "congressional_buy"
    ANALYST_REVISION       = "analyst_revision"
    CORPORATE_ACTION       = "corporate_action"
    TECHNICAL_BREAKOUT     = "technical_breakout"
    MOMENTUM_CONTINUATION  = "momentum_continuation"
    MEAN_REVERSION         = "mean_reversion"
    SECTOR_ROTATION        = "sector_rotation"
    SOCIAL_SENTIMENT       = "social_sentiment"
    CITRINI_THESIS         = "citrini_thesis"
    UNKNOWN                = "unknown"


class RegimeType(str, Enum):
    """DIMENSION 2 — regime_label (taxonomy_v1.0.0.md)"""
    RISK_ON          = "risk_on"
    RISK_OFF         = "risk_off"
    VOLATILITY_SPIKE = "volatility_spike"
    CRISIS           = "crisis"
    LOW_CONVICTION   = "low_conviction"
    UNKNOWN          = "unknown"


class MoveType(str, Enum):
    """DIMENSION 3 — move_character (taxonomy_v1.0.0.md, multi-label allowed)"""
    REAL_INFORMATION    = "real_information"
    SQUEEZE             = "squeeze"
    RETAIL_REFLEXIVITY  = "retail_reflexivity"
    PASSIVE_FLOW        = "passive_flow"
    GAMMA_POSITIONING   = "gamma_positioning"
    SECTOR_SPILLOVER    = "sector_spillover"
    MACRO_REPRICE       = "macro_reprice"
    THIN_TAPE           = "thin_tape"
    UNKNOWN             = "unknown"


class ThesisType(str, Enum):
    """DIMENSION 4 — thesis_type (taxonomy_v1.0.0.md)"""
    MOMENTUM_CONTINUATION  = "momentum_continuation"
    MEAN_REVERSION         = "mean_reversion"
    CATALYST_SWING         = "catalyst_swing"
    SECTOR_ROTATION        = "sector_rotation"
    MACRO_OVERLAY          = "macro_overlay"
    VOLATILITY_EXPRESSION  = "volatility_expression"
    SAFE_HAVEN             = "safe_haven"
    UNKNOWN                = "unknown"


class CloseReasonType(str, Enum):
    """DIMENSION 5 — close_reason / semantic close reason (taxonomy_v1.0.0.md)"""
    STOP_HIT           = "stop_hit"
    TAKE_PROFIT_HIT    = "take_profit_hit"
    DEADLINE_EXIT      = "deadline_exit"
    THESIS_INVALIDATED = "thesis_invalidated"
    RISK_CONTAINMENT   = "risk_containment"
    REALLOCATION       = "reallocation"
    MANUAL_CLOSE       = "manual_close"
    EXPIRY             = "expiry"
    RECONCILE_CLOSE    = "reconcile_close"
    UNKNOWN            = "unknown"


class IncidentType(str, Enum):
    """
    Incident types for IncidentRecord (incident_schema.py).

    Not a taxonomy_v1.0.0.md dimension (taxonomy v1 does not define incident
    types). Values derived from divergence.py EVENT_TYPES and A2 lifecycle
    incidents. Requires taxonomy version bump to formalize as a dimension.
    """
    # Fill / price events
    FILL_PRICE_DRIFT         = "fill_price_drift"
    # Stop / protection events
    STOP_MISSING             = "stop_missing"
    PROTECTION_MISSING       = "protection_missing"
    DUPLICATE_EXIT           = "duplicate_exit"
    # Order events
    ORDER_REJECTED           = "order_rejected"
    # Position events
    POSITION_SIZE_ANOMALY    = "position_size_anomaly"
    POSITION_UNEXPECTED      = "position_unexpected"
    EXPOSURE_MISMATCH        = "exposure_mismatch"
    # Options structure events
    STRUCTURE_PARTIAL_FILL   = "structure_partial_fill"
    STRUCTURE_BROKEN         = "structure_broken"
    STRUCTURE_NEAR_EXPIRY    = "structure_near_expiry"
    STRUCTURE_CLOSE_FAILED   = "structure_close_failed"
    # Account events
    CASH_MISMATCH            = "cash_mismatch"
    BUYING_POWER_MISMATCH    = "buying_power_mismatch"
    POSITION_COUNT_MISMATCH  = "position_count_mismatch"
    # Deadline events
    DEADLINE_EXIT_FAILED     = "deadline_exit_failed"
    # Generic
    UNKNOWN                  = "unknown"


class CatalystFreshness(str, Enum):
    """Staleness of the catalyst driving a trade decision."""
    FRESH    = "fresh"     # < 30 minutes old
    RECENT   = "recent"    # 30 min – 4 hours
    STALE    = "stale"     # 4 – 24 hours
    EXPIRED  = "expired"   # > 24 hours


class HorizonType(str, Enum):
    """Time horizon classification for a trade or thesis."""
    INTRADAY    = "intraday"      # same session
    SWING       = "swing"         # 1–5 days
    POSITIONAL  = "positional"    # 1–4 weeks
    MACRO       = "macro"         # > 4 weeks


# ─────────────────────────────────────────────────────────────────────────────
# Alpha classification constants (alpha_measurement_framework_v1.0.0.md §9)
# ─────────────────────────────────────────────────────────────────────────────

ALPHA_CLASSIFICATIONS: list[str] = [
    "alpha_positive",
    "alpha_neutral",
    "alpha_negative",
    "quality_positive_non_alpha",
    "cost_positive_non_alpha",
    "reliability_positive_non_alpha",
    "insufficient_sample",
]


# ─────────────────────────────────────────────────────────────────────────────
# Validation helper
# ─────────────────────────────────────────────────────────────────────────────

def validate_label(enum_class, value: str, allow_unknown: bool = True) -> str:
    """
    Validate a label value against an enum class.
    allow_unknown=True: returns value unchanged if not in enum, logs WARNING.
    allow_unknown=False: raises ValueError if not in enum.
    Never raises when allow_unknown=True — non-fatal by design.
    """
    try:
        valid_values = {e.value for e in enum_class}
        if value in valid_values:
            return value
        if allow_unknown:
            log.warning(
                "[LABELS] Unknown label %r for %s — using as-is. "
                "Use 'unknown' or request taxonomy version bump.",
                value,
                enum_class.__name__,
            )
            return value
        raise ValueError(
            f"Label {value!r} is not a valid {enum_class.__name__} value. "
            f"Valid: {sorted(valid_values)}"
        )
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("[LABELS] validate_label failed: %s", exc)
        return value
