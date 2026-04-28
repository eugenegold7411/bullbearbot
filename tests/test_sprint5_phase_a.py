"""
tests/test_sprint5_phase_a.py — Sprint 5 Phase A: RSS timestamp fallback,
feed expansion, earnings_intel content guard, lxml requirement.

Tests:
  PA-01  Stale timestamp (>48h) → article gets timestamp_source="fallback_now"
  PA-02  Very stale timestamp (>300d) → also gets timestamp_source="fallback_now"
  PA-03  Fresh timestamp (<48h) → article gets timestamp_source="feed"
  PA-04  Bloomberg feed present in RSS_FEEDS
  PA-05  CNBC-Top feed present in RSS_FEEDS
  PA-06  Yahoo general feed NOT present in RSS_FEEDS
  PA-07  earnings_intel short transcript (<5000 chars) returns placeholder (no Claude)
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_pub_struct(dt: datetime):
    """Convert a datetime to the time.struct_time tuple feedparser returns."""
    return time.gmtime(dt.timestamp())


def _run_fetch_macro_wire_with_single_entry(monkeypatch, pub_dt: datetime):
    """
    Patch feedparser to return one entry with the given publish time and
    return the list of articles that fetch_macro_wire() produces.
    """
    import feedparser as fp

    import macro_wire as mw

    fake_entry = {
        "title": "Fed signals emergency rate cut amid banking contagion",
        "summary": "Federal Reserve moves to stabilize financial system after regional bank failures",
        "link": "https://example.com/article-1",
        "published_parsed": _make_pub_struct(pub_dt),
    }
    fake_feed = MagicMock()
    fake_feed.entries = [fake_entry]

    # Bypass throttle and seen_ids so the article always processes
    monkeypatch.setattr(mw, "_last_fetch_ts", 0.0)
    monkeypatch.setattr(mw, "_load_seen_ids", lambda: set())
    monkeypatch.setattr(mw, "_save_seen_ids", lambda s: None)
    monkeypatch.setattr(fp, "parse", lambda url, **kw: fake_feed)

    articles = mw.fetch_macro_wire()
    return articles


# ─────────────────────────────────────────────────────────────────────────────
# PA-01  Stale timestamp → fallback_now
# ─────────────────────────────────────────────────────────────────────────────

class TestStaleTimestampFallback:
    def test_pa01_stale_77h_gets_fallback_now(self, monkeypatch):
        """CNBC-Econ-style 77h-old timestamp → timestamp_source='fallback_now'."""
        now = datetime.now(timezone.utc)
        stale_dt = now - timedelta(hours=77)
        articles = _run_fetch_macro_wire_with_single_entry(monkeypatch, stale_dt)
        assert len(articles) == 1, f"Expected 1 article, got {len(articles)}"
        assert articles[0]["timestamp_source"] == "fallback_now", (
            f"Expected 'fallback_now', got {articles[0].get('timestamp_source')!r}"
        )

    def test_pa02_stale_320d_gets_fallback_now(self, monkeypatch):
        """MarketWatch-style 320d-old timestamp → timestamp_source='fallback_now'."""
        now = datetime.now(timezone.utc)
        stale_dt = now - timedelta(days=320)
        articles = _run_fetch_macro_wire_with_single_entry(monkeypatch, stale_dt)
        assert len(articles) == 1, f"Expected 1 article, got {len(articles)}"
        assert articles[0]["timestamp_source"] == "fallback_now", (
            f"Expected 'fallback_now', got {articles[0].get('timestamp_source')!r}"
        )

    def test_pa03_fresh_2h_gets_feed_source(self, monkeypatch):
        """Bloomberg-style 2h-old timestamp → timestamp_source='feed'."""
        now = datetime.now(timezone.utc)
        fresh_dt = now - timedelta(hours=2)
        articles = _run_fetch_macro_wire_with_single_entry(monkeypatch, fresh_dt)
        assert len(articles) == 1, f"Expected 1 article, got {len(articles)}"
        assert articles[0]["timestamp_source"] == "feed", (
            f"Expected 'feed', got {articles[0].get('timestamp_source')!r}"
        )

    def test_fallback_now_article_passes_recency_cutoff(self, monkeypatch):
        """A 455d-old timestamp (WSJ-style) must not be discarded — fallback rescues it."""
        now = datetime.now(timezone.utc)
        stale_dt = now - timedelta(days=455)
        articles = _run_fetch_macro_wire_with_single_entry(monkeypatch, stale_dt)
        assert len(articles) == 1, (
            "WSJ-style 455d-old article must pass recency cutoff after fallback"
        )

    def test_fallback_now_age_minutes_is_small(self, monkeypatch):
        """fallback_now article should have age_minutes near 0 (treated as just-fetched)."""
        now = datetime.now(timezone.utc)
        stale_dt = now - timedelta(days=455)
        articles = _run_fetch_macro_wire_with_single_entry(monkeypatch, stale_dt)
        assert len(articles) == 1
        age = articles[0]["age_minutes"]
        assert age < 5, f"fallback_now article should be near 0 age_minutes, got {age}"


# ─────────────────────────────────────────────────────────────────────────────
# PA-04 / PA-05 / PA-06  Feed list checks
# ─────────────────────────────────────────────────────────────────────────────

class TestFeedExpansion:
    def test_pa04_bloomberg_in_feeds(self):
        """Bloomberg RSS must be present in RSS_FEEDS."""
        from macro_wire import RSS_FEEDS
        urls = [u for _, u in RSS_FEEDS]
        assert any("bloomberg.com" in u for u in urls), \
            "Bloomberg feed missing from RSS_FEEDS"

    def test_pa05_cnbc_top_in_feeds(self):
        """CNBC-Top RSS must be present in RSS_FEEDS."""
        from macro_wire import RSS_FEEDS
        sources = {name for name, _ in RSS_FEEDS}
        assert "CNBC-Top" in sources, "CNBC-Top missing from RSS_FEEDS"

    def test_pa06_yahoo_general_not_in_feeds(self):
        """Yahoo general news feed must be removed from RSS_FEEDS."""
        from macro_wire import RSS_FEEDS
        urls = [u for _, u in RSS_FEEDS]
        assert not any("finance.yahoo.com/news/rssindex" in u for u in urls), \
            "Yahoo general news RSS must be removed from RSS_FEEDS"

    def test_stale_feed_threshold_constant_exists(self):
        """_STALE_FEED_HOURS constant must exist and be a positive number."""
        from macro_wire import _STALE_FEED_HOURS
        assert isinstance(_STALE_FEED_HOURS, (int, float))
        assert _STALE_FEED_HOURS > 0


# ─────────────────────────────────────────────────────────────────────────────
# PA-07  earnings_intel short transcript guard
# ─────────────────────────────────────────────────────────────────────────────

class TestEarningsIntelContentGuard:
    def test_pa07_short_transcript_returns_placeholder_no_claude(self, monkeypatch):
        """
        A transcript < 5000 chars must return the short-filing placeholder
        WITHOUT calling Claude (analyze_earnings_transcript must not fire).
        """
        import earnings_intel as ei

        short_transcript = "NVDA reports Q4 2025 results. EPS $0.87 vs $0.84 est." * 10  # ~540 chars

        monkeypatch.setattr(ei, "fetch_earnings_transcript", lambda sym: short_transcript)

        # If analyze_earnings_transcript were called we'd get an error — patch to detect
        called = {"count": 0}
        original_analyze = ei.analyze_earnings_transcript
        def spy_analyze(sym, trans):
            called["count"] += 1
            return original_analyze(sym, trans)

        # Patch with a sentinel that would fail loudly if called
        monkeypatch.setattr(ei, "analyze_earnings_transcript",
                            lambda sym, trans: (_ for _ in ()).throw(
                                AssertionError("analyze_earnings_transcript must NOT be called for short transcripts")))

        result = ei.get_earnings_intel_section("NVDA", 2)
        assert "Short press release" in result or "short" in result.lower(), (
            f"Expected short-filing placeholder, got: {result!r}"
        )

    def test_long_transcript_proceeds_to_analyze(self, monkeypatch):
        """A transcript >= 5000 chars must proceed to analyze_earnings_transcript."""
        import earnings_intel as ei

        long_transcript = "Management commentary: " + ("Revenue grew strongly this quarter. " * 200)
        assert len(long_transcript) >= 5000

        monkeypatch.setattr(ei, "fetch_earnings_transcript", lambda sym: long_transcript)
        called = {"count": 0}

        def fake_analyze(sym, trans):
            called["count"] += 1
            return {"trading_signal": "buy", "management_tone": "confident",
                    "one_line_summary": "Beat on revenue", "guidance_detail": "raised",
                    "key_risks": [], "surprise_elements": []}

        monkeypatch.setattr(ei, "analyze_earnings_transcript", fake_analyze)

        result = ei.get_earnings_intel_section("AAPL", 1)
        assert called["count"] == 1, "analyze_earnings_transcript must be called for long transcripts"
        assert "AAPL" in result
