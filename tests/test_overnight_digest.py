"""
Tests for the overnight Haiku digest pipeline:
- macro_wire.write_overnight_digest() — Haiku synthesis of significant_events.jsonl
- morning_brief._load_overnight_digest() — staleness gate + formatted injection
- scheduler._maybe_write_overnight_digest / _maybe_write_eod_digest — time gates
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

ET_OFFSET = "-04:00"  # America/New_York DST offset (used for synthetic ts strings)


def _write_events(path: Path, events: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def _fake_haiku_response(payload: dict):
    """Build a MagicMock that mimics anthropic.messages.create() return value."""
    resp = MagicMock()
    resp.content = [MagicMock(text=json.dumps(payload))]
    resp.usage = MagicMock(
        input_tokens=100, output_tokens=50,
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
    )
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# write_overnight_digest()
# ─────────────────────────────────────────────────────────────────────────────

class TestWriteOvernightDigest:
    def test_writes_digest_when_qualifying_events_exist(self, tmp_path, monkeypatch):
        import macro_wire as mw
        ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        events = [
            {"ts": ts, "headline": "Fed emergency cut", "impact_score": 8.5,
             "keyword_tier": "critical", "affected_symbols": []},
            {"ts": ts, "headline": "Banking contagion fears", "impact_score": 7.0,
             "keyword_tier": "critical", "affected_symbols": ["JPM"]},
        ]
        sig_path = tmp_path / "significant_events.jsonl"
        _write_events(sig_path, events)
        monkeypatch.setattr(mw, "SIG_EVENTS", sig_path)
        monkeypatch.setattr(mw, "MACRO_DIR", tmp_path)
        monkeypatch.setattr(mw, "_watchlist_symbols", lambda: {"NVDA", "JPM"})

        haiku_payload = {
            "regime_shift": False, "regime_note": None,
            "top_events": [{"headline": "Fed emergency cut", "impact": "high",
                            "affected_symbols": [], "direction": "bearish"}],
            "watchlist_catalysts": {"JPM": "banking stress"},
            "macro_themes": ["risk-off"], "risk_flags": ["contagion"],
            "overnight_summary": "Fed emergency cut amid banking stress.",
        }
        with patch.object(mw._claude.messages, "create",
                          return_value=_fake_haiku_response(haiku_payload)):
            digest = mw.write_overnight_digest(window_hours=12)

        assert digest is not None
        assert digest["overnight_summary"].startswith("Fed")
        assert digest["events_qualifying"] == 2
        out = list(tmp_path.glob("overnight_digest_*.json"))
        assert len(out) == 1
        on_disk = json.loads(out[0].read_text())
        assert on_disk["overnight_summary"] == digest["overnight_summary"]
        assert "generated_at" in on_disk
        assert on_disk["window_hours"] == 12

    def test_returns_none_when_no_events(self, tmp_path, monkeypatch):
        import macro_wire as mw
        sig_path = tmp_path / "significant_events.jsonl"
        sig_path.write_text("")
        monkeypatch.setattr(mw, "SIG_EVENTS", sig_path)
        monkeypatch.setattr(mw, "MACRO_DIR", tmp_path)
        monkeypatch.setattr(mw, "_watchlist_symbols", lambda: set())
        # Patch Haiku so we can assert no call was made
        with patch.object(mw._claude.messages, "create") as mock:
            result = mw.write_overnight_digest(window_hours=12)
        assert result is None
        mock.assert_not_called()

    def test_filters_by_score_threshold(self, tmp_path, monkeypatch):
        """Sub-6 score with no watchlist match excluded → no call, returns None."""
        import macro_wire as mw
        ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        events = [{"ts": ts, "headline": "Minor", "impact_score": 3.0,
                   "keyword_tier": "low", "affected_symbols": ["XYZ"]}]
        sig_path = tmp_path / "significant_events.jsonl"
        _write_events(sig_path, events)
        monkeypatch.setattr(mw, "SIG_EVENTS", sig_path)
        monkeypatch.setattr(mw, "MACRO_DIR", tmp_path)
        monkeypatch.setattr(mw, "_watchlist_symbols", lambda: {"NVDA"})
        with patch.object(mw._claude.messages, "create") as mock:
            result = mw.write_overnight_digest(window_hours=12)
        assert result is None
        mock.assert_not_called()

    def test_watchlist_symbol_qualifies_low_score_event(self, tmp_path, monkeypatch):
        """Score=2.0 + watchlist symbol overlap → qualifies → Haiku is called."""
        import macro_wire as mw
        ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        events = [{"ts": ts, "headline": "NVDA chip rumor", "impact_score": 2.0,
                   "keyword_tier": "low", "affected_symbols": ["NVDA"]}]
        sig_path = tmp_path / "significant_events.jsonl"
        _write_events(sig_path, events)
        monkeypatch.setattr(mw, "SIG_EVENTS", sig_path)
        monkeypatch.setattr(mw, "MACRO_DIR", tmp_path)
        monkeypatch.setattr(mw, "_watchlist_symbols", lambda: {"NVDA"})
        haiku_payload = {
            "overnight_summary": "x", "top_events": [],
            "watchlist_catalysts": {}, "macro_themes": [], "risk_flags": [],
        }
        with patch.object(mw._claude.messages, "create",
                          return_value=_fake_haiku_response(haiku_payload)) as mock:
            digest = mw.write_overnight_digest(window_hours=12)
        mock.assert_called_once()
        assert digest is not None
        assert digest["events_qualifying"] == 1

    def test_haiku_failure_is_non_fatal(self, tmp_path, monkeypatch):
        import macro_wire as mw
        ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        events = [{"ts": ts, "headline": "Fed cut", "impact_score": 9.0,
                   "keyword_tier": "critical", "affected_symbols": []}]
        sig_path = tmp_path / "significant_events.jsonl"
        _write_events(sig_path, events)
        monkeypatch.setattr(mw, "SIG_EVENTS", sig_path)
        monkeypatch.setattr(mw, "MACRO_DIR", tmp_path)
        monkeypatch.setattr(mw, "_watchlist_symbols", lambda: set())
        with patch.object(mw._claude.messages, "create",
                          side_effect=RuntimeError("API down")):
            result = mw.write_overnight_digest(window_hours=12)
        assert result is None  # graceful

    def test_output_schema_has_required_metadata(self, tmp_path, monkeypatch):
        """Output JSON contains generated_at, window_hours, events_considered/qualifying."""
        import macro_wire as mw
        ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        events = [{"ts": ts, "headline": "Big news", "impact_score": 8.0,
                   "keyword_tier": "critical", "affected_symbols": []}]
        sig_path = tmp_path / "significant_events.jsonl"
        _write_events(sig_path, events)
        monkeypatch.setattr(mw, "SIG_EVENTS", sig_path)
        monkeypatch.setattr(mw, "MACRO_DIR", tmp_path)
        monkeypatch.setattr(mw, "_watchlist_symbols", lambda: set())
        haiku_payload = {"overnight_summary": "s"}
        with patch.object(mw._claude.messages, "create",
                          return_value=_fake_haiku_response(haiku_payload)):
            digest = mw.write_overnight_digest(window_hours=12)
        for key in ("generated_at", "window_hours", "events_considered", "events_qualifying"):
            assert key in digest

    def test_strips_markdown_fences_from_haiku_output(self, tmp_path, monkeypatch):
        import macro_wire as mw
        ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        events = [{"ts": ts, "headline": "x", "impact_score": 9.0,
                   "keyword_tier": "critical", "affected_symbols": []}]
        sig_path = tmp_path / "significant_events.jsonl"
        _write_events(sig_path, events)
        monkeypatch.setattr(mw, "SIG_EVENTS", sig_path)
        monkeypatch.setattr(mw, "MACRO_DIR", tmp_path)
        monkeypatch.setattr(mw, "_watchlist_symbols", lambda: set())

        resp = MagicMock()
        # Wrap a valid JSON object in a markdown fence
        resp.content = [MagicMock(
            text='```json\n{"overnight_summary": "ok"}\n```'
        )]
        resp.usage = MagicMock(
            input_tokens=10, output_tokens=5,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        )
        with patch.object(mw._claude.messages, "create", return_value=resp):
            digest = mw.write_overnight_digest(window_hours=12)
        assert digest is not None
        assert digest["overnight_summary"] == "ok"


# ─────────────────────────────────────────────────────────────────────────────
# _load_overnight_digest()
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadOvernightDigest:
    def test_returns_empty_when_no_digest(self, tmp_path, monkeypatch):
        import morning_brief as mb
        # Point _BASE_DIR at empty tmp_path so digest_dir doesn't exist
        monkeypatch.setattr(mb, "_BASE_DIR", tmp_path)
        assert mb._load_overnight_digest() == ""

    def test_staleness_gate_ignores_old_digest(self, tmp_path, monkeypatch):
        import morning_brief as mb
        digest_dir = tmp_path / "data" / "macro_wire"
        digest_dir.mkdir(parents=True)
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=15)).isoformat()
        (digest_dir / "overnight_digest_2026-01-01.json").write_text(json.dumps({
            "overnight_summary": "stale", "generated_at": old_ts,
            "window_hours": 12, "events_qualifying": 5,
        }))
        monkeypatch.setattr(mb, "_BASE_DIR", tmp_path)
        assert mb._load_overnight_digest() == ""

    def test_fresh_digest_is_loaded_and_formatted(self, tmp_path, monkeypatch):
        import morning_brief as mb
        digest_dir = tmp_path / "data" / "macro_wire"
        digest_dir.mkdir(parents=True)
        fresh_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        (digest_dir / "overnight_digest_2026-04-26.json").write_text(json.dumps({
            "overnight_summary": "Asia opened weak on tariff news.",
            "regime_shift": True, "regime_note": "risk-off shift",
            "macro_themes": ["risk-off", "tariffs"],
            "watchlist_catalysts": {"NVDA": "chip ban risk"},
            "risk_flags": ["china tension"],
            "top_events": [{"headline": "China retaliation", "impact": "high",
                            "affected_symbols": ["NVDA", "TSM"]}],
            "generated_at": fresh_ts,
            "window_hours": 12, "events_qualifying": 7,
        }))
        monkeypatch.setattr(mb, "_BASE_DIR", tmp_path)
        out = mb._load_overnight_digest()
        assert "OVERNIGHT MACRO DIGEST" in out
        assert "Asia opened weak" in out
        assert "REGIME SHIFT" in out
        assert "NVDA: chip ban risk" in out
        assert "[7 qualifying events" in out

    def test_picks_most_recent_digest(self, tmp_path, monkeypatch):
        import morning_brief as mb
        digest_dir = tmp_path / "data" / "macro_wire"
        digest_dir.mkdir(parents=True)
        fresh_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        (digest_dir / "overnight_digest_2026-04-25.json").write_text(json.dumps({
            "overnight_summary": "older", "generated_at": fresh_ts,
        }))
        (digest_dir / "overnight_digest_2026-04-26.json").write_text(json.dumps({
            "overnight_summary": "newest", "generated_at": fresh_ts,
        }))
        monkeypatch.setattr(mb, "_BASE_DIR", tmp_path)
        out = mb._load_overnight_digest()
        assert "newest" in out


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler digest jobs
# ─────────────────────────────────────────────────────────────────────────────

class TestSchedulerDigestJobs:
    def test_overnight_job_dry_run_inside_window_marks_date(self, monkeypatch):
        import scheduler as s
        # Reset state
        s._overnight_digest_written_date = ""
        # Build a Tuesday 4:05 AM ET datetime
        fake_now = datetime(2026, 4, 28, 4, 5, tzinfo=s.ET)
        with patch.object(s, "datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            s._maybe_write_overnight_digest(dry_run=True)
        assert s._overnight_digest_written_date == "2026-04-28"

    def test_overnight_job_skips_outside_window(self, monkeypatch):
        import scheduler as s
        s._overnight_digest_written_date = ""
        fake_now = datetime(2026, 4, 28, 9, 0, tzinfo=s.ET)
        with patch.object(s, "datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            s._maybe_write_overnight_digest(dry_run=True)
        assert s._overnight_digest_written_date == ""

    def test_overnight_job_skips_weekend(self, monkeypatch):
        import scheduler as s
        s._overnight_digest_written_date = ""
        fake_now = datetime(2026, 4, 25, 4, 5, tzinfo=s.ET)  # Saturday
        with patch.object(s, "datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            s._maybe_write_overnight_digest(dry_run=True)
        assert s._overnight_digest_written_date == ""

    def test_overnight_job_fires_only_once_per_day(self, monkeypatch):
        import scheduler as s
        s._overnight_digest_written_date = "2026-04-28"
        fake_now = datetime(2026, 4, 28, 4, 5, tzinfo=s.ET)
        called = []
        def fake_write(window_hours=12):
            called.append(window_hours)
        with patch.object(s, "datetime") as mock_dt, \
             patch("macro_wire.write_overnight_digest", side_effect=fake_write):
            mock_dt.now.return_value = fake_now
            s._maybe_write_overnight_digest(dry_run=False)
        assert called == []  # date already marked → noop

    def test_eod_job_dry_run_inside_window_marks_date(self, monkeypatch):
        import scheduler as s
        s._eod_digest_written_date = ""
        fake_now = datetime(2026, 4, 28, 16, 20, tzinfo=s.ET)
        with patch.object(s, "datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            s._maybe_write_eod_digest(dry_run=True)
        assert s._eod_digest_written_date == "2026-04-28"

    def test_eod_job_skips_outside_window(self, monkeypatch):
        import scheduler as s
        s._eod_digest_written_date = ""
        fake_now = datetime(2026, 4, 28, 12, 0, tzinfo=s.ET)
        with patch.object(s, "datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            s._maybe_write_eod_digest(dry_run=True)
        assert s._eod_digest_written_date == ""


# ─────────────────────────────────────────────────────────────────────────────
# Morning-brief integration
# ─────────────────────────────────────────────────────────────────────────────

class TestMorningBriefDigestIntegration:
    def test_load_context_works_without_digest(self, tmp_path, monkeypatch):
        import morning_brief as mb
        monkeypatch.setattr(mb, "_BASE_DIR", tmp_path)
        # _load_context wraps every section in try/except so it never raises
        ctx = mb._load_context()
        assert isinstance(ctx, str)
        assert "OVERNIGHT MACRO DIGEST" not in ctx

    def test_load_context_includes_overnight_digest_when_fresh(self, tmp_path, monkeypatch):
        import morning_brief as mb
        digest_dir = tmp_path / "data" / "macro_wire"
        digest_dir.mkdir(parents=True)
        fresh_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        (digest_dir / "overnight_digest_2026-04-26.json").write_text(json.dumps({
            "overnight_summary": "Asia weak.",
            "generated_at": fresh_ts,
            "events_qualifying": 3, "window_hours": 12,
        }))
        monkeypatch.setattr(mb, "_BASE_DIR", tmp_path)
        ctx = mb._load_context()
        assert "OVERNIGHT MACRO DIGEST" in ctx
        assert "Asia weak" in ctx
