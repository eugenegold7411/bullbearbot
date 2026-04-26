"""
preflight.py — Cycle preflight go/no-go gate (P4).

run_preflight() is called at the top of run_cycle() and run_options_cycle().
It checks local file state only — no network calls, never raises.
Appends a record to data/status/preflight_log.jsonl on every call.

Verdicts:
    go              — all checks pass, proceed normally
    go_degraded     — soft check(s) failed, proceed with degraded mode (logged)
    shadow_only     — only shadow/annex reads allowed (no live orders)
    reconcile_only  — only reconciliation actions (no new trades)
    halt            — do not proceed; return from run_cycle() immediately

HARD failures → halt or reconcile_only
SOFT failures → go_degraded
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_LOG_DIR     = Path("data/status")
_LOG_FILE    = _LOG_DIR / "preflight_log.jsonl"
_A1_MODE     = Path("data/runtime/a1_mode.json")
_A2_MODE     = Path("data/runtime/a2_mode.json")
_CONFIG      = Path("strategy_config.json")
_BOT_LOG     = Path("logs/bot.log")
_PDT_FLOOR   = 26_000.0

SCHEMA_VERSION = 1

_HALT_MODES = frozenset({"HALTED"})
_RECONCILE_MODES = frozenset({"RECONCILE_ONLY", "RISK_CONTAINMENT"})

# Checks return (passed: bool, severity: str, message: str)
# severity: "hard" | "soft"


@dataclass
class CheckResult:
    name: str
    passed: bool
    severity: str          # "hard" | "soft"
    message: str
    verdict_hint: Optional[str] = None  # "halt" | "reconcile_only" | None


@dataclass
class PreflightResult:
    schema_version: int = SCHEMA_VERSION
    checked_at: str = ""
    caller: str = ""           # "run_cycle" | "run_options_cycle"
    session_tier: str = ""
    verdict: str = "go"        # go | go_degraded | shadow_only | reconcile_only | halt
    checks: list = field(default_factory=list)
    blockers: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def _check_config_present() -> CheckResult:
    """strategy_config.json must exist and be parseable — bot cannot run without it."""
    if not _CONFIG.exists():
        return CheckResult("config_present", False, "hard", "strategy_config.json missing")
    try:
        json.loads(_CONFIG.read_text())
        return CheckResult("config_present", True, "hard", "strategy_config.json OK")
    except Exception as exc:
        return CheckResult("config_present", False, "hard", f"strategy_config.json parse error: {exc}")


def _check_pdt_floor(equity: Optional[float]) -> CheckResult:
    """A1 equity must be above PDT floor before attempting trades."""
    if equity is None:
        return CheckResult("pdt_floor", True, "soft", "equity unknown — skipped")
    if equity < _PDT_FLOOR:
        return CheckResult("pdt_floor", False, "hard",
                           f"equity ${equity:,.0f} below PDT floor ${_PDT_FLOOR:,.0f}")
    return CheckResult("pdt_floor", True, "hard", f"equity ${equity:,.0f} OK")


def _check_operating_mode(account_id: str = "a1") -> CheckResult:
    """Divergence operating mode must not be HALTED before starting a trade cycle."""
    path = _A1_MODE if account_id == "a1" else _A2_MODE
    if not path.exists():
        return CheckResult(
            f"operating_mode_{account_id}", False, "hard",
            f"{account_id} mode file absent — entering reconcile_only until state is verified",
            verdict_hint="reconcile_only",
        )
    try:
        data = json.loads(path.read_text())
        mode = data.get("mode", "NORMAL").upper()
        if mode in _HALT_MODES:
            return CheckResult(
                f"operating_mode_{account_id}", False, "hard",
                f"{account_id} divergence mode={mode} — halt",
                verdict_hint="halt",
            )
        if mode in _RECONCILE_MODES:
            return CheckResult(
                f"operating_mode_{account_id}", False, "hard",
                f"{account_id} divergence mode={mode} — reconcile_only",
                verdict_hint="reconcile_only",
            )
        return CheckResult(f"operating_mode_{account_id}", True, "hard", f"{account_id} mode={mode} OK")
    except Exception as exc:
        return CheckResult(
            f"operating_mode_{account_id}", False, "hard",
            f"{account_id} mode file unreadable ({exc}) — entering reconcile_only until state is verified",
            verdict_hint="reconcile_only",
        )


def _check_watchlist_present() -> CheckResult:
    """At least one watchlist file must be non-empty."""
    wl_path = Path("watchlist.json")
    if not wl_path.exists():
        return CheckResult("watchlist_present", False, "soft", "watchlist.json missing")
    try:
        data = json.loads(wl_path.read_text())
        total = sum(len(v) for v in data.values() if isinstance(v, list))
        if total == 0:
            return CheckResult("watchlist_present", False, "soft", "watchlist.json empty")
        return CheckResult("watchlist_present", True, "soft", f"watchlist OK ({total} symbols)")
    except Exception as exc:
        return CheckResult("watchlist_present", False, "soft", f"watchlist parse error: {exc}")


def _check_vix_halt() -> CheckResult:
    """Soft check: if morning snapshot shows VIX > 35, flag degraded."""
    try:
        snap_path = Path("data/market/macro_snapshot.json")
        if not snap_path.exists():
            return CheckResult("vix_gate", True, "soft", "macro_snapshot absent — skipped")
        data = json.loads(snap_path.read_text())
        vix_raw = data.get("vix", 0) or 0
        # macro_snapshot may store VIX as {"price": N, "chg_pct": M} dict
        if isinstance(vix_raw, dict):
            vix_raw = vix_raw.get("price", 0) or 0
        vix = float(vix_raw)
        if vix > 35:
            return CheckResult("vix_gate", False, "soft",
                               f"VIX={vix:.1f} > 35 — crisis regime flag")
        return CheckResult("vix_gate", True, "soft", f"VIX={vix:.1f} OK")
    except Exception as exc:
        return CheckResult("vix_gate", True, "soft", f"VIX check skipped: {exc}")


def _check_recent_halt_markers() -> CheckResult:
    """Soft check: scan recent bot.log for halt/drawdown markers."""
    try:
        if not _BOT_LOG.exists():
            return CheckResult("recent_halt_markers", True, "soft", "bot.log absent — skipped")
        lines = _BOT_LOG.read_text(errors="replace").splitlines()[-100:]
        halt_markers = [
            l for l in lines
            if "DRAWDOWN GUARD" in l or "mode=halted" in l or "[HALT]" in l
        ]
        if halt_markers:
            return CheckResult("recent_halt_markers", False, "soft",
                               f"{len(halt_markers)} halt marker(s) in last 100 log lines")
        return CheckResult("recent_halt_markers", True, "soft", "no halt markers in recent log")
    except Exception as exc:
        return CheckResult("recent_halt_markers", True, "soft", f"log check skipped: {exc}")


def _check_feature_flags_loadable() -> CheckResult:
    """feature_flags.py must be importable and return a dict."""
    try:
        from feature_flags import get_all_flags  # noqa: PLC0415
        flags = get_all_flags()
        if not isinstance(flags, dict):
            return CheckResult("feature_flags_loadable", False, "soft", "get_all_flags() returned non-dict")
        return CheckResult("feature_flags_loadable", True, "soft", f"feature_flags OK ({len(flags)} flags)")
    except Exception as exc:
        return CheckResult("feature_flags_loadable", False, "soft", f"feature_flags import error: {exc}")


def _check_data_dirs() -> CheckResult:
    """Soft: key data directories must exist."""
    required = [
        Path("data/analytics"),
        Path("data/market"),
    ]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        return CheckResult("data_dirs", False, "soft", f"missing dirs: {missing}")
    return CheckResult("data_dirs", True, "soft", "data dirs present")


def _check_strategy_config_keys() -> CheckResult:
    """Soft: strategy_config.json must have active_strategy and parameters."""
    try:
        data = json.loads(_CONFIG.read_text())
        missing_keys = [k for k in ("active_strategy", "parameters") if k not in data]
        if missing_keys:
            return CheckResult("config_keys", False, "soft", f"config missing keys: {missing_keys}")
        return CheckResult("config_keys", True, "soft", "config keys OK")
    except Exception as exc:
        return CheckResult("config_keys", False, "soft", f"config keys check failed: {exc}")


def _check_director_notes_shape() -> CheckResult:
    """
    Soft/hard: director_notes must be a structured dict with active_context key.
    Old plain-string form → soft warn (still usable).
    Present but malformed dict (dict without active_context) → hard fail (cannot trust memo).
    Absent → soft pass (first-run safe).
    """
    try:
        data = json.loads(_CONFIG.read_text())
        dn = data.get("director_notes")
        if dn is None:
            return CheckResult("director_notes_shape", True, "soft", "director_notes absent — skipped")
        if isinstance(dn, str):
            return CheckResult(
                "director_notes_shape", False, "soft",
                "director_notes is plain string — should be migrated to {active_context, expiry, priority}",
            )
        if isinstance(dn, dict):
            if "active_context" not in dn:
                return CheckResult(
                    "director_notes_shape", False, "hard",
                    "director_notes dict missing required key 'active_context'",
                )
            return CheckResult("director_notes_shape", True, "soft", "director_notes shape OK")
        return CheckResult(
            "director_notes_shape", False, "hard",
            f"director_notes has unexpected type {type(dn).__name__}",
        )
    except Exception as exc:
        return CheckResult("director_notes_shape", False, "soft", f"director_notes check failed: {exc}")


_REQUIRED_TIER_KEYS = frozenset({"core_tier_pct", "dynamic_tier_pct", "intraday_tier_pct"})


def _check_tier_sizing_vocabulary() -> CheckResult:
    """Hard: position_sizing block must contain exactly the three kernel tier keys."""
    try:
        data = json.loads(_CONFIG.read_text())
        sizing = data.get("position_sizing", {})
        tier_keys = {k for k in sizing if k.endswith("_tier_pct")}

        missing = sorted(_REQUIRED_TIER_KEYS - tier_keys)
        if missing:
            return CheckResult(
                "tier_sizing_vocabulary", False, "hard",
                f"position_sizing missing required tier key(s): {missing}",
            )

        rogue = sorted(tier_keys - _REQUIRED_TIER_KEYS)
        if rogue:
            return CheckResult(
                "tier_sizing_vocabulary", False, "hard",
                f"position_sizing contains unrecognized tier key(s): {rogue}",
            )

        bad_values = [
            f"{k}={sizing[k]}" for k in _REQUIRED_TIER_KEYS
            if not (isinstance(sizing[k], (int, float)) and 0 < sizing[k] < 1)
        ]
        if bad_values:
            return CheckResult(
                "tier_sizing_vocabulary", False, "hard",
                f"tier key value(s) not in (0, 1): {bad_values}",
            )

        return CheckResult(
            "tier_sizing_vocabulary", True, "hard",
            f"tier keys OK: { {k: sizing[k] for k in sorted(_REQUIRED_TIER_KEYS)} }",
        )
    except Exception as exc:
        return CheckResult("tier_sizing_vocabulary", False, "hard",
                           f"tier sizing check failed: {exc}")


def _derive_verdict(checks: list[CheckResult]) -> tuple[str, list[str], list[str]]:
    """Derive verdict from check results. Returns (verdict, blockers, warnings).

    Precedence (highest → lowest):
      1. Any check with verdict_hint="halt"    → halt
      2. Any check with verdict_hint="reconcile_only" → reconcile_only
      3. Any hard-fail without a hint          → halt
      4. Any soft-fail                         → go_degraded
      5. All passed                            → go
    """
    blockers = []
    warnings = []
    has_halt_hint      = False
    has_reconcile_hint = False
    has_hard_fail      = False

    for c in checks:
        if c.passed:
            continue
        if c.severity == "hard":
            if c.verdict_hint == "halt":
                has_halt_hint = True
            elif c.verdict_hint == "reconcile_only":
                has_reconcile_hint = True
            else:
                has_hard_fail = True
            blockers.append(f"{c.name}: {c.message}")
        else:
            warnings.append(f"{c.name}: {c.message}")

    if has_halt_hint or has_hard_fail:
        return "halt", blockers, warnings
    if has_reconcile_hint:
        return "reconcile_only", blockers, warnings
    if warnings:
        return "go_degraded", blockers, warnings
    return "go", blockers, warnings


def _append_log(result: PreflightResult) -> None:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    record = asdict(result)
    try:
        with _LOG_FILE.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as exc:
        log.warning("[PREFLIGHT] Could not write log: %s", exc)


def run_preflight(
    caller: str = "run_cycle",
    session_tier: str = "market",
    equity: Optional[float] = None,
    account_id: str = "a1",
) -> PreflightResult:
    """
    Run all preflight checks and return a PreflightResult.
    Never raises. Always appends to data/status/preflight_log.jsonl.

    Args:
        caller: "run_cycle" | "run_options_cycle"
        session_tier: "market" | "extended" | "overnight" | "pre_market"
        equity: current account equity if already fetched (optional)
        account_id: "a1" | "a2"
    """
    result = PreflightResult(
        checked_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        caller=caller,
        session_tier=session_tier,
    )

    checks: list[CheckResult] = []

    # Run all checks
    checks.append(_check_config_present())
    checks.append(_check_pdt_floor(equity))
    checks.append(_check_operating_mode(account_id))
    checks.append(_check_watchlist_present())
    checks.append(_check_vix_halt())
    checks.append(_check_recent_halt_markers())
    checks.append(_check_feature_flags_loadable())
    checks.append(_check_data_dirs())
    checks.append(_check_strategy_config_keys())
    checks.append(_check_director_notes_shape())
    checks.append(_check_tier_sizing_vocabulary())

    result.checks = [asdict(c) for c in checks]
    verdict, blockers, warnings = _derive_verdict(checks)
    result.verdict = verdict
    result.blockers = blockers
    result.warnings = warnings

    _append_log(result)

    if verdict == "halt":
        log.error("[PREFLIGHT] verdict=halt  blockers=%s", blockers)
    elif verdict == "reconcile_only":
        log.warning("[PREFLIGHT] verdict=reconcile_only  blockers=%s", blockers)
    elif verdict == "go_degraded":
        log.warning("[PREFLIGHT] verdict=go_degraded  warnings=%s", warnings)
    else:
        log.debug("[PREFLIGHT] verdict=go")

    return result


def run_preflight_desync_check(
    mode_path: Path = _A1_MODE,
    preflight_verdict: str = "go",
) -> bool:
    """
    T-003 DESYNC final safety gate.

    Performs a synchronous fresh read of the divergence mode file at the last
    possible moment before a cycle proceeds. Catches mode changes that occur
    in the window between preflight's _check_operating_mode() call and actual
    cycle execution.

    Returns True  → mode is NORMAL (or file absent); cycle may proceed.
    Returns False → mode is non-NORMAL; caller must abort the cycle.
    Never raises.
    """
    try:
        if not mode_path.exists():
            return True
        data = json.loads(mode_path.read_text())
        mode = data.get("mode", "NORMAL").upper()
        if mode != "NORMAL":
            log.error(
                "[PREFLIGHT] SAFETY OVERRIDE: preflight=%s but a1_mode=%s — "
                "aborting cycle to prevent DESYNC",
                preflight_verdict,
                mode,
            )
            return False
        return True
    except Exception as exc:
        log.warning("[PREFLIGHT] DESYNC check failed (%s) — proceeding with caution", exc)
        return True


def is_go(result) -> bool:
    return result.verdict in ("go", "go_degraded")


def format_preflight_for_log(result) -> str:
    return (
        f"[PREFLIGHT] verdict={result.verdict} "
        f"passed={len(result.passed_checks)} "
        f"failed={len(result.failed_checks)} "
        f"warnings={len(result.warning_checks)} "
        f"session={result.session_tier}"
    )
