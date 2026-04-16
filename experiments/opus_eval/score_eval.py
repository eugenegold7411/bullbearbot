#!/usr/bin/env python3
"""
Scoring aggregator — run AFTER all blind scoring is complete.

Usage:
    python3 score_eval.py --filled scoring_sheet_filled.csv

Steps:
    1. Reads filled scoring sheet
    2. Opens SEALED/model_mapping.json (this is the reveal moment)
    3. Opens call_log.jsonl for cost/latency
    4. Computes per-class and aggregate results
    5. Prints decision matrix against pre-registered criteria
"""

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path


def load_mapping(sealed_dir: Path) -> dict:
    path = sealed_dir / "model_mapping.json"
    if not path.exists():
        print(f"FATAL: mapping not found at {path}", file=sys.stderr)
        sys.exit(2)
    with open(path) as f:
        return json.load(f)


def load_call_log(path: Path) -> dict:
    """Returns {(artifact_id, label): entry}."""
    out = {}
    with open(path) as f:
        for line in f:
            entry = json.loads(line)
            out[(entry["artifact_id"], entry["blinded_label"])] = entry
    return out


def load_scores(path: Path) -> list[dict]:
    with open(path) as f:
        return list(csv.DictReader(f))


def resolve_model(rows: list[dict], mapping: dict) -> list[dict]:
    """Annotate each row with resolved model identity."""
    for row in rows:
        aid = row["artifact_id"]
        label = row["blinded_label"]
        row["model"] = mapping[aid][label]
    return rows


def parse_score(val: str):
    if val == "" or val.upper() == "N/A":
        return None
    try:
        return int(val)
    except ValueError:
        return None


def parse_bool(val: str) -> bool:
    return val.strip().upper() in {"TRUE", "1", "YES", "Y"}


def compute_per_artifact_winner(rows_by_aid: dict, dimension: str) -> dict:
    """For each artifact, compare Sonnet vs Opus on one dimension."""
    results = {}
    for aid, rows in rows_by_aid.items():
        sonnet = next((r for r in rows if "sonnet" in r["model"]), None)
        opus = next((r for r in rows if "opus" in r["model"]), None)
        if not sonnet or not opus:
            continue
        s = parse_score(sonnet.get(dimension, ""))
        o = parse_score(opus.get(dimension, ""))
        if s is None or o is None:
            results[aid] = "na"
        elif o > s:
            results[aid] = "opus"
        elif s > o:
            results[aid] = "sonnet"
        else:
            results[aid] = "tie"
    return results


def tally(results: dict) -> dict:
    counts = defaultdict(int)
    for v in results.values():
        counts[v] += 1
    return dict(counts)


def report_by_class(rows: list[dict], dimensions: list[str]) -> None:
    """Group by artifact_class, compute dimension winners."""
    by_class = defaultdict(list)
    for row in rows:
        by_class[row["artifact_class"]].append(row)

    for cls, cls_rows in by_class.items():
        rows_by_aid = defaultdict(list)
        for r in cls_rows:
            rows_by_aid[r["artifact_id"]].append(r)
        promo = parse_bool(cls_rows[0]["promotion_evidence"])
        flag = " (promotion evidence)" if promo else " (judgment-only, not promotion evidence)"

        print(f"\n### Class: {cls}{flag}")
        print(f"Artifacts: {len(rows_by_aid)}")
        for dim in dimensions:
            winners = compute_per_artifact_winner(rows_by_aid, dim)
            counts = tally(winners)
            opus = counts.get("opus", 0)
            sonnet = counts.get("sonnet", 0)
            tie = counts.get("tie", 0)
            na = counts.get("na", 0)
            print(f"  {dim:<24} Opus:{opus}  Sonnet:{sonnet}  Tie:{tie}  N/A:{na}")


def report_novel_failure_modes(rows: list[dict]) -> None:
    print("\n### Novel Failure Mode Flags")
    for row in rows:
        if parse_bool(row.get("novel_failure_mode_flag", "")):
            verifiable = parse_bool(row.get("novel_failure_mode_verifiable_within_90d", ""))
            ground_truth = parse_bool(row.get("ground_truth_available", ""))
            counts_for_tiebreak = verifiable or ground_truth
            badge = "✓ counts" if counts_for_tiebreak else "✗ does not count"
            print(
                f"  [{row['artifact_class']}] {row['artifact_id']} "
                f"({row['model']}) — {badge}"
            )
            desc = row.get("novel_failure_mode_description", "")[:120]
            print(f"      {desc}")


def report_cost(rows: list[dict], call_log: dict) -> None:
    by_model = defaultdict(lambda: {"cost": 0.0, "wall_ms": 0, "n": 0})
    for row in rows:
        key = (row["artifact_id"], row["blinded_label"])
        entry = call_log.get(key)
        if not entry:
            continue
        m = entry["model"]
        by_model[m]["cost"] += entry["estimated_cost_usd"]
        by_model[m]["wall_ms"] += entry["wall_clock_ms"]
        by_model[m]["n"] += 1

    print("\n### Cost & Latency")
    for m, stats in by_model.items():
        avg_ms = stats["wall_ms"] / stats["n"] if stats["n"] else 0
        print(
            f"  {m}: total ${stats['cost']:.4f} across {stats['n']} calls, "
            f"avg latency {avg_ms:.0f}ms"
        )
    if "claude-sonnet-4-6" in by_model and "claude-opus-4-7" in by_model:
        s_cost = by_model["claude-sonnet-4-6"]["cost"]
        o_cost = by_model["claude-opus-4-7"]["cost"]
        if s_cost > 0:
            mult = o_cost / s_cost
            print(f"  Opus/Sonnet cost multiplier on this run: {mult:.2f}x")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--filled", type=Path, required=True,
                        help="Path to filled scoring_sheet_filled.csv")
    parser.add_argument("--base-dir", type=Path, default=Path("."))
    args = parser.parse_args()

    base = args.base_dir
    mapping = load_mapping(base / "SEALED")
    call_log = load_call_log(base / "call_log.jsonl")
    rows = load_scores(args.filled)

    # Confirm every row has a score
    missing = [r for r in rows if not r.get("information_surfacing", "").strip()]
    if missing:
        print(f"WARNING: {len(missing)} rows appear unscored. Continuing anyway.", file=sys.stderr)

    rows = resolve_model(rows, mapping)

    print("=" * 70)
    print("OPUS 4.7 vs SONNET 4.6 — EVALUATION RESULTS")
    print("=" * 70)

    dimensions = [
        "information_surfacing",
        "correctness",
        "actionability",
        "decision_delta",
        "calibration",
    ]

    report_by_class(rows, dimensions)
    report_novel_failure_modes(rows)
    report_cost(rows, call_log)

    print("\n" + "=" * 70)
    print("Compare against PRE_REGISTRATION.md decision criteria (A/B/C/D/E)")
    print("Do NOT update pre-registration after seeing results.")
    print("=" * 70)


if __name__ == "__main__":
    main()
