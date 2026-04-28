"""Sprint 6 Phase G/H tests: tiered conviction-based margin multiplier."""
import json
from pathlib import Path

import pytest


def _make_config(tiers=None, flat_mult=None, crypto_cap=None):
    cfg = {
        "parameters": {
            "margin_sizing_multiplier": flat_mult or 4.0,
            "max_position_pct_equity": 0.25,
            "max_crypto_margin_multiplier": crypto_cap or 2.0,
        },
        "position_sizing": {},
    }
    if tiers:
        cfg["parameters"]["margin_sizing_multiplier_tiers"] = tiers
    return cfg


_STANDARD_TIERS = {
    "medium":      {"min": 0.50, "max": 0.6499, "multiplier": 1.0},
    "high":        {"min": 0.65, "max": 0.7249, "multiplier": 2.0},
    "strong_high": {"min": 0.725, "max": 0.7999, "multiplier": 3.0},
    "very_high":   {"min": 0.80,  "max": 1.0,   "multiplier": 4.0},
}


class TestGetMarginMultiplier:

    def test_medium_conviction_returns_1x(self):
        import risk_kernel as rk
        cfg = _make_config(tiers=_STANDARD_TIERS)
        assert rk._get_margin_multiplier(0.50, "NVDA", cfg) == 1.0
        assert rk._get_margin_multiplier(0.60, "NVDA", cfg) == 1.0
        assert rk._get_margin_multiplier(0.6499, "NVDA", cfg) == 1.0

    def test_high_conviction_returns_2x(self):
        import risk_kernel as rk
        cfg = _make_config(tiers=_STANDARD_TIERS)
        assert rk._get_margin_multiplier(0.65, "NVDA", cfg) == 2.0
        assert rk._get_margin_multiplier(0.70, "NVDA", cfg) == 2.0
        assert rk._get_margin_multiplier(0.7249, "NVDA", cfg) == 2.0

    def test_strong_high_conviction_returns_3x(self):
        import risk_kernel as rk
        cfg = _make_config(tiers=_STANDARD_TIERS)
        assert rk._get_margin_multiplier(0.725, "NVDA", cfg) == 3.0
        assert rk._get_margin_multiplier(0.75,  "NVDA", cfg) == 3.0
        assert rk._get_margin_multiplier(0.7999, "NVDA", cfg) == 3.0

    def test_very_high_conviction_returns_4x(self):
        import risk_kernel as rk
        cfg = _make_config(tiers=_STANDARD_TIERS)
        assert rk._get_margin_multiplier(0.80, "NVDA", cfg) == 4.0
        assert rk._get_margin_multiplier(0.90, "NVDA", cfg) == 4.0
        assert rk._get_margin_multiplier(1.0,  "NVDA", cfg) == 4.0

    def test_crypto_capped_at_2x_regardless_of_conviction(self):
        import risk_kernel as rk
        cfg = _make_config(tiers=_STANDARD_TIERS, crypto_cap=2.0)
        # Very high conviction on BTC — capped at 2x
        assert rk._get_margin_multiplier(0.90, "BTC/USD", cfg) == 2.0
        assert rk._get_margin_multiplier(0.80, "ETH/USD", cfg) == 2.0
        # High conviction crypto — 2x (same as cap)
        assert rk._get_margin_multiplier(0.70, "BTC/USD", cfg) == 2.0
        # Medium conviction crypto — 1x (below cap)
        assert rk._get_margin_multiplier(0.55, "BTC/USD", cfg) == 1.0

    def test_fallback_to_flat_multiplier_when_no_tiers(self):
        import risk_kernel as rk
        cfg = _make_config(flat_mult=4.0)  # no tiers
        # Fallback returns flat mult for any conviction (HIGH/MEDIUM split
        # is handled by _compute_sizing_basis on the legacy path)
        assert rk._get_margin_multiplier(0.80, "NVDA", cfg) == 4.0

    def test_below_medium_returns_1x(self):
        """Sub-0.50 conviction gets 1x — blocked upstream anyway."""
        import risk_kernel as rk
        cfg = _make_config(tiers=_STANDARD_TIERS)
        result = rk._get_margin_multiplier(0.40, "NVDA", cfg)
        assert result == 1.0  # no matching tier → default

    def test_tier_boundaries_exact(self):
        """Exact boundary values fall in correct tier."""
        import risk_kernel as rk
        cfg = _make_config(tiers=_STANDARD_TIERS)
        # Boundary between high and strong_high
        assert rk._get_margin_multiplier(0.7249, "NVDA", cfg) == 2.0
        assert rk._get_margin_multiplier(0.725,  "NVDA", cfg) == 3.0
        # Boundary between strong_high and very_high
        assert rk._get_margin_multiplier(0.7999, "NVDA", cfg) == 3.0
        assert rk._get_margin_multiplier(0.80,   "NVDA", cfg) == 4.0


class TestSizingLadder:
    """Full sizing ladder — confirms actual $ amounts at each conviction tier."""

    def _make_snapshot(self):
        from schemas import BrokerSnapshot
        return BrokerSnapshot(
            positions=[],
            open_orders=[],
            equity=102_000.0,
            buying_power=116_000.0,
            cash=30_000.0,
        )

    def _make_idea(self, conviction, symbol="NVDA"):
        from schemas import TradeIdea, AccountAction, Tier, Direction
        return TradeIdea(
            symbol=symbol,
            action=AccountAction.BUY,
            tier=Tier.CORE,
            conviction=conviction,
            direction=Direction.BULLISH,
            catalyst="test",
            sector_signal="",
            advisory_stop_pct=0.03,
            advisory_target_r=2.0,
            notes="test",
        )

    def test_medium_conviction_uses_1x(self):
        """0.55 conviction → 1x multiplier (MEDIUM tier) → equity-based sizing."""
        import risk_kernel as rk
        cfg = json.loads(Path("strategy_config.json").read_text())
        snap = self._make_snapshot()
        idea = self._make_idea(0.55)
        qty, value = rk.size_position(idea, snap, cfg, current_price=100.0, vix=18.0)
        assert qty > 0
        # 1x: sizing_basis ≈ equity = $102K; 15% tier → $15.3K
        assert value <= 102_000 * 0.25 * 1.1  # within 10% of max_position_pct_equity

    def test_high_conviction_uses_2x(self):
        """0.65 conviction → 2x multiplier → larger sizing than MEDIUM."""
        import risk_kernel as rk
        cfg = json.loads(Path("strategy_config.json").read_text())
        snap = self._make_snapshot()
        idea_med  = self._make_idea(0.55)
        idea_high = self._make_idea(0.65)
        _, val_med  = rk.size_position(idea_med,  snap, cfg, current_price=100.0, vix=18.0)
        _, val_high = rk.size_position(idea_high, snap, cfg, current_price=100.0, vix=18.0)
        assert val_high >= val_med, (
            f"HIGH ({val_high}) should be >= MEDIUM ({val_med})"
        )

    def test_very_high_conviction_uses_4x(self):
        """0.85 conviction → 4x multiplier → maximum sizing."""
        import risk_kernel as rk
        cfg = json.loads(Path("strategy_config.json").read_text())
        snap = self._make_snapshot()
        idea_high = self._make_idea(0.65)
        idea_vh   = self._make_idea(0.85)
        _, val_high = rk.size_position(idea_high, snap, cfg, current_price=100.0, vix=18.0)
        _, val_vh   = rk.size_position(idea_vh,   snap, cfg, current_price=100.0, vix=18.0)
        assert val_vh >= val_high, (
            f"VERY HIGH ({val_vh}) should be >= HIGH ({val_high})"
        )

    def test_crypto_capped_below_equity_max(self):
        """BTC/USD at 0.85 conviction is capped at 2x — at or below NVDA same conv."""
        import risk_kernel as rk
        cfg = json.loads(Path("strategy_config.json").read_text())
        snap = self._make_snapshot()
        idea_btc  = self._make_idea(0.85, symbol="BTC/USD")
        idea_nvda = self._make_idea(0.85, symbol="NVDA")
        _, val_btc  = rk.size_position(idea_btc,  snap, cfg, current_price=100.0, vix=18.0)
        _, val_nvda = rk.size_position(idea_nvda, snap, cfg, current_price=100.0, vix=18.0)
        assert val_btc <= val_nvda, (
            f"BTC ({val_btc}) should be <= NVDA ({val_nvda}) due to crypto cap"
        )

    def test_pdt_floor_intact(self):
        """PDT floor remains $26,000 after changes."""
        import risk_kernel as rk
        assert rk.PDT_FLOOR == 26_000.0, f"PDT_FLOOR modified: {rk.PDT_FLOOR}"
