"""
market_data.py — session-aware market data for the trading bot.

Reads from data_warehouse cache when fresh (< 26h).
Always fetches live: price, equity, VIX, breaking news (last 15 min).
Builds all prompt sections: sector table, inter-market signals,
earnings calendar, core/dynamic/intraday watchlist strings.

Technical indicators (via pandas-ta):
  RSI(14), MACD(12/26/9), MA20, MA50, Volume ratio, VWAP (intraday)
"""

# ============================================================
# MARKET DATA SECTION INVENTORY
# ============================================================
# REQUIRED sections (compact + full prompt):
#   - get_market_clock()       — {time_et}, {session_tier}
#   - get_vix() / vix_regime() — {vix}, {vix_label}
#   - get_stock_signals()      — feeds signal scorer → {top_signals_block}
#   - get_crypto_signals()     — crypto prices always tracked
#
# OPTIONAL sections (full prompt only, compact skips):
#   - build_crypto_context_section() — crypto F&G / ETH-BTC narrative
#   - get_news()               — breaking_news, sector_news
#   - _build_sector_table()    — sector performance table
#   - _build_intermarket_signals() — oil/gold/dollar signals
#   - _build_earnings_calendar()  — upcoming earnings
#   - _build_global_session_handoff() — Asia/Europe/US futures
#   - _build_core_by_sector()  — watchlist grouped by sector
#   - _build_dynamic_section() — scanner adds
#   - _build_intraday_section() — intraday live adds
#   - build_economic_calendar_section() — Finnhub calendar
#   - build_orb_section()      — ORB candidates
#   - insider_section          — insider intelligence (inline in fetch_all)
#   - morning_brief_section    — morning brief (inline in fetch_all)
#   - reddit_section           — Reddit sentiment (inline in fetch_all)
#   - earnings_intel_section   — earnings intel (inline in fetch_all)
#   - macro_wire_section       — Reuters/AP macro wire (inline in fetch_all)
#
# ENRICHMENT sections (analytics, not prompt-injected directly):
#   - compute_eth_btc_ratio()  — ETH/BTC ratio (consumed by get_crypto_signals)
#   - test_crypto_prices()     — CLI test only
# ============================================================

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from alpaca.data.enums import DataFeed
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import (
    CryptoBarsRequest,
    CryptoLatestTradeRequest,
    NewsRequest,
    StockBarsRequest,
    StockLatestTradeRequest,
)
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from dotenv import load_dotenv

import data_warehouse as dw
import watchlist_manager as wm
from log_setup import get_logger

load_dotenv()

log = get_logger(__name__)
ET  = ZoneInfo("America/New_York")

_trading: TradingClient | None = None
_data:    StockHistoricalDataClient | None = None
_crypto:  CryptoHistoricalDataClient | None = None
_news:    NewsClient | None = None


def _build_trading_client() -> TradingClient:
    key    = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise EnvironmentError(
            "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set to use market_data"
        )
    return TradingClient(key, secret, paper=True)


def _get_trading_client() -> TradingClient:
    global _trading
    if _trading is None:
        _trading = _build_trading_client()
    return _trading


def _build_data_client() -> StockHistoricalDataClient:
    key    = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise EnvironmentError(
            "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set to use market_data"
        )
    return StockHistoricalDataClient(key, secret)


def _get_data_client() -> StockHistoricalDataClient:
    global _data
    if _data is None:
        _data = _build_data_client()
    return _data


def _build_crypto_client() -> CryptoHistoricalDataClient:
    key    = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise EnvironmentError(
            "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set to use market_data"
        )
    return CryptoHistoricalDataClient(key, secret)


def _get_crypto_client() -> CryptoHistoricalDataClient:
    global _crypto
    if _crypto is None:
        _crypto = _build_crypto_client()
    return _crypto


def _build_news_client() -> NewsClient:
    key    = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise EnvironmentError(
            "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set to use market_data"
        )
    return NewsClient(key, secret)


def _get_news_client() -> NewsClient:
    global _news
    if _news is None:
        _news = _build_news_client()
    return _news


# ── Market clock ─────────────────────────────────────────────────────────────

def get_market_clock() -> dict:
    """
    Fetch market clock state from Alpaca.

    Section type: REQUIRED
    Compact prompt: YES ({time_et}, {session_tier})
    Fallback: {"is_open": False, "status": "unknown", "time_et": "?",
               "session_tier": "unknown", "minutes_since_open": -1}
    """
    try:
        clock  = _get_trading_client().get_clock()
        now_et = datetime.now(ET)
        if clock.is_open:
            today_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
            minutes_since_open = max(0, int((now_et - today_open).total_seconds() / 60))
        else:
            minutes_since_open = -1
        return {
            "is_open":            clock.is_open,
            "status":             "open" if clock.is_open else "closed",
            "time_et":            now_et.strftime("%I:%M %p ET"),
            "minutes_since_open": minutes_since_open,
        }
    except Exception as exc:
        log.warning("get_market_clock failed: %s", exc)
        return {
            "is_open":            False,
            "status":             "unknown",
            "time_et":            "?",
            "session_tier":       "unknown",
            "minutes_since_open": -1,
        }


# ── VIX ──────────────────────────────────────────────────────────────────────

def get_vix() -> float:
    """
    Section type: REQUIRED
    Compact prompt: YES ({vix}, {vix_label})
    Fallback: returns 0.0 on exception (already implemented)
    """
    try:
        hist = yf.Ticker("^VIX").history(period="2d")
        if not hist.empty:
            return round(float(hist["Close"].iloc[-1]), 2)
    except Exception:
        pass
    return 0.0


def vix_regime(vix: float) -> tuple[str, str]:
    """Returns (regime_label, instruction)."""
    if vix > 35:
        return "CRISIS", "Go to cash immediately. No new positions. Halt all trading. SMS alert."
    if vix > 25:
        return "ELEVATED", "Cut all position sizes by 50%. No options. Defensive only."
    if vix < 15:
        return "CALM", "Full position sizes. All strategies available."
    return "NORMAL", "Standard rules apply."


# ── Technical indicators ─────────────────────────────────────────────────────

def _ema(closes: list, period: int):
    """Compute EMA for a list of close prices. Returns None on insufficient data."""
    try:
        if not closes or len(closes) < period:
            return None
        # Filter out NaN/zero values
        valid = [c for c in closes if c and c == c]  # NaN check
        if len(valid) < period:
            return None
        multiplier = 2.0 / (period + 1)
        ema = float(valid[0])
        for price in valid[1:]:
            ema = (float(price) - ema) * multiplier + ema
        return round(ema, 2)
    except Exception:
        return None


def _ema9_cross(closes: list) -> str:
    """Detect if EMA9 crossed EMA21 in the last 3 bars. Returns 'bullish', 'bearish', or 'none'."""
    try:
        if len(closes) < 24:
            return "none"
        # Check each of the last 3 bars
        for i in range(-3, 0):
            window_cur  = closes[:len(closes)+i+1] if i < -1 else closes
            window_prev = closes[:len(closes)+i]
            e9_cur  = _ema(window_cur,  9)
            e21_cur = _ema(window_cur,  21)
            e9_prev = _ema(window_prev, 9)
            e21_prev= _ema(window_prev, 21)
            if None in (e9_cur, e21_cur, e9_prev, e21_prev):
                continue
            # Bullish cross: was below, now above
            if e9_prev <= e21_prev and e9_cur > e21_cur:
                return "bullish"
            # Bearish cross: was above, now below
            if e9_prev >= e21_prev and e9_cur < e21_cur:
                return "bearish"
        return "none"
    except Exception:
        return "none"


def _compute_indicators(bars_list) -> dict:
    """Compute RSI, MACD, MA20, MA50, volume ratio from bar list or list of dicts."""
    if len(bars_list) < 27:
        return {}

    # Accept both Alpaca bar objects and dicts (from cache)
    if isinstance(bars_list[0], dict):
        df = pd.DataFrame(bars_list).rename(columns={"close": "close", "volume": "volume"})
        df = df.astype({"open": float, "high": float, "low": float,
                        "close": float, "volume": float})
    else:
        df = pd.DataFrame([{
            "open": b.open, "high": b.high,
            "low":  b.low,  "close": b.close,
            "volume": b.volume,
        } for b in bars_list]).astype(float)

    closes = df["close"]
    result: dict = {}

    try:
        rsi_s = df.ta.rsi(length=14)
        result["rsi"] = round(float(rsi_s.iloc[-1]), 1) if rsi_s is not None and not rsi_s.empty else None
    except Exception:
        result["rsi"] = None

    try:
        macd_df  = df.ta.macd(fast=12, slow=26, signal=9)
        if macd_df is not None and not macd_df.empty:
            macd_col = next((c for c in macd_df.columns if c.upper().startswith("MACD_")),  None)
            sig_col  = next((c for c in macd_df.columns if c.upper().startswith("MACDS_")), None)
            result["macd"]        = round(float(macd_df[macd_col].iloc[-1]), 3) if macd_col else None
            result["macd_signal"] = round(float(macd_df[sig_col].iloc[-1]),  3) if sig_col  else None
        else:
            result["macd"] = result["macd_signal"] = None
    except Exception:
        result["macd"] = result["macd_signal"] = None

    result["ma20"] = round(float(closes.tail(20).mean()), 2) if len(closes) >= 20 else None
    result["ma50"] = round(float(closes.tail(50).mean()), 2) if len(closes) >= 50 else None

    try:
        vol_ma = float(df["volume"].tail(20).mean())
        result["vol_ratio"] = round(float(df["volume"].iloc[-1]) / vol_ma, 2) if vol_ma > 0 else None
    except Exception:
        result["vol_ratio"] = None

    result["price"]   = round(float(closes.iloc[-1]), 2)
    result["prev"]    = round(float(closes.iloc[-2]), 2) if len(closes) >= 2 else result["price"]
    result["day_chg"] = round((result["price"] - result["prev"]) / result["prev"] * 100, 2) \
                        if result.get("prev") else 0.0

    # EMA9 / EMA21
    closes_list = closes.tolist()
    result["ema9"]  = _ema(closes_list, 9)
    result["ema21"] = _ema(closes_list, 21)
    if result["ema9"] is not None and result["ema21"] is not None:
        result["ema9_above_ema21"] = result["ema9"] > result["ema21"]
        result["price_above_ema9"] = result["price"] > result["ema9"]
    else:
        result["ema9_above_ema21"] = None
        result["price_above_ema9"] = None
    result["ema9_cross"] = _ema9_cross(closes_list)

    return result

def _compute_vwap_intraday(symbols: list, session_start_utc: datetime) -> dict:
    now_utc = datetime.now(timezone.utc)
    try:
        bars = _get_data_client().get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Hour,
            start=session_start_utc,
            end=now_utc,
            feed=DataFeed.IEX,
        ))
    except Exception:
        return {}

    result = {}
    for sym in symbols:
        try:
            b = bars[sym]
            if not b:
                result[sym] = None
                continue
            total_pv = sum((bar.high + bar.low + bar.close) / 3 * bar.volume for bar in b)
            total_v  = sum(bar.volume for bar in b)
            result[sym] = round(total_pv / total_v, 2) if total_v > 0 else None
        except Exception:
            result[sym] = None
    return result


def _compute_crypto_vwap_24h(symbols: list) -> dict:
    start = datetime.now(timezone.utc) - timedelta(hours=24)
    try:
        bars = _get_crypto_client().get_crypto_bars(CryptoBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Hour,
            start=start,
        ))
    except Exception:
        return {}

    result = {}
    for sym in symbols:
        try:
            b = _crypto_bars_lookup(bars, sym)
            if not b:
                result[sym] = None
                continue
            total_pv = sum((bar.high + bar.low + bar.close) / 3 * bar.volume for bar in b)
            total_v  = sum(bar.volume for bar in b)
            result[sym] = round(total_pv / total_v, 2) if total_v > 0 else None
        except Exception:
            result[sym] = None
    return result


# ── Stock signals ─────────────────────────────────────────────────────────────

def get_stock_signals(
    symbols: list,
    use_cache: bool = True,
) -> tuple[str, dict, dict, dict]:
    """Returns (formatted_string, current_prices_dict).

    Section type: REQUIRED
    Compact prompt: YES (feeds signal scorer → {top_signals_block})
    Fallback: returns ("  (no stock data)", {}) on exception (already implemented)
    """
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=90)

    # Try cache first
    cached_bars: dict[str, list] = {}
    live_needed: list[str] = []

    if use_cache:
        for sym in symbols:
            cached = dw.load_bars_cached(sym)
            if cached:
                cached_bars[sym] = cached
            else:
                live_needed.append(sym)
    else:
        live_needed = list(symbols)

    # Fetch what's not cached
    live_bars: dict[str, list] = {}
    if live_needed:
        try:
            resp = _get_data_client().get_stock_bars(StockBarsRequest(
                symbol_or_symbols=live_needed,
                timeframe=TimeFrame.Day,
                start=start, end=end,
                feed=DataFeed.IEX,
            ))
            for sym in live_needed:
                try:
                    live_bars[sym] = resp[sym]
                except Exception:
                    pass
        except Exception as exc:
            log.warning("Stock bars fetch error: %s", exc)

    # Latest live prices
    try:
        latest_resp = _get_data_client().get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=symbols, feed=DataFeed.IEX)
        )
    except Exception:
        latest_resp = {}

    # Intraday 5-min summaries (VWAP + RSI/MACD/momentum) from intraday_cache
    intraday_summaries: dict = {}
    try:
        import intraday_cache as _ic
        _now_et = datetime.now(ET)
        if _now_et.hour * 60 + _now_et.minute >= 9 * 60 + 30:
            for _sym in symbols:
                try:
                    intraday_summaries[_sym] = _ic.get_intraday_summary(_sym, _data)
                except Exception:
                    pass
    except Exception as _ic_exc:
        log.debug("intraday_cache unavailable: %s", _ic_exc)

    lines           = []
    current_prices  = {}
    ind_by_symbol: dict[str, dict]   = {}
    # intraday_summaries already built above — retained in an outer var; re-attached below.

    for sym in symbols:
        try:
            bars_raw = live_bars.get(sym) or cached_bars.get(sym)
            if not bars_raw:
                continue

            ind = _compute_indicators(bars_raw)
            if not ind:
                continue

            price = float(latest_resp[sym].price) if sym in latest_resp else ind["price"]
            current_prices[sym] = price
            # Retain live price inside the indicator dict so downstream consumers
            # don't need to re-fetch / re-join against current_prices.
            ind_with_price = dict(ind)
            ind_with_price["price"] = price
            ind_by_symbol[sym] = ind_with_price

            ma20 = ind.get("ma20")
            ma50 = ind.get("ma50")
            rsi  = ind.get("rsi")
            macd = ind.get("macd")
            msig = ind.get("macd_signal")
            vrat = ind.get("vol_ratio")
            dchg = (price - ind["prev"]) / ind["prev"] * 100 if ind.get("prev") else ind.get("day_chg", 0)
            _id_sum = intraday_summaries.get(sym, {})
            vwap = _id_sum.get("vwap")

            ma20_trend = ("ABOVE" if price > ma20 else "BELOW") if ma20 else "?"
            ma50_trend = ("ABOVE" if price > ma50 else "BELOW") if ma50 else "?"
            vs_ma20    = (price - ma20) / ma20 * 100 if ma20 else 0
            vs_ma50    = (price - ma50) / ma50 * 100 if ma50 else 0
            vwap_str   = (("ABOVE" if price > vwap else "BELOW") + f" VWAP(${vwap:.2f})") if vwap else "VWAP=N/A"
            d_sign     = "+" if dchg >= 0 else ""

            ema9  = ind.get("ema9")
            ema21 = ind.get("ema21")
            ecross= ind.get("ema9_cross", "none")
            ema9_str = f"EMA9={ema9:.2f}({'ABOVE' if ind.get('price_above_ema9') else 'BELOW'})" if ema9 else "EMA9=?"
            ema21_str= f"EMA21={ema21:.2f}" if ema21 else "EMA21=?"
            cross_str= f"Cross={ecross}"

            lines.append(
                f"  {sym:<6}  ${price:.2f}  day {d_sign}{dchg:.1f}%  "
                f"MA20={ma20_trend}(${ma20:.2f},{'+' if vs_ma20>=0 else ''}{vs_ma20:.1f}%)  "
                f"MA50={ma50_trend}(${ma50:.2f},{'+' if vs_ma50>=0 else ''}{vs_ma50:.1f}%)"
            )
            rsi_str  = f"RSI={rsi:.1f}" if rsi is not None else "RSI=?"
            macd_str = (f"MACD={macd:+.2f}/sig={msig:+.2f}"
                        if macd is not None and msig is not None else "MACD=?")
            # d1_vol = prior-day close volume vs 20-day daily average (NOT intraday)
            vol_str  = f"d1_vol={vrat:.1f}x vs 20d" if vrat is not None else "d1_vol=?"
            lines.append(f"  {'':6}  {rsi_str}  {macd_str}  {vol_str}  {vwap_str}")
            lines.append(f"  {'':6}  {ema9_str}  {ema21_str}  {cross_str}")

            # 5-min intraday metrics line (from intraday_cache)
            if _id_sum and _id_sum.get("bar_count", 0) >= 3:
                _id_rsi  = (f"5m-RSI={_id_sum['rsi']:.1f}"
                            if _id_sum.get("rsi") is not None else "")
                _id_macd = (f"5m-MACD={_id_sum['macd']:+.3f}/sig={_id_sum['macd_signal']:+.3f}"
                            if _id_sum.get("macd") is not None else "")
                _id_mom  = (f"mom={_id_sum['momentum_5bar']:+.1f}%"
                            if _id_sum.get("momentum_5bar") is not None else "")
                _id_vol  = (f"ivol={_id_sum['vol_ratio']:.1f}x"
                            if _id_sum.get("vol_ratio") is not None else "")
                _id_parts = [p for p in [_id_rsi, _id_macd, _id_mom, _id_vol] if p]
                if _id_parts:
                    lines.append(f"  {'':6}  {' '.join(_id_parts)}")
            else:
                # Explicit no-live-data state so Sonnet doesn't infer from d1_vol
                lines.append(f"  {'':6}  ivol=N/A(no live data)")

        except Exception:
            continue

    return (
        ("\n".join(lines) if lines else "  (no stock data)"),
        current_prices,
        ind_by_symbol,
        intraday_summaries,
    )


# ── Crypto helpers ────────────────────────────────────────────────────────────

# Defensive: tries both BTC/USD and BTCUSD formats due to symbol format split.
# See contract note in exit_manager._is_crypto(): Claude emits "BTC/USD" (slash),
# Alpaca position objects use "BTCUSD" (no slash). Never unify them — each context
# needs the format that its consumer expects.
def _crypto_bars_lookup(bars_resp, sym: str):
    """
    Look up bars for a crypto symbol trying both 'BTC/USD' and 'BTCUSD' formats.
    Alpaca SDK versions differ on which key format they use in the response.
    """
    for key in (sym, sym.replace("/", "")):
        try:
            b = bars_resp[key]
            if b:
                return b
        except (KeyError, TypeError):
            pass
    return None


def _crypto_trade_lookup(trade_resp, sym: str):
    """
    Look up latest trade for a crypto symbol trying both key formats.
    """
    for key in (sym, sym.replace("/", "")):
        if key in trade_resp:
            return trade_resp[key]
    return None



def compute_eth_btc_ratio(eth_price: float, btc_price: float,
                           eth_bars: list, btc_bars: list) -> dict:
    """
    Compute ETH/BTC relative strength from price history.
    Pure math - no API call. Uses bars already fetched in get_crypto_signals().

    Section type: ENRICHMENT
    Compact prompt: NO
    Fallback: returns {} on any error (already implemented)
    """
    if not eth_price or not btc_price or btc_price == 0:
        return {}

    ratio = eth_price / btc_price

    if eth_bars and btc_bars and len(eth_bars) >= 7:
        try:
            recent_eth = [b.close for b in eth_bars[-7:]]
            recent_btc = [b.close for b in btc_bars[-7:]]
            avg_ratios = [e / b for e, b in zip(recent_eth, recent_btc) if b > 0]
            if not avg_ratios:
                return {"ratio": ratio, "signal": "insufficient_data"}
            ratio_7d_avg = sum(avg_ratios) / len(avg_ratios)
        except Exception:
            return {"ratio": ratio, "signal": "insufficient_data"}
    else:
        return {"ratio": ratio, "signal": "insufficient_data"}

    ratio_pct_vs_avg = (ratio - ratio_7d_avg) / ratio_7d_avg * 100

    if ratio > ratio_7d_avg * 1.005:
        signal = "eth_outperforming"
    elif ratio < ratio_7d_avg * 0.995:
        signal = "btc_outperforming"
    else:
        signal = "neutral"

    return {
        "ratio": round(ratio, 6),
        "ratio_7d_avg": round(ratio_7d_avg, 6),
        "ratio_pct_vs_avg": round(ratio_pct_vs_avg, 2),
        "signal": signal,
    }


def build_crypto_context_section(
        sentiment: dict, eth_btc: dict, session_tier: str) -> str:
    """
    Format crypto intelligence for prompt injection.
    Full version for extended/overnight sessions.
    One-line condensed version for market hours (don't bloat stock prompt).
    Never raises - all fields optional.

    Section type: OPTIONAL
    Compact prompt: NO
    Fallback: returns "(crypto context unavailable)" on exception (already implemented)
    """
    try:
        fg_value  = sentiment.get("current", {}).get("value")
        fg_label  = sentiment.get("current", {}).get("label", "")
        fg_trend  = sentiment.get("trend", "")
        fg_signal = sentiment.get("signal", "neutral")
        btc_dom   = sentiment.get("btc_dominance")
        dom_trend = sentiment.get("dominance_trend", "")

        eth_signal = eth_btc.get("signal", "")
        eth_pct    = eth_btc.get("ratio_pct_vs_avg", 0)

        # Condensed for market hours
        if session_tier == "market":
            parts = []
            if fg_value is not None:
                parts.append(f"Crypto F&G: {fg_value}/100 {fg_label}")
            if btc_dom is not None:
                parts.append(f"BTC dom: {btc_dom:.1f}% ({dom_trend})")
            if eth_signal and eth_signal not in ("neutral", "insufficient_data"):
                parts.append(f"ETH/BTC: {eth_signal}")
            return "  " + " | ".join(parts) if parts else "  (crypto context unavailable)"

        # Full version for extended/overnight
        lines = ["=== CRYPTO MARKET CONTEXT ==="]

        if fg_value is not None:
            lines.append(
                f"Fear & Greed: {fg_value}/100 - {fg_label} "
                f"(trend: {fg_trend})"
            )
            if fg_signal == "contrarian_buy":
                lines.append(
                    "  WARNING: EXTREME FEAR - historically strong "
                    "contrarian buy signal"
                )
            elif fg_signal == "contrarian_sell":
                lines.append(
                    "  WARNING: EXTREME GREED - historically strong "
                    "contrarian sell/reduce signal"
                )

        if btc_dom is not None:
            dom_note = {
                "rising":  "money flowing BTC->alts negative, favor BTC over ETH",
                "falling": "altcoin season - ETH may outperform",
                "stable":  "neutral",
            }.get(dom_trend, "")
            lines.append(
                f"BTC Dominance: {btc_dom:.1f}% "
                f"({dom_trend}) - {dom_note}"
            )

        if eth_signal:
            signal_text = {
                "eth_outperforming":
                    f"ETH outperforming BTC by "
                    f"{abs(eth_pct):.1f}% - momentum signal",
                "btc_outperforming":
                    f"BTC outperforming ETH by "
                    f"{abs(eth_pct):.1f}% - favor BTC",
                "neutral":          "ETH/BTC ratio near 7-day average",
                "insufficient_data": "insufficient history",
            }.get(eth_signal, "")
            if signal_text:
                lines.append(f"ETH/BTC Ratio: {signal_text}")

        # Combined regime summary
        if fg_value is not None and btc_dom is not None and eth_signal:
            if fg_value <= 30 and dom_trend == "falling":
                regime = ("Cautious bullish - fear present but "
                          "alts gaining vs BTC")
            elif fg_value >= 70 and dom_trend == "rising":
                regime = ("Risk-off - greed + BTC dominance "
                          "rising = reduce altcoin exposure")
            elif fg_signal == "contrarian_buy":
                regime = ("Contrarian opportunity - extreme fear "
                          "historically precedes recoveries")
            else:
                regime = "Neutral - no extreme readings"
            lines.append(f"Combined regime: {regime}")

        return "\n".join(lines)
    except Exception:
        return "  (crypto context unavailable)"


# ── Crypto signals ────────────────────────────────────────────────────────────

def get_crypto_signals(symbols: list) -> tuple[str, dict, dict]:
    """Returns (formatted_string, current_prices_dict, eth_btc_dict).

    Section type: REQUIRED
    Compact prompt: YES (crypto prices always tracked)
    Fallback: returns error string + empty dicts on exception (already implemented)
    """
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=90)

    try:
        bars_resp = _get_crypto_client().get_crypto_bars(CryptoBarsRequest(
            symbol_or_symbols=symbols, timeframe=TimeFrame.Day,
            start=start, end=end,
        ))
    except Exception as exc:
        log.warning("Crypto bars fetch error: %s", exc)
        return f"  (error fetching crypto bars: {exc})", {}

    try:
        latest_resp = _get_crypto_client().get_crypto_latest_trade(
            CryptoLatestTradeRequest(symbol_or_symbols=symbols)
        )
    except Exception as exc:
        log.warning("Crypto latest trade fetch error: %s", exc)
        latest_resp = {}

    vwaps = _compute_crypto_vwap_24h(symbols)
    lines = []
    current_prices = {}

    for sym in symbols:
        try:
            # Use helper that tries both 'BTC/USD' and 'BTCUSD' key formats
            sym_bars = _crypto_bars_lookup(bars_resp, sym)
            if not sym_bars:
                log.warning("Crypto bars missing for %s — tried 'BTC/USD' and 'BTCUSD' key formats", sym)
                continue
            ind = _compute_indicators(sym_bars)
            if not ind:
                log.warning("Crypto indicators empty for %s (bar count: %d)", sym, len(sym_bars))
                continue

            trade = _crypto_trade_lookup(latest_resp, sym)
            price = float(trade.price) if trade else ind["price"]
            current_prices[sym] = price

            ma20 = ind.get("ma20")
            rsi  = ind.get("rsi")
            macd = ind.get("macd")
            msig = ind.get("macd_signal")
            vrat = ind.get("vol_ratio")
            dchg = (price - ind["prev"]) / ind["prev"] * 100 if ind.get("prev") else 0
            vwap = vwaps.get(sym)

            ma20_trend = ("ABOVE" if price > ma20 else "BELOW") if ma20 else "?"
            vs_ma20    = (price - ma20) / ma20 * 100 if ma20 else 0
            vwap_str   = (("ABOVE" if price > vwap else "BELOW") + f" VWAP(${vwap:,.2f})") if vwap else "VWAP=N/A"
            d_sign     = "+" if dchg >= 0 else ""

            ema9_c  = ind.get("ema9")
            ema21_c = ind.get("ema21")
            ecross_c= ind.get("ema9_cross", "none")
            ema9_cs = f"EMA9={ema9_c:,.2f}({'ABOVE' if ind.get('price_above_ema9') else 'BELOW'})" if ema9_c else "EMA9=?"
            ema21_cs= f"EMA21={ema21_c:,.2f}" if ema21_c else "EMA21=?"

            lines.append(
                f"  {sym:<9}  ${price:,.2f}  day {d_sign}{dchg:.1f}%  "
                f"MA20={ma20_trend}(${ma20:,.2f},{'+' if vs_ma20>=0 else ''}{vs_ma20:.1f}%)"
            )
            rsi_str  = f"RSI={rsi:.1f}" if rsi is not None else "RSI=?"
            macd_str = (f"MACD={macd:+.2f}/sig={msig:+.2f}"
                        if macd is not None and msig is not None else "MACD=?")
            # d1_vol = prior-day close volume vs 20-day daily average (NOT intraday)
            vol_str  = f"d1_vol={vrat:.1f}x vs 20d" if vrat is not None else "d1_vol=?"
            lines.append(f"  {'':9}  {rsi_str}  {macd_str}  {vol_str}  {vwap_str}")
            lines.append(f"  {'':9}  {ema9_cs}  {ema21_cs}  Cross={ecross_c}")
        except Exception as exc:
            log.warning("Crypto signal error for %s: %s", sym, exc)
            continue

    # Compute ETH/BTC ratio if both symbols are present
    eth_btc: dict = {}
    try:
        eth_price_val = current_prices.get("ETH/USD") or current_prices.get("ETHUSD")
        btc_price_val = current_prices.get("BTC/USD") or current_prices.get("BTCUSD")
        if eth_price_val and btc_price_val:
            # Retrieve bar objects for ratio computation
            eth_sym_bars = _crypto_bars_lookup(bars_resp, "ETH/USD")
            btc_sym_bars = _crypto_bars_lookup(bars_resp, "BTC/USD")
            eth_btc = compute_eth_btc_ratio(
                eth_price_val, btc_price_val,
                eth_sym_bars or [], btc_sym_bars or [])
    except Exception as _ratio_exc:
        log.debug("ETH/BTC ratio computation failed (non-fatal): %s", _ratio_exc)

    return ("\n".join(lines) if lines else "  (no crypto data)"), current_prices, eth_btc


# ── News ──────────────────────────────────────────────────────────────────────

def get_news(symbols: list, limit: int = 10,
             since_minutes: int | None = None) -> str:
    """
    Section type: OPTIONAL
    Compact prompt: NO
    Fallback: returns "(news unavailable: ...)" on exception (already implemented)
    """
    try:
        kwargs = dict(symbols=",".join(symbols), limit=limit, sort="desc")
        result   = _get_news_client().get_news(NewsRequest(**kwargs))
        articles = []
        for item in result:
            if isinstance(item, tuple) and len(item) == 2:
                payload = item[1]
                if isinstance(payload, dict) and "news" in payload:
                    articles.extend(payload["news"])

        if since_minutes:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
            articles = [
                a for a in articles
                if hasattr(a, "created_at") and a.created_at and
                   a.created_at.replace(tzinfo=timezone.utc) >= cutoff
            ]

        if not articles:
            return "  (no recent news)"
        lines = []
        for a in articles[:limit]:
            syms = ", ".join(a.symbols) if a.symbols else "general"
            lines.append(f"  [{syms}] {a.headline}")
        return "\n".join(lines)
    except Exception as exc:
        return f"  (news unavailable: {exc})"


# ── Sector & inter-market signals ─────────────────────────────────────────────

def _build_sector_table() -> str:
    """
    Section type: OPTIONAL
    Compact prompt: NO
    Fallback: returns "" on exception
    """
    try:
        sector_perf = dw.load_sector_perf()
        sectors     = sector_perf.get("sectors", {})
        if not sectors:
            return "  (sector data not yet available — run data_warehouse.py)"
        lines = [f"  {'Sector':<16} {'ETF':<6} {'Day%':>7} {'Week%':>7} {'Momentum':<8}"]
        lines.append("  " + "-" * 50)
        for sec, d in sorted(sectors.items(), key=lambda x: x[1].get("day_chg", 0), reverse=True):
            chg  = d.get("day_chg", 0)
            wchg = d.get("week_chg", 0)
            mom  = d.get("momentum", "?")
            etf  = d.get("etf", "?")
            arrow= "▲" if chg > 0 else ("▼" if chg < 0 else "─")
            lines.append(f"  {sec:<16} {etf:<6} {arrow}{chg:>+6.1f}% {wchg:>+6.1f}%  {mom}")
        return "\n".join(lines)
    except Exception as exc:
        log.warning("_build_sector_table failed: %s", exc)
        return ""


def _build_intermarket_signals() -> str:
    """
    Section type: OPTIONAL
    Compact prompt: NO
    Fallback: returns "" on exception
    """
    try:
        macro = dw.load_macro_snapshot()
        if not macro:
            return "  (macro data not yet available)"

        signals = []
        # T1-3: macro snapshot stores vix as {"price": N, "chg_pct": M} dict OR float;
        # guard prevents AttributeError if float arrives at the .get() call site.
        # _vix_price is intentionally unused here — VIX not in this signal set.
        _vix_snap = macro.get("vix", {})
        _vix_price = (
            float(_vix_snap.get("price", 20.0) or 20.0)
            if isinstance(_vix_snap, dict) else float(_vix_snap or 20.0)
        )
        oil  = macro.get("oil",    {}).get("chg_pct", 0)
        gold = macro.get("gold",   {}).get("chg_pct", 0)
        dxy  = macro.get("dollar", {}).get("chg_pct", 0)
        macro.get("sp500",  {}).get("chg_pct", 0)  # using sp500 as proxy

        if oil >= 2.0:
            signals.append(f"  Oil +{oil:.1f}% → geopolitical risk-on: long defense (LMT/RTX/ITA), watch airlines")
        elif oil <= -2.0:
            signals.append(f"  Oil {oil:.1f}% → energy weakness: watch XLE/XOM for short setups")

        if gold >= 1.5:
            signals.append(f"  Gold +{gold:.1f}% → risk-off / dollar weakness signal")
        elif gold <= -1.5:
            signals.append(f"  Gold {gold:.1f}% → risk-on, dollar strength")

        if dxy >= 0.5:
            signals.append(f"  Dollar +{dxy:.1f}% → headwind for EEM, GLD; tailwind for XLF")
        elif dxy <= -0.5:
            signals.append(f"  Dollar {dxy:.1f}% → tailwind for EEM, GLD, commodities")

        if not signals:
            signals.append("  No significant inter-market divergences detected this cycle.")

        return "\n".join(signals)
    except Exception as exc:
        log.warning("_build_intermarket_signals failed: %s", exc)
        return ""


def _build_earnings_calendar() -> str:
    """
    Section type: OPTIONAL
    Compact prompt: NO
    Fallback: returns "" on exception
    """
    try:
        cal  = dw.load_earnings_calendar()
        data = cal.get("calendar", [])
        if not data:
            return "  (earnings data not yet available)"
        lines = []
        for e in data[:10]:
            lines.append(f"  {e.get('symbol','?'):<8}  {e.get('earnings_date','?')}")
        return "\n".join(lines) if lines else "  (none in next 7 days)"
    except Exception as exc:
        log.warning("_build_earnings_calendar failed: %s", exc)
        return ""


def _build_global_session_handoff(session_tier: str = "unknown") -> str:
    """
    Read global_indices.json and produce a formatted session handoff table.
    Groups by Asia / Europe / US Futures / FX with live session status.
    Returns a string for prompt injection (all session tiers).

    Section type: OPTIONAL
    Compact prompt: NO
    Fallback: returns placeholder string when data unavailable (already implemented)
    """
    data = dw.load_global_indices()
    if not data or not data.get("indices"):
        return "  (global indices not yet available — will populate at next 4 AM refresh)"

    indices    = data["indices"]
    fetched_at = data.get("fetched_at", "?")[:16]

    # Freshness check: warn if > 6h old during overnight sessions
    staleness_warning = ""
    _fetched_at_str = data.get("fetched_at", "")
    if _fetched_at_str:
        try:
            _fetched_dt = datetime.fromisoformat(_fetched_at_str)
            if _fetched_dt.tzinfo is None:
                _fetched_dt = _fetched_dt.replace(tzinfo=ET)
            _age_h = (datetime.now(timezone.utc) - _fetched_dt.astimezone(timezone.utc)).total_seconds() / 3600
            if _age_h > 6 and session_tier == "overnight":
                log.warning(
                    "[MARKET_DATA] global_indices.json is stale (%.1fh old) — overnight regime may use outdated data",
                    _age_h,
                )
                staleness_warning = (
                    f"\n  [WARNING: global indices data is {_age_h:.1f}h old — "
                    f"overnight regime context may be outdated]"
                )
        except Exception:
            pass

    # Compute live session status from current UTC time
    now_utc = datetime.now(timezone.utc)
    h       = now_utc.hour + now_utc.minute / 60.0
    wday    = now_utc.weekday()
    trading = wday < 5
    asia_open   = trading and 0.0  <= h < 6.0
    europe_open = trading and 7.0  <= h < 16.5
    us_open     = trading and 13.5 <= h < 20.0

    open_list = (["Asia"] if asia_open else []) + \
                (["Europe"] if europe_open else []) + \
                (["US"] if us_open else [])
    sessions_line = ", ".join(open_list) if open_list else "None (all markets closed)"

    def _fmt(ticker: str) -> str:
        e = indices.get(ticker)
        if not e:
            return f"{ticker}: N/A"
        chg  = e.get("chg_pct", 0)
        sign = "+" if chg >= 0 else ""
        return f"{e['name']}: {sign}{chg:.1f}%"

    asia_status   = "open  " if asia_open   else "closed"
    europe_status = "open  " if europe_open else "closed"

    asia_row    = "  ".join(_fmt(t) for t in ("^N225", "^HSI", "000001.SS"))
    europe_row  = "  ".join(_fmt(t) for t in ("^GDAXI", "^FTSE", "^FCHI", "EURUSD=X"))
    futures_row = "  ".join(_fmt(t) for t in ("ES=F", "NQ=F", "YM=F", "VX=F"))
    fx_row      = "  ".join(_fmt(t) for t in ("JPY=X", "CNY=X"))

    lines = [
        f"  Sessions open: {sessions_line}  (data as of {fetched_at})",
        f"  Asia   ({asia_status}): {asia_row}",
        f"  Europe ({europe_status}): {europe_row}",
        f"  US Futures:    {futures_row}",
        f"  FX Signals:    {fx_row}",
    ]

    # Add overnight context note when Asian markets are primary
    if not us_open and not europe_open:
        lines.append(
            "\n  [OVERNIGHT SESSION] Asian indices are the primary macro leading indicator "
            "for BTC/ETH right now.\n"
            "  USD/JPY and USD/CNY moves reflect crypto risk sentiment directly. "
            "Shanghai/Hang Seng = risk-on/off proxy."
        )

    return "\n".join(lines) + staleness_warning


# ── Watchlist prompt sections ─────────────────────────────────────────────────

def _build_core_by_sector(signals_by_sym: dict) -> str:
    """Group core watchlist signals by sector."""
    core    = wm.get_core()
    sectors: dict[str, list] = {}
    for s in core:
        sectors.setdefault(s["sector"], []).append(s["symbol"])

    lines = []
    for sector, syms in sorted(sectors.items()):
        lines.append(f"\n  [{sector.upper()}]")
        for sym in syms:
            sig = signals_by_sym.get(sym, f"  {sym:<8} (no data)")
            lines.append(sig)
    return "\n".join(lines)


def _build_dynamic_section(current_prices: dict) -> str:
    dynamic = wm.get_dynamic()
    if not dynamic:
        return "  (no dynamic scan finds today)"
    lines = []
    for s in dynamic:
        sym     = s.get("symbol", "?")
        reason  = s.get("reason", "?")
        cat     = s.get("catalyst", "")[:60]
        price   = current_prices.get(sym)
        p_str   = f"${price:.2f}" if price else "price=?"
        score   = s.get("score", 0)
        lines.append(f"  [SCAN] {sym:<8} {p_str}  score={score:.2f}  reason={reason}  catalyst={cat}")
    return "\n".join(lines)


def _build_intraday_section(current_prices: dict) -> str:
    intraday = wm.get_intraday()
    if not intraday:
        return "  (no intraday live adds)"
    lines = []
    for s in intraday:
        sym    = s.get("symbol", "?")
        reason = s.get("reason", "?")
        added  = s.get("added_at", "")[:16]
        price  = current_prices.get(sym)
        p_str  = f"${price:.2f}" if price else "price=?"
        lines.append(f"  [LIVE] {sym:<8} {p_str}  added={added}  reason={reason}")
    return "\n".join(lines)


# ── Master fetch ──────────────────────────────────────────────────────────────

def fetch_all(symbols_stock: list, symbols_crypto: list, session_tier: str,
              next_cycle_time: str = "?") -> dict:
    """
    Fetch all market data for one decision cycle.
    Returns the full context dict consumed by build_user_prompt().
    """
    clock = get_market_clock()
    wm.get_active_watchlist()

    # Always fetch crypto
    crypto_str, crypto_prices, eth_btc = get_crypto_signals(symbols_crypto)

    current_prices: dict = {**crypto_prices}
    watchlist_signals     = ""
    breaking_news         = "  (extended/overnight — not fetched)"
    sector_news           = "  (extended/overnight — not fetched)"
    signals_by_sym: dict  = {}
    ind_by_symbol: dict   = {}   # raw indicator dicts per symbol, for L2 scorer
    intraday_summaries: dict = {}  # intraday_cache summaries, for L2 scorer

    if session_tier == "market":
        stock_str, stock_prices, ind_by_symbol, intraday_summaries = get_stock_signals(symbols_stock)
        current_prices.update(stock_prices)

        # Build per-symbol signal dict for sector grouping
        for line in stock_str.split("\n"):
            stripped = line.strip()
            if stripped and not stripped.startswith("RSI") and not stripped.startswith("MACD"):
                parts = stripped.split()
                if parts:
                    signals_by_sym[parts[0]] = line
            elif stripped:
                # Second line belongs to previous symbol
                if signals_by_sym:
                    last_sym = list(signals_by_sym.keys())[-1]
                    signals_by_sym[last_sym] += "\n" + line

        watchlist_signals = stock_str
        news_syms         = [s for s in symbols_stock if "/" not in s]
        breaking_news     = get_news(news_syms, limit=5, since_minutes=15)
        sector_news       = get_news(news_syms, limit=10)

    elif session_tier == "extended":
        news_syms   = [s for s in symbols_stock if "/" not in s]
        breaking_news = get_news(news_syms, limit=5, since_minutes=15)
        sector_news   = get_news(news_syms, limit=5)
        watchlist_signals = "  (extended session — stock bars not fetched)"

    vix_val              = get_vix()
    vix_label, vix_instr = vix_regime(vix_val)

    # ── New intelligence sections (degrade gracefully) ───────────────────────
    all_stock_syms = [s for s in symbols_stock if "/" not in s]

    insider_section = "  (insider intelligence unavailable this cycle)"
    try:
        from insider_intelligence import (
            build_insider_intelligence_section,  # noqa: PLC0415
        )
        insider_section = build_insider_intelligence_section(all_stock_syms)
    except Exception:
        pass

    morning_brief_section = "  (morning brief not yet generated — runs at 4:15 AM ET)"
    try:
        from morning_brief import format_morning_brief_section  # noqa: PLC0415
        morning_brief_section = format_morning_brief_section()
    except Exception:
        pass

    reddit_section = "  (Reddit sentiment unavailable — configure REDDIT_CLIENT_ID in .env)"
    try:
        from reddit_sentiment import format_reddit_sentiment_section  # noqa: PLC0415
        reddit_section = format_reddit_sentiment_section(all_stock_syms)
    except Exception:
        pass

    # Earnings intel: only for symbols within 3 days of their earnings date
    earnings_intel_section = "  (no symbols within 3 days of earnings)"
    try:
        from earnings_intel import get_earnings_intel_section  # noqa: PLC0415
        cal     = dw.load_earnings_calendar()
        today   = datetime.now().date()
        near_earnings: list[str] = []
        days_map: dict[str, int] = {}
        for e in cal.get("calendar", []):
            sym = e.get("symbol", "")
            dt_str = e.get("earnings_date", "")
            if not sym or not dt_str:
                continue
            try:
                edate = datetime.strptime(dt_str[:10], "%Y-%m-%d").date()
                diff  = (edate - today).days
                if -1 <= diff <= 3 and sym in all_stock_syms:
                    near_earnings.append(sym)
                    days_map[sym] = diff
            except ValueError:
                pass
        if near_earnings:
            intel_lines = []
            for sym in near_earnings[:5]:
                intel_lines.append(get_earnings_intel_section(sym, days_map[sym]))
            earnings_intel_section = "\n".join(intel_lines)

            # Merge analyst intel (beat history + consensus) from 24h cache — no network call
            try:
                import earnings_intel_fetcher as eif  # noqa: PLC0415
                analyst_extras: list[str] = []
                for sym in near_earnings[:5]:
                    cached = eif.load_analyst_intel_cached(sym)
                    if cached:
                        ai_text = eif.format_analyst_intel_text(cached)
                        if ai_text:
                            analyst_extras.append(f"  {sym}: {ai_text}")
                if analyst_extras:
                    earnings_intel_section = (
                        earnings_intel_section
                        + "\n--- Analyst Intel ---\n"
                        + "\n".join(analyst_extras)
                    )
            except Exception:
                pass
    except Exception:
        pass

    # Economic calendar (Finnhub)
    economic_cal_section = "  (economic calendar unavailable)"
    try:
        eco_cal = dw.load_economic_calendar()
        if eco_cal:
            economic_cal_section = build_economic_calendar_section(eco_cal)
    except Exception:
        pass

    # Macro wire (Reuters/AP)
    macro_wire_section = "  No significant macro headlines in the past 4 hours."
    try:
        from macro_wire import build_macro_wire_section  # noqa: PLC0415
        macro_wire_section = build_macro_wire_section()
    except Exception:
        pass

    # ORB candidates
    orb_section = "  No ORB candidates identified for today."
    try:
        import json as _json  # noqa: PLC0415
        orb_path = Path(__file__).parent / "data" / "scanner" / "orb_candidates.json"
        if orb_path.exists():
            orb_data = _json.loads(orb_path.read_text())
            orb_section = build_orb_section(orb_data)
    except Exception:
        pass

    return {
        "session_tier":            session_tier,
        "next_cycle_time":         next_cycle_time,
        "vix":                     vix_val,
        "vix_regime":              f"{vix_label} (VIX={vix_val:.1f})",
        "regime_instruction":      vix_instr,
        "market_status":           clock["status"],
        "time_et":                 clock["time_et"],
        "minutes_since_open":      clock["minutes_since_open"],
        "watchlist_signals":       watchlist_signals,
        "crypto_signals":          crypto_str,
        "eth_btc":                 eth_btc,
        "breaking_news":           breaking_news,
        "sector_news":             sector_news,
        "sector_table":            _build_sector_table(),
        "intermarket_signals":     _build_intermarket_signals(),
        "global_handoff":          _build_global_session_handoff(session_tier),
        "earnings_calendar":       _build_earnings_calendar(),
        "core_by_sector":          _build_core_by_sector(signals_by_sym),
        "dynamic_section":         _build_dynamic_section(current_prices),
        "intraday_section":        _build_intraday_section(current_prices),
        "current_prices":          current_prices,
        "insider_section":         insider_section,
        "morning_brief_section":   morning_brief_section,
        "reddit_section":          reddit_section,
        "earnings_intel_section":  earnings_intel_section,
        "economic_calendar_section": economic_cal_section,
        "macro_wire_section":      macro_wire_section,
        "orb_section":             orb_section,
        # Raw per-symbol indicator dicts retained for L2 python scorer.
        # Keys: symbol → {rsi, macd, macd_signal, ma20, ma50, ema9, ema21,
        #                 ema9_cross, price_above_ema9, vol_ratio, price, prev, ...}
        "ind_by_symbol":           ind_by_symbol,
        # intraday_cache.get_intraday_summary output per symbol (5-min bars).
        # Keys: symbol → {rsi, macd, macd_signal, momentum_5bar, vol_ratio,
        #                 vwap, bar_count, last_bar}
        "intraday_summaries":      intraday_summaries,
    }


# ── Standalone test ───────────────────────────────────────────────────────────

def test_crypto_prices() -> dict:
    """
    Quick sanity check — fetches BTC and ETH prices and prints them.
    Run directly:  python market_data.py
    """
    syms = ["BTC/USD", "ETH/USD"]
    print(f"Fetching crypto signals for {syms} ...")
    crypto_str, prices, eth_btc_test = get_crypto_signals(syms)

    print("\n── Signal output ─────────────────────────────")
    print(crypto_str)
    print("\n── Current prices ────────────────────────────")
    if prices:
        for sym, price in prices.items():
            print(f"  {sym}: ${price:,.2f}  ✓")
    else:
        print("  WARNING: no prices returned — crypto data is broken!")
    print("──────────────────────────────────────────────")
    return prices


if __name__ == "__main__":
    test_crypto_prices()


# ── Economic Calendar section (Finnhub) ───────────────────────────────────────

def build_economic_calendar_section(calendar: dict, lookahead_hours: int = 8) -> str:
    """
    Build prompt section from Finnhub economic calendar.
    Warns if high-impact event within 60 minutes.
    """
    events = calendar.get("events", [])
    if not events:
        return "  No scheduled high-impact events in next 8 hours."

    lookahead_min = lookahead_hours * 60
    upcoming = [
        e for e in events
        if -60 <= e.get("minutes_from_now", 9999) <= lookahead_min
        and e.get("impact") in ("high", "medium")
    ]

    if not upcoming:
        return f"  No scheduled high-impact events in next {lookahead_hours} hours."

    lines = []

    # Check for imminent high-impact event
    next_hi = calendar.get("next_high_impact")
    if next_hi and 0 < next_hi.get("minutes_from_now", 9999) <= 60:
        n = next_hi["minutes_from_now"]
        lines.append(
            f"  ⚠ HIGH-IMPACT EVENT IN {n} MIN: {next_hi['event']}\n"
            f"  Reduce new position size. Widen stops on existing positions.\n"
            f"  Avoid new momentum entries until after the release."
        )
        lines.append("")

    for e in sorted(upcoming, key=lambda x: x.get("minutes_from_now", 9999)):
        try:
            dt_str  = e.get("datetime_et", "")[:16]
            dt      = datetime.fromisoformat(dt_str)
            time_str = dt.strftime("%I:%M %p")
        except Exception:
            time_str = e.get("datetime_et", "?")[:10]
        impact   = e.get("impact", "").upper()[:3]
        event    = e.get("event", "?")
        estimate = e.get("estimate")
        prev     = e.get("prev")
        actual   = e.get("actual")
        tag      = f"[{impact}]"

        detail = ""
        if actual is not None:
            detail = f" — actual {actual}"
        elif estimate is not None:
            detail = f" — consensus {estimate}"
            if prev is not None:
                detail += f" (prev {prev})"

        lines.append(f"  {time_str}  {tag:<6}  {event}{detail}")

    return "\n".join(lines)


# ── ORB Section ───────────────────────────────────────────────────────────────

def build_orb_section(orb_data: dict) -> str:
    """Build prompt section from ORB candidates data."""
    if not orb_data:
        return "  No ORB candidates identified for today."

    candidates = orb_data.get("candidates", [])
    if not candidates:
        return "  No ORB candidates identified for today."

    high   = [c for c in candidates if c.get("conviction") == "HIGH"]
    medium = [c for c in candidates if c.get("conviction") == "MEDIUM"]
    watch  = [c for c in candidates if c.get("conviction") == "WATCH"]

    lines = []
    if high:
        lines.append("  HIGH CONVICTION:")
        for c in high:
            sym   = c.get("symbol", "?")
            gap   = c.get("gap_pct", 0)
            vol   = c.get("pre_mkt_volume_ratio", 0)
            cat   = c.get("catalyst", "")[:50]
            entry = c.get("entry_condition", "")
            inv   = c.get("invalidation", "")
            sign  = "+" if gap >= 0 else ""
            lines.append(f"    {sym:<6}  gap {sign}{gap:.1f}%  vol {vol:.1f}x  {cat}")
            if entry:
                lines.append(f"           Entry: {entry}")
            if inv:
                lines.append(f"           Invalidation: {inv}")
    if medium:
        lines.append("  MEDIUM:")
        for c in medium:
            sym = c.get("symbol", "?")
            gap = c.get("gap_pct", 0)
            vol = c.get("pre_mkt_volume_ratio", 0)
            cat = c.get("catalyst", "")[:50]
            sign = "+" if gap >= 0 else ""
            lines.append(f"    {sym:<6}  gap {sign}{gap:.1f}%  vol {vol:.1f}x  {cat}")
    if watch:
        lines.append("  WATCH (gap present, volume unconfirmed):")
        for c in watch:
            sym = c.get("symbol", "?")
            gap = c.get("gap_pct", 0)
            vol = c.get("pre_mkt_volume_ratio", 0)
            sign = "+" if gap >= 0 else ""
            lines.append(f"    {sym:<6}  gap {sign}{gap:.1f}%  vol {vol:.1f}x")

    return "\n".join(lines) if lines else "  No ORB candidates identified for today."
