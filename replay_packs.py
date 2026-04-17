"""
replay_packs.py — Replay pack library for scenario-based decision testing (P2).

Replay packs are named snapshots of inputs + expected outputs used to test
the decision engine under specific conditions. Stored in data/replay_packs/.

Public API:
    save_pack(pack)               → data/replay_packs/{pack_id}.json
    load_pack(pack_id)            → ReplayPack
    list_packs(pack_type=None)    → list[dict] summary from manifest
    build_pack_from_decision(decision_id)  → ReplayPack (from decisions.json)
    replay_pack(pack_id, fork_config)      → ReplayResult | None
    update_manifest()             → rewrites data/replay_packs/manifest.json
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_PACKS_DIR = Path("data/replay_packs")
_MANIFEST  = _PACKS_DIR / "manifest.json"
_DECISIONS = Path("memory/decisions.json")

SCHEMA_VERSION = 1

PACK_TYPES = [
    "trade_win",          # actual winning trade — test model can identify bullish signal
    "trade_loss",         # actual losing trade — test model can identify risk
    "trade_rejected",     # trade rejected by risk kernel — test kernel alignment
    "trade_near_miss",    # shadow lane near-miss — test what-if
    "weekly_review",      # weekly review scenario — replay agent inputs
    "incident",           # incident record scenario — test recovery reasoning
    "a2_approved",        # A2 options proposal that was approved
    "a2_vetoed",          # A2 options proposal that was vetoed
    "regime_transition",  # regime score shifted significantly mid-pack
    "vix_spike",          # VIX elevated scenario (>25)
    "synthetic",          # hand-crafted test scenario, no live origin
]


@dataclass
class ReplayPack:
    pack_id: str
    pack_type: str
    name: str
    description: str
    created_at: str
    schema_version: int = SCHEMA_VERSION
    decision_id: Optional[str] = None
    decision_date: Optional[str] = None
    symbol: Optional[str] = None
    inputs_snapshot: dict = field(default_factory=dict)
    original_action: Optional[str] = None   # "buy" | "sell" | "hold" | "abstain" | None
    original_outcome: Optional[str] = None  # "win" | "loss" | "pending" | None
    tags: list = field(default_factory=list)
    is_synthetic: bool = False
    source: str = "live"                     # "live" | "synthetic" | "seed"
    replay_count: int = 0
    last_replayed_at: Optional[str] = None


def _read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_pack(pack: ReplayPack) -> Path:
    """Write pack to data/replay_packs/{pack_id}.json and update manifest."""
    _PACKS_DIR.mkdir(parents=True, exist_ok=True)
    pack_path = _PACKS_DIR / f"{pack.pack_id}.json"
    pack_path.write_text(json.dumps(asdict(pack), indent=2))
    _update_manifest_entry(pack)
    return pack_path


def load_pack(pack_id: str) -> Optional[ReplayPack]:
    """Load a replay pack by ID. Returns None if not found."""
    pack_path = _PACKS_DIR / f"{pack_id}.json"
    if not pack_path.exists():
        return None
    try:
        data = json.loads(pack_path.read_text())
        return ReplayPack(**{k: v for k, v in data.items() if k in ReplayPack.__dataclass_fields__})
    except Exception as exc:
        log.warning("[REPLAY_PACKS] load_pack failed for %s: %s", pack_id, exc)
        return None


def list_packs(pack_type: Optional[str] = None) -> list[dict]:
    """Return manifest entries. Filter by pack_type if provided."""
    manifest = _read_json(_MANIFEST, {"packs": []})
    packs = manifest.get("packs", [])
    if pack_type:
        packs = [p for p in packs if p.get("pack_type") == pack_type]
    return packs


def build_pack_from_decision(
    decision_id: str,
    pack_type: str = "trade_win",
    name: Optional[str] = None,
    description: str = "",
    tags: Optional[list] = None,
) -> Optional[ReplayPack]:
    """
    Build a ReplayPack from an existing A1 decision in memory/decisions.json.
    Returns None if decision not found.
    """
    if pack_type not in PACK_TYPES:
        raise ValueError(f"pack_type must be one of {PACK_TYPES}")

    decisions_data = _read_json(_DECISIONS)
    if not decisions_data:
        return None

    # decisions.json can be a list or {"decisions": [...]}
    if isinstance(decisions_data, list):
        decisions = decisions_data
    elif isinstance(decisions_data, dict):
        decisions = decisions_data.get("decisions", [])
    else:
        return None

    record = None
    for d in decisions:
        if isinstance(d, dict) and d.get("decision_id") == decision_id:
            record = d
            break

    if record is None:
        log.debug("[REPLAY_PACKS] decision_id %s not found", decision_id)
        return None

    pack_id = f"pack_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    symbol = None
    if isinstance(record.get("ideas"), list) and record["ideas"]:
        symbol = record["ideas"][0].get("symbol")
    elif isinstance(record.get("actions"), list) and record["actions"]:
        symbol = record["actions"][0].get("symbol")

    return ReplayPack(
        pack_id=pack_id,
        pack_type=pack_type,
        name=name or f"{pack_type}_{symbol or 'unknown'}_{record.get('timestamp', '')[:10]}",
        description=description or f"Built from decision {decision_id}",
        created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        decision_id=decision_id,
        decision_date=record.get("timestamp", "")[:10],
        symbol=symbol,
        inputs_snapshot={
            "regime_score": record.get("regime_score"),
            "regime_view": record.get("regime_view"),
            "session_tier": record.get("session_tier"),
            "top_signals": record.get("scored_symbols", [])[:5],
        },
        original_action=record.get("primary_action"),
        tags=tags or [],
        is_synthetic=False,
        source="live",
    )


def replay_pack(
    pack_id: str,
    fork_config: Optional[object] = None,
) -> Optional[object]:
    """
    Replay a pack through replay_debugger.replay_a1_decision().
    Returns ReplayResult | None. Non-fatal.
    """
    pack = load_pack(pack_id)
    if pack is None:
        log.warning("[REPLAY_PACKS] pack %s not found", pack_id)
        return None

    if not pack.decision_id:
        log.debug("[REPLAY_PACKS] pack %s has no decision_id — cannot replay", pack_id)
        return None

    try:
        import replay_debugger  # noqa: PLC0415
        from replay_debugger import ForkConfig  # noqa: PLC0415

        if fork_config is None:
            fork_config = ForkConfig(fork_reason=f"replay_pack:{pack_id}")

        result = replay_debugger.replay_a1_decision(
            decision_id=pack.decision_id,
            fork_config=fork_config,
        )

        # Update replay stats
        pack.replay_count += 1
        pack.last_replayed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        save_pack(pack)

        return result
    except Exception as exc:
        log.warning("[REPLAY_PACKS] replay_pack failed for %s: %s", pack_id, exc)
        return None


def build_synthetic_pack(
    name: str,
    description: str,
    inputs_snapshot: dict,
    pack_type: str = "synthetic",
    tags: Optional[list] = None,
) -> ReplayPack:
    """Build a synthetic (hand-crafted) replay pack with no live decision origin."""
    if pack_type not in PACK_TYPES:
        raise ValueError(f"pack_type must be one of {PACK_TYPES}")
    pack_id = f"pack_syn_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    return ReplayPack(
        pack_id=pack_id,
        pack_type=pack_type,
        name=name,
        description=description,
        created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        inputs_snapshot=inputs_snapshot,
        tags=tags or [],
        is_synthetic=True,
        source="synthetic",
    )


def _update_manifest_entry(pack: ReplayPack) -> None:
    """Add or update a pack's summary entry in manifest.json."""
    _PACKS_DIR.mkdir(parents=True, exist_ok=True)
    manifest = _read_json(_MANIFEST, {"schema_version": SCHEMA_VERSION, "packs": []})
    packs = manifest.get("packs", [])
    summary = {
        "pack_id": pack.pack_id,
        "pack_type": pack.pack_type,
        "name": pack.name,
        "symbol": pack.symbol,
        "is_synthetic": pack.is_synthetic,
        "source": pack.source,
        "created_at": pack.created_at,
        "replay_count": pack.replay_count,
        "tags": pack.tags,
    }
    existing_ids = {p["pack_id"] for p in packs}
    if pack.pack_id in existing_ids:
        packs = [summary if p["pack_id"] == pack.pack_id else p for p in packs]
    else:
        packs.append(summary)
    manifest["packs"] = packs
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    manifest["total"] = len(packs)
    _MANIFEST.write_text(json.dumps(manifest, indent=2))


def update_manifest() -> Path:
    """Rebuild manifest.json from all pack files in data/replay_packs/."""
    _PACKS_DIR.mkdir(parents=True, exist_ok=True)
    packs = []
    for pack_file in sorted(_PACKS_DIR.glob("pack_*.json")):
        try:
            data = json.loads(pack_file.read_text())
            packs.append({
                "pack_id": data.get("pack_id"),
                "pack_type": data.get("pack_type"),
                "name": data.get("name"),
                "symbol": data.get("symbol"),
                "is_synthetic": data.get("is_synthetic", False),
                "source": data.get("source", "live"),
                "created_at": data.get("created_at"),
                "replay_count": data.get("replay_count", 0),
                "tags": data.get("tags", []),
            })
        except Exception as exc:
            log.warning("[REPLAY_PACKS] manifest: skipping %s: %s", pack_file, exc)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "total": len(packs),
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "packs": packs,
    }
    _MANIFEST.write_text(json.dumps(manifest, indent=2))
    return _MANIFEST
