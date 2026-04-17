# Promotion Contract — ghost_advisors (Lab → Shadow)

**Status:** DRAFT

---

## Module Information

| Field | Value |
|-------|-------|
| Module name | `annex/ghost_advisors.py` |
| Ring | lab → shadow |
| Feature flag | `enable_ghost_advisors` (lab_flags) |
| Evaluation class | `quality_positive_non_alpha` |
| Created | 2026-04-16 |
| Reviewed by | — |
| Approved by | — |

---

## Purpose

`annex/ghost_advisors.py` provides a 5-persona ghost advisor panel: momentum_first, macro_first,
mean_reversion_bias, risk_minimizer, event_driven_only. Each gives one Haiku-generated opinion
per case (forensic record, replay, etc.) from their fixed investment philosophy. Not wired into
live cycles — called from `replay_debugger.py` and post-forensic weekly review sections.
Opinions logged to `data/annex/ghost_advisors/opinions.jsonl`.

---

## Ghost Personas

| Persona | Philosophy Summary |
|---------|-------------------|
| `momentum_first` | Ride trends, price action is the signal, cut losses fast |
| `macro_first` | Regime > individual stocks; rates/dollar/credit determines direction |
| `mean_reversion_bias` | Prices always revert; fade extremes, never chase |
| `risk_minimizer` | Capital preservation first; only asymmetric bets; best trade is often no trade |
| `event_driven_only` | Only trade knowable events; no thesis = no position |

---

## Promotion Criteria

### Required

- [ ] Feature flag `enable_ghost_advisors` tested with `true` for ≥ 14 trading days
- [ ] No FAIL/CRITICAL incidents linked to this module
- [ ] Module importable without error
- [ ] All 5 ghosts return `GhostOpinion` with schema_version=1 on a valid case
- [ ] Out-of-domain abstention fires correctly (e.g., `momentum_first` abstains on mean-reversion case)
- [ ] `test_ghost_abstains_when_case_outside_philosophy` and `test_all_five_ghosts_return_structured_output` both pass
- [ ] Agent 4 (Backtest Analyst) or post-forensic review shows ghost consensus producing useful signal

### Recommended

- [ ] `missed_risk_flag` rate validated — not too high (false alarms) or too low (missing real risks)
- [ ] `agrees_with_prod` divergence from 5-ghost panel correlates with eventual forensic verdict
- [ ] Weekly CTO review shows ghost cost < $0.005/case when flag is true (5 calls × ~$0.001 each)

---

## Observed Cost (lab ring)

| Metric | Value |
|--------|-------|
| LLM calls per case | 5 (one per ghost, parallel) |
| Cost per case | ~$0.005 (5 × Haiku, ~500 tokens input + 350 output each) |
| Ring | lab |
| Budget class | experimental |

---

## Risk Assessment

1. **Not in live cycle path**: Only called from replay_debugger and weekly review. No latency risk to main pipeline.
2. **5 calls per case**: Cost multiplier vs single-call modules. Rate-limit to post-forensic subjects only.
3. **Out-of-domain abstention**: Fast-path keyword check before LLM call reduces unnecessary cost.
4. **Opinion quality**: Persona fidelity depends on Haiku's instruction-following. Validated by review.

---

## Rollback Plan

1. Set `enable_ghost_advisors: false` in `strategy_config.json`
2. No restart required
3. `opinions.jsonl` is analytics-only

---

## Approval Sign-off

| Step | Done | Date |
|------|------|------|
| Lab-ring test ≥ 14 days | ⬜ | — |
| Ghost opinion quality reviewed on 5+ real forensic cases | ⬜ | — |
| Out-of-domain abstention validated per-persona | ⬜ | — |
| Agrees-with-prod signal correlation measured | ⬜ | — |
| Flag moved to `shadow_flags` | ⬜ | — |
| CLAUDE.md updated | ⬜ | — |

---

*Contract version: 1.*
