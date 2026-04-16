# BullBearBot Semantic Taxonomy
# Version: v1.0.0
# Owner: semantic platform owner
# Last updated: 2026-04-16
# Status: LOCKED — changes require version bump and schema owner approval
# Depends on: nothing (this is the root governance document)

---

## Versioning Policy

- Minor addition (new label): bump minor → v1.1.0
- Breaking change (label rename, removal, merge): bump major → v2.0.0
- Every label must have: definition, 3 positive examples, 2 boundary examples,
  1 "not this" example before addition
- Labels with zero usage for 90 days are REVIEWED for retirement, not
  automatically retired
- Rare-but-important labels (crisis, geopolitical, corporate_action,
  congressional_buy) are exempt from automatic review triggers
- One schema owner at all times

---

## Anti-Sprawl Rules (Global)

- Every dimension allows `unknown` — never suppress it
- No free-text category invention in downstream modules
- If a label does not exist, use `unknown` and flag for taxonomy review
- Categories within a dimension are mutually exclusive unless explicitly noted
- New labels require schema owner approval and version bump
- Hard caps enforced per dimension (defined below)

---

## DIMENSION 1 — catalyst_type

**Definition:** What triggered the bot to consider this trade?

**Rules:**
- One primary catalyst per trade
- Secondary catalysts stored as array, may be empty
- `momentum_continuation` is explicitly lower-trust than named-event catalysts
- `technical_breakout` requires: pre-defined level, confirmed volume/participation
- Hard cap: 20 active labels

| Label | Definition |
|-------|-----------|
| `earnings_beat` | Company reported results materially above consensus |
| `earnings_miss` | Company reported results materially below consensus |
| `guidance_raise` | Forward guidance increased by management |
| `guidance_cut` | Forward guidance reduced by management |
| `macro_print` | Economic data release moved markets (PPI, CPI, NFP, etc.) |
| `fed_signal` | Fed statement, speech, meeting minutes, or rate decision |
| `geopolitical` | War, sanctions, diplomatic event, military action |
| `policy_change` | Government or regulatory action (tariffs, executive orders, rule changes) |
| `insider_buy` | Form 4 C-suite purchase on open market |
| `congressional_buy` | Congressional member purchase disclosure |
| `analyst_revision` | Major analyst upgrade, downgrade, target revision, or initiation |
| `corporate_action` | M&A, buyback, spinoff, major contract win/loss, legal settlement |
| `technical_breakout` | Break of pre-defined level with sufficient volume/participation |
| `momentum_continuation` | Continuation setup with no new named event (lower-trust) |
| `mean_reversion` | Extreme move expected to partially reverse, no structural reason |
| `sector_rotation` | Capital visibly rotating into or out of sector |
| `social_sentiment` | Reddit/WSB unusual mention spike coinciding with price momentum |
| `citrini_thesis` | Position aligned with active Citrini Research macro trade |
| `unknown` | Catalyst unclear or not verifiable — always valid |

**Boundary examples:**
- Beat + raise → primary: `earnings_beat`, secondary: `guidance_raise`
- Analyst upgrade on earnings day → primary: `earnings_beat`
- Stock up on sector sympathy → `sector_rotation`, not `earnings_beat`
- WSB mentions with no price action → not a catalyst; use `unknown`
- Congressional buy on same day as earnings → primary: `earnings_beat`,
  secondary: `congressional_buy`

**Not this:**
- `momentum_continuation` is not for stocks with a named catalyst — use the
  named catalyst type
- `technical_breakout` is not for any price move past a round number — level
  must be pre-defined and volume must confirm

---

## DIMENSION 2 — regime_label

**Definition:** What is the current market-operational environment?

**Rules:**
- One regime label active at a time
- Regime persists until explicitly changed by classifier
- `low_conviction` means genuinely unclear signals, not a hedge
- Hard cap: 8 active labels

**Deferred (not in v1.0.0):**
- `goldilocks`, `reflationary`, `disinflationary`, `stagflationary`
- These belong in a future `macro_backdrop_label` dimension
- Reason: macro backdrop descriptors are not mutually exclusive with
  market-operational regime labels. Mixing them in one dimension creates
  ambiguity (e.g. market can be risk_on while macro backdrop is disinflationary)

| Label | Definition | Key Signals |
|-------|-----------|-------------|
| `risk_on` | Broad equity strength, credit tight, VIX low, growth rotation | SPY up, HYG/LQD tight, VIX < 18 |
| `risk_off` | Defensive rotation, credit widening, gold/TLT bid | GLD up, TLT up, VXX up together |
| `volatility_spike` | VIX elevated, intraday swings large, regime uncertain | VIX 25–35, realized vol > implied |
| `crisis` | Systemic stress, credit seizing, correlations → 1 | VIX > 35, HYG crashing |
| `low_conviction` | Mixed signals, no clear regime direction | Conflicting indicators across dimensions |
| `unknown` | Insufficient or contradictory data | Always valid |

**Boundary examples:**
- VIX at 22 with equities flat → `low_conviction`, not `volatility_spike`
- Gold up but equities also up → `low_conviction` or `risk_on` depending on
  credit and VIX
- VIX at 30 with orderly selling → `volatility_spike`, not `crisis`

---

## DIMENSION 3 — move_character

**Definition:** What kind of market action produced this price move?

**Rules:**
- Multiple labels may co-exist — no separate `mixed` label
- There is no `mixed` value; use the applicable combination of labels
- Primary use: advisory/shadow layer and post-trade forensics
- Not required for production trade entry
- Hard cap: 12 active labels

| Label | Definition |
|-------|-----------|
| `real_information` | Move driven by genuine new fundamental information |
| `squeeze` | Move driven by forced covering of short or options positions |
| `retail_reflexivity` | Move driven by retail momentum and social amplification |
| `passive_flow` | Move driven by index rebalancing or ETF flows |
| `gamma_positioning` | Move amplified by options market maker hedging |
| `sector_spillover` | Move driven by related sector rather than stock-specific news |
| `macro_reprice` | Move driven by rates/FX/macro repricing |
| `thin_tape` | Move in low-volume conditions, likely to reverse |
| `unknown` | Character unclear — always valid |

**Multi-label examples:**
- Short squeeze amplified by Reddit → `squeeze` + `retail_reflexivity`
- Index rebalance on option expiry Friday → `passive_flow` + `gamma_positioning`

**Deferred (not in v1.0.0):**
- `liquidity_vacuum` — too close to `thin_tape` and `squeeze` for v1;
  reconsider if distinction proves meaningful in practice

---

## DIMENSION 4 — thesis_type

**Definition:** What is the core strategic rationale for holding this position?

**Rules:**
- One primary thesis per trade
- Hard cap: 10 active labels

| Label | Definition |
|-------|-----------|
| `momentum_continuation` | Riding an established trend with named catalyst |
| `mean_reversion` | Betting on return to prior level after extreme move |
| `catalyst_swing` | Multi-day hold around a specific named event |
| `sector_rotation` | Positioned for capital flows between sectors |
| `macro_overlay` | Position expresses a macro view (Citrini, rates, FX) |
| `volatility_expression` | Options position expressing a view on IV, not direction |
| `safe_haven` | Defensive position in risk-off regime |
| `unknown` | No clear thesis — should rarely appear in production |

**Removed from v1.0.0:**
- `arbitrage_adjacent` — premature for current bot behavior; reintroduce
  in a future version if bot develops event dislocation, volatility
  mispricing, or spread edge

---

## DIMENSION 5 — close_reason

**Definition:** Why was this trade closed? (Semantic/strategic reason)

**Distinct from `closure_source` in trade_closure_contract_v1.0.0.md,
which captures the mechanical cause.**

**Rules:**
- One close reason per trade
- Hard cap: 12 active labels

| Label | Definition |
|-------|-----------|
| `stop_hit` | Stop loss order filled |
| `take_profit_hit` | Take profit order filled |
| `deadline_exit` | Time-bound action deadline reached |
| `thesis_invalidated` | Catalyst or thesis no longer valid |
| `risk_containment` | Closed due to drawdown limit, VIX threshold, or regime change |
| `reallocation` | Closed to fund higher-conviction entry |
| `manual_close` | Human-initiated close |
| `expiry` | Options expiry or position expired |
| `reconcile_close` | Closed to reconcile with broker state |
| `unknown` | Reason not determinable |

**Deferred (not in v1.0.0):**
- `time_stop` — consider adding if holding-period-exceeded closes become
  common without a named deadline artifact

---

## Unknown / Mixed Rules (Global)

- `unknown` is always a valid value in every dimension — never suppress it
- There is no `mixed` label in any dimension
- move_character is the only dimension where multiple labels may co-exist
- No free-text category invention in downstream modules
- If a label does not exist: use `unknown`, flag for taxonomy review
- Downstream modules must never invent new labels — all additions go through
  schema owner review and version bump

---

## Dimension Summary

| Dimension | Hard Cap | Multi-label | Notes |
|-----------|----------|------------|-------|
| catalyst_type | 20 | No (one primary + secondary array) | Lower-trust flag on momentum_continuation |
| regime_label | 8 | No (one active at a time) | macro_backdrop deferred to future dimension |
| move_character | 12 | Yes | No mixed label; co-existing labels allowed |
| thesis_type | 10 | No | arbitrage_adjacent removed from v1 |
| close_reason | 12 | No | time_stop deferred |

---

## Dependency Map

| Ticket | Blocked by this document |
|--------|-------------------------|
| T1.1 Semantic labeling service | T0.7a (this document) |
| T2.1 Trade-thesis checksum | T1.1 |
| T2.2 Structured catalyst normalizer | T1.1 |
| T2.3 Post-trade forensic reviewer | T0.8 + T1.1 |
| T4.2 Regime translation layer | T1.1 + Phase 2 data |
| T4.3 Move character detector | T1.1 + Phase 2 data |
| T6.17 Taxonomy suggestion engine | T1.1 + sufficient labeled data |

---

*Last updated: 2026-04-16*
*Status: LOCKED — changes require version bump and schema owner approval*
*Next review trigger: first label retirement review after 90 days of usage data*
