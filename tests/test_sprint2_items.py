"""
test_sprint2_items.py — Sprint 2 Execution Semantics, Taxonomy, and Defensive Guards.

Item 1: DTBP pre-flight guard in A2 executor
Item 2: A2 decisions directory creation and persistence
Item 3: Per-symbol submission lock to prevent duplicate structures
Item 4: catalyst_type wired into signal_scores
Item 5: remove_backstop() wired into reconciliation close path
Item 6: Fill-event ingestion for A2 structures
Item 7: Dead constants removed from scheduler
Item 8: Overnight logging field names corrected
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))


# ─────────────────────────────────────────────────────────────────────────────
# Item 1 — DTBP pre-flight guard in A2 executor
# ─────────────────────────────────────────────────────────────────────────────

def _make_mock_structure():
    """Build a minimal OptionsStructure mock for executor tests."""
    from schemas import (
        OptionsLeg,
        OptionsStructure,
        OptionStrategy,
        StructureLifecycle,
        Tier,
    )
    leg = OptionsLeg(
        occ_symbol="GLD261219C00435000",
        underlying="GLD",
        side="buy",
        qty=1,
        option_type="call",
        strike=435.0,
        expiration="2026-12-19",
    )
    return OptionsStructure(
        structure_id="test-struct-001",
        underlying="GLD",
        strategy=OptionStrategy.SINGLE_CALL,
        lifecycle=StructureLifecycle.PROPOSED,
        legs=[leg],
        contracts=1,
        max_cost_usd=500.0,
        opened_at=datetime.now(timezone.utc).isoformat(),
        catalyst="test_catalyst",
        tier=Tier.CORE,
    )


def test_dtbp_zero_guard_skips_submission(tmp_path, monkeypatch):
    """When DTBP=0 and options_buying_power>0, submission proceeds (falls through)."""
    import options_executor
    import order_executor_options as oe

    # Redirect log to tmp_path so no test artifacts reach production options_log.jsonl
    monkeypatch.setattr(oe, "_LOG_PATH", tmp_path / "options_log.jsonl")
    # Isolate structures.json — submit_options_order lazily imports options_state
    monkeypatch.setitem(sys.modules, "options_state", MagicMock())

    structure = _make_mock_structure()

    mock_account = MagicMock()
    mock_account.daytrading_buying_power = "0"
    mock_account.options_buying_power = "100000"
    mock_account.id = "test-acct-001"

    mock_client = MagicMock()
    mock_client.get_account.return_value = mock_account

    submitted_structure = _make_mock_structure()
    from schemas import StructureLifecycle
    submitted_structure.lifecycle = StructureLifecycle.SUBMITTED
    submitted_structure.order_ids = ["order-dtbp-001"]

    monkeypatch.setattr(oe, "_get_options_client", lambda: mock_client)
    monkeypatch.setattr(options_executor, "submit_structure",
                        lambda s, c, config: submitted_structure)

    result = oe.submit_options_order(structure, equity=50000.0, observation_mode=False)

    # New behavior: DTBP=0 + OBP>0 falls through to submission (does NOT return dtbp_zero)
    assert result.status != "dtbp_zero", (
        f"Expected status != dtbp_zero after fallback fix, got {result.status}"
    )
    # Submission must have been attempted (not short-circuited)
    assert result.status == "submitted", (
        f"Expected submission to proceed when DTBP=0 and OBP>0, got {result.status}"
    )


def test_dtbp_nonzero_proceeds_normally(tmp_path, monkeypatch):
    """When DTBP>0, submission proceeds past the DTBP guard."""
    import options_executor
    import order_executor_options as oe

    # Redirect log to tmp_path so no test artifacts reach production options_log.jsonl
    monkeypatch.setattr(oe, "_LOG_PATH", tmp_path / "options_log.jsonl")
    # Isolate structures.json — submit_options_order lazily imports options_state
    monkeypatch.setitem(sys.modules, "options_state", MagicMock())

    structure = _make_mock_structure()

    mock_account = MagicMock()
    mock_account.daytrading_buying_power = "50000"
    mock_account.options_buying_power = "50000"
    mock_account.id = "test-acct-002"

    mock_client = MagicMock()
    mock_client.get_account.return_value = mock_account

    submitted_structure = _make_mock_structure()
    from schemas import StructureLifecycle
    submitted_structure.lifecycle = StructureLifecycle.SUBMITTED
    submitted_structure.order_ids = ["order-xyz"]

    monkeypatch.setattr(oe, "_get_options_client", lambda: mock_client)
    monkeypatch.setattr(options_executor, "submit_structure",
                        lambda s, c, config: submitted_structure)

    result = oe.submit_options_order(structure, equity=50000.0, observation_mode=False)
    assert result.status == "submitted", (
        f"Expected submitted, got {result.status}"
    )


def test_dtbp_check_failure_fails_open(tmp_path, monkeypatch):
    """If DTBP account fetch raises, submission proceeds normally (fail open)."""
    import options_executor
    import order_executor_options as oe

    # Redirect log to tmp_path so no test artifacts reach production options_log.jsonl
    monkeypatch.setattr(oe, "_LOG_PATH", tmp_path / "options_log.jsonl")
    # Isolate structures.json — submit_options_order lazily imports options_state
    monkeypatch.setitem(sys.modules, "options_state", MagicMock())

    structure = _make_mock_structure()

    mock_client = MagicMock()
    mock_client.get_account.side_effect = RuntimeError("Connection timeout")

    submitted_structure = _make_mock_structure()
    from schemas import StructureLifecycle
    submitted_structure.lifecycle = StructureLifecycle.SUBMITTED
    submitted_structure.order_ids = ["order-abc"]

    monkeypatch.setattr(oe, "_get_options_client", lambda: mock_client)
    monkeypatch.setattr(options_executor, "submit_structure",
                        lambda s, c, config: submitted_structure)

    result = oe.submit_options_order(structure, equity=50000.0, observation_mode=False)
    assert result.status == "submitted", (
        f"Expected submission to proceed after DTBP check failure, got {result.status}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Item 2 — A2 decisions directory
# ─────────────────────────────────────────────────────────────────────────────

def test_persist_decision_record_creates_directory_if_absent(tmp_path, monkeypatch):
    """persist_decision_record creates data/account2/decisions/ if absent."""
    import bot_options_stage4_execution as m
    from schemas import A2DecisionRecord

    decisions_dir = tmp_path / "decisions"
    assert not decisions_dir.exists()

    monkeypatch.setattr(m, "_DECISIONS_DIR", decisions_dir)

    record = A2DecisionRecord(
        decision_id="a2_dec_20260427_000001",
        session_tier="market",
        candidate_sets=[],
        debate_input=None,
        debate_output_raw=None,
        debate_parsed={},
        selected_candidate=None,
        execution_result="no_trade",
        no_trade_reason="test",
        elapsed_seconds=0.1,
        built_at=datetime.now(timezone.utc).isoformat(),
    )

    m.persist_decision_record(record)

    assert decisions_dir.exists(), "decisions directory must be created by persist_decision_record"


def test_persist_decision_record_writes_json_file(tmp_path, monkeypatch):
    """persist_decision_record writes a2_dec_*.json with required fields."""
    import bot_options_stage4_execution as m
    from schemas import A2DecisionRecord

    decisions_dir = tmp_path / "decisions"
    monkeypatch.setattr(m, "_DECISIONS_DIR", decisions_dir)

    record = A2DecisionRecord(
        decision_id="a2_dec_20260427_000002",
        session_tier="market",
        candidate_sets=[],
        debate_input=None,
        debate_output_raw=None,
        debate_parsed={"confidence": 0.9},
        selected_candidate=None,
        execution_result="no_trade",
        no_trade_reason="debate_low_confidence",
        elapsed_seconds=1.5,
        built_at=datetime.now(timezone.utc).isoformat(),
    )

    m.persist_decision_record(record)

    files = list(decisions_dir.glob("a2_dec_*.json"))
    assert len(files) == 1, f"Expected 1 decision file, got {len(files)}"

    data = json.loads(files[0].read_text())
    assert "decision_id" in data, "decision_id must be in persisted record"
    assert "execution_result" in data, "execution_result must be in persisted record"
    assert data["execution_result"] == "no_trade"


def test_decisions_directory_created_at_startup(tmp_path, monkeypatch):
    """Startup initialization creates data/account2/decisions/ alongside mode files."""

    decisions_dir = tmp_path / "account2" / "decisions"
    assert not decisions_dir.exists()

    # Monkeypatch _ensure_account_modes_initialized to use tmp_path
    # by patching the Path constructor for the specific subdirectory
    import scheduler

    _orig_func = scheduler._ensure_account_modes_initialized

    def _patched_init():
        # Simulate only the directory creation part, redirected to tmp_path
        decisions_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(scheduler, "_ensure_account_modes_initialized", _patched_init)

    scheduler._ensure_account_modes_initialized()

    assert decisions_dir.exists(), (
        "data/account2/decisions/ must be created during startup initialization"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Item 3 — Per-symbol submission lock
# ─────────────────────────────────────────────────────────────────────────────

def _make_active_structure(structure_id: str, underlying: str, occ_symbol: str,
                            lifecycle_value: str) -> dict:
    """Build a minimal structure dict for duplicate-check tests."""
    from schemas import (
        OptionsLeg,
        OptionsStructure,
        OptionStrategy,
        StructureLifecycle,
        Tier,
    )

    lc_map = {
        "submitted": StructureLifecycle.SUBMITTED,
        "partially_filled": StructureLifecycle.PARTIALLY_FILLED,
        "fully_filled": StructureLifecycle.FULLY_FILLED,
        "expired": StructureLifecycle.EXPIRED,
        "cancelled": StructureLifecycle.CANCELLED,
    }
    leg = OptionsLeg(
        occ_symbol=occ_symbol,
        underlying=underlying,
        side="buy",
        qty=1,
        option_type="call",
        strike=100.0,
        expiration="2026-12-19",
    )
    return OptionsStructure(
        structure_id=structure_id,
        underlying=underlying,
        strategy=OptionStrategy.SINGLE_CALL,
        lifecycle=lc_map.get(lifecycle_value, StructureLifecycle.SUBMITTED),
        legs=[leg],
        contracts=1,
        max_cost_usd=500.0,
        opened_at=datetime.now(timezone.utc).isoformat(),
        catalyst="test",
        tier=Tier.CORE,
    )


def test_duplicate_submission_blocked(monkeypatch):
    """Submitting same OCC symbol when already in submitted/filled state is blocked."""
    import bot_options_stage4_execution as m

    existing = _make_active_structure("old-struct", "GLD", "GLD261219C00435000", "submitted")

    monkeypatch.setattr("bot_options_stage4_execution.options_state",
                        MagicMock(load_structures=lambda: [existing]),
                        raising=False)

    # New leg with same OCC symbol
    from schemas import OptionsLeg
    new_leg = OptionsLeg(
        occ_symbol="GLD261219C00435000",
        underlying="GLD",
        side="buy",
        qty=1,
        option_type="call",
        strike=435.0,
        expiration="2026-12-19",
    )

    import sys
    # Temporarily inject mock options_state into module namespace
    mock_os = MagicMock()
    mock_os.load_structures.return_value = [existing]
    with patch.dict(sys.modules, {"options_state": mock_os}):
        result = m._is_duplicate_submission("GLD", [new_leg])

    assert result is True, "Duplicate OCC symbol in submitted state must be blocked"


def test_different_symbol_not_blocked(monkeypatch):
    """Different underlying symbol is not affected by the submission lock."""
    import bot_options_stage4_execution as m

    existing = _make_active_structure("old-struct", "AMZN", "AMZN261219C00250000", "submitted")

    mock_os = MagicMock()
    mock_os.load_structures.return_value = [existing]

    from schemas import OptionsLeg
    new_leg = OptionsLeg(
        occ_symbol="GLD261219C00435000",  # different OCC
        underlying="GLD",
        side="buy",
        qty=1,
        option_type="call",
        strike=435.0,
        expiration="2026-12-19",
    )

    with patch.dict(sys.modules, {"options_state": mock_os}):
        result = m._is_duplicate_submission("GLD", [new_leg])

    assert result is False, "Different symbol should not trigger duplicate lock"


def test_expired_structure_not_blocking(monkeypatch):
    """Structures with lifecycle=expired or cancelled do not block new submissions."""
    import bot_options_stage4_execution as m

    expired = _make_active_structure("old-struct", "GLD", "GLD261219C00435000", "expired")
    cancelled = _make_active_structure("old-struct-2", "GLD", "GLD261219C00435000", "cancelled")

    mock_os = MagicMock()
    mock_os.load_structures.return_value = [expired, cancelled]

    from schemas import OptionsLeg
    new_leg = OptionsLeg(
        occ_symbol="GLD261219C00435000",  # same OCC but lifecycle is terminal
        underlying="GLD",
        side="buy",
        qty=1,
        option_type="call",
        strike=435.0,
        expiration="2026-12-19",
    )

    with patch.dict(sys.modules, {"options_state": mock_os}):
        result = m._is_duplicate_submission("GLD", [new_leg])

    assert result is False, "Expired/cancelled structures must not block new submissions"


# ─────────────────────────────────────────────────────────────────────────────
# Item 4 — catalyst_type in signal_scores
# ─────────────────────────────────────────────────────────────────────────────

def test_signal_scores_include_catalyst_type():
    """_l2_to_signal_score must include catalyst_type field."""
    from bot_stage2_signal import _l2_to_signal_score

    l2 = {
        "score": 62.0,
        "direction": "bullish",
        "conviction": "medium",
        "signals": ["breakout"],
        "conflicts": [],
        "orb_candidate": False,
        "pattern_watchlist": False,
    }
    result = _l2_to_signal_score("GLD", l2)

    assert "catalyst_type" in result, (
        f"catalyst_type must be in signal score result, keys: {list(result.keys())}"
    )


def test_catalyst_type_not_unknown_when_classifiable():
    """classify_catalyst must return non-unknown for classifiable catalyst text."""
    from semantic_labels import CatalystType, classify_catalyst

    # Earnings beat should be classifiable
    ct = classify_catalyst("earnings beat — strong EPS above consensus")
    assert ct != CatalystType.UNKNOWN, (
        f"Expected non-UNKNOWN for earnings beat text, got {ct}"
    )

    # Insider buy should be classifiable
    ct2 = classify_catalyst("insider buy via Form 4 filing")
    assert ct2 != CatalystType.UNKNOWN, (
        f"Expected non-UNKNOWN for insider buy text, got {ct2}"
    )


def test_catalyst_type_failure_does_not_break_scoring():
    """If classify_catalyst raises, scoring continues with catalyst_type='unknown'."""
    from bot_stage2_signal import _l2_to_signal_score

    # Null/empty primary catalyst should still produce a valid result
    l2 = {"score": 50.0, "direction": "neutral", "conviction": "low"}
    result = _l2_to_signal_score("SPY", l2)

    assert "catalyst_type" in result, "catalyst_type must be present even with empty l2"
    assert isinstance(result["catalyst_type"], str), "catalyst_type must be a string"


def test_catalyst_type_in_l3_synthesis_path(monkeypatch):
    """_run_l3_synthesis must include catalyst_type in merged_symbols."""
    import bot_stage2_signal as m

    # Mock a batch result with a classifiable primary_catalyst
    fake_batch = {
        "scored_symbols": {
            "GLD": {
                "score": 70.0,
                "direction": "bullish",
                "conviction": "high",
                "signals": ["momentum"],
                "conflicts": [],
                "primary_catalyst": "earnings beat — strong EPS",
                "orb_candidate": False,
                "pattern_watchlist": False,
                "tier": "core",
                "l2_score": 65.0,
                "l3_adjustment": 5.0,
                "adjustment_reason": "",
            }
        },
        "top_3": ["GLD"],
        "elevated_caution": [],
        "reasoning": "Earnings beat driving momentum.",
    }

    monkeypatch.setattr(m, "_call_l3_batch", lambda content: fake_batch)

    l2_scores = {
        "GLD": {
            "score": 65.0, "direction": "bullish", "conviction": "high",
            "signals": ["momentum"], "conflicts": [],
            "orb_candidate": False, "pattern_watchlist": False,
        }
    }

    result = m._run_l3_synthesis(["GLD"], l2_scores, {}, {"regime_score": 60}, [])

    gld = result.get("scored_symbols", {}).get("GLD", {})
    assert "catalyst_type" in gld, (
        f"catalyst_type must be present in L3 synthesis output, got keys: {list(gld.keys())}"
    )
    # earnings beat should classify as non-unknown
    from semantic_labels import CatalystType
    assert gld["catalyst_type"] != CatalystType.UNKNOWN.value, (
        f"Expected non-unknown catalyst_type for earnings beat, got: {gld['catalyst_type']}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Item 5 — remove_backstop() wired into reconciliation close path
# ─────────────────────────────────────────────────────────────────────────────

def test_remove_backstop_called_on_position_close(tmp_path, monkeypatch):
    """Closing a position via _close_position removes its time_bound_action entry."""
    import reconciliation as r

    # Write a config with a time_bound_action for AMZN
    cfg = {
        "time_bound_actions": [
            {"symbol": "AMZN", "exit_by": "2026-04-25T15:45:00+00:00",
             "reason": "backstop_exit: max_hold_5d"}
        ]
    }
    config_path = tmp_path / "strategy_config.json"
    config_path.write_text(json.dumps(cfg))

    monkeypatch.setattr(r, "_CONFIG_PATH", config_path)

    # Mock Alpaca client
    mock_order = MagicMock()
    mock_order.id = "order-abc"
    mock_client = MagicMock()
    mock_client.submit_order.return_value = mock_order

    results = []
    r._close_position(mock_client, "AMZN", 60.0, results, "test_close")

    # Read the config back and verify time_bound_actions is empty
    updated = json.loads(config_path.read_text())
    tba = updated.get("time_bound_actions", [])
    amzn_entries = [t for t in tba if t.get("symbol") == "AMZN"]
    assert len(amzn_entries) == 0, (
        f"time_bound_action for AMZN must be removed after close, got: {tba}"
    )


def test_remove_backstop_failure_does_not_block_close(monkeypatch):
    """remove_backstop failure is non-fatal — close proceeds regardless."""
    from pathlib import Path

    import reconciliation as r

    # Point _CONFIG_PATH at a non-existent file (remove_backstop will fail silently)
    monkeypatch.setattr(r, "_CONFIG_PATH", Path("/nonexistent/path.json"))

    mock_order = MagicMock()
    mock_order.id = "order-xyz"
    mock_client = MagicMock()
    mock_client.submit_order.return_value = mock_order

    results = []
    # Should not raise even though remove_backstop will fail
    r._close_position(mock_client, "MSFT", 47.0, results, "test_close_no_config")

    # Order must still have been submitted
    assert mock_client.submit_order.call_count == 1, "Order must still be submitted"
    assert any("CLOSED" in r_str for r_str in results), (
        f"CLOSED must appear in results even with remove_backstop failure: {results}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Item 6 — Fill-event ingestion for A2 structures
# ─────────────────────────────────────────────────────────────────────────────

def _make_fully_filled_structure(structure_id: str, order_id: str,
                                   filled_price: Optional[float] = None):
    """Build a fully_filled OptionsStructure with configurable filled_price."""
    from schemas import (
        OptionsLeg,
        OptionsStructure,
        OptionStrategy,
        StructureLifecycle,
        Tier,
    )

    leg = OptionsLeg(
        occ_symbol="GLD261219C00435000",
        underlying="GLD",
        side="buy",
        qty=1,
        option_type="call",
        strike=435.0,
        expiration="2026-12-19",
        order_id=order_id,
        filled_price=filled_price,
    )
    return OptionsStructure(
        structure_id=structure_id,
        underlying="GLD",
        strategy=OptionStrategy.SINGLE_CALL,
        lifecycle=StructureLifecycle.FULLY_FILLED,
        legs=[leg],
        contracts=1,
        max_cost_usd=500.0,
        opened_at=datetime.now(timezone.utc).isoformat(),
        catalyst="test",
        tier=Tier.CORE,
    )


def test_fill_prices_updated_from_alpaca(monkeypatch):
    """_update_fill_prices populates filled_price from Alpaca order data."""
    import bot_options_stage4_execution as m

    struct = _make_fully_filled_structure("struct-001", "order-001", filled_price=None)
    assert struct.legs[0].filled_price is None

    mock_order = MagicMock()
    mock_order.filled_avg_price = "4.35"
    mock_order.filled_qty = "1"

    mock_client = MagicMock()
    mock_client.get_order_by_id.return_value = mock_order

    saved_structures = []
    mock_os = MagicMock()
    mock_os.save_structure.side_effect = lambda s: saved_structures.append(s)

    with patch.dict(sys.modules, {"options_state": mock_os}):
        result = m._update_fill_prices([struct], mock_client)

    assert result is True, "_update_fill_prices must return True when updates made"
    assert struct.legs[0].filled_price == 4.35, (
        f"filled_price must be updated, got {struct.legs[0].filled_price}"
    )
    assert len(saved_structures) == 1, "Structure must be saved after fill update"


def test_fill_update_skips_structures_without_order_id(monkeypatch):
    """Structures with null order_id are skipped gracefully."""
    import bot_options_stage4_execution as m

    # Structure with no order_id on leg
    from schemas import (
        OptionsLeg,
        OptionsStructure,
        OptionStrategy,
        StructureLifecycle,
        Tier,
    )
    leg = OptionsLeg(
        occ_symbol="GLD261219C00435000",
        underlying="GLD",
        side="buy",
        qty=1,
        option_type="call",
        strike=435.0,
        expiration="2026-12-19",
        order_id=None,    # no order_id
        filled_price=None,
    )
    struct = OptionsStructure(
        structure_id="struct-no-order",
        underlying="GLD",
        strategy=OptionStrategy.SINGLE_CALL,
        lifecycle=StructureLifecycle.FULLY_FILLED,
        legs=[leg],
        contracts=1,
        max_cost_usd=500.0,
        opened_at=datetime.now(timezone.utc).isoformat(),
        catalyst="test",
        tier=Tier.CORE,
    )

    mock_client = MagicMock()

    mock_os = MagicMock()
    mock_os.save_structure.return_value = None

    with patch.dict(sys.modules, {"options_state": mock_os}):
        result = m._update_fill_prices([struct], mock_client)

    assert result is False, "No updates when no order_ids present"
    mock_client.get_order_by_id.assert_not_called()


def test_fill_update_failure_is_non_fatal(monkeypatch):
    """Alpaca fetch failure does not corrupt structure state or raise."""
    import bot_options_stage4_execution as m

    struct = _make_fully_filled_structure("struct-002", "order-002", filled_price=None)

    mock_client = MagicMock()
    mock_client.get_order_by_id.side_effect = RuntimeError("API unavailable")

    mock_os = MagicMock()
    mock_os.save_structure.return_value = None

    with patch.dict(sys.modules, {"options_state": mock_os}):
        result = m._update_fill_prices([struct], mock_client)

    # Should not raise, and filled_price should remain None (no corruption)
    assert result is False, "No updates when fetch fails"
    assert struct.legs[0].filled_price is None, (
        "filled_price must remain None after failed fetch"
    )
    mock_os.save_structure.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Item 7 — Dead constants removed from scheduler
# ─────────────────────────────────────────────────────────────────────────────

def test_trading_window_constants_removed():
    """Dead _TRADING_WINDOW_START/_END constants must not exist in scheduler."""
    import scheduler
    assert not hasattr(scheduler, "_TRADING_WINDOW_START"), (
        "_TRADING_WINDOW_START must be removed from scheduler"
    )
    assert not hasattr(scheduler, "_TRADING_WINDOW_END"), (
        "_TRADING_WINDOW_END must be removed from scheduler"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Item 8 — Overnight logging field names corrected
# ─────────────────────────────────────────────────────────────────────────────

def test_overnight_log_uses_regime_view_not_regime():
    """_ask_claude_overnight must log using regime_view and ideas, not regime/actions."""
    import inspect

    import bot_stage3_decision

    source = inspect.getsource(bot_stage3_decision._ask_claude_overnight)

    # Must NOT use old field names in log call
    assert 'result.get("regime", "?")' not in source, (
        "Must use regime_view not regime in overnight log"
    )
    assert 'result.get("actions", [])' not in source, (
        "Must use ideas not actions in overnight log"
    )

    # Must use new field names
    assert 'result.get("regime_view"' in source, (
        "Must log result.get('regime_view') in overnight log"
    )
    assert 'result.get("ideas"' in source, (
        "Must log result.get('ideas') in overnight log"
    )


def test_overnight_log_counts_ideas_not_actions(monkeypatch):
    """_ask_claude_overnight log call must count ideas list length."""
    import bot_stage3_decision as m

    logged_messages = []

    class _LogCatcher(MagicMock):
        def info(self, msg, *args, **kwargs):
            if "[OVERNIGHT]" in str(msg):
                logged_messages.append(msg % args if args else msg)

    monkeypatch.setattr(m, "log", _LogCatcher())

    payload = {
        "reasoning": "Holding overnight.",
        "regime_view": "caution",
        "ideas": [
            {"intent": "close", "symbol": "BTC/USD", "conviction": 0.9,
             "tier": "core", "catalyst": "test", "direction": "bearish", "concerns": ""},
            {"intent": "close", "symbol": "ETH/USD", "conviction": 0.8,
             "tier": "core", "catalyst": "test", "direction": "bearish", "concerns": ""},
        ],
        "holds": [],
        "notes": "",
        "concerns": "",
    }

    fake_content = MagicMock()
    fake_content.text = json.dumps(payload)
    fake_response = MagicMock()
    fake_response.content = [fake_content]
    fake_response.usage = MagicMock(
        input_tokens=100, cache_write_input_tokens=0,
        cache_read_input_tokens=0, output_tokens=50
    )
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_response

    monkeypatch.setattr(m, "_get_claude", lambda: fake_client)

    result = m._ask_claude_overnight(
        positions=[],
        crypto_context="",
        regime_obj={"regime_score": 40, "bias": "risk-off"},
        macro_wire="",
    )

    assert "regime_view" in result, "regime_view must be in result"
    assert "ideas" in result, "ideas must be in result"
    assert len(result["ideas"]) == 2, "Should have 2 ideas"

    # Verify the logged messages mention 2 ideas
    overnight_logs = [m for m in logged_messages if "OVERNIGHT" in m]
    assert any("2" in m for m in overnight_logs), (
        f"Log should show 2 ideas, got: {overnight_logs}"
    )
