# Account 2 — Migration Status

Tracks what has been built, what is still legacy, and known gaps in the A2 options pipeline.
Updated: 2026-04-20 (S3-C/S3-E session).

---

## Done

| Item | File(s) | Notes |
|------|---------|-------|
| Stage decomposition (5 modules) | `bot_options_stage0_preflight.py` through `_stage4_execution.py` | `bot_options.py` is now a thin orchestrator |
| `A2FeaturePack` dataclass | `schemas.py` | Normalized per-symbol feature object; built in Stage 1 |
| `A2CandidateSet` dataclass | `schemas.py` | Tracks router rule fired, allowed structures, generated/vetoed/surviving candidates |
| `A2DecisionRecord` dataclass | `schemas.py` | Full audit trail for one A2 cycle; `no_trade_reason` uses `NO_TRADE_REASONS` taxonomy |
| `NO_TRADE_REASONS` taxonomy | `schemas.py` | 12 canonical values including `rollback_active` |
| `validate_no_trade_reason()` | `schemas.py` | Raises `ValueError` on unknown values; guards all no-trade paths |
| Config-driven router thresholds | `bot_options_stage2_structures.py`, `strategy_config.json["a2_router"]` | `_get_router_config()` falls back to `_A2_ROUTER_DEFAULTS` if key absent |
| Rollback feature flags | `strategy_config.json["a2_rollback"]` | `disable_candidate_generation`, `disable_bounded_debate`, `force_no_trade` — all default false |
| Rollback wired into Stage 1 | `bot_options_stage1_candidates.py:run_candidate_stage()` | Check fires before any local imports |
| Rollback wired into Stage 3 | `bot_options_stage3_debate.py:run_bounded_debate()` | Returns `A2DecisionRecord(no_trade_reason="rollback_active")` |
| `validate_config.py` gates | `validate_config.py` | `a2_router` (4 fields), `a2_rollback` (3 fields, WARN if any active) |
| Bounded debate | `bot_options_stage3_debate.py` | Claude selects from pre-built candidates; confidence ≥ 0.85 required |
| Options reconciliation (Stage 0) | `reconciliation.py`, `bot_options_stage0_preflight.py` | Runs before every new proposal cycle |
| IV history + observation mode | `options_data.py`, `bot_options.py` | 20-trading-day obs mode; 16 core symbols tracked |
| Liquidity gates | `bot_options_stage2_structures.py`, `strategy_config.json["account2"]["liquidity_gates"]` | Pre-debate OI/volume floor + post-debate spread/OI gates |

---

## Still Legacy / To Deprecate

| Item | Location | Notes |
|------|----------|-------|
| `save_legacy_decision()` calls | `bot_options.py:run_options_cycle()` | Writes to `decisions_account2.json` after every cycle for backward compat; safe to remove once A2DecisionRecord log is the sole consumer |
| `decisions_account2.json` | `data/account2/trade_memory/decisions_account2.json` | Flat JSON log; predates `A2DecisionRecord`. Weekly review and any tooling that reads this file must migrate to the structured record log before this can be dropped |
| Free-form debate fallback | `bot_options_stage3_debate.py` | Path for cycles where `candidate_structures` is empty; falls through to open-ended Claude call. Retained for safety but untested in production. Should be removed once all routing paths produce candidates |
| `bot_options.py` backward-compat re-exports | `bot_options.py` lines 52–57 | Re-exports `_build_a2_feature_pack`, `_route_strategy`, `_apply_veto_rules`, `_quick_liquidity_check`, `_STRATEGY_FROM_STRUCTURE`, `_parse_bounded_debate_response` for existing test imports. Remove once all test imports updated |
| `config=` not threaded to `run_bounded_debate()` | `bot_options.py:run_options_cycle()` | S3-C added `config` param to `run_bounded_debate()`, but `run_options_cycle()` does not yet pass it. Stage 3 falls back to `_load_strategy_config()` internally — functionally correct but not the clean path |

---

## Current Source of Truth

| Concern | Authority |
|---------|-----------|
| No-trade reason values | `schemas.NO_TRADE_REASONS` + `validate_no_trade_reason()` |
| Router threshold values | `strategy_config.json["a2_router"]` (fallback: `bot_options_stage2_structures._A2_ROUTER_DEFAULTS`) |
| Rollback switches | `strategy_config.json["a2_rollback"]` |
| Position sizing limits | `strategy_config.json["account2"]["position_sizing"]` |
| IV environment rules | `strategy_config.json["account2"]["iv_rules"]` |
| Liquidity gates | `strategy_config.json["account2"]["liquidity_gates"]` |
| A2 decision audit trail | `A2DecisionRecord` (persisted by `persist_decision_record()` in Stage 4) |

---

## Known Gaps

| Gap | Impact | Suggested Fix |
|-----|--------|---------------|
| Free-form debate fallback reachable | If candidate_structures is empty but candidates is non-empty, Stage 3 runs open-ended debate; Claude could invent contracts | Remove free-form path once all router rules always produce at least one candidate structure, or add a hard guard |
| `config=` not passed to `run_bounded_debate()` | Rollback flags and future config-gated debate params are read from disk by Stage 3 rather than from the already-loaded config | Thread `config=_load_strategy_config()` into the `run_bounded_debate()` call in `run_options_cycle()` |
| `decisions_account2.json` still written | Dual-write adds cost and creates drift risk | Deprecate after weekly review and any external tooling migrates to `A2DecisionRecord` log |
| UW (Unusual Whales) options flow not integrated | A2 candidates built purely from A1 signal scores; no options-flow-specific catalyst | F004 (Unusual Whales subscription) — medium priority, deferred |
| `bot_options.py` backward-compat re-exports | Tests that `from bot_options import _route_strategy` etc. will break if the re-exports are removed | Update test imports to point at stage modules before removing re-exports |
| ~~Congressional API `api.lambdafin.com` dead~~ | ~~Congressional signal permanently stale~~ | **RESOLVED 2026-04-21** — Migrated to QuiverQuant free API (`api.quiverquant.com/beta/live/congresstrading`). Lambda Finance discontinued their free `api.lambdafin.com` subdomain (pivoted to paid MCP tier). QuiverQuant requires browser-like `User-Agent` + `Referer` headers from datacenter IPs to avoid 401. Fix in `insider_intelligence.py`. 800 trades fetched, 24 watchlist hits confirmed live on server. |
