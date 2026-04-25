"""
bot_stage0_precycle.py — Stage 0: pre-cycle infrastructure.

Fetches account state, runs preflight, loads market data, memory, portfolio
intelligence, reconciliation, divergence tracking, and exit manager.

Note: drawdown guard is NOT here — it lives in bot.py because it owns
module-level peak_equity state. run_precycle() returns None only on
preflight verdict=halt.

Public API:
  run_precycle(session_tier, next_cycle_time, publisher) -> PreCycleState | None
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alpaca.trading.requests import GetOrdersRequest

import data_warehouse as dw
import market_data
import memory as mem
import portfolio_intelligence as pi
import preflight as _preflight
import reconciliation as recon
import trade_memory
import watchlist_manager as wm
from bot_clients import _get_alpaca
from log_setup import get_logger
from schemas import BrokerSnapshot
from schemas import NormalizedPosition as _NP

log = get_logger(__name__)


@dataclass
class PreCycleState:
    # Account
    account: Any
    positions: list
    equity: float
    cash: float
    buying_power_float: float
    long_val: float
    exposure: float
    # Preflight
    allow_live_orders: bool
    allow_new_entries: bool
    pf_result: Any
    # Watchlist / market
    wl: dict
    symbols_stock: list
    symbols_crypto: list
    md: dict
    crypto_context: str
    cfg: dict
    # Memory
    recent_decisions: str
    ticker_lessons: str
    vector_memories: str
    similar_scenarios: list
    strategy_config_note: str
    # Portfolio intelligence / reconciliation
    pi_data: dict
    recon_log: list
    recon_diff: Any
    snapshot: Any  # BrokerSnapshot used by reconciliation + divergence
    # Divergence
    a1_mode: Any
    div_events: list = field(default_factory=list)
    # Exit management
    exit_status_str: str = "  (unavailable)"
    # Portfolio allocator shadow output (S6-ALLOCATOR)
    allocator_output: Any = None   # dict | None from portfolio_allocator.run_allocator_shadow()


def run_precycle(
    session_tier: str,
    next_cycle_time: str,
    publisher=None,
) -> "PreCycleState | None":
    """
    Execute Stage 0 infrastructure and return a typed PreCycleState.
    Returns None if preflight verdict is halt (cycle must be aborted).
    """
    # 1. Account
    account   = _get_alpaca().get_account()
    positions = _get_alpaca().get_all_positions()
    equity             = float(account.equity)
    cash               = float(account.cash)
    buying_power_float = float(account.buying_power)
    long_val  = sum(float(p.market_value) for p in positions if float(p.qty) > 0)
    exposure  = long_val / equity * 100 if equity > 0 else 0.0

    log.info("Account  equity=$%s  cash=$%s  exposure=%.1f%%  positions=%d",
             f"{equity:,.0f}", f"{cash:,.0f}", exposure, len(positions))

    # Log any unexpected short positions prominently so the operator can see them.
    _short_positions = [p for p in positions if float(p.qty) < 0]
    for _sp in _short_positions:
        log.warning(
            "[SHORT_POS] Unexpected short position detected: %s qty=%.0f "
            "market_value=$%.0f — manual intervention required to cover",
            _sp.symbol, float(_sp.qty), abs(float(_sp.market_value)),
        )

    # 1b. Preflight gate
    allow_live_orders = True
    allow_new_entries = True
    pf_result         = None
    try:
        pf_result = _preflight.run_preflight(
            caller="run_cycle",
            session_tier=session_tier,
            equity=equity,
            account_id="a1",
        )
        if pf_result.verdict == "halt":
            log.error("[PREFLIGHT] verdict=halt — aborting cycle  blockers=%s",
                      pf_result.blockers)
            return None
        elif pf_result.verdict == "reconcile_only":
            log.warning("[PREFLIGHT] verdict=reconcile_only — new entries blocked  blockers=%s",
                        pf_result.blockers)
            allow_new_entries = False
        elif pf_result.verdict == "shadow_only":
            log.warning("[PREFLIGHT] verdict=shadow_only — live orders suppressed")
            allow_live_orders = False
        elif pf_result.verdict == "go_degraded":
            log.warning("[PREFLIGHT] verdict=go_degraded  warnings=%s", pf_result.warnings)
    except Exception as _pf_exc:
        log.error("[PREFLIGHT] unexpected exception (proceeding with caution): %s", _pf_exc)

    # 3. Watchlist + market data
    wl = wm.get_active_watchlist()
    symbols_stock  = wl["stocks"] + wl["etfs"]
    symbols_crypto = wl["crypto"]

    wm.maybe_reset_session_tiers()
    wm.prune_stale_intraday()

    md = market_data.fetch_all(
        symbols_stock, symbols_crypto, session_tier,
        next_cycle_time=next_cycle_time,
    )
    log.info("Market   status=%s  vix=%.2f  time=%s",
             md["market_status"], md["vix"], md["time_et"])

    # ORB range update (no-op outside 9:30-9:45 window)
    try:
        import scheduler as _sched_orb
        _sched_orb._update_orb_range(md.get("current_prices", {}))
    except Exception as _orb_exc:
        log.debug("ORB range update failed (non-fatal): %s", _orb_exc)

    # Crypto context
    crypto_context = "  (crypto context unavailable)"
    try:
        _crypto_sentiment = dw.load_crypto_sentiment()
        _eth_btc          = md.get("eth_btc", {})
        crypto_context    = market_data.build_crypto_context_section(
            _crypto_sentiment, _eth_btc, session_tier)
    except Exception as _cc_exc:
        log.debug("Crypto context build failed (non-fatal): %s", _cc_exc)

    # 4. Memory
    mem.update_outcomes_from_alpaca()
    recent_decisions = mem.get_recent_decisions_str()
    try:
        ticker_lessons = mem.get_pattern_watchlist_summary()
    except Exception:
        ticker_lessons = mem.get_ticker_lessons()

    # Publisher exit posts for newly resolved trades
    if publisher and publisher.enabled:
        try:
            resolved = mem.get_newly_resolved_trades()
            for trade in resolved:
                _entry_px = float(
                    trade.get("entry_price") or
                    trade.get("avg_entry_price") or
                    trade.get("price") or 0
                )
                publisher.publish_trade_exit(
                    symbol=trade["symbol"],
                    entry_price=_entry_px,
                    exit_price=0.0,
                    qty=float(trade.get("qty") or 1),
                    pnl=float(trade.get("pnl") or 0),
                    hold_time_hours=float(trade.get("hold_time_hours") or 0.0),
                    outcome=trade["outcome"],
                    alpaca_client=_get_alpaca(),
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
    vm_stats = trade_memory.get_collection_stats()
    log.info("VectorMem  status=%s  stored=%d  retrieved=%d  scr_stored=%d",
             vm_stats["status"], vm_stats.get("total", 0),
             len(similar_scenarios), vm_stats.get("scr_total", 0))

    # A/B logging — emit a structured log line so we can compare cycles with
    # vs without retrieved vector context. Sampled by config to keep volume low.
    # cfg isn't loaded yet at this point in the function, so read flags inline.
    try:
        import random as _random  # noqa: PLC0415
        _ab_path = Path(__file__).parent / "strategy_config.json"
        _ab_flags: dict = {}
        if _ab_path.exists():
            try:
                _ab_flags = json.loads(_ab_path.read_text()).get("feature_flags", {})
            except Exception:
                _ab_flags = {}
        _ab_enabled = bool(_ab_flags.get("vector_memory_ab_logging", False))
        _ab_rate    = float(_ab_flags.get("vector_memory_ab_sample_rate", 0.1))
        if _ab_enabled and _random.random() < _ab_rate:
            _vm_entry_count = len(similar_scenarios) + len(_sim_scratchpads)
            _vm_token_estimate = len(vector_memories) // 4   # ~4 chars per token
            log.info(
                "[VECTOR_MEMORY_AB] session=%s entries_retrieved=%d "
                "estimated_tokens=%d had_vector_context=%s",
                session_tier, _vm_entry_count, _vm_token_estimate,
                _vm_entry_count > 0,
            )
    except Exception as _ab_exc:
        log.debug("[VECTOR_MEMORY_AB] sampler failed (non-fatal): %s", _ab_exc)

    # 4c. Strategy config note (for prompt injection — loaded via Stage 3 helper)
    strategy_config_note = "  (strategy_config.json not yet generated — using system prompt defaults)"
    try:
        from bot_stage3_decision import _load_strategy_config  # noqa: PLC0415
        strategy_config_note = _load_strategy_config()
    except Exception:
        pass

    # 4d. Portfolio intelligence + reconciliation
    cfg: dict        = {}
    pi_data: dict    = {}
    recon_log: list  = []
    recon_diff       = None
    snapshot         = None
    try:
        cfg_path = Path(__file__).parent / "strategy_config.json"
        cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
        pi_data = pi.build_portfolio_intelligence(
            equity, positions, cfg, buying_power=buying_power_float
        )

        _norm_positions = []
        for _p in positions:
            try:
                _norm_positions.append(_NP.from_alpaca_position(_p))
            except Exception:
                pass

        _open_orders = []
        try:
            from schemas import NormalizedOrder  # noqa: PLC0415
            _raw_orders = _get_alpaca().get_orders(GetOrdersRequest(status="open")) or []
            for _o in _raw_orders:
                try:
                    _open_orders.append(NormalizedOrder.from_alpaca_order(_o))
                except Exception:
                    pass
        except Exception as _ord_exc:
            log.debug("[RECON] Could not fetch open orders: %s", _ord_exc)

        snapshot = BrokerSnapshot(
            equity=float(equity or 0),
            cash=float(cash or 0),
            buying_power=float(buying_power_float or 0),
            open_orders=_open_orders,
            positions=_norm_positions,
        )

        recon_log, recon_diff = recon.run_account1_reconciliation(
            positions=_norm_positions,
            snapshot=snapshot,
            config=cfg,
            alpaca_client=_get_alpaca(),
            regime="unknown",
            pi_data=pi_data,
        )
        for _rl in recon_log:
            log.info(_rl)

    except Exception as _pi_exc:
        log.warning("Portfolio intelligence / reconciliation block failed: %s", _pi_exc)

    # Divergence tracking — load mode, scan protection (non-fatal)
    a1_mode    = None
    div_events: list = []
    try:
        from divergence import (  # noqa: PLC0415
            OperatingMode,
            check_clean_cycle,
            detect_protection_divergence,
            load_account_mode,
            respond_to_divergence,
        )
        a1_mode = load_account_mode("A1")
        if snapshot is not None and snapshot.positions:
            # Exclude short positions from protection divergence detection: short
            # positions need buy-stop coverage, which divergence.py does not check.
            # Short positions are logged above; operator must manage them manually.
            _long_positions = [p for p in snapshot.positions if p.qty > 0]
            div_events = detect_protection_divergence(
                account="A1",
                positions=_long_positions,
                open_orders=snapshot.open_orders,
                vix=float(md.get("vix", 20) or 20),
            )
        if div_events:
            a1_mode = respond_to_divergence(div_events, "A1", a1_mode)
        a1_mode = check_clean_cycle("A1", a1_mode, div_events)
        # T-003 Desync tripwire — abort (not just log) when state sources disagree
        if (
            pf_result is not None
            and pf_result.verdict in ("go", "go_degraded")
            and a1_mode.mode != OperatingMode.NORMAL
        ):
            log.error(
                "[PREFLIGHT] SAFETY OVERRIDE: preflight=%s but a1_mode=%s — "
                "aborting cycle to prevent DESYNC",
                pf_result.verdict,
                a1_mode.mode.value,
            )
            return None
        if a1_mode.mode != OperatingMode.NORMAL:
            log.warning("[DIV] A1 mode=%s scope=%s/%s",
                        a1_mode.mode.value,
                        a1_mode.scope.value,
                        a1_mode.scope_id)
    except Exception as _div_exc:
        log.warning("[DIV] divergence init failed (non-fatal): %s", _div_exc)

    # 4e. Exit management — refresh stale stops, trail profitable positions
    exit_status_str = "  (unavailable)"
    try:
        import exit_manager as _em  # noqa: PLC0415
        _em.run_exit_manager(positions, _get_alpaca(), cfg)
        exit_status_str = _em.format_exit_status_section(positions, _get_alpaca(), cfg)
    except Exception as _em_exc:
        log.debug("Exit manager failed (non-fatal): %s", _em_exc)

    # 4f. Portfolio allocator shadow (S6-ALLOCATOR)
    # Runs after pi_data is built. Shadow only — no orders, no execute_all().
    allocator_output = None
    try:
        import portfolio_allocator as _pa_mod  # noqa: PLC0415
        allocator_output = _pa_mod.run_allocator_shadow(
            pi_data=pi_data,
            positions=positions,
            cfg=cfg,
            session_tier=session_tier,
            equity=equity,
        )
    except Exception as _pa_exc:
        log.debug("Portfolio allocator shadow failed (non-fatal): %s", _pa_exc)

    # T-003 final DESYNC gate — synchronous fresh file read at the last possible
    # moment before the cycle commits to executing orders.  Catches any mode
    # transition that occurred after preflight's _check_operating_mode() call.
    if not _preflight.run_preflight_desync_check(
        preflight_verdict=pf_result.verdict if pf_result else "unknown",
    ):
        return None

    return PreCycleState(
        account=account,
        positions=positions,
        equity=equity,
        cash=cash,
        buying_power_float=buying_power_float,
        long_val=long_val,
        exposure=exposure,
        allow_live_orders=allow_live_orders,
        allow_new_entries=allow_new_entries,
        pf_result=pf_result,
        wl=wl,
        symbols_stock=symbols_stock,
        symbols_crypto=symbols_crypto,
        md=md,
        crypto_context=crypto_context,
        cfg=cfg,
        recent_decisions=recent_decisions,
        ticker_lessons=ticker_lessons,
        vector_memories=vector_memories,
        similar_scenarios=similar_scenarios,
        strategy_config_note=strategy_config_note,
        pi_data=pi_data,
        recon_log=recon_log,
        recon_diff=recon_diff,
        snapshot=snapshot,
        a1_mode=a1_mode,
        div_events=div_events,
        exit_status_str=exit_status_str,
        allocator_output=allocator_output,
    )
