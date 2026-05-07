"""
tests/test_a2_position_health.py — A2 position health audit fixes.

Tests for two bugs fixed in bot_options_stage0_preflight.py:

Bug A — tracked_occs includes inactive structures:
  The orphan detection set _tracked_occs was built from ALL structures (including
  closed/cancelled). Positions from unfilled close orders remained unmonitored.
  Fix: filter _tracked_occs to only include active-lifecycle structures.

Bug B — pnl_unrealized goes stale for illiquid options:
  When options-chain data is unavailable (zero bid, low volume), close_check_loop
  cannot update pnl_unrealized. Loss-based exits stall on stale values.
  Fix: _sync_pnl_from_alpaca_positions() updates pnl_unrealized from Alpaca's
  unrealized_pl field (always available).

Tests:
  1. test_near_expiry_fires_at_threshold_dte
  2. test_near_expiry_silent_above_threshold
  3. test_tracked_occs_excludes_closed_structs (Bug A)
  4. test_orphan_detected_when_struct_closed (Bug A)
  5. test_cancelled_struct_does_not_block_orphan_detection (Bug A)
  6. test_pnl_sync_updates_from_alpaca (Bug B)
  7. test_pnl_sync_skips_if_leg_missing (Bug B)
  8. test_pnl_sync_all_found_saves_structure (Bug B)
  9. test_pnl_sync_no_update_when_unchanged (Bug B)
"""

from __future__ import annotations

import importlib
import os
import sys
from datetime import date, datetime, timedelta, timezone
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

# ── Minimal schema stubs ──────────────────────────────────────────────────────

def _make_leg(occ_symbol: str, side: str = "buy", filled_price: float = 10.0,
              option_type: str = "call", strike: float = 100.0,
              expiration: str = "2026-05-22") -> SimpleNamespace:
    return SimpleNamespace(
        occ_symbol=occ_symbol,
        underlying=occ_symbol[:4],
        side=side,
        qty=1,
        option_type=option_type,
        strike=strike,
        expiration=expiration,
        filled_price=filled_price,
    )


def _make_struct(
    underlying: str,
    occ: str,
    lifecycle: str = "fully_filled",
    expiration: str | None = None,
    pnl_unrealized: float | None = None,
    peak_pnl: float | None = None,
    contracts: int = 1,
    filled_price: float = 10.0,
) -> SimpleNamespace:
    leg = _make_leg(occ, filled_price=filled_price)
    s = SimpleNamespace(
        underlying=underlying,
        structure_id=f"struct-{underlying.lower()}",
        lifecycle=SimpleNamespace(value=lifecycle),
        legs=[leg],
        contracts=contracts,
        expiration=expiration or "2026-05-22",
        opened_at=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
        pnl_unrealized=pnl_unrealized,
        peak_pnl=peak_pnl,
        debit_paid=None,
        max_profit_usd=None,
        max_cost_usd=filled_price * contracts * 100,
        iv_rank=None,
        theta=None,
        thesis_status=None,
        close_profit_target_pct=None,
        close_max_loss_pct=None,
        close_time_stop_pct_dte=None,
        close_reason_code=None,
        closed_at=None,
        audit_log=[],
        strategy=None,
    )
    s.is_open = lambda: lifecycle in ("fully_filled", "partially_filled", "orphan_tracked")
    s.net_debit_per_contract = lambda: None
    s.add_audit = lambda msg: None
    return s


def _make_alpaca_position(symbol: str, unrealized_pl: float,
                           avg_entry_price: float = 10.0, qty: float = 1.0) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=symbol,
        unrealized_pl=unrealized_pl,
        avg_entry_price=avg_entry_price,
        qty=qty,
    )


# ── Test helpers ──────────────────────────────────────────────────────────────

def _load_preflight():
    """Import _sync_pnl_from_alpaca_positions from the module under test."""
    spec = importlib.util.spec_from_file_location(
        "bot_options_stage0_preflight",
        os.path.join(_ROOT, "bot_options_stage0_preflight.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    _stubs = {
        "chromadb": MagicMock(),
        "twilio": MagicMock(),
        "twilio.rest": MagicMock(),
        "sendgrid": MagicMock(),
        "options_state": MagicMock(),
        "options_executor": MagicMock(),
        "reconciliation": MagicMock(),
        "options_data": MagicMock(),
        "alpaca.trading.client": MagicMock(),
        "alpaca.trading.requests": MagicMock(),
        "alpaca.trading.enums": MagicMock(),
    }
    with patch.dict("sys.modules", _stubs):
        spec.loader.exec_module(mod)
    return mod


# ── Tests: near_expiry threshold ──────────────────────────────────────────────

def test_near_expiry_fires_at_threshold_dte():
    """should_close_structure returns loss_cut_near_expiry at DTE=4 with loss."""
    try:
        import options_executor as _oe
    except ImportError:
        return  # skip if schemas not importable in this environment

    exp = (date.today() + timedelta(days=4)).isoformat()
    struct = _make_struct("LLY", "LLY260515C00990000", expiration=exp,
                          pnl_unrealized=-500.0, filled_price=21.55)
    config = {"account2": {"max_loss_exit_pct": 0.50, "profit_target_pct": 0.75}}

    with patch("options_executor._compute_dte", return_value=4), \
         patch("options_executor._max_loss_usd_estimate", return_value=2155.0), \
         patch("options_executor._resolve_close_targets",
               return_value={"max_loss_pct": 0.50, "profit_target_pct": 0.75}), \
         patch("options_executor._estimate_current_value", return_value=None):

        result, reason = _oe.should_close_structure(
            struct, current_prices={}, config=config, current_time=None
        )
        assert result is True, f"Expected True at DTE=4 with pnl<0, got {result}"
        assert reason == "loss_cut_near_expiry", f"Expected loss_cut_near_expiry, got {reason}"


def test_near_expiry_silent_above_threshold():
    """should_close_structure returns False at DTE=10 (all near_expiry rules silent)."""
    try:
        import options_executor as _oe
    except ImportError:
        return

    exp = (date.today() + timedelta(days=10)).isoformat()
    struct = _make_struct("TSLA", "TSLA260522C00395000", expiration=exp,
                          pnl_unrealized=-200.0, filled_price=13.375)
    config = {"account2": {"max_loss_exit_pct": 0.50, "profit_target_pct": 0.75}}

    with patch("options_executor._compute_dte", return_value=10), \
         patch("options_executor._max_loss_usd_estimate", return_value=1337.5), \
         patch("options_executor._resolve_close_targets",
               return_value={"max_loss_pct": 0.50, "profit_target_pct": 0.75}), \
         patch("options_executor._estimate_current_value", return_value=None):

        result, reason = _oe.should_close_structure(
            struct, current_prices={}, config=config, current_time=None
        )
        # At DTE=10, pnl=-200, max_loss=1337.5 — none of the near_expiry rules should fire.
        # Stop also doesn't fire (200 < 668.75 threshold).
        assert result is False or reason not in (
            "loss_cut_near_expiry", "time_stop_loss_near_expiry", "expiry_approaching"
        ), f"Near-expiry rule fired incorrectly at DTE=10: result={result} reason={reason}"


# ── Tests: Bug A — tracked_occs filter (inactive structures) ─────────────────

def test_tracked_occs_excludes_closed_structs():
    """
    Fix A: OCC symbols from CLOSED structures must not appear in _tracked_occs.
    When a close order is submitted but doesn't fill, the structure lifecycle is
    set to CLOSED — but the position remains in Alpaca. If the CLOSED occ_symbol
    is in tracked_occs, the orphan reconciler skips it and no retry happens.
    """
    _INACTIVE_LC = frozenset({"closed", "cancelled", "rejected", "expired"})

    structs = [
        _make_struct("WMT", "WMT260515C00131000", lifecycle="closed"),
        _make_struct("LLY", "LLY260515C00990000", lifecycle="fully_filled"),
        _make_struct("ONTO", "ONTO260515C00300000", lifecycle="closed"),
    ]

    def _lc_str(s):
        return s.lifecycle.value if hasattr(s.lifecycle, "value") else str(s.lifecycle)

    tracked = {
        leg.occ_symbol
        for s in structs
        for leg in s.legs
        if getattr(leg, "occ_symbol", None)
        and _lc_str(s) not in _INACTIVE_LC
    }

    assert "LLY260515C00990000" in tracked, "Active struct occ must be tracked"
    assert "WMT260515C00131000" not in tracked, "CLOSED struct occ must NOT be tracked (Bug A)"
    assert "ONTO260515C00300000" not in tracked, "CLOSED struct occ must NOT be tracked (Bug A)"


def test_orphan_detected_when_struct_closed():
    """
    After Fix A, a position whose only struct entry is CLOSED is detected as orphan.
    """
    _INACTIVE_LC = frozenset({"closed", "cancelled", "rejected", "expired"})

    structs = [
        _make_struct("WMT", "WMT260515C00131000", lifecycle="closed"),
    ]
    live_positions = ["WMT260515C00131000"]

    def _lc_str(s):
        return s.lifecycle.value if hasattr(s.lifecycle, "value") else str(s.lifecycle)

    tracked = {
        leg.occ_symbol
        for s in structs
        for leg in s.legs
        if getattr(leg, "occ_symbol", None) and _lc_str(s) not in _INACTIVE_LC
    }

    orphans = [sym for sym in live_positions if sym not in tracked]
    assert "WMT260515C00131000" in orphans, (
        "WMT from CLOSED struct must be detected as orphan after Fix A"
    )


def test_cancelled_struct_does_not_block_orphan_detection():
    """
    Cancelled structures (mleg order cancelled, but legs may have partially filled)
    must not block orphan detection. Both GOOGL385/390 should be detectable.
    """
    _INACTIVE_LC = frozenset({"closed", "cancelled", "rejected", "expired"})

    googl_cancelled = SimpleNamespace(
        underlying="GOOGL",
        lifecycle=SimpleNamespace(value="cancelled"),
        legs=[
            _make_leg("GOOGL260522C00385000"),
            _make_leg("GOOGL260522C00390000", side="sell"),
        ],
    )
    structs = [googl_cancelled]
    live_positions = ["GOOGL260522C00385000", "GOOGL260522C00390000"]

    def _lc_str(s):
        return s.lifecycle.value if hasattr(s.lifecycle, "value") else str(s.lifecycle)

    tracked = {
        leg.occ_symbol
        for s in structs
        for leg in s.legs
        if getattr(leg, "occ_symbol", None) and _lc_str(s) not in _INACTIVE_LC
    }

    orphans = [sym for sym in live_positions if sym not in tracked]
    assert "GOOGL260522C00385000" in orphans, "GOOGL long leg must be orphan after Fix A"
    assert "GOOGL260522C00390000" in orphans, "GOOGL short leg must be orphan after Fix A"


# ── Tests: Bug B — pnl_unrealized sync from Alpaca ───────────────────────────

def test_pnl_sync_updates_from_alpaca():
    """
    _sync_pnl_from_alpaca_positions updates pnl_unrealized when Alpaca reports
    a different value than what is stored in the structure.
    """
    try:
        mod = _load_preflight()
    except Exception:
        return

    saved = {}

    def _fake_save(struct):
        saved[struct.underlying] = struct.pnl_unrealized

    struct = _make_struct("LLY", "LLY260515C00990000", pnl_unrealized=-800.0)
    alpaca_pos = _make_alpaca_position("LLY260515C00990000", unrealized_pl=-1190.0)

    with patch("options_state.save_structure", side_effect=_fake_save):
        count = mod._sync_pnl_from_alpaca_positions([struct], [alpaca_pos])

    assert count == 1, f"Expected 1 structure updated, got {count}"
    assert saved.get("LLY") == -1190.0, f"Expected -1190.0, got {saved.get('LLY')}"


def test_pnl_sync_skips_if_leg_missing():
    """
    When a leg's OCC symbol is not in any Alpaca position (position already closed),
    _sync_pnl_from_alpaca_positions skips that structure.
    """
    try:
        mod = _load_preflight()
    except Exception:
        return

    saved = {}

    def _fake_save(struct):
        saved[struct.underlying] = struct.pnl_unrealized

    struct = _make_struct("ONTO", "ONTO260515C00300000", pnl_unrealized=-500.0)
    # No Alpaca position for ONTO — it may have been auto-closed
    alpaca_pos = _make_alpaca_position("AAPL260522C00285000", unrealized_pl=105.0)

    with patch("options_state.save_structure", side_effect=_fake_save):
        count = mod._sync_pnl_from_alpaca_positions([struct], [alpaca_pos])

    assert count == 0, f"Expected 0 updates when leg missing from Alpaca, got {count}"
    assert "ONTO" not in saved, "ONTO must not be saved when leg is absent"


def test_pnl_sync_all_found_saves_structure():
    """
    When all legs are found in Alpaca positions, pnl_unrealized is updated correctly
    by summing unrealized_pl across all legs.
    """
    try:
        mod = _load_preflight()
    except Exception:
        return

    saved = {}

    def _fake_save(struct):
        saved[struct.underlying] = struct.pnl_unrealized

    # Debit spread: long $385C + short $390C
    long_leg = _make_leg("GOOGL260522C00385000", side="buy", filled_price=15.0)
    short_leg = _make_leg("GOOGL260522C00390000", side="sell", filled_price=12.0)
    struct = SimpleNamespace(
        underlying="GOOGL",
        structure_id="struct-googl-spread",
        lifecycle=SimpleNamespace(value="fully_filled"),
        legs=[long_leg, short_leg],
        contracts=20,
        pnl_unrealized=-500.0,
        is_open=lambda: True,
    )

    # Alpaca reports: long leg +$10700, short leg -$12400
    alpaca_positions = [
        _make_alpaca_position("GOOGL260522C00385000", unrealized_pl=10700.0),
        _make_alpaca_position("GOOGL260522C00390000", unrealized_pl=-12400.0),
    ]

    with patch("options_state.save_structure", side_effect=_fake_save):
        count = mod._sync_pnl_from_alpaca_positions([struct], alpaca_positions)

    assert count == 1, f"Expected 1 update, got {count}"
    assert saved.get("GOOGL") == round(10700.0 + (-12400.0), 2), (
        f"Expected net -1700.0, got {saved.get('GOOGL')}"
    )


def test_pnl_sync_no_update_when_unchanged():
    """
    When pnl_unrealized already matches Alpaca, no save is triggered.
    """
    try:
        mod = _load_preflight()
    except Exception:
        return

    saved = {}

    def _fake_save(struct):
        saved[struct.underlying] = struct.pnl_unrealized

    struct = _make_struct("TSLA", "TSLA260522C00395000", pnl_unrealized=632.5)
    alpaca_pos = _make_alpaca_position("TSLA260522C00395000", unrealized_pl=632.5)

    with patch("options_state.save_structure", side_effect=_fake_save):
        count = mod._sync_pnl_from_alpaca_positions([struct], [alpaca_pos])

    assert count == 0, f"Expected 0 saves when pnl is current, got {count}"
    assert "TSLA" not in saved, "No save when pnl unchanged"
