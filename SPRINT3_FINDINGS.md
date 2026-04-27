# Sprint 3 — Config Governance, JSONL Rotation, and Deprecation Fixes
## Implementation Findings

**Date:** 2026-04-27
**Baseline tests:** 1469 passing (post Sprint 2 follow-up)
**Final tests:** 1491 passing (+22 new, 0 regressions)
**Deploy status:** All items deployed; preflight `69 checks passed, 0 failures`

---

## Item T2-3 — `_compute_config_diff` wired into Director Memo
**Status:** ✅ IMPLEMENTED

**File:** `weekly_review.py`

`config_changes` in the director memo was always `{}` because it was hardcoded before
the parameter merge block ran. Fixed in two parts:

**New function** `_compute_config_diff(old_params: dict, new_params: dict) -> dict`:
- Compares two parameter dicts
- Returns `{key: {"old": old_val, "new": new_val}}` for every key that differs
- Empty dict if no changes

**Restructured draft Agent 6 block:**
- `params_update = _extract_and_validate_agent6_json(...)` moved to immediately after
  `agent6_output` is assigned (before recommendation verdict updates) so the params
  are available before `_save_director_memo` is called
- `_draft_config_diff: dict = {}` initialized before `if params_update:` block
- Inside `if params_update:`, after `config = _load_strategy_config()`:
  - `_old_params = dict(config.get("parameters", {}))` snapshots pre-merge state
- After the merge loop: `_draft_config_diff = _compute_config_diff(_old_params, config.get("parameters", {}))`
- `_save_director_memo` moved to after the full `if/else` block and now passes
  `"config_changes": _draft_config_diff`

The director memo now records which parameters actually changed each week, with old and
new values. If Agent 6 makes no parameter adjustments, `config_changes` remains `{}`.

**Tests:** `TestComputeConfigDiff` — 6 tests

---

## Item T2-4 — `_PARAM_RANGES` value-range validator
**Status:** ✅ IMPLEMENTED

**File:** `weekly_review.py`

`_extract_and_validate_agent6_json` validated types (numeric coercion) but not ranges.
An Agent 6 hallucination of `stop_loss_pct_core: 0.50` (50%) would have been accepted
and written to `strategy_config.json`.

**New `_PARAM_RANGES` dict** (22 entries) defines `(lo, hi)` inclusive bounds:
```
stop_loss_pct_core:        (0.005, 0.10)
stop_loss_pct_intraday:    (0.005, 0.10)
stop_loss_pct_overnight:   (0.005, 0.10)
take_profit_multiple:      (0.5, 5.0)
vix_threshold_caution:     (15.0, 50.0)
max_position_pct_equity:   (0.01, 0.20)
max_daily_drawdown_pct:    (0.005, 0.15)
max_weekly_drawdown_pct:   (0.01, 0.25)
max_sector_exposure_pct:   (0.05, 0.50)
max_single_name_pct:       (0.01, 0.20)
... (full list in weekly_review.py:_PARAM_RANGES)
```

**Range validation block** added to `_extract_and_validate_agent6_json` after the
type-coercion loop:
- For each key with a `_PARAM_RANGES` entry: checks `lo <= v <= hi`
- Out-of-range values: added to `range_rejected`, deleted from `param_adj`
- Logs `[REVIEW] ... rejected ... (out of range, not merged)` at WARNING

Boundary values (exactly `lo` or exactly `hi`) are accepted — only strictly outside
the range is rejected.

**Tests:** `TestParamRanges` — 8 tests covering in-range accept, out-of-range reject,
below-range reject, boundary accept, and non-numeric pass-through

---

## Item T2-8 — Wire `_rotate_jsonl()` into Unbounded JSONLs
**Status:** ✅ IMPLEMENTED

**Files:** `decision_outcomes.py`, `shadow_lane.py`, `macro_wire.py`

Three JSONL files had no rotation bound and could grow indefinitely:

| File | Write function | JSONL path |
|------|---------------|-----------|
| `decision_outcomes.py` | `log_outcome_event()` | `data/analytics/decision_outcomes.jsonl` |
| `shadow_lane.py` | `log_shadow_event()` | `data/analytics/near_miss_log.jsonl` |
| `macro_wire.py` | `save_significant_events()` | `data/macro_wire/significant_events.jsonl` |

Pattern applied identically in all three:
```python
try:
    from cost_attribution import _rotate_jsonl  # noqa: PLC0415
    _rotate_jsonl(<LOG_PATH>, max_lines=10_000)
except Exception:
    pass
```

- `_rotate_jsonl` imported inline (avoids circular import risk at module load)
- Wrapped in bare `except Exception: pass` — rotation failure is never fatal
- 10,000-line threshold matches `cost_attribution.py`'s own spine rotation
- `macro_wire.py`: rotation fires only when `new_count > 0` (inside `if new_count:` block)

**Tests:** `TestRotateJsonlWired` — 4 tests: rotation called for each file, and
rotation failure does not block `log_outcome_event`

---

## Item O1 — Replace `datetime.utcnow()` with `datetime.now(timezone.utc)`
**Status:** ✅ IMPLEMENTED

**File:** `order_executor.py`

Single occurrence at line 694 in the cancelled-order fill-check path:
```python
# Before:
from datetime import datetime
...
"timestamp":  datetime.utcnow().isoformat(),

# After:
from datetime import datetime, timezone
...
"timestamp":  datetime.now(timezone.utc).isoformat(),
```

`timezone` added to the existing `datetime` import on line 17. No other changes.

**Tests:** `TestUtcnowFixed` — 2 tests: no `utcnow` in source, `timezone` import present

---

## Item O2 — `_max_position_pct_equity_note` in `strategy_config.json`
**Status:** ✅ ALREADY CORRECT (no change needed)

The note for `parameters._max_position_pct_equity_note` was updated in Sprint 1 to:
```
"Enforced by risk_kernel.size_position() as single-name cap upper bound. Updated Sprint 1 2026-04-26."
```

The spec described this as "currently says unused" — that was the pre-Sprint-1 state.
Confirmed correct on server via grep; no change made.

**Tests:** `TestStrategyConfigNote` — 2 tests verify the note does not say "unused"
and references enforcement

---

## Files Changed

| File | Items | Change |
|------|-------|--------|
| `weekly_review.py` | T2-3, T2-4 | `_PARAM_RANGES`, `_compute_config_diff`, range validation, memo restructure (+50 lines) |
| `decision_outcomes.py` | T2-8 | `_rotate_jsonl` call in `log_outcome_event` (+5 lines) |
| `shadow_lane.py` | T2-8 | `_rotate_jsonl` call in `log_shadow_event` (+5 lines) |
| `macro_wire.py` | T2-8 | `_rotate_jsonl` call in `save_significant_events` (+5 lines) |
| `order_executor.py` | O1 | `datetime.utcnow()` → `datetime.now(timezone.utc)` (+1 line) |
| `tests/test_sprint3.py` | all | 22 new tests (new file) |

---

## Architectural Notes

**No red lines crossed:**
- `risk_kernel.py` untouched — all sizing authority intact
- `strategy_config.json` untouched — no parameter values changed, only validation logic added
- `a1_mode.json`/`a2_mode.json` untouched
- `structures.json` untouched
- No bypasses of `validate_config.py` — preflight confirmed `0 failures`

**T2-3 restructure is safe:**
The order of operations in `run_review()` is unchanged for the external caller:
- Agent 6 is still called first
- Recommendation verdict updates still run before config merge
- Config merge still happens before Phase 2
- Config is still NOT written to disk at draft stage (Phase 3b remains sole write authority)
The only change is that `_save_director_memo` now runs after the config merge block
(instead of before), which is necessary to have the diff available.

**`_rotate_jsonl` inline import pattern:**
`cost_attribution.py` is a low-level module with no imports from the calling modules.
Inline import (`from cost_attribution import _rotate_jsonl`) inside the write function
avoids any circular-import risk at module load time. The function is prefixed with `_`
(internal) but has no `__all__` restriction.

---

## Post-Sprint Verification

| Check | Status |
|-------|--------|
| `pytest tests/test_sprint3.py` — 22 pass | ✅ |
| Full suite 1491 passing (was 1469, +22) | ✅ 0 regressions |
| `validate_config.py` — 0 failures | ✅ `69 checks passed` |
| `ruff check` all modified files | ✅ clean |
| Bot service running | ✅ active (running) |
| `data/runtime/a1_mode.json` intact | ✅ |
| `data/runtime/a2_mode.json` intact | ✅ |
| `data/account2/positions/structures.json` intact | ✅ |
| `strategy_config.json` intact (no parameter changes) | ✅ |
