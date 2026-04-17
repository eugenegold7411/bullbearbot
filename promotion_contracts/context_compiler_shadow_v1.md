# Promotion Contract — context_compiler (Shadow → Production)

**Status:** DRAFT

---

## Module Information

| Field | Value |
|-------|-------|
| Module name | `context_compiler.py` |
| Ring | shadow → prod |
| Feature flag | `enable_context_compressor_shadow` |
| Created | 2026-04-16 |
| Reviewed by | — |
| Approved by | — |

---

## Purpose

`context_compiler.py` is a shadow-ring Claude Haiku prompt compressor. It takes long prompt
sections (portfolio intelligence, macro backdrop, vector memory) and returns `CompressedSection`
objects with a compressed version. The goal is to reduce token count for the Stage 3 Sonnet
call without losing signal fidelity.

Built in the shadow ring to allow side-by-side comparison of compressed vs uncompressed
decision quality before any prod impact.

---

## Promotion Criteria

### Required

- [ ] Feature flag `enable_context_compressor_shadow` tested with `true` on VPS for ≥ 7 consecutive days without incident
- [ ] No FAIL/CRITICAL entries in `data/analytics/incident_log.jsonl` linked to `context_compiler`
- [ ] Module is importable without error (`python3 -m py_compile context_compiler.py`)
- [ ] All public functions have docstrings
- [ ] Cost attribution spine records present (`data/analytics/cost_attribution_spine.jsonl`)
- [ ] At least 1 test in `tests/test_core.py` covering the primary compression path

### Recommended

- [ ] Weekly CTO (Agent 5) report confirms Haiku compression cost < 10% of Sonnet savings
- [ ] CompressedSection quality validated: compressed output has ≥ 80% semantic overlap with original
- [ ] Memory footprint measured (expected: negligible — Haiku, ~100 tokens per section)

---

## Observed Cost (shadow ring)

| Metric | Value |
|--------|-------|
| LLM calls (7d) | not yet measured |
| Total cost (7d) | not yet measured |
| Avg cost/call | ~$0.0001 (Haiku, ~100 input + 50 output tokens) |
| Ring | shadow |
| Budget class | experimental |

---

## Risk Assessment

**Potential risks on promotion to prod:**

1. **Semantic loss**: Compressed prompts may omit context the Sonnet model needs for nuanced decisions.
   Mitigation: A/B compare decisions with and without compression over 20+ cycles.

2. **Latency**: Extra Haiku call adds ~0.5–1.0s per compressed section.
   Mitigation: Compress only sections > 500 tokens; skip if latency budget exceeded.

3. **Cost**: If compression calls cost more than Sonnet savings, ROI is negative.
   Mitigation: Track compression_cost vs sonnet_savings in spine records.

---

## Rollback Plan

1. Set `enable_context_compressor_shadow: false` in `strategy_config.json`
2. `systemctl restart trading-bot` on VPS
3. Verify no `[context_compiler]` log lines in `logs/bot.log`

---

## Approval Sign-off

| Step | Done | Date |
|------|------|------|
| Shadow-ring test ≥ 7 days | ⬜ | — |
| Cost review by CFO (Agent 8) | ⬜ | — |
| Technical review by CTO (Agent 5) | ⬜ | — |
| Flag moved to `feature_flags` section | ⬜ | — |
| CLAUDE.md source table updated | ⬜ | — |

---

*Contract version: 1. Template at `docs/promotion_contract_template.md`.*
