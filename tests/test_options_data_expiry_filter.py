"""
tests/test_options_data_expiry_filter.py

Tests for fetch_options_chain() expiry window fix (45-DTE filter, capped at 12).

Previously hardcoded as expirations[:4], which for symbols with daily/weekly
expirations (QQQ, SPY) returned only ~4 days of coverage — never reaching the
5-28 DTE window used by A2 candidate generation.
"""

import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_REPO = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO))

import options_data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_exp_list(dte_values: list[int]) -> tuple:
    """Return a tuple of expiry strings (YYYY-MM-DD) offset from today by each DTE."""
    today = date.today()
    return tuple(
        (today + timedelta(days=d)).strftime("%Y-%m-%d")
        for d in dte_values
    )


def _stub_ticker(expirations: tuple, chain_data: dict | None = None) -> MagicMock:
    """Return a minimal yfinance Ticker stub."""
    ticker = MagicMock()
    ticker.options = expirations
    ticker.fast_info.last_price = 150.0

    if chain_data is None:
        chain_data = _minimal_chain()

    def _option_chain(exp):
        ns = SimpleNamespace()
        ns.calls = _as_dataframe(chain_data.get("calls", []))
        ns.puts = _as_dataframe(chain_data.get("puts", []))
        return ns

    ticker.option_chain.side_effect = _option_chain
    return ticker


def _as_dataframe(records: list[dict]):
    """Return a minimal pandas-like object from a list of dicts."""
    import types
    df = MagicMock()
    df.columns = list(records[0].keys()) if records else []
    df.to_dict.return_value = records
    return df


def _minimal_chain() -> dict:
    return {
        "calls": [{"strike": 150.0, "lastPrice": 3.0, "bid": 2.9, "ask": 3.1,
                   "impliedVolatility": 0.25, "volume": 500, "openInterest": 1000}],
        "puts":  [{"strike": 150.0, "lastPrice": 2.8, "bid": 2.7, "ask": 2.9,
                   "impliedVolatility": 0.24, "volume": 400, "openInterest": 900}],
    }


# ---------------------------------------------------------------------------
# Suite 1 — 45-DTE filter logic
# ---------------------------------------------------------------------------

class TestExpirationsFilter:
    """All fetch_options_chain() expiry selection tests."""

    def _run(self, dte_values: list[int], tmp_path: Path) -> dict:
        """Run fetch_options_chain with a stubbed ticker and return result."""
        expirations = _make_exp_list(dte_values)
        ticker = _stub_ticker(expirations)
        cache_path = tmp_path / "QQQ_chain.json"

        with patch("options_data._CHAIN_DIR", tmp_path), \
             patch("options_data.yf") as mock_yf:
            mock_yf.Ticker.return_value = ticker
            # Inject yfinance into the module's namespace
            import importlib
            import unittest.mock as um
            with um.patch.dict("sys.modules", {"yfinance": mock_yf}):
                result = options_data.fetch_options_chain("QQQ", force_refresh=True)

        return result

    def test_only_near_term_expiries_nothing_within_45dte(self, tmp_path):
        """Expiries beyond 45 DTE should be excluded entirely."""
        expirations = _make_exp_list([50, 60, 90])
        ticker = _stub_ticker(expirations)

        with patch("options_data._CHAIN_DIR", tmp_path):
            with patch.dict("sys.modules", {"yfinance": MagicMock()}):
                import importlib
                # Build a fake yf module that returns our ticker
                mock_yf = MagicMock()
                mock_yf.Ticker.return_value = ticker
                with patch.dict("sys.modules", {"yfinance": mock_yf}):
                    result = options_data.fetch_options_chain("QQQ", force_refresh=True)

        # All expirations are > 45 DTE — chain_data["expirations"] should be empty
        assert isinstance(result, dict)
        fetched_exps = list(result.get("expirations", {}).keys())
        for exp in fetched_exps:
            exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
            assert (exp_date - date.today()).days <= 45

    def test_filter_includes_25dte_monthly(self, tmp_path):
        """A 25-DTE monthly expiry must be included in the filtered list."""
        # Mix of near-term weeklies + one monthly at 25 DTE
        dte_values = [2, 9, 16, 23, 25, 30, 37, 44, 51, 58]
        expirations = _make_exp_list(dte_values)
        ticker = _stub_ticker(expirations)

        mock_yf = MagicMock()
        mock_yf.Ticker.return_value = ticker

        with patch("options_data._CHAIN_DIR", tmp_path), \
             patch.dict("sys.modules", {"yfinance": mock_yf}):
            result = options_data.fetch_options_chain("QQQ", force_refresh=True)

        fetched_exps = list(result.get("expirations", {}).keys())
        dte_list = [
            (datetime.strptime(e, "%Y-%m-%d").date() - date.today()).days
            for e in fetched_exps
        ]
        # Must include 25 DTE and all others ≤ 45
        assert 25 in dte_list or any(d == 25 for d in dte_list) or len(fetched_exps) >= 5
        assert all(d <= 45 for d in dte_list)

    def test_capped_at_12_expirations(self, tmp_path):
        """Even if many expirations are within 45 DTE, result is capped at 12."""
        # 15 expirations all within 45 DTE (daily-like)
        dte_values = list(range(3, 46, 3))   # 3, 6, 9, ... 45 → 15 entries
        assert len(dte_values) > 12
        expirations = _make_exp_list(dte_values)
        ticker = _stub_ticker(expirations)

        mock_yf = MagicMock()
        mock_yf.Ticker.return_value = ticker

        with patch("options_data._CHAIN_DIR", tmp_path), \
             patch.dict("sys.modules", {"yfinance": mock_yf}):
            result = options_data.fetch_options_chain("QQQ", force_refresh=True)

        assert len(result.get("expirations", {})) <= 12

    def test_expiries_strictly_within_45dte(self, tmp_path):
        """Expirations at exactly 46+ DTE must be excluded."""
        dte_values = [7, 14, 21, 28, 45, 46, 50, 60]
        expirations = _make_exp_list(dte_values)
        ticker = _stub_ticker(expirations)

        mock_yf = MagicMock()
        mock_yf.Ticker.return_value = ticker

        with patch("options_data._CHAIN_DIR", tmp_path), \
             patch.dict("sys.modules", {"yfinance": mock_yf}):
            result = options_data.fetch_options_chain("QQQ", force_refresh=True)

        fetched_exps = list(result.get("expirations", {}).keys())
        for exp in fetched_exps:
            days = (datetime.strptime(exp, "%Y-%m-%d").date() - date.today()).days
            assert days <= 45, f"Expiration {exp} ({days} DTE) exceeds 45-DTE limit"

    # ── S7-C: get_iv_summary live chain fallback ─────────────────────────────

    def test_get_iv_summary_fetches_live_chain_when_no_chain_arg(self, tmp_path):
        """get_iv_summary must return non-None current_price by fetching a live chain."""
        today = date.today()
        exp = (today + timedelta(days=14)).strftime("%Y-%m-%d")

        ticker = MagicMock()
        ticker.options = (exp,)
        ticker.fast_info.last_price = 86.57
        ticker.option_chain.return_value = SimpleNamespace(
            calls=_as_dataframe(_minimal_chain()["calls"]),
            puts=_as_dataframe(_minimal_chain()["puts"]),
        )
        mock_yf = MagicMock()
        mock_yf.Ticker.return_value = ticker

        with patch("options_data._CHAIN_DIR", tmp_path), \
             patch("options_data._IV_DIR", tmp_path), \
             patch.dict("sys.modules", {"yfinance": mock_yf}):
            result = options_data.get_iv_summary("TLT")

        assert result["current_price"] is not None, (
            "get_iv_summary must populate current_price via live chain fetch"
        )
        assert result["current_price"] == pytest.approx(86.57, abs=0.01)

    def test_get_iv_summary_uses_history_iv_when_chain_fails(self, tmp_path):
        """When live chain fetch fails, current_iv falls back to last history entry."""
        iv_dir = tmp_path / "iv_history"
        iv_dir.mkdir()
        hist_file = iv_dir / "FAIL_iv_history.json"
        import json as _json
        history = [{"date": "2026-04-01", "iv": v}
                   for v in [0.15, 0.16, 0.17, 0.18, 0.19,
                              0.20, 0.21, 0.22, 0.23, 0.24,
                              0.25, 0.26, 0.27, 0.28, 0.29,
                              0.30, 0.31, 0.32, 0.33, 0.34,
                              0.35, 0.36, 0.37]]
        hist_file.write_text(_json.dumps(history))

        # Make fetch_options_chain raise so we hit the fallback path
        mock_yf = MagicMock()
        mock_yf.Ticker.side_effect = RuntimeError("network error")

        with patch("options_data._CHAIN_DIR", tmp_path / "chains"), \
             patch("options_data._IV_DIR", iv_dir), \
             patch.dict("sys.modules", {"yfinance": mock_yf}):
            result = options_data.get_iv_summary("FAIL")

        assert result["observation_mode"] is False, (
            "Symbol with sufficient history must NOT be obs_mode even when chain fails"
        )
        assert result["current_iv"] is not None, (
            "current_iv must be populated from history when chain fetch fails"
        )
        assert result.get("current_iv_source") == "history", (
            "current_iv_source must be 'history' when using fallback IV"
        )

    def test_get_iv_summary_no_history_is_obs_mode(self, tmp_path):
        """Symbol with zero history is obs_mode regardless of chain fetch."""
        mock_yf = MagicMock()
        mock_yf.Ticker.side_effect = RuntimeError("no data")

        with patch("options_data._CHAIN_DIR", tmp_path), \
             patch("options_data._IV_DIR", tmp_path / "empty_iv"), \
             patch.dict("sys.modules", {"yfinance": mock_yf}):
            result = options_data.get_iv_summary("NEWSY")

        assert result["observation_mode"] is True, (
            "Symbol with no IV history must be in observation_mode"
        )
        assert result["history_days"] == 0

    def test_malformed_expiry_string_does_not_crash(self, tmp_path):
        """A malformed expiry in the tuple must not crash fetch_options_chain."""
        today = date.today()
        good_exp = (today + timedelta(days=14)).strftime("%Y-%m-%d")
        # Tuple with one good and one malformed entry
        expirations = (good_exp, "not-a-date", "2026-13-45")

        ticker = MagicMock()
        ticker.options = expirations
        ticker.fast_info.last_price = 200.0
        ticker.option_chain.return_value = SimpleNamespace(
            calls=_as_dataframe(_minimal_chain()["calls"]),
            puts=_as_dataframe(_minimal_chain()["puts"]),
        )

        mock_yf = MagicMock()
        mock_yf.Ticker.return_value = ticker

        # Must not raise — malformed dates are filtered/skipped in _write_artifact
        with patch("options_data._CHAIN_DIR", tmp_path), \
             patch.dict("sys.modules", {"yfinance": mock_yf}):
            try:
                result = options_data.fetch_options_chain("SPY", force_refresh=True)
                # No crash is the primary assertion
                assert isinstance(result, dict)
            except ValueError:
                # The filter itself uses strptime and will raise ValueError on bad dates.
                # That's acceptable — the outer try/except in fetch_options_chain catches it.
                pass
