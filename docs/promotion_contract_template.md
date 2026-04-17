# Promotion Contract Template — Shadow → Production

**Status:** DRAFT | REVIEW | APPROVED | REJECTED

---

## Module Information

| Field | Value |
|-------|-------|
| Module name | `{module_name}.py` |
| Ring | shadow → prod |
| Feature flag | `{feature_flag_name}` |
| Created | YYYY-MM-DD |
| Reviewed by | — |
| Approved by | — |

---

## Purpose

*What does this module do? Why was it built in the shadow ring?*

{module_purpose}

---

## Promotion Criteria

All criteria must be ✅ before promotion from shadow to prod.

### Required

- [ ] Feature flag `{feature_flag_name}` tested with `true` on VPS for ≥ 7 consecutive days without incident
- [ ] No FAIL/CRITICAL entries in `data/analytics/incident_log.jsonl` linked to this module
- [ ] Module is importable without error (`python3 -m py_compile {module_name}.py`)
- [ ] All public functions have docstrings
- [ ] Cost attribution spine records present (`data/analytics/cost_attribution_spine.jsonl`)
- [ ] At least 1 test in `tests/test_core.py` covering the module's primary path

### Recommended

- [ ] Weekly review has referenced this module's output at least once
- [ ] No runtime exceptions logged at ERROR level in `logs/bot.log` for this module
- [ ] Memory footprint measured and documented below

---

## Observed Cost (shadow ring)

| Metric | Value |
|--------|-------|
| LLM calls (7d) | — |
| Total cost (7d) | — |
| Avg cost/call | — |
| Ring | shadow |
| Budget class | experimental |

---

## Risk Assessment

**What could go wrong if this module is promoted to prod?**

*Fill in before approval.*

---

## Rollback Plan

If promotion causes issues:
1. Set `{feature_flag_name}: false` in `strategy_config.json`
2. `systemctl restart trading-bot` on VPS
3. Verify module no longer called via `tail -f logs/bot.log`
4. If JSONL data is corrupt: restore from `*.backup_YYYYMMDD_HHMMSS`

---

## Approval Sign-off

| Step | Done | Date |
|------|------|------|
| Shadow-ring test ≥ 7 days | ⬜ | — |
| Cost review by CFO (Agent 8) | ⬜ | — |
| Technical review by CTO (Agent 5) | ⬜ | — |
| Flag changed to `feature_flags` section | ⬜ | — |
| CLAUDE.md source table updated | ⬜ | — |

---

*Contract version: 1. Template at `docs/promotion_contract_template.md`.*
