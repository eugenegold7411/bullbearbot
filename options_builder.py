"""
options_builder.py — Real-chain options structure builder for Account 2.

Replaces the synthetic-estimate approach in options_execution.py with live
chain data from options_data.fetch_options_chain().  This module is pure
computation — it never touches Alpaca or the filesystem.  Callers receive
either a fully-populated OptionsStructure (lifecycle=PROPOSED) or a clear
rejection reason string.

Public API
----------
build_structure(action_dict_or_kwargs, chain, equity, config)
    → (OptionsStructure, None) | (None, reason: str)

Internal helpers (also unit-testable):
    select_expiry(chain, dte_min, dte_max)    → str | None
    select_strikes(chain, expiry, strategy, spot, direction, config)
                                               → dict | None
    validate_liquidity(leg_data, config)       → (bool, str)
    compute_economics(strategy, legs_data)     → dict
    size_contracts(economics, max_cost_usd, equity, config)
                                               → int
    build_legs(symbol, strategy, expiry, strikes_data)
                                               → list[OptionsLeg]

Phase 2 / Phase 3 strategies (straddles, iron condors, calendars) are
explicitly not implemented this session:
    build_structure() returns (None, "not yet supported") for those strategies.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Optional
from uuid import uuid4

from schemas import (
    Direction,
    OptionsLeg,
    OptionsStructure,
    OptionStrategy,
    StructureLifecycle,
    Tier,
)

log = logging.getLogger(__name__)

# ── Phase 1 strategy set ──────────────────────────────────────────────────────
_PHASE1_STRATEGIES: frozenset[OptionStrategy] = frozenset({
    OptionStrategy.CALL_DEBIT_SPREAD,
    OptionStrategy.PUT_DEBIT_SPREAD,
    OptionStrategy.CALL_CREDIT_SPREAD,
    OptionStrategy.PUT_CREDIT_SPREAD,
    OptionStrategy.SINGLE_CALL,
    OptionStrategy.SINGLE_PUT,
    OptionStrategy.SHORT_PUT,
    OptionStrategy.STRADDLE,
    OptionStrategy.STRANGLE,
})

# Default liquidity requirements (tightened; overridden by config liquidity_gates)
_DEFAULT_MIN_OPEN_INTEREST = 200
_DEFAULT_MIN_VOLUME        = 20
_DEFAULT_MAX_BID_ASK_PCT   = 0.08   # max (ask-bid)/mid as fraction
_DEFAULT_MIN_MID_PRICE     = 0.05   # avoid sub-nickel legs


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def build_structure(
    action:      dict | None          = None,
    chain:       dict | None          = None,
    equity:      float                = 100_000.0,
    config:      dict | None          = None,
    *,
    symbol:      str | None           = None,
    strategy:    OptionStrategy | None = None,
    direction:   Direction | str | None = None,
    conviction:  float                 = 0.75,
    iv_rank:     float                 = 50.0,
    max_cost_usd: float                = 0.0,
) -> tuple[Optional[OptionsStructure], Optional[str]]:
    """
    Build a fully-specified OptionsStructure from live chain data.

    Supports two calling conventions:

    Old-style (backward-compat, used by existing tests):
        build_structure(action_dict, chain, equity, config)

    New-style (keyword args from StructureProposal):
        build_structure(
            symbol=..., strategy=..., direction=...,
            conviction=..., iv_rank=..., max_cost_usd=...,
            chain=..., equity=..., config=...,
        )

    Parameters
    ----------
    action : dict, optional
        Old-style: full action dict from options_intelligence.
    chain : dict
        Live chain from options_data.fetch_options_chain().
    equity : float
        Current Account 2 equity (for hard-cap sizing check).
    config : dict
        account2 section of strategy_config.json.
    symbol, strategy, direction, conviction, iv_rank, max_cost_usd
        New-style keyword args extracted from StructureProposal.

    Returns
    -------
    (OptionsStructure, None)  on success
    (None, reason_str)        on any failure
    """
    if config is None:
        config = {}
    if chain is None:
        return None, "build_structure: chain is required"

    # Normalize: convert new-style keyword call into the action dict the
    # rest of the function expects.
    if action is None:
        if symbol is None or strategy is None:
            return None, "build_structure: either action dict or (symbol, strategy) kwargs required"
        # Direction may be a Direction enum or a plain string
        dir_str = direction.value if isinstance(direction, Direction) else (direction or "bullish")
        action = {
            "symbol":          symbol,
            "option_strategy": strategy.value if isinstance(strategy, OptionStrategy) else strategy,
            "direction":       dir_str,
            "conviction":      conviction,
            "iv_rank":         iv_rank,
            "max_cost_usd":    max_cost_usd,
        }

    symbol    = str(action.get("symbol", chain.get("symbol", ""))).upper()
    strategy_raw = action.get("option_strategy") or action.get("option_strategy_hint")
    if not strategy_raw:
        return None, "missing option_strategy in action"

    try:
        strategy = OptionStrategy(str(strategy_raw).lower())
    except ValueError:
        return None, f"unknown option_strategy={strategy_raw!r}"

    # Phase 2/3 gate
    if strategy not in _PHASE1_STRATEGIES:
        return None, f"not yet supported: {strategy.value}"

    spot = chain.get("current_price")
    if not spot or spot <= 0:
        return None, f"no valid current_price in chain for {symbol}"

    direction  = str(action.get("direction", "bullish")).lower()
    iv_rank    = float(action.get("iv_rank") or 50.0)
    catalyst   = str(action.get("catalyst", ""))
    max_cost   = float(action.get("max_cost_usd") or 0)
    conviction = float(action.get("conviction") or 0.75)

    tier_raw = str(action.get("tier", "core")).lower()
    try:
        tier = Tier(tier_raw)
    except ValueError:
        tier = Tier.CORE

    # ── 1. Select expiration ──────────────────────────────────────────────────
    greeks_cfg   = config.get("greeks", {})
    dte_min      = int(greeks_cfg.get("min_dte", 5))
    dte_max      = _dte_max_for_strategy(strategy)
    sizing_cfg   = config.get("position_sizing", {})

    expiry = select_expiry(chain, dte_min, dte_max)
    if expiry is None:
        return None, (
            f"no expiration found in chain for {symbol} with DTE {dte_min}–{dte_max}"
        )

    # ── 2. Select strikes from real chain data ────────────────────────────────
    strikes_data = select_strikes(chain, expiry, strategy, spot, direction, config)
    if strikes_data is None:
        return None, (
            f"could not select strikes for {symbol} {strategy.value} exp={expiry}"
        )

    # ── 3. Validate liquidity ─────────────────────────────────────────────────
    liq_ok, liq_reason = validate_liquidity(strikes_data, config)
    _low_liquidity = False
    if not liq_ok:
        _is_single = strategy in (OptionStrategy.SINGLE_CALL, OptionStrategy.SINGLE_PUT)
        if _is_single:
            # Single legs: warn but proceed — add low_liquidity flag to audit_log
            log.warning(
                "[BUILDER] %s %s: low liquidity (%s) — proceeding with fill_quality: low",
                symbol, strategy.value, liq_reason,
            )
            _low_liquidity = True
        else:
            # Spreads: both legs must pass — reject
            return None, f"liquidity check failed for {symbol}: {liq_reason}"

    # ── 4. Compute economics ──────────────────────────────────────────────────
    economics = compute_economics(strategy, strikes_data)
    if economics.get("net_debit") is None:
        return None, f"could not compute economics for {symbol} (missing mid prices)"

    # ── SHORT_PUT minimum premium check ──────────────────────────────────────
    if strategy == OptionStrategy.SHORT_PUT:
        _max_profit = economics.get("max_profit") or 0.0
        _min_prem   = float(config.get("short_put_min_premium_usd", 50.0))
        if _max_profit * 100 < _min_prem:
            return None, (
                f"{symbol} short_put: premium ${_max_profit * 100:.0f} "
                f"< minimum ${_min_prem:.0f}"
            )

    # ── 5. Size contracts ─────────────────────────────────────────────────────
    # If action already specifies max_cost, use it; otherwise compute from equity
    if not max_cost:
        max_cost = _max_cost_for_tier(equity, strategy, tier, sizing_cfg)
    contracts = size_contracts(economics, max_cost, equity, config)
    if contracts < 1:
        return None, (
            f"sizing produced 0 contracts for {symbol} "
            f"(net_debit={economics['net_debit']:.2f}, max_cost={max_cost:.0f})"
        )

    # ── 6. Build OptionsLeg objects ───────────────────────────────────────────
    legs = build_legs(symbol, strategy, expiry, strikes_data)
    if not legs:
        return None, f"failed to construct legs for {symbol} {strategy.value}"

    # ── 7. Assemble OptionsStructure ──────────────────────────────────────────
    long_strike  = strikes_data.get("long_strike_price")
    short_strike = strikes_data.get("short_strike_price")
    net_debit    = economics["net_debit"]
    max_profit   = economics.get("max_profit")

    structure = OptionsStructure(
        structure_id  = str(uuid4()),
        underlying    = symbol,
        strategy      = strategy,
        lifecycle     = StructureLifecycle.PROPOSED,
        legs          = legs,
        contracts     = contracts,
        max_cost_usd  = round(net_debit * contracts * 100, 2),
        opened_at     = datetime.now(timezone.utc).isoformat(),
        catalyst      = catalyst,
        tier          = tier,
        iv_rank       = iv_rank,
        order_ids     = [],
        direction     = direction,
        expiration    = expiry,
        long_strike   = long_strike,
        short_strike  = short_strike,
        debit_paid    = None,          # set on fill confirmation
        max_profit_usd = round(max_profit * contracts * 100, 2) if max_profit else None,
        audit_log     = [],
    )
    structure.add_audit(
        f"proposed: strategy={strategy.value} exp={expiry} "
        f"long={long_strike} short={short_strike} "
        f"net_debit={net_debit:.2f} contracts={contracts}"
    )
    if _low_liquidity:
        structure.add_audit(f"fill_quality: low — {liq_reason}")

    log.info(
        "[BUILDER] %s %s exp=%s long=%.2f short=%s net_debit=%.2f×%d → max_cost=$%.0f",
        symbol, strategy.value, expiry,
        long_strike or 0,
        f"{short_strike:.2f}" if short_strike else "—",
        net_debit, contracts, net_debit * contracts * 100,
    )
    return structure, None


# ─────────────────────────────────────────────────────────────────────────────
# select_expiry
# ─────────────────────────────────────────────────────────────────────────────

def select_expiry(chain: dict, dte_min: int, dte_max: int) -> Optional[str]:
    """
    Select the best expiration from chain["expirations"] within [dte_min, dte_max].

    Chooses the date closest to the midpoint of the DTE range.
    Returns None if no valid expiration exists.

    Parameters
    ----------
    chain    : chain dict from fetch_options_chain()
    dte_min  : minimum days to expiration (inclusive)
    dte_max  : maximum days to expiration (inclusive)
    """
    today       = date.today()
    expirations = chain.get("expirations", {})
    if not expirations:
        return None

    target_dte = (dte_min + dte_max) / 2.0
    best: Optional[str] = None
    best_dist: float    = float("inf")

    for exp_str in sorted(expirations.keys()):
        try:
            exp_date = date.fromisoformat(exp_str)
        except ValueError:
            continue
        dte = (exp_date - today).days
        if dte_min <= dte <= dte_max:
            dist = abs(dte - target_dte)
            if dist < best_dist:
                best      = exp_str
                best_dist = dist

    return best


# ─────────────────────────────────────────────────────────────────────────────
# select_strikes
# ─────────────────────────────────────────────────────────────────────────────

def select_strikes(
    chain:     dict,
    expiry:    str,
    strategy:  OptionStrategy,
    spot:      float,
    direction: str,
    config:    dict,
) -> Optional[dict]:
    """
    Select strike(s) from real chain data for the given strategy.

    Returns a dict with keys depending on strategy type:
      Single-leg:  {option_type, long_strike_price, long_leg_data}
      Spread:      {option_type, long_strike_price, long_leg_data,
                    short_strike_price, short_leg_data}

    Returns None if suitable strikes cannot be found.

    Strike selection rules:
      - Single call/put:  closest ATM strike (delta ≥ min_delta if available,
                          else closest to spot)
      - Debit spread:     long = ATM, short = 1 strike OTM (wider = more risk)
      - Credit spread:    short = 1 strike OTM, long = 2 strikes OTM (define risk)
    """
    expirations = chain.get("expirations", {})
    exp_data    = expirations.get(expiry)
    if not exp_data:
        return None

    greeks_cfg = config.get("greeks", {})
    min_delta  = float(greeks_cfg.get("min_delta", 0.30))

    # Straddle/strangle need both call and put data — route before single-type branch
    if strategy == OptionStrategy.STRADDLE:
        return _select_straddle_strikes(exp_data, spot, min_delta)
    if strategy == OptionStrategy.STRANGLE:
        return _select_strangle_strikes(exp_data, spot)

    # Determine option type from strategy
    if strategy in (OptionStrategy.SINGLE_CALL,
                    OptionStrategy.CALL_DEBIT_SPREAD,
                    OptionStrategy.CALL_CREDIT_SPREAD):
        option_type = "call"
        opts        = exp_data.get("calls", [])
    elif strategy in (OptionStrategy.SINGLE_PUT,
                      OptionStrategy.PUT_DEBIT_SPREAD,
                      OptionStrategy.PUT_CREDIT_SPREAD,
                      OptionStrategy.SHORT_PUT):
        option_type = "put"
        opts        = exp_data.get("puts", [])
    else:
        return None  # unsupported — caller handles

    if not opts:
        return None

    # Sort by strike ascending
    opts_sorted = sorted(opts, key=lambda o: float(o["strike"]))

    if strategy in (OptionStrategy.SINGLE_CALL, OptionStrategy.SINGLE_PUT):
        long_leg = _pick_atm_leg(opts_sorted, spot, min_delta)
        if long_leg is None:
            return None
        return {
            "option_type":       option_type,
            "long_strike_price": float(long_leg["strike"]),
            "long_leg_data":     long_leg,
            "short_strike_price": None,
            "short_leg_data":    None,
        }

    elif strategy == OptionStrategy.SHORT_PUT:
        a2_cfg       = config.get("account2", config) if isinstance(config.get("account2"), dict) else config
        delta_target = float(a2_cfg.get("short_put_delta_target", 0.275))
        delta_tol    = float(a2_cfg.get("short_put_delta_tolerance", 0.10))
        otm_leg = _pick_otm_put_leg(opts_sorted, spot, delta_target, delta_tol)
        if otm_leg is None:
            return None
        return {
            "option_type":        option_type,
            "long_strike_price":  float(otm_leg["strike"]),  # "long" naming is legacy
            "long_leg_data":      otm_leg,
            "short_strike_price": None,
            "short_leg_data":     None,
        }

    elif strategy in (OptionStrategy.CALL_DEBIT_SPREAD,
                      OptionStrategy.PUT_DEBIT_SPREAD):
        return _select_debit_spread_strikes(opts_sorted, spot, option_type, min_delta)

    elif strategy in (OptionStrategy.CALL_CREDIT_SPREAD,
                      OptionStrategy.PUT_CREDIT_SPREAD):
        return _select_credit_spread_strikes(
            opts_sorted, spot, option_type, direction, min_delta
        )

    return None


def _pick_atm_leg(opts: list[dict], spot: float, min_delta: float) -> Optional[dict]:
    """
    Pick the closest-to-ATM option leg.

    If delta data is available, prefer the leg with delta closest to 0.50
    (or -0.50 for puts) that still satisfies min_delta. Falls back to
    closest-to-spot strike when delta is absent.
    """
    # Check if delta data is present in chain
    has_delta = any("delta" in o for o in opts)

    if has_delta:
        # Filter by min_delta (absolute value)
        eligible = [
            o for o in opts
            if "delta" in o and abs(float(o["delta"])) >= min_delta
        ]
        if not eligible:
            eligible = opts  # relax if none qualify
        # Closest delta to 0.50 (ATM)
        return min(eligible, key=lambda o: abs(abs(float(o.get("delta", 0))) - 0.50))

    # No delta — use closest strike to spot
    return min(opts, key=lambda o: abs(float(o["strike"]) - spot))


def _pick_otm_put_leg(
    opts: list[dict],
    spot: float,
    delta_target: float,
    delta_tol: float,
) -> Optional[dict]:
    """
    Pick an OTM put with delta magnitude closest to delta_target.

    OTM puts have strikes below spot; put deltas are negative (e.g., -0.275).
    delta_target and delta_tol apply to the absolute value of delta.
    Falls back to closest-to-spot OTM strike when delta data is absent.
    """
    otm_opts = [o for o in opts if float(o.get("strike", 0)) < spot]
    if not otm_opts:
        return None

    has_delta = any("delta" in o for o in otm_opts)
    if has_delta:
        eligible = [
            o for o in otm_opts
            if "delta" in o
            and abs(abs(float(o["delta"])) - delta_target) <= delta_tol
        ]
        if not eligible:
            eligible = otm_opts
        return min(eligible, key=lambda o: abs(abs(float(o.get("delta", 0))) - delta_target))

    # No delta — closest OTM strike below spot (highest OTM put)
    return max(otm_opts, key=lambda o: float(o["strike"]))


def _select_debit_spread_strikes(
    opts: list[dict],
    spot: float,
    option_type: str,
    min_delta: float,
) -> Optional[dict]:
    """
    Debit spread: long = ATM, short = next OTM strike.
    For calls: long lower strike, short higher strike.
    For puts:  long higher strike, short lower strike.
    """
    atm_leg = _pick_atm_leg(opts, spot, min_delta)
    if atm_leg is None:
        return None
    atm_strike = float(atm_leg["strike"])

    if option_type == "call":
        # Short leg is the next strike above ATM
        otm_candidates = [o for o in opts if float(o["strike"]) > atm_strike]
        if not otm_candidates:
            return None
        otm_leg = min(otm_candidates, key=lambda o: float(o["strike"]))
        long_leg, short_leg = atm_leg, otm_leg
    else:
        # Put debit spread: long higher strike ATM, short lower strike OTM
        otm_candidates = [o for o in opts if float(o["strike"]) < atm_strike]
        if not otm_candidates:
            return None
        otm_leg = max(otm_candidates, key=lambda o: float(o["strike"]))
        long_leg, short_leg = atm_leg, otm_leg

    return {
        "option_type":        option_type,
        "long_strike_price":  float(long_leg["strike"]),
        "long_leg_data":      long_leg,
        "short_strike_price": float(short_leg["strike"]),
        "short_leg_data":     short_leg,
    }


def _select_credit_spread_strikes(
    opts: list[dict],
    spot: float,
    option_type: str,
    direction: str,
    min_delta: float,
) -> Optional[dict]:
    """
    Credit spread: short = OTM strike, long = further OTM strike (risk-define).
    Call credit spread (bearish): both strikes above spot.
    Put credit spread (bullish):  both strikes below spot.
    """
    if option_type == "call":
        # Both strikes above spot
        otm_calls = sorted(
            [o for o in opts if float(o["strike"]) > spot],
            key=lambda o: float(o["strike"])
        )
        if len(otm_calls) < 2:
            return None
        short_leg = otm_calls[0]   # closest OTM = short
        long_leg  = otm_calls[1]   # next strike out = long (risk-define)
    else:
        # Both strikes below spot
        otm_puts = sorted(
            [o for o in opts if float(o["strike"]) < spot],
            key=lambda o: float(o["strike"]),
            reverse=True
        )
        if len(otm_puts) < 2:
            return None
        short_leg = otm_puts[0]   # closest OTM = short
        long_leg  = otm_puts[1]   # next strike lower = long (risk-define)

    return {
        "option_type":        option_type,
        "long_strike_price":  float(long_leg["strike"]),
        "long_leg_data":      long_leg,
        "short_strike_price": float(short_leg["strike"]),
        "short_leg_data":     short_leg,
    }


def _select_straddle_strikes(exp_data: dict, spot: float, min_delta: float) -> Optional[dict]:
    """
    Straddle: buy ATM call + buy ATM put at the same strike.

    Strike selection: call closest to delta 0.50 (or closest to spot if no delta).
    Put selected at the same numerical strike as the call.
    Returns None if either side is unavailable.
    """
    calls = exp_data.get("calls", [])
    puts  = exp_data.get("puts", [])
    if not calls or not puts:
        return None

    calls_sorted = sorted(calls, key=lambda o: float(o["strike"]))
    puts_sorted  = sorted(puts,  key=lambda o: float(o["strike"]))

    call_leg = _pick_atm_leg(calls_sorted, spot, min_delta)
    if call_leg is None:
        return None
    atm_strike = float(call_leg["strike"])

    # Match put at the same strike; fall back to closest put strike
    exact_put = next((p for p in puts_sorted if float(p["strike"]) == atm_strike), None)
    if exact_put is None:
        exact_put = min(puts_sorted, key=lambda p: abs(float(p["strike"]) - atm_strike))
    if exact_put is None:
        return None

    return {
        "option_type":        "straddle",
        "call_strike_price":  atm_strike,
        "call_leg_data":      call_leg,
        "put_strike_price":   float(exact_put["strike"]),
        "put_leg_data":       exact_put,
        # legacy compat: map call leg to long_* so existing callers still work
        "long_strike_price":  atm_strike,
        "long_leg_data":      call_leg,
        "short_strike_price": None,
        "short_leg_data":     None,
    }


def _select_strangle_strikes(exp_data: dict, spot: float) -> Optional[dict]:
    """
    Strangle: buy OTM call + buy OTM put targeting delta ~0.30 on each side.

    Call leg: OTM call (strike > spot) with delta closest to 0.30.
              Falls back to first strike above spot if delta data absent.
    Put leg:  OTM put  (strike < spot) with delta closest to -0.30.
              Falls back to first strike below spot if delta data absent.
    Returns None if either side is unavailable.
    """
    calls = exp_data.get("calls", [])
    puts  = exp_data.get("puts", [])
    if not calls or not puts:
        return None

    otm_calls = sorted(
        [o for o in calls if float(o["strike"]) > spot],
        key=lambda o: float(o["strike"]),
    )
    otm_puts = sorted(
        [o for o in puts if float(o["strike"]) < spot],
        key=lambda o: float(o["strike"]),
        reverse=True,
    )
    if not otm_calls or not otm_puts:
        return None

    _TARGET_DELTA = 0.30

    has_delta_calls = any("delta" in o for o in otm_calls)
    if has_delta_calls:
        call_leg = min(
            otm_calls,
            key=lambda o: abs(abs(float(o.get("delta", 0))) - _TARGET_DELTA),
        )
    else:
        call_leg = otm_calls[0]  # closest OTM call by strike

    has_delta_puts = any("delta" in o for o in otm_puts)
    if has_delta_puts:
        put_leg = min(
            otm_puts,
            key=lambda o: abs(abs(float(o.get("delta", 0))) - _TARGET_DELTA),
        )
    else:
        put_leg = otm_puts[0]  # closest OTM put by strike

    call_strike = float(call_leg["strike"])
    put_strike  = float(put_leg["strike"])

    return {
        "option_type":        "strangle",
        "call_strike_price":  call_strike,
        "call_leg_data":      call_leg,
        "put_strike_price":   put_strike,
        "put_leg_data":       put_leg,
        # legacy compat
        "long_strike_price":  call_strike,
        "long_leg_data":      call_leg,
        "short_strike_price": None,
        "short_leg_data":     None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# validate_liquidity
# ─────────────────────────────────────────────────────────────────────────────

def validate_liquidity(strikes_data: dict, config: dict) -> tuple[bool, str]:
    """
    Verify that selected legs meet minimum liquidity requirements.

    Reads thresholds from config["liquidity_gates"] (new key),
    falling back to config["liquidity"] for backward compatibility,
    then to module-level defaults.

    Checks per leg:
    - Minimum open interest
    - Minimum daily volume
    - Maximum bid/ask spread as % of mid price  (key: max_spread_pct)
    - Minimum mid price (avoids sub-nickel garbage)

    Returns (True, "ok") or (False, reason_string).

    Caller is responsible for strategy-level strictness:
      - Spreads: reject on (False, reason)
      - Single legs: warn and proceed (add "low_liquidity" to audit_log)
    """
    # Prefer "liquidity_gates" key; fall back to legacy "liquidity" key
    liq_cfg = config.get("liquidity_gates") or config.get("liquidity") or {}

    min_oi  = int(liq_cfg.get("min_open_interest", _DEFAULT_MIN_OPEN_INTEREST))
    min_vol = int(liq_cfg.get("min_volume",         _DEFAULT_MIN_VOLUME))
    # Support both key names: max_spread_pct (new) and max_bid_ask_pct (legacy)
    max_ba  = float(
        liq_cfg.get("max_spread_pct",
        liq_cfg.get("max_bid_ask_pct", _DEFAULT_MAX_BID_ASK_PCT))
    )
    min_mid = float(liq_cfg.get("min_mid_price", _DEFAULT_MIN_MID_PRICE))

    legs_to_check = []
    if strikes_data.get("long_leg_data"):
        legs_to_check.append(("long",  strikes_data["long_leg_data"]))
    if strikes_data.get("short_leg_data"):
        legs_to_check.append(("short", strikes_data["short_leg_data"]))
    # Straddle/strangle: put_leg_data is the second buy leg (not in short_leg_data)
    if strikes_data.get("put_leg_data"):
        legs_to_check.append(("put",   strikes_data["put_leg_data"]))

    for leg_label, leg in legs_to_check:
        oi = leg.get("openInterest", 0) or 0
        if oi < min_oi:
            return False, (
                f"{leg_label} leg strike={leg['strike']} open_interest={oi} < {min_oi}"
            )

        vol = leg.get("volume", 0) or 0
        if vol < min_vol:
            return False, (
                f"{leg_label} leg strike={leg['strike']} volume={vol} < {min_vol}"
            )

        bid = float(leg.get("bid", 0) or 0)
        ask = float(leg.get("ask", 0) or 0)
        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2.0
            if mid < min_mid:
                return False, (
                    f"{leg_label} leg strike={leg['strike']} mid={mid:.3f} < {min_mid}"
                )
            spread_pct = (ask - bid) / mid
            if spread_pct > max_ba:
                return False, (
                    f"{leg_label} leg strike={leg['strike']} "
                    f"bid/ask spread {spread_pct:.1%} > {max_ba:.0%}"
                )

    return True, "ok"


# ─────────────────────────────────────────────────────────────────────────────
# compute_economics
# ─────────────────────────────────────────────────────────────────────────────

def compute_economics(strategy: OptionStrategy, strikes_data: dict) -> dict:
    """
    Compute net debit, max profit, and max loss for the structure.

    Uses mid-price (bid+ask)/2 for each leg. Returns:
    {
        "net_debit":   float | None,   # cost per contract (positive = pay)
        "max_profit":  float | None,   # max profit per contract
        "max_loss":    float | None,   # max loss per contract (positive = loss)
        "long_mid":    float | None,
        "short_mid":   float | None,
    }
    """
    long_leg  = strikes_data.get("long_leg_data")
    short_leg = strikes_data.get("short_leg_data")

    long_mid  = _mid_price(long_leg)
    short_mid = _mid_price(short_leg)

    result: dict = {
        "net_debit":  None,
        "max_profit": None,
        "max_loss":   None,
        "long_mid":   long_mid,
        "short_mid":  short_mid,
    }

    if long_mid is None:
        return result

    if strategy == OptionStrategy.SHORT_PUT:
        # Sell OTM put: net_debit = negative (credit received)
        # max_loss sized on 2x stop (stop at 200% of premium) rather than worst case (strike-premium)
        _STOP_MULT = 2.0
        result["net_debit"]  = round(-long_mid, 4)
        result["max_profit"] = round(long_mid, 4)
        result["max_loss"]   = round(long_mid * _STOP_MULT, 4)

    elif strategy in (OptionStrategy.SINGLE_CALL, OptionStrategy.SINGLE_PUT):
        result["net_debit"]  = round(long_mid, 4)
        result["max_profit"] = None   # theoretically unlimited for calls
        result["max_loss"]   = round(long_mid, 4)

    elif strategy in (OptionStrategy.CALL_DEBIT_SPREAD,
                      OptionStrategy.PUT_DEBIT_SPREAD):
        if short_mid is None:
            return result
        net = long_mid - short_mid
        long_strike  = float(strikes_data["long_strike_price"])
        short_strike = float(strikes_data["short_strike_price"])
        spread_width = abs(short_strike - long_strike)
        result["net_debit"]  = round(net, 4)
        result["max_profit"] = round(spread_width - net, 4)
        result["max_loss"]   = round(net, 4)

    elif strategy in (OptionStrategy.CALL_CREDIT_SPREAD,
                      OptionStrategy.PUT_CREDIT_SPREAD):
        if short_mid is None:
            return result
        # Credit received = short premium − long premium (net_debit is negative)
        net = long_mid - short_mid   # negative for credit spreads
        long_strike  = float(strikes_data["long_strike_price"])
        short_strike = float(strikes_data["short_strike_price"])
        spread_width = abs(short_strike - long_strike)
        credit       = abs(net)
        result["net_debit"]  = round(net, 4)          # negative
        result["max_profit"] = round(credit, 4)        # credit received
        result["max_loss"]   = round(spread_width - credit, 4)

    elif strategy in (OptionStrategy.STRADDLE, OptionStrategy.STRANGLE):
        # Both legs are buys: total debit = call_mid + put_mid
        put_leg = strikes_data.get("put_leg_data")
        put_mid = _mid_price(put_leg)
        if long_mid is None or put_mid is None:
            return result
        total_debit = round(long_mid + put_mid, 4)
        result["net_debit"]  = total_debit
        result["max_loss"]   = total_debit   # max loss = total debit paid
        result["max_profit"] = None          # unlimited (large directional move)
        result["long_mid"]   = long_mid      # call mid
        result["short_mid"]  = put_mid       # repurposed: put mid

    return result


def _mid_price(leg_data: Optional[dict]) -> Optional[float]:
    """Compute mid = (bid + ask) / 2 from a chain leg dict. Returns None if unavailable."""
    if not leg_data:
        return None
    bid = leg_data.get("bid")
    ask = leg_data.get("ask")
    if bid is None or ask is None:
        # Fall back to lastPrice
        last = leg_data.get("lastPrice")
        return float(last) if last else None
    bid, ask = float(bid), float(ask)
    if bid <= 0 and ask <= 0:
        return None
    return (bid + ask) / 2.0


# ─────────────────────────────────────────────────────────────────────────────
# size_contracts
# ─────────────────────────────────────────────────────────────────────────────

def size_contracts(
    economics:    dict,
    max_cost_usd: float,
    equity:       float,
    config:       dict,
) -> int:
    """
    Calculate number of contracts within the max_cost_usd budget.

    For debit structures:  cost = net_debit × contracts × 100
    For credit structures: cost = max_loss  × contracts × 100
        (max_loss = spread_width − credit_received)

    Always returns at least 0 (caller rejects if 0).

    Parameters
    ----------
    economics    : output of compute_economics()
    max_cost_usd : maximum dollar outlay for this structure
    equity       : current equity (for hard-cap sanity check)
    config       : account2 config section
    """
    net_debit = economics.get("net_debit")
    max_loss  = economics.get("max_loss")

    # For credit spreads, size on max loss (max risk), not net credit
    if net_debit is not None and net_debit < 0 and max_loss is not None:
        cost_per_contract = max_loss * 100.0
    elif net_debit is not None and net_debit > 0:
        cost_per_contract = net_debit * 100.0
    else:
        return 0

    if cost_per_contract <= 0:
        return 0

    contracts = int(max_cost_usd / cost_per_contract)

    # Hard cap: never exceed 5% of equity per structure regardless of config
    hard_cap_usd = equity * 0.05
    hard_cap_contracts = int(hard_cap_usd / cost_per_contract)
    contracts = min(contracts, hard_cap_contracts)

    return max(0, contracts)


# ─────────────────────────────────────────────────────────────────────────────
# build_legs
# ─────────────────────────────────────────────────────────────────────────────

def build_legs(
    symbol:       str,
    strategy:     OptionStrategy,
    expiry:       str,
    strikes_data: dict,
) -> list[OptionsLeg]:
    """
    Construct OptionsLeg objects for the structure.

    Leg ordering rule (per OptionsStructure docstring):
      Long leg is always first in the list to avoid naked-short risk at any point.

    OCC symbol format (Alpaca):
      {TICKER}{YYMMDD}{C|P}{STRIKE×1000_padded_8digits}
      e.g. GLD251219C00435000  (no ticker padding — Alpaca rejects OCC paper space format)
    """
    option_type  = strikes_data.get("option_type", "call")
    long_data    = strikes_data.get("long_leg_data")
    short_data   = strikes_data.get("short_leg_data")
    long_strike  = strikes_data.get("long_strike_price")
    short_strike = strikes_data.get("short_strike_price")

    if long_data is None or long_strike is None:
        return []

    legs: list[OptionsLeg] = []

    # Straddle/strangle: two buy legs — call then put at (possibly different) strikes
    if option_type in ("straddle", "strangle"):
        call_data   = strikes_data.get("call_leg_data")
        put_data    = strikes_data.get("put_leg_data")
        call_strike = strikes_data.get("call_strike_price")
        put_strike  = strikes_data.get("put_strike_price")
        if not call_data or not put_data or call_strike is None or put_strike is None:
            return []
        legs.append(OptionsLeg(
            occ_symbol    = _build_occ_symbol(symbol, expiry, "call", call_strike),
            underlying    = symbol,
            side          = "buy",
            qty           = 1,
            option_type   = "call",
            strike        = float(call_strike),
            expiration    = expiry,
            bid           = _maybe_float(call_data.get("bid")),
            ask           = _maybe_float(call_data.get("ask")),
            mid           = _mid_price(call_data),
            delta         = _maybe_float(call_data.get("delta")),
            open_interest = int(call_data["openInterest"]) if call_data.get("openInterest") is not None else None,
            volume        = int(call_data["volume"]) if call_data.get("volume") is not None else None,
        ))
        legs.append(OptionsLeg(
            occ_symbol    = _build_occ_symbol(symbol, expiry, "put", put_strike),
            underlying    = symbol,
            side          = "buy",
            qty           = 1,
            option_type   = "put",
            strike        = float(put_strike),
            expiration    = expiry,
            bid           = _maybe_float(put_data.get("bid")),
            ask           = _maybe_float(put_data.get("ask")),
            mid           = _mid_price(put_data),
            delta         = _maybe_float(put_data.get("delta")),
            open_interest = int(put_data["openInterest"]) if put_data.get("openInterest") is not None else None,
            volume        = int(put_data["volume"]) if put_data.get("volume") is not None else None,
        ))
        return legs

    # Long leg (single or spread). SHORT_PUT is a sell leg — side overridden.
    _leg_side = "sell" if strategy == OptionStrategy.SHORT_PUT else "buy"
    legs.append(OptionsLeg(
        occ_symbol    = _build_occ_symbol(symbol, expiry, option_type, long_strike),
        underlying    = symbol,
        side          = _leg_side,
        qty           = 1,            # qty per contract — OptionsStructure.contracts scales
        option_type   = option_type,
        strike        = float(long_strike),
        expiration    = expiry,
        bid           = _maybe_float(long_data.get("bid")),
        ask           = _maybe_float(long_data.get("ask")),
        mid           = _mid_price(long_data),
        delta         = _maybe_float(long_data.get("delta")),
        open_interest = int(long_data["openInterest"]) if long_data.get("openInterest") is not None else None,
        volume        = int(long_data["volume"]) if long_data.get("volume") is not None else None,
    ))

    # Short leg (spreads only)
    if short_data is not None and short_strike is not None:
        legs.append(OptionsLeg(
            occ_symbol    = _build_occ_symbol(symbol, expiry, option_type, short_strike),
            underlying    = symbol,
            side          = "sell",
            qty           = 1,
            option_type   = option_type,
            strike        = float(short_strike),
            expiration    = expiry,
            bid           = _maybe_float(short_data.get("bid")),
            ask           = _maybe_float(short_data.get("ask")),
            mid           = _mid_price(short_data),
            delta         = _maybe_float(short_data.get("delta")),
            open_interest = int(short_data["openInterest"]) if short_data.get("openInterest") is not None else None,
            volume        = int(short_data["volume"]) if short_data.get("volume") is not None else None,
        ))

    return legs


def _build_occ_symbol(symbol: str, expiry: str, option_type: str, strike: float) -> str:
    r"""
    Build OCC option symbol in Alpaca format (no ticker padding).

    Format: {TICKER}{YYMMDD}{C|P}{8-digit strike×1000}
    Alpaca regex: ^[A-Z]{1,5}\d{6}[CP]\d{8}$
    Examples:
      GLD,    2025-12-19, call, 435.0  -> "GLD251219C00435000"
      NVDA,   2026-04-25, put,  800.0  -> "NVDA260425P00800000"
    """
    ticker = symbol.replace("/", "").upper()
    date_obj = date.fromisoformat(expiry)
    date_str = date_obj.strftime("%y%m%d")
    cp       = "C" if option_type == "call" else "P"
    strike_i = int(round(strike * 1000))
    return f"{ticker}{date_str}{cp}{strike_i:08d}"


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _dte_max_for_strategy(strategy: OptionStrategy) -> int:
    """Return the maximum DTE target for a given strategy."""
    _DTE_MAP = {
        OptionStrategy.SINGLE_CALL:        21,
        OptionStrategy.SINGLE_PUT:         21,
        OptionStrategy.SHORT_PUT:          45,
        OptionStrategy.CALL_DEBIT_SPREAD:  28,
        OptionStrategy.PUT_DEBIT_SPREAD:   28,
        OptionStrategy.CALL_CREDIT_SPREAD: 45,
        OptionStrategy.PUT_CREDIT_SPREAD:  45,
        OptionStrategy.STRADDLE:           28,
        OptionStrategy.STRANGLE:           28,
    }
    return _DTE_MAP.get(strategy, 28)


def _max_cost_for_tier(
    equity:     float,
    strategy:   OptionStrategy,
    tier:       Tier,
    sizing_cfg: dict,
) -> float:
    """Compute max cost in USD from equity and sizing config."""
    is_spread = strategy in (
        OptionStrategy.CALL_DEBIT_SPREAD, OptionStrategy.PUT_DEBIT_SPREAD,
        OptionStrategy.CALL_CREDIT_SPREAD, OptionStrategy.PUT_CREDIT_SPREAD,
        OptionStrategy.SHORT_PUT,   # size on stop-loss risk budget (5%)
    )
    if tier == Tier.DYNAMIC:
        pct = float(sizing_cfg.get("dynamic_max_pct", 0.03))
    elif is_spread:
        pct = float(sizing_cfg.get("core_spread_max_pct", 0.05))
    else:
        # Single legs, straddles, strangles: 3% max (total debit = full risk)
        pct = float(sizing_cfg.get("core_single_leg_max_pct", 0.03))
    return equity * pct


def _maybe_float(v) -> Optional[float]:
    """Coerce to float, returning None on failure."""
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
