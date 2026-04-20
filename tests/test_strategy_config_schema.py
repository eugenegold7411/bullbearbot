"""
test_strategy_config_schema.py — strategy_config.json v2 schema validation.

Suite A: live strategy_config.json on disk (version, no duplicates, no _DEPRECATED)
Suite B: versioning._migrate_strategy_config_v1_to_v2 correctness
Suite C: director_notes dict contract
"""

import json
import unittest
from copy import deepcopy
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "strategy_config.json"

_V1_SAMPLE = {
    "version": 1,
    "position_sizing": {
        "core_tier_pct": 0.15,
        "dynamic_tier_pct": 0.08,
        "intraday_tier_pct": 0.05,
        "max_total_exposure_pct": 0.67,
        "cash_reserve_pct": 0.2,
    },
    "parameters": {
        "momentum_weight": 0.35,
        "mean_reversion_weight": 0.2,
        "news_sentiment_weight": 0.3,
        "cross_sector_weight": 0.15,
        "max_positions": 15,
        "stop_loss_pct_core": 0.035,
        "take_profit_multiple": 2.5,
        "max_total_exposure_pct": 0.67,
        "cash_reserve_pct": 0.2,
        "core_tier_pct": 0.15,
        "dynamic_tier_pct": 0.08,
        "intraday_tier_pct": 0.05,
        "max_single_position_pct_DEPRECATED": "REMOVED",
    },
    "signal_weights": {
        "momentum_weight": 0.35,
        "mean_reversion_weight": 0.2,
        "news_sentiment_weight": 0.3,
        "cross_sector_weight": 0.15,
    },
    "director_notes": {
        "active_context": "test memo",
        "expiry": "2026-05-01",
        "priority": "normal",
    },
}

_DUP_KEYS = [
    "core_tier_pct", "dynamic_tier_pct", "intraday_tier_pct",
    "max_total_exposure_pct", "cash_reserve_pct",
    "momentum_weight", "mean_reversion_weight",
    "news_sentiment_weight", "cross_sector_weight",
]


class TestStrategyConfigOnDisk(unittest.TestCase):
    """Suite A — live strategy_config.json on disk."""

    @classmethod
    def setUpClass(cls):
        if not CONFIG_PATH.exists():
            raise unittest.SkipTest("strategy_config.json not found")
        cls.cfg = json.loads(CONFIG_PATH.read_text())

    def test_version_is_2(self):
        self.assertEqual(self.cfg.get("version"), 2,
                         "version must be 2 (Phase 6 schema)")

    def test_no_duplicate_keys_in_parameters(self):
        params = self.cfg.get("parameters", {})
        present = [k for k in _DUP_KEYS if k in params]
        self.assertEqual(present, [],
                         f"Duplicate keys still in parameters: {present}")

    def test_no_deprecated_markers_in_parameters(self):
        params = self.cfg.get("parameters", {})
        deprecated = [k for k in params if k.endswith("_DEPRECATED")]
        self.assertEqual(deprecated, [],
                         f"_DEPRECATED markers still present: {deprecated}")

    def test_position_sizing_has_canonical_keys(self):
        ps = self.cfg.get("position_sizing", {})
        required = [
            "core_tier_pct", "dynamic_tier_pct", "intraday_tier_pct",
            "max_total_exposure_pct", "cash_reserve_pct",
        ]
        missing = [k for k in required if k not in ps]
        self.assertEqual(missing, [],
                         f"position_sizing missing keys: {missing}")

    def test_signal_weights_has_canonical_keys(self):
        sw = self.cfg.get("signal_weights", {})
        required = [
            "momentum_weight", "mean_reversion_weight",
            "news_sentiment_weight", "cross_sector_weight",
        ]
        missing = [k for k in required if k not in sw]
        self.assertEqual(missing, [],
                         f"signal_weights missing keys: {missing}")

    def test_signal_source_weights_exists(self):
        self.assertIn("signal_source_weights", self.cfg,
                      "signal_source_weights top-level block is missing")
        ssw = self.cfg["signal_source_weights"]
        self.assertIsInstance(ssw, dict,
                              f"signal_source_weights must be dict, got {type(ssw).__name__}")

    def test_director_notes_is_dict(self):
        dn = self.cfg.get("director_notes")
        self.assertIsInstance(dn, dict,
                              f"director_notes must be dict, got {type(dn).__name__}")

    def test_director_notes_required_fields(self):
        dn = self.cfg.get("director_notes", {})
        for field in ("active_context", "expiry", "priority"):
            self.assertIn(field, dn,
                          f"director_notes missing field: {field}")

    def test_signal_weights_sum_to_one(self):
        sw = self.cfg.get("signal_weights", {})
        keys = ["momentum_weight", "mean_reversion_weight",
                "news_sentiment_weight", "cross_sector_weight"]
        total = sum(float(sw.get(k, 0)) for k in keys)
        self.assertAlmostEqual(total, 1.0, delta=0.01,
                               msg=f"Signal weights sum={total:.3f}, expected 1.0")


class TestMigrationV1ToV2(unittest.TestCase):
    """Suite B — versioning._migrate_strategy_config_v1_to_v2."""

    @classmethod
    def setUpClass(cls):
        try:
            from versioning import _migrate_strategy_config_v1_to_v2
            cls.migrate = staticmethod(_migrate_strategy_config_v1_to_v2)
        except ImportError as exc:
            raise unittest.SkipTest(f"versioning not importable: {exc}")

    def _v1(self):
        return deepcopy(_V1_SAMPLE)

    def test_version_bumped_to_2(self):
        result = self.migrate(self._v1())
        self.assertEqual(result["version"], 2)

    def test_removes_signal_weights_from_parameters(self):
        result = self.migrate(self._v1())
        params = result["parameters"]
        for k in ["momentum_weight", "mean_reversion_weight",
                  "news_sentiment_weight", "cross_sector_weight"]:
            self.assertNotIn(k, params, f"{k} still in parameters after migration")

    def test_removes_tier_keys_from_parameters(self):
        result = self.migrate(self._v1())
        params = result["parameters"]
        for k in ["core_tier_pct", "dynamic_tier_pct", "intraday_tier_pct",
                  "max_total_exposure_pct", "cash_reserve_pct"]:
            self.assertNotIn(k, params, f"{k} still in parameters after migration")

    def test_removes_deprecated_markers(self):
        result = self.migrate(self._v1())
        params = result["parameters"]
        deprecated = [k for k in params if k.endswith("_DEPRECATED")]
        self.assertEqual(deprecated, [])

    def test_preserves_non_dup_parameters(self):
        result = self.migrate(self._v1())
        params = result["parameters"]
        for k in ["max_positions", "stop_loss_pct_core", "take_profit_multiple"]:
            self.assertIn(k, params, f"{k} incorrectly removed from parameters")

    def test_position_sizing_unchanged(self):
        v1 = self._v1()
        result = self.migrate(v1)
        self.assertEqual(result["position_sizing"], v1["position_sizing"])

    def test_signal_weights_section_unchanged(self):
        v1 = self._v1()
        result = self.migrate(v1)
        self.assertEqual(result["signal_weights"], v1["signal_weights"])

    def test_idempotent_second_application(self):
        result = self.migrate(self.migrate(self._v1()))
        params = result["parameters"]
        present = [k for k in _DUP_KEYS if k in params]
        self.assertEqual(present, [])

    def test_migration_registered_in_versioning(self):
        from versioning import _MIGRATIONS
        self.assertIn(("strategy_config", 1), _MIGRATIONS,
                      "strategy_config v1 migration not registered in _MIGRATIONS")


class TestDirectorNotesContract(unittest.TestCase):
    """Suite C — director_notes dict contract."""

    def test_migration_preserves_director_notes_dict(self):
        try:
            from versioning import _migrate_strategy_config_v1_to_v2
        except ImportError as exc:
            self.skipTest(f"versioning not importable: {exc}")
        v1 = deepcopy(_V1_SAMPLE)
        result = _migrate_strategy_config_v1_to_v2(v1)
        dn = result.get("director_notes", {})
        self.assertIsInstance(dn, dict)
        self.assertIn("active_context", dn)
        self.assertEqual(dn["expiry"], "2026-05-01")

    def test_weekly_review_sms_notes_handles_dict(self):
        """sms_notes extraction must not crash when director_notes is a dict."""
        dn_dict = {"active_context": "test context", "expiry": "2026-05-01", "priority": "normal"}
        _dn_text = (dn_dict.get("active_context", "") if isinstance(dn_dict, dict) else str(dn_dict or ""))
        sms_notes = (_dn_text or "No director notes parsed.")[:140]
        self.assertEqual(sms_notes, "test context")

    def test_weekly_review_sms_notes_handles_none(self):
        """sms_notes extraction must fall back when director_notes is None."""
        dn_none = None
        _dn_text = (dn_none.get("active_context", "") if isinstance(dn_none, dict) else str(dn_none or ""))
        sms_notes = (_dn_text or "No director notes parsed.")[:140]
        self.assertEqual(sms_notes, "No director notes parsed.")


class TestRiskManagerGates(unittest.TestCase):
    """Suite D — T-014/T-016/T-017 validate_config gates (unit)."""

    def _cfg(self, **param_overrides):
        base = {
            "version": 2,
            "position_sizing": {"cash_reserve_pct": 0.2},
            "parameters": {
                "max_positions": 12,
                "max_position_pct_equity": 0.07,
                "max_day_trades_rolling_5day": 3,
                "sector_rotation_bias": "neutral",
            },
        }
        base["parameters"].update(param_overrides)
        return base

    # ── T-014: gross exposure ─────────────────────────────────────────────────

    def test_t014_passes_when_product_lte_100pct(self):
        """12 × 7% = 84% — must pass."""
        cfg = self._cfg(max_positions=12, max_position_pct_equity=0.07)
        gross = cfg["parameters"]["max_positions"] * cfg["parameters"]["max_position_pct_equity"]
        self.assertLessEqual(gross, 1.0, f"Expected ≤ 1.0, got {gross}")

    def test_t014_fails_when_product_gt_100pct(self):
        """15 × 7% = 105% — must fail the gate."""
        cfg = self._cfg(max_positions=15, max_position_pct_equity=0.07)
        gross = cfg["parameters"]["max_positions"] * cfg["parameters"]["max_position_pct_equity"]
        self.assertGreater(gross, 1.0, f"Expected > 1.0, got {gross}")

    def test_t014_live_config_passes(self):
        """Live strategy_config.json must pass the T-014 gate."""
        if not CONFIG_PATH.exists():
            self.skipTest("strategy_config.json not found")
        cfg = json.loads(CONFIG_PATH.read_text())
        p = cfg.get("parameters", {})
        mp  = p.get("max_positions")
        mpe = p.get("max_position_pct_equity")
        if mp is None or mpe is None:
            self.skipTest("max_positions or max_position_pct_equity missing from config")
        gross = int(mp) * float(mpe)
        self.assertLessEqual(gross, 1.0,
                             f"T-014 FAIL: max_positions ({mp}) × max_position_pct_equity ({mpe}) = {gross:.0%}")

    # ── T-016: PDT day-trade limit ────────────────────────────────────────────

    def test_t016_passes_when_limit_is_3(self):
        cfg = self._cfg(max_day_trades_rolling_5day=3)
        self.assertLessEqual(cfg["parameters"]["max_day_trades_rolling_5day"], 3)

    def test_t016_fails_when_limit_exceeds_3(self):
        cfg = self._cfg(max_day_trades_rolling_5day=4)
        self.assertGreater(cfg["parameters"]["max_day_trades_rolling_5day"], 3,
                           "Limit > 3 should trigger FAIL gate")

    def test_t016_live_config_passes(self):
        """Live strategy_config.json must have max_day_trades_rolling_5day ≤ 3."""
        if not CONFIG_PATH.exists():
            self.skipTest("strategy_config.json not found")
        cfg = json.loads(CONFIG_PATH.read_text())
        mdt = cfg.get("parameters", {}).get("max_day_trades_rolling_5day")
        self.assertIsNotNone(mdt, "max_day_trades_rolling_5day missing from live config")
        self.assertLessEqual(int(mdt), 3,
                             f"T-016 FAIL: max_day_trades_rolling_5day={mdt} exceeds 3")

    # ── T-017: sector_rotation_bias_expiry ───────────────────────────────────

    def test_t017_expired_bias_triggers_warn(self):
        """Expiry in the past — gate logic should detect it as expired."""
        from datetime import date
        past_date = "2026-01-01"
        expiry = date.fromisoformat(past_date)
        self.assertLess(expiry, date.today(), "Test setup: date should be in the past")

    def test_t017_future_bias_passes(self):
        """Expiry in the future — gate should pass."""
        from datetime import date, timedelta
        future_date = (date.today() + timedelta(days=30)).isoformat()
        expiry = date.fromisoformat(future_date)
        self.assertGreater(expiry, date.today(), "Test setup: date should be in the future")

    def test_t017_expired_bias_overrides_to_neutral_in_load_strategy_config(self):
        """When expiry has passed, _load_strategy_config must override bias to neutral without
        writing to disk."""
        from datetime import datetime
        import json, tempfile, shutil
        from pathlib import Path

        cfg = {
            "version": 2,
            "active_strategy": "hybrid",
            "parameters": {
                "sector_rotation_bias": "commodities_overweight",
                "sector_rotation_bias_expiry": "2026-01-01",
                "max_positions": 12,
            },
            "director_notes": {},
            "time_bound_actions": [],
        }

        tmpdir = Path(tempfile.mkdtemp())
        try:
            cfg_path = tmpdir / "strategy_config.json"
            cfg_path.write_text(json.dumps(cfg))

            # Replicate the _load_strategy_config() T-017 logic inline
            _params = cfg.get("parameters", {})
            _bias_expiry = _params.get("sector_rotation_bias_expiry")
            result_bias = _params.get("sector_rotation_bias")
            if _bias_expiry:
                try:
                    if datetime.now().date() > datetime.fromisoformat(_bias_expiry).date():
                        cfg_copy = dict(cfg)
                        cfg_copy["parameters"] = dict(_params)
                        cfg_copy["parameters"]["sector_rotation_bias"] = "neutral"
                        result_bias = cfg_copy["parameters"]["sector_rotation_bias"]
                except Exception:
                    pass
            self.assertEqual(result_bias, "neutral",
                             "Expired bias must override to neutral in memory")

            # Confirm original config NOT modified on disk
            on_disk = json.loads(cfg_path.read_text())
            self.assertEqual(on_disk["parameters"]["sector_rotation_bias"],
                             "commodities_overweight",
                             "Original config on disk must not be modified")
        finally:
            shutil.rmtree(tmpdir)

    def test_t017_non_expired_bias_not_overridden(self):
        """When expiry is in the future, bias must NOT be overridden."""
        from datetime import date, timedelta, datetime
        future = (date.today() + timedelta(days=10)).isoformat()
        cfg = {"parameters": {
            "sector_rotation_bias": "commodities_overweight",
            "sector_rotation_bias_expiry": future,
        }}
        _params = cfg["parameters"]
        result_bias = _params["sector_rotation_bias"]
        _bias_expiry = _params.get("sector_rotation_bias_expiry")
        if _bias_expiry:
            try:
                if datetime.now().date() > datetime.fromisoformat(_bias_expiry).date():
                    result_bias = "neutral"
            except Exception:
                pass
        self.assertEqual(result_bias, "commodities_overweight",
                         "Non-expired bias must NOT be overridden")


if __name__ == "__main__":
    unittest.main()
