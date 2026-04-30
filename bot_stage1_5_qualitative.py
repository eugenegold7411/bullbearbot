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

_MODEL_SONNET = "claude-sonnet-4-6"

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


# ── Main entrypoint ──────────────────────────────────────────────────────────

def run_qualitative_sweep(
    md: dict,
    regime: dict,
    symbols: list[str],
) -> dict:
    """Run a single Sonnet qualitative sweep and persist the result.

    Returns the dict that was written to disk. Returns {} on any error
    (the prior file on disk is left untouched).

    This function is synchronous. The scheduler calls it from a background
    thread so it never blocks the main cycle.
    """
    if not symbols:
        return {}

    # Cap to 30 symbols — ~100 output tokens/symbol × 80 symbols was hitting
    # the 8192 max_tokens ceiling every call ($0.12/call × 28 calls/day = $3.45/day).
    # 30 symbols × ~100 tokens + ~300 overhead ≈ 3300 tokens, well within 6000.
    if len(symbols) > 30:
        symbols = symbols[:30]

    try:
        from bot_clients import MODEL as _MODEL_SONNET_CONST  # noqa: PLC0415
        from bot_clients import _get_claude  # noqa: PLC0415
        model = _MODEL_SONNET_CONST  # canonical "claude-sonnet-4-6"
    except Exception as exc:
        log.warning("[L1] bot_clients import failed: %s", exc)
        return {}

    user_content = _build_user_prompt(md, regime, symbols)

    t_start = time.monotonic()
    try:
        # max_tokens budget: ~100 tokens/symbol × 30 symbols + ~300 overhead ≈ 3300.
        # 6000 provides headroom without hitting the 8192 ceiling that was
        # truncating output and causing parse failures on 57% of calls.
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
        log.warning("[L1] Sonnet call failed: %s", exc)
        return {}

    # Cost tracking (non-fatal)
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
        log.warning("[L1] JSON parse failed: %s (raw head=%r)", exc, raw[:120])
        return {}

    if not isinstance(parsed, dict):
        log.warning("[L1] response not a dict")
        return {}

    now_iso = datetime.now(timezone.utc).isoformat()
    news_hash = news_hash_fingerprint(md)

    # Normalize symbol keys to upper and stamp refreshed_at per entry
    sym_ctx_in = parsed.get("symbol_context", {}) if isinstance(parsed.get("symbol_context"), dict) else {}
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
        # Strip anything that looks like a numeric field — belt-and-braces
        # in case Sonnet ignores the system prompt and includes a price.
        ctx = dict(ctx)
        for forbidden in ("entry_zone", "stop", "target", "price", "score", "entry"):
            ctx.pop(forbidden, None)
        mbs = str(ctx.get("macro_beta_stress") or "").lower()
        if mbs not in ("low", "medium", "high"):
            ctx["macro_beta_stress"] = None
        ctx["refreshed_at"] = now_iso
        sym_ctx_out[key] = ctx

    payload = {
        "generated_at":    now_iso,
        "generated_by":    "layer1_sonnet",
        "model":           model,
        "news_hash":       news_hash,
        "regime_context":  parsed.get("regime_context") if isinstance(parsed.get("regime_context"), dict) else {},
        "symbol_context":  sym_ctx_out,
        "staleness_minutes": 0,
        "elapsed_seconds": round(time.monotonic() - t_start, 2),
        "input_symbols":   sorted(set(s.upper() for s in symbols if s)),
    }

    try:
        _atomic_write(_OUT_PATH, payload)
        log.info(
            "[L1] qualitative sweep complete  symbols=%d  dt=%.1fs  news_hash=%s",
            len(sym_ctx_out), payload["elapsed_seconds"], news_hash,
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
