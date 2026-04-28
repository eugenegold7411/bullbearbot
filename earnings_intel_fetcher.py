"""
earnings_intel_fetcher.py — Analyst intelligence fetcher for pre-earnings context.

Fetches and caches per-symbol analyst intelligence:
  - EPS beat history (last 4 quarters via yfinance)
  - Analyst consensus buy/hold/sell counts (Finnhub /stock/recommendation, free tier)
  - Price target mean + upside (yfinance ticker.info)
  - Recommendation mean score (yfinance ticker.info)

NOTE: Finnhub /stock/price-target returns HTTP 403 on free tier — not used.

Cache: data/earnings_intel/{SYM}_analyst_intel.json
TTL:  24 hours (refreshed by scheduler at 4:05 AM ET via _maybe_refresh_earnings_intel)

Non-fatal everywhere — all public functions return None / "" / empty dict on any failure.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from log_setup import get_logger

log = get_logger(__name__)

_BASE      = Path(__file__).parent
_CACHE_DIR = _BASE / "data" / "earnings_intel"
_CACHE_TTL_H = 24


def _cache_path(sym: str) -> Path:
    return _CACHE_DIR / f"{sym}_analyst_intel.json"


def _is_cache_fresh(path: Path, ttl_hours: int = _CACHE_TTL_H) -> bool:
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
        fetched_at = datetime.fromisoformat(data["fetched_at"])
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - fetched_at).total_seconds() / 3600
        return age_h < ttl_hours
    except Exception:
        return False


# ── yfinance ─────────────────────────────────────────────────────────────────

def _fetch_yfinance_intel(sym: str) -> dict:
    """Return beat history + analyst data from yfinance. Returns partial dict on failure."""
    result: dict = {}
    try:
        import yfinance as yf  # noqa: PLC0415
        ticker = yf.Ticker(sym)

        # Beat history from earnings_history (last 4 quarters)
        try:
            eh = ticker.earnings_history
            if eh is not None and not getattr(eh, "empty", True):
                rows = eh.dropna(subset=["epsActual", "epsEstimate"]).tail(4)
                total = len(rows)
                if total > 0:
                    beats = int((rows["epsActual"] > rows["epsEstimate"]).sum())
                    surp_col = "surprisePercent"
                    if surp_col in rows.columns:
                        # surprisePercent is decimal (0.03 == 3%) — multiply by 100
                        avg_surp = float(rows[surp_col].mean() * 100)
                    else:
                        valid = rows[rows["epsEstimate"] != 0]
                        if len(valid):
                            avg_surp = float(
                                ((valid["epsActual"] - valid["epsEstimate"])
                                 / valid["epsEstimate"].abs()).mean() * 100
                            )
                        else:
                            avg_surp = 0.0
                    result["beat_quarters"] = beats
                    result["total_quarters"] = total
                    result["avg_surprise_pct"] = round(avg_surp, 2)
        except Exception as exc:
            log.debug("[EIF] %s beat history failed: %s", sym, exc)

        # Analyst data from ticker.info
        try:
            info = ticker.info or {}
            pt_mean     = info.get("targetMeanPrice")
            analyst_cnt = info.get("numberOfAnalystOpinions")
            rec_mean    = info.get("recommendationMean")
            cur_price   = info.get("currentPrice") or info.get("regularMarketPrice")

            if pt_mean:
                result["price_target_mean"] = round(float(pt_mean), 2)
                if cur_price and float(cur_price) > 0:
                    upside = (float(pt_mean) / float(cur_price) - 1) * 100
                    result["price_target_upside_pct"] = round(upside, 1)
            if analyst_cnt:
                result["analyst_count"] = int(analyst_cnt)
            if rec_mean:
                result["rec_mean"] = round(float(rec_mean), 2)
        except Exception as exc:
            log.debug("[EIF] %s info failed: %s", sym, exc)

    except Exception as exc:
        log.debug("[EIF] yfinance fetch failed for %s: %s", sym, exc)

    return result


# ── Finnhub ──────────────────────────────────────────────────────────────────

def _fetch_finnhub_analyst(sym: str) -> dict:
    """Return Finnhub recommendation consensus for sym.
    Uses /stock/recommendation (works on free tier).
    Returns partial dict on failure or missing API key.
    """
    result: dict = {}
    api_key = os.getenv("FINNHUB_API_KEY", "")
    if not api_key:
        return result
    try:
        import requests  # noqa: PLC0415
        resp = requests.get(
            "https://finnhub.io/api/v1/stock/recommendation",
            params={"symbol": sym, "token": api_key},
            timeout=8,
        )
        if resp.status_code != 200:
            log.debug("[EIF] Finnhub recommendation %s HTTP %s", sym, resp.status_code)
            return result
        data = resp.json()
        if not data:
            return result
        rec = data[0]  # most recent period
        strong_buy  = rec.get("strongBuy", 0)
        buy         = rec.get("buy", 0)
        hold        = rec.get("hold", 0)
        sell        = rec.get("sell", 0)
        strong_sell = rec.get("strongSell", 0)
        total = strong_buy + buy + hold + sell + strong_sell
        if total > 0:
            result["finnhub_strong_buy"]  = strong_buy
            result["finnhub_buy"]         = buy
            result["finnhub_hold"]        = hold
            result["finnhub_sell"]        = sell
            result["finnhub_strong_sell"] = strong_sell
            result["finnhub_total"]       = total
            result["finnhub_bullish_pct"] = round((strong_buy + buy) / total * 100, 1)
    except Exception as exc:
        log.debug("[EIF] Finnhub analyst fetch failed for %s: %s", sym, exc)
    return result


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_analyst_intel(sym: str) -> Optional[dict]:
    """Fetch and merge analyst intel for sym from yfinance + Finnhub.
    Returns None if neither source yields any data.
    """
    yf_data = _fetch_yfinance_intel(sym)
    fh_data = _fetch_finnhub_analyst(sym)
    merged  = {**yf_data, **fh_data}
    if not merged:
        return None

    # Unified bullish_pct: prefer Finnhub analyst count, else approximate from rec_mean
    if "finnhub_bullish_pct" in merged:
        merged["bullish_pct"] = merged["finnhub_bullish_pct"]
        if "analyst_count" not in merged:
            merged["analyst_count"] = merged.get("finnhub_total")
    elif "rec_mean" in merged:
        # rec_mean: 1=Strong Buy, 5=Strong Sell → linear map to bullish_pct
        rec = merged["rec_mean"]
        merged["bullish_pct"] = round(max(0.0, min(100.0, (5.0 - rec) / 4.0 * 100.0)), 1)

    return merged


def load_analyst_intel_cached(sym: str) -> Optional[dict]:
    """Read analyst intel from cache file without making any network calls.
    Returns None if the cache file does not exist or is unreadable.
    """
    path = _cache_path(sym)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        log.debug("[EIF] cache read failed for %s: %s", sym, exc)
        return None


def refresh_earnings_analyst_intel(symbols: list[str]) -> None:
    """Batch-refresh analyst intel cache for all symbols with stale or missing entries.

    - Skips crypto symbols (contain '/')
    - Respects 24h TTL per symbol
    - Non-fatal per symbol — one failure never blocks the rest
    """
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    refreshed = 0
    for sym in symbols:
        if "/" in sym:
            continue
        path = _cache_path(sym)
        if _is_cache_fresh(path):
            continue
        try:
            intel = fetch_analyst_intel(sym) or {}
            record = {
                "symbol":     sym,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                **intel,
            }
            path.write_text(json.dumps(record, indent=2))
            refreshed += 1
            log.debug("[EIF] cached analyst intel for %s", sym)
        except Exception as exc:
            log.warning("[EIF] refresh failed for %s: %s", sym, exc)
    if refreshed:
        log.info("[EIF] analyst intel refreshed for %d symbols", refreshed)


def format_analyst_intel_text(intel: dict) -> str:
    """Format analyst intel dict into a compact string for prompt injection.
    Returns '' if intel has no usable content.
    """
    if not intel:
        return ""
    parts: list[str] = []

    # Beat history
    beats = intel.get("beat_quarters")
    total = intel.get("total_quarters")
    avg_s = intel.get("avg_surprise_pct")
    if beats is not None and total:
        surp = f" avg {avg_s:+.1f}% surprise" if avg_s is not None else ""
        parts.append(f"Beat: {beats}/{total}{surp}")

    # Analyst consensus + price target
    bullish  = intel.get("bullish_pct")
    count    = intel.get("analyst_count")
    pt_mean  = intel.get("price_target_mean")
    upside   = intel.get("price_target_upside_pct")
    rec_mean = intel.get("rec_mean")

    consensus: list[str] = []
    if bullish is not None and count:
        consensus.append(f"{bullish:.0f}% bullish ({count} analysts)")
    elif rec_mean is not None:
        if rec_mean <= 1.5:
            label = "Strong Buy"
        elif rec_mean <= 2.5:
            label = "Buy"
        elif rec_mean <= 3.5:
            label = "Hold"
        elif rec_mean <= 4.5:
            label = "Sell"
        else:
            label = "Strong Sell"
        consensus.append(f"{label} (rec={rec_mean:.1f}/5)")
    if pt_mean is not None:
        upside_s = f" {upside:+.1f}%" if upside is not None else ""
        consensus.append(f"PT ${pt_mean:.2f}{upside_s}")
    if consensus:
        parts.append("Consensus: " + ", ".join(consensus))

    return " | ".join(parts) if parts else ""
