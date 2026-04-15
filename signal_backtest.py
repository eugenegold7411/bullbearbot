"""
signal_backtest.py — signal-level forward-return backtesting.

For each TradeIdea signal extracted from memory/decisions.json or
data/analytics/near_miss_log.jsonl, looks up the forward close price
at +1d / +3d / +5d business-day offsets in the daily bars cache.

Returns {} on insufficient data or any failure — never raises.

Forward return windows: +1d, +3d, +5d only (daily bars; no intraday resolution).
Business-day arithmetic: each offset is the Nth daily bar strictly after the
signal date (i.e. the Nth trading day close).
"""

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from log_setup import get_logger

log = get_logger(__name__)

BASE_DIR        = Path(__file__).parent
BARS_DIR        = BASE_DIR / "data" / "bars"
DECISIONS_FILE  = BASE_DIR / "memory" / "decisions.json"
NEAR_MISS_LOG   = BASE_DIR / "data" / "analytics" / "near_miss_log.jsonl"
BACKTEST_OUTPUT = BASE_DIR / "data" / "reports" / "backtest_latest.json"

MIN_SIGNALS = 5   # minimum total signals to compute any stats


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class SignalBacktestResult:
    symbol:        str
    intent:        str            # "BUY" | "SELL" | "HOLD"
    signal_score:  float
    decision_date: str            # "YYYY-MM-DD"
    entry_price:   Optional[float]
    return_1d:     Optional[float]   # fractional % return at +1 trading day close
    return_3d:     Optional[float]
    return_5d:     Optional[float]
    correct_1d:    Optional[bool]    # True if price moved in direction of intent
    correct_3d:    Optional[bool]
    correct_5d:    Optional[bool]
    source:        str = "decisions"  # "decisions" | "near_miss"


@dataclass
class SignalBacktestSummary:
    symbol:       str
    n_signals:    int
    avg_score:    float
    win_rate_1d:  float    # fraction 0–1
    win_rate_3d:  float
    win_rate_5d:  float
    avg_return_1d: float   # mean fractional return
    avg_return_3d: float
    avg_return_5d: float
    alpha_score:  float    # composite ranking score (higher = better)
    has_alpha:    bool     # True when win_rate_1d > 0.55 AND avg_return_1d > 0.003


# ── Bar loading ───────────────────────────────────────────────────────────────

def _load_bars_for_symbol(symbol: str) -> list[dict]:
    """
    Load daily bars from data/bars/{SYMBOL}_daily.csv.
    Symbol normalisation: BTC/USD → BTC_USD.
    Returns [] on any failure.
    """
    try:
        csv_name = symbol.replace("/", "_") + "_daily.csv"
        csv_path = BARS_DIR / csv_name
        if not csv_path.exists():
            return []
        df = pd.read_csv(csv_path)
        df.columns = [c.lower().strip() for c in df.columns]
        if "date" not in df.columns or "close" not in df.columns:
            return []
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df = df.dropna(subset=["date", "close"])
        df = df.sort_values("date").reset_index(drop=True)
        return df.to_dict("records")
    except Exception as exc:
        log.debug("[BACKTEST] _load_bars_for_symbol(%s) failed: %s", symbol, exc)
        return []


# ── Price lookup ──────────────────────────────────────────────────────────────

def _get_price_at_offset(bars: list[dict], from_date: str, offset_days: int) -> Optional[float]:
    """
    Return close price at offset_days trading days after from_date.
    offset_days=1 → next available trading day close.
    Returns None when insufficient future bars.
    """
    try:
        future = [b for b in bars if str(b["date"]) > from_date]
        if len(future) < offset_days:
            return None
        return float(future[offset_days - 1]["close"])
    except Exception:
        return None


# ── Return computation ────────────────────────────────────────────────────────

def _compute_forward_return(
    entry_price: float,
    future_price: Optional[float],
    intent: str,
) -> tuple[Optional[float], Optional[bool]]:
    """
    Compute fractional return and direction correctness.

    BUY  → positive return = correct
    SELL → negative return = correct
    HOLD → correct=None (no directional expectation)

    Returns (return_fraction, correct) or (None, None) on any error.
    """
    try:
        if future_price is None or entry_price <= 0:
            return None, None
        ret = (future_price - entry_price) / entry_price
        intent_up = intent.upper()
        if intent_up in ("BUY", "LONG"):
            correct: Optional[bool] = ret > 0
        elif intent_up in ("SELL", "SHORT"):
            correct = ret < 0
        else:
            correct = None
        return round(ret, 6), correct
    except Exception:
        return None, None


# ── Signal extraction ─────────────────────────────────────────────────────────

def _extract_signals_from_decisions(lookback_days: int = 30) -> list[dict]:
    """
    Extract trade signals from memory/decisions.json.
    Handles both:
      - new format: decision["ideas"][].{intent, symbol, signal_score}
      - old format: decision["actions"][].{action, symbol, score}
    Returns [{"symbol", "intent", "signal_score", "date", "source"}].
    """
    signals: list[dict] = []
    try:
        if not DECISIONS_FILE.exists():
            return signals
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=lookback_days)
        ).isoformat()[:10]

        raw       = json.loads(DECISIONS_FILE.read_text())
        decisions = raw if isinstance(raw, list) else raw.get("decisions", [])

        for dec in decisions:
            ts       = dec.get("ts", "")
            date_str = ts[:10] if len(ts) >= 10 else ""
            if not date_str or date_str < cutoff:
                continue

            # Support both new (ideas[]) and legacy (actions[]) format
            ideas = dec.get("ideas") or dec.get("actions") or []
            for idea in ideas:
                intent = (idea.get("intent") or idea.get("action") or "").upper().strip()
                symbol = (idea.get("symbol") or "").upper().strip()
                score  = float(idea.get("signal_score") or idea.get("score") or 0.0)
                if not symbol or not intent:
                    continue
                signals.append({
                    "symbol":       symbol,
                    "intent":       intent,
                    "signal_score": score,
                    "date":         date_str,
                    "source":       "decisions",
                })
    except Exception as exc:
        log.debug("[BACKTEST] _extract_signals_from_decisions failed: %s", exc)
    return signals


def _extract_signals_from_near_misses(lookback_days: int = 30) -> list[dict]:
    """
    Extract rejected / near-miss signals from near_miss_log.jsonl.
    Only includes events with a directional expectation:
      rejected_by_risk_kernel, rejected_by_policy, below_threshold_near_miss.
    Returns [{"symbol", "intent", "signal_score", "date", "source"}].
    """
    signals: list[dict] = []
    try:
        if not NEAR_MISS_LOG.exists():
            return signals
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=lookback_days)
        ).isoformat()[:10]

        with NEAR_MISS_LOG.open() as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    rec      = json.loads(line)
                    ts       = rec.get("ts", "")
                    date_str = ts[:10] if len(ts) >= 10 else ""
                    if not date_str or date_str < cutoff:
                        continue
                    if rec.get("event_type") not in (
                        "rejected_by_risk_kernel",
                        "rejected_by_policy",
                        "below_threshold_near_miss",
                    ):
                        continue
                    det    = rec.get("details", {})
                    symbol = (rec.get("symbol") or "").upper().strip()
                    intent = (det.get("intent") or "BUY").upper()
                    score  = float(det.get("signal_score") or 0.0)
                    if not symbol:
                        continue
                    signals.append({
                        "symbol":       symbol,
                        "intent":       intent,
                        "signal_score": score,
                        "date":         date_str,
                        "source":       "near_miss",
                    })
                except (json.JSONDecodeError, ValueError):
                    continue
    except Exception as exc:
        log.debug("[BACKTEST] _extract_signals_from_near_misses failed: %s", exc)
    return signals


# ── Main backtest ─────────────────────────────────────────────────────────────

def run_signal_backtest(lookback_days: int = 30) -> dict:
    """
    Main entry point.

    1. Extracts signals from decisions.json + near_miss_log.jsonl.
    2. Loads daily bars for each unique symbol.
    3. Computes forward returns at +1d / +3d / +5d.
    4. Aggregates per-symbol summaries.

    Returns {} on any failure.
    Returns {"status": "insufficient_data", ...} when fewer than MIN_SIGNALS found.
    Never raises.
    """
    try:
        # ── Collect signals ───────────────────────────────────────────────────
        signals = _extract_signals_from_decisions(lookback_days)
        signals += _extract_signals_from_near_misses(lookback_days)

        if len(signals) < MIN_SIGNALS:
            log.info(
                "[BACKTEST] Insufficient signals (%d < %d) — skipping",
                len(signals), MIN_SIGNALS,
            )
            return {
                "status":       "insufficient_data",
                "n_signals":    len(signals),
                "min_required": MIN_SIGNALS,
                "summaries":    {},
            }

        # ── Load bars (cache per symbol) ──────────────────────────────────────
        bars_cache: dict[str, list[dict]] = {}
        for sig in signals:
            sym = sig["symbol"]
            if sym not in bars_cache:
                bars_cache[sym] = _load_bars_for_symbol(sym)

        # ── Compute forward returns ───────────────────────────────────────────
        results: list[SignalBacktestResult] = []
        for sig in signals:
            sym    = sig["symbol"]
            date   = sig["date"]
            intent = sig["intent"]
            bars   = bars_cache.get(sym, [])

            # Entry price: close on signal date (or last available before it)
            on_date = [b for b in bars if str(b["date"]) == date]
            if on_date:
                entry_price: Optional[float] = float(on_date[-1]["close"])
            else:
                before = [b for b in bars if str(b["date"]) <= date]
                entry_price = float(before[-1]["close"]) if before else None

            p1 = _get_price_at_offset(bars, date, 1)
            p3 = _get_price_at_offset(bars, date, 3)
            p5 = _get_price_at_offset(bars, date, 5)

            ep = entry_price if entry_price else 0.0
            r1, c1 = _compute_forward_return(ep, p1, intent) if ep > 0 else (None, None)
            r3, c3 = _compute_forward_return(ep, p3, intent) if ep > 0 else (None, None)
            r5, c5 = _compute_forward_return(ep, p5, intent) if ep > 0 else (None, None)

            results.append(SignalBacktestResult(
                symbol        = sym,
                intent        = intent,
                signal_score  = sig["signal_score"],
                decision_date = date,
                entry_price   = entry_price,
                return_1d     = r1,
                return_3d     = r3,
                return_5d     = r5,
                correct_1d    = c1,
                correct_3d    = c3,
                correct_5d    = c5,
                source        = sig["source"],
            ))

        # ── Aggregate per symbol ──────────────────────────────────────────────
        by_symbol: dict[str, list[SignalBacktestResult]] = defaultdict(list)
        for r in results:
            by_symbol[r.symbol].append(r)

        summaries: dict[str, dict] = {}
        for sym, rlist in by_symbol.items():
            n         = len(rlist)
            avg_score = sum(r.signal_score for r in rlist) / n

            def _win_rate(attr: str) -> float:
                vals = [getattr(r, attr) for r in rlist if getattr(r, attr) is not None]
                return (sum(1 for v in vals if v) / len(vals)) if vals else 0.0

            def _avg_return(attr: str) -> float:
                vals = [getattr(r, attr) for r in rlist if getattr(r, attr) is not None]
                return (sum(vals) / len(vals)) if vals else 0.0

            wr1 = _win_rate("correct_1d")
            wr3 = _win_rate("correct_3d")
            wr5 = _win_rate("correct_5d")
            ar1 = _avg_return("return_1d")
            ar3 = _avg_return("return_3d")
            ar5 = _avg_return("return_5d")

            # Composite ranking: weighted win-rate + return contribution
            alpha_score = round((wr1 * 0.5 + wr3 * 0.3 + wr5 * 0.2) * 100 + ar1 * 100, 2)
            has_alpha   = wr1 > 0.55 and ar1 > 0.003

            summaries[sym] = {
                "n_signals":    n,
                "avg_score":    round(avg_score, 2),
                "win_rate_1d":  round(wr1, 4),
                "win_rate_3d":  round(wr3, 4),
                "win_rate_5d":  round(wr5, 4),
                "avg_return_1d": round(ar1, 6),
                "avg_return_3d": round(ar3, 6),
                "avg_return_5d": round(ar5, 6),
                "alpha_score":  alpha_score,
                "has_alpha":    has_alpha,
            }

        return {
            "status":       "ok",
            "n_signals":    len(signals),
            "n_symbols":    len(summaries),
            "lookback_days": lookback_days,
            "summaries":    summaries,
            "results": [
                {
                    "symbol":        r.symbol,
                    "intent":        r.intent,
                    "signal_score":  r.signal_score,
                    "decision_date": r.decision_date,
                    "entry_price":   r.entry_price,
                    "return_1d":     r.return_1d,
                    "return_3d":     r.return_3d,
                    "return_5d":     r.return_5d,
                    "correct_1d":    r.correct_1d,
                    "correct_3d":    r.correct_3d,
                    "correct_5d":    r.correct_5d,
                    "source":        r.source,
                }
                for r in results
            ],
        }

    except Exception as exc:
        log.warning("[BACKTEST] run_signal_backtest failed (non-fatal): %s", exc)
        return {}


# ── Formatting ────────────────────────────────────────────────────────────────

def format_backtest_report(result: dict) -> str:
    """
    Format backtest result dict as a markdown section for Agent 4.
    Returns a placeholder string on failure — never raises.
    """
    try:
        if not result:
            return "### Signal Backtest\nNo result available.\n"

        if result.get("status") == "insufficient_data":
            n     = result.get("n_signals", 0)
            min_r = result.get("min_required", MIN_SIGNALS)
            return (
                f"### Signal Backtest\n"
                f"Insufficient data: {n} signals found, minimum {min_r} required.\n"
                f"Backtest will be meaningful after n≥{min_r} confirmed fills.\n"
            )

        if result.get("status") != "ok":
            return "### Signal Backtest\nNo data available.\n"

        lines = [
            "### Signal Backtest",
            f"- Signals analyzed: {result['n_signals']} over "
            f"{result['lookback_days']} days",
            f"- Symbols with data: {result['n_symbols']}",
            "",
            "| Symbol | N | Score | WR-1d | WR-3d | WR-5d | AvgRet-1d | Alpha |",
            "|--------|---|-------|-------|-------|-------|-----------|-------|",
        ]

        summaries = result.get("summaries", {})
        sorted_syms = sorted(
            summaries.items(),
            key=lambda x: x[1].get("alpha_score", 0),
            reverse=True,
        )

        for sym, s in sorted_syms[:15]:
            alpha_flag = "✓" if s.get("has_alpha") else ""
            lines.append(
                f"| {sym:<6} | {s['n_signals']} | {s['avg_score']:.0f} "
                f"| {s['win_rate_1d']:.0%} | {s['win_rate_3d']:.0%} "
                f"| {s['win_rate_5d']:.0%} | {s['avg_return_1d']:+.2%} "
                f"| {alpha_flag} |"
            )

        near_miss_count = sum(
            1 for r in result.get("results", []) if r.get("source") == "near_miss"
        )
        if near_miss_count > 0:
            lines.append("")
            lines.append(f"**Shadow lane signals included:** {near_miss_count}")

        return "\n".join(lines) + "\n"

    except Exception as exc:
        log.debug("[BACKTEST] format_backtest_report failed: %s", exc)
        return "### Signal Backtest\nReport generation failed.\n"


# ── Persistence ───────────────────────────────────────────────────────────────

def save_backtest_results(result: dict) -> None:
    """
    Persist backtest results to data/reports/backtest_latest.json.
    Non-fatal.
    """
    try:
        if not result:
            return
        BACKTEST_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            **result,
        }
        BACKTEST_OUTPUT.write_text(json.dumps(payload, indent=2, default=str))
        log.info("[BACKTEST] Saved to %s", BACKTEST_OUTPUT)
    except Exception as exc:
        log.debug("[BACKTEST] save_backtest_results failed (non-fatal): %s", exc)
