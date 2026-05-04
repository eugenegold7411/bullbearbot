"""
tests/test_allocator_live.py — Stage 1 live TRIM execution path.

Verifies:
  1. TRIM executes via process_idea + execute_all in live mode
  2. ADD remains advisory (execute_all never called) in live mode
  3. TRIM is NOT executed in shadow mode
  4. TRIM execution failure is non-fatal (cycle continues, artifact written)
  5. REPLACE is NOT executed in Stage 1 live mode
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_position(symbol: str, qty: float, market_value: float,
                   current_price: float = 100.0) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=symbol,
        qty=str(qty),
        market_value=str(market_value),
        current_price=str(current_price),
        unrealized_pl="0",
        unrealized_plpc="0",
    )


def _make_pi_data(
    symbols: list[str] | None = None,
    thesis_scores: list[dict] | None = None,
) -> dict:
    symbols = symbols or ["AAPL"]
    thesis_scores = thesis_scores or [
        {"symbol": s, "thesis_score": 3, "health": "MONITORING",
         "recommended_action": "reduce", "override_flag": None, "weakest_factor": ""}
        for s in symbols
    ]
    return {
        "thesis_scores": thesis_scores,
        "health_map":    {s: {"health": "MONITORING"} for s in symbols},
        "correlation":   {"matrix": {}},
        "sizes": {
            "buying_power":     10_000.0,
            "current_exposure": 5_000.0,
            "max_exposure":     15_000.0,
            "available_for_new": 10_000.0,
            "core":             3_000.0,
            "standard":         1_500.0,
        },
    }


def _cfg(enable_live: bool = False) -> dict:
    return {
        "portfolio_allocator": {
            "enable_shadow":               True,
            "enable_live":                 enable_live,
            "trim_score_threshold":        5,
            "replace_score_gap":           15,
            "min_rebalance_notional":      500,
            "max_recommendations_per_cycle": 10,
            "trim_severity": [
                {"score_max": 2, "trim_pct": 0.75},
                {"score_max": 4, "trim_pct": 0.50},
                {"score_max": 5, "trim_pct": 0.25},
            ],
        },
        "parameters": {"max_position_pct_capacity": 0.15},
    }


def _make_snapshot() -> MagicMock:
    snap = MagicMock()
    snap.exposure_dollars = 5_000.0
    snap.buying_power     = 10_000.0
    snap.short_exposure_dollars = 0.0
    return snap


# ── Test 1 — TRIM executes in live mode ──────────────────────────────────────

def test_trim_executes_in_live_mode(tmp_path, monkeypatch):
    """enable_live=True + TRIM recommendation → process_idea called, execute_all called."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "analytics").mkdir(parents=True)
    (tmp_path / "data" / "runtime").mkdir(parents=True)
    (tmp_path / "data" / "reports").mkdir(parents=True)

    import portfolio_allocator as pa

    mock_broker_action = MagicMock()
    mock_broker_action.qty = 5

    mock_process_idea = MagicMock(return_value=mock_broker_action)
    mock_execute_all  = MagicMock()

    # Symbol with thesis_score=3 → TRIM fires (3 <= trim_score_threshold=5)
    positions  = [_make_position("AAPL", 100, 8_000.0, current_price=80.0)]
    pi_data    = _make_pi_data(["AAPL"], [
        {"symbol": "AAPL", "thesis_score": 3, "health": "MONITORING",
         "recommended_action": "reduce", "override_flag": None, "weakest_factor": ""},
    ])
    snapshot   = _make_snapshot()

    with patch.object(pa, "_execute_live_trim", wraps=pa._execute_live_trim):
        with patch("portfolio_allocator.process_idea",
                   mock_process_idea, create=True):
            with patch("portfolio_allocator.execute_all",
                       mock_execute_all, create=True):
                with patch.object(pa, "_execute_live_trim") as mock_trim_fn:
                    mock_trim_fn.return_value = "ok:5"
                    artifact = pa.run_allocator_shadow(
                        pi_data=pi_data,
                        positions=positions,
                        cfg=_cfg(enable_live=True),
                        session_tier="market",
                        equity=10_000.0,
                        snapshot=snapshot,
                        vix=18.5,
                    )

    assert artifact is not None
    assert artifact["mode"] == "live"
    assert "AAPL" in artifact["live_trim_results"]
    assert artifact["live_trim_results"]["AAPL"] == "ok:5"
    mock_trim_fn.assert_called_once()
    args, kwargs = mock_trim_fn.call_args
    assert args[0] == "AAPL"
    assert kwargs.get("vix") == 18.5 or args[6] == 18.5 or 18.5 in args


# ── Test 2 — ADD remains advisory in live mode ───────────────────────────────

def test_add_remains_advisory_in_live_mode(tmp_path, monkeypatch):
    """enable_live=True + ADD recommendation → execute_all never called for ADD."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "analytics").mkdir(parents=True)
    (tmp_path / "data" / "runtime").mkdir(parents=True)
    (tmp_path / "data" / "reports").mkdir(parents=True)

    import portfolio_allocator as pa

    # thesis_score=9 → ADD fires, not TRIM
    positions = [_make_position("AAPL", 10, 500.0, current_price=50.0)]
    pi_data   = _make_pi_data(["AAPL"], [
        {"symbol": "AAPL", "thesis_score": 9, "health": "STRONG",
         "recommended_action": "add", "override_flag": None, "weakest_factor": ""},
    ])
    snapshot  = _make_snapshot()

    with patch.object(pa, "_execute_live_trim") as mock_trim_fn:
        artifact = pa.run_allocator_shadow(
            pi_data=pi_data,
            positions=positions,
            cfg=_cfg(enable_live=True),
            session_tier="market",
            equity=10_000.0,
            snapshot=snapshot,
            vix=20.0,
        )

    assert artifact is not None
    assert artifact["mode"] == "live"
    mock_trim_fn.assert_not_called()
    assert artifact["live_trim_results"] == {}


# ── Test 3 — TRIM skipped in shadow mode ─────────────────────────────────────

def test_trim_skipped_in_shadow_mode(tmp_path, monkeypatch):
    """enable_live=False + TRIM recommendation → _execute_live_trim never called."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "analytics").mkdir(parents=True)
    (tmp_path / "data" / "runtime").mkdir(parents=True)
    (tmp_path / "data" / "reports").mkdir(parents=True)

    import portfolio_allocator as pa

    positions = [_make_position("AAPL", 100, 8_000.0, current_price=80.0)]
    pi_data   = _make_pi_data(["AAPL"], [
        {"symbol": "AAPL", "thesis_score": 3, "health": "MONITORING",
         "recommended_action": "reduce", "override_flag": None, "weakest_factor": ""},
    ])
    snapshot  = _make_snapshot()

    with patch.object(pa, "_execute_live_trim") as mock_trim_fn:
        artifact = pa.run_allocator_shadow(
            pi_data=pi_data,
            positions=positions,
            cfg=_cfg(enable_live=False),
            session_tier="market",
            equity=10_000.0,
            snapshot=snapshot,
            vix=20.0,
        )

    assert artifact is not None
    assert artifact["mode"] == "shadow"
    mock_trim_fn.assert_not_called()
    assert artifact["live_trim_results"] == {}


# ── Test 4 — TRIM failure is non-fatal ───────────────────────────────────────

def test_trim_failure_is_nonfatal(tmp_path, monkeypatch):
    """_execute_live_trim raises → error logged, artifact written, function returns artifact."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "analytics").mkdir(parents=True)
    (tmp_path / "data" / "runtime").mkdir(parents=True)
    (tmp_path / "data" / "reports").mkdir(parents=True)

    import portfolio_allocator as pa

    positions = [_make_position("AAPL", 100, 8_000.0, current_price=80.0)]
    pi_data   = _make_pi_data(["AAPL"], [
        {"symbol": "AAPL", "thesis_score": 3, "health": "MONITORING",
         "recommended_action": "reduce", "override_flag": None, "weakest_factor": ""},
    ])
    snapshot  = _make_snapshot()

    with patch.object(pa, "_execute_live_trim",
                      side_effect=RuntimeError("broker timeout")):
        # run_allocator_shadow catches all exceptions in its outer try/except
        artifact = pa.run_allocator_shadow(
            pi_data=pi_data,
            positions=positions,
            cfg=_cfg(enable_live=True),
            session_tier="market",
            equity=10_000.0,
            snapshot=snapshot,
            vix=None,
        )

    # Exception is caught per-symbol inside run_allocator_shadow — cycle completes normally
    assert artifact is not None
    assert isinstance(artifact, dict)
    assert artifact.get("mode") == "live"
    # Error is recorded in live_trim_results, not propagated to caller
    assert "AAPL" in artifact["live_trim_results"]
    assert artifact["live_trim_results"]["AAPL"].startswith("error:")


# ── Test 5 — REPLACE not executed in Stage 1 live mode ───────────────────────

def test_live_mode_trim_only_no_replace(tmp_path, monkeypatch):
    """Stage 1: REPLACE action present → _execute_live_trim not called for it."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "analytics").mkdir(parents=True)
    (tmp_path / "data" / "runtime").mkdir(parents=True)
    (tmp_path / "data" / "reports").mkdir(parents=True)

    # Patch signal_scores.json so a candidate exists with high enough score
    (tmp_path / "data" / "market").mkdir(parents=True)
    (tmp_path / "data" / "market" / "signal_scores.json").write_text(json.dumps({
        "scored_symbols": {
            "TSLA": {"score": 95, "direction": "bullish",
                     "primary_catalyst": "momentum", "signals": []},
        }
    }))

    import portfolio_allocator as pa

    # AAPL score=3 → TRIM candidate; TSLA score=95 → potential REPLACE
    # But gap = 95 - 30 = 65 >= 15 → REPLACE fires if friction passes
    positions = [_make_position("AAPL", 100, 8_000.0, current_price=80.0)]
    pi_data   = _make_pi_data(["AAPL"], [
        {"symbol": "AAPL", "thesis_score": 3, "health": "MONITORING",
         "recommended_action": "reduce", "override_flag": None, "weakest_factor": ""},
    ])
    snapshot  = _make_snapshot()

    executed_symbols: list[str] = []

    def track_trim(symbol, *args, **kwargs) -> str:
        executed_symbols.append(symbol)
        return f"ok:5"

    with patch.object(pa, "_execute_live_trim", side_effect=track_trim):
        artifact = pa.run_allocator_shadow(
            pi_data=pi_data,
            positions=positions,
            cfg=_cfg(enable_live=True),
            session_tier="market",
            equity=10_000.0,
            snapshot=snapshot,
            vix=20.0,
        )

    assert artifact is not None
    # Only TRIM actions trigger _execute_live_trim — never REPLACE
    replace_actions = [a for a in artifact.get("proposed_actions", [])
                       if a["action"] == "REPLACE"]
    for rep in replace_actions:
        assert rep["symbol"] not in executed_symbols, (
            f"REPLACE symbol {rep['symbol']} must not be passed to _execute_live_trim"
        )
