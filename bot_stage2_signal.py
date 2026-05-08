"""
bot_stage2_signal.py — Stage 2 signal scorer.

Three-layer architecture (post-overhaul):
  L2 (python, deterministic) — computes numerical anchor for every symbol
  L1 (Sonnet, async)         — writes qualitative_context.json from narrative
  L3 (Haiku, this module)    — synthesises L1 + L2 into final SignalScore

Public API
----------
score_signals_layered(watchlist_symbols, regime, md, positions) -> dict
    Drop-in replacement for the legacy score_signals(). L3 synthesis layer.

format_signal_scores(scores) -> str
    Unchanged — formats the final dict for prompt injection.

score_signals(watchlist_symbols, regime, md, positions) -> dict
    Legacy single-layer Haiku scorer. Kept as a fallback and referenced by
    tests; not used in the hot path once bot.py imports `score_signals_layered`.
"""
from __future__ import annotations

import functools
import json
import time
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from typing import Optional

from bot_clients import MODEL_FAST, _get_claude
from log_setup import get_logger, log_trade

log = get_logger(__name__)

_BASE = Path(__file__).parent
_RISK_FACTORS_PATH = _BASE / "data/config/symbol_risk_factors.json"

_SAFETY_DEDUP_SECS: float = 300.0

# ── Avoid log / short-rule constants ─────────────────────────────────────────
_AVOID_LOG_PATH     = _BASE / "data" / "analytics" / "avoid_log.jsonl"
_SONNET_BRIEF_PATH  = _BASE / "data" / "market" / "morning_brief_sonnet.json"
_AVOID_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB rotation threshold

# Signal keywords that indicate bearish thesis — must match ≥2 for short rule
_BEARISH_KEYWORDS = frozenset({
    "negative_momentum", "bearish_catalyst", "bearish", "insider_selling",
    "insider_sale", "analyst_downgrade", "below_200ma", "downtrend",
    "sell_signal", "weak_fundamentals", "earnings_miss", "revenue_miss",
    "guidance_cut", "overvalued", "short_interest", "weak_momentum",
})
_SAFETY_ALERT_CACHE: dict[str, float] = {}


def _fire_safety_alert(fn_name: str, exc: Exception) -> None:
    """Fire a CRITICAL WhatsApp alert when a safety function throws. 5-min dedup. Never raises.
    # TODO(DASHBOARD): surface safety_system_degraded alerts on dashboard
    """
    try:
        now = time.time()
        if now - _SAFETY_ALERT_CACHE.get(fn_name, 0) < _SAFETY_DEDUP_SECS:
            return
        _SAFETY_ALERT_CACHE[fn_name] = now
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        msg = (
            f"[SAFETY DEGRADED] bot_stage2_signal.{fn_name} threw: "
            f"{type(exc).__name__}: {exc}. "
            f"Fallback active — manual review required. {ts}"
        )
        try:
            from notifications import send_whatsapp_direct  # noqa: PLC0415
            send_whatsapp_direct(msg)
        except Exception:
            pass
    except Exception:
        pass


@functools.lru_cache(maxsize=1)
def _load_symbol_risk_factors() -> dict:
    """Load per-symbol structural risk factors (e.g. China revenue, export controls)."""
    try:
        return json.loads(_RISK_FACTORS_PATH.read_text()) if _RISK_FACTORS_PATH.exists() else {}
    except Exception:
        return {}


# L3 batch size is smaller than the legacy scorer's because each symbol now
# carries an L1+L2 context block (~80-120 tokens) in addition to the bare
# ticker. Target ~1.5K input tokens per batch.
_BATCH_SIZE = 20

# Legacy path uses a larger batch because its input is much leaner (no L1/L2).
_LEGACY_BATCH_SIZE = 20

# Merged call processes the top _MAX_MERGED symbols via a single Haiku call
# that produces both enriched signal scores AND a pre-analysis scratchpad.
# Remaining symbols (beyond _MAX_MERGED) receive L2-only fallback scores.
_MAX_MERGED = 40


# ═════════════════════════════════════════════════════════════════════════════
# L3: Haiku synthesis layer
# ═════════════════════════════════════════════════════════════════════════════

_L3_SYSTEM = (
    "You are a synthesis layer for a trading bot's signal pipeline. "
    "A Python L2 scorer has already computed a numerical score in [0,100] for "
    "each symbol using RSI, MACD, MA stacks, volume, intraday momentum, and "
    "ORB context. A separate Sonnet L1 layer has distilled qualitative context "
    "(insider activity, thesis tags, macro beta stress) into structured tags.\n\n"
    "Your job is NOT to re-score from scratch. Your job is:\n"
    "  1. Treat L2_score as your anchor.\n"
    "  2. Adjust by ≤ 15 points only when L1 context reveals a direct "
    "contradiction or strong corroboration (e.g. L2 bullish + L1 shows active "
    "fraud catalyst → adjust down; L2 neutral + L1 shows recent insider buy → "
    "adjust up modestly).\n"
    "  3. If you adjust by more than 5 points, cite the reason in "
    "adjustment_reason. Otherwise leave adjustment_reason empty.\n"
    "  4. Emit primary_catalyst as a ≤12-word phrase. Use only L1 context and "
    "L2 signals — do NOT quote prose from elsewhere.\n"
    "  5. Emit catalyst_type using ONLY the taxonomy labels listed in the schema. "
    "Use 'earnings_pending' when earnings_days_away ≤ 5. "
    "Use 'unknown' when no clear catalyst. Never invent new label values.\n\n"
    "HARD RULES:\n"
    "- Never use 'today', 'tonight', or 'this week' for earnings timing. L2 "
    "provides earnings_days_away as an integer; write 'earnings in N days' "
    "when relevant.\n"
    "- Never invent price levels. L2's price is the only valid price reference.\n"
    "- If no L1 context is provided for a symbol, use L2 alone — do NOT fabricate.\n\n"
    "Return JSON only, no markdown. Schema:\n"
    '{"scored_symbols":{"SYMBOL":{'
    '"score":<0-100>,"direction":"bullish"|"bearish"|"neutral",'
    '"conviction":"high"|"medium"|"low"|"avoid",'
    '"signals":[<strings>],"conflicts":[<strings>],'
    '"primary_catalyst":"<≤12 words>",'
    '"catalyst_type":"earnings_beat"|"earnings_miss"|"guidance_raise"|"guidance_cut"'
    '|"macro_print"|"fed_signal"|"geopolitical"|"policy_change"|"insider_buy"'
    '|"congressional_buy"|"analyst_revision"|"corporate_action"|"technical_breakout"'
    '|"momentum_continuation"|"mean_reversion"|"sector_rotation"|"social_sentiment"'
    '|"citrini_thesis"|"earnings_pending"|"unknown",'
    '"orb_candidate":true|false,'
    '"pattern_watchlist":false|"<caution note>",'
    '"tier":"core"|"dynamic",'
    '"l2_score":<number>,"l3_adjustment":<number>,'
    '"adjustment_reason":"<string or empty>"'
    '}},'
    '"top_3":["SYM1","SYM2","SYM3"],'
    '"elevated_caution":["SYM4"],'
    '"reasoning":"<2 sentences>"}\n\n'
    "TIER CLASSIFICATION:\n"
    "- 'core': NVDA, MSFT, AMZN, GOOGL, META, AAPL, TSM, ASML, PLTR, CRWV, JPM, GS, "
    "GLD, SLV, COPX, XLE, XOM, CVX, USO, QQQ, SPY, IWM, TLT, VXX, XBI, LLY, JNJ, "
    "WMT, XRT, LMT, RTX, ITA, EWJ, FXI, EEM, EWM, ECH, FRO, STNG, RKT, BE, XLF, "
    "BTC/USD, ETH/USD. Max 15% portfolio per position.\n"
    "- 'dynamic': Any symbol NOT in the core list above (scanner-promoted). Max 8%.\n"
    "- When in doubt: 'core'. Never fabricate tier values.\n\n"
    "ORB CANDIDATE RULES:\n"
    "- orb_candidate: true ONLY when L2 already set orb_candidate=true.\n"
    "- Never upgrade orb_candidate from false to true, even on strong news.\n"
    "- Crypto (/USD symbols) and ETFs are never ORB candidates.\n\n"
    "PATTERN WATCHLIST RULES:\n"
    "- pattern_watchlist: false by default.\n"
    "- Set to a ≤10-word caution note ONLY when you detect:\n"
    "  (a) L1 shows negative catalyst but L2 is strongly bullish (>65)\n"
    "  (b) Insider or congressional SELL against a bullish L2 score\n"
    "  (c) Earnings in ≤2 days on a directional (non-neutral) position\n"
    "  (d) Sector peers broadly down while this symbol shows bullish L2\n\n"
    "CRYPTO SPECIAL HANDLING:\n"
    "- Symbols with '/USD' suffix (BTC/USD, ETH/USD) are crypto — always tier='core'.\n"
    "- Never set orb_candidate=true for crypto.\n"
    "- Crypto has no earnings — omit earnings_days_away references.\n"
    "- Crypto L2 scores span a wider range (10-90) — do not compress them.\n\n"
    "EARNINGS PROXIMITY:\n"
    "- earnings_days_away ≤ 5: always set catalyst_type='earnings_pending'.\n"
    "- earnings_days_away is context only — do NOT add to elevated_caution or "
    "pattern_watchlist solely due to upcoming earnings. Sonnet decides.\n\n"
    "CONFLICT DETECTION:\n"
    "- conflicts: list contradictions between L1 narrative and L2 technicals.\n"
    "- Examples: 'L2 oversold bounce but L1 shows CEO departure news'; "
    "'L2 breakout signal but L1 shows analyst downgrade'. Max 3 conflicts.\n"
    "- Empty list [] is fine when signals align.\n\n"
    "OUTPUT RULES:\n"
    "- Every symbol in the input MUST appear in scored_symbols — no omissions.\n"
    "- Symbols with no L1 context: score and direction from L2 only, "
    "adjustment_reason='', conflicts=[], signals from L2 labels.\n"
    "- top_3: highest 3 scores in this batch. elevated_caution: highest-uncertainty symbols.\n"
    "- reasoning: exactly 2 sentences. Sentence 1: what drove the key adjustments. "
    "Sentence 2: notable batch-wide pattern (all defensive, ORB dominates, etc).\n"
    "- L1_staleness_minutes > 480: treat all L1 context as absent for this batch.\n"
)


# ── Merged L3+Scratchpad system prompt ──────────────────────────────────────
# Used by _run_merged_synthesis(). Must be ≥ 1024 tokens to enable Anthropic
# ephemeral prompt caching. Combining L3 scoring rules + scratchpad pre-analysis
# rules into one call replaces 5 batched L3 calls + 1 standalone scratchpad call.
_MERGED_SYSTEM = (
    "You are a combined signal synthesis and pre-analysis layer for an autonomous trading bot. "
    "You receive L2 (Python deterministic) scores and L1 (qualitative) context for up to 40 symbols. "
    "Your response must produce BOTH a full signal scoring section (scored_symbols) and a "
    "scratchpad pre-analysis section — both in one JSON object.\n\n"
    "COMPACT OUTPUT RULES — strictly enforce to stay within token budget:\n"
    "• signals: max 3 short strings per symbol (omit the least important)\n"
    "• conflicts: max 2 strings per symbol\n"
    "• primary_catalyst: ≤6 words (cut filler — be telegraphic)\n"
    "• adjustment_reason: empty string when adjustment ≤5pts; ≤8 words otherwise\n"
    "• scratchpad.triggers: max 5 entries total across all symbols\n"
    "• conviction_ranking notes: ≤8 words\n\n"

    "═══ PART 1 — SIGNAL SCORING ═══\n"
    "Synthesise L1+L2 into enriched final scores per symbol.\n"
    "• Treat L2_score as anchor. Adjust by ≤15 points only when L1 context reveals a direct "
    "contradiction or strong corroboration. Example: L2 bullish + L1 shows active fraud "
    "catalyst → adjust down; L2 neutral + L1 shows recent insider buy → adjust up modestly.\n"
    "• If adjustment >5 points, cite the reason in adjustment_reason. Otherwise leave it empty.\n"
    "• primary_catalyst: ≤6-word phrase using only L1/L2 context. Never fabricate facts.\n"
    "• catalyst_type: use ONLY these taxonomy values — earnings_beat | earnings_miss | "
    "guidance_raise | guidance_cut | macro_print | fed_signal | geopolitical | policy_change | "
    "insider_buy | congressional_buy | analyst_revision | corporate_action | "
    "technical_breakout | momentum_continuation | mean_reversion | sector_rotation | "
    "social_sentiment | citrini_thesis | earnings_pending | unknown. "
    "Set catalyst_type='earnings_pending' when earnings_days_away ≤ 5. "
    "Use 'unknown' when no clear catalyst.\n"
    "• earnings_days_away passthrough: preserve the field from L2 in your output. "
    "Never write 'today', 'tonight', or 'this week' for earnings — write 'earnings in N days'.\n"
    "• Never invent price levels. L2's Price field is the only valid price reference.\n"
    "• Every symbol in the input MUST appear in scored_symbols — no omissions allowed.\n"
    "• Symbols with no L1 context: score and direction from L2 only; "
    "adjustment_reason=''; conflicts=[]; signals from L2 labels.\n"
    "• L1_staleness_minutes >480: treat all L1 context as absent for this call.\n"
    "• orb_candidate=true ONLY when L2 already flagged it. Never upgrade false→true. "
    "Crypto (/USD suffix) and ETFs: never orb_candidate=true, always tier='core'.\n\n"
    "Tier classification:\n"
    "  core (max 15% portfolio): NVDA MSFT AMZN GOOGL META AAPL TSM ASML PLTR CRWV JPM GS "
    "GLD SLV COPX XLE XOM CVX USO QQQ SPY IWM TLT VXX XBI LLY JNJ WMT XRT LMT RTX ITA "
    "EWJ FXI EEM EWM ECH FRO STNG RKT BE XLF BTC/USD ETH/USD\n"
    "  dynamic (max 8%): any symbol not in core. When in doubt: core.\n\n"
    "pattern_watchlist: false by default. Set to a ≤10-word caution note only when:\n"
    "  (a) L1 shows negative catalyst but L2 is strongly bullish >65\n"
    "  (b) Insider or congressional SELL signal against bullish L2 score\n"
    "  (c) Earnings ≤2 days on a non-neutral directional signal\n"
    "  (d) Sector peers broadly down while this symbol shows bullish L2\n\n"
    "conflicts: list contradictions between L1 narrative and L2 technicals. Max 2. "
    "Empty [] when signals align.\n"
    "reasoning: exactly 2 sentences — (1) key adjustments and why; (2) batch-wide pattern.\n\n"

    "═══ PART 2 — SCRATCHPAD PRE-ANALYSIS ═══\n"
    "Using the scores from Part 1, produce structured pre-analysis for Stage 3 (the main decision model).\n\n"
    "watching (2-8 symbols): always include all held positions (from HELD POSITIONS input); "
    "include score >65 + conviction=high; include orb_candidate=true during ORB window (9:30-9:45 ET). "
    "Cap watching at 4 max when VIX >30. Never include conviction='avoid' unless held.\n\n"
    "blocking (0-5 entries): market-wide blocks first, then symbol-specific. Empty [] when no blocks. "
    "Auto-add for held positions: within 1% of stop → 'SYMBOL: within 1pct of stop — monitor'; "
    "unrealized P&L <-3% (from HELD POSITIONS input) → 'SYMBOL: -N% unrealized — thesis review needed'.\n\n"
    "triggers (1-5 entries, format: 'SYMBOL: specific observable condition'): "
    "must be actionable with specific price or volume levels. Never vague. "
    "Auto-add for held positions with unrealized P&L >+10%: 'SYMBOL: trail stop candidate at current +10%'.\n\n"
    "conviction_ranking: every symbol in watching[], ordered high→low conviction. "
    "notes ≤8 words citing score, catalyst, or risk. "
    "Score mapping: 75-100=high, 50-74=medium, <50=low.\n\n"
    "summary: 1 sentence ≤20 words — overall read: regime + top setup + key risk.\n\n"
    "Regime-adjusted scratchpad behavior:\n"
    "  regime_score <35: watching = held positions only; no new entry triggers\n"
    "  regime_score 35-50: high conviction only (score ≥75); max 3 new entry triggers\n"
    "  regime_score 50-65: standard — score ≥65 qualifies for watching\n"
    "  regime_score >65: full operation — score ≥50 can appear in watching with strong catalyst\n\n"

    "═══ OUTPUT SCHEMA (JSON only — no markdown fences, no text outside the JSON object) ═══\n"
    '{"scored_symbols":{"SYMBOL":{"score":<0-100>,"direction":"bullish"|"bearish"|"neutral",'
    '"conviction":"high"|"medium"|"low"|"avoid","signals":[<strings>],"conflicts":[<strings>],'
    '"primary_catalyst":"<≤6 words>","catalyst_type":"<taxonomy value>",'
    '"orb_candidate":true|false,"pattern_watchlist":false|"<≤10-word note>",'
    '"tier":"core"|"dynamic","l2_score":<num>,"l3_adjustment":<num>,'
    '"adjustment_reason":"<empty or explanation>"}},'
    '"top_3":["SYM1","SYM2","SYM3"],'
    '"elevated_caution":["SYM",...],'
    '"reasoning":"<2 sentences>",'
    '"scratchpad":{'
    '"watching":["SYM",...],'
    '"blocking":["reason or SYMBOL: reason",...],'
    '"triggers":["SYMBOL: specific condition",...],'
    '"conviction_ranking":[{"symbol":"SYM","conviction":"high|medium|low","notes":"<≤8 words>"}],'
    '"summary":"<≤20 words>"}}'
)


def _load_qualitative_context() -> dict:
    """Read qualitative_context.json gracefully.

    Returns {} if:
      - file missing
      - read / parse fails
      - staleness_minutes computed > 480 (8h)
    """
    try:
        from bot_stage1_5_qualitative import (  # noqa: PLC0415
            context_age_minutes,
            load_qualitative_context,
        )
        ctx = load_qualitative_context()
        if not ctx:
            return {}
        age = context_age_minutes()
        if age > 480:
            log.warning(
                "[L3] qualitative_context.json is %.0fm stale (>480m) — "
                "proceeding with L2-only synthesis",
                age,
            )
            return {}
        if age > 360:
            log.info(
                "[L3] qualitative_context.json is %.0fm stale (>360m) — using with warning",
                age,
            )
        ctx["_staleness_minutes"] = age
        return ctx
    except Exception as exc:
        log.debug("[L3] _load_qualitative_context failed: %s", exc)
        return {}


def _get_macro_wire_hits_for_symbol(sym: str) -> list[str]:
    """Return ≤2 recent macro wire headlines that mention this symbol.

    Reads live_cache.json (populated by macro_wire.classify_articles).
    Non-fatal — returns [] on any error.
    """
    try:
        cache_path = _BASE / "data" / "macro_wire" / "live_cache.json"
        if not cache_path.exists():
            return []
        articles = json.loads(cache_path.read_text())
        if not isinstance(articles, list):
            return []
        hits = [
            a["headline"][:80]
            for a in articles
            if isinstance(a, dict) and sym in (a.get("affected_symbols") or [])
        ]
        return hits[:2]
    except Exception:
        return []


def _load_cached_symbol_news(sym: str) -> list[str]:
    """Return ≤3 recent headlines from per-symbol news caches (Yahoo RSS + Finnhub).

    Reads data/news/{SYM}_yahoo_news.json and data/news/{SYM}_finnhub_news.json.
    Cache is populated by data_warehouse.refresh_yahoo_symbol_news() (4 AM + on-demand).
    Returns [] if no cache exists. Non-fatal.
    """
    news_dir = _BASE / "data" / "news"
    headlines: list[str] = []
    for suffix in ("_yahoo_news.json", "_finnhub_news.json"):
        try:
            path = news_dir / f"{sym}{suffix}"
            if not path.exists():
                continue
            data = json.loads(path.read_text())
            for a in (data.get("articles") or []):
                h = (a.get("headline") or "").strip()
                if h and h not in headlines:
                    headlines.append(h[:80])
                    if len(headlines) >= 3:
                        return headlines
        except Exception:
            pass
    return headlines


def _format_l2_for_l3(sym: str, l2: dict, qual_entry: Optional[dict],
                      l2_price: Optional[float]) -> str:
    """Build the compact per-symbol block that Haiku sees."""
    score     = l2.get("score", 50)
    direction = l2.get("direction", "neutral")
    conviction = l2.get("conviction", "low")
    signals   = (l2.get("signals") or [])[:6]
    conflicts = (l2.get("conflicts") or [])[:4]
    eda       = l2.get("earnings_days_away")
    sig_str   = ", ".join(signals) if signals else "(none)"
    con_str   = ", ".join(conflicts) if conflicts else "(none)"

    lines = [
        f"{sym}:",
        f"  L2_score={score} direction={direction} conviction={conviction}",
        f"  L2_signals: {sig_str}",
        f"  L2_conflicts: {con_str}",
    ]
    if qual_entry and isinstance(qual_entry, dict):
        tags = ", ".join((qual_entry.get("thesis_tags") or [])[:5]) or "(none)"
        cat  = qual_entry.get("catalyst_active") or "(none)"
        mbs  = qual_entry.get("macro_beta_stress") or "unknown"
        narr = (qual_entry.get("narrative") or "")[:140]
        lines.append(f"  L1_thesis_tags: {tags}")
        lines.append(f"  L1_catalyst: {cat}")
        lines.append(f"  L1_macro_beta_stress: {mbs}")
        if narr:
            lines.append(f"  L1_narrative: {narr}")

    # Include price + key numbers so Haiku has a concrete anchor for catalyst prose
    price_str = f"Price: ${l2_price:.2f}" if l2_price else "Price: ?"
    eda_str   = f"  earnings_days_away={eda}" if eda is not None else ""
    lines.append(f"  {price_str}{eda_str}")

    # Inject any macro wire hits for this symbol (Phase B — reduces unknown rate)
    wire_hits = _get_macro_wire_hits_for_symbol(sym)
    if wire_hits:
        lines.append("  MACRO_WIRE: " + " | ".join(wire_hits))

    # Inject per-symbol news headlines from Yahoo/Finnhub cache (Phase C)
    sym_news = _load_cached_symbol_news(sym)
    if sym_news:
        lines.append("  SYMBOL_NEWS: " + " | ".join(sym_news))

    # Inject symbol structural risk factors (China revenue, export control risk)
    _risk = _load_symbol_risk_factors().get(sym)
    if _risk:
        _pct  = _risk.get("china_revenue_pct", "?")
        _ctrl = str(_risk.get("export_control_risk", "unknown")).upper()
        _note = (_risk.get("notes") or "")[:100]
        lines.append(f"  SYMBOL_RISK: china_revenue={_pct}% export_control={_ctrl} — {_note}")

    # Flag stale bar data so Haiku discounts technical signals
    if l2.get("data_stale"):
        _age = l2.get("bar_age_minutes", "?")
        lines.append(
            f"  DATA_STALE: bar data is {_age} min old — treat technical signals with lower confidence"
        )

    return "\n".join(lines)


def _call_l3_batch(user_content: str) -> dict:
    """One L3 Haiku call. Returns parsed dict. Raises on total failure."""
    resp = _get_claude().messages.create(
        model=MODEL_FAST,
        max_tokens=8192,
        system=[{
            "type": "text",
            "text": _L3_SYSTEM,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_content}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )
    try:
        from cost_tracker import get_tracker  # noqa: PLC0415
        get_tracker().record_api_call(MODEL_FAST, resp.usage, caller="signal_scorer_l3")
    except Exception as _ct_exc:
        log.debug("[L3] cost tracker failed: %s", _ct_exc)
    try:
        from cost_attribution import log_claude_call_to_spine  # noqa: PLC0415
        log_claude_call_to_spine("signal_scorer_l3", MODEL_FAST, "signal_scoring", resp.usage)
    except Exception:
        pass

    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        last_brace = raw.rfind("}")
        if last_brace >= 0:
            try:
                parsed = json.loads(raw[: last_brace + 1])
                log.debug("[L3] JSON repaired by truncation")
                return parsed
            except json.JSONDecodeError:
                pass
        # One retry with completeness hint
        log.debug("[L3] JSON truncated; retrying with completeness hint")
        _retry_sys = _L3_SYSTEM + (
            "\n\nCRITICAL: Return ONLY valid complete JSON. If you cannot fit "
            "all symbols, return fewer rather than truncating."
        )
        retry = _get_claude().messages.create(
            model=MODEL_FAST, max_tokens=8192,
            system=[{"type": "text", "text": _retry_sys,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_content}],
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
        retry_raw = retry.content[0].text.strip()
        if retry_raw.startswith("```"):
            retry_raw = retry_raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return json.loads(retry_raw)


def _run_l3_synthesis(
    symbols: list[str],
    l2_scores: dict[str, dict],
    qual_ctx: dict,
    regime: dict,
    positions: Optional[list],
) -> dict:
    """Batch L3 synthesis across all L2-scored symbols. Returns the final
    SignalScore dict consumed by downstream A1/A2.

    Falls open to L2-only output on total L3 failure so the cycle never dies.
    """
    sym_ctx = qual_ctx.get("symbol_context", {}) if qual_ctx else {}
    regime_ctx_block = qual_ctx.get("regime_context", {}) if qual_ctx else {}
    staleness = qual_ctx.get("_staleness_minutes", 0) if qual_ctx else 0

    merged_symbols: dict = {}
    all_caution: list = []
    all_reasoning: list = []

    # Build regime / L1 regime header once — cached via prompt_caching in L3.
    regime_line = (
        f"REGIME: score={regime.get('regime_score', 50)} "
        f"bias={regime.get('bias', 'neutral')} "
        f"theme={regime.get('session_theme', '?')}  "
        f"constraints={regime.get('constraints', [])}"
    )
    l1_regime_line = ""
    if isinstance(regime_ctx_block, dict) and regime_ctx_block:
        narr = (regime_ctx_block.get("narrative") or "")[:180]
        ron  = ", ".join(regime_ctx_block.get("risk_on_catalysts", [])[:4]) or "(none)"
        roff = ", ".join(regime_ctx_block.get("risk_off_catalysts", [])[:4]) or "(none)"
        l1_regime_line = (
            f"L1_regime_narrative: {narr}\n"
            f"L1_risk_on: {ron}\n"
            f"L1_risk_off: {roff}\n"
            f"L1_staleness_minutes: {int(staleness)}"
        )

    _it = iter(symbols)
    while True:
        batch = list(islice(_it, _BATCH_SIZE))
        if not batch:
            break

        sym_blocks: list[str] = []
        for sym in batch:
            l2 = l2_scores.get(sym)
            if not l2:
                continue
            l2_price = l2.get("price")
            qual_entry = sym_ctx.get(sym) if isinstance(sym_ctx, dict) else None
            sym_blocks.append(_format_l2_for_l3(sym, l2, qual_entry, l2_price))

        if not sym_blocks:
            continue

        user_content = (
            f"{regime_line}\n\n"
            f"{l1_regime_line}\n\n"
            f"=== SYMBOLS ===\n"
            + "\n\n".join(sym_blocks)
            + "\n\nProduce the JSON synthesis object per the schema. "
              "Every symbol in the input must appear in scored_symbols. "
              "Remember: L2_score is your anchor — ≤15 point adjustment, "
              "explain >5 in adjustment_reason."
        )

        try:
            batch_result = _call_l3_batch(user_content)
        except Exception as exc:
            log.warning("[L3] batch %s failed: %s — falling back to L2 for these symbols", batch, exc)
            # L2-only fallback for this batch
            for sym in batch:
                l2 = l2_scores.get(sym, {})
                merged_symbols[sym] = _l2_to_signal_score(sym, l2)
            continue

        ss = batch_result.get("scored_symbols", {}) or {}
        for sym in batch:
            row = ss.get(sym)
            if isinstance(row, dict):
                # Enforce the ≤15 point adjustment guardrail defensively
                l2 = l2_scores.get(sym, {})
                l2_score = float(l2.get("score", 50))
                final = float(row.get("score", l2_score))
                adjustment = final - l2_score
                if abs(adjustment) > 15.0:
                    clamped = l2_score + max(-15.0, min(15.0, adjustment))
                    log.debug(
                        "[L3] %s clamped adjustment %.1f → %.1f (anchor=%.1f)",
                        sym, adjustment, clamped - l2_score, l2_score,
                    )
                    final = clamped
                    adjustment = final - l2_score
                row["score"]             = round(final, 1)
                row["l2_score"]          = round(l2_score, 1)
                row["l3_adjustment"]     = round(adjustment, 1)
                row.setdefault("adjustment_reason", "")
                row.setdefault("orb_candidate", bool(l2.get("orb_candidate")))
                row.setdefault("pattern_watchlist", l2.get("pattern_watchlist") or False)
                # Keep L2's earnings_days_away passthrough
                if "earnings_days_away" not in row and l2.get("earnings_days_away") is not None:
                    row["earnings_days_away"] = l2.get("earnings_days_away")
                # Use Haiku's self-classified catalyst_type (Phase B, Sprint 5).
                # Validate against known taxonomy; fall back to classify_catalyst()
                # on text-match only when Haiku returned unknown/missing.
                _haiku_ct = (row.get("catalyst_type") or "").strip().lower().replace("-", "_")
                try:
                    from semantic_labels import (  # noqa: PLC0415
                        CatalystType as _CT,
                    )
                    from semantic_labels import (
                        classify_catalyst as _cc,
                    )
                    _known = {e.value for e in _CT}
                    if _haiku_ct and _haiku_ct in _known and _haiku_ct != "unknown":
                        row["catalyst_type"] = _haiku_ct
                    elif row.get("primary_catalyst"):
                        row["catalyst_type"] = _cc(row.get("primary_catalyst", "") or "").value
                    else:
                        row["catalyst_type"] = "unknown"
                except Exception:
                    row["catalyst_type"] = "unknown"
                merged_symbols[sym] = row
            else:
                # L3 dropped this symbol — use L2 anchor
                merged_symbols[sym] = _l2_to_signal_score(sym, l2_scores.get(sym, {}))

        all_caution.extend(batch_result.get("elevated_caution", []) or [])
        if batch_result.get("reasoning"):
            all_reasoning.append(batch_result["reasoning"])

    if not merged_symbols:
        log.warning("[L3] No symbols made it through L3 — full L2 fallback")
        for sym, l2 in l2_scores.items():
            merged_symbols[sym] = _l2_to_signal_score(sym, l2)

    sorted_syms = sorted(
        merged_symbols.items(),
        key=lambda kv: float(kv[1].get("score", 0)) if isinstance(kv[1], dict) else 0,
        reverse=True,
    )
    seen_c: set = set()
    result = {
        "scored_symbols": merged_symbols,
        "top_3": [s for s, _ in sorted_syms[:3]],
        "elevated_caution": [s for s in all_caution if not (s in seen_c or seen_c.add(s))],
        "reasoning": " | ".join(all_reasoning),
        "l1_staleness_minutes": int(staleness),
        "l3_used": True,
    }
    log.info(
        "[L3] synthesised %d symbols  top_3=%s  l1_stale=%sm",
        len(merged_symbols), result["top_3"], int(staleness),
    )
    return result


def _call_merged_l3_scratchpad(user_content: str) -> dict:
    """Single merged Haiku call: signal scoring (up to 40 symbols) + scratchpad.

    Returns parsed dict with scored_symbols AND scratchpad keys.
    Raises on total failure — caller falls back to batched _run_l3_synthesis().
    """
    resp = _get_claude().messages.create(
        model=MODEL_FAST,
        max_tokens=8192,
        system=[{
            "type": "text",
            "text": _MERGED_SYSTEM,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_content}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )
    try:
        from cost_tracker import get_tracker  # noqa: PLC0415
        get_tracker().record_api_call(MODEL_FAST, resp.usage, caller="merged_signal_scratchpad")
    except Exception as _ct_exc:
        log.debug("[MERGED] cost tracker failed: %s", _ct_exc)
    try:
        from cost_attribution import log_claude_call_to_spine  # noqa: PLC0415
        log_claude_call_to_spine(
            "signal_scorer_l3", MODEL_FAST, "merged_signal_scratchpad", resp.usage
        )
    except Exception:
        pass

    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        last_brace = raw.rfind("}")
        if last_brace >= 0:
            try:
                parsed = json.loads(raw[: last_brace + 1])
                log.debug("[MERGED] JSON repaired by truncation")
                return parsed
            except json.JSONDecodeError:
                pass
        log.debug("[MERGED] JSON truncated; retrying with completeness hint")
        _retry_sys = _MERGED_SYSTEM + (
            "\n\nCRITICAL: Return ONLY valid complete JSON with both scored_symbols "
            "and scratchpad sections. Never truncate mid-object."
        )
        retry = _get_claude().messages.create(
            model=MODEL_FAST, max_tokens=8192,
            system=[{"type": "text", "text": _retry_sys,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_content}],
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
        retry_raw = retry.content[0].text.strip()
        if retry_raw.startswith("```"):
            retry_raw = retry_raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return json.loads(retry_raw)


def _run_merged_synthesis(
    symbols: list[str],
    l2_scores: dict[str, dict],
    qual_ctx: dict,
    regime: dict,
    positions: Optional[list],
    md: dict,
) -> dict:
    """Single-call merged synthesis: L3 signal scoring (top 40) + scratchpad.

    Selects the top _MAX_MERGED symbols: held positions first, then highest
    L2-scoring remainder. Remaining symbols receive L2-only fallback scores.
    The returned dict includes a 'scratchpad' key that run_scratchpad_stage()
    extracts without making a second API call.

    Falls back to _run_l3_synthesis() (no embedded scratchpad) on any error.
    """
    sym_ctx        = qual_ctx.get("symbol_context", {}) if qual_ctx else {}
    regime_ctx_blk = qual_ctx.get("regime_context", {}) if qual_ctx else {}
    staleness      = qual_ctx.get("_staleness_minutes", 0) if qual_ctx else 0

    # Held positions are always Haiku-enriched; remainder sorted by L2 score
    _held = {
        getattr(p, "symbol", "")
        for p in (positions or [])
        if float(getattr(p, "qty", 0)) > 0
    }
    _priority    = [s for s in symbols if s in _held]
    _rest_by_l2  = sorted(
        [s for s in symbols if s not in _held],
        key=lambda s: float(l2_scores.get(s, {}).get("score", 0)) if l2_scores.get(s) else 0,
        reverse=True,
    )
    _ordered       = _priority + _rest_by_l2
    haiku_symbols  = _ordered[:_MAX_MERGED]
    l2_only_syms   = _ordered[_MAX_MERGED:]

    # Regime / L1 regime header
    regime_line = (
        f"REGIME: score={regime.get('regime_score', 50)} "
        f"bias={regime.get('bias', 'neutral')} "
        f"theme={regime.get('session_theme', '?')}  "
        f"constraints={regime.get('constraints', [])}"
    )
    l1_regime_line = ""
    if isinstance(regime_ctx_blk, dict) and regime_ctx_blk:
        narr = (regime_ctx_blk.get("narrative") or "")[:180]
        ron  = ", ".join(regime_ctx_blk.get("risk_on_catalysts", [])[:4]) or "(none)"
        roff = ", ".join(regime_ctx_blk.get("risk_off_catalysts", [])[:4]) or "(none)"
        l1_regime_line = (
            f"L1_regime_narrative: {narr}\n"
            f"L1_risk_on: {ron}\n"
            f"L1_risk_off: {roff}\n"
            f"L1_staleness_minutes: {int(staleness)}"
        )

    # Held positions string — includes unrealized P&L for scratchpad stop-risk
    held_lines = []
    for p in (positions or []):
        try:
            qty = float(getattr(p, "qty", 0))
            if qty > 0:
                sym = getattr(p, "symbol", "?")
                unr = float(getattr(p, "unrealized_plpc", 0)) * 100
                held_lines.append(f"{sym} ({unr:+.1f}% unrealized)")
        except Exception:
            pass
    held_str = ", ".join(held_lines) if held_lines else "(none)"

    # Build symbol blocks
    sym_blocks: list[str] = []
    for sym in haiku_symbols:
        l2 = l2_scores.get(sym)
        if not l2:
            continue
        qual_entry = sym_ctx.get(sym) if isinstance(sym_ctx, dict) else None
        sym_blocks.append(_format_l2_for_l3(sym, l2, qual_entry, l2.get("price")))

    if not sym_blocks:
        log.warning("[MERGED] No symbol blocks built — falling back to batched L3")
        return _run_l3_synthesis(symbols, l2_scores, qual_ctx, regime, positions)

    user_content = (
        f"{regime_line}\n\n"
        f"{l1_regime_line}\n\n"
        f"VIX: {md.get('vix', '?')}  "
        f"Regime label: {md.get('vix_regime', '?')}\n\n"
        f"HELD POSITIONS: {held_str}\n\n"
        f"=== SYMBOLS ({len(sym_blocks)} symbols to score) ===\n"
        + "\n\n".join(sym_blocks)
        + "\n\nProduce the JSON with both scored_symbols and scratchpad sections. "
          "Every symbol in the input must appear in scored_symbols. "
          "L2_score is your anchor — ≤15 point adjustment, explain >5 in adjustment_reason. "
          "Scratchpad watching list must include all held positions."
    )

    try:
        merged_result = _call_merged_l3_scratchpad(user_content)
    except Exception as exc:
        log.warning("[MERGED] merged call failed (%s) — falling back to batched L3", exc)
        return _run_l3_synthesis(symbols, l2_scores, qual_ctx, regime, positions)

    # Apply same guardrails as batched L3
    merged_symbols: dict = {}
    ss = merged_result.get("scored_symbols", {}) or {}
    for sym in haiku_symbols:
        l2  = l2_scores.get(sym, {})
        row = ss.get(sym)
        if isinstance(row, dict):
            l2_score = float(l2.get("score", 50))
            final    = float(row.get("score", l2_score))
            adj      = final - l2_score
            if abs(adj) > 15.0:
                clamped = l2_score + max(-15.0, min(15.0, adj))
                log.debug("[MERGED] %s clamped adj %.1f→%.1f", sym, adj, clamped - l2_score)
                final = clamped
                adj   = final - l2_score
            row["score"]         = round(final, 1)
            row["l2_score"]      = round(l2_score, 1)
            row["l3_adjustment"] = round(adj, 1)
            row.setdefault("adjustment_reason", "")
            row.setdefault("orb_candidate", bool(l2.get("orb_candidate")))
            row.setdefault("pattern_watchlist", l2.get("pattern_watchlist") or False)
            if "earnings_days_away" not in row and l2.get("earnings_days_away") is not None:
                row["earnings_days_away"] = l2["earnings_days_away"]
            _haiku_ct = (row.get("catalyst_type") or "").strip().lower().replace("-", "_")
            try:
                from semantic_labels import CatalystType as _CT  # noqa: PLC0415
                from semantic_labels import classify_catalyst as _cc
                _known = {e.value for e in _CT}
                if _haiku_ct and _haiku_ct in _known and _haiku_ct != "unknown":
                    row["catalyst_type"] = _haiku_ct
                elif row.get("primary_catalyst"):
                    row["catalyst_type"] = _cc(row.get("primary_catalyst", "") or "").value
                else:
                    row["catalyst_type"] = "unknown"
            except Exception:
                row["catalyst_type"] = "unknown"
            merged_symbols[sym] = row
        else:
            merged_symbols[sym] = _l2_to_signal_score(sym, l2)

    # L2-only fallback for symbols beyond _MAX_MERGED
    for sym in l2_only_syms:
        merged_symbols[sym] = _l2_to_signal_score(sym, l2_scores.get(sym, {}))

    if not merged_symbols:
        log.warning("[MERGED] No symbols scored — full L2 fallback")
        for sym, l2 in l2_scores.items():
            merged_symbols[sym] = _l2_to_signal_score(sym, l2)

    sorted_syms = sorted(
        merged_symbols.items(),
        key=lambda kv: float(kv[1].get("score", 0)) if isinstance(kv[1], dict) else 0,
        reverse=True,
    )
    seen_c: set = set()
    caution_raw = merged_result.get("elevated_caution") or []
    result = {
        "scored_symbols":     merged_symbols,
        "top_3":              [s for s, _ in sorted_syms[:3]],
        "elevated_caution":   [s for s in caution_raw if not (s in seen_c or seen_c.add(s))],
        "reasoning":          merged_result.get("reasoning", ""),
        "l1_staleness_minutes": int(staleness),
        "l3_used":            True,
    }

    sp_raw = merged_result.get("scratchpad")
    if isinstance(sp_raw, dict):
        result["scratchpad"] = sp_raw
        log.info(
            "[MERGED] %d symbols (Haiku=%d L2=%d)  top_3=%s  watching=%s",
            len(merged_symbols), len(haiku_symbols), len(l2_only_syms),
            result["top_3"], sp_raw.get("watching", []),
        )
    else:
        log.warning("[MERGED] scratchpad section absent — standalone scratchpad will run")
        log.info(
            "[MERGED] %d symbols (Haiku=%d L2=%d)  top_3=%s",
            len(merged_symbols), len(haiku_symbols), len(l2_only_syms),
            result["top_3"],
        )

    return result


def _l2_to_signal_score(sym: str, l2: dict) -> dict:
    """Project a pure L2 result into the legacy SignalScore shape. Used as
    fallback when L3 is unavailable or silently drops a symbol."""
    if not l2:
        return {
            "score": 50.0, "direction": "neutral", "conviction": "low",
            "signals": [], "conflicts": ["l2_missing"],
            "primary_catalyst": "",
            "catalyst_type": "unknown",
            "orb_candidate": False, "pattern_watchlist": False,
            "tier": "dynamic",
            "l2_score": 50.0, "l3_adjustment": 0.0,
            "adjustment_reason": "l3_unavailable",
        }
    return {
        "score":             float(l2.get("score", 50.0)),
        "direction":         l2.get("direction", "neutral"),
        "conviction":        l2.get("conviction", "low"),
        "signals":           list(l2.get("signals", []) or []),
        "conflicts":         list(l2.get("conflicts", []) or []),
        "primary_catalyst":  "",
        "catalyst_type":     "unknown",
        "orb_candidate":     bool(l2.get("orb_candidate")),
        "pattern_watchlist": l2.get("pattern_watchlist") or False,
        "tier":              "dynamic",
        "l2_score":          float(l2.get("score", 50.0)),
        "l3_adjustment":     0.0,
        "adjustment_reason": "l3_unavailable_or_skip",
        "earnings_days_away": l2.get("earnings_days_away"),
    }


# ── Avoid log + short-rule helpers ───────────────────────────────────────────

def _write_avoid_log(scored_symbols: dict, cycle_id: str) -> None:
    """Append one JSONL record per conviction=avoid symbol to data/analytics/avoid_log.jsonl.
    Rotates at _AVOID_LOG_MAX_BYTES."""
    avoid_entries = [
        (sym, data) for sym, data in scored_symbols.items()
        if isinstance(data, dict) and data.get("conviction") == "avoid"
    ]
    if not avoid_entries:
        return
    try:
        _AVOID_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if _AVOID_LOG_PATH.exists() and _AVOID_LOG_PATH.stat().st_size >= _AVOID_LOG_MAX_BYTES:
            ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            _AVOID_LOG_PATH.rename(
                _AVOID_LOG_PATH.parent / f"avoid_log_{ts_str}.jsonl"
            )
        ts = datetime.now(timezone.utc).isoformat()
        with _AVOID_LOG_PATH.open("a") as fh:
            for sym, data in avoid_entries:
                signals = data.get("signals") or []
                fh.write(json.dumps({
                    "ts":        ts,
                    "symbol":    sym,
                    "conviction": "avoid",
                    "reason":    ",".join(signals[:5]) if signals else "",
                    "signals":   signals,
                    "conflicts": data.get("conflicts") or [],
                    "direction": data.get("direction", "neutral"),
                    "score":     data.get("score", 0),
                    "cycle_id":  cycle_id,
                }) + "\n")
        log.info("[SIGNALS] avoid_log: wrote %d avoid record(s) for cycle %s", len(avoid_entries), cycle_id)
    except Exception as exc:
        log.warning("[SIGNALS] avoid_log write failed (non-fatal): %s", exc)


def _maybe_append_short_rule(scored_symbols: dict) -> None:
    """If any conviction=avoid symbol has ≥2 bearish signals, append a SHORT SELLING RULE
    instruction to morning_brief_sonnet.json avoid_line for the current cycle."""
    try:
        short_candidates: list[tuple[str, list[str]]] = []
        for sym, data in scored_symbols.items():
            if not (isinstance(data, dict) and data.get("conviction") == "avoid"):
                continue
            signals = [str(s).lower() for s in (data.get("signals") or [])]
            bearish_hits = [s for s in signals if any(kw in s for kw in _BEARISH_KEYWORDS)]
            if len(bearish_hits) >= 2:
                short_candidates.append((sym, bearish_hits[:3]))

        if not short_candidates:
            return

        if not _SONNET_BRIEF_PATH.exists():
            return
        brief = json.loads(_SONNET_BRIEF_PATH.read_text())
        current_avoid = brief.get("avoid_line", "")
        # Strip any prior short rule to avoid stacking across cycles
        if "\nSHORT SELLING RULE:" in current_avoid:
            current_avoid = current_avoid.split("\nSHORT SELLING RULE:")[0]

        candidates_str = ", ".join(
            f"{sym} ({','.join(hits)})" for sym, hits in short_candidates
        )
        short_rule = (
            f"\nSHORT SELLING RULE: enter_short is valid for {candidates_str} "
            "even in risk_on regime — 2+ confirmed bearish signals. "
            "Size ≤50% of a normal long entry. Risk kernel validates stop placement."
        )
        brief["avoid_line"] = current_avoid + short_rule
        _SONNET_BRIEF_PATH.write_text(json.dumps(brief))
        log.info("[SIGNALS] short_rule appended for %d symbol(s): %s",
                 len(short_candidates), [s for s, _ in short_candidates])
    except Exception as exc:
        log.warning("[SIGNALS] short_rule append failed (non-fatal): %s", exc)


# ── Crypto mean-reversion signal ─────────────────────────────────────────────

_CRYPTO_DROP_CACHE: dict[str, tuple[float, float]] = {}  # yf_symbol -> (drop_pct, ts)
_CRYPTO_CACHE_TTL = 600.0  # seconds — 10-minute TTL per symbol


def _load_crypto_config() -> dict:
    """Read strategy_config.json from disk. Returns {} on any failure."""
    try:
        path = _BASE / "strategy_config.json"
        return json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        return {}


def _compute_4h_drop(symbol_yf: str) -> float | None:
    """Fetch last 8h of hourly closes via yfinance.

    Returns pct change from 4h ago to now (negative = drop).
    Returns None on any fetch failure. Caches result for 10 minutes.
    symbol_yf: yfinance format e.g. 'BTC-USD', 'ETH-USD'
    """
    now = time.time()
    cached = _CRYPTO_DROP_CACHE.get(symbol_yf)
    if cached is not None and (now - cached[1]) < _CRYPTO_CACHE_TTL:
        return cached[0]
    try:
        import yfinance as yf  # noqa: PLC0415
        hist = yf.Ticker(symbol_yf).history(period="2d", interval="1h")
        if hist is None or hist.empty or len(hist) < 4:
            return None
        closes = hist["Close"].tolist()
        drop = (closes[-1] - closes[-4]) / closes[-4] * 100.0
        _CRYPTO_DROP_CACHE[symbol_yf] = (drop, now)
        return drop
    except Exception as exc:
        log.debug("[SIGNALS] _compute_4h_drop(%s) failed: %s", symbol_yf, exc)
        return None


def _maybe_append_crypto_reversion_signal(scored_symbols: dict, config: dict) -> None:
    """Inject crypto mean-reversion signals into scored_symbols when 4h drop threshold is met."""
    cr = config.get("crypto_reversion", {})
    if not cr.get("crypto_reversion_enabled", False):
        return

    # Cross-symbol halt: skip all signals after 3 combined losses
    try:
        from crypto_reversion_state import CryptoReversionState  # noqa: PLC0415
        _state = CryptoReversionState()
        if _state.is_halted():
            log.info("[CRYPTO-MR] halted — 3+ combined losses, suppressing all crypto reversion signals")
            return
    except ImportError:
        pass
    except Exception as _se:
        log.debug("[CRYPTO-MR] halt check failed (non-fatal): %s", _se)

    _PAIRS = [("btc", "BTC/USD", "BTC-USD"), ("eth", "ETH/USD", "ETH-USD")]
    for cfg_key, symbol, symbol_yf in _PAIRS:
        cfg = cr.get(cfg_key, {})
        if not cfg:
            continue
        threshold = float(cfg.get("drop_threshold_pct", 1.0))
        drop = _compute_4h_drop(symbol_yf)
        if drop is None:
            log.debug("[SIGNALS] %s: drop compute returned None, skipping", symbol)
            continue
        if drop > -threshold:
            continue
        catalyst_str = f"crypto_reversion_setup: {symbol} dropped {drop:.1f}% in 4h"
        scored_symbols[symbol] = {
            "score": 72,
            "direction": "bullish",
            "conviction": "medium",
            "primary_catalyst": catalyst_str,
            "catalyst": catalyst_str,
            "catalyst_type": "mean_reversion",
            "signals": ["crypto_mean_reversion", "4h_drop_detected"],
            "drop_pct": drop,
            "stop_pct": float(cfg.get("stop_pct", 1.5)),
            "target_reversion_pct": float(cfg.get("target_reversion_pct", 100)),
            "max_hold_hours": int(cfg.get("max_hold_hours", 8)),
            "sizing_multiplier": float(cfg.get("sizing_multiplier", 0.8)),
        }
        log.info(
            "[CRYPTO-MR] injecting %s signal score=72 drop=%.2f%% threshold=%.1f%%",
            symbol, drop, threshold,
        )


# ═════════════════════════════════════════════════════════════════════════════
# score_signals_layered — public entry point (replaces score_signals)
# ═════════════════════════════════════════════════════════════════════════════

def score_signals_layered(
    watchlist_symbols: list,
    regime: dict,
    md: dict,
    positions: list = None,
) -> dict:
    """Three-layer signal scoring.

    L2 (python)  — numerical anchor per symbol (zero API calls)
    L1 (Sonnet)  — qualitative context, read-only from disk
    L3 (Haiku)   — synthesise L1 + L2 into final SignalScore shape

    Drop-in replacement for the legacy score_signals(). Returns the same
    top-level shape ({"scored_symbols", "top_3", "elevated_caution",
    "reasoning"}) plus two new metadata fields (`l1_staleness_minutes`,
    `l3_used`) which existing consumers safely ignore.
    """
    if not watchlist_symbols:
        return {}

    try:
        _MAX_SCORED = 91
        scored: list[str] = []
        seen: set[str] = set()

        def _add(sym: str) -> None:
            if sym in seen or sym not in watchlist_symbols:
                return
            scored.append(sym)
            seen.add(sym)

        # Priority 1: held positions
        for p in (positions or []):
            if float(getattr(p, "qty", 0)) > 0:
                _add(getattr(p, "symbol", ""))

        # Priority 2: morning brief conviction picks (structural only — no prose)
        try:
            _brief_path = _BASE / "data" / "market" / "morning_brief.json"
            if _brief_path.exists():
                _brief = json.loads(_brief_path.read_text())
                for pick in (_brief.get("conviction_picks") or []):
                    _add(str(pick.get("symbol", "")))
        except Exception:
            pass

        # Priority 3: breaking news mentions
        _news = (md or {}).get("breaking_news", "") or ""
        for sym in watchlist_symbols:
            if sym in _news:
                _add(sym)

        # Priority 4: fill remainder
        for sym in watchlist_symbols:
            if len(scored) >= _MAX_SCORED:
                break
            _add(sym)

        log.debug("[SIGNALS_L] scoring %d/%d symbols", len(scored), len(watchlist_symbols))

        # ── L2: python deterministic (ZERO API calls) ─────────────────────────
        try:
            from bot_stage2_python import score_all_symbols_python  # noqa: PLC0415
            l2_scores = score_all_symbols_python(scored, md or {}, regime or {})
        except Exception as exc:
            log.warning("[SIGNALS_L] L2 python scoring failed: %s — falling back to legacy scorer", exc)
            return score_signals(watchlist_symbols, regime, md, positions)

        # ── L1: qualitative context (read-only from disk) ────────────────────
        qual_ctx = _load_qualitative_context()

        # ── L3+Scratchpad: single merged Haiku call ──────────────────────────
        result = _run_merged_synthesis(scored, l2_scores, qual_ctx, regime or {}, positions, md or {})

        log_trade({
            "event":             "signal_scoring",
            "layer_architecture": "L2+L3",
            "top_3":             result.get("top_3", []),
            "elevated_caution":  result.get("elevated_caution", []),
            "scored_count":      len(result.get("scored_symbols", {})),
            "l1_staleness_min":  result.get("l1_staleness_minutes", 0),
        })

        # Fix 24: persist avoid-conviction symbols for retrospective analysis
        _cycle_id = datetime.now(timezone.utc).strftime("cycle_%Y%m%d_%H%M%S")
        _write_avoid_log(result.get("scored_symbols", {}), _cycle_id)
        # Fix 25: enrich avoid_line with SHORT SELLING RULE when signals are bearish
        _maybe_append_short_rule(result.get("scored_symbols", {}))
        _maybe_append_crypto_reversion_signal(result.get("scored_symbols", {}), _load_crypto_config())

        # Archive to daily_conviction.json (unchanged from legacy behaviour)
        try:
            conv_path = _BASE / "data" / "market" / "daily_conviction.json"
            existing: list = []
            if conv_path.exists():
                try:
                    existing = json.loads(conv_path.read_text())
                    if not isinstance(existing, list):
                        existing = []
                except Exception:
                    existing = []
            existing.append({
                "ts": datetime.now().isoformat(),
                "top_3": result.get("top_3", []),
                "l1_staleness_min": result.get("l1_staleness_minutes", 0),
            })
            conv_path.write_text(json.dumps(existing[-50:], indent=2))
        except Exception:
            pass

        return result

    except Exception as exc:
        log.warning("[SIGNALS_L] Layered scorer failed (%s) — falling back to legacy", exc)
        try:
            return score_signals(watchlist_symbols, regime, md, positions)
        except Exception:
            return {}


# ═════════════════════════════════════════════════════════════════════════════
# Legacy single-layer Haiku scorer (kept as fallback)
# ═════════════════════════════════════════════════════════════════════════════

_SIGNAL_SYS = (
    "You are a signal scorer for a trading bot. Score symbols based on available signals. "
    "JSON only, no markdown.\n"
    "Output: "
    '{"scored_symbols":{"SYMBOL":{"score":<0-100>,"signals":[<strings>],'
    '"conflicts":[<strings>],"conviction":"high"|"medium"|"low"|"avoid",'
    '"primary_catalyst":"<one sentence>","orb_candidate":true|false,'
    '"pattern_watchlist":false|"<caution note>",'
    '"direction":"bullish"|"bearish"|"neutral",'
    '"tier":"core"|"dynamic"}},'
    '"top_3":["SYM1","SYM2","SYM3"],"elevated_caution":["SYM4"],'
    '"reasoning":"<2 sentences>"}'
)


def _call_single_batch(user_content: str) -> dict:
    """Legacy single-Haiku-call batch scorer. Preserved for fallback."""
    resp = _get_claude().messages.create(
        model=MODEL_FAST, max_tokens=8192,
        system=[{
            "type": "text",
            "text": _SIGNAL_SYS,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_content}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )
    try:
        from cost_tracker import get_tracker
        get_tracker().record_api_call(MODEL_FAST, resp.usage, caller="signal_scorer")
    except Exception as _ct_exc:
        log.debug("[SIGNALS] Cost tracker failed: %s", _ct_exc)
    try:
        from cost_attribution import log_claude_call_to_spine
        log_claude_call_to_spine("signal_scorer", MODEL_FAST, "signal_scoring", resp.usage)
    except Exception:
        pass
    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        last_brace = raw.rfind("}")
        if last_brace >= 0:
            try:
                result = json.loads(raw[:last_brace + 1])
                log.debug("[SIGNALS] JSON repaired by truncation (last_brace=%d)", last_brace)
                return result
            except json.JSONDecodeError:
                pass
        log.debug("[SIGNALS] JSON truncated, retrying API call with completeness hint")
        _retry_sys = _SIGNAL_SYS + "\nReturn ONLY valid complete JSON. If you cannot fit all symbols, return fewer rather than truncating."
        _retry_resp = _get_claude().messages.create(
            model=MODEL_FAST, max_tokens=8192,
            system=[{"type": "text", "text": _retry_sys,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_content}],
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
        _retry_raw = _retry_resp.content[0].text.strip()
        if _retry_raw.startswith("```"):
            _retry_raw = _retry_raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return json.loads(_retry_raw)


def score_signals(
    watchlist_symbols: list,
    regime: dict,
    md: dict,
    positions: list = None,
) -> dict:
    """Legacy single-layer Haiku scorer — kept as a fallback path.

    bot.py imports `score_signals_layered` (aliased as `score_signals` at call
    time), so this function is only used when the layered path fails.

    Symbol list is capped at _MAX_SCORED, prioritised:
      1. Currently held positions (always included)
      2. Morning brief conviction picks
      3. Breaking-news mentions this cycle
      4. Remaining watchlist symbols
    """
    if not watchlist_symbols:
        return {}
    try:
        _MAX_SCORED = 91
        scored: list[str] = []
        seen: set[str] = set()

        def _add(sym: str) -> None:
            if sym in seen or sym not in watchlist_symbols:
                return
            scored.append(sym)
            seen.add(sym)

        for p in (positions or []):
            if float(getattr(p, "qty", 0)) > 0:
                _add(p.symbol)

        try:
            _brief_path = _BASE / "data" / "market" / "morning_brief.json"
            if _brief_path.exists():
                _brief = json.loads(_brief_path.read_text())
                for pick in _brief.get("conviction_picks", []):
                    _add(str(pick.get("symbol", "")))
        except Exception:
            pass

        _news = md.get("breaking_news", "") or ""
        for sym in watchlist_symbols:
            if sym in _news:
                _add(sym)

        for sym in watchlist_symbols:
            if len(scored) >= _MAX_SCORED:
                break
            _add(sym)

        log.debug("[SIGNALS] scoring %d/%d symbols: %s",
                  len(scored), len(watchlist_symbols), scored)

        insider_lines = [l.strip() for l in (md.get("insider_section", "") or "").splitlines()
                         if any(s in l for s in scored)][:10]
        orb_str = "(none)"
        try:
            orb_path = _BASE / "data" / "scanner" / "orb_candidates.json"
            if orb_path.exists():
                orb_cands = json.loads(orb_path.read_text()).get("candidates", [])
                orb_str = "\n".join(
                    f"{c['symbol']}: gap {c['gap_pct']:+.1f}% score={c['orb_score']:.2f} {c['conviction']}"
                    for c in orb_cands[:8]
                ) or "(none)"
        except Exception:
            pass
        reddit_lines = [l.strip() for l in (md.get("reddit_section", "") or "").splitlines()
                        if any(s in l for s in scored)][:6]
        morning_lines = (md.get("morning_brief_section", "") or "").splitlines()[:5]
        from memory import _load_pattern_watchlist  # noqa: PLC0415
        pwl       = _load_pattern_watchlist()
        pwl_lines = [f"{s}: min {d.get('minimum_signals_required', 2)} signals required"
                     for s, d in pwl.items() if not d.get("graduated") and s in scored]

        regime_line = (
            f"REGIME: score={regime.get('regime_score', 50)} bias={regime.get('bias', 'neutral')} "
            f"theme={regime.get('session_theme', '?')}\n"
            f"  constraints: {regime.get('constraints', [])}"
        )

        merged_symbols: dict = {}
        all_caution: list = []
        all_reasoning: list = []

        _it = iter(scored)
        while True:
            batch = list(islice(_it, _LEGACY_BATCH_SIZE))
            if not batch:
                break
            b_insider = [l for l in insider_lines if any(s in l for s in batch)]
            b_reddit  = [l for l in reddit_lines  if any(s in l for s in batch)]
            b_pwl     = [l for l in pwl_lines     if any(l.startswith(s) for s in batch)]
            batch_content = (
                f"Symbols to score: {', '.join(batch)}\n\n"
                f"{regime_line}\n\n"
                f"INSIDER/CONGRESSIONAL:\n{chr(10).join(b_insider) or '(none)'}\n\n"
                f"ORB CANDIDATES:\n{orb_str}\n\n"
                f"REDDIT:\n{chr(10).join(b_reddit) or '(none)'}\n\n"
                f"MORNING BRIEF picks:\n{chr(10).join(morning_lines) or '(none)'}\n\n"
                f"PATTERN WATCHLIST (elevated conviction required):\n{chr(10).join(b_pwl) or '(none)'}"
            )
            try:
                batch_result = _call_single_batch(batch_content)
            except Exception as _be:
                log.warning("[SIGNALS] Batch %s failed: %s", batch, _be)
                continue
            merged_symbols.update(batch_result.get("scored_symbols", {}))
            all_caution.extend(batch_result.get("elevated_caution", []))
            if batch_result.get("reasoning"):
                all_reasoning.append(batch_result["reasoning"])
            log.debug("[SIGNALS] batch scored %d symbols", len(batch_result.get("scored_symbols", {})))

        if not merged_symbols:
            log.warning("[SIGNALS] All batches failed — returning empty")
            return {}

        _maybe_append_crypto_reversion_signal(merged_symbols, _load_crypto_config())
        sorted_syms = sorted(merged_symbols.items(), key=lambda kv: kv[1].get("score", 0), reverse=True)
        seen_c: set = set()
        result = {
            "scored_symbols": merged_symbols,
            "top_3": [s for s, _ in sorted_syms[:3]],
            "elevated_caution": [s for s in all_caution if not (s in seen_c or seen_c.add(s))],
            "reasoning": " | ".join(all_reasoning),
        }
        log.info("[SIGNALS] top_3=%s  caution=%s", result.get("top_3", []), result.get("elevated_caution", []))
        log_trade({"event": "signal_scoring", "top_3": result.get("top_3", []),
                   "elevated_caution": result.get("elevated_caution", []),
                   "scored_count": len(result.get("scored_symbols", {}))})
        try:
            from datetime import datetime as _dt
            conv_path = _BASE / "data" / "market" / "daily_conviction.json"
            existing: list = []
            if conv_path.exists():
                try:
                    existing = json.loads(conv_path.read_text())
                    if not isinstance(existing, list):
                        existing = []
                except Exception:
                    existing = []
            existing.append({"ts": _dt.now().isoformat(), "top_3": result.get("top_3", [])})
            conv_path.write_text(json.dumps(existing[-50:], indent=2))
        except Exception:
            pass
        return result
    except Exception as exc:
        log.error("[SIGNALS] Scorer failed (non-fatal): %s", exc)
        _fire_safety_alert("score_signals", exc)
        return {}


# ═════════════════════════════════════════════════════════════════════════════
# Format helper (unchanged)
# ═════════════════════════════════════════════════════════════════════════════

def _alpha_annotation(symbol: str) -> str:
    """Return ` [alpha: X% WR / Nt]` if get_alpha_summary has data, else ""."""
    try:
        from decision_outcomes import get_alpha_summary  # noqa: PLC0415
        summary = get_alpha_summary(symbol)
    except Exception:
        return ""
    if not summary:
        return ""
    n = int(summary.get("n", 0) or 0)
    if n <= 0:
        return ""
    wr = float(summary.get("win_rate", 0.0) or 0.0)
    return f" [alpha: {wr:.0%} WR / {n}t]"


def format_signal_scores(scores: dict) -> str:
    if not scores:
        return "  (signal scoring unavailable this cycle)"
    lines = []
    if scores.get("top_3"):
        lines.append(f"  Top conviction today: {', '.join(scores['top_3'])}")
    if scores.get("elevated_caution"):
        lines.append(f"  Elevated caution: {', '.join(scores['elevated_caution'])}")
    if scores.get("reasoning"):
        lines.append(f"  Signal environment: {scores['reasoning']}")

    all_syms = scores.get("scored_symbols", {})
    crypto_mr = {s: d for s, d in all_syms.items() if d.get("catalyst_type") == "mean_reversion"}
    equity_syms = [(s, d) for s, d in all_syms.items() if d.get("catalyst_type") != "mean_reversion"]

    # Crypto reversion entries always shown first, outside the equity [:10] cap
    for sym, d in crypto_mr.items():
        drop = d.get("drop_pct", 0.0)
        stop = d.get("stop_pct", 1.5)
        lines.append(
            f"  [CRYPTO-MR] {sym}: score={d.get('score', 0)} drop={drop:.2f}% "
            f"stop={stop}% -- enter_long required"
        )

    for sym, d in equity_syms[:10]:
        conv      = d.get("conviction", "?")
        cat       = (d.get("primary_catalyst", "") or "")[:60]
        sigs      = ", ".join((d.get("signals", []) or [])[:4])
        orb_tag   = " ORB" if d.get("orb_candidate") else ""
        pwl_tag   = f"  ⚠{d['pattern_watchlist']}" if d.get("pattern_watchlist") else ""
        alpha     = _alpha_annotation(sym)
        _eda      = d.get("earnings_days_away")
        pe_tag    = " [POST-EARNINGS]" if (_eda is not None and _eda < 0) else ""
        lines.append(f"  {sym}: score={d.get('score', 0)} [{conv}]{orb_tag}{pe_tag}  {cat}{pwl_tag}{alpha}")
        if sigs:
            lines.append(f"    signals: {sigs}")
    return "\n".join(lines) if lines else "  (no signals scored)"
