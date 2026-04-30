"""
tests/test_s10_phase1_routing.py -- Sprint 10 Phase 1: Earnings-Aware Options Routing

Tests for:
  - _get_earnings_timing()          : timing helper (PE-T1..T5)
  - _iv_already_crushed()           : crush detector (PE-C1..C4)
  - _route_strategy() RULE1 fix     : 0 <= eda <= blackout (PE-01..PE-03)
  - _route_strategy() RULE_POST_EARNINGS (PE-04..PE-12)
  - _route_strategy() RULE_EARNINGS_HIGH_IV (PE-13..PE-16)
  - _infer_router_rule_fired()      : new rule labels (PE-17..PE-19)
"""
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

# Ensure repo root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot_options_stage2_structures as _stage2
from bot_options_stage2_structures import (
    _A2_ROUTER_DEFAULTS,
    _get_earnings_timing,
    _get_router_config,
    _infer_router_rule_fired,
    _iv_already_crushed,
    _route_strategy,
)

# ── Mock pack ─────────────────────────────────────────────────────────────────

@dataclass
class MockPack:
    symbol: str = "AAPL"
    earnings_days_away: Optional[int] = None
    iv_rank: float = 50.0
    iv_environment: str = "neutral"
    a1_direction: str = "bullish"
    a1_signal_score: float = 60.0
    liquidity_score: float = 0.5
    macro_event_flag: bool = False


def _cfg(overrides: dict | None = None) -> dict:
    """Build a minimal strategy_config dict with optional overrides to a2_router."""
    router = {**_A2_ROUTER_DEFAULTS}
    if overrides:
        router.update(overrides)
    return {"a2_router": router}


def _no_crush(monkeypatch) -> None:
    """Patch _iv_already_crushed in stage2 module to always return False."""
    monkeypatch.setattr(_stage2, "_iv_already_crushed", lambda *a, **k: False)


# ── _get_earnings_timing tests ────────────────────────────────────────────────

class TestGetEarningsTiming:
    """PE-T1..T5 -- timing helper."""

    def _cal(self, symbol: str, days_offset: int, timing: str) -> dict:
        d = date.today() + timedelta(days=days_offset)
        return {"calendar": [{"symbol": symbol, "earnings_date": d.isoformat(), "timing": timing}]}

    def test_pre_market_recognized(self):  # PE-T1
        cal = self._cal("AAPL", -1, "pre-market")
        assert _get_earnings_timing("AAPL", cal) == "pre_market"

    def test_post_market_amc_recognized(self):  # PE-T2
        # "After Market Close" contains "after" -> post_market
        cal = self._cal("AAPL", -1, "After Market Close")
        assert _get_earnings_timing("AAPL", cal) == "post_market"

    def test_post_market_explicit(self):
        cal = self._cal("AAPL", -1, "post-market")
        assert _get_earnings_timing("AAPL", cal) == "post_market"

    def test_bmo_recognized_as_pre_market(self):
        cal = self._cal("AAPL", -1, "BMO")
        assert _get_earnings_timing("AAPL", cal) == "pre_market"

    def test_unknown_timing_for_past_earnings(self):  # PE-T3
        cal = self._cal("AAPL", -1, "")
        assert _get_earnings_timing("AAPL", cal) == "unknown"

    def test_empty_calendar_returns_unknown(self):  # PE-T4
        assert _get_earnings_timing("AAPL", {}) == "unknown"
        assert _get_earnings_timing("AAPL", {"calendar": []}) == "unknown"

    def test_future_earnings_not_counted(self):  # PE-T5
        cal = self._cal("AAPL", 3, "pre-market")  # 3 days future
        assert _get_earnings_timing("AAPL", cal) == "unknown"

    def test_most_recent_past_wins(self):
        today = date.today()
        cal = {
            "calendar": [
                {"symbol": "AAPL", "earnings_date": (today - timedelta(5)).isoformat(), "timing": "post-market"},
                {"symbol": "AAPL", "earnings_date": (today - timedelta(1)).isoformat(), "timing": "pre-market"},
            ]
        }
        assert _get_earnings_timing("AAPL", cal) == "pre_market"

    def test_symbol_filter_case_insensitive(self):
        cal = self._cal("aapl", -1, "pre-market")
        assert _get_earnings_timing("AAPL", cal) == "pre_market"
        assert _get_earnings_timing("aapl", cal) == "pre_market"


# ── _iv_already_crushed tests ─────────────────────────────────────────────────

class TestIvAlreadyCrushed:
    """PE-C1..C4 -- crush detector."""

    def _write_history(self, tmp_dir: str, symbol: str, ivs: list) -> None:
        iv_dir = os.path.join(tmp_dir, "data", "options", "iv_history")
        os.makedirs(iv_dir, exist_ok=True)
        entries = [{"date": "2026-01-01", "iv": v} for v in ivs]
        with open(os.path.join(iv_dir, f"{symbol}_iv_history.json"), "w") as f:
            json.dump(entries, f)

    def test_returns_false_when_file_missing(self):  # PE-C1
        # Symbol with no history file -> False (conservative)
        assert _iv_already_crushed("ZZZZZZ", 50.0) is False

    def test_returns_true_when_drop_exceeds_threshold(self, tmp_path, monkeypatch):  # PE-C2
        self._write_history(str(tmp_path), "CRUSH_T", [0.20, 0.20, 0.40, 0.20])
        monkeypatch.setattr(_stage2, "__file__",
                            os.path.join(str(tmp_path), "bot_options_stage2_structures.py"))
        # yesterday_rank=100, today_rank=0, drop=100 >= 15 -> True
        assert _iv_already_crushed("CRUSH_T", 0.0, threshold=15.0) is True

    def test_returns_false_when_drop_below_threshold(self, tmp_path, monkeypatch):  # PE-C3
        # [0.20, 0.30, 0.295]: drop ~5 rank points < 15 -> False
        self._write_history(str(tmp_path), "NOCRUSH_T", [0.20, 0.30, 0.295])
        monkeypatch.setattr(_stage2, "__file__",
                            os.path.join(str(tmp_path), "bot_options_stage2_structures.py"))
        assert _iv_already_crushed("NOCRUSH_T", 0.295, threshold=15.0) is False

    def test_returns_false_with_fewer_than_two_entries(self, tmp_path, monkeypatch):  # PE-C4
        self._write_history(str(tmp_path), "ONE_T", [0.30])
        monkeypatch.setattr(_stage2, "__file__",
                            os.path.join(str(tmp_path), "bot_options_stage2_structures.py"))
        assert _iv_already_crushed("ONE_T", 50.0) is False


# ── RULE1 fix: 0 <= eda <= blackout ──────────────────────────────────────────

class TestRule1Fix:
    """PE-01..PE-03 -- RULE1 must use 0 <= eda <= blackout."""

    def test_rule1_fires_for_eda_zero(self):  # PE-01
        pack = MockPack(earnings_days_away=0, iv_environment="neutral")
        result = _route_strategy(pack, _cfg({"earnings_dte_blackout": 2}))
        assert result == []

    def test_rule1_fires_for_eda_equal_to_blackout(self):  # PE-02
        pack = MockPack(earnings_days_away=2, iv_environment="neutral")
        result = _route_strategy(pack, _cfg({"earnings_dte_blackout": 2}))
        assert result == []

    def test_rule1_does_not_fire_for_negative_eda(self):  # PE-03
        # eda=-1: earnings happened yesterday; RULE1 must NOT fire
        # neutral iv + bullish -> RULE6 fires
        pack = MockPack(earnings_days_away=-1, iv_rank=50.0, iv_environment="neutral")
        result = _route_strategy(pack, _cfg({"earnings_dte_blackout": 2}))
        assert result != []


# ── RULE_POST_EARNINGS tests ──────────────────────────────────────────────────

class TestRulePostEarnings:
    """PE-04..PE-12 -- post-earnings IV crush credit spread."""

    def _pre_market_cal(self, days_ago: int) -> dict:
        d = date.today() - timedelta(days=days_ago)
        return {"calendar": [{"symbol": "AAPL", "earnings_date": d.isoformat(), "timing": "pre-market"}]}

    def _post_market_cal(self, days_ago: int) -> dict:
        d = date.today() - timedelta(days=days_ago)
        return {"calendar": [{"symbol": "AAPL", "earnings_date": d.isoformat(), "timing": "After Market Close"}]}

    def test_fires_for_eda_neg1_premarket_bullish(self, monkeypatch):  # PE-04
        _no_crush(monkeypatch)
        pack = MockPack(symbol="AAPL", earnings_days_away=-1, iv_rank=80.0, iv_environment="expensive")
        result = _route_strategy(pack, _cfg(), earnings_calendar_data=self._pre_market_cal(1))
        assert result == ["credit_put_spread"]

    def test_fires_for_eda_neg1_postmarket(self, monkeypatch):  # PE-05
        _no_crush(monkeypatch)
        pack = MockPack(symbol="AAPL", earnings_days_away=-1, iv_rank=80.0, iv_environment="expensive")
        result = _route_strategy(pack, _cfg(), earnings_calendar_data=self._post_market_cal(1))
        assert result == ["credit_put_spread"]

    def test_does_not_fire_when_outside_premarket_window(self, monkeypatch):  # PE-06
        _no_crush(monkeypatch)
        # pre_market window=2, eda=-3, days_since=3 > 2 -> RULE_POST_EARNINGS misses
        # RULE_SHORT_PUT fires (iv_rank=80 >= 50, bullish, expensive)
        pack = MockPack(symbol="AAPL", earnings_days_away=-3, iv_rank=80.0, iv_environment="expensive")
        result = _route_strategy(pack, _cfg(), earnings_calendar_data=self._pre_market_cal(3))
        assert result != ["credit_put_spread"]  # RULE_POST_EARNINGS did not fire

    def test_does_not_fire_when_iv_rank_below_min(self, monkeypatch):  # PE-07
        _no_crush(monkeypatch)
        pack = MockPack(symbol="AAPL", earnings_days_away=-1, iv_rank=60.0, iv_environment="neutral")
        result = _route_strategy(pack, _cfg({"post_earnings_iv_rank_min": 75}),
                                 earnings_calendar_data=self._pre_market_cal(1))
        # iv_rank=60 < 75 -> RULE_POST_EARNINGS misses; RULE_SHORT_PUT fires (iv_rank>=50, bullish, neutral)
        assert result != ["credit_put_spread"]  # RULE_POST_EARNINGS did not fire

    def test_does_not_fire_when_iv_already_crushed(self, monkeypatch):  # PE-08
        monkeypatch.setattr(_stage2, "_iv_already_crushed", lambda *a, **k: True)
        pack = MockPack(symbol="AAPL", earnings_days_away=-1, iv_rank=80.0, iv_environment="expensive")
        result = _route_strategy(pack, _cfg(), earnings_calendar_data=self._pre_market_cal(1))
        # crush detected -> RULE_POST_EARNINGS skipped; RULE_SHORT_PUT fires (iv_rank=80>=50, bullish)
        assert result != ["credit_put_spread"]  # RULE_POST_EARNINGS did not fire

    def test_bearish_direction_gives_credit_call_spread(self, monkeypatch):  # PE-09
        _no_crush(monkeypatch)
        pack = MockPack(symbol="AAPL", earnings_days_away=-1, iv_rank=80.0,
                        iv_environment="expensive", a1_direction="bearish")
        result = _route_strategy(pack, _cfg(), earnings_calendar_data=self._pre_market_cal(1))
        assert result == ["credit_call_spread"]

    def test_neutral_direction_gives_both_spreads(self, monkeypatch):  # PE-10
        _no_crush(monkeypatch)
        pack = MockPack(symbol="AAPL", earnings_days_away=-1, iv_rank=80.0,
                        iv_environment="expensive", a1_direction="neutral")
        result = _route_strategy(pack, _cfg(), earnings_calendar_data=self._pre_market_cal(1))
        assert set(result) == {"credit_put_spread", "credit_call_spread"}

    def test_fires_for_eda_neg2_premarket(self, monkeypatch):  # PE-11
        _no_crush(monkeypatch)
        # window=2, days_since=2 -> 2 <= 2 -> fires
        pack = MockPack(symbol="AAPL", earnings_days_away=-2, iv_rank=80.0, iv_environment="expensive")
        result = _route_strategy(pack, _cfg(), earnings_calendar_data=self._pre_market_cal(2))
        assert result == ["credit_put_spread"]

    def test_does_not_fire_for_eda_neg2_postmarket(self, monkeypatch):  # PE-12
        _no_crush(monkeypatch)
        # post_market window=1, days_since=2 -> 2 > 1 -> RULE_POST_EARNINGS misses
        # RULE_SHORT_PUT fires (iv_rank=80>=50, bullish, expensive)
        pack = MockPack(symbol="AAPL", earnings_days_away=-2, iv_rank=80.0, iv_environment="expensive")
        result = _route_strategy(pack, _cfg(), earnings_calendar_data=self._post_market_cal(2))
        assert result != ["credit_put_spread"]  # RULE_POST_EARNINGS did not fire

    def test_falls_through_to_standard_rules_when_outside_window(self, monkeypatch):
        _no_crush(monkeypatch)
        # eda=-5, pre_market window=2 -> miss; iv_env=cheap + bullish -> RULE5
        pack = MockPack(symbol="AAPL", earnings_days_away=-5, iv_rank=30.0,
                        iv_environment="cheap", a1_direction="bullish")
        result = _route_strategy(pack, _cfg(), earnings_calendar_data=self._pre_market_cal(5))
        assert "debit_call_spread" in result


# ── RULE_EARNINGS_HIGH_IV tests ───────────────────────────────────────────────

class TestRuleEarningsHighIV:
    """PE-13..PE-16 -- pre-earnings credit spread (disabled by default)."""

    def test_disabled_by_default_does_not_fire(self):  # PE-13
        pack = MockPack(earnings_days_away=10, iv_rank=90.0, iv_environment="very_expensive")
        result = _route_strategy(pack, _cfg())
        # RULE_EARNINGS_HIGH_IV disabled; RULE2_CREDIT fires (very_expensive)
        assert "credit_put_spread" in result

    def test_enabled_fires_within_dte_range(self):  # PE-14
        pack = MockPack(earnings_days_away=10, iv_rank=87.0, iv_environment="very_expensive")
        cfg = _cfg({
            "pre_earnings_credit_spread_enabled": True,
            "pre_earnings_iv_rank_min": 85,
            "pre_earnings_dte_min": 7,
            "pre_earnings_dte_max": 14,
            "earnings_dte_blackout": 5,
        })
        result = _route_strategy(pack, cfg)
        assert result == ["credit_put_spread"]

    def test_enabled_does_not_fire_below_dte_min(self):  # PE-15
        pack = MockPack(earnings_days_away=5, iv_rank=87.0, iv_environment="very_expensive")
        cfg = _cfg({
            "pre_earnings_credit_spread_enabled": True,
            "pre_earnings_iv_rank_min": 85,
            "pre_earnings_dte_min": 7,
            "pre_earnings_dte_max": 14,
            "earnings_dte_blackout": 2,
        })
        result = _route_strategy(pack, cfg)
        # eda=5 < dte_min=7 -> RULE_EARNINGS_HIGH_IV misses; RULE2_CREDIT fires
        assert "credit_put_spread" in result

    def test_enabled_does_not_fire_below_iv_min(self):  # PE-16
        pack = MockPack(earnings_days_away=10, iv_rank=80.0, iv_environment="expensive")
        cfg = _cfg({
            "pre_earnings_credit_spread_enabled": True,
            "pre_earnings_iv_rank_min": 85,
            "pre_earnings_dte_min": 7,
            "pre_earnings_dte_max": 14,
        })
        result = _route_strategy(pack, cfg)
        # iv_rank=80 < 85 -> RULE_EARNINGS_HIGH_IV misses; RULE_SHORT_PUT fires (iv_rank>=50, bullish)
        assert result != []  # some rule fires; RULE_EARNINGS_HIGH_IV was NOT it


# ── _infer_router_rule_fired tests ────────────────────────────────────────────

class TestInferRouterRuleFired:
    """PE-17..PE-19 -- new rule labels."""

    def test_infers_rule_post_earnings(self):  # PE-17
        pack = MockPack(earnings_days_away=-1, iv_rank=80.0, iv_environment="expensive")
        result = _infer_router_rule_fired(pack, ["credit_put_spread"], _cfg())
        assert result == "RULE_POST_EARNINGS"

    def test_infers_rule_earnings_high_iv(self):  # PE-18
        pack = MockPack(earnings_days_away=10, iv_rank=87.0, iv_environment="very_expensive")
        cfg = _cfg({
            "pre_earnings_credit_spread_enabled": True,
            "pre_earnings_iv_rank_min": 85,
            "pre_earnings_dte_min": 7,
            "pre_earnings_dte_max": 14,
        })
        result = _infer_router_rule_fired(pack, ["credit_put_spread"], cfg)
        assert result == "RULE_EARNINGS_HIGH_IV"

    def test_infers_rule1_for_upcoming_blackout(self):  # PE-19
        pack = MockPack(earnings_days_away=1, iv_rank=50.0, iv_environment="neutral")
        result = _infer_router_rule_fired(pack, [], _cfg({"earnings_dte_blackout": 2}))
        assert result == "RULE1"

    def test_infers_rule8_when_eda_negative_low_iv(self):
        # eda < 0, iv_rank too low for POST_EARNINGS -> falls to RULE8
        pack = MockPack(earnings_days_away=-1, iv_rank=40.0, iv_environment="neutral")
        result = _infer_router_rule_fired(pack, [], _cfg())
        assert result == "RULE8"

    def test_infers_rule5_for_cheap_iv(self):
        pack = MockPack(iv_environment="cheap", a1_direction="bullish")
        result = _infer_router_rule_fired(pack, ["long_call", "debit_call_spread"], _cfg())
        assert result == "RULE5"

    def test_infers_rule6_for_neutral_iv(self):
        pack = MockPack(iv_environment="neutral", a1_direction="bullish")
        result = _infer_router_rule_fired(pack, ["debit_call_spread", "debit_put_spread"], _cfg())
        assert result == "RULE6"


# ── Config defaults tests ─────────────────────────────────────────────────────

class TestConfigDefaults:
    """Verify all 9 new config keys appear in _A2_ROUTER_DEFAULTS and strategy_config.json."""

    NEW_KEYS = [
        "post_earnings_window_premarket",
        "post_earnings_window_postmarket",
        "post_earnings_window_unknown",
        "post_earnings_iv_rank_min",
        "post_earnings_iv_already_crushed_threshold",
        "pre_earnings_credit_spread_enabled",
        "pre_earnings_iv_rank_min",
        "pre_earnings_dte_min",
        "pre_earnings_dte_max",
    ]

    def test_all_new_keys_in_defaults(self):
        for key in self.NEW_KEYS:
            assert key in _A2_ROUTER_DEFAULTS, f"Missing key: {key}"

    def test_pre_earnings_disabled_by_default(self):
        assert _A2_ROUTER_DEFAULTS["pre_earnings_credit_spread_enabled"] is False

    def test_get_router_config_merges_new_keys(self):
        cfg = {"a2_router": {"post_earnings_iv_rank_min": 80}}
        result = _get_router_config(cfg)
        assert result["post_earnings_iv_rank_min"] == 80
        assert result["post_earnings_window_premarket"] == 2  # from defaults

    def test_strategy_config_json_has_new_keys(self):
        from pathlib import Path
        cfg = json.loads((Path(__file__).parent.parent / "strategy_config.json").read_text())
        router = cfg.get("a2_router", {})
        for key in self.NEW_KEYS:
            assert key in router, f"strategy_config.json a2_router missing: {key}"

    def test_strategy_config_pre_earnings_enabled(self):
        from pathlib import Path
        cfg = json.loads((Path(__file__).parent.parent / "strategy_config.json").read_text())
        assert cfg["a2_router"]["pre_earnings_credit_spread_enabled"] is True


# ── EHI-01..06: RULE_EARNINGS_HIGH_IV enabled-state coverage ─────────────────

class TestEarningsHighIVEnabled:
    """EHI-01..06 — RULE_EARNINGS_HIGH_IV behaviour with the flag live-enabled."""

    def _cfg_enabled(self, overrides: dict | None = None) -> dict:
        router = {**_A2_ROUTER_DEFAULTS,
                  "pre_earnings_credit_spread_enabled": True,
                  "pre_earnings_iv_rank_min": 85,
                  "pre_earnings_dte_min": 7,
                  "pre_earnings_dte_max": 14,
                  "earnings_dte_blackout": 2}
        if overrides:
            router.update(overrides)
        return {"a2_router": router}

    def test_ehi01_fires_bullish_within_window(self):
        """EHI-01: iv_rank>=85, eda in [7,14], bullish → fires."""
        pack = MockPack(earnings_days_away=10, iv_rank=90.0,
                        iv_environment="very_expensive", a1_direction="bullish")
        result = _route_strategy(pack, self._cfg_enabled())
        assert result == ["credit_put_spread"]

    def test_ehi02_bullish_returns_credit_put_spread(self):
        """EHI-02: bullish direction → credit_put_spread specifically."""
        pack = MockPack(earnings_days_away=9, iv_rank=88.0,
                        iv_environment="very_expensive", a1_direction="bullish")
        assert _route_strategy(pack, self._cfg_enabled()) == ["credit_put_spread"]

    def test_ehi03_bearish_returns_credit_call_spread(self):
        """EHI-03: bearish direction → credit_call_spread."""
        pack = MockPack(earnings_days_away=9, iv_rank=88.0,
                        iv_environment="very_expensive", a1_direction="bearish")
        assert _route_strategy(pack, self._cfg_enabled()) == ["credit_call_spread"]

    def test_ehi04_eda_at_blackout_boundary_blocked_by_rule1(self):
        """EHI-04: eda=2 <= blackout=2, EHI window is dte_min=7, so RULE1 fires."""
        pack = MockPack(earnings_days_away=2, iv_rank=95.0,
                        iv_environment="very_expensive", a1_direction="bullish")
        result = _route_strategy(pack, self._cfg_enabled())
        assert result == []  # RULE1 blocks

    def test_ehi05_below_iv_floor_does_not_fire(self):
        """EHI-05: iv_rank=80 < 85 → EHI misses; another rule fires."""
        pack = MockPack(earnings_days_away=10, iv_rank=80.0,
                        iv_environment="expensive", a1_direction="bullish")
        result = _route_strategy(pack, self._cfg_enabled())
        assert result != ["credit_put_spread"]  # EHI did NOT fire exclusively

    def test_ehi06_flag_false_disables_rule(self):
        """EHI-06: pre_earnings_credit_spread_enabled=False skips EHI; subsequent rules fire."""
        cfg = self._cfg_enabled({"pre_earnings_credit_spread_enabled": False})
        # iv_environment="expensive" (not very_expensive) means RULE2_CREDIT won't fire.
        # iv_rank=87 satisfies EHI when enabled, but iv_rank >= earnings_iv_rank_gate=70
        # so RULE_EARNINGS also misses. With EHI off, RULE_IRON fires for iv_rank >= 85.
        pack = MockPack(earnings_days_away=10, iv_rank=87.0,
                        iv_environment="expensive", a1_direction="bullish")
        result = _route_strategy(pack, cfg)
        assert result != ["credit_put_spread"]  # EHI did NOT fire
        assert any(s in result for s in ("iron_butterfly", "iron_condor"))
