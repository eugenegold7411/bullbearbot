# BullBearBot

Autonomous AI trading bot running on a cloud VPS. Trades Alpaca Paper accounts using Claude as its
decision engine. Two parallel accounts run continuously:

- **Account 1** — equities, ETFs, crypto (BTC/ETH) via a four-stage pipeline
- **Account 2** — options only, IV-first strategy with a four-way Claude debate

**Status:** Paper trading only. Launch date: 2026-04-13.

---

## Install

```bash
python3.12 -m venv .venv
source .venv/bin/activate
make install        # pip install -e . -r requirements-dev.txt
```

Copy `.env` to the project root (never commit it). See `CLAUDE.md §Environment Variables` for the
full variable list. Required at minimum:

```
ALPACA_API_KEY / ALPACA_SECRET_KEY
ALPACA_API_KEY_OPTIONS / ALPACA_SECRET_KEY_OPTIONS
ALPACA_BASE_URL=https://paper-api.alpaca.markets
ANTHROPIC_API_KEY
```

---

## Run One Cycle

```bash
python3 bot.py           # Account 1 — equities/ETF/crypto
python3 bot_options.py   # Account 2 — options
```

## Run Scheduler (24/7)

```bash
python3 scheduler.py             # full production loop
python3 scheduler.py --dry-run   # log cycles without trading
```

## Run Tests

```bash
make test   # pytest tests/
```

## Run Lint / Format

```bash
make lint     # ruff check .
make format   # ruff format .
```

## Import Check (CI-safe)

```bash
make import-check   # verifies core module imports without runtime side effects
```

## Preflight / Validate Config

```bash
python3 validate_config.py   # all-gates health check — run before deploying changes
```

---

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full architecture summary.

**Account 1 — four-stage pipeline per cycle:**

| Stage | Component | Model |
|-------|-----------|-------|
| 0 | Pre-cycle infra (account, exits, portfolio intel, macro) | — |
| 1 | Regime classifier | Haiku |
| 2 | Signal scorer + scratchpad (2.5) | Haiku |
| 3 | Main decision (gated by `sonnet_gate.py`) | Sonnet |
| 4 | Execution via `order_executor.py` → Alpaca | — |

**Cycle intervals:** 5 min (market hours) · 15 min (extended) · 30 min (overnight)

**Account 2** runs 90 s after every Account 1 market-hours cycle, reading A1's signal scores.
No separate market data fetch; IV-first strategy selection → four-way debate → options builder → Alpaca.

---

## Deployment / Server Workflow

Full server ops are documented in `CLAUDE.md §Server`. Quick reference:

```bash
# Push local changes to VPS
rsync -avz -e 'ssh -i ~/.ssh/trading_bot' \
  --exclude .venv --exclude __pycache__ --exclude '*.pyc' \
  --exclude .env --exclude 'logs/*.log' --exclude 'logs/*.jsonl' \
  --exclude nohup.out \
  /Users/eugene.gold/trading-bot/ tradingbot:/home/trading-bot/

# Service management
ssh tradingbot 'systemctl status trading-bot'
ssh tradingbot 'systemctl restart trading-bot'
ssh tradingbot 'journalctl -u trading-bot -f'
```

**Server:** DigitalOcean VPS · `161.35.120.8` · SSH alias `tradingbot` · Python 3.12 ·
venv at `/home/trading-bot/.venv/` · systemd service `trading-bot.service` (auto-restarts, 30s backoff).

**Workflow:** Edit locally (or via Claude Code) → `rsync` push → verify on server.
`.env`, `logs/`, and `data/` are excluded from sync. Never edit files directly on the server
for anything non-trivial.

---

## Weekly Review

```bash
python3 weekly_review.py                              # full 11-agent review (Sundays)
./board_meeting.sh 'reason'                           # emergency review
python3 weekly_review.py --emergency --reason '...'   # same, programmatic
```

---

## Configuration

`strategy_config.json` — runtime parameters. Written by the weekly review's Strategy Director.
Key sections: `active_strategy`, `parameters`, `time_bound_actions`, `exit_management`, `account2`.

Feature flags live in `strategy_config.json` under `feature_flags`, `shadow_flags`, `lab_flags`.

---

## v2 Roadmap

Feature roadmap (`F001`–`F016` and beyond) is tracked in `CLAUDE.md §Feature Roadmap`.
Notable upcoming: Reddit credentials (F013), cost optimization (F014), Account 3 aggressive
momentum (F015).

---

## Key Files at a Glance

| File | Role |
|------|------|
| `bot.py` | Account 1 main loop |
| `bot_options.py` | Account 2 options cycle |
| `scheduler.py` | 24/7 session manager |
| `risk_kernel.py` | All position sizing + risk policy (authoritative) |
| `order_executor.py` | A1 Alpaca order submission (backstop validation) |
| `exit_manager.py` | Stop management and trail logic |
| `strategy_config.json` | Runtime parameters (Strategy Director writes) |
| `validate_config.py` | Preflight health check |
| `CLAUDE.md` | Complete project context — read before touching any file |
| `ARCHITECTURE.md` | Architecture summary |

---

## Caution

This repo has a **live production scheduler** running 24/7 on the VPS. Read `CLAUDE.md` and
`ARCHITECTURE.md` fully before making changes. Run `make lint` and `make import-check` locally
before syncing to the server. For policy changes, see `CONTRIBUTING.md`.
