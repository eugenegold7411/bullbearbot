# Promotion Contract — ranking_tournament (Lab → Shadow)

**Status:** DRAFT

---

## Module Information

| Field | Value |
|-------|-------|
| Module name | `annex/ranking_tournament.py` |
| Ring | lab → shadow |
| Feature flag | `enable_annex_ranking_tournament` (lab_flags) |
| Evaluation class | `exploratory` |
| Created | 2026-04-16 |
| Reviewed by | — |
| Approved by | — |

---

## Purpose

`annex/ranking_tournament.py` runs round-robin pairwise comparisons between annex artifacts
(theories, opinions, confessions) from the same case, using a rubric-based scoring function.
No LLM calls. Rubric: specificity (0.40 weight), testability (0.35), calibration (0.25).
Tie if delta < 0.05. Minimum 3 comparisons required for leaderboard entry. Results logged to
`data/annex/ranking_tournament/comparisons.jsonl`. Purpose: identify which annex modules
produce the most actionable, testable, calibrated outputs over time.

---

## Promotion Criteria

### Required

- [ ] Feature flag `enable_annex_ranking_tournament` tested with `true` for ≥ 14 trading days
- [ ] Minimum 3 comparisons accumulated for at least 2 distinct source modules (leaderboard non-empty)
- [ ] No FAIL/CRITICAL incidents linked to this module
- [ ] Module importable without error
- [ ] Weekly review Agent 1 shows leaderboard section with at least 1 ranked entry
- [ ] Tie rate reviewed — excessive ties (> 60%) may indicate rubric is under-discriminating

### Recommended

- [ ] Retrospective validation: check `later_evidence_supported` field after 30+ days
      — winners should be supported by evidence at rate > 50%
- [ ] Rubric weight review: if specificity dominates (all wins are specificity-driven),
      consider rebalancing toward testability
- [ ] Review whether top-ranked module correlates with better weekly review quality ratings

---

## Observed Cost (lab ring)

| Metric | Value |
|--------|-------|
| LLM calls (7d) | 0 (no LLM calls — rubric-based only) |
| Total cost (7d) | $0.00 |
| Avg cost/call | $0.00 |
| Ring | lab |
| Budget class | negligible |

---

## Risk Assessment

1. **No prod mutation**: Appends to `comparisons.jsonl` only. No writes to decisions, strategy_config.
2. **No LLM calls**: Rubric scoring is fully deterministic — zero marginal cost.
3. **Rubric subjectivity**: The scoring functions encode value judgments about what makes a
   "good" artifact. These should be validated empirically, not assumed correct.
4. **Leaderboard minimum**: 3-comparison gate prevents noisy single-comparison rankings.
5. **Annex sandbox**: no imports from bot.py, scheduler.py, order_executor.py, risk_kernel.py.

---

## Rollback Plan

1. Set `enable_annex_ranking_tournament: false` in `strategy_config.json`
2. No restart required
3. `comparisons.jsonl` is analytics-only

---

## Approval Sign-off

| Step | Done | Date |
|------|------|------|
| Lab-ring test ≥ 14 days | ⬜ | — |
| Leaderboard non-empty (≥ 2 sources with ≥ 3 comparisons) | ⬜ | — |
| Tie rate < 60% | ⬜ | — |
| `later_evidence_supported` retrospective (30+ days) | ⬜ | — |
| Flag moved to `shadow_flags` | ⬜ | — |
| CLAUDE.md updated | ⬜ | — |

---

*Contract version: 1.*
