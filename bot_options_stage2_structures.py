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

# Mapping from route_strategy structure type → OptionStrategy enum.
# Used by Stage 4 execution and exposed here for backward compat (tests import from bot_options).
_STRATEGY_FROM_STRUCTURE: dict[str, _OS] = {
    "long_call":          _OS.SINGLE_CALL,
    "long_put":           _OS.SINGLE_PUT,
    "debit_call_spread":  _OS.CALL_DEBIT_SPREAD,
    "debit_put_spread":   _OS.PUT_DEBIT_SPREAD,
    "credit_call_spread": _OS.CALL_CREDIT_SPREAD,
    "credit_put_spread":  _OS.PUT_CREDIT_SPREAD,
}


# ── Strategy routing ──────────────────────────────────────────────────────────

_A2_ROUTER_DEFAULTS: dict = {
    "earnings_dte_blackout": 5,
    "min_liquidity_score":   0.3,
    "macro_iv_gate_rank":    60,
    "iv_env_blackout":       ["very_expensive"],
}


def _get_router_config(config: dict | None = None) -> dict:
    """Return a2_router config block, filling in v1 defaults for missing keys."""
    if not config:
        return dict(_A2_ROUTER_DEFAULTS)
    router_cfg = config.get("a2_router", {})
    return {**_A2_ROUTER_DEFAULTS, **router_cfg}


def _route_strategy(pack, config: dict | None = None) -> list[str]:
    """
    Deterministic rules decide which structures are legal BEFORE AI debate.
    Returns list of allowed structure types, empty list = no trade.

    Thresholds are read from config["a2_router"] with v1 safety defaults.
    Behavior is identical to prior hardcoded version when config matches defaults.
    """
    sym  = pack.symbol
    rcfg = _get_router_config(config)
    earnings_dte_blackout = int(rcfg["earnings_dte_blackout"])
    min_liquidity_score   = float(rcfg["min_liquidity_score"])
    macro_iv_gate_rank    = float(rcfg["macro_iv_gate_rank"])
    iv_env_blackout       = list(rcfg["iv_env_blackout"])

    # Rule 1: earnings blackout
    if pack.earnings_days_away is not None and pack.earnings_days_away <= earnings_dte_blackout:
        log.debug("[OPTS] _route_strategy %s: RULE1 earnings_blackout days=%s <= %d → []",
                  sym, pack.earnings_days_away, earnings_dte_blackout)
        return []

    # Rule 2: no long premium in blacklisted IV environments
    if pack.iv_environment in iv_env_blackout:
        log.debug("[OPTS] _route_strategy %s: RULE2 iv_env=%s in blackout=%s → []",
                  sym, pack.iv_environment, iv_env_blackout)
        return []

    # Rule 3: liquidity floor
    if pack.liquidity_score < min_liquidity_score:
        log.debug("[OPTS] _route_strategy %s: RULE3 liquidity=%.2f < %.2f → []",
                  sym, pack.liquidity_score, min_liquidity_score)
        return []

    # Rule 4: macro event + elevated IV
    if pack.macro_event_flag and pack.iv_rank > macro_iv_gate_rank:
        log.debug("[OPTS] _route_strategy %s: RULE4 macro_event + iv_rank=%.1f > %.1f → []",
                  sym, pack.iv_rank, macro_iv_gate_rank)
        return []

    # Rule 5: cheap IV + directional signal
    if pack.iv_environment in ("very_cheap", "cheap") and pack.a1_direction != "neutral":
        allowed = ["long_call", "long_put", "debit_call_spread", "debit_put_spread"]
        log.debug("[OPTS] _route_strategy %s: RULE5 iv_env=%s dir=%s → %s",
                  sym, pack.iv_environment, pack.a1_direction, allowed)
        return allowed

    # Rule 6: neutral IV + directional signal
    if pack.iv_environment == "neutral" and pack.a1_direction != "neutral":
        allowed = ["debit_call_spread", "debit_put_spread"]
        log.debug("[OPTS] _route_strategy %s: RULE6 iv_env=neutral dir=%s → %s",
                  sym, pack.a1_direction, allowed)
        return allowed

    # Rule 7: expensive IV + directional signal (debit only, no naked long)
    if pack.iv_environment == "expensive" and pack.a1_direction != "neutral":
        allowed = ["debit_call_spread", "debit_put_spread"]
        log.debug("[OPTS] _route_strategy %s: RULE7 iv_env=expensive dir=%s → %s",
                  sym, pack.a1_direction, allowed)
        return allowed

    # Rule 8: default no-trade
    log.debug("[OPTS] _route_strategy %s: RULE8 default no match (iv_env=%s dir=%s) → []",
              sym, pack.iv_environment, pack.a1_direction)
    return []


def _infer_router_rule_fired(pack, allowed: list[str], config: dict | None = None) -> str:
    """Infer which _route_strategy rule fired for audit logging in A2CandidateSet."""
    rcfg = _get_router_config(config)
    earnings_dte_blackout = int(rcfg["earnings_dte_blackout"])
    min_liquidity_score   = float(rcfg["min_liquidity_score"])
    macro_iv_gate_rank    = float(rcfg["macro_iv_gate_rank"])
    iv_env_blackout       = list(rcfg["iv_env_blackout"])

    if not allowed:
        if pack.earnings_days_away is not None and pack.earnings_days_away <= earnings_dte_blackout:
            return "RULE1"
        if pack.iv_environment in iv_env_blackout:
            return "RULE2"
        if pack.liquidity_score < min_liquidity_score:
            return "RULE3"
        if pack.macro_event_flag and pack.iv_rank > macro_iv_gate_rank:
            return "RULE4"
        return "RULE8"
    if pack.iv_environment in ("very_cheap", "cheap"):
        return "RULE5"
    if pack.iv_environment == "neutral":
        return "RULE6"
    if pack.iv_environment == "expensive":
        return "RULE7"
    return "RULE_UNKNOWN"


# ── Veto rules ────────────────────────────────────────────────────────────────

def _apply_veto_rules(candidate: dict, pack, equity: float) -> Optional[str]:
    """
    Apply deterministic veto rules to a fully-specified candidate structure.
    Returns None if all rules pass, or a rejection reason string.

    Rules:
      V1: bid_ask_spread_pct > 0.05  — entry cost too high
      V2: open_interest < 100        — liquidity risk
      V3: |theta| / debit > 0.05     — theta decay rate too fast
      V4: max_loss > equity × 0.03   — position too large
      V5: dte < 5                    — too close to expiry
      V6: expected_value < 0         — negative edge
    """
    spread = candidate.get("bid_ask_spread_pct")
    if spread is not None and spread > 0.05:
        return f"bid_ask_spread_pct={spread:.3f}>0.05"

    oi = candidate.get("open_interest")
    if oi is not None and oi < 100:
        return f"open_interest={oi}<100"

    theta = candidate.get("theta")
    debit = candidate.get("debit")
    if theta is not None and debit is not None and debit > 0:
        rate = abs(theta) / debit
        if rate > 0.05:
            return f"theta_decay_rate={rate:.3f}>0.05"

    max_loss = candidate.get("max_loss")
    if max_loss is not None and max_loss > equity * 0.03:
        return f"max_loss={max_loss:.0f}>equity*0.03={equity*0.03:.0f}"

    dte = candidate.get("dte")
    if dte is not None and dte < 5:
        return f"dte={dte}<5"

    ev = candidate.get("expected_value")
    if ev is not None and ev < 0:
        return f"expected_value={ev:.2f}<0"

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
            return True, ""   # no chain data — pass through, let builder decide

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
            return True, ""   # no chain — pass through

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
        return True, ""   # always pass on error — never block on pre-screen failure


# ── Public API ────────────────────────────────────────────────────────────────

def build_candidate_structures(
    pack,
    equity: float,
    chain: dict,
    allowed_structures: list[str],
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
        )
        if _cand_structs is not None:
            generated = list(_cand_structs)
            for c in generated:
                reason = _apply_veto_rules(c, pack, equity)
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
                log.info("[OPTS] %s: all %d structures vetoed (%s) — skipping",
                         pack.symbol, len(generated), _sample_reason)
    except Exception as _exc:
        log.debug("[OPTS] %s: build_candidate_structures failed (non-fatal): %s",
                  pack.symbol, _exc)

    return generated, vetoed, surviving
