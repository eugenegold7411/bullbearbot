"""
forensic_reviewer.py — Post-trade forensic reviewer (T2.3).

For every closed trade, produces a structured ForensicRecord using a single
Haiku call assessing thesis correctness, execution quality, and management drift.
Feature flag: enable_thesis_checksum (shared with T2.1/T2.2).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import feature_flags
import model_tiering

log = logging.getLogger(__name__)

_FORENSIC_LOG = Path("data/analytics/forensic_log.jsonl")

_SYSTEM_PROMPT = (
    "You are a trading forensic analyst. Given a closed trade's entry thesis, "
    "execution details, and outcome, produce a structured assessment. "
    "Respond ONLY with valid JSON matching the schema provided. "
    "Do not invent facts not present in the input. "
    "If evidence is insufficient, set thesis_verdict to \"inconclusive\" and "
    "abstain with a reason rather than guessing."
)


# ─────────────────────────────────────────────────────────────────────────────
# Dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ForensicRecord:
    schema_version: int = 1
    forensic_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    decision_id: str = ""
    symbol: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    realized_pnl: Optional[float] = None
    hold_duration_hours: Optional[float] = None
    thesis_verdict: str = "inconclusive"
    thesis_verdict_confidence: float = 0.0
    execution_verdict: str = "neutral"
    management_drifted: bool = False
    regime_contradicted: bool = False
    what_worked: Optional[str] = None
    what_failed: Optional[str] = None
    pattern_tags: list = field(default_factory=list)
    checksum_id: Optional[str] = None
    hindsight_id: Optional[str] = None
    alpha_classification: Optional[str] = None
    abstention: Optional[dict] = None
    model_used: str = ""

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "forensic_id": self.forensic_id,
            "decision_id": self.decision_id,
            "symbol": self.symbol,
            "created_at": self.created_at,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "realized_pnl": self.realized_pnl,
            "hold_duration_hours": self.hold_duration_hours,
            "thesis_verdict": self.thesis_verdict,
            "thesis_verdict_confidence": self.thesis_verdict_confidence,
            "execution_verdict": self.execution_verdict,
            "management_drifted": self.management_drifted,
            "regime_contradicted": self.regime_contradicted,
            "what_worked": self.what_worked,
            "what_failed": self.what_failed,
            "pattern_tags": self.pattern_tags,
            "checksum_id": self.checksum_id,
            "hindsight_id": self.hindsight_id,
            "alpha_classification": self.alpha_classification,
            "abstention": self.abstention,
            "model_used": self.model_used,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ForensicRecord":
        return cls(
            schema_version=d.get("schema_version", 1),
            forensic_id=d.get("forensic_id", str(uuid.uuid4())),
            decision_id=d.get("decision_id", ""),
            symbol=d.get("symbol", ""),
            created_at=d.get("created_at", ""),
            entry_price=d.get("entry_price"),
            exit_price=d.get("exit_price"),
            realized_pnl=d.get("realized_pnl"),
            hold_duration_hours=d.get("hold_duration_hours"),
            thesis_verdict=d.get("thesis_verdict", "inconclusive"),
            thesis_verdict_confidence=d.get("thesis_verdict_confidence", 0.0),
            execution_verdict=d.get("execution_verdict", "neutral"),
            management_drifted=d.get("management_drifted", False),
            regime_contradicted=d.get("regime_contradicted", False),
            what_worked=d.get("what_worked"),
            what_failed=d.get("what_failed"),
            pattern_tags=d.get("pattern_tags", []),
            checksum_id=d.get("checksum_id"),
            hindsight_id=d.get("hindsight_id"),
            alpha_classification=d.get("alpha_classification"),
            abstention=d.get("abstention"),
            model_used=d.get("model_used", ""),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Haiku call
# ─────────────────────────────────────────────────────────────────────────────

def _build_prompt(
    symbol: str,
    entry_price: Optional[float],
    exit_price: Optional[float],
    realized_pnl: Optional[float],
    hold_duration_hours: Optional[float],
    exit_reason: str,
    entry_decision: dict,
    checksum: Optional[object],
    catalyst_obj: Optional[object],
    regime_at_entry: Optional[dict],
    regime_at_exit: Optional[dict],
    prior_repair: Optional[dict],
) -> str:
    parts = [f"SYMBOL: {symbol}"]
    if entry_price:
        parts.append(f"ENTRY_PRICE: {entry_price:.4f}")
    if exit_price:
        parts.append(f"EXIT_PRICE: {exit_price:.4f}")
    if realized_pnl is not None:
        parts.append(f"REALIZED_PNL: {realized_pnl:.2f}")
    if hold_duration_hours is not None:
        parts.append(f"HOLD_HOURS: {hold_duration_hours:.1f}")
    parts.append(f"EXIT_REASON: {exit_reason}")

    catalyst = str(entry_decision.get("catalyst", ""))[:200]
    if catalyst:
        parts.append(f"ENTRY_CATALYST: {catalyst}")

    if checksum is not None:
        try:
            parts.append(f"THESIS_TYPE: {checksum.thesis_type}")
            parts.append(f"KEY_ASSUMPTION: {checksum.key_assumption[:150]}")
            parts.append(f"INVALIDATION_CONDITION: {checksum.invalidation_condition[:150]}")
        except Exception:
            pass

    if catalyst_obj is not None:
        try:
            parts.append(f"CATALYST_TYPE: {catalyst_obj.catalyst_type}")
            parts.append(f"CATALYST_VERIFIABLE: {catalyst_obj.is_verifiable}")
        except Exception:
            pass

    if regime_at_entry:
        parts.append(f"REGIME_AT_ENTRY: bias={regime_at_entry.get('bias','?')} score={regime_at_entry.get('regime_score','?')}")
    if regime_at_exit:
        parts.append(f"REGIME_AT_EXIT: bias={regime_at_exit.get('bias','?')} score={regime_at_exit.get('regime_score','?')}")

    if prior_repair:
        parts.append(f"PRIOR_SIMILAR_REPAIR: {str(prior_repair.get('summary',''))[:100]}")

    parts.append(
        '\nRespond with JSON only:\n'
        '{"thesis_verdict":"correct|incorrect|partial|inconclusive",'
        '"thesis_verdict_confidence":0.0,"execution_verdict":"good|poor|neutral",'
        '"management_drifted":false,"regime_contradicted":false,'
        '"what_worked":null,"what_failed":null,'
        '"pattern_tags":[],"alpha_classification":"insufficient_sample",'
        '"abstention":null}'
    )
    return "\n".join(parts)


def _call_haiku(prompt: str, model: str) -> dict:
    """Makes single Haiku API call. Raises on failure."""
    import anthropic  # noqa: PLC0415
    import cost_attribution as _ca  # noqa: PLC0415

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model,
        max_tokens=512,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    input_tokens = getattr(resp.usage, "input_tokens", None)
    output_tokens = getattr(resp.usage, "output_tokens", None)
    est_cost = None
    if input_tokens and output_tokens:
        est_cost = (input_tokens / 1_000_000 * 1.00) + (output_tokens / 1_000_000 * 5.00)

    _ca.log_spine_record(
        module_name="forensic_reviewer",
        layer_name="learning_evaluation",
        ring="prod",
        model=model,
        purpose="trade_forensic",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_usd=est_cost,
    )

    content = resp.content[0].text.strip()
    # Strip markdown fences if present
    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
    return json.loads(content)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def review_closed_trade(
    decision_id: str,
    symbol: str,
    entry_price: float,
    exit_price: float,
    realized_pnl: float,
    hold_duration_hours: float,
    entry_decision: dict,
    exit_reason: str,
    regime_at_entry: Optional[dict] = None,
    regime_at_exit: Optional[dict] = None,
) -> Optional[ForensicRecord]:
    """
    Produces a ForensicRecord for a closed trade via single Haiku call.
    Creates a linked HindsightRecord. Non-fatal — returns None on any failure.
    Only fires if enable_thesis_checksum flag is True.
    """
    if not feature_flags.is_enabled("enable_thesis_checksum"):
        return None

    try:
        model = model_tiering.get_model_for_module("forensic_reviewer")

        # Fetch thesis checksum and catalyst if available
        checksum = None
        catalyst_obj = None
        checksum_id = None
        try:
            from thesis_checksum import get_checksum  # noqa: PLC0415
            checksum = get_checksum(decision_id)
            checksum_id = checksum.checksum_id if checksum else None
        except Exception:
            pass
        try:
            from catalyst_normalizer import get_catalyst  # noqa: PLC0415
            catalyst_obj = get_catalyst(decision_id)
        except Exception:
            pass

        # Fetch prior repaired failure for context
        prior_repair = None
        try:
            from experience_retrieval import retrieve_repaired_failures  # noqa: PLC0415
            cs_type = getattr(checksum, "thesis_type", None) if checksum else None
            cat_type = getattr(catalyst_obj, "catalyst_type", None) if catalyst_obj else None
            repairs = retrieve_repaired_failures(
                thesis_type=cs_type,
                catalyst_type=cat_type,
                top_n=1,
            )
            prior_repair = repairs[0] if repairs else None
        except Exception:
            pass

        prompt = _build_prompt(
            symbol=symbol,
            entry_price=entry_price,
            exit_price=exit_price,
            realized_pnl=realized_pnl,
            hold_duration_hours=hold_duration_hours,
            exit_reason=exit_reason,
            entry_decision=entry_decision,
            checksum=checksum,
            catalyst_obj=catalyst_obj,
            regime_at_entry=regime_at_entry,
            regime_at_exit=regime_at_exit,
            prior_repair=prior_repair,
        )

        llm_resp = _call_haiku(prompt, model)

        record = ForensicRecord(
            decision_id=decision_id,
            symbol=symbol,
            entry_price=entry_price,
            exit_price=exit_price,
            realized_pnl=realized_pnl,
            hold_duration_hours=hold_duration_hours,
            thesis_verdict=str(llm_resp.get("thesis_verdict", "inconclusive")),
            thesis_verdict_confidence=float(llm_resp.get("thesis_verdict_confidence", 0.0)),
            execution_verdict=str(llm_resp.get("execution_verdict", "neutral")),
            management_drifted=bool(llm_resp.get("management_drifted", False)),
            regime_contradicted=bool(llm_resp.get("regime_contradicted", False)),
            what_worked=llm_resp.get("what_worked"),
            what_failed=llm_resp.get("what_failed"),
            pattern_tags=list(llm_resp.get("pattern_tags", [])),
            checksum_id=checksum_id,
            alpha_classification=llm_resp.get("alpha_classification", "insufficient_sample"),
            abstention=llm_resp.get("abstention"),
            model_used=model,
        )

        # Create HindsightRecord linked to this trade
        try:
            from hindsight import build_hindsight_record, log_hindsight_record  # noqa: PLC0415
            hs = build_hindsight_record(
                subject_id=decision_id,
                subject_type="decision",
                verdict="verified" if record.thesis_verdict == "correct" else (
                    "falsified" if record.thesis_verdict == "incorrect" else "inconclusive"
                ),
                evidence_summary=(record.what_worked or record.what_failed or "forensic review"),
                module_name="forensic_reviewer",
            )
            hindsight_id = log_hindsight_record(hs)
            record.hindsight_id = hindsight_id
        except Exception as _hs_exc:
            log.debug("[FORENSIC] hindsight link failed (non-fatal): %s", _hs_exc)

        log_forensic(record)

        # Auto-create experience record
        try:
            from experience_library import build_experience_from_forensic, save_experience  # noqa: PLC0415
            rec_type = (
                "success_case" if record.thesis_verdict == "correct" else
                "failure_case" if record.thesis_verdict == "incorrect" else
                "failure_case"
            )
            exp = build_experience_from_forensic(record, record_type=rec_type)
            save_experience(exp)
        except Exception as _exp_exc:
            log.debug("[FORENSIC] experience auto-save failed (non-fatal): %s", _exp_exc)

        return record

    except Exception as exc:  # noqa: BLE001
        log.warning("[FORENSIC] review_closed_trade failed (non-fatal): %s", exc)
        return None


def log_forensic(record: ForensicRecord) -> Optional[str]:
    """Appends to forensic_log.jsonl. Returns forensic_id or None."""
    try:
        _FORENSIC_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _FORENSIC_LOG.open("a") as f:
            f.write(json.dumps(record.to_dict()) + "\n")
        return record.forensic_id
    except Exception as exc:
        log.warning("[FORENSIC] log_forensic failed: %s", exc)
        return None


def get_forensic(decision_id: str) -> Optional[ForensicRecord]:
    """Reads JSONL, finds first matching decision_id. Returns None if not found."""
    try:
        if not _FORENSIC_LOG.exists():
            return None
        for line in _FORENSIC_LOG.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if d.get("decision_id") == decision_id:
                    return ForensicRecord.from_dict(d)
            except Exception:
                continue
        return None
    except Exception as exc:
        log.warning("[FORENSIC] get_forensic failed: %s", exc)
        return None


def get_forensics(
    symbol: Optional[str] = None,
    thesis_verdict: Optional[str] = None,
    days_back: int = 90,
) -> list[ForensicRecord]:
    """Filtered retrieval of forensic records. Returns [] on error."""
    try:
        if not _FORENSIC_LOG.exists():
            return []
        from datetime import timedelta as _td  # noqa: PLC0415
        cutoff = datetime.now(timezone.utc) - _td(days=days_back)
        results = []
        for line in _FORENSIC_LOG.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                ts_str = d.get("created_at", "")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if ts < cutoff:
                            continue
                    except Exception:
                        pass
                if symbol and d.get("symbol") != symbol:
                    continue
                if thesis_verdict and d.get("thesis_verdict") != thesis_verdict:
                    continue
                results.append(ForensicRecord.from_dict(d))
            except Exception:
                continue
        return results
    except Exception as exc:
        log.warning("[FORENSIC] get_forensics failed: %s", exc)
        return []


def format_forensic_summary_for_review(days_back: int = 30) -> str:
    """Returns markdown summary for Agent 4 injection. Returns '' on error/no data."""
    try:
        records = get_forensics(days_back=days_back)
        if not records:
            return ""
        verdicts: dict[str, int] = {}
        for r in records:
            verdicts[r.thesis_verdict] = verdicts.get(r.thesis_verdict, 0) + 1
        lines = [f"### Forensic Review Summary (last {days_back}d)", ""]
        lines.append(f"Total reviews: {len(records)}")
        for v, count in sorted(verdicts.items(), key=lambda x: -x[1]):
            lines.append(f"- {v}: {count}")
        return "\n".join(lines)
    except Exception:
        return ""
