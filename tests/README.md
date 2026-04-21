# Test Suite

## Quick start

```bash
# From the trading-bot root directory:
pip install -e . -r requirements-dev.txt   # first time only
make test                                   # run all tests
```

## What's in here

| File | Purpose |
|------|---------|
| `conftest.py` | Session fixtures (`kernel_config`) and optional-package stubs |
| `test_core.py` | Main suite — ~341 tests across all production modules |
| `test_import_safety.py` | Verifies orchestrator modules import without env vars |
| `test_risk_kernel_eligibility.py` | `eligibility_check()` — all 6 hard gates |
| `test_risk_kernel_size_position.py` | `size_position()` — tier pcts, VIX scaling, headroom |
| `test_risk_kernel_place_stops.py` | `place_stops()` — stop/target levels, ceilings, MIN_RR |
| `test_scratchpad_isolated.py` | Scratchpad logic (offline-safe subset) |
| `test_scratchpad_memory.py` | Scratchpad + ChromaDB integration (requires chromadb) |
| `test_s4a_veto_thresholds_universe.py` | S4-A: A2 veto threshold config + 43-symbol universe |
| `test_s4c_warehouse_scheduler_fixes.py` | S4-C: data_warehouse ETF/VIX/Finnhub silent failures |
| `test_s6_portfolio_allocator.py` | S6-ALLOCATOR: portfolio allocator shadow engine |
| `KNOWN_FAILURES.md` | Triaged failures with root causes |

## conftest.py

`conftest.py` provides:

**`kernel_config` fixture (session-scoped)**  
A minimal `strategy_config.json`-shaped dict consumed by `risk_kernel`
functions. Mirrors the real config structure without file I/O or external
dependencies. Available to all test files automatically.

**Optional-package stubs**  
If `alpaca`, `anthropic`, `twilio`, or `pandas_ta` are absent (e.g. in a
stripped CI environment), the conftest inserts a bare namespace stub so that
`import` succeeds at collection time. Tests that call real package APIs will
still fail if the package is absent; only import-time failures are prevented.

`chromadb` is intentionally NOT stubbed — `trade_memory.py` has its own
graceful degradation and must detect the real absence of chromadb itself.

## Risk kernel tests

`risk_kernel.py` is pure computation (no broker calls, no file I/O, no env
vars). All kernel tests are:

- **Offline-safe** — no Alpaca, Claude, Twilio, or network calls of any kind
- **Sub-second** — no sleep, no I/O
- **Self-contained** — only depend on `risk_kernel`, `schemas`, and stdlib

To add a new kernel test:

1. Import `from risk_kernel import <function>` directly.
2. Build inputs using `TradeIdea`, `BrokerSnapshot`, `NormalizedPosition` from
   `schemas`.
3. Use the `kernel_config` fixture for the config dict; override specific keys
   with `copy.deepcopy(kernel_config)` when testing non-default config values.
4. Assert on the return type: `None` (eligible / success) vs a non-empty `str`
   (rejection reason), or `tuple[float, float]` vs `str` for sizing/stops.

## Import safety tests

`test_import_safety.py` verifies that `bot.py`, `order_executor.py`,
`weekly_review.py`, and `bot_options.py` all import without raising
`EnvironmentError` when credentials are absent (empty env vars).

Three bot-related tests are **skipped** rather than failed — this is expected
and correct. They skip because `data_warehouse.py` instantiates
`StockHistoricalDataClient` at module level (line 50), which raises
`ValueError` with empty credentials. Fixing this is out of scope (transitive
dependency, not touched by Prompt 2).

## Known failures

See `KNOWN_FAILURES.md` for the two pre-existing `test_core.py` failures and
the eight chromadb-dependent `test_scratchpad_memory.py` failures.
