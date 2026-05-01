# CLAUDE.md — BullBearBot System Brief

This file gives Claude context about the BullBearBot system architecture.

## System overview
Two autonomous trading bots running on a DigitalOcean VPS:
- **A1** — Equity bot. Makes buy/sell decisions using Claude Sonnet as the decision engine.
- **A2** — Options bot. Runs in parallel, builds multi-leg options structures using a 4-agent debate.

Both bots trade paper accounts on Alpaca. The system is autonomous — it reads market data,
runs multi-stage Claude calls, executes trades, manages stops, and posts updates every cycle.

## Key files

### Core pipeline
| File | Purpose |
|------|---------|
| `bot.py` | A1 main entry point and cycle orchestration |
| `bot_stage0_precycle.py` | Pre-cycle infrastructure (positions, market data, exit audit) |
| `bot_stage1_regime.py` | Regime classifier (Haiku) — risk_on / risk_off / caution |
| `bot_stage1_5_qualitative.py` | Qualitative context sweep (Haiku) — per-symbol catalyst synthesis |
| `bot_stage2_signal.py` | Signal scorer (Haiku) — L2 Python anchors + L3 Haiku synthesis |
| `bot_stage2_python.py` | L2 pure-Python signal computation (no API calls) |
| `bot_stage2_5_scratchpad.py` | Scratchpad pre-analysis (Haiku) |
| `bot_stage3_decision.py` | Main Sonnet decision call |
| `bot_stage4_execution.py` | Post-decision execution wiring |
| `bot_options.py` | A2 options cycle entry point |
| `bot_options_stage0_preflight.py` | A2 preflight — reconcile open structures |
| `bot_options_stage1_candidates.py` | A2 candidate generation from A1 signal scores |
| `bot_options_stage2_structures.py` | A2 structure routing (12 rules) and veto gates |
| `bot_options_stage3_debate.py` | A2 4-agent debate (Bull, Bear, IV Analyst, Judge) |
| `bot_options_stage4_execution.py` | A2 execution — submit multi-leg structures to Alpaca |
| `scheduler.py` | 24/7 scheduler — manages session tiers, all maintenance jobs |

### Intelligence stack
| File | Purpose |
|------|---------|
| `morning_brief.py` | Intelligence brief — generated 8x/day, 10 sections |
| `market_data.py` | Live prices, bars, VIX, news |
| `data_warehouse.py` | 4 AM batch refresh for bars, fundamentals, earnings calendar |
| `macro_wire.py` | Reuters/AP RSS → keyword score → Haiku classification |
| `macro_intelligence.py` | Persistent macro backdrop (rates, commodities, credit stress) |
| `earnings_intel.py` | EDGAR 8-K transcript analysis |
| `insider_intelligence.py` | Congressional trades + SEC Form 4 insider buys |
| `portfolio_intelligence.py` | Thesis scoring, correlation, forced exits, sizing |
| `sonnet_gate.py` | State-change gate — controls when Sonnet fires |
| `risk_kernel.py` | Position sizing, eligibility rules, stop placement |

### Dashboard
| File | Purpose |
|------|---------|
| `dashboard/app.py` | Flask dashboard, 7 tabs (Overview, A1, A2, Intelligence, Trades, Transparency, Decision Theater) |

### Memory and learning
| File | Purpose |
|------|---------|
| `trade_memory.py` | ChromaDB vector store (3-tier: recent/medium/long) |
| `memory.py` | Decision log, performance tracking |
| `decision_outcomes.py` | Per-decision outcome log |
| `attribution.py` | PnL attribution and module ROI |
| `divergence.py` | Live vs paper divergence tracking and operating mode management |

### Options stack (A2)
| File | Purpose |
|------|---------|
| `options_data.py` | IV history, chain fetching, IV rank/percentile |
| `options_intelligence.py` | IV-first strategy selector |
| `options_builder.py` | Real-chain structure builder (strikes, expiry, contracts) |
| `options_executor.py` | Alpaca broker adapter for multi-leg orders |
| `options_state.py` | Persistence layer for OptionsStructure |
| `schemas.py` | Shared dataclasses and enums |

### Weekly review
| File | Purpose |
|------|---------|
| `weekly_review.py` | 6-agent weekly review — runs Sundays |
| `performance_tracker.py` | Shadow performance measurement (Sonnet/allocator/A2 alpha) |
| `backtest_runner.py` | Strategy backtesting harness |
| `signal_backtest.py` | Signal-level forward-return backtest |

## Prompts
System prompts are in `prompts/` — kept private (not committed). See `prompts/README.md` for structure.

## Data
Runtime data lives in `data/` — mostly gitignored. Not committed to repo.

## Environment variables
All credentials in `.env` (gitignored). Required:
- `ANTHROPIC_API_KEY`
- `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` (A1)
- `ALPACA_API_KEY_OPTIONS` / `ALPACA_SECRET_KEY_OPTIONS` (A2)
- `ALPACA_BASE_URL` — set to paper endpoint

## Tests
117 test files in `tests/`. Run with `pytest`. Approximately 2,800 tests.

## Deployment
Code is rsync'd to the VPS. Use `make deploy`.
