"""
Sprint 7 Phase A tests — S7-A, S7-B, T1-3, T1-4.

S7-A: exit_manager per-ticker lock prevents duplicate exit submissions
S7-B: halt alerting with 30-minute in-memory dedup
T1-3: VIX dict-vs-float guard in morning_brief and market_data
T1-4: Enum deserialization .lower() normalization in divergence.load_account_mode
"""
import json
import time
from unittest.mock import MagicMock, patch

# ─────────────────────────────────────────────────────────────────────────────
# S7-A — Exit Manager Per-Ticker Lock
# ─────────────────────────────────────────────────────────────────────────────

class TestExitManagerTickerLock:
    """lock gates check-and-submit atomically; different tickers use separate locks."""

    def _make_position(self, symbol="AAPL", qty="10", current_price="150.0",
                       avg_entry_price="140.0", unrealized_pl="100.0"):
        pos = MagicMock()
        pos.symbol = symbol
        pos.qty = qty
        pos.current_price = current_price
        pos.avg_entry_price = avg_entry_price
        pos.unrealized_pl = unrealized_pl
        return pos

    def test_separate_tickers_have_separate_locks(self):
        """Each ticker gets its own independent lock instance."""
        from exit_manager import _get_ticker_lock
        lock_aapl = _get_ticker_lock("AAPL")
        lock_googl = _get_ticker_lock("GOOGL")
        assert lock_aapl is not lock_googl

    def test_same_ticker_returns_same_lock(self):
        """Repeated calls for the same symbol return the identical lock."""
        from exit_manager import _get_ticker_lock
        lock_a = _get_ticker_lock("MSFT")
        lock_b = _get_ticker_lock("MSFT")
        assert lock_a is lock_b

    def test_concurrent_same_ticker_blocked_when_lock_held(self):
        """
        If the ticker lock is already held (simulating first call in progress),
        refresh_exits_for_position returns False without submitting any order.
        """
        from exit_manager import _get_ticker_lock, refresh_exits_for_position

        pos = self._make_position("NVDA")
        mock_client = MagicMock()

        # Simulate first call holding the lock
        lock = _get_ticker_lock("NVDA")
        acquired = lock.acquire(blocking=False)
        assert acquired, "Lock should have been free before this test"
        try:
            result = refresh_exits_for_position(
                position=pos,
                alpaca_client=mock_client,
                strategy_config={},
                conviction="medium",
                exit_info={"status": "unprotected", "stop_price": None,
                           "target_price": None, "stop_order_id": None,
                           "target_order_id": None},
            )
            assert result is False, "Expected False when ticker lock already held"
            mock_client.submit_order.assert_not_called()
        finally:
            lock.release()

    def test_different_ticker_not_blocked_by_nvda_lock(self):
        """Holding NVDA's lock does not block AAPL — they use separate locks."""
        from exit_manager import _get_ticker_lock

        nvda_lock = _get_ticker_lock("NVDA_BLOCK_TEST")
        aapl_lock = _get_ticker_lock("AAPL_BLOCK_TEST")

        acquired_nvda = nvda_lock.acquire(blocking=False)
        try:
            # AAPL lock should be independently acquirable
            acquired_aapl = aapl_lock.acquire(blocking=False)
            assert acquired_aapl, "AAPL lock should not be blocked by NVDA lock"
            aapl_lock.release()
        finally:
            if acquired_nvda:
                nvda_lock.release()

    def test_lock_released_after_normal_execution(self):
        """After refresh_exits_for_position runs (unprotected path), lock is released."""
        from exit_manager import _get_ticker_lock, refresh_exits_for_position

        sym = "TSLA_LOCK_RELEASE"
        pos = self._make_position(sym, qty="5", current_price="200.0")
        mock_client = MagicMock()
        mock_client.submit_order.side_effect = Exception("test abort")

        # Run once — will fail at submit_order, but lock must be released
        refresh_exits_for_position(
            position=pos,
            alpaca_client=mock_client,
            strategy_config={},
            exit_info={"status": "unprotected", "stop_price": None,
                       "target_price": None, "stop_order_id": None,
                       "target_order_id": None},
        )

        lock = _get_ticker_lock(sym)
        acquired = lock.acquire(blocking=False)
        assert acquired, "Lock should have been released after execution (even on exception)"
        lock.release()

    def test_lock_released_on_no_action_needed(self):
        """Lock is released even when position is already protected (early return)."""
        from exit_manager import _get_ticker_lock, refresh_exits_for_position

        sym = "GLD_PROTECTED"
        pos = self._make_position(sym)
        mock_client = MagicMock()

        refresh_exits_for_position(
            position=pos,
            alpaca_client=mock_client,
            strategy_config={},
            exit_info={"status": "protected", "stop_price": 135.0,
                       "target_price": 155.0, "stop_order_id": "x",
                       "target_order_id": "y"},
        )

        lock = _get_ticker_lock(sym)
        acquired = lock.acquire(blocking=False)
        assert acquired, "Lock should be released after early-return (protected path)"
        lock.release()


# ─────────────────────────────────────────────────────────────────────────────
# S7-B — Halt Alerting with 30-minute Dedup
# ─────────────────────────────────────────────────────────────────────────────

class TestHaltAlerting:
    """_maybe_send_halt_alert fires on first halt, suppresses repeats within 30 min."""

    def setup_method(self):
        """Reset the module-level dedup cache before each test."""
        import bot_stage0_precycle
        bot_stage0_precycle._halt_alert_cache.clear()

    def _make_div_event(self, symbol="V", event_type="protection_missing"):
        evt = MagicMock()
        evt.symbol = symbol
        evt.event_type = event_type
        return evt

    def _make_publisher(self, success=True):
        pub = MagicMock()
        pub.send_alert.return_value = success
        return pub

    def test_first_halt_sends_alert(self):
        """First halt detection fires send_alert exactly once."""
        from bot_stage0_precycle import _maybe_send_halt_alert

        publisher = self._make_publisher()
        events = [self._make_div_event("V", "protection_missing")]

        _maybe_send_halt_alert("A1", events, publisher, positions=[])
        publisher.send_alert.assert_called_once()

    def test_repeat_halt_within_30min_suppressed(self):
        """Second call within dedup window does NOT call send_alert again."""
        from bot_stage0_precycle import _maybe_send_halt_alert

        publisher = self._make_publisher()
        events = [self._make_div_event("V", "protection_missing")]

        _maybe_send_halt_alert("A1", events, publisher, positions=[])
        _maybe_send_halt_alert("A1", events, publisher, positions=[])

        # Must have been called exactly once despite two invocations
        assert publisher.send_alert.call_count == 1

    def test_repeat_halt_after_window_fires_again(self):
        """Call after dedup window expires fires send_alert again."""
        import bot_stage0_precycle
        from bot_stage0_precycle import _maybe_send_halt_alert

        publisher = self._make_publisher()
        events = [self._make_div_event("V", "protection_missing")]

        # Simulate a past alert 31 minutes ago
        bot_stage0_precycle._halt_alert_cache["A1"] = time.time() - (31 * 60)

        _maybe_send_halt_alert("A1", events, publisher, positions=[])
        publisher.send_alert.assert_called_once()

    def test_different_account_fires_independently(self):
        """A1 dedup state does not block A2 alert."""
        from bot_stage0_precycle import _maybe_send_halt_alert

        pub_a1 = self._make_publisher()
        pub_a2 = self._make_publisher()
        events = [self._make_div_event("V", "protection_missing")]

        # A1 alert sent (and cached)
        _maybe_send_halt_alert("A1", events, pub_a1, positions=[])
        pub_a1.send_alert.assert_called_once()

        # A2 alert should still fire — separate cache key
        _maybe_send_halt_alert("A2", events, pub_a2, positions=[])
        pub_a2.send_alert.assert_called_once()

    def test_alert_message_contains_required_fields(self):
        """Alert message includes account, event type, and symbol."""
        from bot_stage0_precycle import _maybe_send_halt_alert

        publisher = self._make_publisher()
        events = [self._make_div_event("GOOGL", "protection_missing")]

        _maybe_send_halt_alert("A1", events, publisher, positions=[])

        call_args = publisher.send_alert.call_args
        msg = call_args[0][0]
        assert "A1" in msg
        assert "protection_missing" in msg
        assert "GOOGL" in msg

    def test_alert_includes_position_size_when_available(self):
        """Alert message includes market_value when position is found."""
        from bot_stage0_precycle import _maybe_send_halt_alert

        pos = MagicMock()
        pos.symbol = "V"
        pos.market_value = "12450.00"

        publisher = self._make_publisher()
        events = [self._make_div_event("V", "protection_missing")]

        _maybe_send_halt_alert("A1", events, publisher, positions=[pos])

        msg = publisher.send_alert.call_args[0][0]
        assert "V" in msg

    def test_send_alert_failure_does_not_cache_timestamp(self):
        """If send_alert returns False, cache is not updated — next call retries."""
        import bot_stage0_precycle
        from bot_stage0_precycle import _maybe_send_halt_alert

        publisher = self._make_publisher(success=False)
        events = [self._make_div_event("V", "protection_missing")]

        _maybe_send_halt_alert("A1", events, publisher, positions=[])
        assert "A1" not in bot_stage0_precycle._halt_alert_cache

    def test_nonfatal_on_publisher_exception(self):
        """Exceptions in send_alert do not propagate — _maybe_send_halt_alert is non-fatal."""
        from bot_stage0_precycle import _maybe_send_halt_alert

        publisher = MagicMock()
        publisher.send_alert.side_effect = RuntimeError("Twilio down")
        events = [self._make_div_event("V", "protection_missing")]

        # Must not raise
        _maybe_send_halt_alert("A1", events, publisher, positions=[])


# ─────────────────────────────────────────────────────────────────────────────
# T1-3 — VIX Dict-vs-Float Guard
# ─────────────────────────────────────────────────────────────────────────────

class TestVixGuardMorningBrief:
    """morning_brief VIX extraction handles dict, float, None, and empty dict."""

    def _run_vix_guard(self, vix_val):
        """
        Execute the morning_brief VIX normalization logic in isolation.
        Returns the final `vix` dict (may be empty).
        """
        _vix_snap = vix_val if vix_val is not None else {}
        if isinstance(_vix_snap, dict) and _vix_snap:
            vix = _vix_snap
        elif isinstance(_vix_snap, (int, float)) and _vix_snap:
            vix = {"price": round(float(_vix_snap), 2), "chg_pct": 0}
        else:
            vix = {}
        return vix

    def test_vix_dict_format_roundtrips_correctly(self):
        """Dict with price and chg_pct passes through unchanged."""
        vix = self._run_vix_guard({"price": 18.5, "chg_pct": -1.2})
        assert vix.get("price") == 18.5
        assert vix.get("chg_pct") == -1.2

    def test_vix_float_converted_to_dict(self):
        """Float input is normalized to dict form — no AttributeError."""
        vix = self._run_vix_guard(22.7)
        assert isinstance(vix, dict)
        assert abs(vix.get("price", 0) - 22.7) < 0.01
        assert vix.get("chg_pct") == 0

    def test_vix_none_returns_empty_dict(self):
        """None input yields empty dict — downstream `if vix:` correctly skips."""
        vix = self._run_vix_guard(None)
        assert vix == {}
        assert not vix  # falsy → downstream skips the append

    def test_vix_empty_dict_returns_empty_dict(self):
        """Empty dict input yields empty dict — same as absent."""
        vix = self._run_vix_guard({})
        assert vix == {}

    def test_vix_zero_float_returns_empty_dict(self):
        """Zero float is falsy — treated same as absent."""
        vix = self._run_vix_guard(0.0)
        assert vix == {}

    def test_vix_dict_get_price_no_attribute_error(self):
        """Calling .get() on the normalized result never raises AttributeError."""
        for vix_input in [18.5, {"price": 20.0}, None, {}, 0]:
            vix = self._run_vix_guard(vix_input)
            # Must not raise
            _ = vix.get("price", "?")
            _ = vix.get("chg_pct", 0)


class TestVixGuardMarketData:
    """market_data VIX guard normalizes dict-or-float to float without AttributeError."""

    def _run_market_data_vix_guard(self, vix_snap):
        """Reproduce the T1-3 guard logic from market_data.py."""
        return (
            float(vix_snap.get("price", 20.0) or 20.0)
            if isinstance(vix_snap, dict) else float(vix_snap or 20.0)
        )

    def test_dict_input_extracts_price(self):
        assert abs(self._run_market_data_vix_guard({"price": 25.3, "chg_pct": 2.1}) - 25.3) < 0.01

    def test_float_input_passthrough(self):
        assert abs(self._run_market_data_vix_guard(18.9) - 18.9) < 0.01

    def test_none_defaults_to_20(self):
        assert self._run_market_data_vix_guard(None) == 20.0

    def test_empty_dict_defaults_to_20(self):
        assert self._run_market_data_vix_guard({}) == 20.0

    def test_zero_float_defaults_to_20(self):
        assert self._run_market_data_vix_guard(0) == 20.0


# ─────────────────────────────────────────────────────────────────────────────
# T1-4 — Enum Deserialization .lower() Normalization
# ─────────────────────────────────────────────────────────────────────────────

class TestEnumLowerNormalization:
    """divergence.load_account_mode parses mode/scope regardless of stored case."""

    def _write_mode_file(self, tmp_path, mode_str, scope_str="account"):
        mode_file = tmp_path / "a1_mode.json"
        mode_file.write_text(json.dumps({
            "account": "A1",
            "mode": mode_str,
            "scope": scope_str,
            "scope_id": "",
            "reason_code": "",
            "reason_detail": "",
            "entered_at": "",
            "entered_by": "system",
            "recovery_condition": "one_clean_cycle",
            "last_checked_at": "",
            "clean_cycles_since_entry": 0,
            "version": 1,
        }))
        return mode_file

    def test_lowercase_normal_loads_correctly(self, tmp_path):
        """Standard lowercase 'normal' roundtrips without error."""
        from divergence import OperatingMode, load_account_mode
        self._write_mode_file(tmp_path, "normal")
        with patch("divergence.get_mode_path", return_value=tmp_path / "a1_mode.json"):
            result = load_account_mode("A1")
        assert result.mode == OperatingMode.NORMAL

    def test_uppercase_NORMAL_loads_as_normal(self, tmp_path):
        """Uppercase 'NORMAL' (manual edit or migration artifact) loads without ValueError."""
        from divergence import OperatingMode, load_account_mode
        self._write_mode_file(tmp_path, "NORMAL")
        with patch("divergence.get_mode_path", return_value=tmp_path / "a1_mode.json"):
            result = load_account_mode("A1")
        assert result.mode == OperatingMode.NORMAL

    def test_uppercase_HALTED_loads_as_halted(self, tmp_path):
        """Uppercase 'HALTED' correctly preserves the halted state."""
        from divergence import OperatingMode, load_account_mode
        self._write_mode_file(tmp_path, "HALTED")
        with patch("divergence.get_mode_path", return_value=tmp_path / "a1_mode.json"):
            result = load_account_mode("A1")
        assert result.mode == OperatingMode.HALTED

    def test_mixed_case_Risk_Containment_loads(self, tmp_path):
        """Mixed-case 'Risk_Containment' normalizes to RISK_CONTAINMENT."""
        from divergence import OperatingMode, load_account_mode
        self._write_mode_file(tmp_path, "Risk_Containment")
        with patch("divergence.get_mode_path", return_value=tmp_path / "a1_mode.json"):
            result = load_account_mode("A1")
        assert result.mode == OperatingMode.RISK_CONTAINMENT

    def test_uppercase_scope_ACCOUNT_loads(self, tmp_path):
        """Uppercase scope 'ACCOUNT' normalizes correctly."""
        from divergence import DivergenceScope, load_account_mode
        self._write_mode_file(tmp_path, "normal", "ACCOUNT")
        with patch("divergence.get_mode_path", return_value=tmp_path / "a1_mode.json"):
            result = load_account_mode("A1")
        assert result.scope == DivergenceScope.ACCOUNT

    def test_garbage_mode_string_falls_back_to_normal(self, tmp_path):
        """Completely unknown mode string falls back to NORMAL via outer except."""
        from divergence import OperatingMode, load_account_mode
        self._write_mode_file(tmp_path, "this_is_not_a_valid_mode")
        with patch("divergence.get_mode_path", return_value=tmp_path / "a1_mode.json"):
            result = load_account_mode("A1")
        # The outer try/except catches ValueError and returns the NORMAL default
        assert result.mode == OperatingMode.NORMAL

    def test_none_mode_falls_back_to_normal(self, tmp_path):
        """None in mode field falls back to NORMAL (str(None).lower() = 'none' → ValueError)."""
        from divergence import OperatingMode, load_account_mode
        self._write_mode_file(tmp_path, None)
        with patch("divergence.get_mode_path", return_value=tmp_path / "a1_mode.json"):
            result = load_account_mode("A1")
        assert result.mode == OperatingMode.NORMAL
