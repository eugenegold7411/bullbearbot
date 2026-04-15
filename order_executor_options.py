"""
order_executor_options.py — Options order submission for Account 2.

Thin wrapper: validates equity floor, handles observation mode, then delegates
real submission to options_executor.submit_structure().
Logs all outcomes to data/account2/positions/options_log.jsonl.
"""

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from schemas import OptionsStructure, StructureLifecycle

log = logging.getLogger(__name__)

_LOG_PATH = Path(__file__).parent / "data" / "account2" / "positions" / "options_log.jsonl"
_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

ET = ZoneInfo("America/New_York")


@dataclass
class OptionsExecutionResult:
    symbol: str
    option_strategy: str
    action: str
    status: str                        # "submitted" | "rejected" | "observation" | "error"
    reason: str = ""
    order_id: Optional[str] = None
    structure_id: Optional[str] = None
    max_cost_usd: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now(ET).isoformat())
    observation_mode: bool = False
    iv_rank: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


def _get_options_client():
    """Build Alpaca TradingClient from Account 2 credentials."""
    from alpaca.trading.client import TradingClient
    api_key = os.environ.get("ALPACA_API_KEY_OPTIONS")
    secret_key = os.environ.get("ALPACA_SECRET_KEY_OPTIONS")
    base_url = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

    if not api_key or not secret_key:
        raise RuntimeError(
            "ALPACA_API_KEY_OPTIONS / ALPACA_SECRET_KEY_OPTIONS not set in environment"
        )

    paper = "paper" in base_url
    return TradingClient(api_key=api_key, secret_key=secret_key, paper=paper)


def submit_options_order(
    structure: OptionsStructure,
    equity: float,
    observation_mode: bool = False,
) -> OptionsExecutionResult:
    """
    Thin wrapper around options_executor.submit_structure().
    Validates equity floor, handles observation mode, then delegates.
    The submitted OptionsStructure is persisted by options_executor.

    Args:
        structure: built OptionsStructure from options_builder
        equity: Account 2 equity for floor check
        observation_mode: if True, log but do not submit
    """
    import options_executor

    sym = structure.underlying
    strategy = structure.strategy.value

    # Equity floor
    if equity < 25_000:
        log.warning("[OPTS_EXEC] REJECTED %s — equity $%.0f below $25K floor", sym, equity)
        return OptionsExecutionResult(
            symbol=sym, option_strategy=strategy, action=strategy,
            status="rejected",
            reason=f"equity ${equity:.0f} below $25K floor",
            structure_id=structure.structure_id,
            max_cost_usd=structure.max_cost_usd,
            iv_rank=structure.iv_rank,
        )

    # Observation mode — record intent, no submission
    if observation_mode:
        log.info("[OPTS_EXEC] [OBS] Would submit: %s %s max_cost=$%.0f",
                 sym, strategy, structure.max_cost_usd)
        result = OptionsExecutionResult(
            symbol=sym,
            option_strategy=strategy,
            action=strategy,
            status="observation",
            reason="observation_mode",
            structure_id=structure.structure_id,
            max_cost_usd=structure.max_cost_usd,
            iv_rank=structure.iv_rank,
            observation_mode=True,
        )
        _log_result(result)
        return result

    # Live submission
    try:
        client = _get_options_client()
        filled = options_executor.submit_structure(structure, client, config={})

        _SUBMITTED_OK = {
            StructureLifecycle.SUBMITTED,
            StructureLifecycle.PARTIALLY_FILLED,
            StructureLifecycle.FULLY_FILLED,
        }
        status = "submitted" if filled.lifecycle in _SUBMITTED_OK else (
            "rejected" if filled.lifecycle == StructureLifecycle.REJECTED else "error"
        )
        reason = filled.audit_log[-1] if filled.audit_log else ""

        result = OptionsExecutionResult(
            symbol=sym,
            option_strategy=strategy,
            action=strategy,
            status=status,
            reason=reason,
            order_id=",".join(filled.order_ids) if filled.order_ids else None,
            structure_id=filled.structure_id,
            max_cost_usd=filled.max_cost_usd,
            iv_rank=filled.iv_rank,
        )
        log.info("[OPTS_EXEC] %s %s %s structure_id=%s",
                 status.upper(), sym, strategy, filled.structure_id)
        _log_result(result)
        return result

    except Exception as exc:
        log.error("[OPTS_EXEC] %s: submission error: %s", sym, exc)
        result = OptionsExecutionResult(
            symbol=sym, option_strategy=strategy, action=strategy,
            status="error",
            reason=str(exc),
            structure_id=structure.structure_id,
        )
        _log_result(result)
        return result


def _log_result(result: OptionsExecutionResult):
    """Append execution result to JSONL log."""
    try:
        with open(_LOG_PATH, "a") as f:
            f.write(json.dumps(result.to_dict()) + "\n")
    except Exception as exc:
        log.debug("[OPTS_EXEC] Failed to write options log: %s", exc)
