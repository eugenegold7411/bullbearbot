"""
macro_wire.py — Reuters/AP RSS macro headline fetcher and classifier.

Two-stage system:
  Stage 1: Keyword scoring (no API, instant)
  Stage 2: Haiku classifier for articles scoring >= 5 (batched, single call)

Three-tier storage:
  TIER 1 — data/macro_wire/live_cache.json     (overwritten each fetch, prompt source)
  TIER 2 — data/macro_wire/significant_events.jsonl (append-only, permanent)
  TIER 3 — data/macro_wire/daily_digest/YYYY-MM-DD.json (written once at 4 PM)

Usage:
  from macro_wire import refresh_macro_wire, build_macro_wire_section
"""

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import anthropic
import feedparser
from dotenv import load_dotenv

from log_setup import get_logger

load_dotenv()
log = get_logger(__name__)
ET  = ZoneInfo("America/New_York")
_SEEN_IDS_FILE = Path("data/macro_wire/seen_ids.json")


def _load_seen_ids() -> set:
    """Load seen article IDs, reset daily at midnight ET."""
    try:
        if _SEEN_IDS_FILE.exists():
            data = json.loads(_SEEN_IDS_FILE.read_text())
            from datetime import datetime
            from zoneinfo import ZoneInfo
            today = datetime.now(
                ZoneInfo("America/New_York")
            ).date().isoformat()
            if data.get("date") != today:
                log.info("Macro wire: resetting seen_ids "
                         "for new day")
                return set()
            return set(data.get("ids", []))
    except Exception:
        pass
    return set()


def _save_seen_ids(seen: set) -> None:
    """Save seen article IDs with today's date."""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        today = datetime.now(
            ZoneInfo("America/New_York")
        ).date().isoformat()
        _SEEN_IDS_FILE.write_text(json.dumps(
            {"date": today, "ids": list(seen)[-500:]},
            indent=2))
    except Exception as e:
        log.warning("seen_ids save failed: %s", e)


def _article_id(article: dict) -> str:
    """Stable ID for an article — URL preferred."""
    url = article.get("url", article.get("link", ""))
    title = article.get("title", article.get("headline", ""))
    return url if url else title[:100]



# ── Paths ─────────────────────────────────────────────────────────────────────
_BASE_DIR  = Path(__file__).parent
MACRO_DIR  = _BASE_DIR / "data" / "macro_wire"
LIVE_CACHE = MACRO_DIR / "live_cache.json"
SIG_EVENTS = MACRO_DIR / "significant_events.jsonl"
DIGEST_DIR = MACRO_DIR / "daily_digest"

_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
MODEL   = "claude-haiku-4-5-20251001"

# ── RSS Sources ───────────────────────────────────────────────────────────────
# Investing.com dropped 2026-04-25: floods feed with Form 13G/144 filing press
# releases that score 0 and crowd out real macro signal.
RSS_FEEDS = [
    ("BBC",          "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("NYTimes",      "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml"),
    ("CNBC-Econ",    "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("MarketWatch",  "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines"),
    ("WSJ",          "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),
    ("FT",           "https://www.ft.com/world?format=rss"),
    ("Yahoo",        "https://finance.yahoo.com/news/rssindex"),
]


# ── Filing-noise rejection filter ─────────────────────────────────────────────
# Drops Form 13G/13D/144/144A press releases, earnings call transcripts, and
# the "31 Sloths Acquired/Died/Born" class of pseudo-news that fires the
# critical keyword tier (via "died", "acquired") without any market relevance.
_FILING_NOISE_RE = re.compile(
    r'^Form\s+\d+|^SC\s+13[GD]\b|^Amendment\s+to\s+Form|'
    r'Earnings\s+call\s+transcript:|^\d+\s+\w+\s+(Acquired|Died|Born)\b',
    re.IGNORECASE,
)

# ── Keyword tiers ─────────────────────────────────────────────────────────────
KEYWORD_TIERS = {
    "critical": [
        "Fed", "FOMC", "Federal Reserve", "Powell", "Waller",
        "rate decision", "rate hike", "rate cut", "pivot",
        "quantitative tightening", "QT", "QE",
        "war", "military", "blockade", "sanctions", "default",
        "circuit breaker", "market halt", "flash crash",
        "bank failure", "bank run", "bailout", "contagion",
        "systemic risk", "financial crisis",
        "emergency meeting", "debt default", "sovereign default",
    ],
    "high": [
        "oil", "crude", "OPEC", "Strait", "Hormuz", "pipeline",
        "natural gas", "LNG", "wheat", "grain", "food prices",
        "copper", "lithium", "rare earth",
        "inflation", "CPI", "PCE", "PPI", "GDP", "recession",
        "stagflation", "deflation", "jobs report", "payrolls",
        "unemployment", "labor market", "consumer spending",
        "retail sales", "housing starts", "PMI",
        "China", "Taiwan", "Russia", "Ukraine", "Israel",
        "Iran", "North Korea", "NATO", "nuclear", "missile",
        "attack", "invasion", "tariff", "trade war", "embargo",
        "semiconductor", "chip ban", "export controls",
        "AI regulation", "antitrust", "Big Tech",
        "bankruptcy", "Chapter 11", "fraud", "SEC charges",
        "accounting scandal", "restatement",
    ],
    "medium": [
        "dollar", "DXY", "treasury", "yield", "yield curve",
        "bond", "debt ceiling", "deficit", "spending bill",
        "budget", "stimulus", "fiscal",
        "IMF", "World Bank", "G7", "G20", "BRICS", "WTO",
        "ECB", "Bank of England", "Bank of Japan", "PBOC",
        "supply chain", "shortage", "inventory", "logistics",
        "shipping", "freight", "port",
        "tech layoffs", "hiring freeze", "earnings warning",
        "profit warning", "guidance cut", "guidance raise",
        "merger", "acquisition", "IPO", "spinoff",
        "buyback", "dividend cut", "dividend raise",
        "recession fears", "growth fears", "slowdown",
        "soft landing", "hard landing", "bear market",
        "correction", "rally", "selloff", "volatility spike",
    ],
    "low": [
        "economy", "economic", "market", "stocks", "equities",
        "prices", "costs", "growth", "output", "demand",
        "consumer", "business", "corporate", "earnings",
        "revenue", "profit", "margin", "outlook", "forecast",
    ],
}

TIER_SCORES = {"critical": 4, "high": 2, "medium": 1, "low": 0.5}

# ── Last fetch timestamp (module-level throttle) ──────────────────────────────
_last_fetch_ts: float = 0.0


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_ts(ts_str: str) -> datetime:
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return datetime.now(timezone.utc) - timedelta(days=1)


def _matches_keyword(kw: str, text: str) -> bool:
    """
    Word-boundary match for single-word keywords; substring for multi-word phrases.
    Prevents 'war' matching 'Warsh/warehouse/warns' and 'QE' matching 'Marqeta'.
    Multi-word phrases (e.g. 'rate cut') are already specific so substring is fine.
    """
    kw_lower = kw.lower()
    if " " in kw_lower:
        return kw_lower in text
    return bool(re.search(r"\b" + re.escape(kw_lower) + r"\b", text))


def _score_article(headline: str, summary: str) -> tuple:
    """Returns (score, highest_tier, keywords_matched)."""
    text = (headline + " " + summary).lower()
    score = 0.0
    matched: list = []
    tier_order = ["critical", "high", "medium", "low"]
    highest_tier = "none"

    for tier in tier_order:
        # Word-boundary match for critical/high (false-positive prone single
        # words like "war", "QE", "Fed"). Medium/low keep substring — they're
        # broad by design and only contribute 0.5-1.0 each.
        use_word_boundary = tier in ("critical", "high")
        for kw in KEYWORD_TIERS[tier]:
            hit = _matches_keyword(kw, text) if use_word_boundary else (kw.lower() in text)
            if hit:
                score += TIER_SCORES[tier]
                matched.append(kw)
                if highest_tier == "none" or tier_order.index(tier) < tier_order.index(highest_tier):
                    highest_tier = tier

    # Relative scaling before cap: compress the top end so routine
    # geopolitical articles don't all cluster at 10.0.
    if score >= 8.0:
        final = 8.0 + (score - 8.0) * 0.2
    elif score >= 5.0:
        final = 5.0 + (score - 5.0) * 0.6
    else:
        final = score

    return round(min(final, 10.0), 2), highest_tier, matched[:10]


# ── RSS fetch ─────────────────────────────────────────────────────────────────

def fetch_macro_wire() -> list:
    """
    Fetch from all RSS feeds, score each article.
    Respects 60-second minimum between fetches.
    """
    global _last_fetch_ts
    now_ts = time.time()

    if now_ts - _last_fetch_ts < 60:
        try:
            return json.loads(LIVE_CACHE.read_text()).get("articles", [])
        except Exception:
            return []

    articles: list = []
    seen_headlines: set = set()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    for source, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:20]:
                headline = (entry.get("title") or "").strip()
                summary  = (entry.get("summary") or entry.get("description") or "").strip()
                link     = entry.get("link", "")

                if not headline or headline in seen_headlines:
                    continue
                # Reject filing/transcript/animal-life-event pseudo-news before scoring
                if _FILING_NOISE_RE.match(headline):
                    continue
                seen_headlines.add(headline)

                published_ts = None
                pub_struct   = entry.get("published_parsed") or entry.get("updated_parsed")
                if pub_struct:
                    try:
                        published_ts = datetime(*pub_struct[:6], tzinfo=timezone.utc)
                    except Exception:
                        pass
                if published_ts is None:
                    published_ts = datetime.now(timezone.utc)

                if published_ts < cutoff:
                    continue

                age_minutes = int((datetime.now(timezone.utc) - published_ts).total_seconds() / 60)
                score, tier, keywords = _score_article(headline, summary)

                articles.append({
                    "source":           source,
                    "headline":         headline,
                    "summary":          summary[:300],
                    "link":             link,
                    "published_at":     published_ts.isoformat(),
                    "age_minutes":      age_minutes,
                    "impact_score":     score,
                    "keyword_tier":     tier,
                    "keywords_matched": keywords,
                    "is_market_moving": None,
                    "direction":        None,
                    "affected_sectors": [],
                    "affected_symbols": [],
                    "urgency":          None,
                    "one_line_summary": None,
                })
        except Exception as exc:
            log.warning("RSS fetch failed for %s: %s", url, exc)

    articles.sort(key=lambda x: x["impact_score"], reverse=True)
    _last_fetch_ts = time.time()
    log.debug("Macro wire: fetched %d articles", len(articles))
    return articles


# ── Haiku classifier ──────────────────────────────────────────────────────────

def classify_articles(articles: list) -> list:
    """
    Single Haiku call classifying all articles with impact_score >= 5.
    Fails open (returns articles unchanged) on any error.
    Never makes one call per article — always batched.
    """
    to_classify = [a for a in articles if a.get("impact_score", 0) >= 5]
    if not to_classify:
        return articles

    items_text = "\n\n".join(
        f"[{i+1}] HEADLINE: {a['headline']}\nSUMMARY: {a['summary'][:200]}"
        for i, a in enumerate(to_classify)
    )

    # Inject the current portfolio symbol set so Haiku can populate
    # affected_symbols with names we actually trade rather than guessing.
    try:
        import watchlist_manager as _wm  # noqa: PLC0415
        _wl = _wm.get_active_watchlist()
        _wl_syms = sorted({
            (s if isinstance(s, str) else s.get("symbol", ""))
            for v in _wl.values() if isinstance(v, list)
            for s in v
            if "/" not in (s if isinstance(s, str) else s.get("symbol", ""))
        })
        _wl_syms = [s for s in _wl_syms if s]
        _watchlist_hint = (
            f"\n\nTracked portfolio symbols: {', '.join(_wl_syms)}\n"
            "Prefer these when populating affected_symbols; emit other tickers "
            "only if directly named in the headline."
        ) if _wl_syms else ""
    except Exception:
        _watchlist_hint = ""

    system_prompt = (
        "You are a financial market news classifier. "
        "Given news headlines and summaries, classify each one. "
        "Return ONLY a JSON array, no markdown, no explanation. "
        "One object per article in input order.\n\n"
        'Each object: {"is_market_moving":true|false,"direction":"bullish"|"bearish"|"mixed"|"neutral",'
        '"affected_sectors":["energy",...],"affected_symbols":["XLE",...],'
        '"urgency":"immediate"|"today"|"this_week"|"background",'
        '"one_line_summary":"<under 10 words>"}'
        + _watchlist_hint
    )

    try:
        resp = _claude.messages.create(
            model=MODEL,
            max_tokens=1500,
            system=[{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{
                "role": "user",
                "content": f"Classify these {len(to_classify)} articles:\n\n{items_text}",
            }],
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        classifications = json.loads(raw)
        if not isinstance(classifications, list):
            raise ValueError("Expected JSON array")

        for i, cls in enumerate(classifications):
            if i >= len(to_classify):
                break
            art = to_classify[i]
            art["is_market_moving"] = cls.get("is_market_moving")
            art["direction"]        = cls.get("direction", "neutral")
            art["affected_sectors"] = cls.get("affected_sectors", [])
            art["affected_symbols"] = cls.get("affected_symbols", [])
            art["urgency"]          = cls.get("urgency", "background")
            art["one_line_summary"] = cls.get("one_line_summary", "")

        try:
            from cost_tracker import get_tracker
            get_tracker().record_api_call(MODEL, resp.usage, caller="macro_wire_classifier")
        except Exception:
            pass
        log.info("Macro wire: classified %d articles via Haiku", len(to_classify))

    except Exception as exc:
        log.warning("Macro wire classifier failed (non-fatal): %s", exc)

    return articles


# ── Watchlist cache + trigger gate ────────────────────────────────────────────

_wl_symbols_cache: set = set()
_wl_symbols_ts: float = 0.0
_WL_CACHE_TTL: float = 300.0  # 5 minutes


def _watchlist_symbols() -> set:
    """Active watchlist symbol set, cached 5 minutes. Excludes crypto pairs."""
    global _wl_symbols_cache, _wl_symbols_ts
    now = time.monotonic()
    if now - _wl_symbols_ts > _WL_CACHE_TTL or not _wl_symbols_cache:
        try:
            import watchlist_manager as _wm  # noqa: PLC0415
            wl = _wm.get_active_watchlist()
            _wl_symbols_cache = {
                (s if isinstance(s, str) else s.get("symbol", ""))
                for v in wl.values() if isinstance(v, list)
                for s in v
                if "/" not in (s if isinstance(s, str) else s.get("symbol", ""))
            }
            _wl_symbols_cache.discard("")
        except Exception:
            pass
        _wl_symbols_ts = now
    return _wl_symbols_cache


def _should_trigger_cycle(score: float, tier: str, affected_symbols: list) -> bool:
    """
    Cycle-trigger gate. Fires when:
      1. High-confidence genuine macro event (score >= 8 AND tier in critical/high), OR
      2. Critical-tier event that explicitly mentions a watchlist symbol (any score)
    Tightened from the legacy `score >= 8 OR tier == "critical"` which fired on
    benign critical-tier substring matches like "war" inside "Warsh".
    """
    if score >= 8.0 and tier in ("critical", "high"):
        return True
    if tier == "critical" and affected_symbols:
        if set(affected_symbols) & _watchlist_symbols():
            return True
    return False


# ── Storage ───────────────────────────────────────────────────────────────────

def save_live_cache(articles: list) -> None:
    """Overwrite live_cache.json with articles from last 2 hours."""
    MACRO_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    recent = [
        a for a in articles
        if _parse_ts(a.get("published_at", "")) >= cutoff
    ]
    data = {
        "updated_at": datetime.now(ET).isoformat(),
        "count":      len(recent),
        "articles":   recent,
    }
    LIVE_CACHE.write_text(json.dumps(data, indent=2))


def save_significant_events(articles: list) -> None:
    """
    Append high-significance articles to significant_events.jsonl.
    Criteria: impact_score >= 7 OR keyword_tier == "critical"
    append-only, never deleted or truncated.
    """
    MACRO_DIR.mkdir(parents=True, exist_ok=True)

    existing_headlines: set = set()
    if SIG_EVENTS.exists():
        for line in SIG_EVENTS.read_text().splitlines():
            try:
                rec = json.loads(line)
                existing_headlines.add(rec.get("headline", ""))
            except Exception:
                pass

    vix_now = None
    try:
        import yfinance as yf
        hist = yf.Ticker("^VIX").history(period="1d")
        if not hist.empty:
            vix_now = round(float(hist["Close"].iloc[-1]), 2)
    except Exception:
        pass

    new_count = 0
    try:
        with SIG_EVENTS.open("a") as f:
            for a in articles:
                headline = a.get("headline", "")
                if headline in existing_headlines:
                    continue
                score = a.get("impact_score", 0)
                tier  = a.get("keyword_tier", "none")
                if score >= 7 or tier == "critical":
                    rec = {
                        "ts":                        datetime.now(ET).isoformat(),
                        "source":                    a.get("source", ""),
                        "headline":                  headline,
                        "summary":                   a.get("summary", ""),
                        "keywords_matched":          a.get("keywords_matched", []),
                        "impact_score":              score,
                        "keyword_tier":              tier,
                        "direction":                 a.get("direction"),
                        "affected_sectors":          a.get("affected_sectors", []),
                        "affected_symbols":          a.get("affected_symbols", []),
                        "urgency":                   a.get("urgency"),
                        "one_line_summary":          a.get("one_line_summary", ""),
                        "vix_at_time":               vix_now,
                        "spx_move_next_30min":       None,
                        "trade_decisions_next_60min":[],
                        "stored_reason":             "critical_keyword" if tier == "critical" else "high_impact_score",
                    }
                    f.write(json.dumps(rec) + "\n")
                    existing_headlines.add(headline)
                    new_count += 1
                    # Trigger an immediate scheduler cycle when the gate says so
                    affected = a.get("affected_symbols") or []
                    if _should_trigger_cycle(score, tier, affected):
                        watchlist_hit = bool(set(affected) & _watchlist_symbols()) if affected else False
                        log.info(
                            "[MACRO_WIRE] trigger fired — score=%.1f tier=%s watchlist_hit=%s headline=%s",
                            score, tier, watchlist_hit, headline[:60],
                        )
                        try:
                            import scheduler as _sched  # noqa: PLC0415
                            _sched.trigger_cycle(
                                f"macro wire: {headline[:80]}"
                                f" (score={score}, tier={tier})"
                            )
                        except Exception as _trig_exc:
                            log.debug("trigger_cycle skipped (non-fatal): %s", _trig_exc)
                        # Critical events also invalidate L1 qualitative context
                        # so the next scheduler pass sees news_hash change and fires
                        # a sweep. We nudge by clearing the last_news_hash module
                        # var; the L1 refresh function rechecks hash on next call.
                        if tier == "critical":
                            try:
                                import scheduler as _sched2  # noqa: PLC0415
                                _sched2._last_qualitative_news_hash = ""
                            except Exception:
                                pass
    except Exception as exc:
        log.warning("save_significant_events failed: %s", exc)

    if new_count:
        log.info("Macro wire: saved %d new significant events", new_count)


def write_overnight_digest(window_hours: int = 12) -> Optional[dict]:
    """
    Synthesise overnight macro intelligence into a structured digest for the
    morning brief (or end-of-day digest at 4:15 PM).

    Reads significant_events.jsonl for the last `window_hours` hours, filters
    to events with `impact_score >= 6` OR `affected_symbols ∩ watchlist`, then
    makes a single Haiku call to produce a compact JSON digest. Writes to
    `data/macro_wire/overnight_digest_YYYY-MM-DD.json`.

    Returns the digest dict, or None when no qualifying events / Haiku failure
    (non-fatal in both cases — the morning brief has a graceful fallback).
    """
    MACRO_DIR.mkdir(parents=True, exist_ok=True)

    now_utc = datetime.now(timezone.utc)
    cutoff  = now_utc - timedelta(hours=window_hours)

    # ── Step 1: read events within the rolling window ───────────────────────
    events: list = []
    if SIG_EVENTS.exists():
        for line in SIG_EVENTS.read_text().splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
                ts_str = e.get("ts", "")
                if not ts_str:
                    continue
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts >= cutoff:
                    events.append(e)
            except Exception:
                continue

    if not events:
        log.info("[OVERNIGHT_DIGEST] no events in last %dh — skipping", window_hours)
        return None

    # ── Step 2: filter to high-signal events ────────────────────────────────
    wl_syms = _watchlist_symbols()
    qualifying = [
        e for e in events
        if e.get("impact_score", 0) >= 6
        or bool(set(e.get("affected_symbols", []) or []) & wl_syms)
    ]
    if not qualifying:
        log.info(
            "[OVERNIGHT_DIGEST] %d events but none qualify (score<6, no watchlist hit)",
            len(events),
        )
        return None

    # ── Step 3: build Haiku prompt (top 15 events, sorted by score desc) ────
    top = sorted(
        qualifying, key=lambda x: x.get("impact_score", 0), reverse=True,
    )[:15]
    events_text = "\n".join(
        f"[{e.get('impact_score', 0):.1f}][{e.get('keyword_tier', '?')}] "
        f"{e.get('headline', '?')} "
        f"(affected: {', '.join(e.get('affected_symbols', []) or ['none'])})"
        for e in top
    )
    wl_list = ", ".join(sorted(wl_syms)[:30])

    system_prompt = (
        "You are a macro intelligence analyst. Given a list of overnight "
        "market-moving events, produce a structured JSON digest for a morning "
        "trading brief. Be concise and actionable. Return ONLY valid JSON, "
        "no markdown, no explanation."
    )
    user_prompt = (
        f"Overnight macro events (last {window_hours}h):\n\n"
        f"{events_text}\n\n"
        f"Tracked portfolio symbols: {wl_list}\n\n"
        "Produce a JSON digest with these fields:\n"
        "{\n"
        '  "regime_shift": true/false,\n'
        '  "regime_note": "one sentence if regime_shift, else null",\n'
        '  "top_events": [\n'
        '    {"headline": "...", "impact": "high/medium", '
        '"affected_symbols": [...], "direction": "bullish/bearish/neutral"}\n'
        "  ],\n"
        '  "watchlist_catalysts": {\n'
        '    "SYMBOL": "one-line catalyst description"\n'
        "  },\n"
        '  "macro_themes": ["theme1", "theme2"],\n'
        '  "risk_flags": ["flag1"],\n'
        '  "overnight_summary": "2-3 sentence summary for morning brief"\n'
        "}"
    )

    try:
        resp = _claude.messages.create(
            model=MODEL,
            max_tokens=800,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = resp.content[0].text.strip()
        # Strip markdown fences if Haiku wrapped the JSON
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            if raw.startswith("json"):
                raw = raw[4:].lstrip()
        digest = json.loads(raw)
    except Exception as exc:
        log.warning("[OVERNIGHT_DIGEST] Haiku call failed (non-fatal): %s", exc)
        return None

    # ── Step 4: stamp metadata + write to disk ──────────────────────────────
    digest["generated_at"]      = now_utc.isoformat()
    digest["window_hours"]      = window_hours
    digest["events_considered"] = len(events)
    digest["events_qualifying"] = len(qualifying)

    date_str = now_utc.strftime("%Y-%m-%d")
    out_path = MACRO_DIR / f"overnight_digest_{date_str}.json"
    try:
        out_path.write_text(json.dumps(digest, indent=2))
        log.info(
            "[OVERNIGHT_DIGEST] wrote %d qualifying events → %s",
            len(qualifying), out_path.name,
        )
    except Exception as exc:
        log.warning("[OVERNIGHT_DIGEST] write failed (non-fatal): %s", exc)
        return None

    # Cost tracking via existing macro_wire pattern
    try:
        from cost_tracker import get_tracker  # noqa: PLC0415
        get_tracker().record_api_call(MODEL, resp.usage, caller="overnight_digest")
    except Exception:
        pass

    return digest


def write_daily_digest() -> None:
    """Written once at 4 PM ET — top headlines + significant events of the day."""
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    today_str   = datetime.now(ET).strftime("%Y-%m-%d")
    digest_path = DIGEST_DIR / f"{today_str}.json"

    top_articles: list = []
    try:
        cache = json.loads(LIVE_CACHE.read_text())
        today_arts = [
            a for a in cache.get("articles", [])
            if a.get("published_at", "")[:10] == today_str
        ]
        today_arts.sort(key=lambda x: x.get("impact_score", 0), reverse=True)
        top_articles = today_arts[:5]
    except Exception:
        pass

    sig_events_today: list = []
    try:
        if SIG_EVENTS.exists():
            for line in SIG_EVENTS.read_text().splitlines():
                try:
                    rec = json.loads(line)
                    if rec.get("ts", "")[:10] == today_str:
                        sig_events_today.append(rec)
                except Exception:
                    pass
    except Exception:
        pass

    digest = {
        "date":               today_str,
        "written_at":         datetime.now(ET).isoformat(),
        "top_headlines":      top_articles,
        "significant_events": sig_events_today,
        "count_significant":  len(sig_events_today),
    }
    digest_path.write_text(json.dumps(digest, indent=2))
    log.info("Daily digest saved: %s (%d sig events)", digest_path.name, len(sig_events_today))

    # Archive
    try:
        archive_dir = _BASE_DIR / "data" / "archive" / today_str
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / "macro_wire_digest.json").write_text(json.dumps(digest, indent=2))
    except Exception:
        pass


def backfill_market_impact() -> None:
    """
    Called at 4:15 PM ET daily.
    Backfills spx_move_next_30min and trade_decisions_next_60min
    for today's significant events.
    """
    if not SIG_EVENTS.exists():
        return

    today_str = datetime.now(ET).strftime("%Y-%m-%d")
    lines = SIG_EVENTS.read_text().splitlines()
    if not lines:
        return

    # Load today's trade decisions
    trade_decisions: list = []
    trades_log = _BASE_DIR / "logs" / "trades.jsonl"
    if trades_log.exists():
        for tline in trades_log.read_text().splitlines()[-500:]:
            try:
                rec = json.loads(tline)
                if rec.get("ts", "")[:10] == today_str:
                    trade_decisions.append(rec)
            except Exception:
                pass

    # Fetch SPX intraday data
    spx_hist = None
    try:
        import yfinance as yf
        spx_hist = yf.Ticker("^GSPC").history(period="1d", interval="1m")
    except Exception:
        pass

    changed = False
    updated_lines: list = []

    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            if (rec.get("ts", "")[:10] != today_str or
                    rec.get("spx_move_next_30min") is not None):
                updated_lines.append(line)
                continue

            event_ts = _parse_ts(rec.get("ts", ""))

            # SPX move
            if spx_hist is not None and not spx_hist.empty:
                try:
                    prices_at  = []
                    prices_30m = []
                    for i, idx_ts in enumerate(spx_hist.index):
                        try:
                            idx_dt = idx_ts.to_pydatetime().replace(tzinfo=timezone.utc)
                        except Exception:
                            continue
                        diff_secs = (idx_dt - event_ts.replace(tzinfo=timezone.utc)).total_seconds()
                        if abs(diff_secs) <= 180:
                            prices_at.append(float(spx_hist["Close"].iloc[i]))
                        elif 1500 <= diff_secs <= 2100:
                            prices_30m.append(float(spx_hist["Close"].iloc[i]))
                    if prices_at and prices_30m:
                        p0 = prices_at[0]
                        p1 = prices_30m[-1]
                        rec["spx_move_next_30min"] = round((p1 - p0) / p0 * 100, 3)
                        changed = True
                except Exception:
                    pass

            # Trade decisions within 60 min
            nearby = [
                t for t in trade_decisions
                if abs((_parse_ts(t.get("ts", "")) - event_ts).total_seconds()) <= 3600
            ]
            if nearby:
                rec["trade_decisions_next_60min"] = [
                    {"event": t.get("event"), "symbol": t.get("symbol"),
                     "regime": t.get("regime"), "ts": t.get("ts")}
                    for t in nearby[:5]
                ]
                changed = True

            updated_lines.append(json.dumps(rec))
        except Exception:
            updated_lines.append(line)

    if changed:
        SIG_EVENTS.write_text("\n".join(l for l in updated_lines if l.strip()) + "\n")
        log.info("Macro wire: backfilled market impact for today's significant events")


# ── Prompt section builder ────────────────────────────────────────────────────

def build_macro_wire_section() -> str:
    """
    Reads live_cache.json. Selective injection:
    Always include: age < 30 minutes
    Include if: impact_score >= 7 AND age < 240 min
    Include if: keyword_tier == "critical" AND age < 1440 min
    Cap: 8 headlines max
    """
    try:
        if not LIVE_CACHE.exists():
            return "  No significant macro headlines in the past 4 hours."
        cache = json.loads(LIVE_CACHE.read_text())
        articles = cache.get("articles", [])
    except Exception:
        return "  No significant macro headlines in the past 4 hours."

    qualifying: list = []
    for a in articles:
        age   = a.get("age_minutes", 9999)
        score = a.get("impact_score", 0)
        tier  = a.get("keyword_tier", "none")
        if age < 30 or (score >= 7 and age < 240) or (tier == "critical" and age < 1440):
            qualifying.append(a)

    if not qualifying:
        return "  No significant macro headlines in the past 4 hours."

    qualifying.sort(key=lambda x: (x.get("age_minutes", 9999), -x.get("impact_score", 0)))
    qualifying = qualifying[:8]

    lines = []
    for a in qualifying:
        age      = a.get("age_minutes", "?")
        source   = a.get("source", "?")
        headline = a.get("headline", "")
        summary  = a.get("one_line_summary") or ""
        direction = a.get("direction") or "neutral"
        sectors   = ", ".join((a.get("affected_sectors") or [])[:3]) or "general"
        score     = a.get("impact_score", 0)
        lines.append(f"  [{age}m ago] [{source}] {headline}")
        if summary:
            lines.append(f"    → {summary} | {direction} | sectors: {sectors}  [score={score:.0f}]")

    return "\n".join(lines)


# ── Full refresh ──────────────────────────────────────────────────────────────

def refresh_macro_wire() -> None:
    """
    Full fetch + score + classify + save cycle.
    Called by scheduler._maybe_refresh_macro_wire().
    """
    try:
        articles = fetch_macro_wire()
        if not articles:
            return
        high_scoring = [a for a in articles if a.get("impact_score", 0) >= 5]
        if high_scoring:
            seen_ids = _load_seen_ids()
            articles_to_classify = [
                a for a in high_scoring
                if _article_id(a) not in seen_ids
            ]

            if not articles_to_classify:
                log.debug(
                    "Macro wire: 0 new articles to classify "
                    "(%d already seen)", len(high_scoring))
            else:
                classify_articles(articles_to_classify)
                for article in articles_to_classify:
                    seen_ids.add(_article_id(article))
                _save_seen_ids(seen_ids)
                log.info(
                    "Macro wire: classified %d new articles "
                    "(%d already seen)",
                    len(articles_to_classify),
                    len(high_scoring) - len(articles_to_classify))
        save_live_cache(articles)
        save_significant_events(articles)
    except Exception as exc:
        log.warning("refresh_macro_wire failed (non-fatal): %s", exc)
