"""
versioning.py — Schema migration and versioning framework (T0.5).

Owns: version detection, migration registry, dry-run logic, backup helpers,
compatibility reads. Schema shapes remain in schemas.py.

Non-fatal callers must wrap calls in try/except; this module raises
SchemaVersionTooOld intentionally and propagates IO errors.
schema_version convention: integer, 1 = first v2 artifact version.
Legacy artifacts with no schema_version field are treated as version 0.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Migration registry (module-level)
# ─────────────────────────────────────────────────────────────────────────────

_MIGRATIONS: dict[tuple[str, int], Callable[[dict], dict]] = {}


def register_migration(
    artifact_type: str,
    from_version: int,
    fn: Callable[[dict], dict],
) -> None:
    """Register a migration fn for artifact_type from from_version to from_version+1."""
    _MIGRATIONS[(artifact_type, from_version)] = fn


# ─────────────────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────────────────

class SchemaVersionTooOld(Exception):
    """Raised when an artifact version is older than the supported n-1 minimum."""

    def __init__(
        self,
        artifact_type: str,
        found_version: int,
        minimum_supported_version: int,
    ) -> None:
        self.artifact_type = artifact_type
        self.found_version = found_version
        self.minimum_supported_version = minimum_supported_version
        super().__init__(
            f"{artifact_type}: found version {found_version}, "
            f"minimum supported is {minimum_supported_version}. "
            "Only current (n) and one prior version (n-1) are supported."
        )


# ─────────────────────────────────────────────────────────────────────────────
# MigrationResult
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MigrationResult:
    artifact_type: str
    path: Path
    from_version: int
    to_version: int
    dry_run: bool
    success: bool
    would_change: bool
    changes: list[str] = field(default_factory=list)
    backup_path: Optional[Path] = None
    error: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def detect_version(artifact: dict) -> int:
    """Read schema_version field. Returns 0 if absent (legacy/unversioned)."""
    return int(artifact.get("schema_version", 0))


def write_backup_snapshot(path: Path) -> Path:
    """
    Copy path → path.backup_YYYYMMDD_HHMMSS before any migration.
    Returns backup path. Raises if source doesn't exist.
    """
    if not path.exists():
        raise FileNotFoundError(f"Cannot back up {path}: file does not exist")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.parent / (path.name + f".backup_{ts}")
    shutil.copy2(path, backup)
    log.info("[VERSIONING] Backup created: %s", backup)
    return backup


def load_with_compat(
    path: Path,
    current_version: int,
    migrations: dict,
) -> dict:
    """
    Load JSON from path. Detect version.
    - version == current_version → return as-is.
    - version == current_version - 1 → apply migration, return migrated dict.
    - version < current_version - 1 → raise SchemaVersionTooOld.
    migrations dict: {(artifact_type, from_version): fn} — same format as _MIGRATIONS.
    When scanning, uses the first matching from_version found (callers should
    pass a filtered dict or _MIGRATIONS when the artifact type is unambiguous).
    """
    with open(path) as fh:
        artifact = json.load(fh)

    found = detect_version(artifact)

    if found == current_version:
        return artifact

    minimum = current_version - 1
    if found < minimum:
        raise SchemaVersionTooOld(
            artifact_type="unknown",
            found_version=found,
            minimum_supported_version=minimum,
        )

    # found == current_version - 1: find and apply migration
    # Scan provided dict first, fall back to module-level _MIGRATIONS
    fn: Optional[Callable] = None
    for (_, from_ver), migration_fn in migrations.items():
        if from_ver == found:
            fn = migration_fn
            break
    if fn is None:
        for (_, from_ver), migration_fn in _MIGRATIONS.items():
            if from_ver == found:
                fn = migration_fn
                break

    if fn is None:
        log.warning("[VERSIONING] No migration found for from_version=%d", found)
        return artifact

    return fn(artifact)


def migrate_artifact(
    path: Path,
    current_version: int,
    migrations: dict,
    dry_run: bool = True,
    artifact_type: str = "unknown",
) -> MigrationResult:
    """
    Migrate a JSON artifact to current_version.
    Dry-run by default.

    When dry_run=False:
      1. Read + detect version
      2. Validate pre-migration (must be exactly n-1)
      3. Compute changes
      4. write_backup_snapshot(path)
      5. Apply migration fn
      6. Validate post-migration (schema_version == current_version)
      7. Atomic write (write to .tmp, rename)
      8. Return MigrationResult(success=True, backup_path=..., changes=[...])
    """
    try:
        with open(path) as fh:
            artifact = json.load(fh)
        found = detect_version(artifact)

        if found == current_version:
            return MigrationResult(
                artifact_type=artifact_type,
                path=path,
                from_version=found,
                to_version=current_version,
                dry_run=dry_run,
                success=True,
                would_change=False,
                changes=["Already at current version — no migration needed"],
            )

        minimum = current_version - 1
        if found < minimum:
            return MigrationResult(
                artifact_type=artifact_type,
                path=path,
                from_version=found,
                to_version=current_version,
                dry_run=dry_run,
                success=False,
                would_change=False,
                error=(
                    f"Version {found} is too old; minimum supported is {minimum}. "
                    "Only current (n) and one prior version (n-1) are supported."
                ),
            )

        # Resolve migration fn
        fn: Optional[Callable] = migrations.get((artifact_type, found))
        if fn is None:
            fn = _MIGRATIONS.get((artifact_type, found))
        if fn is None:
            # Fallback: scan by from_version only (artifact_type unknown/mismatched)
            for (_, from_ver), migration_fn in {**_MIGRATIONS, **migrations}.items():
                if from_ver == found:
                    fn = migration_fn
                    break
        if fn is None:
            return MigrationResult(
                artifact_type=artifact_type,
                path=path,
                from_version=found,
                to_version=current_version,
                dry_run=dry_run,
                success=False,
                would_change=False,
                error=(
                    f"No migration registered for ({artifact_type}, "
                    f"{found}→{current_version})"
                ),
            )

        # Compute diff
        migrated = fn(artifact)
        changes: list[str] = []
        for k, v in migrated.items():
            if k not in artifact:
                changes.append(f"Added field '{k}' = {v!r}")
            elif artifact[k] != v:
                changes.append(f"Changed '{k}': {artifact[k]!r} → {v!r}")
        if not changes:
            changes = ["schema_version bumped — no other field changes"]

        if dry_run:
            return MigrationResult(
                artifact_type=artifact_type,
                path=path,
                from_version=found,
                to_version=current_version,
                dry_run=True,
                success=True,
                would_change=True,
                changes=changes,
            )

        # Live migration
        backup = write_backup_snapshot(path)

        if migrated.get("schema_version") != current_version:
            return MigrationResult(
                artifact_type=artifact_type,
                path=path,
                from_version=found,
                to_version=current_version,
                dry_run=False,
                success=False,
                would_change=True,
                changes=changes,
                backup_path=backup,
                error=(
                    f"Post-migration schema_version mismatch: "
                    f"expected {current_version}, "
                    f"got {migrated.get('schema_version')}"
                ),
            )

        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(migrated, indent=2))
        tmp.rename(path)
        log.info("[VERSIONING] Migrated %s v%d→v%d", path, found, current_version)
        return MigrationResult(
            artifact_type=artifact_type,
            path=path,
            from_version=found,
            to_version=current_version,
            dry_run=False,
            success=True,
            would_change=True,
            changes=changes,
            backup_path=backup,
        )

    except Exception as exc:  # noqa: BLE001
        log.warning("[VERSIONING] migrate_artifact failed for %s: %s", path, exc)
        return MigrationResult(
            artifact_type=artifact_type,
            path=path,
            from_version=0,
            to_version=current_version,
            dry_run=dry_run,
            success=False,
            would_change=False,
            error=str(exc),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Wired artifact migrations
# ─────────────────────────────────────────────────────────────────────────────

def _migrate_recommendation_record_v0_to_v1(artifact: dict) -> dict:
    """v0→v1: add schema_version=1, verdict="pending" if absent."""
    result = dict(artifact)
    result["schema_version"] = 1
    if "verdict" not in result:
        result["verdict"] = "pending"
    return result


def _migrate_a2_readiness_state_v1_to_v2(artifact: dict) -> dict:
    """v1→v2: add iv_history_ready and observation_complete as independent fields."""
    result = dict(artifact)
    result["schema_version"] = 2
    if "iv_history_ready" not in result:
        result["iv_history_ready"] = False
    if "observation_complete" not in result:
        result["observation_complete"] = False
    return result


def _migrate_cost_attribution_record_v0_to_v1(artifact: dict) -> dict:
    """v0→v1: add schema_version=1 (new artifact type, stub migration)."""
    result = dict(artifact)
    result["schema_version"] = 1
    return result


register_migration("recommendation_record", 0, _migrate_recommendation_record_v0_to_v1)
register_migration("a2_readiness_state", 1, _migrate_a2_readiness_state_v1_to_v2)
register_migration("cost_attribution_record", 0, _migrate_cost_attribution_record_v0_to_v1)
