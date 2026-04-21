# Shadow Systems Registry

**Last updated:** 2026-04-20  
**Status tracker:** `data/reports/shadow_status_latest.json`

This document lists all shadow systems currently running in BullBearBot.
A *shadow system* produces analytics, logs artifacts, or makes recommendations
but has **zero execution side effects** — it never submits orders, never calls
`execute_all()`, and never modifies live account state.

Shadow systems are promoted to live only after a defined promotion criteria
has been met (see each system below).

---

## Shadow Systems

### 1. Shadow Lane (`shadow_lane.py`)

| Field | Value |
|-------|-------|
| **Name** | Shadow Lane |
| **Purpose** | Logs counterfactual decisions — trades the kernel rejected, signals that were close to firing but didn't, timing misses. Enables weekly near-miss analysis. |
| **Owner module** | `shadow_lane.py` |
| **Feature flag** | `strategy_config.json shadow_lane.enabled` |
| **Enabled** | `true` |
| **Output artifact** | `data/analytics/near_miss_log.jsonl` |
| **Current status** | Active (wired in `bot.py`) |
| **Last run** | Every cycle — wired to `rejected_by_risk_kernel` and `approved_trade` events |
| **Promotion criteria** | N/A — informational only. Shadow lane is not promoted; it runs indefinitely. |
| **Notes** | 7 event types: `approved_trade`, `rejected_by_risk_kernel`, `rejected_by_policy`, `below_threshold_near_miss`, `interesting_but_not_actioned`, `timing_miss`, `structure_rejected`. Injected into Agent 1 weekly review. |

---

### 2. Portfolio Allocator Shadow (`portfolio_allocator.py`)

| Field | Value |
|-------|-------|
| **Name** | Portfolio Allocator Shadow |
| **Purpose** | Ranks held positions vs top candidates each cycle. Produces HOLD/TRIM/ADD/REPLACE recommendations. Advisory only — never submits orders. Provides a compact summary for Stage 3 prompt context. |
| **Owner module** | `portfolio_allocator.py` |
| **Feature flag** | `strategy_config.json portfolio_allocator.enable_shadow` |
| **Enabled** | `true` |
| **Output artifact** | `data/analytics/portfolio_allocator_shadow.jsonl` (rotated at 10,000 lines) |
| **Current status** | Active (wired in `bot_stage0_precycle.py`) |
| **Last run** | Updated in `data/reports/shadow_status_latest.json` after each cycle |
| **Promotion criteria** | Phase 1 promotion (trim-only live execution): Requires (1) ≥14 consecutive shadow cycles with stable artifacts, (2) weekly review confirms recommendation quality, (3) TRIM logic explicitly validated, (4) `enable_live` flag wired to actual execution path. See `portfolio_allocator.py` for `enable_live` guard. |
| **Notes** | Decision rules: TRIM when thesis_score ≤ 4; ADD when thesis_score ≥ 7 + room to grow; REPLACE when candidate signal_score − weakest_incumbent_normalized ≥ replace_score_gap (default 15). Anti-churn: same-sector block, time-bound exit block, daily cooldown, notional floor ($500). REALLOCATE semantics from risk_kernel.py remain advisory only. |

---

### 3. Context Compressor Shadow (`context_compiler.py`)

| Field | Value |
|-------|-------|
| **Name** | Context Compressor Shadow |
| **Purpose** | Tests Claude Haiku prompt compression on individual prompt sections. Logs compression ratios and quality to cost attribution spine. |
| **Owner module** | `context_compiler.py` |
| **Feature flag** | `strategy_config.json shadow_flags.enable_context_compressor_shadow` |
| **Enabled** | `false` |
| **Output artifact** | `data/analytics/cost_attribution_spine.jsonl` (ring=shadow) |
| **Current status** | Disabled (feature flag off) |
| **Last run** | N/A (disabled) |
| **Promotion criteria** | Enable when testing prompt cost reduction. Requires ≥50 compression samples across a variety of market conditions. Compare quality scores before enabling in production prompt path. |
| **Notes** | Header comment: `# SHADOW MODULE — do not import from prod pipeline`. Gated by `enable_context_compressor_shadow`. |

---

### 4. Replay Fork Debugger

| Field | Value |
|-------|-------|
| **Name** | Replay Fork Debugger |
| **Purpose** | Replays captured decision artifacts to fork and compare prompt variants without live cycles. |
| **Owner module** | `data/captures/*.json` + future `replay_harness.py` |
| **Feature flag** | `strategy_config.json shadow_flags.enable_replay_fork_debugger` |
| **Enabled** | `false` |
| **Output artifact** | TBD |
| **Current status** | Disabled (not yet implemented) |
| **Promotion criteria** | N/A — tooling only, not promoted to live execution. |
| **Notes** | Capture artifacts are written by `bot_stage3_decision._write_decision_capture()` every cycle. The harness reads them for offline replay. |

---

## Promotion Checklist Template

When promoting a shadow system to live execution, verify:

- [ ] ≥14 consecutive shadow cycles with valid artifacts (no write failures)
- [ ] Weekly review (Agent 1 or relevant agent) has reviewed shadow output for quality
- [ ] Anti-churn rules confirmed working correctly via test replay
- [ ] `enable_live` flag wired to actual execution path in owning module
- [ ] `validate_config.py` gate added for the live flag
- [ ] `docs/rollback_playbook.md` updated with rollback procedure
- [ ] CLAUDE.md updated with new live system status

---

## Status JSON Schema

`data/reports/shadow_status_latest.json` is updated by each shadow system after every cycle:

```json
{
  "shadow_systems": {
    "portfolio_allocator": {
      "last_run_at": "2026-04-20T12:00:00Z",
      "status": "active"
    }
  },
  "updated_at": "2026-04-20T12:00:00Z"
}
```
