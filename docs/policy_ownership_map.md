# Policy Ownership Map

Policy rules in this codebase are intentionally enforced at two layers:
`risk_kernel.py` (primary, upstream) and `order_executor.py` (hard backstop, downstream).
This document records which module **owns** each policy, what the other module does as a
backstop, and why both layers exist.

---

## Design Principle

The dual-layer approach is deliberate:

- **`risk_kernel.py` (primary):** Converts Claude's intent-based ideas into sized
  `BrokerAction` objects. Applies position sizing, stop limits, session eligibility, and
  exposure caps. This is the policy-enforcement layer: if it rejects an idea, no order
  is ever attempted.
- **`order_executor.py` (backstop):** Validates the resulting `BrokerAction` dict just
  before Alpaca submission. It should never fire for a well-formed kernel output, but
  catches edge cases: direct executor calls from tests, future code paths that bypass
  the kernel, or kernel bugs that emit out-of-policy values.

**Do not consolidate these into a single layer.** The redundancy is load-bearing.
If the kernel were removed, the executor would catch violations. If the executor were
removed, a kernel bug could submit a malformed order directly to Alpaca.

---

## Policy Table

| Policy | Primary Owner | Backstop | Notes |
|--------|--------------|---------|-------|
| **Position sizing (tier ceilings)** | `risk_kernel.py` (`_TIER_MAX_PCT`) | `order_executor.py` (WARNING only) | ✅ resolved: kernel primary, executor WARNING only. Values: core 15%, dynamic 8%, intraday 5%. `TIER_MAX_PCT` removed from executor in Session 1. |
| **PDT floor ($26,000)** | `risk_kernel.py` | `order_executor.py` (`PDT_FLOOR`, hard rejection) | Kernel gate at top of `process_idea()`; executor `PDT_FLOOR` constant kept as hard backstop (regulatory requirement). PDT does NOT apply to crypto. |
| **Stop-loss limits** | `risk_kernel.py` (`_MAX_STOP_PCT`) | `order_executor.py` (WARNING only) | ✅ resolved: kernel primary, executor WARNING only. Crypto stops are wider (8/10/12% vs 4/5/7%). |
| **Session eligibility** | `risk_kernel.py` (`eligibility_check()`) | `order_executor.py` (WARNING only) | ✅ resolved: kernel primary, executor WARNING only. Executor market-open check demoted to `log.warning` in Session 1. |
| **Total exposure cap** | `risk_kernel.py` (`_effective_exposure_cap()`) | `order_executor.py` (WARNING only) | ✅ resolved: kernel primary, executor WARNING only. Executor exposure check demoted in Session 1. |
| **R/R minimum (2.0×)** | `risk_kernel.py` | `order_executor.py` (WARNING only) | ✅ resolved: kernel primary, executor WARNING only. Demoted in Session 1. |
| **VIX halt (> 35)** | `risk_kernel.py` | — | Single-layer. Kernel rejects all new entries. No executor equivalent needed. |
| **Drawdown guard (> 20%)** | `bot.py` (`run_cycle()`) | — | Single-layer at cycle start. Halts entire cycle before any kernel or executor call. |
| **Price-scale sanity** | — | `order_executor.py` (hard rejection) | Executor-only backstop. Catches crypto stop values on the wrong scale (e.g., BUG-008: signal score used as stop_loss price). |
| **Crypto GTC (vs DAY)** | `risk_kernel.py` | `order_executor.py` | Kernel sets `order_type`; executor enforces GTC for crypto symbols. |
| **HOLD market-hours gate** | — | `order_executor.py` | Executor-only. HOLDs/monitors are allowed outside market hours for stop refresh; only new entries warn (not reject) on market closed. |
| **Bracket order shape** | — | `order_executor.py` (hard rejection) | stop_loss required, take_profit required, stop below entry, take_profit above entry — structural invariants the kernel always satisfies but executor verifies as absolute last gate. |
| **ORB formation window** | — | `order_executor.py` (hard rejection) | Executor-only hard gate: blocks new stock/ETF entries during 9:30–9:45 AM ET window. Strategy policy but justified as hard backstop since kernel has no minute-level time check. |

---

## Constants: Current State

After Session 1 consolidation. The kernel is authoritative; the executor is a safety net.

| Constant | `risk_kernel.py` | `order_executor.py` |
|----------|-----------------|-------------------|
| `_TIER_MAX_PCT` (core) | `0.15` | **Removed** — executor uses WARNING only with local `_tier_ceiling` |
| `_TIER_MAX_PCT` (dynamic) | `0.08` | **Removed** |
| `_TIER_MAX_PCT` (intraday) | `0.05` | **Removed** |
| `PDT_FLOOR` | `26_000.0` | `26_000.0` (kept — regulatory backstop) |
| Max stop — equities core | `0.04` ceiling | `0.04` floor (from `strategy_config.json`) |
| Max stop — equities standard | `0.05` | `0.05` |
| Max stop — crypto core | `0.08` | `0.08` |
| Min R/R ratio | `2.0` | `2.0` (`MIN_RR_RATIO`, WARNING only) |

---

## Resolved Duplicates — Session 1 (2026-04-15)

The following executor constants/checks were consolidated in Session 1:

| Item | Before | After |
|------|--------|-------|
| `TIER_MAX_PCT` dict | Hard-coded module-level constant in executor, used for rejection | **Removed from executor.** Kernel `_TIER_MAX_PCT` is sole authoritative definition. Executor logs WARNING only. |
| Position size rejection | `_check(position_value <= max_position, ...)` in `validate_action()` | Demoted to `log.warning(...)` — no rejection |
| Exposure cap rejection | `_check(new_exposure <= effective_cap, ...)` | Demoted to `log.warning(...)` — no rejection |
| Stop-loss width rejection | `_check(stop_pct <= max_stop, ...)` | Demoted to `log.warning(...)` — no rejection |
| R/R ratio rejection | `_check(rr >= MIN_RR_RATIO, ...)` | Demoted to `log.warning(...)` — no rejection |
| Session eligibility rejection | `_check(market_status == "open", ...)` | Demoted to `log.warning(...)` — no rejection |
| Minutes-since-open rejection | `_check(minutes_since_open >= MIN_MINUTES_OPEN, ...)` | Demoted to `log.warning(...)` — no rejection |
| `[MARGIN] log.info` on every buy | Logged at INFO on every validated buy | Demoted to DEBUG |

---

## Module Boundaries (executor input contract)

Since Phase A (2026-04-15), `execute_all()` accepts either `BrokerAction` objects or
legacy dicts (backward-compat). The normalisation block at the top of `execute_all()`
handles routing:

- `BrokerAction` → `.to_dict()` → proceeds normally
- `dict` → WARNING logged → proceeds (backward-compat path)
- Unknown type → WARNING logged → **skipped** (never submitted to Alpaca)

`BrokerAction.to_dict()` maps `conviction` → `"confidence"` for executor consumption.
The `source_idea` field is intentionally excluded from `to_dict()` (internal attribution
only; not needed by executor).

---

## Do Not Add to Executor Alone

If you are tempted to add a new policy check only in `order_executor.py`, ask:
*Can this idea reach the executor without passing through the risk kernel?*
- **No** → add it to `risk_kernel.py` only (or both if backstop is warranted).
- **Yes** (direct executor calls, test harnesses, future code paths) → add to both,
  but make the executor check a **WARNING** unless it is a structural invariant
  (shape check, regulatory floor, or physical impossibility).

Executor-only hard rejections that remain today:
- `price_scale_sanity` — catches signal-score-as-price (kernel cannot observe)
- `PDT_FLOOR` — regulatory, dual enforcement justified
- `stop_loss required / take_profit required` — structural shape
- `stop below entry / take_profit above entry` — physical invariant
- `ORB formation window` — minute-level time check kernel lacks
