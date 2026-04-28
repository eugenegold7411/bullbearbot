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
_BRIEF_FILE = _DATA_DIR / "morning_brief.json"

_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_MODEL  = "claude-sonnet-4-6"

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
}"""


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

    # Earnings calendar (today + tomorrow) — use the structured-date format
    # helper so every line renders as "reports YYYY-MM-DD (N days away)".
    # This avoids relative phrases ("reports today", "reports this week") that
    # Claude can misinterpret as same-session when the brief is hours old.
    try:
        from datetime import date as _date  # noqa: PLC0415

        from data_warehouse import load_earnings_calendar
        from earnings_calendar_lookup import format_earnings_line  # noqa: PLC0415
        ec = load_earnings_calendar()
        today_dt = _date.today()
        today_str = today_dt.isoformat()
        upcoming = [
            e for e in ec.get("calendar", [])
            if e.get("earnings_date", "") >= today_str
        ][:5]
        if upcoming:
            parts.append("\n=== EARNINGS (today + upcoming) ===")
            for e in upcoming:
                sym = e.get("symbol", "?")
                iso = str(e.get("earnings_date", ""))[:10]
                try:
                    n_days = (_date.fromisoformat(iso) - today_dt).days
                except Exception:
                    n_days = None
                parts.append("  " + format_earnings_line(sym, n_days, iso))
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
    dropped with a WARNING."""
    if not isinstance(brief, dict):
        return brief
    picks = brief.get("conviction_picks") or []
    if not picks:
        return brief
    current_prices = _current_prices_from_disk()
    kept: list = []
    for pick in picks:
        if not isinstance(pick, dict):
            continue
        sym = (pick.get("symbol") or "").upper()
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
            system=_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
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


if __name__ == "__main__":
    brief = generate_morning_brief()
    print(json.dumps(brief, indent=2))
