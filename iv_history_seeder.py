"""
iv_history_seeder.py — One-shot IV history bootstrap seeder.

Seeds synthetic historical IV entries for symbols that lack IV history,
enabling Account 2 to have valid IV rank calculations from day one.

Usage:
    python3 iv_history_seeder.py              # Phase 1 only (live)
    python3 iv_history_seeder.py --phase2     # Phase 1 + Phase 2
    python3 iv_history_seeder.py --dry-run    # compute and validate only, no writes

NEVER overwrites existing live IV entries where iv >= MIN_VALID_IV (0.05).
Exception: entries with iv < MIN_VALID_IV are treated as bad entries (e.g.
BUG-005 collapsed-theta same-day expiry artifacts) and are replaced.

Entry format matches options_data.py exactly:
  Live entries:   {"date": "YYYY-MM-DD", "iv": float}
  Seeded entries: {"date": "YYYY-MM-DD", "iv": float,
                   "source": "yfinance_seed", "seed_date": "YYYY-MM-DD",
                   "confidence": "high"|"medium"|"low",
                   "quality_flags": list[str]}

compute_iv_rank() reads only e["iv"] — extra fields are ignored.
"""

import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

# ── Constants ─────────────────────────────────────────────────────────────────

PHASE1_SYMBOLS = [
    "SPY", "QQQ", "NVDA", "AAPL", "MSFT", "AMZN",
    "META", "GOOGL", "TSM", "AMD", "XLE", "GLD",
    "TLT", "IWM", "XLF", "XBI",
]

PHASE2_SYMBOLS = [
    "TSLA", "PLTR", "ASML", "CRWV", "XOM", "CVX",
    "USO", "COPX", "JPM", "GS", "WMT", "XRT",
    "LMT", "RTX", "ITA", "JNJ", "LLY", "EWJ",
    "FXI", "EEM", "EWM", "ECH", "VXX", "FRO",
    "STNG", "RKT", "BE",
]

# IV quality bounds (mirrors options_data.py thresholds)
MIN_VALID_IV      = 0.05   # 5%  — reject below this; replace bad entries below this
MAX_VALID_IV      = 5.00   # 500% — reject above this
MIN_OPEN_INTEREST = 50     # skip illiquid strikes entirely
TARGET_DTE_MIN    = 7
TARGET_DTE_MAX    = 45     # prefer 7-45 DTE window for ATM IV measurement
MIN_DTE           = 2      # BUG-005 pattern: skip same-day and next-day expirations

IV_HISTORY_DIR = Path("data/options/iv_history")


# ── Business day helpers ──────────────────────────────────────────────────────

def _past_business_days(n: int) -> list[str]:
    """
    Return n past weekdays (Mon–Fri) in chronological order (oldest first).
    Starts from yesterday. Holiday filtering not applied — weekday check is
    sufficient for seed purposes; a holiday entry has negligible impact on
    IV rank percentile calculations.
    """
    days: list[str] = []
    d = date.today() - timedelta(days=1)
    while len(days) < n:
        if d.weekday() < 5:  # 0=Mon … 4=Fri
            days.append(d.isoformat())
        d -= timedelta(days=1)
    days.reverse()  # oldest first
    return days


# ── IV fetch ──────────────────────────────────────────────────────────────────

def _fetch_atm_iv_yfinance(
    symbol: str,
    target_dte_min: int = TARGET_DTE_MIN,
    target_dte_max: int = TARGET_DTE_MAX,
) -> tuple[Optional[float], str, dict]:
    """
    Fetch current ATM IV for symbol via yfinance.

    Selection logic:
    1. Get available expirations from ticker.options.
    2. Filter: skip DTE < MIN_DTE (BUG-005 pattern).
    3. Prefer expirations inside [target_dte_min, target_dte_max]; select
       the one closest to the range midpoint.
    4. Fallback: if none inside the range, use the first DTE >= MIN_DTE.
    5. Find ATM strike (closest to spot) in calls and puts.
    6. Average call IV + put IV (or use whichever leg is available).
    7. Validate: MIN_VALID_IV <= iv <= MAX_VALID_IV.
    8. Validate: open_interest >= MIN_OPEN_INTEREST (skip entirely if fails).

    Quality flags appended to metadata["quality_flags"]:
      "low_oi"     — max ATM OI < 200 (passes but flagged)
      "single_leg" — only call or only put IV was available

    Returns (iv, expiry_used, metadata).
    Returns (None, "", {"error": ...}) on any failure.
    """
    metadata: dict = {"quality_flags": []}
    try:
        import yfinance as yf  # lazy import — not in requirements for tests

        ticker = yf.Ticker(symbol)
        expirations = ticker.options
        if not expirations:
            return None, "", {"error": "no_expirations"}

        # Spot price
        try:
            spot = float(ticker.fast_info.last_price)
        except Exception:
            try:
                hist = ticker.history(period="1d")
                spot = float(hist["Close"].iloc[-1]) if not hist.empty else 0.0
            except Exception:
                spot = 0.0
        if spot <= 0:
            return None, "", {"error": "no_spot_price"}

        # Select expiry closest to midpoint of [target_dte_min, target_dte_max]
        today = date.today()
        midpoint = (target_dte_min + target_dte_max) / 2.0
        best_exp: Optional[str] = None
        best_dist = float("inf")

        for exp_str in expirations:
            try:
                dte = (date.fromisoformat(exp_str) - today).days
            except ValueError:
                continue
            if dte < MIN_DTE:
                continue
            if target_dte_min <= dte <= target_dte_max:
                dist = abs(dte - midpoint)
                if dist < best_dist:
                    best_dist = dist
                    best_exp = exp_str

        # Fallback: first DTE >= MIN_DTE regardless of range
        if best_exp is None:
            for exp_str in expirations:
                try:
                    if (date.fromisoformat(exp_str) - today).days >= MIN_DTE:
                        best_exp = exp_str
                        break
                except ValueError:
                    continue

        if best_exp is None:
            return None, "", {"error": "no_valid_expiry"}

        # Fetch chain
        try:
            chain = ticker.option_chain(best_exp)
            calls = chain.calls.to_dict(orient="records") if not chain.calls.empty else []
            puts  = chain.puts.to_dict(orient="records")  if not chain.puts.empty  else []
        except Exception as exc:
            return None, best_exp, {"error": f"chain_fetch_failed: {exc}"}

        if not calls and not puts:
            return None, best_exp, {"error": "empty_chain"}

        dte_used = (date.fromisoformat(best_exp) - today).days

        # ATM IV extraction
        def _atm_iv(contracts: list) -> tuple[Optional[float], int]:
            if not contracts:
                return None, 0
            atm = min(contracts, key=lambda c: abs(float(c.get("strike", 0)) - spot))
            oi  = int(atm.get("openInterest", 0) or 0)
            raw_iv = atm.get("impliedVolatility")
            if raw_iv and float(raw_iv) > MIN_VALID_IV:
                return round(float(raw_iv), 4), oi
            return None, oi

        call_iv, call_oi = _atm_iv(calls)
        put_iv,  put_oi  = _atm_iv(puts)

        # Combine legs
        if call_iv and put_iv:
            iv_val = round((call_iv + put_iv) / 2, 4)
            metadata["legs_used"] = "call+put"
        elif call_iv:
            iv_val = call_iv
            metadata["quality_flags"].append("single_leg")
            metadata["legs_used"] = "call_only"
        elif put_iv:
            iv_val = put_iv
            metadata["quality_flags"].append("single_leg")
            metadata["legs_used"] = "put_only"
        else:
            return None, best_exp, {
                "error": "iv_below_min_valid",
                "quality_flags": metadata["quality_flags"],
            }

        # OI gate
        max_oi = max(call_oi, put_oi)
        if max_oi < MIN_OPEN_INTEREST:
            return None, best_exp, {
                "error": f"oi_too_low: {max_oi} < {MIN_OPEN_INTEREST}",
                "quality_flags": metadata["quality_flags"],
            }
        if max_oi < 200:
            metadata["quality_flags"].append("low_oi")

        # Final bounds check
        if not (MIN_VALID_IV <= iv_val <= MAX_VALID_IV):
            return None, best_exp, {"error": f"iv_out_of_bounds: {iv_val}"}

        metadata.update({
            "spot":    spot,
            "expiry":  best_exp,
            "dte":     dte_used,
            "call_iv": call_iv,
            "put_iv":  put_iv,
            "max_oi":  max_oi,
        })

        return iv_val, best_exp, metadata

    except Exception as exc:
        return None, "", {"error": str(exc)}


# ── Seed entry generation ─────────────────────────────────────────────────────

def _generate_seed_entries(
    symbol: str,
    current_iv: float,
    expiry_used: str,
    metadata: dict,
    target_days: int = 25,
) -> list[dict]:
    """
    Generate target_days synthetic historical IV entries.

    Uses a mean-reverting random walk anchored to current_iv. Reproducible:
    hash(symbol) is used as the RNG seed — same symbol always produces the
    same synthetic history.

    Walk parameters:
    - start_iv:   current_iv * (1 + Gauss(0, 0.20)) — ±20% initial perturbation
    - daily_vol:  0.03 * current_iv  (3% of current IV per day)
    - mean_rev:   0.10               (10% pull toward current_iv each step)
    - clamp:      [MIN_VALID_IV, MAX_VALID_IV]

    Entry format matches live entries plus provenance fields:
      {"date": "YYYY-MM-DD", "iv": float,
       "source": "yfinance_seed", "seed_date": "YYYY-MM-DD",
       "confidence": "high"|"medium"|"low",
       "quality_flags": list[str]}
    """
    today_str      = date.today().isoformat()
    quality_flags  = list(metadata.get("quality_flags", []))
    confidence = (
        "high"   if not quality_flags                  else
        "medium" if "low_oi" in quality_flags          else
        "low"
    )

    rng   = random.Random(abs(hash(symbol)) % (2 ** 32))
    dates = _past_business_days(target_days)

    daily_vol = 0.03 * current_iv
    mean_rev  = 0.10

    # Starting point: perturb current_iv ±20%
    iv = current_iv * (1.0 + rng.gauss(0, 0.20))
    iv = max(MIN_VALID_IV, min(MAX_VALID_IV, iv))

    entries: list[dict] = []
    for date_str in dates:
        noise = rng.gauss(0, daily_vol)
        rev   = mean_rev * (current_iv - iv)
        iv    = iv + rev + noise
        iv    = max(MIN_VALID_IV, min(MAX_VALID_IV, iv))
        entries.append({
            "date":          date_str,
            "iv":            round(iv, 4),
            "source":        "yfinance_seed",
            "seed_date":     today_str,
            "confidence":    confidence,
            "quality_flags": quality_flags,
        })

    return entries


# ── Merge ─────────────────────────────────────────────────────────────────────

def _merge_with_existing(
    symbol: str,
    new_entries: list[dict],
    iv_history_dir: Path = IV_HISTORY_DIR,
) -> tuple[list[dict], int]:
    """
    Merge new seeded entries with existing IV history.

    Rules (in priority order):
    1. Existing entry has iv >= MIN_VALID_IV → keep it untouched (live data).
    2. Existing entry has iv < MIN_VALID_IV  → replace with seeded value.
       (BUG-005 exception: bad entries from collapsed-theta same-day expiries.)
    3. Date not yet present → add seeded entry.

    Returns (merged_list_sorted_by_date, n_entries_added_or_replaced).
    Does NOT write to disk — caller decides whether to save.
    """
    hist_path = iv_history_dir / f"{symbol}_iv_history.json"
    existing: list[dict] = []
    if hist_path.exists():
        try:
            existing = json.loads(hist_path.read_text())
        except Exception:
            existing = []

    # Build date → entry index
    by_date: dict[str, dict] = {e["date"]: e for e in existing}

    n_added = 0
    for entry in new_entries:
        d = entry["date"]
        if d in by_date:
            existing_iv = by_date[d].get("iv", 0)
            if existing_iv < MIN_VALID_IV:
                # Bad entry — overwrite (SPY BUG-005 fix)
                by_date[d] = entry
                n_added += 1
            # else: good live entry — leave it alone
        else:
            by_date[d] = entry
            n_added += 1

    merged = sorted(by_date.values(), key=lambda e: e["date"])
    return merged, n_added


# ── Quality validation ────────────────────────────────────────────────────────

def validate_seed_quality(
    symbol: str,
    iv_history_dir: Path = IV_HISTORY_DIR,
) -> dict:
    """
    Validate IV history quality for a symbol.

    Grades:
    A: >= 20 entries with valid IV, has_variance=True (range > 0.001)
    B: >= 15 entries, has_variance=True
    C: >= 10 entries (any variance)
    F: < 10 valid entries OR no variance (flat line)

    Returns:
    {
        "symbol":          str,
        "total_entries":   int,   # all entries including bad ones
        "seed_entries":    int,   # entries with source=="yfinance_seed"
        "live_entries":    int,   # entries without source tag
        "iv_range":        [min, max],
        "iv_mean":         float,
        "has_variance":    bool,  # iv_range > 0.001
        "ready_for_iv_rank": bool,  # valid_entries >= 20
        "quality_grade":   "A"|"B"|"C"|"F",
    }
    """
    _empty = {
        "symbol": symbol, "total_entries": 0, "seed_entries": 0,
        "live_entries": 0, "iv_range": [0.0, 0.0], "iv_mean": 0.0,
        "has_variance": False, "ready_for_iv_rank": False, "quality_grade": "F",
    }

    hist_path = iv_history_dir / f"{symbol}_iv_history.json"
    if not hist_path.exists():
        return _empty

    try:
        history = json.loads(hist_path.read_text())
    except Exception:
        return _empty

    total  = len(history)
    seeds  = sum(1 for e in history if e.get("source") == "yfinance_seed")
    live   = total - seeds
    ivs    = [e["iv"] for e in history if e.get("iv", 0) >= MIN_VALID_IV]

    if not ivs:
        return {**_empty, "total_entries": total, "seed_entries": seeds, "live_entries": live}

    iv_min  = min(ivs)
    iv_max  = max(ivs)
    iv_mean = round(sum(ivs) / len(ivs), 4)
    iv_rng  = round(iv_max - iv_min, 4)
    has_var = iv_rng > 0.001
    ready   = len(ivs) >= 20

    if   len(ivs) >= 20 and has_var:
        grade = "A"
    elif len(ivs) >= 15 and has_var:
        grade = "B"
    elif len(ivs) >= 10:
        grade = "C"
    else:
        grade = "F"

    return {
        "symbol":           symbol,
        "total_entries":    total,
        "seed_entries":     seeds,
        "live_entries":     live,
        "iv_range":         [round(iv_min, 4), round(iv_max, 4)],
        "iv_mean":          iv_mean,
        "has_variance":     has_var,
        "ready_for_iv_rank": ready,
        "quality_grade":    grade,
    }


# ── Core seeder ───────────────────────────────────────────────────────────────

def seed_iv_history(
    symbols: list[str],
    target_days: int = 25,
    dry_run: bool = False,
) -> dict:
    """
    Seed IV history for given symbols using yfinance.

    For each symbol:
    1. Fetch current ATM IV from yfinance (with DTE filtering).
    2. Validate IV quality (bounds, OI).
    3. Generate synthetic historical entries via mean-reverting random walk.
    4. Merge with existing history (never overwrite good live entries;
       replace bad entries with iv < MIN_VALID_IV).
    5. Save atomically (unless dry_run=True).

    dry_run=True: compute and validate but do not write any files.

    Returns:
    {
        "seeded":          [symbol, ...],
        "skipped":         [(symbol, reason), ...],
        "failed":          [(symbol, error), ...],
        "entries_added":   int,
        "quality_summary": {symbol: quality_grade, ...},
    }
    """
    IV_HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    seeded:      list[str]   = []
    skipped:     list[tuple] = []
    failed:      list[tuple] = []
    total_added: int         = 0

    for sym in symbols:
        print(f"  [{sym:6s}] fetching IV ...", end="", flush=True)
        try:
            iv, expiry, meta = _fetch_atm_iv_yfinance(sym)
            if iv is None:
                reason = meta.get("error", "unknown")
                print(f" SKIP ({reason})")
                skipped.append((sym, reason))
                continue

            flags_str = f"  flags={meta['quality_flags']}" if meta.get("quality_flags") else ""
            print(
                f" IV={iv:.1%}  exp={expiry}  dte={meta.get('dte', '?')}{flags_str} ...",
                end="", flush=True,
            )

            new_entries = _generate_seed_entries(sym, iv, expiry, meta, target_days)
            merged, n_added = _merge_with_existing(sym, new_entries)

            if n_added == 0:
                print(f" no-op ({len(merged)} existing entries)")
                seeded.append(sym)
                continue

            if not dry_run:
                hist_path = IV_HISTORY_DIR / f"{sym}_iv_history.json"
                hist_path.write_text(json.dumps(merged))

            total_added += n_added
            mode_tag = " [DRY RUN]" if dry_run else ""
            print(f" +{n_added} entries (total={len(merged)}){mode_tag}")
            seeded.append(sym)

        except Exception as exc:
            print(f" ERROR: {exc}")
            failed.append((sym, str(exc)))

    quality: dict = {}
    if not dry_run:
        for sym in seeded:
            q = validate_seed_quality(sym)
            quality[sym] = q.get("quality_grade", "?")

    return {
        "seeded":          seeded,
        "skipped":         skipped,
        "failed":          failed,
        "entries_added":   total_added,
        "quality_summary": quality,
    }


# ── Phase runners ─────────────────────────────────────────────────────────────

def run_phase1_seed(dry_run: bool = False) -> dict:
    """Run Phase 1 seeding for all PHASE1_SYMBOLS."""
    print(f"\n--- Phase 1 Seeding ({len(PHASE1_SYMBOLS)} symbols) ---")
    return seed_iv_history(PHASE1_SYMBOLS, target_days=25, dry_run=dry_run)


def run_phase2_seed(dry_run: bool = False) -> dict:
    """Run Phase 2 seeding for all PHASE2_SYMBOLS."""
    print(f"\n--- Phase 2 Seeding ({len(PHASE2_SYMBOLS)} symbols) ---")
    return seed_iv_history(PHASE2_SYMBOLS, target_days=25, dry_run=dry_run)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    phase2  = "--phase2"  in sys.argv

    print("=== IV History Seeder ===")
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")

    result = run_phase1_seed(dry_run=dry_run)
    print(f"\nSeeded:        {result['seeded']}")
    print(f"Skipped:       {result['skipped']}")
    print(f"Failed:        {result['failed']}")
    print(f"Entries added: {result['entries_added']}")

    if not dry_run:
        print("\n--- Phase 1 Quality Validation ---")
        for sym in PHASE1_SYMBOLS:
            q = validate_seed_quality(sym)
            grade   = q.get("quality_grade", "?")
            entries = q.get("total_entries", 0)
            ready   = q.get("ready_for_iv_rank", False)
            print(f"  {sym:6s}: {grade}  ({entries} entries, ready={ready})")

    if phase2:
        result2 = run_phase2_seed(dry_run=dry_run)
        print(f"\nSeeded:        {result2['seeded']}")
        print(f"Skipped:       {result2['skipped']}")
        print(f"Failed:        {result2['failed']}")
        print(f"Entries added: {result2['entries_added']}")

        if not dry_run:
            print("\n--- Phase 2 Quality Validation ---")
            for sym in PHASE2_SYMBOLS:
                q = validate_seed_quality(sym)
                grade   = q.get("quality_grade", "?")
                entries = q.get("total_entries", 0)
                print(f"  {sym:6s}: {grade}  ({entries} entries)")

    print("\nDone.")
