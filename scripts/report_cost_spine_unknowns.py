"""
report_cost_spine_unknowns.py — Cost attribution spine unknown-rate report.

Usage:
    python3 scripts/report_cost_spine_unknowns.py
    python3 scripts/report_cost_spine_unknowns.py --days 7

Reports:
- Total spine records in window
- Unknown rate (module_name="unknown" or enrichment_missing=True)
- Top 10 module names by call count
- Top 10 modules by estimated cost
- Unknown records sample (last 5, for debugging)
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SPINE_PATH = Path("data/analytics/cost_attribution_spine.jsonl")


def _load_records(days_back: int) -> list[dict]:
    if not _SPINE_PATH.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    records = []
    with open(_SPINE_PATH) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ts_str = rec.get("ts", "")
                if ts_str:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if ts < cutoff:
                        continue
                records.append(rec)
            except Exception:
                pass
    return records


def _is_unknown(rec: dict) -> bool:
    return rec.get("module_name", "unknown") == "unknown" or bool(rec.get("enrichment_missing"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Cost attribution spine unknown-rate report")
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days")
    args = parser.parse_args()

    if not _SPINE_PATH.exists():
        print(f"[INFO] Spine file not found: {_SPINE_PATH}")
        print("       Run the bot with enable_cost_attribution_spine=true to start collecting.")
        return 0

    records = _load_records(args.days)

    if not records:
        print(f"[INFO] No spine records in last {args.days} day(s).")
        return 0

    total = len(records)
    unknown_records = [r for r in records if _is_unknown(r)]
    unknown_count = len(unknown_records)
    unknown_rate = unknown_count / total if total else 0.0

    # By call count
    by_calls: dict[str, int] = defaultdict(int)
    by_cost: dict[str, float] = defaultdict(float)
    for rec in records:
        mod = rec.get("module_name", "unknown")
        by_calls[mod] += 1
        by_cost[mod] += float(rec.get("estimated_cost_usd") or 0.0)

    top_calls = sorted(by_calls.items(), key=lambda kv: -kv[1])[:10]
    top_cost = sorted(by_cost.items(), key=lambda kv: -kv[1])[:10]
    total_cost = sum(by_cost.values())

    print(f"\n{'─'*60}")
    print(f"  Cost Attribution Spine — last {args.days} day(s)")
    print(f"{'─'*60}")
    print(f"  Total records : {total}")
    print(f"  Unknown count : {unknown_count}  ({unknown_rate:.1%})")
    print(f"  Total cost    : ${total_cost:.4f}")
    print()

    print(f"  {'Module':<40} {'Calls':>6}")
    print(f"  {'─'*40} {'─'*6}")
    for mod, cnt in top_calls:
        marker = " ⚠" if mod == "unknown" else ""
        print(f"  {mod:<40} {cnt:>6}{marker}")

    print()
    print(f"  {'Module':<40} {'Cost USD':>10}")
    print(f"  {'─'*40} {'─'*10}")
    for mod, cost in top_cost:
        marker = " ⚠" if mod == "unknown" else ""
        print(f"  {mod:<40} ${cost:>9.4f}{marker}")

    if unknown_records:
        print()
        print(f"  Last {min(5, len(unknown_records))} unknown record(s) (for debugging):")
        print(f"  {'─'*60}")
        for rec in unknown_records[-5:]:
            ts = rec.get("ts", "?")[:19]
            purpose = rec.get("purpose", "?")
            model = rec.get("model", "?")
            print(f"    {ts}  purpose={purpose:<25} model={model}")

    print(f"{'─'*60}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
