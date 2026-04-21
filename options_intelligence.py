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
from datetime import date, datetime, timezone
from typing import Optional
from uuid import uuid4

from schemas import A2FeaturePack, Direction, OptionStrategy, StructureProposal

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
    iv_summary.get("iv_rank")
    obs_mode = iv_summary.get("observation_mode", True)
    score = float(signal_data.get("score", 0))
    confidence = signal_data.get("conviction", signal_data.get("confidence", "low"))

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
# A2-2: Candidate structure generator
# ---------------------------------------------------------------------------

_STRUCTURE_MAP: dict[str, OptionStrategy] = {
    "long_call":          OptionStrategy.SINGLE_CALL,
    "long_put":           OptionStrategy.SINGLE_PUT,
    "debit_call_spread":  OptionStrategy.CALL_DEBIT_SPREAD,
    "debit_put_spread":   OptionStrategy.PUT_DEBIT_SPREAD,
    "credit_call_spread": OptionStrategy.CALL_CREDIT_SPREAD,
    "credit_put_spread":  OptionStrategy.PUT_CREDIT_SPREAD,
}

_DTE_RANGE: dict[OptionStrategy, tuple[int, int]] = {
    OptionStrategy.SINGLE_CALL:        (5, 21),
    OptionStrategy.SINGLE_PUT:         (5, 21),
    OptionStrategy.CALL_DEBIT_SPREAD:  (5, 28),
    OptionStrategy.PUT_DEBIT_SPREAD:   (5, 28),
    OptionStrategy.CALL_CREDIT_SPREAD: (5, 45),
    OptionStrategy.PUT_CREDIT_SPREAD:  (5, 45),
}


def _float_or_none(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def generate_candidate_structures(
    pack: A2FeaturePack,
    allowed_structures: list[str],
    equity: float,
    chain: dict,
) -> list[dict]:
    """
    Generate fully-specified candidate structures from live chain data.

    For each allowed structure type, resolves expiry + strikes via options_builder,
    computes economics, and builds a candidate dict with full risk/greek metadata.
    Returns 0-N candidates. Non-fatal per structure — failures logged at DEBUG.

    Candidate dict keys:
        candidate_id, structure_type, symbol, expiry, long_strike, short_strike,
        contracts, debit, max_loss, max_gain, breakeven, delta, theta, vega,
        probability_profit, expected_value, liquidity_score, bid_ask_spread_pct,
        open_interest, dte
    """
    import options_builder as _ob  # noqa: PLC0415

    candidates: list[dict] = []
    spot = chain.get("current_price")
    if not spot or float(spot) <= 0:
        log.debug("[GEN_CAND] %s: no current_price in chain", pack.symbol)
        return []

    spot = float(spot)
    today = date.today()

    for struct_name in allowed_structures:
        strategy = _STRUCTURE_MAP.get(struct_name)
        if strategy is None:
            log.debug("[GEN_CAND] %s: unknown structure type %r", pack.symbol, struct_name)
            continue

        try:
            dte_min, dte_max = _DTE_RANGE.get(strategy, (5, 28))
            expiry = _ob.select_expiry(chain, dte_min, dte_max)
            if expiry is None:
                log.debug("[GEN_CAND] %s %s: no expiry in DTE %d-%d",
                          pack.symbol, struct_name, dte_min, dte_max)
                continue

            strikes_data = _ob.select_strikes(
                chain, expiry, strategy, spot, pack.a1_direction, {}
            )
            if strikes_data is None:
                log.debug("[GEN_CAND] %s %s: select_strikes=None", pack.symbol, struct_name)
                continue

            economics = _ob.compute_economics(strategy, strikes_data)
            net_debit = economics.get("net_debit")
            if net_debit is None:
                log.debug("[GEN_CAND] %s %s: net_debit=None", pack.symbol, struct_name)
                continue

            max_loss_per = economics.get("max_loss") or (abs(net_debit) if net_debit else None)
            if not max_loss_per or max_loss_per <= 0:
                continue

            budget = min(pack.premium_budget_usd, equity * 0.05)
            cost_per = (abs(net_debit) if net_debit > 0 else max_loss_per) * 100
            contracts = max(1, min(10, int(budget / cost_per))) if cost_per > 0 else 1

            max_loss_usd = max_loss_per * contracts * 100
            max_gain_raw = economics.get("max_profit")
            max_gain_usd = (max_gain_raw * contracts * 100) if max_gain_raw is not None else None

            long_strike = float(strikes_data.get("long_strike_price") or 0)
            short_strike = strikes_data.get("short_strike_price")
            if short_strike is not None:
                short_strike = float(short_strike)

            if strategy in (OptionStrategy.SINGLE_CALL,
                            OptionStrategy.CALL_DEBIT_SPREAD,
                            OptionStrategy.CALL_CREDIT_SPREAD):
                breakeven = long_strike + abs(net_debit)
            else:
                breakeven = long_strike - abs(net_debit)

            dte = (date.fromisoformat(expiry) - today).days

            long_leg = strikes_data.get("long_leg_data") or {}
            short_leg = strikes_data.get("short_leg_data")
            delta = _float_or_none(long_leg.get("delta"))
            theta = _float_or_none(long_leg.get("theta"))
            vega  = _float_or_none(long_leg.get("vega"))

            bid = float(long_leg.get("bid") or 0)
            ask = float(long_leg.get("ask") or 0)
            mid = (bid + ask) / 2 if (bid + ask) > 0 else None
            bid_ask_spread_pct = round((ask - bid) / mid, 4) if mid and mid > 0 else None

            oi = long_leg.get("openInterest")
            open_interest = int(oi) if oi is not None else None

            probability_profit: Optional[float] = None
            if strategy in (OptionStrategy.CALL_DEBIT_SPREAD, OptionStrategy.PUT_DEBIT_SPREAD):
                s_delta = _float_or_none((short_leg or {}).get("delta"))
                if s_delta is not None:
                    probability_profit = round(1.0 - abs(s_delta), 3)
            elif strategy in (OptionStrategy.SINGLE_CALL, OptionStrategy.SINGLE_PUT):
                if delta is not None:
                    probability_profit = round(abs(delta), 3)

            expected_value: Optional[float] = None
            if probability_profit is not None and max_gain_usd is not None:
                expected_value = round(
                    max_gain_usd * probability_profit - max_loss_usd * (1 - probability_profit),
                    2,
                )

            candidates.append({
                "candidate_id":       str(uuid4())[:8],
                "structure_type":     struct_name,
                "symbol":             pack.symbol,
                "expiry":             expiry,
                "long_strike":        long_strike,
                "short_strike":       short_strike,
                "contracts":          contracts,
                "debit":              round(net_debit, 4),
                "max_loss":           round(max_loss_usd, 2),
                "max_gain":           round(max_gain_usd, 2) if max_gain_usd is not None else None,
                "breakeven":          round(breakeven, 2),
                "delta":              delta,
                "theta":              theta,
                "vega":               vega,
                "probability_profit": probability_profit,
                "expected_value":     expected_value,
                "liquidity_score":    pack.liquidity_score,
                "bid_ask_spread_pct": bid_ask_spread_pct,
                "open_interest":      open_interest,
                "dte":                dte,
            })

        except Exception as _exc:
            log.debug("[GEN_CAND] %s %s: failed (non-fatal): %s",
                      pack.symbol, struct_name, _exc)
            continue

    log.debug("[GEN_CAND] %s: %d candidates from %d allowed",
              pack.symbol, len(candidates), len(allowed_structures))
    return candidates


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
