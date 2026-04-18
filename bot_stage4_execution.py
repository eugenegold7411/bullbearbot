"""
bot_stage4_execution.py — Stage 4: pre-execution filters.

Public API:
  debate_trade(action, md, equity, session_tier)  -> dict
  fundamental_check(buy_candidates, md)           -> dict
"""

import json
from pathlib import Path

from bot_clients import _get_claude, MODEL, MODEL_FAST
from log_setup import get_logger, log_trade

log = get_logger(__name__)


def debate_trade(
    action:       dict,
    md:           dict,
    equity:       float,
    session_tier: str,
) -> dict:
    """
    Run a 3-call bull/bear/synthesis debate for a proposed buy action.

    Gate conditions (all must be true to run):
      - action == "buy"
      - confidence in ["medium", "high"]
      - session_tier == "market"
      - equity > $26,000

    Returns {proceed: bool, veto_reason: str, synthesis: str,
             conviction_adjustment: str}.
    Fails open (proceed=True) on any error — never blocks a trade due to a bug.
    """
    sym        = action.get("symbol", "?")
    catalyst   = action.get("catalyst", "")
    confidence = action.get("confidence", "low")
    direction  = action.get("action", "")

    if direction != "buy":
        return {"proceed": True}
    if confidence not in ("medium", "high"):
        return {"proceed": True}
    if session_tier != "market":
        return {"proceed": True}
    if equity <= 26_000:  # PDT_FLOOR duplicated from risk_kernel — see docs/policy_leakage_findings.md
        return {"proceed": True}

    log.info("[DEBATE] Running bull/bear debate for %s %s (conf=%s)", direction, sym, confidence)

    context = (
        f"Symbol: {sym}\n"
        f"Proposed action: {direction.upper()}\n"
        f"Catalyst: {catalyst}\n"
        f"VIX: {md.get('vix', '?')}  Regime: {md.get('vix_regime', '?')}\n"
        f"Market status: {md.get('market_status', '?')}\n"
        f"Breaking news: {md.get('breaking_news', '')[:300]}\n"
        f"Inter-market signals: {md.get('intermarket_signals', '')[:200]}\n"
    )

    try:
        bull_resp = _get_claude().messages.create(
            model=MODEL, max_tokens=400,
            system="You are a bullish equity trader. Make the strongest possible case FOR this trade. Be specific and data-driven. Return 3-5 bullet points.",
            messages=[{"role": "user", "content": f"Make the bull case for this trade:\n\n{context}"}],
        )
        bull_case = bull_resp.content[0].text.strip()

        bear_resp = _get_claude().messages.create(
            model=MODEL, max_tokens=400,
            system="You are a risk manager and skeptical trader. Make the strongest possible case AGAINST this trade. Focus on downside risks. Return 3-5 bullet points.",
            messages=[{"role": "user", "content": f"Make the bear case against this trade:\n\n{context}"}],
        )
        bear_case = bear_resp.content[0].text.strip()

        synth_prompt = (
            f"You are a senior portfolio manager reviewing this trade debate.\n\n"
            f"PROPOSED TRADE: {direction.upper()} {sym}\nCATALYST: {catalyst}\n\n"
            f"BULL CASE:\n{bull_case}\n\nBEAR CASE:\n{bear_case}\n\n"
            f"Return ONLY valid JSON:\n"
            f'{{\"proceed\": true or false, \"veto_reason\": \"reason if vetoing or empty string\", '
            f'\"synthesis\": \"1-2 sentence final verdict\", '
            f'\"conviction_adjustment\": \"raise\" or \"maintain\" or \"lower\"}}'
        )
        synth_resp = _get_claude().messages.create(
            model=MODEL, max_tokens=300,
            system="You are a senior portfolio manager. Return only valid JSON.",
            messages=[{"role": "user", "content": synth_prompt}],
        )
        raw = synth_resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        result = json.loads(raw)

        proceed = bool(result.get("proceed", True))
        veto    = result.get("veto_reason", "")
        synth   = result.get("synthesis", "")
        adj     = result.get("conviction_adjustment", "maintain")

        log_trade({
            "event":                 "debate",
            "symbol":                sym,
            "proceed":               proceed,
            "veto_reason":           veto,
            "synthesis":             synth,
            "conviction_adjustment": adj,
            "bull_case":             bull_case[:300],
            "bear_case":             bear_case[:300],
        })

        if not proceed:
            log.info("[DEBATE] VETOED %s — %s", sym, veto)
        else:
            log.info("[DEBATE] APPROVED %s — %s (adj=%s)", sym, synth[:80], adj)

        return {"proceed": proceed, "veto_reason": veto, "synthesis": synth,
                "conviction_adjustment": adj}

    except Exception as exc:
        log.warning("[DEBATE] Debate failed for %s: %s — failing open", sym, exc)
        return {"proceed": True}


def fundamental_check(buy_candidates: list[dict], md: dict) -> dict:
    """
    Single Claude call to evaluate fundamentals for all buy-candidate symbols.

    Reads cached fundamentals from data/fundamentals/{SYM}.json.
    Returns {symbol: {ok: bool, notes: str}} for each candidate.
    Fails open ({}) on any error — never blocks a trade due to a bug.
    Only runs for stock/ETF symbols (skips crypto / symbols with '/').
    """
    stock_buys = [
        a for a in buy_candidates
        if a.get("symbol") and "/" not in a.get("symbol", "")
    ]
    if not stock_buys:
        return {}

    fund_dir   = Path(__file__).parent / "data" / "fundamentals"
    fund_lines: list[str] = []

    for a in stock_buys:
        sym       = a.get("symbol", "")
        fund_path = fund_dir / f"{sym}.json"
        try:
            if fund_path.exists():
                f      = json.loads(fund_path.read_text())
                pe     = f.get("pe_ratio", "N/A")
                mktcap = f.get("market_cap_b", "N/A")
                hi52   = f.get("52w_high", "N/A")
                lo52   = f.get("52w_low", "N/A")
                fund_lines.append(
                    f"{sym}: P/E={pe}  mktcap=${mktcap}B  "
                    f"52w_high={hi52}  52w_low={lo52}"
                )
            else:
                fund_lines.append(f"{sym}: (no fundamentals cached)")
        except Exception:
            fund_lines.append(f"{sym}: (fundamentals unavailable)")

    if not fund_lines:
        return {}

    prompt = (
        "Review the fundamentals for these potential buy candidates. "
        "Flag any with concerning fundamentals (extreme P/E, near 52w high with no catalyst, etc). "
        "Return ONLY valid JSON: {\"TICKER\": {\"ok\": true or false, \"notes\": \"brief note\"}, ...}\n\n"
        + "\n".join(fund_lines)
    )

    try:
        resp = _get_claude().messages.create(
            model=MODEL_FAST, max_tokens=600,
            system=[{
                "type": "text",
                "text": "You are a fundamental equity analyst. Return only valid JSON.",
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": prompt}],
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
        try:
            from cost_tracker import get_tracker
            get_tracker().record_api_call(MODEL_FAST, resp.usage, caller="fundamental_check")
        except Exception as _ct_exc:
            log.warning("Cost tracker failed: %s", _ct_exc)
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        result = json.loads(raw)
        log.info("[FUNDAMENTAL] Evaluated %d buy candidates", len(result))
        return result
    except Exception as exc:
        log.warning("[FUNDAMENTAL] Check failed: %s — failing open", exc)
        return {}
