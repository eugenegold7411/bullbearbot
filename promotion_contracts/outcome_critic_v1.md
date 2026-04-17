# Promotion Contract — outcome_critic (Shadow → Production)

**Status:** DRAFT

---

## Module Information

| Field | Value |
|-------|-------|
| Module name | `outcome_critic.py` |
| Ring | shadow → prod |
| Feature flag | `enable_outcome_critic` (shadow_flags) |
| Evaluation class | `quality_positive_non_alpha` |
| Created | 2026-04-16 |
| Reviewed by | — |
| Approved by | — |

---

## Purpose

`outcome_critic.py` is a sparse-outcome critic that evaluates recommendation and forensic record
quality using a single Haiku call per subject. It scores reasoning quality, evidence quality,
prediction specificity, and abstention appropriateness. Abstains automatically on pending verdicts.
Results logged to `data/analytics/critic_scores.jsonl`.

---

## Promotion Criteria

### Required

- [ ] Feature flag `enable_outcome_critic` tested with `true` on VPS for ≥ 7 consecutive days
- [ ] No FAIL/CRITICAL entries in `incident_log.jsonl` linked to `outcome_critic`
- [ ] Module importable without error (`python3 -m py_compile outcome_critic.py`)
- [ ] `critic_scores.jsonl` entries present with correct `layer_name="shadow_analysis"`, `ring="shadow"`
- [ ] At least 2 tests covering abstention-on-pending and schema validity
- [ ] Agent 4 weekly review shows `format_critic_summary_for_review()` producing non-empty output

### Recommended

- [ ] Overall critic score shows signal: lower-conviction decisions tend to have lower `reasoning_quality`
- [ ] Weekly CTO review shows critic cost < $0.005/day when flag is true
- [ ] Abstention rate on pending recommendations is 100% (by design — verified empirically)

---

## Observed Cost (shadow ring)

| Metric | Value |
|--------|-------|
| LLM calls (7d) | not yet measured |
| Total cost (7d) | not yet measured |
| Avg cost/call | ~$0.0003 (Haiku, ~600 tokens input + 400 output) |
| Ring | shadow |
| Budget class | negligible |

---

## Risk Assessment

1. **No prod mutation**: Scores are append-only to `critic_scores.jsonl`. No writes to decisions,
   strategy_config, or execution paths. Risk: very low.
2. **LLM call frequency**: One call per scored record. Callers control frequency. Feature flag is the kill switch.
3. **Abstention correctness**: Must always abstain for `verdict=pending`. Verified by test.

---

## Rollback Plan

1. Set `enable_outcome_critic: false` in `strategy_config.json`
2. Restart trading-bot service
3. `critic_scores.jsonl` is analytics-only — safe to archive

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

*Contract version: 1.*
