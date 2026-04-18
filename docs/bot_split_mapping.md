# bot.py split mapping

Maps every function extracted from the pre-split `bot.py` to its new home.
The original file was ~2,270 lines; the refactored orchestrator is ~860 lines.

---

## bot_clients.py (new — shared client singletons)

| Symbol | Notes |
|--------|-------|
| `MODEL` | Constant — `claude-sonnet-4-6` |
| `MODEL_FAST` | Constant — `claude-haiku-4-5-20251001` |
| `_get_alpaca()` | Lazy-init Alpaca `TradingClient` singleton |
| `_get_claude()` | Lazy-init `anthropic.Anthropic` singleton |

Not in the original spec — created to break the circular import that would arise if stage
files imported from `bot.py` and `bot.py` imported from stage files.

---

## bot_stage0_precycle.py — Stage 0: pre-cycle infrastructure

| Symbol | Source lines (pre-split) | Notes |
|--------|--------------------------|-------|
| `PreCycleState` (dataclass) | new | Typed return value carrying all pre-cycle state |
| `run_precycle()` | bot.py lines ~175–430 | Accounts, preflight, market data, memory, PI, recon, divergence, exit mgr |

`run_precycle()` returns `None` on preflight `verdict=halt` so `run_cycle()` can abort.
The drawdown guard (`_check_drawdown`) is **not** here — it lives in `bot.py` because it
owns the module-level `_peak_equity` / `_last_drawdown_alert` / `_drawdown_state_loaded`
globals.

---

## bot_stage1_regime.py — Stage 1: Haiku regime classifier

| Symbol | Source lines (pre-split) | Notes |
|--------|--------------------------|-------|
| `_REGIME_SYS` | bot.py ~417 | System prompt constant |
| `classify_regime()` | bot.py ~430–530 | Single Haiku call → regime dict |
| `format_regime_summary()` | bot.py ~532–545 | Formats regime dict for prompt injection |

---

## bot_stage2_signal.py — Stage 2: Haiku signal scorer

| Symbol | Source lines (pre-split) | Notes |
|--------|--------------------------|-------|
| `_SIGNAL_SYS` | bot.py ~550 | System prompt constant |
| `score_signals()` | bot.py ~565–740 | Prioritisation + Haiku call + JSON repair + retry |
| `format_signal_scores()` | bot.py ~742–762 | Formats scored_symbols for prompt injection |

`score_signals()` includes the prioritisation logic (held positions → morning brief →
breaking news → fill) and the JSON repair-by-truncation + retry path.

---

## bot_stage2_5_scratchpad.py — Stage 2.5: Haiku pre-decision scratchpad

| Symbol | Source lines (pre-split) | Notes |
|--------|--------------------------|-------|
| `run_scratchpad_stage()` | bot.py ~765–785 | Thin wrapper around `scratchpad.run_scratchpad` + hot/cold memory |

---

## bot_stage3_decision.py — Stage 3: prompt builders + Claude callers

| Symbol | Source lines (pre-split) | Notes |
|--------|--------------------------|-------|
| `PROMPTS_DIR` | bot.py ~25 | Path constant |
| `_CAPTURES_DIR` | bot.py ~26 | Path constant |
| `_OVERNIGHT_SYS` | bot.py ~795 | Overnight Haiku system prompt |
| `_OVERNIGHT_DEFAULT` | bot.py ~810 | Safe fallback for overnight failures |
| `_compact_template_cache` | bot.py ~820 | Module-level cache for compact template text |
| `_load_prompts()` | bot.py ~830 | Reads `system_v1.txt` + `user_template_v1.txt` fresh each cycle |
| `_write_decision_capture()` | bot.py ~840 | Writes replay-pack artifact to `data/captures/` |
| `_legacy_action_to_intent()` | bot.py ~870 | Converts legacy action string → intent label |
| `_load_strategy_config()` | bot.py ~885 | Returns `director_notes` from `strategy_config.json` as injected note |
| `build_user_prompt()` | bot.py ~900–1050 | Assembles the full (~3,500-token) FULL cycle prompt |
| `_load_compact_template()` | bot.py ~1055 | Loads and caches `compact_template.txt` |
| `build_compact_prompt()` | bot.py ~1070–1150 | Assembles the COMPACT (~1,500-token) prompt |
| `_log_skip_cycle()` | bot.py ~1155 | Logs gate-skip event at INFO |
| `ask_claude()` | bot.py ~1165–1250 | Main Sonnet call — cached system prompt, returns parsed dict |
| `_ask_claude_overnight()` | bot.py ~1255–1310 | Lightweight Haiku overnight decision |

`ask_claude` and `_ask_claude_overnight` are **re-exported from `bot.py`** so that
`test_core.py`'s `mock.patch.object(self.bot, "ask_claude")` and
`inspect.getsource(bot._ask_claude_overnight)` continue to work unchanged.

---

## bot_stage4_execution.py — Stage 4: pre-execution filters

| Symbol | Source lines (pre-split) | Notes |
|--------|--------------------------|-------|
| `debate_trade()` | bot.py ~1320–1415 | Bull/bear/synthesis 3-call debate |
| `fundamental_check()` | bot.py ~1420–1475 | Single Haiku call for all buy candidates |

Both functions preserve their policy gates verbatim. See `docs/policy_leakage_findings.md`
for the `equity > 26_000` PDT_FLOOR duplication documented in `debate_trade()`.

---

## bot.py — thin orchestrator (what remains)

| Symbol | Notes |
|--------|-------|
| `_send_sms()` | Twilio helper — too coupled to the halt/SMS loop to extract |
| `_DRAWDOWN_THRESHOLD` | Module-level constant |
| `_peak_equity` | Module-level state — drawdown guard owns this |
| `_last_drawdown_alert` | Module-level state |
| `_drawdown_state_loaded` | Module-level state |
| `_DRAWDOWN_STATE_FILE` | Path constant |
| `_load_drawdown_state()` | Reads persisted peak from `data/runtime/drawdown_state.json` |
| `_save_drawdown_state()` | Atomically writes drawdown state |
| `_check_drawdown()` | The guard itself — references module-level globals |
| `run_cycle()` | Orchestrator: calls stages 0→4, attribution, risk kernel, execute |

Re-exports from stage modules (for test compatibility):
- `ask_claude` ← `bot_stage3_decision`
- `_ask_claude_overnight` ← `bot_stage3_decision`

---

## Import graph

```
bot_clients.py          (no bot deps)
    ↑
bot_stage1_regime.py    imports bot_clients
bot_stage2_signal.py    imports bot_clients
bot_stage4_execution.py imports bot_clients
    ↑
bot_stage3_decision.py  imports bot_clients + portfolio_intelligence
    ↑
bot_stage0_precycle.py  imports bot_clients + bot_stage3_decision
    ↑
bot.py                  imports all stage modules + bot_clients
```

No circular dependencies.
