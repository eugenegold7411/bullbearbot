"""
portfolio_allocator.py — Portfolio allocator shadow engine (S6-ALLOCATOR).

SHADOW MODE ONLY — does not submit orders, does not trigger execute_all().
Reads pi_data + held positions + scored candidates, produces recommended
actions (HOLD/TRIM/ADD/REPLACE), and writes one artifact per cycle to:
  data/analytics/portfolio_allocator_shadow.jsonl

Feature flags (strategy_config.json portfolio_allocator section):
  enable_shadow  — controls whether shadow runs (default True)
  enable_live    — ALWAYS False this sprint; wired but disabled

Decision rules (explicit, legible):
  HOLD    — default for all incumbents
  TRIM    — thesis_score <= 5 (normalized <=50) AND notional > min_rebalance_notional
  ADD     — thesis_score_normalized >= add_min_score (default 65) AND room below tier ceiling
            AND available_for_new > min_rebalance_notional
  REPLACE — candidate.signal_score − weakest_incumbent.thesis_score_normalized
            >= replace_score_gap (default 15) AND all friction checks pass

Anti-churn friction (all must pass for REPLACE to fire):
  1. Score gap >= replace_score_gap (default 15, on 0–100 normalized scale)
  2. Candidate not in same sector as weakest incumbent (sector correlation proxy)
  3. Weakest incumbent has no time-bound exit within same_day_replace_block_hours
  4. Recommendation notional >= min_rebalance_notional (default $500)
  5. No same-symbol recommendation recorded today (module-level cooldown)
  6. enable_shadow must be True

REALLOCATE semantics from risk_kernel.py are advisory only in this sprint.
execute_reallocate() from portfolio_intelligence.py is NOT called here.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from cost_attribution import _rotate_jsonl as _rotate_artifact_jsonl

log = logging.getLogger(__name__)

_ROOT                = Path(__file__).parent
_ARTIFACT_PATH       = _ROOT / "data" / "analytics" / "portfolio_allocator_shadow.jsonl"
_SIGNAL_SCORES_PATH  = _ROOT / "data" / "market" / "signal_scores.json"
_REGISTRY_JSON_PATH  = _ROOT / "data" / "reports" / "shadow_status_latest.json"
_WL_CORE_PATH        = _ROOT / "watchlist_core.json"
_WL_DYNAMIC_PATH     = _ROOT / "watchlist_dynamic.json"
_WL_INTRA_PATH       = _ROOT / "watchlist_intraday.json"
_COOLDOWN_PATH       = _ROOT / "data" / "runtime" / "allocator_cooldown.json"

SCHEMA_VERSION = 1

# Re-entrancy guard — prevents concurrent allocator cycles from racing.
_ALLOC_LOCK = threading.Lock()


def _build_watchlist_tier_map() -> dict[str, str]:
    """Build {symbol: tier_name} from watchlist files at import time. Non-fatal."""
    out: dict[str, str] = {}
    try:
        for path, tier in [
            (_WL_CORE_PATH, "core"),
            (_WL_DYNAMIC_PATH, "dynamic"),
            (_WL_INTRA_PATH, "intraday"),
        ]:
            if not path.exists():
                continue
            for entry in json.loads(path.read_text()).get("symbols", []):
                sym = (entry.get("symbol") or "").upper()
                if sym and sym not in out:
                    out[sym] = tier
    except Exception:
        pass
    return out


_SYMBOL_TIER_MAP: dict[str, str] = _build_watchlist_tier_map()


def _tier_max_for_symbol(symbol: str, mv: float, sizes: dict) -> float:
    """Return tier max weight fraction for a held symbol.

    Watchlist lookup is authoritative (reflects how the kernel would size a new
    entry in the same symbol). Size-inference from _target_weights is the fallback
    when the symbol is not on any watchlist (e.g. a short-lived dynamic addition
    that has since been pruned).
    """
    tier = _SYMBOL_TIER_MAP.get((symbol or "").upper())
    if tier == "core":
        return 0.20
    if tier == "dynamic":
        return 0.15
    if tier == "intraday":
        return 0.05
    # Size-inference fallback
    core_max = float(sizes.get("core", 0) or 0)
    dyn_max  = float(sizes.get("standard", 0) or 0)
    if core_max > 0 and mv >= core_max * 0.50:
        return 0.20
    if dyn_max > 0 and mv >= dyn_max * 0.50:
        return 0.15
    return 0.05


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

_PA_DEFAULTS: dict = {
    "enable_shadow":                   True,
    "enable_live":                     False,   # ALWAYS False this sprint
    "replace_score_gap":               15.0,
    "trim_score_drop":                 10.0,    # normalized threshold for TRIM (score ≤ 40)
    "trim_score_threshold":            5,       # S7-F/S8-D: raw thesis_score ceiling for TRIM (1–10 scale); aligned with system_v1.txt "4–5/10: TRIM"
    "weight_deadband":                 0.02,    # 2% — min weight gap to trigger action
    "min_rebalance_notional":          500.0,   # $500 minimum to recommend
    "max_recommendations_per_cycle":   5,
    "same_symbol_daily_cooldown_enabled": True,
    "add_cooldown_hours":              1.0,    # rolling-window block for ADD/REPLACE/TRIM (hours since last cooldown entry)
    "same_day_replace_block_hours":    6.0,
    "size_trim_enabled":               True,    # S8: size-based TRIM gate (fires for score ≥ 6 positions over tier max)
    "size_trim_tolerance_pct":         2.0,     # S8: pp over tier max before size TRIM fires
    "enable_live_add":                 False,   # Stage 2: ADD actions execute via risk_kernel BUY path (off by default)
    "enable_live_replace":             False,   # Stage 2: REPLACE actions execute two-phase close+buy (off by default)
    "enable_live_trim":                False,   # explicit trim gate — must be True alongside enable_live for trim to execute
    "replace_cancel_brackets":         True,    # before Phase A close, cancel OCO/bracket sells holding the qty (Alpaca 40310000)
    "replace_cancel_poll_seconds":     3.0,     # max time to wait for cancellations to clear before issuing close
    "max_position_pct_equity":         0.15,    # equity-based single-name cap for SIZE TRIM trigger (position/equity > this fires trim)
    "add_min_score":                   65.0,    # normalized (0-100) thesis_score threshold for ADD gate (65 = 6.5/10 raw)
}


def _get_pa_config(cfg: dict) -> dict:
    """Return portfolio_allocator config merged with defaults. Non-fatal."""
    pa = cfg.get("portfolio_allocator", {})
    merged = dict(_PA_DEFAULTS)
    for k, v in pa.items():
        if k in merged:
            merged[k] = v
    return merged


def _trim_pct_for_score(score: int, pa_cfg: dict) -> float:
    """Return trim fraction for thesis_score using trim_severity config table.

    Iterates tiers in order; uses first tier where score <= score_max.
    Falls back to flat 25% if trim_severity key is absent from pa_cfg.
    """
    severity = pa_cfg.get("trim_severity")
    if severity:
        for tier in severity:
            if score <= int(tier["score_max"]):
                return float(tier["trim_pct"])
    return 0.25  # default fallback — flat 25%


# ─────────────────────────────────────────────────────────────────────────────
# Sector lookup (used for correlation proxy)
# ─────────────────────────────────────────────────────────────────────────────

def _symbol_sector(symbol: str) -> str:
    """Look up sector for a symbol from portfolio_intelligence symbol map."""
    try:
        from portfolio_intelligence import _SYMBOL_SECTOR  # noqa: PLC0415
        return _SYMBOL_SECTOR.get(symbol, "")
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Candidate loading from last signal-scores cycle
# ─────────────────────────────────────────────────────────────────────────────

def _load_candidates(held_symbols: set[str]) -> list[dict]:
    """
    Load top candidates from the most recent signal_scores.json.
    Returns a list of dicts sorted by signal_score descending.
    Excludes symbols already held. Non-fatal — returns [] on any error.
    """
    try:
        data = json.loads(_SIGNAL_SCORES_PATH.read_text())
        scored = data.get("scored_symbols", {})
        if not scored:
            return []

        candidates = []
        for sym, info in scored.items():
            if not isinstance(info, dict):
                continue
            if sym in held_symbols:
                continue
            score = float(info.get("score", 0) or 0)
            if score <= 0:
                continue
            candidates.append({
                "symbol":       sym,
                "signal_score": score,
                "direction":    info.get("direction", "neutral"),
                "catalyst":     (info.get("primary_catalyst") or info.get("catalyst") or "")[:120],
                "signals":      [str(s) for s in (info.get("signals") or [])[:3]],
                "price":        float(info.get("price", 0) or 0),
            })
        candidates.sort(key=lambda c: c["signal_score"], reverse=True)
        return candidates[:20]   # top 20 is more than enough
    except Exception as exc:
        log.debug("[ALLOC] candidate load failed (non-fatal): %s", exc)
        return []


def _enrich_incumbents_with_signal_data(incumbents: list[dict]) -> None:
    """
    Attach signal catalyst + signal list from signal_scores.json to each incumbent.
    Mutates incumbents in-place. Non-fatal — missing/stale data silently skipped.
    Incumbents are held positions excluded from _load_candidates, so they need
    their own enrichment pass to surface fresh signal context in ADD reasons.
    """
    try:
        data   = json.loads(_SIGNAL_SCORES_PATH.read_text())
        scored = data.get("scored_symbols", {})
        for inc in incumbents:
            sym  = inc["symbol"]
            info = scored.get(sym)
            if not isinstance(info, dict):
                continue
            inc["signal_catalyst"] = (
                info.get("primary_catalyst") or info.get("catalyst") or ""
            )[:120]
            inc["signal_signals"] = [str(s) for s in (info.get("signals") or [])[:3]]
    except Exception as exc:
        log.debug("[ALLOC] incumbent signal enrich failed (non-fatal): %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Incumbent ranking
# ─────────────────────────────────────────────────────────────────────────────

def _rank_incumbents(
    pi_data: dict,
    positions: list,
    equity: float = 0.0,
    total_capacity: float = 0.0,
) -> list[dict]:
    """
    Build ranked incumbent list from pi_data thesis_scores + positions.
    Returns list sorted by thesis_score ascending (weakest first).

    total_capacity — total_market_value + buying_power (matches risk_kernel denominator).
    equity — fallback when total_capacity is not supplied.
    """
    thesis_scores = pi_data.get("thesis_scores", [])
    health_map    = pi_data.get("health_map", {})
    ts_by_symbol  = {ts["symbol"]: ts for ts in thesis_scores}

    # Build position market-value lookup
    mv_by_symbol: dict[str, float] = {}
    for pos in positions:
        try:
            if float(pos.qty) > 0:
                mv_by_symbol[pos.symbol] = float(pos.market_value)
        except Exception:
            pass

    denom = total_capacity if total_capacity > 0 else (equity if equity > 0 else sum(mv_by_symbol.values()) or 1.0)

    incumbents = []
    for sym, mv in mv_by_symbol.items():
        ts       = ts_by_symbol.get(sym, {})
        health   = health_map.get(sym, {})
        score    = int(ts.get("thesis_score", 5))
        account_pct = round(mv / denom * 100, 2) if denom > 0 else 0.0

        incumbents.append({
            "symbol":                   sym,
            "market_value":             round(mv, 2),
            "account_pct":              account_pct,
            "thesis_score":             score,
            "thesis_score_normalized":  score * 10,   # 0–100 scale
            "health":                   health.get("health", ts.get("health", "MONITORING")),
            "recommended_pi_action":    ts.get("recommended_action", "hold"),
            "override_flag":            ts.get("override_flag"),
            "weakest_factor":           ts.get("weakest_factor", ""),
        })

    incumbents.sort(key=lambda x: x["thesis_score"])
    return incumbents


# ─────────────────────────────────────────────────────────────────────────────
# Anti-churn friction checks
# ─────────────────────────────────────────────────────────────────────────────

def _load_cooldown() -> dict:
    """
    Load cooldown state from disk. Returns empty dict if file missing.
    Caller filters stale entries via timestamp delta; persistence retains
    history across calendar-day flips so 23:00 UTC ADDs still block at 00:30 UTC.
    Non-fatal — returns empty dict on any error.
    """
    try:
        if not _COOLDOWN_PATH.exists():
            return {}
        data = json.loads(_COOLDOWN_PATH.read_text())
        return data.get("cooldowns", {})
    except Exception as exc:
        log.debug("[ALLOC] _load_cooldown failed (non-fatal): %s", exc)
        return {}


def _save_cooldown(cooldown: dict) -> None:
    """
    Save cooldown state to disk.
    Writes: {"date": today_utc, "cooldowns": {symbol: {action, timestamp}}}
    The "date" field is informational only (auditable); cooldown semantics
    are now timestamp-based (see _is_on_cooldown).
    Non-fatal — logs warning on failure, does not raise.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        _COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
        _COOLDOWN_PATH.write_text(
            json.dumps({"date": today, "cooldowns": cooldown}, indent=2)
        )
    except Exception as exc:
        log.warning("[ALLOC] _save_cooldown failed (non-fatal): %s", exc)


def _is_on_cooldown(symbol: str, action: str, cooldown: dict, hours: float) -> bool:
    """Hours-based cooldown check.

    Returns True if (symbol, action) has an entry whose timestamp is within
    `hours` of now. Out-of-window or mismatched-action entries do not block.
    """
    entry = cooldown.get(symbol)
    if entry is None:
        return False
    if entry.get("action") != action:
        return False
    ts_raw = entry.get("timestamp")
    if not ts_raw:
        return False
    try:
        entry_ts = datetime.fromisoformat(ts_raw)
    except ValueError:
        return False
    if entry_ts.tzinfo is None:
        entry_ts = entry_ts.replace(tzinfo=timezone.utc)
    age_h = (datetime.now(timezone.utc) - entry_ts).total_seconds() / 3600.0
    return age_h < float(hours)


def _add_to_cooldown(symbol: str, action: str, cooldown: dict) -> dict:
    """
    Returns updated cooldown dict with symbol+action added.
    Does NOT save to disk — caller must call _save_cooldown().
    """
    updated = dict(cooldown)
    updated[symbol] = {
        "action":    action,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return updated


def _check_cooldown(symbol: str, pa_cfg: dict, action: str = "REPLACE") -> tuple[bool, str]:
    """Returns (passes, reason). passes=True means no cooldown block.

    Cooldown is rolling-window in hours, not calendar-day. Window is
    `add_cooldown_hours` (default 1) — the same window applies to ADD,
    REPLACE, and TRIM unless future config overrides it per action.
    """
    if not pa_cfg["same_symbol_daily_cooldown_enabled"]:
        return True, ""
    hours = float(pa_cfg.get("add_cooldown_hours", 1.0))
    cooldown = _load_cooldown()
    if _is_on_cooldown(symbol, action, cooldown, hours):
        return False, (
            f"{symbol} {action} cooldown active "
            f"(<{hours:g}h since last {action} recommendation)"
        )
    return True, ""


def _check_time_bound(symbol: str, cfg: dict, pa_cfg: dict) -> tuple[bool, str]:
    """
    Returns (passes, reason). passes=True means no imminent forced exit.
    Blocks REPLACE if incumbent has a time-bound exit within same_day_replace_block_hours.
    """
    block_hours = float(pa_cfg["same_day_replace_block_hours"])
    tba         = cfg.get("time_bound_actions", [])
    now         = datetime.now(timezone.utc)

    for item in tba:
        if item.get("symbol") != symbol:
            continue
        dl_str = item.get("exit_by") or item.get("deadline_utc") or ""
        if not dl_str:
            continue
        try:
            dl_dt = datetime.fromisoformat(dl_str.replace("Z", "+00:00"))
            hours_until = (dl_dt - now).total_seconds() / 3600
            if 0 <= hours_until <= block_hours:
                return False, (
                    f"{symbol} has imminent time-bound exit in {hours_until:.1f}h "
                    f"(block={block_hours}h)"
                )
            if hours_until < 0:
                return False, f"{symbol} time-bound exit already past deadline"
        except Exception:
            pass
    return True, ""


def _check_correlation(
    candidate_symbol: str,
    incumbent_symbol: str,
    pi_data: dict,
) -> tuple[bool, str]:
    """
    Returns (passes, reason). passes=True means correlation check allows REPLACE.
    Uses existing correlation matrix from pi_data if available,
    otherwise falls back to sector-based inference.
    """
    # Direct matrix lookup (candidate unlikely to be in matrix since not held)
    matrix = pi_data.get("correlation", {}).get("matrix", {})
    for s1, s2 in [(candidate_symbol, incumbent_symbol), (incumbent_symbol, candidate_symbol)]:
        corr = matrix.get(s1, {}).get(s2)
        if corr is not None and abs(float(corr)) > 0.70:
            return False, (
                f"correlation={float(corr):.2f} between {candidate_symbol} and "
                f"{incumbent_symbol} — same macro bet"
            )

    # Sector inference fallback
    cand_sector = _symbol_sector(candidate_symbol)
    inc_sector  = _symbol_sector(incumbent_symbol)
    if cand_sector and inc_sector and cand_sector == inc_sector:
        return False, (
            f"same sector ({cand_sector}): {candidate_symbol} vs {incumbent_symbol} "
            f"— likely high correlation"
        )

    return True, ""


# ─────────────────────────────────────────────────────────────────────────────
# Target weight computation
# ─────────────────────────────────────────────────────────────────────────────

def _target_weights(incumbents: list[dict], sizes: dict) -> dict[str, float]:
    """
    Compute simple target weight fractions for each incumbent.
    Uses tier-ceiling logic: infer tier from position size vs pi sizing caps.
    Returns dict: symbol → target_weight_pct (0.0–1.0).
    """
    core_max = float(sizes.get("core", 0) or 0)
    dyn_max  = float(sizes.get("standard", 0) or 0)

    weights = {}
    for inc in incumbents:
        mv = inc["market_value"]
        if core_max > 0 and mv >= core_max * 0.50:
            tier_max = 0.15
        elif dyn_max > 0 and mv >= dyn_max * 0.50:
            tier_max = 0.08
        else:
            tier_max = 0.05
        weights[inc["symbol"]] = tier_max
    return weights


# ─────────────────────────────────────────────────────────────────────────────
# Core decision logic
# ─────────────────────────────────────────────────────────────────────────────

def _decide_actions(
    incumbents:   list[dict],
    candidates:   list[dict],
    pi_data:      dict,
    cfg:          dict,
    pa_cfg:       dict,
    sizes:        dict,
    equity:       float,
) -> tuple[list[dict], list[dict]]:
    """
    Compute proposed_actions and suppressed_actions.

    Returns: (proposed_actions, suppressed_actions)
    Each proposed action: {action, symbol, reason, score_gap, target_weight_pct, exit_symbol}
    Each suppressed action: {proposed_action, symbol, suppression_reason}
    """
    replace_score_gap   = float(pa_cfg["replace_score_gap"])
    min_notional        = float(pa_cfg["min_rebalance_notional"])
    weight_deadband     = float(pa_cfg["weight_deadband"])
    max_recs            = int(pa_cfg["max_recommendations_per_cycle"])
    trim_thresh         = int(pa_cfg["trim_score_threshold"])   # S7-F: config-driven
    size_trim_enabled   = bool(pa_cfg.get("size_trim_enabled", True))
    size_trim_tol       = float(pa_cfg.get("size_trim_tolerance_pct", 2.0)) / 100.0
    available_for_new    = float(sizes.get("available_for_new", 0) or 0)
    total_capacity       = equity * 4   # 4× leverage basis (2× margin × 2 sides)
    # Single-name cap from risk_kernel — ADD and SIZE TRIM must respect this ceiling.
    max_pos_pct_capacity = float(cfg.get("parameters", {}).get("max_position_pct_capacity", 0.15))
    max_pos_cap_dollars  = max_pos_pct_capacity * total_capacity if total_capacity > 0 else float("inf")
    # Equity-based single-name cap for SIZE TRIM trigger (equity × pct, not capacity × pct)
    max_pos_pct_equity   = float(pa_cfg.get("max_position_pct_equity", 0.15))
    add_min_score        = float(pa_cfg.get("add_min_score", 65))

    target_wts = _target_weights(incumbents, sizes)
    proposed:   list[dict] = []
    suppressed: list[dict] = []

    # ── Per-incumbent HOLD / TRIM / ADD decisions ─────────────────────────────
    for inc in incumbents:
        sym      = inc["symbol"]
        score    = inc["thesis_score"]
        norm     = inc["thesis_score_normalized"]
        mv       = inc["market_value"]
        acct_pct = inc["account_pct"] / 100.0   # fraction of total_capacity
        tier_max = _tier_max_for_symbol(sym, mv, sizes)   # S8: watchlist-first, size fallback

        # TRIM: thesis weak AND position is large enough to trim meaningfully
        # threshold: thesis_score <= trim_thresh (normalized <= 40 at default 4),
        # representing "exit_consider" or "reduce" territory in portfolio_intelligence
        if score <= trim_thresh and mv > min_notional:
            # S7-I: graduated trim — severity scales with how weak the thesis is
            _frac = _trim_pct_for_score(score, pa_cfg)
            trim_notional = round(mv * _frac, 2)
            if trim_notional >= min_notional:
                proposed.append({
                    "action":           "TRIM",
                    "symbol":           sym,
                    "reason":           (
                        f"thesis_score={score}/10 (normalized={norm}) — weak thesis; "
                        f"consider trimming ~{_frac:.0%} (${trim_notional:,.0f})"
                    ),
                    "score_gap":        None,
                    "target_weight_pct": tier_max,
                    "exit_symbol":      None,
                })
                continue   # don't double-count as HOLD

        # SIZE TRIM: strong thesis but position exceeds equity-based cap or tier max.
        # Equity-based check (mv/equity > max_pos_pct_equity) fires for real oversize
        # positions regardless of leverage ratio. Capacity-based check (mv/total_capacity
        # > tier_max) is retained as a secondary trigger.
        # Fires independently of thesis-score TRIM (exclusive paths: score ≤ trim_thresh
        # goes to thesis TRIM above; score > trim_thresh falls through to here).
        if size_trim_enabled and score >= 6 and total_capacity > 0 and equity > 0 and mv > min_notional:
            cap_frac    = mv / total_capacity
            equity_frac = mv / equity
            if equity_frac > max_pos_pct_equity + size_trim_tol or cap_frac > tier_max + size_trim_tol:
                target_mv     = min(
                    max_pos_pct_equity * equity,         # equity-based target
                    tier_max * total_capacity,           # tier-based target
                    max_pos_cap_dollars,                 # kernel single-name cap
                )
                trim_notional = round(mv - target_mv, 2)
                if trim_notional >= min_notional:
                    proposed.append({
                        "action":           "TRIM",
                        "symbol":           sym,
                        "reason":           (
                            f"SIZE TRIM — {sym} at {equity_frac*100:.1f}% of equity exceeds "
                            f"{max_pos_pct_equity*100:.0f}% equity cap "
                            f"(tol={size_trim_tol*100:.1f}%) — "
                            f"trim ~${trim_notional:,.0f} to target ${target_mv:,.0f}"
                        ),
                        "score_gap":        None,
                        "target_weight_pct": tier_max,
                        "exit_symbol":      None,
                    })
                    continue

        # ADD: thesis strong AND room to grow below tier ceiling AND below kernel cap.
        # threshold: thesis_score_normalized >= add_min_score (config-driven, default 65)
        if (norm >= add_min_score
                and available_for_new > min_notional
                and acct_pct < tier_max - weight_deadband
                and mv < max_pos_cap_dollars):
            ok_add_cool, reason_add_cool = _check_cooldown(sym, pa_cfg, action="ADD")
            if not ok_add_cool:
                suppressed.append({
                    "proposed_action":   "ADD",
                    "symbol":            sym,
                    "suppression_reason": reason_add_cool,
                })
            else:
                _cat = (inc.get("signal_catalyst") or "")[:80]
                proposed.append({
                    "action":           "ADD",
                    "symbol":           sym,
                    "reason":           (
                        f"thesis_score={score}/10 (conviction={norm/100:.2f}) — strong; room to add "
                        f"(acct_pct={acct_pct:.1%} < tier_max={tier_max:.0%}), "
                        f"available_for_new=${available_for_new:,.0f}"
                        + (f"; catalyst: {_cat}" if _cat else "")
                    ),
                    "catalyst":         inc.get("signal_catalyst", ""),
                    "signals":          inc.get("signal_signals", []),
                    "score_gap":        None,
                    "target_weight_pct": tier_max,
                    "exit_symbol":      None,
                })
            continue

        # HOLD default
        proposed.append({
            "action":           "HOLD",
            "symbol":           sym,
            "reason":           f"thesis_score={score}/10 — holding",
            "score_gap":        None,
            "target_weight_pct": tier_max,
            "exit_symbol":      None,
        })

    # ── REPLACE: weakest incumbent ↔ strongest candidate ─────────────────────
    if incumbents and candidates:
        weakest   = incumbents[0]   # sorted ascending by thesis_score
        strongest = candidates[0]   # sorted descending by signal_score

        weak_sym  = weakest["symbol"]
        cand_sym  = strongest["symbol"]
        weak_norm = weakest["thesis_score_normalized"]
        cand_scr  = float(strongest["signal_score"])
        gap       = cand_scr - weak_norm

        # Notional check: weakest position large enough to exit meaningfully
        notional_ok = weakest["market_value"] >= min_notional

        if gap >= replace_score_gap and notional_ok:
            # Apply friction checks
            ok_corr, reason_corr = _check_correlation(cand_sym, weak_sym, pi_data)
            ok_tba,  reason_tba  = _check_time_bound(weak_sym, cfg, pa_cfg)
            ok_cool, reason_cool = _check_cooldown(weak_sym, pa_cfg)

            if ok_corr and ok_tba and ok_cool:
                _ccat = (strongest.get("catalyst") or "")[:80]
                proposed.append({
                    "action":           "REPLACE",
                    "symbol":           cand_sym,
                    "reason":           (
                        f"candidate signal_score={cand_scr:.0f} vs weakest "
                        f"{weak_sym} normalized={weak_norm} — gap={gap:.0f} "
                        f">= threshold={replace_score_gap:.0f}"
                        + (f"; catalyst: {_ccat}" if _ccat else "")
                    ),
                    "catalyst":         strongest.get("catalyst", ""),
                    "signals":          strongest.get("signals", []),
                    "score_gap":        round(gap, 1),
                    "target_weight_pct": target_wts.get(weak_sym, 0.08),
                    "exit_symbol":      weak_sym,
                })
            else:
                # Record all suppression reasons
                for ok, reason in [
                    (ok_corr, reason_corr),
                    (ok_tba,  reason_tba),
                    (ok_cool, reason_cool),
                ]:
                    if not ok:
                        suppressed.append({
                            "proposed_action":   "REPLACE",
                            "symbol":            cand_sym,
                            "suppression_reason": reason,
                        })
        elif gap < replace_score_gap:
            suppressed.append({
                "proposed_action":   "REPLACE",
                "symbol":            cand_sym,
                "suppression_reason": (
                    f"score gap {gap:.0f} < threshold {replace_score_gap:.0f} "
                    f"(candidate={cand_scr:.0f}, incumbent={weak_sym} normalized={weak_norm})"
                ),
            })
        elif not notional_ok:
            suppressed.append({
                "proposed_action":   "REPLACE",
                "symbol":            cand_sym,
                "suppression_reason": (
                    f"incumbent {weak_sym} market_value=${weakest['market_value']:,.0f} "
                    f"< min_rebalance_notional=${min_notional:,.0f}"
                ),
            })

    # ── Cap total recommendations ─────────────────────────────────────────────
    non_hold = [p for p in proposed if p["action"] != "HOLD"]
    holds    = [p for p in proposed if p["action"] == "HOLD"]
    if len(non_hold) > max_recs:
        excess   = non_hold[max_recs:]
        non_hold = non_hold[:max_recs]
        for ex in excess:
            suppressed.append({
                "proposed_action":    ex["action"],
                "symbol":             ex["symbol"],
                "suppression_reason": f"max_recommendations_per_cycle={max_recs} reached",
            })
    proposed = non_hold + holds

    # ── Record cooldown for non-HOLD recommendations (disk-backed) ──────────
    non_hold_actions = [p for p in proposed if p["action"] != "HOLD"]
    if non_hold_actions:
        cooldown = _load_cooldown()
        for p in non_hold_actions:
            cooldown = _add_to_cooldown(p["symbol"], p["action"], cooldown)
        _save_cooldown(cooldown)

    return proposed, suppressed


# ─────────────────────────────────────────────────────────────────────────────
# Live TRIM execution
# ─────────────────────────────────────────────────────────────────────────────

def _execute_live_trim(
    symbol:             str,
    positions:          list,
    snapshot,
    cfg:                dict,
    session_tier:       str,
    reason:             str,
    vix:                Optional[float] = None,
    account=None,
    market_status:      str = "open",
    minutes_since_open: int = 0,
    equity:             float = 0.0,
    max_pos_pct_equity: float = 0.15,
) -> str:
    """
    Execute a single SIZE TRIM directly — bypasses risk_kernel.
    Uses equity-based target matching the SIZE TRIM trigger:
        target_mv = max_pos_pct_equity × equity
    Returns "ok:{qty}" on success, rejection/error reason string otherwise.
    Non-fatal — caller logs and continues regardless of return value.
    """
    try:
        from order_executor import execute_all  # noqa: PLC0415
        from schemas import (  # noqa: PLC0415
            AccountAction,
            BrokerAction,
            Conviction,
            Tier,
        )
    except ImportError as exc:
        return f"import error: {exc}"

    if account is None:
        return f"account not provided for live TRIM of {symbol}"
    if equity <= 0:
        return f"equity not provided for live TRIM of {symbol}"

    price: Optional[float] = None
    current_qty: int = 0
    for pos in positions:
        try:
            if pos.symbol == symbol and float(pos.qty) > 0:
                price = float(pos.current_price)
                current_qty = int(float(pos.qty))
                break
        except Exception:
            pass

    if price is None or price <= 0:
        return f"no current_price for {symbol}"

    target_mv      = max_pos_pct_equity * equity
    target_qty     = int(target_mv / price)
    shares_to_sell = current_qty - target_qty

    if shares_to_sell <= 0:
        return (
            f"reduce {symbol}: position within {max_pos_pct_equity*100:.0f}% "
            f"equity limit — no trim needed"
        )

    log.info(
        "[ALLOC] TRIM %s: selling %d shares (held=%d → target=%d, %.1f%% equity → %.1f%%)",
        symbol, shares_to_sell, current_qty, target_qty,
        current_qty * price / equity * 100,
        max_pos_pct_equity * 100,
    )

    action = BrokerAction(
        symbol=symbol,
        action=AccountAction.SELL,
        qty=float(shares_to_sell),
        order_type="market",
        tier=Tier.CORE,
        conviction=Conviction.MEDIUM,
        catalyst=reason[:120],
    )

    exec_positions = snapshot.positions if snapshot is not None else positions
    try:
        results = execute_all(
            [action], account, exec_positions, market_status, minutes_since_open,
            session_tier=session_tier,
        )
        if results:
            first = results[0]
            if first.status not in ("submitted", "hold"):
                reason = getattr(first, "reason", None) or first.status
                log.warning("[ALLOC] TRIM %s FAILED — %s", symbol, reason)
                return f"trim_failed: {reason}"
        return f"ok:{shares_to_sell}"
    except Exception as exc:
        return f"execute_all error: {exc}"


def _has_open_buy_order(symbol: str, _client=None) -> tuple[bool, str]:
    """Return (True, order_id) if Alpaca has an open BUY-side order for symbol, else (False, '').

    _client is injected only in tests; production always uses None (lazy _get_alpaca()).
    Side comparison uses str-split to match the existing pattern in order_executor.py and
    avoid enum-identity issues under various test-collection module states.
    """
    try:
        from alpaca.trading.enums import QueryOrderStatus  # noqa: PLC0415
        from alpaca.trading.requests import GetOrdersRequest  # noqa: PLC0415

        if _client is None:
            from order_executor import _get_alpaca  # noqa: PLC0415
            _client = _get_alpaca()

        orders = _client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.OPEN, symbols=[symbol],
        ))
        for o in orders:
            if str(getattr(o, "side", "")).lower().split(".")[-1] == "buy":
                return True, str(o.id)
        return False, ""
    except Exception as exc:
        log.warning(
            "[ALLOC] _has_open_buy_order(%s): query failed (skipping guard) — %s", symbol, exc,
        )
        return False, ""


def _execute_live_add(
    symbol:             str,
    positions:          list,
    snapshot,
    cfg:                dict,
    session_tier:       str,
    reason:             str,
    vix:                Optional[float] = None,
    account=None,
    market_status:      str = "open",
    minutes_since_open: int = 0,
) -> str:
    """
    Execute a single ADD via risk_kernel BUY path + execute_all().
    Returns "ok:{qty}" on success, rejection/error reason string otherwise.
    Non-fatal — caller logs and continues regardless of return value.
    """
    try:
        from order_executor import execute_all  # noqa: PLC0415
        from risk_kernel import process_idea  # noqa: PLC0415
        from schemas import (  # noqa: PLC0415
            AccountAction,
            Direction,
            Tier,
            TradeIdea,
        )
    except ImportError as exc:
        return f"import error: {exc}"

    if account is None:
        return f"account not provided for live ADD of {symbol}"

    price: Optional[float] = None
    for pos in positions:
        try:
            if pos.symbol == symbol and float(pos.qty) > 0:
                price = float(pos.current_price)
                break
        except Exception:
            pass

    if price is None or price <= 0:
        return f"no current_price for {symbol}"

    pending, pending_id = _has_open_buy_order(symbol)
    if pending:
        log.info(
            "[ALLOC] ADD skipped — open buy order already pending for %s (%s), avoiding stack",
            symbol, pending_id,
        )
        return f"skipped: open buy order pending for {symbol}"

    idea = TradeIdea(
        symbol=symbol,
        action=AccountAction.BUY,
        tier=Tier.DYNAMIC,
        conviction=0.6,
        direction=Direction.BULLISH,
        catalyst=reason[:120],
        intent="enter_long",
    )

    result = process_idea(
        idea=idea,
        snapshot=snapshot,
        signal=None,
        config=cfg,
        current_price=price,
        session_tier=session_tier,
        vix=vix,
    )

    if isinstance(result, str):
        return result

    exec_positions = snapshot.positions if snapshot is not None else positions
    try:
        results = execute_all(
            [result], account, exec_positions, market_status, minutes_since_open,
            session_tier=session_tier,
        )
        if results:
            first = results[0]
            if first.status not in ("submitted", "hold"):
                reason = getattr(first, "reason", None) or first.status
                log.warning("[ALLOC] ADD %s FAILED — %s", symbol, reason)
                return f"add_failed: {reason}"
        return f"ok:{result.qty}"
    except Exception as exc:
        return f"execute_all error: {exc}"


def _cancel_protective_orders(
    symbol:        str,
    poll_seconds:  float = 3.0,
) -> int:
    """
    Cancel any open OCO / bracket / stop / stop_limit / trailing_stop SELL orders
    for `symbol`, then poll Alpaca until the cancellations leave OPEN status
    (or `poll_seconds` elapses).

    Returns the number of orders for which a cancel request was submitted.
    Non-fatal — exceptions are logged and the function returns 0.

    Used by _execute_live_replace before Phase A close, to release Alpaca's
    held_for_orders lock that otherwise causes a 40310000 "insufficient qty"
    rejection on the close. Alpaca paper does not synchronously release the
    lock when a cancel is accepted (status sits at PENDING_CANCEL for ~1–3s),
    so we poll until OPEN orders are gone.
    """
    try:
        import time  # noqa: PLC0415

        from alpaca.trading.enums import QueryOrderStatus  # noqa: PLC0415
        from alpaca.trading.requests import GetOrdersRequest  # noqa: PLC0415

        from order_executor import _get_alpaca  # noqa: PLC0415
    except ImportError as exc:
        log.warning("[REPLACE] %s: cancel_protective_orders import error: %s", symbol, exc)
        return 0

    try:
        alpaca = _get_alpaca()
        open_orders = alpaca.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.OPEN, symbols=[symbol],
        ))
    except Exception as exc:
        log.warning("[REPLACE] %s: could not fetch open orders: %s", symbol, exc)
        return 0

    cancelled_ids: list[str] = []
    for o in open_orders:
        o_side  = str(getattr(o, "side",        "")).lower().split(".")[-1]
        o_type  = str(getattr(o, "order_type",  getattr(o, "type", ""))).lower().split(".")[-1]
        o_class = str(getattr(o, "order_class", "")).lower().split(".")[-1]
        if o_side == "sell" and (
            o_type in ("stop", "stop_limit", "trailing_stop")
            or o_class in ("oco", "bracket", "oto")
        ):
            try:
                alpaca.cancel_order_by_id(str(o.id))
                cancelled_ids.append(str(o.id))
            except Exception as exc:
                log.warning(
                    "[REPLACE] %s: cancel order %s failed: %s", symbol, o.id, exc,
                )

    if not cancelled_ids:
        return 0

    log.info(
        "[REPLACE] %s: canceled %d protective orders before close",
        symbol, len(cancelled_ids),
    )

    deadline = time.time() + max(0.0, float(poll_seconds))
    while time.time() < deadline:
        try:
            still_open = alpaca.get_orders(GetOrdersRequest(
                status=QueryOrderStatus.OPEN, symbols=[symbol],
            ))
            still_locking = [
                o for o in still_open if str(o.id) in cancelled_ids
            ]
            if not still_locking:
                return len(cancelled_ids)
        except Exception as exc:
            log.warning("[REPLACE] %s: poll cancel-status error: %s", symbol, exc)
            break
        time.sleep(0.25)

    log.warning(
        "[REPLACE] %s: %d cancellations did not confirm within %.1fs — proceeding anyway",
        symbol, len(cancelled_ids), float(poll_seconds),
    )
    return len(cancelled_ids)


def _execute_live_replace(
    exit_symbol:        str,
    enter_symbol:       str,
    positions:          list,
    snapshot,
    cfg:                dict,
    session_tier:       str,
    reason:             str,
    enter_price:        Optional[float] = None,
    vix:                Optional[float] = None,
    account=None,
    market_status:      str = "open",
    minutes_since_open: int = 0,
) -> str:
    """
    Execute a REPLACE: Phase A closes exit_symbol; Phase B enters enter_symbol.
    Phase B only proceeds if Phase A submission succeeds.
    Returns "ok:close={exit}+enter={enter}:{qty}" on full success.
    Non-fatal — caller logs and continues regardless of return value.

    Note: snapshot passed to Phase B is pre-close (stale). The kernel may
    compute slightly larger Phase B sizing because the exit_symbol value is
    still counted in snapshot.exposure_dollars. Acceptable for Stage 1.
    """
    try:
        from order_executor import execute_all  # noqa: PLC0415
        from risk_kernel import process_idea  # noqa: PLC0415
        from schemas import (  # noqa: PLC0415
            AccountAction,
            Direction,
            Tier,
            TradeIdea,
        )
    except ImportError as exc:
        return f"import error: {exc}"

    if account is None:
        return f"account not provided for live REPLACE of {exit_symbol}→{enter_symbol}"

    # ── Phase A: close exit_symbol ────────────────────────────────────────────
    exit_price: Optional[float] = None
    for pos in positions:
        try:
            if pos.symbol == exit_symbol and float(pos.qty) > 0:
                exit_price = float(pos.current_price)
                break
        except Exception:
            pass

    if exit_price is None or exit_price <= 0:
        return f"no current_price for exit symbol {exit_symbol}"

    close_idea = TradeIdea(
        symbol=exit_symbol,
        action=AccountAction.CLOSE,
        tier=Tier.CORE,
        conviction=1.0,
        direction=Direction.NEUTRAL,
        catalyst=f"REPLACE: exit {exit_symbol} for {enter_symbol}",
        intent="close",
    )

    close_result = process_idea(
        idea=close_idea,
        snapshot=snapshot,
        signal=None,
        config=cfg,
        current_price=exit_price,
        session_tier=session_tier,
        vix=vix,
    )

    if isinstance(close_result, str):
        return f"phase_a_rejected: {close_result}"

    # Pre-cancel any OCO/bracket sell orders holding the incumbent qty.
    # Without this, Alpaca rejects the close with 40310000 ("insufficient qty
    # available") because held_for_orders == position qty. The in-executor
    # OCA-retry exists as a backstop but its 1s sleep is too short for paper
    # trading's PENDING_CANCEL latency, so we cancel-and-poll up-front.
    # TODO(real-money): after Phase A fills, immediately place a stop on
    # enter_symbol before submitting Phase B to close the unprotected window.
    pa_cfg = _get_pa_config(cfg)
    if pa_cfg.get("replace_cancel_brackets", True):
        _cancel_protective_orders(
            exit_symbol,
            poll_seconds=float(pa_cfg.get("replace_cancel_poll_seconds", 3.0)),
        )

    exec_positions = snapshot.positions if snapshot is not None else positions
    try:
        phase_a_res = execute_all(
            [close_result], account, exec_positions, market_status, minutes_since_open,
            session_tier=session_tier,
        )
        if phase_a_res:
            first = phase_a_res[0]
            if first.status not in ("submitted", "hold"):
                reason_a = getattr(first, "reason", None) or first.status
                log.warning("[ALLOC] REPLACE %s→%s Phase A FAILED — %s",
                            exit_symbol, enter_symbol, reason_a)
                return f"phase_a_failed: {reason_a}"
    except Exception as exc:
        return f"phase_a_execute_error: {exc}"

    # ── Phase B: enter enter_symbol (only after Phase A submitted ok) ─────────
    if enter_price is None or enter_price <= 0:
        return f"ok:close={exit_symbol};phase_b_no_price:{enter_symbol}"

    enter_idea = TradeIdea(
        symbol=enter_symbol,
        action=AccountAction.BUY,
        tier=Tier.DYNAMIC,
        conviction=0.6,
        direction=Direction.BULLISH,
        catalyst=reason[:120],
        intent="enter_long",
    )

    enter_result = process_idea(
        idea=enter_idea,
        snapshot=snapshot,
        signal=None,
        config=cfg,
        current_price=enter_price,
        session_tier=session_tier,
        vix=vix,
    )

    if isinstance(enter_result, str):
        return f"ok:close={exit_symbol};phase_b_rejected:{enter_result}"

    try:
        phase_b_res = execute_all(
            [enter_result], account, exec_positions, market_status, minutes_since_open,
            session_tier=session_tier,
        )
        if phase_b_res:
            first = phase_b_res[0]
            if first.status not in ("submitted", "hold"):
                reason_b = getattr(first, "reason", None) or first.status
                return f"ok:close={exit_symbol};phase_b_failed:{reason_b}"
        return f"ok:close={exit_symbol}+enter={enter_symbol}:{enter_result.qty}"
    except Exception as exc:
        return f"ok:close={exit_symbol};phase_b_execute_error:{exc}"


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_allocator_shadow(
    pi_data:            dict,
    positions:          list,
    cfg:                dict,
    session_tier:       str = "market",
    equity:             float = 0.0,
    snapshot=None,
    vix:                Optional[float] = None,
    account=None,
    market_status:      str = "open",
    minutes_since_open: int = 0,
) -> Optional[dict]:
    """
    Run the portfolio allocator (shadow or live).

    Shadow mode (enable_live=False): zero execution side effects.
    Live mode (enable_live=True): TRIM actions execute via risk_kernel REDUCE
      path. Requires snapshot + account params.
    Live ADD (enable_live_add=True): ADD actions execute via BUY path.
    Live REPLACE (enable_live_replace=True): REPLACE executes two-phase close+buy.

    Consumes pi_data (from build_portfolio_intelligence) + held positions +
    top candidates from signal_scores.json. Writes one JSONL artifact per call.
    Returns the artifact dict, or None if shadow is disabled / concurrent cycle
    already running / fatal error.
    """
    pa_cfg = _get_pa_config(cfg)

    if not pa_cfg["enable_shadow"]:
        log.debug("[ALLOC] enable_shadow=false — shadow allocator skipped")
        return None

    # Re-entrancy guard: skip if another cycle is already running.
    if not _ALLOC_LOCK.acquire(blocking=False):
        log.warning("[ALLOC] concurrency guard: another cycle in progress — skipping")
        return None

    try:
        return _run_allocator_shadow_inner(
            pa_cfg=pa_cfg,
            pi_data=pi_data,
            positions=positions,
            cfg=cfg,
            session_tier=session_tier,
            equity=equity,
            snapshot=snapshot,
            vix=vix,
            account=account,
            market_status=market_status,
            minutes_since_open=minutes_since_open,
        )
    finally:
        _ALLOC_LOCK.release()


def _run_allocator_shadow_inner(
    pa_cfg:             dict,
    pi_data:            dict,
    positions:          list,
    cfg:                dict,
    session_tier:       str,
    equity:             float,
    snapshot,
    vix:                Optional[float],
    account,
    market_status:      str,
    minutes_since_open: int,
) -> Optional[dict]:
    """Inner implementation of run_allocator_shadow (called after lock acquired)."""
    if pa_cfg["enable_live"]:
        if snapshot is None or account is None:
            log.warning("[ALLOC] enable_live=True but snapshot/account missing — shadow only")
        else:
            log.info("[ALLOC] live mode active — TRIM actions will execute directly (equity-based)")

    try:
        now_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        # 1. Build held-symbol set (exclude both longs and shorts from candidate pool)
        held_symbols: set[str] = set()
        for pos in positions:
            try:
                if float(pos.qty) != 0:
                    held_symbols.add(pos.symbol)
            except Exception:
                pass

        sizes    = pi_data.get("sizes", {})
        eq_val   = equity or (float(sizes.get("max_exposure", 0) or 0) / 0.30)
        cap_val  = eq_val * 4   # 4× leverage basis (2× margin × 2 sides)

        # 2. Rank incumbents using total_capacity so account_pct matches kernel basis
        incumbents = _rank_incumbents(pi_data, positions, equity=eq_val, total_capacity=cap_val)

        # 2a. Attach signal catalyst/signals to incumbents (held symbols excluded from candidates)
        _enrich_incumbents_with_signal_data(incumbents)

        # 3. Load candidates from last cycle's signal scores
        candidates = _load_candidates(held_symbols)

        # 4. Run decision logic
        proposed, suppressed = _decide_actions(
            incumbents, candidates, pi_data, cfg, pa_cfg, sizes, eq_val
        )

        # 4a. Live TRIM execution
        live_trim_results: dict[str, str] = {}
        if (pa_cfg["enable_live"] and pa_cfg.get("enable_live_trim")
                and market_status == "open"
                and snapshot is not None and account is not None):
            for act in proposed:
                if act["action"] != "TRIM":
                    continue
                sym = act["symbol"]
                try:
                    res = _execute_live_trim(
                        sym, positions, snapshot, cfg, session_tier,
                        act.get("reason", f"allocator trim {sym}"),
                        vix=vix,
                        account=account,
                        market_status=market_status,
                        minutes_since_open=minutes_since_open,
                        equity=eq_val,
                        max_pos_pct_equity=float(pa_cfg.get("max_position_pct_equity", 0.15)),
                    )
                except Exception as _trim_exc:
                    res = f"error: {_trim_exc}"
                    log.warning("[ALLOC] LIVE: trim error %s — %s", sym, _trim_exc)
                live_trim_results[sym] = res
                if res.startswith("ok:"):
                    log.info("[ALLOC] LIVE: trimmed %s — %s", sym, res)
                else:
                    log.warning("[ALLOC] LIVE: trim rejected %s — %s", sym, res)

        # 4b. Live ADD execution (Stage 2 — disabled by default)
        live_add_results: dict[str, str] = {}
        if pa_cfg.get("enable_live_add") and snapshot is not None and account is not None:
            for act in proposed:
                if act["action"] != "ADD":
                    continue
                sym = act["symbol"]
                try:
                    res = _execute_live_add(
                        sym, positions, snapshot, cfg, session_tier,
                        act.get("reason", f"allocator add {sym}"),
                        vix=vix,
                        account=account,
                        market_status=market_status,
                        minutes_since_open=minutes_since_open,
                    )
                except Exception as _add_exc:
                    res = f"error: {_add_exc}"
                    log.warning("[ALLOC] LIVE: add error %s — %s", sym, _add_exc)
                live_add_results[sym] = res
                if res.startswith("ok:"):
                    log.info("[ALLOC] LIVE: added %s — %s", sym, res)
                else:
                    log.warning("[ALLOC] LIVE: add rejected %s — %s", sym, res)

        # 4c. Live REPLACE execution (Stage 2 — disabled by default)
        live_replace_results: dict[str, str] = {}
        if pa_cfg.get("enable_live_replace") and snapshot is not None and account is not None:
            for act in proposed:
                if act["action"] != "REPLACE":
                    continue
                enter_sym = act["symbol"]
                exit_sym  = act.get("exit_symbol", "")
                if not exit_sym:
                    continue
                enter_price = next(
                    (c["price"] for c in candidates if c["symbol"] == enter_sym),
                    None,
                )
                key = f"{exit_sym}→{enter_sym}"
                try:
                    res = _execute_live_replace(
                        exit_sym, enter_sym, positions, snapshot, cfg, session_tier,
                        act.get("reason", f"allocator replace {exit_sym}→{enter_sym}"),
                        enter_price=enter_price,
                        vix=vix,
                        account=account,
                        market_status=market_status,
                        minutes_since_open=minutes_since_open,
                    )
                except Exception as _repl_exc:
                    res = f"error: {_repl_exc}"
                    log.warning("[ALLOC] LIVE: replace error %s — %s", key, _repl_exc)
                live_replace_results[key] = res
                if res.startswith("ok:"):
                    log.info("[ALLOC] LIVE: replaced %s — %s", key, res)
                else:
                    log.warning("[ALLOC] LIVE: replace rejected %s — %s", key, res)
                    # Issue 5: when Phase A close fails (Alpaca 403 / 40310000 etc.),
                    # write a REPLACE cooldown for the exit symbol so the proposal
                    # does not re-fire every cycle and spam 403s. The hours-based
                    # check (Issue 2) clears it automatically after add_cooldown_hours.
                    if "phase_a" in str(res):
                        try:
                            cd = _load_cooldown()
                            cd = _add_to_cooldown(exit_sym, "REPLACE", cd)
                            _save_cooldown(cd)
                            log.info(
                                "[ALLOC] cooldown written for %s REPLACE after phase_a failure",
                                exit_sym,
                            )
                        except Exception as _cd_exc:
                            log.debug("[ALLOC] cooldown write failed: %s", _cd_exc)

        # 5. Identify weakest/strongest for summary
        weakest   = incumbents[0]  if incumbents  else None
        strongest = candidates[0]  if candidates  else None

        # 6. Compute target weights
        target_wts = _target_weights(incumbents, sizes)

        # 7. Summary stats
        n_trim    = sum(1 for p in proposed if p["action"] == "TRIM")
        n_add     = sum(1 for p in proposed if p["action"] == "ADD")
        n_replace = sum(1 for p in proposed if p["action"] == "REPLACE")
        n_hold    = sum(1 for p in proposed if p["action"] == "HOLD")

        summary = {
            "n_incumbents":    len(incumbents),
            "n_candidates":    len(candidates),
            "n_hold":          n_hold,
            "n_trim":          n_trim,
            "n_add":           n_add,
            "n_replace":       n_replace,
            "n_suppressed":    len(suppressed),
            "any_action_fired": (n_trim + n_add + n_replace) > 0,
            "weakest_score":   weakest["thesis_score"] if weakest else None,
            "strongest_score": strongest["signal_score"] if strongest else None,
        }

        # 8. Build artifact
        artifact: dict = {
            "schema_version":        SCHEMA_VERSION,
            "mode":                  "live" if pa_cfg["enable_live"] else "shadow",
            "timestamp":             now_ts,
            "session_tier":          session_tier,
            "current_holdings_snapshot": [
                {"symbol": inc["symbol"],
                 "market_value": inc["market_value"],
                 "account_pct": inc["account_pct"]}
                for inc in incumbents
            ],
            "candidate_snapshot": [
                {"symbol": c["symbol"], "signal_score": c["signal_score"],
                 "direction": c["direction"], "catalyst": c["catalyst"][:80]}
                for c in candidates[:10]
            ],
            "ranked_incumbents":    incumbents,
            "ranked_candidates":    candidates[:10],
            "weakest_incumbent":    weakest,
            "strongest_candidate":  strongest,
            "target_weights":       target_wts,
            "proposed_actions":     proposed,
            "suppressed_actions":   suppressed,
            "friction_blockers":    [s["suppression_reason"] for s in suppressed],
            "summary":              summary,
            "live_trim_results":    live_trim_results,
            "live_add_results":     live_add_results,
            "live_replace_results": live_replace_results,
            "config_snapshot": {
                "replace_score_gap":         pa_cfg["replace_score_gap"],
                "trim_score_drop":           pa_cfg["trim_score_drop"],
                "weight_deadband":           pa_cfg["weight_deadband"],
                "min_rebalance_notional":    pa_cfg["min_rebalance_notional"],
                "max_recommendations":       pa_cfg["max_recommendations_per_cycle"],
            },
        }

        # 9. Write artifact with rotation
        _write_artifact(artifact)

        # 9a. Shadow performance tracker — log allocator recommendations
        try:
            from performance_tracker import (  # noqa: PLC0415
                log_allocator_recommendations as _log_alloc,
            )
            _log_alloc(
                proposed_actions=proposed,
                incumbents=incumbents,
                candidates=candidates,
                positions=positions,
                cycle_id=now_ts,
            )
        except Exception as _pt_exc:
            log.debug("log_allocator_recommendations failed (non-fatal): %s", _pt_exc)

        # 10. Update shadow registry last_run_at
        _update_shadow_registry(now_ts)

        log.info(
            "[ALLOC] %s cycle complete — incumbents=%d candidates=%d "
            "hold=%d trim=%d add=%d replace=%d suppressed=%d",
            "LIVE" if pa_cfg["enable_live"] else "shadow",
            len(incumbents), len(candidates),
            n_hold, n_trim, n_add, n_replace, len(suppressed),
        )

        return artifact

    except Exception as exc:
        log.warning("[ALLOC] run_allocator_shadow failed (non-fatal): %s", exc)
        _write_artifact({
            "schema_version": SCHEMA_VERSION,
            "ts": datetime.now(timezone.utc).isoformat(),
            "status": "error",
            "error": str(exc),
        })
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Artifact I/O
# ─────────────────────────────────────────────────────────────────────────────

def _write_artifact(artifact: dict) -> None:
    """Append artifact to JSONL file and rotate. Non-fatal."""
    try:
        _ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(artifact, default=str)
        with _ARTIFACT_PATH.open("a") as fh:
            fh.write(line + "\n")
        _rotate_artifact_jsonl(_ARTIFACT_PATH, max_lines=10_000)
        log.debug("[ALLOC] shadow artifact written: %s", _ARTIFACT_PATH.name)
    except Exception as exc:
        log.warning("[ALLOC] artifact write failed — %s: %s", type(exc).__name__, exc)


# ─────────────────────────────────────────────────────────────────────────────
# Shadow registry update
# ─────────────────────────────────────────────────────────────────────────────

def _update_shadow_registry(timestamp: str) -> None:
    """
    Update shadow_status_latest.json with allocator's last_run_at.
    Non-fatal.
    """
    try:
        _REGISTRY_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing = json.loads(_REGISTRY_JSON_PATH.read_text())
        except Exception:
            existing = {}

        shadow_systems = existing.get("shadow_systems", {})
        if "portfolio_allocator" not in shadow_systems:
            shadow_systems["portfolio_allocator"] = {}
        shadow_systems["portfolio_allocator"]["last_run_at"] = timestamp
        shadow_systems["portfolio_allocator"]["status"] = "active"

        existing["shadow_systems"] = shadow_systems
        existing["updated_at"] = timestamp

        tmp = _REGISTRY_JSON_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(existing, indent=2))
        import os
        os.replace(tmp, _REGISTRY_JSON_PATH)
    except Exception as exc:
        log.debug("[ALLOC] registry update failed (non-fatal): %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Prompt formatting
# ─────────────────────────────────────────────────────────────────────────────

def format_allocator_section(output: Optional[dict]) -> str:
    """
    Format a compact allocator summary for Stage 3 prompt injection.
    Returns "" if no output available. Advisory context only.

    Authority: PRESENTATION — formats shadow output as advisory prompt text.
      No enforcement authority. Claude treats this as context, not mandate.
    """
    # S7-E: explicit absence header — Claude knowing the section exists but has no
    # data this cycle is more informative than silent omission (Option B confirmed).
    if not output:
        return (
            "=== PORTFOLIO ALLOCATOR SHADOW (advisory only) ===\n"
            "  (allocator not available this cycle)"
        )

    mode  = output.get("mode", "shadow")
    label = "LIVE" if mode == "live" else "SHADOW"
    lines = [f"=== PORTFOLIO ALLOCATOR {label} (advisory only) ==="]

    weakest  = output.get("weakest_incumbent")
    strongest = output.get("strongest_candidate")

    if weakest:
        score = weakest.get("thesis_score", "?")
        sym   = weakest.get("symbol", "?")
        lines.append(f"Weakest incumbent : {sym}  thesis_score={score}/10"
                     f"  health={weakest.get('health','?')}")

    if strongest:
        scr  = strongest.get("signal_score", "?")
        sym  = strongest.get("symbol", "?")
        dirn = strongest.get("direction", "?")
        lines.append(f"Strongest candidate: {sym}  signal_score={scr:.0f}"
                     f"  direction={dirn}")

    # Show non-HOLD proposed actions
    actions = [p for p in output.get("proposed_actions", []) if p.get("action") != "HOLD"]
    if actions:
        lines.append("Allocator recommendations (advisory):")
        for act in actions[:3]:
            gap_str  = f"  gap={act['score_gap']:.0f}" if act.get("score_gap") is not None else ""
            exit_str = f"  exit={act['exit_symbol']}" if act.get("exit_symbol") else ""
            lines.append(f"  {act['action']} {act['symbol']}{exit_str}{gap_str}")
    elif output.get("suppressed_actions"):
        blocker = output["suppressed_actions"][0].get("suppression_reason", "")[:100]
        lines.append(f"No allocator action: {blocker}")

    if mode == "live":
        live_parts = ["TRIM"]
        if output.get("live_add_results") is not None:
            live_parts.append("ADD")
        if output.get("live_replace_results") is not None:
            live_parts.append("REPLACE")
        active = "+".join(live_parts) if len(live_parts) > 1 else live_parts[0]
        lines.append(f"[LIVE — {active} active]")
    else:
        lines.append("[SHADOW MODE — do not treat as live order mandate]")
    return "\n".join(lines)
