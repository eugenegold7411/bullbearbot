---
# BullBearBot — Development Backlog

Last updated: 2026-05-04

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

---

## BACKLOG

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

## COMPLETED TODAY (2026-05-04)

| Commit  | What |
|---------|------|
| 41f5bdc | Wiring test lane: python wiring_test.py / scheduler --dry-run-wiring; 17/17 PASS |

## COMPLETED PREVIOUSLY (2026-05-03)

| Commit  | What |
|---------|------|
| 7722847 | docs(claude): never include Co-Authored-By in commit messages |
| beceb3c | S20: Fix D — ChromaDB health check + test suite cleanup (health_monitor OrderStatus stub) |
| 787a9dc | S19: silent failure remediation HIGH severity #9–#17 (order_executor ValueError) |
| bfd49c7 | S18: overnight crypto new-entry — BTC/ETH enter_long via Haiku; 10 OE tests |
| d58689f | test(bug009b): patch pathlib.Path in _run_submit_buy — path isolation |
| 9285766 | ET_OFFSET → ZoneInfo auto-DST (dashboard/app.py + 6 new tests) |
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
| — | Dashboard OVERSIZE display bug (display only) | Low |
| — | test_scratchpad_memory / test_sprint2_5 ChromaDB failures | Pre-existing |
