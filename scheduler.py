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
import os
import queue
import signal
import time
import traceback
from datetime import datetime, timedelta
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

REPORT_HOUR_ET  = 12   # noon ET = 9 AM PST

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
_market_impact_backfill_date:  str = ""   # "YYYY-MM-DD" of last backfill
_outcomes_backfill_date:       str = ""   # "YYYY-MM-DD" of last outcomes backfill
_econ_calendar_refresh_key:    str = ""   # "YYYY-MM-DD-HHMM" slot key


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
    global _report_sent_date
    now_et = datetime.now(ET)
    today  = _today()
    if _report_sent_date == today:
        return
    report_time = now_et.replace(hour=REPORT_HOUR_ET, minute=0, second=0, microsecond=0)
    if now_et >= report_time:
        log.info("Sending daily report for %s", today)
        try:
            report_module.send_report_email()
            _report_sent_date = today
        except Exception:
            log.error("Daily report failed", exc_info=True)


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
            import options_data as _od        # noqa: PLC0415
            import watchlist_manager as _wm   # noqa: PLC0415
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

    # Always mark the slot as consumed — even on failure or dry-run — so we
    # don't spam Finnhub on every cycle within the window.
    _econ_calendar_refresh_key = slot_key

    if dry_run:
        log.info("[dry-run] Skipping economic calendar refresh (slot=%s)", slot_key)
        return

    try:
        import json as _json                               # noqa: PLC0415
        from pathlib import Path as _Path                  # noqa: PLC0415
        import data_warehouse as _dw                       # noqa: PLC0415

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
        log.warning("[ECON] calendar refresh failed (non-fatal): %s", exc)


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
    if not dry_run:
        try:
            import data_warehouse
            data_warehouse.run_full_refresh()
            log.info("Data warehouse refresh complete")
        except Exception:
            log.error("Data warehouse failed", exc_info=True)

        try:
            import scanner
            scanner.run_scan()
            log.info("Pre-market scanner complete")
        except Exception:
            log.error("Scanner failed", exc_info=True)
    else:
        log.info("[dry-run] Skipping data warehouse + scanner")

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
        import watchlist_manager as wm
        wm.reset_session_tiers()
        _session_reset_done = today
        log.info("8 PM session reset: dynamic/intraday watchlist cleared")


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
        except Exception:
            log.error("Global indices refresh failed", exc_info=True)

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
            log.info("Morning brief complete — tone=%s  picks=%d",
                     brief.get("market_tone"), len(brief.get("conviction_picks", [])))
        except Exception:
            log.error("Morning brief generation failed", exc_info=True)

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


def _maybe_refresh_form4_trades(dry_run: bool = False) -> None:
    """Refresh SEC Form 4 insider trades every 4 hours during weekdays."""
    global _form4_refresh_key
    now_et  = datetime.now(ET)
    now_min = now_et.hour * 60 + now_et.minute
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
        from pathlib import Path  # noqa: PLC0415
        import json as _json  # noqa: PLC0415
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
        from pathlib import Path as _Path  # noqa: PLC0415
        import json as _json  # noqa: PLC0415
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
        from pathlib import Path as _Path  # noqa: PLC0415
        from datetime import date as _date  # noqa: PLC0415
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
        from trade_publisher import TradePublisher  # noqa: PLC0415
        import memory as _mem  # noqa: PLC0415
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
        import json as _json                                               # noqa: PLC0415
        from pathlib import Path as _Path                                  # noqa: PLC0415
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
        from trade_publisher import TradePublisher  # noqa: PLC0415
        import memory as _mem  # noqa: PLC0415
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
        from alpaca.trading.requests import GetOrdersRequest        # noqa: PLC0415
        from alpaca.trading.enums import QueryOrderStatus           # noqa: PLC0415

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
        import json as _json                               # noqa: PLC0415
        from pathlib import Path as _Path                  # noqa: PLC0415

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

    log.info("Scheduler starting (24/7 mode)  dry_run=%s", dry_run)
    print("[scheduler] 24/7 mode active. Press Ctrl+C to stop.\n")

    def _run_one_cycle(session: str, instr_session: str, label: str) -> None:
        """Execute one full trading cycle (Account 1 + Account 2). Updates shared state."""
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
            t_start = time.monotonic()
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
                )

                # Account 2 — options bot (90s offset, market hours only)
                if session in ("market", "pre_open"):
                    try:
                        time.sleep(90)
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
        _maybe_run_premarket_jobs(dry_run)
        _maybe_refresh_macro_intelligence(dry_run)
        _maybe_refresh_iv_history(dry_run)
        _maybe_refresh_economic_calendar(dry_run)  # intraday econ slots
        _maybe_run_morning_brief(dry_run)
        _maybe_run_orb_scan(dry_run)
        _maybe_refresh_global_indices(dry_run)
        _maybe_refresh_reddit_sentiment(dry_run)
        _maybe_refresh_form4_trades(dry_run)
        _maybe_refresh_crypto_sentiment(dry_run)
        _maybe_refresh_macro_wire(dry_run)
        _maybe_run_preopen_cycle(dry_run)
        _maybe_send_daily_report()
        _maybe_reset_session_watchlist()
        _maybe_write_daily_digest(dry_run)
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
                )
                _last_cycle_end_time = time.monotonic()


def _sleep_with_interrupt(seconds: int) -> None:
    chunk = 10
    remaining = seconds
    while remaining > 0:
        time.sleep(min(chunk, remaining))
        remaining -= chunk


# ── New scheduled functions ───────────────────────────────────────────────────

def _maybe_refresh_macro_wire(dry_run: bool = False) -> None:
    """
    Refresh macro wire RSS feeds.
    Market hours: every cycle (~5 min)
    Extended:     every 15 min
    Overnight:    every 30 min
    Minimum 60 seconds between fetches (enforced inside macro_wire.py).
    """
    global _macro_wire_refresh_key
    now_et  = datetime.now(ET)
    now_min = now_et.hour * 60 + now_et.minute
    weekday = now_et.weekday()

    _, interval_sec = get_session_and_interval(now_et)

    # Compute last-fetch window key based on session interval
    slot_min = interval_sec // 60
    slot_key = now_et.strftime("%Y-%m-%d-%H:") + str(now_et.minute // max(slot_min, 1))
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="24/7 trading bot scheduler")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        run(dry_run=args.dry_run)
    except KeyboardInterrupt:
        log.info("Scheduler stopped by user")
        print("\n[scheduler] Stopped.")

