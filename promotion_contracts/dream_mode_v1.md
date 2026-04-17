# Promotion Contract — dream_mode v1

**Status:** DRAFT
**Module:** `annex/dream_mode.py` (T6.12)
**Evaluation class:** exploratory — no alpha claim
**Ring:** lab → (no prod promotion planned for v1)
**Feature flag:** `enable_dream_mode` (lab_flags)
**Cost tier:** DEFAULT (Sonnet — intentionally; unconstrained hypothesis generation)
**Annex sandbox contract:** enforced — no prod pipeline imports; is_hypothesis=True invariant

---

## Design invariants (non-negotiable)

1. `is_hypothesis = True` on every session record, always. Zero exceptions.
2. Harvested ideas NEVER auto-persist to strategy_config.json, risk_kernel, or any execution path.
3. Harvested ideas are human-curated artifacts. The curator (human) decides what, if anything,
   to act on. The module has no write path to prod systems.
4. `evaluation_class = "exploratory — no alpha claim"` on every session record. Never claim
   dream output is actionable without an independent evaluation cycle.

---

## Prerequisite (must be met before evaluation begins)

- [ ] At least 10 dream sessions recorded in `data/annex/dream_mode/sessions.jsonl`.
- [ ] At least 3 sessions manually reviewed (harvest_status = "harvested" or "discarded").

---

## Promotion criteria (ALL must pass)

### Quality gate

- [ ] **is_hypothesis invariant:** All records in sessions.jsonl have `is_hypothesis: true`.
  Zero exceptions. Automated audit: `grep '"is_hypothesis": false' sessions.jsonl` must return
  nothing.
- [ ] **Evaluation class invariant:** All records have `evaluation_class` containing
  "no alpha". Automated audit: `grep '"evaluation_class"' sessions.jsonl | grep -v "no alpha"`
  must return nothing.
- [ ] **Harvest rate:** At least 30% of sessions reviewed (harvest_status ≠ "raw") after 10+
  sessions. Unreviewed sessions indicate the module is producing content no one reads — a cost
  leak with no benefit.
- [ ] **Hypothesis quality (subjective):** Curator review of 5 harvested sessions. At least
  3 must contain a hypothesis the curator considers "interesting enough to track". Purely
  generic or hallucinated outputs are disqualifying.
- [ ] **Cost per session:** Average cost ≤ $0.05 (Sonnet: ~3K input + 500 output tokens).
  If sessions consistently exceed $0.05, reduce context window before next review.

### Cost gate

- [ ] **Gated off-hours:** dream sessions run only when enabled and outside market hours
  (no market-session triggering). Confirm no spine records during 9:30 AM – 4:00 PM ET.
- [ ] **Frequency cap:** ≤ 1 session per day. If multiple per day are firing, add a daily
  frequency cap before promotion.

### Alignment gate

- [ ] **No auto-application:** Confirm that zero harvested idea strings appear in
  strategy_config.json, bot.py, or any prompt file without an explicit human commit.
- [ ] **No shadow or prod ring:** dream_mode remains lab-only. There is no planned prod
  promotion path in v1. This contract governs lab → "trusted lab" only.

### Trusted lab status (replaces standard prod promotion)

- [ ] Curator has reviewed ≥20 sessions and found the hypothesis quality consistently
  interesting.
- [ ] Cost profile is stable (≤ $0.05/session, ≤ 1/day).
- [ ] is_hypothesis and evaluation_class invariants confirmed over 30+ day window.

---

## Disqualifiers (automatic fail)

- Any session record with `is_hypothesis: false`
- Any harvested idea auto-applied to a prod path without human commit
- Cost per session > $0.20 (unconstrained output)

---

**Last updated:** 2026-04-16
**Next review:** after 10 sessions + 3 human reviews
