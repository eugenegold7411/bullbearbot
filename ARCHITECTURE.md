# BullBearBot — Architecture

> This document summarizes the architecture. For complete operational context, server details,
> environment variables, and bug history, read `CLAUDE.md`.

---

## System Overview

BullBearBot is an autonomous AI trading bot. It runs a continuous loop on a VPS, reads live
market data, runs multi-stage Claude calls, executes trades, and manages positions.

Two independent accounts run in parallel:

| Account | Scope | Pipeline |
|---------|-------|----------|
| **A1** | Equities, ETFs, crypto (BTC/ETH) | Four-stage Claude pipeline |
| **A2** | Options only | IV-first selection → four-way debate → options builder |

Both accounts trade Alpaca Paper endpoints only.

---

## Account 1 — Four-Stage Pipeline

Each `run_cycle()` in `bot.py` executes this pipeline:

```
Stage 0 — Pre-Cycle Infrastructure (no Claude)
  ├── fetch account state + open positions (Alpaca)
  ├── drawdown guard: >20% from peak → halt
  ├── market_data.fetch_all() → price bars, VIX, news, ORB levels
  ├── exit_manager.run_exit_manager() → audit stops, trail profits
  ├── portfolio_intelligence.build_portfolio_intelligence()
  │     → thesis scores, correlation matrix, forced exits, dynamic sizing
  └── macro_intelligence.build_macro_backdrop_section()
        → rates (2y/10y/30y), commodities, credit stress, Citrini, geopolitics

Stage 1 — Regime Classifier (claude-haiku-4-5-20251001)
  ├── Input: VIX, macro wire, economic calendar, global indices
  └── Output: regime_score (0–100), bias, session_theme, macro_regime,
              commodity_trend, dollar_trend, credit_stress

Stage 2 — Signal Scorer (claude-haiku-4-5-20251001)
  ├── Input: watchlist symbols (prioritized subset, max 35), market data signals,
  │         momentum, EMA9/EMA21, volume ratios
  └── Output: per-symbol score + confidence + direction + catalyst

Stage 2.5 — Pre-Decision Scratchpad (claude-haiku-4-5-20251001)
  ├── Market hours only (skipped extended/overnight)
  ├── Output: watching[], blocking[], triggers[], conviction_ranking[], summary
  ├── Hot memory: rolling 20 scratchpads in data/memory/hot_scratchpads.json
  └── Cold memory: ChromaDB three-tier (scratchpad_scenarios_short/medium/long)

Stage 3 — Main Decision (claude-sonnet-4-6)
  ├── Gate (sonnet_gate.py): skips Sonnet if no material state change
  │     Cooldown: 15 min · Max consecutive skips: 8
  │     Hard overrides: halt regime, CRITICAL recon action, max_skip_exceeded
  ├── Prompt routing:
  │     COMPACT (~1,500 tokens): low-information cycles
  │     FULL (~3,500 tokens): new catalyst, signal spike, deadline, etc.
  └── Output: ClaudeDecision JSON — ideas[], holds[], regime_view, reasoning, concerns
              Intent-based: no qty/stops/order_type (risk kernel resolves these)

Stage 4 — Execution
  ├── risk_kernel.py → validates ClaudeDecision → BrokerAction list
  ├── order_executor.execute_all() → submit to Alpaca
  ├── exit_manager (post-execution stop refresh)
  └── trade_publisher (tweet generation → approval mode)
```

**Cycle intervals:**

| Session | Time (ET) | Interval | Instruments |
|---------|-----------|----------|-------------|
| market | 9:30 AM – 8:00 PM | 5 min | Stocks, ETFs, crypto |
| extended | 4:00–9:30 AM, 8–11 PM | 15 min | Crypto only |
| overnight | 11 PM – 4 AM, weekends | 30 min | BTC/USD, ETH/USD |

---

## Risk Kernel / Executor Relationship

**`risk_kernel.py` is the sole authoritative source for all risk policy:**

- Position sizing tiers: core ≤15%, dynamic ≤8%, intraday ≤5%
- Max total exposure cap
- Stop-loss widths by tier and instrument class
- PDT floor ($26K), VIX halt (>35), drawdown halt (>20%)
- Session eligibility rules
- Crypto-specific rules (wider stops, GTC orders, no PDT)

`order_executor.py` is a **backstop validator** — it checks Alpaca API constraints (price
sanity, share availability) and handles order mechanics. It does not own risk limits.
Policy belongs in `risk_kernel.py`, not `order_executor.py`. See `docs/policy_ownership_map.md`.

Claude's output (`ClaudeDecision`) is **intent-based** — it says what to do, not how many
shares or where to set stops. The risk kernel translates intent → concrete `BrokerAction`
with proper sizing, stops, and order type before anything reaches the executor.

---

## Account 2 — Options Bot

Runs 90 seconds after every Account 1 market-hours cycle.

```
Stage 0 — Reconciliation (BEFORE new proposals)
  ├── Load open structures from data/account2/positions/structures.json
  ├── Build broker snapshot (positions + open orders from Alpaca A2)
  ├── reconcile_options_structures() → INTACT / BROKEN / EXPIRING / NEEDS_CLOSE / ORPHANED
  ├── plan_structure_repair() → priority-ordered repair actions
  └── execute_reconciliation_plan() → close/repair as needed

1. Equity floor check ($25K) + observation mode state
2. Load A1's last signal scores (data/market/signal_scores.json, ≤10 min stale)
3. Build IV summaries for scored symbols (options_data.get_iv_summary())
4. options_intelligence.select_options_strategy() → StructureProposal
5. Four-way Claude debate (Bull / Bear / IV Analyst / Synthesis)
6. options_builder.build_structure() → OptionsStructure (real chain: strikes, expiry, contracts)
7. options_state.save_structure() → persist with lifecycle=PROPOSED
8. order_executor_options → options_executor.submit_structure() → Alpaca (sequential legs, GTC limit)
9. Close-check loop: options_executor.should_close_structure() per open structure
```

**IV-first strategy selection:**

| IV Rank | Environment | Strategy |
|---------|-------------|----------|
| < 15 | very_cheap | Buy ATM call/put |
| 15–35 | cheap | ATM debit spread (2–3 week expiry) |
| 35–65 | neutral | Debit or credit spread |
| 65–80 | expensive | OTM credit spread (sell premium) |
| > 80 | very_expensive | Avoid new positions |

**Hard rules:** limit orders only · delta ≥ 0.30 · DTE ≥ 5 · no crypto options · no options when equity < $25K · scale 50% when VIX > 25, IV rank > 60, or earnings within 48h.

**Observation mode:** First 20 trading days — debates run but orders are logged as `status="observation"` and not submitted (IV history builds).

---

## Production vs Shadow vs Lab

Feature flags in `strategy_config.json` enforce a three-ring model:

| Ring | Flag prefix | Purpose |
|------|-------------|---------|
| **prod** | `feature_flags` | Live pipeline features, trading enabled |
| **shadow** | `shadow_flags` | Counterfactual / observation only, zero execution side effects |
| **lab** | `lab_flags` | Experimental, may be incomplete |

Key shadow modules: `context_compiler.py` (prompt compressor), `shadow_lane.py` (counterfactual
decision log), `divergence.py` (fill/protection divergence tracker).

Divergence operating mode ladder: `NORMAL → RECONCILE_ONLY → RISK_CONTAINMENT → HALTED`.
Mode state persisted at `data/runtime/a1_mode.json` / `data/runtime/a2_mode.json`.

Never import shadow modules into the prod pipeline. Never execute orders from lab code.

---

## 11-Agent Weekly Review

Runs Sundays via `weekly_review.py`. Three phases:

**Phase 1 — Batch API (50% discount, parallel):** Agents 1–4 (Quant Analyst, Risk Manager,
Execution Engineer, Backtest Analyst) → Sonnet

**Phase 2 — Sequential + parallel:** Agent 5 CTO → Agent 6 Strategy Director draft →
Agents 7–10 parallel (Market Intelligence Researcher, CFO, PM, Compliance/Risk Auditor) →
Agent 11 Narrative Director

**Phase 3:** Agent 6 Strategy Director re-runs with all 11 reports → updates `strategy_config.json`

Side effects: updates `strategy_config.json`, sends SMS summary, writes
`data/reports/weekly_review_YYYY-MM-DD.md`, updates `data/roadmap/features.json`.

---

## Major File / Surface Map

### Core Bot

| File | Role |
|------|------|
| `bot.py` | A1 main loop. `run_cycle()` = full four-stage pipeline. |
| `bot_options.py` | A2 options cycle. Reads A1 signals, runs four-way debate. |
| `scheduler.py` | 24/7 session manager. Runs A1+A2 cycles and all maintenance jobs. |
| `risk_kernel.py` | **Sole authoritative source for all risk policy.** Translates ClaudeDecision → BrokerAction. |
| `order_executor.py` | A1 Alpaca order submission. Backstop validation, PDT guard, price sanity. |
| `exit_manager.py` | Stop auditing, trail-to-breakeven, stop refresh. |
| `reconciliation.py` | A1 position reconciliation and deadline exit enforcement. |

### Intelligence Stack

| File | Role |
|------|------|
| `market_data.py` | Live prices, VIX, bars, news. Cache-first via data_warehouse. |
| `data_warehouse.py` | 4 AM batch refresh: bars, fundamentals, news, sector perf, global indices. |
| `macro_wire.py` | Reuters/AP RSS → keyword score → Haiku classifier. 3-tier storage. |
| `macro_intelligence.py` | Persistent macro backdrop: rates, commodities, credit stress, Citrini. 1h cache. |
| `morning_brief.py` | 4:15 AM conviction brief (3–5 trade ideas). Injected into all market cycles. |
| `scanner.py` | 4 AM pre-market + 4:30 AM ORB scan. Promotes DYNAMIC candidates. |
| `earnings_intel.py` | SEC EDGAR 8-K → Claude analysis. Activates within 3 days of earnings. |
| `insider_intelligence.py` | Congressional trades + SEC Form 4 insider buys. |
| `reddit_sentiment.py` | Reddit/WSB mention frequency + sentiment (credentials pending F013). |
| `portfolio_intelligence.py` | Thesis scoring, correlation matrix, dynamic sizing, forced exits. |
| `sonnet_gate.py` | State-change gate for Stage 3. Controls when Sonnet fires. |

### Options Stack (Account 2)

| File | Role |
|------|------|
| `options_data.py` | IV history (252-day rolling), chain fetching, IV rank/percentile/environment. |
| `options_intelligence.py` | IV-first strategy selector → StructureProposal (no strikes/expiry/contracts). |
| `options_builder.py` | Real-chain structure builder. Resolves strikes, expiry, contracts. |
| `options_executor.py` | Pure Alpaca broker adapter. Sequential leg submission, close/roll logic. |
| `options_state.py` | Persistence for OptionsStructure (atomic writes to structures.json). |
| `order_executor_options.py` | Thin A2 wrapper: equity floor + obs mode gate → delegates to options_executor. |

### Memory & Learning

| File | Role |
|------|------|
| `memory.py` | A1 decision log, performance tracking, pattern watchlist. |
| `trade_memory.py` | ChromaDB vector store (3 tiers: recent/medium/long). |
| `watchlist_manager.py` | 3-tier watchlist: core/dynamic/intraday. Prunes stale entries. |
| `attribution.py` | PnL attribution and module ROI. Logs to `data/analytics/attribution_log.jsonl`. |
| `divergence.py` | Fill drift + protection gap detection. Operating mode ladder. |
| `decision_outcomes.py` | Per-decision outcome log with forward returns and alpha classification. |
| `signal_backtest.py` | Signal-level forward-return backtest (+1d/+3d/+5d windows). |
| `shadow_lane.py` | Counterfactual decision log (zero execution side effects). |

### Epic 1 Shared Substrate

| File | Role |
|------|------|
| `schemas.py` | Canonical dataclasses + enums for all structured data. |
| `semantic_labels.py` | Canonical taxonomy enums (LOCKED to taxonomy_v1.0.0.md). |
| `feature_flags.py` | Canonical flag reader merging feature/shadow/lab flags. |
| `versioning.py` | Schema versioning: detect, migrate, backup. |
| `cost_attribution.py` | Cost attribution spine JSONL. |
| `abstention.py` | Universal abstention contract. |
| `hindsight.py` | HindsightRecord JSONL store. |
| `recommendation_store.py` | Director recommendation persistence. |
| `incident_schema.py` | Incident record JSONL store. |
| `model_tiering.py` | Model tier declarations and escalation predicates. |
| `context_compiler.py` | **Shadow only.** Haiku prompt compressor. |

### Communication & Reporting

| File | Role |
|------|------|
| `trade_publisher.py` | Post generator for @BullBearBotAI (approval mode). |
| `report.py` | HTML performance report via email. |
| `weekly_review.py` | 11-agent weekly review. |
| `cost_tracker.py` | Real-time Claude API cost monitoring. |
| `validate_config.py` | Preflight health check — all gates. Writes `data/reports/readiness_status_latest.json`. |

### Prompts

| File | Role |
|------|------|
| `prompts/system_v1.txt` | A1 system prompt (~250 lines). |
| `prompts/user_template_v1.txt` | FULL cycle prompt (~3,500 tokens). |
| `prompts/compact_template.txt` | COMPACT cycle prompt (~1,500 tokens). |
| `prompts/system_options_v1.txt` | A2 system prompt. |

### Watchlists

| File | Contents |
|------|---------|
| `watchlist_core.json` | 41 core symbols (static, manually curated) |
| `watchlist_dynamic.json` | Scanner-promoted DYNAMIC symbols (same-day) |
| `watchlist_intraday.json` | INTRADAY symbols (intraday only) |

### Key Data Paths

```
data/
├── account2/positions/structures.json   # open options structures
├── analytics/attribution_log.jsonl     # module ROI events
├── analytics/decision_outcomes.jsonl   # per-decision outcome log
├── analytics/divergence_log.jsonl      # divergence events
├── analytics/cost_attribution_spine.jsonl
├── market/signal_scores.json           # A1 → A2 signal handoff (≤10 min stale)
├── market/gate_state.json              # Stage 3 gate state
├── options/iv_history/                 # per-symbol IV history (252-day rolling)
├── reports/readiness_status_latest.json
├── reports/weekly_review_YYYY-MM-DD.md
├── runtime/a1_mode.json / a2_mode.json # divergence operating mode
└── trade_memory/                       # ChromaDB vector store
memory/
├── decisions.json    # rolling A1 decision history (last 500)
└── performance.json  # win/loss stats
```

---

## Cost Profile (reference, as of launch)

- ~$10/day at launch (day 2). Target: ~$100/month.
- Dominant cost: `ask_claude` (main Sonnet call, ~74% of daily spend).
- Haiku handles regime + signals + scratchpad.
- Prompt caching (ephemeral, 5-min TTL) active on all Claude calls.
