# Promotion Contract — reputation_economy v1

**Status:** DRAFT
**Module:** `annex/reputation_economy.py` (T6.7)
**Evaluation class:** quality_positive_non_alpha
**Ring:** lab → shadow → prod
**Feature flag:** `enable_reputation_economy` (lab_flags)
**Cost tier:** CHEAP (no LLM calls in v1 — analytics only)
**Annex sandbox contract:** enforced — no prod pipeline imports, no writes to execution paths

---

## Prerequisite (must be met before evaluation begins)

- [ ] At least n=30 decision outcomes logged to `decision_outcomes.jsonl` with
  `alpha_classification` field populated (non-null).
- [ ] At least 5 distinct catalyst_type values with ≥5 linked outcomes each
  (required for `score_status="active"` to appear for any entity).

---

## Promotion criteria (ALL must pass)

### Quality gate

- [ ] **Active records emerge:** At least 5 entities reach `score_status="active"` within the
  evaluation window (sample_threshold=5 by default). If none emerge, the module is collecting
  data but has no output signal — extend evaluation window.
- [ ] **Score distribution:** Active entity scores span the range [0.3, 0.8] across at least
  5 active records. A distribution clustered near 0.5 indicates insufficient data or calibration
  failure.
- [ ] **Trend detection:** At least 2 entities show trend ≠ "unknown" after 6+ linked outcomes.
- [ ] **Formula correctness audit:** Random-sample 3 active records from reputations.json.
  Manually verify: `reputation_score == win_rate * 0.7 + (1 - loss_rate) * 0.3`.
- [ ] **Neutral prior invariant:** All entities with outcome_linked_count < 5 must have
  `reputation_score == 0.5` and `score_status == "insufficient_sample"`. Zero exceptions.

### Cost gate

- [ ] **$0.00 per cycle:** No LLM calls. Confirm via cost_attribution_spine — no spine records
  with module_name="reputation_economy".

### Alignment gate

- [ ] **Low-reputation entities:** Identify the bottom 3 scoring entities after 30+ outcomes.
  Manually review whether their low scores correspond to genuine poor-performing catalysts or
  theses (not data artifacts).
- [ ] **High-reputation entities:** Same review for top 3. Confirms formula is not inverted.
- [ ] **No prod write path:** Confirm `reputations.json` is read-only from the prod pipeline
  perspective. `grep -r "reputation_economy" bot.py weekly_review.py` — output must be
  read-only calls only.

### Promotion decision

- [ ] Shadow ring: enable in shadow_flags, run for 30 additional outcome-linked cycles.
- [ ] Prod promotion: reputation scores injected into Strategy Director memo context only
  (never into risk_kernel or order_executor).

---

## Disqualifiers (automatic fail)

- Any entity with n ≥ 5 showing reputation_score = 0.5 exactly (formula not applied)
- Any spine record showing LLM cost for this module
- Reputation scores used to block or approve orders (out of scope for v1)

---

**Last updated:** 2026-04-16
**Next review:** after n=30 outcome prerequisites are met
