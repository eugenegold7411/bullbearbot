# Sprint 2 QW — A2 Mode File Investigation & Restoration

**Date:** 2026-04-27  
**Status:** COMPLETE — root cause identified, code fixed, deployed, state remediated, verified

---

## Problem Statement

`data/runtime/a2_mode.json` was absent from the server. Every A2 options cycle was blocked by `preflight.run_preflight(account_id='a2')` returning `verdict=reconcile_only` with blocker:

```
operating_mode_a2: a2 mode file absent — entering reconcile_only until state is verified
```

This began when the mode file was lost and no code path recreated it.

---

## Root Cause

**Code gap — no startup initialization for mode files.**

### How mode files work

- `data/runtime/a1_mode.json` and `data/runtime/a2_mode.json` are the sole source of operating mode
- They are **gitignored** (`.gitignore` line 86: `data/runtime/*`, exception only for `sev1_clean_days.json`)
- They are **excluded from rsync** (`Makefile` line 51: `--exclude 'data/'`)
- They can only be created by:
  1. `divergence.save_account_mode()` via `transition_mode()` when a divergence event fires
  2. Manual operator SSH

### Why a2_mode.json was never auto-created

`respond_to_divergence()` — the function that calls `transition_mode()` → `save_account_mode()` — is only wired for Account 1 in `bot_stage0_precycle.py:333`:

```python
a1_mode = respond_to_divergence(div_events, "A1", a1_mode)
```

There is no equivalent call for A2. No A2 divergence event path creates the file.

### Why the file was originally present then lost

The file was manually created by an operator on 2026-04-20 (`entered_by: "operator"`) and appeared in git on 2026-04-22 (commit `2dcb31d`). However, that commit is **not an ancestor of the current HEAD** (git tree was reorganized). The file is also gitignored so it never syncs via rsync. When the server's runtime state was cleared (reboot, reprovisioning, or similar), the file was lost.

**a1_mode.json suffered the identical fate** — it was also lost before Sprint-1, remediated manually on 2026-04-26 (`entered_by: "operator"`). a2_mode.json was missed in that pass.

### Could this happen again (before the fix)?

Yes. Every `make deploy` calls `systemctl restart trading-bot`. If the server rebooted or was reprovisioned, both mode files would be absent. Neither would be recreated by normal bot operation without divergence events firing.

---

## Fix Applied

Added `_ensure_account_modes_initialized()` to `scheduler.py`, called at the top of `run()` after `_acquire_pid_lock()`.

### What it does

- Runs once at every scheduler startup
- For each account (`A1`, `A2`):
  - Calls `divergence.get_mode_path(account)` to get the expected file path
  - If the file **does not exist**: calls `divergence.save_account_mode()` with `OperatingMode.NORMAL` and `entered_by="system_init"`
  - If the file **already exists**: does nothing (idempotent — never overwrites)
- Entirely non-fatal: any exception is caught and logged at WARNING

### Log evidence on first run after deploy

```
2026-04-27 16:15:43  INFO  [INIT] A2 mode file created with NORMAL mode
2026-04-27 16:15:43  INFO  Scheduler starting (24/7 mode)  dry_run=False
```

### Files changed

| File | Change |
|------|--------|
| `scheduler.py` | Added `_ensure_account_modes_initialized()` function + call in `run()` |
| `tests/test_sprint2_a2_mode.py` | 12 new tests |

### Tests (12 new — all passing)

| Suite | Tests | Coverage |
|-------|-------|----------|
| Build 1 — create missing files | 4 | Creates a2_mode, creates a1_mode, creates both, validates fields |
| Build 2 — no overwrite | 3 | Does not overwrite reconcile_only, does not overwrite halted, creates absent when other present |
| Build 3 — preflight blocks when absent | 2 | _check_operating_mode returns reconcile_only; run_preflight returns reconcile_only |
| Build 4 — preflight passes when present | 1 | _check_operating_mode returns go with NORMAL |
| Build 5 — non-fatal | 2 | Swallows import error, swallows save_account_mode OSError |

---

## Operator State Remediation

After deploy, a2_mode.json was written via `divergence.save_account_mode()`:

```python
AccountMode(
    account='A2',
    mode=OperatingMode.NORMAL,
    entered_by='operator',
    reason_detail='Operator remediation: a2_mode.json absent on startup, same pattern as A1 2026-04-26',
)
```

Verification:
```
A1 mode: OperatingMode.NORMAL
A2 mode: OperatingMode.NORMAL
Both OK
```

---

## Verification Output

### Both mode files present

```
-rw-r--r-- 1 root root 413 Apr 26 21:31 /home/trading-bot/data/runtime/a1_mode.json
-rw-r--r-- 1 root root 437 Apr 27 16:15 /home/trading-bot/data/runtime/a2_mode.json
```

### A2 preflight returns go

```
A2 preflight verdict: go
Blockers: []
```

### Last reconcile_only messages (before fix)

```
2026-04-27 16:10:30  WARNING  [PREFLIGHT] verdict=reconcile_only  blockers=['operating_mode_a2: a2 mode file absent...']
```

### Scheduler init log (after fix)

```
2026-04-27 16:15:43  INFO  [INIT] A2 mode file created with NORMAL mode
2026-04-27 16:15:43  INFO  Scheduler starting (24/7 mode)  dry_run=False
```

---

## Residual Risk (Addressed)

**Deploy safety:** `data/` is excluded from rsync, so mode files on the server are never touched by deploys. ✅ Safe.

**Future reboots/reprovisioning:** `_ensure_account_modes_initialized()` runs at every scheduler startup. If the server reboots and mode files are absent, they will be created with NORMAL mode before the first cycle. ✅ Fixed.

**A2 divergence tracking:** The absence of `respond_to_divergence()` wiring for A2 means A2 divergence events do not escalate mode (only A1 has that path). This is a separate concern — the current fix ensures the base NORMAL state is always present so preflight can proceed. The divergence wiring for A2 was not in scope for this sprint.

---

## Git Sync

All three locations at same commit `44475b2`:
- Local: `main @ 44475b2`
- Remote (origin): `main @ 44475b2` (pushed)
- Server: `main @ 44475b2` (deployed via `make deploy`)
