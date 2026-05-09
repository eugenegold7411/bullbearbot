#!/usr/bin/env python3
"""
symbol_reader.py

Reads and displays canonical symbol data from data/symbols/{SYM}.json.

Usage:
    python3 symbol_reader.py              # list all available symbols
    python3 symbol_reader.py CSCO         # show CSCO data
    python3 symbol_reader.py CSCO AMAT    # show multiple symbols
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT        = Path(__file__).parent
SYMBOLS_DIR = ROOT / "data" / "symbols"


def _age(ts: str | None) -> str:
    if not ts:
        return "unknown"
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        mins = int(delta.total_seconds() / 60)
        if mins < 60:
            return f"{mins}m ago"
        hours = mins // 60
        if hours < 24:
            return f"{hours}h ago"
        return f"{hours // 24}d ago"
    except Exception:
        return "unknown"


def _f2(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.2f}"
    except Exception:
        return str(v)


def _pct(v) -> str:
    if v is None:
        return "—"
    try:
        sign = "+" if float(v) > 0 else ""
        return f"{sign}{float(v):.1f}%"
    except Exception:
        return str(v)


def _val(v) -> str:
    return "—" if v is None else str(v)


def _safe_filename(symbol: str) -> str:
    return symbol.replace("/", "_").replace("\\", "_")


def show_symbol(symbol: str) -> None:
    safe = _safe_filename(symbol)
    path = SYMBOLS_DIR / f"{safe}.json"
    if not path.exists():
        print(f"No data for {symbol}. Run: python3 symbol_enrichment.py {symbol}")
        return

    d = json.loads(path.read_text())
    age = _age(d.get("last_enriched"))

    print(f"\n{'═'*62}")
    print(f"  {symbol}  ·  enriched {age}")
    print(f"{'═'*62}")

    # ── ANALYST TARGETS ──────────────────────────────────────────────
    t = d.get("analyst_targets") or {}
    print("\n── ANALYST TARGETS ────────────────────────────────────────────")
    if t.get("mean"):
        cp = t.get("current_price")
        print(f"  Mean target:    ${_f2(t.get('mean'))}  "
              f"(high ${_f2(t.get('high'))} / low ${_f2(t.get('low'))})")
        print(f"  Median:         ${_f2(t.get('median'))}")
        print(f"  Current price:  ${_f2(cp)}  upside {_pct(t.get('upside_pct'))}")
        print(f"  Analysts:       {_val(t.get('analyst_count'))}  "
              f"consensus={_val(t.get('recommendation_key'))}  "
              f"rec_mean={_f2(t.get('recommendation_mean'))}")
        sb = t.get("strong_buy"); b = t.get("buy")
        h  = t.get("hold");       s = t.get("sell"); ss = t.get("strong_sell")
        if sb is not None:
            print(f"  Distribution:   SB={sb}  B={b}  H={h}  S={s}  SS={ss}")
    else:
        print("  No analyst targets available")

    # ── FUNDAMENTALS ─────────────────────────────────────────────────
    yf = d.get("fundamentals_yf") or {}
    av = d.get("fundamentals_av") or {}
    print("\n── FUNDAMENTALS ───────────────────────────────────────────────")
    print(f"  Sector:         {_val(yf.get('sector'))}")
    print(f"  Industry:       {_val(yf.get('industry'))}")
    mc = yf.get("market_cap_b")
    if mc:
        print(f"  Market cap:     ${mc:.1f}B")
    # prefer yfinance forward_pe; fall back to AV
    fpe = yf.get("forward_pe") or av.get("forward_pe")
    peg = yf.get("peg_ratio") or av.get("peg_ratio")
    pts = yf.get("price_to_sales") or av.get("price_to_sales")
    eps = yf.get("trailing_eps") or av.get("eps")
    print(f"  Forward P/E:    {_f2(fpe)}")
    print(f"  Trailing P/E:   {_f2(yf.get('trailing_pe'))}")
    print(f"  PEG ratio:      {_f2(peg)}")
    print(f"  Price/Sales:    {_f2(pts)}")
    print(f"  EPS (TTM):      {_f2(eps)}")
    print(f"  Beta:           {_f2(yf.get('beta'))}")
    w52h = yf.get("week_52_high") or av.get("week_52_high")
    w52l = yf.get("week_52_low")  or av.get("week_52_low")
    if w52h and w52l:
        print(f"  52w range:      ${_f2(w52h)} / ${_f2(w52l)}")

    # ── IV RANK ──────────────────────────────────────────────────────
    iv = d.get("iv") or {}
    print("\n── IV RANK ────────────────────────────────────────────────────")
    if iv.get("rank") is not None:
        rank = iv["rank"]
        label = ("very cheap" if rank < 20 else
                 "cheap"      if rank < 40 else
                 "neutral"    if rank < 60 else
                 "expensive"  if rank < 80 else
                 "very expensive")
        print(f"  IV rank:        {rank:.1f}  ({label})")
        print(f"  Current IV:     {iv['current']:.4f}")
    else:
        print("  No IV history available")

    # ── EARNINGS CONVICTION ──────────────────────────────────────────
    ec  = d.get("earnings_conviction")
    ai  = d.get("analyst_intel")
    print("\n── EARNINGS CONVICTION ────────────────────────────────────────")
    if ec:
        print(f"  Conviction:     {_val(ec.get('conviction_score'))} "
              f"({_val(ec.get('conviction_level'))})")
        print(f"  EDA:            {_val(ec.get('eda'))} days  "
              f"timing={_val(ec.get('timing'))}")
        print(f"  Direction:      {_val(ec.get('direction'))}  "
              f"confidence={_val(ec.get('direction_confidence'))}")
        print(f"  Beat rate:      {_val(ec.get('beat_rate'))}  "
              f"avg surprise={_val(ec.get('avg_eps_surprise'))}%")
        print(f"  Rec structure:  {_val(ec.get('recommended_structure'))}")
        print(f"  Phase:          {_val(ec.get('phase'))}")
    elif ai:
        print(f"  (from analyst_intel — not in active earnings rotation)")
        print(f"  Beat rate:      {_val(ai.get('beat_rate'))}  "
              f"({_val(ai.get('beat_quarters'))}/{_val(ai.get('total_quarters'))} qtrs)")
        print(f"  Avg surprise:   {_val(ai.get('avg_eps_surprise'))}%")
        print(f"  Analyst count:  {_val(ai.get('analyst_count'))}")
        print(f"  Bullish pct:    {_val(ai.get('bullish_pct'))}%")
    else:
        print("  Not in earnings rotation — no conviction data")

    # ── BEAT HISTORY ─────────────────────────────────────────────────
    beats = d.get("beat_history") or []
    if beats:
        print("\n── BEAT HISTORY (last 4 quarters) ─────────────────────────────")
        for b in beats[:4]:
            surprise = b.get("surprise_pct")
            surprise_str = f"{surprise:+.1f}%" if surprise is not None else "n/a"
            print(f"  {b.get('quarter_end_date')}: "
                  f"actual={_f2(b.get('eps_actual'))}  "
                  f"est={_f2(b.get('eps_estimate'))}  "
                  f"surprise={surprise_str}")

    # ── EDGAR TRANSCRIPT ─────────────────────────────────────────────
    transcript = d.get("edgar_transcript")
    if transcript:
        print("\n── EDGAR 8-K TRANSCRIPT ────────────────────────────────────────")
        print(f"  Filing:   {transcript.get('filing_date')}  (CIK {transcript.get('cik')})")
        print(f"  URL:      {transcript.get('full_url', '')[:80]}")
        excerpt = (transcript.get("transcript_excerpt") or "")[:120]
        print(f"  Excerpt:  {excerpt}...")

    # ── INSIDER ACTIVITY ─────────────────────────────────────────────
    insider = d.get("insider_trades_30d") or []
    congress = d.get("congress_trades_30d") or []
    if insider or congress:
        print("\n── INSIDER ACTIVITY (30 days) ──────────────────────────────────")
        print(f"  Form 4 trades:    {len(insider)}")
        print(f"  Congress trades:  {len(congress)}")
        for tr in insider[:3]:
            print(f"    [{tr.get('date')}] {tr.get('insider_name')}: "
                  f"{tr.get('action')} {tr.get('shares')} @ ${tr.get('price')}")
        for tr in congress[:3]:
            print(f"    [{tr.get('date')}] {tr.get('politician')} ({tr.get('party')}): "
                  f"{tr.get('action')} {tr.get('amount_range')}")

    # ── RECENT NEWS ──────────────────────────────────────────────────
    news = d.get("news_yahoo") or []
    print("\n── RECENT NEWS (Yahoo RSS) ────────────────────────────────────")
    if news:
        for item in news[:5]:
            pub = (item.get("published") or "")[:16]
            hl  = (item.get("headline") or "")[:70]
            print(f"  [{pub}] {hl}")
    else:
        print("  No news available")

    alpaca_news = d.get("news_alpaca") or []
    if alpaca_news:
        print("\n── RECENT NEWS (Alpaca — single-symbol ★) ─────────────────────")
        single = [a for a in alpaca_news if a.get("single_symbol")]
        shown  = (single or alpaca_news)[:5]
        for item in shown:
            pub  = (item.get("published") or "")[:16]
            tag  = "★" if item.get("single_symbol") else " "
            hl   = (item.get("headline") or "")[:66]
            print(f"  {tag}[{pub}] {hl}")

    print()


def list_symbols() -> None:
    if not SYMBOLS_DIR.exists():
        print("data/symbols/ not found. Run symbol_enrichment.py first.")
        return
    files = sorted(SYMBOLS_DIR.glob("*.json"))
    if not files:
        print("No symbol files found.")
        return
    print(f"\nAvailable symbols ({len(files)} total):")
    row: list[str] = []
    for f in files:
        row.append(f.stem)
        if len(row) == 10:
            print("  " + "  ".join(f"{s:<6}" for s in row))
            row = []
    if row:
        print("  " + "  ".join(f"{s:<6}" for s in row))
    print()


def main() -> None:
    if len(sys.argv) < 2:
        list_symbols()
        return
    for sym in sys.argv[1:]:
        show_symbol(sym.upper())


if __name__ == "__main__":
    main()
