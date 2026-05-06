"""
tests/test_realized_pnl_greeks.py

Tests for:
  - FIX A: realized_pnl computed by close_structure() (options_executor)
  - FIX B: closed_unknown outcome in performance_tracker
  - FIX C: delta/theta/vega round-trip on OptionsStructure
"""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

_STUBS = {
    "dotenv":                      None,
    "anthropic":                   None,
    "alpaca":                      None,
    "alpaca.trading":              None,
    "alpaca.trading.client":       None,
    "alpaca.trading.requests":     None,
    "alpaca.trading.enums":        None,
    "alpaca.data":                 None,
    "alpaca.data.historical":      None,
    "alpaca.data.historical.news": None,
    "alpaca.data.requests":        None,
    "alpaca.data.timeframe":       None,
    "alpaca.data.enums":           None,
    "pandas":                      None,
    "yfinance":                    None,
}
for _name in _STUBS:
    if _name not in sys.modules:
        sys.modules[_name] = mock.MagicMock()


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_structure(legs_spec, pnl_unrealized=None, contracts=1):
    """Build a minimal OptionsStructure for close tests."""
    from schemas import (
        OptionsLeg,
        OptionsStructure,
        OptionStrategy,
        StructureLifecycle,
        Tier,
    )
    legs = []
    for occ, side, filled, bid, ask in legs_spec:
        legs.append(OptionsLeg(
            occ_symbol=occ,
            underlying="AAPL",
            side=side,
            qty=contracts,
            option_type="call",
            strike=185.0,
            expiration="2026-06-20",
            filled_price=filled,
            bid=bid,
            ask=ask,
        ))
    return OptionsStructure(
        structure_id="test-sid-001",
        underlying="AAPL",
        strategy=OptionStrategy.CALL_DEBIT_SPREAD,
        lifecycle=StructureLifecycle.FULLY_FILLED,
        legs=legs,
        contracts=contracts,
        max_cost_usd=500.0,
        opened_at=datetime.now(timezone.utc).isoformat(),
        catalyst="test",
        tier=Tier.CORE,
        pnl_unrealized=pnl_unrealized,
    )


# ── FIX A Tests ───────────────────────────────────────────────────────────────

class TestComputeRealizedPnlEstimate(unittest.TestCase):

    def _call(self, structure, current_prices):
        from options_executor import _compute_realized_pnl_estimate
        return _compute_realized_pnl_estimate(structure, current_prices)

    def test_1_debit_spread_profit_from_current_prices(self):
        """Debit spread: long leg gained, short leg lost → positive realized_pnl."""
        # long call: entry 2.00, current 3.50 → +150 per contract × 100
        # short call: entry 1.00, current 0.50 → +(1.00-0.50)*1*100 = +50
        # net = +150 + 50 = $200
        struct = _make_structure([
            ("AAPL260620C00185000", "buy",  2.00, 3.40, 3.60),
            ("AAPL260620C00190000", "sell", 1.00, 0.45, 0.55),
        ], contracts=1)
        current_prices = {
            "AAPL260620C00185000": 3.50,
            "AAPL260620C00190000": 0.50,
        }
        result = self._call(struct, current_prices)
        self.assertAlmostEqual(result, 200.0, places=2)

    def test_2_fallback_to_pnl_unrealized_when_no_current_prices(self):
        """No current_prices → falls back to pnl_unrealized snapshot."""
        struct = _make_structure([
            ("AAPL260620C00185000", "buy", 2.00, None, None),
        ], pnl_unrealized=-120.0)
        result = self._call(struct, None)
        self.assertEqual(result, -120.0)

    def test_3_returns_none_when_no_prices_and_no_pnl_unrealized(self):
        """Neither current_prices nor pnl_unrealized → None."""
        struct = _make_structure([
            ("AAPL260620C00185000", "buy", 2.00, None, None),
        ], pnl_unrealized=None)
        result = self._call(struct, None)
        self.assertIsNone(result)

    def test_1b_current_prices_missing_one_leg_returns_none(self):
        """If current_prices lacks any leg, computation returns None (not partial)."""
        struct = _make_structure([
            ("AAPL260620C00185000", "buy",  2.00, None, None),
            ("AAPL260620C00190000", "sell", 1.00, None, None),
        ], contracts=1)
        current_prices = {"AAPL260620C00185000": 3.50}  # missing short leg
        result = self._call(struct, current_prices)
        self.assertIsNone(result)


class TestCloseStructureSetsRealizedPnl(unittest.TestCase):

    def _run_close(self, struct, current_prices):
        """Run close_structure with a mock trading_client that accepts MLEG orders."""
        from options_executor import close_structure
        mock_client = mock.MagicMock()
        mock_order = mock.MagicMock()
        mock_order.id = "order-close-001"
        mock_client.submit_order.return_value = mock_order
        closed = close_structure(
            struct, mock_client, reason="stop_loss_hit",
            method="limit", current_prices=current_prices,
        )
        return closed

    def test_close_spread_stamps_realized_pnl(self):
        """close_structure with current_prices sets realized_pnl on the structure."""
        struct = _make_structure([
            ("AAPL260620C00185000", "buy",  2.00, 3.40, 3.60),
            ("AAPL260620C00190000", "sell", 1.00, 0.45, 0.55),
        ], contracts=1)
        current_prices = {
            "AAPL260620C00185000": 3.50,
            "AAPL260620C00190000": 0.50,
        }
        closed = self._run_close(struct, current_prices)
        self.assertIsNotNone(closed.realized_pnl)
        self.assertAlmostEqual(closed.realized_pnl, 200.0, places=2)


# ── FIX B Tests ───────────────────────────────────────────────────────────────

class TestLogA2StructureOutcome(unittest.TestCase):

    def _make_closed_struct(self, realized_pnl, closed_at=None):
        from schemas import (
            OptionsLeg,
            OptionsStructure,
            OptionStrategy,
            StructureLifecycle,
            Tier,
        )
        leg = OptionsLeg(
            occ_symbol="AAPL260620C00185000",
            underlying="AAPL",
            side="buy",
            qty=1,
            option_type="call",
            strike=185.0,
            expiration="2026-06-20",
            filled_price=2.00,
        )
        return OptionsStructure(
            structure_id="test-outcome-001",
            underlying="AAPL",
            strategy=OptionStrategy.SINGLE_CALL,
            lifecycle=StructureLifecycle.CLOSED,
            legs=[leg],
            contracts=1,
            max_cost_usd=200.0,
            opened_at=datetime.now(timezone.utc).isoformat(),
            catalyst="test",
            tier=Tier.CORE,
            realized_pnl=realized_pnl,
            closed_at=closed_at or datetime.now(timezone.utc).isoformat(),
            order_ids=["ord-001"],
        )

    def _last_outcome_record(self):
        """Read last record appended to the outcomes file during test."""
        import json

        from performance_tracker import _A2_OUTCOMES_PATH
        if not _A2_OUTCOMES_PATH.exists():
            return None
        lines = _A2_OUTCOMES_PATH.read_text().strip().split("\n")
        return json.loads(lines[-1]) if lines and lines[-1] else None

    def test_4_closed_with_no_realized_pnl_gives_closed_unknown(self):
        """closed_at set + realized_pnl=None → outcome='closed_unknown'."""
        from performance_tracker import log_a2_structure_outcome
        struct = self._make_closed_struct(realized_pnl=None)
        with mock.patch("performance_tracker._append_jsonl") as mock_append:
            log_a2_structure_outcome(struct)
            mock_append.assert_called_once()
            record = mock_append.call_args[0][1][0]
        self.assertEqual(record["outcome"], "closed_unknown")
        self.assertIsNone(record["pnl_usd"])

    def test_5_no_closed_at_and_no_realized_pnl_gives_cancelled(self):
        """closed_at=None + realized_pnl=None → outcome='cancelled'."""
        from performance_tracker import log_a2_structure_outcome
        from schemas import StructureLifecycle
        struct = self._make_closed_struct(realized_pnl=None)
        struct.closed_at = None
        # Force lifecycle to CLOSED so is_cancelled=False branch executes
        struct.lifecycle = StructureLifecycle.CLOSED
        with mock.patch("performance_tracker._append_jsonl") as mock_append:
            log_a2_structure_outcome(struct)
            mock_append.assert_called_once()
            record = mock_append.call_args[0][1][0]
        self.assertEqual(record["outcome"], "cancelled")

    def test_6_performance_summary_fill_rate_nonzero_with_closed_unknown(self):
        """closed_unknown records count as submitted, making fill_rate > 0."""
        from performance_tracker import _compute_performance_summary
        closed_unknown_rec = {
            "timestamp_opened": datetime.now(timezone.utc).isoformat(),
            "timestamp_closed": datetime.now(timezone.utc).isoformat(),
            "structure_id": "su-001",
            "symbol": "AAPL",
            "strategy": "single_call",
            "rule_fired": "stop_loss_hit",
            "debate_confidence": 0.75,
            "entry_price": 2.0,
            "exit_price": None,
            "max_gain": 500.0,
            "max_loss": 200.0,
            "dte_at_entry": 30,
            "dte_at_exit": 15,
            "exit_reason": "stop_hit",
            "pnl_usd": None,
            "pnl_pct_of_max_gain": None,
            "outcome": "closed_unknown",
        }
        cancelled_rec = dict(closed_unknown_rec, outcome="cancelled", structure_id="cancel-001")

        with mock.patch("performance_tracker._load_jsonl") as mock_load:
            def side_effect(path):
                from performance_tracker import _A2_OUTCOMES_PATH
                if path == _A2_OUTCOMES_PATH:
                    return [closed_unknown_rec, cancelled_rec]
                return []
            mock_load.side_effect = side_effect
            summary = _compute_performance_summary(days_back=30)

        a2_summary = summary.get("a2_structures", {})
        fill_rate = a2_summary.get("fill_rate_pct", 0)
        # 1 closed_unknown out of 2 total (1 cancelled filtered out) → 100% submitted, not 0%
        self.assertGreater(fill_rate, 0, "fill_rate_pct should be >0 when closed_unknown records exist")


# ── FIX C Tests ───────────────────────────────────────────────────────────────

class TestOptionsStructureGreeksRoundTrip(unittest.TestCase):

    def test_7_delta_theta_vega_survive_to_dict_from_dict(self):
        """OptionsStructure.to_dict() / from_dict() round-trips delta, theta, vega."""
        from schemas import (
            OptionsLeg,
            OptionsStructure,
            OptionStrategy,
            StructureLifecycle,
            Tier,
        )
        leg = OptionsLeg(
            occ_symbol="AAPL260620C00185000",
            underlying="AAPL",
            side="buy",
            qty=2,
            option_type="call",
            strike=185.0,
            expiration="2026-06-20",
            filled_price=2.50,
            delta=0.42,
        )
        struct = OptionsStructure(
            structure_id="test-greeks-001",
            underlying="AAPL",
            strategy=OptionStrategy.SINGLE_CALL,
            lifecycle=StructureLifecycle.FULLY_FILLED,
            legs=[leg],
            contracts=2,
            max_cost_usd=500.0,
            opened_at=datetime.now(timezone.utc).isoformat(),
            catalyst="test",
            tier=Tier.CORE,
            delta=0.42,
            theta=-0.03,
            vega=0.15,
        )

        d = struct.to_dict()
        self.assertAlmostEqual(d["delta"], 0.42)
        self.assertAlmostEqual(d["theta"], -0.03)
        self.assertAlmostEqual(d["vega"], 0.15)

        restored = OptionsStructure.from_dict(d)
        self.assertAlmostEqual(restored.delta, 0.42)
        self.assertAlmostEqual(restored.theta, -0.03)
        self.assertAlmostEqual(restored.vega, 0.15)

    def test_7b_greeks_default_to_none_when_absent(self):
        """from_dict() with no greek keys → delta/theta/vega are None."""
        from schemas import OptionsStructure
        d = {
            "structure_id": "no-greeks-001",
            "underlying": "AAPL",
            "strategy": "single_call",
            "lifecycle": "fully_filled",
            "legs": [],
            "contracts": 1,
            "max_cost_usd": 200.0,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "catalyst": "test",
            "tier": "core",
        }
        struct = OptionsStructure.from_dict(d)
        self.assertIsNone(struct.delta)
        self.assertIsNone(struct.theta)
        self.assertIsNone(struct.vega)


if __name__ == "__main__":
    unittest.main()
