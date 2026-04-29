"""
portfolio_intelligence.py — Portfolio-level analytics for the trading bot.

Four upgrade modules:
  1. compute_dynamic_sizes()        — percentage-based tier sizing from live equity
  2. compute_position_health()      — per-position drawdown + health classification
     get_forced_exits()             — list CRITICAL positions needing half-exit
  3. compute_portfolio_correlation() — 30-day rolling correlation matrix + effective bets
  4. score_position_thesis()        — heuristic thesis strength score 1-10 (no Claude call)

Integration note (constraint 7):
  All functions live here to avoid conflicts with bot.py/order_executor.py/market_data.py,
  which are being edited by a parallel session. bot.py imports this module via a single
  added line; run_cycle() integration is done by that session when it finishes.

REALLOCATE action:
  execute_reallocate() handles exit + entry atomically. Import and wire into
  order_executor.execute_all() once the parallel session completes.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────

_ROOT         = Path(__file__).parent
_BARS_DIR     = _ROOT / "data" / "bars"
_SECTOR_PERF  = _ROOT / "data" / "market" / "sector_perf.json"
_CORE_WL      = _ROOT / "watchlist_core.json"

# ── Sector → representative ETF mapping (for sector alignment check) ──────────

_SECTOR_ETFS: dict[str, str] = {
    "technology":    "XLK",
    "energy":        "XLE",
    "financials":    "XLF",
    "health":        "XLV",
    "consumer":      "XLY",
    "consumer_disc": "XLY",
    "consumer_stap": "XLP",
    "defense":       "ITA",
    "biotech":       "XBI",
    "commodities":   "GLD",
    "international": "EEM",
    "macro":         "SPY",
    "industrials":   "XLI",
    "utilities":     "XLU",
    "materials":     "XLB",
    "real_estate":   "XLRE",
    "crypto":        "BITO",
}

# ── Core watchlist sector map (symbol → sector) ────────────────────────────────

def _build_symbol_sector_map() -> dict[str, str]:
    try:
        data = json.loads(_CORE_WL.read_text())
        return {entry["symbol"]: entry.get("sector", "") for entry in data.get("symbols", [])}
    except Exception:
        return {}

_SYMBOL_SECTOR: dict[str, str] = _build_symbol_sector_map()


# ─────────────────────────────────────────────────────────────────────────────
# UPGRADE 1 — Dynamic position sizing
# ─────────────────────────────────────────────────────────────────────────────

def compute_dynamic_sizes(
    equity: float,
    config: dict,
    current_exposure_dollars: float = 0.0,
    buying_power: float = 0.0,
) -> dict:
    """
    Compute actual dollar limits from current equity and configured percentages.

    Reads from config["position_sizing"] — percentages set in strategy_config.json.
    Called every cycle with fresh equity from Alpaca.

    Returns dict of tier_name → max_dollars (and derived fields for the prompt).

    Authority: RECOMMENDATION — produces analytics for prompt injection.
      Never places orders. Caller decides whether to act.
    """
    sizing = config.get("position_sizing", {})
    core_pct        = float(sizing.get("core_tier_pct",         0.15))
    standard_pct    = float(sizing.get("standard_tier_pct",     0.08))
    speculative_pct = float(sizing.get("speculative_tier_pct",  0.05))
    dyn_pct         = float(sizing.get("dynamic_tier_pct",      0.08))
    max_exp_pct     = float(sizing.get("max_total_exposure_pct", 0.30))
    cash_reserve_pct = float(sizing.get("cash_reserve_pct",     0.10))

    # Conviction-tiered sizing basis (mirrors risk_kernel._compute_sizing_basis)
    params      = config.get("parameters", {})
    margin_ok   = bool(params.get("margin_authorized", False))
    mult        = float(params.get("margin_sizing_multiplier", 1.0))
    _bp         = max(buying_power, equity)   # safety floor: never below equity
    sizing_basis_high = min(_bp, equity * mult)            if margin_ok else equity
    sizing_basis_med  = min(_bp, equity * min(mult, 1.5))  if margin_ok else equity
    sizing_basis_low  = equity

    max_exposure    = equity * max_exp_pct
    available       = max(0.0, buying_power)
    _total_cap      = current_exposure_dollars + buying_power
    exposure_pct    = round(current_exposure_dollars / _total_cap * 100, 1) if _total_cap > 0 else 0.0

    cap_high   = round(sizing_basis_high, 2)
    cap_medium = round(sizing_basis_med, 2)
    cap_low    = round(sizing_basis_low, 2)
    margin_available = round(max(0.0, buying_power - equity), 2)

    # Per-tier dollar maxes at each conviction level (HIGH core uses 20% bump)
    _core_pct_high = 0.20
    core_high      = round(sizing_basis_high * _core_pct_high, 2)
    core_med       = round(sizing_basis_med  * core_pct,        2)
    core_low       = round(sizing_basis_low  * core_pct,        2)
    dynamic_high   = round(sizing_basis_high * dyn_pct,         2)
    dynamic_med    = round(sizing_basis_med  * dyn_pct,         2)

    # Excess cash detection: actual uninvested > reserve + 10%
    actual_cash_pct  = max(0.0, 1.0 - (current_exposure_dollars / equity)) if equity > 0 else 1.0
    excess_cash_dollars = round(
        max(0.0, (actual_cash_pct - cash_reserve_pct - 0.10) * equity), 2
    )

    return {
        "core":               round(equity * core_pct,        2),
        "standard":           round(equity * standard_pct,    2),
        "speculative":        round(equity * speculative_pct, 2),
        "max_exposure":       round(max_exposure,             2),
        "cash_reserve":       round(equity * cash_reserve_pct, 2),
        "available_for_new":  round(available,                2),
        "current_exposure":   round(current_exposure_dollars, 2),
        "exposure_pct":       exposure_pct,
        "buying_power":       round(buying_power, 2),
        "margin_available":   margin_available,
        "cap_high":           cap_high,
        "cap_medium":         cap_medium,
        "cap_low":            cap_low,
        "core_high":          core_high,
        "core_med":           core_med,
        "core_low":           core_low,
        "dynamic_high":       dynamic_high,
        "dynamic_med":        dynamic_med,
        "margin_authorized":  margin_ok,
        "margin_multiplier":  mult,
        "excess_cash_dollars": excess_cash_dollars,
    }


def format_dynamic_sizes_section(sizes: dict, equity: float) -> str:
    """Format the === DYNAMIC POSITION SIZES === prompt block.

    Authority: PRESENTATION — formats analytics as prompt text only.
      No enforcement authority.
    """
    lines = [
        "=== POSITION SIZING — conviction-tiered (margin authorized) ===",
        f"Current equity: ${equity:,.2f}   Buying power: ${sizes.get('buying_power', 0.0):,.0f}",
        "",
        "Core tier:",
        f"  HIGH conviction:   up to ${sizes.get('core_high', sizes['core']):,.0f}  "
        f"(20% of ${sizes.get('cap_high', equity):,.0f} sizing basis)",
        f"  MEDIUM conviction: up to ${sizes.get('core_med', sizes['core']):,.0f}  "
        f"(15% of ${sizes.get('cap_medium', equity):,.0f} sizing basis)",
        f"  LOW conviction:    up to ${sizes.get('core_low', sizes['core']):,.0f}  "
        "(15% of equity, no margin)",
        "Dynamic tier:",
        f"  HIGH:   up to ${sizes.get('dynamic_high', sizes['standard']):,.0f}",
        f"  MEDIUM: up to ${sizes.get('dynamic_med', sizes['standard']):,.0f}",
        "",
        f"Cash reserve floor: ${sizes['cash_reserve']:,.0f}",
        f"Available for new positions: ${sizes['available_for_new']:,.0f}",
        f"Current exposure: ${sizes['current_exposure']:,.0f} ({sizes['exposure_pct']}% of total capacity)",
    ]
    bp = sizes.get("buying_power", 0.0)
    if bp > equity:
        actual_mult = round(bp / equity, 1) if equity > 0 else 1
        cfg_mult = sizes.get("margin_multiplier", 1.0)
        lines.append(
            f"Margin: {actual_mult}x available; sizing basis = "
            f"min(buying_power, equity × {cfg_mult:.1f}x) for HIGH conviction."
        )
        lines.append(
            "Express HIGH conviction when signals align strongly — the kernel "
            "will size with margin behind it. Do not underreport conviction "
            "to stay within a smaller mental cap."
        )
    excess = sizes.get("excess_cash_dollars", 0.0)
    if excess > 1000:
        lines.append(
            f"[EXCESS_CASH] ${excess:,.0f} above reserve floor — "
            f"deploy into high conviction setups"
        )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# UPGRADE 2 — Per-position drawdown tracking
# ─────────────────────────────────────────────────────────────────────────────

def compute_position_health(position, equity: float) -> dict:
    """
    For an open Alpaca position, compute health metrics and classify status.

    Health classifications:
      CRITICAL   — drawdown > 12%, OR drawdown > 8% with position > 10% of account
      WARNING    — drawdown > 6%
      HEALTHY    — unrealized P&L positive
      MONITORING — unrealized P&L <= 0 but no warning threshold crossed

    Returns dict suitable for prompt injection and forced-exit logic.

    Authority: RECOMMENDATION — produces analytics for prompt injection.
      Never places orders. Caller decides whether to act.
    """
    entry_price   = float(position.avg_entry_price)
    current_price = float(position.current_price)
    market_value  = float(position.market_value)
    unrealized_pl = float(position.unrealized_pl)

    drawdown_pct = ((entry_price - current_price) / entry_price * 100) if entry_price > 0 else 0.0
    account_pct  = (market_value / equity * 100) if equity > 0 else 0.0

    if drawdown_pct > 12 or (drawdown_pct > 8 and account_pct > 10):
        health = "CRITICAL"
    elif drawdown_pct > 6:
        health = "WARNING"
    elif unrealized_pl > 0:
        health = "HEALTHY"
    else:
        health = "MONITORING"

    return {
        "symbol":           position.symbol,
        "drawdown_pct":     round(drawdown_pct, 2),
        "drawdown_dollars": round(unrealized_pl, 2),
        "account_pct":      round(account_pct, 2),
        "health":           health,
        "unrealized_pl":    round(unrealized_pl, 2),
    }


def get_forced_exits(positions: list, equity: float) -> list[dict]:
    """
    Return list of CRITICAL positions that require a forced half-exit.

    Each entry has: symbol, health dict, half_qty (shares to sell), full_qty.
    Called from run_cycle() before Claude is invoked so forced exits can be
    processed as hard rules independent of Claude's decision.

    Authority: RECOMMENDATION — returns candidate exit list based on position
      health heuristics.
      WARNING: reconciliation.py currently consumes this output authoritatively
      to generate forced close actions. This is a known temporary overlap
      documented in docs/policy_ownership_map.md. Target state: risk_kernel.py
      owns forced exit authority; PI remains advisory input only.
    """
    forced = []
    for pos in positions:
        if float(pos.qty) <= 0:
            continue
        health = compute_position_health(pos, equity)
        if health["health"] == "CRITICAL":
            full_qty = int(float(pos.qty))
            half_qty = max(1, full_qty // 2)
            forced.append({
                "symbol":   pos.symbol,
                "health":   health,
                "half_qty": half_qty,
                "full_qty": full_qty,
            })
    return forced


def get_deadline_exits(strategy_config: dict, positions: list) -> list[dict]:
    """
    Return list of positions whose time_bound_action deadline has passed.
    Iterates ALL time_bound_actions — every held position with an expired
    deadline is returned. Called from run_cycle() before Claude is invoked.

    Each entry has: symbol, reason, deadline_et, deadline_utc, full_qty.

    Authority: RECOMMENDATION — returns candidate deadline exit list based on
      strategy_config time_bound_actions.
      WARNING: reconciliation.py currently consumes this output authoritatively
      to generate deadline_exit_market actions. This is a known temporary overlap
      documented in docs/policy_ownership_map.md. Target state: risk_kernel.py
      owns forced exit authority; PI remains advisory input only.
    """
    if not strategy_config or not positions:
        return []
    tba = strategy_config.get("time_bound_actions", [])
    if not tba:
        return []

    now  = datetime.now(timezone.utc)
    held = {pos.symbol: pos for pos in positions if float(pos.qty) > 0}

    expired = []
    for item in tba:
        sym = item.get("symbol", "")
        if not sym or sym not in held:
            continue
        dl_str = item.get("deadline_utc", "")
        if not dl_str:
            continue
        try:
            dl_dt = datetime.fromisoformat(dl_str.replace("Z", "+00:00"))
            if now >= dl_dt:
                pos = held[sym]
                expired.append({
                    "symbol":       sym,
                    "reason":       item.get("reason", ""),
                    "deadline_et":  item.get("deadline_et", ""),
                    "deadline_utc": dl_str,
                    "full_qty":     int(float(pos.qty)),
                })
        except (ValueError, TypeError):
            continue
    return expired


def format_positions_with_health(
    positions: list,
    equity: float,
    buying_power: float = 0.0,
) -> str:
    """
    Format === OPEN POSITIONS === section with per-position health data.
    Replaces the plain positions_table in build_user_prompt().

    Oversize bands use buying_power as denominator (aligns with risk kernel).
    Falls back to equity if buying_power is 0.

    Authority: PRESENTATION — formats analytics as prompt text only.
      No enforcement authority.
    """
    if not positions:
        return "  (none)"

    bp = buying_power if buying_power > 0 else equity

    rows = []
    for p in positions:
        health = compute_position_health(p, equity)
        unreal = float(p.unrealized_pl)
        sign   = "+" if unreal >= 0 else ""
        pnl_pct = (unreal / (float(p.avg_entry_price) * float(p.qty)) * 100
                   if float(p.avg_entry_price) > 0 and float(p.qty) > 0 else 0.0)
        flag = ""
        if health["health"] == "CRITICAL":
            flag = "  *** CRITICAL DRAWDOWN — HALF POSITION FORCED EXIT ***"
        elif health["health"] == "WARNING":
            flag = "  !! WARNING: drawdown approaching stop threshold"

        bp_pct = float(p.market_value) / bp * 100 if bp > 0 else 0.0
        if bp_pct > 25.0:
            oversize_flag = (
                f"\n             !! OVERSIZE — {bp_pct:.1f}% of BP exceeds max tier ceiling 25%"
                f" — TRIM or close regardless of tier"
            )
        elif bp_pct > 15.0:
            oversize_flag = (
                f"\n             !! OVERSIZE — {bp_pct:.1f}% of BP exceeds standard core max 15%"
                f" — confirm HIGH conviction core or TRIM"
            )
        elif bp_pct > 8.0:
            oversize_flag = (
                f"\n             !! OVERSIZE for dynamic/intraday tier — {bp_pct:.1f}% of BP exceeds 8%"
                f" — TRIM or confirm core tier intended"
            )
        else:
            oversize_flag = ""

        rows.append(
            f"  {p.symbol:<9} qty={float(p.qty):>8.4f}  "
            f"entry=${float(p.avg_entry_price):>10.2f}  "
            f"current=${float(p.current_price):>10.2f}  "
            f"P&L={sign}${unreal:.2f} ({sign}{pnl_pct:.1f}%)\n"
            f"             account_pct={health['account_pct']:.1f}%  "
            f"drawdown={health['drawdown_pct']:.1f}%  health={health['health']}"
            + flag
            + oversize_flag
        )
    return "\n".join(rows)


# ─────────────────────────────────────────────────────────────────────────────
# UPGRADE 3 — Portfolio correlation awareness
# ─────────────────────────────────────────────────────────────────────────────

def compute_portfolio_correlation(
    open_symbols: list[str],
    new_symbol: Optional[str] = None,
) -> dict:
    """
    Compute 30-day rolling correlation between all open positions via yfinance.

    If new_symbol is provided, its correlations with existing positions are
    included in new_symbol_correlations (used to assess diversification value).

    Thresholds:
      > 0.70  HIGH     — flag as same macro bet
      0.50–0.70 MODERATE — note but allow
      < 0.50  LOW      — independent position

    Returns:
    {
      "matrix": {sym: {sym2: corr_float}},
      "high_correlation_pairs": [{symbols, correlation, warning}],
      "effective_bets": N,
      "new_symbol_correlations": {sym: corr_float}
    }

    Fails gracefully: returns empty structure on any error or insufficient data.

    Authority: RECOMMENDATION — produces analytics for prompt injection.
      Never places orders. Caller decides whether to act.
    """
    # Crypto tickers: Alpaca uses "/" (BTC/USD) or no separator (ETHUSD, BTCUSD)
    # yfinance requires dash format (BTC-USD, ETH-USD)
    _CRYPTO_YF_MAP = {
        "BTCUSD": "BTC-USD", "ETHUSD": "ETH-USD", "SOLUSD": "SOL-USD",
        "AVAXUSD": "AVAX-USD", "LINKUSD": "LINK-USD", "DOTUSD": "DOT-USD",
    }
    def _yf_ticker(sym: str) -> str:
        if sym in _CRYPTO_YF_MAP:
            return _CRYPTO_YF_MAP[sym]
        return sym.replace("/", "-") if "/" in sym else sym

    all_symbols = list(open_symbols)
    if new_symbol and new_symbol not in all_symbols:
        all_symbols.append(new_symbol)

    if len(all_symbols) < 2:
        return {
            "matrix": {},
            "high_correlation_pairs": [],
            "effective_bets": len(open_symbols),
            "new_symbol_correlations": {},
        }

    yf_symbols = [_yf_ticker(s) for s in all_symbols]

    try:
        import pandas as pd
        import yfinance as yf

        # Download ~45 calendar days to get ~30 trading days
        raw = yf.download(yf_symbols, period="45d", auto_adjust=True, progress=False)
        if raw is None or raw.empty:
            raise ValueError("yfinance returned empty data")

        # Extract Close prices (handles both single and multi-ticker responses)
        if hasattr(raw.columns, "levels"):
            closes = raw["Close"] if "Close" in raw.columns.get_level_values(0) else raw
        else:
            closes = raw["Close"] if "Close" in raw.columns else raw

        # Single-ticker download returns a Series — wrap it
        if isinstance(closes, pd.Series):
            closes = closes.to_frame(name=yf_symbols[0])

        # Remap yfinance tickers back to original symbols
        rename_map = {yf: orig for orig, yf in zip(all_symbols, yf_symbols)}
        closes.rename(columns=rename_map, inplace=True)

        available = [s for s in all_symbols if s in closes.columns]
        if len(available) < 2:
            raise ValueError(f"insufficient symbols available: {available}")

        closes = closes[available].dropna()
        if len(closes) < 20:
            raise ValueError(f"only {len(closes)} trading days — need 20+")

        returns    = closes.pct_change().dropna()
        corr_df    = returns.corr()

    except Exception as exc:
        log.warning("[PI] Correlation matrix failed (%s) — returning empty", exc)
        return {
            "matrix": {},
            "high_correlation_pairs": [],
            "effective_bets": len(open_symbols),
            "new_symbol_correlations": {},
        }

    # Build matrix (open positions only, not new_symbol)
    open_avail = [s for s in open_symbols if s in corr_df.columns]
    matrix: dict[str, dict[str, float]] = {}
    for s1 in open_avail:
        matrix[s1] = {}
        for s2 in open_avail:
            if s1 != s2 and s2 in corr_df.columns and s1 in corr_df.index:
                matrix[s1][s2] = round(float(corr_df.loc[s1, s2]), 2)

    # Identify correlation pairs
    high_pairs: list[dict] = []
    seen: set[tuple] = set()
    for s1 in open_avail:
        for s2 in open_avail:
            if s1 >= s2:
                continue
            key = (s1, s2)
            if key in seen:
                continue
            seen.add(key)
            corr = matrix.get(s1, {}).get(s2, 0.0)
            if abs(corr) > 0.70:
                high_pairs.append({
                    "symbols":     [s1, s2],
                    "correlation": corr,
                    "warning":     "Effectively same macro bet",
                })
            elif abs(corr) > 0.50:
                high_pairs.append({
                    "symbols":     [s1, s2],
                    "correlation": corr,
                    "warning":     "Moderate correlation — note but allow",
                })

    # Effective bets: greedy grouping of corr > 0.70 pairs
    groups:   list[set[str]] = []
    assigned: set[str] = set()
    for s in open_avail:
        if s in assigned:
            continue
        group = {s}
        for s2 in open_avail:
            if s2 not in assigned and s2 != s:
                corr = abs(matrix.get(s, {}).get(s2, 0.0))
                if corr > 0.70:
                    group.add(s2)
        groups.append(group)
        assigned |= group

    effective_bets = len(groups)

    # New symbol correlations
    new_corrs: dict[str, float] = {}
    if new_symbol and new_symbol in corr_df.columns and new_symbol in corr_df.index:
        for s in open_avail:
            if s in corr_df.columns:
                new_corrs[s] = round(float(corr_df.loc[new_symbol, s]), 2)

    return {
        "matrix":                  matrix,
        "high_correlation_pairs":  high_pairs,
        "effective_bets":          effective_bets,
        "new_symbol_correlations": new_corrs,
    }


def format_correlation_section(corr: dict, open_symbols: list[str]) -> str:
    """Format the === PORTFOLIO CORRELATION === prompt block.

    Authority: PRESENTATION — formats analytics as prompt text only.
      No enforcement authority.
    """
    if len(open_symbols) < 2:
        return "=== PORTFOLIO CORRELATION ===\nN/A — fewer than 2 positions"

    lines = ["=== PORTFOLIO CORRELATION ==="]
    lines.append(f"Effective independent bets: {corr['effective_bets']} "
                 f"(not {len(open_symbols)})" if corr['effective_bets'] < len(open_symbols)
                 else f"Effective independent bets: {corr['effective_bets']}")

    if corr["high_correlation_pairs"]:
        for pair in corr["high_correlation_pairs"]:
            s1, s2 = pair["symbols"]
            c      = pair["correlation"]
            level  = "HIGH" if abs(c) > 0.70 else "MODERATE"
            prefix = "⚠" if abs(c) > 0.70 else "~"
            lines.append(f"{prefix} {s1} / {s2}: {c:.2f} correlation "
                         f"({level} — {pair['warning']})")
    else:
        lines.append("No high-correlation pairs detected.")

    if not corr["matrix"]:
        lines.append("(Insufficient data for full matrix — < 20 trading days)")

    lines.append("\nWhen evaluating new entries: if new position correlates > 0.70 "
                 "with existing position, require explicit justification. "
                 "You are not adding diversification.")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# UPGRADE 4 — Portfolio thesis ranking
# ─────────────────────────────────────────────────────────────────────────────

def _load_bars(symbol: str) -> "Optional[pd.DataFrame]":  # noqa: F821
    """Load cached daily bars for a symbol. Returns None if unavailable."""
    try:
        import pandas as pd
        path = _BARS_DIR / f"{symbol}_daily.csv"
        if not path.exists():
            return None
        df = pd.read_csv(path, parse_dates=["date"])
        df = df.sort_values("date").tail(30)
        if len(df) < 5:
            return None
        return df
    except Exception:
        return None


def _load_sector_perf() -> dict:
    """Load latest sector performance snapshot."""
    try:
        return json.loads(_SECTOR_PERF.read_text()).get("sectors", {})
    except Exception:
        return {}


def _get_symbol_sector(symbol: str) -> str:
    """Look up sector for a symbol from core watchlist."""
    return _SYMBOL_SECTOR.get(symbol, "")


def score_position_thesis(
    symbol: str,
    position,
    original_decision: dict,
    current_md: dict,
    days_held: int,
    strategy_config: dict = None,
) -> dict:
    """
    Score the thesis of an open position 1-10 using pure Python heuristics.
    No Claude API call — runs every cycle for every position with zero latency.

    Scoring factors (each adds/subtracts from a base of 8):
      Catalyst recency  — catalyst age in days
      Technical         — price vs MA20, EMA9 from cached bars
      P&L momentum      — trending toward target or stop
      Sector alignment  — sector ETF performance today
      Time decay        — catalyst type specific decay

    Returns:
    {
      symbol, thesis_score, catalyst_age_days, technical_intact,
      trending_toward, sector_aligned, weakest_factor,
      recommended_action: "hold" | "reduce" | "exit_consider"
    }

    Authority: RECOMMENDATION — produces analytics for prompt injection.
      Never places orders. Caller decides whether to act.
    """
    score         = 8
    weaknesses: list[str] = []

    entry_price   = float(position.avg_entry_price)
    current_price = float(position.current_price)
    unrealized_pl = float(position.unrealized_pl)
    catalyst      = original_decision.get("catalyst", "")
    stop_loss_raw = original_decision.get("stop_loss")
    take_profit_raw = original_decision.get("take_profit")

    # ── 1. Catalyst recency ────────────────────────────────────────────────
    if days_held <= 2:
        catalyst_adjustment = 0
    elif days_held <= 5:
        catalyst_adjustment = -1
        weaknesses.append("catalyst aging")
    else:
        catalyst_adjustment = -2
        weaknesses.append("catalyst stale (5+ days)")
    score += catalyst_adjustment

    # ── 2. Technical structure (from bars cache) ───────────────────────────
    bars           = _load_bars(symbol)
    technical_intact = False
    above_ma20    = False
    above_ema9    = False

    if bars is not None and not bars.empty and "close" in bars.columns:
        closes      = bars["close"]
        ma20        = closes.rolling(20).mean().iloc[-1] if len(closes) >= 20 else None
        ema9        = closes.ewm(span=9, adjust=False).mean().iloc[-1]
        above_ma20  = bool(current_price > ma20) if ma20 is not None else True
        above_ema9  = bool(current_price > ema9)

        if above_ma20 and above_ema9:
            score += 2
            technical_intact = True
        elif above_ma20 or above_ema9:
            score += 1
            technical_intact = True
        else:
            score -= 1
            weaknesses.append("below MA20 and EMA9")
    else:
        # No bars data — neutral, small penalty for uncertainty
        score -= 0
        technical_intact = True  # assume intact when we can't verify

    # ── 3. P&L momentum ───────────────────────────────────────────────────
    trending_toward = "unknown"
    if stop_loss_raw is not None and take_profit_raw is not None:
        stop_price   = float(stop_loss_raw)
        target_price = float(take_profit_raw)
        target_price - stop_price if target_price > stop_price else 1.0
        pct_to_target = (current_price - entry_price) / (target_price - entry_price) if target_price != entry_price else 0
        if pct_to_target > 0.1:
            trending_toward = "target"
            score += 2
        elif unrealized_pl < 0 and current_price < entry_price:
            dist_to_stop    = current_price - stop_price
            dist_entry_stop = entry_price   - stop_price
            pct_to_stop     = 1 - (dist_to_stop / dist_entry_stop) if dist_entry_stop > 0 else 0
            if pct_to_stop > 0.5:
                trending_toward = "stop"
                score -= 2
                weaknesses.append("trending toward stop")
            else:
                trending_toward = "stop"
                score -= 1
                weaknesses.append("approaching stop")
        else:
            trending_toward = "flat"
    else:
        # No stop/target in decision — judge by unrealized P&L
        if unrealized_pl > 0:
            trending_toward = "target"
            score += 1
        elif unrealized_pl < -(entry_price * float(position.qty) * 0.03):
            trending_toward = "stop"
            score -= 2
            weaknesses.append("significant unrealized loss")
        else:
            trending_toward = "flat"

    # ── 4. Sector alignment ────────────────────────────────────────────────
    sector_perf   = _load_sector_perf()
    sector        = _get_symbol_sector(symbol)
    sector_aligned = False

    if sector and sector_perf:
        # Normalize sector key (core watchlist uses "consumer", perf uses "consumer_disc")
        sector_key = sector
        if sector == "consumer":
            sector_key = "consumer_disc"
        sector_data = sector_perf.get(sector_key, sector_perf.get(sector, {}))
        if sector_data:
            day_chg = float(sector_data.get("day_chg", 0))
            if day_chg > 0:
                score += 1
                sector_aligned = True
            else:
                score -= 1
                weaknesses.append(f"sector {sector} down {day_chg:.1f}% today")
    # If no sector data available, neutral

    # ── 5. Time decay on catalyst type ────────────────────────────────────
    catalyst_lower = catalyst.lower() if catalyst else ""
    if "earnings" in catalyst_lower and days_held > 2:
        score -= 1
        weaknesses.append("earnings catalyst decays fast")
    elif "insider" in catalyst_lower and days_held > 7:
        score -= 1
        weaknesses.append("insider signal value fading")

    # ── 6. Time-bound action override ─────────────────────────────────────────
    override_flag = None
    if strategy_config:
        _now = datetime.now(timezone.utc)
        for _item in strategy_config.get("time_bound_actions", []):
            if _item.get("symbol") != symbol:
                continue
            _dl = _item.get("deadline_utc", "")
            if _dl:
                try:
                    _dl_dt = datetime.fromisoformat(_dl.replace("Z", "+00:00"))
                    if _now >= _dl_dt:
                        override_flag = "THESIS EXPIRED"
                        weaknesses.insert(0, "time-bound deadline passed")
                except (ValueError, TypeError):
                    pass
            if override_flag is None:
                _tp = _item.get("target_price")
                if _tp is not None:
                    try:
                        if current_price >= float(_tp):
                            override_flag = "TARGET HIT / EXCEEDED"
                            weaknesses.insert(
                                0, f"target ${float(_tp):.2f} reached")
                    except (ValueError, TypeError):
                        pass
            if override_flag:
                break

    # ── Clamp and classify ─────────────────────────────────────────────────
    score = max(1, min(10, score))

    if override_flag:
        score              = min(score, 4)
        recommended_action = "exit_consider"
    elif score <= 3:
        recommended_action = "exit_consider"
    elif score <= 5:
        recommended_action = "reduce"
    else:
        recommended_action = "hold"

    weakest_factor = weaknesses[0] if weaknesses else "none"

    # T-023: thesis_status derived from score and override_flag
    if override_flag or score < 4:
        thesis_status = "invalidated"
    elif score <= 6:
        thesis_status = "weakening"
    else:
        thesis_status = "valid"

    if thesis_status == "invalidated":
        log.warning("[PI] THESIS INVALIDATED: %s — %s", symbol, override_flag or weakest_factor)

    return {
        "symbol":              symbol,
        "thesis_score":        score,
        "thesis_status":       thesis_status,
        "catalyst_age_days":   days_held,
        "technical_intact":    technical_intact,
        "above_ma20":          above_ma20,
        "above_ema9":          above_ema9,
        "trending_toward":     trending_toward,
        "sector_aligned":      sector_aligned,
        "weakest_factor":      weakest_factor,
        "recommended_action":  recommended_action,
        "override_flag":       override_flag,
        "catalyst":            catalyst,
    }


def format_thesis_ranking_section(
    thesis_scores: list[dict],
    weakest_symbol: Optional[str] = None,
) -> str:
    """Format the === PORTFOLIO THESIS RANKING === prompt block.

    Authority: PRESENTATION — formats analytics as prompt text only.
      No enforcement authority.
    """
    if not thesis_scores:
        return "=== PORTFOLIO THESIS RANKING ===\nN/A — no open positions"

    lines = ["=== PORTFOLIO THESIS RANKING ===",
             "Positions ranked by thesis strength:", ""]

    sorted_scores = sorted(thesis_scores, key=lambda x: x["thesis_score"], reverse=True)
    for i, ts in enumerate(sorted_scores, 1):
        score  = ts["thesis_score"]
        sym    = ts["symbol"]
        action = ts["recommended_action"]
        label  = "STRONG" if score >= 7 else ("MODERATE" if score >= 5 else "WEAK — consider exit")

        thesis_status = ts.get("thesis_status", "valid")
        status_tag = {"valid": "✓ VALID", "weakening": "⚠ WEAKENING", "invalidated": "✗ INVALIDATED"}.get(thesis_status, thesis_status)
        lines.append(f"{i}. {sym}  score: {score}/10  [{label}]  thesis: {status_tag}")
        if ts.get("override_flag"):
            lines.append(
                f"   *** {ts['override_flag']} — original thesis no longer valid ***")

        if ts.get("catalyst_consumed"):
            consumed_at = (ts.get("catalyst_consumed_at") or "")[:10]
            tags = ts.get("thesis_tags", [])
            forward = " / ".join(tags) if tags else "re-evaluate forward thesis: revenue trend / guidance / next catalyst"
            catalyst_line = f"   Catalyst: CONSUMED (earnings {consumed_at}) — {forward}"
        else:
            catalyst_line = f"   Catalyst: {ts['catalyst']}" if ts["catalyst"] else "   Catalyst: (none recorded)"
            age = ts["catalyst_age_days"]
            catalyst_line += f" ({age}d old) {'✓' if age <= 2 else '⚠' if age <= 5 else '✗'}"
        lines.append(catalyst_line)

        tech_symbol = "✓" if ts["technical_intact"] else "✗"
        ma_note     = "above MA20" if ts.get("above_ma20") else "below MA20"
        ema_note    = "above EMA9" if ts.get("above_ema9")  else "below EMA9"
        lines.append(f"   Technical: {ma_note}, {ema_note} {tech_symbol}")

        trend_symbol = "✓" if ts["trending_toward"] == "target" else ("—" if ts["trending_toward"] == "flat" else "✗")
        lines.append(f"   Trending: toward {ts['trending_toward']} {trend_symbol}")

        sector_symbol = "✓" if ts["sector_aligned"] else "✗"
        lines.append(f"   Sector: {'aligned' if ts['sector_aligned'] else 'misaligned'} {sector_symbol}")

        action_map = {
            "hold":           "HOLD",
            "reduce":         "REDUCE — thesis weakening",
            "exit_consider":  "EXIT CONSIDER — weakest thesis",
        }
        lines.append(f"   Action: {action_map.get(action, action)}")
        eda    = ts.get("earnings_days_away")
        timing = ts.get("earnings_timing")
        if eda is not None and eda == 0 and timing == "pre-market":
            lines.append("   *** [CATALYST CONSUMED — re-evaluate fresh (pre-market)] ***")
        elif eda is not None and eda == 0:
            # post-market or unknown: event hasn't happened yet from a trading perspective
            lines.append("   ⚠ [EARNINGS TODAY — binary event exposure rule applies]")
        elif eda is not None and eda == 1:
            lines.append("   ⚠ [EARNINGS TOMORROW — apply binary event exposure rule]")
        lines.append("")

    # Reallocation candidates — surface ALL positions scoring ≤4
    weak = [ts for ts in sorted_scores if ts["thesis_score"] <= 4]
    if len(weak) > 1:
        lines.append(
            "Capital reallocation — MULTIPLE weak positions (score ≤4):")
        for rc in weak:
            flag = f" [{rc['override_flag']}]" if rc.get("override_flag") else ""
            lines.append(
                f"  ► {rc['symbol']}{flag}  score={rc['thesis_score']}/10"
                f" — EXIT RECOMMENDED to free capital"
            )
    elif len(weak) == 1:
        rc   = weak[0]
        flag = f" [{rc['override_flag']}]" if rc.get("override_flag") else ""
        lines += [
            "Capital reallocation note:",
            f"If a new HIGH conviction setup appears and exposure is near cap, "
            f"exit this position to fund the new entry. "
            f"Reallocation candidate: {rc['symbol']}{flag} "
            f"(score {rc['thesis_score']}/10).",
        ]
    elif sorted_scores[-1]["thesis_score"] <= 5:
        worst = sorted_scores[-1]
        lines += [
            "Capital reallocation note:",
            f"If a new HIGH conviction setup appears and exposure is near cap, "
            f"consider exiting lowest-scored position to fund the new entry. "
            f"Current reallocation candidate: {worst['symbol']} "
            f"(score {worst['thesis_score']}/10).",
        ]

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# REALLOCATE action — atomic exit + entry
# ─────────────────────────────────────────────────────────────────────────────

def execute_reallocate(
    exit_symbol: str,
    entry_action: dict,
    alpaca_client,
) -> dict:
    """
    Execute a capital reallocation: sell exit_symbol then buy entry_action["symbol"].
    Atomic: if the exit fails, the entry is NOT submitted.

    Returns:
    {
      "status": "submitted" | "exit_failed" | "entry_failed",
      "exit_order_id": str | None,
      "entry_order_id": str | None,
      "reason": str,
    }

    Authority: ENFORCEMENT_ADJACENT — executes broker orders directly
      (close_position + submit_order). Currently DEAD CODE — not wired into
      bot.py. If activated, must route through risk_kernel.py first.
      Never call directly from run_cycle().
    """
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    log.info("[PI] REALLOCATE: exit %s → enter %s", exit_symbol, entry_action.get("symbol"))

    # Step 1 — close exit position
    try:
        exit_order   = alpaca_client.close_position(exit_symbol)
        exit_order_id = str(exit_order.id)
        log.info("[PI] REALLOCATE exit submitted: %s  order_id=%s",
                 exit_symbol, exit_order_id)
    except Exception as exc:
        log.error("[PI] REALLOCATE exit FAILED for %s: %s — entry cancelled", exit_symbol, exc)
        return {
            "status":        "exit_failed",
            "exit_order_id":  None,
            "entry_order_id": None,
            "reason":        str(exc),
        }

    # Step 2 — submit entry (only if exit succeeded)
    try:
        from alpaca.trading.enums import OrderClass
        from alpaca.trading.requests import (
            MarketOrderRequest,
            StopLossRequest,
            TakeProfitRequest,
        )

        entry_sym = entry_action["symbol"]
        qty       = int(float(entry_action["qty"]))
        stop      = float(entry_action["stop_loss"])
        target    = float(entry_action["take_profit"])
        tp        = TakeProfitRequest(limit_price=round(target, 2))
        sl        = StopLossRequest(stop_price=round(stop, 2))

        req = MarketOrderRequest(
            symbol=entry_sym, qty=qty, side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY, order_class=OrderClass.BRACKET,
            take_profit=tp, stop_loss=sl,
        )
        entry_order    = alpaca_client.submit_order(req)
        entry_order_id = str(entry_order.id)
        log.info("[PI] REALLOCATE entry submitted: %s  order_id=%s",
                 entry_sym, entry_order_id)

        return {
            "status":        "submitted",
            "exit_order_id":  exit_order_id,
            "entry_order_id": entry_order_id,
            "reason":        f"exited {exit_symbol}, entered {entry_sym}",
        }

    except Exception as exc:
        log.error("[PI] REALLOCATE entry FAILED for %s: %s", entry_action.get("symbol"), exc)
        return {
            "status":        "entry_failed",
            "exit_order_id":  exit_order_id,
            "entry_order_id": None,
            "reason":        str(exc),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Convenience: full portfolio intelligence snapshot for a cycle
# ─────────────────────────────────────────────────────────────────────────────

def build_portfolio_intelligence(
    equity:                   float,
    positions:                list,
    config:                   dict,
    open_decisions:           dict | None = None,
    position_entry_dates:     dict | None = None,
    buying_power:             float = 0.0,
) -> dict:
    """
    Compute all 4 intelligence modules in one call. Cache-friendly — call once
    per cycle, pass the result to format_*_section() for prompt injection.

    Authority: ORCHESTRATION — aggregates all PI analytics into a single dict
      for prompt injection and reconciliation input. No direct enforcement
      authority; downstream consumers own enforcement.

    Args:
      equity               — current account equity (float)
      positions            — list of Alpaca position objects
      config               — parsed strategy_config.json dict
      open_decisions       — {symbol: original_decision_dict} (optional)
      position_entry_dates — {symbol: datetime} for days_held calc (optional)

    Returns dict with keys: sizes, health, forced_exits, correlation, thesis_scores
    """
    open_decisions       = open_decisions       or {}
    position_entry_dates = position_entry_dates or {}
    now                  = datetime.now(timezone.utc)

    # 1. Dynamic sizes
    long_exposure = sum(float(p.market_value) for p in positions if float(p.qty) > 0)
    sizes = compute_dynamic_sizes(equity, config, long_exposure, buying_power=buying_power)

    # 2. Per-position health
    health_map: dict[str, dict] = {}
    for pos in positions:
        if float(pos.qty) > 0:
            health_map[pos.symbol] = compute_position_health(pos, equity)

    forced_exits   = get_forced_exits(positions, equity)
    deadline_exits = get_deadline_exits(config, positions)

    # 3. Correlation (only if 2+ positions — avoid unnecessary yfinance calls)
    open_syms = [pos.symbol for pos in positions if float(pos.qty) > 0]
    if len(open_syms) >= 2:
        correlation = compute_portfolio_correlation(open_syms)
    else:
        correlation = {
            "matrix": {}, "high_correlation_pairs": [],
            "effective_bets": len(open_syms), "new_symbol_correlations": {},
        }

    # 4. Thesis scores
    _qual_sym_ctx: dict = {}
    try:
        from bot_stage1_5_qualitative import (
            load_qualitative_context as _load_qual,  # noqa: PLC0415
        )
        _qual_sym_ctx = _load_qual().get("symbol_context") or {}
    except Exception:
        pass

    thesis_scores: list[dict] = []
    for pos in positions:
        if float(pos.qty) <= 0:
            continue
        decision   = open_decisions.get(pos.symbol, {})
        entry_dt   = position_entry_dates.get(pos.symbol)
        days_held  = int((now - entry_dt).days) if entry_dt else 1
        ts = score_position_thesis(
            symbol=pos.symbol,
            position=pos,
            original_decision=decision,
            current_md={},
            days_held=days_held,
            strategy_config=config,
        )
        try:
            from earnings_calendar_lookup import (
                earnings_days_away as _eda,  # noqa: PLC0415
            )
            from earnings_calendar_lookup import (
                earnings_timing as _etiming,  # noqa: PLC0415
            )
            ts["earnings_days_away"] = _eda(pos.symbol)
            ts["earnings_timing"] = _etiming(pos.symbol)
        except Exception:
            ts["earnings_days_away"] = None
            ts["earnings_timing"] = None

        eda_val    = ts.get("earnings_days_away")
        eda_timing = ts.get("earnings_timing")
        consumed = (
            (eda_val is not None and eda_val < 0) or
            (eda_val == 0 and eda_timing == "pre-market")
        )
        if consumed:
            ts["catalyst_consumed"] = True
            ts["catalyst_consumed_at"] = datetime.now(timezone.utc).isoformat()
            ts["thesis_score"] = max(0, ts.get("thesis_score", 8) - 1)
        else:
            ts["catalyst_consumed"] = False
            ts["catalyst_consumed_at"] = None

        _qual_entry = _qual_sym_ctx.get(pos.symbol) or {}
        if _qual_entry:
            ts["thesis_tags"] = _qual_entry.get("thesis_tags") or []

        thesis_scores.append(ts)

    return {
        "sizes":          sizes,
        "health_map":     health_map,
        "forced_exits":   forced_exits,
        "deadline_exits": deadline_exits,
        "correlation":    correlation,
        "thesis_scores":  thesis_scores,
    }
