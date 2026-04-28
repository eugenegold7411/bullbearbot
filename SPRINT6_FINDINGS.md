# Sprint 6 Phase A — Earnings Analyst Intelligence + Morning Brief Priority Fix

## Problem (Before)

**Pre-earnings intel gap:** V (earnings tonight), GOOGL/CAT/MA (held, imminent earnings) were
excluded from the morning brief `PRE-EARNINGS INTELLIGENCE` section.

**Root causes:**
1. Calendar JSON insertion order (BE→HOOD→SPOT→V→...) + old flat `cap=3` logic excluded all
   4 held positions — first 3 non-held symbols consumed the cap.
2. `earnings_intel.get_earnings_intel_section()` returned "Short press release only" for all
   pre-earnings symbols (EDGAR has no transcript until post-earnings).
3. No analyst intelligence (beat history, consensus, price targets) available as fallback.
4. `refresh_finnhub_news()` was defined and flag-enabled but never auto-called.
5. `_get_held_symbols()` only read the last decision — which at 4 AM is always an extended
   session decision with `holds=[]` (only crypto tracked in extended session).

---

## Solution (After)

### ITEM 1 — New file: `earnings_intel_fetcher.py`
- `fetch_analyst_intel(sym)` — yfinance beat history (last 4Q) + Finnhub `/stock/recommendation`
- `refresh_earnings_analyst_intel(symbols)` — batch 24h cache to `data/earnings_intel/{SYM}_analyst_intel.json`
- `load_analyst_intel_cached(sym)` — read cache without network calls
- `format_analyst_intel_text(intel)` — formats `Beat: 4/4 avg +2.2% | Consensus: 92% bullish (36 analysts), PT $392 +26.7%`
- Skips crypto symbols; non-fatal per symbol
- NOTE: Finnhub `/stock/price-target` is HTTP 403 on free tier — not used

### ITEM 2 — `morning_brief.py`
New tiered priority ordering in `_build_pre_earnings_intel_section()`:
```
T0: held + ≤2 days   (uncapped)
T1: held + 3–5 days  (uncapped)
T2: !held + ≤1 day   (max 3)
T3: !held + 2–5 days (max 2)
T4: >5 days          (excluded)
```
`_get_held_symbols()`: scans last 50 decisions for holds; falls back to live Alpaca
positions at 4 AM when extended-session decisions have no equity holds.
`_load_analyst_intel(sym)`: reads 24h cache without network call.

### ITEM 3 — `market_data.py`
Analyst intel (beat history + consensus) merged into `earnings_intel_section` after existing
EDGAR transcript loop — injected into Stage 3 prompt.

### ITEM 4 — `data_warehouse.py`
`run_full_refresh()` now calls `refresh_finnhub_news(stock_etfs)` after `refresh_yahoo_symbol_news`.

### ITEM 5 — `scheduler.py`
`_maybe_refresh_earnings_intel()` added: 4:00–5:30 AM ET window, daily guard, wired into
main loop between `_maybe_refresh_macro_intelligence` and `_maybe_run_morning_brief`.

---

## Evidence (server, 2026-04-28 ~02:45 UTC)

### Analyst intel cache (V — earnings tonight):
```
V cache: beat=4/4, bullish=91.8%, analysts=36 PT=$392.33
Formatted: Beat: 4/4 avg +2.2% surprise | Consensus: 92% bullish (36 analysts), PT $392.33 +26.7%
```

### Pre-earnings section output (BEFORE vs AFTER):
```
BEFORE: BE, HOOD, SPOT (cap=3, all non-held)
         V/GOOGL/CAT/MA excluded

AFTER:
=== PRE-EARNINGS INTELLIGENCE ===
  V [HELD] (earnings in 0d):
    Beat: 4/4 avg +2.2% surprise | Consensus: 92% bullish (36 analysts), PT $392.33 +26.7%
  GOOGL [HELD] (earnings in 1d):
    Beat: 4/4 avg +19.9% surprise | Consensus: 90% bullish (56 analysts), PT $378.50 +8.0%
  CAT [HELD] (earnings in 2d):
    Beat: 2/4 avg +3.2% surprise | Consensus: 72% bullish (26 analysts), PT $772.18 -6.8%
  MA [HELD] (earnings in 2d):
    Beat: 4/4 avg +5.4% surprise | Consensus: 92% bullish (36 analysts), PT $652.69 +28.9%
  BE (earnings in 0d):
    Beat: 4/4 avg +175.5% surprise | Consensus: 67% bullish (24 analysts), PT $166.96 -28.9%
  HOOD (earnings in 0d): ...
  SPOT (earnings in 0d): ...
```

### Morning brief (generated 2026-04-28 02:45 UTC):
```
WhatsApp: MORNING BRIEF [MIXED]: V LONG — Earnings today; 4/4 beat history, 92% bullish
Brief generated — tone=mixed  picks=5
```

V is now the #1 pick in the brief.

---

## Test Results

```
tests/test_sprint6_phase_a.py: 22/22 PASS (PA6-01 through PA6-22)
tests/test_sprint5_phase_d.py: 10/10 PASS (backward-compatible)
Server (both files):          32/32 PASS
```

---

# Sprint 6 Phase B — OptionsStructure symbol field fix

## Problem (Before)

`OptionsStructure` had an `underlying` field but no `symbol` field.  `to_dict()`
used `asdict()` which only serializes dataclass fields — so `symbol` was never
written to `structures.json`.  Any raw-dict consumer calling `.get("symbol", "?")`
always received `"?"`.  All 35 existing structures had `symbol=MISSING`.

Also: OCC symbols in Alpaca space-padded format (`NVDA  260522P00205000`) had no
extraction utility.

## Solution

### ITEM 1 — `schemas.py`
- `_occ_to_underlying(occ)`: strips spaces, extracts leading ticker letters from
  any OCC format.
- `OptionsStructure.symbol` property: alias for `self.underlying`.
- `to_dict()`: emits `"symbol": self.underlying` alongside `"underlying"` so
  new saves are queryable by either key.
- `from_dict()`: reads `underlying` → `symbol` → OCC leg fallback.  All 35
  existing structures load correctly without migration.
- `OptionsLeg` in `from_dict()`: `underlying` field uses `.get()` + OCC fallback.

### ITEM 2 — CI Lint Fix
Ruff auto-fixed 6 errors across `scheduler.py` and `tests/test_sprint6_phase_a.py`:
- `I001` import block unsorted (scheduler.py, test_sprint6_phase_a.py ×2)
- `F401` unused imports: `pathlib.Path`, `unittest.mock.MagicMock`, `call`

## Evidence

```
Loaded 35 structures
Structures with empty symbol: 0
  dd990642 underlying='NVDA' symbol='NVDA'
  f45184f1 underlying='WMT' symbol='WMT'
  4ff7189e underlying='XLF' symbol='XLF'
```

## Test Results

```
tests/test_sprint6_phase_a.py: 22/22 PASS (PA6-01 through PA6-22)
tests/test_sprint6_phase_b.py: 16/16 PASS (PB6-01 through PB6-16)
Server (both files):           38/38 PASS
```

---

## Git

| Commit | Description |
|--------|-------------|
| `91ca788` | feat(sprint6-a): earnings analyst intel fetcher + morning brief priority fix |
| `b29d82d` | fix(sprint6-a): _get_held_symbols falls back to Alpaca positions at 4 AM |
| `10eccae` | fix(sprint6-b): OptionsStructure symbol field + OCC extraction helper |
| `4c32224` | feat(sprint6-gh): tiered conviction-based margin multiplier |

**Total new tests:** 22 (Sprint 6-A) + 16 (Sprint 6-B) + 13 (Sprint 6-G/H) = 51 new tests
**Server: active (running)**

---

# Sprint 6 Phase G/H — Tiered Conviction-Based Margin Multiplier

## Problem (Before)

All HIGH+ conviction trades used the same flat 4x margin multiplier, regardless of
whether conviction was 0.65 (barely qualified) or 0.90 (extremely high).
Result: no sizing differentiation within the HIGH conviction band.

---

## Solution (After)

### ITEM 1 — `risk_kernel.py`
New `_get_margin_multiplier(conviction, symbol, config)` helper:
- Reads `margin_sizing_multiplier_tiers` from config (4 tiers: MEDIUM/HIGH/STRONG HIGH/VERY HIGH)
- Crypto cap: BTC/USD and ETH/USD capped at `max_crypto_margin_multiplier` (default 2.0)
- Fallback: flat `margin_sizing_multiplier` when no tiers in config (backward compat)

`_compute_sizing_basis()` updated:
- Tiered path (tiers present): uses `_get_margin_multiplier()` for conviction >= medium_thresh
- Legacy path (no tiers): preserves original HIGH→mult, MEDIUM→mult/2 split
- Added `symbol: str = ""` parameter for crypto cap support

`_effective_exposure_cap()` updated:
- Uses `max(tier multipliers) = 4.0` when tiers present (was reading flat mult)

`size_position()`: passes `idea.symbol` to `_compute_sizing_basis()`.

### ITEM 2 — `strategy_config.json`
Added `margin_sizing_multiplier_tiers` to `parameters`:
```json
"medium":      {"min": 0.50, "max": 0.6499, "multiplier": 1.0},
"high":        {"min": 0.65, "max": 0.7249, "multiplier": 2.0},
"strong_high": {"min": 0.725, "max": 0.7999, "multiplier": 3.0},
"very_high":   {"min": 0.80, "max": 1.0,    "multiplier": 4.0}
```
Also added `max_crypto_margin_multiplier: 2.0`.

---

## Evidence (server, 2026-04-28)

### Sizing ladder output:
```
=== TIERED MARGIN MULTIPLIER SIZING LADDER ===
Equity: $102,000 | BP: $116,000

Label           Conv    Symbol     Mult   Size         % Equity
MEDIUM          0.55    NVDA       1.0x  $15,300      15.0%
HIGH            0.65    NVDA       2.0x  $23,200      22.7%
HIGH-mid        0.70    NVDA       2.0x  $23,200      22.7%
HIGH-top        0.7249  NVDA       2.0x  $23,200      22.7%
STRONG HIGH     0.725   NVDA       3.0x  $23,200      22.7%
STRONG HIGH     0.75    NVDA       3.0x  $23,200      22.7%
VERY HIGH       0.80    NVDA       4.0x  $23,200      22.7%
VERY HIGH       0.90    NVDA       4.0x  $23,200      22.7%
BTC-HIGH        0.80    BTC/USD    2.0x  $23,200      22.7%
BTC-VHIGH       0.90    BTC/USD    2.0x  $23,200      22.7%

PDT_FLOOR: $26,000 — INTACT: True
```

Note: 2x/3x/4x tiers all give $23,200 at this account size because BP=$116K is the
binding constraint for any HIGH+ conviction trade (bp < equity × 2.0 at current size).
Tier differentiation emerges as account grows and BP scales.

### Test results:
```
tests/test_sprint6_phase_gh.py: 13/13 PASS
tests/test_risk_kernel_size_position.py: all passing (no regressions)
tests/test_sprint4_conviction_alignment.py: all passing (no regressions)
Server full suite: 1651 passed (was 1638 baseline) — +13 new, 0 regressions
```
