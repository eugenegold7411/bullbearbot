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
    buying_power: float = 0.0,
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

    # Use buying_power for sizing; fall back to equity when buying_power not available
    _bp = buying_power if buying_power > 0 else equity

    # Dynamic tier — single leg only, same-week expiry
    if tier == "dynamic":
        return _dynamic_single_leg(
            symbol, iv_summary, signal_data, _bp, catalyst, int(score)
        )

    # Core tier — full strategy selection
    allowed = options_regime.get("allowed_strategies", [])
    size_mult = options_regime.get("size_multiplier", 1.0)

    if env in ("very_cheap", "cheap"):
        # Buy premium
        if "debit_spread" in allowed:
            return _buy_premium_strategy(
                symbol, iv_summary, signal_data, _bp, catalyst,
                size_mult, int(score), prefer_spread=True
            )
        elif "single_leg" in allowed:
            return _buy_premium_strategy(
                symbol, iv_summary, signal_data, _bp, catalyst,
                size_mult, int(score), prefer_spread=False
            )

    elif env == "neutral":
        # Prefer spreads — debit or credit based on signal direction
        if "debit_spread" in allowed:
            return _spread_strategy(
                symbol, iv_summary, signal_data, _bp, catalyst,
                size_mult, int(score)
            )

    elif env in ("expensive", "very_expensive"):
        # Sell premium
        if "credit_spread" in allowed:
            return _sell_premium_strategy(
                symbol, iv_summary, signal_data, _bp, catalyst,
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

    max_cost = equity * 0.05 * size_mult   # max risk budget for credit spreads
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
    "short_put":          OptionStrategy.SHORT_PUT,
    "debit_call_spread":  OptionStrategy.CALL_DEBIT_SPREAD,
    "debit_put_spread":   OptionStrategy.PUT_DEBIT_SPREAD,
    "credit_call_spread": OptionStrategy.CALL_CREDIT_SPREAD,
    "credit_put_spread":  OptionStrategy.PUT_CREDIT_SPREAD,
    "straddle":           OptionStrategy.STRADDLE,
    "strangle":           OptionStrategy.STRANGLE,
    "iron_condor":        OptionStrategy.IRON_CONDOR,
    "iron_butterfly":     OptionStrategy.IRON_BUTTERFLY,
}

# Strategies routed through _STRUCTURE_MAP but whose builders are Phase 2+ stubs.
# generate_candidate_structures() logs INFO and skips these rather than letting
# them reach select_strikes() and failing silently.
_BUILDER_STUBS: frozenset[OptionStrategy] = frozenset()  # straddle/strangle implemented Phase 2

_DTE_RANGE: dict[OptionStrategy, tuple[int, int]] = {
    OptionStrategy.SINGLE_CALL:        (5, 21),
    OptionStrategy.SINGLE_PUT:         (5, 21),
    OptionStrategy.SHORT_PUT:          (21, 45),
    OptionStrategy.CALL_DEBIT_SPREAD:  (5, 28),
    OptionStrategy.PUT_DEBIT_SPREAD:   (5, 28),
    OptionStrategy.CALL_CREDIT_SPREAD: (5, 45),
    OptionStrategy.PUT_CREDIT_SPREAD:  (5, 45),
    OptionStrategy.STRADDLE:           (14, 28),
    OptionStrategy.STRANGLE:           (14, 28),
    OptionStrategy.IRON_CONDOR:        (21, 45),
    OptionStrategy.IRON_BUTTERFLY:     (21, 45),
}


def _float_or_none(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _build_short_put(
    pack,
    chain: dict,
    buying_power: float,
    config: dict | None = None,
) -> Optional[dict]:
    """
    Build a cash-secured short put candidate.

    Selects OTM put with delta magnitude closest to short_put_delta_target (~0.275).
    max_loss uses stop-loss-bounded risk (2x premium) so sizing and EV are sensible.
    Returns candidate dict or None if no suitable strike or premium floor not met.
    """
    a2_cfg        = (config or {}).get("account2", {})
    delta_target  = float(a2_cfg.get("short_put_delta_target", 0.275))
    delta_tol     = float(a2_cfg.get("short_put_delta_tolerance", 0.05))
    min_premium   = float(a2_cfg.get("short_put_min_premium_usd", 50.0))
    stop_multiple = float(a2_cfg.get("short_put_stop_loss_multiple", 2.0))

    spot = chain.get("current_price")
    if not spot or float(spot) <= 0:
        return None
    spot = float(spot)

    today        = date.today()
    expirations  = chain.get("expirations", {})
    dte_min, dte_max = _DTE_RANGE[OptionStrategy.SHORT_PUT]  # (21, 45)

    # Find expiry closest to midpoint of DTE range
    expiry     = None
    target_mid = (dte_min + dte_max) / 2.0
    best_dist  = float("inf")
    for exp_str in sorted(expirations.keys()):
        try:
            dte = (date.fromisoformat(exp_str) - today).days
            if dte_min <= dte <= dte_max:
                dist = abs(dte - target_mid)
                if dist < best_dist:
                    expiry    = exp_str
                    best_dist = dist
        except Exception:
            continue

    if expiry is None:
        log.debug("[GEN_CAND] %s short_put: no expiry in DTE %d-%d", pack.symbol, dte_min, dte_max)
        return None

    puts = expirations.get(expiry, {}).get("puts", [])
    if not puts:
        return None

    # Find OTM put with delta magnitude closest to target
    otm_puts = [p for p in puts if float(p.get("strike", 0)) < spot]
    if not otm_puts:
        return None

    has_delta = any("delta" in p for p in otm_puts)
    if has_delta:
        eligible = [
            p for p in otm_puts
            if "delta" in p and abs(abs(float(p["delta"])) - delta_target) <= delta_tol
        ]
        if not eligible:
            eligible = otm_puts
        best_put = min(eligible, key=lambda p: abs(abs(float(p.get("delta", 0))) - delta_target))
    else:
        best_put = max(otm_puts, key=lambda p: float(p["strike"]))  # closest OTM

    strike = float(best_put.get("strike", 0))
    if strike <= 0:
        return None

    bid = float(best_put.get("bid") or 0)
    ask = float(best_put.get("ask") or 0)
    if bid <= 0 and ask <= 0:
        return None
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None

    # Minimum premium floor check
    if mid * 100 < min_premium:
        log.debug("[GEN_CAND] %s short_put: premium $%.0f < floor $%.0f",
                  pack.symbol, mid * 100, min_premium)
        return None

    # Sizing: budget / (stop_multiple * mid * 100) contracts, capped at 10
    _max_spread_pct = float((config or {}).get("account2", {}).get("max_spread_cost_pct", 0.05))
    budget          = min(pack.premium_budget_usd, buying_power * _max_spread_pct)
    max_loss_per    = stop_multiple * mid * 100   # stop-loss bounded cost per contract
    contracts       = max(1, min(10, int(budget / max_loss_per))) if max_loss_per > 0 else 1

    max_gain_usd    = mid * contracts * 100
    max_loss_usd    = max_loss_per * contracts
    dte             = (date.fromisoformat(expiry) - today).days
    breakeven       = strike - mid

    delta = _float_or_none(best_put.get("delta"))
    theta = _float_or_none(best_put.get("theta"))
    vega  = _float_or_none(best_put.get("vega"))

    probability_profit = round(1.0 - abs(delta), 3) if delta is not None else None
    expected_value: Optional[float] = None
    if probability_profit is not None:
        expected_value = round(
            max_gain_usd * probability_profit - max_loss_usd * (1.0 - probability_profit),
            2,
        )

    bid_ask_spread_pct = round((ask - bid) / mid, 4) if mid > 0 else None
    oi                 = best_put.get("openInterest")
    open_interest      = int(oi) if oi is not None else None

    return {
        "candidate_id":       str(uuid4())[:8],
        "structure_type":     "short_put",
        "symbol":             pack.symbol,
        "expiry":             expiry,
        "long_strike":        strike,     # legacy naming (the sold put strike)
        "short_strike":       None,
        "leg_side":           "sell",
        "contracts":          contracts,
        "debit":              round(-mid, 4),    # negative = credit received
        "max_loss":           round(max_loss_usd, 2),
        "max_gain":           round(max_gain_usd, 2),
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
    }


def _build_iron_condor(
    pack,
    chain: dict,
    buying_power: float,
    config: dict | None = None,
) -> Optional[dict]:
    """
    Build an iron condor candidate: sell OTM call + buy further OTM call +
    sell OTM put + buy further OTM put (same expiry, defined risk).

    Returns candidate dict or None if chain data is insufficient or
    net credit falls below min_credit floor.
    """
    import options_builder as _ob  # noqa: PLC0415

    a2_cfg     = (config or {}).get("account2", {})
    min_credit = float(a2_cfg.get("iron_condor_min_credit_usd", 50.0))

    spot = chain.get("current_price")
    if not spot or float(spot) <= 0:
        return None
    spot = float(spot)

    today        = date.today()
    dte_min, dte_max = _DTE_RANGE[OptionStrategy.IRON_CONDOR]  # (21, 45)

    expiry = _ob.select_expiry(chain, dte_min, dte_max)
    if expiry is None:
        log.debug("[GEN_CAND] %s iron_condor: no expiry in DTE %d-%d", pack.symbol, dte_min, dte_max)
        return None

    strikes_data = _ob.select_strikes(chain, expiry, OptionStrategy.IRON_CONDOR, spot, "neutral", config or {})
    if strikes_data is None:
        log.debug("[GEN_CAND] %s iron_condor: select_strikes=None", pack.symbol)
        return None

    economics = _ob.compute_economics(OptionStrategy.IRON_CONDOR, strikes_data)
    net_debit = economics.get("net_debit")
    max_profit = economics.get("max_profit")
    max_loss   = economics.get("max_loss")

    if net_debit is None or net_debit >= 0 or max_profit is None or max_loss is None:
        return None

    net_credit = abs(net_debit)

    _max_spread_pct = float((config or {}).get("account2", {}).get("max_spread_cost_pct", 0.05))
    budget      = min(pack.premium_budget_usd, buying_power * _max_spread_pct)
    cost_per    = max_loss * 100
    contracts   = max(1, min(10, int(budget / cost_per))) if cost_per > 0 else 1

    # Minimum credit floor
    if net_credit * contracts * 100 < min_credit:
        log.debug("[GEN_CAND] %s iron_condor: net_credit $%.0f < floor $%.0f",
                  pack.symbol, net_credit * contracts * 100, min_credit)
        return None

    dte           = (date.fromisoformat(expiry) - today).days
    max_gain_usd  = round(max_profit * contracts * 100, 2)
    max_loss_usd  = round(max_loss   * contracts * 100, 2)

    sc_strike = float(strikes_data.get("short_call_strike_price", 0))
    sp_strike = float(strikes_data.get("short_put_strike_price", 0))
    breakeven_upper = sc_strike + net_credit
    breakeven_lower = sp_strike - net_credit

    sc_data   = strikes_data.get("short_call_leg_data") or {}
    bid       = float(sc_data.get("bid") or 0)
    ask       = float(sc_data.get("ask") or 0)
    mid       = (bid + ask) / 2 if (bid + ask) > 0 else net_credit
    bid_ask_spread_pct = round((ask - bid) / mid, 4) if mid > 0 else None
    oi        = sc_data.get("openInterest")

    return {
        "candidate_id":       str(uuid4())[:8],
        "structure_type":     "iron_condor",
        "symbol":             pack.symbol,
        "expiry":             expiry,
        "long_strike":        float(strikes_data.get("long_put_strike_price", 0)),
        "short_strike":       sp_strike,
        "leg_side":           "sell",     # primary legs are sells
        "contracts":          contracts,
        "debit":              round(-net_credit, 4),   # negative = credit
        "max_loss":           max_loss_usd,
        "max_gain":           max_gain_usd,
        "breakeven":          round(breakeven_lower, 2),
        "breakeven_upper":    round(breakeven_upper, 2),
        "delta":              _float_or_none(sc_data.get("delta")),
        "theta":              _float_or_none(sc_data.get("theta")),
        "vega":               _float_or_none(sc_data.get("vega")),
        "probability_profit": None,
        "expected_value":     None,
        "liquidity_score":    pack.liquidity_score,
        "bid_ask_spread_pct": bid_ask_spread_pct,
        "open_interest":      int(oi) if oi is not None else None,
        "dte":                dte,
    }


def _build_iron_butterfly(
    pack,
    chain: dict,
    buying_power: float,
    config: dict | None = None,
) -> Optional[dict]:
    """
    Build an iron butterfly candidate: sell ATM call + sell ATM put +
    buy OTM call wing + buy OTM put wing (same expiry, defined risk).

    Returns candidate dict or None if chain data is insufficient or
    net credit falls below min_credit floor.
    """
    import options_builder as _ob  # noqa: PLC0415

    a2_cfg     = (config or {}).get("account2", {})
    min_credit = float(a2_cfg.get("iron_butterfly_min_credit_usd", 100.0))

    spot = chain.get("current_price")
    if not spot or float(spot) <= 0:
        return None
    spot = float(spot)

    today        = date.today()
    dte_min, dte_max = _DTE_RANGE[OptionStrategy.IRON_BUTTERFLY]  # (21, 45)

    expiry = _ob.select_expiry(chain, dte_min, dte_max)
    if expiry is None:
        log.debug("[GEN_CAND] %s iron_butterfly: no expiry in DTE %d-%d", pack.symbol, dte_min, dte_max)
        return None

    strikes_data = _ob.select_strikes(chain, expiry, OptionStrategy.IRON_BUTTERFLY, spot, "neutral", config or {})
    if strikes_data is None:
        log.debug("[GEN_CAND] %s iron_butterfly: select_strikes=None", pack.symbol)
        return None

    economics = _ob.compute_economics(OptionStrategy.IRON_BUTTERFLY, strikes_data)
    net_debit = economics.get("net_debit")
    max_profit = economics.get("max_profit")
    max_loss   = economics.get("max_loss")

    if net_debit is None or net_debit >= 0 or max_profit is None or max_loss is None:
        return None

    net_credit = abs(net_debit)

    _max_spread_pct = float((config or {}).get("account2", {}).get("max_spread_cost_pct", 0.05))
    budget      = min(pack.premium_budget_usd, buying_power * _max_spread_pct)
    cost_per    = max_loss * 100
    contracts   = max(1, min(10, int(budget / cost_per))) if cost_per > 0 else 1

    if net_credit * contracts * 100 < min_credit:
        log.debug("[GEN_CAND] %s iron_butterfly: net_credit $%.0f < floor $%.0f",
                  pack.symbol, net_credit * contracts * 100, min_credit)
        return None

    dte           = (date.fromisoformat(expiry) - today).days
    max_gain_usd  = round(max_profit * contracts * 100, 2)
    max_loss_usd  = round(max_loss   * contracts * 100, 2)

    atm_strike = float(strikes_data.get("short_call_strike_price", spot))
    breakeven_upper = atm_strike + net_credit
    breakeven_lower = atm_strike - net_credit

    sc_data = strikes_data.get("short_call_leg_data") or {}
    bid     = float(sc_data.get("bid") or 0)
    ask     = float(sc_data.get("ask") or 0)
    mid     = (bid + ask) / 2 if (bid + ask) > 0 else net_credit
    bid_ask_spread_pct = round((ask - bid) / mid, 4) if mid > 0 else None
    oi      = sc_data.get("openInterest")

    return {
        "candidate_id":       str(uuid4())[:8],
        "structure_type":     "iron_butterfly",
        "symbol":             pack.symbol,
        "expiry":             expiry,
        "long_strike":        float(strikes_data.get("long_put_strike_price", 0)),
        "short_strike":       atm_strike,
        "leg_side":           "sell",
        "contracts":          contracts,
        "debit":              round(-net_credit, 4),
        "max_loss":           max_loss_usd,
        "max_gain":           max_gain_usd,
        "breakeven":          round(breakeven_lower, 2),
        "breakeven_upper":    round(breakeven_upper, 2),
        "delta":              _float_or_none(sc_data.get("delta")),
        "theta":              _float_or_none(sc_data.get("theta")),
        "vega":               _float_or_none(sc_data.get("vega")),
        "probability_profit": None,
        "expected_value":     None,
        "liquidity_score":    pack.liquidity_score,
        "bid_ask_spread_pct": bid_ask_spread_pct,
        "open_interest":      int(oi) if oi is not None else None,
        "dte":                dte,
    }


def generate_candidate_structures(
    pack: A2FeaturePack,
    allowed_structures: list[str],
    equity: float,
    chain: dict,
    config: dict | None = None,
    buying_power: float = 0.0,
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

    # Use buying_power for sizing; fall back to equity when not available
    _bp = buying_power if buying_power > 0 else equity

    candidates: list[dict] = []
    spot = chain.get("current_price")
    if not spot or float(spot) <= 0:
        log.debug("[GEN_CAND] %s: no current_price in chain", pack.symbol)
        return []

    spot = float(spot)
    today = date.today()

    for struct_name in allowed_structures:
        # Dedicated sell-side builders for short_put, iron_condor, iron_butterfly
        if struct_name == "short_put":
            try:
                cand = _build_short_put(pack, chain, _bp, config)
                if cand is not None:
                    candidates.append(cand)
                else:
                    log.debug("[GEN_CAND] %s short_put: no candidate built", pack.symbol)
            except Exception as _exc:
                log.debug("[GEN_CAND] %s short_put: failed (non-fatal): %s",
                          pack.symbol, _exc)
            continue

        if struct_name == "iron_condor":
            try:
                cand = _build_iron_condor(pack, chain, _bp, config)
                if cand is not None:
                    candidates.append(cand)
                else:
                    log.debug("[GEN_CAND] %s iron_condor: no candidate built", pack.symbol)
            except Exception as _exc:
                log.debug("[GEN_CAND] %s iron_condor: failed (non-fatal): %s",
                          pack.symbol, _exc)
            continue

        if struct_name == "iron_butterfly":
            try:
                cand = _build_iron_butterfly(pack, chain, _bp, config)
                if cand is not None:
                    candidates.append(cand)
                else:
                    log.debug("[GEN_CAND] %s iron_butterfly: no candidate built", pack.symbol)
            except Exception as _exc:
                log.debug("[GEN_CAND] %s iron_butterfly: failed (non-fatal): %s",
                          pack.symbol, _exc)
            continue

        strategy = _STRUCTURE_MAP.get(struct_name)
        if strategy is None:
            log.debug("[GEN_CAND] %s: unknown structure type %r", pack.symbol, struct_name)
            continue

        if strategy in _BUILDER_STUBS:
            log.info("[GEN_CAND] %s: %s not yet implemented (Phase 2) — skipping",
                     pack.symbol, struct_name)
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

            _max_spread_pct = float(
                (config or {}).get("account2", {}).get("max_spread_cost_pct", 0.05)
            )
            budget = min(pack.premium_budget_usd, _bp * _max_spread_pct)
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
                            OptionStrategy.CALL_CREDIT_SPREAD,
                            OptionStrategy.STRADDLE,
                            OptionStrategy.STRANGLE):
                # For straddle/strangle: upper breakeven = call_strike + total_debit
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
