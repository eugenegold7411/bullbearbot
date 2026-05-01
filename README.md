# BullBearBot

An autonomous AI trading system built on Claude. Two bots running simultaneously — A1 for equity, A2 for options. Paper trading April 16 – May 16, 2026. Everything logged, everything honest including the bugs.

**Live dashboard:** [161.35.120.8:8080](http://161.35.120.8:8080) — updates every 60 seconds

![Paper trading](https://img.shields.io/badge/status-paper%20trading-yellow)
![Claude Sonnet 4.6](https://img.shields.io/badge/AI-Claude%20Sonnet%204.6-blue)
![Tests](https://img.shields.io/badge/tests-2791%20passing-green)

---

## What this is

BullBearBot uses Claude Sonnet 4.6 as the primary decision engine for autonomous equity and options trading. Every 3 minutes during market hours, the system runs a full pipeline: synthesize news, score 91 symbols, classify market regime, update a scratchpad of active theses, reconcile conviction across three sources, then call Sonnet to make buy/hold/sell decisions against an open portfolio.

In parallel, a second bot (A2) runs an options pipeline — identifying IV opportunities, routing to one of 12 strategy templates, and running a 4-agent bounded debate (Bull, Bear, IV Analyst, Structure Judge) before submitting any structure.

This is a hobby project. The goal is to learn how far Claude can go as a financial decision engine, and to document every failure honestly.

---

## Architecture

### A1 — Equity decision cycle (~3 min)

```
News synthesis        91-symbol signal scoring      Regime classification
(macro wire,    →    (Haiku, 2 parallel batches,  →  (Haiku, risk_on /
qualitative sweep)    technical + qualitative)         risk_off / caution)
    ↓
Scratchpad            Conviction reconciliation     Sonnet gate
pre-analysis    →    (brief + signal + scratchpad  →  (VIX bands, session,
(Haiku)              merged into single table)         pattern triggers)
    ↓
Sonnet decision       Risk kernel                   Order execution
(Claude Sonnet  →    (position sizing, stop     →   (Alpaca API, OCA
4.6, ~13K tok)       placement, eligibility)        bracket orders)
```

### A2 — Options pipeline (parallel, every cycle)

```
IV data refresh → Preflight (cancel unfilled) → Candidate generation →
Routing rules (12 strategies) → Structure builder →
4-agent bounded debate → Execution (Alpaca multi-leg)
```

### Supporting systems

- **Morning intelligence brief** — generated 8x/day (4AM premarket, 9:25AM open, hourly 10:30–3:30PM). 10 sections: market regime, sector snapshot, 20 high-conviction longs, 10 bearish picks, earnings pipeline, insider activity, macro wire, watchlist.
- **Portfolio allocator** — shadow mode, produces ADD/TRIM/REPLACE recommendations that Sonnet can act on. Not yet in live control.
- **Shadow performance tracker** — measures whether Sonnet conviction, allocator recommendations, and A2 routing rules are actually producing alpha. Nightly computation, weekly Haiku report.
- **Weekly board meeting** — Sunday, 6 agents review the week and set priorities.
- **Dashboard** — 7 tabs: Overview, A1 Equity, A2 Options, Intelligence Brief, Trades, Transparency, Decision Theater.

---

## Tech stack

| Component | Technology |
|-----------|-----------|
| Decision engine | Claude Sonnet 4.6 (`claude-sonnet-4-6`) |
| Signal scoring | Claude Haiku 4.5 (`claude-haiku-4-5-20251001`) |
| Broker | Alpaca Paper Trading API |
| Market data | Alpha Vantage, yfinance |
| Earnings transcripts | EDGAR SEC API (real 8-K filings) |
| Insider activity | SEC Form 4 via EDGAR EFTS |
| Infrastructure | DigitalOcean VPS, $12/month |
| Dashboard | Flask, pure CSS dark theme |
| Memory | ChromaDB vector store |
| Testing | pytest, 2,791 tests |
| Language | Python 3.12 |

---

## Performance — paper trading

| Metric | Value |
|--------|-------|
| Start date | April 16, 2026 |
| Starting capital | $100,000 |
| Current equity | $100,582 |
| Realized P&L | +$1,764.52 |
| Win rate | 66.7% (16W / 8L) |
| Total trades | 24 |
| Bug-impacted trades | 2 |

*Updated daily. Honest — including bug-impacted trades.*

---

## How it makes decisions

Every decision cycle, Sonnet receives a prompt containing:

1. **Conviction table** — reconciled from three sources: the morning intelligence brief (hourly), the signal scorer (every cycle), and the scratchpad pre-analysis (every cycle). When sources conflict, explicit priority rules determine which wins.

2. **Regime context** — current market regime score (0–100), VIX level, sector leaders/laggards, session theme.

3. **Live positions** — current holdings with P&L, trail tier status, and stops.

4. **EDGAR transcripts** — real earnings press releases (24K–30K chars) from the SEC for recently-reporting symbols. Not summaries — the actual documents.

5. **Macro wire** — breaking macro events with urgency classification.

6. **Signal scores** — top signals from the 91-symbol universe with direction and catalyst.

Sonnet produces a structured JSON response with ideas (symbol, intent, conviction, tier, catalyst, stop, target). The risk kernel validates each idea against eligibility rules before any order is submitted.

See the [Decision Theater tab](http://161.35.120.8:8080/theater) for an interactive view of any past decision cycle.

---

## What we built — development log

| Date | Feature | Impact |
|------|---------|--------|
| Apr 16 | Initial bot — A1 equity with basic signal scoring | First paper trade |
| Apr 16–22 | Trail stop system — T1/T2/T3 tiers with ratcheting stops | GOOGL +10% captured |
| Apr 23 | OCA bracket orders — stop + take-profit simultaneously | Eliminated naked positions |
| Apr 24 | ChromaDB vector memory — thesis persistence across sessions | Scratchpad coherence |
| Apr 25 | A2 options bot — first debit spread submitted | GOOGL $380/$385 filled |
| Apr 26 | 4-agent debate — Bull/Bear/Analyst/Judge for A2 structures | Confidence calibration |
| Apr 27 | Regime classifier — Haiku classifies market regime each cycle | Context-aware decisions |
| Apr 28 | Signal scorer expansion — 30 → 91 symbols, 2-batch parallel | Full universe coverage |
| Apr 29 | Intelligence brief redesign — 8x/day, 10 sections, conviction table | Hourly fresh context |
| Apr 30 | RULE1/RULE4 smart routing — earnings/macro as opportunities not blocks | A2 post-earnings trades |
| Apr 30 | EDGAR transcript fix — real Q1 2026 earnings data for 5 major symbols | Real earnings analysis |
| Apr 30 | Conviction reconciliation — single merged table from 3 sources | Eliminated anchoring |
| Apr 30 | VIX graduated gate — 4-band system, bearish entries never blocked | Correct crisis behavior |
| Apr 30 | Shadow performance tracker — measuring Sonnet/allocator/A2 alpha | Data for live promotion |
| Apr 30 | Dashboard redesign — dark navy, 7 tabs, Decision Theater | Full transparency |

---

## Bug log

Every bug we found, what caused it, what it cost, and what we learned. All fixed the same day they were found.

### BUG-OCA-001 · HIGH · Apr 13–Apr 30
**Bracket orders silently unprotected (stop coverage gap)**

Alpaca bracket orders produce two OCA children after fill — the stop-loss child enters `held` status and is invisible to `status=open` queries. `exit_manager` saw only the take-profit limit child and assumed the stop was absent, repeatedly placing duplicate stops. The OCA group prevented them from filling. Result: 68 consecutive SELL orders rejected over 3+ hours. Positions were unprotected.

*Fixed: added `execute_trim_with_stop_management()` that cancels OCA stops before any sell. Commit `4c40616`.*

*Learning: OCA groups must be explicitly cancelled before submitting any new sell order against the same position. Never assume stop status from order queries alone.*

---

### BUG-007 · HIGH · Apr 13–Apr 15
**Trail stop never fired (enum serialization bug)**

`OrderType.STOP` serialized as `'ordertype.stop'` not `'stop'`. The string comparison in `get_active_exits()` always failed, so `stop_price` stayed `None` and trail stops never updated. GLD was the primary affected position.

*Fixed: explicit `.value` extraction on all OrderType enums. Commit `d44d68f`.*

*Learning: Always verify enum serialization in API response parsing. Print the raw value before assuming equality.*

---

### BUG-DENOM-001 · HIGH · Apr 12–Apr 30
**Position sizing denominator error (utilization overcount)**

`compute_position_health()`, `SIZE TRIM` trigger, and `account_pct` all used wrong denominator for utilization. Equity-only denominator understated total capacity when buying power was high. AMZN appeared as 40.2% of account (correct: 15.1% of `total_capacity`), triggering a false SIZE TRIM exit. TSM was simultaneously false-exited.

*Fixed: `total_capacity = equity + buying_power` everywhere. Commits `d44d68f`, `7244077`.*

*Learning: On margin accounts, always denominate position size against total capital (equity + buying power), not equity alone. Denominator bugs cascade silently through sizing, display, and enforcement simultaneously.*

---

### BUG-008 · MEDIUM · Apr 13–Apr 15
**BTC/USD HOLD emitting signal score as stop_loss**

Claude occasionally output the signal score integer (0–100) into the `stop_loss` field of a BTC/USD HOLD action. The price-scale guard in `order_executor.py` caught and discarded values < $1,000 for crypto.

*Fixed: explicit output schema validation. Commit `4d4ed99`.*

---

### BUG-014 · MEDIUM · Apr 13–Apr 16
**Deadline exits used limit order instead of market order**

When `exit_manager` detected an expired deadline (CRITICAL priority), it emitted `action_type='close_all'` → `execute_reconciliation_plan()` placed a limit order — not guaranteed to fill at deadline.

*Fixed: deadline exits now use market orders. Commit `55dac86`.*

---

### BUG-EDGAR-001 · MEDIUM · Apr 30
**EDGAR earnings transcripts returning 207-char stubs**

`_search_8k_filings()` was reading `_source.accession_no` (field doesn't exist in EDGAR EFTS — it's `adsh`) and `_source.entity_id` (it's `ciks[0]`). Every EDGAR search returned zero hits and silently fell through to a yfinance stub. GOOGL, AMZN, MSFT, META, AAPL were all trading without real earnings analysis.

*Fixed: correct field names, submissions API path for known CIKs, EX-99.1 exhibit finder. Commit `1dcb009`.*

*Before: GOOGL 212 chars. After: 24,571 chars of real Q1 2026 press release.*

---

## Cost

Running an autonomous AI trading system on Claude API + Alpaca paper trading:

| Component | Cost |
|-----------|------|
| Claude API (signal scoring) | ~$7.71/day |
| Claude API (qualitative sweep) | ~$0.21/day |
| Claude API (intelligence brief) | ~$0.40/day |
| Claude API (Sonnet decisions) | ~$2.70/day |
| Claude API (other) | ~$2.00/day |
| **Total API** | **~$13/day · ~$390/month** |
| DigitalOcean VPS | $12/month |
| **Total** | **~$402/month** |

Note: Haiku 4.5 does not support prompt caching. Sonnet 4.6 caches correctly (system prompt ~4,000 tokens cached after first call).

---

## What's not in this repo

| Content | Status | Reason |
|---------|--------|--------|
| `prompts/system_v1.txt` | Private | Full trading strategy — see `prompts/README.md` for structure |
| `prompts/system_options_v1.txt` | Private | Same |
| `prompts/user_template_v1.txt` | Private | Same |
| `CLAUDE.private.md` | Private | Contains operational details |
| `data/` | Gitignored | Runtime state |
| `citrini_positions.json` | Private | Third-party research |
| `.env` | Gitignored | API keys |
| Everything else | **Public** | |

---

## Running it yourself

```bash
git clone https://github.com/eugenegold7411/bullbearbot
cd bullbearbot
cp .env.example .env
# Add: ANTHROPIC_API_KEY, ALPACA_API_KEY, ALPACA_SECRET_KEY
pip install -r requirements.txt
python bot.py            # A1 equity bot
python bot_options.py    # A2 options bot (separate process)
python dashboard/app.py  # Dashboard on :8080
```

Note: you supply your own prompts in `prompts/`. The architecture is public; the strategy is yours to build.

---

## Weekly updates

Posted every Sunday after the weekly board meeting. What the bot did, what broke, what we learned.

*Week 1 (Apr 16–Apr 30): See [SPRINT1–6 findings](docs/).*

---

## License

MIT — do what you want with the architecture. The prompts are mine.

---

Built by Eugene Gold · April 2026  
Irvine, California  
[Dashboard](http://161.35.120.8:8080) · [GitHub](https://github.com/eugenegold7411/bullbearbot)
