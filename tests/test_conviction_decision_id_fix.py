"""
tests/test_conviction_decision_id_fix.py

Tests for two A2 bug fixes:

Fix 1 — options_intelligence.py reads "conviction" not "confidence"
  PLTR with conviction:"medium" was silently blocked by the confidence gate
  because signal_data.get("confidence", "low") always defaulted to "low".
  The gate now reads conviction first, falls back to confidence (legacy).

Fix 2 — decision_id populated in persisted A2DecisionRecord artifacts
  A2DecisionRecord.decision_id was always "" for the no-candidate / rollback
  paths. persist_decision_record() in stage4 now backfills it to
  "a2_dec_YYYYMMDD_HHMMSS" when the field is empty.
"""

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_REPO = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO))

import options_intelligence as oi


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iv_summary(env="cheap", obs_mode=False) -> dict:
    return {
        "symbol":          "PLTR",
        "iv_environment":  env,
        "iv_rank":         25.0,
        "current_iv":      0.22,
        "history_days":    30,
        "observation_mode": obs_mode,
    }


def _options_regime(regime="normal") -> dict:
    return {
        "regime":             regime,
        "allowed_strategies": ["debit_spread", "single_leg", "credit_spread"],
        "size_multiplier":    1.0,
    }


def _call(signal_data: dict):
    """Call select_options_strategy with minimal valid args and given signal_data."""
    return oi.select_options_strategy(
        symbol="PLTR",
        iv_summary=_iv_summary(),
        signal_data=signal_data,
        vix=18.0,
        tier="core",
        catalyst="earnings beat",
        current_price=25.0,
        equity=100_000.0,
        options_regime=_options_regime(),
    )


# ---------------------------------------------------------------------------
# Fix 1 — conviction field tests
# ---------------------------------------------------------------------------

class TestConvictionField:
    """The confidence gate must read 'conviction', not 'confidence'."""

    def test_conviction_medium_passes_gate(self):
        """conviction:'medium' → conf_score=0.75 → passes the ≥0.75 gate."""
        result = _call({"score": 70, "conviction": "medium", "direction": "bullish", "price": 25.0})
        assert result is not None, (
            "conviction:'medium' must produce a StructureProposal — was blocked by stale "
            "'confidence' field read before fix"
        )

    def test_conviction_high_passes_gate(self):
        """conviction:'high' → conf_score=0.9 → passes gate."""
        result = _call({"score": 80, "conviction": "high", "direction": "bullish", "price": 25.0})
        assert result is not None

    def test_conviction_low_blocked(self):
        """conviction:'low' → conf_score=0.5 < 0.75 → blocked."""
        result = _call({"score": 60, "conviction": "low", "direction": "bullish", "price": 25.0})
        assert result is None, "conviction:'low' must return None (blocked by confidence gate)"

    def test_missing_conviction_defaults_to_low_blocked(self):
        """No conviction AND no confidence → defaults to 'low' → blocked."""
        result = _call({"score": 65, "direction": "bullish", "price": 25.0})
        assert result is None, "missing conviction must default to 'low' and be blocked"

    def test_legacy_confidence_field_still_works(self):
        """Legacy 'confidence':'high' (no conviction key) still passes — backward compat."""
        result = _call({"score": 75, "confidence": "high", "direction": "bullish", "price": 25.0})
        assert result is not None, (
            "legacy 'confidence' field must still work as fallback when 'conviction' absent"
        )

    def test_legacy_confidence_medium_still_works(self):
        """Legacy 'confidence':'medium' passes gate."""
        result = _call({"score": 72, "confidence": "medium", "direction": "bullish", "price": 25.0})
        assert result is not None

    def test_conviction_takes_priority_over_confidence(self):
        """When both fields present, conviction wins."""
        # conviction=medium (passes), confidence=low (would block) → should pass
        result = _call({
            "score": 70,
            "conviction":  "medium",   # should win
            "confidence":  "low",      # should be ignored
            "direction":   "bullish",
            "price":       25.0,
        })
        assert result is not None, "conviction must take priority over confidence"

    def test_conviction_high_beats_confidence_low(self):
        """conviction=high wins over confidence=low."""
        result = _call({
            "score":      80,
            "conviction": "high",
            "confidence": "low",
            "direction":  "bearish",
            "price":      25.0,
        })
        assert result is not None


# ---------------------------------------------------------------------------
# Fix 2 — decision_id backfill in persist_decision_record
# ---------------------------------------------------------------------------

@dataclass
class _MinimalDecisionRecord:
    """Minimal stand-in for A2DecisionRecord."""
    decision_id:       str = ""
    session_tier:      str = "market"
    debate_parsed:     dict = None
    selected_candidate: dict = None
    execution_result:  str = "no_trade"
    no_trade_reason:   str = "no_candidates"
    elapsed_seconds:   float = 0.1
    schema_version:    int = 1
    code_version:      str = "test"
    built_at:          str = ""
    candidate_sets:    list = field(default_factory=list)
    debate_input:      str = None
    debate_output_raw: str = None


class TestDecisionIdBackfill:
    """decision_id must be non-empty in every persisted artifact."""

    def _persist(self, decision_record, tmp_path: Path) -> dict:
        """Call persist_decision_record and return the written JSON."""
        from bot_options_stage4_execution import persist_decision_record

        with patch("bot_options_stage4_execution._DECISIONS_DIR", tmp_path):
            persist_decision_record(decision_record)

        files = list(tmp_path.glob("a2_dec_*.json"))
        assert files, "persist_decision_record must write at least one file"
        return json.loads(files[0].read_text())

    def test_empty_decision_id_is_backfilled(self, tmp_path):
        """decision_id='' must be replaced with a2_dec_YYYYMMDD_HHMMSS before write."""
        record = _MinimalDecisionRecord(decision_id="")
        saved = self._persist(record, tmp_path)
        assert saved["decision_id"], "decision_id must not be empty in persisted artifact"
        assert saved["decision_id"].startswith("a2_dec_"), \
            f"decision_id must start with 'a2_dec_', got: {saved['decision_id']!r}"

    def test_non_empty_decision_id_preserved(self, tmp_path):
        """A pre-populated decision_id must not be overwritten."""
        record = _MinimalDecisionRecord(decision_id="a2_dec_20260421_093500")
        saved = self._persist(record, tmp_path)
        assert saved["decision_id"] == "a2_dec_20260421_093500"

    def test_backfilled_id_matches_filename(self, tmp_path):
        """The backfilled decision_id must match the filename timestamp."""
        record = _MinimalDecisionRecord(decision_id="")
        from bot_options_stage4_execution import persist_decision_record

        with patch("bot_options_stage4_execution._DECISIONS_DIR", tmp_path):
            persist_decision_record(record)

        files = list(tmp_path.glob("a2_dec_*.json"))
        assert files
        filename_ts = files[0].stem          # a2_dec_YYYYMMDD_HHMMSS
        saved = json.loads(files[0].read_text())
        assert saved["decision_id"] == filename_ts, \
            f"decision_id {saved['decision_id']!r} must equal filename stem {filename_ts!r}"

    def test_decision_id_format_is_a2_dec_timestamp(self, tmp_path):
        """Backfilled ID format: a2_dec_YYYYMMDD_HHMMSS (8+6 digits)."""
        import re
        record = _MinimalDecisionRecord(decision_id="")
        saved = self._persist(record, tmp_path)
        assert re.match(r"^a2_dec_\d{8}_\d{6}$", saved["decision_id"]), \
            f"decision_id format wrong: {saved['decision_id']!r}"
