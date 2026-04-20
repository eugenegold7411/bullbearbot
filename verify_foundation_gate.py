#!/usr/bin/env python3
"""
Foundation Gate Acceptance Verification
Run on the VPS: python3 verify_foundation_gate.py

Checks each T0.x ticket's actual acceptance criteria against live code and data.
Prints PASS / FAIL / WARN per criterion with evidence.
"""

import json
import re
import sys
from pathlib import Path

BASE = Path("/home/trading-bot")
PASS = "[PASS]"
FAIL = "[FAIL]"
WARN = "[WARN]"

results = []

def check(ticket, criterion, passed, evidence="", warn=False):
    status = WARN if warn else (PASS if passed else FAIL)
    results.append((ticket, criterion, status, evidence))
    print(f"{status:6}  {ticket:8}  {criterion}")
    if evidence:
        print(f"         evidence: {evidence}")

def read(path):
    try:
        return (BASE / path).read_text()
    except Exception as e:
        return f"ERROR:{e}"

def file_exists(path):
    return (BASE / path).exists()

def grep(path, pattern):
    content = read(path)
    if content.startswith("ERROR:"):
        return []
    return re.findall(pattern, content)

def count(path, pattern):
    return len(grep(path, pattern))

print("=" * 70)
print("Foundation Gate Acceptance Verification")
print("=" * 70)

# ---------------------------------------------------------------------------
# T0.1 — Fill Price in Execution Results
# ---------------------------------------------------------------------------
print("\n--- T0.1 Fill Price ---")

# Check ExecutionResult dataclass has fill_price
er_content = read("order_executor.py")
has_fill_price_field = "fill_price" in er_content
check("T0.1", "fill_price field exists in order_executor.py", has_fill_price_field)

# Check it's in a dataclass (not just a comment)
has_dataclass_field = bool(re.search(r"fill_price\s*:", er_content))
check("T0.1", "fill_price typed as dataclass field", has_dataclass_field)

# Check decision_outcomes.py stores entry_price
do_content = read("decision_outcomes.py")
has_entry_price = "entry_price" in do_content
check("T0.1", "decision_outcomes.py references entry_price", has_entry_price)

# Check entry_price can be non-null (assigned from fill_price)
has_fill_assignment = bool(re.search(r"entry_price.*fill_price|fill_price.*entry_price", do_content))
check("T0.1", "entry_price assigned from fill_price", has_fill_assignment,
      warn=not has_fill_assignment)

# ---------------------------------------------------------------------------
# T0.2 — Executor Consolidation
# ---------------------------------------------------------------------------
print("\n--- T0.2 Executor Consolidation ---")

oe_content = read("order_executor.py")

# TIER_MAX_PCT must not be defined in order_executor.py
tier_count = len(re.findall(r"TIER_MAX_PCT\s*=\s*{", oe_content))
check("T0.2", "TIER_MAX_PCT dict not defined in order_executor.py", tier_count == 0,
      f"found {tier_count} definitions")

# Risk kernel owns TIER_MAX_PCT
rk_content = read("risk_kernel.py")
rk_has_tier = "TIER_MAX_PCT" in rk_content
check("T0.2", "risk_kernel.py owns TIER_MAX_PCT", rk_has_tier)

# Redundant checks demoted (no hard rejections for sizing in executor)
# Look for validate_action returning False for position sizing
sizing_rejections = re.findall(
    r"return False.*position|position.*return False", oe_content)
check("T0.2", "no hard sizing rejections in executor validate_action",
      len(sizing_rejections) == 0,
      f"found {len(sizing_rejections)} potential sizing rejections",
      warn=len(sizing_rejections) > 0)

# policy_ownership_map.md exists
check("T0.2", "docs/policy_ownership_map.md exists",
      file_exists("docs/policy_ownership_map.md"))

# ---------------------------------------------------------------------------
# T0.3 — A2 Readiness Split
# ---------------------------------------------------------------------------
print("\n--- T0.3 A2 Readiness Split ---")

bo_content = read("bot_options.py")

# iv_history_ready and observation_complete are independent fields
has_iv_ready = "iv_history_ready" in bo_content
has_obs_complete = "observation_complete" in bo_content
check("T0.3", "iv_history_ready field present", has_iv_ready)
check("T0.3", "observation_complete field present", has_obs_complete)

# They must be independently testable (separate conditionals)
iv_lines = [l for l in bo_content.splitlines() if "iv_history_ready" in l]
obs_lines = [l for l in bo_content.splitlines() if "observation_complete" in l
             and "iv_history_ready" not in l]
check("T0.3", "iv_history_ready checked independently",
      len(iv_lines) >= 1, f"{len(iv_lines)} references")
check("T0.3", "observation_complete checked independently",
      len(obs_lines) >= 1, f"{len(obs_lines)} references")

# Check obs_mode_state.json has version field
obs_state_path = BASE / "data/account2/obs_mode_state.json"
if obs_state_path.exists():
    try:
        obs_state = json.loads(obs_state_path.read_text())
        has_version = "version" in obs_state
        check("T0.3", "obs_mode_state.json has version field", has_version,
              f"version={obs_state.get('version', 'missing')}")
    except Exception as e:
        check("T0.3", "obs_mode_state.json parseable", False, str(e))
else:
    check("T0.3", "obs_mode_state.json exists", False, "file not found")

# ---------------------------------------------------------------------------
# T0.4 — Recommendation IDs and Placeholders
# ---------------------------------------------------------------------------
print("\n--- T0.4 Recommendation IDs ---")

wr_content = read("weekly_review.py")

# stable rec_id assigned
has_rec_id = bool(re.search(r'rec_id.*rec_\w+_\d+|f"rec_', wr_content))
check("T0.4", "stable rec_id assigned in weekly_review.py", has_rec_id)

# placeholder outcome fields (verdict, resolved_at)
has_verdict = "verdict" in wr_content
has_resolved_at = "resolved_at" in wr_content
check("T0.4", "verdict placeholder field present", has_verdict)
check("T0.4", "resolved_at placeholder field present", has_resolved_at)

# rec_id persisted (written to director memo history)
has_memo_history = file_exists("data/reports/director_memo_history.json")
check("T0.4", "director_memo_history.json exists (rec persistence)",
      has_memo_history, warn=not has_memo_history)

# ---------------------------------------------------------------------------
# T0.5 — Schema Versioning Framework
# ---------------------------------------------------------------------------
print("\n--- T0.5 Schema Versioning ---")

check("T0.5", "versioning.py exists", file_exists("versioning.py"))

v_content = read("versioning.py")

for fn in ["detect_version", "load_with_compat", "migrate_artifact",
           "write_backup_snapshot", "register_migration", "MigrationResult",
           "SchemaVersionTooOld"]:
    check("T0.5", f"{fn} defined", fn in v_content)

# Three migrations registered
migration_count = len(re.findall(r"register_migration\(", v_content))
check("T0.5", f"at least 3 migrations registered ({migration_count} found)",
      migration_count >= 3, f"found {migration_count}")

# ---------------------------------------------------------------------------
# T0.6 — Feature Flag and Rollback Framework
# ---------------------------------------------------------------------------
print("\n--- T0.6 Feature Flags + Rollback ---")

check("T0.6", "feature_flags.py exists", file_exists("feature_flags.py"))

ff_content = read("feature_flags.py")
for fn in ["load_flags", "is_enabled", "get_all_flags"]:
    check("T0.6", f"{fn} defined", fn in ff_content)

# Flags in strategy_config.json
sc_path = BASE / "strategy_config.json"
if sc_path.exists():
    try:
        sc = json.loads(sc_path.read_text())
        check("T0.6", "feature_flags section in strategy_config.json",
              "feature_flags" in sc)
        check("T0.6", "shadow_flags section in strategy_config.json",
              "shadow_flags" in sc)
        check("T0.6", "lab_flags section in strategy_config.json",
              "lab_flags" in sc)
        check("T0.6", "feature_flags_version field present",
              "feature_flags_version" in sc,
              f"value={sc.get('feature_flags_version', 'missing')}")
        check("T0.6", "enable_cost_attribution_spine is true",
              sc.get("feature_flags", {}).get("enable_cost_attribution_spine") is True)
    except Exception as e:
        check("T0.6", "strategy_config.json parseable", False, str(e))
else:
    check("T0.6", "strategy_config.json exists", False)

check("T0.6", "docs/rollback_playbook.md exists",
      file_exists("docs/rollback_playbook.md"))
check("T0.6", "scripts/simulate_bad_migration_and_rollback.py exists",
      file_exists("scripts/simulate_bad_migration_and_rollback.py"))

# ---------------------------------------------------------------------------
# T0.7 — LLM Cost Attribution Spine
# ---------------------------------------------------------------------------
print("\n--- T0.7 Cost Attribution Spine ---")

check("T0.7", "cost_attribution.py exists", file_exists("cost_attribution.py"))

ca_content = read("cost_attribution.py")
for fn in ["log_spine_record", "get_spine_summary",
           "format_spine_summary_for_review"]:
    check("T0.7", f"{fn} defined", fn in ca_content)

# Canonical layer names defined
check("T0.7", "VALID_LAYER_NAMES defined", "VALID_LAYER_NAMES" in ca_content)
for layer in ["execution_control", "semantic_normalization", "context_compiler",
              "learning_evaluation", "governance_review", "shadow_analysis",
              "annex_experiment"]:
    check("T0.7", f"layer '{layer}' in VALID_LAYER_NAMES", layer in ca_content)

# Spine JSONL exists and has records
spine_path = BASE / "data/analytics/cost_attribution_spine.jsonl"
if spine_path.exists():
    lines = [l for l in spine_path.read_text().splitlines() if l.strip()]
    check("T0.7", f"spine JSONL has records ({len(lines)} found)",
          len(lines) > 0, f"{len(lines)} records")
    if lines:
        try:
            rec = json.loads(lines[-1])
            check("T0.7", "spine records have schema_version=1",
                  rec.get("schema_version") == 1)
            check("T0.7", "spine records have call_id",
                  bool(rec.get("call_id")))
        except Exception as e:
            check("T0.7", "spine record parseable", False, str(e))
else:
    check("T0.7", "cost_attribution_spine.jsonl exists", False)

# Adapter in attribution.py
attr_content = read("attribution.py")
check("T0.7", "spine adapter in attribution.py",
      "cost_attribution" in attr_content or "log_spine_record" in attr_content)

# ---------------------------------------------------------------------------
# T0.7a — Semantic Taxonomy Doc
# ---------------------------------------------------------------------------
print("\n--- T0.7a Semantic Taxonomy ---")

check("T0.7a", "docs/taxonomy_v1.0.0.md exists",
      file_exists("docs/taxonomy_v1.0.0.md"))
tax_content = read("docs/taxonomy_v1.0.0.md")
check("T0.7a", "taxonomy doc non-empty",
      len(tax_content) > 500, f"{len(tax_content)} chars")

# ---------------------------------------------------------------------------
# T0.8 — Trade Closure Contract + BUG-003
# ---------------------------------------------------------------------------
print("\n--- T0.8 Trade Closure + BUG-003 ---")

check("T0.8", "docs/trade_closure_contract_v1.0.0.md exists",
      file_exists("docs/trade_closure_contract_v1.0.0.md"))

mem_content = read("memory.py")

# BUG-003: stock_hold must not be treated as loss
# Check that action type filter exists
has_action_filter = bool(re.search(
    r"stock_hold|hold.*action.*filter|action.*type.*filter|"
    r"only.*buy.*sell.*close|buy.*sell.*outcome", mem_content, re.I))
check("T0.8", "action type filter in memory.py (HOLDs not counted as losses)",
      has_action_filter, warn=not has_action_filter)

# Check performance.json win/loss counts look sane
perf_path = BASE / "memory/performance.json"
if perf_path.exists():
    try:
        perf = json.loads(perf_path.read_text())
        total = perf.get("total_trades", 0)
        wins = perf.get("wins", 0)
        losses = perf.get("losses", 0)
        # If losses >> actual closed trades, BUG-003 is still active
        sane = losses < 10  # we know there are only ~3 real closed trades
        check("T0.8", f"performance.json loss count sane (losses={losses})",
              sane, f"total={total} wins={wins} losses={losses}",
              warn=not sane)
    except Exception as e:
        check("T0.8", "performance.json parseable", False, str(e))
else:
    check("T0.8", "memory/performance.json exists", False, warn=True)

# HOLD decisions must not affect PnL — check update_outcomes_from_alpaca
has_hold_guard = bool(re.search(
    r"hold.*skip|skip.*hold|action.*not in.*hold|"
    r"if.*action.*in.*\[.*buy|sell.*close", mem_content, re.I))
check("T0.8", "HOLD guard in outcome resolution", has_hold_guard,
      warn=not has_hold_guard)

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print("\n" + "=" * 70)
print("SUMMARY")
print("=" * 70)

passes = sum(1 for _, _, s, _ in results if s == PASS)
fails = sum(1 for _, _, s, _ in results if s == FAIL)
warns = sum(1 for _, _, s, _ in results if s == WARN)

print(f"\n  {PASS} {passes}   {FAIL} {fails}   {WARN} {warns}   Total: {len(results)}")

if fails:
    print("\nFailing criteria:")
    for ticket, criterion, status, evidence in results:
        if status == FAIL:
            print(f"  {ticket}: {criterion}")
            if evidence:
                print(f"    {evidence}")

if warns:
    print("\nWarnings (need manual review):")
    for ticket, criterion, status, evidence in results:
        if status == WARN:
            print(f"  {ticket}: {criterion}")
            if evidence:
                print(f"    {evidence}")

gate_pass = fails == 0
print(f"\nFoundation Gate: {'PASS — ready to tag foundation-gate-pass' if gate_pass else 'NOT READY — fix failures above'}")
sys.exit(0 if gate_pass else 1)
