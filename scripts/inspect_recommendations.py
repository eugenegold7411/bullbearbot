"""
inspect_recommendations.py — View director recommendation history.

Usage:
    python3 scripts/inspect_recommendations.py
    python3 scripts/inspect_recommendations.py --week 2026-04-19
    python3 scripts/inspect_recommendations.py --status pending
    python3 scripts/inspect_recommendations.py --status resolved
    python3 scripts/inspect_recommendations.py --rec-id rec_20260419_1
    python3 scripts/inspect_recommendations.py --format json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_MEMO_HISTORY = Path("data/reports/director_memo_history.json")
_REC_STORE = Path("data/reports/recommendation_store.json")


def _load_all_recommendations() -> list[dict]:
    recs: list[dict] = []

    # Primary: director_memo_history.json
    if _MEMO_HISTORY.exists():
        try:
            history = json.loads(_MEMO_HISTORY.read_text())
            for week_entry in history.get("weeks", []):
                week = week_entry.get("week", "")
                for rec in week_entry.get("recommendations", []):
                    rec.setdefault("week", week)
                    recs.append(rec)
        except Exception as exc:
            print(f"[WARN] Could not read {_MEMO_HISTORY}: {exc}", file=sys.stderr)

    # Secondary: recommendation_store.json
    if _REC_STORE.exists():
        try:
            store = json.loads(_REC_STORE.read_text())
            seen_ids = {r.get("rec_id") for r in recs}
            for rec in store.get("recommendations", {}).values():
                if rec.get("rec_id") not in seen_ids:
                    recs.append(rec)
        except Exception as exc:
            print(f"[WARN] Could not read {_REC_STORE}: {exc}", file=sys.stderr)

    return recs


def _verdict_icon(status: str, verdict: str) -> str:
    if status == "pending" or not verdict:
        return "⏳"
    if verdict == "accepted":
        return "✅"
    if verdict == "rejected":
        return "❌"
    return "➖"


def _print_table(recs: list[dict]) -> None:
    if not recs:
        print("  (no recommendations)")
        return
    fmt = "  {:<22} {:<12} {:<10} {:<10} {:<60}"
    print(fmt.format("rec_id", "week", "status", "verdict", "recommendation"))
    print("  " + "─" * 114)
    for rec in recs:
        rec_id = rec.get("rec_id", "?")
        week = str(rec.get("week", "?"))[:12]
        status = str(rec.get("status", "pending"))[:10]
        verdict = str(rec.get("verdict", ""))[:10]
        icon = _verdict_icon(status, verdict)
        text = str(rec.get("text", rec.get("recommendation", "")))[:60]
        print(fmt.format(rec_id, week, f"{icon} {status}", verdict, text))


def _print_detail(rec: dict) -> None:
    print()
    for k, v in rec.items():
        val = str(v)
        if len(val) > 200:
            val = val[:200] + "... [truncated]"
        print(f"  {k:<30} {val}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect director recommendation history")
    parser.add_argument("--week", help="Filter by week string (e.g. 2026-04-19)")
    parser.add_argument("--status", choices=["pending", "resolved", "accepted", "rejected"])
    parser.add_argument("--rec-id", dest="rec_id", help="Show full detail for one rec_id")
    parser.add_argument("--format", choices=["table", "json"], default="table")
    args = parser.parse_args()

    if not _MEMO_HISTORY.exists() and not _REC_STORE.exists():
        print("\n  No recommendations found yet.")
        print("  Run the weekly review first: python3 scripts/run_weekly_review.py\n")
        return 0

    recs = _load_all_recommendations()

    if not recs:
        print("\n  No recommendations found yet. Run weekly review first.\n")
        return 0

    # Filter
    if args.week:
        recs = [r for r in recs if str(r.get("week", "")).startswith(args.week)]
    if args.status:
        if args.status == "resolved":
            recs = [r for r in recs if r.get("status") in ("accepted", "rejected")]
        else:
            recs = [r for r in recs if r.get("status") == args.status]
    if args.rec_id:
        matches = [r for r in recs if r.get("rec_id") == args.rec_id]
        if not matches:
            print(f"\n  rec_id {args.rec_id!r} not found.\n")
            return 1
        for rec in matches:
            _print_detail(rec)
        return 0

    if args.format == "json":
        print(json.dumps(recs, indent=2))
        return 0

    print(f"\n  Director Recommendations ({len(recs)} total)\n")
    _print_table(recs)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
