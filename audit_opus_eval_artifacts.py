#!/usr/bin/env python3
"""
Opus Eval Artifact Availability Audit
Run on the VPS: python3 audit_opus_eval_artifacts.py

Checks what data actually exists for each artifact class so we know
which artifacts can be honestly captured vs which must be excluded.
"""

import json
from pathlib import Path

BASE = Path("/home/trading-bot")

def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

def read_json(path):
    try:
        return json.loads((BASE / path).read_text())
    except Exception:
        return None

def read_jsonl(path, limit=None):
    try:
        lines = (BASE / path).read_text().strip().splitlines()
        records = []
        for line in lines:
            try:
                records.append(json.loads(line))
            except:
                pass
        return records[-limit:] if limit else records
    except:
        return []

# ---------------------------------------------------------------------------
# CLASS 1: Forensic Cases (closed A1 trades)
# ---------------------------------------------------------------------------
section("CLASS 1: Forensic Cases — Closed A1 Trades")

decisions = read_json("memory/decisions.json")
if not decisions:
    print("  [MISSING] memory/decisions.json not found")
else:
    all_decisions = decisions if isinstance(decisions, list) else decisions.get("decisions", [])
    print(f"  Total decisions in log: {len(all_decisions)}")

    # Find closed trades (buy + subsequent close/sell)
    buys = [d for d in all_decisions if any(
        i.get("intent") in ("enter_long", "enter_short") or
        a.get("action") in ("buy", "enter_long")
        for i in d.get("ideas", [])
        for a in [i]
    )]

    # Check trades.jsonl for actual fills
    trades = read_jsonl("logs/trades.jsonl")
    buys_filled = [t for t in trades if t.get("action") in ("buy", "enter_long", "sell", "close")]
    symbols_traded = list(set(t.get("symbol") for t in buys_filled if t.get("symbol")))

    print(f"  Trades in logs/trades.jsonl: {len(trades)}")
    print(f"  Symbols with fills: {symbols_traded}")

    # Find the BTC win and ETH loss specifically
    btc_trades = [t for t in trades if "BTC" in str(t.get("symbol", ""))]
    eth_trades = [t for t in trades if "ETH" in str(t.get("symbol", ""))]
    tsm_trades = [t for t in trades if "TSM" in str(t.get("symbol", ""))]

    print(f"\n  BTC trades: {len(btc_trades)}")
    for t in btc_trades[:3]:
        print(f"    {t.get('timestamp','?')[:19]} {t.get('action','?')} {t.get('symbol','?')} qty={t.get('qty','?')} price={t.get('price','?')}")

    print(f"\n  ETH trades: {len(eth_trades)}")
    for t in eth_trades[:3]:
        print(f"    {t.get('timestamp','?')[:19]} {t.get('action','?')} {t.get('symbol','?')} qty={t.get('qty','?')} price={t.get('price','?')}")

    print(f"\n  TSM trades: {len(tsm_trades)}")
    for t in tsm_trades[:5]:
        print(f"    {t.get('timestamp','?')[:19]} {t.get('action','?')} {t.get('symbol','?')} qty={t.get('qty','?')} price={t.get('price','?')}")

    # Check performance.json for closed trade summary
    perf = read_json("memory/performance.json")
    if perf:
        print(f"\n  performance.json: wins={perf.get('wins',0)} losses={perf.get('losses',0)} total={perf.get('total_trades',0)}")

    # Check decision_outcomes for closed records
    outcomes = read_jsonl("data/analytics/decision_outcomes.jsonl")
    closed = [o for o in outcomes if o.get("status") not in ("pending", "submitted", None)]
    print(f"\n  decision_outcomes.jsonl: {len(outcomes)} total, {len(closed)} with resolved status")

# ---------------------------------------------------------------------------
# CLASS 2: Weekly Review Synthesis Cases
# ---------------------------------------------------------------------------
section("CLASS 2: Weekly Review Synthesis Cases")

reports_dir = BASE / "data/reports"
if reports_dir.exists():
    weekly_reports = sorted(reports_dir.glob("weekly_review_*.md"))
    print(f"  Weekly review reports found: {len(weekly_reports)}")
    for r in weekly_reports:
        size = r.stat().st_size
        print(f"    {r.name} ({size} bytes)")
else:
    print("  [MISSING] data/reports/ directory not found")

# Check if Agent 1-5 outputs are preserved anywhere
print("\n  Agent output preservation check:")
for fname in ["data/reports/agent_outputs", "data/reports/weekly_review_latest.json",
              "data/reports/director_memo_history.json"]:
    path = BASE / fname
    exists = path.exists()
    print(f"    {fname}: {'EXISTS' if exists else 'missing'}")
    if exists and fname.endswith(".json"):
        try:
            data = json.loads(path.read_text())
            if isinstance(data, list):
                print(f"      {len(data)} entries")
            elif isinstance(data, dict):
                print(f"      keys: {list(data.keys())[:5]}")
        except:
            pass

# ---------------------------------------------------------------------------
# CLASS 3: Director Recommendations (resolved verdicts only)
# ---------------------------------------------------------------------------
section("CLASS 3: Director Recommendations — Resolved Verdicts Only")

memo_history = read_json("data/reports/director_memo_history.json")
if not memo_history:
    print("  [MISSING] director_memo_history.json not found or empty")
    print("  This file is created on first weekly review run.")
    print("  RESULT: 0 resolved recommendations available — class excluded from eval")
else:
    all_recs = []
    for memo in (memo_history if isinstance(memo_history, list) else [memo_history]):
        recs = memo.get("recommendations", [])
        all_recs.extend(recs)

    print(f"  Total recommendations across all memos: {len(all_recs)}")
    resolved = [r for r in all_recs if r.get("verdict") not in ("pending", None, "")]
    pending = [r for r in all_recs if r.get("verdict") in ("pending", None, "")]
    print(f"  Resolved: {len(resolved)}  Pending: {len(pending)}")

    for r in resolved[:5]:
        print(f"    [{r.get('verdict','?')}] {r.get('rec_id','?')}: {str(r.get('recommendation_text',''))[:80]}")

# Check recommendation_store
rec_store_path = BASE / "data/reports/recommendation_store.json"
if rec_store_path.exists():
    try:
        store = json.loads(rec_store_path.read_text())
        resolved_store = [v for v in store.values() if v.get("verdict") not in ("pending", None)]
        print(f"\n  recommendation_store.json: {len(store)} total, {len(resolved_store)} resolved")
    except:
        print("\n  recommendation_store.json: exists but not parseable")
else:
    print("\n  recommendation_store.json: not found")

# ---------------------------------------------------------------------------
# CLASS 4: A2 Observation-Mode Debates
# ---------------------------------------------------------------------------
section("CLASS 4: A2 Observation-Mode Debates")

a2_decisions = read_json("data/account2/trade_memory/decisions_account2.json")
if not a2_decisions:
    print("  [MISSING] decisions_account2.json not found")
else:
    decisions_list = a2_decisions if isinstance(a2_decisions, list) else a2_decisions.get("decisions", [])
    obs_decisions = [d for d in decisions_list if d.get("status") == "observation"
                     or d.get("observation_mode") == True]
    full_debates = [d for d in obs_decisions if d.get("debate_output") or d.get("synthesis")]

    print(f"  Total A2 decisions: {len(decisions_list)}")
    print(f"  Observation-mode decisions: {len(obs_decisions)}")
    print(f"  With full debate output: {len(full_debates)}")

    for d in full_debates[:2]:
        ts = d.get("timestamp", d.get("created_at", "?"))[:19]
        symbol = d.get("symbol", "?")
        print(f"    {ts} {symbol}")

# ---------------------------------------------------------------------------
# SUMMARY
# ---------------------------------------------------------------------------
section("ARTIFACT AVAILABILITY SUMMARY")

print("""
  CLASS 1 — Forensic (closed trades)
    BTC win: check above for confirmed buy+sell pair
    ETH loss: check above for confirmed buy+sell pair
    STATUS: depends on trades.jsonl entries above

  CLASS 2 — Weekly Review Synthesis
    STATUS: depends on weekly_review_*.md files above
    NOTE: Agent 1-5 outputs must be preserved to reconstruct Agent 6 prompt
    If no reports exist yet: CLASS 2 EXCLUDED

  CLASS 3 — Director Recommendations (resolved only)
    STATUS: depends on director_memo_history.json above
    NOTE: First weekly review hasn't run yet (launched 2026-04-13)
    If no resolved recs: CLASS 3 EXCLUDED

  CLASS 4 — A2 Observation Debates
    STATUS: depends on decisions_account2.json above
    NOTE: judgment-only, promotion_evidence=false

  MINIMUM VIABLE SET: 6 artifacts to proceed
  If classes 2+3 are empty: only forensic + A2 cases available
  May need to DEFER experiment until after Sunday's weekly review
""")
