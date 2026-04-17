# Promotion Contract — taxonomy_suggester v1

**Status:** DRAFT
**Module:** `annex/taxonomy_suggester.py` (T6.17)
**Evaluation class:** quality_positive_non_alpha
**Ring:** lab (skeleton) → shadow → prod (governance support only)
**Feature flag:** `enable_taxonomy_suggester` (lab_flags)
**Cost tier:** CHEAP (no LLM calls in v1)
**Annex sandbox contract:** enforced — no prod pipeline imports; is_auto_generated=False invariant

---

## Design invariants (non-negotiable)

1. `is_auto_generated = False` on every suggestion record. The `submit_suggestion()` function
   enforces this regardless of caller input.
2. Suggestions are human-initiated only. The module never proposes changes autonomously.
3. No writes to `taxonomy_v1.0.0.md`. Suggestions accumulate in `suggestions.jsonl` only.
4. Schema owner review is required before any suggestion is accepted or acted on.

---

## Prerequisite (must be met before evaluation begins)

- [ ] At least 90 days of catalyst_log.jsonl and thesis_checksums.jsonl data (for meaningful
  usage statistics).
- [ ] At least one human-submitted suggestion via `submit_suggestion()`.
- [ ] At least one human schema owner review of suggestions.jsonl.

---

## Promotion criteria (ALL must pass)

### Quality gate

- [ ] **Usage stats coverage:** `get_label_usage_stats()` returns non-empty dicts for both
  catalyst_type and thesis_type after 90 days of data.
- [ ] **Unknown pattern detection:** `get_unknown_catalyst_patterns()` identifies ≥3 recurring
  patterns from unknown catalyst raw_text after 90 days. If no patterns emerge, the catalog
  may already be adequate or raw_text is not being logged.
- [ ] **is_auto_generated invariant:** All records in suggestions.jsonl have
  `is_auto_generated: false`. Automated audit:
  `grep '"is_auto_generated": true' suggestions.jsonl` must return nothing.
- [ ] **Schema owner review cadence:** At least one human schema owner review per quarter.
  If no reviews have occurred after 90 days, the module is producing unused output.

### Cost gate

- [ ] **$0.00 per cycle:** No LLM calls. Confirmed via spine — no records for module.

### Alignment gate

- [ ] **No taxonomy writes:** Confirm `taxonomy_v1.0.0.md` has not been modified since the
  module was enabled. The module is a suggestion engine only; humans own taxonomy changes.
- [ ] **Suggestion lifecycle tracked:** All suggestions have status = "pending", "accepted",
  or "rejected". No suggestions remain pending > 90 days without explicit human review.

### Promotion to shadow

- [ ] `format_usage_report_for_review()` section injected into CTO weekly review prompt.
  The "schema owner review required" notice must remain in the output permanently.

### Promotion to prod governance support

- [ ] At least 1 suggestion accepted by schema owner and implemented in taxonomy_v1.0.0.md.
  This proves the suggestion pipeline has end-to-end value.
- [ ] At least 3 pattern → suggestion → accepted cycles completed.

---

## Disqualifiers (automatic fail)

- Any suggestion record with `is_auto_generated: true`
- Any automated write to taxonomy_v1.0.0.md
- Schema owner review notice removed from `format_usage_report_for_review()` output

---

**Last updated:** 2026-04-16
**Next review:** after 90-day data prerequisite and first human schema owner review
