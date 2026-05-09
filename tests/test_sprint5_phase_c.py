"""
tests/test_sprint5_phase_c.py — Sprint 5 Phase C:
  - refresh_yahoo_symbol_news() writes per-symbol cache
  - refresh_finnhub_news() respects enable_finnhub_news flag
  - _load_cached_symbol_news() reads Yahoo + Finnhub caches
  - _format_l2_for_l3() injects SYMBOL_NEWS when cache exists

Tests:
  PC-01  refresh_yahoo_symbol_news saves {SYM}_yahoo_news.json with correct schema
  PC-02  refresh_yahoo_symbol_news skips crypto symbols (contains "/")
  PC-03  refresh_yahoo_symbol_news respects 30-min TTL (no re-fetch when fresh)
  PC-04  refresh_finnhub_news skips when enable_finnhub_news flag is False
  PC-05  _load_cached_symbol_news reads from _yahoo_news.json
  PC-06  _load_cached_symbol_news reads from _finnhub_news.json
  PC-07  _load_cached_symbol_news returns [] when no cache files exist
  PC-08  _load_cached_symbol_news caps at 3 headlines
  PC-09  _format_l2_for_l3 injects SYMBOL_NEWS line when cache has headlines
  PC-10  _format_l2_for_l3 skips SYMBOL_NEWS line when cache empty
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

# ─────────────────────────────────────────────────────────────────────────────
# PC-01 / PC-02 / PC-03 — refresh_yahoo_symbol_news
# ─────────────────────────────────────────────────────────────────────────────

class TestRefreshYahooSymbolNews:
    def test_pc01_saves_correct_schema(self, tmp_path, monkeypatch):
        """refresh_yahoo_symbol_news writes {SYM}_yahoo_news.json with symbol/fetched_at/articles."""
        import data_warehouse as dw

        monkeypatch.setattr(dw, "NEWS_DIR", tmp_path)

        fake_entry = MagicMock()
        fake_entry.get = lambda k, d=None: {
            "title": "AAPL iPhone demand surge in Asia",
            "link": "https://example.com/aapl",
            "published_parsed": None,
        }.get(k, d)

        fake_feed = MagicMock()
        fake_feed.entries = [fake_entry]

        import feedparser
        monkeypatch.setattr(feedparser, "parse", lambda url, **kw: fake_feed)

        dw.refresh_yahoo_symbol_news(["AAPL"])

        out = tmp_path / "AAPL_yahoo_news.json"
        assert out.exists(), "AAPL_yahoo_news.json was not created"
        data = json.loads(out.read_text())
        assert data["symbol"] == "AAPL"
        assert "fetched_at" in data
        assert isinstance(data["articles"], list)
        assert len(data["articles"]) == 1
        assert data["articles"][0]["headline"] == "AAPL iPhone demand surge in Asia"

    def test_pc02_skips_crypto_symbols(self, tmp_path, monkeypatch):
        """refresh_yahoo_symbol_news must not attempt to fetch for BTC/USD."""
        import data_warehouse as dw

        monkeypatch.setattr(dw, "NEWS_DIR", tmp_path)
        fetched: list[str] = []

        import feedparser
        def spy_parse(url, **kw):
            fetched.append(url)
            return MagicMock(entries=[])
        monkeypatch.setattr(feedparser, "parse", spy_parse)

        dw.refresh_yahoo_symbol_news(["BTC/USD", "ETH/USD", "AAPL"])
        # Only AAPL should be fetched
        assert len(fetched) == 1
        assert "AAPL" in fetched[0]

    def test_pc03_respects_ttl_fresh_cache(self, tmp_path, monkeypatch):
        """refresh_yahoo_symbol_news skips fetch when cache is < 30 min old."""
        import data_warehouse as dw

        monkeypatch.setattr(dw, "NEWS_DIR", tmp_path)

        # Write a fresh cache (5 minutes old)
        fresh_cache = {
            "symbol": "MSFT",
            "fetched_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
            "articles": [{"headline": "MSFT Azure growth accelerates", "source": "yahoo_rss"}],
        }
        cache_path = tmp_path / "MSFT_yahoo_news.json"
        cache_path.write_text(json.dumps(fresh_cache))

        fetched: list[str] = []
        import feedparser
        def spy_parse(url, **kw):
            fetched.append(url)
            return MagicMock(entries=[])
        monkeypatch.setattr(feedparser, "parse", spy_parse)

        dw.refresh_yahoo_symbol_news(["MSFT"])
        assert len(fetched) == 0, "Should NOT fetch when cache is fresh (< 30 min)"

    def test_pc03_refreshes_stale_cache(self, tmp_path, monkeypatch):
        """refresh_yahoo_symbol_news re-fetches when cache is > 30 min old."""
        import data_warehouse as dw

        monkeypatch.setattr(dw, "NEWS_DIR", tmp_path)

        # Write a stale cache (45 minutes old)
        stale_cache = {
            "symbol": "MSFT",
            "fetched_at": (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat(),
            "articles": [],
        }
        cache_path = tmp_path / "MSFT_yahoo_news.json"
        cache_path.write_text(json.dumps(stale_cache))

        fetched: list[str] = []
        import feedparser
        def spy_parse(url, **kw):
            fetched.append(url)
            return MagicMock(entries=[])
        monkeypatch.setattr(feedparser, "parse", spy_parse)

        dw.refresh_yahoo_symbol_news(["MSFT"])
        assert len(fetched) == 1, "Should re-fetch when cache is stale (> 30 min)"


# ─────────────────────────────────────────────────────────────────────────────
# PC-04 — refresh_finnhub_news flag gate
# ─────────────────────────────────────────────────────────────────────────────

class TestRefreshFinnhubNews:
    def test_pc04_skips_when_flag_disabled(self, monkeypatch):
        """refresh_finnhub_news must skip entirely when enable_finnhub_news is False."""
        import data_warehouse as dw

        fetched: list[str] = []

        def fake_is_enabled(flag):
            return False  # all flags off

        import feature_flags
        monkeypatch.setattr(feature_flags, "is_enabled", fake_is_enabled)

        # Patch requests.get to detect any call
        import requests
        def spy_get(url, **kw):
            fetched.append(url)
            return MagicMock(status_code=200, json=lambda: [])
        monkeypatch.setattr(requests, "get", spy_get)

        dw.refresh_finnhub_news(["AAPL", "NVDA"])
        assert len(fetched) == 0, "No HTTP calls when flag is disabled"

