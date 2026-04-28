# Sprint 5 — News Infrastructure, Earnings Intelligence, Feed Upgrades

## Phase A — P0 Fixes (COMPLETE 2026-04-27)

### Investigation Findings

**RSS Feed Timestamp Analysis (live measurements, 2026-04-27):**
| Feed | Entries | First article age | Status |
|------|---------|------------------|--------|
| BBC | 54 | 5.9h | FRESH — OK |
| NYTimes | ? | ? | Assumed OK |
| CNBC-Econ | 30 | 77.3h (3.2d) | STALE — fixed by fallback |
| MarketWatch | 10 | 7697.5h (320.7d) | STALE — fixed by fallback |
| WSJ | 20 | 10925.2h (455.2d) | STALE — fixed by fallback |
| FT | 25 | 1.6h | FRESH — OK |
| Yahoo | 48 | 31.5h (1.3d) | STALE (> 24h cutoff) — replaced |
| Bloomberg | 30 | 0.5h | FRESH — added |
| CNBC-Top | 30 | 1.6h | FRESH — added |

**Root cause:** `fetch_macro_wire()` applies a 24h cutoff (`published_ts < cutoff: continue`).
CNBC-Econ/MarketWatch/WSJ return valid entries but with feed-metadata timestamps 77h–455d old.
These were silently discarded every cycle — 3 of 7 feeds were contributing zero articles.

**earnings_intel.py finding:**
`analyze_earnings_transcript()` was called for short EDGAR press releases (< 500 chars passed, but
Claude returned empty/failed result). Real transcripts are 5,000–30,000 chars. Short 8-K press
releases (earnings announcement with no commentary) waste a Haiku call and produce `"analysis failed"`.

**lxml finding:**
`yfinance.Ticker.calendar` and `.earnings_dates` raise `ImportError: lxml not installed` for all
symbols. lxml was absent from requirements.txt.

### Changes Made

**`macro_wire.py`:**
1. Added `_STALE_FEED_HOURS = 48` constant — articles with feed-reported age > 48h get `published_ts = datetime.now(timezone.utc)` fallback
2. Added `timestamp_source` field to article dict: `"fallback_now"` or `"feed"`
3. Removed Yahoo general RSS (`finance.yahoo.com/news/rssindex`) — consistently 31+ hours stale, outside 24h cutoff
4. Added Bloomberg RSS (`feeds.bloomberg.com/markets/news.rss`) — 30 entries, 0.5h fresh
5. Added CNBC-Top RSS (`www.cnbc.com/id/100003114/device/rss/rss.html`) — 30 entries, 1.6h fresh

**`earnings_intel.py`:**
- Added 5000-char minimum content length guard in `get_earnings_intel_section()`. If transcript < 5000 chars, returns `"Short press release only — full transcript not yet available."` without calling Claude.

**`requirements.txt`:**
- Added `lxml>=4.9,<6`

**`tests/test_news_expansion.py`:**
- Updated `TestMacroWireFeeds.test_new_feeds_present`: replaced "Yahoo" with "Bloomberg" and "CNBC-Top"
- Added `test_yahoo_general_removed`: explicit assertion that Yahoo general feed is absent

**`tests/test_sprint5_phase_a.py` (new):**
- 11 tests (PA-01 through PA-07 + extras)
  - PA-01: 77h stale timestamp → `timestamp_source="fallback_now"`
  - PA-02: 320d stale timestamp → `timestamp_source="fallback_now"`
  - PA-03: 2h fresh timestamp → `timestamp_source="feed"`
  - fallback_now_article_passes_recency_cutoff: WSJ 455d article rescued
  - fallback_now_age_minutes_is_small: fallback article gets age_minutes < 5
  - PA-04: Bloomberg in RSS_FEEDS
  - PA-05: CNBC-Top in RSS_FEEDS
  - PA-06: Yahoo general NOT in RSS_FEEDS
  - _STALE_FEED_HOURS constant exists
  - PA-07: short transcript (<5000 chars) returns placeholder, Claude NOT called
  - long transcript (>=5000 chars) proceeds to analyze_earnings_transcript

### Verification

```
tests/test_sprint5_phase_a.py: 11/11 PASS
tests/test_news_expansion.py: 13/13 PASS (was 12/12, +1 test_yahoo_general_removed)
Full suite (non-chromadb): 1545 PASS (was 1533, +12)
```

RSS_FEEDS verified on server:
- Yahoo: False (removed)
- Bloomberg: True (added)
- CNBC-Top: True (added)
- _STALE_FEED_HOURS: 48

earnings_intel 5000-char guard: verified present
lxml requirement: lxml>=4.9,<6 in requirements.txt

---

## Phase B — catalyst_type alignment (PENDING)

## Phase C — Yahoo symbol RSS + Finnhub news cache (PENDING)

## Phase D — Morning brief earnings integration (PENDING)
