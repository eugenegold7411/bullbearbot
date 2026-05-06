"""
tests/test_trades_log_trade_record.py

Fix D — structured trades log for A1 (data/trades.jsonl) and A2 (data/account2/trades.jsonl).

trades_log_1    — _write_structured_trade writes JSONL line with flock
trades_log_2    — _validate_trades_log renames corrupt last line to .corrupt.{ts}
trade_record_1  — filled order writes rich record to data/trades.jsonl
trade_record_2  — file missing → creates directory + file without error
trade_record_3  — A2 FULLY_FILLED → writes entry record to data/account2/trades.jsonl
trade_record_4  — A2 close_structure → writes close event to data/account2/trades.jsonl
trade_record_5  — _write failure (bad path) → never raises from calling code
"""
from __future__ import annotations

import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Alpaca mock helpers (same pattern as other executor tests) ─────────────────

class _OrderSideEnum:
    BUY  = "buy"
    SELL = "sell"


class _TIFValue:
    def __init__(self, v: str):
        self.value = v
    def __eq__(self, other):
        return self.value == (other.value if isinstance(other, _TIFValue) else other)


class _TimeInForceEnum:
    DAY = _TIFValue("day")
    GTC = _TIFValue("gtc")


class _PositionIntentEnum:
    BUY_TO_OPEN   = "buy_to_open"
    SELL_TO_OPEN  = "sell_to_open"
    BUY_TO_CLOSE  = "buy_to_close"
    SELL_TO_CLOSE = "sell_to_close"


class _OrderClassEnum:
    MLEG   = "mleg"
    SIMPLE = "simple"


def _alpaca_mocks() -> dict:
    enums_mod = MagicMock()
    enums_mod.OrderClass     = _OrderClassEnum
    enums_mod.TimeInForce    = _TimeInForceEnum
    enums_mod.PositionIntent = _PositionIntentEnum
    enums_mod.OrderSide      = _OrderSideEnum

    requests_mod = MagicMock()
    requests_mod.LimitOrderRequest  = lambda **kw: MagicMock(**kw)
    requests_mod.MarketOrderRequest = lambda **kw: MagicMock(**kw)
    requests_mod.OptionLegRequest   = lambda **kw: MagicMock(**kw)

    return {
        "alpaca":                  MagicMock(),
        "alpaca.trading":          MagicMock(),
        "alpaca.trading.enums":    enums_mod,
        "alpaca.trading.requests": requests_mod,
    }


# ── Tests: _write_structured_trade and _validate_trades_log (A1) ──────────────

class TestWriteStructuredTrade(unittest.TestCase):

    def _import(self):
        with patch.dict(sys.modules, _alpaca_mocks()):
            import order_executor as oe
            importlib.reload(oe)
            return oe

    def test_trades_log_1_writes_jsonl_line(self):
        """_write_structured_trade writes a valid JSON line to the target file."""
        oe = self._import()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trades.jsonl"
            oe._write_structured_trade(path, {"event_type": "filled", "symbol": "AAPL", "qty": 10})
            lines = path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        self.assertEqual(rec["event_type"], "filled")
        self.assertEqual(rec["symbol"], "AAPL")
        self.assertIn("written_at", rec)

    def test_trades_log_2_validate_renames_corrupt_last_line(self):
        """_validate_trades_log renames file when last line is corrupt JSON."""
        oe = self._import()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trades.jsonl"
            path.write_text('{"event_type": "filled"}\nNOT_JSON_AT_ALL\n', encoding="utf-8")

            oe._validate_trades_log(path)

            # Original file should be gone (renamed)
            self.assertFalse(path.exists(), "Original corrupt file should be renamed")
            # A .corrupt.* file should exist
            corrupt_files = list(Path(tmp).glob("trades.corrupt.*"))
            self.assertTrue(len(corrupt_files) == 1, f"Expected one .corrupt file; got: {list(Path(tmp).iterdir())}")

    def test_trade_record_2_missing_file_creates_dir_and_file(self):
        """_write_structured_trade creates parent dirs and file if missing."""
        oe = self._import()
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "deep" / "nested" / "trades.jsonl"
            self.assertFalse(nested.exists())
            oe._write_structured_trade(nested, {"symbol": "MSFT", "event_type": "filled"})
            self.assertTrue(nested.exists(), "File must be created if missing")

    def test_trade_record_5_write_failure_never_raises(self):
        """_write_structured_trade never raises even if path is unwritable."""
        oe = self._import()
        bad_path = Path("/dev/null/cannot_write/trades.jsonl")
        try:
            oe._write_structured_trade(bad_path, {"symbol": "FAIL"})
        except Exception as exc:
            self.fail(f"_write_structured_trade raised unexpectedly: {exc}")


class TestA1FillWritesStructuredRecord(unittest.TestCase):
    """trade_record_1 — fill confirmation writes rich record to data/trades.jsonl."""

    def test_trade_record_1_fill_writes_to_structured_log(self):
        with patch.dict(sys.modules, _alpaca_mocks()):
            import order_executor as oe
            importlib.reload(oe)

        written: list[dict] = []

        def _fake_write(path, record):
            written.append(dict(record))

        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "trades.jsonl"
            oe._STRUCTURED_TRADES_LOG = fake_path

            # Seed _pending_fill_checks with a rich entry
            oe._pending_fill_checks["order-fill-001"] = {
                "symbol":         "NVDA",
                "action":         "buy",
                "qty":            5,
                "alert_deferred": False,
                "registered_at":  __import__("time").time(),
                "decision_id":    "dec-abc-123",
                "order_type":     "limit",
                "stop_price":     480.0,
                "take_profit":    560.0,
                "catalyst":       "earnings_beat",
                "confidence":     "high",
                "session":        "market",
                "tier":           "core",
                "regime":         "risk_on",
                "regime_score":   0.72,
                "thesis_summary": "NVDA AI capex supercycle",
            }

            mock_order = MagicMock()
            mock_order.status          = "filled"
            mock_order.filled_avg_price = "512.50"
            mock_order.filled_qty       = "5"
            mock_order.filled_at        = "2026-05-06T14:30:00Z"

            with patch.object(oe, "_get_alpaca") as mock_get_alpaca, \
                 patch.object(oe, "_write_structured_trade", side_effect=_fake_write):
                mock_get_alpaca.return_value.get_order_by_id.return_value = mock_order
                oe._check_pending_fills()

        self.assertEqual(len(written), 1, f"Expected 1 record, got {len(written)}: {written}")
        rec = written[0]
        self.assertEqual(rec["symbol"], "NVDA")
        self.assertEqual(rec["event_type"], "filled")
        self.assertEqual(rec["decision_id"], "dec-abc-123")
        self.assertEqual(rec["stop_price"], 480.0)
        self.assertEqual(rec["take_profit_price"], 560.0)
        self.assertEqual(rec["account"], "a1")
        self.assertEqual(rec["regime"], "risk_on")
        self.assertEqual(rec["catalyst_tags"], "earnings_beat")


# ── Tests: A2 trade logging ───────────────────────────────────────────────────

class _StructureLifecycleEnum:
    FULLY_FILLED = MagicMock(value="fully_filled")
    CANCELLED    = MagicMock(value="cancelled")
    CLOSED       = MagicMock(value="closed")


class _OptionStrategyEnum:
    SINGLE_CALL = MagicMock(value="single_call")


def _make_a2_alpaca_mocks() -> dict:
    enums = MagicMock()
    enums.StructureLifecycle = _StructureLifecycleEnum
    enums.OptionStrategy     = _OptionStrategyEnum

    return {
        "options_executor": MagicMock(),
        "options_state":    MagicMock(),
        "options_data":     MagicMock(),
    }


class TestA2TradeLogging(unittest.TestCase):

    def test_trade_record_3_fully_filled_writes_entry_record(self):
        """A2 structure → FULLY_FILLED triggers entry record in data/account2/trades.jsonl."""
        from schemas import OptionStrategy

        written: list[dict] = []

        # Struct starts in "submitted" lifecycle so _sync_submitted_lifecycles acts on it
        struct = MagicMock()
        struct.underlying   = "NVDA"
        struct.structure_id = "str-abcd-1234"
        struct.strategy     = OptionStrategy.SINGLE_CALL
        struct.order_ids    = ["alpaca-order-xyz"]
        struct.contracts    = 1
        struct.expiration   = "2026-06-20"
        # lifecycle.value must equal "submitted" for the guard to pass
        _submitted_lc       = MagicMock()
        _submitted_lc.value = "submitted"
        struct.lifecycle    = _submitted_lc

        leg              = MagicMock()
        leg.occ_symbol   = "NVDA260620C00900000"
        leg.side         = "buy"
        leg.filled_price = 5.00
        struct.legs      = [leg]
        struct.add_audit = lambda _: None

        mock_state   = MagicMock()
        mock_trading = MagicMock()

        raw_order              = MagicMock()
        raw_order.status       = "filled"
        mock_trading.get_order_by_id.return_value = raw_order

        sys_patch = {"options_state": mock_state}
        with patch.dict(sys.modules, sys_patch):
            import bot_options_stage4_execution as s4
            importlib.reload(s4)
            with patch.object(s4, "_write_a2_trade", side_effect=lambda r: written.append(dict(r))):
                s4._sync_submitted_lifecycles([struct], mock_trading)

        entry_records = [r for r in written if r.get("event_type") == "entry_filled"]
        self.assertTrue(
            len(entry_records) >= 1,
            f"Expected entry_filled record; written: {written}",
        )
        rec = entry_records[0]
        self.assertEqual(rec["symbol"], "NVDA")
        self.assertEqual(rec["structure_id"], "str-abcd-1234")
        self.assertIn("legs", rec)

    def test_trade_record_4_close_submits_close_event(self):
        """A2 close_structure call triggers close_submitted event in trades log."""
        written: list[dict] = []

        from schemas import OptionStrategy, StructureLifecycle

        mock_executor = MagicMock()
        mock_state    = MagicMock()

        struct                     = MagicMock()
        struct.underlying          = "TSLA"
        struct.structure_id        = "str-close-9999"
        struct.strategy            = OptionStrategy.SINGLE_CALL
        struct.order_ids           = []
        struct.pnl_unrealized      = -150.0
        struct.close_attempt_count = 0
        struct.close_reason_code   = "stop_hit"
        struct.closed_at           = "2026-05-06T15:00:00-04:00"
        _ff_lc                     = MagicMock()
        _ff_lc.value               = "fully_filled"
        struct.lifecycle           = _ff_lc

        leg              = MagicMock()
        leg.occ_symbol   = "TSLA260620C00250000"
        leg.side         = "buy"
        leg.filled_price = 3.00
        leg.order_id     = None
        struct.legs      = [leg]

        audit_log        = []
        struct.audit_log = audit_log
        struct.add_audit = lambda m: audit_log.append(m)
        struct.opened_at = "2026-05-06T13:00:00-04:00"

        closed_struct                = MagicMock()
        closed_struct.structure_id   = "str-close-9999"
        closed_struct.underlying     = "TSLA"
        closed_struct.strategy       = OptionStrategy.SINGLE_CALL
        closed_struct.lifecycle      = StructureLifecycle.CLOSED
        closed_struct.pnl_unrealized = -150.0
        closed_struct.closed_at      = "2026-05-06T15:00:00-04:00"

        mock_executor.should_close_structure.return_value = (True, "stop_hit")
        mock_executor.should_roll_structure.return_value  = (False, None)
        mock_executor.close_structure.return_value        = closed_struct

        mock_state.get_open_structures.return_value = [struct]
        alpaca_client = MagicMock()
        # Return the leg's OCC symbol so the position-gone guard sees it as still live
        mock_pos        = MagicMock()
        mock_pos.symbol = "TSLA260620C00250000"
        alpaca_client.get_all_positions.return_value = [mock_pos]

        sys_patch = {
            "options_executor": mock_executor,
            "options_state":    mock_state,
            "options_data":     MagicMock(),
        }
        with patch.dict(sys.modules, sys_patch):
            import bot_options_stage4_execution as s4
            importlib.reload(s4)
            with patch.object(s4, "_write_a2_trade",           side_effect=lambda r: written.append(dict(r))), \
                 patch.object(s4, "_fetch_close_check_prices",  return_value={}), \
                 patch.object(s4, "_update_fill_prices",        return_value=None), \
                 patch.object(s4, "_sync_submitted_lifecycles", return_value=None), \
                 patch.object(s4, "_load_strategy_config",      return_value={}):
                s4.close_check_loop(alpaca_client)

        close_records = [r for r in written if r.get("event_type") == "close_submitted"]
        self.assertTrue(
            len(close_records) >= 1,
            f"Expected close_submitted record; written: {written}",
        )
        rec = close_records[0]
        self.assertEqual(rec["symbol"], "TSLA")
        self.assertIn("close_reason", rec)


if __name__ == "__main__":
    unittest.main()
