# Promotion Contract — tom_profile (Lab → Shadow)

**Status:** DRAFT

---

## Module Information

| Field | Value |
|-------|-------|
| Module name | `annex/tom_profile.py` |
| Ring | lab → shadow |
| Feature flag | `enable_tom_profile` (lab_flags) |
| Evaluation class | `exploratory` |
| Created | 2026-04-16 |
| Reviewed by | — |
| Approved by | — |

---

## Purpose

`annex/tom_profile.py` builds a Theory-of-Mind structural model of how the bot "thinks" about
market participants (retail, institutional, market-makers) based on its own decision patterns.
Requires ≥ 50 decisions in the lookback window; abstains otherwise. Single Haiku call per
profile. Always labeled as hypothesis (`is_hypothesis=True` invariant, `evaluation_class=exploratory`).
Profiles logged to `data/annex/tom_profile/profiles.jsonl`. Not intended to influence prod.

---

## Promotion Criteria

### Required

- [ ] Feature flag `enable_tom_profile` tested with `true` for ≥ 14 trading days
- [ ] System has accumulated ≥ 50 decisions in any 30-day window (prerequisite for non-abstaining
      profiles — this requires the bot to have been live for at least ~30 trading days)
- [ ] No FAIL/CRITICAL incidents linked to this module
- [ ] `is_hypothesis=True` verified on every record in `profiles.jsonl` (post-hoc audit)
- [ ] Non-abstaining profile generated at least once in 14-day window
- [ ] All 3 participant model fields populated: `inferred_retail_model`, `inferred_institutional_model`,
      `inferred_market_maker_model`
- [ ] Module importable without error
- [ ] Weekly review shows ToM section

### Recommended

- [ ] Profile confidence review: if confidence consistently < 0.4, data patterns may be too
      sparse for meaningful inference — consider raising `_TOM_MIN_DECISIONS` to 100
- [ ] `sample_adequacy` audit: confirm "adequate" profiles (≥ 100 decisions) before drawing
      any conclusions from the inferred models
- [ ] Retrospective study: do regime_action_map entries match observed bot behavior in backtest?

---

## Observed Cost (lab ring)

| Metric | Value |
|--------|-------|
| LLM calls (7d) | not yet measured (weekly cadence — ~1 call/week) |
| Total cost (7d) | not yet measured |
| Avg cost/call | ~$0.0005 (Haiku, ~900 tokens input + 400 output) |
| Ring | lab |
| Budget class | negligible |

---

## Risk Assessment

1. **No prod mutation**: Appends to `profiles.jsonl` only. No writes to decisions, strategy_config.
2. **is_hypothesis invariant**: enforced in ALL code paths — dataclass default, abstaining path,
   LLM-response path. Explicitly labeled exploratory, never verified truth.
3. **Minimum sample gate**: 50-decision minimum prevents profiles from weak data.
4. **Annex sandbox**: no imports from bot.py, scheduler.py, order_executor.py, risk_kernel.py.
5. **Risk of over-interpretation**: The "Theory of Mind" framing is evocative — consumers of this
   output must understand these are structurally-derived hypotheses, not psychological facts.

---

## Rollback Plan

1. Set `enable_tom_profile: false` in `strategy_config.json`
2. No restart required
3. `profiles.jsonl` is analytics-only

---

## Approval Sign-off

| Step | Done | Date |
|------|------|------|
| Lab-ring test ≥ 14 days | ⬜ | — |
| ≥ 50 decisions in 30-day window (prerequisite) | ⬜ | — |
| `is_hypothesis=True` audit on all records | ⬜ | — |
| Non-abstaining profile generated and reviewed | ⬜ | — |
| Profile confidence reviewed (≥ 0.4 for useful output) | ⬜ | — |
| Flag moved to `shadow_flags` | ⬜ | — |
| CLAUDE.md updated | ⬜ | — |

---

*Contract version: 1.*
