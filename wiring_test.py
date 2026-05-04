"""
wiring_test.py — Full pipeline wiring verification with synthetic data.

Run via:
    python scheduler.py --dry-run-wiring
    python wiring_test.py

Makes real Haiku calls for A1 signal scoring, A1 decision, and A2 debate.
No real orders submitted to Alpaca. Writes real ChromaDB records (cleaned up
after). Prints a per-stage PASS/FAIL/WARN report and exits non-zero on any failure.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
import traceback
import unittest.mock as mock
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_BOT_DIR            = Path(__file__).parent
_SIGNAL_SCORES_PATH = _BOT_DIR / "data" / "market" / "signal_scores.json"
_SIGNAL_SCORES_BAK  = _BOT_DIR / "data" / "market" / "signal_scores.json.wiring_bak"
_A2_DECISIONS_DIR   = _BOT_DIR / "data" / "account2" / "decisions"
_TRADES_JSONL       = _BOT_DIR / "logs" / "trades.jsonl"
# Correction #1: use data/runtime/scheduler.pid (same as scheduler._PID_FILE)
_PID_FILE           = _BOT_DIR / "data" / "runtime" / "scheduler.pid"

# ---------------------------------------------------------------------------
# Mutable state collected during the run (used by cleanup)
# ---------------------------------------------------------------------------
_WIRING_A1_VECTOR_ID: str = ""          # ChromaDB doc id — captured after write
_intercepted_orders: list[dict] = []    # every submit_order call recorded here
_a2_files_before: set[str] = set()      # A2 decision files that existed pre-cycle
_original_log_trade = None              # saved for restore
_recorded_stop_reasons: list[str] = []  # stop_reason per Claude API call during test
_claude_create_patcher: tuple | None = None  # (messages_obj, original_create)


# ---------------------------------------------------------------------------
# Mock order infrastructure
# ---------------------------------------------------------------------------
@dataclass
class MockOrder:
    id:               str   = field(default_factory=lambda: f"WIRING-{uuid.uuid4().hex[:8]}")
    status:           str   = "filled"
    filled_qty:       str   = "1"
    filled_avg_price: str   = "500.00"
    symbol:           str   = "SPY"
    side:             str   = "buy"
    order_type:       str   = "market"
    legs:             list  = field(default_factory=list)


def _mock_submit_order(req) -> MockOrder:
    symbol = getattr(req, "symbol", None) or getattr(req, "underlying_symbol", "UNKNOWN")
    order  = MockOrder(symbol=str(symbol))
    _intercepted_orders.append({
        "symbol":   str(symbol),
        "req_type": type(req).__name__,
        "mock_id":  order.id,
        "ts":       datetime.now(timezone.utc).isoformat(),
    })
    return order


def _make_mock_alpaca_client():
    client = mock.MagicMock()
    client.submit_order.side_effect          = _mock_submit_order
    client.get_account.return_value          = mock.MagicMock(
        equity                  = "100000.0",
        cash                    = "100000.0",
        buying_power            = "100000.0",
        daytrading_buying_power = "100000.0",
        options_buying_power    = "100000.0",
        daytrade_count          = 0,
    )
    client.get_all_positions.return_value    = []
    client.get_orders.return_value           = []
    client.get_open_orders.return_value      = []
    return client


# ---------------------------------------------------------------------------
# Synthetic data
# ---------------------------------------------------------------------------
_SYNTHETIC_MD = {
    "prices":       {"TEST_AAPL": 185.0, "TEST_MSFT": 420.0, "SPY": 500.0},
    "changes":      {"TEST_AAPL": 0.012, "TEST_MSFT": -0.005, "SPY": 0.004},
    "volumes":      {"TEST_AAPL": 55_000_000, "TEST_MSFT": 22_000_000, "SPY": 80_000_000},
    "vix":          18.5,
    "spy_change":   0.004,
    "qqq_change":   0.008,
    "sector_etfs":  {"XLK": 0.012, "XLF": -0.003},
    "macro_wire":   {"regime": "risk_on", "signal": "neutral"},
    "breaking_news": "",
}

# Correction #3: use SPY so A2 debate and execution stages are exercised
_SYNTHETIC_SIGNAL_SCORES = {
    "scored_symbols": {
        "SPY": {"score": 0.72, "tier": "strong", "regime": "risk_on"},
    },
}


def _build_synthetic_precycle_state():
    """Construct PreCycleState directly, bypassing live Alpaca and market data fetches."""
    from bot_stage0_precycle import PreCycleState

    mock_account = mock.MagicMock()
    mock_account.equity         = "100000.0"
    mock_account.cash           = "100000.0"
    mock_account.buying_power   = "100000.0"
    mock_account.daytrade_count = 0

    return PreCycleState(
        account              = mock_account,
        positions            = [],
        equity               = 100_000.0,
        cash                 = 100_000.0,
        buying_power_float   = 100_000.0,
        long_val             = 0.0,
        exposure             = 0.0,
        allow_live_orders    = False,
        allow_new_entries    = True,
        pf_result            = None,
        wl                   = {"TEST_AAPL": {}, "TEST_MSFT": {}},
        symbols_stock        = ["TEST_AAPL", "TEST_MSFT"],
        symbols_crypto       = [],
        md                   = _SYNTHETIC_MD,
        crypto_context       = "[WIRING TEST — no crypto data]",
        cfg                  = {},
        recent_decisions     = "[WIRING TEST — no prior decisions]",
        ticker_lessons       = "[WIRING TEST — no ticker lessons]",
        vector_memories      = "[WIRING TEST — no vector memories]",
        similar_scenarios    = [],
        strategy_config_note = "[WIRING TEST — synthetic data]",
        pi_data              = {},
        recon_log            = [],
        recon_diff           = None,
        snapshot             = None,
        a1_mode              = None,
        exit_status_str      = "[WIRING TEST — no exits]",
        allocator_output     = None,
    )


# ---------------------------------------------------------------------------
# Stage result tracking
# ---------------------------------------------------------------------------
@dataclass
class StageResult:
    name:    str
    status:  str        # "PASS" | "FAIL" | "SKIP" | "WARN"
    detail:  str  = ""
    elapsed: float = 0.0


_results: list[StageResult] = []


def _record(name: str, status: str, detail: str = "", elapsed: float = 0.0) -> None:
    _results.append(StageResult(name=name, status=status, detail=detail, elapsed=elapsed))


# ---------------------------------------------------------------------------
# Concurrent bot guard (correction #1)
# ---------------------------------------------------------------------------
def _check_no_live_bot() -> tuple[bool, str]:
    """Return (safe, message). safe=False means a live bot is running."""
    import os as _os
    if not _PID_FILE.exists():
        return True, ""
    try:
        existing_pid = int(_PID_FILE.read_text().strip())
        _os.kill(existing_pid, 0)   # signal 0 = liveness probe; no signal sent
        return False, f"PID={existing_pid} is running at {_PID_FILE}"
    except (ProcessLookupError, PermissionError):
        return True, ""   # stale lock
    except Exception:
        return True, ""   # unreadable — treat as stale


# ---------------------------------------------------------------------------
# trades.jsonl intercept (correction #5)
# Wraps log_setup.log_trade so every record written during the wiring test
# carries "wiring_test": True, making cleanup reliable.
# ---------------------------------------------------------------------------
def _install_trade_log_intercept() -> None:
    global _original_log_trade
    import log_setup as _ls
    _original_log_trade = _ls.log_trade

    def _wiring_log_trade(record: dict) -> None:
        record["wiring_test"] = True
        _original_log_trade(record)

    _ls.log_trade = _wiring_log_trade
    # Patch in modules that may have already bound the name via 'from log_setup import log_trade'
    try:
        import order_executor as _oe
        _oe.log_trade = _wiring_log_trade
    except Exception:
        pass
    try:
        import exit_manager as _em
        _em.log_trade = _wiring_log_trade
    except Exception:
        pass


def _uninstall_trade_log_intercept() -> None:
    global _original_log_trade
    if _original_log_trade is None:
        return
    import log_setup as _ls
    _ls.log_trade = _original_log_trade
    try:
        import order_executor as _oe
        _oe.log_trade = _original_log_trade
    except Exception:
        pass
    try:
        import exit_manager as _em
        _em.log_trade = _original_log_trade
    except Exception:
        pass
    _original_log_trade = None


# ---------------------------------------------------------------------------
# Claude API stop_reason recorder (addition A)
# ---------------------------------------------------------------------------
def _install_claude_stop_recorder() -> None:
    global _claude_create_patcher
    try:
        from bot_clients import _get_claude
        client = _get_claude()
        orig = client.messages.create

        def _recording_create(*args, **kwargs):
            resp = orig(*args, **kwargs)
            _recorded_stop_reasons.append(resp.stop_reason or "unknown")
            return resp

        client.messages.create = _recording_create
        _claude_create_patcher = (client.messages, orig)
    except Exception as exc:
        _recorded_stop_reasons.append(f"intercept_failed:{exc}")


def _uninstall_claude_stop_recorder() -> None:
    global _claude_create_patcher
    if _claude_create_patcher is None:
        return
    obj, orig = _claude_create_patcher
    try:
        del obj.__dict__["create"]
    except (KeyError, AttributeError):
        try:
            obj.create = orig
        except Exception:
            pass
    _claude_create_patcher = None


# ---------------------------------------------------------------------------
# A1 pipeline
# ---------------------------------------------------------------------------
def _run_a1_pipeline() -> None:
    global _WIRING_A1_VECTOR_ID

    # D-02: synthetic state
    t = time.monotonic()
    try:
        state = _build_synthetic_precycle_state()
        _record("D-02  synthetic_state", "PASS",
                "PreCycleState constructed", time.monotonic() - t)
    except Exception:
        _record("D-02  synthetic_state", "FAIL", traceback.format_exc(limit=3))
        return  # cannot continue without state

    # D-03: classify_regime
    # classify_regime() returns {regime_score, bias, session_theme, ...} — no top-level 'regime' key.
    t = time.monotonic()
    regime: dict = {"regime_score": 50, "bias": "neutral"}
    try:
        from bot_stage1_regime import classify_regime
        regime = classify_regime(state.md, {})
        if not isinstance(regime, dict) or ("regime_score" not in regime and "bias" not in regime):
            _record("D-03  classify_regime", "FAIL",
                    f"missing regime_score/bias: {list(regime.keys())}", time.monotonic() - t)
        else:
            _record("D-03  classify_regime", "PASS",
                    f"bias={regime.get('bias', '?')}  score={regime.get('regime_score', '?')}",
                    time.monotonic() - t)
    except Exception:
        _record("D-03  classify_regime", "FAIL", traceback.format_exc(limit=3))

    # D-04: score_signals_layered — real Haiku call, TEST_ symbols
    t = time.monotonic()
    signals: dict = {}
    try:
        from bot_stage2_signal import score_signals_layered
        signals = score_signals_layered(state.symbols_stock, regime, state.md)
        scored  = signals.get("scored_symbols", signals) if isinstance(signals, dict) else {}
        _record("D-04  score_signals", "PASS",
                f"symbols_scored={len(scored)}", time.monotonic() - t)
        # B. Signal schema validation
        bad_schema = [
            s for s, d in scored.items()
            if not isinstance(d, dict) or "score" not in d
            or "direction" not in d or "tier" not in d
        ]
        if bad_schema:
            _record("D-04b signal_schema", "WARN",
                    f"{len(bad_schema)} entries missing score/direction/tier: {bad_schema[:3]}")
        else:
            _record("D-04b signal_schema", "PASS",
                    f"all {len(scored)} entries valid")
    except Exception:
        _record("D-04  score_signals", "FAIL", traceback.format_exc(limit=3))

    # D-04c: crypto_signal_completeness — calls REAL get_crypto_signals() and verifies
    # RSI and MACD contain numeric values (not '?'). Missing signals = broken pipeline.
    t = time.monotonic()
    try:
        import re as _re  # noqa: PLC0415

        from market_data import get_crypto_signals  # noqa: PLC0415
        _crypto_result = get_crypto_signals(["BTC/USD", "ETH/USD"])
        _signal_str = _crypto_result[0] if isinstance(_crypto_result, tuple) else _crypto_result
        _missing: list[str] = []
        for _sym in ("BTC/USD", "ETH/USD"):
            if _sym not in _signal_str:
                _missing.append(f"{_sym}:absent")
                continue
            for _field, _pat in (("RSI", r"RSI=([0-9.]+)"), ("MACD", r"MACD=([+-]?[0-9.]+)")):
                if _field + "=" not in _signal_str:
                    _missing.append(f"{_sym}:{_field} field absent")
                elif not _re.search(_pat, _signal_str):
                    _missing.append(f"{_sym}:{_field}=? (pandas_ta not computing)")
        if _missing:
            _record("D-04c crypto_signal_completeness", "FAIL",
                    f"missing required fields: {_missing}", time.monotonic() - t)
        else:
            _rsi_vals = _re.findall(r"RSI=([0-9.]+)", _signal_str)
            _record("D-04c crypto_signal_completeness", "PASS",
                    f"BTC RSI={_rsi_vals[0] if _rsi_vals else '?'}, all fields numeric",
                    time.monotonic() - t)
    except Exception:
        _record("D-04c crypto_signal_completeness", "FAIL", traceback.format_exc(limit=3))

    # D-05: build_compact_prompt
    t = time.monotonic()
    prompt = ""
    try:
        from bot_stage3_decision import build_compact_prompt
        prompt = build_compact_prompt(
            account            = state.account,
            positions          = state.positions,
            md                 = state.md,
            session_tier       = "market",
            regime_obj         = regime,
            signal_scores_obj  = signals,
            time_bound_actions = [],
            pi_data            = state.pi_data,
            exit_status        = state.exit_status_str,
        )
        if not prompt or len(prompt) < 100:
            _record("D-05  build_compact_prompt", "FAIL",
                    f"prompt too short ({len(prompt)} chars)", time.monotonic() - t)
        else:
            _record("D-05  build_compact_prompt", "PASS",
                    f"len={len(prompt)}", time.monotonic() - t)
            # H. Prompt section presence check
            required_sections = [
                "=== ACCOUNT & RISK ===",
                "=== MARKET CONTEXT ===",
                "=== TOP SIGNALS",
                "=== YOUR TASK ===",
            ]
            missing_sections = [s for s in required_sections if s not in prompt]
            if missing_sections:
                _record("D-05b prompt_sections", "FAIL",
                        f"missing: {missing_sections}")
            else:
                _record("D-05b prompt_sections", "PASS",
                        f"all {len(required_sections)} sections present")
    except Exception:
        _record("D-05  build_compact_prompt", "FAIL", traceback.format_exc(limit=3))

    # D-06: ask_claude (MODEL patched to Haiku to reduce cost)
    # Response shape: {reasoning, regime_view, ideas[], holds[], notes, concerns}
    t = time.monotonic()
    decision: dict = {"reasoning": "wiring_test_fallback", "ideas": [], "holds": []}
    if not prompt:
        _record("D-06  a1_decision_call", "SKIP", "no prompt from D-05")
    else:
        try:
            import bot_stage3_decision as _bsd
            from bot_clients import MODEL_FAST
            with mock.patch.object(_bsd, "MODEL", MODEL_FAST):
                decision = _bsd.ask_claude(prompt)
            if not isinstance(decision, dict) or "reasoning" not in decision:
                _record("D-06  a1_decision_call", "FAIL",
                        f"no 'reasoning' key in response: {list(decision.keys())}",
                        time.monotonic() - t)
            else:
                ideas = decision.get("ideas", [])
                # C. WARN when no trade ideas returned (valid HOLD cycle but worth flagging)
                status_d06 = "WARN" if not ideas else "PASS"
                _record("D-06  a1_decision_call", status_d06,
                        f"ideas={len(ideas)}  regime_view={decision.get('regime_view', '?')}",
                        time.monotonic() - t)
                # C. Idea field validation — Claude returns intent/tier_preference (not action/tier)
                required_fields = {"symbol", "intent", "conviction", "catalyst", "direction"}
                bad_ideas = [
                    i for i in ideas
                    if not isinstance(i, dict) or not required_fields.issubset(i.keys())
                ]
                if bad_ideas:
                    _record("D-06b idea_fields", "FAIL",
                            f"{len(bad_ideas)}/{len(ideas)} ideas missing required fields "
                            f"({required_fields})")
                elif ideas:
                    # D. Cross-stage continuity: parse via validate_claude_decision, verify TEST_ filter
                    try:
                        from risk_kernel import eligibility_check as _ec
                        from schemas import validate_claude_decision
                        parsed = validate_claude_decision(decision)
                        if parsed.ideas:
                            _rej = _ec(parsed.ideas[0], None, {})
                            sym = parsed.ideas[0].symbol
                            if _rej and "wiring_test_symbol" in _rej:
                                _record("D-06b idea_fields", "PASS",
                                        f"TradeIdea parsed + TEST_ filter fired  sym={sym}")
                            else:
                                _record("D-06b idea_fields", "WARN",
                                        f"eligibility_check returned: {_rej}")
                        else:
                            _record("D-06b idea_fields", "WARN",
                                    "validate_claude_decision returned 0 TradeIdea objects")
                    except Exception:
                        _record("D-06b idea_fields", "FAIL", traceback.format_exc(limit=3))
        except Exception:
            _record("D-06  a1_decision_call", "FAIL", traceback.format_exc(limit=3))

    # D-07: risk_kernel TEST_ filter — eligibility_check must reject TEST_ symbols
    t = time.monotonic()
    try:
        from risk_kernel import eligibility_check
        from schemas import AccountAction, Conviction, Direction, Tier, TradeIdea

        test_idea = TradeIdea(
            symbol     = "TEST_AAPL",
            action     = AccountAction.BUY,
            direction  = Direction.BULLISH,
            tier       = Tier.INTRADAY,
            conviction = Conviction.MEDIUM,
            catalyst   = "wiring_test",
        )
        rejection = eligibility_check(test_idea, None, {})
        if rejection and "wiring_test_symbol" in rejection:
            _record("D-07  risk_kernel_test_filter", "PASS",
                    rejection[:80], time.monotonic() - t)
        else:
            _record("D-07  risk_kernel_test_filter", "FAIL",
                    f"TEST_ symbol not rejected (got: {rejection})", time.monotonic() - t)
    except Exception:
        _record("D-07  risk_kernel_test_filter", "FAIL", traceback.format_exc(limit=3))

    # D-08: execute_all with order intercept — synthetic SPY action
    t = time.monotonic()
    orders_before = len(_intercepted_orders)
    try:
        from order_executor import execute_all
        synthetic_action = {
            "action":     "buy",
            "symbol":     "SPY",
            "qty":        1,
            "order_type": "market",
            "tier":       "intraday",
            "confidence": "medium",
            "catalyst":   "wiring_test",
            "stop_loss":  None,
        }
        mock_alpaca = _make_mock_alpaca_client()
        with mock.patch("order_executor._get_alpaca", return_value=mock_alpaca):
            exec_results = execute_all(
                actions            = [synthetic_action],
                account            = state.account,
                positions          = [],
                market_status      = "open",
                minutes_since_open = 60,
                current_prices     = {"SPY": 500.0},
                session_tier       = "market",
                decision_id        = "WIRING-TEST-001",
            )
        orders_delta = len(_intercepted_orders) - orders_before
        _record("D-08  execute_all_intercepted", "PASS",
                f"results={len(exec_results)}  intercepted_delta={orders_delta}  alpaca=mock",
                time.monotonic() - t)
    except Exception:
        _record("D-08  execute_all_intercepted", "FAIL", traceback.format_exc(limit=3))

    # D-09: ChromaDB write — capture vector_id for cleanup (correction #2)
    t = time.monotonic()
    try:
        import trade_memory
        synthetic_decision = {
            "action":      "hold",
            "ideas":       [],
            "reasoning":   "wiring_test synthetic decision",
            "regime_view": "risk_on",
            "wiring_test": True,
        }
        vector_id = trade_memory.save_trade_memory(
            decision          = synthetic_decision,
            market_conditions = _SYNTHETIC_MD,
            session_tier      = "market",
        )
        if vector_id:
            _WIRING_A1_VECTOR_ID = vector_id
            _record("D-09  chromadb_write", "PASS",
                    f"vector_id={vector_id}", time.monotonic() - t)
            # E. Query ChromaDB to verify record is actually retrievable
            try:
                short, _, _ = trade_memory._get_collections()
                if short is not None:
                    result = short.get(ids=[vector_id], include=["metadatas"])
                    if result and result.get("ids"):
                        _record("D-09b chromadb_verify", "PASS",
                                f"record confirmed in collection  id={vector_id}")
                    else:
                        _record("D-09b chromadb_verify", "FAIL",
                                f"record not found after write  id={vector_id}")
                else:
                    _record("D-09b chromadb_verify", "SKIP", "collection unavailable")
            except Exception:
                _record("D-09b chromadb_verify", "FAIL", traceback.format_exc(limit=3))
        else:
            _record("D-09  chromadb_write", "FAIL",
                    "save_trade_memory returned empty string", time.monotonic() - t)
    except Exception:
        _record("D-09  chromadb_write", "FAIL", traceback.format_exc(limit=3))

    # D-10: exit_manager — empty positions, no-op
    t = time.monotonic()
    try:
        from exit_manager import run_exit_manager
        exits = run_exit_manager(
            positions       = [],
            alpaca_client   = _make_mock_alpaca_client(),
            strategy_config = {},
        )
        if isinstance(exits, list):
            _record("D-10  exit_manager", "PASS",
                    f"exits={len(exits)}", time.monotonic() - t)
        else:
            _record("D-10  exit_manager", "FAIL",
                    f"expected list, got {type(exits)}", time.monotonic() - t)
    except Exception:
        _record("D-10  exit_manager", "FAIL", traceback.format_exc(limit=3))

    # D-11: Claude stop_reason — FAIL if any call hit max_tokens (A)
    max_tokens_hits = [r for r in _recorded_stop_reasons if r == "max_tokens"]
    unexpected      = [r for r in _recorded_stop_reasons
                       if r not in ("end_turn", "tool_use", "max_tokens")]
    all_reasons_str = ", ".join(_recorded_stop_reasons) if _recorded_stop_reasons else "none"
    if max_tokens_hits:
        _record("D-11  claude_stop_reasons", "FAIL",
                f"max_tokens hit {len(max_tokens_hits)}x — JSON may be truncated; "
                f"all: {all_reasons_str}")
    elif unexpected:
        _record("D-11  claude_stop_reasons", "WARN",
                f"unexpected stop reasons: {unexpected}  all: {all_reasons_str}")
    else:
        _record("D-11  claude_stop_reasons", "PASS",
                f"{len(_recorded_stop_reasons)} calls, all end_turn  all: {all_reasons_str}")


# ---------------------------------------------------------------------------
# A2 pipeline
# ---------------------------------------------------------------------------
def _run_a2_pipeline() -> None:
    global _a2_files_before

    # E-01: write synthetic signal_scores.json for SPY (backup existing first)
    t = time.monotonic()
    try:
        _SIGNAL_SCORES_PATH.parent.mkdir(parents=True, exist_ok=True)
        if _SIGNAL_SCORES_PATH.exists():
            shutil.copy2(str(_SIGNAL_SCORES_PATH), str(_SIGNAL_SCORES_BAK))
        _SIGNAL_SCORES_PATH.write_text(json.dumps(_SYNTHETIC_SIGNAL_SCORES, indent=2))
        readback = json.loads(_SIGNAL_SCORES_PATH.read_text())
        if "scored_symbols" not in readback or "SPY" not in readback["scored_symbols"]:
            _record("E-01  write_signal_scores", "FAIL",
                    "SPY missing after write", time.monotonic() - t)
            return
        _record("E-01  write_signal_scores", "PASS", "", time.monotonic() - t)
    except Exception:
        _record("E-01  write_signal_scores", "FAIL", traceback.format_exc(limit=3))
        return

    # Snapshot A2 decision dir before the cycle
    try:
        _A2_DECISIONS_DIR.mkdir(parents=True, exist_ok=True)
        _a2_files_before = {f.name for f in _A2_DECISIONS_DIR.glob("a2_dec_*.json")}
    except Exception:
        _a2_files_before = set()

    # E-03: verify load_a1_signals reads SPY back
    t = time.monotonic()
    try:
        from bot_options_stage1_candidates import load_a1_signals
        sig = load_a1_signals()
        if "SPY" not in sig:
            _record("E-03  load_a1_signals", "FAIL",
                    f"SPY not in signals: {list(sig.keys())}", time.monotonic() - t)
        else:
            _record("E-03  load_a1_signals", "PASS",
                    f"symbols={list(sig.keys())}", time.monotonic() - t)
    except Exception:
        _record("E-03  load_a1_signals", "FAIL", traceback.format_exc(limit=3))

    # E-02, E-04–E-07: run full A2 cycle with mocks
    t_cycle = time.monotonic()
    mock_alpaca = _make_mock_alpaca_client()

    from bot_clients import MODEL_FAST
    from bot_options_stage0_preflight import A2PreflightResult
    from divergence import AccountMode, DivergenceScope, OperatingMode

    # Build AccountMode directly — avoids calling load_account_mode() which reads
    # the live a2_mode.json file and fires WhatsApp alerts if the file is malformed.
    _synthetic_a2_mode = AccountMode(
        account            = "A2",
        mode               = OperatingMode.NORMAL,
        scope              = DivergenceScope.GLOBAL,
        scope_id           = "",
        reason_code        = "",
        reason_detail      = "",
        entered_at         = "",
        entered_by         = "wiring_test",
        recovery_condition = "",
        last_checked_at    = "",
    )

    synthetic_preflight = A2PreflightResult(
        halt                 = False,
        equity               = 100_000.0,
        cash                 = 100_000.0,
        buying_power         = 100_000.0,
        pf_allow_live_orders = True,
        pf_allow_new_entries = True,
        a2_mode              = _synthetic_a2_mode,
        pending_underlyings  = frozenset(),
    )

    patches = [
        mock.patch(
            "bot_options_stage0_preflight.run_a2_preflight",
            return_value=synthetic_preflight,
        ),
        mock.patch("bot_options._get_alpaca", return_value=mock_alpaca),
        mock.patch(
            "order_executor_options._get_options_client",
            return_value=mock_alpaca,
        ),
        # Correction #3: patch A2 debate MODEL to Haiku — reduces cost while verifying pipeline
        # bot_options_stage3_debate.py defines its own local MODEL constant (not from bot_clients)
        mock.patch("bot_options_stage3_debate.MODEL", MODEL_FAST),
    ]

    try:
        for p in patches:
            p.start()
        _record("E-02  a2_mocks_installed", "PASS",
                "preflight + alpaca + order_executor_options + MODEL patched")

        import bot_options
        try:
            bot_options.run_options_cycle(
                session_tier    = "market",
                next_cycle_time = "WIRING-TEST",
            )
            cycle_elapsed = time.monotonic() - t_cycle

            new_files = sorted(
                f.name
                for f in _A2_DECISIONS_DIR.glob("a2_dec_*.json")
                if f.name not in _a2_files_before
            )

            _record("E-04  a2_cycle_complete", "PASS",
                    f"elapsed={cycle_elapsed:.1f}s", cycle_elapsed)

            if new_files:
                _record("E-05  a2_debate_or_early_exit", "PASS",
                        f"decision_files_written={new_files}")
                _record("E-06  a2_execution_intercepted", "PASS",
                        f"intercepted_total={len(_intercepted_orders)}; alpaca=mock")
                _record("E-07  persist_decision_record", "PASS",
                        f"files={new_files}")
                # G. A2 decision file schema validation
                try:
                    dec_path = _A2_DECISIONS_DIR / new_files[0]
                    dec_data = json.loads(dec_path.read_text())
                    required_top = {"decision_id", "session_tier", "execution_result", "built_at"}
                    missing_top  = required_top - set(dec_data.keys())
                    if missing_top:
                        _record("E-07b a2_decision_schema", "FAIL",
                                f"missing top-level keys: {sorted(missing_top)}")
                    else:
                        dp = dec_data.get("debate_parsed")
                        if dp is not None and isinstance(dp, dict):
                            required_dp = {"selected_candidate_id", "confidence", "reject"}
                            missing_dp  = required_dp - set(dp.keys())
                            if missing_dp:
                                _record("E-07b a2_decision_schema", "FAIL",
                                        f"debate_parsed missing keys: {sorted(missing_dp)}")
                            else:
                                _record("E-07b a2_decision_schema", "PASS",
                                        "top-level + debate_parsed keys valid")
                        else:
                            _record("E-07b a2_decision_schema", "PASS",
                                    "top-level keys valid; debate_parsed=None (no-trade path)")
                except Exception:
                    _record("E-07b a2_decision_schema", "FAIL", traceback.format_exc(limit=3))
            else:
                # I. WARN on early exit — pipeline ran but produced no decision file
                _record("E-05  a2_debate_or_early_exit", "WARN",
                        "early exit — no candidates reached debate stage")
                _record("E-06  a2_execution_intercepted", "SKIP",
                        "no execution path reached")
                _record("E-07  persist_decision_record", "FAIL",
                        "no a2_dec_*.json written — persist_decision_record may not have fired")

        except Exception:
            _record("E-04  a2_cycle_complete", "FAIL", traceback.format_exc(limit=5))

    finally:
        for p in patches:
            try:
                p.stop()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Cleanup (correction #2: delete by vector_id; correction #5: wiring_test flag)
# ---------------------------------------------------------------------------
def _cleanup_wiring_test() -> list[str]:
    actions: list[str] = []

    # 1. Delete ChromaDB record by vector_id (correction #2)
    if _WIRING_A1_VECTOR_ID:
        try:
            import trade_memory
            trade_memory.delete_by_vector_id(_WIRING_A1_VECTOR_ID)
            actions.append(f"ChromaDB: deleted vector_id={_WIRING_A1_VECTOR_ID}")
            # F. Verify the record is actually gone
            try:
                short, _, _ = trade_memory._get_collections()
                if short is not None:
                    gone_check = short.get(ids=[_WIRING_A1_VECTOR_ID], include=["metadatas"])
                    if gone_check and gone_check.get("ids"):
                        actions.append("ChromaDB: WARNING — record still present after delete!")
                    else:
                        actions.append("ChromaDB: verified gone")
            except Exception as _vex:
                actions.append(f"ChromaDB: verify-gone query failed: {_vex}")
        except Exception as exc:
            actions.append(f"ChromaDB cleanup FAILED: {exc}")
    else:
        actions.append("ChromaDB: no vector_id captured — nothing to delete")

    # 2. Restore signal_scores.json from backup (or remove if we wrote it fresh)
    if _SIGNAL_SCORES_BAK.exists():
        try:
            shutil.move(str(_SIGNAL_SCORES_BAK), str(_SIGNAL_SCORES_PATH))
            actions.append("signal_scores.json: restored from backup")
        except Exception as exc:
            actions.append(f"signal_scores.json restore FAILED: {exc}")
    elif _SIGNAL_SCORES_PATH.exists():
        try:
            data = json.loads(_SIGNAL_SCORES_PATH.read_text())
            if data == _SYNTHETIC_SIGNAL_SCORES:
                _SIGNAL_SCORES_PATH.unlink()
                actions.append("signal_scores.json: removed (was synthetic, no pre-existing file)")
        except Exception:
            pass

    # 3. Remove A2 decision files written during wiring test
    try:
        new_files = [
            f for f in _A2_DECISIONS_DIR.glob("a2_dec_*.json")
            if f.name not in _a2_files_before
        ]
        for f in new_files:
            try:
                f.unlink()
                actions.append(f"A2 decision file removed: {f.name}")
            except Exception as exc:
                actions.append(f"A2 decision file removal FAILED {f.name}: {exc}")
    except Exception as exc:
        actions.append(f"A2 decision file scan FAILED: {exc}")

    # 4. Remove wiring_test entries from trades.jsonl (correction #5)
    if _TRADES_JSONL.exists():
        try:
            lines = _TRADES_JSONL.read_text().splitlines()
            clean, removed = [], 0
            for line in lines:
                try:
                    if json.loads(line).get("wiring_test"):
                        removed += 1
                        continue
                except Exception:
                    pass
                clean.append(line)
            if removed:
                _TRADES_JSONL.write_text("\n".join(clean) + ("\n" if clean else ""))
            actions.append(f"trades.jsonl: removed {removed} wiring_test entries")
        except Exception as exc:
            actions.append(f"trades.jsonl cleanup FAILED: {exc}")

    return actions


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def _print_report(cleanup_actions: list[str], total_elapsed: float) -> int:
    from zoneinfo import ZoneInfo
    now_et = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S ET")

    width = 56
    bar   = "=" * width
    print()
    print(bar)
    print(f"  BULLBEARBOT WIRING TEST — {now_et}")
    print(bar)
    print()

    def _section(title: str, items: list[StageResult]) -> None:
        if not items:
            return
        print(title)
        for r in items:
            elapsed_str = f"  ({r.elapsed:.1f}s)" if r.elapsed > 0.05 else ""
            detail_str  = f"  {r.detail[:90]}" if r.detail else ""
            print(f"  {r.name:<44} {r.status}{elapsed_str}{detail_str}")
        print()

    a1 = [r for r in _results if r.name.startswith("D-")]
    a2 = [r for r in _results if r.name.startswith("E-")]

    _section("A1 PIPELINE", a1)
    _section("A2 PIPELINE", a2)

    print("CLEANUP")
    for a in cleanup_actions:
        print(f"  {a}")
    print()

    fails  = sum(1 for r in _results if r.status == "FAIL")
    passes = sum(1 for r in _results if r.status == "PASS")
    skips  = sum(1 for r in _results if r.status == "SKIP")
    warns  = sum(1 for r in _results if r.status == "WARN")
    total  = len(_results)
    overall = "PASS" if fails == 0 else "FAIL"

    print(bar)
    print(f"  OVERALL: {overall}  "
          f"({passes} passed, {fails} failed, {warns} warned, {skips} skipped / {total} checks)")
    print(f"  Duration: {total_elapsed:.1f}s")
    print(bar)
    print()

    return 0 if fails == 0 else 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run_wiring_test() -> None:
    """
    Full pipeline wiring test with synthetic data.

    Called by scheduler.py --dry-run-wiring and directly via python wiring_test.py.
    Does NOT acquire the scheduler PID lock — it runs independently.
    Exits with sys.exit(0) on all-pass, sys.exit(1) on any FAIL, sys.exit(2) on abort.
    """
    t_start = time.monotonic()

    # D-01: concurrency guard (correction #1)
    safe, msg = _check_no_live_bot()
    if not safe:
        print(f"\nABORT: live scheduler detected ({msg}) — "
              "do not run wiring test while the bot is active.\n")
        sys.exit(2)
    _record("D-01  concurrency_guard", "PASS",
            f"no live scheduler at {_PID_FILE}")

    _install_claude_stop_recorder()
    _install_trade_log_intercept()
    try:
        _run_a1_pipeline()
        _run_a2_pipeline()
    finally:
        _uninstall_claude_stop_recorder()
        _uninstall_trade_log_intercept()
        cleanup_actions = _cleanup_wiring_test()

    total_elapsed = time.monotonic() - t_start
    exit_code = _print_report(cleanup_actions, total_elapsed)
    sys.exit(exit_code)


if __name__ == "__main__":
    run_wiring_test()
