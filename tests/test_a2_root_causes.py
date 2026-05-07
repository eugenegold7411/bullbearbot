"""
tests/test_a2_root_causes.py — root-cause regression tests for A2 debate write path.

Tests:
  1. Winner (attempt 1) gets the full debate_result stored on its structure.
  2. Fallback (attempt 2+) gets an empty debate snapshot — no cross-candidate bleed.
  3. Legacy free-form path stores debate_parsed from the decision record.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import bot_options_stage4_execution as _exe

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_decision_record(
    debate_parsed: dict | None = None,
    selected_candidate_id: str = "cand-A",
):
    dr = MagicMock()
    dr.debate_parsed    = debate_parsed if debate_parsed is not None else {}
    dr.decision_id      = "test-decision-001"
    dr.execution_result = None
    dr.no_trade_reason  = None
    return dr


def _make_candidate_structure(candidate_id: str, symbol: str, structure_type: str = "debit_call_spread"):
    return {
        "candidate_id":   candidate_id,
        "symbol":         symbol,
        "structure_type": structure_type,
        "a1_direction":   "bullish",
        "max_loss":       500.0,
        "delta":          0.45,
        "theta":          -0.10,
        "vega":           0.30,
    }


def _make_proposal(symbol: str):
    p = MagicMock()
    p.symbol    = symbol
    p.direction = "bullish"
    p.iv_rank   = 25.0
    return p


def _build_mock_structure(symbol: str):
    s = MagicMock()
    s.underlying = symbol
    s.debate     = None
    s.delta = s.theta = s.vega = None
    s.strategy       = MagicMock()
    s.strategy.value = "call_debit_spread"
    s.legs           = []
    s.order_ids      = []
    return s


def _fake_result(symbol: str, struct_id: str):
    r = MagicMock()
    r.status       = "observation"
    r.structure_id = struct_id
    r.to_dict.return_value = {
        "status": "observation", "structure_id": struct_id, "underlying": symbol,
    }
    return r


# ---------------------------------------------------------------------------
# Test 1: Winner (attempt 1) receives the full debate snapshot
# ---------------------------------------------------------------------------

def test_winner_gets_full_debate():
    """Attempt-1 build stores debate_result with full confidence/reasons/selected_id."""
    full_debate = {
        "selected_candidate_id":     "cand-A",
        "confidence":                0.82,
        "reasons":                   "FRO selected: strong momentum. XPEV rejected: IV too low.",
        "key_risks":                 ["liquidity thin"],
        "reject":                    False,
        "recommended_size_modifier": 1.0,
    }
    dr           = _make_decision_record(debate_parsed=full_debate)
    cand_structs = [_make_candidate_structure("cand-A", "FRO")]
    candidates   = [_make_proposal("FRO")]
    fro_struct   = _build_mock_structure("FRO")
    saved        = []

    with (
        patch("bot_options_stage4_execution._load_strategy_config",
              return_value={"account2": {}}),
        patch("bot_options_stage4_execution._is_duplicate_submission", return_value=False),
        patch("bot_options_stage4_execution._log_attribution"),
        patch.dict(sys.modules, {
            "options_data":
                MagicMock(**{"fetch_options_chain.return_value": MagicMock()}),
            "options_builder":
                MagicMock(**{"build_structure.return_value": (fro_struct, None)}),
            "options_state":
                MagicMock(**{"save_structure.side_effect": saved.append,
                             "load_structures.return_value": []}),
            "order_executor_options":
                MagicMock(**{"submit_options_order.return_value": _fake_result("FRO", "s001")}),
        }),
    ):
        _exe.submit_selected_candidate(
            decision_record=dr, alpaca_client=MagicMock(),
            candidates=candidates, candidate_structures=cand_structs,
            iv_summaries={}, equity=100_000.0,
            pf_allow_new_entries=True, pf_allow_live_orders=False,
            obs_mode=True, a2_mode=None,
        )

    assert len(saved) == 1
    debate = saved[0].debate
    assert debate["confidence"] == 0.82
    assert "FRO selected" in debate["reasons"]
    assert debate["selected_candidate_id"] == "cand-A"


# ---------------------------------------------------------------------------
# Test 2: Fallback (attempt 2) receives an empty debate snapshot — no bleed
# ---------------------------------------------------------------------------

def test_fallback_gets_empty_debate():
    """When winner build fails and fallback succeeds, fallback gets empty debate."""
    full_debate = {
        "selected_candidate_id":     "cand-A",
        "confidence":                0.80,
        "reasons":                   "FRO selected. XPEV is rejected: unfavorable skew.",
        "key_risks":                 [],
        "reject":                    False,
        "recommended_size_modifier": 1.0,
    }
    dr           = _make_decision_record(debate_parsed=full_debate)
    cand_structs = [
        _make_candidate_structure("cand-A", "FRO"),   # winner — will fail
        _make_candidate_structure("cand-B", "XPEV"),  # fallback — will succeed
    ]
    candidates  = [_make_proposal("FRO"), _make_proposal("XPEV")]
    xpev_struct = _build_mock_structure("XPEV")
    saved       = []

    def fake_build(symbol, strategy, direction, conviction, iv_rank,
                   max_cost_usd, chain, equity, config):
        if symbol == "FRO":
            return None, "chain_unavailable"
        return xpev_struct, None

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
                MagicMock(**{"save_structure.side_effect": saved.append,
                             "load_structures.return_value": []}),
            "order_executor_options":
                MagicMock(**{"submit_options_order.return_value": _fake_result("XPEV", "s002")}),
        }),
    ):
        _exe.submit_selected_candidate(
            decision_record=dr, alpaca_client=MagicMock(),
            candidates=candidates, candidate_structures=cand_structs,
            iv_summaries={}, equity=100_000.0,
            pf_allow_new_entries=True, pf_allow_live_orders=False,
            obs_mode=True, a2_mode=None,
        )

    assert len(saved) == 1
    debate = saved[0].debate
    # Empty snapshot — no cross-candidate bleed
    assert debate["confidence"] is None,         f"expected None, got {debate['confidence']}"
    assert debate["reasons"] == "",              f"expected '', got {debate['reasons']!r}"
    assert debate["selected_candidate_id"] is None
    assert "XPEV" not in debate["reasons"]
    assert "FRO" not in debate["reasons"]


# ---------------------------------------------------------------------------
# Test 3: Legacy free-form path stores debate_parsed from the decision record
# ---------------------------------------------------------------------------

def test_legacy_path_stores_debate():
    """Legacy free-form path (no selected_candidate_id) stores decision_record.debate_parsed."""
    legacy_debate = {
        # No selected_candidate_id → triggers legacy path
        "actions": [{"action": "enter_long", "symbol": "SPY", "max_cost_usd": 800.0}],
        "confidence": 0.70,
        "reasons":    "SPY breakout on volume.",
        "key_risks":  ["vix spike risk"],
    }
    dr = _make_decision_record(debate_parsed=legacy_debate)

    spy_struct = _build_mock_structure("SPY")
    spy_prop   = MagicMock()
    spy_prop.symbol      = "SPY"
    spy_prop.direction   = "bullish"
    spy_prop.iv_rank     = 30.0
    spy_prop.conviction  = 0.70
    spy_prop.strategy    = MagicMock()
    spy_prop.max_cost_usd = 800.0
    saved = []

    with (
        patch("bot_options_stage4_execution._load_strategy_config",
              return_value={"account2": {}}),
        patch("bot_options_stage4_execution._log_attribution"),
        patch.dict(sys.modules, {
            "options_data":
                MagicMock(**{"fetch_options_chain.return_value": MagicMock()}),
            "options_builder":
                MagicMock(**{"build_structure.return_value": (spy_struct, None)}),
            "options_state":
                MagicMock(**{"save_structure.side_effect": saved.append,
                             "load_structures.return_value": []}),
            "order_executor_options":
                MagicMock(**{"submit_options_order.return_value": _fake_result("SPY", "s003")}),
        }),
    ):
        _exe.submit_selected_candidate(
            decision_record=dr, alpaca_client=MagicMock(),
            candidates=[spy_prop],
            candidate_structures=[],   # empty → legacy path
            iv_summaries={}, equity=100_000.0,
            pf_allow_new_entries=True, pf_allow_live_orders=False,
            obs_mode=True, a2_mode=None,
        )

    assert len(saved) == 1
    debate = saved[0].debate
    assert debate["confidence"] == 0.70
    assert "SPY breakout" in debate["reasons"]
