"""
run_report.py — Manual report trigger, never blocked by scheduler state or time windows.

Usage:
    python3 scripts/run_report.py --report daily
    python3 scripts/run_report.py --report morning_brief
    python3 scripts/run_report.py --report weekly
    python3 scripts/run_report.py --list
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Allow imports from the project root.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

try:
    from dotenv import load_dotenv  # noqa: E402
    load_dotenv(_ROOT / ".env")
except ImportError:
    pass

ET = ZoneInfo("America/New_York")

_STATUS_DIR  = _ROOT / "data" / "status"
_REPORTS_DIR = _ROOT / "data" / "reports"
_BRIEF_FILE  = _ROOT / "data" / "market" / "morning_brief.json"


def _today_et() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def _last_sent(report_type: str) -> str:
    """Return ISO timestamp of last generation, or 'never'."""
    if report_type == "daily":
        flags = sorted(_STATUS_DIR.glob("daily_report_sent_*.flag"), reverse=True)
        if flags:
            date_str = flags[0].stem.replace("daily_report_sent_", "")
            ts = datetime.fromtimestamp(flags[0].stat().st_mtime, tz=timezone.utc)
            return f"{date_str} (file modified {ts.strftime('%Y-%m-%d %H:%M UTC')})"
        return "never"
    if report_type == "morning_brief":
        if _BRIEF_FILE.exists():
            try:
                brief = json.loads(_BRIEF_FILE.read_text())
                return brief.get("generated_at", "unknown (no generated_at field)")
            except Exception:
                ts = datetime.fromtimestamp(_BRIEF_FILE.stat().st_mtime, tz=timezone.utc)
                return f"file modified {ts.strftime('%Y-%m-%d %H:%M UTC')}"
        return "never"
    if report_type == "weekly":
        reports = sorted(_REPORTS_DIR.glob("weekly_review_*.md"), reverse=True)
        if reports:
            ts = datetime.fromtimestamp(reports[0].stat().st_mtime, tz=timezone.utc)
            return f"{reports[0].name} (modified {ts.strftime('%Y-%m-%d %H:%M UTC')})"
        return "never"
    return "unknown"


def cmd_list() -> None:
    print("Report                Last Generated")
    print("─" * 60)
    for rtype in ("daily", "morning_brief", "weekly"):
        label = {"daily": "Daily Report", "morning_brief": "Morning Brief",
                 "weekly": "Weekly Review"}[rtype]
        print(f"  {label:<18}  {_last_sent(rtype)}")


def cmd_daily() -> None:
    from datetime import date as _date
    import report as report_module

    today = _today_et()
    target = _date.fromisoformat(today)
    print(f"Generating daily report for {today} ...")
    try:
        report_module.send_report_email(target_date=target)
        print(f"✓  Daily report sent for {today}")
        print(f"   Notification: email via SendGrid")
    except Exception as exc:
        print(f"✗  Daily report failed: {exc}", file=sys.stderr)
        sys.exit(1)


def cmd_morning_brief() -> None:
    import morning_brief as mb

    print("Generating morning brief ...")
    try:
        brief = mb.generate_morning_brief()
        artifact = _BRIEF_FILE
        tone  = brief.get("market_tone", "?")
        picks = len(brief.get("conviction_picks", []))
        print(f"✓  Morning brief generated")
        print(f"   Artifact : {artifact}")
        print(f"   Tone     : {tone}  |  Picks: {picks}")
        print(f"   Notification: WhatsApp via Twilio (if configured)")
    except Exception as exc:
        print(f"✗  Morning brief failed: {exc}", file=sys.stderr)
        sys.exit(1)


def cmd_weekly() -> None:
    import weekly_review

    today = _today_et()
    print(f"Running weekly review for {today} (this takes several minutes) ...")
    try:
        report_path = weekly_review.run_review()
        print(f"✓  Weekly review complete")
        print(f"   Artifact : {report_path}")
        print(f"   Notification: SMS + email (if configured)")
    except Exception as exc:
        print(f"✗  Weekly review failed: {exc}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manually trigger any report, bypassing scheduler state and time windows."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--report",
        choices=["daily", "morning_brief", "weekly"],
        metavar="TYPE",
        help="Report to generate: daily | morning_brief | weekly",
    )
    group.add_argument(
        "--list",
        action="store_true",
        help="List all available reports and their last-generated timestamps",
    )
    args = parser.parse_args()

    if args.list:
        cmd_list()
    elif args.report == "daily":
        cmd_daily()
    elif args.report == "morning_brief":
        cmd_morning_brief()
    elif args.report == "weekly":
        cmd_weekly()


if __name__ == "__main__":
    main()
