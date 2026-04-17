# Promotion Contract — theory_of_failure (Lab → Shadow)

**Status:** DRAFT

---

## Module Information

| Field | Value |
|-------|-------|
| Module name | `annex/theory_of_failure.py` |
| Ring | lab → shadow |
| Feature flag | `enable_theory_of_failure` (lab_flags) |
| Evaluation class | `quality_positive_non_alpha` |
| Created | 2026-04-16 |
| Reviewed by | — |
| Approved by | — |

---

## Purpose

`annex/theory_of_failure.py` generates 2-4 competing failure theories for a subject (forensic
record, recommendation, or outcome). Single Haiku call per subject. Abstains if no outcome data
is available. Each theory includes a `testable_prediction` for future validation. Results logged
to `data/annex/theory_of_failure/theories.jsonl`.

---

## Promotion Criteria

### Required

- [ ] Feature flag `enable_theory_of_failure` tested with `true` for ≥ 14 trading days
- [ ] No FAIL/CRITICAL incidents linked to this module
- [ ] Module importable without error
- [ ] Abstention fires 100% of the time when `thesis_verdict=pending`
- [ ] Generated theories have ≥ 2 per subject (validates multi-theory generation)
- [ ] Each theory has a non-empty `testable_prediction`
- [ ] Agent 1 (Quant Analyst) review shows theory summary producing useful output

### Recommended

- [ ] At least 1 dominant theory validated retrospectively (prediction came true)
- [ ] TheoryType distribution reviewed for diversity (not all `overconfidence` or `unknown`)
- [ ] Weekly CTO review shows theory-of-failure cost < $0.01/week when flag is true

---

## Observed Cost (lab ring)

| Metric | Value |
|--------|-------|
| LLM calls (7d) | not yet measured |
| Total cost (7d) | not yet measured |
| Avg cost/call | ~$0.0004 (Haiku, ~800 tokens input + 600 output) |
| Ring | lab |
| Budget class | experimental |

---

## Risk Assessment

1. **No prod mutation**: Appends to `theories.jsonl` only. No writes to decisions, strategy_config.
2. **Abstention on missing data**: Prevents LLM calls when there's nothing to evaluate.
3. **Theory quality dependency**: Quality depends on forensic record depth. Shallow records produce generic theories.

---

## Rollback Plan

1. Set `enable_theory_of_failure: false` in `strategy_config.json`
2. No restart required
3. `theories.jsonl` is analytics-only

---

## Approval Sign-off

| Step | Done | Date |
|------|------|------|
| Lab-ring test ≥ 14 days | ⬜ | — |
| Theory quality reviewed on 10+ real forensic records | ⬜ | — |
| Abstention rate on pending records validated | ⬜ | — |
| Flag moved to `shadow_flags` | ⬜ | — |
| CLAUDE.md updated | ⬜ | — |

---

*Contract version: 1.*
