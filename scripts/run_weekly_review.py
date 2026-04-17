"""
run_weekly_review.py — One-command weekly review runner with archiving.

Usage:
    python3 scripts/run_weekly_review.py
    python3 scripts/run_weekly_review.py --emergency --reason "TSM short blowup"
    python3 scripts/run_weekly_review.py --dry-run

After completion:
- Prints path to dated output folder
- Prints 3-line summary: cost, top agent finding, recommendation count
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_CONFIG = Path("strategy_config.json")
_SPINE_PATH = Path("data/analytics/cost_attribution_spine.jsonl")
_MEMO_HISTORY = Path("data/reports/director_memo_history.json")
_REPORTS_DIR = Path("data/reports")


def _check_dependencies() -> list[str]:
    issues: list[str] = []
    if not _CONFIG.exists():
        issues.append("MISSING: strategy_config.json")
    try:
        import anthropic  # noqa: F401
    except ImportError:
        issues.append("MISSING: anthropic package (pip install anthropic)")
    if not Path("weekly_review.py").exists():
        issues.append("MISSING: weekly_review.py")
    return issues


def _dry_run() -> int:
    print("\n  Weekly Review — Dependency Check\n")
    issues = _check_dependencies()
    if issues:
        for issue in issues:
            print(f"  ✗ {issue}")
        print()
        return 1

    # Check flags
    try:
        cfg = json.loads(_CONFIG.read_text())
        flags = cfg.get("feature_flags", {})
        key_flags = [
            "enable_cost_attribution_spine",
            "enable_thesis_checksum",
            "enable_divergence_summarizer",
        ]
        for flag in key_flags:
            val = flags.get(flag, False)
            status = "✓ on" if val else "⬜ off"
            print(f"  {status}  {flag}")
    except Exception as exc:
        print(f"  [WARN] Could not read feature flags: {exc}")

    print()
    print("  ✓ All dependencies OK. Ready to run.")
    print("  Run without --dry-run to execute.\n")
    return 0


def _print_summary(archive_dir: Path, run_started_at: str) -> None:
    print(f"\n  Archive: {archive_dir}")

    # Cost
    cost_file = archive_dir / "cost_summary.json"
    if cost_file.exists():
        try:
            cost = json.loads(cost_file.read_text())
            total = cost.get("total_cost_usd", 0.0)
            print(f"  Cost:    ${total:.4f} (last 7 days)")
        except Exception:
            pass

    # Recommendations
    rec_file = archive_dir / "recommendation_summary.json"
    if rec_file.exists():
        try:
            recs = json.loads(rec_file.read_text())
            total = recs.get("total", 0)
            pending = recs.get("pending_count", 0)
            print(f"  Recs:    {total} total, {pending} pending")
        except Exception:
            pass

    # Report path
    report_file = archive_dir / "weekly_review_output.md"
    if report_file.exists():
        print(f"  Report:  {report_file}")

    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run weekly review with archiving")
    parser.add_argument("--emergency", action="store_true")
    parser.add_argument("--reason", default="")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    args = parser.parse_args()

    if args.dry_run:
        return _dry_run()

    issues = _check_dependencies()
    if issues:
        print("\n  Dependency check failed:")
        for issue in issues:
            print(f"    {issue}")
        print()
        return 1

    run_started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    print(f"\n  Starting weekly review at {run_started_at[:19]} UTC")
    if args.emergency:
        print(f"  Mode: EMERGENCY — {args.reason}")
    print()

    try:
        import weekly_review  # noqa: PLC0415
        report_path = weekly_review.run_review(
            emergency=args.emergency,
            reason=args.reason,
        )
    except Exception as exc:
        print(f"  [ERROR] Weekly review failed: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1

    run_completed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # Archive
    try:
        from scripts.archive_weekly_review_outputs import archive_outputs  # noqa: PLC0415
        archive_dir = archive_outputs(
            run_started_at=run_started_at,
            run_completed_at=run_completed_at,
        )
        _print_summary(archive_dir, run_started_at)
    except Exception as exc:
        print(f"  [WARN] Archive step failed (review completed OK): {exc}")
        print(f"  Report written to: {report_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
