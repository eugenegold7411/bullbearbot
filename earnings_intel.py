"""
earnings_intel.py — Earnings call transcript ingestion and analysis.

Fetches most recent 8-K earnings filing text from SEC EDGAR.
Runs a Claude analysis to extract trading-relevant insights.
Only activates when a watchlist symbol is within 3 days of earnings.
"""

import json
import os
import re
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import requests
from dotenv import load_dotenv

from log_setup import get_logger

load_dotenv()
log = get_logger(__name__)

_BASE_DIR     = Path(__file__).parent
_EARNINGS_DIR = _BASE_DIR / "data" / "earnings"
_TIMEOUT      = 20
_MAX_CHARS    = 30_000   # ~6 000 words — enough for transcript without overload
_TRANSCRIPT_CACHE_TTL_H = 24   # 24h keeps transcript available all trading day
_RATE_LIMIT_DELAY = 0.15       # SEC limits to 10 req/s; 0.15s is safe

_SEC_HEADERS = {
    "User-Agent":      "trading-bot research@tradingbot.ai",
    "Accept-Encoding": "gzip, deflate",
}

# CIK numbers for watchlist symbols that report US earnings via SEC EDGAR.
# Foreign private issuers (TSM, ASML, FXI, EWJ, EWM, ECH) file 20-F/6-K on
# different schedules and are not included here.
_CIK_MAP: dict[str, str] = {
    "GOOGL": "1652044",
    "GOOG":  "1652044",
    "AMZN":  "1018724",
    "MSFT":  "789019",
    "META":  "1326801",
    "AAPL":  "320193",
    "NVDA":  "1045810",
    "TSLA":  "1318605",
    "JPM":   "19617",
    "GS":    "886982",
    "WMT":   "104169",
    "JNJ":   "200406",
    "LLY":   "59478",
    "PLTR":  "1321655",
    "XOM":   "34088",
    "CVX":   "93410",
    "LMT":   "936468",
    "RTX":   "101829",
    "CRWV":  "1517396",
    "RKT":   "1728688",
}

_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_MODEL  = "claude-haiku-4-5-20251001"

_ANALYSIS_SYSTEM = """You are a sell-side equity analyst. Extract the most trading-relevant insights from this earnings call transcript or 8-K filing. Focus on:
- Actual vs expected EPS and revenue (beat/miss magnitude)
- Forward guidance (raised/lowered/maintained, specific numbers)
- Management tone (confident/cautious/defensive)
- Key risks mentioned (supply chain, tariffs, competition, macro)
- Surprise elements (anything the market likely didn't expect)
- Analyst questions that revealed management concern or evasion

Return ONLY valid JSON with this structure:
{
  "eps_beat_miss": "+12% beat",
  "revenue_beat_miss": "+3% beat",
  "guidance_direction": "raised" | "lowered" | "maintained" | "withdrawn" | "unknown",
  "guidance_detail": "FY guidance raised 8%, above consensus",
  "management_tone": "confident" | "cautious" | "defensive" | "mixed",
  "key_risks": ["risk1", "risk2"],
  "surprise_elements": ["element1"],
  "analyst_sentiment": "positive | negative | neutral",
  "trading_signal": "bullish" | "bearish" | "neutral",
  "one_line_summary": "Clean beat, raised guidance, confident tone — gap-and-go setup"
}"""


# ── SEC EDGAR helpers ──────────────────────────────────────────────────────────

def _get_recent_8k_via_submissions(cik: str, days_back: int = 120) -> list[dict]:
    """Use EDGAR submissions API to get recent 8-K filings with accurate accession numbers."""
    padded = cik.zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{padded}.json"
    try:
        resp = requests.get(url, headers=_SEC_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        recent = data.get("filings", {}).get("recent", {})
        forms  = recent.get("form", [])
        dates  = recent.get("filingDate", [])
        accnos = recent.get("accessionNumber", [])
        cutoff = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        results = []
        for f, d, a in zip(forms, dates, accnos):
            if f == "8-K" and d >= cutoff:
                results.append({"cik": cik, "accession_no": a, "file_date": d})
        return results
    except Exception as exc:
        log.debug("[EARNINGS] Submissions API failed CIK=%s: %s", cik, exc)
        return []


def _find_exhibit_url(cik: str, acc: str) -> str:
    """Find the EX-99.1 press release URL in the 8-K filing index."""
    acc_nodash = acc.replace("-", "")
    index_url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik}/"
        f"{acc_nodash}/{acc}-index.htm"
    )
    try:
        resp = requests.get(index_url, headers=_SEC_HEADERS, timeout=_TIMEOUT)
        if resp.status_code != 200:
            return ""
        links = re.findall(
            r'href="(/Archives/edgar/data/[^"]+\.(?:htm|txt))"', resp.text, re.I
        )
        # Prefer named exhibit99/ex-99 patterns (press release)
        for link in links:
            lower = link.lower()
            if any(p in lower for p in (
                "exhibit99", "ex-99", "ex991", "ex99",
                "pressrelease", "press_release", "earnings",
            )):
                return "https://www.sec.gov" + link
        # Fall back to first non-XBRL htm document
        for link in links:
            lower = link.lower()
            if ".htm" in lower and not any(
                s in lower for s in ("_lab.", "_pre.", "_htm.", "_def.", "_cal.", ".xsd")
            ):
                return "https://www.sec.gov" + link
    except Exception as exc:
        log.debug("[EARNINGS] Index fetch failed %s/%s: %s", cik, acc, exc)
    return ""


def _search_8k_filings(symbol: str, days_back: int = 120) -> list[dict]:
    """Search EDGAR for recent 8-K filings.

    Uses submissions API when CIK is known (accurate accession numbers).
    Falls back to EFTS full-text search with corrected field extraction.
    Returns list of dicts with 'cik', 'accession_no', 'file_date' keys.
    """
    cik = _CIK_MAP.get(symbol)
    if cik:
        results = _get_recent_8k_via_submissions(cik, days_back)
        if results:
            return results

    # EFTS fallback for symbols not in _CIK_MAP
    end_dt   = datetime.now().strftime("%Y-%m-%d")
    start_dt = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    url = (
        "https://efts.sec.gov/LATEST/search-index"
        f"?q=%22{symbol}%22&forms=8-K"
        f"&dateRange=custom&startdt={start_dt}&enddt={end_dt}"
    )
    try:
        resp = requests.get(url, headers=_SEC_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        hits = resp.json().get("hits", {}).get("hits", [])
        results = []
        for h in hits:
            src     = h.get("_source", {})
            adsh    = src.get("adsh", "")        # EDGAR uses 'adsh', not 'accession_no'
            ciks    = src.get("ciks", [])
            hit_cik = ciks[0].lstrip("0") if ciks else ""
            if adsh and hit_cik:
                results.append({
                    "cik":          hit_cik,
                    "accession_no": adsh,
                    "file_date":    src.get("file_date", ""),
                })
        return results
    except Exception as exc:
        log.debug("[EARNINGS] EDGAR 8-K search failed %s: %s", symbol, exc)
        return []


def _fetch_filing_text(accession_no: str, cik: str) -> str:
    """Download the EX-99.1 press release from an 8-K filing."""
    # First: find and fetch the exhibit (EX-99.1 press release)
    exhibit_url = _find_exhibit_url(cik, accession_no)
    if exhibit_url:
        try:
            _time.sleep(_RATE_LIMIT_DELAY)
            resp = requests.get(exhibit_url, headers=_SEC_HEADERS, timeout=_TIMEOUT)
            if resp.status_code == 200 and len(resp.text) > 500:
                return resp.text
        except Exception as exc:
            log.debug("[EARNINGS] Exhibit fetch failed %s: %s", exhibit_url, exc)

    # Fall back: raw .txt filing
    acc_nodash = accession_no.replace("-", "")
    try:
        _time.sleep(_RATE_LIMIT_DELAY)
        txt_url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik}/"
            f"{acc_nodash}/{accession_no}.txt"
        )
        resp = requests.get(txt_url, headers=_SEC_HEADERS, timeout=_TIMEOUT)
        if resp.status_code == 200:
            return resp.text
    except Exception:
        pass

    return ""


def _clean_transcript(raw: str) -> str:
    """Remove HTML/boilerplate, keep substantive content, cap at _MAX_CHARS."""
    if not raw:
        return ""

    # Strip HTML tags
    text = re.sub(r"<[^>]+>", " ", raw)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    # Remove common boilerplate patterns
    boilerplate = [
        r"This transcript is provided for informational purposes only.*",
        r"Safe harbor.*forward.looking statements.*",
        r"This document is the property of.*",
        r"EDGAR\s+Filing.*",
        r"Exhibit\s+\d+\.\d+\s*$",
    ]
    for pat in boilerplate:
        text = re.sub(pat, "", text, flags=re.I | re.S)

    return text[:_MAX_CHARS]


# ── Cache ──────────────────────────────────────────────────────────────────────

def _cache_path(symbol: str) -> Path:
    """Cache file path — refresh each quarter."""
    quarter = f"{datetime.now().year}Q{(datetime.now().month - 1) // 3 + 1}"
    return _EARNINGS_DIR / f"{symbol}_{quarter}_analysis.json"


def _load_cached_analysis(symbol: str) -> dict | None:
    p = _cache_path(symbol)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return None


def _save_analysis(symbol: str, analysis: dict) -> None:
    _EARNINGS_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(symbol).write_text(json.dumps(analysis, indent=2))


# ── Public API ─────────────────────────────────────────────────────────────────

def _transcript_cache_path(symbol: str) -> Path:
    return _EARNINGS_DIR / f"{symbol}_transcript_cache.json"


def _load_transcript_cache(symbol: str) -> str:
    """Return cached transcript if fresh (< 24h) and not a yfinance stub, else ''."""
    p = _transcript_cache_path(symbol)
    if not p.exists():
        return ""
    try:
        data = json.loads(p.read_text())
        cached_at = datetime.fromisoformat(data["cached_at"])
        if (datetime.now(timezone.utc) - cached_at).total_seconds() < _TRANSCRIPT_CACHE_TTL_H * 3600:
            transcript = data.get("transcript", "")
            # Treat yfinance stubs as stale so EDGAR is retried next window
            if transcript.startswith("yfinance fundamentals for"):
                return ""
            return transcript
    except Exception:
        pass
    return ""


def _save_transcript_cache(symbol: str, transcript: str) -> None:
    _EARNINGS_DIR.mkdir(parents=True, exist_ok=True)
    p = _transcript_cache_path(symbol)
    try:
        p.write_text(json.dumps({
            "symbol":    symbol,
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "transcript": transcript,
        }, indent=2))
    except Exception as exc:
        log.debug("[EARNINGS] Failed to write transcript cache for %s: %s", symbol, exc)


def _in_earnings_fetch_window(_now: datetime | None = None) -> bool:
    """Return True during pre-market (4:00–9:30 AM ET) or post-market (4:15 PM–11:00 PM ET).

    _now is injectable for testing (defaults to current local time).
    """
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415
        now_et = (
            _now.astimezone(ZoneInfo("America/New_York"))
            if _now is not None
            else datetime.now(ZoneInfo("America/New_York"))
        )
        total_min = now_et.hour * 60 + now_et.minute
        pre_market  = (4 * 60) <= total_min <= (9 * 60 + 30)
        post_market = (16 * 60 + 15) <= total_min <= (23 * 60)
        return pre_market or post_market
    except Exception:
        return True  # fail-open: always fetch if timezone unavailable


def _yfinance_fallback(symbol: str) -> str:
    """Return yfinance fundamentals as a stub.

    Not cached in the transcript store so EDGAR is retried on the next window cycle.
    """
    try:
        import yfinance as yf  # noqa: PLC0415
        t    = yf.Ticker(symbol)
        info = t.info
        parts = []
        for key in ("trailingEps", "forwardEps", "revenueGrowth", "earningsGrowth",
                    "grossMargins", "operatingMargins", "returnOnEquity"):
            val = info.get(key)
            if val is not None:
                parts.append(f"{key}: {val}")
        if parts:
            return f"yfinance fundamentals for {symbol}: " + "  |  ".join(parts)
    except Exception:
        pass
    return ""


def fetch_earnings_transcript(symbol: str, quarters_back: int = 1) -> str:
    """Fetch the most recent earnings call / 8-K filing text from SEC EDGAR.

    - Cache-first: returns any fresh (< 24h) non-stub transcript without hitting EDGAR.
    - Only fetches from EDGAR during pre/post-market windows.
    - Returns cleaned press release text (typically 5,000–40,000 chars) or "".
    - On EDGAR failure: returns yfinance fundamentals stub WITHOUT caching it,
      so EDGAR is retried on the next window cycle.
    """
    # Cache-first: use any fresh real transcript without hitting EDGAR
    cached = _load_transcript_cache(symbol)
    if cached:
        return cached

    # No fresh cache — only attempt EDGAR during the fetch window
    if not _in_earnings_fetch_window():
        log.debug("[EARNINGS] Outside fetch window and no cache — skipping %s", symbol)
        return ""

    log.info("[EARNINGS] Fetching transcript for %s", symbol)

    hits = _search_8k_filings(symbol, days_back=120)
    if not hits:
        log.debug("[EARNINGS] No 8-K hits for %s", symbol)
        return _yfinance_fallback(symbol)

    for hit in hits[:3]:
        try:
            acc = hit.get("accession_no", "")
            cik = hit.get("cik", "")
            if not acc or not cik:
                continue
            _time.sleep(_RATE_LIMIT_DELAY)
            raw = _fetch_filing_text(acc, cik)
            if raw and len(raw) > 500:
                cleaned = _clean_transcript(raw)
                if cleaned and len(cleaned) > 500:
                    log.info(
                        "[EARNINGS] Transcript fetched for %s: %d chars", symbol, len(cleaned)
                    )
                    _save_transcript_cache(symbol, cleaned)
                    return cleaned
        except Exception as exc:
            log.debug("[EARNINGS] Filing fetch failed %s: %s", symbol, exc)

    log.debug("[EARNINGS] EDGAR yielded no content for %s — using yfinance fallback", symbol)
    return _yfinance_fallback(symbol)


# Alias used by test suite and callers that import the shorter name
get_earnings_transcript = fetch_earnings_transcript


def analyze_earnings_transcript(symbol: str, transcript: str) -> dict:
    """
    Single Claude call to extract trading-relevant insights from earnings transcript.
    Returns structured analysis dict.
    """
    if not transcript:
        return {}

    if transcript.startswith("yfinance fundamentals for"):
        log.debug("[EARNINGS] %s: yfinance stub only — skipping analysis (no real transcript)", symbol)
        return {}

    # Check cache first
    cached = _load_cached_analysis(symbol)
    if cached:
        log.debug("[EARNINGS] Using cached analysis for %s", symbol)
        return cached

    log.info("[EARNINGS] Analyzing transcript for %s (%d chars)", symbol, len(transcript))
    try:
        response = _claude.messages.create(
            model=_MODEL,
            max_tokens=800,
            system=_ANALYSIS_SYSTEM,
            messages=[{
                "role":    "user",
                "content": (
                    f"Analyze this earnings transcript/filing for {symbol}.\n\n"
                    f"{transcript[:_MAX_CHARS]}"
                ),
            }],
        )
        raw = response.content[0].text.strip()
        if not raw:
            log.debug("[EARNINGS] %s: no transcript available (market hours)", symbol)
            return {}
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        try:
            analysis = json.loads(raw)
        except json.JSONDecodeError as _jde:
            log.warning("[EARNINGS] Analysis returned non-JSON for %s: %s", symbol, _jde)
            sentinel = {
                "symbol":             symbol,
                "analyzed_at":        datetime.now().isoformat(),
                "parse_failed":       True,
                "reason":             "non-JSON response from model",
                "eps_beat_miss":      "unknown",
                "revenue_beat_miss":  "unknown",
                "guidance_direction": "unknown",
                "tone":               "unknown",
                "signal":             "neutral",
                "confidence":         "low",
            }
            _save_analysis(symbol, sentinel)
            return sentinel
        analysis["symbol"]       = symbol
        analysis["analyzed_at"]  = datetime.now().isoformat()
        _save_analysis(symbol, analysis)
        return analysis
    except Exception as exc:
        log.warning("[EARNINGS] Analysis failed %s: %s", symbol, exc)
        return {}


def batch_analyze_transcripts(
    symbol_transcript_pairs: list[tuple[str, str]],
) -> dict[str, dict]:
    """
    Analyze multiple earnings transcripts in one Anthropic Batch API call.

    Only submits pairs where:
    - transcript is non-empty
    - no cached analysis exists for this quarter

    Returns {symbol: analysis_dict} for all successfully analyzed symbols.
    Falls back gracefully — skips any symbol that errors.
    Called from data_warehouse.py after fetching transcripts.
    """
    if not symbol_transcript_pairs:
        return {}

    # Filter to uncached symbols
    to_analyze = [
        (sym, trans)
        for sym, trans in symbol_transcript_pairs
        if trans and not _load_cached_analysis(sym)
    ]
    if not to_analyze:
        log.debug("[EARNINGS] All transcripts already cached — batch skipped")
        return {}

    log.info("[EARNINGS] Batch analyzing %d transcripts", len(to_analyze))

    try:
        batch = _claude.beta.messages.batches.create(
            requests=[
                {
                    "custom_id": sym,
                    "params": {
                        "model":      _MODEL,
                        "max_tokens": 800,
                        "system":     _ANALYSIS_SYSTEM,
                        "messages": [{
                            "role":    "user",
                            "content": (
                                f"Analyze this earnings transcript/filing for {sym}.\n\n"
                                f"{trans[:_MAX_CHARS]}"
                            ),
                        }],
                    },
                }
                for sym, trans in to_analyze
            ]
        )
        log.info("[EARNINGS] Batch created: id=%s", batch.id)
    except Exception as exc:
        log.warning("[EARNINGS] Batch create failed: %s — skipping batch", exc)
        return {}

    # Poll until ended (max 10 minutes = 40 × 15s)
    import time
    for _ in range(40):
        time.sleep(15)
        try:
            batch = _claude.beta.messages.batches.retrieve(batch.id)
        except Exception as exc:
            log.warning("[EARNINGS] Batch retrieve failed: %s", exc)
            continue
        if batch.processing_status == "ended":
            break
    else:
        log.warning("[EARNINGS] Batch timed out — transcripts will be analyzed on-demand")
        return {}

    # Collect and cache results
    results: dict[str, dict] = {}
    try:
        for result in _claude.beta.messages.batches.results(batch.id):
            sym = result.custom_id
            if result.result.type != "succeeded":
                log.debug("[EARNINGS] Batch result %s: error=%s", sym, result.result.type)
                continue
            try:
                raw = result.result.message.content[0].text.strip()
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                analysis = json.loads(raw)
                analysis["symbol"]      = sym
                analysis["analyzed_at"] = datetime.now().isoformat()
                _save_analysis(sym, analysis)
                results[sym] = analysis
                log.info("[EARNINGS] Batch: saved analysis for %s", sym)
            except Exception as exc:
                log.debug("[EARNINGS] Batch parse failed for %s: %s", sym, exc)
    except Exception as exc:
        log.warning("[EARNINGS] Batch results collection failed: %s", exc)

    return results


def get_earnings_intel_section(symbol: str, days_to_earnings: int) -> str:
    """
    Return a prompt section for a symbol near earnings.
    Only called when within 3 days of earnings.
    """
    try:
        transcript = fetch_earnings_transcript(symbol)
        if not transcript:
            return f"  {symbol}: No earnings transcript available — monitor for guidance."

        if transcript.startswith("yfinance fundamentals for"):
            # Real transcript unavailable; surface fundamentals so Sonnet has some context
            data_part = transcript[len(f"yfinance fundamentals for {symbol}: "):]
            return (
                f"  {symbol}: [EDGAR transcript unavailable — fundamentals only]\n"
                f"  {data_part[:300]}"
            )

        if len(transcript) < 5000:
            log.debug("[EARNINGS] %s: short filing (%d chars) — skipping Claude analysis", symbol, len(transcript))
            return f"  {symbol}: Short press release only — full transcript not yet available."

        analysis = analyze_earnings_transcript(symbol, transcript)
        if not analysis:
            return f"  {symbol}: Transcript fetched but analysis failed."

        timing = (f"reports in {days_to_earnings} days" if days_to_earnings > 0
                  else f"{abs(days_to_earnings)} days ago")
        lines = [
            f"  {symbol} ({timing}) — last quarter:",
            f"  Signal: {analysis.get('trading_signal','?').upper()}  "
            f"| Tone: {analysis.get('management_tone','?')}",
            f"  Summary: {analysis.get('one_line_summary','?')}",
            f"  Guidance: {analysis.get('guidance_detail','?')}",
        ]
        risks = analysis.get("key_risks", [])
        if risks:
            lines.append(f"  Key risks: {', '.join(risks[:3])}")
        surprises = analysis.get("surprise_elements", [])
        if surprises:
            lines.append(f"  Surprises: {', '.join(surprises[:2])}")
        return "\n".join(lines)

    except Exception as exc:
        log.warning("[EARNINGS] Intel section failed %s: %s", symbol, exc)
        return f"  {symbol}: Earnings intelligence unavailable."
