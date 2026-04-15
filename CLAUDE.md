# BullBearBot — Trading Bot Project Brief

> **For new Claude Code sessions.** This document is the complete context handoff.
> Read it fully before touching any file. The codebase has subtle interdependencies
> and an active production scheduler running 24/7.

---

## What This Is

An autonomous AI trading bot running on a cloud VPS that trades Paper accounts on Alpaca.
It runs a continuous loop (5-minute cycles during market hours, 15-minute extended, 30-minute overnight)
and uses Claude as its decision engine. Two independent accounts run in parallel:

- **Account 1** — equities, ETFs, crypto (stocks + BTC/ETH). Main bot.
- **Account 2** — options only, separate Alpaca account, IV-first strategy with four-way debate.

The bot is autonomous: it reads market data, runs multi-stage Claude calls, executes trades,
manages stops, and publishes to @BullBearBotAI (approval mode currently — human copy-pastes).

**Launch date:** 2026-04-13. Currently paper trading only. All accounts use Alpaca paper endpoints.

---

## Server

| Field | Value |
|-------|-------|
| Provider | DigitalOcean VPS |
| IP | 161.35.120.8 |
| OS | Ubuntu 24.04.4 LTS |
| RAM | 2 GB (1.9 GB total, ~550 MB used) |
| Disk | 24 GB (3.6 GB used, 20 GB free) |
| SSH alias | `tradingbot` |
| SSH key | `~/.ssh/trading_bot` |
| User | root |
| Working dir | `/home/trading-bot/` |
| Python | 3.12.3 |
| Virtualenv | `/home/trading-bot/.venv/` |
| Systemd service | `trading-bot.service` (auto-restarts, 30s backoff) |

**SSH config** (at `~/.ssh/config` on local machine):
```
Host tradingbot
    HostName 161.35.120.8
    User root
    IdentityFile ~/.ssh/trading_bot
    ServerAliveInterval 60
```

**Connect:** `ssh tradingbot`

**Local mirror:** `/Users/eugene.gold/trading-bot/` — source of truth for code.
Synced to server via rsync. `.env`, `logs/`, and `data/` are excluded from sync.

**Deploy new code:**
```bash
rsync -avz -e 'ssh -i ~/.ssh/trading_bot' \
  --exclude .venv --exclude __pycache__ --exclude '*.pyc' \
  --exclude .env --exclude logs/ --exclude data/ \
  tradingbot:/home/trading-bot/ /Users/eugene.gold/trading-bot/   # pull first
rsync -avz -e 'ssh -i ~/.ssh/trading_bot' \
  --exclude .venv --exclude __pycache__ --exclude '*.pyc' \
  --exclude .env --exclude 'logs/*.log' --exclude 'logs/*.jsonl' \
  --exclude nohup.out /Users/eugene.gold/trading-bot/ tradingbot:/home/trading-bot/  # push
```

**Single-file edit workflow** (IMPORTANT — Edit tool requires local files):
```bash
scp tradingbot:/home/trading-bot/file.py /tmp/file_edit.py
# Edit /tmp/file_edit.py with Edit tool
scp /tmp/file_edit.py tradingbot:/home/trading-bot/file.py
ssh tradingbot 'cd /home/trading-bot && source .venv/bin/activate && python3 -m py_compile file.py && echo OK'
```

**Service management:**
```bash
ssh tradingbot 'systemctl status trading-bot'
ssh tradingbot 'systemctl restart trading-bot'
ssh tradingbot 'systemctl stop trading-bot'
ssh tradingbot 'journalctl -u trading-bot -f'
ssh tradingbot 'tail -f /home/trading-bot/logs/bot.log'
```

**Run a single cycle manually:**
```bash
ssh tradingbot 'cd /home/trading-bot && source .venv/bin/activate && python3 bot.py'
ssh tradingbot 'cd /home/trading-bot && source .venv/bin/activate && python3 bot_options.py'
```

**Dry-run scheduler:**
```bash
ssh tradingbot 'cd /home/trading-bot && source .venv/bin/activate && python3 scheduler.py --dry-run'
```

---

## Key Package Versions

| Package | Version |
|---------|---------|
| alpaca-py | 0.43.2 |
| anthropic | 0.93.0 |
| yfinance | 1.2.1 |
| chromadb | 1.5.7 |

---

## Account Status (as of 2026-04-14)

### Account 1 — Equities/ETF/Crypto
- **Equity:** $100,428.17
- **Cash:** $81,504.17
- **Buying power:** $181,932 (margin available)
- **Open positions:** 2
  - GLD: 34 shares, market value $15,113, unrealized P&L +$374
  - TSM: 10 shares, market value $3,811, unrealized P&L +$57
  - ⚠️ **TSM must exit by 2026-04-15 15:45 ET** (TSM earnings April 16 — binary event)
- **PDT floor:** $26,000 (hard limit, checked every cycle)
- **Credentials:** `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`
- **Base URL:** `https://paper-api.alpaca.markets`

### Account 2 — Options Only
- **Equity:** $100,000.00 (starting capital, no trades yet)
- **Cash:** $100,000.00
- **Open positions:** 0
- **Mode:** Observation mode complete (bootstrapped 2026-04-14). Live — 20 symbols with IV history.
  `obs_mode_state.json`: `trading_days_observed=20`, `observation_complete=true`
- **Credentials:** `ALPACA_API_KEY_OPTIONS` / `ALPACA_SECRET_KEY_OPTIONS`

### Performance (all-time as of 2026-04-14)
- Total trades: 27 (3 actual buys, 24 HOLDs recorded)
- Wins: 0 / Losses: 27 / Pending: 0
- ⚠️ Note: performance.py records "stock_hold" as a loss when exits trigger — this
  is a tracking artifact, not reflective of actual P&L. GLD is up $374.

---

## Architecture — Four-Stage Pipeline (Account 1)

Each `run_cycle()` in `bot.py` executes this pipeline:

```
Stage 0 — Pre-Cycle Infrastructure (non-Claude)
  ├── fetch account + positions (Alpaca)
  ├── drawdown guard (>20% → halt)
  ├── market_data.fetch_all() → price bars, VIX, news, ORB levels
  ├── exit_manager.run_exit_manager() → audit/refresh stops, trail profits
  ├── portfolio_intelligence.build_portfolio_intelligence() → thesis scores,
  │   correlation, forced exits, dynamic sizing
  └── macro_intelligence.build_macro_backdrop_section() → rates/commodities/
      credit/Citrini/geopolitical backdrop

Stage 1 — Regime Classifier (claude-haiku-4-5-20251001)
  ├── Input: VIX, macro wire, economic calendar, global indices
  ├── Output: regime_score (0-100), bias, session_theme, constraints,
  │   macro_regime (reflationary/disinflationary/stagflationary/
  │   goldilocks/risk-off), commodity_trend, dollar_trend, credit_stress
  └── Uses prompt caching (ephemeral, 5-min TTL)

Stage 2 — Signal Scorer (claude-haiku-4-5-20251001)
  ├── Input: watchlist symbols (prioritized subset), market data signals,
  │   momentum, EMA9/EMA21, volume ratios
  ├── Output: per-symbol score + confidence + direction + catalyst
  ├── Prioritization: held positions → morning brief picks → breaking news
  │   → watchlist fill (up to _MAX_SCORED symbols)
  └── Uses prompt caching (ephemeral)

Stage 2.5 — Pre-Decision Scratchpad (claude-haiku-4-5-20251001) [added 2026-04-15]
  ├── File: scratchpad.py
  ├── Session gate: market hours only (skipped during extended/overnight)
  ├── Input: signal_scores_obj, regime_obj, md, positions
  ├── Output: watching[] (2-8 symbols), blocking[], triggers[],
  │   conviction_ranking[], summary — structured JSON
  ├── Hot memory: rolling 20 scratchpads → data/memory/hot_scratchpads.json
  │   (save_hot_scratchpad / get_recent_scratchpads)
  ├── Cold memory: ChromaDB three-tier (scratchpad_scenarios_short/medium/long)
  │   in trade_memory.py — reuses _promote_tier() and 60/30/10 blended retrieval
  │   (save_scratchpad_memory / retrieve_similar_scratchpads)
  ├── Prompt injection into Stage 3: format_scratchpad_section() as
  │   STAGE 2.5 SCRATCHPAD section + format_hot_memory_section(3) as
  │   RECENT SCRATCHPAD HISTORY section in user_template_v1.txt
  ├── Cold memory retrieval via get_two_tier_memory() appended to
  │   SIMILAR PAST SCENARIOS vector memory section
  ├── Weekly review API: get_scratchpad_history(days_back=7) and
  │   get_near_miss_summary(days_back=7) in trade_memory.py
  ├── Cost: ~$0.50-0.80/day (Haiku, market hours only ~78 cycles/day)
  └── Degrades gracefully to {} on any failure — never blocks main pipeline

Stage 3 — Main Decision (claude-sonnet-4-6)
  ├── Gate (sonnet_gate.py): fires BEFORE prompt assembly. Skips Sonnet if
  │   no material state change (cooldown 15 min, max 8 consecutive skips).
  │   Hard overrides: halt regime, CRITICAL recon action, max_skip_exceeded.
  │   State persisted in data/market/gate_state.json.
  │   Log markers: [GATE] SKIP | [GATE] SONNET triggered (reason) — COMPACT/FULL
  ├── Prompt routing: COMPACT (~1,500 tokens, compact_template.txt) for
  │   low-information cycles; FULL (~3,500 tokens, user_template_v1.txt)
  │   for high-information cycles (new catalyst, signal spike, deadline, etc.)
  ├── Input (FULL): full system prompt (~3KB cached) + market data + regime +
  │   signal scores + scratchpad pre-analysis + scratchpad history +
  │   portfolio intelligence + exit status + macro backdrop +
  │   vector memories + insider intelligence + reddit sentiment +
  │   earnings intel + morning brief + ORB levels + macro wire
  ├── Input (COMPACT): 6 blocks — account/risk, positions, market context,
  │   top 5 signals, constraints, task schema
  ├── Output: ClaudeDecision JSON (intent-based: ideas[], holds[], regime_view,
  │   reasoning, notes, concerns) — no qty/stops/order_type (risk kernel handles)
  ├── Overnight: always bypasses gate → lightweight Haiku call (_ask_claude_overnight)
  └── Never called without regime + signal stages completing first

Stage 4 — Execution
  ├── order_executor.execute_all() → validate + submit to Alpaca
  ├── exit_manager (post-execution stop refresh)
  └── trade_publisher (post-execution tweet generation → approval)
```

**Cycle intervals:**
| Session | Time (ET) | Interval | Instruments |
|---------|-----------|----------|-------------|
| market | 9:30 AM – 8:00 PM | 5 min (90s ORB window, 120s breakout) | Stocks + ETFs + Crypto |
| extended | 4:00 AM – 9:30 AM, 8 PM – 11 PM | 15 min | Crypto only |
| overnight | 11 PM – 4 AM, all weekend | 30 min | BTC/USD + ETH/USD only |

---

## Architecture — Account 2 (Options Bot)

Runs 90 seconds after every Account 1 market-hours cycle. Reads Account 1's signal
scores and uses them as candidate inputs — Account 2 never fetches its own market data.

**Files:** `bot_options.py`, `options_data.py`, `options_intelligence.py`, `options_builder.py`, `options_executor.py`, `options_state.py`, `order_executor_options.py`
**System prompt:** `prompts/system_options_v1.txt`

**Account 2 Pipeline:**
```
Stage 0 — Options reconciliation (BEFORE new proposals) [added 2026-04-15]
  ├── get_open_structures() → open structures from data/account2/positions/structures.json
  ├── _build_a2_broker_snapshot(alpaca_a2) → BrokerSnapshot (positions + open orders)
  ├── reconcile_options_structures() → OptionsReconResult
  │   ├── INTACT: all leg OCC symbols in broker positions
  │   ├── BROKEN: partial leg presence
  │   ├── EXPIRING SOON: DTE ≤ 2
  │   ├── NEEDS CLOSE: should_close_structure() returns True
  │   └── ORPHANED LEG: OCC position in broker with no matching structure
  ├── plan_structure_repair() → priority-ordered repair actions
  │   (broken > expiring > needs_close > orphaned)
  └── execute_reconciliation_plan(trading_client=alpaca_a2, account_id="account2")
      → close_broken_leg / close_expiring / close_structure / close_orphaned_leg
1. Check equity (floor $25K) + observation mode state
2. Load Account 1's last decision (regime, signal scores, open positions)
3. Build IV summaries for scored symbols (options_data.get_iv_summary())
4. options_intelligence.select_options_strategy() → StructureProposal (no strikes/expiry/contracts)
5. Claude four-way debate (Bull / Bear / IV Analyst / Synthesis)
   — debate receives StructureProposal fields; outputs direction + max_cost_usd (no strikes)
6. options_builder.build_structure() → OptionsStructure (real chain: strikes, expiry, contracts)
7. options_state.save_structure() → persist with lifecycle=PROPOSED
8. order_executor_options.submit_options_order(OptionsStructure) → delegates to options_executor
   — options_executor.submit_structure(): sequential legs, GTC limit, lifecycle updated
9. Close-check loop: options_executor.should_close_structure() per open structure
10. Log to data/account2/trade_memory/decisions_account2.json
```

**IV-first strategy selection:**
| IV Rank | Environment | Strategy |
|---------|-------------|---------|
| < 15 | very_cheap | Buy single leg ATM call/put |
| 15–35 | cheap | ATM debit spread (2–3 week expiry) |
| 35–65 | neutral | Debit or credit spread |
| 65–80 | expensive | OTM credit spread (sell premium) |
| > 80 | very_expensive | Avoid new positions |
| None | unknown/obs | HOLD — insufficient IV history |

**Hard rules:**
- Limit orders ONLY (never market)
- Delta ≥ 0.30, DTE ≥ 5 days
- Core spread: max 5% equity / core single leg: max 3% / dynamic: max 3%
- Scale 50% when VIX > 25, IV rank > 60, or earnings within 48h
- No options on crypto (spot only via Account 1)
- No options when equity < $25K
- Observation mode for first 20 trading days (IV history builds silently)

**Four-way debate format:**
- BULL AGENT: strongest bull case
- BEAR AGENT: key risks and challenges
- IV ANALYST: IV rank + recommended strategy based on environment
- SYNTHESIS: PROCEED / VETO / RESIZE / RESTRUCTURE
- Confidence ≥ 0.85 required for PROCEED

**Observation mode:** Currently active (day 1/20, started 2026-04-14).
Options chain fetched, IV recorded to `data/options/iv_history/{SYMBOL}_iv_history.json`.
Full debates run but orders are logged as `status="observation"` and not submitted.
Exits observation mode automatically after 20 trading days.

---

## Architecture — 10-Agent Weekly Review

Runs Sundays (or manually: `python3 weekly_review.py`). Two phases:

**Phase 1 — Batch API (50% discount, runs in parallel):**
| Agent | Role | Model |
|-------|------|-------|
| 1 — Quant Analyst | Signal quality, timing, sector patterns, ORB accuracy | Sonnet |
| 2 — Risk Manager | Position sizing, drawdown, stop effectiveness, PDT | Sonnet |
| 3 — Execution Engineer | Fill quality, rejections, API reliability | Sonnet |
| 4 — Backtest Analyst | Live vs. expected, vector memory divergences | Sonnet |

**Phase 2 — Sequential (web search + synthesis):**
| Agent | Role | Model |
|-------|------|-------|
| 5 — Strategy Director | Synthesizes 1-4 → strategic memo + JSON params → updates strategy_config.json | Sonnet |
| 6 — Market Intelligence Researcher | External landscape, academic research, competitor signals (has web search) | Sonnet |
| 7 — CFO | Cost tracking, monthly burn projection, ROI per intelligence layer | Haiku |
| 8 — Product Manager | Roadmap updates, prioritization, technical debt | Haiku |
| 9 — Compliance/Risk Auditor | Rule violations, near-misses, behavioral consistency | Haiku |
| 10 — Narrative Director | Weekly Twitter thread script for @BullBearBotAI | Haiku |

**Side effects:** Updates `strategy_config.json`, sends SMS summary, writes
`data/reports/weekly_review_YYYY-MM-DD.md`, updates `data/roadmap/features.json`.

**Emergency session:** `./board_meeting.sh 'reason'` or
`python3 weekly_review.py --emergency --reason 'reason'`
Report saved to `data/reports/emergency_review_{YYYYMMDD_HHMM}.md`.
Use after: major market events, earnings outcomes, regime shifts,
significant drawdown, or any time a full system assessment is needed.

---

## All Source Files

### Core Bot

| File | Purpose |
|------|---------|
| `bot.py` | Account 1 main loop. `run_cycle()` = full 4-stage pipeline. ~1,804 lines. |
| `bot_options.py` | Account 2 options cycle. Reads A1 signals, runs 4-way debate. |
| `scheduler.py` | 24/7 loop. Manages session tiers, runs A1+A2 cycles, all maintenance jobs. |
| `order_executor.py` | Validates + submits A1 orders. PDT guard, price sanity checks, stop floors. |
| `order_executor_options.py` | A2 thin wrapper. Equity floor + obs mode gate → delegates to options_executor. |
| `exit_manager.py` | Audits open positions for stops. Trails profit to breakeven. Crypto-aware. |

### Intelligence Stack

| File | Purpose |
|------|---------|
| `market_data.py` | Fetches live prices, VIX, bars, news. Cache-first (data_warehouse). |
| `data_warehouse.py` | 4 AM batch: refreshes bars, fundamentals, news, sector perf, global indices. |
| `macro_wire.py` | Reuters/AP RSS → keyword score → Haiku classifier. 3-tier storage. |
| `macro_intelligence.py` | Persistent macro backdrop: rates (2y/10y/30y), commodities, credit stress, Citrini, geopolitics. 1h cache. |
| `morning_brief.py` | 4:15 AM daily conviction brief (3–5 trade ideas). Injected into all market cycles. |
| `scanner.py` | 4 AM pre-market scanner → DYNAMIC tier candidates. Also runs ORB scan at 4:30 AM. |
| `earnings_intel.py` | SEC EDGAR 8-K transcripts → Claude analysis. Activates within 3 days of earnings. |
| `insider_intelligence.py` | Congressional trades (Lambda Finance) + SEC Form 4 insider buys. |
| `reddit_sentiment.py` | Reddit/WSB mention frequency + sentiment. Requires credentials (pending F001). |
| `portfolio_intelligence.py` | Thesis scoring, correlation matrix, dynamic sizing, forced exits, REALLOCATE actions. |
| `sonnet_gate.py` | State-change gate for Stage 3. Controls when Sonnet fires each market cycle. GateState in `data/market/gate_state.json`. Triggers: NEW_CATALYST, SIGNAL_THRESHOLD, REGIME_CHANGE, RISK_ANOMALY, POSITION_CHANGE, DEADLINE_APPROACHING, SCHEDULED_WINDOW, RECON_ANOMALY, HARD_OVERRIDE, MAX_SKIP_EXCEEDED, COOLDOWN_EXPIRED. |

### Options Stack (Account 2)

| File | Purpose |
|------|---------|
| `options_data.py` | IV history (252-day rolling), chain fetching via yfinance, IV rank/percentile/environment. |
| `options_intelligence.py` | IV-first strategy selector. Returns `StructureProposal` (direction, DTE range, budget). No strikes, expiry, or contracts — those are resolved by options_builder. |
| `options_builder.py` | Real-chain structure builder. `build_structure()` accepts StructureProposal keyword args or old-style action dict (backward-compat). Phase 1 only; Phase 2/3 return `(None, "not yet supported")`. |
| `options_executor.py` | Pure Alpaca broker adapter. Sequential leg submission (long first, poll, short). GTC limit orders. `build_occ_symbol()`, `submit_structure()`, `close_structure()`, `should_close_structure()`. |
| `options_state.py` | Persistence layer for OptionsStructure. Atomic writes to `data/account2/positions/structures.json`. API: `save_structure()`, `load_structures()`, `get_open_structures()`, `get_structures_by_symbol()`. |
| `order_executor_options.py` | Thin wrapper. Equity floor check + observation mode gate, then delegates to `options_executor.submit_structure()`. `OptionsExecutionResult` references `structure_id` (no redundant strikes/expiry). |

### Memory & Learning

| File | Purpose |
|------|---------|
| `memory.py` | Decision log (`memory/decisions.json`), performance tracking, pattern watchlist. |
| `trade_memory.py` | ChromaDB vector store (3-tier: recent/medium/long). Retrieves 5 similar past scenarios per cycle. |
| `watchlist_manager.py` | Manages 3-tier watchlist (core/dynamic/intraday). Prunes stale entries. |

### Communication & Reporting

| File | Purpose |
|------|---------|
| `trade_publisher.py` | Post generator for @BullBearBotAI. Currently in approval mode (SMS+email). |
| `report.py` | HTML performance report. Sent via email. |
| `weekly_review.py` | 10-agent weekly review. Batch API for agents 1–4. |
| `cost_tracker.py` | Real-time Claude API cost monitoring. Per-caller breakdown. |
| `account_status.py` | Account health summary tool. |

### Manual Tools

| File | Purpose |
|------|---------|
| `ingest_citrini_memo.py` | **Manual one-shot tool.** Parses Citrini Research PDF memos. Usage: `python3 ingest_citrini_memo.py path/to/memo.pdf` |
| `backtest_runner.py` | Backtesting framework. Not currently wired into production. (TD002) |

### Prompts

| File | Purpose |
|------|---------|
| `prompts/system_v1.txt` | Account 1 system prompt. ~250 lines. Full trading philosophy, watchlist tiers, risk rules, signal convergence, butterfly effect reasoning, ORB rules, etc. |
| `prompts/user_template_v1.txt` | Cycle-by-cycle FULL user prompt (~3,500 tokens, 137 lines). Intent-based ClaudeDecision schema: ideas[], regime_view, holds[], concerns. Variables: `{regime_summary}`, `{macro_backdrop}`, `{exit_status}`, `{signal_scores}`, etc. |
| `prompts/compact_template.txt` | COMPACT user prompt (~1,500 tokens). 6 blocks: account/risk, positions, market context, top 5 signals, constraints, task. Used by gate for low-information cycles. |
| `prompts/system_options_v1.txt` | Account 2 system prompt. IV-first hierarchy, four-way debate mandate, options hard rules. |

### Watchlists

| File | Contents |
|------|---------|
| `watchlist_core.json` | 41 core symbols (static, manually curated) |
| `watchlist_dynamic.json` | Scanner-promoted DYNAMIC symbols (same-day, ≤8% size) |
| `watchlist_intraday.json` | INTRADAY symbols (real-time additions, ≤5% size, intraday only) |
| `watchlist.json` | Merged view (read by watchlist_manager) |

**Core watchlist covers:** Technology (NVDA, TSM, MSFT, CRWV, PLTR, ASML), Energy (XLE, XOM, CVX, USO),
Commodities (GLD, SLV, COPX), Financials (JPM, GS, XLF), Consumer (AMZN, WMT, XRT), Defense (LMT, RTX, ITA),
Biotech (XBI), Health (JNJ, LLY), International (EWJ, FXI, EEM, EWM, ECH), Macro (SPY, QQQ, IWM, TLT, VXX),
Crypto (BTC/USD, ETH/USD), Shipping (FRO, STNG), Housing (RKT), Utilities (BE).

---

## Data Directories

```
data/
├── account2/
│   ├── trade_memory/decisions_account2.json   # A2 decision log
│   ├── costs/cost_log.jsonl                   # A2 Claude API costs
│   ├── positions/options_log.jsonl            # A2 execution log
│   └── obs_mode_state.json                    # observation mode counter
├── bars/                  # OHLCV bars cache (per symbol, from data_warehouse)
├── costs/daily_costs.json # A1 Claude API cost tracking
├── earnings/              # SEC EDGAR transcript cache (per symbol)
├── insider/               # Congressional + Form 4 cache
├── macro_intelligence/    # rates.json, commodities.json, credit.json,
│   │                      #   citrini_positions.json, Macro_Memo_Jan_2026.pdf
│   └── significant_events.jsonl
├── macro_wire/            # live_cache.json, significant_events.jsonl,
│   └── daily_digest/      #   daily_digest/YYYY-MM-DD.json
├── market/                # morning_brief.json, macro_snapshot.json,
│   │                      #   sector_perf.json, global_indices.json,
│   │                      #   earnings_calendar.json, premarket_movers.json,
│   │                      #   daily_conviction.json
│   └── signal_scores.json # A1 → A2 signal handoff (fresh within 10 min)
├── memory/                # pattern_learning_watchlist.json
├── options/
│   ├── iv_history/        # {SYMBOL}_iv_history.json (20-day minimum needed)
│   ├── chains/            # {SYMBOL}_chain.json (15-min cache)
│   ├── positions/
│   └── pnl/
├── reports/               # weekly_review_YYYY-MM-DD.md
├── roadmap/features.json  # F001–F010 feature tracker
├── scanner/               # ORB candidates, pre-market scan results
├── social/post_history.json  # Twitter post history (for dedup)
└── trade_memory/          # ChromaDB vector store (3 collections: recent/medium/long)
logs/
├── bot.log                # main rotating log (~1.6MB current)
├── scheduler.log          # scheduler events
└── trades.jsonl           # execution log (buy/sell/hold records)
memory/
├── decisions.json         # rolling A1 decision history (last 500)
└── performance.json       # win/loss stats by bucket
```

---

## Environment Variables (.env)

**Never commit, never echo in logs. File at `/home/trading-bot/.env`**

| Variable | Service | Notes |
|----------|---------|-------|
| `ALPACA_API_KEY` | Alpaca A1 | Paper account |
| `ALPACA_SECRET_KEY` | Alpaca A1 | Paper account |
| `ALPACA_BASE_URL` | Alpaca | `https://paper-api.alpaca.markets` |
| `ALPACA_API_KEY_OPTIONS` | Alpaca A2 | Separate paper account for options |
| `ALPACA_SECRET_KEY_OPTIONS` | Alpaca A2 | Separate paper account for options |
| `ANTHROPIC_API_KEY` | Claude | Shared by A1 + A2 |
| `TWILIO_ACCOUNT_SID` | Twilio | WhatsApp/SMS |
| `TWILIO_AUTH_TOKEN` | Twilio | WhatsApp/SMS |
| `TWILIO_FROM_NUMBER` | Twilio | `whatsapp:+14155238886` (sandbox) |
| `TWILIO_TO_NUMBER` | Twilio | `whatsapp:+18189177789` (recipient) |
| `SENDGRID_API_KEY` | SendGrid | Approval emails |
| `SENDGRID_FROM_EMAIL` | SendGrid | `eugene.gold@gmail.com` |
| `TWITTER_ENABLED` | Twitter | `false` (approval mode) |
| `TWITTER_PAPER_MODE` | Twitter | `true` (adds paper trading disclaimer) |
| `TWITTER_API_KEY/SECRET/etc.` | Twitter | Real credentials present, posting disabled |
| `TWITTER_BOT_HANDLE` | Twitter | `@BullBearBotAI` |
| `FINNHUB_API_KEY` | Finnhub | Available but not actively used |
| `REDDIT_CLIENT_ID/SECRET` | Reddit | **Missing** — F001 pending |

---

## Notification Setup

### Currently Working
- **WhatsApp (Twilio Sandbox):** Receives trade approvals, daily SMS alerts, weekly review summary, drawdown alerts. Uses `whatsapp:+14155238886` → `whatsapp:+18189177789`. The Twilio sandbox must be opted-in by texting "join" to the sandbox number first.
- **Email (SendGrid):** Receives full HTML approval emails for generated tweets. Goes to `eugene.gold@gmail.com`.

### Currently in Approval Mode (not auto-posting)
- **Twitter/X:** `TWITTER_ENABLED=false`. Posts are generated by Claude, delivered via SMS + email for manual copy-paste. To enable auto-posting: set `TWITTER_ENABLED=true` and upgrade to Twitter API Basic ($100/month). Wait for 30-day paper track record first (F003).

### Post Types (all generate content via Claude)
`trade_entry`, `trade_exit`, `premarket_brief`, `weekly_recap`, `flat_day`,
`interesting_skip`, `lookback`, `code_update`, `monthly_milestone`

---

## Citrini Research Integration

Citrini Research is a paid macro strategy newsletter. The bot ingests Citrini memos manually
and uses the extracted positions as high-conviction macro overlay for trade decisions.

**Current Citrini positions (Jan 2026 memo, ingested):**
- IBIT: Long (BTC catch-up rebound, target 100K+)
- Oil futures (CLH6): Long (crowded shorts, Iran geopolitical premium)
- Natural Gas Dec 2027 (NGZ27): Long (mean reversion from 4.42)
- Copper Dec 2026 (HGZ6): Long (LatAm supply chain, AI infrastructure demand, up ~20%)
- 2s30s Yield Curve: Short/Flattener (contrarian — consensus is steepener)
- Tanker Basket (FRO, STNG): Long (Iranian crude, aging fleet, sanctions)
- FXI: Long calls (China recovery, anti-involution, trade tension easing)
- EWM: Long (Malaysia ASEAN onshoring, data center buildout)
- ECH: Long (Chile copper/lithium LatAm realignment)
- RKT: Long (Trump mortgage rate push, MBS purchases, owns Redfin+Mr Cooper)

**Citrini macro view (Jan 2026):**
- US growth: expanding, AI productivity driving above-trend nominal GDP
- Rates: too low — expects 2y to move higher, rate cuts priced out
- Dollar: bullish
- Key risks: Iran resolution (kills oil/tanker premium), Fed/Trump conflict, tariff re-escalation

**How to update:** `python3 ingest_citrini_memo.py path/to/memo.pdf`
- Extracts active_trades, watchlist_themes, macro_view via Claude
- Saves to `data/macro_intelligence/citrini_positions.json`
- Weekly review (Agent 8) reminds to check for new Citrini content
- `macro_intelligence.py:load_citrini_positions()` reads it (never auto-overwrites)

**Watchlist additions from Citrini:** EWM, ECH, FRO, STNG, RKT, BE, COPX were all added based on Citrini theses.

---

## strategy_config.json

Written by the weekly review's Strategy Director (Agent 5). Bot reads it each cycle.
Key sections:
- `active_strategy`: "hybrid" (momentum + mean-reversion + news + cross-sector)
- `parameters`: `stop_loss_pct_core=0.035`, `take_profit_multiple=2.5`, `max_positions=15`, etc.
- `director_notes`: Strategy Director's current operational memo (read by bot)
- `time_bound_actions`: Mandatory exits with deadlines (e.g., TSM exit before earnings)
- `exit_management`: Trail stop config (`trail_trigger_r=1.0`, `trail_to_breakeven_plus_pct=0.005`)
- `account2`: Full Account 2 config (observation_mode_days=20, sizing limits, IV rules, greeks)

---

## Cost Profile

**Current (2026-04-14, day 2):**
- Daily spend: $10.87 (618 Claude calls)
- All-time total: $12.04

**By caller (today):**
| Caller | Cost | Calls |
|--------|------|-------|
| ask_claude (main Sonnet) | $8.05 | 192 |
| signal_scorer (Haiku) | $1.85 | 154 |
| macro_wire_classifier (Haiku) | $0.65 | 97 |
| regime_classifier (Haiku) | $0.28 | 153 |

**Monthly projection at current rate:** ~$300+ (too high — see Known Bugs)

**Target:** ~$100/month. Main optimization needed: `ask_claude` dominates at 74% of spend.
The main Sonnet call prompt is very large (includes full portfolio intelligence, macro backdrop,
vector memories, insider data, etc.). Prompt caching helps but the output cost is high.

**Cost alerts:** SMS sent if daily > $5 (already triggered) or monthly projection > $100.

**Pricing used (per million tokens):**
- Sonnet 4.6: $3.00 input / $15.00 output / $3.75 cache write / $0.30 cache read
- Haiku 4.5: $1.00 input / $5.00 output / $1.25 cache write / $0.10 cache read
- Batch API discount: 50% (used for weekly review agents 1–4)

---

## Known Bugs

### ✅ RESOLVED (2026-04-14) — BUG-001 — `_MAX_SCORED = 9999` (signal scorer never caps at 15)
**File:** `bot.py:430`
**Description:** The signal scorer has a prioritization algorithm designed to cap at 15 symbols
(held positions → morning brief → breaking news → watchlist fill). The comment says "capped at 15"
but `_MAX_SCORED = 9999`. Every cycle scores all 39 symbols, hitting max_tokens and increasing cost.
**Impact:** High — $1.85/day just for signal scoring, all 39 symbols scored vs. intended 15.
**Fix:** Change `_MAX_SCORED = 9999` to `_MAX_SCORED = 25` on line 431.
**Resolution:** _MAX_SCORED changed to 25. Was causing JSON truncation and parse failures in addition to cost bleed.

### ✅ RESOLVED (2026-04-15) — BUG-002 — HOLD actions rejected "market is closed" (288+ occurrences)
**File:** `order_executor.py` → `validate_action()`
**Description:** When Claude issues a HOLD action with stop_loss/take_profit for an extended-session
cycle, the executor rejects it with "market is closed" because stocks can't trade after hours.
But HOLDs with stop updates should be allowed — the stop orders were placed during market hours.
**Impact:** Medium — stop refresh logic doesn't run extended-session. Existing stops remain.
**Fix:** In the HOLD handler in `execute_all()`, skip the market-open check when only
submitting/refreshing stop/limit orders (not new position entries).
**Resolution:** Wrapped market-open check in validate_action() with `act not in ("hold", "monitor", "watch", "observe")` guard. HOLDs now reach the full hold handler in execute_all() during extended session.

### ✅ RESOLVED (2026-04-15) — BUG-003 — `performance.py` records HOLDs as losses
**File:** `memory.py`
**Description:** The performance tracker records "stock_hold" actions as trades with outcome="loss"
because the position didn't close at a profit that cycle. This inflates the loss count to 27
when actual closed-trade losses are 3 (3 actual buys that resolved).
**Impact:** Low — affects weekly review analytics and agent reporting. Doesn't affect trading.
**Resolution:** Added action type filter in update_outcomes_from_alpaca() — only buy/sell/close/options actions get outcome resolution. performance.json reset to reflect 2 real closed trades: BTC +$0.01 (win), ETH -$2.35 (loss).

### ✅ RESOLVED (2026-04-14) — BUG-004 — Account 2 signal handoff depends on file freshness
**File:** `bot_options.py:_load_signal_scores_from_account1()`
**Description:** A2 reads A1's signal scores from `data/market/signal_scores.json` if fresh
within 10 minutes. This file may not be written by A1 (not confirmed in current bot.py).
If A1 doesn't write signal scores to disk, A2 skips its cycle every time.
**Impact:** High — Account 2 may never actually evaluate trades.
**Fix:** Confirm bot.py writes signal scores to `data/market/signal_scores.json` after
`score_signals()` completes, or wire A2 to read from `memory/decisions.json` instead.
**Resolution:** Three fixes: (1) bot.py now writes signal_scores.json after score_signals(). (2) bot_options.py _load_signal_scores_from_account1() fixed to extract scored_symbols key. (3) _build_options_candidates() field name corrected from confidence to conviction — Account 2 was silently skipping all candidates every cycle.

### ✅ RESOLVED (2026-04-14) — BUG-005 — SPY IV reads as 0.02 (2%) — unrealistically low
**File:** `options_data.py:_extract_atm_iv()`
**Description:** yfinance returned `impliedVolatility=0.02` for SPY ATM options on first fetch.
True ATM IV for SPY should be ~15–25% (0.15–0.25). The first yfinance chain fetch may have
returned near-zero IV for the April 14 (same-day) expiration which has collapsed theta.
**Impact:** Low for now (observation mode). Will corrupt IV rank baselines if not corrected.
**Fix:** Skip same-day or next-day expirations when extracting ATM IV for history. Use
the 2nd or 3rd expiration in the chain (7–14 DTE) for IV history recording.
**Resolution:** _extract_atm_iv() now skips DTE < 2, targets 7–14 DTE window. SPY IV was reading 0.02% due to same-day expiration.

### ✅ RESOLVED (2026-04-15) — BUG-007 — exit_manager enum serialization blocks trail stops
**File:** `exit_manager.py:get_active_exits()`
**Description:** `OrderType.STOP` serializes as `"ordertype.stop"` not `"stop"` — the string
comparison `o_type in ("stop", "stop_limit")` always failed. Stop orders were never recognized
as such; `stop_price` stayed None; trail stop and stale-stop refresh never fired.
**Impact:** High — GLD trail stop hadn't fired despite 2.28× profit ($10 gain on $4.39 risk).
Live stop was stuck at $429.11 (~1% below entry) instead of $435.66 (breakeven + 0.5%).
**Resolution:** Added `.split(".")[-1]` normalization for both `o_type` and `o_side` immediately
after the `str(...).lower()` call. GLD stop trailed $429.11 → $435.66 on the first cycle after fix.

### ✅ RESOLVED (2026-04-15) — BUG-008 — BTC/USD hold emitting signal score as stop_loss
**File:** `bot.py:run_cycle()` — post-processing pass after `ask_claude()`
**Description:** BTC/USD hold actions emitting `stop_loss: 68` — the signal score integer
(0–100 scale) used as a price. Claude reads `score=68` in the formatted signal scores section
and occasionally copies it into the `stop_loss` field of a hold action. Price-scale guard in
order_executor.py caught and discarded it, but root cause needed fixing upstream.
**Impact:** Medium — BTC/USD holds had no valid stop refresh during affected cycles.
Would have caused unprotected crypto position if the price-scale guard had ever missed.
**Resolution:** Added post-processing validation pass in bot.py after ask_claude(). For any
crypto action (`/` in symbol) where `stop_loss < 1000`, recalculates stop as
`current_price × (1 − 0.08)` and logs `[BUG008]` warning. Fix is applied before execution
so the corrected value reaches both order_executor and decisions.json.

### ✅ RESOLVED (2026-04-15) — BUG-009 — exit_manager treats take-profit limit as stop coverage
**Files:** `exit_manager.py`
**Description:** `get_active_exits()` fetches orders with `status=OPEN`. Alpaca bracket orders
produce two OCA children (take-profit limit + stop-loss sell). After the parent fills, the
stop-loss child enters a non-"open" status (held/accepted) and is **invisible to status=OPEN
queries**. Only the take-profit limit child appears as open. The prior code classified any sell
order as protection — so TP-only positions landed in "partial" status and
`refresh_exits_for_position()` silently skipped them (its gate only opens for "unprotected").
Result: positions entered via `OrderClass.BRACKET` held NO active stop-loss after the initial
fill, with no alert and no auto-repair.
**Root cause verified:** `GET /orders?status=open` returns bracket TP limit but NOT the bracket
stop child. `GET /orders?status=all` shows both. Stop child status is "held" (OCA).
**Impact:** HIGH — all bracket-entry positions were silently unprotected after entry. AMZN and
XBI had no stop-loss for ~18 hours after entry (discovered 2026-04-15). Manual stops placed
via fix_stops.py as emergency remediation (AMZN $238.91, XBI $130.81).
**Resolution:**
- Added `_has_stop_order()` and `_has_take_profit_order()` helpers to `exit_manager.py`
- Added "tp_only" status: `get_active_exits()` emits this when target_price is visible but
  stop_price is None (rather than falling through to the "partial" fallback)
- `refresh_exits_for_position()` treats "tp_only" as unprotected — cancels TP first (to
  release Alpaca's held-share lock per error 40310000), then places SIMPLE stop; skips TP
  re-submission since Alpaca won't accept two pending sell orders simultaneously
- `run_exit_manager()` now logs INFO per-position status every cycle: "stop protected, no
  take profit — OK" for partial; "fully protected" for protected; WARNING for tp_only/unprotected
- `reconciliation.py:diff_state()` now uses `_has_stop_order()` from exit_manager (consistent
  stop-detection logic across both modules; `_has_stop_order` covers trailing_stop too)
- 4 regression tests added (Suite 13 — BUG-009): `_has_stop_order` False for limit, True
  for stop; `diff_state()` flags limit-sell-only as missing_stops; stop order clears flag

### ✅ RESOLVED (2026-04-15) — BUG-010 — Weekly review agents 7-9 batch `extra_headers` error
**File:** `weekly_review.py:_run_phase2_agents()`
**Description:** Agents 7 (CFO), 8 (PM), and 9 (Compliance) batch requests included
`"extra_headers": {"anthropic-beta": "prompt-caching-2024-07-31"}` inside the `params` dict.
`extra_headers` is an HTTP-level SDK option — not a valid batch request `params` field.
The Batch API rejected the entire Phase 2 batch with
`BetaInvalidRequestError: extra_headers: Extra inputs are not permitted`.
Agents 1-4 (`_run_agents_via_batch`) worked correctly because they never included this field.
The `extra_headers` was also redundant — prompt caching is already activated by the
`cache_control: {type: ephemeral}` block in the system message.
**Resolution:** Removed the `"extra_headers"` key from all three agents' `params` dicts.

### ✅ RESOLVED (2026-04-15) — BUG-011 — ChromaDB protobuf version conflict (intermittent)
**File:** `.env` / `trade_memory.py`
**Description:** `protobuf==7.34.1` + `onnxruntime==1.24.4` + `chromadb==1.5.7` combination
produces `Descriptors cannot be created directly` on import. Once the exception fires, the
`_collections_tried` singleton marks ChromaDB disabled for the entire process lifetime.
The systemd service file already has `Environment=PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python`
hardcoded, so failures were only occurring when trade_memory was imported outside the service
(manual CLI runs, test processes). Added the same var to `.env` for consistency.
**Resolution:** `PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python` confirmed in both `.env`
and the systemd service `EnvironmentFile`.

### ✅ RESOLVED (2026-04-15) — BUG-012 — `trade_memory._build_document()` uses stale field names
**File:** `trade_memory.py:_build_document()` (~line 225)
**Description:** `_build_document()` read `decision.get("regime", "?")` and
`decision.get("actions", [])`. The `ClaudeDecision` format was updated to use `"regime_view"`
and `"ideas"` — but `_build_document()` was not updated. Every trade since the format change
was stored in ChromaDB with `regime="?"` and `actions_str="HOLD"`, degrading similarity
search quality (all vectors looked identical in the regime/actions dimensions).
**Resolution:** Updated `_build_document()` to check new-format fields first with fallback
to old-format fields. `regime` now reads `regime_view` (falls back to `regime`). `actions_str`
now built from `ideas[].intent/symbol` (falls back to `actions[].action/symbol`).

### ✅ RESOLVED (2026-04-15) — BUG-013 — Rejection log entries missing `session` field
**Files:** `order_executor.py:execute_all()`, `bot.py:run_cycle()`
**Description:** `execute_all()` logged all rejections to `trades.jsonl` without a `"session"`
field. `weekly_review.py` aggregates rejection counts by session using
`rec.get("session", "unknown")` — so 100% of executor-path rejections showed as
`session="unknown"`. The board meeting's "33.4% unknown session" finding was a logging artifact,
not a runtime race condition. The runtime session classification was correct throughout
(risk_kernel correctly receives and uses `session_tier` from the scheduler).
**Resolution:** Added `session_tier: str = "unknown"` parameter to `execute_all()`. Pass
`session_tier=session_tier` from `bot.py:run_cycle()`. Added `"session": session_tier` to
the rejection, submitted, and error `log_trade()` calls so weekly review sees full
session distribution across all trade outcomes.
**Side effect fixed:** `order_executor.py` had a `tier_pct = 0.20` override for high-confidence
core buys that referenced the deprecated `max_single_position_pct`. Removed. `TIER_MAX_PCT`
(core=15%, dynamic=8%, intraday=5%) is now the sole position sizing authority in the executor.
Both deprecated `max_single_position_pct` fields removed from `strategy_config.json`.
`validate_config.py` updated: the old consistency check replaced with a deprecation check
(FAIL if either field is present).

---

## Feature Roadmap

| ID | Feature | Status | Priority |
|----|---------|--------|---------|
| F001 | Reddit API credentials | pending | high — code exists, just needs credentials |
| F002 | Alpaca options approval | completed | — |
| F003 | Twitter API Basic upgrade | pending | medium — wait for 30-day track record |
| F004 | Unusual Whales options flow | pending | medium — requires $50–100/month subscription |
| F005 | Account 2 — Options dedicated | completed | — IV history bootstrapped, obs mode complete |
| F006 | Account 3 — Aggressive | pending | medium — momentum-only, WSB signals, wider stops |
| F007 | Crypto intelligence upgrades | completed | — |
| F008 | Portfolio intelligence | completed | — |
| F009 | Sequential synthesis pipeline | completed | — |
| F010 | Market Intelligence Researcher | completed | — |

### Next Builds (Suggested Priority)

**~~F011~~ — Fix BUG-001 + BUG-004 ✅ COMPLETED 2026-04-14**
_MAX_SCORED set to 25 (not 15 — watchlist grew). signal_scores.json write added to bot.py.
Three additional A2 fixes: nested JSON extraction, conviction vs confidence field, obs mode exit.

**~~F012~~ — IV history bootstrap seeding ✅ COMPLETED 2026-04-14**
38/38 symbols seeded with 25 days of IV history from live yfinance chain data. BUG-005 fixed
simultaneously. obs_mode_state.json updated to observation_complete=true. Account 2 is live.

**F013 — Reddit credentials activation [5 min]**
Once Reddit developer app is approved, add `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET`
to `.env`. Code already fully built (`reddit_sentiment.py`). Just needs credentials.

**F014 — Cost optimization: reduce ask_claude spend [1 session]**
`ask_claude` is 74% of daily spend ($8/day). Options:
1. Add caching to the large prompt sections that don't change cycle-to-cycle
2. Move more logic to Haiku (it handles regime + signals fine)
3. Compress portfolio intelligence and vector memory sections
4. Consider a pre-filter: only run Sonnet when signals are above a confidence threshold

**F015 — Account 3: Aggressive Momentum [1 session]**
Third Alpaca paper account, momentum-only. WSB/Reddit-driven entries.
Wider stops (7–10%), no PDT restrictions (account >$25K), higher position concentration.
Would need a third set of Alpaca credentials.

**~~F016~~ — Options builder + executor pipeline ✅ COMPLETED 2026-04-15**
Full A2 pipeline refactor across two sessions.
`options_execution.py` deleted (dead code, broken enums).
`options_intelligence.py` demoted to recommender — returns `StructureProposal` (no strikes/expiry/contracts).
`options_executor.py` created — pure Alpaca broker adapter, sequential leg submission, GTC limit orders, close-check loop.
`order_executor_options.py` thinned to ~130 lines — equity floor + obs mode gate, delegates to options_executor.
`bot_options.py` wired: proposal → builder → save_structure(PROPOSED) → submit → close-check.
`prompts/system_options_v1.txt` updated: removed expiration/strikes/contracts/delta from debate JSON, added direction.
`options_builder.build_structure()` updated to accept StructureProposal keyword args (backward-compat with old dict form).
`schemas.py`: `StructureLifecycle` 8-value spec; `OptionsStructure` gains direction/expiration/strikes/audit_log;
`StructureProposal` added; `StructureProposal.direction` typed as `Direction` enum.
15 new tests (Suites 14+15) — total 160 tests, all passing.

**~~F017~~ — A2 Options structure reconciliation (Stage 0) ✅ COMPLETED 2026-04-15**
Full reconciliation pass for Account 2, running before every new structure proposal.

Phase 1 build status — all 10 items complete:
- ✅ Item 1: Pre-implementation report
- ✅ Item 2: `reconcile_options_structures()` — replaced with 5-check comprehensive version
- ✅ Item 3: `plan_structure_repair()` — new function, priority-ordered repair actions
- ✅ Item 4: `execute_reconciliation_plan()` — extended with A2 options action types + keyword args
- ✅ Item 5: `bot_options.py` — Stage 0 wired, `_build_a2_broker_snapshot()` added
- ✅ Item 6: Tests — 165 tests passing (160 prior + 5 new, Suite 16)
- ✅ Item 7: CLAUDE.md updated
- ✅ Item 8: Service restarted, log verified
- ✅ Item 9: Local mirror rsync'd
- ✅ Item 10: Final board meeting check

`reconciliation.py`: `OptionsReconResult` dataclass added (intact/broken/expiring_soon/needs_close/orphaned_legs).
`reconcile_options_structures()` now takes `(structures, snapshot, current_time, config)` → `OptionsReconResult`.
  Checks: INTACT (all leg OCC symbols in broker), BROKEN (partial), EXPIRING SOON (DTE ≤ 2),
  NEEDS CLOSE (should_close_structure()), ORPHANED LEG (OCC position with no matching structure).
`plan_structure_repair()` added — priority: broken > expiring > needs_close > orphaned.
`execute_reconciliation_plan()` extended — handles close_broken_leg / close_expiring /
  close_structure / close_orphaned_leg; accepts `trading_client`/`account_id`/`dry_run` kwargs;
  backward-compatible with existing A1 positional call.
`bot_options.py` Stage 0 runs after expiring-positions check, before VIX/signal load.
  Wraps `reconcile → plan_repair → execute_reconciliation_plan` in non-fatal try/except.
5 new tests in Suite 16 — intact, broken, expiring, priority ordering, no-structures skip.
Old 6 tests in Suite 10 updated to new API (OCC symbol / BrokerSnapshot based).

---

## Scheduler Maintenance Jobs

These run automatically in the main scheduler loop before each cycle:

| Time | Job | Function |
|------|-----|---------|
| 4:00–5:00 AM ET weekdays | Data warehouse refresh | `_maybe_run_premarket_jobs()` |
| 4:00–5:30 AM ET weekdays | Macro intelligence pre-fetch | `_maybe_refresh_macro_intelligence()` |
| 4:00–5:30 AM ET weekdays | IV history refresh (A2) | `_maybe_refresh_iv_history()` |
| 4:15–5:30 AM ET weekdays | Morning conviction brief | `_maybe_run_morning_brief()` |
| 4:30 AM ET weekdays | ORB candidate scan | `_maybe_run_orb_scan()` |
| 9:30–9:45 AM ET weekdays | ORB formation window | every cycle — `update_orb_candidates()` |
| 9:28–9:30 AM ET weekdays | Pre-open prep cycle | `_maybe_run_preopen_cycle()` |
| 4:00 PM ET weekdays | Daily digest + flat-day post | `_maybe_write_daily_digest()`, `_maybe_publish_flat_day()` |
| 4:15 PM ET weekdays | Market impact backfill | `_maybe_backfill_market_impact()` |
| Every cycle | Reddit sentiment refresh | `_maybe_refresh_reddit_sentiment()` |
| Every cycle | Form 4 + Congressional refresh | `_maybe_refresh_form4_trades()` |
| Every cycle | Macro wire refresh | `_maybe_refresh_macro_wire()` |
| Every cycle | Global indices refresh | `_maybe_refresh_global_indices()` |
| Sunday AM | Weekly review | `_maybe_generate_weekly_summary()` |
| Monday-Friday 6 PM | Lookback post | `_maybe_publish_lookback()` |

---

## Risk Rules (Hard Limits, Never Overridden)

### Account 1
- Core position: max 15% equity
- Dynamic (scanner) position: max 8% equity
- Intraday position: max 5% equity
- Max total exposure: $30,000 hard cap
- Options per trade: max $5,000
- Stop losses: core 3.5%, standard 5%, speculative 7% (set in strategy_config.json)
- Crypto stops: core 8%, standard 10%, speculative 12% (wider due to volatility)
- Crypto orders: GTC (not DAY — crypto trades 24/7)
- PDT floor: never trade if equity < $26,000
- PDT rules: do NOT apply to BTC/USD and ETH/USD
- Never trade first 15 minutes of market open (ORB formation)
- Never chase: no entry if stock already up 5%+ without fresh 30-min catalyst
- Never average down on losing position
- Never hold through binary event at full size (half size max)
- Never hold DYNAMIC or INTRADAY symbols overnight
- VIX > 35: halt all new positions, cash only, send SMS alert
- Drawdown > 20% from peak: halt cycle entirely

### Account 2 (Options)
- Core spread: max 5% equity
- Core single leg / dynamic: max 3% equity
- Intraday symbols: no options
- Delta: min 0.30 required
- DTE: min 5 days
- Limit orders only (never market orders)
- Equity floor: $25,000
- Scale 50% when VIX > 25, IV rank > 60, or earnings within 48h
- Never options on crypto (spot only via A1)
- Crisis regime (VIX > 40): halt all new options positions

---

## Key Architectural Decisions and Why

**Why prompt caching?** The system prompt (~3KB) and large sections are cached with
`cache_control: {type: "ephemeral"}` and the `anthropic-beta: prompt-caching-2024-07-31`
header. Cache TTL is 5 minutes — matches the 5-minute market cycle interval exactly.
Cache reads cost 0.10× input price. Without caching, the system prompt alone would
double input costs.

**Why ChromaDB?** Three-tier vector memory (recent/medium/long-term collections).
Retrieves 5 similar past market scenarios each cycle. The `trade_memory.py` module
handles collection management, aging, and promotion between tiers. The DB lives at
`data/trade_memory/chroma.sqlite3`.

**Why Haiku for Stage 1+2?** Regime classification and signal scoring don't need deep
reasoning — they need speed and cost efficiency. Haiku at $0.002/cycle vs Sonnet at
$0.04/cycle for the same task. The main Sonnet call gets cleaner input as a result.

**Why separate Account 2?** Options require IV-specific analysis that would bloat the
Account 1 prompt. Options can lose 100% of premium (defined risk) but require different
position management. Separating accounts allows different risk profiles, different
instruments, and independent P&L tracking.

**Why Citrini?** Citrini Research is a macro-first strategy newsletter by a respected
ex-hedge fund manager. The bot uses Citrini's longer-horizon theses as a macro overlay
and as confirmation for specific sector trades. EWM, ECH, FRO, STNG, RKT, BE were all
added to the watchlist directly from Citrini recommendations.

**Why no MARKET orders for options (A2)?** Options spreads can have wide bid/ask. Market
orders on thinly-traded strikes can result in fills at ask (significant overpay) or even
fill outside the spread. The executor uses mid-price limits with 5% slippage buffer.

---

## Common Operations for Claude Code Sessions

### Check if bot is running
```bash
ssh tradingbot 'systemctl status trading-bot && tail -5 /home/trading-bot/logs/bot.log'
```

### Check current positions
```bash
ssh tradingbot 'source /home/trading-bot/.venv/bin/activate && cd /home/trading-bot && python3 -c "
from dotenv import load_dotenv; load_dotenv()
from alpaca.trading.client import TradingClient
import os
a1 = TradingClient(os.getenv(\"ALPACA_API_KEY\"), os.getenv(\"ALPACA_SECRET_KEY\"), paper=True)
for p in a1.get_all_positions(): print(p.symbol, p.qty, p.market_value)
"'
```

### Manually run weekly review
```bash
ssh tradingbot 'cd /home/trading-bot && source .venv/bin/activate && python3 weekly_review.py'
```

### Ingest new Citrini memo
```bash
# Copy PDF to server first
scp ~/Downloads/citrini_memo.pdf tradingbot:/home/trading-bot/data/macro_intelligence/
# Run ingestion
ssh tradingbot 'cd /home/trading-bot && source .venv/bin/activate && python3 ingest_citrini_memo.py data/macro_intelligence/citrini_memo.pdf'
```

### Check today's Claude costs
```bash
ssh tradingbot 'python3 -c "import json; d=json.load(open(\"/home/trading-bot/data/costs/daily_costs.json\")); print(f\"Daily: \${d[\"daily_cost\"]:.2f}  Calls: {d[\"daily_calls\"]}\")"'
```

### Check Account 2 observation mode progress
```bash
ssh tradingbot 'cat /home/trading-bot/data/account2/obs_mode_state.json'
```

### Force single options cycle (Account 2)
```bash
ssh tradingbot 'cd /home/trading-bot && source .venv/bin/activate && python3 bot_options.py market'
```

---

## Important Context for Future Sessions

1. **No git.** Code is managed via rsync. Changes pushed to server via rsync or scp.
   Always pull from server before making changes to avoid overwriting server-side edits.

2. **Scheduler is always running.** The systemd service auto-restarts every 30 seconds.
   When you edit a file on the server, the next cycle picks it up automatically (no restart needed
   for most changes). For scheduler.py changes, `systemctl restart trading-bot` is needed.

3. **Edit files via scp.** The Edit tool works on local `/tmp/` copies. Pattern:
   `scp server:file /tmp/edit.py` → edit → `scp /tmp/edit.py server:file` → py_compile check.

4. **Non-fatal everywhere.** Every external call is wrapped in `try/except`. Failures are
   logged at DEBUG or WARNING level. Nothing should ever halt a cycle except: drawdown guard,
   equity below PDT floor, VIX > 35, or unhandled exception in `run_cycle()` itself.

5. **Prompt caching is critical for cost.** Don't restructure system prompts without
   understanding cache implications. The 5-minute TTL aligns with market session cadence.

6. **Account 2 is in observation mode.** It has been running for 1 trading day.
   It needs 19 more before it trades real orders. The IV history files are in
   `data/options/iv_history/`. Currently only SPY has 1 day of history.

7. **TSM time-bound exit.** TSM must be closed by 2026-04-15 15:45 ET (TSM earnings April 16).
   This is in `strategy_config.json:time_bound_actions` and the bot checks it each cycle.

8. **Twitter is in approval mode.** Generated tweets go to `eugene.gold@gmail.com` + WhatsApp.
   Do not change `TWITTER_ENABLED=true` until the Twitter API Basic upgrade is purchased (F003).

9. **Reddit sentiment is built but dormant.** `reddit_sentiment.py` is complete. It returns `{}`
   when credentials are missing (graceful degradation). F001 is just adding credentials to `.env`.

10. **The Twilio WhatsApp sandbox requires opt-in.** The recipient (`+18189177789`) must text
    "join [sandbox-word]" to `+14155238886` to receive WhatsApp messages. If notifications stop
    arriving, the sandbox session may have expired.

11. **Citrini PDF stored on server.** `data/macro_intelligence/Macro_Memo__Jan_2026.pdf` is the
    source PDF. `citrini_positions.json` is the parsed output. Update whenever a new memo arrives.

12. **Cost alert already triggered.** `daily_alert_sent: true` means an SMS was sent when
    today's spend crossed $5. The alert threshold is `$5/day` and `$100/month projection`.
    At $10.87/day, monthly projection is ~$325 — well above target. BUG-001 fix is the
    highest-impact cost reduction available.

13. **VIX during recent cycles:** ~18–20 (normal regime). Macro wire shows Asia rally
    (Nikkei +2.4%), but Crypto Fear & Greed at 21 (Extreme Fear). PPI data pending.
    Bot is holding GLD (safe haven) and TSM (semi thesis) with open stops.

14. **The `memory/decisions.json` file** (Account 1 decisions) is separate from
    `data/trade_memory/` (ChromaDB vector store). The JSON file has the last 500 decisions
    in structured format. ChromaDB has semantic embeddings for similarity search.

15. **Account 2 signal handoff (BUG-004).** The most important unfixed bug for Account 2
    going live. Verify that `bot.py:run_cycle()` writes signal scores to disk after scoring.
    If not, add: `(Path(__file__).parent / "data/market/signal_scores.json").write_text(json.dumps(signals))`
    after the `score_signals()` call.
