"""
scratchpad.py — Stage 2.5: Haiku pre-analysis between signal scoring and
the main Sonnet decision.

Produces a structured scratchpad that focuses the main decision on:
  - watching     : symbols worth active attention this cycle
  - blocking     : conditions preventing new entries (market-wide or per-symbol)
  - triggers     : specific price/volume/catalyst conditions that would unlock entry
  - conviction_ranking : ordered list of symbols by conviction with notes
  - summary      : one-sentence overall read

Hot memory (last 20 scratchpads) is kept as a rolling JSON file at
  data/memory/hot_scratchpads.json

Cold vector memory (ChromaDB scratchpad_scenarios_short/medium/long) is managed
by trade_memory.py (see save_scratchpad_memory / retrieve_similar_scratchpads).

Public API:
  run_scratchpad(signal_scores, regime, market_conditions, positions) -> dict
  save_hot_scratchpad(scratchpad)                                      -> None
  get_recent_scratchpads(n)                                            -> list[dict]
  format_scratchpad_section(scratchpad)                                -> str
  format_hot_memory_section(n)                                         -> str
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from log_setup import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HOT_MEMORY_PATH = Path(__file__).parent / "data" / "memory" / "hot_scratchpads.json"
_HOT_MEMORY_MAX  = 20   # rolling window — oldest dropped when exceeded

# ---------------------------------------------------------------------------
# Lazy Anthropic client
# ---------------------------------------------------------------------------
_claude = None

def _get_claude():
    global _claude
    if _claude is None:
        import anthropic
        _claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
    return _claude

MODEL_FAST = "claude-haiku-4-5-20251001"

# ---------------------------------------------------------------------------
# System prompt (cached)
# ---------------------------------------------------------------------------
_SCRATCHPAD_SYS = """You are a pre-analysis assistant for an autonomous trading bot.

You receive signal scores and market context from Stage 2. Your job is to produce a
focused scratchpad that helps the final decision model (Stage 3) work smarter.

Return ONLY valid JSON. No markdown fences, no commentary.

Output schema:
{
  "watching":           ["SYMBOL", ...],
  "blocking":           ["reason or SYMBOL: reason", ...],
  "triggers":           ["SYMBOL: specific condition to watch for", ...],
  "conviction_ranking": [
    {"symbol": "X", "conviction": "high|medium|low", "notes": "one-line rationale"},
    ...
  ],
  "summary": "one sentence overall read for this cycle"
}

Rules:
- watching: 2-8 symbols with the clearest setups this cycle. Include all held positions.
- blocking: 1-5 conditions. Can be market-wide ("VIX above 30 — reduce size")
  or symbol-specific ("AAPL: no catalyst today — skip").
- triggers: 1-8 entries. Be specific — price levels, volume thresholds, catalyst events.
- conviction_ranking: every symbol in watching, ordered high→low. notes max 12 words.
- summary: single sentence, 20 words max.
- If signal_scores is empty or regime is unknown, return all empty lists and a summary
  of "Insufficient signal data — holding current positions."
"""


# ---------------------------------------------------------------------------
# Core Haiku call
# ---------------------------------------------------------------------------

def run_scratchpad(
    signal_scores: dict,
    regime: dict,
    market_conditions: dict,
    positions: Optional[list] = None,
) -> dict:
    """
    Make a Haiku scratchpad call and return structured pre-analysis.

    Degrades gracefully — returns {} on any failure so bot.py can skip the
    section without crashing.

    Args:
        signal_scores:     Output of score_signals() — keys: scored_symbols,
                           top_3, elevated_caution, reasoning.
        regime:            Output of classify_regime().
        market_conditions: The md dict passed through the pipeline (vix, etc.)
        positions:         Active Alpaca position objects (optional).

    Returns:
        dict with keys: watching, blocking, triggers, conviction_ranking,
                        summary, ts, regime_score, vix
    """
    _default: dict = {}

    try:
        scored = signal_scores.get("scored_symbols", {})
        top3   = signal_scores.get("top_3", [])
        caution= signal_scores.get("elevated_caution", [])
        sig_reasoning = signal_scores.get("reasoning", "")

        # Build held positions string
        held = []
        for p in (positions or []):
            try:
                qty = float(getattr(p, "qty", 0))
                if qty > 0:
                    sym = getattr(p, "symbol", "?")
                    unr = float(getattr(p, "unrealized_plpc", 0)) * 100
                    held.append(f"{sym} ({unr:+.1f}% unrealized)")
            except Exception:
                pass
        held_str = ", ".join(held) if held else "(none)"

        # Compact signal scores table — top 10 by score
        sorted_scores = sorted(
            scored.items(),
            key=lambda kv: kv[1].get("score", 0) if isinstance(kv[1], dict) else 0,
            reverse=True,
        )[:10]
        scores_lines = []
        for sym, data in sorted_scores:
            if not isinstance(data, dict):
                continue
            sc   = data.get("score", "?")
            conv = data.get("conviction", "?")
            cat  = (data.get("primary_catalyst") or "")[:40]
            scores_lines.append(f"  {sym:<8} score={sc:<4} conviction={conv:<8} catalyst={cat}")
        scores_str = "\n".join(scores_lines) or "  (none)"

        user_content = (
            f"REGIME: score={regime.get('regime_score', 50)} "
            f"bias={regime.get('bias', 'neutral')} "
            f"theme={regime.get('session_theme', '?')}\n"
            f"  constraints: {regime.get('constraints', [])}\n\n"
            f"VIX: {market_conditions.get('vix', '?')}  "
            f"Regime label: {market_conditions.get('vix_regime', '?')}\n\n"
            f"HELD POSITIONS: {held_str}\n\n"
            f"TOP 3 SIGNALS: {top3}\n"
            f"ELEVATED CAUTION: {caution}\n\n"
            f"SIGNAL SCORES (top 10 by score):\n{scores_str}\n\n"
            f"SIGNAL SCORER REASONING:\n{sig_reasoning[:400]}\n"
        )

        resp = _get_claude().messages.create(
            model=MODEL_FAST,
            max_tokens=900,
            system=[{
                "type": "text",
                "text": _SCRATCHPAD_SYS,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_content}],
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )

        # Cost tracking (non-fatal)
        try:
            from cost_tracker import get_tracker
            get_tracker().record_api_call(MODEL_FAST, resp.usage, caller="scratchpad")
        except Exception:
            pass

        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        result = json.loads(raw)

        # Stamp with metadata
        result["ts"]           = datetime.now(timezone.utc).isoformat()
        result["regime_score"] = int(regime.get("regime_score", 50))
        result["vix"]          = float(market_conditions.get("vix", 0) or 0)

        log.info(
            "[SCRATCHPAD] watching=%s  blocking=%d  triggers=%d",
            result.get("watching", []),
            len(result.get("blocking", [])),
            len(result.get("triggers", [])),
        )

        return result

    except Exception as exc:
        log.warning("[SCRATCHPAD] Haiku call failed (non-fatal): %s", exc)
        return _default


# ---------------------------------------------------------------------------
# Hot memory — rolling JSON, last 20 scratchpads
# ---------------------------------------------------------------------------

def save_hot_scratchpad(scratchpad: dict) -> None:
    """
    Append scratchpad to the rolling hot memory file.
    Trims to the last _HOT_MEMORY_MAX entries.
    No-op on empty input or I/O failure.
    """
    if not scratchpad:
        return
    try:
        _HOT_MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        existing: list = []
        if _HOT_MEMORY_PATH.exists():
            try:
                existing = json.loads(_HOT_MEMORY_PATH.read_text())
                if not isinstance(existing, list):
                    existing = []
            except Exception:
                existing = []

        existing.append(scratchpad)

        # Keep only the most recent entries
        if len(existing) > _HOT_MEMORY_MAX:
            existing = existing[-_HOT_MEMORY_MAX:]

        _HOT_MEMORY_PATH.write_text(json.dumps(existing, indent=2))
        log.debug("[SCRATCHPAD] hot memory saved (%d entries)", len(existing))

    except Exception as exc:
        log.warning("[SCRATCHPAD] hot memory save failed (non-fatal): %s", exc)


def get_recent_scratchpads(n: int = 5) -> list[dict]:
    """
    Return the n most recent scratchpads from hot memory, newest first.
    Returns [] on any error.
    """
    try:
        if not _HOT_MEMORY_PATH.exists():
            return []
        data = json.loads(_HOT_MEMORY_PATH.read_text())
        if not isinstance(data, list):
            return []
        return list(reversed(data[-n:]))
    except Exception as exc:
        log.debug("[SCRATCHPAD] hot memory read failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Formatting helpers for prompt injection
# ---------------------------------------------------------------------------

def format_scratchpad_section(scratchpad: dict) -> str:
    """
    Format a single scratchpad dict as a human-readable prompt section.
    Used by build_user_prompt() to inject Stage 2.5 output.
    """
    if not scratchpad:
        return "  (scratchpad unavailable this cycle)"

    lines: list[str] = []

    summary = scratchpad.get("summary", "")
    if summary:
        lines.append(f"  Summary  : {summary}")

    watching = scratchpad.get("watching", [])
    if watching:
        lines.append(f"  Watching : {', '.join(watching)}")

    blocking = scratchpad.get("blocking", [])
    if blocking:
        lines.append("  Blocking :")
        for b in blocking:
            lines.append(f"    - {b}")

    triggers = scratchpad.get("triggers", [])
    if triggers:
        lines.append("  Triggers :")
        for t in triggers:
            lines.append(f"    - {t}")

    rankings = scratchpad.get("conviction_ranking", [])
    if rankings:
        lines.append("  Conviction ranking:")
        for r in rankings:
            sym  = r.get("symbol", "?")
            conv = r.get("conviction", "?")
            note = r.get("notes", "")
            lines.append(f"    {sym:<8} [{conv}]  {note}")

    return "\n".join(lines) if lines else "  (no scratchpad data)"


def format_hot_memory_section(n: int = 3) -> str:
    """
    Format recent scratchpad history (last n entries) for prompt injection.
    Used by build_user_prompt() to give Stage 3 short-term scratchpad context.
    """
    recent = get_recent_scratchpads(n)
    if not recent:
        return "  (no recent scratchpad history)"

    lines: list[str] = []
    for i, sp in enumerate(recent, start=1):
        ts    = str(sp.get("ts", ""))[:16]
        vix   = sp.get("vix", "?")
        rscore= sp.get("regime_score", "?")
        summ  = sp.get("summary", "")
        watch = sp.get("watching", [])
        lines.append(
            f"  [{i}] {ts}  vix={vix}  regime={rscore}  "
            f"watching={watch}  summary: {summ}"
        )

    return "\n".join(lines)
