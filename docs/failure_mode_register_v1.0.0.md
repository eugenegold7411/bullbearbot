# Failure Mode Register v1.0.0

> **LIVING DOCUMENT** — not LOCKED. Add new failure modes as they are discovered.
> Machine-readable version: `data/status/failure_mode_register.json`.
> Reviewed weekly by Agent 10 (Compliance/Risk Auditor).
> Updated manually after each incident. Status field reflects current mitigation state.

---

## How to Use This Register

Each failure mode has an ID (FM-xxx), severity, status, and review cadence.

**Status values:**
- `active` — known failure mode, mitigations in place but risk remains
- `mitigated` — controls fully address the risk under normal conditions
- `accepted` — risk acknowledged, no further mitigation planned
- `monitoring` — watching for evidence; mitigations not yet needed

**Severity:** `critical` (trading halted or capital lost) | `high` | `medium` | `low`

**Review cadence:** `weekly_review` | `monthly` | `manual`

---

## Category 1: Data Ingestion Failures

### FM-001 — Stale market data served to decision engine
- **Severity:** high
- **Status:** active
- **Triggers:** VPS internet blip, Alpaca API timeout, yfinance rate limit
- **Mitigations:** 4 AM data warehouse pre-fetch; cache-first data loading in market_data.py; all external calls wrapped in try/except
- **Detection:** `[WARN] market data stale` in bot.log; signal scorer gets empty bars dict
- **Review cadence:** weekly_review

### FM-002 — IV history file missing or corrupted for A2 symbol
- **Severity:** medium
- **Status:** active
- **Triggers:** First-run for new symbol; yfinance IV = 0.02 (same-day expiry bug); file write interrupted
- **Mitigations:** iv_history_seeder.py validates entries (iv < 0.05 replaced); BUG-005 fix targets 7-14 DTE for IV history
- **Detection:** IV rank = 0 or IV environment = "unknown" in options_intelligence output
- **Review cadence:** weekly_review

### FM-003 — Citrini positions file stale
- **Severity:** low
- **Status:** accepted
- **Triggers:** New monthly Citrini memo not ingested; positions.json over 30 days old
- **Mitigations:** Agent 8 (CFO) reminds to check for new content every weekly review
- **Detection:** `citrini_positions.json` mtime > 30 days
- **Review cadence:** monthly

---

## Category 2: Model / API Failures

### FM-004 — Claude API timeout causes empty decision
- **Severity:** high
- **Status:** mitigated
- **Triggers:** Anthropic service interruption; rate limit; network timeout
- **Mitigations:** All Claude calls wrapped in try/except with graceful degradation; bot continues cycle with previous state; overnight uses lightweight Haiku call
- **Detection:** `[ERROR] ask_claude failed` in bot.log; `[GATE] SONNET triggered` missing from log
- **Review cadence:** weekly_review

### FM-005 — Batch API returns empty responses for weekly review agents 1-4
- **Severity:** medium
- **Status:** active
- **Triggers:** Temporary Batch API service degradation; batch timeout after 24h
- **Mitigations:** run_weekly_review.py is idempotent (re-run safe); 5-minute retry guidance in runbook
- **Detection:** weekly review report missing agent 1-4 sections
- **Review cadence:** weekly_review

### FM-006 — Model tier escalation triggers cache miss storm
- **Severity:** medium
- **Status:** active
- **Triggers:** Multiple signals triggering `should_escalate_to_premium()` in same session
- **Mitigations:** Escalation only on main_decision; CACHE_INVALIDATION_WARNING documented; cache_hit_input_tokens monitored in spine
- **Detection:** Cost spike in spine; cache_creation_input_tokens high in attribution log
- **Review cadence:** weekly_review

---

## Category 3: Risk Management Failures

### FM-007 — PDT floor check bypassed
- **Severity:** critical
- **Status:** mitigated
- **Triggers:** Code path skips `_check_drawdown()` or risk_kernel; equity calculation uses wrong account
- **Mitigations:** PDT check in both bot.py AND order_executor.py (dual-layer per policy_ownership_map.md); preflight also checks PDT floor
- **Detection:** Trade submitted with equity < $26,000 (impossible under dual-layer check)
- **Review cadence:** weekly_review

### FM-008 — Bracket stop-loss child invisible to exit_manager (BUG-009 recurrence)
- **Severity:** high
- **Status:** mitigated
- **Triggers:** New bracket entry order; Alpaca OCA holds stop child in "held" status
- **Mitigations:** `_has_stop_order()` uses `status=all` query; `tp_only` status triggers stop repair; BUG-009 fixed 2026-04-15
- **Detection:** exit_manager logs `tp_only` status; position without stop_price
- **Review cadence:** weekly_review

### FM-009 — Dynamic/intraday position held overnight
- **Severity:** high
- **Status:** active
- **Triggers:** Exit manager fails to close; reconciliation doesn't catch DYNAMIC overnight; scheduler session changes while position open
- **Mitigations:** reconciliation.py checks DYNAMIC tier positions at session boundary; time_bound_actions in strategy_config.json
- **Detection:** DYNAMIC position in overnight session log; `[RECON]` forced exit log entry
- **Review cadence:** weekly_review

---

## Category 4: Execution Failures

### FM-010 — Market order fails on crypto GTC requirement
- **Severity:** medium
- **Status:** mitigated
- **Triggers:** Crypto order submitted as DAY instead of GTC; Alpaca rejects
- **Mitigations:** order_executor.py hardcodes GTC for symbols containing "/"; BUG-008 fix validates crypto stop prices
- **Detection:** Alpaca rejection with "TimeInForce not valid for asset" error
- **Review cadence:** monthly

### FM-011 — Options structure leg submission fails midway (partial fill)
- **Severity:** high
- **Status:** active
- **Triggers:** Alpaca rejects second leg after first leg fills; liquidity gap between legs
- **Mitigations:** options_executor.py sequential leg submission with poll-and-retry; reconcile_options_structures() detects BROKEN structures; plan_structure_repair() closes broken legs
- **Detection:** structure.lifecycle = BROKEN; options_log.jsonl close_reason_code = "broken_leg"
- **Review cadence:** weekly_review

---

## Category 5: Memory / State Corruption

### FM-012 — decisions.json exceeds 500 entries and causes memory pressure
- **Severity:** low
- **Status:** active
- **Triggers:** decisions.json grows unbounded; rolling limit not enforced
- **Mitigations:** memory.py trims to last 500 decisions on each save
- **Detection:** File size > 2MB; OOM in VPS logs
- **Review cadence:** monthly

### FM-013 — ChromaDB protobuf conflict disables vector memory
- **Severity:** medium
- **Status:** mitigated
- **Triggers:** Protobuf version incompatibility (BUG-011); import outside systemd context
- **Mitigations:** `PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python` in both .env and systemd service; trade_memory degrades gracefully
- **Detection:** `[ChromaDB] disabled for this process` warning in log
- **Review cadence:** monthly

---

## Category 6: Annex / Shadow Module Failures

### FM-014 — Annex module writes to prod data directory
- **Severity:** high
- **Status:** active
- **Triggers:** Developer error: annex module imports data path from wrong module
- **Mitigations:** Annex sandbox contract enforced by convention; all annex modules use `data/annex/{module_name}/` exclusively; lab_flags gate prevents prod execution
- **Detection:** Unexpected writes to data/analytics/ or data/market/ from annex module imports
- **Review cadence:** weekly_review

### FM-015 — Shadow flag enables shadow module in prod pipeline
- **Severity:** high
- **Status:** active
- **Triggers:** Shadow module imported by bot.py or order_executor.py; shadow ring bypassed
- **Mitigations:** All shadow modules have `# SHADOW MODULE — do not import from prod pipeline` header; ring="shadow" in TIER_DECLARATIONS blocks validate_tier_usage()
- **Detection:** `ring=shadow` module appearing in prod spine records
- **Review cadence:** weekly_review

---

## Category 7: Operational / Infrastructure Failures

### FM-016 — VPS disk fills due to log rotation failure
- **Severity:** medium
- **Status:** active
- **Triggers:** Log files not rotated; data warehouse bars accumulating; ChromaDB growth
- **Mitigations:** bot.log uses RotatingFileHandler (5MB, 3 backups); logs/ excluded from rsync
- **Detection:** `df -h` shows >80% disk; VPS monitoring alert
- **Review cadence:** monthly

### FM-017 — Scheduler misses Sunday weekly review
- **Severity:** low
- **Status:** active
- **Triggers:** Service restart during Sunday window; VPS reboot; scheduler crash during weekly review
- **Mitigations:** run_weekly_review.py manual trigger documented in runbook; review is idempotent (re-run safe)
- **Detection:** No `weekly_review_YYYY-MM-DD.md` file for Sunday's date
- **Review cadence:** weekly_review

---

## Category 8: Emergent Behavior Risks

### FM-018 — Claude self-references or breaks fourth wall in trade output
- **Severity:** medium
- **Status:** active
- **Triggers:** System prompt does not explicitly prohibit self-reference; model hallucination
- **Mitigations:** System prompt instructs bot to behave as institutional trading desk, not AI; Agent 10 Compliance audits for behavioral consistency
- **Detection:** Agent 10 flags "self-reference" or "AI" language in output; human review of tweets
- **Review cadence:** weekly_review

### FM-019 — Model confidently recommends position that violates watchlist exclusion
- **Severity:** high
- **Status:** active
- **Triggers:** Newly added symbol not in watchlist; Claude recommends symbol from news but not watchlist; DYNAMIC tier overflow
- **Mitigations:** risk_kernel validates all symbols against watchlist before execution; non-watchlist symbols generate `[RISK] symbol not in watchlist` rejection
- **Detection:** Rejected orders with "symbol not in watchlist" reason in trades.jsonl
- **Review cadence:** weekly_review
