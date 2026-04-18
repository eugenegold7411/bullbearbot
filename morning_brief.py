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
- State the specific catalyst driving the thesis
- Identify the key risk that could invalidate the trade
- Suggest entry zone, stop level, and target
- Rate conviction: high/medium

Be specific. Reference actual data points from the intelligence provided. No generic market commentary. If there are no high-conviction setups today, say so clearly — "no edge today" is a valid and valuable output.

Return ONLY valid JSON in this exact format:
{
  "market_tone": "bullish" | "bearish" | "mixed" | "neutral",
  "key_themes": ["theme1", "theme2"],
  "conviction_picks": [
    {
      "symbol": "NVDA",
      "direction": "long",
      "catalyst": "CEO bought $2M yesterday, gap above resistance",
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


def _load_context() -> str:
    """Assemble all available overnight intelligence into a context string."""
    parts = []

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
    try:
        from data_warehouse import load_macro_snapshot
        macro = load_macro_snapshot()
        vix = macro.get("vix", {})
        if vix:
            parts.append(f"\nVIX: {vix.get('price','?')} ({vix.get('chg_pct',0):+.1f}%)")
        oil = macro.get("oil", {})
        if oil:
            parts.append(f"Oil: {oil.get('price','?')} ({oil.get('chg_pct',0):+.1f}%)")
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

    # Earnings calendar (today + tomorrow)
    try:
        from data_warehouse import load_earnings_calendar
        ec = load_earnings_calendar()
        today_str = datetime.now().strftime("%Y-%m-%d")
        upcoming = [
            e for e in ec.get("calendar", [])
            if e.get("earnings_date", "") >= today_str
        ][:5]
        if upcoming:
            parts.append("\n=== EARNINGS (today + upcoming) ===")
            for e in upcoming:
                parts.append(f"  {e.get('symbol','?')}  reports: {e.get('earnings_date','?')}")
    except Exception:
        pass

    # Congressional + insider activity (last 48h)
    try:
        from insider_intelligence import fetch_congressional_trades, fetch_form4_insider_trades
        import watchlist_manager as wm
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

    return "\n".join(parts) if parts else "No overnight intelligence available."


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
            lines.append(f"       catalyst: {p.get('catalyst','?')}")
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

    _save_brief(brief)

    # SMS with top pick
    try:
        _send_sms_brief(brief)
    except Exception as exc:
        log.warning("[MORNING] SMS failed: %s", exc)

    picks = brief.get("conviction_picks", [])
    log.info("[MORNING] Brief generated — tone=%s  picks=%d",
             brief.get("market_tone"), len(picks))
    return brief


def _send_sms_brief(brief: dict) -> None:
    sid   = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    from_ = os.getenv("TWILIO_FROM_NUMBER")
    to    = os.getenv("TWILIO_TO_NUMBER")

    if not all([sid, token, from_, to]):
        return

    picks = brief.get("conviction_picks", [])
    tone  = brief.get("market_tone", "?").upper()
    if picks:
        top   = picks[0]
        msg   = (f"MORNING BRIEF [{tone}]: "
                 f"{top.get('symbol')} {top.get('direction','?').upper()} "
                 f"— {top.get('catalyst','?')[:80]}  "
                 f"stop={top.get('stop')} tgt={top.get('target')}")
    else:
        msg = f"MORNING BRIEF [{tone}]: No high-conviction setups today. {brief.get('brief_summary','')[:100]}"

    try:
        from twilio.rest import Client
        Client(sid, token).messages.create(body=msg[:160], from_=from_, to=to)
        log.info("[MORNING] SMS sent: %s", msg[:80])
    except Exception as exc:
        log.warning("[MORNING] SMS error: %s", exc)


if __name__ == "__main__":
    brief = generate_morning_brief()
    print(json.dumps(brief, indent=2))
