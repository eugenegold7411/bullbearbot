"""
intraday_cache.py — 5-minute bar cache with disk persistence.

Fetches 5-min bars from Alpaca (DataFeed.IEX), appends incrementally each
call, persists to data/bars/{symbol}_intraday_{date}.csv, and provides
RSI/MACD/momentum/vol_ratio summaries used in market_data.py signal lines.

Auto-rotates CSVs older than 5 trading days on first session use.
Recovers from restarts by loading today's existing CSV before Alpaca fetch.
"""

import csv
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from log_setup import get_logger

log = get_logger(__name__)
ET = ZoneInfo("America/New_York")

_BAR_DIR: Path = Path(__file__).parent / "data" / "bars"
_CACHE:   dict[str, list[dict]] = {}   # symbol → list of 5-min bar dicts
_ROTATED: bool = False                  # True after first rotation this session
_data_client = None                     # lazy StockHistoricalDataClient


# ── Alpaca client (lazy, self-contained) ──────────────────────────────────────

def _get_data_client():
    global _data_client
    if _data_client is None:
        from alpaca.data.historical import StockHistoricalDataClient
        from dotenv import load_dotenv
        load_dotenv()
        _data_client = StockHistoricalDataClient(
            os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
        )
    return _data_client


# ── Disk helpers ──────────────────────────────────────────────────────────────

def _bar_path(symbol: str, date_str: str) -> Path:
    safe = symbol.replace("/", "-").replace("*", "")
    return _BAR_DIR / f"{safe}_intraday_{date_str}.csv"


def _rotate_old_csvs() -> None:
    """Delete CSVs for dates older than 8 calendar days (~5 trading days)."""
    global _ROTATED
    if _ROTATED:
        return
    _ROTATED = True
    _BAR_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now(ET) - timedelta(days=8)
    for f in _BAR_DIR.glob("*_intraday_*.csv"):
        try:
            date_str = f.stem.split("_intraday_")[-1]
            file_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=ET)
            if file_date < cutoff:
                f.unlink()
                log.debug("Rotated old intraday CSV: %s", f.name)
        except Exception:
            pass


def _load_csv(symbol: str, date_str: str) -> list[dict] | None:
    p = _bar_path(symbol, date_str)
    if not p.exists():
        return None
    try:
        with p.open() as fh:
            rows = list(csv.DictReader(fh))
        return [
            {"t": r["t"], "o": float(r["o"]), "h": float(r["h"]),
             "l": float(r["l"]), "c": float(r["c"]), "v": float(r["v"])}
            for r in rows
        ] or None
    except Exception as exc:
        log.debug("Failed to load intraday CSV %s: %s", p, exc)
        return None


def _save_csv(symbol: str, date_str: str, bars: list[dict]) -> None:
    _BAR_DIR.mkdir(parents=True, exist_ok=True)
    p = _bar_path(symbol, date_str)
    try:
        with p.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["t", "o", "h", "l", "c", "v"])
            writer.writeheader()
            writer.writerows(bars)
    except Exception as exc:
        log.debug("Failed to save intraday CSV %s: %s", p, exc)


# ── Bar fetch ─────────────────────────────────────────────────────────────────

def get_intraday_bars(symbol: str, client=None) -> list[dict]:
    """
    Return today's 5-min bars for symbol, using in-memory → disk → Alpaca
    in that order. Incrementally appends new bars each call.

    client: optional StockHistoricalDataClient; uses internal lazy instance
            if not provided.
    """
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.data.enums import DataFeed

    _rotate_old_csvs()

    now_et  = datetime.now(ET)
    today   = now_et.strftime("%Y-%m-%d")
    now_utc = datetime.now(timezone.utc)

    # In-memory cache hit
    if symbol in _CACHE and _CACHE[symbol]:
        cached = _CACHE[symbol]
    else:
        # Recover from disk
        cached = _load_csv(symbol, today) or []
        _CACHE[symbol] = cached

    # Determine incremental fetch window
    session_start = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    session_start_utc = session_start.astimezone(timezone.utc)

    if cached:
        last_t_str = cached[-1]["t"]
        # Handle both "Z" and "+00:00" ISO formats
        last_t = datetime.fromisoformat(last_t_str.replace("Z", "+00:00"))
        fetch_start = last_t + timedelta(minutes=5)
    else:
        fetch_start = session_start_utc

    # Skip if already up to date (within one bar period)
    if fetch_start >= now_utc - timedelta(minutes=4):
        return cached

    # Fetch from Alpaca
    _client = client or _get_data_client()
    try:
        resp = _client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=fetch_start,
            end=now_utc,
            feed=DataFeed.IEX,
        ))
        try:
            raw_bars = resp[symbol]
        except (KeyError, TypeError):
            raw_bars = []

        new_bars = []
        for bar in raw_bars:
            ts = bar.timestamp
            t_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
            new_bars.append({
                "t": t_str,
                "o": float(bar.open),
                "h": float(bar.high),
                "l": float(bar.low),
                "c": float(bar.close),
                "v": float(bar.volume),
            })

        if new_bars:
            existing_ts = {b["t"] for b in cached}
            for b in new_bars:
                if b["t"] not in existing_ts:
                    cached.append(b)
            cached.sort(key=lambda b: b["t"])
            _CACHE[symbol] = cached
            _save_csv(symbol, today, cached)
            log.debug("Intraday cache [%s]: +%d bars (%d total)", symbol, len(new_bars), len(cached))

    except Exception as exc:
        log.debug("Intraday bar fetch failed for %s: %s", symbol, exc)

    return cached


# ── Indicator math ────────────────────────────────────────────────────────────

def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    return round(100 - 100 / (1 + avg_g / avg_l), 1) if avg_l else 100.0


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k, result = 2 / (period + 1), [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


# ── Summary ───────────────────────────────────────────────────────────────────

def get_intraday_summary(symbol: str, client=None) -> dict:
    """
    Return computed intraday metrics from 5-min bars:
      rsi           — RSI(14) from 5-min closes
      macd          — MACD(12,26,9) line value
      macd_signal   — MACD signal line value
      momentum_5bar — % change over last 5 bars (25 min)
      vol_ratio     — last bar volume / session average
      vwap          — session VWAP from 5-min bars
      bar_count     — number of bars available today
      last_bar      — ISO timestamp of most recent bar

    All numeric fields are None when insufficient data.
    """
    bars = get_intraday_bars(symbol, client)
    empty = {"rsi": None, "macd": None, "macd_signal": None,
             "momentum_5bar": None, "vol_ratio": None, "vwap": None,
             "bar_count": 0, "last_bar": None}
    if not bars:
        return empty

    closes  = [b["c"] for b in bars]
    volumes = [b["v"] for b in bars]
    highs   = [b["h"] for b in bars]
    lows    = [b["l"] for b in bars]

    # RSI(14) — needs 15+ bars (~75 min)
    rsi = _rsi(closes)

    # MACD(12,26,9) — needs 35+ bars (~2h55m)
    macd_val = macd_sig = None
    if len(closes) >= 35:
        ema12 = _ema(closes, 12)
        ema26 = _ema(closes, 26)
        macd_line = [a - b for a, b in zip(ema12, ema26)]
        if len(macd_line) >= 9:
            sig_line = _ema(macd_line, 9)
            macd_val = round(macd_line[-1], 4)
            macd_sig = round(sig_line[-1], 4)

    # 5-bar momentum (last ~25 min)
    momentum = None
    if len(closes) >= 6:
        momentum = round((closes[-1] - closes[-6]) / closes[-6] * 100, 2)

    # Volume ratio: last bar vs session average
    vol_ratio = None
    if len(volumes) >= 2:
        avg_v = sum(volumes[:-1]) / len(volumes[:-1])
        if avg_v > 0:
            vol_ratio = round(volumes[-1] / avg_v, 2)

    # Session VWAP from 5-min bars
    vwap = None
    total_pv = sum((h + l + c) / 3 * v
                   for h, l, c, v in zip(highs, lows, closes, volumes))
    total_v = sum(volumes)
    if total_v > 0:
        vwap = round(total_pv / total_v, 2)

    return {
        "rsi":          rsi,
        "macd":         macd_val,
        "macd_signal":  macd_sig,
        "momentum_5bar": momentum,
        "vol_ratio":    vol_ratio,
        "vwap":         vwap,
        "bar_count":    len(bars),
        "last_bar":     bars[-1]["t"],
    }


def build_intraday_momentum_section(symbols: list[str], current_prices: dict,
                                    client=None) -> str:
    """
    Build a formatted `=== INTRADAY MOMENTUM ===` string for the given symbols.
    Called from bot.py run_cycle(). Returns "(not in market session)" outside
    9:30–8:00 PM ET, "(insufficient data)" during the first ~75 min of session.
    """
    now_et  = datetime.now(ET)
    now_min = now_et.hour * 60 + now_et.minute
    if not (9 * 60 + 30 <= now_min < 20 * 60):
        return "  (not in market session)"

    lines = []
    for sym in symbols[:20]:
        try:
            sm = get_intraday_summary(sym, client)
            if sm["bar_count"] < 3:
                continue
            price = current_prices.get(sym)
            price_str = f"${price:.2f}" if price else ""

            rsi_str  = f"RSI={sm['rsi']:.1f}"  if sm["rsi"]  is not None else "RSI=?"
            macd_str = (f"MACD={sm['macd']:+.3f}/sig={sm['macd_signal']:+.3f}"
                        if sm["macd"] is not None else "")
            mom_str  = (f"mom={sm['momentum_5bar']:+.1f}%"
                        if sm["momentum_5bar"] is not None else "")
            vol_str  = f"vol={sm['vol_ratio']:.1f}x" if sm["vol_ratio"] is not None else ""
            vwap_str = ""
            if sm["vwap"] and price:
                direction = "ABOVE" if price > sm["vwap"] else "BELOW"
                vwap_str  = f"{direction} VWAP(${sm['vwap']:.2f})"
            bars_str = f"[{sm['bar_count']}bars]"

            parts = [p for p in [price_str, rsi_str, macd_str, mom_str, vol_str, vwap_str, bars_str] if p]
            lines.append(f"  {sym:<6}  {'  '.join(parts)}")
        except Exception:
            continue

    return "\n".join(lines) if lines else "  (insufficient intraday data)"
