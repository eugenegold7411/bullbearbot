# Opus 4.7 Evaluation — Run Book

One-afternoon experiment. Follow in order. Do not skip steps.

## 0. Before starting

- [ ] Read `PRE_REGISTRATION.md` end to end
- [ ] Confirm you understand the locked rubric and decision criteria
- [ ] Confirm you will NOT edit `PRE_REGISTRATION.md` after the first API call
- [ ] Have `ANTHROPIC_API_KEY` in env
- [ ] Verify current Anthropic pricing against placeholders in `run_eval.py` `PRICING` dict — update if stale

## 1. Capture prompts

Follow `PROMPT_CAPTURE_PROCEDURE.md` for each artifact class.

For each artifact:
```
prompts/{artifact_id}.txt            # the user prompt
prompts/{artifact_id}_system.txt     # system prompt if any
ground_truth/{artifact_id}.json      # actual outcome (for scoring, not for models)
```

Suggested artifact IDs:
- `fo_01_btc_win`
- `fo_02_eth_loss`
- `wr_01_{YYYYMMDD}` (weekly review)
- `wr_02_{YYYYMMDD}`
- `dr_01_{rec_id}` (only resolved director recommendations)
- `dr_02_{rec_id}` (optional, if resolved)
- `a2_01_{decision_id}` (observation-mode debate)
- `a2_02_{decision_id}`

If resolved director recs < 2, either drop to smaller set or add one reconstructed forensic `fr_01_*` case per the procedure.

**After all prompts captured:**
- [ ] Every file in `prompts/` has a matching entry in your intended artifact set
- [ ] No prompt contains post-artifact information
- [ ] Ground truth files exist for all `promotion_evidence=true` artifacts

## 2. Build the manifest

```bash
cd opus_eval
python3 run_eval.py --build-manifest
```

This scans `prompts/` and writes `manifest.json` with SHA-256 hashes of every prompt file plus the pre-registration hash.

- [ ] Open `manifest.json` and confirm artifact count matches intended set
- [ ] Confirm `pre_registration_sha256` is populated

## 3. Dry run

```bash
python3 run_eval.py --manifest manifest.json --dry-run
```

Expected output:
```
[OK] Pre-registration hash verified: ...
[OK] N artifacts validated
[DRY RUN] Exiting without API calls.
```

If anything fails, fix it before the real run.

## 4. Real run

```bash
python3 run_eval.py --manifest manifest.json
```

This fires the actual API calls. For each artifact:
- Random A/B label assignment (seeded from manifest, reproducible)
- Both models called with identical prompt
- Outputs written to `outputs/{artifact_id}__model_{A|B}.txt`
- Tokens, cost, latency logged to `call_log.jsonl`
- Model mapping sealed to `SEALED/model_mapping.json`

**After completion:**
- [ ] `outputs/` has 2 files per artifact
- [ ] `call_log.jsonl` exists
- [ ] `SEALED/model_mapping.json` exists
- [ ] DO NOT OPEN `SEALED/model_mapping.json` or `call_log.jsonl`

Optional extra protection: `zip -P <password> SEALED.zip SEALED/ && rm -rf SEALED/`, write password to a separate file.

## 5. Blind scoring

Copy the template:
```bash
cp scoring_sheet_template.csv scoring_sheet_filled.csv
```

For each row in the CSV:
1. Open `outputs/{artifact_id}__model_{label}.txt`
2. Open `ground_truth/{artifact_id}.json` (if exists)
3. Score all dimensions per `PRE_REGISTRATION.md` rubric
4. Save the CSV after each artifact (incremental progress)

**Scoring discipline:**
- Score one artifact completely before moving to the next
- Do NOT look at the other model's output for the same artifact until you've scored the current one
- Do NOT open call_log.jsonl or SEALED/
- Do NOT edit previous scores after seeing later outputs
- If you realize mid-scoring that your interpretation of a rubric dimension was wrong, note it but do NOT retroactively change earlier scores

Estimated time: ~15-20 min per artifact × ~8 artifacts = 2-3 hours. Do it in one sitting if possible.

## 6. Reveal and aggregate

```bash
python3 score_eval.py --filled scoring_sheet_filled.csv
```

This opens `SEALED/model_mapping.json` — that's the moment of reveal.

Output:
- Per-class wins/losses/ties on every dimension
- Novel failure mode flags with tie-break eligibility
- Cost totals and Opus/Sonnet multiplier on your actual prompts

## 7. Match against pre-registered decision criteria

Open `PRE_REGISTRATION.md` section "Pre-Registered Decision Criteria". Match results to Outcome A/B/C/D/E. Execute the pre-registered action.

**If results are genuinely ambiguous by the tie-break rules:** pre-registered action is "defer T1.8 and re-run in 4 weeks." Do not re-interpret results.

## 8. Write up

One-page decision memo:
- Outcome category (A/B/C/D/E)
- Per-class result table
- Novel failure mode evidence (if any)
- Cost multiplier on actual prompts
- Committed next action

Commit everything: PRE_REGISTRATION.md, manifest.json, outputs/, call_log.jsonl, SEALED/ (after reveal), scoring_sheet_filled.csv, decision memo. This becomes the evidence base for T1.8 acceptance criteria.

---

## Invariants (do not break)

- Pre-registration cannot be edited after first API call
- Artifact set cannot grow mid-experiment
- Blind scoring cannot be circumvented
- Pending recommendations cannot be added
- Ambiguous results trigger "defer and re-run," not re-interpretation
- Prose quality is never scored, even implicitly
