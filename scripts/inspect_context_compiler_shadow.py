"""
inspect_context_compiler_shadow.py — Inspect context compiler shadow log.

Usage:
    python3 scripts/inspect_context_compiler_shadow.py
    python3 scripts/inspect_context_compiler_shadow.py --section sector_news
    python3 scripts/inspect_context_compiler_shadow.py --cycle-id dec_A1_...
    python3 scripts/inspect_context_compiler_shadow.py --last N
    python3 scripts/inspect_context_compiler_shadow.py --show-content
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SHADOW_LOG = Path("data/analytics/context_compiler_shadow.jsonl")
_CONFIG = Path("strategy_config.json")
_TRUNCATE_AT = 500

_NOT_ENABLED_MSG = (
    "\n  Context compiler shadow not yet enabled.\n"
    "  Set enable_context_compressor_shadow=true in strategy_config.json.\n"
)


def _flag_enabled() -> bool:
    try:
        cfg = json.loads(_CONFIG.read_text())
        return bool(
            cfg.get("shadow_flags", {}).get("enable_context_compressor_shadow", False)
            or cfg.get("feature_flags", {}).get("enable_context_compressor_shadow", False)
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


def _trunc(s: str, n: int = _TRUNCATE_AT) -> str:
    if len(s) <= n:
        return s
    return s[:n] + f" ... [truncated {len(s) - n} chars]"


def _print_summary(records: list[dict]) -> None:
    fmt = "  {:<10} {:<28} {:<20} {:>9} {:>13} {:>7} {:>9}"
    print(fmt.format("date", "cycle_id", "section", "raw_chars", "comp_chars", "ratio", "cost_usd"))
    print("  " + "─" * 100)
    for rec in records:
        ts = str(rec.get("ts", rec.get("timestamp", "?")))[:10]
        cid = str(rec.get("cycle_id", "?"))[:28]
        section = str(rec.get("section", "?"))[:20]
        raw = int(rec.get("raw_chars", 0))
        comp = int(rec.get("compressed_chars", 0))
        ratio = f"{comp/raw:.2f}x" if raw > 0 else "n/a"
        cost = float(rec.get("cost_usd", 0.0))
        print(fmt.format(ts, cid, section, raw, comp, ratio, f"${cost:.5f}"))


def _print_content_view(records: list[dict]) -> None:
    for rec in records:
        print(f"\n  {'─'*70}")
        cid = rec.get("cycle_id", "?")
        section = rec.get("section", "?")
        print(f"  cycle: {cid}  section: {section}")
        raw = str(rec.get("raw_content", ""))
        comp = str(rec.get("compressed_content", ""))
        print(f"\n  [RAW {len(raw)} chars]\n  {_trunc(raw, 300)}")
        print(f"\n  [COMPRESSED {len(comp)} chars]\n  {_trunc(comp, 300)}")
        ratio = f"{len(comp)/len(raw):.2f}x" if len(raw) > 0 else "n/a"
        print(f"\n  Compression ratio: {ratio}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect context compiler shadow log")
    parser.add_argument("--section")
    parser.add_argument("--cycle-id", dest="cycle_id")
    parser.add_argument("--last", type=int, metavar="N")
    parser.add_argument("--show-content", action="store_true")
    args = parser.parse_args()

    if not _flag_enabled() and not _SHADOW_LOG.exists():
        print(_NOT_ENABLED_MSG)
        return 0

    records = _load_records(args.last)

    if not records:
        if not _flag_enabled():
            print(_NOT_ENABLED_MSG)
        else:
            print("\n  No context compiler shadow records yet.\n")
        return 0

    if args.section:
        records = [r for r in records if r.get("section") == args.section]
    if args.cycle_id:
        records = [r for r in records if r.get("cycle_id") == args.cycle_id]

    if not records:
        print("\n  No records match your filters.\n")
        return 0

    total_raw = sum(int(r.get("raw_chars", 0)) for r in records)
    total_comp = sum(int(r.get("compressed_chars", 0)) for r in records)
    total_cost = sum(float(r.get("cost_usd", 0.0)) for r in records)
    avg_ratio = f"{total_comp/total_raw:.2f}x" if total_raw > 0 else "n/a"

    print(f"\n  Context Compiler Shadow — {len(records)} records")
    print(f"  Total raw: {total_raw:,} chars → compressed: {total_comp:,} chars  (avg ratio: {avg_ratio})")
    print(f"  Total cost: ${total_cost:.5f}\n")

    if args.show_content:
        _print_content_view(records)
    else:
        _print_summary(records)

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
