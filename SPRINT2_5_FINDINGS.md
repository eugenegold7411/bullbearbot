# Sprint 2.5 — Aggressive Trading Config, ChromaDB Env, Cost Spine Taxonomy, OI Gate
## Implementation Findings

**Date:** 2026-04-27
**Baseline tests:** 1491 passing (post Sprint 3)
**Final tests:** 1521 passing (+30 new, 0 regressions)
**Deploy status:** All items deployed; preflight `68 checks passed, 0 failures`

---

## Item 1 — Aggressive Trading Configuration

### dynamic_tier_pct basis — CONFIRMED BUYING-POWER-BASED
**Evidence:** `risk_kernel.py:160–190` `_compute_sizing_basis()`:
```python
if margin_ok and conviction >= float(thresholds.get("high", 0.75)):
    return min(bp, equity * mult)
```
For HIGH conviction, the sizing basis is `min(buying_power, equity × multiplier)`.
With current paper account: `min($116K, $101K × 4.0)` = `min($116K, $404K)` = **$116K** — buying power is the binding constraint.
The `dynamic_tier_pct` of 0.10 is applied to this buying-power-limited basis.

**Sprint 4 item:** No behavior change needed for dynamic_tier_pct basis — it is already buying-power-based.

### margin_sizing_multiplier — CONFIRMED FLAT
**Evidence:** `risk_kernel.py:186–190`:
```python
if margin_ok and conviction >= float(thresholds.get("high", 0.75)):
    return min(bp, equity * mult)           # HIGH: full multiplier
if margin_ok and conviction >= float(thresholds.get("medium", 0.50)):
    return min(bp, equity * min(mult, 1.5)) # MEDIUM: capped at 1.5 regardless of mult
return equity                               # LOW: equity only
```
The multiplier is flat — MEDIUM is always capped at 1.5× equity regardless of the `margin_sizing_multiplier` value. Changing `margin_sizing_multiplier` from 3.0 to 4.0 only affects HIGH conviction sizing.

**Additional finding:** `_effective_exposure_cap()` at `risk_kernel.py:193–211` has HARDCODED thresholds (0.75/0.50) and a HARDCODED 3.0× ceiling — it does NOT read from config. Even with `margin_sizing_multiplier=4.0`, the exposure cap remains 3× equity for HIGH conviction. With current buying_power=$116K, this is already the binding constraint.

**Sprint 4 items:**
1. Implement tiered conviction-based multiplier (1×/2×/4× based on conviction buckets) — `risk_kernel._compute_sizing_basis()`
2. Make `_effective_exposure_cap()` configurable from `margin_sizing_multiplier`

### Practical impact of Sprint 2.5 changes
The most impactful change for this $100K paper account is `max_position_pct_equity` 0.07→0.25:
- Before: HIGH CORE conviction → $23,200 budget → capped down to **$7,083** (7% of equity)
- After: HIGH CORE conviction → $23,200 budget → capped at **$25,250** (25% of equity) → **$23,200 passed through**
- Position size increases ~3.3× for core HIGH conviction trades

### Conviction distribution (pre-change baseline)
From `data/analytics/decision_outcomes.jsonl` (286 records):
- HIGH conviction: 11%
- MEDIUM conviction: 48%
- LOW conviction: 36%

### System prompt changes made
**Change 1 — POSITION SIZING section:**
```
# Before:
HIGH   (>=0.75): min(buying_power, equity x 3.0) — use full margin
...
Hard ceiling: 3.0x equity regardless of buying_power.
[no max_positions line]

# After:
HIGH   (>=0.65): min(buying_power, equity x 4.0) — use full margin
...
Hard ceiling: 4.0x equity regardless of buying_power.
Max concurrent positions: 20 (kernel enforced).
```

**Change 2 — HOLD IS A TRADE section:**
```
# Before:
You will HOLD on 60-70% of cycles. That is correct.

# After:
In paper trading mode, prefer action over inaction. When signals align
and regime is favorable, enter. HOLD only when no clear edge exists — not out of habit or
excessive caution. Target HIGH conviction on 25-35% of actionable cycles.
```

### Config values applied
| Key | Old | New |
|-----|-----|-----|
| `parameters.max_position_pct_equity` | 0.07 | **0.25** |
| `parameters.max_positions` | 14 | **20** |
| `parameters.margin_sizing_multiplier` | 3.0 | **4.0** |
| `parameters.margin_sizing_conviction_thresholds.high` | 0.75 | **0.65** |
| `position_sizing.max_total_exposure_pct` | 0.75 | **0.95** (display only, unused by kernel) |
| `position_sizing.cash_reserve_pct` | 0.15 | **0.05** (display only, unused by kernel) |

### validate_config.py updates
Two range checks updated to accommodate new values:
1. `cash_reserve_pct` valid range: `0.10–0.40` → `0.03–0.40`
2. T-014 gross exposure check: FAIL if `gross > 1.0` → WARN (not FAIL) when `margin_authorized=True` + `gross ≤ 6.0`; FAIL only at `gross > 6.0` extreme. With `max_positions=20 × max_position_pct_equity=0.25 = 500%`, T-014 now emits WARN as expected.

### _PARAM_RANGES update
| Key | Old hi | New hi |
|-----|--------|--------|
| `max_position_pct_equity` | 0.20 | **0.30** |

---

## Item 2 — ChromaDB PROTOCOL_BUFFERS Env Var

**Was it in .env?** No — it was only in the systemd service unit (`Environment=PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python`), not in `.env`.

**Action:** Added `PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python` to `/home/trading-bot/.env`.

**Verification:**
```
chromadb: 1.5.7
trade_memory OK
```
Manual CLI runs now match the service environment. The bot was already healthy — this fixes manual runs only.

---

## Item 3 — Cost Spine Taxonomy

**Root cause confirmed:** `attribution._emit_spine_record()` at `attribution.py:289` falls back to `"unknown"` when `module_tags.get("module")` and `event.get("caller")` are both absent.

**Two call sites fixed in `bot.py`:**

| Call site | Before | After |
|-----------|--------|-------|
| `decision_made` (~line 442) | no `extra` | `extra={"caller": "bot_decision"}` |
| `order_submitted` (~line 780) | `extra={"fill_price":..., "filled_qty":...}` | added `"caller": "bot_order_submitted"` |

**How it works:** `log_attribution_event()` does `record.update(extra)` when `extra` is provided. The `_emit_spine_record(record, extra)` then finds `event.get("caller") == "bot_decision"` (or `"bot_order_submitted"`), eliminating the `"unknown"` fallback.

**Historical entries:** 1,458 pre-fix `"unknown"` entries in cost_attribution_spine.jsonl remain as-is — the spine is append-only. Only new records will have correct module names.

**Expected new unknown rate:** ~0% from these two call sites. Other legitimate unknowns (e.g., direct `log_spine_record()` calls without model info) remain.

---

## Item 4 — OI Gate + CVNA

**Gate change:** `account2.liquidity_gates.min_open_interest`: 150 → **100**

**CVNA gate check at OI=142 (after fix):**
| Gate | Threshold | CVNA OI=142 | Result |
|------|-----------|-------------|--------|
| `pre_debate_oi_floor` | 75 | 142 | ✅ PASS |
| `a2_veto_thresholds.min_open_interest` | 100 | 142 | ✅ PASS |
| `liquidity_gates.min_open_interest` | **100** (was 150) | 142 | ✅ PASS |

**CVNA earnings pipeline:** CVNA is in `earnings_rotation._EXTRA_UNIVERSE` (`earnings_rotation.py:61`). It is auto-promoted to `watchlist_rotation.json` when it has upcoming earnings within the lookforward window. No manual watchlist addition needed.

**Other symbols affected by gate change (OI 100–149 range):** Any symbol with ATM OI between 100 and 149 will now pass the builder OI check. These are generally low-liquidity names — the `a2_veto_thresholds.min_open_interest=100` (veto gate) remains as the pre-builder filter.

**Note:** `options_universe_manager.py:244` still uses `min_open_interest=500` for IV history seeding. CVNA already has IV history (`data/options/iv_history/CVNA_iv_history.json`), so the seeder threshold does not affect CVNA trading.

---

## Files Changed

| File | Items | Change |
|------|-------|--------|
| `strategy_config.json` | 1, 4 | 7 value changes (6 risk params + OI gate) |
| `weekly_review.py` | 1 | `_PARAM_RANGES["max_position_pct_equity"]` hi 0.20→0.30 |
| `validate_config.py` | 1 | cash_reserve range, T-014 margin-aware logic |
| `prompts/system_v1.txt` | 1 | HIGH threshold, multiplier, max_positions, HOLD language |
| `bot.py` | 3 | `caller` key added to 2 attribution call sites |
| `tests/test_sprint2_5.py` | all | 30 new tests (new file) |
| `tests/test_strategy_config_schema.py` | 1 | T-014 test updated for margin-aware behavior |
| `.env` (server-only) | 2 | `PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python` added |

---

## Sprint 4 Prep — Known Items

Based on this sprint's investigation, the following are confirmed Sprint 4 code-change items:

1. **`margin_sizing_multiplier` — implement tiered conviction-based logic** (`risk_kernel.py`)
   - Current: multiplier flat, MEDIUM always capped at 1.5× regardless of config value
   - Target: MEDIUM uses `min(mult * 0.5, 1.5)` or separate `medium_margin_multiplier` config key
   - Evidence: `risk_kernel.py:188` `min(mult, 1.5)` hardcode
   
2. **`_effective_exposure_cap()` — make configurable** (`risk_kernel.py`)
   - Current: `equity * 3.0` hardcoded for HIGH, ignores `margin_sizing_multiplier`
   - Target: read `equity * margin_sizing_multiplier` for HIGH, `equity * 1.5` for MEDIUM
   - Evidence: `risk_kernel.py:205` `cap = equity * 3.0`

3. **Signal weight recalibration** — deferred until `n >= 30` closed trades per config gate

---

## Post-Sprint Verification

| Check | Status |
|-------|--------|
| `pytest tests/test_sprint2_5.py` — 30 pass | ✅ |
| Full suite 1521 passing (was 1491, +30) | ✅ 0 regressions |
| `validate_config.py` — 0 failures, 5 warns | ✅ `68 checks passed` |
| `ruff check` all modified files | ✅ clean |
| All 7 config values correct | ✅ |
| `_PARAM_RANGES["max_position_pct_equity"]` hi=0.30 | ✅ |
| ChromaDB imports with env var | ✅ `chromadb: 1.5.7` |
| Bot attribution callers confirmed in source | ✅ |
| OI gate at 100, CVNA passes all 3 gates | ✅ |
| All JSON state files intact | ✅ |
