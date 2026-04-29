#!/usr/bin/env python3
"""BullBearBot health dashboard — port 8080."""

import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request

load_dotenv(Path(__file__).parent.parent / ".env")

# ── Config ────────────────────────────────────────────────────────────────────
BOT_DIR = Path(__file__).parent.parent
DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "bullbearbot")
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8080"))
ALPACA_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_KEY_OPT = os.getenv("ALPACA_API_KEY_OPTIONS", "")
ALPACA_SECRET_OPT = os.getenv("ALPACA_SECRET_KEY_OPTIONS", "")

app = Flask(__name__)

if DASHBOARD_USER == "admin" and DASHBOARD_PASSWORD == "bullbearbot":
    app.logger.warning("Default credentials in use — set DASHBOARD_USER/DASHBOARD_PASSWORD in .env")

# ── In-memory cache ───────────────────────────────────────────────────────────
_cache: dict = {}


def _cached(key: str, ttl: int = 60):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            now = time.time()
            entry = _cache.get(key)
            if entry and now - entry["ts"] < ttl:
                return entry["data"]
            result = fn(*args, **kwargs)
            _cache[key] = {"ts": now, "data": result}
            return result
        return wrapper
    return decorator


# ── HTTP Basic Auth ───────────────────────────────────────────────────────────
def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != DASHBOARD_USER or auth.password != DASHBOARD_PASSWORD:
            return Response(
                "Authentication required",
                401,
                {"WWW-Authenticate": 'Basic realm="BullBearBot"'},
            )
        return f(*args, **kwargs)
    return decorated


# ── File helpers ──────────────────────────────────────────────────────────────
def _rj(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default if default is not None else {}


def _jsonl_last(path: Path, n: int = 1):
    try:
        lines = [ln for ln in path.read_text().strip().splitlines() if ln.strip()]
        return [json.loads(ln) for ln in lines[-n:]]
    except Exception:
        return []


# ── Alpaca data (cached 60 s) ─────────────────────────────────────────────────
@_cached("a1", ttl=60)
def _alpaca_a1():
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import OrderSide, OrderStatus, QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        c = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True)
        acc = c.get_account()
        pos = c.get_all_positions()
        try:
            orders = c.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=200))
        except Exception:
            orders = []
        buys_today = 0
        sells_today = 0
        try:
            today_midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            closed = c.get_orders(GetOrdersRequest(
                status=QueryOrderStatus.CLOSED, after=today_midnight, limit=200
            ))
            filled = [o for o in closed if o.status == OrderStatus.FILLED]
            buys_today = sum(1 for o in filled if o.side == OrderSide.BUY)
            sells_today = sum(1 for o in filled if o.side == OrderSide.SELL)
        except Exception:
            pass
        return {"ok": True, "account": acc, "positions": pos, "orders": orders,
                "buys_today": buys_today, "sells_today": sells_today}
    except Exception as e:
        return {"ok": False, "error": str(e), "account": None, "positions": [], "orders": [],
                "buys_today": 0, "sells_today": 0}


@_cached("a2", ttl=60)
def _alpaca_a2():
    try:
        from alpaca.trading.client import TradingClient

        c = TradingClient(ALPACA_KEY_OPT, ALPACA_SECRET_OPT, paper=True)
        acc = c.get_account()
        pos = c.get_all_positions()
        return {"ok": True, "account": acc, "positions": pos}
    except Exception as e:
        return {"ok": False, "error": str(e), "account": None, "positions": []}


# ── Bot file readers ──────────────────────────────────────────────────────────
def _last_decision():
    try:
        decisions = json.loads((BOT_DIR / "memory/decisions.json").read_text())
        for dec in reversed(decisions):
            r = dec.get("reasoning", "")
            if r and "gate skipped" not in r:
                return dec
        return decisions[-1] if decisions else {}
    except Exception:
        return {}


def _todays_trades():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = []
    try:
        for line in (BOT_DIR / "logs/trades.jsonl").read_text().strip().splitlines():
            try:
                t = json.loads(line)
                if str(t.get("ts", "")).startswith(today):
                    out.append(t)
            except Exception:
                pass
    except Exception:
        pass
    return out


def _recent_errors(n_lines: int = 300, max_out: int = 5):
    try:
        lines = (BOT_DIR / "logs/bot.log").read_text().splitlines()[-n_lines:]
        errs = [ln.strip() for ln in lines if any(k in ln for k in ("  ERROR  ", "  WARNING  ", "  CRITICAL  "))]
        return errs[-max_out:]
    except Exception:
        return []


def _git_hash():
    # Read directly from .git files — no subprocess, no PATH issues
    try:
        head = (BOT_DIR / ".git/HEAD").read_text().strip()
        if head.startswith("ref: "):
            ref_path = BOT_DIR / ".git" / head[5:]
            full = ref_path.read_text().strip()
        else:
            full = head
        return full[:7] if len(full) >= 7 else full
    except Exception:
        try:
            r = subprocess.run(
                ["/usr/bin/git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, cwd=BOT_DIR, timeout=5,
            )
            return r.stdout.strip() or "unknown"
        except Exception:
            return "unknown"


def _service_uptime():
    try:
        r = subprocess.run(
            ["systemctl", "show", "trading-bot", "--property=ActiveEnterTimestamp"],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip().split("=", 1)[-1]
    except Exception:
        return "unknown"


def _earnings_flags():
    cal = _rj(BOT_DIR / "data/market/earnings_calendar.json", default={})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    flags: dict[str, str] = {}
    events = cal if isinstance(cal, list) else cal.get("events", cal.get("earnings", []))
    for ev in (events if isinstance(events, list) else []):
        sym = ev.get("symbol", ev.get("ticker", ""))
        date = ev.get("date", ev.get("report_date", ""))
        if sym and date:
            if date.startswith(today):
                flags[sym] = "EARNINGS TODAY"
            elif date.startswith(tomorrow):
                flags[sym] = "EARNINGS TOMORROW"
    return flags


def _stop_map(orders):
    stops: dict[str, float] = {}
    for o in orders:
        try:
            side = str(getattr(o, "side", "")).lower()
            otype = str(getattr(o, "type", "")).lower()
            sym = getattr(o, "symbol", "")
            sp = getattr(o, "stop_price", None)
            if "sell" in side and ("stop" in otype or "trail" in otype) and sp:
                stops[sym] = float(sp)
        except Exception:
            pass
    return stops


# ── Formatting helpers ────────────────────────────────────────────────────────
ET_OFFSET = timedelta(hours=-4)  # EDT; adjust to -5 in winter


def _to_et(ts_str: str) -> str:
    if not ts_str:
        return "—"
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        et = ts + ET_OFFSET
        return et.strftime("%-m/%-d %-I:%M %p ET")
    except Exception:
        return ts_str[:16]


def _now_et() -> str:
    return (datetime.now(timezone.utc) + ET_OFFSET).strftime("%-m/%-d %-I:%M:%S %p ET")


def _fm(v, prefix="$") -> str:
    try:
        f = float(v)
        sign = "-" if f < 0 else ""
        return f"{sign}{prefix}{abs(f):,.2f}"
    except Exception:
        return "N/A"


def _fp(v, decimals: int = 2) -> str:
    try:
        return f"{float(v):.{decimals}f}%"
    except Exception:
        return "N/A"


def _mode_color(mode_str: str) -> str:
    m = mode_str.lower()
    if m == "normal":
        return "#3fb950"
    if "halt" in m:
        return "#f85149"
    if "risk" in m or "contain" in m:
        return "#d29922"
    if "reconcile" in m:
        return "#d29922"
    return "#8b949e"


# ── HTML renderer ─────────────────────────────────────────────────────────────
def _html(status: dict) -> str:  # noqa: C901
    a1d = status["a1"]
    a2d = status["a2"]
    a1_acc = a1d.get("account")
    a2_acc = a2d.get("account")
    positions = status["positions"]
    gate = status["gate"]
    costs = status["costs"]
    decision = status["decision"]
    trades = status["trades"]
    log_errors = status["log_errors"]
    a1_mode_obj = status["a1_mode"]
    a2_mode_obj = status["a2_mode"]
    shadow = status["shadow"]
    a2_dec = status["a2_decision"]

    # Mode strings
    a1_mode = a1_mode_obj.get("mode", "unknown").upper()
    a2_mode = a2_mode_obj.get("mode", "unknown").upper()
    a1_color = _mode_color(a1_mode)
    a2_color = _mode_color(a2_mode)

    # A1 account numbers
    if a1_acc:
        a1_equity = _fm(a1_acc.equity)
        a1_cash = _fm(a1_acc.cash)
        a1_bp = _fm(a1_acc.buying_power)
        a1_unreal = sum(float(p.get("unreal_pl", 0)) for p in positions)
        a1_unreal_str = ("+" if a1_unreal >= 0 else "") + _fm(a1_unreal)
        a1_eq_float = float(a1_acc.equity or 0)
        a1_unreal_pct = (a1_unreal / a1_eq_float * 100) if a1_eq_float else 0
        a1_unreal_pct_str = ("+" if a1_unreal_pct >= 0 else "") + _fp(a1_unreal_pct)
        a1_pos_count = len(positions)
    else:
        a1_equity = a1_cash = a1_bp = "N/A"
        a1_unreal_str = a1_unreal_pct_str = "N/A"
        a1_pos_count = 0
        a1_eq_float = 0

    # A2 account numbers
    if a2_acc:
        a2_equity = _fm(a2_acc.equity)
        a2_cash = _fm(a2_acc.cash)
        a2_bp = _fm(a2_acc.buying_power)
        a2_pos_count = len(a2d.get("positions", []))
    else:
        a2_equity = a2_cash = a2_bp = "N/A"
        a2_pos_count = 0

    # Gate / Sonnet stats
    sonnet_calls = gate.get("total_calls_today", "—")
    sonnet_skips = gate.get("total_skips_today", "—")
    last_sonnet_ts = _to_et(gate.get("last_sonnet_call_utc", ""))
    last_regime = gate.get("last_regime", "—")
    last_vix_pf = "—"
    pf_entries = _jsonl_last(BOT_DIR / "data/status/preflight_log.jsonl", n=1)
    if pf_entries:
        pf = pf_entries[0]
        for chk in pf.get("checks", []):
            if chk.get("name") == "vix_gate":
                msg = chk.get("message", "")
                if "VIX=" in msg:
                    last_vix_pf = msg.split("VIX=")[1].split()[0]

    # Decision reasoning (2 sentence max)
    reasoning_raw = decision.get("reasoning", "")
    sentences = reasoning_raw.split(". ")
    reasoning = ". ".join(sentences[:2]) + ("." if len(sentences) > 1 else "")
    if len(reasoning) > 280:
        reasoning = reasoning[:277] + "…"
    last_dec_ts = _to_et(decision.get("ts", ""))
    regime_score = decision.get("regime_score", "—")
    dec_session = decision.get("session", "—")

    # Today's trades breakdown — buys/sells from Alpaca filled orders (Bug 1 fix)
    buys_today = status["buys_today"]
    sells_today = status["sells_today"]
    rejected = [t for t in trades if t.get("status") == "rejected"]
    trail_stops = [t for t in trades if t.get("event") == "trail_stop"]

    # Positions table rows
    positions_html = ""
    for p in sorted(positions, key=lambda x: -abs(x.get("unreal_pl", 0))):
        sym = p["symbol"]
        pl = p.get("unreal_pl", 0)
        plpc = p.get("unreal_plpc", 0)
        stop = p.get("stop")
        gap = p.get("gap_to_stop")
        pct_bp = p.get("pct_of_bp", 0)
        earnings_flag = p.get("earnings", "")
        oversize = p.get("oversize", False)

        row_bg = ""
        if gap is not None and gap < 2.0:
            row_bg = "background:#2d2208;"
        pl_color = "#3fb950" if pl >= 0 else "#f85149"
        pl_sign = "+" if pl >= 0 else ""

        flags_html = ""
        if earnings_flag:
            flags_html += f' <span class="flag flag-earn">{earnings_flag}</span>'
        if oversize:
            flags_html += ' <span class="flag flag-over">OVERSIZE</span>'

        stop_str = _fm(stop) if stop else "—"
        gap_str = f"{gap:.1f}%" if gap is not None else "—"
        gap_color = "#d29922" if gap is not None and gap < 2.0 else "#e6edf3"

        positions_html += f"""
        <tr style="{row_bg}">
          <td><b>{sym}</b>{flags_html}</td>
          <td>{int(p.get("qty", 0))}</td>
          <td>{_fm(p.get("entry"))}</td>
          <td>{_fm(p.get("current"))}</td>
          <td style="color:{pl_color}">{pl_sign}{_fm(pl)}</td>
          <td style="color:{pl_color}">{pl_sign}{_fp(plpc)}</td>
          <td>{stop_str}</td>
          <td style="color:{gap_color}">{gap_str}</td>
          <td>{_fp(pct_bp)}</td>
        </tr>"""

    # Active flags
    flags_list = []
    if a1_mode != "NORMAL":
        flags_list.append(f'<div class="alert alert-red">⚠ A1 MODE: {a1_mode} — {a1_mode_obj.get("reason_detail","")[:120]}</div>')
    if a2_mode != "NORMAL":
        flags_list.append(f'<div class="alert alert-orange">⚠ A2 MODE: {a2_mode}</div>')
    for p in positions:
        if p.get("earnings"):
            flags_list.append(f'<div class="alert alert-orange">📅 {p["symbol"]}: {p["earnings"]}</div>')
        if p.get("oversize"):
            flags_list.append(f'<div class="alert alert-orange">⚡ {p["symbol"]}: OVERSIZE ({_fp(p.get("pct_of_bp",0))} of BP)</div>')
        if p.get("gap_to_stop") is not None and p["gap_to_stop"] < 2.0:
            flags_list.append(f'<div class="alert alert-orange">🔴 {p["symbol"]}: stop gap only {p["gap_to_stop"]:.1f}% — near stop</div>')
    if not flags_list:
        flags_list.append('<div class="alert alert-green">✓ No active flags</div>')
    flags_html_block = "\n".join(flags_list)

    # Shadow status
    alloc_status = shadow.get("shadow_systems", {}).get("portfolio_allocator", {})
    alloc_last = _to_et(alloc_status.get("last_run_at", ""))
    alloc_st = alloc_status.get("status", "—")

    # A2 last decision — reads from data/account2/decisions/ (new stage pipeline format, Bug 3 fix)
    a2_ts = _to_et(a2_dec.get("built_at", "")) if a2_dec else "—"
    _a2_result = a2_dec.get("execution_result", "—") if a2_dec else "—"
    _a2_reason = a2_dec.get("no_trade_reason", "") if a2_dec else ""
    a2_action_str = f"{_a2_result} ({_a2_reason})" if _a2_reason else _a2_result
    a2_session = a2_dec.get("session_tier", "") if a2_dec else ""
    a2_reasoning = (f"{a2_session} · {_a2_reason}" if a2_session and _a2_reason
                    else a2_session or _a2_reason or "—")[:100]

    # Costs
    daily_cost = costs.get("daily_cost", 0)
    daily_calls = costs.get("daily_calls", 0)
    all_time = costs.get("all_time_cost", 0)

    # Log errors
    errors_html = ""
    if log_errors:
        for err in log_errors:
            level_color = "#f85149" if "  ERROR  " in err or "  CRITICAL  " in err else "#d29922"
            short = err[-180:]
            errors_html += f'<div class="log-line" style="color:{level_color}">{short}</div>'
    else:
        errors_html = '<div class="log-line" style="color:#3fb950">No recent warnings or errors</div>'

    # Git / uptime
    git_hash = status["git_hash"]
    svc_uptime = status["service_uptime"]

    now_et = _now_et()

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="60">
  <title>BullBearBot</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #0d1117; color: #e6edf3; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; font-size: 15px; line-height: 1.5; }}
    .container {{ max-width: 900px; margin: 0 auto; padding: 12px; }}

    /* Header */
    .header {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px 16px; margin-bottom: 12px; }}
    .header-top {{ display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 8px; }}
    .header-title {{ font-size: 20px; font-weight: 700; color: #58a6ff; letter-spacing: 0.5px; }}
    .badges {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    .badge {{ display: inline-block; padding: 3px 10px; border-radius: 20px; font-size: 13px; font-weight: 600; color: #0d1117; }}
    .header-meta {{ font-size: 13px; color: #8b949e; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 4px; }}

    /* Cards */
    .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px 16px; margin-bottom: 10px; }}
    .section-label {{ font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; color: #8b949e; margin: 16px 0 8px; }}

    /* Two-column account summary */
    .accounts {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
    @media (max-width: 480px) {{ .accounts {{ grid-template-columns: 1fr; }} }}
    .acct-title {{ font-size: 13px; font-weight: 700; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 10px; }}
    .acct-row {{ display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid #21262d; font-size: 14px; }}
    .acct-row:last-child {{ border-bottom: none; }}
    .acct-label {{ color: #8b949e; }}
    .acct-val {{ font-weight: 600; }}
    .green {{ color: #3fb950; }}
    .red {{ color: #f85149; }}
    .orange {{ color: #d29922; }}
    .muted {{ color: #8b949e; }}

    /* Positions table */
    .table-wrap {{ overflow-x: auto; -webkit-overflow-scrolling: touch; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; white-space: nowrap; }}
    th {{ background: #21262d; color: #8b949e; font-weight: 600; text-align: right; padding: 8px 10px; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }}
    th:first-child {{ text-align: left; }}
    td {{ padding: 9px 10px; text-align: right; border-bottom: 1px solid #21262d; }}
    td:first-child {{ text-align: left; }}
    tr:last-child td {{ border-bottom: none; }}
    tr:hover td {{ background: #1c2128; }}
    .flag {{ display: inline-block; font-size: 10px; font-weight: 700; padding: 1px 5px; border-radius: 3px; margin-left: 4px; vertical-align: middle; }}
    .flag-earn {{ background: #4a2e00; color: #d29922; }}
    .flag-over {{ background: #3d1a1a; color: #f85149; }}

    /* Alerts */
    .alert {{ padding: 9px 12px; border-radius: 6px; margin-bottom: 6px; font-size: 14px; }}
    .alert-green {{ background: #0d2018; border: 1px solid #1a4028; color: #3fb950; }}
    .alert-orange {{ background: #2d2208; border: 1px solid #4a3808; color: #d29922; }}
    .alert-red {{ background: #2d0f0f; border: 1px solid #5c1a1a; color: #f85149; }}

    /* Stat grid */
    .stat-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; }}
    .stat-box {{ background: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 10px 12px; }}
    .stat-label {{ font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }}
    .stat-val {{ font-size: 18px; font-weight: 700; margin-top: 2px; }}

    /* Reasoning block */
    .reasoning {{ background: #0d1117; border-left: 3px solid #58a6ff; padding: 10px 14px; border-radius: 0 6px 6px 0; font-size: 14px; color: #c9d1d9; font-style: italic; margin: 10px 0; }}

    /* Log lines */
    .log-line {{ font-family: "SF Mono", "Fira Code", monospace; font-size: 11px; padding: 3px 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}

    /* Kv pairs */
    .kv {{ display: flex; justify-content: space-between; padding: 5px 0; border-bottom: 1px solid #21262d; font-size: 14px; }}
    .kv:last-child {{ border-bottom: none; }}
    .kv-label {{ color: #8b949e; }}
    .kv-val {{ font-weight: 600; text-align: right; }}
  </style>
</head>
<body>
<div class="container">

  <!-- HEADER -->
  <div class="header">
    <div class="header-top">
      <span class="header-title">🐂🐻 BullBearBot</span>
      <div class="badges">
        <span class="badge" style="background:{a1_color}">A1: {a1_mode}</span>
        <span class="badge" style="background:{a2_color}">A2: {a2_mode}</span>
      </div>
    </div>
    <div class="header-meta">
      <span>Updated {now_et}</span>
      <span>Refresh in <span id="cd">60</span>s</span>
    </div>
  </div>

  <!-- ACCOUNTS -->
  <div class="section-label">Accounts</div>
  <div class="accounts">
    <div class="card">
      <div class="acct-title">A1 — Equities</div>
      <div class="acct-row"><span class="acct-label">Equity</span><span class="acct-val">{a1_equity}</span></div>
      <div class="acct-row"><span class="acct-label">Cash</span><span class="acct-val">{a1_cash}</span></div>
      <div class="acct-row"><span class="acct-label">Buying Power</span><span class="acct-val">{a1_bp}</span></div>
      <div class="acct-row"><span class="acct-label">Positions</span><span class="acct-val">{a1_pos_count}</span></div>
      <div class="acct-row"><span class="acct-label">Unrealized P&L</span>
        <span class="acct-val" style="color:{'#3fb950' if a1_unreal_str.startswith('+') else '#f85149'}">{a1_unreal_str} ({a1_unreal_pct_str})</span>
      </div>
    </div>
    <div class="card">
      <div class="acct-title">A2 — Options</div>
      <div class="acct-row"><span class="acct-label">Equity</span><span class="acct-val">{a2_equity}</span></div>
      <div class="acct-row"><span class="acct-label">Cash</span><span class="acct-val">{a2_cash}</span></div>
      <div class="acct-row"><span class="acct-label">Buying Power</span><span class="acct-val">{a2_bp}</span></div>
      <div class="acct-row"><span class="acct-label">Positions</span><span class="acct-val">{a2_pos_count}</span></div>
      <div class="acct-row"><span class="acct-label">Last cycle</span><span class="acct-val muted">{a2_ts}</span></div>
    </div>
  </div>

  <!-- POSITIONS -->
  <div class="section-label">Positions — A1 ({a1_pos_count} open)</div>
  <div class="card" style="padding:0 0 4px">
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Symbol</th><th>Qty</th><th>Entry</th><th>Current</th>
            <th>P&amp;L $</th><th>P&amp;L %</th><th>Stop</th><th>Gap</th><th>% BP</th>
          </tr>
        </thead>
        <tbody>{positions_html}</tbody>
      </table>
    </div>
  </div>

  <!-- TODAY'S ACTIVITY -->
  <div class="section-label">Today's Activity</div>
  <div class="card">
    <div class="stat-grid" style="margin-bottom:12px">
      <div class="stat-box">
        <div class="stat-label">Sonnet Calls</div>
        <div class="stat-val">{sonnet_calls}</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">Skips</div>
        <div class="stat-val muted">{sonnet_skips}</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">Buys Today</div>
        <div class="stat-val green">{buys_today}</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">Sells Today</div>
        <div class="stat-val red">{sells_today}</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">Rejected</div>
        <div class="stat-val orange">{len(rejected)}</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">Stop Trails</div>
        <div class="stat-val">{len(trail_stops)}</div>
      </div>
    </div>
    <div class="kv"><span class="kv-label">Last Sonnet</span><span class="kv-val">{last_sonnet_ts}</span></div>
    <div class="kv"><span class="kv-label">Regime</span><span class="kv-val">{last_regime} (score {regime_score}) · {dec_session}</span></div>
    <div class="kv"><span class="kv-label">VIX</span><span class="kv-val">{last_vix_pf}</span></div>
    <div class="kv"><span class="kv-label">Last decision</span><span class="kv-val muted">{last_dec_ts}</span></div>
    {f'<div class="reasoning">{reasoning}</div>' if reasoning else ''}
  </div>

  <!-- ACTIVE FLAGS -->
  <div class="section-label">Active Flags</div>
  <div>{flags_html_block}</div>

  <!-- A2 STATUS -->
  <div class="section-label">A2 — Options Status</div>
  <div class="card">
    <div class="kv"><span class="kv-label">Mode</span><span class="kv-val" style="color:{a2_color}">{a2_mode}</span></div>
    <div class="kv"><span class="kv-label">Last cycle</span><span class="kv-val">{a2_ts}</span></div>
    <div class="kv"><span class="kv-label">Outcome</span><span class="kv-val">{a2_action_str}</span></div>
    <div class="kv"><span class="kv-label">Reasoning</span><span class="kv-val muted" style="max-width:220px;word-break:break-word;white-space:normal">{a2_reasoning}</span></div>
    <div class="kv"><span class="kv-label">Allocator shadow</span><span class="kv-val">{alloc_st} · {alloc_last}</span></div>
  </div>

  <!-- COSTS -->
  <div class="section-label">Costs Today</div>
  <div class="card">
    <div class="stat-grid">
      <div class="stat-box">
        <div class="stat-label">Today</div>
        <div class="stat-val">{_fm(daily_cost)}</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">Calls</div>
        <div class="stat-val">{daily_calls:,}</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">All-time</div>
        <div class="stat-val">{_fm(all_time)}</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">Proj/month</div>
        <div class="stat-val {'red' if float(daily_cost or 0)*30 > 150 else 'green'}">{_fm(float(daily_cost or 0)*30)}</div>
      </div>
    </div>
  </div>

  <!-- SYSTEM HEALTH -->
  <div class="section-label">System Health</div>
  <div class="card">
    <div class="kv"><span class="kv-label">Git HEAD</span><span class="kv-val" style="font-family:monospace">{git_hash}</span></div>
    <div class="kv"><span class="kv-label">Service up since</span><span class="kv-val">{svc_uptime}</span></div>
    <div class="kv"><span class="kv-label">A1 mode reason</span><span class="kv-val muted" style="max-width:220px;word-break:break-word;white-space:normal">{a1_mode_obj.get("reason_code","—")}</span></div>
    <div style="margin-top:10px;padding-top:10px;border-top:1px solid #21262d">
      <div class="stat-label" style="margin-bottom:6px">Recent Warnings/Errors (last 300 log lines)</div>
      {errors_html}
    </div>
  </div>

  <div style="height:24px"></div>
</div>

<script>
  var secs = 60;
  var el = document.getElementById("cd");
  setInterval(function() {{
    secs -= 1;
    if (secs <= 0) {{ secs = 60; }}
    if (el) el.textContent = secs;
  }}, 1000);
</script>
</body>
</html>"""


def _a2_last_cycle() -> dict:
    """Read most recent A2 decision from data/account2/decisions/ (new stage pipeline format)."""
    try:
        dec_dir = BOT_DIR / "data/account2/decisions"
        files = sorted(dec_dir.glob("a2_dec_*.json"))
        if not files:
            return {}
        return json.loads(files[-1].read_text())
    except Exception:
        return {}


# ── Build status dict ─────────────────────────────────────────────────────────
def _build_status() -> dict:
    a1d = _alpaca_a1()
    a2d = _alpaca_a2()
    a1_acc = a1d.get("account")
    earnings = _earnings_flags()

    positions = []
    if a1_acc:
        equity = float(a1_acc.equity or 0)
        buying_power = float(a1_acc.buying_power or 0)
        denom = buying_power if buying_power else equity  # fall back to equity if BP is 0
        stops = _stop_map(a1d.get("orders", []))
        for p in a1d.get("positions", []):
            sym = p.symbol
            qty = float(p.qty or 0)
            entry = float(p.avg_entry_price or 0)
            current = float(p.current_price or 0)
            market_val = float(p.market_value or 0)
            unreal_pl = float(p.unrealized_pl or 0)
            unreal_plpc = float(p.unrealized_plpc or 0) * 100
            pct_bp = (market_val / denom * 100) if denom else 0
            stop = stops.get(sym)
            gap = ((current - stop) / current * 100) if stop and current else None
            positions.append({
                "symbol": sym, "qty": qty, "entry": entry, "current": current,
                "market_val": market_val, "unreal_pl": unreal_pl,
                "unreal_plpc": unreal_plpc, "pct_of_bp": pct_bp,
                "stop": stop, "gap_to_stop": gap,
                "earnings": earnings.get(sym, ""),
                "oversize": pct_bp > 20,  # flag if >20% of buying power (consistent with risk kernel)
            })

    a2_dec = _a2_last_cycle()

    return {
        "a1": a1d, "a2": a2d,
        "positions": positions,
        "buys_today": a1d.get("buys_today", 0),
        "sells_today": a1d.get("sells_today", 0),
        "a1_mode": _rj(BOT_DIR / "data/runtime/a1_mode.json"),
        "a2_mode": _rj(BOT_DIR / "data/runtime/a2_mode.json"),
        "gate": _rj(BOT_DIR / "data/market/gate_state.json"),
        "costs": _rj(BOT_DIR / "data/costs/daily_costs.json"),
        "shadow": _rj(BOT_DIR / "data/reports/shadow_status_latest.json"),
        "decision": _last_decision(),
        "trades": _todays_trades(),
        "log_errors": _recent_errors(),
        "git_hash": _git_hash(),
        "service_uptime": _service_uptime(),
        "a2_decision": a2_dec,
    }


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
@requires_auth
def index():
    status = _build_status()
    return _html(status)


@app.route("/api/status")
@requires_auth
def api_status():
    status = _build_status()
    # Strip non-serializable Alpaca objects
    safe = {
        "a1_mode": status["a1_mode"],
        "a2_mode": status["a2_mode"],
        "gate": status["gate"],
        "costs": status["costs"],
        "decision": status["decision"],
        "git_hash": status["git_hash"],
        "service_uptime": status["service_uptime"],
        "positions_count": len(status["positions"]),
        "a1_error": status["a1"].get("error"),
        "a2_error": status["a2"].get("error"),
    }
    return jsonify(safe)


@app.route("/health")
def health():
    return "ok", 200


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=DASHBOARD_PORT, debug=False)
