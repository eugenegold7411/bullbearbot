# Promotion Contract — thesis_ecology v1

**Status:** DRAFT
**Module:** `annex/thesis_ecology.py` (T6.9)
**Evaluation class:** quality_positive_non_alpha
**Ring:** lab (skeleton) → shadow → prod
**Feature flag:** `enable_thesis_ecology` (lab_flags)
**Cost tier:** CHEAP (no LLM calls in v1)
**Annex sandbox contract:** enforced — no prod pipeline imports, skeleton-first

---

## Current state (skeleton phase)

This module accumulates data only. `format_ecology_for_review()` will always include
"SKELETON — insufficient data for modeling" until the promotion gate below is reached.
Do not promote until the skeleton prerequisite is satisfied.

---

## Prerequisite (must be met before evaluation begins)

- [ ] At least 90 snapshots in `data/annex/thesis_ecology/snapshots.jsonl` (≈30 days of
  daily market session snapshots at 3+ snapshots/day).
- [ ] At least 5 distinct thesis_type values observed in the snapshot corpus.
- [ ] At least 3 thesis co-occurrence pairs observed (same_symbol_different_thesis > 0 across
  multiple snapshots).

---

## Promotion criteria (ALL must pass)

### Quality gate

- [ ] **Co-occurrence stability:** The top 3 thesis co-occurrence pairs in
  `get_cooccurrence_counts()` are consistent across two consecutive 30-day windows
  (not driven by a single outlier cycle).
- [ ] **Competing direction detection:** competing_direction=True fires for at least 5 snapshots
  in the corpus. Confirms the detector is sensitive to real conflicts.
- [ ] **No data contamination:** Snapshots never contain position data from test or dry-run
  cycles. Confirm by checking that all recorded symbols match the live watchlist.

### Cost gate

- [ ] **$0.00 per cycle:** No LLM calls. Confirmed via spine — no records for module.

### Alignment gate

- [ ] **Ecology report added to weekly review:** `format_ecology_for_review()` output appears
  in at least one weekly review report without the "SKELETON" notice (indicates prerequisite met).
- [ ] **Schema owner review:** Any interpretation of co-occurrence data as "a pattern" requires
  human schema owner review before acting on it.

### Promotion decision

- [ ] Shadow ring: `format_ecology_for_review()` section injected into CTO prompt only.
  No injection into Strategy Director prompt until prod promotion.

---

## Disqualifiers (automatic fail)

- "SKELETON" notice removed from format function before prerequisite is met
- Any write to strategy_config.json or decision objects derived from ecology data

---

**Last updated:** 2026-04-16
**Next review:** after 90-snapshot prerequisite is met
