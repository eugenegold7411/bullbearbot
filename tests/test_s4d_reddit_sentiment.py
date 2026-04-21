"""
Tests for S4-D Reddit Sentiment Fix.

Build 1: 403 responses skip subreddit without using stale cache
Build 2: all-403 scenario sets sentiment_unavailable=True
"""

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_403():
    exc = urllib.error.HTTPError(url="u", code=403, msg="Forbidden", hdrs=None, fp=None)
    return exc


def _make_200_response(posts_data):
    payload = json.dumps({
        "data": {
            "children": [
                {"data": {"title": p["title"], "selftext": "", "score": 10, "created_utc": 0.0}}
                for p in posts_data
            ]
        }
    }).encode()
    resp = MagicMock()
    resp.read.return_value = payload
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _make_provider(cache_dir):
    """Import and instantiate RedditPublicProvider with patched cache dir."""
    import sys
    # Ensure fresh import in case module was already loaded
    if "reddit_sentiment_public" in sys.modules:
        import reddit_sentiment_public as mod
    else:
        import reddit_sentiment_public as mod

    import importlib
    mod = importlib.import_module("reddit_sentiment_public")

    provider = mod.RedditPublicProvider.__new__(mod.RedditPublicProvider)
    provider.sentiment_unavailable = False

    # Redirect cache dir
    import reddit_sentiment_public as rsp
    original = rsp._CACHE_DIR
    rsp._CACHE_DIR = cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    return provider, rsp, original


# ---------------------------------------------------------------------------
# Suite RD1 — _fetch_subreddit returns None on 403
# ---------------------------------------------------------------------------

class TestFetchSubreddit403:
    def test_returns_none_on_403(self, tmp_path):
        from reddit_sentiment_public import RedditPublicProvider
        provider = RedditPublicProvider.__new__(RedditPublicProvider)
        provider.sentiment_unavailable = False

        with patch("urllib.request.urlopen", side_effect=_make_403()):
            result = provider._fetch_subreddit("options", sort="hot", limit=25)

        assert result is None, "403 should return None, not empty list"

    def test_returns_empty_list_on_other_http_error(self, tmp_path):
        from reddit_sentiment_public import RedditPublicProvider
        provider = RedditPublicProvider.__new__(RedditPublicProvider)
        provider.sentiment_unavailable = False

        exc = urllib.error.HTTPError(url="u", code=503, msg="Service Unavailable", hdrs=None, fp=None)
        with patch("urllib.request.urlopen", side_effect=exc):
            result = provider._fetch_subreddit("wallstreetbets", sort="hot", limit=25)

        assert result == []

    def test_returns_empty_list_on_network_error(self):
        from reddit_sentiment_public import RedditPublicProvider
        provider = RedditPublicProvider.__new__(RedditPublicProvider)
        provider.sentiment_unavailable = False

        with patch("urllib.request.urlopen", side_effect=ConnectionError("timeout")):
            result = provider._fetch_subreddit("stocks", sort="hot", limit=25)

        assert result == []

    def test_returns_posts_on_success(self):
        from reddit_sentiment_public import RedditPublicProvider
        provider = RedditPublicProvider.__new__(RedditPublicProvider)

        resp = _make_200_response([{"title": "NVDA moon"}])
        with patch("urllib.request.urlopen", return_value=resp):
            result = provider._fetch_subreddit("wallstreetbets", sort="hot", limit=25)

        assert len(result) == 1
        assert result[0]["title"] == "NVDA moon"


# ---------------------------------------------------------------------------
# Suite RD2 — fetch_all_posts skips 403 subreddits
# ---------------------------------------------------------------------------

class TestFetchAllPostsSkip403:
    def test_skips_403_subreddit_no_stale_cache(self, tmp_path):
        """A 403 subreddit is skipped entirely — stale cache not used."""
        import reddit_sentiment_public as rsp
        from reddit_sentiment_public import RedditPublicProvider

        # Patch cache dir and _SUBREDDITS
        old_cache_dir = rsp._CACHE_DIR
        old_subs = rsp._SUBREDDITS
        rsp._CACHE_DIR = tmp_path
        rsp._SUBREDDITS = ["options"]

        try:
            # Write stale cache for options
            stale = {"fetched_at": "2020-01-01T00:00:00+00:00", "posts": [{"title": "old", "sub": "options"}]}
            (tmp_path / "options.json").write_text(json.dumps(stale))

            provider = RedditPublicProvider()

            # Both hot and new 403
            with patch("urllib.request.urlopen", side_effect=_make_403()):
                with patch("time.sleep"):
                    posts = provider.fetch_all_posts()

            assert posts == [], "403 subreddit should not contribute stale cache"
        finally:
            rsp._CACHE_DIR = old_cache_dir
            rsp._SUBREDDITS = old_subs

    def test_403_subreddit_skipped_next_subreddit_tried(self, tmp_path):
        """When r/options 403s, r/wallstreetbets is still tried."""
        import reddit_sentiment_public as rsp
        from reddit_sentiment_public import RedditPublicProvider

        old_cache_dir = rsp._CACHE_DIR
        old_subs = rsp._SUBREDDITS
        rsp._CACHE_DIR = tmp_path
        rsp._SUBREDDITS = ["options", "wallstreetbets"]

        call_count = [0]

        def side_effect(req, timeout=10):
            call_count[0] += 1
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "options" in url:
                raise _make_403()
            return _make_200_response([{"title": "WSB post"}])

        try:
            provider = RedditPublicProvider()
            with patch("urllib.request.urlopen", side_effect=side_effect):
                with patch("time.sleep"):
                    posts = provider.fetch_all_posts()

            # wallstreetbets should have been fetched
            assert any(p["sub"] == "wallstreetbets" for p in posts), \
                "wallstreetbets should succeed after options 403s"
            assert not any(p.get("sub") == "options" for p in posts)
        finally:
            rsp._CACHE_DIR = old_cache_dir
            rsp._SUBREDDITS = old_subs

    def test_subreddits_tried_in_order(self, tmp_path):
        """Subreddits are attempted in _SUBREDDITS list order."""
        import reddit_sentiment_public as rsp
        from reddit_sentiment_public import RedditPublicProvider

        old_cache_dir = rsp._CACHE_DIR
        old_subs = rsp._SUBREDDITS
        rsp._CACHE_DIR = tmp_path
        rsp._SUBREDDITS = ["wallstreetbets", "stocks", "investing", "options"]

        attempted_subs = []

        def side_effect(req, timeout=10):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            for sub in ["wallstreetbets", "stocks", "investing", "options"]:
                if f"r/{sub}/" in url:
                    if sub not in attempted_subs:
                        attempted_subs.append(sub)
                    break
            raise _make_403()

        try:
            provider = RedditPublicProvider()
            with patch("urllib.request.urlopen", side_effect=side_effect):
                with patch("time.sleep"):
                    provider.fetch_all_posts()

            assert attempted_subs == ["wallstreetbets", "stocks", "investing", "options"], \
                f"Expected order wallstreetbets/stocks/investing/options, got {attempted_subs}"
        finally:
            rsp._CACHE_DIR = old_cache_dir
            rsp._SUBREDDITS = old_subs


# ---------------------------------------------------------------------------
# Suite RD3 — sentiment_unavailable flag
# ---------------------------------------------------------------------------

class TestSentimentUnavailable:
    def test_all_403_sets_sentiment_unavailable(self, tmp_path):
        """All subreddits returning 403 sets sentiment_unavailable=True."""
        import reddit_sentiment_public as rsp
        from reddit_sentiment_public import RedditPublicProvider

        old_cache_dir = rsp._CACHE_DIR
        old_subs = rsp._SUBREDDITS
        rsp._CACHE_DIR = tmp_path
        rsp._SUBREDDITS = ["wallstreetbets", "stocks", "investing", "options"]

        try:
            provider = RedditPublicProvider()
            with patch("urllib.request.urlopen", side_effect=_make_403()):
                with patch("time.sleep"):
                    posts = provider.fetch_all_posts()

            assert posts == []
            assert provider.sentiment_unavailable is True
        finally:
            rsp._CACHE_DIR = old_cache_dir
            rsp._SUBREDDITS = old_subs

    def test_partial_403_does_not_set_unavailable(self, tmp_path):
        """If at least one subreddit returns posts, sentiment_unavailable stays False."""
        import reddit_sentiment_public as rsp
        from reddit_sentiment_public import RedditPublicProvider

        old_cache_dir = rsp._CACHE_DIR
        old_subs = rsp._SUBREDDITS
        rsp._CACHE_DIR = tmp_path
        rsp._SUBREDDITS = ["options", "wallstreetbets"]

        def side_effect(req, timeout=10):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "options" in url:
                raise _make_403()
            return _make_200_response([{"title": "NVDA to moon"}])

        try:
            provider = RedditPublicProvider()
            with patch("urllib.request.urlopen", side_effect=side_effect):
                with patch("time.sleep"):
                    posts = provider.fetch_all_posts()

            assert len(posts) > 0
            assert provider.sentiment_unavailable is False
        finally:
            rsp._CACHE_DIR = old_cache_dir
            rsp._SUBREDDITS = old_subs

    def test_all_empty_non_403_sets_unavailable(self, tmp_path):
        """All subreddits returning [] (non-403) and no cache also sets unavailable."""
        import reddit_sentiment_public as rsp
        from reddit_sentiment_public import RedditPublicProvider

        old_cache_dir = rsp._CACHE_DIR
        old_subs = rsp._SUBREDDITS
        rsp._CACHE_DIR = tmp_path
        rsp._SUBREDDITS = ["wallstreetbets"]

        try:
            provider = RedditPublicProvider()
            exc = urllib.error.HTTPError(url="u", code=503, msg="err", hdrs=None, fp=None)
            with patch("urllib.request.urlopen", side_effect=exc):
                with patch("time.sleep"):
                    posts = provider.fetch_all_posts()

            assert posts == []
            assert provider.sentiment_unavailable is True
        finally:
            rsp._CACHE_DIR = old_cache_dir
            rsp._SUBREDDITS = old_subs

    def test_sentiment_unavailable_resets_on_new_call(self, tmp_path):
        """A second call to fetch_all_posts resets sentiment_unavailable."""
        import reddit_sentiment_public as rsp
        from reddit_sentiment_public import RedditPublicProvider

        old_cache_dir = rsp._CACHE_DIR
        old_subs = rsp._SUBREDDITS
        rsp._CACHE_DIR = tmp_path
        rsp._SUBREDDITS = ["wallstreetbets"]

        try:
            provider = RedditPublicProvider()

            # First call: all 403
            with patch("urllib.request.urlopen", side_effect=_make_403()):
                with patch("time.sleep"):
                    provider.fetch_all_posts()
            assert provider.sentiment_unavailable is True

            # Second call: success
            with patch("urllib.request.urlopen", return_value=_make_200_response([{"title": "x"}])):
                with patch("time.sleep"):
                    posts = provider.fetch_all_posts()
            assert provider.sentiment_unavailable is False
            assert len(posts) > 0
        finally:
            rsp._CACHE_DIR = old_cache_dir
            rsp._SUBREDDITS = old_subs

    def test_initial_sentiment_unavailable_is_false(self, tmp_path):
        """Provider starts with sentiment_unavailable=False before any fetch."""
        import reddit_sentiment_public as rsp
        from reddit_sentiment_public import RedditPublicProvider

        old_cache_dir = rsp._CACHE_DIR
        rsp._CACHE_DIR = tmp_path

        try:
            provider = RedditPublicProvider()
            assert provider.sentiment_unavailable is False
        finally:
            rsp._CACHE_DIR = old_cache_dir


# ---------------------------------------------------------------------------
# Suite RD4 — acceptance criteria checks
# ---------------------------------------------------------------------------

class TestAcceptanceCriteria:
    def test_at_least_3_subreddits_in_list(self):
        """_SUBREDDITS must have at least 3 entries (fallback coverage)."""
        import reddit_sentiment_public as rsp
        assert len(rsp._SUBREDDITS) >= 3, \
            f"Need ≥3 subreddits, got {len(rsp._SUBREDDITS)}: {rsp._SUBREDDITS}"

    def test_wallstreetbets_stocks_investing_in_list(self):
        """Key fallback subreddits must be present in _SUBREDDITS."""
        import reddit_sentiment_public as rsp
        for sub in ("wallstreetbets", "stocks", "investing"):
            assert sub in rsp._SUBREDDITS, f"r/{sub} missing from _SUBREDDITS"

    def test_no_unhandled_exception_on_403(self, tmp_path):
        """403 responses must never raise an unhandled exception."""
        import reddit_sentiment_public as rsp
        from reddit_sentiment_public import RedditPublicProvider

        old_cache_dir = rsp._CACHE_DIR
        old_subs = rsp._SUBREDDITS
        rsp._CACHE_DIR = tmp_path
        rsp._SUBREDDITS = ["wallstreetbets", "stocks", "investing", "options"]

        try:
            provider = RedditPublicProvider()
            try:
                with patch("urllib.request.urlopen", side_effect=_make_403()):
                    with patch("time.sleep"):
                        posts = provider.fetch_all_posts()
            except Exception as exc:
                pytest.fail(f"Unhandled exception on 403: {exc}")

            assert isinstance(posts, list)
        finally:
            rsp._CACHE_DIR = old_cache_dir
            rsp._SUBREDDITS = old_subs
