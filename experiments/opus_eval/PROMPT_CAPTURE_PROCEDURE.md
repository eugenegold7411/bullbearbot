# Prompt Capture Procedure

**Purpose:** Reconstruct the exact prompts sent to the original model for each artifact, without contamination.

**Core rule:** If the prompt cannot be recovered from logs with high fidelity, the artifact is excluded. Do NOT reconstruct from memory or from "what the prompt probably was."

---

## Per-Class Capture Procedure

### Forensic Cases (closed A1 trades: BTC win, ETH loss)

The current system does NOT have T2.3 (post-trade forensic reviewer) built yet. So there is no historical forensic prompt to capture — you will CONSTRUCT the prompt fresh, using inputs that were available at the time of trade closure.

**Inputs to gather:**
1. Decision record from `memory/decisions.json` for the entry decision
2. Decision record for the exit decision
3. Intervening decisions between entry and exit (if any)
4. Signal scores at entry and exit (reconstruct from `data/market/signal_scores.json` backups or from decision record timestamps if embedded)
5. Regime classification at entry and exit
6. Macro backdrop at entry and exit (from `data/macro_intelligence/` archives)
7. Actual entry price, exit price, P&L
8. Any vector memory hits that were retrieved at entry

**Construction rule:** Build the prompt as if T2.3 existed on the day of closure. Use the SAME input set both models see. The prompt asks: "Given these inputs, produce a forensic record covering: thesis right/wrong, execution good/bad, what was missed, what pattern should be added to pattern_learning_watchlist."

**Contamination check:** The prompt must NOT contain any information from after the closure timestamp. If you accidentally include a post-closure weekly review insight, the case is invalidated.

### Weekly Review Synthesis Cases

These have real historical prompts. Capture them directly.

**Inputs to gather:**
1. From `data/reports/weekly_review_YYYY-MM-DD.md`: identify the two most recent reviews where Agent 6 (Strategy Director) made non-trivial parameter changes to `strategy_config.json`.
2. From `weekly_review.py` logs or re-running `_build_agent6_final_input()` against the same week's Agent 1-5 outputs: reconstruct the exact prompt sent to Agent 6 that produced the locked review.
3. If Agent 1-5 outputs are not preserved in full, the case is invalidated. Do not substitute reconstructed agent outputs.

**Construction rule:** Feed both models the captured Agent 6 prompt verbatim. Compare the synthesis memos and the proposed `strategy_config.json` changes.

**Contamination check:** Do not let the fact that you know what the director decided that week influence how you score either model's output. This is the hardest contamination to defend against and is the main reason blinding is non-negotiable here.

### Director Recommendation Cases (resolved verdicts only)

**Inputs to gather:**
1. From `data/reports/director_memo_history.json`: identify recommendations with `verdict` in {`verified`, `falsified`, `neutral`} — NOT `pending`.
2. For each resolved recommendation, reconstruct the Agent 6 prompt at the week it was issued (same procedure as weekly synthesis cases above).
3. Capture the actual verdict and the evidence that produced it.

**Construction rule:** Ask both models to produce recommendations given the week-N inputs. Do NOT tell them about the verdict. Score against the verdict after blind scoring is complete.

**Contamination check:** If a recommendation's resolution depended on post-hoc interpretation rather than concrete observable outcomes, flag it and consider excluding. A verdict of "neutral" because "the thesis was neither confirmed nor refuted" is weak ground truth.

### A2 Observation-Mode Debate Cases

**Inputs to gather:**
1. From `data/account2/trade_memory/decisions_account2.json`: pick two observation-mode entries where the four-way debate produced structured output.
2. Reconstruct the debate prompt from `bot_options.py` and the StructureProposal that was fed in.
3. Capture the observation-only outcome (no actual fill).

**Construction rule:** Feed both models the identical debate prompt. Compare the four-way synthesis output.

**Contamination check:** These cases have NO ground truth. They are judgment-only and flagged `promotion_evidence=false`. Do not let scoring ambition creep in.

**Note:** If A2 has fewer than 2 substantive observation-mode debates, drop one A2 case and add a reconstructed forensic case instead (see forensic section above).

---

## Capture Checklist Per Artifact

Before moving to the next artifact, confirm:

- [ ] Prompt captured verbatim or constructed from a documented procedure
- [ ] No post-artifact information contaminates the prompt
- [ ] Ground truth (if applicable) captured separately, NOT in prompt
- [ ] Artifact assigned a stable `artifact_id` matching the scoring sheet
- [ ] Prompt saved to `prompts/{artifact_id}.txt`
- [ ] Ground truth saved to `ground_truth/{artifact_id}.json`
- [ ] System prompt (if any) saved to `prompts/{artifact_id}_system.txt`
- [ ] Temperature and max_tokens captured
- [ ] SHA-256 of prompt file recorded in `manifest.json`

If any checkbox fails, the artifact is excluded. Do not work around checkbox failures.

---

## Handling Missing Data

**If an artifact cannot be captured honestly:**

1. Exclude it. Do not reconstruct "what the prompt probably was."
2. If total artifact count drops below 7, consider adding ONE reconstructed forensic case from pre-2026-04-13 artifacts, but ONLY if the inputs are genuinely recoverable from logs.
3. If total artifact count drops below 6, pause the experiment and either wait for more resolved data or accept that the current run has insufficient power.

**Do NOT:**

- Reconstruct prompts from memory
- Substitute "similar" prompts
- Pad with pending recommendations
- Use synthetic artifacts constructed for the test

---

## Storage Layout

```
opus_eval/
├── PRE_REGISTRATION.md           (locked)
├── scoring_sheet_template.csv    (locked)
├── prompts/
│   ├── fo_01_btc_win.txt         (prompt text)
│   ├── fo_01_btc_win_system.txt  (system prompt if any)
│   ├── fo_02_eth_loss.txt
│   ├── ...
├── ground_truth/
│   ├── fo_01_btc_win.json        (actual outcome, verdict, etc.)
│   ├── fo_02_eth_loss.json
│   ├── ...
├── manifest.json                  (SHA-256 hashes, artifact metadata)
├── outputs/                       (populated by run_eval.py)
│   ├── fo_01_btc_win__model_A.txt
│   ├── fo_01_btc_win__model_B.txt
│   ├── ...
├── call_log.jsonl                 (populated by run_eval.py — cost, latency, tokens)
├── model_mapping.json             (revealed AFTER scoring only)
└── scoring_sheet_filled.csv       (populated during scoring)
```

## Mapping File (sealed until after scoring)

After all API calls complete, `model_mapping.json` is written:

```json
{
  "fo_01_btc_win": {"A": "claude-sonnet-4-6", "B": "claude-opus-4-7"},
  "fo_02_eth_loss": {"A": "claude-opus-4-7", "B": "claude-sonnet-4-6"},
  ...
}
```

Randomized per artifact to prevent pattern recognition. This file MUST NOT be opened until all scoring is complete. Suggestion: zip it with a password you write down elsewhere, or move it outside the working directory until scoring is done.
