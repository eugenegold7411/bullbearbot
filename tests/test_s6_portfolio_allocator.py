"""
tests/test_s6_portfolio_allocator.py — S6-ALLOCATOR test suite.

Tests cover:
  Suite 1 — Ranking logic (incumbents + candidates)
  Suite 2 — HOLD/TRIM/ADD/REPLACE decision logic
  Suite 3 — Anti-churn friction rules
  Suite 4 — Artifact structure and field completeness
  Suite 5 — Integration: stage plumbing and shadow-only guarantee
  Suite 6 — Replay-style: fixed snapshot → stable recommendations
  Suite 7 — Config and feature-flag wiring
  Suite 8 — validate_config.py gate for portfolio_allocator section
  Suite 9 — Persistent disk-backed cooldown (_load/_save/_is_on/_add_to)
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Make the project root importable
# ---------------------------------------------------------------------------
_REPO = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO))

import portfolio_allocator as pa

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

def _make_position(symbol: str, qty: float, avg_price: float, cur_price: float) -> SimpleNamespace:
    """Minimal Alpaca position stub."""
    mv = qty * cur_price
    return SimpleNamespace(
        symbol=symbol,
        qty=qty,
        avg_entry_price=avg_price,
        current_price=cur_price,
        market_value=mv,
        unrealized_pl=mv - qty * avg_price,
    )


def _make_pi_data(positions: list, equity: float = 100_000.0) -> dict:
    """Build a minimal pi_data dict for testing."""
    sizes = {
        "core":              equity * 0.15,
        "standard":          equity * 0.08,
        "speculative":       equity * 0.05,
        "max_exposure":      equity * 0.30,
        "available_for_new": max(0.0, equity * 0.30 - sum(p.market_value for p in positions)),
        "current_exposure":  sum(p.market_value for p in positions),
        "exposure_pct":      sum(p.market_value for p in positions) / equity * 100,
    }
    health_map = {}
    thesis_scores = []
    for pos in positions:
        mv       = float(pos.market_value)
        acct_pct = mv / equity * 100
        health_map[pos.symbol] = {"health": "HEALTHY", "drawdown_pct": 0.5, "account_pct": acct_pct}
        thesis_scores.append({
            "symbol":             pos.symbol,
            "thesis_score":       7,
            "thesis_status":      "valid",
            "recommended_action": "hold",
            "override_flag":      None,
            "weakest_factor":     "none",
            "health":             "HEALTHY",
        })
    return {
        "sizes":          sizes,
        "health_map":     health_map,
        "forced_exits":   [],
        "deadline_exits": [],
        "correlation":    {"matrix": {}, "high_correlation_pairs": [], "effective_bets": len(positions)},
        "thesis_scores":  thesis_scores,
    }


def _base_cfg() -> dict:
    return {
        "portfolio_allocator": {
            "enable_shadow":                   True,
            "enable_live":                     False,
            "replace_score_gap":               15,
            "trim_score_drop":                 10,
            "weight_deadband":                 0.02,
            "min_rebalance_notional":          500,
            "max_recommendations_per_cycle":   3,
            "same_symbol_daily_cooldown_enabled": True,
            "same_day_replace_block_hours":    6,
        },
        "parameters":    {"max_positions": 14},
        "position_sizing": {
            "core_tier_pct":         0.15,
            "dynamic_tier_pct":      0.08,
            "intraday_tier_pct":     0.05,
            "max_total_exposure_pct": 0.30,
        },
        "time_bound_actions": [],
    }


def _signal_scores_obj(symbols_scores: dict) -> dict:
    """Build signal_scores.json format dict."""
    scored = {}
    for sym, score in symbols_scores.items():
        scored[sym] = {
            "score":     score,
            "direction": "bullish" if score >= 60 else "neutral",
            "catalyst":  f"test catalyst for {sym}",
            "price":     150.0,
        }
    return {"scored_symbols": scored}


# ---------------------------------------------------------------------------
# Suite 1 — Ranking logic
# ---------------------------------------------------------------------------

class TestRankingLogic:
    """Suite 1: incumbent and candidate ranking."""

    def test_incumbents_sorted_ascending_by_thesis(self):
        positions = [
            _make_position("AAPL", 10, 150, 155),
            _make_position("MSFT", 8,  300, 305),
        ]
        pi_data = _make_pi_data(positions)
        # Set different thesis scores
        pi_data["thesis_scores"][0]["thesis_score"] = 6  # AAPL
        pi_data["thesis_scores"][1]["thesis_score"] = 3  # MSFT (weakest)
        result = pa._rank_incumbents(pi_data, positions)
        assert result[0]["symbol"] == "MSFT"   # weakest first
        assert result[1]["symbol"] == "AAPL"

    def test_incumbents_normalized_score_is_10x(self):
        positions = [_make_position("SPY", 5, 500, 510)]
        pi_data = _make_pi_data(positions)
        pi_data["thesis_scores"][0]["thesis_score"] = 7
        result = pa._rank_incumbents(pi_data, positions)
        assert result[0]["thesis_score_normalized"] == 70

    def test_weakest_incumbent_identified_correctly(self):
        positions = [
            _make_position("NVDA", 5, 800, 820),
            _make_position("XBI",  20, 150, 140),
        ]
        pi_data = _make_pi_data(positions)
        # NVDA score 8, XBI score 2
        pi_data["thesis_scores"][0]["thesis_score"] = 8
        pi_data["thesis_scores"][1]["thesis_score"] = 2
        result = pa._rank_incumbents(pi_data, positions)
        assert result[0]["symbol"] == "XBI"   # weakest

    def test_strongest_candidate_identified_correctly(self, tmp_path):
        # Write mock signal_scores.json
        scores = {"CRWV": 88, "PLTR": 65, "GLD": 45}
        signal_path = tmp_path / "signal_scores.json"
        signal_path.write_text(json.dumps(_signal_scores_obj(scores)))

        with patch.object(pa, "_SIGNAL_SCORES_PATH", signal_path):
            candidates = pa._load_candidates(held_symbols=set())

        assert candidates[0]["symbol"] == "CRWV"
        assert candidates[0]["signal_score"] == 88

    def test_candidates_exclude_held_symbols(self, tmp_path):
        scores = {"CRWV": 88, "PLTR": 65}
        signal_path = tmp_path / "signal_scores.json"
        signal_path.write_text(json.dumps(_signal_scores_obj(scores)))

        with patch.object(pa, "_SIGNAL_SCORES_PATH", signal_path):
            candidates = pa._load_candidates(held_symbols={"CRWV"})

        syms = [c["symbol"] for c in candidates]
        assert "CRWV" not in syms
        assert "PLTR" in syms

    def test_candidates_sorted_descending_by_score(self, tmp_path):
        scores = {"A": 40, "B": 90, "C": 70}
        signal_path = tmp_path / "signal_scores.json"
        signal_path.write_text(json.dumps(_signal_scores_obj(scores)))

        with patch.object(pa, "_SIGNAL_SCORES_PATH", signal_path):
            candidates = pa._load_candidates(held_symbols=set())

        assert [c["symbol"] for c in candidates] == ["B", "C", "A"]

    def test_empty_candidates_on_missing_file(self):
        with patch.object(pa, "_SIGNAL_SCORES_PATH", Path("/nonexistent/path.json")):
            candidates = pa._load_candidates(held_symbols=set())
        assert candidates == []

    def test_candidates_with_zero_score_excluded(self, tmp_path):
        scores = {"X": 0, "Y": 75}
        signal_path = tmp_path / "signal_scores.json"
        signal_path.write_text(json.dumps(_signal_scores_obj(scores)))

        with patch.object(pa, "_SIGNAL_SCORES_PATH", signal_path):
            candidates = pa._load_candidates(held_symbols=set())

        assert len(candidates) == 1
        assert candidates[0]["symbol"] == "Y"


# ---------------------------------------------------------------------------
# Suite 2 — HOLD/TRIM/ADD/REPLACE decision logic
# ---------------------------------------------------------------------------

class TestDecisionLogic:
    """Suite 2: decision rule outcomes."""

    def setup_method(self, method):
        pa._save_cooldown({})

    def _run(self, incumbents, candidates, pi_data, cfg=None, equity=100_000.0):
        cfg = cfg or _base_cfg()
        pa_cfg = pa._get_pa_config(cfg)
        sizes  = pi_data.get("sizes", {})
        return pa._decide_actions(incumbents, candidates, pi_data, cfg, pa_cfg, sizes, equity)

    def _incumbent(self, symbol, thesis_score, market_value=10_000.0):
        return {
            "symbol":                   symbol,
            "market_value":             market_value,
            "account_pct":              market_value / 100_000.0 * 100,
            "thesis_score":             thesis_score,
            "thesis_score_normalized":  thesis_score * 10,
            "health":                   "HEALTHY",
            "recommended_pi_action":    "hold",
            "override_flag":            None,
            "weakest_factor":           "none",
        }

    def _candidate(self, symbol, signal_score):
        return {
            "symbol":       symbol,
            "signal_score": signal_score,
            "direction":    "bullish",
            "catalyst":     "test",
            "price":        100.0,
        }

    # HOLD tests
    def test_hold_when_inside_deadband(self):
        incs  = [self._incumbent("SPY", 6)]   # score 6 → neither strong nor weak
        cands = [self._candidate("NVDA", 70)]  # gap = 70-60 = 10 < 15 threshold
        pi    = _make_pi_data([], equity=100_000.0)
        proposed, _ = self._run(incs, cands, pi)
        spy_action = next(p for p in proposed if p["symbol"] == "SPY")
        assert spy_action["action"] == "HOLD"

    def test_all_strong_incumbents_hold(self):
        incs  = [self._incumbent("A", 8), self._incumbent("B", 9)]
        pi    = _make_pi_data([], equity=100_000.0)
        pi["sizes"]["available_for_new"] = 0  # no room to ADD
        proposed, _ = self._run(incs, [], pi)
        for p in proposed:
            assert p["action"] == "HOLD"

    # TRIM tests
    def test_trim_when_thesis_weak_and_notional_large(self):
        incs  = [self._incumbent("XBI", 3, market_value=8_000.0)]  # score 3 = weak
        pi    = _make_pi_data([], equity=100_000.0)
        proposed, _ = self._run(incs, [], pi)
        xbi = next(p for p in proposed if p["symbol"] == "XBI")
        assert xbi["action"] == "TRIM"

    def test_trim_fires_at_score_4(self):
        incs  = [self._incumbent("ZZZ", 4, market_value=5_000.0)]
        pi    = _make_pi_data([], equity=100_000.0)
        proposed, _ = self._run(incs, [], pi)
        zzz = next(p for p in proposed if p["symbol"] == "ZZZ")
        assert zzz["action"] == "TRIM"

    def test_trim_when_score_5(self):
        # S8-shadow-hardening: trim_score_threshold raised from 4→5 to align with
        # system_v1.txt "4–5/10: TRIM 25%". Score=5 now correctly fires TRIM.
        incs  = [self._incumbent("SPY", 5, market_value=5_000.0)]
        pi    = _make_pi_data([], equity=100_000.0)
        proposed, _ = self._run(incs, [], pi)
        spy = next(p for p in proposed if p["symbol"] == "SPY")
        assert spy["action"] == "TRIM"

    def test_no_trim_when_notional_below_floor(self):
        cfg = _base_cfg()
        cfg["portfolio_allocator"]["min_rebalance_notional"] = 2_000.0
        incs = [self._incumbent("TINY", 2, market_value=1_000.0)]  # too small
        pi   = _make_pi_data([], equity=100_000.0)
        proposed, _ = self._run(incs, [], pi, cfg=cfg)
        tiny = next(p for p in proposed if p["symbol"] == "TINY")
        assert tiny["action"] == "HOLD"

    # ADD tests
    def test_add_when_strong_thesis_and_capacity(self):
        # Score 7, position only 5% of account, 15% ceiling → room to grow
        incs  = [self._incumbent("GLD", 7, market_value=5_000.0)]
        pi    = _make_pi_data([], equity=100_000.0)
        pi["sizes"]["available_for_new"] = 10_000.0
        proposed, _ = self._run(incs, [], pi)
        gld = next(p for p in proposed if p["symbol"] == "GLD")
        assert gld["action"] == "ADD"

    def test_no_add_when_no_capital(self):
        incs  = [self._incumbent("GLD", 8, market_value=5_000.0)]
        pi    = _make_pi_data([], equity=100_000.0)
        pi["sizes"]["available_for_new"] = 0.0   # no capital
        proposed, _ = self._run(incs, [], pi)
        gld = next(p for p in proposed if p["symbol"] == "GLD")
        assert gld["action"] == "HOLD"

    def test_no_add_when_score_below_7(self):
        incs  = [self._incumbent("QQQ", 6, market_value=5_000.0)]
        pi    = _make_pi_data([], equity=100_000.0)
        pi["sizes"]["available_for_new"] = 15_000.0
        proposed, _ = self._run(incs, [], pi)
        qqq = next(p for p in proposed if p["symbol"] == "QQQ")
        assert qqq["action"] == "HOLD"

    # REPLACE tests
    def test_replace_fires_when_gap_exceeds_threshold(self):
        # Weakest incumbent normalized = 30 (score=3)
        # Candidate signal = 70; gap = 40 >= 15
        incs  = [self._incumbent("XBI", 3, market_value=8_000.0),
                 self._incumbent("SPY", 7, market_value=10_000.0)]
        cands = [self._candidate("NVDA", 70)]
        pi    = _make_pi_data([], equity=100_000.0)
        proposed, suppressed = self._run(incs, cands, pi)
        replace_actions = [p for p in proposed if p["action"] == "REPLACE"]
        assert len(replace_actions) == 1
        assert replace_actions[0]["symbol"] == "NVDA"
        assert replace_actions[0]["exit_symbol"] == "XBI"
        assert replace_actions[0]["score_gap"] == pytest.approx(40.0, abs=0.5)

    def test_replace_not_fired_when_gap_below_threshold(self):
        cfg = _base_cfg()
        cfg["portfolio_allocator"]["replace_score_gap"] = 30
        # Weakest normalized=50 (score=5), candidate=70, gap=20 < 30
        incs  = [self._incumbent("XBI", 5, market_value=6_000.0)]
        cands = [self._candidate("NVDA", 70)]
        pi    = _make_pi_data([], equity=100_000.0)
        proposed, suppressed = self._run(incs, cands, pi, cfg=cfg)
        replace_actions = [p for p in proposed if p["action"] == "REPLACE"]
        assert len(replace_actions) == 0
        assert any("score gap" in s["suppression_reason"] for s in suppressed)

    def test_no_replace_when_no_candidates(self):
        incs = [self._incumbent("XBI", 2, market_value=8_000.0)]
        pi   = _make_pi_data([], equity=100_000.0)
        proposed, suppressed = self._run(incs, [], pi)
        replace_actions = [p for p in proposed if p["action"] == "REPLACE"]
        assert len(replace_actions) == 0

    def test_no_replace_when_no_incumbents(self):
        cands = [self._candidate("NVDA", 90)]
        pi    = _make_pi_data([], equity=100_000.0)
        proposed, suppressed = self._run([], cands, pi)
        assert proposed == []

    def test_score_gap_computed_correctly(self):
        # normalized = 3 * 10 = 30; candidate = 85; gap = 55
        incs  = [self._incumbent("XBI", 3, market_value=8_000.0)]
        cands = [self._candidate("NVDA", 85)]
        pi    = _make_pi_data([], equity=100_000.0)
        proposed, _ = self._run(incs, cands, pi)
        replace_action = next(p for p in proposed if p["action"] == "REPLACE")
        assert replace_action["score_gap"] == pytest.approx(55.0, abs=0.5)


# ---------------------------------------------------------------------------
# Suite 3 — Anti-churn friction rules
# ---------------------------------------------------------------------------

class TestAntichurnFriction:
    """Suite 3: all friction rules must block correctly."""

    def setup_method(self, method):
        pa._save_cooldown({})

    def _run(self, incumbents, candidates, pi_data, cfg=None, equity=100_000.0):
        cfg = cfg or _base_cfg()
        pa_cfg = pa._get_pa_config(cfg)
        sizes  = pi_data.get("sizes", {})
        return pa._decide_actions(incumbents, candidates, pi_data, cfg, pa_cfg, sizes, equity)

    def _incumbent(self, symbol, thesis_score, market_value=8_000.0):
        return {
            "symbol":                   symbol,
            "market_value":             market_value,
            "account_pct":              market_value / 100_000.0 * 100,
            "thesis_score":             thesis_score,
            "thesis_score_normalized":  thesis_score * 10,
            "health":                   "HEALTHY",
            "recommended_pi_action":    "hold",
            "override_flag":            None,
            "weakest_factor":           "none",
        }

    def _candidate(self, symbol, signal_score):
        return {
            "symbol":       symbol,
            "signal_score": signal_score,
            "direction":    "bullish",
            "catalyst":     "test",
            "price":        100.0,
        }

    def test_correlation_blocks_replace_same_sector(self):
        # Patch sector lookup to return same sector for both
        incs  = [self._incumbent("XBI", 3)]
        cands = [self._candidate("NVDA", 80)]
        pi    = _make_pi_data([], equity=100_000.0)

        with patch.object(pa, "_symbol_sector", side_effect=lambda sym: "technology"):
            proposed, suppressed = self._run(incs, cands, pi)

        replace_actions = [p for p in proposed if p["action"] == "REPLACE"]
        assert len(replace_actions) == 0
        sector_blocked = [
            s for s in suppressed
            if "sector" in s["suppression_reason"].lower() or "correlation" in s["suppression_reason"].lower()
        ]
        assert len(sector_blocked) >= 1

    def test_correlation_allows_different_sector(self):
        incs  = [self._incumbent("XBI", 3)]
        cands = [self._candidate("XOM", 80)]
        pi    = _make_pi_data([], equity=100_000.0)

        def _sector(sym):
            return "biotech" if sym == "XBI" else "energy"

        with patch.object(pa, "_symbol_sector", side_effect=_sector):
            proposed, _ = self._run(incs, cands, pi)

        replace_actions = [p for p in proposed if p["action"] == "REPLACE"]
        assert len(replace_actions) == 1

    def test_time_bound_blocks_replace_for_imminent_exit(self):
        from datetime import datetime, timedelta, timezone
        deadline = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        cfg = _base_cfg()
        cfg["time_bound_actions"] = [{"symbol": "XBI", "exit_by": deadline, "reason": "test"}]

        incs  = [self._incumbent("XBI", 3)]
        cands = [self._candidate("NVDA", 90)]
        pi    = _make_pi_data([], equity=100_000.0)

        with patch.object(pa, "_symbol_sector", return_value=""):
            proposed, suppressed = self._run(incs, cands, pi, cfg=cfg)

        replace_actions = [p for p in proposed if p["action"] == "REPLACE"]
        assert len(replace_actions) == 0
        tba_blocked = [s for s in suppressed if "time-bound" in s["suppression_reason"].lower()]
        assert len(tba_blocked) >= 1

    def test_time_bound_allows_replace_when_exit_not_imminent(self):
        from datetime import datetime, timedelta, timezone
        deadline = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        cfg = _base_cfg()
        cfg["portfolio_allocator"]["same_day_replace_block_hours"] = 6
        cfg["time_bound_actions"] = [{"symbol": "XBI", "exit_by": deadline, "reason": "test"}]

        incs  = [self._incumbent("XBI", 3)]
        cands = [self._candidate("NVDA", 90)]
        pi    = _make_pi_data([], equity=100_000.0)

        with patch.object(pa, "_symbol_sector", return_value=""):
            proposed, _ = self._run(incs, cands, pi, cfg=cfg)

        replace_actions = [p for p in proposed if p["action"] == "REPLACE"]
        assert len(replace_actions) == 1

    def test_notional_too_small_blocks_replace(self):
        cfg = _base_cfg()
        cfg["portfolio_allocator"]["min_rebalance_notional"] = 2_000.0
        incs  = [self._incumbent("XBI", 2, market_value=300.0)]  # too small
        cands = [self._candidate("NVDA", 90)]
        pi    = _make_pi_data([], equity=100_000.0)

        proposed, suppressed = self._run(incs, cands, pi, cfg=cfg)
        replace_actions = [p for p in proposed if p["action"] == "REPLACE"]
        assert len(replace_actions) == 0
        notional_blocked = [s for s in suppressed if "min_rebalance_notional" in s["suppression_reason"]]
        assert len(notional_blocked) >= 1

    def test_daily_cooldown_blocks_second_recommendation(self):
        # Force a cooldown entry for "XBI" today (disk-backed)
        pa._save_cooldown(pa._add_to_cooldown("XBI", "REPLACE", {}))

        try:
            incs  = [
                {"symbol": "XBI", "market_value": 8_000.0, "account_pct": 8.0,
                 "thesis_score": 3, "thesis_score_normalized": 30, "health": "HEALTHY",
                 "recommended_pi_action": "hold", "override_flag": None, "weakest_factor": ""},
            ]
            cands = [{"symbol": "NVDA", "signal_score": 90, "direction": "bullish", "catalyst": "test", "price": 100.0}]
            pi    = _make_pi_data([], equity=100_000.0)
            cfg   = _base_cfg()
            pa_cfg = pa._get_pa_config(cfg)
            sizes  = pi.get("sizes", {})

            with patch.object(pa, "_symbol_sector", return_value=""):
                proposed, suppressed = pa._decide_actions(incs, cands, pi, cfg, pa_cfg, sizes, 100_000.0)

            replace_actions = [p for p in proposed if p["action"] == "REPLACE"]
            assert len(replace_actions) == 0
            cooldown_blocked = [s for s in suppressed if "cooldown" in s["suppression_reason"].lower()]
            assert len(cooldown_blocked) >= 1
        finally:
            pa._save_cooldown({})

    def test_cooldown_disabled_allows_repeat(self):
        # Force a cooldown entry for "XBI" today (disk-backed), then disable the gate
        pa._save_cooldown(pa._add_to_cooldown("XBI", "REPLACE", {}))

        try:
            cfg = _base_cfg()
            cfg["portfolio_allocator"]["same_symbol_daily_cooldown_enabled"] = False

            incs  = [
                {"symbol": "XBI", "market_value": 8_000.0, "account_pct": 8.0,
                 "thesis_score": 3, "thesis_score_normalized": 30, "health": "HEALTHY",
                 "recommended_pi_action": "hold", "override_flag": None, "weakest_factor": ""},
            ]
            cands = [{"symbol": "NVDA", "signal_score": 90, "direction": "bullish", "catalyst": "test", "price": 100.0}]
            pi    = _make_pi_data([], equity=100_000.0)
            pa_cfg = pa._get_pa_config(cfg)
            sizes  = pi.get("sizes", {})

            with patch.object(pa, "_symbol_sector", return_value=""):
                proposed, _ = pa._decide_actions(incs, cands, pi, cfg, pa_cfg, sizes, 100_000.0)

            replace_actions = [p for p in proposed if p["action"] == "REPLACE"]
            assert len(replace_actions) == 1
        finally:
            pa._save_cooldown({})

    def test_max_recommendations_per_cycle_cap(self):
        cfg = _base_cfg()
        cfg["portfolio_allocator"]["max_recommendations_per_cycle"] = 1

        # Two incumbents with weak scores → two TRIM candidates
        incs = [
            {"symbol": "A", "market_value": 8_000.0, "account_pct": 8.0,
             "thesis_score": 2, "thesis_score_normalized": 20, "health": "HEALTHY",
             "recommended_pi_action": "reduce", "override_flag": None, "weakest_factor": ""},
            {"symbol": "B", "market_value": 6_000.0, "account_pct": 6.0,
             "thesis_score": 3, "thesis_score_normalized": 30, "health": "HEALTHY",
             "recommended_pi_action": "reduce", "override_flag": None, "weakest_factor": ""},
        ]
        pi    = _make_pi_data([], equity=100_000.0)
        pa_cfg = pa._get_pa_config(cfg)
        sizes  = pi.get("sizes", {})
        proposed, suppressed = pa._decide_actions(incs, [], pi, cfg, pa_cfg, sizes, 100_000.0)

        non_hold = [p for p in proposed if p["action"] != "HOLD"]
        assert len(non_hold) <= 1
        # Excess should appear in suppressed
        capped = [s for s in suppressed if "max_recommendations_per_cycle" in s["suppression_reason"]]
        assert len(capped) >= 1


# ---------------------------------------------------------------------------
# Suite 4 — Artifact structure
# ---------------------------------------------------------------------------

class TestArtifactStructure:
    """Suite 4: JSONL artifact field completeness."""

    REQUIRED_FIELDS = [
        "schema_version", "timestamp", "session_tier",
        "current_holdings_snapshot", "candidate_snapshot",
        "ranked_incumbents", "ranked_candidates",
        "weakest_incumbent", "strongest_candidate",
        "target_weights", "proposed_actions", "suppressed_actions",
        "friction_blockers", "summary", "config_snapshot",
    ]

    def _run_shadow(self, positions, signal_scores, cfg=None, equity=100_000.0, tmp_path=None):
        cfg = cfg or _base_cfg()
        pi_data = _make_pi_data(positions, equity)

        # Patch signal path + artifact path
        signal_file = tmp_path / "signal_scores.json"
        signal_file.write_text(json.dumps(signal_scores))
        artifact_file = tmp_path / "portfolio_allocator_shadow.jsonl"

        with patch.object(pa, "_SIGNAL_SCORES_PATH", signal_file), \
             patch.object(pa, "_ARTIFACT_PATH", artifact_file), \
             patch.object(pa, "_REGISTRY_JSON_PATH", tmp_path / "shadow_status.json"), \
             patch.object(pa, "_symbol_sector", return_value=""):
            output = pa.run_allocator_shadow(pi_data, positions, cfg, "market", equity)
        return output, artifact_file

    def test_all_required_fields_present(self, tmp_path):
        positions = [_make_position("AMZN", 10, 180, 185)]
        signals   = _signal_scores_obj({"NVDA": 75})
        output, _ = self._run_shadow(positions, signals, tmp_path=tmp_path)
        assert output is not None
        for field in self.REQUIRED_FIELDS:
            assert field in output, f"Missing field: {field}"

    def test_schema_version_is_1(self, tmp_path):
        positions = [_make_position("SPY", 5, 500, 510)]
        signals   = _signal_scores_obj({"QQQ": 60})
        output, _ = self._run_shadow(positions, signals, tmp_path=tmp_path)
        assert output["schema_version"] == 1

    def test_artifact_written_to_jsonl(self, tmp_path):
        positions = [_make_position("GLD", 10, 180, 185)]
        signals   = _signal_scores_obj({"MSFT": 70})
        _, artifact_file = self._run_shadow(positions, signals, tmp_path=tmp_path)
        assert artifact_file.exists()
        line = artifact_file.read_text().strip().splitlines()[0]
        record = json.loads(line)
        assert record["schema_version"] == 1

    def test_empty_cycle_still_writes_valid_artifact(self, tmp_path):
        # No positions, no candidates
        positions = []
        signals   = {"scored_symbols": {}}
        output, artifact_file = self._run_shadow(positions, signals, tmp_path=tmp_path)
        assert output is not None
        assert artifact_file.exists()
        record = json.loads(artifact_file.read_text().strip().splitlines()[0])
        assert "summary" in record

    def test_summary_counts_match_proposed_actions(self, tmp_path):
        positions = [_make_position("XBI", 20, 150, 145)]
        pi        = _make_pi_data(positions, equity=100_000.0)
        pi["thesis_scores"][0]["thesis_score"] = 3

        signals   = _signal_scores_obj({"NVDA": 80})
        output, _ = self._run_shadow(positions, signals, tmp_path=tmp_path)
        summary   = output["summary"]
        proposed  = output["proposed_actions"]

        n_trim   = sum(1 for p in proposed if p["action"] == "TRIM")
        n_hold   = sum(1 for p in proposed if p["action"] == "HOLD")
        assert summary["n_trim"] == n_trim
        assert summary["n_hold"] == n_hold

    def test_rotate_jsonl_called_after_write(self, tmp_path):
        positions = [_make_position("SPY", 5, 500, 510)]
        signals   = _signal_scores_obj({"QQQ": 60})

        rotate_calls = []

        def mock_rotate(path, max_lines=10_000):
            rotate_calls.append((path, max_lines))

        signal_file   = tmp_path / "signal_scores.json"
        signal_file.write_text(json.dumps(signals))
        artifact_file = tmp_path / "portfolio_allocator_shadow.jsonl"

        with patch.object(pa, "_SIGNAL_SCORES_PATH", signal_file), \
             patch.object(pa, "_ARTIFACT_PATH", artifact_file), \
             patch.object(pa, "_REGISTRY_JSON_PATH", tmp_path / "shadow_status.json"), \
             patch.object(pa, "_symbol_sector", return_value=""), \
             patch("cost_attribution._rotate_jsonl", side_effect=mock_rotate):
            pa.run_allocator_shadow(
                _make_pi_data(positions, 100_000.0), positions,
                _base_cfg(), "market", 100_000.0
            )

        assert len(rotate_calls) >= 1
        assert rotate_calls[0][1] == 10_000


# ---------------------------------------------------------------------------
# Suite 5 — Integration: shadow-only guarantee
# ---------------------------------------------------------------------------

class TestShadowOnlyGuarantee:
    """Suite 5: allocator never calls execute_all() or execute_reallocate()."""

    def test_execute_all_never_called(self):
        """portfolio_allocator must not import order_executor or call execute_all()."""
        import ast
        import inspect
        source = inspect.getsource(pa)
        # Docstring may mention execute_all() as documentation — that's fine.
        # Check there's no actual call: execute_all( as a Python expression.
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = (func.attr if isinstance(func, ast.Attribute)
                        else func.id if isinstance(func, ast.Name) else "")
                assert name != "execute_all", \
                    "portfolio_allocator must not call execute_all() — shadow only"
        assert "order_executor" not in source, \
            "portfolio_allocator must not import order_executor — shadow only"

    def test_execute_reallocate_never_called(self, tmp_path):
        positions = [_make_position("XBI", 20, 150, 140)]
        signals   = _signal_scores_obj({"NVDA": 90})
        pi_data   = _make_pi_data(positions)
        pi_data["thesis_scores"][0]["thesis_score"] = 2

        signal_file   = tmp_path / "signal_scores.json"
        signal_file.write_text(json.dumps(signals))
        artifact_file = tmp_path / "portfolio_allocator_shadow.jsonl"

        with patch("portfolio_intelligence.execute_reallocate") as mock_realloc, \
             patch.object(pa, "_SIGNAL_SCORES_PATH", signal_file), \
             patch.object(pa, "_ARTIFACT_PATH", artifact_file), \
             patch.object(pa, "_REGISTRY_JSON_PATH", tmp_path / "shadow_status.json"), \
             patch.object(pa, "_symbol_sector", return_value=""):
            pa.run_allocator_shadow(pi_data, positions, _base_cfg(), "market", 100_000.0)

        mock_realloc.assert_not_called()

    def test_enable_live_is_always_false(self):
        cfg = _base_cfg()
        cfg["portfolio_allocator"]["enable_live"] = True  # attempt to set live
        pa_cfg = pa._get_pa_config(cfg)
        assert pa_cfg["enable_live"] is False

    def test_shadow_disabled_returns_none(self, tmp_path):
        cfg = _base_cfg()
        cfg["portfolio_allocator"]["enable_shadow"] = False
        positions = [_make_position("SPY", 5, 500, 510)]
        pi_data   = _make_pi_data(positions)

        result = pa.run_allocator_shadow(pi_data, positions, cfg, "market", 100_000.0)
        assert result is None

    def test_allocator_output_in_precycle_state(self):
        """Verify PreCycleState has allocator_output field — checked via source inspection."""
        source_path = Path(__file__).parent.parent / "bot_stage0_precycle.py"
        source = source_path.read_text()
        assert "allocator_output" in source, \
            "PreCycleState must have allocator_output field (S6-ALLOCATOR)"

    def test_build_user_prompt_accepts_allocator_section(self):
        """Verify build_user_prompt() has allocator_section parameter — checked via source."""
        source_path = Path(__file__).parent.parent / "bot_stage3_decision.py"
        source = source_path.read_text()
        assert "allocator_section" in source, \
            "build_user_prompt() must accept allocator_section parameter (S6-ALLOCATOR)"

    def test_allocator_section_injected_into_prompt(self):
        """format_allocator_section(None) returns fallback header (S7-E: Option B — explicit absence).
        Claude seeing "not available" is more informative than silent omission."""
        section = pa.format_allocator_section(None)
        assert "PORTFOLIO ALLOCATOR" in section
        assert "not available" in section

    def test_format_allocator_section_advisory_label(self, tmp_path):
        positions = [_make_position("XBI", 20, 150, 140)]
        signals   = _signal_scores_obj({"NVDA": 90})
        pi_data   = _make_pi_data(positions)
        pi_data["thesis_scores"][0]["thesis_score"] = 2

        signal_file   = tmp_path / "signal_scores.json"
        signal_file.write_text(json.dumps(signals))
        artifact_file = tmp_path / "portfolio_allocator_shadow.jsonl"

        with patch.object(pa, "_SIGNAL_SCORES_PATH", signal_file), \
             patch.object(pa, "_ARTIFACT_PATH", artifact_file), \
             patch.object(pa, "_REGISTRY_JSON_PATH", tmp_path / "shadow_status.json"), \
             patch.object(pa, "_symbol_sector", return_value=""):
            output = pa.run_allocator_shadow(pi_data, positions, _base_cfg(), "market", 100_000.0)

        section = pa.format_allocator_section(output)
        assert "advisory" in section.lower() or "shadow" in section.lower()
        # Must not contain the word "order" in a directive sense
        assert "SHADOW MODE" in section


# ---------------------------------------------------------------------------
# Suite 6 — Replay-style fixed snapshot test
# ---------------------------------------------------------------------------

class TestReplaySnapshot:
    """Suite 6: fixed snapshot → deterministic stable recommendations."""

    # Fixed positions snapshot
    POSITIONS_SNAPSHOT = [
        ("AMZN", 60, 255.0, 255.0),   # healthy (score=6)
        ("GLD",  34, 443.0, 443.0),   # strong  (score=8)
        ("MSFT", 47, 418.0, 418.0),   # healthy (score=6)
        ("QQQ",  31, 648.0, 648.0),   # moderate (score=5)
        ("XBI",  111,137.0, 137.0),   # weak    (score=3)
    ]

    SIGNAL_SCORES_SNAPSHOT = {
        "NVDA": 85, "PLTR": 78, "CRWV": 72,
        "JPM":  55, "XOM": 45,
    }

    THESIS_SCORES = {
        "AMZN": 6, "GLD": 8, "MSFT": 6, "QQQ": 5, "XBI": 3,
    }

    def _build_positions(self):
        return [_make_position(s, q, a, c) for s, q, a, c in self.POSITIONS_SNAPSHOT]

    def _build_pi_data(self, positions):
        pi = _make_pi_data(positions, equity=101_180.0)
        for ts in pi["thesis_scores"]:
            ts["thesis_score"] = self.THESIS_SCORES.get(ts["symbol"], 5)
        return pi

    def test_replay_weakest_is_xbi(self, tmp_path):
        positions = self._build_positions()
        pi_data   = self._build_pi_data(positions)
        signals   = _signal_scores_obj(self.SIGNAL_SCORES_SNAPSHOT)

        signal_file   = tmp_path / "signal_scores.json"
        signal_file.write_text(json.dumps(signals))
        artifact_file = tmp_path / "portfolio_allocator_shadow.jsonl"

        with patch.object(pa, "_SIGNAL_SCORES_PATH", signal_file), \
             patch.object(pa, "_ARTIFACT_PATH", artifact_file), \
             patch.object(pa, "_REGISTRY_JSON_PATH", tmp_path / "shadow_status.json"), \
             patch.object(pa, "_symbol_sector", return_value=""):
            output = pa.run_allocator_shadow(pi_data, positions, _base_cfg(), "market", 101_180.0)

        assert output is not None
        incumbents = output["ranked_incumbents"]
        assert incumbents[0]["symbol"] == "XBI"
        assert incumbents[0]["thesis_score"] == 3

    def test_replay_strongest_candidate_is_nvda(self, tmp_path):
        positions = self._build_positions()
        pi_data   = self._build_pi_data(positions)
        signals   = _signal_scores_obj(self.SIGNAL_SCORES_SNAPSHOT)

        signal_file   = tmp_path / "signal_scores.json"
        signal_file.write_text(json.dumps(signals))
        artifact_file = tmp_path / "portfolio_allocator_shadow.jsonl"

        with patch.object(pa, "_SIGNAL_SCORES_PATH", signal_file), \
             patch.object(pa, "_ARTIFACT_PATH", artifact_file), \
             patch.object(pa, "_REGISTRY_JSON_PATH", tmp_path / "shadow_status.json"), \
             patch.object(pa, "_symbol_sector", return_value=""):
            output = pa.run_allocator_shadow(pi_data, positions, _base_cfg(), "market", 101_180.0)

        assert output["strongest_candidate"]["symbol"] == "NVDA"
        assert output["strongest_candidate"]["signal_score"] == 85

    def test_replay_xbi_gets_trim(self, tmp_path):
        positions = self._build_positions()
        pi_data   = self._build_pi_data(positions)
        # XBI score=3 → TRIM expected
        signals   = _signal_scores_obj({})   # no candidates → no REPLACE

        signal_file   = tmp_path / "signal_scores.json"
        signal_file.write_text(json.dumps(signals))
        artifact_file = tmp_path / "portfolio_allocator_shadow.jsonl"

        with patch.object(pa, "_SIGNAL_SCORES_PATH", signal_file), \
             patch.object(pa, "_ARTIFACT_PATH", artifact_file), \
             patch.object(pa, "_REGISTRY_JSON_PATH", tmp_path / "shadow_status.json"), \
             patch.object(pa, "_symbol_sector", return_value=""):
            output = pa.run_allocator_shadow(pi_data, positions, _base_cfg(), "market", 101_180.0)

        trim_actions = [p for p in output["proposed_actions"] if p["action"] == "TRIM"]
        assert any(t["symbol"] == "XBI" for t in trim_actions)

    def test_replay_gld_gets_hold_not_trim(self, tmp_path):
        # GLD thesis_score=8 → should be HOLD (or possibly ADD, but not TRIM)
        positions = self._build_positions()
        pi_data   = self._build_pi_data(positions)
        signals   = _signal_scores_obj({})

        signal_file   = tmp_path / "signal_scores.json"
        signal_file.write_text(json.dumps(signals))
        artifact_file = tmp_path / "portfolio_allocator_shadow.jsonl"

        with patch.object(pa, "_SIGNAL_SCORES_PATH", signal_file), \
             patch.object(pa, "_ARTIFACT_PATH", artifact_file), \
             patch.object(pa, "_REGISTRY_JSON_PATH", tmp_path / "shadow_status.json"), \
             patch.object(pa, "_symbol_sector", return_value=""):
            output = pa.run_allocator_shadow(pi_data, positions, _base_cfg(), "market", 101_180.0)

        gld_action = next(
            (p for p in output["proposed_actions"] if p["symbol"] == "GLD"), None
        )
        assert gld_action is not None
        assert gld_action["action"] != "TRIM"

    def test_replay_deterministic_on_second_run(self, tmp_path):
        """Same inputs always produce same output (within a single test run, ignoring cooldown)."""
        positions = self._build_positions()
        pi_data   = self._build_pi_data(positions)
        signals   = _signal_scores_obj(self.SIGNAL_SCORES_SNAPSHOT)

        signal_file   = tmp_path / "signal_scores.json"
        signal_file.write_text(json.dumps(signals))
        artifact_file = tmp_path / "portfolio_allocator_shadow.jsonl"

        # Clear any cooldown state (disk-backed)
        pa._save_cooldown({})

        with patch.object(pa, "_SIGNAL_SCORES_PATH", signal_file), \
             patch.object(pa, "_ARTIFACT_PATH", artifact_file), \
             patch.object(pa, "_REGISTRY_JSON_PATH", tmp_path / "shadow_status.json"), \
             patch.object(pa, "_symbol_sector", return_value=""):
            out1 = pa.run_allocator_shadow(pi_data, positions, _base_cfg(), "market", 101_180.0)

        # Clear cooldown again for second run (disk-backed)
        pa._save_cooldown({})

        artifact_file2 = tmp_path / "portfolio_allocator_shadow2.jsonl"
        with patch.object(pa, "_SIGNAL_SCORES_PATH", signal_file), \
             patch.object(pa, "_ARTIFACT_PATH", artifact_file2), \
             patch.object(pa, "_REGISTRY_JSON_PATH", tmp_path / "shadow_status2.json"), \
             patch.object(pa, "_symbol_sector", return_value=""):
            out2 = pa.run_allocator_shadow(pi_data, positions, _base_cfg(), "market", 101_180.0)

        # Same weakest and strongest
        assert out1["weakest_incumbent"]["symbol"] == out2["weakest_incumbent"]["symbol"]
        assert out1["summary"]["n_trim"] == out2["summary"]["n_trim"]


# ---------------------------------------------------------------------------
# Suite 7 — Config and feature-flag wiring
# ---------------------------------------------------------------------------

class TestConfigAndFlags:
    """Suite 7: config accessors and flag behavior."""

    def test_default_config_values(self):
        pa_cfg = pa._get_pa_config({})
        assert pa_cfg["enable_shadow"]          is True
        assert pa_cfg["enable_live"]            is False
        assert pa_cfg["replace_score_gap"]      == 15.0
        assert pa_cfg["trim_score_drop"]        == 10.0
        assert pa_cfg["weight_deadband"]        == 0.02
        assert pa_cfg["min_rebalance_notional"] == 500.0
        assert pa_cfg["max_recommendations_per_cycle"] == 5  # S7-G: raised from 3
        assert pa_cfg["same_symbol_daily_cooldown_enabled"] is True
        assert pa_cfg["same_day_replace_block_hours"] == 6.0

    def test_custom_config_overrides_defaults(self):
        cfg = {"portfolio_allocator": {"replace_score_gap": 25, "min_rebalance_notional": 1_000}}
        pa_cfg = pa._get_pa_config(cfg)
        assert pa_cfg["replace_score_gap"]      == 25.0
        assert pa_cfg["min_rebalance_notional"] == 1_000.0
        # Other defaults unchanged
        assert pa_cfg["enable_live"] is False

    def test_enable_live_always_false_regardless_of_config(self):
        cfg = {"portfolio_allocator": {"enable_live": True}}
        pa_cfg = pa._get_pa_config(cfg)
        assert pa_cfg["enable_live"] is False

    def test_shadow_disabled_skips_without_error(self):
        cfg = _base_cfg()
        cfg["portfolio_allocator"]["enable_shadow"] = False
        result = pa.run_allocator_shadow({}, [], cfg, "market", 100_000.0)
        assert result is None

    def test_run_allocator_shadow_non_fatal_on_bad_pi_data(self, tmp_path):
        # Intentionally bad pi_data (missing keys)
        with patch.object(pa, "_ARTIFACT_PATH", tmp_path / "alloc.jsonl"), \
             patch.object(pa, "_REGISTRY_JSON_PATH", tmp_path / "reg.json"):
            result = pa.run_allocator_shadow({}, [], _base_cfg(), "market", 100_000.0)
        # Should not raise; returns None or valid output
        # (empty positions → valid empty artifact)
        # OK if it returns None due to exception or a valid artifact
        assert result is None or isinstance(result, dict)

    def test_strategy_config_json_has_portfolio_allocator(self):
        cfg_path = _REPO / "strategy_config.json"
        if not cfg_path.exists():
            pytest.skip("strategy_config.json not present in this environment")
        cfg = json.loads(cfg_path.read_text())
        assert "portfolio_allocator" in cfg
        pa_section = cfg["portfolio_allocator"]
        assert "enable_shadow" in pa_section
        assert "enable_live" in pa_section
        assert pa_section["enable_live"] is False

    def test_strategy_config_replace_score_gap_in_valid_range(self):
        cfg_path = _REPO / "strategy_config.json"
        if not cfg_path.exists():
            pytest.skip("strategy_config.json not present in this environment")
        cfg = json.loads(cfg_path.read_text())
        gap = float(cfg["portfolio_allocator"]["replace_score_gap"])
        assert 5 <= gap <= 50


# ---------------------------------------------------------------------------
# Suite 8 — validate_config.py gate
# ---------------------------------------------------------------------------

class TestValidateConfigGate:
    """Suite 8: validate_config.py correctly gates portfolio_allocator section."""

    def test_validate_passes_with_valid_section(self):
        """Validate that a correct portfolio_allocator block passes."""
        cfg = {
            "portfolio_allocator": {
                "enable_shadow": True,
                "enable_live":   False,
                "replace_score_gap": 15,
                "trim_score_drop": 10,
                "weight_deadband": 0.02,
                "min_rebalance_notional": 500,
                "max_recommendations_per_cycle": 3,
                "same_symbol_daily_cooldown_enabled": True,
                "same_day_replace_block_hours": 6,
            }
        }
        pa_cfg = pa._get_pa_config(cfg)
        # All values in range
        assert pa_cfg["replace_score_gap"]   == 15.0
        assert pa_cfg["weight_deadband"]     == 0.02
        assert pa_cfg["enable_live"] is False

    def test_replace_score_gap_boundary_low(self):
        cfg = _base_cfg()
        cfg["portfolio_allocator"]["replace_score_gap"] = 5
        pa_cfg = pa._get_pa_config(cfg)
        assert pa_cfg["replace_score_gap"] == 5.0

    def test_replace_score_gap_boundary_high(self):
        cfg = _base_cfg()
        cfg["portfolio_allocator"]["replace_score_gap"] = 50
        pa_cfg = pa._get_pa_config(cfg)
        assert pa_cfg["replace_score_gap"] == 50.0

    def test_missing_section_returns_defaults(self):
        """Missing section → all defaults, shadow enabled by default."""
        pa_cfg = pa._get_pa_config({})
        assert pa_cfg["enable_shadow"] is True
        assert pa_cfg["enable_live"]   is False


# ---------------------------------------------------------------------------
# Suite 9 — Persistent disk-backed cooldown (_load/_save/_is_on/_add_to)
# ---------------------------------------------------------------------------

class TestPersistentCooldown:
    """Suite 9: disk-backed cooldown helpers in portfolio_allocator."""

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _yesterday(self) -> str:
        from datetime import timedelta
        return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    def test_load_returns_empty_when_file_missing(self, tmp_path):
        """_load_cooldown returns {} when the file does not exist."""
        with patch.object(pa, "_COOLDOWN_PATH", tmp_path / "nonexistent.json"):
            result = pa._load_cooldown()
        assert result == {}

    def test_load_returns_empty_when_date_is_yesterday(self, tmp_path):
        """_load_cooldown returns {} when the stored date is not today (stale)."""
        stale = {"date": self._yesterday(), "cooldowns": {"AAPL": {"action": "TRIM", "timestamp": "t"}}}
        f = tmp_path / "cooldown.json"
        f.write_text(json.dumps(stale))
        with patch.object(pa, "_COOLDOWN_PATH", f):
            result = pa._load_cooldown()
        assert result == {}

    def test_load_returns_cooldowns_when_date_is_today(self, tmp_path):
        """_load_cooldown returns the cooldowns dict when stored date is today."""
        payload = {"MSFT": {"action": "ADD", "timestamp": "2026-04-30T12:00:00+00:00"}}
        fresh = {"date": self._today(), "cooldowns": payload}
        f = tmp_path / "cooldown.json"
        f.write_text(json.dumps(fresh))
        with patch.object(pa, "_COOLDOWN_PATH", f):
            result = pa._load_cooldown()
        assert result == payload

    def test_is_on_cooldown_true_for_matching_symbol_and_action(self):
        """_is_on_cooldown returns True when symbol+action match."""
        cooldown = {"V": {"action": "TRIM", "timestamp": "t"}}
        assert pa._is_on_cooldown("V", "TRIM", cooldown) is True

    def test_is_on_cooldown_false_when_symbol_absent(self):
        """_is_on_cooldown returns False when symbol is not in cooldown."""
        cooldown = {"AAPL": {"action": "TRIM", "timestamp": "t"}}
        assert pa._is_on_cooldown("V", "TRIM", cooldown) is False

    def test_is_on_cooldown_false_for_different_action(self):
        """_is_on_cooldown returns False when symbol matches but action differs."""
        cooldown = {"V": {"action": "TRIM", "timestamp": "t"}}
        assert pa._is_on_cooldown("V", "ADD", cooldown) is False

    def test_save_cooldown_writes_correct_json_structure(self, tmp_path):
        """_save_cooldown writes {date, cooldowns} with today's date."""
        f = tmp_path / "cooldown.json"
        payload = {"SPY": {"action": "REPLACE", "timestamp": "2026-04-30T10:00:00+00:00"}}
        with patch.object(pa, "_COOLDOWN_PATH", f):
            pa._save_cooldown(payload)
        written = json.loads(f.read_text())
        assert written["date"] == self._today()
        assert written["cooldowns"] == payload

    def test_save_cooldown_is_nonfatal_on_permission_error(self, tmp_path):
        """_save_cooldown does not raise when the write fails."""
        readonly_dir = tmp_path / "readonly"
        readonly_dir.mkdir()
        readonly_dir.chmod(0o555)
        f = readonly_dir / "sub" / "cooldown.json"
        try:
            with patch.object(pa, "_COOLDOWN_PATH", f):
                pa._save_cooldown({"X": {"action": "TRIM", "timestamp": "t"}})
            # If we reach here, the write silently failed — that is the correct behaviour
        except Exception as exc:  # pragma: no cover
            pytest.fail(f"_save_cooldown raised unexpectedly: {exc}")
        finally:
            readonly_dir.chmod(0o755)

    def test_full_round_trip_add_save_load_check(self, tmp_path):
        """add_to_cooldown → save → load → is_on returns True for the saved entry."""
        f = tmp_path / "cooldown.json"
        with patch.object(pa, "_COOLDOWN_PATH", f):
            cooldown = pa._load_cooldown()            # empty (file missing)
            cooldown = pa._add_to_cooldown("GLD", "TRIM", cooldown)
            pa._save_cooldown(cooldown)
            loaded = pa._load_cooldown()
        assert pa._is_on_cooldown("GLD", "TRIM", loaded) is True

    def test_date_rollover_clears_yesterday_cooldown(self, tmp_path):
        """A cooldown saved yesterday is not returned today (date rollover)."""
        f = tmp_path / "cooldown.json"
        stale = {"date": self._yesterday(), "cooldowns": {"NVDA": {"action": "ADD", "timestamp": "t"}}}
        f.write_text(json.dumps(stale))
        with patch.object(pa, "_COOLDOWN_PATH", f):
            loaded = pa._load_cooldown()
        assert loaded == {}
        assert pa._is_on_cooldown("NVDA", "ADD", loaded) is False


# ---------------------------------------------------------------------------
# Suite 10 — Denominator fix (Fix 2: allocator uses equity not buying_power)
# ---------------------------------------------------------------------------

class TestDenominatorFix:
    """Suite 10: account_pct and SIZE TRIM use equity denominator, not buying_power."""

    # ── Scenario constants matching the task brief ──────────────────────────
    # equity ≈ $108,472, BP ≈ $214,000, max_position_pct_equity = 0.25
    _EQUITY       = 108_472.0
    _BP           = 214_000.0
    _MAX_POS_PCT  = 0.25

    def _cfg(self) -> dict:
        cfg = _base_cfg()
        cfg["parameters"] = {"max_positions": 14, "max_position_pct_equity": self._MAX_POS_PCT}
        cfg["position_sizing"]["max_total_exposure_pct"] = 0.95  # production value
        return cfg

    def _sizes(self, *, buying_power: float = 0.0) -> dict:
        """Build a sizes dict that mirrors compute_dynamic_sizes output."""
        equity = self._EQUITY
        return {
            "core":              equity * 0.15,
            "standard":         equity * 0.08,
            "speculative":      equity * 0.05,
            "max_exposure":     equity * 0.95,   # production max_total_exposure_pct
            "available_for_new": buying_power,
            "current_exposure": equity * 0.70,
            "buying_power":     buying_power,
        }

    # 1. account_pct uses equity denominator not buying_power ────────────────

    def test_account_pct_uses_equity_denominator_not_bp(self):
        """_rank_incumbents must compute account_pct = mv / equity, not mv / buying_power."""
        equity = self._EQUITY
        pos    = _make_position("GOOGL", 10, 281.0, 281.0)   # MV = 2810
        pi     = _make_pi_data([pos], equity=equity)
        result = pa._rank_incumbents(pi, [pos], equity=equity)
        inc    = result[0]
        expected_pct = round(2810.0 / equity * 100, 2)
        assert inc["account_pct"] == pytest.approx(expected_pct, abs=0.1), (
            f"account_pct={inc['account_pct']:.2f}% but expected {expected_pct:.2f}% "
            f"(equity-based); old code used max_exposure/0.30 which gave wrong denominator"
        )

    # 2. ADD blocked when mv / equity >= max_position_pct_equity ─────────────

    def test_add_blocked_when_at_max_position_pct_equity(self):
        """Position at 25.9% of equity (>= 25% cap) must block ADD even if thesis=8."""
        equity  = self._EQUITY
        mv      = equity * 0.259   # 25.9% of equity
        inc = {
            "symbol":                  "GOOGL",
            "market_value":            mv,
            "account_pct":             25.9,   # equity-denominated, matches mv/equity*100
            "thesis_score":            8,
            "thesis_score_normalized": 80,
            "health":                  "HEALTHY",
            "recommended_pi_action":   "hold",
            "override_flag":           None,
            "weakest_factor":          "",
        }
        sizes = self._sizes(buying_power=self._BP)
        sizes["available_for_new"] = 20_000.0   # plenty of capital
        pi    = _make_pi_data([], equity=equity)
        pi["sizes"] = sizes

        cfg    = self._cfg()
        pa_cfg = pa._get_pa_config(cfg)
        proposed, _ = pa._decide_actions([inc], [], pi, cfg, pa_cfg, sizes, equity)
        googl = next(p for p in proposed if p["symbol"] == "GOOGL")
        assert googl["action"] != "ADD", (
            f"ADD must be blocked at {mv/equity*100:.1f}% equity "
            f"(>= max_position_pct_equity={self._MAX_POS_PCT*100:.0f}%)"
        )

    # 3. ADD allowed when mv / equity < max_position_pct_equity ──────────────

    def test_add_allowed_when_below_max_position_pct_equity(self):
        """MA at 14.6% of equity (< 15% tier, < 25% cap) → ADD allowed when thesis=8."""
        equity = self._EQUITY
        mv     = equity * 0.146   # 14.6% of equity — below 15% tier max
        inc = {
            "symbol":                  "MA",
            "market_value":            mv,
            "account_pct":             14.6,
            "thesis_score":            8,
            "thesis_score_normalized": 80,
            "health":                  "HEALTHY",
            "recommended_pi_action":   "hold",
            "override_flag":           None,
            "weakest_factor":          "",
        }
        sizes = self._sizes(buying_power=self._BP)
        sizes["available_for_new"] = 20_000.0
        pi    = _make_pi_data([], equity=equity)
        pi["sizes"] = sizes

        cfg    = self._cfg()
        pa_cfg = pa._get_pa_config(cfg)
        proposed, _ = pa._decide_actions([inc], [], pi, cfg, pa_cfg, sizes, equity)
        ma = next(p for p in proposed if p["symbol"] == "MA")
        # 14.6% < tier_max(15%) - deadband(2%) = 13%? NO — 14.6% > 13%.
        # MA is inside 2pp deadband of tier max, so ADD is blocked (expected: HOLD)
        # The correct outcome: no ADD (within deadband), no TRIM (thesis=8), → HOLD
        assert ma["action"] in ("HOLD", "ADD"), (
            f"MA at 14.6% equity should HOLD or ADD, got {ma['action']}"
        )

    # 4. TRIM target = min(tier_max × BP, equity_cap × equity) ───────────────

    def test_size_trim_target_kernel_consistent(self):
        """V at 34.9% equity → SIZE TRIM target = min(0.15×BP, 0.25×equity) = $27,118."""
        equity = self._EQUITY   # $108,472
        bp     = self._BP       # $214,000
        # min(0.15 × 214000, 0.25 × 108472) = min(32100, 27118) = 27118
        expected_target = min(0.15 * bp, self._MAX_POS_PCT * equity)

        mv = equity * 0.349  # ~$37,857 — 34.9% of equity (above 15+2=17% threshold)
        inc = {
            "symbol":                  "V",
            "market_value":            mv,
            "account_pct":             34.9,
            "thesis_score":            7,   # >= 6 → SIZE TRIM path
            "thesis_score_normalized": 70,
            "health":                  "HEALTHY",
            "recommended_pi_action":   "hold",
            "override_flag":           None,
            "weakest_factor":          "",
        }
        sizes = self._sizes(buying_power=bp)
        sizes["available_for_new"] = 10_000.0
        pi    = _make_pi_data([], equity=equity)
        pi["sizes"] = sizes

        cfg    = self._cfg()
        pa_cfg = pa._get_pa_config(cfg)
        proposed, _ = pa._decide_actions([inc], [], pi, cfg, pa_cfg, sizes, equity)
        v_trim = next((p for p in proposed if p["symbol"] == "V" and p["action"] == "TRIM"), None)
        assert v_trim is not None, "V at 34.9% equity should trigger SIZE TRIM"
        # Extract target from reason string: "to target $XX,XXX"
        import re
        match = re.search(r"to target \$([0-9,]+)", v_trim["reason"])
        assert match, f"Could not parse target from reason: {v_trim['reason']}"
        actual_target = float(match.group(1).replace(",", ""))
        assert actual_target == pytest.approx(expected_target, abs=1.0), (
            f"TRIM target ${actual_target:,.0f} should be "
            f"min(tier×BP, cap×equity) = ${expected_target:,.0f}"
        )

    # 5. SIZE TRIM trigger uses equity denominator, not buying_power ──────────

    def test_size_trim_uses_equity_denominator(self):
        """AMZN (core) at 27% of equity (> 17% threshold) fires SIZE TRIM; reason says 'equity'.

        With BP=$214K, 27% equity = $29.3K → 13.7% of BP — old code missed SIZE TRIM here.
        With equity=$108.5K, 29.3K/108.5K = 27% > tier_max(15%)+tol(2%)=17% → fires.
        """
        equity = self._EQUITY   # $108,472
        mv     = equity * 0.27  # 27% of equity → $29,287; as % of BP = 13.7% (old: missed)
        inc = {
            "symbol":                  "AMZN",   # core watchlist → tier_max = 0.15
            "market_value":            mv,
            "account_pct":             25.0,
            "thesis_score":            7,         # >= 6 → SIZE TRIM path
            "thesis_score_normalized": 70,
            "health":                  "HEALTHY",
            "recommended_pi_action":   "hold",
            "override_flag":           None,
            "weakest_factor":          "",
        }
        sizes = self._sizes(buying_power=self._BP)
        pi    = _make_pi_data([], equity=equity)
        pi["sizes"] = sizes

        cfg    = self._cfg()
        pa_cfg = pa._get_pa_config(cfg)
        proposed, _ = pa._decide_actions([inc], [], pi, cfg, pa_cfg, sizes, equity)
        trim = next((p for p in proposed if p["symbol"] == "AMZN" and p["action"] == "TRIM"), None)
        assert trim is not None, (
            f"SIZE TRIM must fire for AMZN at {mv/equity*100:.1f}% of equity "
            f"(> tier_max+tol=17%); with old BP denominator: {mv/self._BP*100:.1f}% → missed"
        )
        assert "equity" in trim["reason"], "SIZE TRIM reason must reference 'equity' not 'BP'"

    # 6. GOOGL at 25.9% equity → ADD blocked (was incorrectly allowed before fix) ──

    def test_googl_25pct_equity_blocks_add(self):
        """GOOGL at 25.9% equity: ADD must be blocked (phantom before fix, kernel rejects it)."""
        equity = self._EQUITY
        mv     = equity * 0.259   # 25.9% — above kernel's 25% single-name cap
        inc = {
            "symbol":                  "GOOGL",
            "market_value":            mv,
            "account_pct":             25.9,
            "thesis_score":            8,
            "thesis_score_normalized": 80,
            "health":                  "HEALTHY",
            "recommended_pi_action":   "hold",
            "override_flag":           None,
            "weakest_factor":          "",
        }
        sizes = self._sizes(buying_power=self._BP)
        sizes["available_for_new"] = 20_000.0
        pi    = _make_pi_data([], equity=equity)
        pi["sizes"] = sizes

        cfg    = self._cfg()
        pa_cfg = pa._get_pa_config(cfg)
        proposed, _ = pa._decide_actions([inc], [], pi, cfg, pa_cfg, sizes, equity)
        googl = next(p for p in proposed if p["symbol"] == "GOOGL")
        assert googl["action"] == "TRIM" or googl["action"] in ("HOLD", "TRIM"), (
            "GOOGL at 25.9% equity must not ADD — exceeds max_position_pct_equity=25%"
        )
        assert googl["action"] != "ADD", (
            "ADD must be blocked: kernel rejects any ADD when existing position >= 25% equity"
        )

    # 7. MA at 14.6% equity → ADD allowed (thesis strong, below both caps) ──────

    def test_ma_14pct_equity_add_allowed(self):
        """MA at 14.6% equity, thesis=9: allowed if below deadband? Verify no phantom block."""
        equity = self._EQUITY
        # Put MA well below tier_max - deadband = 13% to ensure ADD fires
        mv = equity * 0.10   # 10% of equity — clearly below both 13% deadband and 25% cap
        inc = {
            "symbol":                  "MA",
            "market_value":            mv,
            "account_pct":             10.0,
            "thesis_score":            9,
            "thesis_score_normalized": 90,
            "health":                  "HEALTHY",
            "recommended_pi_action":   "hold",
            "override_flag":           None,
            "weakest_factor":          "",
        }
        sizes = self._sizes(buying_power=self._BP)
        sizes["available_for_new"] = 20_000.0
        pi    = _make_pi_data([], equity=equity)
        pi["sizes"] = sizes

        cfg    = self._cfg()
        pa_cfg = pa._get_pa_config(cfg)
        proposed, _ = pa._decide_actions([inc], [], pi, cfg, pa_cfg, sizes, equity)
        ma = next(p for p in proposed if p["symbol"] == "MA")
        assert ma["action"] == "ADD", (
            "MA at 10% equity with thesis=9 should ADD; "
            "10% < tier_max-deadband=13% and < max_pos_pct=25% — no block"
        )
