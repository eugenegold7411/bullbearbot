"""
trade_journal.py — Closed-trade P&L journal with bug-period annotations.

Public API
----------
build_closed_trades(orders=None, decisions=None) -> list[dict]
build_bug_fix_log() -> list[dict]
KNOWN_BUG_PERIODS  — list of bug period dicts
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_BOT_DIR = Path(__file__).resolve().parent

# ── Known bug periods that affected trade execution / P&L ────────────────────
KNOWN_BUG_PERIODS: list[dict] = [
    {
        "id": "BUG-OCA-001",
        "title": "Bracket orders silently unprotected (stop coverage gap)",
        "description": (
            "Alpaca bracket orders produce two OCA children after fill — the stop-loss "
            "child enters 'held' status and is invisible to status=open queries. "
            "exit_manager saw only the take-profit limit child and classified positions "
            "as 'partial'. All bracket-entry positions held no active stop-loss for up to "
            "18 hours. AMZN and XBI were affected."
        ),
        "severity": "HIGH",
        "start": "2026-04-13",
        "end": "2026-04-15",
        "trading_impact": True,
        "affects_symbols": ["AMZN", "XBI"],
        "resolution": (
            "Added tp_only status detection; refresh_exits_for_position treats "
            "tp_only as unprotected — cancels TP first, then places SIMPLE stop."
        ),
    },
    {
        "id": "BUG-007",
        "title": "Trail stop never fired (enum serialization bug)",
        "description": (
            "OrderType.STOP serialized as 'ordertype.stop' not 'stop'. "
            "The string comparison in get_active_exits() always failed, so stop_price "
            "stayed None and trail stops never updated. GLD was the primary affected "
            "position — its stop was stuck at $429.11 instead of $435.66 (breakeven+0.5%)."
        ),
        "severity": "HIGH",
        "start": "2026-04-13",
        "end": "2026-04-15",
        "trading_impact": True,
        "affects_symbols": ["GLD"],
        "resolution": (
            "Added .split('.')[-1] normalization for OrderType and OrderSide "
            "string values immediately after str(...).lower()."
        ),
    },
    {
        "id": "BUG-DENOM-001",
        "title": "Position sizing denominator error (utilization overcount)",
        "description": (
            "compute_position_health(), SIZE TRIM trigger, and account_pct used wrong "
            "denominator for utilization. Equity-only denominator overstated utilization "
            "when cash/buying-power was high, triggering SIZE TRIM signals prematurely "
            "and causing incorrect position sizing throughout the first 17 trading days."
        ),
        "severity": "MEDIUM",
        "start": "2026-04-13",
        "end": "2026-04-30",
        "trading_impact": True,
        "affects_symbols": [],  # all symbols affected
        "resolution": (
            "Fixed denominator to total_capacity = deployed + buying_power in "
            "portfolio_intelligence.py and allocator. Formula: utilization = "
            "deployed / (deployed + buying_power)."
        ),
    },
    {
        "id": "BUG-008",
        "title": "BTC/USD HOLD emitting signal score as stop_loss",
        "description": (
            "Claude occasionally copied the signal score integer (0–100) into the "
            "stop_loss field of a BTC/USD HOLD action. The price-scale guard in "
            "order_executor.py caught and discarded values < $1,000 for crypto, but "
            "affected cycles had no valid stop refresh for BTC/USD."
        ),
        "severity": "MEDIUM",
        "start": "2026-04-13",
        "end": "2026-04-15",
        "trading_impact": True,
        "affects_symbols": ["BTC/USD", "ETH/USD"],
        "resolution": (
            "Added post-processing validation pass in bot.py after ask_claude(). "
            "For crypto with stop_loss < 1000, recalculates as current_price × 0.92."
        ),
    },
    {
        "id": "BUG-014",
        "title": "Deadline exits used limit order instead of market order",
        "description": (
            "When diff_state() detected an expired deadline (CRITICAL priority), it emitted "
            "action_type='close_all'. execute_reconciliation_plan() placed a limit order — "
            "not guaranteed to fill at deadline. Additionally, an open stop-loss OCA share-lock "
            "could block the new sell order."
        ),
        "severity": "MEDIUM",
        "start": "2026-04-13",
        "end": "2026-04-15",
        "trading_impact": True,
        "affects_symbols": ["TSM"],  # TSM had a time-bound exit deadline
        "resolution": (
            "diff_state() now emits action_type='deadline_exit_market'. New "
            "_execute_deadline_exit() helper cancels all open orders first, then "
            "submits MarketOrderRequest (DAY for equity, GTC for crypto)."
        ),
    },
    {
        "id": "BUG-009",
        "title": "Bracket stop/TP child legs silently voided by Alpaca paper trading",
        "description": (
            "Alpaca paper trading silently voids bracket order child legs (stop-loss "
            "and take-profit) after the parent fills. Positions entered with bracket "
            "orders had no active stop protection. Fallback logic now places a standalone "
            "GTC stop after a confirmed fill."
        ),
        "severity": "HIGH",
        "start": "2026-04-13",
        "end": "2026-05-01",
        "trading_impact": True,
        "affects_symbols": [],
        "resolution": (
            "Fallback places standalone GTC stop after confirmed fill_price is not None. "
            "Bracket child-leg voiding is a known Alpaca paper-trading limitation."
        ),
    },
    {
        "id": "BUG-016",
        "title": "Orphaned stops placed when bracket BUY was cancelled",
        "description": (
            "When a bracket BUY order was cancelled (e.g. due to insufficient buying "
            "power or a duplicate-block), the fallback stop-placement logic still ran "
            "and submitted a GTC stop sell even though no position was ever opened. "
            "This left orphaned stop orders in the account."
        ),
        "severity": "MEDIUM",
        "start": "2026-04-15",
        "end": "2026-05-01",
        "trading_impact": True,
        "affects_symbols": [],
        "resolution": (
            "Fallback stop placement gated on confirmed fill_price is not None. "
            "Cancelled orders with no fill_price are now ignored."
        ),
    },
    {
        "id": "BUG-PENDING-REPLACE",
        "title": "Trail stop advances used replace_order causing PENDING_REPLACE state",
        "description": (
            "When the trail-stop advance logic tried to move a stop higher, it used "
            "replace_order() on the existing stop. Alpaca paper trading enters a "
            "PENDING_REPLACE state that can persist indefinitely, blocking further "
            "modifications and leaving the stop at the old price."
        ),
        "severity": "MEDIUM",
        "start": "2026-04-13",
        "end": "2026-05-01",
        "trading_impact": True,
        "affects_symbols": [],
        "resolution": (
            "Switched to cancel + resubmit pattern for trail-stop advances. "
            "The old stop is cancelled first, then a new GTC stop is placed at the "
            "updated price."
        ),
    },
    {
        "id": "BUG-CONCURRENT-SELL",
        "title": "Bracket BUYs submitted while concurrent market sell pending",
        "description": (
            "The execution loop could submit a bracket BUY for a symbol while a "
            "market SELL for the same symbol was still pending fill. This created "
            "conflicting orders and could result in duplicate positions."
        ),
        "severity": "MEDIUM",
        "start": "2026-04-13",
        "end": "2026-05-01",
        "trading_impact": True,
        "affects_symbols": [],
        "resolution": (
            "Serialized execution: sells are submitted and waited for fill "
            "confirmation before buys are submitted for the same symbol."
        ),
    },
    {
        "id": "BUG-MAX-TOKENS",
        "title": "max_tokens=2048 caused Sonnet JSON truncation",
        "description": (
            "The main Sonnet decision call had max_tokens=2048 which was insufficient "
            "for full JSON responses when the watchlist or position list was long. "
            "Truncated JSON caused parse failures, fell through to repair fallback, "
            "and occasionally resulted in no-trade cycles where trades were warranted."
        ),
        "severity": "MEDIUM",
        "start": "2026-04-13",
        "end": "2026-05-01",
        "trading_impact": True,
        "affects_symbols": [],
        "resolution": "Raised max_tokens to 4096 and added JSON repair fallback.",
    },
    {
        "id": "BUG-A2-CONFIG",
        "title": "config={} passed to A2 build_structure()",
        "description": (
            "The A2 structure builder was called with an empty config dict "
            "(config={}) at three call sites, stripping all A2 strategy parameters "
            "including max_cost_usd limits, allowed strategies, and position-sizing "
            "constraints. Structures may have been built without proper guardrails."
        ),
        "severity": "MEDIUM",
        "start": "2026-04-13",
        "end": "2026-05-01",
        "trading_impact": True,
        "affects_symbols": [],
        "resolution": (
            "Fixed all three call sites to pass the full _a2_cfg config object."
        ),
    },
    {
        "id": "BUG-A2-CURRENT-PRICES",
        "title": "current_prices={} passed to A2 close_check_loop — P&L exits blind",
        "description": (
            "The A2 close_check_loop() was called with current_prices={} (empty dict) "
            "instead of live option prices. All P&L-based exit checks (80% max-gain "
            "and 50% max-loss auto-close thresholds) evaluated against zero prices, "
            "making them effectively blind. Positions that should have closed did not."
        ),
        "severity": "HIGH",
        "start": "2026-04-13",
        "end": "2026-05-01",
        "trading_impact": True,
        "affects_symbols": [],
        "resolution": (
            "Injected live option prices into close_check_loop() from the preflight "
            "data fetch."
        ),
    },
    {
        "id": "BUG-A2-CANCEL-COOLDOWN",
        "title": "Cancelled A2 spreads resubmit every cycle indefinitely",
        "description": (
            "When an A2 spread order was cancelled (e.g. unfillable price), the "
            "structure stayed in submitted state and the debate/build cycle would "
            "select and resubmit the same structure on the next cycle. This could "
            "flood the account with repeated cancelled orders for the same spread."
        ),
        "severity": "MEDIUM",
        "start": "2026-04-13",
        "end": "2026-05-01",
        "trading_impact": True,
        "affects_symbols": [],
        "resolution": (
            "Added per-structure cancel cooldown. Cancelled structures are marked "
            "with a cooldown timestamp and blocked from resubmission for 30 minutes."
        ),
    },
    {
        "id": "BUG-OVERSIZE-DENOMINATOR",
        "title": "Sonnet used buying_power as oversize denominator causing churn",
        "description": (
            "The position health check passed to Sonnet used buying_power as the "
            "denominator for percent-of-account calculations. When buying_power was "
            "low (many positions open), all positions appeared oversize and Sonnet "
            "generated TRIM signals every cycle, creating a churn loop."
        ),
        "severity": "MEDIUM",
        "start": "2026-04-13",
        "end": "2026-05-01",
        "trading_impact": True,
        "affects_symbols": [],
        "resolution": (
            "Fixed denominator to total_capacity (deployed + buying_power) in "
            "format_positions_with_health(). Added 15% threshold for oversize flag."
        ),
    },
]


# ── Public helpers ────────────────────────────────────────────────────────────

def build_bug_fix_log() -> list[dict]:
    """Return known bug periods sorted by severity (HIGH first) then start date."""
    severity_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    return sorted(
        KNOWN_BUG_PERIODS,
        key=lambda b: (severity_order.get(b["severity"], 9), b["start"]),
    )


def build_closed_trades(
    orders: Optional[list] = None,
    decisions: Optional[list] = None,
) -> list[dict]:
    """
    Build closed-trade records from order history.

    Parameters
    ----------
    orders : list, optional
        Alpaca order objects or normalised dicts. If None, fetches from Alpaca API.
    decisions : list, optional
        Decisions list from memory/decisions.json. If None, reads from disk.

    Returns
    -------
    list[dict]  — one record per matched BUY→SELL round-trip, sorted by
                  exit_time descending (most recent first).
    """
    if orders is None:
        orders = _fetch_alpaca_orders()
    if decisions is None:
        decisions = _load_decisions()

    buys, sells = _parse_orders(orders)

    _ts = lambda x: x["filled_at"] or datetime.min.replace(tzinfo=timezone.utc)
    buys.sort(key=_ts)
    sells.sort(key=_ts)

    # FIFO matching per symbol
    pending: dict[str, list[dict]] = {}
    for buy in buys:
        pending.setdefault(buy["symbol"], []).append(buy)

    closed: list[dict] = []
    for sell in sells:
        sym = sell["symbol"]
        queue = pending.get(sym, [])
        if not queue:
            continue
        buy = queue.pop(0)

        entry_t = buy["filled_at"]
        exit_t = sell["filled_at"]
        qty = min(buy["qty"], sell["qty"])
        pnl = round((sell["price"] - buy["price"]) * qty, 2)
        pnl_pct = round((sell["price"] - buy["price"]) / buy["price"] * 100, 2) if buy["price"] else 0.0
        holding_days = max(0, (exit_t - entry_t).days) if entry_t and exit_t else None

        decision, action = (
            _find_entry_decision(sym, entry_t, decisions) if entry_t else (None, None)
        )

        closed.append({
            "symbol": sym,
            "entry_price": buy["price"],
            "exit_price": sell["price"],
            "qty": qty,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "outcome": "win" if pnl > 0 else ("loss" if pnl < 0 else "flat"),
            "entry_time": entry_t.isoformat() if entry_t else None,
            "exit_time": exit_t.isoformat() if exit_t else None,
            "holding_days": holding_days,
            "tier": (action or {}).get("tier"),
            "catalyst": (action or {}).get("catalyst"),
            "catalyst_type": (action or {}).get("catalyst_type"),
            "conviction": (action or {}).get("confidence"),
            "reasoning": (decision or {}).get("reasoning"),
            "regime": (decision or {}).get("regime"),
            "regime_score": (decision or {}).get("regime_score"),
            "decision_id": (decision or {}).get("decision_id"),
            "bug_flags": _apply_bug_flags(sym, entry_t, exit_t),
        })

    closed.sort(key=lambda t: t["exit_time"] or "", reverse=True)
    return closed


# ── Internal helpers ──────────────────────────────────────────────────────────

def _parse_dt(s) -> Optional[datetime]:
    if s is None:
        return None
    try:
        text = str(s).strip()
        if text in ("None", ""):
            return None
        text = text.replace(" ", "T")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _parse_orders(raw: list) -> tuple[list[dict], list[dict]]:
    """Normalise Alpaca SDK objects or dicts into buy/sell lists."""
    buys, sells = [], []
    for o in raw:
        if isinstance(o, dict):
            side = str(o.get("side", "")).lower()
            status = str(o.get("status", "")).lower()
            price_raw = o.get("filled_avg_price")
            qty_raw = o.get("filled_qty") or o.get("qty")
            filled_at_raw = o.get("filled_at")
            symbol = str(o.get("symbol", ""))
            order_id = str(o.get("id", ""))
        else:
            side = str(getattr(o, "side", "")).lower()
            status = str(getattr(o, "status", "")).lower()
            price_raw = getattr(o, "filled_avg_price", None)
            qty_raw = getattr(o, "filled_qty", None) or getattr(o, "qty", None)
            filled_at_raw = getattr(o, "filled_at", None)
            symbol = str(getattr(o, "symbol", ""))
            order_id = str(getattr(o, "id", ""))

        if "filled" not in status:
            continue

        try:
            price = float(price_raw) if price_raw and str(price_raw) != "None" else None
        except (TypeError, ValueError):
            price = None
        if not price:
            continue

        try:
            qty = float(qty_raw) if qty_raw and str(qty_raw) != "None" else 0.0
        except (TypeError, ValueError):
            qty = 0.0
        if qty <= 0:
            continue

        rec = {
            "id": order_id,
            "symbol": symbol,
            "price": price,
            "qty": qty,
            "filled_at": _parse_dt(filled_at_raw),
        }
        # Side may be "orderside.buy", "buy", "OrderSide.BUY", etc.
        side_clean = side.split(".")[-1]
        if "buy" in side_clean:
            buys.append(rec)
        elif "sell" in side_clean:
            sells.append(rec)

    return buys, sells


def _find_entry_decision(
    symbol: str,
    fill_time: datetime,
    decisions: list[dict],
) -> tuple[Optional[dict], Optional[dict]]:
    """Find the decision + action most likely to have triggered this BUY fill."""
    window_before = timedelta(minutes=30)
    window_after = timedelta(minutes=3)
    candidates: list[tuple[float, dict, dict]] = []
    for dec in decisions:
        dec_ts = _parse_dt(dec.get("ts"))
        if not dec_ts:
            continue
        if not (fill_time - window_before <= dec_ts <= fill_time + window_after):
            continue
        for action in dec.get("actions", []):
            if action.get("action") == "buy" and action.get("symbol") == symbol:
                delta = abs((fill_time - dec_ts).total_seconds())
                candidates.append((delta, dec, action))
    if not candidates:
        return None, None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1], candidates[0][2]


def _apply_bug_flags(
    symbol: str,
    entry_time: Optional[datetime],
    exit_time: Optional[datetime],
) -> list[str]:
    """Return IDs of bugs that may have affected this trade."""
    if not entry_time:
        return []
    flags = []
    trade_end = exit_time or datetime.now(timezone.utc)
    for bug in KNOWN_BUG_PERIODS:
        if not bug.get("trading_impact"):
            continue
        bug_start = datetime.fromisoformat(bug["start"]).replace(tzinfo=timezone.utc)
        bug_end = datetime.fromisoformat(bug["end"]).replace(tzinfo=timezone.utc)
        # Bug active during any part of trade's holding window
        if entry_time > bug_end or trade_end < bug_start:
            continue
        affects = bug.get("affects_symbols", [])
        if affects and symbol not in affects:
            continue
        flags.append(bug["id"])
    return flags


def _load_decisions() -> list[dict]:
    try:
        data = json.loads((_BOT_DIR / "memory/decisions.json").read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _fetch_alpaca_orders() -> list:
    try:
        from alpaca.trading.client import TradingClient  # noqa: I001
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        from dotenv import load_dotenv
        load_dotenv(_BOT_DIR / ".env")
        client = TradingClient(
            os.getenv("ALPACA_API_KEY", ""),
            os.getenv("ALPACA_SECRET_KEY", ""),
            paper=True,
        )
        return list(client.get_orders(filter=GetOrdersRequest(
            status=QueryOrderStatus.CLOSED, limit=200,
        )))
    except Exception:
        return []
