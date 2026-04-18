"""
bot_stage2_5_scratchpad.py — Stage 2.5: Haiku pre-decision scratchpad.

Public API:
  run_scratchpad_stage(signal_scores_obj, regime_obj, md, positions) -> dict
"""

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
    Run Stage 2.5 Haiku scratchpad, persist hot + cold memory.
    Returns empty dict on any failure — never blocks the pipeline.
    """
    if not signal_scores_obj:
        return {}
    try:
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
