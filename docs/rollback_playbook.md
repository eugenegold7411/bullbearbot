# BullBearBot Rollback Playbook

**This is an operator procedure, not a design doc.**
Follow it at 2am when something has gone wrong. Read it top to bottom before touching anything.

---

## 1. When to Use This Playbook

Use this playbook when any of the following are true:

- A schema migration corrupted a JSON artifact (bot is logging parse errors or crash-looping on file load)
- A feature flag enabled a new code path that is causing exceptions, unexpected behavior, or cycle failures
- A newly deployed module is raising unhandled exceptions that break bot cycles
- The bot is halting cycles or restarting repeatedly (check: `systemctl status trading-bot`)
- Attribution, spine, or versioning code is logging WARNING-level errors that affect data integrity
- A weekly review update to `strategy_config.json` introduced bad values

**Do NOT use this playbook** for:
- Normal trading losses or missed signals — those are bot decisions, not errors
- Slow cycles or cost overruns — diagnose first, don't roll back blindly

---

## 2. Emergency Stop

**Always stop the bot first. Do not touch files while the bot is running.**

```bash
ssh tradingbot 'systemctl stop trading-bot'
```

Verify it is stopped (Active should show "inactive" or "failed", not "active"):

```bash
ssh tradingbot 'systemctl status trading-bot'
```

Check for any open positions that need manual monitoring:

```bash
ssh tradingbot 'source /home/trading-bot/.venv/bin/activate && cd /home/trading-bot && python3 -c "
from dotenv import load_dotenv; load_dotenv()
from alpaca.trading.client import TradingClient
import os
a1 = TradingClient(os.getenv(\"ALPACA_API_KEY\"), os.getenv(\"ALPACA_SECRET_KEY\"), paper=True)
for p in a1.get_all_positions(): print(p.symbol, p.qty, float(p.market_value), float(p.unrealized_pl))
"'
```

If positions are open: note the symbols, check that stop-loss orders are active on Alpaca's web UI
before proceeding. The bot being stopped does NOT cancel existing Alpaca orders — stops remain live.

---

## 3. Schema Artifact Rollback

Use this when a migration ran and produced a corrupt or incorrect artifact.

**Step 1 — Find the backup file.**

`versioning.py` creates backups with the pattern `{filename}.backup_YYYYMMDD_HHMMSS` in the same
directory as the artifact. List backups:

```bash
ssh tradingbot 'ls -lth /home/trading-bot/data/account2/obs_mode_state.json* 2>/dev/null'
ssh tradingbot 'ls -lth /home/trading-bot/data/analytics/cost_attribution_spine.jsonl* 2>/dev/null'
# Generic pattern:
ssh tradingbot 'find /home/trading-bot/data -name "*.backup_*" -newer /tmp/sentinel 2>/dev/null | sort'
```

**Step 2 — Verify the backup is valid JSON.**

```bash
ssh tradingbot 'python3 -c "import json; print(json.load(open(\"/home/trading-bot/data/account2/obs_mode_state.json.backup_YYYYMMDD_HHMMSS\")))"'
```

Replace the filename with the actual backup path. If it prints without error, it's valid.

**Step 3 — Restore the backup.**

```bash
# Copy backup over the corrupted file (adjust paths as needed):
ssh tradingbot 'cp /home/trading-bot/data/account2/obs_mode_state.json.backup_YYYYMMDD_HHMMSS \
                   /home/trading-bot/data/account2/obs_mode_state.json'
```

**Step 4 — Verify the restored file.**

```bash
ssh tradingbot 'python3 -c "import json; d=json.load(open(\"/home/trading-bot/data/account2/obs_mode_state.json\")); print(d)"'
```

**Step 5 — If no backup exists**, the artifact may need to be recreated from scratch.
Check `data/analytics/attribution_log.jsonl` for the most recent known-good state.
For `obs_mode_state.json`, the fallback is to recreate it with known values from CLAUDE.md.

---

## 4. Feature Flag Rollback

All feature flags live in `strategy_config.json`. Toggling a flag to `false` is safe at any time —
the bot reads this file every cycle and the flag is re-evaluated on next call.

**Step 1 — Identify the flag causing the problem.**

Current flags and their effects:

| Flag | Effect when true |
|------|-----------------|
| `enable_cost_attribution_spine` | Appends to `data/analytics/cost_attribution_spine.jsonl` each cycle |
| `enable_schema_migrations` | Enables schema migration framework (versioning.py) |
| `enable_thesis_checksum` | (not yet wired — safe to toggle) |
| `enable_recommendation_memory` | (not yet wired — safe to toggle) |
| `enable_abstention_contract` | (not yet wired — safe to toggle) |
| `enable_replay_fork_debugger` | (shadow) not yet wired |
| `enable_context_compressor_shadow` | (shadow) not yet wired |

**Step 2 — Edit `strategy_config.json` to flip the flag.**

```bash
# Pull the file to local machine for editing:
scp tradingbot:/home/trading-bot/strategy_config.json /tmp/strategy_config_edit.json
# Edit /tmp/strategy_config_edit.json: change "enable_cost_attribution_spine": true → false
# Push back:
scp /tmp/strategy_config_edit.json tradingbot:/home/trading-bot/strategy_config.json
```

Or edit directly on the server:

```bash
ssh tradingbot 'cd /home/trading-bot && python3 -c "
import json
with open(\"strategy_config.json\") as f: c = json.load(f)
c[\"feature_flags\"][\"enable_cost_attribution_spine\"] = False
with open(\"strategy_config.json\", \"w\") as f: json.dump(c, f, indent=2)
print(\"Done:\", c[\"feature_flags\"])
"'
```

**Step 3 — Verify the change took effect on the next cycle.**

After restarting the bot (see Section 6), grep the log for flag-related output:

```bash
ssh tradingbot 'grep -i "spine\|feature_flag\|SPINE\|FLAG" /home/trading-bot/logs/bot.log | tail -20'
```

If the spine flag is now false, `log_spine_record()` returns None immediately with no file write.
The absence of spine records in the next cycle confirms the flag is off.

---

## 5. Code Rollback

Use this when a deployed module has a bug that can't be fixed quickly.

**Step 1 — Identify the last known-good git tag.**

```bash
# On local machine:
cd /Users/eugene.gold/trading-bot
git tag --sort=-creatordate | head -10
```

Key tags (as of last update):
- `t0-substrate-batch1` — T0.5 + T0.6 + T0.7 foundation infrastructure
- (earlier tags listed in git history)

**Step 2 — Check out the tag on the local mirror.**

```bash
cd /Users/eugene.gold/trading-bot
git show t0-substrate-batch1 --stat   # verify what's in the tag
git checkout t0-substrate-batch1-- versioning.py cost_attribution.py feature_flags.py
# Or to roll back everything:
# git checkout t0-substrate-batch1
```

**Step 3 — Push the rolled-back files to the server.**

```bash
rsync -avz -e 'ssh -i ~/.ssh/trading_bot' \
  --exclude .venv --exclude __pycache__ --exclude '*.pyc' \
  --exclude .env --exclude 'logs/*.log' --exclude 'logs/*.jsonl' \
  --exclude nohup.out /Users/eugene.gold/trading-bot/ tradingbot:/home/trading-bot/
```

**Step 4 — Compile-check the rolled-back files.**

```bash
ssh tradingbot 'cd /home/trading-bot && source .venv/bin/activate && \
  python3 -m py_compile versioning.py cost_attribution.py feature_flags.py attribution.py && \
  echo COMPILE_OK'
```

---

## 6. Post-Rollback Checklist

After completing whichever rollback step was needed:

```bash
# 1. Start the service
ssh tradingbot 'systemctl start trading-bot'

# 2. Verify it started (Active: active (running))
ssh tradingbot 'systemctl status trading-bot'

# 3. Watch the first 30 log lines for errors
ssh tradingbot 'sleep 10 && journalctl -u trading-bot -n 30 --no-pager'

# 4. Confirm no CRITICAL/ERROR lines in bot.log from the first cycle
ssh tradingbot 'tail -50 /home/trading-bot/logs/bot.log | grep -E "CRITICAL|ERROR|Traceback|halt"'

# 5. Confirm positions are intact (same as Emergency Stop check above)
ssh tradingbot 'source /home/trading-bot/.venv/bin/activate && cd /home/trading-bot && python3 -c "
from dotenv import load_dotenv; load_dotenv()
from alpaca.trading.client import TradingClient
import os
a1 = TradingClient(os.getenv(\"ALPACA_API_KEY\"), os.getenv(\"ALPACA_SECRET_KEY\"), paper=True)
for p in a1.get_all_positions(): print(p.symbol, p.qty, float(p.market_value))
"'

# 6. Confirm stops are live (check Alpaca open orders)
ssh tradingbot 'source /home/trading-bot/.venv/bin/activate && cd /home/trading-bot && python3 -c "
from dotenv import load_dotenv; load_dotenv()
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
import os
a1 = TradingClient(os.getenv(\"ALPACA_API_KEY\"), os.getenv(\"ALPACA_SECRET_KEY\"), paper=True)
orders = a1.get_orders(filter=GetOrdersRequest(status=\"open\"))
for o in orders: print(o.symbol, o.order_type, o.stop_price, o.limit_price)
"'
```

A clean first cycle shows:
- No CRITICAL or ERROR in logs
- Positions unchanged from before the stop
- Stop orders present for all open equity positions

---

## 7. CLAUDE.md Reference

`CLAUDE.md` contains the full architecture context: pipeline stages, file inventory,
data directories, risk rules, and historical context for every module.

Read it before any non-emergency intervention. It is at `/home/trading-bot/CLAUDE.md`
on the server and `/Users/eugene.gold/trading-bot/CLAUDE.md` on the local mirror.

**Key paths relevant to this playbook:**
- Config: `strategy_config.json`
- Attribution log: `data/analytics/attribution_log.jsonl`
- Spine log: `data/analytics/cost_attribution_spine.jsonl`
- Obs mode state: `data/account2/obs_mode_state.json`
- Bot log: `logs/bot.log`
- Schema backup files: same directory as original, `.backup_YYYYMMDD_HHMMSS` suffix
