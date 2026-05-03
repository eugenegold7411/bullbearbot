# CLAUDE.md — BullBearBot System Brief

This file gives Claude context about the BullBearBot system architecture.
Read this at the start of every session before touching any code.

---

## Project overview

Two autonomous trading bots running on a DigitalOcean VPS (161.35.120.8, SSH alias `tradingbot`, key `~/.ssh/trading_bot`):
- **A1** — Equity bot. 7-stage pipeline driven by Claude Sonnet as the decision engine.
- **A2** — Options bot. 5-stage pipeline; single Claude call producing a structured 4-agent debate (DIRECTIONAL ADVOCATE / VOL ANALYST / TAPE SKEPTIC / RISK OFFICER).

Both bots trade **paper accounts on Alpaca** ($100k each, paper since April 13 2026). The system is fully autonomous — it reads market data, runs multi-stage Claude calls, executes trades, manages stops, and posts WhatsApp alerts every cycle.

The Flask dashboard at `dashboard/app.py` is a read-only monitoring UI that aggregates Alpaca API state, local data files, and bot log output into a set of HTML pages.

---

## Accounts and strategies

### A1 — Equity account
- Alpaca paper account (env: `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`)
- Signals → regime → decision → execution pipeline
- Positions tracked with trail-stop tiers (config in `strategy_config.json` → `exit_management.trail_tiers`)
- Mode file: `data/runtime/a1_mode.json` (`mode`: NORMAL / PAUSED / CLOSED)
- Decisions logged to: `memory/decisions.json`

### A2 — Options account
- Alpaca paper account (env: `ALPACA_API_KEY_OPTIONS` / `ALPACA_SECRET_KEY_OPTIONS`)
- IV-first strategy: IV rank/percentile drives strategy selector; builds real-chain multi-leg structures
- **4-agent debate is a SINGLE Claude API call** — not separate per-role calls. The debate text is parsed out of one response by `_a2_parse_debate()`.
- Open structures persisted to: `data/account2/positions/structures.json`
- Per-cycle decisions in: `data/account2/decisions/` (one JSON file per cycle)
- Mode file: `data/runtime/a2_mode.json`
- A2 debate runs on 15-min interval during market hours (S13 cadence change)

---

## Dashboard architecture

### Location and deployment
- Source: `dashboard/app.py` (6 232 lines, single-file Flask app — all HTML as f-strings, no Jinja templates)
- Static: `dashboard/static/theme.css` (new --bbb-* design tokens, 300 lines)
- Service name: `trading-bot-dashboard.service`
  - `WorkingDirectory`: `/home/trading-bot/dashboard` (VPS path)
  - `ExecStart`: `/home/trading-bot/.venv/bin/python3 app.py`
  - Restart: always, RestartSec: 10
  - Binds to `127.0.0.1:8080` (localhost only — no direct internet exposure)
- Bot service name: `trading-bot.service` (runs `scheduler.py`)
- **`make deploy` only restarts `trading-bot.service`** — after dashboard changes you must also run: `systemctl restart trading-bot-dashboard`

### Key constants and globals (app.py)
| Symbol | Line | Notes |
|--------|------|-------|
| `ET` | 29 | `ZoneInfo("America/New_York")` — auto-DST, replaces former `ET_OFFSET = timedelta(hours=-4)` |
| `SHARED_CSS` | 36–348 | Legacy CSS string — NOT an f-string; `{}` are literal CSS braces |
| `_COUNTDOWN_JS` | ~240 | Plain string — NOT an f-string |
| `_COMMAND_PALETTE_HTML` | 253 | Plain string — NOT an f-string; contains full modal + IIFE JS |
| `BOT_DIR` | ~25 | `Path(__file__).parent.parent` → `/home/trading-bot/` |
| `_build_status()` | 4098 | Central data assembly — called on every page request, ~25 keys |
| `_page_shell()` | 1498 | Wraps all pages; injects `_COMMAND_PALETTE_HTML` at end |
| `_bbb_build_pill()` | 606 | Build version pill with git hash + days-ago |
| `_bbb_hero_strip_html()` | ~520 | 5-pane hero strip |
| `_bbb_cycle_pulse_html()` | ~560 | Pulse strip with stage breadcrumbs |
| `_bbb_voice_strip_html()` | ~580 | Bot voice strip |
| `/api/search` route | 6129 | Fuzzy search — powers Cmd+K palette |
| `requires_auth` | 369 | **Defined but never applied** — all routes are open |

### Request pattern

Every HTML page route follows the same pattern:
1. `_build_status()` — aggregates all data (Alpaca + file reads + module imports)
2. `_now_et()` — current timestamp in Eastern
3. `_nav_html(active_page, now_et, a1_mode, a2_mode)` — nav bar HTML
4. `_page_X(status, now_et)` — page-specific body HTML
5. `_page_shell(title, nav, body, ticker)` — wraps in `<html><body>`

### Authentication
`requires_auth` is defined (line 369) but **not applied to any route** — all pages and APIs are unauthenticated. The localhost-only bind (`127.0.0.1`) provides implicit protection.

---

## Pages and routes

| Route | Handler | Function line range | Purpose |
|-------|---------|-------|---------|
| `/` | `index()` | ~2090 | Overview — hero strip, pulse strip, voice strip, positions, decisions |
| `/a1` | `page_a1()` | ~2500 | A1 detail — account stats, positions, trail stops, orders, decisions |
| `/a2` | `page_a2()` | ~3000 | A2 detail — cinematic structure cards, pipeline, closed structures, strategy perf |
| `/brief` | `page_brief()` | ~3600 | Full intelligence brief from `morning_brief_full.json` |
| `/trades` | `page_trades()` | ~3800 | Closed trades journal (win rate, average P&L, per-trade rows) |
| `/transparency` | `page_transparency()` | ~4000 | Decision transparency — stage-by-stage reasoning logs |
| `/theater` | `page_theater()` | ~4200 | Decision theater — replay any past cycle; trade lifecycle view |
| `/social` | `page_social()` | ~4400 | Social media post ideas from `data/social/post_ideas.json` |

### JSON API routes

| Route | Returns |
|-------|---------|
| `/api/status` | a1_mode, a2_mode, gate, costs, decision, git_hash, service_uptime, positions_count, today_pnl, warnings |
| `/api/briefs` | List of morning brief files from `data/market/briefs/` |
| `/api/trades` | `{trades, summary{total,wins,losses,total_pnl,win_rate}, bug_log}` |
| `/api/health` | `{all_ok, checks[]}` — 7 health checks from `health_monitor.py` |
| `/health` | Plain text `ok` — uptime probe |
| `/api/search?q=` | `{pages, symbols, trades, cycles}` — powers Cmd+K command palette |
| `/api/theater/cycle/<idx>` | Cycle detail from `decision_theater.get_cycle_view()` |
| `/api/theater/trade/<symbol>` | Trade lifecycle from `decision_theater.get_trade_lifecycle()` |
| `/api/theater/trades` | All trades summary |
| `/api/theater/cycles` | All cycles metadata |
| `/api/theater/calibration` | Calibration data |

---

## Active features

### BBB component strip (Overview page)
Three stacked strips at the top of every overview load:
- **Hero strip** (`_bbb_hero_strip_html`) — 5-pane grid: A2 Equity · Today P&L · Fill Rate · Open Structures · IV Environment.
- **Pulse strip** (`_bbb_cycle_pulse_html`) — pulsing violet dot + last cycle outcome + cost badge + stage breadcrumbs (CANDIDATES›FILTER›DEBATE›APPROVAL›FILL). Dot goes `.is-idle` when cycle is done.
- **Voice strip** (`_bbb_voice_strip_html`) — bot first-person reasoning quote, violet left border, mono text, `BOT · DATE` attribution.

### A2 cinematic structure cards (`_a2_cinematic_card_html`)
Renders each open options structure as a card with:
- Header: sym · strat · strikes · expiry · DTE/lifecycle/verdict pills
- Thesis + source chips
- 2×2 agent debate grid: DIRECTIONAL ADVOCATE / VOL ANALYST / TAPE SKEPTIC / RISK OFFICER (role-colored tints)
- Payoff bar (`_a2_payoff_bar_html`) — `max_gain=None` long calls render "unlimited ↑" in profit green
- MTM P&L, greeks
- Supporting helpers: `_a2_parse_debate()`, `_a2_snip()`, `_a2_vote_tally()`, `_a2_match_decision()`, `_a2_vpill()`, `_a2_closed_structures()`

### Command palette (Cmd+K / Ctrl+K)
`_COMMAND_PALETTE_HTML` constant (app.py line 253) injects a 600px modal + JS IIFE:
- Key JS functions: `cinit()`, `copen()`, `cclose()`, `cfetch(q)`, `cfz()` (fuzzy), `crender()`, `cupd()`, `cmove()`, `cgo()`
- Uses `data-ci` attributes, `.bcr` and `.bcr-r` CSS classes
- Hits `/api/search` with 150ms debounce, returns pages/symbols/trades/cycles
- Footer: "↵ go · ↑↓ navigate · esc close"

### Build pill (`_bbb_build_pill()` at line 606)
Replaces raw "Git HEAD: hash" in System & Performance footer with: `v0.4.2 · {short_hash} · shipped {N}d ago`. Runs two subprocess calls to `/usr/bin/git` (full path — service may have sparse PATH). Uses `--bbb-surface-2` background, 11px mono, 4px radius. Falls back to "build unknown" on any exception.

### Entrance animations (`bbbFlash`)
`bbbFlash(el)` utility appended to `_COMMAND_PALETTE_HTML` script block: opacity 0.4→1.0 over 200ms ease. Wired to `visibilitychange` targeting `.bbb-hero-number` class.
**Known mismatch:** existing hero elements use `.bbb-hero-num` and `.bbb-hero-num-sm` — not `.bbb-hero-number`. Flash currently has no live targets.

### Sparklines
`_bbb_sparkline_svg(values, color, width, height)` renders inline SVG mini-charts for A1 equity, A1 daily P&L, A2 equity, A2 P&L, Claude cost.

### Positions with trail stops
A1 positions table: symbol, qty, entry, current price, unrealized P&L, % of capital, stop price, gap-to-stop %, earnings flag. Oversize thresholds: >25% = `critical`, >20% = `core`, >15% = `dynamic`.

### Warning system
`_build_warnings(status)` generates critical alerts for: A1/A2 mode not NORMAL, A2 consecutive duplicate-blocks, Alpaca API errors, oversize positions, positions within 2% of their stop, positions with upcoming earnings.

### Decision theater
Full cycle replay using `decision_theater.py`. Loads at `/theater`; initialises to last filled cycle (`find_last_filled_cycle_index()`). JS fetches stage detail via `/api/theater/cycle/<idx>`.

### Premarket health check (S14)
6-check WhatsApp health summary at 9:00 AM ET via `premarket_health.py`. Checks: Alpaca A1/A2 connectivity, VIX, overnight gap, macro wire, open structures count.

### Post-execution verifier (S15)
`_attempt_post_execution_verify()` verifies Alpaca state after every A1/A2 submission. Partial spread 3-strike repair wired; `repair_attempt_count` field in OptionsStructure.

### Bottom ticker bar
`_build_ticker_html(positions)` — fixed bottom bar showing open A1 positions with current price and unrealized P&L. Truncated to 8 positions.

### Intelligence brief
`/brief` reads `data/market/morning_brief_full.json`. Generated 8x/day by `morning_brief.py`. Displays: macro backdrop, earnings, insider activity, signal context, qualitative catalyst summaries.

---

## Design system

Two CSS systems coexist in the dashboard. **Do not mix them.**

### Legacy system — `SHARED_CSS` in `app.py` lines 36–348
Applies to all existing page components. Token namespace: `--bg-*`, `--accent-*`, `--text-*`.
`SHARED_CSS` is a plain string, **not an f-string** — `{}` are literal CSS braces, not Python interpolation.

| Token | Value | Use |
|-------|-------|-----|
| `--bg-base` | `#0d0e1f` | Page background |
| `--bg-card` | `#10112a` | Card background |
| `--border` | `#1e2040` | Borders |
| `--text-primary` | `#e8ecff` | Primary text |
| `--text-secondary` | `#c8d0e8` | Body text |
| `--text-muted` | `#4a5080` | Labels, secondary |
| `--accent-blue` | `#4facfe` | Links, active nav, info |
| `--accent-green` | `#00e676` | Gains, NORMAL mode |
| `--accent-red` | `#ff5050` | Losses, errors |
| `--accent-amber` | `#ffaa20` | Warnings, PAUSED mode |
| `--accent-purple` | `#a855f7` | A2 / options |

### New design system — `dashboard/static/theme.css`
Token namespace: `--bbb-*`. **Only** used by: `.bbb-hero-strip`, `.bbb-pulse-strip`, `.bbb-voice-strip`, cinematic A2 cards. **Never** apply `--bbb-*` tokens to existing page components.

| Token | Value | Use |
|-------|-------|-----|
| `--bbb-bg` | `#0B0D14` | Page background |
| `--bbb-surface` | `#13151D` | Default card body |
| `--bbb-surface-2` | `#181B26` | Hover / elevated |
| `--bbb-border` | `#1F2330` | Hairline borders |
| `--bbb-fg` | `#E8EAF0` | Primary text |
| `--bbb-fg-muted` | `#7B8090` | Labels, metadata |
| `--bbb-fg-dim` | `#4A4F60` | Disabled, footnote |
| `--bbb-profit` | `#34D399` | Gains |
| `--bbb-loss` | `#F87171` | Losses |
| `--bbb-warn` | `#FBBF24` | Caution / stale |
| `--bbb-info` | `#60A5FA` | Info / neutral |
| `--bbb-ai` | `#9B5DE5` | **AI accent — violet** |
| `--bbb-ai-soft` | `rgba(155,93,229,.12)` | AI background |
| `--bbb-ai-border` | `rgba(155,93,229,.30)` | AI border |

**AI accent rule**: `--bbb-ai` is reserved exclusively for: cycle-pulse dot, agent avatars, Sonnet reasoning panels, bot voice strip. **Never** on nav, borders, buttons, or fills.

**Type scale**: `--bbb-t-hero` 52px, `--bbb-t-hero-sm` 44px, `--bbb-t-h2` 24px, `--bbb-t-h3` 16px, `--bbb-t-body` 14px, `--bbb-t-reasoning` 13px, `--bbb-t-label` 12px, `--bbb-t-caption` 11px.

**Weights**: 400 and 500 only. Never 600 or 700 in new components.

**Spacing grid** (4px base): `--bbb-s-1` 4px → `--bbb-s-7` 48px.

**Radii**: `--bbb-r-1` 2px, `--bbb-r-2` 4px, `--bbb-r-3` 6px, `--bbb-r-4` 8px.

**Motion**: one pulse animation (`bbb-ai-pulse`, 1.5s, only on `.bbb-pulse-dot`). One fade-in (`bbb-num-tick`, 200ms, on number update). No shadows, gradients, or glow.

**Fonts**: `--bbb-font-sans` = Inter; `--bbb-font-mono` = JetBrains Mono.

---

## Caching

In-memory cache with TTL via `@_cached(key, ttl)` decorator (single-process, resets on restart):

| Cache key | TTL | Source |
|-----------|-----|--------|
| `a1` | 60 s | Alpaca A1 account + positions + orders |
| `a2` | 60 s | Alpaca A2 account + positions + orders |
| `pnl_a1` | 60 s | A1 today's P&L calculation |
| `pnl_a2` | 60 s | A2 today's P&L calculation |
| `trades` | 300 s | Closed trades via `trade_journal.py` |

All other data (file reads, module imports) is loaded fresh on each `_build_status()` call.

---

## Data sources and file locations

All paths are relative to `BOT_DIR` (= `dashboard/../` = the trading-bot root).

### Real-time (Alpaca API)
- A1: equity, buying_power, positions, orders, today's fills
- A2: same, for options account

### File-based (read each request or cached)

| File | Content | Consumer |
|------|---------|----------|
| `memory/decisions.json` | A1 decision log (all cycles) | `_last_decision()`, `_last_n_a1_decisions()`, `/api/search` |
| `data/account2/decisions/` | A2 per-cycle decision files | `_last_n_a2_decisions(50)`, `_a2_last_cycle()` |
| `data/account2/positions/structures.json` | Open A2 options structures | `_a2_structures()`, `_a2_closed_structures()` |
| `data/market/morning_brief.json` | Brief summary (short form) | `_morning_brief()`, overview page |
| `data/market/morning_brief_full.json` | Full intelligence brief | `_intelligence_brief_full()`, `/brief` page |
| `data/market/earnings_calendar.json` | Upcoming earnings by symbol | `_earnings_flags()` |
| `data/market/qualitative_context.json` | Per-symbol catalyst summaries | `_qualitative_context()` |
| `data/market/gate_state.json` | Sonnet gate state | `_build_status()` |
| `data/runtime/a1_mode.json` | A1 operating mode | `_build_status()`, warnings |
| `data/runtime/a2_mode.json` | A2 operating mode | `_build_status()`, warnings |
| `data/costs/daily_costs.json` | Daily API cost totals | `_build_status()`, overview |
| `data/analytics/cost_attribution_spine.jsonl` | Per-module cost detail (signal + regime written here since S17B) | Cost breakdown widget |
| `data/account2/costs/cost_log.jsonl` | A2 cost log | A2 cost widget |
| `data/analytics/portfolio_allocator_shadow.jsonl` | Allocator shadow P&L | `_allocator_shadow_compact()` |
| `data/analytics/near_miss_log.jsonl` | Near-miss event log | Transparency page |
| `data/reports/shadow_status_latest.json` | Shadow performance summary | `_build_status()` |
| `data/status/preflight_log.jsonl` | Preflight market check results | Overview, A1 page |
| `data/social/post_ideas.json` | Social post queue | `/social` page |
| `data/market/briefs/` | Historical brief archive | `/api/briefs` |
| `strategy_config.json` | Trail tiers, veto thresholds, config | `_build_status()`, warnings |
| `logs/trades.jsonl` | Today's trade events | `_todays_trades()` |
| `logs/bot.log` | Bot runtime log | `_recent_errors()` (last 300 lines, max 5 errors) |

### Module imports (lazy, with `sys.path.insert`)
- `trade_journal.py` → `build_closed_trades()` — powers `/trades` and `/api/trades`
- `decision_theater.py` → `get_cycle_view()`, `get_trade_lifecycle()`, `get_calibration_data()`, `get_all_cycles_metadata()`, `get_all_trades_summary()`, `find_last_filled_cycle_index()` — powers `/theater`
- `health_monitor.py` → `get_health_status()` — powers `/api/health`
- `performance_tracker.py` → `load_performance_summary()` — powers A2 strategy perf section

---

## Known issues and status

| Issue | Status | Detail |
|-------|--------|--------|
| `ET_OFFSET` hardcoded to `-4` (EDT) | **Fixed** | Replaced with `ET = ZoneInfo("America/New_York")` at line 29. Auto-DST — no manual change needed in November. |
| `requires_auth` defined but unused | **Active** | Defined at line 369, never applied to any `@app.route`. Dashboard is protected only by localhost bind. |
| `bbbFlash()` class mismatch | **Active** | Flash targets `.bbb-hero-number` but actual elements use `.bbb-hero-num` / `.bbb-hero-num-sm`. Animation has no live targets. |
| `.bbb-stage.is-active` not wired | **Active** | CSS class exists in theme.css for live stage tracking; JS to push live stage state into pulse strip is not implemented. All stages show `.is-done` retroactively. |
| Two coexisting CSS systems | **By design** | `SHARED_CSS` (legacy) and `--bbb-*` (new). Must not be mixed. |
| Single-process in-memory cache | **By design** | Cache resets on service restart. No shared state between workers (single-process Flask). |
| `morning_brief.json` stale | **Fixed** | Brief file is now written directly. See `project_morning_brief_bug.md` in memory. |
| Git safe.directory on VPS | **Fixed** | Service runs as root but repo owned by uid 501. Fixed by: `git config --system --add safe.directory /home/trading-bot` (writes to `/etc/gitconfig`). Do NOT use `--global` — it writes to `/root/.gitconfig` and may not apply when HOME differs. |
| BUG-015: GOOGL bracket TP leg canceled | **Active** | GOOGL limit sell leg canceled at submission 5/1 10:47 AM. Bot is running without TP on GOOGL position. Investigate why bracket limit leg is being auto-canceled — possible OCO order type issue or Alpaca paper account restriction on bracket submissions. |

---

## Core pipeline files

### A1 — Equity bot
| File | Purpose |
|------|---------|
| `bot.py` | A1 entry point and cycle orchestration |
| `bot_stage0_precycle.py` | Pre-cycle infrastructure (positions, market data, exit audit) |
| `bot_stage1_regime.py` | Regime classifier (Haiku) — risk_on / risk_off / caution |
| `bot_stage1_5_qualitative.py` | Qualitative context sweep (Haiku) — per-symbol catalyst synthesis |
| `bot_stage2_signal.py` | Signal scorer (Haiku) — L2 Python anchors + L3 Haiku synthesis |
| `bot_stage2_python.py` | L2 pure-Python signal computation (no API calls) |
| `bot_stage2_5_scratchpad.py` | Scratchpad pre-analysis (Haiku) |
| `bot_stage3_decision.py` | Main Sonnet decision call |
| `bot_stage4_execution.py` | Post-decision execution wiring |

### A2 — Options bot
| File | Purpose |
|------|---------|
| `bot_options.py` | A2 cycle entry point |
| `bot_options_stage0_preflight.py` | Reconcile open structures |
| `bot_options_stage1_candidates.py` | Candidate generation from A1 signal scores |
| `bot_options_stage2_structures.py` | Structure routing (12 rules) and veto gates |
| `bot_options_stage3_debate.py` | 4-agent debate (single Sonnet call, parsed into 4 roles) |
| `bot_options_stage4_execution.py` | Submit multi-leg structures to Alpaca |

### Scheduler and intelligence
| File | Purpose |
|------|---------|
| `scheduler.py` | 24/7 scheduler — session tiers, all maintenance jobs |
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

### Memory and learning
| File | Purpose |
|------|---------|
| `trade_memory.py` | ChromaDB vector store (3-tier: recent/medium/long) |
| `memory.py` | Decision log, performance tracking |
| `decision_outcomes.py` | Per-decision outcome log |
| `attribution.py` | PnL attribution and module ROI |
| `divergence.py` | Live vs paper divergence tracking and operating mode management |

### Options stack
| File | Purpose |
|------|---------|
| `options_data.py` | IV history, chain fetching, IV rank/percentile |
| `options_intelligence.py` | IV-first strategy selector |
| `options_builder.py` | Real-chain structure builder (strikes, expiry, contracts) |
| `options_executor.py` | Alpaca broker adapter for multi-leg orders |
| `options_state.py` | Persistence layer for OptionsStructure |
| `schemas.py` | Shared dataclasses and enums (A2CandidateSet, A2DecisionRecord) |

### Weekly review and analysis
| File | Purpose |
|------|---------|
| `weekly_review.py` | 6-agent weekly review — runs Sundays |
| `performance_tracker.py` | Shadow performance measurement (Sonnet/allocator/A2 alpha) |
| `backtest_runner.py` | Strategy backtesting harness |
| `signal_backtest.py` | Signal-level forward-return backtest |

---

## Environment variables

All credentials in `.env` (gitignored):
- `ANTHROPIC_API_KEY`
- `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` — A1 equity account
- `ALPACA_API_KEY_OPTIONS` / `ALPACA_SECRET_KEY_OPTIONS` — A2 options account
- `ALPACA_BASE_URL` — paper trading endpoint
- `DASHBOARD_USER` / `DASHBOARD_PASSWORD` — dashboard basic auth (default: admin/bullbearbot)
- `DASHBOARD_PORT` — dashboard port (default: 8080)

---

## Tests and deployment

- Test suite: `tests/` — ~117 files, ~1 814 tests (as of S17B). Run with `pytest`.
- Deploy: `make deploy` — rsync to VPS then `systemctl restart trading-bot.service` only.
  - **After any `dashboard/app.py` change:** also run `systemctl restart trading-bot-dashboard`
- `strategy_config.json` is **excluded from deploy** (Makefile rsync exclude) — it lives on the VPS only and should not be overwritten.
- Server address: `161.35.120.8`; SSH alias: `tradingbot`; key: `~/.ssh/trading_bot`

---

## Phased dashboard build — status

### Completed
- **Phase 1** — Basic overview, A1, A2 pages with Alpaca account data
- **Phase 2** — Intelligence brief page (`/brief`), trade journal (`/trades`), transparency log (`/transparency`)
- **Phase 3** — Decision theater (`/theater`) with full cycle replay and trade lifecycle view
- **Phase 4** — BBB design system tokens (`--bbb-*`) in `static/theme.css`; Hero strip, Pulse strip, Voice strip components wired to Overview page
- **Phase 5** — A2 page rebuild: cinematic thesis cards, hero strip, pipeline section, closed structures section (all old A2 list-style sections removed)
- **Phase 6** — Command palette (`Cmd+K`), `/api/search`, social post queue (`/social`), sparkline SVGs, performance summary widgets, build pill, entrance animations

### Pending / not yet started
- **Phase 7** — Live-stage breadcrumb tracking: `.bbb-stage.is-active` class exists in CSS but the JS to push live stage state into the pulse strip is not wired up.
- **Phase 8** — Auth enforcement: apply `requires_auth` to all routes.
- **Phase 9** — EST/EDT auto-detection — **Done**: `ET = ZoneInfo("America/New_York")` replaces all `ET_OFFSET` usages; JS offset injected from Python on each render.

---

## Patterns and gotchas

### Commit messages
Never include a `Co-Authored-By` line in any commit message under any circumstances. No exceptions.

### String vs f-string
`SHARED_CSS`, `_COUNTDOWN_JS`, and `_COMMAND_PALETTE_HTML` are plain Python strings — they contain literal `{}` CSS/JS braces. **Never convert them to f-strings.** All Python interpolation happens in the page functions that embed them.

### mock.patch module namespace binding
When patching a function in tests, patch it where it is **defined**, not where it is imported. If `bot_stage2_signal.py` does `from market_data import get_prices`, patch `bot_stage2_signal.get_prices`, not `market_data.get_prices`. See `feedback_mock_patch_module_namespace.md` in memory.

### A2 single-call architecture
The 4-agent debate is one Claude API call. `bot_options_stage3_debate.py` sends a single prompt instructing the model to produce output from all four roles. `_a2_parse_debate()` in `app.py` splits the raw text into per-role sections. Any refactor that splits this into 4 calls would require major scheduler and cost attribution changes.

### Market session start
Market cycle starts at **9:10 AM ET** (not 9:30 AM) — 20-minute pre-open window for setup (S13 cadence change).

### strategy_config.json
Never commit or deploy this file — it is managed on the VPS. The Makefile rsync excludes it. If you need to inspect it locally, copy from the VPS; do not overwrite the VPS copy.

### dashboard app.py f-string escaping
All f-strings in `app.py` that produce HTML/JS must escape literal braces as `{{` and `}}`. The large CSS block at the top (`SHARED_CSS`) is a plain string and never uses `f""` prefix — adding one would break every CSS rule.

---

## Signal Balance — known observations

**Insider/congressional signal skew**: The signal scorer (`bot_stage2_signal.py`) assigns weight to insider and congressional trade signals. Observed that this signal category tends to dominate high-conviction scores when active, potentially crowding out fundamental and technical signals. This is a known observation — do not adjust scoring weights without a deliberate strategy session. Document any weight changes here with rationale and date.
