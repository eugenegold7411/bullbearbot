"""
system_state.py — Canonical system-state artifact (P6).

Richer superset of generate_system_status.py output.
build_system_state() reads all state from data/ and never raises.
save_system_state() writes atomically to data/status/system_state_snapshot.json.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_STATUS_DIR   = Path("data/status")
_STATE_FILE   = _STATUS_DIR / "system_state_snapshot.json"
_A1_MODE_FILE = Path("data/runtime/a1_mode.json")
_A2_MODE_FILE = Path("data/runtime/a2_mode.json")
_SPINE_LOG    = Path("data/analytics/cost_attribution_spine.jsonl")
_INCIDENT_LOG = Path("data/analytics/incident_log.jsonl")
_MEMO_HISTORY = Path("data/reports/director_memo_history.json")
_PREFLIGHT_LOG = Path("data/status/preflight_log.jsonl")
_PROMOTION_BOARD = Path("data/status/promotion_board.json")
_ANNEX_DIR    = Path("data/annex")
_FORENSIC_LOG = Path("data/analytics/forensic_log.jsonl")
_CONFIG       = Path("strategy_config.json")
_DECISIONS    = Path("memory/decisions.json")
_A2_STRUCTURES = Path("data/account2/positions/structures.json")

SCHEMA_VERSION = 1

# Items that require human verification — not auto-detectable from data files.
PENDING_VERIFICATIONS: list[str] = [
    "Reddit credentials (F001) — PRAW not configured, public fallback only",
    "Twitter auto-posting disabled — pending F003 Twitter API Basic upgrade",
    "Citrini memo — verify monthly memo is current (last ingested: Jan 2026)",
    "Account 2 options liquidity — spot-check fills are within 5% of mid price",
    "VPS disk usage — verify <20GB used before next weekly data warehouse refresh",
]


@dataclass
class SystemStateSnapshot:
    schema_version: int = SCHEMA_VERSION
    generated_at: str = ""

    # Operating modes (from divergence.py)
    a1_mode: str = "unknown"                # NORMAL | RECONCILE_ONLY | RISK_CONTAINMENT | HALTED
    a2_mode: str = "unknown"
    a1_mode_since: Optional[str] = None
    a2_mode_since: Optional[str] = None

    # Account positions
    a1_open_positions: int = 0
    a2_open_structures: int = 0
    a1_position_symbols: list = field(default_factory=list)
    a2_structure_symbols: list = field(default_factory=list)

    # Cost state (from spine)
    cost_7d_usd: float = 0.0
    cost_24h_usd: float = 0.0
    cost_by_ring_7d: dict = field(default_factory=dict)  # prod / shadow / annex
    cost_top_modules_7d: list = field(default_factory=list)  # [{module, cost, calls}]

    # Data availability
    spine_record_count: int = 0
    decision_count: int = 0
    incident_count_7d: int = 0
    incident_severity_counts: dict = field(default_factory=dict)
    last_weekly_review_date: Optional[str] = None
    last_weekly_review_age_days: Optional[float] = None
    forensic_record_count: int = 0

    # Flags
    feature_flags_on: list = field(default_factory=list)
    shadow_flags_on: list = field(default_factory=list)
    lab_flags_on: list = field(default_factory=list)
    flags_off_notable: list = field(default_factory=list)  # flags that are off but file/data is ready

    # Shadow / annex state
    annex_modules_with_data: list = field(default_factory=list)
    annex_record_counts: dict = field(default_factory=dict)

    # Preflight
    last_preflight_verdict: Optional[str] = None
    last_preflight_at: Optional[str] = None
    preflight_blockers: list = field(default_factory=list)

    # Promotion board
    promotion_board_summary: dict = field(default_factory=dict)

    # Pending verifications (hardcoded)
    pending_verifications: list = field(default_factory=list)

    # Build errors (non-fatal accumulator)
    build_errors: list = field(default_factory=list)


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _read_jsonl(path: Path, max_lines: int = 0) -> list[dict]:
    if not path.exists():
        return []
    lines = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    lines.append(json.loads(line))
                except Exception:
                    pass
    if max_lines:
        return lines[-max_lines:]
    return lines


def _read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _load_operating_modes(snap: SystemStateSnapshot) -> None:
    for attr, path in [("a1_mode", _A1_MODE_FILE), ("a2_mode", _A2_MODE_FILE)]:
        data = _read_json(path)
        if data:
            mode = data.get("mode", "unknown")
            since = data.get("since") or data.get("updated_at")
            setattr(snap, attr, mode)
            setattr(snap, f"{attr}_since", since)


def _load_positions(snap: SystemStateSnapshot) -> None:
    decisions = _read_json(_DECISIONS, {})
    if isinstance(decisions, dict):
        positions_raw = decisions.get("positions", [])
        if isinstance(positions_raw, list):
            snap.a1_open_positions = len(positions_raw)
            snap.a1_position_symbols = [
                p.get("symbol", "") for p in positions_raw if isinstance(p, dict)
            ]
    a2_data = _read_json(_A2_STRUCTURES, [])
    if isinstance(a2_data, list):
        open_structs = [s for s in a2_data if isinstance(s, dict) and s.get("lifecycle") not in ("closed", "expired")]
        snap.a2_open_structures = len(open_structs)
        snap.a2_structure_symbols = list({s.get("symbol", "") for s in open_structs})


def _load_cost_state(snap: SystemStateSnapshot) -> None:
    records = _read_jsonl(_SPINE_LOG)
    snap.spine_record_count = len(records)
    if not records:
        return

    now = datetime.now(timezone.utc)
    cutoff_7d = (now - timedelta(days=7)).isoformat()
    cutoff_24h = (now - timedelta(hours=24)).isoformat()

    recent_7d = [r for r in records if r.get("timestamp", "") >= cutoff_7d]
    recent_24h = [r for r in records if r.get("timestamp", "") >= cutoff_24h]

    snap.cost_7d_usd = round(sum(r.get("total_cost_usd", 0.0) for r in recent_7d), 4)
    snap.cost_24h_usd = round(sum(r.get("total_cost_usd", 0.0) for r in recent_24h), 4)

    # By ring
    ring_costs: dict[str, float] = {}
    for r in recent_7d:
        ring = r.get("ring", "unknown")
        ring_costs[ring] = round(ring_costs.get(ring, 0.0) + r.get("total_cost_usd", 0.0), 4)
    snap.cost_by_ring_7d = ring_costs

    # Top modules
    module_costs: dict[str, dict] = {}
    for r in recent_7d:
        mod = r.get("module_name", "unknown")
        entry = module_costs.setdefault(mod, {"cost": 0.0, "calls": 0})
        entry["cost"] = round(entry["cost"] + r.get("total_cost_usd", 0.0), 4)
        entry["calls"] += 1
    top = sorted(module_costs.items(), key=lambda x: x[1]["cost"], reverse=True)[:8]
    snap.cost_top_modules_7d = [{"module": k, **v} for k, v in top]


def _load_incidents(snap: SystemStateSnapshot) -> None:
    now = datetime.now(timezone.utc)
    cutoff_7d = (now - timedelta(days=7)).isoformat()
    records = _read_jsonl(_INCIDENT_LOG)
    recent = [r for r in records if r.get("occurred_at", "") >= cutoff_7d]
    snap.incident_count_7d = len(recent)
    severity_counts: dict[str, int] = {}
    for r in recent:
        sev = r.get("severity", "unknown")
        severity_counts[sev] = severity_counts.get(sev, 0) + 1
    snap.incident_severity_counts = severity_counts


def _load_last_weekly_review(snap: SystemStateSnapshot) -> None:
    history = _read_json(_MEMO_HISTORY, {})
    if not isinstance(history, dict):
        return
    weeks = history.get("weeks", [])
    if not weeks:
        return
    last_week = weeks[-1].get("week", "")
    snap.last_weekly_review_date = last_week
    if last_week:
        try:
            review_dt = datetime.strptime(last_week, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - review_dt).total_seconds() / 86400
            snap.last_weekly_review_age_days = round(age, 1)
        except Exception:
            pass


def _load_forensic_count(snap: SystemStateSnapshot) -> None:
    snap.forensic_record_count = len(_read_jsonl(_FORENSIC_LOG))


def _load_flags(snap: SystemStateSnapshot) -> None:
    config = _read_json(_CONFIG, {})
    if not isinstance(config, dict):
        return

    ff_on, sf_on, lf_on = [], [], []
    for flag, val in config.get("feature_flags", {}).items():
        if val:
            ff_on.append(flag)
    for flag, val in config.get("shadow_flags", {}).items():
        if val:
            sf_on.append(flag)
    for flag, val in config.get("lab_flags", {}).items():
        if val:
            lf_on.append(flag)

    snap.feature_flags_on = sorted(ff_on)
    snap.shadow_flags_on = sorted(sf_on)
    snap.lab_flags_on = sorted(lf_on)

    # Notable flags that are off but data/infra is ready
    notable_off = []
    ff = config.get("feature_flags", {})
    if not ff.get("enable_cost_attribution_spine") and _SPINE_LOG.exists():
        notable_off.append("enable_cost_attribution_spine (spine file exists)")
    if not ff.get("enable_model_tiering"):
        notable_off.append("enable_model_tiering (declarations complete)")
    snap.flags_off_notable = notable_off


def _load_annex_state(snap: SystemStateSnapshot) -> None:
    if not _ANNEX_DIR.exists():
        return
    modules_with_data = []
    record_counts = {}
    for module_dir in sorted(_ANNEX_DIR.iterdir()):
        if not module_dir.is_dir():
            continue
        count = sum(1 for _ in module_dir.rglob("*.jsonl")) + sum(1 for _ in module_dir.rglob("*.json"))
        if count > 0:
            modules_with_data.append(module_dir.name)
            jsonl_records = 0
            for jf in module_dir.rglob("*.jsonl"):
                jsonl_records += len(_read_jsonl(jf))
            record_counts[module_dir.name] = jsonl_records
    snap.annex_modules_with_data = modules_with_data
    snap.annex_record_counts = record_counts


def _load_preflight_state(snap: SystemStateSnapshot) -> None:
    records = _read_jsonl(_PREFLIGHT_LOG)
    if not records:
        return
    last = records[-1]
    snap.last_preflight_verdict = last.get("verdict")
    snap.last_preflight_at = last.get("checked_at")
    snap.preflight_blockers = last.get("blockers", [])


def _load_promotion_board(snap: SystemStateSnapshot) -> None:
    board = _read_json(_PROMOTION_BOARD, {})
    if not isinstance(board, dict):
        return
    entries = board.get("entries", [])
    if not isinstance(entries, list):
        return
    status_counts: dict[str, int] = {}
    for e in entries:
        status = e.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    snap.promotion_board_summary = {
        "total": len(entries),
        "by_status": status_counts,
        "generated_at": board.get("generated_at"),
    }


def _load_decisions(snap: SystemStateSnapshot) -> None:
    decisions = _read_json(_DECISIONS, {})
    if isinstance(decisions, dict):
        snap.decision_count = len(decisions.get("decisions", []))


def build_system_state() -> SystemStateSnapshot:
    """
    Read all state from data/ and return a SystemStateSnapshot.
    Never raises — all errors are accumulated in snap.build_errors.
    """
    snap = SystemStateSnapshot(
        generated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        pending_verifications=PENDING_VERIFICATIONS,
    )

    loaders = [
        ("operating_modes", _load_operating_modes),
        ("positions", _load_positions),
        ("cost_state", _load_cost_state),
        ("incidents", _load_incidents),
        ("last_weekly_review", _load_last_weekly_review),
        ("forensic_count", _load_forensic_count),
        ("flags", _load_flags),
        ("annex_state", _load_annex_state),
        ("preflight_state", _load_preflight_state),
        ("promotion_board", _load_promotion_board),
        ("decisions", _load_decisions),
    ]

    for name, loader in loaders:
        try:
            loader(snap)
        except Exception as exc:
            snap.build_errors.append(f"{name}: {exc}")
            log.warning("[SYSTEM_STATE] loader %s failed: %s", name, exc)

    return snap


def save_system_state(snap: SystemStateSnapshot | None = None) -> Path:
    """
    Atomically write system state to data/status/system_state_snapshot.json.
    Returns the path written.
    """
    if snap is None:
        snap = build_system_state()
    _STATUS_DIR.mkdir(parents=True, exist_ok=True)
    payload = asdict(snap)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=_STATUS_DIR, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, _STATE_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise
    return _STATE_FILE


def format_system_state_markdown(snap: SystemStateSnapshot | None = None) -> str:
    """Return ~30-line markdown summary of current system state."""
    if snap is None:
        snap = build_system_state()

    now_str = snap.generated_at[:19].replace("T", " ")
    lines = [
        f"## System State — {now_str} UTC",
        "",
        "**Operating Modes**",
        f"- A1: `{snap.a1_mode}` (since {snap.a1_mode_since or 'unknown'})",
        f"- A2: `{snap.a2_mode}` (since {snap.a2_mode_since or 'unknown'})",
        "",
        "**Positions**",
        f"- A1 open: {snap.a1_open_positions} ({', '.join(snap.a1_position_symbols) or 'none'})",
        f"- A2 open structures: {snap.a2_open_structures} ({', '.join(snap.a2_structure_symbols) or 'none'})",
        "",
        "**Cost (7d / 24h)**",
        f"- Total: ${snap.cost_7d_usd:.4f} / ${snap.cost_24h_usd:.4f}",
    ]
    if snap.cost_top_modules_7d:
        top3 = snap.cost_top_modules_7d[:3]
        lines.append("- Top modules: " + ", ".join(f"{m['module']} ${m['cost']:.4f}" for m in top3))

    lines += [
        "",
        "**Data Health**",
        f"- Spine records: {snap.spine_record_count}",
        f"- Decisions: {snap.decision_count}",
        f"- Incidents (7d): {snap.incident_count_7d}",
        f"- Forensic records: {snap.forensic_record_count}",
    ]
    if snap.last_weekly_review_date:
        age_str = f" ({snap.last_weekly_review_age_days:.0f}d ago)" if snap.last_weekly_review_age_days else ""
        lines.append(f"- Last weekly review: {snap.last_weekly_review_date}{age_str}")

    lines += ["", "**Flags**"]
    if snap.feature_flags_on:
        lines.append("- Feature flags ON: " + ", ".join(snap.feature_flags_on[:6]))
    if snap.lab_flags_on:
        lines.append("- Lab flags ON: " + ", ".join(snap.lab_flags_on[:6]))
    if snap.flags_off_notable:
        lines.append("- Notable OFF: " + ", ".join(snap.flags_off_notable[:3]))

    if snap.annex_modules_with_data:
        lines += ["", f"**Annex** — {len(snap.annex_modules_with_data)} modules with data: " + ", ".join(snap.annex_modules_with_data[:6])]

    if snap.last_preflight_verdict:
        lines += ["", f"**Preflight** — last verdict: `{snap.last_preflight_verdict}` at {snap.last_preflight_at or 'unknown'}"]
        if snap.preflight_blockers:
            lines.append("  Blockers: " + ", ".join(snap.preflight_blockers[:3]))

    if snap.pending_verifications:
        lines += ["", "**Pending Verifications**"]
        for item in snap.pending_verifications:
            lines.append(f"- {item}")

    if snap.build_errors:
        lines += ["", f"**Build Errors** ({len(snap.build_errors)})"]
        for err in snap.build_errors:
            lines.append(f"- {err}")

    return "\n".join(lines)
