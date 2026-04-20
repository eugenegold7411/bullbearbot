"""
thesis_backtest.py — Deterministic price-return backtest for thesis expressions (TL-2a).

Ring 2 only — advisory shadow, never touches live execution.
Purely deterministic: no Claude calls, no AI in this module.
Gated behind enable_thesis_backtests feature flag (caller responsibility).

Importable with no env vars set.
Zero imports from: bot.py, order_executor.py, risk_kernel.py
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────

_THESIS_LAB_DIR = Path(__file__).parent / "data" / "thesis_lab"
_BACKTESTS_FILE = _THESIS_LAB_DIR / "backtests.jsonl"

# ─────────────────────────────────────────────────────────────────────────────
# Data class
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ThesisBacktestResult:
    thesis_id: str
    expression_id: str      # "base" for base_expression
    mode: str               # "base" | "opportunity_cost"
    entry_date: str         # ISO date from thesis.date_opened
    checkpoints: dict       # {"3m": "YYYY-MM-DD", "6m": ..., "9m": ..., "12m": ...}
    roi_3m: Optional[float]
    roi_6m: Optional[float]
    roi_9m: Optional[float]
    roi_12m: Optional[float]
    max_drawdown: float     # max peak-to-trough over holding period (0–1 scale)
    final_verdict: str      # "profitable" | "loss" | "inconclusive" | "pending"
    data_quality: str       # "full" | "partial" | "insufficient"
    missing_checkpoints: list[str]  # e.g. ["9m", "12m"] when future dates unavailable
    schema_version: int = 1


# ─────────────────────────────────────────────────────────────────────────────
# Feature flag
# ─────────────────────────────────────────────────────────────────────────────

def _is_enabled() -> bool:
    try:
        from feature_flags import is_enabled
        return is_enabled("enable_thesis_backtests", default=False)
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Checkpoint date arithmetic
# ─────────────────────────────────────────────────────────────────────────────

_CHECKPOINT_OFFSETS: dict[str, int] = {"3m": 91, "6m": 182, "9m": 273, "12m": 365}


def compute_checkpoint_dates(entry_date: str) -> dict[str, str]:
    """Return {"3m": "YYYY-MM-DD", ...} — N calendar days from entry_date."""
    try:
        base = datetime.strptime(entry_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        base = date.today()
    return {
        label: (base + timedelta(days=days)).isoformat()
        for label, days in _CHECKPOINT_OFFSETS.items()
    }


# ─────────────────────────────────────────────────────────────────────────────
# Symbol normalisation
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_symbol(symbol: str) -> str:
    """
    Convert a symbol string to a yfinance-compatible ticker.
    Returns "" for symbols that cannot be fetched from yfinance
    (spaces, bond notation, %-containing strings, Chinese A-share suffix).
    """
    s = symbol.strip()
    # Skip descriptive or composite symbols
    if " " in s or "%" in s:
        return ""
    # Convert crypto slash notation: BTC/USD → BTC-USD
    s = s.replace("/", "-")
    return s


# ─────────────────────────────────────────────────────────────────────────────
# Price fetching (yfinance, non-fatal)
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_prices(symbol: str, from_date: str, to_date: str) -> list[dict]:
    """
    Fetch daily adjusted-close prices for symbol in [from_date, to_date].
    Returns [{"date": "YYYY-MM-DD", "close": float}] sorted ascending.
    Returns [] on any error.
    """
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        df = ticker.history(
            start=from_date,
            end=to_date,
            interval="1d",
            auto_adjust=True,
        )
        if df is None or df.empty:
            return []
        df = df[["Close"]].copy()
        # Remove timezone info for consistent string formatting
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df["date"]  = df.index.strftime("%Y-%m-%d")
        df          = df.rename(columns={"Close": "close"})
        df["close"] = df["close"].astype(float)
        df          = df[["date", "close"]].dropna().sort_values("date").reset_index(drop=True)
        return df.to_dict("records")
    except Exception as exc:
        log.debug("[THESIS_BT] _fetch_prices(%s, %s, %s) failed: %s", symbol, from_date, to_date, exc)
        return []


def _price_on_or_after(prices: list[dict], target_date: str) -> Optional[float]:
    """First close on or after target_date. None if no such bar exists."""
    for p in prices:
        if str(p["date"]) >= target_date:
            return float(p["close"])
    return None


def _price_on_or_before(prices: list[dict], target_date: str) -> Optional[float]:
    """Last close on or before target_date. None if no such bar exists."""
    result = None
    for p in prices:
        if str(p["date"]) <= target_date:
            result = float(p["close"])
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Pure calculation functions (deterministic, no I/O)
# ─────────────────────────────────────────────────────────────────────────────

def calc_roi(entry_price: float, exit_price: float, direction: str) -> float:
    """
    ROI for a single price pair.
    Long:  (exit - entry) / entry
    Short: (entry - exit) / entry  (P&L inverted)
    Returns 0.0 if entry_price ≤ 0.
    """
    if entry_price <= 0:
        return 0.0
    raw = (exit_price - entry_price) / entry_price
    return round(-raw if direction.lower() == "short" else raw, 6)


def calc_max_drawdown(prices: list[dict], direction: str = "long") -> float:
    """
    Maximum peak-to-trough drawdown over the price series.
    Long:  max((peak - trough) / peak) across all peaks
    Short: computed on the inverted price series (1/close)
    Returns 0.0 when fewer than 2 data points.
    """
    closes = [float(p["close"]) for p in prices if p.get("close") is not None]
    if len(closes) < 2:
        return 0.0

    if direction.lower() == "short":
        closes = [1.0 / c for c in closes if c > 0]
        if len(closes) < 2:
            return 0.0

    peak   = closes[0]
    max_dd = 0.0
    for c in closes:
        if c > peak:
            peak = c
        elif peak > 0:
            dd = (peak - c) / peak
            if dd > max_dd:
                max_dd = dd

    return round(max_dd, 6)


# ─────────────────────────────────────────────────────────────────────────────
# Verdict and quality logic (deterministic)
# ─────────────────────────────────────────────────────────────────────────────

def compute_data_quality(rois: list[Optional[float]]) -> str:
    """
    full        → all 4 checkpoint ROIs are present
    partial     → 1–3 checkpoint ROIs present
    insufficient → no checkpoint ROIs at all
    """
    n = sum(1 for r in rois if r is not None)
    if n == 4:
        return "full"
    if n == 0:
        return "insufficient"
    return "partial"


def compute_verdict(
    roi_3m: Optional[float],
    roi_6m: Optional[float],
    roi_9m: Optional[float],
    roi_12m: Optional[float],
    data_quality: str,
) -> str:
    """
    pending       → no checkpoint data (all ROIs None)
    profitable    → most-recent available ROI > 1%
    loss          → most-recent available ROI < -1%
    inconclusive  → available ROI within ±1% (noise zone)
    """
    if data_quality == "insufficient":
        return "pending"
    # Use the most recently-reached checkpoint that has data
    for roi in (roi_12m, roi_9m, roi_6m, roi_3m):
        if roi is not None:
            if roi > 0.01:
                return "profitable"
            if roi < -0.01:
                return "loss"
            return "inconclusive"
    return "pending"


# ─────────────────────────────────────────────────────────────────────────────
# Per-symbol backtest
# ─────────────────────────────────────────────────────────────────────────────

def _backtest_symbol(
    symbol: str,
    entry_date: str,
    checkpoints: dict[str, str],
    direction: str,
) -> dict:
    """
    Fetch prices and compute ROIs for a single symbol.
    Returns a dict with roi_3m/6m/9m/12m and max_drawdown.
    All ROI fields are None if data unavailable.
    Never raises.
    """
    ticker = _normalize_symbol(symbol)
    if not ticker:
        log.debug("[THESIS_BT] %r skipped — not a yfinance ticker", symbol)
        return {"roi_3m": None, "roi_6m": None, "roi_9m": None, "roi_12m": None, "max_drawdown": 0.0}

    today      = datetime.now().strftime("%Y-%m-%d")
    max_cp     = max(checkpoints.values())
    # Fetch data from entry_date to today (checkpoints may be future — that's fine)
    end_date   = min(max_cp, today)

    prices = _fetch_prices(ticker, entry_date, end_date)
    if not prices:
        log.debug("[THESIS_BT] No price data for %s from %s", ticker, entry_date)
        return {"roi_3m": None, "roi_6m": None, "roi_9m": None, "roi_12m": None, "max_drawdown": 0.0}

    entry_price = _price_on_or_after(prices, entry_date)
    if entry_price is None or entry_price <= 0:
        return {"roi_3m": None, "roi_6m": None, "roi_9m": None, "roi_12m": None, "max_drawdown": 0.0}

    rois: dict[str, Optional[float]] = {}
    for label, cp_date in checkpoints.items():
        cp_price = _price_on_or_after(prices, cp_date)
        rois[label] = (
            calc_roi(entry_price, cp_price, direction)
            if cp_price is not None
            else None
        )

    max_dd = calc_max_drawdown(prices, direction)
    return {
        "roi_3m":  rois.get("3m"),
        "roi_6m":  rois.get("6m"),
        "roi_9m":  rois.get("9m"),
        "roi_12m": rois.get("12m"),
        "max_drawdown": max_dd,
    }


def _average_symbol_results(symbol_results: list[dict]) -> dict:
    """
    Average ROI values across multiple symbols.
    A checkpoint is None only if ALL symbols lack data for it.
    max_drawdown is the worst (highest) across symbols.
    """
    def _avg(field: str) -> Optional[float]:
        vals = [r[field] for r in symbol_results if r.get(field) is not None]
        return round(sum(vals) / len(vals), 6) if vals else None

    return {
        "roi_3m":       _avg("roi_3m"),
        "roi_6m":       _avg("roi_6m"),
        "roi_9m":       _avg("roi_9m"),
        "roi_12m":      _avg("roi_12m"),
        "max_drawdown": max((r.get("max_drawdown", 0.0) for r in symbol_results), default=0.0),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Storage
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_dir() -> None:
    _THESIS_LAB_DIR.mkdir(parents=True, exist_ok=True)


def append_backtest_result(result: ThesisBacktestResult) -> None:
    """Append one backtest result to backtests.jsonl (JSONL, one record per line)."""
    _ensure_dir()
    entry = {"backtested_at": datetime.now(timezone.utc).isoformat(), **asdict(result)}
    with _BACKTESTS_FILE.open("a") as fh:
        fh.write(json.dumps(entry, default=str) + "\n")


def load_backtest_results(thesis_id: str = None) -> list[dict]:
    """
    Load all backtest results from backtests.jsonl.
    If thesis_id is given, return only results for that thesis.
    Returns [] on any I/O error.
    """
    _ensure_dir()
    if not _BACKTESTS_FILE.exists():
        return []
    results = []
    try:
        with _BACKTESTS_FILE.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    if thesis_id is None or r.get("thesis_id") == thesis_id:
                        results.append(r)
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def backtest_thesis(thesis) -> ThesisBacktestResult:
    """
    Backtest a ThesisRecord using historical prices from yfinance.
    For each symbol in thesis.base_expression.symbols:
      - Fetch prices from thesis.date_opened to the latest available date
      - Calculate returns at 3m/6m/9m/12m checkpoints
      - Calculate max drawdown over the holding period
    Averages ROIs across all symbols in the expression.
    Non-fatal: returns data_quality="insufficient" on any data failure.
    Never modifies ThesisRecord status (that belongs to the evaluator).
    """
    thesis_id  = getattr(thesis, "thesis_id", "")
    entry_date = getattr(thesis, "date_opened", "") or datetime.now().strftime("%Y-%m-%d")
    expr       = getattr(thesis, "base_expression", {}) or {}
    symbols    = expr.get("symbols") or []
    direction  = expr.get("direction", "long") or "long"

    checkpoints    = compute_checkpoint_dates(entry_date)
    symbol_results = []

    for sym in symbols:
        if not sym or not str(sym).strip():
            continue
        try:
            r = _backtest_symbol(sym, entry_date, checkpoints, direction)
            symbol_results.append(r)
        except Exception as exc:
            log.warning("[THESIS_BT] Symbol %s in %s failed: %s", sym, thesis_id, exc)
            symbol_results.append({
                "roi_3m": None, "roi_6m": None, "roi_9m": None, "roi_12m": None,
                "max_drawdown": 0.0,
            })

    averaged = (
        _average_symbol_results(symbol_results)
        if symbol_results
        else {"roi_3m": None, "roi_6m": None, "roi_9m": None, "roi_12m": None, "max_drawdown": 0.0}
    )

    rois    = [averaged["roi_3m"], averaged["roi_6m"], averaged["roi_9m"], averaged["roi_12m"]]
    missing = [
        label for label, roi in zip(("3m", "6m", "9m", "12m"), rois)
        if roi is None
    ]
    data_quality = compute_data_quality(rois)
    verdict      = compute_verdict(
        averaged["roi_3m"], averaged["roi_6m"],
        averaged["roi_9m"], averaged["roi_12m"],
        data_quality,
    )

    return ThesisBacktestResult(
        thesis_id           = thesis_id,
        expression_id       = "base",
        mode                = "base",
        entry_date          = entry_date,
        checkpoints         = checkpoints,
        roi_3m              = averaged["roi_3m"],
        roi_6m              = averaged["roi_6m"],
        roi_9m              = averaged["roi_9m"],
        roi_12m             = averaged["roi_12m"],
        max_drawdown        = averaged["max_drawdown"],
        final_verdict       = verdict,
        data_quality        = data_quality,
        missing_checkpoints = missing,
        schema_version      = 1,
    )


def backtest_all_theses(
    status_filter: str = "researched",
    force: bool = False,
) -> list[ThesisBacktestResult]:
    """
    Run backtest_thesis() on all theses matching status_filter.
    Skips quarantined entries regardless of status_filter.
    Appends each result to backtests.jsonl.
    Non-fatal: logs WARNING on individual thesis failures and continues.

    Gated behind enable_thesis_backtests flag unless force=True.
    """
    if not force and not _is_enabled():
        log.info("[THESIS_BT] Skipping: enable_thesis_backtests=false (pass force=True to override)")
        return []

    from thesis_registry import list_theses

    theses  = list_theses(status=status_filter)
    results = []

    log.info("[THESIS_BT] Running backtests for %d theses (filter=%s)", len(theses), status_filter)

    for thesis in theses:
        if thesis.status == "quarantine":
            log.debug("[THESIS_BT] Skipping quarantined %s", thesis.thesis_id)
            continue
        try:
            result = backtest_thesis(thesis)
            append_backtest_result(result)
            results.append(result)
            log.info(
                "[THESIS_BT] %s | verdict=%s dq=%s roi_3m=%s roi_12m=%s",
                thesis.thesis_id[:30],
                result.final_verdict,
                result.data_quality,
                f"{result.roi_3m:.2%}" if result.roi_3m is not None else "—",
                f"{result.roi_12m:.2%}" if result.roi_12m is not None else "—",
            )
        except Exception as exc:
            log.warning("[THESIS_BT] backtest_thesis(%s) failed: %s", thesis.thesis_id, exc)

    log.info("[THESIS_BT] Done: %d results written", len(results))
    return results
