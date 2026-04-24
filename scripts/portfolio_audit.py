"""
scripts/portfolio_audit.py — on-demand portfolio audit synthesizing A1 & A2
positions, decision captures, bracket protection, and earnings context into
per-position green/yellow/red verdicts.

CLI:
    python3 scripts/portfolio_audit.py             # run + send WhatsApp
    python3 scripts/portfolio_audit.py --quiet     # run, print only
    python3 scripts/portfolio_audit.py --json      # JSON output only

Scheduled at 9:10 AM ET (open) and 3:55 PM ET (close) via scheduler.py.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_PROJECT_ROOT / ".env")

log = logging.getLogger("portfolio_audit")
ET  = ZoneInfo("America/New_York")

_CAPTURES_DIR     = _PROJECT_ROOT / "data" / "captures"
_A2_DECISIONS_DIR = _PROJECT_ROOT / "data" / "account2" / "decisions"
_REPORTS_DIR      = _PROJECT_ROOT / "data" / "reports"
_AUDIT_LATEST     = _REPORTS_DIR / "portfolio_audit_latest.json"
_AUDIT_LOG        = _REPORTS_DIR / "portfolio_audit_log.jsonl"

_HAIKU_MODEL = "claude-haiku-4-5-20251001"


# ─────────────────────────────────────────────────────────────────────────────
# Catalyst prose sanitisation — strips hallucination vectors before Haiku sees them
# ─────────────────────────────────────────────────────────────────────────────

_EARNINGS_PROSE_PATTERNS = [
    (r"\breports?\s+(today|tonight|this\s+week)\b", "TODAY_TOKEN"),
    (r"\bearnings\s+(today|tonight|this\s+week)\b", "TODAY_TOKEN"),
    (r"\bannounc(?:es|ing)\s+(today|tonight|this\s+week)\b", "TODAY_TOKEN"),
]


def _sanitize_catalyst(
    catalyst_text: Optional[str],
    eda: Optional[int],
    sym: str,
    current_price: Optional[float],
) -> Optional[str]:
    """Strip hallucination vectors from catalyst prose before Haiku sees it.

    Two classes of fix (both motivated by observed hallucinations):
      1. Earnings timing: if eda >= 1, rewrite "reports today" / "this week"
         into "reports in {eda} days". Prevents the audit from echoing
         stale morning-brief prose.
      2. Implausible price levels: if the prose contains a dollar number
         whose ratio to current_price is > 2.0× or < 0.5×, replace with
         "[price removed]". This catches the XLE-with-WTI-spot confusion.
    """
    if not catalyst_text:
        return catalyst_text
    text = str(catalyst_text)

    # Earnings timing rewrite
    if eda is not None and eda >= 1:
        # Case-insensitive replacement across all known prose forms
        replacement = f"reports in {eda} days"
        for pattern, _tag in _EARNINGS_PROSE_PATTERNS:
            text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    # Implausible price level sanitisation
    if current_price and current_price > 0:
        def _check_price(m: re.Match) -> str:
            try:
                val = float(m.group(1).replace(",", ""))
            except Exception:
                return m.group(0)
            if val <= 0:
                return m.group(0)
            ratio = val / current_price
            if ratio > 2.0 or ratio < 0.5:
                log.debug(
                    "[AUDIT] sanitize %s: $%s stripped (ratio %.2fx vs $%.2f)",
                    sym, m.group(1), ratio, current_price,
                )
                return "[price removed]"
            return m.group(0)

        text = re.sub(r"\$(\d[\d,]*\.?\d*)", _check_price, text)
    return text


def _earnings_date_iso(sym: str) -> Optional[str]:
    """Return the ISO earnings date for sym if known, else None."""
    try:
        from earnings_calendar_lookup import load_calendar_map, _load_raw  # noqa: PLC0415
    except Exception:
        return None
    try:
        raw = _load_raw()
    except Exception:
        return None
    sym_u = sym.upper()
    today = date.today()
    candidates = []
    for entry in raw:
        if (entry.get("symbol") or "").upper() != sym_u:
            continue
        try:
            d = date.fromisoformat(str(entry.get("earnings_date", ""))[:10])
        except Exception:
            continue
        if d >= today:
            candidates.append(d)
    if not candidates:
        return None
    return min(candidates).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# Alpaca clients
# ─────────────────────────────────────────────────────────────────────────────

def _get_alpaca_a1():
    from alpaca.trading.client import TradingClient
    return TradingClient(
        api_key=os.getenv("ALPACA_API_KEY"),
        secret_key=os.getenv("ALPACA_SECRET_KEY"),
        paper=True,
    )


def _get_alpaca_a2():
    from alpaca.trading.client import TradingClient
    return TradingClient(
        api_key=os.getenv("ALPACA_API_KEY_OPTIONS"),
        secret_key=os.getenv("ALPACA_SECRET_KEY_OPTIONS"),
        paper=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Decision capture lookup
# ─────────────────────────────────────────────────────────────────────────────

def _find_latest_capture_for_symbol(symbol: str) -> Optional[dict]:
    """Return the most recent capture dict whose broker_actions include a BUY for symbol."""
    if not _CAPTURES_DIR.exists():
        return None
    files = sorted(
        _CAPTURES_DIR.glob("dec_A1_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    sym_upper = symbol.upper()
    for f in files:
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        for ba in data.get("broker_actions", []) or []:
            if (ba.get("symbol", "") or "").upper() == sym_upper and \
                    str(ba.get("action", "")).lower() in ("buy", "reallocate"):
                return data
    return None


def _find_latest_a2_decision_for_symbol(symbol: str) -> Optional[dict]:
    """Return the most recent A2 decision record mentioning symbol."""
    if not _A2_DECISIONS_DIR.exists():
        return None
    files = sorted(
        _A2_DECISIONS_DIR.glob("a2_dec_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    sym_upper = symbol.upper()
    for f in files[:50]:
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        blob = json.dumps(data)
        if sym_upper in blob.upper():
            return data
    return None


def _extract_a1_thesis(capture: Optional[dict], symbol: str) -> dict:
    """Pull reasoning/catalyst/conviction/stop/target out of a capture."""
    if not capture:
        return {}
    raw = capture.get("raw_response", "") or ""
    reasoning = ""
    try:
        obj = json.loads(raw) if raw else {}
        reasoning = obj.get("reasoning", "") or ""
    except Exception:
        reasoning = raw[:500]

    catalyst = stop_loss = take_profit = None
    conviction = None
    for ba in capture.get("broker_actions", []) or []:
        if (ba.get("symbol", "") or "").upper() != symbol.upper():
            continue
        catalyst    = ba.get("catalyst") or catalyst
        stop_loss   = ba.get("stop_loss") or stop_loss
        take_profit = ba.get("take_profit") or take_profit
        conviction  = ba.get("conviction") or ba.get("confidence") or conviction
    return {
        "reasoning":    reasoning,
        "catalyst":     catalyst,
        "stop_loss":    stop_loss,
        "take_profit":  take_profit,
        "conviction":   conviction,
        "decision_id":  capture.get("decision_id"),
    }


def _extract_a2_reasoning(decision: Optional[dict]) -> str:
    if not decision:
        return ""
    # A2DecisionRecord schema — skip debate_output_raw (raw transcript), use synthesis only
    for key in ("synthesis", "notes"):
        v = decision.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()[:800]
        if isinstance(v, dict):
            # Only use structured synthesis fields, not raw debate transcripts
            rationale = v.get("rationale") or v.get("reasons") or v.get("summary")
            if rationale and "DIRECTIONAL ADVOCATE" not in str(rationale):
                return str(rationale)[:800]
    # Fallback: selected_candidate reasoning
    sc = decision.get("selected_candidate") or {}
    if isinstance(sc, dict):
        r = sc.get("reasoning") or sc.get("rationale") or sc.get("catalyst")
        if r:
            return str(r)[:800]
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# A2 structure type inference
# ─────────────────────────────────────────────────────────────────────────────

def _infer_structure_type(structure) -> str:
    """Infer a human-readable structure type when strategy is None or unset."""
    strat = getattr(structure, "strategy", None)
    if strat is not None:
        return str(getattr(strat, "value", strat))
    legs = list(getattr(structure, "legs", []) or [])
    if len(legs) == 1:
        opt = str(getattr(legs[0], "option_type", "")).lower()
        return "single_call" if opt.startswith("c") else "single_put"
    if len(legs) == 2:
        sides = [str(getattr(l, "side", "")).lower() for l in legs]
        opts  = [str(getattr(l, "option_type", "")).lower() for l in legs]
        if sides == ["buy", "buy"]:
            return "straddle"
        if "buy" in sides and "sell" in sides:
            return f"{opts[0][0]}_spread" if opts[0] == opts[1] else "spread"
    return "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# Data collection
# ─────────────────────────────────────────────────────────────────────────────

def _collect_a1(alpaca) -> list[dict]:
    rows: list[dict] = []
    try:
        positions = alpaca.get_all_positions() or []
    except Exception as exc:
        log.warning("[AUDIT] A1 positions fetch failed: %s", exc)
        return []

    try:
        from bracket_registry import get_active_bracket, is_bracket_protected
    except Exception:
        get_active_bracket = lambda s: None  # noqa: E731
        is_bracket_protected = lambda s: False  # noqa: E731
    try:
        from earnings_calendar_lookup import earnings_days_away
    except Exception:
        earnings_days_away = lambda s: None  # noqa: E731

    for p in positions:
        sym = (getattr(p, "symbol", "") or "").upper()
        if not sym:
            continue
        try:
            qty       = float(getattr(p, "qty", 0) or 0)
            mv        = float(getattr(p, "market_value", 0) or 0)
            cost      = float(getattr(p, "cost_basis", 0) or 0)
            upl       = float(getattr(p, "unrealized_pl", 0) or 0)
            upl_pct   = float(getattr(p, "unrealized_plpc", 0) or 0) * 100.0
            avg_entry = float(getattr(p, "avg_entry_price", 0) or 0)
            cur_px    = float(getattr(p, "current_price", 0) or 0)
        except (TypeError, ValueError):
            qty = mv = cost = upl = upl_pct = avg_entry = cur_px = 0.0

        capture = _find_latest_capture_for_symbol(sym)
        thesis  = _extract_a1_thesis(capture, sym)
        bracket = get_active_bracket(sym) or {}
        protected = is_bracket_protected(sym)

        # Fetch live stop from Alpaca open orders (authoritative)
        stop_price = None
        target_price = None
        try:
            from alpaca.trading.requests import GetOrdersRequest  # noqa: PLC0415
            from alpaca.trading.enums import QueryOrderStatus     # noqa: PLC0415
            orders = alpaca.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL,
                                                         limit=50))
            for o in orders:
                if (getattr(o, "symbol", "") or "").upper() != sym:
                    continue
                ot = str(getattr(o, "order_type", "")).lower().split(".")[-1]
                os_ = str(getattr(o, "status", "")).lower().split(".")[-1]
                if os_ in ("canceled", "expired", "rejected", "replaced"):
                    continue
                if ot in ("stop", "stop_limit", "trailing_stop"):
                    sp = getattr(o, "stop_price", None)
                    if sp:
                        stop_price = float(sp)
                lp = getattr(o, "limit_price", None)
                if lp and ot == "limit":
                    lp_f = float(lp)
                    cur = float(getattr(p, "current_price", 0) or 0)
                    if lp_f > cur:  # above-market limit = take-profit
                        target_price = lp_f
        except Exception as _oe:
            log.debug("[AUDIT] live order fetch failed for %s: %s", sym, _oe)
            stop_price = bracket.get("stop_price") or thesis.get("stop_loss")
            target_price = bracket.get("target_price") or thesis.get("take_profit")

        # Fallback to bracket registry if no live stop found
        if stop_price is None:
            stop_price = bracket.get("stop_price") or thesis.get("stop_loss")
        if target_price is None:
            target_price = bracket.get("target_price") or thesis.get("take_profit")
        eda = earnings_days_away(sym)
        eda_iso = _earnings_date_iso(sym)

        # Sanitise catalyst / reasoning prose before Haiku sees it.
        # eda is authoritative; any "today"/"this week" phrase when eda >= 1 is wrong.
        raw_catalyst  = thesis.get("catalyst")
        raw_reasoning = thesis.get("reasoning")
        catalyst_clean = _sanitize_catalyst(raw_catalyst,  eda, sym, cur_px or None)
        reasoning_clean = _sanitize_catalyst(raw_reasoning, eda, sym, cur_px or None)

        rows.append({
            "account":      "A1",
            "symbol":       sym,
            "qty":          qty,
            "market_value": mv,
            "cost_basis":   cost,
            "unrealized_pl":    upl,
            "unrealized_plpc":  upl_pct,
            "avg_entry":    avg_entry,
            "current_price": cur_px,
            "bracket_protected": bool(protected),
            "stop_price":   stop_price,
            "target_price": target_price,
            "catalyst":     catalyst_clean,
            "reasoning":    reasoning_clean,
            "conviction":   thesis.get("conviction"),
            "decision_id":  thesis.get("decision_id"),
            "capture_found": capture is not None,
            "earnings_days_away": eda,
            "earnings_date_iso":  eda_iso,
        })
    return rows


def _collect_a2() -> list[dict]:
    rows: list[dict] = []
    try:
        from options_state import get_open_structures
    except Exception as exc:
        log.warning("[AUDIT] options_state import failed: %s", exc)
        return []
    try:
        structures = get_open_structures() or []
    except Exception as exc:
        log.warning("[AUDIT] A2 structures fetch failed: %s", exc)
        return []

    try:
        from earnings_calendar_lookup import earnings_days_away
    except Exception:
        earnings_days_away = lambda s: None  # noqa: E731

    # Live A2 P&L per underlying — sum unrealized_pl across every live OCC leg.
    a2_live_by_underlying: dict[str, float] = {}
    try:
        _a2c = _get_alpaca_a2()
        for _lp in (_a2c.get_all_positions() or []):
            _occ = (getattr(_lp, "symbol", "") or "").upper()
            _upl = float(getattr(_lp, "unrealized_pl", 0) or 0)
            _m = re.match(r"^([A-Z]+)", _occ)
            _k = _m.group(1) if _m else _occ
            a2_live_by_underlying[_k] = a2_live_by_underlying.get(_k, 0.0) + _upl
    except Exception as _a2e:
        log.debug("[AUDIT] A2 live P&L fetch failed (non-fatal): %s", _a2e)

    for s in structures:
        underlying = (getattr(s, "underlying", "") or "").upper()
        if not underlying:
            continue
        stype = _infer_structure_type(s)
        legs  = list(getattr(s, "legs", []) or [])
        leg_summary = []
        for leg in legs:
            leg_summary.append({
                "side":        getattr(leg, "side", "?"),
                "option_type": getattr(leg, "option_type", "?"),
                "strike":      getattr(leg, "strike", None),
                "occ":         getattr(leg, "occ_symbol", None),
                "filled_price": getattr(leg, "filled_price", None),
            })
        decision = _find_latest_a2_decision_for_symbol(underlying)
        reasoning = _extract_a2_reasoning(decision)
        eda = earnings_days_away(underlying)

        # Fix 4: parse expiry from the first leg's OCC when structure.expiration is empty.
        expiration = getattr(s, "expiration", "") or ""
        if not expiration and leg_summary:
            first_occ = (leg_summary[0].get("occ") or "")
            m_exp = re.search(r"(\d{6})[CP]", str(first_occ).upper())
            if m_exp:
                try:
                    yy, mm, dd = m_exp.group(1)[:2], m_exp.group(1)[2:4], m_exp.group(1)[4:6]
                    expiration = f"20{yy}-{mm}-{dd}"
                except Exception:
                    expiration = ""

        # Fix 2: live P&L and percent-of-max-cost for this underlying.
        max_cost = getattr(s, "max_cost_usd", 0) or 0
        upl = a2_live_by_underlying.get(underlying)
        upl_pct = (
            round(upl / max_cost * 100, 1)
            if upl is not None and max_cost
            else None
        )

        # Sanitise A2 catalyst + reasoning (same hallucination vectors as A1).
        a2_catalyst_raw  = getattr(s, "catalyst", "") or ""
        a2_reasoning_raw = reasoning or ""
        a2_catalyst_clean  = _sanitize_catalyst(a2_catalyst_raw,  eda, underlying, None)
        a2_reasoning_clean = _sanitize_catalyst(a2_reasoning_raw, eda, underlying, None)
        eda_iso = _earnings_date_iso(underlying)

        rows.append({
            "account":      "A2",
            "symbol":       underlying,
            "structure_id": getattr(s, "structure_id", ""),
            "structure_type": stype,
            "direction":    getattr(s, "direction", ""),
            "contracts":    getattr(s, "contracts", 0),
            "expiration":   expiration,
            "legs":         leg_summary,
            "iv_rank":      getattr(s, "iv_rank", None),
            "max_cost_usd": max_cost,
            "catalyst":     a2_catalyst_clean,
            "reasoning":    a2_reasoning_clean,
            "lifecycle":    getattr(getattr(s, "lifecycle", None), "value",
                                    str(getattr(s, "lifecycle", ""))),
            "earnings_days_away": eda,
            "earnings_date_iso":  eda_iso,
            "unrealized_pl":     upl,
            "unrealized_plpc":   upl_pct,
        })
    return rows


def _fetch_equity(client) -> float:
    try:
        acct = client.get_account()
        return float(getattr(acct, "equity", 0) or 0)
    except Exception:
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Haiku synthesis
# ─────────────────────────────────────────────────────────────────────────────

_AUDIT_SYSTEM = (
    "You are a portfolio auditor for an AI trading bot. Assess each position "
    "against its original thesis and current market state. Be concise and "
    "specific. Output valid JSON only."
)

_AUDIT_SCHEMA_HINT = """
Output JSON of the shape:
{
  "positions": [
    {
      "symbol": str,
      "account": "A1" | "A2",
      "verdict": "green" | "yellow" | "red",
      "structure": str,
      "thesis_intact": bool,
      "concerns": [str],
      "narrative": str,
      "stop_status": str,
      "earnings": str,
      "pnl_str": str
    }
  ]
}
Rules:
- green: thesis intact, protective orders correct, no imminent concern
- yellow: earnings ≤7 days, stop wider than ideal, thesis weakening, IV elevated
- red: stop missing, partial/naked spread, P&L breaching max loss, catalyst invalidated
- Be specific — cite concrete dollar levels, dates, or ratios in concerns.
- Two-to-three-sentence narrative per position.
- CRITICAL — EARNINGS TIMING AUTHORITY:
  The `earnings_days_away` field is the ONLY authoritative source for earnings timing.
  The `earnings_date_iso` field is the ONLY authoritative source for the exact date.
  If earnings_days_away >= 1, any phrase containing "today", "tonight", "reports today",
  or "this week" in any catalyst / reasoning / narrative field is INCORRECT. Rewrite
  every earnings timing mention as "reports in {earnings_days_away} days ({earnings_date_iso})".
  Do NOT echo or quote catalyst prose for earnings dates — use ONLY the structured fields.
- CRITICAL — PRICE LEVELS:
  If the catalyst / reasoning text contains a dollar number with value "[price removed]",
  the sanitizer already stripped an implausible price (commodity-vs-equity confusion).
  Do NOT try to reconstruct the removed price. Cite `current_price`, `avg_entry`, or
  `stop_price` when you need a price reference.
- A1: if stop_price is non-null the position IS protected. Never say "not bracket-protected"
  as a concern if stop_price exists.
- A2: use unrealized_pl/unrealized_plpc for pnl_str when present. Only write "P&L unknown"
  if both are null.
"""


def _synthesize_with_haiku(rows_a1: list[dict], rows_a2: list[dict]) -> list[dict]:
    """One Haiku call that returns per-position verdicts. Non-fatal: returns []
    on failure so the caller can ship data without verdicts."""
    try:
        import anthropic
    except Exception as exc:
        log.warning("[AUDIT] anthropic SDK not importable: %s", exc)
        return []

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        log.warning("[AUDIT] ANTHROPIC_API_KEY missing — skipping Haiku synthesis")
        return []

    payload = json.dumps({
        "a1_positions": rows_a1,
        "a2_structures": rows_a2,
    }, default=str)

    today_str = datetime.now(ET).strftime("%Y-%m-%d")
    user_prompt = (
        f"Today's date is {today_str}.\n\n"
        f"Collected portfolio state (JSON):\n{payload}\n\n{_AUDIT_SCHEMA_HINT}"
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=_HAIKU_MODEL,
            max_tokens=4000,
            system=_AUDIT_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = "".join(
            (block.text if hasattr(block, "text") else str(block))
            for block in getattr(resp, "content", [])
        )

        # Cost spine logging (non-fatal)
        try:
            from cost_attribution import log_spine_record  # noqa: PLC0415
            usage = getattr(resp, "usage", None)
            log_spine_record({
                "ts":             datetime.now(timezone.utc).isoformat(),
                "module_name":    "portfolio_audit",
                "layer_name":     "portfolio_audit",
                "model":          _HAIKU_MODEL,
                "input_tokens":   int(getattr(usage, "input_tokens",  0) or 0),
                "output_tokens":  int(getattr(usage, "output_tokens", 0) or 0),
                "ring":           "prod",
            })
        except Exception as _cs_exc:
            log.debug("[AUDIT] cost spine log failed (non-fatal): %s", _cs_exc)

        # Strip markdown fences
        cleaned = re.sub(r"```(?:json)?\s*", "", raw).replace("```", "").strip()
        # Try direct parse first
        obj = None
        for candidate in [cleaned, raw]:
            try:
                obj = json.loads(candidate)
                break
            except json.JSONDecodeError:
                pass
        # Fallback: extract outermost {...} and retry
        if obj is None:
            m = re.search(r"\{[\s\S]*\}", candidate)
            if m:
                try:
                    obj = json.loads(m.group(0))
                except json.JSONDecodeError:
                    # Last resort: fix trailing commas and retry
                    fixed = re.sub(r",\s*([}\]])", r"\1", m.group(0))
                    try:
                        obj = json.loads(fixed)
                    except json.JSONDecodeError:
                        pass
        if obj is None:
            log.warning("[AUDIT] Haiku returned unparseable JSON")
            return []
        return list(obj.get("positions", []))
    except Exception as exc:
        log.warning("[AUDIT] Haiku synthesis failed (non-fatal): %s", exc)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Formatting
# ─────────────────────────────────────────────────────────────────────────────

_VERDICT_EMOJI = {"green": "🟢", "yellow": "🟡", "red": "🔴"}


def _format_audit_message(
    equity_a1: float,
    equity_a2: float,
    positions: list[dict],
    time_et: datetime,
) -> str:
    lines: list[str] = []
    time_str = time_et.strftime("%Y-%m-%d %H:%M %Z")
    lines.append(f"📋 PORTFOLIO AUDIT — {time_str}")
    lines.append(f"A1: ${equity_a1:,.0f} | A2: ${equity_a2:,.0f}")
    lines.append("")

    if not positions:
        lines.append("(no positions to audit)")
    for p in positions:
        verdict = (p.get("verdict") or "yellow").lower()
        emoji   = _VERDICT_EMOJI.get(verdict, "⚪")
        lines.append(
            f"{emoji} {p.get('symbol', '?')} [{p.get('account', '?')}] — "
            f"{p.get('structure', '?')}"
        )
        lines.append(f"{p.get('pnl_str', 'n/a')} | {p.get('stop_status', '?')}")
        if p.get("narrative"):
            lines.append(str(p["narrative"]))
        for c in p.get("concerns") or []:
            lines.append(f"  • {c}")
        if p.get("earnings"):
            lines.append(f"Earnings: {p['earnings']}")
        lines.append("")

    n_green  = sum(1 for p in positions if (p.get("verdict") or "").lower() == "green")
    n_yellow = sum(1 for p in positions if (p.get("verdict") or "").lower() == "yellow")
    n_red    = sum(1 for p in positions if (p.get("verdict") or "").lower() == "red")
    lines.append(f"Overall: {n_green} 🟢 {n_yellow} 🟡 {n_red} 🔴")
    lines.append("Run `python3 scripts/portfolio_audit.py` for on-demand")
    return "\n".join(lines)


def _fallback_positions_from_rows(rows_a1: list[dict], rows_a2: list[dict]) -> list[dict]:
    """Build a minimal 'positions' list without Haiku verdicts — used if Haiku fails."""
    out: list[dict] = []
    for r in rows_a1:
        sym = r["symbol"]
        pnl = r.get("unrealized_pl", 0.0)
        pct = r.get("unrealized_plpc", 0.0)
        stop = r.get("stop_price")
        stop_str = f"stop at ${stop:.2f}" + (" (bracket)" if r.get("bracket_protected") else "") \
            if stop else "no stop"
        eda = r.get("earnings_days_away")
        earnings = f"in {eda} days" if eda is not None else "none in 30 days"
        out.append({
            "symbol":     sym,
            "account":    "A1",
            "verdict":    "yellow",
            "structure":  f"{r['qty']:.0f} shares long" if r.get("qty", 0) > 0 else f"{r['qty']:.0f} shares",
            "thesis_intact": r.get("capture_found", False),
            "concerns":   [] if stop else ["no stop order visible (no bracket, no SL)"],
            "narrative":  r.get("reasoning") or r.get("catalyst") or
                          "No decision capture found — position may have been entered "
                          "before audit system was active",
            "stop_status": stop_str,
            "earnings":   earnings,
            "pnl_str":    f"{'+' if pnl >= 0 else ''}${pnl:,.0f} ({pct:+.1f}%)",
        })
    for r in rows_a2:
        eda = r.get("earnings_days_away")
        earnings = f"in {eda} days" if eda is not None else "none in 30 days"
        out.append({
            "symbol":     r["symbol"],
            "account":    "A2",
            "verdict":    "yellow",
            "structure":  str(r.get("structure_type") or "unknown").replace("_", " "),
            "thesis_intact": bool(r.get("reasoning")),
            "concerns":   [],
            "narrative":  r.get("reasoning")
                          or f"{r.get('contracts', '?')} contracts, "
                             f"expiring {r.get('expiration', '?')}",
            "stop_status": f"lifecycle={r.get('lifecycle', '?')}",
            "earnings":   earnings,
            "pnl_str":    f"max_cost=${r.get('max_cost_usd', 0):,.0f}",
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def run_portfolio_audit(send_whatsapp: bool = True) -> dict:
    """
    Execute the audit end-to-end. Returns a structured result dict.

    Non-fatal: Haiku-call failures fall back to data-only formatting;
    WhatsApp failures log warnings but don't raise.
    """
    now_et = datetime.now(ET)
    log.info("[AUDIT] Portfolio audit start — %s ET", now_et.isoformat())

    try:
        a1_client = _get_alpaca_a1()
    except Exception as exc:
        log.warning("[AUDIT] A1 client init failed: %s", exc)
        a1_client = None
    try:
        a2_client = _get_alpaca_a2()
    except Exception as exc:
        log.warning("[AUDIT] A2 client init failed: %s", exc)
        a2_client = None

    rows_a1 = _collect_a1(a1_client) if a1_client else []
    rows_a2 = _collect_a2() if a2_client else []

    equity_a1 = _fetch_equity(a1_client) if a1_client else 0.0
    equity_a2 = _fetch_equity(a2_client) if a2_client else 0.0

    positions = _synthesize_with_haiku(rows_a1, rows_a2)
    if not positions:
        positions = _fallback_positions_from_rows(rows_a1, rows_a2)

    message = _format_audit_message(equity_a1, equity_a2, positions, now_et)

    result = {
        "timestamp":  now_et.isoformat(),
        "equity_a1":  equity_a1,
        "equity_a2":  equity_a2,
        "positions":  positions,
        "n_green":    sum(1 for p in positions if (p.get("verdict") or "").lower() == "green"),
        "n_yellow":   sum(1 for p in positions if (p.get("verdict") or "").lower() == "yellow"),
        "n_red":      sum(1 for p in positions if (p.get("verdict") or "").lower() == "red"),
        "message":    message,
    }

    # Persist
    try:
        _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        _AUDIT_LATEST.write_text(json.dumps(result, indent=2, default=str))
        with _AUDIT_LOG.open("a") as fh:
            fh.write(json.dumps({
                "ts":         now_et.isoformat(),
                "n_green":    result["n_green"],
                "n_yellow":   result["n_yellow"],
                "n_red":      result["n_red"],
                "equity_a1":  equity_a1,
                "equity_a2":  equity_a2,
            }) + "\n")
    except Exception as exc:
        log.warning("[AUDIT] persistence failed (non-fatal): %s", exc)

    if send_whatsapp:
        try:
            from trade_publisher import TradePublisher  # noqa: PLC0415
            pub = TradePublisher()
            if hasattr(pub, "send_alert_long"):
                pub.send_alert_long(message)
            else:
                pub.send_alert(message)
        except Exception as exc:
            log.warning("[AUDIT] WhatsApp send failed (non-fatal): %s", exc)

    log.info(
        "[AUDIT] complete — green=%d yellow=%d red=%d positions=%d",
        result["n_green"], result["n_yellow"], result["n_red"], len(positions),
    )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="On-demand portfolio audit")
    parser.add_argument("--quiet", action="store_true",
                        help="Do not send WhatsApp; print to stdout only")
    parser.add_argument("--json",  action="store_true",
                        help="Output raw JSON only (implies --quiet)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    send = not (args.quiet or args.json)
    result = run_portfolio_audit(send_whatsapp=send)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(result["message"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
