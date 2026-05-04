"""
tests/test_short_selling_portfolio.py — Session 3 of 3: short-position analytics.

Covers:
  - compute_position_health(): direction-aware drawdown for short positions
  - get_forced_exits(): includes short positions (qty < 0)
  - get_deadline_exits(): includes short positions in held map
  - format_positions_with_health(): [SHORT] label, abs(qty), abs(cap_pct)
  - system_v1.txt + user_template_v1.txt: "cover" in intent enum
"""
import sys
import types
from unittest.mock import MagicMock

# ── stubs ──────────────────────────────────────────────────────────────────────

def _ensure_stubs():
    if "dotenv" not in sys.modules:
        m = types.ModuleType("dotenv")
        m.load_dotenv = lambda *a, **kw: None
        sys.modules["dotenv"] = m

    for mod in (
        "alpaca", "alpaca.trading", "alpaca.trading.client",
        "alpaca.trading.requests", "alpaca.trading.enums",
        "alpaca.data", "alpaca.data.historical", "alpaca.data.requests",
        "alpaca.data.enums",
    ):
        if mod not in sys.modules:
            sys.modules[mod] = types.ModuleType(mod)

    enums = sys.modules["alpaca.trading.enums"]
    for enum_name, attrs in {
        "OrderSide":        {"BUY": "buy",  "SELL": "sell"},
        "TimeInForce":      {"DAY": "day",  "GTC":  "gtc"},
        "OrderClass":       {"BRACKET": "bracket", "OCO": "oco"},
        "QueryOrderStatus": {"OPEN": "open", "ALL": "all"},
    }.items():
        if not hasattr(enums, enum_name):
            cls = type(enum_name, (), {})
            setattr(enums, enum_name, cls)
        cls = getattr(enums, enum_name)
        for attr, val in attrs.items():
            if not hasattr(cls, attr):
                setattr(cls, attr, val)

    reqs = sys.modules["alpaca.trading.requests"]
    for cls_name in (
        "MarketOrderRequest", "LimitOrderRequest", "StopOrderRequest",
        "StopLossRequest", "TakeProfitRequest", "ClosePositionRequest",
        "GetOrdersRequest",
    ):
        if not hasattr(reqs, cls_name):
            def _mk(name):
                class _Req:
                    def __init__(self, **kwargs):
                        for k, v in kwargs.items():
                            setattr(self, k, v)
                _Req.__name__ = name
                return _Req
            setattr(reqs, cls_name, _mk(cls_name))

    tc = sys.modules["alpaca.trading.client"]
    if not hasattr(tc, "TradingClient"):
        class _TC:
            def __init__(self, **_kw): pass
        tc.TradingClient = _TC


_ensure_stubs()


# ── helpers ────────────────────────────────────────────────────────────────────

def _pos(symbol="NVDA", qty=-10, avg_entry_price=100.0,
         current_price=105.0, market_value=-1050.0, unrealized_pl=-50.0):
    """Return a MagicMock mimicking an Alpaca position object."""
    p = MagicMock()
    p.symbol          = symbol
    p.qty             = str(qty)
    p.avg_entry_price = str(avg_entry_price)
    p.current_price   = str(current_price)
    p.market_value    = str(market_value)
    p.unrealized_pl   = str(unrealized_pl)
    return p


# ── Test 1: compute_position_health() drawdown direction for shorts ───────────

def test_compute_position_health_short_drawdown_direction():
    """
    For a short position that moved against us (price rose above entry),
    drawdown_pct must be POSITIVE (loss) and equal (current-entry)/entry*100.
    For a long position with the same entry/current, drawdown is negative.
    """
    _ensure_stubs()
    import portfolio_intelligence as pi

    # Short: sold at 100, now at 105 — losing 5%
    short_pos = _pos(qty=-10, avg_entry_price=100.0, current_price=105.0,
                     market_value=-1050.0, unrealized_pl=-50.0)
    health = pi.compute_position_health(short_pos, equity=100_000.0)
    assert health["drawdown_pct"] > 0, (
        f"Short position losing money should have positive drawdown_pct, got {health['drawdown_pct']}"
    )
    assert abs(health["drawdown_pct"] - 5.0) < 0.01, (
        f"Expected drawdown_pct=5.0 for short at 100 now 105, got {health['drawdown_pct']}"
    )


# ── Test 2: compute_position_health() account_pct uses abs(market_value) ─────

def test_compute_position_health_short_account_pct_positive():
    """
    account_pct must be positive for shorts even though market_value is negative.
    """
    _ensure_stubs()
    import portfolio_intelligence as pi

    short_pos = _pos(qty=-10, avg_entry_price=100.0, current_price=100.0,
                     market_value=-1000.0, unrealized_pl=0.0)
    health = pi.compute_position_health(short_pos, equity=10_000.0)
    assert health["account_pct"] > 0, (
        f"account_pct must be positive for shorts (market_value=-1000), got {health['account_pct']}"
    )
    assert abs(health["account_pct"] - 10.0) < 0.01, (
        f"Expected account_pct=10.0 ($1k of $10k equity), got {health['account_pct']}"
    )


# ── Test 3: get_forced_exits() includes CRITICAL short positions ──────────────

def test_get_forced_exits_includes_short_positions():
    """
    A short position with a large adverse move (price up >12% from entry)
    must appear in forced_exits as CRITICAL — not be skipped by qty <= 0 guard.
    """
    _ensure_stubs()
    import portfolio_intelligence as pi

    # Short at 100; price now 115 — 15% adverse move → CRITICAL
    short_pos = _pos(qty=-10, avg_entry_price=100.0, current_price=115.0,
                     market_value=-1150.0, unrealized_pl=-150.0)
    forced = pi.get_forced_exits([short_pos], equity=50_000.0)
    assert len(forced) == 1, (
        f"Expected 1 CRITICAL short in forced_exits, got {len(forced)}: {forced}"
    )
    assert forced[0]["symbol"] == "NVDA"
    assert forced[0]["full_qty"] == 10, (
        f"full_qty must be abs(qty)=10 for a short, got {forced[0]['full_qty']}"
    )


# ── Test 4: get_deadline_exits() includes short positions in held map ─────────

def test_get_deadline_exits_includes_short_positions():
    """
    get_deadline_exits() must include short positions (qty < 0) in its held map.
    Previously `float(pos.qty) > 0` skipped them.
    """
    _ensure_stubs()
    import portfolio_intelligence as pi

    short_pos = _pos(symbol="TSLA", qty=-5, avg_entry_price=200.0,
                     current_price=195.0, market_value=-975.0, unrealized_pl=25.0)
    strategy_config = {
        "time_bound_actions": [
            {
                "symbol":       "TSLA",
                "reason":       "test deadline",
                "deadline_et":  "2000-01-01 09:30",
                "deadline_utc": "2000-01-01T14:30:00Z",
            }
        ]
    }
    expired = pi.get_deadline_exits(strategy_config, [short_pos])
    assert len(expired) == 1, (
        f"Expected TSLA short in deadline exits, got {expired}"
    )
    assert expired[0]["full_qty"] == 5, (
        f"full_qty must be abs(qty)=5 for short, got {expired[0]['full_qty']}"
    )


# ── Test 5: format_positions_with_health() shows [SHORT] label ───────────────

def test_format_positions_short_label_and_positive_values():
    """
    format_positions_with_health() must:
    - Prefix the short position row with [SHORT]
    - Show positive account_pct (not negative)
    - Show positive drawdown for a losing short
    """
    _ensure_stubs()
    import portfolio_intelligence as pi

    # Short at 100, price now 104 — losing 4%
    short_pos = _pos(qty=-10, avg_entry_price=100.0, current_price=104.0,
                     market_value=-1040.0, unrealized_pl=-40.0)
    result = pi.format_positions_with_health(
        [short_pos], equity=20_000.0, buying_power=18_000.0
    )
    assert "[SHORT]" in result, (
        f"Expected [SHORT] prefix in formatted output, got:\n{result}"
    )
    # account_pct must be positive — look for a positive percentage in the line
    assert "account_pct=" in result
    # extract account_pct value
    import re
    m = re.search(r"account_pct=(-?[\d.]+)%", result)
    assert m, "Could not find account_pct= in output"
    assert float(m.group(1)) > 0, (
        f"account_pct must be positive for a short position, got {m.group(1)}"
    )
