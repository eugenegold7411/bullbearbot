"""
data_warehouse.py — daily data fetch and cache for all CORE watchlist symbols.

Runs at 4:00 AM ET daily. Saves to data/ subdirectories.
market_data.py reads from cache when fresh (< 26h), fetches live otherwise.

Usage:
    python data_warehouse.py              # full refresh
    python data_warehouse.py --symbol NVDA  # single symbol refresh
"""

import argparse
import csv
import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf
from alpaca.data.enums import DataFeed
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import (
    NewsRequest,
    StockBarsRequest,
)
from alpaca.data.timeframe import TimeFrame
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except ImportError:
    pass

import watchlist_manager as wm
from log_setup import get_logger
log = get_logger(__name__)
ET  = ZoneInfo("America/New_York")

DATA     = Path(__file__).parent / "data"
BARS_DIR         = DATA / "bars"
FUND_DIR         = DATA / "fundamentals"
NEWS_DIR         = DATA / "news"
OPT_DIR          = DATA / "options"
MARKET_DIR       = DATA / "market"
ARCHIVE_DIR      = DATA / "archive"
CRYPTO_DIR       = DATA / "crypto" 

_data:    StockHistoricalDataClient | None = None
_crypto:  CryptoHistoricalDataClient | None = None
_news_cl: NewsClient | None = None


def _build_data_client() -> StockHistoricalDataClient:
    key    = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise EnvironmentError(
            "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set to use data_warehouse"
        )
    return StockHistoricalDataClient(key, secret)


def _get_data_client() -> StockHistoricalDataClient:
    global _data
    if _data is None:
        _data = _build_data_client()
    return _data


def _build_crypto_client() -> CryptoHistoricalDataClient:
    key    = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise EnvironmentError(
            "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set to use data_warehouse"
        )
    return CryptoHistoricalDataClient(key, secret)


def _get_crypto_client() -> CryptoHistoricalDataClient:
    global _crypto
    if _crypto is None:
        _crypto = _build_crypto_client()
    return _crypto


def _build_news_client() -> NewsClient:
    key    = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise EnvironmentError(
            "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set to use data_warehouse"
        )
    return NewsClient(key, secret)


def _get_news_client() -> NewsClient:
    global _news_cl
    if _news_cl is None:
        _news_cl = _build_news_client()
    return _news_cl


# ── Helpers ───────────────────────────────────────────────────────────────────

def _today() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def _is_fresh(path: Path, max_age_hours: float = 26.0) -> bool:
    if not path.exists():
        return False
    age = (datetime.now(timezone.utc).timestamp() - path.stat().st_mtime) / 3600
    return age < max_age_hours


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))


def _archive(filename: str, data) -> None:
    """Copy today's snapshot to archive/YYYY-MM-DD/."""
    dst = ARCHIVE_DIR / _today() / filename
    _save_json(dst, data)


# ── Bars ──────────────────────────────────────────────────────────────────────

def refresh_bars(symbols: list[str]) -> None:
    """Fetch 90 days of daily OHLCV for stock/ETF symbols, save to CSV."""
    log.info("Refreshing bars for %d symbols", len(symbols))
    now   = datetime.now(timezone.utc)
    start = now - timedelta(days=90)
    BARS_DIR.mkdir(parents=True, exist_ok=True)

    for i in range(0, len(symbols), 50):
        batch = symbols[i:i+50]
        try:
            resp = _get_data_client().get_stock_bars(StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame.Day,
                start=start, end=now,
                feed=DataFeed.IEX,
            ))
            for sym in batch:
                try:
                    bars = resp[sym]
                    if not bars:
                        continue
                    rows = [{
                        "date":   b.timestamp.strftime("%Y-%m-%d"),
                        "open":   b.open, "high": b.high,
                        "low":    b.low,  "close": b.close,
                        "volume": b.volume,
                    } for b in bars]
                    pd.DataFrame(rows).to_csv(BARS_DIR / f"{sym}_daily.csv", index=False)
                except Exception as e:
                    log.debug("Bars save failed %s: %s", sym, e)
        except Exception as exc:
            log.warning("Bars fetch batch error: %s", exc)


def load_bars_cached(symbol: str) -> list[dict] | None:
    """Return cached bars as list of dicts, or None if stale/missing."""
    path = BARS_DIR / f"{symbol}_daily.csv"
    if not _is_fresh(path):
        return None
    try:
        df = pd.read_csv(path)
        return df.to_dict("records")
    except Exception:
        return None


# ── ETF symbol set ────────────────────────────────────────────────────────────

# ETFs do not have quoteSummary data on yfinance — skip them in refresh_fundamentals.
# Primary source is watchlist_manager; this fallback covers CLI single-symbol runs
# and any watchlist-manager import failures.
_ETF_SYMBOLS_FALLBACK: frozenset[str] = frozenset({
    "XLE", "USO", "GLD", "SLV", "XLF", "XRT", "ITA", "XBI",
    "EWJ", "FXI", "QQQ", "IWM", "EEM", "COPX", "EWM", "ECH",
    "VXX", "TLT", "SPY", "BITO", "IBB", "XLK", "XLV", "XLY",
    "XLP", "XLI", "XLU", "XLB", "XLRE",
})


def _get_etf_symbols() -> frozenset[str]:
    """Return current ETF symbol set from active watchlist, falling back to static set."""
    try:
        return frozenset(wm.get_active_watchlist().get("etfs", []))
    except Exception:
        return _ETF_SYMBOLS_FALLBACK


# ── Fundamentals ──────────────────────────────────────────────────────────────

def refresh_fundamentals(symbols: list[str]) -> None:
    """Fetch P/E, market cap, 52w high/low via yfinance. Skips ETFs (no quoteSummary)."""
    FUND_DIR.mkdir(parents=True, exist_ok=True)
    etf_symbols    = _get_etf_symbols()
    equity_symbols = [s for s in symbols if s not in etf_symbols]
    skipped        = len(symbols) - len(equity_symbols)
    log.info("Refreshing fundamentals for %d equity symbols (%d ETF skipped)",
             len(equity_symbols), skipped)

    for sym in equity_symbols:
        try:
            info = yf.Ticker(sym).info
            data = {
                "symbol":       sym,
                "fetched_at":   datetime.now(ET).isoformat(),
                "market_cap":   info.get("marketCap"),
                "pe_ratio":     info.get("trailingPE"),
                "fwd_pe":       info.get("forwardPE"),
                "52w_high":     info.get("fiftyTwoWeekHigh"),
                "52w_low":      info.get("fiftyTwoWeekLow"),
                "avg_volume":   info.get("averageVolume"),
                "sector":       info.get("sector"),
                "industry":     info.get("industry"),
                "short_name":   info.get("shortName"),
                "dividend_yield": info.get("dividendYield"),
            }
            _save_json(FUND_DIR / f"{sym}.json", data)
        except Exception as exc:
            log.debug("Fundamentals failed %s: %s", sym, exc)


# ── News ──────────────────────────────────────────────────────────────────────

def refresh_news(symbols: list[str]) -> None:
    """Fetch last 24h news per symbol group."""
    NEWS_DIR.mkdir(parents=True, exist_ok=True)
    # Fetch in batches of 20
    for i in range(0, len(symbols), 20):
        batch = [s for s in symbols[i:i+20] if "/" not in s]
        if not batch:
            continue
        try:
            resp     = _get_news_client().get_news(NewsRequest(
                symbols=",".join(batch), limit=50, sort="desc"
            ))
            articles_by_sym: dict[str, list] = {s: [] for s in batch}
            articles = []
            for item in resp:
                if isinstance(item, tuple) and len(item) == 2:
                    payload = item[1]
                    if isinstance(payload, dict) and "news" in payload:
                        articles.extend(payload["news"])
            for a in articles:
                for sym in (a.symbols or []):
                    if sym in articles_by_sym:
                        articles_by_sym[sym].append({
                            "headline": a.headline,
                            "url":      a.url,
                            "created_at": str(a.created_at),
                            "symbols":  list(a.symbols or []),
                        })
            for sym, arts in articles_by_sym.items():
                _save_json(NEWS_DIR / f"{sym}_news.json", {
                    "symbol": sym, "fetched_at": datetime.now(ET).isoformat(),
                    "articles": arts,
                })
        except Exception as exc:
            log.warning("News refresh batch error: %s", exc)


# ── Symbol-specific news caches ───────────────────────────────────────────────

_YAHOO_SYMBOL_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"
_SYMBOL_NEWS_TTL_MIN = 30  # minutes — skip re-fetch if cache is fresher than this


def refresh_yahoo_symbol_news(symbols: list[str]) -> None:
    """Fetch Yahoo Finance RSS headlines per symbol, save to data/news/{SYM}_yahoo_news.json.

    Respects a 30-minute TTL so repeated intra-day calls are cheap.
    Skips crypto ("/") symbols. Non-fatal per symbol.
    """
    import feedparser  # noqa: PLC0415 — optional, same dep as macro_wire

    NEWS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    cutoff_stale = now - timedelta(minutes=_SYMBOL_NEWS_TTL_MIN)

    for sym in symbols:
        if "/" in sym:
            continue
        out_path = NEWS_DIR / f"{sym}_yahoo_news.json"
        # TTL check — skip if cache is fresh enough
        try:
            if out_path.exists():
                cached = json.loads(out_path.read_text())
                fetched_at_str = cached.get("fetched_at", "")
                if fetched_at_str:
                    fetched_at = datetime.fromisoformat(fetched_at_str)
                    if fetched_at.tzinfo is None:
                        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
                    if fetched_at > cutoff_stale:
                        continue
        except Exception:
            pass

        try:
            url  = _YAHOO_SYMBOL_RSS.format(symbol=sym)
            feed = feedparser.parse(url)
            articles = []
            for entry in feed.entries[:20]:
                headline = (entry.get("title") or "").strip()
                if not headline:
                    continue
                pub = entry.get("published_parsed") or entry.get("updated_parsed")
                pub_str = ""
                if pub:
                    try:
                        pub_str = datetime(*pub[:6], tzinfo=timezone.utc).isoformat()
                    except Exception:
                        pass
                articles.append({
                    "headline":    headline,
                    "url":         entry.get("link", ""),
                    "published_at": pub_str,
                    "source":      "yahoo_rss",
                })
            _save_json(out_path, {
                "symbol":     sym,
                "fetched_at": now.isoformat(),
                "articles":   articles,
            })
            log.debug("Yahoo symbol news: %s → %d articles", sym, len(articles))
        except Exception as exc:
            log.debug("Yahoo symbol news fetch failed for %s: %s", sym, exc)


def refresh_finnhub_news(symbols: list[str]) -> None:
    """Fetch Finnhub company news per symbol, save to data/news/{SYM}_finnhub_news.json.

    Gated by feature flag `enable_finnhub_news` (default False).
    Requires FINNHUB_API_KEY. Non-fatal per symbol.
    """
    try:
        from feature_flags import is_enabled  # noqa: PLC0415
        if not is_enabled("enable_finnhub_news"):
            log.debug("refresh_finnhub_news: feature flag enable_finnhub_news is off — skipping")
            return
    except Exception:
        return

    finnhub_key = os.getenv("FINNHUB_API_KEY")
    if not finnhub_key:
        log.debug("refresh_finnhub_news: FINNHUB_API_KEY not set — skipping")
        return

    NEWS_DIR.mkdir(parents=True, exist_ok=True)
    today      = datetime.now(ET).date()
    from_date  = (today - timedelta(days=3)).strftime("%Y-%m-%d")
    to_date    = today.strftime("%Y-%m-%d")

    for sym in symbols:
        if "/" in sym:
            continue
        try:
            url  = "https://finnhub.io/api/v1/company-news"
            resp = requests.get(
                url,
                params={"symbol": sym, "from": from_date, "to": to_date, "token": finnhub_key},
                timeout=10,
            )
            if resp.status_code != 200:
                log.debug("Finnhub news %s: HTTP %d", sym, resp.status_code)
                continue
            raw = resp.json()
            if not isinstance(raw, list):
                continue
            articles = [
                {
                    "headline":    a.get("headline", ""),
                    "url":         a.get("url", ""),
                    "published_at": datetime.fromtimestamp(
                        a.get("datetime", 0), tz=timezone.utc
                    ).isoformat() if a.get("datetime") else "",
                    "source":      "finnhub",
                }
                for a in raw[:50]
                if a.get("headline")
            ]
            _save_json(NEWS_DIR / f"{sym}_finnhub_news.json", {
                "symbol":     sym,
                "fetched_at": datetime.now(ET).isoformat(),
                "articles":   articles,
            })
            log.debug("Finnhub news: %s → %d articles", sym, len(articles))
        except Exception as exc:
            log.debug("Finnhub news fetch failed for %s: %s", sym, exc)


# ── Market snapshots ──────────────────────────────────────────────────────────

def refresh_sector_performance() -> None:
    """Fetch sector ETF performance."""
    sectors = {
        "technology":   "XLK",
        "energy":       "XLE",
        "financials":   "XLF",
        "health":       "XLV",
        "consumer_disc":"XLY",
        "consumer_stap":"XLP",
        "industrials":  "XLI",
        "utilities":    "XLU",
        "materials":    "XLB",
        "real_estate":  "XLRE",
        "defense":      "ITA",
        "biotech":      "XBI",
        "crypto":       "BITO",
    }
    result = {}
    for sector, etf in sectors.items():
        try:
            hist = yf.Ticker(etf).history(period="5d")
            if len(hist) >= 2:
                today_close = float(hist["Close"].iloc[-1])
                prev_close  = float(hist["Close"].iloc[-2])
                chg_pct     = (today_close - prev_close) / prev_close * 100
                week_chg    = (today_close - float(hist["Close"].iloc[0])) / float(hist["Close"].iloc[0]) * 100
                result[sector] = {
                    "etf": etf, "close": round(today_close, 2),
                    "day_chg": round(chg_pct, 2),
                    "week_chg": round(week_chg, 2),
                    "momentum": "up" if chg_pct > 0.5 else ("down" if chg_pct < -0.5 else "flat"),
                }
        except Exception:
            pass

    data = {"fetched_at": datetime.now(ET).isoformat(), "sectors": result}
    _save_json(MARKET_DIR / "sector_perf.json", data)
    _archive("sector_perf.json", data)
    log.info("Sector performance saved (%d sectors)", len(result))


def refresh_macro_snapshot() -> None:
    """VIX, oil, gold, dollar index snapshot."""
    tickers = {"vix": "^VIX", "oil": "CL=F", "gold": "GC=F",
               "dollar": "DX-Y.NYB", "sp500": "^GSPC", "nasdaq": "^IXIC"}
    snap = {"fetched_at": datetime.now(ET).isoformat()}
    for key, sym in tickers.items():
        try:
            hist = yf.Ticker(sym).history(period="2d")
            if not hist.empty:
                price = float(hist["Close"].iloc[-1])
                prev  = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else price
                snap[key] = {
                    "price":   round(price, 2),
                    "chg_pct": round((price - prev) / prev * 100, 2) if prev else 0,
                }
        except Exception:
            pass

    _save_json(MARKET_DIR / "macro_snapshot.json", snap)
    _archive("macro_snapshot.json", snap)
    log.info("Macro snapshot saved")


# ── Earnings calendar — Alpha Vantage exclusive writer ────────────────────────

def refresh_earnings_calendar() -> None:
    """
    Deprecated no-op. The yfinance writer was removed when AV became the
    exclusive source. This stub is kept so external callers and tests that
    monkey-patch the symbol still work. Use refresh_earnings_calendar_av().
    """
    log.debug("[EARNINGS] refresh_earnings_calendar() called — deprecated no-op (use AV writer)")
    return None



_EXTRA_TRACKED_UNIVERSE: frozenset[str] = frozenset({
    # Names not in core watchlist but still tracked for rotation/A2 universe
    "AAPL", "META", "GOOGL", "AMD", "NFLX", "CRM", "ORCL", "ADBE", "NOW",
    "WDAY", "ZM", "V", "MA", "PYPL", "SQ", "AFRM", "UPST", "SOFI", "HOOD",
    "BAC", "C", "WFC", "GE", "CAT", "DE", "BA", "UNH", "UBER", "LYFT",
    "ABNB", "DASH", "COIN", "MSTR", "SHOP", "DDOG", "NET", "CRWD", "OKTA",
    "ZS", "TEAM", "MDB", "ESTC", "U", "RBLX", "ARM", "SMCI", "MRVL", "QCOM",
    "MU", "INTC", "TXN", "AMAT", "KLAC", "LRCX", "ONTO", "ENTG", "SNAP",
    "SPOT", "RDFN", "Z", "OPEN", "CVNA", "RIVN", "LCID", "NIO", "XPEV",
    "LI", "BIDU", "JD", "PDD", "BABA", "SE", "GRAB", "GOTO", "TSLA",
})


def _get_tracked_universe() -> set[str]:
    """
    Return the symbol set we filter the AV calendar against.
    Union of: watchlist (core + rotation) + A2 universe + extras.
    """
    syms: set[str] = set()
    try:
        wl = wm.get_active_watchlist()
        for s in wl.get("stocks", []) + wl.get("etfs", []):
            if isinstance(s, str) and "/" not in s:
                syms.add(s.upper())
    except Exception as exc:
        log.debug("[EARNINGS_AV] watchlist load failed (non-fatal): %s", exc)

    # A2 options universe (whatever has IV history)
    try:
        from options_universe_manager import get_universe  # noqa: PLC0415
        u = get_universe()
        for k in (u.get("symbols") or {}):
            syms.add(k.upper())
    except Exception:
        pass

    syms |= _EXTRA_TRACKED_UNIVERSE
    return syms


def refresh_earnings_calendar_av() -> dict:
    """
    Fetch the 3-month earnings calendar from Alpha Vantage.
    AV is the exclusive source — yfinance does NOT write earnings_calendar.json.

    Filters to _get_tracked_universe() with a core invariant: any core stock
    present in raw AV data is force-added even if outside the tracked universe.

    Merge semantics:
      - Past-date entries (< today) are dropped from any prior file.
      - On reschedule (existing entry has a different date), preserves
        prior_reported_date.

    Returns the saved dict ({} on any error or empty CSV).
    Non-fatal: HTTP failures leave any existing file in place.
    """
    api_key = os.getenv("ALPHA_VANTAGE_API_KEY") or ""
    if not api_key:
        log.warning("[EARNINGS_AV] ALPHA_VANTAGE_API_KEY not set — skipping refresh")
        return {}

    # ── Fetch ─────────────────────────────────────────────────────────────────
    url = f"https://www.alphavantage.co/query?function=EARNINGS_CALENDAR&horizon=3month&apikey={api_key}"
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        body = resp.text or ""
    except Exception as exc:
        log.warning("[EARNINGS_AV] HTTP fetch failed (non-fatal): %s", exc)
        return {}

    # ── Parse CSV ─────────────────────────────────────────────────────────────
    rows: list[dict] = []
    try:
        reader = csv.DictReader(io.StringIO(body))
        for row in reader:
            sym = (row.get("symbol") or "").strip().upper()
            if not sym:
                continue
            rows.append(row)
    except Exception as exc:
        log.warning("[EARNINGS_AV] CSV parse failed (non-fatal): %s", exc)
        return {}

    if not rows:
        log.warning("[EARNINGS_AV] CSV had 0 rows — leaving existing file in place")
        return {}

    # ── Filter to tracked universe + force-add core invariants ────────────────
    tracked = _get_tracked_universe()
    core_stocks: set[str] = set()
    try:
        for e in (wm.get_core() or []):
            if isinstance(e, dict) and e.get("type") == "stock":
                sym = (e.get("symbol") or "").upper()
                if sym:
                    core_stocks.add(sym)
    except Exception:
        pass

    today = datetime.now().date()

    def _parse_eps(v) -> float | None:
        try:
            return float(v) if v not in (None, "") else None
        except Exception:
            return None

    # Load existing for prior_reported_date and past-date drop
    cal_path = MARKET_DIR / "earnings_calendar.json"
    existing_by_sym: dict[str, dict] = {}
    if cal_path.exists():
        try:
            prior = json.loads(cal_path.read_text())
            for e in prior.get("calendar", []):
                sym = (e.get("symbol") or "").upper()
                if not sym:
                    continue
                iso = str(e.get("earnings_date", ""))[:10]
                # Drop past dates from prior
                try:
                    if iso and datetime.fromisoformat(iso).date() < today:
                        continue
                except Exception:
                    continue
                existing_by_sym[sym] = e
        except Exception as exc:
            log.debug("[EARNINGS_AV] prior file unreadable (%s) — proceeding fresh", exc)

    confirmed_iso = datetime.now(timezone.utc).isoformat()
    now_iso       = datetime.now(ET).isoformat()
    new_entries:   list[dict] = []
    seen_keys:     set[tuple[str, str]] = set()

    for row in rows:
        sym = (row.get("symbol") or "").strip().upper()
        if not sym:
            continue
        # Filter: tracked-universe OR core invariant
        if sym not in tracked and sym not in core_stocks:
            continue
        iso = str(row.get("reportDate") or "")[:10]
        if not iso:
            continue
        try:
            d = datetime.fromisoformat(iso).date()
            if d < today:
                continue
        except Exception:
            continue
        if (sym, iso) in seen_keys:
            continue
        seen_keys.add((sym, iso))

        timing = (row.get("timeOfTheDay") or "").strip() or "unknown"
        eps    = _parse_eps(row.get("estimate"))

        entry = {
            "symbol":              sym,
            "earnings_date":       iso,
            "timing":              timing,
            "eps_estimate":        eps,
            "source":              "alphavantage",
            "source_confirmed_at": confirmed_iso,
        }

        # Reschedule detection
        prior = existing_by_sym.get(sym)
        if prior:
            prior_iso = str(prior.get("earnings_date", ""))[:10]
            if prior_iso and prior_iso != iso:
                entry["prior_reported_date"] = prior_iso

        new_entries.append(entry)

    # Sort by date asc
    new_entries.sort(key=lambda e: e.get("earnings_date", ""))

    saved = {
        "fetched_at": now_iso,
        "source":     "alphavantage",
        "calendar":   new_entries,
    }

    try:
        MARKET_DIR.mkdir(parents=True, exist_ok=True)
        tmp = cal_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(saved, indent=2))
        tmp.replace(cal_path)
    except Exception as exc:
        log.warning("[EARNINGS_AV] write failed (non-fatal): %s", exc)
        return {}

    n_wl = sum(1 for e in new_entries if e["symbol"] in core_stocks)
    log.info(
        "[EARNINGS_AV] Calendar saved: %d entries covering %d core stocks",
        len(new_entries), n_wl,
    )
    return saved


def refresh_earnings_calendar_yfinance_confirm(symbols: list[str]) -> dict:
    """
    Per-symbol yfinance reconciliation against the canonical AV calendar.
    Updates the AV calendar in place when yfinance disagrees on the date.
    NEVER writes a non-AV-tagged file — preserves source: 'alphavantage'.

    Returns: {"updated": [syms], "unchanged": [syms], "missing": [syms]}.
    Non-fatal — never raises.
    """
    cal_path = MARKET_DIR / "earnings_calendar.json"
    out: dict[str, list[str]] = {"updated": [], "unchanged": [], "missing": []}

    if not cal_path.exists():
        return out
    try:
        cal = json.loads(cal_path.read_text())
    except Exception:
        return out
    entries = cal.get("calendar", [])
    by_sym  = {(e.get("symbol") or "").upper(): e for e in entries if isinstance(e, dict)}

    for sym in symbols:
        sym_u = (sym or "").upper()
        if not sym_u:
            continue
        entry = by_sym.get(sym_u)
        if not entry:
            out["missing"].append(sym_u)
            continue
        try:
            yf_cal = yf.Ticker(sym_u).calendar
            yf_dates = (yf_cal or {}).get("Earnings Date", []) if isinstance(yf_cal, dict) else None
            if not yf_dates:
                out["missing"].append(sym_u)
                continue
            yf_d = yf_dates[0]
            yf_iso = yf_d.isoformat() if hasattr(yf_d, "isoformat") else str(yf_d)[:10]
            current_iso = str(entry.get("earnings_date", ""))[:10]
            if yf_iso and yf_iso != current_iso:
                entry["prior_reported_date"] = current_iso
                entry["earnings_date"]       = yf_iso
                out["updated"].append(sym_u)
            else:
                out["unchanged"].append(sym_u)
        except Exception as exc:
            log.debug("[EARNINGS_YF_CONFIRM] %s failed (non-fatal): %s", sym_u, exc)
            out["missing"].append(sym_u)

    # Write back if anything updated
    if out["updated"]:
        try:
            tmp = cal_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(cal, indent=2))
            tmp.replace(cal_path)
            log.info("[EARNINGS_YF_CONFIRM] Updated %d symbols: %s",
                     len(out["updated"]), out["updated"])
        except Exception as exc:
            log.warning("[EARNINGS_YF_CONFIRM] write failed (non-fatal): %s", exc)

    return out


def _check_earnings_calendar_staleness() -> str:
    """
    Inspect data/market/earnings_calendar.json and emit a WARNING-level log
    if the calendar is stale (> 14 days) or wrong source.
    Returns one of: 'ok' | 'stale_warn' | 'stale_critical' | 'missing'.
    """
    cal_path = MARKET_DIR / "earnings_calendar.json"
    if not cal_path.exists():
        log.warning("[EARNINGS_STALE] calendar file missing")
        return "missing"
    try:
        d = json.loads(cal_path.read_text())
    except Exception as exc:
        log.warning("[EARNINGS_STALE] calendar unreadable: %s", exc)
        return "missing"

    src = d.get("source", "")
    if src != "alphavantage":
        log.warning("[EARNINGS_STALE] calendar source=%s (expected alphavantage)", src)
        return "stale_critical"

    fetched = (d.get("fetched_at") or "")[:19]
    try:
        fetched_dt = datetime.fromisoformat(fetched)
    except Exception:
        log.warning("[EARNINGS_STALE] calendar fetched_at unparseable: %s", fetched)
        return "stale_critical"

    age_days = (datetime.now() - fetched_dt).days
    if age_days > 28:
        log.warning("[EARNINGS_STALE] calendar age=%dd > 28d (CRITICAL)", age_days)
        return "stale_critical"
    if age_days > 14:
        log.warning("[EARNINGS_STALE] calendar age=%dd > 14d (DEGRADED)", age_days)
        return "stale_warn"
    return "ok"


def refresh_premarket_movers() -> None:
    """Top movers in pre-market using yfinance."""
    # Use a curated set of highly-liquid names as proxy
    actives = [
        "SPY","QQQ","AAPL","MSFT","NVDA","AMZN","TSLA","META","GOOGL","AMD",
        "COIN","HOOD","SOFI","MARA","RIOT","PLTR","CRWV","RIVN","NIO","BABA",
    ]
    movers = []
    for sym in actives:
        try:
            fi = yf.Ticker(sym).fast_info
            prev  = float(fi.get("regularMarketPreviousClose") or 0)
            pre   = float(fi.get("preMarketPrice") or 0)
            if prev > 0 and pre > 0:
                chg = (pre - prev) / prev * 100
                movers.append({"symbol": sym, "pre_price": pre, "chg_pct": round(chg, 2)})
        except Exception:
            pass

    movers.sort(key=lambda x: x["chg_pct"], reverse=True)
    data = {
        "fetched_at": datetime.now(ET).isoformat(),
        "top_up":     movers[:20],
        "top_down":   list(reversed(movers))[:20],
    }
    _save_json(MARKET_DIR / "premarket_movers.json", data)
    log.info("Pre-market movers saved")


# ── Load helpers (used by market_data.py) ─────────────────────────────────────

def load_sector_perf() -> dict:
    path = MARKET_DIR / "sector_perf.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


def load_macro_snapshot() -> dict:
    path = MARKET_DIR / "macro_snapshot.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


def load_earnings_calendar() -> dict:
    path = MARKET_DIR / "earnings_calendar.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


# ── Global indices ─────────────────────────────────────────────────────────────

_GLOBAL_INDICES = {
    "Nikkei 225":     "^N225",
    "Hang Seng":      "^HSI",
    "Shanghai":       "000001.SS",
    "DAX":            "^GDAXI",
    "FTSE 100":       "^FTSE",
    "CAC 40":         "^FCHI",
    "SP500 Fut":      "ES=F",
    "Nasdaq Fut":     "NQ=F",
    "Dow Fut":        "YM=F",
    "VIX":            "^VIX",
    "USD/JPY":        "JPY=X",
    "USD/CNY":        "CNY=X",
    "EUR/USD":        "EURUSD=X",
}


def _session_status_utc() -> dict:
    """Return which exchange sessions are currently open based on UTC time."""
    now_utc  = datetime.now(timezone.utc)
    h        = now_utc.hour + now_utc.minute / 60.0
    weekday  = now_utc.weekday()   # 0=Mon … 6=Sun
    trading  = weekday < 5
    return {
        "asia":   "open" if (trading and 0.0 <= h < 6.0)   else "closed",
        "europe": "open" if (trading and 7.0 <= h < 16.5)  else "closed",
        "us":     "open" if (trading and 13.5 <= h < 20.0) else "closed",
    }


def refresh_global_indices() -> None:
    """Fetch global indices, futures, and FX pairs via yfinance.
    Saves to data/market/global_indices.json and archives the snapshot."""
    MARKET_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Refreshing global indices (%d symbols)", len(_GLOBAL_INDICES))

    session_status = _session_status_utc()
    results: dict = {}

    for name, ticker in _GLOBAL_INDICES.items():
        try:
            hist = yf.Ticker(ticker).history(period="5d")
            if hist.empty or len(hist) < 1:
                log.debug("Global index no data: %s (%s)", name, ticker)
                continue
            last_price = float(hist["Close"].iloc[-1])
            prev_price = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else last_price
            chg_pct    = (last_price - prev_price) / prev_price * 100 if prev_price else 0.0
            results[ticker] = {
                "name":       name,
                "ticker":     ticker,
                "last_price": round(last_price, 4),
                "chg_pct":    round(chg_pct, 2),
                "prev_price": round(prev_price, 4),
            }
        except Exception as exc:
            log.debug("Global index fetch failed %s (%s): %s", name, ticker, exc)

    data = {
        "fetched_at":     datetime.now(ET).isoformat(),
        "session_status": session_status,
        "indices":        results,
    }
    _save_json(MARKET_DIR / "global_indices.json", data)
    _archive("global_indices.json", data)
    log.info("Global indices saved (%d/%d)", len(results), len(_GLOBAL_INDICES))


def load_global_indices() -> dict:
    path = MARKET_DIR / "global_indices.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}



# ── Crypto sentiment ──────────────────────────────────────────────────────────

def refresh_crypto_sentiment() -> None:
    """
    Fetches Fear & Greed Index and BTC dominance.
    Saves to data/crypto/fear_greed.json.
    Called every 4 hours by scheduler (crypto trades 24/7).
    Graceful - never raises, preserves cache on any error.
    """
    import time as _time  # noqa: PLC0415

    import requests  # noqa: PLC0415

    CRYPTO_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CRYPTO_DIR / "fear_greed.json"

    # Load existing cache to preserve btc_dominance_previous
    try:
        existing = json.loads(out_path.read_text()) if out_path.exists() else {}
    except Exception:
        existing = {}

    # STEP 1 - Fear & Greed Index (alternative.me, free, no key)
    fg_data = None
    try:
        resp = requests.get("https://api.alternative.me/fng/?limit=7", timeout=10)
        resp.raise_for_status()
        raw = resp.json()
        items = raw.get("data", [])
        if items:
            current_item = items[0]
            value = int(current_item.get("value", 50))
            label = current_item.get("value_classification", "")
            ts = datetime.fromtimestamp(
                int(current_item.get("timestamp", 0)), tz=timezone.utc
            ).isoformat()

            history_7d = []
            for it in items:
                try:
                    history_7d.append({
                        "value": int(it.get("value", 0)),
                        "label": it.get("value_classification", ""),
                        "timestamp": datetime.fromtimestamp(
                            int(it.get("timestamp", 0)), tz=timezone.utc
                        ).isoformat(),
                    })
                except Exception:
                    pass

            # Trend: compare current vs 7 days ago
            if len(items) >= 2:
                oldest_val = int(items[-1].get("value", value))
                diff = value - oldest_val
                if diff > 5:
                    trend = "improving"
                elif diff < -5:
                    trend = "worsening"
                else:
                    trend = "stable"
            else:
                trend = "stable"

            # Signal
            if value <= 20:
                signal = "contrarian_buy"
            elif value >= 80:
                signal = "contrarian_sell"
            else:
                signal = "neutral"

            fg_data = {
                "current": {"value": value, "label": label, "timestamp": ts},
                "history_7d": history_7d,
                "trend": trend,
                "signal": signal,
            }
            log.info("Fear & Greed: %d - %s  trend=%s  signal=%s",
                     value, label, trend, signal)
    except Exception as exc:
        log.warning("Fear & Greed fetch failed (keeping cache): %s", exc)

    # STEP 2 - BTC Dominance (CoinGecko, free, no key)
    btc_dom = None
    dom_trend = "stable"
    _time.sleep(2)  # CoinGecko free-tier rate limit
    try:
        resp2 = requests.get(
            "https://api.coingecko.com/api/v3/global", timeout=10
        )
        resp2.raise_for_status()
        raw2 = resp2.json()
        pct = raw2.get("data", {}).get("market_cap_percentage", {}).get("btc")
        if pct is not None:
            btc_dom = round(float(pct), 2)
            prev_dom = existing.get("btc_dominance")
            if prev_dom is not None:
                diff = btc_dom - float(prev_dom)
                if diff > 0.5:
                    dom_trend = "rising"
                elif diff < -0.5:
                    dom_trend = "falling"
                else:
                    dom_trend = "stable"
            log.info("BTC dominance: %.2f%%  trend=%s", btc_dom, dom_trend)
    except Exception as exc:
        log.warning("CoinGecko BTC dominance fetch failed (keeping cache): %s", exc)
        btc_dom = existing.get("btc_dominance")
        dom_trend = existing.get("dominance_trend", "stable")

    # Build output - merge new data with any cached values
    if fg_data is None and not existing:
        log.warning("refresh_crypto_sentiment: no data and no cache - skipping write")
        return

    output = {
        "fetched_at": datetime.now(ET).isoformat(),
    }
    if fg_data:
        output.update(fg_data)
    else:
        for k in ("current", "history_7d", "trend", "signal"):
            if k in existing:
                output[k] = existing[k]

    output["btc_dominance"] = btc_dom
    output["btc_dominance_previous"] = existing.get("btc_dominance")
    output["dominance_trend"] = dom_trend

    try:
        _save_json(out_path, output)
        log.info("Crypto sentiment saved to %s", out_path)
    except Exception as exc:
        log.warning("Failed to save crypto sentiment: %s", exc)


def load_crypto_sentiment() -> dict:
    """Load cached crypto sentiment (Fear & Greed + BTC dominance).
    Returns {} if file missing or unreadable."""
    path = CRYPTO_DIR / "fear_greed.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}

# ── Full refresh ──────────────────────────────────────────────────────────────

def run_full_refresh(target_symbol: str | None = None) -> None:
    watchlist = wm.get_active_watchlist()
    stocks    = watchlist["stocks"]
    etfs      = watchlist["etfs"]
    stock_etfs= [s for s in stocks + etfs if "/" not in s]

    if target_symbol:
        stock_etfs = [target_symbol]

    log.info("Data warehouse refresh starting  symbols=%d", len(stock_etfs))

    refresh_bars(stock_etfs)
    try:
        refresh_economic_calendar_finnhub()
    except Exception as _ec_exc:
        log.warning("Economic calendar refresh failed (non-fatal): %s", _ec_exc)
    refresh_fundamentals(stock_etfs)
    refresh_news(stock_etfs)
    try:
        refresh_yahoo_symbol_news(stock_etfs)
    except Exception as _ysn_exc:
        log.warning("Yahoo symbol news refresh failed (non-fatal): %s", _ysn_exc)
    try:
        refresh_finnhub_news(stock_etfs)
    except Exception as _fhn_exc:
        log.warning("Finnhub news refresh failed (non-fatal): %s", _fhn_exc)
    refresh_sector_performance()
    refresh_macro_snapshot()
    # NOTE: earnings calendar is owned exclusively by AV
    # (refresh_earnings_calendar_av) called weekly by the scheduler.
    # yfinance no longer writes earnings_calendar.json.
    refresh_premarket_movers()
    refresh_global_indices()

    # Insider intelligence — congressional (6h TTL) and Form 4 (4h TTL)
    # Force-refresh at 4AM by passing all symbols; caches handle deduplication
    try:
        all_syms = stock_etfs
        # Invalidate stale caches by temporarily removing cache files if > 4h old
        from insider_intelligence import (  # noqa: PLC0415
            _CONGRESS_FILE,
            _FORM4_FILE,
            _is_stale,
            _load_cache,
            fetch_congressional_trades,
            fetch_form4_insider_trades,
        )
        for path, max_h in ((_CONGRESS_FILE, 6.0), (_FORM4_FILE, 4.0)):
            cache = _load_cache(path)
            if _is_stale(cache, max_h):
                fetch_congressional_trades(all_syms, days_back=45)
                fetch_form4_insider_trades(all_syms, days_back=30)
                break
        log.info("Insider intelligence refreshed")
    except Exception as exc:
        log.warning("Insider intelligence refresh failed (non-fatal): %s", exc)

    # Crypto sentiment — Fear & Greed + BTC dominance
    try:
        refresh_crypto_sentiment()
    except Exception as _cs_exc:
        log.warning("Crypto sentiment refresh failed (non-fatal): %s", _cs_exc)

    # A2 universe bootstrap queue — process up to 5 pending symbols
    try:
        import options_universe_manager as _oum  # noqa: PLC0415
        _bootstrap_result = _oum.run_bootstrap_queue()
        if _bootstrap_result.get("bootstrapped") or _bootstrap_result.get("failed"):
            log.info(
                "A2 bootstrap queue: bootstrapped=%s failed=%s remaining=%d",
                _bootstrap_result["bootstrapped"],
                _bootstrap_result["failed"],
                len(_bootstrap_result.get("remaining", [])),
            )
    except Exception as exc:
        log.warning("A2 bootstrap queue failed (non-fatal): %s", exc)

    # Prune stale intraday bar files (non-fatal)
    try:
        _prune_bars()
    except Exception as exc:
        log.warning("Bar pruning failed (non-fatal): %s", exc)

    log.info("Data warehouse refresh complete")


def _prune_bars(keep_days: int = 30) -> None:
    """Delete intraday bar CSVs older than keep_days. Never deletes daily CSVs."""
    cutoff = datetime.now() - timedelta(days=keep_days)
    deleted = 0
    for f in BARS_DIR.glob("*_intraday_*.csv"):
        try:
            date_str = f.stem.split("_intraday_")[1]
            file_date = datetime.strptime(date_str, "%Y-%m-%d")
            if file_date < cutoff:
                f.unlink()
                deleted += 1
        except (IndexError, ValueError):
            continue
    if deleted:
        log.info("Pruned %d stale intraday bar files (keep_days=%d)", deleted, keep_days)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Data warehouse daily refresh")
    parser.add_argument("--symbol", help="Refresh single symbol only")
    args = parser.parse_args()
    run_full_refresh(target_symbol=args.symbol)


# ── Finnhub Economic Calendar ─────────────────────────────────────────────────

def refresh_economic_calendar_finnhub() -> None:
    """
    Fetch next 7 days of US economic events from Finnhub.
    Saves to data/market/economic_calendar.json and archives.
    Graceful failure: preserves existing cache if Finnhub unavailable.
    """

    import requests  # noqa: PLC0415

    finnhub_key = os.getenv("FINNHUB_API_KEY")
    if not finnhub_key:
        log.debug("FINNHUB_API_KEY not set — writing empty economic calendar placeholder")
        MARKET_DIR.mkdir(parents=True, exist_ok=True)
        _placeholder = {
            "fetched_at":       datetime.now(ET).isoformat(),
            "events":           [],
            "next_high_impact": None,
            "_source":          "no_key_placeholder",
        }
        _save_json(MARKET_DIR / "economic_calendar.json", _placeholder)
        return

    now_et  = datetime.now(ET)
    from_dt = now_et.strftime("%Y-%m-%d")
    to_dt   = (now_et + timedelta(days=7)).strftime("%Y-%m-%d")

    try:
        url  = "https://finnhub.io/api/v1/calendar/economic"
        resp = requests.get(url, params={"token": finnhub_key}, timeout=10)
        resp.raise_for_status()
        raw  = resp.json()
    except Exception as exc:
        log.warning("Finnhub economic calendar fetch failed (keeping cache): %s", exc)
        return

    events: list = []
    next_high_impact = None

    for ev in (raw.get("economicCalendar") or []):
        try:
            ev_date  = str(ev.get("time") or ev.get("date") or "")[:10]
            if not ev_date or ev_date < from_dt or ev_date > to_dt:
                continue
            country = str(ev.get("country") or "").upper()
            if country and country not in ("", "US", "USD"):
                continue

            ev_time_str = str(ev.get("time") or "")
            try:
                ev_dt = datetime.fromisoformat(ev_time_str.replace("Z", "+00:00"))
                ev_et = ev_dt.astimezone(ET)
                datetime_et = ev_et.isoformat()
                minutes_from_now = int((ev_et - now_et.replace(tzinfo=ET)).total_seconds() / 60)
                is_market_hours = 9 * 60 + 30 <= ev_et.hour * 60 + ev_et.minute < 16 * 60
            except Exception:
                datetime_et = ev_date + "T00:00:00"
                minutes_from_now = 0
                is_market_hours = False

            impact_raw = str(ev.get("impact") or "").lower()
            if impact_raw in ("high", "3"):
                impact = "high"
            elif impact_raw in ("medium", "2"):
                impact = "medium"
            else:
                impact = "low"

            # Filter to high/medium only
            if impact not in ("high", "medium"):
                continue

            entry = {
                "event":           str(ev.get("event") or ev.get("name") or ""),
                "datetime_et":     datetime_et,
                "impact":          impact,
                "estimate":        ev.get("estimate"),
                "prev":            ev.get("prev"),
                "actual":          ev.get("actual"),
                "minutes_from_now": minutes_from_now,
                "is_market_hours": is_market_hours,
            }
            events.append(entry)

            # Track next high-impact event
            if impact == "high" and minutes_from_now > 0:
                if next_high_impact is None or minutes_from_now < next_high_impact["minutes_from_now"]:
                    next_high_impact = {
                        "event":          entry["event"],
                        "datetime_et":    entry["datetime_et"],
                        "minutes_from_now": minutes_from_now,
                    }
        except Exception:
            continue

    data = {
        "fetched_at":      now_et.isoformat(),
        "events":          events,
        "next_high_impact": next_high_impact,
    }
    MARKET_DIR.mkdir(parents=True, exist_ok=True)
    _save_json(MARKET_DIR / "economic_calendar.json", data)
    _archive("economic_calendar.json", data)
    log.info("Economic calendar (Finnhub): %d events, next high-impact=%s",
             len(events), next_high_impact.get("event") if next_high_impact else "none")


def load_economic_calendar() -> dict:
    """Load Finnhub economic calendar. Returns {} if unavailable."""
    path = MARKET_DIR / "economic_calendar.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}
