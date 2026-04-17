# Promotion Contract — personality_forks (Lab → Shadow)

**Status:** DRAFT

---

## Module Information

| Field | Value |
|-------|-------|
| Module name | `annex/personality_forks.py` |
| Ring | lab → shadow |
| Feature flag | `enable_personality_forks` (lab_flags) |
| Evaluation class | `quality_positive_non_alpha` |
| Created | 2026-04-16 |
| Reviewed by | — |
| Approved by | — |

---

## Purpose

`annex/personality_forks.py` runs 5 alternative "personality" evaluations of each decision cycle.
Each fork applies a different cognitive style (paranoid, opportunist, minimalist, anti_crowding,
catalyst_purist) and returns a `ForkOpinion` with conviction_adjustment, primary_reason, and
cognitive_conflict. Rule-based pre-filters run before any LLM call to minimize cost: paranoid
applies 0.6 discount factor; catalyst_purist vetoes null-catalyst entries without LLM. Results
logged to `data/annex/personality_forks/opinions.jsonl`.

---

## Promotion Criteria

### Required

- [ ] Feature flag `enable_personality_forks` tested with `true` for ≥ 14 trading days
- [ ] No FAIL/CRITICAL incidents linked to this module
- [ ] Rule-based pre-filter rate reviewed: paranoid + catalyst_purist should account for ≥ 50%
      of fork opinions without LLM call (validates cost efficiency)
- [ ] Each of the 5 forks produces at least 1 non-abstaining opinion in the 14-day window
- [ ] Module importable without error
- [ ] Weekly review Agent 1 shows personality fork section with ≥ 1 cognitive_conflict per week

### Recommended

- [ ] Retrospective study: compare cases where ≥ 3 forks disagreed with prod vs. outcome
- [ ] Spine cost review: total fork cost < $0.02/day (5 forks × Haiku rate)
- [ ] Review conviction_adjustment distribution — confirm paranoid is genuinely more conservative
      than opportunist across ≥ 20 matched decisions

---

## Observed Cost (lab ring)

| Metric | Value |
|--------|-------|
| LLM calls (7d) | not yet measured |
| Total cost (7d) | not yet measured |
| Avg cost/call | ~$0.0003 (Haiku, ~600 tokens input + 400 output) |
| Ring | lab |
| Budget class | experimental |

---

## Risk Assessment

1. **No prod mutation**: Appends to `opinions.jsonl` only. No writes to decisions, strategy_config.
2. **Rule-based pre-filters**: paranoid and catalyst_purist apply deterministic rules before
   calling LLM — avoiding cost leak from trivial cases.
3. **Domain abstention**: opportunist abstains in risk_off regime; anti_crowding abstains when
   no consensus data present. Both are pre-LLM checks.
4. **Annex sandbox**: no imports from bot.py, scheduler.py, order_executor.py, risk_kernel.py.

---

## Rollback Plan

1. Set `enable_personality_forks: false` in `strategy_config.json`
2. No restart required
3. `opinions.jsonl` is analytics-only

---

## Approval Sign-off

| Step | Done | Date |
|------|------|------|
| Lab-ring test ≥ 14 days | ⬜ | — |
| Pre-filter LLM-save rate ≥ 50% validated | ⬜ | — |
| All 5 forks producing non-abstaining opinions | ⬜ | — |
| Spine cost reviewed < $0.02/day | ⬜ | — |
| Flag moved to `shadow_flags` | ⬜ | — |
| CLAUDE.md updated | ⬜ | — |

---

*Contract version: 1.*
