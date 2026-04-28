"""
tests/test_sprint5_phase_d.py — Sprint 5 Phase D:
  - _build_pre_earnings_intel_section() wired into morning_brief._load_context()
  - earnings intel injected for symbols ≤ 5 days from earnings
  - section absent when no symbols near earnings
  - capped at 3 symbols
  - prompts/system_v1.txt references PRE-EARNINGS INTELLIGENCE

Tests:
  PD-01  _build_pre_earnings_intel_section returns non-empty when symbol ≤ 5 days away
  PD-02  _build_pre_earnings_intel_section returns "" when no upcoming earnings
  PD-03  _build_pre_earnings_intel_section skips symbols > 5 days away
  PD-04  _build_pre_earnings_intel_section caps output at 3 symbols
  PD-05  _build_pre_earnings_intel_section is non-fatal (returns "" on exception)
  PD-06  _load_context includes PRE-EARNINGS INTELLIGENCE when near-earnings symbols exist
  PD-07  _load_context skips PRE-EARNINGS INTELLIGENCE when no near-earnings symbols
  PD-08  system_v1.txt references PRE-EARNINGS INTELLIGENCE section
  PD-09  system_v1.txt references earnings_pending catalyst_type
  PD-10  _build_pre_earnings_intel_section calls get_earnings_intel_section per symbol
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_calendar(*symbols_and_days: tuple[str, int]) -> dict:
    """Build a fake earnings calendar with (symbol, days_away) pairs."""
    today = date.today()
    return {
        "calendar": [
            {
                "symbol": sym,
                "earnings_date": (today + timedelta(days=n)).isoformat(),
            }
            for sym, n in symbols_and_days
        ]
    }


# ─────────────────────────────────────────────────────────────────────────────
# PD-01 / PD-02 / PD-03 / PD-04 / PD-05 — _build_pre_earnings_intel_section
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildPreEarningsIntelSection:
    def test_pd01_returns_section_when_symbol_near_earnings(self):
        """Returns non-empty string when a symbol is ≤ 5 days from earnings."""
        import morning_brief as mb

        cal = _make_calendar(("NVDA", 3))

        with patch("data_warehouse.load_earnings_calendar", return_value=cal), \
             patch("earnings_intel.get_earnings_intel_section",
                   return_value="  NVDA (reports in 3 days) — last quarter:\n  Signal: BULLISH"):
            result = mb._build_pre_earnings_intel_section()

        assert "PRE-EARNINGS INTELLIGENCE" in result
        assert "NVDA" in result

    def test_pd02_returns_empty_when_no_upcoming_earnings(self, monkeypatch):
        """Returns '' when no symbols have upcoming earnings."""
        import morning_brief as mb

        cal = {"calendar": []}
        with patch("data_warehouse.load_earnings_calendar", return_value=cal):
            result = mb._build_pre_earnings_intel_section()

        assert result == ""

    def test_pd03_skips_symbols_more_than_5_days_away(self, monkeypatch):
        """Symbols with earnings > 5 days away are excluded."""
        import morning_brief as mb

        cal = _make_calendar(("AAPL", 10), ("MSFT", 14))
        with patch("data_warehouse.load_earnings_calendar", return_value=cal), \
             patch("earnings_intel.get_earnings_intel_section") as mock_intel:
            result = mb._build_pre_earnings_intel_section()

        mock_intel.assert_not_called()
        assert result == ""

    def test_pd04_caps_at_3_symbols(self, monkeypatch):
        """Only the first 3 near-earnings symbols are processed."""
        import morning_brief as mb

        cal = _make_calendar(("AAPL", 1), ("NVDA", 2), ("MSFT", 3), ("AMZN", 4), ("TSLA", 5))
        calls: list[str] = []

        def fake_intel(sym, n_days):
            calls.append(sym)
            return f"  {sym}: signal BULLISH"

        with patch("data_warehouse.load_earnings_calendar", return_value=cal), \
             patch("earnings_intel.get_earnings_intel_section", side_effect=fake_intel):
            mb._build_pre_earnings_intel_section()

        assert len(calls) <= 3, f"Called intel for {len(calls)} symbols, expected ≤ 3"

    def test_pd05_non_fatal_on_exception(self, monkeypatch):
        """Returns '' gracefully when load_earnings_calendar raises."""
        import morning_brief as mb

        with patch("data_warehouse.load_earnings_calendar", side_effect=RuntimeError("DB error")):
            result = mb._build_pre_earnings_intel_section()

        assert result == ""

    def test_pd10_calls_get_earnings_intel_section_per_symbol(self, monkeypatch):
        """get_earnings_intel_section is called once per qualifying symbol."""
        import morning_brief as mb

        cal = _make_calendar(("GLD", 2), ("XBI", 4))
        calls: list[tuple] = []

        def fake_intel(sym, n_days):
            calls.append((sym, n_days))
            return f"  {sym}: signal NEUTRAL"

        with patch("data_warehouse.load_earnings_calendar", return_value=cal), \
             patch("earnings_intel.get_earnings_intel_section", side_effect=fake_intel):
            mb._build_pre_earnings_intel_section()

        assert len(calls) == 2
        syms = [c[0] for c in calls]
        assert "GLD" in syms
        assert "XBI" in syms


# ─────────────────────────────────────────────────────────────────────────────
# PD-06 / PD-07 — _load_context integration
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadContextIntegration:
    def test_pd06_load_context_includes_pre_earnings_when_near_symbols(self, monkeypatch):
        """_load_context output includes PRE-EARNINGS INTELLIGENCE section when symbols near."""
        import morning_brief as mb

        monkeypatch.setattr(
            mb, "_build_pre_earnings_intel_section",
            lambda: "\n=== PRE-EARNINGS INTELLIGENCE ===\n  NVDA (reports in 2 days)"
        )
        # Suppress all other context sections to keep test focused
        monkeypatch.setattr(mb, "_load_overnight_digest", lambda: "")

        context = mb._load_context()
        assert "PRE-EARNINGS INTELLIGENCE" in context
        assert "NVDA" in context

    def test_pd07_load_context_skips_pre_earnings_when_empty(self, monkeypatch):
        """_load_context does NOT include PRE-EARNINGS INTELLIGENCE when section is empty."""
        import morning_brief as mb

        monkeypatch.setattr(mb, "_build_pre_earnings_intel_section", lambda: "")
        monkeypatch.setattr(mb, "_load_overnight_digest", lambda: "")

        context = mb._load_context()
        assert "PRE-EARNINGS INTELLIGENCE" not in context


# ─────────────────────────────────────────────────────────────────────────────
# PD-08 / PD-09 — system_v1.txt prompt checks
# ─────────────────────────────────────────────────────────────────────────────

class TestSystemPromptEarnings:
    def _load_system(self) -> str:
        p = Path(__file__).parent.parent / "prompts" / "system_v1.txt"
        if not p.exists():
            import pytest
            pytest.skip("system_v1.txt not found")
        return p.read_text()

    def test_pd08_system_references_pre_earnings_intelligence(self):
        """system_v1.txt EARNINGS section references PRE-EARNINGS INTELLIGENCE."""
        text = self._load_system()
        assert "PRE-EARNINGS INTELLIGENCE" in text, (
            "system_v1.txt must mention PRE-EARNINGS INTELLIGENCE section"
        )

    def test_pd09_system_references_earnings_pending_catalyst_type(self):
        """system_v1.txt mentions earnings_pending as the catalyst_type for these setups."""
        text = self._load_system()
        assert "earnings_pending" in text, (
            "system_v1.txt EARNINGS section must reference earnings_pending catalyst_type"
        )
