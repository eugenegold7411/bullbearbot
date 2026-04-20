"""
conftest.py — Shared fixtures and optional-dependency stubs.

Stub policy: if an optional third-party package is absent, a minimal namespace
stub is inserted into sys.modules so that *import* succeeds at collection time.
Stubs do NOT implement runtime behaviour — tests that invoke real package APIs
will still fail or be skipped if the package is absent.

chromadb is intentionally excluded: trade_memory.py has its own graceful
degradation (logs a WARNING and disables vector memory). Stubbing chromadb
would bypass that degradation and produce confusing AttributeErrors instead.
"""

import sys
import types

import pytest

# Load .env early so test_t012's module-level load_dotenv stub (a no-op) doesn't
# prevent API keys from reaching os.environ when memory.py is imported later.
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except ImportError:
    pass


# ── Optional-dependency stubs ─────────────────────────────────────────────────

def _stub_if_absent(top_name: str, sub_names: list | None = None) -> None:
    """Insert a bare namespace module tree if the package is not installed."""
    if top_name in sys.modules:
        return
    try:
        __import__(top_name)
        return
    except ImportError:
        pass
    top = types.ModuleType(top_name)
    sys.modules[top_name] = top
    for sub in (sub_names or []):
        full = f"{top_name}.{sub}"
        if full not in sys.modules:
            mod = types.ModuleType(full)
            sys.modules[full] = mod
            parts = sub.split(".")
            parent = top
            for part in parts[:-1]:
                parent = sys.modules.get(f"{top_name}.{'.'.join(sub.split('.')[:sub.split('.').index(part)+1])}", parent)
            setattr(parent, parts[-1], mod)


# ── alpaca compatibility check ────────────────────────────────────────────────
# The server runs alpaca-py 0.43.2 with TradingClient at alpaca.trading.client.
# If the local install is absent or incompatible, evict it and insert full stubs
# so tests can import without errors.  Stubs provide no runtime behaviour.

def _alpaca_compatible() -> bool:
    """Return True only if alpaca-py 0.43.x is installed with the expected API."""
    try:
        from alpaca.trading.client import TradingClient  # noqa: F401
        return True
    except Exception:
        return False


def _stub_alpaca_tree() -> None:
    """Evict any installed alpaca and replace with bare namespace stubs."""
    for key in list(sys.modules):
        if key == "alpaca" or key.startswith("alpaca."):
            del sys.modules[key]

    _alpaca_sub_names = [
        "trading", "trading.client", "trading.requests",
        "trading.enums", "trading.models",
        "data", "data.historical", "data.historical.requests",
        "data.historical.stock", "data.historical.crypto",
        "data.historical.news",
        "data.requests",
        "data.enums",
        "data.timeframe",
    ]
    top = types.ModuleType("alpaca")
    sys.modules["alpaca"] = top
    for sub in _alpaca_sub_names:
        full = f"alpaca.{sub}"
        mod  = types.ModuleType(full)
        sys.modules[full] = mod
        parts  = sub.split(".")
        parent = top
        for i, part in enumerate(parts[:-1]):
            parent = sys.modules[f"alpaca.{'.'.join(parts[:i+1])}"]
        setattr(parent, parts[-1], mod)

    # Populate the classes the production code imports
    _tc_mod  = sys.modules["alpaca.trading.client"]
    _rq_mod  = sys.modules["alpaca.trading.requests"]
    _en_mod  = sys.modules["alpaca.trading.enums"]
    _mo_mod  = sys.modules["alpaca.trading.models"]

    def _cls(name: str):
        return type(name, (), {"__init__": lambda self, *a, **kw: None})

    for _name in ("TradingClient",):
        setattr(_tc_mod, _name, _cls(_name))
    for _name in (
        "ClosePositionRequest", "GetOptionContractsRequest", "GetOrdersRequest",
        "GetOrderByIdRequest", "GetAssetsRequest", "GetPortfolioHistoryRequest",
        "LimitOrderRequest", "MarketOrderRequest",
        "StopLossRequest", "StopOrderRequest", "TakeProfitRequest",
        "TrailingStopOrderRequest",
    ):
        setattr(_rq_mod, _name, _cls(_name))

    class _KwargsRequest:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)
    setattr(_rq_mod, "ReplaceOrderRequest", _KwargsRequest)
    for _name in (
        "AssetClass", "AssetStatus", "ContractType", "ExerciseStyle",
        "OrderClass", "OrderStatus", "OrderType",
        "PositionSide", "TimeInForce",
    ):
        setattr(_en_mod, _name, _cls(_name))
    setattr(_en_mod, "OrderSide", types.SimpleNamespace(BUY="buy", SELL="sell"))
    setattr(_en_mod, "QueryOrderStatus", types.SimpleNamespace(OPEN="open", CLOSED="closed"))
    for _name in ("TradeAccount", "Position", "Order", "Asset"):
        setattr(_mo_mod, _name, _cls(_name))

    _dh_mod = sys.modules["alpaca.data.historical"]
    setattr(_dh_mod, "CryptoHistoricalDataClient", _cls("CryptoHistoricalDataClient"))

    class _StockHistoricalDataClient:
        def __init__(self, *a, **kw): pass
        def get_stock_latest_trade(self, req):
            sym = getattr(req, "symbol_or_symbols", "UNKNOWN")
            return {sym: types.SimpleNamespace(price="100.0")}
    setattr(_dh_mod, "StockHistoricalDataClient", _StockHistoricalDataClient)

    _news_mod = sys.modules["alpaca.data.historical.news"]
    for _name in ("NewsRequest", "NewsClient"):
        setattr(_news_mod, _name, _cls(_name))

    _stock_mod = sys.modules["alpaca.data.historical.stock"]
    for _name in ("StockBarsRequest", "StockLatestQuoteRequest"):
        setattr(_stock_mod, _name, _cls(_name))

    _crypto_mod = sys.modules["alpaca.data.historical.crypto"]
    for _name in ("CryptoBarsRequest", "CryptoLatestQuoteRequest"):
        setattr(_crypto_mod, _name, _cls(_name))

    _dr_mod = sys.modules["alpaca.data.requests"]
    for _name in ("CryptoBarsRequest", "CryptoLatestTradeRequest",
                  "CryptoLatestQuoteRequest", "NewsRequest",
                  "StockBarsRequest", "StockLatestQuoteRequest"):
        setattr(_dr_mod, _name, _cls(_name))

    class _StockLatestTradeRequest:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)
    setattr(_dr_mod, "StockLatestTradeRequest", _StockLatestTradeRequest)

    _de_mod = sys.modules["alpaca.data.enums"]
    setattr(_de_mod, "DataFeed", types.SimpleNamespace(IEX="iex", SIP="sip"))

    _tf_mod = sys.modules["alpaca.data.timeframe"]
    for _name in ("TimeFrame", "TimeFrameUnit"):
        setattr(_tf_mod, _name, _cls(_name))


if not _alpaca_compatible():
    _stub_alpaca_tree()

_stub_if_absent("anthropic")
_stub_if_absent("twilio", ["rest"])
_stub_if_absent("pandas_ta")
_stub_if_absent("yfinance")
_stub_if_absent("pandas", ["core", "core.frame"])
_stub_if_absent("requests")
_stub_if_absent("pydantic")
_stub_if_absent("sendgrid", ["helpers", "helpers.mail"])

# Ensure anthropic.Anthropic exists with a .messages attribute even if the stub is bare
try:
    import anthropic as _ant
    if not hasattr(_ant, "Anthropic"):
        _messages_stub = types.SimpleNamespace(create=lambda *a, **kw: None)
        _ant.Anthropic = type("Anthropic", (), {
            "__init__": lambda self, *a, **kw: None,
            "messages": _messages_stub,
        })
    elif not hasattr(_ant.Anthropic, "messages"):
        _ant.Anthropic.messages = types.SimpleNamespace(create=lambda *a, **kw: None)
except Exception:
    pass


# Pre-import trade_memory so the real module is in sys.modules before any test
# file's module-level _stub("trade_memory", ...) can replace it with an incomplete stub.
try:
    import trade_memory as _tm  # noqa: F401
except Exception:
    pass


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def kernel_config() -> dict:
    """
    Minimal strategy_config dict for risk_kernel tests.

    Shape mirrors strategy_config.json without any runtime side-effects.
    Session-scoped: built once, shared across all kernel test modules.
    """
    return {
        "parameters": {
            "max_positions": 15,
            "stop_loss_pct_core": 0.035,
            "stop_loss_pct_intraday": 0.018,
            "take_profit_multiple": 2.5,
            "catalyst_tag_required_for_entry": True,
            "catalyst_tag_disallowed_values": ["", "none", "null", "no"],
            "session_gate_enforce": True,
        },
        "position_sizing": {
            "core_tier_pct": 0.15,
            "dynamic_tier_pct": 0.08,
            "intraday_tier_pct": 0.05,
        },
        "time_bound_actions": [],
    }
