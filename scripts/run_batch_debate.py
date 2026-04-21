#!/usr/bin/env python3
"""
scripts/run_batch_debate.py — A2 Full Universe Batch Debate Report

Runs every symbol in the A2 universe through the complete pipeline:
  IV summary → feature pack → router → veto → debate

Usage:
  python3 scripts/run_batch_debate.py            # full run with debate
  python3 scripts/run_batch_debate.py --dry-run  # router/veto only, no API calls
  python3 scripts/run_batch_debate.py --no-confirm  # skip cost confirmation
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except ImportError:
    pass

ET = ZoneInfo("America/New_York")
TODAY = date.today().isoformat()
REPORT_PATH = _ROOT / "data" / "reports" / f"batch_debate_{TODAY}.json"

# Cost estimate constants (Sonnet 4.6)
_INPUT_TOKENS_PER_CALL  = 1_800   # system + candidate prompt
_OUTPUT_TOKENS_PER_CALL = 800
_COST_PER_CALL = (
    _INPUT_TOKENS_PER_CALL  / 1_000_000 * 3.00  # $3/M input
    + _OUTPUT_TOKENS_PER_CALL / 1_000_000 * 15.00  # $15/M output
)
_MAX_DEBATE_CALLS = 10


# ── Universe / config helpers ─────────────────────────────────────────────────

def _get_universe() -> list[str]:
    try:
        import watchlist_manager as wm  # noqa: PLC0415
        wl = wm.get_active_watchlist()
        return wl.get("stocks", []) + wl.get("etfs", [])
    except Exception:
        return [
            "NVDA", "TSM", "MSFT", "CRWV", "PLTR", "ASML",
            "XLE", "XOM", "CVX", "USO",
            "GLD", "SLV", "COPX",
            "JPM", "GS", "XLF",
            "AMZN", "WMT", "XRT",
            "LMT", "RTX", "ITA",
            "XBI", "JNJ", "LLY",
            "EWJ", "FXI", "EEM", "EWM", "ECH",
            "SPY", "QQQ", "IWM", "TLT", "VXX",
            "FRO", "STNG", "RKT", "BE",
            "AAPL", "META", "GOOGL", "AMD",
        ]


def _load_config() -> dict:
    try:
        return json.loads((_ROOT / "strategy_config.json").read_text())
    except Exception:
        return {}


def _load_vix() -> float:
    try:
        vc = _ROOT / "data" / "market" / "vix_cache.json"
        if vc.exists() and (time.time() - vc.stat().st_mtime) < 600:
            return float(json.loads(vc.read_text()).get("vix", 20.0))
    except Exception:
        pass
    return 20.0


def _load_equity() -> tuple[float, bool]:
    """Returns (equity, fetched_live)."""
    try:
        from alpaca.trading.client import TradingClient  # noqa: PLC0415
        api_key = os.getenv("ALPACA_API_KEY_OPTIONS")
        secret  = os.getenv("ALPACA_SECRET_KEY_OPTIONS")
        if api_key and secret:
            tc   = TradingClient(api_key=api_key, secret_key=secret, paper=True)
            acct = tc.get_account()
            return float(acct.equity), True
    except Exception:
        pass
    return 100_000.0, False


def _load_signal_scores() -> tuple[dict, float | None]:
    """Returns (scores_dict, age_seconds). age=None when file absent."""
    path = _ROOT / "data" / "market" / "signal_scores.json"
    if not path.exists():
        return {}, None
    age = time.time() - path.stat().st_mtime
    try:
        data = json.loads(path.read_text())
        scores = data.get("scored_symbols", data) if isinstance(data, dict) else {}
        return scores, age
    except Exception:
        return {}, age


# ── Per-symbol pipeline ────────────────────────────────────────────────────────

def _run_symbol(
    symbol: str,
    signal_scores: dict,
    iv_summaries: dict,
    chains: dict,
    equity: float,
    vix: float,
    config: dict,
) -> dict:
    """Full pipeline for one symbol. Non-fatal — exceptions are captured."""
    from bot_options_stage1_candidates import _build_a2_feature_pack  # noqa: PLC0415
    from bot_options_stage2_structures import (  # noqa: PLC0415
        _infer_router_rule_fired,
        _route_strategy,
        build_candidate_structures,
    )

    r: dict = {
        "symbol": symbol,
        "iv_environment": "unknown",
        "iv_rank": None,
        "observation_mode": False,
        "current_price": None,
        "a1_direction": "neutral",
        "liquidity_score": None,
        "router_rule": "—",
        "n_generated": 0,
        "n_surviving": 0,
        "veto_reasons": [],
        "surviving_candidates": [],
        "debate_result": None,
        "debate_input": None,
        "debate_output_raw": None,
        "error": None,
    }

    try:
        iv = iv_summaries.get(symbol, {})
        r["iv_environment"]  = iv.get("iv_environment", "unknown")
        r["iv_rank"]         = iv.get("iv_rank")
        r["observation_mode"] = iv.get("observation_mode", False)
        r["current_price"]   = iv.get("current_price")

        if r["iv_environment"] == "unknown":
            r["router_rule"] = "NO_IV"
            return r

        chain = chains.get(symbol) or {}

        pack = _build_a2_feature_pack(
            symbol=symbol,
            signal_scores=signal_scores,
            iv_summaries=iv_summaries,
            equity=equity,
            vix=vix,
            chain=chain or None,
        )
        if pack is None:
            r["router_rule"] = "NO_PACK"
            return r

        r["a1_direction"]   = pack.a1_direction
        r["liquidity_score"] = pack.liquidity_score

        allowed = _route_strategy(pack, config=config)
        r["router_rule"] = _infer_router_rule_fired(pack, allowed, config=config)

        if not allowed:
            return r

        generated, vetoed, surviving = build_candidate_structures(
            pack=pack,
            equity=equity,
            chain=chain,
            allowed_structures=allowed,
            config=config,
        )
        r["n_generated"]          = len(generated)
        r["n_surviving"]          = len(surviving)
        r["veto_reasons"]         = [v["reason"] for v in vetoed]
        r["surviving_candidates"] = surviving

    except Exception as exc:
        r["error"] = str(exc)

    return r


# ── Debate runner ──────────────────────────────────────────────────────────────

def _debate(
    r: dict,
    iv_summaries: dict,
    equity: float,
    vix: float,
    regime: str,
    account1_summary: str,
) -> None:
    """Run bounded debate for symbol result dict. Mutates in-place."""
    from bot_options_stage3_debate import run_options_debate  # noqa: PLC0415
    try:
        debate_result, prompt_used, raw = run_options_debate(
            candidates=[],
            iv_summaries=iv_summaries,
            vix=vix,
            regime=regime,
            account1_summary=account1_summary,
            obs_mode=False,
            equity=equity,
            candidate_structures=r["surviving_candidates"],
        )
        r["debate_result"]      = debate_result
        r["debate_input"]       = prompt_used
        r["debate_output_raw"]  = raw
    except Exception as exc:
        r["debate_result"] = {"error": str(exc), "reject": True, "confidence": 0.0}
        r["error"] = f"debate: {exc}"


# ── Formatting helpers ─────────────────────────────────────────────────────────

_IV_SHORT = {
    "very_cheap":    "v.cheap",
    "cheap":         "cheap",
    "neutral":       "neutral",
    "expensive":     "exp",
    "very_expensive": "v.exp",
    "unknown":       "unknown",
}


def _fmt_iv(r: dict) -> str:
    short = _IV_SHORT.get(r.get("iv_environment", "unknown"), r.get("iv_environment", "?"))
    rank  = r.get("iv_rank")
    return f"{short}({rank:.0f})" if rank is not None else short


def _fmt_debate(r: dict) -> str:
    dr = r.get("debate_result")
    if dr is None:
        return "—"
    if "error" in dr:
        return f"ERROR: {str(dr['error'])[:30]}"
    conf = float(dr.get("confidence", 0))
    if dr.get("reject") or not dr.get("selected_candidate_id"):
        return f"REJECT conf={conf:.2f}"
    cid = dr.get("selected_candidate_id", "?")
    return f"APPROVE {cid} conf={conf:.2f}"


def _print_report(results: list[dict], dry_run: bool, vix: float, equity: float) -> None:
    W = 74
    print()
    print("╔" + "═" * W + "╗")
    title = f"  A2 FULL UNIVERSE BATCH DEBATE — {TODAY}"
    if dry_run:
        title += "  [DRY RUN]"
    print(f"║{title:<{W}}║")
    print("╚" + "═" * W + "╝")
    print()
    print(f" VIX={vix:.1f}  equity=${equity:,.0f}  symbols={len(results)}")
    print()
    print(f" {'Symbol':<7} {'IV Env':<15} {'Dir':<8} {'Router':<8} {'Gen':>4} {'Pass':>5}  Debate")
    print(f" {'──────':<7} {'───────────────':<15} {'───────':<8} {'──────':<8} {'───':>4} {'────':>5}  ──────")

    for r in results:
        sym      = r["symbol"]
        iv_str   = _fmt_iv(r)
        direction = r.get("a1_direction", "—")
        router   = r.get("router_rule", "—")
        n_gen    = r.get("n_generated", 0)
        n_pass   = r.get("n_surviving", 0)
        debate   = _fmt_debate(r)
        obs_tag  = " [OBS]" if r.get("observation_mode") else ""
        err_tag  = f" ← {r['error'][:35]}" if r.get("error") and r.get("iv_environment") == "unknown" else ""
        gen_s    = str(n_gen) if n_gen else "—"
        pass_s   = str(n_pass) if n_pass else "—"
        print(f" {sym:<7} {iv_str:<15} {direction:<8} {router:<8} {gen_s:>4} {pass_s:>5}  {debate}{obs_tag}{err_tag}")

    # Summary counts
    n_no_iv     = sum(1 for r in results if r.get("router_rule") in ("NO_IV", "NO_PACK"))
    n_blocked   = sum(1 for r in results if r.get("n_generated") == 0
                      and r.get("router_rule") not in ("NO_IV", "NO_PACK", "—"))
    n_gen_syms  = sum(1 for r in results if r.get("n_generated", 0) > 0)
    n_surv_syms = sum(1 for r in results if r.get("n_surviving", 0) > 0)
    n_debated   = sum(1 for r in results if r.get("debate_result") is not None)
    n_approved  = sum(1 for r in results
                      if r.get("debate_result") and not r["debate_result"].get("reject")
                      and r["debate_result"].get("selected_candidate_id"))
    n_rejected  = sum(1 for r in results
                      if r.get("debate_result")
                      and (r["debate_result"].get("reject") or
                           not r["debate_result"].get("selected_candidate_id")))
    est_cost    = _COST_PER_CALL * n_debated

    debated_sorted = sorted(
        [r for r in results if r.get("debate_result") and "error" not in r["debate_result"]],
        key=lambda r: float(r["debate_result"].get("confidence", 0) or 0),
        reverse=True,
    )

    print()
    print("═" * (W + 2))
    print("SUMMARY")
    print(f"  Symbols evaluated:      {len(results)}")
    print(f"  No IV / no pack:        {n_no_iv}")
    print(f"  Router blocked:         {n_blocked}")
    print(f"  Candidates generated:   {n_gen_syms} symbols")
    print(f"  Survived veto:          {n_surv_syms} symbols")
    print(f"  Debate ran:             {n_debated}")
    print(f"  Debate approved:        {n_approved}")
    print(f"  Debate rejected:        {n_rejected}")
    print(f"  Estimated cost:         ${est_cost:.4f}")
    if dry_run:
        print(f"  [DRY RUN — no debate calls made]")

    if debated_sorted:
        print()
        print("TOP CANDIDATES (by confidence):")
        for i, r in enumerate(debated_sorted[:5], 1):
            sym   = r["symbol"]
            conf  = float(r["debate_result"].get("confidence", 0) or 0)
            risks = r["debate_result"].get("key_risks", [])
            note  = risks[0][:55] if risks else r["debate_result"].get("reasons", "")[:55]
            flag  = "APPROVED" if not r["debate_result"].get("reject") else "rejected"
            print(f"  {i}. {sym:<6} conf={conf:.2f}  {flag}  {note}")

    print("═" * (W + 2))
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="A2 Full Universe Batch Debate")
    parser.add_argument("--dry-run",    action="store_true",
                        help="Skip all debate API calls; show cost estimate only")
    parser.add_argument("--no-confirm", action="store_true",
                        help="Skip cost confirmation prompt (auto-proceeds)")
    args = parser.parse_args()

    # Non-interactive detection — auto-proceed when stdin is not a TTY
    interactive = sys.stdin.isatty() and not args.no_confirm

    # ── Setup ──────────────────────────────────────────────────────────────────
    print(f"\nA2 Batch Debate — {TODAY}")
    print(f"Loading universe and config...", end="", flush=True)

    universe = _get_universe()
    config   = _load_config()
    vix      = _load_vix()

    equity, live_equity = _load_equity()
    eq_src = "live Alpaca" if live_equity else "default"
    print(f" {len(universe)} symbols  VIX={vix:.1f}  equity=${equity:,.0f} ({eq_src})")

    signal_scores, sig_age = _load_signal_scores()
    if sig_age is None:
        print("  [WARN] signal_scores.json not found — all symbols use neutral direction")
    elif sig_age > 600:
        print(f"  [WARN] signal_scores.json is {sig_age/60:.0f}m old — A1 scores may be stale")
    else:
        print(f"  signal_scores: {len(signal_scores)} symbols, age={sig_age:.0f}s")

    # ── IV summaries ───────────────────────────────────────────────────────────
    print(f"\nFetching IV summaries for {len(universe)} symbols (uses 15-min chain cache)...")
    import options_data  # noqa: PLC0415

    iv_summaries: dict = {}
    chains: dict       = {}

    for i, sym in enumerate(universe):
        print(f"  [{i+1:2d}/{len(universe)}] {sym:<7}", end="\r", flush=True)
        try:
            chain = options_data.fetch_options_chain(sym) or {}
            chains[sym] = chain
            iv_summaries[sym] = options_data.get_iv_summary(sym, chain=chain)
        except Exception as exc:
            chains[sym] = {}
            iv_summaries[sym] = {
                "symbol": sym, "iv_environment": "unknown",
                "observation_mode": True, "history_days": 0,
            }

    n_obs = sum(1 for iv in iv_summaries.values() if iv.get("observation_mode"))
    print(f"  IV summaries done: {len(iv_summaries)} symbols  ({n_obs} observation-mode)     ")

    # ── Per-symbol pipeline ────────────────────────────────────────────────────
    print(f"\nRunning pipeline for {len(universe)} symbols...")
    results: list[dict] = []
    for sym in universe:
        try:
            r = _run_symbol(
                symbol=sym,
                signal_scores=signal_scores,
                iv_summaries=iv_summaries,
                chains=chains,
                equity=equity,
                vix=vix,
                config=config,
            )
        except Exception as exc:
            r = {
                "symbol": sym, "iv_environment": "unknown", "iv_rank": None,
                "observation_mode": False, "current_price": None,
                "a1_direction": "neutral", "liquidity_score": None,
                "router_rule": "ERROR", "n_generated": 0, "n_surviving": 0,
                "veto_reasons": [], "surviving_candidates": [],
                "debate_result": None, "debate_input": None,
                "debate_output_raw": None, "error": str(exc),
            }
        results.append(r)

    # ── Debate ─────────────────────────────────────────────────────────────────
    candidates_for_debate = [r for r in results if r.get("n_surviving", 0) > 0]
    regime          = "normal"
    account1_summary = "  Account 1: not loaded."

    if not args.dry_run and candidates_for_debate:
        # Sort by A1 signal score descending; cap at _MAX_DEBATE_CALLS
        def _score_key(r: dict) -> float:
            sig = signal_scores.get(r["symbol"])
            if isinstance(sig, dict):
                return float(sig.get("score", 0))
            return 0.0

        candidates_for_debate.sort(key=_score_key, reverse=True)
        candidates_for_debate = candidates_for_debate[:_MAX_DEBATE_CALLS]
        est_cost = _COST_PER_CALL * len(candidates_for_debate)

        print(f"\n{len(candidates_for_debate)} symbols have surviving candidates "
              f"(capped at {_MAX_DEBATE_CALLS}).")
        print(f"  Estimated debate cost: ${est_cost:.4f}  "
              f"({len(candidates_for_debate)} calls × ~${_COST_PER_CALL:.4f}/call)")

        proceed = True
        if interactive:
            try:
                ans = input("  Proceed with debate calls? [y/N]: ").strip().lower()
                proceed = (ans == "y")
            except (EOFError, KeyboardInterrupt):
                proceed = False

        if not proceed:
            print("  Skipping debate calls.")
            candidates_for_debate = []
        else:
            # Load A1 context for debate prompt
            try:
                from bot_options_stage1_candidates import (  # noqa: PLC0415
                    _load_account1_last_decision,
                    _summarize_account1_for_prompt,
                )
                a1_dec = _load_account1_last_decision()
                regime = a1_dec.get("regime", "normal") if isinstance(a1_dec, dict) else "normal"
                account1_summary = _summarize_account1_for_prompt(a1_dec)
            except Exception:
                pass

            for i, r in enumerate(candidates_for_debate):
                sym = r["symbol"]
                print(f"  [{i+1}/{len(candidates_for_debate)}] Debating {sym}...",
                      end="", flush=True)
                t0 = time.monotonic()
                _debate(r, iv_summaries, equity, vix, regime, account1_summary)
                elapsed = time.monotonic() - t0
                verdict = _fmt_debate(r)
                print(f" {verdict}  ({elapsed:.1f}s)")

    elif args.dry_run and candidates_for_debate:
        n_would = min(len(candidates_for_debate), _MAX_DEBATE_CALLS)
        est = _COST_PER_CALL * n_would
        print(f"\n[DRY RUN] {len(candidates_for_debate)} symbols have surviving candidates.")
        print(f"  Would run {n_would} debate call(s) — estimated cost ${est:.4f}")

    # ── Report ─────────────────────────────────────────────────────────────────
    is_dry = args.dry_run or (not candidates_for_debate and
                               not any(r.get("debate_result") for r in results))
    _print_report(results, dry_run=is_dry, vix=vix, equity=equity)

    # ── Save artifact ──────────────────────────────────────────────────────────
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)

    n_debated  = sum(1 for r in results if r.get("debate_result") is not None)
    n_approved = sum(1 for r in results
                     if r.get("debate_result") and not r["debate_result"].get("reject")
                     and r["debate_result"].get("selected_candidate_id"))

    artifact = {
        "date":          TODAY,
        "generated_at":  datetime.now(ET).isoformat(),
        "vix":           vix,
        "equity":        equity,
        "regime":        regime,
        "dry_run":       args.dry_run,
        "n_symbols":     len(universe),
        "summary": {
            "n_no_iv":              sum(1 for r in results if r.get("router_rule") in ("NO_IV", "NO_PACK")),
            "n_router_blocked":     sum(1 for r in results if r.get("n_generated") == 0
                                        and r.get("router_rule") not in ("NO_IV", "NO_PACK", "—")),
            "n_candidates_syms":    sum(1 for r in results if r.get("n_generated", 0) > 0),
            "n_survived_veto_syms": sum(1 for r in results if r.get("n_surviving", 0) > 0),
            "n_debated":            n_debated,
            "n_approved":           n_approved,
            "estimated_cost_usd":   round(_COST_PER_CALL * n_debated, 4),
        },
        "results": results,
    }
    REPORT_PATH.write_text(json.dumps(artifact, indent=2, default=str))
    print(f"Artifact saved → {REPORT_PATH.name}")


if __name__ == "__main__":
    main()
