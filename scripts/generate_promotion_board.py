"""
generate_promotion_board.py — Build and display the annex promotion board.

Usage:
    python3 scripts/generate_promotion_board.py
    python3 scripts/generate_promotion_board.py --format markdown
    python3 scripts/generate_promotion_board.py --no-write
"""
from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate annex promotion board")
    parser.add_argument("--format", choices=["json", "markdown", "table"], default="table")
    parser.add_argument("--no-write", action="store_true", dest="no_write")
    args = parser.parse_args()

    try:
        from promotion_board import build_promotion_board, save_promotion_board, format_promotion_board_for_review  # noqa: PLC0415
    except ImportError as exc:
        print(f"[ERROR] Cannot import promotion_board: {exc}", file=sys.stderr)
        return 1

    entries = build_promotion_board()

    if args.format == "json":
        print(json.dumps(entries, indent=2))
    elif args.format == "markdown":
        print(format_promotion_board_for_review(entries))
    else:
        # Table view
        print(f"\n  Annex Promotion Board — {len(entries)} modules\n")
        print(f"  {'Module':<35} {'Status':<22} {'Flag':<5} {'Records':<10}")
        print(f"  {'-'*35} {'-'*22} {'-'*5} {'-'*10}")
        for e in entries:
            flag_icon = "✓" if e.get("flag_enabled") else "—"
            print(f"  {e['module_name']:<35} {e['status']:<22} {flag_icon:<5} {e['annex_record_count']:<10}")
        print()

    if not args.no_write:
        try:
            out_path = save_promotion_board(entries)
            print(f"  Written: {out_path}", file=sys.stderr)
        except Exception as exc:
            print(f"  [WARN] Could not write board: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
