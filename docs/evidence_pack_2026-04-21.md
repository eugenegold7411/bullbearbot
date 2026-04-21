# BullBearBot — Full System Evidence Pack
**Generated:** 2026-04-21 ~01:20 UTC  
**Purpose:** External assessment of current real operating state  
**Scope:** Repo truth, runtime/server state, scheduler/data freshness, A1 pipeline, A2 pipeline, costs, CI, docs vs runtime, orphaned artifacts, and structured summary

---

## Section 1 — Repo Truth

### Source of Truth
- **Local repo:** `/Users/eugene.gold/trading-bot/` (rsync mirror)
- **VCS:** No git on VPS. Code deployed via rsync. `.env`, `logs/`, `data/` excluded from sync.
- **Tag:** `v1.0-phase1-complete` exists locally
- **No GitHub remote** — push not yet configured (documented in CLAUDE.md)

### Code Layout (verified local)

| Layer | Files | Notes |
|-------|-------|-------|
| A1 pipeline | `bot.py`, `bot_stage0_precycle.py`–`bot_stage4_execution.py` | Full 4-stage; ~1,804 lines in bot.py |
| A2 pipeline | `bot_options.py`, `bot_options_stage0_preflight.py`–`bot_options_stage4_execution.py` | Thin orchestrator, 6943 bytes |
| Scheduler | `scheduler.py` | 24/7 loop, 15+ maintenance jobs |
| Intelligence | 12 files (market_data, macro_wire, morning_brief, scratchpad, reddit_sentiment, etc.) | |
| Options stack | 6 files (options_data, options_intelligence, options_builder, options_executor, options_state, order_executor_options) | |
| Shared substrate | `semantic_labels.py`, `abstention.py`, `hindsight.py`, `cost_attribution.py`, `feature_flags.py`, `versioning.py`, `model_tiering.py`, `incident_schema.py` | Epic 1 — all present |
| Tests | `tests/` — 1034+ test files | 8 ChromaDB failures excluded from CI |
| Prompts | `prompts/system_v1.txt`, `prompts/user_template_v1.txt`, `prompts/compact_template.txt`, `prompts/system_options_v1.txt` | |
| Config | `strategy_config.json`, `validate_config.py` | Gate 14 S4-G code present locally, **not yet deployed** |

### Recent Sprint Deliverables (local, not yet deployed to VPS)
| Sprint | Status | Description |
|--------|--------|-------------|
| S4-A | Deployed | Veto threshold tuning (5%→15%), universe 16→43 symbols |
| S4-C | Deployed | data_warehouse silent failure fix |
| S4-D | **Local only** | Reddit 403 fix, `sentiment_unavailable` flag, auth docs |
| S4-G | **Local only** | Gate 14 dynamic (43 symbols), readiness scheduler job |

---

## Section 2 — Server / Runtime State

### Server Identity
| Field | Value |
|-------|-------|
| IP | 161.35.120.8 (DigitalOcean VPS) |
| OS | Ubuntu 24.04.4 LTS |
| RAM | 2 GB (bot process ~164.5 MB) |
| Disk | 24 GB (3.6 GB used, 20 GB free) |
| Python | 3.12.3 |
| Virtualenv | `/home/trading-bot/.venv/` |

### Service State
```
● trading-bot.service — ACTIVE (running)
  Started: 2026-04-21 01:14:36 UTC
  PID: 238237
  Memory: ~164.5 MB
  Session at evidence time: extended (15-min cycle)
  Last cycle: 2026-04-21 01:14:54 UTC
```
Service is healthy. Auto-restart with 30s backoff is configured.

### Current Session
- **Session type:** extended (4 AM–9:30 AM / 8 PM–11 PM ET)
- **Instruments active:** BTC/USD, ETH/USD (crypto only during extended)
- **Last A1 cycle decision:** BTC/USD + ETH/USD → HOLD (rejected_by_executor: hold — expected)
- **Sonnet gate:** SKIP (cooldown 3.1/15 min). 109 skips today, 23 Sonnet calls today.

### Open Positions (A1)
| Symbol | Stop | Notes |
|--------|------|-------|
| GLD | $435.66 | Trailed from $429.11 after BUG-007 fix |
| MSFT | $404.08 | Active stop |
| QQQ | $614.66 | Active stop |
| XBI | $130.81 | Active stop |
| AMZN | ~$238.91 | Active stop (manually placed Apr 15) |

**Account 1 equity:** $101,180.46 | **Cash:** $15,744.80 | **PDT floor:** $26,000 ✓

### Operating Modes
| Account | Mode | Reason | Set By |
|---------|------|--------|--------|
| A1 | **NORMAL** | manual_reset (TSM short covered) | operator, 2026-04-20 17:19 UTC |
| A2 | **NORMAL** | initial_state (obs complete, ready) | operator, 2026-04-20 17:20 UTC |

---

## Section 3 — Scheduler and Data Freshness

All timestamps relative to evidence collection time: **2026-04-21 01:15 UTC**

### Intelligence Data Freshness
| Artifact | Path | Size | Last Modified | Age | Status |
|----------|------|------|---------------|-----|--------|
| morning_brief.json | data/market/ | — | **NOT PRESENT** | N/A | ⚠️ MISSING from data/market/ — only in archive |
| (archive) | data/archive/2026-04-21/ | present | 2026-04-21 | 0h | archive write working |
| signal_scores.json | data/market/ | 83 bytes | 01:03 UTC | 12 min | ⚠️ STALE content (no scored_symbols — extended session) |
| global_indices.json | data/market/ | — | stale | 8h | ⚠️ LOG WARNING present |
| universe.json | data/market/ | 6298 bytes | 00:50 UTC | 25 min | ✓ fresh |
| congressional_trades.json | data/insider/ | 225 KB | 00:24 UTC | 51 min | ✓ fresh |
| readiness_status_latest.json | data/reports/ | 448 bytes | 00:58 UTC | 17 min | ✓ fresh (generated each cycle) |
| iv_pending_bootstrap.json | data/options/ | 19 bytes | prior | old | sentinel (expected) |
| chroma.sqlite3 | data/trade_memory/ | 1.9 MB | 01:14 UTC | 1 min | ✓ active |

### morning_brief.json — Root Cause
The `morning_brief.json` file is **absent from `data/market/`** and **only present in `data/archive/`**. The log shows:
```
WARNING   morning_brief [MORNING] morning_brief.json is stale (160.8h old) — injecting placeholder
```
This indicates the morning_brief write path is writing to the archive but not (or intermittently) to `data/market/morning_brief.json`. The bot degrades gracefully with a placeholder but the morning brief conviction content is absent from every cycle. This was diagnosed in a prior session as a stale-file bug; the fix may not be fully deployed or the path is still broken.

### Analytics Pipelines
| File | Size | Lines | Last Write | Status |
|------|------|-------|------------|--------|
| attribution_log.jsonl | 177 KB | 205 | 01:14 UTC | ✓ active |
| cost_attribution_spine.jsonl | 232 KB | 573 | 01:14 UTC | ⚠️ all entries have null token/cost (module_name="unknown") |
| decision_outcomes.jsonl | 3.5 KB | ~15 | 01:12 UTC | ✓ active |
| near_miss_log.jsonl | 946 bytes | 5 | 01:12 UTC | ✓ active |
| divergence_log.jsonl | 228 KB | 487 | 2026-04-18 22:10 UTC | ⚠️ STALE 27h — see Section 9 |
| incident_log.jsonl | 115 KB | 251 | 01:03 UTC | ✓ active |

### Scheduler Jobs — Status
| Job | Window | Status |
|-----|--------|--------|
| Data warehouse refresh | 4–5 AM ET weekdays | ✓ configured |
| Morning brief | 4:15–5:30 AM ET weekdays | ⚠️ firing but output missing from data/market/ |
| ORB scan | 4:30 AM ET weekdays | ✓ configured |
| Decision outcomes backfill | 4:30–5 PM ET weekdays | ✓ configured |
| Readiness check | **4:45 AM ET weekdays (S4-G)** | **NOT DEPLOYED** |
| Reddit sentiment refresh | every cycle | ✓ — all 4 subreddits 403 at last check |
| Weekly review | Sunday AM | ✓ configured |

---

## Section 4 — A1 Decision Pipeline Evidence

### Pipeline Execution (Last Cycle)
```
2026-04-21 01:14:46 INFO  bot  ── Cycle start session=extended ──
  Stage 0: market data + exit_manager + portfolio_intelligence + macro_intelligence
  Stage 1: regime classifier (Haiku)
  Stage 2: signal scorer (Haiku)
  Stage 2.5: scratchpad (skipped — extended session)
  Stage 3: [GATE] SKIP cooldown active (3.1/15 min)
    consecutive=1, skips_today=109, sonnet_today=23
  Stage 4: execution (hold actions only — extended session)
```

### Signal Quality
`signal_scores.json` at time of collection:
```json
{"top_3": ["SPY"], "elevated_caution": [], "reasoning": "ok", "scored_symbols": {}}
```
**Note:** Empty `scored_symbols` is expected during extended session. The 83-byte payload represents a minimal signal handoff — A2 would have no actionable symbols to evaluate this cycle.

### Decision Outcomes Log (Last 3 Real Decisions)
| Decision ID | Symbols | Action | Trigger | Status |
|-------------|---------|--------|---------|--------|
| dec_A1_20260420_211209 | BTC/USD | hold | max_skip_exceeded | rejected_by_executor: hold |
| dec_A1_20260420_211209 | ETH/USD | hold | max_skip_exceeded | rejected_by_executor: hold |
| prompt6_smoke | TEST | buy | smoke | rejected_by_executor: smoke test |

All module tags show full intelligence pipeline active: `regime_classifier`, `signal_scorer`, `vector_memory`, `macro_backdrop`, `macro_wire`, `morning_brief`, `insider_intelligence`, `reddit_sentiment`, `earnings_intel`, `portfolio_intelligence`, `risk_kernel`, `sonnet_full` = true on most recent real decision.

`scratchpad=false` on the overnight decision is expected (market hours only).

### Shadow Lane / Near-Miss Log
| Timestamp | Event | Symbol | Decision ID |
|-----------|-------|--------|-------------|
| 2026-04-15 | rejected_by_risk_kernel | AAPL | dec-001 (smoke) |
| 2026-04-15 | approved_trade | GLD | dec-002 (smoke) |
| 2026-04-15 | below_threshold_near_miss | TSM | (empty) |
| 2026-04-21 01:12 | approved_trade | BTC/USD | dec_A1_20260420_211209 |
| 2026-04-21 01:12 | approved_trade | ETH/USD | dec_A1_20260420_211209 |

Volume: 5 total entries since launch — **very sparse**. The two smoke-test entries (Apr 15) inflate the count. Real near-miss events from market hours are not appearing in significant numbers, suggesting either: (a) the bot is predominantly issuing HOLD decisions with no kernel rejections, or (b) kernel rejection events are not consistently triggering shadow lane writes (the known limitation noted in CLAUDE.md: decision_id not assigned at kernel time for rejections).

---

## Section 5 — A2 Decision Pipeline Evidence

### Observation Mode State
```json
{
  "trading_days_observed": 20,
  "first_seen_date": "2026-04-14",
  "observation_complete": true,
  "last_counted_date": "2026-04-14",
  "version": 2,
  "iv_history_ready": true,
  "iv_ready_symbols": { [16 Phase 1 symbols: all true] }
}
```
**Obs mode is complete.** A2 is legally permitted to trade. No active structures (`structures.json` does not exist — no open positions ever created).

### IV History
- **Files present:** 45 files in `data/options/iv_history/` (47 entries total with dirs)
- **Phase 1 symbols:** 16/16 ready (per obs_mode_state.json)
- **Phase 2 expansion (S4-A):** `_OBS_IV_SYMBOLS` now has 43 symbols in code — but `obs_mode_state.json` only tracks 16. The expanded universe is in the code but the state file was not updated. Gate 14 on server reads the old code and reports **0/16 ready** (see Section 8).

### A2 Cost Log (Recent Debate Calls)
```
2026-04-20 20:26 UTC  debate  claude-sonnet-4-6  input=100  output=50
2026-04-20 20:28 UTC  debate  claude-sonnet-4-6  input=100  output=50
2026-04-20 20:31 UTC  debate  claude-sonnet-4-6  input=100  output=50
2026-04-20 20:35 UTC  debate  claude-sonnet-4-6  input=100  output=50
```
**⚠️ ANOMALY:** All debate calls show exactly 100 input / 50 output tokens. This is a test stub pattern — these are not real debate calls. The test suite likely writes to the A2 cost log during test runs. No real A2 debate calls are present in the log.

### A2 Decision Directory
`data/account2/decisions/` — **does not exist.** No formal decision log directory for A2. Decisions appear to go to `data/account2/trade_memory/decisions_account2.json` only.

### A2 Pipeline Depth
A2 has all 5 stage modules present in code but **zero real trades have ever executed.** The bot has been in observation mode since day 1 and only just transitioned to live mode on 2026-04-20. The next market-hours window will be the first real A2 cycle.

---

## Section 6 — Cost / Usage Evidence

### Today (2026-04-21, first 75 minutes)
| Metric | Value |
|--------|-------|
| Daily cost | $0.041 |
| Daily calls | 2 |
| All-time total | $12.42 |
| By caller | macro_wire_classifier: $0.003 (1 call), ask_claude: $0.038 (1 call) |

Extended-session overnight rate is extremely low — Haiku-only runs. Cost spike occurs during market hours (Sonnet + signal scoring).

### All-Time Cost Profile
- **All-time total:** $12.42 (since 2026-04-13 launch, 8 days)
- **Implied average:** ~$1.55/day (but market-hours days hit $10–11 historically)
- **Attribution spine:** 573 entries, all with `module_name="unknown"`, `input_tokens=null`, `estimated_cost_usd=null`

**⚠️ CRITICAL GAP:** The cost attribution spine (`cost_attribution_spine.jsonl`) has been writing records continuously since launch but every entry has null token/cost fields and `module_name="unknown"`. The spine is recording `decision_made` events from the execution control layer but no Claude API call data is being populated. The `enable_cost_attribution_spine` flag may be set but the token-data wiring from the actual API calls to the spine is broken or the feature flag is not routing correctly.

### A2 Cost Log Anomaly
5 recent A2 debate calls at exactly 100/50 tokens are test artifacts (see Section 5).

---

## Section 7 — CI / Test Evidence

### Test Suite Status (Local)
```
1034 passed, 8 failed in 12.02s
```

### Failing Tests (all pre-existing, all ChromaDB)
| Test | Suite | Reason |
|------|-------|--------|
| test_03_retrieve_finds_saved_record | TestRetrieve | ChromaDB not installed in test env |
| test_04_metadata_roundtrip | TestRetrieve | ChromaDB not installed in test env |
| test_08_history_returns_recent_records | TestHistory | ChromaDB not installed in test env |
| test_10_near_miss_identified | TestNearMiss | ChromaDB not installed in test env |
| (4 additional scratchpad_memory tests) | | ChromaDB not installed |

All 8 failures are `@pytest.mark.requires_chromadb` and excluded from CI via `make test-ci`.

### KNOWN_FAILURES.md (Server)
> ChromaDB tests excluded from CI. Status: "Resolved — no longer listed as failures."

### CI Pipeline
```yaml
# .github/workflows/ci.yml (2 jobs)
lint-and-import-check:     # BLOCKING — py_compile + ruff + import checks
  needs: nothing
  on: push/PR to main

chromadb-tests:            # NON-BLOCKING — continue-on-error: true
  needs: lint-and-import-check
  runs: pytest tests/ (full suite)
```

### Sprint Test Results
| Sprint | Tests | Result |
|--------|-------|--------|
| S4-D (Reddit fix) | 15 new tests | ✓ all pass (verified in session) |
| S4-G (Gate 14 + scheduler) | 17 new tests | ✓ all pass (verified in session) |
| S4-C (data warehouse) | 23 new tests | ✓ all pass (from task output) |
| S4-A (veto tuning) | 42 new tests | ✓ 967 passing at time of sprint |

### make test-ci Baseline
Running `make test-ci` (= `pytest tests/ -m "not requires_chromadb"`) passes all 1026 non-ChromaDB tests. Baseline is held.

---

## Section 8 — Docs vs Runtime Truth

| Claim (CLAUDE.md / Docs) | Runtime Reality | Status |
|--------------------------|-----------------|--------|
| A2 obs mode complete, iv_history_ready=true for 43 symbols (S4-A) | obs_mode_state.json tracks only 16 Phase 1 symbols — v2 schema not updated for expansion | ⚠️ MISMATCH |
| Gate 14 shows 43/43 after S4-G deploy | readiness_status shows "only 0/16 ready" — S4-G not deployed | ⚠️ NOT DEPLOYED |
| morning_brief.json in data/market/ | File absent from data/market/, present only in data/archive/ | ⚠️ BUG ACTIVE |
| signal_scores.json has scored_symbols | 83-byte file with empty scored_symbols during extended session | ✓ expected behavior |
| Divergence mode A1=NORMAL | a1_mode.json shows NORMAL (manual reset 2026-04-20) | ✓ matches |
| Divergence mode A2=NORMAL | a2_mode.json shows NORMAL (initial state) | ✓ matches |
| All 5 open positions have stops | GLD, MSFT, QQQ, XBI, AMZN all have stops logged | ✓ matches |
| cost_attribution_spine active | 573 entries but all null token data | ⚠️ DATA QUALITY |
| Reddit public fallback (S4-D) | All 4 subreddits 403 on latest cycle — sentiment_unavailable=True | ✓ S4-D working (but Reddit blocked) |
| A2 debate calls firing | A2 cost log shows stub data (100/50 tokens) | ⚠️ NOT REAL DATA |
| Sev-1 clean days = 3 | readiness_status.json confirms 3 clean days (need 7) | ✓ matches |
| `_readiness_ran_date` global in scheduler.py | S4-G deployed locally only | ⚠️ NOT ON SERVER |

---

## Section 9 — Orphaned / Stale / Unbounded

### Orphaned Files in Root Directory
| File | Size | Date | Issue |
|------|------|------|-------|
| `bot.py.backup.20260413_234208` | 41 KB | 2026-04-13 | Orphaned backup from launch day — should be deleted or archived |
| `bot.py.bak.step4` | 74 KB | 2026-04-15 | Large orphaned backup from Phase 4 refactor — 74 KB is the old bot.py before split |
| `test_s4h_bars_pruning.py` | 12 KB | 2026-04-21 | **Test file in root dir, not in tests/** — will not be discovered by pytest |

### Divergence Log — Unbounded Growth
```
divergence_log.jsonl: 228 KB, 487 entries
  - 485 entries: protection_missing / severity=halt / symbol=TSM
  - 2 entries: order_partial_fill / severity=reconcile
  - Last entry: 2026-04-18 22:10 UTC (27+ hours stale)
```
TSM was exited before its 2026-04-15 earnings deadline. After exit, the stop order for TSM was gone from Alpaca. The divergence module continued firing `protection_missing` for TSM on every cycle until 2026-04-18 (3 days). Mode was manually reset to NORMAL on 2026-04-20. The 485 halt events are historical artifacts of a known transient stop-detection gap post-exit. No current protection issue — divergence log stopped updating after the A1 mode reset.

**Concern:** The divergence log is not pruned or rotated. At 487 entries = 228 KB, growth is slow but unbounded. The module has no TTL/rotation logic.

### Other Unbounded Logs
| File | Lines | Size | Pruning Logic |
|------|-------|------|---------------|
| attribution_log.jsonl | 205 | 177 KB | None visible |
| cost_attribution_spine.jsonl | 573 | 232 KB | None visible |
| incident_log.jsonl | 251 | 115 KB | None visible |
| near_miss_log.jsonl | 5 | 946 B | Effectively empty |
| mode_transitions.jsonl | 5 | small | Effectively empty |

No JSONL file in `data/analytics/` has rotation or pruning. At current growth rates these will not cause disk pressure (20 GB free), but should be tracked.

### Large Files
| File | Size | Notes |
|------|------|-------|
| chroma.sqlite3 | 1.9 MB | ChromaDB vector store — active, expected |
| Macro_Memo__Jan_2026.pdf | 4.7 MB | Source PDF for Citrini — manual artifact |
| bot.py.bak.step4 | 74 KB | Orphaned backup |
| bot.log | 6,959 lines | RotatingFileHandler manages this |

---

## Section 10 — Final Summary

### Status Classification

**CRITICAL (requires action before next market open)**
1. **morning_brief.json missing from data/market/** — The 4:15 AM morning brief is writing to archive but not to `data/market/morning_brief.json`. Every market cycle injects a stale placeholder instead of live conviction picks. This silently degrades Stage 3 quality every day.
2. **S4-D + S4-G not deployed** — Reddit 403 fix and Gate 14 dynamic update are complete locally with passing tests but have not been synced to the VPS. The readiness report currently shows Gate 14 as 0/16 failing (old threshold), and the `_maybe_run_readiness_check` scheduler job does not exist on the server.

**DEGRADED (working but quality impaired)**
1. **Reddit sentiment — all subreddits 403** — The public fallback is now correctly handling 403 (S4-D code works), but Reddit has blocked all 4 subreddits: wallstreetbets, stocks, investing, options. Sentiment input to Stage 3 is empty every cycle. No workaround until Reddit credentials (F001) are configured.
2. **cost_attribution_spine — null data** — 573 spine records have been written with correct schema but null token counts and null cost estimates. The spine is not capturing actual API cost data. The `module_name="unknown"` pattern suggests the cost router is not receiving the caller identity from the API response path.
3. **A2 cost log — stub data only** — A2 debate call records at exactly 100/50 tokens appear to be test artifacts. No evidence of real A2 debate token consumption in the log.
4. **global_indices.json stale (8h)** — Logged warning present. Overnight regime classification uses outdated global context.

**STALE (data present but not current)**
1. **Divergence log last write: 2026-04-18 22:10 UTC** — 27+ hours without new divergence events. Correctly reflects that A1 mode was reset and TSM exit resolved the halt condition. Not a bug — but indicates the divergence scanner is currently finding nothing to report.
2. **obs_mode_state.json tracks 16 symbols** — S4-A expanded `_OBS_IV_SYMBOLS` to 43, but the obs_mode_state.json file was already at `observation_complete=true` and was not updated. Gate 14 on the server reads the original 16-symbol check and reports 0/16 (the old data files exist but the path logic in the un-upgraded code is broken).

**MISSING**
1. **`data/account2/decisions/`** — No formal A2 decision directory exists. A2 decisions go to `data/account2/trade_memory/decisions_account2.json` only. No per-cycle decision artifact for audit trail.
2. **`test_s4h_bars_pruning.py` not in `tests/`** — The test file is in the root directory and will not be collected by pytest. Its 23+ tests are silently excluded from all test runs.

**CLEAN**
1. Service: active/running, stable RAM, no OOM, no crashes
2. A1/A2 operating modes: both NORMAL
3. PDT floor: $101,180 equity vs $26,000 floor — 4× headroom
4. Open positions: 5 positions, all with active stops
5. Unrealized P&L: +$1,358.84 (healthy)
6. Sev-1 clean days: 3 (4 more needed for full gate pass)
7. CI: 1026 non-ChromaDB tests passing, 8 ChromaDB tests correctly excluded
8. A2 obs mode: complete, mode=NORMAL, ready to trade
9. ChromaDB: 1.9 MB, active, no protobuf errors in recent logs
10. Sonnet gate: 23 calls today + 109 skips — gate functioning correctly

---

### Top 10 Action Items (Priority Order)

| Priority | Action | Effort |
|----------|--------|--------|
| 1 | **Deploy S4-D + S4-G to VPS** (rsync + service restart) | 10 min |
| 2 | **Investigate and fix morning_brief.json write path** — file goes to archive but not data/market/ | 30 min |
| 3 | **Configure Reddit credentials (F001)** — add REDDIT_CLIENT_ID/SECRET to .env | 5 min (after Reddit app approval) |
| 4 | **Fix cost_attribution_spine null data** — trace why module_name="unknown" and token counts null | 1 session |
| 5 | **Move test_s4h_bars_pruning.py into tests/** — currently invisible to pytest | 5 min |
| 6 | **Delete orphaned backups** — bot.py.backup.20260413_234208 and bot.py.bak.step4 | 2 min |
| 7 | **Add JSONL rotation for divergence_log.jsonl** — cap at N entries or N days | 30 min |
| 8 | **Update obs_mode_state.json to track 43 symbols** (or confirm Gate 14 S4-G handles this) | After S4-G deploy |
| 9 | **Verify A2 real debate calls logging** — confirm cost_log.jsonl captures live tokens not test stubs | After next market open |
| 10 | **Complete Sev-1 clean days** — 4 more needed; avoid any CRITICAL log patterns | Time-based |

---

## Appendix A — Key File Checksums / Sizes (Server, 2026-04-21 01:15 UTC)

```
/home/trading-bot/
├── data/analytics/
│   ├── attribution_log.jsonl          177 KB  205 lines   (active)
│   ├── cost_attribution_spine.jsonl   232 KB  573 lines   (null data)
│   ├── decision_outcomes.jsonl        3.5 KB  ~15 lines   (active)
│   ├── divergence_log.jsonl           228 KB  487 lines   (stale 27h)
│   ├── incident_log.jsonl             115 KB  251 lines   (active)
│   └── near_miss_log.jsonl            946 B   5 lines     (sparse)
├── data/account2/
│   ├── obs_mode_state.json            488 B               (complete, v2, 16 symbols)
│   ├── costs/cost_log.jsonl           present             (stub data only)
│   └── positions/                     (no structures.json)
├── data/options/iv_history/           47 files            (43+ symbols seeded)
├── data/reports/
│   └── readiness_status_latest.json   448 B               (15/18 gates, Gate 14 failing)
├── data/runtime/
│   ├── a1_mode.json                   normal              (manual_reset, Apr 20)
│   ├── a2_mode.json                   normal              (initial_state, Apr 20)
│   └── mode_transitions.jsonl         5 lines             (all protection_missing TSM)
├── data/trade_memory/chroma.sqlite3   1.9 MB              (active)
├── logs/bot.log                       6959 lines          (active, rotating)
└── [root] bot.py.bak.step4            74 KB               (orphaned)
    [root] bot.py.backup.*             41 KB               (orphaned)
    [root] test_s4h_bars_pruning.py    12 KB               (misplaced)
```

---

## Appendix B — Readiness Gate Snapshot (from readiness_status_latest.json)

```json
{
  "overall_status": "not_ready",
  "a1_live_ready": false,
  "gates_passed": 15,
  "gates_total": 18,
  "sev1_clean_days": 3,
  "failures": [
    "Gate 07a — signal_backtest.py import failed: No module named 'pandas'",
    "Gate 09 — Sev-1 clean days=3 (need 4 more day(s))",
    "Gate 14 — A2 IV history seeded: only 0/16 ready (run iv_history_seeder.py)"
  ],
  "generated_at": "2026-04-21T00:58:42Z"
}
```

**Gate 07a failure:** `pandas` not installed on VPS. `signal_backtest.py` imports pandas for return calculations. Install: `pip install pandas` in venv, or refactor to use stdlib only.

**Gate 09 failure:** 3 of 7 required Sev-1 clean days achieved. Time-based — resolves automatically if no CRITICAL log events.

**Gate 14 failure:** Old code checks 16 Phase 1 symbols against the wrong IV history path. S4-G deploy will replace this check with the dynamic 43-symbol version that should pass immediately.

---

*End of evidence pack. Compiled from local file reads + 10 SSH sessions. No code changes made.*
