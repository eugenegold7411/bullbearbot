"""
archive_weekly_review_outputs.py — Archive weekly review outputs into dated folder.

Usage:
    python3 scripts/archive_weekly_review_outputs.py
    python3 scripts/archive_weekly_review_outputs.py --date 2026-04-20

Finds the most recent (or specified) weekly review report and creates:
    data/weekly_review/YYYY-MM-DD/
    ├── weekly_review_output.md
    ├── run_manifest.json
    ├── cost_summary.json
    ├── recommendation_summary.json
    └── status_snapshot.json

Safe to run multiple times — won't overwrite existing manifest.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPORTS_DIR = Path("data/reports")
_ARCHIVE_BASE = Path("data/weekly_review")
_SPINE_PATH = Path("data/analytics/cost_attribution_spine.jsonl")
_MEMO_HISTORY = Path("data/reports/director_memo_history.json")
_CONFIG = Path("strategy_config.json")


def _find_report_path(date_str: str | None) -> Path | None:
    if date_str:
        p = _REPORTS_DIR / f"weekly_review_{date_str}.md"
        return p if p.exists() else None
    # Find most recent
    candidates = sorted(_REPORTS_DIR.glob("weekly_review_*.md"), reverse=True)
    return candidates[0] if candidates else None


def _load_flags() -> dict:
    try:
        cfg = json.loads(_CONFIG.read_text())
        flags: dict = {}
        flags.update(cfg.get("feature_flags", {}))
        flags.update(cfg.get("shadow_flags", {}))
        flags.update(cfg.get("lab_flags", {}))
        return flags
    except Exception:
        return {}


def _build_cost_summary(days_back: int = 7) -> dict:
    try:
        from cost_attribution import get_spine_summary  # noqa: PLC0415
        by_module = get_spine_summary(days_back=days_back, group_by="module_name")
        by_model = get_spine_summary(days_back=days_back, group_by="model")
        total = sum(v["total_cost_usd"] for v in by_module.values())
        return {
            "schema_version": 1,
            "days_back": days_back,
            "total_cost_usd": round(total, 6),
            "by_module": by_module,
            "by_model": by_model,
        }
    except Exception as exc:
        return {"schema_version": 1, "error": str(exc)}


def _build_recommendation_summary() -> dict:
    recs: list[dict] = []
    try:
        if _MEMO_HISTORY.exists():
            history = json.loads(_MEMO_HISTORY.read_text())
            for week_entry in history.get("weeks", []):
                for rec in week_entry.get("recommendations", []):
                    recs.append({
                        "rec_id": rec.get("rec_id"),
                        "week": week_entry.get("week"),
                        "status": rec.get("status", "pending"),
                        "verdict": rec.get("verdict", ""),
                        "text": str(rec.get("text", rec.get("recommendation", "")))[:120],
                    })
    except Exception as exc:
        return {"schema_version": 1, "error": str(exc), "recommendations": []}
    pending = [r for r in recs if r.get("status") == "pending"]
    return {
        "schema_version": 1,
        "total": len(recs),
        "pending_count": len(pending),
        "recommendations": recs,
    }


def _build_status_snapshot() -> dict:
    try:
        from scripts.generate_system_status import generate_status  # noqa: PLC0415
        return generate_status()
    except Exception:
        pass
    # Minimal fallback
    flags = _load_flags()
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "active_flags": {k: v for k, v in flags.items() if v},
        "note": "Full status unavailable — run generate_system_status.py for complete snapshot",
    }


def archive_outputs(
    date_str: str | None = None,
    run_started_at: str | None = None,
    run_completed_at: str | None = None,
    notes: str = "",
) -> Path:
    """
    Archive weekly review artifacts into data/weekly_review/YYYY-MM-DD/.
    Returns the archive directory path.
    """
    report_path = _find_report_path(date_str)

    # Determine archive date
    if date_str:
        archive_date = date_str
    elif report_path:
        # Extract date from filename
        stem = report_path.stem  # weekly_review_2026-04-20
        parts = stem.split("_")
        archive_date = parts[-1] if len(parts) >= 3 else datetime.now().strftime("%Y-%m-%d")
    else:
        archive_date = datetime.now().strftime("%Y-%m-%d")

    archive_dir = _ARCHIVE_BASE / archive_date
    archive_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    artifact_paths: dict[str, str] = {}

    # Copy main report
    report_dest = archive_dir / "weekly_review_output.md"
    if report_path and report_path.exists():
        shutil.copy2(report_path, report_dest)
        artifact_paths["weekly_review_output"] = str(report_dest)
    else:
        artifact_paths["weekly_review_output"] = None  # type: ignore[assignment]

    # Cost summary
    cost_dest = archive_dir / "cost_summary.json"
    cost_data = _build_cost_summary()
    cost_dest.write_text(json.dumps(cost_data, indent=2))
    artifact_paths["cost_summary"] = str(cost_dest)

    # Recommendation summary
    rec_dest = archive_dir / "recommendation_summary.json"
    rec_data = _build_recommendation_summary()
    rec_dest.write_text(json.dumps(rec_data, indent=2))
    artifact_paths["recommendation_summary"] = str(rec_dest)

    # Status snapshot
    status_dest = archive_dir / "status_snapshot.json"
    status_data = _build_status_snapshot()
    status_dest.write_text(json.dumps(status_data, indent=2))
    artifact_paths["status_snapshot"] = str(status_dest)

    # Model usage from cost summary
    model_usage = {
        k: v.get("total_calls", 0)
        for k, v in cost_data.get("by_model", {}).items()
    }

    # Manifest (don't overwrite if exists)
    manifest_dest = archive_dir / "run_manifest.json"
    artifact_paths["run_manifest"] = str(manifest_dest)
    if not manifest_dest.exists():
        flags = _load_flags()
        manifest = {
            "schema_version": 1,
            "run_date": archive_date,
            "run_started_at": run_started_at or now,
            "run_completed_at": run_completed_at or now,
            "artifact_paths": artifact_paths,
            "active_feature_flags": flags,
            "schema_versions_seen": [1],
            "model_usage_summary": model_usage,
            "test_count": None,
            "notes": notes,
        }
        manifest_dest.write_text(json.dumps(manifest, indent=2))

    return archive_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive weekly review outputs")
    parser.add_argument("--date", help="YYYY-MM-DD date of the review to archive")
    args = parser.parse_args()

    archive_dir = archive_outputs(date_str=args.date)
    print(f"[OK] Archived to: {archive_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
