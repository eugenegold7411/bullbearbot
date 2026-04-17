# Promotion Contract — self_deception_detector (Lab → Shadow)

**Status:** DRAFT

---

## Module Information

| Field | Value |
|-------|-------|
| Module name | `annex/self_deception_detector.py` |
| Ring | lab → shadow |
| Feature flag | `enable_self_deception_detector` (lab_flags) |
| Evaluation class | `quality_positive_non_alpha` |
| Created | 2026-04-16 |
| Reviewed by | — |
| Approved by | — |

---

## Purpose

`annex/self_deception_detector.py` detects patterns where the bot's stated reasoning diverges
from actual behavior. Five rule-based detectors (no LLM): confidence/outcome mismatch, catalyst
fabrication, regime contradiction, repeated forensic mistakes, and thesis drift within 24h.
Signals appended to `data/annex/self_deception_detector/signals.jsonl`.

---

## Promotion Criteria

### Required

- [ ] Feature flag `enable_self_deception_detector` tested with `true` for ≥ 14 trading days
- [ ] No FAIL/CRITICAL incidents linked to this module
- [ ] Module importable without error
- [ ] At least 1 signal of each type detected on real data OR confirmed zero false positives on synthetic data
- [ ] Agent 10 (Compliance) review shows signal summary producing useful output
- [ ] `test_catalyst_fabrication_detected` and `test_no_false_positive_on_valid_decision` both pass

### Recommended

- [ ] Signal confidence thresholds reviewed against real detection rate (catalyst_fabrication=0.85, regime_contradiction=0.7)
- [ ] `_detect_repeated_same_mistake` threshold of 3 validated against forensic log density

---

## Observed Cost (lab ring)

| Metric | Value |
|--------|-------|
| LLM calls | none (pure rule-based) |
| Cost | $0.00 |
| Ring | lab |
| Budget class | negligible |

---

## Risk Assessment

1. **No LLM calls**: Pure analytics — no inference cost or latency risk.
2. **Read-only on prod data**: Reads `memory/decisions.json` and analytics logs. Never writes to them.
3. **False positive risk**: Conservative thresholds (confidence < 1.0 on all detectors). Signals are advisory only.

---

## Rollback Plan

1. Set `enable_self_deception_detector: false` in `strategy_config.json`
2. No restart required — flag checked at call time
3. `signals.jsonl` is analytics-only — safe to archive

---

## Approval Sign-off

| Step | Done | Date |
|------|------|------|
| Lab-ring test ≥ 14 days | ⬜ | — |
| Agent 10 output reviewed for signal quality | ⬜ | — |
| False positive audit on 30 days of real decisions | ⬜ | — |
| Flag moved to `shadow_flags` | ⬜ | — |
| CLAUDE.md updated | ⬜ | — |

---

*Contract version: 1.*
