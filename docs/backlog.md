---
# BullBearBot — Development Backlog

Last updated: 2026-05-04 (Agent 6 config write guards — 408e7f6)

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

### Weekly Review — Critical Structural Fixes (from 2026-05-04 review)
Priority: CRITICAL — win rate 26.3%, stock_buy 13.2%, Tech sector 0-for-24
Estimated effort: 2–4 session sprint

Findings from Agent 1 (Quant), Agent 2 (Risk), Agent 6 (Strategy Director):
1. **Bracket-order deadlock** (8 symbols can't exit at take-profit) — held_for_orders
   == existing_qty across CAT, GOOGL, MSFT, etc. Recurring 3-week carry. This is
   exit infrastructure failure, not a market regime problem.
2. **414 missing stop-losses** — divergence module KeyError leaves 89% of divergence
   events unpatched. Stock_buy entering without exit protection.
3. **Signal scorer 0W/25L on QCOM+MSFT** — top-ranked symbols 0% win rate. Either
   inputs are stale or scoring function has confirmation bias.
4. **Catalyst taxonomy 248/248 unknown** — outcome attribution completely broken;
   no learning signal from any closed trade. Long-tier ChromaDB at zero.
5. **Regime oscillation** — unstable in 21:30–22:30 ET window; cycling normal→caution
   5 times in 40 minutes. Suppressing trade generation with no external trigger.
6. **Bearish template lock** — "Iran war, inflation, Fed" recycled verbatim in 7/20
   decisions; macro reasoning not refreshing per cycle.
7. **Pending trade accounting problem** — MA/TSM/STNG/AMZN/BTC show zero closed
   trades despite high fill counts; 26.3% win rate may understate actual losses.

Agent 6 parameter changes already applied to strategy_config.json on VPS:
- min_confidence_threshold: medium (was unknown)
- stop_loss_pct_core: 0.03
- take_profit_multiple: 2.5
- max_weekly_drawdown_pct: 0.035, max_daily_drawdown_pct: 0.025
- max_positions: 30, margin_sizing_multiplier: 4.0

RESOLVED: blocked_symbols guard added (S24 c45da26). QCOM restored to server config.
_merge_blocked_symbols() now enforces append-only semantics at both write paths.

---

### Agent 6 Config Write Guards
COMPLETED — commit 408e7f6

---

---

## COMPLETED TODAY (2026-05-04)

| Commit  | What |
|---------|------|
| 408e7f6 | feat(weekly-review): Agent 6 config write guards A–I — _PARAM_READONLY frozenset (6 booleans + 4 arch fields); 9 unguarded numerics + max_day_trades added to _NUMERIC_PARAM_FIELDS/_PARAM_RANGES; nested-dict/list/enum guards in extractor; _validate_signal_source_weights(); active_strategy + director_notes.priority enum guards at both Phase 1 + Phase 3b write sites; 15 new tests; 3048 passing |
| b00b0df | fix(test): ET timezone in test_br05_old_files_pruned — fixes Ubuntu CI failure (naive datetime.now() produced UTC date, off by 1 day vs ET pruning cutoff) |
| ab75de3 | S25: MEDIUM/LOW silent failure remediation #18–#29 — _fire_safety_alert() in 5 modules; 9 MEDIUM upgrades (log.error + WhatsApp); 2 LOW upgrades (log.error only); bars_save was debug level; 29 new tests; 3033 passing |
| c45da26 | S24: blocked_symbols append-only guard in weekly_review.py — _merge_blocked_symbols() helper; Phase 1 + Phase 3b guards; 6 new tests; QCOM restored to server config |
| 09a9592 | S23: Wiring test schema validation — 23-check suite (17→23); D-04b/D-05b/D-06b/D-09b/D-11/E-07b; WARN status; 32 unit tests |
| 41f5bdc | S22: Wiring test lane: python wiring_test.py / scheduler --dry-run-wiring; 17/17 PASS |
| e966edd | fix(lint): remove unused imports in test_signal_quality_fixes.py — unblocks CI lint step |

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
