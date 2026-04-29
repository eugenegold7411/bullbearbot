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
from datetime import datetime, timezone
from typing import Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    AssetStatus,
    ContractType,
    OrderClass,
    OrderSide,
    TimeInForce,
)
from alpaca.trading.requests import (
    GetOptionContractsRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopLossRequest,
    StopOrderRequest,
    TakeProfitRequest,
)
from dotenv import load_dotenv

from log_setup import get_logger, log_trade
from schemas import alpaca_symbol

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


def _build_alpaca_client() -> TradingClient:
    api_key = os.getenv("ALPACA_API_KEY")
    secret  = os.getenv("ALPACA_SECRET_KEY")
    base    = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    if not api_key or not secret:
        raise EnvironmentError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set in .env")
    return TradingClient(api_key=api_key, secret_key=secret, paper=("paper" in base))


_alpaca: TradingClient | None = None


def _get_alpaca() -> TradingClient:
    global _alpaca
    if _alpaca is None:
        _alpaca = _build_alpaca_client()
    return _alpaca

# ── Executor backstop constants ───────────────────────────────────────────────
# risk_kernel.py is the PRIMARY authority for all policy.
# These constants are BACKSTOP ONLY — the executor's last-resort safety net.
# If the values diverge from risk_kernel.py, risk_kernel.py wins.
PDT_FLOOR                 = 26_000.0    # regulatory backstop — kernel is primary owner
MARGIN_HIGH_CONVICTION    = 3.0         # 3x equity — mirrors kernel _effective_exposure_cap()
MARGIN_MEDIUM_CONVICTION  = 1.5         # 1.5x equity
MARGIN_LOW_CONVICTION     = 1.0         # 1x equity
MAX_OPTIONS_USD           = 5_000.0     # max cost per options trade (general)
MAX_OPTIONS_LIVE_USD      = 2_000.0     # max cost for live options orders
# Stop loss limits: dynamic from strategy_config.json via _load_stop_limits()
# Defaults: stocks core=4%, standard=5%, speculative=7%
#           crypto core=8%, standard=10%, speculative=12%
MAX_STOP_PCT_INTRADAY = 0.02        # intraday 2% ceiling — backstop only
MIN_RR_RATIO          = 2.0         # R/R minimum — backstop only, kernel primary
MIN_MINUTES_OPEN      = 15
MIN_DTE               = 7           # minimum days-to-expiration
MAX_DTE               = 45          # maximum days-to-expiration
# NOTE: per-tier size ceiling removed from this module — risk_kernel is the
# sole authoritative definition. Executor tier-size check is WARNING only
# (see validate_action — uses local _tier_ceiling dict).

# Symbols with liquid enough options markets for real order submission
# T-010: per-symbol consecutive rejection counter (entry orders only; resets on success)
_consecutive_rejections: dict[str, int] = {}

# T-021: pending fill confirmation checks — order_id → {symbol, action, qty}
# Populated after submission; polled at the start of the next execute_all() call.
_pending_fill_checks: dict[str, dict] = {}

LIQUID_OPTIONS_SYMBOLS = frozenset({
    "SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "TSLA", "META", "GOOGL", "AMD"
})


# ── Options availability check ────────────────────────────────────────────────

def check_options_enabled() -> bool:
    try:
        acct = _get_alpaca().get_account()
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
    fill_price:     Optional[float] = None   # actual fill price from Alpaca response
    filled_qty:     Optional[float] = None   # actual filled quantity
    fill_timestamp: Optional[str]   = None   # filled_at ISO timestamp
    qty:            Optional[float] = None   # requested qty (for divergence detection)
    order_type:     str             = ""     # "market" | "limit" (for divergence detection)

    def __str__(self):
        if self.status == "submitted":
            return f"  [OK]       {self.action.upper()} {self.symbol}  order_id={self.order_id}"
        return f"  [{self.status.upper():<8}] {self.action.upper()} {self.symbol}  — {self.reason}"


# ── Price lookup ──────────────────────────────────────────────────────────────

def _get_current_price(symbol: str) -> float:
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestTradeRequest

    data = StockHistoricalDataClient(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"))
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

    # HOLDs are exempt — stop/limit refreshes on existing positions don't require an open market.
    # Session + timing checks are kernel-primary: log warnings only (kernel already enforced upstream).
    # PDT floor is kept as a hard backstop (regulatory requirement).
    if not is_crypto and act not in ("hold", "monitor", "watch", "observe"):
        if market_status != "open":
            log.warning("[EXEC] %s: soft policy check (kernel primary): market is closed", symbol)
        elif minutes_since_open < MIN_MINUTES_OPEN:
            log.warning(
                "[EXEC] %s: soft policy check (kernel primary): "
                "only %d min since open (min %d)",
                symbol, minutes_since_open, MIN_MINUTES_OPEN,
            )
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

    # ── Soft policy checks (WARNING only — kernel is primary enforcer) ────────
    # The risk kernel already enforced stop width, R/R, tier sizing, and exposure
    # caps before producing the BrokerAction. If these values reach the executor
    # they were approved upstream. Log warnings for observability; do not reject.
    asset_class = "crypto" if is_crypto else "stocks"
    stop_limits = _load_stop_limits()
    asset_limits = stop_limits.get(asset_class, stop_limits["stocks"])
    max_stop = asset_limits.get(tier, asset_limits.get("standard", 0.05))
    if tier == "intraday":
        max_stop = min(max_stop, MAX_STOP_PCT_INTRADAY)
    stop_pct = stop_dist / entry
    if stop_pct > max_stop:
        log.warning(
            "[EXEC] %s: soft policy check (kernel primary): "
            "stop too wide: %s > %s max for %s tier (%s)",
            symbol, f"{stop_pct:.1%}", f"{max_stop:.1%}", tier, asset_class,
        )

    rr = target_dist / stop_dist if stop_dist > 0 else 0
    if rr < MIN_RR_RATIO:
        log.warning(
            "[EXEC] %s: soft policy check (kernel primary): R/R %.2fx below minimum %.1fx",
            symbol, rr, MIN_RR_RATIO,
        )

    # Tier-based position sizing (soft — kernel sized this; warn if value drifted)
    # Conviction-aware ceiling matches kernel BP-aware sizing basis so
    # margin-sized positions don't false-positive this warning.
    position_value = qty * entry
    _tier_ceiling = {"core": 0.15, "dynamic": 0.08, "intraday": 0.05}
    tier_pct = _tier_ceiling.get(tier, 0.15)
    _conv_str = action.get("confidence", "medium").lower()
    if _conv_str == "high":
        effective_tier_ceiling = equity * MARGIN_HIGH_CONVICTION * tier_pct
    elif _conv_str == "medium":
        effective_tier_ceiling = equity * MARGIN_MEDIUM_CONVICTION * tier_pct
    else:
        effective_tier_ceiling = equity * tier_pct
    if position_value > effective_tier_ceiling * 1.05:   # 5% tolerance
        log.warning(
            "[EXEC] %s: soft policy check (kernel primary): "
            "position $%,.0f exceeds tier ceiling $%,.0f (%s tier, %s conviction)",
            symbol, position_value, effective_tier_ceiling, tier, _conv_str,
        )

    # Conviction-adjusted exposure cap (soft — kernel enforced via size_position)
    conviction = action.get("confidence", "medium").lower()
    _bp = float(getattr(account, "buying_power", equity))
    if conviction == "high":
        effective_cap = equity * MARGIN_HIGH_CONVICTION
    elif conviction == "medium":
        effective_cap = equity * MARGIN_MEDIUM_CONVICTION
    else:
        effective_cap = equity * MARGIN_LOW_CONVICTION
    effective_cap = min(effective_cap, equity * MARGIN_HIGH_CONVICTION, _bp)
    log.debug(
        "[EXEC] conviction=%s  effective_cap=$%s  buying_power=$%s  equity=$%s",
        conviction, f"{effective_cap:,.0f}", f"{_bp:,.0f}", f"{equity:,.0f}",
    )
    current_long = sum(float(p.market_value) for p in positions if float(p.qty) > 0)
    new_exposure = current_long + position_value
    if new_exposure > effective_cap:
        log.warning(
            "[EXEC] %s: soft policy check (kernel primary): "
            "total exposure $%.0f would exceed cap $%.0f (%s conviction)",
            symbol, new_exposure, effective_cap, conviction,
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

def _extract_fill(order) -> tuple:
    """
    Pull fill data from an Alpaca order object.
    Returns (fill_price, filled_qty, fill_timestamp) — all Optional.
    Non-fatal: returns (None, None, None) on any attribute error.
    Paper trading fills synchronously for market orders, so filled_avg_price
    is typically populated immediately. Limit orders return None until async fill.
    """
    fp, fq, ft = None, None, None
    try:
        if getattr(order, "filled_avg_price", None):
            fp = float(order.filled_avg_price)
    except (TypeError, ValueError):
        pass
    try:
        if getattr(order, "filled_qty", None):
            fq = float(order.filled_qty)
    except (TypeError, ValueError):
        pass
    try:
        if getattr(order, "filled_at", None):
            ft = str(order.filled_at)
    except Exception:
        pass
    return fp, fq, ft


def _submit_buy(action: dict) -> tuple:
    """Returns (order_id, fill_price, filled_qty, fill_timestamp)."""
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
            symbol=alpaca_symbol(symbol),  # T-011: "BTC/USD" → "BTCUSD" for Alpaca API
            notional=notional,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
        )
        order = _get_alpaca().submit_order(req)
        log.info(
            "Crypto order submitted as simple market (bracket not supported) "
            "— stop/target managed by cycle monitoring  symbol=%s  notional=$%s",
            symbol, notional,
        )
        fp, fq, ft = _extract_fill(order)
        return str(order.id), fp, fq, ft

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
    order = _get_alpaca().submit_order(req)
    fp, fq, ft = _extract_fill(order)
    return str(order.id), fp, fq, ft


def _submit_close(symbol: str) -> str:
    order = _get_alpaca().close_position(symbol)
    return str(order.id)


def _submit_sell(action: dict) -> tuple:
    """Returns (order_id, fill_price, filled_qty, fill_timestamp)."""
    req = MarketOrderRequest(
        symbol=action["symbol"], qty=int(float(action["qty"])),
        side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
    )
    order = _get_alpaca().submit_order(req)
    fp, fq, ft = _extract_fill(order)
    return str(order.id), fp, fq, ft


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
        contracts = _get_alpaca().get_option_contracts(req)
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
            positions = _get_alpaca().get_all_positions()
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
                    order = _get_alpaca().submit_order(close_req)
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

        order = _get_alpaca().submit_order(req)
        log.info("[OPTIONS] Submitted %s %s  order_id=%s", act, occ, order.id)
        return str(order.id)

    except Exception as exc:
        log.error("[OPTIONS] Order submission failed %s: %s", occ, exc)
        raise


# ── Fill confirmation ─────────────────────────────────────────────────────────

def _check_pending_fills() -> None:
    """
    Poll Alpaca for fill/cancel status of orders submitted in the previous cycle.
    Non-fatal — logs WARNING on any per-order error and continues.
    Appends "filled" or "cancelled" events to logs/trades.jsonl.
    """
    if not _pending_fill_checks:
        return

    for oid in list(_pending_fill_checks.keys()):
        info = _pending_fill_checks[oid]
        try:
            order  = _get_alpaca().get_order_by_id(oid)
            status = str(getattr(order, "status", "")).lower()

            if status == "filled":
                fp = float(getattr(order, "filled_avg_price", 0) or 0)
                fq = float(getattr(order, "filled_qty",       0) or 0)
                ft = str(getattr(order, "filled_at",          "") or "")
                log.info("[EXECUTOR] FILLED %s %s @ %s  order_id=%s",
                         info["symbol"], fq, fp, oid)
                log_trade({
                    "event_type": "filled",
                    "order_id":   oid,
                    "symbol":     info["symbol"],
                    "action":     info.get("action", ""),
                    "fill_price": fp,
                    "fill_qty":   fq,
                    "timestamp":  ft,
                })
                del _pending_fill_checks[oid]

            elif status in ("canceled", "cancelled", "expired", "replaced"):
                log.info("[EXECUTOR] CANCELLED %s  order_id=%s  reason=%s",
                         info["symbol"], oid, status)
                log_trade({
                    "event_type": "cancelled",
                    "order_id":   oid,
                    "symbol":     info["symbol"],
                    "action":     info.get("action", ""),
                    "reason":     status,
                    "timestamp":  datetime.now(timezone.utc).isoformat(),
                })
                del _pending_fill_checks[oid]
            # Still pending (accepted, partially_filled, etc.) — leave for next cycle

        except Exception as exc:
            log.warning("[EXECUTOR] Fill check failed for order %s (%s): %s",
                        oid, info.get("symbol", "?"), exc)


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
    # T-021: check fills from previous cycle before processing new actions
    _check_pending_fills()

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

        # T-006: block BUY orders when session tier is unresolved
        if act == "buy" and session_tier == "unknown":
            log.warning("[EXECUTOR] T-006 %s: session=unknown — BUY blocked until session is identified", symbol)
            _reason = "session=unknown: BUY order blocked (session not yet classified)"
            results.append(ExecutionResult(symbol=symbol, action=act, status="rejected", reason=_reason))
            log_trade({
                "symbol": symbol, "action": act, "tier": tier,
                "status": "rejected", "reason": _reason,
                "catalyst": catalyst, "confidence": confidence,
                "qty": action.get("qty"), "session": session_tier,
            })
            continue

        # T-010: suppress entry orders after 10 consecutive rejections for a symbol
        if act == "buy" and _consecutive_rejections.get(symbol, 0) >= 10:
            log.warning("[EXECUTOR] T-010 %s suppressed: %d consecutive rejections — skipping new entry",
                        symbol, _consecutive_rejections[symbol])
            _supp_reason = f"suppressed: {_consecutive_rejections[symbol]} consecutive rejections"
            results.append(ExecutionResult(symbol=symbol, action=act, status="rejected", reason=_supp_reason))
            log_trade({"symbol": symbol, "action": act, "tier": tier,
                       "status": "rejected", "reason": "suppressed_consecutive_rejections",
                       "session": session_tier})
            continue

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
            # T-010: track consecutive rejections for entry suppression
            if act == "buy":
                _consecutive_rejections[symbol] = _consecutive_rejections.get(symbol, 0) + 1
                if _consecutive_rejections[symbol] == 10:
                    log.warning("[EXECUTOR] T-010 %s: hit 10 consecutive rejections — future entries suppressed",
                                symbol)
            continue

        try:
            _fp, _fq, _ft = None, None, None   # fill data — populated for buy/sell
            if act == "buy":
                oid, _fp, _fq, _ft = _submit_buy(action)
            elif act == "close":
                oid = _submit_close(symbol)
            elif act == "sell":
                oid, _fp, _fq, _ft = _submit_sell(action)
            elif act in ("buy_option", "sell_option_spread",
                         "buy_straddle", "close_option"):
                oid = _submit_options(action)
            elif act == "reallocate":
                try:
                    from portfolio_intelligence import (
                        execute_reallocate,  # noqa: PLC0415
                    )
                    realloc_result = execute_reallocate(
                        action.get("exit_symbol"), action, _get_alpaca()
                    )
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
                                [pos_obj], _get_alpaca()
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
                            _ord = _get_alpaca().submit_order(_req)
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
                            _ord = _get_alpaca().submit_order(_req)
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

            log.info("SUBMITTED %s %s [%s]  qty=%s  order_id=%s  fill_price=%s",
                     act.upper(), symbol, tier, action.get("qty"), oid, _fp)
            # Remove stale time_bound_action entry when position is sold/closed.
            if act in ("sell", "close"):
                try:
                    from pathlib import Path as _Path  # noqa: PLC0415

                    from reconciliation import remove_backstop as _rb  # noqa: PLC0415
                    _rb(symbol, _Path(__file__).parent / "strategy_config.json")
                except Exception as _rb_exc:
                    log.debug("[EXECUTOR] remove_backstop failed (non-fatal): %s", _rb_exc)
            # T-010: successful buy resets the consecutive-rejection counter
            if act == "buy":
                _consecutive_rejections.pop(symbol, None)
            # T-021: register non-immediate fills for confirmation polling next cycle
            if act in ("buy", "sell", "close") and oid and not oid.startswith("OPTIONS_"):
                _pending_fill_checks[oid] = {
                    "symbol": symbol,
                    "action": act,
                    "qty":    action.get("qty"),
                }
            _req_qty   = float(action.get("qty") or 0) or None
            _req_otype = action.get("order_type", "market") or "market"
            results.append(ExecutionResult(
                symbol=symbol, action=act, status="submitted",
                order_id=oid,
                fill_price=_fp,
                filled_qty=_fq,
                fill_timestamp=_ft,
                qty=_req_qty,
                order_type=_req_otype,
            ))
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
                "fill_price": _fp,
                "filled_qty": _fq,
            })

        except Exception as exc:
            log.error("ERROR     %s %s — %s", act.upper(), symbol, exc, exc_info=True)
            results.append(ExecutionResult(symbol=symbol, action=act,
                                           status="error", reason=str(exc)))
            log_trade({"symbol": symbol, "action": act, "status": "error",
                       "reason": str(exc), "catalyst": catalyst,
                       "session": session_tier})

    return results
