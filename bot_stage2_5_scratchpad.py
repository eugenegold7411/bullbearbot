"""
bot_stage2_5_scratchpad.py — Stage 2.5: Haiku pre-decision scratchpad.

Public API:
  run_scratchpad_stage(signal_scores_obj, regime_obj, md, positions) -> dict

When score_signals_layered() runs the merged L3+Scratchpad call, the scratchpad
is already embedded in signal_scores_obj["scratchpad"]. This function extracts
it (no second API call) and persists hot + cold memory as before.
Falls back to a standalone Haiku scratchpad call when no embedded scratchpad.
"""

from datetime import datetime, timezone

import scratchpad as _scratchpad
import trade_memory
from log_setup import get_logger

log = get_logger(__name__)


def run_scratchpad_stage(
    signal_scores_obj: dict,
    regime_obj: dict,
    md: dict,
    positions: list,
) -> dict:
    """
    Stage 2.5: persist scratchpad and return it.

    Fast path: if the merged L3 call already produced a scratchpad section in
    signal_scores_obj["scratchpad"], extract it, stamp metadata, and persist
    without making a new API call.

    Fallback: if no embedded scratchpad, run a standalone Haiku call (original
    behaviour) so the pipeline is never left without pre-analysis.

    Returns empty dict on any failure — never blocks the pipeline.
    """
    if not signal_scores_obj:
        return {}
    try:
        embedded = signal_scores_obj.get("scratchpad")
        if isinstance(embedded, dict) and embedded.get("watching") is not None:
            result = dict(embedded)
            result.setdefault("ts",           datetime.now(timezone.utc).isoformat())
            result.setdefault("regime_score", int((regime_obj or {}).get("regime_score", 50)))
            result.setdefault("vix",          float((md or {}).get("vix", 0) or 0))
            if result:
                _scratchpad.save_hot_scratchpad(result)
                trade_memory.save_scratchpad_memory(result)
            log.debug("[SCRATCHPAD] extracted from merged L3 result (no extra API call)")
            return result

        # Fallback: standalone Haiku call (merged call absent or failed)
        log.debug("[SCRATCHPAD] no embedded scratchpad — running standalone Haiku call")
        result = _scratchpad.run_scratchpad(
            signal_scores     = signal_scores_obj,
            regime            = regime_obj,
            market_conditions = md,
            positions         = positions,
        )
        if result:
            _scratchpad.save_hot_scratchpad(result)
            trade_memory.save_scratchpad_memory(result)
        return result or {}
    except Exception as exc:
        log.warning("[SCRATCHPAD] Stage 2.5 failed (non-fatal): %s", exc)
        return {}
