# Opus 4.7 vs Sonnet 4.6 — Pre-Registered Evaluation Protocol

**Status:** LOCKED — do not edit after first API call fires
**Author:** Eugene Gold
**Created:** 2026-04-16
**Purpose:** Determine whether Opus 4.7 warrants T1.8 (Model Tiering Policy) build, and if so, on which subsystems.

---

## Core Question

**Does Opus 4.7 surface information that changes what I would do next, on the specific artifact types BullBearBot produces?**

Explicitly NOT the question: "Is Opus 4.7 better written, more thorough, more articulate, or more confident-sounding than Sonnet 4.6?"

## Locked Artifact Set

Target: 7–10 artifacts. Resolved ground truth only. No pending recommendations.

| Class | Count | Ground truth | Promotion evidence? |
|-------|-------|--------------|---------------------|
| Forensic (closed A1 trades) | 2 (BTC win, ETH loss) | YES — actual outcome | YES |
| Weekly review synthesis | 2 (most recent Agent 6 runs with non-trivial parameter changes) | PARTIAL — parameter changes observable, effect traceable | YES |
| Director recommendations with RESOLVED verdicts | 1–4 (only resolved, no pending) | YES — verdict on file | YES |
| Reconstructed forensic (optional, if recommendation count low) | 0–1 | YES — post-hoc reconstruction from existing closed trade artifacts | YES (flagged as reconstructed) |
| A2 observation-mode debate | 2 | NO | NO — judgment-only, flagged `promotion_evidence=false` |

**If resolved recommendation count drops the total below 7:** add one reconstructed forensic case from pre-2026-04-13 closed-trade artifacts if available honestly. Otherwise run with smaller n. Do not pad with pending recommendations.

## Scoring Rubric (LOCKED)

Each artifact scored on these dimensions. Score all outputs blind before revealing model mapping.

### Information Surfacing (0/1/2)
- `0` = identified nothing the other model didn't
- `1` = identified something the other missed, but relevance is questionable
- `2` = identified something the other missed that is clearly material

### Correctness (0/1, or N/A)
- `0` = claim about what happened does not match what actually happened
- `1` = claim matches what actually happened
- `N/A` = case has no ground truth (A2 observation cases only)

### Actionability (0/1/2) — output SHAPE
- `0` = output is commentary, no implied action
- `1` = output implies an action but doesn't specify it
- `2` = output specifies a concrete change (parameter, threshold, process, code location)

### Decision Delta (0/1) — output EFFECT on operator
- `1` = this output would cause me to: modify `strategy_config.json`, open a ticket, change a threshold, add a pattern to the watchlist, reclassify a closed trade, or modify a prompt
- `0` = anything else (including "confirms what I already believed", "interesting observation", "restates the obvious")

Decision delta is scored against operator's *current state of belief*, not against an abstract reader.

### Calibration (0/1/2)
- `0` = expresses confidence where evidence is weak, OR abstains where evidence is strong
- `1` = mixed; some claims well-calibrated, some not
- `2` = confidence tracks evidence throughout

Confident-sounding wrong answer scores `0`, not `1`.

### Novel Failure Mode (FLAG, not score)
Binary flag: did this model identify a failure mode, pattern, or risk that is new to the operator?

Counts toward tie-break ONLY if:
- The case has ground truth (closed trade, resolved recommendation, parameter-effect observable), OR
- The failure mode is verifiable within 90 days against a future closed trade, weekly review, or resolved recommendation

"Verifiable within 90 days" means: a concrete predicate is stated that can be checked against future artifacts. Vague claims ("regime may shift") do not qualify.

### Explicitly NOT Scored
- Prose quality
- Thoroughness / length
- Tone / confidence register
- Structural organization
- Whether the output "feels thoughtful"

These are the persuasion axes. They are off-limits.

## Metadata Captured Per Call

Every API call logs:
- `artifact_id`
- `model` (opaque `A` or `B` at score time; resolved after scoring)
- `run_timestamp`
- `cache_hit_input_tokens`
- `cache_write_input_tokens`
- `uncached_input_tokens`
- `output_tokens`
- `wall_clock_ms`
- `estimated_cost_usd` (computed from token counts × current published rates)

Effective cost multiplier computed per-artifact as `(Opus total $) / (Sonnet total $)`, NOT from list price alone. Per-artifact variation expected due to cache behavior.

## Pre-Registered Decision Criteria

Results computed per artifact class BEFORE aggregate. Aggregate computed on `promotion_evidence=true` subset only (8 artifacts max, excluding A2 observation cases).

### Outcome A — Opus wins on synthesis + forensics + calibration
**Trigger:** Opus beats Sonnet on information surfacing, correctness, and calibration in the forensic class AND the weekly synthesis class. Aggregate decision delta score higher for Opus in at least 2 of 3 ground-truth classes.

**Action:** T1.8 proceeds. Opus allowed for:
- Strategy Director final synthesis (ONLY after director memo verdict history reaches ≥4 resolved recommendations)
- CTO synthesis (Agent 5)
- Post-trade forensic reviewer (T2.3 when built)

NOT allowed for:
- A1 real-time decisions
- A2 live-cycle calls

### Outcome B — Opus wins on synthesis only, ties on forensics
**Trigger:** Opus beats Sonnet on weekly synthesis class. Forensic class is a tie or near-tie on correctness and decision delta.

**Action:** T1.8 proceeds but narrower. Opus only for weekly review synthesis roles. Forensic reviewer stays on Sonnet. Narrative Director (Agent 11) gets Opus first as the lowest-stakes test bed before CTO.

### Outcome C — Opus wins only on novel-failure-mode flag
**Trigger:** Opus raises ≥2 novel failure mode flags that meet ground-truth or 90-day-verifiability criteria, but does NOT clearly win on information surfacing or correctness aggregates.

**Action:** T1.8 deferred. Re-run protocol in 4 weeks with fresh artifact set. Do not proceed on single flashy cases.

### Outcome D — Opus wins on articulation but not on information, correctness, or calibration
**Trigger:** Opus's information surfacing, correctness, calibration, and decision delta scores are at or below Sonnet's, despite subjectively "feeling" more thorough.

**Action:** T1.8 shelved. The persuasion trap is confirmed. Focus shifts to T0.7 (cost attribution) and T3.6 (compact/full routing) for cost optimization. Revisit Opus evaluation in 6+ months or when Opus pricing changes materially.

### Outcome E — Opus loses or ties on everything
**Trigger:** Sonnet equals or exceeds Opus on aggregate across all scored dimensions.

**Action:** T1.8 shelved. Existing Sonnet prompting is extracting the real information content. Do not reopen Opus evaluation without a new trigger (new model release, prompt architecture change, or dramatic pricing shift).

## Tie-Break Rules

If outcome is ambiguous between two categories:

1. **Information surfacing without correctness/calibration does NOT justify upgrade.** Surfacing claims that turn out wrong is worse than silence.

2. **Novel failure mode flag counts toward upgrade ONLY IF:** ≥2 flags meet ground-truth or 90-day-verifiability criteria, AND they appear in at least 2 different artifact classes. A cluster of flags in one class is evidence about that class only.

3. **Class-specific wins produce class-specific upgrades.** "Opus wins on weekly synthesis" does not generalize to "Opus wins on forensics."

4. **A2 observation cases have zero weight in tie-break.** `promotion_evidence=false`. Their only purpose in this run is documenting operator judgment for future comparison when A2 has resolved structures.

## Anti-Patterns to Avoid (LOCKED)

- Do NOT extend artifact set mid-experiment
- Do NOT re-score after seeing cost data
- Do NOT re-score after seeing model identity
- Do NOT let a single dramatic insight override the aggregate
- Do NOT update the rubric after seeing any output
- Do NOT pad with pending recommendations
- Do NOT score prose quality, even implicitly

## Commitment

I commit to executing the decision pre-registered above based on the rubric pre-registered above. If the result is ambiguous by the tie-break rules, the pre-registered action is "defer T1.8 and re-run in 4 weeks," not "look at the outputs again and decide."

Signed: _______________________  Date: _______________________

---

## File Integrity

SHA-256 of this file captured after commit, before first API call:
```
(computed at run time and logged to run manifest)
```

If this file is modified after the integrity hash is captured, the run is invalidated.
