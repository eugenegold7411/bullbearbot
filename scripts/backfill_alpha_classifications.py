"""
scripts/backfill_alpha_classifications.py — one-time alpha-classification backfill.

For every closed round-trip in trade_journal since 2026-04-16, locate the
matching DecisionOutcomeRecord, compute the realized outcome_pct, run
classify_alpha(), and rewrite the record in decision_outcomes.jsonl.

Match precedence per closed trade:
  1. trade.decision_id  → exact match against record.decision_id
  2. fallback: same symbol + record.timestamp within ±24h of entry_time

Usage:
    cd /home/trading-bot
    .venv/bin/python3 scripts/backfill_alpha_classifications.py [--dry-run]

--dry-run prints what would be written without modifying decision_outcomes.jsonl.

Idempotent: running twice produces the same result. classify_alpha is
deterministic and update_outcome_record overwrites the previous classification.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

_BOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BOT_DIR))

try:
    from dotenv import load_dotenv
    load_dotenv(_BOT_DIR / ".env")
except ImportError:
    pass

os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")

import decision_outcomes as _do  # noqa: E402
import trade_journal as _tj  # noqa: E402

OUTCOMES_LOG = _BOT_DIR / "data" / "analytics" / "decision_outcomes.jsonl"
SINCE = datetime(2026, 4, 16, tzinfo=timezone.utc)
MATCH_WINDOW = timedelta(hours=24)


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        text = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _load_records() -> list[dict]:
    records: list[dict] = []
    if not OUTCOMES_LOG.exists():
        return records
    with open(OUTCOMES_LOG) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                continue
    return records


def _match(closed: dict, records: list[dict]) -> dict | None:
    """Locate the DecisionOutcomeRecord that this closed trade scores.

    decision_outcomes.jsonl has one record per (decision_id, symbol, action) —
    multi-symbol decisions produce multiple rows under the same decision_id.
    Match on (decision_id, symbol) where possible to avoid scoring the wrong leg.
    """
    decision_id = closed.get("decision_id")
    sym = closed.get("symbol")
    if decision_id:
        # Prefer (decision_id, symbol) buy-action match
        for r in records:
            if (r.get("decision_id") == decision_id
                and r.get("symbol") == sym
                and r.get("action") == "buy"):
                return r
        # Then any (decision_id, symbol) match
        for r in records:
            if r.get("decision_id") == decision_id and r.get("symbol") == sym:
                return r
    entry_t = _parse_iso(closed.get("entry_time"))
    if entry_t is None:
        return None
    best = None
    best_delta = MATCH_WINDOW
    for r in records:
        if r.get("symbol") != sym:
            continue
        if r.get("action") != "buy":
            continue
        rt = _parse_iso(r.get("timestamp"))
        if rt is None:
            continue
        delta = abs(rt - entry_t)
        if delta <= best_delta:
            best = r
            best_delta = delta
    return best


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print results without writing to decision_outcomes.jsonl")
    args = parser.parse_args()

    print(f"Backfill alpha classifications — dry_run={args.dry_run}")
    print(f"Cutoff: {SINCE.isoformat()}")

    closed_all = _tj.build_closed_trades()
    closed_recent = [
        t for t in closed_all
        if (_parse_iso(t.get("exit_time")) or datetime.min.replace(tzinfo=timezone.utc)) >= SINCE
    ]
    print(f"Closed trades total: {len(closed_all)}, since cutoff: {len(closed_recent)}")

    records = _load_records()
    print(f"DecisionOutcomeRecords loaded: {len(records)}")

    classifications: Counter = Counter()
    matched = 0
    unmatched: list[str] = []
    updates: list[tuple[str, str, float, str]] = []

    for ct in closed_recent:
        rec_dict = _match(ct, records)
        if rec_dict is None:
            unmatched.append(f"{ct.get('symbol')}@{ct.get('exit_time')}")
            continue
        entry_price = ct.get("entry_price") or 0.0
        exit_price  = ct.get("exit_price")  or 0.0
        if not entry_price:
            unmatched.append(f"{ct.get('symbol')}@{ct.get('exit_time')}:no_entry_price")
            continue
        outcome_pct = (exit_price - entry_price) / entry_price

        rec = _do.DecisionOutcomeRecord.from_dict(rec_dict)
        rec.outcome_pct = outcome_pct
        classification = _do.classify_alpha(rec)
        classifications[classification] += 1
        matched += 1
        updates.append((rec.decision_id, rec.symbol, outcome_pct, classification))

    if args.dry_run:
        print()
        print("=== DRY RUN — no writes ===")
        for did, sym, opct, cls in updates[:20]:
            print(f"  {did}  {sym:6s}  outcome={opct*100:+.2f}%  → {cls}")
        if len(updates) > 20:
            print(f"  ... and {len(updates) - 20} more")
    else:
        for did, sym, opct, cls in updates:
            _do.update_outcome_record(
                did,
                symbol=sym,
                action="buy",
                outcome_pct=opct,
                alpha_classification=cls,
                alpha_classification_reason="backfill_realized_outcome",
                alpha_classified_at=datetime.now(timezone.utc)
                    .isoformat().replace("+00:00", "Z"),
            )

    print()
    print(f"Matched & classified: {matched}")
    print(f"Unmatched: {len(unmatched)}")
    if unmatched:
        for u in unmatched[:10]:
            print(f"  - {u}")
        if len(unmatched) > 10:
            print(f"  ... and {len(unmatched) - 10} more")
    print(f"Classifications: {dict(classifications)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
