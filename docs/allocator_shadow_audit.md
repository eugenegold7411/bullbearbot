# Allocator Shadow Audit — 2026-04-28 UTC

---

## 0. Executive Summary

The BullBearBot A1 allocator shadow system (`portfolio_allocator.py`, Sprint S6-ALLOCATOR) is **wired but not producing artifacts**. The shadow JSONL file (`data/analytics/portfolio_allocator_shadow.jsonl`) has **zero records** — the file does not exist on the local mirror. The `shadow_status_latest.json` registry file likewise does not exist. The module is imported and called every cycle via `bot_stage0_precycle.py`, config shows `enable_shadow: true`, and code paths are non-fatal, so the bot continues running. The absence of artifacts is most likely caused by a persistent runtime exception in `run_allocator_shadow()` that is silently swallowed.

The allocator design is substantially well-thought-out. Control law is legible (TRIM/ADD/REPLACE thresholds are explicit integers), anti-churn friction is multi-layered, and the module correctly hardwires `enable_live = False`. However, **zero runtime evidence exists** to validate any behavioral claim. Promotion cannot be assessed because baseline data is absent.

A second, structurally separate execution path exists: `portfolio_intelligence.execute_reallocate()` is called from `order_executor.execute_all()` when `act == "reallocate"`. This is **DEAD CODE in production** (the executor calls it with the wrong signature — `execute_reallocate(action, _get_alpaca())` — whereas `portfolio_intelligence.execute_reallocate()` takes `(exit_symbol: str, entry_action: dict, alpaca_client)`). The `realloc_log.jsonl` (112 records, mix of submitted/failed/skipped) appears to be **test artifact data**, not production executions, as all records reference `equity=100000.0`, a stub value.

**Bottom line:** The shadow system is NOT READY for promotion. It is not producing observable runtime artifacts, contains a signature mismatch bug in the live REALLOCATE path, and has no vetted cycle history to inspect.

---

## 1. Current Allocator System Map

### Component Table

| Component | Owner file | Purpose | Inputs | Outputs | Live/Shadow/Doc-only |
|-----------|-----------|---------|--------|---------|----------------------|
| Portfolio Allocator Shadow | `portfolio_allocator.py` | Ranks incumbents vs candidates; produces HOLD/TRIM/ADD/REPLACE recs | `pi_data`, `positions`, `cfg`, `session_tier`, `equity` | `data/analytics/portfolio_allocator_shadow.jsonl`, shadow registry update, `format_allocator_section()` string for Stage 3 prompt | **Shadow** (enable_live hardwired False) |
| Portfolio Intelligence | `portfolio_intelligence.py` | Computes dynamic sizes, position health, correlation matrix, thesis scores | `equity`, `positions`, `config`, `open_decisions`, `position_entry_dates`, `buying_power` | `pi_data` dict: sizes/health_map/forced_exits/deadline_exits/correlation/thesis_scores | **Live** (read-only analytics) |
| execute_reallocate() | `portfolio_intelligence.py` (lines 906–988) | Atomic exit+entry capital reallocation | `exit_symbol: str`, `entry_action: dict`, `alpaca_client` | Alpaca market orders; status dict | **DEAD CODE** — present but not correctly callable |
| Risk Kernel REALLOCATE | `risk_kernel.py` (lines 816–900) | Sizes the REALLOCATE entry side, returns BrokerAction | `idea` (TradeIdea with exit_symbol+entry_symbol), `snapshot`, `config`, `current_price`, `session_tier`, `vix` | `BrokerAction(action=REALLOCATE)` or rejection str | **Live** — handles kernel validation but executor call is broken |
| Executor REALLOCATE handler | `order_executor.py` (lines 801–827) | Dispatches reallocate action | `action dict` | Calls `execute_reallocate(action, alpaca_client)` — wrong signature | **Live** — wired but broken: passes `(dict, client)` instead of `(str, dict, client)` |
| Stage 3 Allocator Injection | `bot_stage3_decision.py` (lines 351–355) | Appends `format_allocator_section()` output after full prompt render | `allocator_output` from PreCycleState | Advisory text block appended to Stage 3 prompt | **Live** (prompt advisory only) |
| validate_config.py gate | `validate_config.py` (lines 759–803) | Checks portfolio_allocator section completeness | `strategy_config.json` | PASS/FAIL gate | **Live** (config validation only) |

**Is there one allocator or multiple fragmented?** — [CODE] Two conceptually separate systems share the "allocator" concept: (1) `portfolio_allocator.py`, a pure shadow recommender with explicit HOLD/TRIM/ADD/REPLACE decision rules, and (2) a `execute_reallocate()` in `portfolio_intelligence.py` plus a REALLOCATE handler in `risk_kernel.py` + `order_executor.py`. These are NOT integrated — the shadow allocator never calls execute_reallocate(), and the risk_kernel REALLOCATE handler is only reachable if Claude emits an idea with `exit_symbol + entry_symbol` fields, which the current prompt JSON schema does support.

**Additive-only or trim/replace too?** — [CODE] TRIM, ADD, and REPLACE are all defined in decision logic. TRIM is the most mature (graduated severity table exists in config). ADD requires available headroom. REPLACE requires a 15-point score gap plus multi-factor friction. FREE CASH is not a named action — cash freeing is a side effect of TRIM/REPLACE.

**Single source of truth?** — [CODE] No. `portfolio_allocator.py` maintains its own scoring thresholds (`trim_score_threshold=4`, `replace_score_gap=15`). `portfolio_intelligence.py:format_thesis_ranking_section()` provides independent TRIM guidance to Claude ("4–5/10: TRIM 25%", "2–3/10: TRIM 50%"). `prompts/system_v1.txt` contains a third independent "THESIS SCORE ACTION GUIDE" with the same 4–5/10 threshold. These three are not synchronized through a single config key.

---

## 2. Current Runtime / Shadow Evidence

### Observed Field Table

| Observed field | Present in code? | Present in runtime? | Fill quality | Notes |
|----------------|-----------------|---------------------|--------------|-------|
| `schema_version` | Yes (=1) | Unknown — no artifacts | N/A | Written to JSONL header |
| `timestamp` | Yes (UTC ISO) | Unknown | N/A | |
| `session_tier` | Yes | Unknown | N/A | Passed from bot_stage0_precycle |
| `ranked_incumbents` | Yes | Unknown | N/A | Built from pi_data thesis_scores |
| `ranked_candidates` | Yes | Unknown | N/A | Read from signal_scores.json |
| `proposed_actions` | Yes | Unknown | N/A | Contains HOLD/TRIM/ADD/REPLACE |
| `suppressed_actions` | Yes | Unknown | N/A | With suppression_reason |
| `config_snapshot` | Yes | Unknown | N/A | 5 config values at write time |
| `summary` | Yes | Unknown | N/A | n_hold/n_trim/n_add/n_replace/n_suppressed |
| `shadow_status_latest.json` | Yes | **NOT FOUND** | Poor | Registry not updated |

### Summary

**Record count:** [RUNTIME] **0 records** — `data/analytics/portfolio_allocator_shadow.jsonl` does not exist on the local mirror. The server may have artifacts, but the local mirror (source of truth per CLAUDE.md) shows zero.

**Shadow_status_latest.json:** [RUNTIME] **NOT FOUND** — Registry file `data/reports/shadow_status_latest.json` does not exist.

**Action distribution:** [RUNTIME] Cannot determine — no records.

**Suppressed distribution:** [RUNTIME] Cannot determine — no records.

**Quality assessment:** [UNKNOWN] The module is wired, non-fatal, and config-enabled. The most likely explanations for zero artifacts are: (1) `_load_candidates()` fails silently because `data/market/signal_scores.json` is stale/absent on the local mirror, causing `run_allocator_shadow()` to still succeed but produce empty candidates; or (2) a Python exception inside the try/except block causes `run_allocator_shadow()` to return None and suppress the write entirely. The `data/analytics/` directory exists (confirmed: it has attribution_log, cost_spine, etc.), so the parent directory is not the blocker.

**Note on realloc_log.jsonl:** [RUNTIME] The file `data/analytics/realloc_log.jsonl` has 112 records (52 submitted, 32 failed, 28 skipped), all with `equity=100000.0` and test-style patterns (`order_id: "ord-ok"`, `reason: "disk full"`, `reason: "broker down"`). This is **test fixture data**, not production execution logs. No source file in the current codebase writes to this path — it appears to be a legacy test artifact. It does not reflect actual TRIM executions.

---

## 3. Control Law Analysis

### Signal/Input Table

| Signal/input | Used in code? | Used in shadow output? | Quality | Comments |
|-------------|--------------|----------------------|---------|----------|
| `thesis_score` (1–10) from PI | Yes — primary incumbent ranking signal | Yes (in ranked_incumbents) | [CODE] Derived from heuristics: catalyst age, MA20/EMA9, P&L momentum, sector alignment, time decay, time-bound overrides | No Claude call; pure Python heuristics |
| `signal_score` (0–100) from signal_scores.json | Yes — primary candidate ranking signal | Yes (in candidate_snapshot) | [CODE] Written by Stage 2 Haiku scorer | Stale if bot not running |
| `market_value` | Yes — notional floor check, target weight computation | Yes | [CODE] From Alpaca position live feed | |
| `account_pct` | Yes — weight deadband check | Yes | [CODE] Computed from market_value / equity | |
| `correlation matrix` from PI | Yes — REPLACE friction check (>0.70 = block) | No (not in summary) | [CODE] Falls back to sector-based proxy when matrix is empty | Matrix is empty when <2 positions or yfinance fails |
| `sector` from `_SYMBOL_SECTOR` | Yes — correlation proxy for REPLACE | No | [CODE] Loaded from watchlist_core.json at import time | |
| `time_bound_actions` from cfg | Yes — REPLACE friction block within same_day_replace_block_hours | No | [CODE] Reads strategy_config.json at call time | |
| `available_for_new` from PI sizes | Yes — ADD gate | No explicit output field | [CODE] From `compute_dynamic_sizes()` result | |
| `same-day cooldown` | Yes — module-level `_daily_cooldown` dict | Yes (suppression_reason field) | [CODE] In-memory only, resets on restart | Not persisted |
| `session_tier` | Yes — passed to artifact but not used in decision logic | Yes (in artifact header) | [CODE] | |
| VIX | No | No | [UNKNOWN] | Not consumed by allocator; VIX guard lives in risk_kernel/sonnet_gate |
| Buying power / margin | No | No | [INFERRED] | `available_for_new` from PI implicitly captures this |

### What Triggers Each Action

**ADD:** [CODE] `thesis_score >= 7` (normalized >= 70) AND `available_for_new > min_rebalance_notional ($500)` AND `acct_pct < tier_max - weight_deadband (2%)`. Tier max is inferred from market_value vs PI sizing caps (15% for core-sized, 8% for standard, 5% for small). Only fires for *incumbents* already held, not for new names.

**TRIM:** [CODE] `thesis_score <= trim_score_threshold (4)` AND `market_value > min_rebalance_notional ($500)`. Trim fraction is graduated from `trim_severity` config: score ≤ 2 → 75%, score ≤ 4 → 50%, score ≤ 6 → 25%.

**REPLACE:** [CODE] `candidate.signal_score - weakest_incumbent.thesis_score_normalized >= replace_score_gap (15)` AND `weakest_incumbent.market_value >= min_rebalance_notional` AND all friction checks pass (correlation, time-bound, daily cooldown). Only compares the *single* weakest incumbent vs *single* strongest candidate.

**HOLD:** [CODE] Default for all incumbents that don't hit TRIM (score ≤ 4) or ADD (score ≥ 7) thresholds, or where ADD/TRIM conditions are not met.

**FREE CASH:** [CODE] Not a named action. Cash is freed implicitly if TRIM or REPLACE is recommended and executed.

**NO ACTION (shadow suppression):** [CODE] Six paths: (1) `enable_shadow=False`, (2) score gap below `replace_score_gap`, (3) correlation > 0.70 between candidate and incumbent, (4) time-bound exit within `same_day_replace_block_hours`, (5) daily cooldown active for symbol, (6) `max_recommendations_per_cycle` cap (5) exceeded.

---

## 4. Anti-Churn / Stability Analysis

### Anti-Churn Table

| Mechanism | Exists? | Where enforced | Confidence | Gap |
|-----------|---------|----------------|------------|-----|
| Score gap threshold (replace_score_gap=15) | Yes | `_decide_actions()` | [CODE] | Uses normalized 0–100 scale for incumbent, raw 0–100 for candidate — same scale, but candidate score from Haiku Stage 2 scorer is not directly comparable to thesis_score×10 |
| Sector / correlation proxy for REPLACE | Yes | `_check_correlation()` | [CODE] | Matrix is only populated for held symbols; candidate vs incumbent direct pair is usually absent from matrix (candidate not held). Falls back to sector string match. |
| Time-bound exit block for REPLACE | Yes | `_check_time_bound()` | [CODE] | Only blocks REPLACE of the target symbol; ADD/TRIM are not time-bound-gated |
| Daily per-symbol cooldown | Yes | `_check_cooldown()`, `_daily_cooldown` dict | [CODE] | In-memory only — resets on every process restart; 5-min systemd restart means cooldown is effectively non-functional during crash-restart scenarios |
| Minimum notional floor ($500) | Yes | `_decide_actions()` both TRIM and REPLACE paths | [CODE] | |
| Max recommendations cap (5 per cycle) | Yes | `_decide_actions()` tail section | [CODE] | HOLDs are not capped; only non-HOLD actions count toward cap |
| Weight deadband (2%) | Yes | ADD gate (`acct_pct < tier_max - 0.02`) | [CODE] | |

**Top 3 anti-churn weaknesses:**

1. **[CODE] Daily cooldown resets on restart.** The `_daily_cooldown` dict is module-level and in-memory. Systemd restarts the service every 30 seconds on failure. Any crash resets all cooldowns, potentially allowing the same REPLACE recommendation to fire every cycle on restart.

2. **[CODE] REPLACE only considers weakest vs strongest, not portfolio-wide.** If the weakest incumbent changes each cycle due to minor price fluctuations, REPLACE targets will rotate. No minimum holding period is enforced. A position could be recommended for REPLACE one cycle and then, after a small price recovery, no longer be the weakest, causing the allocator to flip.

3. **[CODE/INFERRED] Candidate signal scores (0–100, Haiku) vs incumbent thesis scores (×10 normalized, pure Python heuristics) are on the same numerical scale but computed by fundamentally different methods.** A candidate with signal_score=65 and an incumbent with thesis_score=5 (normalized=50) produces a gap=15, triggering REPLACE. But these two numbers reflect very different inputs and cannot be directly compared. The Haiku signal scorer assigns scores based on momentum/news/EMA signals within the last cycle; the thesis scorer reflects multi-day holding health. A fresh, high-momentum candidate will nearly always beat any incumbent that has been held for days (catalyst aging alone costs 1–2 points over 5 days). This creates a structural bias toward turnover for aging positions.

---

## 5. Margin / Buying Power Awareness

### Capital-Awareness Table

| Feature | Present? | In code | In shadow output | Notes |
|---------|----------|---------|-----------------|-------|
| Available-for-new budget | Yes | `sizes.available_for_new` from PI `compute_dynamic_sizes()` | Implicit (used in ADD gate) | PI computes `available = max(0, max_exposure - current_exposure)` |
| Buying power (margin) | Indirect | `compute_dynamic_sizes()` reads `buying_power` param and computes margin-aware tier caps | Not exposed in shadow artifact | Shadow allocator reads `sizes.available_for_new` which reflects equity*max_exp_pct - current_exposure, not full buying_power |
| Margin multiplier tiers | No | Not consumed by `portfolio_allocator.py` | No | Risk kernel handles margin; allocator does not |
| Equity floor (PDT $26K) | No | Not checked by allocator | No | Risk kernel is authoritative |
| Per-position cap ceiling | Partial | `_target_weights()` uses 15/8/5% tier logic | Yes (target_weight_pct field) | Inferred from market_value bands; not reading strategy_config tier_pcts |
| Exposure cap / max_total_exposure | No | Not explicitly checked | No | `available_for_new` implicitly incorporates it |

**Truly margin-aware or just equity-aware?**

[CODE] **Equity-aware only, with indirect margin pass-through.** The allocator reads `sizes.available_for_new` from PI, which is computed as `max(0, equity × max_exposure_pct - current_exposure)`. The `max_exposure_pct` in the current `strategy_config.json` is 0.95, meaning it will allow adding almost to full equity. The true margin-aware budget (based on buying_power with conviction-tiered multipliers) lives in `risk_kernel._compute_sizing_basis()` and `risk_kernel.size_position()`. The allocator does not call these — it never knows what the kernel would actually approve.

**Practical consequence:** The allocator may recommend ADD when `available_for_new > $500` even if the risk kernel would reject the actual BUY order due to the `max_position_pct_equity` cap (currently 0.25), VIX scaling, or exposure headroom constraints. The advisory text may mislead Claude.

---

## 6. Relationship to A1 Prompt / A1 Decision Layer

### What Each Layer Should Own

**A1 prompt (system_v1.txt) should own:**
- Narrative reasoning about thesis quality
- Catalyst recency judgment
- When to suggest REALLOCATE vs HOLD vs CLOSE
- Direction of capital rotation (sector preference, regime fit)

**Allocator (portfolio_allocator.py) should own:**
- Numeric ranking of incumbents and candidates
- Threshold-based advisory recommendations (TRIM/ADD/REPLACE)
- Anti-churn friction enforcement
- Artifact logging for weekly review analysis

**Risk kernel (risk_kernel.py) should own:**
- All sizing math (margin, tier pcts, VIX scaling)
- Hard eligibility gates (PDT floor, VIX halt, session gate, max positions)
- Stop/target placement
- REALLOCATE execution safety (exit verification before entry)

**Division of labor assessment:** [CODE] The current architecture has a **three-way duplication** of the TRIM threshold guidance:

1. `system_v1.txt` "THESIS SCORE ACTION GUIDE": "4–5/10: TRIM 25% if fresher opportunities exist; 2–3/10: TRIM 50% or close"
2. `portfolio_intelligence.format_thesis_ranking_section()`: "EXIT CONSIDER — weakest thesis" for score ≤ 3, with capital reallocation recommendation text
3. `portfolio_allocator.py`: `trim_score_threshold=4` from config, graduated severity from `trim_severity` table

These three are not always consistent. The system prompt says score 4–5 warrants 25% trim; the allocator fires TRIM at score ≤ 4 with 50% trim severity. Claude sees all three sources and must reconcile them.

The allocator section is correctly marked as advisory in both the module docstring and the format string ("SHADOW MODE — do not treat as live order mandate"), but the prompt injection creates implicit pressure on Claude to follow the recommendation.

---

## 7. Relationship to Risk Kernel / Execution

### Current REALLOCATE Wiring Status

[CODE] **The REALLOCATE execution path has a signature mismatch bug.**

- `risk_kernel.py:process_idea()` correctly handles `AccountAction.REALLOCATE`, validates exit position, sizes the entry, and returns a `BrokerAction(action=REALLOCATE, exit_symbol=..., entry_symbol=...)`.
- `order_executor.execute_all()` receives this BrokerAction, converts it via `BrokerAction.to_dict()`, and dispatches to the `elif act == "reallocate":` branch.
- That branch calls: `execute_reallocate(action, _get_alpaca())` — passing `(dict, TradingClient)`.
- But `portfolio_intelligence.execute_reallocate()` signature is: `(exit_symbol: str, entry_action: dict, alpaca_client)` — expects `(str, dict, TradingClient)`.
- This call will **raise a TypeError** at runtime because `action` (a dict) is passed where `exit_symbol` (a str) is expected.
- The exception is caught by the outer `except Exception as re_exc:` block in `execute_all()` and returns an `ExecutionResult(status="error")` — non-fatal but non-functional.
- Additionally, `portfolio_intelligence.execute_reallocate()` itself is marked "DEAD CODE" in its own docstring: "Currently DEAD CODE — not wired into bot.py. If activated, must route through risk_kernel.py first."

### Required Invariants If Allocator Is Promoted

If `enable_live` were to be enabled, the following invariants must be satisfied before any TRIM/ADD/REPLACE action executes:

1. **Risk kernel must gate all live actions.** The allocator must produce a `TradeIdea` and route it through `risk_kernel.process_idea()` — it must not call `execute_reallocate()` directly.
2. **No double-execution.** Claude's Stage 3 decision may independently recommend the same TRIM or CLOSE that the allocator would initiate. There must be an idempotency check or a clear authority hierarchy preventing duplicate order submission.
3. **No execution during halt/reconcile mode.** The allocator must read `a1_mode` from divergence tracking before initiating any live action.
4. **TRIM must cancel conflicting OCA orders first.** The executor's existing BUG-009 fix shows that bracket order OCA children can block new sell orders. TRIM must pre-cancel any open stop or take-profit orders for the symbol before submitting a partial close.
5. **Position accuracy.** Live TRIM must read fresh Alpaca position qty at execution time, not from the cached positions snapshot from 5 minutes earlier.

---

## 8. Relationship to A2

### A1 Needs vs A2 Needs Comparison

| Need | A1 Allocator | A2 Options |
|------|-------------|------------|
| Incumbent ranking | thesis_score (heuristic, multi-factor, 1–10) | structure lifecycle + should_close_structure() + roll logic |
| Candidate ranking | signal_score from Haiku Stage 2 scorer | IV environment + confidence from four-way debate |
| Trim/replace trigger | thesis_score threshold + score gap vs candidate | DTE, IV crush, time-stop (40%/50% elapsed DTE) |
| Anti-churn | sector block, daily cooldown, notional floor | No equivalent cooldown; structure reconciliation prevents duplicates |
| Capital budget | available_for_new from PI sizes | equity floor ($25K) + per-tier pct sizing from A2 config |
| Execution path | SHADOW — no orders | LIVE via options_executor.submit_structure() |

**Conclusion: One allocator, one+governor, or separate?**

[INFERRED] The two accounts should maintain **separate allocation logic**, as they already do. A2's allocation is intrinsically coupled to options lifecycle mechanics (DTE, IV crush, leg reconciliation) that are incompatible with A1's equity-based heuristics. A shared allocator abstraction would require significant parameter divergence that effectively recreates two separate systems. The existing separation is architecturally correct.

However, for A1: the current fragmentation between `portfolio_allocator.py` (shadow recommender), `portfolio_intelligence.py` (analytics provider), and `risk_kernel.py` (execution gatekeeper) is the right long-term architecture. The shadow system correctly depends on PI analytics and defers all execution to the kernel. The only structural problem is the REALLOCATE execution bug and the three-way duplication of threshold guidance.

---

## 9. Promotion Readiness Assessment

**Promotion stage:** NOT READY

### Criterion Table

| Criterion | Status | Evidence | Blocks promotion? |
|-----------|--------|----------|-----------------|
| ≥14 consecutive shadow cycles with valid artifacts | FAIL | 0 records in shadow JSONL | YES — highest blocker |
| `shadow_status_latest.json` populated | FAIL | File not found | YES |
| Weekly review has reviewed shadow output | UNKNOWN | Cannot review what doesn't exist | YES |
| TRIM logic validated end-to-end | UNKNOWN | No runtime evidence | YES |
| `enable_live` flag wired to actual execution path | NOT APPLICABLE | Flag exists and is hardwired False | Not blocking (correct) |
| validate_config.py gate present | PASS | [CODE] Gate exists in validate_config.py lines 759–803 | No |
| Anti-churn rules confirmed via test replay | PARTIAL | Test suite (test_s6_portfolio_allocator.py, test_s7_phase_c.py) covers decision logic | No — tests pass in isolation |
| REALLOCATE execution path functional | FAIL | Signature mismatch bug in order_executor.py:806 — passes wrong types | YES — if live REALLOCATE is ever used |
| Daily cooldown survives process restarts | FAIL | In-memory only; resets on every systemd restart | YES (for live promotion) |
| Candidate signal scores comparable to thesis scores | UNKNOWN | Structural concern — different methods, different time horizons | Should be documented |
| Prompt guidance consistent across three sources | FAIL | Three independent TRIM threshold sources (system prompt, PI formatter, allocator config) | Not a blocker for shadow, but must be resolved for live |

---

## 10. Recommended Target Architecture

### Recommended Future Control Law (Plain English)

**Incumbent ranking:** Each held position receives a `thesis_score` (1–10) computed by `portfolio_intelligence.score_position_thesis()` every cycle. This score reflects catalyst age, technical structure, P&L trajectory, sector alignment, and any time-bound override. The scores are stable (heuristic, no Claude call) and are already computed in the existing pipeline. The allocator uses them as the primary signal for all incumbent decisions.

**Challenger ranking:** Unowned symbols from `signal_scores.json` (written by Stage 2 Haiku scorer after each cycle) are ranked by signal_score (0–100). Only symbols with signal_score > 0 and not already held are considered challengers.

**When it trims:** When an incumbent's `thesis_score <= 4` and its position is large enough (`market_value >= $500`), the allocator recommends TRIM. The trim fraction scales with weakness: score ≤ 2 → 75% trim, score ≤ 4 → 50% trim, score ≤ 6 → 25% trim. These severity tiers are already config-driven via `trim_severity` in `strategy_config.json`.

**When it replaces:** When `challenger.signal_score - weakest_incumbent.thesis_score_normalized >= 15` AND the weakest incumbent has at least `$500` in value AND the challenger is not in the same sector as the incumbent AND the incumbent has no imminent time-bound exit AND the symbol has not been recommended today. Only the weakest incumbent vs the strongest challenger are compared.

**When it frees cash:** Cash is freed as a side effect of TRIM or REPLACE execution. There is no standalone FREE CASH action. If exposure is below the reserve floor and no good setups exist, the allocator does nothing.

**When it does nothing:** HOLD is the default for all incumbents with scores 5–6. No REPLACE is fired when the score gap is below threshold. The module always returns something (even if all HOLDs) — it never blocks the cycle.

**For live promotion, the control law must additionally:**
1. Route all TRIM/REPLACE/ADD recommendations through `risk_kernel.process_idea()` before submitting.
2. Pre-cancel conflicting OCA orders before any partial close (TRIM).
3. Persist cooldown state to disk (not in-memory only) to survive process restarts.
4. Read fresh Alpaca position qty at execution time, not from cached snapshot.
5. Check `a1_mode` (divergence operating mode) and abort if not NORMAL.
6. Resolve the three-source TRIM threshold inconsistency — pick one authority (recommend: config-driven `trim_score_threshold` in `portfolio_allocator` section) and remove the duplicates from system_v1.txt and PI formatter.

---

## 11. Open Questions / Required Proof Before Promotion

1. **[UNKNOWN] Why is `portfolio_allocator_shadow.jsonl` empty?** The most likely cause is a runtime exception in `run_allocator_shadow()` swallowed by the outer try/except in `bot_stage0_precycle.py`. To diagnose: enable DEBUG logging, filter for `[ALLOC]` prefix in bot.log, and check which exception is being caught. The `_ARTIFACT_PATH` creation uses `_ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)` which should be safe, but a failure in `_load_candidates()` (e.g., `signal_scores.json` absent) would leave `candidates=[]` while still completing without error. If candidates are always empty, no REPLACE or ADD recommendations fire — only HOLDs. The artifact would still be written. So the failure is likely earlier, in `_rank_incumbents()` or `pi_data` parsing.

2. **[UNKNOWN] Does `data/market/signal_scores.json` exist and is it fresh on the server?** The allocator's candidate loading depends entirely on this file. If it is stale (>10 minutes) or absent, `_load_candidates()` returns `[]` silently. The bot.py Stage 2 is supposed to write this file after `score_signals()` completes (BUG-004 fix). Confirm the file is being written.

3. **[CODE — requires verification] What does the allocator section look like in actual Stage 3 prompts?** The section is appended after `user_template_v1.txt` renders. If `allocator_output = None` (because shadow fails), `format_allocator_section(None)` returns the explicit absence header "(allocator not available this cycle)". Claude sees this every cycle but cannot act on it. Confirm whether Claude is receiving the absence message or real output.

4. **[CODE] The REALLOCATE signature mismatch must be fixed before ANY live REALLOCATE executions.** `order_executor.py:806` calls `execute_reallocate(action, _get_alpaca())` but the function signature is `(exit_symbol: str, entry_action: dict, alpaca_client)`. This has been dead code so far, but if Claude ever emits an idea with `exit_symbol + entry_symbol`, the kernel approves it, the executor will error silently. Fix: change the call to `execute_reallocate(action.get('exit_symbol', ''), action, _get_alpaca())` or, better, remove `execute_reallocate` from PI and route through the kernel + executor properly.

5. **[INFERRED] Are the realloc_log.jsonl records production data?** The 112 records all show `equity=100000.0` with stub order IDs and test-pattern reasons. If these are test data, the file should be cleared to avoid confusion in weekly review analytics. If they are production data, there is an undiscovered live code path writing to `realloc_log.jsonl` that is not visible in the current codebase.

6. **[UNKNOWN] Does the graduated `trim_severity` table produce correct behavior?** The config shows `score_max=6 → trim_pct=0.25` which conflicts with the allocator's TRIM gate of `score <= 4`. A position with score=5 or 6 will never reach the TRIM block (gate requires `score <= trim_score_threshold = 4`), so the `score_max=6` tier in `trim_severity` is currently unreachable. This may be intentional for future config changes, but should be verified.

7. **[UNKNOWN] Has the weekly review ever consumed allocator shadow output?** `weekly_review.py` is not referenced in the allocator code, and no reference to `portfolio_allocator_shadow.jsonl` exists in the weekly review source. The shadow output is currently isolated — it affects the Stage 3 prompt advisory but has no weekly review feedback loop. This should be added as part of promotion prep.

---

## Appendix A — Commands Run

All commands run locally on the mirror at `/Users/eugene.gold/trading-bot/`. Date: 2026-04-28.

**1. `wc -l portfolio_intelligence.py`**
```
1072 /Users/eugene.gold/trading-bot/portfolio_intelligence.py
```

**2. `find . -name "*allocat*" | sort`**
```
/Users/eugene.gold/trading-bot/portfolio_allocator.py
/Users/eugene.gold/trading-bot/tests/test_s6_portfolio_allocator.py
```
Note: `portfolio_allocator_shadow.py` does NOT exist. The shadow engine lives in `portfolio_allocator.py`.

**3. `ls -la data/analytics/portfolio_allocator_shadow.jsonl`**
```
NOT FOUND
```

**4. Latest 10 shadow records**
```
NO SHADOW DATA (file does not exist)
```

**5. `wc -l data/analytics/portfolio_allocator_shadow.jsonl`**
```
0 (file does not exist)
```

**6. Action distribution in shadow**
```
NO DATA
```

**7. `cat data/reports/shadow_status_latest.json`**
```
NOT FOUND
```

**8. `grep -i "allocat\|reallocat\|trim\|replace\|free.cash" logs/bot.log | tail -30`**
```
(no output — log file empty or search returned nothing on local mirror)
```

**9. strategy_config.json allocator flags**
```
feature_flags: {}
shadow_flags: {'enable_context_compressor_shadow': False}
lab_flags: {}
portfolio_allocator section: {'enable_shadow': True, 'enable_live': False, 'replace_score_gap': 15,
  'trim_score_drop': 10, 'weight_deadband': 0.02, 'min_rebalance_notional': 500,
  'max_recommendations_per_cycle': 5, 'same_symbol_daily_cooldown_enabled': True,
  'same_day_replace_block_hours': 6, 'trim_severity': [
    {'score_max': 2, 'trim_pct': 0.75}, {'score_max': 4, 'trim_pct': 0.50},
    {'score_max': 6, 'trim_pct': 0.25}],
  '_note': 'S6-ALLOCATOR: shadow mode only. enable_live must remain false until trim-only promotion sprint. trim_severity added S7-I.'}
```

**10. `grep -n "allocat\|reallocat\|trim" reconciliation.py | head -40`**
```
284: deadline_dt = deadline_dt.replace(tzinfo=timezone.utc)
756: deadline = (now_et.replace(hour=15, minute=45, second=0, microsecond=0)
```
No allocator/reallocate/trim references in reconciliation.py.

**11. `grep -n "portfolio_intelligence\|build_portfolio_intelligence\|allocat" bot.py | head -50`**
```
47: from portfolio_allocator import format_allocator_section as _format_allocator_section
376: pi_data=state.pi_data,
397: pi_data=state.pi_data,
402: allocator_section=_format_allocator_section(state.allocator_output),
499: _idea.action.value in ("buy", "reallocate")
```

**12. `grep -n "REALLOCAT\|reallocat\|BrokerAction\|AccountAction" order_executor.py | head -30`**
```
801:            elif act == "reallocate":
804:                        execute_reallocate,  # noqa: PLC0415
806:                    realloc_result = execute_reallocate(action, _get_alpaca())
...
```

**13. `grep -n "REALLOCAT\|reallocat" risk_kernel.py | head -20`**
```
720:      REALLOCATE → size entry side; include exit info for executor
816:    # ── REALLOCATE ────────────────────────────────────────────────────────────
817:    if act == AccountAction.REALLOCATE:
...
885:            action=AccountAction.REALLOCATE,
```

**14. `grep -n "allocat\|portfolio_intelligence\|pi_data" bot_stage3_decision.py | head -30`**
```
23: import portfolio_intelligence as pi
151: "hold": "hold", "reallocate": "enter_long",
229: pi_data: dict = None,
234: allocator_section: str = "",
264: _pi = pi_data or {}
351: # Inject allocator shadow section if provided (advisory only; appended after template render).
353: if allocator_section and allocator_section.strip():
354:     rendered += "\n\n" + allocator_section
...
```

**15. Latest 5 shadow records (detailed)**
```
EMPTY (file does not exist)
```

**16. `ls -la data/analytics/`**
```
total 7024
-rw-r--r--@  1 eugene.gold  staff        0 Apr 15 13:47 .gitkeep
-rw-r--r--@  1 eugene.gold  staff     2600 Apr 16 16:24 advisor_credibility.json
-rw-r--r--@  1 eugene.gold  staff   674129 Apr 28 13:27 attribution_log.jsonl
-rw-r--r--@  1 eugene.gold  staff  1541679 Apr 28 13:27 cost_attribution_spine.jsonl
-rw-r--r--@  1 eugene.gold  staff      643 Apr 17 18:42 decision_outcomes.jsonl
-rw-r--r--@  1 eugene.gold  staff     9620 Apr 23 14:30 divergence_log.jsonl
-rw-r--r--@  1 eugene.gold  staff   282705 Apr 28 13:27 incident_log.jsonl
-rw-r--r--@  1 eugene.gold  staff      532 Apr 15 15:08 near_miss_log.jsonl
-rw-r--r--@  1 eugene.gold  staff    34554 Apr 23 14:30 realloc_log.jsonl
```
Note: `portfolio_allocator_shadow.jsonl` is absent from this list.

**17. Allocator import/usage across pipeline**
```
/trading-bot/portfolio_allocator.py:44: _ARTIFACT_PATH = _ROOT / "data" / "analytics" / "portfolio_allocator_shadow.jsonl"
/trading-bot/bot.py:47: from portfolio_allocator import format_allocator_section as _format_allocator_section
/trading-bot/bot.py:402: allocator_section=_format_allocator_section(state.allocator_output),
/trading-bot/bot_stage0_precycle.py:137: allocator_output: Any = None
/trading-bot/bot_stage0_precycle.py:433: import portfolio_allocator as _pa_mod
/trading-bot/bot_stage0_precycle.py:434: allocator_output = _pa_mod.run_allocator_shadow(pi_data=pi_data, positions=positions, cfg=cfg, session_tier=session_tier, equity=equity)
/trading-bot/validate_config.py:759: # portfolio_allocator section (S6-ALLOCATOR)
/trading-bot/scripts/feature_audit.py:180: def check_portfolio_allocator() -> tuple[str, str]:
/trading-bot/scripts/feature_audit.py:536: ("Portfolio Allocator Shadow", check_portfolio_allocator),
```

**18. Suppressed actions distribution**
```
NO DATA (no shadow records)
```

---

## Appendix B — Key Artifact Samples

### B.1 — portfolio_allocator section in strategy_config.json (live, verified)

```json
"portfolio_allocator": {
  "enable_shadow": true,
  "enable_live": false,
  "replace_score_gap": 15,
  "trim_score_drop": 10,
  "weight_deadband": 0.02,
  "min_rebalance_notional": 500,
  "max_recommendations_per_cycle": 5,
  "same_symbol_daily_cooldown_enabled": true,
  "same_day_replace_block_hours": 6,
  "trim_severity": [
    {"score_max": 2, "trim_pct": 0.75},
    {"score_max": 4, "trim_pct": 0.50},
    {"score_max": 6, "trim_pct": 0.25}
  ],
  "_note": "S6-ALLOCATOR: shadow mode only. enable_live must remain false until trim-only promotion sprint. trim_severity added S7-I."
}
```

### B.2 — REALLOCATE signature mismatch (critical bug)

`order_executor.py:806` (actual call):
```python
realloc_result = execute_reallocate(action, _get_alpaca())
# action is a dict; _get_alpaca() is TradingClient
```

`portfolio_intelligence.execute_reallocate()` signature (expected):
```python
def execute_reallocate(
    exit_symbol: str,          # <-- expects a string
    entry_action: dict,        # <-- expects a dict
    alpaca_client,             # <-- expects TradingClient
) -> dict:
```

This call will raise `TypeError: close_position() argument 'symbol' must be str, not dict` (or similar) whenever a REALLOCATE action reaches the executor. The exception is caught by `except Exception as re_exc` and returned as `ExecutionResult(status="error")`.

### B.3 — realloc_log.jsonl sample (confirmed test data pattern)

```json
{"status": "submitted", "trim_symbol": "AMZN", "shares_sold": 100,
 "cancelled_orders": [], "target_value": 15000.0, "freed_estimate": 10000.0,
 "stop_order_id": "ord-trim", "order_id": "ord-trim",
 "reason": "trimmed 100 shares of AMZN @ ~$100.00 → target $15,000 (15% of equity); cancelled 0 prior orders",
 "ts": "2026-04-22T21:02:30.083187+00:00", "equity": 100000.0}

{"status": "failed", "trim_symbol": "AMZN", "shares_sold": 0,
 "reason": "trim sell failed after cancelling 0 orders: broker down",
 "ts": "2026-04-22T21:02:30.129518+00:00", "equity": 100000.0}
```

Evidence this is test data: `equity=100000.0` is a round stub value; `reason: "broker down"` and `reason: "disk full"` are test error strings; `order_id: "ord-ok"` and `order_id: "ord-trim"` are test stubs. No Python source file in the codebase writes to this path.

### B.4 — format_allocator_section() output when shadow is working (code-derived example)

When `run_allocator_shadow()` returns a non-None result, the Stage 3 prompt receives:
```
=== PORTFOLIO ALLOCATOR SHADOW (advisory only) ===
Weakest incumbent : XBI  thesis_score=4/10  health=MONITORING
Strongest candidate: NVDA  signal_score=78  direction=bullish
Shadow recommendations (advisory):
  TRIM XBI  gap=None
  REPLACE NVDA  exit=XBI  gap=28.0
[SHADOW MODE — do not treat as live order mandate]
```

When it returns None (current state), Claude sees:
```
=== PORTFOLIO ALLOCATOR SHADOW (advisory only) ===
  (allocator not available this cycle)
```

### B.5 — Three-source TRIM threshold inconsistency

Source 1 — `prompts/system_v1.txt` THESIS SCORE ACTION GUIDE:
> "4–5/10: TRIM 25% if fresher opportunities exist; 2–3/10: TRIM 50% or close"

Source 2 — `portfolio_intelligence.format_thesis_ranking_section()`:
> Emits "Capital reallocation — EXIT RECOMMENDED to free capital" for score ≤ 4
> Emits "REDUCE — thesis weakening" for score ≤ 5

Source 3 — `portfolio_allocator.py` config:
> `trim_score_threshold = 4` → triggers TRIM at score ≤ 4
> `trim_severity`: score ≤ 2 → 75%, score ≤ 4 → 50%, score ≤ 6 → 25%

The system prompt says score 4–5 warrants 25% trim. The allocator says score ≤ 4 warrants 50% trim. These are in direct conflict for score=4, which is the threshold value.
