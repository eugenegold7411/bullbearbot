# Sprint 4 — Conviction Threshold Alignment
**Date:** 2026-04-27
**Baseline tests:** 1521 passing (post Sprint 2.5)
**Final tests:** 1548 passing (+27 new, 0 regressions)

---

## P0 Audit — `_float_to_conviction()` Impact

### All callers identified
Grep: `grep -rn "_float_to_conviction" /home/trading-bot --include="*.py"`

| Location | Line | What it does with result |
|----------|------|--------------------------|
| `risk_kernel.py` | 587 | Sets `conviction` on `BrokerAction` returned from HOLD path |
| `risk_kernel.py` | 612 | Sets `conviction` on `BrokerAction` returned from CLOSE/SELL path |
| `risk_kernel.py` | 661 | Sets `conviction` on `BrokerAction` returned from BUY path |
| `risk_kernel.py` | 743 | Sets `conviction` on `BrokerAction` returned from REALLOCATE path |
| `risk_kernel.py` | 1014 | Sets `conviction` on `OptionsAction` HOLD passthrough |
| `risk_kernel.py` | 1118 | Sets `conviction` on `OptionsAction` BUY output |

**All 6 callers are within `risk_kernel.py`. Zero external callers.**

### Conviction enum consumers (outside risk_kernel)
`BrokerAction.conviction` is serialized as `"confidence"` (string) via `BrokerAction.to_dict()` (`schemas.py:392`).

| Consumer | File | Impact |
|----------|------|--------|
| Soft exposure warning | `order_executor.py:364–383` | Reads `action.get("confidence")` string. Emits `log.warning()` only — **no hard rejection, no execution blocking.** Would change from "medium" to "high" label for 0.65–0.74 band trades post-fix: soft cap from 1.5× to 3.0× equity, but both are above current BP ($116K) so check still passes. |
| Outcome logging | `decision_outcomes.py:73, 211` | Stores `"confidence"` label ("high"/"medium"/"low") in analytics JSONL. Purely analytical — no execution gating. Post-fix: gap-band trades labeled "high" instead of "medium". |

**Conviction-based sizing uses `idea.conviction` (raw float) directly** in `_compute_sizing_basis()`, `size_position()`, `_effective_exposure_cap()`, `eligibility_check()`. The Conviction enum produced by `_float_to_conviction()` is labeling only.

### Audit conclusion
**SAFE TO IMPLEMENT.** `_float_to_conviction()` has zero external callers. The only downstream effect is correcting the `"confidence"` label in warning logs and analytics records. No execution gating depends on this enum value.

---

## P1 — HIGH CORE Tier Bump Fix

**File:line:** `risk_kernel.py:414` (was `risk_kernel.py:410` before P3 additions shifted lines)

**Before:**
```python
if idea.conviction >= 0.75 and idea.tier == Tier.CORE:
    tier_pct = _CORE_HIGH_CONVICTION_PCT  # 20%
```

**After:**
```python
_high_thresh = float(
    _params(config).get("margin_sizing_conviction_thresholds", {}).get("high", 0.75)
)
if idea.conviction >= _high_thresh and idea.tier == Tier.CORE:
    tier_pct = _CORE_HIGH_CONVICTION_PCT  # 20%
```

**Sizing impact:** Convictions 0.65–0.74 now correctly use 20% tier_pct instead of 15%.

| Conviction | Before fix | After fix | Delta |
|---|---|---|---|
| 0.68 CORE | $17,400 (15% × $116K) | **$23,200 (20% × $116K)** | +$5,800 |
| 0.72 CORE | $17,400 | **$23,200** | +$5,800 |
| 0.80 CORE | $23,200 | $23,200 (unchanged) | — |

**Safety:** Default=0.75 preserved when config is absent — existing behavior unchanged if config doesn't include `margin_sizing_conviction_thresholds`.

---

## P2 — `_effective_exposure_cap()` Config-Driven

**File:lines:** `risk_kernel.py:197–229` (function definition) and call site at line 442.

**Before (hardcoded):**
```python
def _effective_exposure_cap(snapshot: BrokerSnapshot, conviction: float) -> float:
    equity = snapshot.equity
    bp     = max(snapshot.buying_power, equity)
    if conviction >= 0.75:       # HARDCODED threshold
        cap = equity * 3.0       # HARDCODED multiplier
    elif conviction >= 0.50:     # HARDCODED threshold
        cap = equity * 1.5       # HARDCODED
    else:
        cap = equity * 1.0
    return min(cap, equity * 3.0, bp)   # HARDCODED ceiling
```

**After (config-driven):**
```python
def _effective_exposure_cap(
    snapshot: BrokerSnapshot,
    conviction: float,
    config: dict | None = None,
) -> float:
    cfg  = config or {}
    equity = snapshot.equity
    bp     = max(snapshot.buying_power, equity)
    thresholds = _params(cfg).get("margin_sizing_conviction_thresholds", {})
    high_t = float(thresholds.get("high", 0.75))
    med_t  = float(thresholds.get("medium", 0.50))
    mult   = float(_params(cfg).get("margin_sizing_multiplier", 3.0))
    if conviction >= high_t:
        cap = equity * mult
    elif conviction >= med_t:
        cap = equity * (mult / 2.0)
    else:
        cap = equity * 1.0
    return min(cap, equity * mult, bp)
```

**Call site updated at `risk_kernel.py:442`:**
```python
eff_cap  = _effective_exposure_cap(snapshot, idea.conviction, config)
```

**Backward compatibility:** With `config=None` (existing callers), defaults to `mult=3.0` — preserving original hardcoded behavior exactly. Optional parameter is backward-compatible.

**Current impact:** At paper account scale (equity=$102K, BP=$116K), buying power is the binding constraint for all conviction levels — numerical output unchanged. The mismatch fix (threshold 0.75→0.65) is now consistent with `_compute_sizing_basis()`.

**Future impact:** When BP > equity × mult/2.0 (e.g., larger accounts or wider margin ratios), the ceiling and MEDIUM cap will correctly scale with `margin_sizing_multiplier`.

---

## P3 — MEDIUM Sizing Basis Fix

**File:line:** `risk_kernel.py:193`

**Before:**
```python
if margin_ok and conviction >= float(thresholds.get("medium", 0.50)):
    return min(bp, equity * min(mult, 1.5))  # 1.5 HARDCODED
```

**After:**
```python
if margin_ok and conviction >= float(thresholds.get("medium", 0.50)):
    return min(bp, equity * (mult / 2.0))  # proportional: half of HIGH multiplier
```

**Current impact:** None at paper account scale.
- `min(bp=$116K, equity×(4.0/2.0))` = `min($116K, $204K)` = **$116K** (same as old)
- `min(bp=$116K, equity×min(4.0,1.5))` = `min($116K, $153K)` = **$116K**
- Both bind at buying power.

**Future impact:** Activates when BP > equity × 1.5. Example:
- Account equity=$100K, BP=$200K, mult=4.0:
  - Old: `min($200K, $100K×1.5)` = **$150K**
  - New: `min($200K, $100K×2.0)` = **$200K** (+$50K MEDIUM basis)

**Special case:** When mult=3.0, `mult/2.0 = 1.5` exactly — identical to old hardcode. The change only has effect when mult > 3.0.

---

## Sizing Comparison Table (Live Server, 2026-04-27)

Account: equity=$102K, BP=$116K, `margin_sizing_multiplier=4.0`, `high_thresh=0.65`
CORE tier, VIX=18, price=$100, zero existing exposure.

| Conviction | Level | sizing_basis | tier_pct | Position value | % equity | % BP |
|---|---|---|---|---|---|---|
| 0.45 (LOW) | LOW | $102K (equity) | 15% (base) | **$15,300** | 15.0% | 13.2% |
| 0.55 (MEDIUM) | MEDIUM | $116K (BP) | 15% (base) | **$17,400** | 17.1% | 15.0% |
| 0.68 (HIGH-A) | HIGH (fixed) | $116K (BP) | **20% (P1 fix)** | **$23,200** | 22.7% | 20.0% |
| 0.72 (HIGH-B) | HIGH (fixed) | $116K (BP) | **20% (P1 fix)** | **$23,200** | 22.7% | 20.0% |
| 0.80 (HIGH-C) | HIGH (unchanged) | $116K (BP) | 20% | **$23,200** | 22.7% | 20.0% |

**Before P1 fix:** HIGH-A and HIGH-B would have produced $17,400 instead of $23,200.

---

## Items Found But Not Fixed

None. All 4 items were within scope and have been addressed. No unexpected interactions discovered during audit.

---

## Post-Sprint Verification

| Check | Status |
|-------|--------|
| `pytest tests/test_sprint4_conviction_alignment.py` — 27 pass | ✅ |
| Full suite 1548 passing (was 1521, +27 new, 0 regressions) | ✅ |
| `validate_config.py` — 68 checks passed, 0 failures | ✅ |
| Live sizing trace — HIGH-A/B/C all produce $23,200 | ✅ |
| `_float_to_conviction(0.65–0.74)` → `Conviction.HIGH` | ✅ |
| PDT_FLOOR = 26_000.0 unchanged | ✅ |
| All JSON state files intact | ✅ |
| No A2 execution paths touched | ✅ confirmed by audit |
| All 4 changes in `risk_kernel.py` only | ✅ |
