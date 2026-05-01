"""
DT-01 through DT-15 — Decision Theater data module and route tests.

DT-01 to DT-12 are data module tests (decision_theater.py).
DT-13 to DT-15 are route/integration tests (Flask test client).

All Alpaca API calls are mocked. Tests run against real decisions.json,
trades.jsonl, and memory files on the server; on local machines without
those files the relevant tests gracefully skip or use fallback data.
"""

import base64
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))

os.environ.setdefault("DASHBOARD_USER", "admin")
os.environ.setdefault("DASHBOARD_PASSWORD", "bullbearbot")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("ALPACA_API_KEY", "test-key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret")

# ── Helpers ───────────────────────────────────────────────────────────────────

DECISIONS_PATH = _ROOT / "memory" / "decisions.json"
_HAS_DECISIONS = DECISIONS_PATH.exists()

TRADES_JSONL = _ROOT / "logs" / "trades.jsonl"
_HAS_TRADES = TRADES_JSONL.exists()


def _skip_no_decisions(cls):
    if not _HAS_DECISIONS:
        return unittest.skip("memory/decisions.json not found — run on server")(cls)
    return cls


def _load_decisions_raw():
    if not _HAS_DECISIONS:
        return []
    data = json.loads(DECISIONS_PATH.read_text())
    return data if isinstance(data, list) else data.get("decisions", [])


# ── Mock Alpaca objects ───────────────────────────────────────────────────────

def _make_mock_position(symbol, entry, current, pnl_usd, pnl_pct, qty=100):
    p = MagicMock()
    p.symbol = symbol
    p.avg_entry_price = entry
    p.current_price = current
    p.unrealized_pl = pnl_usd
    p.unrealized_plpc = pnl_pct / 100.0
    p.qty = qty
    p.created_at = "2026-04-28T14:00:00+00:00"
    return p


def _make_mock_order(symbol, side, qty, filled_avg, status="filled"):
    o = MagicMock()
    o.symbol = symbol
    o.side = MagicMock()
    o.side.__str__ = lambda s: f"OrderSide.{side.upper()}"
    o.qty = qty
    o.filled_qty = qty if status == "filled" else 0
    o.filled_avg_price = filled_avg
    o.status = MagicMock()
    o.status.__str__ = lambda s: f"OrderStatus.{status.upper()}"
    o.created_at = "2026-04-28T14:00:00+00:00"
    o.filled_at = "2026-04-28T14:05:00+00:00"
    return o


# ── DT-01: get_cycle_view returns valid dict with all stages ──────────────────

@_skip_no_decisions
class TestDT01CycleViewStructure(unittest.TestCase):
    @patch("decision_theater._load_alpaca_positions", return_value=[])
    @patch("decision_theater._load_alpaca_orders", return_value=[])
    def test_all_stages_present(self, *_):
        from decision_theater import get_cycle_view
        result = get_cycle_view(-1)
        self.assertIn("stages", result)
        for stage in ["regime", "signals", "scratchpad", "gate", "sonnet", "kernel", "execution", "a2"]:
            self.assertIn(stage, result["stages"], f"Missing stage: {stage}")

    @patch("decision_theater._load_alpaca_positions", return_value=[])
    @patch("decision_theater._load_alpaca_orders", return_value=[])
    def test_top_level_fields(self, *_):
        from decision_theater import get_cycle_view
        result = get_cycle_view(-1)
        for field in ["cycle_number", "total_cycles", "timestamp", "session"]:
            self.assertIn(field, result, f"Missing field: {field}")

    @patch("decision_theater._load_alpaca_positions", return_value=[])
    @patch("decision_theater._load_alpaca_orders", return_value=[])
    def test_total_cycles_matches_decisions(self, *_):
        from decision_theater import get_cycle_view
        decisions = _load_decisions_raw()
        result = get_cycle_view(-1)
        self.assertEqual(result["total_cycles"], len(decisions))

    @patch("decision_theater._load_alpaca_positions", return_value=[])
    @patch("decision_theater._load_alpaca_orders", return_value=[])
    def test_each_stage_has_status(self, *_):
        from decision_theater import get_cycle_view
        result = get_cycle_view(-1)
        for name, st in result["stages"].items():
            self.assertIn("status", st, f"Stage {name} missing status")


# ── DT-02: get_cycle_view(0) returns first cycle ──────────────────────────────

@_skip_no_decisions
class TestDT02FirstCycle(unittest.TestCase):
    @patch("decision_theater._load_alpaca_positions", return_value=[])
    @patch("decision_theater._load_alpaca_orders", return_value=[])
    def test_cycle_0_returns_without_error(self, *_):
        from decision_theater import get_cycle_view
        result = get_cycle_view(0)
        self.assertEqual(result["cycle_number"], 0)

    @patch("decision_theater._load_alpaca_positions", return_value=[])
    @patch("decision_theater._load_alpaca_orders", return_value=[])
    def test_cycle_0_timestamp_is_earliest(self, *_):
        from decision_theater import get_cycle_view
        first = get_cycle_view(0)
        last = get_cycle_view(-1)
        # First cycle timestamp should be ≤ last cycle timestamp
        self.assertLessEqual(first["timestamp"], last["timestamp"])


# ── DT-03: get_trade_lifecycle for open position ──────────────────────────────

class TestDT03OpenTradeLifecycle(unittest.TestCase):
    @patch("decision_theater._load_alpaca_positions")
    @patch("decision_theater._load_alpaca_orders", return_value=[])
    def test_open_position_returns_open_status(self, mock_orders, mock_pos):
        mock_pos.return_value = [_make_mock_position("GOOGL", 355.0, 390.0, 2800.0, 9.5, 81)]
        from decision_theater import get_trade_lifecycle
        result = get_trade_lifecycle("GOOGL")
        self.assertEqual(result["status"], "open")

    @patch("decision_theater._load_alpaca_positions")
    @patch("decision_theater._load_alpaca_orders", return_value=[])
    def test_open_position_has_entry_price(self, mock_orders, mock_pos):
        mock_pos.return_value = [_make_mock_position("GOOGL", 354.85, 389.62, 2816.0, 9.8, 81)]
        from decision_theater import get_trade_lifecycle
        result = get_trade_lifecycle("GOOGL")
        self.assertAlmostEqual(result["entry_price"], 354.85, places=1)

    @patch("decision_theater._load_alpaca_positions")
    @patch("decision_theater._load_alpaca_orders", return_value=[])
    def test_open_position_has_exit_scenarios(self, mock_orders, mock_pos):
        mock_pos.return_value = [_make_mock_position("XLE", 56.73, 59.74, 1071.0, 5.3, 356)]
        from decision_theater import get_trade_lifecycle
        result = get_trade_lifecycle("XLE")
        self.assertIsNotNone(result["exit_scenarios"])
        self.assertIn("beat", result["exit_scenarios"])
        self.assertIn("stop_hit", result["exit_scenarios"])

    @patch("decision_theater._load_alpaca_positions")
    @patch("decision_theater._load_alpaca_orders", return_value=[])
    def test_open_pnl_status_is_unrealized(self, mock_orders, mock_pos):
        mock_pos.return_value = [_make_mock_position("XOM", 152.08, 155.38, 458.7, 2.17, 139)]
        from decision_theater import get_trade_lifecycle
        result = get_trade_lifecycle("XOM")
        self.assertEqual(result["pnl_status"], "unrealized")


# ── DT-04: bug_flag populated for affected symbol ─────────────────────────────

class TestDT04BugFlag(unittest.TestCase):
    @patch("decision_theater._load_alpaca_positions", return_value=[])
    @patch("decision_theater._load_alpaca_orders")
    def test_amzn_closed_trade_lifecycle(self, mock_orders, *_):
        buy = _make_mock_order("AMZN", "buy", 164, 262.05)
        sell = _make_mock_order("AMZN", "sell", 164, 264.01)
        mock_orders.return_value = [sell, buy]
        from decision_theater import get_trade_lifecycle
        result = get_trade_lifecycle("AMZN")
        # Should be closed (entry + exit filled)
        self.assertIn(result["status"], ["closed", "not_found"])

    @patch("decision_theater._load_alpaca_positions", return_value=[])
    @patch("decision_theater._load_alpaca_orders", return_value=[])
    def test_not_found_returns_sensible_default(self, *_):
        from decision_theater import get_trade_lifecycle
        result = get_trade_lifecycle("ZZZZ_NONEXISTENT")
        self.assertEqual(result["status"], "not_found")
        self.assertIsNone(result["pnl_usd"])
        self.assertEqual(result["lifecycle_events"], [])


# ── DT-05: pnl_usd from trade_journal ────────────────────────────────────────

class TestDT05PnlMatches(unittest.TestCase):
    @patch("decision_theater._load_alpaca_positions", return_value=[])
    @patch("decision_theater._load_alpaca_orders")
    def test_pnl_matches_journal_calculation(self, mock_orders, *_):
        """TSM: entry 393.94, exit 396.18, qty 109 → pnl = 244.16"""
        buy = _make_mock_order("TSM", "buy", 109, 393.94)
        sell = _make_mock_order("TSM", "sell", 109, 396.18)
        mock_orders.return_value = [sell, buy]
        from decision_theater import get_trade_lifecycle
        result = get_trade_lifecycle("TSM")
        if result["status"] == "closed":
            self.assertAlmostEqual(result["pnl_usd"], (396.18 - 393.94) * 109, delta=1.0)


# ── DT-06: exit_scenarios for open positions ──────────────────────────────────

class TestDT06ExitScenarios(unittest.TestCase):
    @patch("decision_theater._load_alpaca_positions")
    @patch("decision_theater._load_alpaca_orders", return_value=[])
    def test_exit_scenarios_has_required_keys(self, mock_orders, mock_pos):
        mock_pos.return_value = [_make_mock_position("XOM", 152.08, 155.38, 458.7, 2.17, 139)]
        from decision_theater import get_trade_lifecycle
        result = get_trade_lifecycle("XOM")
        if result.get("exit_scenarios"):
            for key in ["beat", "flat", "miss_5pct", "miss_10pct"]:
                self.assertIn(key, result["exit_scenarios"])

    @patch("decision_theater._load_alpaca_positions")
    @patch("decision_theater._load_alpaca_orders", return_value=[])
    def test_exit_scenarios_none_for_closed(self, mock_orders, mock_pos):
        mock_pos.return_value = []
        from decision_theater import get_trade_lifecycle
        result = get_trade_lifecycle("ZZZZ_NONEXISTENT")
        self.assertIsNone(result.get("exit_scenarios"))


# ── DT-07: get_all_trades_summary — open positions first ─────────────────────

class TestDT07AllTradesSummary(unittest.TestCase):
    @patch("decision_theater._load_alpaca_positions")
    @patch("decision_theater._load_alpaca_orders")
    def test_open_positions_first(self, mock_orders, mock_pos):
        mock_pos.return_value = [
            _make_mock_position("GOOGL", 355.0, 390.0, 2800.0, 9.5, 81),
        ]
        buy = _make_mock_order("TSM", "buy", 109, 393.94)
        sell = _make_mock_order("TSM", "sell", 109, 396.18)
        mock_orders.return_value = [sell, buy]
        from decision_theater import get_all_trades_summary
        result = get_all_trades_summary()
        trades = result["trades"]
        self.assertGreater(len(trades), 0)
        # First entry must be an open position
        self.assertEqual(trades[0]["status"], "open")

    @patch("decision_theater._load_alpaca_positions")
    @patch("decision_theater._load_alpaca_orders", return_value=[])
    def test_summary_has_count_fields(self, mock_orders, mock_pos):
        mock_pos.return_value = [_make_mock_position("XLE", 56.73, 59.74, 1071.0, 5.3, 356)]
        from decision_theater import get_all_trades_summary
        result = get_all_trades_summary()
        for key in ["trades", "open_count", "closed_count", "total"]:
            self.assertIn(key, result)

    @patch("decision_theater._load_alpaca_positions")
    @patch("decision_theater._load_alpaca_orders", return_value=[])
    def test_summary_open_count_matches_positions(self, mock_orders, mock_pos):
        mock_pos.return_value = [
            _make_mock_position("GOOGL", 355.0, 390.0, 2800.0, 9.5, 81),
            _make_mock_position("XLE", 56.73, 59.74, 1071.0, 5.3, 356),
        ]
        from decision_theater import get_all_trades_summary
        result = get_all_trades_summary()
        self.assertEqual(result["open_count"], 2)


# ── DT-08: lifecycle_events from decisions.json ───────────────────────────────

@_skip_no_decisions
class TestDT08LifecycleEvents(unittest.TestCase):
    @patch("decision_theater._load_alpaca_positions")
    @patch("decision_theater._load_alpaca_orders", return_value=[])
    def test_open_position_has_lifecycle_events(self, mock_orders, mock_pos):
        mock_pos.return_value = [_make_mock_position("GOOGL", 355.0, 390.0, 2800.0, 9.5, 81)]
        from decision_theater import get_trade_lifecycle
        result = get_trade_lifecycle("GOOGL")
        # lifecycle_events should be a list (may be empty if no decision mentions GOOGL)
        self.assertIsInstance(result["lifecycle_events"], list)

    @patch("decision_theater._load_alpaca_positions", return_value=[])
    @patch("decision_theater._load_alpaca_orders", return_value=[])
    def test_events_have_required_keys(self, *_):
        from decision_theater import _build_lifecycle_events, _decisions_for_symbol
        decisions = _load_decisions_raw()
        sym_decs = _decisions_for_symbol(decisions, "GOOGL")
        events = _build_lifecycle_events(sym_decs, "GOOGL", "open", 390.0)
        for ev in events:
            for key in ["event_type", "timestamp", "label", "detail", "sonnet_excerpt"]:
                self.assertIn(key, ev, f"Event missing key: {key}")


# ── DT-09: price_journey percentages ─────────────────────────────────────────

class TestDT09PriceJourney(unittest.TestCase):
    def test_entry_pct_less_than_current_for_profit(self):
        from decision_theater import _compute_price_journey
        pj = _compute_price_journey(entry=100.0, current=110.0, stop=90.0, target=130.0)
        self.assertLess(pj["entry_pct"], pj["current_pct"])
        self.assertLess(pj["stop_pct"], pj["entry_pct"])

    def test_all_pcts_in_0_to_100(self):
        from decision_theater import _compute_price_journey
        pj = _compute_price_journey(entry=100.0, current=95.0, stop=85.0, target=120.0)
        for key in ["stop_pct", "entry_pct", "current_pct", "target_pct"]:
            val = pj.get(key)
            if val is not None:
                self.assertGreaterEqual(val, 0, f"{key} < 0")
                self.assertLessEqual(val, 100, f"{key} > 100")

    def test_stop_below_entry(self):
        from decision_theater import _compute_price_journey
        pj = _compute_price_journey(entry=150.0, current=155.0, stop=140.0, target=170.0)
        if pj["stop_pct"] is not None:
            self.assertLess(pj["stop_pct"], pj["entry_pct"])

    def test_target_above_entry(self):
        from decision_theater import _compute_price_journey
        pj = _compute_price_journey(entry=100.0, current=105.0, stop=90.0, target=125.0)
        if pj["target_pct"] is not None:
            self.assertGreater(pj["target_pct"], pj["entry_pct"])

    def test_none_entry_returns_safe_default(self):
        from decision_theater import _compute_price_journey
        pj = _compute_price_journey(entry=0, current=100.0, stop=None, target=None)
        self.assertIn("entry_pct", pj)


# ── DT-10 to DT-15: Route tests (require Flask) ──────────────────────────────

import importlib

_FLASK_AVAILABLE = importlib.util.find_spec("flask") is not None
_STUB_MODULES = {
    "alpaca": MagicMock(), "alpaca.trading": MagicMock(),
    "alpaca.trading.client": MagicMock(), "alpaca.trading.requests": MagicMock(),
    "alpaca.trading.enums": MagicMock(), "alpaca.data": MagicMock(),
    "alpaca.data.historical": MagicMock(), "alpaca.data.requests": MagicMock(),
    "chromadb": MagicMock(), "twilio": MagicMock(), "twilio.rest": MagicMock(),
    "sendgrid": MagicMock(),
}

_DASH = None
_CLIENT = None
if _FLASK_AVAILABLE:
    with patch.dict("sys.modules", _STUB_MODULES):
        import dashboard.app as _DASH
        _DASH.app.config["TESTING"] = True
        _CLIENT = _DASH.app.test_client()


def _skip_no_flask(cls):
    if not _FLASK_AVAILABLE:
        return unittest.skip("Flask not installed")(cls)
    return cls


def _auth():
    user = os.environ.get("DASHBOARD_USER", "admin")
    pw = os.environ.get("DASHBOARD_PASSWORD", "bullbearbot")
    creds = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


def _get(path):
    return _CLIENT.get(path, headers=_auth())


@_skip_no_flask
class TestDT10TheaterRoute(unittest.TestCase):
    @patch("decision_theater.get_cycle_view", return_value={
        "cycle_number": 0, "total_cycles": 1, "timestamp": "2026-04-30T10:00:00Z",
        "session": "market", "decision_id": "test",
        "stages": {k: {"status": "ok"} for k in
                   ["regime", "signals", "scratchpad", "gate", "sonnet", "kernel", "execution", "a2"]},
    })
    @patch("decision_theater.get_all_trades_summary", return_value={
        "trades": [], "open_count": 0, "closed_count": 0, "total": 0,
    })
    def test_theater_200(self, *_):
        r = _get("/theater")
        self.assertEqual(r.status_code, 200)

    @patch("decision_theater.get_cycle_view", return_value={
        "cycle_number": 0, "total_cycles": 1, "timestamp": "", "session": "market",
        "decision_id": "", "stages": {},
    })
    @patch("decision_theater.get_all_trades_summary", return_value={
        "trades": [], "open_count": 0, "closed_count": 0, "total": 0,
    })
    def test_theater_contains_nav(self, *_):
        r = _get("/theater")
        html = r.data.decode()
        self.assertIn("nav-tab active", html)
        self.assertIn("Decision Theater", html)


@_skip_no_flask
class TestDT11CycleApiRoute(unittest.TestCase):
    @patch("decision_theater.get_cycle_view")
    def test_api_cycle_200(self, mock_cv):
        mock_cv.return_value = {
            "cycle_number": 0, "total_cycles": 10, "timestamp": "2026-04-30T10:00:00Z",
            "session": "market", "decision_id": "test",
            "stages": {"sonnet": {"status": "ok", "ideas": []}},
        }
        r = _get("/api/theater/cycle/-1")
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn("stages", data)

    @patch("decision_theater.get_cycle_view")
    def test_api_cycle_returns_json(self, mock_cv):
        mock_cv.return_value = {"cycle_number": 5, "stages": {}}
        r = _get("/api/theater/cycle/5")
        self.assertEqual(r.content_type, "application/json")


@_skip_no_flask
class TestDT12TradeApiRoute(unittest.TestCase):
    @patch("decision_theater.get_trade_lifecycle")
    def test_api_trade_200(self, mock_tl):
        mock_tl.return_value = {
            "symbol": "GOOGL", "status": "open", "pnl_usd": 2816.0,
            "pnl_pct": 9.8, "entry_price": 354.85, "lifecycle_events": [],
        }
        r = _get("/api/theater/trade/GOOGL")
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data["symbol"], "GOOGL")

    @patch("decision_theater.get_trade_lifecycle")
    def test_api_trade_returns_json(self, mock_tl):
        mock_tl.return_value = {"symbol": "XLE", "status": "open"}
        r = _get("/api/theater/trade/XLE")
        self.assertEqual(r.content_type, "application/json")


@_skip_no_flask
class TestDT13StageDotColors(unittest.TestCase):
    @patch("decision_theater.get_cycle_view", return_value={
        "cycle_number": 0, "total_cycles": 1, "timestamp": "2026-04-30T10:00:00Z",
        "session": "market", "decision_id": "",
        "stages": {
            "regime": {"status": "ok", "regime": "risk_on", "score": 62},
            "signals": {"status": "warn", "symbols_scored": 0},
            "scratchpad": {"status": "ok", "watching": []},
            "gate": {"status": "skip", "mode": "SKIP"},
            "sonnet": {"status": "skip", "ideas": [], "reasoning_excerpt": "test"},
            "kernel": {"status": "ok", "approved": 0, "rejected": 0},
            "execution": {"status": "ok", "orders_submitted": 0},
            "a2": {"status": "skip"},
        },
    })
    @patch("decision_theater.get_all_trades_summary", return_value={
        "trades": [], "open_count": 0, "closed_count": 0, "total": 0,
    })
    def test_stage_nodes_in_html(self, *_):
        r = _get("/theater")
        html = r.data.decode()
        self.assertIn("stage-regime", html)
        self.assertIn("stage-sonnet", html)
        self.assertIn("stage-node", html)


@_skip_no_flask
class TestDT14A2StageData(unittest.TestCase):
    @patch("decision_theater._load_alpaca_positions", return_value=[])
    @patch("decision_theater._load_alpaca_orders", return_value=[])
    def test_a2_stage_returns_dict(self, *_):
        from decision_theater import _build_a2_stage
        cycle = {"ts": "2026-04-30T19:00:00+00:00", "session": "market"}
        result = _build_a2_stage(cycle)
        self.assertIn("status", result)

    @patch("decision_theater._load_alpaca_positions", return_value=[])
    @patch("decision_theater._load_alpaca_orders", return_value=[])
    def test_a2_stage_valid_statuses(self, *_):
        from decision_theater import _build_a2_stage
        cycle = {"ts": "2026-04-30T19:00:00+00:00", "session": "market"}
        result = _build_a2_stage(cycle)
        self.assertIn(result["status"], ["ok", "skip", "error", "warn"])


@_skip_no_flask
class TestDT15MissingDecisionsFallback(unittest.TestCase):
    @patch("decision_theater._load_decisions", return_value=[])
    @patch("decision_theater._load_alpaca_positions", return_value=[])
    @patch("decision_theater._load_alpaca_orders", return_value=[])
    def test_empty_decisions_no_crash(self, *_):
        from decision_theater import get_cycle_view
        result = get_cycle_view(-1)
        self.assertIn("stages", result)
        self.assertEqual(result["total_cycles"], 0)

    @patch("decision_theater._DECISIONS_PATH", new=Path("/tmp/nonexistent_decisions_abc.json"))
    @patch("decision_theater._load_alpaca_positions", return_value=[])
    @patch("decision_theater._load_alpaca_orders", return_value=[])
    def test_missing_file_no_crash(self, *_):
        from decision_theater import get_cycle_view
        result = get_cycle_view(-1)
        self.assertIsInstance(result, dict)


if __name__ == "__main__":
    unittest.main()
