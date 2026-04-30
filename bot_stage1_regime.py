"""
bot_stage1_regime.py — Stage 1: Haiku market regime classifier.

Public API:
  classify_regime(md, calendar)  -> dict
  format_regime_summary(regime)  -> str
"""

import json
import re

from bot_clients import MODEL_FAST, _get_claude
from log_setup import get_logger, log_trade

log = get_logger(__name__)

_REGIME_SYS = (
    "You are a market regime classifier for a trading bot. "
    "Output a structured JSON assessment only. No markdown, just valid JSON.\n"
    "Output: {\n"
    '  "regime_score": <0-100>,\n'
    '  "bias": "risk_on"|"risk_off"|"neutral",\n'
    '  "session_theme": "<one descriptive phrase>",\n'
    '  "constraints": [<strings>],\n'
    '  "high_impact_warning": null|"<event and minutes away>",\n'
    '  "orb_context": "<one line on opening range>",\n'
    '  "confidence": "high"|"medium"|"low",\n'
    '  "macro_regime": "reflationary"|"disinflationary"|"stagflationary"|"goldilocks"|"risk_off",\n'
    '  "commodity_trend": "bullish"|"bearish"|"neutral",\n'
    '  "dollar_trend": "strong"|"weak"|"neutral",\n'
    '  "credit_stress": "tight"|"normal"|"wide"\n'
    "}\n\n"
    "REGIME SCORE GUIDE:\n"
    "  0-15  Crisis/crash: VIX > 40, credit blowout, major index breakdown. "
    "Constraints: halt_all_new_positions. Bias: risk_off.\n"
    "  16-30 Risk-off/defensive: VIX 28-40, broad selling, sector rotation to "
    "utilities and gold. Constraints: reduce_position_size. Bias: risk_off.\n"
    "  31-45 Cautious: VIX 22-28, mixed signals, macro uncertainty, no clear trend. "
    "Constraints: high_conviction_only. Bias: neutral or risk_off.\n"
    "  46-60 Neutral: VIX 16-22, range-bound, balanced flows. No special constraints. "
    "Bias: neutral.\n"
    "  61-75 Risk-on: VIX 12-16, broad market rally, momentum favoring risk assets. "
    "Bias: risk_on.\n"
    "  76-90 Trending: VIX < 12, sustained rally, high breadth, low volatility. "
    "Bias: risk_on.\n"
    "  91-100 Euphoria: VIX < 10, parabolic moves, extreme crowding. "
    "Constraints: protect_profits. Bias: risk_on but watch for reversal.\n\n"
    "MACRO REGIME CLASSIFICATION:\n"
    "  reflationary: Rising inflation + rising growth. Favors commodities, energy, financials.\n"
    "  disinflationary: Falling inflation + rising growth (Goldilocks). Favors growth tech.\n"
    "  stagflationary: Rising inflation + falling growth (worst case). Favors cash, gold.\n"
    "  goldilocks: Low stable inflation + solid growth. Favors equities broadly.\n"
    "  risk_off: Falling or negative growth dominates. Favors treasuries, utilities, gold.\n\n"
    "CREDIT STRESS:\n"
    "  tight: HY spreads < 300bps, IG < 80bps. Credit market healthy.\n"
    "  normal: HY 300-500bps, IG 80-130bps. Standard credit risk premium.\n"
    "  wide: HY > 500bps or IG > 130bps. Stress. Add 'credit_stress_elevated' to constraints.\n\n"
    "COMMON CONSTRAINT VALUES (use exact strings):\n"
    "  halt_all_new_positions, reduce_position_size, high_conviction_only, no_leverage,\n"
    "  avoid_earnings_binary, avoid_options_new, crypto_only, defensive_only,\n"
    "  credit_stress_elevated, orb_formation_active, fed_day_caution,\n"
    "  major_data_pending, vix_elevated, protect_profits.\n\n"
    "BIAS FIELD RULES:\n"
    "  Use underscores: 'risk_on', 'risk_off', 'neutral'. Never hyphens.\n"
    "  macro_regime also uses underscores: 'risk_off' not 'risk-off'.\n\n"
    "CONFIDENCE CALIBRATION:\n"
    "  high: VIX confirms bias, macro data aligns, multiple signals agree.\n"
    "  medium: Signals mixed or macro data is stale (>2h).\n"
    "  low: Contradictory signals, missing data, or regime is transitioning.\n\n"
    "SESSION THEME EXAMPLES (one descriptive phrase):\n"
    "  'pre-market rally', 'post-data drift', 'fed-watch defensive', 'tech-led risk-on',\n"
    "  'defensive rotation', 'commodities surge', 'credit risk elevated',\n"
    "  'ORB formation window', 'overnight crypto drift', 'earnings-driven volatility'.\n\n"
    "HIGH IMPACT WARNING:\n"
    "  null when no high-impact event within 60 minutes.\n"
    "  '<event name> in <N> min' when a HIGH-impact calendar event is ≤60 min away.\n"
    "  Only flag events with impact='HIGH' from the economic calendar.\n\n"
    "DOLLAR TREND:\n"
    "  strong: DXY up >0.3% on the day or clear 5-day uptrend.\n"
    "  weak: DXY down >0.3% on the day or clear 5-day downtrend.\n"
    "  neutral: DXY ±0.3% or mixed signals.\n\n"
    "COMMODITY TREND:\n"
    "  bullish: Oil (WTI) up >1% OR gold up >0.5% with volume, commodities complex leading.\n"
    "  bearish: Oil down >1% OR gold down >0.5%, commodities complex lagging.\n"
    "  neutral: Mixed commodity moves or flat on the day.\n\n"
    "ORB CONTEXT RULES:\n"
    "  If market is 9:30-9:45 ET: report 'ORB formation in progress — N symbols tracked'.\n"
    "  If ORB is locked (post-9:45): report 'ORB locked — top candidates: SYM1, SYM2'.\n"
    "  If outside market hours or no ORB data: report 'Not in ORB window'.\n\n"
    "MULTI-FACTOR SCORING GUIDE:\n"
    "  Start from VIX as base: VIX 10=90, VIX 15=75, VIX 20=60, VIX 25=45, VIX 30=35, "
    "VIX 40=20.\n"
    "  Adjust +5 for: strong global session, positive macro data, bullish Fed tone.\n"
    "  Adjust -5 for: negative global session, bearish macro print, credit stress, "
    "geopolitical shock.\n"
    "  Final score should reflect the combined weight of ALL inputs, not just VIX.\n"
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
            model=MODEL_FAST, max_tokens=600,
            system=[{"type": "text", "text": _REGIME_SYS,
                     "cache_control": {"type": "ephemeral"}}],
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
        # Extract outermost JSON object in case of preamble/postamble
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            raw = m.group(0)
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
