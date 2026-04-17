"""
generate_system_status.py — Structured system state snapshot.

Usage:
    python3 scripts/generate_system_status.py
    python3 scripts/generate_system_status.py --format markdown

Output: data/status/system_status_snapshot.json (creates dir if needed)
        data/status/system_status_snapshot.md (with --format markdown)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

_CONFIG = Path("strategy_config.json")
_STATUS_DIR = Path("data/status")
_STATUS_JSON = _STATUS_DIR / "system_status_snapshot.json"
_STATUS_MD = _STATUS_DIR / "system_status_snapshot.md"
_SPINE_PATH = Path("data/analytics/cost_attribution_spine.jsonl")
_DECISIONS_PATH = Path("memory/decisions.json")
_OUTCOMES_PATH = Path("data/analytics/decision_outcomes.jsonl")
_FORENSIC_LOG = Path("data/analytics/forensic_log.jsonl")
_MEMO_HISTORY = Path("data/reports/director_memo_history.json")
_TEST_FILE = Path("tests/test_core.py")


# ─────────────────────────────────────────────────────────────────────────────
# Data helpers
# ─────────────────────────────────────────────────────────────────────────────

def _jsonl_count(path: str | Path) -> int:
    p = Path(path)
    if not p.exists():
        return 0
    count = 0
    with open(p) as fh:
        for line in fh:
            if line.strip():
                count += 1
    return count


def _decision_count() -> int:
    if not _DECISIONS_PATH.exists():
        return 0
    try:
        data = json.loads(_DECISIONS_PATH.read_text())
        if isinstance(data, list):
            return len(data)
        if isinstance(data, dict):
            return len(data.get("decisions", data.get("entries", [])))
        return 0
    except Exception:
        return 0


def _outcome_count() -> int:
    return _jsonl_count(_OUTCOMES_PATH)


def _test_count() -> int:
    if not _TEST_FILE.exists():
        return 0
    # Count "def test_" lines as a proxy
    count = 0
    with open(_TEST_FILE) as fh:
        for line in fh:
            if line.strip().startswith("def test_"):
                count += 1
    return count


def _spine_record_count(days_back: int = 1) -> int:
    if not _SPINE_PATH.exists():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    count = 0
    with open(_SPINE_PATH) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ts_str = rec.get("ts", "")
                if ts_str:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if ts >= cutoff:
                        count += 1
            except Exception:
                pass
    return count


def _last_cycle_at() -> str | None:
    if not _SPINE_PATH.exists():
        return None
    try:
        last_ts = None
        with open(_SPINE_PATH) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                ts = rec.get("ts")
                if ts:
                    last_ts = ts
        return last_ts
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Waiting thresholds
# ─────────────────────────────────────────────────────────────────────────────

WAITING_THRESHOLDS: dict[str, dict] = {
    "enable_outcome_critic": {
        "requires": "director_memo_history.json exists",
        "check": lambda: _MEMO_HISTORY.exists(),
    },
    "enable_experience_library": {
        "requires": "forensic_log.jsonl has records",
        "check": lambda: _jsonl_count(_FORENSIC_LOG) > 0,
    },
    "enable_tom_profile": {
        "requires": "50+ A1 decisions in decisions.json",
        "check": lambda: _decision_count() >= 50,
    },
    "enable_reputation_economy": {
        "requires": "5+ outcome-linked records in decision_outcomes.jsonl",
        "check": lambda: _outcome_count() >= 5,
    },
}

# Modules that are always-on (no feature flag)
ALWAYS_ON_MODULES = [
    "regime_classifier", "signal_scorer", "main_decision",
    "risk_kernel", "order_executor", "exit_manager",
    "market_data", "attribution",
]

# Known open issues
VERIFY_NEXT = [
    {
        "issue": "spine module_name='unknown' rate",
        "check_cmd": "python3 scripts/report_cost_spine_unknowns.py --days 1",
        "note": "Should be near 0% after O1 enrichment deploy",
    },
    {
        "issue": "session_tag on rejected trades",
        "check_cmd": "grep session logs/trades.jsonl | grep unknown | head -5",
        "note": "BUG-013 fixed; verify session field present on all recent trade records",
    },
    {
        "issue": "TSM/AMZN/XBI/QQQ/MSFT backstop exits",
        "check_cmd": "python3 -c \"import json; print([a['symbol'] for a in json.load(open('strategy_config.json'))['time_bound_actions']])\"",
        "note": "Verify these are cleared or still active",
    },
    {
        "issue": "Account 2 observation mode status",
        "check_cmd": "cat data/account2/obs_mode_state.json",
        "note": "Should show observation_complete=true after 20 trading days",
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# Main status logic
# ─────────────────────────────────────────────────────────────────────────────

def _load_all_flags() -> dict:
    try:
        cfg = json.loads(_CONFIG.read_text())
        flags: dict = {}
        flags.update(cfg.get("feature_flags", {}))
        flags.update(cfg.get("shadow_flags", {}))
        flags.update(cfg.get("lab_flags", {}))
        return flags
    except Exception:
        return {}


def _module_file_exists(flag_name: str) -> bool:
    """Heuristic: derive module filename from flag name."""
    # strip "enable_" prefix, map to likely file path
    name = flag_name.replace("enable_", "").replace("_shadow", "")
    # Check annex/ and root
    candidates = [
        Path(f"{name}.py"),
        Path(f"annex/{name}.py"),
    ]
    return any(p.exists() for p in candidates)


def generate_status() -> dict:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    flags = _load_all_flags()

    running_live: list[dict] = []
    flag_off_ready: list[dict] = []
    waiting_for_data: list[dict] = []
    experiments: list[dict] = []

    # Always-on modules
    for mod in ALWAYS_ON_MODULES:
        running_live.append({"module": mod, "reason": "always_on", "flag": None})

    for flag_name, flag_value in sorted(flags.items()):
        module_name = flag_name.replace("enable_", "")
        file_exists = _module_file_exists(flag_name)

        if flag_value:
            running_live.append({
                "module": module_name,
                "reason": f"{flag_name}=true",
                "flag": flag_name,
                "file_exists": file_exists,
            })
        else:
            # Check waiting thresholds
            threshold = WAITING_THRESHOLDS.get(flag_name)
            if threshold:
                try:
                    met = bool(threshold["check"]())
                except Exception:
                    met = False
                waiting_for_data.append({
                    "module": module_name,
                    "flag": flag_name,
                    "requires": threshold["requires"],
                    "prerequisite_met": met,
                    "file_exists": file_exists,
                })
            else:
                entry = {
                    "module": module_name,
                    "flag": flag_name,
                    "file_exists": file_exists,
                }
                # Lab ring flags are experiments
                lab_flags = set()
                try:
                    cfg = json.loads(_CONFIG.read_text())
                    lab_flags = set(cfg.get("lab_flags", {}).keys())
                except Exception:
                    pass
                if flag_name in lab_flags:
                    experiments.append(entry)
                else:
                    flag_off_ready.append(entry)

    spine_count = _spine_record_count(days_back=1)
    test_count = _test_count()
    last_cycle = _last_cycle_at()

    snapshot = {
        "schema_version": 1,
        "generated_at": now,
        "categories": {
            "running_live": running_live,
            "flag_off_ready": flag_off_ready,
            "waiting_for_data": waiting_for_data,
            "experiments": experiments,
            "verify_next": VERIFY_NEXT,
        },
        "summary": {
            "total_modules": (
                len(running_live) + len(flag_off_ready) +
                len(waiting_for_data) + len(experiments)
            ),
            "running_live_count": len(running_live),
            "flag_off_ready_count": len(flag_off_ready),
            "waiting_count": len(waiting_for_data),
            "experiment_count": len(experiments),
            "test_count": test_count,
            "spine_record_count_24h": spine_count,
            "last_cycle_at": last_cycle,
        },
    }
    return snapshot


def _format_markdown(snapshot: dict) -> str:
    now = snapshot["generated_at"][:19]
    summary = snapshot["summary"]
    cats = snapshot["categories"]
    lines = [
        f"# System Status Snapshot",
        f"Generated: {now}",
        "",
        "## Summary",
        f"- Total modules tracked: {summary['total_modules']}",
        f"- Running live: {summary['running_live_count']}",
        f"- Flag-off ready: {summary['flag_off_ready_count']}",
        f"- Waiting for data: {summary['waiting_count']}",
        f"- Lab experiments: {summary['experiment_count']}",
        f"- Test count: {summary['test_count']}",
        f"- Spine records (24h): {summary['spine_record_count_24h']}",
        f"- Last cycle: {summary.get('last_cycle_at') or 'unknown'}",
        "",
        "## Running Live",
    ]
    for m in cats["running_live"]:
        reason = m.get("reason", "")
        lines.append(f"  - `{m['module']}` ({reason})")

    lines += ["", "## Flag-Off Ready (flip to enable)"]
    for m in cats["flag_off_ready"]:
        fe = "✓" if m.get("file_exists") else "✗ file missing"
        lines.append(f"  - `{m['module']}` — flag: `{m['flag']}` [{fe}]")

    lines += ["", "## Waiting for Data"]
    for m in cats["waiting_for_data"]:
        met = "✅" if m.get("prerequisite_met") else "⬜"
        lines.append(f"  - `{m['module']}` — {met} {m['requires']}")

    lines += ["", "## Lab Experiments (disabled)"]
    for m in cats["experiments"]:
        fe = "✓" if m.get("file_exists") else "✗"
        lines.append(f"  - `{m['module']}` [{fe}]")

    lines += ["", "## Verify Next"]
    for v in cats["verify_next"]:
        lines.append(f"  - **{v['issue']}**")
        lines.append(f"    ```")
        lines.append(f"    {v['check_cmd']}")
        lines.append(f"    ```")
        lines.append(f"    {v['note']}")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate system status snapshot")
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    args = parser.parse_args()

    try:
        snapshot = generate_status()
    except Exception as exc:
        print(f"[ERROR] generate_status failed: {exc}", file=sys.stderr)
        return 1

    _STATUS_DIR.mkdir(parents=True, exist_ok=True)

    _STATUS_JSON.write_text(json.dumps(snapshot, indent=2))
    print(f"[OK] Written: {_STATUS_JSON}")

    if args.format == "markdown":
        md = _format_markdown(snapshot)
        _STATUS_MD.write_text(md)
        print(f"[OK] Written: {_STATUS_MD}")

    # Print brief summary to stdout
    summary = snapshot["summary"]
    print(
        f"     Live: {summary['running_live_count']} | "
        f"Ready: {summary['flag_off_ready_count']} | "
        f"Waiting: {summary['waiting_count']} | "
        f"Experiments: {summary['experiment_count']} | "
        f"Tests: {summary['test_count']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
