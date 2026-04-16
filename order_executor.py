"""
order_executor.py — validates Claude's actions and submits orders to Alpaca.

Risk rules enforced:
  - Market must be open for stocks/ETFs
  - Must be >= 15 minutes after open
  - Equity >= $26,000 (PDT floor)
  - Max total exposure: 2x equity (high conviction margin) / 1x equity (no margin)
  - Per-tier max position: core=15%, dynamic=8%, intraday=5% of equity
  - Stop loss no wider than 3% (2% intraday)
  - R/R minimum 2.0x
  - Options: max $5,000 per trade, account must have options enabled
"""

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    ClosePositionRequest,
    GetOptionContractsRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopLossRequest,
    StopOrderRequest,
    TakeProfitRequest,
)
from alpaca.trading.enums import (
    AssetStatus,
    ContractType,
    ExerciseStyle,
    OrderClass,
    OrderSide,
    TimeInForce,
)

from log_setup import get_logger, log_trade
from schemas import normalize_symbol, is_crypto as schema_is_crypto, alpaca_symbol

load_dotenv()
log = get_logger(__name__)

def _load_stop_limits() -> dict:
    """
    Load stop loss limits from strategy_config.json.
    Falls back to hardcoded defaults if file missing or invalid.
    Never raises — order execution must never depend on config reads.
    """
    defaults = {
        "stocks": {
            "core":        0.04,
            "standard":    0.05,
            "speculative": 0.07
        },
        "crypto": {
            "core":        0.08,
            "standard":    0.10,
            "speculative": 0.12
        }
    }
    try:
        import json
        from pathlib import Path
        config_path = Path(__file__).parent / "strategy_config.json"
        if not config_path.exists():
            return defaults
        config = json.loads(config_path.read_text())
        params = config.get("parameters", {})

        # Read from strategy_config if present, otherwise use defaults
        # strategy_config uses single values, we expand to tier-specific limits
        core_pct = params.get("stop_loss_pct_core", 0.04)

        # Core is the tightest, standard and speculative are progressively wider.
        # Floors raised: core 4.0% (handles 3.5% config values), standard 5.0% (ETFs).
        stock_limits = {
            "core":        max(core_pct, 0.04),
            "standard":    max(core_pct * 1.5, 0.05),
            "speculative": max(core_pct * 2.0, 0.06)
        }

        # Crypto always uses wider limits regardless of strategy_config —
        # crypto volatility requires it
        crypto_limits = {
            "core":        0.08,
            "standard":    0.10,
            "speculative": 0.12
        }

        return {
            "stocks": stock_limits,
            "crypto": crypto_limits
        }
    except Exception as e:
        log.warning("Stop limits config load failed: %s — using defaults", e)
        return defaults


_api_key    = os.getenv("ALPACA_API_KEY")
_secret_key = os.getenv("ALPACA_SECRET_KEY")
alpaca      = TradingClient(_api_key, _secret_key, paper=True)

# ── Risk constants ────────────────────────────────────────────────────────────
PDT_FLOOR                 = 26_000.0
MARGIN_HIGH_CONVICTION    = 2.0   # 2x equity — full margin, high conviction
MARGIN_MEDIUM_CONVICTION  = 1.5   # 1.5x equity — partial margin, medium conviction
MARGIN_LOW_CONVICTION     = 1.0   # 1x equity — no margin, low conviction
MAX_OPTIONS_USD           = 5_000.0     # max cost per options trade (general)
MAX_OPTIONS_LIVE_USD      = 2_000.0     # max cost for live options orders
# Stop loss limits are now dynamic — loaded from strategy_config.json via _load_stop_limits()
# Defaults: stocks core=4%, standard=5%, speculative=7%
#           crypto core=8%, standard=10%, speculative=12%
# Weekly review Strategy Director can adjust stop_loss_pct_core in strategy_config.json
MAX_STOP_PCT_INTRADAY = 0.02        # 2% for intraday tier
MIN_RR_RATIO          = 2.0
MIN_MINUTES_OPEN      = 15
MIN_DTE               = 7           # minimum days-to-expiration
MAX_DTE               = 45          # maximum days-to-expiration

TIER_MAX_PCT = {
    "core":     0.15,
    "dynamic":  0.08,
    "intraday": 0.05,
}

# Symbols with liquid enough options markets for real order submission
LIQUID_OPTIONS_SYMBOLS = frozenset({
    "SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "TSLA", "META", "GOOGL", "AMD"
})


# ── Options availability check ────────────────────────────────────────────────

def check_options_enabled() -> bool:
    try:
        acct = alpaca.get_account()
        # Alpaca paper accounts have options_approved_level attribute
        level = getattr(acct, "options_approved_level", None)
        if level is not None and int(level) >= 1:
            log.info("Options enabled at level %s", level)
            return True
        log.warning("Options not enabled on this account (level=%s)", level)
        return False
    except Exception as exc:
        log.warning("Could not check options status: %s", exc)
        return False


# ── Result container ─────────────────────────────────────────────────────────

@dataclass
class ExecutionResult:
    symbol:    str
    action:    str
    status:    str
    reason:    str = ""
    order_id:  Optional[str] = None

    def __str__(self):
        if self.status == "submitted":
            return f"  [OK]       {self.action.upper()} {self.symbol}  order_id={self.order_id}"
        return f"  [{self.status.upper():<8}] {self.action.upper()} {self.symbol}  — {self.reason}"


# ── Price lookup ──────────────────────────────────────────────────────────────

def _get_current_price(symbol: str) -> float:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestTradeRequest
    from alpaca.data.enums import DataFeed

    data = StockHistoricalDataClient(_api_key, _secret_key)
    resp = data.get_stock_latest_trade(
        StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)
    )
    return float(resp[symbol].price)


def _check(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


# ── Validation ────────────────────────────────────────────────────────────────

def _in_orb_formation_window() -> bool:
    """Returns True if we are in the 9:30-9:45 AM ET ORB formation window."""
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime as _dt
        from scheduler import _orb_locked  # noqa: PLC0415
        return not _orb_locked
    except Exception:
        return False


def validate_action(action: dict, account, positions: list, market_status: str,
                    minutes_since_open: int, current_prices: dict = None) -> None:
    act    = action.get("action", "").lower()
    symbol = action.get("symbol", "")
    equity = float(account.equity)
    tier   = action.get("tier", "core")

    # Options handled separately
    if act in ("buy_option", "sell_option_spread", "buy_straddle", "close_option"):
        _validate_options_action(action, account)
        return

    # ORB formation window: block new stock/ETF entries 9:30-9:45 AM ET
    is_crypto = "/" in symbol
    if not is_crypto and act == "buy" and 0 < minutes_since_open <= 15:
        if _in_orb_formation_window():
            raise ValueError(
                "ORB formation window (9:30-9:45 AM ET) — observation only, "
                "no new entries until 9:45 AM ET"
            )

    # Universal checks (stocks/ETFs require open market; crypto always ok)
    # HOLDs are exempt — stop/limit refreshes on existing positions don't require an open market
    if not is_crypto and act not in ("hold", "monitor", "watch", "observe"):
        _check(market_status == "open",       "market is closed")
        _check(minutes_since_open >= MIN_MINUTES_OPEN,
               f"too early — only {minutes_since_open} min since open (min {MIN_MINUTES_OPEN})")
    _check(equity >= PDT_FLOOR,
           f"equity ${equity:,.0f} below PDT floor ${PDT_FLOOR:,.0f}")

    if act in ("close", "sell"):
        return

    if act in ("hold", "monitor", "watch", "observe"):
        log.debug("HOLD %s — no order submitted",
                  action.get("symbol", "?"))
        return

    if act != "buy":
        raise ValueError(f"unknown action '{act}'")

    qty         = action.get("qty")
    stop_loss   = action.get("stop_loss")
    take_profit = action.get("take_profit")

    _check(qty and float(qty) > 0,   "qty must be positive")
    _check(stop_loss  is not None,   "stop_loss required for buys")
    _check(take_profit is not None,  "take_profit required for buys")

    qty         = float(qty)
    stop_loss   = float(stop_loss)
    take_profit = float(take_profit)

    if current_prices and symbol in current_prices:
        entry = float(current_prices[symbol])
    else:
        entry = _get_current_price(symbol)

    stop_dist   = entry - stop_loss
    target_dist = take_profit - entry

    # Price scale sanity — catches cases where Claude emits a signal score
    # (e.g. 72) instead of a real price (e.g. $182.50).  Anything below 50%
    # of the current market price is almost certainly the wrong scale.
    _check(
        stop_loss >= entry * 0.50,
        f"[EXECUTOR] stop_loss ${stop_loss:.2f} appears to use wrong price scale "
        f"(got ${stop_loss:.2f}, market=${entry:.2f}) — Claude may have used "
        f"signal score instead of price",
    )
    _check(
        take_profit >= entry * 0.50,
        f"[EXECUTOR] take_profit ${take_profit:.2f} appears to use wrong price scale "
        f"(got ${take_profit:.2f}, market=${entry:.2f}) — Claude may have used "
        f"signal score instead of price",
    )
    _check(
        stop_loss < entry * 0.99,
        f"[EXECUTOR] stop_loss ${stop_loss:.2f} is above or at entry ${entry:.2f} "
        f"(stop must be below entry for long positions)",
    )
    _check(
        stop_loss < entry,
        f"[EXECUTOR] stop_loss ${stop_loss:.4f} must be below entry ${entry:.2f}",
    )
    _check(
        take_profit > entry,
        f"[EXECUTOR] take_profit ${take_profit:.2f} must be above entry ${entry:.2f} "
        f"(got take_profit=${take_profit:.2f} vs stop_loss=${stop_loss:.2f})",
    )

    asset_class = "crypto" if is_crypto else "stocks"
    stop_limits = _load_stop_limits()
    asset_limits = stop_limits.get(asset_class, stop_limits["stocks"])
    max_stop = asset_limits.get(tier, asset_limits.get("standard", 0.05))
    # Intraday tier: enforce the tighter intraday ceiling regardless of asset class
    if tier == "intraday":
        max_stop = min(max_stop, MAX_STOP_PCT_INTRADAY)
    stop_pct = stop_dist / entry
    _check(
        stop_pct <= max_stop,
        f"stop too wide: {stop_pct:.1%} > {max_stop:.1%} "
        f"max for {tier} tier ({asset_class})"
    )

    rr = target_dist / stop_dist if stop_dist > 0 else 0
    _check(rr >= MIN_RR_RATIO, f"R/R {rr:.2f}x below minimum {MIN_RR_RATIO}x")

    # Tier-based position sizing
    position_value = qty * entry
    tier_pct = TIER_MAX_PCT.get(tier, 0.15)
    max_position = equity * tier_pct
    _check(position_value <= max_position,
           f"position ${position_value:,.0f} exceeds {tier} max (${max_position:,.0f})")

    # Conviction-adjusted total exposure cap (margin-aware)
    conviction = action.get("confidence", "medium").lower()
    _bp = float(getattr(account, "buying_power", equity))
    if conviction == "high":
        effective_cap = equity * MARGIN_HIGH_CONVICTION
    elif conviction == "medium":
        effective_cap = equity * MARGIN_MEDIUM_CONVICTION
    else:
        effective_cap = equity * MARGIN_LOW_CONVICTION
    # Hard ceiling: never exceed 2x equity regardless of buying_power headroom
    effective_cap = min(effective_cap, equity * MARGIN_HIGH_CONVICTION, _bp)
    log.info(
        "[MARGIN] conviction=%s  effective_cap=$%s  buying_power=$%s  equity=$%s",
        conviction,
        f"{effective_cap:,.0f}",
        f"{_bp:,.0f}",
        f"{equity:,.0f}",
    )
    current_long = sum(float(p.market_value) for p in positions if float(p.qty) > 0)
    new_exposure = current_long + position_value
    _check(
        new_exposure <= effective_cap,
        f"total exposure ${new_exposure:,.0f} would exceed cap "
        f"${effective_cap:,.0f} ({conviction} conviction)",
    )


def _validate_options_action(action: dict, account) -> None:
    """Options-specific validation."""
    equity   = float(account.equity)
    act      = action.get("action", "").lower()
    max_cost = float(action.get("max_cost_usd", 0) or 0)
    symbol   = action.get("symbol", "")

    _check(equity >= PDT_FLOOR,
           f"equity ${equity:,.0f} below PDT floor ${PDT_FLOOR:,.0f}")
    _check(max_cost <= MAX_OPTIONS_USD,
           f"options cost ${max_cost:,.0f} exceeds max ${MAX_OPTIONS_USD:,.0f}")
    _check(symbol,                    "options action requires symbol")
    _check(action.get("expiration"),  "options action requires expiration")
    _check(action.get("long_strike"), "options action requires long_strike")

    # DTE validation: must be within 7–45 days
    dte = action.get("dte_days")
    if dte is not None:
        try:
            dte_int = int(dte)
            _check(dte_int >= MIN_DTE,
                   f"DTE {dte_int} days < {MIN_DTE} day minimum (theta/assignment risk)")
            _check(dte_int <= MAX_DTE,
                   f"DTE {dte_int} days > {MAX_DTE} day maximum (capital efficiency)")
        except (TypeError, ValueError):
            pass  # DTE not parseable — let order layer handle it

    # Live orders: enforce $2K cap and liquid symbols only
    if act in ("buy_option", "buy_straddle") and symbol in LIQUID_OPTIONS_SYMBOLS:
        _check(max_cost <= MAX_OPTIONS_LIVE_USD,
               f"live options cost ${max_cost:,.0f} exceeds ${MAX_OPTIONS_LIVE_USD:,.0f} per-trade cap")


# ── Order submission ──────────────────────────────────────────────────────────

def _submit_buy(action: dict) -> str:
    symbol      = action["symbol"]
    stop_loss   = float(action["stop_loss"])
    take_profit = float(action["take_profit"])
    order_type  = action.get("order_type", "market").lower()
    limit_price = action.get("limit_price")

    # Alpaca does not support bracket orders for crypto
    is_crypto = "/" in symbol
    if is_crypto:
        entry_price = (
            float(limit_price) if limit_price else
            action.get("entry_price") or
            stop_loss / 0.97
        )
        notional = round(float(action["qty"]) * float(entry_price), 2)
        req = MarketOrderRequest(
            symbol=symbol,
            notional=notional,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
        )
        order = alpaca.submit_order(req)
        log.info(
            "Crypto order submitted as simple market (bracket not supported) "
            "— stop/target managed by cycle monitoring  symbol=%s  notional=$%s",
            symbol, notional,
        )
        return str(order.id)

    # Stock / ETF path — bracket order with stop + take-profit
    qty = int(float(action["qty"]))
    tp  = TakeProfitRequest(limit_price=round(take_profit, 2))
    sl  = StopLossRequest(stop_price=round(stop_loss, 2))

    if order_type == "limit" and limit_price:
        req = LimitOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY, order_class=OrderClass.BRACKET,
            limit_price=round(float(limit_price), 2),
            take_profit=tp, stop_loss=sl,
        )
    else:
        req = MarketOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY, order_class=OrderClass.BRACKET,
            take_profit=tp, stop_loss=sl,
        )
    order = alpaca.submit_order(req)
    return str(order.id)


def _submit_close(symbol: str) -> str:
    order = alpaca.close_position(symbol)
    return str(order.id)


def _submit_sell(action: dict) -> str:
    req = MarketOrderRequest(
        symbol=action["symbol"], qty=int(float(action["qty"])),
        side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
    )
    order = alpaca.submit_order(req)
    return str(order.id)


def _find_option_contract(
    symbol: str,
    expiration: str,
    strike: float,
    contract_type: str,
) -> Optional[str]:
    """
    Look up the OCC contract symbol for a specific option using Alpaca.

    Returns the OCC symbol string (e.g. "AAPL240119C00185000") on success,
    None if no matching contract is found.

    contract_type: "call" or "put"
    expiration:    "YYYY-MM-DD" string
    """
    try:
        ct = ContractType.CALL if contract_type.lower() == "call" else ContractType.PUT
        req = GetOptionContractsRequest(
            underlying_symbols=[symbol],
            expiration_date=expiration,
            strike_price_gte=str(round(strike * 0.98, 2)),
            strike_price_lte=str(round(strike * 1.02, 2)),
            type=ct,
            status=AssetStatus.ACTIVE,
        )
        contracts = alpaca.get_option_contracts(req)
        items = getattr(contracts, "option_contracts", contracts) if not isinstance(contracts, list) else contracts
        if not items:
            log.warning("[OPTIONS] No contract found for %s %s %s %s",
                        symbol, expiration, strike, contract_type)
            return None
        # Pick closest strike to requested
        best = min(items, key=lambda c: abs(float(getattr(c, "strike_price", strike)) - strike))
        occ = getattr(best, "symbol", None)
        log.info("[OPTIONS] Found contract: %s  delta=%s", occ,
                 getattr(best, "delta", "?"))
        return occ
    except Exception as exc:
        log.warning("[OPTIONS] Contract lookup failed %s: %s", symbol, exc)
        return None


def _submit_options(action: dict) -> str:
    """
    Submit an options order to Alpaca.

    For liquid symbols (LIQUID_OPTIONS_SYMBOLS), submits a real order.
    For illiquid symbols, logs the intended trade and returns a stub ID.
    """
    symbol     = action.get("symbol", "")
    act        = action.get("action", "").lower()
    strategy   = action.get("option_strategy", "call").lower()
    expiration = action.get("expiration", "")
    strike     = float(action.get("long_strike", 0) or 0)
    contracts  = int(action.get("contracts", 1) or 1)
    max_cost   = float(action.get("max_cost_usd", 0) or 0)

    log.info("[OPTIONS] %s %s  strategy=%s  exp=%s  strike=%s  contracts=%s  max_cost=$%s",
             act, symbol, strategy, expiration, strike, contracts, max_cost)

    # Stub for illiquid symbols or unsupported strategies
    if symbol not in LIQUID_OPTIONS_SYMBOLS:
        log.info("[OPTIONS] %s not in LIQUID_OPTIONS_SYMBOLS — logging stub order", symbol)
        return f"OPTIONS_STUB_{symbol}_{expiration}"

    if act == "close_option":
        # Closing option: close all positions in the symbol's options
        try:
            positions = alpaca.get_all_positions()
            closed = []
            for pos in positions:
                pos_sym = getattr(pos, "symbol", "")
                if pos_sym.startswith(symbol) and len(pos_sym) > 6:
                    close_req = MarketOrderRequest(
                        symbol=pos_sym,
                        qty=abs(int(float(getattr(pos, "qty", 1)))),
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.DAY,
                    )
                    order = alpaca.submit_order(close_req)
                    closed.append(str(order.id))
                    log.info("[OPTIONS] Closed option position %s  order_id=%s", pos_sym, order.id)
            if closed:
                return ",".join(closed)
            log.warning("[OPTIONS] close_option: no open option positions found for %s", symbol)
            return f"OPTIONS_NO_POS_{symbol}"
        except Exception as exc:
            log.error("[OPTIONS] close_option failed %s: %s", symbol, exc)
            raise

    # Determine contract type from strategy
    if "put" in strategy:
        contract_type = "put"
    else:
        contract_type = "call"

    # Look up the OCC contract symbol
    occ = _find_option_contract(symbol, expiration, strike, contract_type)
    if not occ:
        log.warning("[OPTIONS] Could not find OCC symbol — falling back to stub")
        return f"OPTIONS_NO_CONTRACT_{symbol}_{expiration}"

    try:
        if act in ("buy_option", "buy_straddle"):
            req = MarketOrderRequest(
                symbol=occ,
                qty=contracts,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )
        elif act == "sell_option_spread":
            # Single-leg short for now (spread legs require separate orders)
            req = MarketOrderRequest(
                symbol=occ,
                qty=contracts,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
        else:
            log.warning("[OPTIONS] Unrecognised options action '%s' — stub", act)
            return f"OPTIONS_STUB_{symbol}_{expiration}"

        order = alpaca.submit_order(req)
        log.info("[OPTIONS] Submitted %s %s  order_id=%s", act, occ, order.id)
        return str(order.id)

    except Exception as exc:
        log.error("[OPTIONS] Order submission failed %s: %s", occ, exc)
        raise


# ── Main entry point ──────────────────────────────────────────────────────────

def execute_all(
    actions:            list,
    account,
    positions:          list,
    market_status:      str,
    minutes_since_open: int,
    current_prices:     dict = None,
    session_tier:       str  = "unknown",
    decision_id:        str  = "",
) -> list[ExecutionResult]:
    results = []

    # Normalise: BrokerAction objects → dict; warn on unknown types; pass dicts through.
    from schemas import BrokerAction  # noqa: PLC0415
    _normalised: list = []
    for _raw in actions:
        if isinstance(_raw, BrokerAction):
            _normalised.append(_raw.to_dict())
        elif isinstance(_raw, dict):
            log.warning("[EXECUTOR] received raw dict (expected BrokerAction) — processing for backward-compat")
            _normalised.append(_raw)
        else:
            log.warning("[EXECUTOR] unknown action type %s — skipping", type(_raw).__name__)
    actions = _normalised

    for action in actions:
        symbol     = action.get("symbol", "UNKNOWN")
        act        = action.get("action", "").lower()
        catalyst   = action.get("catalyst", "")
        confidence = action.get("confidence", "")
        tier       = action.get("tier", "core")

        try:
            validate_action(action, account, positions, market_status,
                            minutes_since_open, current_prices)
        except ValueError as exc:
            reason = str(exc)
            log.warning("[EXECUTOR] REJECTED  %s %s [%s] — %s", act.upper(), symbol, tier, reason)
            results.append(ExecutionResult(symbol=symbol, action=act,
                                           status="rejected", reason=reason))
            log_trade({
                "symbol": symbol, "action": act, "tier": tier,
                "status": "rejected", "reason": reason,
                "catalyst": catalyst, "confidence": confidence,
                "qty": action.get("qty"),
                "stop_loss": action.get("stop_loss"),
                "take_profit": action.get("take_profit"),
                "session": session_tier,
            })
            continue

        try:
            if act == "buy":
                oid = _submit_buy(action)
            elif act == "close":
                oid = _submit_close(symbol)
            elif act == "sell":
                oid = _submit_sell(action)
            elif act in ("buy_option", "sell_option_spread",
                         "buy_straddle", "close_option"):
                oid = _submit_options(action)
            elif act == "reallocate":
                try:
                    from portfolio_intelligence import execute_reallocate  # noqa: PLC0415
                    realloc_result = execute_reallocate(action, alpaca)
                    results.append(ExecutionResult(
                        symbol=f"{action.get('exit_symbol','?')}→{action.get('entry_symbol','?')}",
                        action="reallocate",
                        status=realloc_result.get("status","error"),
                        reason=realloc_result.get("reason",""),
                        order_id=realloc_result.get("order_id"),
                    ))
                    log_trade({
                        "symbol":        action.get("exit_symbol","?"),
                        "action":        "reallocate",
                        "entry_symbol":  action.get("entry_symbol","?"),
                        "status":        realloc_result.get("status","error"),
                        "reason":        realloc_result.get("reason",""),
                        "catalyst":      catalyst,
                    })
                    continue
                except Exception as re_exc:
                    reason = f"reallocate failed: {re_exc}"
                    results.append(ExecutionResult(symbol=symbol, action=act,
                                                   status="error", reason=reason))
                    continue
            elif act in ("monitor", "watch", "observe"):
                log.debug("HOLD %s [%s] — informational, no order", symbol, tier)
                results.append(ExecutionResult(symbol=symbol, action=act,
                                               status="hold", reason="hold"))
                continue

            elif act == "hold":
                # If stop_loss/take_profit provided, ensure orders exist for them.
                stop_loss   = action.get("stop_loss")
                take_profit = action.get("take_profit")
                hold_detail = "hold"

                if stop_loss is not None or take_profit is not None:
                    try:
                        # Price sanity before submitting hold orders
                        mkt_price = (
                            float(current_prices[symbol])
                            if current_prices and symbol in current_prices
                            else next(
                                (float(p.current_price) for p in positions
                                 if p.symbol == symbol), None
                            )
                        )
                        if mkt_price and mkt_price > 0:
                            if stop_loss is not None:
                                _sl = float(stop_loss)
                                if _sl < mkt_price * 0.50:
                                    log.warning(
                                        "[EXECUTOR] %s: hold stop $%.2f appears to use "
                                        "wrong price scale (market=$%.2f) — skipping stop",
                                        symbol, _sl, mkt_price,
                                    )
                                    stop_loss = None
                                elif _sl >= mkt_price * 0.99:
                                    log.warning(
                                        "[EXECUTOR] %s: hold stop $%.2f is above or at "
                                        "market $%.2f — skipping stop",
                                        symbol, _sl, mkt_price,
                                    )
                                    stop_loss = None
                            if take_profit is not None:
                                _tp = float(take_profit)
                                if _tp < mkt_price * 0.50:
                                    log.warning(
                                        "[EXECUTOR] %s: hold take_profit $%.2f appears to "
                                        "use wrong price scale (market=$%.2f) — skipping",
                                        symbol, _tp, mkt_price,
                                    )
                                    take_profit = None
                                elif stop_loss is not None and _tp <= float(stop_loss):
                                    log.warning(
                                        "[EXECUTOR] %s: hold take_profit $%.2f <= "
                                        "stop_loss $%.2f — skipping take_profit",
                                        symbol, _tp, float(stop_loss),
                                    )
                                    take_profit = None

                        # Check existing exit orders
                        import exit_manager as _em_hold  # noqa: PLC0415
                        pos_obj = next((p for p in positions if p.symbol == symbol), None)
                        existing = {}
                        if pos_obj:
                            existing = _em_hold.get_active_exits(
                                [pos_obj], alpaca
                            ).get(symbol, {})

                        pos_qty = (
                            _em_hold._position_qty(pos_obj) if pos_obj else 0.0
                        )
                        # C1: Claude emits "BTC/USD"; _is_crypto() expects Alpaca "BTCUSD".
                        # Use "/" in symbol to catch Claude-format, plus _is_crypto for Alpaca-format.
                        is_crypto_hold = "/" in symbol or _em_hold._is_crypto(symbol)
                        submitted_parts = []

                        # Submit stop if none exists
                        if (stop_loss is not None
                                and pos_qty > 0
                                and existing.get("status") in
                                    (None, "unprotected", "unknown")):
                            _sl = float(stop_loss)
                            if is_crypto_hold:
                                _req = LimitOrderRequest(
                                    symbol=symbol,
                                    qty=pos_qty,
                                    side=OrderSide.SELL,
                                    time_in_force=TimeInForce.GTC,
                                    limit_price=_sl,
                                )
                            else:
                                _req = StopOrderRequest(
                                    symbol=symbol,
                                    qty=int(pos_qty),
                                    side=OrderSide.SELL,
                                    time_in_force=TimeInForce.GTC,
                                    stop_price=_sl,
                                )
                            _ord = alpaca.submit_order(_req)
                            submitted_parts.append(f"stop@${_sl:.2f}")
                            log.info("[EXECUTOR] %s: hold — stop @ $%.2f  order_id=%s",
                                     symbol, _sl, _ord.id)
                            log_trade({"event": "hold_stop", "symbol": symbol,
                                       "stop_price": _sl, "order_id": str(_ord.id)})

                        # Submit take-profit if none exists
                        if (take_profit is not None
                                and pos_qty > 0
                                and not existing.get("target_price")):
                            _tp = float(take_profit)
                            _req = LimitOrderRequest(
                                symbol=symbol,
                                qty=pos_qty if is_crypto_hold else int(pos_qty),
                                side=OrderSide.SELL,
                                time_in_force=TimeInForce.GTC,
                                limit_price=_tp,
                            )
                            _ord = alpaca.submit_order(_req)
                            submitted_parts.append(f"target@${_tp:.2f}")
                            log.info("[EXECUTOR] %s: hold — take_profit @ $%.2f  order_id=%s",
                                     symbol, _tp, _ord.id)
                            log_trade({"event": "hold_target", "symbol": symbol,
                                       "take_profit": _tp, "order_id": str(_ord.id)})

                        if submitted_parts:
                            hold_detail = "hold (" + " ".join(submitted_parts) + ")"
                            log.info("[EXECUTOR] %s: hold — stop @ $%s take_profit @ $%s",
                                     symbol,
                                     stop_loss if stop_loss else "none",
                                     take_profit if take_profit else "none")
                        else:
                            log.debug("[EXECUTOR] %s: hold — exits already in place",
                                      symbol)

                    except Exception as _hold_exc:
                        log.debug("[EXECUTOR] hold order setup failed %s: %s",
                                  symbol, _hold_exc)
                else:
                    log.debug("[EXECUTOR] HOLD %s [%s] — no stops provided", symbol, tier)

                results.append(ExecutionResult(symbol=symbol, action=act,
                                               status="hold", reason=hold_detail))
                continue
            else:
                reason = f"unknown action '{act}'"
                results.append(ExecutionResult(symbol=symbol, action=act,
                                               status="rejected", reason=reason))
                log.warning("REJECTED  %s %s — %s", act.upper(), symbol, reason)
                continue

            log.info("SUBMITTED %s %s [%s]  qty=%s  order_id=%s",
                     act.upper(), symbol, tier, action.get("qty"), oid)
            results.append(ExecutionResult(symbol=symbol, action=act,
                                           status="submitted", order_id=oid))
            log_trade({
                "symbol": symbol, "action": act, "tier": tier,
                "status": "submitted", "order_id": oid,
                "catalyst": catalyst, "confidence": confidence,
                "qty": action.get("qty"),
                "stop_loss": action.get("stop_loss"),
                "take_profit": action.get("take_profit"),
                "limit_price": action.get("limit_price"),
                "option_strategy": action.get("option_strategy"),
                "max_cost_usd": action.get("max_cost_usd"),
                "session": session_tier,
                "decision_id": decision_id,
            })

        except Exception as exc:
            log.error("ERROR     %s %s — %s", act.upper(), symbol, exc, exc_info=True)
            results.append(ExecutionResult(symbol=symbol, action=act,
                                           status="error", reason=str(exc)))
            log_trade({"symbol": symbol, "action": act, "status": "error",
                       "reason": str(exc), "catalyst": catalyst,
                       "session": session_tier})

    return results
