# Decision Authority Reference

**Version:** v0.1 — draft, April 29, 2026  
**Status:** Living reference. Updates every sprint.  
**Purpose:** Documents current design intent for how trading decisions are made, constrained, and logged. Not an enforcement specification — the code is authoritative. This document exists so future sessions start with a correct mental model.

---

## Section 1 — Authority Hierarchy

Three layers, applied in strict precedence order:

### Layer 1 — Risk Kernel (Absolute)
Hard limits enforced in `risk_kernel.py`. Cannot be overridden by Sonnet, the system prompt, or any config value. A kernel rejection is a final rejection — the action is dropped and logged. No reasoning or override path exists at this layer.

### Layer 2 — System Prompt (Strong Guidance)
Rules defined in `prompts/system_v1.txt`. Sonnet is expected to follow these. Deviation is permitted only with explicit, documented reasoning in the `notes` field of the relevant `TradeIdea`. Deviations are captured in `memory/decisions.json` and reviewed at the weekly board meeting.

### Layer 3 — Sonnet Judgment (Everything Else)
All decisions not covered by Layer 1 or Layer 2 are left to Sonnet's in-context reasoning. This is the intended default — the system is designed to be minimally constrained.

---

## Section 2 — What the Kernel Owns (Hard Rules)

These rules live in `risk_kernel.py`. Each fires as a rejection with a reason string that propagates to `logs/bot.log` and `logs/trades.jsonl`.

### 2.1 — Per-Tier Position Size Cap
**What it does:** Limits any single position to a fixed percentage of equity, by watchlist tier.  
**Where:** `risk_kernel.py:73–79` (`_TIER_MAX_PCT`)  
**Values:**
- `core`: 15% of equity
- `dynamic`: 8% of equity
- `intraday`: 5% of equity
- High-conviction core override: 20% (`_CORE_HIGH_CONVICTION_PCT`, `risk_kernel.py:82`)

**On fire:** Action rejected. Reason string includes requested size and cap value. Sonnet receives no retry signal.

### 2.2 — Maximum Total Portfolio Exposure
**What it does:** Caps total deployed capital based on conviction level and a margin multiplier. Three conviction bands: high (≥0.75), medium (≥0.50), low (<0.50). Higher conviction unlocks proportionally more buying power.  
**Where:** `risk_kernel.py:249` (`_effective_exposure_cap()`)  
**Note:** `strategy_config.json:11` contains `max_total_exposure_pct: 0.95` but this field is display-only (explicitly marked as unused for enforcement). The kernel's `_effective_exposure_cap` is the sole enforcer.  
**On fire:** Buy action rejected if current exposure + proposed size would breach the conviction-band ceiling.

### 2.3 — Conviction Minimum for ADD Actions
**What it does:** Enforces a minimum conviction of 0.65 before the kernel approves adding to an existing position (S7-H). Prevents Sonnet from pyramid-buying positions the signal scorer is lukewarm about.  
**Where:** `risk_kernel.py:440`, configured at `strategy_config.json:72` (`add_conviction_gate: 0.65`)  
**On fire:** Action type `ADD` rejected with reason `conviction_below_add_gate`. Action is fully dropped.

### 2.4 — Cap Headroom Check for ADD
**What it does:** When evaluating an ADD order, the kernel subtracts the existing position value from the tier cap before computing available headroom. Prevents ADD from double-counting already-deployed capital and breaching the tier ceiling (S7-P).  
**Where:** `risk_kernel.py:530–534`  
**On fire:** If `max_pos_dollars - existing_val ≤ 0`, the ADD is rejected. The existing position is at or above cap.

### 2.5 — Stop Width Floors by Tier and Asset Class
**What it does:** The kernel defines maximum stop-loss percentages per tier and asset class. Sonnet may request tighter stops but never wider ones. Config values are soft guidance; these ceilings are absolute.  
**Where:** `risk_kernel.py:83–96` (`_MAX_STOP_PCT`)  
**Values:**
| Tier | Stocks | Crypto |
|------|--------|--------|
| core | 4% | 8% |
| dynamic | 5% | 10% |
| intraday | 2% | 8% |

**On fire:** Stop is silently clamped to the ceiling. No rejection — the trade proceeds with a tighter stop than requested.

### 2.6 — PDT Equity Floor
**What it does:** Halts all new A1 positions if account equity falls below $26,000. Protects against PDT rule violations under US pattern day-trading regulations. Note: PDT rules do not apply to BTC/USD or ETH/USD.  
**Where:** `risk_kernel.py:66` (`PDT_FLOOR = 26_000.0`), checked at `risk_kernel.py:406`. Backstop also at `order_executor.py:122`.  
**On fire:** All BUY/ADD actions rejected for that cycle. HOLDs and exits still execute.

### 2.7 — A2 Equity Floor
**What it does:** Halts all new Account 2 options positions if A2 account equity falls below $25,000.  
**Where:** `risk_kernel.py:1168–1170` (`a2_floor = float(a2_cfg.get("equity_floor", 25_000.0))`)  
**On fire:** Options cycle exits early. No new structures proposed or submitted.

### 2.8 — Tier Demotion for Low-Score CORE Requests
**What it does:** If a signal score for a symbol tagged as `core` is below 65, the kernel applies the `dynamic` tier cap instead of the `core` tier cap. This prevents the system from deploying 15% of equity into a name the signal scorer rates below conviction threshold (S7-P apply_tier_cap).  
**Where:** `risk_kernel.py:656` (`_TIER_CAP_SCORE_THRESHOLD: float = 65.0`), applied at `risk_kernel.py:681–688`  
**On fire:** Position size silently capped at 8% (dynamic ceiling) instead of 15% (core ceiling). The tier label is not changed — only the sizing cap is demoted.

---

## Section 3 — What the Prompt Owns (Judgment Calls)

These rules are defined in `prompts/system_v1.txt`. Sonnet is expected to follow them. Override is permitted with explicit `notes` field documentation.

### 3.1 — Binary Event Exposure Rule
**What it instructs:** Before holding full size or adding to a position within 1 trading day of a binary event (earnings, macro print), Sonnet must reason explicitly through the evidence stack in the `notes` field.  
**Source:** `prompts/system_v1.txt:99`, `prompts/system_v1.txt:231`  
**Can Sonnet override?** Yes. Full-size holds and adds are explicitly allowed when the evidence stack is strong (see Section 4).  
**Override logging requirement:** The `notes` field must document the reasoning. Silence defaults to reducing position size. The decision is captured in `memory/decisions.json` and visible at board review.

### 3.2 — Post-Earnings / Catalyst Consumed Rule
**What it instructs:** Once a held position has reported earnings or a major catalyst has resolved, the prior thesis is consumed. Sonnet must re-evaluate as a fresh entry: would I enter this today at this price? If yes with a new forward catalyst: HOLD. If yes but no fresh catalyst: consider TRIM. If no: close.  
**Source:** `prompts/system_v1.txt:241–251`  
**Can Sonnet override?** Yes, with explicit reasoning about the forward catalyst.  
**Override logging requirement:** `notes` field must state the new thesis.

### 3.3 — Thesis Score Action Guide
**What it instructs:** Default actions based on portfolio intelligence thesis scores for held positions.  
**Source:** `prompts/system_v1.txt:281–290`  
| Score | Default Action |
|-------|---------------|
| 8–10 | HOLD unless capital is clearly better used elsewhere |
| 6–7 | HOLD with monitoring |
| 4–5 | TRIM 25% if fresher opportunities exist |
| 2–3 | TRIM 50% or close |
| 0–1 | Close |

**Can Sonnet override?** Yes. The prompt explicitly states "these are defaults, not blind rules. Override only with explicit reasoning."  
**Override logging requirement:** `notes` field must document the override rationale.

### 3.4 — Scratchpad Soft Gate
**What it instructs:** A BUY on a symbol not in the scratchpad's `watching` list is a soft block. Sonnet must set `override_scratchpad: true` and populate `override_reason` to proceed.  
**Source:** `bot.py:492–506` (runtime gate), `schemas.py:329` (`override_scratchpad: bool = False`)  
**Can Sonnet override?** Yes — the gate is explicitly soft. The flag is available in the decision schema.  
**Override logging requirement:** `override_reason` field in the `TradeIdea`. Logged to `memory/decisions.json`.

### 3.5 — Sector Rotation Awareness
**What it instructs:** Sonnet should track sector correlation and portfolio concentration. The portfolio intelligence section provides a correlation matrix and health flags each cycle. Sonnet is expected to consider sector balance when proposing entries.  
**Source:** `prompts/system_v1.txt:270–278`, `portfolio_intelligence.py` (PRESENTATION authority)  
**Can Sonnet override?** Yes — full Sonnet discretion at this layer.

### 3.6 — ORB Rules (9:30–9:45 Observe Only)
**What it instructs:** During the first 15 minutes of market open, observe only. No new positions. After 9:45 AM the ORB range is set; breakouts above/below with volume are valid entry signals.  
**Source:** `prompts/system_v1.txt:261–268`  
**Can Sonnet override?** No. The "never trade the first 15 minutes" rule is listed in the WHAT YOU NEVER DO section (`system_v1.txt:98`).  
**Note:** This is a prompt-level rule, not kernel-enforced. It could in principle be overridden by Sonnet, but the prompt framing is absolute.

### 3.7 — Regime-Based Participation Levels (VIX Bands)
**What it instructs:** VIX bands drive participation reduction:
- VIX 22–25: caution — reduce weakest setups, prefer liquid names
- VIX 25–35: elevated — reduce size materially, prefer defined-risk structures
- VIX > 35: crisis — halt new aggressive risk  

**Source:** `prompts/system_v1.txt:91–95`  
**Note:** VIX > 35 halt is also kernel-enforced (`risk_kernel.py:67`, `VIX_HALT = 35.0`). VIX 25–35 band is prompt guidance only.  
**Can Sonnet override?** Yes for the 22–35 range, with explicit reasoning. VIX > 35 kernel halt is not overridable.

---

## Section 4 — Binary Event Policy

**Decision date:** April 29, 2026

### Core principle
The binary event rule is a **reasoning requirement, not a sizing mandate**. The system does not automatically reduce position size near earnings — that would be a kernel rule. Instead, the rule requires Sonnet to explicitly engage with the risk.

### What is allowed
- Full-size holds and adds are explicitly permitted when the evidence stack justifies them.
- The prompt defines the full-size carry threshold: thesis score 8/10+, earnings intelligence favorable, tape confirming, and a forward catalyst existing beyond the print.
- A strong historical beat rate alone does not justify full-size carry.

### What is required
- Sonnet must populate the `notes` field when holding or adding at full size within 1 trading day of a binary event.
- Silence is not acceptable — the prompt is explicit: "Unsupported silence defaults to reducing."
- Source: `prompts/system_v1.txt:99`, `prompts/system_v1.txt:229–235`

### Kernel enforcement
None. The kernel does not inspect `earnings_days_away` or enforce size reduction near events. This is entirely a prompt-layer accountability mechanism.

### Board visibility
Every binary event decision (hold/add) is captured in `memory/decisions.json`. The `notes` field is the audit trail. Weekly review agents 1 and 10 review decision quality, including binary event handling.

---

## Section 5 — Stop Strategy Near Binary Events

**Decision date:** April 29, 2026

### Short-term: trail-stop tightening pauses near earnings (implemented)
When `earnings_days_away ≤ 1`, the exit manager replaces the tight trail target with a wider IV-based stop floor. This prevents the position from being stopped out by earnings-day volatility expansion before the catalyst resolves.  
**Where:** `exit_manager.py:695–715`  
**Config key:** `earnings_aware_stop_enabled` (default: `false`)  
**Behavior when enabled:** Stop floor set to `entry × (1 − max(IV, iv_floor_pct))`. Only widens the stop — never narrows it. If the computed earnings floor is below the current stop, no change is made.

### Medium-term: earnings-aware stop floor (implemented, disabled by default)
The logic above is fully implemented and tested. It is disabled by default (`strategy_config.json:106`, `earnings_aware_stop_enabled: false`) pending further validation. Enabling requires an explicit config change.

### Long-term: graduated trail-stop system (planned, Sprint 9)
The current trail-stop formula is fixed: a single trigger threshold and a single trail offset. Sprint 9 will replace this with a graduated system that steps the trail progressively as profit-R increases. Not yet implemented.

---

## Section 6 — Exposure and Sizing Basis

**Decision date:** April 29, 2026

These decisions were codified in portfolio_intelligence.py (display) and risk_kernel.py (enforcement). They apply consistently across all modules.

### Exposure percentage denominator
All exposure calculations use **total capacity** as the denominator:  
`total_capacity = current_exposure_dollars + buying_power`  
**Where:** `portfolio_intelligence.py:112`

This denominator correctly reflects deployed + available capital. Using equity alone would overstate exposure percentage for leveraged accounts; using buying_power alone would understate it.

### Available capital calculation
All available-capital calculations use `buying_power` directly. Buying power is already net of deployed margin, so no adjustment is needed.

### Individual position concentration display
Position concentration is displayed to Sonnet as percentage of equity (not total capacity), because equity is the human-readable reference for portfolio health. This is for display only — the kernel uses buying_power for sizing decisions.

### Oversize flags (portfolio intelligence)
Oversize bands use `buying_power` as denominator with these thresholds:
- `> 25%`: hard ceiling flag
- `> 15%`: core confirm required
- `> 8%`: dynamic flag  

**Where:** `portfolio_intelligence.py:341` (authority: PRESENTATION — no enforcement)

### Scope
These decisions apply to: `portfolio_intelligence.py`, `bot_stage3_decision.py`, `bot_stage0_precycle.py`, `portfolio_allocator.py`.

---

## Section 7 — A2 Authority Boundaries

### A1 → A2 relationship
A2 operates as an independent decision system. A1 informs A2's macro context by writing signal scores to `data/market/signal_scores.json` after each market cycle. A1's signal scores are candidate inputs — A2's four-way debate (Bull / Bear / IV Analyst / Synthesis) makes its own trade decisions. A1 does not dictate A2 entries.

### Confidence floor
| Mode | Threshold | Config key |
|------|-----------|-----------|
| Paper | 0.75 | `account2.paper_confidence_floor` |
| Live | 0.85 | `account2.live_confidence_floor` |

**Where:** `bot_options_stage3_debate.py:439–441`, `strategy_config.json:144`

If Synthesis confidence < floor → `no_trade` outcome with reason `debate_low_confidence`. No kernel involvement at this gate — it is a debate-stage gate.

### Observation mode
Complete. `trading_days_observed: 20`, `observation_complete: true`. IV history seeded for all 43 optionable symbols. A2 is live — structures are submitted to Alpaca paper.

### Portfolio allocator (A1 trim-only)
**Status: NOT READY for promotion.** The allocator runs in shadow mode only. Its output is logged to `data/reports/shadow_status_latest.json` but does not feed execution. Promotion requires the gates in Section 8.

---

## Section 8 — Promotion Readiness Gates

### Gate A — A1 Portfolio Allocator (trim-only promotion)

The allocator currently operates in shadow mode. Promotion to executing TRIM recommendations requires all of the following:

| Gate | Status |
|------|--------|
| 14 consecutive valid shadow cycles | pending |
| `shadow_status_latest.json` populated and reviewed | pending |
| Trim threshold authority verified (post-Sprint 7) | ✅ resolved |
| Cooldown persistence strategy decided | pending |
| Weekly board meeting reviews shadow output | ongoing |
| Several real TRIM/ADD/REPLACE examples reviewed qualitatively | pending |
| `decision_authority.md` finalized | pending (this document) |

**Where shadow registry lives:** `portfolio_allocator.py:46` (`data/reports/shadow_status_latest.json`)

No target date set. Promotion requires explicit board meeting approval, not just passing gates.

### Gate B — A2 Live Trading Promotion

A2 is currently paper trading. Promotion to live execution requires:

| Gate | Status |
|------|--------|
| Sufficient paper trading evidence | target: May 16, 2026 |
| Confidence distribution review at 0.75 threshold | pending |
| IV data reliability confirmed for all A1 position symbols | ✅ seeded (43 symbols) |
| Graduated trail-stop implemented and tested | pending (Sprint 9) |
| Weekly board meeting explicitly approves | pending |

**Target date:** May 16, 2026. This is a target, not a hard deadline — board meeting approval is required regardless.

---

## Section 9 — Known Gaps and Sprint 9 Items

Items to resolve before May 16, 2026 target date for A2 live promotion:

| Item | Description | Priority |
|------|-------------|---------|
| Graduated trail-stop | Replaces fixed trail formula with progressive steps as profit-R increases | High |
| Notification accuracy | Notifications currently fire on order submission, not on fill confirmation | Medium |
| EIA inventory data | Current macro wire reads EIA via RSS; direct API pull would be more reliable | Low |
| GOOGL morning brief entry zone | Entry zone in morning brief may become stale intraday — needs freshness check | Medium |
| Scratchpad binary event flag | `scratchpad.py` binary event flag currently applies to new entries only; held positions are not re-flagged | Medium |
| PI scorer earnings adjustment | `catalyst_consumed` auto-flip timing in portfolio intelligence scorer is not calibrated | Low |
| Reddit credentials (F001) | `reddit_sentiment.py` is complete; just needs credentials in `.env` | Low |
| Bot health dashboard | In progress — operational visibility for session-level health | Medium |

---

## Appendix — Key File Reference

| File | Role in authority chain |
|------|------------------------|
| `risk_kernel.py` | Layer 1 — absolute hard limits |
| `prompts/system_v1.txt` | Layer 2 — strong guidance and judgment framework |
| `strategy_config.json` | Config values read by kernel and prompt assembly |
| `portfolio_intelligence.py` | PRESENTATION authority — formats analytics for Sonnet |
| `exit_manager.py` | Stop management — earnings-aware stop floor |
| `bot.py` | Scratchpad soft gate at Layer 2/3 boundary |
| `bot_options_stage3_debate.py` | A2 confidence floor gate |
| `portfolio_allocator.py` | Shadow ring only — trim-only allocator, not yet in execution path |
| `docs/policy_ownership_map.md` | Companion doc — dual-layer policy ownership, executor input contract |
