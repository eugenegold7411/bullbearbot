"""
conftest.py — Shared fixtures and optional-dependency stubs.

Stub policy: if an optional third-party package is absent, a minimal namespace
stub is inserted into sys.modules so that *import* succeeds at collection time.
Stubs do NOT implement runtime behaviour — tests that invoke real package APIs
will still fail or be skipped if the package is absent.

chromadb is intentionally excluded: trade_memory.py has its own graceful
degradation (logs a WARNING and disables vector memory). Stubbing chromadb
would bypass that degradation and produce confusing AttributeErrors instead.

Test-isolation guards (must run before any production module import):
  1. PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python — required for ChromaDB on
     this protobuf 7.x install. Production systemd sets the same env var.
  2. Production log RotatingFileHandler is suppressed so test output never
     appears in logs/bot.log.
  3. divergence.py runtime + log paths are redirected to a temp directory so
     test fixtures never write to data/runtime/ or data/analytics/.
"""

# ── PRE-IMPORT ENV: must run before any module that imports google.protobuf
import os as _os

_os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")

import sys
import types
from unittest.mock import patch

import pytest

# Load .env early so test_t012's module-level load_dotenv stub (a no-op) doesn't
# prevent API keys from reaching os.environ when memory.py is imported later.
# If dotenv is absent, install a no-op stub so all production `from dotenv import
# load_dotenv` calls succeed without raising ImportError.
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except ImportError:
    _dotenv_stub = types.ModuleType("dotenv")
    _dotenv_stub.load_dotenv = lambda *a, **kw: None  # type: ignore[attr-defined]
    sys.modules.setdefault("dotenv", _dotenv_stub)


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
    ):
        setattr(_rq_mod, _name, _cls(_name))

    class _KwargsRequest:
        def __init__(self, *a, **kw):
            for k, v in kw.items():
                setattr(self, k, v)
    # Order request classes must capture kwargs so tests can assert on fields
    for _name in (
        "LimitOrderRequest", "MarketOrderRequest",
        "StopLossRequest", "StopOrderRequest", "TrailingStopOrderRequest",
        "ReplaceOrderRequest", "TakeProfitRequest",
        "OptionLegRequest",
    ):
        setattr(_rq_mod, _name, _KwargsRequest)
    for _name in (
        "AssetClass", "AssetStatus", "ContractType", "ExerciseStyle",
        "OrderClass", "OrderType",
        "PositionIntent", "PositionSide",
    ):
        setattr(_en_mod, _name, _cls(_name))
    # OrderStatus: proper StrEnum matching alpaca-py 0.43.x so attribute access
    # (OrderStatus.NEW, OrderStatus.FILLED, etc.) and value comparisons both work
    # in tests running without alpaca installed.
    from enum import Enum as _Enum
    class _OrderStatus(str, _Enum):
        NEW               = "new"
        PENDING_NEW       = "pending_new"
        ACCEPTED          = "accepted"
        PARTIALLY_FILLED  = "partially_filled"
        FILLED            = "filled"
        DONE_FOR_DAY      = "done_for_day"
        CANCELED          = "canceled"
        EXPIRED           = "expired"
        REPLACED          = "replaced"
        PENDING_CANCEL    = "pending_cancel"
        PENDING_REPLACE   = "pending_replace"
        HELD              = "held"
        REJECTED          = "rejected"
        SUSPENDED         = "suspended"
    setattr(_en_mod, "OrderStatus", _OrderStatus)
    setattr(_en_mod, "OrderSide", types.SimpleNamespace(BUY="buy", SELL="sell"))
    setattr(_en_mod, "TimeInForce", types.SimpleNamespace(
        GTC="gtc", DAY="day", IOC="ioc", FOK="fok", OPG="opg", CLS="cls",
    ))
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
                  "StockBarsRequest", "StockLatestQuoteRequest",
                  "OptionSnapshotRequest", "OptionLatestQuoteRequest"):
        setattr(_dr_mod, _name, _cls(_name))

    class _StockLatestTradeRequest:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)
    setattr(_dr_mod, "StockLatestTradeRequest", _StockLatestTradeRequest)

    _de_mod = sys.modules["alpaca.data.enums"]
    setattr(_de_mod, "DataFeed", types.SimpleNamespace(IEX="iex", SIP="sip"))

    _tf_mod = sys.modules["alpaca.data.timeframe"]
    _TimeFrame_stub = _cls("TimeFrame")
    # market_data.py uses TimeFrame.Day, .Hour, etc. as timeframe arguments to
    # CryptoBarsRequest/StockBarsRequest.  Tests that mock the bar-fetch client
    # still need these attributes to exist so the CryptoBarsRequest constructor
    # doesn't raise AttributeError before the client mock is reached.
    for _tf_attr in ("Day", "Hour", "Minute", "Week", "Month"):
        setattr(_TimeFrame_stub, _tf_attr, _tf_attr)
    setattr(_tf_mod, "TimeFrame", _TimeFrame_stub)
    setattr(_tf_mod, "TimeFrameUnit", _cls("TimeFrameUnit"))


if not _alpaca_compatible():
    _stub_alpaca_tree()
else:
    # Pre-load alpaca.data subpackages when the real alpaca is installed.
    # test_sprint2_a2_mode.py inserts MagicMock stubs at module-collection time
    # using `if not in sys.modules` guards — pre-loading the real submodules here
    # ensures those guards find them already populated and skip the MagicMock.
    try:
        from alpaca.data.enums import DataFeed as _dfe  # noqa: F401
        from alpaca.data.historical import (  # noqa: F401
            CryptoHistoricalDataClient as _chdc,
        )
        from alpaca.data.historical.news import NewsClient as _nc  # noqa: F401
        from alpaca.data.requests import NewsRequest as _nr  # noqa: F401
        from alpaca.data.timeframe import TimeFrame as _tf  # noqa: F401
    except Exception:
        pass
    # Pre-load alpaca.trading subpackages so module-collection-time guards in
    # test_a2_decision_store.py, test_a2_feature_pack.py, and test_a2_integration.py
    # (all using `if name not in sys.modules`) find them already populated and skip
    # inserting MagicMock/None stubs that would contaminate options_executor tests.
    try:
        from alpaca.trading.enums import PositionIntent as _pi  # noqa: F401
        from alpaca.trading.requests import OptionLegRequest as _olr  # noqa: F401
    except Exception:
        pass

_stub_if_absent("anthropic")
_stub_if_absent("twilio", ["rest"])
_stub_if_absent("pandas_ta")
_stub_if_absent("yfinance")
# Ensure yfinance.Ticker exists so monkeypatch.setattr("yfinance.Ticker", ...) works
try:
    import yfinance as _yf_stub
    if not hasattr(_yf_stub, "Ticker"):
        _yf_stub.Ticker = type("Ticker", (), {"__init__": lambda self, s: None})
except Exception:
    pass
_stub_if_absent("pandas", ["core", "core.frame"])
# Ensure pandas.DataFrame exists so pd.DataFrame(...) in tests works
try:
    import pandas as _pd_stub
    if not hasattr(_pd_stub, "DataFrame"):
        class _DataFrame:
            def __init__(self, data=None, *a, **kw):
                self._data = data or []
            @property
            def empty(self):
                return not self._data
            def to_dict(self, orient=None):
                return self._data
        _pd_stub.DataFrame = _DataFrame
except Exception:
    pass
_stub_if_absent("requests")
# Ensure requests.get exists so monkeypatch.setattr("requests.get", ...) works
try:
    import requests as _req_stub
    if not hasattr(_req_stub, "get"):
        _req_stub.get = lambda *a, **kw: None
except Exception:
    pass
_stub_if_absent("pydantic")
_stub_if_absent("sendgrid", ["helpers", "helpers.mail"])
_stub_if_absent("feedparser")
# Ensure feedparser.parse exists (used by macro_wire.py)
try:
    import feedparser as _fp_stub
    if not hasattr(_fp_stub, "parse"):
        _fp_stub.parse = lambda url, *a, **kw: types.SimpleNamespace(entries=[])
except Exception:
    pass

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


# ── Logging isolation ────────────────────────────────────────────────────────
# log_setup._configure() is idempotent on a `_configured` flag. If we attach
# a stderr handler to the root logger and set `_configured = True` BEFORE any
# production module imports log_setup, _configure() returns early and never
# attaches the RotatingFileHandler that points at logs/bot.log.

import logging as _logging
import logging.handlers as _logging_handlers  # noqa: F401  (binds for the strip below)

_root_logger = _logging.getLogger()
_root_logger.setLevel(_logging.WARNING)  # quiet during tests; caplog overrides per-test
if not any(isinstance(h, _logging.StreamHandler) for h in _root_logger.handlers):
    _stderr_handler = _logging.StreamHandler()
    _stderr_handler.setLevel(_logging.WARNING)
    _stderr_handler.setFormatter(_logging.Formatter("%(name)s: %(message)s"))
    _root_logger.addHandler(_stderr_handler)

try:
    import log_setup as _log_setup
    _log_setup._configured = True
except Exception:
    pass

# Belt-and-suspenders: strip any FileHandler that snuck in via early imports.
for _h in list(_root_logger.handlers):
    if isinstance(_h, _logging.FileHandler):
        _root_logger.removeHandler(_h)
        try:
            _h.close()
        except Exception:
            pass


# ── Divergence state isolation ───────────────────────────────────────────────
# divergence.py defines four module-level paths that point at data/runtime/
# and data/analytics/. Test fixtures that exercise the divergence engine
# would otherwise pollute production state files. Redirect to a temp dir
# created once per pytest session.

import tempfile as _tempfile
from pathlib import Path as _Path

_TEST_TMP = _Path(_tempfile.mkdtemp(prefix="bullbearbot_test_"))
_TEST_RUNTIME_DIR = _TEST_TMP / "runtime"
_TEST_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
(_TEST_TMP / "analytics").mkdir(parents=True, exist_ok=True)

try:
    import divergence as _div
    _div.RUNTIME_DIR             = _TEST_RUNTIME_DIR
    _div.DIVERGENCE_LOG          = _TEST_TMP / "analytics" / "divergence_log.jsonl"
    _div.MODE_TRANSITION_LOG     = _TEST_RUNTIME_DIR / "mode_transitions.jsonl"
    _div.DIVERGENCE_COUNTS_PATH  = _TEST_RUNTIME_DIR / "divergence_counts.json"
except Exception:
    pass


# Pre-import trade_memory so the real module is in sys.modules before any test
# file's module-level _stub("trade_memory", ...) can replace it with an incomplete stub.
try:
    import trade_memory as _tm  # noqa: F401
except Exception:
    pass

# Pre-import chromadb so that test_cr_conviction_reconciliation.py (and similar
# files) cannot install a MagicMock stub at collection time. Without this,
# chromadb is absent from sys.modules when those files collect (trade_memory
# uses lazy init), so their "if not in sys.modules" guard does not fire, and the
# MagicMock stub poisons trade_memory._get_collections() for the duration of the
# session — breaking test_scratchpad_memory.py even though real chromadb is
# installed. See PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION env-var set above.
try:
    import chromadb as _chromadb_preload  # noqa: F401
except Exception:
    pass

# Pre-import modules that test_morning_brief_held_exempt.py stubs at module level via
# sys.modules.setdefault(). Without this, bare stubs (no attributes) get locked into
# sys.modules before later tests need the real functions (e.g. get_core, save_structure).
try:
    import watchlist_manager as _wm  # noqa: F401
except Exception:
    pass
try:
    import options_state as _os_mod  # noqa: F401
except Exception:
    pass
try:
    import insider_intelligence as _ii  # noqa: F401
except Exception:
    pass
try:
    import earnings_calendar_lookup as _ecl  # noqa: F401
except Exception:
    pass
try:
    import data_warehouse as _dw  # noqa: F401
except Exception:
    pass


# ── Marker-based auto-skip ───────────────────────────────────────────────────
# requires_chromadb: skip automatically when chromadb is absent from the env.
# requires_prompts: skip when the private prompt directory is absent.

def pytest_runtest_setup(item: pytest.Item) -> None:
    if item.get_closest_marker("requires_chromadb"):
        from unittest.mock import MagicMock as _MagicMock
        _chromadb = sys.modules.get("chromadb")
        # Skip when chromadb is absent or was stubbed with a MagicMock by another test file.
        if _chromadb is None or isinstance(_chromadb, _MagicMock):
            pytest.skip("chromadb not installed in this environment")


# ── Fixtures ──────────────────────────────────────────────────────────────────

# Minimal strategy_config for CI (server has the real file; CI and local dev do not).
# Values match what the config-assertion tests expect. Written once per session,
# cleaned up afterward. Never overwrites an existing file.
_CI_STRATEGY_CONFIG: dict = {
    "version": 2,
    "active_strategy": "momentum",
    "parameters": {
        "max_positions": 30,
        "max_position_pct_capacity": 0.15,
        "_max_position_pct_capacity_note": (
            "Enforced by risk_kernel.size_position() as single-name cap upper bound. "
            "Changed from max_position_pct_equity in Sprint 10 aggressiveness pass."
        ),
        "margin_sizing_multiplier": 4.0,
        "margin_sizing_conviction_thresholds": {"high": 0.65, "medium": 0.5},
        "margin_sizing_multiplier_tiers": {
            "medium":      {"min": 0.5,   "max": 0.6499, "multiplier": 1.0},
            "high":        {"min": 0.65,  "max": 0.7249, "multiplier": 2.0},
            "strong_high": {"min": 0.725, "max": 0.7999, "multiplier": 3.0},
            "very_high":   {"min": 0.8,   "max": 1.0,    "multiplier": 4.0},
        },
        "max_crypto_margin_multiplier": 2.0,
        "stop_loss_pct_core": 0.03,
        "stop_loss_pct_intraday": 0.018,
        "take_profit_multiple": 2.5,
        "catalyst_tag_required_for_entry": True,
        "catalyst_tag_disallowed_values": ["no", "none", "null", ""],
        "session_gate_enforce": True,
        "vix_threshold_caution": 25,
        "max_daily_drawdown_pct": 0.025,
        "margin_authorized": True,
        "add_conviction_gate": 0.6,
        "max_short_exposure_pct": 1.0,
        "max_short_position_pct": 0.05,
        "max_day_trades_rolling_5day": 999,
    },
    "position_sizing": {
        "core_tier_pct": 0.20,
        "dynamic_tier_pct": 0.15,
        "intraday_tier_pct": 0.05,
        "cash_reserve_pct": 0.05,
        "max_total_exposure_pct": 0.95,
    },
    "account2": {
        "enabled": True,
        "paper_confidence_floor": 0.70,
        "live_confidence_floor": 0.85,
        "liquidity_gates": {
            "min_open_interest": 50,
            "pre_debate_oi_floor": 40,
            "min_volume": 20,
            "max_spread_pct": 0.18,
        },
        "iron_condor_short_delta_target": 0.175,
        "iron_condor_spread_width": 5.0,
        "iron_condor_min_credit_usd": 50,
        "iron_butterfly_wing_width": 10.0,
        "iron_butterfly_min_credit_usd": 100,
        "max_open_positions": 20,
        "capital_utilization_target": 0.9,
        "short_put_delta_target": 0.275,
        "short_put_delta_tolerance": 0.05,
        "short_put_min_premium_usd": 50,
        "short_put_stop_loss_multiple": 2.0,
    },
    "a2_veto_thresholds": {
        "max_bid_ask_spread_pct": 0.18,
        "min_open_interest": 100,
        "max_theta_decay_pct": 0.05,
        "min_dte": 5,
        "min_expected_value": 0.0,
        "_tuned": "CI fixture — matches production veto threshold values.",
    },
    "a2_rollback": {
        "disable_candidate_generation": False,
        "disable_bounded_debate": False,
        "force_no_trade": False,
    },
    "a2_router": {
        "earnings_dte_blackout": 2,
        "min_liquidity_score": 0.25,
        "macro_iv_gate_rank": 70,
        "iv_env_blackout": [],
        "iron_iv_rank_min": 50,
        "short_put_iv_rank_min": 50,
        "post_earnings_window_premarket": 2,
        "post_earnings_window_postmarket": 1,
        "post_earnings_window_unknown": 1,
        "post_earnings_iv_rank_min": 75,
        "post_earnings_iv_already_crushed_threshold": 15,
        "pre_earnings_credit_spread_enabled": True,
        "pre_earnings_iv_rank_min": 85,
        "pre_earnings_dte_min": 7,
        "pre_earnings_dte_max": 14,
    },
    "portfolio_allocator": {
        "trim_score_threshold": 5,
        "size_trim_enabled": True,
        "size_trim_tolerance_pct": 2.0,
        "trim_severity": [
            {"score_max": 2, "trim_pct": 0.75},
            {"score_max": 4, "trim_pct": 0.50},
            {"score_max": 5, "trim_pct": 0.25},
        ],
        "enable_shadow": True,
        "enable_live": False,
        "replace_score_gap": 15,
        "weight_deadband": 0.02,
        "min_rebalance_notional": 500,
    },
    "signal_weights": {
        "momentum_weight": 0.35,
        "mean_reversion_weight": 0.2,
        "news_sentiment_weight": 0.3,
        "cross_sector_weight": 0.15,
    },
    "signal_source_weights": {
        "congressional": "medium",
        "form4_insider": "medium",
        "earnings": "high",
        "macro": "medium",
    },
    "director_notes": {
        "active_context": "CI fixture — no active director override.",
        "expiry": "2099-12-31",
        "priority": "normal",
    },
    "time_bound_actions": [],
}


@pytest.fixture(autouse=True)
def _isolate_allocator_cooldown(tmp_path):
    """Route all portfolio_allocator cooldown disk I/O to a temp file.

    Prevents test runs from writing SYM0/SYM1/SYM2/SYM3/SYM4/XYZ entries
    to the production allocator_cooldown.json file in data/runtime/.
    """
    try:
        import portfolio_allocator as pa
        fake = tmp_path / "allocator_cooldown_test.json"
        with patch.object(pa, "_COOLDOWN_PATH", fake):
            yield fake
    except ImportError:
        yield None


@pytest.fixture(autouse=True)
def _clear_cycle_committed():
    """Clear per-cycle committed-symbols state between tests.

    Prevents REPLACE/ADD deduplication from leaking committed symbols
    from one test into the next (bot_stage4_execution._cycle_committed is
    a module-level set cleared by clear_cycle_committed() each real cycle).
    """
    try:
        from bot_stage4_execution import clear_cycle_committed
        clear_cycle_committed()
        yield
        clear_cycle_committed()
    except ImportError:
        yield


@pytest.fixture(scope="session", autouse=True)
def _strategy_config_ci():
    """Write a minimal strategy_config.json at the project root for CI.

    Only creates the file when it is absent — the server has the real one.
    Cleaned up at session teardown so it does not linger between runs.
    """
    import json as _json

    _cfg_path = _Path(__file__).parent.parent / "strategy_config.json"
    _created = False

    if not _cfg_path.exists():
        _cfg_path.write_text(_json.dumps(_CI_STRATEGY_CONFIG, indent=2))
        _created = True

    yield

    if _created:
        try:
            _cfg_path.unlink()
        except Exception:
            pass


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
