"""Cross-symbol halt tracking for crypto mean-reversion signals."""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_STATE_PATH = Path(__file__).parent / "data" / "runtime" / "crypto_reversion_state.json"
_HALT_THRESHOLD = 3  # combined losses before cross-symbol halt


class CryptoReversionState:
    """Tracks consecutive losses per symbol and a combined cross-symbol halt.

    Persists to data/runtime/crypto_reversion_state.json.
    Loads existing state on init; defaults to clean if file is missing or corrupt.
    """

    def __init__(self) -> None:
        self._state = self._load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> dict:
        try:
            if _STATE_PATH.exists():
                raw = json.loads(_STATE_PATH.read_text())
                return {
                    "btc_losses": int(raw.get("btc_losses", 0)),
                    "eth_losses": int(raw.get("eth_losses", 0)),
                    "combined_losses": int(raw.get("combined_losses", 0)),
                    "cross_symbol_halt": bool(raw.get("cross_symbol_halt", False)),
                }
        except Exception as exc:
            log.warning("[CRYPTO-MR] state load failed, starting clean: %s", exc)
        return {"btc_losses": 0, "eth_losses": 0, "combined_losses": 0, "cross_symbol_halt": False}

    def _save(self) -> None:
        try:
            _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _STATE_PATH.write_text(json.dumps(self._state, indent=2))
        except Exception as exc:
            log.error("[CRYPTO-MR] state save failed: %s", exc)

    # ── Mutation ──────────────────────────────────────────────────────────────

    def record_loss(self, symbol: str) -> None:
        """Record a loss for symbol. Increments combined count and checks halt threshold."""
        key = self._loss_key(symbol)
        if key:
            self._state[key] = self._state.get(key, 0) + 1
        self._state["combined_losses"] = self._state.get("combined_losses", 0) + 1
        if self._state["combined_losses"] >= _HALT_THRESHOLD:
            self._state["cross_symbol_halt"] = True
            log.warning(
                "[CRYPTO-MR] cross_symbol_halt engaged after %d combined losses",
                self._state["combined_losses"],
            )
        self._save()
        log.info("[CRYPTO-MR] loss recorded: %s  combined=%d  halted=%s",
                 symbol, self._state["combined_losses"], self._state["cross_symbol_halt"])

    def record_win(self, symbol: str) -> None:
        """Record a win for symbol. Resets that symbol's consecutive loss count."""
        key = self._loss_key(symbol)
        if key:
            self._state[key] = 0
        self._save()
        log.info("[CRYPTO-MR] win recorded: %s  halted=%s", symbol, self._state["cross_symbol_halt"])

    def reset(self) -> None:
        """Clear all loss counts and lift the halt."""
        self._state = {"btc_losses": 0, "eth_losses": 0, "combined_losses": 0, "cross_symbol_halt": False}
        self._save()
        log.info("[CRYPTO-MR] state reset — halt lifted")

    # ── Query ─────────────────────────────────────────────────────────────────

    def is_halted(self) -> bool:
        return bool(self._state.get("cross_symbol_halt", False))

    def get_state(self) -> dict:
        return dict(self._state)

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _loss_key(symbol: str) -> str | None:
        s = symbol.upper().replace("/", "").replace("-", "").replace("USD", "")
        if s == "BTC":
            return "btc_losses"
        if s == "ETH":
            return "eth_losses"
        return None
