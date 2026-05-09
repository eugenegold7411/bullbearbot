#!/usr/bin/env python3
"""
conviction_scorer.py — Component-based earnings conviction scoring.

Reads canonical symbol files, scores 4 components (0-100 or null each),
writes daily conviction JSON for A1/A2 consumption.

Components:
  analyst_momentum  — recommendation consensus + price target upside
  beat_consistency  — EPS beat streak + average surprise magnitude
  insider_activity  — Form 4 + congressional trades (buys vs sells)
  news_catalyst     — Haiku directional scoring with company_name guard

Run:   .venv/bin/python3 conviction_scorer.py [--date YYYY-MM-DD]
       .venv/bin/python3 conviction_scorer.py --symbols NVDA AMAT TSLA
Schedule: 9:05 AM daily via scheduler (after 4 AM enrichment)
Output: data/convictions/YYYY-MM-DD_scores.json
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

_BASE = Path(__file__).parent
_SYMBOLS_DIR = _BASE / "data" / "symbols"
_CONVICTIONS_DIR = _BASE / "data" / "convictions"

_ALERT_DEDUP: dict[str, float] = {}
_ALERT_DEDUP_SECS: float = 300.0

ETF_SYMBOLS: frozenset[str] = frozenset({
    "SPY", "QQQ", "IWM", "GLD", "SLV", "TLT", "VXX", "USO",
    "EEM", "FXI", "EWJ", "EWM", "ECH",
    "XLE", "XLF", "XBI", "XRT", "ITA", "COPX",
})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize_company_name(company_name: str) -> str:
    """Strip legal suffixes then leading articles for headline matching."""
    # Order matters: longer / more specific patterns first.
    # No break — apply every matching suffix in sequence so compound forms
    # like "Amazon.com, Inc." strip cleanly to "Amazon".
    suffixes = [
        " ETF Trust", " ETF", " ETN", ", LP",
        " plc", " N.V.", " Trust",
        " & Company", " and Company", " & Co.",
        " Corporation", " Corp.", " Corp",
        ", Inc.", " Inc.", " Inc",
        " Ltd.", " Ltd", " LLC", " Limited",
        " Co.", " Company", ".com",
    ]
    normalized = company_name
    for suffix in suffixes:
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
    # Strip leading "The " / "A "
    for prefix in ("The ", "A "):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            break
    return normalized.strip()


def _extract_json(text: str) -> dict:
    """Extract JSON from Haiku response, which often wraps in code fences."""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
    if m:
        text = m.group(1).strip()
    start, end = text.find("{"), text.rfind("}") + 1
    if start >= 0 and end > start:
        return json.loads(text[start:end])
    return json.loads(text)


def _haiku_call(prompt: str, max_tokens: int = 400) -> str:
    """Single Haiku call. Returns raw response text. Raises on failure."""
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        system="You output only valid JSON. No explanation. No markdown. No code fences.",
        messages=[{"role": "user", "content": prompt}],
    )
    return r.content[0].text


def _send_alert(symbol: str, detail: str) -> None:
    """Fire a WhatsApp CRITICAL alert on persistent scoring failure. 5-min dedup. Never raises.
    # TODO(DASHBOARD): surface conviction_scorer failures on safety panel
    """
    import time
    key = f"conviction_scorer.{symbol}"
    now = time.time()
    if now - _ALERT_DEDUP.get(key, 0) < _ALERT_DEDUP_SECS:
        return
    _ALERT_DEDUP[key] = now
    try:
        from notifications import send_whatsapp_direct  # noqa: PLC0415
        send_whatsapp_direct(
            f"[CONVICTION SCORER] {symbol}: news_catalyst failed after retries "
            f"— scoring with null news component. {detail}"
        )
    except Exception:
        pass


# ── Component scorers ─────────────────────────────────────────────────────────

def score_analyst_momentum(canonical: dict) -> Optional[float]:
    """
    Score analyst consensus: recommendation key, buy/sell distribution, upside %.

    Bullish (70-100): strong_buy/buy key + >60% buy ratings + meaningful upside.
    Neutral (40-60):  hold key or mixed ratings.
    Bearish (0-35):   underperform/sell key or >40% sell ratings.
    Returns None if <3 analysts covering the symbol.
    """
    t = canonical.get("analyst_targets") or {}
    if not t:
        return None

    strong_buy  = int(t.get("strong_buy")  or 0)
    buy         = int(t.get("buy")         or 0)
    hold        = int(t.get("hold")        or 0)
    sell        = int(t.get("sell")        or 0)
    strong_sell = int(t.get("strong_sell") or 0)

    total_buy  = strong_buy + buy
    total_sell = sell + strong_sell
    total      = total_buy + hold + total_sell

    if total < 3:
        return None

    buy_pct  = total_buy  / total * 100
    sell_pct = total_sell / total * 100

    rec = (t.get("recommendation_key") or "hold").lower()
    upside = t.get("upside_pct")  # float or None

    # Base score from recommendation key
    if rec in ("strong_buy",):
        base = 80.0
    elif rec in ("buy",):
        base = 68.0
    elif rec in ("hold",):
        base = 50.0
    elif rec in ("underperform",):
        base = 35.0
    elif rec in ("sell",):
        base = 20.0
    else:
        base = 50.0

    # Adjust for distribution skew
    base += (buy_pct - 50) * 0.15   # +7.5 at 100% buy, -7.5 at 0% buy
    base -= (sell_pct - 5) * 0.20   # extra penalty for meaningful sell fraction

    # Adjust for price target upside
    if upside is not None:
        if upside > 30:
            base += 8
        elif upside > 15:
            base += 4
        elif upside < -10:
            base -= 8
        elif upside < 0:
            base -= 4

    return round(min(100.0, max(0.0, base)), 1)


def score_beat_consistency(canonical: dict) -> Optional[float]:
    """
    Score EPS beat history: consecutive beat/miss streaks + average surprise magnitude.

    Bullish (70-100): 4-quarter beat streak + avg surprise >8%.
    Bearish (0-30):   3-quarter miss streak or avg surprise < -5%.
    Returns None if <2 quarters of history.
    """
    beats = canonical.get("beat_history") or []
    if len(beats) < 2:
        return None

    surprises = [b.get("surprise_pct") for b in beats if b.get("surprise_pct") is not None]
    if not surprises:
        return None
    avg_surprise = sum(surprises) / len(surprises)

    # Streaks from most-recent quarter (index 0) forward.
    # Stop counting when the streak breaks.
    beat_streak = 0
    for b in beats:
        if (b.get("surprise_pct") or 0) > 0:
            beat_streak += 1
        else:
            break

    miss_streak = 0
    for b in beats:
        if (b.get("surprise_pct") or 0) <= 0:
            miss_streak += 1
        else:
            break

    base = 50.0

    # Average surprise component
    if avg_surprise > 10:
        base += 25
    elif avg_surprise > 5:
        base += 15
    elif avg_surprise > 2:
        base += 5
    elif avg_surprise < -5:
        base -= 25
    elif avg_surprise < -2:
        base -= 15
    elif avg_surprise < 0:
        base -= 5

    # Streak component
    if beat_streak >= 6:
        base += 20
    elif beat_streak >= 4:
        base += 12
    elif beat_streak >= 2:
        base += 6
    elif miss_streak >= 4:
        base -= 25
    elif miss_streak >= 3:
        base -= 18
    elif miss_streak >= 2:
        base -= 10

    return round(min(100.0, max(0.0, base)), 1)


def score_insider_activity(canonical: dict) -> Optional[float]:
    """
    Score insider (Form 4) and congressional trade activity in last 30 days.

    Bullish (65-100): executive buys, multiple insiders buying.
    Bearish (0-40):   executive sells, multiple insiders selling.
    Returns None if no trades reported.
    """
    insider  = canonical.get("insider_trades_30d") or []
    congress = canonical.get("congress_trades_30d") or []

    if not insider and not congress:
        return None

    buy_weight  = 0.0
    sell_weight = 0.0

    for trade in insider:
        action = (trade.get("action") or "").lower()
        role   = (trade.get("role")   or "").lower()
        # Executive (CEO/CFO/President/Director) counts double
        is_exec = any(kw in role for kw in ("ceo", "cfo", "president", "director", "officer"))
        weight = 2.0 if is_exec else 1.0
        if "buy" in action or "purchase" in action:
            buy_weight += weight
        elif "sell" in action or "sale" in action:
            sell_weight += weight

    for trade in congress:
        action = (trade.get("action") or "").lower()
        if "buy" in action or "purchase" in action:
            buy_weight += 1.0
        elif "sell" in action or "sale" in action:
            sell_weight += 0.5  # congressional sells are less informative

    net = buy_weight - sell_weight
    # Map net signal to 0-100: net=0→50, net=+4→90, net=-4→10
    score = 50.0 + net * 10.0
    return round(min(100.0, max(0.0, score)), 1)


def score_news_catalyst(canonical: dict, symbol: str) -> Optional[float]:
    """
    Score recent news using Haiku directional impact scoring with company_name guard.

    Steps:
      1. Pull ≤10 headlines from news_yahoo.
      2. Ask Haiku to score each -100 (strong bearish) to +100 (strong bullish)
         for THIS stock specifically.
      3. Company_name guard: if a headline doesn't mention the ticker or
         normalized company name, cap its absolute impact at ±20.
      4. Average → convert to 0-100.

    Returns None if <3 articles available.
    """
    news = canonical.get("news_yahoo") or []
    if len(news) < 3:
        return None

    headlines = [
        (a.get("headline") or "").strip()
        for a in news[:10]
        if (a.get("headline") or "").strip()
    ]
    if len(headlines) < 3:
        return None

    company_name = canonical.get("company_name", "")
    company_core = _normalize_company_name(company_name).lower() if company_name else ""
    # First significant word of company name (≥4 chars) as looser match
    company_words = [w for w in company_core.split() if len(w) >= 4]
    company_first = company_words[0] if company_words else ""

    sector = (canonical.get("fundamentals_yf") or {}).get("sector") or "Equities"
    sym_lower = symbol.lower()

    hl_text = "\n".join(f"{i + 1}. {h}" for i, h in enumerate(headlines))
    prompt = (
        f"Stock trading analyst. For each headline, predict the short-term "
        f"(1-2 week) price impact specifically on {symbol} ({sector} sector).\n\n"
        f"Score -100 to +100:\n"
        f"-100 to -60: Strong negative (stock likely drops 5%+)\n"
        f"-60 to -20: Moderate negative (drops 1-4%)\n"
        f"-20 to +20: Negligible / already priced in\n"
        f"+20 to +60: Moderate positive (rises 1-4%)\n"
        f"+60 to +100: Strong positive (rises 5%+)\n\n"
        f"Key rules:\n"
        f"- Company-specific news scores higher magnitude than macro/sector news\n"
        f"- Analyst upgrades: +20 to +50 (may be priced in)\n"
        f"- Analyst downgrades: -20 to -50\n"
        f"- Earnings beats: +30 to +70 depending on magnitude\n"
        f"- Chip/export restrictions on {symbol}: strong negative\n"
        f"- Unrelated tickers / general market commentary: near 0\n\n"
        f'Output JSON: {{"scores": [n, n, ...]}}\n\n'
        f"Headlines:\n{hl_text}"
    )

    haiku_scores: list[float] | None = None
    last_raw = ""
    last_exc: Exception | None = None

    for attempt in range(2):
        try:
            raw = _haiku_call(prompt, max_tokens=250)
            last_raw = raw
            data = _extract_json(raw)
            scores = data["scores"]
            if not isinstance(scores, list) or len(scores) < len(headlines):
                raise ValueError(
                    f"Expected {len(headlines)} scores, got "
                    f"{len(scores) if isinstance(scores, list) else type(scores).__name__}"
                )
            haiku_scores = [float(s) for s in scores[: len(headlines)]]
            break
        except Exception as exc:
            last_exc = exc
            if attempt == 0:
                log.warning(
                    "%s: news_catalyst parse failed (attempt 1/2), retrying: %s",
                    symbol, exc,
                )

    if haiku_scores is None:
        log.error(
            "%s: news_catalyst failed after 2 attempts: %s | raw: %.200s",
            symbol, last_exc, last_raw,
        )
        _send_alert(symbol, str(last_exc))
        return None

    filtered: list[float] = []
    for i, score in enumerate(haiku_scores):
        hl = headlines[i].lower()
        is_etf = symbol in ETF_SYMBOLS
        if is_etf:
            is_direct = sym_lower in hl
        else:
            is_direct = (
                sym_lower in hl
                or (company_core and company_core in hl)
                or (company_first and company_first in hl)
            )
        if is_direct:
            filtered.append(float(score))
        else:
            # Indirect / sector news: cap at ±20 to reduce noise
            filtered.append(max(-20.0, min(20.0, float(score))))

    avg = sum(filtered) / len(filtered)
    return round((avg + 100) / 2, 1)  # map -100..+100 → 0..100


# ── Symbol scorer ─────────────────────────────────────────────────────────────

def score_symbol(symbol: str, eda: int) -> Optional[dict]:
    """
    Score all 4 components for one symbol.

    Args:
        symbol: Ticker (e.g. "NVDA").
        eda:    Earnings days away (positive = future, 0 = today).

    Returns dict with symbol, eda, components dict; or None if no canonical file.
    """
    path = _SYMBOLS_DIR / f"{symbol}.json"
    if not path.exists():
        log.warning("%s: no canonical file at %s", symbol, path)
        return None

    try:
        canonical = json.loads(path.read_text())
    except Exception as exc:
        log.error("%s: canonical read failed: %s", symbol, exc)
        return None

    log.info("%s (eda=%d): scoring...", symbol, eda)

    components = {
        "analyst_momentum": score_analyst_momentum(canonical),
        "beat_consistency": score_beat_consistency(canonical),
        "insider_activity": score_insider_activity(canonical),
        "news_catalyst":    score_news_catalyst(canonical, symbol),
    }

    non_null = [v for v in components.values() if v is not None]
    composite = round(sum(non_null) / len(non_null), 1) if non_null else None

    log.info(
        "%s: analyst=%.0f beat=%.0f insider=%s news=%s composite=%s",
        symbol,
        components["analyst_momentum"] or 0,
        components["beat_consistency"] or 0,
        f"{components['insider_activity']:.0f}" if components["insider_activity"] is not None else "null",
        f"{components['news_catalyst']:.1f}" if components["news_catalyst"] is not None else "null",
        f"{composite:.1f}" if composite is not None else "null",
    )

    return {
        "symbol":     symbol,
        "eda":        eda,
        "composite":  composite,
        "components": components,
        "scored_at":  datetime.now(timezone.utc).isoformat(),
    }


# ── Full run ──────────────────────────────────────────────────────────────────

def run_conviction_scoring(
    target_date: Optional[date] = None,
    symbols_override: Optional[list[str]] = None,
    lookforward: int = 14,
) -> dict:
    """
    Score all earnings candidates in the upcoming window.

    Args:
        target_date:      Date to use as "today" (default: today).
        symbols_override: If provided, score these symbols at eda=0 regardless
                          of the earnings calendar (useful for testing / manual runs).
        lookforward:      Max DTE window to include (default 14 days).

    Returns dict with generated_at and candidates list.
    """
    today = target_date or date.today()
    candidates: list[dict] = []

    if symbols_override:
        for sym in symbols_override:
            result = score_symbol(sym.upper(), eda=0)
            if result:
                candidates.append(result)
    else:
        # Load earnings calendar
        try:
            from data_warehouse import load_earnings_calendar  # noqa: PLC0415
            cal_raw = load_earnings_calendar()
            entries = cal_raw.get("calendar", []) if isinstance(cal_raw, dict) else []
        except Exception as exc:
            log.error("load_earnings_calendar failed: %s", exc)
            entries = []

        # Universe filter
        tracked: set[str] = set()
        try:
            from universe_manager import get_universe_snapshot  # noqa: PLC0415
            tracked = set(get_universe_snapshot()["all_symbols"])
            log.info("Universe filter: %d symbols", len(tracked))
        except Exception as exc:
            log.warning("universe_manager unavailable — no filter: %s", exc)

        for entry in entries:
            sym = (entry.get("symbol") or "").upper()
            if not sym or "/" in sym:
                continue
            if tracked and sym not in tracked:
                continue
            ed_raw = str(entry.get("earnings_date", ""))[:10]
            try:
                ed  = date.fromisoformat(ed_raw)
                eda = (ed - today).days
            except ValueError:
                continue
            if not (0 <= eda <= lookforward):
                continue
            result = score_symbol(sym, eda)
            if result:
                candidates.append(result)

    candidates.sort(key=lambda c: c.get("composite") or 0, reverse=True)
    log.info("Scored %d candidates", len(candidates))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scored_date":  today.isoformat(),
        "candidates":   candidates,
    }


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    from dotenv import load_dotenv
    load_dotenv(_BASE / ".env")

    target_date: Optional[date] = None
    symbols_override: Optional[list[str]] = None

    args = sys.argv[1:]
    if "--date" in args:
        idx = args.index("--date")
        try:
            target_date = date.fromisoformat(args[idx + 1])
        except (IndexError, ValueError) as exc:
            log.error("Invalid --date value: %s", exc)
            sys.exit(1)
    if "--symbols" in args:
        idx = args.index("--symbols")
        symbols_override = [s.upper() for s in args[idx + 1:] if not s.startswith("--")]

    result = run_conviction_scoring(
        target_date=target_date,
        symbols_override=symbols_override,
    )

    output_date = target_date or date.today()
    _CONVICTIONS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _CONVICTIONS_DIR / f"{output_date}_scores.json"
    out_path.write_text(json.dumps(result, indent=2))
    log.info("Output: %s (%d candidates)", out_path, len(result["candidates"]))


if __name__ == "__main__":
    main()
