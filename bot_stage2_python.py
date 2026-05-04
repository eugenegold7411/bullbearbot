"""
bot_stage2_python.py — Layer 2: deterministic Python signal scorer.

Pure-Python scoring using already-computed numerical indicators. Zero Claude
API calls. Runs before the Haiku synthesis layer (Stage 2 / L3) to produce
a reliable numerical anchor for every tracked symbol.

Public API
----------
score_symbol_python(sym, md, regime)     -> dict
score_all_symbols_python(symbols, md, regime) -> dict[str, dict]

Output shape matches the `scored_symbols[sym]` block of the existing signal
scorer so L3 can wrap / adjust rather than re-score from scratch.

Inputs required on `md`:
  md["ind_by_symbol"][sym]       — daily indicator dict (RSI/MACD/MA/EMA/vol)
  md["intraday_summaries"][sym]  — 5-min intraday summary dict (optional)
  md["current_prices"][sym]      — live price
All disk artefacts (morning brief, ORB, earnings, pattern watchlist,
insider cache) are loaded lazily and cached at module level for the cycle.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_BASE = Path(__file__).parent
_STRATEGY_CONFIG_PATH = _BASE / "strategy_config.json"
_MORNING_BRIEF_PATH   = _BASE / "data" / "market" / "morning_brief.json"
_ORB_PATH             = _BASE / "data" / "scanner" / "orb_candidates.json"
_EARNINGS_CAL_PATH    = _BASE / "data" / "market" / "earnings_calendar.json"

# ── Signal weights (loaded once from strategy_config.json) ───────────────────

_SIGNAL_WEIGHTS_DEFAULT: dict = {
    "momentum_weight":       0.35,
    "mean_reversion_weight": 0.20,
    "news_sentiment_weight": 0.30,
    "cross_sector_weight":   0.15,
}
_SIGNAL_SOURCE_WEIGHTS_DEFAULT: dict = {
    "congressional":   "low",
    "form4_insider":   "medium",
    "reddit_sentiment": "low",
    "orb_breakout":    "medium",
    "macro_wire":      "high",
    "earnings_intel":  "medium",
}

_SIGNAL_WEIGHTS_CACHE: Optional[dict] = None
_SIGNAL_SOURCE_WEIGHTS_CACHE: Optional[dict] = None

_SAFETY_DEDUP_SECS = 300
_SAFETY_ALERT_CACHE: dict[str, float] = {}


def _fire_safety_alert(fn_name: str, exc: Exception) -> None:
    """Fire a CRITICAL WhatsApp alert when a safety function throws. 5-min dedup. Never raises.
    # TODO(DASHBOARD): surface safety_system_degraded alerts on dashboard
    """
    try:
        now = time.time()
        if now - _SAFETY_ALERT_CACHE.get(fn_name, 0) < _SAFETY_DEDUP_SECS:
            return
        _SAFETY_ALERT_CACHE[fn_name] = now
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        msg = (
            f"[SAFETY DEGRADED] bot_stage2_python.{fn_name} threw: "
            f"{type(exc).__name__}: {exc}. "
            f"Fallback active — manual review required. {ts}"
        )
        try:
            from notifications import send_whatsapp_direct  # noqa: PLC0415
            send_whatsapp_direct(msg)
        except Exception:
            pass
    except Exception:
        pass


def _load_signal_weights() -> tuple[dict, dict]:
    """Load signal_weights + signal_source_weights from strategy_config.json.
    Cached for the process lifetime. Falls back to defaults on any error.
    """
    global _SIGNAL_WEIGHTS_CACHE, _SIGNAL_SOURCE_WEIGHTS_CACHE
    if _SIGNAL_WEIGHTS_CACHE is not None and _SIGNAL_SOURCE_WEIGHTS_CACHE is not None:
        return _SIGNAL_WEIGHTS_CACHE, _SIGNAL_SOURCE_WEIGHTS_CACHE
    try:
        cfg = json.loads(_STRATEGY_CONFIG_PATH.read_text())
        _SIGNAL_WEIGHTS_CACHE = {**_SIGNAL_WEIGHTS_DEFAULT, **cfg.get("signal_weights", {})}
        _SIGNAL_SOURCE_WEIGHTS_CACHE = {**_SIGNAL_SOURCE_WEIGHTS_DEFAULT, **cfg.get("signal_source_weights", {})}
    except Exception as exc:
        log.debug("[L2] strategy_config.json read failed, using defaults: %s", exc)
        _SIGNAL_WEIGHTS_CACHE = dict(_SIGNAL_WEIGHTS_DEFAULT)
        _SIGNAL_SOURCE_WEIGHTS_CACHE = dict(_SIGNAL_SOURCE_WEIGHTS_DEFAULT)
    return _SIGNAL_WEIGHTS_CACHE, _SIGNAL_SOURCE_WEIGHTS_CACHE


# ── Cycle-scoped caches (per batch scoring call) ─────────────────────────────
# These are populated by _prepare_cycle_cache() and consumed by
# score_symbol_python(). Safe to re-populate each score_all_symbols_python
# invocation; the point is to do the disk reads ONCE per cycle rather than
# once per symbol.

_CYCLE_CACHE: dict[str, Any] = {
    "morning_brief": None,
    "orb_by_sym":    None,
    "earnings_map":  None,
    "pattern_wl":    None,
    "insider_evt":   None,
}


def _prepare_cycle_cache() -> None:
    """Populate module-level caches with one-shot disk reads for this cycle."""
    # Morning brief
    try:
        if _MORNING_BRIEF_PATH.exists():
            _CYCLE_CACHE["morning_brief"] = json.loads(_MORNING_BRIEF_PATH.read_text())
        else:
            _CYCLE_CACHE["morning_brief"] = {}
    except Exception as exc:
        log.debug("[L2] morning_brief read failed: %s", exc)
        _CYCLE_CACHE["morning_brief"] = {}

    # ORB candidates, indexed by symbol
    try:
        if _ORB_PATH.exists():
            orb_data = json.loads(_ORB_PATH.read_text())
            _CYCLE_CACHE["orb_by_sym"] = {
                c.get("symbol", ""): c for c in orb_data.get("candidates", [])
                if c.get("symbol")
            }
        else:
            _CYCLE_CACHE["orb_by_sym"] = {}
    except Exception as exc:
        log.debug("[L2] orb_candidates read failed: %s", exc)
        _CYCLE_CACHE["orb_by_sym"] = {}

    # Earnings calendar → {sym: days_away_int} map
    # load_calendar_map() returns {sym: entry_dict}; convert to int so score_symbol_python
    # can compare eda <= 2 directly (guards against dict-vs-int TypeError).
    try:
        from earnings_calendar_lookup import (  # noqa: PLC0415
            earnings_days_away,
            load_calendar_map,
        )
        raw_cal = load_calendar_map()
        _CYCLE_CACHE["earnings_map"] = {
            sym: earnings_days_away(sym, raw_cal) for sym in raw_cal
        }
    except Exception as exc:
        log.error("[L2] earnings_map load failed — signal scorer running without earnings context: %s", exc)
        _fire_safety_alert("earnings_map_load", exc)
        _CYCLE_CACHE["earnings_map"] = {}

    # Pattern watchlist
    try:
        from memory import _load_pattern_watchlist  # noqa: PLC0415
        _CYCLE_CACHE["pattern_wl"] = _load_pattern_watchlist()
    except Exception as exc:
        log.debug("[L2] pattern_watchlist read failed: %s", exc)
        _CYCLE_CACHE["pattern_wl"] = {}

    # Insider events: {sym: count_last_48h}
    insider_counts: dict[str, int] = {}
    try:
        from insider_intelligence import (  # noqa: PLC0415
            fetch_congressional_trades,
            fetch_form4_insider_trades,
        )
        try:
            import watchlist_manager as _wm  # noqa: PLC0415
            wl = _wm.get_active_watchlist()
            all_syms = [s["symbol"] for s in wl.get("all", []) if s.get("symbol")]
        except Exception:
            all_syms = []
        if all_syms:
            for evt in fetch_congressional_trades(all_syms, days_back=2) or []:
                sym = evt.get("ticker") or evt.get("symbol") or ""
                if sym:
                    insider_counts[sym] = insider_counts.get(sym, 0) + 1
            for evt in fetch_form4_insider_trades(all_syms, days_back=2) or []:
                sym = evt.get("ticker") or evt.get("symbol") or ""
                tc  = evt.get("transaction_code", "")
                if sym and tc == "P":  # open-market purchase only; skip A/M/S/F grants/exercises
                    insider_counts[sym] = insider_counts.get(sym, 0) + 1
    except Exception as exc:
        log.debug("[L2] insider events read failed: %s", exc)
    _CYCLE_CACHE["insider_evt"] = insider_counts


# ── Core scoring ─────────────────────────────────────────────────────────────

def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def score_symbol_python(sym: str, md: dict, regime: dict) -> dict:
    """Deterministic numerical score for a single symbol. Zero API calls.

    Returns a SignalScore-compatible dict with `layer="L2_python"` tag.
    Never raises — on any unexpected input, returns a neutral score with the
    error captured in `conflicts`.
    """
    try:
        ind = (md.get("ind_by_symbol") or {}).get(sym, {}) or {}
        id_sum = (md.get("intraday_summaries") or {}).get(sym, {}) or {}
        current_prices = md.get("current_prices", {}) or {}
        price = float(current_prices.get(sym) or ind.get("price") or 0) or None

        _bar_fetched_at = ind.get("bar_fetched_at")
        bar_age_minutes: int | None = None
        data_stale = False
        if _bar_fetched_at:
            try:
                _bft = datetime.fromisoformat(_bar_fetched_at)
                if _bft.tzinfo is None:
                    _bft = _bft.replace(tzinfo=timezone.utc)
                bar_age_minutes = int(
                    (datetime.now(timezone.utc) - _bft).total_seconds() / 60
                )
                data_stale = bar_age_minutes > 60
            except Exception:
                pass

        signals: list[str]   = []
        conflicts: list[str] = []
        score = 50.0  # neutral baseline

        # --- Daily trend block (±15 max) -------------------------------------
        ma20 = ind.get("ma20")
        ma50 = ind.get("ma50")
        if ma20 and ma50 and price:
            if price > ma20 > ma50:
                score += 10
                signals.append("uptrend_stack")
            elif price < ma20 < ma50:
                score -= 10
                signals.append("downtrend_stack")

        ema_cross = ind.get("ema9_cross", "none")
        if ema_cross == "golden":
            score += 5
            signals.append("ema_golden_cross")
        elif ema_cross == "death":
            score -= 5
            signals.append("ema_death_cross")

        # --- Momentum / oscillators (±15 max) --------------------------------
        rsi = ind.get("rsi")
        if rsi is not None:
            if rsi >= 70:
                score -= 5
                conflicts.append(f"rsi_overbought_{rsi:.0f}")
            elif rsi <= 30:
                score += 5
                signals.append(f"rsi_oversold_{rsi:.0f}")
            elif rsi > 55:
                score += 3
                signals.append("rsi_bullish")
            elif rsi < 45:
                score -= 3
                signals.append("rsi_bearish")
            # 45..55 → neutral, no move

        macd = ind.get("macd")
        msig = ind.get("macd_signal")
        if macd is not None and msig is not None:
            if macd > msig:
                score += 4
                signals.append("macd_bullish_cross")
            else:
                score -= 4
                signals.append("macd_bearish_cross")

        # --- Volume / flow (±10 max) -----------------------------------------
        vrat = ind.get("vol_ratio")
        if vrat is not None:
            if vrat >= 2.0:
                score += 7
                signals.append(f"vol_spike_{vrat:.1f}x")
            elif vrat <= 0.5:
                score -= 3
                conflicts.append("low_volume")

        # VWAP position (from intraday summary)
        vwap = id_sum.get("vwap") if id_sum else None
        if vwap and price:
            if price > vwap:
                score += 3
                signals.append("above_vwap")
            else:
                score -= 3
                signals.append("below_vwap")

        # --- 5-min intraday momentum (±10 max) -------------------------------
        m5 = id_sum.get("momentum_5bar") if id_sum else None
        if m5 is not None:
            if m5 >= 2.0:
                score += 8
                signals.append(f"5m_momentum_+{m5:.1f}%")
            elif m5 <= -2.0:
                score -= 8
                signals.append(f"5m_momentum_{m5:.1f}%")

        # --- ORB context (+10 max) -------------------------------------------
        orb = (_CYCLE_CACHE.get("orb_by_sym") or {}).get(sym)
        if orb:
            orb_score = float(orb.get("orb_score", 0) or 0)
            score += min(10.0, orb_score * 10.0)
            signals.append(f"orb_{orb.get('conviction','?')}")

        # --- Morning brief conviction (±8 / +4) ------------------------------
        # Only structural fields — symbol, conviction, direction — NOT catalyst prose.
        # This was the source of the XLE/oil hallucination.
        mb = _CYCLE_CACHE.get("morning_brief") or {}
        mb_direction: Optional[str] = None
        for pick in (mb.get("conviction_picks", []) or []):
            if not isinstance(pick, dict):
                continue
            if (pick.get("symbol") or "").upper() == sym.upper():
                conv = str(pick.get("conviction") or "").lower()
                direction = str(pick.get("direction") or "").lower()
                mb_direction = direction if direction in ("long", "short") else None
                if conv == "high":
                    score += 8 if direction != "short" else -8
                    signals.append("morning_brief_high")
                elif conv == "medium":
                    score += 4 if direction != "short" else -4
                    signals.append("morning_brief_medium")
                break

        # --- Earnings proximity (context only — no penalty) ------------------
        earnings_map = _CYCLE_CACHE.get("earnings_map") or {}
        eda = earnings_map.get(sym.upper())

        # --- Insider / congressional activity --------------------------------
        insider_evt = _CYCLE_CACHE.get("insider_evt") or {}
        if insider_evt.get(sym, 0) >= 1:
            score += 5
            signals.append("insider_purchase_48h")

        # --- Pattern watchlist caution flag ----------------------------------
        pwl = _CYCLE_CACHE.get("pattern_wl") or {}
        pwl_entry = pwl.get(sym) if isinstance(pwl, dict) else None
        pwl_flag: Any = False
        if isinstance(pwl_entry, dict) and not pwl_entry.get("graduated"):
            pwl_flag = (
                f"min_{pwl_entry.get('minimum_signals_required', 2)}_signals_required"
            )
            conflicts.append("pattern_watchlist_caution")

        # --- Regime bias dampener --------------------------------------------
        bias = str((regime or {}).get("bias", "neutral")).lower()
        if bias == "risk_off" and score > 50:
            score = 50.0 + (score - 50.0) * 0.5
        elif bias == "risk_on" and score < 50:
            score = 50.0 - (50.0 - score) * 0.5

        score_final = round(_clamp(score, 0.0, 100.0), 1)

        if score_final >= 60:
            direction_out = "bullish"
        elif score_final <= 40:
            direction_out = "bearish"
        else:
            direction_out = "neutral"

        # Override direction if morning brief gave an explicit call
        if mb_direction == "long":
            direction_out = "bullish" if score_final >= 50 else direction_out
        elif mb_direction == "short":
            direction_out = "bearish" if score_final <= 50 else direction_out

        if score_final >= 75:
            conviction_out = "high"
        elif score_final >= 60:
            conviction_out = "medium"
        elif score_final >= 40:
            conviction_out = "low"
        else:
            conviction_out = "avoid"

        return {
            "score":      score_final,
            "direction":  direction_out,
            "conviction": conviction_out,
            "signals":    signals[:8],
            "conflicts":  conflicts[:4],
            "layer":      "L2_python",
            # Metadata useful for L3 synthesis
            "orb_candidate":    bool(orb),
            "pattern_watchlist": pwl_flag,
            "earnings_days_away": eda,
            "price":            price,
            "data_stale":       data_stale,
            "bar_age_minutes":  bar_age_minutes,
        }
    except Exception as exc:
        log.debug("[L2] score_symbol_python(%s) failed (non-fatal): %s", sym, exc)
        return {
            "score":      50.0,
            "direction":  "neutral",
            "conviction": "low",
            "signals":    [],
            "conflicts":  [f"l2_error:{type(exc).__name__}"],
            "layer":      "L2_python",
            "orb_candidate":    False,
            "pattern_watchlist": False,
            "earnings_days_away": None,
            "price":            None,
        }


def score_all_symbols_python(
    symbols: list[str],
    md: dict,
    regime: dict,
) -> dict[str, dict]:
    """Batch entry point — scores every symbol with zero API calls.

    Populates the cycle cache once, then scores all symbols. Returns
    {sym: L2_result_dict}. Safe to call with an empty list (returns {}).
    """
    # Touch signal weights so the config is loaded even if no symbols; this
    # also lets future scoring logic thread weights into the math without
    # re-reading the config on every symbol.
    _load_signal_weights()
    _prepare_cycle_cache()

    out: dict[str, dict] = {}
    for sym in symbols:
        if not sym:
            continue
        out[sym] = score_symbol_python(sym, md, regime)
    return out


# ── Standalone smoke test ─────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # Fabricate a minimal md dict to exercise the scorer end-to-end without
    # needing Alpaca or network access.
    fake_md = {
        "ind_by_symbol": {
            "NVDA": {
                "price": 142.50, "prev": 140.00,
                "ma20": 138.0, "ma50": 130.0,
                "ema9": 141.2, "ema21": 138.5, "ema9_cross": "golden",
                "price_above_ema9": True,
                "rsi": 58.0, "macd": 1.2, "macd_signal": 0.8,
                "vol_ratio": 1.4,
            },
            "XYZ": {  # bearish sample
                "price": 10.0, "prev": 11.0,
                "ma20": 12.0, "ma50": 13.0,
                "ema9": 10.5, "ema21": 11.5, "ema9_cross": "death",
                "price_above_ema9": False,
                "rsi": 28.0, "macd": -0.3, "macd_signal": -0.1,
                "vol_ratio": 0.4,
            },
        },
        "intraday_summaries": {
            "NVDA": {
                "rsi": 61.0, "macd": 0.22, "macd_signal": 0.15,
                "momentum_5bar": 1.8, "vol_ratio": 1.6, "vwap": 141.1,
                "bar_count": 30,
            },
            "XYZ": {
                "rsi": 35.0, "macd": -0.05, "macd_signal": 0.01,
                "momentum_5bar": -2.5, "vol_ratio": 0.9, "vwap": 10.5,
                "bar_count": 30,
            },
        },
        "current_prices": {"NVDA": 142.50, "XYZ": 10.0},
    }
    regime = {"bias": "neutral", "regime_score": 50}
    results = score_all_symbols_python(["NVDA", "XYZ"], fake_md, regime)
    print(json.dumps(results, indent=2, default=str))
