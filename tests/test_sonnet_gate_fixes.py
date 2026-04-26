"""
Tests for sonnet_gate fixes:
- SIGNAL_THRESHOLD: peak >= threshold forces full prompt; peak < threshold falls
  through to position-count / window heuristics (no longer dead code).
- SCHEDULED_WINDOW: parser tolerant of both {start,end} HH:MM and {hour,minute}
  point-in-time config shapes.
- trigger_reason plumbing: macro_wire trigger reason forces full prompt
  regardless of internal trigger reasons.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _gate_state():
    from sonnet_gate import _GATE_DEFAULTS, GateState
    return GateState(**_GATE_DEFAULTS)


def _cfg(scheduled_windows=None, signal_score_threshold=12):
    return {
        "sonnet_gate": {
            "cooldown_minutes": 15,
            "max_consecutive_skips": 12,
            "signal_score_threshold": signal_score_threshold,
            "exposure_change_threshold": 0.05,
            "deadline_warning_minutes": 30,
            "scheduled_windows": scheduled_windows or [],
        }
    }


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL_THRESHOLD — dead code fix
# ─────────────────────────────────────────────────────────────────────────────

class TestSignalThresholdLive:
    def test_high_peak_score_forces_full_prompt(self):
        """Peak signal score >= threshold (75) returns False (full prompt)."""
        from sonnet_gate import TriggerReason, should_use_compact_prompt
        result = should_use_compact_prompt(
            reasons=[TriggerReason.SIGNAL_THRESHOLD, TriggerReason.COOLDOWN_EXPIRED],
            positions=[],
            signal_scores={"scored_symbols": {"GLD": {"score": 80}}},
            recon_diff=None,
        )
        assert result is False, "peak=80 must force full prompt"

    def test_low_peak_score_allows_compact(self):
        """Peak signal score < threshold with no other trigger now allows compact."""
        from sonnet_gate import TriggerReason, should_use_compact_prompt
        # SIGNAL_THRESHOLD with peak 60 < 75, no positions, no recon → compact
        result = should_use_compact_prompt(
            reasons=[TriggerReason.SIGNAL_THRESHOLD, TriggerReason.COOLDOWN_EXPIRED],
            positions=[],
            signal_scores={"scored_symbols": {"GLD": {"score": 60}}},
            recon_diff=None,
        )
        assert result is True, "peak=60 < 75 must fall through to compact (was dead before)"

    def test_signal_threshold_reads_from_config(self):
        """Threshold value comes from config when provided."""
        from sonnet_gate import TriggerReason, should_use_compact_prompt
        # Set threshold to 50; peak=60 should now be >= threshold → full
        cfg = {"sonnet_gate": {"signal_peak_full_threshold": 50}}
        result = should_use_compact_prompt(
            reasons=[TriggerReason.SIGNAL_THRESHOLD],
            positions=[],
            signal_scores={"scored_symbols": {"GLD": {"score": 60}}},
            recon_diff=None,
            config=cfg,
        )
        assert result is False, "peak=60 >= configured threshold=50 must force full"

    def test_signal_threshold_with_positions_still_full(self):
        """Sub-threshold peak with 3+ positions still gets full (position rule wins)."""
        from sonnet_gate import TriggerReason, should_use_compact_prompt
        result = should_use_compact_prompt(
            reasons=[TriggerReason.SIGNAL_THRESHOLD, TriggerReason.COOLDOWN_EXPIRED],
            positions=["p1", "p2", "p3"],
            signal_scores={"scored_symbols": {"GLD": {"score": 60}}},
            recon_diff=None,
        )
        assert result is False, "3+ positions still force full"


# ─────────────────────────────────────────────────────────────────────────────
# SCHEDULED_WINDOW — config-format fix
# ─────────────────────────────────────────────────────────────────────────────

class TestScheduledWindow:
    def test_window_fires_inside_start_end_range(self):
        """SCHEDULED_WINDOW fires when current time is inside {start,end} range."""
        from sonnet_gate import TriggerReason, should_run_sonnet
        windows = [{"start": "09:25", "end": "09:35"}]
        now_et = datetime(2026, 4, 28, 9, 30, tzinfo=ET)  # Tue inside window
        ran, reasons, _ = should_run_sonnet(
            session_tier="market", regime="risk_on", vix=18.0,
            signal_scores={}, positions=[], recon_diff=None,
            breaking_news="", time_bound_actions=[],
            current_time_et=now_et, gate_state=_gate_state(),
            config=_cfg(scheduled_windows=windows),
            equity=100000.0,
        )
        assert TriggerReason.SCHEDULED_WINDOW in reasons

    def test_window_does_not_fire_outside_range(self):
        """SCHEDULED_WINDOW does not fire outside the configured window."""
        from sonnet_gate import TriggerReason, should_run_sonnet
        windows = [{"start": "09:25", "end": "09:35"}]
        now_et = datetime(2026, 4, 28, 11, 0, tzinfo=ET)  # outside window
        _ran, reasons, _ = should_run_sonnet(
            session_tier="market", regime="risk_on", vix=18.0,
            signal_scores={}, positions=[], recon_diff=None,
            breaking_news="", time_bound_actions=[],
            current_time_et=now_et, gate_state=_gate_state(),
            config=_cfg(scheduled_windows=windows),
            equity=100000.0,
        )
        assert TriggerReason.SCHEDULED_WINDOW not in reasons

    def test_legacy_hour_minute_format_parses_without_error(self):
        """Legacy {hour, minute} config does not raise and fires at the exact minute."""
        from sonnet_gate import TriggerReason, should_run_sonnet
        windows = [{"hour": 9, "minute": 30}]  # legacy point-in-time
        now_et = datetime(2026, 4, 28, 9, 30, tzinfo=ET)
        _ran, reasons, _ = should_run_sonnet(
            session_tier="market", regime="risk_on", vix=18.0,
            signal_scores={}, positions=[], recon_diff=None,
            breaking_news="", time_bound_actions=[],
            current_time_et=now_et, gate_state=_gate_state(),
            config=_cfg(scheduled_windows=windows),
            equity=100000.0,
        )
        # No KeyError raised, and at the exact minute it fires
        assert TriggerReason.SCHEDULED_WINDOW in reasons

    def test_legacy_hour_minute_off_by_one_does_not_fire(self):
        """Legacy {hour,minute} format fires only at exact minute (1-min window)."""
        from sonnet_gate import TriggerReason, should_run_sonnet
        windows = [{"hour": 9, "minute": 30}]
        now_et = datetime(2026, 4, 28, 9, 31, tzinfo=ET)
        _ran, reasons, _ = should_run_sonnet(
            session_tier="market", regime="risk_on", vix=18.0,
            signal_scores={}, positions=[], recon_diff=None,
            breaking_news="", time_bound_actions=[],
            current_time_et=now_et, gate_state=_gate_state(),
            config=_cfg(scheduled_windows=windows),
            equity=100000.0,
        )
        assert TriggerReason.SCHEDULED_WINDOW not in reasons


# ─────────────────────────────────────────────────────────────────────────────
# Trigger-reason plumbing — macro_wire forces full prompt
# ─────────────────────────────────────────────────────────────────────────────

class TestTriggerReasonPlumbing:
    def test_macro_wire_trigger_forces_full_prompt(self):
        """trigger_reason containing 'macro wire' forces full prompt."""
        from sonnet_gate import TriggerReason, should_use_compact_prompt
        # All other inputs would normally produce compact (no positions, no triggers)
        result = should_use_compact_prompt(
            reasons=[TriggerReason.COOLDOWN_EXPIRED],
            positions=[],
            signal_scores={},
            recon_diff=None,
            trigger_reason="macro wire: Fed announces emergency rate cut (score=8.8, tier=critical)",
        )
        assert result is False, "macro wire trigger must force full prompt"

    def test_macro_wire_trigger_case_insensitive(self):
        """macro wire match is case-insensitive."""
        from sonnet_gate import TriggerReason, should_use_compact_prompt
        result = should_use_compact_prompt(
            reasons=[TriggerReason.COOLDOWN_EXPIRED],
            positions=[],
            signal_scores={},
            recon_diff=None,
            trigger_reason="MACRO WIRE: something happened",
        )
        assert result is False

    def test_non_macro_trigger_allows_compact(self):
        """Non-macro trigger reasons (e.g. momentum) follow normal compact rules."""
        from sonnet_gate import TriggerReason, should_use_compact_prompt
        result = should_use_compact_prompt(
            reasons=[TriggerReason.COOLDOWN_EXPIRED],
            positions=[],
            signal_scores={},
            recon_diff=None,
            trigger_reason="momentum: NVDA +3.2% vol=2.5x",
        )
        assert result is True

    def test_trigger_reason_default_empty_string(self):
        """should_use_compact_prompt works with no trigger_reason argument."""
        from sonnet_gate import TriggerReason, should_use_compact_prompt
        # No exception raised when called positionally as before
        result = should_use_compact_prompt(
            [TriggerReason.COOLDOWN_EXPIRED], [], {}, None
        )
        assert isinstance(result, bool)

    def test_run_cycle_accepts_trigger_reason_kwarg(self):
        """bot.run_cycle accepts trigger_reason keyword without error."""
        import inspect

        import bot
        sig = inspect.signature(bot.run_cycle)
        assert "trigger_reason" in sig.parameters
        assert sig.parameters["trigger_reason"].default == ""

    def test_scheduler_runs_cycle_with_trigger_reason(self):
        """scheduler._run_one_cycle is defined inside run() — verify via source inspection."""
        import inspect

        import scheduler
        src = inspect.getsource(scheduler.run)
        assert "trigger_reason" in src, "scheduler.run() must reference trigger_reason"
        assert "trigger_reason=combined" in src, (
            "scheduler must forward the combined trigger reason to _run_one_cycle"
        )
