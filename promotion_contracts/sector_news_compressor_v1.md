# Promotion Contract — sector_news_compressor (Shadow → Production Trial)

**Status:** ACTIVE

---

## Module Information

| Field | Value |
|-------|-------|
| Module name | `context_compiler.py` (`get_compressed_sector_news`) |
| Ring | prod trial (feature-flagged) |
| Feature flag | `enable_sector_news_compression_trial` |
| Created | 2026-04-16 |
| Reviewed by | — |
| Approved by | — |

---

## Purpose

`get_compressed_sector_news()` in `context_compiler.py` is a prod-trial Haiku compressor
for the `sector_news` prompt section. When the flag is enabled, it compresses sector news
before injecting it into the Stage 3 Sonnet FULL prompt, reducing token count without
losing signal fidelity.

This is a **prod-ring trial**: unlike shadow modules, it directly affects the Stage 3
prompt content when `enable_sector_news_compression_trial=true`. The uncompressed path
is always available by setting the flag to false.

Core guarantees:
- Never returns None (returns raw on any failure)
- Never raises (try/except wraps all API and logging calls)
- Logs spine with `purpose="prod_compression_trial"`, `ring="prod"`
- Falls back to raw sector_news silently if Haiku call fails

---

## Promotion Criteria

### Required

- [ ] Feature flag `enable_sector_news_compression_trial` tested with `true` on VPS for ≥ 7 consecutive days without incident
- [ ] No FAIL/CRITICAL entries in `data/analytics/incident_log.jsonl` linked to `context_compiler` + `prod_compression_trial`
- [ ] Module is importable without error (`python3 -m py_compile context_compiler.py`)
- [ ] Spine records present with `purpose="prod_compression_trial"`, `ring="prod"` (verify via `data/analytics/cost_attribution_spine.jsonl`)
- [ ] At least 3 tests in `tests/test_core.py` covering the trial compression path
- [ ] Compression cost < 10% of Sonnet token savings (measured over 7d)

### Recommended

- [ ] Compressed output quality validated: semantic overlap ≥ 80% vs original
- [ ] No Stage 3 decision quality degradation observed during trial period
- [ ] Weekly CTO review confirms compression ROI positive

---

## Observed Cost (prod trial)

| Metric | Value |
|--------|-------|
| LLM calls (7d) | not yet measured |
| Total cost (7d) | not yet measured |
| Avg cost/call | ~$0.0001 (Haiku, ~150 input + 50 output tokens) |
| Ring | prod |
| Budget class | low |

---

## Alpha Classification

| Dimension | Classification |
|-----------|---------------|
| Primary | `cost_improvement` — Haiku compression cost < Sonnet savings on sector_news tokens |

---

## Risk Assessment

**Potential risks:**

1. **Semantic loss**: Compressed sector news may omit context Sonnet needs.
   Mitigation: Flag default is `false`. A/B compare decisions with/without compression
   over 20+ cycles before leaving enabled permanently.

2. **Haiku latency**: Extra Haiku call adds ~0.5–1.0s per FULL cycle.
   Mitigation: Acceptable for FULL cycles only (not COMPACT or overnight).

3. **Fallback always present**: On any failure, raw sector_news is used unchanged.
   Zero blast radius for failures.

---

## Rollback Plan

1. Set `enable_sector_news_compression_trial: false` in `strategy_config.json`
2. `systemctl restart trading-bot` on VPS (or wait for next cycle — flag is checked per call)
3. Verify no `[COMPILER] sector_news compressed` log lines in `logs/bot.log`

---

## Approval Sign-off

| Step | Done | Date |
|------|------|------|
| Prod trial ≥ 7 days with flag enabled | ⬜ | — |
| Cost ROI confirmed positive | ⬜ | — |
| Cost review by CFO (Agent 8) | ⬜ | — |
| Technical review by CTO (Agent 5) | ⬜ | — |
| Flag promoted to always-on or removed | ⬜ | — |
| CLAUDE.md source table updated | ⬜ | — |

---

*Contract version: 1. Template at `docs/promotion_contract_template.md`.*
