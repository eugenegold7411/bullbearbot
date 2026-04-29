"""
exit_manager.py — Automatic exit management for open positions.

Responsibilities every cycle (called pre-Claude, post-PI):
  1. Audit open bracket orders — detect UNPROTECTED positions
  2. Refresh stale stops (stop > refresh_if_stop_stale_pct below current price)
  3. Trail profitable stops to breakeven + trail_to_breakeven_plus_pct
     when profit >= trail_trigger_r × stop distance
  4. Return a formatted section for Claude's prompt

All operations are non-fatal: a crash here never blocks a cycle.
Logs at INFO level with [EXIT_MGR] and [TRAIL_STOP] prefixes.
"""

from __future__ import annotations

import os
import threading
from typing import Optional

from dotenv import load_dotenv

from log_setup import get_logger, log_trade

load_dotenv()
log = get_logger(__name__)


def _get_eda(sym: str, strategy_config: dict) -> Optional[int]:  # noqa: ARG001
    """Return days-to-earnings for sym from earnings_calendar.json. None if not found."""
    try:
        import json as _json  # noqa: PLC0415
        from datetime import date as _date  # noqa: PLC0415
        from pathlib import Path as _Path  # noqa: PLC0415
        cal_path = _Path("data/market/earnings_calendar.json")
        if not cal_path.exists():
            return None
        cal = _json.loads(cal_path.read_text())
        today = _date.today()
        for entry in cal.get("calendar", []):
            if entry.get("symbol") == sym:
                iso = str(entry.get("earnings_date", ""))[:10]
                try:
                    return (_date.fromisoformat(iso) - today).days
                except Exception:
                    pass
        return None
    except Exception:
        return None


def _get_latest_iv(sym: str) -> Optional[float]:
    """Return most recent IV for sym from iv_history file. None if unavailable."""
    try:
        import json as _json  # noqa: PLC0415
        from pathlib import Path as _Path  # noqa: PLC0415
        path = _Path("data/options/iv_history") / f"{sym}_iv_history.json"
        if not path.exists():
            return None
        data = _json.loads(path.read_text())
        if not data:
            return None
        latest = data[-1]
        iv_val = float(latest.get("iv") or latest.get("iv_rank") or 0)
        return iv_val if iv_val > 0 else None
    except Exception:
        return None


# ── Crypto symbol detection ───────────────────────────────────────────────────
# _is_crypto() below handles Alpaca-format symbols (BTCUSD).
# schema_is_crypto() handles any format via normalize_symbol().
_CRYPTO_BASE = {
    "BTC", "ETH", "SOL", "DOGE", "AVAX", "MATIC", "LTC", "XRP", "ADA", "DOT",
    "LINK", "UNI", "AAVE", "ALGO", "ATOM", "FIL", "NEAR", "SHIB",
}


# NOTE: _is_crypto() is designed for Alpaca position object symbols
# (e.g. "BTCUSD", "ETHUSD"). For Claude-emitted action symbols
# (e.g. "BTC/USD", "ETH/USD"), use: "/" in symbol
# Do NOT call _is_crypto() with Claude-emitted symbols.
def _is_crypto(symbol: str) -> bool:
    """Return True for Alpaca crypto symbols like BTCUSD, ETHUSD (no separator)."""
    if symbol.endswith("USD") and len(symbol) > 3:
        return symbol[:-3] in _CRYPTO_BASE
    return False


def _position_qty(position) -> float:
    """
    Return tradeable qty: fractional float for crypto, integer for stocks.
    Always positive.
    """
    raw = abs(float(position.qty))
    if _is_crypto(position.symbol):
        return round(raw, 9)
    return float(abs(int(raw)))


def _has_stop_order(symbol: str, open_orders: list, is_short: bool = False) -> bool:
    """
    Return True if any order in open_orders is a protective stop for symbol.

    For long positions (is_short=False): looks for sell-stop orders.
    For short positions (is_short=True): looks for buy-stop orders (cover-on-rise).

    Handles both raw Alpaca order objects (o.type = "OrderType.STOP") and
    NormalizedOrder objects (o.order_type = "stop") via flexible attribute lookup
    and enum-prefix stripping.
    """
    expected_side = "buy" if is_short else "sell"
    for order in open_orders:
        order_symbol = getattr(order, "symbol", "")
        if order_symbol != symbol and order_symbol != symbol.replace("/", ""):
            continue
        raw_type = str(
            getattr(order, "type", getattr(order, "order_type", ""))
        ).lower()
        order_type = raw_type.split(".")[-1]
        order_side = str(getattr(order, "side", "")).lower().split(".")[-1]
        if order_side == expected_side and order_type in ("stop", "stop_limit", "trailing_stop"):
            return True
    return False


def _has_take_profit_order(symbol: str, open_orders: list) -> bool:
    """
    Return True if any order in open_orders is a limit sell for symbol.

    Uses the same flexible attribute lookup as _has_stop_order().
    """
    for order in open_orders:
        order_symbol = getattr(order, "symbol", "")
        if order_symbol != symbol and order_symbol != symbol.replace("/", ""):
            continue
        raw_type = str(
            getattr(order, "type", getattr(order, "order_type", ""))
        ).lower()
        order_type = raw_type.split(".")[-1]
        order_side = str(getattr(order, "side", "")).lower().split(".")[-1]
        if order_side == "sell" and order_type == "limit":
            return True
    return False


# ── Per-ticker lock — prevents duplicate exit-order submissions ───────────────
# Guards the check-and-submit sequence in refresh_exits_for_position() so that
# two concurrent callers for the same symbol cannot both pass the
# "is_unprotected" gate and both submit a stop order.
_ticker_locks: dict[str, threading.Lock] = {}
_ticker_locks_guard = threading.Lock()

# Consecutive trail-stop replace failure counter, keyed by stop order ID.
# After trail_replace_max_failures consecutive failures the replace is abandoned
# so a stuck PENDING_REPLACE order does not generate a warning every cycle.
# Cleared on success or when a new stop order ID appears for the same symbol.
_trail_replace_failures: dict[str, int] = {}


def _get_ticker_lock(symbol: str) -> threading.Lock:
    """Return (or lazily create) the threading.Lock for a given ticker symbol."""
    with _ticker_locks_guard:
        if symbol not in _ticker_locks:
            _ticker_locks[symbol] = threading.Lock()
        return _ticker_locks[symbol]


# ── Config defaults (overridden by strategy_config["exit_management"]) ────────
_DEFAULT_CFG = {
    "trail_stop_enabled":           True,
    "trail_trigger_r":              1.0,    # trail when profit >= 1× stop distance
    "trail_to_breakeven_plus_pct":  0.005,  # trail stop to entry + 0.5%
    "refresh_if_stop_stale_pct":    0.15,   # refresh if stop >15% below current price
    "backstop_days":                7,      # new-entry backstop horizon (calendar days)
    "trail_cancel_replace_enabled": True,   # S8: cancel-and-replace when 42210000
}


def _em_config(strategy_config: dict) -> dict:
    base = dict(_DEFAULT_CFG)
    base.update(strategy_config.get("exit_management", {}))
    return base


# ── Alpaca helpers ────────────────────────────────────────────────────────────

def _open_orders_by_symbol(alpaca_client) -> dict[str, list]:
    """Return {symbol: [order, ...]} for all open sell-side orders, including bracket legs."""
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    try:
        orders = alpaca_client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=200)
        )
        result: dict[str, list] = {}
        for o in (orders if isinstance(orders, list) else []):
            sym = getattr(o, "symbol", "")
            result.setdefault(sym, []).append(o)
            # Bracket legs may be nested under the parent rather than returned
            # as top-level orders — index them too so get_active_exits() sees them.
            for leg in (getattr(o, "legs", None) or []):
                leg_sym = getattr(leg, "symbol", "") or sym
                result.setdefault(leg_sym, []).append(leg)
        return result
    except Exception as exc:
        log.debug("[EXIT_MGR] get_orders failed: %s", exc)
        return {}


# ── 1. Audit exits ────────────────────────────────────────────────────────────

def get_active_exits(positions: list, alpaca_client=None) -> dict[str, dict]:
    """
    For each open position, inspect open sell orders to determine protection status.

    Returns {symbol: {"stop_price", "target_price", "stop_order_id",
                       "target_order_id", "status": protected|partial|unprotected}}
    """
    from alpaca.trading.client import TradingClient
    client = alpaca_client or TradingClient(
        os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"), paper=True
    )
    orders_by_sym = _open_orders_by_symbol(client)
    result: dict[str, dict] = {}

    for pos in positions:
        if float(pos.qty) == 0:
            continue
        is_short    = float(pos.qty) < 0
        sym         = pos.symbol
        cur_price   = float(pos.current_price)
        open_orders = orders_by_sym.get(sym, [])

        stop_price        = None
        stop_oid          = None
        stop_order_status = None   # captured from already-fetched order list; no extra API call
        target_price      = None
        target_oid        = None
        any_sell_oid      = None  # fallback for long positions: any sell order counts as protection

        # Protective orders are buy-side for shorts, sell-side for longs.
        protective_side = "buy" if is_short else "sell"

        for o in open_orders:
            o_type   = str(getattr(o, "type",   "")).lower()
            o_side   = str(getattr(o, "side",   "")).lower()
            o_status = str(getattr(o, "status", "")).lower()
            # Normalize Alpaca enum repr: "OrderType.STOP" → "stop", "OrderSide.SELL" → "sell"
            o_type   = o_type.split(".")[-1]
            o_side   = o_side.split(".")[-1]
            o_status = o_status.split(".")[-1]
            if protective_side not in o_side:
                continue
            if not is_short and any_sell_oid is None:
                any_sell_oid = str(o.id)
            if o_type in ("stop", "stop_limit"):
                sp = getattr(o, "stop_price", None)
                if sp:
                    stop_price        = float(sp)
                    stop_oid          = str(o.id)
                    stop_order_status = o_status
            elif o_type == "limit" and not is_short:
                lp = getattr(o, "limit_price", None)
                if lp:
                    lp_f = float(lp)
                    # Above-market limit = take-profit; below-market = stop-limit leg
                    if lp_f > cur_price * 0.99:
                        target_price = float(lp)
                        target_oid   = str(o.id)
                    elif stop_price is None:
                        # Below-market sell limit (bracket stop-limit leg) — treat as stop
                        stop_price = lp_f
                        stop_oid   = str(o.id)

        if is_short:
            # Short positions: a buy-stop is full protection; no TP tracking here.
            status = "partial" if stop_price else "unprotected"
        elif stop_price and target_price:
            status = "protected"
        elif stop_price:
            status = "partial"
        elif target_price and not stop_price:
            # BUG-009: take-profit limit is visible but no stop found in open-order
            # queries. Alpaca bracket stop-loss children use a non-"open" OCA status
            # (held/accepted) and are invisible to status=OPEN queries. Flag as
            # "tp_only" so refresh_exits_for_position() cancels the TP and places a
            # SIMPLE stop instead.
            log.warning(
                "[EXIT_MGR] %s: take-profit order %s visible but no stop in "
                "open orders — status=tp_only (will cancel TP and place SIMPLE stop)",
                sym, target_oid,
            )
            status = "tp_only"
        elif any_sell_oid:
            # Found a sell order we couldn't classify (e.g., bracket leg with
            # unexpected type) — still counts as protection; skip refresh.
            log.debug("[EXIT_MGR] %s: found existing exit order %s — skipping",
                      sym, any_sell_oid)
            status = "partial"
        else:
            status = "unprotected"

        result[sym] = {
            "stop_price":        stop_price,
            "target_price":      target_price,
            "stop_order_id":     stop_oid,
            "stop_order_status": stop_order_status,
            "target_order_id":   target_oid,
            "status":            status,
        }

    return result


# ── 2. Generate exit plan ─────────────────────────────────────────────────────

def generate_exit_plan(
    position,
    current_price: float,
    strategy_config: dict,
    conviction: str = "medium",
) -> dict:
    """
    Compute stop_loss and take_profit for a position based on current price.

    Trails the stop up when in profit (uses current_price as base, not entry).
    conviction: "high" = wider stop (more room), "medium" = standard, "low" = tighter.
    """
    params   = strategy_config.get("parameters", {})
    tier     = str(getattr(position, "tier", None) or "core").lower()
    is_intra = "intraday" in tier

    base_stop_pct = float(
        params.get("stop_loss_pct_intraday", 0.02) if is_intra
        else params.get("stop_loss_pct_core", 0.035)
    )
    conv_factor = {"high": 1.2, "medium": 1.0, "low": 0.8}.get(
        conviction.lower(), 1.0
    )
    stop_pct = base_stop_pct * conv_factor

    take_profit_multiple = float(params.get("take_profit_multiple", 2.5))
    entry_price  = float(position.avg_entry_price)
    unrealized   = float(position.unrealized_pl)

    # Trail: use current price as base when in profit
    if unrealized > 0 and current_price > entry_price:
        stop_base = current_price
        rationale = f"trailing stop {stop_pct:.1%} below current ${current_price:.2f}"
    else:
        stop_base = entry_price
        rationale = f"standard stop {stop_pct:.1%} below entry ${entry_price:.2f}"

    stop_loss   = round(stop_base * (1 - stop_pct), 2)
    stop_dist   = current_price - stop_loss
    take_profit = round(current_price + stop_dist * take_profit_multiple, 2)
    target_pct  = round((take_profit - current_price) / current_price * 100, 2)

    return {
        "stop_loss":   stop_loss,
        "take_profit": take_profit,
        "stop_pct":    round(stop_pct * 100, 2),
        "target_pct":  target_pct,
        "rationale":   rationale,
    }


# ── 3. Refresh stale exits ────────────────────────────────────────────────────

def refresh_exits_for_position(
    position,
    alpaca_client,
    strategy_config: dict,
    conviction: str = "medium",
    exit_info: Optional[dict] = None,
) -> bool:
    """
    Submit a fresh stop-loss order when a position is UNPROTECTED or has a
    stop more than refresh_if_stop_stale_pct below the current price.

    Cancels the stale stop order first if one exists.
    Returns True if a new stop was successfully submitted.
    """
    em_cfg = _em_config(strategy_config)
    sym    = position.symbol
    qty    = _position_qty(position)
    price  = float(position.current_price)

    if qty <= 0:
        log.warning("[EXIT_MGR] %s: qty=%s — skipping (zero qty, check position)", sym, qty)
        return False

    # Acquire per-ticker lock (non-blocking). If a concurrent call is already
    # processing this symbol, skip rather than submitting a duplicate order.
    _lock = _get_ticker_lock(sym)
    if not _lock.acquire(blocking=False):
        log.debug("[EXIT_MGR] %s: concurrent exit submission in progress — skipping", sym)
        return False

    try:
        return _refresh_exits_locked(
            position, alpaca_client, strategy_config, conviction, exit_info,
            sym, qty, price, em_cfg,
        )
    finally:
        _lock.release()


def _refresh_exits_locked(
    position,
    alpaca_client,
    strategy_config: dict,
    conviction: str,
    exit_info: Optional[dict],
    sym: str,
    qty: float,
    price: float,
    em_cfg: dict,
) -> bool:
    """Inner implementation of refresh_exits_for_position, called under per-ticker lock."""
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest, StopOrderRequest

    ei = exit_info if exit_info is not None else (
        get_active_exits([position], alpaca_client).get(sym, {})
    )

    stop_price     = ei.get("stop_price")
    ei_status      = ei.get("status", "unknown")
    is_tp_only     = ei_status == "tp_only"
    is_unprotected = ei_status in ("unprotected", "unknown") or is_tp_only
    stale_threshold = em_cfg["refresh_if_stop_stale_pct"]
    is_stale = (
        stop_price is not None
        and price > 0
        and (price - stop_price) / price > stale_threshold
    )

    if not (is_unprotected or is_stale):
        return False

    reason = (
        "TP_ONLY (take-profit visible, stop missing — BUG-009)" if is_tp_only
        else "UNPROTECTED" if ei_status in ("unprotected", "unknown")
        else f"stale stop ${stop_price:.2f} ({(price-stop_price)/price:.1%} below current)"
    )
    log.info("[EXIT_MGR] %s: refreshing exits — %s", sym, reason)

    # BUG-009: for tp_only positions, cancel the take-profit order first so Alpaca
    # releases the held-share lock (error 40310000) before we place the stop.
    _skip_tp_resubmit = False
    if is_tp_only and ei.get("target_order_id"):
        try:
            alpaca_client.cancel_order_by_id(ei["target_order_id"])
            log.info(
                "[EXIT_MGR] %s: cancelled tp_only order %s to free shares for stop",
                sym, ei["target_order_id"],
            )
            _skip_tp_resubmit = True
        except Exception as exc:
            log.warning(
                "[EXIT_MGR] %s: cancel tp_only order failed: %s — stop placement may fail",
                sym, exc,
            )

    # Cancel stale stop if present
    if is_stale and ei.get("stop_order_id"):
        try:
            alpaca_client.cancel_order_by_id(ei["stop_order_id"])
            log.info("[EXIT_MGR] %s: cancelled stale stop order %s",
                     sym, ei["stop_order_id"])
        except Exception as exc:
            log.debug("[EXIT_MGR] %s: cancel stale stop failed: %s", sym, exc)

    plan = generate_exit_plan(position, price, strategy_config, conviction)

    try:
        if _is_crypto(sym):
            # Alpaca does not support StopOrderRequest for crypto — use a limit
            # sell at the stop price instead (executes if price falls to that level).
            stop_req = LimitOrderRequest(
                symbol=sym,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                limit_price=plan["stop_loss"],
            )
            stop_order = alpaca_client.submit_order(stop_req)
            log.info(
                "[EXIT_MGR] %s: crypto limit-stop submitted @ $%.4f  order_id=%s",
                sym, plan["stop_loss"], stop_order.id,
            )
        else:
            stop_req = StopOrderRequest(
                symbol=sym,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                stop_price=plan["stop_loss"],
            )
            stop_order = alpaca_client.submit_order(stop_req)
            log.info(
                "[EXIT_MGR] %s: stop order submitted — stop=$%.2f  order_id=%s",
                sym, plan["stop_loss"], stop_order.id,
            )
        log_trade({
            "event":      "exit_refresh_stop",
            "symbol":     sym,
            "reason":     reason,
            "stop_price": plan["stop_loss"],
            "order_id":   str(stop_order.id),
        })
    except Exception as exc:
        log.warning("[EXIT_MGR] %s: stop order submission failed: %s", sym, exc)
        return False

    # Also submit a take-profit limit order (separate from the stop since the
    # original bracket is gone — GLD-style one-sided protection fix).
    # Skip when we just cancelled the TP (tp_only path): the stop now holds the
    # shares and Alpaca will reject any additional sell (error 40310000).
    if _skip_tp_resubmit:
        log.info(
            "[EXIT_MGR] %s: skipping TP re-submit after tp_only cancel"
            " — stop placed, TP blocked by Alpaca 40310000",
            sym,
        )
    else:
        try:
            tp_req = LimitOrderRequest(
                symbol=sym,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                limit_price=plan["take_profit"],
            )
            tp_order = alpaca_client.submit_order(tp_req)
            log.info(
                "[EXIT_MGR] %s: take-profit limit submitted — target=$%.2f  order_id=%s",
                sym, plan["take_profit"], tp_order.id,
            )
            log_trade({
                "event":      "exit_refresh_target",
                "symbol":     sym,
                "target":     plan["take_profit"],
                "order_id":   str(tp_order.id),
            })
        except Exception as exc:
            log.warning("[EXIT_MGR] %s: take-profit order submission failed: %s", sym, exc)
            # Stop is already placed — still counts as a successful refresh

    return True


# ── 4. Trail stop ─────────────────────────────────────────────────────────────

def _trail_cancel_and_replace(
    alpaca_client,
    position,
    stop_oid: str,
    new_stop: float,
    sym: str,
    em_cfg: dict,
) -> bool:
    """
    Cancel-and-replace fallback for trail stops stuck in Alpaca 'accepted' status.

    Called when replace_order fails with 42210000 (cannot replace accepted order).
    Cancels the existing stop, then places a fresh GTC stop at new_stop.
    Returns True if the new stop was successfully placed.

    Failure count shares _trail_replace_failures[stop_oid] with the in-place path —
    after trail_replace_max_failures total attempts the trail is abandoned.
    If cancel succeeds but placement fails, the position is temporarily unprotected;
    run_exit_manager() will detect this on the next cycle and re-place a backstop.
    """
    from alpaca.trading.enums import OrderSide, TimeInForce  # noqa: PLC0415
    from alpaca.trading.requests import (  # noqa: PLC0415
        LimitOrderRequest,
        StopOrderRequest,
    )

    max_failures    = int(em_cfg.get("trail_replace_max_failures", 3))
    failures_so_far = _trail_replace_failures.get(stop_oid, 0)

    if failures_so_far >= max_failures:
        log.warning(
            "[TRAIL_STOP] %s: cancel-and-replace abandoned after %d failures (order_id=%s)",
            sym, max_failures, stop_oid,
        )
        return False

    qty = _position_qty(position)

    # Step 1: cancel the accepted-status stop to free the order slot.
    try:
        alpaca_client.cancel_order_by_id(stop_oid)
        log.info(
            "[TRAIL_STOP] %s: cancelled accepted-status stop %s for cancel-and-replace",
            sym, stop_oid,
        )
    except Exception as exc:
        new_count = failures_so_far + 1
        _trail_replace_failures[stop_oid] = new_count
        log.warning(
            "[TRAIL_STOP] %s: cancel-and-replace cancel step failed (attempt %d/%d): %s",
            sym, new_count, max_failures, exc,
        )
        return False

    # Step 2: place a fresh stop at the trail target price.
    try:
        if _is_crypto(sym):
            stop_req = LimitOrderRequest(
                symbol=sym, qty=qty, side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC, limit_price=new_stop,
            )
        else:
            stop_req = StopOrderRequest(
                symbol=sym, qty=qty, side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC, stop_price=new_stop,
            )
        new_order = alpaca_client.submit_order(stop_req)
        _trail_replace_failures.pop(stop_oid, None)   # success — clear counter
        log.info(
            "[TRAIL_STOP] %s: cancel-and-replace succeeded — new stop $%.2f  order_id=%s",
            sym, new_stop, new_order.id,
        )
        log_trade({
            "event":          "trail_stop_cancel_replace",
            "symbol":         sym,
            "old_stop_id":    stop_oid,
            "new_stop_price": new_stop,
            "order_id":       str(new_order.id),
        })
        return True
    except Exception as exc:
        new_count = failures_so_far + 1
        _trail_replace_failures[stop_oid] = new_count
        log.warning(
            "[TRAIL_STOP] %s: cancel-and-replace place step failed (attempt %d/%d): %s — "
            "position temporarily unprotected; exit manager will re-place stop next cycle",
            sym, new_count, max_failures, exc,
        )
        return False


def maybe_trail_stop(
    position,
    alpaca_client,
    strategy_config: dict,
    exit_info: Optional[dict] = None,
) -> bool:
    """
    If position profit >= trail_trigger_r × stop distance, trail stop to
    entry + trail_to_breakeven_plus_pct. Returns True if trail was applied.
    """
    em_cfg = _em_config(strategy_config)
    if not em_cfg.get("trail_stop_enabled", True):
        return False

    sym         = position.symbol
    entry_price = float(position.avg_entry_price)
    current     = float(position.current_price)
    unreal      = float(position.unrealized_pl)

    if unreal <= 0 or current <= entry_price:
        return False

    ei         = exit_info if exit_info is not None else (
        get_active_exits([position], alpaca_client).get(sym, {})
    )
    stop_price = ei.get("stop_price")
    stop_oid   = ei.get("stop_order_id")

    if stop_price is None:
        return False

    stop_dist = entry_price - stop_price
    if stop_dist <= 0:
        return False

    trigger_r = em_cfg.get("trail_trigger_r", 1.0)
    profit_r  = (current - entry_price) / stop_dist

    if profit_r < trigger_r:
        return False

    plus_pct = em_cfg.get("trail_to_breakeven_plus_pct", 0.005)
    new_stop = round(entry_price * (1 + plus_pct), 2)

    # Earnings-aware stop floor: when earnings are imminent, replace the tight
    # trail target with a wider IV-based floor so the position isn't stopped out
    # by the earnings-day volatility swing.
    if em_cfg.get("earnings_aware_stop_enabled", False):
        eda = _get_eda(sym, strategy_config)
        eda_trigger = int(em_cfg.get("earnings_stop_eda_trigger", 1))
        if eda is not None and 0 <= eda <= eda_trigger:
            iv = _get_latest_iv(sym)
            iv_floor = float(em_cfg.get("earnings_stop_iv_floor_pct", 0.05))
            if iv is not None and iv > 0:
                expected_move_pct = max(iv, iv_floor)
                earnings_floor = round(entry_price * (1 - expected_move_pct), 2)
                if earnings_floor > stop_price:
                    log.info(
                        "[EARNINGS_STOP] %s eda=%d: widening stop $%.2f → $%.2f (IV=%.1f%%)",
                        sym, eda, new_stop, earnings_floor, iv * 100,
                    )
                    new_stop = earnings_floor

    if new_stop <= stop_price:
        return False  # existing stop already at or better than trail target

    log.info(
        "[TRAIL_STOP] %s: profit_r=%.2fx ≥ trigger=%.1fx — trailing "
        "stop $%.2f → $%.2f (breakeven+%.1f%%)",
        sym, profit_r, trigger_r, stop_price, new_stop, plus_pct * 100,
    )

    if stop_oid:
        # Skip if the stop order is mid-replace — status from already-cached order
        # list, no extra Alpaca API call.
        if ei.get("stop_order_status") == "pending_replace":
            log.debug(
                "[TRAIL_STOP] %s: stop order %s is PENDING_REPLACE — skipping this cycle",
                sym, stop_oid,
            )
            return False

        max_failures = int(em_cfg.get("trail_replace_max_failures", 3))
        if _trail_replace_failures.get(stop_oid, 0) >= max_failures:
            log.debug(
                "[TRAIL_STOP] %s: replace abandoned after %d failures (order_id=%s)",
                sym, max_failures, stop_oid,
            )
            return False

        try:
            from alpaca.trading.requests import ReplaceOrderRequest  # noqa: PLC0415
            alpaca_client.replace_order_by_id(
                stop_oid, ReplaceOrderRequest(stop_price=new_stop)
            )
            log.info("[TRAIL_STOP] %s: stop updated $%.2f → $%.2f  order_id=%s",
                     sym, stop_price, new_stop, stop_oid)
            _trail_replace_failures.pop(stop_oid, None)   # success — clear failure count
            log_trade({
                "event":    "trail_stop",
                "symbol":   sym,
                "old_stop": stop_price,
                "new_stop": new_stop,
                "profit_r": round(profit_r, 2),
                "order_id": stop_oid,
            })
            return True
        except Exception as exc:
            if "42210000" in str(exc) and em_cfg.get("trail_cancel_replace_enabled", True):
                if _trail_cancel_and_replace(
                    alpaca_client, position, stop_oid, new_stop, sym, em_cfg
                ):
                    return True
            else:
                failures = _trail_replace_failures.get(stop_oid, 0) + 1
                _trail_replace_failures[stop_oid] = failures
                log.warning("[TRAIL_STOP] %s: replace_order failed (attempt %d/%d): %s",
                            sym, failures, max_failures, exc)
                if failures >= max_failures:
                    log.warning(
                        "[TRAIL_STOP] %s: trail stop replace abandoned after %d consecutive "
                        "failures (order_id=%s) — manual review needed",
                        sym, max_failures, stop_oid,
                    )
    return False


# ── 5. Master orchestrator ────────────────────────────────────────────────────

def run_exit_manager(
    positions: list,
    alpaca_client,
    strategy_config: dict,
) -> list[dict]:
    """
    Audit + fix exits for all open positions. Returns list of actions taken.
    Never raises — all exceptions caught and logged.
    """
    if not positions:
        return []

    actions_taken: list[dict] = []

    try:
        exits = get_active_exits(positions, alpaca_client)
    except Exception as exc:
        log.debug("[EXIT_MGR] get_active_exits failed: %s", exc)
        exits = {}

    for pos in positions:
        if float(pos.qty) == 0:
            continue
        is_short = float(pos.qty) < 0
        sym = pos.symbol
        ei  = exits.get(sym, {"status": "unknown"})

        # Log per-position protection status so each cycle is auditable.
        _status = ei.get("status", "unknown")
        _stop   = ei.get("stop_price")
        _tp     = ei.get("target_price")
        if _status == "protected":
            log.info("[EXIT_MGR] %s: fully protected — stop=$%s  target=$%s",
                     sym, _stop, _tp)
        elif _status == "partial":
            log.info("[EXIT_MGR] %s: stop protected, no take profit — OK"
                     "  stop=$%s", sym, _stop)
        elif _status == "tp_only":
            log.warning("[EXIT_MGR] %s: take-profit visible, no stop — will repair"
                        "  target=$%s", sym, _tp)
        elif _status in ("unprotected", "unknown"):
            log.warning("[EXIT_MGR] %s: UNPROTECTED — no stop order found", sym)

        if is_short:
            log.info("[EXIT_MGR] %s: SHORT position (qty=%.0f) — auto-management skipped",
                     sym, float(pos.qty))
            continue

        # Refresh if UNPROTECTED / tp_only / stale
        try:
            refreshed = refresh_exits_for_position(
                pos, alpaca_client, strategy_config, exit_info=ei
            )
            if refreshed:
                actions_taken.append({
                    "symbol": sym, "action": "refresh_exits",
                    "detail": f"was {ei.get('status','?')}",
                })
        except Exception as exc:
            log.debug("[EXIT_MGR] refresh_exits failed %s: %s", sym, exc)

        # Trail if sufficiently profitable
        try:
            trailed = maybe_trail_stop(pos, alpaca_client, strategy_config, exit_info=ei)
            if trailed:
                actions_taken.append({
                    "symbol": sym, "action": "trail_stop",
                    "detail": "trailed to breakeven+",
                })
        except Exception as exc:
            log.debug("[EXIT_MGR] trail_stop failed %s: %s", sym, exc)

    if actions_taken:
        log.info("[EXIT_MGR] %d action(s) taken: %s",
                 len(actions_taken), [a["symbol"] for a in actions_taken])
    return actions_taken


# ── Prompt formatter ──────────────────────────────────────────────────────────

def format_exit_status_section(
    positions: list,
    alpaca_client,
    strategy_config: dict,
) -> str:
    """
    Build the EXIT STATUS block for Claude's prompt.
    Per-position: stop level, target, % distance to each, protection status.
    """
    if not positions:
        return "  (no open positions)"

    try:
        exits = get_active_exits(positions, alpaca_client)
    except Exception:
        exits = {}

    lines = []
    for pos in positions:
        if float(pos.qty) == 0:
            continue
        sym     = pos.symbol
        current = float(pos.current_price)
        entry   = float(pos.avg_entry_price)
        qty     = float(pos.qty)
        is_short = qty < 0
        unreal  = float(pos.unrealized_pl)
        sign    = "+" if unreal >= 0 else ""
        cost    = abs(entry * qty)
        pnl_pct = round(unreal / cost * 100, 1) if cost > 0 else 0.0
        ei      = exits.get(sym, {})

        stop    = ei.get("stop_price")
        target  = ei.get("target_price")
        status  = ei.get("status", "unknown")

        stop_str = (
            f"stop=${stop:.2f} ({(current-stop)/current*100:+.1f}% away)"
            if stop else "stop=NONE"
        )
        tgt_str = (
            f"target=${target:.2f} ({(target-current)/current*100:+.1f}% away)"
            if target else "target=NONE"
        )
        pnl_str = f"P&L={sign}${unreal:.0f} ({sign}{pnl_pct:.1f}%)"

        side_label = "[SHORT]" if is_short else ""
        flag = ""
        if status == "unprotected":
            flag = "  *** UNPROTECTED — NO STOP LOSS ***"
        elif status == "tp_only":
            flag = "  *** TP_ONLY — NO STOP (bracket stop invisible — will repair) ***"
        elif status == "partial":
            flag = "  ! partial protection (stop only)"

        lines.append(
            f"  {sym:<8}  {stop_str}  {tgt_str}  {pnl_str}  [{status.upper()}]{side_label}{flag}"
        )

    return "\n".join(lines) if lines else "  (no positions)"
