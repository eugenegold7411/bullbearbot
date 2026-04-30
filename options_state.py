"""
options_state.py — Persistence layer for Account 2 options structures.

Handles loading, saving, and querying OptionsStructure objects from
data/account2/positions/structures.json.

All writes are atomic (write to .tmp then rename) to avoid corruption
if the process is interrupted mid-write.

Public API
----------
save_structure(structure)             → None
load_structures()                     → list[OptionsStructure]
get_open_structures()                 → list[OptionsStructure]
get_structures_by_symbol(symbol)      → list[OptionsStructure]
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from schemas import OptionsStructure

log = logging.getLogger(__name__)

_STRUCTURES_PATH = Path(__file__).parent / "data" / "account2" / "positions" / "structures.json"


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def save_structure(structure: OptionsStructure) -> None:
    """
    Persist a single OptionsStructure to the structures file.

    If a structure with the same structure_id already exists, it is replaced
    in-place (update). New structures are appended.

    Write is atomic: data is written to a .tmp file then renamed.

    Parameters
    ----------
    structure : OptionsStructure
        The structure to save (any lifecycle state).
    """
    _ensure_dir()
    all_structs = _load_raw()

    # Replace existing entry by structure_id, or append
    found = False
    for i, s in enumerate(all_structs):
        if s.get("structure_id") == structure.structure_id:
            all_structs[i] = structure.to_dict()
            found = True
            break
    if not found:
        all_structs.append(structure.to_dict())

    _write_atomic(all_structs)
    log.debug(
        "[OPTIONS_STATE] saved structure_id=%s lifecycle=%s",
        structure.structure_id,
        structure.lifecycle.value,
    )


def load_structures() -> list[OptionsStructure]:
    """
    Load all structures from disk.

    Returns an empty list if the file does not exist or is empty.
    Malformed entries are skipped with a warning.
    """
    raw = _load_raw()
    result: list[OptionsStructure] = []
    for entry in raw:
        try:
            result.append(OptionsStructure.from_dict(entry))
        except Exception as exc:
            _eid = (
                entry.get("structure_id", "?")
                if isinstance(entry, dict)
                else repr(entry)[:60]
            )
            log.warning(
                "[OPTIONS_STATE] skipping malformed structure entry: %s — %s",
                _eid,
                exc,
            )
    return result


def get_open_structures() -> list[OptionsStructure]:
    """
    Return all structures that are currently active (FULLY_FILLED or PARTIALLY_FILLED).

    Does NOT include PROPOSED or SUBMITTED structures — those have not been filled yet.
    """
    return [
        s for s in load_structures()
        if s.is_open()
    ]


def compute_capital_utilization(
    structures: list[OptionsStructure],
    equity: float,
) -> tuple[float, float]:
    """
    Compute how much of equity is deployed in open structures.

    Deployed capital = sum of |net_debit| × contracts × 100 across all structures.
    Structures with net_debit=None are treated as 0 (conservative — undercounts
    utilization, so the gate never fires incorrectly on missing data).

    Returns (utilization_pct, deployed_usd) where utilization_pct is 0.0–1.0+.
    """
    deployed_usd = sum(
        abs(s.net_debit or 0.0) * (s.contracts or 0) * 100
        for s in structures
    )
    utilization_pct = deployed_usd / equity if equity > 0 else 0.0
    return utilization_pct, deployed_usd


def get_structures_by_symbol(symbol: str) -> list[OptionsStructure]:
    """
    Return all structures for a given underlying symbol (any lifecycle state).

    Parameters
    ----------
    symbol : str
        Canonical underlying symbol (e.g. "GLD"). Case-insensitive match.
    """
    sym_upper = symbol.upper()
    return [
        s for s in load_structures()
        if s.underlying.upper() == sym_upper
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_dir() -> None:
    """Create the structures directory if it doesn't exist."""
    _STRUCTURES_PATH.parent.mkdir(parents=True, exist_ok=True)


def _load_raw() -> list[dict]:
    """Load raw JSON list from disk. Returns [] if file missing or empty."""
    if not _STRUCTURES_PATH.exists():
        return []
    try:
        text = _STRUCTURES_PATH.read_text(encoding="utf-8").strip()
        if not text:
            return []
        return json.loads(text)
    except (json.JSONDecodeError, OSError) as exc:
        log.error("[OPTIONS_STATE] failed to load %s: %s", _STRUCTURES_PATH, exc)
        return []


def _write_atomic(data: list[dict]) -> None:
    """Write data to the structures file atomically via .tmp rename."""
    _ensure_dir()
    tmp_path = _STRUCTURES_PATH.with_suffix(".tmp")
    try:
        tmp_path.write_text(
            json.dumps(data, indent=2, default=str),
            encoding="utf-8",
        )
        os.replace(tmp_path, _STRUCTURES_PATH)
    except OSError as exc:
        log.error("[OPTIONS_STATE] failed to write %s: %s", _STRUCTURES_PATH, exc)
        raise
