"""
decision_theater.py — Data module for the Decision Theater dashboard tab.

Assembles structured data for:
  - Cycle view: per-decision-cycle pipeline stage breakdown
  - Trade lifecycle: full entry→hold→exit story for one trade
  - All trades summary: selector pills data

All functions are non-fatal. Missing data returns sparse/default fields;
callers should handle None gracefully. No side effects — read-only.

Public API:
    get_cycle_view(cycle_index=-1) -> dict
    get_trade_lifecycle(symbol, entry_date=None) -> dict
    get_all_trades_summary() -> dict
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_BOT_DIR = Path(__file__).resolve().parent
_DECISIONS_PATH = _BOT_DIR / "memory" / "decisions.json"
_SCRATCHPADS_PATH = _BOT_DIR / "data" / "memory" / "hot_scratchpads.json"
_GATE_STATE_PATH = _BOT_DIR / "data" / "market" / "gate_state.json"
_SIGNAL_SCORES_PATH = _BOT_DIR / "data" / "market" / "signal_scores.json"
_TRADES_JSONL = _BOT_DIR / "logs" / "trades.jsonl"
_BOT_LOG = _BOT_DIR / "logs" / "bot.log"
_BOT_LOG_1 = _BOT_DIR / "logs" / "bot.log.1"
_A2_DECISIONS_PATH = _BOT_DIR / "data" / "account2" / "trade_memory" / "decisions_account2.json"
_A2_STRUCTURES_PATH = _BOT_DIR / "data" / "account2" / "positions" / "structures.json"


# ── Loaders ───────────────────────────────────────────────────────────────────

def _load_json(path: Path, default=None):
    try:
        data = json.loads(path.read_text())
        return data
    except Exception:
        return default


def _load_decisions() -> list[dict]:
    data = _load_json(_DECISIONS_PATH, [])
    if isinstance(data, list):
        return data
    return data.get("decisions", [])


def _load_scratchpads() -> list[dict]:
    data = _load_json(_SCRATCHPADS_PATH, [])
    if isinstance(data, list):
        return data
    return data.get("scratchpads", [])


def _load_trades_jsonl() -> list[dict]:
    try:
        lines = []
        for line in _TRADES_JSONL.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    lines.append(json.loads(line))
                except Exception:
                    pass
        return lines
    except Exception:
        return []


def _load_a2_structures() -> list[dict]:
    data = _load_json(_A2_STRUCTURES_PATH, [])
    return data if isinstance(data, list) else []


def _load_alpaca_positions() -> list[dict]:
    """Load current Alpaca positions without crashing if not configured."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
        from alpaca.trading.client import TradingClient
        api_key = os.environ.get("ALPACA_API_KEY", "")
        api_secret = os.environ.get("ALPACA_SECRET_KEY", "")
        if not api_key or not api_secret:
            return []
        client = TradingClient(api_key, api_secret, paper=True)
        return list(client.get_all_positions())
    except Exception:
        return []


def _load_alpaca_orders(limit: int = 200) -> list:
    """Load closed Alpaca orders without crashing."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
        from alpaca.trading.client import TradingClient  # noqa: I001
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        api_key = os.environ.get("ALPACA_API_KEY", "")
        api_secret = os.environ.get("ALPACA_SECRET_KEY", "")
        if not api_key or not api_secret:
            return []
        client = TradingClient(api_key, api_secret, paper=True)
        return list(client.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=limit)
        ))
    except Exception:
        return []


def _build_closed_trades_safe() -> list[dict]:
    try:
        from trade_journal import build_closed_trades
        orders = _load_alpaca_orders()
        return build_closed_trades(orders)
    except Exception:
        return []


def _load_bug_fix_log() -> list[dict]:
    try:
        from trade_journal import build_bug_fix_log
        return build_bug_fix_log()
    except Exception:
        return []


# ── Log parsing ───────────────────────────────────────────────────────────────

def _parse_log_lines_for_cycle(cycle_ts: str) -> dict:
    """
    Parse bot.log around a given cycle timestamp to extract stage metrics.
    Returns a dict with keys: signals_batches, symbols_scored, gate_skip, gate_reason,
    sonnet_cost, sonnet_in, sonnet_out, sonnet_cr, regime_cost, scratchpad_cost.
    """
    result: dict = {}
    try:
        ts_dt = datetime.fromisoformat(cycle_ts.replace("Z", "+00:00"))
        # Search in current log then rotated log
        for log_path in [_BOT_LOG, _BOT_LOG_1]:
            if not log_path.exists():
                continue
            try:
                text = log_path.read_text(errors="replace")
            except Exception:
                continue
            _parse_log_block(text, ts_dt, result)
            if result:
                break
    except Exception:
        pass
    return result


def _parse_log_block(text: str, ts_dt: datetime, result: dict) -> None:
    """Extract stage metrics from log text near the given timestamp."""
    # Build a 5-minute window
    lines = text.splitlines()
    window_lines: list[str] = []
    in_window = False
    for line in lines:
        m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
        if m:
            try:
                line_dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
                delta = abs((line_dt - ts_dt).total_seconds())
                in_window = delta < 360  # ±6 minutes
            except Exception:
                in_window = False
        if in_window:
            window_lines.append(line)

    batches = 0
    scored = 0
    for line in window_lines:
        # [SIGNALS] batch scored N symbols
        m = re.search(r"\[SIGNALS\]\s+batch scored (\d+) symbols", line)
        if m:
            batches += 1
            scored += int(m.group(1))

        # [GATE] SKIP — gate skipped
        if "[GATE] SKIP" in line and "gate_skip" not in result:
            result["gate_skip"] = True
            gm = re.search(r"SKIP\s+(.*)", line)
            result["gate_reason"] = gm.group(1)[:80] if gm else "cooldown"

        # Cost [ask_claude]: in=X cw=Y cr=Z out=W
        m = re.search(r"Cost \[ask_claude\]:\s+in=(\d+)\s+cw=(\d+)\s+cr=(\d+)\s+out=(\d+)\s+→\s+\$([0-9.]+)", line)
        if m and "sonnet_cost" not in result:
            result["sonnet_in"] = int(m.group(1))
            result["sonnet_cw"] = int(m.group(2))
            result["sonnet_cr"] = int(m.group(3))
            result["sonnet_out"] = int(m.group(4))
            result["sonnet_cost"] = float(m.group(5))

        # Cost [regime_classifier]
        m = re.search(r"Cost \[regime_classifier\]:\s+in=(\d+).*out=(\d+)\s+→\s+\$([0-9.]+)", line)
        if m and "regime_cost" not in result:
            result["regime_cost"] = float(m.group(3))

        # Cost [scratchpad]
        m = re.search(r"Cost \[scratchpad\]:\s+in=(\d+).*out=(\d+)\s+→\s+\$([0-9.]+)", line)
        if m and "scratchpad_cost" not in result:
            result["scratchpad_cost"] = float(m.group(3))

    if batches:
        result["signals_batches"] = batches
        result["symbols_scored"] = scored


# ── Stage builders ────────────────────────────────────────────────────────────

def _build_regime_stage(cycle: dict, log_data: dict) -> dict:
    regime = cycle.get("regime") or cycle.get("regime_view", "unknown")
    score = cycle.get("regime_score")
    return {
        "status": "ok" if regime and regime != "unknown" else "warn",
        "regime": regime,
        "score": score,
        "cost_usd": log_data.get("regime_cost"),
    }


def _build_signals_stage(cycle: dict, log_data: dict) -> dict:
    symbols_scored = log_data.get("symbols_scored", 0)
    # Extract top symbols from actions in this cycle
    actions = cycle.get("actions") or cycle.get("ideas", [])
    top_3 = []
    for a in actions[:3]:
        sym = a.get("symbol", "")
        if sym:
            top_3.append({"symbol": sym, "tier": a.get("tier", ""), "catalyst": a.get("catalyst", "")[:60]})
    top_syms = ", ".join(s["symbol"] for s in top_3) if top_3 else ""
    summary = f"{symbols_scored} scored" + (f" — top: {top_syms}" if top_syms else "")
    return {
        "status": "ok" if symbols_scored > 0 else "warn",
        "symbols_scored": symbols_scored,
        "batches": log_data.get("signals_batches", 0),
        "top_3": top_3,
        "n_actions": cycle.get("n_actions", 0),
        "summary": summary,
    }


def _build_scratchpad_stage(cycle: dict, log_data: dict) -> dict:
    # Try to find the nearest scratchpad to this cycle ts
    scratchpads = _load_scratchpads()
    sp = scratchpads[-1] if scratchpads else {}
    watching = sp.get("watching", [])
    blocking = sp.get("blocking", [])
    conviction = sp.get("conviction_ranking", [])
    summary = f"watching {len(watching[:6])}"
    if blocking:
        summary += f" · {len(blocking[:4])} blocking"
    return {
        "status": "ok" if watching else "warn",
        "watching": watching[:6],
        "blocking": blocking[:4],
        "conviction_ranking": conviction[:5],
        "cost_usd": log_data.get("scratchpad_cost"),
        "summary": summary,
    }


def _build_gate_stage(cycle: dict, log_data: dict) -> dict:
    gate_state = _load_json(_GATE_STATE_PATH, {})
    skipped = log_data.get("gate_skip", False)
    # Detect skip from reasoning text
    reasoning = cycle.get("reasoning", "")
    if "gate skipped" in reasoning.lower():
        skipped = True
    mode = "SKIP" if skipped else "FULL"
    return {
        "status": "skip" if skipped else "ok",
        "triggered": not skipped,
        "mode": mode,
        "skip_reason": log_data.get("gate_reason", "") if skipped else None,
        "consecutive_skips": gate_state.get("consecutive_skips"),
        "sonnet_calls_today": gate_state.get("total_calls_today"),
        "skips_today": gate_state.get("total_skips_today"),
    }


def _build_sonnet_stage(cycle: dict, log_data: dict) -> dict:
    reasoning = cycle.get("reasoning", "")
    skipped = "gate skipped" in reasoning.lower()
    ideas = cycle.get("actions") or cycle.get("ideas", [])
    ideas_clean = []
    for a in ideas:
        ideas_clean.append({
            "symbol": a.get("symbol", ""),
            "intent": a.get("action") or a.get("intent", "hold"),
            "tier": a.get("tier", ""),
            "catalyst": (a.get("catalyst") or "")[:80],
            "catalyst_type": a.get("catalyst_type", ""),
        })
    return {
        "status": "skip" if skipped else "ok",
        "ideas_generated": len(ideas),
        "ideas": ideas_clean,
        "prompt_tokens": log_data.get("sonnet_in"),
        "cache_read_tokens": log_data.get("sonnet_cr"),
        "cache_write_tokens": log_data.get("sonnet_cw"),
        "output_tokens": log_data.get("sonnet_out"),
        "cost_usd": log_data.get("sonnet_cost"),
        "reasoning_excerpt": reasoning[:300] + ("…" if len(reasoning) > 300 else ""),
        "reasoning_full": reasoning,
        "regime_view": cycle.get("regime") or cycle.get("regime_view", ""),
    }


def _build_kernel_stage(cycle: dict, trades_index: dict) -> dict:
    decision_id = cycle.get("decision_id", "")
    submitted = trades_index.get("submitted", {}).get(decision_id, [])
    rejected = trades_index.get("rejected", {}).get(decision_id, [])
    ideas = cycle.get("actions") or cycle.get("ideas", [])
    return {
        "status": "ok" if ideas else "warn",
        "ideas_in": len(ideas),
        "approved": len(submitted),
        "rejected": len(rejected),
        "submitted": [
            {"symbol": t["symbol"], "action": t.get("action", ""), "qty": t.get("qty"),
             "fill_price": t.get("fill_price")}
            for t in submitted[:6]
        ],
        "rejections": [
            {"symbol": t["symbol"], "reason": t.get("reason", "")[:60]}
            for t in rejected[:6]
        ],
    }


def _build_execution_stage(cycle: dict, trades_index: dict) -> dict:
    decision_id = cycle.get("decision_id", "")
    submitted = trades_index.get("submitted", {}).get(decision_id, [])
    filled = [t for t in submitted if t.get("fill_price")]
    return {
        "status": "ok" if submitted else "warn",
        "orders_submitted": len(submitted),
        "orders_filled": len(filled),
        "actions": [
            {"symbol": t["symbol"], "action": t.get("action", ""),
             "qty": t.get("qty"), "price": t.get("fill_price")}
            for t in submitted[:6]
        ],
    }


def _build_a2_stage(cycle: dict) -> dict:
    # Look for an A2 decision near this cycle's timestamp
    try:
        a2_data = _load_json(_A2_DECISIONS_PATH, [])
        a2_list = a2_data if isinstance(a2_data, list) else a2_data.get("decisions", [])
        if not a2_list:
            return {"status": "skip", "reason": "no_a2_data"}
        cycle_ts = cycle.get("ts", "")
        # Find the closest A2 decision
        best = None
        best_delta = float("inf")
        for d in a2_list:
            d_ts = d.get("timestamp", "")
            try:
                dt1 = datetime.fromisoformat(cycle_ts.replace("Z", "+00:00"))
                dt2 = datetime.fromisoformat(d_ts.replace("Z", "+00:00"))
                delta = abs((dt1 - dt2).total_seconds())
                if delta < best_delta:
                    best_delta = delta
                    best = d
            except Exception:
                pass
        if not best or best_delta > 600:
            return {"status": "skip", "reason": "no_nearby_a2_cycle"}
        reasoning = best.get("reasoning", "")
        return {
            "status": "ok" if best.get("actions") else "skip",
            "reasoning": reasoning[:150],
            "actions": best.get("actions", []),
            "regime": best.get("regime", ""),
        }
    except Exception:
        return {"status": "error", "reason": "a2_load_failed"}


def _build_trades_index(trade_lines: list[dict]) -> dict:
    """Index trade log entries by decision_id."""
    submitted: dict[str, list] = {}
    rejected: dict[str, list] = {}
    for t in trade_lines:
        did = t.get("decision_id", "")
        status = t.get("status", "")
        if status == "submitted":
            submitted.setdefault(did, []).append(t)
        elif status == "rejected":
            rejected.setdefault(did, []).append(t)
    return {"submitted": submitted, "rejected": rejected}


# ── Public: calibration data ──────────────────────────────────────────────────

_CONVICTION_MAP = {
    "high": 0.85, "HIGH": 0.85, "HIG↑": 0.90,
    "medium": 0.65, "MED": 0.65,
    "low": 0.45, "LOW": 0.45,
    "core": 0.80, "satellite": 0.60, "exploratory": 0.45,
}


def get_calibration_data() -> dict:
    """
    Returns conviction × realized-return data for closed trades.
    Points: [{symbol, conviction_str, conviction_x, pnl_pct, outcome}]
    Also computes a Brier score (lower = better calibrated).
    Returns empty points list gracefully when no closed trade data exists.
    """
    points = []

    # Primary source: build_closed_trades (requires Alpaca, may return empty)
    try:
        closed = _build_closed_trades_safe()
        for t in closed:
            pnl_pct = float(t.get("pnl_pct") or 0)
            conv_str = (t.get("conviction") or "").strip()
            conv_x = _CONVICTION_MAP.get(conv_str)
            if conv_x is None:
                continue
            points.append({
                "symbol": t.get("symbol", ""),
                "conviction_str": conv_str,
                "conviction_x": conv_x,
                "pnl_pct": round(pnl_pct, 2),
                "outcome": "win" if pnl_pct > 0 else "loss",
            })
    except Exception:
        pass

    # Fallback: scan decisions.json for actions where pnl field is populated
    if not points:
        try:
            for d in _load_decisions():
                for a in (d.get("actions") or []):
                    pnl = a.get("pnl")
                    conv_str = (a.get("confidence") or a.get("tier") or "").strip()
                    if pnl is None or not conv_str:
                        continue
                    conv_x = _CONVICTION_MAP.get(conv_str)
                    if conv_x is None:
                        continue
                    pnl_pct = float(pnl)
                    points.append({
                        "symbol": a.get("symbol", ""),
                        "conviction_str": conv_str,
                        "conviction_x": conv_x,
                        "pnl_pct": round(pnl_pct, 2),
                        "outcome": "win" if pnl_pct > 0 else "loss",
                    })
        except Exception:
            pass

    # Brier score: (conviction_x - binary_outcome)²
    brier = None
    if len(points) >= 3:
        sq_errors = [(p["conviction_x"] - (1.0 if p["outcome"] == "win" else 0.0)) ** 2
                     for p in points]
        brier = round(sum(sq_errors) / len(sq_errors), 3)

    return {"points": points, "n": len(points), "brier_score": brier}


# ── Public: all-cycles metadata (lightweight) ─────────────────────────────────

def get_all_cycles_metadata() -> list[dict]:
    """
    Returns [{ts, outcome}] for every cycle, oldest-first.
    Reads decisions.json only — no log parsing, no Alpaca calls.
    """
    decisions = _load_decisions()
    result = []
    for d in decisions:
        ts = d.get("ts", "")
        reasoning = d.get("reasoning", "").lower()
        actions = d.get("actions") or []
        if "gate skipped" in reasoning or not reasoning:
            outcome = "skipped"
        elif len(actions) > 0:
            outcome = "filled"
        else:
            outcome = "hold"
        result.append({"ts": ts, "outcome": outcome})
    return result


# ── Public: last-filled index ─────────────────────────────────────────────────

def find_last_filled_cycle_index() -> int:
    """
    Return the index of the most recent cycle where at least one trade
    action was proposed (actions list non-empty). Falls back to the
    most recent cycle if none found.
    """
    decisions = _load_decisions()
    if not decisions:
        return 0
    for i in range(len(decisions) - 1, -1, -1):
        actions = decisions[i].get("actions") or []
        if len(actions) > 0:
            return i
    return len(decisions) - 1


# ── Public: cycle view ────────────────────────────────────────────────────────

def get_cycle_view(cycle_index: int = -1) -> dict:
    """
    Returns full data for one decision cycle.
    cycle_index: -1 = most recent, 0 = first ever, N = Nth cycle.
    """
    decisions = _load_decisions()
    if not decisions:
        return _empty_cycle_view()

    n = len(decisions)
    actual_index = cycle_index if cycle_index >= 0 else n + cycle_index
    actual_index = max(0, min(actual_index, n - 1))
    cycle = decisions[actual_index]

    trade_lines = _load_trades_jsonl()
    trades_index = _build_trades_index(trade_lines)
    log_data = _parse_log_lines_for_cycle(cycle.get("ts", ""))

    stages = {
        "regime": _build_regime_stage(cycle, log_data),
        "signals": _build_signals_stage(cycle, log_data),
        "scratchpad": _build_scratchpad_stage(cycle, log_data),
        "gate": _build_gate_stage(cycle, log_data),
        "sonnet": _build_sonnet_stage(cycle, log_data),
        "kernel": _build_kernel_stage(cycle, trades_index),
        "execution": _build_execution_stage(cycle, trades_index),
        "a2": _build_a2_stage(cycle),
    }

    gate_st = stages["gate"]
    cycle_actions = cycle.get("actions") or []
    if gate_st.get("status") == "skip":
        outcome = "skipped"
    elif len(cycle_actions) > 0:
        outcome = "filled"
    else:
        outcome = "hold"

    return {
        "cycle_number": actual_index,
        "total_cycles": n,
        "timestamp": cycle.get("ts", ""),
        "session": cycle.get("session", "unknown"),
        "decision_id": cycle.get("decision_id", ""),
        "outcome": outcome,
        "stages": stages,
    }


def _empty_cycle_view() -> dict:
    empty_stage = {"status": "warn"}
    return {
        "cycle_number": 0, "total_cycles": 0,
        "timestamp": "", "session": "unknown", "decision_id": "",
        "stages": {k: dict(empty_stage) for k in
                   ["regime", "signals", "scratchpad", "gate", "sonnet",
                    "kernel", "execution", "a2"]},
    }


# ── Public: trade lifecycle ───────────────────────────────────────────────────

def get_trade_lifecycle(symbol: str, entry_date: Optional[str] = None) -> dict:
    """
    Returns full lifecycle data for one trade (open or closed).
    If multiple trades exist for symbol, entry_date disambiguates.
    """
    # 1. Check open positions first
    open_positions = _load_alpaca_positions()
    open_pos = next((p for p in open_positions if getattr(p, "symbol", "") == symbol), None)

    # 2. Get closed trades
    closed_trades = _build_closed_trades_safe()
    closed_for_sym = [t for t in closed_trades if t.get("symbol") == symbol]
    if entry_date:
        closed_for_sym = [t for t in closed_for_sym
                          if (t.get("entry_time") or "").startswith(entry_date[:10])]

    # 3. Load bug log
    bug_log = _load_bug_fix_log()
    bug_flag = next((b for b in bug_log if b.get("symbol") == symbol), None)

    # 4. Decisions for this symbol
    decisions = _load_decisions()
    sym_decisions = _decisions_for_symbol(decisions, symbol)

    if open_pos:
        return _build_open_lifecycle(open_pos, sym_decisions, bug_flag)
    elif closed_for_sym:
        trade = closed_for_sym[-1]  # most recent if multiple
        if entry_date:
            # pick closest match
            for t in closed_for_sym:
                if (t.get("entry_time") or "").startswith(entry_date[:10]):
                    trade = t
                    break
        return _build_closed_lifecycle(trade, sym_decisions, bug_flag)
    else:
        return _empty_lifecycle(symbol)


def _decisions_for_symbol(decisions: list[dict], symbol: str) -> list[dict]:
    """Find all decision cycles that mention a symbol in their actions/ideas."""
    result = []
    for dec in decisions:
        actions = dec.get("actions") or dec.get("ideas", [])
        for a in actions:
            if a.get("symbol") == symbol:
                result.append({"cycle": dec, "action": a})
                break
    return result


def _build_open_lifecycle(pos, sym_decisions: list[dict], bug_flag) -> dict:
    symbol = getattr(pos, "symbol", "")
    entry_price = float(getattr(pos, "avg_entry_price", 0) or 0)
    current_price = float(getattr(pos, "current_price", 0) or 0)
    qty = float(getattr(pos, "qty", 0) or 0)
    pnl_usd = float(getattr(pos, "unrealized_pl", 0) or 0)
    pnl_pct = float(getattr(pos, "unrealized_plpc", 0) or 0) * 100

    # Get entry decision
    entry_dec = sym_decisions[0] if sym_decisions else {}
    entry_action = entry_dec.get("action", {}) if entry_dec else {}
    entry_cycle = entry_dec.get("cycle", {}) if entry_dec else {}

    stop_price = float(entry_action.get("stop_loss") or 0) or None
    target_price = float(entry_action.get("take_profit") or 0) or None
    entry_ts = getattr(pos, "created_at", None)
    entry_ts_str = str(entry_ts) if entry_ts else ""

    price_journey = _compute_price_journey(entry_price, current_price, stop_price, target_price)
    lifecycle_events = _build_lifecycle_events(sym_decisions, symbol, "open", current_price)
    exit_scenarios = _build_exit_scenarios(symbol, entry_price, current_price, stop_price)

    return {
        "symbol": symbol,
        "status": "open",
        "tier": entry_action.get("tier", "core"),
        "strategy": "equity",
        "entry_price": entry_price,
        "exit_price": None,
        "current_price": current_price,
        "stop_price": stop_price,
        "target_price": target_price,
        "trail_tier": None,
        "shares": int(qty),
        "contracts": None,
        "entry_value": entry_price * qty,
        "current_value": current_price * qty,
        "pnl_usd": pnl_usd,
        "pnl_pct": pnl_pct,
        "pnl_status": "unrealized",
        "entry_date": entry_ts_str,
        "exit_date": None,
        "hold_days": None,
        "conviction_at_entry": entry_action.get("confidence") or entry_action.get("conviction", ""),
        "score_at_entry": None,
        "catalyst_at_entry": (entry_action.get("catalyst") or "")[:120],
        "entry_reasoning": (entry_cycle.get("reasoning") or "")[:400],
        "regime_at_entry": entry_cycle.get("regime") or entry_cycle.get("regime_view", ""),
        "exit_reason": None,
        "exit_reasoning": None,
        "bug_flag": bug_flag,
        "a2_companion": _find_a2_companion(symbol),
        "lifecycle_events": lifecycle_events,
        "price_journey": price_journey,
        "exit_scenarios": exit_scenarios,
    }


def _build_closed_lifecycle(trade: dict, sym_decisions: list[dict], bug_flag) -> dict:
    symbol = trade.get("symbol", "")
    entry_price = float(trade.get("entry_price") or 0)
    exit_price = float(trade.get("exit_price") or 0)
    qty = float(trade.get("qty") or 0)
    pnl_usd = float(trade.get("pnl") or 0)
    pnl_pct = float(trade.get("pnl_pct") or 0)
    entry_ts = trade.get("entry_time", "")
    exit_ts = trade.get("exit_time", "")
    hold_days = float(trade.get("holding_days") or 0)

    entry_dec = sym_decisions[0] if sym_decisions else {}
    entry_action = entry_dec.get("action", {}) if entry_dec else {}
    entry_cycle = entry_dec.get("cycle", {}) if entry_dec else {}
    exit_dec = sym_decisions[-1] if len(sym_decisions) > 1 else {}
    exit_cycle = exit_dec.get("cycle", {}) if exit_dec else {}

    stop_price = float(entry_action.get("stop_loss") or 0) or None

    price_journey = _compute_price_journey(entry_price, exit_price, stop_price, None)
    lifecycle_events = _build_lifecycle_events(sym_decisions, symbol, "closed", exit_price)

    return {
        "symbol": symbol,
        "status": "closed",
        "tier": trade.get("tier", "core"),
        "strategy": "equity",
        "entry_price": entry_price,
        "exit_price": exit_price,
        "current_price": exit_price,
        "stop_price": stop_price,
        "target_price": None,
        "trail_tier": None,
        "shares": int(qty),
        "contracts": None,
        "entry_value": entry_price * qty,
        "current_value": exit_price * qty,
        "pnl_usd": pnl_usd,
        "pnl_pct": pnl_pct,
        "pnl_status": "realized",
        "entry_date": entry_ts,
        "exit_date": exit_ts,
        "hold_days": hold_days,
        "conviction_at_entry": trade.get("conviction") or entry_action.get("confidence", ""),
        "score_at_entry": None,
        "catalyst_at_entry": (trade.get("catalyst") or "")[:120],
        "entry_reasoning": (trade.get("reasoning") or "")[:400],
        "regime_at_entry": trade.get("regime") or entry_cycle.get("regime", ""),
        "exit_reason": _infer_exit_reason(pnl_pct, stop_price, entry_price),
        "exit_reasoning": (exit_cycle.get("reasoning") or "")[:300],
        "bug_flag": _match_bug_flag(trade, bug_flag),
        "a2_companion": _find_a2_companion(symbol),
        "lifecycle_events": lifecycle_events,
        "price_journey": price_journey,
        "exit_scenarios": None,
    }


def _compute_price_journey(
    entry: float, current: float, stop: Optional[float], target: Optional[float]
) -> dict:
    """Compute percentage positions along the price bar for visualization."""
    if not entry or entry <= 0:
        return {"stop_pct": 0, "entry_pct": 50, "current_pct": 50, "target_pct": 100, "trail_pct": None}

    # Define the display range
    lo = min(filter(None, [stop, entry * 0.90, current * 0.90]))
    hi = max(filter(None, [target, entry * 1.15, current * 1.10]))
    span = max(hi - lo, 1.0)

    def pct(price):
        if price is None:
            return None
        return max(0.0, min(100.0, (price - lo) / span * 100))

    return {
        "stop_pct": pct(stop),
        "entry_pct": pct(entry),
        "current_pct": pct(current),
        "target_pct": pct(target),
        "trail_pct": None,
        "lo": lo,
        "hi": hi,
        "stop": stop,
        "entry": entry,
        "current": current,
        "target": target,
    }


def _build_lifecycle_events(
    sym_decisions: list[dict], symbol: str, status: str, current_price: float
) -> list[dict]:
    """Build timeline events from decision history for a symbol."""
    events = []
    seen = set()
    for i, item in enumerate(sym_decisions):
        cycle = item.get("cycle", {})
        action = item.get("action", {})
        dec_id = cycle.get("decision_id", "")
        ts = cycle.get("ts", "")
        if dec_id in seen:
            continue
        seen.add(dec_id)

        intent = (action.get("action") or action.get("intent", "hold") or "hold").lower()
        catalyst = (action.get("catalyst") or "")[:80]
        reasoning = (cycle.get("reasoning") or "")[:200]

        if i == 0 and intent in ("buy", "add"):
            event_type = "entry"
            label = f"ENTRY · {intent.upper()} @ market"
        elif i == len(sym_decisions) - 1 and status == "closed":
            event_type = "exit"
            label = f"EXIT · {intent.upper()} @ market"
        else:
            event_type = "hold"
            label = f"HOLD · {intent.upper() if intent not in ('hold', 'none', '') else 'MONITOR'}"

        events.append({
            "event_type": event_type,
            "timestamp": ts,
            "cycle_number": None,
            "label": label,
            "detail": catalyst or reasoning[:80],
            "sonnet_excerpt": reasoning[:160],
            "decision_id": dec_id,
        })

    if not events and status == "open":
        events.append({
            "event_type": "open",
            "timestamp": "",
            "cycle_number": None,
            "label": "OPEN — position active",
            "detail": "Position held in portfolio",
            "sonnet_excerpt": "",
            "decision_id": "",
        })

    return events


def _build_exit_scenarios(
    symbol: str, entry: float, current: float, stop: Optional[float]
) -> Optional[dict]:
    """Generate hypothetical exit scenarios for open positions."""
    if not entry or entry <= 0:
        return None
    return {
        "beat": f"+5% from here → ${current * 1.05:,.2f} (+{(current*1.05-entry)/entry*100:.1f}% vs entry)",
        "flat": f"No move → ${current:,.2f} ({(current-entry)/entry*100:+.1f}% vs entry)",
        "miss_5pct": f"-5% from here → ${current * 0.95:,.2f} ({(current*0.95-entry)/entry*100:+.1f}% vs entry)",
        "miss_10pct": f"-10% from here → ${current * 0.90:,.2f} ({(current*0.90-entry)/entry*100:+.1f}% vs entry)",
        "stop_hit": (
            f"Stop hit → ${stop:,.2f} ({(stop-entry)/entry*100:+.1f}% vs entry)"
            if stop else "No stop configured"
        ),
    }


def _infer_exit_reason(pnl_pct: float, stop_price: Optional[float], entry_price: float) -> str:
    if pnl_pct < -2.5 and stop_price:
        return "stop_loss"
    elif pnl_pct > 5:
        return "take_profit"
    elif abs(pnl_pct) < 1:
        return "time_exit"
    elif pnl_pct < 0:
        return "thesis_invalidated"
    else:
        return "manual_exit"


def _match_bug_flag(trade: dict, bug_flag) -> Optional[dict]:
    """Return bug flag if the trade's timeline overlaps the bug period."""
    if not bug_flag:
        return None
    flags = trade.get("bug_flags", [])
    if flags:
        return flags[0]
    return None


def _find_a2_companion(symbol: str) -> Optional[dict]:
    """Find any A2 options structure for this underlying symbol."""
    try:
        structs = _load_a2_structures()
        for s in structs:
            if s.get("underlying") == symbol or s.get("symbol") == symbol:
                return {
                    "structure_id": s.get("structure_id", ""),
                    "strategy": s.get("strategy", ""),
                    "lifecycle": s.get("lifecycle", ""),
                    "iv_rank": s.get("iv_rank"),
                    "direction": s.get("direction", ""),
                    "max_cost_usd": s.get("max_cost_usd"),
                }
    except Exception:
        pass
    return None


def _empty_lifecycle(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "status": "not_found",
        "tier": None,
        "strategy": None,
        "entry_price": None,
        "exit_price": None,
        "current_price": None,
        "stop_price": None,
        "target_price": None,
        "trail_tier": None,
        "shares": None,
        "contracts": None,
        "entry_value": None,
        "current_value": None,
        "pnl_usd": None,
        "pnl_pct": None,
        "pnl_status": None,
        "entry_date": None,
        "exit_date": None,
        "hold_days": None,
        "conviction_at_entry": None,
        "score_at_entry": None,
        "catalyst_at_entry": None,
        "entry_reasoning": None,
        "regime_at_entry": None,
        "exit_reason": None,
        "exit_reasoning": None,
        "bug_flag": None,
        "a2_companion": None,
        "lifecycle_events": [],
        "price_journey": None,
        "exit_scenarios": None,
    }


# ── Public: all trades summary ────────────────────────────────────────────────

def get_all_trades_summary() -> dict:
    """
    Summary list of all trades for the trade selector pills.
    Open positions first, then closed most-recent-first.
    """
    open_positions = _load_alpaca_positions()
    closed_trades = _build_closed_trades_safe()
    bug_log = _load_bug_fix_log()
    bug_symbols = {b.get("symbol") for b in bug_log if b.get("symbol")}

    items = []

    # Open positions
    for pos in open_positions:
        sym = getattr(pos, "symbol", "")
        pnl_pct = float(getattr(pos, "unrealized_plpc", 0) or 0) * 100
        pnl_usd = float(getattr(pos, "unrealized_pl", 0) or 0)
        entry_ts = str(getattr(pos, "created_at", ""))
        items.append({
            "symbol": sym,
            "status": "open",
            "pnl_pct": round(pnl_pct, 2),
            "pnl_usd": round(pnl_usd, 2),
            "has_bug": sym in bug_symbols,
            "entry_date": entry_ts[:10] if entry_ts else "",
            "outcome": "open",
            "pill_class": "tp-open",
        })

    # Closed trades (most recent first)
    for trade in reversed(closed_trades):
        sym = trade.get("symbol", "")
        pnl_pct = float(trade.get("pnl_pct") or 0)
        pnl_usd = float(trade.get("pnl") or 0)
        entry_ts = trade.get("entry_time", "")
        has_bug = bool(trade.get("bug_flags")) or sym in bug_symbols
        outcome = trade.get("outcome", "flat")
        if has_bug:
            pill_class = "tp-bug"
        elif outcome == "win":
            pill_class = "tp-win"
        elif outcome == "loss":
            pill_class = "tp-loss"
        else:
            pill_class = "tp-flat"
        items.append({
            "symbol": sym,
            "status": "closed",
            "pnl_pct": round(pnl_pct, 2),
            "pnl_usd": round(pnl_usd, 2),
            "has_bug": has_bug,
            "entry_date": entry_ts[:10] if entry_ts else "",
            "outcome": outcome,
            "pill_class": pill_class,
        })

    return {
        "trades": items,
        "open_count": len(open_positions),
        "closed_count": len(closed_trades),
        "total": len(items),
    }
