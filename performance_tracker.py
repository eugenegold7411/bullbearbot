"""
performance_tracker.py — Shadow performance measurement system.

Measures whether decision-cycle conviction, portfolio allocator recommendations,
and A2 routing rules are actually producing alpha.

Data streams (written by call sites in respective modules):
  data/analytics/trade_ideas.jsonl               — decision-cycle trade ideas
  data/analytics/allocator_recommendations.jsonl — ADD/TRIM/REPLACE recs
  data/analytics/a2_structure_outcomes.jsonl     — closed/cancelled A2 structures

Nightly computation at 4:30 AM ET:
  compute_overnight_outcomes() — fills in outcome_1d/3d/5d, followed_by_bot

Weekly Haiku report at Sunday 6 AM ET:
  generate_weekly_performance_report() — writes weekly_performance_report.json

Dashboard:
  load_performance_summary() — reads data/analytics/performance_summary.json
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_ROOT                = Path(__file__).parent
_TRADE_IDEAS_PATH    = _ROOT / "data" / "analytics" / "trade_ideas.jsonl"
_ALLOCATOR_RECS_PATH = _ROOT / "data" / "analytics" / "allocator_recommendations.jsonl"
_A2_OUTCOMES_PATH    = _ROOT / "data" / "analytics" / "a2_structure_outcomes.jsonl"
_SUMMARY_PATH        = _ROOT / "data" / "analytics" / "performance_summary.json"
_WEEKLY_REPORT_PATH  = _ROOT / "data" / "analytics" / "weekly_performance_report.json"

_INTENTS_TO_LOG  = {"enter_long", "enter_short", "reduce", "close"}
_BEARISH_INTENTS = {"enter_short", "reduce", "close"}


# ─────────────────────────────────────────────────────────────────────────────
# JSONL I/O helpers
# ─────────────────────────────────────────────────────────────────────────────

def _append_jsonl(path: Path, records: list) -> None:
    """Append records to a JSONL file. Non-fatal."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, default=str) + "\n")
    except Exception as exc:
        log.warning("[PERF] _append_jsonl %s failed: %s", path.name, exc)


def _load_jsonl(path: Path) -> list:
    """Load all records from a JSONL file. Returns [] if missing."""
    if not path.exists():
        return []
    records = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except Exception as exc:
        log.warning("[PERF] _load_jsonl %s failed: %s", path.name, exc)
    return records


def _rewrite_jsonl(path: Path, records: list) -> None:
    """Atomically rewrite a JSONL file. Non-fatal."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, default=str) + "\n")
        os.replace(tmp, path)
    except Exception as exc:
        log.warning("[PERF] _rewrite_jsonl %s failed: %s", path.name, exc)


def _records_last_n_days(records: list, days: int, ts_key: str = "timestamp") -> list:
    """Filter records to those from the last N calendar days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = []
    for rec in records:
        try:
            ts = rec.get(ts_key) or rec.get("timestamp_opened", "")
            if not ts:
                continue
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                result.append(rec)
        except Exception:
            pass
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Trading day helpers
# ─────────────────────────────────────────────────────────────────────────────

def _trading_days_elapsed(ts_str: str) -> int:
    """Count Mon-Fri trading days elapsed since ts_str (ISO UTC)."""
    try:
        dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        today = datetime.now(timezone.utc).date()
        d = dt.date()
        count = 0
        while d < today:
            d += timedelta(days=1)
            if d.weekday() < 5:
                count += 1
        return count
    except Exception:
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# Logging entry points (called by respective modules)
# ─────────────────────────────────────────────────────────────────────────────

def log_trade_ideas(
    ideas,
    approved_symbols: set,
    executed_symbols: set,
    rejection_map: dict,
    prices: dict,
    signal_scores_obj: dict,
    session_tier: str,
    decision_id: str,
    broker_actions_map: dict,
) -> None:
    """Append actionable trade ideas (enter/reduce/close) to trade_ideas.jsonl. Non-fatal."""
    try:
        now_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        scored = (signal_scores_obj or {}).get("scored_symbols", {})
        if not isinstance(scored, dict):
            scored = {}

        records = []
        for idea in (ideas or []):
            intent = (getattr(idea, "intent", "") or "").lower()
            if intent not in _INTENTS_TO_LOG:
                continue

            raw_sym = getattr(idea, "symbol", "") or ""
            try:
                from schemas import normalize_symbol as _ns  # noqa: PLC0415
                sym = _ns(raw_sym)
            except Exception:
                sym = raw_sym.upper()

            sig_data = scored.get(sym, {}) if isinstance(scored, dict) else {}
            price = prices.get(sym) or prices.get(raw_sym)
            price = float(price) if price else None
            score = int(sig_data.get("score", 0) or 0) if isinstance(sig_data, dict) else 0
            kernel_ok = sym in approved_symbols

            ba = broker_actions_map.get(sym)
            stop_proposed = None
            target_proposed = None
            if ba is not None:
                _sl = getattr(ba, "stop_loss", None)
                _tp = getattr(ba, "take_profit", None)
                stop_proposed   = float(_sl) if _sl else None
                target_proposed = float(_tp) if _tp else None
            if stop_proposed is None:
                adv = getattr(idea, "advisory_stop_pct", None)
                if adv and price:
                    stop_proposed = round(price * (1.0 - float(adv)), 4)

            tier_val = ""
            _t = getattr(idea, "tier", None)
            if _t is not None:
                tier_val = (_t.value if hasattr(_t, "value") else str(_t)).upper()

            dir_val = ""
            _d = getattr(idea, "direction", None)
            if _d is not None:
                dir_val = _d.value if hasattr(_d, "value") else str(_d)

            records.append({
                "timestamp":         now_ts,
                "decision_id":       decision_id or "",
                "symbol":            sym,
                "intent":            intent,
                "tier":              tier_val,
                "conviction":        round(float(getattr(idea, "conviction", 0) or 0), 4),
                "score":             score,
                "direction":         dir_val,
                "catalyst":          (getattr(idea, "catalyst", "") or "")[:200],
                "kernel_result":     "approved" if kernel_ok else "rejected",
                "rejection_reason":  str(rejection_map.get(sym, ""))[:200] if not kernel_ok else None,
                "price_at_decision": round(price, 4) if price else None,
                "stop_proposed":     round(stop_proposed, 4) if stop_proposed else None,
                "target_proposed":   round(target_proposed, 4) if target_proposed else None,
                "executed":          kernel_ok and sym in executed_symbols,
                "session":           session_tier,
                "outcome_1d":        None,
                "outcome_3d":        None,
                "outcome_5d":        None,
                "outcome_closed":    None,
                "outcome_filled_at": None,
            })

        if records:
            _append_jsonl(_TRADE_IDEAS_PATH, records)
            log.debug("[PERF] logged %d trade ideas  decision=%s", len(records), decision_id)
    except Exception as exc:
        log.debug("[PERF] log_trade_ideas failed (non-fatal): %s", exc)


def log_allocator_recommendations(
    proposed_actions: list,
    incumbents: list,
    candidates: list,
    positions: list,
    cycle_id: str = "",
) -> None:
    """Append ADD/TRIM/REPLACE recommendations to allocator_recommendations.jsonl. Non-fatal."""
    try:
        now_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        price_map: dict = {}
        for pos in (positions or []):
            try:
                price_map[pos.symbol] = float(pos.current_price or 0)
            except Exception:
                pass

        inc_map: dict  = {inc["symbol"]: inc for inc in (incumbents or [])}
        cand_map: dict = {c["symbol"]: c for c in (candidates or [])}

        records = []
        for action in (proposed_actions or []):
            act = action.get("action", "")
            if act not in ("ADD", "TRIM", "REPLACE"):
                continue

            sym  = action.get("symbol", "")
            inc  = inc_map.get(sym)
            cand = cand_map.get(sym)

            price    = price_map.get(sym) or float((cand or {}).get("price", 0) or 0) or None
            acct_pct = round((inc or {}).get("account_pct", 0.0) / 100.0, 4) if inc else 0.0

            if act in ("ADD", "TRIM") and inc:
                conviction = round(inc["thesis_score_normalized"] / 100.0, 4)
            elif act == "REPLACE" and cand:
                conviction = round(float(cand.get("signal_score", 0) or 0) / 100.0, 4)
            else:
                conviction = 0.0

            records.append({
                "timestamp":                     now_ts,
                "cycle_id":                      cycle_id or now_ts,
                "symbol":                        sym,
                "action":                        act,
                "reason":                        (action.get("reason") or "")[:200],
                "conviction":                    conviction,
                "price_at_recommendation":       round(float(price), 4) if price else None,
                "account_pct_at_recommendation": acct_pct,
                "followed_by_bot":               None,
                "outcome_1d":                    None,
                "outcome_3d":                    None,
                "outcome_5d":                    None,
                "outcome_filled_at":             None,
            })

        if records:
            _append_jsonl(_ALLOCATOR_RECS_PATH, records)
            log.debug("[PERF] logged %d allocator recs", len(records))
    except Exception as exc:
        log.debug("[PERF] log_allocator_recommendations failed (non-fatal): %s", exc)


def log_a2_structure_outcome(structure) -> None:
    """Append outcome when A2 structure lifecycle → CLOSED or CANCELLED (submitted). Non-fatal."""
    try:
        from schemas import StructureLifecycle  # noqa: PLC0415

        lifecycle = structure.lifecycle
        is_closed    = lifecycle == StructureLifecycle.CLOSED
        is_cancelled = lifecycle == StructureLifecycle.CANCELLED

        if not (is_closed or is_cancelled):
            return
        if is_cancelled and not structure.order_ids:
            return

        now_ts     = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        opened_str = structure.opened_at or now_ts
        closed_str = structure.closed_at or now_ts

        dte_at_entry = dte_at_exit = None
        expiry_str = structure.expiration or ""
        if expiry_str:
            try:
                from datetime import date as _date  # noqa: PLC0415
                expiry_d = _date.fromisoformat(expiry_str[:10])
                opened_d = datetime.fromisoformat(opened_str.replace("Z", "+00:00")).date()
                closed_d = datetime.fromisoformat(closed_str.replace("Z", "+00:00")).date()
                dte_at_entry = max(0, (expiry_d - opened_d).days)
                dte_at_exit  = max(0, (expiry_d - closed_d).days)
            except Exception:
                pass

        entry_price = structure.debit_paid
        if entry_price is None:
            entry_price = structure.net_debit_per_contract()

        realized = structure.realized_pnl
        exit_price = None
        if entry_price is not None and realized is not None and structure.contracts > 0:
            exit_price = round(entry_price + realized / (structure.contracts * 100), 4)

        max_gain = structure.max_profit_usd or 0.0
        max_loss = abs(structure.max_cost_usd or 0.0)

        pnl_pct_of_max = None
        if realized is not None and max_gain and max_gain != 0:
            pnl_pct_of_max = round(realized / abs(float(max_gain)) * 100, 1)

        if is_cancelled:
            exit_reason, outcome = "cancelled_unfilled", "cancelled"
        else:
            code = (structure.close_reason_code or "").lower()
            if "target" in code or "profit" in code:
                exit_reason = "target_hit"
            elif "stop" in code:
                exit_reason = "stop_hit"
            elif "expir" in code:
                exit_reason = "expired"
            else:
                exit_reason = "manual"
            if realized is None:
                outcome = "cancelled"
            elif realized > 0.0:
                outcome = "win"
            elif realized < 0.0:
                outcome = "loss"
            else:
                outcome = "breakeven"

        strategy_val = structure.strategy.value if hasattr(structure.strategy, "value") else str(structure.strategy)

        record = {
            "timestamp_opened":    opened_str,
            "timestamp_closed":    closed_str,
            "structure_id":        structure.structure_id,
            "symbol":              structure.underlying,
            "strategy":            strategy_val,
            "rule_fired":          structure.close_reason_code or "unknown",
            "debate_confidence":   getattr(structure, "debate_confidence", None),
            "entry_price":         round(float(entry_price), 4) if entry_price is not None else None,
            "exit_price":          round(float(exit_price),  4) if exit_price  is not None else None,
            "max_gain":            round(float(max_gain), 2),
            "max_loss":            round(float(max_loss), 2),
            "dte_at_entry":        dte_at_entry,
            "dte_at_exit":         dte_at_exit,
            "exit_reason":         exit_reason,
            "pnl_usd":             round(float(realized), 2) if realized is not None else None,
            "pnl_pct_of_max_gain": pnl_pct_of_max,
            "outcome":             outcome,
        }

        _append_jsonl(_A2_OUTCOMES_PATH, [record])
        log.debug("[PERF] logged A2 outcome: %s %s %s", structure.structure_id, strategy_val, outcome)
    except Exception as exc:
        log.debug("[PERF] log_a2_structure_outcome failed (non-fatal): %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Price fetching
# ─────────────────────────────────────────────────────────────────────────────

def _to_yf_ticker(sym: str) -> str:
    return sym.replace("/", "-")


def _isnan(v) -> bool:
    try:
        import math
        return math.isnan(float(v))
    except Exception:
        return True


def _fetch_close_prices(symbols: list, days_back: int = 90) -> dict:
    """
    Fetch daily closing prices for symbols. Returns {symbol: {date_str: close}}.
    Non-fatal — returns {} on any error.
    """
    if not symbols:
        return {}
    try:
        import yfinance as yf  # noqa: PLC0415
        yf_tickers = [_to_yf_ticker(s) for s in symbols]
        df = yf.download(
            yf_tickers, period=f"{days_back}d", interval="1d",
            auto_adjust=True, progress=False,
        )["Close"]
        if df is None or df.empty:
            return {}

        result: dict = {}
        is_multi = len(yf_tickers) > 1

        for orig_sym, yf_sym in zip(symbols, yf_tickers):
            try:
                col = df[yf_sym] if is_multi else df
                price_by_date: dict = {}
                for ts, price in col.items():
                    if price is not None and not _isnan(price):
                        price_by_date[str(ts.date())] = round(float(price), 4)
                result[orig_sym] = price_by_date
            except Exception:
                result[orig_sym] = {}

        return result
    except Exception as exc:
        log.warning("[PERF] _fetch_close_prices failed (non-fatal): %s", exc)
        return {}


def _find_nth_trading_close(
    close_by_date: dict,
    reference_date: str,
    n: int,
) -> Optional[float]:
    """Close price N trading days after reference_date, using sorted available dates."""
    sorted_dates = sorted(close_by_date.keys())
    after_idx = None
    for i, d in enumerate(sorted_dates):
        if d > reference_date:
            after_idx = i
            break
    if after_idx is None:
        return None
    target_idx = after_idx + n - 1
    if target_idx < 0 or target_idx >= len(sorted_dates):
        return None
    return close_by_date.get(sorted_dates[target_idx])


def _compute_pct_return(price_at: float, price_after: float, intent: str) -> float:
    """Return % change, flipped for bearish intents so positive = correct call."""
    ret = (price_after - price_at) / price_at
    if intent in _BEARISH_INTENTS:
        ret = -ret
    return round(ret * 100, 4)


# ─────────────────────────────────────────────────────────────────────────────
# Nightly outcome computation
# ─────────────────────────────────────────────────────────────────────────────

def _compute_trade_idea_outcomes(price_fetcher=None) -> int:
    """Fill in outcome_1d/3d/5d for mature trade_ideas records. Returns count updated."""
    records = _load_jsonl(_TRADE_IDEAS_PATH)
    if not records:
        return 0

    needs = [r for r in records if r.get("price_at_decision") is not None and (
        r.get("outcome_1d") is None or r.get("outcome_3d") is None or r.get("outcome_5d") is None)]
    if not needs:
        return 0

    symbols = list({r["symbol"] for r in needs if r.get("symbol")})
    close_prices = (price_fetcher or _fetch_close_prices)(symbols)

    updated = 0
    for rec in records:
        price_at = float(rec.get("price_at_decision") or 0)
        sym      = rec.get("symbol", "")
        ts       = rec.get("timestamp", "")
        if price_at <= 0 or not sym or not ts:
            continue

        close_by_date = close_prices.get(sym, {})
        if not close_by_date:
            continue

        try:
            dec_date = datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except Exception:
            continue

        intent  = rec.get("intent", "")
        changed = False
        elapsed = _trading_days_elapsed(ts)

        for n, key in ((1, "outcome_1d"), (3, "outcome_3d"), (5, "outcome_5d")):
            if rec.get(key) is not None or elapsed < n:
                continue
            try:
                p = _find_nth_trading_close(close_by_date, dec_date, n)
                if p is not None:
                    rec[key] = _compute_pct_return(price_at, p, intent)
                    changed = True
            except Exception:
                pass

        if changed:
            rec["outcome_filled_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            updated += 1

    if updated > 0:
        _rewrite_jsonl(_TRADE_IDEAS_PATH, records)
        log.info("[PERF] Updated %d trade_ideas outcome records", updated)
    return updated


def _compute_allocator_outcomes(price_fetcher=None) -> int:
    """Fill in outcome fields and followed_by_bot for allocator_recommendations records."""
    rec_list = _load_jsonl(_ALLOCATOR_RECS_PATH)
    if not rec_list:
        return 0

    symbols = list({r["symbol"] for r in rec_list
                    if r.get("price_at_recommendation") is not None and r.get("symbol")
                    and (r.get("outcome_1d") is None or r.get("outcome_5d") is None)})
    close_prices = (price_fetcher or _fetch_close_prices)(symbols) if symbols else {}

    # Load trade_ideas for followed_by_bot check
    ideas = _load_jsonl(_TRADE_IDEAS_PATH)
    ideas_by_sym: dict = {}
    for idea in ideas:
        s = idea.get("symbol", "")
        if s:
            ideas_by_sym.setdefault(s, []).append(idea)

    updated = 0
    for rec in rec_list:
        changed  = False
        sym      = rec.get("symbol", "")
        ts       = rec.get("timestamp", "")
        action   = rec.get("action", "")
        price_at = float(rec.get("price_at_recommendation") or 0)
        if not sym or not ts:
            continue

        # Outcome computation
        if price_at > 0:
            close_by_date = close_prices.get(sym, {})
            try:
                rec_date = datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%Y-%m-%d")
            except Exception:
                rec_date = ""

            intent_proxy = "reduce" if action == "TRIM" else "enter_long"
            elapsed = _trading_days_elapsed(ts)

            for n, key in ((1, "outcome_1d"), (3, "outcome_3d"), (5, "outcome_5d")):
                if rec.get(key) is not None or elapsed < n or not close_by_date or not rec_date:
                    continue
                try:
                    p = _find_nth_trading_close(close_by_date, rec_date, n)
                    if p is not None:
                        rec[key] = _compute_pct_return(price_at, p, intent_proxy)
                        changed = True
                except Exception:
                    pass

        # followed_by_bot
        if rec.get("followed_by_bot") is None:
            try:
                rec_dt  = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                cutoff  = rec_dt + timedelta(minutes=10)
                followed = False
                for idea in ideas_by_sym.get(sym, []):
                    i_ts = idea.get("timestamp", "")
                    if not i_ts:
                        continue
                    i_dt = datetime.fromisoformat(i_ts.replace("Z", "+00:00"))
                    if not (rec_dt <= i_dt <= cutoff):
                        continue
                    if not idea.get("executed"):
                        continue
                    i_intent = idea.get("intent", "")
                    if action in ("ADD", "REPLACE") and i_intent in ("enter_long", "enter_short"):
                        followed = True
                        break
                    if action == "TRIM" and i_intent in ("reduce", "close"):
                        followed = True
                        break
                rec["followed_by_bot"] = followed
                changed = True
            except Exception:
                pass

        if changed:
            rec["outcome_filled_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            updated += 1

    if updated > 0:
        _rewrite_jsonl(_ALLOCATOR_RECS_PATH, rec_list)
        log.info("[PERF] Updated %d allocator_recommendations records", updated)
    return updated


def compute_overnight_outcomes(price_fetcher=None) -> None:
    """
    Fill in outcome fields for all JSONL records old enough to have prices.
    Non-fatal per file — one failure doesn't abort the others.
    Accepts price_fetcher for testing (default: _fetch_close_prices).
    """
    log.info("[PERF] Starting overnight outcome computation")
    try:
        n1 = _compute_trade_idea_outcomes(price_fetcher)
        log.info("[PERF] Trade ideas: %d records updated", n1)
    except Exception as exc:
        log.warning("[PERF] Trade idea outcomes failed (non-fatal): %s", exc)
    try:
        n2 = _compute_allocator_outcomes(price_fetcher)
        log.info("[PERF] Allocator recs: %d records updated", n2)
    except Exception as exc:
        log.warning("[PERF] Allocator outcomes failed (non-fatal): %s", exc)
    try:
        summary = _compute_performance_summary()
        _save_summary(summary)
        si = summary.get("trade_ideas", {})
        al = summary.get("allocator", {})
        a2 = summary.get("a2_structures", {})
        log.info("[PERF] performance_summary.json updated  ideas_7d=%d alloc_7d=%d a2_7d=%d",
                 si.get("total_ideas_7d", 0),
                 al.get("total_recommendations_7d", 0),
                 a2.get("total_submitted_7d", 0))
    except Exception as exc:
        log.warning("[PERF] Performance summary failed (non-fatal): %s", exc)
    log.info("[PERF] Overnight computation complete")


# ─────────────────────────────────────────────────────────────────────────────
# Performance summary
# ─────────────────────────────────────────────────────────────────────────────

def _pct(num: int, denom: int) -> Optional[float]:
    if denom <= 0:
        return None
    return round(num / denom * 100, 1)


def _avg(values: list) -> Optional[float]:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 2)


def _estimate_data_days(records: list) -> int:
    dates = set()
    for r in records:
        ts = r.get("timestamp", "")
        if ts:
            try:
                dates.add(datetime.fromisoformat(str(ts).replace("Z", "+00:00")).strftime("%Y-%m-%d"))
            except Exception:
                pass
    return len(dates)


def _compute_performance_summary(days_back: int = 7) -> dict:
    """Compute stats from last N days of all three JSONL streams."""
    now_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    ideas_all = _load_jsonl(_TRADE_IDEAS_PATH)
    ideas_7d  = _records_last_n_days(ideas_all, days_back)
    approved  = [r for r in ideas_7d if r.get("kernel_result") == "approved"]
    rejected  = [r for r in ideas_7d if r.get("kernel_result") == "rejected"]

    apr_1d = [r for r in approved if r.get("outcome_1d") is not None]
    apr_5d = [r for r in approved if r.get("outcome_5d") is not None]
    rej_1d = [r for r in rejected if r.get("outcome_1d") is not None]

    hi_conv = [r for r in ideas_7d if float(r.get("conviction") or 0) >= 0.75 and r.get("outcome_5d") is not None]
    lo_conv = [r for r in ideas_7d if float(r.get("conviction") or 0) <  0.65 and r.get("outcome_5d") is not None]

    def _tier_stats(tier: str) -> dict:
        t_recs = [r for r in ideas_7d if r.get("tier", "").upper() == tier]
        t_5d   = [r for r in t_recs if r.get("outcome_5d") is not None]
        return {"count": len(t_recs), "profitable_5d_pct": _pct(sum(1 for r in t_5d if (r.get("outcome_5d") or 0) > 0), len(t_5d))}

    alloc_all = _load_jsonl(_ALLOCATOR_RECS_PATH)
    alloc_7d  = _records_last_n_days(alloc_all, days_back)
    add_recs  = [r for r in alloc_7d if r.get("action") == "ADD"]
    trim_recs = [r for r in alloc_7d if r.get("action") == "TRIM"]
    add_1d    = [r for r in add_recs  if r.get("outcome_1d") is not None]
    add_5d    = [r for r in add_recs  if r.get("outcome_5d") is not None]
    trim_1d   = [r for r in trim_recs if r.get("outcome_1d") is not None]
    trim_5d   = [r for r in trim_recs if r.get("outcome_5d") is not None]
    followed  = [r for r in alloc_7d if r.get("followed_by_bot") is True]
    false_trim = [r for r in trim_1d if (r.get("outcome_1d") or 0) < 0]

    a2_all       = _load_jsonl(_A2_OUTCOMES_PATH)
    a2_7d        = _records_last_n_days(a2_all, days_back, ts_key="timestamp_opened")
    a2_submitted = [r for r in a2_7d if r.get("outcome") != "cancelled"]
    a2_closed    = [r for r in a2_submitted if r.get("outcome") in ("win", "loss", "breakeven")]
    a2_wins      = [r for r in a2_closed if r.get("outcome") == "win"]

    def _a2_rule(rule: str) -> dict:
        recs  = [r for r in a2_closed if (r.get("rule_fired") or "").upper() == rule]
        wins  = sum(1 for r in recs if r.get("outcome") == "win")
        return {"count": len(recs), "win_rate_pct": _pct(wins, len(recs)), "avg_pnl": _avg([r.get("pnl_usd") for r in recs])}

    def _a2_strat(strat: str) -> dict:
        recs = [r for r in a2_closed if (r.get("strategy") or "").lower() == strat.lower()]
        wins = sum(1 for r in recs if r.get("outcome") == "win")
        return {"count": len(recs), "win_rate_pct": _pct(wins, len(recs))}

    hi_conf = [r for r in a2_closed if float(r.get("debate_confidence") or 0) >= 0.80]
    lo_conf = [r for r in a2_closed if 0 < float(r.get("debate_confidence") or 0) < 0.75]

    return {
        "computed_at": now_ts,
        "days_back":   days_back,
        "data_days":   _estimate_data_days(ideas_all),
        "trade_ideas": {
            "total_ideas_7d":                          len(ideas_7d),
            "approved_pct":                            _pct(len(approved), len(ideas_7d)),
            "approved_profitable_1d_pct":              _pct(sum(1 for r in apr_1d if (r.get("outcome_1d") or 0) > 0), len(apr_1d)),
            "approved_profitable_5d_pct":              _pct(sum(1 for r in apr_5d if (r.get("outcome_5d") or 0) > 0), len(apr_5d)),
            "rejected_wouldve_been_profitable_1d_pct": _pct(sum(1 for r in rej_1d if (r.get("outcome_1d") or 0) > 0), len(rej_1d)),
            "conviction_calibration": {
                "high_conviction_75plus_profitable_pct": _pct(sum(1 for r in hi_conv if (r.get("outcome_5d") or 0) > 0), len(hi_conv)),
                "low_conviction_sub_65_profitable_pct":  _pct(sum(1 for r in lo_conv if (r.get("outcome_5d") or 0) > 0), len(lo_conv)),
            },
            "by_tier": {
                "CORE":     _tier_stats("CORE"),
                "DYNAMIC":  _tier_stats("DYNAMIC"),
                "INTRADAY": _tier_stats("INTRADAY"),
            },
        },
        "allocator": {
            "total_recommendations_7d": len(alloc_7d),
            "add_accuracy_1d_pct":      _pct(sum(1 for r in add_1d  if (r.get("outcome_1d") or 0) > 0), len(add_1d)),
            "add_accuracy_5d_pct":      _pct(sum(1 for r in add_5d  if (r.get("outcome_5d") or 0) > 0), len(add_5d)),
            "trim_accuracy_1d_pct":     _pct(sum(1 for r in trim_1d if (r.get("outcome_1d") or 0) > 0), len(trim_1d)),
            "trim_accuracy_5d_pct":     _pct(sum(1 for r in trim_5d if (r.get("outcome_5d") or 0) > 0), len(trim_5d)),
            "follow_rate_pct":          _pct(len(followed), len(alloc_7d)),
            "false_trim_rate_pct":      _pct(len(false_trim), len(trim_1d)),
        },
        "a2_structures": {
            "total_submitted_7d":      len(a2_7d),
            "fill_rate_pct":           _pct(len(a2_submitted), len(a2_7d)) if a2_7d else None,
            "win_rate_pct":            _pct(len(a2_wins), len(a2_closed)) if a2_closed else None,
            "avg_pnl_pct_of_max_gain": _avg([r.get("pnl_pct_of_max_gain") for r in a2_closed]),
            "by_rule": {
                "RULE5":              _a2_rule("RULE5"),
                "RULE2_CREDIT":       _a2_rule("RULE2_CREDIT"),
                "RULE_POST_EARNINGS": _a2_rule("RULE_POST_EARNINGS"),
            },
            "by_strategy": {
                "call_debit_spread": _a2_strat("call_debit_spread"),
                "credit_put_spread": _a2_strat("credit_put_spread"),
            },
            "debate_confidence_calibration": {
                "high_conf_80plus_win_pct": _pct(sum(1 for r in hi_conf if r.get("outcome") == "win"), len(hi_conf)),
                "low_conf_sub_75_win_pct":  _pct(sum(1 for r in lo_conf if r.get("outcome") == "win"), len(lo_conf)),
            },
        },
    }


def _save_summary(summary: dict) -> None:
    """Atomically save performance_summary.json. Non-fatal."""
    try:
        _SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SUMMARY_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(summary, indent=2, default=str))
        os.replace(tmp, _SUMMARY_PATH)
    except Exception as exc:
        log.warning("[PERF] _save_summary failed: %s", exc)


def load_performance_summary() -> dict:
    """Load performance_summary.json. Returns {} if missing or >25h stale."""
    try:
        if not _SUMMARY_PATH.exists():
            return {}
        data = json.loads(_SUMMARY_PATH.read_text())
        ts = data.get("computed_at", "")
        if ts:
            dt  = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
            if age > 25:
                return {}
        return data
    except Exception:
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Weekly Haiku report
# ─────────────────────────────────────────────────────────────────────────────

def generate_weekly_performance_report() -> None:
    """
    Generate a Haiku-written weekly performance analysis.
    Reads the three JSONL files and performance_summary.json.
    Writes data/analytics/weekly_performance_report.json.
    Non-fatal.
    """
    try:
        log.info("[PERF] Generating weekly performance report")
        summary = _compute_performance_summary(days_back=7)
        _save_summary(summary)

        week_str  = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        data_days = summary.get("data_days", 0)
        si = summary.get("trade_ideas", {})
        al = summary.get("allocator", {})
        a2 = summary.get("a2_structures", {})
        cc = (si.get("conviction_calibration") or {})
        dc = (a2.get("debate_confidence_calibration") or {})

        def _fmt(v) -> str:
            return f"{v:.1f}" if v is not None else "N/A"

        prompt = (
            f"Write a 400-500 word weekly performance report for an autonomous paper trading bot.\n"
            f"Week of {week_str}. Data available: {data_days} trading day(s).\n\n"
            f"TRADE IDEA CONVICTION DATA ({si.get('total_ideas_7d', 0)} ideas, "
            f"{_fmt(si.get('approved_pct'))}% approved):\n"
            f"- Approved profitable 1d: {_fmt(si.get('approved_profitable_1d_pct'))}%\n"
            f"- Approved profitable 5d: {_fmt(si.get('approved_profitable_5d_pct'))}%\n"
            f"- Kernel false rejection rate 1d: {_fmt(si.get('rejected_wouldve_been_profitable_1d_pct'))}%\n"
            f"- High conviction (≥0.75) 5d profitable: {_fmt(cc.get('high_conviction_75plus_profitable_pct'))}%\n"
            f"- Low conviction (<0.65) 5d profitable: {_fmt(cc.get('low_conviction_sub_65_profitable_pct'))}%\n\n"
            f"ALLOCATOR ({al.get('total_recommendations_7d', 0)} recs, "
            f"follow rate: {_fmt(al.get('follow_rate_pct'))}%):\n"
            f"- ADD accuracy 1d/5d: {_fmt(al.get('add_accuracy_1d_pct'))}% / {_fmt(al.get('add_accuracy_5d_pct'))}%\n"
            f"- TRIM accuracy 1d/5d: {_fmt(al.get('trim_accuracy_1d_pct'))}% / {_fmt(al.get('trim_accuracy_5d_pct'))}%\n"
            f"- False trim rate: {_fmt(al.get('false_trim_rate_pct'))}%\n\n"
            f"A2 STRUCTURES ({a2.get('total_submitted_7d', 0)} submitted):\n"
            f"- Fill rate: {_fmt(a2.get('fill_rate_pct'))}%\n"
            f"- Win rate: {_fmt(a2.get('win_rate_pct'))}%\n"
            f"- Avg P&L: {_fmt(a2.get('avg_pnl_pct_of_max_gain'))}% of max gain\n"
            f"- High conf (≥0.80) win: {_fmt(dc.get('high_conf_80plus_win_pct'))}%  "
            f"Low conf (<0.75) win: {_fmt(dc.get('low_conf_sub_75_win_pct'))}%\n\n"
            "Write the report in this exact format:\n\n"
            f"WEEKLY PERFORMANCE REPORT — Week of {week_str}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "SONNET CONVICTION QUALITY\n"
            "- [bullet 1]\n- [bullet 2]\n- [bullet 3]\n\n"
            "ALLOCATOR ACCURACY\n"
            "- [bullet 1]\n- [bullet 2]\n\n"
            "A2 STRUCTURE PERFORMANCE\n"
            "- [bullet 1]\n- [bullet 2]\n\n"
            "SYSTEM HEALTH ASSESSMENT\n"
            "[1-2 sentence overall assessment for live promotion on May 16 2026]\n"
            "[Specific concerns if any]\n"
            "[Recommendation: promote to live / continue paper trading / fix X first]"
        )

        from bot_clients import MODEL_FAST, _get_claude  # noqa: PLC0415
        response = _get_claude().messages.create(
            model=MODEL_FAST, max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        try:
            from cost_tracker import get_tracker  # noqa: PLC0415
            get_tracker().record_api_call(MODEL_FAST, response.usage, caller="performance_tracker")
        except Exception:
            pass

        report_text = response.content[0].text.strip()
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "week_of":      week_str,
            "data_days":    data_days,
            "report_text":  report_text,
            "summary":      summary,
        }

        _WEEKLY_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _WEEKLY_REPORT_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(report, indent=2, default=str))
        os.replace(tmp, _WEEKLY_REPORT_PATH)
        log.info("[PERF] Weekly report written (%d chars)", len(report_text))
    except Exception as exc:
        log.warning("[PERF] generate_weekly_performance_report failed (non-fatal): %s", exc)
