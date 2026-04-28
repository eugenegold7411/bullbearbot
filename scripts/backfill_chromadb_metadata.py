"""
S7-D — ChromaDB metadata backfill.

Scans all three trade-scenario ChromaDB tiers for records with degraded
document text (produced before the BUG-012 fix on 2026-04-15).

Degraded records contain the literal strings "regime=?" or "actions: HOLD"
in the document body, which makes their semantic embeddings nearly identical
and degrades similarity search.

For each degraded record whose id is found in memory/decisions.json
(matched via vector_id field), the document text is regenerated using
_build_document() and re-upserted (ChromaDB re-embeds on upsert with new text).

Records without a matching decision entry are left in place — their text
cannot be safely reconstructed.

Usage (run from the repo root):
    python3 scripts/backfill_chromadb_metadata.py [--dry-run]

Safe defaults used for missing market_conditions fields:
    session:  "unknown"  — don't assert a session that wasn't recorded
    vix:       0.0       — clear sentinel, won't be mistaken for a real reading
"""
import json
import sys
from pathlib import Path

# ── Ensure repo root is on sys.path ─────────────────────────────────────────
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

_DRY_RUN = "--dry-run" in sys.argv

_DECISIONS_FILE = _REPO / "memory" / "decisions.json"
_DEGRADED_PATTERNS = ("regime=?", "actions: HOLD")


def _is_degraded(doc: str) -> bool:
    return any(pat in doc for pat in _DEGRADED_PATTERNS)


def _load_decisions_by_vector_id() -> dict:
    """Return {vector_id: decision_dict} from memory/decisions.json."""
    try:
        raw = json.loads(_DECISIONS_FILE.read_text())
        result = {}
        for d in raw if isinstance(raw, list) else []:
            vid = d.get("vector_id")
            if vid:
                result[vid] = d
        print(f"  Loaded {len(result)} decisions with vector_id from decisions.json")
        return result
    except Exception as exc:
        print(f"  ERROR loading decisions.json: {exc}")
        return {}


def _rebuild_document(decision: dict, session: str) -> str:
    """
    Regenerate document text using trade_memory._build_document().
    market_conditions filled with safe sentinels for fields not in decisions.json.
    """
    from trade_memory import _build_document  # noqa: PLC0415

    market_conditions = {
        "vix":                0.0,      # safe sentinel per S7-D confirmation
        "vix_regime":         "unknown",
        "intermarket_signals": decision.get("notes", ""),
        "breaking_news":      "",
    }
    return _build_document(decision, market_conditions, session)


def _backfill_collection(coll, name: str, decisions_by_vid: dict) -> dict:
    """
    Scan one collection, regenerate degraded documents, and upsert.
    Returns stats dict.
    """
    stats = {"scanned": 0, "degraded": 0, "rebuilt": 0, "skipped_no_match": 0}

    try:
        total = coll.count()
        if total == 0:
            print(f"  [{name}] empty — skipping")
            return stats

        # Fetch in batches of 200 to avoid memory pressure
        batch_size = 200
        offset = 0

        ids_to_upsert   = []
        docs_to_upsert  = []
        metas_to_upsert = []

        while offset < total:
            chunk = coll.get(
                limit=batch_size,
                offset=offset,
                include=["documents", "metadatas"],
            )
            offset += batch_size

            chunk_ids   = chunk.get("ids", [])
            chunk_docs  = chunk.get("documents", [])
            chunk_metas = chunk.get("metadatas", [])

            for rec_id, doc, meta in zip(chunk_ids, chunk_docs, chunk_metas):
                stats["scanned"] += 1

                if not _is_degraded(doc):
                    continue

                stats["degraded"] += 1
                decision = decisions_by_vid.get(rec_id)
                if decision is None:
                    stats["skipped_no_match"] += 1
                    continue

                # Recover session from metadata; fall back to "unknown"
                session = meta.get("session", "unknown")
                if not session or session == "?":
                    session = "unknown"

                new_doc = _rebuild_document(decision, session)

                # Patch metadata: normalize "?" regime sentinel
                new_meta = meta.copy()
                if new_meta.get("regime") == "?":
                    new_meta["regime"] = "unknown"

                ids_to_upsert.append(rec_id)
                docs_to_upsert.append(new_doc)
                metas_to_upsert.append(new_meta)
                stats["rebuilt"] += 1

        if ids_to_upsert and not _DRY_RUN:
            coll.upsert(
                ids=ids_to_upsert,
                documents=docs_to_upsert,
                metadatas=metas_to_upsert,
            )
            print(
                f"  [{name}] upserted {len(ids_to_upsert)} rebuilt documents "
                f"(re-embedded by ChromaDB)"
            )
        elif ids_to_upsert:
            print(
                f"  [{name}] DRY-RUN: would upsert {len(ids_to_upsert)} documents"
            )

    except Exception as exc:
        print(f"  [{name}] ERROR during backfill: {exc}")

    return stats


def main() -> None:
    import os  # noqa: PLC0415

    # Load .env if present
    try:
        from dotenv import load_dotenv  # noqa: PLC0415
        load_dotenv(_REPO / ".env")
    except ImportError:
        pass

    # Set protocol-buffers env var required by ChromaDB
    os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")

    if _DRY_RUN:
        print("=== DRY-RUN mode — no writes will occur ===\n")

    print("Loading decisions index...")
    decisions_by_vid = _load_decisions_by_vector_id()

    print("\nInitialising ChromaDB collections...")
    try:
        import trade_memory  # noqa: PLC0415
        short, medium, long_ = trade_memory._get_collections()  # noqa: SLF001
    except Exception as exc:
        print(f"  ERROR: could not open ChromaDB: {exc}")
        sys.exit(1)

    if short is None:
        print("  ERROR: ChromaDB unavailable — exiting")
        sys.exit(1)

    totals = {"scanned": 0, "degraded": 0, "rebuilt": 0, "skipped_no_match": 0}

    for coll, name in [
        (short,  "short"),
        (medium, "medium"),
        (long_,  "long"),
    ]:
        if coll is None:
            print(f"  [{name}] collection unavailable — skipping")
            continue
        print(f"\n[{name}] count={coll.count()}")
        stats = _backfill_collection(coll, name, decisions_by_vid)
        for k, v in stats.items():
            totals[k] = totals.get(k, 0) + v
        print(
            f"  [{name}] scanned={stats['scanned']}  degraded={stats['degraded']}  "
            f"rebuilt={stats['rebuilt']}  skipped_no_match={stats['skipped_no_match']}"
        )

    print(
        f"\n=== TOTAL  scanned={totals['scanned']}  degraded={totals['degraded']}  "
        f"rebuilt={totals['rebuilt']}  skipped_no_match={totals['skipped_no_match']} ==="
    )
    if _DRY_RUN:
        print("DRY-RUN complete — re-run without --dry-run to apply changes.")


if __name__ == "__main__":
    main()
