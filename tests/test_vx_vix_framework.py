"""
test_vx_vix_framework.py — Tests for the graduated VIX gate in risk_kernel.eligibility_check().

VX-01: VIX=17 → no restrictions, all entries allowed
VX-02: VIX=22 → no hard blocks (elevated note injected via get_vix_context_note)
VX-03: VIX=27 → INTRADAY long blocked, CORE long allowed, bearish BUY allowed
VX-04: VIX=35 → INTRADAY+DYNAMIC long blocked, CORE needs conviction >= 0.75
VX-05: VIX=35 → bearish entry with conviction=0.60 ALLOWED (bearish never restricted)
VX-06: VIX=45 → all long entries blocked, bearish still allowed
VX-07: VIX=45 → exits and stops never blocked
VX-08: Config thresholds respected (not hardcoded)
"""

import pytest
from risk_kernel import eligibility_check, get_vix_context_note
from schemas import (
    AccountAction,
    BrokerSnapshot,
    Direction,
    NormalizedPosition,
    Tier,
    TradeIdea,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _snapshot(equity: float = 100_000.0) -> BrokerSnapshot:
    return BrokerSnapshot(
        equity=equity,
        cash=equity * 0.8,
        buying_power=equity,
        open_orders=[],
        positions=[],
    )


def _idea(
    tier: Tier = Tier.CORE,
    conviction: float = 0.80,
    direction: Direction = Direction.BULLISH,
    action: AccountAction = AccountAction.BUY,
    symbol: str = "AAPL",
    catalyst: str = "earnings_beat",
    intent: str = "enter_long",
) -> TradeIdea:
    return TradeIdea(
        symbol=symbol,
        action=action,
        tier=tier,
        conviction=conviction,
        direction=direction,
        catalyst=catalyst,
        intent=intent,
    )


_BASE_CONFIG = {
    "parameters": {
        "max_positions": 15,
        "catalyst_tag_required_for_entry": True,
        "catalyst_tag_disallowed_values": ["", "none", "null", "no"],
    },
    "position_sizing": {"core_tier_pct": 0.15},
    "time_bound_actions": [],
}


def _config_with_vix(**overrides) -> dict:
    """Return a config with custom VIX thresholds in parameters."""
    cfg = {
        "parameters": {
            **_BASE_CONFIG["parameters"],
            **overrides,
        },
        "position_sizing": _BASE_CONFIG["position_sizing"],
        "time_bound_actions": [],
    }
    return cfg


# ── VX-01: VIX=17 calm — no restrictions ──────────────────────────────────────

class TestVX01Calm:
    """VIX=17 (calm) — all entry tiers allowed."""

    def test_core_bullish_passes(self):
        assert eligibility_check(_idea(tier=Tier.CORE), _snapshot(), _BASE_CONFIG, vix=17.0) is None

    def test_dynamic_bullish_passes(self):
        assert eligibility_check(_idea(tier=Tier.DYNAMIC), _snapshot(), _BASE_CONFIG, vix=17.0) is None

    def test_intraday_bullish_passes(self):
        assert eligibility_check(_idea(tier=Tier.INTRADAY), _snapshot(), _BASE_CONFIG, vix=17.0) is None

    def test_bearish_passes(self):
        assert eligibility_check(_idea(direction=Direction.BEARISH), _snapshot(), _BASE_CONFIG, vix=17.0) is None

    def test_context_note_calm_is_none(self):
        assert get_vix_context_note(17.0, _BASE_CONFIG) is None


# ── VX-02: VIX=22 elevated — no hard blocks ───────────────────────────────────

class TestVX02Elevated:
    """VIX=22 (elevated) — no hard eligibility blocks; context note returned."""

    def test_core_bullish_passes(self):
        assert eligibility_check(_idea(tier=Tier.CORE), _snapshot(), _BASE_CONFIG, vix=22.0) is None

    def test_dynamic_bullish_passes(self):
        assert eligibility_check(_idea(tier=Tier.DYNAMIC), _snapshot(), _BASE_CONFIG, vix=22.0) is None

    def test_intraday_bullish_passes(self):
        assert eligibility_check(_idea(tier=Tier.INTRADAY), _snapshot(), _BASE_CONFIG, vix=22.0) is None

    def test_bearish_passes(self):
        assert eligibility_check(_idea(direction=Direction.BEARISH), _snapshot(), _BASE_CONFIG, vix=22.0) is None

    def test_context_note_elevated_is_not_none(self):
        note = get_vix_context_note(22.0, _BASE_CONFIG)
        assert note is not None
        assert "VIX" in note
        assert "22" in note


# ── VX-03: VIX=27 cautious — INTRADAY long blocked ───────────────────────────

class TestVX03Cautious:
    """VIX=27 (cautious) — INTRADAY long blocked; CORE/DYNAMIC long and bearish allowed."""

    def test_intraday_bullish_blocked(self):
        result = eligibility_check(_idea(tier=Tier.INTRADAY), _snapshot(), _BASE_CONFIG, vix=27.0)
        assert result is not None
        assert "VIX" in result
        assert "intraday" in result.lower()

    def test_core_bullish_allowed(self):
        assert eligibility_check(_idea(tier=Tier.CORE), _snapshot(), _BASE_CONFIG, vix=27.0) is None

    def test_dynamic_bullish_allowed(self):
        assert eligibility_check(_idea(tier=Tier.DYNAMIC), _snapshot(), _BASE_CONFIG, vix=27.0) is None

    def test_bearish_buy_allowed(self):
        # Buying an inverse ETF (bearish) should never be blocked in cautious regime
        result = eligibility_check(
            _idea(direction=Direction.BEARISH, conviction=0.60), _snapshot(), _BASE_CONFIG, vix=27.0
        )
        assert result is None

    def test_context_note_cautious(self):
        note = get_vix_context_note(27.0, _BASE_CONFIG)
        assert note is not None
        assert "CAUTIOUS" in note or "cautious" in note.lower()


# ── VX-04: VIX=35 stressed — INTRADAY+DYNAMIC blocked; CORE needs conviction ─

class TestVX04Stressed:
    """VIX=35 (stressed) — INTRADAY+DYNAMIC blocked; CORE needs conviction >= 0.75."""

    def test_intraday_bullish_blocked(self):
        result = eligibility_check(_idea(tier=Tier.INTRADAY), _snapshot(), _BASE_CONFIG, vix=35.0)
        assert result is not None
        assert "VIX" in result

    def test_dynamic_bullish_blocked(self):
        result = eligibility_check(_idea(tier=Tier.DYNAMIC), _snapshot(), _BASE_CONFIG, vix=35.0)
        assert result is not None
        assert "VIX" in result

    def test_core_high_conviction_passes(self):
        # conviction=0.80 >= floor (0.75) → allowed
        assert eligibility_check(_idea(tier=Tier.CORE, conviction=0.80), _snapshot(), _BASE_CONFIG, vix=35.0) is None

    def test_core_low_conviction_blocked(self):
        # conviction=0.65 < floor (0.75) → blocked
        result = eligibility_check(_idea(tier=Tier.CORE, conviction=0.65), _snapshot(), _BASE_CONFIG, vix=35.0)
        assert result is not None
        assert "conviction" in result.lower()

    def test_context_note_stressed(self):
        note = get_vix_context_note(35.0, _BASE_CONFIG)
        assert note is not None
        assert "STRESSED" in note or "stressed" in note.lower()


# ── VX-05: VIX=35 — bearish entry with low conviction is always allowed ───────

class TestVX05BearishStressed:
    """VIX=35 stressed — bearish BUY is never blocked regardless of conviction."""

    def test_bearish_core_low_conviction_allowed(self):
        result = eligibility_check(
            _idea(direction=Direction.BEARISH, conviction=0.60, tier=Tier.CORE),
            _snapshot(), _BASE_CONFIG, vix=35.0,
        )
        assert result is None

    def test_bearish_intraday_allowed(self):
        result = eligibility_check(
            _idea(direction=Direction.BEARISH, tier=Tier.INTRADAY, conviction=0.50),
            _snapshot(), _BASE_CONFIG, vix=35.0,
        )
        assert result is None

    def test_bearish_dynamic_allowed(self):
        result = eligibility_check(
            _idea(direction=Direction.BEARISH, tier=Tier.DYNAMIC, conviction=0.55),
            _snapshot(), _BASE_CONFIG, vix=35.0,
        )
        assert result is None


# ── VX-06: VIX=45 crisis — all long entries blocked, bearish allowed ──────────

class TestVX06Crisis:
    """VIX=45 (crisis) — all non-bearish long entries blocked; bearish fully enabled."""

    def test_core_bullish_blocked(self):
        result = eligibility_check(_idea(tier=Tier.CORE, conviction=0.95), _snapshot(), _BASE_CONFIG, vix=45.0)
        assert result is not None
        assert "VIX" in result

    def test_dynamic_bullish_blocked(self):
        result = eligibility_check(_idea(tier=Tier.DYNAMIC, conviction=0.95), _snapshot(), _BASE_CONFIG, vix=45.0)
        assert result is not None

    def test_intraday_bullish_blocked(self):
        result = eligibility_check(_idea(tier=Tier.INTRADAY, conviction=0.95), _snapshot(), _BASE_CONFIG, vix=45.0)
        assert result is not None

    def test_bearish_buy_allowed(self):
        result = eligibility_check(
            _idea(direction=Direction.BEARISH, conviction=0.60),
            _snapshot(), _BASE_CONFIG, vix=45.0,
        )
        assert result is None

    def test_context_note_crisis(self):
        note = get_vix_context_note(45.0, _BASE_CONFIG)
        assert note is not None
        assert "CRISIS" in note or "crisis" in note.lower()


# ── VX-07: VIX=45 — exits and stops never blocked ────────────────────────────

class TestVX07ExitsNeverBlocked:
    """VIX gate is BUY-only; CLOSE, SELL, HOLD must pass through unaffected."""

    def test_close_not_blocked(self):
        idea = _idea(action=AccountAction.CLOSE, intent="close")
        assert eligibility_check(idea, _snapshot(), _BASE_CONFIG, vix=45.0) is None

    def test_hold_not_blocked(self):
        idea = _idea(action=AccountAction.HOLD, intent="hold")
        assert eligibility_check(idea, _snapshot(), _BASE_CONFIG, vix=45.0) is None

    def test_sell_not_blocked(self):
        idea = _idea(action=AccountAction.SELL, intent="close")
        assert eligibility_check(idea, _snapshot(), _BASE_CONFIG, vix=45.0) is None


# ── VX-08: Config thresholds respected (not hardcoded) ───────────────────────

class TestVX08ConfigDriven:
    """All VIX band thresholds come from config, not hardcoded constants."""

    def test_custom_cautious_threshold(self):
        # Move cautious start to 22 — at VIX=23, intraday should be blocked
        cfg = _config_with_vix(vix_elevated_threshold=22.0, vix_cautious_threshold=28.0)
        result = eligibility_check(_idea(tier=Tier.INTRADAY), _snapshot(), cfg, vix=23.0)
        assert result is not None  # blocked because 23 >= elevated=22 in cautious range

    def test_custom_stressed_threshold(self):
        # Lower crisis start to 35 — at VIX=35, all long entries should be blocked
        cfg = _config_with_vix(
            vix_elevated_threshold=25.0,
            vix_cautious_threshold=30.0,
            vix_stressed_threshold=35.0,
        )
        result = eligibility_check(_idea(tier=Tier.CORE, conviction=0.90), _snapshot(), cfg, vix=35.0)
        assert result is not None  # crisis starts at 35 in this config

    def test_custom_conviction_floor(self):
        # Raise conviction floor to 0.85 — conviction=0.80 at VIX=35 should be blocked
        cfg = _config_with_vix(
            vix_cautious_threshold=30.0,
            vix_stressed_threshold=40.0,
            vix_stressed_conviction_floor=0.85,
        )
        result = eligibility_check(_idea(tier=Tier.CORE, conviction=0.80), _snapshot(), cfg, vix=35.0)
        assert result is not None
        assert "conviction" in result.lower()

    def test_default_config_matches_band_defaults(self):
        # With bare config, defaults from _VIX_BAND_DEFAULTS apply
        from risk_kernel import _VIX_BAND_DEFAULTS
        bare = {"parameters": {}, "position_sizing": {}, "time_bound_actions": []}
        # At VIX just below crisis default (40), CORE high conviction passes
        result = eligibility_check(
            _idea(tier=Tier.CORE, conviction=0.90), _snapshot(), bare,
            vix=_VIX_BAND_DEFAULTS["vix_stressed_threshold"] - 0.1,
        )
        # In stressed band, conviction=0.90 >= floor (0.75) → passes
        assert result is None
