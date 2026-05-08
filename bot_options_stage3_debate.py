"""
bot_options_stage3_debate.py — A2 Stage 3: Claude four-way debate.

Public API:
  run_bounded_debate(candidate_sets, candidates, candidate_structures,
                     allowed_by_sym, equity, vix, regime, account1_summary,
                     obs_mode, session_tier, t_start)
      -> A2DecisionRecord

Responsibilities:
  - Prompt assembly (bounded A2-3b and legacy free-form paths)
  - Claude Sonnet call with prompt caching
  - JSON extraction and parsing
  - A2DecisionRecord construction
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from log_setup import get_logger

log = get_logger(__name__)

ET = ZoneInfo("America/New_York")
PROMPTS_DIR = Path(__file__).parent / "prompts"

MODEL = "claude-sonnet-4-6"

# ── OCC symbol detection ──────────────────────────────────────────────────────

_OCC_RE = re.compile(r'^[A-Z/]+\d{6}[CP]\d{8}$')


def _is_options_occ_symbol(sym: str) -> bool:
    """True if sym matches OCC option format (e.g. GOOGL260522C00390000)."""
    return bool(_OCC_RE.match((sym or "").upper()))


# ── Existing position context builder ─────────────────────────────────────────

def _build_existing_position_context(
    underlying: str,
    all_structures: list,
    alpaca_positions: list | None,
) -> str:
    """
    Return a formatted string describing existing positions on `underlying`.

    all_structures:   list[OptionsStructure] — full structures.json contents
    alpaca_positions: list of alpaca Position objects (options only); None if fetch failed

    Covers active tracked structures AND orphan Alpaca positions (in broker but
    not covered by any tracked structure leg).
    """
    _ACTIVE_LC = {"submitted", "partially_filled", "fully_filled"}
    underlying_upper = underlying.upper()

    active_structs: list = []
    all_tracked_occs: set[str] = set()
    for s in all_structures:
        if (getattr(s, "underlying", "") or "").upper() != underlying_upper:
            continue
        for leg in (getattr(s, "legs", None) or []):
            occ = (getattr(leg, "occ_symbol", "") or "").upper()
            if occ:
                all_tracked_occs.add(occ)
        lc_val = (
            s.lifecycle.value
            if hasattr(s.lifecycle, "value")
            else str(s.lifecycle)
        )
        if lc_val in _ACTIVE_LC:
            active_structs.append(s)

    orphan_positions: list = []
    if alpaca_positions is not None:
        for pos in alpaca_positions:
            pos_sym = (getattr(pos, "symbol", "") or "").upper()
            if not pos_sym.startswith(underlying_upper):
                continue
            if pos_sym not in all_tracked_occs:
                orphan_positions.append(pos)

    if not active_structs and not orphan_positions:
        if alpaca_positions is None:
            return (
                f"EXISTING POSITIONS ON {underlying}: "
                f"Alpaca fetch failed — orphan detection unavailable. "
                f"No tracked structures found."
            )
        return f"No existing positions on {underlying}."

    lines = [f"EXISTING POSITIONS ON {underlying}:"]
    total_unrealized = 0.0

    for s in active_structs:
        lc_val = (
            s.lifecycle.value if hasattr(s.lifecycle, "value") else str(s.lifecycle)
        )
        strat_val = (
            s.strategy.value if hasattr(s.strategy, "value") else str(s.strategy)
        )
        opened = (getattr(s, "opened_at", "") or "")[:10]
        lines.append(
            f"  [TRACKED] {strat_val}  lifecycle={lc_val}  opened={opened}"
        )
        for leg in (getattr(s, "legs", None) or []):
            side_str = "LONG" if getattr(leg, "side", "") == "buy" else "SHORT"
            fp = getattr(leg, "filled_price", None)
            entry_str = f"avg_entry ${fp:.2f}" if fp is not None else "entry unknown"
            lines.append(
                f"    {getattr(leg, 'occ_symbol', '?')}  {side_str}  "
                f"{getattr(leg, 'qty', '?')} contracts  {entry_str}"
            )
        pnl = getattr(s, "pnl_unrealized", None)
        if pnl is not None:
            lines.append(f"    Unrealized P&L: ${pnl:+,.2f}")
            total_unrealized += pnl

    for pos in orphan_positions:
        pos_sym = getattr(pos, "symbol", "?")
        try:
            qty_raw = float(getattr(pos, "qty", 0) or 0)
            side_str = "LONG" if qty_raw >= 0 else "SHORT"
            qty_abs = int(abs(qty_raw))
            avg_entry = float(getattr(pos, "avg_entry_price", 0) or 0)
            unreal_pl = float(getattr(pos, "unrealized_pl", 0) or 0)
            unreal_plpc = float(getattr(pos, "unrealized_plpc", 0) or 0) * 100
            lines.append(
                f"  [UNTRACKED] {pos_sym}  {side_str}  {qty_abs} contracts  "
                f"avg_entry ${avg_entry:.2f}  "
                f"P&L: ${unreal_pl:+,.2f} ({unreal_plpc:+.1f}%)"
            )
            total_unrealized += unreal_pl
        except Exception:
            lines.append(f"  [UNTRACKED] {pos_sym}  (details unavailable)")

    n_tracked_legs = sum(
        len(getattr(s, "legs", None) or []) for s in active_structs
    )
    n_orphan = len(orphan_positions)
    parts: list[str] = []
    if n_tracked_legs:
        parts.append(f"{n_tracked_legs} tracked leg(s)")
    if n_orphan:
        parts.append(f"{n_orphan} untracked position(s)")
    if alpaca_positions is None:
        lines.append("  Note: Alpaca fetch failed — orphan detection skipped")
    if parts:
        lines.append(
            f"  NET: {' + '.join(parts)}  |  "
            f"Net unrealized: ${total_unrealized:+,.2f}"
        )

    return "\n".join(lines)


# ── Historical alpha context ──────────────────────────────────────────────────
# Decisions are classified post-hoc by decision_outcomes.classify_alpha into
# alpha_positive / alpha_negative / alpha_neutral / quality_positive_non_alpha.
# We surface a per-symbol summary so the debate has the same hindsight the
# weekly review would. Inject only when n>=_ALPHA_MIN_INJECT; mark "(thin
# sample)" below _ALPHA_THIN_THRESHOLD so the model can weight accordingly.

_ALPHA_THIN_THRESHOLD = 5  # below this we annotate as thin sample

_ALPHA_OUTCOME_EMOJI = {
    "alpha_positive": "✅",
    "alpha_negative": "❌",
    "alpha_neutral":  "➖",
}


def _format_alpha_summary_for_debate(symbol: str) -> str:
    """Return a debate-prompt-ready alpha block for `symbol`, or "" if too thin.

    Reads from decision_outcomes.get_alpha_summary which enforces the minimum
    sample threshold (currently 2) — we trust None as the "skip" signal.
    Samples below _ALPHA_THIN_THRESHOLD (5) are tagged "(thin sample)".

    Failure-tolerant: any exception returns "" so the debate proceeds without
    historical context rather than crashing.
    """
    try:
        from decision_outcomes import get_alpha_summary  # noqa: PLC0415
        summary = get_alpha_summary(symbol)
    except Exception as exc:
        log.debug("[OPTS] alpha summary fetch failed for %s: %s", symbol, exc)
        return ""

    if not summary:
        return ""
    n = int(summary.get("n", 0) or 0)

    win_rate = float(summary.get("win_rate", 0.0) or 0.0)
    avg      = float(summary.get("avg_outcome_pct", 0.0) or 0.0)
    last     = list(summary.get("last_outcomes") or summary.get("last_n_outcomes") or [])
    if last and "last_outcomes" not in summary:
        last = list(reversed(last[-3:]))   # legacy ordering → most-recent-first
    emojis   = " ".join(_ALPHA_OUTCOME_EMOJI.get(c, "❔") for c in last[:3]) or "—"
    thin     = " (thin sample)" if n < _ALPHA_THIN_THRESHOLD else ""
    return (
        f"HISTORICAL ALPHA — {symbol} (last {n} decisions{thin}):\n"
        f"  Win rate: {win_rate:.0%} | Avg outcome: {avg:+.1%}\n"
        f"  Recent: {emojis}"
    )


# ── Prompt system loader ──────────────────────────────────────────────────────

_OPTS_SYSTEM = None


def _load_opts_system() -> str:
    global _OPTS_SYSTEM
    if _OPTS_SYSTEM is None:
        path = PROMPTS_DIR / "system_options_v1.txt"
        _OPTS_SYSTEM = path.read_text().strip()
    return _OPTS_SYSTEM


# ── Cost tracking ─────────────────────────────────────────────────────────────

_COST_LOG = Path(__file__).parent / "data" / "account2" / "costs" / "cost_log.jsonl"


def _log_claude_cost(resp, call_type: str = "unknown"):
    """Log Claude API usage to Account 2 cost log."""
    try:
        usage = resp.usage
        entry = {
            "timestamp": datetime.now(ET).isoformat(),
            "call_type": call_type,
            "model": MODEL,
            "input_tokens": getattr(usage, "input_tokens", 0),
            "output_tokens": getattr(usage, "output_tokens", 0),
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0),
            "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0),
        }
        with open(_COST_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# ── Bounded debate response parsing ──────────────────────────────────────────

def _parse_bounded_debate_response(raw: str) -> dict:
    """
    Extract and parse bounded debate JSON from a Claude response.
    Handles markdown fences (```json / ``` wrappers).
    On any parse failure returns a reject_all sentinel dict.
    """
    _REJECT_ALL: dict = {
        "selected_candidate_id": None,
        "confidence": 0.0,
        "reject": True,
        "key_risks": [],
        "reasons": "json_parse_failed",
        "recommended_size_modifier": 1.0,
    }

    if not raw:
        return _REJECT_ALL

    text = raw.strip()

    # Find the last JSON fence block anywhere in the response (handles prose preamble)
    fence_matches = re.findall(r'```(?:json)?\s*\n(.*?)\n```', text, re.DOTALL)
    if fence_matches:
        text = fence_matches[-1].strip()

    # Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Extract last {...} block (last to avoid prose containing { before the real JSON)
    start = text.rfind("{")
    end   = text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    log.warning("[OPTS] _parse_bounded_debate_response: parse failed  raw=%s", raw[:200])
    return _REJECT_ALL


# ── Core debate function ──────────────────────────────────────────────────────

def run_options_debate(
    candidates: list,
    iv_summaries: dict,
    vix: float,
    regime: str,
    account1_summary: str,
    obs_mode: bool,
    equity: float,
    allowed_structures_by_symbol: dict | None = None,
    candidate_structures: list[dict] | None = None,
    conf_floor: float = 0.75,
    per_symbol_context: dict | None = None,
) -> tuple[dict, Optional[str], Optional[str]]:
    """
    A2-3b bounded adjudication debate.

    When candidate_structures is provided (A2-3b path):
      Prompt includes pre-built candidate dicts; AI picks ONE or rejects all.
      Returns (result_dict, prompt_used, raw_response)

    When candidate_structures is absent/empty (legacy fallback):
      Falls back to old free-form debate.
      Returns (result_dict, prompt_used, raw_response)
    """
    system_prompt = _load_opts_system()

    claude = _get_claude()

    # Format IV environment summary (used by both paths)
    iv_lines = []
    for sym, iv in iv_summaries.items():
        env   = iv.get("iv_environment", "unknown")
        rank  = iv.get("iv_rank")
        obs   = " [OBS]" if iv.get("observation_mode") else ""
        rank_s = f"{rank:.0f}" if rank is not None else "N/A"
        iv_lines.append(f"  {sym}: env={env} rank={rank_s}{obs}")
    iv_section = "\n".join(iv_lines) if iv_lines else "  (no IV data)"

    obs_notice = (
        "\n⚠ OBSERVATION MODE ACTIVE: Conduct full analysis but trades will NOT be submitted. "
        "Output your best trade decisions as if live — they are used for IV calibration.\n"
        if obs_mode else ""
    )

    # ── A2-3b bounded path ──────────────────────────────────────────────────
    if candidate_structures:
        candidate_blocks = []
        allowed_actions_parts = []
        for c in candidate_structures:
            cid   = c.get("candidate_id", "?")
            stype = c.get("structure_type", "?")
            sym   = c.get("symbol", "?")
            exp   = c.get("expiry", "?")
            ls    = c.get("long_strike", 0)
            ss    = c.get("short_strike")
            strike_str = f"{ls:.2f}/{ss:.2f}" if ss else f"{ls:.2f}"
            debit    = c.get("debit", 0) or 0
            max_loss = c.get("max_loss", 0) or 0
            max_gain = c.get("max_gain")
            gain_str = f"${max_gain:.0f}" if max_gain is not None else "unlimited"
            beven    = c.get("breakeven", 0) or 0
            delta    = c.get("delta")
            theta    = c.get("theta")
            vega     = c.get("vega")
            prob     = c.get("probability_profit")
            ev       = c.get("expected_value")
            dte      = c.get("dte", 0) or 0
            oi       = c.get("open_interest")
            delta_s  = f"{delta:.2f}" if delta is not None else "N/A"
            theta_s  = f"${theta:.3f}/day" if theta is not None else "N/A"
            vega_s   = f"{vega:.4f}" if vega is not None else "N/A"
            prob_s   = f"{prob:.1%}" if prob is not None else "N/A"
            ev_s     = f"${ev:.2f}" if ev is not None else "N/A"
            oi_s     = str(oi) if oi is not None else "N/A"
            # A1 signal context (Fix 2 — enriched in stage 1)
            a1_dir_c = c.get("a1_direction", "")
            a1_conv  = c.get("a1_conviction", "")
            a1_sc    = c.get("a1_score")
            a1_cat   = c.get("a1_primary_catalyst", "")
            a1_sc_s  = f"{a1_sc}/100" if a1_sc is not None else "N/A"
            a1_line  = (
                f"\n A1 signal: {a1_dir_c} | conviction={a1_conv} | score={a1_sc_s}"
                + (f" | {a1_cat}" if a1_cat else "")
            ) if (a1_dir_c or a1_conv) else ""
            _dir_lower = (a1_dir_c or "").lower()
            if _dir_lower == "bearish":
                mandate_line = (
                    f"\n ⚡ DIRECTION MANDATE: A1 is BEARISH on {sym}"
                    " — only select PUT structures or BEARISH CALL structures"
                )
            elif _dir_lower == "bullish":
                mandate_line = (
                    f"\n ⚡ DIRECTION MANDATE: A1 is BULLISH on {sym}"
                    " — only select CALL structures or BULLISH PUT structures"
                )
            else:
                mandate_line = ""
            _pos_ctx = (per_symbol_context or {}).get(
                sym, f"No existing positions on {sym}."
            )
            candidate_blocks.append(
                f"{_pos_ctx}\n\n"
                f"[Candidate {cid} — {stype} {sym} {exp} {strike_str}\n"
                f" Debit: ${debit:.2f}/share | Max loss: ${max_loss:.0f} | "
                f"Max gain: {gain_str} | Breakeven: {beven:.2f}\n"
                f" Delta: {delta_s} | Theta: {theta_s} | Vega: {vega_s} | "
                f"EV: {ev_s} | DTE: {dte} | OI: {oi_s} | P(profit): {prob_s}"
                f"{a1_line}{mandate_line}]"
            )
            allowed_actions_parts.append(f"prefer {cid}")
        allowed_actions_parts.append("reject_all")
        allowed_actions_str = ", ".join(allowed_actions_parts)
        candidate_blocks_text = "\n\n".join(candidate_blocks)
        risk_budget = equity * 0.05

        user_content = f"""{obs_notice}
=== MARKET CONTEXT ===
VIX: {vix:.2f}
Regime: {regime}
Account 2 Equity: ${equity:,.0f}

=== ACCOUNT 1 AWARENESS ===
{account1_summary}

=== IV ENVIRONMENT ===
{iv_section}

=== CANDIDATE STRUCTURES ===
{candidate_blocks_text}

RISK BUDGET: ${risk_budget:,.0f}
ALLOWED ACTIONS: {allowed_actions_str}

=== EXIT CRITERIA (applied automatically after entry) ===
These rules fire every cycle on all open structures:
• Profit target: gain ≥ 80% of max profit → close (target_profit_hit)
• Stop loss: loss ≥ 50% of max risk → close (stop_loss_hit)
• Expiry guard: DTE ≤ 2 → close to avoid assignment risk
• Time-stop: elapsed DTE ≥ 40% for single legs, ≥ 50% for debit spreads
• Thesis invalidation: A1 signal flips bearish on a call position → close
Factor these into entry decisions: avoid single legs with < 10 DTE remaining,
avoid debit spreads past 50% of their DTE window, and prefer structures
where max gain is meaningfully larger than the stop-loss trigger.

=== DEBATE ROLES ===
- DIRECTIONAL ADVOCATE: Is the underlying thesis real and is now the right time?
- VOL/STRUCTURE ANALYST: Which candidate has better premium geometry for this thesis?
  Also verify: the selected structure's directional exposure matches A1's signal direction.
  A long_put profits when the stock falls — correct for a bearish signal.
  A debit_call_spread profits when the stock rises — correct for a bullish signal.
  Flag any candidate whose profit direction contradicts A1's signal.
- TAPE/FLOW SKEPTIC: Does flow imbalance and positioning support or challenge this?
- RISK OFFICER: Which candidate best fits risk budget, theta horizon, and expiry?

Synthesize the debate and respond ONLY with this JSON — no other text:
{{
  "selected_candidate_id": "<candidate_id or null>",
  "confidence": <float 0.0-1.0>,
  "key_risks": ["<risk1>", "<risk2>"],
  "reasons": "<one paragraph max>",
  "recommended_size_modifier": 1.0,
  "reject": <true|false>
}}
Confidence >= {conf_floor:.2f} required for PROCEED. If rejecting all: selected_candidate_id=null, reject=true.
"""
        try:
            resp = claude.messages.create(
                model=MODEL,
                max_tokens=4000,
                system=[{
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": user_content}],
                extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
            )
            raw = resp.content[0].text.strip() if resp.content else ""
            _log_claude_cost(resp, "bounded_debate")
            try:
                from cost_attribution import log_claude_call_to_spine
                log_claude_call_to_spine("bot_options_stage3_debate", MODEL,
                                         "bounded_debate", resp.usage)
            except Exception:
                pass
        except Exception as exc:
            log.error("[OPTS] Bounded debate Claude call failed: %s", exc)
            return _parse_bounded_debate_response(""), user_content, ""

        result = _parse_bounded_debate_response(raw)
        if result.get("reject") or not result.get("selected_candidate_id"):
            log.info("[OPTS] Bounded debate: reject=True  reasons=%s",
                     result.get("reasons", "")[:120])
        else:
            log.info("[OPTS] Bounded debate: selected=%s  confidence=%.2f",
                     result.get("selected_candidate_id"), result.get("confidence", 0))
        return result, user_content, raw

    # ── Legacy free-form path (no pre-built candidates) ──────────────────────
    cands_text = json.dumps([asdict(c) for c in candidates], indent=2, default=str) if candidates else "[]"

    allowed_section = ""
    if allowed_structures_by_symbol:
        allowed_lines = [f"  {sym}: {al}" for sym, al in allowed_structures_by_symbol.items()]
        allowed_section = (
            "\n=== ALLOWED STRUCTURES (pre-approved by routing gate) ===\n"
            + "\n".join(allowed_lines)
            + "\nYou MUST only recommend structure types listed above for each symbol.\n"
        )

    user_content = f"""{obs_notice}
=== MARKET CONTEXT ===
VIX: {vix:.2f}
Regime: {regime}
Account 2 Equity: ${equity:,.0f}

=== ACCOUNT 1 AWARENESS ===
{account1_summary}

=== IV ENVIRONMENT SUMMARY ===
{iv_section}

=== CANDIDATE TRADES (from signal scoring) ===
{cands_text}
{allowed_section}
=== YOUR TASK ===
Conduct the four-way debate for each candidate:
1. BULL AGENT: strongest bull case with specific catalyst
2. BEAR AGENT: strongest bear case and key risks
3. IV ANALYST: IV rank assessment and recommended strategy
4. SYNTHESIS: PROCEED | VETO | RESIZE | RESTRUCTURE

Output your top 1-3 approved trades (or all HOLDs if no setup qualifies).
Minimum confidence {conf_floor:.2f} for any PROCEED. Apply all hard rules from system prompt.
Respond ONLY with valid JSON. No markdown. No explanation outside JSON fields.
"""
    raw = ""
    try:
        resp = claude.messages.create(
            model=MODEL,
            max_tokens=2000,
            system=[{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_content}],
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
        raw = resp.content[0].text.strip() if resp.content else ""
        _log_claude_cost(resp, "debate")
        try:
            from cost_attribution import log_claude_call_to_spine
            log_claude_call_to_spine("bot_options_stage3_debate", MODEL, "debate", resp.usage)
        except Exception:
            pass

        if not raw:
            log.warning("[OPTS] Claude returned empty response")
            return {"regime": regime, "actions": [], "reasoning": "empty response"}, user_content, raw

        try:
            return json.loads(raw), user_content, raw
        except json.JSONDecodeError:
            last_brace = raw.rfind("}")
            if last_brace >= 0:
                try:
                    return json.loads(raw[:last_brace + 1]), user_content, raw
                except json.JSONDecodeError:
                    pass
            log.warning("[OPTS] JSON parse failed, raw=%s", raw[:200])
            return {"regime": regime, "actions": [], "reasoning": "json_parse_failed"}, user_content, raw

    except Exception as exc:
        log.error("[OPTS] Claude debate failed: %s", exc)
        return {"regime": regime, "actions": [], "reasoning": f"error: {exc}"}, user_content, raw


# ── Strategy config loader ────────────────────────────────────────────────────

def _load_strategy_config() -> dict:
    """Load strategy_config.json. Returns {} on failure — non-fatal."""
    import json as _json  # noqa: PLC0415
    try:
        _cfg_path = Path(__file__).parent / "strategy_config.json"
        return _json.loads(_cfg_path.read_text(encoding="utf-8"))
    except Exception as _exc:
        log.debug("[OPTS] _load_strategy_config failed (non-fatal): %s", _exc)
        return {}


# ── Claude client (lazy-init, separate from A1 client) ────────────────────────

import os as _os

_claude_client = None


def _get_claude():
    global _claude_client
    if _claude_client is None:
        import anthropic  # noqa: PLC0415
        from dotenv import load_dotenv  # noqa: PLC0415
        load_dotenv()
        key = _os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise EnvironmentError("ANTHROPIC_API_KEY not set in .env")
        _claude_client = anthropic.Anthropic(api_key=key)
    return _claude_client


# ── Public API ────────────────────────────────────────────────────────────────

def run_bounded_debate(
    candidate_sets: list,
    candidates: list,
    candidate_structures: list[dict],
    allowed_by_sym: dict,
    equity: float,
    vix: float,
    regime: str,
    account1_summary: str,
    obs_mode: bool,
    session_tier: str,
    iv_summaries: dict,
    t_start: float,
    config: dict | None = None,
    alpaca_client=None,
) -> object:
    """
    Run the A2 four-way debate and return an A2DecisionRecord.

    Wraps run_options_debate(), captures prompt and raw response, and
    packages everything into a typed A2DecisionRecord for audit tracking.
    config is used to check a2_rollback flags before calling Claude.
    """
    from schemas import A2DecisionRecord, validate_no_trade_reason  # noqa: PLC0415

    if config is None:
        config = _load_strategy_config()

    # Rollback flag check — force_no_trade and disable_bounded_debate both skip debate.
    _rollback = config.get("a2_rollback", {})
    if _rollback.get("force_no_trade") or _rollback.get("disable_bounded_debate"):
        _flag = "force_no_trade" if _rollback.get("force_no_trade") else "disable_bounded_debate"
        log.warning("[OPTS] Rollback flag active: %s — skipping debate", _flag)
        _reason = validate_no_trade_reason("rollback_active")
        return A2DecisionRecord(
            decision_id="",
            session_tier=session_tier,
            candidate_sets=candidate_sets,
            debate_input=None,
            debate_output_raw=None,
            debate_parsed=None,
            selected_candidate=None,
            execution_result="no_trade",
            no_trade_reason=_reason,
            elapsed_seconds=time.monotonic() - t_start,
        )

    # Generate decision ID — A2 format: a2_dec_YYYYMMDD_HHMMSS
    _ts = datetime.now(ET).strftime("%Y%m%d_%H%M%S")
    decision_id = f"a2_dec_{_ts}"

    # Determine confidence floor from Alpaca base URL (paper vs live account)
    _a2_cfg = config.get("account2", {})
    _base_url = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    _is_paper = "paper-api.alpaca.markets" in _base_url.lower()
    _conf_floor = float(_a2_cfg.get(
        "paper_confidence_floor" if _is_paper else "live_confidence_floor",
        0.75 if _is_paper else 0.85,
    ))

    # Fetch existing positions once per cycle for debate context injection.
    # Alpaca call happens here (not per-candidate) to ensure caching.
    _all_structures: list = []
    _alpaca_positions: list | None = []  # empty list = fetch ok, no options positions
    try:
        from options_state import load_structures as _load_structures  # noqa: PLC0415
        _all_structures = _load_structures()
    except Exception as _ls_exc:
        log.warning("[OPTS] Could not load structures for debate context: %s", _ls_exc)

    if alpaca_client is not None:
        try:
            _raw = alpaca_client.get_all_positions()
            _alpaca_positions = [
                p for p in _raw
                if _is_options_occ_symbol(getattr(p, "symbol", ""))
            ]
            log.debug(
                "[OPTS] Fetched %d A2 option position(s) for debate context",
                len(_alpaca_positions),
            )
        except Exception as _ap_exc:
            log.warning(
                "[OPTS] Alpaca positions fetch failed for debate context: %s", _ap_exc
            )
            _alpaca_positions = None
    else:
        _alpaca_positions = None

    # Build per-symbol context once; reused for all candidates
    _candidate_syms = {c.get("symbol", "") for c in (candidate_structures or [])}
    _per_symbol_context: dict[str, str] = {}
    for _sym in _candidate_syms:
        if not _sym:
            continue
        _ctx = _build_existing_position_context(
            _sym, _all_structures, _alpaca_positions
        )
        _alpha_block = _format_alpha_summary_for_debate(_sym)
        if _alpha_block:
            _ctx = f"{_ctx}\n\n{_alpha_block}"
        _per_symbol_context[_sym] = _ctx

    # Position-intel greek context — append per-symbol intel for any drift
    # state ≠ NORMAL. Reads data/options/position_intel_latest.json (file-based;
    # decoupled from in-process state). Non-fatal: missing file → no inject.
    try:
        import options_position_manager as _opm  # noqa: PLC0415
        for _sym in list(_per_symbol_context.keys()):
            _recs = _opm.get_recommendations(symbol=_sym)
            for _rec in _recs:
                _drift = (_rec.details or {}).get("drift_state")
                if (_rec.action or "HOLD") == "HOLD" and _drift in (None, "NORMAL"):
                    continue
                _greek_block = "\n".join([
                    "",
                    f"POSITION INTELLIGENCE for {_sym}:",
                    f"  Drift state: {_drift or 'n/a'}",
                    f"  Recommended action: {_rec.action} ({_rec.urgency})",
                    f"  Reason: {_rec.reason}",
                ])
                _per_symbol_context[_sym] = (
                    _per_symbol_context.get(_sym, "") + "\n" + _greek_block
                )
    except Exception as _pi_exc:
        log.debug("[OPTS] position_intel greek context inject failed: %s", _pi_exc)

    debate_result, prompt_used, raw_response = run_options_debate(
        candidates=candidates,
        iv_summaries=iv_summaries,
        vix=vix,
        regime=regime,
        account1_summary=account1_summary,
        obs_mode=obs_mode,
        equity=equity,
        allowed_structures_by_symbol=allowed_by_sym or None,
        candidate_structures=candidate_structures or None,
        conf_floor=_conf_floor,
        per_symbol_context=_per_symbol_context,
    )

    log.info("[OPTS] Debate complete: bounded=%s  selected=%s  confidence=%s  reject=%s",
             bool(candidate_structures),
             debate_result.get("selected_candidate_id", "—"),
             debate_result.get("confidence", debate_result.get("regime", "?")),
             debate_result.get("reject", "—"),
             )

    # Determine no_trade_reason from debate result
    no_trade_reason: Optional[str] = None
    if candidate_structures:
        _reject = debate_result.get("reject", True)
        _sel_id = debate_result.get("selected_candidate_id")
        _conf   = float(debate_result.get("confidence", 0.0))
        if _reject or not _sel_id:
            no_trade_reason = "debate_rejected_all"
        elif _conf < _conf_floor:
            no_trade_reason = "debate_low_confidence"

    # Find selected candidate dict
    selected_candidate: Optional[dict] = None
    if candidate_structures and not no_trade_reason:
        _sel_id = debate_result.get("selected_candidate_id")
        selected_candidate = next(
            (c for c in candidate_structures if c.get("candidate_id") == _sel_id), None
        )
        if selected_candidate is None and _sel_id:
            log.warning("[OPTS] Bounded debate selected_candidate_id=%s not found", _sel_id)
            no_trade_reason = "debate_rejected_all"

    elapsed = time.monotonic() - t_start

    record = A2DecisionRecord(
        decision_id=decision_id,
        session_tier=session_tier,
        candidate_sets=candidate_sets,
        debate_input=prompt_used,
        debate_output_raw=raw_response,
        debate_parsed=debate_result,
        selected_candidate=selected_candidate,
        execution_result=None,         # set by Stage 4
        no_trade_reason=no_trade_reason,
        elapsed_seconds=elapsed,
    )
    return record
