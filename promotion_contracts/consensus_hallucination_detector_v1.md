# Promotion Contract — consensus_hallucination_detector (Lab → Shadow)

**Status:** DRAFT

---

## Module Information

| Field | Value |
|-------|-------|
| Module name | `annex/consensus_hallucination_detector.py` |
| Ring | lab → shadow |
| Feature flag | `enable_consensus_hallucination_detector` (lab_flags) |
| Evaluation class | `quality_positive_non_alpha` |
| Created | 2026-04-16 |
| Reviewed by | — |
| Approved by | — |

---

## Purpose

`annex/consensus_hallucination_detector.py` applies 4 rule-based detectors (no LLM calls) to
identify when apparent multi-source signal agreement may be an artifact of correlated data
rather than genuine convergence. Detectors: ALL_SIGNALS_AGREE_BAD_OUTCOME,
MORNING_BRIEF_ECHO, REDDIT_TECHNICAL_ALIGNMENT, SINGLE_SOURCE_AMPLIFICATION. Results logged
to `data/annex/consensus_hallucination/signals.jsonl`. No spine cost (no LLM).

---

## Promotion Criteria

### Required

- [ ] Feature flag `enable_consensus_hallucination_detector` tested with `true` for ≥ 14 trading days
- [ ] No FAIL/CRITICAL incidents linked to this module
- [ ] At least 1 signal of each detector type fired during 14-day window (validates coverage)
- [ ] Module importable without error
- [ ] Weekly review Agent 10 (Compliance) shows hallucination signal section
- [ ] Zero false-positive rate on verified alpha_positive outcomes (detector 1 must not fire
      when outcome is positive)

### Recommended

- [ ] Retrospective study: fraction of detected signals that preceded adverse outcomes (precision)
- [ ] Precision > 30% for ALL_SIGNALS_AGREE_BAD_OUTCOME type before promoting to shadow
- [ ] Review MORNING_BRIEF_ECHO rate — if > 80% of brief symbols trigger it, morning brief
      is too correlated with signal scorer (architectural concern)

---

## Observed Cost (lab ring)

| Metric | Value |
|--------|-------|
| LLM calls (7d) | 0 (no LLM calls — rule-based only) |
| Total cost (7d) | $0.00 |
| Avg cost/call | $0.00 |
| Ring | lab |
| Budget class | negligible |

---

## Risk Assessment

1. **No prod mutation**: Appends to `signals.jsonl` only. No writes to decisions, strategy_config.
2. **No LLM calls**: Rule-based only — zero marginal cost.
3. **False positive risk**: HIGH CONVICTION + BAD OUTCOME detector may flag genuinely
   unlucky trades. Threshold (top-3 convictions all > 0.7) is intentionally strict.
4. **Annex sandbox**: no imports from bot.py, scheduler.py, order_executor.py, risk_kernel.py.

---

## Rollback Plan

1. Set `enable_consensus_hallucination_detector: false` in `strategy_config.json`
2. No restart required
3. `signals.jsonl` is analytics-only

---

## Approval Sign-off

| Step | Done | Date |
|------|------|------|
| Lab-ring test ≥ 14 days | ⬜ | — |
| All 4 detector types have fired at least once | ⬜ | — |
| Zero false positives on alpha_positive outcomes (detector 1) | ⬜ | — |
| Morning brief echo rate reviewed | ⬜ | — |
| Flag moved to `shadow_flags` | ⬜ | — |
| CLAUDE.md updated | ⬜ | — |

---

*Contract version: 1.*
