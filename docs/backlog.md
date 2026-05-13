---
# BullBearBot — Development Backlog

Last updated: 2026-05-13 (S2 routing bias fixes)

---

## IN PROGRESS

_(none)_

---

## QUEUED (approved, not yet started)

| Task | Notes |
|------|-------|
| Vector memory & learning loop fixes | Waiting for diagnosis session |

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

### Weekly Review — Remaining Structural Findings (from 2026-05-04 review)
Priority: Medium — partially addressed by today's sprint
Estimated effort: 1–2 session sprint

Items still unresolved from Agent 1/2/6 weekly review:
4. **Catalyst taxonomy 248/248 unknown** — outcome attribution completely broken;
   no learning signal from any closed trade. Long-tier ChromaDB at zero.
   (linked to Vector Memory fixes above)
5. **Regime oscillation** — unstable in 21:30–22:30 ET window; cycling normal→caution
   5 times in 40 minutes. May have improved with signal scorer and PI fixes today.
6. **Bearish template lock** — "Iran war, inflation, Fed" recycled verbatim in 7/20
   decisions; macro reasoning not refreshing per cycle. macro_wire fix today
   (63c4b20) may partially address this.
7. **Pending trade accounting problem** — MA/TSM/STNG/AMZN/BTC show zero closed
   trades despite high fill counts; 26.3% win rate may understate actual losses.

RESOLVED in today's sprint:
- Bracket-order deadlock → mleg TIF DAY→GTC (b7c5461), OCO race fix (ab0eb93)
- Missing stop-losses → divergence side-filter + cycle-scoped stop registry (ab0eb93)
- Signal scorer 0W/25L bias → PI base score + technical bias fix (20a2a3f)
- Blocked_symbols guard → already done (c45da26 on 2026-05-04)

---

## COMPLETED TODAY (2026-05-13)

| Commit  | What |
|---------|------|
| a3347bd | feat(S2-routing): NEW-3 remove long_call from all routing; BIAS-2 remove post-event suppression; BIAS-4 iron condor for directional iv>=75; BIAS-5 iron condor for neutral pre-event iv>=60 |
| ec66c8e | fix(S2-routing-tests): update test assertions for NEW-3, BIAS-2, BIAS-4, BIAS-5 regressions (R1-03/04/06, IC14, IC16, test_f, replay harness, fixtures) |

---

## COMPLETED (2026-05-07)

| Commit  | What |
|---------|------|
| a8875b7 | feat(earnings): A1 EARNINGS RULE prompt injection at eda≤3 + straddle direction override for earnings vol plays + calendar sparse-coverage health check |
| 02f5d5f | feat(intelligence): avoid_log.jsonl per cycle + short rule in avoid_line for bearish signals + A2 receives high-IV avoided symbols as credit spread candidates |
| f0cec37 | fix(a2): add earnings_credit_spread_max_size=0.5 default + upgrade RULE_EARNINGS_HIGH_IV log to info with [STRUCT] prefix |
| 4325a96 | fix(dashboard): stop/TP from position_targets.json + missing Chart() paren on allocator+equity canvas + zero-suppress A2 pre-launch equity |
| 63c4b20 | fix(macro_wire): remove standalone tier==critical storage arm — require score>=8.0+tier OR score>=6.0+haiku_confirmed |
| a19c5a4 | chore: commit test_order_hygiene.py (6 order hygiene tests) + backlog update |
| 7c4418b | fix(ci): resolve 9 test_a2_spread_execution failures from alpaca stub contamination |
| e805e7f | fix(a2): debate snapshot stored only for winner — fallback structures get empty debate (eliminates cross-candidate bleed) |
| a2ac4f8 | fix(a2): preflight max-age guard for GTC mleg spreads (30min default) + TTL backstop 15→60min |
| dfbaaec | fix(dashboard): suppress cross-candidate debate bleed on A2 cards + test ordering isolation for trim_stop tests |
| 8cb327f | chore(lint): fix ruff I001+F401 in test_protection_layer.py (unblocks CI) |
| b7c5461 | fix(a2): spread leg intent from existing positions (eliminates 42210000 Alpaca errors) + mleg TIF DAY→GTC |
| ab0eb93 | fix(protection): divergence side-filter stops false duplicate_exit + exit_manager cycle-scoped stop registry prevents TRIM race |
| 20a2a3f | fix(pi): lower base score 8→6 + fix technical bias (both MAs required for +1) |
| 5e6ea15 | fix(allocator): churn guard — skip ADD when open buy order already pending for symbol |

## COMPLETED (2026-05-06)

| Commit  | What |
|---------|------|
| ddcf9da | S-Merge: merged Stage 2 L3 + Stage 2.5 scratchpad into single Haiku call/cycle — `_run_merged_synthesis()` top-40 symbols, held positions bumped to front, L2-only fallback for 41-102, embedded scratchpad extraction in `run_scratchpad_stage()`, fallback chain to batched L3; 11 new tests |
| d9b5c7e | fix: compact output rules in `_MERGED_SYSTEM` — signals max 3, conflicts max 2, catalyst ≤6 words, triggers max 5 total; resolves token overflow (8,400→~5,200 estimated tokens) that caused merged call to fall back on every cycle |

## COMPLETED (2026-05-04)

| Commit  | What |
|---------|------|
| 33a27f6 | test(ci): strategy_config.json skip guards — 17 tests now skip gracefully when file absent on CI clean clone |
| —       | Killed /tmp/alpaca_watcher2.py (PID 42920) — manual debug script from May 1, was polling A2 account with output going nowhere. Removed both v1 and v2 from /tmp. |
| ec7e5f9 | fix(market-data): add missing pandas_ta import — restores RSI and MACD for all signals (crypto + equity); wiring test D-04c hard FAIL on RSI=?; 8 new CS tests; 2996 passing; 24/24 wiring PASS |
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
| — | test_scratchpad_memory / test_bug009b_tp_fallback ordering failures | Pre-existing |
| — | CI: 56 pre-existing test failures (test_short_selling, test_swtp_shorts, test_scratchpad) | Pre-existing |
