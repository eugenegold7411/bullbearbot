# Promotion Contract — confession_channel (Lab → Shadow)

**Status:** DRAFT

---

## Module Information

| Field | Value |
|-------|-------|
| Module name | `annex/confession_channel.py` |
| Ring | lab → shadow |
| Feature flag | `enable_confession_channel` (lab_flags) |
| Evaluation class | `quality_positive_non_alpha` |
| Created | 2026-04-16 |
| Reviewed by | — |
| Approved by | — |

---

## Purpose

`annex/confession_channel.py` generates a structured confession artifact for a given module case.
Single Haiku call per case. Confession types: UNCERTAINTY, RULE_CONFLICT, SUPPRESSED_INTENT,
POSSIBLE_SHORTCUT. All confessions are hypotheses (`is_hypothesis=True` invariant). LLM abstains
if no genuine confession can be supported by the evidence. Evidence strength validated to
"weak"|"moderate"|"strong". Results logged to `data/annex/confession_channel/confessions.jsonl`.

---

## Promotion Criteria

### Required

- [ ] Feature flag `enable_confession_channel` tested with `true` for ≥ 14 trading days
- [ ] No FAIL/CRITICAL incidents linked to this module
- [ ] `is_hypothesis=True` verified on every record in `confessions.jsonl` (post-hoc audit)
- [ ] All 4 confession types (UNCERTAINTY, RULE_CONFLICT, SUPPRESSED_INTENT, POSSIBLE_SHORTCUT)
      observed at least once in 14-day window
- [ ] Abstention rate reviewed: if > 80%, evidence quality may be too low to be useful
- [ ] Module importable without error
- [ ] Weekly review shows confession section with ≥ 1 non-abstaining confession per week

### Recommended

- [ ] Evidence strength distribution reviewed: proportion of "strong" vs "weak" vs "moderate"
- [ ] SUPPRESSED_INTENT confessions reviewed qualitatively — do they identify real constraint
      conflicts that were previously invisible in prod reviews?
- [ ] Spine cost review: total cost < $0.01/day (Haiku rate × cases submitted)

---

## Observed Cost (lab ring)

| Metric | Value |
|--------|-------|
| LLM calls (7d) | not yet measured |
| Total cost (7d) | not yet measured |
| Avg cost/call | ~$0.0003 (Haiku, ~700 tokens input + 300 output) |
| Ring | lab |
| Budget class | experimental |

---

## Risk Assessment

1. **No prod mutation**: Appends to `confessions.jsonl` only. No writes to decisions, strategy_config.
2. **is_hypothesis invariant**: enforced in ALL code paths — dataclass default, abstaining path,
   LLM-response path. Cannot be set False without modifying source.
3. **Evidence integrity**: LLM instructed to abstain if no genuine confession supported by
   case_data. Empty `case_data` dict returns an abstaining record before any LLM call.
4. **Annex sandbox**: no imports from bot.py, scheduler.py, order_executor.py, risk_kernel.py.

---

## Rollback Plan

1. Set `enable_confession_channel: false` in `strategy_config.json`
2. No restart required
3. `confessions.jsonl` is analytics-only

---

## Approval Sign-off

| Step | Done | Date |
|------|------|------|
| Lab-ring test ≥ 14 days | ⬜ | — |
| `is_hypothesis=True` audit on all records | ⬜ | — |
| All 4 confession types observed | ⬜ | — |
| Abstention rate < 80% | ⬜ | — |
| Spine cost reviewed < $0.01/day | ⬜ | — |
| Flag moved to `shadow_flags` | ⬜ | — |
| CLAUDE.md updated | ⬜ | — |

---

*Contract version: 1.*
