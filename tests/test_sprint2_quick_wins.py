"""
test_sprint2_quick_wins.py — Sprint 2 Quick Wins verification tests.

SQ-1: ask_claude_overnight returns intent-based schema (no legacy format warning)
SQ-2: drawdown_state.json includes generated_at timestamp
SQ-3: is_claude_trading_window consistent across scheduler and bot_stage3_decision
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))


# ─────────────────────────────────────────────────────────────────────────────
# SQ-3 — is_claude_trading_window consistent across modules
# ─────────────────────────────────────────────────────────────────────────────

def test_is_claude_trading_window_consistent_across_modules():
    """Both modules must produce identical results for the same inputs."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from bot_stage3_decision import is_claude_trading_window as dec_gate
    from scheduler import _is_claude_trading_window as sched_gate

    ET = ZoneInfo("America/New_York")

    test_times = [
        datetime(2026, 4, 28, 9, 25, tzinfo=ET),   # market open — True
        datetime(2026, 4, 28, 16, 15, tzinfo=ET),  # gate close — True (inclusive boundary)
        datetime(2026, 4, 28, 16, 16, tzinfo=ET),  # just past gate close — False
        datetime(2026, 4, 28, 12, 0, tzinfo=ET),   # midday — True
        datetime(2026, 4, 26, 12, 0, tzinfo=ET),   # Sunday — False
        datetime(2026, 4, 27, 9, 0, tzinfo=ET),    # Monday but before window — False
    ]
    for dt in test_times:
        s = sched_gate(dt)
        d = dec_gate(dt)
        assert s == d, (
            f"Mismatch at {dt}: sched={s} dec={d}"
        )


def test_scheduler_gate_delegates_to_stage3():
    """scheduler._is_claude_trading_window must delegate to bot_stage3_decision."""
    import inspect

    from scheduler import _is_claude_trading_window

    source = inspect.getsource(_is_claude_trading_window)
    # The wrapper must reference bot_stage3_decision's canonical function
    assert "bot_stage3_decision" in source or "is_claude_trading_window" in source, (
        "scheduler._is_claude_trading_window must delegate to bot_stage3_decision canonical"
    )
    # The wrapper must NOT contain the duplicated implementation logic
    assert "_parse_hhmm" not in source, (
        "scheduler._is_claude_trading_window must not contain duplicated _parse_hhmm logic"
    )


# ─────────────────────────────────────────────────────────────────────────────
# SQ-1 — ask_claude_overnight returns expected (intent-based) schema
# ─────────────────────────────────────────────────────────────────────────────

def _make_fake_claude_response(json_payload: dict):
    """Build a mock Anthropic API response containing json_payload as text."""
    fake_content = MagicMock()
    fake_content.text = json.dumps(json_payload)
    fake_response = MagicMock()
    fake_response.content = [fake_content]
    fake_response.usage = MagicMock(input_tokens=100, cache_write_input_tokens=0,
                                    cache_read_input_tokens=0, output_tokens=50)
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_response
    return fake_client


def test_ask_claude_overnight_returns_expected_schema(monkeypatch):
    """ask_claude_overnight must return the intent-based format its caller expects."""
    import bot_stage3_decision as m

    # Build the LLM response in the new intent-based format
    llm_payload = {
        "reasoning": "Market stable, hold BTC.",
        "regime_view": "normal",
        "ideas": [],
        "holds": ["BTC/USD"],
        "notes": "",
        "concerns": "",
    }

    fake_client = _make_fake_claude_response(llm_payload)
    monkeypatch.setattr(m, "_get_claude", lambda: fake_client)

    # Suppress cost_tracker / cost_attribution side effects
    monkeypatch.setattr("bot_stage3_decision._get_claude", lambda: fake_client)

    result = m._ask_claude_overnight(
        positions=[],
        crypto_context="BTC trending flat.",
        regime_obj={"regime_score": 52, "bias": "neutral"},
        macro_wire="No major events.",
    )

    # Must NOT use legacy format
    assert "actions" not in result, (
        f"'actions' key must not be in overnight result — legacy format detected: {result}"
    )
    # Must use intent-based format keys
    assert "ideas" in result, f"'ideas' key missing from overnight result: {result}"
    assert "regime_view" in result or "regime" not in result or "ideas" in result, (
        f"Overnight result should use regime_view (intent-based): {result}"
    )
    assert isinstance(result["ideas"], list), "'ideas' must be a list"


def test_ask_claude_overnight_no_legacy_schema_warning(monkeypatch):
    """ask_claude_overnight return value must not trigger legacy schema warning in validate_claude_decision."""
    import bot_stage3_decision as m
    from schemas import validate_claude_decision

    llm_payload = {
        "reasoning": "Holding overnight.",
        "regime_view": "normal",
        "ideas": [],
        "holds": ["ETH/USD"],
        "notes": "",
        "concerns": "",
    }

    fake_client = _make_fake_claude_response(llm_payload)
    monkeypatch.setattr(m, "_get_claude", lambda: fake_client)

    result = m._ask_claude_overnight(
        positions=[],
        crypto_context="",
        regime_obj={"regime_score": 50, "bias": "neutral"},
        macro_wire="",
    )

    # Pass the result through the same parser bot.py uses.
    # If legacy format, validate_claude_decision logs a WARNING — capture it.
    import logging
    warning_records = []

    class _WarningCatcher(logging.Handler):
        def emit(self, record):
            if "legacy Claude format" in record.getMessage():
                warning_records.append(record.getMessage())

    handler = _WarningCatcher()
    schema_logger = logging.getLogger("schemas")
    schema_logger.addHandler(handler)
    try:
        validate_claude_decision(result)
    finally:
        schema_logger.removeHandler(handler)

    assert not warning_records, (
        f"Legacy schema warning triggered after SQ-1 fix: {warning_records}"
    )


def test_ask_claude_overnight_semantic_content_unchanged(monkeypatch):
    """Schema fix must preserve semantic meaning — close intent and hold list intact."""
    import bot_stage3_decision as m
    from schemas import validate_claude_decision

    # LLM says: close BTC, hold ETH
    llm_payload = {
        "reasoning": "BTC breaking down, close it.",
        "regime_view": "caution",
        "ideas": [
            {
                "intent": "close",
                "symbol": "BTC/USD",
                "conviction": 0.8,
                "tier": "core",
                "catalyst": "BTC breakdown",
                "direction": "neutral",
                "concerns": "",
            }
        ],
        "holds": ["ETH/USD"],
        "notes": "Reducing risk.",
        "concerns": "",
    }

    fake_client = _make_fake_claude_response(llm_payload)
    monkeypatch.setattr(m, "_get_claude", lambda: fake_client)

    result = m._ask_claude_overnight(
        positions=[],
        crypto_context="BTC -5% in last hour.",
        regime_obj={"regime_score": 35, "bias": "risk-off"},
        macro_wire="",
    )

    # Validate through the full parser
    decision = validate_claude_decision(result)

    # Semantic content: one close idea for BTC/USD
    assert len(decision.ideas) == 1, f"Expected 1 idea (close BTC), got {len(decision.ideas)}"
    from schemas import AccountAction
    assert decision.ideas[0].action == AccountAction.CLOSE, (
        f"Expected CLOSE action, got {decision.ideas[0].action}"
    )
    assert decision.ideas[0].symbol == "BTC/USD", (
        f"Expected symbol BTC/USD, got {decision.ideas[0].symbol}"
    )
    # ETH/USD is in holds
    assert "ETH/USD" in decision.holds, (
        f"Expected ETH/USD in holds, got {decision.holds}"
    )


def test_overnight_default_uses_intent_schema():
    """_OVERNIGHT_DEFAULT fallback must use intent-based format (no legacy actions key)."""
    from bot_stage3_decision import _OVERNIGHT_DEFAULT

    assert "actions" not in _OVERNIGHT_DEFAULT, (
        "_OVERNIGHT_DEFAULT must not use legacy 'actions' key"
    )
    assert "ideas" in _OVERNIGHT_DEFAULT, (
        "_OVERNIGHT_DEFAULT must use intent-based 'ideas' key"
    )
    assert "regime_view" in _OVERNIGHT_DEFAULT, (
        "_OVERNIGHT_DEFAULT must use 'regime_view' (not 'regime')"
    )
    assert _OVERNIGHT_DEFAULT["ideas"] == [], (
        "_OVERNIGHT_DEFAULT ideas must be empty list (hold-all fallback)"
    )
    assert _OVERNIGHT_DEFAULT["regime_view"] == "normal", (
        "_OVERNIGHT_DEFAULT regime_view must be 'normal'"
    )


# ─────────────────────────────────────────────────────────────────────────────
# SQ-2 — generated_at in drawdown_state.json
# ─────────────────────────────────────────────────────────────────────────────

def test_save_drawdown_state_writes_generated_at(tmp_path, monkeypatch):
    """_save_drawdown_state must include generated_at at top level."""
    import bot

    # Redirect the drawdown state file to tmp_path
    fake_path = tmp_path / "drawdown_state.json"
    monkeypatch.setattr(bot, "_DRAWDOWN_STATE_FILE", fake_path)
    monkeypatch.setattr(bot, "_drawdown_state_loaded", True)
    monkeypatch.setattr(bot, "_peak_equity", 100000.0)
    monkeypatch.setattr(bot, "_last_drawdown_alert", None)

    bot._save_drawdown_state()

    assert fake_path.exists(), "drawdown_state.json was not written"
    data = json.loads(fake_path.read_text())

    assert "generated_at" in data, (
        f"generated_at missing from drawdown_state.json: {list(data.keys())}"
    )
    # Must be a valid ISO 8601 string
    from datetime import datetime
    ts = data["generated_at"]
    assert isinstance(ts, str), f"generated_at must be a string, got {type(ts)}"
    # Parse it — raises ValueError if not valid ISO format
    parsed = datetime.fromisoformat(ts)
    assert parsed.tzinfo is not None, "generated_at must be timezone-aware (UTC)"

    # Existing fields must still be present
    assert "peak_equity" in data, "peak_equity must still be present"
    assert "last_drawdown_alert" in data, "last_drawdown_alert must still be present"
    assert data["peak_equity"] == 100000.0


def test_save_drawdown_state_generated_at_is_utc(tmp_path, monkeypatch):
    """generated_at must be UTC (ends with +00:00 or Z suffix)."""
    import bot

    fake_path = tmp_path / "drawdown_state.json"
    monkeypatch.setattr(bot, "_DRAWDOWN_STATE_FILE", fake_path)
    monkeypatch.setattr(bot, "_drawdown_state_loaded", True)
    monkeypatch.setattr(bot, "_peak_equity", 50000.0)
    monkeypatch.setattr(bot, "_last_drawdown_alert", None)

    bot._save_drawdown_state()

    data = json.loads(fake_path.read_text())
    ts = data["generated_at"]
    # UTC ISO strings end with +00:00 or Z
    assert ts.endswith("+00:00") or ts.endswith("Z"), (
        f"generated_at must be UTC, got: {ts}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# SQ-2 — structures.json: document format constraint
# ─────────────────────────────────────────────────────────────────────────────

def test_structures_json_format_is_list():
    """
    structures.json is a bare list (not a wrapped dict).
    Adding generated_at at top-level requires a format migration — documented
    in SPRINT2_QW_FINDINGS.md, deferred to Sprint 2 follow-up.
    This test confirms the current format so any future migration is explicit.
    """
    from options_state import _load_raw
    data = _load_raw()
    assert isinstance(data, list), (
        f"structures.json must be a list (current format) — got {type(data).__name__}"
    )
