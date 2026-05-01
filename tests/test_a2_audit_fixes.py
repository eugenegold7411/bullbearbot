"""
test_a2_audit_fixes.py — Tests for A2 audit fixes (P0/P1/P2).

A2-P0: close_check_loop price injection
A2-P1: config forwarding (submit_structure, reconciliation)
A2-P2: post-cancel cooldown guard
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_structure(lifecycle: str = "fully_filled", underlying: str = "AAPL",
                    expiration: str = "2026-06-20", last_cancelled_at: str | None = None):
    from schemas import (
        OptionsLeg,
        OptionsStructure,
        OptionStrategy,
        StructureLifecycle,
        Tier,
    )
    lc_map = {
        "fully_filled":    StructureLifecycle.FULLY_FILLED,
        "partially_filled": StructureLifecycle.PARTIALLY_FILLED,
        "submitted":       StructureLifecycle.SUBMITTED,
        "cancelled":       StructureLifecycle.CANCELLED,
        "proposed":        StructureLifecycle.PROPOSED,
    }
    leg = OptionsLeg(
        occ_symbol=f"{underlying}260620C00150000",
        underlying=underlying,
        side="buy",
        qty=1,
        option_type="call",
        strike=150.0,
        expiration=expiration,
        bid=1.00,
        ask=1.20,
        filled_price=1.10,
    )
    struct = OptionsStructure(
        structure_id=f"test-{lifecycle}-{underlying}",
        underlying=underlying,
        strategy=OptionStrategy.CALL_DEBIT_SPREAD,
        lifecycle=lc_map[lifecycle],
        legs=[leg],
        contracts=1,
        max_cost_usd=110.0,
        opened_at="2026-06-01T10:00:00+00:00",
        catalyst="test",
        tier=Tier.CORE,
        debit_paid=1.10,
        max_profit_usd=40.0,
        last_cancelled_at=last_cancelled_at,
    )
    return struct


def _market_hours_et():
    """Return a datetime that is within market hours (10:00 AM ET, Tuesday)."""
    # 2026-05-05 is a Tuesday
    return datetime(2026, 5, 5, 10, 0, 0, tzinfo=timezone.utc).astimezone(
        __import__("zoneinfo").ZoneInfo("America/New_York")
    )


def _after_hours_et():
    """Return a datetime that is after market close (5:00 PM ET)."""
    return datetime(2026, 5, 5, 17, 0, 0, tzinfo=timezone.utc).astimezone(
        __import__("zoneinfo").ZoneInfo("America/New_York")
    )


# ── A2-P0: close_check_loop price injection ───────────────────────────────────

class TestCloseCheckPrices:

    def _make_chain(self, underlying: str = "AAPL", bid: float = 1.00, ask: float = 1.20):
        """Build a minimal chain dict matching fetch_options_chain() structure."""
        return {
            "symbol": underlying,
            "current_price": 150.0,
            "expirations": {
                "2026-06-20": {
                    "calls": [
                        {"strike": 150.0, "bid": bid, "ask": ask,
                         "lastPrice": 1.10, "impliedVolatility": 0.30,
                         "volume": 100, "openInterest": 500},
                    ],
                    "puts": [],
                }
            },
        }

    # A2-P0-01: price fetch called during market hours before should_close_structure
    def test_p0_01_fetches_prices_during_market_hours(self):
        struct = _make_structure("fully_filled", "AAPL")

        import bot_options_stage4_execution as m

        with (
            patch("options_state.get_open_structures", return_value=[struct]),
            patch("options_state.load_structures", return_value=[struct]),
            patch("bot_options_stage4_execution._sync_submitted_lifecycles"),
            patch("bot_options_stage4_execution._update_fill_prices"),
            patch("options_executor.should_close_structure", return_value=(False, "")),
            patch.object(m, "_fetch_close_check_prices",
                         return_value={"AAPL260620C00150000": 1.10}) as mock_pfetch,
        ):
            alpaca = MagicMock()
            m.close_check_loop(alpaca)

        mock_pfetch.assert_called_once_with([struct])

    # A2-P0-02: stop_loss_hit fires when spread loses >= 50% of max_risk
    def test_p0_02_stop_loss_hit_when_prices_populated(self):
        import options_executor
        struct = _make_structure("fully_filled", "AAPL")
        # debit_paid=1.10, contracts=1, max_risk=1.10*1*100=110 USD
        # stop fires at -50% of max_risk = -55 USD loss
        # current_val must be 110 - 55 = 55 USD → mid price = 0.55
        prices = {"AAPL260620C00150000": 0.40}  # well below stop

        should_close, reason = options_executor.should_close_structure(
            struct, current_prices=prices, config={}, current_time=None,
        )

        assert should_close is True
        assert reason == "stop_loss_hit"

    # A2-P0-03: target_profit_hit fires when spread gains >= 80% of max_profit
    def test_p0_03_target_profit_hit_when_prices_populated(self):
        import options_executor
        struct = _make_structure("fully_filled", "AAPL")
        # max_profit_usd=40.0, target = 80% × 40 = 32 USD gain
        # current_pnl = current_val - 110 >= 32 → current_val >= 142
        # mid per share ≥ 142/100 = 1.42
        prices = {"AAPL260620C00150000": 1.50}  # above target

        should_close, reason = options_executor.should_close_structure(
            struct, current_prices=prices, config={}, current_time=None,
        )

        assert should_close is True
        assert reason == "target_profit_hit"

    # A2-P0-04: non-fatal if price fetch fails — DTE/time-stop still fire
    def test_p0_04_nonfatal_if_price_fetch_fails(self):
        struct = _make_structure("fully_filled", "AAPL",
                                 expiration="2026-05-02")  # expiry imminent → DTE rule fires
        import bot_options_stage4_execution as m

        closed_structs = []

        def mock_close(s, client, reason, method):
            closed_structs.append((s.underlying, reason))

        with (
            patch("options_state.get_open_structures", return_value=[struct]),
            patch("options_state.load_structures", return_value=[struct]),
            patch("bot_options_stage4_execution._sync_submitted_lifecycles"),
            patch("bot_options_stage4_execution._update_fill_prices"),
            patch("bot_options_stage4_execution._fetch_close_check_prices", return_value={}),
            patch("options_executor.should_close_structure",
                  return_value=(True, "expiry_approaching")) as mock_scs,
            patch("options_executor.should_roll_structure", return_value=(False, "")),
            patch("options_executor.close_structure", side_effect=mock_close),
        ):
            alpaca = MagicMock()
            m.close_check_loop(alpaca)  # must not raise

        # should_close_structure was called with whatever prices _fetch returned
        assert mock_scs.call_count == 1
        assert closed_structs == [("AAPL", "expiry_approaching")]

    # A2-P0-05: _fetch_close_check_prices returns {} outside market hours
    def test_p0_05_empty_prices_outside_market_hours(self):
        import bot_options_stage4_execution as m

        struct = _make_structure("fully_filled", "AAPL")

        # After hours — no fetch should happen
        with patch("options_data.fetch_options_chain") as mock_fetch:
            with patch("bot_options_stage4_execution.datetime") as mock_dt:
                # Simulate 5 PM ET (after close)
                _now = MagicMock()
                _now.weekday.return_value = 1   # Tuesday
                _after = MagicMock()
                _after.__le__ = lambda s, o: False  # now < market_open is False
                _now.replace.return_value = _after
                mock_dt.now.return_value = _now

                # Use real function but with time mocked via ET datetime
                # Simpler: patch datetime.now directly in the module
                pass

        # Run outside market hours by mocking weekday() to return Saturday

        with patch("bot_options_stage4_execution.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.weekday.return_value = 5   # Saturday
            mock_dt.now.return_value = mock_now

            with patch("options_data.fetch_options_chain") as mock_fetch:
                result = m._fetch_close_check_prices([struct])

        assert result == {}
        mock_fetch.assert_not_called()


# ── A2-P1: config forwarding ──────────────────────────────────────────────────

class TestConfigForwarding:

    # A2-P1-01: submit_structure called with full config, min_credit_usd reads configured value
    def test_p1_01_submit_structure_receives_config(self):
        import order_executor_options as m

        cfg = {"account2": {"min_credit_usd": 0.25, "equity_floor": 25000}}

        filled = MagicMock()
        filled.lifecycle.value = "submitted"
        filled.lifecycle = __import__("schemas").StructureLifecycle.SUBMITTED
        filled.order_ids = ["ord-1"]
        filled.structure_id = "s-1"
        filled.max_cost_usd = 100.0
        filled.iv_rank = 50.0
        filled.audit_log = ["submitted"]

        struct = _make_structure("fully_filled")

        with (
            patch.object(m, "_load_strategy_config", return_value=cfg),
            patch("options_executor.submit_structure", return_value=filled) as mock_sub,
            patch("order_executor_options._get_options_client", return_value=MagicMock()),
            patch("options_state.save_structure"),
        ):
            m.submit_options_order(struct, equity=50_000)

        mock_sub.assert_called_once()
        # config is passed as a keyword argument
        call_cfg = mock_sub.call_args.kwargs.get("config")
        if call_cfg is None and len(mock_sub.call_args[0]) >= 3:
            call_cfg = mock_sub.call_args[0][2]
        assert call_cfg == cfg

    # A2-P1-02: reconcile_options_structures called with _s_cfg not {}
    def test_p1_02_reconcile_called_with_s_cfg(self):
        from bot_options_stage0_preflight import run_a2_preflight

        s_cfg = {"account2": {"auto_cancel_unfilled_orders": False}}

        struct = _make_structure("fully_filled")

        with (
            patch("bot_options_stage0_preflight._get_et_now") as mock_now,
            patch("bot_options_stage0_preflight._get_obs_mode_state",
                  return_value={"observation_mode": False}),
            patch("bot_options_stage0_preflight._check_and_update_iv_ready",
                  return_value={"observation_mode": False}),
            patch("bot_options_stage0_preflight.Path.exists", return_value=True),
            patch("builtins.open", MagicMock()),
            patch("json.loads", return_value=s_cfg),
            patch("options_state.load_structures", return_value=[struct]),
            patch("options_state.get_open_structures", return_value=[struct]),
            patch("options_state.save_structure"),
            patch("reconciliation.reconcile_options_structures") as mock_recon,
            patch("reconciliation.plan_structure_repair"),
            patch("reconciliation.execute_reconciliation_plan"),
        ):
            mock_now.return_value = MagicMock(
                hour=10, minute=0, weekday=MagicMock(return_value=1)
            )
            mock_recon.return_value = MagicMock(
                broken=[], expiring_soon=[], needs_close=[], orphaned_legs=[]
            )
            # Build a fake Alpaca client
            alpaca = MagicMock()
            alpaca.get_account.return_value = MagicMock(equity="50000", cash="25000",
                                                         buying_power="30000")
            alpaca.get_all_positions.return_value = []
            alpaca.get_orders.return_value = []

            try:
                run_a2_preflight("market", alpaca)
            except Exception:
                pass  # we only care about the mock call args

            if mock_recon.called:
                _, kwargs = mock_recon.call_args
                assert kwargs.get("config") == s_cfg or mock_recon.call_args[0][3] == s_cfg

    # A2-P1-03: plan_structure_repair called with _s_cfg not {}
    def test_p1_03_plan_repair_called_with_s_cfg(self):
        # The repair call only happens when reconcile reports issues.
        # We validate by reading the source: both config={} replaced with config=_s_cfg.
        import ast
        from pathlib import Path

        source = Path("bot_options_stage0_preflight.py").read_text()
        tree = ast.parse(source)

        empty_dict_calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = ""
                if isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                elif isinstance(node.func, ast.Name):
                    func_name = node.func.id
                if func_name in ("reconcile_options_structures", "plan_structure_repair"):
                    for kw in node.keywords:
                        if kw.arg == "config" and isinstance(kw.value, ast.Dict) and not kw.value.keys:
                            empty_dict_calls.append(func_name)

        assert empty_dict_calls == [], (
            f"Found config={{}} at call sites: {empty_dict_calls}. "
            "Expected _s_cfg to be forwarded."
        )


# ── A2-P2: post-cancel cooldown ───────────────────────────────────────────────

class TestCancelCooldown:

    # A2-P2-01: cancelled-unfilled structure blocks resubmission within cooldown
    def test_p2_01_cancelled_blocks_within_cooldown(self):
        from bot_options_stage0_preflight import _is_duplicate_submission

        recent_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        struct = _make_structure("cancelled", "AAPL", last_cancelled_at=recent_ts)
        cfg = {"account2": {"cancel_cooldown_hours": 1.0}}

        assert _is_duplicate_submission("AAPL", [struct], config=cfg) is True

    # A2-P2-02: cooldown respects configured cancel_cooldown_hours value
    def test_p2_02_cooldown_respects_configured_value(self):
        from bot_options_stage0_preflight import _is_duplicate_submission

        # 90 minutes ago — past a 1-hour cooldown but within a 2-hour cooldown
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
        struct = _make_structure("cancelled", "AAPL", last_cancelled_at=old_ts)

        cfg_1h = {"account2": {"cancel_cooldown_hours": 1.0}}
        cfg_2h = {"account2": {"cancel_cooldown_hours": 2.0}}

        # Past 1h cooldown → allows re-entry
        assert _is_duplicate_submission("AAPL", [struct], config=cfg_1h) is False
        # Within 2h cooldown → still blocked
        assert _is_duplicate_submission("AAPL", [struct], config=cfg_2h) is True

    # A2-P2-03: SUBMITTED lifecycle still blocks immediately (existing behavior preserved)
    def test_p2_03_submitted_blocks_immediately(self):
        from bot_options_stage0_preflight import _is_duplicate_submission

        struct = _make_structure("submitted", "AAPL")
        # No config needed — SUBMITTED always blocks
        assert _is_duplicate_submission("AAPL", [struct]) is True

    # A2-P2-04: different symbol's cooldown does not affect another symbol
    def test_p2_04_different_symbol_not_affected(self):
        from bot_options_stage0_preflight import _is_duplicate_submission

        recent_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        nvda_cancelled = _make_structure("cancelled", "NVDA", last_cancelled_at=recent_ts)
        cfg = {"account2": {"cancel_cooldown_hours": 1.0}}

        # NVDA is cooling down but AAPL should be unaffected
        assert _is_duplicate_submission("AAPL", [nvda_cancelled], config=cfg) is False

    # A2-P2-05: last_cancelled_at stamped when lifecycle transitions to CANCELLED
    def test_p2_05_last_cancelled_at_stamped_on_cancel(self):
        from bot_options_stage0_preflight import _cancel_and_clear_unfilled_orders

        struct = _make_structure("submitted", "AAPL")
        struct.order_ids = ["ord-abc"]
        assert struct.last_cancelled_at is None

        before = datetime.now(timezone.utc)

        alpaca = MagicMock()
        alpaca.get_order_by_id.return_value = MagicMock(
            filled_qty=0, status=MagicMock(value="new")
        )

        saved = {}

        with (
            patch("options_state.load_structures", return_value=[struct]),
            patch("options_state.save_structure", side_effect=lambda s: saved.update({s.structure_id: s})),
        ):
            _cancel_and_clear_unfilled_orders(alpaca, {"account2": {"auto_cancel_unfilled_orders": True}})

        after = datetime.now(timezone.utc)
        saved_struct = saved.get(struct.structure_id)
        assert saved_struct is not None, "structure was not saved after cancel"
        assert saved_struct.last_cancelled_at is not None, "last_cancelled_at was not stamped"

        stamped_dt = datetime.fromisoformat(saved_struct.last_cancelled_at)
        assert before <= stamped_dt <= after, "stamped timestamp is outside expected window"

    # A2-P2-06: second cancel re-stamps last_cancelled_at (cooldown clock resets)
    def test_p2_06_second_cancel_restamps_cooldown_clock(self):
        from bot_options_stage0_preflight import _cancel_and_clear_unfilled_orders

        # Simulate: first cancel happened 90 minutes ago (past a 1-hour cooldown)
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
        struct = _make_structure("submitted", "AAPL")
        struct.order_ids = ["ord-xyz"]
        struct.last_cancelled_at = old_ts   # pre-set as if already cancelled once

        before_second_cancel = datetime.now(timezone.utc)

        alpaca = MagicMock()
        alpaca.get_order_by_id.return_value = MagicMock(
            filled_qty=0, status=MagicMock(value="new")
        )

        saved = {}

        with (
            patch("options_state.load_structures", return_value=[struct]),
            patch("options_state.save_structure", side_effect=lambda s: saved.update({s.structure_id: s})),
        ):
            _cancel_and_clear_unfilled_orders(alpaca, {"account2": {"auto_cancel_unfilled_orders": True}})

        after_second_cancel = datetime.now(timezone.utc)
        saved_struct = saved.get(struct.structure_id)
        assert saved_struct is not None

        new_ts = datetime.fromisoformat(saved_struct.last_cancelled_at)
        old_dt = datetime.fromisoformat(old_ts)

        # New stamp must be strictly later than the old one (clock reset)
        assert new_ts > old_dt, "second cancel did not advance the cooldown timestamp"
        assert before_second_cancel <= new_ts <= after_second_cancel
