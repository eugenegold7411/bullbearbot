"""
thesis_research.py — Parses raw input into ThesisRecord objects via Claude (Build 2).

Uses claude-haiku-4-5-20251001 for all AI calls (cost control).
Ring 2 only — advisory shadow, never touches live execution.
Weekly cadence only — not called from the 5-minute cycle.

Zero imports from: bot.py, order_executor.py, risk_kernel.py
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from thesis_registry import (
    ThesisRecord,
    build_review_schedule,
    create_thesis,
    generate_thesis_id,
    write_quarantine,
)

log = logging.getLogger(__name__)

_HAIKU_MODEL = "claude-haiku-4-5-20251001"

# ─────────────────────────────────────────────────────────────────────────────
# Claude client — lazy init so module is importable without ANTHROPIC_API_KEY
# ─────────────────────────────────────────────────────────────────────────────

_client = None


def _get_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    return _client


# ─────────────────────────────────────────────────────────────────────────────
# Extraction prompt
# ─────────────────────────────────────────────────────────────────────────────

_EXTRACTION_SYSTEM = (
    "You are a thesis research analyst. Extract structured investment thesis data "
    "from the provided text. Return valid JSON only — no prose, no markdown, no code blocks."
)

_EXTRACTION_PROMPT = """\
Extract the following fields from this investment thesis text. Return a single JSON object.

Required JSON schema:
{{
  "title": "concise thesis name (5–10 words)",
  "narrative": "full thesis narrative in 2–4 sentences",
  "market_belief": "what this thesis believes about current market conditions",
  "market_missing": "what the market is mispricing or overlooking",
  "primary_bottleneck": "main risk or catalyst that could invalidate this thesis",
  "confirming_signals": ["2–4 signals that confirm the thesis"],
  "countersignals": ["2–3 signals that argue against the thesis"],
  "anchor_metrics": ["2–3 key metrics to track for this thesis"],
  "base_expression": {{"instrument": "equity", "symbols": ["TICKER"], "direction": "long"}},
  "alternate_expressions": [],
  "tags": ["2–4 relevant tags"],
  "time_horizons": [3, 6, 9, 12]
}}

Rules:
- Use "" (empty string) not null for missing string fields
- Use [] (empty list) not null for missing list fields
- base_expression.instrument must be one of: equity, option, etf, macro
- base_expression.direction must be: long or short
- base_expression.symbols must be a list of ticker symbols (use [""] if no specific ticker)
- time_horizons must be a list of integers (months); use [3, 6, 9, 12] as default
- Return ONLY the JSON object — no markdown fences, no explanation

Thesis text to analyze:
{text}"""


# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────

def _validate_extracted(data: dict) -> Optional[str]:
    """
    Return quarantine reason string if the extracted data fails quality checks.
    Returns None if valid.

    Preserving data quality over completeness: missing primary_bottleneck or
    ambiguous base_expression triggers quarantine rather than forced insertion.
    """
    if not data.get("title", "").strip():
        return "missing required field: title"
    if not data.get("narrative", "").strip():
        return "missing required field: narrative"
    if not data.get("market_belief", "").strip():
        return "missing required field: market_belief"
    if not data.get("primary_bottleneck", "").strip():
        return "missing required field: primary_bottleneck"

    expr = data.get("base_expression", {})
    if not isinstance(expr, dict):
        return "base_expression is not a dict"

    symbols = expr.get("symbols", [])
    if not isinstance(symbols, list) or not symbols:
        return "base_expression.symbols is missing or empty"
    if all(not str(s).strip() for s in symbols):
        return "base_expression.symbols contains only empty values — ambiguous expression"

    direction = expr.get("direction", "")
    if direction not in ("long", "short"):
        return f"base_expression.direction must be 'long' or 'short', got: {direction!r}"

    return None  # valid


# ─────────────────────────────────────────────────────────────────────────────
# Instrument inference
# ─────────────────────────────────────────────────────────────────────────────

_ETF_SYMBOLS = frozenset({
    "SPY", "QQQ", "IWM", "TLT", "VXX", "XLE", "XLF", "XBI", "XRT", "ITA",
    "GLD", "SLV", "IBIT", "FXI", "EWM", "ECH", "EWJ", "EEM", "EWY",
    "COPX", "XLU", "VNM", "EIDO",
})

_FUTURES_PREFIXES = ("CL", "NG", "HG", "ES", "NQ")


def _infer_instrument(symbol: str) -> str:
    if not symbol:
        return "equity"
    s = symbol.upper().strip()
    if s in _ETF_SYMBOLS:
        return "etf"
    if (any(s.startswith(p) for p in _FUTURES_PREFIXES)
            or any(c.isdigit() for c in s)
            or "/" in s
            or len(s) > 6):
        return "macro"
    return "equity"


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def parse_thesis_from_text(text: str, source_ref: str) -> ThesisRecord:
    """
    Send text to Claude Haiku with a structured extraction prompt.
    Returns a ThesisRecord in 'proposed' status.
    Raises on API failure or unparseable JSON response.
    """
    client = _get_client()
    prompt = _EXTRACTION_PROMPT.format(text=text)

    response = client.messages.create(
        model=_HAIKU_MODEL,
        max_tokens=1024,
        system=_EXTRACTION_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    raw_text = response.content[0].text.strip()

    # Strip markdown fences if Claude adds them despite instructions
    if raw_text.startswith("```"):
        lines    = raw_text.split("\n")
        end_idx  = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        raw_text = "\n".join(lines[1:end_idx])

    extracted = json.loads(raw_text)

    today         = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    time_horizons = extracted.get("time_horizons") or [3, 6, 9, 12]
    if not isinstance(time_horizons, list):
        time_horizons = [3, 6, 9, 12]

    return ThesisRecord(
        thesis_id=generate_thesis_id(),
        source_type="memo",
        source_ref=source_ref,
        title=extracted.get("title") or "",
        date_opened=today,
        status="proposed",
        time_horizons=time_horizons,
        narrative=extracted.get("narrative") or "",
        market_belief=extracted.get("market_belief") or "",
        market_missing=extracted.get("market_missing") or "",
        primary_bottleneck=extracted.get("primary_bottleneck") or "",
        confirming_signals=extracted.get("confirming_signals") or [],
        countersignals=extracted.get("countersignals") or [],
        anchor_metrics=extracted.get("anchor_metrics") or [],
        base_expression=extracted.get("base_expression") or {},
        alternate_expressions=extracted.get("alternate_expressions") or [],
        review_schedule=build_review_schedule(today, time_horizons),
        tags=extracted.get("tags") or [],
        archetype_candidates=[],
        notes="",
        schema_version=1,
    )


def parse_thesis_batch(items: list[dict], source_ref: str) -> list[ThesisRecord]:
    """
    Parse a list of trade ideas (from Citrini format) into ThesisRecords.
    Each item should have at minimum: title/name or symbol, thesis/narrative or
    thesis_summary, and symbols.

    Successfully parsed records receive status='researched'.
    Items that fail Claude parsing are returned with status='quarantine'.
    """
    results: list[ThesisRecord] = []

    for item in items:
        symbol    = item.get("symbol", item.get("name", ""))
        direction = item.get("direction", "long")
        thesis    = item.get("thesis_summary", item.get("thesis", item.get("narrative", "")))
        notes_raw = item.get("entry_notes", item.get("notes", ""))
        theme     = item.get("theme", "")
        rationale = item.get("rationale", "")
        symbols   = item.get("symbols", [symbol] if symbol else [])

        parts = []
        if theme:
            parts.append(f"Theme: {theme}")
        elif symbol:
            parts.append(f"Symbol: {symbol}")
            parts.append(f"Direction: {direction}")
        if thesis:
            parts.append(f"Thesis: {thesis}")
        if notes_raw:
            parts.append(f"Entry context: {notes_raw}")
        if rationale:
            parts.append(f"Rationale: {rationale}")
        if symbols and theme:
            parts.append(f"Instruments: {', '.join(str(s) for s in symbols)}")

        text = "\n".join(parts).strip()
        if not text:
            log.warning("[THESIS] parse_thesis_batch: empty text for item %s — skipping", item)
            continue

        try:
            record = parse_thesis_from_text(text, source_ref)

            # Override base_expression with structured source fields (more authoritative)
            primary_symbol = symbol if (symbol and not theme) else (symbols[0] if symbols else "")
            record.base_expression = {
                "instrument": _infer_instrument(primary_symbol),
                "symbols":    [s for s in (symbols if symbols else [symbol]) if str(s).strip()],
                "direction":  direction,
            }
            record.source_type = "batch_memo"
            record.status      = "researched"

        except Exception as exc:
            log.warning("[THESIS] parse failed for %s: %s", symbol or theme, exc)
            label = symbol or theme or item.get("name", "unknown")
            record = ThesisRecord(
                thesis_id=generate_thesis_id(),
                source_type="batch_memo",
                source_ref=source_ref,
                title=label,
                date_opened=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                status="quarantine",
                time_horizons=[],
                narrative=thesis or rationale,
                market_belief="",
                market_missing="",
                primary_bottleneck="",
                confirming_signals=[],
                countersignals=[],
                anchor_metrics=[],
                base_expression={
                    "instrument": _infer_instrument(symbol),
                    "symbols":    [s for s in (symbols if symbols else [symbol]) if str(s).strip()],
                    "direction":  direction,
                },
                alternate_expressions=[],
                review_schedule=[],
                tags=[],
                archetype_candidates=[],
                notes=f"Parse failed: {exc}",
                schema_version=1,
            )

        results.append(record)

    return results


def ingest_citrini_corpus(corpus_path: str) -> list[str]:
    """
    Read citrini_positions.json, parse each position into a ThesisRecord,
    save via thesis_registry, return list of thesis_ids created.

    Ingests both active_trades (10 items) and watchlist_themes (8 items).
    Records that fail parsing or validation are written to quarantine.jsonl.
    Logs a summary: N ingested, M quarantined with reasons.
    """
    path = Path(corpus_path)
    if not path.exists():
        raise FileNotFoundError(f"Citrini corpus not found: {corpus_path}")

    corpus     = json.loads(path.read_text())
    source_ref = path.name

    active_trades    = corpus.get("active_trades", [])
    watchlist_themes = corpus.get("watchlist_themes", [])
    items            = active_trades + watchlist_themes

    log.info(
        "[THESIS] Ingesting Citrini corpus: %d items (%d active_trades + %d watchlist_themes)",
        len(items), len(active_trades), len(watchlist_themes),
    )

    records = parse_thesis_batch(items, source_ref)

    ingested:    list[str]         = []
    quarantined: int               = 0
    quarantine_reasons: list[str]  = []

    for record in records:
        if record.status == "researched":
            # Post-parse quality gate — quarantine rather than force bad fields
            from dataclasses import asdict
            reason = _validate_extracted(asdict(record))
            if reason:
                record.status = "quarantine"
                record.notes  = (f"{record.notes}\nValidation failed: {reason}").strip()
                quarantine_reasons.append(f"{record.title}: {reason}")

        if record.status == "quarantine":
            write_quarantine(
                record_dict={
                    "thesis_id":      record.thesis_id,
                    "title":          record.title,
                    "source_ref":     source_ref,
                    "narrative":      record.narrative[:200],
                    "base_expression": record.base_expression,
                    "notes":          record.notes,
                },
                reason=record.notes or "unknown failure",
            )
            quarantined += 1
        else:
            create_thesis(record)
            ingested.append(record.thesis_id)

    log.info(
        "[THESIS] Citrini ingestion complete — ingested: %d, quarantined: %d, total: %d",
        len(ingested), quarantined, len(records),
    )
    for reason in quarantine_reasons:
        log.warning("[THESIS] Quarantine: %s", reason)

    return ingested
