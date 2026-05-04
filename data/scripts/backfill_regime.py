"""
data/scripts/backfill_regime.py

Normalises regime values in both A1 and A2 decisions files:
  "normal"  → "neutral"   (115 records in A1, 4 in A2)
  "risk-on" → "risk_on"   (14 records in A2)

Root cause: _OVERNIGHT_SYS in bot_stage3_decision.py used "normal" as the
proceed-normally token while Stage 1 uses "neutral" for the same concept —
a naming collision from two independent prompt authors. Fixed in
bot_stage3_decision.py (prompt) and bot_stage1_regime.py
(_normalize_regime_labels). This script backfills existing records.

Supports --dry-run. Idempotent. Atomic write via tmp + rename.

Usage:
    cd /home/trading-bot
    .venv/bin/python data/scripts/backfill_regime.py --dry-run
    .venv/bin/python data/scripts/backfill_regime.py
"""

import argparse
import json
import os
from collections import Counter
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent.parent

_NORMALIZATIONS: dict[str, str] = {
    "normal":  "neutral",
    "risk-on": "risk_on",
}

_TARGETS = [
    _BASE / "memory" / "decisions.json",
    _BASE / "data" / "account2" / "trade_memory" / "decisions_account2.json",
]


def _normalise(value: str) -> str:
    return _NORMALIZATIONS.get(value, value)


def process_file(path: Path, dry_run: bool) -> None:
    if not path.exists():
        print(f"  SKIP  {path.name} — file not found")
        return

    records = json.loads(path.read_text())
    before = Counter(r.get("regime", "MISSING") for r in records)

    changed = 0
    for r in records:
        old = r.get("regime", "")
        new = _normalise(old)
        if old != new:
            r["regime"] = new
            changed += 1

    after = Counter(r.get("regime", "MISSING") for r in records)

    label = "DRY RUN" if dry_run else "LIVE RUN"
    print(f"\n[{label}] {path.name}")
    print(f"  Total records : {len(records)}")
    print(f"  Records changed: {changed}")
    print("  BEFORE:")
    for k, v in before.most_common():
        mark = "  <- will change" if k in _NORMALIZATIONS else ""
        print(f"    {repr(k):<20s} {v}{mark}")
    print("  AFTER:")
    for k, v in after.most_common():
        print(f"    {repr(k):<20s} {v}")

    if dry_run:
        return

    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(records, indent=2, ensure_ascii=False))
    os.replace(tmp, path)
    print(f"  Written -> {path.name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill regime label normalisation")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()

    os.chdir(_BASE)
    for path in _TARGETS:
        process_file(path, dry_run=args.dry_run)

    print("\nDone.")


if __name__ == "__main__":
    main()
