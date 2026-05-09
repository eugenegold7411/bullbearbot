#!/usr/bin/env python3
"""
symbol_enrichment.py

Builds/refreshes canonical per-symbol JSON files in data/symbols/.
Standalone script — reads no production pipeline state, writes no pipeline state.

Usage:
    python3 symbol_enrichment.py                    # full universe
    python3 symbol_enrichment.py CSCO AMAT NVDA     # targeted (3 symbols)
"""

from __future__ import annotations

import json
import logging
import math
import os
import html as _html
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from xml.etree import ElementTree as ET

import requests
import yfinance as yf

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT              = Path(__file__).parent
SYMBOLS_DIR       = ROOT / "data" / "symbols"
CORE_PATH         = ROOT / "watchlist_core.json"
DYNAMIC_PATH      = ROOT / "watchlist_dynamic.json"
IV_HIST_DIR       = ROOT / "data" / "options" / "iv_history"
CONVICTIONS_PATH  = ROOT / "data" / "market" / "earnings_convictions.json"
ANALYST_INTEL_DIR = ROOT / "data" / "earnings_intel"
INSIDER_PATH      = ROOT / "data" / "insider" / "form4_trades.json"
CONGRESS_PATH     = ROOT / "data" / "insider" / "congressional_trades.json"
ENV_PATH          = ROOT / ".env"

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ENRICHMENT] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Globals ────────────────────────────────────────────────────────────────────
_AV_KEY: str | None = None
_AV_EXHAUSTED: bool = False   # set True when AV returns rate-limit notice
_YF_HEADERS    = {"User-Agent": "Mozilla/5.0 (compatible; BullBearBot/1.0)"}
_EDGAR_HEADERS = {"User-Agent": "BullBearBot research@bullbearbot.com"}
_EDGAR_TICKERS_MAP: dict[str, str] | None = None  # ticker -> cik_str, loaded once


# ── .env loader ────────────────────────────────────────────────────────────────
def _load_env() -> None:
    if not ENV_PATH.exists():
        return
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


# ── Universe ───────────────────────────────────────────────────────────────────
def _get_universe() -> list[str]:
    symbols: set[str] = set()
    for path in [CORE_PATH, DYNAMIC_PATH]:
        if path.exists():
            data = json.loads(path.read_text())
            for entry in data.get("symbols", []):
                if isinstance(entry, dict):
                    symbols.add(entry["symbol"])
                elif isinstance(entry, str):
                    symbols.add(entry)
    return sorted(symbols)


# ── Filename sanitizer ─────────────────────────────────────────────────────────
def sanitize_symbol_for_filename(symbol: str) -> str:
    """Convert symbols with special chars (e.g. BTC/USD) to valid filenames."""
    return symbol.replace("/", "_").replace("\\", "_")


# ── Market hours ──────────────────────────────────────────────────────────────
def _is_market_open() -> bool:
    """True only during regular US equity market hours (9:30–16:00 ET, Mon–Fri)."""
    now = datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:
        return False
    market_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return market_open <= now < market_close


# ── IV rank from history ────────────────────────────────────────────────────────
def _iv_rank(symbol: str) -> tuple[float | None, float | None]:
    path = IV_HIST_DIR / f"{symbol}_iv_history.json"
    if not path.exists():
        return None, None
    try:
        history = json.loads(path.read_text())
        ivs = [e["iv"] for e in history if isinstance(e, dict) and e.get("iv") is not None]
        if len(ivs) < 5:
            return None, None
        current = ivs[-1]
        lo, hi = min(ivs), max(ivs)
        if hi == lo:
            return 50.0, round(current, 4)
        rank = (current - lo) / (hi - lo) * 100
        return round(rank, 1), round(current, 4)
    except Exception:
        return None, None


# ── Earnings conviction ─────────────────────────────────────────────────────────
def _earnings_conviction(symbol: str) -> dict | None:
    if not CONVICTIONS_PATH.exists():
        return None
    try:
        data = json.loads(CONVICTIONS_PATH.read_text())
        for candidate in data.get("candidates", []):
            if candidate.get("symbol") == symbol:
                return candidate
    except Exception:
        pass
    return None


# ── Analyst intel ───────────────────────────────────────────────────────────────
def _analyst_intel(symbol: str) -> dict | None:
    path = ANALYST_INTEL_DIR / f"{symbol}_analyst_intel.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


# ── yfinance analyst targets ───────────────────────────────────────────────────
def _yf_targets(symbol: str) -> dict:
    try:
        t = yf.Ticker(symbol)
        targets = t.analyst_price_targets or {}
        info = t.info or {}

        # current-month distribution from recommendations_summary
        strong_buy = buy = hold = sell = strong_sell = None
        try:
            rec = t.recommendations_summary
            if rec is not None and not rec.empty:
                row = rec[rec["period"] == "0m"]
                if not row.empty:
                    r = row.iloc[0]
                    strong_buy  = int(r.get("strongBuy", 0))
                    buy         = int(r.get("buy", 0))
                    hold        = int(r.get("hold", 0))
                    sell        = int(r.get("sell", 0))
                    strong_sell = int(r.get("strongSell", 0))
        except Exception:
            pass

        mean          = targets.get("mean")
        high          = targets.get("high")
        low           = targets.get("low")
        median        = targets.get("median")
        current_price = targets.get("current") or info.get("currentPrice")
        analyst_count = info.get("numberOfAnalystOpinions")
        rec_mean      = info.get("recommendationMean")
        rec_key       = info.get("recommendationKey")

        upside = None
        if mean and current_price and float(current_price) > 0:
            upside = round((float(mean) - float(current_price)) / float(current_price) * 100, 2)

        return {
            "mean":              round(float(mean), 2) if mean else None,
            "high":              round(float(high), 2) if high else None,
            "low":               round(float(low), 2) if low else None,
            "median":            round(float(median), 2) if median else None,
            "current_price":     round(float(current_price), 2) if current_price else None,
            "analyst_count":     int(analyst_count) if analyst_count else None,
            "recommendation_key":  rec_key,
            "recommendation_mean": round(float(rec_mean), 3) if rec_mean else None,
            "strong_buy":        strong_buy,
            "buy":               buy,
            "hold":              hold,
            "sell":              sell,
            "strong_sell":       strong_sell,
            "upside_pct":        upside,
        }
    except Exception as exc:
        log.warning(f"{symbol}: yfinance targets error: {exc}")
        return {}


# ── yfinance fundamentals ──────────────────────────────────────────────────────
def _yf_fundamentals(symbol: str) -> dict:
    try:
        info = yf.Ticker(symbol).info or {}
        mc = info.get("marketCap")
        return {
            "sector":            info.get("sector"),
            "industry":          info.get("industry"),
            "market_cap":        int(mc) if mc else None,
            "market_cap_b":      round(mc / 1e9, 2) if mc else None,
            "beta":              info.get("beta"),
            "trailing_pe":       info.get("trailingPE"),
            "forward_pe":        info.get("forwardPE"),
            "peg_ratio":         info.get("pegRatio"),
            "price_to_book":     info.get("priceToBook"),
            "price_to_sales":    info.get("priceToSalesTrailing12Months"),
            "trailing_eps":      info.get("trailingEps"),
            "dividend_yield":    info.get("dividendYield"),
            "week_52_high":      info.get("fiftyTwoWeekHigh"),
            "week_52_low":       info.get("fiftyTwoWeekLow"),
        }
    except Exception as exc:
        log.warning(f"{symbol}: yfinance fundamentals error: {exc}")
        return {}


# ── Alpha Vantage OVERVIEW (supplemental — EPS, revenue, cross-check) ──────────
def _av_overview(symbol: str, use_delay: bool = False) -> dict:
    global _AV_KEY, _AV_EXHAUSTED
    if _AV_EXHAUSTED:
        return {}
    if _AV_KEY is None:
        _AV_KEY = os.environ.get("ALPHA_VANTAGE_API_KEY", "")
    if not _AV_KEY:
        return {}
    try:
        url = (
            f"https://www.alphavantage.co/query"
            f"?function=OVERVIEW&symbol={symbol}&apikey={_AV_KEY}"
        )
        r = requests.get(url, timeout=15).json()

        # rate-limit or error responses
        if not r or "Note" in r or "Information" in r or "Error" in r:
            log.warning(f"{symbol}: AV rate-limited or no data — disabling AV for this run")
            _AV_EXHAUSTED = True
            return {}

        def _f(key: str) -> float | None:
            v = r.get(key)
            try:
                return float(v) if v and v not in ("None", "-", "") else None
            except Exception:
                return None

        result = {
            "forward_pe":            _f("ForwardPE"),
            "peg_ratio":             _f("PEGRatio"),
            "eps":                   _f("EPS"),
            "price_to_sales":        _f("PriceToSalesRatioTTM"),
            "revenue_per_share_ttm": _f("RevenuePerShareTTM"),
            "week_52_high":          _f("52WeekHigh"),
            "week_52_low":           _f("52WeekLow"),
            "analyst_target":        _f("AnalystTargetPrice"),
        }
        if use_delay:
            time.sleep(12)  # AV free tier: 5 req/min
        return result
    except Exception as exc:
        log.warning(f"{symbol}: AV OVERVIEW error: {exc}")
        return {}


# ── Yahoo RSS news ─────────────────────────────────────────────────────────────
def _yahoo_rss(symbol: str, limit: int = 20) -> list[dict]:
    try:
        url = (
            f"https://feeds.finance.yahoo.com/rss/2.0/headline"
            f"?s={symbol}&region=US&lang=en-US"
        )
        r = requests.get(url, timeout=10, headers=_YF_HEADERS)
        root = ET.fromstring(r.text)
        articles = []
        for item in root.findall(".//item")[:limit]:
            articles.append({
                "headline":  item.findtext("title", ""),
                "published": item.findtext("pubDate", ""),
                "link":      item.findtext("link", ""),
                "source":    "yahoo_rss",
            })
        return articles
    except Exception as exc:
        log.warning(f"{symbol}: Yahoo RSS error: {exc}")
        return []


# ── Alpaca news ────────────────────────────────────────────────────────────────
def _alpaca_news(symbol: str, days: int = 7) -> list[dict]:
    try:
        from alpaca.data.historical import NewsClient
        from alpaca.data.requests import NewsRequest

        client = NewsClient(
            api_key=os.environ.get("ALPACA_API_KEY", ""),
            secret_key=os.environ.get("ALPACA_SECRET_KEY", ""),
        )
        req = NewsRequest(
            symbols=symbol,
            start=datetime.now(timezone.utc) - timedelta(days=days),
            end=datetime.now(timezone.utc),
            limit=10,
        )
        news = client.get_news(req)
        articles = []
        for v in news.data.values():
            for a in v:
                syms = getattr(a, "symbols", []) or []
                articles.append({
                    "headline":      a.headline,
                    "published":     a.created_at.isoformat() if a.created_at else None,
                    "source":        a.source,
                    "symbols":       syms,
                    "single_symbol": len(syms) == 1,
                })
        articles.sort(key=lambda x: x.get("published") or "", reverse=True)
        return articles
    except Exception as exc:
        log.warning(f"{symbol}: Alpaca news error: {exc}")
        return []


# ── Beat history (yfinance earnings_dates) ─────────────────────────────────────
def fetch_beat_history(symbol: str) -> list[dict] | None:
    """
    Fetch earnings beat history from yfinance.
    Returns up to 8 past quarters with eps_actual, eps_estimate, surprise_pct.
    Returns None if no data available (ETFs, non-reporting symbols).
    """
    try:
        t = yf.Ticker(symbol)
        try:
            hist = t.get_earnings_dates(limit=12)
        except Exception:
            hist = t.earnings_dates
        if hist is None or hist.empty:
            log.debug(f"{symbol}: No earnings_dates from yfinance")
            return None
        records = []
        for dt, row in hist.iterrows():
            eps_act = row.get("Reported EPS")
            eps_est = row.get("EPS Estimate")
            surprise = row.get("Surprise(%)")
            # Skip future dates (no actuals yet)
            try:
                if eps_act is None or (isinstance(eps_act, float) and math.isnan(eps_act)):
                    continue
            except (TypeError, ValueError):
                continue
            records.append({
                "quarter_end_date": dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)[:10],
                "eps_actual":   round(float(eps_act), 4) if eps_act is not None else None,
                "eps_estimate": round(float(eps_est), 4) if eps_est is not None and not (isinstance(eps_est, float) and math.isnan(eps_est)) else None,
                "surprise_pct": round(float(surprise), 2) if surprise is not None and not (isinstance(surprise, float) and math.isnan(surprise)) else None,
            })
        return records[:8] if records else None
    except Exception as exc:
        log.warning(f"{symbol}: beat_history error: {exc}")
        return None


# ── EDGAR transcript (SEC 8-K) ─────────────────────────────────────────────────

def _edgar_cik(symbol: str) -> str | None:
    """
    Resolve ticker to CIK using SEC company_tickers.json.
    Caches the full map on first call (one HTTP request for all symbols).
    Returns CIK as plain numeric string (no leading zeros), or None.
    """
    global _EDGAR_TICKERS_MAP
    if _EDGAR_TICKERS_MAP is None:
        try:
            r = requests.get(
                "https://www.sec.gov/files/company_tickers.json",
                headers=_EDGAR_HEADERS, timeout=15,
            )
            _EDGAR_TICKERS_MAP = {
                v["ticker"].upper(): str(v["cik_str"])
                for v in r.json().values()
            }
            time.sleep(1)
        except Exception as exc:
            log.debug("EDGAR tickers map fetch failed: %s", exc)
            _EDGAR_TICKERS_MAP = {}
    return _EDGAR_TICKERS_MAP.get(symbol.upper())


def fetch_edgar_transcript(symbol: str) -> dict | None:
    """
    Fetch most recent 8-K filing excerpt from SEC EDGAR.
    Resolves CIK via company_tickers.json, then fetches the filing document text.
    Returns None on CIK lookup failure (expected for non-US/ETF symbols).
    """
    try:
        # Step 1: Resolve CIK
        cik_str = _edgar_cik(symbol)
        if not cik_str:
            log.debug(f"{symbol}: EDGAR CIK not found in company_tickers.json")
            return None
        cik_padded = cik_str.zfill(10)

        # Step 2: Get submissions JSON for this CIK
        sub_url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
        r = requests.get(sub_url, timeout=15, headers=_EDGAR_HEADERS)
        time.sleep(1)
        if r.status_code != 200:
            log.debug(f"{symbol}: EDGAR submissions fetch failed (status={r.status_code})")
            return None
        subs = r.json()

        filings    = subs.get("filings", {}).get("recent", {})
        forms      = filings.get("form", [])
        accessions = filings.get("accessionNumber", [])
        dates      = filings.get("filingDate", [])

        # Step 3: Find most recent 8-K
        for i, form in enumerate(forms):
            if form != "8-K":
                continue
            filing_date      = dates[i]
            accession_raw    = accessions[i]           # "0000858877-26-000057"
            accession_nodash = accession_raw.replace("-", "")

            # Step 4: List the filing directory to find the document file
            dir_url = (
                f"https://www.sec.gov/Archives/edgar/data/{cik_str}/"
                f"{accession_nodash}/"
            )
            r2 = requests.get(dir_url, timeout=15, headers=_EDGAR_HEADERS)
            time.sleep(1)
            if r2.status_code != 200:
                continue

            # Extract filing-specific .htm links (filter to /Archives/edgar/data/... paths)
            links = re.findall(
                r'href="(/Archives/edgar/data/[^"]+\.htm)"',
                r2.text, re.IGNORECASE,
            )
            # Prefer the company-named HTM (e.g. csco-20260427.htm) over index files
            doc_path = None
            for lnk in links:
                basename = lnk.split("/")[-1].lower()
                if "index" not in basename and not basename.startswith("0000"):
                    doc_path = lnk
                    break
            if not doc_path and links:
                doc_path = links[0]
            if not doc_path:
                continue

            doc_url = "https://www.sec.gov" + doc_path

            # Step 5: Fetch document and strip HTML to get text excerpt
            r3 = requests.get(doc_url, timeout=20, headers=_EDGAR_HEADERS)
            time.sleep(1)
            if r3.status_code != 200:
                continue

            raw   = r3.text[:5000]
            clean = re.sub(r"<[^>]+>", " ", raw)
            clean = _html.unescape(clean)
            clean = " ".join(clean.split())

            return {
                "cik":                cik_str,
                "filing_date":        filing_date,
                "transcript_excerpt": clean[:2000],
                "full_url":           doc_url,
            }

        log.debug(f"{symbol}: No 8-K filings found in EDGAR submissions")
        return None

    except Exception as exc:
        log.debug(f"{symbol}: EDGAR error (non-fatal): {exc}")
        return None


# ── Insider trades (Form 4) ────────────────────────────────────────────────────
def fetch_insider_trades(symbol: str) -> list[dict]:
    """
    Parse data/insider/form4_trades.json and return trades for symbol from last 30 days.
    """
    if not INSIDER_PATH.exists():
        log.error(f"form4_trades.json not found at {INSIDER_PATH}")
        return []
    try:
        data = json.loads(INSIDER_PATH.read_text())
        trades = data.get("trades", [])
        result = []
        for t in trades:
            if t.get("ticker") != symbol:
                continue
            if t.get("days_since_filing", 999) > 30:
                continue
            result.append({
                "date":         t.get("filing_date"),
                "insider_name": t.get("insider_name"),
                "role":         t.get("role"),
                "action":       "buy" if t.get("acquired_disposed") == "A" else "sell",
                "shares":       t.get("transaction_shares"),
                "price":        t.get("price"),
                "value":        t.get("value_usd"),
            })
        return result
    except Exception as exc:
        log.error(f"{symbol}: form4 parse error: {exc}")
        return []


# ── Congressional trades ──────────────────────────────────────────────────────
def fetch_congress_trades(symbol: str) -> list[dict]:
    """
    Parse data/insider/congressional_trades.json and return trades for symbol from last 30 days.
    """
    if not CONGRESS_PATH.exists():
        log.error(f"congressional_trades.json not found at {CONGRESS_PATH}")
        return []
    try:
        data = json.loads(CONGRESS_PATH.read_text())
        trades = data.get("trades", [])
        result = []
        for t in trades:
            if t.get("ticker") != symbol:
                continue
            if t.get("days_since_trade", 999) > 30:
                continue
            result.append({
                "date":         t.get("filing_date"),
                "politician":   t.get("politician"),
                "party":        t.get("party"),
                "chamber":      t.get("chamber"),
                "action":       t.get("action"),
                "amount_range": t.get("amount_range"),
            })
        return result
    except Exception as exc:
        log.error(f"{symbol}: congressional parse error: {exc}")
        return []


# ── Main enrichment ────────────────────────────────────────────────────────────
def enrich_symbol(symbol: str, av_delay: bool = False) -> bool:
    out: dict = {
        "symbol":       symbol,
        "last_enriched": datetime.now(timezone.utc).isoformat(),
    }

    # ── yfinance analyst targets ──
    targets = _yf_targets(symbol)
    if targets.get("mean"):
        print(f"  {symbol}: Fetched analyst targets "
              f"(mean=${targets['mean']}, N={targets.get('analyst_count')} analysts)")
    else:
        print(f"  {symbol}: WARNING — no yfinance analyst targets")
    out["analyst_targets"] = targets

    # ── yfinance fundamentals ──
    fund_yf = _yf_fundamentals(symbol)
    if fund_yf.get("sector"):
        print(f"  {symbol}: Fetched yfinance fundamentals "
              f"(sector={fund_yf.get('sector')})")
    else:
        print(f"  {symbol}: WARNING — yfinance fundamentals empty (ETF or no sector)")
    out["fundamentals_yf"] = fund_yf

    # ── Alpha Vantage OVERVIEW ──
    fund_av = _av_overview(symbol, use_delay=av_delay)
    if fund_av.get("forward_pe"):
        print(f"  {symbol}: Fetched AV fundamentals "
              f"(ForwardPE={fund_av.get('forward_pe')})")
    else:
        print(f"  {symbol}: No AV OVERVIEW data")
    out["fundamentals_av"] = fund_av

    # ── Beat history ──
    beats = fetch_beat_history(symbol)
    if beats:
        print(f"  {symbol}: Fetched beat history ({len(beats)} quarters)")
    else:
        print(f"  {symbol}: No beat history (ETF or no earnings data)")
    out["beat_history"] = beats

    # ── EDGAR transcript ──
    edgar = fetch_edgar_transcript(symbol)
    if edgar:
        print(f"  {symbol}: Fetched EDGAR 8-K (filed {edgar.get('filing_date')}, "
              f"CIK={edgar.get('cik')})")
    else:
        print(f"  {symbol}: No EDGAR transcript (CIK lookup failed or no 8-K found)")
    out["edgar_transcript"] = edgar

    # ── Yahoo RSS ──
    yahoo = _yahoo_rss(symbol)
    print(f"  {symbol}: Fetched {len(yahoo)} articles from Yahoo RSS")
    out["news_yahoo"] = yahoo

    # ── Alpaca news ──
    alpaca = _alpaca_news(symbol)
    single = [a for a in alpaca if a.get("single_symbol")]
    print(f"  {symbol}: Fetched {len(alpaca)} articles from Alpaca "
          f"({len(single)} single-symbol)")
    out["news_alpaca"] = alpaca

    # ── IV rank ──
    iv_rank, iv_current = _iv_rank(symbol)
    iv_preserved = False
    if iv_rank is None and not _is_market_open():
        safe = sanitize_symbol_for_filename(symbol)
        existing_path = SYMBOLS_DIR / f"{safe}.json"
        if existing_path.exists():
            try:
                existing = json.loads(existing_path.read_text())
                ex_iv = existing.get("iv") or {}
                ex_rank    = ex_iv.get("rank")    if isinstance(ex_iv, dict) else None
                ex_current = ex_iv.get("current") if isinstance(ex_iv, dict) else None
                if ex_rank is not None:
                    iv_rank, iv_current, iv_preserved = ex_rank, ex_current, True
            except Exception:
                pass
    if iv_preserved:
        print(f"  {symbol}: Markets closed — preserved IV rank = {iv_rank}")
    elif iv_rank is not None:
        print(f"  {symbol}: Found IV rank = {iv_rank}")
    else:
        print(f"  {symbol}: No options IV history")
    out["iv"] = {"rank": iv_rank, "current": iv_current}

    # ── Earnings conviction ──
    conviction = _earnings_conviction(symbol)
    if conviction:
        print(f"  {symbol}: Found conviction score = {conviction.get('conviction_score')} "
              f"(eda={conviction.get('eda')})")
    else:
        print(f"  {symbol}: Not in convictions file")
    out["earnings_conviction"] = conviction

    # ── Analyst intel from earnings_intel/ ──
    out["analyst_intel"] = _analyst_intel(symbol)

    # ── Insider trades (Form 4, last 30 days) ──
    insiders = fetch_insider_trades(symbol)
    print(f"  {symbol}: Found {len(insiders)} Form 4 insider trades (30d)")
    out["insider_trades_30d"] = insiders

    # ── Congressional trades (last 30 days) ──
    congress = fetch_congress_trades(symbol)
    print(f"  {symbol}: Found {len(congress)} congressional trades (30d)")
    out["congress_trades_30d"] = congress

    # ── Write ──
    SYMBOLS_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = sanitize_symbol_for_filename(symbol)
    out_path = SYMBOLS_DIR / f"{safe_name}.json"
    out_path.write_text(json.dumps(out, indent=2, default=str))
    print(f"  {symbol}: Wrote canonical file to data/symbols/{safe_name}.json")
    print(f"  {symbol}: ✓ Enrichment complete")
    return True


def main() -> None:
    _load_env()

    targeted = len(sys.argv) > 1
    if targeted:
        symbols = [s.upper() for s in sys.argv[1:]]
    else:
        symbols = _get_universe()

    total = len(symbols)
    print(f"[ENRICHMENT] Starting enrichment for {total} symbols...")
    if not targeted and total > 10:
        print(f"[ENRICHMENT] Full-universe mode: AV calls enabled (rate-limited), "
              f"~{total * 1.5:.0f}s estimated")

    failed = []
    for i, sym in enumerate(symbols, 1):
        print(f"\n[{i}/{total}] Processing {sym}...")
        try:
            # Use AV delay only for targeted runs (≤10 symbols) to stay in free-tier limits
            enrich_symbol(sym, av_delay=targeted and total <= 10)
        except Exception as exc:
            log.error(f"{sym}: ✗ Enrichment failed: {exc}")
            failed.append(sym)
        if not targeted:
            time.sleep(1)  # gentle pacing for full-universe run

    print(f"\n[ENRICHMENT] Done. {total - len(failed)}/{total} succeeded.")
    if failed:
        print(f"[ENRICHMENT] Failed symbols: {failed}")


if __name__ == "__main__":
    main()
