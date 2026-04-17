"""
inspect_forensic_reviews.py — Inspect post-trade forensic review records.

Usage:
    python3 scripts/inspect_forensic_reviews.py
    python3 scripts/inspect_forensic_reviews.py --decision-id dec_A1_...
    python3 scripts/inspect_forensic_reviews.py --symbol TSM
    python3 scripts/inspect_forensic_reviews.py --verdict incorrect
    python3 scripts/inspect_forensic_reviews.py --last N
    python3 scripts/inspect_forensic_reviews.py --validate
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_FORENSIC_LOG = Path("data/analytics/forensic_log.jsonl")
_CONFIG = Path("strategy_config.json")

_OPTIONAL_FIELDS = [
    "checksum_id", "hindsight_id", "alpha_classification",
    "what_worked", "what_failed", "entry_price", "exit_price",
    "realized_pnl", "hold_duration_hours",
]


def _thesis_checksum_enabled() -> bool:
    try:
        cfg = json.loads(_CONFIG.read_text())
        return bool(cfg.get("feature_flags", {}).get("enable_thesis_checksum", False))
    except Exception:
        return False


def _load_records(last_n: int | None) -> list[dict]:
    if not _FORENSIC_LOG.exists():
        return []
    records = []
    with open(_FORENSIC_LOG) as fh:
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


def _print_table(records: list[dict]) -> None:
    fmt = "  {:<11} {:<8} {:<12} {:<12} {:>6} {:>9} {:<20}"
    print(fmt.format("date", "symbol", "thesis_v", "exec_v", "conf%", "pnl", "alpha_class"))
    print("  " + "─" * 80)
    for rec in records:
        ts = str(rec.get("created_at", "?"))[:10]
        sym = str(rec.get("symbol", "?"))[:8]
        tv = str(rec.get("thesis_verdict", "?"))[:12]
        ev = str(rec.get("execution_verdict", "?"))[:12]
        conf = rec.get("thesis_verdict_confidence", 0.0)
        pnl = rec.get("realized_pnl")
        pnl_str = f"${pnl:.2f}" if pnl is not None else "n/a"
        alpha = str(rec.get("alpha_classification") or "")[:20]
        print(fmt.format(ts, sym, tv, ev, f"{conf*100:.0f}%", pnl_str, alpha))


def _print_detail(rec: dict) -> None:
    print()
    labels = {
        "forensic_id": "Forensic ID",
        "decision_id": "Decision ID",
        "symbol": "Symbol",
        "created_at": "Date",
        "entry_price": "Entry Price",
        "exit_price": "Exit Price",
        "realized_pnl": "Realized PnL",
        "hold_duration_hours": "Hold (hours)",
        "thesis_verdict": "Thesis Verdict",
        "thesis_verdict_confidence": "Confidence",
        "execution_verdict": "Execution Verdict",
        "management_drifted": "Mgmt Drifted",
        "regime_contradicted": "Regime Contradicted",
        "what_worked": "What Worked",
        "what_failed": "What Failed",
        "pattern_tags": "Pattern Tags",
        "checksum_id": "Checksum ID",
        "hindsight_id": "Hindsight ID",
        "alpha_classification": "Alpha Class",
        "model_used": "Model Used",
    }
    for key, label in labels.items():
        val = rec.get(key)
        if val is None:
            print(f"  {label:<30} (not set)")
        else:
            print(f"  {label:<30} {val}")
    if rec.get("abstention"):
        print(f"  {'Abstention':<30} {rec['abstention']}")
    print()


def _print_validation(records: list[dict]) -> None:
    print(f"\n  Validation report ({len(records)} records)\n")
    any_issues = False
    for rec in records:
        issues = [f for f in _OPTIONAL_FIELDS if not rec.get(f)]
        if issues:
            any_issues = True
            fid = rec.get("decision_id", rec.get("forensic_id", "?"))
            print(f"  {fid}: missing {', '.join(issues)}")
            if "checksum_id" in issues:
                print(f"    → thesis not captured at entry (enable_thesis_checksum must be true at decision time)")
    if not any_issues:
        print("  All records have all optional fields populated.")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect forensic review records")
    parser.add_argument("--decision-id", dest="decision_id")
    parser.add_argument("--symbol")
    parser.add_argument("--verdict", help="Filter by thesis_verdict value")
    parser.add_argument("--last", type=int, metavar="N")
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args()

    enabled = _thesis_checksum_enabled()

    if not _FORENSIC_LOG.exists() or _FORENSIC_LOG.stat().st_size == 0:
        print("\n  No forensic records yet.")
        print("  enable_thesis_checksum must be True AND a trade must close before records appear.")
        print(f"\n  Current enable_thesis_checksum status: {enabled}\n")
        if not enabled:
            print("  → Set enable_thesis_checksum=true in strategy_config.json to enable.\n")
        return 0

    records = _load_records(args.last)

    if not records:
        print("\n  No forensic records found.\n")
        return 0

    # Filters
    if args.decision_id:
        records = [r for r in records if r.get("decision_id") == args.decision_id]
        if not records:
            print(f"\n  decision_id {args.decision_id!r} not found.\n")
            return 1
        _print_detail(records[0])
        return 0

    if args.symbol:
        records = [r for r in records if r.get("symbol", "").upper() == args.symbol.upper()]
    if args.verdict:
        records = [r for r in records if r.get("thesis_verdict") == args.verdict]

    if args.validate:
        _print_validation(records)
        return 0

    print(f"\n  Forensic Reviews ({len(records)} records)  |  enable_thesis_checksum={enabled}\n")
    _print_table(records)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
