# Weekly Review Runbook

> Operator procedure for the 11-agent weekly review.
> Style mirrors `docs/rollback_playbook.md`.
> Keep this updated when the review pipeline changes.

---

## 1. When to Run

### Automatic (Sunday)
The scheduler triggers `run_review()` automatically every Sunday morning via
`_maybe_generate_weekly_summary()`. You do not need to do anything unless it fails.

To verify Sunday's review ran:
```bash
ls data/reports/weekly_review_*.md | tail -3
ls data/weekly_review/
```

### Manual trigger (any day)
Use when:
- Sunday review failed or timed out
- Major market event (flash crash, earnings blow-up, regime shift)
- Before a significant strategy change decision
- After resolving a critical bug that affects trade analysis

```bash
python3 scripts/run_weekly_review.py
```

### Emergency board meeting
Use when immediate multi-agent deliberation is needed:
```bash
python3 scripts/run_weekly_review.py --emergency --reason "TSM short blowup"
# or use the shell alias:
./board_meeting.sh "reason here"
```
Emergency reports saved to `data/reports/emergency_review_{YYYYMMDD_HHMM}.md`.

---

## 2. Pre-Run Checklist

Before triggering a review manually:

| Check | Command |
|-------|---------|
| Service is running | `systemctl status trading-bot` |
| No active emergencies | `tail -20 logs/bot.log | grep -i "halt\|critical"` |
| Cost spine accessible | `tail -3 data/analytics/cost_attribution_spine.jsonl` |
| Strategy config valid | `python3 validate_config.py` |
| Dependency check | `python3 scripts/run_weekly_review.py --dry-run` |

The `--dry-run` flag checks all Python imports and key config files. Exit 0 means
you are ready to run.

---

## 3. Run Command

```bash
# Standard run
python3 scripts/run_weekly_review.py

# Emergency
python3 scripts/run_weekly_review.py --emergency --reason "describe the incident"

# Direct (bypasses archiving wrapper — use only if wrapper has a bug)
python3 weekly_review.py
```

Expected runtime: **10–20 minutes** (Anthropic Batch API for agents 1–4 adds latency;
sequential agents 5–6 add ~5 minutes; parallel agents 7–11 add ~3 minutes).

---

## 4. Output Locations

| Artifact | Path |
|---------|------|
| Main report | `data/reports/weekly_review_YYYY-MM-DD.md` |
| Archived folder | `data/weekly_review/YYYY-MM-DD/` |
| Run manifest | `data/weekly_review/YYYY-MM-DD/run_manifest.json` |
| Cost summary | `data/weekly_review/YYYY-MM-DD/cost_summary.json` |
| Recommendation summary | `data/weekly_review/YYYY-MM-DD/recommendation_summary.json` |
| Status snapshot | `data/weekly_review/YYYY-MM-DD/status_snapshot.json` |
| Updated strategy config | `strategy_config.json` (overwritten by Agent 6) |
| Director memo history | `data/reports/director_memo_history.json` |

**What to look at first:**
1. `data/reports/weekly_review_YYYY-MM-DD.md` → search for "CRITICAL" or "ALERT"
2. `strategy_config.json` → check `director_notes` and `time_bound_actions`
3. `python3 scripts/inspect_recommendations.py --status pending` → pending items
4. `python3 scripts/report_cost_spine_unknowns.py` → cost health check

---

## 5. What Healthy Output Looks Like

| Agent | Healthy Signal |
|-------|---------------|
| Agent 1 — Quant Analyst | Signal quality table present; no "no data" warnings |
| Agent 2 — Risk Manager | Drawdown figures present; no PDT floor warnings |
| Agent 3 — Execution Engineer | Fill rate present; no "all unknown session" warnings |
| Agent 4 — Backtest Analyst | Backtest section present (may be sparse early) |
| Agent 5 — CTO | Module cost table present; no unresolved architecture risks |
| Agent 6 — Strategy Director | `director_notes` updated; `strategy_config.json` written |
| Agent 7 — Market Researcher | Research section present; web search completed |
| Agent 8 — CFO | Cost breakdown present; monthly projection shown |
| Agent 9 — PM | Roadmap section present; F-series items tracked |
| Agent 10 — Compliance | No rule violations flagged; abstention contract healthy |
| Agent 11 — Narrative | Twitter thread script present |

**Signs of a degraded review:**
- Any agent section ends with `[error]` or `[timed out]`
- `strategy_config.json` unchanged (Agent 6 didn't write)
- Missing sections in the report (agent returned empty string)
- `run_manifest.json` missing `run_completed_at`

---

## 6. What to Do If the Review Errors

### Agent API error (timeout, rate limit)
```bash
# Check error in logs
tail -50 logs/bot.log | grep -i "weekly_review\|batch\|anthropic"

# Re-run — the review is idempotent (will overwrite the same date's file)
python3 scripts/run_weekly_review.py
```

### Batch API returned empty responses
Agents 1–4 run via the Anthropic Batch API. If all four return empty:
```bash
# Check if batch was created
python3 -c "import json; [print(l) for l in open('logs/bot.log').readlines()[-100:] if 'batch' in l.lower()]"
```
Wait 5 minutes and retry. The Batch API can have temporary delays.

### strategy_config.json not updated
Agent 6 writes the config. If it didn't write:
1. Check the report for Agent 6 output — look for the JSON block it produced
2. Manually extract and merge the JSON into strategy_config.json
3. Run `python3 validate_config.py` to verify

### One agent missing from report
The 11-agent pipeline is fault-tolerant. A missing agent section means that agent's
call errored. The rest of the review is valid. You can re-run just that agent manually
by importing `weekly_review` and calling the specific helper function.

### Archiving failed but review completed
```bash
python3 scripts/archive_weekly_review_outputs.py
```
This is safe to run after the fact — it reads the existing report file and creates
the dated folder.

---

## 7. Emergency Board Meeting Procedure

Trigger when: flash crash, unexplained drawdown > 5%, critical bug discovered,
earnings outcome significantly different from thesis, regime shift.

```bash
# Step 1: Stop new trades if needed
ssh tradingbot 'systemctl stop trading-bot'  # only if you need to halt

# Step 2: Run emergency review
python3 scripts/run_weekly_review.py --emergency --reason "describe what happened"

# Step 3: Read the report
cat "data/reports/emergency_review_$(date +%Y%m%d)_*.md"

# Step 4: Act on Agent 6 recommendations
# Review director_notes in strategy_config.json
# Check time_bound_actions for new forced exits

# Step 5: Restart if stopped
ssh tradingbot 'systemctl start trading-bot'

# Step 6: Monitor
ssh tradingbot 'journalctl -u trading-bot -f'
```

**Do not trigger emergency reviews more than once per 2 hours** — each review
costs $5–15 in API calls and generates a Batch API write that cannot be cancelled.
