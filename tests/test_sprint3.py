"""
tests/test_sprint3.py — Sprint 3 verification suite (Items T2-3, T2-4, T2-8, O1).

Items covered:
  T2-3  — _compute_config_diff populates config_changes in director memo
  T2-4  — _PARAM_RANGES rejects out-of-bounds parameter_adjustments values
  T2-8  — _rotate_jsonl wired into decision_outcomes, shadow_lane, macro_wire
  O1    — datetime.utcnow() replaced with datetime.now(timezone.utc) in order_executor
  O2    — _max_position_pct_equity_note already correct (Sprint 1); verified, no change needed
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# ─── Suite 1: T2-3 — _compute_config_diff ─────────────────────────────────────

class TestComputeConfigDiff:
    def _import(self):
        # Ensure fresh import in case of cached state
        if "weekly_review" in sys.modules:
            wr = sys.modules["weekly_review"]
        else:
            import weekly_review as wr
        return wr

    def test_no_changes_returns_empty(self):
        import weekly_review as wr
        params = {"stop_loss_pct_core": 0.03, "max_positions": 14}
        assert wr._compute_config_diff(params, params.copy()) == {}

    def test_single_change_captured(self):
        import weekly_review as wr
        old = {"stop_loss_pct_core": 0.03, "max_positions": 14}
        new = {"stop_loss_pct_core": 0.025, "max_positions": 14}
        diff = wr._compute_config_diff(old, new)
        assert "stop_loss_pct_core" in diff
        assert diff["stop_loss_pct_core"] == {"old": 0.03, "new": 0.025}

    def test_unchanged_keys_excluded(self):
        import weekly_review as wr
        old = {"stop_loss_pct_core": 0.03, "max_positions": 14}
        new = {"stop_loss_pct_core": 0.025, "max_positions": 14}
        diff = wr._compute_config_diff(old, new)
        assert "max_positions" not in diff

    def test_multiple_changes(self):
        import weekly_review as wr
        old = {"stop_loss_pct_core": 0.03, "max_positions": 14, "vix_threshold_caution": 22}
        new = {"stop_loss_pct_core": 0.025, "max_positions": 10, "vix_threshold_caution": 22}
        diff = wr._compute_config_diff(old, new)
        assert set(diff.keys()) == {"stop_loss_pct_core", "max_positions"}

    def test_new_key_added(self):
        import weekly_review as wr
        old = {"stop_loss_pct_core": 0.03}
        new = {"stop_loss_pct_core": 0.03, "new_param": 99}
        diff = wr._compute_config_diff(old, new)
        assert "new_param" in diff
        assert diff["new_param"] == {"old": None, "new": 99}

    def test_diff_structure_has_old_and_new(self):
        import weekly_review as wr
        old = {"stop_loss_pct_core": 0.03}
        new = {"stop_loss_pct_core": 0.025}
        diff = wr._compute_config_diff(old, new)
        entry = diff["stop_loss_pct_core"]
        assert "old" in entry and "new" in entry


# ─── Suite 2: T2-4 — _PARAM_RANGES + range validation ─────────────────────────

class TestParamRanges:
    def test_param_ranges_exists_and_non_empty(self):
        import weekly_review as wr
        assert isinstance(wr._PARAM_RANGES, dict)
        assert len(wr._PARAM_RANGES) >= 10

    def test_stop_loss_pct_core_has_bounds(self):
        import weekly_review as wr
        lo, hi = wr._PARAM_RANGES["stop_loss_pct_core"]
        assert lo < hi
        assert lo > 0
        assert hi <= 0.15

    def test_all_ranges_have_two_elements(self):
        import weekly_review as wr
        for key, rng in wr._PARAM_RANGES.items():
            assert len(rng) == 2, f"{key} range should be (lo, hi)"
            assert rng[0] < rng[1], f"{key} lo must be < hi"

    def test_in_range_value_accepted(self):
        import weekly_review as wr
        block = '{"active_strategy": "hybrid", "parameter_adjustments": {"stop_loss_pct_core": 0.03}}'
        result = wr._extract_and_validate_agent6_json(block)
        assert result is not None
        assert result["parameter_adjustments"]["stop_loss_pct_core"] == 0.03

    def test_out_of_range_value_rejected(self):
        import weekly_review as wr
        # stop_loss_pct_core max is 0.10; 0.50 should be rejected
        block = '{"active_strategy": "hybrid", "parameter_adjustments": {"stop_loss_pct_core": 0.50}}'
        result = wr._extract_and_validate_agent6_json(block)
        assert result is not None
        assert "stop_loss_pct_core" not in result.get("parameter_adjustments", {})

    def test_below_range_value_rejected(self):
        import weekly_review as wr
        # stop_loss_pct_core min is 0.005; 0.0001 should be rejected
        block = '{"active_strategy": "hybrid", "parameter_adjustments": {"stop_loss_pct_core": 0.0001}}'
        result = wr._extract_and_validate_agent6_json(block)
        assert result is not None
        assert "stop_loss_pct_core" not in result.get("parameter_adjustments", {})

    def test_boundary_values_accepted(self):
        import weekly_review as wr
        lo, hi = wr._PARAM_RANGES["stop_loss_pct_core"]
        for v in [lo, hi]:
            block = json.dumps({"active_strategy": "hybrid", "parameter_adjustments": {"stop_loss_pct_core": v}})
            result = wr._extract_and_validate_agent6_json(block)
            assert result is not None
            assert result["parameter_adjustments"].get("stop_loss_pct_core") == v

    def test_param_without_range_entry_passes_through(self):
        import weekly_review as wr
        # active_strategy is not a numeric field — should always pass through
        block = '{"active_strategy": "momentum", "parameter_adjustments": {}}'
        result = wr._extract_and_validate_agent6_json(block)
        assert result is not None
        assert result.get("active_strategy") == "momentum"


# ─── Suite 3: T2-8 — _rotate_jsonl wired into JSONL write paths ───────────────

class TestRotateJsonlWired:
    def test_rotate_called_in_decision_outcomes(self, tmp_path, monkeypatch):
        """log_outcome_event calls _rotate_jsonl after write."""
        import cost_attribution as ca
        import decision_outcomes as do

        monkeypatch.setattr(do, "OUTCOMES_LOG", tmp_path / "outcomes.jsonl")
        rotated = []

        def _fake_rotate(path, max_lines=10_000):
            rotated.append((path, max_lines))

        monkeypatch.setattr(ca, "_rotate_jsonl", _fake_rotate)

        from decision_outcomes import DecisionOutcomeRecord
        rec = DecisionOutcomeRecord(
            decision_id="dec_test_001", account="A1", symbol="AAPL",
            timestamp=datetime.now(timezone.utc).isoformat(),
            action="buy", tier="core",
        )
        do.log_outcome_event(rec)
        assert len(rotated) == 1
        assert rotated[0][1] == 10_000

    def test_rotate_called_in_shadow_lane(self, tmp_path, monkeypatch):
        """log_shadow_event calls _rotate_jsonl after write."""
        import cost_attribution as ca
        import shadow_lane as sl

        monkeypatch.setattr(sl, "NEAR_MISS_LOG", tmp_path / "near_miss.jsonl")
        rotated = []

        def _fake_rotate(path, max_lines=10_000):
            rotated.append((path, max_lines))

        monkeypatch.setattr(ca, "_rotate_jsonl", _fake_rotate)

        sl.log_shadow_event("approved_trade", "AAPL", {"score": 75})
        assert len(rotated) == 1
        assert rotated[0][1] == 10_000

    def test_rotate_called_in_macro_wire(self, tmp_path, monkeypatch):
        """save_significant_events calls _rotate_jsonl when new events are written."""
        import cost_attribution as ca
        import macro_wire as mw

        monkeypatch.setattr(mw, "SIG_EVENTS", tmp_path / "sig_events.jsonl")
        rotated = []

        def _fake_rotate(path, max_lines=10_000):
            rotated.append((path, max_lines))

        monkeypatch.setattr(ca, "_rotate_jsonl", _fake_rotate)
        monkeypatch.setattr(mw, "MACRO_DIR", tmp_path)

        articles = [{
            "headline":        "Fed signals rate cut",
            "source":          "Reuters",
            "impact_score":    8,
            "keyword_tier":    "high",
            "summary":         "Fed hints at cuts",
            "keywords_matched": ["fed"],
            "direction":       "bullish",
            "affected_sectors": [],
            "affected_symbols": [],
            "urgency":         "high",
            "one_line_summary": "Fed signals cut",
        }]
        mw.save_significant_events(articles)
        assert len(rotated) == 1

    def test_rotate_failure_is_non_fatal_decision_outcomes(self, tmp_path, monkeypatch):
        """Rotation failure does not block log_outcome_event."""
        import cost_attribution as ca
        import decision_outcomes as do

        monkeypatch.setattr(do, "OUTCOMES_LOG", tmp_path / "outcomes.jsonl")

        def _bad_rotate(*_a, **_kw):
            raise RuntimeError("disk full")

        monkeypatch.setattr(ca, "_rotate_jsonl", _bad_rotate)

        from decision_outcomes import DecisionOutcomeRecord
        rec = DecisionOutcomeRecord(
            decision_id="dec_test_002", account="A1", symbol="AAPL",
            timestamp=datetime.now(timezone.utc).isoformat(),
            action="buy", tier="core",
        )
        # Must not raise
        do.log_outcome_event(rec)
        assert (tmp_path / "outcomes.jsonl").exists()


# ─── Suite 4: O1 — datetime.utcnow() replaced ─────────────────────────────────

class TestUtcnowFixed:
    def test_utcnow_not_in_order_executor(self):
        """order_executor.py must not reference the deprecated datetime.utcnow()."""
        src = Path("order_executor.py").read_text()
        assert "utcnow" not in src, "datetime.utcnow() still present in order_executor.py"

    def test_timezone_import_present(self):
        """datetime import includes timezone."""
        src = Path("order_executor.py").read_text()
        assert "from datetime import datetime, timezone" in src or \
               "timezone" in src, "timezone not imported in order_executor.py"


# ─── Suite 5: O2 — _max_position_pct_equity_note already correct ──────────────

class TestStrategyConfigNote:
    def test_note_is_not_stale(self):
        """_max_position_pct_capacity_note must not say 'unused'."""
        cfg = json.loads(Path("strategy_config.json").read_text())
        note = cfg.get("parameters", {}).get("_max_position_pct_capacity_note", "")
        assert "unused" not in note.lower(), \
            f"Stale note still present: {note}"

    def test_note_mentions_enforcement(self):
        """Note should reference risk_kernel enforcement (was fixed in Sprint 1)."""
        cfg = json.loads(Path("strategy_config.json").read_text())
        note = cfg.get("parameters", {}).get("_max_position_pct_capacity_note", "")
        assert "risk_kernel" in note.lower() or "enforced" in note.lower(), \
            f"Note does not mention enforcement: {note}"
