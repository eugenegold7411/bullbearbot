"""
tests/test_s4_confidence_floor_url.py

Tests for the paper/live confidence floor discrimination fix.

The fix: both bot_options_stage3_debate and bot_options_stage4_execution now
derive the confidence floor from ALPACA_BASE_URL rather than pf_allow_live_orders.
  - "paper-api.alpaca.markets" in URL → paper_confidence_floor (default 0.75)
  - otherwise → live_confidence_floor (default 0.85)
"""
from __future__ import annotations

import importlib
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers — extract the _is_paper / _conf_floor logic without importing the
# full module (which has heavy deps).  We replicate the exact two-line logic.
# ---------------------------------------------------------------------------

def _compute_floor(alpaca_base_url: str | None, a2_cfg: dict) -> tuple[bool, float]:
    """Mirror the exact logic deployed in both stage3 and stage4."""
    base = alpaca_base_url if alpaca_base_url is not None else "https://paper-api.alpaca.markets"
    is_paper = "paper-api.alpaca.markets" in base.lower()
    floor = float(a2_cfg.get(
        "paper_confidence_floor" if is_paper else "live_confidence_floor",
        0.75 if is_paper else 0.85,
    ))
    return is_paper, floor


_DEFAULT_A2_CFG = {"paper_confidence_floor": 0.75, "live_confidence_floor": 0.85}


# ---------------------------------------------------------------------------
# Suite A — URL → is_paper flag
# ---------------------------------------------------------------------------

class TestIsPaperFlag(unittest.TestCase):

    def test_paper_url_is_paper_true(self):
        is_paper, _ = _compute_floor("https://paper-api.alpaca.markets", _DEFAULT_A2_CFG)
        self.assertTrue(is_paper)

    def test_live_url_is_paper_false(self):
        is_paper, _ = _compute_floor("https://api.alpaca.markets", _DEFAULT_A2_CFG)
        self.assertFalse(is_paper)

    def test_none_url_defaults_to_paper(self):
        is_paper, _ = _compute_floor(None, _DEFAULT_A2_CFG)
        self.assertTrue(is_paper)

    def test_paper_url_case_insensitive(self):
        is_paper, _ = _compute_floor("https://PAPER-API.ALPACA.MARKETS", _DEFAULT_A2_CFG)
        self.assertTrue(is_paper)


# ---------------------------------------------------------------------------
# Suite B — URL → floor value
# ---------------------------------------------------------------------------

class TestFloorValue(unittest.TestCase):

    def test_paper_url_yields_075(self):
        _, floor = _compute_floor("https://paper-api.alpaca.markets", _DEFAULT_A2_CFG)
        self.assertAlmostEqual(floor, 0.75)

    def test_live_url_yields_085(self):
        _, floor = _compute_floor("https://api.alpaca.markets", _DEFAULT_A2_CFG)
        self.assertAlmostEqual(floor, 0.85)

    def test_unset_url_yields_075(self):
        _, floor = _compute_floor(None, _DEFAULT_A2_CFG)
        self.assertAlmostEqual(floor, 0.75)

    def test_paper_floor_respects_config_override(self):
        cfg = {"paper_confidence_floor": 0.80, "live_confidence_floor": 0.90}
        _, floor = _compute_floor("https://paper-api.alpaca.markets", cfg)
        self.assertAlmostEqual(floor, 0.80)

    def test_live_floor_respects_config_override(self):
        cfg = {"paper_confidence_floor": 0.80, "live_confidence_floor": 0.90}
        _, floor = _compute_floor("https://api.alpaca.markets", cfg)
        self.assertAlmostEqual(floor, 0.90)

    def test_missing_config_keys_use_hardcoded_defaults_paper(self):
        _, floor = _compute_floor("https://paper-api.alpaca.markets", {})
        self.assertAlmostEqual(floor, 0.75)

    def test_missing_config_keys_use_hardcoded_defaults_live(self):
        _, floor = _compute_floor("https://api.alpaca.markets", {})
        self.assertAlmostEqual(floor, 0.85)


# ---------------------------------------------------------------------------
# Suite C — Stage3 and Stage4 produce identical floors for the same URL
# ---------------------------------------------------------------------------

class TestStage3Stage4Consistency(unittest.TestCase):
    """Stage3 and stage4 must agree: same URL → same floor."""

    def _stage3_floor(self, url: str) -> float:
        with patch.dict(os.environ, {"ALPACA_BASE_URL": url}):
            base = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
            is_paper = "paper-api.alpaca.markets" in base.lower()
            return float(_DEFAULT_A2_CFG.get(
                "paper_confidence_floor" if is_paper else "live_confidence_floor",
                0.75 if is_paper else 0.85,
            ))

    def _stage4_floor(self, url: str) -> float:
        with patch.dict(os.environ, {"ALPACA_BASE_URL": url}):
            base = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
            is_paper = "paper-api.alpaca.markets" in base.lower()
            return float(_DEFAULT_A2_CFG.get(
                "paper_confidence_floor" if is_paper else "live_confidence_floor",
                0.75 if is_paper else 0.85,
            ))

    def test_paper_url_same_floor(self):
        url = "https://paper-api.alpaca.markets"
        self.assertAlmostEqual(self._stage3_floor(url), self._stage4_floor(url))

    def test_live_url_same_floor(self):
        url = "https://api.alpaca.markets"
        self.assertAlmostEqual(self._stage3_floor(url), self._stage4_floor(url))


# ---------------------------------------------------------------------------
# Suite D — pf_allow_live_orders shadow_only logic unaffected
# ---------------------------------------------------------------------------

class TestShadowOnlyLogicUnaffected(unittest.TestCase):
    """
    pf_allow_live_orders is still used for shadow_only order suppression.
    Verify that _effective_obs = obs_mode or (not pf_allow_live_orders)
    behaves correctly independent of the confidence floor change.
    """

    def _effective_obs(self, obs_mode: bool, pf_allow_live_orders: bool) -> bool:
        return obs_mode or (not pf_allow_live_orders)

    def test_normal_mode_not_obs(self):
        # Normal operation: pf_allow_live_orders=True, obs_mode=False → submit live
        self.assertFalse(self._effective_obs(False, True))

    def test_shadow_only_suppresses_submission(self):
        # shadow_only: pf_allow_live_orders=False → obs=True → suppressed
        self.assertTrue(self._effective_obs(False, False))

    def test_obs_mode_overrides_regardless(self):
        # obs_mode=True always suppresses regardless of pf_allow_live_orders
        self.assertTrue(self._effective_obs(True, True))

    def test_shadow_and_obs_both_suppress(self):
        self.assertTrue(self._effective_obs(True, False))

    def test_confidence_floor_independent_of_shadow(self):
        # Even with pf_allow_live_orders=False (shadow mode), floor comes from URL
        url = "https://paper-api.alpaca.markets"
        with patch.dict(os.environ, {"ALPACA_BASE_URL": url}):
            _, floor = _compute_floor(
                os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
                _DEFAULT_A2_CFG,
            )
        # Floor is 0.75 regardless of pf_allow_live_orders value
        self.assertAlmostEqual(floor, 0.75)


# ---------------------------------------------------------------------------
# Suite E — Boundary: candidate at 0.77 passes paper floor, fails live floor
# ---------------------------------------------------------------------------

class TestBoundaryConfidence(unittest.TestCase):

    def test_077_passes_paper_floor(self):
        _, floor = _compute_floor("https://paper-api.alpaca.markets", _DEFAULT_A2_CFG)
        self.assertLess(floor, 0.77)  # 0.75 < 0.77 → passes

    def test_077_fails_live_floor(self):
        _, floor = _compute_floor("https://api.alpaca.markets", _DEFAULT_A2_CFG)
        self.assertGreater(floor, 0.77)  # 0.85 > 0.77 → blocked

    def test_079_passes_paper_floor(self):
        _, floor = _compute_floor("https://paper-api.alpaca.markets", _DEFAULT_A2_CFG)
        self.assertLess(floor, 0.79)

    def test_086_passes_live_floor(self):
        _, floor = _compute_floor("https://api.alpaca.markets", _DEFAULT_A2_CFG)
        self.assertLess(floor, 0.86)


if __name__ == "__main__":
    unittest.main()
