"""
options_data.py — IV history tracking, options chain fetching, and IV rank/percentile
computation for Account 2 options trading bot.

All data persisted to:
  data/options/iv_history/{symbol}_iv_history.json
  data/options/chains/{symbol}_chain.json
"""

import json
import logging
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent / "data" / "options"
_IV_DIR = _DATA_DIR / "iv_history"
_CHAIN_DIR = _DATA_DIR / "chains"
_IV_SNAP_DIR = _DATA_DIR / "iv_snapshots"
_IV_DIR.mkdir(parents=True, exist_ok=True)
_CHAIN_DIR.mkdir(parents=True, exist_ok=True)
_IV_SNAP_DIR.mkdir(parents=True, exist_ok=True)

# Minimum history entries before IV rank is considered valid
_MIN_IV_HISTORY = 20
# TTL for chain cache (seconds)
_CHAIN_CACHE_TTL = 900  # 15 minutes
# Maximum entries to keep in IV history (252 = 1 trading year)
_MAX_IV_HISTORY = 252


# ---------------------------------------------------------------------------
# Options chain fetch
# ---------------------------------------------------------------------------

def fetch_options_chain(symbol: str, force_refresh: bool = False) -> dict:
    """
    Fetch options chain for symbol. Uses yfinance. Returns dict with calls/puts
    for the nearest 4 expiration dates. Caches to disk for _CHAIN_CACHE_TTL seconds.

    Returns {} on failure (non-fatal).
    """
    cache_path = _CHAIN_DIR / f"{symbol}_chain.json"

    # Check cache freshness
    if not force_refresh and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            fetched_at = cached.get("fetched_at", 0)
            if time.time() - fetched_at < _CHAIN_CACHE_TTL:
                return cached
        except Exception:
            pass

    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        expirations = ticker.options
        if not expirations:
            log.debug("[OPTIONS_DATA] %s: no options expirations found", symbol)
            return {}

        # Take nearest 4 expirations
        target_exps = list(expirations[:4])
        chain_data = {
            "symbol": symbol,
            "fetched_at": time.time(),
            "expirations": {},
            "current_price": None,
        }

        # Get current price
        try:
            info = ticker.fast_info
            chain_data["current_price"] = float(info.last_price)
        except Exception:
            try:
                hist = ticker.history(period="1d")
                if not hist.empty:
                    chain_data["current_price"] = float(hist["Close"].iloc[-1])
            except Exception:
                pass

        for exp in target_exps:
            try:
                chain = ticker.option_chain(exp)
                calls = chain.calls[["strike", "lastPrice", "bid", "ask", "impliedVolatility",
                                      "volume", "openInterest", "delta", "theta", "gamma"]
                                     if all(c in chain.calls.columns
                                            for c in ["delta", "theta", "gamma"])
                                     else ["strike", "lastPrice", "bid", "ask",
                                           "impliedVolatility", "volume", "openInterest"]]
                puts = chain.puts[["strike", "lastPrice", "bid", "ask", "impliedVolatility",
                                    "volume", "openInterest", "delta", "theta", "gamma"]
                                   if all(c in chain.puts.columns
                                          for c in ["delta", "theta", "gamma"])
                                   else ["strike", "lastPrice", "bid", "ask",
                                         "impliedVolatility", "volume", "openInterest"]]
                chain_data["expirations"][exp] = {
                    "calls": calls.to_dict(orient="records"),
                    "puts": puts.to_dict(orient="records"),
                }
            except Exception as exc:
                log.debug("[OPTIONS_DATA] %s exp=%s chain fetch failed: %s", symbol, exp, exc)

        cache_path.write_text(json.dumps(chain_data, default=str))
        log.debug("[OPTIONS_DATA] %s: chain fetched, %d expirations", symbol, len(chain_data["expirations"]))
        return chain_data

    except Exception as exc:
        log.warning("[OPTIONS_DATA] %s: fetch_options_chain failed: %s", symbol, exc)
        return {}


# ---------------------------------------------------------------------------
# IV history management
# ---------------------------------------------------------------------------

def update_iv_history(symbol: str, iv_value: float) -> bool:
    """
    Append today's IV to the symbol's history file.
    Returns True if history was updated, False if today already recorded.

    iv_value should be a decimal (e.g. 0.35 for 35% IV).
    """
    if iv_value <= 0 or iv_value > 5.0:
        log.warning("[OPTIONS_DATA] %s: implausible IV value %.4f — skipping history update", symbol, iv_value)
        return False

    hist_path = _IV_DIR / f"{symbol}_iv_history.json"
    today_str = date.today().isoformat()

    history = []
    if hist_path.exists():
        try:
            history = json.loads(hist_path.read_text())
        except Exception:
            history = []

    # Dedup: don't add if today's date already present
    if history and history[-1].get("date") == today_str:
        history[-1]["iv"] = iv_value  # update with latest value
    else:
        history.append({"date": today_str, "iv": iv_value})

    # Trim to max history
    if len(history) > _MAX_IV_HISTORY:
        history = history[-_MAX_IV_HISTORY:]

    hist_path.write_text(json.dumps(history))
    return True


def _load_iv_history(symbol: str) -> list:
    """Load IV history list for symbol. Returns [] if not found."""
    hist_path = _IV_DIR / f"{symbol}_iv_history.json"
    if not hist_path.exists():
        return []
    try:
        return json.loads(hist_path.read_text())
    except Exception:
        return []


def compute_iv_rank(symbol: str) -> float | None:
    """
    IV rank = (current_iv - 52w_low) / (52w_high - 52w_low) * 100
    Returns None if insufficient history (<_MIN_IV_HISTORY entries).
    """
    history = _load_iv_history(symbol)
    if len(history) < _MIN_IV_HISTORY:
        log.debug("[OPTIONS_DATA] %s: insufficient IV history (%d/%d entries)",
                  symbol, len(history), _MIN_IV_HISTORY)
        return None

    ivs = [e["iv"] for e in history if e.get("iv", 0) > 0]
    if not ivs:
        return None

    current = ivs[-1]
    low = min(ivs)
    high = max(ivs)

    if high == low:
        return 50.0  # flat IV history — return neutral rank

    rank = (current - low) / (high - low) * 100
    return round(rank, 1)


def compute_iv_percentile(symbol: str) -> float | None:
    """
    IV percentile = percentage of days in history where IV was BELOW current IV.
    Distinct from rank — more robust to outliers.
    Returns None if insufficient history.
    """
    history = _load_iv_history(symbol)
    if len(history) < _MIN_IV_HISTORY:
        return None

    ivs = [e["iv"] for e in history if e.get("iv", 0) > 0]
    if not ivs:
        return None

    current = ivs[-1]
    pct = sum(1 for v in ivs[:-1] if v < current) / len(ivs[:-1]) * 100
    return round(pct, 1)


def _classify_iv_environment(rank: float | None) -> str:
    """
    Classify IV rank into actionable environment label.
    Returns: very_cheap | cheap | neutral | expensive | very_expensive | unknown
    """
    if rank is None:
        return "unknown"
    if rank < 15:
        return "very_cheap"
    if rank < 35:
        return "cheap"
    if rank < 65:
        return "neutral"
    if rank < 80:
        return "expensive"
    return "very_expensive"


def get_iv_summary(symbol: str, chain: dict | None = None) -> dict:
    """
    Returns full IV summary for a symbol:
      {
        "symbol": str,
        "current_iv": float | None,
        "iv_rank": float | None,
        "iv_percentile": float | None,
        "iv_environment": str,
        "history_days": int,
        "observation_mode": bool,   # True if < _MIN_IV_HISTORY days
        "current_price": float | None,
      }

    If chain is provided, extracts ATM IV from it and updates history.
    """
    result = {
        "symbol": symbol,
        "current_iv": None,
        "iv_rank": None,
        "iv_percentile": None,
        "iv_environment": "unknown",
        "history_days": len(_load_iv_history(symbol)),
        "observation_mode": False,
        "current_price": None,
    }

    # Extract ATM IV from chain if available
    if chain and chain.get("expirations") and chain.get("current_price"):
        spot = chain["current_price"]
        result["current_price"] = spot
        atm_iv = _extract_atm_iv(chain, spot)
        if atm_iv:
            result["current_iv"] = atm_iv
            update_iv_history(symbol, atm_iv)
            result["history_days"] = len(_load_iv_history(symbol))

    rank = compute_iv_rank(symbol)
    pct = compute_iv_percentile(symbol)
    result["iv_rank"] = rank
    result["iv_percentile"] = pct
    result["iv_environment"] = _classify_iv_environment(rank)
    result["observation_mode"] = result["history_days"] < _MIN_IV_HISTORY

    return result


def _extract_atm_iv(chain: dict, spot: float) -> float | None:
    """
    Extract approximate ATM IV from chain data.

    Skips same-day and next-day expirations (DTE 0-1) which have collapsed
    theta and return near-zero IV. Prefers the first expiration with DTE >= 2,
    targeting 7-14 DTE when available (BUG-005 fix).
    """
    try:
        expirations = chain.get("expirations", {})
        if not expirations:
            return None

        today = datetime.now(ZoneInfo("America/New_York")).date()

        # Select best expiration: skip DTE < 2, prefer 7-14 DTE range
        chosen_exp: str | None = None
        for exp_key in sorted(expirations.keys()):
            try:
                exp_date = datetime.strptime(exp_key, "%Y-%m-%d").date()
                dte = (exp_date - today).days
            except ValueError:
                continue
            if dte < 2:
                continue          # skip same-day / next-day (collapsed theta)
            chosen_exp = exp_key
            if dte >= 7:
                break             # first expiration with >= 7 DTE wins

        if chosen_exp is None:
            log.debug("[OPTIONS_DATA] _extract_atm_iv: no valid expiration (all DTE < 2)")
            return None

        exp_data = expirations[chosen_exp]
        calls = exp_data.get("calls", [])
        puts = exp_data.get("puts", [])

        if not calls and not puts:
            return None

        # Find closest strike to spot
        def _atm_iv_from_chain(contracts):
            if not contracts:
                return None
            closest = min(contracts, key=lambda c: abs(float(c.get("strike", 0)) - spot))
            iv = closest.get("impliedVolatility")
            if iv and float(iv) > 0.01:
                return float(iv)
            return None

        call_iv = _atm_iv_from_chain(calls)
        put_iv = _atm_iv_from_chain(puts)

        if call_iv and put_iv:
            return round((call_iv + put_iv) / 2, 4)
        return call_iv or put_iv

    except Exception as exc:
        log.debug("[OPTIONS_DATA] _extract_atm_iv failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Batch refresh
# ---------------------------------------------------------------------------

def refresh_all_iv_data(symbols: list[str]) -> dict:
    """
    Refresh IV data for all symbols in list.
    Fetches chains, extracts ATM IV, updates history.
    Returns summary dict: {symbol: iv_summary, ...}

    Designed to run in 4 AM maintenance block. Non-fatal per symbol.
    """
    results = {}
    for sym in symbols:
        try:
            chain = fetch_options_chain(sym, force_refresh=True)
            summary = get_iv_summary(sym, chain=chain)
            results[sym] = summary
            log.info("[OPTIONS_DATA] %s: IV=%.1f%% rank=%s env=%s history=%dd%s",
                     sym,
                     (summary["current_iv"] or 0) * 100,
                     f"{summary['iv_rank']:.0f}" if summary["iv_rank"] is not None else "N/A",
                     summary["iv_environment"],
                     summary["history_days"],
                     " [OBS]" if summary["observation_mode"] else "")
        except Exception as exc:
            log.warning("[OPTIONS_DATA] %s: refresh failed (non-fatal): %s", sym, exc)
            results[sym] = {"symbol": sym, "iv_environment": "unknown", "observation_mode": True}

    log.info("[OPTIONS_DATA] IV refresh complete: %d symbols processed", len(results))
    return results


# ---------------------------------------------------------------------------
# Options regime
# ---------------------------------------------------------------------------

def get_options_regime(vix: float) -> dict:
    """
    Translate VIX level into options-specific regime constraints.
    Returns dict used by options intelligence layer.
    """
    if vix < 15:
        return {
            "regime": "calm",
            "allowed_strategies": ["debit_spread", "single_leg", "straddle", "credit_spread"],
            "size_multiplier": 1.0,
            "max_contracts_multiplier": 1.0,
            "notes": "Low vol — favor debit spreads and defined-risk calls/puts",
        }
    elif vix < 22:
        return {
            "regime": "normal",
            "allowed_strategies": ["debit_spread", "single_leg", "straddle", "credit_spread"],
            "size_multiplier": 1.0,
            "max_contracts_multiplier": 1.0,
            "notes": "Normal vol — all strategies available",
        }
    elif vix < 30:
        return {
            "regime": "elevated",
            "allowed_strategies": ["debit_spread", "credit_spread"],
            "size_multiplier": 0.5,
            "max_contracts_multiplier": 0.5,
            "notes": "Elevated vol — spreads only, cut size 50%, no single-legs",
        }
    elif vix < 40:
        return {
            "regime": "high",
            "allowed_strategies": ["credit_spread"],
            "size_multiplier": 0.25,
            "max_contracts_multiplier": 0.25,
            "notes": "High vol — credit spreads only if conviction > 0.9, size 25%",
        }
    else:
        return {
            "regime": "crisis",
            "allowed_strategies": [],
            "size_multiplier": 0.0,
            "max_contracts_multiplier": 0.0,
            "notes": "Crisis vol — no new options positions, close existing if possible",
        }


# ---------------------------------------------------------------------------
# IV crush monitoring
# ---------------------------------------------------------------------------

def snapshot_pre_event_iv(
    symbol: str,
    event_date: str,
    event_type: str = "earnings",
) -> bool:
    """
    Record the current ATM IV as a pre-event baseline for symbol.

    Fetches a fresh options chain to get current ATM IV, then writes an
    entry to data/options/iv_snapshots/{symbol}_snapshots.json.

    Parameters
    ----------
    symbol     : Underlying symbol (e.g. "AAPL")
    event_date : ISO date of the upcoming event (e.g. "2026-04-25")
    event_type : "earnings" | "macro" | "fed" | other label

    Returns True if snapshot was recorded, False on any failure.
    """
    snap_path = _IV_SNAP_DIR / f"{symbol}_snapshots.json"
    today_str = date.today().isoformat()

    # Load existing snapshots
    snapshots: list[dict] = []
    if snap_path.exists():
        try:
            snapshots = json.loads(snap_path.read_text())
        except Exception:
            snapshots = []

    # Deduplicate: skip if we already have a snapshot for this event_date
    if any(s.get("event_date") == event_date for s in snapshots):
        log.debug(
            "[OPTIONS_DATA] %s: pre-event IV snapshot for %s already exists",
            symbol, event_date,
        )
        return False

    # Get current ATM IV from fresh chain
    try:
        chain = fetch_options_chain(symbol, force_refresh=True)
        if not chain or not chain.get("current_price"):
            log.debug("[OPTIONS_DATA] %s: snapshot_pre_event_iv — no chain data", symbol)
            return False
        atm_iv = _extract_atm_iv(chain, chain["current_price"])
        if not atm_iv:
            log.debug("[OPTIONS_DATA] %s: snapshot_pre_event_iv — could not extract ATM IV", symbol)
            return False
    except Exception as exc:
        log.warning("[OPTIONS_DATA] %s: snapshot_pre_event_iv failed: %s", symbol, exc)
        return False

    snapshots.append({
        "event_date":    event_date,
        "snapshot_date": today_str,
        "iv":            round(atm_iv, 4),
        "event_type":    event_type,
    })

    # Keep most recent 30 snapshots
    if len(snapshots) > 30:
        snapshots = snapshots[-30:]

    try:
        snap_path.write_text(json.dumps(snapshots))
    except Exception as exc:
        log.warning("[OPTIONS_DATA] %s: snapshot write failed: %s", symbol, exc)
        return False

    log.info(
        "[OPTIONS_DATA] %s: pre-event IV snapshot recorded iv=%.1f%% event=%s type=%s",
        symbol, atm_iv * 100, event_date, event_type,
    )
    return True


def detect_iv_crush(symbol: str, config: dict) -> tuple[bool, str]:
    """
    Detect whether IV has crushed post-event for symbol.

    Compares current ATM IV to the most recent pre-event snapshot. If
    current IV < snapshot_iv × (1 − crush_threshold), returns (True, reason).

    Only fires when config["account2"]["iv_monitoring"]["auto_close_on_crush"]
    is True (default False — observe before auto-closing).

    Parameters
    ----------
    symbol : Underlying symbol
    config : Full strategy_config dict (reads account2.iv_monitoring)

    Returns (crush_detected: bool, reason: str)
    """
    iv_mon = config.get("account2", {}).get("iv_monitoring", {})
    if not iv_mon.get("auto_close_on_crush", False):
        return False, ""

    crush_threshold = float(iv_mon.get("crush_threshold", 0.30))

    snap_path = _IV_SNAP_DIR / f"{symbol}_snapshots.json"
    if not snap_path.exists():
        return False, ""

    try:
        snapshots = json.loads(snap_path.read_text())
    except Exception:
        return False, ""

    if not snapshots:
        return False, ""

    # Use the most recent snapshot
    snap = snapshots[-1]
    pre_iv = snap.get("iv")
    if not pre_iv or pre_iv <= 0:
        return False, ""

    # Get current ATM IV from history (avoid fetching live chain during close-check)
    history = _load_iv_history(symbol)
    if not history:
        return False, ""
    current_iv = history[-1].get("iv")
    if not current_iv or current_iv <= 0:
        return False, ""

    crush_level = pre_iv * (1.0 - crush_threshold)
    if current_iv < crush_level:
        reason = (
            f"iv_crush: current={current_iv:.3f} "
            f"pre_event={pre_iv:.3f} "
            f"threshold={crush_threshold:.0%} "
            f"event={snap.get('event_date','?')}"
        )
        log.info("[OPTIONS_DATA] %s: IV crush detected — %s", symbol, reason)
        return True, reason

    return False, ""


def check_iv_history_ready(symbols: list[str]) -> dict:
    """
    Check which symbols have sufficient IV history to compute a valid IV rank.

    Returns a dict with:
      "symbol_ready"  : {symbol: bool}  — True if symbol has >= _MIN_IV_HISTORY entries
      "all_ready"     : bool            — True if every symbol is ready
      "ready_count"   : int
      "total_count"   : int

    Non-fatal: returns all-False dict on any exception.
    Never raises.
    """
    try:
        symbol_ready: dict[str, bool] = {}
        for sym in symbols:
            history = _load_iv_history(sym)
            symbol_ready[sym] = len(history) >= _MIN_IV_HISTORY
        ready_count = sum(1 for v in symbol_ready.values() if v)
        return {
            "symbol_ready": symbol_ready,
            "all_ready":    ready_count == len(symbols) if symbols else True,
            "ready_count":  ready_count,
            "total_count":  len(symbols),
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("[OPTIONS_DATA] check_iv_history_ready failed: %s", exc)
        return {
            "symbol_ready": {s: False for s in symbols},
            "all_ready":    False,
            "ready_count":  0,
            "total_count":  len(symbols),
        }
