"""Runtime health monitor — 7 checks, alert routing, deduplication."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytz

log = logging.getLogger(__name__)

_ET = pytz.timezone("America/New_York")
_MARKET_OPEN = (9, 25)
_MARKET_CLOSE = (16, 5)

_DATA_DIR = Path("data")
_RUNTIME_DIR = _DATA_DIR / "runtime"
_STATE_FILE = _RUNTIME_DIR / "health_monitor_state.json"
_DEDUP_WINDOW_MIN = 30


@dataclass
class CheckResult:
    name: str
    ok: bool
    severity: str  # "CRITICAL" | "WARNING" | "OK"
    message: str
    details: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Market hours helper
# ---------------------------------------------------------------------------

def _is_market_hours(now_et: datetime) -> bool:
    if now_et.weekday() >= 5:
        return False
    t = (now_et.hour, now_et.minute)
    return _MARKET_OPEN <= t <= _MARKET_CLOSE


# ---------------------------------------------------------------------------
# Deduplication state
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text())
    except Exception:
        pass
    return {}


def _save_state(state: dict) -> None:
    try:
        _RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as exc:
        log.warning("[HEALTH] Could not save state: %s", exc)


def _should_alert(state: dict, check_key: str) -> bool:
    last = state.get(check_key)
    if last is None:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        return datetime.now(timezone.utc) - last_dt > timedelta(minutes=_DEDUP_WINDOW_MIN)
    except Exception:
        return True


def _mark_alerted(state: dict, check_key: str) -> None:
    state[check_key] = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Notification senders
# ---------------------------------------------------------------------------

def _send_whatsapp(message: str) -> None:
    from_num = os.environ.get("WHATSAPP_FROM", "")
    to_num = os.environ.get("WHATSAPP_TO", "")
    if not from_num or not to_num:
        log.warning("[HEALTH] WhatsApp env vars not set — skipping send")
        return
    try:
        from twilio.rest import Client as TwilioClient
        sid = os.environ["TWILIO_ACCOUNT_SID"]
        token = os.environ["TWILIO_AUTH_TOKEN"]
        client = TwilioClient(sid, token)
        client.messages.create(body=message, from_=from_num, to=to_num)
        log.info("[HEALTH] WhatsApp alert sent")
    except Exception as exc:
        log.error("[HEALTH] WhatsApp send failed: %s", exc)


def _send_email(subject: str, body: str) -> None:
    api_key = os.environ.get("SENDGRID_API_KEY", "")
    if not api_key:
        log.warning("[HEALTH] SENDGRID_API_KEY not set — skipping email")
        return
    try:
        import sendgrid
        from sendgrid.helpers.mail import Mail
        sg = sendgrid.SendGridAPIClient(api_key=api_key)
        msg = Mail(
            from_email=os.environ.get("ALERT_FROM_EMAIL", "alerts@bullbearbot.ai"),
            to_emails=os.environ.get("ALERT_TO_EMAIL", "eugene.gold@gmail.com"),
            subject=subject,
            plain_text_content=body,
        )
        sg.send(msg)
        log.info("[HEALTH] Email alert sent: %s", subject)
    except Exception as exc:
        log.error("[HEALTH] Email send failed: %s", exc)


def _dispatch_alert(result: CheckResult, state: dict, dry_run: bool = False) -> None:
    if result.ok:
        return
    key = f"alert_{result.name}"
    if not _should_alert(state, key):
        log.debug("[HEALTH] Dedup suppressed alert for %s", result.name)
        return

    msg = f"[BullBearBot {result.severity}] {result.name}: {result.message}"
    if dry_run:
        log.info("[HEALTH DRY-RUN] Would send alert: %s", msg)
        _mark_alerted(state, key)
        return

    _send_whatsapp(msg)
    if result.severity == "CRITICAL":
        _send_email(f"BullBearBot CRITICAL: {result.name}", f"{msg}\n\nDetails: {result.details}")
    _mark_alerted(state, key)


# ---------------------------------------------------------------------------
# Check 1 — A1 cycle freshness
# ---------------------------------------------------------------------------

def _check_a1_cycle(now_et: datetime) -> CheckResult:
    name = "a1_cycle"
    if not _is_market_hours(now_et):
        return CheckResult(name=name, ok=True, severity="OK", message="outside market hours")

    decisions_path = _DATA_DIR / "memory" / "decisions.json"
    try:
        if not decisions_path.exists():
            return CheckResult(name=name, ok=False, severity="CRITICAL",
                               message="decisions.json missing")
        records = json.loads(decisions_path.read_text())
        if not records:
            return CheckResult(name=name, ok=False, severity="CRITICAL",
                               message="decisions.json is empty")
        last_ts = records[-1].get("ts", "")
        last_dt = datetime.fromisoformat(last_ts)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        age_min = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
        if age_min > 15:
            return CheckResult(name=name, ok=False, severity="CRITICAL",
                               message=f"A1 last cycle {age_min:.1f} min ago (>15 min)",
                               details={"age_min": age_min, "last_ts": last_ts})
        return CheckResult(name=name, ok=True, severity="OK",
                           message=f"A1 cycle {age_min:.1f} min ago")
    except Exception as exc:
        return CheckResult(name=name, ok=False, severity="CRITICAL",
                           message=f"error reading decisions.json: {exc}")


# ---------------------------------------------------------------------------
# Check 2 — A2 cycle freshness
# ---------------------------------------------------------------------------

def _check_a2_cycle(now_et: datetime) -> CheckResult:
    name = "a2_cycle"
    if not _is_market_hours(now_et):
        return CheckResult(name=name, ok=True, severity="OK", message="outside market hours")

    preflight_path = _DATA_DIR / "status" / "preflight_log.jsonl"
    try:
        if not preflight_path.exists():
            return CheckResult(name=name, ok=False, severity="CRITICAL",
                               message="preflight_log.jsonl missing")
        lines = preflight_path.read_text().strip().splitlines()
        last_ts: str | None = None
        for line in reversed(lines):
            try:
                rec = json.loads(line)
                if rec.get("caller") == "run_options_cycle":
                    last_ts = rec.get("checked_at", "")
                    break
            except Exception:
                continue
        if last_ts is None:
            return CheckResult(name=name, ok=False, severity="CRITICAL",
                               message="no A2 cycle entries in preflight_log.jsonl")
        last_dt = datetime.fromisoformat(last_ts)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        age_min = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
        if age_min > 15:
            return CheckResult(name=name, ok=False, severity="CRITICAL",
                               message=f"A2 last cycle {age_min:.1f} min ago (>15 min)",
                               details={"age_min": age_min, "last_ts": last_ts})
        return CheckResult(name=name, ok=True, severity="OK",
                           message=f"A2 cycle {age_min:.1f} min ago")
    except Exception as exc:
        return CheckResult(name=name, ok=False, severity="CRITICAL",
                           message=f"error reading preflight_log.jsonl: {exc}")


# ---------------------------------------------------------------------------
# Check 3 — A2 fill rate
# ---------------------------------------------------------------------------

def _check_a2_fill_rate(now_et: datetime) -> CheckResult:
    name = "a2_fill_rate"
    if not _is_market_hours(now_et):
        return CheckResult(name=name, ok=True, severity="OK", message="outside market hours")

    api_key = os.environ.get("ALPACA_API_KEY_OPTIONS", "")
    secret_key = os.environ.get("ALPACA_SECRET_KEY_OPTIONS", "")
    if not api_key or not secret_key:
        return CheckResult(name=name, ok=True, severity="OK",
                           message="A2 Alpaca creds not configured — skipping")
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import OrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        client = TradingClient(api_key=api_key, secret_key=secret_key, paper=True)
        since = datetime.now(timezone.utc) - timedelta(hours=2)
        req = GetOrdersRequest(after=since.isoformat())
        orders = client.get_orders(req)
        submitted = [o for o in orders if o.status in (OrderStatus.NEW, OrderStatus.PENDING_NEW,
                                                        OrderStatus.ACCEPTED,
                                                        OrderStatus.PARTIALLY_FILLED)]
        filled = [o for o in orders if o.status == OrderStatus.FILLED]
        if len(submitted) > 3 and len(filled) == 0:
            return CheckResult(name=name, ok=False, severity="CRITICAL",
                               message=f"A2 fill rate: {len(submitted)} submitted, 0 filled in 2h",
                               details={"submitted": len(submitted), "filled": len(filled)})
        return CheckResult(name=name, ok=True, severity="OK",
                           message=f"A2 orders: {len(submitted)} submitted, {len(filled)} filled")
    except Exception as exc:
        return CheckResult(name=name, ok=False, severity="CRITICAL",
                           message=f"error checking A2 fill rate: {exc}")


# ---------------------------------------------------------------------------
# Check 4 — A1 churn
# ---------------------------------------------------------------------------

def _check_a1_churn(now_et: datetime) -> CheckResult:
    name = "a1_churn"
    if not _is_market_hours(now_et):
        return CheckResult(name=name, ok=True, severity="OK", message="outside market hours")

    decisions_path = _DATA_DIR / "memory" / "decisions.json"
    try:
        if not decisions_path.exists():
            return CheckResult(name=name, ok=True, severity="OK",
                               message="decisions.json missing — skipping churn check")
        records = json.loads(decisions_path.read_text())
        recent = records[-6:] if len(records) >= 6 else records
        buy_syms: set[str] = set()
        sell_syms: set[str] = set()
        for rec in recent:
            for action in rec.get("actions", []):
                sym = action.get("symbol", "")
                act = action.get("action", "")
                if act == "buy":
                    buy_syms.add(sym)
                elif act in ("sell", "close"):
                    sell_syms.add(sym)
        churned = buy_syms & sell_syms
        if churned:
            return CheckResult(name=name, ok=False, severity="CRITICAL",
                               message=f"A1 churn detected on {sorted(churned)}",
                               details={"churned_symbols": sorted(churned)})
        return CheckResult(name=name, ok=True, severity="OK", message="no A1 churn detected")
    except Exception as exc:
        return CheckResult(name=name, ok=False, severity="CRITICAL",
                           message=f"error checking A1 churn: {exc}")


# ---------------------------------------------------------------------------
# Check 5 — Operating modes (24/7)
# ---------------------------------------------------------------------------

def _check_modes() -> CheckResult:
    name = "modes"
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from divergence import OperatingMode, load_account_mode
        a1_mode = load_account_mode("A1")
        a2_mode = load_account_mode("A2")
        halted = []
        if a1_mode.mode == OperatingMode.HALTED:
            halted.append("A1")
        if a2_mode.mode == OperatingMode.HALTED:
            halted.append("A2")
        if halted:
            return CheckResult(name=name, ok=False, severity="CRITICAL",
                               message=f"{', '.join(halted)} in HALTED mode",
                               details={"a1_mode": str(a1_mode.mode), "a2_mode": str(a2_mode.mode)})
        return CheckResult(name=name, ok=True, severity="OK",
                           message=f"A1={a1_mode.mode.value} A2={a2_mode.mode.value}")
    except Exception as exc:
        return CheckResult(name=name, ok=False, severity="CRITICAL",
                           message=f"error checking operating modes: {exc}")


# ---------------------------------------------------------------------------
# Check 6 — Equity drawdown
# ---------------------------------------------------------------------------

def _check_equity_drawdown(now_et: datetime) -> CheckResult:
    name = "equity_drawdown"
    if not _is_market_hours(now_et):
        return CheckResult(name=name, ok=True, severity="OK", message="outside market hours")

    api_key = os.environ.get("ALPACA_API_KEY", "")
    secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
    if not api_key or not secret_key:
        return CheckResult(name=name, ok=True, severity="OK",
                           message="A1 Alpaca creds not configured — skipping")
    try:
        from alpaca.trading.client import TradingClient
        client = TradingClient(api_key=api_key, secret_key=secret_key, paper=True)
        account = client.get_account()
        last_equity = float(account.last_equity)
        equity = float(account.equity)
        if last_equity <= 0:
            return CheckResult(name=name, ok=True, severity="OK",
                               message="last_equity is 0 — skipping drawdown check")
        drawdown_pct = (equity - last_equity) / last_equity * 100
        if drawdown_pct < -3.0:
            return CheckResult(name=name, ok=False, severity="CRITICAL",
                               message=f"equity drawdown {drawdown_pct:.2f}% (< -3%)",
                               details={"equity": equity, "last_equity": last_equity,
                                        "drawdown_pct": drawdown_pct})
        return CheckResult(name=name, ok=True, severity="OK",
                           message=f"equity drawdown {drawdown_pct:.2f}%")
    except Exception as exc:
        return CheckResult(name=name, ok=False, severity="CRITICAL",
                           message=f"error checking equity drawdown: {exc}")


# ---------------------------------------------------------------------------
# Check 7 — A2 stuck structures
# ---------------------------------------------------------------------------

def _check_a2_stuck_structures(now_et: datetime) -> CheckResult:
    name = "a2_stuck_structures"
    structures_path = _DATA_DIR / "account2" / "positions" / "structures.json"
    try:
        if not structures_path.exists():
            return CheckResult(name=name, ok=True, severity="OK",
                               message="structures.json not found — skipping")
        records = json.loads(structures_path.read_text())
        stuck = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
        for rec in records:
            if not isinstance(rec, dict):
                continue
            if rec.get("lifecycle") != "SUBMITTED":
                continue
            opened_at = rec.get("opened_at", "")
            try:
                opened_dt = datetime.fromisoformat(opened_at)
                if opened_dt.tzinfo is None:
                    opened_dt = opened_dt.replace(tzinfo=timezone.utc)
                if opened_dt < cutoff:
                    stuck.append(rec.get("structure_id", "unknown"))
            except Exception:
                continue
        if stuck:
            return CheckResult(name=name, ok=False, severity="WARNING",
                               message=f"{len(stuck)} SUBMITTED structure(s) stuck >2h: {stuck}",
                               details={"stuck_ids": stuck})
        return CheckResult(name=name, ok=True, severity="OK",
                           message="no stuck SUBMITTED structures")
    except Exception as exc:
        return CheckResult(name=name, ok=False, severity="WARNING",
                           message=f"error checking stuck structures: {exc}")


# ---------------------------------------------------------------------------
# Check 8 — ChromaDB vector memory health
# ---------------------------------------------------------------------------

def _check_chromadb() -> CheckResult:
    name = "chromadb"
    try:
        import trade_memory as _tm
        short, medium, long_ = _tm._get_collections()
        if short is None:
            return CheckResult(
                name=name, ok=False, severity="CRITICAL",
                message="ChromaDB collections are None — vector memory disabled",
            )
        total = sum(
            c.count() for c in (short, medium, long_)
            if c is not None
        )
        return CheckResult(
            name=name, ok=True, severity="OK",
            message=f"ChromaDB OK — {total} records",
        )
    except Exception as exc:
        return CheckResult(
            name=name, ok=False, severity="CRITICAL",
            message=f"ChromaDB check failed: {exc}",
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _run_all_checks(now_et: datetime) -> list[CheckResult]:
    return [
        _check_a1_cycle(now_et),
        _check_a2_cycle(now_et),
        _check_a2_fill_rate(now_et),
        _check_a1_churn(now_et),
        _check_modes(),
        _check_equity_drawdown(now_et),
        _check_a2_stuck_structures(now_et),
        _check_chromadb(),
    ]


def run_health_checks(dry_run: bool = False) -> list[CheckResult]:
    """Run all health checks and dispatch alerts for failures."""
    now_et = datetime.now(_ET)
    results = _run_all_checks(now_et)
    state = _load_state()
    for result in results:
        if not result.ok:
            _dispatch_alert(result, state, dry_run=dry_run)
    _save_state(state)
    failed = [r for r in results if not r.ok]
    if failed:
        log.warning("[HEALTH] %d/%d checks failed: %s",
                    len(failed), len(results),
                    [r.name for r in failed])
    else:
        log.info("[HEALTH] All %d checks OK", len(results))
    return results


def get_health_status() -> dict:
    """Return health status dict for dashboard. No alerts dispatched."""
    now_et = datetime.now(_ET)
    results = _run_all_checks(now_et)
    checks = [
        {
            "name": r.name,
            "ok": r.ok,
            "severity": r.severity,
            "message": r.message,
            "details": r.details,
        }
        for r in results
    ]
    return {
        "all_ok": all(r.ok for r in results),
        "checks": checks,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="BullBearBot health monitor")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run all checks and log alerts without sending notifications")
    args = parser.parse_args()

    results = run_health_checks(dry_run=args.dry_run)
    print("\n--- Health Check Results ---")
    for r in results:
        status = "OK" if r.ok else r.severity
        print(f"  [{status:8s}] {r.name}: {r.message}")
    all_ok = all(r.ok for r in results)
    print(f"\nOverall: {'OK' if all_ok else 'FAILING'}")
    sys.exit(0 if all_ok else 1)
