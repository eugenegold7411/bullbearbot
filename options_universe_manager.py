"""
options_universe_manager.py — A2 tradeable universe manager with IV bootstrap queue.

Manages which symbols have sufficient IV history to trade options, and
provides automatic IV bootstrapping for newly-encountered symbols.

Importable with no env vars — reads/writes only local data files.

Storage:
  data/options/universe.json             — canonical tradeable universe
  data/options/iv_pending_bootstrap.json — bootstrap queue

Public API:
  is_tradeable(symbol)                              -> bool
  queue_for_bootstrap(symbol, source)               -> None
  run_bootstrap_queue()                             -> dict
  get_universe()                                    -> dict
  initialize_universe_from_existing_iv_history()    -> None
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_DATA_DIR        = Path(__file__).parent / "data" / "options"
_UNIVERSE_FILE   = _DATA_DIR / "universe.json"
_BOOTSTRAP_QUEUE = _DATA_DIR / "iv_pending_bootstrap.json"
_IV_DIR          = _DATA_DIR / "iv_history"

_MIN_IV_HISTORY       = 20   # mirrors options_data._MIN_IV_HISTORY
_MAX_DAILY_BOOTSTRAPS = 5


# ── Internal helpers ──────────────────────────────────────────────────────────

def _has_sufficient_iv_history(symbol: str) -> bool:
    """True if symbol's IV history file has >= _MIN_IV_HISTORY valid entries."""
    hist_path = _IV_DIR / f"{symbol}_iv_history.json"
    if not hist_path.exists():
        return False
    try:
        history = json.loads(hist_path.read_text())
        valid = [e for e in history if isinstance(e, dict) and e.get("iv", 0) >= 0.05]
        return len(valid) >= _MIN_IV_HISTORY
    except Exception:
        return False


def _save_universe(universe: dict) -> None:
    """Atomically write universe to disk. Non-fatal."""
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        universe["updated_at"] = datetime.now(timezone.utc).isoformat()
        _UNIVERSE_FILE.write_text(json.dumps(universe, indent=2))
    except Exception as exc:
        log.warning("[UNIVERSE] _save_universe failed (non-fatal): %s", exc)


# ── Public API ────────────────────────────────────────────────────────────────

def get_universe() -> dict:
    """Load and return universe.json. Creates it if absent."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    if _UNIVERSE_FILE.exists():
        try:
            return json.loads(_UNIVERSE_FILE.read_text())
        except Exception as exc:
            log.warning("[UNIVERSE] get_universe: load failed (non-fatal): %s", exc)
    universe: dict = {
        "symbols":    {},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_universe(universe)
    return universe


def is_tradeable(symbol: str) -> bool:
    """
    Check if symbol has sufficient IV history.
    If not, queue it for bootstrap and return False.
    Non-fatal — returns False on any error.
    """
    try:
        universe = get_universe()
        entry    = universe.get("symbols", {}).get(symbol)

        if entry and entry.get("bootstrap_complete"):
            if _has_sufficient_iv_history(symbol):
                return True
            # IV history lost since bootstrap — re-queue
            log.warning("[UNIVERSE] %s: in universe but IV history lost — re-queuing", symbol)
            queue_for_bootstrap(symbol, "auto_recheck")
            return False

        # Not in universe yet — check raw IV history
        if _has_sufficient_iv_history(symbol):
            # Promote automatically so future calls skip the file scan
            universe.setdefault("symbols", {})[symbol] = {
                "bootstrap_complete": True,
                "added_at":           datetime.now(timezone.utc).isoformat(),
                "source":             "auto_promote",
            }
            _save_universe(universe)
            return True

        # Not tradeable — queue for next 4 AM bootstrap
        queue_for_bootstrap(symbol, "a1_signal")
        return False

    except Exception as exc:
        log.warning("[UNIVERSE] is_tradeable(%s) failed (non-fatal): %s", symbol, exc)
        return False


def queue_for_bootstrap(symbol: str, source: str) -> None:
    """
    Add symbol to iv_pending_bootstrap.json if not already present.
    source: "a1_signal" | "uw_flow" | "manual" | "auto_recheck"
    Non-fatal.
    """
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        queue: dict = {}
        if _BOOTSTRAP_QUEUE.exists():
            try:
                queue = json.loads(_BOOTSTRAP_QUEUE.read_text())
            except Exception:
                queue = {}

        pending = queue.setdefault("pending", {})
        if symbol not in pending:
            pending[symbol] = {
                "source":    source,
                "queued_at": datetime.now(timezone.utc).isoformat(),
            }
            _BOOTSTRAP_QUEUE.write_text(json.dumps(queue, indent=2))
            log.info("[UNIVERSE] %s: queued for bootstrap (source=%s)", symbol, source)
    except Exception as exc:
        log.debug("[UNIVERSE] queue_for_bootstrap(%s) failed (non-fatal): %s", symbol, exc)


def run_bootstrap_queue() -> dict:
    """
    Called during 4 AM data warehouse refresh.
    Runs iv_history_seeder on up to _MAX_DAILY_BOOTSTRAPS pending symbols.
    Returns {"bootstrapped": [...], "failed": [...], "remaining": [...]}
    """
    result: dict = {"bootstrapped": [], "failed": [], "remaining": []}

    try:
        if not _BOOTSTRAP_QUEUE.exists():
            log.debug("[UNIVERSE] bootstrap queue: no queue file — nothing to do")
            return result

        try:
            queue = json.loads(_BOOTSTRAP_QUEUE.read_text())
        except Exception as exc:
            log.warning("[UNIVERSE] bootstrap queue: failed to load: %s", exc)
            return result

        pending: dict = queue.get("pending", {})
        if not pending:
            log.debug("[UNIVERSE] bootstrap queue: empty — nothing to do")
            return result

        batch = list(pending.keys())[:_MAX_DAILY_BOOTSTRAPS]
        log.info("[UNIVERSE] bootstrap queue: processing %d/%d symbols",
                 len(batch), len(pending))

        try:
            from iv_history_seeder import seed_iv_history  # noqa: PLC0415
        except ImportError as exc:
            log.warning("[UNIVERSE] bootstrap queue: iv_history_seeder not importable: %s", exc)
            result["remaining"] = list(pending.keys())
            return result

        seed_result = seed_iv_history(batch, target_days=25)

        universe    = get_universe()
        syms_dict   = universe.setdefault("symbols", {})
        now_ts      = datetime.now(timezone.utc).isoformat()

        for sym in seed_result.get("seeded", []):
            if _has_sufficient_iv_history(sym):
                syms_dict[sym] = {
                    "bootstrap_complete": True,
                    "added_at":           now_ts,
                    "source":             "bootstrap_queue",
                }
                result["bootstrapped"].append(sym)
                pending.pop(sym, None)
            else:
                result["failed"].append(sym)

        for item in seed_result.get("skipped", []) + seed_result.get("failed", []):
            sym = item[0] if isinstance(item, (list, tuple)) else item
            if sym not in result["bootstrapped"]:
                result["failed"].append(sym)

        _save_universe(universe)

        queue["pending"] = pending
        try:
            _BOOTSTRAP_QUEUE.write_text(json.dumps(queue, indent=2))
        except Exception as exc:
            log.debug("[UNIVERSE] bootstrap queue: failed to update queue file: %s", exc)

        result["remaining"] = list(pending.keys())
        log.info(
            "[UNIVERSE] bootstrap done: bootstrapped=%s failed=%s remaining=%d",
            result["bootstrapped"], result["failed"], len(result["remaining"]),
        )

    except Exception as exc:
        log.warning("[UNIVERSE] run_bootstrap_queue failed (non-fatal): %s", exc)

    return result


def initialize_universe_from_existing_iv_history() -> None:
    """
    One-time setup: scan data/options/iv_history/ for existing _iv_history.json
    files, add all found symbols to universe.json as grandfathered
    (bootstrap_complete=True). Safe to call multiple times (idempotent).
    """
    _DATA_DIR.mkdir(parents=True, exist_ok=True)

    universe   = get_universe()
    syms_dict  = universe.setdefault("symbols", {})
    now_ts     = datetime.now(timezone.utc).isoformat()
    added      = 0
    skipped    = 0

    if not _IV_DIR.exists():
        log.info("[UNIVERSE] initialize: iv_history dir absent — nothing to scan")
        return

    for hist_file in sorted(_IV_DIR.glob("*_iv_history.json")):
        sym = hist_file.name[: -len("_iv_history.json")]
        if syms_dict.get(sym, {}).get("bootstrap_complete"):
            skipped += 1
            continue
        if _has_sufficient_iv_history(sym):
            syms_dict[sym] = {
                "bootstrap_complete": True,
                "added_at":           now_ts,
                "source":             "grandfathered",
            }
            added += 1

    _save_universe(universe)
    log.info(
        "[UNIVERSE] initialize: added=%d skipped=%d total=%d",
        added, skipped, len(syms_dict),
    )
