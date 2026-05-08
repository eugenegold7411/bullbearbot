"""
tests/test_spread_execution_guard.py — FIX-A/B/C/D spread execution guard tests.

Tests:
  a) Spread short leg fails → trade aborted (FIX-A), no order, CRITICAL logged
  b) Strategy mismatch: debate=spread, structure=single_call → _validate returns False
  c) Strategy match: debate=spread, structure=spread → _validate returns True
  d) execution_fallback=True AND risk_executed > risk_authorized → CRITICAL, no order
  e) Audit trail fields populated correctly on clean winner execution
  f) Single_call selected by debate → single_call executes → no mismatch
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

import bot_options_stage4_execution as _exe
from bot_options_stage4_execution import _validate_execution_matches_debate

# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_decision_record(
    debate_parsed: dict | None = None,
    decision_id: str = "a2_dec_test_001",
):
    dr = MagicMock()
    dr.debate_parsed = debate_parsed if debate_parsed is not None else {}
    dr.decision_id = decision_id
    dr.execution_result = None
    dr.no_trade_reason = None
    return dr


def _make_candidate(
    candidate_id: str,
    symbol: str,
    structure_type: str,
    max_loss: float = 775.0,
) -> dict:
    return {
        "candidate_id":   candidate_id,
        "symbol":         symbol,
        "structure_type": structure_type,
        "a1_direction":   "bullish",
        "max_loss":       max_loss,
        "delta":          0.50,
        "theta":          -0.20,
        "vega":           0.25,
    }


def _make_structure(strategy_value: str) -> MagicMock:
    from schemas import OptionStrategy
    s = MagicMock()
    s.strategy = OptionStrategy(strategy_value)
    s.legs = []
    return s


def _fake_result(symbol: str, structure_id: str = "sid-001") -> MagicMock:
    r = MagicMock()
    r.status = "submitted"
    r.structure_id = structure_id
    r.to_dict.return_value = {
        "status": "submitted",
        "structure_id": structure_id,
        "underlying": symbol,
    }
    return r


def _spread_debate(candidate_id: str, conf: float = 0.76) -> dict:
    return {
        "selected_candidate_id": candidate_id,
        "confidence":            conf,
        "reject":                False,
        "reasons":               "Spread is better.",
        "key_risks":             [],
        "recommended_size_modifier": 1.0,
    }


# ── Test a: spread short leg fails → FIX-A aborts, no order ──────────────────

def test_spread_short_leg_fail_aborts_no_order():
    """When spread build fails and fallback has different strategy, trade is aborted."""
    spread_cand = _make_candidate("spread-01", "V", "debit_call_spread", max_loss=775.0)
    call_cand   = _make_candidate("call-02",   "V", "long_call",          max_loss=4792.5)

    dr = _make_decision_record(_spread_debate("spread-01"))
    submitted = []
    whatsapp_msgs = []

    def fake_build(symbol, strategy, direction, conviction, iv_rank,
                   max_cost_usd, chain, equity, config):
        return None, "liquidity check failed: short leg OI=31 < 50"

    with (
        patch("bot_options_stage4_execution._load_strategy_config",
              return_value={"account2": {}}),
        patch("bot_options_stage4_execution._is_duplicate_submission", return_value=False),
        patch("bot_options_stage4_execution._log_attribution"),
        patch.dict(sys.modules, {
            "options_data":
                MagicMock(**{"fetch_options_chain.return_value": MagicMock()}),
            "options_builder":
                MagicMock(**{"build_structure.side_effect": fake_build}),
            "options_state":
                MagicMock(**{"save_structure.return_value": None,
                             "load_structures.return_value": []}),
            "order_executor_options":
                MagicMock(**{"submit_options_order.side_effect":
                             lambda *a, **kw: submitted.append(True) or _fake_result("V")}),
            "notifications":
                MagicMock(**{"send_whatsapp_direct.side_effect": whatsapp_msgs.append}),
        }),
    ):
        result = _exe.submit_selected_candidate(
            decision_record=dr, alpaca_client=MagicMock(),
            candidates=[MagicMock(symbol="V", direction="bullish", iv_rank=16.3)],
            candidate_structures=[spread_cand, call_cand],
            iv_summaries={}, equity=100_000.0,
            pf_allow_new_entries=True, pf_allow_live_orders=False,
            obs_mode=True, a2_mode=None,
        )

    assert result == "rejected",           f"expected rejected, got {result}"
    assert len(submitted) == 0,            "no order should have been submitted"
    assert dr.no_trade_reason == "spread_leg_failed_no_fallback"
    assert len(whatsapp_msgs) == 1
    assert "ABORTED" in whatsapp_msgs[0]
    assert "V" in whatsapp_msgs[0]


# ── Test b: _validate returns False on strategy mismatch ─────────────────────

def test_validate_returns_false_on_mismatch():
    """debate=debit_call_spread but structure.strategy=single_call → False + CRITICAL."""
    from bot_options_stage2_structures import _STRATEGY_FROM_STRUCTURE as smap
    winner = _make_candidate("w", "V", "debit_call_spread")
    structure = _make_structure("single_call")

    whatsapp_msgs = []
    with patch.dict(sys.modules, {
        "notifications": MagicMock(**{
            "send_whatsapp_direct.side_effect": whatsapp_msgs.append,
        }),
    }):
        result = _validate_execution_matches_debate(winner, structure, smap)

    assert result is False
    assert len(whatsapp_msgs) == 1
    assert "mismatch" in whatsapp_msgs[0].lower() or "BLOCKED" in whatsapp_msgs[0]


# ── Test c: _validate returns True on strategy match ─────────────────────────

def test_validate_returns_true_on_match():
    """debate=debit_call_spread and structure.strategy=call_debit_spread → True."""
    from bot_options_stage2_structures import _STRATEGY_FROM_STRUCTURE as smap
    winner = _make_candidate("w", "V", "debit_call_spread")
    structure = _make_structure("call_debit_spread")

    with patch.dict(sys.modules, {
        "notifications": MagicMock(),
    }):
        result = _validate_execution_matches_debate(winner, structure, smap)

    assert result is True


# ── Test d: fallback with higher risk → CRITICAL, no order ───────────────────

def test_fallback_higher_risk_blocked():
    """Same-type fallback where risk_executed > risk_authorized fires CRITICAL and blocks."""
    spread_winner  = _make_candidate("s1", "IWM", "debit_call_spread", max_loss=500.0)
    spread_fallback = _make_candidate("s2", "IWM", "debit_call_spread", max_loss=1500.0)

    dr = _make_decision_record(_spread_debate("s1"))
    submitted = []
    whatsapp_msgs = []

    call_count = [0]

    def fake_build(symbol, strategy, direction, conviction, iv_rank,
                   max_cost_usd, chain, equity, config):
        call_count[0] += 1
        if call_count[0] == 1:
            return None, "long leg volume too low"
        return _make_structure("call_debit_spread"), None

    with (
        patch("bot_options_stage4_execution._load_strategy_config",
              return_value={"account2": {}}),
        patch("bot_options_stage4_execution._is_duplicate_submission", return_value=False),
        patch("bot_options_stage4_execution._log_attribution"),
        patch.dict(sys.modules, {
            "options_data":
                MagicMock(**{"fetch_options_chain.return_value": MagicMock()}),
            "options_builder":
                MagicMock(**{"build_structure.side_effect": fake_build}),
            "options_state":
                MagicMock(**{"save_structure.return_value": None,
                             "load_structures.return_value": []}),
            "order_executor_options":
                MagicMock(**{"submit_options_order.side_effect":
                             lambda *a, **kw: submitted.append(True) or _fake_result("IWM")}),
            "notifications":
                MagicMock(**{"send_whatsapp_direct.side_effect": whatsapp_msgs.append}),
        }),
    ):
        result = _exe.submit_selected_candidate(
            decision_record=dr, alpaca_client=MagicMock(),
            candidates=[MagicMock(symbol="IWM", direction="bullish", iv_rank=20.0)],
            candidate_structures=[spread_winner, spread_fallback],
            iv_summaries={}, equity=100_000.0,
            pf_allow_new_entries=True, pf_allow_live_orders=False,
            obs_mode=True, a2_mode=None,
        )

    assert result == "rejected"
    assert len(submitted) == 0,   "no order when fallback risk exceeds authorized"
    assert dr.no_trade_reason == "spread_leg_failed_no_fallback"
    assert any("CRITICAL" in m or "fallback" in m.lower() for m in whatsapp_msgs), \
        f"expected CRITICAL WhatsApp, got: {whatsapp_msgs}"


# ── Test e: audit trail fields stamped on winner execution ────────────────────

def test_audit_trail_populated_on_winner_execution():
    """When winner succeeds, intended_structure/execution_fallback/risk fields are stamped."""
    spread_cand = _make_candidate("s1", "V", "debit_call_spread", max_loss=775.0)
    structure   = _make_structure("call_debit_spread")
    saved       = []

    dr = _make_decision_record(_spread_debate("s1"))

    with (
        patch("bot_options_stage4_execution._load_strategy_config",
              return_value={"account2": {}}),
        patch("bot_options_stage4_execution._is_duplicate_submission", return_value=False),
        patch("bot_options_stage4_execution._log_attribution"),
        patch.dict(sys.modules, {
            "options_data":
                MagicMock(**{"fetch_options_chain.return_value": MagicMock()}),
            "options_builder":
                MagicMock(**{"build_structure.return_value": (structure, None)}),
            "options_state":
                MagicMock(**{"save_structure.side_effect": saved.append,
                             "load_structures.return_value": []}),
            "order_executor_options":
                MagicMock(**{"submit_options_order.return_value": _fake_result("V")}),
            "notifications": MagicMock(),
        }),
    ):
        result = _exe.submit_selected_candidate(
            decision_record=dr, alpaca_client=MagicMock(),
            candidates=[MagicMock(symbol="V", direction="bullish", iv_rank=16.3)],
            candidate_structures=[spread_cand],
            iv_summaries={}, equity=100_000.0,
            pf_allow_new_entries=True, pf_allow_live_orders=False,
            obs_mode=True, a2_mode=None,
        )

    assert result == "submitted"
    assert len(saved) == 1
    s = saved[0]
    assert s.intended_structure == "debit_call_spread", \
        f"expected 'debit_call_spread', got {s.intended_structure!r}"
    assert s.execution_fallback is False, \
        f"expected False, got {s.execution_fallback!r}"
    assert s.fallback_reason is None, \
        f"expected None, got {s.fallback_reason!r}"
    assert s.risk_authorized == pytest.approx(775.0), \
        f"expected 775.0, got {s.risk_authorized}"
    assert s.risk_executed == pytest.approx(775.0), \
        f"expected 775.0, got {s.risk_executed}"


# ── Test f: single_call approved by debate → executes cleanly, no mismatch ───

def test_single_call_approved_by_debate_no_mismatch():
    """When debate selects long_call, single_call execution is valid (no mismatch)."""
    call_cand = _make_candidate("c1", "SPY", "long_call", max_loss=2000.0)
    structure = _make_structure("single_call")
    saved     = []

    dr = _make_decision_record({
        "selected_candidate_id": "c1",
        "confidence":            0.80,
        "reject":                False,
        "reasons":               "High conviction long call.",
        "key_risks":             [],
        "recommended_size_modifier": 1.0,
    })

    with (
        patch("bot_options_stage4_execution._load_strategy_config",
              return_value={"account2": {}}),
        patch("bot_options_stage4_execution._is_duplicate_submission", return_value=False),
        patch("bot_options_stage4_execution._log_attribution"),
        patch.dict(sys.modules, {
            "options_data":
                MagicMock(**{"fetch_options_chain.return_value": MagicMock()}),
            "options_builder":
                MagicMock(**{"build_structure.return_value": (structure, None)}),
            "options_state":
                MagicMock(**{"save_structure.side_effect": saved.append,
                             "load_structures.return_value": []}),
            "order_executor_options":
                MagicMock(**{"submit_options_order.return_value": _fake_result("SPY")}),
            "notifications": MagicMock(),
        }),
    ):
        result = _exe.submit_selected_candidate(
            decision_record=dr, alpaca_client=MagicMock(),
            candidates=[MagicMock(symbol="SPY", direction="bullish", iv_rank=15.0)],
            candidate_structures=[call_cand],
            iv_summaries={}, equity=100_000.0,
            pf_allow_new_entries=True, pf_allow_live_orders=False,
            obs_mode=True, a2_mode=None,
        )

    assert result == "submitted", f"expected submitted, got {result}"
    assert len(saved) == 1
    s = saved[0]
    assert s.intended_structure == "long_call"
    assert s.execution_fallback is False
    assert s.risk_authorized == pytest.approx(2000.0)
    assert s.risk_executed   == pytest.approx(2000.0)
