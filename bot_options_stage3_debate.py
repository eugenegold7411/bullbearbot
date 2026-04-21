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

    # Strip markdown code fences
    if text.startswith("```"):
        lines = text.splitlines()
        inner: list[str] = []
        in_fence = False
        for line in lines:
            if line.startswith("```") and not in_fence:
                in_fence = True
                continue
            if line.startswith("```") and in_fence:
                break
            if in_fence:
                inner.append(line)
        text = "\n".join(inner).strip()

    # Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Extract first {...} block
    start = text.find("{")
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
            strike_str = f"{ls:.0f}/{ss:.0f}" if ss else f"{ls:.0f}"
            debit    = c.get("debit", 0) or 0
            max_loss = c.get("max_loss", 0) or 0
            max_gain = c.get("max_gain")
            gain_str = f"${max_gain:.0f}" if max_gain is not None else "unlimited"
            beven    = c.get("breakeven", 0) or 0
            delta    = c.get("delta")
            theta    = c.get("theta")
            ev       = c.get("expected_value")
            dte      = c.get("dte", 0) or 0
            oi       = c.get("open_interest")
            delta_s  = f"{delta:.2f}" if delta is not None else "N/A"
            theta_s  = f"${theta:.3f}/day" if theta is not None else "N/A"
            ev_s     = f"${ev:.2f}" if ev is not None else "N/A"
            oi_s     = str(oi) if oi is not None else "N/A"
            candidate_blocks.append(
                f"[Candidate {cid} — {stype} {sym} {exp} {strike_str}\n"
                f" Debit: ${debit:.2f}/share | Max loss: ${max_loss:.0f} | "
                f"Max gain: {gain_str} | Breakeven: {beven:.2f}\n"
                f" Delta: {delta_s} | Theta: {theta_s} | EV: {ev_s} | "
                f"DTE: {dte} | OI: {oi_s}]"
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

=== DEBATE ROLES ===
- DIRECTIONAL ADVOCATE: Is the underlying thesis real and is now the right time?
- VOL/STRUCTURE ANALYST: Which candidate has better premium geometry for this thesis?
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
Confidence >= 0.85 required for PROCEED. If rejecting all: selected_candidate_id=null, reject=true.
"""
        try:
            resp = claude.messages.create(
                model=MODEL,
                max_tokens=1000,
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
Minimum confidence 0.85 for any PROCEED. Apply all hard rules from system prompt.
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

    # Generate decision ID
    decision_id = ""
    try:
        from attribution import generate_decision_id  # noqa: PLC0415
        decision_id = generate_decision_id("A2", datetime.now(ET).strftime("%Y%m%d_%H%M%S"))
    except Exception as _did_exc:
        log.debug("[OPTS] generate_decision_id failed (non-fatal): %s", _did_exc)

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
        elif _conf < 0.85:
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
