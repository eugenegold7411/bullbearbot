"""
scheduler.py — 24/7 trading bot scheduler.

Session tiers (ET, Mon-Fri unless noted):
  market    9:30 AM –  8:00 PM   5-min cycles   stocks + ETFs + crypto
  extended  4:00 AM –  9:30 AM   15-min cycles  crypto only
            8:00 PM – 11:00 PM   15-min cycles  crypto only
  overnight 11:00 PM –  4:00 AM  30-min cycles  BTC/ETH only
  overnight all day Sat/Sun       30-min cycles  BTC/ETH only

Scheduled jobs:
  4:00 AM ET daily    — data_warehouse.py + scanner.py (pre-market prep)
  8:00 PM ET daily    — dynamic/intraday watchlist reset
  9:00 AM PST / noon ET — daily email report
  Sunday 6:00 AM ET   — weekly performance summary

Usage:
  python scheduler.py
  python scheduler.py --dry-run
"""

import argparse
import atexit
import json
import os
import queue
import signal
import threading
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import bot
import report as report_module
import weekly_review
from log_setup import get_logger

log = get_logger(__name__)
ET  = ZoneInfo("America/New_York")

SESSION_MARKET    = "market"
SESSION_EXTENDED  = "extended"
SESSION_OVERNIGHT = "overnight"

INTERVALS = {
    SESSION_MARKET:    5,
    SESSION_EXTENDED:  15,
    SESSION_OVERNIGHT: 30,
}

SESSION_INSTRUMENTS = {
    SESSION_MARKET:    "stocks, ETFs, crypto (BTC/USD, ETH/USD)",
    SESSION_EXTENDED:  "crypto only (BTC/USD, ETH/USD) — watching for overnight stock gaps",
    SESSION_OVERNIGHT: "BTC/USD and ETH/USD only",
}

_MARKET_START   =  9 * 60 + 30   # 9:30 AM ET
_MARKET_END     = 20 * 60         # 8:00 PM ET
_EXTENDED_START =  4 * 60         # 4:00 AM ET
_EXTENDED_END   = 23 * 60         # 11:00 PM ET



def _is_claude_trading_window(now_et: datetime | None = None,
                              cfg: dict | None = None) -> bool:
    """
    Thin wrapper — delegates to the canonical implementation in
    bot_stage3_decision.is_claude_trading_window().

    SQ-3: consolidation — one implementation, one update point.
    (bot_stage3_decision is the canonical module because it owns the public
    name and is re-exported through bot.py for all callers.)
    """
    from bot_stage3_decision import is_claude_trading_window as _canonical
    return _canonical(now_et=now_et, cfg=cfg)


def _load_strategy_config_safe() -> dict:
    """Read strategy_config.json; return {} on failure. Used by gate helpers."""
    try:
        cfg_path = Path(__file__).parent / "strategy_config.json"
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return {}

REPORT_HOUR_ET   = 16   # 4 PM ET
REPORT_MINUTE_ET = 30   # 4:30 PM ET — after market close, before extended session

_STATUS_DIR = Path("data/status")   # flag files for once-per-day jobs

# ── PID lockfile — prevent duplicate scheduler instances ─────────────────────

_PID_FILE = Path("data/runtime/scheduler.pid")


def _check_pid_lock(pid_path: Path = _PID_FILE) -> None:
    """
    Inspect an existing PID lockfile.
    - No file         → return (no prior instance).
    - Live PID found  → log CRITICAL and raise SystemExit(1).
    - Dead PID (stale)→ log WARNING, remove file, return.
    Never raises other than SystemExit.
    """
    if not pid_path.exists():
        return
    try:
        existing_pid = int(pid_path.read_text().strip())
    except (ValueError, OSError) as exc:
        log.warning("[PID] Unreadable lockfile at %s (%s) — treating as stale", pid_path, exc)
        pid_path.unlink(missing_ok=True)
        return
    try:
        os.kill(existing_pid, 0)   # signal 0 = liveness probe; no signal sent
        log.critical(
            "[PID] CRITICAL: scheduler already running as PID %d. "
            "Refusing to start a second instance (lockfile: %s). "
            "Stop the existing process or delete the file if it is stale.",
            existing_pid, pid_path,
        )
        raise SystemExit(1)
    except OSError:
        # os.kill raised → process is not running; stale lock
        log.warning(
            "[PID] Stale lockfile found (PID %d no longer running) — "
            "removing and continuing.",
            existing_pid,
        )
        pid_path.unlink(missing_ok=True)


def _acquire_pid_lock(pid_path: Path = _PID_FILE) -> None:
    """Check for live instance, then write our PID to the lockfile.
    Registers atexit cleanup so the lock is released on any normal exit."""
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    _check_pid_lock(pid_path)
    pid_path.write_text(str(os.getpid()))
    atexit.register(_release_pid_lock, pid_path)
    log.info("[PID] Lockfile written: %s (PID %d)", pid_path, os.getpid())


def _release_pid_lock(pid_path: Path = _PID_FILE) -> None:
    """Remove the lockfile on clean shutdown — only if it belongs to this process."""
    try:
        if pid_path.exists() and int(pid_path.read_text().strip()) == os.getpid():
            pid_path.unlink()
            log.info("[PID] Lockfile released: %s", pid_path)
    except Exception as exc:
        log.warning("[PID] Could not release lockfile: %s", exc)


def _handle_sigterm(signum, frame) -> None:
    """Convert SIGTERM to KeyboardInterrupt so the scheduler shuts down cleanly."""
    raise KeyboardInterrupt


def _ensure_account_modes_initialized() -> None:
    """Create a1_mode.json and a2_mode.json with NORMAL mode if absent. Idempotent.

    Mode files are gitignored and excluded from rsync, so they must be created
    at runtime. They are only written by divergence events during normal operation,
    meaning a fresh server or reboot leaves them absent — blocking preflight with
    reconcile_only. This guard runs once at scheduler startup to prevent that.
    """
    try:
        from datetime import timezone  # noqa: PLC0415

        from divergence import (  # noqa: PLC0415
            AccountMode,
            DivergenceScope,
            OperatingMode,
            get_mode_path,
            save_account_mode,
        )
        now_iso = datetime.now(timezone.utc).isoformat()
        for account in ("A1", "A2"):
            path = get_mode_path(account)
            if not path.exists():
                save_account_mode(AccountMode(
                    account=account,
                    mode=OperatingMode.NORMAL,
                    scope=DivergenceScope.ACCOUNT,
                    scope_id="",
                    reason_code="",
                    reason_detail=f"{account} mode file absent on startup — initialized to NORMAL",
                    entered_at=now_iso,
                    entered_by="system_init",
                    recovery_condition="one_clean_cycle",
                    last_checked_at=now_iso,
                ))
                log.info("[INIT] %s mode file created with NORMAL mode", account)

        # Ensure A2 decisions directory exists (gitignored, excluded from rsync).
        # persist_decision_record() also mkdir-on-writes, but creating it here
        # guarantees it is present before the first A2 cycle fires.
        try:
            from pathlib import Path as _Path  # noqa: PLC0415
            _decisions_dir = _Path(__file__).parent / "data" / "account2" / "decisions"
            _decisions_dir.mkdir(parents=True, exist_ok=True)
        except Exception as _de:
            log.warning("[INIT] Failed to pre-create A2 decisions directory (non-fatal): %s", _de)

    except Exception as exc:
        log.warning("[INIT] _ensure_account_modes_initialized failed (non-fatal): %s", exc)


# ── ORB formation tracking ────────────────────────────────────────────────────
_orb_high:   dict[str, float] = {}   # symbol → formation-window high
_orb_low:    dict[str, float] = {}   # symbol → formation-window low
_orb_date:   str              = ""
_orb_locked: bool             = False  # True after 9:45 AM — enables breakout entries


def get_session_and_interval(now_et=None) -> tuple[str, int]:
    """
    Returns (session_tier, interval_seconds) with fine-grained cadence.

    Pre-open:         9:28-9:30 AM  → ("pre_open",  60)
    ORB formation:    9:30-9:45 AM  → ("market",    90)
    Breakout window:  9:45-10:30 AM → ("market",   120)
    Normal session:   10:30-3:30 PM → ("market",   300)
    Closing window:   3:30-4:00 PM  → ("market",   120)
    Post-market:      4:00 PM-9:28 AM→("extended", 900)
    Overnight/Weekend:              → ("overnight",1800)
    """
    if now_et is None:
        now_et = datetime.now(ET)
    now_min = now_et.hour * 60 + now_et.minute
    weekday = now_et.weekday()

    if weekday >= 5:
        return SESSION_OVERNIGHT, 1800

    # Pre-open: 9:28-9:30 AM
    if 9 * 60 + 28 <= now_min < 9 * 60 + 30:
        return "pre_open", 60

    # Market hours
    if _MARKET_START <= now_min < _MARKET_END:
        if now_min < 9 * 60 + 45:     # ORB formation  9:30-9:45
            return SESSION_MARKET, 90
        if now_min < 10 * 60 + 30:    # Breakout window 9:45-10:30
            return SESSION_MARKET, 120
        if now_min < 15 * 60 + 30:    # Normal session 10:30-3:30
            return SESSION_MARKET, 300
        return SESSION_MARKET, 120    # Closing window 3:30-4:00 PM

    # Extended session: 4:00 AM-9:28 AM and 4:00 PM-11:00 PM
    if _EXTENDED_START <= now_min < _EXTENDED_END:
        return SESSION_EXTENDED, 900

    return SESSION_OVERNIGHT, 1800


# Keep get_session() for backward compat
def get_session() -> tuple[str, int]:
    session, interval_sec = get_session_and_interval()
    if session == "pre_open":
        return SESSION_MARKET, 1
    return session, interval_sec // 60


def _update_orb_range(current_prices: dict) -> None:
    """Track per-symbol ORB high/low during the 9:30-9:45 AM formation window."""
    global _orb_high, _orb_low, _orb_date, _orb_locked

    now_et  = datetime.now(ET)
    today   = now_et.strftime("%Y-%m-%d")
    now_min = now_et.hour * 60 + now_et.minute

    # Reset at start of new trading day
    if _orb_date != today:
        _orb_high   = {}
        _orb_low    = {}
        _orb_date   = today
        _orb_locked = False
        log.info("ORB levels reset for new trading day %s", today)

    # Lock after 9:45 AM
    if now_min >= 9 * 60 + 45 and not _orb_locked and _orb_high:
        _orb_locked = True
        summary = "  ".join(
            f"{s} H=${_orb_high[s]:.2f}/L=${_orb_low.get(s, 0):.2f}"
            for s in list(_orb_high)[:6]
        )
        log.info("ORB range locked: %d symbol(s) — %s", len(_orb_high), summary)

    # Track during formation window (per-symbol high/low)
    if _MARKET_START <= now_min < 9 * 60 + 45 and not _orb_locked:
        for sym, price in current_prices.items():
            if price and price > 0:
                if sym not in _orb_high or price > _orb_high[sym]:
                    _orb_high[sym] = round(price, 2)
                if sym not in _orb_low or price < _orb_low[sym]:
                    _orb_low[sym] = round(price, 2)


# ── Scheduled job trackers ────────────────────────────────────────────────────

_report_sent_date:             str = ""
_premarket_ran_date:           str = ""
_session_reset_done:           str = ""   # "YYYY-MM-DD" of last 8PM reset
_weekly_summary_date:          str = ""
_global_indices_refresh_key:   str = ""   # "YYYY-MM-DD-HH" of last refresh
_morning_brief_ran_date:       str = ""   # "YYYY-MM-DD" of last morning brief
_intelligence_brief_slots_ran: set = set()  # slot keys fired today
_reddit_refresh_key:           str = ""   # "YYYY-MM-DD-HH" of last Reddit refresh
_form4_refresh_key:            str = ""   # "YYYY-MM-DD-HH:mm/4" key for 4h refresh
_crypto_sentiment_refresh_key: str = ""   # "YYYY-MM-DD-N" key for 4h refresh
_flat_day_posted_date:         str = ""   # "YYYY-MM-DD" of last flat-day post
_lookback_posted_key:          str = ""   # "YYYY-MM-DD-DOW" of last lookback post
_engagement_update_date:       str = ""   # "YYYY-MM-DD" of last engagement update
_monthly_milestone_posted:     str = ""   # "YYYY-MM" of last milestone post
_macro_wire_refresh_key:       str = ""   # last macro wire refresh timestamp
_macro_intel_refresh_key:      str = ""   # "YYYY-MM-DD-HH" of last macro intel pre-fetch
_iv_refresh_ran_date:          str = ""   # "YYYY-MM-DD" of last IV history refresh
_orb_scan_ran_date:            str = ""   # "YYYY-MM-DD" of last ORB scan
_preopen_ran_date:             str = ""   # "YYYY-MM-DD" of last pre-open cycle
_daily_digest_written_date:    str = ""   # "YYYY-MM-DD" of last daily digest
_overnight_digest_written_date:str = ""   # "YYYY-MM-DD" of last 4 AM overnight digest
_eod_digest_written_date:      str = ""   # "YYYY-MM-DD" of last 4:15 PM EOD digest
_market_impact_backfill_date:  str = ""   # "YYYY-MM-DD" of last backfill
_outcomes_backfill_date:       str = ""   # "YYYY-MM-DD" of last outcomes backfill
_readiness_ran_date:           str = ""   # "YYYY-MM-DD" of last readiness check
_econ_calendar_refresh_key:    str = ""   # "YYYY-MM-DD-HHMM" slot key
_earnings_av_refresh_key:      str = ""   # ISO-week key — weekly AV calendar refresh
_earnings_rotation_ran_date:   str = ""   # "YYYY-MM-DD" of last rotation run
_earnings_cull_ran_date:       str = ""   # "YYYY-MM-DD" of last 2 AM cull
_earnings_stale_check_date:    str = ""   # "YYYY-MM-DD" of last staleness check
_earnings_intel_ran_date:      str = ""   # "YYYY-MM-DD" of last analyst intel refresh
_zero_fill_alert_date:         str = ""   # "YYYY-MM-DD" of last zero-fill alert
_last_qualitative_sweep_key:   str = ""   # "YYYY-MM-DD-HH" of last L1 sweep (hourly slot)
_last_qualitative_news_hash:   str = ""   # news hash at last L1 sweep, for event-driven refresh
_qualitative_sweep_running:    bool = False  # guard against concurrent sweeps
_qualitative_thread = None                  # threading.Thread for the background sweep
_momentum_cfg: dict | None = None           # cached momentum_trigger config
_momentum_last_fired: dict[str, float] = {}  # sym → monotonic ts of last momentum trigger


# ── Event-driven cycle trigger queue ─────────────────────────────────────────
# Any module can call trigger_cycle(reason) to request an immediate out-of-
# schedule cycle. The scheduler drains this queue after each sleep interval.

_trigger_queue: queue.Queue = queue.Queue()
_last_cycle_end_time: float = 0.0   # monotonic timestamp — enforces 60s cooldown


def trigger_cycle(reason: str) -> None:
    """
    Request an immediate out-of-schedule trading cycle.
    Thread-safe. Safe to call from any module.
    Reasons are aggregated — multiple calls during one sleep window fire
    a single cycle with all reasons combined.
    """
    _trigger_queue.put(reason)
    log.debug("[TRIGGER] queued: %s", reason)


def _today() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def _maybe_check_api_costs() -> None:
    """Check API cost thresholds each cycle. Sends Twilio SMS once per day if exceeded."""
    try:
        from cost_tracker import get_tracker
        alert = get_tracker().should_alert()
        if alert:
            log.warning("API cost alert: %s", alert)
            try:
                import os

                from twilio.rest import Client
                sid   = os.getenv("TWILIO_ACCOUNT_SID")
                token = os.getenv("TWILIO_AUTH_TOKEN")
                from_ = os.getenv("TWILIO_FROM_NUMBER")
                to    = os.getenv("TWILIO_TO_NUMBER")
                if all([sid, token, from_, to]):
                    Client(sid, token).messages.create(
                        body=f"TRADING BOT: {alert}", from_=from_, to=to
                    )
            except Exception as sms_exc:
                log.debug("Cost alert SMS failed (non-fatal): %s", sms_exc)
    except Exception:
        pass  # cost_tracker is optional — never crash the scheduler


def _maybe_send_daily_report() -> None:
    """Send the daily report once at 4:30 PM ET on market days.

    Uses a flag file at data/status/daily_report_sent_YYYY-MM-DD.flag so the
    report is not re-sent if the scheduler restarts after market close.
    """
    global _report_sent_date
    now_et  = datetime.now(ET)
    today   = _today()
    weekday = now_et.weekday()

    if weekday >= 5:   # skip weekends
        return

    # In-process guard (fast path)
    if _report_sent_date == today:
        return

    # Persistent guard — survives scheduler restarts
    flag_file = _STATUS_DIR / f"daily_report_sent_{today}.flag"
    if flag_file.exists():
        _report_sent_date = today
        return

    now_min = now_et.hour * 60 + now_et.minute
    if now_min < REPORT_HOUR_ET * 60 + REPORT_MINUTE_ET:
        return

    log.info("Sending daily report for %s", today)
    try:
        report_module.send_report_email(target_date=date.fromisoformat(today))
        _report_sent_date = today
        _STATUS_DIR.mkdir(parents=True, exist_ok=True)
        flag_file.touch()
    except Exception:
        log.error("Daily report failed", exc_info=True)


def _maybe_send_zero_fill_alert(dry_run: bool = False) -> None:
    """At 11:00 AM ET on market days, alert if no order fills have occurred since open.

    Fires at most once per day. Uses a flag file to survive restarts.
    Market must have been open 90+ minutes before this can trigger.
    """
    global _zero_fill_alert_date
    now_et  = datetime.now(ET)
    today   = _today()
    weekday = now_et.weekday()
    now_min = now_et.hour * 60 + now_et.minute

    if weekday >= 5:   # no alert on weekends
        return
    # Only fire between 11:00 AM and 12:00 PM ET
    if now_min < 11 * 60 or now_min >= 12 * 60:
        return
    # Market must have been open 90+ min (9:30 + 90 = 11:00 AM exactly)
    if now_min < _MARKET_START + 90:
        return

    if _zero_fill_alert_date == today:
        return

    flag_file = _STATUS_DIR / f"zero_fill_alert_sent_{today}.flag"
    if flag_file.exists():
        _zero_fill_alert_date = today
        return

    if dry_run:
        log.info("[dry-run] Skipping zero-fill alert check")
        return

    try:
        trade_log = Path("logs/trades.jsonl")
        fills_today = 0
        if trade_log.exists():
            for line in trade_log.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("status") == "submitted" and rec.get("ts", "").startswith(today):
                        fills_today += 1
                except Exception:
                    continue

        if fills_today > 0:
            return   # fills recorded — no alert needed

        log.warning("[ZERO_FILL] No fills by 11 AM ET on %s — sending alert", today)
        _zero_fill_alert_date = today
        _STATUS_DIR.mkdir(parents=True, exist_ok=True)
        flag_file.touch()

        # Build alert body
        equity_str = "(unavailable)"
        pos_html   = "<li>(unavailable)</li>"
        try:
            acct = report_module._get_account()
            if acct:
                equity_str = f"${float(acct.equity):,.0f}"
            positions  = report_module._get_positions()
            pos_html   = "".join(
                f"<li>{p.symbol}: {p.qty} shares, value ${float(p.market_value):,.0f}</li>"
                for p in positions
            ) or "<li>(no open positions)</li>"
        except Exception:
            pass

        last5_html = "(unavailable)"
        try:
            log_path = Path("logs/bot.log")
            if log_path.exists():
                lines = log_path.read_text().splitlines()
                last5_html = "<br>".join(
                    ln.replace("&", "&amp;").replace("<", "&lt;") for ln in lines[-5:]
                )
        except Exception:
            pass

        body = (
            "<html><body style='font-family:Arial,sans-serif;max-width:700px'>"
            "<h2 style='color:#cc6600'>[BullBearBot] Zero Fills by 11 AM ET</h2>"
            "<p>No order submissions recorded today by 11:00 AM ET. "
            "The market has been open for 90+ minutes with no fills.</p>"
            "<table style='border-collapse:collapse;width:100%'>"
            f"<tr><td style='padding:4px 8px'><b>Date</b></td><td>{today}</td></tr>"
            f"<tr><td style='padding:4px 8px'><b>Account Equity</b></td>"
            f"<td>{equity_str}</td></tr>"
            "</table>"
            f"<h3>Open Positions</h3><ul>{pos_html}</ul>"
            "<h3>Last 5 Bot Log Lines</h3>"
            f"<pre style='background:#f5f5f5;padding:12px;font-size:11px'>{last5_html}</pre>"
            "<p>Review <code>logs/bot.log</code> to confirm signal scoring and "
            "Stage 3 decisions are running normally.</p>"
            "</body></html>"
        )
        report_module.send_alert_email(
            f"[BullBearBot] ALERT: Zero fills by 11 AM — {today}",
            body,
        )
    except Exception as exc:
        log.warning("[ZERO_FILL] Alert check failed (non-fatal): %s", exc)


def _maybe_refresh_macro_intelligence(dry_run: bool = False) -> None:
    """Pre-fetch rates, commodities, and credit at 4 AM ET so first cycle has warm cache."""
    global _macro_intel_refresh_key
    now_et  = datetime.now(ET)
    weekday = now_et.weekday()
    now_min = now_et.hour * 60 + now_et.minute
    key     = now_et.strftime("%Y-%m-%d-%H")

    if _macro_intel_refresh_key == key:
        return
    if weekday >= 5:   # skip weekends
        return
    if now_min < 4 * 60 or now_min > 5 * 60 + 30:   # 4:00–5:30 AM window
        return

    if not dry_run:
        try:
            import macro_intelligence as _mi  # noqa: PLC0415
            _mi.fetch_rates_snapshot()
            _mi.fetch_commodities_snapshot()
            _mi.fetch_credit_snapshot()
            log.info("Macro intelligence pre-fetch complete (rates, commodities, credit)")
        except Exception:
            log.debug("Macro intelligence pre-fetch failed (non-fatal)", exc_info=True)
    else:
        log.info("[dry-run] Skipping macro intelligence pre-fetch")

    _macro_intel_refresh_key = key


def _maybe_refresh_earnings_intel(dry_run: bool = False) -> None:
    """Refresh per-symbol analyst intel cache at 4:00–5:30 AM ET on weekdays.
    Runs once per day (daily guard). Feeds morning brief and market_data
    with beat history + analyst consensus before the first market cycle.
    Non-fatal — never blocks the scheduler loop.
    """
    global _earnings_intel_ran_date
    now_et  = datetime.now(ET)
    weekday = now_et.weekday()
    today   = now_et.strftime("%Y-%m-%d")
    now_min = now_et.hour * 60 + now_et.minute

    if _earnings_intel_ran_date == today:
        return
    if weekday >= 5:
        return
    if now_min < 4 * 60 or now_min > 5 * 60 + 30:   # 4:00–5:30 AM window
        return

    if not dry_run:
        try:
            import earnings_intel_fetcher as _eif  # noqa: PLC0415
            import watchlist_manager as _wm  # noqa: PLC0415
            wl   = _wm.get_active_watchlist()
            syms = [s["symbol"] for s in wl.get("all", [])]
            _eif.refresh_earnings_analyst_intel(syms)
            log.info("Earnings analyst intel cache refreshed (%d symbols)", len(syms))
        except Exception:
            log.debug("Earnings intel refresh failed (non-fatal)", exc_info=True)
    else:
        log.info("[dry-run] Skipping earnings analyst intel refresh")

    _earnings_intel_ran_date = today


def _maybe_refresh_iv_history(dry_run: bool = False) -> None:
    """Refresh IV history for all equity symbols at 4:00–5:30 AM ET on weekdays.
    Fetches options chains and updates IV history files used by Account 2.
    Non-fatal — never blocks Account 1 or the scheduler loop.
    """
    global _iv_refresh_ran_date
    now_et  = datetime.now(ET)
    weekday = now_et.weekday()
    now_min = now_et.hour * 60 + now_et.minute
    today   = now_et.strftime("%Y-%m-%d")

    if _iv_refresh_ran_date == today:
        return
    if weekday >= 5:   # skip weekends
        return
    if now_min < 4 * 60 or now_min > 5 * 60 + 30:   # 4:00–5:30 AM window
        return

    if not dry_run:
        try:
            import options_data as _od  # noqa: PLC0415
            import watchlist_manager as _wm  # noqa: PLC0415
            wl = _wm.get_active_watchlist()
            equity_symbols = wl.get("stocks", []) + wl.get("etfs", [])
            if equity_symbols:
                _od.refresh_all_iv_data(equity_symbols)
                log.info("[IV] History refresh complete: %d equity symbols", len(equity_symbols))
            else:
                log.debug("[IV] No equity symbols in watchlist — skipping IV refresh")
        except Exception:
            log.debug("IV history refresh failed (non-fatal)", exc_info=True)
    else:
        log.info("[dry-run] Skipping IV history refresh")

    _iv_refresh_ran_date = today


def _maybe_refresh_economic_calendar(dry_run: bool = False) -> None:
    """
    Refresh Finnhub economic calendar at key intraday windows on weekdays:
      4:00 AM ET  — morning data load (existing behavior)
      8:35 AM ET  — captures 8:30 AM releases (PPI, CPI, NFP, retail sales)
      10:05 AM ET — captures 10:00 AM releases (ISM, JOLTS, consumer confidence)
      2:05 PM ET  — captures afternoon Fed releases

    Uses slot_key = "YYYY-MM-DD-HHMM" so only the first cycle inside each
    window triggers a refresh. Non-fatal: keeps existing cache on Finnhub failure.
    Fires trigger_cycle() when new actuals are detected.
    """
    global _econ_calendar_refresh_key
    now_et  = datetime.now(ET)
    weekday = now_et.weekday()
    now_min = now_et.hour * 60 + now_et.minute

    if weekday >= 5:   # skip weekends
        return

    # Determine which slot we're in (None = no active slot)
    slot_key: str | None = None
    if 4 * 60 <= now_min < 5 * 60 + 30:            # 4:00–5:30 AM
        slot_key = now_et.strftime("%Y-%m-%d") + "-0400"
    elif 8 * 60 + 35 <= now_min < 8 * 60 + 45:     # 8:35–8:44 AM
        slot_key = now_et.strftime("%Y-%m-%d") + "-0835"
    elif 10 * 60 + 5 <= now_min < 10 * 60 + 15:    # 10:05–10:14 AM
        slot_key = now_et.strftime("%Y-%m-%d") + "-1005"
    elif 14 * 60 + 5 <= now_min < 14 * 60 + 15:    # 2:05–2:14 PM
        slot_key = now_et.strftime("%Y-%m-%d") + "-1405"

    if slot_key is None:
        return
    if _econ_calendar_refresh_key == slot_key:
        return

    if dry_run:
        log.info("[dry-run] Skipping economic calendar refresh (slot=%s)", slot_key)
        _econ_calendar_refresh_key = slot_key
        return

    try:
        import json as _json  # noqa: PLC0415
        from pathlib import Path as _Path  # noqa: PLC0415

        import data_warehouse as _dw  # noqa: PLC0415

        # Snapshot previous actuals before refreshing
        prev_actuals: dict[str, object] = {}
        cal_path = _Path(_dw.MARKET_DIR) / "economic_calendar.json"
        if cal_path.exists():
            try:
                _prev = _json.loads(cal_path.read_text())
                for ev in _prev.get("events", []):
                    name = ev.get("event", "")
                    if name:
                        prev_actuals[name] = ev.get("actual")
            except Exception:
                pass

        # Run the refresh
        _dw.refresh_economic_calendar_finnhub()
        log.info("[ECON] Calendar refreshed (slot=%s)", slot_key)
        _econ_calendar_refresh_key = slot_key

        # Detect newly-populated actuals and fire triggers
        if cal_path.exists():
            try:
                _new = _json.loads(cal_path.read_text())
                for ev in _new.get("events", []):
                    name     = ev.get("event", "")
                    actual   = ev.get("actual")
                    estimate = ev.get("estimate")
                    if name and actual is not None and prev_actuals.get(name) is None:
                        log.info(
                            "[ECON] %s: actual=%s vs estimate=%s — PRINTED",
                            name, actual, estimate,
                        )
                        trigger_cycle(
                            f"econ print: {name} actual={actual} estimate={estimate}"
                        )
            except Exception as _cmp_exc:
                log.debug("[ECON] actual comparison failed (non-fatal): %s", _cmp_exc)

    except Exception as exc:
        log.warning("[ECON] calendar refresh failed — will retry next eligible slot: %s", exc)


def _maybe_run_premarket_jobs(dry_run: bool = False) -> None:
    """Run data warehouse refresh + pre-market scanner at 4 AM ET."""
    global _premarket_ran_date
    now_et  = datetime.now(ET)
    today   = _today()
    now_min = now_et.hour * 60 + now_et.minute
    weekday = now_et.weekday()

    if _premarket_ran_date == today:
        return
    if weekday >= 5:   # skip weekends
        return
    if now_min < 4 * 60 or now_min > 5 * 60:   # only run between 4-5 AM
        return

    log.info("Running pre-market jobs (data warehouse + scanner)")
    _warehouse_ok = False
    if not dry_run:
        try:
            import data_warehouse
            data_warehouse.run_full_refresh()
            log.info("Data warehouse refresh complete")
            _warehouse_ok = True
        except Exception:
            log.error("Data warehouse failed — will retry in next eligible cycle", exc_info=True)

        try:
            import scanner
            scanner.run_scan()
            log.info("Pre-market scanner complete")
        except Exception:
            log.error("Scanner failed", exc_info=True)

        # ChromaDB tier-promotion maintenance — once per day at premarket so
        # short→medium (>7d) and medium→long (>90d) promotions happen
        # regardless of save cadence. Lazy per-save promotion in trade_memory
        # remains as belt-and-suspenders.
        try:
            import trade_memory  # noqa: PLC0415
            trade_memory.run_promotion_maintenance()
        except Exception:
            log.warning("Tier promotion maintenance failed (non-fatal)", exc_info=True)
    else:
        log.info("[dry-run] Skipping data warehouse + scanner")
        _warehouse_ok = True

    if _warehouse_ok:
        _premarket_ran_date = today


def _maybe_reset_session_watchlist() -> None:
    """Reset dynamic/intraday watchlist tiers at 8 PM ET."""
    global _session_reset_done
    now_et  = datetime.now(ET)
    today   = _today()
    now_min = now_et.hour * 60 + now_et.minute

    if _session_reset_done == today:
        return
    if now_min >= 20 * 60:   # 8 PM ET
        try:
            import watchlist_manager as wm
            wm.reset_session_tiers()
            _session_reset_done = today
            log.info("8 PM session reset: dynamic/intraday watchlist cleared")
        except Exception as exc:
            log.warning("Session watchlist reset failed (non-fatal): %s", exc, exc_info=True)


def _maybe_refresh_global_indices(dry_run: bool = False) -> None:
    """Refresh global indices at market-open (9:30 AM ET) and extended-session-start (4:00 AM ET).
    Each trigger window fires once per hour-slot to avoid duplicate runs."""
    global _global_indices_refresh_key
    now_et  = datetime.now(ET)
    now_min = now_et.hour * 60 + now_et.minute
    weekday = now_et.weekday()

    # Only refresh on weekdays at 4:00–4:59 AM or 9:30–10:29 AM ET
    is_extended_start = (weekday < 5 and  4 * 60 <= now_min < 5 * 60)
    is_market_open    = (weekday < 5 and  9 * 60 + 30 <= now_min < 10 * 60 + 30)
    if not (is_extended_start or is_market_open):
        return

    # Deduplicate: key = "YYYY-MM-DD-HH" so we fire at most once per hour
    slot_key = now_et.strftime("%Y-%m-%d-%H")
    if _global_indices_refresh_key == slot_key:
        return

    log.info("Refreshing global indices (session start)")
    if not dry_run:
        try:
            import data_warehouse
            data_warehouse.refresh_global_indices()
            _global_indices_refresh_key = slot_key
        except Exception:
            log.error("Global indices refresh failed — will retry next eligible slot", exc_info=True)
    else:
        _global_indices_refresh_key = slot_key


def _maybe_run_morning_brief(dry_run: bool = False) -> None:
    """Generate morning conviction brief at 4:15 AM ET on weekdays."""
    global _morning_brief_ran_date
    now_et  = datetime.now(ET)
    today   = _today()
    now_min = now_et.hour * 60 + now_et.minute
    weekday = now_et.weekday()

    if _morning_brief_ran_date == today:
        return
    if weekday >= 5:
        return
    if now_min < 4 * 60 + 15 or now_min > 5 * 60 + 30:  # 4:15–5:30 AM window
        return

    log.info("Generating morning conviction brief")
    brief = {}
    if not dry_run:
        try:
            from morning_brief import generate_morning_brief  # noqa: PLC0415
            brief = generate_morning_brief()
            _brief_summary = brief.get("brief_summary", "")
            if "failed" in _brief_summary.lower():
                log.warning("[MORNING] Brief saved with failure content: %s", _brief_summary[:120])
            else:
                log.info("Morning brief complete — tone=%s  picks=%d",
                         brief.get("market_tone"), len(brief.get("conviction_picks", [])))
        except Exception as exc:
            log.error("[MORNING] Morning brief generation failed — %s", exc, exc_info=True)

        # Publish premarket brief to Twitter
        try:
            from trade_publisher import TradePublisher  # noqa: PLC0415
            _pub = TradePublisher()
            if _pub.enabled and brief:
                _pub.publish_premarket_brief(brief)
        except Exception:
            log.debug("Premarket brief tweet failed (non-fatal)", exc_info=True)
    else:
        log.info("[dry-run] Skipping morning brief")

    _morning_brief_ran_date = today


def _maybe_run_intelligence_brief(dry_run: bool = False) -> None:
    """Generate intelligence brief at scheduled slots:
    - 4:00 AM ET: premarket
    - 9:25 AM ET: market_open
    - Hourly intraday: 10:30, 11:30, 12:30, 1:30, 2:30, 3:30 PM ET
    """
    global _intelligence_brief_slots_ran
    now_et  = datetime.now(ET)
    today   = _today()
    now_min = now_et.hour * 60 + now_et.minute
    weekday = now_et.weekday()

    if weekday >= 5:
        return

    # Reset slot tracking at midnight
    slot_day = now_et.strftime("%Y-%m-%d")

    # Determine which slot applies now
    # Slot windows (each slot fires within a 3-minute window)
    slots: list[tuple[int, int, str]] = [
        (4 * 60,      4 * 60 + 15,  "premarket"),       # 4:00–4:15 AM
        (9 * 60 + 25, 9 * 60 + 35,  "market_open"),     # 9:25–9:35 AM
        (10 * 60 + 30, 10 * 60 + 40, "intraday_update"), # 10:30–10:40 AM
        (11 * 60 + 30, 11 * 60 + 40, "intraday_update"), # 11:30–11:40 AM
        (12 * 60 + 30, 12 * 60 + 40, "intraday_update"), # 12:30–12:40 PM
        (13 * 60 + 30, 13 * 60 + 40, "intraday_update"), # 1:30–1:40 PM
        (14 * 60 + 30, 14 * 60 + 40, "intraday_update"), # 2:30–2:40 PM
        (15 * 60 + 30, 15 * 60 + 40, "intraday_update"), # 3:30–3:40 PM
    ]

    for (start_min, end_min, brief_type) in slots:
        if not (start_min <= now_min < end_min):
            continue
        # Build a unique key per slot per day
        slot_key = f"{slot_day}-{start_min}"
        if slot_key in _intelligence_brief_slots_ran:
            return  # already ran this slot

        if not dry_run:
            try:
                from morning_brief import generate_intelligence_brief  # noqa: PLC0415
                brief = generate_intelligence_brief(brief_type=brief_type)
                log.info("[INTELLIGENCE] %s brief generated — regime=%s longs=%d",
                         brief_type,
                         brief.get("market_regime", {}).get("regime", "?"),
                         len(brief.get("high_conviction_longs", [])))

                # For premarket brief, also publish to Twitter
                if brief_type == "premarket":
                    try:
                        from trade_publisher import TradePublisher  # noqa: PLC0415
                        _pub = TradePublisher()
                        if _pub.enabled and brief:
                            # Build legacy-compatible brief for publisher
                            legacy_brief = {
                                "market_tone": brief.get("market_regime", {}).get("regime", "neutral"),
                                "conviction_picks": [
                                    {"symbol": p.get("symbol"), "direction": "long",
                                     "catalyst": {"short_text": p.get("catalyst", "")[:80]},
                                     "stop": p.get("stop"), "target": p.get("target")}
                                    for p in brief.get("high_conviction_longs", [])[:3]
                                ],
                                "brief_summary": brief.get("market_regime", {}).get("tone", ""),
                            }
                            _pub.publish_premarket_brief(legacy_brief)
                    except Exception:
                        log.debug("[INTELLIGENCE] Premarket tweet failed (non-fatal)", exc_info=True)

            except Exception as exc:
                log.error("[INTELLIGENCE] Brief generation failed: %s", exc, exc_info=True)
        else:
            log.info("[dry-run] Skipping intelligence brief slot=%s type=%s", slot_key, brief_type)

        _intelligence_brief_slots_ran.add(slot_key)
        return  # only fire one slot per call


def _maybe_refresh_reddit_sentiment(dry_run: bool = False) -> None:
    """Refresh Reddit sentiment once per hour during market + extended session hours."""
    global _reddit_refresh_key
    now_et  = datetime.now(ET)
    now_min = now_et.hour * 60 + now_et.minute
    weekday = now_et.weekday()

    # Only run during weekdays in extended/market sessions (4 AM – 11 PM)
    if weekday >= 5 or not (4 * 60 <= now_min < 23 * 60):
        return

    slot_key = now_et.strftime("%Y-%m-%d-%H")
    if _reddit_refresh_key == slot_key:
        return

    if not dry_run:
        try:
            import watchlist_manager as wm  # noqa: PLC0415
            from reddit_sentiment import fetch_reddit_sentiment  # noqa: PLC0415
            wl   = wm.get_active_watchlist()
            syms = wl["stocks"] + wl["etfs"]
            fetch_reddit_sentiment(syms)
            log.info("Reddit sentiment refresh complete")
        except Exception:
            log.debug("Reddit sentiment refresh failed (non-fatal)", exc_info=True)

    _reddit_refresh_key = slot_key


def _maybe_refresh_earnings_calendar_av(dry_run: bool = False) -> None:
    """
    Weekly AV earnings calendar refresh.
    Fires Sundays 5:00–6:00 AM ET, once per ISO week.
    Non-fatal: failure leaves the existing file in place.
    """
    global _earnings_av_refresh_key
    now_et  = datetime.now(ET)
    now_min = now_et.hour * 60 + now_et.minute
    weekday = now_et.weekday()  # Mon=0 ... Sun=6

    if weekday != 6:                                  # Sunday only
        return
    if not (5 * 60 <= now_min <= 6 * 60):             # 5:00–6:00 AM ET
        return

    iso_year, iso_week, _ = now_et.isocalendar()
    key = f"{iso_year}-W{iso_week:02d}"
    if _earnings_av_refresh_key == key:
        return

    if dry_run:
        log.info("[dry-run] Skipping AV earnings refresh (slot=%s)", key)
        _earnings_av_refresh_key = key
        return

    try:
        import data_warehouse as dw  # noqa: PLC0415
        result = dw.refresh_earnings_calendar_av()
        if result:
            n = len(result.get("calendar", []))
            log.info("[EARNINGS_AV] weekly refresh complete (entries=%d, week=%s)", n, key)
            _earnings_av_refresh_key = key
        else:
            log.warning("[EARNINGS_AV] weekly refresh returned empty — will retry next eligible cycle")
    except Exception as exc:
        log.warning("[EARNINGS_AV] weekly refresh failed (non-fatal): %s", exc)


def _maybe_run_earnings_rotation(dry_run: bool = False) -> None:
    """
    Daily rotation run — 4:15–5:00 AM ET weekdays, after premarket jobs.
    Skips when AV calendar is not the current source (don't run on stale data).
    """
    global _earnings_rotation_ran_date
    now_et  = datetime.now(ET)
    now_min = now_et.hour * 60 + now_et.minute
    weekday = now_et.weekday()
    today   = _today()

    if _earnings_rotation_ran_date == today:
        return
    if weekday >= 5:
        return
    if not (4 * 60 + 15 <= now_min <= 5 * 60):
        return

    if dry_run:
        log.info("[dry-run] Skipping earnings rotation")
        _earnings_rotation_ran_date = today
        return

    # Guard: only run when AV calendar is canonical
    try:
        import data_warehouse as dw  # noqa: PLC0415
        cal = dw.load_earnings_calendar()
        if cal.get("source") != "alphavantage":
            log.warning(
                "[ROTATION] skipping run — earnings_calendar source=%s (expected alphavantage)",
                cal.get("source"),
            )
            return
    except Exception:
        pass

    try:
        import earnings_rotation as er  # noqa: PLC0415
        result = er.run_earnings_rotation()
        log.info("[ROTATION] daily run complete: added=%s size_after=%d",
                 result.get("added", []), result.get("watchlist_size_after", 0))
        # Short-horizon pass: add _EXTRA_UNIVERSE names with earnings within 5 days
        try:
            expanded = er.expand_watchlist_for_upcoming_earnings(days_ahead=5)
            if expanded:
                log.info("[ROTATION] short-horizon expansion: %s", expanded)
        except Exception as exc2:
            log.warning("[ROTATION] expand_watchlist failed (non-fatal): %s", exc2)
        _earnings_rotation_ran_date = today
    except Exception as exc:
        log.warning("[ROTATION] daily run failed (non-fatal): %s", exc)


def _maybe_cull_post_earnings(dry_run: bool = False) -> None:
    """
    Nightly cull of post-earnings rotation symbols.
    Fires weekdays 2:00–3:00 AM ET only (per spec).
    """
    global _earnings_cull_ran_date
    now_et  = datetime.now(ET)
    now_min = now_et.hour * 60 + now_et.minute
    weekday = now_et.weekday()
    today   = _today()

    if _earnings_cull_ran_date == today:
        return
    if weekday >= 5:
        return
    if not (2 * 60 <= now_min <= 3 * 60):
        return

    if dry_run:
        log.info("[dry-run] Skipping post-earnings cull")
        _earnings_cull_ran_date = today
        return

    try:
        import earnings_rotation as er  # noqa: PLC0415
        culled = er._cull_post_earnings_symbols()
        log.info("[ROTATION] 2 AM cull complete: %d symbols culled", len(culled))
        _earnings_cull_ran_date = today
    except Exception as exc:
        log.warning("[ROTATION] 2 AM cull failed (non-fatal): %s", exc)


def _maybe_check_earnings_calendar_staleness(dry_run: bool = False) -> None:
    """
    Daily earnings calendar staleness check.
    Fires 8:00–9:00 AM ET, once per day. Surfaces in logs as [EARNINGS_STALE].
    """
    global _earnings_stale_check_date
    now_et  = datetime.now(ET)
    now_min = now_et.hour * 60 + now_et.minute
    today   = _today()

    if _earnings_stale_check_date == today:
        return
    if not (8 * 60 <= now_min <= 9 * 60):
        return

    if dry_run:
        _earnings_stale_check_date = today
        return

    try:
        import data_warehouse as dw  # noqa: PLC0415
        status = dw._check_earnings_calendar_staleness()
        if status == "ok":
            log.info("[EARNINGS_STALE] calendar fresh")
        _earnings_stale_check_date = today
    except Exception as exc:
        log.debug("[EARNINGS_STALE] check failed (non-fatal): %s", exc)


def _maybe_refresh_form4_trades(dry_run: bool = False) -> None:
    """Refresh SEC Form 4 insider trades every 4 hours during weekdays."""
    global _form4_refresh_key
    now_et  = datetime.now(ET)
    now_et.hour * 60 + now_et.minute
    weekday = now_et.weekday()

    if weekday >= 5:
        return

    # Key = "YYYY-MM-DD-N" where N = hour // 4  (refreshes at 0,4,8,12,16,20)
    slot_key = now_et.strftime("%Y-%m-%d-") + str(now_et.hour // 4)
    if _form4_refresh_key == slot_key:
        return

    if not dry_run:
        try:
            import watchlist_manager as wm  # noqa: PLC0415
            from insider_intelligence import fetch_form4_insider_trades  # noqa: PLC0415
            wl   = wm.get_active_watchlist()
            syms = [s for s in wl["stocks"] + wl["etfs"] if "/" not in s]
            fetch_form4_insider_trades(syms, days_back=30)
            log.info("Form 4 insider trades refresh complete")
        except Exception:
            log.debug("Form 4 refresh failed (non-fatal)", exc_info=True)

    _form4_refresh_key = slot_key



def _maybe_refresh_crypto_sentiment(dry_run: bool = False) -> None:
    """Refresh crypto Fear & Greed + BTC dominance every 4 hours. 24/7 - never pauses."""
    global _crypto_sentiment_refresh_key
    now_et   = datetime.now(ET)

    # Key = "YYYY-MM-DD-N" where N = hour // 4  (refreshes at 0,4,8,12,16,20)
    slot_key = now_et.strftime("%Y-%m-%d-") + str(now_et.hour // 4)
    if _crypto_sentiment_refresh_key == slot_key:
        return

    if not dry_run:
        try:
            import data_warehouse as _dw  # noqa: PLC0415
            _dw.refresh_crypto_sentiment()
            log.info("Crypto sentiment refresh complete")
        except Exception:
            log.debug("Crypto sentiment refresh failed (non-fatal)", exc_info=True)

    _crypto_sentiment_refresh_key = slot_key


def _maybe_publish_flat_day() -> None:
    """Post flat-day content at 4 PM ET on zero-trade weekdays."""
    global _flat_day_posted_date
    now_et  = datetime.now(ET)
    today   = _today()
    now_min = now_et.hour * 60 + now_et.minute
    weekday = now_et.weekday()

    if _flat_day_posted_date == today:
        return
    if weekday >= 5:
        return
    if now_min < 16 * 60 or now_min > 16 * 60 + 30:  # 4:00–4:30 PM ET
        return

    try:
        from trade_publisher import TradePublisher  # noqa: PLC0415
        publisher = TradePublisher()
        if not publisher.enabled:
            _flat_day_posted_date = today  # don't retry if publisher disabled
            return

        # Check closed trades in log for today
        import json as _json  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415
        trades_log = Path(__file__).parent / "logs" / "trades.jsonl"
        closed_trades_today: list = []
        if trades_log.exists():
            for line in trades_log.read_text().splitlines()[-200:]:
                try:
                    rec = _json.loads(line)
                    if (rec.get("status") == "submitted" and
                            rec.get("ts", "")[:10] == today):
                        closed_trades_today.append(rec)
                except Exception:
                    pass

        # Check for open positions (holding positions = not a flat day)
        open_positions: list = []
        try:
            import bot as _bot  # noqa: PLC0415
            open_positions = list(_bot._get_alpaca().get_all_positions())
        except Exception:
            pass

        if not closed_trades_today and not open_positions:
            import market_data  # noqa: PLC0415
            vix = market_data.get_vix()
            publisher.publish_flat_day(
                cycles=0,  # approximate — scheduler doesn't track cycle count
                vix=vix,
                skips=[],
                open_positions=open_positions,
                closed_trades_today=closed_trades_today,
            )

        # Always mark today as checked — prevents re-scanning every cycle
        _flat_day_posted_date = today
    except Exception:
        log.debug("_maybe_publish_flat_day failed (non-fatal)", exc_info=True)


def _maybe_publish_lookback() -> None:
    """Post lookback content at 6 PM ET on Mon/Wed/Fri."""
    global _lookback_posted_key
    import random  # noqa: PLC0415
    now_et  = datetime.now(ET)
    now_min = now_et.hour * 60 + now_et.minute
    weekday = now_et.weekday()  # 0=Mon, 2=Wed, 4=Fri

    if weekday not in (0, 2, 4):
        return
    if now_min < 18 * 60 or now_min > 18 * 60 + 30:  # 6:00–6:30 PM ET
        return

    slot_key = now_et.strftime("%Y-%m-%d-%w")
    if _lookback_posted_key == slot_key:
        return

    try:
        from trade_publisher import TradePublisher  # noqa: PLC0415
        publisher = TradePublisher()
        if not publisher.enabled:
            return

        # Find a trade from 3-14 days ago in trades.jsonl
        import json as _json  # noqa: PLC0415
        from pathlib import Path as _Path  # noqa: PLC0415
        trades_log = _Path(__file__).parent / "logs" / "trades.jsonl"
        if not trades_log.exists():
            return

        days_ago = random.randint(3, 14)
        target_date = (now_et - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        past_trades = []
        for line in trades_log.read_text().splitlines():
            try:
                rec = _json.loads(line)
                if rec.get("ts", "")[:10] == target_date and rec.get("status") == "submitted":
                    past_trades.append(rec)
            except Exception:
                pass

        if past_trades:
            trade = past_trades[0]
            publisher.publish_lookback(days_ago=days_ago, trade=trade)
            _lookback_posted_key = slot_key
    except Exception:
        log.debug("_maybe_publish_lookback failed (non-fatal)", exc_info=True)


def _maybe_update_engagement() -> None:
    """Update Twitter engagement stats daily at 8 PM ET."""
    global _engagement_update_date
    now_et  = datetime.now(ET)
    today   = _today()
    now_min = now_et.hour * 60 + now_et.minute

    if _engagement_update_date == today:
        return
    if now_min < 20 * 60 or now_min > 20 * 60 + 30:  # 8:00–8:30 PM ET
        return

    try:
        from trade_publisher import TradePublisher  # noqa: PLC0415
        publisher = TradePublisher()
        if publisher.enabled:
            publisher.update_engagement_stats()
            _engagement_update_date = today
    except Exception:
        log.debug("_maybe_update_engagement failed (non-fatal)", exc_info=True)


def _maybe_publish_monthly_milestone() -> None:
    """Post monthly milestone on the 13th of each month (min 28 days live)."""
    global _monthly_milestone_posted
    now_et = datetime.now(ET)
    month_key = now_et.strftime("%Y-%m")

    if _monthly_milestone_posted == month_key:
        return
    if now_et.day != 13:
        return
    if now_et.hour < 9:  # after 9 AM ET
        return

    # Guard: require at least 28 days live before posting
    try:
        import json as _json  # noqa: PLC0415
        from datetime import date as _date  # noqa: PLC0415
        from pathlib import Path as _Path  # noqa: PLC0415
        _cfg = _json.loads(_Path("strategy_config.json").read_text())
        _launch = _date.fromisoformat(_cfg.get("launch_date", "2026-04-13"))
        _days_live = (now_et.date() - _launch).days
        if _days_live < 28:
            log.info(
                "Monthly milestone skipped — %d days live (need 28)", _days_live)
            return
    except Exception as _mg_exc:
        log.debug("Milestone guard check failed (non-fatal): %s", _mg_exc)

    try:
        import memory as _mem  # noqa: PLC0415
        from trade_publisher import TradePublisher  # noqa: PLC0415
        publisher = TradePublisher()
        if not publisher.enabled:
            return

        perf = _mem.get_performance_summary()
        start_date = datetime(2026, 4, 13)
        months_running = (now_et.year - start_date.year) * 12 + (now_et.month - start_date.month) + 1

        publisher.publish_monthly_milestone(
            month_number=months_running,
            stats={
                "performance":  perf,
                "start_date":   "2026-04-13",
                "current_date": now_et.strftime("%Y-%m-%d"),
            },
        )
        _monthly_milestone_posted = month_key
    except Exception:
        log.debug("_maybe_publish_monthly_milestone failed (non-fatal)", exc_info=True)


def _maybe_generate_weekly_summary() -> None:
    """
    Every Sunday at 6 AM ET:
    1. Generate JSON performance summary (memory.generate_weekly_summary)
    2. Run the 5-agent strategic review (weekly_review.run_review)
    """
    global _weekly_summary_date
    now_et  = datetime.now(ET)
    today   = _today()
    weekday = now_et.weekday()   # 6 = Sunday
    now_min = now_et.hour * 60 + now_et.minute

    if _weekly_summary_date == today:
        return
    if weekday != 6:   # only Sunday
        return
    if now_min < 6 * 60:   # after 6 AM
        return

    try:
        import memory as mem
        summary = mem.generate_weekly_summary()
        log.info("Weekly summary generated  trades=%d  win_rate=%.1f%%",
                 summary.get("total_trades", 0), summary.get("win_rate", 0))
    except Exception:
        log.error("Weekly summary failed", exc_info=True)

    report_path = None
    try:
        log.info("Starting 5-agent weekly review...")
        report_path = weekly_review.run_review()
        log.info("Weekly review complete  report=%s", report_path)
    except Exception:
        log.error("Weekly review failed", exc_info=True)

    # Citrini Research availability check
    try:
        import json as _json  # noqa: PLC0415
        from pathlib import Path as _Path  # noqa: PLC0415
        _cit_path = _Path(__file__).parent / "data" / "macro_intelligence" / "citrini_positions.json"
        if _cit_path.exists():
            _cit = _json.loads(_cit_path.read_text())
            _memo_date = _cit.get("memo_date", "unknown")
            log.info(
                "Weekly review: Citrini memo on file dated %s — "
                "check citrini.com / @citrini on X for newer content; "
                "if a new memo is available run: python ingest_citrini_memo.py path/to/memo.pdf",
                _memo_date,
            )
        else:
            log.info(
                "Weekly review: no Citrini memo loaded — "
                "check citrini.com / @citrini on X for free memos; "
                "if available run: python ingest_citrini_memo.py path/to/memo.pdf"
            )
    except Exception:
        pass

    # Publish weekly recap to Twitter
    try:
        import memory as _mem  # noqa: PLC0415
        from trade_publisher import TradePublisher  # noqa: PLC0415
        _publisher = TradePublisher()
        if _publisher.enabled:
            _publisher.publish_weekly_recap(
                weekly_review_data={
                    "stats": _mem.generate_weekly_summary(),
                },
                report_path=str(report_path) if report_path else "",
            )
    except Exception:
        log.debug("Weekly recap tweet failed (non-fatal)", exc_info=True)

    _weekly_summary_date = today


# ── Trigger sources ───────────────────────────────────────────────────────────

def _check_stop_fills(prev_open_order_ids: set) -> set:
    """
    Compare currently-open Alpaca orders against the set from the previous cycle.
    If any order disappeared (filled or cancelled), check if it was a stop or
    take-profit and fire a trigger so Claude sees the updated portfolio promptly.

    Returns the new set of open order IDs (for the next call).
    Non-fatal — returns prev_open_order_ids unchanged on any error.
    """
    try:
        from alpaca.trading.enums import QueryOrderStatus  # noqa: PLC0415
        from alpaca.trading.requests import GetOrdersRequest  # noqa: PLC0415

        open_orders  = bot._get_alpaca().get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
        current_ids  = {str(o.id) for o in open_orders}
        filled_ids   = prev_open_order_ids - current_ids

        if filled_ids:
            # Look up what those orders were
            closed = bot._get_alpaca().get_orders(
                GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=50)
            )
            for o in closed:
                oid = str(o.id)
                if oid not in filled_ids:
                    continue
                o_type  = str(getattr(o, "type",   "")).lower()
                o_side  = str(getattr(o, "side",   "")).lower()
                o_sym   = str(getattr(o, "symbol", ""))
                filled  = float(getattr(o, "filled_qty",     0) or 0)
                avg_px  = float(getattr(o, "avg_fill_price", 0) or 0)

                is_exit = o_type in ("stop", "stop_limit", "limit") and "sell" in o_side
                if is_exit and filled > 0:
                    reason = (
                        f"stop fill: {o_sym} {o_type} "
                        f"qty={filled:.4g} @ ${avg_px:.4g}"
                    )
                    log.info("[TRIGGER] stop/tp fill detected → %s", reason)
                    trigger_cycle(reason)

        return current_ids

    except Exception as exc:
        log.debug("[TRIGGER] stop-fill check failed (non-fatal): %s", exc)
        return prev_open_order_ids


def _check_deadline_proximity() -> None:
    """
    Check time_bound_actions in strategy_config.json each cycle.
    If any mandatory exit deadline is within 30 minutes, fire a trigger
    so the scheduler doesn't miss the window between 5-minute cycles.
    Non-fatal.
    """
    try:
        import json as _json  # noqa: PLC0415
        from pathlib import Path as _Path  # noqa: PLC0415

        cfg_path = _Path(__file__).parent / "strategy_config.json"
        if not cfg_path.exists():
            return

        cfg  = _json.loads(cfg_path.read_text())
        tbas = cfg.get("time_bound_actions", [])
        now_utc = datetime.now(ZoneInfo("UTC"))

        for tba in tbas:
            deadline_str = tba.get("deadline_utc") or tba.get("deadline_et")
            if not deadline_str:
                continue
            sym    = tba.get("symbol", "?")
            action = tba.get("action", "exit")

            try:
                # Parse ISO timestamp; treat naive as ET
                dl = datetime.fromisoformat(deadline_str.replace("Z", "+00:00"))
                if dl.tzinfo is None:
                    dl = dl.replace(tzinfo=ET)
                dl_utc = dl.astimezone(ZoneInfo("UTC"))
            except Exception:
                continue

            minutes_left = (dl_utc - now_utc).total_seconds() / 60
            if 0 < minutes_left <= 30:
                reason = (
                    f"deadline: {sym} {action} in "
                    f"{int(minutes_left)}min (by {dl.astimezone(ET).strftime('%H:%M ET')})"
                )
                log.info("[TRIGGER] deadline approaching → %s", reason)
                trigger_cycle(reason)

    except Exception as exc:
        log.debug("[TRIGGER] deadline check failed (non-fatal): %s", exc)


# ── Trigger-aware sleep ────────────────────────────────────────────────────────

def _sleep_watching_triggers(seconds: int) -> list[str]:
    """
    Sleep for up to `seconds`, but wake early if a trigger arrives.
    Checks the trigger queue every 10 seconds.

    Returns a list of reason strings if triggers fired (possibly empty).
    """
    chunk     = 10
    remaining = seconds
    while remaining > 0:
        time.sleep(min(chunk, remaining))
        remaining -= chunk

        # Drain the queue and collect all pending reasons
        reasons: list[str] = []
        while True:
            try:
                reasons.append(_trigger_queue.get_nowait())
            except queue.Empty:
                break

        if reasons:
            return reasons

    return []


# ── Main loop ─────────────────────────────────────────────────────────────────

def run(dry_run: bool = False) -> None:
    _acquire_pid_lock()

    # Convert SIGTERM → KeyboardInterrupt so the finally block always runs.
    try:
        signal.signal(signal.SIGTERM, _handle_sigterm)
    except (ValueError, OSError):
        pass  # not in main thread; SIGTERM handler not registered

    global _last_cycle_end_time
    cycle_num = 0
    trigger_cycle_num = 0
    open_order_ids: set = set()   # for stop-fill detection

    _ensure_account_modes_initialized()
    log.info("Scheduler starting (24/7 mode)  dry_run=%s", dry_run)
    print("[scheduler] 24/7 mode active. Press Ctrl+C to stop.\n")

    def _run_one_cycle(
        session: str,
        instr_session: str,
        label: str,
        trigger_reason: str = "",
    ) -> None:
        """Execute one full trading cycle (Account 1 + Account 2). Updates shared state.

        `trigger_reason` is the scheduler-side reason string (e.g. "macro wire: ...").
        Empty for scheduled cycles; populated for triggered cycles. Forwarded to
        bot.run_cycle so the sonnet gate can force a full prompt on macro-wire triggers.
        """
        nonlocal open_order_ids
        now_et  = datetime.now(ET)
        now_str = now_et.strftime("%a %b %d  %I:%M %p ET")
        _, interval_sec = get_session_and_interval(now_et)
        wake_et  = now_et + timedelta(seconds=interval_sec)
        next_str = wake_et.strftime("%I:%M %p ET")

        print(f"\n{'━'*62}")
        print(f"  {label}  |  {session.upper()}  |  {now_str}")
        print(f"  Instruments: {SESSION_INSTRUMENTS[instr_session]}")
        print(f"{'━'*62}")

        if not dry_run:
            time.monotonic()
            try:
                now_min = now_et.hour * 60 + now_et.minute
                if _MARKET_START <= now_min < _MARKET_START + 15 and not _orb_locked:
                    try:
                        import scanner as _scanner  # noqa: PLC0415
                        _scanner.update_orb_candidates()
                        log.debug("ORB candidates updated (formation cycle at %d min)", now_min)
                    except Exception as _orb_upd_exc:
                        log.debug("ORB candidate update failed (non-fatal): %s", _orb_upd_exc)

                bot.run_cycle(
                    session_tier=session,
                    session_instruments=SESSION_INSTRUMENTS[instr_session],
                    next_cycle_time=next_str,
                    trigger_reason=trigger_reason,
                )

                # Account 2 — options bot (90s offset, market hours only).
                # Also gated by the hard trading window so A2 cannot bleed into
                # post-4:00 PM ET via the 90 s offset or close-window cycles.
                if session in ("market", "pre_open") and _is_claude_trading_window(
                    cfg=_load_strategy_config_safe()
                ):
                    try:
                        time.sleep(90)
                        # Re-check after sleep — 90 s may push us past 4:15 PM
                        if not _is_claude_trading_window(
                            cfg=_load_strategy_config_safe()
                        ):
                            log.info("[A2] window closed during 90s offset — skipping debate")
                        else:
                            import bot_options as _bot_opts  # noqa: PLC0415
                            _bot_opts.run_options_cycle(
                                session_tier=session,
                                next_cycle_time=next_str,
                            )
                    except Exception as _opts_exc:
                        log.error("[OPTS] Account 2 cycle error (non-fatal): %s", _opts_exc,
                                  exc_info=True)

            except KeyboardInterrupt:
                raise
            except Exception:
                log.error("%s error", label, exc_info=True)
                print(f"\n[scheduler] !! {label} error — skipping:")
                traceback.print_exc()

            # Snapshot open orders for stop-fill detection next iteration
            open_order_ids = _check_stop_fills(open_order_ids)

        else:
            print(f"  [dry-run] skipping bot.run_cycle() ({label})")
            print("  [dry-run] skipping bot_options.run_options_cycle()")

    while True:
        # Dynamic interval based on market phase
        session, interval_sec = get_session_and_interval()
        instr_session = SESSION_MARKET if session == "pre_open" else session

        # Scheduled jobs (run before each cycle)
        _maybe_cull_post_earnings(dry_run)
        _maybe_refresh_earnings_calendar_av(dry_run)
        _maybe_run_premarket_jobs(dry_run)
        _maybe_run_earnings_rotation(dry_run)
        _maybe_check_earnings_calendar_staleness(dry_run)
        _maybe_refresh_macro_intelligence(dry_run)
        _maybe_refresh_earnings_intel(dry_run)
        _maybe_refresh_iv_history(dry_run)
        _maybe_refresh_economic_calendar(dry_run)  # intraday econ slots
        _maybe_write_overnight_digest(dry_run)
        _maybe_run_intelligence_brief(dry_run)
        _maybe_run_orb_scan(dry_run)
        _maybe_run_readiness_check(dry_run)
        _maybe_refresh_global_indices(dry_run)
        _maybe_refresh_reddit_sentiment(dry_run)
        _maybe_refresh_form4_trades(dry_run)
        _maybe_refresh_crypto_sentiment(dry_run)
        _maybe_refresh_macro_wire(dry_run)
        _maybe_refresh_qualitative_context(dry_run)
        _maybe_run_options_close_check(dry_run)
        _check_intraday_momentum(dry_run)
        _maybe_run_preopen_cycle(dry_run)
        _maybe_send_daily_report()
        _maybe_send_zero_fill_alert(dry_run)
        _maybe_reset_session_watchlist()
        _maybe_write_daily_digest(dry_run)
        _maybe_write_eod_digest(dry_run)
        _maybe_backfill_market_impact(dry_run)
        _maybe_backfill_decision_outcomes(dry_run)
        _maybe_generate_weekly_summary()
        _maybe_publish_flat_day()
        _maybe_publish_lookback()
        _maybe_update_engagement()
        _maybe_publish_monthly_milestone()
        _maybe_check_api_costs()

        # Deadline proximity check — triggers an immediate cycle if needed
        _check_deadline_proximity()

        # Skip normal cycle during pre_open (handled by _maybe_run_preopen_cycle)
        if session == "pre_open":
            _sleep_watching_triggers(interval_sec)
            continue

        cycle_num += 1
        now_et = datetime.now(ET)
        log.info("Cycle #%d  session=%s  interval=%ds  %s",
                 cycle_num, session, interval_sec,
                 now_et.strftime("%a %b %d  %I:%M %p ET"))

        _t_cycle_start = time.monotonic()
        _run_one_cycle(session, instr_session, f"CYCLE #{cycle_num}")
        _last_cycle_end_time = time.monotonic()

        # Sleep for the remainder of the interval after accounting for cycle runtime
        sleep_for = max(0, interval_sec - (_last_cycle_end_time - _t_cycle_start))

        wake = datetime.now(ET) + timedelta(seconds=sleep_for)
        log.info("Sleeping up to %.0fs — next scheduled cycle at %s",
                 sleep_for, wake.strftime("%I:%M %p ET"))
        print(f"\n[scheduler] Next cycle ~{wake.strftime('%I:%M %p ET')} "
              f"({interval_sec}s interval). Watching for triggers.")

        trigger_reasons = _sleep_watching_triggers(sleep_for)

        # Process any trigger(s) that arrived during sleep
        if trigger_reasons:
            combined = " | ".join(dict.fromkeys(trigger_reasons))  # dedup, preserve order
            now_mono = time.monotonic()
            secs_since_last = now_mono - _last_cycle_end_time
            _COOLDOWN = 60  # seconds

            if secs_since_last < _COOLDOWN:
                # Too soon — re-queue the reasons for the next scheduled slot
                log.info(
                    "[TRIGGER] cooldown active (%.0fs < %ds) — deferring: %s",
                    secs_since_last, _COOLDOWN, combined,
                )
                for r in trigger_reasons:
                    _trigger_queue.put(r)
            else:
                # Fire an immediate out-of-schedule cycle
                trigger_cycle_num += 1
                session_now, _ = get_session_and_interval()
                instr_now = SESSION_MARKET if session_now == "pre_open" else session_now
                log.info(
                    "[TRIGGER] firing cycle #T%d (%.0fs early) — %s",
                    trigger_cycle_num,
                    sleep_for - secs_since_last,
                    combined,
                )
                _run_one_cycle(
                    session_now, instr_now,
                    f"TRIGGER #{trigger_cycle_num} ({combined[:60]})",
                    trigger_reason=combined,
                )
                _last_cycle_end_time = time.monotonic()


def _sleep_with_interrupt(seconds: int) -> None:
    chunk = 10
    remaining = seconds
    while remaining > 0:
        time.sleep(min(chunk, remaining))
        remaining -= chunk


# ── New scheduled functions ───────────────────────────────────────────────────

def _check_intraday_momentum(dry_run: bool = False) -> None:
    """Lightweight intraday momentum detector.

    Runs every scheduler loop pass during market hours. Uses already-cached
    intraday_cache data (no fresh API calls). Fires scheduler.trigger_cycle
    when a symbol's 5-min momentum and volume both exceed config thresholds.

    Scope:
      - All symbols with open A1 positions
      - Top-N symbols from the latest signal_scores.json (default N=10)

    Cooldown: the existing 60-second run() cooldown handles flood prevention
    for triggered cycles; no additional rate-limit needed here. Per-symbol
    last-fire timestamps prevent re-alerting on the same bar rollover.
    """
    now_et  = datetime.now(ET)
    weekday = now_et.weekday()
    now_min = now_et.hour * 60 + now_et.minute
    if weekday >= 5:
        return
    # Only during core 9:30 AM – 4:00 PM ET window (avoid extended session noise)
    if now_min < 9 * 60 + 30 or now_min >= 16 * 60:
        return
    if dry_run:
        return

    # Load thresholds from strategy_config.json (cached per module lifetime)
    global _momentum_cfg, _momentum_last_fired
    try:
        if _momentum_cfg is None:
            try:
                _scfg = json.loads((Path(__file__).parent / "strategy_config.json").read_text())
                _momentum_cfg = _scfg.get("momentum_trigger", {}) or {}
            except Exception:
                _momentum_cfg = {}
        min_move = float(_momentum_cfg.get("min_price_move_pct", 1.5))
        min_vol  = float(_momentum_cfg.get("min_vol_ratio",       2.0))
        cooldown_sec = int(_momentum_cfg.get("per_symbol_cooldown_sec", 300))
        top_n = int(_momentum_cfg.get("top_n_from_signals", 10))
    except Exception:
        return

    # Build universe: open A1 positions + top-N scored symbols
    universe: set[str] = set()
    try:
        key = os.getenv("ALPACA_API_KEY")
        sec = os.getenv("ALPACA_SECRET_KEY")
        if key and sec:
            from alpaca.trading.client import TradingClient  # noqa: PLC0415
            client = TradingClient(key, sec, paper=True)
            for p in client.get_all_positions():
                s = (getattr(p, "symbol", "") or "").upper()
                if s and "/" not in s:
                    universe.add(s)
    except Exception as exc:
        log.debug("[MOMENTUM] positions fetch failed: %s", exc)

    try:
        _ss_path = Path("data/market/signal_scores.json")
        if _ss_path.exists():
            _ss = json.loads(_ss_path.read_text())
            scored = _ss.get("scored_symbols", {}) or {}
            ranked = sorted(
                scored.items(),
                key=lambda kv: float(kv[1].get("score", 0)) if isinstance(kv[1], dict) else 0,
                reverse=True,
            )[:top_n]
            for sym, _ in ranked:
                universe.add(sym.upper())
    except Exception as exc:
        log.debug("[MOMENTUM] signal_scores read failed: %s", exc)

    if not universe:
        return

    try:
        import intraday_cache as _ic  # noqa: PLC0415
    except Exception:
        return

    now_ts = time.monotonic()
    for sym in universe:
        try:
            # Per-symbol cooldown to avoid re-triggering on the same bar
            last = _momentum_last_fired.get(sym, 0.0)
            if now_ts - last < cooldown_sec:
                continue
            s = _ic.get_intraday_summary(sym)
            if not s or s.get("bar_count", 0) < 3:
                continue
            mom = s.get("momentum_5bar")
            vol = s.get("vol_ratio")
            if mom is None or vol is None:
                continue
            if abs(mom) >= min_move and vol >= min_vol:
                reason = f"momentum: {sym} {mom:+.1f}% vol={vol:.1f}x"
                try:
                    trigger_cycle(reason)
                    _momentum_last_fired[sym] = now_ts
                    log.info("[MOMENTUM] trigger fired — %s", reason)
                except Exception as exc:
                    log.debug("[MOMENTUM] trigger_cycle failed for %s: %s", sym, exc)
        except Exception as exc:
            log.debug("[MOMENTUM] check %s failed: %s", sym, exc)


def _run_qualitative_sweep_background(
    md: dict,
    regime: dict,
    symbols: list,
    now_slot: str,
    news_hash: str,
) -> None:
    """Background worker for the L1 sweep. Sets the running guard on entry
    and clears it on exit. Updates last-run keys on success. Non-fatal."""
    global _qualitative_sweep_running, _last_qualitative_sweep_key
    global _last_qualitative_news_hash
    try:
        from bot_stage1_5_qualitative import run_qualitative_sweep  # noqa: PLC0415
        result = run_qualitative_sweep(md, regime, symbols)
        if result:
            _last_qualitative_sweep_key = now_slot
            _last_qualitative_news_hash = news_hash
    except Exception as exc:
        log.warning("[L1] background sweep failed (non-fatal): %s", exc)
    finally:
        _qualitative_sweep_running = False


def _maybe_refresh_qualitative_context(dry_run: bool = False) -> None:
    """Fire the L1 Sonnet qualitative sweep when due.

    Scheduled windows (weekdays): 2:00 AM, 6:00 AM, 10:00 AM ET.
    Event-driven: if the news-hash fingerprint changed since last sweep
    AND last sweep is older than 30 min.

    Age gate: never re-fire within 4 hours unless event-driven.

    Runs in a background thread so the main cycle never blocks on a ~20-40s
    Sonnet call. The `_qualitative_sweep_running` flag prevents overlap.
    """
    global _qualitative_sweep_running, _qualitative_thread

    now_et = datetime.now(ET)
    weekday = now_et.weekday()
    now_min = now_et.hour * 60 + now_et.minute

    # Build a "slot" key — scheduled windows are 6/10 AM ET weekdays.
    # The 2 AM slot was removed for cost (~$0.07/day) — 6 AM sweep is
    # sufficient warm-up for the 4:15 AM morning brief and 9:30 AM open.
    # Anything outside those windows can only be triggered by event-driven
    # refresh (news hash changed + last sweep > 30 min) inside 4 AM–8 PM ET.
    scheduled_slot: str | None = None
    if weekday < 5:
        if 6 * 60 <= now_min < 7 * 60:
            scheduled_slot = now_et.strftime("%Y-%m-%d-06")
        elif 10 * 60 <= now_min < 11 * 60:
            scheduled_slot = now_et.strftime("%Y-%m-%d-10")

    if _qualitative_sweep_running:
        return   # another thread is mid-sweep

    # ── Build snapshot inputs (cheap — just reads market_data cache) ──────
    try:
        import market_data as _md_mod  # noqa: PLC0415
        import watchlist_manager as _wm  # noqa: PLC0415

        wl = _wm.get_active_watchlist()
        all_syms = [s["symbol"] for s in wl.get("all", []) if s.get("symbol")]
        if not all_syms:
            return

        # Read the current cycle's cached md dict if available; otherwise
        # build a lightweight snapshot.
        try:
            md_snap = _md_mod.fetch_all(
                wl.get("stocks", []) + wl.get("etfs", []),
                wl.get("crypto", []),
                "market",
                next_cycle_time="?",
            )
        except Exception as exc:
            log.debug("[L1] md snapshot build failed: %s", exc)
            return

        # Regime snapshot (cheap — one Haiku call already happens every cycle,
        # but we reuse the cached last regime from gate state to avoid an extra call).
        regime_snap: dict = {}
        try:
            _gate_path = Path("data/market/gate_state.json")
            if _gate_path.exists():
                _gs = json.loads(_gate_path.read_text())
                regime_snap = {
                    "bias":          _gs.get("last_regime", "neutral"),
                    "regime_score":  50,
                    "session_theme": "",
                }
        except Exception:
            pass

        from bot_stage1_5_qualitative import news_hash_fingerprint  # noqa: PLC0415
        news_hash = news_hash_fingerprint(md_snap)
    except Exception as exc:
        log.debug("[L1] snapshot assembly failed: %s", exc)
        return

    # Event-driven and age-fallback paths are both confined to the
    # 4 AM–8 PM ET weekday window so we never wake at 3 AM on a news blip.
    _event_window = (weekday < 5) and (4 * 60 <= now_min < 20 * 60)

    # Decide whether to fire
    fire = False
    reason = ""
    if scheduled_slot and scheduled_slot != _last_qualitative_sweep_key:
        fire = True
        reason = f"scheduled_slot={scheduled_slot}"
    else:
        # Event-driven: news changed AND age gate (>30 min) elapsed
        try:
            from bot_stage1_5_qualitative import context_age_minutes  # noqa: PLC0415
            age_min = context_age_minutes()
        except Exception:
            age_min = 1e9
        if (news_hash and news_hash != _last_qualitative_news_hash
                and age_min > 30 and _event_window):
            fire = True
            reason = f"news_hash_change age={age_min:.0f}m"
        elif age_min > 240 and news_hash and _event_window:
            # Hard fallback: 4h age gate ensures we don't go stale even on
            # quiet news days. Same 4 AM–8 PM ET window.
            fire = True
            reason = f"age_gate age={age_min:.0f}m"

    if not fire:
        return

    if dry_run:
        log.info("[L1] [dry-run] would fire qualitative sweep (%s)", reason)
        return

    log.info("[L1] firing qualitative sweep — %s", reason)
    _qualitative_sweep_running = True
    slot_key = scheduled_slot or now_et.strftime("%Y-%m-%d-%H")
    try:
        _qualitative_thread = threading.Thread(
            target=_run_qualitative_sweep_background,
            args=(md_snap, regime_snap, all_syms, slot_key, news_hash),
            daemon=True,
            name="L1_qualitative_sweep",
        )
        _qualitative_thread.start()
    except Exception as exc:
        _qualitative_sweep_running = False
        log.warning("[L1] thread start failed (non-fatal): %s", exc)


def _maybe_refresh_macro_wire(dry_run: bool = False) -> None:
    """
    Refresh macro wire RSS feeds on a fixed 15-minute slot key — independent
    of the session interval. The 60-second module-level throttle inside
    macro_wire.fetch_macro_wire() is the floor; this 15-min slot is the
    ceiling for unique fetches per hour.
    """
    global _macro_wire_refresh_key
    now_et  = datetime.now(ET)

    # Fixed 15-minute slot regardless of session
    slot_key = now_et.strftime("%Y-%m-%d-%H-") + str(now_et.minute // 15)
    if _macro_wire_refresh_key == slot_key:
        return

    if not dry_run:
        try:
            from macro_wire import refresh_macro_wire  # noqa: PLC0415
            refresh_macro_wire()
            _macro_wire_refresh_key = slot_key
        except Exception as exc:
            log.debug("_maybe_refresh_macro_wire failed (non-fatal): %s", exc)
    else:
        _macro_wire_refresh_key = slot_key


def _maybe_run_orb_scan(dry_run: bool = False) -> None:
    """Run ORB candidate scan at 4:30 AM ET on weekdays."""
    global _orb_scan_ran_date
    now_et  = datetime.now(ET)
    today   = _today()
    now_min = now_et.hour * 60 + now_et.minute
    weekday = now_et.weekday()

    if _orb_scan_ran_date == today:
        return
    if weekday >= 5:
        return
    if not (4 * 60 + 30 <= now_min <= 5 * 60 + 30):
        return

    log.info("Running ORB candidate scan")
    if not dry_run:
        try:
            import scanner  # noqa: PLC0415
            scanner.run_orb_scan()
            log.info("ORB scan complete")
        except Exception:
            log.error("ORB scan failed", exc_info=True)
    else:
        log.info("[dry-run] Skipping ORB scan")
    _orb_scan_ran_date = today


def _maybe_run_preopen_cycle(dry_run: bool = False) -> None:
    """Run pre-open cycle at 9:28 AM ET — refresh ORB candidates, macro wire, check calendar."""
    global _preopen_ran_date
    now_et  = datetime.now(ET)
    today   = _today()
    now_min = now_et.hour * 60 + now_et.minute
    weekday = now_et.weekday()

    if _preopen_ran_date == today:
        return
    if weekday >= 5:
        return
    if not (9 * 60 + 28 <= now_min < 9 * 60 + 31):
        return

    log.info("PRE-OPEN: running 9:28 AM pre-market prep cycle")
    if not dry_run:
        try:
            import scanner  # noqa: PLC0415
            scanner.update_orb_candidates()
            log.info("PRE-OPEN: ORB candidates updated")
        except Exception:
            log.error("ORB candidate update failed", exc_info=True)

        _maybe_refresh_macro_wire(dry_run=False)

        # Check for events in next 60 min
        try:
            import data_warehouse  # noqa: PLC0415
            cal = data_warehouse.load_economic_calendar()
            upcoming = [
                e for e in cal.get("events", [])
                if 0 < e.get("minutes_from_now", 9999) <= 60
                and e.get("impact") in ("high", "medium")
            ]
            log.info("PRE-OPEN: %d economic events in next 60 min", len(upcoming))
            for e in upcoming:
                log.info("  PRE-OPEN event: %s in %d min [%s]",
                         e.get("event"), e.get("minutes_from_now"), e.get("impact"))
        except Exception:
            pass

        try:
            bot.run_cycle(
                session_tier="market",
                session_instruments=SESSION_INSTRUMENTS[SESSION_MARKET],
                next_cycle_time="09:30 AM ET",
            )
        except Exception:
            log.error("Pre-open cycle failed", exc_info=True)
    else:
        log.info("[dry-run] Skipping pre-open cycle")

    _preopen_ran_date = today


def _maybe_write_overnight_digest(dry_run: bool = False) -> None:
    """
    Write Haiku overnight digest at 4:00 AM ET on weekdays, before the
    morning brief at 4:15 AM. Window: last 12 hours (≈ yesterday close → 4 AM).
    Once-per-day; non-fatal.
    """
    global _overnight_digest_written_date
    now_et  = datetime.now(ET)
    today   = _today()
    now_min = now_et.hour * 60 + now_et.minute
    weekday = now_et.weekday()

    if _overnight_digest_written_date == today:
        return
    if weekday >= 5:
        return
    if not (4 * 60 <= now_min < 4 * 60 + 10):  # 4:00–4:10 AM ET window
        return

    if dry_run:
        log.info("[OVERNIGHT_DIGEST] [dry-run] would write overnight digest")
        _overnight_digest_written_date = today
        return

    try:
        from macro_wire import write_overnight_digest  # noqa: PLC0415
        write_overnight_digest(window_hours=12)
        _overnight_digest_written_date = today
    except Exception as exc:
        log.warning("[OVERNIGHT_DIGEST] failed (non-fatal): %s", exc)


def _maybe_write_eod_digest(dry_run: bool = False) -> None:
    """
    End-of-day digest at 4:15 PM ET on weekdays. Window: last 8 hours
    (≈ 8:15 AM → 4:15 PM ET) — captures the trading-day macro stream.
    Once-per-day; non-fatal.
    """
    global _eod_digest_written_date
    now_et  = datetime.now(ET)
    today   = _today()
    now_min = now_et.hour * 60 + now_et.minute
    weekday = now_et.weekday()

    if _eod_digest_written_date == today:
        return
    if weekday >= 5:
        return
    if not (16 * 60 + 15 <= now_min < 16 * 60 + 25):  # 4:15–4:25 PM ET window
        return

    if dry_run:
        log.info("[EOD_DIGEST] [dry-run] would write EOD digest")
        _eod_digest_written_date = today
        return

    try:
        from macro_wire import write_overnight_digest  # noqa: PLC0415
        write_overnight_digest(window_hours=8)
        _eod_digest_written_date = today
    except Exception as exc:
        log.warning("[EOD_DIGEST] failed (non-fatal): %s", exc)


def _maybe_write_daily_digest(dry_run: bool = False) -> None:
    """Write daily macro wire digest at 4:00 PM ET."""
    global _daily_digest_written_date
    now_et  = datetime.now(ET)
    today   = _today()
    now_min = now_et.hour * 60 + now_et.minute
    weekday = now_et.weekday()

    if _daily_digest_written_date == today:
        return
    if weekday >= 5:
        return
    if not (16 * 60 <= now_min <= 16 * 60 + 15):
        return

    if not dry_run:
        try:
            from macro_wire import write_daily_digest  # noqa: PLC0415
            write_daily_digest()
        except Exception:
            log.error("Daily digest write failed", exc_info=True)

    _daily_digest_written_date = today


def _maybe_backfill_market_impact(dry_run: bool = False) -> None:
    """Backfill macro wire market impact at 4:15 PM ET."""
    global _market_impact_backfill_date
    now_et  = datetime.now(ET)
    today   = _today()
    now_min = now_et.hour * 60 + now_et.minute
    weekday = now_et.weekday()

    if _market_impact_backfill_date == today:
        return
    if weekday >= 5:
        return
    if not (16 * 60 + 15 <= now_min <= 16 * 60 + 30):
        return

    if not dry_run:
        try:
            from macro_wire import backfill_market_impact  # noqa: PLC0415
            backfill_market_impact()
            log.info("Macro wire market impact backfilled")
        except Exception:
            log.error("Market impact backfill failed", exc_info=True)

    _market_impact_backfill_date = today


def _maybe_backfill_decision_outcomes(dry_run: bool = False) -> None:
    """Backfill forward returns into decision_outcomes.jsonl at 4:30 PM ET weekdays.

    Runs once per trading day after market close. Joins submitted decisions
    against backtest_latest.json on (symbol, date) to populate return_1d/3d/5d.
    Non-fatal — a failed backfill never affects the scheduler loop.
    """
    global _outcomes_backfill_date
    now_et  = datetime.now(ET)
    today   = _today()
    now_min = now_et.hour * 60 + now_et.minute
    weekday = now_et.weekday()

    if _outcomes_backfill_date == today:
        return
    if weekday >= 5:
        return
    if not (16 * 60 + 30 <= now_min <= 17 * 60):   # 4:30–5:00 PM window
        return

    if not dry_run:
        try:
            from decision_outcomes import backfill_forward_returns  # noqa: PLC0415
            updated = backfill_forward_returns(days_back=30)
            log.info("[OUTCOMES] Daily backfill complete — %d records updated", updated)
        except Exception:
            log.warning("[OUTCOMES] Daily backfill failed (non-fatal)", exc_info=True)

    _outcomes_backfill_date = today


def _maybe_run_options_close_check(dry_run: bool = False) -> None:
    """
    Run the A2 close-check loop outside the 9:25 AM–4:15 PM ET window.

    Claude-free — only checks open structures for expiry/stop/roll conditions
    and submits limit closes via Alpaca. Inside the trading window the inline
    `bot_options.run_options_cycle()` path handles close-check at the end of
    each A2 debate cycle, so this function is a no-op then.

    Weekday-only — A2 has no positions to manage on weekends (no expiry rolls,
    no theta urgency until Monday's pre-market check).
    """
    now_et = datetime.now(ET)
    if now_et.weekday() >= 5:
        return
    cfg = _load_strategy_config_safe()
    if _is_claude_trading_window(now_et=now_et, cfg=cfg):
        return  # handled inline by run_options_cycle during the window

    if dry_run:
        log.info("[A2_CLOSE_CHECK] [dry-run] would run off-hours close-check")
        return

    try:
        # close_check_loop lives in bot_options_stage4_execution; bot_options.py
        # also exposes it via the orchestrator's run path.
        from bot_options import _get_alpaca  # noqa: PLC0415
        from bot_options_stage4_execution import close_check_loop  # noqa: PLC0415
        close_check_loop(_get_alpaca())
    except Exception as exc:
        log.warning("[A2_CLOSE_CHECK] off-hours check failed (non-fatal): %s", exc)


def _maybe_run_readiness_check(dry_run: bool = False) -> None:
    """Run validate_config.py once daily at 4:45 AM ET — updates readiness_status_latest.json.

    Runs after the data warehouse refresh (4:00 AM) and ORB scan (4:30 AM) so the
    report reflects the freshest data. Non-fatal — never blocks the main bot cycle.
    """
    global _readiness_ran_date
    now_et  = datetime.now(ET)
    today   = _today()
    now_min = now_et.hour * 60 + now_et.minute
    weekday = now_et.weekday()

    if _readiness_ran_date == today:
        return
    if weekday >= 5:
        return
    if not (4 * 60 + 45 <= now_min <= 5 * 60 + 30):   # 4:45–5:30 AM window
        return

    log.info("[READINESS] Running daily readiness check (validate_config.py)")
    if not dry_run:
        try:
            import subprocess  # noqa: PLC0415
            _vc_path = Path(__file__).parent / "validate_config.py"
            _proc    = subprocess.run(
                ["python3", str(_vc_path)],
                capture_output=True, text=True, timeout=60,
            )
            if _proc.returncode == 0:
                log.info("[READINESS] Readiness check passed — readiness_status_latest.json updated")
            else:
                log.warning(
                    "[READINESS] Readiness check has failures — see data/reports/readiness_status_latest.json"
                )
        except Exception as _rc_exc:
            log.warning("[READINESS] Readiness check failed (non-fatal): %s", _rc_exc)
    else:
        log.info("[dry-run] Skipping readiness check")

    _readiness_ran_date = today


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="24/7 trading bot scheduler")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        run(dry_run=args.dry_run)
    except KeyboardInterrupt:
        log.info("Scheduler stopped by user")
        print("\n[scheduler] Stopped.")

