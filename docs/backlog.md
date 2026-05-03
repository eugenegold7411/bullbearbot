---
# BullBearBot — Development Backlog

Last updated: 2026-05-03

---

## IN PROGRESS

| Session | Task |
|---------|------|
| 4 | Vector memory & learning loop diagnosis |

---

## QUEUED (approved, not yet started)

| Task | Notes |
|------|-------|
| Vector memory & learning loop fixes | Waiting for Session 4 diagnosis |
| test_bug009b_tp_fallback.py path isolation | Session 2 in progress |

---

## BACKLOG

### Testing Lane — Dry Run / Wiring Verification Mode
Priority: High — complete before May 16 live promotion
Estimated effort: 2–3 hour build session

A triggerable mode (python wiring_test.py or --dry-run-wiring flag)
that exercises the full A1 and A2 pipeline with synthetic data and
confirms every stage fires and passes data correctly to the next stage.

Scope:
- Synthetic market data injected at bot_stage0_precycle.py (bypasses
  real data fetch)
- Pre-canned morning brief JSON (no Claude call for intel brief)
- Real Claude calls for A1/A2 decision stages with labeled test symbols
  (e.g., TEST_AAPL, TEST_BTC) to confirm prompts are well-formed and
  responses parse correctly
- Fake order submission — intercept order_executor.py before Alpaca
  call, return synthetic fill confirmation, pipeline continues as if
  filled
- Real ChromaDB writes with test decision_id — verify record appears
  in ChromaDB with all fields populated
- Real exit_manager run with synthetic positions — confirm SW-TP check,
  stop logic, position_targets.json write/read all work
- Real macro wire fetch and classify (cheap Haiku call)
- End-to-end trace log: every stage shows input received → output
  produced → passed to next stage → confirmed received
- FULL CLEANUP after test run: all synthetic records removed from
  ChromaDB, position_targets.json restored to pre-test state, all
  temp files deleted, log entries clearly marked as TEST so they
  don't pollute weekly review analysis

Does NOT:
- Submit real orders to Alpaca
- Use real capital or affect position sizing
- Trigger real WhatsApp/email alerts (or route to test number)
- Affect live bot state files permanently

Output: Pass/fail report per stage + full trace log. Any stage that
fails or produces unexpected output flagged with exact error.

---

### Crypto Outside-Market-Hours Entry & Cycles
Priority: High
Estimated effort: 1–2 hour build session
Dependencies: None — infrastructure 90% built

Enable BTC/ETH new position entry and full cycle management during
overnight and extended sessions. See crypto prompt below for full
scope.

---

### Vector Memory & Learning Loop Fixes
Priority: High — blocks recursive improvement
Estimated effort: 1–2 hour build session
Dependencies: Vector diagnosis (Session 4 in progress)

Fix the broken learning loop in priority order:
1. Fix decision_id propagation — currently '' on all submitted orders,
   breaking the decision→outcome linkage
2. Fix catalyst taxonomy write path — 248/248 unknown labels, closed
   trades are analytically inert
3. Fix silent ChromaDB write failures (#10, #18 from silent failure
   audit) — promote to proper alerting
4. Verify retrieval is influencing decisions — add log line showing
   what was retrieved and confirm it appears in Sonnet prompt

Implementation mandate: no step is complete until data is traced
end-to-end through actual live wiring using synthetic test records
with known decision_ids. Full cleanup of test records after
verification.

---

### Remaining Silent Failures (#9–#29)
Priority: High — must fix before May 16 live promotion
Estimated effort: 2–3 hour build session
Dependencies: None

From the silent failure audit, 27 findings remain unaddressed:
- #9: options_state.py atomic write swallowed at call sites (HIGH)
- #10: trade_memory.py save failure returns "" silently (HIGH)
- #11: fill-check loop leaves orders in pending dict forever (HIGH)
- #12: exit_manager TP submission failure misreported as success (HIGH)
- #13: reconciliation cancel-before-replace sequence fails silently (HIGH)
- #14: mode-transition audit log write fails silently (HIGH)
- #15: options leg close continues on exception → naked short risk (HIGH)
- #16: emergency close leg fails silently → naked position (HIGH)
- #17: attribution log write fails silently → P&L audit gap (HIGH)
- #18–#29: MEDIUM and LOW findings
Fix approach: fail-alert pattern (same as divergence.py a6812b0) for
HIGH severity. Promote log level + add WhatsApp alert with 5-min dedup.

---

### Dashboard Safety Panel
Priority: Low — cosmetic until safety alerts fire in production
Estimated effort: 1 hour
Dependencies: Divergence.py fail-alert (done — a6812b0)

Surface safety_system_degraded alerts on dashboard.
- Write to data/runtime/safety_alerts.json on each fail-alert fire
- Dashboard Safety panel: timestamp, function name, error, level
- Clear/acknowledge button
Each write point is already marked with TODO(DASHBOARD) comment in
divergence.py.

---

### Dashboard OVERSIZE Display Bug
Priority: Low — display only, risk_kernel correct
Estimated effort: 15 minutes

Dashboard shows position size as % of buying_power instead of
% of total_capacity. Fix display calculation only.

---

### order_executor.py:354 ValueError
Priority: Low
Estimated effort: 15 minutes

ValueError: unsupported format character ',' in validate_action().
Silent logging error, not blocking execution. Fix the format string.

---

## COMPLETED TODAY (2026-05-03)

| Commit  | What |
|---------|------|
| TBD     | ET_OFFSET → ZoneInfo auto-DST (dashboard/app.py + 6 new tests) |
| 7854b1e | Dashboard A2 redesign + lint + test fix |
| b954bbd | Weekly agent overhaul + Friday 9PM schedule + Agent 7 3-call pipeline |
| 67e789a | BUG-009b + position_targets SW-TP fix — 8 positions now protected |
| a6812b0 | Divergence.py fail-alert on 5 safety functions |
| 84e5b6c | Sizing mismatch fix — Sonnet/kernel aligned at 0.25 |
| 5ab065e | Brief slot persistence — no duplicate brief calls on restart |
| 4f40a8d | Trail stop dead code cleanup — stale test metadata removed |

---

## KNOWN BUGS (active)

| ID | Description | Severity |
|----|-------------|----------|
| BUG-015 | OCO on existing positions requires cancel+resubmit with unprotected window | Low |
| — | order_executor.py:354 ValueError in validate_action() | Low |
| — | Dashboard OVERSIZE display bug (display only) | Low |
| — | test_health_monitor.py OrderStatus.NEW mock failure | Pre-existing |
| — | test_scratchpad_memory / test_sprint2_5 ChromaDB failures | Pre-existing |
