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
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

    log.info("[INSIDER] Fetching congressional trades from Lambda Finance")
    try:
        resp = requests.get(
            "https://api.lambdafin.com/api/congressional/recent",
            timeout=_TIMEOUT,
            headers={"User-Agent": "trading-bot/1.0"},
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
            ticker = (item.get("ticker") or item.get("symbol") or "").upper().strip()
            if not ticker or len(ticker) > 6 or not ticker.isalpha():
                continue

            date_str = (
                item.get("transaction_date") or item.get("filing_date") or
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

            tx = (item.get("transaction_type") or item.get("type") or "").lower()
            action = (
                "buy"  if any(k in tx for k in ("purchas", "buy", "acqui")) else
                "sell" if any(k in tx for k in ("sale", "sell", "dispose")) else
                tx or "unknown"
            )

            trades.append({
                "ticker":          ticker,
                "politician":      (item.get("politician") or item.get("representative") or
                                    item.get("name") or "Unknown"),
                "party":           item.get("party") or "",
                "chamber":         item.get("chamber") or "",
                "committee":       item.get("committee") or item.get("committees") or "",
                "action":          action,
                "amount_range":    item.get("amount") or item.get("amount_range") or "",
                "filing_date":     date_str[:10] if date_str else "",
                "days_since_trade": days_ago,
            })
        except Exception:
            continue

    return trades


# ── SEC Form 4 Insider Trades ──────────────────────────────────────────────────

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

    for symbol in symbols[:25]:
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
            category    = src.get("category", "insider")

            days_ago = 999
            if filing_date:
                try:
                    days_ago = max(0, (datetime.now() - datetime.strptime(filing_date[:10], "%Y-%m-%d")).days)
                except ValueError:
                    pass

            # Only keep recent purchases by named insiders
            # We mark high_conviction for CEO/CFO/President roles
            role_lower = category.lower()
            is_csuite  = any(k in role_lower for k in ("chief", "ceo", "cfo", "president", "coo"))

            trades.append({
                "ticker":            symbol,
                "insider_name":      entity_name,
                "role":              category,
                "shares_purchased":  None,   # not parsed at filing index level
                "price":             None,
                "value_usd":         None,
                "filing_date":       filing_date[:10] if filing_date else "",
                "days_since_filing": days_ago,
                "high_conviction":   is_csuite,
                "accession_number":  src.get("accession_no", ""),
            })
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
            for t in [x for x in sym_form4 if x.get("high_conviction")][:1]:
                parts.append(f"Insider {t.get('role','?')} filed Form 4 on {t.get('filing_date','?')}")
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
                form4_lines.append(
                    f"  {sym}: {t.get('insider_name','?')} ({t.get('role','?')}) "
                    f"filed Form 4 on {t.get('filing_date','?')} "
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
