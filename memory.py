"""
memory.py — rolling memory of trading decisions with performance tracking.

Persists to memory/decisions.json. Injected into every prompt cycle.
Tracks performance by: trade type, sector, session, catalyst type, options strategy.
Weekly summary generated every Sunday at 6 AM ET.
"""

import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus, OrderSide
from dotenv import load_dotenv

import logging
import trade_memory
from semantic_labels import classify_catalyst

log = logging.getLogger(__name__)

load_dotenv()

MEMORY_DIR      = Path(__file__).parent / "memory"
DECISIONS_FILE  = MEMORY_DIR / "decisions.json"
PERF_FILE       = MEMORY_DIR / "performance.json"
REPORTS_DIR     = Path(__file__).parent / "data" / "reports"
_MAX_DECISIONS  = 500   # T-004: rolling window — stores full trade history
PROMPT_WINDOW   = 10

# T-008: symbol → sector mapping (used at record-write time for performance buckets)
_SECTOR_MAP: dict[str, str] = {
    # Technology
    "NVDA": "Technology", "TSM": "Technology", "MSFT": "Technology",
    "CRWV": "Technology", "PLTR": "Technology", "ASML": "Technology",
    # Energy
    "XLE": "Energy", "XOM": "Energy", "CVX": "Energy", "USO": "Energy",
    # Commodities
    "GLD": "Commodities", "SLV": "Commodities", "COPX": "Commodities",
    # Financials
    "JPM": "Financials", "GS": "Financials", "XLF": "Financials",
    # Consumer
    "AMZN": "Consumer", "WMT": "Consumer", "XRT": "Consumer",
    # Defense
    "LMT": "Defense", "RTX": "Defense", "ITA": "Defense",
    # Biotech
    "XBI": "Biotech",
    # Health
    "JNJ": "Health", "LLY": "Health",
    # International
    "EWJ": "International", "FXI": "International", "EEM": "International",
    "EWM": "International", "ECH": "International",
    # Macro
    "SPY": "Macro", "QQQ": "Macro", "IWM": "Macro", "TLT": "Macro", "VXX": "Macro",
    # Crypto (both canonical slash and Alpaca format)
    "BTC/USD": "Crypto", "ETH/USD": "Crypto", "BTCUSD": "Crypto", "ETHUSD": "Crypto",
    # Shipping
    "FRO": "Shipping", "STNG": "Shipping",
    # Housing
    "RKT": "Housing",
    # Utilities
    "BE": "Utilities",
}


def _get_active_strategy() -> str:
    """Read active_strategy from strategy_config.json. Returns 'unknown' on any error."""
    try:
        cfg_path = Path(__file__).parent / "strategy_config.json"
        if cfg_path.exists():
            return json.loads(cfg_path.read_text()).get("active_strategy", "unknown") or "unknown"
    except Exception:
        pass
    return "unknown"


# ── Persistence ───────────────────────────────────────────────────────────────

def _load_decisions() -> list:
    MEMORY_DIR.mkdir(exist_ok=True)
    if not DECISIONS_FILE.exists():
        return []
    try:
        return json.loads(DECISIONS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def _save_decisions(decisions: list) -> None:
    MEMORY_DIR.mkdir(exist_ok=True)
    DECISIONS_FILE.write_text(json.dumps(decisions, indent=2))


def _load_perf() -> dict:
    if PERF_FILE.exists():
        try:
            return json.loads(PERF_FILE.read_text())
        except Exception:
            pass
    return _empty_perf()


def _save_perf(perf: dict) -> None:
    MEMORY_DIR.mkdir(exist_ok=True)
    PERF_FILE.write_text(json.dumps(perf, indent=2))


def _empty_perf() -> dict:
    return {
        "by_type":     {},   # "stock_long", "stock_short", "options_spread", etc.
        "by_sector":   {},
        "by_session":  {},   # "market", "extended", "overnight"
        "by_catalyst": {},
        "by_strategy": {},   # options strategies
        "by_tier":     {},   # "core", "dynamic", "intraday"
        "totals":      {"trades": 0, "wins": 0, "losses": 0, "pending": 0},
    }


def _bucket_inc(perf: dict, bucket: str, key: str, outcome: str) -> None:
    perf[bucket].setdefault(key, {"trades": 0, "wins": 0, "losses": 0, "pending": 0})
    perf[bucket][key]["trades"] += 1
    if outcome == "win":
        perf[bucket][key]["wins"] += 1
    elif outcome == "loss":
        perf[bucket][key]["losses"] += 1
    else:
        perf[bucket][key]["pending"] += 1


# ── Write ─────────────────────────────────────────────────────────────────────

def save_decision(claude_decision: dict, session_tier: str,
                  vector_id: str = "",
                  decision_id: str = "") -> None:
    """
    Persist a Claude decision to JSON rolling memory.
    vector_id:   ChromaDB document ID from trade_memory.save_trade_memory() —
                 stored here so update_outcomes_from_alpaca() can update the
                 vector store when trades resolve.
    decision_id: Attribution ID (dec_A1_YYYYMMDD_HHMMSS) — links to
                 data/analytics/attribution_log.jsonl events.
    """
    decisions = _load_decisions()

    actions = claude_decision.get("actions", [])
    _strategy = _get_active_strategy()
    record = {
        "ts":          datetime.now(timezone.utc).isoformat(),
        "session":     session_tier,
        "regime":      claude_decision.get("regime_view") or claude_decision.get("regime", "unknown"),
        "regime_score": claude_decision.get("regime_score"),  # T-007: from Stage 1 via bot.py injection
        "reasoning":   claude_decision.get("reasoning", ""),
        "n_actions":   len(actions),
        "vector_id":   vector_id,       # links to ChromaDB record for outcome updates
        "decision_id": decision_id,     # links to attribution log
        "actions": [
            {
                "action":        a.get("action"),
                "symbol":        a.get("symbol"),
                "qty":           a.get("qty"),
                "stop_loss":     a.get("stop_loss"),
                "take_profit":   a.get("take_profit"),
                "tier":          a.get("tier", "core"),
                "catalyst":      a.get("catalyst"),
                "catalyst_type": classify_catalyst(a.get("catalyst") or "").value,  # T-022
                "sector_signal": a.get("sector_signal"),
                "confidence":    a.get("confidence"),
                "strategy":      _strategy,                                   # T-008
                "sector":        _SECTOR_MAP.get(a.get("symbol", ""), "unknown"),  # T-008
                # options fields
                "option_strategy": a.get("option_strategy"),
                "expiration":    a.get("expiration"),
                "long_strike":   a.get("long_strike"),
                "short_strike":  a.get("short_strike"),
                "max_cost_usd":  a.get("max_cost_usd"),
                "outcome":       None,
                "pnl":           None,
            }
            for a in actions
        ],
    }
    decisions.append(record)
    _save_decisions(decisions[-_MAX_DECISIONS:])


def update_outcomes_from_alpaca() -> None:
    """Scan Alpaca closed orders and update outcome/pnl for matching actions."""
    api_key    = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        return

    try:
        client = TradingClient(api_key, secret_key, paper=True)
        filled = client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.CLOSED, limit=50,
        ))
    except Exception:
        return

    # T-025: collect SELL fills (exit fills) and BUY fills (entry fills) separately.
    # Outcome is determined by comparing the exit price to stop_loss/take_profit thresholds.
    sell_fills: dict[str, list] = defaultdict(list)
    buy_fills:  dict[str, list] = defaultdict(list)
    for o in filled:
        if o.filled_avg_price is None or o.filled_qty is None:
            continue
        info = {
            "order_id":   str(o.id),
            "fill_price": float(o.filled_avg_price),
            "qty":        float(o.filled_qty),
        }
        if o.side == OrderSide.SELL:
            sell_fills[o.symbol].append(info)
        elif o.side == OrderSide.BUY:
            buy_fills[o.symbol].append(info)

    decisions = _load_decisions()
    perf      = _load_perf()
    changed   = False

    for decision in decisions:
        session = decision.get("session", "market")
        for action in decision.get("actions", []):
            if action.get("outcome") is not None:
                continue
            # Only resolve outcomes for actual trade actions — not holds/monitors
            if action.get("action") not in ("buy", "sell", "close", "buy_option",
                                             "sell_option_spread", "buy_straddle"):
                continue
            sym = action.get("symbol")
            if not sym or sym not in sell_fills:
                continue

            for fill in sell_fills[sym]:
                stop  = float(action.get("stop_loss", 0) or 0)
                tp    = float(action.get("take_profit", 0) or 0)
                exit_price = fill["fill_price"]

                if tp > 0 and exit_price >= tp * 0.99:
                    outcome = "win"
                elif stop > 0 and exit_price <= stop * 1.01:
                    outcome = "loss"
                else:
                    continue

                # Use BUY fill price as entry for PnL; fall back to stop_loss approximation
                entry_fills = buy_fills.get(sym)
                if entry_fills:
                    entry_price = entry_fills[0]["fill_price"]
                elif stop > 0:
                    entry_price = stop
                else:
                    entry_price = exit_price
                pnl = round((exit_price - entry_price) * fill["qty"], 2)

                action["outcome"] = outcome
                action["pnl"]     = pnl
                changed = True

                # Mirror outcome to ChromaDB vector store
                vid = decision.get("vector_id", "")
                if vid:
                    trade_memory.update_trade_outcome(vid, outcome, pnl)

                # Update performance buckets
                tier     = action.get("tier", "core")
                act      = action.get("action", "buy")
                opt_strat= action.get("option_strategy")
                catalyst = action.get("catalyst", "")[:30]

                trade_type = (f"options_{opt_strat}" if opt_strat
                              else f"stock_{act}")
                _bucket_inc(perf, "by_type",     trade_type, outcome)
                _bucket_inc(perf, "by_session",  session,    outcome)
                _bucket_inc(perf, "by_tier",     tier,       outcome)
                sector = action.get("sector") or _SECTOR_MAP.get(sym, "unknown")
                _bucket_inc(perf, "by_sector", sector, outcome)
                if opt_strat:
                    _bucket_inc(perf, "by_strategy", opt_strat, outcome)
                else:
                    strategy = action.get("strategy") or "unknown"
                    _bucket_inc(perf, "by_strategy", strategy, outcome)
                if catalyst:
                    cat_key = catalyst.split()[0].lower() if catalyst else "unknown"
                    _bucket_inc(perf, "by_catalyst", cat_key, outcome)

                t = perf["totals"]
                t["trades"] += 1
                if outcome == "win":
                    t["wins"] += 1
                else:
                    t["losses"] += 1

    if changed:
        _save_decisions(decisions)
        _save_perf(perf)


# ── Read ──────────────────────────────────────────────────────────────────────

def get_recent_decisions_str(n: int = PROMPT_WINDOW) -> str:
    decisions = _load_decisions()
    recent    = decisions[-n:]

    if not recent:
        return "  (no prior decisions this session)"

    lines = []
    for d in reversed(recent):
        ts       = d.get("ts", "")[:16].replace("T", " ")
        regime   = d.get("regime", "?")
        n_act    = d.get("n_actions", 0)
        reasoning= d.get("reasoning", "")[:120]
        lines.append(f"  [{ts}] regime={regime}  actions={n_act}  \"{reasoning}\"")

        for a in d.get("actions", []):
            sym      = a.get("symbol", "?")
            act      = a.get("action", "?")
            tier     = a.get("tier", "?")
            outcome  = a.get("outcome") or "pending"
            pnl_str  = f"  pnl=${a['pnl']:+.0f}" if a.get("pnl") is not None else ""
            opt_tag  = f"  [{a['option_strategy']}]" if a.get("option_strategy") else ""
            lines.append(f"    → {act.upper()} {sym} [{tier}]{opt_tag}  outcome={outcome}{pnl_str}")

    return "\n".join(lines)


def get_ticker_lessons() -> str:
    decisions = _load_decisions()
    if len(decisions) < 2:
        return ""

    ticker_outcomes: dict[str, list] = defaultdict(list)
    for d in decisions[-10:]:
        for a in d.get("actions", []):
            sym     = a.get("symbol")
            outcome = a.get("outcome")
            if sym:
                ticker_outcomes[sym].append(outcome)

    lessons = []
    for sym, outcomes in ticker_outcomes.items():
        if len(outcomes) >= 2:
            last_two = outcomes[-2:]
            if all(o == "loss" for o in last_two):
                lessons.append(
                    f"  ⚠ {sym} lost twice in a row — avoid unless strong NEW catalyst"
                )
    return "\n".join(lessons) if lessons else ""


def get_ticker_stats() -> dict:
    decisions = _load_decisions()
    stats: dict[str, dict] = defaultdict(
        lambda: {"trades": 0, "wins": 0, "losses": 0, "pending": 0}
    )
    for d in decisions:
        for a in d.get("actions", []):
            sym = a.get("symbol")
            if not sym:
                continue
            stats[sym]["trades"] += 1
            outcome = a.get("outcome")
            if outcome == "win":
                stats[sym]["wins"] += 1
            elif outcome == "loss":
                stats[sym]["losses"] += 1
            else:
                stats[sym]["pending"] += 1
    return dict(stats)


def get_performance_summary() -> dict:
    return _load_perf()


def get_newly_resolved_trades() -> list[dict]:
    """
    Return trades that changed from pending → win/loss since last call.

    Marks returned trades with "published": true so they are not
    returned again on the next call. Used by bot.py to trigger
    trade_exit posts without double-publishing.
    """
    decisions = _load_decisions()
    resolved  = []
    changed   = False

    for decision in decisions:
        for action in decision.get("actions", []):
            outcome = action.get("outcome")
            if outcome not in ("win", "loss"):
                continue
            if action.get("published"):
                continue
            resolved.append({
                "symbol":      action.get("symbol", ""),
                "action":      action.get("action", "buy"),
                "qty":         action.get("qty"),
                "stop_loss":   action.get("stop_loss"),
                "take_profit": action.get("take_profit"),
                "outcome":     outcome,
                "pnl":         action.get("pnl"),
                "catalyst":    action.get("catalyst", ""),
                "tier":        action.get("tier", "core"),
                "session":     decision.get("session", "market"),
                "ts":          decision.get("ts", ""),
            })
            action["published"] = True
            changed = True

    if changed:
        _save_decisions(decisions)

    return resolved


# ── Weekly summary ────────────────────────────────────────────────────────────

def generate_weekly_summary() -> dict:
    """Generate weekly performance report. Saved to data/reports/weekly_summary.json."""
    perf      = _load_perf()
    decisions = _load_decisions()
    totals    = perf.get("totals", {})
    trades    = totals.get("trades", 0)
    wins      = totals.get("wins", 0)
    win_rate  = wins / trades * 100 if trades > 0 else 0

    def _best_worst(bucket: dict) -> tuple[str, str]:
        if not bucket:
            return "n/a", "n/a"
        by_wr = {}
        for k, v in bucket.items():
            t = v.get("trades", 0)
            w = v.get("wins", 0)
            by_wr[k] = w / t if t >= 2 else None
        valid = {k: v for k, v in by_wr.items() if v is not None}
        if not valid:
            return "n/a", "n/a"
        best  = max(valid, key=lambda x: valid[x])
        worst = min(valid, key=lambda x: valid[x])
        return best, worst

    best_strat,  worst_strat  = _best_worst(perf.get("by_strategy", {}))
    best_sector, worst_sector = _best_worst(perf.get("by_sector", {}))
    best_type,   worst_type   = _best_worst(perf.get("by_type", {}))

    summary = {
        "generated_at":   datetime.now().isoformat(),
        "total_trades":   trades,
        "win_rate":       round(win_rate, 1),
        "best_strategy":  best_strat,
        "worst_strategy": worst_strat,
        "best_sector":    best_sector,
        "worst_sector":   worst_sector,
        "best_type":      best_type,
        "worst_type":     worst_type,
        "by_session":     perf.get("by_session", {}),
        "by_tier":        perf.get("by_tier", {}),
        "recent_decisions": len(decisions),
    }

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (REPORTS_DIR / "weekly_summary.json").write_text(
        json.dumps(summary, indent=2)
    )
    return summary


# ── Pattern Learning Watchlist ────────────────────────────────────────────────

PATTERN_WL_FILE = Path(__file__).parent / "data" / "memory" / "pattern_learning_watchlist.json"


def _load_pattern_watchlist() -> dict:
    PATTERN_WL_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not PATTERN_WL_FILE.exists():
        return {}
    try:
        return json.loads(PATTERN_WL_FILE.read_text())
    except Exception:
        return {}


def _save_pattern_watchlist(wl: dict) -> None:
    PATTERN_WL_FILE.parent.mkdir(parents=True, exist_ok=True)
    PATTERN_WL_FILE.write_text(json.dumps(wl, indent=2))


def get_pattern_watchlist_summary() -> str:
    """
    Returns formatted prompt section for Pattern Learning Watchlist.
    Replaces get_ticker_lessons().
    """
    wl = _load_pattern_watchlist()
    if not wl:
        return "  No symbols currently in pattern learning."

    lines = []
    for sym, data in wl.items():
        if data.get("graduated"):
            continue
        losses      = data.get("loss_history", [])
        total_pnl   = sum(l.get("pnl", 0) or 0 for l in losses)
        n_losses    = len(losses)
        pattern     = data.get("emerging_pattern") or "still learning"
        conditions  = data.get("re_entry_conditions", [])
        min_signals = data.get("minimum_signals_required", 2)
        observations= data.get("observations", [])
        last_obs    = observations[-1]["lesson"][:100] if observations else "no observations yet"

        pnl_str = f"${total_pnl:+.0f}" if total_pnl else "$0"
        lines.append(f"  {sym} [{n_losses} {'loss' if n_losses == 1 else 'losses'}, {pnl_str} total]")
        lines.append(f"    Pattern so far: {pattern}")
        if conditions:
            for cond in conditions[:3]:
                lines.append(f"    Needs to see: {cond}")
        lines.append(f"    Min signals required: {min_signals}")
        lines.append(f"    Recent observation: {last_obs}")

    return "\n".join(lines) if lines else "  No symbols currently in pattern learning."


def add_watchlist_observation(
    symbol: str,
    price_action: str,
    conditions: list,
    lesson: str,
    source: str = "auto_observation",
) -> None:
    """Append an observation for a symbol in the pattern learning watchlist."""
    wl = _load_pattern_watchlist()

    if symbol not in wl:
        # Auto-create entry for symbol being added
        wl[symbol] = {
            "status":              "learning",
            "added_date":          datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "added_reason":        "auto-created from observation",
            "loss_history":        [],
            "observations":        [],
            "emerging_pattern":    "",
            "re_entry_conditions": [],
            "minimum_signals_required": 2,
            "confidence_required": "high",
            "graduate_condition":  "Pattern confirmed across 3+ observations",
            "graduated":           False,
            "graduated_date":      None,
            "weekly_review_notes": "",
        }

    obs = {
        "date":               datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "price_action":       price_action,
        "conditions_present": conditions,
        "lesson":             lesson,
        "source":             source,
    }
    wl[symbol].setdefault("observations", [])
    wl[symbol]["observations"].append(obs)

    # Cap at 50 observations per symbol
    if len(wl[symbol]["observations"]) > 50:
        wl[symbol]["observations"] = wl[symbol]["observations"][-50:]

    _save_pattern_watchlist(wl)


def add_symbol_to_pattern_watchlist(
    symbol: str,
    reason: str,
    pnl: float = 0.0,
    entry_conditions: str = "",
    exit_reason: str = "",
    market_context: str = "",
) -> None:
    """
    Add a symbol to the pattern learning watchlist after consecutive losses.
    Called from bot.py when a symbol loses twice in a row.
    """
    wl = _load_pattern_watchlist()

    if symbol not in wl:
        wl[symbol] = {
            "status":              "learning",
            "added_date":          datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "added_reason":        reason,
            "loss_history":        [],
            "observations":        [],
            "emerging_pattern":    "",
            "re_entry_conditions": [],
            "minimum_signals_required": 2,
            "confidence_required": "high",
            "graduate_condition":  "Pattern confirmed across 3+ observations AND re_entry_conditions defined",
            "graduated":           False,
            "graduated_date":      None,
            "weekly_review_notes": "",
        }

    if pnl != 0.0 or entry_conditions or exit_reason:
        wl[symbol].setdefault("loss_history", [])
        wl[symbol]["loss_history"].append({
            "date":               datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "entry_conditions":   entry_conditions,
            "exit_reason":        exit_reason,
            "pnl":                pnl,
            "market_context":     market_context,
            "initial_hypothesis": "",
        })

    _save_pattern_watchlist(wl)
    log.info("[PATTERN_WL] %s added to pattern learning watchlist: %s", symbol, reason)


def maybe_graduate_symbol(symbol: str) -> bool:
    """
    Check if symbol meets graduation criteria.
    Returns True if graduated.
    """
    wl = _load_pattern_watchlist()
    if symbol not in wl:
        return False

    entry = wl[symbol]
    if entry.get("graduated"):
        return False

    obs       = entry.get("observations", [])
    pattern   = entry.get("emerging_pattern", "")
    conditions= entry.get("re_entry_conditions", [])
    notes     = entry.get("weekly_review_notes", "")

    if len(obs) >= 3 and pattern and len(conditions) >= 2 and notes:
        entry["graduated"]      = True
        entry["graduated_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        wl[symbol] = entry
        _save_pattern_watchlist(wl)

        from log_setup import log_trade  # noqa: PLC0415
        log_trade({
            "event":           "watchlist_graduation",
            "symbol":          symbol,
            "emerging_pattern": pattern,
            "re_entry_conditions": conditions,
            "observations":    len(obs),
        })
        log.info("[PATTERN_WL] %s graduated from pattern learning watchlist", symbol)
        return True

    return False


def update_pattern_watchlist_from_review(updates: dict) -> None:
    """
    Called by weekly_review.py Strategy Director to update watchlist entries.
    updates = {"SYMBOL": {"emerging_pattern": "...", "re_entry_conditions": [...],
                           "graduate": bool, "notes": "..."}, ...}
    """
    wl = _load_pattern_watchlist()

    for sym, upd in updates.items():
        if sym not in wl:
            continue
        if upd.get("emerging_pattern"):
            wl[sym]["emerging_pattern"] = upd["emerging_pattern"]
        if upd.get("re_entry_conditions"):
            wl[sym]["re_entry_conditions"] = upd["re_entry_conditions"]
        if upd.get("notes"):
            wl[sym]["weekly_review_notes"] = upd["notes"]
        if upd.get("graduate"):
            maybe_graduate_symbol(sym)

    _save_pattern_watchlist(wl)
