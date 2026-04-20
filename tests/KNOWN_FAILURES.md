# Known Test Failures

Tests that are currently failing and have been triaged. Do not fix without
updating this document.

---

## Pre-existing failures in test_core.py

One failure was present before Prompt 3 and is tracked here for visibility.
It is **not a regression** introduced by the test infrastructure work.

---

### `TestOrderExecutorValidation::test_exposure_cap_low_conviction_exceeded`

**File:** `tests/test_core.py`  
**Error:** `ValueError: unsupported format character ',' (0x2c) at index 64`

**Root cause:** `order_executor.py:369` has a `log.warning()` call with a format
string that uses `%,` (comma as a thousands separator), which is not supported
by Python's `%`-style logging format strings. The format string is:

```
'[EXEC] %s: soft policy check (kernel primary): total exposure $%,.0f ...'
```

Python's `logging` uses `%`-style substitution (`msg % args`), which does not
recognise the `,` flag. This raises `ValueError` when pytest's logging handler
tries to format the record.

**Fix (not applied):** Change `$%,.0f` to `$%.0f` in the `log.warning()` call,
or switch to f-string formatting: `f"... total exposure ${exp:,.0f} ..."`.

**Status:** Tracked, not fixed. The production bot is unaffected because the
VPS runs Python 3.12 and the logging handler never raises on formatting errors
(it calls `handleError()` which silently discards the record).

---

## Environment-dependent failures in test_scratchpad_memory.py

Eight tests in `tests/test_scratchpad_memory.py` fail locally because
**chromadb is not installed in the local development environment**.

```
TestSaveAndStats::test_01_save_returns_id
TestSaveAndStats::test_02_stats_reflect_save
TestSaveAndStats::test_13_stats_increment_on_multiple_saves
TestSaveAndStats::test_14_summary_stored_in_metadata
TestRetrieve::test_03_retrieve_finds_saved_record
TestRetrieve::test_04_metadata_roundtrip
TestHistory::test_08_history_returns_recent_records
TestNearMiss::test_10_near_miss_identified
```

**Root cause:** `trade_memory.py` imports chromadb at startup. When it is absent
the module logs `WARNING: chromadb not installed — vector memory disabled` and
all save/retrieve functions return empty/zero results. The scratchpad tests
assert non-empty ids and non-zero counts, so they fail.

**On the VPS:** chromadb is installed (`pip install chromadb`), so these tests
pass in the production environment.

**Fix (not applied):** Install chromadb in the local venv (`pip install chromadb`)
or mark the scratchpad tests with `@pytest.mark.skipif(not chromadb_available, ...)`.

**Status:** Environment-specific. Do not add chromadb stubs in `conftest.py` —
`trade_memory.py` has its own graceful degradation that must be preserved.
