# Sprint 2 — Execution Semantics, Taxonomy, and Defensive Guards
## Implementation Findings

**Date:** 2026-04-27
**Baseline tests:** 1428 passing (post Sprint 2 QW)
**Final tests:** 1449 passing (+21 new, 0 regressions)
**Deploy status:** All items deployed; preflight `verdict=go, blockers=[]`

---

## Item 1 — DTBP Pre-Flight Guard in A2 Executor
**Status:** ✅ IMPLEMENTED

**File:** `order_executor_options.py` (lines 119–141)

Alpaca paper accounts intermittently return `daytrading_buying_power=0` while
`options_buying_power > 0`. Submissions in this state fail with error 40310000.

Guard added between client creation and `submit_structure()` call:
- Fetches account state via `client.get_account()`
- If `dtbp == 0` and `obp > 0`: logs `[EXECUTOR] DTBP_ZERO` warning with account ID
  and actionable message ("Contact Alpaca support to reset"), returns status=`dtbp_zero`
- If account fetch raises (e.g., network error): logs at DEBUG and falls open (submission proceeds)
- The guard is non-fatal in all edge cases

**Tests:** `test_dtbp_zero_guard_skips_submission`, `test_dtbp_nonzero_proceeds_normally`,
`test_dtbp_check_failure_fails_open`

**Verification pending:** Requires a cycle where Alpaca paper account is in the broken state
to see `[EXECUTOR] DTBP_ZERO` in logs. Not triggered during normal operation (correct behavior).

---

## Item 2 — A2 Decisions Directory Startup Initialization
**Status:** ✅ IMPLEMENTED (pre-condition already satisfied)

**File:** `scheduler.py` — `_ensure_account_modes_initialized()`

**Finding:** The `data/account2/decisions/` directory was already present on the VPS
with 44 decision files. `persist_decision_record()` in `bot_options_stage4_execution.py`
already uses `mkdir(parents=True, exist_ok=True)` on first write. The directory was
created automatically during the first successful A2 cycle after Sprint 2 QW restored
`a2_mode.json`.

**Change made:** Added belt-and-suspenders initialization at the end of
`_ensure_account_modes_initialized()` — creates the directory on startup if absent.
This prevents the edge case where the bot restarts on a fresh provision or after the
`data/account2/decisions/` directory is manually deleted.

**Tests:** `test_decisions_directory_created_at_startup`, `test_persist_decision_record_creates_directory_if_absent`,
`test_persist_decision_record_writes_json_file`

---

## Item 3 — Per-Symbol Submission Lock (Duplicate Guard)
**Status:** ✅ IMPLEMENTED

**File:** `bot_options_stage4_execution.py` — `_is_duplicate_submission()` helper + wiring

New helper `_is_duplicate_submission(symbol, legs)` checks `options_state.load_structures()`
for any structure in `{submitted, partially_filled, fully_filled}` state with matching
underlying + overlapping OCC symbols. If a match is found:
- Logs `[OPTS] DUPLICATE_SUBMIT blocked` with symbol, overlapping OCC set, structure_id, lifecycle
- Returns `True` (caller skips submission and sets `execution_result="no_trade"`,
  `no_trade_reason="duplicate_submission_blocked"`)

Wired in the bounded execution path in `run_stage4_execution()`, after `build_structure()`
succeeds and before `save_structure(PROPOSED)`.

The check is non-fatal: if `load_structures()` raises, the helper logs at DEBUG and returns
`False` (fail open — prefer a duplicate submission over a missed trade).

**Tests:** `test_duplicate_submission_blocked`, `test_different_symbol_not_blocked`,
`test_expired_structure_not_blocking`

---

## Item 4 — Wire `catalyst_type` into Signal Scores
**Status:** ✅ IMPLEMENTED

**Files:** `bot_stage2_signal.py` — `_run_l3_synthesis()` and `_l2_to_signal_score()`

`classify_catalyst()` from `semantic_labels.py` is called on each symbol's
`primary_catalyst` text in the L3 synthesis hot path. The `.value` (string form of
`CatalystType` enum) is written to `row["catalyst_type"]`.

Both fallback return paths in `_l2_to_signal_score()` also include `"catalyst_type": "unknown"`
so downstream consumers can always rely on the field being present.

On exception from `classify_catalyst()`, falls back to `"unknown"` (non-fatal).

**Tests:** `test_signal_scores_include_catalyst_type`, `test_catalyst_type_not_unknown_when_classifiable`,
`test_catalyst_type_failure_does_not_break_scoring`, `test_catalyst_type_in_l3_synthesis_path`

**Verification pending:** `data/market/signal_scores.json` was checked at 4:53 PM ET
(market closed, no scored symbols). Verify Monday morning that `catalyst_type` field appears
with non-`unknown` values for symbols with classifiable catalysts.

---

## Item 5 — Wire `remove_backstop()` into Reconciliation Close
**Status:** ✅ IMPLEMENTED

**File:** `reconciliation.py` — `_close_position()` + new `_CONFIG_PATH` constant

`_close_position()` is the single clean hook in the reconciliation path. After the position
is closed (order submitted, result appended), `remove_backstop(symbol, _CONFIG_PATH)` is
called to prune stale `time_bound_actions` entries.

`_CONFIG_PATH = Path(__file__).parent / "strategy_config.json"` added as module-level
constant (same pattern as `seed_backstop()`'s existing path handling).

The call is wrapped in `try/except` — any failure is logged at DEBUG and does not block
the close or the reconciliation result.

**Scope note:** This wires the backstop removal only for reconciliation-driven closes.
Exits via `exit_manager` (trail stop fires, stop hit) and `order_executor` direct closes
do not yet call `remove_backstop()`. Those paths have independent ownership and are out of
scope for this sprint.

**Tests:** `test_remove_backstop_called_on_position_close`,
`test_remove_backstop_failure_does_not_block_close`

---

## Item 6 — Fill-Event Ingestion for A2 Structures
**Status:** ✅ IMPLEMENTED

**File:** `bot_options_stage4_execution.py` — `_update_fill_prices()` helper + wiring

New helper `_update_fill_prices(structures, trading_client)`:
- Iterates all structures in `{submitted, partially_filled, fully_filled}` state
- For each leg with `order_id` set but `filled_price is None`: fetches `filled_avg_price`
  and `filled_qty` from Alpaca via `get_order_by_id()`
- Updates `leg.filled_price` (and `leg.filled_qty` if available) in place
- Persists via `options_state.save_structure()` if any leg was updated
- Logs `[FILL]` at INFO for each updated leg
- All Alpaca and save failures are non-fatal (logged at DEBUG)

Wired at the start of `close_check_loop()`: `load_structures()` → `_update_fill_prices()` →
then the existing `get_open_structures()` + close-check loop runs.

**Background:** At time of implementation, 8 fully_filled structures had null `filled_price`
on all legs. `close_structure()` gates on `filled_price is not None` for P&L calculation.
Without fill data, realized P&L was unknown for all closed positions.

**Tests:** `test_fill_prices_updated_from_alpaca`, `test_fill_update_skips_structures_without_order_id`,
`test_fill_update_failure_is_non_fatal`

---

## Item 7 — Remove Dead `_TRADING_WINDOW_*` Constants
**Status:** ✅ IMPLEMENTED

**File:** `scheduler.py`

Removed two dead constants that were defined but never referenced anywhere in the codebase:
```python
_TRADING_WINDOW_START = 9 * 60 + 25    # 9:25 AM ET
_TRADING_WINDOW_END   = 16 * 60 + 15   # 4:15 PM ET
```

Confirmed via grep that no file in the project imported or referenced these names.
The scheduler's actual trading window logic uses session tier comparisons in `_get_session_tier()`,
not these constants.

**Tests:** `test_trading_window_constants_removed`

---

## Item 8 — Fix Overnight Log Field Names
**Status:** ✅ IMPLEMENTED

**File:** `bot_stage3_decision.py` — `_ask_claude_overnight()` (lines 635–636)

Fixed stale field names in the overnight decision log call:
```python
# Before:
log.info("[OVERNIGHT] Haiku decision: regime=%s  actions=%d",
         result.get("regime", "?"), len(result.get("actions", [])))

# After:
log.info("[OVERNIGHT] Haiku decision: regime=%s  ideas=%d",
         result.get("regime_view", "?"), len(result.get("ideas", [])))
```

`ClaudeDecision` schema uses `regime_view` (not `regime`) and `ideas` (not `actions`) since
the intent-based schema migration. The overnight log was silently showing `regime=?` and
`ideas=0` for every overnight Haiku cycle.

**Tests:** `test_overnight_log_uses_regime_view_not_regime`,
`test_overnight_log_counts_ideas_not_actions`

---

## Files Changed

| File | Items | Change |
|------|-------|--------|
| `order_executor_options.py` | 1 | DTBP pre-flight guard (+28 lines) |
| `scheduler.py` | 2, 7 | Decisions dir init + remove dead constants |
| `bot_options_stage4_execution.py` | 3, 6 | Duplicate lock + fill ingestion (+97 lines) |
| `bot_stage2_signal.py` | 4 | catalyst_type wired (+10 lines) |
| `reconciliation.py` | 5 | remove_backstop wiring + _CONFIG_PATH (+8 lines) |
| `bot_stage3_decision.py` | 8 | Fix overnight field names (4 lines changed) |
| `tests/test_sprint2_items.py` | all | 21 new tests (new file) |

---

## Architectural Notes

**No red lines crossed:**
- `risk_kernel.py` untouched — all sizing authority intact
- `structures.json` sole A2 state source — `_is_duplicate_submission()` reads via `options_state.load_structures()` only
- `a1_mode.json`/`a2_mode.json` sole operating mode source — unchanged
- Cost attribution spine append-only — no changes to `cost_attribution.py`
- No bypasses of `validate_config.py` — preflight confirmed `verdict=go`

**Scope boundaries respected:**
- Item 5 wired only to reconciliation close path (explicit scope per sprint spec)
- Item 6 fill ingestion reads from Alpaca, never writes directly to `structures.json` (uses `options_state.save_structure()`)
- No changes outside the 8 items

---

## Post-Sprint Verification Checklist

| Check | Status |
|-------|--------|
| `pytest tests/test_sprint2_items.py` — 21 pass | ✅ 1449 total, 0 regressions |
| `validate_config.py` preflight | ✅ `verdict=go, blockers=[]` |
| `data/runtime/a1_mode.json` intact | ✅ |
| `data/runtime/a2_mode.json` intact | ✅ |
| `data/account2/positions/structures.json` intact | ✅ |
| `data/market/signal_scores.json` intact | ✅ |
| `strategy_config.json` intact | ✅ |
| Bot service running | ✅ active (running) |
| catalyst_type in live signal_scores | ⏳ Verify Monday morning |
| DTBP_ZERO log trigger | ⏳ Requires Alpaca paper account broken state |
