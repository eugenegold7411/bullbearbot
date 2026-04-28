"""
tests/test_news_expansion.py — Commit 1: news pipeline expansion + noise filter.

Covers:
  - Alpaca news no-cap (51 watchlist symbols passed, not just 20)
  - Form 4 fetcher iterates all symbols (no [:25])
  - macro_wire.RSS_FEEDS contains the new sources, drops Investing.com
  - _FILING_NOISE_RE rejects Form/transcript/animal-life-event noise
  - 15-minute fixed slot key in scheduler._maybe_refresh_macro_wire
  - classify_articles() injects watchlist symbols into the system prompt
"""
from __future__ import annotations

import inspect

# ═══════════════════════════════════════════════════════════════════════════
# Change 1A — Alpaca news no-symbol-cap
# ═══════════════════════════════════════════════════════════════════════════

class TestAlpacaNewsCap:
    def test_no_twenty_symbol_cap_in_market_data(self):
        """market_data.fetch_all builds news_syms without [:20]."""
        import inspect as _i

        import market_data as md
        src = _i.getsource(md.fetch_all)
        # The two news_syms assignments must not slice [:20]
        assert "news_syms" in src
        assert "[:20]" not in src, (
            "market_data.fetch_all still has a [:20] slice on news_syms"
        )

    def test_crypto_symbols_excluded_from_news_syms(self):
        """The news_syms list still filters out '/' (crypto)."""
        import inspect as _i

        import market_data as md
        src = _i.getsource(md.fetch_all)
        # We still strip crypto symbols
        assert '"/" not in s' in src


# ═══════════════════════════════════════════════════════════════════════════
# Change 1B — Form 4 no-symbol-cap
# ═══════════════════════════════════════════════════════════════════════════

class TestForm4Cap:
    def test_no_twenty_five_cap_in_form4(self):
        """fetch_form4_insider_trades iterates all symbols, not just [:25]."""
        import insider_intelligence as ii
        src = inspect.getsource(ii.fetch_form4_insider_trades)
        assert "for symbol in symbols:" in src
        assert "symbols[:25]" not in src

    def test_form4_walks_all_input_symbols(self, monkeypatch):
        """If 30 symbols are passed, _fetch_edgar_form4 is called 30 times."""
        import insider_intelligence as ii

        # Force cache refresh
        monkeypatch.setattr(ii, "_is_stale", lambda *a, **kw: True)
        monkeypatch.setattr(ii, "_save_cache", lambda *a, **kw: None)
        monkeypatch.setattr(ii, "_load_cache", lambda *a, **kw: {})

        seen: list[str] = []

        def fake_edgar(sym, start, end):
            seen.append(sym)
            return []

        monkeypatch.setattr(ii, "_fetch_edgar_form4", fake_edgar)

        symbols = [f"SYM{i:02d}" for i in range(30)]
        ii.fetch_form4_insider_trades(symbols, days_back=30)
        assert len(seen) == 30, f"Expected 30 calls, got {len(seen)}"


# ═══════════════════════════════════════════════════════════════════════════
# Change 1C — Feed expansion
# ═══════════════════════════════════════════════════════════════════════════

class TestMacroWireFeeds:
    def test_investing_com_not_in_feeds(self):
        from macro_wire import RSS_FEEDS
        urls = [u for _, u in RSS_FEEDS]
        assert not any("investing.com" in u.lower() for u in urls), \
            "Investing.com must be removed from RSS_FEEDS"

    def test_new_feeds_present(self):
        from macro_wire import RSS_FEEDS
        sources = {name for name, _ in RSS_FEEDS}
        # CNBC-Econ/MarketWatch/WSJ/FT retained; Bloomberg+CNBC-Top added 2026-04-27
        for expected in ("CNBC-Econ", "MarketWatch", "WSJ", "FT", "Bloomberg", "CNBC-Top"):
            assert expected in sources, f"{expected} missing from RSS_FEEDS"

    def test_yahoo_general_removed(self):
        """Yahoo general feed dropped 2026-04-27 — consistently 30-32h stale."""
        from macro_wire import RSS_FEEDS
        urls = [u for _, u in RSS_FEEDS]
        assert not any("finance.yahoo.com/news/rssindex" in u for u in urls), \
            "Yahoo general news RSS must be removed from RSS_FEEDS"

    def test_legacy_feeds_retained(self):
        from macro_wire import RSS_FEEDS
        sources = {name for name, _ in RSS_FEEDS}
        assert "BBC" in sources
        assert "NYTimes" in sources


# ═══════════════════════════════════════════════════════════════════════════
# Change 1D — Filing noise filter
# ═══════════════════════════════════════════════════════════════════════════

class TestFilingNoiseFilter:
    def test_filter_blocks_form_filings(self):
        from macro_wire import _FILING_NOISE_RE
        noisy = [
            "Form 13G Nurix Therapeutics For: 24 April",
            "Form 13G/A filing by Vanguard Group",
            "Form 144: Insider to Sell 50,000 Shares",
            "Form 4 filed by John Doe",
            "SC 13G Vanguard filing",
            "SC 13D activist stake disclosure",
            "Amendment to Form 10-K filed by Tesla",
            "Earnings call transcript: Romande Energie beats Q4 2025",
            "31 Sloths Acquired by Orlando Animal Attraction",
            "12 Birds Died at Local Zoo",
        ]
        for t in noisy:
            assert _FILING_NOISE_RE.match(t), f"Should be filtered: {t!r}"

    def test_filter_passes_real_news(self):
        from macro_wire import _FILING_NOISE_RE
        real = [
            "Fed signals rate cut pause amid tariff uncertainty",
            "Powell speaks at Jackson Hole symposium",
            "OPEC+ agrees to extend production cuts",
            "China GDP misses expectations in Q1",
            "Iran cancels nuclear talks with U.S.",
            "Apple unveils new iPhone at Cupertino event",
            "Tesla beats Q1 revenue estimates",
            "Russian forces pound Ukraine's Dnipro region",
        ]
        for t in real:
            assert not _FILING_NOISE_RE.match(t), f"Should NOT be filtered: {t!r}"


# ═══════════════════════════════════════════════════════════════════════════
# Change 1E — 15-minute fixed slot key
# ═══════════════════════════════════════════════════════════════════════════

class TestFifteenMinuteSlot:
    def test_slot_key_is_session_independent(self):
        """_maybe_refresh_macro_wire computes slot from %M//15, not session."""
        import scheduler as sch
        src = inspect.getsource(sch._maybe_refresh_macro_wire)
        # Fixed 15-min slot key — must contain the literal divisor
        assert "minute // 15" in src
        # Old session-dependent computation must be gone
        assert "interval_sec" not in src
        assert "slot_min" not in src

    def test_slot_key_format_matches_15min_buckets(self):
        """Verify the slot-key string format yields 4 buckets per hour."""
        from datetime import datetime
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
        # Exercise across an hour
        seen_buckets = set()
        for minute in range(60):
            t = datetime(2026, 4, 28, 14, minute, 0, tzinfo=ET)
            slot = t.strftime("%Y-%m-%d-%H-") + str(t.minute // 15)
            seen_buckets.add(slot)
        # 4 distinct buckets per hour
        assert len(seen_buckets) == 4


# ═══════════════════════════════════════════════════════════════════════════
# Change 1F — Watchlist injected into Haiku classifier prompt
# ═══════════════════════════════════════════════════════════════════════════

class TestClassifierWatchlistInjection:
    def test_classifier_includes_watchlist_in_prompt(self, monkeypatch):
        """classify_articles() injects 'Tracked portfolio symbols:' into system prompt."""
        import macro_wire as mw

        captured: dict = {}

        class FakeUsage:
            input_tokens = 50
            output_tokens = 20
            cache_creation_input_tokens = 0
            cache_read_input_tokens = 0

        class FakeContent:
            def __init__(self, txt):
                self.text = txt

        class FakeResp:
            def __init__(self):
                self.content = [FakeContent('[{"is_market_moving":true,"direction":"neutral",'
                                             '"affected_sectors":[],"affected_symbols":[],'
                                             '"urgency":"background","one_line_summary":"x"}]')]
                self.usage = FakeUsage()

        def fake_create(**kwargs):
            captured["system"] = kwargs.get("system")
            captured["messages"] = kwargs.get("messages")
            return FakeResp()

        # Patch Claude messages.create
        monkeypatch.setattr(mw._claude.messages, "create", fake_create)

        articles = [{
            "headline": "Fed considers rate cuts amid tariff escalation",
            "summary":  "x" * 50,
            "impact_score": 6.0,
            "keyword_tier": "high",
        }]
        mw.classify_articles(articles)

        # Extract system prompt text — list of dicts when cache_control used
        sys_block = captured["system"]
        if isinstance(sys_block, list):
            sys_text = sys_block[0].get("text", "")
        else:
            sys_text = sys_block

        assert "Tracked portfolio symbols" in sys_text, (
            "system prompt must mention 'Tracked portfolio symbols' "
            f"(got: {sys_text[:200]!r})"
        )
        # And include at least one well-known watchlist symbol
        # (NVDA is in the live watchlist_core.json)
        assert "NVDA" in sys_text
