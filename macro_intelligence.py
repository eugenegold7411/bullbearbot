"""
macro_intelligence.py — Persistent macro context for every trading cycle.

Fetches and caches rates, commodities, credit stress, and geopolitical data.
Integrates manually-maintained Citrini Research positions.

All fetches are non-fatal and cache-first. Log prefix: [MACRO_INTEL].
Cache TTL: 1 hour for market data, Citrini is never auto-overwritten.

Data directory: data/macro_intelligence/
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from log_setup import get_logger

log = get_logger(__name__)

_BASE_DIR  = Path(__file__).parent
_MACRO_DIR = _BASE_DIR / "data" / "macro_intelligence"
_WIRE_DIR  = _BASE_DIR / "data" / "macro_wire"

_CACHE_TTL_HOURS = 1


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _cache_valid(path: Path, ttl_hours: float = _CACHE_TTL_HOURS) -> bool:
    """Return True if cache file exists and is younger than ttl_hours."""
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
        ts = data.get("last_updated") or data.get("cached_at")
        if not ts:
            return False
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
        return age < ttl_hours * 3600
    except Exception:
        return False


def _write_cache(path: Path, data: dict) -> None:
    _MACRO_DIR.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(data, indent=2))
    except Exception as exc:
        log.debug("[MACRO_INTEL] cache write failed %s: %s", path.name, exc)


def _read_cache(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


# ── yfinance helper ───────────────────────────────────────────────────────────

def _yf_price_and_change(ticker: str, history_days: int = 25) -> tuple[float, float, float, float]:
    """
    Return (price, pct_1d, pct_5d, price_vs_20ma_pct) for a yfinance ticker.
    Returns (0.0, 0.0, 0.0, 0.0) on any failure.
    """
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period=f"{history_days}d", interval="1d", auto_adjust=True)
        if hist.empty or len(hist) < 2:
            return 0.0, 0.0, 0.0, 0.0
        closes = hist["Close"].dropna()
        price  = float(closes.iloc[-1])
        prev   = float(closes.iloc[-2])
        pct_1d = (price - prev) / prev * 100 if prev else 0.0
        pct_5d = (price - float(closes.iloc[-6])) / float(closes.iloc[-6]) * 100 \
                 if len(closes) >= 6 else 0.0
        ma20   = float(closes.tail(20).mean()) if len(closes) >= 20 else price
        vs_ma  = (price - ma20) / ma20 * 100 if ma20 else 0.0
        return price, round(pct_1d, 2), round(pct_5d, 2), round(vs_ma, 2)
    except Exception as exc:
        log.debug("[MACRO_INTEL] yfinance %s failed: %s", ticker, exc)
        return 0.0, 0.0, 0.0, 0.0


# ── 1. Rates snapshot ─────────────────────────────────────────────────────────

def fetch_rates_snapshot() -> dict:
    """
    Fetch US Treasury yields via yfinance.
    ^IRX = 13-week bill (proxy for 2Y short end)
    ^TNX = 10-year
    ^TYX = 30-year
    Returns cached result if <1h old.
    """
    _path = _MACRO_DIR / "rates.json"
    if _cache_valid(_path):
        cached = _read_cache(_path)
        if cached:
            return cached

    try:
        us2y_price,  _, _, _ = _yf_price_and_change("^IRX")
        us10y_price, _, _, _ = _yf_price_and_change("^TNX")
        us30y_price, _, _, _ = _yf_price_and_change("^TYX")

        # ^IRX is quoted as annualised % directly; ^TNX and ^TYX are percentage points
        us2y  = round(us2y_price  / 10 if us2y_price  > 20 else us2y_price,  3)
        us10y = round(us10y_price / 10 if us10y_price > 20 else us10y_price, 3)
        us30y = round(us30y_price / 10 if us30y_price > 20 else us30y_price, 3)

        spread_2s10s = round((us10y - us2y) * 100, 1)   # basis points
        spread_2s30s = round((us30y - us2y) * 100, 1)

        if spread_2s10s < -10:
            curve_shape = "inverted"
        elif abs(spread_2s10s) <= 10:
            curve_shape = "flat"
        else:
            curve_shape = "normal"

        result = {
            "us2y":          us2y,
            "us10y":         us10y,
            "us30y":         us30y,
            "spread_2s10s":  spread_2s10s,
            "spread_2s30s":  spread_2s30s,
            "curve_shape":   curve_shape,
            "last_updated":  datetime.now(timezone.utc).isoformat(),
        }
        _write_cache(_path, result)
        log.info("[MACRO_INTEL] Rates: 2Y=%.2f%% 10Y=%.2f%% 30Y=%.2f%% 2s10s=%+.0fbps [%s]",
                 us2y, us10y, us30y, spread_2s10s, curve_shape)
        return result

    except Exception as exc:
        log.warning("[MACRO_INTEL] fetch_rates_snapshot failed: %s", exc)
        return _read_cache(_path) or {}


# ── 2. Commodities snapshot ───────────────────────────────────────────────────

def fetch_commodities_snapshot() -> dict:
    """
    Fetch WTI oil, copper, gold, nat gas, DXY via yfinance.
    Returns cached result if <1h old.
    """
    _path = _MACRO_DIR / "commodities.json"
    if _cache_valid(_path):
        cached = _read_cache(_path)
        if cached:
            return cached

    tickers = {
        "wti":    "CL=F",
        "copper": "HG=F",
        "gold":   "GC=F",
        "natgas": "NG=F",
        "dxy":    "DX-Y.NYB",
    }

    result: dict = {"last_updated": datetime.now(timezone.utc).isoformat()}
    try:
        for key, ticker in tickers.items():
            price, pct_1d, pct_5d, vs_ma = _yf_price_and_change(ticker)
            ma_label = "above_20ma" if vs_ma > 0 else "below_20ma"
            result[key] = {
                "price":    round(price, 4),
                "pct_1d":   pct_1d,
                "pct_5d":   pct_5d,
                "vs_20ma":  vs_ma,
                "ma_label": ma_label,
            }
        _write_cache(_path, result)
        log.info("[MACRO_INTEL] Commodities: WTI=$%.2f (%+.1f%%)  "
                 "Copper=$%.3f (%+.1f%%)  Gold=$%.0f (%+.1f%%)",
                 result.get("wti",   {}).get("price", 0),
                 result.get("wti",   {}).get("pct_1d", 0),
                 result.get("copper",{}).get("price", 0),
                 result.get("copper",{}).get("pct_1d", 0),
                 result.get("gold",  {}).get("price", 0),
                 result.get("gold",  {}).get("pct_1d", 0))
        return result
    except Exception as exc:
        log.warning("[MACRO_INTEL] fetch_commodities_snapshot failed: %s", exc)
        return _read_cache(_path) or {}


# ── 3. Credit stress snapshot ─────────────────────────────────────────────────

def fetch_credit_snapshot() -> dict:
    """
    Compute HYG/LQD ratio as a credit stress proxy.
    A falling ratio → credit spreads widening → risk-off signal.
    Returns cached result if <1h old.
    """
    _path = _MACRO_DIR / "credit.json"
    if _cache_valid(_path):
        cached = _read_cache(_path)
        if cached:
            return cached

    try:
        import yfinance as yf
        hyg_hist = yf.Ticker("HYG").history(period="25d", interval="1d", auto_adjust=True)
        lqd_hist = yf.Ticker("LQD").history(period="25d", interval="1d", auto_adjust=True)

        if hyg_hist.empty or lqd_hist.empty:
            raise ValueError("HYG or LQD history empty")

        hyg_closes = hyg_hist["Close"].dropna()
        lqd_closes = lqd_hist["Close"].dropna()
        min_len = min(len(hyg_closes), len(lqd_closes))
        if min_len < 2:
            raise ValueError("Not enough data")

        hyg_c = hyg_closes.iloc[-min_len:]
        lqd_c = lqd_closes.iloc[-min_len:]
        ratio_series = hyg_c.values / lqd_c.values

        ratio_now  = float(ratio_series[-1])
        ratio_prev = float(ratio_series[-2])
        ratio_ma20 = float(ratio_series[-20:].mean()) if min_len >= 20 else ratio_now
        ratio_1d_pct = (ratio_now - ratio_prev) / ratio_prev * 100 if ratio_prev else 0.0
        vs_ma = (ratio_now - ratio_ma20) / ratio_ma20 * 100 if ratio_ma20 else 0.0

        if vs_ma < -1.5:
            stress_label = "wide"
        elif vs_ma > 1.5:
            stress_label = "tight"
        else:
            stress_label = "normal"

        result = {
            "hyg_lqd_ratio":      round(ratio_now, 4),
            "ratio_1d_pct":       round(ratio_1d_pct, 2),
            "ratio_vs_20ma_pct":  round(vs_ma, 2),
            "stress_label":       stress_label,
            "last_updated":       datetime.now(timezone.utc).isoformat(),
        }
        _write_cache(_path, result)
        log.info("[MACRO_INTEL] Credit: HYG/LQD=%.4f (%+.2f%%) [%s]",
                 ratio_now, ratio_1d_pct, stress_label)
        return result

    except Exception as exc:
        log.warning("[MACRO_INTEL] fetch_credit_snapshot failed: %s", exc)
        return _read_cache(_path) or {}


# ── 4. Citrini positions (manual) ─────────────────────────────────────────────

def load_citrini_positions() -> dict:
    """
    Load manually-maintained Citrini Research macro positions.
    File: data/macro_intelligence/citrini_positions.json
    Written by ingest_citrini_memo.py — never auto-overwritten here.
    Returns empty dict if file doesn't exist.
    """
    _path = _MACRO_DIR / "citrini_positions.json"
    if not _path.exists():
        return {}
    try:
        return json.loads(_path.read_text())
    except Exception as exc:
        log.debug("[MACRO_INTEL] citrini_positions.json read failed: %s", exc)
        return {}


# ── 5. Geopolitical score ─────────────────────────────────────────────────────

def get_geopolitical_score() -> dict:
    """
    Scan data/macro_wire/significant_events.jsonl for recent critical/high events.
    Score: 0-10 based on event count and severity in last 7 days.
    """
    _events_file = _WIRE_DIR / "significant_events.jsonl"
    if not _events_file.exists():
        return {"score": 0, "dominant_theme": "unavailable", "recent_events_count": 0}

    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        events: list[dict] = []
        for line in _events_file.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            ts_str = ev.get("timestamp") or ev.get("ts") or ev.get("date") or ""
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    from datetime import timezone as _tz
                    ts = ts.replace(tzinfo=_tz.utc)
                if ts >= cutoff:
                    events.append(ev)
            except Exception:
                continue

        if not events:
            return {"score": 0, "dominant_theme": "quiet", "recent_events_count": 0}

        severity_weights = {"critical": 3, "high": 2, "medium": 1, "low": 0}
        raw_score = sum(
            severity_weights.get(str(ev.get("severity", ev.get("impact", ""))).lower(), 1)
            for ev in events
        )
        score = min(10, raw_score)

        # Find dominant theme from categories/tags
        theme_counts: dict[str, int] = {}
        for ev in events:
            for field in ("category", "theme", "region", "type"):
                val = str(ev.get(field, "")).strip()
                if val and val not in ("", "unknown"):
                    theme_counts[val] = theme_counts.get(val, 0) + 1
        dominant = max(theme_counts, key=theme_counts.get) if theme_counts else "geopolitical"

        return {
            "score":                score,
            "dominant_theme":       dominant,
            "recent_events_count":  len(events),
        }

    except Exception as exc:
        log.debug("[MACRO_INTEL] get_geopolitical_score failed: %s", exc)
        return {"score": 0, "dominant_theme": "unavailable", "recent_events_count": 0}


# ── 6. Master section builder ─────────────────────────────────────────────────

def build_macro_backdrop_section() -> str:
    """
    Build the === MACRO BACKDROP (PERSISTENT) === section for Claude's prompt.
    Cache-first for all sub-fetches. Never raises — returns "" on total failure.
    """
    try:
        rates      = fetch_rates_snapshot()
        comms      = fetch_commodities_snapshot()
        credit     = fetch_credit_snapshot()
        citrini    = load_citrini_positions()
        geo        = get_geopolitical_score()

        lines: list[str] = ["=== MACRO BACKDROP (PERSISTENT) ==="]

        # Rates
        if rates:
            us2y  = rates.get("us2y",  0)
            us10y = rates.get("us10y", 0)
            us30y = rates.get("us30y", 0)
            s2s10 = rates.get("spread_2s10s", 0)
            s2s30 = rates.get("spread_2s30s", 0)
            shape = rates.get("curve_shape", "?")
            lines.append(
                f"Rates:  2Y={us2y:.2f}%  10Y={us10y:.2f}%  30Y={us30y:.2f}%"
            )
            lines.append(
                f"        2s10s={s2s10:+.0f}bps [{shape}]  2s30s={s2s30:+.0f}bps"
            )
        else:
            lines.append("Rates:  (unavailable)")

        # Commodities
        if comms:
            wti = comms.get("wti",    {})
            cop = comms.get("copper", {})
            gld = comms.get("gold",   {})
            ng  = comms.get("natgas", {})
            dxy = comms.get("dxy",    {})

            lines.append(
                f"Commodities: WTI=${wti.get('price',0):.2f} ({wti.get('pct_1d',0):+.1f}%)  "
                f"Copper=${cop.get('price',0):.3f} ({cop.get('pct_1d',0):+.1f}%)  "
                f"Gold=${gld.get('price',0):,.0f} ({gld.get('pct_1d',0):+.1f}%)  "
                f"NatGas=${ng.get('price',0):.3f} ({ng.get('pct_1d',0):+.1f}%)"
            )

            dxy_price = dxy.get("price", 0)
            dxy_pct   = dxy.get("pct_1d", 0)
            dxy_vs_ma = dxy.get("vs_20ma", 0)
            if dxy_vs_ma > 1.0:
                dxy_label = "strong"
            elif dxy_vs_ma < -1.0:
                dxy_label = "weak"
            else:
                dxy_label = "neutral"
            lines.append(
                f"Dollar (DXY): {dxy_price:.1f} ({dxy_pct:+.1f}%) [{dxy_label}]"
            )
        else:
            lines.append("Commodities: (unavailable)")

        # Credit
        if credit:
            ratio  = credit.get("hyg_lqd_ratio", 0)
            pct_1d = credit.get("ratio_1d_pct", 0)
            stress = credit.get("stress_label", "unknown")
            lines.append(f"Credit: HYG/LQD={ratio:.4f} ({pct_1d:+.2f}%) [{stress} spreads]")
        else:
            lines.append("Credit: (unavailable)")

        # Geopolitical
        geo_score = geo.get("score", 0)
        geo_theme = geo.get("dominant_theme", "quiet")
        lines.append(f"Geopolitical score: {geo_score}/10 — {geo_theme}")

        # Citrini Research
        if citrini:
            memo_date  = citrini.get("memo_date", "")
            memo_title = citrini.get("memo_title", "")
            header     = "Citrini Research"
            if memo_date or memo_title:
                header += f" — {memo_title or ''} ({memo_date or 'date unknown'})"
            lines.append("")
            lines.append(header + ":")

            macro_view = citrini.get("macro_view", {})
            if macro_view:
                lines.append(f"  Macro view: growth={macro_view.get('us_growth','?')}  "
                              f"rates={macro_view.get('rates_view','?')}  "
                              f"dollar={macro_view.get('dollar_view','?')}")
                risks = macro_view.get("key_risks", [])
                if risks:
                    lines.append(f"  Key risks: {', '.join(risks[:3])}")

            active_trades = [t for t in citrini.get("active_trades", []) if t.get("active")]
            if active_trades:
                lines.append("  ACTIVE TRADES:")
                for t in active_trades[:6]:
                    sym = t.get("symbol", "?")
                    direction = t.get("direction", "?").upper()
                    thesis = t.get("thesis_summary", "")[:80]
                    lines.append(f"    {sym} [{direction}]: {thesis}")

            themes = citrini.get("watchlist_themes", [])
            if themes:
                lines.append("  WATCHLIST THEMES:")
                for th in themes[:4]:
                    name     = th.get("theme", "?")
                    syms     = ", ".join(th.get("symbols", [])[:4])
                    rationale = th.get("rationale", "")[:60]
                    lines.append(f"    {name}: {syms} — {rationale}")
        else:
            lines.append("")
            lines.append("Citrini Research: (no memo loaded — run ingest_citrini_memo.py)")

        return "\n".join(lines)

    except Exception as exc:
        log.warning("[MACRO_INTEL] build_macro_backdrop_section failed: %s", exc)
        return ""


# ── Regime classifier inputs ──────────────────────────────────────────────────

def get_regime_macro_inputs() -> dict:
    """
    Return a compact dict of macro inputs for the regime classifier Haiku call.
    Always returns a dict (may be empty on failure). Cache-first.
    """
    try:
        rates  = fetch_rates_snapshot()
        comms  = fetch_commodities_snapshot()
        credit = fetch_credit_snapshot()

        dxy    = comms.get("dxy", {})
        dxy_vs = dxy.get("vs_20ma", 0)
        dollar = "strong" if dxy_vs > 1.0 else ("weak" if dxy_vs < -1.0 else "neutral")

        wti_pct = comms.get("wti",    {}).get("pct_5d", 0)
        cop_pct = comms.get("copper", {}).get("pct_5d", 0)
        gld_pct = comms.get("gold",   {}).get("pct_5d", 0)
        avg_com = (wti_pct + cop_pct) / 2
        commodity_trend = "bullish" if avg_com > 1.0 else ("bearish" if avg_com < -1.0 else "neutral")

        return {
            "rates_summary":    (f"2Y={rates.get('us2y',0):.2f}%  "
                                 f"10Y={rates.get('us10y',0):.2f}%  "
                                 f"2s10s={rates.get('spread_2s10s',0):+.0f}bps"
                                 f" [{rates.get('curve_shape','?')}]"),
            "commodity_trend":  commodity_trend,
            "credit_stress":    credit.get("stress_label", "normal"),
            "dollar_trend":     dollar,
            "gold_5d_pct":      gld_pct,
        }
    except Exception as exc:
        log.debug("[MACRO_INTEL] get_regime_macro_inputs failed: %s", exc)
        return {}
