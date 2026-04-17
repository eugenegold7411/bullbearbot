"""
inspect_shadow_diffs.py — Inspect semantic router shadow log diffs.

Usage:
    python3 scripts/inspect_shadow_diffs.py
    python3 scripts/inspect_shadow_diffs.py --mismatches-only
    python3 scripts/inspect_shadow_diffs.py --cycle-id dec_A1_20260419_123456
    python3 scripts/inspect_shadow_diffs.py --last N
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SHADOW_LOG = Path("data/analytics/router_shadow_log.jsonl")
_CONFIG = Path("strategy_config.json")

_NOT_ENABLED_MSG = (
    "\n  Semantic router shadow not yet enabled.\n"
    "  Set enable_semantic_router_shadow=true in strategy_config.json to start collecting.\n"
)


def _flag_enabled() -> bool:
    try:
        cfg = json.loads(_CONFIG.read_text())
        return bool(
            cfg.get("shadow_flags", {}).get("enable_semantic_router_shadow", False)
            or cfg.get("feature_flags", {}).get("enable_semantic_router_shadow", False)
        )
    except Exception:
        return False


def _load_records(last_n: int | None) -> list[dict]:
    if not _SHADOW_LOG.exists():
        return []
    records = []
    with open(_SHADOW_LOG) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                pass
    if last_n is not None:
        records = records[-last_n:]
    return records


def _print_summary(records: list[dict], mismatches_only: bool) -> None:
    fmt = "  {:<12} {:<28} {:<10} {:<10} {:<8} {}"
    print(fmt.format("date", "cycle_id", "gate", "router", "diverged", "reason"))
    print("  " + "─" * 90)
    for rec in records:
        diverged = bool(rec.get("diverged", False))
        if mismatches_only and not diverged:
            continue
        ts = str(rec.get("ts", rec.get("timestamp", "?")))[:10]
        cid = str(rec.get("cycle_id", "?"))[:28]
        gate = str(rec.get("gate_decision", "?"))[:10]
        router = str(rec.get("router_decision", "?"))[:10]
        div_flag = "YES" if diverged else "no"
        reason = str(rec.get("divergence_reason", ""))[:40]
        print(fmt.format(ts, cid, gate, router, div_flag, reason))


def _print_detail(rec: dict) -> None:
    print()
    for k, v in rec.items():
        val = str(v)
        if len(val) > 300:
            val = val[:300] + "... [truncated]"
        print(f"  {k:<35} {val}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect semantic router shadow log")
    parser.add_argument("--mismatches-only", action="store_true")
    parser.add_argument("--cycle-id", dest="cycle_id")
    parser.add_argument("--last", type=int, metavar="N")
    args = parser.parse_args()

    if not _flag_enabled() and not _SHADOW_LOG.exists():
        print(_NOT_ENABLED_MSG)
        return 0

    records = _load_records(args.last)

    if not records:
        if not _flag_enabled():
            print(_NOT_ENABLED_MSG)
        else:
            print("\n  No router shadow records yet. Flag is enabled — records will appear after cycles run.\n")
        return 0

    if args.cycle_id:
        matches = [r for r in records if r.get("cycle_id") == args.cycle_id]
        if not matches:
            print(f"\n  cycle_id {args.cycle_id!r} not found.\n")
            return 1
        for rec in matches:
            _print_detail(rec)
        return 0

    n_diverged = sum(1 for r in records if r.get("diverged"))
    print(f"\n  Router Shadow Log — {len(records)} records, {n_diverged} diverged\n")
    _print_summary(records, args.mismatches_only)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
