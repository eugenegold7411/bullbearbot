# Backtest / Live-Model Boundary Audit

**Date:** 2026-04-18  
**Scope:** Read-only audit. No code was modified.  
**Prompt:** Phase 5 — investigation only.

---

## 1. Conclusion

`backtest_runner.py` is **contaminated**: every simulated trade decision is made by a
live Claude Sonnet call, and the Strategy Director's output directly overwrites
`strategy_config.json` with `active_strategy`, `stop_loss_pct_core`,
`take_profit_multiple`, and other live control-plane parameters. The numeric metrics
(Sharpe, win rate, return) are produced by the LLM's historical decisions, not by
replaying a fixed deterministic rule set — this is evaluation leakage.

`signal_backtest.py` is **clean**: it is fully deterministic. No LLM calls exist
anywhere in its call graph. It reads historical signals from disk, looks up daily bar
prices, and computes forward returns arithmetically.

The **weekly review** (Agent 4 path) consumes `signal_backtest` only — the clean module.
`run_weekly_backtest()` from `backtest_runner.py` is documented as an Agent 4 input
source but is not wired into `weekly_review.py` in the current codebase.

---

## 2. Files inspected

| File | Local / Server | Notes |
|------|---------------|-------|
| `backtest_runner.py` | local | 1,145 lines; full read |
| `signal_backtest.py` | local | 477 lines; full read |
| `weekly_review.py` | local | Agent 4 path only; full grep |
| `strategy_config.json` | local | backtest/recalibration fields only |

No server-only file inspection was required.

---

## 3. Entry points reviewed

### `backtest_runner.py`

| Function | Lines | Purpose |
|----------|-------|---------|
| `run_backtest(strategy, days)` | 915–1023 | Full 5-strategy simulation + Director vote + config write |
| `run_weekly_backtest(lookback_days)` | 1028–1125 | Hybrid-only read-only subset; calls `_run_strategy()` |
| `_run_strategy(...)` | 560–728 | Inner loop: builds prompt → calls Claude per trade date → simulates fill |
| `_run_strategy_director(results)` | 733–842 | Compares strategy stats → calls Claude → returns winner + params |
| `_write_strategy_config(director, results)` | 847–882 | Writes Director output to `strategy_config.json` |

### `signal_backtest.py`

| Function | Lines | Purpose |
|----------|-------|---------|
| `run_signal_backtest(lookback_days)` | 246–392 | Main entry: extract signals → lookup bars → forward returns |
| `format_backtest_report(result)` | 397–455 | Deterministic markdown formatter |
| `save_backtest_results(result)` | 460–476 | Writes `data/reports/backtest_latest.json` |

### `weekly_review.py` — Agent 4 path

The Agent 4 input builder (around line 2419–2509) calls:

1. `signal_backtest.run_signal_backtest(lookback_days=30)` — deterministic
2. `signal_backtest.format_backtest_report(_bt_result)` — deterministic
3. `signal_backtest.save_backtest_results(_bt_result)` — deterministic

The formatted report is injected as context into an Agent 4 Claude call (Batch API).
The LLM produces a commentary report — it does not compute or alter any numeric metric.

**`backtest_runner.run_weekly_backtest()` is not called anywhere in `weekly_review.py`.**
This contradicts the CLAUDE.md entry that describes it as "lightweight Agent 4 call." The
function exists but is orphaned from the weekly review pipeline.

---

## 4. LLM / external-service call sites

| File | Line | Function | Service | Classification | Rationale |
|------|------|----------|---------|----------------|-----------|
| `backtest_runner.py` | 606 | `_run_strategy()` | Anthropic `claude-sonnet-4-6` | **evaluation_leakage** | Claude decides which assets to buy/sell on each simulated date. This determines which `_simulate_trade()` calls are made, which determines P&L, which determines all output metrics (Sharpe, win rate, return, profit factor). The LLM is not commentating on results — it *is* the strategy being evaluated. Outputs are non-deterministic and vary across runs. |
| `backtest_runner.py` | 793 | `_run_strategy_director()` | Anthropic `claude-sonnet-4-6` | **evaluation_leakage** | Director receives the numeric stats table (already produced via LLM decisions at line 606) and outputs `winner`, `stop_loss_pct_core`, `take_profit_multiple`, `max_positions`, `director_notes`, and signal weights. `_write_strategy_config()` immediately persists these to `strategy_config.json`. The live bot reads these values every cycle. |

No LLM calls were found in `signal_backtest.py`, in any function reachable from
`run_signal_backtest()`, or in the Agent 4 data-assembly path of `weekly_review.py`
prior to the Agent 4 Claude call itself.

---

## 5. Determinism assessment

### `signal_backtest.py` — fully deterministic

Input sources:
- `memory/decisions.json` — static at read time
- `data/analytics/near_miss_log.jsonl` — static at read time
- `data/bars/{SYM}_daily.csv` — static daily bars cache

Computation: arithmetic (`(future_price - entry_price) / entry_price`), sorted lists,
averages. No randomness, no external API calls, no LLM calls anywhere in the file.

Given identical inputs, two runs of `run_signal_backtest()` produce bit-identical output.

### `backtest_runner.py` — non-deterministic; LLM-dependent throughout

`_run_strategy()` calls `claude.messages.create()` on every simulated trade date.
The LLM response determines:
- Whether a buy/sell action is emitted
- Which symbol
- What qty, stop_loss, take_profit

These flow into `_simulate_trade()`, which performs deterministic price-bar lookup given
those inputs. But the inputs themselves vary across runs and across time (temperature > 0,
prompt caching affects context, model updates change outputs).

`EVERY_NTH_DAY = 3` means roughly `30 / 3 = 10` LLM calls per strategy per 30-day window,
or ~50 calls for a full 5-strategy `run_backtest()`. A 90-day full run makes ~150 calls.

There is no deterministic "rule-based" strategy being replayed. The strategy prompt is fed
to Claude, and Claude's output is treated as ground truth for what the strategy would have
done — this is analogous to asking the model to grade its own exam.

---

## 6. Control-plane impact

### How `backtest_results` reaches `strategy_config.json`

Only `backtest_runner._write_strategy_config()` writes `backtest_results`. It is called
from `run_backtest()` only when all five strategies are run (the `if not strategy and
len(results) > 1:` gate). `weekly_review.py` never calls either function.

Current value in `strategy_config.json`: `"backtest_results": {}` — the key exists but
has never been populated by the weekly review pipeline.

### Whether LLM outputs influence recalibration or gating

**Yes — via `_run_strategy_director()`.**

The Director call at line 793 receives a stats table whose rows were produced by Claude
decisions at line 606. The Director then outputs:

```json
{
  "winner": "...",                           → strategy_config.active_strategy
  "parameter_adjustments": {
    "stop_loss_pct_core": ...,               → strategy_config.parameters.stop_loss_pct_core
    "take_profit_multiple": ...,             → strategy_config.parameters.take_profit_multiple
    "max_positions": ...,                    → strategy_config.parameters.max_positions
    "min_confidence_threshold": "...",       → strategy_config.parameters.min_confidence_threshold
    "sector_rotation_bias": "...",           → strategy_config.parameters.sector_rotation_bias
    "director_notes": "..."                  → strategy_config.director_notes
  }
}
```

These values are then merged into `strategy_config.json` immediately. The live `run_cycle()`
reads `strategy_config.json` on every 5-minute market cycle. So an LLM-derived backtest
conclusion flows directly into live position sizing and stop-loss parameters.

**The `backtest_minimum_sample_before_recalibration: 30` field does not enforce a code-level
gate.** It appears only as a descriptive string in `strategy_config.json` and is injected
into the Strategy Director's weekly-review prompt as guidance text. There is no `if`-branch
in any `.py` file that reads this field and blocks a config write.

---

## 7. Recommendation

### `signal_backtest.py` — no action needed

The module is clean. It is correctly used in the weekly review pipeline. The Agent 4 LLM
call that consumes its output is a post-computation commentary layer, not a numeric step.
No changes required.

### `backtest_runner.py` — minimum follow-up investigation warranted

The specific risk is not that Claude is used; it is that **Claude's outputs at line 606
become the sole determinant of the numeric metrics used at line 793 to justify config
writes**. A more expensive or newer model will produce different "backtest" results for
identical historical data, making the results unreproducible and model-version-dependent.

**If `run_backtest()` is to remain in its current form, consider:**

1. Clearly documenting (in the file header and in CLAUDE.md) that this is an
   "LLM-in-the-loop simulation," not a deterministic historical replay. The current header
   says "Replays 90 days of cached daily bars through 5 strategy variants" — this implies
   rule-based replay, which is inaccurate.

2. Adding a `run_backtest()` gate that checks the `backtest_minimum_sample_before_recalibration`
   field in `strategy_config.json` before invoking `_write_strategy_config()`, to prevent
   the Director from overwriting live params when the bot has fewer than N confirmed fills.

3. Separating `_write_strategy_config()` from `run_backtest()` so the director verdict is
   logged for review but does not auto-apply. The weekly review's Agent 6 already performs
   a Strategy Director role with more context — two independent Director calls writing to
   the same `active_strategy` field is a race condition risk.

**`run_weekly_backtest()` is orphaned.** It appears in `backtest_runner.py` and is
documented in CLAUDE.md as an "Agent 4 call," but `weekly_review.py` does not import or
call it. If it was intended to replace or augment `signal_backtest` in Agent 4, that wiring
was never completed. If not, the CLAUDE.md entry should be corrected.

No production code changes were made in this audit.

---

## Verification checklist

- [x] All Anthropic/Claude call sites found: 2 (lines 606 and 793 in `backtest_runner.py`)
- [x] All listed call sites have non-empty classification
- [x] `signal_backtest.py` confirmed to have zero LLM calls
- [x] `strategy_config.json` control-plane flow traced end-to-end
- [x] No code changes made
