# Policy leakage findings

Policy logic that lives outside `risk_kernel.py`. This document is the authoritative
register of known leakage. Each item describes what was found, where, why it was left
in place, and what a future cleanup would look like.

`risk_kernel.py` is the declared sole authority for risk rules (see
`docs/policy_ownership_map.md`). Any check that duplicates or re-implements a
`risk_kernel` constant in another module is a leak.

---

## PLF-001 — PDT_FLOOR in debate_trade()

**File:** `bot_stage4_execution.py:debate_trade()` line ~49  
**Code:**
```python
if equity <= 26_000:  # PDT_FLOOR duplicated from risk_kernel — see docs/policy_leakage_findings.md
    return {"proceed": True}
```

**Leakage type:** Hard-coded constant reuse.

**What it does:** Skips the bull/bear/synthesis debate when equity is at or below the
PDT floor ($26,000). The rationale is that debate overhead is not worthwhile when the
account is in PDT-preservation mode. This is a reasonable optimisation gate, but it
reuses the exact PDT_FLOOR value rather than importing it from `risk_kernel`.

**Why it was left in place:** The bot.py split prompt explicitly required that no policy
logic be moved during the split. The value and the condition were preserved verbatim with
a comment pointing here.

**Correct owner:** `risk_kernel.PDT_FLOOR` (currently `26_000`, defined at module level).

**Cleanup approach (separate prompt):**
1. Export `PDT_FLOOR` as a public constant from `risk_kernel.py`.
2. Import it in `bot_stage4_execution.py`:
   ```python
   from risk_kernel import PDT_FLOOR as _PDT_FLOOR
   ```
3. Replace the literal:
   ```python
   if equity <= _PDT_FLOOR:
   ```
4. Add a test asserting that `debate_trade` returns `{"proceed": True}` for equity
   equal to `risk_kernel.PDT_FLOOR`, so a future change to the constant is caught.

---

## PLF-002 — Confidence + session gates in debate_trade()

**File:** `bot_stage4_execution.py:debate_trade()` lines ~42–47  
**Code:**
```python
if direction != "buy":
    return {"proceed": True}
if confidence not in ("medium", "high"):
    return {"proceed": True}
if session_tier != "market":
    return {"proceed": True}
```

**Leakage type:** Inline session / confidence policy.

**What it does:** Limits the debate to buy actions with medium-or-higher confidence
during the market session only. The confidence and session checks mirror constraints
that also appear in the signal scorer and risk kernel.

**Why it was left in place:** Same prompt-level freeze as PLF-001. The conditions are
preserved exactly.

**Correct owner:** These are debate-specific eligibility rules, not core risk policy.
The cleanest home is `bot_stage4_execution.py` itself, but the confidence threshold
(`"medium", "high"`) should reference a shared constant rather than a string literal.

**Cleanup approach (separate prompt):**
- Define `_DEBATE_MIN_CONFIDENCE = frozenset({"medium", "high"})` at module level so
  it is visible and testable.
- The session-tier gate may stay as-is (it is inherently local to the debate stage).

---

## PLF-003 — exposure_cap in order_executor.py (resolved)

**File:** `order_executor.py` — resolved in Session 1 (executor policy consolidation).  
**Status:** ✅ Hard rejection demoted to `log.warning`. `risk_kernel` is now sole
authority for position sizing and exposure cap enforcement.

---

## Summary table

| ID | File | Value | Risk kernel authority | Status |
|----|------|-------|-----------------------|--------|
| PLF-001 | `bot_stage4_execution.py` | `26_000` (PDT_FLOOR) | `risk_kernel.PDT_FLOOR` | open — comment added |
| PLF-002 | `bot_stage4_execution.py` | `"medium"`, `"high"` confidence gate | debate-local — needs constant | open |
| PLF-003 | `order_executor.py` | exposure_cap, position sizing | `risk_kernel` | resolved ✅ |
