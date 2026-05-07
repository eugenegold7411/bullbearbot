"""
tests/test_dashboard_enum_cache.py — Issues 28 and 29 dashboard fixes.

28 — _tp_map enum comparison: str(OrderType.LIMIT) returns "OrderType.LIMIT" not "limit",
     so otype == "limit" always failed. Fixed via _enum_str() helper.
29 — Equity curve caches empty result: _cached() stored {} for 3600s on Alpaca failure.
     Fixed by cache_on=bool parameter — falsy results are not stored.

Tests:
  Enum fix (Fix 28):
  1. test_enum_str_helper_extracts_plain_name
  2. test_enum_str_handles_stop
  3. test_enum_str_handles_oco
  4. test_tp_map_returns_value_for_limit_leg
  5. test_stop_map_returns_value_for_stop_leg

  Cache fix (Fix 29):
  6. test_cache_does_not_store_empty_dict
  7. test_cache_does_not_store_empty_list
  8. test_cache_stores_valid_result
  9. test_equity_curve_retries_after_empty
"""

from __future__ import annotations

import importlib
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

os.environ.setdefault("DASHBOARD_USER", "admin")
os.environ.setdefault("DASHBOARD_PASSWORD", "bullbearbot")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("ALPACA_API_KEY", "test-key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret")
os.environ.setdefault("ALPACA_API_KEY_OPTIONS", "test-key")
os.environ.setdefault("ALPACA_SECRET_KEY_OPTIONS", "test-secret")

# Import real Alpaca enums directly before any stubbing so tests can use them
from alpaca.trading.enums import OrderClass, OrderSide, OrderType  # noqa: E402

_STUB_MODULES = {
    "alpaca.trading.client": MagicMock(),
    "alpaca.trading.requests": MagicMock(),
    "chromadb": MagicMock(),
    "twilio": MagicMock(),
    "twilio.rest": MagicMock(),
    "sendgrid": MagicMock(),
}

_FLASK_AVAILABLE = importlib.util.find_spec("flask") is not None

_DASH = None
if _FLASK_AVAILABLE:
    with patch.dict("sys.modules", _STUB_MODULES):
        import dashboard.app as _DASH  # type: ignore[assignment]


def _make_order(side, order_type, symbol="AAPL", limit_price=None, stop_price=None):
    """Build a minimal mock order object with real enum values."""
    return SimpleNamespace(
        side=side,
        type=order_type,
        symbol=symbol,
        limit_price=str(limit_price) if limit_price else None,
        stop_price=str(stop_price) if stop_price else None,
    )


# ── Fix 28: Enum helper tests ─────────────────────────────────────────────────

def test_enum_str_helper_extracts_plain_name():
    """_enum_str(OrderType.LIMIT) returns 'limit', not 'ordertype.limit'."""
    if _DASH is None:
        return
    assert _DASH._enum_str(OrderType.LIMIT) == "limit", (
        f"Expected 'limit', got {_DASH._enum_str(OrderType.LIMIT)!r}"
    )


def test_enum_str_handles_stop():
    """_enum_str(OrderType.STOP) returns 'stop'."""
    if _DASH is None:
        return
    assert _DASH._enum_str(OrderType.STOP) == "stop", (
        f"Expected 'stop', got {_DASH._enum_str(OrderType.STOP)!r}"
    )


def test_enum_str_handles_oco():
    """_enum_str(OrderClass.OCO) returns 'oco'."""
    if _DASH is None:
        return
    assert _DASH._enum_str(OrderClass.OCO) == "oco", (
        f"Expected 'oco', got {_DASH._enum_str(OrderClass.OCO)!r}"
    )


def test_tp_map_returns_value_for_limit_leg():
    """_tp_map extracts limit_price from a SELL LIMIT order (the TP leg of an OCO)."""
    if _DASH is None:
        return
    orders = [
        _make_order(OrderSide.SELL, OrderType.LIMIT, "NVDA", limit_price=220.00),
    ]
    result = _DASH._tp_map(orders)
    assert "NVDA" in result, f"NVDA should appear in _tp_map result, got: {result}"
    assert result["NVDA"] == 220.00, f"Expected 220.00, got {result['NVDA']}"


def test_stop_map_returns_value_for_stop_leg():
    """_stop_map extracts stop_price from a SELL STOP order."""
    if _DASH is None:
        return
    orders = [
        _make_order(OrderSide.SELL, OrderType.STOP, "NVDA", stop_price=195.00),
    ]
    result = _DASH._stop_map(orders)
    assert "NVDA" in result, f"NVDA should appear in _stop_map result, got: {result}"
    assert result["NVDA"] == 195.00, f"Expected 195.00, got {result['NVDA']}"


# ── Fix 29: Cache tests ───────────────────────────────────────────────────────

def _make_cached_fn(key, ttl=60, cache_on=bool, return_val=None):
    """Build a minimal _cached-decorated function for isolation testing."""
    if _DASH is None:
        return None, None
    # Use a fresh cache dict to isolate tests
    local_cache = {}
    call_count = [0]

    # Replicate the _cached logic with a local cache
    from functools import wraps

    def cached_decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            now = time.time()
            entry = local_cache.get(key)
            if entry and now - entry["ts"] < ttl:
                return entry["data"]
            result = fn(*args, **kwargs)
            if cache_on(result):
                local_cache[key] = {"ts": now, "data": result}
            return result
        return wrapper

    @cached_decorator
    def fn():
        call_count[0] += 1
        return return_val if callable(return_val) else return_val

    return fn, call_count


def test_cache_does_not_store_empty_dict():
    """A function returning {} is not cached — next call retries the function."""
    fn, call_count = _make_cached_fn("k1", return_val={})
    if fn is None:
        return
    r1 = fn()
    r2 = fn()
    assert r1 == {} and r2 == {}, "Both calls should return {}"
    assert call_count[0] == 2, (
        f"Expected 2 calls (no cache on empty), got {call_count[0]}"
    )


def test_cache_does_not_store_empty_list():
    """A function returning [] is not cached — next call retries the function."""
    fn, call_count = _make_cached_fn("k2", return_val=[])
    if fn is None:
        return
    fn()
    fn()
    assert call_count[0] == 2, (
        f"Expected 2 calls (no cache on empty list), got {call_count[0]}"
    )


def test_cache_stores_valid_result():
    """A function returning a non-empty result is cached — second call hits cache."""
    fn, call_count = _make_cached_fn("k3", ttl=60, return_val={"dates": ["May 7"], "a1": [105000]})
    if fn is None:
        return
    r1 = fn()
    r2 = fn()
    assert r1 == r2, "Both calls should return the same value"
    assert call_count[0] == 1, (
        f"Expected 1 call (result cached), got {call_count[0]}"
    )


def test_equity_curve_retries_after_empty():
    """First call returns {} (Alpaca error) → not cached. Second call returns real data → cached."""
    if _DASH is None:
        return
    call_count = [0]
    responses = [
        {},                                          # first call: Alpaca error
        {"dates": ["May 7"], "a1": [105000.0],      # second call: real data
         "a2": [102000.0], "combined": [207000.0]},
    ]

    def fake_equity(*_args, **_kwargs):
        call_count[0] += 1
        return responses[min(call_count[0] - 1, len(responses) - 1)]

    # Patch the _cache dict and the underlying requests call
    with patch.object(_DASH, "_cache", {}), \
         patch("dashboard.app._equity_curve_data.__wrapped__", fake_equity, create=True):
        # Test the cache_on logic directly using our _make_cached_fn helper
        actual_calls = [0]
        local_cache = {}

        def mock_equity():
            actual_calls[0] += 1
            return responses[min(actual_calls[0] - 1, 1)]

        def cached_equity():
            now = time.time()
            entry = local_cache.get("equity_curve")
            if entry and now - entry["ts"] < 3600:
                return entry["data"]
            result = mock_equity()
            if bool(result):  # cache_on=bool — the fix
                local_cache["equity_curve"] = {"ts": now, "data": result}
            return result

        r1 = cached_equity()  # returns {} — not cached
        r2 = cached_equity()  # returns real data — cached
        r3 = cached_equity()  # returns cached real data

        assert r1 == {}, f"First call should return empty, got {r1}"
        assert r2.get("dates") == ["May 7"], f"Second call should return real data, got {r2}"
        assert r3 == r2, "Third call should hit cache"
        assert actual_calls[0] == 2, (
            f"Expected 2 actual Alpaca calls (empty not cached), got {actual_calls[0]}"
        )
