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
  TRIM    — thesis_score <= 4 (normalized <=40) AND notional > min_rebalance_notional
  ADD     — thesis_score >= 7 (normalized >=70) AND room below tier ceiling
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_ROOT                = Path(__file__).parent
_ARTIFACT_PATH       = _ROOT / "data" / "analytics" / "portfolio_allocator_shadow.jsonl"
_SIGNAL_SCORES_PATH  = _ROOT / "data" / "market" / "signal_scores.json"
_REGISTRY_JSON_PATH  = _ROOT / "data" / "reports" / "shadow_status_latest.json"

SCHEMA_VERSION = 1

# Module-level same-day cooldown: symbol → "YYYY-MM-DD" of last recommendation.
# Cleared implicitly when the date changes. Not persisted across restarts.
_daily_cooldown: dict[str, str] = {}


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

_PA_DEFAULTS: dict = {
    "enable_shadow":                   True,
    "enable_live":                     False,   # ALWAYS False this sprint
    "replace_score_gap":               15.0,
    "trim_score_drop":                 10.0,    # normalized threshold for TRIM (score ≤ 40)
    "weight_deadband":                 0.02,    # 2% — min weight gap to trigger action
    "min_rebalance_notional":          500.0,   # $500 minimum to recommend
    "max_recommendations_per_cycle":   3,
    "same_symbol_daily_cooldown_enabled": True,
    "same_day_replace_block_hours":    6.0,
}


def _get_pa_config(cfg: dict) -> dict:
    """Return portfolio_allocator config merged with defaults. Non-fatal."""
    pa = cfg.get("portfolio_allocator", {})
    merged = dict(_PA_DEFAULTS)
    for k, v in pa.items():
        if k in merged:
            merged[k] = v
    merged["enable_live"] = False   # hard-override: live disabled this sprint
    return merged


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
                "catalyst":     (info.get("catalyst", "") or "")[:120],
                "price":        float(info.get("price", 0) or 0),
            })
        candidates.sort(key=lambda c: c["signal_score"], reverse=True)
        return candidates[:20]   # top 20 is more than enough
    except Exception as exc:
        log.debug("[ALLOC] candidate load failed (non-fatal): %s", exc)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Incumbent ranking
# ─────────────────────────────────────────────────────────────────────────────

def _rank_incumbents(pi_data: dict, positions: list) -> list[dict]:
    """
    Build ranked incumbent list from pi_data thesis_scores + positions.
    Returns list sorted by thesis_score ascending (weakest first).
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

    equity = float(pi_data.get("sizes", {}).get("max_exposure", 0) or 0) / 0.30
    if equity <= 0:
        # fallback: sum of all market values
        equity = sum(mv_by_symbol.values()) or 1.0

    incumbents = []
    for sym, mv in mv_by_symbol.items():
        ts       = ts_by_symbol.get(sym, {})
        health   = health_map.get(sym, {})
        score    = int(ts.get("thesis_score", 5))
        account_pct = round(mv / equity * 100, 2) if equity > 0 else 0.0

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

def _check_cooldown(symbol: str, pa_cfg: dict) -> tuple[bool, str]:
    """Returns (passes, reason). passes=True means no cooldown block."""
    if not pa_cfg["same_symbol_daily_cooldown_enabled"]:
        return True, ""
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    recorded  = _daily_cooldown.get(symbol)
    if recorded == today_str:
        return False, f"{symbol} already received a recommendation today (daily cooldown)"
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
    available_for_new   = float(sizes.get("available_for_new", 0) or 0)

    target_wts = _target_weights(incumbents, sizes)
    proposed:   list[dict] = []
    suppressed: list[dict] = []

    # ── Per-incumbent HOLD / TRIM / ADD decisions ─────────────────────────────
    for inc in incumbents:
        sym      = inc["symbol"]
        score    = inc["thesis_score"]
        norm     = inc["thesis_score_normalized"]
        mv       = inc["market_value"]
        acct_pct = inc["account_pct"] / 100.0   # fraction
        tier_max = target_wts.get(sym, 0.08)

        # TRIM: thesis weak AND position is large enough to trim meaningfully
        # threshold: thesis_score <= 4 (normalized <= 40), which represents
        # "exit_consider" or "reduce" territory in portfolio_intelligence
        if score <= 4 and mv > min_notional:
            trim_notional = round(mv * 0.25, 2)   # trim ~25% of position
            if trim_notional >= min_notional:
                proposed.append({
                    "action":           "TRIM",
                    "symbol":           sym,
                    "reason":           (
                        f"thesis_score={score}/10 (normalized={norm}) — weak thesis; "
                        f"consider trimming ~25% (${trim_notional:,.0f})"
                    ),
                    "score_gap":        None,
                    "target_weight_pct": tier_max,
                    "exit_symbol":      None,
                })
                continue   # don't double-count as HOLD

        # ADD: thesis strong AND room to grow below tier ceiling
        # threshold: thesis_score >= 7 (normalized >= 70)
        if (score >= 7
                and available_for_new > min_notional
                and acct_pct < tier_max - weight_deadband):
            proposed.append({
                "action":           "ADD",
                "symbol":           sym,
                "reason":           (
                    f"thesis_score={score}/10 — strong; room to add "
                    f"(acct_pct={acct_pct:.1%} < tier_max={tier_max:.0%}), "
                    f"available_for_new=${available_for_new:,.0f}"
                ),
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
                proposed.append({
                    "action":           "REPLACE",
                    "symbol":           cand_sym,
                    "reason":           (
                        f"candidate signal_score={cand_scr:.0f} vs weakest "
                        f"{weak_sym} normalized={weak_norm} — gap={gap:.0f} "
                        f">= threshold={replace_score_gap:.0f}"
                    ),
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

    # ── Record cooldown for non-HOLD recommendations ──────────────────────────
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for p in proposed:
        if p["action"] != "HOLD":
            _daily_cooldown[p["symbol"]] = today_str

    return proposed, suppressed


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_allocator_shadow(
    pi_data:      dict,
    positions:    list,
    cfg:          dict,
    session_tier: str = "market",
    equity:       float = 0.0,
) -> Optional[dict]:
    """
    Run the portfolio allocator in shadow mode.

    Consumes pi_data (from build_portfolio_intelligence) + held positions +
    top candidates from signal_scores.json. Writes one JSONL artifact per call.
    Returns the artifact dict, or None if shadow is disabled / fatal error.

    Authority: SHADOW — zero execution side effects. Does not call execute_all().
    Does not call execute_reallocate(). Output is advisory only.
    """
    pa_cfg = _get_pa_config(cfg)

    if not pa_cfg["enable_shadow"]:
        log.debug("[ALLOC] enable_shadow=false — shadow allocator skipped")
        return None

    # Hard-wired safety: live mode is never enabled this sprint
    if pa_cfg["enable_live"]:
        log.warning("[ALLOC] enable_live=True ignored — live allocator disabled this sprint")

    try:
        now_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        # 1. Build held-symbol set
        held_symbols: set[str] = set()
        for pos in positions:
            try:
                if float(pos.qty) > 0:
                    held_symbols.add(pos.symbol)
            except Exception:
                pass

        # 2. Rank incumbents
        incumbents = _rank_incumbents(pi_data, positions)

        # 3. Load candidates from last cycle's signal scores
        candidates = _load_candidates(held_symbols)

        sizes  = pi_data.get("sizes", {})
        eq_val = equity or (float(sizes.get("max_exposure", 0) or 0) / 0.30)

        # 4. Run decision logic
        proposed, suppressed = _decide_actions(
            incumbents, candidates, pi_data, cfg, pa_cfg, sizes, eq_val
        )

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

        # 10. Update shadow registry last_run_at
        _update_shadow_registry(now_ts)

        log.info(
            "[ALLOC] shadow cycle complete — incumbents=%d candidates=%d "
            "hold=%d trim=%d add=%d replace=%d suppressed=%d",
            len(incumbents), len(candidates),
            n_hold, n_trim, n_add, n_replace, len(suppressed),
        )

        return artifact

    except Exception as exc:
        log.warning("[ALLOC] run_allocator_shadow failed (non-fatal): %s", exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Artifact I/O
# ─────────────────────────────────────────────────────────────────────────────

def _write_artifact(artifact: dict) -> None:
    """Append artifact to JSONL file and rotate. Non-fatal."""
    try:
        _ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
        line   = json.dumps(artifact, default=str)
        # Atomic append: write to tmp then rename is not practical for append-only
        # JSONL. Use standard append and rotate after.
        with _ARTIFACT_PATH.open("a") as fh:
            fh.write(line + "\n")
        # Import _rotate_jsonl from cost_attribution (same pattern as S5-3)
        from cost_attribution import _rotate_jsonl  # noqa: PLC0415
        _rotate_jsonl(_ARTIFACT_PATH, max_lines=10_000)
    except Exception as exc:
        log.warning("[ALLOC] artifact write failed (non-fatal): %s", exc)


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
    if not output:
        return ""

    lines = ["=== PORTFOLIO ALLOCATOR SHADOW (advisory only) ==="]

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
        lines.append("Shadow recommendations (advisory):")
        for act in actions[:3]:
            gap_str = f"  gap={act['score_gap']:.0f}" if act.get("score_gap") is not None else ""
            exit_str = f"  exit={act['exit_symbol']}" if act.get("exit_symbol") else ""
            lines.append(f"  {act['action']} {act['symbol']}{exit_str}{gap_str}")
    elif output.get("suppressed_actions"):
        blocker = output["suppressed_actions"][0].get("suppression_reason", "")[:100]
        lines.append(f"No shadow action: {blocker}")

    lines.append("[SHADOW MODE — do not treat as live order mandate]")
    return "\n".join(lines)
