#!/usr/bin/env python3
"""
simulate_bad_migration_and_rollback.py — T0.6 Rollback Simulation

Proves the rollback playbook works end-to-end.
Uses only tempdir — never touches data/ or any production file.

Run: python3 scripts/simulate_bad_migration_and_rollback.py
Exit 0 = all steps PASS.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

# Ensure repo root is on sys.path regardless of CWD
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import versioning
import feature_flags


def _pass(step: str, detail: str = "") -> None:
    print(f"[PASS] Step {step}" + (f": {detail}" if detail else ""))


def _fail(step: str, reason: str) -> None:
    print(f"[FAIL] Step {step}: {reason}")


def main() -> int:
    results: list[bool] = []
    tmpdir = Path(tempfile.mkdtemp(prefix="bullbearbot_sim_"))

    try:
        print(f"\n=== BullBearBot Rollback Simulation ===")
        print(f"Tempdir: {tmpdir}\n")

        # ─────────────────────────────────────────────────────────────────────
        # Step 1: Create a synthetic v0 artifact (recommendation_record)
        # ─────────────────────────────────────────────────────────────────────
        try:
            artifact_path = tmpdir / "rec_test.json"
            v0_artifact = {
                "rec_id": "rec_2026-04-16_0",
                "week_str": "2026-04-16",
                "created_at": "2026-04-16T12:00:00Z",
                "resolved_at": None,
                # deliberately NO schema_version, NO verdict
            }
            artifact_path.write_text(json.dumps(v0_artifact, indent=2))
            assert artifact_path.exists(), "artifact file not created"
            loaded = json.loads(artifact_path.read_text())
            assert "schema_version" not in loaded, "v0 artifact must not have schema_version"
            assert "verdict" not in loaded, "v0 artifact must not have verdict"
            _pass("1", "v0 artifact created without schema_version or verdict")
            results.append(True)
        except Exception as exc:
            _fail("1", f"v0 artifact creation failed: {exc}")
            results.append(False)

        # ─────────────────────────────────────────────────────────────────────
        # Step 2: migrate_artifact dry_run=True — assert would_change=True
        # ─────────────────────────────────────────────────────────────────────
        try:
            result = versioning.migrate_artifact(
                path=artifact_path,
                current_version=1,
                migrations=versioning._MIGRATIONS,
                dry_run=True,
                artifact_type="recommendation_record",
            )
            assert result.would_change is True, f"would_change should be True, got {result.would_change}"
            assert result.success is True, f"dry-run success should be True, got {result.success}"
            assert result.backup_path is None, "dry-run must not create backup"
            # Verify original file is unchanged
            still_v0 = json.loads(artifact_path.read_text())
            assert "schema_version" not in still_v0, "dry-run must not modify file"
            print(f"       Changes that would be made: {result.changes}")
            _pass("2", f"dry_run=True: would_change=True, file unchanged. Changes: {result.changes}")
            results.append(True)
        except Exception as exc:
            _fail("2", f"dry-run failed: {exc}\n{traceback.format_exc()}")
            results.append(False)

        # ─────────────────────────────────────────────────────────────────────
        # Step 3: migrate_artifact dry_run=False — backup created, file migrated
        # ─────────────────────────────────────────────────────────────────────
        try:
            result = versioning.migrate_artifact(
                path=artifact_path,
                current_version=1,
                migrations=versioning._MIGRATIONS,
                dry_run=False,
                artifact_type="recommendation_record",
            )
            assert result.success is True, f"migration failed: {result.error}"
            assert result.backup_path is not None, "backup_path must be set"
            assert result.backup_path.exists(), f"backup file not found: {result.backup_path}"

            migrated = json.loads(artifact_path.read_text())
            assert migrated.get("schema_version") == 1, (
                f"schema_version should be 1, got {migrated.get('schema_version')}"
            )
            assert migrated.get("verdict") == "pending", (
                f"verdict should be 'pending', got {migrated.get('verdict')}"
            )
            _pass(
                "3",
                f"migration applied: schema_version=1, verdict='pending'. "
                f"Backup: {result.backup_path.name}"
            )
            results.append(True)
        except Exception as exc:
            _fail("3", f"live migration failed: {exc}\n{traceback.format_exc()}")
            results.append(False)

        # ─────────────────────────────────────────────────────────────────────
        # Step 4: Simulate a "bad migration" — corrupt the migrated file
        # ─────────────────────────────────────────────────────────────────────
        try:
            artifact_path.write_text("{ THIS IS NOT VALID JSON !!!")
            try:
                json.loads(artifact_path.read_text())
                _fail("4", "file should be invalid JSON but parsed successfully")
                results.append(False)
            except json.JSONDecodeError:
                _pass("4", "file successfully corrupted with invalid JSON")
                results.append(True)
        except Exception as exc:
            _fail("4", f"corruption step failed: {exc}")
            results.append(False)

        # ─────────────────────────────────────────────────────────────────────
        # Step 5: Restore from backup — assert matches original v0 artifact
        # ─────────────────────────────────────────────────────────────────────
        try:
            backup = result.backup_path  # type: ignore[union-attr]
            assert backup is not None and backup.exists(), "backup path missing"
            shutil.copy2(backup, artifact_path)
            restored = json.loads(artifact_path.read_text())
            assert "schema_version" not in restored, (
                "restored file should be v0 (no schema_version)"
            )
            assert restored.get("rec_id") == "rec_2026-04-16_0", (
                f"restored rec_id wrong: {restored.get('rec_id')}"
            )
            _pass("5", "backup restored successfully — file matches original v0 artifact")
            results.append(True)
        except Exception as exc:
            _fail("5", f"restore from backup failed: {exc}\n{traceback.format_exc()}")
            results.append(False)

        # ─────────────────────────────────────────────────────────────────────
        # Step 6: Simulate feature flag rollback via feature_flags module
        # ─────────────────────────────────────────────────────────────────────
        try:
            config_path = tmpdir / "strategy_config.json"

            # Write config with flag=true, point feature_flags at it
            config_path.write_text(json.dumps({
                "feature_flags": {"enable_cost_attribution_spine": True},
                "shadow_flags": {},
                "lab_flags": {},
            }, indent=2))

            # Override module-level path and reset cache
            feature_flags._CONFIG_PATH = config_path
            feature_flags._FLAG_CACHE = {}
            feature_flags._CACHE_LOADED = False

            enabled = feature_flags.is_enabled("enable_cost_attribution_spine")
            assert enabled is True, f"expected True, got {enabled}"

            # Flip flag to false
            config_path.write_text(json.dumps({
                "feature_flags": {"enable_cost_attribution_spine": False},
                "shadow_flags": {},
                "lab_flags": {},
            }, indent=2))

            # Force reload
            feature_flags.load_flags(force_reload=True)
            disabled = feature_flags.is_enabled("enable_cost_attribution_spine")
            assert disabled is False, f"expected False after flag flip, got {disabled}"

            _pass("6", "feature flag rollback: true→false verified via force_reload")
            results.append(True)
        except Exception as exc:
            _fail("6", f"feature flag simulation failed: {exc}\n{traceback.format_exc()}")
            results.append(False)

    finally:
        # Reset feature_flags to production path
        feature_flags._CONFIG_PATH = Path("strategy_config.json")
        feature_flags._FLAG_CACHE = {}
        feature_flags._CACHE_LOADED = False

        shutil.rmtree(tmpdir, ignore_errors=True)
        print(f"\nTempdir cleaned up: {tmpdir}")

    # ─────────────────────────────────────────────────────────────────────────
    # Summary
    # ─────────────────────────────────────────────────────────────────────────
    passed = sum(results)
    total = len(results)
    print(f"\n=== Results: {passed}/{total} steps passed ===")
    if passed == total:
        print("ALL STEPS PASS — rollback playbook is functional.\n")
        return 0
    else:
        failed_steps = [i + 1 for i, ok in enumerate(results) if not ok]
        print(f"FAILED STEPS: {failed_steps}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
