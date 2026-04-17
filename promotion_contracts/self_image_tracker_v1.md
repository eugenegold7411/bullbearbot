# Promotion Contract — self_image_tracker (Lab → Shadow)

**Status:** DRAFT

---

## Module Information

| Field | Value |
|-------|-------|
| Module name | `annex/self_image_tracker.py` |
| Ring | lab → shadow |
| Feature flag | `enable_self_image_tracker` (lab_flags) |
| Evaluation class | `quality_positive_non_alpha` |
| Created | 2026-04-16 |
| Reviewed by | — |
| Approved by | — |

---

## Purpose

`annex/self_image_tracker.py` builds a longitudinal profile comparing the bot's stated identity
(regime views, conviction claims, stated concerns) against its actual behavior (hold rate,
avg conviction on submitted trades, top catalysts). Drift detection flags significant shifts
between weekly snapshots. No LLM calls. Snapshots appended to
`data/annex/self_image_tracker/snapshots.jsonl`.

---

## Promotion Criteria

### Required

- [ ] Feature flag `enable_self_image_tracker` tested with `true` for ≥ 4 consecutive weekly reviews
- [ ] No FAIL/CRITICAL incidents linked to this module
- [ ] Module importable without error
- [ ] At least 4 snapshots in `snapshots.jsonl` with valid `stated_regime_views` and `actual_hold_rate`
- [ ] Agent 2 (Risk Manager) review shows self-image summary producing useful output
- [ ] Drift detection has fired at least once on real data OR validated on synthetic data

### Recommended

- [ ] Hold rate drift threshold (0.15) validated — too sensitive or too loose?
- [ ] Avg conviction drift threshold (0.10) validated
- [ ] `top_catalyst` change detection produces actionable signal (not just noise)

---

## Observed Cost (lab ring)

| Metric | Value |
|--------|-------|
| LLM calls | none (pure analytics) |
| Cost | $0.00 |
| Ring | lab |
| Budget class | negligible |

---

## Risk Assessment

1. **No LLM calls**: Zero inference cost or latency risk.
2. **Read-only on prod data**: Only reads `memory/decisions.json`. No writes to prod artifacts.
3. **Snapshot accumulation**: `snapshots.jsonl` grows ~1 entry/week. Negligible storage.

---

## Rollback Plan

1. Set `enable_self_image_tracker: false` in `strategy_config.json`
2. No restart required
3. `snapshots.jsonl` is analytics-only

---

## Approval Sign-off

| Step | Done | Date |
|------|------|------|
| Lab-ring test ≥ 4 weekly reviews | ⬜ | — |
| Agent 2 output reviewed for signal quality | ⬜ | — |
| Drift thresholds validated | ⬜ | — |
| Flag moved to `shadow_flags` | ⬜ | — |
| CLAUDE.md updated | ⬜ | — |

---

*Contract version: 1.*
