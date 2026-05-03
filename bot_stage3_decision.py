"""
bot_stage3_decision.py — Stage 3: prompt builders, Claude callers, decision capture.

Public API:
  _load_prompts()                   -> tuple[str, str]
  _load_strategy_config()           -> str
  build_user_prompt(...)            -> str
  build_compact_prompt(...)         -> str
  _load_compact_template()          -> str
  _log_skip_cycle(state)            -> None
  ask_claude(user_prompt)           -> dict
  _ask_claude_overnight(...)        -> dict
  _write_decision_capture(...)      -> None
  _legacy_action_to_intent(action)  -> str
"""

import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import portfolio_intelligence as pi
from bot_clients import MODEL, MODEL_FAST, _get_claude
from log_setup import get_logger

log = get_logger(__name__)

PROMPTS_DIR   = Path(__file__).parent / "prompts"
_CAPTURES_DIR = Path(__file__).parent / "data" / "captures"

_ET = ZoneInfo("America/New_York")
_TRADING_WINDOW_START_DEFAULT = 9 * 60 + 25    # 9:25 AM ET
_TRADING_WINDOW_END_DEFAULT   = 16 * 60 + 15   # 4:15 PM ET


def is_claude_trading_window(now_et: datetime | None = None,
                             cfg: dict | None = None) -> bool:
    """
    Returns True only during 9:25 AM–4:15 PM ET on weekdays — the window
    when Stage 3 Sonnet trading-decision calls are authorized.

    When `feature_flags.hard_gate_claude_to_trading_window` is False the
    gate is disabled and this function always returns True.

    Window boundaries are read from `feature_flags.trading_window_start_et`
    and `trading_window_end_et` ("HH:MM"); defaults are 09:25 / 16:15.
    """
    flags = (cfg or {}).get("feature_flags", {}) if isinstance(cfg, dict) else {}
    if not flags.get("hard_gate_claude_to_trading_window", True):
        return True

    if now_et is None:
        now_et = datetime.now(_ET)
    if now_et.weekday() >= 5:
        return False

    def _parse_hhmm(s: str, fallback: int) -> int:
        try:
            h, m = s.split(":")
            return int(h) * 60 + int(m)
        except Exception:
            return fallback

    start_min = _parse_hhmm(flags.get("trading_window_start_et", ""), _TRADING_WINDOW_START_DEFAULT)
    end_min   = _parse_hhmm(flags.get("trading_window_end_et",   ""), _TRADING_WINDOW_END_DEFAULT)
    now_min   = now_et.hour * 60 + now_et.minute
    return start_min <= now_min <= end_min

_OVERNIGHT_SYS = (
    "You are a crypto trading manager for an overnight session. "
    "Only BTC/USD and ETH/USD are tradeable. JSON only, no markdown.\n"
    "TWO modes:\n"
    "  1. MANAGE OPEN POSITIONS: For each open crypto position, decide hold or close.\n"
    "  2. NEW ENTRY (only if no positions open and conviction >= 0.70 and regime_view=normal):\n"
    "     Entry criteria: RSI 40-65, trend aligned with MA20 and EMA9/EMA21, "
    "clear catalyst, not overbought. "
    "Conservative sizing: tier='dynamic' (not core). "
    "Stop >= 8% below entry. Target >= 16% above entry (2R minimum). "
    "Do NOT enter if BTC/USD or ETH/USD position already in holds[] or open positions.\n"
    "Output schema:\n"
    '{"reasoning":"<1 sentence>","regime_view":"normal"|"caution"|"halt",'
    '"ideas":['
    '{"intent":"close"|"enter_long",'
    '"symbol":"BTC/USD"|"ETH/USD",'
    '"conviction":0.75,'
    '"tier":"dynamic"|"core",'
    '"stop_loss_pct":0.09,'
    '"take_profit_pct":0.18,'
    '"catalyst":"<required — specific technical reason>",'
    '"direction":"bullish"|"neutral",'
    '"concerns":""}'
    "],"
    '"holds":["<symbol to hold unchanged>"],'
    '"notes":"","concerns":""}'
    "\n"
    "close: exit a held position. "
    "enter_long: new buy (include stop_loss_pct and take_profit_pct). "
    "holds[]: symbols to keep without action. "
    "If in doubt or macro is uncertain, hold cash — return empty ideas[] and holds[]."
)

_OVERNIGHT_DEFAULT: dict = {
    "reasoning": "Overnight default — hold all positions.",
    "regime_view": "normal",
    "ideas": [],
    "holds": [],
    "notes": "",
    "concerns": "",
}

_compact_template_cache: str = ""
_system_prompt_cache:    str = ""  # loaded once; identical bytes every call = cache hits


def _load_prompts() -> tuple[str, str]:
    """
    Return (system_prompt, user_template).

    system_v1.txt is cached at module level after the first read — the bytes must be
    byte-for-byte identical on every call for Anthropic prompt caching to activate.
    user_template_v1.txt is read fresh each call so hot-edits take effect without restart.

    IMPORTANT: never inject dynamic content (timestamps, equity values, prices) into the
    system prompt.  All cycle-specific data belongs in the user message only.
    """
    global _system_prompt_cache
    if not _system_prompt_cache:
        _system_prompt_cache = (PROMPTS_DIR / "system_v1.txt").read_text().strip()
    template = (PROMPTS_DIR / "user_template_v1.txt").read_text().strip()
    return _system_prompt_cache, template


def _write_decision_capture(
    decision_id: str,
    system_prompt: str,
    user_prompt: str,
    model: str,
    raw_response: str,
    broker_actions: list,
) -> None:
    """Write a decision capture artifact for the replay harness. Non-fatal."""
    try:
        _CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timezone
        record = {
            "schema_version": 1,
            "decision_id":    decision_id,
            "timestamp":      datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "model":          model,
            "system_prompt":  system_prompt,
            "user_prompt":    user_prompt,
            "raw_response":   raw_response,
            "broker_actions": broker_actions,
        }
        _path = _CAPTURES_DIR / f"{decision_id}.json"
        _tmp  = _path.with_suffix(".tmp")
        _tmp.write_text(json.dumps(record, indent=2))
        os.replace(_tmp, _path)
    except Exception as _cap_exc:
        log.debug("[CAPTURE] non-fatal write failure: %s", _cap_exc)


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
        cfg = json.loads(path.read_text())

        # T-017: sector_rotation_bias_expiry — auto-revert to neutral in memory if past.
        # Non-destructive: does not write back to strategy_config.json.
        # The weekly review Strategy Director is the correct agent to formally reset.
        _params = cfg.get("parameters", {})
        _bias_expiry = _params.get("sector_rotation_bias_expiry")
        if _bias_expiry:
            try:
                if datetime.now().date() > datetime.fromisoformat(_bias_expiry).date():
                    log.warning(
                        "[CONFIG] sector_rotation_bias_expiry %s has passed — "
                        "treating bias as neutral for this cycle",
                        _bias_expiry,
                    )
                    cfg = dict(cfg)
                    cfg["parameters"] = dict(_params)
                    cfg["parameters"]["sector_rotation_bias"] = "neutral"
            except Exception as _exp_exc:
                log.debug("[CONFIG] sector_rotation_bias_expiry parse failed (non-fatal): %s", _exp_exc)

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
        tba = cfg.get("time_bound_actions", [])
        if tba:
            lines.append("")
            lines.append("=== TIME-BOUND ACTIONS (MANDATORY) ===")
            for item in tba:
                lines.append(
                    f"  ⚠ {item.get('priority', 'HIGH')}: "
                    f"{item.get('action', 'exit').upper()} {item.get('symbol', '?')} "
                    f"by {item.get('deadline_et', '?')} ET"
                )
                lines.append(f"    Reason: {item.get('reason', '')}")
        return "\n".join(lines)
    except Exception:
        return "  (strategy_config.json not yet generated — using system prompt defaults)"


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
    intraday_momentum:    str = "",
    exit_status:          str = "",
    macro_backdrop:       str = "",
    scratchpad_section:   str = "",
    allocator_section:    str = "",
    signal_scores_raw:    dict = None,
    scratchpad_raw:       dict = None,
) -> str:
    equity        = float(account.equity)
    cash          = float(account.cash)
    buying_power  = float(account.buying_power)
    pdt_used      = int(getattr(account, "daytrade_count", 0) or 0)
    pdt_remaining = max(0, 3 - pdt_used)
    log.info("PDT      daytrade_count=%d  used=%d  (PDT limit N/A — equity $%.0f above $25K threshold)",
             pdt_used, pdt_used, equity)

    long_value        = sum(float(p.market_value) for p in positions if float(p.qty) > 0)
    _total_cap        = long_value + buying_power
    exposure_pct      = (long_value / _total_cap * 100) if _total_cap > 0 else ((long_value / equity * 100) if equity > 0 else 0.0)
    available_for_new = buying_power

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

    _pi = pi_data or {}
    dyn_sizes_sec  = "  (portfolio intelligence unavailable)"
    pos_health_sec = "  (portfolio intelligence unavailable)"
    corr_sec       = "  (portfolio intelligence unavailable)"
    thesis_sec     = "  (portfolio intelligence unavailable)"
    try:
        if _pi:
            open_syms      = [p.symbol for p in positions if float(p.qty) > 0]
            dyn_sizes_sec  = pi.format_dynamic_sizes_section(_pi.get("sizes", {}), equity)
            pos_health_sec = pi.format_positions_with_health(positions, equity, buying_power=buying_power)
            corr_sec       = pi.format_correlation_section(_pi.get("correlation", {}), open_syms)
            thesis_sec     = pi.format_thesis_ranking_section(
                _pi.get("thesis_scores", []), _pi.get("weakest_symbol"))
    except Exception as _pi_exc:
        log.debug("PI section formatting failed (non-fatal): %s", _pi_exc)

    # C4: Extended session omits market-hours-only sections to reduce prompt size.
    _is_extended   = (session_tier == "extended")
    _sector_table  = "  (not shown — extended session)" if _is_extended else md.get("sector_table", "  (not available)")

    # Build reconciled conviction table from three sources (non-fatal)
    _conviction_table  = "  (conviction brief not yet generated)"
    _regime_line       = ""
    _positions_line    = ""
    _avoid_line        = ""
    if not _is_extended:
        try:
            from morning_brief import (  # noqa: PLC0415
                build_conviction_reconciliation,
                load_sonnet_brief,
            )
            _sb = load_sonnet_brief()
            if _sb:
                _regime_line    = _sb.get("regime_line", "")
                _positions_line = _sb.get("positions_line", "")
                _avoid_line     = _sb.get("avoid_line", "")
            _conviction_table = build_conviction_reconciliation(
                sonnet_brief=_sb,
                signal_scores=signal_scores_raw,
                scratchpad=scratchpad_raw,
            )
        except Exception as _sb_exc:
            log.debug("[BRIEF] conviction reconciliation failed (non-fatal): %s", _sb_exc)
    else:
        _conviction_table = "  (not shown — extended session)"
    _orb_section   = "  (not shown — extended session)" if _is_extended else md.get("orb_section", "  No ORB candidates identified for today.")
    _intraday_mom  = "  (not shown — extended session)" if _is_extended else (intraday_momentum or "  (unavailable)")
    _sector_news   = "  (not shown — extended session)" if _is_extended else md.get("sector_news", "  (none)")

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
    rendered = user_template.format(
        session_tier=session_tier,
        session_instruments=session_instruments,
        next_cycle_time=next_cycle_time,
        equity=f"{equity:,.2f}",
        cash=f"{cash:,.2f}",
        buying_power=f"{buying_power:,.2f}",
        available_for_new=f"{available_for_new:,.0f}",
        pdt_used=pdt_used,
        pdt_remaining=pdt_remaining,
        exposure_pct=exposure_pct,
        vix=md.get("vix", 20.0),
        vix_regime=md.get("vix_regime", str(md.get("vix", 20.0))),
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
        conviction_table=_conviction_table,
        regime_line=_regime_line,
        positions_line=_positions_line,
        avoid_line=_avoid_line,
        insider_section=md.get("insider_section", "  (insider intelligence unavailable)"),
        reddit_section=md.get("reddit_section", "  (Reddit sentiment unavailable)"),
        earnings_intel_section=md.get("earnings_intel_section", "  (no symbols near earnings)"),
        economic_calendar_section=md.get("economic_calendar_section", "  (economic calendar unavailable)"),
        macro_wire_section=md.get("macro_wire_section", "  No significant macro headlines in the past 4 hours."),
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
    # Inject allocator shadow section if provided (advisory only; appended after template render).
    # This avoids modifying user_template_v1.txt for backward compatibility with cached prompts.
    if allocator_section and allocator_section.strip():
        rendered += "\n\n" + allocator_section
    return rendered


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
    condensed_memories: str = "",
) -> str:
    """
    Build the compact 6-block prompt (~1,500 tokens vs ~7,500 for full).
    Used for low-information cycles where no material state change occurred.
    """
    template = _load_compact_template()
    if not template:
        log.warning("[GATE] compact_template.txt missing — falling back to full prompt indicator")
        return ""

    equity        = float(account.equity)
    cash          = float(account.cash)
    buying_power  = float(account.buying_power)
    pdt_used      = int(getattr(account, "daytrade_count", 0) or 0)
    pdt_remaining = max(0, 3 - pdt_used)
    long_val          = sum(float(p.market_value) for p in positions if float(p.qty) > 0)
    _total_cap        = long_val + buying_power
    exposure_pct      = (long_val / _total_cap * 100) if _total_cap > 0 else ((long_val / equity * 100) if equity > 0 else 0.0)
    cash_pct          = (cash / equity * 100) if equity > 0 else 0.0
    available_for_new = buying_power
    vix           = float(md.get("vix", 0) or 0)

    _pi          = pi_data or {}
    drawdown_pct = float(_pi.get("drawdown_pct", 0) or 0)

    if vix > 35:
        vix_label = "HALT"
    elif vix > 25:
        vix_label = "elevated"
    elif vix < 15:
        vix_label = "calm"
    else:
        vix_label = "normal"

    if positions:
        rows = []
        for p in positions:
            sym   = p.symbol
            qty   = float(p.qty)
            entry = float(p.avg_entry_price)
            cur   = float(p.current_price)
            pnl   = float(p.unrealized_pl)
            sign  = "+" if pnl >= 0 else ""
            rows.append(
                f"  {sym:<8} qty={qty:.4f}  entry=${entry:.2f}  "
                f"now=${cur:.2f}  P&L={sign}${pnl:.2f}"
            )
            if exit_status and sym in exit_status:
                for line in exit_status.splitlines():
                    if sym in line and ("stop" in line.lower() or "target" in line.lower()):
                        rows.append("    " + line.strip())
                        break
        positions_block = "\n".join(rows)
    else:
        positions_block = "  No open positions."

    regime_bias  = regime_obj.get("bias", "unknown")
    regime_score = regime_obj.get("regime_score", 50)
    constraints  = regime_obj.get("constraints", [])
    hi_warning   = regime_obj.get("high_impact_warning", "")

    sector_lines = [
        l.strip() for l in (md.get("sector_table", "") or "").splitlines()
        if ("▲" in l or "▼" in l) and l.strip()
    ]
    top_sectors = "  ".join(sector_lines[:2]) if sector_lines else "unavailable"

    if hi_warning:
        macro_constraint = hi_warning
    elif constraints:
        macro_constraint = constraints[0]
    else:
        macro_constraint = "No hard macro constraint"

    breaking     = (md.get("breaking_news", "") or "").strip()
    top_catalyst = breaking[:120] if breaking and breaking not in ("(none)", "(extended/overnight — not fetched)") else "No fresh catalyst last 15 min"

    scored   = signal_scores_obj.get("scored_symbols", {}) if signal_scores_obj else {}
    n_scored = len(scored)
    if scored:
        top5      = sorted(scored.items(), key=lambda kv: float(kv[1].get("score", 0)) if isinstance(kv[1], dict) else 0, reverse=True)[:5]
        sig_lines = []
        for sym, data in top5:
            if not isinstance(data, dict):
                continue
            sc        = data.get("score", 0)
            dirn      = data.get("direction", "neutral")
            cat       = data.get("catalyst", "")[:80]
            sigs      = data.get("signals", "")[:60]
            price     = data.get("price", 0)
            price_str = f"  ${price:.2f}" if price else ""
            sig_lines.append(
                f"  {sym}: score={sc:.0f} direction={dirn}{price_str}\n"
                f"    catalyst: {cat}\n"
                f"    signals: {sigs}"
            )
        top_signals_block = "\n".join(sig_lines) if sig_lines else "  No signals scored this cycle."
    else:
        top_signals_block = "  No signals scored this cycle."

    clines = []
    if vix >= 35:
        clines.append("HALT: VIX >= 35 — no new positions")
    elif vix >= 25:
        clines.append(f"VIX {vix:.1f} >= 25 — reduce all sizes 50%")
    for tba in (time_bound_actions or []):
        sym    = tba.get("symbol", "")
        reason = tba.get("reason", "time-bound exit")
        et     = tba.get("deadline_et", tba.get("exit_by", ""))
        clines.append(f"DEADLINE EXIT: {sym} by {et} — {reason}")
    if constraints:
        clines.extend(constraints[:2])
    constraints_block = "\n".join(f"  {l}" for l in clines) if clines else "  No active constraints."

    try:
        rendered = template.format(
            equity=f"{equity:,.2f}",
            cash_pct=f"{cash_pct:.1f}",
            exposure_pct=f"{exposure_pct:.1f}",
            buying_power=f"{buying_power:,.2f}",
            available_for_new=f"{available_for_new:,.0f}",
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

    if condensed_memories:
        mem_block = f"=== RELEVANT MEMORIES ===\n{condensed_memories[:600]}\n\n"
        if "=== OUTPUT" in rendered:
            rendered = rendered.replace("=== OUTPUT", mem_block + "=== OUTPUT", 1)
        else:
            rendered += "\n\n" + mem_block

    return rendered


def _log_skip_cycle(state) -> None:
    log.info(
        "[GATE] SKIP consecutive=%d  skips_today=%d  sonnet_today=%d",
        state.consecutive_skips,
        state.total_skips_today,
        state.total_calls_today,
    )


def _repair_truncated_json(raw: str) -> dict | None:
    """
    If Claude's response was token-truncated mid-string in the trailing 'notes'
    field, drop the incomplete field and close the JSON so the trade ideas
    (which always precede 'notes') are still usable.
    """
    import re
    match = re.search(r',\s*"notes"\s*:', raw)
    if match:
        candidate = raw[: match.start()] + "\n}"
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    return None


def ask_claude(user_prompt: str) -> dict:
    system_prompt, _ = _load_prompts()
    response = _get_claude().messages.create(
        model=MODEL,
        max_tokens=4096,
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
    try:
        from cost_attribution import log_claude_call_to_spine
        log_claude_call_to_spine("bot_stage3_decision", MODEL, "decision", usage)
    except Exception:
        pass
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        raw = raw.rsplit("```", 1)[0].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        if "Unterminated string" in str(exc):
            repaired = _repair_truncated_json(raw)
            if repaired is not None:
                log.warning("ask_claude: JSON truncated at token limit — repaired (notes dropped)")
                return repaired
        raise ValueError(f"Claude returned non-JSON:\n{raw}") from exc


def _ask_claude_overnight(
    positions: list,
    crypto_context: str,
    regime_obj: dict,
    macro_wire: str,
    crypto_signals: str = "",
    equity: float = 0.0,
    buying_power: float = 0.0,
    condensed_memories: str = "",
) -> dict:
    """
    Lightweight Haiku decision for overnight crypto-only sessions.
    Falls back to hold-all default on any error, never raises.

    Supports two modes:
      - Manage existing crypto positions (hold or close)
      - New entry when no positions open and conviction >= 0.70

    C3: saves ~$0.042/cycle × 24 overnight cycles/day ≈ $1.01/day vs Sonnet.
    """
    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _ZI
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

        # Extract current prices from crypto_signals for stop/target computation
        import re as _re
        _crypto_prices: dict[str, float] = {}
        try:
            for _sym in ("BTC/USD", "ETH/USD"):
                _m = _re.search(
                    rf"{_re.escape(_sym)}\s+\$([0-9,]+\.?[0-9]*)",
                    crypto_signals or "",
                )
                if _m:
                    _crypto_prices[_sym] = float(_m.group(1).replace(",", ""))
        except Exception:
            pass

        _mem_block = (
            f"=== RELEVANT MEMORY ===\n{condensed_memories[:350]}\n\n"
            if condensed_memories else ""
        )
        prompt = (
            f"=== OVERNIGHT SESSION ===\n"
            f"Time: {_time_str}\n\n"
            f"=== OPEN POSITIONS ===\n{pos_block}\n\n"
            f"=== REGIME ===\n"
            f"Score: {regime_score}  Bias: {bias}\n\n"
            f"{_mem_block}"
            f"=== CRYPTO SIGNALS (prices + technicals) ===\n"
            f"{crypto_signals or '  (unavailable)'}\n\n"
            f"=== ACCOUNT ===\n"
            f"Equity: ${equity:,.0f}  Buying power: ${buying_power:,.0f}\n\n"
            f"=== CRYPTO CONTEXT ===\n{crypto_context or '  (unavailable)'}\n\n"
            f"=== MACRO WIRE (top items) ===\n{macro_wire or '  (none)'}\n\n"
            "=== TASK ===\n"
            "Overnight session. Only BTC/USD and ETH/USD are tradeable.\n"
            "For each open crypto position: hold or close.\n"
            "If no positions open: you MAY enter BTC/USD or ETH/USD if conviction >= 0.70,\n"
            "  regime_view is 'normal', RSI is 40-65, and a clear catalyst exists.\n"
            "  Use intent='enter_long'. Include stop_loss_pct (>= 0.08) and take_profit_pct (>= 0.16).\n"
            "  Do NOT enter if a position in that symbol is already open.\n"
            "Never fabricate catalysts. If conviction is below 0.70 or setup is ambiguous, hold cash."
        )

        response = _get_claude().messages.create(
            model=MODEL_FAST,
            max_tokens=700,
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
        try:
            from cost_attribution import log_claude_call_to_spine
            log_claude_call_to_spine("bot_stage3_decision", MODEL_FAST, "overnight_decision",
                                     response.usage)
        except Exception:
            pass

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        result = json.loads(raw)

        # Post-process enter_long ideas: convert pct fields to absolute prices,
        # enforce overnight tier cap (core → dynamic).
        _processed: list = []
        for _idea in result.get("ideas", []):
            if _idea.get("intent") == "enter_long":
                _sym       = _idea.get("symbol", "")
                _cur_price = _crypto_prices.get(_sym, 0.0)
                _sl_pct    = float(_idea.get("stop_loss_pct",   0.09))
                _tp_pct    = float(_idea.get("take_profit_pct", 0.18))
                if _cur_price > 0:
                    _idea["stop_loss"]   = round(_cur_price * (1 - _sl_pct), 2)
                    _idea["take_profit"] = round(_cur_price * (1 + _tp_pct), 2)
                    _idea["entry_price"] = _cur_price
                if _idea.get("tier") == "core":
                    _idea["tier"] = "dynamic"
                    log.info("[OVERNIGHT] enter_long tier capped core→dynamic for %s", _sym)
            _processed.append(_idea)
        result["ideas"] = _processed

        log.info("[OVERNIGHT] Haiku decision: regime=%s  ideas=%d",
                 result.get("regime_view", "?"), len(result.get("ideas", [])))
        return result

    except Exception as _exc:
        log.warning("[OVERNIGHT] _ask_claude_overnight failed (%s) — using hold-all default", _exc)
        return _OVERNIGHT_DEFAULT
