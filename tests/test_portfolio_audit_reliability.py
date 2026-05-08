"""tests/test_portfolio_audit_reliability.py — reliability bugs in the
portfolio audit system. Two bugs fixed in this suite:

  Fix A (scripts/portfolio_audit.py):
    1. Raw Haiku response is logged on parse failure.
    2. One retry is issued with the parse error appended to the prompt.
    3. After both attempts fail, fallback positions carry verdict='unknown'
       (sentinel) instead of being silently coerced to 'yellow'.
    4. WhatsApp message includes an "[AUDIT DEGRADED]" banner when fallback
       is active.

  Fix B (scheduler.py):
    5. Audit slot completion is persisted to data/runtime/audit_slots_ran.json.
    6. On scheduler restart, persisted slots for today are loaded so a
       slot that already ran is not re-fired.
    7. Persisted slot file from a previous day is discarded on load.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

# ─────────────────────────────────────────────────────────────────────────────
# Fix A — JSON parser tests
# ─────────────────────────────────────────────────────────────────────────────

class _FakeBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeResp:
    def __init__(self, text: str) -> None:
        self.content = [_FakeBlock(text)]
        self.usage   = mock.MagicMock(input_tokens=10, output_tokens=20)


class _FakeMessages:
    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._replies:
            raise AssertionError("FakeMessages exhausted")
        return _FakeResp(self._replies.pop(0))


class _FakeAnthropic:
    def __init__(self, replies: list[str]) -> None:
        self.messages = _FakeMessages(replies)

    def __call__(self, *args, **kwargs):
        return self


def _patch_anthropic(replies: list[str]):
    """Build a context manager that swaps anthropic.Anthropic in for the test."""
    fake_module = mock.MagicMock()
    fake_module.Anthropic = lambda **kw: _FakeAnthropic(replies)  # noqa: ARG005
    return mock.patch.dict("sys.modules", {"anthropic": fake_module})


def test_haiku_response_logged_on_parse_failure(caplog):
    """When Haiku output fails to parse, the raw text must be logged at WARNING."""
    from scripts import portfolio_audit as pa

    junk = "Here is the audit:\n[not actually json]"
    with _patch_anthropic([junk, junk]), \
         mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
        caplog.set_level(logging.WARNING, logger="portfolio_audit")
        positions, meta = pa._synthesize_with_haiku([], [])

    assert positions == []
    assert meta["ok"] is False
    # The raw text must appear in at least one WARNING record
    raw_in_logs = any(
        "Haiku raw response" in rec.getMessage() and "not actually json" in rec.getMessage()
        for rec in caplog.records
    )
    assert raw_in_logs, "raw response not logged on parse failure"


def test_retry_on_parse_failure():
    """First reply unparseable → second call must include the parse error."""
    from scripts import portfolio_audit as pa

    bad  = "I cannot output JSON sorry"
    good = json.dumps({"positions": [{"symbol": "TST", "verdict": "green"}]})

    fake = _FakeAnthropic([bad, good])
    fake_module = mock.MagicMock()
    fake_module.Anthropic = lambda **kw: fake  # noqa: ARG005

    with mock.patch.dict("sys.modules", {"anthropic": fake_module}), \
         mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
        positions, meta = pa._synthesize_with_haiku([], [])

    assert meta["retried"] is True
    assert len(fake.messages.calls) == 2
    second_prompt = fake.messages.calls[1]["messages"][0]["content"]
    assert "previous response failed JSON parsing" in second_prompt
    assert "valid JSON, no preamble, no markdown" in second_prompt
    assert positions == [{"symbol": "TST", "verdict": "green"}]
    assert meta["ok"] is True


def test_unknown_sentinel_on_double_failure():
    """Both Haiku attempts fail → run_portfolio_audit returns verdicts of
    'unknown' (sentinel), never silently 'yellow'."""
    from scripts import portfolio_audit as pa

    junk = "still no json"
    rows_a1 = [{
        "symbol": "AAPL", "qty": 10, "unrealized_pl": 0.0,
        "unrealized_plpc": 0.0, "stop_price": None, "earnings_days_away": None,
        "bracket_protected": False, "capture_found": False,
        "reasoning": None, "catalyst": None,
    }]

    with _patch_anthropic([junk, junk]), \
         mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}), \
         mock.patch.object(pa, "_get_alpaca_a1", return_value=mock.MagicMock()), \
         mock.patch.object(pa, "_get_alpaca_a2", return_value=mock.MagicMock()), \
         mock.patch.object(pa, "_collect_a1", return_value=rows_a1), \
         mock.patch.object(pa, "_collect_a2", return_value=[]), \
         mock.patch.object(pa, "_fetch_equity", return_value=100_000.0), \
         mock.patch.object(pa, "_AUDIT_LATEST", Path("/tmp/_test_audit_latest.json")), \
         mock.patch.object(pa, "_AUDIT_LOG",    Path("/tmp/_test_audit_log.jsonl")), \
         mock.patch.object(pa, "_REPORTS_DIR",  Path("/tmp")):
        result = pa.run_portfolio_audit(send_whatsapp=False)

    verdicts = [(p["symbol"], p["verdict"]) for p in result["positions"]]
    assert ("AAPL", "unknown") in verdicts, f"expected unknown sentinel, got {verdicts}"
    assert all(v != "yellow" for _, v in verdicts), "fallback must NOT use 'yellow'"
    assert result["fallback_used"] is True
    assert result["n_unknown"] == 1
    assert result["n_yellow"]  == 0


def test_valid_json_parses_correctly():
    """Well-formed Haiku response → verdicts extracted, no fallback."""
    from scripts import portfolio_audit as pa

    payload = json.dumps({"positions": [
        {"symbol": "MSFT", "verdict": "green", "account": "A1"},
        {"symbol": "NVDA", "verdict": "red",   "account": "A1"},
    ]})

    with _patch_anthropic([payload]), \
         mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
        positions, meta = pa._synthesize_with_haiku([], [])

    assert meta["ok"] is True
    assert meta["retried"] is False
    verdicts = {p["symbol"]: p["verdict"] for p in positions}
    assert verdicts == {"MSFT": "green", "NVDA": "red"}


def test_whatsapp_alert_indicates_fallback():
    """When fallback is active the formatted message contains 'AUDIT DEGRADED'."""
    from scripts import portfolio_audit as pa

    positions = [{
        "symbol": "AAPL", "account": "A1", "verdict": "unknown",
        "structure": "10 shares long", "pnl_str": "+$0 (+0.0%)",
        "stop_status": "no stop",
    }]
    msg = pa._format_audit_message(
        equity_a1=100_000.0,
        equity_a2=100_000.0,
        positions=positions,
        time_et=datetime(2026, 5, 7, 9, 12),
        fallback_used=True,
        raw_excerpt="Here is the audit: prose preamble...",
    )
    assert "[AUDIT DEGRADED]" in msg
    assert "1 positions unrated" in msg
    assert "Here is the audit" in msg
    # Must NOT advertise yellow when fallback is active
    assert "1 🟡" not in msg.split("Overall:")[1]


# ─────────────────────────────────────────────────────────────────────────────
# Fix B — Slot persistence tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def slot_path(tmp_path, monkeypatch):
    """Redirect _AUDIT_SLOTS_PATH to a tmp file. Returns the Path."""
    import scheduler
    p = tmp_path / "audit_slots_ran.json"
    monkeypatch.setattr(scheduler, "_AUDIT_SLOTS_PATH", p)
    monkeypatch.setattr(scheduler, "_portfolio_audit_slots_ran", set())
    return p


def test_slot_persisted_to_disk(slot_path, monkeypatch):
    """_persist_audit_slots_ran writes today's slot to disk."""
    import scheduler

    monkeypatch.setattr(scheduler, "_portfolio_audit_slots_ran", {"2026-05-07-open"})
    fake_now = datetime(2026, 5, 7, 9, 12, tzinfo=scheduler.ET)
    with mock.patch.object(scheduler, "datetime") as md:
        md.now.return_value = fake_now
        scheduler._persist_audit_slots_ran()

    assert slot_path.exists()
    saved = json.loads(slot_path.read_text())
    assert saved["date"] == "2026-05-07"
    assert "2026-05-07-open" in saved["slots"]


def test_slot_not_rerun_after_restart(slot_path, monkeypatch):
    """Slot file exists for today → load returns the persisted set, audit
    function sees slot already ran and does not re-fire."""
    import scheduler

    slot_path.write_text(json.dumps({
        "date": "2026-05-07",
        "slots": ["2026-05-07-open"],
    }))

    fake_now = datetime(2026, 5, 7, 9, 12, tzinfo=scheduler.ET)
    with mock.patch.object(scheduler, "datetime") as md:
        md.now.return_value = fake_now
        loaded = scheduler._load_audit_slots_ran()

    assert loaded == {"2026-05-07-open"}

    # Now drive _maybe_run_portfolio_audit and confirm it short-circuits.
    monkeypatch.setattr(scheduler, "_portfolio_audit_slots_ran", loaded)

    called = {"hit": False}
    def _fake_run(*args, **kwargs):  # pragma: no cover — must NOT be called
        called["hit"] = True
        return {"positions": []}

    fake_audit_module = mock.MagicMock()
    fake_audit_module.run_portfolio_audit = _fake_run
    fake_scripts_module = mock.MagicMock()
    fake_scripts_module.portfolio_audit = fake_audit_module

    with mock.patch.object(scheduler, "datetime") as md, \
         mock.patch.object(scheduler, "_today", return_value="2026-05-07"), \
         mock.patch.dict("sys.modules", {
             "scripts": fake_scripts_module,
             "scripts.portfolio_audit": fake_audit_module,
         }):
        md.now.return_value = fake_now
        scheduler._maybe_run_portfolio_audit(dry_run=False)

    assert called["hit"] is False, "audit re-fired despite persisted slot"


def test_slot_resets_for_new_day(slot_path):
    """Slot file from yesterday is discarded; fresh empty set returned."""
    import scheduler

    slot_path.write_text(json.dumps({
        "date": "2026-05-06",
        "slots": ["2026-05-06-open", "2026-05-06-close"],
    }))

    fake_now = datetime(2026, 5, 7, 9, 12, tzinfo=scheduler.ET)
    with mock.patch.object(scheduler, "datetime") as md:
        md.now.return_value = fake_now
        loaded = scheduler._load_audit_slots_ran()

    assert loaded == set(), "stale-day slots must be discarded"


# ─────────────────────────────────────────────────────────────────────────────
# QW1 / #51 — max_tokens bump and batching
# ─────────────────────────────────────────────────────────────────────────────

def test_max_tokens_set_to_8192():
    """Single Haiku call uses max_tokens=8192 (Haiku-4.5 max output)."""
    from scripts import portfolio_audit as pa

    payload = json.dumps({"positions": [{"symbol": "TST", "verdict": "green"}]})
    fake = _FakeAnthropic([payload])
    fake_module = mock.MagicMock()
    fake_module.Anthropic = lambda **kw: fake  # noqa: ARG005

    with mock.patch.dict("sys.modules", {"anthropic": fake_module}), \
         mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
        positions, meta = pa._synthesize_with_haiku([{"symbol": "TST"}], [])

    assert meta["ok"] is True
    assert len(fake.messages.calls) == 1
    assert fake.messages.calls[0]["max_tokens"] == 8192


def test_batching_when_over_threshold():
    """25 combined rows → 2 Haiku calls (chunks of <= 15)."""
    from scripts import portfolio_audit as pa

    payload = json.dumps({"positions": [{"symbol": "X", "verdict": "green"}]})
    # We need 2 replies — one per batch. Add a third as buffer.
    fake = _FakeAnthropic([payload, payload, payload])
    fake_module = mock.MagicMock()
    fake_module.Anthropic = lambda **kw: fake  # noqa: ARG005

    rows_a1 = [{"symbol": f"A{i}"} for i in range(15)]
    rows_a2 = [{"symbol": f"B{i}"} for i in range(10)]

    with mock.patch.dict("sys.modules", {"anthropic": fake_module}), \
         mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
        positions, meta = pa._synthesize_with_haiku(rows_a1, rows_a2)

    assert meta["ok"] is True
    assert len(fake.messages.calls) == 2, (
        "expected 2 batched calls for 25 rows / batch_size=15"
    )
    for call in fake.messages.calls:
        assert call["max_tokens"] == 8192
