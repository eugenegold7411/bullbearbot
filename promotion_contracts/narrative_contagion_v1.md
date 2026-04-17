# Promotion Contract — narrative_contagion v1

**Status:** DRAFT
**Module:** `annex/narrative_contagion.py` (T6.10)
**Evaluation class:** quality_positive_non_alpha
**Ring:** lab (skeleton) → shadow → prod
**Feature flag:** `enable_narrative_contagion` (lab_flags)
**Cost tier:** CHEAP (no LLM calls in v1)
**Annex sandbox contract:** enforced — no prod pipeline imports, skeleton-first

---

## Current state (skeleton phase)

This module records potential contagion candidates for later analysis only. It does not
simulate, model, or predict propagation. `format_contagion_for_review()` always includes
"SKELETON — insufficient data for simulation". Do not promote until the prerequisite below.

---

## Prerequisite (must be met before evaluation begins)

- [ ] At least 60 events in `data/annex/narrative_contagion/events.jsonl` (≈30 trading days
  of market-hours recording).
- [ ] At least 5 distinct source_catalyst_type values observed.
- [ ] At least 10 events where observed_propagation is non-empty (actual propagation detected
  above the signal threshold of 15).

---

## Promotion criteria (ALL must pass)

### Quality gate

- [ ] **Propagation base rate:** observed_propagation non-empty in ≥20% of recorded events.
  If < 20%, the signal threshold (15) may be too high or sector_map coverage is insufficient.
- [ ] **Sector peer coverage:** At least 8 distinct source symbols with ≥3 recorded events each.
- [ ] **Lag data availability:** At least 20 events have a non-null lag_hours field
  (requires downstream wiring to update lag after propagation is confirmed).
- [ ] **Propagation stability:** The top 3 propagation pairs (source→peer) appear across
  multiple distinct market sessions, not just one event date.

### Cost gate

- [ ] **$0.00 per cycle:** No LLM calls. Confirmed via spine.

### Alignment gate

- [ ] **Contagion report in weekly review:** `format_contagion_for_review()` section appears
  in at least one CTO report without the "SKELETON" notice.
- [ ] **Manual review before acting:** Any contagion pattern used to influence watchlist
  additions requires explicit human approval and a strategy_config.json update signed by
  the Strategy Director weekly review memo.

### Promotion decision

- [ ] Shadow ring: `format_contagion_for_review()` injected into CTO prompt (informational).
- [ ] Prod promotion: propagation data may be offered as a signal suggestion to the morning
  brief. Never auto-applied to signal scores.

---

## Disqualifiers (automatic fail)

- "SKELETON" notice removed before prerequisite is met
- Propagation data directly modifying signal scores or order decisions

---

**Last updated:** 2026-04-16
**Next review:** after 60-event prerequisite is met
