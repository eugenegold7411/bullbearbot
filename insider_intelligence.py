"""
insider_intelligence.py — Congressional trades + SEC Form 4 insider trades.

Data sources:
  Congressional : Lambda Finance free API  (no auth required)
  Form 4        : SEC EDGAR full-text search (no auth required)

Cache:
  Congressional : data/insider/congressional_trades.json  (6h TTL)
  Form 4        : data/insider/form4_trades.json          (4h TTL)

All public functions degrade gracefully — return [] / empty string on any error.
"""

import json
import time as _time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

from log_setup import get_logger

log = get_logger(__name__)

_BASE_DIR       = Path(__file__).parent
_INSIDER_DIR    = _BASE_DIR / "data" / "insider"
_CONGRESS_FILE  = _INSIDER_DIR / "congressional_trades.json"
_FORM4_FILE     = _INSIDER_DIR / "form4_trades.json"

_TIMEOUT     = 15
# SEC EDGAR requires a user-agent identifying your app + contact email
_SEC_HEADERS = {
    "User-Agent":       "trading-bot research@tradingbot.ai",
    "Accept-Encoding":  "gzip, deflate",
}

# Pacing for the per-filing XML fetch loop. SEC EDGAR's documented rate limit
# is 10 req/sec. We pace at ~8 req/sec to stay well under.
_XML_PACING_SEC = 0.12

# Threshold (USD) above which an open-market officer purchase is flagged
# high_conviction. Grants/RSU vests (transactionCode=A or M) never qualify.
_HIGH_CONVICTION_USD = 10_000.0


# ── Cache helpers ──────────────────────────────────────────────────────────────

def _load_cache(path: Path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return {}


def _save_cache(path: Path, data: dict) -> None:
    try:
        _INSIDER_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, default=str))
    except Exception as exc:
        log.warning("[INSIDER] Cache save failed: %s", exc)


def _is_stale(cache: dict, max_age_hours: float) -> bool:
    fetched = cache.get("fetched_at")
    if not fetched:
        return True
    try:
        dt = datetime.fromisoformat(fetched)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600 > max_age_hours
    except Exception:
        return True


# ── Congressional trades ───────────────────────────────────────────────────────

def fetch_congressional_trades(symbols: list[str], days_back: int = 45) -> list[dict]:
    """
    Fetch recent congressional trades via Lambda Finance free API.
    Filters to our watchlist symbols. Caches for 6 hours.
    Returns [] on any failure.
    """
    cache = _load_cache(_CONGRESS_FILE)
    if not _is_stale(cache, max_age_hours=6.0):
        return _filter_by_symbols(cache.get("trades", []), symbols, days_back, "days_since_trade")

    # QuiverQuant free public API — no auth required.
    # Previously used api.lambdafin.com which is no longer DNS-resolvable (discontinued 2026-04).
    # QuiverQuant rate-limits datacenter IPs without browser-like headers; browser UA is required.
    log.info("[INSIDER] Fetching congressional trades from QuiverQuant")
    try:
        resp = requests.get(
            "https://api.quiverquant.com/beta/live/congresstrading",
            timeout=_TIMEOUT,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json",
                "Referer": "https://www.quiverquant.com/",
            },
        )
        resp.raise_for_status()
        raw = resp.json()
    except Exception as exc:
        log.warning("[INSIDER] Congressional API failed: %s — using cached data", exc)
        return _filter_by_symbols(cache.get("trades", []), symbols, days_back, "days_since_trade")

    trades = _parse_congressional(raw)
    _save_cache(_CONGRESS_FILE, {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "trades":     trades,
    })
    log.info("[INSIDER] Congressional trades saved: %d total", len(trades))
    return _filter_by_symbols(trades, symbols, days_back, "days_since_trade")


def _parse_congressional(raw) -> list[dict]:
    items = raw if isinstance(raw, list) else raw.get("data", raw.get("trades", raw.get("results", [])))
    if not isinstance(items, list):
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=180)
    trades = []
    for item in items:
        try:
            # Support both Lambda Finance (lowercase) and QuiverQuant (PascalCase) field names
            ticker = (item.get("ticker") or item.get("Ticker") or item.get("symbol") or "").upper().strip()
            if not ticker or len(ticker) > 6 or not ticker.isalpha():
                continue

            date_str = (
                item.get("transaction_date") or item.get("TransactionDate") or
                item.get("filing_date") or item.get("ReportDate") or
                item.get("date") or ""
            )
            days_ago = 999
            if date_str:
                try:
                    dt = datetime.fromisoformat(date_str[:10]).replace(tzinfo=timezone.utc)
                    if dt < cutoff:
                        continue
                    days_ago = max(0, (datetime.now(timezone.utc) - dt).days)
                except ValueError:
                    pass

            tx = (item.get("transaction_type") or item.get("Transaction") or item.get("type") or "").lower()
            action = (
                "buy"  if any(k in tx for k in ("purchas", "buy", "acqui")) else
                "sell" if any(k in tx for k in ("sale", "sell", "dispose")) else
                tx or "unknown"
            )

            trades.append({
                "ticker":          ticker,
                "politician":      (item.get("politician") or item.get("Representative") or
                                    item.get("representative") or item.get("name") or "Unknown"),
                "party":           item.get("party") or item.get("Party") or "",
                "chamber":         item.get("chamber") or item.get("House") or "",
                "committee":       item.get("committee") or item.get("committees") or "",
                "action":          action,
                "amount_range":    item.get("amount") or item.get("Range") or item.get("amount_range") or "",
                "filing_date":     date_str[:10] if date_str else "",
                "days_since_trade": days_ago,
            })
        except Exception:
            continue

    return trades


# ── SEC Form 4 Insider Trades ──────────────────────────────────────────────────

def _parse_form4_xml(xml_text: str) -> dict:
    """
    Parse Form 4 XML and return enriched fields. Returns {} on parse error.
    Public for testability — the network-fetcher delegates here.
    """
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return {}

    def _text(tag: str) -> Optional[str]:
        el = root.find(f".//{tag}")
        if el is None or el.text is None:
            return None
        s = el.text.strip()
        return s or None

    def _float(path: str) -> Optional[float]:
        v = _text(path)
        try:
            return float(v) if v else None
        except Exception:
            return None

    return {
        "issuer_trading_symbol": _text("issuerTradingSymbol"),
        "issuer_name":           _text("issuerName"),
        "transaction_shares":    _float("transactionShares/value"),
        "transaction_price":     _float("transactionPricePerShare/value"),
        "acquired_disposed":     _text("transactionAcquiredDisposedCode/value"),
        "transaction_code":      _text("transactionCode"),
        "is_director":           _text("isDirector") == "1",
        "is_officer":            _text("isOfficer") == "1",
        "is_ten_percent_owner":  _text("isTenPercentOwner") == "1",
        "officer_title":         _text("officerTitle"),
        "shares_after":          _float("sharesOwnedFollowingTransaction/value"),
    }


def _fetch_form4_xml(cik: str, accession_no: str) -> dict:
    """
    Fetch and parse a Form 4 XML filing from SEC EDGAR.

    Process:
      1. GET index.json to discover the actual XML filename (filenames vary
         per filer: "form4.xml", "wk-form4_NNN.xml", "form4-MMDDYYYY_NNN.xml").
      2. GET the discovered XML file.
      3. Parse and return enriched fields.

    Returns {} on any failure. Always sleeps _XML_PACING_SEC at the end for
    SEC rate-limit compliance.
    """
    if not cik or not accession_no:
        return {}
    try:
        cik_int = int(str(cik).lstrip("0") or "0")
        adsh = str(accession_no).replace("-", "")
        if not cik_int or not adsh:
            return {}

        idx_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{adsh}/index.json"
        r = requests.get(idx_url, headers=_SEC_HEADERS, timeout=_TIMEOUT)
        if r.status_code != 200:
            return {}
        items = (r.json().get("directory", {}) or {}).get("item", []) or []
        xml_files = [it.get("name", "") for it in items
                     if it.get("name", "").lower().endswith(".xml")]
        if not xml_files:
            return {}
        # Prefer files whose name contains 'form4'; else first .xml
        primary = next(
            (n for n in xml_files if "form4" in n.lower()),
            xml_files[0],
        )

        xml_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{adsh}/{primary}"
        r2 = requests.get(xml_url, headers=_SEC_HEADERS, timeout=_TIMEOUT)
        if r2.status_code != 200:
            return {}
        return _parse_form4_xml(r2.text)
    except Exception as exc:
        log.debug("[INSIDER] _fetch_form4_xml(%s,%s) failed: %s",
                  cik, accession_no, exc)
        return {}
    finally:
        _time.sleep(_XML_PACING_SEC)


def is_high_conviction_trade(trade: dict) -> bool:
    """
    Structured high_conviction predicate.
    True iff:
      - is_officer is True, AND
      - transaction_code == "P" (open-market purchase), AND
      - transaction_shares * transaction_price > _HIGH_CONVICTION_USD.
    Grants ("A"), exercises ("M"), tax withholdings ("F") are never high
    conviction even by senior officers.
    """
    if not trade.get("is_officer"):
        return False
    if trade.get("transaction_code") != "P":
        return False
    shares = trade.get("transaction_shares") or 0
    price  = trade.get("transaction_price") or 0
    try:
        return float(shares) * float(price) > _HIGH_CONVICTION_USD
    except Exception:
        return False


def fetch_form4_insider_trades(symbols: list[str], days_back: int = 30) -> list[dict]:
    """
    Fetch SEC Form 4 insider purchase filings via EDGAR EFTS.
    Focuses on C-suite/director purchases. Caches for 4 hours.
    Returns [] on any failure.
    """
    cache = _load_cache(_FORM4_FILE)
    if not _is_stale(cache, max_age_hours=4.0):
        return _filter_by_symbols(cache.get("trades", []), symbols, days_back, "days_since_filing")

    log.info("[INSIDER] Fetching Form 4 insider trades from SEC EDGAR")
    end_dt   = datetime.now(timezone.utc).date()
    start_dt = end_dt - timedelta(days=days_back + 5)
    all_trades: list[dict] = []

    for symbol in symbols:
        if "/" in symbol:
            continue
        try:
            trades = _fetch_edgar_form4(symbol, str(start_dt), str(end_dt))
            all_trades.extend(trades)
        except Exception as exc:
            log.debug("[INSIDER] Form4 EDGAR failed %s: %s", symbol, exc)

    _save_cache(_FORM4_FILE, {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "trades":     all_trades,
    })
    log.info("[INSIDER] Form 4 trades saved: %d total", len(all_trades))
    return _filter_by_symbols(all_trades, symbols, days_back, "days_since_filing")


def _fetch_edgar_form4(symbol: str, start_dt: str, end_dt: str) -> list[dict]:
    url = (
        "https://efts.sec.gov/LATEST/search-index"
        f"?q=%22{symbol}%22&forms=4"
        f"&dateRange=custom&startdt={start_dt}&enddt={end_dt}"
    )
    try:
        resp = requests.get(url, headers=_SEC_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.debug("[INSIDER] EDGAR request failed %s: %s", symbol, exc)
        return []

    hits   = data.get("hits", {}).get("hits", [])
    trades = []
    for hit in hits[:8]:
        try:
            src         = hit.get("_source", {})
            filing_date = src.get("period_of_report") or src.get("file_date") or ""
            names       = src.get("display_names", [])
            entity_name = names[0] if names else src.get("entity_name", "Unknown")
            # EDGAR FTS returns the accession_no under 'adsh' (no underscore form).
            adsh        = src.get("adsh") or src.get("accession_no") or ""
            ciks        = src.get("ciks") or []
            reporter_cik = ciks[0] if ciks else ""

            days_ago = 999
            if filing_date:
                try:
                    days_ago = max(0, (datetime.now() - datetime.strptime(filing_date[:10], "%Y-%m-%d")).days)
                except ValueError:
                    pass

            # Per-filing XML enrichment — best-effort, non-fatal.
            xml_data = _fetch_form4_xml(reporter_cik, adsh) if (reporter_cik and adsh) else {}

            shares = xml_data.get("transaction_shares")
            price  = xml_data.get("transaction_price")
            value_usd = (
                round(shares * price, 2)
                if (shares is not None and price is not None and shares and price)
                else None
            )

            trade = {
                "ticker":             symbol,
                "insider_name":       entity_name,
                "role":               xml_data.get("officer_title") or "insider",
                "officer_title":      xml_data.get("officer_title"),
                "is_officer":         xml_data.get("is_officer", False),
                "is_director":        xml_data.get("is_director", False),
                "is_ten_percent_owner": xml_data.get("is_ten_percent_owner", False),
                "issuer_trading_symbol": xml_data.get("issuer_trading_symbol"),
                "transaction_code":   xml_data.get("transaction_code"),
                "acquired_disposed":  xml_data.get("acquired_disposed"),
                "transaction_shares": shares,
                "transaction_price":  price,
                "shares_after":       xml_data.get("shares_after"),
                # Legacy aliases (kept for any existing readers)
                "shares_purchased":   shares,
                "price":              price,
                "value_usd":          value_usd,
                "filing_date":        filing_date[:10] if filing_date else "",
                "days_since_filing":  days_ago,
                "accession_number":   adsh,
            }
            trade["high_conviction"] = is_high_conviction_trade(trade)
            trades.append(trade)
        except Exception:
            continue

    return trades


# ── Conviction score ───────────────────────────────────────────────────────────

def compute_insider_conviction_score(
    symbol: str,
    congress_trades: list[dict],
    form4_trades: list[dict],
) -> dict:
    """Combine congressional + Form 4 signals into a per-symbol conviction score."""
    s = symbol.upper()
    cong   = [t for t in congress_trades if t.get("ticker", "").upper() == s]
    form4  = [t for t in form4_trades    if t.get("ticker", "").upper() == s]

    cong_buys_30d  = sum(1 for t in cong  if t.get("action") == "buy"  and t.get("days_since_trade", 999)  <= 30)
    cong_sells_30d = sum(1 for t in cong  if t.get("action") == "sell" and t.get("days_since_trade", 999)  <= 30)
    form4_buys_30d = sum(1 for t in form4 if t.get("days_since_filing", 999) <= 30)

    committee_overlap  = any(bool(t.get("committee")) for t in cong if t.get("action") == "buy")
    high_conviction    = any(t.get("high_conviction") for t in form4)
    triple_signal      = cong_buys_30d > 0 and form4_buys_30d > 0 and committee_overlap

    parts = []
    if cong_buys_30d:
        names = [t.get("politician", "?") for t in cong if t.get("action") == "buy"][:2]
        parts.append(f"{cong_buys_30d} congressional buy(s) by {', '.join(names)}")
    if cong_sells_30d:
        parts.append(f"{cong_sells_30d} congressional sell(s)")
    if form4_buys_30d:
        parts.append(f"{form4_buys_30d} Form 4 filing(s)")
    if high_conviction:
        parts.append("HIGH CONVICTION: C-suite insider buy")
    if triple_signal:
        parts.append("TRIPLE SIGNAL: congress + Form4 + committee")

    return {
        "symbol":                  s,
        "congressional_buys_30d":  cong_buys_30d,
        "congressional_sells_30d": cong_sells_30d,
        "committee_overlap":       committee_overlap,
        "form4_buys_30d":          form4_buys_30d,
        "high_conviction_flag":    high_conviction,
        "triple_signal":           triple_signal,
        "summary":                 "; ".join(parts) if parts else "no insider activity",
    }


# ── Prompt section ─────────────────────────────────────────────────────────────

def build_insider_intelligence_section(symbols: list[str]) -> str:
    """
    Build the full INSIDER & CONGRESSIONAL ACTIVITY prompt section.
    Returns a placeholder string on any failure — never raises.
    """
    try:
        cong  = fetch_congressional_trades(symbols, days_back=45)
        form4 = fetch_form4_insider_trades(symbols, days_back=30)
    except Exception as exc:
        log.warning("[INSIDER] Section build failed: %s", exc)
        return "  (insider intelligence unavailable this cycle)"

    if not cong and not form4:
        return "  (no congressional or insider activity found for watchlist symbols)"

    high_conv_lines = []
    congress_lines  = []
    form4_lines     = []
    sell_lines      = []

    for sym in symbols:
        if "/" in sym:
            continue
        score = compute_insider_conviction_score(sym, cong, form4)
        sym_cong  = [t for t in cong  if t.get("ticker", "").upper() == sym.upper()]
        sym_form4 = [t for t in form4 if t.get("ticker", "").upper() == sym.upper()]

        if score["triple_signal"] or score["high_conviction_flag"]:
            parts = [f"{sym}:"]
            # high_conviction trades are open-market officer purchases (code=P)
            # — exclude grants/RSU vests/option exercises from this block.
            for t in [x for x in sym_form4 if x.get("high_conviction")][:1]:
                title = t.get("officer_title") or t.get("insider_name", "?")
                value = t.get("value_usd") or 0
                shares = t.get("transaction_shares") or 0
                desc = (
                    f"{title} open-market purchase {int(shares):,} sh "
                    f"=${value:,.0f} on {t.get('filing_date','?')}"
                )
                parts.append(desc)
            for t in [x for x in sym_cong if x.get("action") == "buy"][:1]:
                parts.append(
                    f"{t.get('politician','?')} ({t.get('committee','') or t.get('chamber','?')}) "
                    f"bought {t.get('amount_range','?')} on {t.get('filing_date','?')}"
                )
            high_conv_lines.append("  " + "  |  ".join(parts))
        else:
            for t in [x for x in sym_cong if x.get("action") == "buy"][:1]:
                congress_lines.append(
                    f"  {sym}: {t.get('politician','?')} "
                    f"({t.get('committee','') or t.get('chamber','?')}) "
                    f"bought {t.get('amount_range','?')} on {t.get('filing_date','?')}"
                )
            for t in sym_form4[:1]:
                code = t.get("transaction_code") or "?"
                code_label = {
                    "P": "purchase", "S": "sale", "A": "grant",
                    "M": "option exercise", "F": "tax-withholding",
                    "G": "gift", "D": "disposition",
                }.get(code, f"code={code}")
                title = t.get("officer_title") or t.get("role", "insider")
                shares = t.get("transaction_shares")
                price  = t.get("transaction_price")
                if shares and price:
                    detail = f" {int(shares):,} sh @ ${price:.2f} = ${shares*price:,.0f}"
                else:
                    detail = ""
                form4_lines.append(
                    f"  {sym}: {t.get('insider_name','?')} ({title}) "
                    f"{code_label}{detail} on {t.get('filing_date','?')} "
                    f"({t.get('days_since_filing','?')} days ago)"
                )

        for t in [x for x in sym_cong if x.get("action") == "sell"][:1]:
            sell_lines.append(
                f"  {sym}: {t.get('politician','?')} SOLD {t.get('amount_range','?')} "
                f"on {t.get('filing_date','?')}"
            )

    sections = []
    if high_conv_lines:
        sections += ["[HIGH CONVICTION]", *high_conv_lines]
    if congress_lines:
        sections += ["[CONGRESSIONAL ACTIVITY]", *congress_lines]
    if form4_lines:
        sections += ["[RECENT FORM 4 PURCHASES]", *form4_lines]
    if sell_lines:
        sections += ["[CONGRESSIONAL SELLS — caution signal]", *sell_lines]

    if not sections:
        return "  (no significant insider/congressional activity for watchlist symbols)"

    sections.append("\n  Note: 45-day disclosure lag on congressional trades. Form 4 = 2-day lag.")
    return "\n".join(sections)


# ── Shared helper ──────────────────────────────────────────────────────────────

def _filter_by_symbols(
    trades: list[dict],
    symbols: list[str],
    days_back: int,
    age_key: str,
) -> list[dict]:
    sym_set = {s.upper() for s in symbols}
    return [
        t for t in trades
        if t.get("ticker", "").upper() in sym_set
        and t.get(age_key, 9999) <= days_back
    ]
