"""
promotion_board.py — Promotion board for Mad Science Annex modules (P3).

build_promotion_board() reads promotion_contracts/*.md + lab_flags + annex data
and returns a list of PromotionBoardEntry dicts saved to data/status/promotion_board.json.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_CONTRACTS_DIR = Path("promotion_contracts")
_ANNEX_DIR     = Path("data/annex")
_STATUS_DIR    = Path("data/status")
_BOARD_FILE    = _STATUS_DIR / "promotion_board.json"
_CONFIG        = Path("strategy_config.json")

SCHEMA_VERSION = 1

STATUS_VALUES = [
    "not_built",
    "built_not_enabled",
    "shadow_collecting",
    "awaiting_sample",
    "ready_for_review",
    "promoted",
    "deferred",
    "retired",
]

# Status ordering for sort (lower = earlier in pipeline)
_STATUS_ORDER = {s: i for i, s in enumerate(STATUS_VALUES)}


@dataclass
class PromotionBoardEntry:
    module_name: str
    contract_file: str
    contract_status: str         # raw status from .md file ("DRAFT", "PROMOTED", etc.)
    status: str                  # canonical STATUS_VALUES member
    feature_flag: str = ""
    flag_enabled: bool = False
    ring: str = ""
    evaluation_class: str = ""
    annex_record_count: int = 0
    annex_data_files: int = 0
    notes: str = ""


def _read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _read_jsonl_count(path: Path) -> int:
    if not path.exists():
        return 0
    count = 0
    with path.open() as f:
        for line in f:
            if line.strip():
                count += 1
    return count


def _load_all_flags() -> dict:
    config = _read_json(_CONFIG, {})
    if not isinstance(config, dict):
        return {}
    flags: dict = {}
    flags.update(config.get("feature_flags", {}))
    flags.update(config.get("shadow_flags", {}))
    flags.update(config.get("lab_flags", {}))
    return flags


def _parse_contract(path: Path) -> dict:
    """Extract module_name, status, feature_flag, ring, evaluation_class from .md."""
    text = path.read_text(errors="replace")
    result: dict = {
        "contract_status": "DRAFT",
        "feature_flag": "",
        "ring": "",
        "evaluation_class": "",
    }

    # Status line: **Status:** DRAFT
    m = re.search(r"\*\*Status:\*\*\s*(.+)", text)
    if m:
        result["contract_status"] = m.group(1).strip().upper()

    # Feature flag: **Feature flag:** `enable_internal_parliament`
    m = re.search(r"\*\*Feature flag:\*\*\s*`?([^`\n]+)`?", text)
    if m:
        result["feature_flag"] = m.group(1).strip()

    # Ring: **Ring:** lab → shadow → prod
    m = re.search(r"\*\*Ring:\*\*\s*(.+)", text)
    if m:
        result["ring"] = m.group(1).strip()

    # Evaluation class
    m = re.search(r"\*\*Evaluation class:\*\*\s*(.+)", text)
    if m:
        result["evaluation_class"] = m.group(1).strip()

    return result


def _get_annex_stats(module_name: str) -> tuple[int, int]:
    """Return (record_count, data_file_count) for a module's annex dir."""
    module_dir = _ANNEX_DIR / module_name
    if not module_dir.exists():
        return 0, 0
    data_files = list(module_dir.rglob("*.jsonl")) + list(module_dir.rglob("*.json"))
    record_count = 0
    for jf in module_dir.rglob("*.jsonl"):
        record_count += _read_jsonl_count(jf)
    return record_count, len(data_files)


def _derive_module_name(contract_path: Path) -> str:
    """Derive canonical module name from filename (e.g., internal_parliament_v1.md → internal_parliament)."""
    stem = contract_path.stem  # internal_parliament_v1
    # Strip trailing _v{N} suffix
    stem = re.sub(r"_v\d+$", "", stem)
    return stem


def _derive_status(
    contract_status: str,
    flag_enabled: bool,
    module_name: str,
    annex_record_count: int,
    flag_name: str,
    all_flags: dict,
) -> str:
    """Map contract state + runtime data to a canonical STATUS_VALUES member."""
    cs = contract_status.upper()

    if cs == "PROMOTED":
        return "promoted"
    if cs == "DEFERRED":
        return "deferred"
    if cs == "RETIRED":
        return "retired"
    if cs == "READY":
        return "ready_for_review"

    # DRAFT / ACTIVE / unknown — determine from runtime data
    module_file = Path(f"annex/{module_name}.py")
    module_exists = module_file.exists()

    if not module_exists:
        return "not_built"

    if not flag_enabled:
        return "built_not_enabled"

    # Flag is enabled — determine collection phase
    if annex_record_count == 0:
        return "awaiting_sample"

    # Has some data — check ring to determine shadow_collecting vs awaiting_sample
    ring_str = all_flags.get(flag_name, False)
    # If we have records but few (< 10), still awaiting_sample
    if annex_record_count < 10:
        return "awaiting_sample"

    return "shadow_collecting"


def build_promotion_board() -> list[dict]:
    """
    Build the promotion board from promotion_contracts/ + lab_flags + annex data.
    Returns a list of entry dicts (for JSON serialization).
    Never raises.
    """
    all_flags = _load_all_flags()
    entries: list[PromotionBoardEntry] = []

    if not _CONTRACTS_DIR.exists():
        log.warning("[PROMOTION_BOARD] promotion_contracts/ dir not found")
        return []

    for contract_path in sorted(_CONTRACTS_DIR.glob("*_v*.md")):
        try:
            module_name = _derive_module_name(contract_path)
            parsed = _parse_contract(contract_path)
            flag_name = parsed.get("feature_flag", f"enable_{module_name}")
            flag_enabled = bool(all_flags.get(flag_name, False))
            record_count, file_count = _get_annex_stats(module_name)
            status = _derive_status(
                contract_status=parsed.get("contract_status", "DRAFT"),
                flag_enabled=flag_enabled,
                module_name=module_name,
                annex_record_count=record_count,
                flag_name=flag_name,
                all_flags=all_flags,
            )
            entry = PromotionBoardEntry(
                module_name=module_name,
                contract_file=str(contract_path),
                contract_status=parsed.get("contract_status", "DRAFT"),
                status=status,
                feature_flag=flag_name,
                flag_enabled=flag_enabled,
                ring=parsed.get("ring", ""),
                evaluation_class=parsed.get("evaluation_class", ""),
                annex_record_count=record_count,
                annex_data_files=file_count,
            )
            entries.append(entry)
        except Exception as exc:
            log.warning("[PROMOTION_BOARD] error processing %s: %s", contract_path, exc)

    entries.sort(key=lambda e: (_STATUS_ORDER.get(e.status, 99), e.module_name))
    return [asdict(e) for e in entries]


def save_promotion_board(entries: list[dict] | None = None) -> Path:
    """Write promotion board to data/status/promotion_board.json. Returns path."""
    if entries is None:
        entries = build_promotion_board()
    _STATUS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "entry_count": len(entries),
        "entries": entries,
    }
    _BOARD_FILE.write_text(json.dumps(payload, indent=2))
    return _BOARD_FILE


def format_promotion_board_for_review(entries: list[dict] | None = None) -> str:
    """
    Return markdown table of promotion board for CTO weekly review.
    Grouped by status. Suitable for injection into Agent 5 prompt.
    """
    if entries is None:
        try:
            entries = build_promotion_board()
        except Exception as exc:
            return f"(promotion board unavailable: {exc})"

    if not entries:
        return "(no promotion contracts found)"

    lines = [
        "### Annex Promotion Board",
        "",
        f"| Module | Status | Flag | Records | Ring |",
        f"|--------|--------|------|---------|------|",
    ]
    for e in entries:
        flag_str = f"`{e['feature_flag']}`" if e["feature_flag"] else "—"
        flag_icon = "✓" if e["flag_enabled"] else "—"
        lines.append(
            f"| {e['module_name']} | {e['status']} | {flag_icon} {flag_str} | "
            f"{e['annex_record_count']} | {e['ring'] or '—'} |"
        )

    # Status summary
    from collections import Counter
    status_counts = Counter(e["status"] for e in entries)
    summary_parts = [f"{count}× {status}" for status, count in sorted(status_counts.items())]
    lines += ["", f"**Summary:** {len(entries)} modules — " + ", ".join(summary_parts)]

    return "\n".join(lines)
