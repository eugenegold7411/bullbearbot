"""
report.py — trading bot performance dashboard.

Terminal:  python report.py --print
Email now: python report.py
Scheduler calls send_report_email() daily at 9 AM PST.

Dashboard sections:
  1. Account Overview  — equity, cash, day P&L, all-time P&L vs starting $100K
  2. Equity Curve      — ASCII chart of daily equity from Alpaca portfolio history
  3. Trade History     — every filled order from Alpaca with P&L per trade
  4. Per-Symbol Stats  — win rate, total P&L, trade count per symbol
  5. Bot Activity      — cycles today, regime/session distribution
  6. Memory Snapshot   — ticker stats and any active lessons
"""

import argparse
import json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest, GetPortfolioHistoryRequest
from alpaca.trading.enums import QueryOrderStatus, OrderSide

from log_setup import get_logger
import memory as mem
import portfolio_intelligence as pi

load_dotenv()

log        = get_logger(__name__)
TRADE_LOG  = Path(__file__).parent / "logs" / "trades.jsonl"
TO_EMAIL   = "eugene.gold@gmail.com"
FROM_EMAIL = os.getenv("SENDGRID_FROM_EMAIL", "eugene.gold@gmail.com")
STARTING_EQUITY = 100_000.0


# ── Alpaca helpers ────────────────────────────────────────────────────────────

def _alpaca_client():
    return TradingClient(
        os.getenv("ALPACA_API_KEY"),
        os.getenv("ALPACA_SECRET_KEY"),
        paper=True,
    )


def _get_account():
    try:
        return _alpaca_client().get_account()
    except Exception:
        return None


def _get_portfolio_history(period: str = "1M") -> list[dict]:
    """Returns list of {date, equity, pl, pl_pct} dicts, newest last."""
    try:
        h = _alpaca_client().get_portfolio_history(
            GetPortfolioHistoryRequest(period=period, timeframe="1D")
        )
        rows = []
        for ts, eq, pl, plp in zip(h.timestamp, h.equity,
                                    h.profit_loss, h.profit_loss_pct):
            if eq and eq > 0:
                rows.append({
                    "date":    datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d"),
                    "equity":  float(eq),
                    "pl":      float(pl),
                    "pl_pct":  float(plp) * 100,
                })
        return rows
    except Exception:
        return []


def _get_closed_orders(days_back: int = 30) -> list:
    try:
        return _alpaca_client().get_orders(GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            limit=200,
            after=datetime.now(timezone.utc) - timedelta(days=days_back),
        ))
    except Exception:
        return []


def _get_positions() -> list:
    try:
        return _alpaca_client().get_all_positions()
    except Exception:
        return []


# ── Trade journal helpers ─────────────────────────────────────────────────────

def _load_journal(since: datetime | None = None) -> list[dict]:
    if not TRADE_LOG.exists():
        return []
    records = []
    for line in TRADE_LOG.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            if since:
                ts = datetime.fromisoformat(
                    r.get("ts", "1970-01-01T00:00:00+00:00"))
                if ts < since:
                    continue
            records.append(r)
        except (json.JSONDecodeError, ValueError):
            continue
    return records


# ── ASCII equity chart ────────────────────────────────────────────────────────

def _ascii_chart(history: list[dict], width: int = 50, height: int = 8) -> str:
    """Renders a simple ASCII line chart of equity over time."""
    if len(history) < 2:
        return "  (not enough history for chart yet)"

    equities = [r["equity"] for r in history]
    dates    = [r["date"]   for r in history]

    lo = min(equities)
    hi = max(equities)
    span = hi - lo if hi != lo else 1.0

    # Scale to grid
    def _y(val):
        return int((val - lo) / span * (height - 1))

    # Build grid
    grid = [[" "] * width for _ in range(height)]

    # Plot points
    step = max(1, len(equities) / width)
    prev_col, prev_row = None, None
    for i, eq in enumerate(equities):
        col = min(width - 1, int(i / len(equities) * width))
        row = height - 1 - _y(eq)
        grid[row][col] = "●"
        if prev_col is not None and col > prev_col:
            # Fill gaps with dashes
            for c in range(prev_col + 1, col):
                r = height - 1 - _y(prev_eq + (eq - prev_eq) * (c - prev_col) / (col - prev_col))
                r = max(0, min(height - 1, r))
                grid[r][c] = "─"
        prev_col, prev_row, prev_eq = col, row, eq

    # Y-axis labels
    lines = []
    for r, row in enumerate(grid):
        eq_val = hi - (r / (height - 1)) * span
        label  = f"  ${eq_val/1000:>6.1f}k │"
        lines.append(label + "".join(row))

    # X-axis
    lines.append("         └" + "─" * width)
    # Date labels (first and last)
    if dates:
        pad    = width - len(dates[0]) - len(dates[-1])
        lines.append(f"          {dates[0]}{' ' * max(1, pad)}{dates[-1]}")

    return "\n".join(lines)


# ── P&L from closed orders ────────────────────────────────────────────────────

def _compute_trade_pnl(orders: list) -> list[dict]:
    """
    Pairs buy fills with subsequent sell/stop fills to compute per-trade P&L.
    Returns list of trade dicts sorted newest first.
    """
    # Group by symbol, then match buys to sells chronologically
    by_symbol: dict[str, list] = defaultdict(list)
    for o in orders:
        if o.filled_qty and float(o.filled_qty) > 0:
            by_symbol[o.symbol].append(o)

    trades = []
    for sym, sym_orders in by_symbol.items():
        sym_orders.sort(key=lambda o: o.filled_at or o.created_at)
        buys  = [o for o in sym_orders if o.side == OrderSide.BUY]
        sells = [o for o in sym_orders if o.side == OrderSide.SELL]

        for buy in buys:
            entry_price = float(buy.filled_avg_price or 0)
            qty         = float(buy.filled_qty or 0)
            filled_at   = buy.filled_at

            # Find the nearest sell after this buy
            exit_order = next(
                (s for s in sells
                 if (s.filled_at or s.created_at) >= (filled_at or buy.created_at)),
                None
            )

            if exit_order:
                exit_price = float(exit_order.filled_avg_price or 0)
                pl         = (exit_price - entry_price) * qty
                trades.append({
                    "symbol":      sym,
                    "entry":       entry_price,
                    "exit":        exit_price,
                    "qty":         qty,
                    "pl":          round(pl, 2),
                    "outcome":     "win" if pl > 0 else ("loss" if pl < 0 else "flat"),
                    "date":        (filled_at or buy.created_at).strftime("%b %d %H:%M") if filled_at else "?",
                })
            else:
                trades.append({
                    "symbol":  sym,
                    "entry":   entry_price,
                    "exit":    None,
                    "qty":     qty,
                    "pl":      None,
                    "outcome": "open",
                    "date":    (filled_at or buy.created_at).strftime("%b %d %H:%M") if filled_at else "?",
                })

    trades.sort(key=lambda t: t["date"], reverse=True)
    return trades


# ── Master report generator ───────────────────────────────────────────────────

def generate_report(target_date: date | None = None) -> dict:
    today    = target_date or date.today()
    since_dt = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)

    # Account
    account      = _get_account()
    equity       = float(account.equity)       if account else 0.0
    cash         = float(account.cash)         if account else 0.0
    last_equity  = float(account.last_equity)  if account else 0.0
    day_pl       = equity - last_equity
    all_time_pl  = equity - STARTING_EQUITY

    # Portfolio history (last 30 days)
    history = _get_portfolio_history("1M")

    # Closed orders & trade P&L
    closed_orders = _get_closed_orders(days_back=30)
    trades        = _compute_trade_pnl(closed_orders)
    closed_trades = [t for t in trades if t["outcome"] != "open"]
    wins          = [t for t in closed_trades if t["outcome"] == "win"]
    losses        = [t for t in closed_trades if t["outcome"] == "loss"]
    win_rate      = len(wins) / len(closed_trades) * 100 if closed_trades else 0.0
    avg_win       = sum(t["pl"] for t in wins)   / len(wins)   if wins   else 0.0
    avg_loss      = sum(t["pl"] for t in losses) / len(losses) if losses else 0.0
    profit_factor = abs(avg_win / avg_loss) if avg_loss != 0 else 0.0

    # Per-symbol breakdown
    symbol_stats: dict[str, dict] = defaultdict(
        lambda: {"trades": 0, "wins": 0, "losses": 0, "pl": 0.0})
    for t in closed_trades:
        s = symbol_stats[t["symbol"]]
        s["trades"] += 1
        s["pl"]     += t["pl"] or 0
        if t["outcome"] == "win":    s["wins"]   += 1
        elif t["outcome"] == "loss": s["losses"] += 1

    # Journal (today only)
    journal       = _load_journal(since=since_dt)
    cycles_today  = [j for j in journal if j.get("event") == "cycle_decision"]
    orders_today  = [j for j in journal if j.get("event") != "cycle_decision"]
    regime_dist   = defaultdict(int)
    session_dist  = defaultdict(int)
    for c in cycles_today:
        regime_dist[c.get("regime", "?")]  += 1
        session_dist[c.get("session", "?")] += 1

    # Memory
    ticker_stats  = mem.get_ticker_stats()
    lessons       = mem.get_ticker_lessons()

    # Portfolio intelligence
    positions     = _get_positions()
    pi_config     = {}
    try:
        pi_config = json.loads(
            (Path(__file__).parent / "strategy_config.json").read_text()
        )
    except Exception:
        pass
    portfolio_intel = {}
    try:
        portfolio_intel = pi.build_portfolio_intelligence(equity, positions, pi_config)
    except Exception as exc:
        log.warning("Portfolio intelligence failed: %s", exc)

    return {
        "date":          today.isoformat(),
        "equity":        equity,
        "cash":          cash,
        "day_pl":        day_pl,
        "all_time_pl":   all_time_pl,
        "history":       history,
        "trades":        trades,
        "closed_trades": len(closed_trades),
        "win_rate":      win_rate,
        "avg_win":       avg_win,
        "avg_loss":      avg_loss,
        "profit_factor": profit_factor,
        "symbol_stats":  dict(symbol_stats),
        "cycles_today":  len(cycles_today),
        "submitted_today": sum(1 for o in orders_today if o.get("status") == "submitted"),
        "rejected_today":  sum(1 for o in orders_today if o.get("status") == "rejected"),
        "regime_dist":   dict(regime_dist),
        "session_dist":  dict(session_dist),
        "ticker_stats":       ticker_stats,
        "lessons":            lessons,
        "portfolio_intel":    portfolio_intel,
    }


# ── Terminal renderer ─────────────────────────────────────────────────────────

def format_terminal(r: dict) -> str:
    day_sign = "+" if r["day_pl"] >= 0 else ""
    atm_sign = "+" if r["all_time_pl"] >= 0 else ""
    day_col  = ""   # no ANSI in terminal for broad compat

    lines = [
        "",
        "╔" + "═" * 58 + "╗",
        f"║{'  TRADING BOT PERFORMANCE DASHBOARD':^58}║",
        f"║{'  ' + r['date']:^58}║",
        "╠" + "═" * 58 + "╣",
        "",
        "  ── ACCOUNT OVERVIEW ──────────────────────────────────",
        f"  Equity          :  ${r['equity']:>12,.2f}",
        f"  Cash            :  ${r['cash']:>12,.2f}",
        f"  Day P&L         :  {day_sign}${r['day_pl']:>11,.2f}",
        f"  All-Time P&L    :  {atm_sign}${r['all_time_pl']:>11,.2f}  "
        f"({atm_sign}{r['all_time_pl']/STARTING_EQUITY*100:.2f}% vs ${STARTING_EQUITY/1000:.0f}k start)",
        "",
        "  ── EQUITY CURVE (30 days) ──────────────────────────────",
    ]
    lines.append(_ascii_chart(r["history"]))
    lines += [
        "",
        "  ── TRADE PERFORMANCE ───────────────────────────────────",
        f"  Closed trades   :  {r['closed_trades']}",
        f"  Win rate        :  {r['win_rate']:.1f}%",
        f"  Avg win         :  ${r['avg_win']:>8,.2f}",
        f"  Avg loss        :  ${r['avg_loss']:>8,.2f}",
        f"  Profit factor   :  {r['profit_factor']:.2f}x",
    ]

    if r["trades"]:
        lines.append("")
        lines.append("  ── RECENT TRADES ───────────────────────────────────────")
        lines.append(f"  {'Date':<14} {'Symbol':<7} {'Side':<5} {'Qty':>6} "
                     f"{'Entry':>8} {'Exit':>8} {'P&L':>9} {'Result':<7}")
        lines.append("  " + "─" * 64)
        for t in r["trades"][:15]:
            pl_str  = f"${t['pl']:>+,.2f}" if t["pl"] is not None else "  open"
            ex_str  = f"${t['exit']:,.2f}" if t["exit"] else "   open"
            lines.append(
                f"  {t['date']:<14} {t['symbol']:<7} {'BUY':<5} {t['qty']:>6.2f} "
                f"  ${t['entry']:>6,.2f} {ex_str:>8} {pl_str:>9}  {t['outcome']:<7}"
            )
    else:
        lines.append("  No closed trades yet.")

    if r["symbol_stats"]:
        lines += [
            "",
            "  ── PER-SYMBOL BREAKDOWN ────────────────────────────────",
            f"  {'Symbol':<8} {'Trades':>7} {'Wins':>6} {'Losses':>7} "
            f"{'Win%':>6} {'Total P&L':>11}",
            "  " + "─" * 50,
        ]
        for sym, s in sorted(r["symbol_stats"].items(),
                              key=lambda x: x[1]["pl"], reverse=True):
            wr = s["wins"] / s["trades"] * 100 if s["trades"] else 0
            lines.append(
                f"  {sym:<8} {s['trades']:>7} {s['wins']:>6} {s['losses']:>7} "
                f"{wr:>5.0f}%  ${s['pl']:>+9,.2f}"
            )

    lines += [
        "",
        "  ── BOT ACTIVITY TODAY ──────────────────────────────────",
        f"  Cycles run      :  {r['cycles_today']}",
        f"  Orders submitted:  {r['submitted_today']}",
        f"  Orders rejected :  {r['rejected_today']}",
    ]
    if r["regime_dist"]:
        lines.append("  Regime dist     :  " +
                     "  ".join(f"{k}={v}" for k, v in r["regime_dist"].items()))
    if r["session_dist"]:
        lines.append("  Session dist    :  " +
                     "  ".join(f"{k}={v}" for k, v in r["session_dist"].items()))

    if r["ticker_stats"]:
        lines += [
            "",
            "  ── MEMORY: ALL-TIME TICKER STATS ───────────────────────",
            f"  {'Symbol':<8} {'Trades':>7} {'Wins':>6} {'Losses':>7} "
            f"{'Win%':>6} {'Pending':>8}",
            "  " + "─" * 44,
        ]
        for sym, s in sorted(r["ticker_stats"].items(),
                              key=lambda x: x[1]["trades"], reverse=True):
            wr = (s["wins"] / (s["wins"] + s["losses"]) * 100
                  if s["wins"] + s["losses"] > 0 else 0)
            lines.append(
                f"  {sym:<8} {s['trades']:>7} {s['wins']:>6} {s['losses']:>7} "
                f"{wr:>5.0f}%  {s['pending']:>7}"
            )

    if r["lessons"]:
        lines += ["", "  ── ACTIVE LESSONS / AVOID LIST ────────────────────────"]
        lines.append(r["lessons"])

    pi_data = r.get("portfolio_intel", {})
    if pi_data:
        lines += ["", "  ── PORTFOLIO INTELLIGENCE ──────────────────────────────"]
        sizes       = pi_data.get("sizes", {})
        thesis      = pi_data.get("thesis_scores", [])
        corr        = pi_data.get("correlation", {})
        forced      = pi_data.get("forced_exits", [])

        if sizes:
            lines += [
                f"  Capital utilization: ${sizes.get('current_exposure', 0):,.0f} "
                f"of ${sizes.get('max_exposure', 0):,.0f} cap "
                f"({sizes.get('exposure_pct', 0):.1f}%)",
                f"  Available for new positions: ${sizes.get('available_for_new', 0):,.0f}",
            ]

        if corr and corr.get("effective_bets") is not None:
            n_pos = len(pi_data.get("health_map", {}))
            lines.append(
                f"  Effective diversification: {corr['effective_bets']} independent bets "
                f"across {n_pos} positions"
            )

        if thesis:
            best  = max(thesis, key=lambda x: x["thesis_score"])
            worst = min(thesis, key=lambda x: x["thesis_score"])
            lines += [
                f"  Strongest thesis: {best['symbol']} ({best['thesis_score']}/10)",
                f"  Weakest thesis:   {worst['symbol']} ({worst['thesis_score']}/10)",
            ]

        health_map = pi_data.get("health_map", {})
        if health_map:
            biggest_dd = max(health_map.values(), key=lambda h: h.get("drawdown_pct", 0))
            lines.append(
                f"  Largest position drawdown: {biggest_dd['symbol']} "
                f"-{biggest_dd['drawdown_pct']:.1f}%  [{biggest_dd['health']}]"
            )

        if forced:
            for fe in forced:
                h = fe["health"]
                lines.append(
                    f"  *** FORCED EXIT PENDING: {fe['symbol']} "
                    f"drawdown={h['drawdown_pct']:.1f}% / "
                    f"account={h['account_pct']:.1f}% — half position ***"
                )

    try:
        from cost_tracker import get_tracker
        lines += ["", "  ── API COST TRACKER ────────────────────────────────────"]
        lines.append(get_tracker().format_report_section())
    except Exception:
        pass

    lines += ["", "╚" + "═" * 58 + "╝", ""]
    return "\n".join(lines)


# ── HTML email renderer ───────────────────────────────────────────────────────

def _cost_html_section() -> str:
    """Return an HTML snippet with today's API cost summary, or empty string."""
    try:
        from cost_tracker import get_tracker
        s  = get_tracker().get_daily_summary()
        ce = get_tracker().get_cache_efficiency()
        proj = get_tracker().get_monthly_projection()
        top_callers = sorted(s.get("by_caller", {}).items(),
                             key=lambda x: x[1]["cost"], reverse=True)[:5]
        caller_rows = "".join(
            f"<tr><td>{k}</td><td>{v['calls']}</td>"
            f"<td>${v['cost']:.4f}</td></tr>"
            for k, v in top_callers
        )
        return f"""<h3 style="color:#37474f">API Cost (Today)</h3>
<table style="width:100%;border-collapse:collapse;background:#fff;margin-bottom:20px;font-size:13px">
  <tr style="background:#eceff1">
    <td style="padding:8px">Daily Spend</td>
    <td style="padding:8px"><b>${s['daily_cost']:.4f}</b></td>
    <td style="padding:8px">API Calls</td>
    <td style="padding:8px"><b>{s['daily_calls']}</b></td></tr>
  <tr>
    <td style="padding:8px">Cache Hit Rate</td>
    <td style="padding:8px;color:#2e7d32"><b>{ce['hit_rate_pct']:.1f}%</b></td>
    <td style="padding:8px">Cache Savings</td>
    <td style="padding:8px;color:#2e7d32"><b>${ce['savings_usd']:.4f}</b></td></tr>
  <tr style="background:#eceff1">
    <td style="padding:8px">Monthly Proj.</td>
    <td style="padding:8px"><b>${proj:.2f}</b></td>
    <td style="padding:8px">All-Time</td>
    <td style="padding:8px"><b>${s['all_time_cost']:.2f}</b></td></tr>
</table>
{"<table border='1' cellpadding='6' style='border-collapse:collapse;width:100%;font-size:12px;margin-bottom:20px'><tr style='background:#cfd8dc'><th>Caller</th><th>Calls</th><th>Cost</th></tr>" + caller_rows + "</table>" if caller_rows else ""}"""
    except Exception:
        return ""


def format_html(r: dict) -> str:
    day_sign  = "+" if r["day_pl"] >= 0 else ""
    atm_sign  = "+" if r["all_time_pl"] >= 0 else ""
    day_color = "#2e7d32" if r["day_pl"] >= 0 else "#c62828"
    atm_color = "#2e7d32" if r["all_time_pl"] >= 0 else "#c62828"

    trade_rows = ""
    for t in r["trades"][:20]:
        pl_str  = f"{'+' if (t['pl'] or 0)>=0 else ''}${t['pl']:,.2f}" if t["pl"] is not None else "open"
        ex_str  = f"${t['exit']:,.2f}" if t["exit"] else "open"
        bg      = "#e8f5e9" if t["outcome"]=="win" else ("#ffebee" if t["outcome"]=="loss" else "#fff")
        trade_rows += (
            f"<tr style='background:{bg}'>"
            f"<td>{t['date']}</td><td>{t['symbol']}</td><td>{t['qty']:.2f}</td>"
            f"<td>${t['entry']:,.2f}</td><td>{ex_str}</td>"
            f"<td style='font-weight:bold'>{pl_str}</td><td>{t['outcome']}</td></tr>"
        )

    sym_rows = ""
    for sym, s in sorted(r["symbol_stats"].items(),
                         key=lambda x: x[1]["pl"], reverse=True):
        wr     = s["wins"] / s["trades"] * 100 if s["trades"] else 0
        pl_clr = "#2e7d32" if s["pl"] >= 0 else "#c62828"
        sym_rows += (
            f"<tr><td>{sym}</td><td>{s['trades']}</td><td>{s['wins']}</td>"
            f"<td>{s['losses']}</td><td>{wr:.0f}%</td>"
            f"<td style='color:{pl_clr};font-weight:bold'>"
            f"{'+' if s['pl']>=0 else ''}${s['pl']:,.2f}</td></tr>"
        )

    lessons_html = (f"<p style='color:#e65100'><b>⚠ Lessons:</b><br>{r['lessons'].replace(chr(10), '<br>')}</p>"
                    if r["lessons"] else "")

    # Portfolio Intelligence HTML section
    pi_data  = r.get("portfolio_intel", {})
    pi_html  = ""
    if pi_data:
        sizes    = pi_data.get("sizes", {})
        thesis   = pi_data.get("thesis_scores", [])
        corr     = pi_data.get("correlation", {})
        forced   = pi_data.get("forced_exits", [])
        hmap     = pi_data.get("health_map", {})

        forced_rows = "".join(
            f"<tr style='background:#ffebee'><td colspan='2'>"
            f"<b>*** FORCED EXIT: {fe['symbol']} "
            f"drawdown={fe['health']['drawdown_pct']:.1f}% / "
            f"account={fe['health']['account_pct']:.1f}%</b></td></tr>"
            for fe in forced
        )
        best_thesis  = max(thesis, key=lambda x: x["thesis_score"]) if thesis else None
        worst_thesis = min(thesis, key=lambda x: x["thesis_score"]) if thesis else None
        biggest_dd   = (max(hmap.values(), key=lambda h: h.get("drawdown_pct", 0))
                        if hmap else None)
        n_pos = len(hmap)

        pi_html = f"""<h3 style="color:#37474f">Portfolio Intelligence</h3>
<table style="width:100%;border-collapse:collapse;background:#fff;margin-bottom:20px;font-size:13px">
  <tr style="background:#eceff1">
    <td style="padding:8px">Capital Utilization</td>
    <td style="padding:8px"><b>${sizes.get('current_exposure', 0):,.0f}</b>
      of ${sizes.get('max_exposure', 0):,.0f} cap
      ({sizes.get('exposure_pct', 0):.1f}%)</td></tr>
  <tr>
    <td style="padding:8px">Available for New</td>
    <td style="padding:8px;color:#2e7d32"><b>${sizes.get('available_for_new', 0):,.0f}</b></td></tr>
  <tr style="background:#eceff1">
    <td style="padding:8px">Effective Diversification</td>
    <td style="padding:8px"><b>{corr.get('effective_bets', n_pos)}</b>
      independent bets across {n_pos} positions</td></tr>
  {"<tr><td style='padding:8px'>Strongest Thesis</td><td style='padding:8px;color:#2e7d32'><b>" + best_thesis['symbol'] + f" ({best_thesis['thesis_score']}/10)</b></td></tr>" if best_thesis else ""}
  {"<tr style='background:#eceff1'><td style='padding:8px'>Weakest Thesis</td><td style='padding:8px;color:#c62828'><b>" + worst_thesis['symbol'] + f" ({worst_thesis['thesis_score']}/10) — {worst_thesis['recommended_action']}</b></td></tr>" if worst_thesis else ""}
  {"<tr><td style='padding:8px'>Largest Drawdown</td><td style='padding:8px'><b>" + biggest_dd['symbol'] + f" -{biggest_dd['drawdown_pct']:.1f}%</b> [{biggest_dd['health']}]</td></tr>" if biggest_dd else ""}
  {forced_rows}
</table>"""

    return f"""
<html><body style="font-family:Arial,sans-serif;background:#f5f5f5;padding:24px;max-width:700px">
<h2 style="color:#1a237e;border-bottom:2px solid #1a237e;padding-bottom:8px">
  📈 Trading Bot Daily Report — {r['date']}</h2>

<table style="width:100%;border-collapse:collapse;background:#fff;border-radius:8px;
              box-shadow:0 1px 4px rgba(0,0,0,.1);margin-bottom:20px">
  <tr><td style="padding:12px 16px;color:#555">Portfolio Equity</td>
      <td style="padding:12px 16px;font-size:20px;font-weight:bold">${r['equity']:,.2f}</td></tr>
  <tr style="background:#fafafa">
      <td style="padding:12px 16px;color:#555">Cash</td>
      <td style="padding:12px 16px">${r['cash']:,.2f}</td></tr>
  <tr><td style="padding:12px 16px;color:#555">Day P&amp;L</td>
      <td style="padding:12px 16px;color:{day_color};font-weight:bold;font-size:18px">
        {day_sign}${r['day_pl']:,.2f}</td></tr>
  <tr style="background:#fafafa">
      <td style="padding:12px 16px;color:#555">All-Time P&amp;L</td>
      <td style="padding:12px 16px;color:{atm_color};font-weight:bold">
        {atm_sign}${r['all_time_pl']:,.2f}
        ({atm_sign}{r['all_time_pl']/STARTING_EQUITY*100:.2f}%)</td></tr>
</table>

<h3 style="color:#37474f">Trade Performance</h3>
<table style="width:100%;border-collapse:collapse;background:#fff;margin-bottom:20px">
  <tr style="background:#eceff1">
    <td style="padding:8px">Closed Trades</td><td style="padding:8px"><b>{r['closed_trades']}</b></td>
    <td style="padding:8px">Win Rate</td><td style="padding:8px"><b>{r['win_rate']:.1f}%</b></td></tr>
  <tr><td style="padding:8px">Avg Win</td><td style="padding:8px;color:#2e7d32"><b>${r['avg_win']:,.2f}</b></td>
      <td style="padding:8px">Avg Loss</td><td style="padding:8px;color:#c62828"><b>${r['avg_loss']:,.2f}</b></td></tr>
  <tr style="background:#eceff1">
    <td style="padding:8px">Profit Factor</td>
    <td style="padding:8px" colspan="3"><b>{r['profit_factor']:.2f}x</b></td></tr>
</table>

{'<h3 style="color:#37474f">Recent Trades</h3><table border="1" cellpadding="6" style="border-collapse:collapse;width:100%;font-size:13px"><tr style="background:#cfd8dc"><th>Date</th><th>Symbol</th><th>Qty</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Result</th></tr>' + trade_rows + '</table>' if r['trades'] else '<p style="color:#666">No closed trades yet.</p>'}

{'<h3 style="color:#37474f">Per-Symbol Breakdown</h3><table border="1" cellpadding="6" style="border-collapse:collapse;width:100%;font-size:13px"><tr style="background:#cfd8dc"><th>Symbol</th><th>Trades</th><th>Wins</th><th>Losses</th><th>Win%</th><th>P&L</th></tr>' + sym_rows + '</table>' if sym_rows else ''}

<h3 style="color:#37474f">Bot Activity Today</h3>
<p>Cycles: <b>{r['cycles_today']}</b> &nbsp;|&nbsp;
   Submitted: <b>{r['submitted_today']}</b> &nbsp;|&nbsp;
   Rejected: <b>{r['rejected_today']}</b></p>

{lessons_html}

{pi_html}

{_cost_html_section()}

<p style="color:#9e9e9e;font-size:11px;margin-top:24px">
  Generated by Trading Bot (paper account) · {r['date']}</p>
</body></html>"""


# ── SendGrid email ────────────────────────────────────────────────────────────

def send_report_email(target_date: date | None = None) -> None:
    api_key = os.getenv("SENDGRID_API_KEY")
    if not api_key or api_key.startswith("your_"):
        log.warning("SENDGRID_API_KEY not configured — skipping email")
        return

    r    = generate_report(target_date)
    html = format_html(r)
    subj = (f"Trading Bot {r['date']} — "
            f"P&L {'+' if r['day_pl']>=0 else ''}${r['day_pl']:,.0f}  |  "
            f"Equity ${r['equity']:,.0f}")
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail
        resp = SendGridAPIClient(api_key).send(
            Mail(from_email=FROM_EMAIL, to_emails=TO_EMAIL,
                 subject=subj, html_content=html)
        )
        log.info("Report email sent — status=%d  subject=%s", resp.status_code, subj)
    except Exception as exc:
        log.error("Report email failed: %s", exc, exc_info=True)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Trading bot performance dashboard")
    parser.add_argument("--print", action="store_true", dest="print_only",
                        help="Print to terminal only (no email)")
    args = parser.parse_args()

    r = generate_report()
    print(format_terminal(r))

    if not args.print_only:
        send_report_email()
