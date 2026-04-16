# BullBearBot Alpha Measurement Framework
# Version: v1.0.0
# Owner: production core owner + semantic platform owner + CFO review owner
# Last updated: 2026-04-16
# Status: LOCKED
# Depends on:
#   - taxonomy_v1.0.0.md
#   - trade_closure_contract_v1.0.0.md
#   - fill_price in canonical execution results (T0.1)
#   - cost attribution spine (T0.7)
#   - decision identity spine / stable decision_id

---

## Purpose

- Define what "alpha" means for BullBearBot
- Define which improvements count as alpha vs quality/reliability improvements
- Define baselines, metrics, minimum samples, and promotion thresholds
- Prevent elegant nonsense and false-positive "improvements" during v2 buildout

---

## 1. Versioning Policy

- Minor addition (new metric, new benchmark, new module measurement field): bump minor → v1.1.0
- Breaking change (metric definition change, benchmark change, promotion threshold change): bump major → v2.0.0
- Every computed alpha artifact must include schema_version
- Backward-compatible readers required for at least one prior version

---

## 2. Core Principle

BullBearBot may improve in many ways:
- better architecture
- better governance
- better observability
- better reliability
- lower cost
- better trade selection
- better trade management
- better portfolio outcomes

**These are NOT the same thing.**

### Locked Rule

A module may be valuable without generating alpha.

Therefore every module improvement must be classified as one of:

1. Alpha improvement
2. Quality improvement but not alpha
3. Cost improvement
4. Reliability / governance improvement
5. No demonstrated lift

No feature may be described as "alpha-generating" unless it meets the framework below.

---

## 3. Definitions

### 3.1 Alpha

Alpha means:

> Excess return or decision quality above a fair baseline, after costs and risk,
> in a way that is repeatable and attributable.

### 3.2 Signal Alpha

Did approved ideas outperform:
- rejected ideas
- near misses
- naive candidate baselines

over a defined forward-return window?

### 3.3 Management Alpha

Did actual exits / reductions / time management outperform simple baseline management rules?

### 3.4 Portfolio Alpha

Did the realized account outperform a matched benchmark:
- net of costs
- with risk considered
- over a sufficient sample?

### 3.5 Feature Lift

Did a module improve any of:
- signal alpha
- management alpha
- portfolio alpha
- cost-adjusted value

compared to the baseline system without that module?

### 3.6 Quality Improvement But Not Alpha

A module that improves:
- interpretability
- consistency
- labeling quality
- incident response
- governance memory
- reliability

without demonstrated return lift.

This is still valuable, but it must not be called alpha.

---

## 4. Measurement Layers

BullBearBot measures alpha at four layers.

### Layer 1 — Trade Selection Alpha

**Question:** Did the system choose better trades than the alternatives it saw?

Primary comparisons:
- approved vs rejected_by_risk_kernel
- approved vs rejected_by_policy
- approved vs below-threshold near_miss
- approved vs top-scored-but-not-taken candidate if available

Primary metrics:
- forward_return_1d
- forward_return_3d
- forward_return_5d
- hit rate (% positive forward return)
- average forward return
- median forward return
- tail loss rate
- score-weighted forward return

### Layer 2 — Trade Management Alpha

**Question:** Did the system manage trades better than naive exit logic?

Primary comparisons:
- realized PnL vs simple stop/target baseline
- realized PnL vs fixed-hold-period baseline
- realized PnL vs close-at-end-of-day baseline for intraday names
- realized PnL vs no-reduction baseline if reduce actions are used

Primary metrics:
- realized_pnl_usd
- realized_pnl_r
- exit efficiency
- average adverse excursion if available later
- average favorable excursion if available later
- holding-period-adjusted return

### Layer 3 — Portfolio Alpha

**Question:** Did the actual account outperform a fair benchmark after costs and risk?

Primary metrics:
- cumulative return
- excess return vs benchmark
- Sharpe / simplified risk-adjusted return if enough data
- max drawdown
- win rate
- expectancy
- turnover-adjusted return
- cost-adjusted return

### Layer 4 — Feature Alpha

**Question:** Did a module improve Layer 1, 2, or 3 enough to justify itself?

Primary metrics:
- delta in forward returns
- delta in realized outcomes
- delta in selection quality
- delta in cost per useful decision
- delta in incident rate or review quality if module is non-alpha class

---

## 5. Baseline Hierarchy

Every comparison must specify a baseline.

### 5.1 A1 Portfolio Baseline

Default benchmark:
- primary: SPY
- secondary: QQQ if A1 is tech-heavy during the measured window

Tiebreaker rule:
- If A1 sector exposure during the measurement window is greater than 40%
  concentrated in a single sector, use a blended benchmark anchored by the
  corresponding sector ETF for that window. Otherwise default to SPY.
- Benchmark selection must be declared at the start of the measurement window
  and cannot be changed retroactively.

### 5.2 A1 Signal Baseline

Default baselines:
- top-N scored candidates without model selection
- naive momentum / catalyst continuation heuristic
- rejected / near-miss set from the same cycle

### 5.3 A1 Management Baseline

Default baselines:
- simple fixed stop / fixed target
- fixed holding-period exit
- no-reduction baseline for trades where reduction occurred

### 5.4 A2 Portfolio Baseline

Default benchmarks:
- simple defined-risk options baseline on same underlyings where available
- stock-equivalent directional baseline for directional options trades
- "no options, stock-only expression" comparison where feasible

### 5.5 A2 Expression Baseline

**Question:** Did the chosen structure improve the outcome versus a simpler expression?

Examples:
- call debit spread vs single call
- put debit spread vs short stock equivalent proxy
- credit spread vs no-trade in high-IV environment
- volatility_expression vs directional stock position

### 5.6 Feature Baseline

Every feature must define one of:
- control period before feature enabled
- shadow output vs prod output
- module-on vs module-off comparison
- compact vs full prompt comparison
- advisory output ignored vs used

---

## 6. Minimum Sample Rules

No alpha claim is valid below minimum sample.

### 6.1 Trade Selection Alpha

- Hard minimum for directional interpretation: 20 approved trades
- Strong confidence threshold: 50+ approved trades
- Approved vs rejected / near-miss comparisons require:
  - at least 20 approved
  - at least 20 comparison candidates in the same window

### 6.2 Trade Management Alpha

- Hard minimum: 15 fully closed trades
- Strong confidence threshold: 40+ fully closed trades

### 6.3 Portfolio Alpha

Portfolio alpha requires BOTH:
- at least 8 trading weeks, AND
- at least 30 fully closed trades

If these conditions diverge, use the higher bar.
Until both are satisfied: classify as insufficient_sample.

### 6.4 A2-Specific

No strong alpha claim before:
- observation_complete = true
- at least 10 fully closed A2 structures
- Stronger threshold: 25+ fully closed A2 structures

### 6.5 Feature Alpha

No module promotion into prod on alpha grounds without:
- minimum 20 relevant events for that module, OR
- one full review window plus enough events to support interpretation

Below these thresholds:
- module may be classified as "promising"
- NEVER "alpha-generating"

---

## 7. Cost-Adjusted Measurement

All alpha claims must be evaluated with cost context.

### Required Cost Fields Per LLM-Influenced Feature

From T0.7 cost attribution spine:
- module_name
- layer_name
- ring
- model
- estimated_cost_usd
- linked subject ID/type

### Required Derived Cost Metrics

- cost per decision
- cost per submitted trade
- cost per closed trade
- cost per profitable trade
- cost per resolved recommendation
- cost per useful insight if classified manually later

### Locked Rule

A module that improves outcomes slightly but increases cost disproportionately
may fail promotion.

---

## 8. Promotion Contract for Shadow/Advisory Modules

Before any shadow module can move toward production influence, it must define:

| Field | Description |
|-------|-------------|
| metric | What is being measured |
| baseline | What it is compared against |
| measurement_window | How long the evaluation runs |
| minimum_sample | Minimum events before judgment |
| pass_threshold | What constitutes success |
| kill_threshold | What triggers removal |
| classification_target | alpha / quality / cost / reliability |

### Example: Prompt Compressor

- metric: schema-valid decision rate + cost per decision + selection quality
- baseline: raw section prompt path
- window: 2 weekly review windows
- minimum sample: 30 cycles
- pass threshold: no drop in schema-valid decision rate AND ≥15% lower cost
  per decision OR measurable improvement in Layer 1 metrics
- kill threshold: any sustained degradation in decision validity or clear
  decline in trade-selection quality
- classification_target: cost improvement

---

## 9. Alpha Classification Outputs

Every weekly review and major module evaluation must assign one of:

| Classification | Meaning |
|---------------|---------|
| `alpha_positive` | Evidence suggests module improved selection, management, or portfolio outcomes vs baseline |
| `alpha_neutral` | No meaningful difference vs baseline |
| `alpha_negative` | Evidence suggests module degraded outcomes |
| `quality_positive_non_alpha` | Improved governance/reliability/interpretability without demonstrated return lift |
| `cost_positive_non_alpha` | Reduced cost without demonstrated return lift |
| `reliability_positive_non_alpha` | Improved operational robustness without demonstrated return lift |
| `insufficient_sample` | Not enough data for judgment |

These labels must be used instead of vague language like:
- "seems useful"
- "probably helping"
- "smart"
- "better intuition"

---

## 10. Required Artifacts

### 10.1 decision_outcomes.py

Must become the canonical analysis spine for:
- approved trades
- rejected trades
- near misses
- prompt mode
- module tags
- realized outcomes
- forward-return comparisons

### 10.2 Weekly Review

Must consume:
- trade selection alpha summaries
- management alpha summaries
- portfolio alpha summaries
- feature lift summaries
- recommendation verdict summary
- cost-adjusted module summaries
- insufficient-sample flags

### 10.3 Recommendation Memory

Must track:
- recommendation ID
- expected target metric
- later observed effect
- verdict
- confidence

### 10.4 Cost Attribution Ledger

Must support:
- module cost aggregation
- ring/layer cost aggregation
- feature ROI summaries

---

## 11. Fields Required in Alpha Artifacts

All alpha measurement artifacts must include:

**Required:**
- schema_version
- measurement_window_start
- measurement_window_end
- subject_scope (A1 / A2 / module / recommendation / portfolio)
- baseline_name
- sample_size
- confidence_level
- classification
- notes

**Optional:**
- benchmark_symbol
- cost_adjusted (boolean)
- linked_artifact_ids

---

## 12. What Does NOT Count as Alpha

The following are explicitly excluded unless tied to measured baseline improvement:

- better narrative quality
- better summaries
- cleaner weekly review prose
- more interesting board meeting output
- more confident explanations
- more sophisticated ontologies
- more agent debate
- more lab activity
- more module complexity

These may still count as:
- quality improvements
- reliability improvements
- governance improvements

**But not alpha.**

---

## 13. A1-Specific Alpha Interpretation

A1 trade-selection alpha is strongest when:
- approved trades outperform rejected and near-miss candidates from the same
  regime window
- outcomes remain positive after costs
- performance is not explained by broad market beta alone

A1 portfolio alpha is strongest when:
- A1 outperforms SPY / appropriate blended benchmark
- drawdown is controlled
- gains are not concentrated in one symbol/theme
- live performance is directionally consistent with paper expectations

---

## 14. A2-Specific Alpha Interpretation

A2 alpha must answer two separate questions:
1. Was the directional thesis good?
2. Was the options expression better than a simpler alternative?

Therefore A2 should separately measure:
- thesis accuracy
- structure choice quality
- IV-environment alignment
- realized result vs stock-equivalent baseline
- realized result vs simpler options baseline where feasible

### Locked Rule

No strong A2 alpha claim before:
- observation complete
- sufficient closed structures
- enough structure-comparison data exists

---

## 15. Annex / Lab Rule

Every lab module must declare its intended evaluation class up front:
- alpha improvement
- quality improvement but not alpha
- cost improvement
- reliability/governance improvement
- exploratory / no alpha claim

No lab module may be discussed as "working" without a framework
classification or insufficient_sample.

No annex module is exempt from this framework.

---

## 16. Dependency Map

| Ticket | Blocked by |
|--------|-----------|
| T2.3 Post-trade forensic reviewer | T0.8 + T0.1 + this framework |
| T2.4 Recommendation outcome resolver | this framework |
| T2.5 Memory anti-pattern miner | this framework + T2.3 |
| T3.2 Compact/full ROI table | T0.7 + this framework |
| T3.3 Module ROI with actual outcomes | T0.7 + this framework + T2.3 |
| T3.4 Shadow → prod promotion contracts | this framework |
| T6.x Mad science annex modules | this framework (anti-elegant-nonsense gate) |
| Any shadow module promotion | this framework |

---

## 17. Implementation Notes for Claude Code

### Non-Goals

- Do not invent new metrics beyond this document without version bump
- Do not treat "quality improvement" as alpha by default
- Do not auto-promote modules based on prose summaries
- Do not compute alpha from performance.json alone

### Minimal Code-Facing Additions

**Enum / constants:**
```python
ALPHA_CLASSIFICATIONS = [
    "alpha_positive",
    "alpha_neutral",
    "alpha_negative",
    "quality_positive_non_alpha",
    "cost_positive_non_alpha",
    "reliability_positive_non_alpha",
    "insufficient_sample",
]
```

**Promotion contract shape:**
```json
{
  "module_name": "...",
  "classification_target": "alpha improvement",
  "metric": "...",
  "baseline": "...",
  "measurement_window": "...",
  "minimum_sample": 0,
  "pass_threshold": "...",
  "kill_threshold": "..."
}
```

**Alpha summary shape:**
```json
{
  "schema_version": "1.0.0",
  "measurement_window_start": "...",
  "measurement_window_end": "...",
  "subject_scope": "A1",
  "baseline_name": "SPY",
  "sample_size": 0,
  "confidence_level": "low",
  "classification": "insufficient_sample",
  "cost_adjusted": true,
  "notes": "..."
}
```

---

## 18. Locked Design Opinions

- Alpha must be measured against a baseline, never in isolation
- Cost-adjusted lift matters
- Not every useful feature is alpha-generating
- Shadow/advisory modules need numeric promotion contracts
- A2 alpha must separate thesis quality from expression quality
- `insufficient_sample` is a first-class output, not a failure
- Benchmark selection must be declared before the measurement window opens,
  not after results are known

---

*Last updated: 2026-04-16*
*Status: LOCKED — changes require version bump and schema owner approval*
