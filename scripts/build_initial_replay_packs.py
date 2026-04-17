"""
build_initial_replay_packs.py — Seed data/replay_packs/ with initial packs.

Creates at minimum 2 synthetic replay packs representing archetypal scenarios.
Safe to run multiple times — does not overwrite existing pack IDs (by timestamp).

Usage:
    python3 scripts/build_initial_replay_packs.py
    python3 scripts/build_initial_replay_packs.py --from-decisions   # also seed from live decisions
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _build_synthetic_packs() -> list:
    """Create 2 synthetic packs covering crisis and golden path scenarios."""
    from replay_packs import build_synthetic_pack, save_pack  # noqa: PLC0415

    packs = []

    # Pack 1: Crisis regime — VIX spike, signals conflicting
    crisis = build_synthetic_pack(
        name="crisis_regime_vix_spike",
        description=(
            "Synthetic: VIX > 35, signals conflicting, regime_score = 18 (defensive). "
            "Tests whether main decision correctly abstains or recommends cash."
        ),
        inputs_snapshot={
            "regime_score": 18,
            "regime_view": "bearish",
            "vix": 38.5,
            "signals_conflict": True,
            "catalyst_count": 2,
            "open_position_count": 3,
            "top_signals": [
                {"symbol": "GLD", "score": 72, "direction": "long", "catalyst": "safe haven demand"},
                {"symbol": "SPY", "score": 41, "direction": "short", "catalyst": "macro deterioration"},
                {"symbol": "NVDA", "score": 38, "direction": "long", "catalyst": "earnings beat"},
            ],
            "session_tier": "market",
            "macro_backdrop": "VIX spike, credit stress elevated, risk-off environment",
        },
        pack_type="vix_spike",
        tags=["crisis", "vix_above_35", "risk_off", "seed"],
    )
    path = save_pack(crisis)
    packs.append({"name": crisis.name, "pack_id": crisis.pack_id, "path": str(path)})
    print(f"  ✓ Created: {crisis.name}  [{crisis.pack_id}]")

    # Pack 2: Golden path — clear bullish regime, strong signal convergence
    golden = build_synthetic_pack(
        name="golden_path_bullish_convergence",
        description=(
            "Synthetic: regime_score = 78 (bullish), top signals all agree, "
            "catalyst_count = 1 (earnings beat), no conflict. "
            "Tests whether model correctly identifies high-conviction entry."
        ),
        inputs_snapshot={
            "regime_score": 78,
            "regime_view": "bullish",
            "vix": 14.2,
            "signals_conflict": False,
            "catalyst_count": 1,
            "open_position_count": 1,
            "top_signals": [
                {"symbol": "NVDA", "score": 88, "direction": "long", "catalyst": "earnings beat +15%"},
                {"symbol": "TSM", "score": 79, "direction": "long", "catalyst": "AI demand commentary"},
                {"symbol": "QQQ", "score": 71, "direction": "long", "catalyst": "tech sector rally"},
            ],
            "session_tier": "market",
            "macro_backdrop": "Goldilocks: low VIX, AI infrastructure demand, no macro headwinds",
        },
        pack_type="trade_win",
        tags=["bullish", "convergence", "high_conviction", "seed"],
    )
    path = save_pack(golden)
    packs.append({"name": golden.name, "pack_id": golden.pack_id, "path": str(path)})
    print(f"  ✓ Created: {golden.name}  [{golden.pack_id}]")

    return packs


def _build_packs_from_decisions(max_packs: int = 5) -> list:
    """Attempt to build packs from the most recent live decisions."""
    from replay_packs import build_pack_from_decision, save_pack  # noqa: PLC0415

    decisions_path = Path("memory/decisions.json")
    if not decisions_path.exists():
        print("  [SKIP] memory/decisions.json not found — skipping live decision packs")
        return []

    try:
        data = json.loads(decisions_path.read_text())
    except Exception as exc:
        print(f"  [SKIP] Cannot read decisions.json: {exc}")
        return []

    if isinstance(data, list):
        decisions = data
    elif isinstance(data, dict):
        decisions = data.get("decisions", [])
    else:
        return []

    if not decisions:
        print("  [SKIP] No decisions found in decisions.json")
        return []

    # Take most recent N decisions with a decision_id
    recent = [d for d in reversed(decisions) if isinstance(d, dict) and d.get("decision_id")][:max_packs]
    if not recent:
        print("  [SKIP] No decisions with decision_id found")
        return []

    packs = []
    for d in recent:
        try:
            pack = build_pack_from_decision(
                decision_id=d["decision_id"],
                pack_type="trade_win",  # default; operator can reclassify
                tags=["seed", "live"],
            )
            if pack:
                path = save_pack(pack)
                packs.append({"name": pack.name, "pack_id": pack.pack_id, "path": str(path)})
                print(f"  ✓ Created from decision: {pack.name}  [{pack.pack_id}]")
        except Exception as exc:
            print(f"  [WARN] Could not build pack from {d.get('decision_id')}: {exc}")

    return packs


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed initial replay packs")
    parser.add_argument("--from-decisions", action="store_true", dest="from_decisions",
                        help="Also build packs from recent live decisions")
    args = parser.parse_args()

    try:
        from replay_packs import update_manifest  # noqa: PLC0415
    except ImportError as exc:
        print(f"[ERROR] Cannot import replay_packs: {exc}", file=sys.stderr)
        return 1

    print("\n  Building initial replay packs...\n")
    all_packs = []

    all_packs.extend(_build_synthetic_packs())

    if args.from_decisions:
        all_packs.extend(_build_packs_from_decisions())

    manifest_path = update_manifest()
    print(f"\n  {len(all_packs)} pack(s) created")
    print(f"  Manifest: {manifest_path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
