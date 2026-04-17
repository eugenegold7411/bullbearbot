# Promotion Contract — semantic_router (Shadow → Production)

**Status:** DRAFT

---

## Module Information

| Field | Value |
|-------|-------|
| Module name | `semantic_router.py` |
| Ring | shadow → prod |
| Feature flag | `enable_semantic_router_shadow` |
| Created | 2026-04-16 |
| Reviewed by | — |
| Approved by | — |

---

## Purpose

`semantic_router.py` is a shadow-ring deterministic cycle router. It evaluates the same
market context inputs as `sonnet_gate.py` but applies a fixed rule set (no LLM calls) to
recommend FULL vs COMPACT prompt mode for each cycle.

Core use cases:
- Validate whether sonnet_gate's LLM-based routing matches deterministic rules
- Identify systematic divergences (e.g., gate fires FULL when router says COMPACT)
- Provide a cost-zero baseline for routing quality measurement
- Feed weekly CTO review with divergence rate and mode distribution

**Critical constraint:** `sonnet_gate.py` is the live production routing authority. This
module MUST NOT be imported by `sonnet_gate.py`. All outputs are shadow-only.

Cost attribution: zero per cycle (no LLM calls). Spine records logged with
`layer_name="semantic_router"`, `ring="shadow"`, `purpose="routing_decision"`, `cost_usd=0.0`.
Routing decisions logged to `data/analytics/router_decisions.jsonl`.

---

## Promotion Criteria

### Required

- [ ] Feature flag `enable_semantic_router_shadow` tested with `true` on VPS for ≥ 7 consecutive days
- [ ] No FAIL/CRITICAL entries in `data/analytics/incident_log.jsonl` linked to `semantic_router`
- [ ] Module is importable without error (`python3 -m py_compile semantic_router.py`)
- [ ] At least 200 routing decisions logged in `data/analytics/router_decisions.jsonl`
- [ ] Divergence rate < 10% (router vs gate) measured over ≥ 200 decisions
- [ ] No degradation in Stage 3 decision quality compared to pre-router baseline
- [ ] At least 3 tests in `tests/test_core.py` covering routing rules and divergence detection
- [ ] `sonnet_gate.py` unchanged (verify via `git diff sonnet_gate.py` — must be empty)

### Recommended

- [ ] FULL cycle cost vs COMPACT cycle cost differential documented
- [ ] Weekly CTO review shows router cost-per-cycle ≤ $0.0001 (zero LLM cost)
- [ ] `format_router_summary_for_review()` output reviewed by CTO in ≥ 2 weekly reviews
- [ ] Router rules calibrated against 20+ real divergence events

---

## Alpha Classification

| Dimension | Classification |
|-----------|---------------|
| Primary | `cost_improvement` — correct COMPACT routing avoids full Sonnet call |
| Secondary | `quality_positive_non_alpha` — routing quality measurement (no direct P&L impact) |

**Minimum sample for promotion:** 200 routing decisions.

**Pass threshold:** divergence rate < 10% AND no decision quality degradation AND
lower average cost-per-cycle than sonnet_gate baseline.

---

## Observed Cost (shadow ring)

| Metric | Value |
|--------|-------|
| LLM calls (7d) | 0 (deterministic, no LLM) |
| Total cost (7d) | $0.00 |
| Avg cost/call | $0.0000 |
| Ring | shadow |
| Budget class | negligible |

---

## Risk Assessment

**Potential risks on promotion to prod:**

1. **sonnet_gate.py authority conflict**: Router must NEVER override the gate — it can only
   log divergences and inform the weekly review. Prod promotion means gate CALLS router to get
   a recommendation, but gate retains final authority. Risk: low if integration is additive only.

2. **Rule miscalibration**: Deterministic rules may be too aggressive (always FULL) or too
   conservative (always COMPACT). 200-decision calibration window mitigates this.
   Mitigation: require < 10% divergence at shadow baseline before promotion.

3. **Zero cost but non-zero latency**: Adding a Python function call + JSONL write per cycle
   adds negligible latency (~1ms). Acceptable.

---

## Rollback Plan

1. Set `enable_semantic_router_shadow: false` in `strategy_config.json`
2. `systemctl restart trading-bot` on VPS
3. Verify no `[ROUTER]` log lines in `logs/bot.log`
4. `router_decisions.jsonl` can be safely archived (analytics only, no prod deps)

---

## Approval Sign-off

| Step | Done | Date |
|------|------|------|
| Shadow-ring test ≥ 7 days | ⬜ | — |
| 200 routing decisions logged | ⬜ | — |
| Divergence rate < 10% confirmed | ⬜ | — |
| Cost review by CFO (Agent 8) | ⬜ | — |
| Technical review by CTO (Agent 5) | ⬜ | — |
| Flag moved to `feature_flags` section | ⬜ | — |
| CLAUDE.md source table updated | ⬜ | — |

---

*Contract version: 1. Template at `docs/promotion_contract_template.md`.*
