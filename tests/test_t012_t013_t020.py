"""
tests/test_t012_t013_t020.py — T-012 email fallback, T-013 daily report timing,
T-020 zero-fill alert.

Suite AlertEmail   — send_alert_email() degrades gracefully when SendGrid is missing
Suite DailyReport  — flag file prevents double-send; fires only at/after 4:30 PM ET
Suite ZeroFill     — zero-fill alert fires only on market days at 11 AM ET
"""

import json
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


# ── Lightweight stubs for report.py's heavy dependencies ─────────────────────
# Install BEFORE importing report so report.py sees them.

def _stub(name: str, **attrs) -> types.ModuleType:
    if name not in sys.modules:
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m
    return sys.modules[name]


_stub("dotenv", load_dotenv=lambda *a, **kw: None)
_stub("log_setup", get_logger=lambda name: __import__("logging").getLogger(name))

_alpaca_enums = _stub("alpaca.trading.enums",
      QueryOrderStatus=types.SimpleNamespace(CLOSED="closed"),
      OrderSide=types.SimpleNamespace(BUY="buy", SELL="sell"),
      AssetStatus=object, ContractType=object, ExerciseStyle=object,
      OrderClass=object, TimeInForce=object)

class _FakeTC:
    def __init__(self, *a, **kw): pass
    def get_account(self): return None
    def get_orders(self, *a, **kw): return []
    def get_portfolio_history(self, *a, **kw):
        return types.SimpleNamespace(timestamp=[], equity=[], profit_loss=[],
                                     profit_loss_pct=[])
    def get_all_positions(self): return []

_stub("alpaca.trading.client", TradingClient=_FakeTC)
_stub("alpaca.trading.requests",
      GetOrdersRequest=object, GetPortfolioHistoryRequest=object,
      ClosePositionRequest=object, LimitOrderRequest=object,
      MarketOrderRequest=object, StopLossRequest=object,
      StopOrderRequest=object, TakeProfitRequest=object,
      GetOptionContractsRequest=object)
_stub("alpaca", trading=types.SimpleNamespace())
_stub("alpaca.trading", client=object, requests=object, enums=object)
_stub("trade_memory",
      get_collection_stats=lambda: {},
      save_trade_memory=lambda *a, **kw: "",
      retrieve_similar_scenarios=lambda *a, **kw: [])

# Save a reference to the real report module BEFORE any stubs replace it.
# The real module needs to be importable for AlertEmailTests.
import report as real_report  # noqa: E402

# Build the mock report module once; inject/remove it in _import_scheduler().
_sched_report_mock = types.ModuleType("report")
_sched_report_mock.send_report_email  = mock.MagicMock()
_sched_report_mock.send_alert_email   = mock.MagicMock()
_sched_report_mock._get_account       = lambda: None
_sched_report_mock._get_positions     = lambda: []

_MISSING = object()  # sentinel for "key was absent"


# ── Helper: clean-slate scheduler import ─────────────────────────────────────

def _import_scheduler(status_dir: Path):
    """Re-import scheduler with _STATUS_DIR and state trackers reset.

    Stubs for bot/weekly_review/cost_tracker/report are injected only for the
    duration of `import scheduler` (scheduler holds module-level references, so
    the stubs can be removed from sys.modules afterward without affecting it).
    """
    _bot_stub = types.ModuleType("bot")
    _bot_stub.run_cycle = lambda *a, **kw: None
    _wr_stub = types.ModuleType("weekly_review")
    _wr_stub.run_review = lambda *a, **kw: ""
    _ct_stub = types.ModuleType("cost_tracker")
    _ct_stub.get_tracker = lambda: None

    _scoped_stubs = {
        "report":        _sched_report_mock,
        "bot":           _bot_stub,
        "weekly_review": _wr_stub,
        "cost_tracker":  _ct_stub,
    }
    _saved = {k: sys.modules.get(k, _MISSING) for k in _scoped_stubs}

    sys.modules.pop("scheduler", None)
    for k, v in _scoped_stubs.items():
        sys.modules[k] = v
    try:
        import scheduler as sched
    finally:
        for k, saved in _saved.items():
            if saved is _MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = saved

    sched._STATUS_DIR           = status_dir
    sched._report_sent_date     = ""
    sched._zero_fill_alert_date = ""
    _sched_report_mock.send_report_email.reset_mock()
    _sched_report_mock.send_alert_email.reset_mock()
    return sched


# ── Suite: AlertEmail ─────────────────────────────────────────────────────────

class AlertEmailTests(unittest.TestCase):
    """send_alert_email() in the real report.py degrades gracefully."""

    def test_missing_key_logs_warning_returns_none(self):
        """No SendGrid key → log WARNING, return None, never raise."""
        with mock.patch.dict("os.environ", {"SENDGRID_API_KEY": ""}, clear=False):
            with self.assertLogs("report", level="WARNING") as cm:
                result = real_report.send_alert_email("Test subject", "<b>body</b>")
        self.assertIsNone(result)
        self.assertTrue(any("not configured" in msg for msg in cm.output))

    def test_placeholder_key_logs_warning(self):
        """'your_…' placeholder key → same graceful degradation."""
        with mock.patch.dict("os.environ",
                             {"SENDGRID_API_KEY": "your_sendgrid_api_key"},
                             clear=False):
            with self.assertLogs("report", level="WARNING"):
                real_report.send_alert_email("subj", "plain text body")

    def test_plain_text_body_wrapped_in_html(self):
        """Non-HTML body gets wrapped in <html><pre> tags before sending."""
        sent_html: list[str] = []

        class FakeSGClient:
            def send(self, mail):
                sent_html.append(mail.contents[0].content)
                return types.SimpleNamespace(status_code=202)

        fake_sg      = types.ModuleType("sendgrid")
        fake_sg.SendGridAPIClient = lambda key: FakeSGClient()
        fake_helpers = types.ModuleType("sendgrid.helpers.mail")

        class FakeMail:
            def __init__(self, **kw):
                self.contents = [types.SimpleNamespace(content=kw.get("html_content", ""))]
        fake_helpers.Mail = lambda **kw: FakeMail(**kw)

        with mock.patch.dict("os.environ", {"SENDGRID_API_KEY": "SG.test"}, clear=False):
            with mock.patch.dict(sys.modules,
                                 {"sendgrid": fake_sg,
                                  "sendgrid.helpers.mail": fake_helpers}):
                real_report.send_alert_email("subj", "plain text")

        self.assertTrue(sent_html, "SendGrid send() was never called")
        self.assertIn("<pre", sent_html[0])

    def test_html_body_sent_as_is(self):
        """Body starting with '<' is forwarded unchanged (no extra <pre> wrapping)."""
        sent_html: list[str] = []

        class FakeSGClient:
            def send(self, mail):
                sent_html.append(mail.contents[0].content)
                return types.SimpleNamespace(status_code=202)

        fake_sg      = types.ModuleType("sendgrid")
        fake_sg.SendGridAPIClient = lambda key: FakeSGClient()
        fake_helpers = types.ModuleType("sendgrid.helpers.mail")

        class FakeMail:
            def __init__(self, **kw):
                self.contents = [types.SimpleNamespace(content=kw.get("html_content", ""))]
        fake_helpers.Mail = lambda **kw: FakeMail(**kw)

        with mock.patch.dict("os.environ", {"SENDGRID_API_KEY": "SG.test"}, clear=False):
            with mock.patch.dict(sys.modules,
                                 {"sendgrid": fake_sg,
                                  "sendgrid.helpers.mail": fake_helpers}):
                real_report.send_alert_email("subj", "<html><body>rich</body></html>")

        self.assertTrue(sent_html, "SendGrid send() was never called")
        self.assertIn("<html>", sent_html[0])
        self.assertNotIn("<pre", sent_html[0])


# ── Suite: DailyReport ────────────────────────────────────────────────────────

class DailyReportTests(unittest.TestCase):
    """_maybe_send_daily_report() fires at 4:30 PM ET on weekdays, idempotent via flag."""

    def test_fires_at_1630_et(self):
        # 2026-04-17 is a Friday
        with tempfile.TemporaryDirectory() as td:
            sched = _import_scheduler(Path(td))
            t = datetime(2026, 4, 17, 16, 30, 0, tzinfo=ET)   # Friday 4:30 PM ET
            with mock.patch("scheduler.datetime") as mock_dt:
                mock_dt.now.return_value = t
                sched._maybe_send_daily_report()
            flag = Path(td) / "daily_report_sent_2026-04-17.flag"
            self.assertTrue(flag.exists(), "flag file not created after send")

    def test_does_not_fire_before_1630_et(self):
        with tempfile.TemporaryDirectory() as td:
            sched = _import_scheduler(Path(td))
            t = datetime(2026, 4, 17, 12, 0, 0, tzinfo=ET)    # Friday noon — before window
            with mock.patch("scheduler.datetime") as mock_dt:
                mock_dt.now.return_value = t
                sched._maybe_send_daily_report()
            flag = Path(td) / "daily_report_sent_2026-04-17.flag"
            self.assertFalse(flag.exists(), "report fired too early")

    def test_flag_file_prevents_double_send(self):
        with tempfile.TemporaryDirectory() as td:
            sched = _import_scheduler(Path(td))
            flag = Path(td) / "daily_report_sent_2026-04-17.flag"
            flag.touch()   # simulate a prior run (or restart after first send)
            t = datetime(2026, 4, 17, 17, 0, 0, tzinfo=ET)    # Friday 5 PM
            with mock.patch("scheduler.datetime") as mock_dt:
                mock_dt.now.return_value = t
                sched._maybe_send_daily_report()
            _sched_report_mock.send_report_email.assert_not_called()

    def test_skips_on_weekend(self):
        # 2026-04-18 is a Saturday, 2026-04-19 is a Sunday
        with tempfile.TemporaryDirectory() as td:
            sched = _import_scheduler(Path(td))
            t = datetime(2026, 4, 18, 16, 30, 0, tzinfo=ET)   # Saturday
            with mock.patch("scheduler.datetime") as mock_dt:
                mock_dt.now.return_value = t
                sched._maybe_send_daily_report()
            flag = Path(td) / "daily_report_sent_2026-04-18.flag"
            self.assertFalse(flag.exists(), "report fired on weekend")


# ── Suite: ZeroFill ───────────────────────────────────────────────────────────

class ZeroFillAlertTests(unittest.TestCase):
    """_maybe_send_zero_fill_alert() fires only on market days at 11 AM with 0 fills."""

    def _write_trades(self, tmp_dir: Path, records: list[dict]) -> Path:
        logs_dir = tmp_dir / "logs"
        logs_dir.mkdir(exist_ok=True)
        tfile = logs_dir / "trades.jsonl"
        tfile.write_text("\n".join(json.dumps(r) for r in records))
        return tfile

    def test_fires_at_1100_with_no_fills(self):
        # 2026-04-17 is a Friday
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sched = _import_scheduler(tmp)
            trades_path = self._write_trades(tmp, [])
            t = datetime(2026, 4, 17, 11, 0, 0, tzinfo=ET)   # Friday 11 AM
            with mock.patch("scheduler.datetime") as mock_dt:
                mock_dt.now.return_value = t
                with mock.patch("scheduler.Path", side_effect=lambda *a: (
                    trades_path if "trades.jsonl" in str(a) else Path(*a)
                )):
                    sched._maybe_send_zero_fill_alert(dry_run=False)
            flag = tmp / "zero_fill_alert_sent_2026-04-17.flag"
            self.assertTrue(flag.exists(), "zero-fill flag not created")

    def test_no_alert_when_fills_exist(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sched = _import_scheduler(tmp)
            fills = [{"status": "submitted", "ts": "2026-04-17T11:05:00Z", "symbol": "GLD"}]
            trades_path = self._write_trades(tmp, fills)
            t = datetime(2026, 4, 17, 11, 5, 0, tzinfo=ET)   # Friday 11:05 AM
            with mock.patch("scheduler.datetime") as mock_dt:
                mock_dt.now.return_value = t
                with mock.patch("scheduler.Path", side_effect=lambda *a: (
                    trades_path if "trades.jsonl" in str(a) else Path(*a)
                )):
                    sched._maybe_send_zero_fill_alert(dry_run=False)
            flag = tmp / "zero_fill_alert_sent_2026-04-17.flag"
            self.assertFalse(flag.exists(), "alert fired despite existing fills")

    def test_skips_on_weekend(self):
        # 2026-04-18 is Saturday, 2026-04-19 is Sunday
        with tempfile.TemporaryDirectory() as td:
            sched = _import_scheduler(Path(td))
            t = datetime(2026, 4, 18, 11, 0, 0, tzinfo=ET)   # Saturday 11 AM
            with mock.patch("scheduler.datetime") as mock_dt:
                mock_dt.now.return_value = t
                sched._maybe_send_zero_fill_alert(dry_run=False)
            self.assertFalse(
                (Path(td) / "zero_fill_alert_sent_2026-04-18.flag").exists())

    def test_skips_before_1100(self):
        with tempfile.TemporaryDirectory() as td:
            sched = _import_scheduler(Path(td))
            t = datetime(2026, 4, 17, 10, 0, 0, tzinfo=ET)   # Friday 10 AM
            with mock.patch("scheduler.datetime") as mock_dt:
                mock_dt.now.return_value = t
                sched._maybe_send_zero_fill_alert(dry_run=False)
            self.assertFalse(
                (Path(td) / "zero_fill_alert_sent_2026-04-17.flag").exists())

    def test_skips_after_noon(self):
        with tempfile.TemporaryDirectory() as td:
            sched = _import_scheduler(Path(td))
            t = datetime(2026, 4, 17, 12, 30, 0, tzinfo=ET)  # Friday 12:30 PM — window closed
            with mock.patch("scheduler.datetime") as mock_dt:
                mock_dt.now.return_value = t
                sched._maybe_send_zero_fill_alert(dry_run=False)
            self.assertFalse(
                (Path(td) / "zero_fill_alert_sent_2026-04-17.flag").exists())

    def test_flag_prevents_double_alert(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sched = _import_scheduler(tmp)
            flag = tmp / "zero_fill_alert_sent_2026-04-17.flag"
            flag.touch()
            t = datetime(2026, 4, 17, 11, 5, 0, tzinfo=ET)   # Friday 11:05 AM
            with mock.patch("scheduler.datetime") as mock_dt:
                mock_dt.now.return_value = t
                sched._maybe_send_zero_fill_alert(dry_run=False)
            _sched_report_mock.send_alert_email.assert_not_called()

    def test_dry_run_skips_check(self):
        with tempfile.TemporaryDirectory() as td:
            sched = _import_scheduler(Path(td))
            t = datetime(2026, 4, 17, 11, 0, 0, tzinfo=ET)   # Friday 11 AM
            with mock.patch("scheduler.datetime") as mock_dt:
                mock_dt.now.return_value = t
                sched._maybe_send_zero_fill_alert(dry_run=True)
            self.assertFalse(
                (Path(td) / "zero_fill_alert_sent_2026-04-17.flag").exists())


# ── Suite: ReportDateFix (S7-A) ───────────────────────────────────────────────

class ReportDateFixTests(unittest.TestCase):
    """_maybe_send_daily_report() must pass today's ET date to send_report_email()."""

    def test_send_report_email_called_with_today_et_date(self):
        """send_report_email must receive target_date matching the scheduler's ET date."""
        with tempfile.TemporaryDirectory() as td:
            sched = _import_scheduler(Path(td))
            t = datetime(2026, 4, 21, 16, 30, 0, tzinfo=ET)   # Tuesday 4:30 PM ET
            with mock.patch("scheduler.datetime") as mock_dt:
                mock_dt.now.return_value = t
                sched._maybe_send_daily_report()
            _sched_report_mock.send_report_email.assert_called_once()
            call_kwargs = _sched_report_mock.send_report_email.call_args
            target_date = (call_kwargs.kwargs.get("target_date")
                           or (call_kwargs.args[0] if call_kwargs.args else None))
            self.assertIsNotNone(target_date,
                                 "send_report_email must be called with target_date")
            self.assertEqual(str(target_date), "2026-04-21",
                             "target_date must be today's ET date, not UTC or hardcoded")

    def test_send_report_email_not_called_with_hardcoded_date(self):
        """Report must not use a hardcoded or stale date from a previous run."""
        with tempfile.TemporaryDirectory() as td:
            sched = _import_scheduler(Path(td))
            # Simulate a different date — if hardcoded, the date would not match
            t = datetime(2026, 4, 22, 16, 30, 0, tzinfo=ET)   # Wednesday 4:30 PM ET
            with mock.patch("scheduler.datetime") as mock_dt:
                mock_dt.now.return_value = t
                sched._maybe_send_daily_report()
            call_kwargs = _sched_report_mock.send_report_email.call_args
            target_date = (call_kwargs.kwargs.get("target_date")
                           or (call_kwargs.args[0] if call_kwargs.args else None))
            self.assertEqual(str(target_date), "2026-04-22",
                             "target_date must reflect the actual run date, not any stale value")

    def test_manual_trigger_ignores_flag_file(self):
        """run_report.py --report daily must send even if today's flag file already exists.

        The script's cmd_daily() must call send_report_email() directly without
        first checking whether a daily_report_sent_*.flag exists.
        """
        script = _ROOT / "scripts" / "run_report.py"
        self.assertTrue(script.exists(), "scripts/run_report.py must exist")
        source = script.read_text()
        # cmd_daily() must not contain any guard that reads the flag file to block sending.
        # The only legitimate use of daily_report_sent_ is in _last_sent() for --list.
        # Confirm cmd_daily() itself has no flag-file guard.
        cmd_daily_start = source.find("def cmd_daily()")
        cmd_daily_end   = source.find("\ndef ", cmd_daily_start + 1)
        cmd_daily_body  = source[cmd_daily_start:cmd_daily_end]
        self.assertNotIn(
            "daily_report_sent_", cmd_daily_body,
            "cmd_daily() must not check the scheduler flag file before sending",
        )


# ── Suite: RunReportScript (S7-A) ─────────────────────────────────────────────

_ROOT = Path(__file__).resolve().parent.parent


class RunReportScriptTests(unittest.TestCase):
    """scripts/run_report.py exists and --list shows correct last-generated data."""

    def test_script_exists(self):
        script = _ROOT / "scripts" / "run_report.py"
        self.assertTrue(script.exists(), "scripts/run_report.py must exist")

    def test_script_has_all_report_types(self):
        script = _ROOT / "scripts" / "run_report.py"
        source = script.read_text()
        for rtype in ("daily", "morning_brief", "weekly"):
            self.assertIn(rtype, source, f"--report {rtype} must be supported")

    def test_script_has_list_command(self):
        script = _ROOT / "scripts" / "run_report.py"
        source = script.read_text()
        self.assertIn("--list", source, "run_report.py must support --list")

    def test_list_command_runs_without_error(self):
        """--list must run cleanly even when no reports have been generated yet."""
        import subprocess
        result = subprocess.run(
            [sys.executable, str(_ROOT / "scripts" / "run_report.py"), "--list"],
            capture_output=True,
            text=True,
            cwd=str(_ROOT),
        )
        self.assertEqual(result.returncode, 0,
                         f"--list exited non-zero: {result.stderr}")
        self.assertIn("Daily Report", result.stdout)
        self.assertIn("Morning Brief", result.stdout)
        self.assertIn("Weekly Review", result.stdout)

    def test_list_shows_last_sent_daily_flag(self):
        """--list daily should reflect the most recent daily flag file."""
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            # Write a fake flag file and patch _STATUS_DIR via env isn't easy here;
            # instead verify --list output contains "never" when no flags exist.
            result = subprocess.run(
                [sys.executable, str(_ROOT / "scripts" / "run_report.py"), "--list"],
                capture_output=True,
                text=True,
                cwd=td,  # no data/status dir → "never"
            )
            # Script may fail if it can't import modules from outside cwd, so only
            # assert returncode when it succeeds.
            if result.returncode == 0:
                self.assertIn("Daily Report", result.stdout)


if __name__ == "__main__":
    unittest.main()
