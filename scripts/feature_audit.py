#!/usr/bin/env python3
"""
scripts/feature_audit.py — On-Demand Full Feature Status Report

Run:    python3 scripts/feature_audit.py
Exit 0: all features OK.
Exit 1: one or more features BROKEN.

Reads local files when run locally, server files when run on the server.
No external dependencies beyond the standard library. Runs in < 30 seconds.
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
DATA     = BASE_DIR / "data"
LOGS     = BASE_DIR / "logs"

NOW       = datetime.now()
TODAY_STR = NOW.strftime("%Y-%m-%d")

# ── Helpers ───────────────────────────────────────────────────────────────────

def _age_hours(path: Path) -> float | None:
    try:
        return (time.time() - path.stat().st_mtime) / 3600
    except Exception:
        return None


def _read_log_tail(n: int = 2000) -> list[str]:
    log = LOGS / "bot.log"
    if not log.exists():
        return []
    try:
        return log.read_text(errors="replace").splitlines()[-n:]
    except Exception:
        return []


def _lines_today(lines: list[str]) -> list[str]:
    return [l for l in lines if TODAY_STR in l]


def _jsonl_lines_today(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return sum(1 for l in path.read_text().splitlines() if TODAY_STR in l)
    except Exception:
        return 0


def _jsonl_total(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return sum(1 for l in path.read_text().splitlines() if l.strip())
    except Exception:
        return 0


def _today_midnight_ts() -> float:
    return NOW.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


# ── Feature checks ─────────────────────────────────────────────────────────────
# Each returns (status, detail).  status ∈ {"OK", "DEGRADED", "BROKEN", "UNKNOWN"}

def check_daily_report() -> tuple[str, str]:
    try:
        flag = DATA / "status" / f"daily_report_sent_{TODAY_STR}.flag"
        if flag.exists():
            return "OK", f"flag present: {flag.name}"
        lines = _lines_today(_read_log_tail())
        if any("Sending daily report" in l and TODAY_STR in l for l in lines):
            return "OK", "log: report sent today"
        # Report window: 4:30 PM ET ≈ 20:30 UTC
        if NOW.hour < 20 or (NOW.hour == 20 and NOW.minute < 30):
            return "DEGRADED", "not yet sent (fires 4:30 PM ET)"
        return "BROKEN", "no flag or log entry after report window"
    except Exception as e:
        return "UNKNOWN", str(e)


def check_email_delivery() -> tuple[str, str]:
    try:
        lines = _lines_today(_read_log_tail())
        hits = [l for l in lines if "status=202" in l or "status=200" in l]
        if hits:
            return "OK", f"{len(hits)} successful SendGrid send(s) today"
        return "DEGRADED", "no status=202/200 in today's log"
    except Exception as e:
        return "UNKNOWN", str(e)


def check_sms_delivery() -> tuple[str, str]:
    try:
        lines = _lines_today(_read_log_tail())
        hits = [l for l in lines if "SMS sent" in l or ("201" in l and "twilio" in l.lower())]
        if hits:
            return "OK", "Twilio SMS confirmed in today's log"
        return "DEGRADED", "no SMS delivery logged today"
    except Exception as e:
        return "UNKNOWN", str(e)


def check_morning_brief() -> tuple[str, str]:
    try:
        path = DATA / "market" / "morning_brief.json"
        if not path.exists():
            return "BROKEN", "morning_brief.json missing"
        age = _age_hours(path)
        data = json.loads(path.read_text())
        brief_date = data.get("date", data.get("generated_at", ""))[:10]
        if brief_date == TODAY_STR:
            return "OK", f"today's brief present (age={age:.1f}h)"
        if age and age < 24:
            return "DEGRADED", f"fresh file but dated {brief_date}"
        return "BROKEN", f"stale: dated {brief_date}, age={age:.0f}h"
    except Exception as e:
        return "UNKNOWN", str(e)


def check_signal_scores() -> tuple[str, str]:
    try:
        path = DATA / "market" / "signal_scores.json"
        if not path.exists():
            return "BROKEN", "signal_scores.json missing"
        age = _age_hours(path)
        data = json.loads(path.read_text())
        n = len(data.get("scored_symbols", {}))
        top3 = data.get("top_3", [])
        if age is not None and age < (10 / 60):
            return "OK", f"fresh ({age*60:.0f}min), {n} symbols, top3={top3}"
        if age is not None and age < 1:
            return "DEGRADED", f"age={age*60:.0f}min (>10min), {n} symbols"
        return "BROKEN", f"stale: age={age:.1f}h, {n} symbols"
    except Exception as e:
        return "UNKNOWN", str(e)


def check_a2_decisions() -> tuple[str, str]:
    try:
        dec_dir = DATA / "account2" / "decisions"
        if not dec_dir.exists():
            return "DEGRADED", "decisions dir missing (no A2 cycle run yet)"
        all_files = list(dec_dir.glob("a2_dec_*.json"))
        today_files = [f for f in all_files if TODAY_STR in f.stem]
        if today_files:
            return "OK", f"{len(today_files)} decision(s) today, {len(all_files)} total"
        if all_files:
            return "DEGRADED", f"0 decisions today, {len(all_files)} total on file"
        return "DEGRADED", "no A2 decisions written yet"
    except Exception as e:
        return "UNKNOWN", str(e)


def check_decision_outcomes() -> tuple[str, str]:
    try:
        path = DATA / "analytics" / "decision_outcomes.jsonl"
        if not path.exists():
            return "BROKEN", "decision_outcomes.jsonl missing"
        today = _jsonl_lines_today(path)
        total = _jsonl_total(path)
        if today > 0:
            return "OK", f"{today} outcome(s) today, {total} total"
        if total > 0:
            return "DEGRADED", f"0 outcomes today ({total} total — backfill runs 4:30 PM)"
        return "DEGRADED", "no outcome records yet"
    except Exception as e:
        return "UNKNOWN", str(e)


def check_portfolio_allocator() -> tuple[str, str]:
    try:
        path = DATA / "analytics" / "portfolio_allocator_shadow.jsonl"
        if not path.exists():
            return "BROKEN", "portfolio_allocator_shadow.jsonl missing"
        today = _jsonl_lines_today(path)
        total = _jsonl_total(path)
        if today > 0:
            return "OK", f"{today} shadow run(s) today, {total} total"
        if total > 0:
            return "DEGRADED", f"0 allocator runs today ({total} total)"
        return "DEGRADED", "no shadow allocator records yet (feature flag off?)"
    except Exception as e:
        return "UNKNOWN", str(e)


def check_near_miss_log() -> tuple[str, str]:
    try:
        path = DATA / "analytics" / "near_miss_log.jsonl"
        if not path.exists():
            return "BROKEN", "near_miss_log.jsonl missing"
        today = _jsonl_lines_today(path)
        total = _jsonl_total(path)
        if today > 0:
            return "OK", f"{today} near-miss event(s) today, {total} total"
        return "DEGRADED", f"0 events today ({total} total — shadow lane may be quiet)"
    except Exception as e:
        return "UNKNOWN", str(e)


def check_cost_attribution() -> tuple[str, str]:
    try:
        path = DATA / "analytics" / "cost_attribution_spine.jsonl"
        if not path.exists():
            return "BROKEN", "cost_attribution_spine.jsonl missing"
        today = _jsonl_lines_today(path)
        total = _jsonl_total(path)
        if today > 0:
            return "OK", f"{today} record(s) today, {total} total"
        return "DEGRADED", f"0 attribution records today (enable_cost_attribution_spine?)"
    except Exception as e:
        return "UNKNOWN", str(e)


def check_weekly_review() -> tuple[str, str]:
    try:
        reports_dir = DATA / "reports"
        if not reports_dir.exists():
            return "BROKEN", "data/reports dir missing"
        reports = sorted(reports_dir.glob("weekly_review_*.md"))
        if not reports:
            return "DEGRADED", "no weekly_review_*.md found"
        latest = reports[-1]
        age = _age_hours(latest)
        if age is not None and age < 7 * 24:
            return "OK", f"latest: {latest.name} (age={age/24:.1f}d)"
        return "DEGRADED", f"latest: {latest.name} (age={age/24:.0f}d, >7 days old)"
    except Exception as e:
        return "UNKNOWN", str(e)


def check_sector_perf() -> tuple[str, str]:
    try:
        path = DATA / "market" / "sector_perf.json"
        if not path.exists():
            return "BROKEN", "sector_perf.json missing"
        age = _age_hours(path)
        data = json.loads(path.read_text())
        n = len(data) if isinstance(data, dict) else 0
        if age is not None and age < 24:
            return "OK", f"age={age:.1f}h, {n} sector(s)"
        return "BROKEN", f"stale: age={age:.0f}h"
    except Exception as e:
        return "UNKNOWN", str(e)


def check_macro_snapshot() -> tuple[str, str]:
    try:
        path = DATA / "market" / "macro_snapshot.json"
        if not path.exists():
            return "BROKEN", "macro_snapshot.json missing"
        age = _age_hours(path)
        if age is not None and age < 24:
            return "OK", f"age={age:.1f}h"
        return "BROKEN", f"stale: age={age:.0f}h"
    except Exception as e:
        return "UNKNOWN", str(e)


def check_earnings_calendar() -> tuple[str, str]:
    try:
        path = DATA / "market" / "earnings_calendar.json"
        if not path.exists():
            return "BROKEN", "earnings_calendar.json missing"
        age = _age_hours(path)
        data = json.loads(path.read_text())
        events = data.get("events", data) if isinstance(data, dict) else data
        n = len(events) if isinstance(events, list) else 0
        if age is not None and age < 24:
            return "OK", f"age={age:.1f}h, {n} event(s)"
        return "DEGRADED", f"stale: age={age:.0f}h"
    except Exception as e:
        return "UNKNOWN", str(e)


def check_premarket_movers() -> tuple[str, str]:
    try:
        path = DATA / "market" / "premarket_movers.json"
        if not path.exists():
            return "BROKEN", "premarket_movers.json missing"
        age = _age_hours(path)
        if age is not None and age < 24:
            return "OK", f"age={age:.1f}h"
        return "DEGRADED", f"stale: age={age:.0f}h"
    except Exception as e:
        return "UNKNOWN", str(e)


def check_iv_history() -> tuple[str, str]:
    try:
        iv_dir = DATA / "options" / "iv_history"
        if not iv_dir.exists():
            return "BROKEN", "iv_history dir missing"
        all_files = list(iv_dir.glob("*.json"))
        midnight = _today_midnight_ts()
        updated_today = [f for f in all_files if f.stat().st_mtime >= midnight]
        if updated_today:
            return "OK", f"{len(updated_today)}/{len(all_files)} IV files updated today"
        if all_files:
            return "DEGRADED", f"{len(all_files)} files exist, none updated today"
        return "BROKEN", "no IV history files found"
    except Exception as e:
        return "UNKNOWN", str(e)


def check_insider_intelligence() -> tuple[str, str]:
    try:
        path = DATA / "insider" / "congressional_trades.json"
        if not path.exists():
            return "BROKEN", "congressional_trades.json missing"
        age = _age_hours(path)
        if age is not None and age < 48:
            return "OK", f"age={age:.1f}h (refreshes every 4h weekdays)"
        return "DEGRADED", f"stale: age={age:.0f}h"
    except Exception as e:
        return "UNKNOWN", str(e)


def check_macro_rates() -> tuple[str, str]:
    try:
        path = DATA / "macro_intelligence" / "rates.json"
        if not path.exists():
            return "BROKEN", "rates.json missing"
        age = _age_hours(path)
        if age is not None and age < 24:
            return "OK", f"age={age:.1f}h"
        return "DEGRADED", f"stale: age={age:.0f}h"
    except Exception as e:
        return "UNKNOWN", str(e)


def check_macro_commodities() -> tuple[str, str]:
    try:
        path = DATA / "macro_intelligence" / "commodities.json"
        if not path.exists():
            return "BROKEN", "commodities.json missing"
        age = _age_hours(path)
        if age is not None and age < 24:
            return "OK", f"age={age:.1f}h"
        return "DEGRADED", f"stale: age={age:.0f}h"
    except Exception as e:
        return "UNKNOWN", str(e)


def check_macro_wire() -> tuple[str, str]:
    try:
        path = DATA / "macro_wire" / "live_cache.json"
        if not path.exists():
            return "BROKEN", "macro_wire/live_cache.json missing"
        age = _age_hours(path)
        data = json.loads(path.read_text())
        items = data.get("items", data.get("headlines", [])) if isinstance(data, dict) else []
        n = len(items)
        if age is not None and age < 2:
            return "OK", f"age={age*60:.0f}min, {n} item(s)"
        if age is not None and age < 6:
            return "DEGRADED", f"age={age:.1f}h (>2h threshold)"
        return "BROKEN", f"stale: age={age:.0f}h"
    except Exception as e:
        return "UNKNOWN", str(e)


def check_macro_wire_digest() -> tuple[str, str]:
    try:
        path = DATA / "macro_wire" / "daily_digest" / f"{TODAY_STR}.json"
        if path.exists():
            data = json.loads(path.read_text())
            items = data.get("items", data.get("events", [])) if isinstance(data, dict) else []
            return "OK", f"today's digest present, {len(items)} item(s)"
        if NOW.weekday() >= 5:
            return "DEGRADED", "weekend — digest not expected"
        # Digest written at 4:00 PM ET ≈ 20:00 UTC
        if NOW.hour < 20:
            return "DEGRADED", "not yet written (scheduled 4:00 PM ET)"
        return "BROKEN", f"expected {path.name} after 4 PM ET — missing"
    except Exception as e:
        return "UNKNOWN", str(e)


def check_global_indices() -> tuple[str, str]:
    try:
        path = DATA / "market" / "global_indices.json"
        if not path.exists():
            return "BROKEN", "global_indices.json missing"
        age = _age_hours(path)
        data = json.loads(path.read_text())
        n = len(data) if isinstance(data, dict) else 0
        if age is not None and age < 24:
            return "OK", f"age={age:.1f}h, {n} indices"
        return "BROKEN", f"stale: age={age:.0f}h"
    except Exception as e:
        return "UNKNOWN", str(e)


def check_readiness_status() -> tuple[str, str]:
    try:
        path = DATA / "reports" / "readiness_status_latest.json"
        if not path.exists():
            return "BROKEN", "readiness_status_latest.json missing"
        age = _age_hours(path)
        data = json.loads(path.read_text())
        gates    = data.get("gates", {})
        n_gates  = len(gates) if isinstance(gates, dict) else 0
        n_fail   = sum(1 for v in gates.values() if "FAIL" in str(v)) if isinstance(gates, dict) else 0
        n_pass   = n_gates - n_fail
        if age is not None and age < 24:
            if n_fail:
                fail_list = [k for k, v in gates.items() if "FAIL" in str(v)]
                return "DEGRADED", f"age={age:.1f}h, {n_fail} FAIL: {', '.join(fail_list[:3])}"
            return "OK", f"age={age:.1f}h, {n_pass}/{n_gates} gates PASS"
        return "DEGRADED", f"stale: age={age:.0f}h"
    except Exception as e:
        return "UNKNOWN", str(e)


def check_trade_memory() -> tuple[str, str]:
    try:
        path = DATA / "trade_memory" / "chroma.sqlite3"
        if not path.exists():
            return "BROKEN", "chroma.sqlite3 missing"
        size_kb = path.stat().st_size / 1024
        age = _age_hours(path)
        if size_kb >= 1:
            return "OK", f"size={size_kb:.0f}KB, age={age:.1f}h"
        return "DEGRADED", f"file exists but tiny ({size_kb:.1f}KB) — may be uninitialised"
    except Exception as e:
        return "UNKNOWN", str(e)


def check_divergence_log() -> tuple[str, str]:
    try:
        path = DATA / "analytics" / "divergence_log.jsonl"
        if not path.exists():
            return "BROKEN", "divergence_log.jsonl missing"
        total = _jsonl_total(path)
        today = _jsonl_lines_today(path)
        return "OK", f"{total} total divergence records, {today} today"
    except Exception as e:
        return "UNKNOWN", str(e)


def check_incident_log() -> tuple[str, str]:
    try:
        path = DATA / "analytics" / "incident_log.jsonl"
        if not path.exists():
            return "BROKEN", "incident_log.jsonl missing"
        total = _jsonl_total(path)
        today = _jsonl_lines_today(path)
        if today > 0:
            return "DEGRADED", f"{today} incident(s) logged TODAY — review required"
        return "OK", f"{total} total incident records, 0 today"
    except Exception as e:
        return "UNKNOWN", str(e)


def check_sonnet_gate() -> tuple[str, str]:
    try:
        lines = _lines_today(_read_log_tail())
        gate_lines = [l for l in lines if "[GATE]" in l]
        if not gate_lines:
            return "DEGRADED", "no [GATE] log entries today — bot may not be running"
        skips  = sum(1 for l in gate_lines if "SKIP" in l)
        fires  = sum(1 for l in gate_lines if "SONNET triggered" in l or "FULL" in l)
        return "OK", f"{len(gate_lines)} gate events: {fires} Sonnet fire, {skips} skip"
    except Exception as e:
        return "UNKNOWN", str(e)


# ── Feature registry (25 features) ───────────────────────────────────────────

FEATURES: list[tuple[str, object]] = [
    ("Daily Report Sent",           check_daily_report),
    ("Email Delivery (SendGrid)",   check_email_delivery),
    ("SMS Delivery (Twilio)",       check_sms_delivery),
    ("Morning Conviction Brief",    check_morning_brief),
    ("Signal Scores (A1 → A2)",     check_signal_scores),
    ("A2 Decision Records",         check_a2_decisions),
    ("Decision Outcomes Log",       check_decision_outcomes),
    ("Portfolio Allocator Shadow",  check_portfolio_allocator),
    ("Near-Miss Log (Shadow Lane)", check_near_miss_log),
    ("Cost Attribution Spine",      check_cost_attribution),
    ("Weekly Review Report",        check_weekly_review),
    ("Sector Performance",          check_sector_perf),
    ("Macro Snapshot",              check_macro_snapshot),
    ("Earnings Calendar",           check_earnings_calendar),
    ("Premarket Movers",            check_premarket_movers),
    ("IV History Refresh (A2)",     check_iv_history),
    ("Insider Intelligence",        check_insider_intelligence),
    ("Macro Intelligence: Rates",   check_macro_rates),
    ("Macro Intelligence: Commod.", check_macro_commodities),
    ("Macro Wire Live Cache",       check_macro_wire),
    ("Macro Wire Daily Digest",     check_macro_wire_digest),
    ("Global Indices",              check_global_indices),
    ("Readiness Status",            check_readiness_status),
    ("ChromaDB Trade Memory",       check_trade_memory),
    ("Divergence Log",              check_divergence_log),
    ("Incident Log",                check_incident_log),
    ("Sonnet Gate Activity",        check_sonnet_gate),
]

ICONS = {"OK": "✅", "DEGRADED": "⚠️ ", "BROKEN": "❌", "UNKNOWN": "⏳"}

# Terminal width for most status strings (emoji may render as 2-wide in terminals)
_STATUS_LABELS = {"OK": "OK      ", "DEGRADED": "DEGRADED", "BROKEN": "BROKEN  ", "UNKNOWN": "UNKNOWN "}


def _render_table(results: list[tuple[str, str, str]]) -> str:
    name_w   = max(len(n) for n, _, _ in results)
    detail_w = max(len(d) for _, _, d in results)
    # columns: name | status (fixed 12) | detail
    C1, C2, C3 = name_w, 12, detail_w

    def row(n="", s="", d="", sep="│"):
        return f"{sep} {n:<{C1}} {sep} {s:<{C2}} {sep} {d:<{C3}} {sep}"

    H = "─"
    lines = [
        f"┌{'─'*(C1+2)}┬{'─'*(C2+2)}┬{'─'*(C3+2)}┐",
        row("Feature", "Status", "Detail"),
        f"╞{'═'*(C1+2)}╪{'═'*(C2+2)}╪{'═'*(C3+2)}╡",
    ]
    for i, (name, status, detail) in enumerate(results):
        icon  = ICONS.get(status, "⏳")
        slabel = _STATUS_LABELS.get(status, status)
        status_cell = f"{icon} {slabel}"
        lines.append(row(name, status_cell, detail))
        if i < len(results) - 1:
            lines.append(f"├{'─'*(C1+2)}┼{'─'*(C2+2)}┼{'─'*(C3+2)}┤")
    lines.append(f"└{'─'*(C1+2)}┴{'─'*(C2+2)}┴{'─'*(C3+2)}┘")
    return "\n".join(lines)


def main() -> int:
    print(f"\n  BullBearBot Feature Audit  ·  {TODAY_STR}  {NOW.strftime('%H:%M:%S')}")
    print(f"  Base: {BASE_DIR}\n")

    results: list[tuple[str, str, str]] = []
    for name, fn in FEATURES:
        try:
            status, detail = fn()
        except Exception as exc:
            status, detail = "UNKNOWN", f"check raised: {exc}"
        results.append((name, status, detail))

    print(_render_table(results))

    ok       = [r for r in results if r[1] == "OK"]
    degraded = [r for r in results if r[1] == "DEGRADED"]
    broken   = [r for r in results if r[1] == "BROKEN"]
    unknown  = [r for r in results if r[1] == "UNKNOWN"]

    print()
    W = 54
    print(f"  ┌{'─'*W}┐")
    print(f"  │{'Summary':^{W}}│")
    print(f"  ├{'─'*W}┤")
    print(f"  │  ✅ OK:        {len(ok):>3}     ❌ BROKEN:   {len(broken):>3}{'':>16}│")
    print(f"  │  ⚠️  DEGRADED:  {len(degraded):>3}     ⏳ UNKNOWN:  {len(unknown):>3}{'':>16}│")

    if broken:
        print(f"  ├{'─'*W}┤")
        print(f"  │  BROKEN:{'':>{W-9}}│")
        for name, _, detail in broken:
            line = f"    ❌ {name}"
            print(f"  │{line:<{W}}│")

    if degraded:
        print(f"  ├{'─'*W}┤")
        print(f"  │  DEGRADED:{'':>{W-11}}│")
        for name, _, detail in degraded:
            line = f"    ⚠️  {name}"
            print(f"  │{line:<{W}}│")

    print(f"  └{'─'*W}┘")
    print()

    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
