"""
Trading bot — main entry point.
Run directly for a single cycle:  python bot.py
Run via scheduler for 24/7 mode:  python scheduler.py
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv as _load_dotenv_bot
except ImportError:
    _load_dotenv_bot = None  # type: ignore

import memory as mem
import order_executor
import risk_kernel
import scratchpad as _scratchpad
import sonnet_gate as _gate
import trade_memory
import watchlist_manager as wm
from bot_clients import (  # noqa: F401 (_get_claude re-exported for callers)
    MODEL,
    _get_alpaca,
    _get_claude,
)
from bot_stage0_precycle import run_precycle
from bot_stage1_regime import classify_regime, format_regime_summary
from bot_stage2_5_scratchpad import run_scratchpad_stage
from bot_stage2_signal import format_signal_scores
from bot_stage2_signal import score_signals_layered as score_signals
from bot_stage3_decision import (
    _OVERNIGHT_DEFAULT,  # re-exported — test_core.py accesses bot._OVERNIGHT_DEFAULT  # noqa: F401
    _ask_claude_overnight,  # re-exported — test_core.py accesses bot._ask_claude_overnight
    _log_skip_cycle,
    _write_decision_capture,
    ask_claude,  # re-exported — test_core.py mocks bot.ask_claude
    build_compact_prompt,
    build_user_prompt,
    is_claude_trading_window,
)
from bot_stage4_execution import debate_trade, fundamental_check
from log_setup import get_logger, log_trade
from portfolio_allocator import format_allocator_section as _format_allocator_section
from schemas import (
    BrokerAction as _BrokerAction,
)
from schemas import (
    BrokerSnapshot as _BrokerSnapshot,
)
from schemas import (
    NormalizedPosition as _NP,
)
from schemas import (
    normalize_symbol,
    validate_claude_decision,
)

# trade_publisher is optional — import failure must never break the bot
try:
    from trade_publisher import TradePublisher
    publisher = TradePublisher()
except Exception:
    publisher = None  # type: ignore

if _load_dotenv_bot:
    _load_dotenv_bot()

log = get_logger(__name__)

# ── Twilio SMS ────────────────────────────────────────────────────────────────

def _send_sms(message: str) -> None:
    # TWILIO_FROM_NUMBER (+66 Thai number) caused error 21659 (country mismatch).
    # Route through WhatsApp instead — WHATSAPP_FROM/WHATSAPP_TO are confirmed working.
    sid   = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    from_ = os.getenv("WHATSAPP_FROM")
    to    = os.getenv("WHATSAPP_TO")

    if not all([sid, token, from_, to]):
        log.warning("Twilio not configured — alert skipped: %s", message)
        return

    try:
        from twilio.rest import Client
        Client(sid, token).messages.create(body=message, from_=from_, to=to)
        log.info("WhatsApp alert sent: %s", message)
    except Exception as exc:
        log.error("WhatsApp alert failed: %s", exc)


from notifications import build_order_email_html as _build_order_email_html


def _send_email_alert(subject: str, body: str) -> None:
    """Send an alert email via SendGrid. No-op if not configured. Non-fatal."""
    api_key    = os.getenv("SENDGRID_API_KEY")
    from_email = os.getenv("SENDGRID_FROM_EMAIL", "eugene.gold@gmail.com")
    to_email   = "eugene.gold@gmail.com"
    if not api_key or api_key.startswith("your_"):
        log.warning("SENDGRID_API_KEY not configured — email alert skipped: %s", subject)
        return
    if body.lstrip().startswith("<"):
        html = body
    else:
        html = (
            "<html><body style='font-family:Arial,sans-serif;max-width:700px'>"
            f"<pre style='white-space:pre-wrap'>{body}</pre></body></html>"
        )
    try:
        from sendgrid import SendGridAPIClient  # noqa: PLC0415
        from sendgrid.helpers.mail import Mail  # noqa: PLC0415
        resp = SendGridAPIClient(api_key).send(
            Mail(from_email=from_email, to_emails=to_email,
                 subject=subject, html_content=html)
        )
        log.info("Alert email sent — status=%d  subject=%s", resp.status_code, subject)
    except Exception as exc:
        log.error("Alert email failed: %s", exc)


# ── Drawdown guard ────────────────────────────────────────────────────────────
_DRAWDOWN_THRESHOLD    = 0.20
_last_drawdown_alert   = 0.0
_peak_equity           = None
_drawdown_state_loaded = False
_DRAWDOWN_STATE_FILE   = Path("data/runtime/drawdown_state.json")


def _load_drawdown_state() -> None:
    """Load persisted peak_equity and last_drawdown_alert. Non-fatal."""
    global _peak_equity, _last_drawdown_alert, _drawdown_state_loaded
    _drawdown_state_loaded = True
    if not _DRAWDOWN_STATE_FILE.exists():
        return
    try:
        data = json.loads(_DRAWDOWN_STATE_FILE.read_text())
        if data.get("peak_equity") is not None:
            _peak_equity = float(data["peak_equity"])
        if data.get("last_drawdown_alert") is not None:
            _last_drawdown_alert = data["last_drawdown_alert"]
        log.info("[DRAWDOWN] Loaded persisted state: peak=$%s  last_alert=%s",
                 f"{_peak_equity:,.0f}" if _peak_equity is not None else "None",
                 _last_drawdown_alert)
    except Exception as exc:
        log.warning("[DRAWDOWN] Failed to load persisted state (%s) — starting fresh", exc)


def _save_drawdown_state() -> None:
    """Atomically persist peak_equity and last_drawdown_alert. Non-fatal."""
    try:
        _DRAWDOWN_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _DRAWDOWN_STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "generated_at":        datetime.now(timezone.utc).isoformat(),
            "peak_equity":         _peak_equity,
            "last_drawdown_alert": _last_drawdown_alert,
        }, indent=2))
        os.replace(tmp, _DRAWDOWN_STATE_FILE)
    except Exception as exc:
        log.warning("[DRAWDOWN] Failed to save state: %s", exc)


def _check_drawdown(equity: float) -> bool:
    global _peak_equity, _last_drawdown_alert, _drawdown_state_loaded

    if not _drawdown_state_loaded:
        _load_drawdown_state()

    if _peak_equity is None or equity > _peak_equity:
        _peak_equity = equity
        _save_drawdown_state()

    drawdown = (_peak_equity - equity) / _peak_equity

    if drawdown >= _DRAWDOWN_THRESHOLD and equity != _last_drawdown_alert:
        _last_drawdown_alert = equity
        _save_drawdown_state()
        msg = (f"TRADING BOT ALERT: 20% drawdown triggered. "
               f"Peak equity ${_peak_equity:,.0f} → current ${equity:,.0f} "
               f"({drawdown:.1%} drawdown). Bot halting — review required.")
        log.error(msg)
        _send_sms(msg)
        _drawdown_html = (
            "<html><body style='font-family:Arial,sans-serif;max-width:700px'>"
            "<h2 style='color:#cc0000'>20% Drawdown Alert — Bot Halting</h2>"
            "<table style='border-collapse:collapse;width:100%'>"
            f"<tr><td style='padding:4px 8px'><strong>Peak equity</strong></td><td>${_peak_equity:,.0f}</td></tr>"
            f"<tr><td style='padding:4px 8px'><strong>Current equity</strong></td><td>${equity:,.0f}</td></tr>"
            f"<tr><td style='padding:4px 8px'><strong>Drawdown</strong></td><td>{drawdown:.1%}</td></tr>"
            "</table>"
            "<p>The bot has halted. Manual review required before resuming.</p>"
            "</body></html>"
        )
        _send_email_alert("BullBearBot ALERT: 20% Drawdown Triggered — Bot Halting", _drawdown_html)
        return True

    return False


# ── Cycle ─────────────────────────────────────────────────────────────────────

def run_cycle(
    session_tier:        str = "market",
    session_instruments: str = "stocks, ETFs, crypto",
    next_cycle_time:     str = "?",
    trigger_reason:      str = "",
) -> None:
    t_start = time.monotonic()
    log.info("── Cycle start  session=%s ─────────────────────────────", session_tier)

    # Early-init regime so forced/deadline exit guards never see a NameError.
    regime = "unknown"

    # Stage 0 — pre-cycle infrastructure
    state = run_precycle(session_tier, next_cycle_time, publisher=publisher)
    if state is None:
        return  # preflight verdict=halt

    # Drawdown guard — owns module-level peak_equity state, stays in bot.py
    if _check_drawdown(state.equity):
        log.error("Drawdown guard triggered — halting cycle")
        return

    # Stage 1 + 2 — regime classifier + signal scorer (market session only)
    regime_obj        = {}
    signal_scores_obj = {}
    if session_tier == "market":
        try:
            import data_warehouse as dw
            calendar = dw.load_economic_calendar()
        except Exception:
            calendar = {}
        regime_obj        = classify_regime(state.md, calendar)
        signal_scores_obj = score_signals(state.symbols_stock, regime_obj, state.md,
                                          positions=state.positions)

        # B1: inject current price into scored symbols for Account 2 handoff
        try:
            _prices = state.md.get("current_prices", {})
            _scored = signal_scores_obj.get("scored_symbols", {})
            for _sym, _sig in _scored.items():
                if isinstance(_sig, dict):
                    _price = _prices.get(_sym) or _prices.get(_sym.replace("/", ""))
                    if _price:
                        _sig["price"] = float(_price)
                    else:
                        log.debug("[SIGNALS] %s: no price in current_prices — omitted", _sym)
        except Exception as _pinj_exc:
            log.warning("[SIGNALS] price injection failed (non-fatal): %s", _pinj_exc)

        # Write signal scores to disk for Account 2 handoff (BUG-004)
        try:
            _ss_path = Path(__file__).parent / "data" / "market" / "signal_scores.json"
            _ss_path.parent.mkdir(parents=True, exist_ok=True)
            _ss_path.write_text(json.dumps(signal_scores_obj))
            log.debug("[SIGNALS] wrote %d scores to signal_scores.json",
                      len(signal_scores_obj))
        except Exception as _ss_exc:
            log.warning("[SIGNALS] could not write signal_scores.json (non-fatal): %s", _ss_exc)

    # Stage 2.5 — Haiku scratchpad pre-analysis (market session only)
    scratchpad_result = {}
    if session_tier == "market" and signal_scores_obj:
        scratchpad_result = run_scratchpad_stage(
            signal_scores_obj, regime_obj, state.md, state.positions
        )

    regime_summary_str = format_regime_summary(regime_obj)
    signal_scores_str  = format_signal_scores(signal_scores_obj)

    # Intraday momentum section (non-fatal)
    intraday_momentum_str = "  (unavailable)"
    try:
        import intraday_cache as _ic
        intraday_momentum_str = _ic.build_intraday_momentum_section(
            state.symbols_stock, state.md.get("current_prices", {})
        )
    except Exception as _im_exc:
        log.debug("Intraday momentum section failed (non-fatal): %s", _im_exc)

    # Persistent macro backdrop (cache-first, non-fatal)
    macro_backdrop_str = ""
    try:
        import macro_intelligence as _macro  # noqa: PLC0415
        macro_backdrop_str = _macro.build_macro_backdrop_section()
    except Exception as _macro_exc:
        log.debug("Macro backdrop failed (non-fatal): %s", _macro_exc)

    # Stage 3 — build prompt and call Claude
    _cap_sys = _cap_user = _cap_raw = None  # set only when Sonnet fires
    from datetime import datetime as _dt  # noqa: PLC0415
    from zoneinfo import ZoneInfo as _ZI  # noqa: PLC0415
    if session_tier == "overnight":
        decision = _ask_claude_overnight(
            positions=state.positions,
            crypto_context=state.crypto_context,
            regime_obj=regime_obj,
            macro_wire=state.md.get("macro_wire_section", ""),
        )
    else:
        # Derive regime_str from VIX + bias (classify_regime returns bias, not halt/caution)
        _vix  = float(state.md.get("vix", 0) or 0)
        _bias = regime_obj.get("bias", "neutral")
        if _vix >= 35:
            regime_str = "halt"
        elif _bias == "risk-off" and _vix > 25:
            regime_str = "caution"
        else:
            regime_str = _bias

        # Sonnet gate — skip if no material state change since last call
        _use_compact = False
        _gate_state  = _gate.load_gate_state()
        _gate_full_cfg = state.cfg if isinstance(state.cfg, dict) else {}
        _tba_list      = state.cfg.get("time_bound_actions", []) if isinstance(state.cfg, dict) else []

        # Hard trading-window gate: outside 9:25 AM–4:15 PM ET (weekdays),
        # never fire Stage 3 Sonnet. Stage 0/1/2/2.5 already ran above;
        # overnight Haiku path is unaffected (handled in the if/else above).
        _gate_reasons = []
        _now_et = _dt.now(_ZI("America/New_York"))
        if not is_claude_trading_window(now_et=_now_et, cfg=_gate_full_cfg):
            log.info("[GATE] WINDOW closed (%s ET) — Sonnet suppressed",
                     _now_et.strftime("%I:%M %p"))
            _run_sonnet = False
        else:
            _run_sonnet, _gate_reasons, _gate_state = _gate.should_run_sonnet(
                session_tier=session_tier,
                regime=regime_str,
                vix=_vix,
                signal_scores=signal_scores_obj,
                positions=state.positions,
                recon_diff=state.recon_diff,
                breaking_news=state.md.get("breaking_news", ""),
                time_bound_actions=_tba_list,
                current_time_et=_now_et,
                gate_state=_gate_state,
                config=_gate_full_cfg,
                equity=state.equity,
                buying_power=state.buying_power_float,
            )
            _gate.save_gate_state(_gate_state)

        if not _run_sonnet:
            _log_skip_cycle(_gate_state)
            decision = {
                "reasoning": "gate skipped — no material state change",
                "regime_view": regime_str,
                "ideas": [],
                "holds": [normalize_symbol(p.symbol) for p in state.positions],
                "notes": "",
                "concerns": "",
            }
        else:
            _use_compact = _gate.should_use_compact_prompt(
                _gate_reasons, state.positions, signal_scores_obj, state.recon_diff,
                trigger_reason=trigger_reason,
                config=_gate_full_cfg,
            )
            if _use_compact:
                log.info("[GATE] SONNET triggered (%s) — COMPACT",
                         ", ".join(r.value for r in _gate_reasons))
                user_prompt = build_compact_prompt(
                    account=state.account,
                    positions=state.positions,
                    md=state.md,
                    session_tier=session_tier,
                    regime_obj=regime_obj,
                    signal_scores_obj=signal_scores_obj,
                    time_bound_actions=_tba_list,
                    pi_data=state.pi_data,
                    exit_status=state.exit_status_str,
                )
            else:
                log.info("[GATE] SONNET triggered (%s) — FULL",
                         ", ".join(r.value for r in _gate_reasons))
                user_prompt = build_user_prompt(
                    account=state.account,
                    positions=state.positions,
                    md=state.md,
                    session_tier=session_tier,
                    session_instruments=session_instruments,
                    recent_decisions=state.recent_decisions,
                    ticker_lessons=state.ticker_lessons,
                    next_cycle_time=next_cycle_time,
                    vector_memories=state.vector_memories,
                    strategy_config_note=state.strategy_config_note,
                    crypto_signals=state.md.get("crypto_signals", "  (none)"),
                    crypto_context=state.crypto_context,
                    regime_summary=regime_summary_str,
                    signal_scores=signal_scores_str,
                    pi_data=state.pi_data,
                    intraday_momentum=intraday_momentum_str,
                    exit_status=state.exit_status_str,
                    macro_backdrop=macro_backdrop_str,
                    scratchpad_section=_scratchpad.format_scratchpad_section(scratchpad_result),
                    allocator_section=_format_allocator_section(state.allocator_output),
                )
            _cap_sys, _ = __import__("bot_stage3_decision")._load_prompts()
            _cap_user    = user_prompt
            decision     = ask_claude(user_prompt)
            _cap_raw     = json.dumps(decision)

    # Parse Claude's response — supports both new intent-based and legacy formats
    try:
        claude_decision = validate_claude_decision(decision)
    except Exception as _cd_exc:
        log.warning("[SCHEMA] ClaudeDecision parse failed: %s — using raw dict fallback", _cd_exc)
        claude_decision = None

    regime    = claude_decision.regime_view if claude_decision else decision.get("regime", "unknown")
    reasoning = claude_decision.reasoning   if claude_decision else decision.get("reasoning", "")
    notes     = claude_decision.notes       if claude_decision else decision.get("notes", "")

    # Attribution
    _decision_id    = ""
    _module_tags:   dict = {}
    _trigger_flags: dict = {}
    try:
        from attribution import (  # noqa: PLC0415
            build_module_tags,
            build_trigger_flags,
            generate_decision_id,
            log_attribution_event,
        )
        _decision_id = generate_decision_id(
            "A1", _dt.now(_ZI("America/New_York")).strftime("%Y%m%d_%H%M%S")
        )
        _module_tags = build_module_tags(
            session_tier=session_tier,
            gate_reasons=_gate_reasons if "_gate_reasons" in dir() else [],
            used_compact=_use_compact if "_use_compact" in dir() else False,
            gate_skipped=not _run_sonnet if "_run_sonnet" in dir() else True,
            scratchpad_result=scratchpad_result,
            retrieved_memories=state.similar_scenarios,
            macro_backdrop_str=macro_backdrop_str,
            macro_wire_str=state.md.get("macro_wire_section", ""),
            morning_brief=state.md.get("morning_brief_section", ""),
            insider_section=state.md.get("insider_section", ""),
            reddit_section=state.md.get("reddit_section", ""),
            earnings_intel=state.md.get("earnings_intel_section", ""),
            recon_diff=state.recon_diff,
            positions=state.positions,
        )
        _trigger_flags = build_trigger_flags(
            _gate_reasons if "_gate_reasons" in dir() else []
        )
        log_attribution_event(
            event_type="decision_made",
            decision_id=_decision_id,
            account="A1",
            symbol="portfolio",
            module_tags=_module_tags,
            trigger_flags=_trigger_flags,
            extra={"caller": "bot_decision"},
        )
    except Exception as _attr_exc:
        log.debug("Attribution block failed (non-fatal): %s", _attr_exc)

    # Risk kernel loop — process ideas → broker-ready actions
    broker_actions: list = []
    if claude_decision and claude_decision.ideas and regime != "halt" and state.allow_new_entries:
        _prices = state.md.get("current_prices", {})
        _rk_positions = []
        for _p in state.positions:
            try:
                _rk_positions.append(_NP.from_alpaca_position(_p))
            except Exception:
                pass
        _rk_snapshot = _BrokerSnapshot(
            equity=float(state.equity or 0),
            cash=float(state.cash or 0),
            buying_power=float(state.buying_power_float or 0),
            open_orders=[],
            positions=_rk_positions,
        )
        _rk_cfg = {}
        try:
            _rk_cfg_path = Path(__file__).parent / "strategy_config.json"
            _rk_cfg = json.loads(_rk_cfg_path.read_text()) if _rk_cfg_path.exists() else {}
        except Exception:
            pass

        # Cap Tier.CORE → Tier.DYNAMIC when signal score < 65
        risk_kernel.apply_tier_cap(claude_decision.ideas, signal_scores_obj)

        # Scratchpad soft gate: BUY on off-watching symbols requires override
        _watching_syms = set(scratchpad_result.get("watching", []))
        if _watching_syms:
            _filtered_ideas = []
            for _idea in claude_decision.ideas:
                _sym_norm = normalize_symbol(_idea.symbol)
                if (
                    _idea.action.value in ("buy", "reallocate")
                    and _sym_norm not in _watching_syms
                    and not _idea.override_scratchpad
                ):
                    log.warning(
                        "[SCRATCHPAD_GATE] REJECTED %s — not in scratchpad watching list "
                        "and no override_scratchpad flag. Watching: %s",
                        _sym_norm, sorted(_watching_syms),
                    )
                else:
                    _filtered_ideas.append(_idea)
            claude_decision.ideas[:] = _filtered_ideas

        for _idea in claude_decision.ideas:
            _sym   = normalize_symbol(_idea.symbol)
            _price = _prices.get(_sym) or _prices.get(_idea.symbol)
            _price = float(_price) if _price else None
            _result = risk_kernel.process_idea(
                _idea, _rk_snapshot, None, _rk_cfg,
                _price, session_tier, float(state.md.get("vix", 20.0))
            )
            if isinstance(_result, _BrokerAction):
                broker_actions.append(_result)
                log.info("[KERNEL] APPROVED %s %s → action=%s qty=%s stop=$%s",
                         _idea.intent, _idea.symbol, _result.action.value,
                         _result.qty, _result.stop_loss)
            else:
                log.info("[KERNEL] REJECTED %s %s — %s", _idea.intent, _idea.symbol, _result)
                try:
                    from shadow_lane import (
                        log_shadow_event as _log_shadow,  # noqa: PLC0415
                    )
                    _log_shadow(
                        "rejected_by_risk_kernel", _sym,
                        {
                            "intent":           _idea.intent,
                            "intended_action":  str(_idea.action.value) if hasattr(_idea.action, "value") else str(_idea.action),
                            "rejection_reason": str(_result),
                            "signal_score":     getattr(_idea, "signal_score", 0),
                            "conviction":       getattr(_idea, "conviction", 0.0),
                            "direction":        str(_idea.direction.value) if hasattr(_idea.direction, "value") else str(getattr(_idea, "direction", "")),
                            "thesis_summary":   getattr(_idea, "catalyst", ""),
                            "regime":           regime,
                            "vix":              float(state.md.get("vix", 0) or 0),
                            "module_tags":      _module_tags,
                        },
                        decision_id=_decision_id,
                        session=session_tier,
                    )
                except Exception:
                    pass
                try:
                    from datetime import datetime as _d
                    from datetime import timezone as _tz

                    from decision_outcomes import (  # noqa: PLC0415
                        DecisionOutcomeRecord,
                        log_outcome_event,
                    )
                    log_outcome_event(DecisionOutcomeRecord(
                        decision_id=_decision_id, account="A1", symbol=_sym,
                        timestamp=_d.now(_tz.utc).isoformat().replace("+00:00", "Z"),
                        action=str(_idea.action.value) if hasattr(_idea.action, "value") else str(_idea.action),
                        tier=str(_idea.tier.value) if hasattr(_idea.tier, "value") else str(getattr(_idea, "tier", "")),
                        confidence=str(getattr(_idea, "conviction", "")),
                        catalyst=getattr(_idea, "catalyst", None),
                        session=session_tier, status="rejected_by_kernel",
                        reject_reason=str(_result), module_tags=_module_tags,
                        trigger_flags=_trigger_flags,
                    ))
                except Exception:
                    pass

    actions = [ba.to_dict() for ba in broker_actions]

    # Decision capture
    if _decision_id and _cap_user is not None:
        _write_decision_capture(_decision_id, _cap_sys, _cap_user, MODEL, _cap_raw, actions)

    # Mode gate — filter new entries if A1 operating mode is not NORMAL
    if state.a1_mode is not None:
        try:
            from divergence import OperatingMode, is_action_allowed  # noqa: PLC0415
            if state.a1_mode.mode != OperatingMode.NORMAL:
                _filtered: list = []
                for _ba_dict in actions:
                    _allowed, _reason = is_action_allowed(
                        state.a1_mode,
                        _ba_dict.get("action", "hold"),
                        _ba_dict.get("symbol", ""),
                    )
                    if _allowed:
                        _filtered.append(_ba_dict)
                    else:
                        log.warning("[DIV] BLOCKED %s %s — %s",
                                    _ba_dict.get("action"), _ba_dict.get("symbol"), _reason)
                actions = _filtered
        except Exception as _div_gate_exc:
            log.warning("[DIV] mode gate failed (non-fatal): %s", _div_gate_exc)

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
        broker_actions = []
        _send_sms(f"TRADING BOT: Claude called HALT. Reasoning: {reasoning[:160]}")
        _halt_html = (
            "<html><body style='font-family:Arial,sans-serif;max-width:700px'>"
            "<h2 style='color:#cc6600'>Claude Returned regime=halt</h2>"
            "<p>Execution skipped this cycle. No new orders until regime clears.</p>"
            "<h3>Full Reasoning</h3>"
            f"<pre style='white-space:pre-wrap;background:#f5f5f5;padding:12px'>{reasoning}</pre>"
            "</body></html>"
        )
        _send_email_alert("BullBearBot: Claude Called HALT", _halt_html)

    # T-007: inject regime_score from Stage 1 so memory.py can persist it
    decision["regime_score"] = regime_obj.get("regime_score")

    # Persist decision
    vector_id = trade_memory.save_trade_memory(decision, state.md, session_tier)
    mem.save_decision(decision, session_tier, vector_id=vector_id, decision_id=_decision_id)

    # Pattern learning observations
    try:
        from memory import (  # noqa: PLC0415
            _load_pattern_watchlist,
            add_watchlist_observation,
        )
        pwl            = _load_pattern_watchlist()
        current_prices = state.md.get("current_prices", {})
        for sym in list(pwl.keys()):
            if pwl[sym].get("graduated"):
                continue
            price = current_prices.get(sym)
            if price is None:
                continue
            claude_mention = ""
            full_text = reasoning + " " + (notes or "")
            if sym in full_text:
                for sent in full_text.split("."):
                    if sym in sent:
                        claude_mention = sent.strip()[:120]
                        break
            lesson_text       = claude_mention or f"{sym} observed at ${price:.2f}"
            conditions_active = []
            if sym in state.md.get("watchlist_signals", ""):
                for line in state.md["watchlist_signals"].splitlines():
                    if sym in line:
                        conditions_active.append(line.strip()[:80])
                        break
            add_watchlist_observation(
                symbol=sym, price_action=f"${price:.2f} today",
                conditions=conditions_active[:3], lesson=lesson_text,
                source="auto_observation",
            )
    except Exception as _pwl_exc:
        log.debug("Pattern watchlist observation failed (non-fatal): %s", _pwl_exc)

    # Auto-enroll skipped symbols into pattern watchlist
    try:
        for action in actions:
            if action.get("action", "").lower() == "skip":
                sym = action.get("symbol", "")
                if not sym:
                    continue
                rationale = (action.get("catalyst") or action.get("rationale") or reasoning)[:300]
                mem.add_symbol_to_pattern_watchlist(
                    symbol=sym, reason=rationale,
                    market_context=f"VIX={state.md.get('vix', 20.0):.1f} regime={regime}",
                )
                log.info("[PATTERN_WL] Auto-enrolled %s — skip rationale: %s", sym, rationale[:80])
    except Exception as _skip_exc:
        log.debug("Skip auto-enroll failed (non-fatal): %s", _skip_exc)

    # Watchlist feedback loop (breaking news only)
    wm.run_feedback_loop(breaking_news_text=state.md.get("breaking_news", ""))

    # Log cycle decision
    log_trade({
        "event":       "cycle_decision",
        "session":     session_tier,
        "regime_view": regime,
        "reasoning":   reasoning,
        "notes":       notes,
        "vix":         state.md.get("vix", 20.0),
        "equity":      state.equity,
        "exposure":    round(state.exposure, 2),
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

    # Stage 4 — pre-execution filters: fundamental check + bull/bear debate
    debate_results: dict = {}
    if actions and regime != "halt":
        buy_candidates = [a for a in actions if a.get("action") == "buy"]

        fund_results = fundamental_check(buy_candidates, state.md)
        for sym, fr in fund_results.items():
            if not fr.get("ok", True):
                log.warning("[FUNDAMENTAL] Concern for %s: %s", sym, fr.get("notes", ""))

        vetoed_syms: set[str] = set()
        debate_results = {}
        for a in buy_candidates:
            debate = debate_trade(a, state.md, state.equity, session_tier)
            debate_results[a.get("symbol", "")] = debate
            if not debate.get("proceed", True):
                vetoed_syms.add(a.get("symbol", ""))
                log.info("[DEBATE] Removing vetoed action: %s %s",
                         a.get("action"), a.get("symbol"))
                if publisher and publisher.enabled:
                    try:
                        publisher.publish_interesting_skip(a, "debate_veto", debate)
                    except Exception:
                        pass

        if vetoed_syms:
            actions = [a for a in actions if a.get("symbol") not in vetoed_syms
                       or a.get("action") != "buy"]
            log.info("[DEBATE] %d action(s) vetoed, %d remaining",
                     len(vetoed_syms), len(actions))

    # Stage 4 — execute
    if actions and regime != "halt" and state.allow_live_orders:
        results = order_executor.execute_all(
            actions=actions,
            account=state.account,
            positions=state.positions,
            market_status=state.md["market_status"],
            minutes_since_open=state.md["minutes_since_open"],
            current_prices=state.md.get("current_prices", {}),
            session_tier=session_tier,
            decision_id=_decision_id,
        )
        print("\n" + "=" * 62)
        print("  EXECUTION RESULTS")
        print("=" * 62)
        for r in results:
            print(r)
            if r.status == "submitted":
                _exec_action = next(
                    (a for a in actions if a.get("symbol") == r.symbol), {}
                )
                _idea_conviction: float | None = None
                if claude_decision:
                    _idea = next(
                        (i for i in (claude_decision.ideas or [])
                         if getattr(i, "symbol", None) == r.symbol), None
                    )
                    if _idea is not None:
                        _idea_conviction = getattr(_idea, "conviction", None)

                _order_html = _build_order_email_html(
                    r, _exec_action, signal_scores_obj,
                    _idea_conviction, state.equity, reasoning,
                )
                _send_email_alert(f"BullBearBot Order: {r.action.upper()} {r.symbol}", _order_html)
                if publisher:
                    try:
                        if r.fill_price is not None:
                            publisher.send_trade_alert(
                                action=r.action,
                                symbol=r.symbol,
                                qty=r.qty,
                                price=r.fill_price,
                                conviction=_idea_conviction,
                                catalyst=_exec_action.get("catalyst"),
                                equity=state.equity,
                            )
                        else:
                            log.info(
                                "[PUBLISHER] %s %s: fill_price=None — alert deferred "
                                "pending fill confirmation (T-021 will notify on fill/cancel)",
                                r.action, r.symbol,
                            )
                    except Exception as _ta_exc:
                        log.debug("send_trade_alert failed (non-fatal): %s", _ta_exc)
                if publisher and publisher.enabled and r.action in ("buy", "sell", "close"):
                    try:
                        _pub_action = next(
                            (a for a in actions if a.get("symbol") == r.symbol
                             and a.get("action") == r.action), None,
                        )
                        if _pub_action:
                            publisher.publish_trade_entry(
                                action=_pub_action,
                                debate_result=debate_results.get(r.symbol),
                                market_context=(
                                    f"VIX={state.md.get('vix', 20.0):.1f} regime={regime}"
                                ),
                                alpaca_client=_get_alpaca(),
                            )
                    except Exception as _pub_exc:
                        log.debug("publisher trade_entry failed (non-fatal): %s", _pub_exc)
                try:
                    from attribution import (
                        log_attribution_event as _log_attr,  # noqa: PLC0415
                    )
                    _log_attr(
                        event_type="order_submitted",
                        decision_id=_decision_id,
                        account="A1",
                        symbol=r.symbol,
                        module_tags=_module_tags,
                        trigger_flags=_trigger_flags,
                        trade_id=str(r.order_id) if r.order_id else None,
                        extra={"fill_price": r.fill_price, "filled_qty": r.filled_qty, "caller": "bot_order_submitted"},
                    )
                except Exception as _oa_exc:
                    log.debug("Attribution order_submitted failed (non-fatal): %s", _oa_exc)
                try:
                    from datetime import datetime as _d
                    from datetime import timezone as _tz

                    from decision_outcomes import (  # noqa: PLC0415
                        DecisionOutcomeRecord,
                        log_outcome_event,
                    )
                    _matching_action = next(
                        (a for a in actions if a.get("symbol") == r.symbol), {}
                    )
                    log_outcome_event(DecisionOutcomeRecord(
                        decision_id=_decision_id, account="A1", symbol=r.symbol,
                        timestamp=_d.now(_tz.utc).isoformat().replace("+00:00", "Z"),
                        action=r.action,
                        tier=_matching_action.get("tier"),
                        confidence=_matching_action.get("confidence"),
                        catalyst=_matching_action.get("catalyst"),
                        session=session_tier,
                        order_id=str(r.order_id) if r.order_id else None,
                        entry_price=r.fill_price,
                        stop_loss=_matching_action.get("stop_loss"),
                        take_profit=_matching_action.get("take_profit"),
                        status="submitted",
                        module_tags=_module_tags,
                        trigger_flags=_trigger_flags,
                    ))
                except Exception:
                    pass
            else:
                try:
                    from datetime import datetime as _d
                    from datetime import timezone as _tz

                    from decision_outcomes import (  # noqa: PLC0415
                        DecisionOutcomeRecord,
                        log_outcome_event,
                    )
                    _rej_action = next((a for a in actions if a.get("symbol") == r.symbol), {})
                    log_outcome_event(DecisionOutcomeRecord(
                        decision_id=_decision_id, account="A1", symbol=r.symbol,
                        timestamp=_d.now(_tz.utc).isoformat().replace("+00:00", "Z"),
                        action=getattr(r, "action", _rej_action.get("action", "")),
                        tier=_rej_action.get("tier"),
                        confidence=_rej_action.get("confidence"),
                        catalyst=_rej_action.get("catalyst"),
                        session=session_tier,
                        status="rejected_by_executor",
                        reject_reason=getattr(r, "reason", None) or str(r.status),
                        module_tags=_module_tags,
                        trigger_flags=_trigger_flags,
                    ))
                except Exception as _rej_ex_exc:
                    log.warning("[OUTCOMES] rejected_by_executor log failed: %s", _rej_ex_exc)
                try:
                    from shadow_lane import (
                        log_shadow_event as _log_shadow,  # noqa: PLC0415
                    )
                    _log_shadow(
                        "approved_trade", r.symbol,
                        {"action": r.action, "order_id": str(r.order_id) if r.order_id else ""},
                        decision_id=_decision_id, session=session_tier,
                    )
                except Exception:
                    pass
                if r.action == "buy":
                    try:
                        from feature_flags import (
                            is_enabled as _ff_enabled,  # noqa: PLC0415
                        )
                        if _ff_enabled("enable_thesis_checksum"):
                            from catalyst_normalizer import (  # noqa: PLC0415
                                log_catalyst,
                                normalize_catalyst,
                            )
                            from thesis_checksum import (  # noqa: PLC0415
                                build_checksum_from_decision,
                                log_checksum,
                            )
                            _matching_action = next(
                                (a for a in actions if a.get("symbol") == r.symbol), {}
                            )
                            _cs = build_checksum_from_decision(
                                decision_id=_decision_id, symbol=r.symbol,
                                idea=_matching_action, regime_obj=regime_obj,
                                signal_scores=signal_scores_obj,
                            )
                            if _cs:
                                log_checksum(_cs)
                            _cat = normalize_catalyst(
                                raw_text=_matching_action.get("catalyst", ""),
                                decision_id=_decision_id, symbol=r.symbol,
                            )
                            log_catalyst(_cat)
                    except Exception as _tc_exc:
                        log.debug("[CHECKSUM] thesis/catalyst capture failed (non-fatal): %s", _tc_exc)
                try:
                    from divergence import detect_fill_divergence  # noqa: PLC0415
                    _matching_action = next(
                        (a for a in actions if a.get("symbol") == r.symbol), {}
                    )
                    detect_fill_divergence(
                        symbol=r.symbol, account="A1",
                        intended_price=float(_matching_action.get("limit_price") or 0),
                        actual_fill_price=float(r.fill_price or 0),
                        intended_qty=float(r.qty or 0),
                        actual_qty=float(r.filled_qty or 0),
                        order_type=r.order_type,
                        decision_id=_decision_id,
                        trade_id=str(r.order_id) if r.order_id else None,
                    )
                except Exception as _fd_exc:
                    log.debug("[DIV] fill divergence check failed (non-fatal): %s", _fd_exc)
        print("=" * 62)

        # Forensic review for closed positions (T2.3)
        for _fr in results:
            if _fr.status != "submitted" or _fr.action not in ("sell", "close"):
                continue
            try:
                from feature_flags import is_enabled as _ff_fr  # noqa: PLC0415
                if _ff_fr("enable_thesis_checksum"):
                    from forensic_reviewer import review_closed_trade  # noqa: PLC0415
                    _fr_action    = next((a for a in actions if a.get("symbol") == _fr.symbol), {})
                    _entry_price  = float(_fr.fill_price or 0) or 0.0
                    _exit_price   = float(_fr.fill_price or 0) or 0.0
                    review_closed_trade(
                        decision_id=_decision_id, symbol=_fr.symbol,
                        entry_price=_entry_price, exit_price=_exit_price,
                        realized_pnl=0.0, hold_duration_hours=0.0,
                        entry_decision=_fr_action,
                        exit_reason=_fr_action.get("catalyst", ""),
                        regime_at_entry=regime_obj,
                    )
            except Exception as _frev_exc:
                log.debug("[FORENSIC] review_closed_trade failed (non-fatal): %s", _frev_exc)

        # Seed backstop for each new buy executed
        try:
            _cfg_path     = Path(__file__).parent / "strategy_config.json"
            _sc           = json.loads(_cfg_path.read_text()) if _cfg_path.exists() else {}
            _backstop_days = int(_sc.get("exit_management", {}).get("backstop_days", 7))
            for _r in results:
                if _r.status != "submitted" or _r.action != "buy":
                    continue
                import reconciliation as recon  # noqa: PLC0415
                recon.seed_backstop(
                    symbol=normalize_symbol(_r.symbol),
                    config_path=_cfg_path,
                    max_hold_days=_backstop_days,
                )
        except Exception as _tba_exc:
            log.warning("Backstop seeding failed: %s", _tba_exc)

    else:
        if actions and not state.allow_live_orders and regime != "halt":
            log.warning("[PREFLIGHT] shadow_only — %d action(s) blocked; logging blocked_by_mode",
                        len(actions))
            try:
                from datetime import datetime as _d
                from datetime import timezone as _tz

                from decision_outcomes import (  # noqa: PLC0415
                    DecisionOutcomeRecord,
                    log_outcome_event,
                )
                _pf_reason = (
                    f"preflight verdict={state.pf_result.verdict}"
                    if state.pf_result is not None
                    else "shadow_only mode — live orders suppressed"
                )
                for _ba in actions:
                    log_outcome_event(DecisionOutcomeRecord(
                        decision_id=_decision_id, account="A1",
                        symbol=_ba.get("symbol", ""),
                        timestamp=_d.now(_tz.utc).isoformat().replace("+00:00", "Z"),
                        action=_ba.get("action", ""),
                        tier=_ba.get("tier"), confidence=_ba.get("confidence"),
                        catalyst=_ba.get("catalyst"), session=session_tier,
                        status="blocked_by_mode", reject_reason=_pf_reason,
                        module_tags=_module_tags, trigger_flags=_trigger_flags,
                    ))
            except Exception as _bm_exc:
                log.warning("[OUTCOMES] blocked_by_mode log failed: %s", _bm_exc)
        else:
            log.info("Execute  no actions this cycle")

    elapsed = time.monotonic() - t_start
    log.info("── Cycle done in %.1fs ─────────────────────────────────", elapsed)


if __name__ == "__main__":
    run_cycle()
