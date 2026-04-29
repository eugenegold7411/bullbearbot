"""
Tests for Fix C: morning brief entry_zone staleness filter.

  - Held positions bypass the ratio check (_validate_and_sanitize_brief)
  - Non-held positions with stale entry zones are still dropped
  - _load_context injects held-position prices
"""
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Minimal stubs so morning_brief can be imported without real dependencies
# ---------------------------------------------------------------------------

def _make_stub(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    sys.modules.setdefault(name, mod)
    return mod


for _name in [
    "anthropic",
    "dotenv",
    "log_setup",
    # data_warehouse intentionally omitted — it is the real module (or already
    # stubbed by conftest) and must not be replaced at module level; otherwise
    # tests that import market_data (which does `import data_warehouse as dw`)
    # would receive our stub instead of the real module, breaking their patches.
    "watchlist_manager",
    "options_state",
    "insider_intelligence",
    "earnings_calendar_lookup",
]:
    _make_stub(_name)

# anthropic stub
sys.modules["anthropic"].Anthropic = MagicMock(return_value=MagicMock())

# dotenv stub
sys.modules["dotenv"].load_dotenv = lambda: None

# log_setup: return real Python loggers so caplog works in other test files
sys.modules["log_setup"].get_logger = lambda name: __import__("logging").getLogger(name)

sys.path.insert(0, str(Path(__file__).parent.parent))
import morning_brief as mb  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pick(symbol: str, entry_zone: str) -> dict:
    return {
        "symbol": symbol,
        "direction": "long",
        "entry_zone": entry_zone,
        "stop": "1",
        "target": "999",
        "conviction": "high",
        "catalyst": {"type": "macro", "date_iso": None, "days_away": None, "short_text": "test"},
        "risk": "test risk",
    }


def _brief(picks: list) -> dict:
    return {"market_tone": "bullish", "key_themes": [], "conviction_picks": picks, "avoid_today": [], "brief_summary": "test"}


# ---------------------------------------------------------------------------
# Suite A — _validate_and_sanitize_brief
# ---------------------------------------------------------------------------

class TestValidateAndSanitizeBrief:
    """A1–A5: held-position exemption + non-held staleness filter."""

    def test_A1_held_position_with_stale_entry_zone_kept(self):
        """GOOGL-case: held at $350, entry_zone $160 → must NOT be dropped."""
        brief = _brief([_make_pick("GOOGL", "159-163")])
        with (
            patch.object(mb, "_get_held_symbols", return_value={"GOOGL"}),
            patch.object(mb, "_current_prices_from_disk", return_value={"GOOGL": 349.76}),
        ):
            result = mb._validate_and_sanitize_brief(brief)
        assert len(result["conviction_picks"]) == 1
        assert result["conviction_picks"][0]["symbol"] == "GOOGL"
        assert "_validation_note" not in result

    def test_A2_non_held_stale_entry_zone_dropped(self):
        """SPOT-case: not held, entry_zone $170 vs price $518 → dropped."""
        brief = _brief([_make_pick("SPOT", "168-172")])
        with (
            patch.object(mb, "_get_held_symbols", return_value=set()),
            patch.object(mb, "_current_prices_from_disk", return_value={"SPOT": 518.01}),
        ):
            result = mb._validate_and_sanitize_brief(brief)
        assert result["conviction_picks"] == []
        assert "1 pick(s) dropped" in result.get("_validation_note", "")

    def test_A3_held_position_with_valid_entry_zone_also_kept(self):
        """Held position with a reasonable entry zone: kept and no note added."""
        brief = _brief([_make_pick("XOM", "148-153")])
        with (
            patch.object(mb, "_get_held_symbols", return_value={"XOM"}),
            patch.object(mb, "_current_prices_from_disk", return_value={"XOM": 150.55}),
        ):
            result = mb._validate_and_sanitize_brief(brief)
        assert len(result["conviction_picks"]) == 1
        assert "_validation_note" not in result

    def test_A4_mixed_held_and_stale_non_held(self):
        """GOOGL held (stale zone) + SPOT not held (stale zone): GOOGL kept, SPOT dropped."""
        brief = _brief([
            _make_pick("GOOGL", "160-165"),
            _make_pick("SPOT", "168-172"),
        ])
        with (
            patch.object(mb, "_get_held_symbols", return_value={"GOOGL"}),
            patch.object(mb, "_current_prices_from_disk", return_value={
                "GOOGL": 349.76,
                "SPOT": 518.01,
            }),
        ):
            result = mb._validate_and_sanitize_brief(brief)
        syms = [p["symbol"] for p in result["conviction_picks"]]
        assert syms == ["GOOGL"]
        assert "1 pick(s) dropped" in result.get("_validation_note", "")

    def test_A5_no_price_data_pick_kept_for_non_held(self):
        """If no price is available for a non-held symbol, give benefit of the doubt."""
        brief = _brief([_make_pick("NEWCO", "50-55")])
        with (
            patch.object(mb, "_get_held_symbols", return_value=set()),
            patch.object(mb, "_current_prices_from_disk", return_value={}),
        ):
            result = mb._validate_and_sanitize_brief(brief)
        assert len(result["conviction_picks"]) == 1


# ---------------------------------------------------------------------------
# Suite B — _load_context held-prices injection
# ---------------------------------------------------------------------------

class TestLoadContextHeldPrices:
    """B1–B3: held-position price section is injected into context.

    _load_context() uses local imports inside try/except blocks; those imports
    resolve from sys.modules["data_warehouse"] at call time.  We use
    patch.dict(sys.modules) to temporarily swap in a MagicMock stub for the
    duration of each test.  This is fully scoped and does not pollute the real
    data_warehouse module that other test files need.
    """

    def _bars(self, price: float) -> list:
        return [{"close": price, "open": price, "high": price, "low": price, "volume": 1000}]

    def _dw_stub(self, bars_fn=None) -> MagicMock:
        """Return a data_warehouse MagicMock where every section-helper raises
        (so only the held-prices block under test can produce output) and
        load_bars_cached is optionally customised."""
        dw = MagicMock()
        dw.load_global_indices.side_effect    = Exception("skip")
        dw.load_macro_snapshot.side_effect    = Exception("skip")
        dw.load_sector_perf.side_effect       = Exception("skip")
        dw.load_earnings_calendar.side_effect = Exception("skip")
        if bars_fn is not None:
            dw.load_bars_cached.side_effect = bars_fn
        else:
            dw.load_bars_cached.side_effect = Exception("skip")
        return dw

    def test_B1_held_prices_section_appears_in_context(self):
        """When positions are held, context includes HELD POSITIONS section."""
        fake_bars = {"GOOGL": self._bars(349.76), "V": self._bars(336.63)}
        dw = self._dw_stub(bars_fn=lambda sym: fake_bars.get(sym, []))
        with (
            patch.dict(sys.modules, {"data_warehouse": dw}),
            patch.object(mb, "_get_held_symbols", return_value={"GOOGL", "V"}),
            patch.object(mb, "_load_overnight_digest", return_value=""),
        ):
            ctx = mb._load_context()

        assert "HELD POSITIONS" in ctx
        assert "GOOGL: $349.76" in ctx
        assert "V: $336.63" in ctx

    def test_B2_no_held_positions_no_section(self):
        """When no positions held, context omits the HELD POSITIONS section."""
        dw = self._dw_stub()
        with (
            patch.dict(sys.modules, {"data_warehouse": dw}),
            patch.object(mb, "_get_held_symbols", return_value=set()),
            patch.object(mb, "_load_overnight_digest", return_value=""),
        ):
            ctx = mb._load_context()

        assert "HELD POSITIONS" not in ctx

    def test_B3_held_prices_failure_does_not_crash_context(self):
        """Exception in held-price lookup is swallowed; rest of context is returned."""
        dw = self._dw_stub()
        with (
            patch.dict(sys.modules, {"data_warehouse": dw}),
            patch.object(mb, "_get_held_symbols", side_effect=RuntimeError("alpaca down")),
            patch.object(mb, "_load_overnight_digest", return_value="some digest"),
        ):
            ctx = mb._load_context()

        assert "HELD POSITIONS" not in ctx
        assert "some digest" in ctx
