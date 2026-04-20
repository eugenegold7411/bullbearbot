"""
scripts/replay_a2_decision.py — Offline A2 decision pipeline replay.

Usage:
    python3 scripts/replay_a2_decision.py --id a2_dec_20260421_093012
    python3 scripts/replay_a2_decision.py --id a2_dec_20260421_093012 --live
    python3 scripts/replay_a2_decision.py --id a2_dec_20260421_093012 --json

In offline mode (default): reconstructs A2FeaturePack objects from stored
candidate_sets and re-runs Stage 2 routing + veto deterministically.
The debate stage is NOT replayed (Claude output is non-deterministic).

Returns a dict:
    {
        "original": dict  — the stored A2DecisionRecord,
        "replayed": dict  — new record with re-derived routing/veto,
        "diff":     dict  — per-symbol routing differences (empty = identical),
        "match":    bool  — True if routing+veto decisions are identical,
    }
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── Pack reconstruction ───────────────────────────────────────────────────────

def _reconstruct_pack(pack_dict: dict):
    """
    Reconstruct an A2FeaturePack dataclass from a stored dict.
    All fields default gracefully — returns None only on import failure.
    """
    try:
        from schemas import A2FeaturePack  # noqa: PLC0415
        return A2FeaturePack(
            symbol               = str(pack_dict.get("symbol", "")),
            a1_signal_score      = float(pack_dict.get("a1_signal_score", 0.0)),
            a1_direction         = str(pack_dict.get("a1_direction", "neutral")),
            trend_score          = pack_dict.get("trend_score"),
            momentum_score       = pack_dict.get("momentum_score"),
            sector_alignment     = str(pack_dict.get("sector_alignment", "")),
            iv_rank              = float(pack_dict.get("iv_rank", 50.0)),
            iv_environment       = str(pack_dict.get("iv_environment", "neutral")),
            term_structure_slope = pack_dict.get("term_structure_slope"),
            skew                 = pack_dict.get("skew"),
            expected_move_pct    = float(pack_dict.get("expected_move_pct", 0.0)),
            flow_imbalance_30m   = pack_dict.get("flow_imbalance_30m"),
            sweep_count          = pack_dict.get("sweep_count"),
            gex_regime           = pack_dict.get("gex_regime"),
            oi_concentration     = pack_dict.get("oi_concentration"),
            earnings_days_away   = pack_dict.get("earnings_days_away"),
            macro_event_flag     = bool(pack_dict.get("macro_event_flag", False)),
            premium_budget_usd   = float(pack_dict.get("premium_budget_usd", 5000.0)),
            liquidity_score      = float(pack_dict.get("liquidity_score", 0.5)),
            built_at             = str(pack_dict.get("built_at", "")),
            data_sources         = list(pack_dict.get("data_sources", [])),
        )
    except Exception as exc:
        print(f"[REPLAY] pack reconstruction failed for {pack_dict.get('symbol', '?')}: {exc}",
              file=sys.stderr)
        return None


# ── Routing + veto replay ─────────────────────────────────────────────────────

def _replay_routing(original_record: dict) -> tuple[list[dict], dict]:
    """
    Re-run Stage 2 routing + veto on stored packs from a serialized A2DecisionRecord.

    Returns:
        (replayed_candidate_sets, routing_diff)
        routing_diff: per-symbol dict of changed fields — empty dict = identical
    """
    from bot_options_stage2_structures import (  # noqa: PLC0415
        _route_strategy, _apply_veto_rules, _infer_router_rule_fired,
    )

    replayed_sets: list[dict] = []
    routing_diff: dict[str, dict] = {}

    for cs in original_record.get("candidate_sets", []):
        pack_dict = cs.get("pack") or {}
        if not pack_dict:
            replayed_sets.append(dict(cs))
            continue

        pack = _reconstruct_pack(pack_dict)
        if pack is None:
            replayed_sets.append(dict(cs))
            continue

        # Derive equity from premium_budget (budget = equity × 0.05)
        equity = (pack.premium_budget_usd / 0.05
                  if pack.premium_budget_usd > 0 else 100_000.0)

        # Re-run deterministic routing
        allowed_replayed    = _route_strategy(pack)
        rule_fired_replayed = _infer_router_rule_fired(pack, allowed_replayed)

        # Re-run veto on original generated candidates
        vetoed_replayed:    list[dict] = []
        surviving_replayed: list[dict] = []
        for c in cs.get("generated_candidates", []):
            reason = _apply_veto_rules(c, pack, equity)
            if reason is not None:
                vetoed_replayed.append({
                    "candidate_id": c.get("candidate_id", "?"),
                    "reason": reason,
                })
            else:
                surviving_replayed.append(c)

        # Build replayed candidate set
        replayed_cs = dict(cs)
        replayed_cs["allowed_structures"]   = allowed_replayed
        replayed_cs["router_rule_fired"]    = rule_fired_replayed
        replayed_cs["vetoed_candidates"]    = vetoed_replayed
        replayed_cs["surviving_candidates"] = surviving_replayed
        replayed_sets.append(replayed_cs)

        # Per-symbol diff (only fields that changed)
        sym = pack.symbol
        sym_diff: dict = {}

        if cs.get("router_rule_fired") != rule_fired_replayed:
            sym_diff["router_rule_fired"] = {
                "original": cs.get("router_rule_fired"),
                "replayed": rule_fired_replayed,
            }
        if sorted(cs.get("allowed_structures") or []) != sorted(allowed_replayed):
            sym_diff["allowed_structures"] = {
                "original": cs.get("allowed_structures"),
                "replayed": allowed_replayed,
            }
        orig_reasons  = sorted(v.get("reason", "") for v in cs.get("vetoed_candidates", []))
        rply_reasons  = sorted(v.get("reason", "") for v in vetoed_replayed)
        if orig_reasons != rply_reasons:
            sym_diff["veto_reasons"] = {
                "original": orig_reasons,
                "replayed": rply_reasons,
            }

        if sym_diff:
            routing_diff[sym] = sym_diff

    return replayed_sets, routing_diff


# ── Public API ────────────────────────────────────────────────────────────────

def replay_decision(decision_id: str, offline: bool = True) -> dict:
    """
    Load a stored A2DecisionRecord and replay the deterministic pipeline stages.

    offline=True  — use stored packs; re-run routing+veto only (deterministic).
                    Claude debate is NOT replayed (non-deterministic).
    offline=False — same replay, but prints a warning that live chain data
                    may diverge from stored pack values.

    Returns:
    {
        "original": dict  — stored A2DecisionRecord (or None if not found),
        "replayed": dict  — new record with re-derived routing/veto (or None),
        "diff":     dict  — per-symbol routing differences (empty = identical),
        "match":    bool  — True if all routing+veto decisions are identical,
    }
    """
    import a2_decision_store  # noqa: PLC0415

    original = a2_decision_store.get_decision_by_id(decision_id)
    if original is None:
        return {
            "original": None,
            "replayed": None,
            "diff": {"error": f"decision_id={decision_id!r} not found"},
            "match": False,
        }

    if not offline:
        print(
            "[REPLAY] offline=False: routing replay uses stored pack values. "
            "Live chain data would produce different IV/liquidity/flow fields.",
            file=sys.stderr,
        )

    replayed_sets, routing_diff = _replay_routing(original)

    replayed: dict = dict(original)
    replayed["candidate_sets"] = replayed_sets
    replayed["_replay_note"] = (
        "Routing + veto re-derived from stored packs. "
        "Claude debate not replayed (non-deterministic)."
    )
    replayed["_replayed_at"] = datetime.now(timezone.utc).isoformat()

    return {
        "original": original,
        "replayed": replayed,
        "diff":     routing_diff,
        "match":    len(routing_diff) == 0,
    }


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay a stored A2 decision through the deterministic pipeline stages.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--id", required=True, metavar="DECISION_ID",
                        help="decision_id to replay (e.g. a2_dec_20260421_093012)")
    parser.add_argument("--live", action="store_true", default=False,
                        help="offline=False mode (warns about live data divergence)")
    parser.add_argument("--json", action="store_true", dest="json_out",
                        help="Output full result as JSON to stdout")
    args = parser.parse_args()

    result = replay_decision(decision_id=args.id, offline=not args.live)

    if args.json_out:
        print(json.dumps(result, default=str, indent=2))
        return

    print(f"\n── A2 Replay: {args.id} ──────────────────────────────")

    if result["original"] is None:
        print(f"  ERROR: {result['diff'].get('error', 'record not found')}")
        sys.exit(1)

    orig  = result["original"]
    match = result["match"]
    diff  = result["diff"]
    n_sets = len(orig.get("candidate_sets", []))

    print(f"  Original no_trade_reason : {orig.get('no_trade_reason')}")
    print(f"  Original execution_result: {orig.get('execution_result')}")
    print(f"  Candidate sets           : {n_sets}")
    print(f"  Routing/veto match       : {'✓ IDENTICAL' if match else '✗ CHANGED'}")

    if diff:
        print("\n  Routing differences detected:")
        for sym, changes in diff.items():
            print(f"    {sym}:")
            for field, vals in changes.items():
                print(f"      {field}:")
                print(f"        original: {vals['original']}")
                print(f"        replayed: {vals['replayed']}")
    else:
        print("  No routing/veto changes detected.")

    print()


if __name__ == "__main__":
    main()
