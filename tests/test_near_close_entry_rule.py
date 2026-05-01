"""
test_near_close_entry_rule.py — Verify system prompt enforces the correct 3:55 PM close rule.

Regression test for the 3:33 PM near-close entry refusal incident (2026-04-30).
Sonnet was self-imposing a ~30-minute near-close restriction based on pretraining
knowledge and the INTRADAY exit rule. The fix adds an explicit 3:55 PM boundary rule.
"""

from pathlib import Path

import pytest

_PROMPT_PATH = Path("prompts/system_v1.txt")
if not _PROMPT_PATH.exists():
    pytest.skip("prompts/system_v1.txt not available in this environment", allow_module_level=True)

SYSTEM_PROMPT = _PROMPT_PATH.read_text()


def test_explicit_close_boundary_present():
    """System prompt must state the correct 3:55 PM ET boundary."""
    assert "3:55 PM ET" in SYSTEM_PROMPT, (
        "system_v1.txt must contain '3:55 PM ET' — the correct near-close boundary"
    )


def test_no_30_minute_entry_restriction():
    """System prompt must NOT contain a 30-minute near-close entry restriction."""
    lower = SYSTEM_PROMPT.lower()
    assert "last 30 minutes of" not in lower, (
        "Found 'last 30 minutes of' — this looks like a close-proximity entry restriction"
    )
    assert "30 minutes before close" not in lower
    assert "30 minutes before market close" not in lower


def test_3_55_rule_in_what_you_never_do():
    """The 3:55 PM rule must appear in the WHAT YOU NEVER DO section."""
    sections = SYSTEM_PROMPT.split("WHAT YOU NEVER DO")
    assert len(sections) == 2, "Expected exactly one WHAT YOU NEVER DO section"
    never_do_section = sections[1].split("\n\n")[0]
    assert "3:55 PM ET" in never_do_section, (
        "The 3:55 PM entry rule must be inside WHAT YOU NEVER DO"
    )


def test_intraday_exit_rule_has_entry_carveout():
    """INTRADAY exit rule must clarify it governs exits, not entries."""
    assert "not entries" in SYSTEM_PROMPT or "not entry" in SYSTEM_PROMPT, (
        "INTRADAY exit rule must explicitly say it governs exits, not entries"
    )


def test_entries_valid_until_3_55():
    """System prompt must say entries are valid until 3:55 PM for all tiers."""
    assert "Entries are valid for all tiers until 3:55 PM ET" in SYSTEM_PROMPT, (
        "Missing explicit statement that all-tier entries are valid until 3:55 PM ET"
    )


def test_core_entries_valid_after_3_55():
    """After 3:55 PM, CORE entries must remain valid until close."""
    assert "CORE entries remain valid until close" in SYSTEM_PROMPT, (
        "Missing statement that CORE entries remain valid after 3:55 PM ET"
    )


def test_27_minutes_not_near_close():
    """System prompt must explicitly state 27 minutes before close is not near close."""
    assert "27 minutes before close is NOT" in SYSTEM_PROMPT, (
        "Missing explicit statement that 27 minutes before close is NOT near close"
    )
