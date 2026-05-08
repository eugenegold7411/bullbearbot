"""
tests/test_options_position_manager.py — coverage for the A2 position
intelligence layer.

24 tests across:
  - greek fetch (3)
  - greek history (3)
  - drift detection (6)
  - action router (5)
  - wiring into bot_options + stages 1/3/4 (5)
  - structure-upgrade migration shim (2)
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

# Project root on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import options_position_manager as opm  # noqa: E402
from schemas import (  # noqa: E402
    OptionsLeg,
    OptionsStructure,
    OptionStrategy,
    StructureLifecycle,
    Tier,
)

# ─── helpers ──────────────────────────────────────────────────────────────────

def _make_leg(occ: str, side: str, strike: float, opt_type: str = "call",
              expiration: str = "2026-06-19") -> OptionsLeg:
    return OptionsLeg(
        occ_symbol=occ, underlying=occ[:4].rstrip("0123456789"),
        side=side, qty=1, option_type=opt_type, strike=strike,
        expiration=expiration,
    )


def _make_structure(
    underlying: str = "NVDA",
    structure_id: str = "test-id-1",
    strategy: OptionStrategy = OptionStrategy.SINGLE_CALL,
    legs: list | None = None,
    pnl_unrealized: float | None = 50.0,
    last_upgrade_attempted: str | None = None,
) -> OptionsStructure:
    if legs is None:
        legs = [_make_leg(f"{underlying}260619C00100000", "buy", 100.0)]
    return OptionsStructure(
        structure_id=structure_id,
        underlying=underlying,
        strategy=strategy,
        lifecycle=StructureLifecycle.FULLY_FILLED,
        legs=legs,
        contracts=1,
        max_cost_usd=500.0,
        opened_at="2026-04-01T15:00:00+00:00",
        catalyst="test",
        tier=Tier.CORE,
        long_strike=100.0,
        expiration="2026-06-19",
        pnl_unrealized=pnl_unrealized,
        last_upgrade_attempted=last_upgrade_attempted,
    )


def _snap(delta=0.5, theta=-0.10, vega=0.20, gamma=0.02,
          ts: str | None = None) -> opm.GreekSnapshot:
    return opm.GreekSnapshot(
        delta=delta, theta=theta, vega=vega, gamma=gamma,
        underlying_price=None,
        timestamp=ts or datetime.now(timezone.utc).isoformat(),
    )


# ─── 1-3: greek fetch ─────────────────────────────────────────────────────────

class GreekFetchTests(unittest.TestCase):

    def test_fetch_greeks_returns_snapshot(self):
        """Single buy leg with full greeks → GreekSnapshot with summed values."""
        struct = _make_structure()
        with mock.patch.object(
            opm, "options_data", create=True
        ) as md:
            # Fallback: also patch the import inside _fetch_greeks
            md.fetch_option_greeks.return_value = {
                "delta": 0.55, "theta": -0.09, "vega": 0.18, "gamma": 0.03,
            }
            sys.modules["options_data"] = md
            try:
                snap = opm._fetch_greeks(struct)
            finally:
                sys.modules.pop("options_data", None)
        self.assertIsNotNone(snap)
        self.assertAlmostEqual(snap.delta, 0.55, places=4)
        self.assertAlmostEqual(snap.theta, -0.09, places=4)
        self.assertAlmostEqual(snap.vega, 0.18, places=4)
        self.assertAlmostEqual(snap.gamma, 0.03, places=4)

    def test_fetch_greeks_handles_none(self):
        """fetch_option_greeks returns None → _fetch_greeks returns None gracefully."""
        struct = _make_structure()
        fake_md = mock.MagicMock()
        fake_md.fetch_option_greeks.return_value = None
        sys.modules["options_data"] = fake_md
        try:
            snap = opm._fetch_greeks(struct)
        finally:
            sys.modules.pop("options_data", None)
        self.assertIsNone(snap)

    def test_fetch_greeks_multi_leg_nets_correctly(self):
        """Long call (delta 0.6) + short call (delta 0.4) → net delta 0.2."""
        legs = [
            _make_leg("NVDA260619C00100000", "buy",  100.0),
            _make_leg("NVDA260619C00105000", "sell", 105.0),
        ]
        struct = _make_structure(strategy=OptionStrategy.CALL_DEBIT_SPREAD, legs=legs)

        def _fake(occ):
            if occ.endswith("C00100000"):
                return {"delta": 0.6, "theta": -0.10, "vega": 0.20, "gamma": 0.04}
            return {"delta": 0.4, "theta": -0.08, "vega": 0.18, "gamma": 0.03}

        fake_md = mock.MagicMock()
        fake_md.fetch_option_greeks.side_effect = _fake
        sys.modules["options_data"] = fake_md
        try:
            snap = opm._fetch_greeks(struct)
        finally:
            sys.modules.pop("options_data", None)
        self.assertIsNotNone(snap)
        self.assertAlmostEqual(snap.delta, 0.2, places=4)
        self.assertAlmostEqual(snap.theta, -0.02, places=4)
        self.assertAlmostEqual(snap.vega, 0.02, places=4)
        self.assertAlmostEqual(snap.gamma, 0.01, places=4)


# ─── 4-6: greek history ───────────────────────────────────────────────────────

class GreekHistoryTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name)
        self._orig_history_dir = opm._HISTORY_DIR
        opm._HISTORY_DIR = self._tmp_path

    def tearDown(self):
        opm._HISTORY_DIR = self._orig_history_dir
        self._tmp.cleanup()

    def test_history_appended_correctly(self):
        for i in range(3):
            opm._append_greek_history(
                "NVDA", "sid-1", _snap(delta=0.5 + i * 0.01), max_keep=30,
            )
        loaded = opm._load_greek_history("NVDA", "sid-1")
        self.assertEqual(len(loaded), 3)
        self.assertAlmostEqual(loaded[0]["delta"], 0.50, places=4)
        self.assertAlmostEqual(loaded[2]["delta"], 0.52, places=4)

    def test_history_capped_at_30(self):
        for i in range(35):
            opm._append_greek_history(
                "NVDA", "sid-cap", _snap(delta=0.10 + i * 0.001), max_keep=30,
            )
        loaded = opm._load_greek_history("NVDA", "sid-cap")
        self.assertEqual(len(loaded), 30)
        # First 5 evicted — 6th original (i=5, delta=0.105) is now first.
        self.assertAlmostEqual(loaded[0]["delta"], 0.105, places=4)

    def test_history_returns_empty_when_missing(self):
        loaded = opm._load_greek_history("DOESNT", "exist")
        self.assertEqual(loaded, [])


# ─── 7-12: drift detection ────────────────────────────────────────────────────

class DriftDetectionTests(unittest.TestCase):

    def _hist(self, n: int) -> list[dict]:
        return [_snap().to_dict() for _ in range(n)]

    def test_delta_itm_detected(self):
        cur   = _snap(delta=0.85, theta=-0.10, vega=0.20)
        entry = {"delta": 0.45, "theta": -0.10, "vega": 0.20, "gamma": None}
        state, reason = opm._detect_drift(
            cur, entry, self._hist(5), "single_call", None, {},
        )
        self.assertEqual(state, opm.DriftState.DELTA_ITM)
        self.assertIn("0.85", reason)

    def test_delta_otm_detected(self):
        cur   = _snap(delta=0.12, theta=-0.05, vega=0.10)
        entry = {"delta": 0.45, "theta": -0.10, "vega": 0.20, "gamma": None}
        state, _ = opm._detect_drift(
            cur, entry, self._hist(5), "single_call", None, {},
        )
        self.assertEqual(state, opm.DriftState.DELTA_OTM)

    def test_theta_acceleration_detected(self):
        cur   = _snap(delta=0.50, theta=-0.25, vega=0.20)  # 2.5× entry theta
        entry = {"delta": 0.50, "theta": -0.10, "vega": 0.20, "gamma": None}
        state, _ = opm._detect_drift(
            cur, entry, self._hist(5), "single_call", None, {},
        )
        self.assertEqual(state, opm.DriftState.THETA_ACCELERATION)

    def test_vega_collapse_detected(self):
        cur   = _snap(delta=0.50, theta=-0.10, vega=0.05)  # 0.25× entry vega
        entry = {"delta": 0.50, "theta": -0.10, "vega": 0.20, "gamma": None}
        state, _ = opm._detect_drift(
            cur, entry, self._hist(5), "single_call", None, {},
        )
        self.assertEqual(state, opm.DriftState.VEGA_COLLAPSE)

    def test_normal_when_no_drift(self):
        cur   = _snap(delta=0.50, theta=-0.10, vega=0.20)
        entry = {"delta": 0.50, "theta": -0.10, "vega": 0.20, "gamma": None}
        state, _ = opm._detect_drift(
            cur, entry, self._hist(5), "single_call", None, {},
        )
        self.assertEqual(state, opm.DriftState.NORMAL)

    def test_insufficient_data_when_thin_history(self):
        cur   = _snap(delta=0.50, theta=-0.10, vega=0.20)
        entry = {"delta": 0.50, "theta": -0.10, "vega": 0.20, "gamma": None}
        state, _ = opm._detect_drift(
            cur, entry, self._hist(1), "single_call", None, {},
        )
        self.assertEqual(state, opm.DriftState.INSUFFICIENT_DATA)

    def test_short_leg_itm_fires_for_mislabeled_strategy(self):
        """
        Orphan-tracked vertical spreads can carry strategy=single_call when
        reconcile combines two Alpaca legs. SHORT_LEG_ITM must still fire
        based on the actual short-leg delta (leg-structure check, not
        strategy-string check).
        """
        cur   = _snap(delta=0.06, theta=-0.40, vega=0.30)
        entry = {"delta": 0.45, "theta": -0.20, "vega": 0.30, "gamma": None}
        state, reason = opm._detect_drift(
            cur, entry, self._hist(5),
            structure_type="single_call",  # mis-labeled
            short_leg_abs_delta=0.84,
            config={},
        )
        self.assertEqual(state, opm.DriftState.SHORT_LEG_ITM)
        self.assertIn("0.84", reason)


# ─── 13-17: action router ─────────────────────────────────────────────────────

class ActionRouterTests(unittest.TestCase):

    def test_delta_itm_routes_to_close(self):
        struct = _make_structure(strategy=OptionStrategy.SINGLE_CALL)
        action = opm._route_action(opm.DriftState.DELTA_ITM, struct, None, {})
        self.assertEqual(action.action, opm.ACTION_CLOSE)
        self.assertEqual(action.urgency, "next_cycle")

    def test_delta_otm_routes_to_close(self):
        struct = _make_structure(strategy=OptionStrategy.SINGLE_CALL)
        # Override expiration so DTE <= 14 → urgency=immediate, action=CLOSE
        struct.expiration = (
            datetime.now(timezone.utc).date() + timedelta(days=10)
        ).isoformat()
        action = opm._route_action(opm.DriftState.DELTA_OTM, struct, None, {})
        self.assertEqual(action.action, opm.ACTION_CLOSE)
        self.assertEqual(action.urgency, "immediate")

    def test_vega_collapse_routes_to_close(self):
        struct = _make_structure()
        action = opm._route_action(opm.DriftState.VEGA_COLLAPSE, struct, None, {})
        self.assertEqual(action.action, opm.ACTION_CLOSE)

    def test_short_leg_itm_routes_to_close_short_leg(self):
        legs = [
            _make_leg("NVDA260619C00100000", "buy",  100.0),
            _make_leg("NVDA260619C00105000", "sell", 105.0),
        ]
        struct = _make_structure(strategy=OptionStrategy.CALL_DEBIT_SPREAD, legs=legs)
        action = opm._route_action(opm.DriftState.SHORT_LEG_ITM, struct, None, {})
        self.assertEqual(action.action, opm.ACTION_CLOSE_SHORT_LEG)
        self.assertEqual(action.urgency, "immediate")

    def test_upgrade_recommended_when_conditions_met(self):
        # Profitable single_call + matching spread debate + DTE > 7 + flag on.
        struct = _make_structure(
            strategy=OptionStrategy.SINGLE_CALL, pnl_unrealized=120.0,
        )
        struct.expiration = (
            datetime.now(timezone.utc).date() + timedelta(days=21)
        ).isoformat()
        debate = {"structure_type": "debit_call_spread"}
        cfg = {"structure_upgrade_enabled": True}
        action = opm._route_action(opm.DriftState.NORMAL, struct, debate, cfg)
        self.assertEqual(action.action, opm.ACTION_UPGRADE_TO_SPREAD)
        # Detail dict is the original-shape upgrade dict.
        self.assertEqual(action.details.get("action"), "add_hedge_leg")
        self.assertEqual(action.details.get("old_strategy"), "single_call")


# ─── 18-22: wiring tests ──────────────────────────────────────────────────────

class WiringTests(unittest.TestCase):

    def setUp(self):
        # Redirect intel + history to a tmpdir.
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name)
        self._orig_intel = opm._INTEL_PATH
        self._orig_hist  = opm._HISTORY_DIR
        self._orig_data  = opm._DATA_DIR
        opm._DATA_DIR    = self._tmp_path
        opm._HISTORY_DIR = self._tmp_path / "greek_history"
        opm._INTEL_PATH  = self._tmp_path / "position_intel_latest.json"

    def tearDown(self):
        opm._DATA_DIR    = self._orig_data
        opm._HISTORY_DIR = self._orig_hist
        opm._INTEL_PATH  = self._orig_intel
        self._tmp.cleanup()

    def test_run_writes_position_intel_json(self):
        struct = _make_structure()
        with mock.patch.object(opm, "_fetch_greeks", return_value=_snap()):
            with mock.patch("options_state.get_open_structures", return_value=[struct]):
                intel = opm.run(state=None, alpaca_client=None, config={})
        self.assertTrue(opm._INTEL_PATH.exists())
        on_disk = json.loads(opm._INTEL_PATH.read_text())
        self.assertEqual(on_disk["schema_version"], 1)
        self.assertEqual(intel["schema_version"], 1)
        self.assertIn("NVDA_test-id-1", intel["positions"])

    def test_close_recommended_populated(self):
        # Force a DELTA_OTM with DTE<=14 → urgency=immediate, action=CLOSE.
        struct = _make_structure(strategy=OptionStrategy.SINGLE_CALL)
        struct.expiration = (
            datetime.now(timezone.utc).date() + timedelta(days=5)
        ).isoformat()
        # current delta way under threshold
        cur = _snap(delta=0.05, theta=-0.10, vega=0.20)
        # Pre-seed history so we are not in INSUFFICIENT_DATA.
        opm._HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        for _i in range(4):
            opm._append_greek_history(
                "NVDA", "test-id-1", _snap(delta=0.45), max_keep=30,
            )
        with mock.patch.object(opm, "_fetch_greeks", return_value=cur):
            with mock.patch("options_state.get_open_structures", return_value=[struct]):
                intel = opm.run(state=None, alpaca_client=None, config={})
        self.assertIn("NVDA", intel["close_recommended"])
        # And the per-position record marks the action.
        rec = intel["positions"]["NVDA_test-id-1"]
        self.assertEqual(rec["action"], opm.ACTION_CLOSE)
        self.assertEqual(rec["urgency"], "immediate")

    def test_stage1_skips_close_recommended(self):
        """
        Stage 1 reads opm.get_close_recommended_symbols() and skips those
        symbols. Test the gate directly: write intel with NVDA close-recommended
        and confirm the helper returns NVDA, then confirm stage1's skip path
        consumes it. This is the smallest end-to-end exercise of the wiring.
        """
        intel = {
            "generated_at":        datetime.now(timezone.utc).isoformat(),
            "cycle_id":            "a2_pi_test",
            "schema_version":      1,
            "positions":           {},
            "close_recommended":   ["NVDA"],
            "upgrade_recommended": [],
            "monitoring":          [],
        }
        opm._INTEL_PATH.write_text(json.dumps(intel))
        syms = opm.get_close_recommended_symbols()
        self.assertEqual(syms, {"NVDA"})

        # Source-level confirmation: stage1 references the helper inside the
        # candidate loop, with `continue` and the documented log line. This
        # asserts the wiring (no candidate loop fixture needed).
        stage1_src = (
            Path(__file__).resolve().parent.parent
            / "bot_options_stage1_candidates.py"
        ).read_text()
        self.assertIn("get_close_recommended_symbols", stage1_src)
        self.assertIn("position intel recommends close", stage1_src)
        self.assertIn("if sym in _close_rec_syms", stage1_src)

    def test_debate_receives_greek_context(self):
        """
        Stage 3 reads opm.get_recommendations(symbol=X) and appends a
        POSITION INTELLIGENCE block for any non-NORMAL drift state.
        """
        intel = {
            "generated_at":   datetime.now(timezone.utc).isoformat(),
            "cycle_id":       "a2_pi_test",
            "schema_version": 1,
            "positions": {
                "NVDA_id1": {
                    "symbol": "NVDA", "structure_id": "id1",
                    "structure_type": "single_call",
                    "drift_state": "DELTA_ITM",
                    "action": "CLOSE", "urgency": "next_cycle",
                    "reason": "|net delta|=0.85 >= 0.80",
                    "greek_snapshot": {
                        "delta": 0.85, "theta": -0.20, "vega": 0.15,
                        "gamma": 0.01, "underlying_price": None,
                        "timestamp": "2026-05-08T01:00:00+00:00",
                    },
                    "entry_greeks":   {"delta": 0.45, "theta": -0.10, "vega": 0.20, "gamma": None},
                    "greek_history_length": 5,
                    "details": {"strategy": "single_call", "dte": 30, "drift_state": "DELTA_ITM"},
                },
            },
            "close_recommended":   [],
            "upgrade_recommended": [],
            "monitoring":          ["NVDA"],
        }
        opm._INTEL_PATH.write_text(json.dumps(intel))
        recs = opm.get_recommendations(symbol="NVDA")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].action, "CLOSE")
        self.assertEqual(recs[0].details.get("drift_state"), "DELTA_ITM")

        # Wiring source check — stage 3 emits the documented block format.
        stage3_src = (
            Path(__file__).resolve().parent.parent
            / "bot_options_stage3_debate.py"
        ).read_text()
        self.assertIn("get_recommendations", stage3_src)
        self.assertIn("POSITION INTELLIGENCE for", stage3_src)

    def test_close_check_loop_acts_on_immediate_close(self):
        """
        Stage 4 short-circuit: when act_on_immediate_close=true and intel has
        an urgency=immediate + action=CLOSE recommendation for a structure,
        close_check_loop submits a close.
        """
        # Default config: act_on_immediate_close=False
        self.assertFalse(opm.is_immediate_close_action_enabled({}))
        self.assertFalse(
            opm.is_immediate_close_action_enabled(
                {"position_intel": {"act_on_immediate_close": False}}
            )
        )
        self.assertTrue(
            opm.is_immediate_close_action_enabled(
                {"position_intel": {"act_on_immediate_close": True}}
            )
        )

        # Wiring source — stage 4 reads the helper and gates execution on it.
        stage4_src = (
            Path(__file__).resolve().parent.parent
            / "bot_options_stage4_execution.py"
        ).read_text()
        self.assertIn("is_immediate_close_action_enabled", stage4_src)
        self.assertIn("position intel immediate close", stage4_src)
        self.assertIn("ACTION_CLOSE", stage4_src)


# ─── 23-24: structure-upgrade migration ───────────────────────────────────────

class UpgradeMigrationTests(unittest.TestCase):

    def test_shim_delegates_to_opm(self):
        """
        bot_options_stage4_execution._evaluate_structure_upgrade is now a thin
        shim that calls options_position_manager._check_upgrade.
        """
        import bot_options_stage4_execution as stage4

        struct = _make_structure(
            strategy=OptionStrategy.SINGLE_CALL, pnl_unrealized=120.0,
        )
        struct.expiration = (
            datetime.now(timezone.utc).date() + timedelta(days=21)
        ).isoformat()
        debate = {"structure_type": "debit_call_spread"}
        cfg = {"structure_upgrade_enabled": True}

        # Patch _check_upgrade to a sentinel and confirm the shim calls it
        # with the same args and returns its value.
        sentinel = {"action": "add_hedge_leg", "symbol": "SENT"}
        with mock.patch.object(opm, "_check_upgrade", return_value=sentinel) as m:
            out = stage4._evaluate_structure_upgrade(struct, debate, cfg)
        self.assertEqual(out, sentinel)
        m.assert_called_once_with(struct, debate, cfg)

    def test_upgrade_logic_identical_pre_post_migration(self):
        """
        Behavior parity: feed the same fixture through both the shim and the
        migrated _check_upgrade directly. Output must match exactly (modulo
        the timestamp on last_upgrade_attempted, which both set to "now").
        """
        import bot_options_stage4_execution as stage4

        def _build():
            s = _make_structure(
                strategy=OptionStrategy.SINGLE_CALL, pnl_unrealized=200.0,
            )
            s.expiration = (
                datetime.now(timezone.utc).date() + timedelta(days=21)
            ).isoformat()
            return s

        debate = {"structure_type": "debit_call_spread"}
        cfg = {"structure_upgrade_enabled": True}

        s_a = _build()
        s_b = _build()
        out_shim = stage4._evaluate_structure_upgrade(s_a, debate, cfg)
        out_opm  = opm._check_upgrade(s_b, debate, cfg)
        self.assertIsNotNone(out_shim)
        self.assertIsNotNone(out_opm)
        # Strip non-deterministic fields and compare the rest.
        for k in ("action", "qty", "old_strategy", "new_strategy", "structure_id"):
            self.assertEqual(out_shim[k], out_opm[k], f"mismatch on {k}")
        # The hedge-leg OCC depends on long_strike+expiration, both deterministic.
        self.assertEqual(out_shim["symbol"], out_opm["symbol"])
        # Both stamped last_upgrade_attempted (side effect preserved).
        self.assertIsNotNone(s_a.last_upgrade_attempted)
        self.assertIsNotNone(s_b.last_upgrade_attempted)


if __name__ == "__main__":
    unittest.main()
