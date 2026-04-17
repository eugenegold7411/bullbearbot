# Model Tiering Policy v1.0.0

> **LOCKED** — changes require a new version file and CLAUDE.md update.
> Governs model selection across all LLM-calling modules.
> Canonical implementation: `model_tiering.py`.

---

## 1. Purpose and Scope

This policy defines which AI model tier each module is permitted to use, the conditions
under which escalation to a higher tier is allowed, and the governance process for changing
tier assignments.

Scope: all modules in `model_tiering.TIER_DECLARATIONS`. Any module making Claude API calls
that is not declared is considered undeclared and will log a WARNING on every call.

---

## 2. Tier Definitions

| Tier | Model | Use Case |
|------|-------|---------|
| CHEAP | `claude-haiku-4-5-20251001` | Classification, scoring, labeling, formatting — no deep reasoning required |
| DEFAULT | `claude-sonnet-4-6` | Main decision-making, synthesis, multi-step reasoning |
| PREMIUM | `claude-opus-4-7` | Crisis-level decisions only, triggered by escalation predicates |

**Default rule:** if a module is not declared, it defaults to DEFAULT tier and logs a WARNING.

---

## 3. Module Declarations

All module declarations live in `model_tiering.TIER_DECLARATIONS` (typed `ModuleTierDeclaration`
objects) and the backward-compatible `MODULE_TIER_DECLARATIONS` (plain dicts).

**Rings:**
- `prod` — runs in production pipeline
- `shadow` — shadow/counterfactual only, never affects live orders
- `annex` — Mad Science Annex lab modules, write to `data/annex/` only

**Budget classes:**
- `negligible` — < $0.01/day
- `low` — $0.01–$0.10/day
- `medium` — $0.10–$1.00/day
- `experimental` — cost unknown, under measurement

Run `python3 scripts/audit_model_tier_declarations.py` to see current declarations and
flag any undeclared callers.

---

## 4. Escalation Policy

Escalation from DEFAULT → PREMIUM is permitted only for modules with `escalation_allowed=True`.
Currently only `main_decision` meets this criterion.

**Escalation triggers** (any one fires → escalate):

| Trigger | Condition |
|---------|-----------|
| Ambiguous signal environment | `signals_conflict=True` AND all top-3 scores within 10 points |
| Multiple competing catalysts | `catalyst_count >= 3` AND `signals_conflict=True` |
| Many positions in defensive regime | `open_position_count >= 5` AND `regime_score < 30` |
| Forced decision under ambiguity | `deadline_approaching=True` AND `signals_conflict=True` |
| Crisis regime | `vix_level > 35` |
| Breaking news with conflict | `has_breaking_news=True` AND `signals_conflict=True` |
| Explicit request | `explicit_escalation_requested=True` |

**Cache invalidation warning:** each escalation causes a cache miss on the new model,
paying full cache-write cost. Monitor `cache_hit_input_tokens` in the spine summary weekly.

**Prohibited escalation paths:**
- CHEAP → PREMIUM (never, must go through DEFAULT)
- Any annex module → PREMIUM without eval contract on file
- shadow ring modules → any escalation

---

## 5. Annex Module Rules

Annex modules (ring="annex") are subject to additional restrictions beyond the standard policy:

1. **CHEAP only by default.** All annex modules are declared CHEAP with `allowed_tiers=(CHEAP,)`.
2. **No premium without eval contract.** `ANNEX_PREMIUM_REQUIRES_EVAL_CONTRACT = True` is enforced
   in `model_tiering.py`. Calling `annex_may_use_premium(module_name, eval_contract_on_file=False)`
   always returns False.
3. **Ring enforcement.** Annex modules must declare `ring="annex"`. If an annex module
   attempts to log a spine record with `ring="prod"`, it violates the annex sandbox contract.
4. **No prod pipeline imports.** Annex modules must not be imported by `bot.py`, `scheduler.py`,
   or any prod-ring module. The annex sandbox contract is enforced by convention (not at runtime).

---

## 6. Governance and Change Process

**To change a module's tier declaration:**
1. Update `TIER_DECLARATIONS` in `model_tiering.py`
2. Update `MODULE_TIER_DECLARATIONS` (backward-compat dict) to match
3. Run `python3 scripts/audit_model_tier_declarations.py` — must exit 0
4. Run `python3 -m py_compile model_tiering.py`
5. Update CLAUDE.md with the change
6. Deploy to VPS

**To promote an annex module to DEFAULT tier:**
1. A numeric evaluation contract must be on file in `data/promotion_contracts/`
2. The contract must include: baseline_metric, threshold, measurement_window, current_status
3. Change ring from "annex" to "prod" in the declaration
4. Change `allowed_tiers` to include DEFAULT
5. Follow the standard change process above

**To add a new module:**
1. Add a `_make_decl()` entry to `TIER_DECLARATIONS`
2. Add the corresponding dict entry to `MODULE_TIER_DECLARATIONS`
3. Set conservative defaults: CHEAP tier, `escalation_allowed=False`
4. Follow the standard change process above

---

## 7. Audit and Compliance

**Weekly review:** Agent 5 (CTO) receives `format_tier_summary_for_review()` output in every
weekly review. The CTO reviews for undeclared modules and cost anomalies.

**Audit script:**
```bash
python3 scripts/audit_model_tier_declarations.py
```

Output: undeclared callers, ring mismatches, escalation-eligible modules, cost by ring.
Exit 0 = clean. Exit 1 = undeclared modules found (must resolve before next deploy).

**Spine monitoring:** Every spine record has a `module_name` field. Run
`python3 scripts/report_cost_spine_unknowns.py` to find callers that resolved to "unknown"
(indicates a module is calling the API without being declared in CALLER_MODULE_MAP).
