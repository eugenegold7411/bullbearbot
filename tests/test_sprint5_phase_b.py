"""
tests/test_sprint5_phase_b.py — Sprint 5 Phase B:
  - catalyst_type self-classification in L3 Haiku output
  - EARNINGS_PENDING enum addition (taxonomy v1.1.0)
  - Macro wire hits injection into L3 per-symbol block
  - classify_catalyst fallback when Haiku returns unknown

Tests:
  PB-01  EARNINGS_PENDING in CatalystType enum with correct value
  PB-02  SEMANTIC_LABELS_VERSION == 2
  PB-03  catalyst_type taxonomy values listed in _L3_SYSTEM
  PB-04  _run_l3_synthesis uses Haiku's catalyst_type when valid known value
  PB-05  _run_l3_synthesis falls back to classify_catalyst when Haiku returns "unknown"
  PB-06  _run_l3_synthesis falls back to classify_catalyst when catalyst_type missing
  PB-07  _get_macro_wire_hits_for_symbol returns hits from live_cache.json
  PB-08  _format_l2_for_l3 injects MACRO_WIRE line when hits exist
  PB-09  _format_l2_for_l3 skips MACRO_WIRE line when no hits
  PB-10  catalyst_normalizer has synonyms for earnings_pending
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch


# ─────────────────────────────────────────────────────────────────────────────
# PB-01 / PB-02 — Enum and version
# ─────────────────────────────────────────────────────────────────────────────

class TestTaxonomyV11:
    def test_pb01_earnings_pending_in_enum(self):
        from semantic_labels import CatalystType
        assert CatalystType.EARNINGS_PENDING.value == "earnings_pending"

    def test_pb01_earnings_pending_in_all_values(self):
        from semantic_labels import CatalystType
        assert "earnings_pending" in {e.value for e in CatalystType}

    def test_pb02_semantic_labels_version_is_2(self):
        from semantic_labels import SEMANTIC_LABELS_VERSION
        assert SEMANTIC_LABELS_VERSION == 2

    def test_hard_cap_not_exceeded(self):
        """taxonomy hard cap is 20 active labels; we must be at or under it."""
        from semantic_labels import CatalystType
        assert len(list(CatalystType)) <= 20, (
            f"CatalystType has {len(list(CatalystType))} values — exceeds hard cap of 20"
        )


# ─────────────────────────────────────────────────────────────────────────────
# PB-03 — _L3_SYSTEM schema contains catalyst_type field
# ─────────────────────────────────────────────────────────────────────────────

class TestL3SystemSchema:
    def test_pb03_catalyst_type_in_l3_schema(self):
        from bot_stage2_signal import _L3_SYSTEM
        assert '"catalyst_type"' in _L3_SYSTEM, (
            "_L3_SYSTEM schema must include catalyst_type field"
        )

    def test_pb03_earnings_pending_listed_in_l3_schema(self):
        from bot_stage2_signal import _L3_SYSTEM
        assert "earnings_pending" in _L3_SYSTEM, (
            "_L3_SYSTEM must list earnings_pending as a valid catalyst_type value"
        )

    def test_pb03_unknown_listed_in_l3_schema(self):
        from bot_stage2_signal import _L3_SYSTEM
        assert '"unknown"' in _L3_SYSTEM

    def test_pb03_instruction_5_added(self):
        from bot_stage2_signal import _L3_SYSTEM
        assert "catalyst_type" in _L3_SYSTEM
        assert "taxonomy" in _L3_SYSTEM.lower() or "ONLY" in _L3_SYSTEM


# ─────────────────────────────────────────────────────────────────────────────
# PB-04 / PB-05 / PB-06 — catalyst_type resolution logic in _run_l3_synthesis
# ─────────────────────────────────────────────────────────────────────────────

def _make_l3_result_with_catalyst_type(catalyst_type: str, primary_catalyst: str = "") -> dict:
    """Build a minimal batch_result dict that _run_l3_synthesis would receive."""
    return {
        "scored_symbols": {
            "NVDA": {
                "score": 72,
                "direction": "bullish",
                "conviction": "high",
                "signals": ["momentum"],
                "conflicts": [],
                "primary_catalyst": primary_catalyst,
                "catalyst_type": catalyst_type,
                "orb_candidate": False,
                "pattern_watchlist": False,
                "tier": "core",
                "l2_score": 70,
                "l3_adjustment": 2,
                "adjustment_reason": "",
            }
        },
        "top_3": ["NVDA"],
        "elevated_caution": [],
        "reasoning": "Test run",
    }


class TestCatalystTypeResolution:
    def test_pb04_haiku_valid_type_used_directly(self, monkeypatch):
        """When Haiku returns a valid known catalyst_type, it is used as-is."""
        import bot_stage2_signal as bss

        fake_result = _make_l3_result_with_catalyst_type(
            "insider_buy", "C-suite insider purchase on open market"
        )
        monkeypatch.setattr(bss, "_call_l3_batch", lambda uc: fake_result)
        monkeypatch.setattr(bss, "_get_macro_wire_hits_for_symbol", lambda sym: [])
        monkeypatch.setattr(bss, "_load_qualitative_context", lambda: {})

        l2 = {"NVDA": {"score": 70, "direction": "bullish", "conviction": "high",
                       "signals": [], "conflicts": [], "price": 900.0}}
        result = bss._run_l3_synthesis(["NVDA"], l2, {}, {"regime_score": 60, "bias": "bullish"}, [])
        ct = result["scored_symbols"]["NVDA"]["catalyst_type"]
        assert ct == "insider_buy", f"Expected 'insider_buy', got {ct!r}"

    def test_pb04_earnings_pending_used_directly(self, monkeypatch):
        """When Haiku returns 'earnings_pending', it is used as-is."""
        import bot_stage2_signal as bss

        fake_result = _make_l3_result_with_catalyst_type(
            "earnings_pending", "earnings in 2 days momentum setup"
        )
        monkeypatch.setattr(bss, "_call_l3_batch", lambda uc: fake_result)
        monkeypatch.setattr(bss, "_get_macro_wire_hits_for_symbol", lambda sym: [])
        monkeypatch.setattr(bss, "_load_qualitative_context", lambda: {})

        l2 = {"NVDA": {"score": 68, "direction": "bullish", "conviction": "medium",
                       "signals": [], "conflicts": [], "price": 900.0, "earnings_days_away": 2}}
        result = bss._run_l3_synthesis(["NVDA"], l2, {}, {"regime_score": 60, "bias": "bullish"}, [])
        ct = result["scored_symbols"]["NVDA"]["catalyst_type"]
        assert ct == "earnings_pending", f"Expected 'earnings_pending', got {ct!r}"

    def test_pb05_fallback_to_classify_when_haiku_returns_unknown(self, monkeypatch):
        """When Haiku returns 'unknown', classify_catalyst() runs on primary_catalyst text."""
        import bot_stage2_signal as bss

        fake_result = _make_l3_result_with_catalyst_type(
            "unknown", "earnings beat consensus by 12 percent"
        )
        monkeypatch.setattr(bss, "_call_l3_batch", lambda uc: fake_result)
        monkeypatch.setattr(bss, "_get_macro_wire_hits_for_symbol", lambda sym: [])
        monkeypatch.setattr(bss, "_load_qualitative_context", lambda: {})

        l2 = {"NVDA": {"score": 78, "direction": "bullish", "conviction": "high",
                       "signals": [], "conflicts": [], "price": 900.0}}
        result = bss._run_l3_synthesis(["NVDA"], l2, {}, {"regime_score": 60, "bias": "bullish"}, [])
        ct = result["scored_symbols"]["NVDA"]["catalyst_type"]
        # "earnings beat consensus" should match earnings_beat via classify_catalyst
        assert ct == "earnings_beat", f"Expected fallback to 'earnings_beat', got {ct!r}"

    def test_pb06_fallback_when_catalyst_type_missing(self, monkeypatch):
        """When Haiku omits catalyst_type entirely, classify_catalyst() runs."""
        import bot_stage2_signal as bss

        fake_result = _make_l3_result_with_catalyst_type("", "fed signals rate cut pause")
        # Remove the catalyst_type key entirely
        del fake_result["scored_symbols"]["NVDA"]["catalyst_type"]
        monkeypatch.setattr(bss, "_call_l3_batch", lambda uc: fake_result)
        monkeypatch.setattr(bss, "_get_macro_wire_hits_for_symbol", lambda sym: [])
        monkeypatch.setattr(bss, "_load_qualitative_context", lambda: {})

        l2 = {"NVDA": {"score": 65, "direction": "neutral", "conviction": "medium",
                       "signals": [], "conflicts": [], "price": 900.0}}
        result = bss._run_l3_synthesis(["NVDA"], l2, {}, {"regime_score": 60, "bias": "neutral"}, [])
        ct = result["scored_symbols"]["NVDA"]["catalyst_type"]
        # "fed signals rate cut pause" should match fed_signal via classify_catalyst
        assert ct == "fed_signal", f"Expected fallback to 'fed_signal', got {ct!r}"


# ─────────────────────────────────────────────────────────────────────────────
# PB-07 / PB-08 / PB-09 — Macro wire hits injection
# ─────────────────────────────────────────────────────────────────────────────

class TestMacroWireInjection:
    def test_pb07_get_macro_wire_hits_returns_matching_headlines(self, tmp_path, monkeypatch):
        """_get_macro_wire_hits_for_symbol reads live_cache.json and filters by symbol."""
        import bot_stage2_signal as bss

        cache = [
            {"headline": "NVDA AI chip demand surges amid enterprise spending",
             "affected_symbols": ["NVDA", "AMD"]},
            {"headline": "Fed holds rates steady at March meeting",
             "affected_symbols": ["SPY", "TLT"]},
            {"headline": "NVDA faces export control challenges",
             "affected_symbols": ["NVDA"]},
        ]
        cache_path = tmp_path / "data" / "macro_wire" / "live_cache.json"
        cache_path.parent.mkdir(parents=True)
        cache_path.write_text(json.dumps(cache))

        # Patch _BASE so _get_macro_wire_hits_for_symbol reads from tmp_path
        monkeypatch.setattr(bss, "_BASE", tmp_path)

        hits = bss._get_macro_wire_hits_for_symbol("NVDA")
        assert len(hits) == 2
        assert any("NVDA AI chip" in h for h in hits)
        assert any("export control" in h for h in hits)

    def test_pb07_get_macro_wire_hits_empty_when_no_cache(self, tmp_path, monkeypatch):
        """Returns [] when live_cache.json does not exist."""
        import bot_stage2_signal as bss
        monkeypatch.setattr(bss, "_BASE", tmp_path)
        hits = bss._get_macro_wire_hits_for_symbol("NVDA")
        assert hits == []

    def test_pb08_format_l2_injects_macro_wire_line(self, monkeypatch):
        """_format_l2_for_l3 includes MACRO_WIRE line when hits exist."""
        import bot_stage2_signal as bss
        monkeypatch.setattr(bss, "_get_macro_wire_hits_for_symbol",
                            lambda sym: ["NVDA AI chip demand surges"])
        block = bss._format_l2_for_l3(
            "NVDA",
            {"score": 70, "direction": "bullish", "conviction": "high", "signals": [], "conflicts": []},
            None,
            900.0,
        )
        assert "MACRO_WIRE" in block
        assert "NVDA AI chip demand surges" in block

    def test_pb09_format_l2_no_macro_wire_line_when_no_hits(self, monkeypatch):
        """_format_l2_for_l3 omits MACRO_WIRE line when no hits."""
        import bot_stage2_signal as bss
        monkeypatch.setattr(bss, "_get_macro_wire_hits_for_symbol", lambda sym: [])
        block = bss._format_l2_for_l3(
            "NVDA",
            {"score": 70, "direction": "bullish", "conviction": "high", "signals": [], "conflicts": []},
            None,
            900.0,
        )
        assert "MACRO_WIRE" not in block


# ─────────────────────────────────────────────────────────────────────────────
# PB-10 — catalyst_normalizer has EARNINGS_PENDING synonyms
# ─────────────────────────────────────────────────────────────────────────────

class TestCatalystNormalizerEarningsPending:
    def test_pb10_earnings_pending_in_catalyst_keywords(self):
        from catalyst_normalizer import _CATALYST_KEYWORDS
        from semantic_labels import CatalystType
        assert CatalystType.EARNINGS_PENDING.value in _CATALYST_KEYWORDS

    def test_earnings_pending_synonyms_non_empty(self):
        from catalyst_normalizer import _CATALYST_KEYWORDS
        from semantic_labels import CatalystType
        synonyms = _CATALYST_KEYWORDS[CatalystType.EARNINGS_PENDING.value]
        assert len(synonyms) > 0

    def test_classify_catalyst_maps_earnings_in_n_days(self):
        """'earnings in 3 days setup' should map to earnings_pending via normalizer."""
        from catalyst_normalizer import normalize_catalyst
        result = normalize_catalyst(
            "earnings in 3 days bullish pre-announcement",
            decision_id="test-001",
            symbol="NVDA",
        )
        assert result.catalyst_type == "earnings_pending", (
            f"Expected 'earnings_pending', got {result.catalyst_type!r}"
        )
