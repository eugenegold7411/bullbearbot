# Prompts

The prompt files in this directory are kept private — they encode the trading strategy and
decision logic that took months of iteration to develop.

## Structure

### system_v1.txt — A1 system prompt (~4,000 tokens)
The core instruction set for the Sonnet decision agent. Covers:
- Role definition and decision authority
- Position sizing philosophy (tier system: CORE / DYNAMIC / INTRADAY)
- Risk rules (stop placement, trailing stop tiers, binary event handling)
- Conviction scoring framework (HIGH / MEDIUM / LOW / AVOID)
- Earnings event handling (explicit reasoning required before binary events)
- Near-close gate behavior
- Output schema (structured JSON ideas format)

### user_template_v1.txt — Per-cycle user prompt template
Injected fresh every decision cycle with live data. Contains:
- `{conviction_table}` — reconciled conviction from brief + signal scores + scratchpad
- `{regime_line}` — current market regime and VIX
- `{positions_line}` — current open positions with P&L
- `{signal_scores}` — top scored symbols this cycle
- `{scratchpad_section}` — pre-analysis from scratchpad agent
- `{earnings_intel}` — EDGAR transcript excerpts for relevant symbols
- `{macro_wire}` — breaking macro alerts
- `{qualitative_context}` — per-symbol qualitative synthesis

### system_options_v1.txt — A2 system prompt
Instruction set for the options decision agent. Covers:
- 12 routing rules (RULE1 through RULE_POST_EARNINGS)
- 4-agent debate structure (Bull, Bear, IV Analyst, Structure Judge)
- IV rank thresholds for each strategy type
- Greeks constraints and risk limits

### compact_template.txt — Compressed overnight prompt
Stripped-down version used during extended hours when the Sonnet gate is suppressed.
Token budget: ~350 tokens vs ~13,000 for the full template.

## Why private?
The prompts represent the actual edge of the system — not the architecture (which is public),
but the specific instructions that took 30+ days of paper trading to calibrate.
If you're building something similar, the architecture in the codebase is the starting point.
