# Promotion Contract — internal_parliament v1

**Status:** DRAFT
**Module:** `annex/internal_parliament.py` (T6.6)
**Evaluation class:** quality_positive_non_alpha
**Ring:** lab → shadow → prod
**Feature flag:** `enable_internal_parliament` (lab_flags)
**Cost tier:** CHEAP (Haiku × 4 calls per convened session)
**Annex sandbox contract:** enforced — no prod pipeline imports, no writes to execution paths

---

## Prerequisite (must be met before evaluation begins)

- [ ] At least 30 market cycles with the flag enabled and parliament convening at least once per
  week (triggers fire naturally — do not force-trigger for evaluation purposes).

---

## Promotion criteria (ALL must pass)

### Quality gate

- [ ] **Trigger precision:** ≥80% of sessions convened correspond to a trigger condition that
  was legitimately active (not spurious flag combinations). Manual audit of sessions.jsonl.
- [ ] **Verdict distribution:** At least 3 of 5 valid verdict values (`proceed`, `hold`,
  `reduce`, `exit`, `abstain`) observed across the evaluation window.
- [ ] **Risk auditor veto rate:** minority_veto_issued fires in ≥10% of sessions where
  synthesis_confidence < 0.65. (Sanity check: veto_power is only meaningful if it ever fires.)
- [ ] **No abstention dominance:** abstention (synthesis_verdict="abstain") rate < 50% across
  all sessions. A parliament that always abstains provides no signal.
- [ ] **Synthesis coherence:** synthesis_verdict must always be one of `_VALID_VERDICTS`. Zero
  invalid verdict strings in sessions.jsonl.

### Cost gate

- [ ] **Cost per session:** Average cost ≤ $0.002 (4 Haiku calls at ~$0.0005 each).
- [ ] **Trigger rate:** Parliament convenes in ≤25% of market cycles (not every cycle).
  If trigger rate exceeds 25%, tighten trigger predicates before promotion.

### Alignment gate

- [ ] **Post-hoc verdict correlation (manual):** Reviewer reads 10 sessions where verdict ≠
  main decision and assesses whether the parliament's minority view had merit in retrospect.
  At least 5/10 must be considered "useful signal" by the reviewer.
- [ ] **No prod pipeline influence:** Confirm sessions.jsonl is the only write path.
  `grep -r "convene_parliament" bot.py` must show non-fatal try/except wrapping only.

### Promotion decision

- [ ] Shadow ring: enable in shadow_flags and run 2 additional weeks before prod promotion.
- [ ] Prod promotion requires a second sign-off from the weekly review Strategy Director memo.

---

## Disqualifiers (automatic fail)

- Any session where synthesis_verdict is outside `_VALID_VERDICTS`
- Any unhandled exception that propagates outside the non-fatal try/except and affects main cycle
- Evidence that parliament output influenced order submission directly

---

**Last updated:** 2026-04-16
**Next review:** after 30-session evaluation window
