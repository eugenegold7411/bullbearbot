"""
tests/test_s11_orb_entry_gate_removal.py -- ORB hard entry block removal

ORB-01: Order submitted inside the 9:30-9:45 AM window is NOT rejected
ORB-02: _orb_locked flag still flips True at 9:45 AM
ORB-03: signal scorer output schema still carries orb_candidate field
ORB-04: build_orb_section() still returns formatted text when ORB data exists
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── ORB-01: no rejection inside formation window ───────────────────────────────

class TestOrbGateRemoved:
    """ORB-01: validate_action no longer raises inside 9:30-9:45 AM window."""

    def test_buy_in_orb_window_not_rejected(self, monkeypatch):
        # Simulate _orb_locked=False (formation window still open)
        mock_sched = types.ModuleType("scheduler")
        mock_sched._orb_locked = False
        monkeypatch.setitem(sys.modules, "scheduler", mock_sched)

        from types import SimpleNamespace

        from order_executor import validate_action

        account = SimpleNamespace(equity="50000.0")
        action = {
            "action": "buy",
            "symbol": "AAPL",
            "qty": 1,
            "stop_loss": 145.0,
            "take_profit": 165.0,
            "tier": "core",
        }
        # minutes_since_open=5 → inside 9:30-9:45 window; _orb_locked=False
        # Prior code raised ValueError here; new code only logs debug
        validate_action(
            action, account, [], "open",
            minutes_since_open=5,
            current_prices={"AAPL": 150.0},
        )  # must not raise

    def test_orb_block_code_removed_from_source(self):
        import inspect

        import order_executor
        src = inspect.getsource(order_executor.validate_action)
        assert "ORB formation window (9:30-9:45 AM ET) — observation only" not in src
        assert "no new entries until 9:45 AM ET" not in src

    def test_orb_debug_log_present_in_source(self):
        import inspect

        import order_executor
        src = inspect.getsource(order_executor.validate_action)
        assert "[ORB] Formation window active but entry not blocked" in src


# ── ORB-02: _orb_locked flag still updates at 9:45 AM ─────────────────────────

class TestOrbLockedFlagUpdates:
    """ORB-02: scheduler._orb_locked flips True after 9:45 AM."""

    def test_orb_locked_flag_exists_in_scheduler(self):
        import scheduler
        assert hasattr(scheduler, "_orb_locked")
        assert isinstance(scheduler._orb_locked, bool)

    def test_update_orb_range_exists(self):
        import scheduler
        assert callable(getattr(scheduler, "_update_orb_range", None))

    def test_orb_lock_at_9_45_present_in_source(self):
        """_update_orb_range still locks _orb_locked at 9*60+45 minutes."""
        import inspect

        import scheduler
        src = inspect.getsource(scheduler._update_orb_range)
        assert "_orb_locked = True" in src
        assert "9 * 60 + 45" in src


# ── ORB-03: signal scorer output schema carries orb_candidate ─────────────────

class TestOrbCandidateInSignalSchema:
    """ORB-03: L3 system prompt schema includes orb_candidate boolean field."""

    def test_l3_system_prompt_has_orb_candidate(self):
        import bot_stage2_signal as s
        assert "orb_candidate" in s._L3_SYSTEM

    def test_legacy_signal_sys_has_orb_candidate(self):
        import bot_stage2_signal as s
        assert "orb_candidate" in s._SIGNAL_SYS


# ── ORB-04: build_orb_section still builds text ───────────────────────────────

class TestBuildOrbSection:
    """ORB-04: build_orb_section returns formatted text when ORB data exists."""

    def test_returns_formatted_string_with_candidates(self):
        from market_data import build_orb_section
        orb_data = {
            "candidates": [
                {
                    "symbol": "NVDA",
                    "gap_pct": 2.5,
                    "pre_mkt_volume_ratio": 3.1,
                    "conviction": "HIGH",
                    "catalyst": "earnings beat",
                    "entry_condition": "break above 892",
                    "invalidation": "below 870",
                },
            ]
        }
        result = build_orb_section(orb_data)
        assert "NVDA" in result
        assert result != "  No ORB candidates identified for today."

    def test_returns_fallback_on_empty_candidates(self):
        from market_data import build_orb_section
        result = build_orb_section({"candidates": []})
        assert "No ORB candidates" in result

    def test_returns_fallback_on_missing_candidates_key(self):
        from market_data import build_orb_section
        result = build_orb_section({})
        assert "No ORB candidates" in result
