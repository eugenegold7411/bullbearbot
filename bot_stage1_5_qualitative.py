"""
bot_stage1_5_qualitative.py — Layer 1: Sonnet qualitative sweep.

Runs asynchronously from the scheduler (never inline from run_cycle). Reads
narrative intelligence (insider filings, reddit sentiment, macro wire,
breaking news, earnings intel, regime output) and produces a per-symbol
qualitative-context record written to data/market/qualitative_context.json.

L3 (Haiku synthesis) later reads this file read-only and pairs each symbol's
qualitative tags with its L2 numerical score. Zero numerical content is
produced here — no price targets, no entry zones, no scores.

Public API
----------
run_qualitative_sweep(md, regime, symbols) -> dict
    — synchronous; returns the dict that was written to disk (or {} on error)

Scheduler helpers (called from scheduler.py):
    load_qualitative_context() -> dict
    context_age_minutes()      -> float
    news_hash_fingerprint(md)  -> str

Constraints
-----------
- Atomic write: .tmp → os.replace (same pattern as earnings_rotation).
- Non-fatal everywhere.
- Staleness: regime_context stale after 6h, per-symbol after 8h, absent
  after 24h. L3 handles "stale but present" gracefully.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_BASE = Path(__file__).parent
_OUT_PATH = _BASE / "data" / "market" / "qualitative_context.json"

_MODEL_HAIKU = "claude-haiku-4-5-20251001"

# Staleness thresholds (minutes)
_REGIME_STALE_MIN  = 360    # 6h
_SYMBOL_STALE_MIN  = 480    # 8h
_SYMBOL_ABSENT_MIN = 1440   # 24h

# Soft caps on input size — keep the prompt deterministic
_INSIDER_LINES_MAX    = 40
_REDDIT_LINES_MAX     = 25
_MACRO_WIRE_LINES_MAX = 15
_BREAKING_CHARS_MAX   = 2500
_EARNINGS_CHARS_MAX   = 2000

_SYSTEM_PROMPT = (
    "You are the qualitative-context layer for a trading bot. "
    "Your only job is to distill narrative signal (news, filings, macro wire, "
    "earnings context, reddit chatter) into structured per-symbol context tags.\n\n"
    "HARD RULES — violating any of these is a failure:\n"
    "1. NEVER emit numerical price levels, entry zones, stop levels, or targets.\n"
    "2. NEVER emit numerical scores. A separate Python layer handles scoring.\n"
    "3. `macro_beta_stress` is the ONLY numerical-adjacent field; it must be exactly "
    "one of: \"low\" | \"medium\" | \"high\".\n"
    "4. `catalyst_active` must be a short phrase (≤ 8 words), not a sentence with "
    "numbers, price levels, or ISO dates.\n"
    "5. If you have no useful information for a symbol, emit null for that symbol "
    "(do NOT omit it; do NOT fabricate).\n"
    "6. For earnings-related catalysts, reference them as 'earnings catalyst' or "
    "'pre-earnings' — never use 'today' or 'this week'. The decision layer has the "
    "structured date already.\n\n"
    "Return ONLY valid JSON matching the schema. No markdown fences."
)


# ── Disk I/O ─────────────────────────────────────────────────────────────────

def load_qualitative_context() -> dict:
    """Read qualitative_context.json. Returns {} if missing or unreadable."""
    if not _OUT_PATH.exists():
        return {}
    try:
        return json.loads(_OUT_PATH.read_text())
    except Exception as exc:
        log.debug("[L1] load_qualitative_context failed: %s", exc)
        return {}


def context_age_minutes() -> float:
    """Minutes since qualitative_context.json was last written. Returns a
    very large number if the file is missing so callers can uniformly treat
    'missing' as 'stale'."""
    ctx = load_qualitative_context()
    if not ctx:
        return 1e9
    try:
        gen = ctx.get("generated_at", "")
        if not gen:
            return 1e9
        dt = datetime.fromisoformat(gen)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
    except Exception:
        return 1e9


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    os.replace(tmp, path)


# ── News hash fingerprint (event-driven refresh) ──────────────────────────────

def news_hash_fingerprint(md: dict) -> str:
    """8-char MD5 of the breaking-news + macro-wire first chunk. Scheduler
    can compare this to the last-sweep fingerprint to decide whether to
    re-fire L1 even if the age threshold hasn't been reached yet.
    """
    try:
        breaking = (md or {}).get("breaking_news", "") or ""
        macro    = (md or {}).get("macro_wire_section", "") or ""
        mix      = (breaking[:1500] + "\n---\n" + macro[:1500])
        return hashlib.md5(mix.encode()).hexdigest()[:8]
    except Exception:
        return ""


# ── Input preparation ────────────────────────────────────────────────────────

def _truncate_lines(text: str, max_lines: int) -> str:
    if not text:
        return "(none)"
    lines = [l for l in text.splitlines() if l.strip()]
    return "\n".join(lines[:max_lines]) if lines else "(none)"


def _build_user_prompt(md: dict, regime: dict, symbols: list[str]) -> str:
    """Assemble the Sonnet user message — purely qualitative content."""
    insider   = _truncate_lines(md.get("insider_section", "") or "", _INSIDER_LINES_MAX)
    reddit    = _truncate_lines(md.get("reddit_section", "") or "",  _REDDIT_LINES_MAX)
    macro_w   = _truncate_lines(md.get("macro_wire_section", "") or "", _MACRO_WIRE_LINES_MAX)
    breaking  = (md.get("breaking_news", "") or "")[:_BREAKING_CHARS_MAX]
    earnings  = (md.get("earnings_intel_section", "") or "")[:_EARNINGS_CHARS_MAX]

    bias      = (regime or {}).get("bias", "neutral")
    theme     = (regime or {}).get("session_theme", "?")
    regime_s  = (regime or {}).get("regime_score", 50)
    macro_reg = (regime or {}).get("macro_regime", "unknown")
    constraints = (regime or {}).get("constraints", []) or []

    symbol_list = ", ".join(sorted(set(s.upper() for s in symbols if s)))

    schema_hint = (
        "{\n"
        '  "regime_context": {\n'
        '    "narrative": "<1-2 sentences, no numbers>",\n'
        '    "risk_on_catalysts":  ["<phrase>", ...],\n'
        '    "risk_off_catalysts": ["<phrase>", ...]\n'
        "  },\n"
        '  "symbol_context": {\n'
        '    "NVDA": {\n'
        '      "thesis_tags": ["ai_capex", "earnings_tailwind"],\n'
        '      "macro_beta_stress": "low|medium|high",\n'
        '      "catalyst_active": "<≤8 words or null>",\n'
        '      "catalyst_expiry_date": "YYYY-MM-DD or null",\n'
        '      "narrative": "<one sentence max>"\n'
        "    },\n"
        '    "SYMBOL_WITH_NO_INFO": null\n'
        "  }\n"
        "}"
    )

    return (
        f"Tracked symbols ({len(symbols)}): {symbol_list}\n\n"
        f"=== REGIME ===\n"
        f"bias={bias}  score={regime_s}  theme={theme}  macro_regime={macro_reg}\n"
        f"constraints: {constraints}\n\n"
        f"=== MACRO WIRE (top {_MACRO_WIRE_LINES_MAX}) ===\n{macro_w}\n\n"
        f"=== BREAKING NEWS ===\n{breaking or '(none)'}\n\n"
        f"=== INSIDER / CONGRESSIONAL (recent) ===\n{insider}\n\n"
        f"=== REDDIT / RETAIL SENTIMENT ===\n{reddit}\n\n"
        f"=== EARNINGS INTEL (next 3 days) ===\n{earnings or '(none)'}\n\n"
        f"Produce JSON matching this schema exactly. For every symbol in the "
        f"tracked list, emit either a context object OR null. Do NOT fabricate "
        f"information — null is better than invented tags.\n\n"
        f"SCHEMA:\n{schema_hint}"
    )


# ── Priority-ordered symbol selection ────────────────────────────────────────

_A1_POSITIONS_PATH   = _BASE / "data" / "market" / "signal_scores.json"
_A2_STRUCTURES_PATH  = _BASE / "data" / "account2" / "positions" / "structures.json"
_MORNING_BRIEF_PATH  = _BASE / "data" / "market" / "morning_brief.json"

# Two parallel Haiku calls: ≤47 symbols each covers a 93-symbol universe.
# Cost doubles vs the old single-call-capped-at-30 approach: ~$0.042/sweep
# (5 sweeps/day ≈ $0.21/day vs $0.10/day — negligible for the coverage gain).
_BATCH1_SIZE = 47
_BATCH2_SIZE = 46


def _get_a1_held_symbols() -> list[str]:
    """Return A1 held position symbols from signal_scores.json, preserving no order."""
    held: list[str] = []
    try:
        if not _A1_POSITIONS_PATH.exists():
            return held
        ss = json.loads(_A1_POSITIONS_PATH.read_text())
        # signal_scores.json also contains a positions snapshot under 'positions'
        positions = ss.get("positions") or {}
        if isinstance(positions, dict):
            for sym in positions:
                s = str(sym or "").upper()
                if s:
                    held.append(s)
        elif isinstance(positions, list):
            for p in positions:
                s = (p.get("symbol") or "").upper() if isinstance(p, dict) else str(p).upper()
                if s:
                    held.append(s)
    except Exception as exc:
        log.debug("[L1] held symbols read failed (non-fatal): %s", exc)
    return held


def _get_a2_underlying_symbols() -> list[str]:
    """Return underlying symbols for open A2 options structures."""
    syms: list[str] = []
    try:
        if not _A2_STRUCTURES_PATH.exists():
            return syms
        data = json.loads(_A2_STRUCTURES_PATH.read_text())
        if isinstance(data, list):
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                lifecycle = (entry.get("lifecycle") or "").lower()
                # Only open structures — anything not closed/expired/rolled_away
                if lifecycle in ("closed", "expired", "rolled_away"):
                    continue
                s = (entry.get("underlying") or entry.get("symbol") or "").upper()
                if s:
                    syms.append(s)
    except Exception as exc:
        log.debug("[L1] A2 structures read failed (non-fatal): %s", exc)
    return syms


def _get_morning_brief_symbols() -> list[str]:
    """Return conviction_picks symbols from today's morning brief."""
    syms: list[str] = []
    try:
        if not _MORNING_BRIEF_PATH.exists():
            return syms
        brief = json.loads(_MORNING_BRIEF_PATH.read_text())
        for pick in (brief.get("conviction_picks") or []):
            s = (pick.get("symbol") or "").upper() if isinstance(pick, dict) else str(pick).upper()
            if s:
                syms.append(s)
    except Exception as exc:
        log.debug("[L1] morning brief read failed (non-fatal): %s", exc)
    return syms


def _get_signal_score_ranked_symbols() -> list[str]:
    """Return all scored symbols ordered by descending score."""
    syms: list[str] = []
    try:
        if not _A1_POSITIONS_PATH.exists():
            return syms
        ss = json.loads(_A1_POSITIONS_PATH.read_text())
        scored = ss.get("scored_symbols") or {}
        if isinstance(scored, dict):
            ranked = sorted(
                scored.items(),
                key=lambda kv: float(kv[1].get("score", 0)) if isinstance(kv[1], dict) else 0,
                reverse=True,
            )
            syms = [str(sym or "").upper() for sym, _ in ranked if sym]
        elif isinstance(scored, list):
            ranked_list = sorted(
                scored,
                key=lambda x: float(x.get("score", 0)) if isinstance(x, dict) else 0,
                reverse=True,
            )
            syms = [(x.get("symbol") or "").upper() for x in ranked_list if isinstance(x, dict)]
    except Exception as exc:
        log.debug("[L1] signal score ranking failed (non-fatal): %s", exc)
    return [s for s in syms if s]


def build_priority_ordered_symbols(all_symbols: list[str]) -> list[str]:
    """Return all_symbols reordered by priority, deduplicated.

    Priority (highest to lowest):
      1. A1 held positions
      2. A2 open structure underlyings
      3. Morning brief conviction picks
      4. Highest signal-score symbols (descending)
      5. Remaining watchlist symbols (alphabetical)

    Returns a deduplicated list preserving priority order. Symbols not in
    all_symbols are ignored — the output is a permutation of all_symbols.
    """
    universe = set(s.upper() for s in all_symbols if s)
    seen: set[str] = set()
    ordered: list[str] = []

    def _add(sym: str) -> None:
        s = sym.upper()
        if s in universe and s not in seen:
            seen.add(s)
            ordered.append(s)

    for s in _get_a1_held_symbols():
        _add(s)
    for s in _get_a2_underlying_symbols():
        _add(s)
    for s in _get_morning_brief_symbols():
        _add(s)
    for s in _get_signal_score_ranked_symbols():
        _add(s)
    # Remaining symbols alphabetically
    for s in sorted(universe - seen):
        _add(s)

    return ordered


# ── Single-batch API call ────────────────────────────────────────────────────

def _run_single_batch(
    md: dict,
    regime: dict,
    symbols: list[str],
    batch_num: int,
) -> tuple[dict, dict]:
    """Run one Haiku qualitative sweep call for a slice of symbols.

    Returns (sym_ctx_out, regime_context_dict). On any failure returns ({}, {}).
    Non-fatal — the caller merges both batches and one failure doesn't block the other.
    """
    if not symbols:
        return {}, {}

    try:
        from bot_clients import MODEL_FAST as _MODEL_FAST_CONST  # noqa: PLC0415
        from bot_clients import _get_claude  # noqa: PLC0415
        model = _MODEL_FAST_CONST
    except Exception as exc:
        log.warning("[L1] batch%d bot_clients import failed: %s", batch_num, exc)
        return {}, {}

    user_content = _build_user_prompt(md, regime, symbols)

    try:
        resp = _get_claude().messages.create(
            model=model,
            max_tokens=6000,
            system=[{
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_content}],
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
    except Exception as exc:
        log.warning("[L1] batch%d API call failed (non-fatal): %s", batch_num, exc)
        return {}, {}

    try:
        from cost_tracker import get_tracker  # noqa: PLC0415
        get_tracker().record_api_call(model, resp.usage, caller="qualitative_sweep")
    except Exception:
        pass
    try:
        from cost_attribution import log_claude_call_to_spine  # noqa: PLC0415
        log_claude_call_to_spine("bot_stage1_5_qualitative", model,
                                  "qualitative_sweep", resp.usage)
    except Exception:
        pass

    raw = ""
    try:
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        parsed = json.loads(raw)
    except Exception as exc:
        log.warning("[L1] batch%d JSON parse failed: %s (raw head=%r)", batch_num, exc, raw[:120])
        return {}, {}

    if not isinstance(parsed, dict):
        log.warning("[L1] batch%d response not a dict", batch_num)
        return {}, {}

    now_iso = datetime.now(timezone.utc).isoformat()

    sym_ctx_in = parsed.get("symbol_context", {})
    if not isinstance(sym_ctx_in, dict):
        sym_ctx_in = {}
    sym_ctx_out: dict = {}
    for sym, ctx in sym_ctx_in.items():
        key = str(sym or "").upper()
        if not key:
            continue
        if ctx is None:
            sym_ctx_out[key] = None
            continue
        if not isinstance(ctx, dict):
            continue
        ctx = dict(ctx)
        for forbidden in ("entry_zone", "stop", "target", "price", "score", "entry"):
            ctx.pop(forbidden, None)
        mbs = str(ctx.get("macro_beta_stress") or "").lower()
        if mbs not in ("low", "medium", "high"):
            ctx["macro_beta_stress"] = None
        ctx["refreshed_at"] = now_iso
        sym_ctx_out[key] = ctx

    regime_ctx = parsed.get("regime_context")
    if not isinstance(regime_ctx, dict):
        regime_ctx = {}

    log.debug("[L1] batch%d complete  symbols=%d", batch_num, len(sym_ctx_out))
    return sym_ctx_out, regime_ctx


# ── Main entrypoint ──────────────────────────────────────────────────────────

def run_qualitative_sweep(
    md: dict,
    regime: dict,
    symbols: list[str],
) -> dict:
    """Run two parallel Haiku qualitative sweeps covering all symbols.

    Splits symbols into two batches (≤47 and ≤46) and runs them simultaneously
    via ThreadPoolExecutor. Results are merged into a single payload. If one
    batch fails, the other batch's results are still saved.

    Returns the dict that was written to disk. Returns {} on total failure
    (both batches fail or no symbols). The prior file on disk is left untouched
    on total failure.

    This function is synchronous. The scheduler calls it from a background
    thread so it never blocks the main cycle.
    """
    if not symbols:
        return {}

    # Priority-order before splitting so highest-priority symbols land in batch 1
    ordered = build_priority_ordered_symbols(symbols)
    if not ordered:
        ordered = [s.upper() for s in symbols if s]

    batch1 = ordered[:_BATCH1_SIZE]
    batch2 = ordered[_BATCH1_SIZE:_BATCH1_SIZE + _BATCH2_SIZE]

    t_start = time.monotonic()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        fut1 = pool.submit(_run_single_batch, md, regime, batch1, 1)
        fut2 = pool.submit(_run_single_batch, md, regime, batch2, 2) if batch2 else None

        sym_ctx1, regime_ctx1 = {}, {}
        sym_ctx2, regime_ctx2 = {}, {}

        try:
            sym_ctx1, regime_ctx1 = fut1.result(timeout=120)
        except Exception as exc:
            log.warning("[L1] batch1 result failed (non-fatal): %s", exc)

        if fut2 is not None:
            try:
                sym_ctx2, regime_ctx2 = fut2.result(timeout=120)
            except Exception as exc:
                log.warning("[L1] batch2 result failed (non-fatal): %s", exc)

    if not sym_ctx1 and not sym_ctx2:
        log.warning("[L1] both batches produced no output — sweep aborted")
        return {}

    # Merge: prefer the entry with the more recent refreshed_at if a symbol
    # somehow appears in both (should not happen, but handle gracefully).
    merged: dict = {}
    merged.update(sym_ctx1)
    for sym, ctx in sym_ctx2.items():
        if sym not in merged:
            merged[sym] = ctx
        else:
            # Keep the one with the later refreshed_at
            try:
                existing_ts = (merged[sym] or {}).get("refreshed_at", "") if isinstance(merged[sym], dict) else ""
                new_ts = (ctx or {}).get("refreshed_at", "") if isinstance(ctx, dict) else ""
                if new_ts > existing_ts:
                    merged[sym] = ctx
            except Exception:
                pass  # keep existing on any comparison error

    # Use regime_context from batch1 (covers the priority symbols); batch2 is a fallback.
    regime_context_out = regime_ctx1 if regime_ctx1 else regime_ctx2

    now_iso = datetime.now(timezone.utc).isoformat()
    news_hash = news_hash_fingerprint(md)
    all_input_syms = sorted(set(s.upper() for s in ordered if s))

    payload = {
        "generated_at":    now_iso,
        "generated_by":    "layer1_haiku",
        "model":           _MODEL_HAIKU,
        "news_hash":       news_hash,
        "regime_context":  regime_context_out,
        "symbol_context":  merged,
        "staleness_minutes": 0,
        "elapsed_seconds": round(time.monotonic() - t_start, 2),
        "input_symbols":   all_input_syms,
        "batch_count":     2 if batch2 else 1,
    }

    try:
        _atomic_write(_OUT_PATH, payload)
        log.info(
            "[L1] qualitative sweep complete  symbols=%d/%d  batches=%d  dt=%.1fs  news_hash=%s",
            len(merged), len(all_input_syms), payload["batch_count"],
            payload["elapsed_seconds"], news_hash,
        )
    except Exception as exc:
        log.warning("[L1] atomic write failed: %s", exc)
        return {}

    return payload


# ── Staleness helpers used by L3 ─────────────────────────────────────────────

def symbol_context_for(sym: str) -> Optional[dict]:
    """Return the per-symbol context dict if fresh (< 24h), else None.
    L3 uses this to decide whether to include an L1_context line."""
    ctx = load_qualitative_context()
    if not ctx:
        return None
    sc = ctx.get("symbol_context", {}) or {}
    entry = sc.get(sym.upper())
    if not isinstance(entry, dict):
        return None
    ref_at = entry.get("refreshed_at") or ctx.get("generated_at", "")
    try:
        dt = datetime.fromisoformat(ref_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
        if age_min > _SYMBOL_ABSENT_MIN:
            return None
        return entry
    except Exception:
        return None


def regime_context() -> dict:
    """Return regime_context block if fresh (< 6h), else {}."""
    ctx = load_qualitative_context()
    if not ctx:
        return {}
    try:
        dt = datetime.fromisoformat(ctx.get("generated_at", ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
        if age_min > _REGIME_STALE_MIN:
            return {}
    except Exception:
        return {}
    return ctx.get("regime_context", {}) or {}
