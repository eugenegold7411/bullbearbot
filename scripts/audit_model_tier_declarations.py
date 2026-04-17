"""
audit_model_tier_declarations.py — Audit model tier declarations for completeness.

Usage:
    python3 scripts/audit_model_tier_declarations.py
    python3 scripts/audit_model_tier_declarations.py --json

Exit 0 = all callers declared, no violations.
Exit 1 = undeclared callers or ring violations found.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _load_declarations() -> tuple[dict, dict]:
    """Returns (TIER_DECLARATIONS, MODULE_TIER_DECLARATIONS, CALLER_MODULE_MAP)."""
    try:
        from model_tiering import TIER_DECLARATIONS, MODULE_TIER_DECLARATIONS  # noqa: PLC0415
        from cost_attribution import CALLER_MODULE_MAP  # noqa: PLC0415
        return TIER_DECLARATIONS, MODULE_TIER_DECLARATIONS, CALLER_MODULE_MAP
    except ImportError as exc:
        print(f"[ERROR] Could not import declarations: {exc}", file=sys.stderr)
        sys.exit(2)


def _check_caller_coverage(
    caller_map: dict[str, str],
    tier_decls: dict,
) -> list[str]:
    """Return list of canonical module names in CALLER_MODULE_MAP not in TIER_DECLARATIONS."""
    undeclared = []
    seen = set()
    for canonical in caller_map.values():
        if canonical in seen:
            continue
        seen.add(canonical)
        if canonical not in tier_decls and canonical != "unknown":
            undeclared.append(canonical)
    return sorted(undeclared)


def _check_dict_vs_typed_sync(
    tier_decls: dict,
    module_tier_decls: dict,
) -> list[str]:
    """Return modules where TIER_DECLARATIONS and MODULE_TIER_DECLARATIONS disagree on tier."""
    mismatches = []
    for module_name, decl in tier_decls.items():
        legacy = module_tier_decls.get(module_name)
        if legacy is None:
            continue  # annex/new modules may not be in legacy dict
        legacy_tier = legacy.get("tier")
        if hasattr(legacy_tier, "value"):
            legacy_tier = legacy_tier.value
        declared_tier = decl.default_tier.value if hasattr(decl.default_tier, "value") else str(decl.default_tier)
        if str(legacy_tier) != str(declared_tier):
            mismatches.append(
                f"{module_name}: TIER_DECLARATIONS={declared_tier}, MODULE_TIER_DECLARATIONS={legacy_tier}"
            )
    return mismatches


def _check_annex_escalation(tier_decls: dict) -> list[str]:
    """Return annex modules that incorrectly have escalation_allowed=True."""
    violations = []
    for module_name, decl in tier_decls.items():
        if decl.ring == "annex" and decl.escalation_allowed:
            violations.append(f"{module_name}: annex ring must have escalation_allowed=False")
    return violations


def _print_summary_table(tier_decls: dict) -> None:
    from model_tiering import ModelTier  # noqa: PLC0415

    print("\n  Module Tier Declarations\n")
    print(f"  {'Module':<40} {'Tier':<10} {'Ring':<8} {'Budget':<14} {'Escalation'}")
    print(f"  {'-'*40} {'-'*10} {'-'*8} {'-'*14} {'-'*10}")

    for ring in ("prod", "shadow", "annex"):
        ring_decls = {k: v for k, v in tier_decls.items() if v.ring == ring}
        if not ring_decls:
            continue
        print(f"\n  [{ring.upper()}]")
        for module_name in sorted(ring_decls):
            decl = ring_decls[module_name]
            tier = decl.default_tier.value if hasattr(decl.default_tier, "value") else str(decl.default_tier)
            budget = decl.budget_class.value if hasattr(decl.budget_class, "value") else str(decl.budget_class)
            esc = "✓ allowed" if decl.escalation_allowed else "—"
            print(f"  {module_name:<40} {tier:<10} {ring:<8} {budget:<14} {esc}")

    total = len(tier_decls)
    prod_count = sum(1 for v in tier_decls.values() if v.ring == "prod")
    annex_count = sum(1 for v in tier_decls.values() if v.ring == "annex")
    shadow_count = sum(1 for v in tier_decls.values() if v.ring == "shadow")
    print(f"\n  Total: {total} ({prod_count} prod, {annex_count} annex, {shadow_count} shadow)\n")


def main() -> int:
    use_json = "--json" in sys.argv

    tier_decls, module_tier_decls, caller_map = _load_declarations()

    undeclared = _check_caller_coverage(caller_map, tier_decls)
    mismatches = _check_dict_vs_typed_sync(tier_decls, module_tier_decls)
    annex_violations = _check_annex_escalation(tier_decls)

    issues = undeclared + mismatches + annex_violations
    exit_code = 1 if issues else 0

    if use_json:
        result = {
            "exit_code": exit_code,
            "total_declared": len(tier_decls),
            "undeclared_callers": undeclared,
            "tier_sync_mismatches": mismatches,
            "annex_violations": annex_violations,
        }
        print(json.dumps(result, indent=2))
        return exit_code

    _print_summary_table(tier_decls)

    if undeclared:
        print(f"  ✗ UNDECLARED CALLERS ({len(undeclared)}) — these resolve to 'unknown' in spine:")
        for m in undeclared:
            print(f"    • {m}")
        print()

    if mismatches:
        print(f"  ✗ TIER SYNC MISMATCHES ({len(mismatches)}):")
        for m in mismatches:
            print(f"    • {m}")
        print()

    if annex_violations:
        print(f"  ✗ ANNEX ESCALATION VIOLATIONS ({len(annex_violations)}):")
        for v in annex_violations:
            print(f"    • {v}")
        print()

    if not issues:
        print(f"  ✓ All {len(tier_decls)} modules declared. No violations.\n")
    else:
        print(f"  {len(issues)} issue(s) found. Fix before next deploy.\n")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
