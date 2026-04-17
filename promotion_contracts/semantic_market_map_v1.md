# Promotion Contract — semantic_market_map v1

**Status:** DRAFT
**Module:** `annex/semantic_market_map.py` (T6.11)
**Evaluation class:** quality_positive_non_alpha
**Ring:** lab (skeleton) → shadow → prod
**Feature flag:** `enable_semantic_market_map` (lab_flags)
**Cost tier:** CHEAP (no LLM calls in v1)
**Annex sandbox contract:** enforced — no prod pipeline imports, skeleton-first

---

## Current state (skeleton phase)

This module builds a co-occurrence graph of symbols and catalysts. Edge strength is normalized
to [0, 1] over 50 observations (min(1.0, count/50)). `format_map_for_review()` always includes
"SKELETON — visualization pending". Do not promote until prerequisites below are met.

---

## Prerequisite (must be met before evaluation begins)

- [ ] `nodes.json` contains ≥30 distinct nodes.
- [ ] `edges.json` contains ≥20 edges.
- [ ] At least 5 edges with observation_count ≥ 5 (approaching statistically non-trivial strength).
- [ ] At least 60 days of market-session recording.

---

## Promotion criteria (ALL must pass)

### Quality gate

- [ ] **Node stability:** Top 10 nodes by co_occurrence_count are consistent across two
  consecutive 30-day windows (high-volume symbols should dominate, not rotate randomly).
- [ ] **Edge strength calibration:** At least one edge reaches strength ≥ 0.3 (15+ observations).
  If no edge reaches 0.3 after 60 days, the co-occurrence signal is too sparse to be useful.
- [ ] **No phantom nodes:** Every node in nodes.json maps to a symbol in the live watchlist or a
  known catalyst_type. No garbage strings from malformed data.
- [ ] **Atomic write integrity:** nodes.json and edges.json are always valid JSON (no partial
  writes visible). Verify by reading both files immediately after a cycle where an upsert fired.

### Cost gate

- [ ] **$0.00 per cycle:** No LLM calls. Confirmed via spine.

### Alignment gate

- [ ] **Map report in weekly review:** `format_map_for_review()` appears in CTO report without
  the "SKELETON" notice.
- [ ] **Visualization deferred:** No visualization or graph rendering work until prod promotion.
  Data collection only in v1.
- [ ] **Schema owner review:** Any use of edge strengths to suggest watchlist changes requires
  schema owner sign-off.

### Promotion decision

- [ ] Shadow ring: map summary injected into CTO prompt. No injection into Strategy Director
  until prod promotion.
- [ ] Prod promotion: high-strength edges (≥ 0.5) may be offered as cross-sector signal hints
  in the morning brief context. Never auto-applied.

---

## Disqualifiers (automatic fail)

- Partial JSON write corrupts nodes.json or edges.json mid-cycle
- Edge strength used to modify signal scores directly

---

**Last updated:** 2026-04-16
**Next review:** after 60-day / 30-node prerequisite is met
