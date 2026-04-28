"""
trade_memory.py — Hierarchical ChromaDB vector memory for the trading bot.

Three collections with different retention horizons:
  trade_scenarios_short   — last 7 days  (weight 0.60 in retrieval)
  trade_scenarios_medium  — last 90 days (weight 0.30 in retrieval)
  trade_scenarios_long    — all-time     (weight 0.10 in retrieval)

New records always land in 'short'. Records are auto-promoted to 'medium'
after 7 days and to 'long' after 90 days. Promotion is checked lazily on
each save_trade_memory() call via _maybe_promote_aged_records().

Migration: on first init, any records in the legacy 'trade_scenarios'
collection are moved to 'trade_scenarios_short' without data loss.

All public functions degrade gracefully — the bot keeps running if
chromadb is unavailable.

Public API (unchanged signatures from v1):
  save_trade_memory(decision, market_conditions, session_tier) -> str
  update_trade_outcome(decision_id, outcome, pnl) -> None
  retrieve_similar_scenarios(market_conditions, session_tier, n_results) -> list
  format_retrieved_memories(scenarios) -> str
  get_collection_stats() -> dict

New in v2:
  promote_to_medium_term(decision_id) -> bool
  promote_to_long_term(decision_id) -> bool

Scratchpad cold storage (v3):
  save_scratchpad_memory(scratchpad)                                 -> str
  retrieve_similar_scratchpads(market_conditions, session_tier, n)   -> list
  get_scratchpad_history(days_back)                                  -> list
  get_near_miss_summary(days_back)                                   -> str
  get_two_tier_memory(market_conditions, session_tier, ...)          -> dict
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from log_setup import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_DB_PATH = str(Path(__file__).parent / "data" / "trade_memory")

# Tier weights for blended retrieval (must sum to 1.0)
_WEIGHT_SHORT  = 0.60
_WEIGHT_MEDIUM = 0.30
_WEIGHT_LONG   = 0.10

# Age thresholds for auto-promotion
_SHORT_MAX_DAYS  = 7
_MEDIUM_MAX_DAYS = 90

# ---------------------------------------------------------------------------
# Lazy singletons — initialised on first call to _get_collections()
# ---------------------------------------------------------------------------
_client            = None   # chromadb.PersistentClient or None
_coll_short        = None
_coll_medium       = None
_coll_long         = None
_collections_tried = False  # True once initialisation has been attempted

_HNSW_META = {
    "hnsw:space":           "cosine",
    "hnsw:M":               8,
    "hnsw:construction_ef": 50,
    "hnsw:search_ef":       10,
}


def _get_collections() -> tuple:
    """
    Lazy-initialise all three ChromaDB collections.

    Returns (short, medium, long) on success, (None, None, None) on failure.
    Result is cached for the process lifetime.
    """
    global _client, _coll_short, _coll_medium, _coll_long, _collections_tried

    if _collections_tried:
        return _coll_short, _coll_medium, _coll_long

    _collections_tried = True

    try:
        import chromadb  # noqa: PLC0415
        from chromadb.utils.embedding_functions import (  # noqa: PLC0415
            DefaultEmbeddingFunction,
        )

        Path(_DB_PATH).mkdir(parents=True, exist_ok=True)
        settings = chromadb.Settings(anonymized_telemetry=False)
        _client  = chromadb.PersistentClient(path=_DB_PATH, settings=settings)
        ef       = DefaultEmbeddingFunction()

        _coll_short  = _client.get_or_create_collection(
            name="trade_scenarios_short",
            embedding_function=ef,
            metadata=_HNSW_META,
        )
        _coll_medium = _client.get_or_create_collection(
            name="trade_scenarios_medium",
            embedding_function=ef,
            metadata=_HNSW_META,
        )
        _coll_long   = _client.get_or_create_collection(
            name="trade_scenarios_long",
            embedding_function=ef,
            metadata=_HNSW_META,
        )

        log.debug(
            "trade_memory: collections ready — short=%d  medium=%d  long=%d",
            _coll_short.count(), _coll_medium.count(), _coll_long.count(),
        )

        # One-time migration from legacy 'trade_scenarios' collection
        _migrate_legacy(ef)

    except ImportError:
        log.warning("trade_memory: chromadb not installed — vector memory disabled")
        _coll_short = _coll_medium = _coll_long = None
    except Exception as exc:  # pylint: disable=broad-except
        log.warning("trade_memory: ChromaDB init failed — vector memory disabled: %s", exc)
        _coll_short = _coll_medium = _coll_long = None

    return _coll_short, _coll_medium, _coll_long


def _migrate_legacy(ef) -> None:
    """
    If a legacy 'trade_scenarios' collection exists with records, migrate
    all documents to 'trade_scenarios_short' and delete the old collection.
    """
    try:
        existing = [c.name for c in _client.list_collections()]
        if "trade_scenarios" not in existing:
            return

        legacy = _client.get_collection(name="trade_scenarios", embedding_function=ef)
        total  = legacy.count()
        if total == 0:
            _client.delete_collection("trade_scenarios")
            log.info("trade_memory: deleted empty legacy collection")
            return

        log.info("trade_memory: migrating %d records from legacy 'trade_scenarios' → short", total)
        batch_size = 100
        offset     = 0
        migrated   = 0
        while True:
            chunk = legacy.get(
                limit=batch_size,
                offset=offset,
                include=["documents", "metadatas", "embeddings"],
            )
            ids = chunk.get("ids", [])
            if not ids:
                break

            docs      = chunk.get("documents", [])
            metas     = chunk.get("metadatas", [])
            embeddings = chunk.get("embeddings", [])

            # Add tier tag to metadata
            for m in metas:
                if isinstance(m, dict):
                    m["tier"] = "short"

            try:
                # embeddings may be a numpy array — check length, not truthiness
                has_embeddings = (
                    embeddings is not None and
                    hasattr(embeddings, "__len__") and
                    len(embeddings) > 0
                )
                if has_embeddings:
                    _coll_short.add(
                        ids=ids,
                        documents=docs,
                        metadatas=metas,
                        embeddings=embeddings,
                    )
                else:
                    _coll_short.add(
                        ids=ids,
                        documents=docs,
                        metadatas=metas,
                    )
                migrated += len(ids)
            except Exception as exc:
                log.warning("trade_memory: migration batch failed: %s", exc)

            offset += batch_size
            if len(ids) < batch_size:
                break

        _client.delete_collection("trade_scenarios")
        log.info("trade_memory: migration complete — %d records moved, legacy collection deleted", migrated)

    except Exception as exc:
        log.warning("trade_memory: legacy migration failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Document builders
# ---------------------------------------------------------------------------

def _build_document(
    decision: dict,
    market_conditions: dict,
    session_tier: str,
) -> str:
    session     = session_tier
    vix         = market_conditions.get("vix", "?")
    vix_regime  = market_conditions.get("vix_regime", "?")
    # Support both old format (regime/actions) and new (regime_view/ideas)
    regime      = decision.get("regime_view") or decision.get("regime", "?")

    # Support both old actions[] and new ideas[]
    ideas        = decision.get("ideas", [])
    actions_list = decision.get("actions", ideas)
    action_parts = []
    if ideas:
        for i in ideas:
            symbol   = i.get("symbol", "?")
            intent   = i.get("intent", "hold")
            tier     = i.get("tier", "?")
            catalyst = (i.get("catalyst") or "")[:80]
            action_parts.append(f"{intent.upper()} {symbol} [{tier}] catalyst={catalyst}")
    elif actions_list:
        for act in actions_list:
            symbol   = act.get("symbol", "?")
            action   = act.get("action", "?")
            tier     = act.get("tier", "?")
            catalyst = (act.get("catalyst") or "")[:80]
            action_parts.append(f"{action.upper()} {symbol} [{tier}] catalyst={catalyst}")
    actions_str = "; ".join(action_parts) if action_parts else "HOLD"

    reasoning     = (decision.get("reasoning") or "")[:300]
    intermarket   = (market_conditions.get("intermarket_signals") or "")[:200]
    breaking_news = (market_conditions.get("breaking_news") or "")[:200]

    return (
        f"session={session} vix={vix} regime={vix_regime} decision={regime} "
        f"actions: {actions_str} "
        f"reasoning: {reasoning} "
        f"intermarket: {intermarket} "
        f"news: {breaking_news}"
    )


def _build_conditions_query(market_conditions: dict, session_tier: str) -> str:
    session       = session_tier
    vix           = market_conditions.get("vix", "?")
    vix_regime    = market_conditions.get("vix_regime", "?")
    intermarket   = (market_conditions.get("intermarket_signals") or "")[:300]
    breaking_news = (market_conditions.get("breaking_news") or "")[:200]
    sector_table  = (market_conditions.get("sector_table") or "")[:200]

    return (
        f"session={session} vix={vix} regime={vix_regime} "
        f"intermarket: {intermarket} "
        f"news: {breaking_news} "
        f"sectors: {sector_table}"
    )


# ---------------------------------------------------------------------------
# Auto-promotion helpers
# ---------------------------------------------------------------------------

def run_promotion_maintenance() -> dict:
    """
    Public daily-maintenance entry point. Runs both trade and scratchpad tier
    promotion regardless of save activity. Wired into scheduler's premarket
    job (4 AM ET) so promotions happen even on days with low write activity.

    Returns a dict with before/after counts for each tier so the caller can
    log a summary line. Never raises — all internal failures swallowed.
    """
    summary: dict = {
        "trade": {"before": {}, "after": {}, "promoted_short_to_medium": 0,
                  "promoted_medium_to_long": 0},
        "scratchpad": {"before": {}, "after": {}, "promoted_short_to_medium": 0,
                       "promoted_medium_to_long": 0},
    }
    try:
        s, m, l = _get_collections()
        if all([s, m, l]):
            summary["trade"]["before"] = {
                "short": s.count(), "medium": m.count(), "long": l.count(),
            }
            _maybe_promote_aged_records()
            summary["trade"]["after"] = {
                "short": s.count(), "medium": m.count(), "long": l.count(),
            }
            summary["trade"]["promoted_short_to_medium"] = (
                summary["trade"]["before"]["short"] - summary["trade"]["after"]["short"]
            )
            summary["trade"]["promoted_medium_to_long"] = (
                summary["trade"]["after"]["long"] - summary["trade"]["before"]["long"]
            )
    except Exception as exc:
        log.warning("trade_memory: trade promotion maintenance failed: %s", exc)
    try:
        ss, sm, sl = _get_scratchpad_collections()
        if all([ss, sm, sl]):
            summary["scratchpad"]["before"] = {
                "short": ss.count(), "medium": sm.count(), "long": sl.count(),
            }
            _maybe_promote_aged_scratchpad_records()
            summary["scratchpad"]["after"] = {
                "short": ss.count(), "medium": sm.count(), "long": sl.count(),
            }
            summary["scratchpad"]["promoted_short_to_medium"] = (
                summary["scratchpad"]["before"]["short"]
                - summary["scratchpad"]["after"]["short"]
            )
            summary["scratchpad"]["promoted_medium_to_long"] = (
                summary["scratchpad"]["after"]["long"]
                - summary["scratchpad"]["before"]["long"]
            )
    except Exception as exc:
        log.warning("trade_memory: scratchpad promotion maintenance failed: %s", exc)

    log.info(
        "trade_memory: promotion maintenance done — "
        "trade s/m/l=%s/%s/%s  scratchpad s/m/l=%s/%s/%s",
        summary["trade"].get("after", {}).get("short", "?"),
        summary["trade"].get("after", {}).get("medium", "?"),
        summary["trade"].get("after", {}).get("long", "?"),
        summary["scratchpad"].get("after", {}).get("short", "?"),
        summary["scratchpad"].get("after", {}).get("medium", "?"),
        summary["scratchpad"].get("after", {}).get("long", "?"),
    )
    return summary


def _maybe_promote_aged_records() -> None:
    """
    Check short and medium collections for aged records and promote them.
    Called lazily on each save_trade_memory() to keep tiers in sync.
    No-op on any failure.
    """
    short, medium, long_ = _get_collections()
    if not all([short, medium, long_]):
        return

    now = datetime.now(timezone.utc)

    try:
        # Promote short → medium (records older than _SHORT_MAX_DAYS)
        _promote_tier(
            src=short,
            dst=medium,
            max_days=_SHORT_MAX_DAYS,
            src_tier_tag="short",
            dst_tier_tag="medium",
            now=now,
        )
    except Exception as exc:
        log.debug("trade_memory: short→medium promotion check failed: %s", exc)

    try:
        # Promote medium → long (records older than _MEDIUM_MAX_DAYS)
        _promote_tier(
            src=medium,
            dst=long_,
            max_days=_MEDIUM_MAX_DAYS,
            src_tier_tag="medium",
            dst_tier_tag="long",
            now=now,
        )
    except Exception as exc:
        log.debug("trade_memory: medium→long promotion check failed: %s", exc)


def _promote_tier(src, dst, max_days: int, src_tier_tag: str, dst_tier_tag: str,
                  now: datetime) -> None:
    """
    Move records older than max_days from src collection to dst collection.
    Skips records without a valid 'ts' metadata field.
    """
    count = src.count()
    if count == 0:
        return

    # Fetch all records from src (metadata only first to avoid loading embeddings)
    all_records = src.get(
        limit=count,
        include=["documents", "metadatas"],
    )

    ids_to_promote = []
    docs_to_add    = []
    metas_to_add   = []

    for rec_id, doc, meta in zip(
        all_records.get("ids", []),
        all_records.get("documents", []),
        all_records.get("metadatas", []),
    ):
        if not isinstance(meta, dict):
            continue
        ts_str = meta.get("ts", "")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        age_days = (now - ts).days
        if age_days >= max_days:
            ids_to_promote.append(rec_id)
            updated_meta = meta.copy()
            updated_meta["tier"] = dst_tier_tag
            docs_to_add.append(doc)
            metas_to_add.append(updated_meta)

    if not ids_to_promote:
        return

    # Add to destination (let ChromaDB re-embed)
    dst.upsert(
        ids=ids_to_promote,
        documents=docs_to_add,
        metadatas=metas_to_add,
    )
    # Remove from source
    src.delete(ids=ids_to_promote)
    log.info(
        "trade_memory: promoted %d records %s→%s",
        len(ids_to_promote), src_tier_tag, dst_tier_tag,
    )


# ---------------------------------------------------------------------------
# Public API — unchanged signatures from v1
# ---------------------------------------------------------------------------

def save_trade_memory(
    decision: dict,
    market_conditions: dict,
    session_tier: str,
) -> str:
    """
    Embed and persist a Claude decision to the short-term vector store.

    Returns a decision_id string on success, empty string on failure.
    Auto-promotes aged records across tiers as a side-effect.
    """
    short, _medium, _long = _get_collections()
    if short is None:
        return ""

    try:
        decision_id = datetime.now(timezone.utc).strftime("trade_%Y%m%d_%H%M%S_%f")
        document    = _build_document(decision, market_conditions, session_tier)

        # Support both new format (ideas[]) and legacy format (actions[])
        ideas_list = decision.get("ideas") or decision.get("actions") or []
        symbols_str = ",".join(
            a.get("symbol") or a.get("ticker") or ""
            for a in ideas_list
            if a.get("symbol") or a.get("ticker")
        )

        vix_raw = market_conditions.get("vix", 0.0)
        try:
            vix_float = float(vix_raw)
        except (TypeError, ValueError):
            vix_float = 0.0

        metadata = {
            "ts":        datetime.now(timezone.utc).isoformat(),
            "session":   session_tier,
            "regime":    str(
                decision.get("regime_view")
                or decision.get("regime")
                or "unknown"
            ),
            "n_actions": int(len(ideas_list)),
            "vix":       vix_float,
            "outcome":   "pending",
            "pnl":       0.0,
            "symbols":   symbols_str,
            "tier":      "short",
        }

        short.add(
            documents=[document],
            metadatas=[metadata],
            ids=[decision_id],
        )
        log.debug("trade_memory: saved %s to short tier (%d actions)", decision_id, len(ideas_list))

        # Lazily promote aged records across tiers
        _maybe_promote_aged_records()

        return decision_id

    except Exception as exc:  # pylint: disable=broad-except
        log.warning("trade_memory: save failed: %s", exc)
        return ""


def update_trade_outcome(decision_id: str, outcome: str, pnl: float) -> None:
    """
    Update the outcome and P&L for an existing vector record.

    Searches all three tiers for the id. No-op if not found.
    """
    if not decision_id:
        return

    short, medium, long_ = _get_collections()
    colls = [c for c in (short, medium, long_) if c is not None]
    if not colls:
        return

    for coll in colls:
        try:
            result = coll.get(ids=[decision_id], include=["metadatas", "documents"])
            if not result or not result.get("ids"):
                continue

            existing_meta = result["metadatas"][0].copy()
            existing_meta["outcome"] = outcome
            existing_meta["pnl"]     = float(pnl)

            coll.update(ids=[decision_id], metadatas=[existing_meta])
            log.debug(
                "trade_memory: updated %s outcome=%s pnl=%.2f",
                decision_id, outcome, pnl,
            )
            return  # Found and updated — done

        except Exception as exc:  # pylint: disable=broad-except
            log.debug("trade_memory: update_trade_outcome search failed in one tier: %s", exc)

    log.warning("trade_memory: update_trade_outcome — id not found in any tier: %s", decision_id)


def retrieve_similar_scenarios(
    market_conditions: dict,
    session_tier: str,
    n_results: int = 5,
) -> list[dict]:
    """
    Query all three tiers and return blended results weighted 60/30/10.

    Scoring: weighted_score = (1 - cosine_distance) * tier_weight.
    Results are sorted by weighted_score descending. Returns [] if fewer
    than 2 total records or ChromaDB is unavailable.
    """
    short, medium, long_ = _get_collections()
    if short is None:
        return []

    try:
        total = sum(c.count() for c in (short, medium, long_) if c is not None)
        if total < 2:
            return []

        query = _build_conditions_query(market_conditions, session_tier)
        candidates: list[dict] = []

        for coll, weight in (
            (short,  _WEIGHT_SHORT),
            (medium, _WEIGHT_MEDIUM),
            (long_,  _WEIGHT_LONG),
        ):
            if coll is None:
                continue
            tier_count = coll.count()
            if tier_count == 0:
                continue

            n = min(n_results, tier_count)
            try:
                raw = coll.query(
                    query_texts=[query],
                    n_results=n,
                    include=["documents", "metadatas", "distances"],
                )
            except Exception as exc:
                log.debug("trade_memory: tier query failed: %s", exc)
                continue

            for doc, meta, dist in zip(
                raw["documents"][0],
                raw["metadatas"][0],
                raw["distances"][0],
            ):
                relevance = max(0.0, 1.0 - float(dist))
                candidates.append({
                    "weighted_score": round(relevance * weight, 4),
                    "distance":       round(float(dist), 4),
                    "document":       doc,
                    "metadata":       meta,
                })

        if not candidates:
            return []

        # Deduplicate by document text (same record may appear in multiple tiers)
        seen_docs: set[str] = set()
        unique: list[dict] = []
        for c in sorted(candidates, key=lambda x: x["weighted_score"], reverse=True):
            doc_key = c["document"][:100]
            if doc_key not in seen_docs:
                seen_docs.add(doc_key)
                unique.append(c)

        return unique[:n_results]

    except Exception as exc:  # pylint: disable=broad-except
        log.warning("trade_memory: retrieve_similar_scenarios failed: %s", exc)
        return []


def format_retrieved_memories(scenarios: list[dict]) -> str:
    """
    Format retrieved similar scenarios as a human-readable prompt section.
    """
    if not scenarios:
        return "  (no similar past scenarios yet — vector memory building up)"

    lines = []
    for i, s in enumerate(scenarios, start=1):
        meta     = s.get("metadata", {})
        doc      = s.get("document", "")
        s.get("distance", 0.0)
        tier     = meta.get("tier", "short")

        ts      = str(meta.get("ts", ""))
        session = meta.get("session", "?")
        vix     = meta.get("vix", 0.0)
        regime  = meta.get("regime", "?")
        symbols = meta.get("symbols", "?")
        outcome = meta.get("outcome", "?")
        pnl     = meta.get("pnl", 0.0)

        pnl_str  = f"  P&L=${pnl:+.0f}" if outcome != "pending" else ""
        tier_tag = f"[{tier}] " if tier != "short" else ""
        line1 = (
            f"  [{i}] {tier_tag}[{ts[:16]}] sess={session} vix={vix:.1f} "
            f"regime={regime} actions={symbols} outcome={outcome}{pnl_str}"
        )

        reasoning_text = ""
        if "reasoning: " in doc:
            after = doc.split("reasoning: ", 1)[1]
            if " intermarket:" in after:
                after = after.split(" intermarket:", 1)[0]
            reasoning_text = after.strip()[:140]

        line2 = f"      reasoning: {reasoning_text}" if reasoning_text else ""

        lines.append(line1)
        if line2:
            lines.append(line2)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# New v2 public API
# ---------------------------------------------------------------------------

def promote_to_medium_term(decision_id: str) -> bool:
    """
    Manually promote a decision from short → medium tier.
    Returns True on success, False if not found or on error.
    """
    short, medium, _long = _get_collections()
    if short is None or medium is None:
        return False
    return _manual_promote(decision_id, src=short, dst=medium,
                           dst_tag="medium")


def promote_to_long_term(decision_id: str) -> bool:
    """
    Manually promote a decision to the long-term tier.
    Searches short and medium tiers for the id.
    Returns True on success, False if not found or on error.
    """
    short, medium, long_ = _get_collections()
    if long_ is None:
        return False
    for src in (c for c in (medium, short) if c is not None):
        if _manual_promote(decision_id, src=src, dst=long_, dst_tag="long"):
            return True
    return False


def _manual_promote(decision_id: str, src, dst, dst_tag: str) -> bool:
    try:
        result = src.get(
            ids=[decision_id],
            include=["documents", "metadatas"],
        )
        if not result or not result.get("ids"):
            return False

        doc  = result["documents"][0]
        meta = result["metadatas"][0].copy()
        meta["tier"] = dst_tag

        dst.upsert(ids=[decision_id], documents=[doc], metadatas=[meta])
        src.delete(ids=[decision_id])
        log.info("trade_memory: manually promoted %s → %s", decision_id, dst_tag)
        return True

    except Exception as exc:
        log.warning("trade_memory: manual promotion failed %s: %s", decision_id, exc)
        return False


def get_collection_stats() -> dict:
    """
    Return stats for all six collections (trade + scratchpad tiers).

    Keys: status ("ok" | "disabled" | "error"), short/medium/long (int counts),
    total (int), path (str), plus scr_short/scr_medium/scr_long/scr_total for
    scratchpad collections.
    """
    short, medium, long_ = _get_collections()
    if short is None:
        return {"status": "disabled", "short": 0, "medium": 0, "long": 0,
                "total": 0, "scr_short": 0, "scr_medium": 0, "scr_long": 0,
                "scr_total": 0, "path": _DB_PATH}

    try:
        sc = short.count()
        mc = medium.count() if medium is not None else 0
        lc = long_.count()  if long_  is not None else 0

        scr_s, scr_m, scr_l = _get_scratchpad_collections()
        ss = scr_s.count() if scr_s is not None else 0
        sm = scr_m.count() if scr_m is not None else 0
        sl = scr_l.count() if scr_l is not None else 0

        return {
            "status":     "ok",
            "short":      sc,
            "medium":     mc,
            "long":       lc,
            "total":      sc + mc + lc,
            "scr_short":  ss,
            "scr_medium": sm,
            "scr_long":   sl,
            "scr_total":  ss + sm + sl,
            "path":       _DB_PATH,
        }
    except Exception as exc:
        log.warning("trade_memory: get_collection_stats failed: %s", exc)
        return {"status": "error", "short": 0, "medium": 0, "long": 0,
                "total": 0, "scr_short": 0, "scr_medium": 0, "scr_long": 0,
                "scr_total": 0, "path": _DB_PATH}


# ===========================================================================
# Scratchpad cold storage — three-tier ChromaDB (scratchpad_scenarios_*)
#
# Mirrors the trade_scenarios_* pattern exactly:
#   scratchpad_scenarios_short   — last 7 days  (weight 0.60)
#   scratchpad_scenarios_medium  — last 90 days (weight 0.30)
#   scratchpad_scenarios_long    — all-time     (weight 0.10)
#
# New records land in short; _maybe_promote_aged_scratchpad_records() lazily
# promotes them using the shared _promote_tier() helper.
#
# Public scratchpad API:
#   save_scratchpad_memory(scratchpad)                                 -> str
#   retrieve_similar_scratchpads(market_conditions, session_tier, n)   -> list
#   get_scratchpad_history(days_back)                                  -> list
#   get_near_miss_summary(days_back)                                   -> str
#   get_two_tier_memory(market_conditions, session_tier, ...)          -> dict
# ===========================================================================

# ---------------------------------------------------------------------------
# Lazy singletons for scratchpad collections
# ---------------------------------------------------------------------------
_scr_short        = None
_scr_medium       = None
_scr_long         = None
_scratchpad_tried = False  # True once init has been attempted


def _get_scratchpad_collections() -> tuple:
    """
    Lazy-initialise the three scratchpad ChromaDB collections.

    Shares the same PersistentClient as the trade collections — call
    _get_collections() first so _client is already initialised.
    Returns (short, medium, long) on success, (None, None, None) on failure.
    Result is cached for the process lifetime.
    """
    global _scr_short, _scr_medium, _scr_long, _scratchpad_tried

    if _scratchpad_tried:
        return _scr_short, _scr_medium, _scr_long

    _scratchpad_tried = True

    # Ensure the main client is up (creates _client if needed)
    _get_collections()

    if _client is None:
        return None, None, None

    try:
        from chromadb.utils.embedding_functions import (  # noqa: PLC0415
            DefaultEmbeddingFunction,
        )
        ef = DefaultEmbeddingFunction()

        _scr_short  = _client.get_or_create_collection(
            name="scratchpad_scenarios_short",
            embedding_function=ef,
            metadata=_HNSW_META,
        )
        _scr_medium = _client.get_or_create_collection(
            name="scratchpad_scenarios_medium",
            embedding_function=ef,
            metadata=_HNSW_META,
        )
        _scr_long   = _client.get_or_create_collection(
            name="scratchpad_scenarios_long",
            embedding_function=ef,
            metadata=_HNSW_META,
        )

        log.debug(
            "trade_memory: scratchpad collections ready — short=%d  medium=%d  long=%d",
            _scr_short.count(), _scr_medium.count(), _scr_long.count(),
        )

    except Exception as exc:
        log.warning("trade_memory: scratchpad ChromaDB init failed: %s", exc)
        _scr_short = _scr_medium = _scr_long = None

    return _scr_short, _scr_medium, _scr_long


# ---------------------------------------------------------------------------
# Document builder for scratchpad records
# ---------------------------------------------------------------------------

def _build_scratchpad_document(scratchpad: dict) -> str:
    """
    Build an embeddable text string from a scratchpad dict.

    The format mirrors _build_document() — structured key=value text that
    embeds well for semantic cosine similarity queries.
    """
    vix          = scratchpad.get("vix", "?")
    regime_score = scratchpad.get("regime_score", "?")
    watching     = ",".join(scratchpad.get("watching", []))
    blocking_raw = scratchpad.get("blocking", [])
    blocking_str = "; ".join(str(b) for b in blocking_raw)[:200]
    triggers_raw = scratchpad.get("triggers", [])
    triggers_str = "; ".join(str(t) for t in triggers_raw)[:200]
    summary      = (scratchpad.get("summary") or "")[:150]

    return (
        f"vix={vix} regime_score={regime_score} watching={watching} "
        f"blocking: {blocking_str} "
        f"triggers: {triggers_str} "
        f"summary: {summary}"
    )


def _build_scratchpad_query(market_conditions: dict, session_tier: str) -> str:
    """
    Build a query string to find scratchpads with similar market context.
    Mirrors _build_conditions_query().
    """
    vix        = market_conditions.get("vix", "?")
    vix_regime = market_conditions.get("vix_regime", "?")
    intermarket= (market_conditions.get("intermarket_signals") or "")[:200]
    news       = (market_conditions.get("breaking_news") or "")[:150]

    return (
        f"vix={vix} regime_score={vix_regime} session={session_tier} "
        f"intermarket: {intermarket} "
        f"news: {news}"
    )


# ---------------------------------------------------------------------------
# Auto-promotion for scratchpad tiers (reuses shared _promote_tier helper)
# ---------------------------------------------------------------------------

def _maybe_promote_aged_scratchpad_records() -> None:
    """
    Lazily promote aged scratchpad records between tiers.
    Called on each save_scratchpad_memory(). No-op on any failure.
    """
    short, medium, long_ = _get_scratchpad_collections()
    if not all([short, medium, long_]):
        return

    now = datetime.now(timezone.utc)

    try:
        _promote_tier(
            src=short, dst=medium,
            max_days=_SHORT_MAX_DAYS,
            src_tier_tag="short", dst_tier_tag="medium",
            now=now,
        )
    except Exception as exc:
        log.debug("trade_memory: scratchpad short→medium promotion failed: %s", exc)

    try:
        _promote_tier(
            src=medium, dst=long_,
            max_days=_MEDIUM_MAX_DAYS,
            src_tier_tag="medium", dst_tier_tag="long",
            now=now,
        )
    except Exception as exc:
        log.debug("trade_memory: scratchpad medium→long promotion failed: %s", exc)


# ---------------------------------------------------------------------------
# Public scratchpad API
# ---------------------------------------------------------------------------

def save_scratchpad_memory(scratchpad: dict) -> str:
    """
    Embed and persist a scratchpad dict to the short-term scratchpad store.

    Returns a scratchpad_id string on success, empty string on failure.
    Auto-promotes aged scratchpad records across tiers as a side-effect.
    """
    short, _medium, _long = _get_scratchpad_collections()
    if short is None:
        return ""

    if not scratchpad:
        return ""

    try:
        scr_id   = datetime.now(timezone.utc).strftime("scr_%Y%m%d_%H%M%S_%f")
        document = _build_scratchpad_document(scratchpad)

        watching_str = ",".join(scratchpad.get("watching", []))
        summary_str  = (scratchpad.get("summary") or "")[:200]

        vix_raw = scratchpad.get("vix", 0.0)
        try:
            vix_float = float(vix_raw)
        except (TypeError, ValueError):
            vix_float = 0.0

        regime_raw = scratchpad.get("regime_score", 50)
        try:
            regime_int = int(regime_raw)
        except (TypeError, ValueError):
            regime_int = 50

        metadata = {
            "ts":           scratchpad.get("ts", datetime.now(timezone.utc).isoformat()),
            "vix":          vix_float,
            "regime_score": regime_int,
            "watching":     watching_str,
            "summary":      summary_str,
            "n_watching":   int(len(scratchpad.get("watching", []))),
            "n_blocking":   int(len(scratchpad.get("blocking", []))),
            "n_triggers":   int(len(scratchpad.get("triggers", []))),
            "tier":         "short",
        }

        short.add(
            documents=[document],
            metadatas=[metadata],
            ids=[scr_id],
        )
        log.debug(
            "trade_memory: scratchpad %s saved — watching=%s",
            scr_id, watching_str,
        )

        _maybe_promote_aged_scratchpad_records()
        return scr_id

    except Exception as exc:
        log.warning("trade_memory: save_scratchpad_memory failed: %s", exc)
        return ""


def retrieve_similar_scratchpads(
    market_conditions: dict,
    session_tier: str,
    n_results: int = 3,
) -> list[dict]:
    """
    Query all three scratchpad tiers and return blended results weighted 60/30/10.

    Same algorithm as retrieve_similar_scenarios() — cosine similarity * tier
    weight, deduped by document prefix, top-N returned.
    """
    short, medium, long_ = _get_scratchpad_collections()
    if short is None:
        return []

    try:
        total = sum(c.count() for c in (short, medium, long_) if c is not None)
        if total < 2:
            return []

        query      = _build_scratchpad_query(market_conditions, session_tier)
        candidates: list[dict] = []

        for coll, weight in (
            (short,  _WEIGHT_SHORT),
            (medium, _WEIGHT_MEDIUM),
            (long_,  _WEIGHT_LONG),
        ):
            if coll is None:
                continue
            tier_count = coll.count()
            if tier_count == 0:
                continue

            n = min(n_results, tier_count)
            try:
                raw = coll.query(
                    query_texts=[query],
                    n_results=n,
                    include=["documents", "metadatas", "distances"],
                )
            except Exception as exc:
                log.debug("trade_memory: scratchpad tier query failed: %s", exc)
                continue

            for doc, meta, dist in zip(
                raw["documents"][0],
                raw["metadatas"][0],
                raw["distances"][0],
            ):
                relevance = max(0.0, 1.0 - float(dist))
                candidates.append({
                    "weighted_score": round(relevance * weight, 4),
                    "distance":       round(float(dist), 4),
                    "document":       doc,
                    "metadata":       meta,
                })

        if not candidates:
            return []

        seen_docs: set[str] = set()
        unique: list[dict]  = []
        for c in sorted(candidates, key=lambda x: x["weighted_score"], reverse=True):
            doc_key = c["document"][:100]
            if doc_key not in seen_docs:
                seen_docs.add(doc_key)
                unique.append(c)

        return unique[:n_results]

    except Exception as exc:
        log.warning("trade_memory: retrieve_similar_scratchpads failed: %s", exc)
        return []


def get_scratchpad_history(days_back: int = 7) -> list[dict]:
    """
    Return all scratchpad records from the last days_back days, newest first.

    Scans all three tiers. Useful for weekly review agents and near-miss
    analysis. Returns [] on ChromaDB unavailability.
    """
    short, medium, long_ = _get_scratchpad_collections()
    colls = [c for c in (short, medium, long_) if c is not None]
    if not colls:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    records: list[dict] = []

    for coll in colls:
        try:
            count = coll.count()
            if count == 0:
                continue
            all_recs = coll.get(limit=count, include=["documents", "metadatas"])
            for rec_id, doc, meta in zip(
                all_recs.get("ids", []),
                all_recs.get("documents", []),
                all_recs.get("metadatas", []),
            ):
                if not isinstance(meta, dict):
                    continue
                ts_str = meta.get("ts", "")
                if not ts_str:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_str)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                if ts >= cutoff:
                    records.append({
                        "id":       rec_id,
                        "document": doc,
                        "metadata": meta,
                    })
        except Exception as exc:
            log.debug("trade_memory: get_scratchpad_history tier scan failed: %s", exc)

    # Sort newest first
    records.sort(
        key=lambda r: r["metadata"].get("ts", ""),
        reverse=True,
    )
    return records


def get_near_miss_summary(days_back: int = 7) -> str:
    """
    Analyse scratchpad history to surface repeatedly-watched symbols that
    were never unblocked — "near misses" for the weekly review agent.

    A near miss is a symbol that:
      - Appeared in watching[] in at least 2 scratchpads in the window, AND
      - Appeared in blocking[] in ≥ 50% of the scratchpads where it was watched.

    Returns a human-readable string (empty if no near misses or no history).
    """
    history = get_scratchpad_history(days_back=days_back)
    if not history:
        return ""

    from collections import defaultdict
    watched_count:  dict[str, int] = defaultdict(int)
    blocked_count:  dict[str, int] = defaultdict(int)
    trigger_map:    dict[str, list] = defaultdict(list)

    for rec in history:
        doc = rec.get("document", "")
        meta = rec.get("metadata", {})

        # Reconstruct watching list from metadata (stored as comma-joined string)
        watching_str = meta.get("watching", "")
        watching     = [s.strip() for s in watching_str.split(",") if s.strip()]

        # Extract blocking symbols from document text
        blocking_text = ""
        if "blocking: " in doc:
            after = doc.split("blocking: ", 1)[1]
            if " triggers:" in after:
                after = after.split(" triggers:", 1)[0]
            blocking_text = after.strip()

        # Extract triggers from document text
        triggers_text = ""
        if "triggers: " in doc:
            after = doc.split("triggers: ", 1)[1]
            if " summary:" in after:
                after = after.split(" summary:", 1)[0]
            triggers_text = after.strip()

        for sym in watching:
            watched_count[sym] += 1
            # Symbol is "blocked" if it appears in the blocking text
            if sym in blocking_text:
                blocked_count[sym] += 1
            # Collect trigger snippets (first 60 chars of relevant trigger)
            for trig in triggers_text.split(";"):
                if sym in trig and trig.strip() not in trigger_map[sym]:
                    trigger_map[sym].append(trig.strip()[:60])

    near_misses = [
        sym for sym, wc in watched_count.items()
        if wc >= 2 and (blocked_count[sym] / wc) >= 0.50
    ]

    if not near_misses:
        return ""

    lines = [f"Near misses (last {days_back}d):"]
    for sym in sorted(near_misses, key=lambda s: watched_count[s], reverse=True):
        wc = watched_count[sym]
        bc = blocked_count[sym]
        trigs = trigger_map[sym][:2]
        trig_str = "; ".join(trigs) if trigs else "(no triggers logged)"
        lines.append(
            f"  {sym}: watched {wc}x, blocked {bc}/{wc} times — triggers: {trig_str}"
        )
    return "\n".join(lines)


def get_two_tier_memory(
    market_conditions: dict,
    session_tier: str,
    n_trade_results: int = 5,
    n_scratchpad_results: int = 3,
) -> dict:
    """
    Retrieve both trade scenario memories and similar scratchpad memories
    in a single call.

    Returns:
        {
          "trade_scenarios":    list from retrieve_similar_scenarios(),
          "recent_scratchpads": list from retrieve_similar_scratchpads(),
        }

    Both lists may be empty if ChromaDB is unavailable or has < 2 records.
    """
    return {
        "trade_scenarios":    retrieve_similar_scenarios(
            market_conditions, session_tier, n_results=n_trade_results,
        ),
        "recent_scratchpads": retrieve_similar_scratchpads(
            market_conditions, session_tier, n_results=n_scratchpad_results,
        ),
    }
