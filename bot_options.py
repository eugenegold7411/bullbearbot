"""
bot_options.py — Account 2 options trading bot.

Runs independently from Account 1 (bot.py) on a separate Alpaca account.
Uses IV-first strategy selection with a four-way debate (Bull/Bear/IV Analyst/Synthesis).

Run directly for a single cycle:  python bot_options.py
Run via scheduler (90s offset after Account 1): scheduler.py handles this.

Account 2 credentials: ALPACA_API_KEY_OPTIONS / ALPACA_SECRET_KEY_OPTIONS
"""

import json
import logging
import math
import os
import time
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import anthropic
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient

import options_builder
import options_data
import options_executor
import options_intelligence
import options_state
import order_executor_options as oe_opts
import preflight as _preflight
from dataclasses import asdict
from log_setup import get_logger
from reconciliation import (
    reconcile_options_structures,
    plan_structure_repair,
    execute_reconciliation_plan,
)
from schemas import (
    A2FeaturePack,
    BrokerSnapshot,
    NormalizedOrder,
    NormalizedPosition,
    normalize_symbol,
    is_crypto as schema_is_crypto,
    StructureProposal,
)

load_dotenv()

log = get_logger("bot_options")

ET = ZoneInfo("America/New_York")
PROMPTS_DIR = Path(__file__).parent / "prompts"

# Model selection — same as Account 1
MODEL      = "claude-sonnet-4-6"
MODEL_FAST = "claude-haiku-4-5-20251001"

# Account 2 data paths (separate from Account 1)
_A2_DIR         = Path(__file__).parent / "data" / "account2"
_DECISION_LOG   = _A2_DIR / "trade_memory" / "decisions_account2.json"
_COST_LOG       = _A2_DIR / "costs" / "cost_log.jsonl"
def _ensure_dirs() -> None:
    _A2_DIR.mkdir(parents=True, exist_ok=True)
    (_A2_DIR / "trade_memory").mkdir(exist_ok=True)
    (_A2_DIR / "costs").mkdir(exist_ok=True)
    (_A2_DIR / "positions").mkdir(exist_ok=True)

# Observation mode: first 20 trading days while IV history builds
_OBS_MODE_DAYS        = 20
_OBS_MODE_FILE        = _A2_DIR / "obs_mode_state.json"
_OBS_SCHEMA_VERSION   = 2
# Core A2 symbols required for IV history before obs mode is fully meaningful
_OBS_IV_SYMBOLS = [
    "SPY", "QQQ", "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL",
    "TSM", "AMD", "XLE", "GLD", "TLT", "IWM", "XLF", "XBI",
]

# Equity floor
_EQUITY_FLOOR = 25_000.0

# Strategy config path (read for close-check config, roll decisions)
_STRATEGY_FILE = Path(__file__).parent / "strategy_config.json"


def _load_strategy_config() -> dict:
    """Load strategy_config.json. Returns {} on failure — non-fatal."""
    try:
        return json.loads(_STRATEGY_FILE.read_text(encoding="utf-8"))
    except Exception as _exc:
        log.debug("[OPTS] _load_strategy_config failed (non-fatal): %s", _exc)
        return {}

# Max cycles per day to log (cost control)
_MAX_DAILY_CYCLES = 48


# ── Client initialization ─────────────────────────────────────────────────────

def _build_alpaca_client() -> TradingClient:
    api_key = os.getenv("ALPACA_API_KEY_OPTIONS")
    secret  = os.getenv("ALPACA_SECRET_KEY_OPTIONS")
    base    = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    if not api_key or not secret:
        raise EnvironmentError(
            "ALPACA_API_KEY_OPTIONS / ALPACA_SECRET_KEY_OPTIONS not set in .env"
        )
    return TradingClient(api_key=api_key, secret_key=secret, paper=("paper" in base))


def _build_claude_client() -> anthropic.Anthropic:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise EnvironmentError("ANTHROPIC_API_KEY not set in .env")
    return anthropic.Anthropic(api_key=key)


# Lazy-init clients (so import doesn't fail if creds missing)
_alpaca: TradingClient | None = None
_claude: anthropic.Anthropic | None = None


def _get_alpaca() -> TradingClient:
    global _alpaca
    if _alpaca is None:
        _alpaca = _build_alpaca_client()
    return _alpaca


def _get_claude() -> anthropic.Anthropic:
    global _claude
    if _claude is None:
        _claude = _build_claude_client()
    return _claude


# ── Observation mode tracking ─────────────────────────────────────────────────

def _get_obs_mode_state() -> dict:
    """Load or initialize observation mode tracking state."""
    if _OBS_MODE_FILE.exists():
        try:
            return json.loads(_OBS_MODE_FILE.read_text())
        except Exception:
            pass
    return {
        "version": _OBS_SCHEMA_VERSION,
        "trading_days_observed": 0,
        "first_seen_date": None,
        "observation_complete": False,
        "iv_history_ready": False,
        "iv_ready_symbols": {},
    }


def _is_trading_day(iso_date: str) -> bool:
    """
    Return True if iso_date (YYYY-MM-DD) is a NYSE trading day.
    Excludes weekends. Excludes a fixed set of US market holidays.
    """
    from datetime import date
    d = date.fromisoformat(iso_date)
    # Weekends
    if d.weekday() >= 5:
        return False
    # Fixed NYSE holidays (year-agnostic month/day check)
    _fixed = {(1, 1), (7, 4), (12, 25)}
    if (d.month, d.day) in _fixed:
        return False
    # Floating holidays: MLK (3rd Mon Jan), Presidents (3rd Mon Feb),
    # Memorial (last Mon May), Labor (1st Mon Sep), Thanksgiving (4th Thu Nov)
    import calendar as _cal
    def _nth_weekday(year, month, weekday, n):
        """n-th occurrence (1-based) of weekday in month."""
        first = date(year, month, 1)
        delta = (weekday - first.weekday()) % 7
        return date(year, month, 1 + delta + (n - 1) * 7)
    def _last_monday(year, month):
        last = date(year, month, _cal.monthrange(year, month)[1])
        return last - __import__("datetime").timedelta(days=(last.weekday()) % 7)
    floating = {
        _nth_weekday(d.year, 1, 0, 3),   # MLK Day
        _nth_weekday(d.year, 2, 0, 3),   # Presidents Day
        _last_monday(d.year, 5),          # Memorial Day
        _nth_weekday(d.year, 9, 0, 1),   # Labor Day
        _nth_weekday(d.year, 11, 3, 4),  # Thanksgiving
    }
    return d not in floating


def _update_obs_mode_state(state: dict) -> bool:
    """
    Update observation mode counter. Increment trading_days_observed only on
    NYSE trading days (no weekends, no US market holidays).
    Returns True if still in observation mode.
    """
    from datetime import date
    today = date.today().isoformat()

    if state.get("observation_complete"):
        # v2 migration: patch in new fields if this is a pre-v2 state file
        if state.get("version", 1) < _OBS_SCHEMA_VERSION:
            state = _check_and_update_iv_ready(state)
            state["version"] = _OBS_SCHEMA_VERSION
            try:
                _OBS_MODE_FILE.write_text(json.dumps(state, indent=2))
                log.info("[OPTS] obs_mode_state.json migrated to v%d", _OBS_SCHEMA_VERSION)
            except Exception:
                pass
        return False

    if state.get("first_seen_date") is None:
        state["first_seen_date"] = today

    # Only count each trading day once — skip weekends and market holidays
    if state.get("last_counted_date") != today and _is_trading_day(today):
        state["trading_days_observed"] = state.get("trading_days_observed", 0) + 1
        state["last_counted_date"] = today
    elif not _is_trading_day(today):
        log.debug("[OPTS] Observation mode: %s is not a trading day — not counting", today)

    days = state["trading_days_observed"]
    log.info("[OPTS] Observation mode: %d/%d trading days", days, _OBS_MODE_DAYS)

    if days >= _OBS_MODE_DAYS:
        state["observation_complete"] = True
        state["version"] = _OBS_SCHEMA_VERSION
        state = _check_and_update_iv_ready(state)
        log.info("[OPTS] Observation mode COMPLETE — Account 2 now live trading")

    try:
        _OBS_MODE_FILE.write_text(json.dumps(state, indent=2))
    except Exception:
        pass

    return not state.get("observation_complete", False)


def is_observation_mode() -> bool:
    """Quick check: is Account 2 still in observation mode?"""
    state = _get_obs_mode_state()
    return not state.get("observation_complete", False)


def _check_and_update_iv_ready(state: dict) -> dict:
    """
    Check IV history readiness for all core A2 symbols via options_data.
    Writes iv_history_ready + iv_ready_symbols into state dict (in-place).
    Never modifies observation_complete. Non-fatal.
    Returns the mutated state dict.
    """
    try:
        result = options_data.check_iv_history_ready(_OBS_IV_SYMBOLS)
        state["iv_history_ready"] = result["all_ready"]
        state["iv_ready_symbols"] = result["symbol_ready"]
        log.info("[OPTS] IV history check: %d/%d symbols ready",
                 result["ready_count"], result["total_count"])
    except Exception as exc:  # noqa: BLE001
        log.warning("[OPTS] _check_and_update_iv_ready failed (non-fatal): %s", exc)
        state.setdefault("iv_history_ready", False)
        state.setdefault("iv_ready_symbols", {})
    return state


# ── Account 1 awareness ───────────────────────────────────────────────────────

def _load_account1_last_decision() -> dict:
    """
    Read Account 1's last decision from memory/decisions.json.
    Returns {} if not available. Non-fatal.
    """
    try:
        path = Path(__file__).parent / "data" / "trade_memory" / "decisions.json"
        if not path.exists():
            # Try alternate path
            path = Path(__file__).parent / "memory" / "decisions.json"
        if path.exists():
            data = json.loads(path.read_text())
            # Could be a list (JSONL-style) or a dict
            if isinstance(data, list) and data:
                return data[-1]  # most recent
            if isinstance(data, dict):
                return data
    except Exception as exc:
        log.debug("[OPTS] Could not load Account 1 decisions: %s", exc)
    return {}


def _summarize_account1_for_prompt(decision: dict) -> str:
    """Format Account 1's last decision as a compact prompt section."""
    if not decision:
        return "  Account 1: no recent decisions available."

    regime = decision.get("regime", "unknown")
    actions = decision.get("actions", [])
    reasoning = decision.get("reasoning", "")
    ts = decision.get("timestamp", "")[:16] if decision.get("timestamp") else ""

    lines = [f"  Account 1 last cycle [{ts}]:"]
    lines.append(f"    Regime: {regime}")
    if reasoning:
        lines.append(f"    Read: {reasoning[:150]}")

    open_syms = [a.get("symbol") for a in actions if a.get("action") not in ("hold",) and a.get("symbol")]
    if open_syms:
        lines.append(f"    Active trades: {', '.join(open_syms[:8])}")

    return "\n".join(lines)


# ── IV data for watchlist ─────────────────────────────────────────────────────

def _get_iv_summaries_for_symbols(symbols: list[str]) -> dict:
    """
    Return IV summaries for a list of symbols.
    Uses cached chains (refreshed in 4 AM block) + on-demand fetch if cache miss.
    Non-fatal per symbol.
    """
    summaries = {}
    for sym in symbols:
        try:
            chain = options_data.fetch_options_chain(sym)  # uses cache
            summary = options_data.get_iv_summary(sym, chain=chain)
            summaries[sym] = summary
        except Exception as exc:
            log.debug("[OPTS] IV summary failed for %s: %s", sym, exc)
            summaries[sym] = {
                "symbol": sym, "iv_environment": "unknown",
                "observation_mode": True, "history_days": 0,
            }
    return summaries


# ── Strategy selection ────────────────────────────────────────────────────────

def _quick_liquidity_check(
    chain: dict,
    proposal: "StructureProposal",
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
        from datetime import date as _date
        today = _date.today()
        chosen_exp = None
        for exp_str in sorted(expirations.keys()):
            try:
                dte = (_date.fromisoformat(exp_str) - today).days
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
        from schemas import OptionStrategy as _OS
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


def _load_earnings_days_away(symbol: str) -> Optional[int]:
    """
    Return days until the nearest earnings event for symbol, or None if unknown.
    Reads data/market/earnings_calendar.json. Non-fatal.
    """
    try:
        cal_path = Path(__file__).parent / "data" / "market" / "earnings_calendar.json"
        if not cal_path.exists():
            return None
        cal = json.loads(cal_path.read_text())
        today = date.today()
        min_days: Optional[int] = None
        for entry in cal.get("calendar", []):
            if entry.get("symbol", "").upper() != symbol.upper():
                continue
            raw = entry.get("earnings_date", "")
            if not raw:
                continue
            try:
                days = (date.fromisoformat(str(raw)[:10]) - today).days
                if days >= 0:
                    min_days = days if min_days is None else min(min_days, days)
            except Exception:
                continue
        return min_days
    except Exception:
        return None


def _build_a2_feature_pack(
    symbol: str,
    signal_scores: dict,
    iv_summaries: dict,
    equity: float,
    vix: float,
    chain: dict | None = None,
) -> Optional[A2FeaturePack]:
    """
    Build a normalized A2FeaturePack from available data.
    Returns None if required data is missing (IV summary required).
    Flow/GEX fields are all None until UW is integrated (Phase 2).
    """
    sig = signal_scores.get(symbol, {})
    iv  = iv_summaries.get(symbol, {})

    if not iv or iv.get("iv_environment", "unknown") == "unknown":
        log.debug("[OPTS] A2FeaturePack: %s — missing IV summary, skipping", symbol)
        return None

    iv_rank = iv.get("iv_rank")
    if iv_rank is None:
        log.debug("[OPTS] A2FeaturePack: %s — iv_rank=None, skipping", symbol)
        return None

    a1_direction = str(sig.get("direction", "neutral")).lower()
    if a1_direction not in ("bullish", "bearish", "neutral"):
        a1_direction = "neutral"

    # expected_move_pct: IV × sqrt(30/252) expressed as percentage
    current_iv = iv.get("current_iv") or 0.0
    expected_move_pct = round(current_iv * math.sqrt(30 / 252) * 100, 2) if current_iv > 0 else 0.0

    # Liquidity score from chain data (0-1); default 0.5 when chain unavailable
    liquidity_score = 0.5
    if chain and chain.get("expirations") and chain.get("current_price"):
        try:
            spot = float(chain["current_price"])
            exp_data = next(
                (v for k, v in sorted(chain["expirations"].items())
                 if (date.fromisoformat(k) - date.today()).days >= 2),
                None,
            )
            if exp_data:
                opts = exp_data.get("calls", []) or exp_data.get("puts", [])
                if opts:
                    atm = min(opts, key=lambda o: abs(float(o.get("strike", 0)) - spot))
                    oi  = int(atm.get("openInterest", 0) or 0)
                    vol = int(atm.get("volume", 0) or 0)
                    bid = float(atm.get("bid", 0) or 0)
                    ask = float(atm.get("ask", 0) or 0)
                    mid = (bid + ask) / 2 if (bid + ask) > 0 else 0
                    spread_pct = (ask - bid) / mid if mid > 0 else 1.0
                    oi_score      = min(1.0, oi / 500)
                    vol_score     = min(1.0, vol / 50)
                    spread_score  = max(0.0, 1.0 - spread_pct / 0.10)
                    liquidity_score = round((oi_score + vol_score + spread_score) / 3, 3)
        except Exception as _liq_exc:
            log.debug("[OPTS] A2FeaturePack: %s liquidity calc failed: %s", symbol, _liq_exc)

    # flow_imbalance_30m: (call_vol - put_vol) / (call_vol + put_vol) across ATM contracts
    flow_imbalance_30m = None
    if chain and chain.get("expirations") and chain.get("current_price"):
        try:
            _fi_spot = float(chain["current_price"])
            _fi_exp = next(
                (v for k, v in sorted(chain["expirations"].items())
                 if (date.fromisoformat(k) - date.today()).days >= 2),
                None,
            )
            if _fi_exp:
                _atm_band = _fi_spot * 0.05
                _call_vol = sum(
                    int(o.get("volume") or 0)
                    for o in _fi_exp.get("calls", [])
                    if abs(float(o.get("strike", 0)) - _fi_spot) <= _atm_band
                )
                _put_vol = sum(
                    int(o.get("volume") or 0)
                    for o in _fi_exp.get("puts", [])
                    if abs(float(o.get("strike", 0)) - _fi_spot) <= _atm_band
                )
                _total_vol = _call_vol + _put_vol
                if _total_vol > 0:
                    flow_imbalance_30m = round((_call_vol - _put_vol) / _total_vol, 4)
        except Exception as _fi_exc:
            log.debug("[OPTS] A2FeaturePack: %s flow_imbalance calc failed: %s", symbol, _fi_exc)

    data_sources = ["signal_scores", "iv_history"]
    if chain:
        data_sources.append("options_chain")
    if flow_imbalance_30m is not None:
        data_sources.append("flow_signals")

    pack = A2FeaturePack(
        symbol               = symbol,
        a1_signal_score      = float(sig.get("score", 0)),
        a1_direction         = a1_direction,
        trend_score          = None,
        momentum_score       = None,
        sector_alignment     = str(sig.get("sector_signal", sig.get("sector", ""))),
        iv_rank              = float(iv_rank),
        iv_environment       = str(iv.get("iv_environment", "unknown")),
        term_structure_slope = None,
        skew                 = None,
        expected_move_pct    = expected_move_pct,
        flow_imbalance_30m   = flow_imbalance_30m,
        sweep_count          = None,
        gex_regime           = None,
        oi_concentration     = None,
        earnings_days_away   = _load_earnings_days_away(symbol),
        macro_event_flag     = False,
        premium_budget_usd   = round(equity * 0.05, 2),
        liquidity_score      = liquidity_score,
        built_at             = datetime.now(timezone.utc).isoformat(),
        data_sources         = data_sources,
    )
    log.debug("[OPTS] A2FeaturePack built for %s: iv_rank=%.1f env=%s dir=%s earn=%s liq=%.2f",
              symbol, pack.iv_rank, pack.iv_environment, pack.a1_direction,
              pack.earnings_days_away, pack.liquidity_score)
    return pack


def _route_strategy(pack: A2FeaturePack) -> list[str]:
    """
    Deterministic rules decide which structures are legal BEFORE AI debate.
    Returns list of allowed structure types, empty list = no trade.

    IMPORTANT: These are v1 safety defaults, hardcoded for initial deployment.
    They are intentionally conservative. Each rule is a candidate for migration
    into strategy_config.json parameters in a future sprint once shadow validation
    confirms they are calibrated correctly. Do not treat them as permanent policy.
    """
    sym = pack.symbol

    # Rule 1: earnings blackout
    # v1 default: no new entries near earnings. Config candidate: earnings_dte_blackout
    if pack.earnings_days_away is not None and pack.earnings_days_away <= 5:
        log.debug("[OPTS] _route_strategy %s: RULE1 earnings_blackout days=%s → []",
                  sym, pack.earnings_days_away)
        return []

    # Rule 2: no long premium in extreme IV
    # v1 default: Config candidate: iv_env_blackout_list
    if pack.iv_environment == "very_expensive":
        log.debug("[OPTS] _route_strategy %s: RULE2 iv_env=very_expensive → []", sym)
        return []

    # Rule 3: liquidity floor
    # v1 default: Config candidate: min_liquidity_score
    if pack.liquidity_score < 0.3:
        log.debug("[OPTS] _route_strategy %s: RULE3 liquidity=%.2f < 0.3 → []",
                  sym, pack.liquidity_score)
        return []

    # Rule 4: macro event + elevated IV
    # v1 default: Config candidate: macro_iv_gate
    if pack.macro_event_flag and pack.iv_rank > 60:
        log.debug("[OPTS] _route_strategy %s: RULE4 macro_event + iv_rank=%.1f > 60 → []",
                  sym, pack.iv_rank)
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


def _apply_veto_rules(candidate: dict, pack: A2FeaturePack, equity: float) -> Optional[str]:
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


def _build_options_candidates(
    watchlist_symbols: list[str],
    iv_summaries: dict,
    signal_scores: dict,
    vix: float,
    equity: float,
    config: dict | None = None,
) -> tuple[list[StructureProposal], dict[str, list[str]]]:
    """
    For each watchlist symbol with a signal score, run options_intelligence
    to get a candidate trade. Returns (candidates, allowed_structures_by_symbol).
    None returns (hold/skip) are filtered out.

    Pipeline per symbol:
      1. Universe tradeable gate (options_universe_manager.is_tradeable)
      2. Build A2FeaturePack and run deterministic strategy router
      3. Pre-debate liquidity pre-screen (loose thresholds = 50% of full gate)
      4. options_intelligence.select_options_strategy → StructureProposal
    """
    import options_universe_manager as _oum  # noqa: PLC0415
    from options_data import get_options_regime  # noqa: PLC0415

    if config is None:
        config = {}

    options_regime         = get_options_regime(vix)
    candidates: list[StructureProposal]  = []
    allowed_by_sym: dict[str, list[str]] = {}

    # Filter to symbols that have a meaningful signal.
    # score_signals() uses "conviction" field ("high"/"medium"/"low").
    scored_symbols = sorted(
        [(sym, sig) for sym, sig in signal_scores.items()
         if isinstance(sig, dict) and sig.get("conviction") in ("medium", "high")],
        key=lambda x: {"high": 2, "medium": 1, "low": 0}.get(x[1].get("conviction", "low"), 0),
        reverse=True
    )[:8]  # top 8 candidates only

    for sym, sig_data in scored_symbols:
        iv_summary = iv_summaries.get(sym, {
            "symbol": sym, "iv_environment": "unknown", "observation_mode": True
        })

        # Universe tradeable gate — replaces raw options_data.check_iv_history_ready()
        if not _oum.is_tradeable(sym):
            log.debug("[OPTS] %s: not in tradeable universe — queued for bootstrap", sym)
            continue

        # Determine tier: use dynamic if sig_data says dynamic, otherwise core
        tier          = sig_data.get("tier", "core")
        catalyst      = sig_data.get("primary_catalyst", "no specific catalyst")
        current_price = sig_data.get("price", 0)

        if current_price <= 0:
            log.debug("[OPTS] %s: no price data — skipping", sym)
            continue

        # Fetch chain once — used by both liquidity check and A2FeaturePack
        chain: dict = {}
        try:
            chain = options_data.fetch_options_chain(sym) or {}
        except Exception as _ce:
            log.debug("[OPTS] %s: chain fetch failed (non-fatal): %s", sym, _ce)

        # Build A2FeaturePack (additive — existing code paths unaffected if None)
        pack = _build_a2_feature_pack(
            symbol=sym,
            signal_scores=signal_scores,
            iv_summaries=iv_summaries,
            equity=equity,
            vix=vix,
            chain=chain or None,
        )

        # Deterministic strategy router — gates debate before AI sees this candidate
        if pack is not None:
            allowed = _route_strategy(pack)
            if not allowed:
                log.debug("[OPTS] %s: routing gate blocked — no allowed structures", sym)
                continue
            allowed_by_sym[sym] = allowed

            # A2-2: generate fully-specified structures + apply veto rules
            if chain:
                try:
                    from options_intelligence import generate_candidate_structures as _gen  # noqa
                    _cand_structs = _gen(pack=pack, allowed_structures=allowed,
                                        equity=equity, chain=chain)
                    if _cand_structs is not None:
                        _surviving = [
                            c for c in _cand_structs
                            if _apply_veto_rules(c, pack, equity) is None
                        ]
                        _vetoed = len(_cand_structs) - len(_surviving)
                        if _vetoed:
                            log.debug("[OPTS] %s: %d/%d structures vetoed",
                                      sym, _vetoed, len(_cand_structs))
                        if not _surviving:
                            if _cand_structs:
                                _sample_reason = _apply_veto_rules(_cand_structs[0], pack, equity)
                                log.info("[OPTS] %s: all %d structures vetoed (%s) — skipping",
                                         sym, len(_cand_structs), _sample_reason)
                            else:
                                log.info("[OPTS] %s: no structures generated — skipping", sym)
                            continue
                except Exception as _veto_exc:
                    log.debug("[OPTS] %s: veto pass failed (non-fatal): %s", sym, _veto_exc)

        proposal = options_intelligence.select_options_strategy(
            symbol=sym,
            iv_summary=iv_summary,
            signal_data=sig_data,
            vix=vix,
            tier=tier,
            catalyst=catalyst,
            current_price=current_price,
            equity=equity,
            options_regime=options_regime,
        )
        if proposal is None:
            continue

        # Pre-debate liquidity pre-screen (loose thresholds — 50% of full gate)
        if chain:
            try:
                liq_ok, liq_reason = _quick_liquidity_check(chain, proposal, config)
                if not liq_ok:
                    log.debug("[OPTS] %s: pre-debate liquidity fail — %s", sym, liq_reason)
                    continue
            except Exception as _liq_err:
                log.debug("[OPTS] %s: pre-debate liquidity check error (passing): %s", sym, _liq_err)

        candidates.append(proposal)

    return candidates, allowed_by_sym


# ── Claude four-way debate ─────────────────────────────────────────────────────

_OPTS_SYSTEM = None  # cached system prompt


def _load_opts_system() -> str:
    global _OPTS_SYSTEM
    if _OPTS_SYSTEM is None:
        path = PROMPTS_DIR / "system_options_v1.txt"
        _OPTS_SYSTEM = path.read_text().strip()
    return _OPTS_SYSTEM


def run_options_debate(
    candidates: list[StructureProposal],
    iv_summaries: dict,
    vix: float,
    regime: str,
    account1_summary: str,
    obs_mode: bool,
    equity: float,
    allowed_structures_by_symbol: dict | None = None,
) -> dict:
    """
    Submit candidates to Claude for the four-way options debate.
    Claude conducts Bull/Bear/IV Analyst/Synthesis internally and returns
    the final approved actions.

    Returns parsed JSON response dict.
    """
    system_prompt = _load_opts_system()
    claude = _get_claude()

    # Format candidates for prompt
    cands_text = json.dumps([asdict(c) for c in candidates], indent=2, default=str) if candidates else "[]"

    # Format IV environment summary
    iv_lines = []
    for sym, iv in iv_summaries.items():
        env = iv.get("iv_environment", "unknown")
        rank = iv.get("iv_rank")
        days = iv.get("history_days", 0)
        obs = " [OBS]" if iv.get("observation_mode") else ""
        rank_str = f"{rank:.0f}" if rank is not None else "N/A"
        iv_lines.append(f"  {sym}: env={env} rank={rank_str} history={days}d{obs}")
    iv_section = "\n".join(iv_lines) if iv_lines else "  (no IV data)"

    obs_notice = (
        "\n⚠ OBSERVATION MODE ACTIVE: Conduct full analysis but trades will NOT be submitted. "
        "Output your best trade decisions as if live — they are used for IV calibration.\n"
        if obs_mode else ""
    )

    # Format pre-approved structure types per symbol (from deterministic routing gate)
    allowed_section = ""
    if allowed_structures_by_symbol:
        allowed_lines = [
            f"  {sym}: {allowed}"
            for sym, allowed in allowed_structures_by_symbol.items()
        ]
        allowed_section = (
            "\n=== ALLOWED STRUCTURES (pre-approved by routing gate) ===\n"
            + "\n".join(allowed_lines)
            + "\nYou MUST only recommend structure types listed above for each symbol.\n"
        )

    user_content = f"""{obs_notice}
=== MARKET CONTEXT ===
VIX: {vix:.2f}
Regime: {regime}
Account 2 Equity: ${equity:,.0f}

=== ACCOUNT 1 AWARENESS ===
{account1_summary}

=== IV ENVIRONMENT SUMMARY ===
{iv_section}

=== CANDIDATE TRADES (from signal scoring) ===
{cands_text}
{allowed_section}
=== YOUR TASK ===
For each candidate, conduct the four-way debate:
1. BULL AGENT: strongest bull case with specific catalyst
2. BEAR AGENT: strongest bear case and key risks
3. IV ANALYST: IV rank assessment and recommended strategy
4. SYNTHESIS: PROCEED | VETO | RESIZE | RESTRUCTURE

Output your top 1-3 approved trades (or all HOLDs if no setup qualifies).
Minimum confidence 0.85 for any PROCEED. Apply all hard rules from system prompt.
Respond ONLY with valid JSON. No markdown. No explanation outside JSON fields.
"""

    try:
        resp = claude.messages.create(
            model=MODEL,
            max_tokens=2000,
            system=[{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_content}],
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )

        raw = resp.content[0].text.strip() if resp.content else ""
        _log_claude_cost(resp, "debate")

        if not raw:
            log.warning("[OPTS] Claude returned empty response")
            return {"regime": regime, "actions": [], "reasoning": "empty response"}

        # JSON parse with repair
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            last_brace = raw.rfind("}")
            if last_brace >= 0:
                try:
                    return json.loads(raw[:last_brace + 1])
                except json.JSONDecodeError:
                    pass
            log.warning("[OPTS] JSON parse failed, raw=%s", raw[:200])
            return {"regime": regime, "actions": [], "reasoning": "json_parse_failed"}

    except Exception as exc:
        log.error("[OPTS] Claude debate failed: %s", exc)
        return {"regime": regime, "actions": [], "reasoning": f"error: {exc}"}


# ── Cost tracking ─────────────────────────────────────────────────────────────

def _log_claude_cost(resp, call_type: str = "unknown"):
    """Log Claude API usage to Account 2 cost log."""
    try:
        usage = resp.usage
        entry = {
            "timestamp": __import__("datetime").datetime.now(ET).isoformat(),
            "call_type": call_type,
            "model": MODEL,
            "input_tokens": getattr(usage, "input_tokens", 0),
            "output_tokens": getattr(usage, "output_tokens", 0),
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0),
            "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0),
        }
        with open(_COST_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# ── Decision logging ──────────────────────────────────────────────────────────

def _save_decision(cycle_result: dict):
    """Append cycle decision to Account 2 decision log."""
    try:
        from datetime import datetime
        cycle_result["timestamp"] = datetime.now(ET).isoformat()
        history = []
        if _DECISION_LOG.exists():
            try:
                history = json.loads(_DECISION_LOG.read_text())
                if not isinstance(history, list):
                    history = [history]
            except Exception:
                history = []

        history.append(cycle_result)
        # Keep last 500 decisions
        if len(history) > 500:
            history = history[-500:]
        _DECISION_LOG.write_text(json.dumps(history, indent=2))
    except Exception as exc:
        log.debug("[OPTS] Decision log write failed: %s", exc)


# ── Position management ───────────────────────────────────────────────────────

def _get_open_options_positions(alpaca_client: TradingClient) -> list:
    """Get open options positions from Account 2."""
    try:
        positions = alpaca_client.get_all_positions()
        # Options positions have symbols like AAPL230120C00150000
        opts = [p for p in positions if len(getattr(p, "symbol", "")) > 10
                and any(c in getattr(p, "symbol", "") for c in ("C", "P"))]
        return opts
    except Exception as exc:
        log.warning("[OPTS] Could not fetch Account 2 positions: %s", exc)
        return []


def _check_expiring_positions(positions: list, alpaca_client: TradingClient) -> list[str]:
    """
    Check for options positions expiring within 5 days.
    Returns list of symbols that should be reviewed for close.
    """
    from datetime import date
    warn_symbols = []
    today = date.today()

    for pos in positions:
        sym = getattr(pos, "symbol", "")
        if len(sym) < 15:
            continue
        try:
            # OCC format: AAPL230120C00150000 — extract YYMMDD (positions 4-10 from root)
            # Find the first digit after the underlying letters
            i = 0
            while i < len(sym) and not sym[i].isdigit():
                i += 1
            if i >= len(sym):
                continue
            date_str = sym[i:i+6]  # YYMMDD
            exp_date = date(2000 + int(date_str[:2]), int(date_str[2:4]), int(date_str[4:6]))
            dte = (exp_date - today).days
            if dte <= 5:
                log.warning("[OPTS] %s: expires in %d days — consider closing", sym, dte)
                warn_symbols.append(sym)
        except Exception:
            continue
    return warn_symbols


# ── Signal score loading ──────────────────────────────────────────────────────

def _load_signal_scores_from_account1() -> dict:
    """
    Load Account 1's signal scores from the most recent score file.
    Falls back to empty dict if not available.
    """
    try:
        # Account 1 may have written signal scores to data/market/signal_scores.json
        path = Path(__file__).parent / "data" / "market" / "signal_scores.json"
        if path.exists() and (time.time() - path.stat().st_mtime) < 600:  # fresh < 10 min
            data = json.loads(path.read_text())
            # score_signals() returns {"scored_symbols": {...}, "top_3": [...], ...}
            # Extract the flat symbol→score dict from the nested structure.
            return data.get("scored_symbols", data) if isinstance(data, dict) else {}
    except Exception as exc:
        log.debug("[OPTS] Could not load Account 1 signal scores: %s", exc)
    return {}


def _get_core_equity_symbols() -> list[str]:
    """
    Get equity (non-crypto) symbols from core watchlist.
    Options only trade on equities.
    """
    try:
        import watchlist_manager as wm
        wl = wm.get_active_watchlist()
        return wl.get("stocks", []) + wl.get("etfs", [])
    except Exception as exc:
        log.debug("[OPTS] Could not load watchlist: %s", exc)
        # Fallback core list
        return [
            "SPY", "QQQ", "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL",
            "TSM", "AMD", "XLE", "GLD", "TLT", "IWM", "XLF", "XBI",
        ]


# ── A2 broker snapshot ────────────────────────────────────────────────────────

def _build_a2_broker_snapshot(alpaca_client: TradingClient) -> BrokerSnapshot:
    """
    Build a BrokerSnapshot from Account 2's current live state.

    Fetches positions and open orders from the A2 Alpaca account.
    Returns a BrokerSnapshot with normalised positions and orders.
    Non-fatal — returns an empty snapshot on any error so reconciliation
    can degrade gracefully rather than blocking the cycle.
    """
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus

    norm_positions: list[NormalizedPosition] = []
    norm_orders: list[NormalizedOrder] = []
    equity = buying_power = cash = 0.0

    try:
        account = alpaca_client.get_account()
        equity       = float(account.equity)
        cash         = float(account.cash)
        buying_power = float(account.buying_power)
    except Exception as exc:
        log.warning("[OPTS_RECON] snapshot: failed to fetch account: %s", exc)

    try:
        positions = alpaca_client.get_all_positions()
        norm_positions = [NormalizedPosition.from_alpaca_position(p) for p in positions]
    except Exception as exc:
        log.warning("[OPTS_RECON] snapshot: failed to fetch positions: %s", exc)

    try:
        orders = alpaca_client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN)
        )
        norm_orders = [NormalizedOrder.from_alpaca_order(o) for o in orders]
    except Exception as exc:
        log.warning("[OPTS_RECON] snapshot: failed to fetch orders: %s", exc)

    return BrokerSnapshot(
        positions=norm_positions,
        open_orders=norm_orders,
        equity=equity,
        cash=cash,
        buying_power=buying_power,
    )


# ── Main cycle ────────────────────────────────────────────────────────────────

def run_options_cycle(
    session_tier: str = "market",
    next_cycle_time: str = "?",
) -> None:
    """
    Run one Account 2 options cycle.

    1. Check Account 2 equity and equity floor
    2. Load Account 1 context
    3. Build IV summaries for equity watchlist
    4. Build candidate trades (pre-screened by IV + signal)
    5. Run Claude four-way debate
    6. Execute approved trades via order_executor_options
    7. Log decisions and costs
    """
    _ensure_dirs()
    t_start = time.monotonic()
    log.info("── [OPTS] Cycle start  session=%s ─────────────────────────", session_tier)

    # Skip non-market sessions for options (no overnight options)
    if session_tier not in ("market", "pre_market"):
        log.info("[OPTS] Session=%s — options cycle skipped (market hours only)", session_tier)
        return

    # 1. Account 2 status
    try:
        alpaca_client = _get_alpaca()
        account = alpaca_client.get_account()
        equity = float(account.equity)
        cash   = float(account.cash)
        log.info("[OPTS] Account 2: equity=$%s  cash=$%s", f"{equity:,.0f}", f"{cash:,.0f}")
    except Exception as exc:
        log.error("[OPTS] Cannot fetch Account 2 status: %s — skipping cycle", exc)
        return

    if equity < _EQUITY_FLOOR:
        log.warning("[OPTS] Account 2 equity $%.0f below floor $%.0f — halting", equity, _EQUITY_FLOOR)
        return

    # 1b. Preflight gate
    _pf_allow_live_orders = True
    _pf_allow_new_entries = True
    try:
        _pf_result = _preflight.run_preflight(
            caller="run_options_cycle",
            session_tier=session_tier,
            equity=equity,
            account_id="a2",
        )
        if _pf_result.verdict == "halt":
            log.error("[PREFLIGHT] verdict=halt — aborting options cycle  blockers=%s",
                      _pf_result.blockers)
            return
        elif _pf_result.verdict == "reconcile_only":
            log.warning("[PREFLIGHT] verdict=reconcile_only — new A2 entries blocked  blockers=%s",
                        _pf_result.blockers)
            _pf_allow_new_entries = False
        elif _pf_result.verdict == "shadow_only":
            log.warning("[PREFLIGHT] verdict=shadow_only — A2 live orders suppressed")
            _pf_allow_live_orders = False
        elif _pf_result.verdict == "go_degraded":
            log.warning("[PREFLIGHT] verdict=go_degraded  warnings=%s", _pf_result.warnings)
    except Exception as _pf_exc:
        log.error("[PREFLIGHT] unexpected exception (proceeding with caution): %s", _pf_exc)

    # 2. Observation mode check
    obs_state = _get_obs_mode_state()
    obs_mode = _update_obs_mode_state(obs_state)

    # Load A2 operating mode (non-fatal)
    _a2_mode = None
    try:
        from divergence import load_account_mode, OperatingMode  # noqa: PLC0415
        _a2_mode = load_account_mode("A2")
        if _a2_mode.mode != OperatingMode.NORMAL:
            log.warning("[DIV] A2 mode=%s scope=%s/%s",
                        _a2_mode.mode.value,
                        _a2_mode.scope.value,
                        _a2_mode.scope_id)
    except Exception as _div_a2_exc:
        log.warning("[DIV] A2 mode load failed (non-fatal): %s", _div_a2_exc)

    # 3. Open positions — check for expiring contracts
    open_positions = _get_open_options_positions(alpaca_client)
    expiring = _check_expiring_positions(open_positions, alpaca_client)
    if expiring:
        log.warning("[OPTS] %d positions expiring within 5 days: %s", len(expiring), expiring)

    # Stage 0 — Options structure reconciliation
    # Runs before new proposals so any broken/expiring structures are repaired first.
    _open_structs_for_recon = options_state.get_open_structures()
    if _open_structs_for_recon:
        try:
            _recon_snapshot = _build_a2_broker_snapshot(alpaca_client)
            _struct_diff = reconcile_options_structures(
                structures=_open_structs_for_recon,
                snapshot=_recon_snapshot,
                current_time=datetime.now(timezone.utc).isoformat(),
                config={},
            )
            if any([
                _struct_diff.broken,
                _struct_diff.expiring_soon,
                _struct_diff.needs_close,
                _struct_diff.orphaned_legs,
            ]):
                _repair_plan = plan_structure_repair(
                    diff=_struct_diff,
                    structures=_open_structs_for_recon,
                    snapshot=_recon_snapshot,
                    config={},
                )
                log.info(
                    "[OPTS_RECON] %d broken, %d expiring, "
                    "%d needs_close, %d orphaned — %d repair action(s)",
                    len(_struct_diff.broken),
                    len(_struct_diff.expiring_soon),
                    len(_struct_diff.needs_close),
                    len(_struct_diff.orphaned_legs),
                    len(_repair_plan),
                )
                execute_reconciliation_plan(
                    plan=_repair_plan,
                    trading_client=alpaca_client,
                    account_id="account2",
                    dry_run=False,
                )
            else:
                log.debug("[OPTS_RECON] %d open structures — all intact",
                          len(_open_structs_for_recon))
        except Exception as _recon_err:
            log.warning("[OPTS_RECON] Failed (non-fatal): %s", _recon_err)
    else:
        log.debug("[OPTS_RECON] No open structures — skipping reconciliation")

    # 4. VIX and regime from Account 1 signal context
    vix = 20.0  # default
    regime = "normal"
    try:
        import market_data
        # Quick VIX fetch (reuses Account 1's cache)
        vix_cache = Path(__file__).parent / "data" / "market" / "vix_cache.json"
        if vix_cache.exists() and (time.time() - vix_cache.stat().st_mtime) < 600:
            vix_data = json.loads(vix_cache.read_text())
            vix = float(vix_data.get("vix", vix))
    except Exception:
        pass

    # Check Account 1's last regime classification
    a1_decision = _load_account1_last_decision()
    if isinstance(a1_decision, dict):
        regime = a1_decision.get("regime", regime)

    account1_summary = _summarize_account1_for_prompt(a1_decision)

    # 5. Get equity symbols for options trading
    equity_symbols = _get_core_equity_symbols()

    # 6. Load signal scores from Account 1 (fresh within 10 min)
    signal_scores = _load_signal_scores_from_account1()

    # If no Account 1 signal scores, we can't proceed meaningfully
    if not signal_scores:
        log.info("[OPTS] No signal scores from Account 1 — skipping debate (no catalyst context)")
        _save_decision({
            "reasoning": "no_account1_signals",
            "regime": regime,
            "actions": [],
            "observation_mode": obs_mode,
        })
        return

    # 7. IV summaries for scored symbols
    scored_syms = list(signal_scores.keys())[:20]  # cap at 20
    iv_summaries = _get_iv_summaries_for_symbols(scored_syms)

    obs_count = sum(1 for iv in iv_summaries.values() if iv.get("observation_mode", True))
    log.info("[OPTS] IV summaries: %d symbols, %d in obs mode", len(iv_summaries), obs_count)

    # 7b. Initialize universe from existing IV history on first run
    try:
        import options_universe_manager as _oum  # noqa: PLC0415
        _universe_path = Path(__file__).parent / "data" / "options" / "universe.json"
        if not _universe_path.exists():
            log.info("[OPTS] universe.json absent — initializing from IV history")
            _oum.initialize_universe_from_existing_iv_history()
    except Exception as _uni_exc:
        log.debug("[OPTS] universe init failed (non-fatal): %s", _uni_exc)

    # 8. Build candidate trades
    options_regime = options_data.get_options_regime(vix)
    candidates, allowed_by_sym = _build_options_candidates(
        watchlist_symbols=equity_symbols,
        iv_summaries=iv_summaries,
        signal_scores=signal_scores,
        vix=vix,
        equity=equity,
        config=_load_strategy_config(),
    )

    log.info("[OPTS] Candidates: %d  VIX=%.1f  regime=%s  options_regime=%s",
             len(candidates), vix, regime, options_regime.get("regime", "?"))

    if not candidates and not obs_mode:
        log.info("[OPTS] No candidates after filtering — holding")
        _save_decision({
            "reasoning": "no_candidates_after_filter",
            "regime": regime,
            "actions": [],
            "observation_mode": obs_mode,
        })
        return

    # 9. Claude four-way debate
    debate_result = run_options_debate(
        candidates=candidates,
        iv_summaries=iv_summaries,
        vix=vix,
        regime=regime,
        account1_summary=account1_summary,
        obs_mode=obs_mode,
        equity=equity,
        allowed_structures_by_symbol=allowed_by_sym or None,
    )

    log.info("[OPTS] Debate complete: regime=%s  actions=%d",
             debate_result.get("regime", "?"),
             len(debate_result.get("actions", [])))

    # 10. Execute approved trades
    execution_results = []
    if not _pf_allow_new_entries:
        log.warning("[PREFLIGHT] New A2 entries suppressed by preflight (reconcile_only)")
    for action in debate_result.get("actions", []) if _pf_allow_new_entries else []:
        if action.get("action") == "hold":
            log.info("[OPTS] HOLD %s — %s",
                     action.get("symbol", "?"), action.get("reason", ""))
            # B6: record hold decisions so obs-mode cycles are traceable
            execution_results.append({
                "action": "hold",
                "symbol": action.get("symbol", ""),
                "status": "hold",
                "reason": action.get("reason", ""),
                "observation_mode": obs_mode,
            })
            continue

        sym = action.get("symbol", "")
        if not sym:
            continue

        # Look up original proposal so build_structure gets real chain-resolution params
        proposal = next((c for c in candidates if c.symbol == sym), None)
        if proposal is None:
            log.warning("[OPTS] %s: no matching proposal found in candidates", sym)
            continue

        # Mode gate — block new entries if A2 mode is not NORMAL
        if _a2_mode is not None:
            try:
                from divergence import is_action_allowed  # noqa: PLC0415
                _a2_allowed, _a2_reason = is_action_allowed(
                    _a2_mode, "enter_long", action.get("symbol", "")
                )
                if not _a2_allowed:
                    log.warning("[DIV] A2 BLOCKED %s — %s",
                                action.get("symbol", ""), _a2_reason)
                    continue
            except Exception as _div_gate_exc:
                log.debug("[DIV] A2 mode gate failed (non-fatal): %s", _div_gate_exc)

        # Build fully-specified OptionsStructure from live chain data
        try:
            chain = options_data.fetch_options_chain(sym)
            structure, build_err = options_builder.build_structure(
                symbol=proposal.symbol,
                strategy=proposal.strategy,
                direction=proposal.direction,
                conviction=proposal.conviction,
                iv_rank=proposal.iv_rank,
                max_cost_usd=action.get("max_cost_usd", proposal.max_cost_usd),
                chain=chain,
                equity=equity,
                config={},
            )
        except Exception as exc:
            log.error("[OPTS] %s: chain/build failed: %s", sym, exc)
            execution_results.append({
                "action": "error", "symbol": sym,
                "status": "error", "reason": str(exc),
            })
            continue

        if structure is None:
            log.warning("[OPTS] %s: build_structure rejected — %s", sym, build_err)
            execution_results.append({
                "action": "rejected", "symbol": sym,
                "status": "rejected", "reason": build_err or "build_failed",
            })
            continue

        # Persist proposed structure before submission (lifecycle=PROPOSED)
        options_state.save_structure(structure)

        # Submit — options_executor updates lifecycle and re-persists
        # shadow_only: treat as observation regardless of obs_mode flag
        _effective_obs = obs_mode or (not _pf_allow_live_orders)
        if not _pf_allow_live_orders:
            log.warning("[PREFLIGHT] shadow_only — suppressing live A2 submission for %s", sym)
        result = oe_opts.submit_options_order(structure, equity, _effective_obs)
        execution_results.append(result.to_dict())
        log.info("[OPTS] %s %s  status=%s%s",
                 sym, structure.strategy.value, result.status,
                 f"  structure_id={result.structure_id}" if result.structure_id else "")

    # 11. Save decision
    _decision_id_a2 = ""
    try:
        from attribution import generate_decision_id  # noqa: PLC0415
        _decision_id_a2 = generate_decision_id(
            "A2", datetime.now(ET).strftime("%Y%m%d_%H%M%S")
        )
    except Exception as _did_exc:
        log.debug("[OPTS] generate_decision_id failed (non-fatal): %s", _did_exc)

    cycle_record = {
        "decision_id": _decision_id_a2,
        "reasoning": debate_result.get("reasoning", ""),
        "regime": debate_result.get("regime", regime),
        "observation_mode": obs_mode,
        "account1_awareness": debate_result.get("account1_awareness", ""),
        "actions": debate_result.get("actions", []),
        "execution_results": execution_results,
        "notes": debate_result.get("notes", ""),
        "vix": vix,
        "equity": equity,
        "candidates_evaluated": len(candidates),
    }
    _save_decision(cycle_record)

    # Attribution — log order_submitted events for successfully submitted structures
    try:
        from attribution import log_attribution_event  # noqa: PLC0415
        _a2_tags = {"debate_layer": True, "risk_kernel": True, "sonnet_full": True}
        _a2_flags: dict = {}
        for _er in execution_results:
            if _er.get("status") in ("submitted", "observation") and _er.get("structure_id"):
                log_attribution_event(
                    event_type="order_submitted",
                    decision_id=_decision_id_a2,
                    account="A2",
                    symbol=_er.get("underlying", ""),
                    module_tags=_a2_tags,
                    trigger_flags=_a2_flags,
                    structure_id=_er.get("structure_id"),
                )
    except Exception as _a2_attr_exc:
        log.debug("[OPTS] Attribution failed (non-fatal): %s", _a2_attr_exc)

    # 12. Close-check and roll evaluation for open structures
    try:
        _strategy_cfg = _load_strategy_config()
        open_structs  = options_state.get_open_structures()
        if open_structs:
            trading_client = _get_alpaca()
            for struct in open_structs:
                should_close, close_reason = options_executor.should_close_structure(
                    struct, current_prices={}, config=_strategy_cfg,
                    current_time=None,
                )
                if should_close:
                    # Check for roll opportunity before plain close
                    should_roll, roll_reason = options_executor.should_roll_structure(
                        struct, close_reason, _strategy_cfg
                    )
                    if should_roll:
                        log.info("[OPTS] Rolling %s (%s): %s",
                                 struct.underlying, struct.structure_id, roll_reason)
                        options_executor.execute_roll(
                            struct, trading_client, roll_reason, _strategy_cfg
                        )
                    else:
                        log.info("[OPTS] Closing %s (%s): %s",
                                 struct.underlying, struct.structure_id, close_reason)
                        options_executor.close_structure(
                            struct, trading_client, reason=close_reason, method="limit"
                        )
    except Exception as exc:
        log.warning("[OPTS] Close-check loop error: %s", exc)

    elapsed = time.monotonic() - t_start
    log.info("── [OPTS] Cycle done  %.1fs  obs=%s  executed=%d ─────────────",
             elapsed, obs_mode, len(execution_results))


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    session = sys.argv[1] if len(sys.argv) > 1 else "market"
    run_options_cycle(session_tier=session)
