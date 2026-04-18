"""
bot_stage1_regime.py — Stage 1: Haiku market regime classifier.

Public API:
  classify_regime(md, calendar)  -> dict
  format_regime_summary(regime)  -> str
"""

import json

from bot_clients import _get_claude, MODEL_FAST
from log_setup import get_logger, log_trade

log = get_logger(__name__)

_REGIME_SYS = (
    "You are a market regime classifier for a trading bot. "
    "Output a structured JSON assessment only. No markdown, just valid JSON.\n"
    "Output: {\n"
    '  "regime_score": <0-100>,\n'
    '  "bias": "risk-on"|"risk-off"|"neutral",\n'
    '  "session_theme": "<one descriptive phrase>",\n'
    '  "constraints": [<strings>],\n'
    '  "high_impact_warning": null|"<event and minutes away>",\n'
    '  "orb_context": "<one line on opening range>",\n'
    '  "confidence": "high"|"medium"|"low",\n'
    '  "macro_regime": "reflationary"|"disinflationary"|"stagflationary"|"goldilocks"|"risk-off",\n'
    '  "commodity_trend": "bullish"|"bearish"|"neutral",\n'
    '  "dollar_trend": "strong"|"weak"|"neutral",\n'
    '  "credit_stress": "tight"|"normal"|"wide"\n'
    "}"
)


def _normalize_regime_labels(result: dict) -> dict:
    """T-005: Claude sometimes emits 'risk-on'/'risk-off' with hyphens; normalize to underscores."""
    for _f in ("bias", "macro_regime"):
        if isinstance(result.get(_f), str):
            result[_f] = result[_f].replace("-", "_")
    return result


def classify_regime(md: dict, calendar: dict) -> dict:
    """
    Stage 1: Haiku call classifying market regime from macro data.
    System prompt cached. Fails to safe defaults on any error.
    """
    _default = {
        "regime_score": 50, "bias": "neutral",
        "session_theme": "regime classification unavailable",
        "constraints": [], "high_impact_warning": None,
        "orb_context": "", "confidence": "low",
        "macro_regime": "unknown", "commodity_trend": "neutral",
        "dollar_trend": "neutral", "credit_stress": "normal",
    }
    try:
        vix     = md.get("vix", 0)
        vreg    = md.get("vix_regime", "")
        glob    = "\n".join((md.get("global_handoff", "") or "").splitlines()[:3])
        cal_evs = calendar.get("events", [])
        cal_str = "\n".join(
            f"  {e.get('datetime_et','?')[:16]}  [{e.get('impact','?').upper()[:3]}]  {e.get('event','?')}"
            for e in sorted(cal_evs, key=lambda x: abs(x.get("minutes_from_now", 9999)))[:3]
        ) or "  (none)"
        try:
            from macro_wire import build_macro_wire_section  # noqa: PLC0415
            macro_str = "\n".join(build_macro_wire_section().splitlines()[:6])
        except Exception:
            macro_str = "  (unavailable)"
        sec_lines = [l for l in (md.get("sector_table", "") or "").splitlines() if "▲" in l or "▼" in l]
        sec_str   = "\n".join(sec_lines[:3] + sec_lines[-3:]) if sec_lines else ""
        try:
            import scheduler as _sched
            if _sched._orb_locked and _sched._orb_high:
                _orb_parts = [
                    f"{s} H=${_sched._orb_high[s]:.2f}/L=${_sched._orb_low.get(s, 0):.2f}"
                    for s in list(_sched._orb_high)[:6]
                ]
                orb_str = "ORB locked: " + "  ".join(_orb_parts)
            elif not _sched._orb_locked and md.get("minutes_since_open", -1) >= 0:
                orb_str = "ORB formation in progress (9:30-9:45 AM window)"
            else:
                orb_str = "Not in ORB window"
        except Exception:
            orb_str = "(unavailable)"

        macro_inputs_str = "  (unavailable)"
        try:
            import macro_intelligence as _mi  # noqa: PLC0415
            _mi_data = _mi.get_regime_macro_inputs()
            if _mi_data:
                macro_inputs_str = (
                    f"  Rates: {_mi_data.get('rates_summary','?')}\n"
                    f"  Commodities: {_mi_data.get('commodity_trend','?')} "
                    f"(gold 5d: {_mi_data.get('gold_5d_pct',0):+.1f}%)\n"
                    f"  Credit spreads: {_mi_data.get('credit_stress','?')}\n"
                    f"  Dollar: {_mi_data.get('dollar_trend','?')}"
                )
        except Exception:
            pass

        user_content = (
            f"VIX: {vix}  Regime: {vreg}\n"
            f"Time: {md.get('time_et','?')}  Status: {md.get('market_status','?')}\n"
            f"ORB: {orb_str}\n\n"
            f"GLOBAL SESSION:\n{glob}\n\n"
            f"ECONOMIC CALENDAR (next 3):\n{cal_str}\n\n"
            f"MACRO WIRE (top headlines):\n{macro_str}\n\n"
            f"MACRO BACKDROP (rates/commodities/credit):\n{macro_inputs_str}\n\n"
            f"SECTOR ROTATION (top+bottom 3):\n{sec_str}"
        )
        resp = _get_claude().messages.create(
            model=MODEL_FAST, max_tokens=300,
            system=[{"type": "text", "text": _REGIME_SYS, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_content}],
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
        try:
            from cost_tracker import get_tracker
            get_tracker().record_api_call(MODEL_FAST, resp.usage, caller="regime_classifier")
        except Exception as _ct_exc:
            log.warning("Cost tracker failed: %s", _ct_exc)
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        result = _normalize_regime_labels(json.loads(raw))
        cr = getattr(resp.usage, "cache_read_input_tokens", 0) or 0
        log.debug("[REGIME] score=%d bias=%s confidence=%s cache_read=%d",
                  result.get("regime_score", 50), result.get("bias"), result.get("confidence"), cr)
        log_trade({"event": "regime_classification", "regime_score": result.get("regime_score"),
                   "bias": result.get("bias"), "session_theme": result.get("session_theme"),
                   "confidence": result.get("confidence"), "constraints": result.get("constraints", [])})
        return result
    except Exception as exc:
        log.warning("[REGIME] Classifier failed (non-fatal): %s", exc)
        return _default


def format_regime_summary(regime: dict) -> str:
    if not regime or (regime.get("confidence") == "low" and not regime.get("session_theme")):
        return "  (regime classification unavailable this cycle)"
    lines = [
        f"  Score: {regime.get('regime_score', 50)}/100  Bias: {regime.get('bias', 'neutral')}  "
        f"Confidence: {regime.get('confidence', 'low')}",
        f"  Theme: {regime.get('session_theme', '')}",
    ]
    if regime.get("high_impact_warning"):
        lines.append(f"  WARNING: {regime['high_impact_warning']}")
    if regime.get("orb_context"):
        lines.append(f"  ORB: {regime['orb_context']}")
    for c in regime.get("constraints", []):
        lines.append(f"  Constraint: {c}")
    return "\n".join(lines)
