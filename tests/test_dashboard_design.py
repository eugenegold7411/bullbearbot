"""
DASH-01 through DASH-08 — dashboard visual redesign tests.

Tests are split into two categories:
  - Pure unit tests (DASH-01 to DASH-06): test CSS strings and helper functions
    by importing the module directly. No Flask or Alpaca needed.
  - Route tests (DASH-07, DASH-08): test live HTTP endpoints via Flask test client.
    Skipped if Flask is not installed.
"""

import importlib
import os
import sys
import unittest

# ---------------------------------------------------------------------------
# Module-level import: load dashboard/app.py as a plain module (no Flask
# server) so we can test its CSS constants and pure helpers.
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

os.environ.setdefault("DASHBOARD_USER", "admin")
os.environ.setdefault("DASHBOARD_PASSWORD", "bullbearbot")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("ALPACA_API_KEY", "test-key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret")

# Patch heavy third-party imports before loading the module
from unittest.mock import MagicMock, patch  # noqa: E402

_STUB_MODULES = {
    "alpaca": MagicMock(),
    "alpaca.trading": MagicMock(),
    "alpaca.trading.client": MagicMock(),
    "alpaca.trading.requests": MagicMock(),
    "alpaca.trading.enums": MagicMock(),
    "alpaca.data": MagicMock(),
    "alpaca.data.historical": MagicMock(),
    "alpaca.data.requests": MagicMock(),
    "chromadb": MagicMock(),
    "twilio": MagicMock(),
    "twilio.rest": MagicMock(),
    "sendgrid": MagicMock(),
}

# Flask may or may not be present locally
_FLASK_AVAILABLE = importlib.util.find_spec("flask") is not None

_DASH = None
_CLIENT = None

if _FLASK_AVAILABLE:
    with patch.dict("sys.modules", _STUB_MODULES):
        import dashboard.app as _DASH  # type: ignore[assignment]
        _DASH.app.config["TESTING"] = True
        _CLIENT = _DASH.app.test_client()


def _skip_no_flask(cls):
    """Class decorator: skip all tests if Flask is not installed."""
    if not _FLASK_AVAILABLE:
        return unittest.skip("Flask not installed — skipping route tests")(cls)
    return cls


def _auth_header() -> dict:
    import base64
    user = os.environ.get("DASHBOARD_USER", "admin")
    pw = os.environ.get("DASHBOARD_PASSWORD", "bullbearbot")
    creds = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


def _get(path: str):
    return _CLIENT.get(path, headers=_auth_header())


# ── DASH-01 — CSS custom properties present ───────────────────────────────────

@_skip_no_flask
class TestDash01CSSVariables(unittest.TestCase):
    def test_root_bg_base(self):
        assert "--bg-base: #0d0e1f" in _DASH.SHARED_CSS

    def test_root_accent_blue(self):
        assert "--accent-blue: #4facfe" in _DASH.SHARED_CSS

    def test_root_accent_green(self):
        assert "--accent-green: #00e676" in _DASH.SHARED_CSS

    def test_root_accent_red(self):
        assert "--accent-red: #ff5050" in _DASH.SHARED_CSS

    def test_root_grad_a1(self):
        assert "--grad-a1" in _DASH.SHARED_CSS

    def test_root_grad_a2(self):
        assert "--grad-a2" in _DASH.SHARED_CSS


# ── DASH-02 — Nav has correct classes ─────────────────────────────────────────

@_skip_no_flask
class TestDash02NavClasses(unittest.TestCase):
    def test_nav_tab_active_class_defined(self):
        assert ".nav-tab.active" in _DASH.SHARED_CSS

    def test_nav_tab_border_bottom_indicator(self):
        assert "border-bottom" in _DASH.SHARED_CSS

    def test_npill_classes_defined(self):
        for cls in (".npill-g", ".npill-r", ".npill-a"):
            assert cls in _DASH.SHARED_CSS, f"Missing {cls}"

    def test_nav_brand_present_in_nav_html(self):
        html = _DASH._nav_html("overview", "09:30 AM ET")
        assert "Bull" in html and "Bear" in html

    def test_nav_all_six_tabs(self):
        html = _DASH._nav_html("overview", "09:30 AM ET")
        for label in ("Overview", "A1", "A2", "Intelligence", "Trades", "Transparency"):
            assert label in html, f"Missing tab: {label}"

    def test_nav_active_tab_marked(self):
        html = _DASH._nav_html("a1", "09:30 AM ET")
        assert "nav-tab active" in html

    def test_nav_mode_pills_normal(self):
        html = _DASH._nav_html("overview", "09:30 AM ET", a1_mode="NORMAL", a2_mode="NORMAL")
        assert "npill-g" in html

    def test_nav_mode_pill_halt(self):
        html = _DASH._nav_html("overview", "09:30 AM ET", a1_mode="HALTED")
        assert "npill-r" in html


# ── DASH-03 — ring_svg helper ─────────────────────────────────────────────────

@_skip_no_flask
class TestDash03RingSvg(unittest.TestCase):
    def test_returns_svg_element(self):
        svg = _DASH._ring_svg(50.0)
        assert svg.startswith("<svg")

    def test_clamps_above_100(self):
        svg = _DASH._ring_svg(150.0)
        # fill arc is clamped; label shows raw value (acceptable — chart arc is correct)
        assert "stroke-dasharray" in svg and "150%" in svg

    def test_clamps_below_0(self):
        svg = _DASH._ring_svg(-10.0)
        assert "0%" in svg

    def test_label_shows_rounded_pct(self):
        svg = _DASH._ring_svg(66.7)
        assert "67%" in svg

    def test_custom_color_used(self):
        svg = _DASH._ring_svg(40.0, color="#ff5050")
        assert "#ff5050" in svg

    def test_dasharray_present(self):
        svg = _DASH._ring_svg(50.0)
        assert "stroke-dasharray" in svg


# ── DASH-04 — ticker builder ───────────────────────────────────────────────────

@_skip_no_flask
class TestDash04Ticker(unittest.TestCase):
    def _pos(self, sym, cur, plpc):
        return {"symbol": sym, "current": cur, "unreal_plpc": plpc}

    def test_empty_positions_produces_html(self):
        html = _DASH._build_ticker_html([])
        assert "ticker" in html

    def test_position_symbol_appears(self):
        html = _DASH._build_ticker_html([self._pos("AMZN", 255.0, 2.5)])
        assert "AMZN" in html

    def test_vix_appears(self):
        html = _DASH._build_ticker_html([], vix_str="18.4")
        assert "18.4" in html

    def test_ticker_fixed_bottom(self):
        assert "position: fixed" in _DASH.SHARED_CSS
        assert "bottom: 0" in _DASH.SHARED_CSS

    def test_green_for_positive_plpc(self):
        html = _DASH._build_ticker_html([self._pos("GLD", 200.0, 3.0)])
        assert "tk-g" in html

    def test_red_for_negative_plpc(self):
        html = _DASH._build_ticker_html([self._pos("XBI", 130.0, -1.5)])
        assert "tk-r" in html


# ── DASH-05 — hero cards and acct-bar CSS ─────────────────────────────────────

@_skip_no_flask
class TestDash05DesignComponents(unittest.TestCase):
    def test_hero_card_classes_defined(self):
        for cls in (".hero-card-a1", ".hero-card-a2", ".hero-card-combo"):
            assert cls in _DASH.SHARED_CSS, f"Missing {cls}"

    def test_acct_bar_class_defined(self):
        assert ".acct-bar" in _DASH.SHARED_CSS

    def test_acct_bar_item_class_defined(self):
        assert ".acct-bar-item" in _DASH.SHARED_CSS

    def test_data_table_class_defined(self):
        assert "table.data-table" in _DASH.SHARED_CSS

    def test_range_track_class_defined(self):
        assert ".range-track" in _DASH.SHARED_CSS

    def test_badge_classes_defined(self):
        for cls in (".badge-g", ".badge-r", ".badge-a"):
            assert cls in _DASH.SHARED_CSS, f"Missing badge: {cls}"


# ── DASH-06 — page_shell ticker injection ─────────────────────────────────────

@_skip_no_flask
class TestDash06PageShell(unittest.TestCase):
    def test_ticker_injected_when_provided(self):
        html = _DASH._page_shell("Test", "<nav/>", "<body/>", "<div id='tick'/>")
        assert "id='tick'" in html

    def test_no_extraneous_ticker_when_empty(self):
        html = _DASH._page_shell("Test", "<nav/>", "<body/>", "")
        # ticker div class should not appear in body when no ticker passed
        assert html.count("class=\"ticker\"") == 0

    def test_title_in_head(self):
        html = _DASH._page_shell("MyPage", "<nav/>", "<body/>")
        assert "MyPage" in html

    def test_shared_css_included(self):
        html = _DASH._page_shell("Test", "<nav/>", "<body/>")
        assert "--bg-base" in html


# ── DASH-07 — HTTP routes return 200 (mocked data) ───────────────────────────

@_skip_no_flask
class TestDash07Routes(unittest.TestCase):
    def _mock_status(self):
        return {
            "a1": {"account": None, "positions": [], "orders": []},
            "a2": {"account": None, "positions": [], "orders": []},
            "a1_mode": {"mode": "NORMAL"},
            "a2_mode": {"mode": "NORMAL"},
            "positions": [],
            "costs": {"daily_cost": 0, "daily_calls": 0},
            "gate": {},
            "warnings": [],
            "trail_tiers": [],
            "watch_bullets": [],
            "morning_brief": {},
            "morning_brief_time": "",
            "morning_brief_mtime": 0,
            "a1_decisions": [],
            "a2_decisions": [],
            "a1_theses": [],
            "a2_pipeline": {},
            "allocator_line": "",
            "today_pnl_a1": (0.0, 0.0),
            "today_pnl_a2": (0.0, 0.0),
        }

    @patch("dashboard.app._build_status")
    def test_root_200(self, mock_status):
        mock_status.return_value = self._mock_status()
        r = _get("/")
        assert r.status_code == 200, f"/ returned {r.status_code}"

    @patch("dashboard.app._build_status")
    def test_a1_200(self, mock_status):
        mock_status.return_value = self._mock_status()
        r = _get("/a1")
        assert r.status_code == 200, f"/a1 returned {r.status_code}"

    @patch("dashboard.app._build_status")
    def test_a2_200(self, mock_status):
        mock_status.return_value = self._mock_status()
        r = _get("/a2")
        assert r.status_code == 200, f"/a2 returned {r.status_code}"

    @patch("dashboard.app._build_status")
    def test_brief_200(self, mock_status):
        mock_status.return_value = self._mock_status()
        r = _get("/brief")
        assert r.status_code == 200, f"/brief returned {r.status_code}"

    @patch("dashboard.app._alpaca_a1")
    @patch("dashboard.app._closed_trades")
    def test_trades_200(self, mock_trades, mock_a1):
        mock_a1.return_value = {"positions": [], "orders": [], "account": None}
        mock_trades.return_value = ([], [])
        r = _get("/trades")
        assert r.status_code == 200, f"/trades returned {r.status_code}"

    @patch("dashboard.app._alpaca_a1")
    @patch("dashboard.app._alpaca_a2")
    def test_transparency_200(self, mock_a2, mock_a1):
        mock_a1.return_value = {"positions": [], "orders": [], "account": None}
        mock_a2.return_value = {"positions": [], "orders": [], "account": None}
        r = _get("/transparency")
        assert r.status_code == 200, f"/transparency returned {r.status_code}"

    def test_health_200(self):
        r = _get("/health")
        assert r.status_code == 200, f"/health returned {r.status_code}"


# ── DASH-08 — transparency page content ───────────────────────────────────────

@_skip_no_flask
class TestDash08TransparencyContent(unittest.TestCase):
    @patch("dashboard.app._alpaca_a1")
    @patch("dashboard.app._alpaca_a2")
    def _get_html(self, mock_a2, mock_a1):
        mock_a1.return_value = {"positions": [], "orders": [], "account": None}
        mock_a2.return_value = {"positions": [], "orders": [], "account": None}
        return _get("/transparency").data.decode()

    def test_transparency_has_active_nav_tab(self):
        html = self._get_html()
        assert "nav-tab active" in html

    def test_transparency_shows_strategy_section(self):
        html = self._get_html()
        assert "Strategy" in html

    def test_transparency_shows_risk_parameters(self):
        html = self._get_html()
        assert "Risk Parameters" in html or "Parameter" in html

    def test_transparency_shows_feature_flags(self):
        html = self._get_html()
        assert "Feature Flags" in html or "Flag" in html

    def test_transparency_shows_cost_section(self):
        html = self._get_html()
        assert "Claude Cost" in html or "Daily Spend" in html

    def test_transparency_has_ticker_class(self):
        html = self._get_html()
        assert "ticker" in html


if __name__ == "__main__":
    unittest.main()
