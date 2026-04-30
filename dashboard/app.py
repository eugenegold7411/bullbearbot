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


# ── Warning helpers ───────────────────────────────────────────────────────────
def _build_warnings(status: dict) -> list:
    warnings = []
    a1_mode = status["a1_mode"].get("mode", "normal").upper()
    a2_mode = status["a2_mode"].get("mode", "normal").upper()
    if a1_mode != "NORMAL":
        detail = status["a1_mode"].get("reason_detail", "")[:100]
        warnings.append(("critical", f"&#x26A0; A1 MODE: {a1_mode} — {detail}"))
    if a2_mode != "NORMAL":
        warnings.append(("orange", f"&#x26A0; A2 MODE: {a2_mode}"))
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
            warnings.append(("orange", f"&#x1F534; {p['symbol']}: near stop — gap {p['gap_to_stop']:.1f}%"))
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
    pages = [("overview", "/", "Overview"), ("a1", "/a1", "A1 Equities"), ("a2", "/a2", "A2 Options")]
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
    proj_color = "#f85149" if daily_cost * 30 > 150 else "#3fb950"
    sonnet_calls = gate.get("total_calls_today", "—")
    buys = status["buys_today"]
    sells = status["sells_today"]

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

    body = f"""
<div class="container">
{warn_html}
<div class="section-label">Accounts</div>
<div class="accounts">
  <div class="card">
    <div class="acct-title">A1 &mdash; Equities &nbsp;<span style="font-size:11px;background:{a1_color};color:#0d1117;padding:2px 7px;border-radius:10px;font-weight:700">{a1_mode}</span></div>
    <div class="acct-row"><span class="acct-label">Equity</span><span class="acct-val">{_fm(a1_equity)}</span></div>
    <div class="acct-row"><span class="acct-label">Buying Power</span><span class="acct-val">{_fm(a1_bp)}</span></div>
    <div class="acct-row"><span class="acct-label">Positions</span><span class="acct-val">{a1_pos_count}</span></div>
    <div class="acct-row"><span class="acct-label">Today P&amp;L</span><span class="acct-val" style="color:{a1_pnl_color}">{a1_pnl_sign}{_fm(a1_pnl)} ({a1_pnl_sign}{a1_pnl_pct:.2f}%)</span></div>
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
    <div class="acct-row"><span class="acct-label">Today P&amp;L</span><span class="acct-val" style="color:{a2_pnl_color}">{a2_pnl_sign}{_fm(a2_pnl)} ({a2_pnl_sign}{a2_pnl_pct:.2f}%)</span></div>
    <div class="acct-row"><span class="acct-label">Last cycle</span><span class="acct-val muted">{a2_ts}</span></div>
    <div style="margin-top:10px;font-size:12px;text-align:right"><a href="/a2">Full detail &rarr;</a></div>
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
      <tr><td>30-day Projection</td><td style="color:{proj_color}">{_fm(daily_cost * 30)}</td><td>—</td></tr>
    </tbody>
  </table>
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
        brief_html = (
            f'<div style="font-size:12px;color:#8b949e;margin-bottom:8px">'
            f'Tone: <b style="color:{tone_color}">{tone}</b> &nbsp;|&nbsp; {len(picks)} picks'
            f' &nbsp;|&nbsp; {brief_time_str}</div>{picks_html}'
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
    proj_color = "#f85149" if daily_cost * 30 > 150 else "#3fb950"
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
    <div class="stat-box"><div class="stat-label">Proj/Month</div><div class="stat-val" style="font-size:14px;color:{proj_color}">{_fm(daily_cost*30)}</div></div>
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
        a2_cards_html += (
            f'<div style="background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:12px 14px;margin-bottom:8px">'
            f'<div style="font-weight:700;font-size:14px;color:#58a6ff;margin-bottom:4px">{card["title"]}</div>'
            f'<div style="font-size:12px;color:#8b949e;margin-bottom:8px">'
            f'Strategy: {card["strategy_label"]} &nbsp;|&nbsp; IV: {card["iv_env"]} (rank {card["iv_rank_str"]})</div>'
            f'<div style="font-size:13px;margin-bottom:3px">&#x1F4C8; {card["profit_line"]}</div>'
            f'<div style="font-size:13px;margin-bottom:3px">Max gain: {card["max_gain_str"]} &nbsp;|&nbsp; Max loss: {card["max_loss_str"]}</div>'
            f'<div style="font-size:13px;margin-bottom:3px;color:{card["pnl_color"]}">Current P&amp;L: {card["pnl_str"]}</div>'
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
        "a1_decisions": a1_decs,
        "a2_decisions": a2_decs,
        "a2_pos_cards": _build_a2_position_cards(a2_structs, a2_live_pos),
        "a1_theses": _a1_top_theses(a1_decs, qctx),
        "a2_theses": _a2_top_theses(a2_decs),
        "today_pnl_a1": today_pnl_a1,
        "today_pnl_a2": today_pnl_a2,
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
