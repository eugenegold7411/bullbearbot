# Promotion Contract — replay_debugger (Shadow → Production)

**Status:** DRAFT

---

## Module Information

| Field | Value |
|-------|-------|
| Module name | `replay_debugger.py` |
| Ring | shadow → prod |
| Feature flag | `enable_replay_fork_debugger` |
| Created | 2026-04-16 |
| Reviewed by | — |
| Approved by | — |

---

## Purpose

`replay_debugger.py` is a shadow replay fork engine. It allows forking past A1 decisions and
weekly review agents (Agent 6 — Strategy Director) with alternate model tiers, prompt versions,
or module overrides — without touching production state.

Core use cases:
- Compare Haiku vs Sonnet decision quality on the same market context
- Replay last week's strategy review with an updated prompt version
- Validate that a prompt change improves synthesis quality before deploying

All LLM calls are attributed with `layer_name="shadow_analysis"`, `ring="shadow"`, `purpose="replay_fork"`.
Results are logged to `data/analytics/replay_log.jsonl`.

---

## Promotion Criteria

### Required

- [ ] Feature flag `enable_replay_fork_debugger` tested with `true` on VPS for ≥ 7 consecutive days
- [ ] No FAIL/CRITICAL entries in `data/analytics/incident_log.jsonl` linked to `replay_debugger`
- [ ] Module is importable without error (`python3 -m py_compile replay_debugger.py`)
- [ ] `replay_log.jsonl` entries present with correct `layer_name`, `ring`, `purpose` fields
- [ ] At least 2 tests in `tests/test_core.py` covering replay and result provenance

### Recommended

- [ ] `format_diff()` output has been reviewed for quality on 5+ real replays
- [ ] ReplayResult `diff_summary` is useful for identifying model-tier differences
- [ ] Weekly CTO review shows replay cost is < $0.01/day when flag is true

---

## Observed Cost (shadow ring)

| Metric | Value |
|--------|-------|
| LLM calls (7d) | not yet measured |
| Total cost (7d) | not yet measured |
| Avg cost/call | ~$0.0002 (Haiku, 1000 input + 500 output tokens per replay) |
| Ring | shadow |
| Budget class | experimental |

---

## Risk Assessment

**Potential risks on promotion to prod:**

1. **No prod mutation by design**: All outputs are shadow-only. Replay results are logged to
   `replay_log.jsonl` but never affect `decisions.json`, `structures.json`, or any live state.
   Risk: very low.

2. **Cost leak**: If triggered too frequently (e.g., every cycle), cost accumulates.
   Mitigation: Feature flag default is `false`. When enabled, callers should rate-limit
   (e.g., at most 1 replay per hour per target_type).

3. **Stale context**: Replays use historical decision context; if the production prompt has
   changed significantly, fork comparisons may not be apples-to-apples.
   Mitigation: Document `prompt_version` in ForkConfig.

---

## Rollback Plan

1. Set `enable_replay_fork_debugger: false` in `strategy_config.json`
2. `systemctl restart trading-bot` on VPS
3. Verify no `[REPLAY]` log lines in `logs/bot.log`
4. `replay_log.jsonl` can be safely archived or deleted (analytics only, no prod deps)

---

## Approval Sign-off

| Step | Done | Date |
|------|------|------|
| Shadow-ring test ≥ 7 days | ⬜ | — |
| Cost review by CFO (Agent 8) | ⬜ | — |
| Technical review by CTO (Agent 5) | ⬜ | — |
| Flag moved to `feature_flags` section | ⬜ | — |
| CLAUDE.md source table updated | ⬜ | — |

---

*Contract version: 1. Template at `docs/promotion_contract_template.md`.*
