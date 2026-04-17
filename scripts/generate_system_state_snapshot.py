"""
generate_system_state_snapshot.py — Write or display the canonical system-state artifact.

Usage:
    python3 scripts/generate_system_state_snapshot.py
    python3 scripts/generate_system_state_snapshot.py --format markdown
    python3 scripts/generate_system_state_snapshot.py --watch          # refresh every 60s
    python3 scripts/generate_system_state_snapshot.py --no-write       # print only
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


def _run(args: argparse.Namespace) -> int:
    try:
        from system_state import build_system_state, save_system_state, format_system_state_markdown, _STATE_FILE  # noqa: PLC0415
    except ImportError as exc:
        print(f"[ERROR] Cannot import system_state: {exc}", file=sys.stderr)
        return 1

    snap = build_system_state()

    if args.format == "markdown":
        md = format_system_state_markdown(snap)
        print(md)
        if not args.no_write:
            md_path = Path("data/status/system_state_snapshot.md")
            md_path.parent.mkdir(parents=True, exist_ok=True)
            md_path.write_text(md)
            print(f"\n  Written: {md_path}")
    else:
        import json
        from dataclasses import asdict
        payload = asdict(snap)
        print(json.dumps(payload, indent=2))

    if not args.no_write:
        try:
            out_path = save_system_state(snap)
            if args.format != "markdown":
                print(f"\n  Written: {out_path}", file=sys.stderr)
        except Exception as exc:
            print(f"  [WARN] Could not write snapshot: {exc}", file=sys.stderr)

    if snap.build_errors:
        print(f"\n  [WARN] {len(snap.build_errors)} build error(s):", file=sys.stderr)
        for err in snap.build_errors:
            print(f"    {err}", file=sys.stderr)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate system state snapshot")
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    parser.add_argument("--watch", action="store_true", help="Refresh every 60 seconds")
    parser.add_argument("--no-write", action="store_true", dest="no_write")
    args = parser.parse_args()

    if args.watch:
        try:
            while True:
                print("\033[2J\033[H", end="")  # clear screen
                _run(args)
                print("\n  [watching — Ctrl-C to stop]")
                time.sleep(60)
        except KeyboardInterrupt:
            print("\n  Stopped.")
            return 0
    else:
        return _run(args)


if __name__ == "__main__":
    sys.exit(main())
