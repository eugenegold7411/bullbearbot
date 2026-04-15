"""
reddit_sentiment_public.py — Public JSON Reddit provider (no OAuth/PRAW required).

Uses Reddit's public .json endpoints (e.g. reddit.com/r/wallstreetbets/hot.json).
No credentials needed. Rate-limited: 1.2s between requests.
Cached per-subreddit to data/social/reddit_cache/ (TTL 1 hour, same as PRAW path).

Used as fallback in reddit_sentiment.py when PRAW credentials are unavailable.
"""

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from log_setup import get_logger

log = get_logger(__name__)

_BASE_DIR       = Path(__file__).parent
_CACHE_DIR      = _BASE_DIR / "data" / "social" / "reddit_cache"
_CACHE_TTL_H    = 1.0     # match PRAW provider TTL
_USER_AGENT     = "trading_bot/1.0 (public endpoints; contact: trading_bot)"
_REQUEST_DELAY  = 1.2     # seconds between requests (well under public rate limit)
_SUBREDDITS     = ["wallstreetbets", "stocks", "investing", "options"]
_MAX_POSTS      = 50      # per subreddit, hot; Reddit .json max is 100


class RedditPublicProvider:
    """
    Fetches subreddit hot+new posts via public Reddit JSON API.

    No PRAW, no OAuth, no credentials. Returns the same post-dict list
    format that reddit_sentiment.py expects from the PRAW fetch loop:
      [{"title": str, "selftext": str, "score": int, "created": float, "sub": str}, ...]

    Per-subreddit caches are kept at data/social/reddit_cache/{subreddit}.json.
    """

    def __init__(self) -> None:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # ── Cache helpers ──────────────────────────────────────────────────────────

    def _cache_path(self, subreddit: str) -> Path:
        return _CACHE_DIR / f"{subreddit}.json"

    def _load_cache(self, subreddit: str) -> dict:
        path = self._cache_path(subreddit)
        try:
            if path.exists():
                return json.loads(path.read_text())
        except Exception:
            pass
        return {}

    def _save_cache(self, subreddit: str, posts: list) -> None:
        try:
            data = {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "posts":      posts,
            }
            self._cache_path(subreddit).write_text(json.dumps(data))
        except Exception as exc:
            log.debug("[REDDIT_PUB] Cache save failed for r/%s: %s", subreddit, exc)

    def _is_fresh(self, cached: dict) -> bool:
        ts = cached.get("fetched_at")
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

    # ── HTTP fetch ─────────────────────────────────────────────────────────────

    def _fetch_subreddit(
        self,
        subreddit: str,
        sort: str = "hot",
        limit: int = 25,
    ) -> list[dict]:
        """
        Fetch posts from reddit.com/r/{subreddit}/{sort}.json.
        Returns list of post dicts compatible with the PRAW format.
        Returns [] on any failure — non-fatal.
        """
        url = f"https://www.reddit.com/r/{subreddit}/{sort}.json?limit={limit}"
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw   = json.loads(resp.read().decode("utf-8"))
                posts = []
                for child in raw.get("data", {}).get("children", []):
                    d = child.get("data", {})
                    posts.append({
                        "title":    d.get("title", ""),
                        "selftext": (d.get("selftext") or "")[:300],
                        "score":    d.get("score", 0),
                        "created":  d.get("created_utc", 0.0),
                        "sub":      subreddit,
                    })
                return posts
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                log.warning("[REDDIT_PUB] Rate limited on r/%s — will use cache", subreddit)
            else:
                log.debug("[REDDIT_PUB] HTTP %d on r/%s", exc.code, subreddit)
            return []
        except Exception as exc:
            log.debug("[REDDIT_PUB] Fetch failed r/%s: %s", subreddit, exc)
            return []

    # ── Public API ─────────────────────────────────────────────────────────────

    def fetch_all_posts(self) -> list[dict]:
        """
        Fetch hot+new posts from all configured subreddits.

        Uses per-subreddit disk cache (TTL 1 hour). Falls back to stale cache
        per subreddit if a live fetch fails. Returns flat list of post dicts.
        Returns [] only if all subreddits fail AND no cache exists.
        """
        all_posts: list[dict] = []

        for subreddit in _SUBREDDITS:
            cached = self._load_cache(subreddit)

            if self._is_fresh(cached):
                log.debug("[REDDIT_PUB] r/%s: cache hit (< 1h)", subreddit)
                all_posts.extend(cached.get("posts", []))
                continue

            # Fetch hot
            hot_posts = self._fetch_subreddit(subreddit, sort="hot", limit=_MAX_POSTS)
            time.sleep(_REQUEST_DELAY)

            # Fetch new (half count)
            new_posts = self._fetch_subreddit(subreddit, sort="new", limit=_MAX_POSTS // 2)
            time.sleep(_REQUEST_DELAY)

            if hot_posts or new_posts:
                posts = hot_posts + new_posts
                self._save_cache(subreddit, posts)
                all_posts.extend(posts)
                log.debug("[REDDIT_PUB] r/%s: fetched %d posts", subreddit, len(posts))
            else:
                # Use stale cache rather than contributing nothing
                stale = cached.get("posts", [])
                all_posts.extend(stale)
                if stale:
                    log.debug(
                        "[REDDIT_PUB] r/%s: live fetch failed — using stale cache (%d posts)",
                        subreddit, len(stale),
                    )

        log.debug("[REDDIT_PUB] fetch_all_posts: %d total posts", len(all_posts))
        return all_posts
