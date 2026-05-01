#!/usr/bin/env python3
"""BullBearBot health dashboard — three-page: / overview, /a1 detail, /a2 detail."""

import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
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

ET_OFFSET = timedelta(hours=-4)  # EDT; adjust to -5 in winter

app = Flask(__name__)

if DASHBOARD_USER == "admin" and DASHBOARD_PASSWORD == "bullbearbot":
    app.logger.warning("Default credentials — set DASHBOARD_USER/DASHBOARD_PASSWORD in .env")

# ── Shared CSS (plain string — NOT an f-string — so {} are real CSS braces) ──
SHARED_CSS = """
:root {
  --bg-base: #0d0e1f;
  --bg-card: #10112a;
  --bg-card-2: #0d0e1f;
  --bg-input: #1a1b35;
  --border: #1e2040;
  --border-subtle: #1a1b35;
  --text-primary: #e8ecff;
  --text-secondary: #c8d0e8;
  --text-muted: #4a5080;
  --text-dim: #3a4070;
  --text-ghost: #2a3060;
  --accent-blue: #4facfe;
  --accent-green: #00e676;
  --accent-red: #ff5050;
  --accent-amber: #ffaa20;
  --accent-purple: #a855f7;
  --grad-a1: linear-gradient(135deg, #1a2a4a 0%, #0d1a35 100%);
  --grad-a2: linear-gradient(135deg, #1a3a2a 0%, #0d2a1a 100%);
  --grad-combo: linear-gradient(135deg, #2a1a4a 0%, #1a0d35 100%);
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg-base); color: var(--text-secondary); font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; font-size: 13px; line-height: 1.5; }
a { color: var(--accent-blue); text-decoration: none; }
a:hover { text-decoration: underline; }
details > summary { cursor: pointer; }
details > summary::-webkit-details-marker { display: none; }

.container { max-width: 1100px; margin: 0 auto; padding: 12px 16px 72px; }

/* Nav */
.nav { background: var(--bg-card); border-bottom: 1px solid var(--border); padding: 0 16px; display: flex; align-items: center; gap: 0; position: sticky; top: 0; z-index: 100; height: 44px; }
.nav-brand { font-size: 14px; color: var(--text-primary); margin-right: 16px; white-space: nowrap; flex-shrink: 0; }
.nav-brand .bear { color: var(--accent-blue); }
.nav-tabs { display: flex; align-items: stretch; height: 44px; gap: 0; }
.nav-tab { display: flex; align-items: center; padding: 0 13px; font-size: 11px; color: var(--text-muted); border-bottom: 2px solid transparent; white-space: nowrap; text-decoration: none; transition: color 0.12s, border-color 0.12s; }
.nav-tab:hover { color: var(--text-secondary); text-decoration: none; }
.nav-tab.active { color: var(--accent-blue); border-bottom-color: var(--accent-blue); }
.nav-pills { display: flex; align-items: center; gap: 5px; margin-left: 10px; }
.npill { font-size: 10px; padding: 2px 7px; border-radius: 3px; border: 1px solid; letter-spacing: 0.4px; }
.npill-g { background: rgba(0,230,118,.1); border-color: rgba(0,230,118,.3); color: var(--accent-green); }
.npill-a { background: rgba(255,170,32,.1); border-color: rgba(255,170,32,.3); color: var(--accent-amber); }
.npill-r { background: rgba(255,80,80,.1); border-color: rgba(255,80,80,.3); color: var(--accent-red); }
.nav-right { margin-left: auto; font-size: 10px; color: var(--text-ghost); white-space: nowrap; flex-shrink: 0; }

/* Ticker */
.ticker { background: var(--bg-card); border-top: 1px solid var(--border); padding: 6px 16px; font-size: 9px; color: var(--text-muted); display: flex; align-items: center; gap: 10px; position: fixed; bottom: 0; left: 0; right: 0; z-index: 100; overflow: hidden; white-space: nowrap; }
.tk-sep { color: var(--text-dim); user-select: none; }
.tk-sym { color: var(--text-muted); letter-spacing: 0.3px; }
.tk-val { color: var(--text-secondary); }
.tk-g { color: var(--accent-green); }
.tk-r { color: var(--accent-red); }
.tk-dim { color: var(--text-dim); }

/* Section titles */
.section-label { font-size: 10px; letter-spacing: 1.5px; text-transform: uppercase; color: var(--text-dim); margin: 18px 0 8px; }

/* Cards */
.card { background: var(--bg-card); border: 1px solid var(--border); border-radius: 12px; padding: 14px; margin-bottom: 10px; }
.card-2 { background: var(--bg-card-2); border: 1px solid var(--border-subtle); border-radius: 8px; padding: 8px; }
.card-row { display: flex; justify-content: space-between; align-items: center; padding: 5px 0; border-bottom: 1px solid var(--border-subtle); }
.card-row:last-child { border-bottom: none; }
.card-label { font-size: 12px; color: var(--text-muted); }
.card-val { font-size: 12px; color: var(--text-secondary); }

/* Hero gradient cards */
.hero-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; margin-bottom: 12px; }
@media (max-width: 760px) { .hero-grid { grid-template-columns: 1fr; } }
.hero-card { border-radius: 12px; border: 1px solid var(--border); padding: 16px; }
.hero-card-a1 { background: var(--grad-a1); }
.hero-card-a2 { background: var(--grad-a2); }
.hero-card-combo { background: var(--grad-combo); }
.hero-inner { display: flex; justify-content: space-between; align-items: flex-start; }
.hero-lbl { font-size: 10px; letter-spacing: 1.5px; text-transform: uppercase; color: var(--text-muted); margin-bottom: 6px; }
.hero-num { font-size: 26px; font-weight: 600; letter-spacing: -1px; line-height: 1; }
.hero-sub { font-size: 12px; color: var(--text-muted); margin-top: 5px; }
.hero-badge { display: inline-block; font-size: 10px; padding: 2px 8px; border-radius: 3px; border: 1px solid; margin-top: 8px; }
.hero-badge-g { background: rgba(0,230,118,.12); border-color: rgba(0,230,118,.3); color: var(--accent-green); }
.hero-badge-r { background: rgba(255,80,80,.12); border-color: rgba(255,80,80,.3); color: var(--accent-red); }
.hero-mini-stats { margin-top: 10px; display: flex; flex-direction: column; gap: 5px; }
.hero-mini-row { display: flex; justify-content: space-between; font-size: 11px; }
.hero-mini-lbl { color: var(--text-muted); }
.hero-mini-val { color: var(--text-secondary); }

/* Range bars */
.range-track { height: 5px; background: var(--bg-input); border-radius: 3px; position: relative; margin: 4px 0; overflow: visible; }
.range-fill { position: absolute; top: 0; height: 100%; border-radius: 3px; }

/* Tables */
.table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
table.data-table { width: 100%; border-collapse: collapse; font-size: 12px; white-space: nowrap; }
table.data-table th { background: var(--bg-card-2); color: var(--text-muted); font-size: 10px; font-weight: 500; text-align: right; padding: 8px 10px; text-transform: uppercase; letter-spacing: 0.8px; border-bottom: 1px solid var(--border); }
table.data-table th:first-child { text-align: left; }
table.data-table td { padding: 8px 10px; text-align: right; border-bottom: 1px solid var(--border-subtle); color: var(--text-secondary); }
table.data-table td:first-child { text-align: left; color: var(--text-primary); }
table.data-table tr:last-child td { border-bottom: none; }
table.data-table tr:hover td { background: rgba(79,172,254,.03); }

/* qs-table kept for kv widgets */
.qs-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.qs-table th { background: var(--bg-card-2); color: var(--text-muted); font-size: 10px; padding: 7px 10px; font-weight: 500; text-transform: uppercase; letter-spacing: 0.8px; text-align: left; border-bottom: 1px solid var(--border); }
.qs-table td { padding: 7px 10px; border-bottom: 1px solid var(--border-subtle); font-size: 12px; color: var(--text-secondary); }
.qs-table tr:last-child td { border-bottom: none; }
.qs-table td:first-child { color: var(--text-muted); }
.qs-table td:not(:first-child) { text-align: right; }
.qs-table th:not(:first-child) { text-align: right; }

/* pos-table alias for backward compat */
table.pos-table { width: 100%; border-collapse: collapse; font-size: 12px; white-space: nowrap; }
table.pos-table th { background: var(--bg-card-2); color: var(--text-muted); font-weight: 500; text-align: right; padding: 8px 10px; font-size: 10px; text-transform: uppercase; letter-spacing: 0.8px; border-bottom: 1px solid var(--border); }
table.pos-table th:first-child { text-align: left; }
table.pos-table td { padding: 8px 10px; text-align: right; border-bottom: 1px solid var(--border-subtle); color: var(--text-secondary); }
table.pos-table td:first-child { text-align: left; color: var(--text-primary); }
table.pos-table tr:last-child td { border-bottom: none; }
table.pos-table tr:hover td { background: rgba(79,172,254,.03); }

/* Badges */
.badge { display: inline-block; font-size: 10px; padding: 2px 8px; border-radius: 3px; border: 1px solid; letter-spacing: 0.3px; vertical-align: middle; }
.badge-g { background: rgba(0,230,118,.1); border-color: rgba(0,230,118,.3); color: var(--accent-green); }
.badge-r { background: rgba(255,80,80,.1); border-color: rgba(255,80,80,.3); color: var(--accent-red); }
.badge-a { background: rgba(255,170,32,.1); border-color: rgba(255,170,32,.3); color: var(--accent-amber); }
.badge-b { background: rgba(79,172,254,.1); border-color: rgba(79,172,254,.3); color: var(--accent-blue); }
.badge-p { background: rgba(168,85,247,.1); border-color: rgba(168,85,247,.3); color: var(--accent-purple); }
.badge-x { background: rgba(74,80,128,.1); border-color: rgba(74,80,128,.3); color: var(--text-muted); }

/* Flag badges (legacy compat) */
.flag { display: inline-block; font-size: 9px; padding: 1px 5px; border-radius: 3px; margin-left: 4px; vertical-align: middle; }
.flag-earn { background: rgba(255,170,32,.15); color: var(--accent-amber); }
.flag-over { background: rgba(255,80,80,.15); color: var(--accent-red); }
.flag-warn { background: rgba(255,170,32,.12); color: var(--accent-amber); }
.flag-trail { background: rgba(0,230,118,.12); color: var(--accent-green); }
.flag-be { background: rgba(79,172,254,.12); color: var(--accent-blue); }

/* Alert/warning */
.warn-critical { background: rgba(255,80,80,.08); border: 1px solid rgba(255,80,80,.25); color: var(--accent-red); border-radius: 8px; padding: 10px 14px; margin-bottom: 8px; font-size: 11px; }
.warn-orange { background: rgba(255,170,32,.08); border: 1px solid rgba(255,170,32,.25); color: var(--accent-amber); border-radius: 8px; padding: 10px 14px; margin-bottom: 8px; font-size: 11px; }
.alert { padding: 9px 12px; border-radius: 8px; margin-bottom: 6px; font-size: 11px; }
.alert-green { background: rgba(0,230,118,.07); border: 1px solid rgba(0,230,118,.2); color: var(--accent-green); }
.alert-orange { background: rgba(255,170,32,.07); border: 1px solid rgba(255,170,32,.2); color: var(--accent-amber); }
.alert-red { background: rgba(255,80,80,.07); border: 1px solid rgba(255,80,80,.2); color: var(--accent-red); }

/* Stat boxes */
.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 8px; }
.stat-box { background: var(--bg-card-2); border: 1px solid var(--border-subtle); border-radius: 8px; padding: 10px 12px; }
.stat-label { font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.8px; }
.stat-val { font-size: 18px; font-weight: 600; margin-top: 3px; color: var(--text-primary); letter-spacing: -0.5px; }

/* kv rows */
.kv { display: flex; justify-content: space-between; padding: 5px 0; border-bottom: 1px solid var(--border-subtle); font-size: 12px; }
.kv:last-child { border-bottom: none; }
.kv-label { color: var(--text-muted); font-size: 12px; }
.kv-val { color: var(--text-secondary); text-align: right; font-size: 12px; }

/* Reasoning */
.reasoning { background: var(--bg-card-2); border-left: 2px solid var(--accent-blue); padding: 10px 14px; border-radius: 0 8px 8px 0; font-size: 11px; color: var(--text-secondary); font-style: italic; margin: 8px 0; }
.log-line { font-family: "SF Mono", "Fira Code", monospace; font-size: 10px; padding: 2px 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-muted); }

/* Thesis cards */
.thesis-card { border: 1px solid var(--border-subtle); border-radius: 8px; padding: 10px 12px; margin-bottom: 8px; }
.thesis-card:last-child { margin-bottom: 0; }

/* Watch bullets */
.watch-bullet { padding: 5px 0; border-bottom: 1px solid var(--border-subtle); font-size: 12px; }
.watch-bullet:last-child { border-bottom: none; }

/* Compact grids */
.compact-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
@media (max-width: 600px) { .compact-grid { grid-template-columns: 1fr; } }
.tri-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; }
@media (max-width: 760px) { .tri-grid { grid-template-columns: 1fr; } }

/* Trail table */
.trail-table { width: 100%; border-collapse: collapse; font-size: 11px; }
.trail-table th { background: var(--bg-card-2); color: var(--text-muted); font-weight: 500; padding: 6px 10px; text-align: left; font-size: 9px; text-transform: uppercase; letter-spacing: 0.8px; border-bottom: 1px solid var(--border); }
.trail-table td { padding: 6px 10px; border-bottom: 1px solid var(--border-subtle); }
.trail-table tr:last-child td { border-bottom: none; }

/* Progress bar */
.progress-wrap { background: var(--bg-input); border-radius: 3px; height: 5px; margin: 4px 0 2px; overflow: hidden; }
.progress-fill { height: 5px; border-radius: 3px; }

/* Dec panel */
.dec-panel { max-height: 340px; overflow-y: auto; }

/* Acct summary bar (horizontal) */
.acct-bar { display: flex; gap: 0; background: var(--bg-card); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; margin-bottom: 10px; }
.acct-bar-item { padding: 12px 16px; flex: 1; border-right: 1px solid var(--border-subtle); }
.acct-bar-item:last-child { border-right: none; }
.acct-bar-lbl { font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 4px; }
.acct-bar-val { font-size: 13px; color: var(--text-primary); }

/* Legacy acct rows */
.acct-title { font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 10px; }
.acct-row { display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid var(--border-subtle); font-size: 12px; }
.acct-row:last-child { border-bottom: none; }
.acct-label { color: var(--text-muted); font-size: 12px; }
.acct-val { color: var(--text-secondary); }

/* Color utilities */
.green { color: var(--accent-green); }
.red { color: var(--accent-red); }
.orange { color: var(--accent-amber); }
.blue { color: var(--accent-blue); }
.purple { color: var(--accent-purple); }
.muted { color: var(--text-muted); }
.primary { color: var(--text-primary); }
"""

# Plain string — no escaping needed when injected via {_COUNTDOWN_JS} in f-strings
_COUNTDOWN_JS = """<script>
var secs = 60, el = document.getElementById("cd");
setInterval(function() { secs -= 1; if (secs <= 0) secs = 60; if (el) el.textContent = secs; }, 1000);
</script>"""

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
            return Response("Authentication required", 401,
                            {"WWW-Authenticate": 'Basic realm="BullBearBot"'})
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
        orders = []
        try:
            orders = c.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=200))
        except Exception:
            pass
        buys_today = sells_today = 0
        recent_orders = []
        try:
            today_midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            closed = c.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED,
                                                    after=today_midnight, limit=200))
            filled = [o for o in closed if o.status == OrderStatus.FILLED]
            buys_today = sum(1 for o in filled if o.side == OrderSide.BUY)
            sells_today = sum(1 for o in filled if o.side == OrderSide.SELL)
        except Exception:
            pass
        try:
            recent_orders = list(c.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, limit=20)))
        except Exception:
            pass
        return {"ok": True, "account": acc, "positions": pos, "orders": orders,
                "buys_today": buys_today, "sells_today": sells_today, "recent_orders": recent_orders}
    except Exception as e:
        return {"ok": False, "error": str(e), "account": None, "positions": [], "orders": [],
                "buys_today": 0, "sells_today": 0, "recent_orders": []}


@_cached("a2", ttl=60)
def _alpaca_a2():
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        c = TradingClient(ALPACA_KEY_OPT, ALPACA_SECRET_OPT, paper=True)
        acc = c.get_account()
        pos = c.get_all_positions()
        recent_orders = []
        try:
            recent_orders = list(c.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, limit=15)))
        except Exception:
            pass
        return {"ok": True, "account": acc, "positions": pos, "recent_orders": recent_orders}
    except Exception as e:
        return {"ok": False, "error": str(e), "account": None, "positions": [], "recent_orders": []}


@_cached("pnl_a1", ttl=60)
def _today_pnl_a1() -> tuple:
    try:
        import requests as req
        r = req.get(
            "https://paper-api.alpaca.markets/v2/account/portfolio/history"
            "?period=1D&timeframe=1Min&extended_hours=true",
            headers={"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET},
            timeout=10,
        )
        eq = r.json().get("equity", [])
        if len(eq) >= 2 and eq[0]:
            pnl = eq[-1] - eq[0]
            return pnl, pnl / eq[0] * 100
    except Exception:
        pass
    return 0.0, 0.0


@_cached("pnl_a2", ttl=60)
def _today_pnl_a2() -> tuple:
    try:
        import requests as req
        r = req.get(
            "https://paper-api.alpaca.markets/v2/account/portfolio/history"
            "?period=1D&timeframe=1Min",
            headers={"APCA-API-KEY-ID": ALPACA_KEY_OPT, "APCA-API-SECRET-KEY": ALPACA_SECRET_OPT},
            timeout=10,
        )
        eq = r.json().get("equity", [])
        if len(eq) >= 2 and eq[0]:
            pnl = eq[-1] - eq[0]
            return pnl, pnl / eq[0] * 100
    except Exception:
        pass
    return 0.0, 0.0


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


def _last_n_a1_decisions(n: int = 5) -> list:
    try:
        decisions = json.loads((BOT_DIR / "memory/decisions.json").read_text())
        valids = [d for d in decisions
                  if d.get("reasoning", "") and "gate skipped" not in d.get("reasoning", "")]
        return list(reversed(valids[-n:]))
    except Exception:
        return []


def _last_n_a2_decisions(n: int = 5) -> list:
    try:
        dec_dir = BOT_DIR / "data/account2/decisions"
        files = sorted(dec_dir.glob("a2_dec_*.json"))[-n:]
        result = []
        for f in reversed(files):
            try:
                result.append(json.loads(f.read_text()))
            except Exception:
                pass
        return result
    except Exception:
        return []


def _a2_last_cycle() -> dict:
    try:
        dec_dir = BOT_DIR / "data/account2/decisions"
        files = sorted(dec_dir.glob("a2_dec_*.json"))
        return json.loads(files[-1].read_text()) if files else {}
    except Exception:
        return {}


def _a2_structures() -> list:
    try:
        raw = json.loads((BOT_DIR / "data/account2/positions/structures.json").read_text())
        structs = [s for s in raw if isinstance(s, dict)]
        active_lc = {"fully_filled", "open", "submitted", "proposed"}
        return [s for s in structs if s.get("lifecycle") in active_lc]
    except Exception:
        return []


def _morning_brief() -> dict:
    try:
        return json.loads((BOT_DIR / "data/market/morning_brief.json").read_text())
    except Exception:
        return {}


def _morning_brief_time() -> str:
    try:
        mtime = (BOT_DIR / "data/market/morning_brief.json").stat().st_mtime
        dt = datetime.fromtimestamp(mtime, tz=timezone.utc) + ET_OFFSET
        return dt.strftime("%-I:%M %p ET")
    except Exception:
        return "?"


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
        errs = [ln.strip() for ln in lines
                if any(k in ln for k in ("  ERROR  ", "  WARNING  ", "  CRITICAL  "))]
        return errs[-max_out:]
    except Exception:
        return []


def _git_hash():
    try:
        head = (BOT_DIR / ".git/HEAD").read_text().strip()
        if head.startswith("ref: "):
            full = (BOT_DIR / ".git" / head[5:]).read_text().strip()
        else:
            full = head
        return full[:7] if len(full) >= 7 else full
    except Exception:
        try:
            r = subprocess.run(["/usr/bin/git", "rev-parse", "--short", "HEAD"],
                               capture_output=True, text=True, cwd=BOT_DIR, timeout=5)
            return r.stdout.strip() or "unknown"
        except Exception:
            return "unknown"


def _service_uptime():
    try:
        r = subprocess.run(["systemctl", "show", "trading-bot", "--property=ActiveEnterTimestamp"],
                           capture_output=True, text=True, timeout=5)
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
        date_str = ev.get("date", ev.get("report_date", ""))
        if sym and date_str:
            if date_str.startswith(today):
                flags[sym] = "EARNINGS TODAY"
            elif date_str.startswith(tomorrow):
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


def _qualitative_context() -> dict:
    try:
        return json.loads((BOT_DIR / "data/market/qualitative_context.json").read_text())
    except Exception:
        return {}


# ── Thesis helpers ────────────────────────────────────────────────────────────
def _a1_top_theses(decisions: list, qctx: dict) -> list:
    seen: dict[str, dict] = {}
    for d in decisions:
        actions = d.get("actions", d.get("ideas", []))
        ts = d.get("ts", "")
        for a in actions:
            sym = a.get("symbol", "")
            intent = (a.get("action") or a.get("intent") or "").upper()
            if sym and sym not in seen and intent not in ("HOLD", "WATCH", "MONITOR", "OBSERVE"):
                seen[sym] = {"symbol": sym, "intent": intent, "ts": ts}
    sym_ctx = qctx.get("symbol_context", {}) if isinstance(qctx, dict) else {}
    result = []
    for sym, info in list(seen.items())[:8]:
        ctx = (sym_ctx.get(sym) or {}) if isinstance(sym_ctx, dict) else {}
        narrative = (ctx.get("narrative", "") or "")[:220]
        tags = ctx.get("thesis_tags", []) or []
        result.append({
            "symbol": sym,
            "intent": info["intent"],
            "ts": info["ts"],
            "narrative": narrative,
            "tags": tags[:4],
            "catalyst_active": ctx.get("catalyst_active", False),
        })
        if len(result) >= 5:
            break
    return result


def _a2_top_theses(a2_decs: list) -> list:
    result = []
    for d in a2_decs[:5]:
        cand = d.get("selected_candidate") or {}
        if not isinstance(cand, dict) or not cand:
            continue
        sym = cand.get("symbol", "")
        strategy = cand.get("structure_type", cand.get("strategy", ""))
        debate = d.get("debate_parsed") or {}
        conf = debate.get("confidence", "?") if isinstance(debate, dict) else "?"
        reasons = debate.get("reasons", []) if isinstance(debate, dict) else []
        if isinstance(reasons, str):
            reasons = [reasons]
        result.append({
            "symbol": sym,
            "strategy": strategy,
            "confidence": conf,
            "reasons": (reasons or [])[:2],
            "ts": d.get("built_at", ""),
            "result": d.get("execution_result", "?"),
        })
    return result


# ── Formatting helpers ────────────────────────────────────────────────────────
def _to_et(ts_str: str) -> str:
    if not ts_str:
        return "—"
    try:
        s = ts_str.replace("Z", "+00:00")
        if "T" not in s:
            s = s[:19].replace(" ", "T") + "+00:00"
        ts = datetime.fromisoformat(s)
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
    if "risk" in m or "contain" in m or "reconcile" in m:
        return "#d29922"
    return "#8b949e"


def _trail_status_badge(entry: float, stop: float) -> str:
    if not entry or not stop:
        return ""
    ratio = stop / entry
    if ratio >= 1.001:
        return '<span class="flag flag-trail">PROFIT TRAIL</span>'
    if ratio >= 0.998:
        return '<span class="flag flag-be">BREAKEVEN</span>'
    return ""


def _is_market_hours() -> bool:
    et = datetime.now(timezone.utc) + ET_OFFSET
    if et.weekday() >= 5:
        return False
    h, m = et.hour, et.minute
    return (h == 9 and m >= 30) or (10 <= h < 16)


# ── A2 qualitative helpers ────────────────────────────────────────────────────
def _iv_env_label(iv_rank) -> str:
    if iv_rank is None:
        return "unknown"
    r = float(iv_rank)
    if r < 15:   return "very cheap"
    if r < 35:   return "cheap"
    if r < 65:   return "neutral"
    if r < 80:   return "expensive"
    return "very expensive"


def _parse_net_debit(structure: dict):
    for entry in structure.get("audit_log", []):
        msg = entry.get("msg", "")
        if "net_debit=" in msg:
            try:
                return float(msg.split("net_debit=")[1].split()[0])
            except Exception:
                pass
    return None


def _calc_dte(expiry_str: str):
    try:
        return (date.fromisoformat(expiry_str) - date.today()).days
    except Exception:
        return None


def _build_a2_position_cards(structures: list, a2_live_positions: list) -> list:
    occ_pnl: dict[str, float] = {}
    for p in a2_live_positions:
        sym = getattr(p, "symbol", "")
        unreal = float(getattr(p, "unrealized_pl", 0) or 0)
        if sym:
            occ_pnl[sym] = unreal

    cards = []
    seen = set()
    for struct in structures:
        sid = struct.get("structure_id", "")
        if sid in seen:
            continue
        underlying = struct.get("underlying", "")
        strategy = struct.get("strategy", "")
        expiry_str = struct.get("expiration", "")
        long_strike = struct.get("long_strike")
        short_strike = struct.get("short_strike")
        max_cost = struct.get("max_cost_usd")
        max_profit = struct.get("max_profit_usd")
        iv_rank = struct.get("iv_rank")
        direction = struct.get("direction", "")
        legs = struct.get("legs", [])

        net_pnl = 0.0
        matched = False
        for leg in legs:
            occ = leg.get("occ_symbol", "")
            if occ in occ_pnl:
                net_pnl += occ_pnl[occ]
                matched = True
        if not matched:
            continue
        seen.add(sid)

        net_debit = _parse_net_debit(struct)
        dte = _calc_dte(expiry_str)
        dte_str = f"{dte} DTE" if dte is not None else "?"
        iv_env = _iv_env_label(iv_rank)
        iv_rank_str = f"{iv_rank:.1f}" if iv_rank is not None else "?"
        max_loss_str = _fm(max_cost) if max_cost else "N/A"
        is_single = "single" in strategy
        max_gain_str = _fm(max_profit) if max_profit else ("unlimited" if is_single else "N/A")
        net_pnl_pct = (net_pnl / max_cost * 100) if max_cost and max_cost > 0 else 0.0
        pnl_sign = "+" if net_pnl >= 0 else ""
        pnl_color = "#3fb950" if net_pnl >= 0 else "#f85149"
        pnl_str = f"{pnl_sign}{_fm(net_pnl)} ({pnl_sign}{net_pnl_pct:.1f}%)"
        s = strategy
        ls = long_strike
        ss = short_strike

        if "call_debit_spread" in s or ("debit" in s and "call" in s):
            breakeven = (ls + net_debit) if ls and net_debit else None
            s_range = f"${ls:.0f}/${ss:.0f}" if ss else f"${ls:.0f}"
            title = f"{underlying} {s_range} Call Debit Spread — {expiry_str} ({dte_str})"
            profit_line = (f"Profit if {underlying} &gt; ${breakeven:.2f} (breakeven)"
                           if breakeven else f"Profit if {underlying} rises above ${ss:.0f}" if ss else f"Profit if {underlying} rises")
            rationale = f"IV rank {iv_rank_str} ({iv_env}) — debit call spread. Max risk = premium paid."
        elif "put_debit_spread" in s or ("debit" in s and "put" in s):
            breakeven = (ls - net_debit) if ls and net_debit else None
            s_range = f"${ls:.0f}/${ss:.0f}" if ss else f"${ls:.0f}"
            title = f"{underlying} {s_range} Put Debit Spread — {expiry_str} ({dte_str})"
            profit_line = (f"Profit if {underlying} &lt; ${breakeven:.2f} (breakeven)"
                           if breakeven else f"Profit if {underlying} falls")
            rationale = f"IV rank {iv_rank_str} ({iv_env}) — put debit spread. Bearish thesis."
        elif "call_credit_spread" in s:
            s_range = f"${ls:.0f}/${ss:.0f}" if ss else f"${ls:.0f}"
            title = f"{underlying} {s_range} Call Credit Spread — {expiry_str} ({dte_str})"
            profit_line = f"Profit if {underlying} stays below ${ss:.0f}" if ss else "Profit if flat/down"
            rationale = f"IV rank {iv_rank_str} ({iv_env}) — selling call premium when vol is elevated."
        elif "put_credit_spread" in s:
            s_range = f"${ls:.0f}/${ss:.0f}" if ss else f"${ls:.0f}"
            title = f"{underlying} {s_range} Put Credit Spread — {expiry_str} ({dte_str})"
            profit_line = f"Profit if {underlying} stays above ${ss:.0f}" if ss else "Profit if flat/up"
            rationale = f"IV rank {iv_rank_str} ({iv_env}) — selling put premium."
        elif "single_call" in s:
            breakeven = (ls + net_debit) if ls and net_debit else None
            title = f"{underlying} ${ls:.0f} Call — {expiry_str} ({dte_str})"
            profit_line = (f"Profit if {underlying} &gt; ${breakeven:.2f}" if breakeven
                           else f"Profit if {underlying} rises above ${ls:.0f}")
            rationale = f"IV rank {iv_rank_str} ({iv_env}) — long call, {direction} thesis."
        elif "single_put" in s:
            breakeven = (ls - net_debit) if ls and net_debit else None
            title = f"{underlying} ${ls:.0f} Put — {expiry_str} ({dte_str})"
            profit_line = (f"Profit if {underlying} &lt; ${breakeven:.2f}" if breakeven
                           else f"Profit if {underlying} falls below ${ls:.0f}")
            rationale = f"IV rank {iv_rank_str} ({iv_env}) — long put, bearish/protective."
        else:
            title = f"{underlying} {s.replace('_', ' ').title()} — {expiry_str} ({dte_str})"
            profit_line = f"{direction.title()} thesis on {underlying}"
            rationale = f"IV rank {iv_rank_str} ({iv_env})"

        cards.append({
            "title": title, "strategy_label": s.replace("_", " ").title(),
            "iv_env": iv_env, "iv_rank_str": iv_rank_str,
            "profit_line": profit_line, "max_gain_str": max_gain_str, "max_loss_str": max_loss_str,
            "pnl_str": pnl_str, "pnl_color": pnl_color, "rationale": rationale,
            "progress_html": _a2_position_progress_html(net_pnl, max_cost, max_profit),
        })
    return cards[:10]


def _fmt_orders_html(recent_orders, is_options: bool = False, limit: int = 6) -> str:
    html = ""
    count = 0
    for o in (recent_orders or []):
        if count >= limit:
            break
        try:
            sym = getattr(o, "symbol", None) or "MLEG"
            side_raw = str(getattr(o, "side", "")).lower()
            qty = getattr(o, "qty", "?")
            filled_price = getattr(o, "filled_avg_price", None)
            status_raw = str(getattr(o, "status", "")).lower()
            created_raw = str(getattr(o, "created_at", ""))
            status_str = status_raw.split(".")[-1]
            side_str = side_raw.split(".")[-1].upper()
            ts_et = _to_et(created_raw)
            unit = "ct" if is_options else "sh"
            if status_str == "filled" and side_str == "BUY":
                icon, color = "&#x2705;", "#3fb950"
                price_part = f"@ {_fm(filled_price)}" if filled_price else ""
            elif status_str == "filled" and side_str in ("SELL", "SELL_SHORT"):
                icon, color = "&#x1F534;", "#f85149"
                price_part = f"@ {_fm(filled_price)}" if filled_price else ""
            elif status_str in ("canceled", "cancelled", "rejected"):
                icon, color, price_part = "&#x26A0;&#xFE0F;", "#d29922", status_str.upper()
            elif status_str in ("new", "held", "accepted", "pending_new", "partially_filled"):
                icon, color, price_part = "&#x23F3;", "#8b949e", "pending"
            else:
                icon, color, price_part = "&middot;", "#8b949e", status_str
            html += (
                f'<div style="font-size:12px;color:{color};padding:4px 0;'
                f'border-bottom:1px solid #21262d;font-family:monospace">'
                f'{icon} {side_str} {sym} {qty}{unit} {price_part} '
                f'<span style="color:#8b949e">[{ts_et}]</span></div>'
            )
            count += 1
        except Exception:
            pass
    return html or '<div style="color:#8b949e;font-size:13px">No recent orders</div>'


# ── New UX helpers ────────────────────────────────────────────────────────────
def _morning_brief_mtime_float() -> float:
    try:
        return (BOT_DIR / "data/market/morning_brief.json").stat().st_mtime
    except Exception:
        return 0.0


def _intelligence_brief_full() -> dict:
    try:
        return json.loads((BOT_DIR / "data/market/morning_brief_full.json").read_text())
    except Exception:
        return {}


def _brief_staleness_html(mtime_float: float) -> str:
    if not mtime_float or not _is_market_hours():
        return ""
    age_h = (time.time() - mtime_float) / 3600
    if age_h < 2:
        return ""
    if age_h < 6:
        return f' <span style="color:#d29922;font-size:11px">&#x26A0;&#xFE0F; {age_h:.0f}h ago</span>'
    return f' <span style="color:#f85149;font-size:11px">&#x1F534; {age_h:.0f}h ago (stale)</span>'


def _a2_position_progress_html(net_pnl: float, max_cost, max_profit) -> str:
    try:
        if not max_cost or max_cost <= 0:
            return ""
        max_l = float(max_cost)
        max_p = float(max_profit) if max_profit else max_l
        span = max_l + max_p
        if span <= 0:
            return ""
        clamped = max(-max_l, min(max_p, net_pnl))
        pos_pct = (clamped + max_l) / span * 100
        stop_pct = (max_l * 0.5) / span * 100
        be_pct = max_l / span * 100
        target_pct = (max_l + max_p * 0.8) / span * 100
        fill_color = "#3fb950" if net_pnl >= 0 else "#f85149"
        if net_pnl >= 0:
            fill_from, fill_to = be_pct, pos_pct
        else:
            fill_from, fill_to = pos_pct, be_pct
        fill_width = abs(fill_to - fill_from)
        dist_to_target = max_p * 0.8 - net_pnl
        dist_to_stop = net_pnl - (-max_l * 0.5)
        dist_to_stop_sign = "+" if dist_to_stop >= 0 else ""
        dist_to_target_arrow = "&#x2191; " if dist_to_target > 0 else "&#x2713; "
        return (
            f'<div style="position:relative;height:18px;background:#21262d;border-radius:4px;margin:8px 0;overflow:hidden">'
            f'<div style="position:absolute;left:{fill_from:.1f}%;width:{fill_width:.1f}%;height:100%;background:{fill_color};opacity:0.6"></div>'
            f'<div style="position:absolute;left:{stop_pct:.1f}%;top:0;width:2px;height:100%;background:#f85149;opacity:0.8" title="50% stop"></div>'
            f'<div style="position:absolute;left:{be_pct:.1f}%;top:0;width:2px;height:100%;background:#58a6ff;opacity:0.8" title="breakeven"></div>'
            f'<div style="position:absolute;left:{target_pct:.1f}%;top:0;width:2px;height:100%;background:#3fb950;opacity:0.8" title="80% target"></div>'
            f'<div style="position:absolute;left:calc({pos_pct:.1f}% - 3px);top:3px;width:6px;height:12px;background:#fff;border-radius:2px;opacity:0.9"></div>'
            f'</div>'
            f'<div style="font-size:10px;color:#8b949e;display:flex;justify-content:space-between;margin-bottom:3px">'
            f'<span style="color:#f85149">-{_fm(max_l)}</span>'
            f'<span style="color:#58a6ff">BE</span>'
            f'<span style="color:#3fb950">+{_fm(max_p*0.8)}</span>'
            f'<span>+{_fm(max_p)}</span>'
            f'</div>'
            f'<div style="font-size:11px;color:#8b949e">'
            f'{dist_to_target_arrow}{_fm(abs(dist_to_target))} to target &nbsp;|&nbsp; '
            f'{dist_to_stop_sign}{_fm(abs(dist_to_stop))} margin above stop'
            f'</div>'
        )
    except Exception:
        return ""


def _trail_table_html(positions: list, trail_tiers: list) -> str:
    if not positions:
        return '<div style="color:#8b949e;font-size:13px">No open positions.</div>'
    rows = ""
    for p in positions:
        entry = p.get("entry", 0.0)
        current = p.get("current", 0.0)
        stop = p.get("stop")
        if not entry or not current:
            continue
        sym = p["symbol"]
        gain_pct = (current - entry) / entry * 100
        current_tier_idx = -1
        for i, tier in enumerate(trail_tiers):
            if gain_pct >= tier.get("gain_pct", 0) * 100:
                current_tier_idx = i
        if gain_pct < 0 or current_tier_idx < 0:
            tier_label = "No trail"
            tier_color = "#8b949e"
        else:
            stop_floor = trail_tiers[current_tier_idx].get("stop_pct", 0) * 100
            tier_label = f"T{current_tier_idx+1} (stop &ge;+{stop_floor:.0f}%)"
            tier_color = "#3fb950"
        next_idx = current_tier_idx + 1
        if next_idx < len(trail_tiers):
            nt = trail_tiers[next_idx]
            trig_price = entry * (1 + nt.get("gain_pct", 0))
            next_trigger = f"${trig_price:.2f} (+{nt.get('gain_pct',0)*100:.0f}%)"
        elif trail_tiers and current_tier_idx < 0:
            t1 = trail_tiers[0]
            trig_price = entry * (1 + t1.get("gain_pct", 0))
            next_trigger = f"${trig_price:.2f} (+{t1.get('gain_pct',0)*100:.0f}%)"
        else:
            next_trigger = "max tier"
        if stop is None:
            stop_str, stop_color = "—", "#f85149"
        elif stop >= entry * 1.001:
            stop_str, stop_color = f"${stop:.2f}", "#3fb950"
        elif stop >= entry * 0.998:
            stop_str, stop_color = f"${stop:.2f}", "#58a6ff"
        else:
            stop_str, stop_color = f"${stop:.2f}", "#d29922"
        gain_color = "#3fb950" if gain_pct >= 0 else "#f85149"
        gain_sign = "+" if gain_pct >= 0 else ""
        rows += (
            f'<tr>'
            f'<td><b>{sym}</b></td>'
            f'<td style="color:{gain_color}">{gain_sign}{gain_pct:.1f}%</td>'
            f'<td style="color:{tier_color}">{tier_label}</td>'
            f'<td style="color:#8b949e">{next_trigger}</td>'
            f'<td style="color:{stop_color}">{stop_str}</td>'
            f'</tr>'
        )
    if not rows:
        return '<div style="color:#8b949e;font-size:13px">No position data.</div>'
    return (
        '<div class="table-wrap"><table class="trail-table">'
        '<thead><tr><th>Symbol</th><th>Gain %</th><th>Trail Tier</th><th>Next Trigger</th><th>Stop</th></tr></thead>'
        f'<tbody>{rows}</tbody></table></div>'
    )


def _allocator_shadow_compact() -> str:
    try:
        path = BOT_DIR / "data/analytics/portfolio_allocator_shadow.jsonl"
        entries = _jsonl_last(path, n=1)
        if not entries:
            return ""
        entry = entries[0]
        actions = entry.get("proposed_actions", [])
        if not actions:
            return ""
        ts = entry.get("timestamp", "")
        ts_label = ""
        if ts:
            try:
                s = ts.replace("Z", "+00:00")
                if "T" not in s:
                    s = s[:19].replace(" ", "T") + "+00:00"
                dt = datetime.fromisoformat(s)
                et = dt + ET_OFFSET
                ts_label = et.strftime("%-I:%M %p")
            except Exception:
                ts_label = ""
        parts = [f"{a.get('action','').upper()} {a.get('symbol','')}"
                 for a in actions if a.get("action","").upper() != "HOLD" and a.get("symbol")]
        if not parts:
            parts = [f"{a.get('action','').upper()} {a.get('symbol','')}"
                     for a in actions[:3] if a.get("symbol")]
        action_str = " | ".join(parts[:5])
        if len(actions) > 5:
            action_str += f" +{len(actions)-5}"
        ts_part = (f' <span style="color:#8b949e">[updated {ts_label}]</span>' if ts_label else "")
        return (
            f'<div style="font-size:13px;padding:8px 0">'
            f'<span style="color:#d29922;font-weight:600;font-family:monospace">ALLOCATOR (shadow):</span> '
            f'<span style="color:#c9d1d9;font-family:monospace">{action_str}</span>{ts_part}'
            f'</div>'
        )
    except Exception:
        return ""


def _a2_pipeline_today() -> dict:
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = {"total": 0, "submitted": 0, "fully_filled": 0, "cancelled": 0, "proposed": 0}
    try:
        raw = json.loads((BOT_DIR / "data/account2/positions/structures.json").read_text())
        for s in raw:
            if not isinstance(s, dict):
                continue
            opened = str(s.get("opened_at", ""))
            if not opened.startswith(today_str):
                continue
            out["total"] += 1
            lc = s.get("lifecycle", "")
            if lc in out:
                out[lc] += 1
    except Exception:
        pass
    return out


def _a1_decisions_compact_html(decisions: list) -> str:
    if not decisions:
        return '<div style="color:#8b949e;font-size:12px">No decisions yet.</div>'
    rows = ""
    for d in decisions[:5]:
        ts = _to_et(d.get("ts", ""))
        regime = d.get("regime", d.get("regime_view", "?"))
        score = d.get("regime_score", "")
        actions = d.get("actions", d.get("ideas", []))
        act_parts = []
        for a in actions[:4]:
            sym = a.get("symbol", "")
            intent = (a.get("action") or a.get("intent") or "").upper()
            if sym and intent:
                c = "#3fb950" if intent in ("BUY", "ADD") else ("#f85149" if intent in ("SELL", "EXIT", "TRIM") else "#8b949e")
                act_parts.append(f'<span style="color:{c}">{intent} {sym}</span>')
        acts_line = " &middot; ".join(act_parts) if act_parts else '<span style="color:#8b949e">HOLD</span>'
        score_str = f"({score})" if score != "" else ""
        rc = "#3fb950" if "risk_on" in str(regime) or "bullish" in str(regime) else (
             "#f85149" if "risk_off" in str(regime) or "bearish" in str(regime) else "#d29922")
        rows += (
            f'<div style="font-size:12px;padding:3px 0;border-bottom:1px solid #21262d;'
            f'font-family:monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'
            f'<span style="color:#8b949e">[{ts}]</span> '
            f'<span style="color:{rc}">{regime}</span><span style="color:#8b949e">{score_str}</span> '
            f'{acts_line}</div>'
        )
    return rows


def _a2_decisions_compact_html(decs: list) -> str:
    if not decs:
        return '<div style="color:#8b949e;font-size:12px">No A2 decisions yet.</div>'
    rows = ""
    for d in decs[:5]:
        ts = _to_et(d.get("built_at", ""))
        result = d.get("execution_result", "?")
        cand = d.get("selected_candidate") or {}
        sym = cand.get("symbol", "") if isinstance(cand, dict) else ""
        st = cand.get("structure_type", "") if isinstance(cand, dict) else ""
        cand_str = f"{sym} {st}".strip() if sym else "—"
        reason = d.get("no_trade_reason", "") or ""
        rc = "#3fb950" if result == "submitted" else ("#d29922" if result == "no_trade" else "#8b949e")
        reason_part = f' <span style="color:#8b949e">({reason[:30]})</span>' if reason else ""
        rows += (
            f'<div style="font-size:12px;padding:3px 0;border-bottom:1px solid #21262d;'
            f'font-family:monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'
            f'<span style="color:#8b949e">[{ts}]</span> '
            f'<span style="color:{rc}">{result}</span>{reason_part} '
            f'<span style="color:#8b949e">{cand_str}</span></div>'
        )
    return rows


def _watch_now_bullets(status: dict) -> list:
    bullets = []

    # 1. Earnings binary events
    for p in status.get("positions", []):
        earn = p.get("earnings", "")
        if earn:
            sym = p["symbol"]
            entry = p.get("entry", 1)
            current = p.get("current", entry)
            gain = (current - entry) / entry * 100 if entry else 0
            bullets.append(("critical",
                f"<b>{sym}</b>: {earn} &mdash; {gain:+.1f}% open. Consider sizing before binary event."))
            if len(bullets) >= 6:
                return bullets

    # 2. Trail triggers — within 1% of next tier
    trail_tiers = status.get("trail_tiers", [])
    for p in status.get("positions", []):
        entry = p.get("entry", 0)
        current = p.get("current", 0)
        if not entry or not current:
            continue
        gain_pct = (current - entry) / entry * 100
        for i, tier in enumerate(trail_tiers):
            tier_gain = tier.get("gain_pct", 0) * 100
            if 0 < tier_gain - gain_pct < 1.0:
                trig = entry * (1 + tier.get("gain_pct", 0))
                bullets.append(("orange",
                    f"<b>{p['symbol']}</b>: {gain_pct:.1f}% gain &mdash; {tier_gain-gain_pct:.1f}% from T{i+1} trail trigger at ${trig:.2f}"))
                break
        if len(bullets) >= 6:
            return bullets

    # 3. Entries primed — most recent decision has BUY/ADD
    a1_decs = status.get("a1_decisions", [])
    if a1_decs:
        d = a1_decs[0]
        actions = d.get("actions", d.get("ideas", []))
        buys = [a.get("symbol", "") for a in actions
                if (a.get("action") or a.get("intent") or "").upper() in ("BUY", "ADD") and a.get("symbol")]
        if buys:
            bullets.append(("orange", f"Bot eyeing entries: <b>{', '.join(buys[:3])}</b> &mdash; last decision has BUY/ADD intent"))
            if len(bullets) >= 6:
                return bullets

    # 4. A2 duplicate-block streak
    a2_decs = status.get("a2_decisions", [])
    dup_count = sum(1 for d in a2_decs if d.get("no_trade_reason") == "duplicate_submission_blocked")
    if dup_count >= 2:
        bullets.append(("orange",
            f"A2: {dup_count} of last 5 cycles blocked as duplicate submissions &mdash; possible stale structure"))
        if len(bullets) >= 6:
            return bullets

    # 5. Cost burn
    costs = status.get("costs", {})
    daily_cost = float(costs.get("daily_cost", 0) or 0)
    proj = daily_cost * 22
    if proj > 400:
        bullets.append(("critical", f"Cost burn &#x1F534; <b>{_fm(proj)}/month</b> projected ({_fm(daily_cost)}/day &times; 22 days)"))
    elif proj > 250:
        bullets.append(("orange", f"Cost burn &#x26A0;&#xFE0F; <b>{_fm(proj)}/month</b> projected ({_fm(daily_cost)}/day &times; 22 days)"))
    if len(bullets) >= 6:
        return bullets

    # 6. Regime score
    decision = status.get("decision", {})
    regime = decision.get("regime", decision.get("regime_view", ""))
    try:
        rs = float(decision.get("regime_score", 50) or 50)
        if rs < 25:
            bullets.append(("critical", f"Regime score {rs:.0f}/100 &mdash; risk-off: <b>{regime}</b>"))
        elif rs < 40:
            bullets.append(("orange", f"Regime score {rs:.0f}/100 &mdash; defensive: <b>{regime}</b>"))
    except Exception:
        pass

    return bullets[:6]


# ── Warning helpers ───────────────────────────────────────────────────────────
def _build_warnings(status: dict) -> list:
    warnings = []
    a1_mode = status["a1_mode"].get("mode", "normal").upper()
    a2_mode = status["a2_mode"].get("mode", "normal").upper()
    if a1_mode != "NORMAL":
        detail = status["a1_mode"].get("reason_detail", "")[:100]
        # Find pending SELL/TRIM actions from recent decisions
        pending_exits = []
        for d in status.get("a1_decisions", [])[:3]:
            for a in d.get("actions", d.get("ideas", [])):
                intent = (a.get("action") or a.get("intent") or "").upper()
                sym = a.get("symbol", "")
                if intent in ("SELL", "EXIT", "TRIM") and sym:
                    pending_exits.append(f"{intent} {sym}")
        blocked_str = ""
        if pending_exits:
            blocked_str = f" | Bot wants: {', '.join(pending_exits[:3])}"
        since = status["a1_mode"].get("entered_at", "")
        since_str = f" since {_to_et(since)}" if since else ""
        warnings.append(("critical", f"&#x26A0; A1 MODE: {a1_mode} &mdash; {detail}{since_str}{blocked_str}"))
    if a2_mode != "NORMAL":
        # Check for A2 duplicate-block streak
        a2_decs = status.get("a2_decisions", [])
        dup_count = sum(1 for d in a2_decs if d.get("no_trade_reason") == "duplicate_submission_blocked")
        dup_note = f" ({dup_count} consecutive duplicate-blocks)" if dup_count >= 2 else ""
        warnings.append(("orange", f"&#x26A0; A2 MODE: {a2_mode}{dup_note}"))
    if not status["a1"].get("ok"):
        warnings.append(("critical", f"&#x26A0; A1 API ERROR: {status['a1'].get('error','?')[:80]}"))
    if not status["a2"].get("ok"):
        warnings.append(("orange", f"&#x26A0; A2 API ERROR: {status['a2'].get('error','?')[:80]}"))
    for p in status["positions"]:
        ov = p.get("oversize", False)
        if ov == "critical":
            warnings.append(("critical", f"&#x26A1; {p['symbol']}: OVERSIZE CRITICAL ({_fp(p['pct_of_bp'])} of BP)"))
        elif ov in ("core", "dynamic"):
            warnings.append(("orange", f"&#x26A1; {p['symbol']}: OVERSIZE {ov.upper()} ({_fp(p['pct_of_bp'])} of BP)"))
        if p.get("gap_to_stop") is not None and p["gap_to_stop"] < 2.0:
            warnings.append(("orange", f"&#x1F534; {p['symbol']}: near stop &mdash; gap {p['gap_to_stop']:.1f}%"))
        if p.get("earnings"):
            warnings.append(("orange", f"&#x1F4C5; {p['symbol']}: {p['earnings']}"))
    return warnings


def _warnings_html(warnings: list) -> str:
    if not warnings:
        return ""
    parts = []
    for severity, msg in warnings:
        cls = "warn-critical" if severity == "critical" else "warn-orange"
        parts.append(f'<div class="{cls}">{msg}</div>')
    return "\n".join(parts)


# ── Ring SVG helper ───────────────────────────────────────────────────────────
def _ring_svg(pct: float, color: str = "#4facfe") -> str:
    circ = 138
    fill = min(circ, max(0, pct / 100 * circ))
    gap = circ - fill
    label = f"{int(round(pct))}%"
    return (
        f'<svg width="56" height="56" viewBox="0 0 56 56">'
        f'<circle cx="28" cy="28" r="22" fill="none" stroke="#1e2040" stroke-width="5"/>'
        f'<circle cx="28" cy="28" r="22" fill="none" stroke="{color}" stroke-width="5"'
        f' stroke-dasharray="{fill:.1f} {gap:.1f}" stroke-dashoffset="34" stroke-linecap="round"/>'
        f'<text x="28" y="32" text-anchor="middle" font-size="10" fill="{color}">{label}</text>'
        f'</svg>'
    )


# ── Ticker builder ────────────────────────────────────────────────────────────
def _build_ticker_html(positions: list, vix_str: str = "—") -> str:
    items = []
    for p in positions[:8]:
        sym = p.get("symbol", "")
        cur = p.get("current", 0)
        pct = p.get("unreal_plpc", 0)
        sign = "+" if pct >= 0 else ""
        cls = "tk-g" if pct >= 0 else "tk-r"
        items.append(
            f'<span class="tk-sym">{sym}</span>'
            f' <span class="tk-val">${cur:,.2f}</span>'
            f' <span class="{cls}">{sign}{pct:.1f}%</span>'
        )
    parts = ['<span class="tk-sep"> | </span>'.join(
        f'<span class="ticker-item">{i}</span>' for i in items
    )]
    parts.append('<span class="tk-sep"> | </span>')
    parts.append(f'<span class="tk-sym">VIX</span> <span class="tk-val">{vix_str}</span>')
    return (
        '<div class="ticker">'
        + '<span class="tk-dim" style="margin-right:6px;letter-spacing:1px;font-size:8px">LIVE</span>'
        + "".join(parts)
        + '</div>'
    )


# ── Navigation ────────────────────────────────────────────────────────────────
def _nav_html(active_page: str, now_et: str, a1_mode: str = "NORMAL", a2_mode: str = "NORMAL",
              session_label: str = "") -> str:
    pages = [
        ("overview", "/", "Overview"),
        ("a1", "/a1", "A1 Equity"),
        ("a2", "/a2", "A2 Options"),
        ("brief", "/brief", "Intelligence"),
        ("trades", "/trades", "Trades"),
        ("transparency", "/transparency", "Transparency"),
        ("theater", "/theater", "Decision Theater"),
    ]
    tabs = ""
    for pid, href, label in pages:
        cls = "nav-tab active" if pid == active_page else "nav-tab"
        tabs += f'<a href="{href}" class="{cls}">{label}</a>'

    a1_pill_cls = "npill-g" if a1_mode == "NORMAL" else ("npill-r" if "HALT" in a1_mode else "npill-a")
    a2_pill_cls = "npill-g" if a2_mode == "NORMAL" else ("npill-r" if "HALT" in a2_mode else "npill-a")
    sess_cls = "npill-a" if session_label and session_label != "MARKET" else "npill-g"
    sess_pill = f' <span class="npill {sess_cls}">{session_label}</span>' if session_label else ""

    return (
        f'<div class="nav">'
        f'<span class="nav-brand">Bull<span class="bear">Bear</span>Bot</span>'
        f'<div class="nav-tabs">{tabs}</div>'
        f'<div class="nav-pills">'
        f'<span class="npill {a1_pill_cls}">A1 {a1_mode}</span>'
        f'<span class="npill {a2_pill_cls}">A2 {a2_mode}</span>'
        f'{sess_pill}'
        f'</div>'
        f'<div class="nav-right">'
        f'<span style="font-size:10px;color:var(--text-muted);margin-right:12px">Paper trading &middot; not financial advice</span>'
        f'{now_et}&nbsp;&nbsp;&#x21BB;&nbsp;<span id="cd">60</span>s'
        f'</div>'
        f'</div>'
    )


def _page_shell(title: str, nav: str, body: str, ticker: str = "") -> str:
    return (
        '<!DOCTYPE html><html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta http-equiv="refresh" content="60">'
        f'<title>{title} — BullBearBot</title>'
        '<style>' + SHARED_CSS + '</style>'
        '</head><body>'
        + nav + body + ticker + _COUNTDOWN_JS +
        '</body></html>'
    )


# ── Performance widget helpers ────────────────────────────────────────────────
def _insuf_data_card(days: int) -> str:
    return (
        '<div class="card"><div style="color:#8b949e;font-size:13px">'
        f'Insufficient data ({days} day{"s" if days != 1 else ""} of outcomes — need 3+)</div></div>'
    )


def _pct_clr(pct: float | None, good: float = 55.0, warn: float = 45.0) -> str:
    if pct is None:
        return "#8b949e"
    return "#3fb950" if pct >= good else ("#d29922" if pct >= warn else "#f85149")


def _kv_row(label: str, val_html: str) -> str:
    return (
        f'<div class="kv"><span class="kv-label">{label}</span>'
        f'<span class="kv-val">{val_html}</span></div>'
    )


def _perf_overview_html(ps: dict) -> str:
    """One-liner performance card for the overview page."""
    days = ps.get("data_days", 0)
    if days < 3:
        return _insuf_data_card(days)
    si = ps.get("sonnet_ideas", {})
    al = ps.get("allocator", {})
    a2 = ps.get("a2_structures", {})
    parts = []
    apr_1d = si.get("approved_profitable_1d_pct")
    if apr_1d is not None:
        clr = _pct_clr(apr_1d)
        parts.append(f'Sonnet approved 1d: <span style="color:{clr};font-weight:600">{apr_1d:.0f}%</span>')
    follow_pct = al.get("follow_rate_pct")
    if follow_pct is not None:
        parts.append(f'Alloc follow: <span style="color:#c9d1d9">{follow_pct:.0f}%</span>')
    a2_win = a2.get("win_rate_pct")
    if a2_win is not None:
        clr = _pct_clr(a2_win)
        parts.append(f'A2 win: <span style="color:{clr}">{a2_win:.0f}%</span>')
    body = " &nbsp;|&nbsp; ".join(parts) if parts else "No outcome data yet."
    return (
        f'<div class="card"><div style="font-size:13px;color:#c9d1d9">'
        f'<span style="font-size:11px;color:#8b949e;text-transform:uppercase;'
        f'letter-spacing:0.5px;margin-right:8px">7d</span>'
        f'{body}</div></div>'
    )


def _perf_a1_decisions_html(ps: dict) -> str:
    """A1 Decision Quality widget for the A1 detail page."""
    days = ps.get("data_days", 0)
    if days < 3:
        return _insuf_data_card(days)
    si = ps.get("sonnet_ideas", {})
    al = ps.get("allocator", {})
    lines = []

    n_ideas = si.get("total_ideas_7d")
    if n_ideas is not None:
        lines.append(_kv_row("Ideas logged (7d)", f'<span style="color:#c9d1d9">{n_ideas}</span>'))
    apr_pct = si.get("approved_pct")
    if apr_pct is not None:
        lines.append(_kv_row("Approval rate", f'<span style="color:#c9d1d9">{apr_pct:.0f}%</span>'))
    apr_1d = si.get("approved_profitable_1d_pct")
    if apr_1d is not None:
        clr = _pct_clr(apr_1d)
        lines.append(_kv_row("Approved profitable (1d)", f'<span style="color:{clr}">{apr_1d:.0f}%</span>'))
    apr_5d = si.get("approved_profitable_5d_pct")
    if apr_5d is not None:
        clr = _pct_clr(apr_5d)
        lines.append(_kv_row("Approved profitable (5d)", f'<span style="color:{clr}">{apr_5d:.0f}%</span>'))
    rej_1d = si.get("rejected_wouldve_been_profitable_1d_pct")
    if rej_1d is not None:
        clr = "#f85149" if rej_1d > 55 else "#8b949e"
        lines.append(_kv_row("False kernel rejection (1d)", f'<span style="color:{clr}">{rej_1d:.0f}%</span>'))

    n_alloc = al.get("total_recommendations_7d")
    if n_alloc is not None:
        lines.append(_kv_row("Allocator recs (7d)", f'<span style="color:#c9d1d9">{n_alloc}</span>'))
    follow_pct = al.get("follow_rate_pct")
    if follow_pct is not None:
        clr = "#3fb950" if follow_pct >= 50 else "#8b949e"
        lines.append(_kv_row("Alloc follow rate", f'<span style="color:{clr}">{follow_pct:.0f}%</span>'))
    add_1d = al.get("add_accuracy_1d_pct")
    if add_1d is not None:
        clr = _pct_clr(add_1d)
        lines.append(_kv_row("ADD accuracy (1d)", f'<span style="color:{clr}">{add_1d:.0f}%</span>'))

    if not lines:
        return '<div class="card"><div style="color:#8b949e;font-size:13px">No decision quality data yet.</div></div>'
    return '<div class="card">' + "".join(lines) + '</div>'


def _perf_a2_strategies_html(ps: dict) -> str:
    """A2 Strategy Performance widget for the A2 detail page."""
    days = ps.get("data_days", 0)
    if days < 3:
        return _insuf_data_card(days)
    a2 = ps.get("a2_structures", {})
    lines = []

    n_sub = a2.get("total_submitted_7d")
    if n_sub is not None:
        lines.append(_kv_row("Structures submitted (7d)", f'<span style="color:#c9d1d9">{n_sub}</span>'))
    fill_pct = a2.get("fill_rate_pct")
    if fill_pct is not None:
        clr = "#3fb950" if fill_pct >= 70 else "#d29922"
        lines.append(_kv_row("Fill rate", f'<span style="color:{clr}">{fill_pct:.0f}%</span>'))
    win_pct = a2.get("win_rate_pct")
    if win_pct is not None:
        clr = _pct_clr(win_pct)
        lines.append(_kv_row("Win rate", f'<span style="color:{clr}">{win_pct:.0f}%</span>'))
    avg_pnl = a2.get("avg_pnl_pct_of_max_gain")
    if avg_pnl is not None:
        clr = "#3fb950" if avg_pnl >= 0 else "#f85149"
        sign = "+" if avg_pnl >= 0 else ""
        lines.append(_kv_row("Avg P&amp;L (% of max gain)", f'<span style="color:{clr}">{sign}{avg_pnl:.1f}%</span>'))

    by_strat = a2.get("by_strategy", {})
    if by_strat:
        lines.append(
            '<div style="margin-top:8px;font-size:11px;color:#8b949e;'
            'text-transform:uppercase;letter-spacing:0.5px">By Strategy</div>'
        )
        for strat, sv in by_strat.items():
            n = sv.get("count", 0)
            wr = sv.get("win_rate_pct")
            wr_str = f'{wr:.0f}%' if wr is not None else "?"
            wr_clr = _pct_clr(wr)
            lines.append(
                f'<div class="kv"><span class="kv-label" style="color:#8b949e">'
                f'{strat.replace("_", " ")}</span>'
                f'<span class="kv-val">{n} trades &nbsp;'
                f'<span style="color:{wr_clr}">{wr_str} WR</span></span></div>'
            )
    if not lines:
        return '<div class="card"><div style="color:#8b949e;font-size:13px">No A2 strategy performance data yet.</div></div>'
    return '<div class="card">' + "".join(lines) + '</div>'


# ── Overview page ─────────────────────────────────────────────────────────────
def _page_overview(status: dict, now_et: str) -> str:
    a1d = status["a1"]
    a2d = status["a2"]
    a1_acc = a1d.get("account")
    a2_acc = a2d.get("account")
    a1_mode = status["a1_mode"].get("mode", "unknown").upper()
    a2_mode = status["a2_mode"].get("mode", "unknown").upper()
    a1_color = _mode_color(a1_mode)
    a2_color = _mode_color(a2_mode)
    nav = _nav_html("overview", now_et, a1_mode, a2_mode)
    warn_html = _warnings_html(status.get("warnings", []))

    a1_pnl, a1_pnl_pct = status.get("today_pnl_a1", (0.0, 0.0))
    a2_pnl, a2_pnl_pct = status.get("today_pnl_a2", (0.0, 0.0))
    a1_pnl_color = "#3fb950" if a1_pnl >= 0 else "#f85149"
    a2_pnl_color = "#3fb950" if a2_pnl >= 0 else "#f85149"
    a1_pnl_sign = "+" if a1_pnl >= 0 else ""
    a2_pnl_sign = "+" if a2_pnl >= 0 else ""

    if a1_acc:
        a1_equity = float(a1_acc.equity or 0)
        a1_pos_count = len(status["positions"])
        a1_unreal = sum(p["unreal_pl"] for p in status["positions"])
        a1_unreal_c = "#3fb950" if a1_unreal >= 0 else "#f85149"
        a1_unreal_s = "+" if a1_unreal >= 0 else ""
        a1_invested = sum(p["market_val"] for p in status["positions"])
        a1_util = min(100.0, a1_invested / a1_equity * 100) if a1_equity else 0.0
    else:
        a1_equity = a1_invested = a1_util = 0.0
        a1_pos_count = 0
        a1_unreal = 0.0; a1_unreal_c = "#8b949e"; a1_unreal_s = ""

    if a2_acc:
        a2_equity = float(a2_acc.equity or 0)
        a2_pos_count = len(a2d.get("positions", []))
    else:
        a2_equity = 0.0
        a2_pos_count = 0

    costs = status["costs"]
    gate = status["gate"]
    daily_cost = float(costs.get("daily_cost", 0) or 0)
    proj_monthly = daily_cost * 22
    if proj_monthly > 400:
        proj_color = "#f85149"
        proj_icon = "&#x1F534; "
    elif proj_monthly > 250:
        proj_color = "#d29922"
        proj_icon = "&#x26A0;&#xFE0F; "
    else:
        proj_color = "#3fb950"
        proj_icon = ""
    sonnet_calls = gate.get("total_calls_today", "—")
    buys = status["buys_today"]
    sells = status["sells_today"]

    # Top-3 callers by cost
    by_caller = costs.get("by_caller", {}) if isinstance(costs, dict) else {}
    top_callers = sorted(by_caller.items(), key=lambda x: float(x[1].get("cost", 0) if isinstance(x[1], dict) else 0), reverse=True)[:3]
    callers_html = ""
    for caller, cdata in top_callers:
        c_cost = float(cdata.get("cost", 0) if isinstance(cdata, dict) else 0)
        c_calls = cdata.get("calls", 0) if isinstance(cdata, dict) else 0
        callers_html += (
            f'<div style="font-size:11px;color:#8b949e;padding:2px 0">'
            f'<span style="color:#c9d1d9">{caller}</span>: {_fm(c_cost)} ({c_calls} calls)</div>'
        )

    flags_list = []
    for p in status["positions"]:
        if p.get("earnings"):
            flags_list.append(f'<div class="alert alert-orange">&#x1F4C5; {p["symbol"]}: {p["earnings"]}</div>')
        ov = p.get("oversize", False)
        if ov == "critical":
            flags_list.append(f'<div class="alert alert-red">&#x26A1; {p["symbol"]}: OVERSIZE CRITICAL ({_fp(p["pct_of_bp"])} of BP)</div>')
        elif ov == "core":
            flags_list.append(f'<div class="alert alert-orange">&#x26A1; {p["symbol"]}: OVERSIZE ({_fp(p["pct_of_bp"])} &gt; 20%)</div>')
        elif ov == "dynamic":
            flags_list.append(f'<div class="alert alert-orange">&#x26A1; {p["symbol"]}: OVER DYN TIER ({_fp(p["pct_of_bp"])} &gt; 15%)</div>')
        if p.get("gap_to_stop") is not None and p["gap_to_stop"] < 2.0:
            flags_list.append(f'<div class="alert alert-orange">&#x1F534; {p["symbol"]}: stop gap {p["gap_to_stop"]:.1f}%</div>')
    if not flags_list:
        flags_list = ['<div class="alert alert-green">&#x2713; No active flags</div>']
    flags_html = "\n".join(flags_list)

    a2_dec = status.get("a2_decision") or {}
    a2_ts = _to_et(a2_dec.get("built_at", "")) if a2_dec else "—"
    git_hash = status["git_hash"]
    svc_uptime = status["service_uptime"]

    # Earnings calendar staleness
    try:
        import sys as _sys  # noqa: PLC0415
        _bot_dir = str(BOT_DIR)
        if _bot_dir not in _sys.path:
            _sys.path.insert(0, _bot_dir)
        import data_warehouse as _dw  # noqa: PLC0415
        _ec_stale = _dw.get_earnings_calendar_staleness()
    except Exception:
        _ec_stale = {"stale": False, "warning": False, "hours_old": None, "entry_count": 0}
    _ec_hours = _ec_stale.get("hours_old")
    _ec_count = _ec_stale.get("entry_count", 0)
    if _ec_stale.get("warning"):
        _ec_icon  = "&#x1F534;"
        _ec_color = "#f85149"
        _ec_label = f"{_ec_hours:.0f}h ago" if _ec_hours is not None else "unknown age"
    elif _ec_stale.get("stale"):
        _ec_icon  = "&#x26A0;&#xFE0F;"
        _ec_color = "#d29922"
        _ec_label = f"{_ec_hours:.0f}h ago" if _ec_hours is not None else "unknown age"
    else:
        _ec_icon  = "&#x2705;"
        _ec_color = "#3fb950"
        _ec_label = f"updated {_ec_hours:.0f}h ago" if _ec_hours is not None else "fresh"
    earnings_cal_html = (
        f'<div class="kv"><span class="kv-label">Earnings cal</span>'
        f'<span class="kv-val" style="color:{_ec_color}">'
        f'{_ec_icon} {_ec_label} ({_ec_count} entries)</span></div>'
    )

    # Performance summary widget
    perf_7d_html = _perf_overview_html(status.get("perf_summary", {}))

    # P&L hero
    combined_pnl = a1_pnl + a2_pnl
    combined_color = "#3fb950" if combined_pnl >= 0 else "#f85149"
    combined_sign = "+" if combined_pnl >= 0 else ""

    # Watch Now
    watch_bullets = _watch_now_bullets(status)
    if watch_bullets:
        items = ""
        for sev, text in watch_bullets:
            icon = "&#x1F534;" if sev == "critical" else "&#x26A0;&#xFE0F;"
            color = "#f85149" if sev == "critical" else "#d29922"
            items += f'<div class="watch-bullet"><span style="color:{color}">{icon}</span> {text}</div>'

    # Trail table
    trail_tiers = status.get("trail_tiers", [])
    trail_html = _trail_table_html(status["positions"], trail_tiers)

    # Allocator compact line
    allocator_line = status.get("allocator_line", "") or ""

    # Compact decisions (two-column grid)
    a1_comp = _a1_decisions_compact_html(status.get("a1_decisions", []))
    a2_comp = _a2_decisions_compact_html(status.get("a2_decisions", []))

    # A2 pipeline today
    a2_pipe = status.get("a2_pipeline", {})
    a2_pipe_total = a2_pipe.get("total", 0)
    a2_pipe_str = (
        f'Today: {a2_pipe_total} structures &mdash; '
        f'{a2_pipe.get("fully_filled",0)} filled / '
        f'{a2_pipe.get("submitted",0)} submitted / '
        f'{a2_pipe.get("cancelled",0)} cancelled / '
        f'{a2_pipe.get("proposed",0)} proposed'
    ) if a2_pipe_total else "No A2 structures today."

    # VIX for ticker
    _vix_for_ticker = "—"
    try:
        pf_entries_t = _jsonl_last(BOT_DIR / "data/status/preflight_log.jsonl", n=1)
        if pf_entries_t:
            for chk in pf_entries_t[0].get("checks", []):
                if chk.get("name") == "vix_gate" and "VIX=" in chk.get("message", ""):
                    _vix_for_ticker = chk["message"].split("VIX=")[1].split()[0]
    except Exception:
        pass
    ticker = _build_ticker_html(status["positions"], _vix_for_ticker)

    # Build positions compact HTML for overview
    _pos_rows = ""
    for p in status["positions"][:8]:
        _sym = p["symbol"]
        _pl = p.get("unreal_pl", 0)
        _plpc = p.get("unreal_plpc", 0)
        _entry = p.get("entry", 0)
        _cur = p.get("current", 0)
        _stop = p.get("stop")
        _plc = "var(--accent-green)" if _pl >= 0 else "var(--accent-red)"
        _pls = "+" if _pl >= 0 else ""
        _earn = p.get("earnings", "")
        _earn_dot = ' <span style="color:var(--accent-amber);font-size:9px">&#9679;</span>' if _earn else ""
        _stop_str = f'<span style="color:var(--text-muted);font-size:9px">stop {_fm(_stop)}</span>' if _stop else ""
        # range bar: stop→entry→current
        _bar = ""
        if _entry and _stop and _cur and _entry > _stop:
            _span = max(_cur * 1.15 - _stop, _cur - _stop + 1)
            _ep = min(99, (_entry - _stop) / _span * 100)
            _cp = min(99, (_cur - _stop) / _span * 100)
            _fill_l = min(_ep, _cp)
            _fill_w = abs(_cp - _ep)
            _fill_c = "var(--accent-green)" if _cur >= _entry else "var(--accent-red)"
            _bar = (
                f'<div class="range-track" style="margin:3px 0 5px">'
                f'<div class="range-fill" style="left:{_fill_l:.0f}%;width:{_fill_w:.0f}%;'
                f'background:{_fill_c};opacity:0.6"></div>'
                f'<div style="position:absolute;left:{_ep:.0f}%;top:-1px;width:2px;height:7px;'
                f'background:var(--text-muted);border-radius:1px"></div>'
                f'<div style="position:absolute;left:{_cp:.0f}%;top:-1px;width:2px;height:7px;'
                f'background:{_fill_c}"></div>'
                f'</div>'
            )
        _pos_rows += (
            f'<div style="padding:6px 0;border-bottom:1px solid var(--border-subtle)">'
            f'<div style="display:flex;justify-content:space-between;align-items:baseline">'
            f'<span style="color:var(--accent-blue);font-size:12px">{_sym}</span>{_earn_dot}'
            f'<span style="color:{_plc};font-size:11px">{_pls}{_fm(_pl)} ({_pls}{_plpc:.1f}%)</span>'
            f'</div>'
            f'{_bar}'
            f'<div style="display:flex;justify-content:space-between">'
            f'<span style="color:var(--text-muted);font-size:9px">{int(p.get("qty",0))} sh · entry {_fm(_entry)}</span>'
            f'{_stop_str}'
            f'</div>'
            f'</div>'
        )
    if not _pos_rows:
        _pos_rows = '<div style="color:var(--text-muted);font-size:11px">No open positions.</div>'

    # conviction picks from morning brief
    _brief = status.get("morning_brief") or {}
    _picks = _brief.get("conviction_picks", [])
    _conv_rows = ""
    for _pk in _picks[:6]:
        _psym = _pk.get("symbol", "")
        _pdir = _pk.get("direction", "long")
        _pconv = _pk.get("conviction", "medium")
        _pcat = _pk.get("catalyst", {})
        _ptxt = (_pcat.get("short_text", "") if isinstance(_pcat, dict) else str(_pcat or ""))[:55]
        _tcls = "var(--accent-green)" if _pconv == "high" else ("var(--accent-amber)" if _pconv == "medium" else "var(--text-muted)")
        _dicon = "&#x2191;" if _pdir == "long" else "&#x2193;"
        _dirc = "var(--accent-green)" if _pdir == "long" else "var(--accent-red)"
        _conv_rows += (
            f'<div style="display:flex;align-items:baseline;gap:6px;padding:5px 0;border-bottom:1px solid var(--border-subtle)">'
            f'<span style="color:{_tcls};font-size:9px;min-width:28px">{_pconv.upper()[:3]}</span>'
            f'<span style="color:{_dirc};font-size:9px">{_dicon}</span>'
            f'<span style="color:var(--accent-blue);font-size:11px;min-width:38px">{_psym}</span>'
            f'<span style="color:var(--text-muted);font-size:10px">{_ptxt}</span>'
            f'</div>'
        )
    if not _conv_rows:
        _conv_rows = '<div style="color:var(--text-muted);font-size:10px">Brief not yet generated.</div>'

    # regime score for combo card
    _decision = status.get("decision", {})
    _regime_score = _decision.get("regime_score") or 50
    try:
        _regime_score = float(_regime_score)
    except Exception:
        _regime_score = 50.0
    _regime_view = _decision.get("regime_view", _decision.get("regime", "—"))

    body = f"""
<div class="container">

{warn_html}

<div class="section-label">Today&apos;s P&amp;L</div>
<div class="hero-grid">
  <div class="hero-card hero-card-a1">
    <div class="hero-inner">
      <div>
        <div class="hero-lbl">A1 Equity &middot; Today</div>
        <div class="hero-num" style="color:{a1_pnl_color}">{a1_pnl_sign}{_fm(a1_pnl)}</div>
        <div class="hero-sub">{_fm(a1_equity)} &middot; {a1_pos_count} positions</div>
        <span class="hero-badge {'hero-badge-g' if a1_pnl >= 0 else 'hero-badge-r'}">{a1_pnl_sign}{a1_pnl_pct:.2f}%</span>
      </div>
      {_ring_svg(a1_util, "#4facfe")}
    </div>
    <div class="hero-mini-stats" style="margin-top:10px">
      <div class="hero-mini-row"><span class="hero-mini-lbl">Utilization</span><span style="color:var(--accent-blue)">{a1_util:.0f}%</span></div>
      <div class="hero-mini-row"><span class="hero-mini-lbl">Unrealized P&amp;L</span><span style="color:{a1_unreal_c}">{a1_unreal_s}{_fm(a1_unreal)}</span></div>
      <div class="hero-mini-row"><span class="hero-mini-lbl">Mode</span><span style="color:{a1_color}">{a1_mode}</span></div>
    </div>
    <div style="text-align:right;margin-top:8px"><a href="/a1" style="font-size:10px;color:var(--text-muted)">Detail &rarr;</a></div>
  </div>
  <div class="hero-card hero-card-a2">
    <div class="hero-inner">
      <div>
        <div class="hero-lbl">A2 Options &middot; Today</div>
        <div class="hero-num" style="color:{a2_pnl_color}">{a2_pnl_sign}{_fm(a2_pnl)}</div>
        <div class="hero-sub">{_fm(a2_equity)} &middot; {a2_pos_count} structures</div>
        <span class="hero-badge {'hero-badge-g' if a2_pnl >= 0 else 'hero-badge-r'}">{a2_pnl_sign}{a2_pnl_pct:.2f}%</span>
      </div>
      {_ring_svg(min(100, a2_pos_count * 10), "#a855f7")}
    </div>
    <div class="hero-mini-stats" style="margin-top:10px">
      <div class="hero-mini-row"><span class="hero-mini-lbl">Last cycle</span><span class="hero-mini-val">{a2_ts}</span></div>
      <div class="hero-mini-row"><span class="hero-mini-lbl">Pipeline today</span><span class="hero-mini-val">{a2_pipe_str[:40]}</span></div>
      <div class="hero-mini-row"><span class="hero-mini-lbl">Mode</span><span style="color:{a2_color}">{a2_mode}</span></div>
    </div>
    <div style="text-align:right;margin-top:8px"><a href="/a2" style="font-size:10px;color:var(--text-muted)">Detail &rarr;</a></div>
  </div>
  <div class="hero-card hero-card-combo">
    <div class="hero-inner">
      <div>
        <div class="hero-lbl">Combined &middot; Portfolio</div>
        <div class="hero-num" style="color:{combined_color}">{combined_sign}{_fm(combined_pnl)}</div>
        <div class="hero-sub">Paper trading</div>
      </div>
      {_ring_svg(_regime_score, "#ffaa20")}
    </div>
    <div class="hero-mini-stats" style="margin-top:10px">
      <div class="hero-mini-row"><span class="hero-mini-lbl">Regime score</span><span style="color:var(--accent-amber)">{_regime_score:.0f}/100</span></div>
      <div class="hero-mini-row"><span class="hero-mini-lbl">Claude cost today</span><span style="color:{proj_color}">{_fm(daily_cost)}</span></div>
      <div class="hero-mini-row"><span class="hero-mini-lbl">Sonnet calls</span><span class="hero-mini-val">{sonnet_calls}</span></div>
    </div>
  </div>
</div>

<div class="tri-grid">
  <div>
    <div class="section-label">Open Positions</div>
    <div class="card" style="padding:10px 14px">{_pos_rows}</div>
    <div class="section-label">Trail Status</div>
    <div class="card" style="padding:0 0 4px">{trail_html}</div>
  </div>
  <div>
    <div class="section-label">Watch Now</div>
    <div class="card" style="padding:10px 14px">
      {''.join(f'<div class="watch-bullet"><span style="color:{"var(--accent-red)" if s=="critical" else "var(--accent-amber)"}">{("&#x25CF;" if s=="critical" else "&#x25B2;")}</span> {t}</div>' for s,t in (watch_bullets or [("ok", "&#x2713; Nothing urgent.")]))}
    </div>
    {f'<div class="section-label">Allocator</div><div class="card" style="font-size:10px;color:var(--text-muted)">{allocator_line}</div>' if allocator_line else ''}
  </div>
  <div>
    <div class="section-label">Conviction</div>
    <div class="card" style="padding:10px 14px">{_conv_rows}</div>
  </div>
</div>

<div class="compact-grid">
  <div>
    <div class="section-label">Recent Decisions</div>
    <div class="card" style="padding:10px 12px">
      <div style="font-size:9px;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.8px;margin-bottom:6px">A1</div>
      {a1_comp}
      <div style="font-size:9px;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.8px;margin:8px 0 6px">A2</div>
      {a2_comp}
    </div>
  </div>
  <div>
    <div class="section-label">System &amp; Performance</div>
    <div class="card">
      <div class="card-row"><span class="card-label">Git HEAD</span><span class="card-val" style="font-family:monospace;font-size:10px">{git_hash}</span></div>
      <div class="card-row"><span class="card-label">Service up since</span><span class="card-val muted" style="font-size:10px">{svc_uptime[:30] if svc_uptime != "unknown" else "unknown"}</span></div>
      {earnings_cal_html}
      <div class="card-row"><span class="card-label">Buys / Sells today</span><span class="card-val">{buys} / {sells}</span></div>
      <div class="card-row"><span class="card-label">Proj/month (22d)</span><span class="card-val" style="color:{proj_color}">{proj_icon}{_fm(proj_monthly)}</span></div>
    </div>
    <div class="section-label">Active Flags</div>
    {flags_html}
  </div>
</div>

<div class="section-label">Performance (7d)</div>
{perf_7d_html}

</div>"""
    return _page_shell("Overview", nav, body, ticker)


# ── A1 detail page ────────────────────────────────────────────────────────────
def _page_a1(status: dict, now_et: str) -> str:
    a1d = status["a1"]
    a1_acc = a1d.get("account")
    a1_mode = status["a1_mode"].get("mode", "unknown").upper()
    a2_mode = status["a2_mode"].get("mode", "unknown").upper()
    nav = _nav_html("a1", now_et, a1_mode, a2_mode)
    warn_html = _warnings_html(status.get("warnings", []))
    ticker = _build_ticker_html(status["positions"])

    a1_pnl, a1_pnl_pct = status.get("today_pnl_a1", (0.0, 0.0))
    a1_pnl_color = "var(--accent-green)" if a1_pnl >= 0 else "var(--accent-red)"
    a1_pnl_sign = "+" if a1_pnl >= 0 else ""

    if a1_acc:
        a1_equity = float(a1_acc.equity or 0)
        a1_cash = float(a1_acc.cash or 0)
        a1_bp = float(a1_acc.buying_power or 0)
        a1_pos_count = len(status["positions"])
        a1_unreal = sum(p["unreal_pl"] for p in status["positions"])
        a1_unreal_c = "#3fb950" if a1_unreal >= 0 else "#f85149"
        a1_unreal_s = "+" if a1_unreal >= 0 else ""
        a1_invested = sum(p["market_val"] for p in status["positions"])
        a1_util = min(100.0, a1_invested / a1_equity * 100) if a1_equity else 0.0
        a1_util_c = "#f85149" if a1_util > 80 else ("#d29922" if a1_util > 60 else "#3fb950")
    else:
        a1_equity = a1_cash = a1_bp = a1_invested = a1_util = 0.0
        a1_pos_count = 0
        a1_unreal = 0.0; a1_unreal_c = "#8b949e"; a1_unreal_s = ""
        a1_util_c = "#8b949e"

    # Morning brief
    brief = status.get("morning_brief", {})
    brief_time_str = status.get("morning_brief_time", "?")
    if brief:
        tone = brief.get("market_tone", "?").upper()
        picks = brief.get("conviction_picks", [])
        tl = tone.lower()
        tone_color = "#3fb950" if "bull" in tl else ("#f85149" if "bear" in tl else "#d29922")
        picks_html = ""
        for pick in picks[:6]:
            sym = pick.get("symbol", "")
            direction = pick.get("direction", "")
            cat_raw = pick.get("catalyst", {})
            cat_text = cat_raw.get("short_text", "") if isinstance(cat_raw, dict) else str(cat_raw or "")
            conviction = pick.get("conviction", "")
            dir_icon = "&#x2191;" if direction == "long" else "&#x2193;"
            dir_color = "#3fb950" if direction == "long" else "#f85149"
            conv_badge = ""
            if conviction == "high":
                conv_badge = ' <span style="font-size:10px;background:#0d2018;color:#3fb950;padding:1px 4px;border-radius:3px">HIGH</span>'
            elif conviction == "medium":
                conv_badge = ' <span style="font-size:10px;background:#2d2208;color:#d29922;padding:1px 4px;border-radius:3px">MED</span>'
            picks_html += (
                f'<div style="padding:5px 0;border-bottom:1px solid #21262d;font-size:13px">'
                f'<b style="color:{dir_color}">{dir_icon} {sym}</b>{conv_badge}'
                f' <span style="color:#8b949e">— {cat_text[:90]}</span></div>'
            )
        stale_html = _brief_staleness_html(status.get("morning_brief_mtime", 0))
        brief_html = (
            f'<div style="font-size:12px;color:#8b949e;margin-bottom:8px">'
            f'Tone: <b style="color:{tone_color}">{tone}</b> &nbsp;|&nbsp; {len(picks)} picks'
            f' &nbsp;|&nbsp; {brief_time_str}{stale_html}</div>{picks_html}'
        )
    else:
        brief_html = '<div style="color:#8b949e;font-size:13px">Morning brief not yet generated.</div>'

    # Top 5 theses
    a1_theses = status.get("a1_theses", [])
    if a1_theses:
        theses_html = ""
        for th in a1_theses:
            intent_color = "#3fb950" if th["intent"] in ("BUY", "ADD") else (
                "#f85149" if th["intent"] in ("SELL", "EXIT", "TRIM") else "#d29922")
            tags_html = " ".join(
                f'<span style="font-size:10px;background:#21262d;color:#8b949e;padding:1px 5px;border-radius:3px">{t}</span>'
                for t in th["tags"]
            )
            cat_dot = ' <span style="color:#d29922;font-size:10px">&#x25CF; catalyst active</span>' if th["catalyst_active"] else ""
            theses_html += (
                f'<div class="thesis-card">'
                f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">'
                f'<b style="color:#58a6ff;font-size:15px">{th["symbol"]}</b>'
                f'<span style="font-size:11px;font-weight:700;color:{intent_color}">{th["intent"]}</span>'
                f'{cat_dot}'
                f'<span style="margin-left:auto;font-size:11px;color:#8b949e">{_to_et(th["ts"])}</span>'
                f'</div>'
                f'<div style="font-size:13px;color:#c9d1d9;margin-bottom:6px">{th["narrative"] or "No narrative available."}</div>'
                f'<div style="display:flex;gap:4px;flex-wrap:wrap">{tags_html}</div>'
                f'</div>'
            )
    else:
        theses_html = '<div style="color:#8b949e;font-size:13px">No thesis data yet.</div>'

    # Compact decisions summary
    a1_comp = _a1_decisions_compact_html(status.get("a1_decisions", []))

    # Last 5 A1 decisions
    a1_decs = status.get("a1_decisions", [])
    a1_decs_html = ""
    for d in a1_decs[:5]:
        ts = _to_et(d.get("ts", ""))
        regime = d.get("regime", d.get("regime_view", "?"))
        score = d.get("regime_score", "?")
        actions = d.get("actions", d.get("ideas", []))
        r_raw = d.get("reasoning", "")
        r_short = r_raw[:140] + ("…" if len(r_raw) > 140 else "")
        act_strs = [f"{a.get('symbol','')} {(a.get('action') or a.get('intent') or '').upper()}"
                    for a in actions[:4] if a.get("symbol")]
        acts_line = ", ".join(act_strs) if act_strs else "—"
        regime_color = "#3fb950" if "risk_on" in regime or "bullish" in regime else (
            "#f85149" if "risk_off" in regime or "bearish" in regime else "#d29922")
        a1_decs_html += (
            f'<div style="padding:8px 0;border-bottom:1px solid #21262d">'
            f'<div style="font-size:12px"><span style="color:#8b949e">[{ts}]</span> '
            f'<span style="color:{regime_color};font-weight:600">{regime}</span> '
            f'<span style="color:#8b949e">({score})</span></div>'
            f'<div style="font-size:12px;color:#c9d1d9;margin:2px 0;font-style:italic">{r_short}</div>'
            f'<div style="font-size:12px;color:#3fb950">Actions: {acts_line}</div>'
            f'</div>'
        )
    if not a1_decs_html:
        a1_decs_html = '<div style="color:#8b949e;font-size:13px">No decisions yet.</div>'

    # Positions table with trail badge
    positions = status["positions"]
    pos_sorted = sorted(positions, key=lambda x: -abs(x.get("unreal_pl", 0)))
    pos_display = pos_sorted[:10]
    pos_extra = max(0, len(positions) - 10)
    positions_html = ""
    for p in pos_display:
        sym = p["symbol"]
        pl = p.get("unreal_pl", 0)
        plpc = p.get("unreal_plpc", 0)
        stop = p.get("stop")
        gap = p.get("gap_to_stop")
        pct_bp = p.get("pct_of_bp", 0)
        entry = p.get("entry", 0.0)
        oversize = p.get("oversize", False)
        earnings_flag = p.get("earnings", "")
        row_bg = "background:#2d2208;" if gap is not None and gap < 2.0 else ""
        pl_color = "#3fb950" if pl >= 0 else "#f85149"
        pl_sign = "+" if pl >= 0 else ""
        trail_badge = _trail_status_badge(entry, stop) if stop else ""
        flags_h = ""
        if earnings_flag:
            flags_h += f' <span class="flag flag-earn">{earnings_flag}</span>'
        if oversize == "critical":
            flags_h += ' <span class="flag flag-over">OVERSIZE!</span>'
        elif oversize == "core":
            flags_h += ' <span class="flag flag-over">OVERSIZE</span>'
        elif oversize == "dynamic":
            flags_h += ' <span class="flag flag-warn">OVER DYN</span>'
        stop_str = _fm(stop) if stop else "—"
        gap_str = f"{gap:.1f}%" if gap is not None else "—"
        gap_color = "#d29922" if gap is not None and gap < 2.0 else "#e6edf3"
        positions_html += (
            f'<tr style="{row_bg}">'
            f'<td><b>{sym}</b>{flags_h}{trail_badge}</td>'
            f'<td>{int(p.get("qty", 0))}</td>'
            f'<td>{_fm(entry)}</td>'
            f'<td>{_fm(p.get("current"))}</td>'
            f'<td style="color:{pl_color}">{pl_sign}{_fm(pl)}</td>'
            f'<td style="color:{pl_color}">{pl_sign}{_fp(plpc)}</td>'
            f'<td>{stop_str}</td>'
            f'<td style="color:{gap_color}">{gap_str}</td>'
            f'<td>{_fp(pct_bp)}</td>'
            f'</tr>'
        )
    pos_extra_note = (f'<div style="font-size:12px;color:#8b949e;padding:6px 10px">+{pos_extra} more not shown</div>'
                      if pos_extra else "")

    # Today's activity
    gate = status["gate"]
    costs = status["costs"]
    trades = status["trades"]
    decision = status["decision"]
    shadow = status["shadow"]
    sonnet_calls = gate.get("total_calls_today", "—")
    sonnet_skips = gate.get("total_skips_today", "—")
    last_sonnet_ts = _to_et(gate.get("last_sonnet_call_utc", ""))
    last_regime = gate.get("last_regime", "—")
    daily_cost = float(costs.get("daily_cost", 0) or 0)
    proj_monthly_a1 = daily_cost * 22
    if proj_monthly_a1 > 400:
        proj_color = "#f85149"
    elif proj_monthly_a1 > 250:
        proj_color = "#d29922"
    else:
        proj_color = "#3fb950"
    buys_today = status["buys_today"]
    sells_today = status["sells_today"]
    rejected = [t for t in trades if t.get("status") == "rejected"]
    trail_stops = [t for t in trades if t.get("event") == "trail_stop"]

    # VIX from preflight log
    last_vix_pf = "—"
    pf_entries = _jsonl_last(BOT_DIR / "data/status/preflight_log.jsonl", n=1)
    if pf_entries:
        for chk in pf_entries[0].get("checks", []):
            if chk.get("name") == "vix_gate" and "VIX=" in chk.get("message", ""):
                last_vix_pf = chk["message"].split("VIX=")[1].split()[0]

    regime_score = decision.get("regime_score", "—")
    dec_session = decision.get("session", "—")
    reasoning_raw = decision.get("reasoning", "")
    sentences = reasoning_raw.split(". ")
    reasoning_2s = ". ".join(sentences[:2]) + ("." if len(sentences) > 1 else "")
    if len(reasoning_2s) > 280:
        reasoning_2s = reasoning_2s[:277] + "…"
    last_dec_ts = _to_et(decision.get("ts", ""))

    # Allocator shadow
    alloc = shadow.get("shadow_systems", {}).get("portfolio_allocator", {})
    alloc_st = alloc.get("status", "—")
    alloc_last = _to_et(alloc.get("last_run_at", ""))

    # Decision quality performance widget
    dec_quality_html = _perf_a1_decisions_html(status.get("perf_summary", {}))

    # Recent errors
    import html as _html
    log_errors = status["log_errors"]
    _err_lines = ""
    for err in log_errors:
        lc = "#f85149" if "  ERROR  " in err or "  CRITICAL  " in err else "#d29922"
        _err_lines += f'<div class="log-line" style="color:{lc}">{_html.escape(err[-180:])}</div>'
    if not _err_lines:
        _err_lines = '<div class="log-line" style="color:#3fb950">No recent warnings or errors</div>'
    errors_html = (
        f'<details><summary style="font-size:11px;color:var(--text-muted);cursor:pointer;padding:4px 0">'
        f'Raw logs (debug) &#x25BE;</summary>'
        f'<div style="margin-top:6px">{_err_lines}</div></details>'
    )

    a1_orders_html = _fmt_orders_html(a1d.get("recent_orders", []), is_options=False, limit=6)

    body = f"""
<div class="container">
{warn_html}
<div class="section-label">A1 Account Summary</div>
<div class="acct-bar">
  <div class="acct-bar-item"><div class="acct-bar-lbl">Equity</div><div class="acct-bar-val">{_fm(a1_equity)}</div></div>
  <div class="acct-bar-item"><div class="acct-bar-lbl">Cash</div><div class="acct-bar-val">{_fm(a1_cash)}</div></div>
  <div class="acct-bar-item"><div class="acct-bar-lbl">Buying Power</div><div class="acct-bar-val">{_fm(a1_bp)}</div></div>
  <div class="acct-bar-item"><div class="acct-bar-lbl">Positions</div><div class="acct-bar-val">{a1_pos_count}</div></div>
  <div class="acct-bar-item"><div class="acct-bar-lbl">Today P&amp;L</div><div class="acct-bar-val" style="color:{a1_pnl_color}">{a1_pnl_sign}{_fm(a1_pnl)} ({a1_pnl_sign}{a1_pnl_pct:.2f}%)</div></div>
  <div class="acct-bar-item"><div class="acct-bar-lbl">Unrealized P&amp;L</div><div class="acct-bar-val" style="color:{a1_unreal_c}">{a1_unreal_s}{_fm(a1_unreal)}</div></div>
</div>
<div style="margin-bottom:10px">
  <div style="display:flex;justify-content:space-between;font-size:9px;color:var(--text-muted);margin-bottom:3px">
    <span>Capital utilization</span><span style="color:{a1_util_c}">{a1_util:.0f}% of ${a1_invested:,.0f} deployed</span>
  </div>
  <div class="progress-wrap"><div class="progress-fill" style="width:{a1_util:.0f}%;background:{a1_util_c}"></div></div>
</div>

<div class="section-label">Morning Brief</div>
<div class="card">{brief_html}</div>

<div class="section-label">Active Theses</div>
<div class="card">{theses_html}</div>

<div class="section-label">Last 5 Decisions</div>
<div class="card" style="padding:10px 14px">
  <details>
    <summary style="font-size:10px;color:var(--text-muted);cursor:pointer;margin-bottom:6px">Expand full reasoning &#x25BE;</summary>
    <div class="dec-panel" style="margin-top:8px">{a1_decs_html}</div>
  </details>
  <div>{a1_comp}</div>
</div>

<div class="section-label">Positions ({a1_pos_count} open)</div>
<div class="card" style="padding:0 0 4px">
  <div class="table-wrap">
    <table class="data-table">
      <thead><tr>
        <th>Symbol</th><th>Qty</th><th>Entry</th><th>Current</th>
        <th>P&amp;L $</th><th>P&amp;L %</th><th>Stop</th><th>Gap%</th><th>%BP</th>
      </tr></thead>
      <tbody>{positions_html}</tbody>
    </table>
  </div>
  {pos_extra_note}
</div>

<div class="section-label">Recent Orders (last 6)</div>
<div class="card">{a1_orders_html}</div>

<div class="section-label">Today&apos;s Activity</div>
<div class="card">
  <div class="stat-grid" style="margin-bottom:12px">
    <div class="stat-box"><div class="stat-label">Sonnet Calls</div><div class="stat-val">{sonnet_calls}</div></div>
    <div class="stat-box"><div class="stat-label">Skips</div><div class="stat-val muted">{sonnet_skips}</div></div>
    <div class="stat-box"><div class="stat-label">Buys</div><div class="stat-val green">{buys_today}</div></div>
    <div class="stat-box"><div class="stat-label">Sells</div><div class="stat-val red">{sells_today}</div></div>
    <div class="stat-box"><div class="stat-label">Rejected</div><div class="stat-val orange">{len(rejected)}</div></div>
    <div class="stat-box"><div class="stat-label">Stop Trails</div><div class="stat-val">{len(trail_stops)}</div></div>
    <div class="stat-box"><div class="stat-label">Cost Today</div><div class="stat-val" style="font-size:15px;color:{proj_color}">{_fm(daily_cost)}</div></div>
    <div class="stat-box"><div class="stat-label">Proj/Month</div><div class="stat-val" style="font-size:15px;color:{proj_color}">{_fm(proj_monthly_a1)}</div></div>
  </div>
  <div class="kv"><span class="kv-label">Last Sonnet</span><span class="kv-val">{last_sonnet_ts}</span></div>
  <div class="kv"><span class="kv-label">Regime</span><span class="kv-val">{last_regime} (score {regime_score}) &middot; {dec_session}</span></div>
  <div class="kv"><span class="kv-label">VIX</span><span class="kv-val">{last_vix_pf}</span></div>
  <div class="kv"><span class="kv-label">Last Decision</span><span class="kv-val muted">{last_dec_ts}</span></div>
  {f'<div class="reasoning">{reasoning_2s}</div>' if reasoning_2s else ''}
</div>

<div class="compact-grid">
  <div>
    <div class="section-label">Decision Quality (7d)</div>
    {dec_quality_html}
  </div>
  <div>
    <div class="section-label">Allocator Shadow</div>
    <div class="card">
      <div class="kv"><span class="kv-label">Status</span><span class="kv-val">{alloc_st}</span></div>
      <div class="kv"><span class="kv-label">Last Run</span><span class="kv-val muted">{alloc_last}</span></div>
    </div>
    <div class="card" style="padding:10px 14px">{errors_html}</div>
  </div>
</div>
<div style="height:24px"></div>
</div>"""
    return _page_shell("A1 Equities", nav, body, ticker)


# ── A2 detail page ────────────────────────────────────────────────────────────
def _page_a2(status: dict, now_et: str) -> str:
    a2d = status["a2"]
    a2_acc = a2d.get("account")
    a1_mode = status["a1_mode"].get("mode", "unknown").upper()
    a2_mode = status["a2_mode"].get("mode", "unknown").upper()
    a2_color = _mode_color(a2_mode)
    nav = _nav_html("a2", now_et, a1_mode, a2_mode)
    warn_html = _warnings_html(status.get("warnings", []))
    ticker = _build_ticker_html(status["positions"])

    a2_pnl, a2_pnl_pct = status.get("today_pnl_a2", (0.0, 0.0))
    a2_pnl_color = "var(--accent-green)" if a2_pnl >= 0 else "var(--accent-red)"
    a2_pnl_sign = "+" if a2_pnl >= 0 else ""

    if a2_acc:
        a2_equity = float(a2_acc.equity or 0)
        a2_cash = float(a2_acc.cash or 0)
        a2_bp = float(a2_acc.buying_power or 0)
        a2_pos_count = len(a2d.get("positions", []))
    else:
        a2_equity = a2_cash = a2_bp = 0.0
        a2_pos_count = 0

    # A2 theses
    a2_theses = status.get("a2_theses", [])
    if a2_theses:
        a2_theses_html = ""
        for th in a2_theses:
            result_color = "#3fb950" if th["result"] == "submitted" else "#8b949e"
            conf_str = f"{float(th['confidence']):.0%}" if th["confidence"] not in ("?", None, "") else "?"
            reasons_html = "".join(
                f'<div style="font-size:12px;color:#8b949e;margin-top:3px">&middot; {r}</div>'
                for r in th["reasons"]
            )
            a2_theses_html += (
                f'<div class="thesis-card">'
                f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">'
                f'<b style="color:#58a6ff;font-size:15px">{th["symbol"]}</b>'
                f'<span style="font-size:11px;color:#8b949e">{th["strategy"].replace("_"," ").title()}</span>'
                f'<span style="margin-left:auto;font-size:12px;color:{result_color}">{th["result"]}</span>'
                f'<span style="font-size:11px;color:#8b949e">{_to_et(th["ts"])}</span>'
                f'</div>'
                f'<div style="font-size:12px;color:#d29922;margin-bottom:3px">Confidence: {conf_str}</div>'
                f'{reasons_html}'
                f'</div>'
            )
    else:
        a2_theses_html = '<div style="color:#8b949e;font-size:13px">No A2 thesis data yet.</div>'

    # A2 position cards
    a2_cards_html = ""
    for card in status.get("a2_pos_cards", []):
        prog_html = card.get("progress_html", "")
        a2_cards_html += (
            f'<div style="background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:12px 14px;margin-bottom:8px">'
            f'<div style="font-weight:700;font-size:14px;color:#58a6ff;margin-bottom:4px">{card["title"]}</div>'
            f'<div style="font-size:12px;color:#8b949e;margin-bottom:8px">'
            f'Strategy: {card["strategy_label"]} &nbsp;|&nbsp; IV: {card["iv_env"]} (rank {card["iv_rank_str"]})</div>'
            f'<div style="font-size:13px;margin-bottom:3px">&#x1F4C8; {card["profit_line"]}</div>'
            f'<div style="font-size:13px;margin-bottom:3px">Max gain: {card["max_gain_str"]} &nbsp;|&nbsp; Max loss: {card["max_loss_str"]}</div>'
            f'<div style="font-size:13px;margin-bottom:6px;color:{card["pnl_color"]}">Current P&amp;L: {card["pnl_str"]}</div>'
            f'{prog_html}'
            f'<div style="font-size:12px;color:#d29922;margin-bottom:6px">Auto-close: 80% of max gain or 50% of max loss</div>'
            f'<div style="font-size:12px;color:#8b949e;border-top:1px solid #21262d;padding-top:6px">{card["rationale"]}</div>'
            f'</div>'
        )
    if not a2_cards_html:
        a2_cards_html = '<div style="color:#8b949e;font-size:13px">No open options positions.</div>'

    # Last 5 A2 decisions
    a2_decs = status.get("a2_decisions", [])
    a2_decs_html = ""
    for d in a2_decs[:5]:
        ts = _to_et(d.get("built_at", ""))
        result = d.get("execution_result", "?")
        reason = d.get("no_trade_reason") or ""
        cand = d.get("selected_candidate") or {}
        cand_str = ""
        if isinstance(cand, dict) and cand:
            sym = cand.get("symbol", "")
            st = cand.get("structure_type", "")
            conf = d.get("debate_parsed", {}).get("confidence", "") if d.get("debate_parsed") else ""
            cand_str = f"{sym} {st}".strip()
            if conf:
                cand_str += f" (conf={conf})"
        result_color = "#3fb950" if result == "submitted" else "#8b949e"
        result_display = result + (f" — {reason}" if reason else "")
        cand_line = (f'<div style="font-size:12px;color:#8b949e">Candidate: {cand_str}</div>'
                     if cand_str else "")
        a2_decs_html += (
            f'<div style="padding:8px 0;border-bottom:1px solid #21262d">'
            f'<div style="font-size:12px"><span style="color:#8b949e">[{ts}]</span> '
            f'<span style="color:{result_color};font-weight:600">{result_display}</span></div>'
            f'{cand_line}</div>'
        )
    if not a2_decs_html:
        a2_decs_html = '<div style="color:#8b949e;font-size:13px">No A2 decisions yet.</div>'

    a2_orders_html = _fmt_orders_html(a2d.get("recent_orders", []), is_options=True, limit=6)

    # Strategy pipeline status from last A2 decision
    a2_dec = status.get("a2_decision") or {}
    a2_ts = _to_et(a2_dec.get("built_at", "")) if a2_dec else "—"
    a2_result = a2_dec.get("execution_result", "—") if a2_dec else "—"
    a2_reason = a2_dec.get("no_trade_reason", "") if a2_dec else ""
    a2_action_str = f"{a2_result} — {a2_reason}" if a2_reason else a2_result

    # Candidate set stats
    pipeline_html = ""
    cand_sets = a2_dec.get("candidate_sets", []) if a2_dec else []
    if cand_sets and isinstance(cand_sets, list):
        total_gen = sum(len(cs.get("generated_candidates", [])) for cs in cand_sets if isinstance(cs, dict))
        total_vet = sum(len(cs.get("vetoed_candidates", [])) for cs in cand_sets if isinstance(cs, dict))
        total_surv = sum(len(cs.get("surviving_candidates", [])) for cs in cand_sets if isinstance(cs, dict))
        symbols_seen = [cs.get("symbol", "?") for cs in cand_sets if isinstance(cs, dict)]
        pipeline_html = (
            f'<div class="kv"><span class="kv-label">Symbols Evaluated</span>'
            f'<span class="kv-val">{len(cand_sets)} ({", ".join(symbols_seen[:6])}{"…" if len(symbols_seen)>6 else ""})</span></div>'
            f'<div class="kv"><span class="kv-label">Candidates Generated</span><span class="kv-val">{total_gen}</span></div>'
            f'<div class="kv"><span class="kv-label">Vetoed</span><span class="kv-val orange">{total_vet}</span></div>'
            f'<div class="kv"><span class="kv-label">Surviving</span><span class="kv-val green">{total_surv}</span></div>'
        )
    debate = a2_dec.get("debate_parsed") or {} if a2_dec else {}
    debate_conf = debate.get("confidence", "?") if isinstance(debate, dict) else "?"
    debate_dir = debate.get("direction", "?") if isinstance(debate, dict) else "?"
    debate_synth = debate.get("synthesis", "?") if isinstance(debate, dict) else "?"

    # A2 strategy performance widget
    a2_perf_html = _perf_a2_strategies_html(status.get("perf_summary", {}))

    # IV environment summary from structures
    structs_all = _a2_structures()
    iv_summary_html = ""
    if structs_all:
        for s in structs_all[:5]:
            iv_r = s.get("iv_rank")
            iv_env = _iv_env_label(iv_r)
            sym = s.get("underlying", "?")
            iv_str = f"{iv_r:.1f}" if iv_r is not None else "?"
            iv_summary_html += (
                f'<div class="kv"><span class="kv-label">{sym}</span>'
                f'<span class="kv-val">IV rank {iv_str} — {iv_env}</span></div>'
            )
    if not iv_summary_html:
        iv_summary_html = '<div style="color:#8b949e;font-size:13px">No IV data from open structures.</div>'

    body = f"""
<div class="container">
{warn_html}
<div class="section-label">A2 Account Summary</div>
<div class="acct-bar">
  <div class="acct-bar-item"><div class="acct-bar-lbl">Equity</div><div class="acct-bar-val">{_fm(a2_equity)}</div></div>
  <div class="acct-bar-item"><div class="acct-bar-lbl">Cash</div><div class="acct-bar-val">{_fm(a2_cash)}</div></div>
  <div class="acct-bar-item"><div class="acct-bar-lbl">Buying Power</div><div class="acct-bar-val">{_fm(a2_bp)}</div></div>
  <div class="acct-bar-item"><div class="acct-bar-lbl">Structures</div><div class="acct-bar-val">{a2_pos_count}</div></div>
  <div class="acct-bar-item"><div class="acct-bar-lbl">Today P&amp;L</div><div class="acct-bar-val" style="color:{a2_pnl_color}">{a2_pnl_sign}{_fm(a2_pnl)} ({a2_pnl_sign}{a2_pnl_pct:.2f}%)</div></div>
  <div class="acct-bar-item"><div class="acct-bar-lbl">Mode</div><div class="acct-bar-val" style="color:{a2_color}">{a2_mode}</div></div>
</div>

<div class="section-label">Top A2 Theses</div>
<div class="card">{a2_theses_html}</div>

<div class="section-label">Open Structures ({a2_pos_count})</div>
<div class="card">{a2_cards_html}</div>

<div class="compact-grid">
  <div>
    <div class="section-label">Last 5 Decisions</div>
    <div class="card"><div class="dec-panel">{a2_decs_html}</div></div>
    <div class="section-label">Recent Orders (last 6)</div>
    <div class="card">{a2_orders_html}</div>
  </div>
  <div>
    <div class="section-label">Strategy Pipeline &mdash; Last Cycle</div>
    <div class="card">
      <div class="kv"><span class="kv-label">Last Cycle</span><span class="kv-val muted">{a2_ts}</span></div>
      <div class="kv"><span class="kv-label">Outcome</span><span class="kv-val">{a2_action_str}</span></div>
      <div class="kv"><span class="kv-label">Debate Direction</span><span class="kv-val">{debate_dir}</span></div>
      <div class="kv"><span class="kv-label">Debate Synthesis</span><span class="kv-val">{debate_synth}</span></div>
      <div class="kv"><span class="kv-label">Confidence</span><span class="kv-val">{debate_conf}</span></div>
      {pipeline_html}
    </div>
    <div class="section-label">Strategy Performance (7d)</div>
    {a2_perf_html}
    <div class="section-label">IV Environment</div>
    <div class="card">{iv_summary_html}</div>
  </div>
</div>
<div style="height:24px"></div>
</div>"""
    return _page_shell("A2 Options", nav, body, ticker)


def _page_brief(status: dict, now_et: str) -> str:
    a1_mode = status["a1_mode"].get("mode", "unknown").upper()
    a2_mode = status["a2_mode"].get("mode", "unknown").upper()
    nav = _nav_html("brief", now_et, a1_mode, a2_mode)
    ticker = _build_ticker_html(status["positions"])

    brief = status.get("intelligence_brief", {})
    if not brief:
        body = '<div style="padding:40px;color:var(--text-muted);text-align:center;font-size:14px">Intelligence brief not yet generated.<br>Runs at 4:00 AM ET (premarket) and 9:25 AM ET (market open).</div>'
        return _page_shell("Intelligence Brief", nav, body, ticker)

    gen_at = brief.get("generated_at", "")
    brief_type = brief.get("brief_type", "?")
    next_update = brief.get("next_update_at", "")

    # Staleness warning
    stale_html = ""
    if gen_at and _is_market_hours():
        try:
            gen_ts = datetime.fromisoformat(gen_at)
            if gen_ts.tzinfo is None:
                gen_ts = gen_ts.replace(tzinfo=timezone.utc)
            age_min = (datetime.now(timezone.utc) - gen_ts.astimezone(timezone.utc)).total_seconds() / 60
            if age_min > 90:
                stale_html = f'<div style="background:#2d1a00;border:1px solid #d29922;border-radius:6px;padding:10px 16px;margin-bottom:12px;color:#d29922">&#x26A0;&#xFE0F; Brief is stale ({age_min:.0f} min old). Next update: {next_update[:16] if next_update else "?"}.</div>'
        except Exception:
            pass

    gen_display = gen_at[:16].replace("T", " ") if gen_at else "?"
    next_display = next_update[:16].replace("T", " ") if next_update else "?"
    type_color = "#58a6ff" if brief_type == "market_open" else ("#3fb950" if brief_type == "intraday_update" else "#8b949e")

    header_html = f'''
    <div style="margin-bottom:16px">
      <div style="font-size:20px;font-weight:700;color:#e6edf3;margin-bottom:6px">&#x1F4CA; Intelligence Brief</div>
      <div style="font-size:12px;color:#8b949e">
        Generated: <b style="color:#e6edf3">{gen_display}</b> &nbsp;|&nbsp;
        Type: <b style="color:{type_color}">{brief_type}</b> &nbsp;|&nbsp;
        Next: <b style="color:#e6edf3">{next_display}</b>
      </div>
    </div>'''

    # Latest updates box
    updates = brief.get("latest_updates", [])[:5]
    updates_html = ""
    if updates:
        update_items = ""
        for u in updates:
            ts = u.get("timestamp", "")[:16].replace("T", " ")
            cat = u.get("category", "?")
            sym = u.get("symbol", "")
            summary = u.get("summary", "")
            cat_color = "#d29922" if "catalyst" in cat else ("#58a6ff" if "macro" in cat else "#8b949e")
            update_items += f'<div style="padding:6px 0;border-bottom:1px solid #2d2208;font-size:13px"><span style="color:#8b949e">{ts}</span> <span style="color:{cat_color}">[{cat}]</span> <b style="color:#58a6ff">{sym}</b> — {summary}</div>'
        updates_html = f'<div style="background:#2d2208;border:1px solid #d29922;border-radius:6px;padding:12px 16px;margin-bottom:16px"><div style="font-size:12px;font-weight:700;color:#d29922;margin-bottom:8px">&#x1F534; LATEST UPDATES</div>{update_items}</div>'

    # Market Regime
    mr = brief.get("market_regime", {})
    regime = mr.get("regime", "?")
    score = mr.get("score", 0)
    conf = mr.get("confidence", "?")
    vix = mr.get("vix", 0)
    tone = mr.get("tone", "")
    drivers = mr.get("key_drivers", [])
    events = mr.get("todays_events", [])
    regime_color = "#3fb950" if "risk_on" in regime else ("#f85149" if "risk_off" in regime or "defensive" in regime else "#d29922")
    drivers_html = "".join(f'<li style="margin:2px 0;color:#c9d1d9">{d}</li>' for d in drivers)
    events_html = ""
    for e in events[:4]:
        impact = e.get("impact", "low")
        ic = "#f85149" if impact == "high" else ("#d29922" if impact == "medium" else "#8b949e")
        events_html += f'<div style="padding:3px 0;font-size:12px"><span style="color:{ic}">&#x25CF;</span> <b>{e.get("time","?")}</b> — {e.get("event","?")} <span style="color:#8b949e">({impact})</span></div>'
    regime_html = f'''
    <div class="card" style="margin-bottom:16px">
      <div style="font-size:13px;font-weight:700;color:#8b949e;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:10px">Market Regime</div>
      <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:10px">
        <div><span style="font-size:22px;font-weight:700;color:{regime_color}">{regime.replace("_"," ").upper()}</span> <span style="color:#8b949e;font-size:14px">({score}/100, {conf})</span></div>
        <div style="font-size:14px;color:#e6edf3">VIX: <b>{vix:.1f}</b></div>
      </div>
      <div style="font-size:13px;color:#c9d1d9;margin-bottom:8px">{tone}</div>
      {'<ul style="margin:0;padding-left:20px;font-size:13px">' + drivers_html + '</ul>' if drivers else ''}
      {('<div style="margin-top:10px">' + events_html + '</div>') if events_html else ''}
    </div>'''

    # Sector Snapshot
    sectors = brief.get("sector_snapshot", [])
    sector_rows = ""
    for sec in sectors:
        chg = sec.get("etf_change_pct", 0) or 0
        status_val = sec.get("status", "NEUTRAL")
        sc = "#3fb950" if status_val in ("LEADING", "BULLISH") else ("#f85149" if status_val in ("BEARISH", "WEAK") else "#8b949e")
        news_items = sec.get("news", [])[:2]
        news_str = " · ".join(news_items) if news_items else ""
        sector_rows += f'''
        <tr>
          <td style="color:#e6edf3;font-weight:600">{sec.get("sector","?")}</td>
          <td style="color:#8b949e;text-align:center">{sec.get("etf","?")}</td>
          <td style="text-align:right;color:{"#3fb950" if chg>=0 else "#f85149"}">{chg:+.1f}%</td>
          <td style="text-align:center"><span style="background:#21262d;color:{sc};padding:2px 8px;border-radius:10px;font-size:11px;font-weight:700">{status_val}</span></td>
          <td style="color:#8b949e;font-size:12px">{sec.get("summary","")[:80]}</td>
          <td style="color:#8b949e;font-size:11px">{news_str[:80]}</td>
        </tr>'''
    sectors_html = f'''
    <div class="card" style="margin-bottom:16px">
      <div style="font-size:13px;font-weight:700;color:#8b949e;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:10px">Sector Snapshot</div>
      <div class="table-wrap"><table class="pos-table">
        <thead><tr><th>Sector</th><th style="text-align:center">ETF</th><th style="text-align:right">Change</th><th style="text-align:center">Status</th><th>Summary</th><th>News</th></tr></thead>
        <tbody>{sector_rows}</tbody>
      </table></div>
    </div>''' if sectors else ""

    # High Conviction Longs
    longs = brief.get("high_conviction_longs", [])
    long_cards = ""
    for p in longs:
        rank = p.get("rank", 0)
        sym = p.get("symbol", "?")
        score_v = p.get("score", 0)
        conv = p.get("conviction", "MEDIUM")
        cat = p.get("catalyst", "")[:100]
        entry = p.get("entry_zone", "?")
        stop = p.get("stop", 0)
        target = p.get("target", 0)
        rr = p.get("risk_reward", 0)
        tech = p.get("technical_summary", "")[:80]
        a2_note = p.get("a2_strategy_note", "")
        risk = p.get("risk_note", "")[:80]
        conv_c = "#3fb950" if conv == "HIGH" else "#d29922"
        rr_c = "#3fb950" if rr >= 2.0 else ("#d29922" if rr >= 1.5 else "#8b949e")
        long_cards += f'''
        <div style="border:1px solid #21262d;border-radius:6px;padding:10px 14px;margin-bottom:8px">
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
            <span style="color:#8b949e;font-size:12px">#{rank}</span>
            <b style="color:#3fb950;font-size:16px">{sym}</b>
            <span style="background:#0d2018;color:{conv_c};padding:1px 6px;border-radius:3px;font-size:11px;font-weight:700">{conv}</span>
            <span style="color:#8b949e;font-size:12px">score={score_v}</span>
            <span style="color:{rr_c};font-size:13px;font-weight:700;margin-left:auto">R/R {rr:.1f}x</span>
          </div>
          <div style="font-size:13px;color:#c9d1d9;margin-bottom:4px">{cat}</div>
          <div style="font-size:12px;color:#8b949e;margin-bottom:4px">entry={entry} &nbsp; stop={stop} &nbsp; target={target}</div>
          <div style="font-size:12px;color:#8b949e;margin-bottom:2px">{tech}</div>
          {f'<div style="font-size:11px;color:#58a6ff">{a2_note}</div>' if a2_note else ''}
          {f'<div style="font-size:11px;color:#f85149">Risk: {risk}</div>' if risk else ''}
        </div>'''
    longs_html = f'''
    <div class="card" style="margin-bottom:16px">
      <div style="font-size:13px;font-weight:700;color:#3fb950;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:10px">High Conviction Longs ({len(longs)})</div>
      {long_cards if long_cards else '<div style="color:#8b949e;font-size:13px">No high conviction longs.</div>'}
    </div>''' if longs is not None else ""

    # High Conviction Bearish
    bears = brief.get("high_conviction_bearish", [])
    bear_cards = ""
    for p in bears:
        rank = p.get("rank", 0)
        sym = p.get("symbol", "?")
        score_v = p.get("score", 0)
        conv = p.get("conviction", "MEDIUM")
        cat = p.get("catalyst", "")[:100]
        entry = p.get("entry_zone", "?")
        stop = p.get("stop", 0)
        target = p.get("target", 0)
        rr = p.get("risk_reward", 0)
        risk = p.get("risk_note", "")[:80]
        conv_c = "#f85149" if conv == "HIGH" else "#d29922"
        bear_cards += f'''
        <div style="border:1px solid #21262d;border-radius:6px;padding:10px 14px;margin-bottom:8px;border-left:3px solid #f85149">
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
            <span style="color:#8b949e;font-size:12px">#{rank}</span>
            <b style="color:#f85149;font-size:16px">{sym}</b>
            <span style="background:#2d0c0c;color:{conv_c};padding:1px 6px;border-radius:3px;font-size:11px;font-weight:700">{conv}</span>
            <span style="color:#8b949e;font-size:12px">score={score_v}</span>
            <span style="color:#8b949e;font-size:13px;margin-left:auto">R/R {rr:.1f}x</span>
          </div>
          <div style="font-size:13px;color:#c9d1d9;margin-bottom:4px">{cat}</div>
          <div style="font-size:12px;color:#8b949e;margin-bottom:4px">entry={entry} &nbsp; stop={stop} &nbsp; target={target}</div>
          {f'<div style="font-size:11px;color:#f85149">Risk: {risk}</div>' if risk else ''}
        </div>'''
    bears_html = f'''
    <div class="card" style="margin-bottom:16px">
      <div style="font-size:13px;font-weight:700;color:#f85149;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:10px">High Conviction Bearish ({len(bears)})</div>
      {bear_cards if bear_cards else '<div style="color:#8b949e;font-size:13px">No bearish signals.</div>'}
    </div>''' if bears is not None else ""

    # Earnings Pipeline
    earnings = brief.get("earnings_pipeline", [])
    earn_rows = ""
    for e in earnings:
        timing = e.get("timing", "?")
        tc = "#f85149" if "today" in timing else ("#d29922" if "tomorrow" in timing else "#8b949e")
        held = e.get("held_by_a1", False)
        held_badge = ' <span style="background:#0d2018;color:#3fb950;padding:1px 4px;border-radius:3px;font-size:10px">HELD</span>' if held else ""
        iv = e.get("iv_rank")
        iv_str = f"{iv:.0f}" if iv is not None else "—"
        earn_rows += f'''
        <tr>
          <td><b style="color:#58a6ff">{e.get("symbol","?")}</b>{held_badge}</td>
          <td><span style="color:{tc}">{timing}</span></td>
          <td style="text-align:right">{iv_str}</td>
          <td style="color:#8b949e;font-size:12px">{e.get("beat_history","")[:40]}</td>
          <td style="color:#8b949e;font-size:12px">{e.get("a2_rule","")[:40]}</td>
        </tr>'''
    earnings_html = f'''
    <div class="card" style="margin-bottom:16px">
      <div style="font-size:13px;font-weight:700;color:#8b949e;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:10px">Earnings Pipeline</div>
      <div class="table-wrap"><table class="pos-table">
        <thead><tr><th>Symbol</th><th>Timing</th><th style="text-align:right">IV Rank</th><th>Beat History</th><th>A2 Rule</th></tr></thead>
        <tbody>{earn_rows}</tbody>
      </table></div>
    </div>''' if earnings else ""

    # Macro Wire Alerts
    macro_alerts = brief.get("macro_wire_alerts", [])
    alert_cards = ""
    for a in macro_alerts:
        tier = a.get("tier", "medium")
        tc = "#f85149" if tier == "critical" else ("#d29922" if tier == "high" else "#8b949e")
        bg = "#2d0c0c" if tier == "critical" else ("#2d2208" if tier == "high" else "#161b22")
        sectors_affected = ", ".join(a.get("affected_sectors", [])[:3])
        alert_cards += f'''
        <div style="background:{bg};border:1px solid {tc};border-radius:6px;padding:8px 12px;margin-bottom:6px">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
            <span style="color:{tc};font-weight:700;font-size:12px;text-transform:uppercase">{tier}</span>
            <span style="color:#8b949e;font-size:11px">score={a.get("score",0):.1f}</span>
            {f'<span style="color:#8b949e;font-size:11px">{sectors_affected}</span>' if sectors_affected else ''}
          </div>
          <div style="font-size:13px;color:#e6edf3">{a.get("headline","")[:120]}</div>
          <div style="font-size:12px;color:#8b949e;margin-top:3px">{a.get("impact","")[:80]}</div>
        </div>'''
    macro_html = f'''
    <div class="card" style="margin-bottom:16px">
      <div style="font-size:13px;font-weight:700;color:#8b949e;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:10px">Macro Wire Alerts</div>
      {alert_cards if alert_cards else '<div style="color:#8b949e;font-size:13px">No significant macro alerts.</div>'}
    </div>''' if macro_alerts is not None else ""

    # Avoid List
    avoid = brief.get("avoid_list", [])
    avoid_cards = ""
    for a in avoid:
        avoid_cards += f'''
        <div style="display:inline-block;margin:4px;padding:6px 12px;background:#2d0c0c;border:1px solid #f85149;border-radius:6px;font-size:13px">
          <b style="color:#f85149">{a.get("symbol","?")}</b> <span style="color:#8b949e;font-size:11px">— {a.get("reason","")[:60]}</span>
        </div>'''
    avoid_html = f'''
    <div class="card" style="margin-bottom:16px">
      <div style="font-size:13px;font-weight:700;color:#f85149;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:10px">Avoid List</div>
      {avoid_cards if avoid_cards else '<div style="color:#8b949e;font-size:13px">No symbols flagged to avoid.</div>'}
    </div>''' if avoid is not None else ""

    # Insider Activity
    insider = brief.get("insider_activity", {})
    hc = insider.get("high_conviction", [])
    cong = insider.get("congressional", [])
    f4 = insider.get("form4_purchases", [])
    insider_items = ""
    for item in (hc + cong + f4)[:8]:
        insider_items += f'<div style="font-size:13px;color:#c9d1d9;padding:3px 0">{item}</div>'
    insider_html = f'''
    <div class="card" style="margin-bottom:16px">
      <div style="font-size:13px;font-weight:700;color:#8b949e;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:10px">Insider Activity</div>
      {insider_items if insider_items else '<div style="color:#8b949e;font-size:13px">No insider activity.</div>'}
    </div>''' if (hc or cong or f4) else ""

    # Watch List
    watch = brief.get("watch_list", [])[:10]
    watch_rows = ""
    for w in watch:
        dir_color = "#3fb950" if w.get("direction", "").lower() == "bullish" else ("#f85149" if w.get("direction", "").lower() == "bearish" else "#8b949e")
        watch_rows += f'''
        <tr>
          <td><b style="color:#58a6ff">{w.get("symbol","?")}</b></td>
          <td style="text-align:right">{w.get("score",0)}</td>
          <td><span style="color:{dir_color}">{w.get("direction","?")}</span></td>
          <td style="color:#8b949e;font-size:12px">{w.get("entry_trigger","")[:60]}</td>
        </tr>'''
    watch_html = f'''
    <div class="card" style="margin-bottom:16px">
      <div style="font-size:13px;font-weight:700;color:#8b949e;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:10px">Watch List</div>
      <div class="table-wrap"><table class="pos-table">
        <thead><tr><th>Symbol</th><th style="text-align:right">Score</th><th>Direction</th><th>Entry Trigger</th></tr></thead>
        <tbody>{watch_rows}</tbody>
      </table></div>
    </div>''' if watch else ""

    body = (
        '<div class="container">'
        + header_html + stale_html + updates_html
        + '<div class="compact-grid" style="gap:12px">'
        + '<div>' + regime_html + sectors_html + earnings_html + insider_html + macro_html + '</div>'
        + '<div>' + longs_html + bears_html + watch_html + avoid_html + '</div>'
        + '</div></div>'
    )
    return _page_shell("Intelligence Brief", nav, body, ticker)


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
        denom = buying_power if buying_power else equity
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
            if pct_bp > 25:
                oversize = "critical"
            elif pct_bp > 20:
                oversize = "core"
            elif pct_bp > 15:
                oversize = "dynamic"
            else:
                oversize = False
            positions.append({
                "symbol": sym, "qty": qty, "entry": entry, "current": current,
                "market_val": market_val, "unreal_pl": unreal_pl,
                "unreal_plpc": unreal_plpc, "pct_of_bp": pct_bp,
                "stop": stop, "gap_to_stop": gap,
                "earnings": earnings.get(sym, ""),
                "oversize": oversize,
            })

    a2_dec = _a2_last_cycle()
    a2_structs = _a2_structures()
    a2_live_pos = a2d.get("positions", [])
    a1_decs = _last_n_a1_decisions(5)
    a2_decs = _last_n_a2_decisions(5)
    qctx = _qualitative_context()
    today_pnl_a1 = _today_pnl_a1()
    today_pnl_a2 = _today_pnl_a2()

    # Load trail tiers from strategy_config
    trail_tiers = []
    try:
        cfg = json.loads((BOT_DIR / "strategy_config.json").read_text())
        trail_tiers = cfg.get("exit_management", {}).get("trail_tiers", [])
    except Exception:
        pass

    st = {
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
        "morning_brief": _morning_brief(),
        "morning_brief_time": _morning_brief_time(),
        "morning_brief_mtime": _morning_brief_mtime_float(),
        "intelligence_brief": _intelligence_brief_full(),
        "a1_decisions": a1_decs,
        "a2_decisions": a2_decs,
        "a2_pos_cards": _build_a2_position_cards(a2_structs, a2_live_pos),
        "a1_theses": _a1_top_theses(a1_decs, qctx),
        "a2_theses": _a2_top_theses(a2_decs),
        "today_pnl_a1": today_pnl_a1,
        "today_pnl_a2": today_pnl_a2,
        "trail_tiers": trail_tiers,
        "a2_pipeline": _a2_pipeline_today(),
        "allocator_line": _allocator_shadow_compact(),
        "perf_summary": _load_perf_summary(),
    }
    st["warnings"] = _build_warnings(st)
    return st


def _load_perf_summary() -> dict:
    """Load performance_summary.json. Returns {} on any error (non-fatal)."""
    try:
        import sys  # noqa: PLC0415
        sys.path.insert(0, str(BOT_DIR))
        from performance_tracker import load_performance_summary  # noqa: PLC0415
        return load_performance_summary()
    except Exception:
        return {}


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    status = _build_status()
    return _page_overview(status, _now_et())


@app.route("/a1")
def page_a1():
    status = _build_status()
    return _page_a1(status, _now_et())


@app.route("/a2")
def page_a2():
    status = _build_status()
    return _page_a2(status, _now_et())


@app.route("/brief")
def page_brief():
    status = _build_status()
    return _page_brief(status, _now_et())


@app.route("/api/status")
def api_status():
    status = _build_status()
    return jsonify({
        "a1_mode": status["a1_mode"],
        "a2_mode": status["a2_mode"],
        "gate": status["gate"],
        "costs": status["costs"],
        "decision": status["decision"],
        "git_hash": status["git_hash"],
        "service_uptime": status["service_uptime"],
        "positions_count": len(status["positions"]),
        "today_pnl_a1": status["today_pnl_a1"][0],
        "today_pnl_a2": status["today_pnl_a2"][0],
        "a1_error": status["a1"].get("error"),
        "a2_error": status["a2"].get("error"),
        "warnings": status.get("warnings", []),
    })


@app.route("/health")
def health():
    return "ok", 200


# ── Trade journal (cached 5 min — Alpaca API call) ───────────────────────────
@_cached("trades", ttl=300)
def _closed_trades() -> list[dict]:
    try:
        sys.path.insert(0, str(BOT_DIR))
        from trade_journal import build_bug_fix_log, build_closed_trades  # noqa: I001
        return build_closed_trades(), build_bug_fix_log()
    except Exception as e:
        app.logger.warning("trade_journal error: %s", e)
        return [], []


def _page_trades(now_et: str) -> str:
    a1 = _alpaca_a1()
    _a1_positions = [
        {"symbol": getattr(p, "symbol", ""), "current": float(getattr(p, "current_price", 0) or 0),
         "unreal_plpc": float(getattr(p, "unrealized_plpc", 0) or 0) * 100}
        for p in a1.get("positions", [])
    ]
    nav = _nav_html("trades", now_et)
    ticker = _build_ticker_html(_a1_positions)

    result = _closed_trades()
    trades, bug_log = result if isinstance(result, tuple) else (result, [])

    # ── summary stats ─────────────────────────────────────────────────────────
    n = len(trades)
    wins = sum(1 for t in trades if t.get("outcome") == "win")
    losses = sum(1 for t in trades if t.get("outcome") == "loss")
    total_pnl = sum(t.get("pnl", 0.0) or 0.0 for t in trades)
    win_rate = (wins / n * 100) if n else 0.0
    pnl_color = "#3fb950" if total_pnl >= 0 else "#f85149"
    pnl_sign = "+" if total_pnl >= 0 else ""
    wr_color = "#3fb950" if win_rate >= 55 else ("#d29922" if win_rate >= 45 else "#f85149")

    summary_html = (
        f'<div class="stat-grid" style="margin-bottom:12px">'
        f'<div class="stat-box"><div class="stat-label">Closed Trades</div>'
        f'<div class="stat-val">{n}</div></div>'
        f'<div class="stat-box"><div class="stat-label">Total P&amp;L</div>'
        f'<div class="stat-val" style="color:{pnl_color}">{pnl_sign}${total_pnl:,.2f}</div></div>'
        f'<div class="stat-box"><div class="stat-label">Win Rate</div>'
        f'<div class="stat-val" style="color:{wr_color}">{win_rate:.0f}%</div></div>'
        f'<div class="stat-box"><div class="stat-label">W / L</div>'
        f'<div class="stat-val">{wins} / {losses}</div></div>'
        f'</div>'
    )

    # ── trades table ──────────────────────────────────────────────────────────
    if not trades:
        trades_html = '<div class="card"><p class="muted" style="font-size:13px">No closed trades yet.</p></div>'
    else:
        rows = ""
        for t in trades:
            sym = t.get("symbol", "")
            pnl = t.get("pnl", 0.0) or 0.0
            pnl_pct = t.get("pnl_pct", 0.0) or 0.0
            outcome = t.get("outcome", "flat")
            clr = "#3fb950" if outcome == "win" else ("#f85149" if outcome == "loss" else "#8b949e")
            sign = "+" if pnl >= 0 else ""
            entry = t.get("entry_price", 0)
            exit_ = t.get("exit_price", 0)
            qty = int(t.get("qty", 0))
            holding = t.get("holding_days")
            hold_str = f"{holding}d" if holding is not None else "—"
            exit_t = t.get("exit_time", "")
            date_str = exit_t[:10] if exit_t else "—"
            flags = t.get("bug_flags", [])
            flag_html = "".join(
                f'<span class="flag flag-warn" title="Known bug may have affected this trade">{f}</span>'
                for f in flags
            )
            catalyst = t.get("catalyst") or ""
            cat_short = (catalyst[:60] + "…") if len(catalyst) > 60 else catalyst
            rows += (
                f'<tr>'
                f'<td style="text-align:left;font-weight:700">{sym}{flag_html}</td>'
                f'<td>{date_str}</td>'
                f'<td>${entry:,.2f} → ${exit_:,.2f}</td>'
                f'<td>{qty}</td>'
                f'<td>{hold_str}</td>'
                f'<td style="color:{clr};font-weight:700">{sign}${pnl:,.2f} ({sign}{pnl_pct:.1f}%)</td>'
                f'<td style="color:#8b949e;font-size:12px;white-space:normal;max-width:280px">{cat_short}</td>'
                f'</tr>'
            )
        trades_html = (
            '<div class="table-wrap">'
            '<table class="pos-table">'
            '<thead><tr>'
            '<th style="text-align:left">Symbol</th>'
            '<th>Exit Date</th>'
            '<th>Entry → Exit</th>'
            '<th>Qty</th>'
            '<th>Hold</th>'
            '<th>P&amp;L</th>'
            '<th style="text-align:left;white-space:normal">Catalyst</th>'
            '</tr></thead>'
            f'<tbody>{rows}</tbody>'
            '</table></div>'
        )

    # ── trade detail cards (full context + bug annotations) ───────────────────
    # Build bug lookup for quick access
    _bug_by_id = {b["id"]: b for b in bug_log}

    detail_html = ""
    for t in trades[:5]:
        sym = t.get("symbol", "")
        reasoning = t.get("reasoning") or ""
        pnl = t.get("pnl", 0.0) or 0.0
        clr = "#3fb950" if pnl >= 0 else "#f85149"
        sign = "+" if pnl >= 0 else ""
        flags = t.get("bug_flags", [])
        has_bug = bool(flags)
        title_suffix = " · BUG EXIT" if has_bug else ""
        title_color = "#d29922" if has_bug else clr

        # Trade meta details
        entry_p = t.get("entry_price", 0)
        exit_p = t.get("exit_price", 0)
        qty = int(t.get("qty", 0))
        conviction = (t.get("conviction") or "").upper() or "?"
        tier = (t.get("tier") or "").upper() or "?"
        regime_score = t.get("regime_score", "")
        score_str = f" (score {regime_score})" if regime_score not in ("", None) else ""
        # Compute hold duration
        entry_ts = t.get("entry_time", "")
        exit_ts = t.get("exit_time", "")
        hold_label = ""
        if entry_ts and exit_ts:
            try:
                _et = datetime.fromisoformat(entry_ts.replace("Z", "+00:00"))
                _xt = datetime.fromisoformat(exit_ts.replace("Z", "+00:00"))
                _mins = int((_xt - _et).total_seconds() / 60)
                hold_label = f"{_mins}m hold" if _mins < 1440 else f"{_mins // 1440}d hold"
            except Exception:
                hold_label = ""

        # Trade context block
        meta_parts = []
        if conviction and conviction != "?":
            meta_parts.append(f'Conviction: <b style="color:#d29922">{conviction}</b>{score_str} &middot; Tier: <b>{tier}</b>')
        if entry_p and exit_p:
            meta_parts.append(f'Entry: <b>${entry_p:,.2f}</b> &times; {qty}sh &rarr; Exit: <b>${exit_p:,.2f}</b>{(" (" + hold_label + ")") if hold_label else ""}')
        meta_html = "".join(
            f'<div style="font-size:12px;color:var(--text-muted);padding:2px 0">{m}</div>'
            for m in meta_parts
        )

        # Bug annotation blocks (additive, below thesis)
        bug_annotations = ""
        for bug_id in flags:
            bug = _bug_by_id.get(bug_id, {})
            if not bug:
                bug_annotations += f'<div style="margin-top:8px;padding:8px 10px;background:rgba(255,170,32,.08);border:1px solid rgba(255,170,32,.3);border-radius:6px;font-size:12px;color:#d29922">&#x26A0; {bug_id}</div>'
                continue
            sev = bug.get("severity", "")
            sev_c = "#f85149" if sev == "HIGH" else "#d29922"
            resolution = bug.get("resolution", "")
            desc = bug.get("description", "")
            res_short = (resolution[:120] + "…") if len(resolution) > 120 else resolution
            res_div = (f'<div style="font-size:11px;color:var(--text-secondary)">Fixed: {res_short}</div>'
                       if resolution else "")
            desc_short = (desc[:160] + "…") if len(desc) > 160 else desc
            bug_annotations += (
                f'<div style="margin-top:8px;padding:8px 10px;background:rgba(255,170,32,.08);'
                f'border:1px solid rgba(255,170,32,.3);border-radius:6px">'
                f'<div style="font-size:11px;font-weight:700;color:{sev_c};margin-bottom:3px">'
                f'&#x26A0; {bug_id}: {bug.get("title","")}</div>'
                f'<div style="font-size:11px;color:var(--text-muted);margin-bottom:3px">{desc_short}</div>'
                f'{res_div}'
                f'</div>'
            )

        thesis_html = (
            f'<div class="reasoning" style="margin:6px 0 4px">{reasoning[:400]}{"…" if len(reasoning) > 400 else ""}</div>'
            if reasoning else ""
        )

        detail_html += (
            f'<div class="thesis-card">'
            f'<div style="font-size:14px;font-weight:700;margin-bottom:4px;color:{title_color}">'
            f'{sym} <span style="color:{clr}">{sign}${pnl:,.2f}</span>{title_suffix}</div>'
            f'{thesis_html}'
            f'{meta_html}'
            f'{bug_annotations}'
            f'</div>'
        )

    # ── known issues section ──────────────────────────────────────────────────
    bugs_html = ""
    for bug in bug_log:
        sev = bug.get("severity", "LOW")
        sev_color = "#f85149" if sev == "HIGH" else ("#d29922" if sev == "MEDIUM" else "#8b949e")
        bugs_html += (
            f'<div class="card" style="margin-bottom:8px">'
            f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">'
            f'<span style="font-weight:700;font-size:13px">{bug["id"]}</span>'
            f'<span style="font-size:11px;font-weight:700;color:{sev_color};text-transform:uppercase">{sev}</span>'
            f'<span style="font-size:12px;color:#8b949e">{bug["start"]} – {bug["end"]}</span>'
            f'</div>'
            f'<div style="font-size:13px;font-weight:600;margin-bottom:4px">{bug["title"]}</div>'
            f'<div style="font-size:12px;color:#8b949e">{bug["description"][:200]}{"…" if len(bug["description"]) > 200 else ""}</div>'
            f'</div>'
        )

    body = (
        '<div class="container">'
        + summary_html
        + '<div class="section-label">Closed Round-Trips</div>'
        + '<div class="card" style="padding:0"><div style="padding:14px 16px 0">'
        + trades_html
        + '</div></div>'
        + ('<div class="section-label">Recent Reasoning</div>' + detail_html if detail_html else "")
        + '<div class="section-label">Known Issue Log</div>'
        + (bugs_html or '<div class="card"><p class="muted" style="font-size:13px">No logged bugs.</p></div>')
        + '</div>'
    )
    return _page_shell("Trade Journal", nav, body, ticker)


@app.route("/trades")
def page_trades():
    return _page_trades(_now_et())


@app.route("/api/trades")
def api_trades():
    result = _closed_trades()
    trades, bug_log = result if isinstance(result, tuple) else (result, [])
    wins = sum(1 for t in trades if t.get("outcome") == "win")
    losses = sum(1 for t in trades if t.get("outcome") == "loss")
    total_pnl = sum(t.get("pnl", 0.0) or 0.0 for t in trades)
    return jsonify({
        "trades": trades,
        "summary": {
            "total": len(trades),
            "wins": wins,
            "losses": losses,
            "total_pnl": round(total_pnl, 2),
            "win_rate": round(wins / len(trades) * 100, 1) if trades else 0.0,
        },
        "bug_log": bug_log,
    })


def _page_transparency(now_et: str) -> str:
    a1 = _alpaca_a1()
    _a1_positions = [
        {"symbol": getattr(p, "symbol", ""), "current": float(getattr(p, "current_price", 0) or 0),
         "unreal_plpc": float(getattr(p, "unrealized_plpc", 0) or 0) * 100}
        for p in a1.get("positions", [])
    ]
    nav = _nav_html("transparency", now_et)
    ticker = _build_ticker_html(_a1_positions)

    # ── strategy config ────────────────────────────────────────────────────────
    try:
        import json as _json
        _cfg = _json.loads(Path("strategy_config.json").read_text())
    except Exception:
        _cfg = {}

    params = _cfg.get("parameters", {})
    director_notes = _cfg.get("director_notes", "No director notes available.")
    active_strategy = _cfg.get("active_strategy", "hybrid")
    ff = _cfg.get("feature_flags", {})
    shadow_flags = _cfg.get("shadow_flags", {})

    def _flag_row(name: str, val: object) -> str:
        enabled = bool(val)
        badge = '<span class="badge-g">ON</span>' if enabled else '<span class="badge-r">OFF</span>'
        return f'<tr><td style="font-family:monospace;font-size:11px">{name}</td><td>{badge}</td></tr>'

    flags_html = "".join(_flag_row(k, v) for k, v in {**ff, **shadow_flags}.items()) or (
        '<tr><td colspan="2" class="muted">No flags configured.</td></tr>'
    )

    def _param_row(k: str, v: object) -> str:
        return (
            f'<tr><td style="font-family:monospace;font-size:11px;color:var(--text-secondary)">{k}</td>'
            f'<td style="text-align:right;font-family:monospace;font-size:11px">{v}</td></tr>'
        )

    params_html = "".join(_param_row(k, v) for k, v in params.items()) or (
        '<tr><td colspan="2" class="muted">No parameters.</td></tr>'
    )

    # ── cost data ──────────────────────────────────────────────────────────────
    try:
        costs = _json.loads(Path("data/costs/daily_costs.json").read_text())
        daily_cost = costs.get("daily_cost", 0.0)
        daily_calls = costs.get("daily_calls", 0)
        by_caller = costs.get("by_caller", {})
    except Exception:
        daily_cost = 0.0
        daily_calls = 0
        by_caller = {}

    caller_rows = ""
    for caller, info in sorted(by_caller.items(), key=lambda x: -x[1].get("cost", 0)):
        c = info.get("cost", 0.0)
        n = info.get("calls", 0)
        pct = (c / daily_cost * 100) if daily_cost else 0
        bar_w = f"{pct:.0f}%"
        caller_rows += (
            f'<tr><td style="font-size:11px;font-family:monospace">{caller}</td>'
            f'<td style="text-align:right;font-size:11px">{n}</td>'
            f'<td style="text-align:right;font-size:11px">${c:.3f}</td>'
            f'<td style="width:80px"><div class="range-track" style="margin-top:3px">'
            f'<div style="height:5px;width:{bar_w};background:var(--accent-blue);border-radius:3px"></div>'
            f'</div></td></tr>'
        )
    if not caller_rows:
        caller_rows = '<tr><td colspan="4" class="muted">No cost data.</td></tr>'

    # ── time-bound actions ─────────────────────────────────────────────────────
    tba = _cfg.get("time_bound_actions", [])
    if tba:
        tba_rows = ""
        for action in tba:
            sym = action.get("symbol", "")
            deadline = action.get("deadline", "")
            reason = action.get("reason", "")
            tba_rows += (
                f'<tr><td style="font-family:monospace;font-size:11px">{sym}</td>'
                f'<td style="font-size:11px">{deadline}</td>'
                f'<td style="font-size:11px;color:var(--text-secondary)">{reason}</td></tr>'
            )
        tba_html = (
            '<div class="section-label">Time-Bound Actions</div>'
            '<div class="card" style="padding:0"><table class="data-table"><thead><tr>'
            '<th>Symbol</th><th>Deadline</th><th>Reason</th>'
            f'</tr></thead><tbody>{tba_rows}</tbody></table></div>'
        )
    else:
        tba_html = ""

    # ── left column: public context, architecture, bug log, learnings ─────────
    try:
        from trade_journal import build_bug_fix_log as _bfl  # noqa: I001
        _bugs = _bfl()
    except Exception:
        _bugs = []

    # Architecture overview cards
    arch_rows = (
        f'<div class="kv"><span class="kv-label">Strategy</span><span class="kv-val" style="text-transform:uppercase">{active_strategy}</span></div>'
        f'<div class="kv"><span class="kv-label">Accounts</span><span class="kv-val">A1 Equities/ETF/Crypto &middot; A2 Options</span></div>'
        f'<div class="kv"><span class="kv-label">Pipeline</span><span class="kv-val" style="font-size:11px">Regime&rarr;Signals&rarr;Scratchpad&rarr;Gate&rarr;Sonnet&rarr;Kernel&rarr;Exec</span></div>'
        f'<div class="kv"><span class="kv-label">Models</span><span class="kv-val">Haiku (stages 1-2) &middot; Sonnet (stage 3)</span></div>'
        f'<div class="kv"><span class="kv-label">Cycle</span><span class="kv-val">5 min market / 15 min extended / 30 min overnight</span></div>'
        f'<div class="kv"><span class="kv-label">Memory</span><span class="kv-val">ChromaDB 3-tier vector store + hot scratchpads</span></div>'
        f'<div class="kv"><span class="kv-label">Paper trading</span><span class="kv-val" style="color:var(--accent-green)">Alpaca Paper &mdash; launch 2026-04-13</span></div>'
    )

    # Bug fix log
    bug_left_html = ""
    for b in _bugs:
        sev = b.get("severity", "LOW")
        sev_c = "#f85149" if sev == "HIGH" else ("#d29922" if sev == "MEDIUM" else "#8b949e")
        full_res = b.get("resolution", "")
        res = (full_res[:120] + "…") if len(full_res) > 120 else full_res
        res_div = f'<div style="font-size:11px;color:var(--text-muted)">&#x2714; {res}</div>' if res else ""
        bug_left_html += (
            f'<div style="padding:8px 0;border-bottom:1px solid var(--border-subtle)">'
            f'<div style="display:flex;gap:6px;align-items:baseline;margin-bottom:3px">'
            f'<span style="font-size:11px;font-weight:700;font-family:monospace;color:var(--accent-blue)">{b["id"]}</span>'
            f'<span style="font-size:10px;font-weight:700;color:{sev_c}">{sev}</span>'
            f'<span style="font-size:10px;color:var(--text-muted)">{b["start"]} – {b["end"]}</span>'
            f'</div>'
            f'<div style="font-size:12px;color:var(--text-primary);margin-bottom:2px">{b["title"]}</div>'
            f'{res_div}'
            f'</div>'
        )
    if not bug_left_html:
        bug_left_html = '<div style="color:var(--text-muted);font-size:12px">No bugs logged.</div>'

    # Director notes (learning journal)
    director_html = (
        f'<p style="font-size:12px;color:var(--text-secondary);line-height:1.7;margin:0">'
        f'{director_notes}</p>'
    )

    left_col = (
        '<div class="section-label">Architecture</div>'
        f'<div class="card">{arch_rows}</div>'
        '<div class="section-label">Bug Fix Log</div>'
        f'<div class="card" style="padding:10px 14px">{bug_left_html}</div>'
        '<div class="section-label">Strategy Director Notes</div>'
        f'<div class="card">{director_html}</div>'
    )

    right_col = (
        # ── strategy overview ──────────────────────────────────────────────────
        '<div class="section-label">Risk Parameters</div>'
        '<div class="card" style="padding:0">'
        '<table class="data-table"><thead><tr><th>Parameter</th><th style="text-align:right">Value</th></tr></thead>'
        f'<tbody>{params_html}</tbody></table></div>'
        + tba_html
        # ── feature flags ──────────────────────────────────────────────────────
        + '<div class="section-label">Feature Flags</div>'
        '<div class="card" style="padding:0">'
        '<table class="data-table"><thead><tr><th>Flag</th><th>State</th></tr></thead>'
        f'<tbody>{flags_html}</tbody></table></div>'
        # ── claude cost breakdown ──────────────────────────────────────────────
        + '<div class="section-label">Claude Cost — Today</div>'
        f'<div class="card">'
        f'<div class="stat-grid" style="margin-bottom:12px">'
        f'<div class="stat-box"><div class="stat-label">Daily Spend</div>'
        f'<div class="stat-val" style="color:var(--accent-amber)">${daily_cost:.2f}</div></div>'
        f'<div class="stat-box"><div class="stat-label">API Calls</div>'
        f'<div class="stat-val">{daily_calls}</div></div>'
        f'<div class="stat-box"><div class="stat-label">Monthly Est.</div>'
        f'<div class="stat-val" style="color:var(--text-secondary)">${daily_cost*30:.0f}</div></div>'
        f'</div>'
        '<table class="data-table" style="margin-top:8px"><thead><tr>'
        '<th>Caller</th><th style="text-align:right">Calls</th><th style="text-align:right">Cost</th><th>Share</th>'
        f'</tr></thead><tbody>{caller_rows}</tbody></table>'
        f'</div>'
    )

    body = (
        '<div class="container">'
        f'<div class="compact-grid">'
        f'<div>{left_col}</div>'
        f'<div>{right_col}</div>'
        f'</div>'
        '</div>'
    )
    return _page_shell("Transparency", nav, body, ticker)


@app.route("/transparency")
def page_transparency():
    return _page_transparency(_now_et())


def _page_theater(now_et: str) -> str:
    nav = _nav_html("theater", now_et)
    # Initial data loaded server-side; JS fetches updates
    try:
        from decision_theater import get_all_trades_summary, get_cycle_view  # noqa: I001
        cycle = get_cycle_view(-1)
        trades_sum = get_all_trades_summary()
    except Exception:
        cycle = {"cycle_number": 0, "total_cycles": 0, "timestamp": "", "session": "unknown",
                 "decision_id": "", "stages": {}}
        trades_sum = {"trades": [], "open_count": 0, "closed_count": 0, "total": 0}

    # ── Trade pills ────────────────────────────────────────────────────────────
    pills_html = ""
    for t in trades_sum["trades"][:20]:
        sym = t["symbol"]
        status = t["status"]
        pnl_pct = t["pnl_pct"]
        sign = "+" if pnl_pct >= 0 else ""
        entry_date = t.get("entry_date", "")
        pc = t.get("pill_class", "tp-flat")
        label = f"{sym} · {status} · {sign}{pnl_pct:.1f}%"
        pills_html += (
            f'<button class="trade-pill {pc}" '
            f'onclick="loadTrade(\'{sym}\',\'{entry_date}\')">{label}</button>\n'
        )
    if not pills_html:
        pills_html = '<span class="muted" style="font-size:11px">No trades yet.</span>'

    # ── Cycle stage grid ───────────────────────────────────────────────────────
    stage_defs = [
        ("regime",    "🌡",  "Regime",     "#1e3a5a"),
        ("signals",   "📡",  "Signals",    "#3a1e5a"),
        ("scratchpad","📋",  "Scratchpad", "#5a3a1e"),
        ("gate",      "🚦",  "Gate",       "#4a4a1e"),
        ("sonnet",    "🧠",  "Sonnet",     "#1e3a5a"),
        ("kernel",    "⚙",  "Kernel",     "#5a1e1e"),
        ("execution", "⚡",  "Execution",  "#1a4020"),
        ("a2",        "📊",  "A2 Opts",   "#3a1e5a"),
    ]
    stages = cycle.get("stages", {})
    nodes_html = ""
    for stage_id, icon, name, bg in stage_defs:
        st = stages.get(stage_id, {})
        status = st.get("status", "warn")
        dot_c = "var(--accent-green)" if status == "ok" else (
            "var(--text-muted)" if status in ("skip", "skipped") else
            "var(--accent-amber)" if status == "warn" else "var(--accent-red)"
        )
        metric = _theater_stage_metric(stage_id, st)
        nodes_html += (
            f'<div class="stage-node" id="stage-{stage_id}" '
            f'onclick="selectStage(\'{stage_id}\')" '
            f'style="background:{bg};border-color:{dot_c};">'
            f'<span style="font-size:16px">{icon}</span>'
            f'<div style="font-size:8px;color:var(--text-muted);text-transform:uppercase;letter-spacing:.8px;margin-top:2px">{name}</div>'
            f'<div style="font-size:10px;color:var(--text-secondary);margin-top:1px">{metric}</div>'
            f'<div class="stage-dot" style="background:{dot_c}"></div>'
            f'</div>'
        )

    # ── Initial detail panel from most recent cycle ───────────────────────────
    cycle_ts = cycle.get("timestamp", "")[:19].replace("T", " ")
    cycle_num = cycle.get("cycle_number", 0)
    total = cycle.get("total_cycles", 0)
    session = cycle.get("session", "")

    # Serialize cycle data for JS
    cycle_json = json.dumps(cycle, default=str)
    trades_json = json.dumps(trades_sum, default=str)

    import base64 as _b64
    _auth_b64 = _b64.b64encode(f"{DASHBOARD_USER}:{DASHBOARD_PASSWORD}".encode()).decode()

    body = f"""
<div id="auth-b64" data-val="{_auth_b64}" style="display:none"></div>
<div class="container" style="padding-bottom:60px">

  <!-- Mode toggle -->
  <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px">
    <button class="mode-btn active" id="btn-cycle" onclick="setMode('cycle')">Cycle View</button>
    <button class="mode-btn" id="btn-trade" onclick="setMode('trade')">Trade Lifecycle</button>
    <div style="margin-left:auto;font-size:10px;color:var(--text-muted)">{cycle_ts} · {session} · cycle {cycle_num+1}/{total}</div>
  </div>

  <!-- ── CYCLE VIEW ── -->
  <div id="panel-cycle">

    <!-- Pipeline flow -->
    <div class="section-label">Pipeline — Cycle #{cycle_num+1}</div>
    <div class="card" style="padding:12px">
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        {nodes_html}
      </div>
      <!-- scrubber -->
      <div style="margin-top:10px;display:flex;align-items:center;gap:8px">
        <span style="font-size:10px;color:var(--text-muted)">← Cycle</span>
        <input type="range" id="cycle-scrubber" min="0" max="{total-1}"
               value="{cycle_num}" style="flex:1;accent-color:var(--accent-blue)">
        <span style="font-size:10px;color:var(--text-muted)">→</span>
        <span style="font-size:10px;color:var(--text-secondary)" id="scrubber-label">#{cycle_num+1}</span>
      </div>
    </div>

    <!-- Detail panels: stage detail left, context right -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px">
      <div>
        <div class="section-label">Stage Detail</div>
        <div class="card" id="stage-detail-panel" style="min-height:200px">
          <p style="font-size:11px;color:var(--text-muted)">Click a pipeline stage above to see details.</p>
        </div>
      </div>
      <div>
        <div class="section-label">Sonnet Reasoning</div>
        <div class="card" id="sonnet-reasoning-panel" style="min-height:200px">
          <p style="font-size:11px;color:var(--text-secondary);line-height:1.6" id="reasoning-text">
            {(stages.get("sonnet", {}).get("reasoning_excerpt") or "Loading…")[:400]}
          </p>
        </div>
      </div>
    </div>

    <!-- Ideas from this cycle -->
    <div class="section-label">Ideas Generated</div>
    <div class="card" style="padding:0" id="ideas-panel">
      {_theater_ideas_html(stages.get("sonnet", {}).get("ideas", []))}
    </div>

  </div>

  <!-- ── TRADE LIFECYCLE VIEW ── -->
  <div id="panel-trade" style="display:none">

    <!-- Trade pills -->
    <div class="section-label">Select a Trade</div>
    <div class="card" style="padding:10px 14px">
      <div style="display:flex;flex-wrap:wrap;gap:6px">
        {pills_html}
      </div>
    </div>

    <!-- Trade hero card -->
    <div id="trade-hero" style="margin-top:10px">
      <div class="card" style="color:var(--text-muted);font-size:12px;text-align:center;padding:24px">
        Select a trade above to view its lifecycle.
      </div>
    </div>

    <!-- Price journey bar -->
    <div id="price-journey-wrap" style="display:none;margin-top:10px">
      <div class="section-label">Price Journey</div>
      <div class="card" style="padding:16px">
        <div id="price-journey-bar" style="position:relative;height:60px;background:var(--bg-card-2);border-radius:6px">
        </div>
        <div id="price-journey-labels" style="display:flex;justify-content:space-between;margin-top:6px;font-size:9px;color:var(--text-muted)">
        </div>
      </div>
    </div>

    <!-- Timeline + thesis side by side -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px">
      <div>
        <div class="section-label">Lifecycle Timeline</div>
        <div class="card" id="lifecycle-timeline" style="min-height:160px">
          <p style="font-size:11px;color:var(--text-muted)">No trade selected.</p>
        </div>
      </div>
      <div>
        <div class="section-label">Entry Thesis &amp; Exit Scenarios</div>
        <div class="card" id="trade-thesis" style="min-height:160px">
          <p style="font-size:11px;color:var(--text-muted)">No trade selected.</p>
        </div>
      </div>
    </div>

  </div>

</div>

<style>
.stage-node {{
  width: 80px; min-height: 56px; border-radius: 8px; border: 1px solid;
  padding: 7px 6px 6px; cursor: pointer; position: relative;
  text-align: center; transition: box-shadow 0.12s, border-color 0.15s;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
}}
.stage-node:hover {{ box-shadow: 0 0 0 2px var(--accent-blue); }}
.stage-node.active {{ box-shadow: 0 0 0 2px var(--accent-blue); border-color: var(--accent-blue); }}
.stage-dot {{
  position: absolute; top: 5px; right: 5px;
  width: 7px; height: 7px; border-radius: 50%;
}}
.mode-btn {{
  background: var(--bg-input); border: 1px solid var(--border);
  color: var(--text-muted); font-size: 11px; padding: 5px 14px;
  border-radius: 6px; cursor: pointer;
}}
.mode-btn.active {{
  background: rgba(79,172,254,.1); border-color: var(--accent-blue);
  color: var(--accent-blue);
}}
.trade-pill {{
  background: var(--bg-card); border: 1px solid var(--border);
  color: var(--text-secondary); font-size: 10px; padding: 4px 10px;
  border-radius: 12px; cursor: pointer; white-space: nowrap;
}}
.tp-open  {{ border-color: rgba(0,230,118,.4); color: var(--accent-green); }}
.tp-win   {{ border-color: rgba(79,172,254,.4); color: var(--accent-blue); }}
.tp-bug   {{ border-color: rgba(255,170,32,.4); color: var(--accent-amber); }}
.tp-loss  {{ border-color: rgba(255,80,80,.4); color: var(--accent-red); }}
.tp-flat  {{ border-color: var(--border); color: var(--text-muted); }}
.trade-pill:hover, .trade-pill.selected {{ box-shadow: 0 0 0 2px var(--accent-blue); }}
.tl-event {{ padding: 8px 0; border-bottom: 1px solid var(--border-subtle); font-size: 11px; }}
.tl-event:last-child {{ border-bottom: none; }}
.tl-dot {{
  display: inline-block; width: 9px; height: 9px;
  border-radius: 50%; margin-right: 6px; vertical-align: middle;
}}
#loading-indicator {{
  display: none; font-size: 10px; color: var(--text-muted);
  padding: 4px 8px; background: var(--bg-input); border-radius: 4px;
}}
</style>

<script>
var _cycleData = {cycle_json};
var _tradesData = {trades_json};
var _currentCycle = _cycleData;
var _selectedStage = null;

function _authHeader() {{
  var el = document.getElementById('auth-b64');
  return 'Basic ' + (el ? el.getAttribute('data-val') : btoa('admin:bullbearbot'));
}}

function setMode(mode) {{
  document.getElementById('panel-cycle').style.display = mode==='cycle' ? '' : 'none';
  document.getElementById('panel-trade').style.display  = mode==='trade' ? '' : 'none';
  document.getElementById('btn-cycle').classList.toggle('active', mode==='cycle');
  document.getElementById('btn-trade').classList.toggle('active', mode==='trade');
}}

function selectStage(name) {{
  document.querySelectorAll('.stage-node').forEach(n => n.classList.remove('active'));
  var el = document.getElementById('stage-' + name);
  if (el) el.classList.add('active');
  _selectedStage = name;
  renderStageDetail(_currentCycle.stages[name] || {{}}, name);
}}

function renderStageDetail(st, name) {{
  var html = '<div style="font-size:10px;text-transform:uppercase;letter-spacing:.8px;color:var(--text-muted);margin-bottom:8px">' + name + '</div>';
  html += '<div style="font-size:11px;color:var(--text-secondary)">';
  for (var k in st) {{
    if (k === 'ideas' || k === 'conviction_ranking' || k === 'submitted' || k === 'rejections') continue;
    var v = st[k];
    if (v === null || v === undefined) continue;
    if (typeof v === 'object') v = JSON.stringify(v).substring(0,80);
    html += '<div style="display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid var(--border-subtle)">';
    html += '<span style="color:var(--text-muted)">' + k + '</span>';
    html += '<span>' + v + '</span></div>';
  }}
  html += '</div>';
  // Rejections
  if (st.rejections && st.rejections.length) {{
    html += '<div style="margin-top:8px;font-size:10px;color:var(--accent-amber)">Rejections:</div>';
    st.rejections.forEach(function(r) {{
      html += '<div style="font-size:10px;padding:2px 0;color:var(--text-muted)">' + r.symbol + ': ' + r.reason + '</div>';
    }});
  }}
  document.getElementById('stage-detail-panel').innerHTML = html;
  // Also update reasoning panel if sonnet stage
  if (name === 'sonnet' && st.reasoning_excerpt) {{
    document.getElementById('reasoning-text').textContent = st.reasoning_excerpt;
  }}
}}

function loadCycle(index) {{
  var li = document.getElementById('loading-indicator');
  if (li) li.style.display = 'inline-block';
  fetch('/api/theater/cycle/' + index, {{credentials: 'include', headers: {{'Authorization': _authHeader()}}}})
    .then(r => r.json())
    .then(function(data) {{
      _currentCycle = data;
      renderCycle(data);
      if (li) li.style.display = 'none';
    }})
    .catch(function() {{ if (li) li.style.display = 'none'; }});
}}

function renderCycle(data) {{
  var stages = data.stages || {{}};
  var stageIds = ['regime','signals','scratchpad','gate','sonnet','kernel','execution','a2'];
  stageIds.forEach(function(id) {{
    var el = document.getElementById('stage-' + id);
    if (!el) return;
    var st = stages[id] || {{}};
    var status = st.status || 'warn';
    var dotEl = el.querySelector('.stage-dot');
    var c = status==='ok' ? 'var(--accent-green)' : (
            status==='skip' ? 'var(--text-muted)' : (
            status==='warn' ? 'var(--accent-amber)' : 'var(--accent-red)'));
    if (dotEl) dotEl.style.background = c;
    el.style.borderColor = c;
  }});
  // Update ideas panel
  var ideas = (stages.sonnet || {{}}).ideas || [];
  document.getElementById('ideas-panel').innerHTML = renderIdeas(ideas);
  // Update reasoning
  var rex = (stages.sonnet || {{}}).reasoning_excerpt || '';
  document.getElementById('reasoning-text').textContent = rex;
  // Update cycle label
  var ts = (data.timestamp || '').substring(0,16).replace('T',' ');
  var num = (data.cycle_number || 0) + 1;
  var total = data.total_cycles || 0;
  // Re-render detail if stage was selected
  if (_selectedStage && stages[_selectedStage]) {{
    renderStageDetail(stages[_selectedStage], _selectedStage);
  }}
  document.getElementById('scrubber-label').textContent = '#' + num;
}}

function renderIdeas(ideas) {{
  if (!ideas || !ideas.length) {{
    return '<p style="padding:12px;font-size:11px;color:var(--text-muted)">No ideas this cycle.</p>';
  }}
  var html = '<table class="data-table"><thead><tr><th>Symbol</th><th>Intent</th><th>Tier</th><th>Catalyst</th></tr></thead><tbody>';
  ideas.forEach(function(idea) {{
    var ic = idea.intent === 'buy' || idea.intent === 'add' ? 'var(--accent-green)' :
             idea.intent === 'sell' || idea.intent === 'exit' ? 'var(--accent-red)' : 'var(--text-muted)';
    html += '<tr><td style="font-weight:600">' + (idea.symbol||'') + '</td>';
    html += '<td><span style="color:' + ic + ';font-size:10px;font-weight:700">' + (idea.intent||'—').toUpperCase() + '</span></td>';
    html += '<td>' + (idea.tier||'') + '</td>';
    html += '<td style="color:var(--text-secondary)">' + (idea.catalyst||'').substring(0,60) + '</td></tr>';
  }});
  html += '</tbody></table>';
  return html;
}}

// Scrubber
document.getElementById('cycle-scrubber').addEventListener('input', function() {{
  loadCycle(parseInt(this.value));
}});

function loadTrade(symbol, entryDate) {{
  document.querySelectorAll('.trade-pill').forEach(p => p.classList.remove('selected'));
  var li = document.getElementById('loading-indicator');
  if (li) li.style.display = 'inline-block';
  fetch('/api/theater/trade/' + symbol + '?entry_date=' + entryDate,
        {{credentials: 'include', headers: {{'Authorization': _authHeader()}}}})
    .then(r => r.json())
    .then(function(data) {{
      renderTradeLifecycle(data);
      if (li) li.style.display = 'none';
    }})
    .catch(function() {{ if (li) li.style.display = 'none'; }});
}}

function renderTradeLifecycle(data) {{
  if (!data || data.status === 'not_found') {{
    document.getElementById('trade-hero').innerHTML = '<div class="card" style="color:var(--text-muted);font-size:12px;padding:16px">Trade not found.</div>';
    return;
  }}
  var pnl = data.pnl_usd || 0;
  var pnlPct = data.pnl_pct || 0;
  var pnlSign = pnl >= 0 ? '+' : '';
  var pnlColor = pnl >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';
  var statusBadge = data.status === 'open' ?
    '<span class="badge-g">OPEN</span>' :
    (pnl >= 0 ? '<span class="badge-b">WIN</span>' : '<span class="badge-r">LOSS</span>');

  var hero = '<div class="hero-card" style="background:var(--grad-a1)">';
  hero += '<div style="display:flex;align-items:flex-start;justify-content:space-between">';
  hero += '<div><div style="font-size:22px;font-weight:700;color:var(--text-primary)">' + data.symbol + '</div>';
  hero += '<div style="margin-top:4px">' + statusBadge + '</div></div>';
  hero += '<div style="text-align:right"><div style="font-size:26px;font-weight:600;color:' + pnlColor + '">' + pnlSign + '$' + Math.abs(pnl).toFixed(2) + '</div>';
  hero += '<div style="font-size:12px;color:' + pnlColor + '">' + pnlSign + pnlPct.toFixed(2) + '% · ' + (data.pnl_status||'') + '</div></div></div>';
  hero += '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:12px">';
  var metas = [
    ['Entry', data.entry_price ? '$'+data.entry_price.toFixed(2) : '—'],
    ['Current', data.current_price ? '$'+data.current_price.toFixed(2) : '—'],
    ['Stop', data.stop_price ? '$'+data.stop_price.toFixed(2) : '—'],
    ['Exit', data.exit_price ? '$'+data.exit_price.toFixed(2) : (data.status==='open' ? 'Open' : '—')],
  ];
  metas.forEach(function(m) {{
    hero += '<div><div style="font-size:9px;color:var(--text-muted);text-transform:uppercase;letter-spacing:.8px">' + m[0] + '</div>';
    hero += '<div style="font-size:13px;color:var(--text-primary)">' + m[1] + '</div></div>';
  }});
  hero += '</div>';
  if (data.catalyst_at_entry) {{
    hero += '<div style="margin-top:10px;font-size:11px;color:var(--text-secondary);line-height:1.5">' + data.catalyst_at_entry + '</div>';
  }}
  hero += '</div>';
  document.getElementById('trade-hero').innerHTML = hero;

  // Price journey
  renderPriceJourney(data.price_journey);

  // Timeline
  renderTimeline(data.lifecycle_events || []);

  // Thesis
  renderThesis(data);
}}

function renderPriceJourney(pj) {{
  var wrap = document.getElementById('price-journey-wrap');
  if (!pj || !pj.entry) {{ wrap.style.display = 'none'; return; }}
  wrap.style.display = '';
  var bar = document.getElementById('price-journey-bar');
  var labels = document.getElementById('price-journey-labels');

  var html = '';
  // Red zone: stop→entry
  if (pj.stop_pct !== null && pj.entry_pct !== null) {{
    var w = Math.max(0, pj.entry_pct - pj.stop_pct);
    html += '<div style="position:absolute;left:' + pj.stop_pct + '%;width:' + w + '%;height:100%;background:rgba(255,80,80,.12);border-radius:4px 0 0 4px"></div>';
  }}
  // Green zone: entry→current/target
  if (pj.entry_pct !== null && pj.current_pct !== null) {{
    var lo2 = Math.min(pj.entry_pct, pj.current_pct);
    var w2 = Math.abs(pj.current_pct - pj.entry_pct);
    var gc = pj.current_pct >= pj.entry_pct ? 'rgba(0,230,118,.2)' : 'rgba(255,80,80,.2)';
    html += '<div style="position:absolute;left:' + lo2 + '%;width:' + w2 + '%;height:100%;background:' + gc + '"></div>';
  }}
  // Vertical lines
  function vline(pct, color, label2) {{
    if (pct === null) return;
    html += '<div style="position:absolute;left:' + pct + '%;top:0;bottom:0;width:2px;background:' + color + ';border-radius:1px"></div>';
  }}
  vline(pj.stop_pct, '#ff5050', 'Stop');
  vline(pj.entry_pct, '#4facfe', 'Entry');
  vline(pj.current_pct, '#00e676', pj.target ? 'Now' : 'Exit');
  if (pj.target_pct !== null) vline(pj.target_pct, '#a855f7', 'Target');
  bar.innerHTML = html;

  var labHtml = '';
  var pts = [
    [pj.stop, 'Stop', pj.stop_pct],
    [pj.entry, 'Entry', pj.entry_pct],
    [pj.current, pj.target ? 'Now' : 'Exit', pj.current_pct],
    [pj.target, 'Target', pj.target_pct],
  ];
  pts.forEach(function(p) {{
    if (!p[0]) return;
    labHtml += '<span>' + p[1] + ' $' + p[0].toFixed(2) + '</span>';
  }});
  labels.innerHTML = labHtml;
}}

function renderTimeline(events) {{
  var el = document.getElementById('lifecycle-timeline');
  if (!events.length) {{ el.innerHTML = '<p style="font-size:11px;color:var(--text-muted)">No events.</p>'; return; }}
  var dotColors = {{entry:'var(--accent-blue)',exit:'var(--accent-red)',hold:'var(--accent-green)',open:'var(--accent-green)',trail_advance:'var(--accent-amber)'}};
  var html = '';
  events.forEach(function(ev) {{
    var dc = dotColors[ev.event_type] || 'var(--text-muted)';
    var ts = (ev.timestamp||'').substring(0,16).replace('T',' ');
    html += '<div class="tl-event">';
    html += '<span class="tl-dot" style="background:' + dc + '"></span>';
    html += '<span style="font-weight:600;color:var(--text-primary)">' + ev.label + '</span>';
    if (ts) html += ' <span style="color:var(--text-muted);font-size:10px">' + ts + '</span>';
    if (ev.detail) html += '<div style="color:var(--text-secondary);font-size:10px;margin-top:2px;padding-left:15px">' + ev.detail.substring(0,100) + '</div>';
    html += '</div>';
  }});
  el.innerHTML = html;
}}

function renderThesis(data) {{
  var el = document.getElementById('trade-thesis');
  var html = '';
  if (data.entry_reasoning) {{
    html += '<div style="font-size:9px;text-transform:uppercase;letter-spacing:.8px;color:var(--text-muted);margin-bottom:4px">Entry Reasoning</div>';
    html += '<p style="font-size:11px;color:var(--text-secondary);line-height:1.6;margin-bottom:10px">' + data.entry_reasoning.substring(0,400) + '</p>';
  }}
  if (data.exit_scenarios) {{
    html += '<div style="font-size:9px;text-transform:uppercase;letter-spacing:.8px;color:var(--text-muted);margin-bottom:4px">Exit Scenarios</div>';
    for (var k in data.exit_scenarios) {{
      var c = k==='beat' ? 'var(--accent-green)' : (k.includes('miss') || k==='stop_hit' ? 'var(--accent-red)' : 'var(--text-muted)');
      html += '<div style="font-size:10px;padding:3px 0;color:' + c + '">' + data.exit_scenarios[k] + '</div>';
    }}
  }}
  if (data.exit_reason) {{
    html += '<div style="margin-top:8px;font-size:10px;color:var(--text-muted)">Exit reason: <span style="color:var(--accent-amber)">' + data.exit_reason + '</span></div>';
  }}
  if (data.bug_flag) {{
    html += '<div style="margin-top:8px;padding:6px 8px;background:rgba(255,170,32,.08);border:1px solid rgba(255,170,32,.3);border-radius:6px;font-size:10px;color:var(--accent-amber)">⚠ Bug period: ' + (data.bug_flag.title||data.bug_flag.id||'') + '</div>';
  }}
  if (!html) html = '<p style="font-size:11px;color:var(--text-muted)">No thesis data.</p>';
  el.innerHTML = html;
}}

// Pre-render ideas on load
document.getElementById('ideas-panel').innerHTML = renderIdeas(
  (_cycleData.stages && _cycleData.stages.sonnet && _cycleData.stages.sonnet.ideas) || []
);
</script>
"""
    return _page_shell("Decision Theater", nav, body)


def _theater_stage_metric(stage_id: str, st: dict) -> str:
    """One-line metric for a stage node."""
    if stage_id == "regime":
        r = st.get("regime", "")
        s = st.get("score")
        return f"{r} {s}" if s else (r or "—")
    if stage_id == "signals":
        n = st.get("symbols_scored", 0)
        return f"{n} scored" if n else "—"
    if stage_id == "scratchpad":
        w = len(st.get("watching", []))
        return f"{w} watching" if w else "—"
    if stage_id == "gate":
        return "SKIP" if st.get("mode") == "SKIP" else (st.get("mode") or "—")
    if stage_id == "sonnet":
        n = st.get("ideas_generated", 0)
        c = st.get("cost_usd")
        if c:
            return f"{n} ideas ${c:.3f}"
        return f"{n} ideas" if n else "—"
    if stage_id == "kernel":
        a = st.get("approved", 0)
        r = st.get("rejected", 0)
        return f"{a}✓ {r}✗" if (a or r) else "—"
    if stage_id == "execution":
        n = st.get("orders_submitted", 0)
        return f"{n} orders" if n else "—"
    if stage_id == "a2":
        return st.get("regime", st.get("reason", "—"))[:12]
    return "—"


def _theater_ideas_html(ideas: list) -> str:
    if not ideas:
        return '<p style="padding:12px;font-size:11px;color:var(--text-muted)">No ideas this cycle.</p>'
    rows = ""
    for idea in ideas:
        intent = (idea.get("intent") or "hold").lower()
        ic = ("var(--accent-green)" if intent in ("buy", "add") else
              "var(--accent-red)" if intent in ("sell", "exit") else
              "var(--text-muted)")
        rows += (
            f'<tr><td style="font-weight:600">{idea.get("symbol","")}</td>'
            f'<td><span style="color:{ic};font-size:10px;font-weight:700">{intent.upper()}</span></td>'
            f'<td>{idea.get("tier","")}</td>'
            f'<td style="color:var(--text-secondary)">{(idea.get("catalyst","") or "")[:60]}</td></tr>'
        )
    return (
        '<table class="data-table"><thead><tr>'
        '<th>Symbol</th><th>Intent</th><th>Tier</th><th>Catalyst</th>'
        f'</tr></thead><tbody>{rows}</tbody></table>'
    )


@app.route("/theater")
def page_theater():
    return _page_theater(_now_et())


@app.route("/api/theater/cycle/<cycle_index>")
def api_theater_cycle(cycle_index: str):
    try:
        from decision_theater import get_cycle_view
        return jsonify(get_cycle_view(int(cycle_index)))
    except Exception as _exc:
        return jsonify({"error": str(_exc)}), 500


@app.route("/api/theater/trade/<symbol>")
def api_theater_trade(symbol: str):
    try:
        from decision_theater import get_trade_lifecycle
        entry_date = request.args.get("entry_date")
        return jsonify(get_trade_lifecycle(symbol, entry_date))
    except Exception as _exc:
        return jsonify({"error": str(_exc)}), 500


@app.route("/api/theater/trades")
def api_theater_trades():
    try:
        from decision_theater import get_all_trades_summary
        return jsonify(get_all_trades_summary())
    except Exception as _exc:
        return jsonify({"error": str(_exc)}), 500


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=DASHBOARD_PORT, debug=False)
