"""
scripts/backfill_trade_outcomes.py — one-time ChromaDB outcome backfill.

Two backfill paths:
  Path A — decisions.json: reads memory/decisions.json, finds actions with
            resolved outcome (win/loss) from update_outcomes_from_alpaca(),
            and updates ChromaDB via update_trade_outcome(vector_id, outcome, pnl).

  Path B — decision_outcomes.jsonl: reads data/analytics/decision_outcomes.jsonl,
            finds submitted records with correct_1d resolved (from forward return
            backfill), joins on decision_id → vector_id via decisions.json,
            and updates ChromaDB.

Usage:
    cd /home/trading-bot
    PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python .venv/bin/python3 \
        scripts/backfill_trade_outcomes.py

Safe to re-run: update_trade_outcome() is idempotent (upserts metadata).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")

DECISIONS_FILE     = Path("memory/decisions.json")
OUTCOMES_LOG       = Path("data/analytics/decision_outcomes.jsonl")


def _build_vector_lookup(decisions: list[dict]) -> dict[str, str]:
    """Build {decision_id: vector_id} from decisions.json records."""
    return {
        d["decision_id"]: d["vector_id"]
        for d in decisions
        if d.get("decision_id") and d.get("vector_id")
    }


def run_backfill() -> None:
    try:
        import trade_memory as tm
    except ImportError:
        print("ERROR: trade_memory not importable — is ChromaDB available?")
        sys.exit(1)

    stats = tm.get_collection_stats()
    if stats.get("status") != "ok":
        print(f"ERROR: ChromaDB unavailable — status={stats.get('status')}")
        print("Run with: PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python")
        sys.exit(1)

    print(f"ChromaDB status: OK  (short={stats['short']} medium={stats['medium']} long={stats['long']})")

    # Load decisions.json
    if not DECISIONS_FILE.exists():
        print(f"ERROR: {DECISIONS_FILE} not found")
        sys.exit(1)
    decisions = json.loads(DECISIONS_FILE.read_text())
    print(f"Loaded {len(decisions)} decisions from {DECISIONS_FILE}")
    vector_lookup = _build_vector_lookup(decisions)
    print(f"  decision_id → vector_id entries: {len(vector_lookup)}")

    # ── Path A: decisions.json actions with resolved outcome ──────────────────
    path_a_updated = 0
    path_a_skipped = 0
    for dec in decisions:
        vid = dec.get("vector_id", "")
        if not vid:
            continue
        for action in dec.get("actions", []):
            outcome = action.get("outcome")
            pnl     = action.get("pnl")
            if outcome in ("win", "loss") and pnl is not None:
                try:
                    tm.update_trade_outcome(vid, outcome, float(pnl))
                    path_a_updated += 1
                except Exception as exc:
                    print(f"  Path A: failed to update {vid}: {exc}")
                    path_a_skipped += 1

    print("\nPath A (decisions.json resolved actions):")
    print(f"  Updated: {path_a_updated}  Skipped/error: {path_a_skipped}")
    if path_a_updated == 0:
        print("  (0 resolved actions in decisions.json — expected when no trades have closed)")

    # ── Path B: decision_outcomes.jsonl with forward returns ──────────────────
    path_b_updated = 0
    path_b_no_vector = 0
    path_b_no_returns = 0

    if OUTCOMES_LOG.exists():
        outcomes = []
        for line in OUTCOMES_LOG.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                outcomes.append(json.loads(line))
            except Exception:
                pass

        print("\nPath B (decision_outcomes.jsonl):")
        print(f"  Loaded {len(outcomes)} outcome records")

        submitted = [r for r in outcomes if r.get("status") == "submitted"]
        print(f"  Submitted (executed trades): {len(submitted)}")

        for rec in submitted:
            decision_id = rec.get("decision_id", "")
            correct_1d  = rec.get("correct_1d")
            return_1d   = rec.get("return_1d")

            if correct_1d is None or return_1d is None:
                path_b_no_returns += 1
                continue

            vid = vector_lookup.get(decision_id)
            if not vid:
                path_b_no_vector += 1
                continue

            outcome = "win" if correct_1d else "loss"
            try:
                tm.update_trade_outcome(vid, outcome, float(return_1d))
                path_b_updated += 1
            except Exception as exc:
                print(f"  Path B: failed to update {vid}: {exc}")

        print(f"  Updated: {path_b_updated}")
        print(f"  No forward returns yet: {path_b_no_returns}")
        print(f"  No matching vector_id: {path_b_no_vector}")
    else:
        print(f"\nPath B: {OUTCOMES_LOG} not found — skipping")

    # ── Summary ───────────────────────────────────────────────────────────────
    total = path_a_updated + path_b_updated
    print(f"\n{'='*50}")
    print(f"BACKFILL COMPLETE: {total} ChromaDB entries updated")
    stats_after = tm.get_collection_stats()
    print(f"ChromaDB totals after: short={stats_after['short']} medium={stats_after['medium']} long={stats_after['long']}")


if __name__ == "__main__":
    run_backfill()
