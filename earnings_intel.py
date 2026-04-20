"""
earnings_intel.py — Earnings call transcript ingestion and analysis.

Fetches most recent 8-K earnings filing text from SEC EDGAR.
Runs a Claude analysis to extract trading-relevant insights.
Only activates when a watchlist symbol is within 3 days of earnings.
"""

import json
import os
import re
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

_SEC_HEADERS = {
    "User-Agent":      "trading-bot research@tradingbot.ai",
    "Accept-Encoding": "gzip, deflate",
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

def _search_8k_filings(symbol: str, days_back: int = 90) -> list[dict]:
    """Search EDGAR for recent 8-K filings for this symbol."""
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
        return resp.json().get("hits", {}).get("hits", [])
    except Exception as exc:
        log.debug("[EARNINGS] EDGAR 8-K search failed %s: %s", symbol, exc)
        return []


def _fetch_filing_text(accession_no: str, cik: str) -> str:
    """Download the primary document from an 8-K filing."""
    # Normalise accession number
    acc = accession_no.replace("-", "")
    # Try the filing index first
    index_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{accession_no}-index.htm"
    try:
        resp = requests.get(index_url, headers=_SEC_HEADERS, timeout=_TIMEOUT)
        # Find .txt or .htm document link
        links = re.findall(r'href="(/Archives/edgar/data/[^"]+\.(?:txt|htm))"', resp.text, re.I)
        if links:
            doc_url = "https://www.sec.gov" + links[0]
            doc_resp = requests.get(doc_url, headers=_SEC_HEADERS, timeout=_TIMEOUT)
            return doc_resp.text
    except Exception:
        pass

    # Fall back: fetch raw filing text directly
    try:
        txt_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{accession_no}.txt"
        resp = requests.get(txt_url, headers=_SEC_HEADERS, timeout=_TIMEOUT)
        return resp.text
    except Exception:
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
    """Return cached transcript if it exists and is <6 hours old, else ''."""
    p = _transcript_cache_path(symbol)
    if not p.exists():
        return ""
    try:
        data = json.loads(p.read_text())
        cached_at = datetime.fromisoformat(data["cached_at"])
        if (datetime.now(timezone.utc) - cached_at).total_seconds() < 6 * 3600:
            return data.get("transcript", "")
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


def _in_earnings_fetch_window() -> bool:
    """Return True only during pre-market (4:00–9:15 AM ET) or post-market (4:15–8:00 PM ET)."""
    try:
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo("America/New_York"))
        h, m = now_et.hour, now_et.minute
        total_min = h * 60 + m
        pre_market  = (4 * 60) <= total_min <= (9 * 60 + 15)
        post_market = (16 * 60 + 15) <= total_min <= (20 * 60)
        return pre_market or post_market
    except Exception:
        return True  # fail-open: always fetch if timezone unavailable


def fetch_earnings_transcript(symbol: str, quarters_back: int = 1) -> str:
    """
    Fetch the most recent earnings call / 8-K filing text from SEC EDGAR.
    Only fetches during pre-market (4:00–9:15 AM ET) or post-market (4:15–8:00 PM ET).
    Caches results for 6 hours. Returns cleaned text or "" on failure.
    """
    # Always try cache first regardless of window
    cached = _load_transcript_cache(symbol)

    if not _in_earnings_fetch_window():
        if cached:
            log.debug("[EARNINGS] Outside fetch window — using cache for %s", symbol)
            return cached
        log.debug("[EARNINGS] Outside fetch window and no cache — skipping %s", symbol)
        return ""

    log.info("[EARNINGS] Fetching transcript for %s", symbol)
    hits = _search_8k_filings(symbol, days_back=120)
    if not hits:
        log.debug("[EARNINGS] No 8-K hits for %s", symbol)
        return ""

    for hit in hits[:3]:
        try:
            src = hit.get("_source", {})
            acc = src.get("accession_no", "")
            cik = src.get("entity_id", "") or src.get("file_num", "")

            # Try to extract CIK from the entity URL or accession number
            if not cik:
                src.get("file_date", "")
                # Try inline id
                entity_id = str(src.get("id", ""))
                if "/" in entity_id:
                    cik = entity_id.split("/")[0].lstrip("0")

            if not acc:
                continue

            raw = _fetch_filing_text(acc, cik or "0")
            if raw and len(raw) > 500:
                cleaned = _clean_transcript(raw)
                if cleaned:
                    log.info("[EARNINGS] Transcript fetched for %s: %d chars", symbol, len(cleaned))
                    _save_transcript_cache(symbol, cleaned)
                    return cleaned
        except Exception as exc:
            log.debug("[EARNINGS] Filing fetch failed %s: %s", symbol, exc)

    # Fallback: yfinance earnings summary
    try:
        import yfinance as yf
        t = yf.Ticker(symbol)
        info = t.info
        summary_parts = []
        for key in ("trailingEps", "forwardEps", "revenueGrowth", "earningsGrowth",
                    "grossMargins", "operatingMargins", "returnOnEquity"):
            val = info.get(key)
            if val is not None:
                summary_parts.append(f"{key}: {val}")
        if summary_parts:
            summary = f"yfinance fundamentals for {symbol}: " + "  |  ".join(summary_parts)
            _save_transcript_cache(symbol, summary)
            return summary
    except Exception:
        pass

    return ""


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
