"""
bot_options_stage0_preflight.py — A2 Stage 0: preflight, eligibility, reconciliation.

Public API:
  run_a2_preflight(session_tier, alpaca_client) -> A2PreflightResult
  _get_obs_mode_state() -> dict
  _update_obs_mode_state(state) -> bool
  is_observation_mode() -> bool
  _check_and_update_iv_ready(state) -> dict

Responsibilities:
  - Session gate (market/pre_market only)
  - Account eligibility check (equity floor)
  - Preflight verdict
  - A2 operating mode check
  - Options structure reconciliation
  - Observation mode tracking
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _get_et_now():
    """Return current datetime in US/Eastern. Extracted for testability."""
    from zoneinfo import ZoneInfo  # noqa: PLC0415
    return datetime.now(ZoneInfo("America/New_York"))
from pathlib import Path
from typing import Any

from log_setup import get_logger

log = get_logger(__name__)

_EQUITY_FLOOR = 25_000.0

_A2_DIR = Path(__file__).parent / "data" / "account2"

# Observation mode: first 20 trading days while IV history builds
_OBS_MODE_DAYS      = 20
_OBS_MODE_FILE      = _A2_DIR / "obs_mode_state.json"
_OBS_SCHEMA_VERSION = 2
# Full A2 optionable universe — used for IV readiness checks.
# Crypto (BTC/USD, ETH/USD) excluded — no options available.
# Symbols without IV history are bootstrapped automatically by the 4 AM job.
_OBS_IV_SYMBOLS = [
    # Technology
    "NVDA", "TSM", "MSFT", "CRWV", "PLTR", "ASML",
    # Energy
    "XLE", "XOM", "CVX", "USO",
    # Commodities
    "GLD", "SLV", "COPX",
    # Financials
    "JPM", "GS", "XLF",
    # Consumer
    "AMZN", "WMT", "XRT",
    # Defense
    "LMT", "RTX", "ITA",
    # Biotech / Health
    "XBI", "JNJ", "LLY",
    # International
    "EWJ", "FXI", "EEM", "EWM", "ECH",
    # Macro
    "SPY", "QQQ", "IWM", "TLT", "VXX",
    # Shipping / Housing / Utilities
    "FRO", "STNG", "RKT", "BE",
    # Legacy bootstrap symbols (original A2 Phase 1, not in watchlist_core)
    "AAPL", "META", "GOOGL", "AMD",
]


# ── Observation mode tracking ─────────────────────────────────────────────────

def _get_obs_mode_state() -> dict:
    """Load or initialize observation mode tracking state."""
    if _OBS_MODE_FILE.exists():
        try:
            return json.loads(_OBS_MODE_FILE.read_text())
        except Exception:
            pass
    return {
        "version": _OBS_SCHEMA_VERSION,
        "trading_days_observed": 0,
        "first_seen_date": None,
        "observation_complete": False,
        "iv_history_ready": False,
        "iv_ready_symbols": {},
    }


def _is_trading_day(iso_date: str) -> bool:
    """
    Return True if iso_date (YYYY-MM-DD) is a NYSE trading day.
    Excludes weekends. Excludes a fixed set of US market holidays.
    """
    from datetime import date  # noqa: PLC0415
    d = date.fromisoformat(iso_date)
    if d.weekday() >= 5:
        return False
    _fixed = {(1, 1), (7, 4), (12, 25)}
    if (d.month, d.day) in _fixed:
        return False
    import calendar as _cal  # noqa: PLC0415
    def _nth_weekday(year, month, weekday, n):
        """n-th occurrence (1-based) of weekday in month."""
        first = date(year, month, 1)
        delta = (weekday - first.weekday()) % 7
        return date(year, month, 1 + delta + (n - 1) * 7)
    def _last_monday(year, month):
        last = date(year, month, _cal.monthrange(year, month)[1])
        return last - __import__("datetime").timedelta(days=(last.weekday()) % 7)
    floating = {
        _nth_weekday(d.year, 1, 0, 3),
        _nth_weekday(d.year, 2, 0, 3),
        _last_monday(d.year, 5),
        _nth_weekday(d.year, 9, 0, 1),
        _nth_weekday(d.year, 11, 3, 4),
    }
    return d not in floating


def _update_obs_mode_state(state: dict) -> bool:
    """
    Update observation mode counter. Increment trading_days_observed only on
    NYSE trading days (no weekends, no US market holidays).
    Returns True if still in observation mode.
    """
    from datetime import date  # noqa: PLC0415
    today = date.today().isoformat()

    if state.get("observation_complete"):
        if state.get("version", 1) < _OBS_SCHEMA_VERSION:
            state = _check_and_update_iv_ready(state)
            state["version"] = _OBS_SCHEMA_VERSION
            try:
                _OBS_MODE_FILE.write_text(json.dumps(state, indent=2))
                log.info("[OPTS] obs_mode_state.json migrated to v%d", _OBS_SCHEMA_VERSION)
            except Exception:
                pass
        return False

    if state.get("first_seen_date") is None:
        state["first_seen_date"] = today

    if state.get("last_counted_date") != today and _is_trading_day(today):
        state["trading_days_observed"] = state.get("trading_days_observed", 0) + 1
        state["last_counted_date"] = today
    elif not _is_trading_day(today):
        log.debug("[OPTS] Observation mode: %s is not a trading day — not counting", today)

    days = state["trading_days_observed"]
    log.info("[OPTS] Observation mode: %d/%d trading days", days, _OBS_MODE_DAYS)

    if days >= _OBS_MODE_DAYS:
        state["observation_complete"] = True
        state["version"] = _OBS_SCHEMA_VERSION
        state = _check_and_update_iv_ready(state)
        log.info("[OPTS] Observation mode COMPLETE — Account 2 now live trading")

    try:
        _OBS_MODE_FILE.write_text(json.dumps(state, indent=2))
    except Exception:
        pass

    return not state.get("observation_complete", False)


def is_observation_mode() -> bool:
    """Quick check: is Account 2 still in observation mode?"""
    state = _get_obs_mode_state()
    return not state.get("observation_complete", False)


def _any_leg_has_fill(structure, alpaca_client) -> bool:
    """Return True if any Alpaca order for this structure has filled_qty > 0."""
    for order_id in (structure.order_ids or []):
        try:
            order = alpaca_client.get_order_by_id(order_id)
            if float(getattr(order, "filled_qty", 0) or 0) > 0:
                return True
        except Exception:
            pass
    return False


_SPREAD_STRATEGIES: frozenset[str] = frozenset({
    "call_debit_spread", "put_debit_spread",
    "call_credit_spread", "put_credit_spread",
    "iron_condor", "iron_butterfly",
    "straddle", "strangle",
})


def _cancel_and_clear_unfilled_orders(
    alpaca_client,
    config: dict,
) -> tuple[int, frozenset]:
    """
    For every SUBMITTED structure with zero filled qty:
      1. Cancel all Alpaca orders for that structure
      2. Set lifecycle to CANCELLED so the symbol re-enters the candidate pool

    For spread (mleg) strategies using GTC: applies a max_age guard so a fresh
    spread order is not immediately cancelled on the next cycle. Only cancels
    mleg spreads that have been SUBMITTED for longer than mleg_max_age_minutes
    (default 30, config key: account2.mleg_max_age_minutes). Single-leg GTC
    orders are cancelled every cycle for re-pricing.

    This ensures A2 always re-prices on fresh mid values each cycle rather
    than leaving stale limit orders open indefinitely (GTC single legs) or
    waiting for DAY spread orders to expire silently at close.

    Gated by account2.auto_cancel_unfilled_orders (default True).
    Returns (count, frozenset[underlying]) — the set is used by the pending
    guard to skip cooldown for structures cancelled in this same cycle.
    Non-fatal per structure.
    """
    if not config.get("account2", {}).get("auto_cancel_unfilled_orders", True):
        return 0, frozenset()

    import options_state as _os  # noqa: PLC0415
    from schemas import StructureLifecycle  # noqa: PLC0415

    _max_age_min = float(config.get("account2", {}).get("mleg_max_age_minutes", 30.0))
    _now = datetime.now(timezone.utc)

    all_structs = _os.load_structures()
    cancelled = 0
    cancelled_underlyings: set[str] = set()
    _first_pass_ids: set[str] = set()  # track IDs processed in first pass

    for s in all_structs:
        try:
            if s.lifecycle != StructureLifecycle.SUBMITTED:
                continue
            if not s.order_ids:
                continue
            if _any_leg_has_fill(s, alpaca_client):
                continue  # partial or full fill — do not cancel

            # Max-age guard for mleg spreads: give GTC spread orders time to fill
            # before cancelling and re-pricing. Single-leg orders cancel every cycle.
            _strat_val = s.strategy.value if hasattr(s.strategy, "value") else str(s.strategy)
            if _strat_val in _SPREAD_STRATEGIES:
                try:
                    _opened = datetime.fromisoformat(s.opened_at.replace("Z", "+00:00"))
                    if _opened.tzinfo is None:
                        _opened = _opened.replace(tzinfo=timezone.utc)
                    _age_min = (_now - _opened).total_seconds() / 60.0
                except Exception:
                    _age_min = _max_age_min + 1.0  # unparseable → treat as stale

                if _age_min < _max_age_min:
                    log.info(
                        "[PREFLIGHT] GTC mleg %s (%s) age=%.0fmin < max=%.0fmin — keeping",
                        s.underlying, _strat_val, _age_min, _max_age_min,
                    )
                    continue
                log.info(
                    "[PREFLIGHT] canceling stale GTC mleg %s (%s) age=%.0fmin > max=%.0fmin",
                    s.underlying, _strat_val, _age_min, _max_age_min,
                )

            for order_id in s.order_ids:
                try:
                    alpaca_client.cancel_order_by_id(order_id)
                    log.info(
                        "[PREFLIGHT] Cancelled unfilled order %s for %s (%s)",
                        order_id[:8], s.underlying, s.strategy.value,
                    )
                except Exception as _ce:
                    log.debug(
                        "[PREFLIGHT] Cancel order %s failed (non-fatal): %s",
                        order_id[:8], _ce,
                    )

            s.last_cancelled_at = _now.isoformat()
            s.lifecycle = StructureLifecycle.CANCELLED
            s.add_audit(
                "auto-cancelled: unfilled order — resubmitting with fresh pricing next cycle"
            )
            _os.save_structure(s)
            cancelled_underlyings.add(s.underlying)
            _first_pass_ids.add(s.structure_id)
            cancelled += 1
        except Exception as _e:
            log.debug("[PREFLIGHT] _cancel_and_clear_unfilled_orders skip (non-fatal): %s", _e)

    if cancelled:
        log.info(
            "[PREFLIGHT] Cancelled %d unfilled order(s) — symbols re-enter candidate pool",
            cancelled,
        )

    # Second pass — cancel stale orders on terminal structures.
    # Entry orders can survive as "open" in Alpaca when a prior cancel call
    # failed silently or a structure was forcibly closed. cancel_order_by_id
    # is idempotent — returns an error (caught below) if already done.
    _TERMINAL = frozenset({
        StructureLifecycle.CLOSED,
        StructureLifecycle.CANCELLED,
        StructureLifecycle.REJECTED,
        StructureLifecycle.EXPIRED,
    })
    stale_cancelled = 0
    for s in all_structs:
        try:
            if s.lifecycle not in _TERMINAL or not s.order_ids:
                continue
            if s.structure_id in _first_pass_ids:
                continue  # just cancelled by first pass — order already handled
            for order_id in s.order_ids:
                try:
                    alpaca_client.cancel_order_by_id(order_id)
                    log.debug(
                        "[PREFLIGHT] Cancelled stale order %s on terminal %s (%s)",
                        order_id[:8], s.underlying, s.lifecycle.value,
                    )
                    stale_cancelled += 1
                except Exception as _ce:
                    log.debug(
                        "[PREFLIGHT] Stale cancel %s (already done, non-fatal): %s",
                        order_id[:8], _ce,
                    )
        except Exception as _e:
            log.debug("[PREFLIGHT] stale-order cleanup skip (non-fatal): %s", _e)
    if stale_cancelled:
        log.info(
            "[PREFLIGHT] Cleaned %d stale order(s) from terminal structures",
            stale_cancelled,
        )

    return cancelled, frozenset(cancelled_underlyings)


def _infer_strategy_from_legs(legs: list):
    """
    Infer the most likely OptionStrategy from a list of OptionsLeg objects.

    Uses leg count, option_type, and side to distinguish spreads from singles.
    Defaults to SINGLE_CALL when the pattern is ambiguous (e.g. 3+ mixed legs).

    Call/put spread credit vs debit is determined by strike ordering:
      credit call spread: buy_strike > sell_strike (bear call — net credit)
      debit  call spread: buy_strike < sell_strike (bull call — net debit)
      credit put  spread: sell_strike > buy_strike (bull put  — net credit)
      debit  put  spread: sell_strike < buy_strike (bear put  — net debit)
    """
    from schemas import OptionStrategy  # noqa: PLC0415

    n = len(legs)
    if n == 0:
        return OptionStrategy.SINGLE_CALL
    if n == 1:
        leg = legs[0]
        if leg.option_type == "put":
            return OptionStrategy.SINGLE_PUT if leg.side == "buy" else OptionStrategy.SHORT_PUT
        return OptionStrategy.SINGLE_CALL
    if n == 2:
        types = {leg.option_type for leg in legs}
        buys  = [leg for leg in legs if leg.side == "buy"]
        sells = [leg for leg in legs if leg.side == "sell"]
        if buys and sells:
            if types == {"call"}:
                return (
                    OptionStrategy.CALL_CREDIT_SPREAD
                    if buys[0].strike > sells[0].strike
                    else OptionStrategy.CALL_DEBIT_SPREAD
                )
            if types == {"put"}:
                return (
                    OptionStrategy.PUT_CREDIT_SPREAD
                    if sells[0].strike > buys[0].strike
                    else OptionStrategy.PUT_DEBIT_SPREAD
                )
        if types == {"call", "put"}:
            return OptionStrategy.STRADDLE
    if n == 4:
        return OptionStrategy.IRON_CONDOR
    return OptionStrategy.SINGLE_CALL


def _reconcile_orphan_positions(
    live_opts: list,
    tracked_occs: set,
    existing_structures: list,
) -> set[str]:
    """
    Create orphan_tracked structures for live Alpaca option positions that have
    no matching leg in structures.json.

    Idempotent: skips any underlying that already has an orphan_tracked structure.
    Fires a log.error (+ optional WhatsApp) when unrealized loss > 50% of cost basis.

    Returns the set of underlyings for which a new orphan_tracked structure was created.
    Non-fatal per underlying.
    """
    import re as _re  # noqa: PLC0415

    import options_state as _os  # noqa: PLC0415
    from schemas import (  # noqa: PLC0415
        OptionsLeg,
        OptionsStructure,
        StructureLifecycle,
        Tier,
    )

    _existing_orphan_unders = {
        s.underlying
        for s in existing_structures
        if (
            s.lifecycle.value
            if hasattr(s.lifecycle, "value")
            else str(s.lifecycle)
        ) in ("orphan_tracked", "manual_review_required")
    }

    # Group untracked positions by underlying
    _orphan_by_under: dict[str, list] = {}
    for _p in live_opts:
        _sym = str(getattr(_p, "symbol", "") or "")
        if _sym and _sym not in tracked_occs:
            _m = _re.match(r"^([A-Z]+)\d", _sym)
            if _m:
                _orphan_by_under.setdefault(_m.group(1), []).append(_p)

    created: set[str] = set()
    for _under, _oplist in _orphan_by_under.items():
        if _under in _existing_orphan_unders:
            continue

        try:
            _total_pnl = sum(
                float(getattr(_p, "unrealized_pl", 0) or 0) for _p in _oplist
            )
            _total_cost = sum(
                abs(float(getattr(_p, "qty", 0) or 0))
                * float(getattr(_p, "avg_entry_price", 0) or 0)
                * 100
                for _p in _oplist
            )

            _legs: list = []
            for _p in _oplist:
                _osym = str(getattr(_p, "symbol", "") or "")
                _qty_raw = float(getattr(_p, "qty", 0) or 0)
                _oleg_m = _re.match(
                    r"^([A-Z/]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$", _osym
                )
                if _oleg_m:
                    _oexp = f"20{_oleg_m.group(2)}-{_oleg_m.group(3)}-{_oleg_m.group(4)}"
                    _otype = "call" if _oleg_m.group(5) == "C" else "put"
                    _ostrike = int(_oleg_m.group(6)) / 1000.0
                else:
                    _oexp = ""; _otype = "call"; _ostrike = 0.0
                _legs.append(OptionsLeg(
                    occ_symbol=_osym,
                    underlying=_under,
                    side="buy" if _qty_raw > 0 else "sell",
                    qty=abs(int(_qty_raw)),
                    option_type=_otype,
                    strike=_ostrike,
                    expiration=_oexp,
                    filled_price=float(getattr(_p, "avg_entry_price", 0) or 0),
                ))

            _struct = OptionsStructure(
                structure_id=(
                    f"orphan_{_under}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
                ),
                underlying=_under,
                strategy=_infer_strategy_from_legs(_legs),
                lifecycle=StructureLifecycle.ORPHAN_TRACKED,
                legs=_legs,
                contracts=max(
                    abs(int(float(getattr(_p, "qty", 0) or 0))) for _p in _oplist
                ),
                max_cost_usd=_total_cost,
                opened_at=datetime.now(timezone.utc).isoformat(),
                catalyst="orphan_recovered",
                tier=Tier.CORE,
                notes=(
                    "Recovered orphan — original structure was cancelled "
                    "but position remains live in Alpaca"
                ),
                close_reason_code="orphan_recovered",
                # Tiered-exit per-structure overrides (orphan defaults).
                # Orphans lack original entry context, so close targets are
                # tightened relative to fresh entries.
                close_profit_target_pct=0.50,
                close_max_loss_pct=0.35,
                close_time_stop_pct_dte=0.50,
            )
            _struct.pnl_unrealized = _total_pnl
            _os.save_structure(_struct)
            log.warning(
                "[PREFLIGHT] Orphan recovered: %s — %d position(s), pnl=$%.0f"
                " — now tracked as orphan_tracked",
                _under, len(_oplist), _total_pnl,
            )
            created.add(_under)

            # Safety alert: fire when unrealized loss > 50% of cost basis
            if _total_cost > 0 and _total_pnl < -(_total_cost * 0.5):
                log.error(
                    "[PREFLIGHT] ORPHAN ALERT: %s at $%.0f loss (%.0f%% of cost"
                    " $%.0f) — manual close needed",
                    _under, abs(_total_pnl),
                    abs(_total_pnl) / _total_cost * 100, _total_cost,
                )
                try:
                    from alerts import send_whatsapp_direct  # noqa: PLC0415
                    send_whatsapp_direct(
                        f"[ORPHAN ALERT] {_under}: orphan at "
                        f"${abs(_total_pnl):,.0f} loss "
                        f"({abs(_total_pnl)/_total_cost*100:.0f}% of cost "
                        f"${_total_cost:,.0f}) — manual close needed",
                        dedup_key=f"orphan_alert_{_under}",
                        dedup_minutes=60,
                    )
                except Exception:
                    pass
        except Exception as _e:
            log.debug("[PREFLIGHT] Orphan reconciler skip %s (non-fatal): %s", _under, _e)

    return created


def _sync_pnl_from_alpaca_positions(open_structs: list, alpaca_positions: list) -> int:
    """
    Update pnl_unrealized on open structures using Alpaca's unrealized_pl field.

    Supplements the options-chain price path in close_check_loop. When the chain
    data is unavailable (illiquid contracts, zero-bid options, market closed), the
    stored pnl_unrealized goes stale and loss-based exits (stop_loss_hit, max_loss_exit)
    may fail to fire even when the position is deeply underwater.

    Returns count of structures updated. Non-fatal per structure.
    """
    import options_state as _os  # noqa: PLC0415

    # Build {occ_symbol: unrealized_pl} from Alpaca — always available, direction-aware.
    alpaca_pnl: dict[str, float] = {}
    for p in alpaca_positions:
        sym = str(getattr(p, "symbol", "") or "")
        raw = getattr(p, "unrealized_pl", None)
        if sym and raw is not None:
            try:
                alpaca_pnl[sym] = float(raw)
            except (TypeError, ValueError):
                pass

    updated = 0
    for struct in open_structs:
        try:
            total = 0.0
            all_found = True
            for leg in struct.legs:
                occ = getattr(leg, "occ_symbol", None)
                if not occ or occ not in alpaca_pnl:
                    all_found = False
                    break
                total += alpaca_pnl[occ]
            if not all_found:
                continue
            new_pnl = round(total, 2)
            if struct.pnl_unrealized != new_pnl:
                struct.pnl_unrealized = new_pnl
                _os.save_structure(struct)
                updated += 1
        except Exception:
            pass

    if updated:
        log.info("[PREFLIGHT] Synced pnl_unrealized from Alpaca for %d structure(s)", updated)
    return updated


def _is_duplicate_submission(
    symbol: str,
    structures: list,
    config: dict | None = None,
) -> bool:
    """
    Return True if a new structure for this symbol should be blocked.

    Blocks when:
    - A SUBMITTED structure exists (in-flight, not yet cancelled), OR
    - A CANCELLED structure has last_cancelled_at within cancel_cooldown_hours.
      Cooldown only applies when last_cancelled_at is set (old structures without
      the field are not blocked).

    After _cancel_and_clear_unfilled_orders() runs, any remaining SUBMITTED
    structure has at least a partial fill — blocking is correct.
    """
    from schemas import StructureLifecycle  # noqa: PLC0415

    _cfg = config or {}
    cooldown_hours = float(_cfg.get("account2", {}).get("cancel_cooldown_hours", 1.0))
    now = datetime.now(timezone.utc)

    for s in structures:
        if s.underlying != symbol:
            continue
        if s.lifecycle == StructureLifecycle.SUBMITTED:
            return True
        if s.lifecycle == StructureLifecycle.CANCELLED:
            cancelled_at = getattr(s, "last_cancelled_at", None)
            if cancelled_at is not None:
                try:
                    elapsed = (now - datetime.fromisoformat(cancelled_at)).total_seconds()
                    if elapsed < cooldown_hours * 3600:
                        return True
                except (ValueError, TypeError):
                    pass
    return False


def _cleanup_stale_proposed_structures(
    structures: list,
    max_age_hours: float = 2.0,
) -> int:
    """
    Cancel PROPOSED structures older than max_age_hours with empty order_ids.

    These are proposals that were never submitted — either pre-fix artifacts
    from a cycle that failed before reaching the executor, or proposals that
    expired without being acted upon.

    Rules:
      - Only touches PROPOSED lifecycle structures
      - Only touches structures with empty order_ids (non-empty = may be in-flight)
      - Only touches structures older than max_age_hours
      - Non-fatal per structure — one bad entry never blocks the rest

    Returns count of structures cancelled.
    """
    import options_state as _os  # noqa: PLC0415
    from schemas import StructureLifecycle  # noqa: PLC0415

    cancelled = 0
    now = datetime.now(timezone.utc)

    for s in structures:
        try:
            if s.lifecycle != StructureLifecycle.PROPOSED:
                continue
            if s.order_ids:
                continue  # has order_ids — may be in-flight, do not touch

            try:
                opened = datetime.fromisoformat(s.opened_at.replace("Z", "+00:00"))
                if opened.tzinfo is None:
                    opened = opened.replace(tzinfo=timezone.utc)
                age_hours = (now - opened).total_seconds() / 3600
            except Exception:
                age_hours = max_age_hours + 1.0  # unparseable timestamp → treat as stale

            if age_hours <= max_age_hours:
                continue

            s.lifecycle = StructureLifecycle.CANCELLED
            s.add_audit(
                f"auto-cancelled: stale proposed, age={age_hours:.1f}h > {max_age_hours}h, "
                "no order_ids — never submitted"
            )
            _os.save_structure(s)
            log.info(
                "[PREFLIGHT] Cancelled stale PROPOSED %s (%s) age=%.1fh",
                s.structure_id[:8], s.underlying, age_hours,
            )
            cancelled += 1
        except Exception as _e:
            log.debug("[PREFLIGHT] _cleanup_stale_proposed skip (non-fatal): %s", _e)

    return cancelled


def _check_and_update_iv_ready(state: dict) -> dict:
    """
    Check IV history readiness for all core A2 symbols via options_data.
    Writes iv_history_ready + iv_ready_symbols into state dict (in-place).
    Never modifies observation_complete. Non-fatal.
    Returns the mutated state dict.

    NOTE: iv_ready_symbols in obs_mode_state.json is a legacy field.
    It was authoritative during observation mode (first 20 trading days) when
    individual symbols could be not-yet-seeded. Once observation_complete=true,
    this dict is no longer checked — all 43 symbols are assumed IV-ready and
    the bot proceeds unconditionally. The on-disk snapshot may show only the
    original 16 symbols (pre-S4-A expansion); this is stale but harmless.
    """
    try:
        import options_data  # noqa: PLC0415
        result = options_data.check_iv_history_ready(_OBS_IV_SYMBOLS)
        state["iv_history_ready"] = result["all_ready"]
        state["iv_ready_symbols"] = result["symbol_ready"]
        log.info("[OPTS] IV history check: %d/%d symbols ready",
                 result["ready_count"], result["total_count"])
    except Exception as exc:  # noqa: BLE001
        log.warning("[OPTS] _check_and_update_iv_ready failed (non-fatal): %s", exc)
        state.setdefault("iv_history_ready", False)
        state.setdefault("iv_ready_symbols", {})
    return state


@dataclass
class A2PreflightResult:
    """Result from run_a2_preflight. halt=True means the cycle must abort."""
    halt: bool = False
    halt_reason: str = ""
    equity: float = 0.0
    cash: float = 0.0
    buying_power: float = 0.0
    pf_allow_live_orders: bool = True
    pf_allow_new_entries: bool = True
    a2_mode: Any = None
    pending_underlyings: frozenset = field(default_factory=frozenset)


def _build_a2_broker_snapshot(alpaca_client):
    """
    Build a BrokerSnapshot from Account 2's current live state.

    Fetches positions and open orders from the A2 Alpaca account.
    Returns a BrokerSnapshot with normalised positions and orders.
    Non-fatal — returns an empty snapshot on any error so reconciliation
    can degrade gracefully rather than blocking the cycle.
    """
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    from schemas import BrokerSnapshot, NormalizedOrder, NormalizedPosition

    norm_positions: list = []
    norm_orders: list = []
    equity = buying_power = cash = 0.0

    try:
        account = alpaca_client.get_account()
        equity       = float(account.equity)
        cash         = float(account.cash)
        buying_power = float(account.buying_power)
    except Exception as exc:
        log.warning("[OPTS_RECON] snapshot: failed to fetch account: %s", exc)

    try:
        positions = alpaca_client.get_all_positions()
        norm_positions = [NormalizedPosition.from_alpaca_position(p) for p in positions]
    except Exception as exc:
        log.warning("[OPTS_RECON] snapshot: failed to fetch positions: %s", exc)

    try:
        orders = alpaca_client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN)
        )
        norm_orders = [NormalizedOrder.from_alpaca_order(o) for o in orders]
    except Exception as exc:
        log.warning("[OPTS_RECON] snapshot: failed to fetch orders: %s", exc)

    return BrokerSnapshot(
        positions=norm_positions,
        open_orders=norm_orders,
        equity=equity,
        cash=cash,
        buying_power=buying_power,
    )


def run_a2_preflight(
    session_tier: str,
    alpaca_client,
) -> A2PreflightResult:
    """
    Run A2 preflight checks. Returns A2PreflightResult with halt=True if cycle
    should abort. Handles: session gate, equity floor, preflight verdict,
    A2 operating mode, and options structure reconciliation.

    Note: observation mode tracking (_update_obs_mode_state) is handled by
    the orchestrator in bot_options.py — those helpers are tested with
    mock.patch("bot_options.*") so they must stay there.
    """
    result = A2PreflightResult()

    # Session gate — options only trade during market hours
    if session_tier not in ("market", "pre_market"):
        log.info("[OPTS] Session=%s — options cycle skipped (market hours only)", session_tier)
        return A2PreflightResult(halt=True, halt_reason="session_not_market")

    # Near-close gate: no new options structures after 15:50 ET.
    # Options need time to fill; returning halt=True skips new proposals.
    # Non-fatal: proceeds normally if timezone check raises.
    try:
        _et = _get_et_now()
        if _et.hour == 15 and _et.minute >= 50:
            log.info("[PREFLIGHT] near_close_gate: blocking new structures after 15:50 ET")
            result.halt = True
            result.halt_reason = "near_close_gate: no new options structures after 15:50 ET"
            return result
    except Exception:
        pass  # non-fatal — proceed if timezone check fails

    # Account equity check
    try:
        account = alpaca_client.get_account()
        result.equity        = float(account.equity)
        result.cash          = float(account.cash)
        result.buying_power  = float(account.buying_power)
        log.info("[OPTS] Account 2: equity=$%s  cash=$%s  buying_power=$%s",
                 f"{result.equity:,.0f}", f"{result.cash:,.0f}",
                 f"{result.buying_power:,.0f}")
    except Exception as exc:
        log.error("[OPTS] Cannot fetch Account 2 status: %s — skipping cycle", exc)
        return A2PreflightResult(halt=True, halt_reason="account_fetch_failed")

    if result.equity < _EQUITY_FLOOR:
        log.warning("[OPTS] Account 2 equity $%.0f below floor $%.0f — halting",
                    result.equity, _EQUITY_FLOOR)
        return A2PreflightResult(halt=True, halt_reason="equity_below_floor",
                                 equity=result.equity, cash=result.cash)

    # Preflight gate
    try:
        import preflight as _preflight  # noqa: PLC0415
        _pf_result = _preflight.run_preflight(
            caller="run_options_cycle",
            session_tier=session_tier,
            equity=result.equity,
            account_id="a2",
        )
        if _pf_result.verdict == "halt":
            log.error("[PREFLIGHT] verdict=halt — aborting options cycle  blockers=%s",
                      _pf_result.blockers)
            return A2PreflightResult(halt=True, halt_reason="preflight_halt",
                                     equity=result.equity, cash=result.cash)
        elif _pf_result.verdict == "reconcile_only":
            log.warning("[PREFLIGHT] verdict=reconcile_only — new A2 entries blocked  blockers=%s",
                        _pf_result.blockers)
            result.pf_allow_new_entries = False
        elif _pf_result.verdict == "shadow_only":
            log.warning("[PREFLIGHT] verdict=shadow_only — A2 live orders suppressed")
            result.pf_allow_live_orders = False
        elif _pf_result.verdict == "go_degraded":
            log.warning("[PREFLIGHT] verdict=go_degraded  warnings=%s", _pf_result.warnings)
    except Exception as _pf_exc:
        log.error("[PREFLIGHT] unexpected exception (proceeding with caution): %s", _pf_exc)

    # A2 operating mode (non-fatal)
    try:
        from divergence import OperatingMode, load_account_mode  # noqa: PLC0415
        result.a2_mode = load_account_mode("A2")
        if result.a2_mode.mode != OperatingMode.NORMAL:
            log.warning("[DIV] A2 mode=%s scope=%s/%s",
                        result.a2_mode.mode.value,
                        result.a2_mode.scope.value,
                        result.a2_mode.scope_id)
    except Exception as _div_exc:
        log.warning("[DIV] A2 mode load failed (non-fatal): %s", _div_exc)

    # Load config once for all preflight cleanup steps
    _cfg_path = Path(__file__).parent / "strategy_config.json"
    _s_cfg: dict = {}
    try:
        _s_cfg = json.loads(_cfg_path.read_text()) if _cfg_path.exists() else {}
    except Exception:
        pass

    # Stale PROPOSED structure cleanup (before reconciliation)
    try:
        import options_state as _oss  # noqa: PLC0415
        _max_age = float(_s_cfg.get("account2", {}).get("stale_cleanup_max_age_hours", 2.0))
        _all_structs = _oss.load_structures()
        _n_cleaned = _cleanup_stale_proposed_structures(_all_structs, _max_age)
        if _n_cleaned:
            log.info("[PREFLIGHT] Cancelled %d stale PROPOSED structure(s)", _n_cleaned)
    except Exception as _cleanup_err:
        log.debug("[PREFLIGHT] stale PROPOSED cleanup failed (non-fatal): %s", _cleanup_err)

    # Cancel unfilled SUBMITTED orders and reset lifecycle so symbols re-enter pool.
    # Runs before reconciliation so the pending_underlyings guard sees fresh state.
    _just_cancelled_under: frozenset = frozenset()
    try:
        _n_cancelled, _just_cancelled_under = _cancel_and_clear_unfilled_orders(alpaca_client, _s_cfg)
    except Exception as _cancel_err:
        log.debug("[PREFLIGHT] _cancel_and_clear_unfilled_orders failed (non-fatal): %s", _cancel_err)

    # Options structure reconciliation (before new proposals)
    try:
        import options_state  # noqa: PLC0415
        from reconciliation import (  # noqa: PLC0415
            execute_reconciliation_plan,
            plan_structure_repair,
            reconcile_options_structures,
        )
        _open_structs = options_state.get_open_structures()
        # Pass all structures (including SUBMITTED) to reconcile_options_structures so
        # their OCC symbols are registered as known.  This prevents orphan-close of
        # positions whose fills arrived between cycles before _update_fill_prices() ran.
        _all_structs = options_state.load_structures()
        if _open_structs:
            _recon_snapshot = _build_a2_broker_snapshot(alpaca_client)
            _struct_diff = reconcile_options_structures(
                structures=_all_structs,
                snapshot=_recon_snapshot,
                current_time=datetime.now(timezone.utc).isoformat(),
                config=_s_cfg,
            )
            if any([
                _struct_diff.broken,
                _struct_diff.expiring_soon,
                _struct_diff.needs_close,
                _struct_diff.orphaned_legs,
            ]):
                _repair_plan = plan_structure_repair(
                    diff=_struct_diff,
                    structures=_open_structs,
                    snapshot=_recon_snapshot,
                    config=_s_cfg,
                )
                log.info(
                    "[OPTS_RECON] %d broken, %d expiring, "
                    "%d needs_close, %d orphaned — %d repair action(s)",
                    len(_struct_diff.broken),
                    len(_struct_diff.expiring_soon),
                    len(_struct_diff.needs_close),
                    len(_struct_diff.orphaned_legs),
                    len(_repair_plan),
                )
                execute_reconciliation_plan(
                    plan=_repair_plan,
                    trading_client=alpaca_client,
                    account_id="account2",
                    dry_run=False,
                )
            else:
                log.debug("[OPTS_RECON] %d open structures — all intact", len(_open_structs))
        else:
            log.debug("[OPTS_RECON] No open structures — skipping reconciliation")

        # Pending in-flight guard: after _cancel_and_clear_unfilled_orders() has run,
        # any remaining SUBMITTED structure has at least a partial fill from a prior
        # cycle. Block re-submission for those underlyings to avoid double-positioning.
        # Uses load_structures() (all lifecycle states) — get_open_structures() returns
        # only FULLY_FILLED/PARTIALLY_FILLED and would never see SUBMITTED.
        #
        # Key invariant: structures cancelled by _cancel_and_clear_unfilled_orders
        # in THIS cycle (_just_cancelled_under) are immediately eligible for
        # re-submission — they must NOT be subject to cooldown. Without this guard,
        # the CANCELLED cooldown blocks re-entry for the full cooldown period even
        # though the order was just cancelled moments ago.
        try:
            from schemas import StructureLifecycle  # noqa: PLC0415
            _all_for_guard = options_state.load_structures()
            _cooldown_hours = float(
                _s_cfg.get("account2", {}).get("cancel_cooldown_hours", 1.0)
            )
            _ttl_secs = float(
                _s_cfg.get("account2", {}).get("submitted_ttl_minutes", 60.0)
            ) * 60.0
            _now = datetime.now(timezone.utc)
            _blocked: list[str] = []
            for _gs in _all_for_guard:
                if _gs.lifecycle == StructureLifecycle.SUBMITTED:
                    # TTL safety: SUBMITTED order with no fill for >ttl_secs → force-cancel.
                    # Guards against Alpaca cancel failures or bot restarts leaving stale state.
                    _sub_ts = getattr(_gs, "opened_at", None)
                    _stale = False
                    if _sub_ts:
                        try:
                            _stale = (_now - datetime.fromisoformat(_sub_ts)).total_seconds() > _ttl_secs
                        except (ValueError, TypeError):
                            pass
                    if _stale:
                        for _oid in _gs.order_ids:
                            try:
                                alpaca_client.cancel_order_by_id(_oid)
                            except Exception:
                                pass
                        _gs.lifecycle = StructureLifecycle.CANCELLED
                        _gs.last_cancelled_at = _now.isoformat()
                        _gs.add_audit(
                            f"TTL safety: force-cleared SUBMITTED order after >{_ttl_secs/60:.0f} min with no fill"
                        )
                        options_state.save_structure(_gs)
                        log.warning(
                            "[PREFLIGHT] %s: TTL safety — force-cleared stale SUBMITTED order "
                            "(>%.0f min no fill) — re-entering pool",
                            _gs.underlying, _ttl_secs / 60,
                        )
                        # Treat as just-cancelled: immediately eligible for re-submission
                        _just_cancelled_under = frozenset(_just_cancelled_under | {_gs.underlying})
                    else:
                        _blocked.append(_gs.underlying)
                elif _gs.lifecycle == StructureLifecycle.CANCELLED:
                    # Structures cancelled in THIS cycle are immediately eligible — no cooldown.
                    if _gs.underlying in _just_cancelled_under:
                        log.info(
                            "[PREFLIGHT] %s: pending state cleared — re-entering pool",
                            _gs.underlying,
                        )
                        continue
                    _ts = getattr(_gs, "last_cancelled_at", None)
                    if _ts is not None:
                        try:
                            _elapsed = (_now - datetime.fromisoformat(_ts)).total_seconds()
                            if _elapsed < _cooldown_hours * 3600:
                                _blocked.append(_gs.underlying)
                        except (ValueError, TypeError):
                            pass
            if _blocked:
                result.pending_underlyings = frozenset(_blocked)
                log.info("[OPTS] Pending/cooling — skip new candidates for: %s",
                         ", ".join(sorted(result.pending_underlyings)))
        except Exception as _pe:
            log.debug("[OPTS] pending_underlyings check failed (non-fatal): %s", _pe)

    except Exception as _recon_err:
        log.warning("[OPTS_RECON] Failed (non-fatal): %s", _recon_err)

    # Untracked-position gate — block new entries for underlyings that have live Alpaca
    # positions not tracked in structures.json. Prevents 42210000 position-intent-mismatch
    # errors (e.g. TSM) where the candidate stage doesn't know positions exist.
    try:
        import re as _re_utp  # noqa: PLC0415

        import options_state as _oss_utp  # noqa: PLC0415
        _live_opts = [
            p for p in alpaca_client.get_all_positions()
            if len(str(getattr(p, "symbol", ""))) > 10
        ]
        # Fix B: sync pnl_unrealized from Alpaca so loss-based exits see current values
        # even when options-chain data is unavailable (illiquid contracts, zero bid).
        try:
            _sync_pnl_from_alpaca_positions(
                open_structs=_oss_utp.get_open_structures(),
                alpaca_positions=_live_opts,
            )
        except Exception as _pnl_sync_err:
            log.debug("[PREFLIGHT] pnl sync from Alpaca failed (non-fatal): %s", _pnl_sync_err)

        if _live_opts:
            # Only consider OCC symbols from ACTIVE structures as "tracked".
            # Closed/cancelled/rejected/expired structures held positions that
            # are no longer monitored — treat those positions as orphans so the
            # reconciler can create orphan_tracked structures and retry closing.
            _INACTIVE_LC = frozenset({"closed", "cancelled", "rejected", "expired"})
            _tracked_occs = {
                leg.occ_symbol
                for s in _oss_utp.load_structures()
                for leg in s.legs
                if getattr(leg, "occ_symbol", None)
                and (
                    s.lifecycle.value if hasattr(s.lifecycle, "value") else str(s.lifecycle)
                ) not in _INACTIVE_LC
            }
            _untracked_under: set[str] = set()
            for _pos in _live_opts:
                _sym = str(getattr(_pos, "symbol", "") or "")
                if _sym and _sym not in _tracked_occs:
                    _m = _re_utp.match(r"^([A-Z]+)\d", _sym)
                    if _m:
                        _untracked_under.add(_m.group(1))
            if _untracked_under:
                result.pending_underlyings = frozenset(
                    result.pending_underlyings | _untracked_under
                )
                log.info(
                    "[PREFLIGHT] Untracked Alpaca positions — blocking new candidates for: %s",
                    sorted(_untracked_under),
                )

            # Orphan reconciler — create monitoring structures for untracked live positions.
            # Runs after the gate so pending_underlyings is always set even if reconciler fails.
            try:
                _reconcile_orphan_positions(
                    live_opts=_live_opts,
                    tracked_occs=_tracked_occs,
                    existing_structures=_oss_utp.load_structures(),
                )
            except Exception as _orp_err:
                log.debug("[PREFLIGHT] Orphan reconciler failed (non-fatal): %s", _orp_err)

    except Exception as _utp_err:
        log.debug("[PREFLIGHT] untracked-position gate failed (non-fatal): %s", _utp_err)

    # Structure count gate — runs after reconciliation so expired/closed structures
    # are already removed from the open-structures list before counting.
    # Suppresses new entries (not the full cycle) so close-check loop still runs.
    try:
        import options_state as _oss_gate  # noqa: PLC0415
        _cfg_path_gate = Path(__file__).parent / "strategy_config.json"
        _cfg_gate = json.loads(_cfg_path_gate.read_text()) if _cfg_path_gate.exists() else {}
        _max_pos = int(_cfg_gate.get("account2", {}).get("max_open_positions", 8))
        _open_count = len(_oss_gate.get_open_structures())
        if _open_count >= _max_pos:
            log.info(
                "[PREFLIGHT] max_open_positions reached (%d/%d) — new entries suppressed",
                _open_count, _max_pos,
            )
            result.pf_allow_new_entries = False
    except Exception as _cnt_err:
        log.debug("[PREFLIGHT] structure count gate failed (non-fatal): %s", _cnt_err)

    return result
