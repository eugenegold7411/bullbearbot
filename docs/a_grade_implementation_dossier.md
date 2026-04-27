# BullBearBot: A-Grade Implementation Dossier
Generated: 2026-04-26
Git HEAD: 931e390 (fix(deploy): sync server git index to origin/main after rsync)
Passes completed: 1 (Architecture), 2 (Runtime Truth), 3 (Shadow Systems), 4 (Learning Loop)

---

## Executive Summary

The bot is healthy at the substrate level (scheduler PID 430128 cycling on time, 1005+ tests passing, weekly review running on schedule producing 107KB structured memos), but it has been operating in **`reconcile_only` mode every cycle since 2026-04-24** because `data/runtime/a1_mode.json` is absent — preflight treats absence as a hard failure. Behind that single-file gap sits a deeper class of issues: the position-cap config field (`max_position_pct_equity`) is annotated as orphaned from the risk kernel, the VIX halt gate silently no-ops on a type error, A2 has 8 `fully_filled` structures with null fill prices and a duplicate XLE submission, and the learning loop has zero forward-return labels written despite 258 decision outcomes captured. The top three immediate gaps are: (1) restore `a1_mode.json` so trading can begin Monday and the divergence enum/ladder works; (2) wire `max_position_pct_equity` into `risk_kernel.size_position()` and fix the VIX dict-vs-float schema; (3) make `decision_outcomes -> backtest_latest -> return_1d/3d/5d` join actually match keys so the supervised learning fields populate. Monday's session will validate three things specifically: whether the `a1_mode.json` restoration unblocks new entries, whether the catalyst taxonomy classifier produces non-`unknown` rates above zero on real cycle data, and whether A2 cycles resume producing `a2_dec_*.json` artifacts (currently the directory does not exist on disk despite mkdir-on-write).

## System Map

**Two parallel pipelines orchestrated by a single 24/7 scheduler.**

- **Account 1 (A1)**: equities + ETFs + crypto. Entry point `bot.py:run_cycle()` (988 LOC). Stages decomposed into `bot_stage0_precycle.py`, `bot_stage1_regime.py`, `bot_stage1_5_qualitative.py`, `bot_stage2_python.py`, `bot_stage2_signal.py`, `bot_stage2_5_scratchpad.py`, `bot_stage3_decision.py`, `bot_stage4_execution.py`. Risk kernel is `risk_kernel.py` — single authority for tier sizing, stop limits, exposure, VIX halt, PDT floor.
- **Account 2 (A2)**: options-only, IV-first, 4-way debate. Entry point `bot_options.py:run_cycle()`. Stages `bot_options_stage0_preflight.py`, `bot_options_stage1_candidates.py`, `bot_options_stage2_5_veto.py`, `bot_options_stage3_debate.py`, `bot_options_stage4_execution.py`. Cycles 90s after A1 to consume fresh signal scores.
- **Scheduler**: `scheduler.py` (2226 LOC). Owns session tiering, cycle intervals (5/15/30 min by tier), trigger queue, PID lock, ORB formation tracker, and 35+ `_maybe_*` scheduled jobs (premarket bars, morning brief, macro wire, weekly review, EOD digest, decision-outcome backfill, daily report, etc.).
- **Single source-of-truth files**: `strategy_config.json` (config), `data/runtime/a1_mode.json` + `a2_mode.json` (operating modes — A1 file currently absent), `data/account2/positions/structures.json` (A2 positions), `data/market/signal_scores.json` (A1->A2 handoff), `data/analytics/cost_attribution_spine.jsonl` (cost telemetry), `data/analytics/decision_outcomes.jsonl` (A1 outcomes). A2 has no equivalent learning artifact in active use.
- **Coupling hotspots**: `bot.py` (29 imports), `weekly_review.py` (27), `scheduler.py` (24), `bot_stage0_precycle.py` (16). `is_claude_trading_window` is duplicated across `scheduler.py` and `bot_stage3_decision.py`.

## Runtime Truth Gaps

Severity-ordered from Pass 2.

1. **CRITICAL — `a1_mode.json` absent → perpetual `reconcile_only`.** No new A1 positions opened since 2026-04-24. Sole preflight blocker.
2. **HIGH — `vix_gate` silently broken.** `_check_vix_halt()` does `float(data.get("vix", 0))` but `macro_snapshot.json` stores VIX as a dict `{"price": 18.97, "chg_pct": -1.76}`. TypeError caught, returned as `passed=True`. VIX halt has never evaluated successfully.
3. **HIGH — 8 of 8 `fully_filled` A2 structures have `filled_price=null`** (NVDA, WMT, XLF also have `order_id=null`). A2 P&L untrackable. close_check_loop is blind to cost basis.
4. **MEDIUM — 4 expired `time_bound_actions` entries for closed positions** (AMZN, XBI, QQQ, MSFT) accumulating in `strategy_config.json`. Reconciler may attempt spurious exits at market open. `reconciliation.seed_backstop()` writes on open but never cleans up on close.
5. **MEDIUM — Duplicate XLE `submitted` structures** for `XLE260508C00057000` from 2026-04-23 17:56 and 18:54 (58 minutes apart, different order_ids). May represent two live calls on Alpaca A2.
6. **MEDIUM — Attribution `_dt` NameError — 99 silent failures in `bot.log`.** Per project memory this was previously identified (Bug D) and fixed; the error continues to appear so either fix did not deploy or a different code path triggers it.
7. **MEDIUM — `divergence.load_account_mode()` enum error — 50 occurrences.** `OperatingMode("NORMAL")` (uppercase string) called against enum where value is `"normal"` (lowercase). Mode ladder load fails on every call.
8. **LOW — Legacy Claude schema warning every overnight cycle (104 occurrences).** Non-functional log pollution from `ask_claude_overnight()` returning legacy dict.
9. **LOW / INFO — `catalyst_type` not in signal_scores.** Schema produces `primary_catalyst` (string), not `catalyst_type` (enum). Field is inferred post-decision in `thesis_checksum.py`.
10. **LOW / INFO — `max_position_pct_equity` declared in config but never read by risk kernel.** Field is explicitly annotated as unused in the config itself. Tier ceilings (core 15% / dynamic 10% / intraday 5%) are the only active size constraint.

## Shadow Systems & Promotion Ladders

| System | State | Records | Promotion Status |
|--------|-------|---------|------------------|
| Portfolio Allocator Shadow | Shadow only, triple-locked enable_live=False | 1129 (5 days) | 14-cycle quantitative threshold met; quality not validated; no live execution path wired |
| Shadow Lane | Permanent advisory | 251 (Apr 15-24) | No promotion defined — informational only |
| Shadow Counterfactual | Wired, 0 verdicts | 0 | Stalled — no eligible (≥5d old + matching outcome) events because reconcile_only blocks new entries |
| Context Compressor Shadow | Disabled | 1 (test record) | No production data; flag toggle bypasses doc-only quality gate |
| Shadow Governance | Disabled | 0 | Advisors initialize on import but credibility never used |
| Replay Fork Debugger | Not implemented | — | `replay_harness.py` does not exist |
| Scratchpad (Stage 2.5) | LIVE (not shadow) | 20 hot rolling | Already in production; ChromaDB cold path BROKEN (protobuf import error) |
| Vector Memory A/B Logger | Sampling at 10% | 6 log lines | Telemetry only — no consumer; ChromaDB break makes `entries_retrieved=0` always |

**Machine-enforced gates:** portfolio_allocator `enable_live=False` (triple lock — default dict + line-79 override + validate_config error); shadow_only preflight verdict blocks order dispatch in `bot.py:952-965` and `bot_options_stage4_execution.py:211,295`.

**Documentation-only gates:** weekly review parameter value ranges (Agent 6 may set any numeric for any whitelisted key); shadow promotion 6-item checklist; counterfactual `advisory=True` flag (returned but read nowhere); context-compressor sample-count gate; signal-weights `n>=30 closed trades` recalibration trigger (string comment, not enforced).

## Learning Loop Health

Per Pass 4 segment scores:

| Segment | Verdict | Why |
|---------|---------|-----|
| Observe | PARTIAL | A1 writes 258 outcomes with full tags but no fill prices. **A2 writes ZERO** — `data/account2/decisions/` directory does not exist on disk despite mkdir-on-write. ChromaDB cold writes broken. |
| Classify | BROKEN | `catalyst_type` field absent from `decision_outcomes.jsonl`. Agent 6 in 2026-04-26 memo: "100% taxonomy failure." |
| Score | BROKEN | 0/258 outcomes have `return_1d/3d/5d` populated. `backfill_forward_returns` runs daily but key match against `backtest_latest.json` (16 results) joins zero records. |
| Summarize | PARTIAL | Weekly review consumes 20 evidence sources, produces 107KB markdown. Consumes ONLY A1 artifacts; A2 entirely absent. ChromaDB stats consumed are zero-from-broken-state. |
| Promote | PARTIAL | Single atomic write, whitelist on keys, BUT no value-range validator and `config_changes` field in director memo always `{}`. |
| Prune | BROKEN | `recommendation_resolver` gated off (`enable_recommendation_memory=false`). Verdicts sit at `pending` indefinitely. No rotation on `decision_outcomes`, `near_miss_log`, `portfolio_allocator_shadow`, or `macro_wire/significant_events`. |

**Top 3 highest-leverage fixes (Pass 4):** (1) repair forward-return backfill linkage so labels populate; (2) wire `decision_id` through `trades.jsonl` AND wire `entry_price` through `ExecutionResult` so cycle log and outcome log are joinable; (3) implement structured config diff in director memo + parameter value-range validator.

## Non-Negotiable Invariants

These red lines are derived from Pass 1 architecture (ownership boundaries) and Pass 2 truth gaps. They MUST hold across every sprint.

1. **`risk_kernel.py` is the SOLE authority for A1 equity position sizing.** No other module — not `order_executor.py`, not `bot_stage3_decision.py`, not `portfolio_allocator.py`, not Stage 4 — may compute or override tier ceilings, exposure caps, stop-distance limits, R/R minimums, VIX halt, PDT floor, or session eligibility. `order_executor` is permitted backstop validation (price-scale sanity, PDT regulatory floor, ORB formation window) but must not redefine tier-size rejections (WARNING only per `policy_ownership_map.md`).
2. **A2 stays bounded and structured — no freeform model output reaches execution.** All A2 candidate selection and structure construction must pass `bot_options_stage1_candidates.py` (universe + IV gates), `bot_options_stage2_5_veto.py` (spread/OI/theta/EV thresholds), and `bot_options_stage3_debate.py` (4-way debate with `debate_confidence_floor=0.65`). Stage 4 dispatches only what the previous stages emit as a typed `OptionsStructure`.
3. **No test fixtures or test artifacts may contaminate production log files.** Pass 2 Step 8 confirms zero contamination across `decision_outcomes.jsonl`, `logs/trades.jsonl`, and `options_log.jsonl`. The test artifact guard (project memory: A1 production fixes Bug B) must remain in place. CI must continue to fail if `pytest`/`mock`/`fixture`/`TEST_` patterns appear in any production artifact path.
4. **Shadow systems remain advisory until machine-enforced promotion criteria are met.** `portfolio_allocator.enable_live` stays `False` — and the triple-lock (default dict + line-79 unconditional override + `validate_config.py` error) stays in place. Promotion to Phase 1 (trim-only) requires (a) a wired execution path, (b) ≥14 consecutive cycles of qualitative TRIM/REPLACE evaluation (not just quantitative cycle count), (c) explicit removal of all three locks in the same commit, (d) a rollback playbook entry. No shadow may be enabled live by config flag alone.
5. **Every operator-facing artifact must either be fresh OR self-identify as stale.** `morning_brief.json` already does this (placeholder injection on weekend staleness). Any new operator-facing artifact (preflight log, weekly review summary, governance probe report, allocator shadow registry) must include a `generated_at` timestamp AND a freshness predicate the consumer checks before display. Silent staleness is forbidden.
6. **Weekly review config changes must be traceable to specific evidence.** Agent 6 may only modify keys already present in `config["parameters"]` (whitelist enforced in `_extract_and_validate_agent6_json`). `_save_strategy_config()` is the SINGLE atomic disk write (Phase 3b only — Phase 1 draft never touches disk). `director_memo_history.json` must record `config_changes` as a structured `{key: (old, new)}` diff — not the current always-empty `{}`. A value-range validator must reject out-of-bounds parameter values BEFORE the disk write.
7. **`a1_mode.json` and `a2_mode.json` are the SOLE source of operating mode.** Only `divergence.save_account_mode()` may write them. Absence is treated as a HARD FAILURE by `preflight._check_operating_mode()` returning `verdict_hint="reconcile_only"` — this is the intended safety behavior. Restoration must explicitly `divergence.save_account_mode("A1", OperatingMode.NORMAL, ...)` rather than touching the file directly.
8. **`structures.json` is the SOLE source of A2 position state.** Only `options_state.save_structures()` may write it. No module may infer A2 positions from `options_log.jsonl` or from Alpaca polling without round-tripping through `options_state`.
9. **Cost attribution spine is append-only.** `cost_attribution.log_claude_call_to_spine()` and `log_spine_record()` are the only writers; rotation at 10,000 lines is the only deletion path. Per-decision ROI computation must read from spine, not recompute from per-call invocation.
10. **No deploy bypasses pre-deploy validation.** `validate_config.py` must run successfully before any rsync to the server. The deploy script (per the recent commit `931e390`) syncs git index to `origin/main` after rsync — this requires that the local working tree match `origin/main` AND that `validate_config.py` passed.

## Grade Gap Analysis

| Category | Evidence | Gap to A | Gap to A+ | Closes With |
|----------|----------|----------|-----------|-------------|
| 1. A1 pipeline correctness | Pass 2 GAP 1: reconcile_only every cycle since Apr 24; Pass 2 GAP 6: Attribution _dt NameError 99x; 258 outcomes captured but 0 entry_price | Restore `a1_mode.json` AND fix Attribution _dt path | Wire entry_price through ExecutionResult so per-decision P&L computable | Restore mode file via `divergence.save_account_mode`; trace Attribution _dt code path that still triggers |
| 2. A2 pipeline correctness | Pass 2 GAP 3: 8/8 fully_filled have null fill_price; GAP 5: duplicate XLE submitted; Pass 4: A2 decisions dir does not exist on disk | Resolve null-fill schema by reading Alpaca fill events into structures.json; add per-symbol submission lock | Wire A2 outcomes into weekly review evidence packet (currently zero A2 visibility) | Update `bot_options_stage4_execution.persist_decision_record` mkdir robustness; add `record_fill_event` callback path; add submission lock keyed on (underlying, occ_symbol, lifecycle=submitted) |
| 3. Risk kernel enforcement | Pass 2 GAP 10: `max_position_pct_equity` orphaned; GAP 2: VIX halt non-functional; weekly memo just reduced margin_sizing_multiplier 3.0->1.0 because position cap is unenforced | Wire `max_position_pct_equity` read into `risk_kernel.size_position()`; fix VIX dict access | Add value-range bounds enforcement on every config-driven kernel parameter at load time | Add single-name cap clause in `risk_kernel`; change VIX read to `data["vix"]["price"]` or rewrite macro_snapshot to scalar |
| 4. Execution reliability | A2 76/124 rejections (61%) are Alpaca 42210000 asset-not-found; 8/8 fully_filled have null fill data; duplicate XLE submission | Resolve options chain mismatch (likely OCC builder vs Alpaca chain freshness); persist Alpaca fill events into structures.json | Pre-submit chain validation against current Alpaca options chain | Add chain-freshness check in `bot_options_stage1_candidates`; add fill-event ingestion in `options_state` |
| 5. Operator surface truthfulness | Pass 2 Step 3: morning_brief stale 59h (weekend, expected); decision_outcomes/divergence/near_miss stale since Apr 24 (reconcile_only); structures.json has no timestamp field | Add `generated_at` to every operator artifact lacking one; add freshness check in display layer | Operator dashboard surfaces freshness-vs-expected per artifact (not just timestamp) | Add timestamp fields in `options_state.save_structures` and `_save_drawdown_state`; add freshness predicates in dashboard/report code |
| 6. Catalyst/outcome taxonomy | Weekly memo: "100% taxonomy failure (257/257 catalyst_unknown)"; `catalyst_type` not in signal_scores schema; thesis_checksum infers post-decision; project memory S16 noted classify_catalyst() fixed 144/144 unknown previously | Wire `classify_catalyst()` output into signal_scores so downstream consumers see typed catalyst | Cross-validate Agent 6 catalyst classification against thesis_checksum and emit divergence events | Add `catalyst_type` field at signal_scores write time using catalyst_normalizer |
| 7. Shadow system maturity | Pass 3: portfolio_allocator 1129 records but 100% are ADD (no TRIM/REPLACE exercised); shadow_counterfactual 0 verdicts; context_compressor 1 test record | Generate market-hours TRIM/REPLACE records by waiting for new entries to resume; require qualitative weekly review eval | Promote portfolio_allocator to trim-only live with all 4 promotion criteria machine-checked | Resume new entries (depends on GAP 1); add explicit promotion-criteria checks in `validate_config.py`; require Agent 6 to write a `shadow_qualitative_eval` block |
| 8. Learning loop closure | Pass 4: 0/258 outcomes have return_1d backfilled; trades.jsonl has no decision_id; recommendation_resolver disabled | Repair forward-return key match between decision_outcomes and backtest_latest; enable recommendation_resolver | A2 learning loop wired in (currently completely open per Pass 4 verdict) | Investigate date format / status filter / symbol mismatch in `decision_outcomes.backfill_forward_returns`; turn `enable_recommendation_memory=true` once labels populate |
| 9. Test coverage quality | Project memory: 1814 tests passing post S17B; mock.patch namespace lesson recorded in feedback memory | Add tests covering preflight `a1_mode.json` absence path AND Attribution _dt path AND VIX dict-vs-float path | Property-based tests for `risk_kernel.size_position` invariants across value ranges | Add unit tests for the three known silent-failure paths; add hypothesis-style invariant tests for kernel |
| 10. Cost attribution quality | Pass 4: 5063 spine records; 23% module_name=unknown; 65% no linked_subject_id | Eliminate `module_name="unknown"` (1177 records); thread decision_id through every Claude call site | Per-decision ROI computation in weekly review (cost / realized return) | Add module_name resolution at every `log_claude_call_to_spine` call site; depends on decision_id propagation (Item 8) |
| 11. Allocator maturity | Pass 3: 1129 shadow records; all overnight ADD because signal_scores empty; weight_deadband 0.02; min_rebalance_notional $500; max_recommendations_per_cycle 3 | Wire trim-only execution path (currently no code reads enable_live=True for action) | Replace logic explicitly validated against weekly outcome data | Add execution dispatch keyed on TRIM action; require qualitative weekly evaluation before flip |

## Implementation Wishlist

### Tier 1 — A-Grade Blockers

**T1-1. Restore `a1_mode.json` to NORMAL via `divergence.save_account_mode()`.** Single most important change. Unblocks the entire A1 trading loop. (Blast: HIGH — directly enables live order routing. Reversibility: easy — delete the file to revert to reconcile_only.)

**T1-2. Wire `max_position_pct_equity` into `risk_kernel.size_position()`.** Currently config-only, annotated as unused. Without this, single-name cap is unenforced; weekly memo just reduced `margin_sizing_multiplier` from 3.0 to 1.0 specifically because position cap is unenforced. (Blast: HIGH — actively constrains sizing. Reversibility: easy — config flip back.)

**T1-3. Fix VIX dict-vs-float in `_check_vix_halt()`.** Schema mismatch: `macro_snapshot.json` stores `{"price": ..., "chg_pct": ...}`, gate does `float(data.get("vix", 0))`. Type error caught silently. (Blast: MEDIUM — VIX currently 18.97; halt would only fire above threshold. Reversibility: easy.)

**T1-4. Fix `divergence.load_account_mode()` enum error.** Some call site passes `"NORMAL"` (uppercase) to `OperatingMode()` constructor where value is `"normal"`. 50 failures in bot.log. Mode ladder cannot function. (Blast: MEDIUM — silent mode-load failure today. Reversibility: easy.)

**T1-5. Fix Attribution `_dt` NameError.** 99 occurrences. Project memory says this was fixed in A1 production fixes Bug D — either fix did not deploy or different code path triggers it. Trace and resolve. (Blast: LOW — already non-fatal. Reversibility: easy.)

**T1-6. Resolve A2 null-fill data in `fully_filled` structures.** 8/8 structures have `filled_price=null` on all legs. Add Alpaca fill-event ingestion path in `options_state`. (Blast: MEDIUM — A2 P&L untrackable until fixed. Reversibility: easy — read-only fix.)

**T1-7. Investigate why `data/account2/decisions/` does not exist on disk.** A2 has not produced a single `a2_dec_*.json` despite mkdir-on-write in `persist_decision_record`. Either A2 cycles have stopped firing or persist is silently failing without the documented warning. (Blast: HIGH for learning loop — A2 entirely unobserved. Reversibility: investigation only initially.)

### Tier 2 — A-Grade Polish

**T2-1. Add per-symbol submission lock in A2 to prevent double-submit.** Duplicate XLE structures from Apr 23 (58 minutes apart) indicate no gating. Lock keyed on `(underlying, occ_symbol, lifecycle=submitted)`. (Blast: MEDIUM. Reversibility: easy.)

**T2-2. Clean up expired `time_bound_actions` on position close in `reconciliation.py`.** 4 stale entries (AMZN, XBI, QQQ, MSFT) accumulating in strategy_config.json; no cleanup path. (Blast: LOW. Reversibility: easy.)

**T2-3. Add structured `config_changes` diff in director memo.** Currently always `{}`. Compute `{key: (old, new)}` for every changed parameter before `_save_strategy_config`. (Blast: LOW — write-only telemetry. Reversibility: easy.)

**T2-4. Add value-range validator in `_extract_and_validate_agent6_json`.** Per-key min/max bounds. Reject out-of-range BEFORE disk write. Pair with structured diff. (Blast: MEDIUM — could reject otherwise-allowed Agent 6 changes. Reversibility: easy — relax bounds.)

**T2-5. Repair forward-return backfill key match.** 0/258 outcomes labeled despite daily backfill running. Likely date format, status filter, or symbol mismatch. (Blast: LOW — backfill is read-only on outcomes file structure. Reversibility: easy.)

**T2-6. Wire `decision_id` through `logs/trades.jsonl` AND wire `entry_price` through `ExecutionResult`.** Two coupled bookkeeping fixes. Schema gap is documented in `decision_outcomes.py:23-25`. (Blast: MEDIUM — schema change, requires migration consideration. Reversibility: medium — old records lack new field.)

**T2-7. Wire `catalyst_type` into signal_scores at write time.** Use existing `catalyst_normalizer.classify_catalyst()`. Project memory S16 noted this fixed 144/144 unknown rate in a previous sprint. (Blast: LOW. Reversibility: easy.)

**T2-8. Add rotation policy on `decision_outcomes.jsonl`, `near_miss_log.jsonl`, `portfolio_allocator_shadow.jsonl`, `macro_wire/significant_events.jsonl`.** Currently unbounded growth. Use the existing `cost_attribution._rotate_jsonl(path, max_lines=10000)` helper. (Blast: LOW. Reversibility: easy.)

### Tier 3 — A+ Differentiators

**T3-1. Wire A2 outcomes into weekly review evidence packet.** Currently `weekly_review.py` consumes zero A2 artifacts. Adding A2 outcomes (options_log.jsonl, structures.json, a2_dec_*.json) makes Agent 6 capable of reasoning about cross-account portfolio behavior. (Blast: HIGH — changes Agent 6 reasoning surface. Reversibility: medium — Agent 6 outputs may diverge from historical baseline.)

**T3-2. Promote portfolio allocator to Phase 1 trim-only live.** Requires (a) wired execution path that currently does not exist in `portfolio_allocator.py` (no call to `execute_all` or `execute_reallocate`), (b) removal of all three `enable_live=False` locks in the same commit, (c) qualitative TRIM/REPLACE evaluation in weekly review (Agent 6 currently does not specifically evaluate). Also requires resumption of new entries to generate non-vacuous shadow records. (Blast: VERY HIGH. Reversibility: hard — once live, rollback requires immediate manual unwind.)

**T3-3. Repair ChromaDB protobuf import.** chromadb 1.5.7 fails on import; cold vector memory has not been written or read since the break. Pin protobuf or upgrade chromadb. Once restored, `vector_memory_ab` logger will record real entries-retrieved instead of zero baseline. (Blast: MEDIUM — silent today; behavior change once fixed. Reversibility: easy via dependency pin.)

**T3-4. Cross-validate catalyst classification with `thesis_checksum` and emit divergence events.** When taxonomy disagrees with checksum, log a `catalyst_divergence` event for forensic_reviewer. (Blast: LOW. Reversibility: easy.)

**T3-5. Per-decision ROI computation in weekly review.** Read cost spine + decision_outcomes + realized P&L; emit ROI per `decision_id`. Depends on T2-6. (Blast: LOW — read-only analytics. Reversibility: easy.)

**T3-6. Replay fork debugger.** Build `replay_harness.py` (does not exist). Allows replaying a captured cycle against modified config to observe counterfactual decision. (Blast: LOW until used in promotion. Reversibility: easy.)

**T3-7. Eliminate `module_name="unknown"` in cost spine.** 1177 records (23%) have no module attribution. Audit every `log_claude_call_to_spine` call site. (Blast: LOW. Reversibility: easy.)

**T3-8. Promotion-criteria machine checks in `validate_config.py`.** For each shadow system, hard-check the documented promotion criteria (≥14 cycles, weekly eval present, code wiring present, rollback playbook entry exists) before allowing the corresponding live flag to flip. (Blast: MEDIUM — gates future promotion attempts. Reversibility: easy — relax check.)

### Safe Quick Wins

- **SQ-1.** Suppress legacy Claude schema WARNING in overnight Haiku call (104 occurrences). Update `ask_claude_overnight()` to return intent format directly.
- **SQ-2.** Add timestamp field to `data/account2/positions/structures.json` and `data/runtime/drawdown_state.json` (both currently NO_TS).
- **SQ-3.** Consolidate `is_claude_trading_window` duplication between `scheduler.py` and `bot_stage3_decision.py` into a shared module.
- **SQ-4.** Wrap `_maybe_reset_session_watchlist` in try/except like the other `_maybe_*` jobs (currently the only unguarded scheduled job — Pass 1 Open Question 8).
- **SQ-5.** Document `data/market/pending_rotation.json` lifecycle (Pass 1 Open Question 15) or remove if dead.
- **SQ-6.** Confirm `structures_pre_migration.json` is inert backup not read by any code path (Pass 1 Open Question 9) and move to `data/archive/migrations/`.

### Do Not Touch Yet

- **DNT-1. portfolio_allocator `enable_live=True`.** 1129 records but 100% ADD; no qualitative TRIM/REPLACE evaluation; no execution path wired. Promotion needs evidence we do not have.
- **DNT-2. Signal weight changes from current values.** Weekly memo explicitly notes recalibration gate is at 30 closed trades; we are at 9. Changing weights now is unsupported by evidence.
- **DNT-3. `enable_recommendation_memory=true`.** Will cause `recommendation_resolver` to fire against unlabeled decisions (0/258 have return_1d) and produce noise. Wait until T2-5 (forward-return backfill) is verified working.
- **DNT-4. `enable_shadow_governance=true`.** No calibration data; advisors initialize but credibility scoring would start from a cold prior with no hindsight evidence. Wait until A2 learning loop and forward-return labels exist.
- **DNT-5. Margin multiplier above 1.0.** Weekly memo just reduced from 3.0 to 1.0 specifically because `max_position_pct_equity` is unenforced. Do not raise until T1-2 lands.
- **DNT-6. `enable_context_compressor_shadow=true`.** Only 1 test record; no production data. Enabling immediately starts Haiku API calls without quality gate.
- **DNT-7. `enable_thesis_*` flags.** Entire `thesis_lab` block is off; no operational evidence of correctness.
- **DNT-8. Modify `risk_kernel` ownership of A1 sizing.** Pass 1 ownership boundary explicit. Any attempt to add sizing logic in `order_executor`, `bot_stage3_decision`, or `portfolio_allocator` violates the architecture.

## Dependency-Aware Sequencing

### Sprint 1 — Pre-trading stabilization (this week)

Goal: Be ready for Monday open with the bot able to open new entries safely. Order matters — each item depends on the previous.

1. **T1-1: Restore `a1_mode.json` via `divergence.save_account_mode("A1", OperatingMode.NORMAL, ...)`.** Verify `preflight.run_preflight()` returns verdict=`go` (not `reconcile_only`).
2. **T1-4: Fix `divergence.load_account_mode()` enum error.** With `a1_mode.json` restored, the loader is exercised on every cycle — broken enum path will surface immediately.
3. **T1-2: Wire `max_position_pct_equity` into `risk_kernel.size_position()`.** Required precondition for raising `margin_sizing_multiplier` back above 1.0 in any future weekly review. Must be merged before T1-1 takes effect on a real trading session, OR margin must remain at 1.0 — confirm one or the other.
4. **T1-3: Fix VIX dict access in `_check_vix_halt()`.** Standalone fix, no dependencies.
5. **T1-5: Trace and fix Attribution `_dt` NameError.** Standalone, but resolution clears 99-occurrence log noise that masks future regressions.
6. **T2-2: Clean expired `time_bound_actions` on close.** Remove the 4 stale AMZN/XBI/QQQ/MSFT entries manually before Monday open AND fix the code path so future closes self-clean.
7. **SQ-4: Wrap `_maybe_reset_session_watchlist` in try/except.** Prevents scheduler-loop crash if watchlist_manager raises during overnight.

### Sprint 2 — Execution semantics + taxonomy (next week)

Goal: A2 produces credible structures and outcomes; catalyst taxonomy stops returning 100% unknown.

1. **T1-6: Add Alpaca fill-event ingestion in `options_state`.** Resolves null `filled_price`/`order_id` on `fully_filled` structures.
2. **T1-7: Investigate `data/account2/decisions/` non-existence.** Determine whether persist is silently failing or A2 cycles have stopped. Restore A2 decision artifact production.
3. **T2-1: Add per-symbol submission lock in A2.** Prevents future XLE-style double-submits.
4. **T2-7: Wire `catalyst_type` into signal_scores via `catalyst_normalizer.classify_catalyst()`.** Eliminates 100% unknown rate in subsequent weekly reviews.
5. **SQ-1: Suppress legacy Claude schema warning.** Cleans 104 log occurrences/day.
6. **SQ-2: Add timestamp fields to `structures.json` and `drawdown_state.json`.**
7. **SQ-3: Consolidate `is_claude_trading_window` duplication.**

### Sprint 3 — Learning loop closure (week 3)

Goal: Forward-return labels populate; per-decision ROI is computable; A2 outcomes feed weekly review.

1. **T2-5: Repair `decision_outcomes.backfill_forward_returns` key match.** Diagnose date format / status filter / symbol mismatch. Verify `return_1d` populates on at least one record, then check coverage.
2. **T2-6: Wire `decision_id` through `logs/trades.jsonl` AND wire `entry_price` through `ExecutionResult`.** Coupled schema changes; do together.
3. **T2-3: Structured `config_changes` diff in director memo.** Standalone; should land before next weekly review.
4. **T2-4: Value-range validator in `_extract_and_validate_agent6_json`.** Pair with T2-3.
5. **T2-8: Rotation on unbounded JSONLs.** Standalone; reuses existing `_rotate_jsonl` helper.
6. **T3-5: Per-decision ROI computation in weekly review.** Depends on T2-6 + T2-5.
7. **T3-3: Repair ChromaDB protobuf import.** Once cold memory is back, vector_memory_ab logger will measure actual entries-retrieved.

### Sprint 4 — Allocator maturity + A+ polish (week 4+)

Goal: Portfolio allocator promoted to trim-only live with full machine-enforced gates; A+ analytics layer in place.

1. **T3-1: Wire A2 outcomes into weekly review.** Cross-account visibility for Agent 6.
2. **T3-8: Promotion-criteria machine checks in `validate_config.py`.** Hard-check criteria before any live flag flip.
3. **T3-7: Eliminate `module_name="unknown"` in cost spine.** Required for full per-module ROI.
4. **T3-4: Catalyst cross-validation with thesis_checksum.** Emit divergence events.
5. **T3-2: Promote portfolio allocator to Phase 1 trim-only live.** Only after qualitative weekly evaluation present, execution path wired, all three locks removed in single commit, rollback playbook entry, and T3-8 gates passing.
6. **T3-6: Replay fork debugger harness.**

## Promotion Checklists

### Portfolio Allocator → Phase 1 (trim-only live)
- [ ] Execution path exists in `portfolio_allocator.py` that calls `order_executor.submit_order()` for TRIM recommendations (currently absent — `run_allocator_shadow()` only writes artifacts and returns dict)
- [ ] All three `enable_live=False` locks (default dict, line-79 unconditional override, validate_config error) removed in a single commit
- [ ] ≥14 consecutive market-hours cycles (NOT overnight) with TRIM or REPLACE recommendations recorded in `portfolio_allocator_shadow.jsonl`
- [ ] Weekly review Agent 6 has explicitly evaluated TRIM recommendation quality in at least one prior memo
- [ ] Rollback playbook entry exists in `docs/shadow_systems_registry.md` with named operator and revert command
- [ ] `validate_config.py` machine-checks all four criteria above and refuses deploy if any fail

### Recommendation Resolver / Hindsight (`enable_recommendation_memory=true`)
- [ ] At least 30 records in `decision_outcomes.jsonl` have `return_1d` populated (currently 0)
- [ ] `recommendation_store.json` exists with at least one entry from prior weekly review
- [ ] `_resolve_single` produces non-`pending` verdict on at least one synthetic test case in CI
- [ ] Director memo `verdict` field populates from resolver path, not from LLM narrative

### Shadow Counterfactual (verdict accumulation → advisory active)
- [ ] At least 50 verdicts in `data/logs/shadow_counterfactual_verdicts.jsonl` (currently 0)
- [ ] Verdict computation runs against ≥5 days of post-event outcome data per record
- [ ] Weekly review reads counterfactual `advisory=True` flag and gates Agent 6 reasoning on it (currently flag returned but read nowhere)

### Context Compressor Shadow (`enable_context_compressor_shadow=true`)
- [ ] At least 50 production-cycle compression samples across multiple market regimes (currently 1 test record)
- [ ] Average compression ratio above documented threshold across all sections, not just `macro_backdrop`
- [ ] Quality eval shows no semantic loss on randomly-sampled compressions

### Shadow Governance (`enable_shadow_governance=true`)
- [ ] `advisor_credibility.json` initialized with at least 4 weeks of advisor track record
- [ ] Calibration scoring validated against backfilled forward-return labels (depends on T2-5)
- [ ] Governance score consumed by at least one production decision path

### Margin Multiplier > 1.0
- [ ] T1-2 merged: `max_position_pct_equity` actively read by `risk_kernel.size_position()` and unit-tested
- [ ] At least 30 closed trades in `memory/performance.json` (currently 2)
- [ ] Weekly review explicitly recommends raising multiplier with cited evidence

## Open Questions

These require market-session evidence and cannot be resolved from this audit's snapshot.

1. **Will Monday's open actually unblock new entries after `a1_mode.json` is restored?** The fix is mechanical but the divergence enum bug (Item T1-4) means even after the file exists, the loader may keep failing. Order matters — restore the file then verify preflight returns `go` AND `divergence.load_account_mode("a1")` returns `OperatingMode.NORMAL` without exception.
2. **Once new entries resume, does the catalyst taxonomy classifier produce non-`unknown` rates?** Project memory S16 says `classify_catalyst()` fixed 144/144 previously; weekly memo today says 257/257 unknown. Either S16 fix regressed or the field is being inferred at the wrong stage.
3. **Why does `data/account2/decisions/` not exist on disk?** Either A2 cycles have stopped firing entirely (last `[OPTS] Cycle done` was 2026-04-25 00:53), or `persist_decision_record` is silently failing without the documented warning. Cannot determine from artifact-only audit.
4. **Why do 16 backtest results not join with any of the 258 decision outcomes in forward-return backfill?** Date format, symbol case, status filter all candidates. Needs runtime inspection of the actual JOIN keys.
5. **Are the two duplicate XLE structures from Apr 23 actually live on Alpaca, or is one cancelled?** Both have `lifecycle=submitted` in our state — but Alpaca state may differ. Reconciliation cycle should reveal.
6. **Is `enable_live=False` triple-lock truly impassable, or does an undocumented code path exist?** Pass 3 says no — but a market-hours cycle with deliberate flag-flip would confirm definitively.
7. **Does the overnight crypto path keep stops active on Alpaca, or does it need to refresh stops after the Haiku decision?** Pass 1 Open Question 12 — needs runtime confirmation.

## Red Lines Summary

(Print at the top of every sprint prompt.)

1. `risk_kernel.py` is the SOLE authority for A1 equity position sizing.
2. A2 stays bounded — no freeform model output reaches execution.
3. No test artifacts in production log files.
4. Shadow systems remain advisory until machine-enforced promotion criteria pass.
5. Every operator artifact must be fresh OR self-identify as stale.
6. Weekly review config changes must be traceable to evidence (whitelist + range validator + structured diff).
7. `a1_mode.json`/`a2_mode.json` are SOLE source of operating mode; only `divergence.save_account_mode()` writes them.
8. `structures.json` is SOLE source of A2 position state; only `options_state.save_structures()` writes it.
9. Cost attribution spine is append-only.
10. No deploy bypasses pre-deploy `validate_config.py`.
