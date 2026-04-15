"""
Trading bot — main entry point.
Run directly for a single cycle:  python bot.py
Run via scheduler for 24/7 mode:  python scheduler.py
"""

import json
import os
import time
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest

import data_warehouse as dw
import market_data
import memory as mem
import order_executor
import portfolio_intelligence as pi
import reconciliation as recon
import sonnet_gate as _gate
import risk_kernel
import trade_memory
import scratchpad as _scratchpad
import watchlist_manager as wm
from log_setup import get_logger, log_trade
from schemas import normalize_symbol, validate_claude_decision
from schemas import BrokerAction as _BrokerAction, BrokerSnapshot as _BrokerSnapshot
from schemas import NormalizedPosition as _NP

# trade_publisher is optional — import failure must never break the bot
try:
    from trade_publisher import TradePublisher
    publisher = TradePublisher()
except Exception as _tp_exc:
    log_import_err = None
    publisher = None  # type: ignore

load_dotenv()

log = get_logger(__name__)

# ── Prompt directory ──────────────────────────────────────────────────────────
PROMPTS_DIR = Path(__file__).parent / "prompts"

def _load_prompts() -> tuple[str, str]:
    """Read prompt files fresh each cycle so edits take effect without restart."""
    system   = (PROMPTS_DIR / "system_v1.txt").read_text().strip()
    template = (PROMPTS_DIR / "user_template_v1.txt").read_text().strip()
    return system, template


def _legacy_action_to_intent(action: dict) -> str:
    """Map a legacy action string to new intent string for logging only."""
    return {
        "buy": "enter_long", "sell": "enter_short", "close": "close",
        "hold": "hold", "reallocate": "enter_long",
    }.get(str(action.get("action", "hold")).lower(), "hold")


def _load_strategy_config() -> str:
    """
    Read strategy_config.json (written by backtest_runner and weekly_review).
    Returns a formatted string for prompt injection, or empty string if missing.
    """
    path = Path(__file__).parent / "strategy_config.json"
    try:
        cfg  = json.loads(path.read_text())
        strat = cfg.get("active_strategy", "hybrid")
        notes = cfg.get("director_notes", "")[:300]
        p     = cfg.get("parameters", {})
        lines = [
            f"  Active strategy : {strat}",
            f"  Momentum weight : {p.get('momentum_weight', '?')}   "
            f"Mean-rev weight : {p.get('mean_reversion_weight', '?')}",
            f"  News weight     : {p.get('news_sentiment_weight', '?')}   "
            f"Cross-sector    : {p.get('cross_sector_weight', '?')}",
            f"  Confidence floor: {p.get('min_confidence_threshold', '?')}   "
            f"Max positions   : {p.get('max_positions', '?')}",
            f"  Sector bias     : {p.get('sector_rotation_bias', 'neutral')}",
        ]
        if notes:
            lines.append(f"  Director notes  : {notes}")

        # Time-bound actions (mandatory exits with deadlines)
        tba = cfg.get("time_bound_actions", [])
        if tba:
            lines.append("")
            lines.append("=== TIME-BOUND ACTIONS (MANDATORY) ===")
            for item in tba:
                lines.append(
                    f"  ⚠ {item.get('priority','HIGH')}: "
                    f"{item.get('action','exit').upper()} {item.get('symbol','?')} "
                    f"by {item.get('deadline_et','?')} ET"
                )
                lines.append(f"    Reason: {item.get('reason','')}")

        return "\n".join(lines)
    except Exception:
        return "  (strategy_config.json not yet generated — using system prompt defaults)"

# ── Clients ───────────────────────────────────────────────────────────────────
_alpaca_key    = os.getenv("ALPACA_API_KEY")
_alpaca_secret = os.getenv("ALPACA_SECRET_KEY")
_anthropic_key = os.getenv("ANTHROPIC_API_KEY")

if not _alpaca_key or not _alpaca_secret:
    raise EnvironmentError("Missing ALPACA_API_KEY or ALPACA_SECRET_KEY in .env")
if not _anthropic_key:
    raise EnvironmentError("Missing ANTHROPIC_API_KEY in .env")

alpaca     = TradingClient(_alpaca_key, _alpaca_secret, paper=True)
claude     = anthropic.Anthropic(api_key=_anthropic_key)
MODEL      = "claude-sonnet-4-6"
MODEL_FAST = "claude-haiku-4-5-20251001"

# ── Drawdown guard ────────────────────────────────────────────────────────────
_DRAWDOWN_THRESHOLD  = 0.20
_last_drawdown_alert = 0.0
_peak_equity         = None


# ── Twilio SMS ────────────────────────────────────────────────────────────────

def _send_sms(message: str) -> None:
    sid   = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    from_ = os.getenv("TWILIO_FROM_NUMBER")
    to    = os.getenv("TWILIO_TO_NUMBER")

    if not all([sid, token, from_, to]):
        log.warning("Twilio not configured — SMS skipped: %s", message)
        return

    try:
        from twilio.rest import Client
        Client(sid, token).messages.create(body=message, from_=from_, to=to)
        log.info("SMS sent: %s", message)
    except Exception as exc:
        log.error("SMS failed: %s", exc)


# ── Drawdown check ────────────────────────────────────────────────────────────

def _check_drawdown(equity: float) -> bool:
    global _peak_equity, _last_drawdown_alert

    if _peak_equity is None or equity > _peak_equity:
        _peak_equity = equity

    drawdown = (_peak_equity - equity) / _peak_equity

    if drawdown >= _DRAWDOWN_THRESHOLD and equity != _last_drawdown_alert:
        _last_drawdown_alert = equity
        msg = (f"TRADING BOT ALERT: 20% drawdown triggered. "
               f"Peak equity ${_peak_equity:,.0f} → current ${equity:,.0f} "
               f"({drawdown:.1%} drawdown). Bot halting — review required.")
        log.error(msg)
        _send_sms(msg)
        return True

    return False


# ── Prompt builder ────────────────────────────────────────────────────────────

def build_user_prompt(
    account,
    positions:            list,
    md:                   dict,
    session_tier:         str,
    session_instruments:  str,
    recent_decisions:     str,
    ticker_lessons:       str,
    next_cycle_time:      str = "?",
    vector_memories:      str = "",
    strategy_config_note: str = "",
    crypto_signals:       str = "",
    crypto_context:       str = "",
    regime_summary:       str = "",
    signal_scores:        str = "",
    pi_data:              dict = None,
    intraday_momentum:    str  = "",
    exit_status:          str  = "",
    macro_backdrop:       str  = "",
    scratchpad_section:   str  = "",
) -> str:
    equity        = float(account.equity)
    cash          = float(account.cash)
    buying_power  = float(account.buying_power)
    pdt_used      = int(getattr(account, "daytrade_count", 0) or 0)
    pdt_remaining = max(0, 3 - pdt_used)
    log.info("PDT      daytrade_count=%d  used=%d  remaining=%d",
             pdt_used, pdt_used, pdt_remaining)

    long_value   = sum(float(p.market_value) for p in positions if float(p.qty) > 0)
    exposure_pct = (long_value / equity * 100) if equity > 0 else 0.0

    if positions:
        rows = []
        for p in positions:
            unreal = float(p.unrealized_pl)
            sign   = "+" if unreal >= 0 else ""
            rows.append(
                f"  {p.symbol:<9} qty={float(p.qty):>8.4f}  "
                f"avg=${float(p.avg_entry_price):>10.2f}  "
                f"mkt=${float(p.current_price):>10.2f}  "
                f"P&L={sign}${unreal:.2f}"
            )
        positions_table = "\n".join(rows)
    else:
        positions_table = "  (none)"

    _, user_template = _load_prompts()

    # Portfolio intelligence sections
    _pi = pi_data or {}
    dyn_sizes_sec = "  (portfolio intelligence unavailable)"
    pos_health_sec = "  (portfolio intelligence unavailable)"
    corr_sec = "  (portfolio intelligence unavailable)"
    thesis_sec = "  (portfolio intelligence unavailable)"
    try:
        if _pi:
            open_syms = [p.symbol for p in positions if float(p.qty) > 0]
            dyn_sizes_sec  = pi.format_dynamic_sizes_section(
                _pi.get("sizes", {}), equity)
            pos_health_sec = pi.format_positions_with_health(positions, equity)
            corr_sec       = pi.format_correlation_section(
                _pi.get("correlation", {}), open_syms)
            thesis_sec     = pi.format_thesis_ranking_section(
                _pi.get("thesis_scores", []),
                _pi.get("weakest_symbol"))
    except Exception as _pi_exc:
        log.debug("PI section formatting failed (non-fatal): %s", _pi_exc)

    # C4: Extended session omits market-hours-only sections to reduce prompt size.
    # Estimated saving: ~1,000 tokens/call × 16 extended cycles/day.
    _is_extended = (session_tier == "extended")

    _sector_table     = "  (not shown — extended session)" if _is_extended else md.get("sector_table", "  (not available)")
    _morning_brief    = "  (not shown — extended session)" if _is_extended else md.get("morning_brief_section", "  (morning brief not yet generated)")
    _orb_section      = "  (not shown — extended session)" if _is_extended else md.get("orb_section", "  No ORB candidates identified for today.")
    _intraday_mom     = "  (not shown — extended session)" if _is_extended else (intraday_momentum or "  (unavailable)")
    _sector_news      = "  (not shown — extended session)" if _is_extended else md.get("sector_news", "  (none)")

    # C4: Extended session: reduce core watchlist to held positions + crypto only.
    if _is_extended:
        _held_syms = [p.symbol for p in positions if float(p.qty) > 0]
        _crypto_wl = ["BTC/USD", "ETH/USD"]
        _ext_syms  = _held_syms + [s for s in _crypto_wl if s not in _held_syms]
        _core_watchlist = "  Extended session — held positions + crypto only:\n  " + ", ".join(_ext_syms) if _ext_syms else "  (none)"
    else:
        _core_watchlist = md.get("core_by_sector", "  (not available)")

    log.debug("[PROMPT] building prompt  session=%s  scratchpad=%s  length_estimate=%d",
              session_tier,
              "yes" if scratchpad_section and "unavailable" not in scratchpad_section else "no",
              len(user_template))
    return user_template.format(
        session_tier=session_tier,
        session_instruments=session_instruments,
        next_cycle_time=next_cycle_time,
        equity=f"{equity:,.2f}",
        cash=f"{cash:,.2f}",
        buying_power=f"{buying_power:,.2f}",
        pdt_used=pdt_used,
        pdt_remaining=pdt_remaining,
        exposure_pct=exposure_pct,
        vix=md["vix"],
        vix_regime=md.get("vix_regime", str(md["vix"])),
        regime_instruction=md.get("regime_instruction", "Standard rules apply."),
        market_status=md["market_status"],
        time_et=md["time_et"],
        minutes_since_open=md["minutes_since_open"],
        sector_table=_sector_table,
        intermarket_signals=md.get("intermarket_signals", "  (not available)"),
        global_handoff=md.get("global_handoff", "  (not available)"),
        earnings_calendar=md.get("earnings_calendar", "  (not available)"),
        core_watchlist_by_sector=_core_watchlist,
        dynamic_watchlist=md.get("dynamic_section", "  (none)"),
        intraday_watchlist=md.get("intraday_section", "  (none)"),
        positions_table=positions_table,
        recent_decisions=recent_decisions or "  (none)",
        vector_memories=vector_memories or "  (none)",
        ticker_lessons=ticker_lessons or "  (none)",
        crypto_signals=md.get("crypto_signals", "  (none)"),
        crypto_context=crypto_context or "  (crypto context unavailable)",
        breaking_news=md.get("breaking_news", "  (none)"),
        sector_news=_sector_news,
        strategy_config_note=strategy_config_note or "  (using system prompt defaults)",
        morning_brief_section=_morning_brief,
        insider_section=md.get("insider_section",
                               "  (insider intelligence unavailable)"),
        reddit_section=md.get("reddit_section",
                               "  (Reddit sentiment unavailable)"),
        earnings_intel_section=md.get("earnings_intel_section",
                                      "  (no symbols near earnings)"),
        economic_calendar_section=md.get("economic_calendar_section",
                                         "  (economic calendar unavailable)"),
        macro_wire_section=md.get("macro_wire_section",
                                   "  No significant macro headlines in the past 4 hours."),
        orb_section=_orb_section,
        regime_summary=regime_summary or "  (regime classification unavailable)",
        signal_scores=signal_scores or "  (signal scoring unavailable)",
        intraday_momentum=_intraday_mom,
        exit_status=exit_status or "  (unavailable)",
        macro_backdrop=macro_backdrop or "",
        dynamic_sizes_section=dyn_sizes_sec,
        positions_with_health=pos_health_sec,
        correlation_section=corr_sec,
        thesis_ranking_section=thesis_sec,
        scratchpad_section=scratchpad_section or "  (scratchpad unavailable this cycle)",
    )


# ── Sequential Synthesis Pipeline ─────────────────────────────────────────────

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


def classify_regime(md: dict, calendar: dict) -> dict:
    """
    Phase 1: Haiku call classifying market regime from macro data.
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
        sec_lines = [l for l in (md.get("sector_table","") or "").splitlines() if "▲" in l or "▼" in l]
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

        # Macro backdrop inputs for richer regime classification
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
        resp = claude.messages.create(
            model=MODEL_FAST, max_tokens=300,
            system=[{"type":"text","text":_REGIME_SYS,"cache_control":{"type":"ephemeral"}}],
            messages=[{"role":"user","content":user_content}],
            extra_headers={"anthropic-beta":"prompt-caching-2024-07-31"},
        )
        try:
            from cost_tracker import get_tracker
            get_tracker().record_api_call(MODEL_FAST, resp.usage, caller="regime_classifier")
        except Exception as _ct_exc:
            log.warning("Cost tracker failed: %s", _ct_exc)
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n",1)[-1].rsplit("```",1)[0].strip()
        result = json.loads(raw)
        cr = getattr(resp.usage,"cache_read_input_tokens",0) or 0
        log.debug("[REGIME] score=%d bias=%s confidence=%s cache_read=%d",
                  result.get("regime_score",50), result.get("bias"), result.get("confidence"), cr)
        log_trade({"event":"regime_classification","regime_score":result.get("regime_score"),
                   "bias":result.get("bias"),"session_theme":result.get("session_theme"),
                   "confidence":result.get("confidence"),"constraints":result.get("constraints",[])})
        return result
    except Exception as exc:
        log.warning("[REGIME] Classifier failed (non-fatal): %s", exc)
        return _default


def score_signals(
    watchlist_symbols: list,
    regime: dict,
    md: dict,
    positions: list = None,
) -> dict:
    """
    Phase 2: Haiku call scoring watchlist symbols against all signals.
    Only runs during market session. Fails to empty dict on error.
    System prompt cached.

    Symbol list is capped at 15, prioritised:
      1. Currently held positions (always included)
      2. Morning brief conviction picks
      3. Breaking-news mentions this cycle
      4. Remaining watchlist symbols
    """
    if not watchlist_symbols:
        return {}
    try:
        # ── Build prioritised 15-symbol list ─────────────────────────────────
        _MAX_SCORED = 25
        scored: list[str] = []
        seen: set[str] = set()

        def _add(sym: str) -> None:
            if sym in seen or sym not in watchlist_symbols:
                return
            scored.append(sym)
            seen.add(sym)

        # 1. Held positions
        for p in (positions or []):
            if float(getattr(p, "qty", 0)) > 0:
                _add(p.symbol)

        # 2. Morning brief conviction picks
        try:
            _brief_path = Path(__file__).parent / "data" / "market" / "morning_brief.json"
            if _brief_path.exists():
                _brief = json.loads(_brief_path.read_text())
                for pick in _brief.get("conviction_picks", []):
                    _add(str(pick.get("symbol", "")))
        except Exception:
            pass

        # 3. Symbols mentioned in breaking news this cycle
        _news = md.get("breaking_news", "") or ""
        for sym in watchlist_symbols:
            if sym in _news:
                _add(sym)

        # 4. Fill remaining slots from original watchlist order
        for sym in watchlist_symbols:
            if len(scored) >= _MAX_SCORED:
                break
            _add(sym)

        log.debug("[SIGNALS] scoring %d/%d symbols: %s",
                  len(scored), len(watchlist_symbols), scored)

        insider_lines = [l.strip() for l in (md.get("insider_section","") or "").splitlines()
                         if any(s in l for s in scored)][:10]
        orb_str = "(none)"
        try:
            orb_path = Path(__file__).parent / "data" / "scanner" / "orb_candidates.json"
            if orb_path.exists():
                orb_cands = json.loads(orb_path.read_text()).get("candidates", [])
                orb_str = "\n".join(
                    f"{c['symbol']}: gap {c['gap_pct']:+.1f}% score={c['orb_score']:.2f} {c['conviction']}"
                    for c in orb_cands[:8]
                ) or "(none)"
        except Exception:
            pass
        reddit_lines = [l.strip() for l in (md.get("reddit_section","") or "").splitlines()
                        if any(s in l for s in scored)][:6]
        morning_lines= (md.get("morning_brief_section","") or "").splitlines()[:5]
        from memory import _load_pattern_watchlist  # noqa: PLC0415
        pwl      = _load_pattern_watchlist()
        pwl_lines= [f"{s}: min {d.get('minimum_signals_required',2)} signals required"
                    for s,d in pwl.items() if not d.get("graduated") and s in scored]

        user_content = (
            f"Symbols to score: {', '.join(scored)}\n\n"
            f"REGIME: score={regime.get('regime_score',50)} bias={regime.get('bias','neutral')} "
            f"theme={regime.get('session_theme','?')}\n"
            f"  constraints: {regime.get('constraints',[])}\n\n"
            f"INSIDER/CONGRESSIONAL:\n{chr(10).join(insider_lines) or '(none)'}\n\n"
            f"ORB CANDIDATES:\n{orb_str}\n\n"
            f"REDDIT:\n{chr(10).join(reddit_lines) or '(none)'}\n\n"
            f"MORNING BRIEF picks:\n{chr(10).join(morning_lines) or '(none)'}\n\n"
            f"PATTERN WATCHLIST (elevated conviction required):\n{chr(10).join(pwl_lines) or '(none)'}"
        )
        resp = claude.messages.create(
            model=MODEL_FAST, max_tokens=4000,
            system=[{"type":"text","text":_SIGNAL_SYS,"cache_control":{"type":"ephemeral"}}],
            messages=[{"role":"user","content":user_content}],
            extra_headers={"anthropic-beta":"prompt-caching-2024-07-31"},
        )
        try:
            from cost_tracker import get_tracker
            get_tracker().record_api_call(MODEL_FAST, resp.usage, caller="signal_scorer")
        except Exception as _ct_exc:
            log.warning("Cost tracker failed: %s", _ct_exc)
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n",1)[-1].rsplit("```",1)[0].strip()
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            # Repair attempt 1: truncate to last closing brace (handles token-cutoff)
            last_brace = raw.rfind("}")
            _repaired = False
            if last_brace >= 0:
                try:
                    result = json.loads(raw[:last_brace + 1])
                    log.debug("[SIGNALS] JSON repaired by truncation (last_brace=%d)", last_brace)
                    _repaired = True
                except json.JSONDecodeError:
                    pass
            if not _repaired:
                # Repair attempt 2: retry API call with completeness hint
                log.debug("[SIGNALS] JSON truncated, retrying API call with completeness hint")
                _retry_sys = _SIGNAL_SYS + "\nReturn ONLY valid complete JSON. If you cannot fit all symbols, return fewer rather than truncating."
                try:
                    _retry_resp = claude.messages.create(
                        model=MODEL_FAST, max_tokens=4000,
                        system=[{"type": "text", "text": _retry_sys}],
                        messages=[{"role": "user", "content": user_content}],
                        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                    )
                    _retry_raw = _retry_resp.content[0].text.strip()
                    if _retry_raw.startswith("```"):
                        _retry_raw = _retry_raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                    result = json.loads(_retry_raw)
                    log.debug("[SIGNALS] JSON recovered via retry")
                except Exception as _retry_exc:
                    log.warning("[SIGNALS] JSON parse failed after repair+retry — returning empty: %s", _retry_exc)
                    return {}
        log.info("[SIGNALS] top_3=%s  caution=%s", result.get("top_3",[]), result.get("elevated_caution",[]))
        log_trade({"event":"signal_scoring","top_3":result.get("top_3",[]),
                   "elevated_caution":result.get("elevated_caution",[]),
                   "scored_count":len(result.get("scored_symbols",{}))})
        try:
            from datetime import datetime as _dt
            conv_path = Path(__file__).parent / "data" / "market" / "daily_conviction.json"
            existing: list = []
            if conv_path.exists():
                try:
                    existing = json.loads(conv_path.read_text())
                    if not isinstance(existing, list):
                        existing = []
                except Exception:
                    existing = []
            existing.append({"ts": _dt.now().isoformat(), "top_3": result.get("top_3",[])})
            conv_path.write_text(json.dumps(existing[-50:], indent=2))
        except Exception:
            pass
        return result
    except Exception as exc:
        log.warning("[SIGNALS] Scorer failed (non-fatal): %s", exc)
        return {}


def format_regime_summary(regime: dict) -> str:
    if not regime or (regime.get("confidence") == "low" and not regime.get("session_theme")):
        return "  (regime classification unavailable this cycle)"
    lines = [
        f"  Score: {regime.get('regime_score',50)}/100  Bias: {regime.get('bias','neutral')}  "
        f"Confidence: {regime.get('confidence','low')}",
        f"  Theme: {regime.get('session_theme','')}",
    ]
    if regime.get("high_impact_warning"):
        lines.append(f"  WARNING: {regime['high_impact_warning']}")
    if regime.get("orb_context"):
        lines.append(f"  ORB: {regime['orb_context']}")
    for c in regime.get("constraints", []):
        lines.append(f"  Constraint: {c}")
    return "\n".join(lines)


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
    for sym, d in list(scores.get("scored_symbols", {}).items())[:10]:
        conv = d.get("conviction","?")
        cat  = (d.get("primary_catalyst","") or "")[:60]
        sigs = ", ".join((d.get("signals",[]) or [])[:4])
        orb_tag = " ORB" if d.get("orb_candidate") else ""
        pwl_tag = f"  ⚠{d['pattern_watchlist']}" if d.get("pattern_watchlist") else ""
        lines.append(f"  {sym}: score={d.get('score',0)} [{conv}]{orb_tag}  {cat}{pwl_tag}")
        if sigs:
            lines.append(f"    signals: {sigs}")
    return "\n".join(lines) if lines else "  (no signals scored)"


# ── Bull/Bear Trade Debate ────────────────────────────────────────────────────

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
    if equity <= 26_000:
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
        bull_resp = claude.messages.create(
            model=MODEL, max_tokens=400,
            system="You are a bullish equity trader. Make the strongest possible case FOR this trade. Be specific and data-driven. Return 3-5 bullet points.",
            messages=[{"role": "user", "content": f"Make the bull case for this trade:\n\n{context}"}],
        )
        bull_case = bull_resp.content[0].text.strip()

        bear_resp = claude.messages.create(
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
        synth_resp = claude.messages.create(
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


# ── Fundamental Pre-Check ─────────────────────────────────────────────────────

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

    fund_dir  = Path(__file__).parent / "data" / "fundamentals"
    fund_lines: list[str] = []

    for a in stock_buys:
        sym       = a.get("symbol", "")
        fund_path = fund_dir / f"{sym}.json"
        try:
            if fund_path.exists():
                f     = json.loads(fund_path.read_text())
                pe    = f.get("pe_ratio", "N/A")
                mktcap = f.get("market_cap_b", "N/A")
                hi52  = f.get("52w_high", "N/A")
                lo52  = f.get("52w_low", "N/A")
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
        resp = claude.messages.create(
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


# ── Claude ────────────────────────────────────────────────────────────────────

_OVERNIGHT_SYS = (
    "You are a crypto position manager for an overnight trading session. "
    "Only BTC/USD and ETH/USD are tradeable. JSON only, no markdown.\n"
    "Output: "
    '{"reasoning":"<1 sentence>","regime":"normal"|"caution"|"halt",'
    '"actions":[{"action":"hold"|"close","symbol":"BTC/USD"|"ETH/USD",'
    '"qty":<float>,"order_type":"market","stop_loss":<float>,'
    '"take_profit":<float>,"tier":"core","catalyst":"<reason>",'
    '"confidence":"low"|"medium"|"high"}],'
    '"notes":"<flag anything unusual>"}'
)

_OVERNIGHT_DEFAULT: dict = {
    "reasoning": "Overnight default — hold all positions.",
    "regime": "normal",
    "actions": [],
    "notes": "",
}


def _ask_claude_overnight(
    positions: list,
    crypto_context: str,
    regime_obj: dict,
    macro_wire: str,
) -> dict:
    """
    Lightweight Haiku decision for overnight crypto-only sessions.
    Uses a minimal prompt — no signal scores, no sector data, no watchlist.
    Falls back to hold-all default on any error, never raises.

    C3: saves ~$0.042/cycle × 24 overnight cycles/day ≈ $1.01/day vs Sonnet.
    """
    try:
        from zoneinfo import ZoneInfo as _ZI
        from datetime import datetime as _dt
        _time_str = _dt.now(_ZI("America/New_York")).strftime("%I:%M %p ET")

        pos_lines = []
        for p in positions:
            unreal = float(p.unrealized_pl)
            pos_lines.append(
                f"  {p.symbol} qty={float(p.qty):.6f}  "
                f"avg=${float(p.avg_entry_price):.2f}  "
                f"mkt=${float(p.current_price):.2f}  "
                f"P&L={'+'if unreal>=0 else ''}{unreal:.2f}"
            )
        pos_block = "\n".join(pos_lines) or "  (none)"

        regime_score = regime_obj.get("regime_score", 50)
        bias         = regime_obj.get("bias", "neutral")

        prompt = (
            f"=== OVERNIGHT SESSION ===\n"
            f"Time: {_time_str}\n\n"
            f"=== OPEN POSITIONS ===\n{pos_block}\n\n"
            f"=== REGIME ===\n"
            f"Score: {regime_score}  Bias: {bias}\n\n"
            f"=== CRYPTO CONTEXT ===\n{crypto_context or '  (unavailable)'}\n\n"
            f"=== MACRO WIRE (top items) ===\n{macro_wire or '  (none)'}\n\n"
            "=== TASK ===\n"
            "Overnight session. Only BTC/USD and ETH/USD are tradeable.\n"
            "For each open crypto position: hold (with updated stop/target) or close.\n"
            "If no crypto positions exist, return empty actions list.\n"
            "Never fabricate catalysts. If in doubt, hold."
        )

        response = claude.messages.create(
            model=MODEL_FAST,
            max_tokens=400,
            system=[{"type": "text", "text": _OVERNIGHT_SYS}],
            messages=[{"role": "user", "content": prompt}],
        )
        try:
            from cost_tracker import get_tracker
            get_tracker().record_api_call(
                MODEL_FAST, response.usage, caller="ask_claude_overnight"
            )
        except Exception:
            pass

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        result = json.loads(raw)
        log.info("[OVERNIGHT] Haiku decision: regime=%s  actions=%d",
                 result.get("regime", "?"), len(result.get("actions", [])))
        return result

    except Exception as _exc:
        log.warning("[OVERNIGHT] _ask_claude_overnight failed (%s) — using hold-all default", _exc)
        return _OVERNIGHT_DEFAULT


# ── Compact prompt builder (for gate COMPACT cycles) ──────────────────────────

_compact_template_cache: str = ""


def _load_compact_template() -> str:
    global _compact_template_cache
    if not _compact_template_cache:
        p = Path(__file__).parent / "prompts" / "compact_template.txt"
        _compact_template_cache = p.read_text() if p.exists() else ""
    return _compact_template_cache


def build_compact_prompt(
    account,
    positions:          list,
    md:                 dict,
    session_tier:       str,
    regime_obj:         dict,
    signal_scores_obj:  dict,
    time_bound_actions: list,
    pi_data:            dict,
    exit_status:        str = "",
) -> str:
    """
    Build the compact 6-block prompt (~1,500 tokens vs ~7,500 for full).
    Used for low-information cycles where no material state change occurred.
    """
    template = _load_compact_template()
    if not template:
        log.warning("[GATE] compact_template.txt missing — falling back to full prompt indicator")
        return ""

    equity       = float(account.equity)
    cash         = float(account.cash)
    buying_power = float(account.buying_power)
    pdt_used     = int(getattr(account, "daytrade_count", 0) or 0)
    pdt_remaining = max(0, 3 - pdt_used)
    long_val      = sum(float(p.market_value) for p in positions if float(p.qty) > 0)
    exposure_pct  = (long_val / equity * 100) if equity > 0 else 0.0
    cash_pct      = (cash / equity * 100) if equity > 0 else 0.0
    vix           = float(md.get("vix", 0) or 0)

    # Drawdown from peak (pulled from portfolio intelligence if available)
    _pi = pi_data or {}
    drawdown_pct = float(_pi.get("drawdown_pct", 0) or 0)

    # VIX label
    if vix > 35:
        vix_label = "HALT"
    elif vix > 25:
        vix_label = "elevated"
    elif vix < 15:
        vix_label = "calm"
    else:
        vix_label = "normal"

    # Positions block
    if positions:
        rows = []
        for p in positions:
            sym    = p.symbol
            qty    = float(p.qty)
            entry  = float(p.avg_entry_price)
            cur    = float(p.current_price)
            pnl    = float(p.unrealized_pl)
            sign   = "+" if pnl >= 0 else ""
            rows.append(
                f"  {sym:<8} qty={qty:.4f}  entry=${entry:.2f}  "
                f"now=${cur:.2f}  P&L={sign}${pnl:.2f}"
            )
            # Append exit info if available
            if exit_status and sym in exit_status:
                for line in exit_status.splitlines():
                    if sym in line and ("stop" in line.lower() or "target" in line.lower()):
                        rows.append("    " + line.strip())
                        break
        positions_block = "\n".join(rows)
    else:
        positions_block = "  No open positions."

    # Regime context
    regime_bias  = regime_obj.get("bias", "unknown")
    regime_score = regime_obj.get("regime_score", 50)
    constraints  = regime_obj.get("constraints", [])
    hi_warning   = regime_obj.get("high_impact_warning", "")

    # Leading sectors (from sector_table — top 2 lines)
    sector_lines = [
        l.strip() for l in (md.get("sector_table", "") or "").splitlines()
        if ("▲" in l or "▼" in l) and l.strip()
    ]
    top_sectors = "  ".join(sector_lines[:2]) if sector_lines else "unavailable"

    # Macro constraint (first regime constraint or high-impact warning)
    if hi_warning:
        macro_constraint = hi_warning
    elif constraints:
        macro_constraint = constraints[0]
    else:
        macro_constraint = "No hard macro constraint"

    # Top catalyst (from breaking news — first 120 chars)
    breaking = (md.get("breaking_news", "") or "").strip()
    top_catalyst = breaking[:120] if breaking and breaking not in ("(none)", "(extended/overnight — not fetched)") else "No fresh catalyst last 15 min"

    # Top 5 signals
    scored = signal_scores_obj.get("scored_symbols", {}) if signal_scores_obj else {}
    n_scored = len(scored)
    if scored:
        top5 = sorted(scored.items(), key=lambda kv: float(kv[1].get("score", 0)) if isinstance(kv[1], dict) else 0, reverse=True)[:5]
        sig_lines = []
        for sym, data in top5:
            if not isinstance(data, dict):
                continue
            sc   = data.get("score", 0)
            dirn = data.get("direction", "neutral")
            cat  = data.get("catalyst", "")[:80]
            sigs = data.get("signals", "")[:60]
            price = data.get("price", 0)
            price_str = f"  ${price:.2f}" if price else ""
            sig_lines.append(
                f"  {sym}: score={sc:.0f} direction={dirn}{price_str}\n"
                f"    catalyst: {cat}\n"
                f"    signals: {sigs}"
            )
        top_signals_block = "\n".join(sig_lines) if sig_lines else "  No signals scored this cycle."
    else:
        top_signals_block = "  No signals scored this cycle."

    # Constraints block (deadlines + VIX gates + PDT)
    clines = []
    if vix >= 35:
        clines.append("HALT: VIX >= 35 — no new positions")
    elif vix >= 25:
        clines.append(f"VIX {vix:.1f} >= 25 — reduce all sizes 50%")
    if pdt_remaining == 0:
        clines.append("PDT: 0 day trades remaining — no new stock/ETF entries")
    for tba in (time_bound_actions or []):
        sym = tba.get("symbol", "")
        reason = tba.get("reason", "time-bound exit")
        et = tba.get("deadline_et", tba.get("exit_by", ""))
        clines.append(f"DEADLINE EXIT: {sym} by {et} — {reason}")
    if constraints:
        clines.extend(constraints[:2])
    constraints_block = "\n".join(f"  {l}" for l in clines) if clines else "  No active constraints."

    try:
        return template.format(
            equity=f"{equity:,.2f}",
            cash_pct=f"{cash_pct:.1f}",
            exposure_pct=f"{exposure_pct:.1f}",
            buying_power=f"{buying_power:,.2f}",
            pdt_remaining=pdt_remaining,
            drawdown_pct=f"{drawdown_pct:.1f}",
            vix=f"{vix:.1f}",
            vix_label=vix_label,
            session_tier=session_tier,
            time_et=md.get("time_et", "?"),
            n_positions=len(positions),
            positions_block=positions_block,
            regime_bias=regime_bias,
            regime_score=regime_score,
            top_sectors=top_sectors,
            macro_constraint=macro_constraint,
            top_catalyst=top_catalyst,
            n_scored=n_scored,
            top_signals_block=top_signals_block,
            constraints_block=constraints_block,
        )
    except KeyError as _ke:
        log.warning("[GATE] compact prompt format error: %s", _ke)
        return ""



def _log_skip_cycle(state) -> None:
    log.info(
        "[GATE] SKIP consecutive=%d  skips_today=%d  sonnet_today=%d",
        state.consecutive_skips,
        state.total_skips_today,
        state.total_calls_today,
    )


def ask_claude(user_prompt: str) -> dict:
    system_prompt, _ = _load_prompts()
    response = claude.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=[{
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_prompt}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )
    usage       = response.usage
    cache_read  = getattr(usage, "cache_read_input_tokens",     0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    log.debug("Cache stats: reads=%d writes=%d regular=%d",
              cache_read, cache_write,
              max(0, (usage.input_tokens or 0) - cache_read - cache_write))
    try:
        from cost_tracker import get_tracker
        get_tracker().record_api_call(MODEL, usage, caller="ask_claude")
    except Exception as _ct_exc:
        log.warning("Cost tracker failed: %s", _ct_exc)
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        raw = raw.rsplit("```", 1)[0].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Claude returned non-JSON:\n{raw}") from exc


# ── Cycle ─────────────────────────────────────────────────────────────────────

def run_cycle(
    session_tier:        str = "market",
    session_instruments: str = "stocks, ETFs, crypto",
    next_cycle_time:     str = "?",
) -> None:
    t_start = time.monotonic()
    log.info("── Cycle start  session=%s ─────────────────────────────", session_tier)

    # Early-init regime so forced/deadline exit guards never see a NameError.
    # The real value is overwritten at ~line 1131 once ask_claude() returns.
    regime = "unknown"

    # 1. Account
    account   = alpaca.get_account()
    positions = alpaca.get_all_positions()
    equity          = float(account.equity)
    cash            = float(account.cash)
    buying_power_float = float(account.buying_power)
    long_val  = sum(float(p.market_value) for p in positions if float(p.qty) > 0)
    exposure  = long_val / equity * 100 if equity > 0 else 0.0

    log.info("Account  equity=$%s  cash=$%s  exposure=%.1f%%  positions=%d",
             f"{equity:,.0f}", f"{cash:,.0f}", exposure, len(positions))

    # 2. Drawdown guard
    if _check_drawdown(equity):
        log.error("Drawdown guard triggered — halting cycle")
        return

    # 3. Watchlist + market data
    wl = wm.get_active_watchlist()
    symbols_stock  = wl["stocks"] + wl["etfs"]
    symbols_crypto = wl["crypto"]

    # Run watchlist feedback loop
    wm.maybe_reset_session_tiers()
    wm.prune_stale_intraday()

    md = market_data.fetch_all(
        symbols_stock, symbols_crypto, session_tier,
        next_cycle_time=next_cycle_time,
    )
    log.info("Market   status=%s  vix=%.2f  time=%s",
             md["market_status"], md["vix"], md["time_et"])

    # Update ORB formation range (no-op outside 9:30-9:45 window)
    try:
        import scheduler as _sched_orb
        _sched_orb._update_orb_range(md.get("current_prices", {}))
    except Exception as _orb_rng_exc:
        log.debug("ORB range update failed (non-fatal): %s", _orb_rng_exc)

    # Load crypto context (sentiment + ETH/BTC ratio)
    try:
        _crypto_sentiment = dw.load_crypto_sentiment()
        _eth_btc          = md.get("eth_btc", {})
        crypto_context    = market_data.build_crypto_context_section(
            _crypto_sentiment, _eth_btc, session_tier)
    except Exception as _cc_exc:
        log.debug("Crypto context build failed (non-fatal): %s", _cc_exc)
        crypto_context = "  (crypto context unavailable)"

    # 4. Memory
    mem.update_outcomes_from_alpaca()
    recent_decisions = mem.get_recent_decisions_str()
    # Use pattern learning watchlist instead of simple avoid list
    try:
        ticker_lessons = mem.get_pattern_watchlist_summary()
    except Exception:
        ticker_lessons = mem.get_ticker_lessons()

    # Publish exit posts for newly resolved trades
    if publisher and publisher.enabled:
        try:
            resolved = mem.get_newly_resolved_trades()
            for trade in resolved:
                # entry_price from memory (stored when position was opened)
                _entry_px = float(
                    trade.get("entry_price") or
                    trade.get("avg_entry_price") or
                    trade.get("price") or 0
                )
                publisher.publish_trade_exit(
                    symbol=trade["symbol"],
                    entry_price=_entry_px,
                    exit_price=0.0,        # will be overwritten by Alpaca fill data
                    qty=float(trade.get("qty") or 1),
                    pnl=float(trade.get("pnl") or 0),
                    hold_time_hours=float(trade.get("hold_time_hours") or 0.0),
                    outcome=trade["outcome"],
                    alpaca_client=alpaca,  # verify position gone + fetch real fill
                )
        except Exception as _pub_exc:
            log.debug("publisher exit posts failed (non-fatal): %s", _pub_exc)

    # 4b. Vector memory — retrieve similar past scenarios (trade + scratchpad cold)
    _two_tier         = trade_memory.get_two_tier_memory(md, session_tier,
                                                         n_trade_results=5,
                                                         n_scratchpad_results=3)
    similar_scenarios = _two_tier["trade_scenarios"]
    vector_memories   = trade_memory.format_retrieved_memories(similar_scenarios)
    _sim_scratchpads  = _two_tier["recent_scratchpads"]
    if _sim_scratchpads:
        _scr_cold_lines = []
        for _i, _s in enumerate(_sim_scratchpads, start=1):
            _m = _s.get("metadata", {})
            _scr_cold_lines.append(
                f"  [SP{_i}] {str(_m.get('ts',''))[:16]}  "
                f"vix={_m.get('vix','?')}  regime={_m.get('regime_score','?')}  "
                f"watching={_m.get('watching','')}  \u2192 {_m.get('summary','')}"
            )
        vector_memories += "\n\nSimilar past scratchpads:\n" + "\n".join(_scr_cold_lines)
    vm_stats          = trade_memory.get_collection_stats()
    log.info("VectorMem  status=%s  stored=%d  retrieved=%d  scr_stored=%d",
             vm_stats["status"], vm_stats.get("total", 0),
             len(similar_scenarios), vm_stats.get("scr_total", 0))

    # 4c. Strategy config
    strategy_config_note = _load_strategy_config()

    # 4d. Portfolio intelligence — compute BEFORE pipeline, then reconcile
    pi_data = {}
    recon_log: list[str] = []
    recon_diff = None  # ReconciliationDiff — used by sonnet gate
    try:
        cfg = json.loads((Path(__file__).parent / "strategy_config.json").read_text()) \
            if (Path(__file__).parent / "strategy_config.json").exists() else {}
        pi_data = pi.build_portfolio_intelligence(
            equity, positions, cfg, buying_power=buying_power_float
        )

        # ── Reconciliation: deadline exits + forced exits + stop audit ────────
        # Build a BrokerSnapshot for the reconciliation engine
        from schemas import BrokerSnapshot, NormalizedPosition as _NP  # noqa: PLC0415

        _norm_positions = []
        for _p in positions:
            try:
                _norm_positions.append(_NP.from_alpaca_position(_p))
            except Exception:
                pass

        # Fetch open orders for stop audit (non-fatal if unavailable)
        _open_orders = []
        try:
            from schemas import NormalizedOrder  # noqa: PLC0415
            _raw_orders = alpaca.get_orders(GetOrdersRequest(status="open")) or []
            for _o in _raw_orders:
                try:
                    _open_orders.append(NormalizedOrder.from_alpaca_order(_o))
                except Exception:
                    pass
        except Exception as _ord_exc:
            log.debug("[RECON] Could not fetch open orders: %s", _ord_exc)

        _snapshot = BrokerSnapshot(
            equity=float(equity or 0),
            cash=float(equity or 0),
            buying_power=float(buying_power_float or 0),
            open_orders=_open_orders,
            positions=_norm_positions,
        )

        recon_log, recon_diff = recon.run_account1_reconciliation(
            positions=_norm_positions,
            snapshot=_snapshot,
            config=cfg,
            alpaca_client=alpaca,
            regime=regime,
            pi_data=pi_data,
        )
        for _rl in recon_log:
            log.info(_rl)

    except Exception as _pi_exc:
        log.warning("Portfolio intelligence / reconciliation block failed: %s", _pi_exc)

    # 4e. Exit management — refresh stale stops, trail profitable positions
    exit_manager_results = []
    exit_status_str = "  (unavailable)"
    try:
        import exit_manager as _em  # noqa: PLC0415
        exit_manager_results = _em.run_exit_manager(positions, alpaca, cfg)
        exit_status_str = _em.format_exit_status_section(positions, alpaca, cfg)
    except Exception as _em_exc:
        log.debug("Exit manager failed (non-fatal): %s", _em_exc)

    # 5. Sequential synthesis pipeline (Haiku pre-classification)
    regime_obj = {}
    signal_scores_obj = {}
    if session_tier == "market":
        try:
            calendar = dw.load_economic_calendar()
        except Exception:
            calendar = {}
        regime_obj = classify_regime(md, calendar)
        signal_scores_obj = score_signals(symbols_stock, regime_obj, md, positions=positions)

        # Write signal scores to disk for Account 2 to read (BUG-004)
        # B1: inject current price into each symbol's dict so Account 2 can generate candidates.
        # Try both slash-format (BTC/USD) and no-slash (BTCUSD) — same defensive pattern as
        # market_data._crypto_bars_lookup().
        try:
            _prices = md.get("current_prices", {})
            _scored = signal_scores_obj.get("scored_symbols", {})
            for _sym, _sig in _scored.items():
                if isinstance(_sig, dict):
                    _price = _prices.get(_sym) or _prices.get(_sym.replace("/", ""))
                    if _price:
                        _sig["price"] = float(_price)
                    else:
                        log.debug("[SIGNALS] %s: no price in current_prices — price field omitted", _sym)
        except Exception as _pinj_exc:
            log.warning("[SIGNALS] price injection failed (non-fatal): %s", _pinj_exc)

        try:
            _ss_path = Path(__file__).parent / "data" / "market" / "signal_scores.json"
            _ss_path.parent.mkdir(parents=True, exist_ok=True)
            _ss_path.write_text(json.dumps(signal_scores_obj))
            log.debug("[SIGNALS] wrote %d scores to signal_scores.json", len(signal_scores_obj))
        except Exception as _ss_exc:
            log.warning("[SIGNALS] could not write signal_scores.json (non-fatal): %s", _ss_exc)

    # 5a. Stage 2.5 — Haiku scratchpad pre-analysis (market session only)
    scratchpad_result = {}
    if session_tier == "market" and signal_scores_obj:
        try:
            scratchpad_result = _scratchpad.run_scratchpad(
                signal_scores     = signal_scores_obj,
                regime            = regime_obj,
                market_conditions = md,
                positions         = positions,
            )
            if scratchpad_result:
                _scratchpad.save_hot_scratchpad(scratchpad_result)
                trade_memory.save_scratchpad_memory(scratchpad_result)
        except Exception as _sp_exc:
            log.warning("[SCRATCHPAD] Stage 2.5 failed (non-fatal): %s", _sp_exc)

    regime_summary_str = format_regime_summary(regime_obj)
    signal_scores_str  = format_signal_scores(signal_scores_obj)

    # Build intraday momentum section from 5-min bar cache
    intraday_momentum_str = "  (unavailable)"
    try:
        import intraday_cache as _ic
        intraday_momentum_str = _ic.build_intraday_momentum_section(
            symbols_stock, md.get("current_prices", {})
        )
    except Exception as _im_exc:
        log.debug("Intraday momentum section failed (non-fatal): %s", _im_exc)

    # Build persistent macro backdrop section (cache-first, non-fatal)
    macro_backdrop_str = ""
    try:
        import macro_intelligence as _macro  # noqa: PLC0415
        macro_backdrop_str = _macro.build_macro_backdrop_section()
    except Exception as _macro_exc:
        log.debug("Macro backdrop failed (non-fatal): %s", _macro_exc)

    # 5b. Build prompt & call Claude
    # C3: overnight session uses a lightweight Haiku call instead of Sonnet.
    # Saves ~$0.042/cycle × 24 cycles/day ≈ $1.01/day.
    if session_tier == "overnight":
        decision = _ask_claude_overnight(
            positions=positions,
            crypto_context=crypto_context,
            regime_obj=regime_obj,
            macro_wire=md.get("macro_wire_section", ""),
        )
    else:
        # Issue C: derive regime_str â classify_regime() returns bias, not "halt"/"caution"
        _vix = float(md.get("vix", 0) or 0)
        _bias = regime_obj.get("bias", "neutral")
        if _vix >= 35:
            regime_str = "halt"
        elif _bias == "risk-off" and _vix > 25:
            regime_str = "caution"
        else:
            regime_str = _bias

        # [GATE] Sonnet gate â skip if no material state change since last call
        from datetime import datetime as _dt       # noqa: PLC0415
        from zoneinfo import ZoneInfo as _ZI       # noqa: PLC0415
        _use_compact = False  # initialised before gate so attribution can read it unconditionally
        _gate_state = _gate.load_gate_state()
        _gate_full_cfg = cfg if isinstance(cfg, dict) else {}
        _tba_list   = cfg.get("time_bound_actions", []) if isinstance(cfg, dict) else []
        _run_sonnet, _gate_reasons, _gate_state = _gate.should_run_sonnet(
            session_tier=session_tier,
            regime=regime_str,
            vix=_vix,
            signal_scores=signal_scores_obj,
            positions=positions,
            recon_diff=recon_diff,
            breaking_news=md.get("breaking_news", ""),
            time_bound_actions=_tba_list,
            current_time_et=_dt.now(_ZI("America/New_York")),
            gate_state=_gate_state,
            config=_gate_full_cfg,
            equity=equity,
        )
        _gate.save_gate_state(_gate_state)

        if not _run_sonnet:
            _log_skip_cycle(_gate_state)
            decision = {
                "reasoning": "gate skipped â no material state change",
                "regime_view": regime_str,
                "ideas": [],
                "holds": [normalize_symbol(p.symbol) for p in positions],
                "notes": "",
                "concerns": "",
            }
        else:
            _use_compact = _gate.should_use_compact_prompt(
                _gate_reasons, positions, signal_scores_obj, recon_diff
            )
            if _use_compact:
                log.info("[GATE] SONNET triggered (%s) â COMPACT",
                         ", ".join(r.value for r in _gate_reasons))
                user_prompt = build_compact_prompt(
                    account=account,
                    positions=positions,
                    md=md,
                    session_tier=session_tier,
                    regime_obj=regime_obj,
                    signal_scores_obj=signal_scores_obj,
                    time_bound_actions=_tba_list,
                    pi_data=pi_data,
                    exit_status=exit_status_str,
                )
            else:
                log.info("[GATE] SONNET triggered (%s) â FULL",
                         ", ".join(r.value for r in _gate_reasons))
                user_prompt = build_user_prompt(
                    account=account,
                    positions=positions,
                    md=md,
                    session_tier=session_tier,
                    session_instruments=session_instruments,
                    recent_decisions=recent_decisions,
                    ticker_lessons=ticker_lessons,
                    next_cycle_time=next_cycle_time,
                    vector_memories=vector_memories,
                    strategy_config_note=strategy_config_note,
                    crypto_signals=md.get("crypto_signals", "  (none)"),
                    crypto_context=crypto_context,
                    regime_summary=regime_summary_str,
                    signal_scores=signal_scores_str,
                    pi_data=pi_data,
                    intraday_momentum=intraday_momentum_str,
                    exit_status=exit_status_str,
                    macro_backdrop=macro_backdrop_str,
                    scratchpad_section=_scratchpad.format_scratchpad_section(scratchpad_result),
                )
            decision = ask_claude(user_prompt)
    # Parse Claude's response — supports both new intent-based and legacy formats
    try:
        claude_decision = validate_claude_decision(decision)
    except Exception as _cd_exc:
        log.warning("[SCHEMA] ClaudeDecision parse failed: %s — using raw dict fallback", _cd_exc)
        claude_decision = None

    regime    = claude_decision.regime_view if claude_decision else decision.get("regime", "unknown")
    reasoning = claude_decision.reasoning   if claude_decision else decision.get("reasoning", "")
    notes     = claude_decision.notes       if claude_decision else decision.get("notes", "")

    # Process ideas through risk kernel → broker-ready action dicts
    broker_actions: list = []
    if claude_decision and claude_decision.ideas and regime != "halt":
        _prices = md.get("current_prices", {})
        # Build snapshot for risk kernel (reuse normalized positions built above)
        _rk_positions = []
        for _p in positions:
            try:
                _rk_positions.append(_NP.from_alpaca_position(_p))
            except Exception:
                pass
        _rk_snapshot = _BrokerSnapshot(
            equity=float(equity or 0),
            cash=float(equity or 0),
            buying_power=float(buying_power_float or 0),
            open_orders=[],
            positions=_rk_positions,
        )
        _rk_cfg = {}
        try:
            _rk_cfg_path = Path(__file__).parent / "strategy_config.json"
            _rk_cfg = json.loads(_rk_cfg_path.read_text()) if _rk_cfg_path.exists() else {}
        except Exception:
            pass

        for _idea in claude_decision.ideas:
            _sym = normalize_symbol(_idea.symbol)
            _price = _prices.get(_sym) or _prices.get(_idea.symbol)
            _price = float(_price) if _price else None
            _result = risk_kernel.process_idea(
                _idea, _rk_snapshot, None, _rk_cfg,
                _price, session_tier, float(md.get("vix", 20.0))
            )
            if isinstance(_result, _BrokerAction):
                broker_actions.append(_result)
                log.info(
                    "[KERNEL] APPROVED %s %s → action=%s qty=%s stop=$%s",
                    _idea.intent, _idea.symbol, _result.action.value,
                    _result.qty, _result.stop_loss,
                )
            else:
                log.info("[KERNEL] REJECTED %s %s — %s", _idea.intent, _idea.symbol, _result)

    actions = [ba.to_dict() for ba in broker_actions]

    log.info(
        "[SCHEMA] regime_view=%s  ideas=%d  kernel_approved=%d  reasoning: %s",
        regime,
        len(claude_decision.ideas) if claude_decision else 0,
        len(actions),
        reasoning,
    )
    if notes:
        log.debug("Claude notes: %s", notes)

    if regime == "halt":
        log.warning("Claude returned regime=halt — skipping execution this cycle")
        _send_sms(f"TRADING BOT: Claude called HALT. Reasoning: {reasoning[:160]}")

    # Attribution — build tags, generate decision ID, log event
    _decision_id = ""
    _module_tags: dict = {}
    _trigger_flags: dict = {}
    try:
        from attribution import (  # noqa: PLC0415
            build_module_tags, build_trigger_flags,
            generate_decision_id, log_attribution_event,
        )
        _decision_id = generate_decision_id(
            "A1", _dt.now(_ZI("America/New_York")).strftime("%Y%m%d_%H%M%S")
        )
        _module_tags = build_module_tags(
            session_tier=session_tier,
            gate_reasons=_gate_reasons,
            used_compact=_use_compact,
            gate_skipped=not _run_sonnet,
            scratchpad_result=scratchpad_result if "scratchpad_result" in dir() else {},
            retrieved_memories=similar_scenarios if "similar_scenarios" in dir() else [],
            macro_backdrop_str=macro_backdrop_str if "macro_backdrop_str" in dir() else "",
            macro_wire_str=md.get("macro_wire_section", ""),
            morning_brief=md.get("morning_brief_section", ""),
            insider_section=md.get("insider_section", ""),
            reddit_section=md.get("reddit_section", ""),
            earnings_intel=md.get("earnings_intel_section", ""),
            recon_diff=recon_diff if "recon_diff" in dir() else None,
            positions=positions,
        )
        _trigger_flags = build_trigger_flags(_gate_reasons)
        log_attribution_event(
            event_type="decision_made",
            decision_id=_decision_id,
            account="A1",
            symbol="portfolio",
            module_tags=_module_tags,
            trigger_flags=_trigger_flags,
        )
    except Exception as _attr_exc:
        log.debug("Attribution block failed (non-fatal): %s", _attr_exc)

    # Persist decision — JSON rolling memory + ChromaDB vector store
    vector_id = trade_memory.save_trade_memory(decision, md, session_tier)
    mem.save_decision(decision, session_tier, vector_id=vector_id,
                      decision_id=_decision_id)

    # Log pattern learning observations for watchlisted symbols
    try:
        from memory import _load_pattern_watchlist, add_watchlist_observation  # noqa: PLC0415
        pwl = _load_pattern_watchlist()
        current_prices = md.get("current_prices", {})
        for sym in list(pwl.keys()):
            if pwl[sym].get("graduated"):
                continue
            price = current_prices.get(sym)
            if price is None:
                continue
            # Extract any mention of this symbol in Claude's notes/reasoning
            claude_mention = ""
            full_text = reasoning + " " + (notes or "")
            if sym in full_text:
                # Find the sentence containing the symbol
                for sent in full_text.split("."):
                    if sym in sent:
                        claude_mention = sent.strip()[:120]
                        break
            lesson_text = claude_mention or f"{sym} observed at ${price:.2f}"
            conditions_active = []
            if sym in md.get("watchlist_signals",""):
                for line in md["watchlist_signals"].splitlines():
                    if sym in line:
                        conditions_active.append(line.strip()[:80])
                        break
            add_watchlist_observation(
                symbol=sym,
                price_action=f"${price:.2f} today",
                conditions=conditions_active[:3],
                lesson=lesson_text,
                source="auto_observation",
            )
    except Exception as _pwl_exc:
        log.debug("Pattern watchlist observation failed (non-fatal): %s", _pwl_exc)

    # Auto-enroll skipped symbols into the pattern learning watchlist
    try:
        for action in actions:
            if action.get("action", "").lower() == "skip":
                sym = action.get("symbol", "")
                if not sym:
                    continue
                rationale = (
                    action.get("catalyst")
                    or action.get("rationale")
                    or reasoning
                )[:300]
                mem.add_symbol_to_pattern_watchlist(
                    symbol=sym,
                    reason=rationale,
                    market_context=f"VIX={md['vix']:.1f} regime={regime}",
                )
                log.info("[PATTERN_WL] Auto-enrolled %s — skip rationale: %s",
                         sym, rationale[:80])
    except Exception as _skip_enroll_exc:
        log.debug("Skip auto-enroll failed (non-fatal): %s", _skip_enroll_exc)

    # Auto-promote tickers from breaking news only (not Claude's reasoning text)
    wm.run_feedback_loop(
        breaking_news_text=md.get("breaking_news", ""),
    )

    # Log cycle decision
    log_trade({
        "event":       "cycle_decision",
        "session":     session_tier,
        "regime_view": regime,
        "reasoning":   reasoning,
        "notes":       notes,
        "vix":         md["vix"],
        "equity":      equity,
        "exposure":    round(exposure, 2),
        "n_ideas":     len(claude_decision.ideas) if claude_decision else 0,
        "n_actions":   len(actions),
    })

    # Print to terminal
    print("=" * 62)
    print("  CLAUDE'S DECISION")
    print("=" * 62)
    print(f"  Regime    : {regime}")
    print(f"  Reasoning : {reasoning}")

    if claude_decision and claude_decision.ideas:
        print(f"\n  Ideas ({len(claude_decision.ideas)}):")
        for i, idea in enumerate(claude_decision.ideas, 1):
            tier_val = idea.tier.value if hasattr(idea.tier, "value") else str(idea.tier)
            print(f"    [{i}] {idea.intent.upper()} {normalize_symbol(idea.symbol)} "
                  f"[{tier_val}]  conviction={idea.conviction:.2f}")
            print(f"        catalyst  : {idea.catalyst}")
            if idea.sector_signal:
                print(f"        sector    : {idea.sector_signal}")
            if idea.advisory_stop_pct:
                print(f"        adv_stop  : {idea.advisory_stop_pct:.1%}  "
                      f"adv_r: {idea.advisory_target_r or '?'}")
    else:
        print("\n  Ideas     : none this cycle")

    if claude_decision and claude_decision.holds:
        print(f"\n  Holds     : {', '.join(claude_decision.holds)}")

    if broker_actions:
        print(f"\n  Kernel    : {len(broker_actions)} action(s) approved:")
        for ba in broker_actions:
            tier_v = ba.tier.value if hasattr(ba.tier, "value") else str(ba.tier)
            print(f"    → {ba.action.value.upper()} {ba.symbol} [{tier_v}] "
                  f"qty={ba.qty}  stop=${ba.stop_loss}  target=${ba.take_profit}")

    if notes:
        print(f"\n  Notes     : {notes}")
    if claude_decision and claude_decision.concerns:
        print(f"  Concerns  : {claude_decision.concerns}")
    print("=" * 62)

    # 6. Pre-execution filters: fundamental check + bull/bear debate
    debate_results: dict = {}
    if actions and regime != "halt":
        buy_candidates = [a for a in actions if a.get("action") == "buy"]

        # Fundamental check — flag concerns but don't auto-veto (Claude already saw the data)
        fund_results = fundamental_check(buy_candidates, md)
        for sym, fr in fund_results.items():
            if not fr.get("ok", True):
                log.warning("[FUNDAMENTAL] Concern for %s: %s", sym, fr.get("notes", ""))

        # Bull/Bear debate — may veto individual buys
        vetoed_syms: set[str] = set()
        debate_results: dict = {}
        for a in buy_candidates:
            debate = debate_trade(a, md, equity, session_tier)
            debate_results[a.get("symbol", "")] = debate
            if not debate.get("proceed", True):
                vetoed_syms.add(a.get("symbol", ""))
                log.info("[DEBATE] Removing vetoed action: %s %s",
                         a.get("action"), a.get("symbol"))
                # Publish interesting skip for debate vetoes
                if publisher and publisher.enabled:
                    try:
                        publisher.publish_interesting_skip(
                            a, "debate_veto", debate
                        )
                    except Exception:
                        pass

        if vetoed_syms:
            actions = [a for a in actions if a.get("symbol") not in vetoed_syms
                       or a.get("action") != "buy"]
            log.info("[DEBATE] %d action(s) vetoed, %d remaining",
                     len(vetoed_syms), len(actions))

    # 7. Execute
    if actions and regime != "halt":
        results = order_executor.execute_all(
            actions=actions,
            account=account,
            positions=positions,
            market_status=md["market_status"],
            minutes_since_open=md["minutes_since_open"],
            current_prices=md.get("current_prices", {}),
            session_tier=session_tier,
        )
        print("\n" + "=" * 62)
        print("  EXECUTION RESULTS")
        print("=" * 62)
        for r in results:
            print(r)
            if r.status == "submitted":
                _send_sms(
                    f"BOT ORDER: {r.action.upper()} {r.symbol}  "
                    f"order_id={r.order_id}"
                )
                # Attribution — log order submitted event
                try:
                    from attribution import log_attribution_event as _log_attr  # noqa: PLC0415
                    _log_attr(
                        event_type="order_submitted",
                        decision_id=_decision_id,
                        account="A1",
                        symbol=r.symbol,
                        module_tags=_module_tags,
                        trigger_flags=_trigger_flags,
                        trade_id=str(r.order_id) if r.order_id else None,
                    )
                except Exception as _oa_exc:
                    log.debug("Attribution order_submitted failed (non-fatal): %s", _oa_exc)
                # Publish trade entry to Twitter
                if publisher and publisher.enabled:
                    try:
                        matching_action = next(
                            (a for a in actions if a.get("symbol") == r.symbol
                             and a.get("action") == r.action),
                            None,
                        )
                        if matching_action:
                            publisher.publish_trade_entry(
                                action=matching_action,
                                debate_result=debate_results.get(r.symbol),
                                market_context=f"VIX={md['vix']:.1f} regime={regime}",
                                alpaca_client=alpaca,  # verify fill + use real entry price
                            )
                    except Exception as _pub_exc:
                        log.debug("publisher trade_entry failed (non-fatal): %s", _pub_exc)
        print("=" * 62)

        # Seed backstop for each new buy executed (via reconciliation module)
        try:
            _cfg_path = Path(__file__).parent / 'strategy_config.json'
            _sc = json.loads(_cfg_path.read_text()) if _cfg_path.exists() else {}
            _backstop_days = int(
                _sc.get('exit_management', {}).get('backstop_days', 7)
            )
            for _r in results:
                if _r.status != 'submitted' or _r.action != 'buy':
                    continue
                recon.seed_backstop(
                    symbol=normalize_symbol(_r.symbol),
                    config_path=_cfg_path,
                    max_hold_days=_backstop_days,
                )
        except Exception as _tba_exc:
            log.warning('Backstop seeding failed: %s', _tba_exc)

    else:
        log.info("Execute  no actions this cycle")

    elapsed = time.monotonic() - t_start
    log.info("── Cycle done in %.1fs ─────────────────────────────────", elapsed)


if __name__ == "__main__":
    run_cycle()
