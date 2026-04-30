"""
morning_brief.py — Pre-market morning conviction brief.

Synthesises all available overnight intelligence into 3-5 high-conviction
trade ideas for the day via a single Claude API call.

Runs at 4:15 AM ET weekdays (after data_warehouse + scanner complete).
Output saved to data/market/morning_brief.json and archived.
Injects into all market-session prompt cycles via fetch_all() in market_data.py.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from log_setup import get_logger

load_dotenv()
log = get_logger(__name__)

_BASE_DIR   = Path(__file__).parent
_DATA_DIR   = _BASE_DIR / "data" / "market"
_ARCHIVE    = _BASE_DIR / "data" / "archive"
_BRIEF_FILE        = _DATA_DIR / "morning_brief.json"
_FULL_BRIEF_FILE   = _DATA_DIR / "morning_brief_full.json"
_SONNET_BRIEF_FILE = _DATA_DIR / "morning_brief_sonnet.json"

_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_MODEL  = "claude-sonnet-4-6"

_INTELLIGENCE_SYSTEM = """You are a senior portfolio manager producing a structured intelligence brief for an autonomous AI trading bot.

Return ONLY valid JSON. CRITICAL JSON SAFETY RULES:
- Never use apostrophes (') in any string value — use alternative wording
- Never embed double quotes inside string values — rephrase instead
- All string values must be plain ASCII text; no special characters
- Keep all text fields under 80 characters
- Validate that every opening brace has a matching close before outputting
- Never truncate mid-object — if running long, reduce the number of items in arrays

Top-level keys (in order):
market_regime, sector_snapshot, high_conviction_longs, high_conviction_bearish, current_positions, watch_list, earnings_pipeline, insider_activity, macro_wire_alerts, avoid_list, latest_updates

SCHEMAS:
market_regime: {"regime": "risk_on|caution|defensive|risk_off", "score": 0-100, "confidence": "high|medium|low", "vix": float, "tone": "1 sentence max 80 chars", "key_drivers": ["max 3 strings, 40 chars each"], "todays_events": [{"time": str, "event": "max 50 chars", "impact": "high|medium|low"}]}

sector_snapshot: [{"sector": str, "etf": str, "etf_change_pct": float, "status": "LEADING|BULLISH|NEUTRAL|BEARISH|WEAK", "summary": "max 60 chars", "news": ["max 2 headlines, 60 chars each"], "symbols": [str]}]
  status rules: LEADING >+2%, BULLISH 0 to +2%, NEUTRAL, BEARISH -2% to 0%, WEAK < -2%
  Include up to 8 sectors

high_conviction_longs: [{"symbol": str, "score": int, "conviction": "HIGH|MEDIUM", "rank": int, "catalyst": "max 60 chars — no apostrophes", "entry_zone": "EQUITY price range, max 15 chars", "stop": float, "stop_pct": float, "target": float, "target_pct": float, "risk_reward": float, "technical_summary": "max 50 chars", "a2_strategy_note": "iv_rank=N RULE strategy or NA", "risk_note": "max 50 chars"}]
  conviction: HIGH if score>=70, MEDIUM if 50-69
  risk_reward: (target - entry_mid) / (entry_mid - stop) rounded to 1 decimal
  Include up to 12 items ordered by score desc

high_conviction_bearish: same schema as longs, up to 8 items ordered by score desc

current_positions: {"a1_equity": [{"symbol": str, "shares": int, "entry": float, "current": float, "unrealized_pct": float, "unrealized_usd": float, "stop": float, "trail_tier": str, "binary_event_flag": bool, "binary_event_note": str}], "a2_options": [{"symbol": str, "strategy": str, "contracts": int, "fill_price": float, "current_value": float, "pnl_pct": float, "dte": int, "breakeven": float, "target": float, "stop": float, "pct_of_max_gain": float}]}

watch_list: [{"symbol": str, "score": int, "direction": str, "entry_trigger": "max 50 chars"}] — up to 8 items

earnings_pipeline: [{"symbol": str, "timing": "today_postmarket|tomorrow_premarket|tomorrow_postmarket|this_week", "iv_rank": float or null, "beat_history": "max 30 chars", "held_by_a1": bool, "a1_notes": "max 40 chars", "a2_rule": "max 20 chars", "a2_notes": "max 40 chars"}] — up to 6 items

insider_activity: {"high_conviction": ["max 60 chars each, max 3 items"], "congressional": ["max 60 chars each, max 3 items"], "form4_purchases": ["max 60 chars each, max 3 items"]}

macro_wire_alerts: [{"tier": "critical|high|medium", "score": float, "headline": "max 80 chars", "impact": "max 60 chars", "affected_sectors": [str]}] — up to 5 items

avoid_list: [{"symbol": str, "reason": "max 50 chars"}] — up to 6 items

latest_updates: [{"timestamp": "ISO", "category": "new_catalyst|thesis_change|position_update|macro_alert", "symbol": str, "summary": "max 60 chars"}] — empty [] unless brief_type is intraday_update

HARD RULES:
1. entry_zone/stop/target MUST be equity or ETF share prices ONLY — never commodity spot prices
2. a2_strategy_note: iv_rank<15 RULE1_BUY single_leg; 15-35 RULE2_DEBIT debit_spread; 35-65 RULE3_NEUTRAL mixed; 65-80 RULE4_CREDIT credit_spread; >80 RULE5_AVOID; NA if no iv data
3. risk_reward must be calculated
4. latest_updates is always empty [] unless brief_type is intraday_update

OUTPUT COMPLETENESS RULES:
- All 11 top-level keys must be present even if empty (use [] or {} as appropriate).
- sector_snapshot: include ALL sectors with signal data, minimum 4 sectors.
- high_conviction_longs: include any symbol with score >= 65 from signal data.
- high_conviction_bearish: include any symbol with score <= 35 from signal data.
- current_positions: always populate from portfolio data — never omit held positions.
- earnings_pipeline: include all symbols with earnings within 5 trading days.
- insider_activity: populate from insider/congressional data; empty lists if none.
- macro_wire_alerts: include top 3-5 macro wire events by score; empty list if none.
- avoid_list: symbols to flag for avoidance; empty list is valid.

JSON SAFETY (CRITICAL):
- No apostrophes in string values — rephrase using alternative words.
- No embedded double-quotes in strings — rephrase to avoid them.
- All numeric fields must be actual numbers, not strings.
- Validate brace matching before outputting — unclosed objects will crash the parser.
- If the output would exceed the token limit, reduce items in arrays rather than truncating mid-structure."""

_SYSTEM = """You are a senior portfolio manager running a morning briefing for your trading desk. Based on overnight intelligence, identify the 3-5 highest conviction trade ideas for today's session.

For each idea:
- Name the symbol and direction (long/short)
- State the catalyst using the STRUCTURED schema below — never free prose for dates
- Identify the key risk that could invalidate the trade
- Suggest entry zone, stop level, and target for the EQUITY (never for a commodity, index, or underlying asset)
- Rate conviction: high/medium

HARD CONSTRAINTS:
1. `entry_zone` MUST be the equity or option price range you are recommending — NEVER a commodity spot price, index level, or underlying asset price. If the thesis depends on a commodity, reference the commodity separately in `catalyst.short_text`.
2. `catalyst.date_iso` and `catalyst.days_away` MUST come from the earnings calendar data provided — do NOT infer dates. For non-earnings catalysts, emit null for both.
3. `catalyst.short_text` MUST use "in N days" (never "today" / "tonight" / "this week") for any future date reference, where N equals `catalyst.days_away`.
4. If no high-conviction setups, emit `conviction_picks: []` — "no edge today" is a valid output.

TRADE IDEA QUALITY CRITERIA:
- Catalyst clarity: Is there a specific, time-bound event driving the setup? Vague macro themes score lower.
- Technical alignment: Does price action confirm the thesis direction?
- Risk definition: Can you place a clear stop with defined risk?
- Asymmetry: Is the risk/reward at least 2:1 (target distance >= 2x stop distance)?
- Catalyst freshness: Overnight news or data release that the market hasn't fully digested yet.

WHAT MAKES A HIGH CONVICTION PICK:
- Specific near-term catalyst (earnings in 2-5 days, analyst day, FDA date, data print)
- Insider or congressional buying within 30 days
- Technical breakout above key resistance on elevated volume
- Macro tailwind confirmed by sector peers moving in the same direction
- Citrini Research position alignment

WHAT TO AVOID:
- "No news" stocks — avoid if there is no identifiable catalyst for today or this week
- Stocks up >5% pre-market without a clear catalyst (chase risk)
- Symbols with earnings within 24h if not sizing for the binary event
- Overowned positions the bot already holds at high concentration

STOP AND TARGET RULES:
- stop: a specific price level (not a percentage). Must be below entry for longs.
- target: a specific price level. Must be above entry for longs.
- entry_zone: a price range like "118-121" or a single price like "118". Equity prices only.
- Never enter the underlying commodity price as entry_zone (e.g., do NOT write "2,400" for GLD entry — write the GLD share price instead).

Return ONLY valid JSON in this exact format:
{
  "market_tone": "bullish" | "bearish" | "mixed" | "neutral",
  "key_themes": ["theme1", "theme2"],
  "conviction_picks": [
    {
      "symbol": "NVDA",
      "direction": "long",
      "catalyst": {
        "type": "earnings" | "insider" | "macro" | "technical" | "other",
        "date_iso": "YYYY-MM-DD" | null,
        "days_away": <integer> | null,
        "short_text": "max 12 words — use 'in N days' not 'today' for future dates"
      },
      "risk": "Broader tech selloff on rate fears",
      "entry_zone": "118-120",
      "stop": "115",
      "target": "128",
      "conviction": "high"
    }
  ],
  "avoid_today": ["symbol1"],
  "brief_summary": "2 sentence market tone summary"
}

FIELD VALIDATION:
- market_tone: exactly one of bullish/bearish/mixed/neutral.
- key_themes: 2-4 short phrases (< 8 words each) describing today's dominant themes.
- conviction_picks: 3-5 items ordered by conviction descending. Empty list is valid.
- avoid_today: symbols to actively avoid with no rationale needed in the list.
- brief_summary: exactly 2 sentences. Sentence 1: overall market assessment.
  Sentence 2: single most important thing to watch today.
- Return valid JSON only — no markdown fences, no preamble text.

AVOID_TODAY CRITERIA:
- Symbols with earnings today or tomorrow at full size (binary event risk).
- Symbols up >5% pre-market without clear fundamental catalyst.
- Symbols with active class action, SEC investigation, or accounting review.
- Sectors under regulatory scrutiny with pending decisions.
- ETFs with structurally declining volume (liquidity risk).

BRIEF_SUMMARY CONSTRUCTION:
- First sentence: name the regime (bullish/cautious/risk-off), name the key driver
  (Fed, earnings, macro data, geopolitical), and name the dominant sector.
- Second sentence: the single highest-conviction setup or the most important risk to watch.
- Combined length: 30-50 words maximum.
- Example: "Cautious risk-on session led by AI hardware earnings beats; tech sector outperforming.
  NVDA earnings in 3 days represents the clearest setup with defined risk at $850."

MARKET TONE CLASSIFICATION:
- bullish: >60% of signal scores above 55, regime score > 60, VIX below 18.
- bearish: >60% of signal scores below 45, regime score < 40, VIX above 25.
- mixed: Broad dispersion in scores across sectors, no clear directional bias.
- neutral: Scores clustered near 50, VIX 18-25, no dominant catalyst."""


def _load_overnight_digest() -> str:
    """
    Load the most recent Haiku overnight digest and format it for injection
    into the morning brief context. Returns the empty string if no digest
    exists or if the most recent digest is older than 14 hours (stale gate
    matches the morning_brief 24h gate but tighter — overnight digest must be
    same-night).
    """
    digest_dir = _BASE_DIR / "data" / "macro_wire"
    if not digest_dir.exists():
        return ""
    digests = sorted(digest_dir.glob("overnight_digest_*.json"), reverse=True)
    if not digests:
        return ""

    try:
        d = json.loads(digests[0].read_text())
    except Exception:
        return ""

    generated = d.get("generated_at", "")
    if generated:
        try:
            ts = datetime.fromisoformat(generated.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
            if age_hours > 14:
                return ""
        except Exception:
            pass

    lines = ["=== OVERNIGHT MACRO DIGEST ==="]
    summary = d.get("overnight_summary", "")
    if summary:
        lines.append(f"Summary: {summary}")
    if d.get("regime_shift"):
        note = d.get("regime_note") or ""
        lines.append(f"REGIME SHIFT: {note}")
    themes = d.get("macro_themes") or []
    if themes:
        lines.append(f"Themes: {', '.join(themes)}")
    catalysts = d.get("watchlist_catalysts") or {}
    if catalysts:
        lines.append("Watchlist catalysts:")
        for sym, note in list(catalysts.items())[:8]:
            lines.append(f"  {sym}: {note}")
    risk_flags = d.get("risk_flags") or []
    if risk_flags:
        lines.append(f"Risk flags: {', '.join(risk_flags)}")
    top = d.get("top_events") or []
    if top:
        lines.append("Top events:")
        for e in top[:5]:
            syms = ", ".join((e.get("affected_symbols") or [])[:3])
            head = e.get("headline", "?")
            impact = (e.get("impact") or "?").upper()
            suffix = f" ({syms})" if syms else ""
            lines.append(f"  [{impact}] {head}{suffix}")
    lines.append(
        f"[{d.get('events_qualifying', 0)} qualifying events, "
        f"window={d.get('window_hours', '?')}h]"
    )
    return "\n".join(lines)


def _get_held_symbols() -> set[str]:
    """Return set of symbols currently held in Account 1.

    Strategy:
    1. Scan the last 50 decisions for any that contain hold entries
       (market-session decisions track holds; extended/overnight do not).
    2. If none found (e.g. at 4 AM before the first market cycle), fall back
       to reading live Alpaca positions — non-fatal.
    """
    # Try decisions.json first (no network call)
    try:
        dec_path = _BASE_DIR / "memory" / "decisions.json"
        if dec_path.exists():
            data = json.loads(dec_path.read_text())
            if isinstance(data, list) and data:
                for d in reversed(data[-50:]):
                    holds = d.get("holds", [])
                    if holds:
                        return {
                            (h.get("symbol", "") if isinstance(h, dict) else str(h))
                            for h in holds
                            if h
                        }
    except Exception:
        pass

    # Fallback: live Alpaca positions (non-fatal; used at 4 AM when no recent market decisions)
    try:
        from dotenv import load_dotenv as _lde  # noqa: PLC0415
        _lde()
        from alpaca.trading.client import TradingClient  # noqa: PLC0415
        _a1 = TradingClient(
            os.getenv("ALPACA_API_KEY"),
            os.getenv("ALPACA_SECRET_KEY"),
            paper=True,
        )
        return {p.symbol for p in _a1.get_all_positions()}
    except Exception:
        return set()


def _load_analyst_intel(sym: str):
    """Load cached analyst intel for sym. Returns None if not cached or on failure."""
    try:
        import earnings_intel_fetcher as eif  # noqa: PLC0415
        return eif.load_analyst_intel_cached(sym)
    except Exception:
        return None


def _build_pre_earnings_intel_section() -> str:
    """
    Return a formatted '=== PRE-EARNINGS INTELLIGENCE ===' section using a
    tiered priority ordering that guarantees held positions are always included:

      T0 — held + ≤ 2 days  (uncapped)
      T1 — held + 3–5 days  (uncapped)
      T2 — not held + ≤ 1 day  (max 3)
      T3 — not held + 2–5 days (max 2)
      T4 — > 5 days         (excluded)

    Each entry includes analyst intel (beat history + consensus) from the
    24h-cached earnings_intel_fetcher data, plus the EDGAR transcript analysis
    from earnings_intel.get_earnings_intel_section().
    Non-fatal — returns '' on any exception.
    """
    try:
        from datetime import date as _date  # noqa: PLC0415

        from data_warehouse import load_earnings_calendar  # noqa: PLC0415
        from earnings_intel import get_earnings_intel_section  # noqa: PLC0415

        ec = load_earnings_calendar()
        today_dt = _date.today()
        today_str = today_dt.isoformat()
        held = _get_held_symbols()

        # Bucket into tiers
        t0: list[tuple[str, int]] = []  # held + ≤2d
        t1: list[tuple[str, int]] = []  # held + 3–5d
        t2: list[tuple[str, int]] = []  # !held + ≤1d
        t3: list[tuple[str, int]] = []  # !held + 2–5d

        for e in ec.get("calendar", []):
            sym = e.get("symbol", "")
            iso = str(e.get("earnings_date", ""))[:10]
            if not sym or not iso or iso < today_str:
                continue
            try:
                n_days = (_date.fromisoformat(iso) - today_dt).days
            except Exception:
                continue
            if n_days > 5:
                continue
            is_held = sym in held
            if is_held and n_days <= 2:
                t0.append((sym, n_days))
            elif is_held:
                t1.append((sym, n_days))
            elif n_days <= 1:
                t2.append((sym, n_days))
            else:
                t3.append((sym, n_days))

        # Apply caps: T0/T1 uncapped; T2 max 3; T3 max 2
        to_process = t0 + t1 + t2[:3] + t3[:2]
        if not to_process:
            return ""

        lines = ["\n=== PRE-EARNINGS INTELLIGENCE ==="]
        for sym, n_days in to_process:
            held_tag = " [HELD]" if sym in held else ""
            header = f"  {sym}{held_tag} (earnings in {n_days}d):"
            lines.append(header)

            # Analyst intel (from 24h cache — no network call)
            intel = _load_analyst_intel(sym)
            if intel:
                try:
                    import earnings_intel_fetcher as eif  # noqa: PLC0415
                    ai_text = eif.format_analyst_intel_text(intel)
                    if ai_text:
                        lines.append(f"    {ai_text}")
                except Exception:
                    pass

            # EDGAR transcript / earnings analysis
            section = get_earnings_intel_section(sym, n_days)
            lines.append(section)

        return "\n".join(lines)
    except Exception as exc:
        log.debug("[MORNING] pre-earnings intel section failed: %s", exc)
        return ""


def _load_context() -> str:
    """Assemble all available overnight intelligence into a context string."""
    parts = []

    # Overnight Haiku digest (if generated at 4:00 AM by the scheduler) —
    # placed first so Sonnet sees synthesized macro intel before raw indices.
    overnight = _load_overnight_digest()
    if overnight:
        parts.append(overnight)

    # Global session handoff
    try:
        from data_warehouse import load_global_indices
        gi = load_global_indices()
        if gi.get("indices"):
            parts.append("=== GLOBAL SESSION (overnight) ===")
            for ticker, v in gi["indices"].items():
                parts.append(f"  {v['name']}: {v['chg_pct']:+.1f}%")
    except Exception:
        pass

    # VIX + macro snapshot
    # Commodity prices are labelled explicitly to prevent the author from
    # confusing WTI crude spot with XLE or USO equity prices. If an equity
    # tracks a commodity (XLE → energy sector, USO → WTI), its price is
    # emitted alongside the commodity spot so the contrast is obvious.
    xle_price = None
    uso_price = None
    try:
        from data_warehouse import load_bars_cached
        _xle = load_bars_cached("XLE") or []
        if _xle:
            xle_price = float(_xle[-1].get("close", 0) or 0) or None
        _uso = load_bars_cached("USO") or []
        if _uso:
            uso_price = float(_uso[-1].get("close", 0) or 0) or None
    except Exception:
        pass
    try:
        from data_warehouse import load_macro_snapshot
        macro = load_macro_snapshot()
        # T1-3: macro snapshot may store vix as {"price": N, "chg_pct": M} dict OR float
        _vix_snap = macro.get("vix", {})
        if isinstance(_vix_snap, dict) and _vix_snap:
            vix = _vix_snap
        elif isinstance(_vix_snap, (int, float)) and _vix_snap:
            vix = {"price": round(float(_vix_snap), 2), "chg_pct": 0}
        else:
            vix = {}
        if vix:
            parts.append(f"\nVIX: {vix.get('price','?')} ({vix.get('chg_pct',0):+.1f}%)")
        oil = macro.get("oil", {})
        if oil:
            parts.append(
                f"WTI crude oil spot (NOT equity price): "
                f"${oil.get('price','?')} ({oil.get('chg_pct',0):+.1f}%)"
            )
            if xle_price:
                parts.append(f"XLE ETF price: ${xle_price:.2f}")
            if uso_price:
                parts.append(f"USO ETF price: ${uso_price:.2f}")
    except Exception:
        pass

    # Sector performance yesterday
    try:
        from data_warehouse import load_sector_perf
        sp = load_sector_perf()
        sectors = sp.get("sectors", {})
        if sectors:
            parts.append("\n=== SECTOR PERFORMANCE (yesterday) ===")
            for sec, d in sorted(sectors.items(), key=lambda x: x[1].get("day_chg", 0), reverse=True)[:5]:
                parts.append(f"  {sec}: day {d.get('day_chg',0):+.1f}%  week {d.get('week_chg',0):+.1f}%")
    except Exception:
        pass

    # Pre-market movers
    try:
        premarket_path = _BASE_DIR / "data" / "market" / "premarket_movers.json"
        if premarket_path.exists():
            pm = json.loads(premarket_path.read_text())
            top_up   = pm.get("top_up",   [])[:5]
            top_down = pm.get("top_down", [])[:3]
            if top_up:
                parts.append("\n=== PRE-MARKET MOVERS ===")
                for m in top_up:
                    parts.append(f"  {m['symbol']}: {m['chg_pct']:+.1f}%  (${m['pre_price']:.2f})")
                for m in top_down[:3]:
                    parts.append(f"  {m['symbol']}: {m['chg_pct']:+.1f}%")
    except Exception:
        pass

    # Scanner candidates from this morning
    try:
        scanner_path = _BASE_DIR / "data" / "scanner" / "daily_candidates.json"
        if scanner_path.exists():
            sc = json.loads(scanner_path.read_text())
            candidates = sc.get("candidates", [])[:5]
            if candidates:
                parts.append("\n=== SCANNER CANDIDATES (4 AM scan) ===")
                for c in candidates:
                    parts.append(
                        f"  {c.get('symbol','?')}  score={c.get('score',0):.2f}  "
                        f"reason={c.get('reason','?')}  catalyst={c.get('catalyst','?')[:60]}"
                    )
    except Exception:
        pass

    # Earnings calendar (today + upcoming ≤5 days) — enriched with timing,
    # IV rank, beat history, and A1 held status.
    # Date anchoring via format_earnings_line() avoids relative-phrase drift
    # when the brief is hours old.
    try:
        from datetime import date as _date  # noqa: PLC0415

        from data_warehouse import load_earnings_calendar
        from earnings_calendar_lookup import format_earnings_line  # noqa: PLC0415
        ec = load_earnings_calendar()
        today_dt = _date.today()
        today_str = today_dt.isoformat()
        upcoming = []
        for _e in ec.get("calendar", []):
            _iso = str(_e.get("earnings_date", ""))[:10]
            if not _iso or _iso < today_str:
                continue
            try:
                if (_date.fromisoformat(_iso) - today_dt).days > 5:
                    continue
            except Exception:
                continue
            upcoming.append(_e)
            if len(upcoming) >= 8:
                break

        if upcoming:
            # Preload IV ranks and held symbols once for all entries
            _earn_syms = [e.get("symbol", "") for e in upcoming]
            _iv_map    = _load_iv_ranks_for_brief(_earn_syms)
            _held_syms = _get_held_symbols()

            parts.append("\n=== EARNINGS PIPELINE (≤5 days, enriched) ===")
            for e in upcoming:
                sym    = e.get("symbol", "?")
                iso    = str(e.get("earnings_date", ""))[:10]
                timing = e.get("timing", "unknown")
                try:
                    n_days = (_date.fromisoformat(iso) - today_dt).days
                except Exception:
                    n_days = None

                base = format_earnings_line(sym, n_days, iso)
                extras: list[str] = [timing]

                iv_rank = _iv_map.get(sym)
                if iv_rank is not None:
                    extras.append(f"iv_rank={iv_rank:.0f}")

                # Beat history from analyst intel cache (no network call)
                try:
                    import earnings_intel_fetcher as _eif  # noqa: PLC0415
                    _intel = _eif.load_analyst_intel_cached(sym)
                    if _intel:
                        beats = _intel.get("beat_quarters")
                        total = _intel.get("total_quarters")
                        avg_s = _intel.get("avg_surprise_pct")
                        if beats is not None and total:
                            surp = f" avg {avg_s:+.1f}%" if avg_s is not None else ""
                            extras.append(f"beat {beats}/{total}{surp}")
                except Exception:
                    pass

                if sym in _held_syms:
                    extras.append("HELD-A1")

                parts.append(f"  {base} — {' | '.join(extras)}")
    except Exception:
        pass

    # Pre-earnings intelligence (transcript analysis for symbols ≤ 5 days away)
    _pre_earnings_section = _build_pre_earnings_intel_section()
    if _pre_earnings_section:
        parts.append(_pre_earnings_section)

    # Congressional + insider activity (last 48h)
    try:
        import watchlist_manager as wm
        from insider_intelligence import (
            fetch_congressional_trades,
            fetch_form4_insider_trades,
        )
        wl = wm.get_active_watchlist()
        all_syms = [s["symbol"] for s in wl["all"]]
        cong  = [t for t in fetch_congressional_trades(all_syms, days_back=2)]
        form4 = [t for t in fetch_form4_insider_trades(all_syms, days_back=2)]
        if cong or form4:
            parts.append("\n=== INSIDER ACTIVITY (last 48h) ===")
            for t in cong[:3]:
                parts.append(
                    f"  {t['ticker']}: {t.get('politician','?')} "
                    f"{'BOUGHT' if t.get('action')=='buy' else 'SOLD'} "
                    f"{t.get('amount_range','?')}"
                )
            for t in form4[:3]:
                parts.append(f"  {t['ticker']}: Insider Form 4 filed {t.get('filing_date','?')}")
    except Exception:
        pass

    # Account 2 open options structures
    try:
        from options_state import (  # noqa: PLC0415
            format_a2_closed_today,
            format_a2_summary_section,
        )
        _a2_open   = format_a2_summary_section()
        _a2_closed = format_a2_closed_today()
        parts.append("\n=== ACCOUNT 2 (OPTIONS) OPEN POSITIONS ===")
        parts.append(_a2_open)
        if _a2_closed and "no A2 close events today" not in _a2_closed:
            parts.append("Closed yesterday:")
            parts.append(_a2_closed)
    except Exception:
        pass

    # Inject current prices for held positions so Claude generates accurate entry zones.
    # Without this, Claude uses training-data prices (e.g. GOOGL ~$160) which then fail
    # the post-generation staleness filter and silently drop the pick.
    try:
        _held_syms = _get_held_symbols()
        if _held_syms:
            from data_warehouse import load_bars_cached  # noqa: PLC0415
            _held_price_lines: list[str] = []
            for _sym in sorted(_held_syms):
                _bars = load_bars_cached(_sym) or []
                if _bars:
                    _price = float(_bars[-1].get("close", 0) or 0)
                    if _price > 0:
                        _held_price_lines.append(f"  {_sym}: ${_price:.2f} (currently held)")
            if _held_price_lines:
                parts.append(
                    "\n=== HELD POSITIONS — CURRENT PRICES (use for entry_zone) ==="
                )
                parts.extend(_held_price_lines)
    except Exception:
        pass

    return "\n".join(parts) if parts else "No overnight intelligence available."


def _parse_entry_zone_midpoint(ez: str) -> float | None:
    """Parse "91.50-92.50" / "$91.50-$92.50" / "91.5 to 92.5" → midpoint float."""
    if not ez:
        return None
    import re as _re
    nums = _re.findall(r"\d+(?:\.\d+)?", str(ez))
    if not nums:
        return None
    try:
        vals = [float(n) for n in nums[:2]]
    except Exception:
        return None
    if len(vals) == 1:
        return vals[0]
    return (vals[0] + vals[1]) / 2.0


def _current_prices_from_disk() -> dict:
    """Best-effort snapshot of last-known equity prices from cached bars.
    Used by the morning-brief post-gen validator. Non-fatal."""
    prices: dict = {}
    try:
        import watchlist_manager as _wm  # noqa: PLC0415
        from data_warehouse import load_bars_cached
        wl = _wm.get_active_watchlist()
        for sym in wl.get("stocks", []) + wl.get("etfs", []):
            try:
                bars = load_bars_cached(sym) or []
                if bars:
                    prices[sym] = float(bars[-1].get("close", 0) or 0)
            except Exception:
                pass
    except Exception:
        pass
    return prices


def _validate_and_sanitize_brief(brief: dict) -> dict:
    """Drop picks whose entry_zone is implausible for the symbol's actual
    price (ratio > 2.0x or < 0.5x vs last-known close). This catches the
    XLE/WTI confusion failure mode — if Sonnet's entry_zone midpoint is
    within 50-200% of the equity price, the pick is kept; otherwise it's
    dropped with a WARNING.

    Held positions are exempt: a symbol we already own must always surface
    in the brief regardless of whether the entry_zone is stale."""
    if not isinstance(brief, dict):
        return brief
    picks = brief.get("conviction_picks") or []
    if not picks:
        return brief
    current_prices = _current_prices_from_disk()
    held = _get_held_symbols()
    kept: list = []
    for pick in picks:
        if not isinstance(pick, dict):
            continue
        sym = (pick.get("symbol") or "").upper()
        if sym in held:
            kept.append(pick)
            continue
        ez  = pick.get("entry_zone", "")
        cp  = current_prices.get(sym)
        if sym and cp and cp > 0:
            mid = _parse_entry_zone_midpoint(ez)
            if mid and mid > 0:
                ratio = mid / cp
                if ratio > 2.0 or ratio < 0.5:
                    log.warning(
                        "[MORNING] dropping %s — entry_zone=%s midpoint=$%.2f "
                        "vs price=$%.2f (ratio=%.2fx)",
                        sym, ez, mid, cp, ratio,
                    )
                    continue
        kept.append(pick)
    if len(kept) != len(picks):
        brief = dict(brief)
        brief["conviction_picks"] = kept
        brief["_validation_note"] = (
            f"{len(picks) - len(kept)} pick(s) dropped by entry_zone sanity check"
        )
    return brief


def _save_brief(brief: dict) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    brief["generated_at"] = datetime.now().isoformat()
    _BRIEF_FILE.write_text(json.dumps(brief, indent=2))

    # Archive
    today = datetime.now().strftime("%Y-%m-%d")
    archive_dir = _ARCHIVE / today
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / "morning_brief.json").write_text(json.dumps(brief, indent=2))


def load_morning_brief() -> dict:
    """Load today's morning brief from cache. Returns {} if not yet generated."""
    if not _BRIEF_FILE.exists():
        return {}
    try:
        return json.loads(_BRIEF_FILE.read_text())
    except Exception:
        return {}


def format_morning_brief_section() -> str:
    """Format the morning brief for prompt injection."""
    brief = load_morning_brief()
    if not brief or not brief.get("brief_summary"):
        return "  (morning brief not yet generated — runs at 4:15 AM ET)"

    # Staleness gate: reject briefs older than 24 hours
    _gen = brief.get("generated_at", "")
    if _gen:
        try:
            _gen_dt = datetime.fromisoformat(_gen)
            if _gen_dt.tzinfo is None:
                _gen_dt = _gen_dt.replace(tzinfo=timezone.utc)
            _age_h = (datetime.now(timezone.utc) - _gen_dt.astimezone(timezone.utc)).total_seconds() / 3600
            if _age_h > 24:
                log.warning(
                    "[MORNING] morning_brief.json is stale (%.1fh old) — injecting placeholder",
                    _age_h,
                )
                _gen_date = _gen[:10]
                return (
                    f"  (morning brief unavailable — last generated {_gen_date}, "
                    f"{_age_h:.0f}h ago)"
                )
        except Exception:
            pass

    tone   = brief.get("market_tone", "?").upper()
    themes = ", ".join(brief.get("key_themes", []))
    lines  = [
        f"  Market tone: {tone}  |  Themes: {themes}",
        f"  {brief.get('brief_summary','')}",
    ]

    picks = brief.get("conviction_picks", [])
    if picks:
        lines.append("\n  Conviction picks:")
        for p in picks:
            conv = p.get("conviction", "?").upper()
            lines.append(
                f"    [{conv}] {p.get('symbol','?')} {p.get('direction','?').upper()}  "
                f"entry={p.get('entry_zone','?')}  stop={p.get('stop','?')}  "
                f"target={p.get('target','?')}"
            )
            # catalyst may be the legacy free-prose string OR the new structured dict
            cat_raw = p.get("catalyst", "?")
            if isinstance(cat_raw, dict):
                ctype = cat_raw.get("type", "other")
                days  = cat_raw.get("days_away")
                iso   = cat_raw.get("date_iso")
                text  = cat_raw.get("short_text", "")
                date_suffix = ""
                if days is not None:
                    date_suffix = f" (in {days}d"
                    if iso:
                        date_suffix += f", {iso}"
                    date_suffix += ")"
                cat_line = f"[{ctype}] {text}{date_suffix}"
            else:
                cat_line = str(cat_raw)
            lines.append(f"       catalyst: {cat_line}")
            lines.append(f"       risk:     {p.get('risk','?')}")

    avoid = brief.get("avoid_today", [])
    if avoid:
        lines.append(f"\n  Avoid today: {', '.join(avoid)}")

    gen = brief.get("generated_at", "")
    if gen:
        lines.append(f"\n  (generated at {gen[:16]})")

    return "\n".join(lines)


def generate_morning_brief() -> dict:
    """
    Single Claude call synthesising overnight intelligence into conviction picks.
    Saves to data/market/morning_brief.json and archives.
    Returns the brief dict (or {} on failure).
    """
    log.info("[MORNING] Generating morning conviction brief")
    context = _load_context()

    user_content = (
        f"Here is the overnight market intelligence for today's briefing:\n\n{context}\n\n"
        "Based on this data, generate today's morning conviction brief. "
        "Reference specific data points. Return ONLY JSON."
    )

    try:
        response = _claude.messages.create(
            model=_MODEL,
            max_tokens=1500,
            system=[{"type": "text", "text": _SYSTEM,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_content}],
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
        raw = response.content[0].text.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        brief = json.loads(raw)
    except Exception as exc:
        log.error("[MORNING] Brief generation failed: %s", exc)
        brief = {
            "market_tone":     "neutral",
            "key_themes":      [],
            "conviction_picks": [],
            "avoid_today":     [],
            "brief_summary":   f"Morning brief generation failed: {exc}",
        }

    # Post-generation sanity check: drop picks where entry_zone is implausible
    # vs the symbol's actual price (catches commodity-price-vs-equity confusion).
    brief = _validate_and_sanitize_brief(brief)

    _save_brief(brief)

    # WhatsApp with top pick
    try:
        _send_whatsapp_brief(brief)
    except Exception as exc:
        log.warning("[MORNING] WhatsApp failed: %s", exc)

    picks = brief.get("conviction_picks", [])
    log.info("[MORNING] Brief generated — tone=%s  picks=%d",
             brief.get("market_tone"), len(picks))
    return brief


def _send_whatsapp_brief(brief: dict) -> None:
    sid   = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    from_ = os.getenv("WHATSAPP_FROM")
    to    = os.getenv("WHATSAPP_TO")

    if not all([sid, token, from_, to]):
        return

    picks = brief.get("conviction_picks", [])
    tone  = brief.get("market_tone", "?").upper()
    if picks:
        top   = picks[0]
        cat_raw = top.get("catalyst", "?")
        if isinstance(cat_raw, dict):
            cat_text = (cat_raw.get("short_text") or "")[:80]
        else:
            cat_text = str(cat_raw)[:80]
        msg   = (f"MORNING BRIEF [{tone}]: "
                 f"{top.get('symbol')} {top.get('direction','?').upper()} "
                 f"— {cat_text}  "
                 f"stop={top.get('stop')} tgt={top.get('target')}")
    else:
        msg = f"MORNING BRIEF [{tone}]: No high-conviction setups today. {brief.get('brief_summary','')[:100]}"

    try:
        from twilio.rest import Client
        Client(sid, token).messages.create(body=msg[:160], from_=from_, to=to)
        log.info("[MORNING] WhatsApp sent: %s", msg[:80])
    except Exception as exc:
        log.warning("[MORNING] WhatsApp error: %s", exc)


def _load_signal_scores_for_brief() -> list[dict]:
    """Load scored_symbols from signal_scores.json as a list of dicts. Non-fatal.
    Handles both formats: list[dict] and dict{symbol: {score, direction, ...}}."""
    try:
        path = _BASE_DIR / "data" / "market" / "signal_scores.json"
        if not path.exists():
            return []
        d = json.loads(path.read_text())
        if not isinstance(d, dict):
            return []
        raw = d.get("scored_symbols", [])
        # Dict format: {symbol: {score, direction, conviction, ...}}
        if isinstance(raw, dict):
            out: list[dict] = []
            for sym, info in raw.items():
                if isinstance(info, dict):
                    entry = dict(info)
                    entry["symbol"] = sym
                    out.append(entry)
            return out
        # List format: [{symbol, score, direction, ...}]
        if isinstance(raw, list):
            return [s for s in raw if isinstance(s, dict)]
        return []
    except Exception:
        return []


def _load_iv_ranks_for_brief(symbols: list) -> dict:
    """Load most recent IV rank for symbols from iv_history files. Non-fatal."""
    ranks: dict = {}
    iv_dir = _BASE_DIR / "data" / "options" / "iv_history"
    if not iv_dir.exists():
        return ranks
    for sym in symbols:
        try:
            path = iv_dir / f"{sym}_iv_history.json"
            if not path.exists():
                continue
            history = json.loads(path.read_text())
            if history and isinstance(history, list):
                latest = history[-1]
                rank = latest.get("iv_rank") or latest.get("iv_percentile")
                if rank is not None:
                    ranks[sym] = float(rank)
        except Exception:
            pass
    return ranks


def _load_intelligence_context(brief_type: str) -> str:
    """Assemble full context for intelligence brief generation."""
    import zoneinfo as _zi
    ET_ZONE = _zi.ZoneInfo("America/New_York")
    now_et = datetime.now(ET_ZONE)
    parts: list[str] = []

    parts.append(f"Brief type: {brief_type}")
    parts.append(f"Current time: {now_et.strftime('%Y-%m-%d %H:%M ET, %A')}")

    # Signal scores
    signals = _load_signal_scores_for_brief()
    if signals:
        bullish = sorted(
            [s for s in signals if s.get("direction", "").lower() in ("bullish", "long", "") or s.get("score", 0) >= 50],
            key=lambda x: x.get("score", 0), reverse=True
        )
        bearish = sorted(
            [s for s in signals if s.get("direction", "").lower() in ("bearish", "short") or s.get("score", 0) < 40],
            key=lambda x: x.get("score", 0)
        )
        parts.append(f"\n=== SIGNAL SCORES ({len(signals)} symbols) ===")
        for s in bullish[:25]:
            sym = s.get("symbol", "?")
            score = s.get("score", 0)
            direction = s.get("direction", "?")
            cat = str(s.get("catalyst", ""))[:70]
            parts.append(f"  {sym}: score={score} dir={direction} catalyst={cat}")
        if bearish:
            parts.append("  [BEARISH SIGNALS]")
            for s in bearish[:10]:
                parts.append(f"  {s.get('symbol','?')}: score={s.get('score',0)} dir={s.get('direction','?')}")

        # IV ranks for top signals
        top_syms = [s.get("symbol", "") for s in bullish[:20] + bearish[:10] if s.get("symbol")]
        iv_ranks = _load_iv_ranks_for_brief(top_syms)
        if iv_ranks:
            parts.append("  IV Ranks (for A2 strategy notes):")
            for sym, rank in sorted(iv_ranks.items(), key=lambda x: x[1], reverse=True)[:15]:
                env = "cheap" if rank < 25 else ("neutral" if rank < 60 else ("expensive" if rank < 80 else "very_expensive"))
                parts.append(f"    {sym}: iv_rank={rank:.0f} ({env})")

    # Overnight context (reuse existing _load_context() data sources)
    overnight = _load_overnight_digest()
    if overnight:
        parts.append(overnight)

    # Global session + VIX + macro
    try:
        from data_warehouse import load_global_indices  # noqa: PLC0415
        gi = load_global_indices()
        if gi.get("indices"):
            parts.append("\n=== GLOBAL SESSION ===")
            for ticker, v in gi["indices"].items():
                parts.append(f"  {v.get('name', ticker)}: {v.get('chg_pct', 0):+.1f}%")
    except Exception:
        pass

    try:
        from data_warehouse import load_macro_snapshot  # noqa: PLC0415
        macro = load_macro_snapshot()
        vix_snap = macro.get("vix", {})
        if isinstance(vix_snap, dict) and vix_snap:
            vix_val = vix_snap.get("price", "?")
            vix_chg = vix_snap.get("chg_pct", 0)
        elif isinstance(vix_snap, (int, float)):
            vix_val = round(float(vix_snap), 2)
            vix_chg = 0
        else:
            vix_val = None
        if vix_val:
            parts.append(f"\nVIX: {vix_val} ({vix_chg:+.1f}%)")
        oil = macro.get("oil", {})
        if oil:
            parts.append(f"WTI crude spot (NOT equity price): ${oil.get('price','?')} ({oil.get('chg_pct',0):+.1f}%)")
    except Exception:
        pass

    # Sector performance with ETF prices
    try:
        from data_warehouse import load_bars_cached, load_sector_perf  # noqa: PLC0415
        sp = load_sector_perf()
        sectors = sp.get("sectors", {})
        if sectors:
            parts.append("\n=== SECTOR PERFORMANCE ===")
            for sec, d in sorted(sectors.items(), key=lambda x: x[1].get("day_chg", 0), reverse=True):
                day = d.get("day_chg", 0)
                wk = d.get("week_chg", 0)
                etf = d.get("etf", "")
                etf_price = ""
                if etf:
                    try:
                        bars = load_bars_cached(etf) or []
                        if bars:
                            price = float(bars[-1].get("close", 0) or 0)
                            if price > 0:
                                etf_price = f" | {etf}=${price:.2f}"
                    except Exception:
                        pass
                parts.append(f"  {sec}: day {day:+.1f}% week {wk:+.1f}%{etf_price}")
    except Exception:
        pass

    # Current prices for top signal symbols
    try:
        from data_warehouse import load_bars_cached as _lbc  # noqa: PLC0415
        if signals:
            top_syms_price = [s.get("symbol", "") for s in signals[:30] if s.get("symbol")]
            price_lines: list[str] = []
            for sym in top_syms_price:
                try:
                    bars = _lbc(sym) or []
                    if bars:
                        price = float(bars[-1].get("close", 0) or 0)
                        if price > 0:
                            price_lines.append(f"  {sym}: ${price:.2f}")
                except Exception:
                    pass
            if price_lines:
                parts.append("\n=== CURRENT PRICES (use for entry_zone/stop/target) ===")
                parts.extend(price_lines)
    except Exception:
        pass

    # Earnings calendar (≤5 days, enriched with timing / IV rank / beat history)
    try:
        from datetime import date as _date  # noqa: PLC0415

        from data_warehouse import load_earnings_calendar  # noqa: PLC0415
        ec = load_earnings_calendar()
        today_dt  = _date.today()
        today_str = today_dt.isoformat()
        upcoming = []
        for _e in ec.get("calendar", []):
            _iso = str(_e.get("earnings_date", ""))[:10]
            if not _iso or _iso < today_str:
                continue
            try:
                if (_date.fromisoformat(_iso) - today_dt).days > 5:
                    continue
            except Exception:
                continue
            upcoming.append(_e)
            if len(upcoming) >= 8:
                break
        held = _get_held_symbols()
        if upcoming:
            _earn_syms = [e.get("symbol", "") for e in upcoming]
            _iv_map    = _load_iv_ranks_for_brief(_earn_syms)
            parts.append("\n=== EARNINGS PIPELINE (≤5 days, enriched) ===")
            for e in upcoming:
                sym    = e.get("symbol", "?")
                iso    = str(e.get("earnings_date", ""))[:10]
                timing = e.get("timing", "unknown")
                try:
                    n = (_date.fromisoformat(iso) - today_dt).days
                except Exception:
                    n = None

                n_str = (f"in {n}d" if n is not None and n > 1
                         else ("today" if n == 0 else "tomorrow"))
                extras: list[str] = [timing]

                iv_rank = _iv_map.get(sym)
                if iv_rank is not None:
                    extras.append(f"iv_rank={iv_rank:.0f}")

                try:
                    import earnings_intel_fetcher as _eif  # noqa: PLC0415
                    _intel = _eif.load_analyst_intel_cached(sym)
                    if _intel:
                        beats = _intel.get("beat_quarters")
                        total = _intel.get("total_quarters")
                        avg_s = _intel.get("avg_surprise_pct")
                        if beats is not None and total:
                            surp = f" avg {avg_s:+.1f}%" if avg_s is not None else ""
                            extras.append(f"beat {beats}/{total}{surp}")
                except Exception:
                    pass

                held_tag = " [HELD-A1]" if sym in held else ""
                parts.append(f"  {sym}{held_tag}: {n_str} ({' | '.join(extras)})")
    except Exception:
        pass

    # Pre-earnings intel section
    _pe_section = _build_pre_earnings_intel_section()
    if _pe_section:
        parts.append(_pe_section)

    # Insider / congressional activity
    try:
        import watchlist_manager as wm  # noqa: PLC0415
        from insider_intelligence import (  # noqa: PLC0415
            fetch_congressional_trades,
            fetch_form4_insider_trades,
        )
        wl = wm.get_active_watchlist()
        all_syms = [s["symbol"] for s in wl["all"]]
        cong  = list(fetch_congressional_trades(all_syms, days_back=5))
        form4 = list(fetch_form4_insider_trades(all_syms, days_back=5))
        if cong or form4:
            parts.append("\n=== INSIDER ACTIVITY (last 5 days) ===")
            for t in cong[:5]:
                parts.append(
                    f"  CONGRESSIONAL: {t['ticker']} — {t.get('politician','?')} "
                    f"{'BOUGHT' if t.get('action')=='buy' else 'SOLD'} {t.get('amount_range','?')}"
                )
            for t in form4[:5]:
                parts.append(
                    f"  FORM4: {t['ticker']} — Insider filed {t.get('filing_date','?')}"
                )
    except Exception:
        pass

    # Macro wire (recent significant events)
    try:
        from macro_wire import get_recent_events  # noqa: PLC0415
        events = get_recent_events(hours=4, min_score=5.0)
        if events:
            parts.append("\n=== MACRO WIRE (last 4h, score≥5) ===")
            for e in events[:8]:
                parts.append(
                    f"  [{e.get('tier','?').upper()}] score={e.get('score',0):.1f} "
                    f"{e.get('headline','?')[:100]}"
                )
    except Exception:
        pass

    # A2 open structures
    try:
        from options_state import format_a2_summary_section  # noqa: PLC0415
        _a2_open = format_a2_summary_section()
        if _a2_open and "no open" not in _a2_open.lower():
            parts.append("\n=== A2 OPEN OPTIONS STRUCTURES ===")
            parts.append(_a2_open)
    except Exception:
        pass

    # Previous brief summary (for intraday diff)
    if brief_type == "intraday_update":
        try:
            prev = load_intelligence_brief()
            if prev:
                prev_longs = [(s.get("symbol"), s.get("score")) for s in prev.get("high_conviction_longs", [])[:10]]
                if prev_longs:
                    parts.append("\n=== PREVIOUS BRIEF (for diff) ===")
                    prev_gen = prev.get("generated_at", "?")[:16]
                    parts.append(f"  Generated: {prev_gen}")
                    parts.append("  Previous top longs: " + ", ".join(f"{sym}({sc})" for sym, sc in prev_longs))
                    prev_avoid = [a.get("symbol") for a in prev.get("avoid_list", [])[:5]]
                    if prev_avoid:
                        parts.append("  Previous avoid list: " + ", ".join(prev_avoid))
        except Exception:
            pass

    # Held positions with current prices
    try:
        held_syms = _get_held_symbols()
        if held_syms:
            from data_warehouse import load_bars_cached as _lbc2  # noqa: PLC0415
            parts.append("\n=== HELD POSITIONS (use for current_positions section) ===")
            for sym in sorted(held_syms):
                bars = _lbc2(sym) or []
                price_str = f"current=${float(bars[-1].get('close',0)):.2f}" if bars else ""
                parts.append(f"  {sym} (currently held A1) {price_str}")
    except Exception:
        pass

    return "\n".join(parts) if parts else "No context available."


def _catalyst_3char(catalyst: str) -> str:
    """Extract a 3-char catalyst code from a catalyst string."""
    c = catalyst.lower()
    if "earn" in c:
        return "ern"
    if "congress" in c or "senator" in c or "representative" in c:
        return "cng"
    if "insider" in c or "form4" in c or "form 4" in c:
        return "ins"
    if "macro" in c or "rate" in c or "fed" in c or "fomc" in c:
        return "mac"
    if "momentum" in c or "breakout" in c or "technical" in c:
        return "tec"
    if "news" in c or "headline" in c:
        return "nws"
    if "beat" in c or "exceed" in c:
        return "bet"
    if "upgrade" in c or "analyst" in c:
        return "upg"
    if "miss" in c or "disappoint" in c:
        return "mis"
    if "sell" in c or "bearish" in c or "short" in c:
        return "sel"
    return "oth"


def _build_conviction_state(full_brief: dict) -> str:
    """Render compressed conviction state string for Sonnet prompt. Target ≤350 tokens (~1400 chars)."""
    import zoneinfo as _zi
    ET_ZONE = _zi.ZoneInfo("America/New_York")
    now_str = datetime.now(ET_ZONE).strftime("%-I:%M %p ET")

    longs = full_brief.get("high_conviction_longs", [])[:20]
    bears = full_brief.get("high_conviction_bearish", [])[:10]

    def fmt_entry(item: dict) -> str:
        sym = item.get("symbol", "?")
        score = item.get("score", 0)
        cat = item.get("catalyst", "")
        code = _catalyst_3char(str(cat))
        return f"{sym}({score},{code})"

    # Build lines
    lines = [f"CONVICTION STATE [updated {now_str}]"]

    if longs:
        # Wrap at ~80 chars per line
        entries = [fmt_entry(s) for s in longs]
        prefix_long = "HIGH LONG:    "
        prefix_cont = "              "
        current_line = prefix_long
        first_line = True
        for i, entry in enumerate(entries):
            if len(current_line) + len(entry) + 2 > 90 and not first_line:
                lines.append(current_line.rstrip())
                current_line = prefix_cont + entry + "  "
            else:
                current_line += entry + "  "
                first_line = False
        if current_line.strip():
            lines.append(current_line.rstrip())

    if bears:
        entries = [fmt_entry(s) for s in bears]
        prefix_bear = "HIGH BEARISH: "
        prefix_cont = "              "
        current_line = prefix_bear
        first_line = True
        for entry in entries:
            if len(current_line) + len(entry) + 2 > 90 and not first_line:
                lines.append(current_line.rstrip())
                current_line = prefix_cont + entry + "  "
            else:
                current_line += entry + "  "
                first_line = False
        if current_line.strip():
            lines.append(current_line.rstrip())

    result = "\n".join(lines)

    # Hard truncation: if over ~1400 chars (~350 tokens), trim from bottom of lists
    if len(result) > 1400:
        result = result[:1400].rsplit("\n", 1)[0] + "\n[truncated — token limit]"

    return result


def _build_regime_line(full_brief: dict) -> str:
    """Single-line regime summary for Sonnet prompt."""
    mr = full_brief.get("market_regime", {})
    regime = mr.get("regime", "?")
    score = mr.get("score", 0)
    vix = mr.get("vix", 0)
    drivers = mr.get("key_drivers", [])
    # Compress drivers to short tags
    tags = []
    for d in drivers[:3]:
        words = str(d).lower().split()
        if words:
            tags.append("_".join(words[:2]))
    tags_str = " ".join(tags) if tags else ""
    return f"{regime}({score}) VIX={vix:.1f} {tags_str}".strip()


def _build_positions_line(full_brief: dict) -> str:
    """Single-line positions summary for Sonnet prompt."""
    a1 = full_brief.get("current_positions", {}).get("a1_equity", [])
    a2 = full_brief.get("current_positions", {}).get("a2_options", [])
    parts: list[str] = []
    for p in a1[:6]:
        sym = p.get("symbol", "?")
        pct = p.get("unrealized_pct", 0)
        sign = "+" if pct >= 0 else ""
        parts.append(f"{sym}{sign}{pct:.1f}%")
    a1_str = " ".join(parts) if parts else "none"
    a2_parts: list[str] = []
    for s in a2[:3]:
        sym = s.get("symbol", "?")
        strat = s.get("strategy", "?")
        a2_parts.append(f"{sym}_{strat}")
    a2_str = " ".join(a2_parts) if a2_parts else "none"
    return f"A1: {a1_str} | A2: {a2_str}"


def _build_avoid_line(full_brief: dict) -> str:
    """Single-line avoid list for Sonnet prompt."""
    avoid = full_brief.get("avoid_list", [])[:8]
    if not avoid:
        return "AVOID: none"
    syms = [a.get("symbol", "?") for a in avoid]
    return "AVOID: " + " ".join(syms)


def _save_intelligence_briefs(full_brief: dict) -> None:
    """Write morning_brief_full.json, morning_brief_sonnet.json, and legacy morning_brief.json."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now().isoformat()
    full_brief["generated_at"] = now_iso

    # Write full brief
    _FULL_BRIEF_FILE.write_text(json.dumps(full_brief, indent=2))

    # Build and write sonnet brief (compressed)
    conviction_state = _build_conviction_state(full_brief)
    regime_line = _build_regime_line(full_brief)
    positions_line = _build_positions_line(full_brief)
    avoid_line = _build_avoid_line(full_brief)

    sonnet_brief = {
        "generated_at": now_iso,
        "brief_type": full_brief.get("brief_type", "unknown"),
        "next_update_at": full_brief.get("next_update_at", ""),
        "conviction_state": conviction_state,
        "regime_line": regime_line,
        "positions_line": positions_line,
        "avoid_line": avoid_line,
    }
    _SONNET_BRIEF_FILE.write_text(json.dumps(sonnet_brief, indent=2))

    # Write legacy morning_brief.json for backward compat
    # Map new format → old format so existing consumers don't break
    mr = full_brief.get("market_regime", {})
    regime = mr.get("regime", "neutral")
    tone_map = {"risk_on": "bullish", "caution": "mixed", "defensive": "mixed", "risk_off": "bearish"}
    legacy_tone = tone_map.get(regime, "neutral")

    longs = full_brief.get("high_conviction_longs", [])[:5]
    legacy_picks = []
    for p in longs:
        legacy_picks.append({
            "symbol": p.get("symbol"),
            "direction": "long",
            "catalyst": {"type": "other", "short_text": p.get("catalyst", "")[:80], "date_iso": None, "days_away": None},
            "risk": p.get("risk_note", ""),
            "entry_zone": p.get("entry_zone", ""),
            "stop": str(p.get("stop", "")),
            "target": str(p.get("target", "")),
            "conviction": "high" if p.get("conviction") == "HIGH" else "medium",
        })
    avoid_syms = [a.get("symbol") for a in full_brief.get("avoid_list", [])[:5] if a.get("symbol")]
    legacy = {
        "market_tone": legacy_tone,
        "key_themes": mr.get("key_drivers", [])[:3],
        "conviction_picks": legacy_picks,
        "avoid_today": avoid_syms,
        "brief_summary": mr.get("tone", ""),
        "generated_at": now_iso,
    }
    _BRIEF_FILE.write_text(json.dumps(legacy, indent=2))

    # Archive full brief
    today = datetime.now().strftime("%Y-%m-%d")
    archive_dir = _ARCHIVE / today
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / "morning_brief_full.json").write_text(json.dumps(full_brief, indent=2))


def load_intelligence_brief() -> dict:
    """Load the full intelligence brief. Returns {} if not yet generated."""
    if not _FULL_BRIEF_FILE.exists():
        return {}
    try:
        return json.loads(_FULL_BRIEF_FILE.read_text())
    except Exception:
        return {}


def load_sonnet_brief() -> dict:
    """Load the compressed sonnet brief. Returns {} if not yet generated."""
    if not _SONNET_BRIEF_FILE.exists():
        return {}
    try:
        return json.loads(_SONNET_BRIEF_FILE.read_text())
    except Exception:
        return {}


def generate_intelligence_brief(brief_type: str = "premarket") -> dict:
    """
    Generate a comprehensive intelligence brief. Saves morning_brief_full.json,
    morning_brief_sonnet.json, and legacy morning_brief.json.
    Returns the full brief dict (or {} on failure).
    brief_type: "premarket" | "market_open" | "intraday_update"
    """
    log.info("[INTELLIGENCE] Generating %s intelligence brief", brief_type)
    context = _load_intelligence_context(brief_type)

    # Compute next_update_at based on brief_type
    import zoneinfo as _zi
    ET_ZONE = _zi.ZoneInfo("America/New_York")
    now_et = datetime.now(ET_ZONE)
    if brief_type == "premarket":
        # Next update: market_open at 9:25 AM
        from datetime import timedelta as _td  # noqa: PLC0415
        next_update = now_et.replace(hour=9, minute=25, second=0, microsecond=0)
        if next_update <= now_et:
            next_update = next_update + _td(days=1)
    elif brief_type == "market_open":
        # Next update: 10:30 AM
        next_update = now_et.replace(hour=10, minute=30, second=0, microsecond=0)
    else:
        # intraday_update: next hour on the slot schedule
        slots = [10, 11, 12, 13, 14, 15]
        next_hour = next((h for h in slots if h > now_et.hour), None)
        if next_hour:
            next_update = now_et.replace(hour=next_hour, minute=30, second=0, microsecond=0)
        else:
            next_update = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    next_update_iso = next_update.isoformat()

    user_content = (
        f"Generate a {brief_type} intelligence brief for the trading bot based on this market data:\n\n"
        f"{context}\n\n"
        f"Return the full JSON document. Be specific with prices and catalysts. "
        f"Use actual signal scores and current prices from the data above."
    )

    try:
        response = _claude.messages.create(
            model=_MODEL,
            max_tokens=8192,
            system=[
                {
                    "type": "text",
                    "text": _INTELLIGENCE_SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_content}],
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        # Attempt direct parse; fall back to outermost-object extraction on failure
        try:
            full_brief = json.loads(raw)
        except json.JSONDecodeError:
            import re as _re
            m = _re.search(r"\{.*\}", raw, _re.DOTALL)
            if m:
                full_brief = json.loads(m.group(0))
            else:
                raise
    except Exception as exc:
        log.error("[INTELLIGENCE] Brief generation failed: %s", exc)
        # Return minimal valid structure
        full_brief = {
            "market_regime": {"regime": "caution", "score": 50, "confidence": "low", "vix": 20.0,
                              "tone": f"Intelligence brief generation failed: {exc}",
                              "key_drivers": [], "todays_events": []},
            "sector_snapshot": [], "high_conviction_longs": [], "high_conviction_bearish": [],
            "current_positions": {"a1_equity": [], "a2_options": []},
            "watch_list": [], "earnings_pipeline": [], "macro_wire_alerts": [],
            "insider_activity": {"high_conviction": [], "congressional": [], "form4_purchases": []},
            "avoid_list": [], "latest_updates": [],
        }

    full_brief["brief_type"] = brief_type
    full_brief["next_update_at"] = next_update_iso

    _save_intelligence_briefs(full_brief)

    picks_count = len(full_brief.get("high_conviction_longs", []))
    log.info("[INTELLIGENCE] Brief complete — type=%s regime=%s longs=%d",
             brief_type, full_brief.get("market_regime", {}).get("regime", "?"), picks_count)
    return full_brief


if __name__ == "__main__":
    brief = generate_morning_brief()
    print(json.dumps(brief, indent=2))
