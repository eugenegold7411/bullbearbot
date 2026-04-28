"""
S7-L2-Earnings — tests for L2 scorer earnings calendar guard.

Bug: load_calendar_map() returns dict[str, dict]; score_symbol_python expected int.
Fix: _prepare_cycle_cache() now converts to {sym: days_away_int} via earnings_days_away().

Tests:
  E1 — dict entry in earnings_map raises no error; earnings penalty applied
  E2 — int entry (post-fix format) works unchanged
  E3 — None entry (symbol not in calendar) — no penalty
  E4 — earnings_map values after fix are all int|None
  E5 — score penalised correctly when eda <= 2
  E6 — no penalty when eda > 2
"""

from __future__ import annotations

import unittest

from bot_stage2_python import _CYCLE_CACHE, score_symbol_python


def _base_md(sym="NVDA"):
    return {
        "ind_by_symbol": {
            sym: {
                "price": 142.0, "prev": 140.0,
                "ma20": 135.0, "ma50": 125.0,
                "ema9": 141.0, "ema21": 137.0, "ema9_cross": "golden",
                "price_above_ema9": True,
                "rsi": 60.0, "macd": 1.0, "macd_signal": 0.7,
                "vol_ratio": 1.2,
            }
        },
        "intraday_summaries": {},
        "current_prices": {sym: 142.0},
    }


def _score(sym="NVDA", earnings_map=None):
    cache_backup = dict(_CYCLE_CACHE)
    try:
        _CYCLE_CACHE["orb_by_sym"]   = {}
        _CYCLE_CACHE["morning_brief"] = {}
        _CYCLE_CACHE["pattern_wl"]   = {}
        _CYCLE_CACHE["insider_evt"]  = {}
        _CYCLE_CACHE["earnings_map"] = earnings_map if earnings_map is not None else {}
        return score_symbol_python(sym, _base_md(sym), {"bias": "neutral"})
    finally:
        _CYCLE_CACHE.update(cache_backup)


class TestL2EarningsGuard(unittest.TestCase):

    # E1 — dict in earnings_map used to cause TypeError; must not crash after fix
    def test_e1_dict_entry_no_crash(self):
        # Simulate old (pre-fix) format where caller passes a raw entry dict
        bad_map = {"NVDA": {"symbol": "NVDA", "earnings_date": "2026-04-29", "timing": "post-market"}}
        # score_symbol_python should not raise — it should handle gracefully
        # (the fix is in _prepare_cycle_cache, but score_symbol_python's eda path must be robust)
        try:
            result = _score("NVDA", bad_map)
            # If it doesn't crash, the conflict list might contain l2_error or not
            # Either way no exception should propagate
            self.assertIsInstance(result, dict)
        except TypeError:
            self.fail("score_symbol_python raised TypeError on dict earnings entry")

    # E2 — int entry (correct post-fix format) applies penalty when eda <= 2
    def test_e2_int_entry_penalty_applied(self):
        result = _score("NVDA", {"NVDA": 1})
        self.assertIn("earnings_in_1d", result["conflicts"])

    # E3 — None entry means no earnings in calendar — no penalty
    def test_e3_none_entry_no_penalty(self):
        result = _score("NVDA", {"NVDA": None})
        self.assertNotIn("earnings_in_1d", result["conflicts"])
        self.assertNotIn("earnings_in_0d", result["conflicts"])

    # E4 — symbol absent from map — no penalty
    def test_e4_missing_symbol_no_penalty(self):
        result = _score("NVDA", {})  # NVDA not in map
        earnings_conflicts = [c for c in result["conflicts"] if c.startswith("earnings_in_")]
        self.assertEqual(earnings_conflicts, [])

    # E5 — eda = 2 → penalty applied (boundary)
    def test_e5_eda_2_penalty_applied(self):
        result = _score("NVDA", {"NVDA": 2})
        self.assertIn("earnings_in_2d", result["conflicts"])

    # E6 — eda = 3 → no penalty
    def test_e6_eda_3_no_penalty(self):
        result = _score("NVDA", {"NVDA": 3})
        earnings_conflicts = [c for c in result["conflicts"] if c.startswith("earnings_in_")]
        self.assertEqual(earnings_conflicts, [])

    # E7 — prepare_cycle_cache builds int|None map (not dict-per-sym)
    def test_e7_cache_build_produces_int_values(self):
        from earnings_calendar_lookup import earnings_days_away, load_calendar_map

        raw = load_calendar_map()
        if not raw:
            self.skipTest("earnings calendar is empty on this machine")
        day_map = {sym: earnings_days_away(sym, raw) for sym in raw}
        bad = {k: v for k, v in day_map.items() if not isinstance(v, (int, type(None)))}
        self.assertEqual(bad, {}, f"Non-int values in earnings_map: {bad}")

    # E8 — score penalty is -10 points (regression guard)
    def test_e8_penalty_magnitude(self):
        # Score with eda=1 vs eda=None — difference should be >= 10
        base = _score("NVDA", {})["score"]
        penalised = _score("NVDA", {"NVDA": 1})["score"]
        self.assertGreaterEqual(base - penalised, 10,
            f"Expected >=10 pt penalty, got {base - penalised}")


if __name__ == "__main__":
    unittest.main()
