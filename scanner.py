"""
scanner.py — pre-market scanner. Runs at 4:00 AM ET daily.

Scans for Tier 2 (DYNAMIC) candidates using:
  - Momentum (volume spike, price move)
  - News catalyst (Alpaca News API)
  - Earnings momentum
  - Pre-market gap

Scores each candidate 0.0-1.0 and promotes top 5-8
to watchlist_dynamic.json via watchlist_manager.

Usage:
    python scanner.py                  # full scan
    python scanner.py --dry-run        # print candidates, don't promote
"""

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf
from dotenv import load_dotenv

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest, StockBarsRequest
from alpaca.data.enums import DataFeed
from alpaca.data.timeframe import TimeFrame

import watchlist_manager as wm
from log_setup import get_logger

load_dotenv()
log = get_logger(__name__)
ET  = ZoneInfo("America/New_York")

DATA_DIR    = Path(__file__).parent / "data" / "scanner"
CANDIDATES_FILE = DATA_DIR / "daily_candidates.json"

_api_key    = os.getenv("ALPACA_API_KEY")
_secret_key = os.getenv("ALPACA_SECRET_KEY")
_data       = StockHistoricalDataClient(_api_key, _secret_key)
_news_client= NewsClient(_api_key, _secret_key)

# Scan universe — S&P 500 bellwethers + high-vol names (extendable)
SCAN_UNIVERSE = [
    "AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","AMD","INTC","QCOM",
    "AVGO","MU","AMAT","LRCX","KLAC","NFLX","DIS","PYPL","SQ","SHOP",
    "COIN","HOOD","SOFI","UPST","AFRM","RBLX","SNAP","PINS","UBER","LYFT",
    "ABNB","DASH","RIVN","LCID","NIO","XPEV","LI","BIDU","JD","BABA",
    "PDD","MELI","SE","GRAB","DKNG","MGM","WYNN","LVS","CZR","PENN",
    "MRNA","BNTX","PFE","ABBV","BMY","GILD","REGN","BIIB","VRTX","ILMN",
    "BA","GE","HON","CAT","DE","MMM","UPS","FDX","DAL","UAL","AAL","LUV",
    "XOM","CVX","COP","OXY","HAL","SLB","DVN","FANG","MPC","VLO",
    "JPM","BAC","WFC","C","GS","MS","BLK","SCHW","AXP","V","MA",
    "SPY","QQQ","IWM","XLE","XLF","XBI","XRT","ITA","GLD","SLV","TLT","VXX",
]


# ── Scoring ───────────────────────────────────────────────────────────────────

def _score_candidate(sym: str, bars: list, has_catalyst: bool,
                     earnings_beat: bool, premarket_gap_pct: float,
                     above_20ma: bool, has_earnings_soon: bool) -> float:
    if len(bars) < 21:
        return 0.0

    score = 0.0

    # Volume ratio (yesterday vs 30d avg)
    vols = [b.volume for b in bars[-31:-1]]
    if vols:
        avg_vol = sum(vols) / len(vols)
        last_vol = bars[-1].volume
        if avg_vol > 0:
            vol_ratio = last_vol / avg_vol
            if vol_ratio >= 10:  score += 0.50
            elif vol_ratio >= 5: score += 0.35
            elif vol_ratio >= 3: score += 0.20

    # Price move yesterday >= 4%
    if len(bars) >= 2:
        prev  = bars[-2].close
        last  = bars[-1].close
        if prev > 0:
            pct = (last - prev) / prev * 100
            if abs(pct) >= 4.0:
                score += 0.15

    # Named catalyst in news
    if has_catalyst:
        score += 0.25

    # Earnings beat
    if earnings_beat:
        score += 0.20

    # Pre-market gap confirmation
    if premarket_gap_pct >= 2.0:
        score += 0.15

    # Above 20MA
    if above_20ma:
        score += 0.10

    # Penalty: earnings in next 48h (binary risk)
    if has_earnings_soon:
        score -= 0.20

    return round(min(max(score, 0.0), 1.0), 3)


# ── News scan ─────────────────────────────────────────────────────────────────

def _get_news_catalysts(symbols: list) -> dict[str, str]:
    """Returns {symbol: headline} for symbols with news in last 24h."""
    result: dict[str, str] = {}
    batch = symbols[:50]   # API limit per call
    try:
        resp     = _news_client.get_news(NewsRequest(
            symbols=",".join(batch), limit=50, sort="desc"
        ))
        articles = []
        for item in resp:
            if isinstance(item, tuple) and len(item) == 2:
                payload = item[1]
                if isinstance(payload, dict) and "news" in payload:
                    articles.extend(payload["news"])
        for a in articles:
            for sym in (a.symbols or []):
                if sym not in result:
                    result[sym] = a.headline
    except Exception as exc:
        log.warning("News scan failed: %s", exc)
    return result


# ── Pre-market gap ────────────────────────────────────────────────────────────

def _get_premarket_gap(symbol: str) -> float:
    """Return estimated pre-market gap % using yfinance fast_info."""
    try:
        t = yf.Ticker(symbol)
        info = t.fast_info
        prev_close = float(info.get("previousClose") or info.get("regularMarketPreviousClose") or 0)
        pre_price  = float(info.get("preMarketPrice") or info.get("currentPrice") or 0)
        if prev_close > 0 and pre_price > 0:
            return round((pre_price - prev_close) / prev_close * 100, 2)
    except Exception:
        pass
    return 0.0


# ── Main scan ─────────────────────────────────────────────────────────────────

def run_scan(dry_run: bool = False) -> list[dict]:
    log.info("Scanner starting  universe=%d symbols", len(SCAN_UNIVERSE))
    now    = datetime.now(timezone.utc)
    start  = now - timedelta(days=45)

    # Fetch bars for scan universe in batches of 50
    all_bars: dict[str, list] = {}
    for i in range(0, len(SCAN_UNIVERSE), 50):
        batch = SCAN_UNIVERSE[i:i+50]
        try:
            resp = _data.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame.Day,
                start=start, end=now,
                feed=DataFeed.IEX,
            ))
            for sym in batch:
                try:
                    all_bars[sym] = resp[sym]
                except Exception:
                    pass
        except Exception as exc:
            log.warning("Bars fetch error (batch %d): %s", i, exc)

    log.info("Scanner fetched bars for %d symbols", len(all_bars))

    # News catalysts
    catalysts = _get_news_catalysts(SCAN_UNIVERSE)

    # Core symbols to exclude from dynamic (already tracked)
    core_syms = {s["symbol"] for s in wm.get_core()}

    candidates: list[dict] = []

    for sym, bars in all_bars.items():
        if sym in core_syms:
            continue
        if len(bars) < 21:
            continue

        last_bar = bars[-1]
        price = float(last_bar.close)

        # Basic filters
        if price < 10.0:
            continue

        # Volume filter: last day > 500K shares AND 3x 30d avg
        vols_30 = [b.volume for b in bars[-31:-1]]
        if not vols_30:
            continue
        avg_vol = sum(vols_30) / len(vols_30)
        if avg_vol < 500_000:
            continue
        vol_ratio = last_bar.volume / avg_vol if avg_vol > 0 else 0
        if vol_ratio < 3.0:
            continue

        # Price move filter
        if len(bars) >= 2:
            prev_close = float(bars[-2].close)
            pct_chg    = (price - prev_close) / prev_close * 100 if prev_close > 0 else 0
        else:
            pct_chg = 0.0

        if abs(pct_chg) < 4.0:
            continue

        # Above 20MA?
        closes  = [b.close for b in bars[-20:]]
        ma20    = sum(closes) / len(closes) if closes else 0
        above_20ma = price > ma20

        has_catalyst  = sym in catalysts
        premarket_gap = _get_premarket_gap(sym)

        score = _score_candidate(
            sym, bars,
            has_catalyst=has_catalyst,
            earnings_beat=False,         # placeholder — no earnings API
            premarket_gap_pct=premarket_gap,
            above_20ma=above_20ma,
            has_earnings_soon=False,     # placeholder
        )

        if score < 0.30:
            continue

        candidates.append({
            "symbol":       sym,
            "tier":         "dynamic",
            "type":         "stock",
            "sector":       "unknown",
            "score":        score,
            "vol_ratio":    round(vol_ratio, 2),
            "pct_chg":      round(pct_chg, 2),
            "price":        price,
            "has_catalyst": has_catalyst,
            "catalyst":     catalysts.get(sym, ""),
            "premarket_gap":premarket_gap,
            "above_20ma":   above_20ma,
            "reason":       f"vol_{vol_ratio:.1f}x_move_{pct_chg:+.1f}pct",
            "added_at":     datetime.now(ET).isoformat(),
        })

    # Rank by score, take top 8
    candidates.sort(key=lambda x: x["score"], reverse=True)
    top = candidates[:8]

    log.info("Scanner found %d qualifying candidates, top %d promoted",
             len(candidates), len(top))

    # Save all candidates to file
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CANDIDATES_FILE.write_text(json.dumps({
        "date":       datetime.now(ET).strftime("%Y-%m-%d"),
        "scanned":    len(all_bars),
        "qualifying": len(candidates),
        "candidates": candidates,
    }, indent=2))

    if not dry_run:
        wm.set_dynamic(top)
        for c in top:
            log.info("  [SCAN→DYNAMIC] %s  score=%.2f  vol=%.1fx  chg=%+.1f%%  catalyst=%s",
                     c["symbol"], c["score"], c["vol_ratio"], c["pct_chg"],
                     c["catalyst"][:60] if c["catalyst"] else "none")
    else:
        print("\n=== SCAN RESULTS (dry-run) ===")
        for c in top:
            print(f"  {c['symbol']:<8}  score={c['score']:.2f}  "
                  f"vol={c['vol_ratio']:.1f}x  chg={c['pct_chg']:+.1f}%  "
                  f"gap={c['premarket_gap']:+.1f}%  "
                  f"catalyst={'YES' if c['has_catalyst'] else 'no'}")

    return top


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pre-market scanner")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print results without promoting to watchlist")
    args = parser.parse_args()
    run_scan(dry_run=args.dry_run)


# ── S&P 500 Universe ──────────────────────────────────────────────────────────

SP500_UNIVERSE = [
    # Technology
    "AAPL","MSFT","NVDA","AVGO","ORCL","AMD","QCOM","TXN","INTC","CSCO","IBM","AMAT","LRCX","KLAC",
    "MU","ADI","MCHP","CDNS","SNPS","FTNT","PANW","CRWD","ANSS","KEYS","VRSN","IT","GDDY","CTSH",
    "HPQ","HPE","WDC","STX","NTAP","AKAM","CDW","ZBRA","JNPR","FFIV","SWKS","QRVO","TER","MPWR",
    # Software/Cloud
    "CRM","ADBE","NOW","INTU","WDAY","TEAM","DDOG","SNOW","PLTR","OKTA","TWLO","ZI","S","GTLB",
    "HUBS","VEEV","ANGI","NET","ZS","COUP","PCTY","SMAR","TOST","DXCM","IDXX",
    # Consumer Discretionary
    "AMZN","TSLA","HD","MCD","NKE","SBUX","TJX","LOW","BKNG","ABNB","GM","F","CMG","YUM","DPZ",
    "MHK","RL","PVH","TPR","LULU","RH","BBY","DLTR","DG","ORLY","AZO","GPC","AAP",
    "MGM","WYNN","LVS","CZR","DKNG","PENN","VFC","HBI","COLM","BYD",
    # Consumer Staples
    "PG","KO","PEP","WMT","COST","PM","MO","MDLZ","CL","GIS","K","KHC","CPB","HSY","MKC","SJM",
    "TSN","HRL","SYY","STZ","EL","ULTA","KR","ADM","BG","CAG",
    # Healthcare
    "UNH","LLY","JNJ","ABBV","MRK","TMO","ABT","PFE","BMY","AMGN","MDT","ISRG","SYK","BSX","DHR",
    "A","ILMN","REGN","VRTX","BIIB","GILD","MRNA","ZBH","EW","BAX","HUM","CI","CVS","HCA",
    "MOH","CNC","ELV","MCK","ABC","CAH","STE","HOLX","ALGN","DXCM","PODD","WST","MTD","RMD",
    # Energy
    "XOM","CVX","COP","OXY","SLB","HAL","BKR","DVN","HES","MPC","VLO","PSX","WMB","KMI","OKE",
    "LNG","PXD","APA","EOG","CTRA","FANG","MRO","CVI","DT","TRGP","CHRD",
    # Financials
    "JPM","BAC","WFC","C","GS","MS","BLK","SCHW","AXP","V","MA","COF","DFS","SYF","USB","TFC",
    "PNC","KEY","RF","HBAN","CFG","MTB","FITB","CMA","ZION","STT","BK","CB","AIG","AFL","MET",
    "PRU","PGR","ALL","TRV","HIG","UNM","CINF","L","RNR","AON","MMC","WLTW","BRO","FNF",
    "SPGI","MCO","ICE","CME","CBOE","NDAQ","IVZ","BEN","AMG","RJF","LPLA","SEIC",
    # Industrials
    "BA","GE","HON","CAT","DE","MMM","UPS","FDX","LMT","RTX","NOC","GD","LHX","BA","ITW","EMR",
    "ETN","ROK","PH","DOV","XYL","PCAR","FAST","GWW","FTV","GNRC","CARR","OTIS","IR","TT",
    "DAL","UAL","AAL","LUV","ALK","JBLU","SWA","EXPD","XPO","CHRW","JBHT","ODFL","SAIA","WERN",
    "NSC","CSX","UNP","CNI","CP","WAB","TRN","GATX","ARII",
    # Materials
    "APD","LIN","SHW","ECL","FCX","NEM","NUE","RS","VMC","MLM","CF","MOS","IFF","DD","DOW",
    "LYB","PPG","PKG","SEE","WPM","FMC","ALB","MP","CTRA","SLGN","ATI","CMC","STLD","CLF",
    # Utilities
    "NEE","DUK","SO","D","EXC","AEP","XEL","SRE","PCG","ED","ES","DTE","PPL","EIX","FE",
    "NI","ATO","LNT","NRG","PNW","EVRG","CMS","AES","WEC","ETR","CNP","OGE","MDU",
    # Real Estate
    "AMT","PLD","CCI","EQIX","PSA","EQR","AVB","O","SPG","VICI","WELL","VTR","PEAK","HST",
    "MAA","UDR","EXR","INVH","AMH","SBA","SBAC","DLR","IRM","COLD","KIM","REG","FRT","BXP",
    # Communication Services
    "GOOGL","GOOG","META","NFLX","CMCSA","TMUS","VZ","T","DIS","PARA","WBD","FOXA","FOX",
    "OMC","IPG","TTWO","EA","ZM","MTCH","IAC","ANGI","MKSI","SNAP","PINS","RBLX","TDC",
    # ETFs in universe for ORB
    "SPY","QQQ","IWM","XLE","XLF","XBI","XRT","ITA","GLD","SLV","TLT","VXX","HYG","EMB",
    "SOXX","SMH","IBB","ARKK","XLK","XLV","XLY","XLP","XLI","XLU","XLB","XLRE",
]

# Deduplicate with SCAN_UNIVERSE
ORB_SCAN_UNIVERSE = list(dict.fromkeys(SP500_UNIVERSE + SCAN_UNIVERSE))


# ── ORB Scoring ───────────────────────────────────────────────────────────────

def compute_orb_score(gap_pct: float, vol_ratio: float,
                       has_catalyst: bool, avg_daily_vol: int) -> float:
    """
    Score 0.0-1.0 for ORB candidate quality.
    gap_pct weight: 40%   vol_ratio weight: 35%
    catalyst bonus: 20%   liquidity score: 5%
    """
    score = 0.0

    # Gap weight (40%)
    gap_abs = abs(gap_pct)
    if gap_abs >= 5.0:
        score += 0.40
    elif gap_abs >= 3.0:
        score += 0.30
    elif gap_abs >= 2.0:
        score += 0.22
    elif gap_abs >= 1.0:
        score += 0.12

    # Volume weight (35%)
    if vol_ratio >= 5.0:
        score += 0.35
    elif vol_ratio >= 3.0:
        score += 0.25
    elif vol_ratio >= 2.0:
        score += 0.18
    elif vol_ratio >= 1.5:
        score += 0.10

    # Catalyst bonus (20%)
    if has_catalyst:
        score += 0.20

    # Liquidity score (5%)
    if avg_daily_vol >= 5_000_000:
        score += 0.05
    elif avg_daily_vol >= 1_000_000:
        score += 0.03

    return round(min(score, 1.0), 3)


def _conviction_label(score: float) -> str:
    if score >= 0.65:
        return "HIGH"
    if score >= 0.45:
        return "MEDIUM"
    return "WATCH"


def run_orb_scan() -> list:
    """
    Scan ORB_SCAN_UNIVERSE for pre-market gap + volume.
    Called at 4:30 AM daily. Returns top 15 candidates.
    Saves to data/scanner/orb_candidates.json
    """
    log.info("ORB scan starting  universe=%d symbols", len(ORB_SCAN_UNIVERSE))
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    catalysts = _get_news_catalysts(ORB_SCAN_UNIVERSE[:80])

    candidates: list = []

    # Batch yfinance fast_info fetches
    for sym in ORB_SCAN_UNIVERSE:
        # Skip non-standard symbols
        if "/" in sym or len(sym) > 6:
            continue
        try:
            fi = yf.Ticker(sym).fast_info
            prev_close  = float(fi.get("regularMarketPreviousClose") or
                                fi.get("previousClose") or 0)
            pre_price   = float(fi.get("preMarketPrice") or
                                fi.get("currentPrice") or 0)
            pre_vol     = float(fi.get("preMarketVolume") or 0)
            avg_vol     = float(fi.get("threeMonthAverageVolume") or
                                fi.get("averageVolume") or 0)

            if prev_close <= 0 or pre_price <= 0:
                continue
            if pre_price < 10.0:
                continue
            if avg_vol < 500_000:
                continue

            gap_pct    = (pre_price - prev_close) / prev_close * 100
            gap_abs    = abs(gap_pct)
            if gap_abs < 1.0:
                continue

            vol_ratio = (pre_vol / (avg_vol / 6.5)) if avg_vol > 0 else 0
            if vol_ratio < 1.5:
                continue

            has_catalyst = sym in catalysts
            score = compute_orb_score(gap_pct, vol_ratio, has_catalyst, int(avg_vol))

            if score < 0.20:
                continue

            conviction = _conviction_label(score)
            entry_price = pre_price
            gap_dir = "up" if gap_pct > 0 else "down"
            entry_cond = (
                f"Break {'above' if gap_dir=='up' else 'below'} ${entry_price:.2f} with 1.5x vol"
                if conviction == "HIGH" else ""
            )
            inv_level = round(prev_close * 1.005 if gap_dir == "up" else prev_close * 0.995, 2)

            candidates.append({
                "symbol":               sym,
                "gap_pct":              round(gap_pct, 2),
                "gap_direction":        gap_dir,
                "pre_mkt_volume_ratio": round(vol_ratio, 2),
                "avg_daily_volume":     int(avg_vol),
                "prior_close":          round(prev_close, 2),
                "pre_mkt_price":        round(pre_price, 2),
                "has_catalyst":         has_catalyst,
                "catalyst":             catalysts.get(sym, ""),
                "orb_score":            score,
                "conviction":           conviction,
                "entry_condition":      entry_cond,
                "invalidation":         f"Fails to hold ${inv_level}",
            })
        except Exception:
            continue

    # Sort and cap at 15
    candidates.sort(key=lambda x: x["orb_score"], reverse=True)
    top = candidates[:15]

    orb_path = DATA_DIR / "orb_candidates.json"
    orb_data = {
        "generated_at":    datetime.now(ET).isoformat(),
        "market_open_et":  datetime.now(ET).replace(
            hour=9, minute=30, second=0, microsecond=0).isoformat(),
        "candidates":      top,
    }
    orb_path.write_text(json.dumps(orb_data, indent=2))

    # Archive
    try:
        arc = Path(__file__).parent / "data" / "archive" / datetime.now(ET).strftime("%Y-%m-%d")
        arc.mkdir(parents=True, exist_ok=True)
        (arc / "orb_candidates.json").write_text(json.dumps(orb_data, indent=2))
    except Exception:
        pass

    log.info("ORB scan: %d candidates found, top %d saved", len(candidates), len(top))
    for c in top[:5]:
        log.info("  [ORB] %s  gap %+.1f%%  vol %.1fx  score=%.2f  %s",
                 c["symbol"], c["gap_pct"], c["pre_mkt_volume_ratio"],
                 c["orb_score"], c["conviction"])
    return top


def update_orb_candidates() -> None:
    """
    Called at 9:28 AM pre-open cycle.
    Re-fetches pre-market prices for existing candidates.
    Updates gap_pct, vol_ratio with latest data.
    """
    orb_path = DATA_DIR / "orb_candidates.json"
    if not orb_path.exists():
        return

    try:
        orb_data = json.loads(orb_path.read_text())
        candidates = orb_data.get("candidates", [])
    except Exception:
        return

    updated = []
    for c in candidates:
        sym = c.get("symbol", "")
        if not sym:
            continue
        try:
            fi        = yf.Ticker(sym).fast_info
            prev_close = float(fi.get("regularMarketPreviousClose") or
                                fi.get("previousClose") or c["prior_close"])
            pre_price  = float(fi.get("preMarketPrice") or
                                fi.get("currentPrice") or c["pre_mkt_price"])
            pre_vol    = float(fi.get("preMarketVolume") or 0)
            avg_vol    = float(fi.get("threeMonthAverageVolume") or c["avg_daily_volume"])

            old_gap   = c["gap_pct"]
            new_gap   = round((pre_price - prev_close) / prev_close * 100, 2) if prev_close > 0 else old_gap
            new_vol   = round((pre_vol / (avg_vol / 6.5)) if avg_vol > 0 else c["pre_mkt_volume_ratio"], 2)
            new_score = compute_orb_score(new_gap, new_vol, c["has_catalyst"], int(avg_vol))

            # Flag conviction change
            old_conv = c.get("conviction")
            new_conv = _conviction_label(new_score)
            if old_conv != new_conv:
                log.info("[ORB UPDATE] %s conviction changed %s→%s (gap %+.1f%%→%+.1f%%)",
                         sym, old_conv, new_conv, old_gap, new_gap)

            c["gap_pct"]             = new_gap
            c["pre_mkt_price"]       = round(pre_price, 2)
            c["pre_mkt_volume_ratio"]= new_vol
            c["orb_score"]           = new_score
            c["conviction"]          = new_conv
            c["gap_direction"]       = "up" if new_gap > 0 else "down"
            updated.append(c)
        except Exception:
            updated.append(c)

    updated.sort(key=lambda x: x["orb_score"], reverse=True)
    orb_data["candidates"]   = updated
    orb_data["updated_at"]   = datetime.now(ET).isoformat()
    orb_path.write_text(json.dumps(orb_data, indent=2))
    log.info("ORB candidates updated: %d symbols refreshed", len(updated))
