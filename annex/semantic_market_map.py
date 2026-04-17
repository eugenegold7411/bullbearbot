# ANNEX MODULE — lab ring, no prod pipeline imports
"""
annex/semantic_market_map.py — Semantic Market Map skeleton (T6.11).

Evaluation class: exploratory — no alpha claim
Status: Schema and artifact storage only. No clustering or visualization yet.
Upserts nodes and edges from cycle data.

Storage:
  data/annex/semantic_market_map/nodes.json
  data/annex/semantic_market_map/edges.json
Feature flag: enable_semantic_market_map (lab_flags, default False).
Promotion contract: promotion_contracts/semantic_market_map_v1.md (DRAFT).

Annex sandbox contract:
- No imports from bot.py, scheduler.py, order_executor.py, risk_kernel.py
- No writes to decision objects, strategy_config, execution paths
- Kill-switchable via feature flag
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_ANNEX_DIR = Path("data/annex/semantic_market_map")
_NODES_FILE = _ANNEX_DIR / "nodes.json"
_EDGES_FILE = _ANNEX_DIR / "edges.json"


# ─────────────────────────────────────────────────────────────────────────────
# Dataclasses
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MapNode:
    schema_version: int = 1
    node_id: str = ""
    node_type: str = ""             # "symbol" | "sector" | "catalyst_type" | "thesis_type"
    semantic_tags: list = field(default_factory=list)
    co_occurrence_count: int = 0
    last_seen: str = ""
    notes: str = ""


@dataclass
class MapEdge:
    schema_version: int = 1
    edge_id: str = ""
    node_a: str = ""
    node_b: str = ""
    relationship_type: str = ""     # "sector_peer" | "catalyst_correlation" | "thesis_overlap"
    strength: float = 0.0           # 0.0–1.0 based on co-occurrence frequency
    observation_count: int = 0
    last_updated: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_enabled() -> bool:
    try:
        from feature_flags import is_enabled  # noqa: PLC0415
        return is_enabled("enable_semantic_market_map")
    except Exception:
        return False


def _load_nodes() -> dict:
    try:
        if _NODES_FILE.exists():
            return json.loads(_NODES_FILE.read_text())
    except Exception:
        pass
    return {}


def _load_edges() -> dict:
    try:
        if _EDGES_FILE.exists():
            return json.loads(_EDGES_FILE.read_text())
    except Exception:
        pass
    return {}


def _save_nodes(nodes: dict) -> None:
    _ANNEX_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _NODES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(nodes, indent=2))
    tmp.replace(_NODES_FILE)


def _save_edges(edges: dict) -> None:
    _ANNEX_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _EDGES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(edges, indent=2))
    tmp.replace(_EDGES_FILE)


def _edge_key(node_a: str, node_b: str, relationship_type: str) -> str:
    a, b = sorted([node_a, node_b])
    return f"{a}|{b}|{relationship_type}"


def _recompute_strength(observation_count: int, max_count: int = 50) -> float:
    return round(min(1.0, observation_count / max(max_count, 1)), 4)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def upsert_node(node_id: str, node_type: str, tags: list) -> None:
    """Creates or updates a MapNode. Non-fatal."""
    try:
        if not _is_enabled():
            return
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        nodes = _load_nodes()
        if node_id in nodes:
            existing = nodes[node_id]
            existing["co_occurrence_count"] = existing.get("co_occurrence_count", 0) + 1
            existing["last_seen"] = now
            # Merge tags
            existing_tags = set(existing.get("semantic_tags", []))
            existing_tags.update(tags)
            existing["semantic_tags"] = sorted(existing_tags)
        else:
            node = MapNode(
                schema_version=1,
                node_id=node_id,
                node_type=node_type,
                semantic_tags=sorted(set(tags)),
                co_occurrence_count=1,
                last_seen=now,
            )
            nodes[node_id] = asdict(node)
        _save_nodes(nodes)
    except Exception as exc:
        log.debug("[MAP] upsert_node failed for %s: %s", node_id, exc)


def upsert_edge(node_a: str, node_b: str, relationship_type: str) -> None:
    """Creates or increments strength of a MapEdge. Non-fatal."""
    try:
        if not _is_enabled():
            return
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        edges = _load_edges()
        key = _edge_key(node_a, node_b, relationship_type)
        if key in edges:
            edges[key]["observation_count"] = edges[key].get("observation_count", 0) + 1
            edges[key]["strength"] = _recompute_strength(edges[key]["observation_count"])
            edges[key]["last_updated"] = now
        else:
            edge = MapEdge(
                schema_version=1,
                edge_id=str(uuid.uuid4()),
                node_a=node_a,
                node_b=node_b,
                relationship_type=relationship_type,
                strength=_recompute_strength(1),
                observation_count=1,
                last_updated=now,
            )
            edges[key] = asdict(edge)
        _save_edges(edges)
    except Exception as exc:
        log.debug("[MAP] upsert_edge failed for %s-%s: %s", node_a, node_b, exc)


def get_neighbors(node_id: str, min_strength: float = 0.1) -> list:
    """Returns nodes connected to node_id with strength >= min_strength."""
    try:
        edges = _load_edges()
        nodes = _load_nodes()
        neighbors = []
        for key, edge in edges.items():
            if edge.get("strength", 0) < min_strength:
                continue
            if edge.get("node_a") == node_id:
                nbr = edge.get("node_b")
            elif edge.get("node_b") == node_id:
                nbr = edge.get("node_a")
            else:
                continue
            if nbr and nbr in nodes:
                neighbors.append(nodes[nbr])
        return neighbors
    except Exception as exc:
        log.debug("[MAP] get_neighbors failed: %s", exc)
        return []


def get_map_stats() -> dict:
    """Returns {node_count, edge_count, top_nodes_by_degree}."""
    try:
        nodes = _load_nodes()
        edges = _load_edges()
        # Degree count per node
        degree: dict = {}
        for edge in edges.values():
            for n in (edge.get("node_a", ""), edge.get("node_b", "")):
                if n:
                    degree[n] = degree.get(n, 0) + 1
        top_nodes = sorted(degree.items(), key=lambda x: -x[1])[:5]
        return {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "top_nodes_by_degree": [{"node_id": n, "degree": d} for n, d in top_nodes],
        }
    except Exception as exc:
        log.debug("[MAP] get_map_stats failed: %s", exc)
        return {}


def format_map_for_review() -> str:
    """Brief stats summary with SKELETON notice."""
    try:
        stats = get_map_stats()
        if not stats or stats.get("node_count", 0) == 0:
            return ""

        lines = [
            "## Semantic Market Map — SKELETON — visualization pending\n",
            f"Nodes: {stats.get('node_count', 0)} | Edges: {stats.get('edge_count', 0)}",
        ]
        top = stats.get("top_nodes_by_degree", [])
        if top:
            top_str = ", ".join(f"{t['node_id']}(d={t['degree']})" for t in top[:3])
            lines.append(f"Top nodes by degree: {top_str}")

        return "\n".join(lines)
    except Exception as exc:
        log.warning("[MAP] format_map_for_review failed: %s", exc)
        return ""
