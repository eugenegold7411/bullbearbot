# Preflight Contract v1.0.0

> Governs the cycle go/no-go gate implemented in `preflight.py`.
> Called at the top of `run_cycle()` (bot.py) and `run_options_cycle()` (bot_options.py).

---

## 1. Purpose

The preflight gate runs before any account fetch or market data load. Its job is to catch
known-bad system states early and return a verdict that callers act on. It reads only local
files — no network calls, never raises.

Every preflight call appends a structured record to `data/status/preflight_log.jsonl` for
audit and post-incident review.

---

## 2. Checks and Verdicts

### Check Families

| Check | Severity | Passes When |
|-------|----------|------------|
| `config_present` | HARD | `strategy_config.json` exists and is valid JSON |
| `pdt_floor` | HARD | equity ≥ $26,000 (when equity is known) |
| `operating_mode_a1` | HARD | A1 divergence mode is NORMAL (not HALTED or RECONCILE_ONLY) |
| `operating_mode_a2` | HARD | A2 divergence mode is NORMAL |
| `watchlist_present` | SOFT | `watchlist.json` exists and has ≥1 symbol |
| `vix_gate` | SOFT | VIX < 35 (from `data/market/macro_snapshot.json` if available) |
| `recent_halt_markers` | SOFT | No DRAWDOWN GUARD / mode=halted / [HALT] in last 100 log lines |
| `feature_flags_loadable` | SOFT | `feature_flags.get_all_flags()` returns a dict |
| `data_dirs` | SOFT | `data/analytics/` and `data/market/` exist |
| `config_keys` | SOFT | `strategy_config.json` has `active_strategy` and `parameters` keys |

### Verdict Derivation

| Condition | Verdict |
|-----------|---------|
| Any HARD failure referencing halt | `halt` |
| Any HARD failure referencing reconcile | `reconcile_only` |
| One or more SOFT failures | `go_degraded` |
| All checks pass | `go` |

### Caller Behavior

| Verdict | `run_cycle()` | `run_options_cycle()` |
|---------|--------------|----------------------|
| `go` | Proceed | Proceed |
| `go_degraded` | Log WARNING, proceed | Log WARNING, proceed |
| `shadow_only` | Future use (not currently returned) | Future use |
| `reconcile_only` | Log WARNING, skip new trades | Log WARNING, skip new proposals |
| `halt` | Log ERROR, return immediately | Log ERROR, return immediately |

---

## 3. Log Format

Every call appends a JSON line to `data/status/preflight_log.jsonl`:

```json
{
  "schema_version": 1,
  "checked_at": "2026-04-16T14:23:11Z",
  "caller": "run_cycle",
  "session_tier": "market",
  "verdict": "go_degraded",
  "checks": [
    {"name": "config_present", "passed": true, "severity": "hard", "message": "strategy_config.json OK"},
    {"name": "vix_gate", "passed": false, "severity": "soft", "message": "VIX=28.3 OK"}
  ],
  "blockers": [],
  "warnings": ["vix_gate: VIX=38.1 > 35 — crisis regime flag"]
}
```

---

## 4. Adding New Checks

To add a new preflight check:

1. Write a `_check_*() -> CheckResult` function in `preflight.py`
2. Set `severity="hard"` only if a failure must stop the cycle (reserve for true halts)
3. Add the call to the `checks` list in `run_preflight()`
4. Add the check to the table in this document and bump the version
5. Run `python3 -m py_compile preflight.py`
6. Add a test to Suite 36 in `tests/test_core.py`

**Hard check criteria:** The system is in a state where executing a trade could cause
financial loss beyond the intended risk envelope, or where a required dependency (config,
operating mode) is known-corrupted. When in doubt, use SOFT.
