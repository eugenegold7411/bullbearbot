"""
bot_options_stage2_structures.py — A2 Stage 2: strategy routing and structure veto.

Public API:
  build_candidate_structures(pack, equity, chain, allowed_structures)
      -> (generated, vetoed, surviving)

Responsibilities:
  - _route_strategy() — deterministic rules gate which structures are legal
  - _apply_veto_rules() — per-candidate deterministic veto
  - _quick_liquidity_check() — pre-debate liquidity pre-screen
  - build_candidate_structures() — assembles all three stages above
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from log_setup import get_logger
from schemas import OptionStrategy as _OS

log = get_logger(__name__)

# Mapping from route_strategy structure type -> OptionStrategy enum.
# Used by Stage 4 execution and exposed here for backward compat (tests import from bot_options).
_STRATEGY_FROM_STRUCTURE: dict[str, _OS] = {
    "long_call":          _OS.SINGLE_CALL,
    "long_put":           _OS.SINGLE_PUT,
    "short_put":          _OS.SHORT_PUT,
    "debit_call_spread":  _OS.CALL_DEBIT_SPREAD,
    "debit_put_spread":   _OS.PUT_DEBIT_SPREAD,
    "credit_call_spread": _OS.CALL_CREDIT_SPREAD,
    "credit_put_spread":  _OS.PUT_CREDIT_SPREAD,
    "straddle":           _OS.STRADDLE,
    "strangle":           _OS.STRANGLE,
    "iron_condor":        _OS.IRON_CONDOR,
    "iron_butterfly":     _OS.IRON_BUTTERFLY,
}


# ── Strategy routing ──────────────────────────────────────────────────────────

_A2_ROUTER_DEFAULTS: dict = {
    "earnings_dte_blackout": 5,
    "earnings_dte_window":   14,   # RULE_EARNINGS active when blackout < dte <= window
    "earnings_iv_rank_gate": 70,   # RULE_EARNINGS only fires when iv_rank < this
    "min_liquidity_score":   0.3,
    "macro_iv_gate_rank":    60,
    "iv_env_blackout":       [],  # S7-VOL: very_expensive now routes to credit spreads (RULE2_CREDIT)
    # Post-earnings IV crush config (RULE_POST_EARNINGS)
    "post_earnings_window_premarket":              2,   # days since earnings to stay in window (pre-mkt print)
    "post_earnings_window_postmarket":             1,   # days since earnings to stay in window (post-mkt print)
    "post_earnings_window_unknown":                1,   # days since earnings when timing unknown
    "post_earnings_iv_rank_min":                  75,   # minimum IV rank to enter post-earnings credit spread
    "post_earnings_iv_already_crushed_threshold": 15,  # rank-point drop that signals crush already done
    # Pre-earnings high-IV credit config (RULE_EARNINGS_HIGH_IV -- DISABLED BY DEFAULT)
    "pre_earnings_credit_spread_enabled":        False, # master gate -- must be true for rule to fire
    "pre_earnings_iv_rank_min":                   85,   # IV rank floor (very elevated only)
    "pre_earnings_dte_min":                        7,   # minimum DTE for pre-earnings credit spread
    "pre_earnings_dte_max":                       14,   # maximum DTE for pre-earnings credit spread
    # RULE_STRADDLE_STRANGLE: cheap IV + approaching earnings window
    "straddle_iv_rank_max":  40,   # IV rank ceiling (cheap premium required)
    "straddle_dte_min":       6,   # minimum DTE window for straddle/strangle entry
    "straddle_dte_max":      14,   # maximum DTE window for straddle/strangle entry
    # RULE_SHORT_PUT: sell OTM put in elevated IV + bullish/neutral environments
    "short_put_iv_rank_min": 50,   # minimum IV rank to enter short put
    # RULE_IRON: iron condor/butterfly when IV is very elevated + neutral outlook
    "iron_iv_rank_min": 70,        # minimum IV rank for iron structures
}


def _get_earnings_timing(sym: str, earnings_calendar_data: dict) -> str:
    """
    Return 'pre_market', 'post_market', or 'unknown' for the most recent
    past earnings event for a symbol. Used to set RULE_POST_EARNINGS window width.
    """
    today = date.today()
    best: tuple | None = None   # (earnings_date, timing_str)
    for entry in earnings_calendar_data.get("calendar", []):
        if entry.get("symbol", "").upper() != sym.upper():
            continue
        raw = entry.get("earnings_date", "")
        if not raw:
            continue
        try:
            eda_date = date.fromisoformat(str(raw)[:10])
            if eda_date < today:
                if best is None or eda_date > best[0]:
                    raw_timing = str(entry.get("timing", "")).lower()
                    norm = ("pre_market"  if any(k in raw_timing for k in ("pre", "bmo")) else
                            "post_market" if any(k in raw_timing for k in ("post", "amc", "after")) else
                            "unknown")
                    best = (eda_date, norm)
        except Exception:
            continue
    return best[1] if best is not None else "unknown"


def _iv_already_crushed(sym: str, current_iv_rank: float, threshold: float = 15.0) -> bool:
    """
    Return True if IV rank dropped by more than `threshold` points since yesterday.
    Uses the same 52-week rank formula as compute_iv_rank() but comparing the
    last two entries in the IV history file.
    Returns False if history is unavailable (conservative -- don't skip trade).
    """
    try:
        import json as _json  # noqa: PLC0415
        from pathlib import Path as _Path  # noqa: PLC0415
        hist_path = (_Path(__file__).parent / "data" / "options" / "iv_history"
                     / f"{sym}_iv_history.json")
        if not hist_path.exists():
            return False
        history = _json.loads(hist_path.read_text())
        ivs = [e["iv"] for e in history if isinstance(e, dict) and e.get("iv", 0) > 0]
        if len(ivs) < 2:
            return False
        low  = min(ivs)
        high = max(ivs)
        if high == low:
            return False
        today_rank     = (ivs[-1] - low) / (high - low) * 100
        yesterday_rank = (ivs[-2] - low) / (high - low) * 100
        drop = yesterday_rank - today_rank
        return drop >= threshold
    except Exception:
        return False


def _get_router_config(config: dict | None = None) -> dict:
    """Return a2_router config block, filling in v1 defaults for missing keys."""
    if not config:
        return dict(_A2_ROUTER_DEFAULTS)
    router_cfg = config.get("a2_router", {})
    return {**_A2_ROUTER_DEFAULTS, **router_cfg}


def _route_strategy(
    pack,
    config: dict | None = None,
    earnings_calendar_data: dict | None = None,
) -> list[str]:
    """
    Deterministic rules decide which structures are legal BEFORE AI debate.
    Returns list of allowed structure types, empty list = no trade.

    Thresholds are read from config["a2_router"] with v1 safety defaults.
    Behavior is identical to prior hardcoded version when config matches defaults.

    earnings_calendar_data: optional dict with "calendar" list used by
    RULE_POST_EARNINGS for timing-aware window width. Loaded from disk if None.

    Rule order:
      RULE_EARNINGS_HIGH_IV  — pre-earnings credit spread (disabled by default)
      RULE1                  — upcoming earnings blackout (0 <= eda <= blackout)
      RULE2                  — IV environment blackout
      RULE3                  — liquidity floor
      RULE4                  — macro event + elevated IV
      RULE_POST_EARNINGS     — post-earnings IV crush trade (eda < 0)
      RULE_EARNINGS          — near-earnings direction split
      RULE2_CREDIT           — very_expensive IV -> credit structures
      RULE5                  — cheap IV + directional
      RULE6                  — neutral IV + directional
      RULE7                  — expensive IV + directional
      RULE8                  — default no-trade
    """
    sym  = pack.symbol
    rcfg = _get_router_config(config)
    earnings_dte_blackout = int(rcfg["earnings_dte_blackout"])
    earnings_dte_window   = int(rcfg.get("earnings_dte_window", 14))
    earnings_iv_rank_gate = float(rcfg.get("earnings_iv_rank_gate", 70))
    min_liquidity_score   = float(rcfg["min_liquidity_score"])
    macro_iv_gate_rank    = float(rcfg["macro_iv_gate_rank"])
    iv_env_blackout       = list(rcfg["iv_env_blackout"])

    # Post-earnings config
    pe_win_pre   = int(rcfg.get("post_earnings_window_premarket", 2))
    pe_win_post  = int(rcfg.get("post_earnings_window_postmarket", 1))
    pe_win_unk   = int(rcfg.get("post_earnings_window_unknown", 1))
    pe_iv_min    = float(rcfg.get("post_earnings_iv_rank_min", 75))
    pe_crush_thr = float(rcfg.get("post_earnings_iv_already_crushed_threshold", 15))

    # Pre-earnings high-IV config
    pre_earn_enabled = bool(rcfg.get("pre_earnings_credit_spread_enabled", False))
    pre_earn_iv_min  = float(rcfg.get("pre_earnings_iv_rank_min", 85))
    pre_earn_dte_min = int(rcfg.get("pre_earnings_dte_min", 7))
    pre_earn_dte_max = int(rcfg.get("pre_earnings_dte_max", 14))

    eda = pack.earnings_days_away

    # RULE_EARNINGS_HIGH_IV — disabled by default; preempts RULE1 for very-high IV pre-earnings.
    # When enabled: if earnings is within the target DTE range and IV is very elevated,
    # sell a credit spread to capture IV premium before the binary event.
    if pre_earn_enabled:
        if (eda is not None
                and pre_earn_dte_min <= eda <= pre_earn_dte_max
                and pack.iv_rank >= pre_earn_iv_min):
            if pack.a1_direction == "bullish":
                _pehi = ["credit_put_spread"]
            elif pack.a1_direction == "bearish":
                _pehi = ["credit_call_spread"]
            else:
                _pehi = ["credit_put_spread", "credit_call_spread"]
            log.debug(
                "[OPTS] _route_strategy %s: RULE_EARNINGS_HIGH_IV eda=%s iv_rank=%.1f dir=%s -> %s",
                sym, eda, pack.iv_rank, pack.a1_direction, _pehi,
            )
            return _pehi

    # RULE1: upcoming earnings blackout — block all trades within N days of earnings.
    # Uses 0 <= eda to exclude past earnings (negative eda handled by RULE_POST_EARNINGS).
    if eda is not None and 0 <= eda <= earnings_dte_blackout:
        log.debug("[OPTS] _route_strategy %s: RULE1 earnings_blackout days=%s <= %d -> []",
                  sym, eda, earnings_dte_blackout)
        return []

    # RULE2: no long premium in blacklisted IV environments
    if pack.iv_environment in iv_env_blackout:
        log.debug("[OPTS] _route_strategy %s: RULE2 iv_env=%s in blackout=%s -> []",
                  sym, pack.iv_environment, iv_env_blackout)
        return []

    # RULE3: liquidity floor
    if pack.liquidity_score < min_liquidity_score:
        log.debug("[OPTS] _route_strategy %s: RULE3 liquidity=%.2f < %.2f -> []",
                  sym, pack.liquidity_score, min_liquidity_score)
        return []

    # RULE4: macro event + elevated IV
    if pack.macro_event_flag and pack.iv_rank > macro_iv_gate_rank:
        log.debug("[OPTS] _route_strategy %s: RULE4 macro_event + iv_rank=%.1f > %.1f -> []",
                  sym, pack.iv_rank, macro_iv_gate_rank)
        return []

    # RULE_POST_EARNINGS: sell IV premium after earnings print when IV still elevated.
    # eda < 0 means earnings already happened (abs(eda) = days since print).
    # Timing-aware window: pre-market print gives a 2-day window; post-market gives 1.
    # Blocked if IV has already crashed (crush detected from history).
    if eda is not None and eda < 0:
        # Lazy-load earnings calendar from disk if not supplied by caller
        if earnings_calendar_data is None:
            try:
                import json as _json  # noqa: PLC0415
                from pathlib import Path as _Path  # noqa: PLC0415
                cal_path = _Path(__file__).parent / "data" / "market" / "earnings_calendar.json"
                earnings_calendar_data = _json.loads(cal_path.read_text()) if cal_path.exists() else {}
            except Exception:
                earnings_calendar_data = {}
        timing = _get_earnings_timing(sym, earnings_calendar_data)
        if timing == "pre_market":
            window = pe_win_pre
        elif timing == "post_market":
            window = pe_win_post
        else:
            window = pe_win_unk
        days_since = -eda   # positive: days since print
        if (days_since <= window
                and pack.iv_rank >= pe_iv_min
                and not _iv_already_crushed(sym, pack.iv_rank, pe_crush_thr)):
            if pack.a1_direction == "bullish":
                _pe = ["credit_put_spread"]
            elif pack.a1_direction == "bearish":
                _pe = ["credit_call_spread"]
            else:
                _pe = ["credit_put_spread", "credit_call_spread"]
            log.debug(
                "[OPTS] _route_strategy %s: RULE_POST_EARNINGS eda=%s timing=%s "
                "window=%d iv_rank=%.1f dir=%s -> %s",
                sym, eda, timing, window, pack.iv_rank, pack.a1_direction, _pe,
            )
            return _pe

    # RULE_STRADDLE_STRANGLE: cheap IV + earnings approaching in the straddle window.
    # Fires before RULE_EARNINGS so that when IV is cheap (<40) and earnings are
    # 6–14 days away, we prefer straddle/strangle over a directional debit spread.
    straddle_iv_max  = float(rcfg.get("straddle_iv_rank_max", 40))
    straddle_dte_min = int(rcfg.get("straddle_dte_min", 6))
    straddle_dte_max = int(rcfg.get("straddle_dte_max", 14))
    if (eda is not None
            and straddle_dte_min <= eda <= straddle_dte_max
            and eda > earnings_dte_blackout
            and pack.iv_rank < straddle_iv_max):
        allowed = ["straddle", "strangle"]
        log.info("[OPTS] RULE_STRADDLE_STRANGLE %s: eda=%d iv_rank=%.1f → %s",
                 sym, eda, pack.iv_rank, allowed)
        return allowed

    # RULE_EARNINGS: direction-split when near (but not in blackout for) earnings
    # AND iv_rank is not elevated. Elevated IV (>= gate) falls through to RULE2_CREDIT/7
    # so we don't buy premium into expected vol crush.
    if (eda is not None
            and earnings_dte_blackout < eda <= earnings_dte_window
            and pack.iv_rank < earnings_iv_rank_gate):
        if pack.a1_direction == "bullish":
            allowed = ["debit_call_spread", "straddle"]
        elif pack.a1_direction == "bearish":
            allowed = ["debit_put_spread", "straddle"]
        else:  # neutral
            allowed = ["straddle"]
        log.debug("[OPTS] _route_strategy %s: RULE_EARNINGS dte=%s iv_rank=%.1f dir=%s -> %s",
                  sym, eda, pack.iv_rank, pack.a1_direction, allowed)
        return allowed

    # RULE2_CREDIT: very expensive IV -> route to credit structures (sell premium)
    if pack.iv_environment == "very_expensive":
        if pack.a1_direction == "bullish":
            _vexp = ["credit_put_spread"]
        elif pack.a1_direction == "bearish":
            _vexp = ["credit_call_spread"]
        else:  # neutral -- allow both sides
            _vexp = ["credit_put_spread", "credit_call_spread"]
        log.debug("[OPTS] _route_strategy %s: RULE2_CREDIT iv_env=very_expensive dir=%s -> %s",
                  sym, pack.a1_direction, _vexp)
        return _vexp

    # RULE_IRON: iron condor when IV is very elevated (≥70) and direction is neutral.
    # iv_rank ≥ 85 with any direction routes to both iron_butterfly and iron_condor
    # (iron_butterfly pins; iron_condor gives wider profit range).
    # Fires after RULE2_CREDIT so very_expensive with directional view routes to credit spreads first.
    _iron_iv_min = float(rcfg.get("iron_iv_rank_min", 70))
    _iron_earn_ok = (eda is None or eda < 0 or eda > earnings_dte_blackout)
    if pack.iv_rank >= _iron_iv_min and _iron_earn_ok:
        if pack.iv_rank >= 85:
            _iron = ["iron_butterfly", "iron_condor"]
        elif pack.a1_direction == "neutral":
            _iron = ["iron_condor"]
        else:
            _iron = None
        if _iron is not None:
            log.debug(
                "[OPTS] _route_strategy %s: RULE_IRON iv_rank=%.1f dir=%s -> %s",
                sym, pack.iv_rank, pack.a1_direction, _iron,
            )
            return _iron

    # RULE_SHORT_PUT: sell OTM put when IV is elevated and direction is bullish/neutral.
    # After RULE_IRON (very high IV handled) and before debit/mixed rules.
    # iv_env check blocks cheap environments where selling premium has poor edge.
    _sp_iv_min = float(rcfg.get("short_put_iv_rank_min", 50))
    if (pack.iv_rank >= _sp_iv_min
            and pack.a1_direction in ("bullish", "neutral")
            and pack.iv_environment not in ("very_cheap", "cheap")):
        _sp_score      = pack.a1_signal_score or 0
        _sp_conviction = "high" if _sp_score >= 70 else "medium" if _sp_score >= 40 else "low"
        _sp_earn_ok    = (eda is None or eda < 0 or eda > earnings_dte_blackout)
        if _sp_conviction in ("high", "medium") and _sp_earn_ok:
            log.debug(
                "[OPTS] _route_strategy %s: RULE_SHORT_PUT iv_rank=%.1f iv_env=%s dir=%s -> ['short_put']",
                sym, pack.iv_rank, pack.iv_environment, pack.a1_direction,
            )
            return ["short_put"]

    # RULE5: cheap IV + directional signal
    if pack.iv_environment in ("very_cheap", "cheap") and pack.a1_direction != "neutral":
        allowed = ["long_call", "long_put", "debit_call_spread", "debit_put_spread"]
        log.debug("[OPTS] _route_strategy %s: RULE5 iv_env=%s dir=%s -> %s",
                  sym, pack.iv_environment, pack.a1_direction, allowed)
        return allowed

    # RULE6: neutral IV + directional signal
    if pack.iv_environment == "neutral" and pack.a1_direction != "neutral":
        allowed = ["debit_call_spread", "debit_put_spread"]
        log.debug("[OPTS] _route_strategy %s: RULE6 iv_env=neutral dir=%s -> %s",
                  sym, pack.a1_direction, allowed)
        return allowed

    # RULE7: expensive IV + directional signal (mixed: credit preferred, debit allowed)
    if pack.iv_environment == "expensive" and pack.a1_direction != "neutral":
        allowed = ["credit_put_spread", "credit_call_spread", "debit_call_spread", "debit_put_spread"]
        log.debug("[OPTS] _route_strategy %s: RULE7_MIXED iv_env=expensive dir=%s -> %s",
                  sym, pack.a1_direction, allowed)
        return allowed

    # RULE8: default no-trade
    log.debug("[OPTS] _route_strategy %s: RULE8 default no match (iv_env=%s dir=%s) -> []",
              sym, pack.iv_environment, pack.a1_direction)
    return []


def _infer_router_rule_fired(pack, allowed: list[str], config: dict | None = None) -> str:
    """Infer which _route_strategy rule fired for audit logging in A2CandidateSet."""
    rcfg = _get_router_config(config)
    earnings_dte_blackout = int(rcfg["earnings_dte_blackout"])
    earnings_dte_window   = int(rcfg.get("earnings_dte_window", 14))
    min_liquidity_score   = float(rcfg["min_liquidity_score"])
    macro_iv_gate_rank    = float(rcfg["macro_iv_gate_rank"])
    iv_env_blackout       = list(rcfg["iv_env_blackout"])
    pre_earn_enabled      = bool(rcfg.get("pre_earnings_credit_spread_enabled", False))
    pre_earn_iv_min       = float(rcfg.get("pre_earnings_iv_rank_min", 85))
    pre_earn_dte_min      = int(rcfg.get("pre_earnings_dte_min", 7))
    pre_earn_dte_max      = int(rcfg.get("pre_earnings_dte_max", 14))
    pe_iv_min             = float(rcfg.get("post_earnings_iv_rank_min", 75))

    eda = pack.earnings_days_away

    if not allowed:
        if (pre_earn_enabled
                and eda is not None
                and pre_earn_dte_min <= eda <= pre_earn_dte_max
                and pack.iv_rank >= pre_earn_iv_min):
            return "RULE_EARNINGS_HIGH_IV"
        if eda is not None and 0 <= eda <= earnings_dte_blackout:
            return "RULE1"
        if pack.iv_environment in iv_env_blackout:
            return "RULE2"
        if pack.liquidity_score < min_liquidity_score:
            return "RULE3"
        if pack.macro_event_flag and pack.iv_rank > macro_iv_gate_rank:
            return "RULE4"
        return "RULE8"

    # Non-empty allowed: infer which positive rule fired
    if (pre_earn_enabled
            and eda is not None
            and pre_earn_dte_min <= eda <= pre_earn_dte_max
            and pack.iv_rank >= pre_earn_iv_min):
        return "RULE_EARNINGS_HIGH_IV"
    if eda is not None and eda < 0 and pack.iv_rank >= pe_iv_min:
        return "RULE_POST_EARNINGS"
    straddle_iv_max  = float(rcfg.get("straddle_iv_rank_max", 40))
    straddle_dte_min = int(rcfg.get("straddle_dte_min", 6))
    straddle_dte_max = int(rcfg.get("straddle_dte_max", 14))
    if (eda is not None
            and straddle_dte_min <= eda <= straddle_dte_max
            and eda > earnings_dte_blackout
            and pack.iv_rank < straddle_iv_max):
        return "RULE_STRADDLE_STRANGLE"
    if (eda is not None
            and earnings_dte_blackout < eda <= earnings_dte_window):
        return "RULE_EARNINGS"
    if pack.iv_environment == "very_expensive":
        return "RULE2_CREDIT"
    _iron_iv_min_i = float(rcfg.get("iron_iv_rank_min", 70))
    if ("iron_condor" in allowed or "iron_butterfly" in allowed) and pack.iv_rank >= _iron_iv_min_i:
        return "RULE_IRON"
    if "short_put" in allowed:
        return "RULE_SHORT_PUT"
    if pack.iv_environment in ("very_cheap", "cheap"):
        return "RULE5"
    if pack.iv_environment == "neutral":
        return "RULE6"
    if pack.iv_environment == "expensive":
        return "RULE7"
    return "RULE_UNKNOWN"


# ── Veto thresholds ───────────────────────────────────────────────────────────

_A2_VETO_DEFAULTS: dict = {
    "max_bid_ask_spread_pct": 0.05,
    "min_open_interest":      50,
    "max_theta_decay_pct":    0.05,
    "max_loss_pct":           0.03,
    "min_dte":                5,
    "min_expected_value":     0.0,
}


def _get_veto_config(config: dict | None = None) -> dict:
    """Return a2_veto_thresholds config block, filling in v1 defaults for missing keys."""
    if not config:
        return dict(_A2_VETO_DEFAULTS)
    veto_cfg = config.get("a2_veto_thresholds", {})
    return {**_A2_VETO_DEFAULTS, **veto_cfg}


# ── Veto rules ────────────────────────────────────────────────────────────────

def _apply_veto_rules(
    candidate: dict,
    pack,
    equity: float,
    config: dict | None = None,
) -> Optional[str]:
    """
    Apply deterministic veto rules to a fully-specified candidate structure.
    Returns None if all rules pass, or a rejection reason string.

    Thresholds are read from config["a2_veto_thresholds"] with v1 safety defaults.

    Rules:
      V1: bid_ask_spread_pct > max_bid_ask_spread_pct  — entry cost too high
      V2: open_interest < min_open_interest            — liquidity risk
      V3: |theta| / debit > max_theta_decay_pct        — theta decay rate too fast
      V4: max_loss > equity × max_loss_pct              — position too large
      V5: dte < min_dte                                — too close to expiry
      V6: expected_value < min_expected_value          — negative edge
    """
    vcfg = _get_veto_config(config)
    max_spread  = float(vcfg["max_bid_ask_spread_pct"])
    min_oi      = int(vcfg["min_open_interest"])
    max_theta   = float(vcfg["max_theta_decay_pct"])
    max_loss_pct = float(vcfg["max_loss_pct"])
    min_dte     = int(vcfg["min_dte"])
    min_ev      = float(vcfg["min_expected_value"])

    spread = candidate.get("bid_ask_spread_pct")
    if spread is not None and spread > max_spread:
        return f"bid_ask_spread_pct={spread:.3f}>{max_spread}"

    oi = candidate.get("open_interest")
    if oi is not None and oi < min_oi:
        return f"open_interest={oi}<{min_oi}"

    theta = candidate.get("theta")
    debit = candidate.get("debit")
    if theta is not None and debit is not None and debit > 0:
        rate = abs(theta) / debit
        if rate > max_theta:
            return f"theta_decay_rate={rate:.3f}>{max_theta}"

    max_loss = candidate.get("max_loss")
    if max_loss is not None and max_loss > equity * max_loss_pct:
        return f"max_loss={max_loss:.0f}>equity*{max_loss_pct}={equity*max_loss_pct:.0f}"

    dte = candidate.get("dte")
    if dte is not None and dte < min_dte:
        return f"dte={dte}<{min_dte}"

    ev = candidate.get("expected_value")
    if ev is not None and ev < min_ev:
        return f"expected_value={ev:.2f}<{min_ev}"

    return None


# ── Liquidity pre-screen ──────────────────────────────────────────────────────

def _quick_liquidity_check(
    chain: dict,
    proposal,
    config: dict,
) -> tuple[bool, str]:
    """
    Pre-debate liquidity pre-screen using loose thresholds (50% of full gate).

    Checks ATM strike OI and volume for the expected option type.
    Returns (True, "") if passes, (False, reason) if fails.

    This is a pre-screen only — options_builder.validate_liquidity() is the
    final gate with full thresholds applied to the actual selected strikes.
    """
    try:
        liq_gates = config.get("account2", {}).get("liquidity_gates", {})
        oi_floor  = int(liq_gates.get("pre_debate_oi_floor", 100))
        vol_floor = int(liq_gates.get("pre_debate_volume_floor", 10))

        expirations = chain.get("expirations", {})
        if not expirations:
            return True, ""   # no chain data -- pass through, let builder decide

        # Pick first usable expiration (DTE >= 2)
        today = date.today()
        chosen_exp = None
        for exp_str in sorted(expirations.keys()):
            try:
                dte = (date.fromisoformat(exp_str) - today).days
                if dte >= 2:
                    chosen_exp = exp_str
                    break
            except Exception:
                continue

        if chosen_exp is None:
            return True, ""

        exp_data = expirations[chosen_exp]
        spot = chain.get("current_price", 0) or 0

        # Determine option type from proposal strategy
        from schemas import OptionStrategy as _OS  # noqa: PLC0415
        strat = proposal.strategy
        if strat in (_OS.SINGLE_CALL, _OS.CALL_DEBIT_SPREAD, _OS.CALL_CREDIT_SPREAD):
            opts = exp_data.get("calls", [])
        else:
            opts = exp_data.get("puts", [])

        if not opts:
            return True, ""   # no chain -- pass through

        # Find ATM strike
        atm = min(opts, key=lambda o: abs(float(o.get("strike", 0)) - spot))

        oi  = int(atm.get("openInterest", 0) or 0)
        vol = int(atm.get("volume", 0) or 0)

        if oi < oi_floor:
            return False, f"ATM OI={oi} < pre-debate floor {oi_floor}"
        if vol < vol_floor:
            return False, f"ATM vol={vol} < pre-debate floor {vol_floor}"

        return True, ""

    except Exception as _e:
        log.debug("[OPTS] _quick_liquidity_check failed (non-fatal, passing): %s", _e)
        return True, ""   # always pass on error -- never block on pre-screen failure


# ── Public API ────────────────────────────────────────────────────────────────

def _enrich_with_greeks(candidate: dict) -> None:
    """
    Enrich a surviving candidate with Alpaca option snapshot greeks when
    chain data did not provide them. Modifies candidate dict in place.
    Non-fatal — silently skips on any error.

    Adds/updates: delta, theta, vega (if None in candidate)
    Adds:         gamma, rho (not in candidate dict by default)
    """
    try:
        import options_data as _od  # noqa: PLC0415
        sym        = candidate.get("symbol", "")
        expiry     = candidate.get("expiry", "")
        long_stk   = candidate.get("long_strike")
        struct_type = candidate.get("structure_type", "")
        if not (sym and expiry and long_stk):
            return
        # Build OCC symbol in Alpaca format (no ticker padding)
        _date_obj = date.fromisoformat(expiry)
        _ticker   = sym.replace("/", "").upper()
        _cp       = "C" if "call" in struct_type else "P"
        _strike_i = int(round(float(long_stk) * 1000))
        occ_sym   = f"{_ticker}{_date_obj.strftime('%y%m%d')}{_cp}{_strike_i:08d}"
        g = _od.fetch_option_greeks(occ_sym)
        if not g:
            return
        if candidate.get("delta") is None:
            candidate["delta"] = g.get("delta")
        if candidate.get("theta") is None:
            candidate["theta"] = g.get("theta")
        if candidate.get("vega") is None:
            candidate["vega"] = g.get("vega")
        candidate.setdefault("gamma", g.get("gamma"))
        candidate.setdefault("rho",   g.get("rho"))
        log.debug("[OPTS] %s: greeks enriched from Alpaca snapshot (%s) "
                  "delta=%s theta=%s vega=%s",
                  sym, occ_sym, g.get("delta"), g.get("theta"), g.get("vega"))
    except Exception as _exc:
        log.debug("[OPTS] %s: greeks enrichment failed (non-fatal): %s",
                  candidate.get("symbol", "?"), _exc)


def build_candidate_structures(
    pack,
    equity: float,
    chain: dict,
    allowed_structures: list[str],
    config: dict | None = None,
    buying_power: float = 0.0,
) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Generate fully-specified candidate structures from an A2FeaturePack and
    apply deterministic veto rules.

    Returns (generated, vetoed, surviving) where:
      generated  — all raw candidates from generate_candidate_structures()
      vetoed     — list of {candidate_id, reason} for rejected candidates
      surviving  — candidates that passed all veto rules
    """
    generated: list[dict] = []
    vetoed: list[dict] = []
    surviving: list[dict] = []

    if not chain:
        return generated, vetoed, surviving

    try:
        from options_intelligence import (
            generate_candidate_structures as _gen,  # noqa: PLC0415
        )
        _cand_structs = _gen(
            pack=pack,
            allowed_structures=allowed_structures,
            equity=equity,
            chain=chain,
            config=config,
            buying_power=buying_power,
        )
        if _cand_structs is not None:
            generated = list(_cand_structs)
            for c in generated:
                reason = _apply_veto_rules(c, pack, equity, config=config)
                if reason is None:
                    surviving.append(c)
                else:
                    vetoed.append({"candidate_id": c.get("candidate_id", "?"), "reason": reason})
            _vetoed_count = len(vetoed)
            if _vetoed_count:
                log.debug("[OPTS] %s: %d/%d structures vetoed",
                          pack.symbol, _vetoed_count, len(generated))
            if not surviving:
                _sample_reason = vetoed[0]["reason"] if vetoed else "no_structures"
                log.info("[OPTS] %s: all %d structures vetoed (%s) -- skipping",
                         pack.symbol, len(generated), _sample_reason)
            # Enrich surviving candidates with Alpaca greeks where chain data was absent
            for c in surviving:
                if c.get("delta") is None or c.get("theta") is None:
                    _enrich_with_greeks(c)
    except Exception as _exc:
        log.debug("[OPTS] %s: build_candidate_structures failed (non-fatal): %s",
                  pack.symbol, _exc)

    return generated, vetoed, surviving
