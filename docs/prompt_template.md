# Prompt Template — BullBearBot

Every Claude Code prompt for this project must follow this structure.
Copy this template. Fill in the blanks. Do not skip any section.

---

## [PROMPT NAME]

**Risk level:** [Low / Medium / High]
**Run:** [Alone / Parallel with: X]
**Touches:** [list files that will change]

---

### Context
[What is broken or missing. Include evidence — log lines, file output, error messages.
Never write "I think" or "probably" — only what you know from evidence.]

### Task
[Exact description of what to build or fix. Be specific.]

### Hard constraints
[What must not change. What must not be touched.]

### Steps
[Ordered steps. Each step that modifies code must be followed by a test or verification.]

### Tests
[Exact tests to add or update. Unit tests are not enough — include server-side verification.]

---

### VERIFICATION (mandatory — do not skip)

This section must be completed before the task is considered done.
Run each command, paste the output, and confirm it matches expected.

**Step 1 — Local verification:**
```bash
[exact command]
```
Expected: [what you expect to see]
Actual: [paste output here]
Pass/Fail: [ ]

**Step 2 — Server verification:**
```bash
ssh tradingbot '[exact command]'
```
Expected: [what you expect to see]
Actual: [paste output here]
Pass/Fail: [ ]

**Step 3 — Feature audit:**
```bash
ssh tradingbot 'cd /home/trading-bot && .venv/bin/python3 scripts/feature_audit.py 2>/dev/null | grep "[feature name]"'
```
Expected: ✅ OK
Actual: [paste output here]
Pass/Fail: [ ]

---

### Acceptance criteria
The task is NOT complete until all verification steps show Pass.
If any step fails, fix and re-verify. Do not move on.
