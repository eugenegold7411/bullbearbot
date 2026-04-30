#!/usr/bin/env python3
"""BullBearBot health dashboard — three-page: / overview, /a1 detail, /a2 detail."""

import json
import os
import subprocess
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
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0d1117; color: #e6edf3; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; font-size: 15px; line-height: 1.5; }
a { color: #58a6ff; text-decoration: none; }
a:hover { text-decoration: underline; }
.container { max-width: 960px; margin: 0 auto; padding: 12px; }

.nav { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 10px 16px; margin-bottom: 12px; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.nav-brand { font-size: 17px; font-weight: 700; color: #58a6ff; margin-right: 4px; white-space: nowrap; }
.nav-link { padding: 4px 12px; border-radius: 20px; font-size: 13px; font-weight: 600; color: #8b949e; border: 1px solid transparent; white-space: nowrap; }
.nav-link:hover { color: #e6edf3; text-decoration: none; }
.nav-link.active { color: #0d1117; border-color: transparent; }
.nav-right { margin-left: auto; font-size: 12px; color: #8b949e; white-space: nowrap; }

.warn-critical { background: #2d0f0f; border: 1px solid #5c1a1a; color: #f85149; border-radius: 6px; padding: 10px 14px; margin-bottom: 8px; font-size: 14px; }
.warn-orange { background: #2d2208; border: 1px solid #4a3808; color: #d29922; border-radius: 6px; padding: 10px 14px; margin-bottom: 8px; font-size: 14px; }

.card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px 16px; margin-bottom: 10px; }
.section-label { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; color: #8b949e; margin: 16px 0 8px; }

.accounts { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
@media (max-width: 520px) { .accounts { grid-template-columns: 1fr; } }
.acct-title { font-size: 13px; font-weight: 700; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 10px; }
.acct-row { display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid #21262d; font-size: 14px; }
.acct-row:last-child { border-bottom: none; }
.acct-label { color: #8b949e; }
.acct-val { font-weight: 600; }

.progress-wrap { background: #21262d; border-radius: 4px; height: 6px; margin: 4px 0 2px; overflow: hidden; }
.progress-fill { height: 6px; border-radius: 4px; }

.green { color: #3fb950; } .red { color: #f85149; } .orange { color: #d29922; } .muted { color: #8b949e; } .blue { color: #58a6ff; }

.qs-table { width: 100%; border-collapse: collapse; font-size: 14px; }
.qs-table th { background: #21262d; color: #8b949e; font-size: 12px; padding: 8px 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; text-align: left; }
.qs-table td { padding: 8px 12px; border-bottom: 1px solid #21262d; }
.qs-table tr:last-child td { border-bottom: none; }
.qs-table td:first-child { color: #8b949e; }
.qs-table td:not(:first-child) { font-weight: 600; text-align: right; }
.qs-table th:not(:first-child) { text-align: right; }

.table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
table.pos-table { width: 100%; border-collapse: collapse; font-size: 13px; white-space: nowrap; }
table.pos-table th { background: #21262d; color: #8b949e; font-weight: 600; text-align: right; padding: 8px 10px; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
table.pos-table th:first-child { text-align: left; }
table.pos-table td { padding: 9px 10px; text-align: right; border-bottom: 1px solid #21262d; }
table.pos-table td:first-child { text-align: left; }
table.pos-table tr:last-child td { border-bottom: none; }
table.pos-table tr:hover td { background: #1c2128; }

.flag { display: inline-block; font-size: 10px; font-weight: 700; padding: 1px 5px; border-radius: 3px; margin-left: 4px; vertical-align: middle; }
.flag-earn { background: #4a2e00; color: #d29922; }
.flag-over { background: #3d1a1a; color: #f85149; }
.flag-warn { background: #2d2208; color: #d29922; }
.flag-trail { background: #0d2018; color: #3fb950; }
.flag-be { background: #0d1f38; color: #58a6ff; }

.alert { padding: 9px 12px; border-radius: 6px; margin-bottom: 6px; font-size: 14px; }
.alert-green { background: #0d2018; border: 1px solid #1a4028; color: #3fb950; }
.alert-orange { background: #2d2208; border: 1px solid #4a3808; color: #d29922; }
.alert-red { background: #2d0f0f; border: 1px solid #5c1a1a; color: #f85149; }

.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 8px; }
.stat-box { background: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 10px 12px; }
.stat-label { font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }
.stat-val { font-size: 18px; font-weight: 700; margin-top: 2px; }

.reasoning { background: #0d1117; border-left: 3px solid #58a6ff; padding: 10px 14px; border-radius: 0 6px 6px 0; font-size: 14px; color: #c9d1d9; font-style: italic; margin: 10px 0; }
.log-line { font-family: "SF Mono", "Fira Code", monospace; font-size: 11px; padding: 3px 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.kv { display: flex; justify-content: space-between; padding: 5px 0; border-bottom: 1px solid #21262d; font-size: 14px; }
.kv:last-child { border-bottom: none; }
.kv-label { color: #8b949e; }
.kv-val { font-weight: 600; text-align: right; }
.dec-panel { max-height: 340px; overflow-y: auto; }
.thesis-card { border-left: 3px solid #58a6ff; background: #0d1117; padding: 10px 14px; border-radius: 0 6px 6px 0; margin-bottom: 8px; }
.thesis-card:last-child { margin-bottom: 0; }
.hero-pnl { text-align: center; padding: 20px 16px; }
.hero-number { font-size: 44px; font-weight: 800; line-height: 1.1; }
.hero-sub { font-size: 14px; margin-top: 2px; }
.watch-bullet { padding: 5px 0; border-bottom: 1px solid #21262d; font-size: 13px; }
.watch-bullet:last-child { border-bottom: none; }
.compact-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.trail-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.trail-table th { background: #21262d; color: #8b949e; font-weight: 600; padding: 6px 10px; text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }
.trail-table td { padding: 6px 10px; border-bottom: 1px solid #21262d; }
.trail-table tr:last-child td { border-bottom: none; }
@media (max-width: 520px) { .compact-grid { grid-template-columns: 1fr; } }
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
        ctx = sym_ctx.get(sym, {}) if isinstance(sym_ctx, dict) else {}
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
            profit_line = f"Profit if {underlying} stays below ${ss:.0f}" if ss else f"Profit if flat/down"
            rationale = f"IV rank {iv_rank_str} ({iv_env}) — selling call premium when vol is elevated."
        elif "put_credit_spread" in s:
            s_range = f"${ls:.0f}/${ss:.0f}" if ss else f"${ls:.0f}"
            title = f"{underlying} {s_range} Put Credit Spread — {expiry_str} ({dte_str})"
            profit_line = f"Profit if {underlying} stays above ${ss:.0f}" if ss else f"Profit if flat/up"
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


# ── Navigation ────────────────────────────────────────────────────────────────
def _nav_html(active_page: str, now_et: str, a1_color: str, a2_color: str) -> str:
    pages = [("overview", "/", "Overview"), ("a1", "/a1", "A1 Equities"), ("a2", "/a2", "A2 Options"), ("brief", "/brief", "Intelligence Brief")]
    links = ""
    for pid, href, label in pages:
        if pid == active_page:
            bg = a1_color if pid == "a1" else (a2_color if pid == "a2" else "#58a6ff")
            links += f'<a href="{href}" class="nav-link active" style="background:{bg}">{label}</a>'
        else:
            links += f'<a href="{href}" class="nav-link">{label}</a>'
    return (
        f'<div class="nav"><span class="nav-brand">&#x1F402;&#x1F43B; BullBearBot</span>'
        f'{links}'
        f'<div class="nav-right">Updated {now_et}&nbsp;&nbsp;Refresh in <span id="cd">60</span>s</div>'
        f'</div>'
    )


def _page_shell(title: str, nav: str, body: str) -> str:
    return (
        '<!DOCTYPE html><html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta http-equiv="refresh" content="60">'
        f'<title>{title} — BullBearBot</title>'
        '<style>' + SHARED_CSS + '</style>'
        '</head><body>'
        + nav + body + _COUNTDOWN_JS +
        '</body></html>'
    )


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
    nav = _nav_html("overview", now_et, a1_color, a2_color)
    warn_html = _warnings_html(status.get("warnings", []))

    a1_pnl, a1_pnl_pct = status.get("today_pnl_a1", (0.0, 0.0))
    a2_pnl, a2_pnl_pct = status.get("today_pnl_a2", (0.0, 0.0))
    a1_pnl_color = "#3fb950" if a1_pnl >= 0 else "#f85149"
    a2_pnl_color = "#3fb950" if a2_pnl >= 0 else "#f85149"
    a1_pnl_sign = "+" if a1_pnl >= 0 else ""
    a2_pnl_sign = "+" if a2_pnl >= 0 else ""

    if a1_acc:
        a1_equity = float(a1_acc.equity or 0)
        a1_bp = float(a1_acc.buying_power or 0)
        a1_pos_count = len(status["positions"])
        a1_unreal = sum(p["unreal_pl"] for p in status["positions"])
        a1_unreal_c = "#3fb950" if a1_unreal >= 0 else "#f85149"
        a1_unreal_s = "+" if a1_unreal >= 0 else ""
        a1_invested = sum(p["market_val"] for p in status["positions"])
        a1_util = min(100.0, a1_invested / a1_equity * 100) if a1_equity else 0.0
        a1_util_c = "#f85149" if a1_util > 80 else ("#d29922" if a1_util > 60 else "#3fb950")
    else:
        a1_equity = a1_bp = a1_invested = a1_util = 0.0
        a1_pos_count = 0
        a1_unreal = 0.0; a1_unreal_c = "#8b949e"; a1_unreal_s = ""
        a1_util_c = "#8b949e"

    if a2_acc:
        a2_equity = float(a2_acc.equity or 0)
        a2_bp = float(a2_acc.buying_power or 0)
        a2_pos_count = len(a2d.get("positions", []))
    else:
        a2_equity = a2_bp = 0.0
        a2_pos_count = 0

    costs = status["costs"]
    gate = status["gate"]
    daily_cost = float(costs.get("daily_cost", 0) or 0)
    daily_calls = costs.get("daily_calls", 0)
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

    # P&L hero
    combined_pnl = a1_pnl + a2_pnl
    combined_color = "#3fb950" if combined_pnl >= 0 else "#f85149"
    combined_sign = "+" if combined_pnl >= 0 else ""

    # Watch Now
    watch_bullets = _watch_now_bullets(status)
    watch_html = ""
    if watch_bullets:
        items = ""
        for sev, text in watch_bullets:
            icon = "&#x1F534;" if sev == "critical" else "&#x26A0;&#xFE0F;"
            color = "#f85149" if sev == "critical" else "#d29922"
            items += f'<div class="watch-bullet"><span style="color:{color}">{icon}</span> {text}</div>'
        watch_html = f'<div class="card" style="padding:10px 14px">{items}</div>'

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

    body = f"""
<div class="container">

<div class="section-label">Today&apos;s P&amp;L</div>
<div class="card hero-pnl">
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px">
    <div>
      <div style="font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px">A1 Equities</div>
      <div class="hero-number" style="color:{a1_pnl_color}">{a1_pnl_sign}{_fm(a1_pnl)}</div>
      <div class="hero-sub" style="color:{a1_pnl_color}">{a1_pnl_sign}{a1_pnl_pct:.2f}%</div>
    </div>
    <div>
      <div style="font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px">A2 Options</div>
      <div class="hero-number" style="color:{a2_pnl_color}">{a2_pnl_sign}{_fm(a2_pnl)}</div>
      <div class="hero-sub" style="color:{a2_pnl_color}">{a2_pnl_sign}{a2_pnl_pct:.2f}%</div>
    </div>
  </div>
  <div style="border-top:1px solid #30363d;padding-top:12px">
    <div style="font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px">Combined</div>
    <div style="font-size:28px;font-weight:700;color:{combined_color}">{combined_sign}{_fm(combined_pnl)}</div>
  </div>
</div>

<div class="section-label">Watch Now</div>
{watch_html if watch_html else '<div class="card"><div style="color:#3fb950;font-size:13px">&#x2713; Nothing urgent to watch.</div></div>'}

{warn_html}

<div class="section-label">Accounts</div>
<div class="accounts">
  <div class="card">
    <div class="acct-title">A1 &mdash; Equities &nbsp;<span style="font-size:11px;background:{a1_color};color:#0d1117;padding:2px 7px;border-radius:10px;font-weight:700">{a1_mode}</span></div>
    <div class="acct-row"><span class="acct-label">Equity</span><span class="acct-val">{_fm(a1_equity)}</span></div>
    <div class="acct-row"><span class="acct-label">Buying Power</span><span class="acct-val">{_fm(a1_bp)}</span></div>
    <div class="acct-row"><span class="acct-label">Positions</span><span class="acct-val">{a1_pos_count}</span></div>
    <div class="acct-row"><span class="acct-label">Unrealized P&amp;L</span><span class="acct-val" style="color:{a1_unreal_c}">{a1_unreal_s}{_fm(a1_unreal)}</span></div>
    <div style="margin-top:8px">
      <div style="font-size:11px;color:#8b949e;margin-bottom:3px">Capital utilization {a1_util:.0f}%</div>
      <div class="progress-wrap"><div class="progress-fill" style="width:{a1_util:.0f}%;background:{a1_util_c}"></div></div>
    </div>
    <div style="margin-top:10px;font-size:12px;text-align:right"><a href="/a1">Full detail &rarr;</a></div>
  </div>
  <div class="card">
    <div class="acct-title">A2 &mdash; Options &nbsp;<span style="font-size:11px;background:{a2_color};color:#0d1117;padding:2px 7px;border-radius:10px;font-weight:700">{a2_mode}</span></div>
    <div class="acct-row"><span class="acct-label">Equity</span><span class="acct-val">{_fm(a2_equity)}</span></div>
    <div class="acct-row"><span class="acct-label">Buying Power</span><span class="acct-val">{_fm(a2_bp)}</span></div>
    <div class="acct-row"><span class="acct-label">Positions</span><span class="acct-val">{a2_pos_count}</span></div>
    <div class="acct-row"><span class="acct-label">Last cycle</span><span class="acct-val muted">{a2_ts}</span></div>
    <div style="margin-top:10px;font-size:12px;text-align:right"><a href="/a2">Full detail &rarr;</a></div>
  </div>
</div>

<div class="section-label">Trail Status</div>
<div class="card" style="padding:0 0 4px">{trail_html}</div>

{f'<div class="section-label">Allocator</div><div class="card">{allocator_line}</div>' if allocator_line else ''}

<div class="section-label">Recent Decisions</div>
<div class="compact-grid">
  <div class="card" style="padding:10px 12px">
    <div style="font-size:11px;font-weight:700;color:#8b949e;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px">A1</div>
    {a1_comp}
  </div>
  <div class="card" style="padding:10px 12px">
    <div style="font-size:11px;font-weight:700;color:#8b949e;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px">A2</div>
    {a2_comp}
  </div>
</div>

<div class="section-label">Quick Stats</div>
<div class="card" style="padding:0">
  <table class="qs-table">
    <thead><tr><th></th><th>A1 Equities</th><th>A2 Options</th></tr></thead>
    <tbody>
      <tr><td>Today P&amp;L</td><td style="color:{a1_pnl_color}">{a1_pnl_sign}{_fm(a1_pnl)}</td><td style="color:{a2_pnl_color}">{a2_pnl_sign}{_fm(a2_pnl)}</td></tr>
      <tr><td>Open Positions</td><td>{a1_pos_count}</td><td>{a2_pos_count}</td></tr>
      <tr><td>Mode</td><td style="color:{a1_color}">{a1_mode}</td><td style="color:{a2_color}">{a2_mode}</td></tr>
      <tr><td>Sonnet Calls Today</td><td>{sonnet_calls}</td><td>—</td></tr>
      <tr><td>Buys / Sells</td><td>{buys} / {sells}</td><td>—</td></tr>
      <tr><td>Claude Cost Today</td><td style="color:{proj_color}">{_fm(daily_cost)} ({daily_calls:,} calls)</td><td>—</td></tr>
      <tr><td>Proj/Month (22d)</td><td style="color:{proj_color}">{proj_icon}{_fm(proj_monthly)}</td><td>—</td></tr>
    </tbody>
  </table>
  {f'<div style="padding:8px 12px;border-top:1px solid #21262d"><div style="font-size:11px;color:#8b949e;margin-bottom:4px;text-transform:uppercase;letter-spacing:0.5px">Top callers</div>{callers_html}</div>' if callers_html else ''}
</div>

<div class="section-label">A2 Pipeline Today</div>
<div class="card">
  <div style="font-size:13px;color:#c9d1d9;font-family:monospace">{a2_pipe_str}</div>
</div>

<div class="section-label">Active Flags</div>
<div>{flags_html}</div>

<div class="section-label">System</div>
<div class="card">
  <div class="kv"><span class="kv-label">Git HEAD</span><span class="kv-val" style="font-family:monospace">{git_hash}</span></div>
  <div class="kv"><span class="kv-label">Service up since</span><span class="kv-val muted">{svc_uptime}</span></div>
</div>
<div style="height:24px"></div>
</div>"""
    return _page_shell("Overview", nav, body)


# ── A1 detail page ────────────────────────────────────────────────────────────
def _page_a1(status: dict, now_et: str) -> str:
    a1d = status["a1"]
    a1_acc = a1d.get("account")
    a1_mode = status["a1_mode"].get("mode", "unknown").upper()
    a2_mode = status["a2_mode"].get("mode", "unknown").upper()
    a1_color = _mode_color(a1_mode)
    a2_color = _mode_color(a2_mode)
    nav = _nav_html("a1", now_et, a1_color, a2_color)
    warn_html = _warnings_html(status.get("warnings", []))

    a1_pnl, a1_pnl_pct = status.get("today_pnl_a1", (0.0, 0.0))
    a1_pnl_color = "#3fb950" if a1_pnl >= 0 else "#f85149"
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
    daily_calls = costs.get("daily_calls", 0)
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

    # Recent errors
    log_errors = status["log_errors"]
    errors_html = ""
    for err in log_errors:
        lc = "#f85149" if "  ERROR  " in err or "  CRITICAL  " in err else "#d29922"
        errors_html += f'<div class="log-line" style="color:{lc}">{err[-180:]}</div>'
    if not errors_html:
        errors_html = '<div class="log-line" style="color:#3fb950">No recent warnings or errors</div>'

    a1_orders_html = _fmt_orders_html(a1d.get("recent_orders", []), is_options=False, limit=6)

    body = f"""
<div class="container">
{warn_html}
<div class="section-label">A1 Account Summary</div>
<div class="card">
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:0">
    <div>
      <div class="acct-row"><span class="acct-label">Equity</span><span class="acct-val">{_fm(a1_equity)}</span></div>
      <div class="acct-row"><span class="acct-label">Cash</span><span class="acct-val">{_fm(a1_cash)}</span></div>
      <div class="acct-row"><span class="acct-label">Buying Power</span><span class="acct-val">{_fm(a1_bp)}</span></div>
    </div>
    <div style="padding-left:16px;border-left:1px solid #21262d">
      <div class="acct-row"><span class="acct-label">Positions</span><span class="acct-val">{a1_pos_count}</span></div>
      <div class="acct-row"><span class="acct-label">Today P&amp;L</span><span class="acct-val" style="color:{a1_pnl_color}">{a1_pnl_sign}{_fm(a1_pnl)} ({a1_pnl_sign}{a1_pnl_pct:.2f}%)</span></div>
      <div class="acct-row"><span class="acct-label">Unrealized P&amp;L</span><span class="acct-val" style="color:{a1_unreal_c}">{a1_unreal_s}{_fm(a1_unreal)}</span></div>
    </div>
  </div>
  <div style="margin-top:10px">
    <div style="font-size:11px;color:#8b949e;margin-bottom:3px">Capital utilization {a1_util:.0f}% (${a1_invested:,.0f} deployed)</div>
    <div class="progress-wrap"><div class="progress-fill" style="width:{a1_util:.0f}%;background:{a1_util_c}"></div></div>
  </div>
</div>

<div class="section-label">Morning Brief</div>
<div class="card">{brief_html}</div>

<div class="section-label">Top 5 Active Theses</div>
<div class="card">{theses_html}</div>

<div class="section-label">Last 5 Decisions &mdash; A1</div>
<div class="card"><div class="dec-panel">{a1_decs_html}</div></div>

<div class="section-label">Positions &mdash; A1 ({a1_pos_count} open)</div>
<div class="card" style="padding:0 0 4px">
  <div class="table-wrap">
    <table class="pos-table">
      <thead><tr>
        <th>Symbol</th><th>Qty</th><th>Entry</th><th>Current</th>
        <th>P&amp;L $</th><th>P&amp;L %</th><th>Stop</th><th>Gap</th><th>% BP</th>
      </tr></thead>
      <tbody>{positions_html}</tbody>
    </table>
  </div>
  {pos_extra_note}
</div>

<div class="section-label">Recent Orders &mdash; A1 (last 6)</div>
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
    <div class="stat-box"><div class="stat-label">Cost Today</div><div class="stat-val" style="font-size:14px;color:{proj_color}">{_fm(daily_cost)}</div></div>
    <div class="stat-box"><div class="stat-label">Proj/Month (22d)</div><div class="stat-val" style="font-size:14px;color:{proj_color}">{_fm(proj_monthly_a1)}</div></div>
  </div>
  <div class="kv"><span class="kv-label">Last Sonnet</span><span class="kv-val">{last_sonnet_ts}</span></div>
  <div class="kv"><span class="kv-label">Regime</span><span class="kv-val">{last_regime} (score {regime_score}) &middot; {dec_session}</span></div>
  <div class="kv"><span class="kv-label">VIX</span><span class="kv-val">{last_vix_pf}</span></div>
  <div class="kv"><span class="kv-label">Last Decision</span><span class="kv-val muted">{last_dec_ts}</span></div>
  {f'<div class="reasoning">{reasoning_2s}</div>' if reasoning_2s else ''}
</div>

<div class="section-label">Allocator Shadow</div>
<div class="card">
  <div class="kv"><span class="kv-label">Status</span><span class="kv-val">{alloc_st}</span></div>
  <div class="kv"><span class="kv-label">Last Run</span><span class="kv-val muted">{alloc_last}</span></div>
</div>

<div class="section-label">Recent Log Events</div>
<div class="card">{errors_html}</div>

<div style="height:24px"></div>
</div>"""
    return _page_shell("A1 Equities", nav, body)


# ── A2 detail page ────────────────────────────────────────────────────────────
def _page_a2(status: dict, now_et: str) -> str:
    a2d = status["a2"]
    a2_acc = a2d.get("account")
    a1_mode = status["a1_mode"].get("mode", "unknown").upper()
    a2_mode = status["a2_mode"].get("mode", "unknown").upper()
    a1_color = _mode_color(a1_mode)
    a2_color = _mode_color(a2_mode)
    nav = _nav_html("a2", now_et, a1_color, a2_color)
    warn_html = _warnings_html(status.get("warnings", []))

    a2_pnl, a2_pnl_pct = status.get("today_pnl_a2", (0.0, 0.0))
    a2_pnl_color = "#3fb950" if a2_pnl >= 0 else "#f85149"
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
<div class="card">
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:0">
    <div>
      <div class="acct-row"><span class="acct-label">Equity</span><span class="acct-val">{_fm(a2_equity)}</span></div>
      <div class="acct-row"><span class="acct-label">Cash</span><span class="acct-val">{_fm(a2_cash)}</span></div>
      <div class="acct-row"><span class="acct-label">Buying Power</span><span class="acct-val">{_fm(a2_bp)}</span></div>
    </div>
    <div style="padding-left:16px;border-left:1px solid #21262d">
      <div class="acct-row"><span class="acct-label">Positions</span><span class="acct-val">{a2_pos_count}</span></div>
      <div class="acct-row"><span class="acct-label">Today P&amp;L</span><span class="acct-val" style="color:{a2_pnl_color}">{a2_pnl_sign}{_fm(a2_pnl)} ({a2_pnl_sign}{a2_pnl_pct:.2f}%)</span></div>
      <div class="acct-row"><span class="acct-label">Mode</span><span class="acct-val" style="color:{a2_color}">{a2_mode}</span></div>
    </div>
  </div>
</div>

<div class="section-label">Top 5 A2 Theses</div>
<div class="card">{a2_theses_html}</div>

<div class="section-label">Positions &mdash; A2 Options ({a2_pos_count} open)</div>
<div class="card">{a2_cards_html}</div>

<div class="section-label">Last 5 Decisions &mdash; A2</div>
<div class="card"><div class="dec-panel">{a2_decs_html}</div></div>

<div class="section-label">Recent Orders &mdash; A2 (last 6)</div>
<div class="card">{a2_orders_html}</div>

<div class="section-label">Strategy Pipeline &mdash; Last Cycle</div>
<div class="card">
  <div class="kv"><span class="kv-label">Last Cycle</span><span class="kv-val muted">{a2_ts}</span></div>
  <div class="kv"><span class="kv-label">Outcome</span><span class="kv-val">{a2_action_str}</span></div>
  <div class="kv"><span class="kv-label">Debate Direction</span><span class="kv-val">{debate_dir}</span></div>
  <div class="kv"><span class="kv-label">Debate Synthesis</span><span class="kv-val">{debate_synth}</span></div>
  <div class="kv"><span class="kv-label">Confidence</span><span class="kv-val">{debate_conf}</span></div>
  {pipeline_html}
</div>

<div class="section-label">IV Environment &mdash; Open Structures</div>
<div class="card">{iv_summary_html}</div>

<div style="height:24px"></div>
</div>"""
    return _page_shell("A2 Options", nav, body)


def _page_brief(status: dict, now_et: str) -> str:
    a1_mode = status["a1_mode"].get("mode", "unknown").upper()
    a2_mode = status["a2_mode"].get("mode", "unknown").upper()
    a1_color = _mode_color(a1_mode)
    a2_color = _mode_color(a2_mode)
    nav = _nav_html("brief", now_et, a1_color, a2_color)

    brief = status.get("intelligence_brief", {})
    if not brief:
        body = '<div style="padding:40px;color:#8b949e;text-align:center;font-size:16px">Intelligence brief not yet generated.<br>Runs at 4:00 AM ET (premarket) and 9:25 AM ET (market open).</div>'
        return _page_shell("Intelligence Brief", nav, body)

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
        f'<div style="max-width:1200px;margin:0 auto;padding:0 8px">'
        + header_html + stale_html + updates_html
        + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:0">'
        + '<div>' + regime_html + sectors_html + earnings_html + insider_html + macro_html + '</div>'
        + '<div>' + longs_html + bears_html + watch_html + avoid_html + '</div>'
        + '</div></div>'
    )
    return _page_shell("Intelligence Brief", nav, body)


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
    }
    st["warnings"] = _build_warnings(st)
    return st


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
@requires_auth
def index():
    status = _build_status()
    return _page_overview(status, _now_et())


@app.route("/a1")
@requires_auth
def page_a1():
    status = _build_status()
    return _page_a1(status, _now_et())


@app.route("/a2")
@requires_auth
def page_a2():
    status = _build_status()
    return _page_a2(status, _now_et())


@app.route("/brief")
@requires_auth
def page_brief():
    status = _build_status()
    return _page_brief(status, _now_et())


@app.route("/api/status")
@requires_auth
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


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=DASHBOARD_PORT, debug=False)
