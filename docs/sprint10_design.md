# Sprint 10 Design Reference — A2 Options Pipeline Deep Dive

**Generated:** 2026-04-29  
**Purpose:** Pre-implementation research for Sprint 10 scope definition  
**Status:** Read-only investigation — no code was modified  

---

## Table of Contents

1. [A2 System Prompt Analysis](#1-a2-system-prompt-analysis)
2. [Full A2 Decision Pipeline](#2-full-a2-decision-pipeline)
3. [Data Availability Audit](#3-data-availability-audit)
4. [Earnings Calendar and Blackout Logic](#4-earnings-calendar-and-blackout-logic)
5. [Strategy Gap Analysis](#5-strategy-gap-analysis)
6. [Cross-Bot Integration](#6-cross-bot-integration)
7. [IV Lifecycle Routing Design](#7-iv-lifecycle-routing-design)
8. [Sprint 10 Readiness Assessment](#8-sprint-10-readiness-assessment)

---

## 1. A2 System Prompt Analysis

**File:** `prompts/system_options_v1.txt` (335 lines)

### Full Text Summary

The system prompt (V2) establishes A2 as a growth-oriented, defined-risk options agent that prioritizes IV regime fit above direction. It runs a four-role bounded adjudication debate on pre-built candidate structures.

### Sections and Coverage

#### Strategy Selection Logic
The prompt defines a five-tier IV-first hierarchy:
- IV Rank < 15 (very_cheap): buy premium aggressively — single-leg ATM call/put or ATM debit spread
- IV Rank 15–35 (cheap): buy premium with structure — ATM debit spread preferred
- IV Rank 35–65 (neutral): spreads preferred — debit or credit based on thesis quality
- IV Rank 65–80 (expensive): sell premium — OTM credit spread, defined-risk
- IV Rank > 80 (very_expensive): avoid buying; sell premium only if catalyst clear, structure high-quality

**Straddle mention in expiry targets section:** The system prompt has this line under EXPIRY TARGETS:
> Straddle on binary event: 14–28 days only if event sensitivity clearly justifies it

**This references straddles as a valid strategy**, but the builder (`options_builder.py`) returns `(None, "not yet supported")` for any strategy not in `_PHASE1_STRATEGIES`. The debate can select a straddle from a router-built candidate, but the builder cannot build one. This is a live inconsistency.

#### IV Environment Guidance
Well-aligned with codebase. The DECISION PRIORITY STACK correctly places hard risk constraints above IV regime, which matches the router's gate ordering (blackout → liquidity → IV env).

#### Earnings Handling
The prompt addresses earnings in two places:
1. HARD RULES: "Scale all sizes down 50% when... earnings within 48h"
2. CATALYSTS section: "Near earnings: prefer reduced size or better-defined structure. Single-legs through earnings are strongly disfavored. Spreads are preferred when event uncertainty is high."

**Gap:** The prompt has no concept of pre-print vs post-print. It treats "near earnings" as a single state. The router also has no concept of post-earnings IV crush as a sell-premium opportunity trigger. This is a key Sprint 10 design question.

#### Confidence Scoring Guidance
- 0.85–1.00: high conviction, required for live trading
- 0.75–0.84: valid and tradable in paper mode
- 0.65–0.74: marginal, paper mode only
- < 0.65: reject

**Codebase alignment:** `paper_confidence_floor = 0.75` in strategy_config.json. The debate returns a `recommended_size_modifier` of 0.5 for VIX > 25, IV rank > 60, earnings within 48h, or macro urgency high.

#### Exit Criteria
The system prompt has NO explicit exit guidance. Exit rules live entirely in `options_executor.should_close_structure()`:
- Rule 1: CANCELLED lifecycle → close broken structure
- Rule 3: force_close_structures config list → manual close
- Rule 4: DTE ≤ 2 → expiry_approaching
- Rule 4a: time-stop at 40% elapsed DTE (single legs) or 50% (debit spreads)
- Rule 4b: IV crush (only when auto_close_on_crush=true)
- Rule 5: loss ≥ 50% of max_risk → stop_loss_hit
- Rule 6: gain ≥ 80% of max_profit → target_profit_hit

This is a prompt gap — the debate has no exit context for open positions.

#### Position Sizing
System prompt states:
- Core spread: max 5% equity
- Core single-leg: max 3% equity
- Dynamic: max 3% equity
- Scale 50% when VIX > 25, IV rank > 60, earnings within 48h, macro urgency high

**Codebase alignment:** strategy_config.json has `core_spread_max_pct=0.06` (6%) — slightly larger than prompt's 5%. The hard cap in `options_builder.size_contracts()` is 5% equity regardless of config. Minor inconsistency: prompt says 5%, config says 6%, builder enforces 5%.

### Flags and Inconsistencies

| Issue | Location | Severity |
|-------|----------|----------|
| Straddle referenced in EXPIRY TARGETS but builder returns "not yet supported" | system_options_v1.txt line 223 vs options_builder.py:148 | Medium |
| `_sell_premium_strategy()` uses `equity * 0.04` but system prompt says 5% | options_intelligence.py:232 | Low |
| Core spread max_pct = 0.06 in config but 0.05 in system prompt | strategy_config.json:128 vs prompt line 195 | Low |
| No exit criteria visible to debate agent | system_options_v1.txt | Medium |
| No pre-print vs post-print earnings distinction | system_options_v1.txt | High (Sprint 10 target) |
| Iron Condor, Strangle, Iron Butterfly mentioned nowhere | system_options_v1.txt | Missing (Sprint 10 target) |

---

## 2. Full A2 Decision Pipeline

### Stage 0: Scheduler Trigger

**File:** `scheduler.py`, lines 1550–1571  
**Trigger:** Every `market` or `pre_open` session cycle, 90 seconds after Account 1's `bot.run_cycle()` completes.

```python
# scheduler.py ~1553
if session in ("market", "pre_open") and _is_claude_trading_window(cfg=...):
    time.sleep(90)
    if _is_claude_trading_window(cfg=...):
        bot_options.run_options_cycle(session_tier=session)
```

**Inputs:** session_tier (market/pre_open), next_cycle_time  
**Gate:** `_is_claude_trading_window()` checks `trading_window_start_et=09:25` and `trading_window_end_et=16:15` ET  
**Frequency:** Every 5 minutes during market session (same cadence as A1, 90s offset)  
**Off-hours close check:** `_maybe_run_options_close_check()` runs outside the window for portfolio maintenance (no new entries, just close-check on open structures)

### Stage 0: Preflight

**File:** `bot_options_stage0_preflight.py`, function `run_a2_preflight()`  
**Inputs:** session_tier, alpaca_client (A2 credentials)  
**Outputs:** `A2PreflightResult` dataclass

Checks in order:
1. Session gate: only `market` or `pre_market` proceed; others halt with `session_not_market`
2. Account equity: fetch `account.equity` and `account.cash`; halt if equity < $25,000
3. Preflight verdict: calls `preflight.run_preflight(caller="run_options_cycle")` — may return halt/reconcile_only/shadow_only/go_degraded
4. A2 operating mode: loads divergence mode from `data/runtime/a2_mode.json`
5. Options structure reconciliation: loads open structures, builds BrokerSnapshot, runs `reconcile_options_structures()` → `plan_structure_repair()` → `execute_reconciliation_plan()`
6. Pending mleg guard: identifies any SUBMITTED structures to block re-submission of same underlying

**Key output fields:**
- `halt: bool` — if True, cycle aborts immediately
- `equity: float`
- `pf_allow_new_entries: bool` — False if preflight returns reconcile_only
- `pf_allow_live_orders: bool` — False if preflight returns shadow_only
- `pending_underlyings: frozenset[str]` — symbols with active mleg order in flight

### Stage 0 (continued): Orchestrator setup

**File:** `bot_options.py`, function `run_options_cycle()`

After preflight:
- Reads VIX from `data/market/vix_cache.json` (fallback: 20.0)
- Loads A1 last decision and A1 regime
- Loads A1 signal scores from `data/market/signal_scores.json` (must be < 10 min old)
- If no signal scores: early exit with `no_signal_scores`
- Fetches IV summaries for all scored symbols
- Initializes options universe via `options_universe_manager` (non-fatal)
- Loads strategy_config.json
- Injects `_pending_underlyings` into config

### Stage 1: Candidates

**File:** `bot_options_stage1_candidates.py`, function `run_candidate_stage()`  
**Inputs:** signal_scores (dict), iv_summaries (dict), equity, vix, equity_symbols, config  
**Outputs:** (candidate_sets, proposals, allowed_by_sym, all_candidate_structures)

**Candidate filtering:**
1. Filter signal_scores to symbols with conviction == "medium" or "high"
2. Sort by conviction (high first), take top 8
3. For each symbol: skip if in `_pending_underlyings`
4. Universe tradeable gate via `options_universe_manager.is_tradeable()`
5. Skip if current_price ≤ 0
6. Fetch options chain (15-min cache via `options_data.fetch_options_chain()`)
7. Build `A2FeaturePack` with all metadata
8. Run `_route_strategy()` — deterministic gate; skip if returns empty list
9. Build `A2CandidateSet` via `build_candidate_set()` → calls `build_candidate_structures()` → calls `options_intelligence.generate_candidate_structures()`
10. Build `StructureProposal` via `options_intelligence.select_options_strategy()` (legacy path)
11. Pre-debate liquidity pre-screen via `_quick_liquidity_check()`

**Each candidate carries at this point:**
- candidate_id (UUID prefix)
- structure_type (long_call / debit_call_spread / etc.)
- symbol, expiry (YYYY-MM-DD), long_strike, short_strike
- contracts, debit (net cost/credit per share), max_loss (USD), max_gain (USD or None)
- breakeven
- delta, theta, vega (from chain data if available, else None — enriched from Alpaca snapshot fallback)
- probability_profit, expected_value
- liquidity_score, bid_ask_spread_pct, open_interest
- dte

### Stage 2: Strategy Routing and Veto

**File:** `bot_options_stage2_structures.py`

#### `_route_strategy(pack, config)` — full decision tree

```
pack = A2FeaturePack with: symbol, iv_rank, iv_environment, a1_direction,
       earnings_days_away, liquidity_score, macro_event_flag

Config reads from config["a2_router"] (current live values):
  earnings_dte_blackout = 2   ← (configured in strategy_config.json)
  earnings_dte_window   = 14  ← (hardcoded default, not in config file)
  earnings_iv_rank_gate = 70  ← (hardcoded default, not in config file)
  min_liquidity_score   = 0.25
  macro_iv_gate_rank    = 70  (NOTE: a2_router in config has this key)
  iv_env_blackout       = []

RULE1: earnings_days_away <= 2                   → [] (no trade)
RULE2: iv_environment in iv_env_blackout         → [] (currently iv_env_blackout=[])
RULE3: liquidity_score < 0.25                    → [] (no trade)
RULE4: macro_event_flag AND iv_rank > 70         → [] (no trade)

RULE_EARNINGS: 2 < earnings_days_away <= 14 AND iv_rank < 70:
  bullish  → ["debit_call_spread", "straddle"]
  bearish  → ["debit_put_spread", "straddle"]
  neutral  → ["straddle"]

RULE2_CREDIT: iv_environment == "very_expensive":
  bullish  → ["credit_put_spread"]
  bearish  → ["credit_call_spread"]
  neutral  → ["credit_put_spread", "credit_call_spread"]

RULE5: iv_environment in ("very_cheap", "cheap") AND direction != "neutral":
  → ["long_call", "long_put", "debit_call_spread", "debit_put_spread"]

RULE6: iv_environment == "neutral" AND direction != "neutral":
  → ["debit_call_spread", "debit_put_spread"]

RULE7: iv_environment == "expensive" AND direction != "neutral":
  → ["credit_put_spread", "credit_call_spread", "debit_call_spread", "debit_put_spread"]

RULE8: default (neutral direction, or cheap+neutral direction, etc.)
  → [] (no trade)
```

**Key gap:** `straddle` appears in RULE_EARNINGS output but has no entry in `_STRUCTURE_MAP` in `options_intelligence.py` and no implementation in `options_builder.py`. If the router allows straddle, `generate_candidate_structures()` silently skips it (`_STRUCTURE_MAP.get(struct_name)` returns None → continue).

#### `_apply_veto_rules(candidate, pack, equity, config)` — post-generation filter

Current live veto thresholds (from strategy_config.json):
- V1: bid_ask_spread_pct > 0.18 → reject
- V2: open_interest < 100 → reject  
- V3: |theta| / debit > 0.05 → reject (theta decay rate)
- V4: max_loss > equity × 0.05 → reject ($5,000 on $100K account)
- V5: dte < 5 → reject
- V6: expected_value < 0.0 → reject (negative edge)

### Stage 3: Debate

**File:** `bot_options_stage3_debate.py`, function `run_bounded_debate()` → `run_options_debate()`

**Inputs to Claude (bounded path when candidate_structures exist):**

```
MARKET CONTEXT: VIX, Regime, Account 2 Equity
ACCOUNT 1 AWARENESS: A1 last decision (timestamp, regime, reasoning excerpt, active trades)
IV ENVIRONMENT: per-symbol env + rank + obs flag
CANDIDATE STRUCTURES: for each:
  - candidate_id, structure_type, symbol, expiry, strike(s)
  - Debit, Max loss, Max gain, Breakeven
  - Delta, Theta, EV, DTE, OI
RISK BUDGET: equity × 0.05
ALLOWED ACTIONS: prefer <candidate_id>, ..., reject_all
DEBATE ROLES: Directional Advocate / Vol+Structure Analyst / Tape+Flow Skeptic / Risk Officer
OUTPUT FORMAT: JSON with selected_candidate_id, confidence, key_risks, reasons, recommended_size_modifier, reject
```

**Confidence floor:** 0.75 (paper mode) — from `strategy_config.json account2.paper_confidence_floor`  
**Model:** `claude-sonnet-4-6`  
**System prompt caching:** `cache_control: {type: ephemeral}`

**What Sonnet is asked to evaluate:**
1. Is the underlying thesis directionally real and timely?
2. Which candidate has the best premium geometry for this IV environment?
3. Does tape/flow (represented by flow_imbalance_30m if available) support or challenge?
4. Which candidate best fits risk budget, theta profile, and expiry?
5. Select ONE or reject all. Must name the selected candidate_id.

### Stage 4: Execution

**File:** `bot_options_stage4_execution.py`, function `submit_selected_candidate()`

**Inputs:** decision_record, alpaca_client, candidate_structures, iv_summaries, equity, pf_allow_new_entries, pf_allow_live_orders, obs_mode, a2_mode

**Flow (bounded path):**
1. Check reject flag and confidence floor — if either fails, execution_result = "no_trade"
2. Look up selected candidate dict by candidate_id
3. Divergence mode gate: `is_action_allowed(a2_mode, "enter_long", sym)` 
4. Look up OptionStrategy enum from structure_type via `_STRATEGY_FROM_STRUCTURE`
5. Call `options_builder.build_structure()` with the selected strategy, direction, conviction, iv_rank, max_loss as max_cost_usd
6. Duplicate submission guard: `_is_duplicate_submission()` checks existing structures
7. `options_state.save_structure()` (lifecycle=PROPOSED)
8. `order_executor_options.submit_options_order()` → `options_executor.submit_structure()`
9. Attribution logging

**Then:** `close_check_loop()` runs for all open structures.

### Stage 4 (continued): Order submission

**File:** `options_executor.py`, function `submit_structure()`

- **Single-leg (single_call / single_put):** GTC LimitOrderRequest at mid price, rounded to $0.05 tick
- **Spreads (debit/credit):** Single atomic MLEG order (OrderClass.MLEG, TimeInForce.DAY) at net mid price
- On fill: lifecycle → SUBMITTED, order_id assigned to all legs
- On rejection: lifecycle → REJECTED, reason logged

**Important:** MLEG uses DAY time_in_force. If unfilled at EOD, the order expires and the next cycle re-evaluates.

---

## 3. Data Availability Audit

### Per-Underlying Data

| Data Point | Fetched? | Source | Stored in structure? | In debate prompt? | Used in mechanical rule? |
|---|---|---|---|---|---|
| IV rank (0–100) | Yes | `options_data.compute_iv_rank()` from 252-day rolling history | A2FeaturePack.iv_rank, OptionsStructure.iv_rank | Yes (IV ENVIRONMENT section) | Yes (router: RULE_EARNINGS gate, RULE2_CREDIT) |
| IV percentile | Yes | `options_data.compute_iv_percentile()` | In iv_summary dict, not in FeaturePack | No | No |
| IV environment classification | Yes | `options_data._classify_iv_environment(rank)` | A2FeaturePack.iv_environment | Yes | Yes (all router rules) |
| Current price | Yes | `yfinance.fast_info.last_price` or history | chain["current_price"], A2FeaturePack via signal_data["price"] | No (implicit via candidate strikes) | Yes (strike selection) |
| A1 signal score | Yes | `data/market/signal_scores.json` (< 10min) | A2FeaturePack.a1_signal_score | No (not explicitly, only in IV section) | No (only used for conviction filter: medium/high) |
| A1 signal direction | Yes | signal_scores["direction"] | A2FeaturePack.a1_direction | No (implicit via candidate structure type) | Yes (router rules all use a1_direction) |
| Earnings days away | Yes | `data/market/earnings_calendar.json` | A2FeaturePack.earnings_days_away | No | Yes (RULE1 blackout, RULE_EARNINGS) |
| Historical IV data (days) | Yes | `data/options/iv_history/{SYMBOL}_iv_history.json` | iv_summary["history_days"] | Yes (OBS flag if < 20 days) | Yes (observation_mode gate) |

**IV history status (as of 2026-04-29):** All 43 symbols have ≥ 20 entries. Observation mode is complete.

### Per-Contract Data (from Options Chain via yfinance)

| Data Point | Fetched? | Stored in structure? | In debate prompt? | Used in mechanical rule? |
|---|---|---|---|---|
| Bid | Yes | OptionsLeg.bid | Yes (via debit/max_loss in candidate block) | Yes (mid price calculation for limit orders) |
| Ask | Yes | OptionsLeg.ask | Yes | Yes |
| Mid | Computed (bid+ask)/2 | OptionsLeg.mid | Implicit (debit field) | Yes (limit order pricing) |
| Volume | Yes (yfinance) | OptionsLeg.volume | Yes (OI field shown) | Yes (V2 veto: min OI=100) |
| Open Interest | Yes (yfinance) | OptionsLeg.open_interest | Yes (OI shown in candidate) | Yes (V2 veto: min OI=100; pre-screen: OI≥75) |
| Implied Volatility (contract-level) | Yes (yfinance impliedVolatility) | Not in OptionsLeg (only ATM IV stored in iv_history) | No | No (only underlying ATM IV used) |
| Delta | Sometimes (when yfinance provides delta/theta/gamma columns) | OptionsLeg.delta | Yes (Delta field in candidate) | Yes (min_delta=0.30 for ATM selection) |
| Gamma | Sometimes | Not in OptionsLeg (only in enrichment dict) | No | No |
| Theta | Sometimes | Not in OptionsLeg | Yes (Theta field in candidate) | Yes (V3 veto: theta/debit > 0.05) |
| Vega | Sometimes | Not in OptionsLeg | No (vega in candidate dict but not shown in bounded debate prompt) | No |
| Rho | No (yfinance rarely provides) | Not in OptionsLeg | No | No |
| Strike | Yes | OptionsLeg.strike | Yes | Yes (strike selection logic) |
| Expiry | Yes | OptionsLeg.expiration | Yes | Yes (DTE gates) |
| ITM/ATM/OTM classification | Computed | No — implicit from strike vs spot | No | Implicit (ATM selection uses min abs(strike-spot)) |

**Notes on Greeks availability:**
- yfinance provides delta/theta/gamma columns only when they exist in the chain data; this is not guaranteed
- The builder has a `_enrich_with_greeks()` fallback that fetches from Alpaca snapshot API (`fetch_option_greeks()`) if delta or theta are None on surviving candidates
- Gamma and rho are added to the candidate dict via enrichment but are NOT shown in the bounded debate prompt
- Vega is computed from yfinance but NOT rendered in the debate prompt (present in candidate dict, not in the prompt template in `run_options_debate()`)

**Missing from debate prompt:** vega, gamma, rho, probability_profit (computed but not displayed), expected_value for single legs when delta unavailable.

---

## 4. Earnings Calendar and Blackout Logic

### Data Source and Storage

- **Source:** Alpha Vantage earnings API (primary), fetched by `_maybe_refresh_earnings_calendar_av()` in scheduler
- **File:** `data/market/earnings_calendar.json`
- **Format:** `{"fetched_at": "...", "source": "alphavantage", "calendar": [{"symbol": "AMZN", "earnings_date": "2026-04-29", "timing": "post-market", "eps_estimate": 1.61, ...}]}`
- **How A2 reads it:** `bot_options_stage1_candidates._load_earnings_days_away(symbol)` — reads calendar, finds minimum days until any earnings entry for the symbol with `days >= 0`, returns as `Optional[int]`

### Current Router Earnings Rules

From `_route_strategy()` with live config (`strategy_config.json a2_router.earnings_dte_blackout = 2`):

```
RULE1  (hard blackout): earnings_days_away <= 2
         → return [] (no trade at all)
         
RULE_EARNINGS (near window, low IV): 2 < earnings_days_away <= 14 AND iv_rank < 70
         bullish → ["debit_call_spread", "straddle"]
         bearish → ["debit_put_spread", "straddle"]
         neutral → ["straddle"]
         
# If earnings_days_away is in window BUT iv_rank >= 70:
# Falls through to RULE2_CREDIT / RULE5-8 based on IV environment
# (elevated IV pre-earnings is treated as a regular "expensive" scenario)
```

**Complete logic flow for earnings scenarios:**

| earnings_days_away | iv_rank | direction | Rule fired | Allowed structures |
|---|---|---|---|---|
| 0–2 | any | any | RULE1 | [] (blocked) |
| 3–14 | < 70 | bullish | RULE_EARNINGS | ["debit_call_spread", "straddle"] |
| 3–14 | < 70 | bearish | RULE_EARNINGS | ["debit_put_spread", "straddle"] |
| 3–14 | < 70 | neutral | RULE_EARNINGS | ["straddle"] |
| 3–14 | ≥ 70 | bullish | RULE7 (expensive) or RULE2_CREDIT (very_expensive) | credit spreads |
| 3–14 | ≥ 70 | bearish | RULE7 or RULE2_CREDIT | credit spreads |
| > 14 | any | any | Normal routing (RULE5-8) | Normal allowed structures |

### Config Values (Current Live)

From `strategy_config.json`:
```json
"a2_router": {
  "earnings_dte_blackout": 2,      ← confirmed 2 (was 5 in code defaults before S4-A)
  "min_liquidity_score": 0.25,
  "macro_iv_gate_rank": 70,
  "iv_env_blackout": []
}
```

The `earnings_dte_window` (14) and `earnings_iv_rank_gate` (70) are NOT in the config file — they are hardcoded defaults in `_A2_ROUTER_DEFAULTS` in `bot_options_stage2_structures.py`. They are not overridable from strategy_config.json without code changes.

### Live Earnings Calendar — A2 Universe as of 2026-04-29

| Symbol | Earnings Date | Timing | Days Away | Today's Status |
|---|---|---|---|---|
| AMZN | 2026-04-29 | post-market | 0 | **RULE1 BLOCKED** |
| GOOGL | 2026-04-29 | post-market | 0 | **RULE1 BLOCKED** |
| META | 2026-04-29 | post-market | 0 | **RULE1 BLOCKED** |
| MSFT | 2026-04-29 | post-market | 0 | **RULE1 BLOCKED** |
| BE | 2026-04-28 | post-market | -1 | Stale, no effect |
| AAPL | 2026-04-30 | post-market | 1 | **RULE1 BLOCKED** |
| LLY | 2026-04-30 | pre-market | 1 | **RULE1 BLOCKED** |
| CVX | 2026-05-01 | pre-market | 2 | **RULE1 BLOCKED** (≤ 2) |
| XOM | 2026-05-01 | pre-market | 2 | **RULE1 BLOCKED** (≤ 2) |
| PLTR | 2026-05-04 | post-market | 5 | RULE_EARNINGS window (days 3–14) |
| AMD | 2026-05-05 | post-market | 6 | RULE_EARNINGS window |
| STNG | 2026-05-05 | pre-market | 6 | RULE_EARNINGS window |
| CRWV | 2026-05-07 | post-market | 8 | RULE_EARNINGS window |
| RKT | 2026-05-14 | unknown | 15 | Normal routing (> 14 day window) |
| NVDA | 2026-05-20 | post-market | 21 | Normal routing |
| WMT | 2026-05-21 | pre-market | 22 | Normal routing |
| GS, JPM, JNJ, LMT, RTX | Jul 2026 | various | 76–83 | Normal routing |

**Impact on tomorrow (2026-04-30):** AMZN/GOOGL/META/MSFT will be days_away=1 (still RULE1 blocked). Post-print IV crush opportunity window opens when days_away becomes negative (yesterday) — currently calendar doesn't support negative lookback for post-event scenarios.

### Pre-Print vs Post-Print Gap

**Currently missing:** There is no concept of "post-earnings" in the pipeline. Once earnings_date is in the past (days_away < 0), the calendar entry has no effect — `_load_earnings_days_away()` filters for `days >= 0` only. 

Post-earnings scenarios that should have special handling:
1. **IV crush opportunity (1–3 days after earnings):** IV elevated from pre-earnings spike, now collapsing. Ideal for selling premium via credit spreads or iron condors.
2. **Post-earnings momentum (same day or next day):** Stock makes big move on earnings. Directional debit spread with fresh catalyst.
3. **Post-earnings reset (2–5 days later):** IV normalized, stock settled. Return to normal IV-based routing.

**To add this:** Would require:
1. A "days since earnings" counter (negative days_away) alongside current "days until"
2. A new router rule: `RULE_POST_EARNINGS`: days_since_earnings in [0, 3] AND iv_rank >= earnings_iv_rank_gate → credit spreads (sell the IV crush)
3. `earnings_calendar.json` needs to include past entries (or a separate `recent_earnings.json`)

---

## 5. Strategy Gap Analysis

### Current Builder Phase 1 Strategies (fully implemented)

```python
_PHASE1_STRATEGIES = frozenset({
    OptionStrategy.CALL_DEBIT_SPREAD,
    OptionStrategy.PUT_DEBIT_SPREAD,
    OptionStrategy.CALL_CREDIT_SPREAD,
    OptionStrategy.PUT_CREDIT_SPREAD,
    OptionStrategy.SINGLE_CALL,
    OptionStrategy.SINGLE_PUT,
})
```

### Strategy Readiness Assessment

| Strategy | Router Rule Exists? | Builder Implemented? | Executor Implemented? | Data Available? | Ready to Build? |
|---|---|---|---|---|---|
| Long Call (single_call) | Yes (RULE5) | Yes (PHASE1) | Yes (single-leg GTC) | Yes | **Already live** |
| Long Put (single_put) | Yes (RULE5) | Yes (PHASE1) | Yes | Yes | **Already live** |
| Debit Call Spread | Yes (RULE5/6/7) | Yes (PHASE1) | Yes (MLEG DAY) | Yes | **Already live** |
| Debit Put Spread | Yes (RULE5/6/7) | Yes (PHASE1) | Yes | Yes | **Already live** |
| Credit Call Spread | Yes (RULE7/RULE2_CREDIT) | Yes (PHASE1) | Yes | Yes | **Already live** |
| Credit Put Spread | Yes (RULE7/RULE2_CREDIT) | Yes (PHASE1) | Yes | Yes | **Already live** |
| Long Straddle | Yes (RULE_EARNINGS only) | **No — "not yet supported"** | **No** | Yes (needs 2-leg builder) | No |
| Long Strangle | No | **No** | **No** | Yes | No |
| Iron Condor | No | **No** | **No** | Yes (needs 4-leg builder) | No |
| Iron Butterfly | No | **No** | **No** | Yes (needs 4-leg builder) | No |
| Short Straddle | No | **No** | **No** | Yes | No — naked risk, violates hard rules |
| Short Strangle | No | **No** | **No** | Yes | No — naked risk, violates hard rules |
| Calendar Spread | No | **No** | **No** | Limited (needs 2 expiries) | No |
| Diagonal Spread | No | **No** | **No** | Limited | No |
| Short Put (cash-secured) | No | **No** | **No** | Yes | Possible (defined risk if sized correctly) |
| Covered Call | No | **No** | **No** | No — requires A1 equity positions | No |
| Protective Put | No | **No** | **No** | No — requires A1 equity positions | No |
| Collar | No | **No** | **No** | No — requires A1 equity positions | No |

### What Is Missing for Each Unimplemented Strategy

**Long Straddle:**
- Router: exists (RULE_EARNINGS allows "straddle")
- Builder: needs ATM call + ATM put legs on same expiry; compute_economics needs to handle `net_debit = call_mid + put_mid`, `max_profit = unlimited`; `select_strikes()` needs to return both option types
- Executor: MLEG order with 2 legs (buy call + buy put) — same as credit spread but both buy
- Schema: `OptionStrategy.STRADDLE` already defined in schemas.py
- _STRUCTURE_MAP in options_intelligence.py: missing "straddle" entry
- Complexity: **Medium** — builder is the main work; executor handles MLEG already

**Long Strangle:**
- Router: no rule exists yet
- Builder: ATM call (higher strike) + ATM put (lower strike); similar to straddle but OTM strikes
- Schema: no enum — needs `OptionStrategy.STRANGLE` added
- Complexity: **Medium** — mostly a builder variant of straddle

**Iron Condor:**
- Router: no rule exists
- Builder: 4 legs — short OTM call + long further-OTM call + short OTM put + long further-OTM put; requires `select_strikes()` to handle 4-leg selection; `compute_economics()` needs iron condor P&L math (max profit = net credit; max loss = spread width - credit)
- Executor: MLEG order with 4 legs — supported by Alpaca (mleg accepts N legs); code only constructs 2-leg leg_requests currently
- Schema: no enum — needs `OptionStrategy.IRON_CONDOR` added
- Data: needs both call and put chains simultaneously — currently `select_strikes()` only handles one option_type at a time
- Complexity: **Large** — requires builder refactor for 4-leg selection and economics

**Iron Butterfly:**
- Similar to Iron Condor but ATM short strikes instead of OTM — same complexity class
- Complexity: **Large** (build alongside Iron Condor)

**Short Straddle / Short Strangle:**
- Hard rule violation: "Never sell naked calls or puts" in system_options_v1.txt
- These are naked short positions — not appropriate for A2 even in paper mode
- **Do not build**

**Calendar Spread:**
- Needs 2 different expiry dates from chain — `select_expiry()` only returns one
- Complexity: **Large** — requires calendar-spread specific expiry selection logic, different economics model (net debit = near_mid - far_mid), complex close logic (near leg expires, far remains)

**Diagonal Spread:**
- Similar to calendar with different strikes — same complexity class plus strike selection
- Complexity: **Large**

**Short Put (cash-secured):**
- Defined risk: max loss = strike × contracts × 100 (if stock goes to 0), but practically sized as credit spread
- Could be treated as a 1-leg credit structure with max_loss = (strike - credit_received) × 100
- Router: could add to RULE2_CREDIT for bullish very_expensive scenarios
- Builder: `select_strikes()` picks OTM put below spot; economics: net_debit = -credit, max_profit = credit
- Executor: single-leg GTC sell order — already supported
- Schema: needs `OptionStrategy.SHORT_PUT` added
- Complexity: **Small** — builder variant of existing single-leg logic with reversed side

**Covered Call:**
- Requires A2 to know which equity positions A1 holds and at what size
- This data is not currently shared between A1 and A2 (see Section 6)
- **Blocked by data infrastructure** — cannot build until A1 positions are shared

**Protective Put / Collar:**
- Same dependency as covered call — needs A1 equity positions
- **Blocked by data infrastructure**

---

## 6. Cross-Bot Integration

### Current A1 Visibility in A2

A2 currently reads two A1 data artifacts:

**1. Signal scores** — `data/market/signal_scores.json`
```python
# bot.py ~258 — written after score_signals()
_ss_path.write_text(json.dumps(signal_scores_obj))
```
Format: `{"scored_symbols": {symbol: {"score": int, "conviction": str, "direction": str, "price": float, "tier": str, "primary_catalyst": str, ...}}, "top_3": [...], ...}`

**2. A1 last decision** — `memory/decisions.json` (tries `data/trade_memory/decisions.json` first)
```python
# bot_options_stage1_candidates._load_account1_last_decision()
# Reads last decision (most recent element of list)
# Used only for regime and summary text sent to Sonnet
```
Fields used: `regime`, `actions` (for active trades summary), `reasoning` (truncated to 150 chars)

### What A2 Does NOT See

- **A1 open positions:** A2 does not fetch A1's actual Alpaca positions. It reads the last Claude *decision*, not the actual broker state.
- **A1 position sizes and entry prices:** Not available to A2
- **A1 stop levels or P&L:** Not available
- **A1 pending orders:** Not available

### Impact on Covered Call Eligibility

For covered call to work, A2 needs to know:
1. Which equity symbols A1 currently holds
2. How many shares (to size the call contract qty — 1 contract = 100 shares)
3. The cost basis or current price (to select appropriate strike above cost basis)

**None of this is currently in any shared file.**

### Proposed Data Sharing Mechanism

The minimal addition is a new file written by A1 at the end of each `run_cycle()`:

**File:** `data/market/a1_positions_snapshot.json`

**Written by:** `bot.py` at end of `run_cycle()` after execution completes  
**Read by:** `bot_options_stage1_candidates._load_a1_positions()` (new function)

```json
{
  "written_at": "2026-04-29T14:30:00-04:00",
  "equity": 101180.46,
  "positions": [
    {
      "symbol": "AMZN",
      "qty": 60,
      "market_value": 15300.60,
      "avg_entry_price": 249.78,
      "unrealized_pl": 414.00,
      "side": "long"
    },
    ...
  ]
}
```

**Usage in A2:**
- Covered call eligibility: `positions[symbol].qty >= 100` AND `symbol in scored_symbols`
- Strike selection: above avg_entry_price + minimum profit buffer
- Contract quantity: `int(positions[symbol].qty / 100)`

**Freshness gate:** Only use if `written_at` is within 10 minutes (same as signal_scores freshness gate).

This is the only blocker for covered calls, protective puts, and collars. Everything else (chain data, greeks, expiry selection) already exists.

---

## 7. IV Lifecycle Routing Design

### Current `_route_strategy()` Decision Tree

```
INPUT: A2FeaturePack (symbol, iv_rank, iv_environment, a1_direction, 
                      earnings_days_away, liquidity_score, macro_event_flag)

┌─ RULE1: earnings_days_away <= 2 ─────────────────────────────── [] BLOCKED
│
├─ RULE2: iv_env_blackout contains iv_environment ─────────────── [] BLOCKED
│
├─ RULE3: liquidity_score < 0.25 ──────────────────────────────── [] BLOCKED
│
├─ RULE4: macro_event_flag AND iv_rank > 70 ───────────────────── [] BLOCKED
│
├─ RULE_EARNINGS: 2 < dte <= 14 AND iv_rank < 70
│    ├─ bullish → [debit_call_spread, straddle*]
│    ├─ bearish → [debit_put_spread, straddle*]
│    └─ neutral → [straddle*]
│    (* straddle silently skipped — not in _STRUCTURE_MAP)
│
├─ RULE2_CREDIT: iv_env == very_expensive
│    ├─ bullish → [credit_put_spread]
│    ├─ bearish → [credit_call_spread]
│    └─ neutral → [credit_put_spread, credit_call_spread]
│
├─ RULE5: iv_env in (very_cheap, cheap) AND dir != neutral
│    → [long_call, long_put, debit_call_spread, debit_put_spread]
│
├─ RULE6: iv_env == neutral AND dir != neutral
│    → [debit_call_spread, debit_put_spread]
│
├─ RULE7: iv_env == expensive AND dir != neutral
│    → [credit_put_spread, credit_call_spread, debit_call_spread, debit_put_spread]
│
└─ RULE8: default ─────────────────────────────────────────────── [] NO TRADE
    (covers: neutral direction with cheap/neutral IV, any direction with unknown IV)
```

### Proposed Sprint 10 Decision Tree

The redesigned tree adds three new capability areas:
1. **Straddle/Strangle for pre-event setups** (fix the existing RULE_EARNINGS stub)
2. **Post-earnings IV crush capture** (new RULE_POST_EARNINGS)
3. **Greek-aware leg selection for Iron Condor** (new RULE_NEUTRAL_RANGE)

```
INPUT: A2FeaturePack — unchanged PLUS new fields:
  days_since_earnings: Optional[int]   ← new: negative earnings_days_away
  pre_event_iv_snap: Optional[float]   ← new: snapshot_pre_event_iv result

┌─ RULE1: earnings_days_away <= 2 ─────────────────────────────── [] BLOCKED (unchanged)

├─ RULE_POST_EARNINGS [NEW]: days_since_earnings in [0, 3] AND iv_rank >= 60
│  (post-print: IV elevated from pre-event spike, beginning to crush)
│    ├─ bullish → [credit_put_spread]    (sell puts that are too expensive)
│    ├─ bearish → [credit_call_spread]   (sell calls that are too expensive)
│    └─ neutral → [credit_put_spread, credit_call_spread]  ← Iron Condor territory
│  This is the primary iron condor trigger in Sprint 10.

├─ RULE_EARNINGS: 2 < earnings_days_away <= 14 AND iv_rank < 60 [MODIFIED]
│  (iv_rank gate lowered from 70 → 60 to be more selective pre-event)
│    ├─ bullish → [debit_call_spread, long_straddle]    ← straddle now buildable
│    ├─ bearish → [debit_put_spread, long_straddle]
│    └─ neutral → [long_straddle, long_strangle]        ← strangle added for neutral

├─ RULE_EARNINGS_HIGH_IV [NEW]: 2 < earnings_days_away <= 14 AND iv_rank >= 60
│  (pre-event with elevated IV — don't buy premium, but can sell if very_expensive)
│    → if iv_env == very_expensive: [credit_put_spread, credit_call_spread]
│    → else: [] (sit out — buying into elevated pre-event IV is bad edge)

├─ RULE2_CREDIT: iv_env == very_expensive ─────────────────────── unchanged

├─ RULE_NEUTRAL_RANGE [NEW]: iv_env in (neutral, expensive) AND dir == neutral
│                             AND iv_rank >= 50
│  (undefined direction but elevated IV — range-bound trading thesis)
│    → [iron_condor]   ← requires 4-leg builder
│  This catches the cases RULE8 currently throws away for neutral direction.

├─ RULE5: iv_env in (very_cheap, cheap) AND dir != neutral ──── unchanged + add long_straddle
├─ RULE6: iv_env == neutral AND dir != neutral ──────────────── unchanged
├─ RULE7: iv_env == expensive AND dir != neutral ────────────── unchanged

└─ RULE8: default ──────────────────────────────────────────────── [] NO TRADE
```

### New Data Requirements vs Existing Data

| New Rule | New Data Required | Can Use Existing Data |
|---|---|---|
| RULE_POST_EARNINGS | `days_since_earnings` (negative of earnings_days_away if past) — needs calendar to include past events | iv_rank already available |
| RULE_EARNINGS_HIGH_IV | None new | Uses existing earnings_days_away + iv_rank |
| RULE_NEUTRAL_RANGE (iron condor) | None new | Uses existing iv_environment + iv_rank + direction |
| Long Straddle in RULE_EARNINGS | None new | Needs builder implementation |
| Long Strangle in RULE_EARNINGS neutral | None new | Needs builder implementation |

### New Strategy Implementations Required

| Rule | Strategies to Build | Builder Complexity | Executor Complexity |
|---|---|---|---|
| RULE_EARNINGS (fixed) | long_straddle | Medium (2-leg ATM call+put same expiry) | Low (MLEG buy both) |
| RULE_EARNINGS neutral | long_strangle | Medium (OTM call + OTM put) | Low (MLEG buy both) |
| RULE_POST_EARNINGS | iron_condor | Large (4-leg builder, delta-aware strikes) | Low (MLEG already supports N legs) |
| RULE_NEUTRAL_RANGE | iron_condor | (same as above) | (same) |

---

## 8. Sprint 10 Readiness Assessment

### Ready to Build Now (data exists, just needs implementation)

**1. Long Straddle** — `OptionStrategy.STRADDLE` already in schemas.py

*Implementation path:*
- `options_intelligence.py`: Add `"straddle": OptionStrategy.STRADDLE` to `_STRUCTURE_MAP`; add `_DTE_RANGE[OptionStrategy.STRADDLE] = (14, 28)`
- `options_builder.py`: Add STRADDLE to `_PHASE1_STRATEGIES`; extend `select_strikes()` to handle straddle (returns both call and put ATM leg at same strike); update `compute_economics()` for straddle (net_debit = call_mid + put_mid, max_profit = None/unlimited, max_loss = net_debit)
- `options_builder.build_legs()`: Build 2 buy legs (one call, one put) at same strike
- `options_executor.py`: Add STRADDLE to `_PHASE1_STRATEGIES`; `_submit_spread_mleg()` already handles N-leg orders — straddle is just 2 buy legs
- `bot_options_stage2_structures.py`: Add "straddle" to `_STRUCTURE_MAP`
- Estimated complexity: **Medium (2–3 days)**

**2. Long Strangle** — needs new schema entry

*Implementation path:*
- `schemas.py`: Add `OptionStrategy.STRANGLE = "long_strangle"` 
- Same builder pattern as straddle but OTM strikes: call strike slightly above spot, put strike slightly below spot (1 strike OTM each direction)
- Router: Add "long_strangle" to RULE_EARNINGS neutral outcome
- Estimated complexity: **Small (1 day)** — build alongside straddle

**3. RULE_EARNINGS_HIGH_IV gate** — config + router only, no new strategy

*Implementation path:*
- `bot_options_stage2_structures.py`: Split existing `RULE_EARNINGS` into two branches based on `iv_rank` vs `earnings_iv_rank_gate` (already in code as a single check — just expose the else branch as explicit RULE_EARNINGS_HIGH_IV logic)
- Add `earnings_iv_rank_gate` and `earnings_dte_window` to strategy_config.json's `a2_router` block so they're config-driven (currently hardcoded defaults)
- Estimated complexity: **Small (< 1 day)**

**4. Short Put (cash-secured)** — minor builder addition

*Implementation path:*
- `schemas.py`: Add `OptionStrategy.SHORT_PUT = "short_put"`
- `options_builder.py`: Handle SHORT_PUT — single sell leg OTM put below spot, economics: net credit received = put_mid, max_profit = credit × contracts × 100, max_loss = (strike − credit) × contracts × 100
- `options_executor.py`: Single-leg GTC sell order — already supported by `_submit_single_leg()` pattern
- Router: Add to RULE2_CREDIT for bullish + very_expensive scenario
- System prompt: Add short_put guidance
- Estimated complexity: **Small (1 day)**

### Needs Data Infrastructure First

**5. Iron Condor / Iron Butterfly** — needs 4-leg builder

*What needs to be built first:*
- `options_builder.select_strikes()` needs a 4-leg path: select short OTM call, long further-OTM call, short OTM put, long further-OTM put; or for iron butterfly: short ATM call, long OTM call, short ATM put, long OTM put (same ATM strike for both shorts)
- Delta-aware short strike selection: the iron condor's edge comes from selling the short strikes at a specific delta target (typically 0.15–0.25 delta). This requires greeks from chain data. yfinance provides delta when available, but it's not guaranteed. Alpaca enrichment fallback exists (`_enrich_with_greeks()`) but requires live calls per contract.
- `compute_economics()` for 4-leg structures: max_profit = net_credit; max_loss = spread_width − credit (same on both sides)
- `build_legs()`: 4-leg list instead of 2
- `options_executor._submit_spread_mleg()`: Already iterates over `structure.legs` dynamically — would handle 4 legs without code changes. Only the leg_requests list construction needs to work for N legs (it already does).
- New router rules: RULE_POST_EARNINGS (needs `days_since_earnings`) and RULE_NEUTRAL_RANGE (uses existing data)

*Data infrastructure needed:*
- `days_since_earnings` counter: `_load_earnings_days_away()` currently filters `days >= 0`. Needs to also check entries with past dates and return the most recent past earnings as a negative integer (e.g., -1 means yesterday). Minor change to `_load_earnings_days_away()`.
- `A2FeaturePack`: Add `days_since_earnings: Optional[int]` field to `schemas.py`
- Delta availability for iron condor strike selection: Currently inconsistent — chain data from yfinance sometimes includes delta, sometimes not. `_enrich_with_greeks()` via Alpaca API is the fallback. Iron condor requires delta-targeted strikes, so this enrichment step must be promoted from "optional enhancement" to "required for iron condor builds."

*Estimated complexity:* **Large (1 week+)**

**6. Covered Call / Protective Put / Collar** — needs A1 position sharing

*Blocker:* A1 does not write a positions snapshot that A2 can read. The `data/market/signal_scores.json` handoff only contains signal scores, not actual broker positions.

*Infrastructure needed:*
- A1 (`bot.py`): Write `data/market/a1_positions_snapshot.json` at end of `execute_all()` with current Alpaca positions (symbol, qty, avg_entry_price, market_value, side). Non-fatal, max 10-min freshness gate in A2.
- A2 (`bot_options_stage1_candidates.py`): New `_load_a1_positions()` function with same freshness gate pattern as signal_scores
- A2 (`A2FeaturePack`): Add `a1_held_qty: Optional[int]` and `a1_avg_entry: Optional[float]` fields
- Router: New RULE_COVERED_CALL: a1_held_qty >= 100 AND iv_env in (expensive, very_expensive) AND direction neutral/bearish → ["covered_call"]

*Estimated complexity:* **Medium for infrastructure + Small for strategy build (2–3 days total)**

### Needs Design Decision (Eugene's Input)

**7. Post-Earnings IV Crush (RULE_POST_EARNINGS)**

The design calls for routing to credit spreads or iron condors in the 0–3 days after an earnings print. This requires:
- Calendar must preserve past earnings dates (currently `days >= 0` filter drops them)
- A new config parameter: `post_earnings_iv_sell_window_days` (proposed: 3)
- A new config parameter: `post_earnings_iv_rank_floor` (proposed: 60)

**Open question:** Should this be triggered automatically, or should it require a specific catalyst tag in the A1 signal that says "post-earnings"? The current signal scorer does include earnings catalysts in its output, but A2 doesn't parse the catalyst string for specific patterns.

**Recommendation:** Start with the mechanical calendar-based trigger (days_since_earnings in [0,3] AND iv_rank >= 60). No Claude interpretation needed for the routing decision — the IV rank does the heavy lifting. Claude still adjudicates in the debate.

**8. Iron Condor Strike Selection Delta Target**

For iron condors, the convention is to sell short strikes at approximately 0.15–0.25 delta (roughly 15–25% probability of expiring ITM). This requires:
- Reliable delta data from chain (not always available from yfinance)
- A fallback when delta is absent (use percentage OTM distance instead: e.g., 5–8% OTM from spot)

**Open question:** Should the iron condor builder hard-require delta data (reject if unavailable) or accept a strike-distance fallback?

**Recommendation:** Use delta when available; fall back to 7% OTM distance when delta is None. Document the fallback in the audit_log entry.

**9. Confidence Floor for New Strategies**

New strategies (straddle, strangle, iron condor) in paper mode should have higher confidence requirements initially since the system has no historical performance data on them. 

**Open question:** Should Sprint 10 add per-strategy confidence floors in strategy_config.json?

**Recommendation:** Yes — add `min_confidence_by_strategy` dict to `account2` section. Default to 0.78 for straddle/strangle (slightly above current 0.75 floor) and 0.82 for iron condor (below live 0.85 but above paper floor). These can be lowered as track record builds.

### Estimated Complexity Summary

| Item | Category | Complexity | Prerequisites |
|---|---|---|---|
| Straddle builder + router fix | Ready now | Medium (2–3 days) | None |
| Strangle builder | Ready now | Small (1 day, build with straddle) | None |
| RULE_EARNINGS_HIGH_IV | Ready now | Small (< 1 day) | None |
| Short Put | Ready now | Small (1 day) | None |
| Per-strategy confidence floors (config) | Design decision | Small (< 1 day) | Design input |
| `earnings_dte_window` + `earnings_iv_rank_gate` to config | Ready now | Small (< 1 day) | None |
| Post-earnings calendar lookback | Data infra | Small (< 1 day) | None |
| `days_since_earnings` in FeaturePack | Data infra | Small (< 1 day) | Calendar lookback |
| RULE_POST_EARNINGS | Design decision | Small (< 1 day) | days_since_earnings |
| A1 positions snapshot write | Data infra | Small (< 1 day) | Eugene sign-off |
| Covered call builder | Needs infra | Small (1 day) | A1 positions snapshot |
| Iron condor 4-leg builder | Needs infra | Large (1 week+) | Delta enrichment, design decisions |
| Iron butterfly builder | Needs infra | Medium (2–3 days alongside condor) | Iron condor builder |
| Calendar spread | Needs infra | Large (1 week+) | Multi-expiry chain logic |

### Recommended Sprint 10 Sequence

**Phase 1 — Foundation (no blockers, do first):**
1. Add `earnings_dte_window` and `earnings_iv_rank_gate` to strategy_config.json a2_router block (make them config-driven)
2. Add `RULE_EARNINGS_HIGH_IV` branch to router
3. Add post-earnings calendar lookback to `_load_earnings_days_away()` (return negative integers for past earnings)
4. Add `days_since_earnings` to A2FeaturePack

**Phase 2 — Straddle/Strangle (core Sprint 10 deliverable):**
5. Implement long straddle: schemas → _STRUCTURE_MAP → builder → executor
6. Implement long strangle alongside straddle
7. Add RULE_POST_EARNINGS to router
8. Update system_options_v1.txt to describe straddle/strangle/post-earnings logic

**Phase 3 — Short Put (small win):**
9. Short put: schemas → builder → router → system prompt

**Phase 4 — Infrastructure for covered call:**
10. A1 position snapshot write (bot.py)
11. A2 position reader (stage1_candidates)
12. Covered call builder + router + system prompt

**Phase 5 — Iron Condor (large, may extend to Sprint 11):**
13. Validate delta data availability across A2 universe
14. Iron condor 4-leg builder with delta-targeted strikes
15. RULE_NEUTRAL_RANGE router rule
16. System prompt update for iron condor adjudication guidance
17. Iron butterfly (if condor lands well)

---

## Appendix A: Key File Locations

| File | Purpose |
|------|---------|
| `/Users/eugene.gold/trading-bot/prompts/system_options_v1.txt` | A2 system prompt |
| `/Users/eugene.gold/trading-bot/bot_options.py` | Thin orchestrator, re-exports |
| `/Users/eugene.gold/trading-bot/bot_options_stage0_preflight.py` | Preflight, obs mode, reconciliation |
| `/Users/eugene.gold/trading-bot/bot_options_stage1_candidates.py` | Signal loading, IV summaries, FeaturePack, candidate assembly |
| `/Users/eugene.gold/trading-bot/bot_options_stage2_structures.py` | `_route_strategy()`, `_apply_veto_rules()`, `build_candidate_structures()` |
| `/Users/eugene.gold/trading-bot/bot_options_stage3_debate.py` | Bounded debate, Claude call, prompt assembly |
| `/Users/eugene.gold/trading-bot/bot_options_stage4_execution.py` | Execution, close-check, persistence |
| `/Users/eugene.gold/trading-bot/options_builder.py` | Real-chain structure builder (Phase 1 strategies only) |
| `/Users/eugene.gold/trading-bot/options_executor.py` | Alpaca broker adapter |
| `/Users/eugene.gold/trading-bot/options_intelligence.py` | Strategy selector, `generate_candidate_structures()` |
| `/Users/eugene.gold/trading-bot/options_data.py` | IV history, chain fetch, Greeks fetch |
| `/Users/eugene.gold/trading-bot/schemas.py` | `OptionStrategy` enum, `A2FeaturePack`, `A2CandidateSet`, `OptionsStructure` |
| `/Users/eugene.gold/trading-bot/strategy_config.json` | `a2_router`, `a2_veto_thresholds`, `account2` config sections |
| `data/market/earnings_calendar.json` | Live earnings calendar (Alpha Vantage) |
| `data/market/signal_scores.json` | A1→A2 signal handoff (< 10 min freshness) |
| `data/options/iv_history/{SYMBOL}_iv_history.json` | Per-symbol rolling IV history (252 days max) |
| `data/account2/positions/structures.json` | Open options structures |
| `data/account2/positions/options_log.jsonl` | Execution audit log |

## Appendix B: strategy_config.json A2 Router Live Values

```json
"a2_router": {
  "earnings_dte_blackout": 2,
  "min_liquidity_score": 0.25,
  "macro_iv_gate_rank": 70,
  "iv_env_blackout": []
}
```

Missing from config (hardcoded defaults in `_A2_ROUTER_DEFAULTS`):
- `earnings_dte_window`: 14
- `earnings_iv_rank_gate`: 70

These should be added to strategy_config.json in Sprint 10 Phase 1.

## Appendix C: OptionStrategy Enum — Current vs Needed

**Current `schemas.OptionStrategy`:**
```python
CALL_DEBIT_SPREAD  = "call_debit_spread"   ← live
PUT_DEBIT_SPREAD   = "put_debit_spread"    ← live
CALL_CREDIT_SPREAD = "call_credit_spread"  ← live
PUT_CREDIT_SPREAD  = "put_credit_spread"   ← live
SINGLE_CALL        = "single_call"         ← live
SINGLE_PUT         = "single_put"          ← live
STRADDLE           = "straddle"            ← enum exists, builder stub only
CLOSE_OPTION       = "close_option"        ← internal
```

**Sprint 10 additions needed:**
```python
LONG_STRANGLE      = "long_strangle"       ← Phase 2
IRON_CONDOR        = "iron_condor"         ← Phase 5
IRON_BUTTERFLY     = "iron_butterfly"      ← Phase 5
SHORT_PUT          = "short_put"           ← Phase 3
COVERED_CALL       = "covered_call"        ← Phase 4
```
