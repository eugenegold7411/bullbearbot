"""
tests/test_earnings_edgar_fix.py — EDGAR earnings transcript fix tests

ET-01  fetch_earnings_transcript("GOOGL") returns > 500 chars via mocked EDGAR
ET-02  fetch_earnings_transcript("AMZN") returns > 500 chars via mocked EDGAR
ET-03  _in_earnings_fetch_window() returns True at 5:00 AM ET (pre-market)
ET-04  _in_earnings_fetch_window() returns True at 8:30 AM ET (pre-market open window)
ET-05  Cache file written to data/earnings/ after successful fetch
ET-06  Cache file used on second call — no second EDGAR hit within 24h
ET-07  EDGAR unavailable + yfinance unavailable → returns "" without exception
ET-08  get_earnings_intel_section shows real content for near-earnings symbol
ET-09  _RATE_LIMIT_DELAY >= 0.10 (rate limiting constant enforced)
ET-10  _CIK_MAP correct for all 5 major symbols
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_submissions_response(cik: str, acc: str, date: str = "2026-04-29") -> dict:
    """Minimal EDGAR submissions JSON response."""
    return {
        "filings": {
            "recent": {
                "form":            ["8-K"],
                "filingDate":      [date],
                "accessionNumber": [acc],
                "primaryDocument": ["exhibit991.htm"],
            }
        }
    }


def _fake_requests_get(url: str, **kwargs):
    """Route mock HTTP calls to fake EDGAR endpoints."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()

    if "data.sec.gov/submissions" in url:
        # CIK → submissions
        if "1652044" in url:            # GOOGL
            resp.status_code = 200
            resp.json.return_value = _make_submissions_response("1652044", "0001652044-26-000043")
        elif "1018724" in url:          # AMZN
            resp.status_code = 200
            resp.json.return_value = _make_submissions_response("1018724", "0001018724-26-000012")
        else:
            resp.status_code = 404
            resp.json.return_value = {}

    elif "-index.htm" in url:
        # Filing index — return HTML with exhibit link
        resp.status_code = 200
        resp.text = (
            '<html><body>'
            '<a href="/Archives/edgar/data/1652044/000165204426000043/exhibit991q1.htm">'
            'Exhibit 99.1</a>'
            '</body></html>'
        )

    elif "exhibit991" in url or "ex99" in url.lower():
        # Exhibit 99.1 press release
        resp.status_code = 200
        resp.text = (
            "Alphabet Announces First Quarter 2026 Results\n"
            "Revenue: $109.9B (+22% YoY). Cloud grew 63% to $20.0B. "
            "EPS $5.11, beat estimate of $4.62 by +10.6%. "
            "Guidance raised for Q2: $111-113B vs consensus $109B. "
            "Operating margin expanded to 36.1% (+2pp YoY). "
            "Management tone: confident. AI investments driving all segments. "
            "Search queries at all-time high. YouTube ads up +11%. "
            "Google One subscriptions reached 350M paid users. "
            "Analyst reaction positive — stock up +8.9% next day. "
        ) * 20  # repeat to exceed 500-char threshold

    else:
        resp.status_code = 200
        resp.text = ""

    return resp


# ── ET-01 / ET-02 ─────────────────────────────────────────────────────────────

class TestFetchTranscript:
    """ET-01, ET-02 — real content returned when EDGAR path works."""

    def _run_fetch(self, symbol: str, tmp_path: Path) -> str:
        import earnings_intel as ei
        with (
            patch.object(ei, "_EARNINGS_DIR", tmp_path),
            patch("earnings_intel.requests.get", side_effect=_fake_requests_get),
            patch("earnings_intel._in_earnings_fetch_window", return_value=True),
            patch("earnings_intel._time") as mock_time,
        ):
            mock_time.sleep = MagicMock()
            return ei.fetch_earnings_transcript(symbol)

    def test_et01_googl_returns_real_content(self, tmp_path):
        """ET-01: GOOGL transcript > 500 chars."""
        result = self._run_fetch("GOOGL", tmp_path)
        assert len(result) > 500, f"Expected >500 chars, got {len(result)}: {result[:100]}"
        # Should contain meaningful text (not just yfinance fundamentals)
        assert "yfinance fundamentals for" not in result

    def test_et02_amzn_returns_real_content(self, tmp_path):
        """ET-02: AMZN transcript > 500 chars."""
        result = self._run_fetch("AMZN", tmp_path)
        assert len(result) > 500, f"Expected >500 chars, got {len(result)}: {result[:100]}"
        assert "yfinance fundamentals for" not in result


# ── ET-03 / ET-04 ─────────────────────────────────────────────────────────────

class TestFetchWindow:
    """ET-03, ET-04 — fetch window opens at expected times."""

    def _window_at(self, hour: int, minute: int = 0) -> bool:
        from zoneinfo import ZoneInfo

        import earnings_intel as ei
        now = datetime(2026, 4, 30, hour, minute, tzinfo=ZoneInfo("America/New_York"))
        return ei._in_earnings_fetch_window(_now=now)

    def test_et03_pre_market_5am_open(self):
        """ET-03: pre-market window open at 5:00 AM ET (next-morning fetch after post-market earnings)."""
        assert self._window_at(5, 0) is True

    def test_et04_pre_market_830am_open(self):
        """ET-04: pre-market window open at 8:30 AM ET (covers same-day pre-earnings print)."""
        assert self._window_at(8, 30) is True

    def test_market_hours_closed(self):
        """Market hours (10 AM - 4 PM ET) are outside the fetch window."""
        assert self._window_at(10, 0) is False
        assert self._window_at(14, 0) is False

    def test_post_market_open(self):
        """Post-market 5 PM ET is within fetch window."""
        assert self._window_at(17, 0) is True

    def test_late_evening_open(self):
        """10 PM ET is within extended post-market window (8-K filings available)."""
        assert self._window_at(22, 0) is True

    def test_midnight_closed(self):
        """After 11 PM ET, window is closed (0:30 AM)."""
        assert self._window_at(0, 30) is False


# ── ET-05 / ET-06 ─────────────────────────────────────────────────────────────

class TestTranscriptCache:
    """ET-05, ET-06 — cache written after fetch; reused on second call."""

    def test_et05_cache_written_after_fetch(self, tmp_path):
        """ET-05: cache file created in data/earnings/ after successful EDGAR fetch."""
        import earnings_intel as ei
        with (
            patch.object(ei, "_EARNINGS_DIR", tmp_path),
            patch("earnings_intel.requests.get", side_effect=_fake_requests_get),
            patch("earnings_intel._in_earnings_fetch_window", return_value=True),
            patch("earnings_intel._time") as mock_time,
        ):
            mock_time.sleep = MagicMock()
            ei.fetch_earnings_transcript("GOOGL")

        cache_file = tmp_path / "GOOGL_transcript_cache.json"
        assert cache_file.exists(), "Cache file not written after successful EDGAR fetch"
        data = json.loads(cache_file.read_text())
        assert "transcript" in data
        assert len(data["transcript"]) > 500
        assert "cached_at" in data

    def test_et06_cache_used_on_second_call(self, tmp_path):
        """ET-06: second call uses cache without hitting EDGAR again."""
        import earnings_intel as ei
        call_count = {"n": 0}
        real_get = _fake_requests_get

        def counting_get(url, **kwargs):
            call_count["n"] += 1
            return real_get(url, **kwargs)

        with (
            patch.object(ei, "_EARNINGS_DIR", tmp_path),
            patch("earnings_intel.requests.get", side_effect=counting_get),
            patch("earnings_intel._in_earnings_fetch_window", return_value=True),
            patch("earnings_intel._time") as mock_time,
        ):
            mock_time.sleep = MagicMock()
            first = ei.fetch_earnings_transcript("GOOGL")
            calls_after_first = call_count["n"]
            second = ei.fetch_earnings_transcript("GOOGL")
            calls_after_second = call_count["n"]

        assert len(first) > 500
        assert second == first, "Second call should return identical cached content"
        assert calls_after_second == calls_after_first, (
            "Second call should use cache without any EDGAR requests"
        )

    def test_yfinance_stub_not_served_from_cache(self, tmp_path):
        """yfinance stubs in cache are treated as stale → EDGAR retried on next window."""
        import earnings_intel as ei
        # Write a yfinance stub to cache
        cache_file = tmp_path / "GOOGL_transcript_cache.json"
        cache_file.write_text(json.dumps({
            "symbol":    "GOOGL",
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "transcript": "yfinance fundamentals for GOOGL: trailingEps: 13.12",
        }))

        # _load_transcript_cache should return "" for yfinance stubs
        with patch.object(ei, "_EARNINGS_DIR", tmp_path):
            result = ei._load_transcript_cache("GOOGL")
        assert result == "", "yfinance stubs should not be served from cache"


# ── ET-07 ─────────────────────────────────────────────────────────────────────

class TestGracefulFallback:
    """ET-07 — EDGAR unavailable returns "" (no exception, no silent stub crash)."""

    def test_et07_edgar_unavailable_returns_empty(self, tmp_path):
        """ET-07: EDGAR + yfinance both fail → returns '' without raising."""
        import earnings_intel as ei

        def failing_get(url, **kwargs):
            raise RuntimeError("network error")

        with (
            patch.object(ei, "_EARNINGS_DIR", tmp_path),
            patch("earnings_intel.requests.get", side_effect=failing_get),
            patch("earnings_intel._in_earnings_fetch_window", return_value=True),
            patch("earnings_intel._time") as mock_time,
            patch("earnings_intel._yfinance_fallback", return_value=""),
        ):
            mock_time.sleep = MagicMock()
            result = ei.fetch_earnings_transcript("GOOGL")

        assert result == "", f"Expected '' on total failure, got: {result!r}"

    def test_yfinance_fallback_not_cached(self, tmp_path):
        """yfinance fallback is returned but NOT written to transcript cache."""
        import earnings_intel as ei

        def no_hits_get(url, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.status_code = 200
            if "submissions" in url:
                resp.json.return_value = {"filings": {"recent": {"form": [], "filingDate": [], "accessionNumber": []}}}
            else:
                resp.json.return_value = {"hits": {"hits": []}}
            return resp

        with (
            patch.object(ei, "_EARNINGS_DIR", tmp_path),
            patch("earnings_intel.requests.get", side_effect=no_hits_get),
            patch("earnings_intel._in_earnings_fetch_window", return_value=True),
            patch("earnings_intel._time") as mock_time,
        ):
            mock_time.sleep = MagicMock()
            ei.fetch_earnings_transcript("GOOGL")

        # yfinance stub may be returned (if yfinance is available) or ""
        # Either way, it must NOT be in the cache
        cache_file = tmp_path / "GOOGL_transcript_cache.json"
        if cache_file.exists():
            data = json.loads(cache_file.read_text())
            assert not data.get("transcript", "").startswith("yfinance fundamentals for"), (
                "yfinance stubs must not be written to transcript cache"
            )


# ── ET-08 ─────────────────────────────────────────────────────────────────────

class TestEarningsIntelSection:
    """ET-08 — get_earnings_intel_section returns real content for near-earnings symbol."""

    def test_et08_intel_section_contains_real_content(self, tmp_path):
        """ET-08: section contains more than a stub message when transcript is real."""
        import earnings_intel as ei

        fake_transcript = (
            "Alphabet Announces Q1 2026 Results. Revenue $109.9B (+22%). "
            "Cloud grew 63% to $20.0B. EPS $5.11 beat estimate $4.62 by 10.6%. "
            "Operating margin 36.1%. Guidance raised Q2 $111-113B. "
            "Management tone confident. AI driving growth across segments. "
        ) * 80  # > 5000 chars (crosses the 'short press release' threshold)

        fake_analysis = {
            "eps_beat_miss":      "+10.6% beat",
            "revenue_beat_miss":  "+2% beat",
            "guidance_direction": "raised",
            "guidance_detail":    "Q2 raised above consensus",
            "management_tone":    "confident",
            "key_risks":          ["tariff exposure", "competition"],
            "surprise_elements":  ["Cloud 63% growth"],
            "analyst_sentiment":  "positive",
            "trading_signal":     "bullish",
            "one_line_summary":   "Clean beat, raised guidance, AI everywhere",
        }

        with (
            patch.object(ei, "_EARNINGS_DIR", tmp_path),
            patch("earnings_intel.fetch_earnings_transcript", return_value=fake_transcript),
            patch("earnings_intel.analyze_earnings_transcript", return_value=fake_analysis),
        ):
            section = ei.get_earnings_intel_section("GOOGL", days_to_earnings=-1)

        assert "bullish" in section.lower() or "beat" in section.lower(), (
            f"Section should contain trading signal or beat info, got: {section}"
        )
        assert len(section) > 50, f"Section too short: {section!r}"
        assert "EDGAR transcript unavailable" not in section

    def test_et08_yfinance_stub_clearly_labeled(self, tmp_path):
        """Yfinance stub section clearly labels as unavailable, not 'Short press release'."""
        import earnings_intel as ei
        stub = "yfinance fundamentals for GOOGL: trailingEps: 13.12  |  forwardEps: 13.53"

        with (
            patch.object(ei, "_EARNINGS_DIR", tmp_path),
            patch("earnings_intel.fetch_earnings_transcript", return_value=stub),
        ):
            section = ei.get_earnings_intel_section("GOOGL", days_to_earnings=-1)

        assert "EDGAR transcript unavailable" in section, (
            f"yfinance stub should be labeled as unavailable, got: {section!r}"
        )
        assert "Short press release only" not in section


# ── ET-09 ─────────────────────────────────────────────────────────────────────

class TestRateLimiting:
    """ET-09 — EDGAR rate limiting constant enforced."""

    def test_et09_rate_limit_delay_at_least_100ms(self):
        """ET-09: _RATE_LIMIT_DELAY >= 0.10 seconds (SEC allows 10 req/s)."""
        import earnings_intel as ei
        assert ei._RATE_LIMIT_DELAY >= 0.10, (
            f"_RATE_LIMIT_DELAY must be >= 0.10s, got {ei._RATE_LIMIT_DELAY}"
        )

    def test_rate_limit_sleep_called_during_fetch(self, tmp_path):
        """sleep() is called between EDGAR requests during a fetch."""
        import earnings_intel as ei
        sleep_calls = []

        def record_sleep(secs):
            sleep_calls.append(secs)

        with (
            patch.object(ei, "_EARNINGS_DIR", tmp_path),
            patch("earnings_intel.requests.get", side_effect=_fake_requests_get),
            patch("earnings_intel._in_earnings_fetch_window", return_value=True),
            patch("earnings_intel._time") as mock_time,
        ):
            mock_time.sleep = record_sleep
            ei.fetch_earnings_transcript("GOOGL")

        assert len(sleep_calls) >= 1, "Expected at least one rate-limit sleep during EDGAR fetch"
        for s in sleep_calls:
            assert s >= 0.10, f"Sleep value too short: {s}"


# ── ET-10 ─────────────────────────────────────────────────────────────────────

class TestCIKMap:
    """ET-10 — _CIK_MAP has correct CIKs for major symbols."""

    @pytest.mark.parametrize("symbol,expected_cik", [
        ("GOOGL", "1652044"),
        ("AMZN",  "1018724"),
        ("MSFT",  "789019"),
        ("META",  "1326801"),
        ("AAPL",  "320193"),
    ])
    def test_et10_cik_map_correct(self, symbol, expected_cik):
        """ET-10: _CIK_MAP contains correct SEC CIK for each major symbol."""
        import earnings_intel as ei
        assert symbol in ei._CIK_MAP, f"{symbol} missing from _CIK_MAP"
        assert ei._CIK_MAP[symbol] == expected_cik, (
            f"{symbol}: expected CIK {expected_cik}, got {ei._CIK_MAP[symbol]}"
        )


# ── Structural fixes ──────────────────────────────────────────────────────────

class TestStructuralFixes:
    """Verify the two root-cause bugs are fixed in code structure."""

    def test_search_8k_uses_submissions_for_known_cik(self, tmp_path):
        """_search_8k_filings uses submissions API (not EFTS) when CIK is in map."""
        import earnings_intel as ei

        submissions_called = {"n": 0}
        efts_called = {"n": 0}

        def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.status_code = 200
            if "data.sec.gov/submissions" in url:
                submissions_called["n"] += 1
                resp.json.return_value = _make_submissions_response(
                    "1652044", "0001652044-26-000043"
                )
            elif "efts.sec.gov" in url:
                efts_called["n"] += 1
                resp.json.return_value = {"hits": {"hits": []}}
            return resp

        with patch("earnings_intel.requests.get", side_effect=mock_get):
            result = ei._search_8k_filings("GOOGL")

        assert submissions_called["n"] >= 1, "Submissions API should have been called for GOOGL"
        assert efts_called["n"] == 0, "EFTS should NOT be called when CIK is known"
        assert len(result) > 0, "Should return hits from submissions API"

    def test_efts_fallback_uses_adsh_field(self, tmp_path):
        """EFTS fallback reads 'adsh' field (not the old 'accession_no' field)."""
        import earnings_intel as ei

        def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.status_code = 200
            # Simulate EFTS response with adsh field (no 'accession_no')
            resp.json.return_value = {
                "hits": {
                    "hits": [{
                        "_source": {
                            "adsh":      "0001234567-26-000099",   # correct field
                            "ciks":      ["0001234567"],
                            "file_date": "2026-04-01",
                            # 'accession_no' is absent (it was the wrong field name)
                        }
                    }]
                }
            }
            return resp

        # Use an unknown symbol so it falls through to EFTS
        with patch("earnings_intel.requests.get", side_effect=mock_get):
            result = ei._search_8k_filings("FAKESTOCK_UNKNOWN")
        assert len(result) == 1
        assert result[0]["accession_no"] == "0001234567-26-000099"
        assert result[0]["cik"] == "1234567"

    def test_fetch_filing_uses_exhibit_url(self, tmp_path):
        """_fetch_filing_text fetches EX-99.1 exhibit, not the XBRL primary doc."""
        import earnings_intel as ei

        fetched_urls = []

        def mock_get(url, **kwargs):
            fetched_urls.append(url)
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            if "-index.htm" in url:
                resp.text = (
                    '<a href="/Archives/edgar/data/123/000123456726000001/exhibit991earnings.htm">'
                    'EX-99.1</a>'
                )
            else:
                resp.text = "Real earnings press release content " * 100
            return resp

        with patch("earnings_intel.requests.get", side_effect=mock_get):
            with patch("earnings_intel._time") as mock_time:
                mock_time.sleep = MagicMock()
                result = ei._fetch_filing_text("0001234567-26-000001", "123456")

        exhibit_fetched = any("exhibit991" in u.lower() for u in fetched_urls)
        assert exhibit_fetched, (
            f"Expected exhibit URL to be fetched. URLs fetched: {fetched_urls}"
        )
        assert len(result) > 100, "Expected real content from exhibit fetch"
