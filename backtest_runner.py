"""
backtest_runner.py — strategy backtesting harness.

Replays 90 days of cached daily bars through 5 strategy variants, lets Claude
make trading decisions on historical snapshots, simulates fills, and uses a
Strategy Director Claude call to pick the winner and update strategy_config.json.

Usage:
    python backtest_runner.py                        # all 5 strategies, 30 days
    python backtest_runner.py --strategy momentum    # single strategy
    python backtest_runner.py --days 60              # longer window
"""

import argparse
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
from dotenv import load_dotenv

import anthropic
from log_setup import get_logger
from watchlist_manager import get_core

load_dotenv()

log = get_logger(__name__)

BASE_DIR     = Path(__file__).parent
BARS_DIR     = BASE_DIR / "data" / "bars"
REPORTS_DIR  = BASE_DIR / "data" / "reports"
CONFIG_FILE  = BASE_DIR / "strategy_config.json"

claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# ── Strategy system prompts ───────────────────────────────────────────────────

STRATEGY_PROMPTS: dict[str, str] = {
    "momentum": (
        "You are a momentum trader focused on stocks exhibiting strong upward price velocity. "
        "Only buy when: price is above MA20, volume ratio is 3x or higher, and RSI is between 50 and 70. "
        "Prioritize clean breakouts above recent resistance with expanding volume. "
        "Avoid buying extended stocks (RSI > 70) or anything below its MA20. "
        "Target 6-8% gains; cut losses at 2.5% below entry. "
        "Respond with JSON: {\"actions\": [{\"action\": \"buy\"|\"sell\"|\"hold\", "
        "\"symbol\": \"SYM\", \"qty\": N, \"stop_loss\": P, \"take_profit\": P, "
        "\"tier\": \"core\"|\"dynamic\", \"rationale\": \"...\"}], \"rationale\": \"overall reasoning\"}. "
        "If no trade meets criteria, return {\"actions\": [], \"rationale\": \"no setup\"}."
    ),
    "mean_reversion": (
        "You are a mean reversion trader who profits from extremes snapping back to average. "
        "Buy when RSI is below 30 and price is more than 5% below MA20 (oversold). "
        "Short (action=sell) when RSI is above 70 and price is more than 5% above MA20 (overbought). "
        "Never fight a strong trend — check that the sector is not in a one-directional move. "
        "Set stop loss beyond the extreme; target the MA20 as take-profit. "
        "Respond with JSON: {\"actions\": [{\"action\": \"buy\"|\"sell\"|\"hold\", "
        "\"symbol\": \"SYM\", \"qty\": N, \"stop_loss\": P, \"take_profit\": P, "
        "\"tier\": \"core\"|\"dynamic\", \"rationale\": \"...\"}], \"rationale\": \"overall reasoning\"}. "
        "If no extreme is present, return {\"actions\": [], \"rationale\": \"no extreme\"}."
    ),
    "news_sentiment": (
        "You are a news-driven trader who acts only on named catalysts with clear directional impact. "
        "Ignore all technical indicators — trade exclusively on earnings surprises, FDA approvals, "
        "M&A announcements, macro policy shifts, or major geopolitical events mentioned in the data. "
        "Size positions conservatively given event risk; always define stop loss below key support. "
        "Respond with JSON: {\"actions\": [{\"action\": \"buy\"|\"sell\"|\"hold\", "
        "\"symbol\": \"SYM\", \"qty\": N, \"stop_loss\": P, \"take_profit\": P, "
        "\"tier\": \"core\"|\"dynamic\", \"rationale\": \"<catalyst name>\"}], \"rationale\": \"overall reasoning\"}. "
        "If no named catalyst exists, return {\"actions\": [], \"rationale\": \"no catalyst\"}."
    ),
    "cross_sector": (
        "You are a cross-sector correlation trader who exploits second-order butterfly effects between sectors. "
        "Look for signals like: oil spike → long defense (LMT/RTX), dollar strength → short EEM/FXI, "
        "gold rally → risk-off positioning, tech selloff → rotate into consumer staples. "
        "Base every trade on an inter-market signal chain, not just the target symbol's own price action. "
        "Respond with JSON: {\"actions\": [{\"action\": \"buy\"|\"sell\"|\"hold\", "
        "\"symbol\": \"SYM\", \"qty\": N, \"stop_loss\": P, \"take_profit\": P, "
        "\"tier\": \"core\"|\"dynamic\", \"rationale\": \"<signal chain>\"}], \"rationale\": \"overall reasoning\"}. "
        "If no cross-sector signal is present, return {\"actions\": [], \"rationale\": \"no signal\"}."
    ),
    "hybrid": (
        "You are a balanced trader who synthesizes momentum, mean reversion, news catalysts, and cross-sector signals. "
        "Weight signals by conviction: strong named catalyst > breakout with volume > oversold bounce > sector rotation. "
        "Require at least two confirming signals before entering a trade. "
        "Manage risk with tier-appropriate position sizing: core 15%, dynamic 8% of equity. "
        "Respond with JSON: {\"actions\": [{\"action\": \"buy\"|\"sell\"|\"hold\", "
        "\"symbol\": \"SYM\", \"qty\": N, \"stop_loss\": P, \"take_profit\": P, "
        "\"tier\": \"core\"|\"dynamic\", \"rationale\": \"...\"}], \"rationale\": \"overall reasoning\"}. "
        "If no high-conviction setup exists, return {\"actions\": [], \"rationale\": \"no setup\"}."
    ),
}

# ── Tier sizing constants (mirrors order_executor.py) ─────────────────────────

TIER_MAX_PCT: dict[str, float] = {
    "core":     0.15,
    "dynamic":  0.08,
    "intraday": 0.05,
}

MIN_BARS_REQUIRED  = 30   # skip symbols with fewer than this many bars
MA20_WINDOW        = 20
MA50_WINDOW        = 50
RSI_PERIOD         = 14
MAX_HOLD_DAYS      = 5    # close after this many bars if TP/SL not hit
EVERY_NTH_DAY      = 3    # simulate every Nth trading day to keep cost down


# ── Bar loading ───────────────────────────────────────────────────────────────

def _load_all_bars() -> dict[str, list[dict]]:
    """Load all cached bar CSVs from data/bars/. Returns {symbol: [row_dicts]} sorted by date asc."""
    all_bars: dict[str, list[dict]] = {}

    for csv_path in BARS_DIR.glob("*_daily.csv"):
        sym = csv_path.stem.replace("_daily", "").replace("_", "/")
        try:
            df = pd.read_csv(csv_path)
            # Normalize column names (lowercase)
            df.columns = [c.lower().strip() for c in df.columns]
            if "date" not in df.columns:
                log.debug("Skipping %s — no 'date' column", csv_path.name)
                continue
            # Cast numeric columns
            for col in ("open", "high", "low", "close", "volume"):
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.dropna(subset=["date", "close"])
            df = df.sort_values("date").reset_index(drop=True)
            rows = df.to_dict("records")
            if len(rows) < MIN_BARS_REQUIRED:
                log.debug("Skipping %s — only %d bars", sym, len(rows))
                continue
            all_bars[sym] = rows
            log.debug("Loaded %d bars for %s", len(rows), sym)
        except Exception as exc:
            log.debug("Failed to load %s: %s", csv_path.name, exc)

    log.info("Loaded bar data for %d symbols", len(all_bars))
    return all_bars


# ── Indicator helpers ─────────────────────────────────────────────────────────

def _rsi(closes: list[float], period: int = 14) -> float:
    """Simple RSI approximation over the last `period` bars."""
    if len(closes) < period + 1:
        return 50.0  # neutral fallback
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    window = deltas[-period:]
    gains  = [d for d in window if d > 0]
    losses = [abs(d) for d in window if d < 0]
    avg_gain = sum(gains) / period if gains else 0.0
    avg_loss = sum(losses) / period if losses else 0.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _ma(closes: list[float], window: int) -> Optional[float]:
    """Simple moving average. Returns None if not enough data."""
    if len(closes) < window:
        return None
    return sum(closes[-window:]) / window


def _compute_indicators(bars_up_to: list[dict]) -> dict:
    """Compute indicators from bars up to (not including) the simulation date."""
    if not bars_up_to:
        return {}
    closes  = [b["close"] for b in bars_up_to]
    volumes = [b.get("volume", 0) for b in bars_up_to]
    last    = bars_up_to[-1]
    prev    = bars_up_to[-2] if len(bars_up_to) >= 2 else last

    ma20 = _ma(closes, MA20_WINDOW)
    ma50 = _ma(closes, MA50_WINDOW)
    rsi  = _rsi(closes, RSI_PERIOD)

    vol20_mean = (sum(volumes[-MA20_WINDOW:]) / MA20_WINDOW) if len(volumes) >= MA20_WINDOW else None
    vol_ratio  = (last.get("volume", 0) / vol20_mean) if vol20_mean and vol20_mean > 0 else 1.0

    prev_close = prev["close"]
    day_chg    = ((last["close"] - prev_close) / prev_close * 100.0) if prev_close else 0.0

    pct_vs_ma20 = ((last["close"] - ma20) / ma20 * 100.0) if ma20 and ma20 > 0 else None

    return {
        "close":       last["close"],
        "open":        last.get("open", last["close"]),
        "high":        last.get("high", last["close"]),
        "low":         last.get("low",  last["close"]),
        "volume":      last.get("volume", 0),
        "ma20":        ma20,
        "ma50":        ma50,
        "rsi":         round(rsi, 1),
        "vol_ratio":   round(vol_ratio, 2),
        "day_chg":     round(day_chg, 2),
        "pct_vs_ma20": round(pct_vs_ma20, 2) if pct_vs_ma20 is not None else None,
    }


# ── Historical snapshot builder ───────────────────────────────────────────────

def _build_historical_snapshot(
    all_bars:     dict[str, list[dict]],
    date_str:     str,
    core_symbols: list[dict],
) -> dict:
    """
    For each core symbol, compute indicators from bars UP TO (not including) date_str.
    Returns a snapshot dict used to build the backtest prompt.
    """
    sym_indicators: dict[str, dict] = {}

    for sym_info in core_symbols:
        sym = sym_info["symbol"]
        if sym not in all_bars:
            continue
        bars     = all_bars[sym]
        hist     = [b for b in bars if str(b["date"]) < date_str]
        if len(hist) < MIN_BARS_REQUIRED:
            continue
        ind = _compute_indicators(hist)
        ind["sector"] = sym_info.get("sector", "unknown")
        ind["type"]   = sym_info.get("type",   "stock")
        ind["tier"]   = sym_info.get("tier",   "core")
        sym_indicators[sym] = ind

    # VIX proxy: use SPY day change to infer regime
    spy_ind = sym_indicators.get("SPY", {})
    spy_chg = spy_ind.get("day_chg", 0.0)
    vix_regime = "ELEVATED" if spy_chg < 0 else "NORMAL"

    # Sector table: avg day_chg per sector
    sector_day_chgs: dict[str, list[float]] = {}
    for sym, ind in sym_indicators.items():
        sec = ind.get("sector", "other")
        sector_day_chgs.setdefault(sec, []).append(ind.get("day_chg", 0.0))
    sector_table = {
        sec: round(sum(chgs) / len(chgs), 2)
        for sec, chgs in sector_day_chgs.items()
    }

    # Inter-market signals from GLD, TLT, XLE, EEM
    intermarket_signals: list[str] = []
    for sig_sym, threshold, bull_msg, bear_msg in [
        ("GLD",   0.5,  "Gold rising — risk-off, consider TLT/GLD long, dollar pressure",
                        "Gold falling — risk-on tilt, equities likely supported"),
        ("TLT",   0.5,  "Bonds rising (TLT up) — rate fears easing, growth stocks may rally",
                        "Bonds falling (TLT down) — rising rate environment, pressure on tech/growth"),
        ("XLE",   1.0,  "Energy sector strong — oil tailwind, consider defense/energy long",
                        "Energy sector weak — oil headwind, may compress consumer spending"),
        ("EEM",   0.5,  "Emerging markets rising — risk-on, dollar may be weakening",
                        "Emerging markets falling — dollar strength, reduce international exposure"),
    ]:
        chg = sym_indicators.get(sig_sym, {}).get("day_chg", 0.0)
        if chg >= threshold:
            intermarket_signals.append(bull_msg)
        elif chg <= -threshold:
            intermarket_signals.append(bear_msg)

    # Core by sector for prompt
    core_by_sector: dict[str, list[dict]] = {}
    for sym, ind in sym_indicators.items():
        sec = ind.get("sector", "other")
        core_by_sector.setdefault(sec, []).append({"symbol": sym, **ind})

    return {
        "date":               date_str,
        "vix_regime":         vix_regime,
        "spy_day_chg":        spy_chg,
        "sector_table":       sector_table,
        "intermarket_signals": intermarket_signals,
        "core_by_sector":     core_by_sector,
        "sym_indicators":     sym_indicators,
    }


# ── Backtest prompt builder ───────────────────────────────────────────────────

def _build_backtest_prompt(snapshot: dict, date_str: str, sim_equity: float) -> str:
    """Build a simplified historical prompt (no live news, no real PDT state)."""
    lines: list[str] = []

    lines.append(f"BACKTEST MODE — historical data as of {date_str}")
    lines.append(f"Simulated equity: ${sim_equity:,.2f}")
    lines.append("")

    lines.append("=== SESSION ===")
    lines.append("Session: MARKET (simulated regular hours)")
    lines.append(f"Date: {date_str}")
    lines.append("PDT: N/A (backtest)")
    lines.append("")

    lines.append("=== ACCOUNT STATE ===")
    lines.append(f"Equity:        ${sim_equity:,.2f}")
    lines.append(f"Cash:          ${sim_equity:,.2f}  (simulated, no open positions shown)")
    lines.append(f"Open positions: see simulation state")
    lines.append("")

    lines.append("=== MARKET REGIME ===")
    vix_regime = snapshot.get("vix_regime", "NORMAL")
    spy_chg    = snapshot.get("spy_day_chg", 0.0)
    lines.append(f"VIX Regime: {vix_regime}  (SPY prev-day chg: {spy_chg:+.2f}%)")
    if vix_regime == "ELEVATED":
        lines.append("Instruction: Elevated regime — reduce size, prefer defensive names.")
    else:
        lines.append("Instruction: Normal regime — full sizing allowed.")
    lines.append("")

    lines.append("=== SECTOR ROTATION TODAY ===")
    lines.append(f"{'Sector':<20} {'Day%':>7}")
    lines.append("-" * 30)
    for sec, chg in sorted(snapshot.get("sector_table", {}).items(), key=lambda x: -x[1]):
        lines.append(f"{sec:<20} {chg:>+7.2f}%")
    lines.append("")

    lines.append("=== INTER-MARKET SIGNALS ===")
    signals = snapshot.get("intermarket_signals", [])
    if signals:
        for sig in signals:
            lines.append(f"  - {sig}")
    else:
        lines.append("  No significant inter-market signals today.")
    lines.append("")

    lines.append("=== CORE WATCHLIST (by sector) ===")
    for sec, entries in sorted(snapshot.get("core_by_sector", {}).items()):
        lines.append(f"\n[{sec.upper()}]")
        lines.append(f"  {'Symbol':<10} {'Close':>8} {'Day%':>7} {'RSI':>6} {'Vol×':>6} "
                     f"{'vs MA20%':>9} {'MA20':>8} {'MA50':>8}")
        lines.append("  " + "-" * 70)
        for e in sorted(entries, key=lambda x: x["symbol"]):
            sym      = e["symbol"]
            close    = e.get("close",       0.0)
            day_chg  = e.get("day_chg",     0.0)
            rsi      = e.get("rsi",        50.0)
            vol_r    = e.get("vol_ratio",   1.0)
            vs_ma20  = e.get("pct_vs_ma20", None)
            ma20     = e.get("ma20",        None)
            ma50     = e.get("ma50",        None)
            vs_str   = f"{vs_ma20:+.1f}%" if vs_ma20 is not None else "  N/A"
            ma20_str = f"{ma20:.2f}"       if ma20  is not None else "   N/A"
            ma50_str = f"{ma50:.2f}"       if ma50  is not None else "   N/A"
            lines.append(
                f"  {sym:<10} {close:>8.2f} {day_chg:>+7.2f}% {rsi:>6.1f} "
                f"{vol_r:>6.2f}x {vs_str:>9} {ma20_str:>8} {ma50_str:>8}"
            )
    lines.append("")

    lines.append("=== RECENT DECISIONS (MEMORY) ===")
    lines.append("(No memory injected in backtest mode — decide from market data alone.)")
    lines.append("")

    lines.append("=== YOUR TASK ===")
    lines.append(
        "Analyze the historical market data above. Identify the best 1-3 trade setups "
        "that fit your strategy. For each trade, specify: action (buy/sell/hold), symbol, "
        "qty (integer shares), stop_loss price, take_profit price, tier (core or dynamic), "
        "and a brief rationale. Use position sizing of 15% equity for core, 8% for dynamic. "
        "Return ONLY a JSON object — no markdown, no explanation outside JSON. "
        'Format: {"actions": [...], "rationale": "overall reasoning"}'
    )

    return "\n".join(lines)


# ── Trade simulation ──────────────────────────────────────────────────────────

def _get_bars_from_date(all_bars: dict[str, list[dict]], symbol: str, from_date: str) -> list[dict]:
    """Return bars for symbol on or after from_date."""
    if symbol not in all_bars:
        return []
    return [b for b in all_bars[symbol] if str(b["date"]) >= from_date]


def _simulate_trade(
    symbol:     str,
    action:     dict,
    entry_date: str,
    all_bars:   dict[str, list[dict]],
) -> Optional[dict]:
    """
    Simulate a trade fill and outcome.

    Fill at open of the bar AFTER entry_date.
    Check stop_loss and take_profit against subsequent bars (up to MAX_HOLD_DAYS).
    Returns: {symbol, entry_price, exit_price, qty, pnl, outcome, hold_days}
    or None if the trade can't be simulated (missing bars, bad data).
    """
    future_bars = _get_bars_from_date(all_bars, symbol, entry_date)
    # Skip entry_date itself — we want the bar strictly after
    future_bars = [b for b in future_bars if str(b["date"]) > entry_date]

    if not future_bars:
        log.debug("No future bars for %s after %s — skipping trade", symbol, entry_date)
        return None

    entry_bar   = future_bars[0]
    entry_price = entry_bar.get("open") or entry_bar.get("close")
    if not entry_price or entry_price <= 0:
        return None

    stop_loss   = float(action.get("stop_loss",   entry_price * 0.97))
    take_profit = float(action.get("take_profit", entry_price * 1.06))
    qty         = int(action.get("qty", 1))
    is_short    = str(action.get("action", "buy")).lower() == "sell"

    hold_days   = 0
    exit_price  = entry_price
    outcome     = "neutral"

    for bar in future_bars[1: MAX_HOLD_DAYS + 1]:
        hold_days += 1
        bar_low  = bar.get("low",  bar["close"])
        bar_high = bar.get("high", bar["close"])

        if not is_short:
            # Long: hit stop if low <= stop_loss; hit TP if high >= take_profit
            if bar_low <= stop_loss:
                exit_price = stop_loss
                outcome    = "loss"
                break
            if bar_high >= take_profit:
                exit_price = take_profit
                outcome    = "win"
                break
        else:
            # Short: hit stop if high >= stop_loss; hit TP if low <= take_profit
            if bar_high >= stop_loss:
                exit_price = stop_loss
                outcome    = "loss"
                break
            if bar_low <= take_profit:
                exit_price = take_profit
                outcome    = "win"
                break
    else:
        # Held to expiry — close at last available bar's close
        last_bar   = future_bars[min(MAX_HOLD_DAYS, len(future_bars) - 1)]
        exit_price = last_bar["close"]
        hold_days  = min(MAX_HOLD_DAYS, len(future_bars))
        outcome    = "neutral"

    if not is_short:
        pnl = (exit_price - entry_price) * qty
    else:
        pnl = (entry_price - exit_price) * qty

    return {
        "symbol":      symbol,
        "action":      action.get("action", "buy"),
        "entry_price": round(entry_price, 4),
        "exit_price":  round(exit_price, 4),
        "stop_loss":   round(stop_loss,  4),
        "take_profit": round(take_profit, 4),
        "qty":         qty,
        "pnl":         round(pnl, 2),
        "outcome":     outcome,
        "hold_days":   hold_days,
        "entry_date":  entry_date,
    }


# ── Statistics ────────────────────────────────────────────────────────────────

def _compute_stats(equity_curve: list[float], trades: list[dict], strategy: str) -> dict:
    """Compute Sharpe, max drawdown, profit factor, win rate."""
    total_trades = len(trades)
    wins         = [t for t in trades if t.get("outcome") == "win"]
    losses       = [t for t in trades if t.get("outcome") == "loss"]

    win_rate = (len(wins) / total_trades * 100.0) if total_trades > 0 else 0.0

    wins_pnl   = sum(t["pnl"] for t in wins)
    losses_pnl = sum(t["pnl"] for t in losses)
    profit_factor = (
        round(wins_pnl / abs(losses_pnl), 3) if losses_pnl < 0 else
        (float("inf") if wins_pnl > 0 else 0.0)
    )

    # Sharpe ratio (annualised, daily returns)
    if len(equity_curve) >= 2:
        daily_returns = [
            (equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1]
            for i in range(1, len(equity_curve))
        ]
        mean_ret = sum(daily_returns) / len(daily_returns)
        variance = sum((r - mean_ret) ** 2 for r in daily_returns) / len(daily_returns)
        std_ret  = math.sqrt(variance) if variance > 0 else 0.0
        sharpe   = (mean_ret / std_ret * math.sqrt(252)) if std_ret > 0 else 0.0
    else:
        sharpe = 0.0

    # Max drawdown
    max_drawdown = 0.0
    peak = equity_curve[0] if equity_curve else 1.0
    for val in equity_curve:
        if val > peak:
            peak = val
        dd = (peak - val) / peak if peak > 0 else 0.0
        if dd > max_drawdown:
            max_drawdown = dd

    # Return %
    if equity_curve:
        return_pct = (equity_curve[-1] - equity_curve[0]) / equity_curve[0] * 100.0
    else:
        return_pct = 0.0

    return {
        "strategy":      strategy,
        "return_pct":    round(return_pct, 2),
        "win_rate":      round(win_rate, 1),
        "sharpe":        round(sharpe, 3),
        "max_drawdown":  round(max_drawdown * 100.0, 2),   # in percent
        "profit_factor": profit_factor,
        "total_trades":  total_trades,
        "wins":          len(wins),
        "losses":        len(losses),
        "equity_curve":  [round(e, 2) for e in equity_curve],
    }


# ── Strategy simulation loop ──────────────────────────────────────────────────

def _parse_claude_actions(text: str) -> Optional[list[dict]]:
    """Extract the actions list from Claude's JSON response. Returns None on failure."""
    # Strip markdown code fences if present
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text  = "\n".join(lines[1:])
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]

    try:
        data = json.loads(text)
        return data.get("actions", [])
    except json.JSONDecodeError:
        # Try to find JSON object within text
        start = text.find("{")
        end   = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start:end])
                return data.get("actions", [])
            except json.JSONDecodeError:
                pass
    return None


def _run_strategy(
    strategy_name:   str,
    system_prompt:   str,
    all_bars:        dict[str, list[dict]],
    core_symbols:    list[dict],
    trade_dates:     list[str],
    starting_equity: float = 30_000.0,
) -> dict:
    """
    Run one strategy over all trade_dates.
    Returns stats dict.
    """
    log.info("=" * 60)
    log.info("Starting backtest: strategy=%s  days=%d", strategy_name, len(trade_dates))

    equity_curve:   list[float] = [starting_equity]
    sim_equity:     float       = starting_equity
    all_trades:     list[dict]  = []
    open_positions: dict[str, dict] = {}  # {symbol: {entry_price, qty, stop_loss, take_profit, entry_date}}

    total_days = len(trade_dates)

    for i, date_str in enumerate(trade_dates, 1):
        log.info("Backtesting %s: day %d/%d  date=%s", strategy_name, i, total_days, date_str)

        # Print progress every 5 days
        if i % 5 == 0 or i == 1 or i == total_days:
            print(f"  {strategy_name}: day {i}/{total_days}  equity=${sim_equity:,.0f}")

        # Build snapshot and prompt
        try:
            snapshot = _build_historical_snapshot(all_bars, date_str, core_symbols)
        except Exception as exc:
            log.warning("Snapshot build failed for %s on %s: %s", strategy_name, date_str, exc)
            equity_curve.append(sim_equity)
            continue

        if not snapshot.get("sym_indicators"):
            log.debug("No indicator data for %s — skipping", date_str)
            equity_curve.append(sim_equity)
            continue

        prompt = _build_backtest_prompt(snapshot, date_str, sim_equity)

        # Call Claude (strategy system prompt is static per run — cache it)
        try:
            response = claude.messages.create(
                model="claude-sonnet-4-6",
                system=[{
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1024,
                extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
            )
            usage    = response.usage
            cr       = getattr(usage, "cache_read_input_tokens",     0) or 0
            cw       = getattr(usage, "cache_creation_input_tokens", 0) or 0
            log.debug("[BACKTEST] Cache: reads=%d writes=%d", cr, cw)
            try:
                from cost_tracker import get_tracker
                get_tracker().record_api_call(
                    "claude-sonnet-4-6", usage,
                    caller=f"backtest_{strategy_name[:20]}",
                )
            except Exception:
                pass
            raw_text = response.content[0].text if response.content else ""
        except Exception as exc:
            log.warning("Claude API error for %s on %s: %s", strategy_name, date_str, exc)
            equity_curve.append(sim_equity)
            time.sleep(0.5)
            continue

        time.sleep(0.5)  # Rate limit buffer

        # Parse actions
        actions = _parse_claude_actions(raw_text)
        if actions is None:
            log.debug("JSON parse failed for %s on %s — treating as hold", strategy_name, date_str)
            equity_curve.append(sim_equity)
            continue

        # Execute each action
        day_pnl = 0.0
        for action in actions:
            action_type = str(action.get("action", "hold")).lower()
            if action_type not in ("buy", "sell"):
                continue

            sym = str(action.get("symbol", "")).upper().strip()
            if not sym:
                continue

            # Skip crypto (can't simulate easily with daily bars)
            if "/" in sym:
                log.debug("Skipping crypto symbol %s in backtest", sym)
                continue

            # Skip if already holding
            if sym in open_positions:
                log.debug("Already holding %s — skipping duplicate entry", sym)
                continue

            # Position sizing: cap qty by tier
            tier     = str(action.get("tier", "core")).lower()
            tier_pct = TIER_MAX_PCT.get(tier, TIER_MAX_PCT["core"])
            max_pos  = sim_equity * tier_pct

            # Get entry price from snapshot indicators
            entry_close = snapshot["sym_indicators"].get(sym, {}).get("close", None)
            if not entry_close or entry_close <= 0:
                log.debug("No close price for %s on %s — skipping", sym, date_str)
                continue

            # Override qty with properly sized amount
            claude_qty    = max(1, int(action.get("qty", 1)))
            max_qty       = max(1, int(max_pos / entry_close))
            effective_qty = min(claude_qty, max_qty)

            # Validate stop and take profit
            try:
                stop_loss   = float(action.get("stop_loss",   entry_close * 0.97))
                take_profit = float(action.get("take_profit", entry_close * 1.06))
            except (TypeError, ValueError):
                stop_loss   = entry_close * 0.97
                take_profit = entry_close * 1.06

            # Simulate the trade
            sim_action = {
                "action":      action_type,
                "symbol":      sym,
                "qty":         effective_qty,
                "stop_loss":   stop_loss,
                "take_profit": take_profit,
                "tier":        tier,
            }
            result = _simulate_trade(sym, sim_action, date_str, all_bars)
            if result is None:
                continue

            all_trades.append(result)
            day_pnl += result["pnl"]
            log.info(
                "[%s] %s %s %d@%.2f → exit %.2f  pnl=%.2f  outcome=%s  days=%d",
                strategy_name, action_type.upper(), sym,
                effective_qty, result["entry_price"], result["exit_price"],
                result["pnl"], result["outcome"], result["hold_days"],
            )

        sim_equity += day_pnl
        sim_equity  = max(sim_equity, 0.01)  # Guard against going negative
        equity_curve.append(sim_equity)

    stats = _compute_stats(equity_curve, all_trades, strategy_name)
    stats["trades"] = all_trades

    print(
        f"\n  [{strategy_name}] DONE — "
        f"return={stats['return_pct']:+.1f}%  "
        f"win_rate={stats['win_rate']:.0f}%  "
        f"sharpe={stats['sharpe']:.2f}  "
        f"drawdown={stats['max_drawdown']:.1f}%  "
        f"trades={stats['total_trades']}"
    )

    return stats


# ── Strategy Director ─────────────────────────────────────────────────────────

def _run_strategy_director(results: dict[str, dict]) -> dict:
    """
    Call Claude as Strategy Director to compare results and recommend the winner.
    Returns dict with: winner, rationale, parameter_adjustments.
    """
    log.info("Running Strategy Director vote...")

    # Build results table
    table_lines = [
        f"{'Strategy':<20} {'Return%':>8} {'WinRate%':>9} {'Sharpe':>7} "
        f"{'MaxDD%':>7} {'ProfFactor':>11} {'Trades':>7}",
        "-" * 73,
    ]
    for name, r in results.items():
        pf = r.get("profit_factor", 0)
        pf_str = f"{pf:.2f}" if isinstance(pf, float) and not math.isinf(pf) else "inf"
        table_lines.append(
            f"{name:<20} {r.get('return_pct', 0):>+8.2f} "
            f"{r.get('win_rate', 0):>9.1f} "
            f"{r.get('sharpe', 0):>7.3f} "
            f"{r.get('max_drawdown', 0):>7.2f} "
            f"{pf_str:>11} "
            f"{r.get('total_trades', 0):>7}"
        )

    results_table = "\n".join(table_lines)

    user_prompt = f"""You are reviewing backtesting results for a trading bot.
Below are performance metrics for 5 strategy variants tested on the same historical data.

{results_table}

Strategy descriptions:
- momentum: Buys breakouts above MA20 with RSI 50-70 and 3x+ volume. High-conviction trend following.
- mean_reversion: Buys extreme oversold (RSI<30, >5% below MA20), shorts extreme overbought.
- news_sentiment: Trades only on named catalysts (earnings, FDA, M&A). Ignores technicals.
- cross_sector: Exploits second-order sector correlations (oil→defense, dollar→EM, etc.).
- hybrid: Balanced synthesis of all signals, requires 2+ confirming signals.

Based on these results, recommend the best strategy and provide parameter tuning guidance.

Respond with ONLY valid JSON (no markdown, no explanation outside JSON):
{{
  "winner": "<strategy_name>",
  "rationale": "<2-3 sentences explaining why this strategy won>",
  "parameter_adjustments": {{
    "momentum_weight": <float 0-1>,
    "mean_reversion_weight": <float 0-1>,
    "news_sentiment_weight": <float 0-1>,
    "cross_sector_weight": <float 0-1>,
    "min_confidence_threshold": "low|medium|high",
    "max_positions": <int>,
    "sector_rotation_bias": "<sector name or neutral>",
    "stop_loss_pct_core": <float>,
    "take_profit_multiple": <float>,
    "director_notes": "<one paragraph with specific tuning recommendations>"
  }}
}}"""

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            system=(
                "You are a Strategy Director reviewing backtesting results for a trading bot. "
                "Analyze the performance metrics of 5 strategy variants and recommend the winner "
                "with specific parameter adjustments. Be quantitative and concise."
            ),
            messages=[{"role": "user", "content": user_prompt}],
            max_tokens=1024,
        )
        raw_text = response.content[0].text if response.content else "{}"
    except Exception as exc:
        log.error("Strategy Director API call failed: %s", exc)
        raw_text = "{}"

    time.sleep(0.5)

    # Parse Director response
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text  = "\n".join(lines[1:])
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]

    try:
        director = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end   = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                director = json.loads(text[start:end])
            except json.JSONDecodeError:
                log.warning("Director response parse failed — using fallback")
                director = {}
        else:
            director = {}

    # Validate winner is a known strategy
    winner = director.get("winner", "")
    if winner not in STRATEGY_PROMPTS:
        # Fallback: pick highest Sharpe
        winner = max(results, key=lambda s: results[s].get("sharpe", -999))
        log.warning("Director returned invalid winner '%s' — falling back to %s (best Sharpe)", director.get("winner"), winner)
        director["winner"]   = winner
        director["rationale"] = "Fallback selection: highest Sharpe ratio."

    log.info("Strategy Director selected: %s", winner)
    return director


# ── Config writer ─────────────────────────────────────────────────────────────

def _write_strategy_config(director: dict, results: dict[str, dict]) -> None:
    """Update strategy_config.json with Director's verdict and backtest results."""
    try:
        existing = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    except Exception:
        existing = {}

    params      = director.get("parameter_adjustments", {})
    director_notes = params.pop("director_notes", director.get("rationale", ""))

    # Merge with existing parameters (keep keys not touched by Director)
    existing_params = existing.get("parameters", {})
    merged_params   = {**existing_params, **params}

    config = {
        "version":          existing.get("version", 1),
        "generated_at":     datetime.now(timezone.utc).isoformat(),
        "generated_by":     "backtest",
        "active_strategy":  director.get("winner", existing.get("active_strategy", "hybrid")),
        "backtest_results": {
            name: {
                "return_pct":    r.get("return_pct"),
                "win_rate":      r.get("win_rate"),
                "sharpe":        r.get("sharpe"),
                "max_drawdown":  r.get("max_drawdown"),
                "profit_factor": r.get("profit_factor") if not math.isinf(r.get("profit_factor", 0)) else 99.9,
                "total_trades":  r.get("total_trades"),
            }
            for name, r in results.items()
        },
        "parameters":       merged_params,
        "director_notes":   director_notes,
    }

    CONFIG_FILE.write_text(json.dumps(config, indent=2))
    log.info("strategy_config.json updated — active_strategy=%s", config["active_strategy"])


# ── Terminal output ───────────────────────────────────────────────────────────

def _print_results_table(results: dict[str, dict], winner: str = "") -> None:
    """Print a formatted terminal summary."""
    print("\n" + "=" * 78)
    print("  BACKTEST RESULTS SUMMARY")
    print("=" * 78)
    print(
        f"  {'Strategy':<20} {'Return%':>8} {'WinRate':>8} {'Sharpe':>7} "
        f"{'MaxDD%':>7} {'PF':>6} {'Trades':>7}"
    )
    print("  " + "-" * 70)
    for name, r in sorted(results.items(), key=lambda x: -x[1].get("return_pct", -999)):
        tag = " <-- WINNER" if name == winner else ""
        pf  = r.get("profit_factor", 0)
        pf_str = f"{pf:.2f}" if isinstance(pf, float) and not math.isinf(pf) else " inf"
        print(
            f"  {name:<20} {r.get('return_pct', 0):>+8.2f}% "
            f"{r.get('win_rate', 0):>7.1f}% "
            f"{r.get('sharpe', 0):>7.3f} "
            f"{r.get('max_drawdown', 0):>6.2f}% "
            f"{pf_str:>6} "
            f"{r.get('total_trades', 0):>7}"
            f"{tag}"
        )
    print("=" * 78 + "\n")


# ── Main entry point ──────────────────────────────────────────────────────────

def run_backtest(strategy: Optional[str] = None, days: int = 30) -> dict:
    """
    Main entry point.
    strategy: if specified, run only that strategy (skip Director vote).
    days: number of historical trading days to simulate (default 30, max 90).
    Returns results dict.
    """
    days = min(max(days, 1), 90)

    log.info("Loading historical bar data...")
    all_bars = _load_all_bars()

    if not all_bars:
        log.error("No bar data found in %s — run data_warehouse.py first", BARS_DIR)
        return {}

    log.info("Loading core watchlist symbols...")
    core_symbols = get_core()

    # Collect all available trading dates across all symbols
    all_dates: set[str] = set()
    for bars in all_bars.values():
        for b in bars:
            d = str(b.get("date", ""))
            if d:
                all_dates.add(d)

    sorted_dates = sorted(all_dates)

    if len(sorted_dates) < EVERY_NTH_DAY + 1:
        log.error("Not enough trading dates in bar data (%d found)", len(sorted_dates))
        return {}

    # Use the last `days` trading dates, then pick every Nth
    window_dates = sorted_dates[-(days):]
    trade_dates  = window_dates[::EVERY_NTH_DAY]

    if not trade_dates:
        log.error("No trade dates selected")
        return {}

    log.info(
        "Simulation window: %s → %s  (%d dates → %d trade days, every %dth)",
        window_dates[0], window_dates[-1], len(window_dates), len(trade_dates), EVERY_NTH_DAY,
    )

    # Select strategies to run
    strategies_to_run: dict[str, str] = (
        {strategy: STRATEGY_PROMPTS[strategy]} if strategy and strategy in STRATEGY_PROMPTS
        else STRATEGY_PROMPTS
    )

    results: dict[str, dict] = {}
    for strat_name, strat_prompt in strategies_to_run.items():
        print(f"\n{'─' * 60}")
        print(f"  Running strategy: {strat_name.upper()}")
        print(f"{'─' * 60}")
        results[strat_name] = _run_strategy(
            strategy_name=strat_name,
            system_prompt=strat_prompt,
            all_bars=all_bars,
            core_symbols=core_symbols,
            trade_dates=trade_dates,
        )

    # Strategy Director vote (only when running all strategies)
    director: dict = {}
    winner: str    = strategy or ""
    if not strategy and len(results) > 1:
        print(f"\n{'─' * 60}")
        print("  Running Strategy Director vote...")
        print(f"{'─' * 60}")
        director = _run_strategy_director(results)
        winner   = director.get("winner", "")
        print(f"\n  Director's verdict: {winner.upper()}")
        print(f"  Rationale: {director.get('rationale', '')}")
        _write_strategy_config(director, results)

    # Print terminal table
    _print_results_table(results, winner=winner)

    # Save report
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report_path = REPORTS_DIR / f"backtest_{report_date}.json"
    report = {
        "generated_at":    datetime.now(timezone.utc).isoformat(),
        "days_simulated":  days,
        "trade_dates":     trade_dates,
        "strategies_run":  list(results.keys()),
        "winner":          winner,
        "director":        director,
        "results":         {
            name: {k: v for k, v in r.items() if k != "trades"}
            for name, r in results.items()
        },
        "all_trades": {
            name: r.get("trades", [])
            for name, r in results.items()
        },
    }
    try:
        report_path.write_text(json.dumps(report, indent=2, default=str))
        log.info("Backtest report saved to %s", report_path)
        print(f"  Report saved: {report_path}")
    except Exception as exc:
        log.error("Failed to write report: %s", exc)

    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Strategy backtesting harness")
    parser.add_argument(
        "--strategy",
        choices=list(STRATEGY_PROMPTS.keys()),
        help="Run a single strategy (default: all 5)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Historical days to simulate (default: 30, max: 90)",
    )
    args = parser.parse_args()
    run_backtest(strategy=args.strategy, days=args.days)
