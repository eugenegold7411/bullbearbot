"""
data/scripts/backfill_decisions.py

Backfills catalyst_type for action records in memory/decisions.json where the
value is "unknown", empty, or missing AND a raw catalyst string is present.

The existing classify_catalyst() (semantic_labels.py) is applied first.
A second-pass _backfill_enhance() covers patterns present in historical data
that the production keyword list was not written to handle.  This function
lives only in this script — no production code is modified.

Conviction and regime are not touched:
  - conviction: field was never written to action dicts; no source to backfill.
  - regime: normalisation handled by data/scripts/backfill_regime.py (separate script).

Usage:
    cd /home/trading-bot
    .venv/bin/python data/scripts/backfill_decisions.py --dry-run
    .venv/bin/python data/scripts/backfill_decisions.py
"""

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from semantic_labels import CatalystType, classify_catalyst  # noqa: E402

DECISIONS_PATH = Path("memory/decisions.json")


def _backfill_enhance(catalyst_str: str) -> str | None:
    """
    Extended keyword matching for backfill only.

    Covers patterns present in historical catalyst strings that the production
    classify_catalyst() keyword list does not handle.  Never imported by the
    running bot — changes here have no effect on live classification.

    Returns a CatalystType .value string, or None if still unclassifiable.
    """
    lo = catalyst_str.lower()

    if any(k in lo for k in ("insider option exercise", "insider option", "insider accumulation")):
        return CatalystType.INSIDER_BUY.value
    if any(k in lo for k in ("congressional accumulation", "congressional buy")):
        return CatalystType.CONGRESSIONAL_BUY.value
    if "congressional" in lo and any(k in lo for k in ("buy", "accumulation", "purchase")):
        return CatalystType.CONGRESSIONAL_BUY.value
    if any(k in lo for k in ("beats q", "beat expectations", "stellar results")):
        return CatalystType.EARNINGS_BEAT.value
    if any(k in lo for k in ("raising pt", "raising price target", "raised pt")):
        return CatalystType.ANALYST_REVISION.value
    if any(k in lo for k in ("pce", "stagflat", "ism manufacturing", "ism services")):
        return CatalystType.MACRO_PRINT.value

    return None


def backfill(dry_run: bool) -> None:
    raw = DECISIONS_PATH.read_text()
    data = json.loads(raw)

    before: Counter = Counter()
    after: Counter = Counter()
    updated = 0
    skipped_already_classified = 0
    skipped_no_catalyst = 0

    for record in data:
        for action in record.get("actions", []):
            ct = action.get("catalyst_type") or ""
            cat = action.get("catalyst") or ""

            before[ct if ct else "MISSING"] += 1

            if ct not in ("unknown", ""):
                # already has a real classification — leave it alone
                skipped_already_classified += 1
                after[ct] += 1
                continue

            if not cat.strip():
                skipped_no_catalyst += 1
                after["unknown"] += 1
                continue

            # Step 1: production classifier
            result = classify_catalyst(cat)

            # Step 2: enhanced backfill classifier if still unknown
            if result == CatalystType.UNKNOWN:
                enhanced = _backfill_enhance(cat)
                result_str = enhanced if enhanced else CatalystType.UNKNOWN.value
            else:
                result_str = result.value

            old_str = ct if ct else "unknown"
            changed = result_str != old_str

            if changed:
                updated += 1
                if dry_run:
                    sym = action.get("symbol", "?")
                    print(
                        f"  [{sym:8s}] {old_str} → {result_str}\n"
                        f"             {cat[:110]}"
                    )

            if not dry_run:
                action["catalyst_type"] = result_str

            after[result_str] += 1

    total_actions = sum(before.values())

    print()
    print("=" * 60)
    print(f"{'DRY RUN — no changes written' if dry_run else 'REAL RUN — changes written'}")
    print("=" * 60)
    print(f"Records examined:                 {len(data)}")
    print(f"Total action entries:             {total_actions}")
    print(f"Already classified (skipped):     {skipped_already_classified}")
    print(f"No raw catalyst (skipped):        {skipped_no_catalyst}")
    print(f"Updated:                          {updated}")
    remaining_unknown = after.get("unknown", 0)
    print(f"Remaining unknown after pass:     {remaining_unknown}")
    print()
    print("BEFORE distribution:")
    for k, v in sorted(before.items(), key=lambda x: -x[1]):
        print(f"  {k:<30s} {v}")
    print()
    print("AFTER  distribution:")
    for k, v in sorted(after.items(), key=lambda x: -x[1]):
        print(f"  {k:<30s} {v}")

    if not dry_run:
        tmp = DECISIONS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        os.replace(tmp, DECISIONS_PATH)
        print()
        print(f"Written atomically → {DECISIONS_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill catalyst_type in decisions.json")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    args = parser.parse_args()

    os.chdir(Path(__file__).resolve().parent.parent.parent)
    backfill(dry_run=args.dry_run)
