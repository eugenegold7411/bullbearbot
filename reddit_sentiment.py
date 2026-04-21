"""
reddit_sentiment.py — Reddit/WSB mention frequency and sentiment analysis.

Requires PRAW (pip install praw) + Reddit OAuth credentials in .env:
  REDDIT_CLIENT_ID=...
  REDDIT_CLIENT_SECRET=...
  REDDIT_USER_AGENT=trading_bot_sentiment/1.0

Gracefully returns {} if credentials are missing or PRAW is unavailable.
Fetches at most once per hour during market hours. Cached to disk.

Rate limits:
  - OAuth apps: ~60 req/min (well within our 1/hour fetch cadence)
  - Aggressive caching: never hit Reddit during live trading cycles

──────────────────────────────────────────────────────────────────────────────
REDDIT API CREDENTIALS — HOW TO ACTIVATE (FREE, ~5 MINUTES)
──────────────────────────────────────────────────────────────────────────────
Without credentials, the public JSON fallback (reddit_sentiment_public.py) is
used. It has no rate-limit headers and may get 403s on restricted subreddits
like r/options. Authenticated apps get ~60 req/min and access to all subs.

1. Log in at reddit.com, go to: https://www.reddit.com/prefs/apps
2. Click "create another app" (or "create app")
3. Choose "script" type, fill in any name/description/redirect (use http://localhost)
4. After creation, note the client_id (under the app name) and client_secret
5. Add to .env on the VPS:
     REDDIT_CLIENT_ID=<your_client_id>
     REDDIT_CLIENT_SECRET=<your_client_secret>
     REDDIT_USERNAME=<your_reddit_username>   (optional — read-only app doesn't need login)
     REDDIT_PASSWORD=<your_reddit_password>   (optional — only needed for user-context calls)
6. Run: pip install praw  (already in requirements.txt if listed)
7. Restart the trading-bot service

This module auto-detects the credentials and switches to authenticated PRAW.
The public fallback is still used if credentials are missing or PRAW import fails.
──────────────────────────────────────────────────────────────────────────────
"""

import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from log_setup import get_logger

load_dotenv()
log = get_logger(__name__)

_BASE_DIR      = Path(__file__).parent
_CACHE_DIR     = _BASE_DIR / "data" / "sentiment"
_CACHE_FILE    = _CACHE_DIR / "reddit_cache.json"
_CACHE_TTL_H   = 1.0    # refresh at most once per hour
_SUBREDDITS    = ["wallstreetbets", "stocks", "investing", "options"]
_MAX_POSTS     = 50     # posts per subreddit per fetch

_claude      = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_MODEL       = "claude-sonnet-4-6"
_MODEL_FAST  = "claude-haiku-4-5-20251001"


# ── Cache ──────────────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    try:
        if _CACHE_FILE.exists():
            return json.loads(_CACHE_FILE.read_text())
    except Exception:
        pass
    return {}


def _save_cache(data: dict) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(data, indent=2, default=str))
    except Exception as exc:
        log.warning("[REDDIT] Cache save failed: %s", exc)


def _cache_is_fresh(cache: dict) -> bool:
    ts = cache.get("fetched_at")
    if not ts:
        return False
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        return age_h < _CACHE_TTL_H
    except Exception:
        return False


# ── Reddit client ──────────────────────────────────────────────────────────────

def _setup_reddit():
    """Return praw.Reddit client or None if not configured."""
    try:
        import praw  # noqa: PLC0415
        client_id     = os.getenv("REDDIT_CLIENT_ID")
        client_secret = os.getenv("REDDIT_CLIENT_SECRET")
        user_agent    = os.getenv("REDDIT_USER_AGENT", "trading_bot_sentiment/1.0")

        if not client_id or not client_secret:
            log.debug("[REDDIT] Credentials not configured — sentiment disabled")
            return None

        reddit = praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            user_agent=user_agent,
        )
        # Verify connection (read-only mode, no login needed)
        _ = reddit.subreddit("wallstreetbets").hot(limit=1)
        return reddit
    except ImportError:
        log.debug("[REDDIT] praw not installed — Reddit sentiment disabled")
        return None
    except Exception as exc:
        log.warning("[REDDIT] Reddit client setup failed: %s", exc)
        return None


# ── Mention counting ───────────────────────────────────────────────────────────

def _count_mentions(posts: list[dict], symbols: list[str]) -> dict[str, list[str]]:
    """
    Count ticker mentions in post titles + selftext.
    Returns {symbol: [list of matching post snippets]}.
    Both $TICKER and plain TICKER formats count.
    """
    sym_upper = {s.upper() for s in symbols if "/" not in s}
    mentions: dict[str, list[str]] = defaultdict(list)

    for post in posts:
        text = (post.get("title", "") + " " + post.get("selftext", ""))[:500]
        words = re.findall(r"\$?([A-Z]{2,5})\b", text.upper())
        for word in words:
            if word in sym_upper:
                snippet = post.get("title", "")[:120]
                if snippet not in mentions[word]:
                    mentions[word].append(snippet)

    return dict(mentions)


# ── Sentiment scoring ──────────────────────────────────────────────────────────

def _score_sentiment(mentions_with_text: dict[str, list[str]]) -> dict[str, float]:
    """
    Single Claude call to score Reddit sentiment for all mentioned symbols.
    Returns {symbol: score} where score is -1.0 to +1.0.
    Returns {} on failure.
    """
    if not mentions_with_text:
        return {}

    lines = []
    for sym, snippets in mentions_with_text.items():
        lines.append(f"{sym}: {' | '.join(snippets[:3])}")

    prompt = (
        "Score the Reddit sentiment for each stock ticker below based on the post titles. "
        "Return ONLY a JSON object mapping ticker to sentiment score from -1.0 (very bearish) "
        "to +1.0 (very bullish). Example: {\"NVDA\": 0.8, \"TSLA\": -0.3}\n\n"
        + "\n".join(lines)
    )

    try:
        resp = _claude.messages.create(
            model=_MODEL_FAST,
            max_tokens=300,
            system=[{
                "type": "text",
                "text": "You are a sentiment analysis model. Return only valid JSON.",
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": prompt}],
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
        try:
            from cost_tracker import get_tracker
            get_tracker().record_api_call(_MODEL_FAST, resp.usage, caller="reddit_sentiment")
        except Exception:
            pass
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return json.loads(raw)
    except Exception as exc:
        log.warning("[REDDIT] Sentiment scoring failed: %s", exc)
        return {}


# ── 7-day average baseline (for trending detection) ───────────────────────────

def _get_7day_averages(cache: dict) -> dict[str, float]:
    """Compute 7-day average mention count per symbol from cached history."""
    history = cache.get("history", [])
    if not history:
        return {}
    counts: dict[str, list] = defaultdict(list)
    for snapshot in history[-7:]:
        for sym, data in snapshot.get("symbols", {}).items():
            counts[sym].append(data.get("mentions_24h", 0))
    return {sym: sum(v) / len(v) for sym, v in counts.items() if v}


# ── Public API ─────────────────────────────────────────────────────────────────

def fetch_reddit_sentiment(symbols: list[str]) -> dict[str, dict]:
    """
    Fetch mention frequency + sentiment for watchlist symbols.
    Rate-limit safe: returns cached data if < 1 hour old.
    Returns {} on any failure — never raises.

    Result format: {symbol: {mentions_24h, sentiment_score, top_post, trending}}
    """
    cache = _load_cache()

    # Return cached if still fresh
    if _cache_is_fresh(cache):
        log.debug("[REDDIT] Using cached sentiment (< 1h old)")
        return cache.get("symbols", {})

    # Filter to stock symbols only
    stock_syms = [s for s in symbols if "/" not in s]

    reddit = _setup_reddit()
    if reddit is None:
        # Fall back to public JSON provider (no PRAW/OAuth needed)
        try:
            from reddit_sentiment_public import (
                RedditPublicProvider as _Pub,  # noqa: PLC0415
            )
            _pub     = _Pub()
            _pub_posts = _pub.fetch_all_posts()
            _sentiment_unavailable = _pub.sentiment_unavailable
        except Exception as _pub_exc:
            log.debug("[REDDIT] Public provider failed: %s", _pub_exc)
            _pub_posts = []
            _sentiment_unavailable = True
        if not _pub_posts:
            if _sentiment_unavailable:
                log.info(
                    "[REDDIT] All subreddits unavailable (403/empty) — treating as neutral, "
                    "returning cached symbols"
                )
            return cache.get("symbols", {})
        log.info("[REDDIT] PRAW unavailable — public JSON provider: %d posts", len(_pub_posts))
        all_posts: list[dict] = _pub_posts
    else:
        log.info("[REDDIT] Fetching Reddit sentiment for %d symbols", len(stock_syms))
        all_posts = []
        for sub_name in _SUBREDDITS:
            try:
                sub = reddit.subreddit(sub_name)
                for post in sub.hot(limit=_MAX_POSTS):
                    all_posts.append({
                        "title":    post.title,
                        "selftext": (post.selftext or "")[:300],
                        "score":    post.score,
                        "created":  post.created_utc,
                        "sub":      sub_name,
                    })
                for post in sub.new(limit=_MAX_POSTS // 2):
                    all_posts.append({
                        "title":    post.title,
                        "selftext": (post.selftext or "")[:300],
                        "score":    post.score,
                        "created":  post.created_utc,
                        "sub":      sub_name,
                    })
            except Exception as exc:
                log.warning("[REDDIT] Subreddit %s failed: %s", sub_name, exc)

    if not all_posts:
        log.warning("[REDDIT] No posts fetched — returning cached data")
        return cache.get("symbols", {})

    mentions_with_text = _count_mentions(all_posts, stock_syms)
    sentiment_scores   = _score_sentiment(mentions_with_text)
    day_avgs           = _get_7day_averages(cache)

    result: dict[str, dict] = {}
    for sym in stock_syms:
        sym_up   = sym.upper()
        snippets = mentions_with_text.get(sym_up, [])
        count    = len(snippets)
        if count == 0 and sym_up not in sentiment_scores:
            continue

        avg     = day_avgs.get(sym_up, 1.0)
        trending = count > 0 and avg > 0 and (count / avg) >= 10.0

        result[sym_up] = {
            "mentions_24h":    count,
            "sentiment_score": round(sentiment_scores.get(sym_up, 0.0), 2),
            "top_post_title":  snippets[0] if snippets else "",
            "trending":        trending,
            "trend_ratio":     round(count / avg, 1) if avg > 0 else 0.0,
        }

    # Save with rolling history
    history = cache.get("history", [])
    history.append({"ts": datetime.now(timezone.utc).isoformat(), "symbols": result})
    history = history[-8:]  # keep last 8 snapshots (1/hr × 8h)

    new_cache = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "symbols":    result,
        "history":    history,
    }
    _save_cache(new_cache)
    log.info("[REDDIT] Sentiment saved: %d symbols with mentions", len(result))
    return result


def format_reddit_sentiment_section(symbols: list[str]) -> str:
    """
    Build the SOCIAL SENTIMENT prompt section from cached data.
    Returns placeholder if unavailable.
    """
    try:
        data = fetch_reddit_sentiment(symbols)
    except Exception:
        data = {}

    if not data:
        return "  (Reddit sentiment unavailable — configure REDDIT_CLIENT_ID/.env)"

    lines = []
    # Sort by mentions (trending first)
    sorted_syms = sorted(data.items(), key=lambda x: (-x[1].get("mentions_24h", 0),
                                                       -abs(x[1].get("sentiment_score", 0))))
    for sym, d in sorted_syms[:10]:
        mentions = d.get("mentions_24h", 0)
        score    = d.get("sentiment_score", 0.0)
        trending = d.get("trending", False)
        ratio    = d.get("trend_ratio", 0.0)
        trend_tag = f"  TRENDING ({ratio:.0f}x avg)" if trending else ""
        lines.append(
            f"  {sym:<6}  {mentions:>3} mentions  "
            f"sentiment {score:+.2f}{trend_tag}"
        )

    return "\n".join(lines) if lines else "  (no Reddit mentions for watchlist symbols this hour)"
