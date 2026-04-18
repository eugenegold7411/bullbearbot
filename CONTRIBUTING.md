# Contributing

---

## Development Workflow

This repo has a live production scheduler running 24/7 on a remote VPS. The workflow is:

1. **Edit locally** at `/Users/eugene.gold/trading-bot/` (source of truth)
2. **Verify locally** — run lint and import-check before touching the server
3. **Sync to server** via rsync (see `CLAUDE.md §Server` for exact commands)
4. **Verify on server** — confirm the service restarts cleanly and logs look healthy

```bash
# Local verification (required before every sync)
make lint
make import-check

# After rsync push, verify service on server
ssh tradingbot 'systemctl status trading-bot'
ssh tradingbot 'tail -20 /home/trading-bot/logs/bot.log'
```

For single-file edits, the scp workflow in `CLAUDE.md §Single-file edit workflow` is faster
than a full rsync.

---

## Before Merging / Syncing

All changes must pass:

- `make lint` — ruff check passes with no errors
- `make import-check` — core modules import cleanly

If you've added a new module that should be import-safe, add it to the `import-check` target
in `Makefile`. Hold off on adding `bot`, `order_executor`, or `weekly_review` until import-safety
for those has been explicitly verified.

Run `python3 validate_config.py` before deploying anything that touches `strategy_config.json`,
feature flags, or data directory layout. This runs all preflight gates and writes
`data/reports/readiness_status_latest.json`.

---

## Design Rules

### Production / Shadow / Lab Separation

Modules belong to exactly one ring:

| Ring | Flag prefix | Rule |
|------|-------------|------|
| **prod** | `feature_flags` | May execute trades. Any import that reaches the order executor. |
| **shadow** | `shadow_flags` | Observation and counterfactual only. Zero execution side effects. |
| **lab** | `lab_flags` | Experimental. Never imported from prod pipeline. |

Never import a shadow or lab module from the prod pipeline without explicit flag gating.
Never allow lab code to submit orders.

### Policy Belongs in `risk_kernel.py`

All risk policy — position sizing limits, stop-loss widths, PDT floors, exposure caps,
session eligibility — belongs in `risk_kernel.py`.

`order_executor.py` is a **backstop validator**, not a policy owner. If you are tempted
to add a risk rule to `order_executor.py`, add it to `risk_kernel.py` instead. See
`docs/policy_ownership_map.md` for the full ownership map and dual-layer rationale.

### Don't Flatten Prod / Shadow / Lab

These three concepts appear throughout the codebase (watchlist tiers, module rings, flag
namespaces). Keep them consistent. A new module should declare its ring explicitly in its
module docstring and never cross ring boundaries without explicit flag gating.

---

## Rollback

New files added in a session can be reverted by deleting them or `git revert`.
Changes to existing production `.py` files should be tested with `make import-check`
and `python3 validate_config.py` before and after.

The server can be rolled back by syncing a known-good local state and restarting
`systemctl restart trading-bot`. There is no database migration to undo for the
core bot — all state files are JSON/JSONL and are not modified by imports.
