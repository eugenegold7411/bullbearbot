# strategy_config.json — Schema v2 Reference

**Introduced:** Phase 6 (2026-04-18)  
**Sole authorized writer:** `weekly_review.py` Agent 6 (Strategy Director) via `_save_strategy_config()`  
**Previous writer removed:** `backtest_runner.py` (Phase 5)

---

## Top-level shape

| Key | Type | Description |
|-----|------|-------------|
| `version` | int | Schema version. Current: **2** |
| `generated_at` | str (ISO) | Timestamp of last Agent 6 write |
| `generated_by` | str | Always `"weekly_review"` or `"weekly_review_final"` |
| `active_strategy` | str | One of: `hybrid`, `momentum`, `mean_reversion`, `news_sentiment`, `cross_sector` |
| `backtest_results` | dict | Last backtest run summary (may be `{}`) |
| `position_sizing` | dict | **Canonical** tier/exposure percentages (see below) |
| `parameters` | dict | Runtime parameters consumed by `risk_kernel.py` and bot pipeline |
| `signal_weights` | dict | **Canonical** per-strategy signal weights (see below) |
| `director_notes` | dict | Strategy Director operational memo (see below) |
| `launch_date` | str | Bot launch date |
| `time_bound_actions` | list | Mandatory symbol exits with deadlines |
| `exit_management` | dict | Trail stop config |
| `account2` | dict | Account 2 (options) full config |
| `scratchpad` | dict | Pre-decision scratchpad config |
| `sonnet_gate` | dict | Stage 3 gate config |
| `shadow_lane` | dict | Counterfactual log config |
| `feature_flags_version` | int | Flag schema version |
| `feature_flags` | dict | Production feature flags |
| `shadow_flags` | dict | Shadow-ring feature flags |
| `lab_flags` | dict | Lab/experimental feature flags |

---

## Canonical sections

### `position_sizing` (canonical owner for tier/exposure keys)

These five keys live **only** in `position_sizing`. They must NOT appear in `parameters`.

| Key | Default | Read by |
|-----|---------|---------|
| `core_tier_pct` | 0.15 | `risk_kernel._sizing()` |
| `dynamic_tier_pct` | 0.08 | `risk_kernel._sizing()` |
| `intraday_tier_pct` | 0.05 | `risk_kernel._sizing()` |
| `max_total_exposure_pct` | 0.67 | `risk_kernel._sizing()` |
| `cash_reserve_pct` | 0.20 | `validate_config.py` |

### `signal_weights` (canonical owner for strategy weights)

These four keys live **only** in `signal_weights`. They must NOT appear in `parameters`.

| Key | Default | Read by |
|-----|---------|---------|
| `momentum_weight` | 0.35 | Agent 6 (weekly review) |
| `mean_reversion_weight` | 0.20 | Agent 6 (weekly review) |
| `news_sentiment_weight` | 0.30 | Agent 6 (weekly review) |
| `cross_sector_weight` | 0.15 | Agent 6 (weekly review) |

Weights must sum to 1.0. Recalibration requires n ≥ 30 confirmed closed trades
(see `parameters.backtest_minimum_sample_before_recalibration`).

### `parameters` (runtime parameters)

Keys consumed by `risk_kernel.py` at trade time:

| Key | Read by |
|-----|---------|
| `stop_loss_pct_core` | `risk_kernel._default_stop_pct()` |
| `stop_loss_pct_intraday` | `risk_kernel._default_stop_pct()` |
| `take_profit_multiple` | `risk_kernel.size_position()` |
| `max_positions` | `risk_kernel.check_eligibility()` |
| `catalyst_tag_disallowed_values` | `risk_kernel.check_eligibility()` |

Agent 6 may only update keys that already exist in `parameters` (whitelist gate in
`weekly_review._save_strategy_config()`). This prevents the migration from being
reversed by a future weekly review run.

### `director_notes` (operational memo)

Always a `dict` with three required fields:

| Field | Type | Description |
|-------|------|-------------|
| `active_context` | str | 2–4 paragraph strategic memo |
| `expiry` | str (YYYY-MM-DD) | When this memo expires (typically next Sunday) |
| `priority` | str | `normal`, `elevated`, or `critical` |
| `memo_path` | str (optional) | Path to the extracted dated memo file |

Dated memo files are written to `data/reports/director_memos/director_memo_YYYY-MM-DD.md`
when Agent 6 runs.

---

## Migration: v1 → v2

Registered in `versioning.py` as `("strategy_config", 1)`.

**Changes:**
- Removed 9 duplicate keys from `parameters` (see canonical sections above)
- Removed `max_single_position_pct_DEPRECATED` string-marker field from `parameters`
- Bumped `version` from 1 to 2

**Canonical locations after migration:**
- Tier/exposure keys → `position_sizing` only
- Signal weight keys → `signal_weights` only

---

## Invariants enforced by `validate_config.py`

- `version == 2` (Gate 16)
- None of the 9 canonical-section keys appear in `parameters` (FAIL if present)
- No `_DEPRECATED` marker fields in `parameters` (FAIL if present)
- `director_notes` is a dict with `expiry` field (WARN if plain string)
- `max_single_position_pct` absent from both `position_sizing` and `parameters` (FAIL if present)

---

## Files that read `strategy_config.json`

| File | What it reads |
|------|---------------|
| `risk_kernel.py` | `parameters` (stop/profit/position keys), `position_sizing` (tier pcts), `time_bound_actions`, `account2` |
| `bot.py` | Full config → `risk_kernel`; `exit_management.backstop_days` |
| `bot_options.py` | `account2.liquidity_gates` |
| `feature_flags.py` | `feature_flags`, `shadow_flags`, `lab_flags` |
| `validate_config.py` | All sections (validation only) |
| `backtest_runner.py` | `parameters.backtest_minimum_sample_before_recalibration` |
