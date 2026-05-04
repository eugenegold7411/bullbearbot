"""
Tests for daily brief append model (BR-01 through BR-09).
"""
import json
import sys
import types
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch


# ── stub dotenv so morning_brief can import without .env present ──────────────
def _ensure_stubs():
    if "dotenv" not in sys.modules:
        m = types.ModuleType("dotenv")
        m.load_dotenv = lambda *a, **kw: None
        sys.modules["dotenv"] = m
    if "anthropic" not in sys.modules:
        m = types.ModuleType("anthropic")
        m.Anthropic = MagicMock
        sys.modules["anthropic"] = m


_ensure_stubs()


def _make_brief(brief_type="premarket"):
    return {
        "brief_type": brief_type,
        "generated_at": datetime.now().isoformat(),
        "next_update_at": (datetime.now() + timedelta(hours=1)).isoformat(),
        "market_regime": {"regime": "risk_on", "score": 72, "confidence": "high",
                          "tone": "Constructive.", "key_drivers": ["energy"], "todays_events": []},
        "sector_snapshot": [],
        "high_conviction_longs": [
            {"symbol": "AAPL", "score": 80, "conviction": "HIGH", "rank": 1,
             "catalyst": "Beat earnings", "entry_zone": "180-182", "stop": 175.0,
             "stop_pct": 0.03, "target": 195.0, "target_pct": 0.08, "risk_reward": 2.5,
             "technical_summary": "Breakout.", "a2_strategy_note": "N/A", "risk_note": "Macro risk."},
        ],
        "high_conviction_bearish": [],
        "current_positions": {"a1_equity": [], "a2_options": []},
        "watch_list": [],
        "earnings_pipeline": [],
        "insider_activity": {"high_conviction": [], "congressional": [], "form4_purchases": []},
        "macro_wire_alerts": [],
        "avoid_list": [],
        "latest_updates": [],
    }


# ── BR-01: _append_to_daily_brief creates file on first call ─────────────────

def test_br01_creates_daily_file(tmp_path):
    import morning_brief as mb

    brief = _make_brief("premarket")
    with patch.object(mb, "_BRIEFS_DIR", tmp_path / "briefs"):
        mb._append_to_daily_brief(brief)

    files = list((tmp_path / "briefs").glob("morning_brief_????????.json"))
    assert len(files) == 1, "Expected exactly one daily brief file"


# ── BR-02: second call appends a second entry ─────────────────────────────────

def test_br02_appends_second_entry(tmp_path):
    import morning_brief as mb

    briefs_dir = tmp_path / "briefs"
    with patch.object(mb, "_BRIEFS_DIR", briefs_dir):
        mb._append_to_daily_brief(_make_brief("premarket"))
        mb._append_to_daily_brief(_make_brief("market_open"))

    files = list(briefs_dir.glob("morning_brief_????????.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert len(data["updates"]) == 2
    assert data["updates"][0]["brief_type"] == "premarket"
    assert data["updates"][1]["brief_type"] == "market_open"


# ── BR-03: daily file has correct structure ───────────────────────────────────

def test_br03_daily_file_structure(tmp_path):
    import morning_brief as mb

    briefs_dir = tmp_path / "briefs"
    with patch.object(mb, "_BRIEFS_DIR", briefs_dir):
        mb._append_to_daily_brief(_make_brief("premarket"))

    data = json.loads(list(briefs_dir.glob("*.json"))[0].read_text())
    assert "date" in data, "Missing 'date' key"
    assert "updates" in data, "Missing 'updates' key"
    assert isinstance(data["updates"], list)
    assert len(data["updates"]) == 1


# ── BR-04: _save_intelligence_briefs calls _append_to_daily_brief ─────────────

def test_br04_save_calls_append(tmp_path):
    import morning_brief as mb

    brief = _make_brief()
    called = []

    def _fake_append(b):
        called.append(b)

    with (
        patch.object(mb, "_DATA_DIR", tmp_path),
        patch.object(mb, "_FULL_BRIEF_FILE", tmp_path / "morning_brief_full.json"),
        patch.object(mb, "_SONNET_BRIEF_FILE", tmp_path / "morning_brief_sonnet.json"),
        patch.object(mb, "_BRIEF_FILE", tmp_path / "morning_brief.json"),
        patch.object(mb, "_ARCHIVE", tmp_path / "archive"),
        patch.object(mb, "_append_to_daily_brief", side_effect=_fake_append),
    ):
        mb._save_intelligence_briefs(brief)

    assert len(called) == 1, "_append_to_daily_brief should be called exactly once"


# ── BR-05: files older than 14 days are pruned ───────────────────────────────

def test_br05_old_files_pruned(tmp_path):
    from zoneinfo import ZoneInfo

    import morning_brief as mb

    briefs_dir = tmp_path / "briefs"
    briefs_dir.mkdir()

    et = ZoneInfo("America/New_York")

    # Create a 15-day-old file (use ET to match production pruning logic)
    old_date = (datetime.now(et) - timedelta(days=15)).strftime("%Y%m%d")
    old_file = briefs_dir / f"morning_brief_{old_date}.json"
    old_file.write_text(json.dumps({"date": old_date, "updates": []}))

    # Create a 1-day-old file (should be kept)
    recent_date = (datetime.now(et) - timedelta(days=1)).strftime("%Y%m%d")
    recent_file = briefs_dir / f"morning_brief_{recent_date}.json"
    recent_file.write_text(json.dumps({"date": recent_date, "updates": []}))

    with patch.object(mb, "_BRIEFS_DIR", briefs_dir):
        mb._append_to_daily_brief(_make_brief())

    assert not old_file.exists(), "15-day-old file should have been pruned"
    assert recent_file.exists(), "1-day-old file should be kept"


# ── BR-06: corrupted daily file is recovered gracefully ──────────────────────

def test_br06_corrupted_file_recovered(tmp_path):
    import morning_brief as mb

    briefs_dir = tmp_path / "briefs"
    briefs_dir.mkdir()

    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y%m%d")
    corrupt_file = briefs_dir / f"morning_brief_{today}.json"
    corrupt_file.write_text("NOT VALID JSON {{{")

    with patch.object(mb, "_BRIEFS_DIR", briefs_dir):
        mb._append_to_daily_brief(_make_brief("premarket"))

    data = json.loads(corrupt_file.read_text())
    assert "updates" in data
    assert len(data["updates"]) == 1


# ── BR-07: live brief files (full/sonnet/legacy) still written ────────────────

def test_br07_live_files_still_written(tmp_path):
    import morning_brief as mb

    brief = _make_brief()
    with (
        patch.object(mb, "_DATA_DIR", tmp_path),
        patch.object(mb, "_FULL_BRIEF_FILE", tmp_path / "morning_brief_full.json"),
        patch.object(mb, "_SONNET_BRIEF_FILE", tmp_path / "morning_brief_sonnet.json"),
        patch.object(mb, "_BRIEF_FILE", tmp_path / "morning_brief.json"),
        patch.object(mb, "_ARCHIVE", tmp_path / "archive"),
        patch.object(mb, "_BRIEFS_DIR", tmp_path / "briefs"),
    ):
        mb._save_intelligence_briefs(brief)

    assert (tmp_path / "morning_brief_full.json").exists()
    assert (tmp_path / "morning_brief_sonnet.json").exists()
    assert (tmp_path / "morning_brief.json").exists()


# ── BR-08: daily file name uses ET date not UTC ───────────────────────────────

def test_br08_filename_uses_et_date(tmp_path):
    from zoneinfo import ZoneInfo

    import morning_brief as mb
    et_date = datetime.now(ZoneInfo("America/New_York")).strftime("%Y%m%d")
    expected_file = tmp_path / "briefs" / f"morning_brief_{et_date}.json"

    with patch.object(mb, "_BRIEFS_DIR", tmp_path / "briefs"):
        mb._append_to_daily_brief(_make_brief())

    assert expected_file.exists(), f"Expected file {expected_file.name} to exist"


# ── BR-09: updates array preserves all brief fields intact ───────────────────

def test_br09_updates_preserve_all_fields(tmp_path):
    import morning_brief as mb

    brief = _make_brief("market_open")
    brief["market_regime"]["score"] = 88
    brief["high_conviction_longs"][0]["symbol"] = "TSLA"

    briefs_dir = tmp_path / "briefs"
    with patch.object(mb, "_BRIEFS_DIR", briefs_dir):
        mb._append_to_daily_brief(brief)

    data = json.loads(list(briefs_dir.glob("*.json"))[0].read_text())
    saved = data["updates"][0]
    assert saved["brief_type"] == "market_open"
    assert saved["market_regime"]["score"] == 88
    assert saved["high_conviction_longs"][0]["symbol"] == "TSLA"
