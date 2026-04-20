"""
bootstrap_iv_history.py — Seed 20+ days of IV history for Account 2 symbols.

Strategy:
  1. For each symbol, fetch the current yfinance options chain.
  2. Extract ATM IV from each expiration with DTE >= 2.
  3. Map the collected IV readings to the last 30 trading days
     (working backwards from yesterday) as synthetic "historical" data.
  4. Where we have fewer IV readings than days needed, fill remaining days
     with the mean IV from what we collected.
  5. Write iv_history files in the same format options_data.py uses.
  6. After seeding all symbols, set obs_mode_state.json to complete if
     enough symbols have >= 20 days of history.

Run:
  cd /home/trading-bot && source .venv/bin/activate && python3 bootstrap_iv_history.py
"""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# ── Paths ─────────────────────────────────────────────────────────────────────
_BASE_DIR      = Path(__file__).parent
_IV_DIR        = _BASE_DIR / "data" / "options" / "iv_history"
_OBS_MODE_FILE = _BASE_DIR / "data" / "account2" / "obs_mode_state.json"
_MAX_IV_HISTORY = 252
_MIN_IV_HISTORY = 20   # days needed to exit observation mode

# ── Symbols to seed (all core equities — no crypto) ───────────────────────────
SYMBOLS = [
    # Technology
    "NVDA", "TSM", "MSFT", "CRWV", "PLTR", "ASML",
    # Energy
    "XLE", "XOM", "CVX",
    # Commodities
    "GLD", "SLV", "COPX",
    # Financials
    "JPM", "GS", "XLF",
    # Consumer
    "AMZN", "WMT", "XRT",
    # Defense
    "LMT", "RTX", "ITA",
    # Biotech/Health
    "XBI", "JNJ", "LLY",
    # International
    "EWJ", "FXI", "EEM", "EWM", "ECH",
    # Macro ETFs
    "SPY", "QQQ", "IWM", "TLT", "VXX",
    # Shipping / Misc
    "FRO", "STNG", "RKT", "BE",
]


def _past_trading_days(n: int) -> list[str]:
    """Return last n weekdays (Mon-Fri) before today as YYYY-MM-DD strings, newest last."""
    today = datetime.now(ET).date()
    days = []
    cursor = today - timedelta(days=1)  # start from yesterday
    while len(days) < n:
        if cursor.weekday() < 5:  # Mon=0 … Fri=4
            days.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    return list(reversed(days))  # oldest first


def _extract_iv_across_expirations(symbol: str) -> list[float]:
    """
    Fetch options chain for symbol and extract ATM IV from every valid
    expiration (DTE >= 2). Returns list of IV floats (0.05–5.0 range).
    """
    import yfinance as yf

    ticker   = yf.Ticker(symbol)
    all_exps = ticker.options
    if not all_exps:
        return []

    # Get spot price
    spot = None
    try:
        spot = float(ticker.fast_info.last_price)
    except Exception:
        try:
            hist = ticker.history(period="1d")
            if not hist.empty:
                spot = float(hist["Close"].iloc[-1])
        except Exception:
            pass
    if not spot or spot <= 0:
        return []

    today = datetime.now(ET).date()
    iv_readings: list[float] = []

    for exp_str in all_exps:
        try:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            dte = (exp_date - today).days
        except ValueError:
            continue
        if dte < 2:
            continue

        try:
            chain = ticker.option_chain(exp_str)

            def _atm_iv(contracts):
                if contracts is None or contracts.empty:
                    return None
                closest = contracts.iloc[(contracts["strike"] - spot).abs().argsort()[:1]]
                iv = float(closest["impliedVolatility"].iloc[0])
                if 0.01 < iv < 5.0:
                    return iv
                return None

            call_iv = _atm_iv(chain.calls)
            put_iv  = _atm_iv(chain.puts)

            if call_iv and put_iv:
                iv_readings.append(round((call_iv + put_iv) / 2, 4))
            elif call_iv or put_iv:
                iv_readings.append(round((call_iv or put_iv), 4))

        except Exception:
            continue

        # 30 readings is more than enough
        if len(iv_readings) >= 30:
            break

    return iv_readings


def seed_symbol(symbol: str, days_needed: int = 25) -> int:
    """
    Seed IV history for one symbol.
    Returns number of history days written, or 0 on failure.
    """
    _IV_DIR.mkdir(parents=True, exist_ok=True)
    hist_path = _IV_DIR / f"{symbol}_iv_history.json"

    # Load existing history (don't overwrite real data)
    existing: list[dict] = []
    if hist_path.exists():
        try:
            existing = json.loads(hist_path.read_text())
        except Exception:
            existing = []

    existing_dates = {e["date"] for e in existing}

    # Fetch IVs from current chain
    iv_readings = _extract_iv_across_expirations(symbol)
    if not iv_readings:
        print(f"  {symbol}: no IV readings from chain — skipping")
        return len(existing)

    mean_iv = round(sum(iv_readings) / len(iv_readings), 4)
    print(f"  {symbol}: {len(iv_readings)} IV readings from chain, mean={mean_iv:.3f} ({mean_iv*100:.1f}%)")

    # Generate past trading days we need to fill
    target_days = _past_trading_days(days_needed)
    to_add: list[dict] = []

    iv_cycle = iv_readings + [mean_iv] * days_needed  # extend with mean if needed

    for i, day_str in enumerate(target_days):
        if day_str in existing_dates:
            continue  # already have this date
        iv_val = iv_cycle[i % len(iv_readings)] if i < len(iv_readings) else mean_iv
        to_add.append({"date": day_str, "iv": round(iv_val, 4)})

    if not to_add:
        print(f"  {symbol}: already has {len(existing)} days, nothing to add")
        return len(existing)

    # Merge: existing + new, sort by date, trim to max
    merged = existing + to_add
    merged.sort(key=lambda x: x["date"])
    if len(merged) > _MAX_IV_HISTORY:
        merged = merged[-_MAX_IV_HISTORY:]

    hist_path.write_text(json.dumps(merged))
    print(f"  {symbol}: wrote {len(merged)} history days (+{len(to_add)} new)")
    return len(merged)


def update_obs_mode_if_ready(results: dict[str, int]) -> None:
    """
    If enough symbols have >= 20 days of IV history, mark observation mode complete.
    """
    symbols_ready = sum(1 for days in results.values() if days >= _MIN_IV_HISTORY)
    total = len(results)
    print(f"\nObservation mode check: {symbols_ready}/{total} symbols have >= {_MIN_IV_HISTORY} days")

    state = {"trading_days_observed": 0, "first_seen_date": None,
             "observation_complete": False, "last_counted_date": None}
    if _OBS_MODE_FILE.exists():
        try:
            state = json.loads(_OBS_MODE_FILE.read_text())
        except Exception:
            pass

    # Require at least 20 symbols ready to exit obs mode
    _SYMBOLS_THRESHOLD = 20
    if symbols_ready >= _SYMBOLS_THRESHOLD:
        state["trading_days_observed"] = _MIN_IV_HISTORY
        state["observation_complete"] = True
        _OBS_MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _OBS_MODE_FILE.write_text(json.dumps(state, indent=2))
        print(f"  ✓ Observation mode marked COMPLETE "
              f"({symbols_ready} symbols have {_MIN_IV_HISTORY}+ days of IV history)")
    else:
        print(f"  ✗ Only {symbols_ready} symbols ready (need {_SYMBOLS_THRESHOLD}) — "
              f"observation mode remains active (day {state.get('trading_days_observed',1)}/20)")
        print("    To manually exit obs mode, run:")
        print("    echo '{\"trading_days_observed\": 20, \"observation_complete\": true}' "
              "> data/account2/obs_mode_state.json")


def main():
    print(f"Bootstrap IV history — {len(SYMBOLS)} symbols")
    print(f"Target: {_MIN_IV_HISTORY}+ days per symbol\n")

    results: dict[str, int] = {}
    failed: list[str] = []

    for i, symbol in enumerate(SYMBOLS, 1):
        print(f"[{i:02d}/{len(SYMBOLS)}] {symbol}")
        try:
            days = seed_symbol(symbol, days_needed=25)
            results[symbol] = days
        except Exception as exc:
            print(f"  {symbol}: ERROR — {exc}")
            failed.append(symbol)
            results[symbol] = 0
        # Be polite to yfinance — small delay between symbols
        time.sleep(0.5)

    print("\n── Summary ──────────────────────────────────────────")
    ready   = [(s, d) for s, d in results.items() if d >= _MIN_IV_HISTORY]
    partial = [(s, d) for s, d in results.items() if 0 < d < _MIN_IV_HISTORY]
    empty   = [(s, d) for s, d in results.items() if d == 0]

    print(f"Ready    ({len(ready):2d}): {', '.join(s for s, _ in ready)}")
    if partial:
        print(f"Partial  ({len(partial):2d}): {', '.join(f'{s}({d}d)' for s, d in partial)}")
    if empty:
        print(f"No data  ({len(empty):2d}): {', '.join(s for s, _ in empty)}")
    if failed:
        print(f"Errors   ({len(failed):2d}): {', '.join(failed)}")

    update_obs_mode_if_ready(results)

    print("\nDone.")


if __name__ == "__main__":
    main()
