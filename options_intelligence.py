"""
options_intelligence.py — Strategy selection engine for Account 2 options trading.

Takes an IV summary, signal data, and market context and returns a StructureProposal
(strategy family, direction, DTE target, and budget). No strikes, expiry, or contract
counts — those are resolved by options_builder against the live chain.

Strategy hierarchy (IV-first):
  very_cheap / cheap  (<35 rank) → buy premium: ATM debit spread or single leg
  neutral             (35-65)    → spreads preferred: debit or credit spread
  expensive / very_expensive (>65) → sell premium: credit spread
  unknown (obs mode)             → HOLD — insufficient IV history
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from schemas import Direction, OptionStrategy, StructureProposal

log = logging.getLogger(__name__)

# Days to expiration targets by strategy
_DTE_DEBIT_SPREAD    = (14, 21)   # 2-3 week expiry
_DTE_SINGLE_LEG      = (7, 14)    # same-week to 2-week
_DTE_CREDIT_SPREAD   = (21, 45)   # 3-6 weeks for theta decay
_DTE_STRADDLE        = (14, 28)   # need time for move
_DTE_DYNAMIC_SINGLE  = (3, 7)     # same-week expiry for dynamic tier

# Delta targets
_DELTA_ATM           = 0.45        # near ATM
_DELTA_OTM_CREDIT    = 0.25        # OTM for credit spreads
_DELTA_MIN           = 0.30        # minimum delta per system rules


def select_options_strategy(
    symbol: str,
    iv_summary: dict,
    signal_data: dict,
    vix: float,
    tier: str,
    catalyst: str,
    current_price: float,
    equity: float,
    options_regime: dict,
) -> Optional[StructureProposal]:
    """
    Core strategy selector. Returns a StructureProposal or None (hold/skip).

    Args:
        symbol: ticker
        iv_summary: from options_data.get_iv_summary()
        signal_data: score/confidence from bot scoring
        vix: current VIX level
        tier: "core" | "dynamic"
        catalyst: named catalyst string
        current_price: spot price (unused — kept for API stability)
        equity: Account 2 total equity
        options_regime: from options_data.get_options_regime(vix)

    Returns StructureProposal or None.
    None means hold/skip — caller should treat as no candidate.
    """
    env = iv_summary.get("iv_environment", "unknown")
    iv_rank = iv_summary.get("iv_rank")
    obs_mode = iv_summary.get("observation_mode", True)
    score = float(signal_data.get("score", 0))
    confidence = signal_data.get("confidence", "low")

    # Observation mode — no trades, just log
    if obs_mode:
        log.debug(
            "[OPTIONS_INTEL] hold: obs_mode symbol=%s history_days=%s",
            symbol, iv_summary.get("history_days", 0),
        )
        return None

    # Crisis regime — no options
    if options_regime.get("regime") == "crisis":
        log.debug("[OPTIONS_INTEL] hold: crisis_regime symbol=%s", symbol)
        return None

    # Minimum confidence gate
    conf_map = {"high": 0.9, "medium": 0.75, "low": 0.5}
    conf_score = conf_map.get(str(confidence).lower(), 0.5)
    if conf_score < 0.75:
        log.debug(
            "[OPTIONS_INTEL] hold: low_confidence symbol=%s conf=%s",
            symbol, confidence,
        )
        return None

    # Dynamic tier — single leg only, same-week expiry
    if tier == "dynamic":
        return _dynamic_single_leg(
            symbol, iv_summary, signal_data, equity, catalyst, int(score)
        )

    # Core tier — full strategy selection
    allowed = options_regime.get("allowed_strategies", [])
    size_mult = options_regime.get("size_multiplier", 1.0)

    if env in ("very_cheap", "cheap"):
        # Buy premium
        if "debit_spread" in allowed:
            return _buy_premium_strategy(
                symbol, iv_summary, signal_data, equity, catalyst,
                size_mult, int(score), prefer_spread=True
            )
        elif "single_leg" in allowed:
            return _buy_premium_strategy(
                symbol, iv_summary, signal_data, equity, catalyst,
                size_mult, int(score), prefer_spread=False
            )

    elif env == "neutral":
        # Prefer spreads — debit or credit based on signal direction
        if "debit_spread" in allowed:
            return _spread_strategy(
                symbol, iv_summary, signal_data, equity, catalyst,
                size_mult, int(score)
            )

    elif env in ("expensive", "very_expensive"):
        # Sell premium
        if "credit_spread" in allowed:
            return _sell_premium_strategy(
                symbol, iv_summary, signal_data, equity, catalyst,
                size_mult, int(score)
            )

    log.debug(
        "[OPTIONS_INTEL] hold: no_eligible_strategy symbol=%s iv_env=%s allowed=%s",
        symbol, env, allowed,
    )
    return None


def _buy_premium_strategy(
    symbol: str,
    iv_summary: dict,
    signal_data: dict,
    equity: float,
    catalyst: str,
    size_mult: float,
    signal_score: int,
    prefer_spread: bool = True,
) -> StructureProposal:
    """ATM debit spread or single leg when IV is cheap."""
    iv_rank = iv_summary.get("iv_rank", 50)
    direction = signal_data.get("direction", "bullish")

    # Max cost: 5% of equity for core spread, 3% for single leg
    if prefer_spread:
        max_cost = equity * 0.05 * size_mult
        strategy = "call_debit_spread" if direction == "bullish" else "put_debit_spread"
        dte_range = _DTE_DEBIT_SPREAD
    else:
        max_cost = equity * 0.03 * size_mult
        strategy = "single_call" if direction == "bullish" else "single_put"
        dte_range = _DTE_SINGLE_LEG

    dir_enum = Direction(direction) if direction in ("bullish", "bearish", "neutral") else Direction.NEUTRAL
    return StructureProposal(
        symbol=symbol,
        strategy=OptionStrategy(strategy),
        direction=dir_enum,
        conviction=0.80,
        iv_rank=float(iv_rank),
        max_cost_usd=round(max_cost, 2),
        target_dte_min=dte_range[0],
        target_dte_max=dte_range[1],
        rationale=(
            f"IV rank {iv_rank:.0f} — cheap premium environment. "
            f"{strategy.replace('_', ' ').title()} targeting {direction} move. "
            f"Catalyst: {catalyst}."
        ),
        signal_score=signal_score,
        proposed_at=datetime.now(timezone.utc).isoformat(),
    )


def _spread_strategy(
    symbol: str,
    iv_summary: dict,
    signal_data: dict,
    equity: float,
    catalyst: str,
    size_mult: float,
    signal_score: int,
) -> StructureProposal:
    """Debit or credit spread for neutral IV environment."""
    iv_rank = iv_summary.get("iv_rank", 50)
    direction = signal_data.get("direction", "bullish")

    strategy = "call_debit_spread" if direction == "bullish" else "put_debit_spread"
    max_cost = equity * 0.05 * size_mult
    dir_enum = Direction(direction) if direction in ("bullish", "bearish", "neutral") else Direction.NEUTRAL
    return StructureProposal(
        symbol=symbol,
        strategy=OptionStrategy(strategy),
        direction=dir_enum,
        conviction=0.78,
        iv_rank=float(iv_rank),
        max_cost_usd=round(max_cost, 2),
        target_dte_min=_DTE_DEBIT_SPREAD[0],
        target_dte_max=_DTE_DEBIT_SPREAD[1],
        rationale=(
            f"IV rank {iv_rank:.0f} — neutral IV, spreads preferred. "
            f"{strategy.replace('_', ' ').title()} on {direction} catalyst. "
            f"Catalyst: {catalyst}."
        ),
        signal_score=signal_score,
        proposed_at=datetime.now(timezone.utc).isoformat(),
    )


def _sell_premium_strategy(
    symbol: str,
    iv_summary: dict,
    signal_data: dict,
    equity: float,
    catalyst: str,
    size_mult: float,
    signal_score: int,
) -> StructureProposal:
    """OTM credit spread when IV is expensive."""
    iv_rank = iv_summary.get("iv_rank", 75)
    direction = signal_data.get("direction", "bullish")

    if direction == "bullish":
        strategy = "put_credit_spread"
    else:
        strategy = "call_credit_spread"

    max_cost = equity * 0.04 * size_mult   # max risk budget for credit spreads
    dir_enum = Direction(direction) if direction in ("bullish", "bearish", "neutral") else Direction.NEUTRAL
    return StructureProposal(
        symbol=symbol,
        strategy=OptionStrategy(strategy),
        direction=dir_enum,
        conviction=0.75,
        iv_rank=float(iv_rank),
        max_cost_usd=round(max_cost, 2),
        target_dte_min=_DTE_CREDIT_SPREAD[0],
        target_dte_max=_DTE_CREDIT_SPREAD[1],
        rationale=(
            f"IV rank {iv_rank:.0f} — expensive premium, sell premium. "
            f"{strategy.replace('_', ' ').title()}. "
            f"IV will compress post-catalyst. Catalyst: {catalyst}."
        ),
        signal_score=signal_score,
        proposed_at=datetime.now(timezone.utc).isoformat(),
    )


def _dynamic_single_leg(
    symbol: str,
    iv_summary: dict,
    signal_data: dict,
    equity: float,
    catalyst: str,
    signal_score: int,
) -> StructureProposal:
    """Same-week single-leg for dynamic tier momentum plays."""
    iv_rank = iv_summary.get("iv_rank", 50)
    direction = signal_data.get("direction", "bullish")

    strategy = "single_call" if direction == "bullish" else "single_put"
    max_cost = equity * 0.03  # 3% max for dynamic
    dir_enum = Direction(direction) if direction in ("bullish", "bearish", "neutral") else Direction.NEUTRAL
    return StructureProposal(
        symbol=symbol,
        strategy=OptionStrategy(strategy),
        direction=dir_enum,
        conviction=0.76,
        iv_rank=float(iv_rank),
        max_cost_usd=round(max_cost, 2),
        target_dte_min=_DTE_DYNAMIC_SINGLE[0],
        target_dte_max=_DTE_DYNAMIC_SINGLE[1],
        rationale=(
            f"Dynamic tier momentum play. {strategy.replace('_', ' ').title()}, "
            f"same-week expiry. IV rank {iv_rank:.0f}. Catalyst: {catalyst}."
        ),
        signal_score=signal_score,
        proposed_at=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# Expiration selection
# ---------------------------------------------------------------------------

def select_expiration(dte_range: tuple[int, int], expirations: list[str]) -> str | None:
    """
    Select best expiration from available chain dates given target DTE range.
    Returns expiration date string (YYYY-MM-DD) or None.
    """
    from datetime import date
    today = date.today()
    target_min, target_max = dte_range

    best = None
    best_dte = None

    for exp_str in sorted(expirations):
        try:
            exp_date = date.fromisoformat(exp_str)
            dte = (exp_date - today).days
            if target_min <= dte <= target_max:
                if best_dte is None or abs(dte - (target_min + target_max) // 2) < abs(best_dte - (target_min + target_max) // 2):
                    best = exp_str
                    best_dte = dte
        except Exception:
            continue

    return best
