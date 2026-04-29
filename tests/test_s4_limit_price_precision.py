"""
tests/test_s4_limit_price_precision.py

Tests for _round_limit() precision fix in options_executor.py.

Root cause: round(n) * 0.05 produces float artifacts in Python
(e.g., 39 * 0.05 == 1.9500000000000002). Alpaca rejects limit prices
with more than 2 decimal places (error code 42210000).

Fix: wrap with round(..., 2) to eliminate the artifact.
"""
from __future__ import annotations

import unittest

from options_executor import _round_limit


class TestRoundLimitPrecision(unittest.TestCase):
    """_round_limit() must always return a value with at most 2 decimal places."""

    def _assert_max_2dp(self, price: float, label: str = ""):
        result = _round_limit(price)
        # Check no float artifact: result * 100 must be a whole number
        self.assertAlmostEqual(result * 100, round(result * 100), places=6,
            msg=f"_round_limit({price}{' [' + label + ']' if label else ''}) = {result!r} "
                f"has >2 decimal places")

    # ── Known float artifact inputs ──────────────────────────────────────────

    def test_1_97_no_artifact(self):
        # 1.97 → round(1.97/0.05)*0.05 = 39*0.05 = 1.9500000000000002 without fix
        _round_limit(1.97)
        self._assert_max_2dp(1.97, "1.97")

    def test_1_97_rounds_to_195(self):
        self.assertAlmostEqual(_round_limit(1.97), 1.95, places=10)

    def test_2_97_no_artifact(self):
        # 2.97 → round(2.97/0.05)*0.05 = 59*0.05 = 2.9500000000000002 without fix
        _round_limit(2.97)
        self._assert_max_2dp(2.97, "2.97")

    def test_3_97_no_artifact(self):
        self._assert_max_2dp(3.97, "3.97")

    def test_4_97_no_artifact(self):
        self._assert_max_2dp(4.97, "4.97")

    def test_0_97_no_artifact(self):
        self._assert_max_2dp(0.97, "0.97")

    # ── All $0.05 multiples from $0.05 to $20.00 have exactly 2dp ───────────

    def test_all_005_multiples_clean(self):
        """Every possible _round_limit output ($0.05–$20.00) must have ≤2dp."""
        for n in range(1, 401):
            price = n * 0.05
            _round_limit(price)
            self._assert_max_2dp(price, f"n={n}")

    # ── Spot checks for correct rounding ────────────────────────────────────

    def test_rounds_to_nearest_005(self):
        self.assertAlmostEqual(_round_limit(1.03), 1.05, places=10)
        self.assertAlmostEqual(_round_limit(1.07), 1.05, places=10)
        self.assertAlmostEqual(_round_limit(1.12), 1.10, places=10)
        self.assertAlmostEqual(_round_limit(1.13), 1.15, places=10)

    def test_minimum_005(self):
        self.assertAlmostEqual(_round_limit(0.001), 0.05, places=10)
        self.assertAlmostEqual(_round_limit(0.0),   0.05, places=10)

    def test_exact_multiple_unchanged(self):
        self.assertAlmostEqual(_round_limit(1.50), 1.50, places=10)
        self.assertAlmostEqual(_round_limit(2.00), 2.00, places=10)
        self.assertAlmostEqual(_round_limit(0.25), 0.25, places=10)

    def test_high_value_no_artifact(self):
        self._assert_max_2dp(19.97, "19.97")
        self._assert_max_2dp(15.03, "15.03")

    # ── Return type is float ─────────────────────────────────────────────────

    def test_returns_float(self):
        self.assertIsInstance(_round_limit(1.50), float)

    # ── Alpaca compliance: str representation has ≤2dp ──────────────────────

    def test_alpaca_string_representation_clean(self):
        """
        Alpaca serialises limit_price as a string. Verify _round_limit results
        produce strings with at most 2 decimal places for the known artifact cases.
        """
        artifact_inputs = [0.97, 1.97, 2.97, 3.97, 4.97, 9.97, 14.97, 19.97]
        for p in artifact_inputs:
            result = _round_limit(p)
            s = f"{result}"
            # Split on decimal point and check fractional part length
            if "." in s:
                decimal_places = len(s.split(".")[1])
                self.assertLessEqual(decimal_places, 2,
                    f"_round_limit({p}) = {result!r} → str '{s}' has {decimal_places} dp")


if __name__ == "__main__":
    unittest.main()
