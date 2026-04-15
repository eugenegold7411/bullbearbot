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
| **Position sizing (TIER_MAX_PCT)** | `risk_kernel.py` | `order_executor.py` | Kernel sizes qty; executor checks `position_value ≤ equity × tier_pct`. Values: core 15%, dynamic 8%, intraday 5%. |
| **PDT floor ($26,000)** | `risk_kernel.py` | `order_executor.py` | Kernel gate at top of `process_idea()`; executor `PDT_FLOOR` constant. PDT does NOT apply to crypto (BTC/USD, ETH/USD). |
| **Stop-loss limits** | `risk_kernel.py` (`_MAX_STOP_PCT`)| `order_executor.py` (`_load_stop_limits()`) | Kernel enforces during sizing; executor re-checks `stop_pct ≤ max_stop` before submit. Crypto stops are wider (8/10/12% vs 3.5/5/7%). |
| **Session eligibility** | `risk_kernel.py` (`eligibility_check()`) | `order_executor.py` (market-open check in `validate_action()`) | Kernel rejects ideas outside valid session windows; executor checks `market_status` / `is_crypto` for DAY vs GTC order type. |
| **Total exposure cap** | `risk_kernel.py` | `order_executor.py` | Kernel tracks aggregate exposure in `BrokerSnapshot`; executor checks conviction-adjusted effective cap against current long exposure. |
| **VIX halt (> 35)** | `risk_kernel.py` | — | Single-layer. Kernel rejects all new entries. No executor equivalent needed (kernel would have blocked upstream). |
| **Drawdown guard (> 20%)** | `bot.py` (`run_cycle()`) | — | Single-layer at cycle start. Halts entire cycle before any kernel or executor call. |
| **R/R minimum (1.5×)** | `risk_kernel.py` | `order_executor.py` | Kernel enforces during stop/target calculation; executor re-checks `rr ≥ MIN_RR_RATIO`. |
| **Price-scale sanity** | — | `order_executor.py` | Executor-only backstop. Catches crypto stop values on the wrong scale (e.g., BUG-008: signal score used as stop_loss price). `_is_price_scale_error()` helper. |
| **Crypto GTC (vs DAY)** | `risk_kernel.py` | `order_executor.py` | Kernel sets `order_type`; executor enforces GTC for crypto symbols (contains `/`). |
| **HOLD market-hours gate** | — | `order_executor.py` | Executor-only. HOLDs/monitors are allowed outside market hours for stop refresh; only new entries require market open. |

---

## Constants Declared in Both Modules

These constants are duplicated intentionally. If you change a value, update **both** modules
and update this table. The risk kernel is authoritative; the executor is a safety net.

| Constant | `risk_kernel.py` | `order_executor.py` |
|----------|-----------------|-------------------|
| `TIER_MAX_PCT` (core) | `0.15` | `0.15` |
| `TIER_MAX_PCT` (dynamic) | `0.08` | `0.08` |
| `TIER_MAX_PCT` (intraday) | `0.05` | `0.05` |
| `PDT_FLOOR` | `26_000.0` | `26_000.0` |
| Max stop — equities core | `0.035` | `0.035` (from `strategy_config.json` or fallback) |
| Max stop — equities standard | `0.05` | `0.05` |
| Max stop — crypto core | `0.08` | `0.08` |
| Min R/R ratio | `1.5` | `1.5` (`MIN_RR_RATIO`) |

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
- **No** → add it to `risk_kernel.py` only (or both, if backstop is warranted).
- **Yes** (direct executor calls, test harnesses, future code paths) → add to both.

Executor-only checks that exist today (`price_scale_sanity`, `HOLD market-hours gate`)
are for catching failure modes that the kernel cannot observe (wrong-scale numbers from
Claude, session-unaware HOLDs).
