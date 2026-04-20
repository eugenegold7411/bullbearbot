"""
bot_stage2_signal.py — Stage 2: Haiku signal scorer.

Public API:
  score_signals(watchlist_symbols, regime, md, positions) -> dict
  format_signal_scores(scores)                            -> str
"""

import json
from itertools import islice
from pathlib import Path

from bot_clients import _get_claude, MODEL_FAST
from log_setup import get_logger, log_trade

log = get_logger(__name__)

_BATCH_SIZE = 15

_SIGNAL_SYS = (
    "You are a signal scorer for a trading bot. Score symbols based on available signals. "
    "JSON only, no markdown.\n"
    "Output: "
    '{"scored_symbols":{"SYMBOL":{"score":<0-100>,"signals":[<strings>],'
    '"conflicts":[<strings>],"conviction":"high"|"medium"|"low"|"avoid",'
    '"primary_catalyst":"<one sentence>","orb_candidate":true|false,'
    '"pattern_watchlist":false|"<caution note>",'
    '"direction":"bullish"|"bearish"|"neutral",'
    '"tier":"core"|"dynamic"}},'
    '"top_3":["SYM1","SYM2","SYM3"],"elevated_caution":["SYM4"],'
    '"reasoning":"<2 sentences>"}'
)


def _call_single_batch(user_content: str) -> dict:
    """Make one scored-symbols API call with repair+retry. Returns parsed result dict."""
    resp = _get_claude().messages.create(
        model=MODEL_FAST, max_tokens=4000,
        system=[{"type": "text", "text": _SIGNAL_SYS}],
        messages=[{"role": "user", "content": user_content}],
    )
    try:
        from cost_tracker import get_tracker
        get_tracker().record_api_call(MODEL_FAST, resp.usage, caller="signal_scorer")
    except Exception as _ct_exc:
        log.warning("Cost tracker failed: %s", _ct_exc)
    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        last_brace = raw.rfind("}")
        if last_brace >= 0:
            try:
                result = json.loads(raw[:last_brace + 1])
                log.debug("[SIGNALS] JSON repaired by truncation (last_brace=%d)", last_brace)
                return result
            except json.JSONDecodeError:
                pass
        log.debug("[SIGNALS] JSON truncated, retrying API call with completeness hint")
        _retry_sys = _SIGNAL_SYS + "\nReturn ONLY valid complete JSON. If you cannot fit all symbols, return fewer rather than truncating."
        _retry_resp = _get_claude().messages.create(
            model=MODEL_FAST, max_tokens=4000,
            system=[{"type": "text", "text": _retry_sys}],
            messages=[{"role": "user", "content": user_content}],
        )
        _retry_raw = _retry_resp.content[0].text.strip()
        if _retry_raw.startswith("```"):
            _retry_raw = _retry_raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return json.loads(_retry_raw)  # raises on failure — caller handles


def score_signals(
    watchlist_symbols: list,
    regime: dict,
    md: dict,
    positions: list = None,
) -> dict:
    """
    Stage 2: Haiku call scoring watchlist symbols against all signals.
    Only runs during market session. Fails to empty dict on error.
    System prompt cached.

    Symbol list is capped at _MAX_SCORED, prioritised:
      1. Currently held positions (always included)
      2. Morning brief conviction picks
      3. Breaking-news mentions this cycle
      4. Remaining watchlist symbols
    """
    if not watchlist_symbols:
        return {}
    try:
        _MAX_SCORED = 35
        scored: list[str] = []
        seen: set[str] = set()

        def _add(sym: str) -> None:
            if sym in seen or sym not in watchlist_symbols:
                return
            scored.append(sym)
            seen.add(sym)

        for p in (positions or []):
            if float(getattr(p, "qty", 0)) > 0:
                _add(p.symbol)

        try:
            _brief_path = Path(__file__).parent / "data" / "market" / "morning_brief.json"
            if _brief_path.exists():
                _brief = json.loads(_brief_path.read_text())
                for pick in _brief.get("conviction_picks", []):
                    _add(str(pick.get("symbol", "")))
        except Exception:
            pass

        _news = md.get("breaking_news", "") or ""
        for sym in watchlist_symbols:
            if sym in _news:
                _add(sym)

        for sym in watchlist_symbols:
            if len(scored) >= _MAX_SCORED:
                break
            _add(sym)

        log.debug("[SIGNALS] scoring %d/%d symbols: %s",
                  len(scored), len(watchlist_symbols), scored)

        insider_lines = [l.strip() for l in (md.get("insider_section", "") or "").splitlines()
                         if any(s in l for s in scored)][:10]
        orb_str = "(none)"
        try:
            orb_path = Path(__file__).parent / "data" / "scanner" / "orb_candidates.json"
            if orb_path.exists():
                orb_cands = json.loads(orb_path.read_text()).get("candidates", [])
                orb_str = "\n".join(
                    f"{c['symbol']}: gap {c['gap_pct']:+.1f}% score={c['orb_score']:.2f} {c['conviction']}"
                    for c in orb_cands[:8]
                ) or "(none)"
        except Exception:
            pass
        reddit_lines = [l.strip() for l in (md.get("reddit_section", "") or "").splitlines()
                        if any(s in l for s in scored)][:6]
        morning_lines = (md.get("morning_brief_section", "") or "").splitlines()[:5]
        from memory import _load_pattern_watchlist  # noqa: PLC0415
        pwl       = _load_pattern_watchlist()
        pwl_lines = [f"{s}: min {d.get('minimum_signals_required', 2)} signals required"
                     for s, d in pwl.items() if not d.get("graduated") and s in scored]

        regime_line = (
            f"REGIME: score={regime.get('regime_score', 50)} bias={regime.get('bias', 'neutral')} "
            f"theme={regime.get('session_theme', '?')}\n"
            f"  constraints: {regime.get('constraints', [])}"
        )

        merged_symbols: dict = {}
        all_caution: list = []
        all_reasoning: list = []

        _it = iter(scored)
        while True:
            batch = list(islice(_it, _BATCH_SIZE))
            if not batch:
                break
            b_insider = [l for l in insider_lines if any(s in l for s in batch)]
            b_reddit  = [l for l in reddit_lines  if any(s in l for s in batch)]
            b_pwl     = [l for l in pwl_lines     if any(l.startswith(s) for s in batch)]
            batch_content = (
                f"Symbols to score: {', '.join(batch)}\n\n"
                f"{regime_line}\n\n"
                f"INSIDER/CONGRESSIONAL:\n{chr(10).join(b_insider) or '(none)'}\n\n"
                f"ORB CANDIDATES:\n{orb_str}\n\n"
                f"REDDIT:\n{chr(10).join(b_reddit) or '(none)'}\n\n"
                f"MORNING BRIEF picks:\n{chr(10).join(morning_lines) or '(none)'}\n\n"
                f"PATTERN WATCHLIST (elevated conviction required):\n{chr(10).join(b_pwl) or '(none)'}"
            )
            try:
                batch_result = _call_single_batch(batch_content)
            except Exception as _be:
                log.warning("[SIGNALS] Batch %s failed: %s", batch, _be)
                continue
            merged_symbols.update(batch_result.get("scored_symbols", {}))
            all_caution.extend(batch_result.get("elevated_caution", []))
            if batch_result.get("reasoning"):
                all_reasoning.append(batch_result["reasoning"])
            log.debug("[SIGNALS] batch scored %d symbols", len(batch_result.get("scored_symbols", {})))

        if not merged_symbols:
            log.warning("[SIGNALS] All batches failed — returning empty")
            return {}

        sorted_syms = sorted(merged_symbols.items(), key=lambda kv: kv[1].get("score", 0), reverse=True)
        seen_c: set = set()
        result = {
            "scored_symbols": merged_symbols,
            "top_3": [s for s, _ in sorted_syms[:3]],
            "elevated_caution": [s for s in all_caution if not (s in seen_c or seen_c.add(s))],
            "reasoning": " | ".join(all_reasoning),
        }
        log.info("[SIGNALS] top_3=%s  caution=%s", result.get("top_3", []), result.get("elevated_caution", []))
        log_trade({"event": "signal_scoring", "top_3": result.get("top_3", []),
                   "elevated_caution": result.get("elevated_caution", []),
                   "scored_count": len(result.get("scored_symbols", {}))})
        try:
            from datetime import datetime as _dt
            conv_path = Path(__file__).parent / "data" / "market" / "daily_conviction.json"
            existing: list = []
            if conv_path.exists():
                try:
                    existing = json.loads(conv_path.read_text())
                    if not isinstance(existing, list):
                        existing = []
                except Exception:
                    existing = []
            existing.append({"ts": _dt.now().isoformat(), "top_3": result.get("top_3", [])})
            conv_path.write_text(json.dumps(existing[-50:], indent=2))
        except Exception:
            pass
        return result
    except Exception as exc:
        log.warning("[SIGNALS] Scorer failed (non-fatal): %s", exc)
        return {}


def format_signal_scores(scores: dict) -> str:
    if not scores:
        return "  (signal scoring unavailable this cycle)"
    lines = []
    if scores.get("top_3"):
        lines.append(f"  Top conviction today: {', '.join(scores['top_3'])}")
    if scores.get("elevated_caution"):
        lines.append(f"  Elevated caution: {', '.join(scores['elevated_caution'])}")
    if scores.get("reasoning"):
        lines.append(f"  Signal environment: {scores['reasoning']}")
    for sym, d in list(scores.get("scored_symbols", {}).items())[:10]:
        conv    = d.get("conviction", "?")
        cat     = (d.get("primary_catalyst", "") or "")[:60]
        sigs    = ", ".join((d.get("signals", []) or [])[:4])
        orb_tag = " ORB" if d.get("orb_candidate") else ""
        pwl_tag = f"  ⚠{d['pattern_watchlist']}" if d.get("pattern_watchlist") else ""
        lines.append(f"  {sym}: score={d.get('score', 0)} [{conv}]{orb_tag}  {cat}{pwl_tag}")
        if sigs:
            lines.append(f"    signals: {sigs}")
    return "\n".join(lines) if lines else "  (no signals scored)"
