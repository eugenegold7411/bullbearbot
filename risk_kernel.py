"""
risk_kernel.py — Sole authority on whether/how much/how to trade.

Input:  TradeIdea + BrokerSnapshot + Optional[SignalScore] + config dict
Output: BrokerAction (approved) | str (rejection reason)

All qty / stop / target / margin / exposure math lives here.
No broker API calls — pure computation. No file I/O.

Translation chain (Account 1):
  ClaudeDecision.ideas[] -> process_idea() -> BrokerAction -> execute_all()

Translation chain (Account 2):
  ClaudeDecision.ideas[] -> process_options_idea() -> OptionsAction
  -> options_execution.submit_structure()

Public API:
  process_idea(idea, snapshot, signal, config, current_price,
               session_tier, vix) -> BrokerAction | str

  process_options_idea(idea, snapshot, signal, config, iv_summary,
                       options_regime, current_price) -> OptionsAction | str

Public API:
  get_vix_context_note(vix, config) -> str | None

Internal helpers (exported for testing):
  eligibility_check(idea, snapshot, config, session_tier, vix) -> str | None
  size_position(idea, snapshot, config, current_price, vix)
      -> tuple[float, float]          (qty, position_value)
  place_stops(idea, current_price, config)
      -> tuple[float, float]          (stop_loss, take_profit)
  select_structure(direction, iv_summary, options_regime, tier)
      -> OptionStrategy | None
  select_expiry(strategy, available_expirations) -> str
  compute_real_economics(strategy, current_price, iv, equity, tier,
                         a2_config, size_mult) -> tuple[int, float]
  liquidity_gate(symbol, iv_summary) -> str | None
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Union

from schemas import (
    AccountAction,
    BrokerAction,
    BrokerSnapshot,
    Conviction,
    Direction,
    OptionsAction,
    OptionStrategy,
    SignalScore,
    Tier,
    TradeIdea,
    alpaca_symbol,
    is_crypto,
    normalize_symbol,
)

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Hard-coded safety constants (never overridden by config)
# ─────────────────────────────────────────────────────────────────────────────

PDT_FLOOR         = 26_000.0   # never trade below this equity
MIN_RR_RATIO      = 2.0        # minimum reward/risk
MAX_OPTIONS_USD   = 5_000.0    # max cost per options trade

# VIX band thresholds — overridable via strategy_config.json parameters section.
# Defaults here match the config values so the kernel is safe with a bare config dict.
_VIX_BAND_DEFAULTS: dict = {
    "vix_calm_threshold":            20.0,   # below = calm, no restrictions
    "vix_elevated_threshold":        25.0,   # 20-25 = elevated, no hard blocks
    "vix_cautious_threshold":        30.0,   # 25-30 = cautious, intraday long blocked
    "vix_stressed_threshold":        40.0,   # 30-40 = stressed, intraday+dynamic blocked; 40+ = crisis
    "vix_stressed_conviction_floor": 0.75,   # minimum conviction for CORE buy in stressed regime
}

# Backward-compat aliases for tests and callers that imported these constants.
VIX_HALT   = 35.0   # legacy; actual crisis threshold is vix_stressed_threshold (default 40)
VIX_CAUTION = 25.0  # legacy; actual size-cut threshold is vix_elevated_threshold (default 25)

# Per-tier max position as fraction of equity (hard ceilings)
_TIER_MAX_PCT: dict[str, float] = {
    "core":     0.20,
    "dynamic":  0.15,
    "intraday": 0.05,
}
# High-conviction core bump: matches executor's special case
_CORE_HIGH_CONVICTION_PCT = 0.25

# Stop loss ceilings by tier and asset class
# These cap the stop — Claude may request tighter but never wider
_MAX_STOP_PCT: dict[str, dict[str, float]] = {
    "stocks": {
        "core":     0.04,   # 4%  (floor of config stop_loss_pct_core)
        "dynamic":  0.05,   # 5%
        "intraday": 0.02,   # 2%  (MAX_STOP_PCT_INTRADAY from executor)
    },
    "crypto": {
        "core":     0.08,   # 8%  (crypto volatility floor)
        "dynamic":  0.10,   # 10%
        "intraday": 0.08,   # crypto intraday same as core floor
    },
}

# DTE ranges by options strategy
_DTE_BY_STRATEGY: dict[OptionStrategy, tuple[int, int]] = {
    OptionStrategy.CALL_DEBIT_SPREAD:  (14, 21),
    OptionStrategy.PUT_DEBIT_SPREAD:   (14, 21),
    OptionStrategy.CALL_CREDIT_SPREAD: (21, 45),
    OptionStrategy.PUT_CREDIT_SPREAD:  (21, 45),
    OptionStrategy.SINGLE_CALL:        (7,  14),
    OptionStrategy.SINGLE_PUT:         (7,  14),
    OptionStrategy.STRADDLE:           (14, 28),
    OptionStrategy.CLOSE_OPTION:       (0,  0),
}

# Debit cost estimates per strategy type (per contract, in dollars)
# Used when live IV chain is unavailable
_DEBIT_EST: dict[str, float] = {
    "spread":  300.0,   # $3.00 × 100
    "single":  500.0,   # $5.00 × 100
    "straddle": 800.0,  # $8.00 × 100
}


# ─────────────────────────────────────────────────────────────────────────────
# Config accessors (safe, never raise)
# ─────────────────────────────────────────────────────────────────────────────

def _params(config: dict) -> dict:
    return config.get("parameters", {})


def _sizing(config: dict) -> dict:
    return config.get("position_sizing", {})


def _a2_config(config: dict) -> dict:
    return config.get("account2", {})


def _vix_params(config: dict) -> dict:
    """Read VIX band thresholds from config parameters, falling back to defaults."""
    p = _params(config)
    return {k: float(p.get(k, v)) for k, v in _VIX_BAND_DEFAULTS.items()}


def _default_stop_pct(config: dict, tier: str, asset_class: str) -> float:
    """Default stop pct from config, floored by hard ceiling."""
    params = _params(config)
    if tier == "intraday":
        base = float(params.get("stop_loss_pct_intraday", 0.018))
    else:
        base = float(params.get("stop_loss_pct_core", 0.035))
    # Crypto gets a wider floor regardless
    if asset_class == "crypto":
        base = max(base, _MAX_STOP_PCT["crypto"].get(tier, 0.08))
    return base


def _max_stop_pct(tier: str, asset_class: str) -> float:
    """Hard ceiling on stop width."""
    return _MAX_STOP_PCT.get(asset_class, _MAX_STOP_PCT["stocks"]).get(tier, 0.05)


def _float_to_conviction(v: float, config: dict | None = None) -> Conviction:
    """Convert conviction float (0.0-1.0) to Conviction enum for BrokerAction/OptionsAction."""
    cfg = config or {}
    thresholds = _params(cfg).get("margin_sizing_conviction_thresholds", {})
    high_t = float(thresholds.get("high", 0.75))
    med_t  = float(thresholds.get("medium", 0.50))
    if v >= high_t:
        return Conviction.HIGH
    if v >= med_t:
        return Conviction.MEDIUM
    return Conviction.LOW


def _get_margin_multiplier(conviction: float, symbol: str, config: dict) -> float:
    """
    Return margin multiplier for a given conviction score.

    Tiers (from strategy_config.json margin_sizing_multiplier_tiers):
        MEDIUM:      0.50 - 0.6499 → 1x
        HIGH:        0.65 - 0.7249 → 2x
        STRONG HIGH: 0.725 - 0.7999 → 3x
        VERY HIGH:   0.80+          → 4x

    Crypto cap: max_crypto_margin_multiplier (default 2.0)
    Fallback: margin_sizing_multiplier (flat, default 4.0) when tiers absent.
    _compute_sizing_basis applies the legacy HIGH/MEDIUM split on the fallback path.
    """
    params = _params(config)
    tiers  = params.get("margin_sizing_multiplier_tiers")
    if not tiers:
        return float(params.get("margin_sizing_multiplier", 4.0))

    multiplier = 1.0  # default — no boost for sub-medium
    for _tier_name, tier_cfg in tiers.items():
        tier_min = float(tier_cfg.get("min", 0))
        tier_max = float(tier_cfg.get("max", 1))
        tier_mult = float(tier_cfg.get("multiplier", 1.0))
        if tier_min <= conviction <= tier_max:
            multiplier = tier_mult
            break

    _CRYPTO_SYMBOLS = {"BTC/USD", "ETH/USD", "BTCUSD", "ETHUSD"}
    if symbol.upper() in _CRYPTO_SYMBOLS:
        crypto_cap = float(params.get("max_crypto_margin_multiplier", 2.0))
        multiplier = min(multiplier, crypto_cap)

    return multiplier


def _compute_sizing_basis(
    snapshot: BrokerSnapshot,
    conviction: float,
    config: dict,
    symbol: str = "",
) -> float:
    """
    Returns the dollar basis used for per-position sizing.

    When margin_sizing_multiplier_tiers present (tiered path):
      conviction >= medium_thresh → min(bp, equity × _get_margin_multiplier())
      conviction < medium_thresh  → equity

    Legacy path (no tiers):
      HIGH   (>= thresholds["high"], default 0.75) → min(buying_power, equity × multiplier)
      MEDIUM (>= thresholds["medium"], default 0.50) → min(buying_power, equity × (multiplier / 2.0))
      LOW   / margin_authorized=False              → equity only

    Always floors at equity (via bp safety floor) so sizing never goes below
    cash-account behavior. Used by size_position (per-position dollar budget).
    """
    equity = snapshot.equity
    bp     = max(snapshot.buying_power, equity)  # safety floor — never below equity

    params    = config.get("parameters", {}) if config else {}
    margin_ok = bool(params.get("margin_authorized", False))
    thresholds = params.get(
        "margin_sizing_conviction_thresholds",
        {"high": 0.75, "medium": 0.50},
    )
    high_t = float(thresholds.get("high", 0.75))
    med_t  = float(thresholds.get("medium", 0.50))

    if params.get("margin_sizing_multiplier_tiers"):
        # Tiered path: per-conviction multiplier from _get_margin_multiplier()
        if margin_ok and conviction >= med_t:
            mult = _get_margin_multiplier(conviction, symbol, config)
            return min(bp, equity * mult)
    else:
        # Legacy flat path: HIGH gets full mult, MEDIUM gets mult/2
        flat = float(params.get("margin_sizing_multiplier", 1.0))
        if margin_ok and conviction >= high_t:
            return min(bp, equity * flat)
        if margin_ok and conviction >= med_t:
            return min(bp, equity * (flat / 2.0))

    return equity


def _effective_exposure_cap(
    snapshot: BrokerSnapshot,
    conviction: float,
    config: dict | None = None,
) -> float:
    """
    Max total portfolio exposure allowed for this conviction level.

    Reads conviction thresholds and margin multiplier from config:
      >= high_thresh   → mult × equity (full margin)
      >= medium_thresh → (mult / 2.0) × equity (partial margin)
      < medium_thresh  → 1.0× equity (no margin)
    Hard ceiling: never exceed mult × equity or buying_power.

    Defaults (when config absent): high=0.75, medium=0.50, mult=3.0
    — preserving the original hardcoded behavior.
    """
    cfg  = config or {}
    equity = snapshot.equity
    bp     = max(snapshot.buying_power, equity)  # safety floor

    thresholds = _params(cfg).get("margin_sizing_conviction_thresholds", {})
    high_t = float(thresholds.get("high", 0.75))
    med_t  = float(thresholds.get("medium", 0.50))
    _tiers = _params(cfg).get("margin_sizing_multiplier_tiers", {})
    if _tiers:
        mult = max(float(t.get("multiplier", 1.0)) for t in _tiers.values())
    else:
        mult = float(_params(cfg).get("margin_sizing_multiplier", 3.0))

    if conviction >= high_t:
        cap = equity * mult
    elif conviction >= med_t:
        cap = equity * (mult / 2.0)
    else:
        cap = equity * 1.0

    return min(cap, equity * mult, bp)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Eligibility check
# ─────────────────────────────────────────────────────────────────────────────

def _check_time_bound_actions(
    symbol: str,
    intent: str,
    config: dict,
    current_time_utc: str,
) -> tuple[bool, str]:
    """
    Block new long entries on symbols with pending exit mandates.
    Returns (eligible, reason).
    """
    if intent not in ("enter_long", "enter_short"):
        return True, ""

    time_bound = config.get("time_bound_actions", [])

    try:
        now = datetime.fromisoformat(
            current_time_utc.replace("Z", "+00:00")
        )
    except (ValueError, AttributeError):
        now = datetime.now(timezone.utc)

    for tba in time_bound:
        tba_symbol = tba.get("symbol", "")
        tba_action = tba.get("action", "")
        deadline_str = (
            tba.get("exit_by")
            or tba.get("deadline_utc")
            or tba.get("deadline_et")
        )

        if tba_symbol != symbol:
            continue
        if tba_action != "exit":
            continue
        if not deadline_str:
            continue

        try:
            # Parse deadline — handle both UTC and ET formats
            if "T" in deadline_str and (
                "Z" in deadline_str or "+" in deadline_str
            ):
                deadline = datetime.fromisoformat(
                    deadline_str.replace("Z", "+00:00")
                )
            else:
                # ET datetime string — treat as same-day constraint
                from zoneinfo import ZoneInfo
                deadline = datetime.strptime(
                    deadline_str, "%Y-%m-%d %H:%M"
                ).replace(tzinfo=ZoneInfo("America/New_York"))

            # Block if deadline is today or in the past
            deadline_date = deadline.date()
            now_date = now.date()

            if deadline_date <= now_date:
                return False, (
                    f"time_bound_action: {symbol} has mandatory "
                    f"exit by {deadline_str} — blocking new "
                    f"{intent} entry"
                )
        except Exception as _e:
            # If we can't parse the deadline, block to be safe
            return False, (
                f"time_bound_action: {symbol} deadline parse "
                f"failed ({_e}) — blocking entry as precaution"
            )

    return True, ""


def _get_et_now():
    """Return current datetime in US/Eastern. Extracted for testability."""
    from zoneinfo import ZoneInfo  # noqa: PLC0415
    return datetime.now(ZoneInfo("America/New_York"))


def get_vix_context_note(vix: float, config: dict) -> Optional[str]:
    """
    Return a context string describing the current VIX regime for prompt injection.
    Returns None when VIX is calm (< vix_calm_threshold) — no note needed.

    VIX bands (default thresholds, overridable via strategy_config parameters):
      calm      (< 20):  None — full participation
      elevated  (20-25): note only — no hard blocks
      cautious  (25-30): intraday long blocked
      stressed  (30-40): intraday+dynamic long blocked; core needs conviction >= floor
      crisis    (>= 40): all long entries blocked; bearish fully enabled
    """
    vp = _vix_params(config)
    if vix >= vp["vix_stressed_threshold"]:
        return (
            f"VIX {vix:.1f} — CRISIS regime (>= {vp['vix_stressed_threshold']:.0f}): "
            f"all new long entries blocked. Bearish entries and exits fully enabled."
        )
    if vix >= vp["vix_cautious_threshold"]:
        floor = vp["vix_stressed_conviction_floor"]
        return (
            f"VIX {vix:.1f} — STRESSED regime "
            f"({vp['vix_cautious_threshold']:.0f}–{vp['vix_stressed_threshold']:.0f}): "
            f"intraday and dynamic long entries blocked; "
            f"core long requires conviction >= {floor:.0%}. "
            f"Bearish and defined-risk structures preferred."
        )
    if vix >= vp["vix_elevated_threshold"]:
        return (
            f"VIX {vix:.1f} — CAUTIOUS regime "
            f"({vp['vix_elevated_threshold']:.0f}–{vp['vix_cautious_threshold']:.0f}): "
            f"intraday long entries blocked. Core and dynamic long allowed. "
            f"Bearish entries fully enabled."
        )
    if vix >= vp["vix_calm_threshold"]:
        return f"VIX {vix:.1f} — ELEVATED: consider tighter stops on new long entries."
    return None  # calm — no note needed


def eligibility_check(
    idea: TradeIdea,
    snapshot: BrokerSnapshot,
    config: dict,
    session_tier: str = "market",
    vix: float = 20.0,
    current_time_utc: str = "",
) -> Optional[str]:
    """
    Hard gates — returns rejection reason string, or None if eligible.

    Checks (in order):
      0. Time-bound action block (same-day mandatory exits)
      1. VIX halt (> 35)
      2. PDT equity floor (< $26K)
      3. Session gate — stocks/ETFs require market session
      4. Intraday tier requires market session
      5. Max open positions
      6. Catalyst required for buys
    """
    act    = idea.action
    symbol = normalize_symbol(idea.symbol)
    crypto = is_crypto(symbol)

    # ── 0. Time-bound action block ────────────────────────────────────────────
    _tba_time = current_time_utc or datetime.now(timezone.utc).isoformat()
    _tba_ok, _tba_reason = _check_time_bound_actions(
        symbol=symbol,
        intent=idea.intent,
        config=config,
        current_time_utc=_tba_time,
    )
    if not _tba_ok:
        return _tba_reason

    # ── 1. VIX graduated gate ─────────────────────────────────────────────────
    # Bearish entries (inverse ETFs, hedges) are never VIX-gated: high VIX is
    # precisely when bearish trades have the most edge. Only long (bullish/neutral)
    # entries face graduated restrictions.
    if act == AccountAction.BUY and idea.direction != Direction.BEARISH:
        vp             = _vix_params(config)
        _vix_elevated  = vp["vix_elevated_threshold"]   # 25 — start of cautious
        _vix_cautious  = vp["vix_cautious_threshold"]   # 30 — start of stressed
        _vix_stressed  = vp["vix_stressed_threshold"]   # 40 — start of crisis
        _conv_floor    = vp["vix_stressed_conviction_floor"]

        if vix >= _vix_stressed:
            # crisis (>= 40): block all long entries
            return (
                f"VIX {vix:.1f} >= {_vix_stressed:.0f} — crisis regime, "
                f"no new long entries"
            )
        if vix >= _vix_cautious:
            # stressed (30-40): INTRADAY+DYNAMIC blocked; CORE needs conviction floor
            if idea.tier in (Tier.INTRADAY, Tier.DYNAMIC):
                return (
                    f"VIX {vix:.1f} >= {_vix_cautious:.0f} — stressed regime, "
                    f"{idea.tier.value} long entries blocked"
                )
            if idea.tier == Tier.CORE and idea.conviction < _conv_floor:
                return (
                    f"VIX {vix:.1f} stressed regime — CORE entry requires "
                    f"conviction >= {_conv_floor:.2f} (got {idea.conviction:.2f})"
                )
        elif vix >= _vix_elevated:
            # cautious (25-30): INTRADAY long entries blocked only
            if idea.tier == Tier.INTRADAY:
                return (
                    f"VIX {vix:.1f} >= {_vix_elevated:.0f} — cautious regime, "
                    f"intraday long entries blocked"
                )

    # ── 2. Equity floor ───────────────────────────────────────────────────────
    if snapshot.equity < PDT_FLOOR:
        return (
            f"equity ${snapshot.equity:,.0f} below PDT floor "
            f"${PDT_FLOOR:,.0f}"
        )

    # ── 3. Session gate (stocks/ETFs only) ────────────────────────────────────
    if act == AccountAction.BUY and not crypto and session_tier != "market":
        return (
            f"session={session_tier} — stock/ETF buys require market session"
        )

    # ── 4. Intraday tier gate ─────────────────────────────────────────────────
    if act == AccountAction.BUY and idea.tier == Tier.INTRADAY and session_tier != "market":
        return "intraday tier requires market session"

    # ── 4.5. Near-close gate ──────────────────────────────────────────────────
    # Block DYNAMIC/INTRADAY buys after 15:55 ET; CORE exempt until 16:00.
    # Exits and stops are never blocked. Non-fatal: allows trade if clock fails.
    if act == AccountAction.BUY and session_tier == "market":
        try:
            _et = _get_et_now()
            if _et.hour == 15 and _et.minute >= 55:
                _tier = getattr(idea, "tier", None)
                if _tier and _tier != Tier.CORE:
                    return (
                        f"near_close_gate: {_tier.value} entries blocked after 15:55 ET"
                    )
        except Exception:
            pass  # non-fatal — allow trade if timezone check fails

    # ── 5. Max positions ──────────────────────────────────────────────────────
    if act == AccountAction.BUY:
        max_pos = int(_params(config).get("max_positions", 15))
        n_open  = len([p for p in snapshot.positions if p.qty > 0])
        if n_open >= max_pos:
            return f"max_positions={max_pos} reached (currently {n_open} open)"

    # ── 6. Catalyst required for buys ─────────────────────────────────────────
    if act == AccountAction.BUY:
        cat = (idea.catalyst or "").strip().lower()
        blocked = _params(config).get(
            "catalyst_tag_disallowed_values", ["", "none", "null", "no"]
        )
        if cat in blocked:
            return "buy requires a named catalyst (catalyst_tag_required_for_entry)"

    # ── 7. ADD conviction gate ─────────────────────────────────────────────────
    if act == AccountAction.BUY:
        add_gate = float(_params(config).get("add_conviction_gate", 0.65))
        existing = any(
            p.symbol == normalize_symbol(idea.symbol) and p.qty > 0
            for p in snapshot.positions
        )
        if existing and idea.conviction < add_gate:
            return (
                f"add to existing {idea.symbol} requires conviction >= {add_gate:.2f} "
                f"(got {idea.conviction:.2f})"
            )

    return None  # all checks passed


# ─────────────────────────────────────────────────────────────────────────────
# 2. Position sizing
# ─────────────────────────────────────────────────────────────────────────────

def size_position(
    idea: TradeIdea,
    snapshot: BrokerSnapshot,
    config: dict,
    current_price: float,
    vix: float = 20.0,
) -> Union[tuple[float, float], str]:
    """
    Compute (qty, position_value) for a BUY idea.

    Returns rejection reason str if sizing is impossible
    (no headroom, price zero, etc.).

    Sizing logic:
      1. tier_pct from config (core=15%, dynamic=8%, intraday=5%)
      2. +bump to 20% for HIGH conviction CORE (matches executor)
      3. VIX scaling: 50% reduction when VIX >= VIX_CAUTION (25)
      4. Cap to available exposure headroom
      5. Integer qty for stocks; up-to-6-decimal for crypto
    """
    if current_price is None or current_price <= 0:
        return "current_price unavailable or zero"

    tier_str     = idea.tier.value
    equity       = snapshot.equity                                    # PDT/log/aggregate cap
    sizing_basis = _compute_sizing_basis(snapshot, idea.conviction, config, idea.symbol)
    crypto       = is_crypto(idea.symbol)

    log.info(
        "[RISK] size_position %s: conviction=%.2f sizing_basis=$%.0f "
        "equity=$%.0f bp=$%.0f margin_ok=%s",
        idea.symbol, idea.conviction, sizing_basis, equity,
        max(snapshot.buying_power, equity),
        bool(config.get("parameters", {}).get("margin_authorized", False)),
    )

    # ── Tier pct ─────────────────────────────────────────────────────────────
    tier_pct = float(
        _sizing(config).get(f"{tier_str}_tier_pct", _TIER_MAX_PCT.get(tier_str, 0.08))
    )
    _high_thresh = float(
        _params(config).get("margin_sizing_conviction_thresholds", {}).get("high", 0.75)
    )
    if idea.conviction >= _high_thresh and idea.tier == Tier.CORE:
        tier_pct = _CORE_HIGH_CONVICTION_PCT  # 25%

    # ── VIX scaling ───────────────────────────────────────────────────────────
    size_mult = 0.5 if vix >= _vix_params(config)["vix_elevated_threshold"] else 1.0

    # ── Dollar budget ─────────────────────────────────────────────────────────
    max_dollars = sizing_basis * tier_pct * size_mult

    # ── Exposure headroom ─────────────────────────────────────────────────────
    eff_cap  = _effective_exposure_cap(snapshot, idea.conviction, config)
    headroom = eff_cap - snapshot.exposure_dollars
    if headroom <= 0:
        return (
            f"no exposure headroom: current ${snapshot.exposure_dollars:,.0f} "
            f"vs cap ${eff_cap:,.0f} ({idea.conviction:.2f} conviction)"
        )
    max_dollars = min(max_dollars, headroom)

    if max_dollars < current_price:
        return (
            f"budget ${max_dollars:,.0f} < price ${current_price:,.2f} "
            f"(not enough for 1 share/unit)"
        )

    # ── max_position_pct_capacity hard cap ───────────────────────────────────
    # Denominator is total_capacity = exposure_dollars + buying_power, not equity.
    max_pos_pct = _params(config).get("max_position_pct_capacity")
    if max_pos_pct is not None:
        total_capacity = snapshot.exposure_dollars + snapshot.buying_power
        max_pos_dollars = total_capacity * float(max_pos_pct)
        # Subtract existing position value so ADD orders don't breach cap.
        existing_val = next(
            (p.market_value for p in snapshot.positions if p.symbol == idea.symbol), 0.0
        )
        max_pos_dollars = max(0.0, max_pos_dollars - existing_val)
        if existing_val:
            log.debug(
                "[RISK] %s: max_position_pct_capacity headroom adjusted by existing $%.0f → $%.0f",
                idea.symbol, existing_val, max_pos_dollars,
            )
        if max_dollars > max_pos_dollars:
            log.debug(
                "[RISK] %s: budget $%.0f capped to $%.0f by max_position_pct_capacity=%.0f%%",
                idea.symbol, max_dollars, max_pos_dollars, float(max_pos_pct) * 100,
            )
            max_dollars = max_pos_dollars
        if max_dollars < current_price:
            return (
                f"budget ${max_dollars:,.0f} < price ${current_price:,.2f} "
                f"after max_position_pct_capacity cap"
            )

    # ── Qty ──────────────────────────────────────────────────────────────────
    raw_qty = max_dollars / current_price
    if crypto:
        qty = round(raw_qty, 6)
    else:
        qty = max(1, int(raw_qty))

    position_value = round(qty * current_price, 2)

    log.debug(
        "[RISK] size_position %s: tier=%s pct=%.0f%% vix_mult=%.1f "
        "budget=$%.0f qty=%s val=$%.0f",
        idea.symbol, tier_str, tier_pct * 100, size_mult,
        max_dollars, qty, position_value,
    )

    return (qty, position_value)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Stop / target placement
# ─────────────────────────────────────────────────────────────────────────────

def place_stops(
    idea: TradeIdea,
    current_price: float,
    config: dict,
) -> Union[tuple[float, float], str]:
    """
    Compute (stop_loss, take_profit) prices for a BUY entry.

    Stop logic:
      1. Use idea.advisory_stop_pct if provided; else config default
      2. Cap at hard ceiling (_MAX_STOP_PCT by tier + asset_class)
      3. Intraday hard ceiling: 2%

    Target logic:
      1. Use idea.advisory_target_r if provided; else config take_profit_multiple
      2. Enforce MIN_RR_RATIO (2.0× minimum)

    Returns rejection str if R/R is not achievable.
    """
    if current_price is None or current_price <= 0:
        return "current_price unavailable — cannot place stops"

    tier_str   = idea.tier.value
    asset_cls  = "crypto" if is_crypto(idea.symbol) else "stocks"
    max_stop   = _max_stop_pct(tier_str, asset_cls)
    params     = _params(config)

    # ── Stop pct ─────────────────────────────────────────────────────────────
    if idea.advisory_stop_pct is not None and idea.advisory_stop_pct > 0:
        stop_pct = min(float(idea.advisory_stop_pct), max_stop)
        if stop_pct < float(idea.advisory_stop_pct):
            log.debug(
                "[RISK] %s: advisory_stop_pct %.1f%% capped to %.1f%% "
                "(tier=%s asset=%s)",
                idea.symbol,
                idea.advisory_stop_pct * 100, stop_pct * 100,
                tier_str, asset_cls,
            )
    else:
        default = _default_stop_pct(config, tier_str, asset_cls)
        stop_pct = min(default, max_stop)

    # ── Target R/R ────────────────────────────────────────────────────────────
    if idea.advisory_target_r is not None and idea.advisory_target_r >= 1.0:
        target_r = float(idea.advisory_target_r)
    else:
        target_r = float(params.get("take_profit_multiple", 2.5))

    # Enforce minimum R/R
    target_r = max(target_r, MIN_RR_RATIO)

    # ── Price levels ─────────────────────────────────────────────────────────
    stop_dist   = current_price * stop_pct
    stop_loss   = round(current_price - stop_dist, 2)
    take_profit = round(current_price + stop_dist * target_r, 2)

    # Sanity: stop must be below entry, target above
    if stop_loss >= current_price:
        return f"stop_loss ${stop_loss:.2f} >= current_price ${current_price:.2f}"
    if take_profit <= current_price:
        return f"take_profit ${take_profit:.2f} <= current_price ${current_price:.2f}"

    actual_rr = (take_profit - current_price) / (current_price - stop_loss)
    if actual_rr < MIN_RR_RATIO:
        return (
            f"R/R {actual_rr:.2f}x below minimum {MIN_RR_RATIO}x "
            f"(stop=${stop_loss:.2f} target=${take_profit:.2f})"
        )

    log.debug(
        "[RISK] place_stops %s: stop_pct=%.1f%% stop=$%.2f target=$%.2f R/R=%.2fx",
        idea.symbol, stop_pct * 100, stop_loss, take_profit, actual_rr,
    )

    return (stop_loss, take_profit)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Tier cap — Option C: Claude can request core, capped if signal score < threshold
# ─────────────────────────────────────────────────────────────────────────────

_TIER_CAP_SCORE_THRESHOLD: float = 65.0


def apply_tier_cap(ideas: list, signal_scores_obj: dict) -> None:
    """Cap Tier.CORE → Tier.DYNAMIC for BUY ideas whose signal score < 65.

    Mutates ideas in place. If the symbol is not in signal_scores_obj, the
    tier is left unchanged (can't validate without a score). Only BUY actions
    are subject to the cap — holds/closes are unaffected.

    Called from bot.py before the kernel loop.
    """
    if not signal_scores_obj or not ideas:
        return
    scored = signal_scores_obj.get("scored_symbols", {})
    for idea in ideas:
        if not hasattr(idea, "tier") or not hasattr(idea, "action"):
            continue
        if idea.tier != Tier.CORE or idea.action != AccountAction.BUY:
            continue
        sym = getattr(idea, "symbol", "")
        sig = scored.get(sym) or scored.get(sym.replace("/", ""))
        if sig is None:
            continue  # symbol absent from scorer — cannot validate, leave as-is
        score = float(sig.get("score", 50.0))
        if score < _TIER_CAP_SCORE_THRESHOLD:
            scorer_tier_raw = sig.get("tier", "dynamic")
            try:
                capped = Tier(scorer_tier_raw)
            except ValueError:
                capped = Tier.DYNAMIC
            if capped == Tier.CORE:
                capped = Tier.DYNAMIC  # don't let scorer also claim core on a weak score
            log.warning(
                "[TIER_CAP] %s requested core but signal score %.1f < %.1f — "
                "capping to %s (scorer tier: %s)",
                sym, score, _TIER_CAP_SCORE_THRESHOLD, capped.value, scorer_tier_raw,
            )
            idea.tier = capped


# ─────────────────────────────────────────────────────────────────────────────
# 5. Main entry point — Account 1
# ─────────────────────────────────────────────────────────────────────────────

def process_idea(
    idea: TradeIdea,
    snapshot: BrokerSnapshot,
    signal: Optional[SignalScore],
    config: dict,
    current_price: Optional[float] = None,
    session_tier: str = "market",
    vix: float = 20.0,
    current_time_utc: Optional[str] = None,
) -> Union[BrokerAction, str]:
    """
    Core Account 1 trade construction.

    Returns BrokerAction on success, rejection reason str on failure.

    Dispatch:
      HOLD       → passthrough (qty=0, no stops — exit_manager handles stops)
      CLOSE/SELL → look up position qty in snapshot
      BUY        → eligibility + sizing + stops
      REALLOCATE → size entry side; include exit info for executor
    """
    act    = idea.action
    symbol = normalize_symbol(idea.symbol)

    # ── HOLD ─────────────────────────────────────────────────────────────────
    if act == AccountAction.HOLD:
        return BrokerAction(
            symbol=symbol,
            action=AccountAction.HOLD,
            qty=0,
            order_type="market",
            tier=idea.tier,
            conviction=_float_to_conviction(idea.conviction, config),
            catalyst=idea.catalyst or "hold",
            sector_signal=idea.sector_signal,
            source_idea=idea,
        )

    # ── CLOSE / SELL ──────────────────────────────────────────────────────────
    if act in (AccountAction.CLOSE, AccountAction.SELL):
        pos_sym = alpaca_symbol(symbol)   # Alpaca position key
        # Search by both canonical and Alpaca format
        pos = (
            snapshot.position_by_symbol.get(symbol)
            or snapshot.position_by_symbol.get(pos_sym)
        )
        if pos is None:
            return f"no open position for {symbol} to {act.value}"
        if pos.qty <= 0:
            return f"position qty={pos.qty} for {symbol} — nothing to {act.value}"
        qty = pos.qty if is_crypto(symbol) else float(int(pos.qty))
        return BrokerAction(
            symbol=symbol,
            action=act,
            qty=qty,
            order_type="market",
            tier=idea.tier,
            conviction=_float_to_conviction(idea.conviction, config),
            catalyst=idea.catalyst or f"{act.value} {symbol}",
            sector_signal=idea.sector_signal,
            source_idea=idea,
        )

    # ── BUY ───────────────────────────────────────────────────────────────────
    if act == AccountAction.BUY:
        # Eligibility
        _utc = current_time_utc or datetime.now(timezone.utc).isoformat()
        rejection = eligibility_check(
            idea, snapshot, config, session_tier, vix, _utc
        )
        if rejection:
            log.debug("[RISK] REJECTED %s %s — %s", act.value, symbol, rejection)
            return rejection

        # Size
        if current_price is None or current_price <= 0:
            return f"current_price unavailable for {symbol}"
        size_result = size_position(idea, snapshot, config, current_price, vix)
        if isinstance(size_result, str):
            log.debug("[RISK] REJECTED %s %s — size: %s", act.value, symbol, size_result)
            return size_result
        qty, position_value = size_result

        # Stops
        stops_result = place_stops(idea, current_price, config)
        if isinstance(stops_result, str):
            log.debug("[RISK] REJECTED %s %s — stops: %s", act.value, symbol, stops_result)
            return stops_result
        stop_loss, take_profit = stops_result

        order_type  = idea.order_type or "market"
        limit_price = idea.limit_price if order_type == "limit" else None

        log.info(
            "[RISK] APPROVED BUY %s qty=%s @ $%.2f  stop=$%.2f  target=$%.2f  "
            "tier=%s  conviction=%.2f  vix=%.1f",
            symbol, qty, current_price, stop_loss, take_profit,
            idea.tier.value, idea.conviction, vix,
        )

        return BrokerAction(
            symbol=symbol,
            action=AccountAction.BUY,
            qty=qty,
            order_type=order_type,
            tier=idea.tier,
            conviction=_float_to_conviction(idea.conviction, config),
            catalyst=idea.catalyst,
            stop_loss=stop_loss,
            take_profit=take_profit,
            limit_price=limit_price,
            sector_signal=idea.sector_signal,
            source_idea=idea,
        )

    # ── REALLOCATE ────────────────────────────────────────────────────────────
    if act == AccountAction.REALLOCATE:
        if not idea.exit_symbol or not idea.entry_symbol:
            return "reallocate requires both exit_symbol and entry_symbol"

        exit_sym  = normalize_symbol(idea.exit_symbol)
        entry_sym = normalize_symbol(idea.entry_symbol)

        # Verify exit position exists
        exit_pos = (
            snapshot.position_by_symbol.get(exit_sym)
            or snapshot.position_by_symbol.get(alpaca_symbol(exit_sym))
        )
        if exit_pos is None:
            return f"reallocate: no open position for exit_symbol {exit_sym}"

        # Eligibility for the entry side
        rejection = eligibility_check(
            idea._replace_symbol(entry_sym) if hasattr(idea, "_replace_symbol") else idea,
            snapshot, config, session_tier, vix,
        )
        if rejection:
            # Try with a synthetic idea substituting the entry symbol
            entry_idea = TradeIdea(
                symbol=entry_sym,
                action=AccountAction.BUY,
                tier=idea.tier,
                conviction=idea.conviction,
                direction=idea.direction,
                catalyst=idea.catalyst,
            )
            rejection = eligibility_check(entry_idea, snapshot, config, session_tier, vix)
            if rejection:
                return f"reallocate entry {entry_sym} rejected: {rejection}"

        if current_price is None or current_price <= 0:
            return f"current_price unavailable for entry {entry_sym}"

        # Synthesise entry idea for sizing
        entry_idea = TradeIdea(
            symbol=entry_sym,
            action=AccountAction.BUY,
            tier=idea.tier,
            conviction=idea.conviction,
            direction=idea.direction,
            catalyst=idea.catalyst,
            advisory_stop_pct=idea.advisory_stop_pct,
            advisory_target_r=idea.advisory_target_r,
            order_type=idea.order_type,
            limit_price=idea.limit_price,
        )
        size_result = size_position(entry_idea, snapshot, config, current_price, vix)
        if isinstance(size_result, str):
            return f"reallocate entry sizing failed: {size_result}"
        qty, _ = size_result

        stops_result = place_stops(entry_idea, current_price, config)
        if isinstance(stops_result, str):
            return f"reallocate entry stops failed: {stops_result}"
        stop_loss, take_profit = stops_result

        log.info(
            "[RISK] APPROVED REALLOCATE exit=%s entry=%s qty=%s "
            "stop=$%.2f target=$%.2f",
            exit_sym, entry_sym, qty, stop_loss, take_profit,
        )

        return BrokerAction(
            symbol=symbol,         # primary symbol for logging
            action=AccountAction.REALLOCATE,
            qty=qty,               # entry qty
            order_type=idea.order_type or "market",
            tier=idea.tier,
            conviction=_float_to_conviction(idea.conviction, config),
            catalyst=idea.catalyst,
            stop_loss=stop_loss,
            take_profit=take_profit,
            sector_signal=idea.sector_signal,
            exit_symbol=exit_sym,
            entry_symbol=entry_sym,
            source_idea=idea,
        )

    # Unknown action
    return f"unknown action '{act.value}'"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Options functions — Account 2
# ─────────────────────────────────────────────────────────────────────────────

def liquidity_gate(
    symbol: str,
    iv_summary: dict,
) -> Optional[str]:
    """
    Basic liquidity / IV validity gate before placing options.

    Returns None if trade is eligible, rejection reason str if not.

    Checks:
      - Observation mode (IV history < minimum days)
      - iv_environment unknown
      - Very expensive IV (avoid new long premium)
    """
    if not iv_summary:
        return f"{symbol}: no IV summary available"

    if iv_summary.get("observation_mode", True):
        days = iv_summary.get("history_days", 0)
        min_days = iv_summary.get("min_history_days", 20)
        return (
            f"observation_mode: {days}/{min_days} IV history days for {symbol}"
        )

    env = iv_summary.get("iv_environment", "unknown")
    if env == "unknown":
        return f"{symbol}: IV environment unknown — insufficient history"

    if env == "very_expensive":
        rank = iv_summary.get("iv_rank", "?")
        return f"{symbol}: IV rank {rank} — very_expensive environment, avoid new positions"

    return None


def select_structure(
    direction: Direction,
    iv_summary: dict,
    options_regime: dict,
    tier: Tier,
) -> Optional[OptionStrategy]:
    """
    Map IV environment + directional signal + tier → OptionStrategy.

    Returns None if no eligible strategy exists for this combination.

    IV hierarchy:
      very_cheap / cheap  (rank < 35) → buy premium: debit spread or single leg
      neutral             (35-65)     → debit spread preferred
      expensive           (65-80)     → sell premium: credit spread
      (very_expensive blocked by liquidity_gate before reaching here)

    Credit spread direction mapping:
      Bullish signal → sell PUT credit spread (below market, profit if stays up)
      Bearish signal → sell CALL credit spread (above market, profit if stays down)
    """
    env     = iv_summary.get("iv_environment", "unknown")
    allowed = options_regime.get("allowed_strategies", [])

    # Dynamic tier: single leg only
    if tier == Tier.DYNAMIC:
        return (
            OptionStrategy.SINGLE_CALL
            if direction != Direction.BEARISH
            else OptionStrategy.SINGLE_PUT
        )

    if env in ("very_cheap", "cheap"):
        if "debit_spread" in allowed:
            return (
                OptionStrategy.CALL_DEBIT_SPREAD
                if direction != Direction.BEARISH
                else OptionStrategy.PUT_DEBIT_SPREAD
            )
        if "single_leg" in allowed:
            return (
                OptionStrategy.SINGLE_CALL
                if direction != Direction.BEARISH
                else OptionStrategy.SINGLE_PUT
            )

    elif env == "neutral":
        if "debit_spread" in allowed:
            return (
                OptionStrategy.CALL_DEBIT_SPREAD
                if direction != Direction.BEARISH
                else OptionStrategy.PUT_DEBIT_SPREAD
            )

    elif env == "expensive":
        if "credit_spread" in allowed:
            # Bullish → sell puts below market; bearish → sell calls above market
            return (
                OptionStrategy.PUT_CREDIT_SPREAD
                if direction == Direction.BULLISH
                else OptionStrategy.CALL_CREDIT_SPREAD
            )

    return None  # no eligible strategy for this combination


def select_expiry(
    strategy: OptionStrategy,
    available_expirations: Optional[list[str]] = None,
) -> str:
    """
    Select best expiration date for the given strategy.

    If available_expirations is provided (real options chain dates),
    picks the date closest to the midpoint of the target DTE range.

    Otherwise, generates a synthetic target Friday from today.
    """
    dte_range = _DTE_BY_STRATEGY.get(strategy, (14, 21))
    target_dte = (dte_range[0] + dte_range[1]) // 2
    today = date.today()

    if available_expirations:
        best, best_dte = None, None
        for exp_str in sorted(available_expirations):
            try:
                exp_date = date.fromisoformat(exp_str)
                dte_val  = (exp_date - today).days
                if dte_range[0] <= dte_val <= dte_range[1]:
                    mid_dist = abs(dte_val - target_dte)
                    if best_dte is None or mid_dist < abs(best_dte - target_dte):
                        best, best_dte = exp_str, dte_val
            except Exception:
                continue
        if best:
            return best
        # Fallback: pick the first available expiry beyond the minimum DTE
        for exp_str in sorted(available_expirations):
            try:
                exp_date = date.fromisoformat(exp_str)
                if (exp_date - today).days >= max(dte_range[0], 5):
                    return exp_str
            except Exception:
                continue

    # Synthetic: nearest Friday at target DTE
    target_date = today + timedelta(days=target_dte)
    days_to_fri = (4 - target_date.weekday()) % 7
    if days_to_fri == 0 and target_dte < 5:
        days_to_fri = 7  # avoid same-day
    return (target_date + timedelta(days=days_to_fri)).isoformat()


def compute_real_economics(
    strategy: OptionStrategy,
    current_price: float,
    iv: float,
    equity: float,
    tier: Tier,
    a2_config: dict,
    size_mult: float = 1.0,
) -> tuple[int, float]:
    """
    Compute (contracts, max_cost_usd) for an options position.

    Uses conservative fixed-debit estimates when live IV chain is not provided:
      Debit spreads: $3.00 per contract
      Single legs:   $5.00 per contract
      Straddles:     $8.00 per contract

    Applies Account 2 size limits from config, then hard-caps at MAX_OPTIONS_USD.
    """
    strat_val   = strategy.value
    a2_sizing   = a2_config.get("position_sizing", {})

    # Tier / strategy size limit
    is_single   = "single" in strat_val
    is_straddle = "straddle" in strat_val
    is_credit   = "credit" in strat_val

    if tier == Tier.DYNAMIC:
        max_pct = float(a2_sizing.get("dynamic_max_pct", 0.03))
    elif is_single or is_straddle:
        max_pct = float(a2_sizing.get("core_single_leg_max_pct", 0.03))
    else:
        max_pct = float(a2_sizing.get("core_spread_max_pct", 0.05))

    max_budget = equity * max_pct * size_mult

    # Per-contract cost estimate
    if is_single:
        cost_per = _DEBIT_EST["single"]
    elif is_straddle:
        cost_per = _DEBIT_EST["straddle"]
    elif is_credit:
        # Credit spread: cost = max risk = spread_width - credit_received
        spread_width_dollars = current_price * 0.03 * 100   # ~3% width, 1 contract
        cost_per = spread_width_dollars * 0.67               # max risk = 67% of width
    else:
        cost_per = _DEBIT_EST["spread"]

    contracts   = max(1, int(max_budget / cost_per))
    actual_cost = round(contracts * cost_per, 2)

    # Hard cap: MAX_OPTIONS_USD
    if actual_cost > MAX_OPTIONS_USD:
        contracts   = max(1, int(MAX_OPTIONS_USD / cost_per))
        actual_cost = round(contracts * cost_per, 2)

    # Never exceed budget
    if actual_cost > max_budget:
        contracts   = max(1, int(max_budget / cost_per))
        actual_cost = round(contracts * cost_per, 2)

    return contracts, actual_cost


def process_options_idea(
    idea: TradeIdea,
    snapshot: BrokerSnapshot,
    signal: Optional[SignalScore],
    config: dict,
    iv_summary: dict,
    options_regime: dict,
    current_price: float,
) -> Union[OptionsAction, str]:
    """
    Account 2 options trade construction.

    Returns OptionsAction on success, rejection reason str on failure.

    Pipeline:
      1. Equity floor + crisis regime check
      2. Liquidity gate (observation mode, IV environment)
      3. Confidence gate
      4. select_structure() → OptionStrategy
      5. select_expiry() → expiration date
      6. compute_real_economics() → (contracts, max_cost_usd)
      7. Build OptionsAction

    HOLD ideas pass through immediately with action="hold".
    """
    symbol   = normalize_symbol(idea.symbol)
    a2_cfg   = _a2_config(config)
    equity   = snapshot.equity

    # ── HOLD passthrough ─────────────────────────────────────────────────────
    if idea.action in (AccountAction.HOLD, AccountAction.CLOSE):
        return OptionsAction(
            symbol=symbol,
            action="hold",
            option_strategy=OptionStrategy.CLOSE_OPTION,
            expiration="",
            long_strike=None,
            short_strike=None,
            contracts=0,
            max_cost_usd=0.0,
            tier=idea.tier,
            conviction=_float_to_conviction(idea.conviction, config),
            catalyst=idea.catalyst or "hold",
            direction=idea.direction,
            reason=f"action={idea.action.value}",
            source_idea=idea,
        )

    # ── A2 equity floor ───────────────────────────────────────────────────────
    a2_floor = float(a2_cfg.get("equity_floor", 25_000.0))
    if equity < a2_floor:
        return f"A2 equity ${equity:,.0f} below floor ${a2_floor:,.0f}"

    # ── Crisis regime ─────────────────────────────────────────────────────────
    if options_regime.get("regime") == "crisis":
        return "options_regime=crisis — no new options positions"

    # ── Liquidity gate ────────────────────────────────────────────────────────
    gate = liquidity_gate(symbol, iv_summary)
    if gate:
        return gate

    # ── Minimum confidence (options require medium+) ──────────────────────────
    if idea.conviction <= 0.35:
        return f"conviction={idea.conviction:.2f} — options require medium or high conviction (> 0.35)"

    # ── Strategy selection ────────────────────────────────────────────────────
    strategy = select_structure(idea.direction, iv_summary, options_regime, idea.tier)
    if strategy is None:
        env     = iv_summary.get("iv_environment", "unknown")
        allowed = options_regime.get("allowed_strategies", [])
        return (
            f"no eligible options strategy for direction={idea.direction.value} "
            f"iv_env={env} allowed={allowed}"
        )

    # Override with hint if provided and compatible
    if (idea.option_strategy_hint is not None
            and idea.option_strategy_hint != strategy):
        log.debug(
            "[RISK] %s: using option_strategy_hint %s (kernel selected %s)",
            symbol, idea.option_strategy_hint.value, strategy.value,
        )
        strategy = idea.option_strategy_hint

    # ── Expiry ────────────────────────────────────────────────────────────────
    expirations = iv_summary.get("available_expirations")   # real chain dates if available
    expiration  = select_expiry(strategy, expirations)

    # ── VIX / IV scaling ──────────────────────────────────────────────────────
    a2_cfg.get("vix_gates", {})
    iv_rank   = iv_summary.get("iv_rank", 50)
    # Scale down 50% when: VIX > 25, IV rank > 60 (covered by options_regime size_mult)
    size_mult = float(options_regime.get("size_multiplier", 1.0))

    # ── Economics ─────────────────────────────────────────────────────────────
    current_iv   = float(iv_summary.get("current_iv", 0.30) or 0.30)
    contracts, max_cost = compute_real_economics(
        strategy=strategy,
        current_price=current_price,
        iv=current_iv,
        equity=equity,
        tier=idea.tier,
        a2_config=a2_cfg,
        size_mult=size_mult,
    )

    # ── Build strikes (placeholder — options_execution will use live chain) ───
    # The risk kernel computes a ballpark strike; options_execution.build_structure()
    # will resolve to real OCC strikes via the live chain lookup.
    long_strike  = _round_strike(current_price)
    short_strike = None
    if "spread" in strategy.value:
        spread_pct = 0.03  # ~3% width
        if "call" in strategy.value:
            short_strike = _round_strike(current_price * (1 + spread_pct))
        else:
            short_strike = _round_strike(current_price * (1 - spread_pct))

    greeks_cfg = a2_cfg.get("greeks", {})
    delta = float(greeks_cfg.get("min_delta", 0.30)) + 0.15  # ATM ~0.45

    action_str = (
        "sell_option_spread" if "credit" in strategy.value
        else "buy_option" if "single" in strategy.value or "straddle" in strategy.value
        else "buy_option"
    )

    log.info(
        "[RISK A2] APPROVED %s %s strategy=%s exp=%s contracts=%d max_cost=$%.0f "
        "iv_rank=%.0f",
        action_str, symbol, strategy.value, expiration,
        contracts, max_cost, iv_rank,
    )

    return OptionsAction(
        symbol=symbol,
        action=action_str,
        option_strategy=strategy,
        expiration=expiration,
        long_strike=long_strike,
        short_strike=short_strike,
        contracts=contracts,
        max_cost_usd=max_cost,
        tier=idea.tier,
        conviction=_float_to_conviction(idea.conviction, config),
        catalyst=idea.catalyst,
        direction=idea.direction,
        iv_rank=float(iv_rank) if iv_rank is not None else None,
        delta=round(delta, 2),
        rationale=(
            f"IV rank {iv_rank:.0f if isinstance(iv_rank, float) else iv_rank} "
            f"({iv_summary.get('iv_environment','?')}) — {strategy.value}. "
            f"{idea.catalyst}"
        ),
        confidence=0.0,   # filled by four-way debate synthesis in bot_options.py
        source_idea=idea,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _round_strike(price: float) -> float:
    """Round to nearest standard options strike increment."""
    if price < 10:
        return round(price * 2) / 2        # $0.50
    elif price < 25:
        return round(price)                 # $1.00
    elif price < 100:
        return round(price / 2.5) * 2.5    # $2.50
    elif price < 200:
        return round(price / 5) * 5        # $5.00
    else:
        return round(price / 10) * 10      # $10.00
