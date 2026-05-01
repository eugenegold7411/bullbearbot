"""
test_risk_kernel_eligibility.py — Tests for risk_kernel.eligibility_check().

Covers all six hard gates:
  0. Time-bound action block
  1. VIX halt (>= 35)
  2. PDT equity floor (< $26K)
  3. Session gate — stocks/ETFs require market session
  4. Intraday tier gate
  5. Max open positions
  6. Catalyst required for buys

All tests are offline-safe (no Alpaca / Claude / Twilio / network calls).
"""


from risk_kernel import PDT_FLOOR, eligibility_check, get_vix_context_note
from schemas import (
    AccountAction,
    BrokerSnapshot,
    Direction,
    NormalizedPosition,
    Tier,
    TradeIdea,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _snapshot(equity: float = 100_000.0, n_positions: int = 0) -> BrokerSnapshot:
    positions = [
        NormalizedPosition(
            symbol=f"SYM{i}", alpaca_sym=f"SYM{i}",
            qty=10.0, avg_entry_price=100.0, current_price=100.0,
            market_value=1_000.0, unrealized_pl=0.0, unrealized_plpc=0.0,
            is_crypto_pos=False,
        )
        for i in range(n_positions)
    ]
    return BrokerSnapshot(
        positions=positions,
        open_orders=[],
        equity=equity,
        cash=equity * 0.8,
        buying_power=equity,
    )


def _idea(
    symbol: str = "AAPL",
    action: AccountAction = AccountAction.BUY,
    tier: Tier = Tier.CORE,
    conviction: float = 0.80,
    direction: Direction = Direction.BULLISH,
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


# ── PDT equity floor ──────────────────────────────────────────────────────────

class TestPDTFloor:
    def test_equity_below_floor_rejected(self, kernel_config):
        result = eligibility_check(_idea(), _snapshot(equity=PDT_FLOOR - 1), kernel_config)
        assert result is not None
        assert "PDT" in result or "equity" in result.lower()

    def test_equity_exactly_at_floor_passes(self, kernel_config):
        result = eligibility_check(_idea(), _snapshot(equity=PDT_FLOOR), kernel_config)
        assert result is None

    def test_equity_well_above_floor_passes(self, kernel_config):
        result = eligibility_check(_idea(), _snapshot(equity=100_000.0), kernel_config)
        assert result is None

    def test_pdt_floor_also_blocks_close(self, kernel_config):
        # PDT floor is not BUY-specific; CLOSE is also blocked below floor
        idea = _idea(action=AccountAction.CLOSE, intent="close")
        result = eligibility_check(idea, _snapshot(equity=PDT_FLOOR - 1), kernel_config)
        assert result is not None


# ── VIX graduated gate ────────────────────────────────────────────────────────

class TestVIXGate:
    def test_vix_crisis_blocks_bullish_core_buy(self, kernel_config):
        # VIX >= 40 (crisis) blocks all long entries regardless of tier/conviction
        result = eligibility_check(_idea(), _snapshot(), kernel_config, vix=40.0)
        assert result is not None
        assert "VIX" in result

    def test_vix_crisis_blocks_all_long_tiers(self, kernel_config):
        for tier in (Tier.CORE, Tier.DYNAMIC, Tier.INTRADAY):
            result = eligibility_check(_idea(tier=tier), _snapshot(), kernel_config, vix=45.0)
            assert result is not None, f"tier={tier.value} should be blocked in crisis"

    def test_vix_stressed_blocks_intraday_long(self, kernel_config):
        # stressed (30-40) blocks INTRADAY long
        idea = _idea(tier=Tier.INTRADAY)
        result = eligibility_check(idea, _snapshot(), kernel_config, vix=35.0)
        assert result is not None
        assert "VIX" in result

    def test_vix_stressed_core_high_conviction_passes(self, kernel_config):
        # stressed + CORE + conviction >= 0.75 → allowed
        result = eligibility_check(_idea(conviction=0.80), _snapshot(), kernel_config, vix=35.0)
        assert result is None

    def test_vix_gate_does_not_block_close(self, kernel_config):
        # VIX gate is BUY-only — CLOSE must not be blocked
        idea = _idea(action=AccountAction.CLOSE, intent="close")
        result = eligibility_check(idea, _snapshot(), kernel_config, vix=45.0)
        assert result is None

    def test_vix_context_note_elevated(self, kernel_config):
        # elevated band returns a non-None note
        note = get_vix_context_note(22.0, kernel_config)
        assert note is not None
        assert "VIX" in note

    def test_vix_context_note_calm_returns_none(self, kernel_config):
        note = get_vix_context_note(15.0, kernel_config)
        assert note is None


# ── Session gate ──────────────────────────────────────────────────────────────

class TestSessionGate:
    def test_stock_buy_blocked_in_extended_session(self, kernel_config):
        result = eligibility_check(
            _idea(symbol="AAPL"), _snapshot(), kernel_config,
            session_tier="extended",
        )
        assert result is not None
        assert "session" in result.lower()

    def test_stock_buy_blocked_in_overnight_session(self, kernel_config):
        result = eligibility_check(
            _idea(symbol="AAPL"), _snapshot(), kernel_config,
            session_tier="overnight",
        )
        assert result is not None

    def test_stock_buy_allowed_in_market_session(self, kernel_config):
        result = eligibility_check(
            _idea(symbol="AAPL"), _snapshot(), kernel_config,
            session_tier="market",
        )
        assert result is None

    def test_crypto_buy_allowed_in_extended_session(self, kernel_config):
        # Crypto is not subject to the stock session gate
        result = eligibility_check(
            _idea(symbol="BTC/USD"), _snapshot(), kernel_config,
            session_tier="extended",
        )
        assert result is None

    def test_intraday_crypto_blocked_outside_market_session(self, kernel_config):
        # INTRADAY tier requires market session regardless of asset class
        idea = _idea(symbol="BTC/USD", tier=Tier.INTRADAY)
        result = eligibility_check(idea, _snapshot(), kernel_config,
                                   session_tier="extended")
        assert result is not None
        assert "intraday" in result.lower() or "session" in result.lower()


# ── Max positions ─────────────────────────────────────────────────────────────

class TestMaxPositions:
    def test_at_max_positions_blocks_buy(self, kernel_config):
        import copy
        cfg = copy.deepcopy(kernel_config)
        cfg["parameters"]["max_positions"] = 3
        result = eligibility_check(_idea(), _snapshot(n_positions=3), cfg)
        assert result is not None
        assert "max_position" in result.lower() or "3" in result

    def test_one_below_max_positions_allows_buy(self, kernel_config):
        import copy
        cfg = copy.deepcopy(kernel_config)
        cfg["parameters"]["max_positions"] = 3
        result = eligibility_check(_idea(), _snapshot(n_positions=2), cfg)
        assert result is None


# ── Catalyst gate ─────────────────────────────────────────────────────────────

class TestCatalystGate:
    def test_empty_catalyst_blocks_buy(self, kernel_config):
        result = eligibility_check(_idea(catalyst=""), _snapshot(), kernel_config)
        assert result is not None
        assert "catalyst" in result.lower()

    def test_none_string_catalyst_blocks_buy(self, kernel_config):
        result = eligibility_check(_idea(catalyst="none"), _snapshot(), kernel_config)
        assert result is not None

    def test_null_string_catalyst_blocks_buy(self, kernel_config):
        result = eligibility_check(_idea(catalyst="null"), _snapshot(), kernel_config)
        assert result is not None

    def test_named_catalyst_passes(self, kernel_config):
        result = eligibility_check(_idea(catalyst="earnings_beat_q4"), _snapshot(), kernel_config)
        assert result is None

    def test_hold_bypasses_catalyst_check(self, kernel_config):
        # HOLD is not subject to catalyst gate
        idea = _idea(action=AccountAction.HOLD, catalyst="", intent="hold")
        result = eligibility_check(idea, _snapshot(), kernel_config)
        assert result is None
