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

import portfolio_intelligence as pi
from bot_clients import MODEL, MODEL_FAST, _get_claude
from log_setup import get_logger

log = get_logger(__name__)

PROMPTS_DIR   = Path(__file__).parent / "prompts"
_CAPTURES_DIR = Path(__file__).parent / "data" / "captures"

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

    _pi = pi_data or {}
    dyn_sizes_sec  = "  (portfolio intelligence unavailable)"
    pos_health_sec = "  (portfolio intelligence unavailable)"
    corr_sec       = "  (portfolio intelligence unavailable)"
    thesis_sec     = "  (portfolio intelligence unavailable)"
    try:
        if _pi:
            open_syms      = [p.symbol for p in positions if float(p.qty) > 0]
            dyn_sizes_sec  = pi.format_dynamic_sizes_section(_pi.get("sizes", {}), equity)
            pos_health_sec = pi.format_positions_with_health(positions, equity)
            corr_sec       = pi.format_correlation_section(_pi.get("correlation", {}), open_syms)
            thesis_sec     = pi.format_thesis_ranking_section(
                _pi.get("thesis_scores", []), _pi.get("weakest_symbol"))
    except Exception as _pi_exc:
        log.debug("PI section formatting failed (non-fatal): %s", _pi_exc)

    # C4: Extended session omits market-hours-only sections to reduce prompt size.
    _is_extended   = (session_tier == "extended")
    _sector_table  = "  (not shown — extended session)" if _is_extended else md.get("sector_table", "  (not available)")
    _morning_brief = "  (not shown — extended session)" if _is_extended else md.get("morning_brief_section", "  (morning brief not yet generated)")
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
    long_val      = sum(float(p.market_value) for p in positions if float(p.qty) > 0)
    exposure_pct  = (long_val / equity * 100) if equity > 0 else 0.0
    cash_pct      = (cash / equity * 100) if equity > 0 else 0.0
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
    if pdt_remaining == 0:
        clines.append("PDT: 0 day trades remaining — no new stock/ETF entries")
    for tba in (time_bound_actions or []):
        sym    = tba.get("symbol", "")
        reason = tba.get("reason", "time-bound exit")
        et     = tba.get("deadline_et", tba.get("exit_by", ""))
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
    response = _get_claude().messages.create(
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
        raise ValueError(f"Claude returned non-JSON:\n{raw}") from exc


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

        response = _get_claude().messages.create(
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
        log.info("[OVERNIGHT] Haiku decision: regime=%s  actions=%d",
                 result.get("regime", "?"), len(result.get("actions", [])))
        return result

    except Exception as _exc:
        log.warning("[OVERNIGHT] _ask_claude_overnight failed (%s) — using hold-all default", _exc)
        return _OVERNIGHT_DEFAULT
