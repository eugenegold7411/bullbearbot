"""
tests/test_oca_unlock_and_bugs.py

Tests for three live-production bug fixes:
  BUG-NEW-1  OCA share-lock sell retry in order_executor.py
  BUG-NEW-2  options_state.load_structures() handles non-dict entries
  BUG-NEW-3  bot_stage1_5_qualitative.py symbol cap (30) and max_tokens (6000)
"""

from __future__ import annotations

import json
import os
import tempfile
import types
import unittest
from unittest.mock import MagicMock, call, patch


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_open_order(order_id: str, side, order_type, stop_price, qty: int):
    o = MagicMock()
    o.id = order_id
    o.side = side
    o.order_type = order_type
    o.stop_price = stop_price
    o.qty = str(qty)
    return o


def _oca_error(available: int, existing: int, held: int, symbol: str) -> Exception:
    msg = (
        f'{{"available":"{available}","code":40310000,'
        f'"existing_qty":"{existing}","held_for_orders":"{held}",'
        f'"message":"insufficient qty available",'
        f'"symbol":"{symbol}"}}'
    )
    return Exception(msg)


# ── BUG-NEW-1: OCA share-lock sell retry ─────────────────────────────────────

class TestOcaUnlockAvailableShares(unittest.TestCase):
    """GOOGL case: 45 of 126 shares available — retry with available qty."""

    def setUp(self):
        import order_executor as oe
        self._oe = oe

    def test_retries_with_available_qty_from_error(self):
        err = _oca_error(available=45, existing=126, held=81, symbol="GOOGL")
        action = {"symbol": "GOOGL", "qty": 126}
        positions = []

        call_count = [0]
        submitted_qty = [None]

        def fake_submit(a):
            call_count[0] += 1
            if call_count[0] == 1:
                raise err
            submitted_qty[0] = int(float(a["qty"]))
            return ("order-retry", 381.0, 45, "2026-04-30T17:35:29Z")

        with patch.object(self._oe, "_submit_sell", side_effect=fake_submit):
            oid, fp, fq, ft = self._oe._sell_with_oca_retry(action, positions)

        self.assertEqual(call_count[0], 2)
        self.assertEqual(submitted_qty[0], 45)  # retried with available, not 126
        self.assertEqual(oid, "order-retry")
        self.assertAlmostEqual(fp, 381.0)

    def test_passes_through_non_oca_errors(self):
        action = {"symbol": "GOOGL", "qty": 126}
        positions = []

        with patch.object(self._oe, "_submit_sell", side_effect=ValueError("some other error")):
            with self.assertRaises(ValueError):
                self._oe._sell_with_oca_retry(action, positions)

    def test_first_call_succeeds_no_retry(self):
        action = {"symbol": "XLE", "qty": 50}
        positions = []

        with patch.object(self._oe, "_submit_sell", return_value=("ok-id", 59.5, 50, None)):
            oid, fp, fq, ft = self._oe._sell_with_oca_retry(action, positions)

        self.assertEqual(oid, "ok-id")
        self.assertAlmostEqual(fp, 59.5)


class TestOcaCancelStopPath(unittest.TestCase):
    """MA case: all 91 shares locked — cancel stop then sell."""

    def setUp(self):
        import order_executor as oe
        self._oe = oe

    def _run_cancel_and_sell(
        self,
        symbol: str,
        sell_qty: int,
        stop_orders,
        positions: list,
        sell_side_effect=None,
        sell_return=None,
    ):
        from alpaca.trading.enums import OrderSide, QueryOrderStatus

        def fake_get_orders(req):
            return stop_orders

        def fake_cancel(order_id):
            pass

        alpaca = MagicMock()
        alpaca.get_orders.side_effect = fake_get_orders
        alpaca.cancel_order_by_id.side_effect = fake_cancel
        if sell_return:
            alpaca.submit_order.return_value = MagicMock(id="sell-id", filled_avg_price=None, filled_qty=None)

        cancelled = []

        with patch.object(self._oe, "_get_alpaca", return_value=alpaca):
            if sell_side_effect:
                with patch.object(self._oe, "_submit_sell", side_effect=sell_side_effect):
                    return self._oe._sell_cancel_stop_and_sell(symbol, sell_qty, positions)
            else:
                with patch.object(self._oe, "_submit_sell", return_value=sell_return):
                    with patch.object(self._oe, "_replace_stop") as mock_replace:
                        result = self._oe._sell_cancel_stop_and_sell(symbol, sell_qty, positions)
                        return result, mock_replace

    def test_cancels_stop_before_sell(self):
        from alpaca.trading.enums import OrderSide

        stop_order = _make_open_order(
            "f7e6a6c0", OrderSide.SELL, MagicMock(__str__=lambda s: "stop"), 495.71, 91
        )

        alpaca = MagicMock()
        alpaca.get_orders.return_value = [stop_order]
        cancelled_ids = []
        alpaca.cancel_order_by_id.side_effect = lambda oid: cancelled_ids.append(oid)

        with patch.object(self._oe, "_get_alpaca", return_value=alpaca):
            with patch.object(self._oe, "_submit_sell", return_value=("sell-id", 501.0, 91, None)):
                with patch.object(self._oe, "_replace_stop"):
                    self._oe._sell_cancel_stop_and_sell("MA", 91, [])

        self.assertIn("f7e6a6c0", cancelled_ids)

    def test_replaces_stop_for_remaining_shares(self):
        from alpaca.trading.enums import OrderSide

        stop_order = _make_open_order(
            "stop-id", OrderSide.SELL, MagicMock(__str__=lambda s: "stop"), 365.50, 126
        )
        # Position still has 126 shares before sell — after selling 45, 81 remain
        pos = MagicMock()
        pos.symbol = "GOOGL"
        pos.qty = "126"

        alpaca = MagicMock()
        alpaca.get_orders.return_value = [stop_order]
        alpaca.cancel_order_by_id.return_value = None

        replaced = []
        with patch.object(self._oe, "_get_alpaca", return_value=alpaca):
            with patch.object(self._oe, "_submit_sell", return_value=("sell-id", 383.0, 45, None)):
                with patch.object(self._oe, "_replace_stop", side_effect=lambda s, q, sp: replaced.append((s, q, sp))):
                    self._oe._sell_cancel_stop_and_sell("GOOGL", 45, [pos])

        # Should re-place stop for remaining 126 - 45 = 81 shares
        self.assertEqual(len(replaced), 1)
        sym, qty, sp = replaced[0]
        self.assertEqual(sym, "GOOGL")
        self.assertEqual(qty, 81)
        self.assertAlmostEqual(sp, 365.50)

    def test_no_stop_replace_when_full_position_sold(self):
        from alpaca.trading.enums import OrderSide

        stop_order = _make_open_order(
            "stop-id", OrderSide.SELL, MagicMock(__str__=lambda s: "stop"), 495.71, 91
        )
        pos = MagicMock()
        pos.symbol = "MA"
        pos.qty = "91"

        alpaca = MagicMock()
        alpaca.get_orders.return_value = [stop_order]
        alpaca.cancel_order_by_id.return_value = None

        replaced = []
        with patch.object(self._oe, "_get_alpaca", return_value=alpaca):
            with patch.object(self._oe, "_submit_sell", return_value=("sell-id", 501.0, 91, None)):
                with patch.object(self._oe, "_replace_stop", side_effect=lambda s, q, sp: replaced.append((s, q, sp))):
                    self._oe._sell_cancel_stop_and_sell("MA", 91, [pos])

        # remaining = 91 - 91 = 0 — no stop re-placement
        self.assertEqual(len(replaced), 0)


class TestSellWithOcaRetryZeroAvailable(unittest.TestCase):
    """When available=0, should take the cancel-stop path."""

    def setUp(self):
        import order_executor as oe
        self._oe = oe

    def test_zero_available_calls_cancel_stop_path(self):
        err = _oca_error(available=0, existing=91, held=91, symbol="MA")
        action = {"symbol": "MA", "qty": 91}
        positions = []

        with patch.object(self._oe, "_submit_sell", side_effect=err):
            with patch.object(
                self._oe,
                "_sell_cancel_stop_and_sell",
                return_value=("cancel-oid", 501.0, 91, None),
            ) as mock_cancel:
                oid, fp, fq, ft = self._oe._sell_with_oca_retry(action, positions)

        mock_cancel.assert_called_once_with("MA", 91, positions)
        self.assertEqual(oid, "cancel-oid")


# ── BUG-NEW-2: options_state handles non-dict entries ─────────────────────────

class TestOptionsStateNonDictEntry(unittest.TestCase):
    """structures.json had a MagicMock string entry that caused TypeError."""

    def _load_with_bad_entry(self, entries: list) -> list:
        import options_state as os_mod
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(entries, f)
            fname = f.name

        orig = os_mod._STRUCTURES_PATH
        try:
            import pathlib
            os_mod._STRUCTURES_PATH = pathlib.Path(fname)
            result = os_mod.load_structures()
        finally:
            os_mod._STRUCTURES_PATH = orig
            os.unlink(fname)
        return result

    def test_string_entry_skipped_without_typeerror(self):
        bad_entry = "<MagicMock name='mock.submit_structure().to_dict()' id='12345'>"
        # A minimal valid structure dict
        good_entry = {
            "structure_id": "test-id-001",
            "underlying": "AAPL",
            "strategy": "single_call",
            "lifecycle": "cancelled",
            "legs": [],
            "contracts": 1,
            "max_cost_usd": 500.0,
            "opened_at": "2026-04-29T00:00:00+00:00",
            "direction": "bullish",
            "tier": "core",
            "iv_rank": 40.0,
            "order_ids": [],
        }
        # Should not raise, should return only the good entry
        result = self._load_with_bad_entry([good_entry, bad_entry])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].underlying, "AAPL")

    def test_all_bad_entries_returns_empty_list(self):
        result = self._load_with_bad_entry(["string1", 42, None, True])
        self.assertEqual(result, [])

    def test_good_entries_all_loaded(self):
        entries = [
            {
                "structure_id": f"id-{i}",
                "underlying": "NVDA",
                "strategy": "single_call",
                "lifecycle": "cancelled",
                "legs": [],
                "contracts": 1,
                "max_cost_usd": 300.0,
                "opened_at": "2026-04-29T00:00:00+00:00",
                "direction": "bullish",
                "tier": "core",
                "iv_rank": 30.0,
                "order_ids": [],
            }
            for i in range(3)
        ]
        result = self._load_with_bad_entry(entries)
        self.assertEqual(len(result), 3)


# ── BUG-NEW-3: qualitative sweep symbol cap and max_tokens ───────────────────

class TestQualitativeSweepSymbolCap(unittest.TestCase):

    def setUp(self):
        import bot_stage1_5_qualitative as q
        self._q = q

    def _make_fake_client(self, create_side_effect=None, create_return=None):
        fake_resp = MagicMock()
        fake_resp.content = [MagicMock(text='{"regime_context": {}, "symbol_context": {}}')]
        fake_resp.usage = MagicMock(
            input_tokens=100, output_tokens=50,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        )
        fake_client = MagicMock()
        if create_side_effect:
            fake_client.messages.create.side_effect = create_side_effect
        else:
            fake_client.messages.create.return_value = create_return or fake_resp
        return fake_client, fake_resp

    def test_symbols_capped_at_30(self):
        """run_qualitative_sweep must trim to 30 symbols before calling Claude."""
        symbols_80 = [f"SYM{i:02d}" for i in range(80)]
        captured_symbols = []

        def fake_build(md, regime, syms):
            captured_symbols.extend(syms)
            return "prompt"

        fake_client, _ = self._make_fake_client()
        with patch.object(self._q, "_build_user_prompt", side_effect=fake_build):
            with patch("bot_clients._get_claude", return_value=fake_client):
                with patch("bot_stage1_5_qualitative._atomic_write"):
                    self._q.run_qualitative_sweep({}, {}, symbols_80)

        self.assertLessEqual(len(captured_symbols), 30)

    def test_max_tokens_is_6000(self):
        """The Claude call must use max_tokens=6000, not 8192."""
        symbols_5 = ["AAPL", "MSFT", "GOOGL", "NVDA", "TSLA"]
        create_kwargs = {}

        _, fake_resp = self._make_fake_client()

        def capture_create(**kwargs):
            create_kwargs.update(kwargs)
            return fake_resp

        fake_client, _ = self._make_fake_client(create_side_effect=capture_create)
        with patch("bot_clients._get_claude", return_value=fake_client):
            with patch("bot_stage1_5_qualitative._atomic_write"):
                self._q.run_qualitative_sweep({}, {}, symbols_5)

        self.assertEqual(create_kwargs.get("max_tokens"), 6000)

    def test_small_symbol_list_not_truncated(self):
        """A list of ≤30 symbols should be passed through unchanged."""
        symbols_10 = [f"SYM{i}" for i in range(10)]
        captured = []

        def fake_build(md, regime, syms):
            captured.extend(syms)
            return "prompt"

        fake_client, _ = self._make_fake_client()
        with patch.object(self._q, "_build_user_prompt", side_effect=fake_build):
            with patch("bot_clients._get_claude", return_value=fake_client):
                with patch("bot_stage1_5_qualitative._atomic_write"):
                    self._q.run_qualitative_sweep({}, {}, symbols_10)

        self.assertEqual(len(captured), 10)


if __name__ == "__main__":
    unittest.main()
