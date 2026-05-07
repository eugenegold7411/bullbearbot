"""
tests/test_visibility_intelligence.py — Issues 24, 25, 26 visibility improvements.

24 — avoid_log.jsonl: structured record of conviction=avoid per cycle
25 — SHORT SELLING RULE appended to avoid_line when 2+ bearish signals
26 — A2 debate receives high-IV (>60) avoided symbols as credit spread candidates

Tests:
  Avoid log (Fix 24):
  1. test_avoid_log_written_on_avoid_conviction
  2. test_avoid_log_record_has_required_fields
  3. test_avoid_log_appends_across_cycles
  4. test_avoid_log_rotation_on_size

  Short instruction (Fix 25):
  5. test_short_instruction_in_avoid_line_when_bearish
  6. test_short_instruction_absent_when_insufficient_bearish
  7. test_short_instruction_absent_when_no_avoided_symbols

  A2 avoid context (Fix 26):
  8. test_a2_receives_high_iv_avoided_symbols
  9. test_a2_filters_low_iv_avoided
  10. test_a2_avoid_context_log_line
"""

from __future__ import annotations

import json
from unittest.mock import patch

# ── helpers ──────────────────────────────────────────────────────────────────

def _scored(conviction: str, signals: list[str], direction: str = "bearish") -> dict:
    return {
        "conviction": conviction,
        "signals": signals,
        "conflicts": [],
        "direction": direction,
        "score": 40.0,
        "primary_catalyst": "test",
    }


def _make_sonnet_brief(avoid_line: str) -> dict:
    return {
        "generated_at": "2026-05-07T12:00:00",
        "avoid_line": avoid_line,
        "regime_line": "risk_on(70)",
        "conviction_state": "",
        "positions_line": "",
    }


# ── Fix 24: Avoid log tests ──────────────────────────────────────────────────

def test_avoid_log_written_on_avoid_conviction(tmp_path):
    """conviction=avoid symbols produce records in avoid_log.jsonl."""
    from bot_stage2_signal import _write_avoid_log

    log_path = tmp_path / "avoid_log.jsonl"
    scored = {
        "XLE": _scored("avoid", ["negative_momentum", "below_200ma"]),
        "CRWV": _scored("avoid", ["bearish_catalyst"]),
        "NVDA": _scored("high", ["ai_capex", "institutional_buy"]),
    }

    with patch("bot_stage2_signal._AVOID_LOG_PATH", log_path):
        _write_avoid_log(scored, "cycle_test_001")

    assert log_path.exists(), "avoid_log.jsonl was not created"
    records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
    avoid_syms = [r["symbol"] for r in records]
    assert "XLE" in avoid_syms
    assert "CRWV" in avoid_syms
    assert "NVDA" not in avoid_syms, "high-conviction symbol should not appear in avoid log"
    assert len(records) == 2


def test_avoid_log_record_has_required_fields(tmp_path):
    """Each avoid_log record contains all required fields."""
    from bot_stage2_signal import _write_avoid_log

    log_path = tmp_path / "avoid_log.jsonl"
    scored = {
        "AFRM": _scored("avoid", ["insider_selling", "analyst_downgrade", "overvalued"]),
    }

    with patch("bot_stage2_signal._AVOID_LOG_PATH", log_path):
        _write_avoid_log(scored, "cycle_test_002")

    records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
    assert len(records) == 1
    rec = records[0]
    for field in ("ts", "symbol", "conviction", "reason", "signals", "conflicts", "direction", "score", "cycle_id"):
        assert field in rec, f"Required field '{field}' missing from avoid_log record"
    assert rec["symbol"] == "AFRM"
    assert rec["conviction"] == "avoid"
    assert rec["cycle_id"] == "cycle_test_002"
    assert "insider_selling" in rec["signals"]


def test_avoid_log_appends_across_cycles(tmp_path):
    """Records from multiple cycles accumulate in the same file."""
    from bot_stage2_signal import _write_avoid_log

    log_path = tmp_path / "avoid_log.jsonl"
    scored1 = {"XLE": _scored("avoid", ["negative_momentum"])}
    scored2 = {"PLTR": _scored("avoid", ["analyst_downgrade"])}

    with patch("bot_stage2_signal._AVOID_LOG_PATH", log_path):
        _write_avoid_log(scored1, "cycle_001")
        _write_avoid_log(scored2, "cycle_002")

    records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
    assert len(records) == 2
    syms = {r["symbol"] for r in records}
    assert syms == {"XLE", "PLTR"}
    cycle_ids = {r["cycle_id"] for r in records}
    assert cycle_ids == {"cycle_001", "cycle_002"}


def test_avoid_log_rotation_on_size(tmp_path):
    """File exceeding _AVOID_LOG_MAX_BYTES is rotated before new write."""
    from bot_stage2_signal import _write_avoid_log

    log_path = tmp_path / "avoid_log.jsonl"
    # Write a file that exceeds 10MB threshold
    log_path.write_text("x" * (10 * 1024 * 1024 + 1))
    assert log_path.stat().st_size > 10 * 1024 * 1024

    scored = {"ARM": _scored("avoid", ["bearish_catalyst"])}

    with patch("bot_stage2_signal._AVOID_LOG_PATH", log_path), \
         patch("bot_stage2_signal._AVOID_LOG_MAX_BYTES", 10 * 1024 * 1024):
        _write_avoid_log(scored, "cycle_after_rotation")

    # The original large file should have been renamed
    rotated = list(tmp_path.glob("avoid_log_*.jsonl"))
    assert len(rotated) == 1, "Expected one rotated file"
    # The main log_path should contain only the new record
    records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
    assert len(records) == 1
    assert records[0]["symbol"] == "ARM"


# ── Fix 25: Short instruction tests ──────────────────────────────────────────

def test_short_instruction_in_avoid_line_when_bearish(tmp_path):
    """avoid_line gets SHORT SELLING RULE when an avoid symbol has 2+ bearish signals."""
    from bot_stage2_signal import _maybe_append_short_rule

    brief_path = tmp_path / "morning_brief_sonnet.json"
    brief_path.write_text(json.dumps(_make_sonnet_brief("AVOID: XLE PLTR")))

    scored = {
        "XLE": _scored("avoid", ["negative_momentum", "below_200ma", "energy_sector_weakness"]),
        "NVDA": _scored("high", ["ai_capex"]),
    }

    with patch("bot_stage2_signal._SONNET_BRIEF_PATH", brief_path):
        _maybe_append_short_rule(scored)

    updated = json.loads(brief_path.read_text())["avoid_line"]
    assert "SHORT SELLING RULE:" in updated, f"Short rule absent. Got: {updated!r}"
    assert "XLE" in updated
    # Verify the original avoid line is preserved before the rule
    assert updated.startswith("AVOID: XLE PLTR")


def test_short_instruction_absent_when_insufficient_bearish(tmp_path):
    """With only 1 bearish signal, SHORT SELLING RULE is NOT added."""
    from bot_stage2_signal import _maybe_append_short_rule

    brief_path = tmp_path / "morning_brief_sonnet.json"
    brief_path.write_text(json.dumps(_make_sonnet_brief("AVOID: RTX")))

    scored = {
        "RTX": _scored("avoid", ["negative_momentum"]),  # only 1 bearish signal
    }

    with patch("bot_stage2_signal._SONNET_BRIEF_PATH", brief_path):
        _maybe_append_short_rule(scored)

    updated = json.loads(brief_path.read_text())["avoid_line"]
    assert "SHORT SELLING RULE:" not in updated, (
        f"Short rule should be absent with 1 bearish signal. Got: {updated!r}"
    )


def test_short_instruction_absent_when_no_avoided_symbols(tmp_path):
    """No avoid symbols means no SHORT SELLING RULE appended."""
    from bot_stage2_signal import _maybe_append_short_rule

    brief_path = tmp_path / "morning_brief_sonnet.json"
    original = "AVOID: none"
    brief_path.write_text(json.dumps(_make_sonnet_brief(original)))

    scored = {
        "NVDA": _scored("high", ["ai_capex", "institutional_buy"], direction="bullish"),
        "MSFT": _scored("medium", ["cloud_growth"], direction="bullish"),
    }

    with patch("bot_stage2_signal._SONNET_BRIEF_PATH", brief_path):
        _maybe_append_short_rule(scored)

    updated = json.loads(brief_path.read_text())["avoid_line"]
    assert "SHORT SELLING RULE:" not in updated
    assert updated == original


# ── Fix 26: A2 avoid context tests ───────────────────────────────────────────

def test_a2_receives_high_iv_avoided_symbols(tmp_path):
    """Avoided symbols with iv_rank > 60 appear in account1_summary."""
    from bot_options_stage1_candidates import _augment_with_avoid_context

    brief_path = tmp_path / "morning_brief_sonnet.json"
    brief_path.write_text(json.dumps(_make_sonnet_brief("AVOID: AFRM BABA SE")))

    signal_scores = {
        "AFRM": _scored("avoid", ["insider_selling", "earnings_miss"], "bearish"),
        "BABA": _scored("avoid", ["analyst_downgrade"], "bearish"),
        "SE":   _scored("avoid", ["bearish_catalyst", "negative_momentum"], "bearish"),
    }
    iv_summaries = {
        "AFRM": {"iv_rank": 95.0, "iv_environment": "very_expensive"},
        "BABA": {"iv_rank": 80.0, "iv_environment": "expensive"},
        "SE":   {"iv_rank": 100.0, "iv_environment": "very_expensive"},
    }

    with patch("bot_options_stage1_candidates._MORNING_BRIEF_SONNET_PATH", brief_path):
        result = _augment_with_avoid_context("base_summary", signal_scores, iv_summaries)

    assert "CREDIT SPREAD CANDIDATES" in result, f"Avoid section missing. Got: {result!r}"
    assert "AFRM" in result
    assert "BABA" in result
    assert "SE" in result


def test_a2_filters_low_iv_avoided(tmp_path):
    """Avoided symbols with iv_rank ≤ 60 are NOT passed to A2 debate."""
    from bot_options_stage1_candidates import _augment_with_avoid_context

    brief_path = tmp_path / "morning_brief_sonnet.json"
    brief_path.write_text(json.dumps(_make_sonnet_brief("AVOID: COIN")))

    signal_scores = {
        "COIN": _scored("avoid", ["bearish_catalyst", "negative_momentum"], "bearish"),
    }
    iv_summaries = {
        "COIN": {"iv_rank": 40.0, "iv_environment": "cheap"},
    }

    with patch("bot_options_stage1_candidates._MORNING_BRIEF_SONNET_PATH", brief_path):
        result = _augment_with_avoid_context("base_summary", signal_scores, iv_summaries)

    # Low IV symbol — section should not appear
    assert "CREDIT SPREAD CANDIDATES" not in result, (
        f"Low-IV avoided symbol should not appear. Got: {result!r}"
    )
    assert result == "base_summary"


def test_a2_avoid_context_log_line(tmp_path, caplog):
    """[A2_SIGNAL] log line fires with count when avoided symbols pass the iv_rank gate."""
    import logging

    from bot_options_stage1_candidates import _augment_with_avoid_context

    brief_path = tmp_path / "morning_brief_sonnet.json"
    brief_path.write_text(json.dumps(_make_sonnet_brief("AVOID: AFRM NET")))

    signal_scores = {
        "AFRM": _scored("avoid", ["insider_selling", "earnings_miss"], "bearish"),
        "NET":  _scored("avoid", ["analyst_downgrade", "negative_momentum"], "bearish"),
    }
    iv_summaries = {
        "AFRM": {"iv_rank": 95.0},
        "NET":  {"iv_rank": 92.0},
    }

    with patch("bot_options_stage1_candidates._MORNING_BRIEF_SONNET_PATH", brief_path), \
         caplog.at_level(logging.INFO, logger="bot_options_stage1_candidates"):
        _augment_with_avoid_context("base_summary", signal_scores, iv_summaries)

    log_messages = " ".join(caplog.messages)
    assert "[A2_SIGNAL]" in log_messages, f"Expected [A2_SIGNAL] log line. Got: {log_messages!r}"
    assert "credit spread candidate" in log_messages.lower(), (
        f"Expected 'credit spread candidate' in log. Got: {log_messages!r}"
    )
