"""
tests/test_sprint6_phase_b.py — Sprint 6 Phase B: OptionsStructure symbol field fix.

Root cause: OptionsStructure used `underlying` but never emitted a `symbol` key
in to_dict()/JSON.  Consumers reading the raw dict with .get("symbol", "?") always
received "?".

Fix (schemas.py):
  - _occ_to_underlying(occ) helper: strips padding, extracts leading ticker letters.
  - OptionsStructure.symbol property: alias for self.underlying.
  - to_dict() now emits "symbol": self.underlying alongside "underlying".
  - from_dict() accepts "underlying" OR "symbol" (priority order), with OCC fallback.

Tests PB6-01 .. PB6-16
"""
from __future__ import annotations

import json
from dataclasses import asdict

from schemas import (
    OptionsLeg,
    OptionsStructure,
    OptionStrategy,
    StructureLifecycle,
    Tier,
    _occ_to_underlying,
)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_leg(occ: str, underlying: str = "NVDA") -> OptionsLeg:
    return OptionsLeg(
        occ_symbol=occ,
        underlying=underlying,
        side="buy",
        qty=1,
        option_type="call",
        strike=200.0,
        expiration="2026-05-08",
    )


def _make_structure(underlying: str = "NVDA", occ: str = "NVDA260508C00200000") -> OptionsStructure:
    return OptionsStructure(
        structure_id="test-abc",
        underlying=underlying,
        strategy=OptionStrategy.CALL_DEBIT_SPREAD,
        lifecycle=StructureLifecycle.PROPOSED,
        legs=[_make_leg(occ, underlying)],
        contracts=1,
        max_cost_usd=138.0,
        opened_at="2026-04-28T00:00:00+00:00",
        catalyst="",
        tier=Tier.CORE,
    )


# ─────────────────────────────────────────────────────────────────────────────
# PB6-01..PB6-04 — _occ_to_underlying
# ─────────────────────────────────────────────────────────────────────────────

class TestOccToUnderlying:
    def test_pb601_standard_format(self):
        """Standard OCC symbol without padding returns correct ticker."""
        assert _occ_to_underlying("NVDA260508C00200000") == "NVDA"

    def test_pb602_padded_format(self):
        """Space-padded Alpaca OCC format returns correct ticker."""
        assert _occ_to_underlying("NVDA  260522P00205000") == "NVDA"

    def test_pb603_longer_ticker(self):
        """Five-letter ticker extracted correctly."""
        assert _occ_to_underlying("GOOGL260508C00155000") == "GOOGL"

    def test_pb604_empty_input(self):
        """Empty string returns empty string, no exception."""
        assert _occ_to_underlying("") == ""


# ─────────────────────────────────────────────────────────────────────────────
# PB6-05..PB6-08 — OptionsStructure.symbol property
# ─────────────────────────────────────────────────────────────────────────────

class TestSymbolProperty:
    def test_pb605_symbol_equals_underlying(self):
        """structure.symbol returns the same value as structure.underlying."""
        st = _make_structure("NVDA")
        assert st.symbol == st.underlying == "NVDA"

    def test_pb606_symbol_property_different_tickers(self):
        """symbol property works for arbitrary tickers."""
        for ticker in ("AAPL", "GOOGL", "XLE", "GLD"):
            st = _make_structure(ticker)
            assert st.symbol == ticker

    def test_pb607_symbol_not_in_asdict(self):
        """symbol is a property, not a field — asdict() must NOT include it."""
        st = _make_structure("NVDA")
        d = asdict(st)
        assert "symbol" not in d, "symbol is a property, asdict() must not duplicate it"

    def test_pb608_to_dict_has_both_keys(self):
        """to_dict() emits both 'symbol' and 'underlying' with the same value."""
        st = _make_structure("NVDA")
        d = st.to_dict()
        assert "symbol" in d
        assert "underlying" in d
        assert d["symbol"] == d["underlying"] == "NVDA"


# ─────────────────────────────────────────────────────────────────────────────
# PB6-09..PB6-12 — from_dict backward compatibility
# ─────────────────────────────────────────────────────────────────────────────

class TestFromDictBackwardCompat:
    def test_pb609_existing_structure_no_symbol_key(self):
        """from_dict() loads a dict without 'symbol' using 'underlying' (existing structures)."""
        st = _make_structure("WMT")
        d = st.to_dict()
        del d["symbol"]  # simulate old JSON format
        loaded = OptionsStructure.from_dict(d)
        assert loaded.symbol == "WMT"
        assert loaded.underlying == "WMT"

    def test_pb610_occ_fallback_when_both_missing(self):
        """If both 'underlying' and 'symbol' absent, symbol is derived from OCC leg."""
        st = _make_structure("NVDA", "NVDA260508C00200000")
        d = st.to_dict()
        del d["symbol"]
        del d["underlying"]
        for leg in d["legs"]:
            leg.pop("underlying", None)
        loaded = OptionsStructure.from_dict(d)
        assert loaded.symbol == "NVDA"

    def test_pb611_symbol_key_used_when_underlying_absent(self):
        """If 'underlying' absent but 'symbol' present, uses 'symbol'."""
        st = _make_structure("GLD")
        d = st.to_dict()
        del d["underlying"]
        # d still has "symbol": "GLD"
        loaded = OptionsStructure.from_dict(d)
        assert loaded.symbol == "GLD"

    def test_pb612_new_structure_round_trip(self):
        """Full round-trip: to_dict() → from_dict() → symbol/underlying match."""
        st = _make_structure("XLE")
        loaded = OptionsStructure.from_dict(st.to_dict())
        assert loaded.symbol == "XLE"
        assert loaded.underlying == "XLE"


# ─────────────────────────────────────────────────────────────────────────────
# PB6-13..PB6-16 — real-world structures.json compatibility
# ─────────────────────────────────────────────────────────────────────────────

class TestRealWorldCompat:
    _SAMPLE_RAW = {
        "structure_id": "dd990642-db1e-4782-99be-4e0e7535c692",
        "underlying": "NVDA",
        "strategy": "call_debit_spread",
        "lifecycle": "fully_filled",
        "legs": [
            {
                "occ_symbol": "NVDA260508C00200000",
                "underlying": "NVDA",
                "side": "buy",
                "qty": 1,
                "option_type": "call",
                "strike": 200.0,
                "expiration": "2026-05-08",
                "order_id": None,
                "filled_price": None,
                "bid": 7.1,
                "ask": 7.2,
                "mid": 7.15,
                "delta": None,
                "open_interest": 64046,
                "volume": 862,
            }
        ],
        "contracts": 10,
        "max_cost_usd": 1375.0,
        "opened_at": "2026-04-22T16:07:34.540005+00:00",
        "catalyst": "",
        "tier": "core",
        "iv_rank": 32.5,
        "order_ids": [],
        "closed_at": None,
        "realized_pnl": None,
        "notes": "",
        "direction": "bullish",
        "expiration": "2026-05-08",
        "long_strike": 200.0,
        "short_strike": 202.5,
        "debit_paid": None,
        "max_profit_usd": 1125.0,
        "audit_log": [],
        "roll_group_id": None,
        "roll_from_structure_id": None,
        "roll_reason": "",
        "thesis_status": "intact",
        "close_reason_code": None,
        "close_reason_detail": None,
        "roll_reason_code": None,
        "roll_reason_detail": None,
        "rolled_to_structure_id": None,
        "initiated_by": None,
    }

    def test_pb613_existing_json_loads_with_symbol(self):
        """A real structures.json entry (without 'symbol') loads and has .symbol set."""
        loaded = OptionsStructure.from_dict(self._SAMPLE_RAW)
        assert loaded.symbol == "NVDA"

    def test_pb614_save_adds_symbol_to_json(self):
        """After one save/load cycle the JSON now contains 'symbol'."""
        loaded = OptionsStructure.from_dict(self._SAMPLE_RAW)
        d = loaded.to_dict()
        assert d["symbol"] == "NVDA"

    def test_pb615_padded_occ_fallback(self):
        """Space-padded OCC symbol in legs produces correct underlying via fallback."""
        raw = dict(self._SAMPLE_RAW)
        raw = {k: v for k, v in raw.items() if k not in ("underlying", "symbol")}
        raw["legs"] = [
            {**self._SAMPLE_RAW["legs"][0], "occ_symbol": "NVDA  260508C00200000",
             "underlying": ""}
        ]
        loaded = OptionsStructure.from_dict(raw)
        assert loaded.symbol == "NVDA"

    def test_pb616_json_serialisable(self):
        """to_dict() output is JSON-serialisable (no datetime objects or enums)."""
        st = _make_structure("NVDA")
        serialised = json.dumps(st.to_dict())
        parsed = json.loads(serialised)
        assert parsed["symbol"] == "NVDA"
        assert parsed["underlying"] == "NVDA"
