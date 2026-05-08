"""QW4 / #54 — dashboard deployed_pct uses long_market_value / portfolio_value."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DASH_DIR     = _PROJECT_ROOT / "dashboard"
if str(_DASH_DIR) not in sys.path:
    sys.path.insert(0, str(_DASH_DIR))
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

flask = pytest.importorskip("flask")  # dashboard requires Flask; skip cleanly if absent

import app as dashboard_app  # noqa: E402


def _make_account(*, equity, lmv, cash, bp, pv=None):
    acct = MagicMock()
    acct.equity            = equity
    acct.long_market_value = lmv
    acct.cash              = cash
    acct.buying_power      = bp
    acct.portfolio_value   = pv if pv is not None else equity
    return acct


def test_deployed_pct_uses_long_market_value(monkeypatch):
    """deployed_pct = long_market_value / portfolio_value * 100."""
    a1_acc = _make_account(equity=100_000, lmv=50_000, cash=50_000, bp=50_000)
    a2_acc = _make_account(equity=100_000, lmv=20_000, cash=80_000, bp=80_000)

    monkeypatch.setattr(dashboard_app, "_build_status", lambda: {
        "a1": {"account": a1_acc},
        "a2": {"account": a2_acc},
    })

    with dashboard_app.app.test_client() as client:
        resp = client.get("/api/account")
        assert resp.status_code == 200
        data = resp.get_json()

    assert data["a1"]["deployed_pct"] == 50.0
    assert data["a2"]["deployed_pct"] == 20.0
    assert data["a1"]["long_market_value"] == 50_000.0
    assert data["a1"]["portfolio_value"]   == 100_000.0
    assert data["a1"]["margin_in_use"] is False


def test_deployed_shows_margin_usage(monkeypatch):
    """Heavy margin → cash<0 sets margin_in_use; deployed_pct can exceed 100%."""
    a1_acc = _make_account(equity=100_000, lmv=180_000, cash=-80_000, bp=500)

    monkeypatch.setattr(dashboard_app, "_build_status", lambda: {
        "a1": {"account": a1_acc},
        "a2": {"account": None},
    })

    with dashboard_app.app.test_client() as client:
        resp = client.get("/api/account")
        assert resp.status_code == 200
        data = resp.get_json()

    assert data["a1"]["deployed_pct"] == 180.0
    assert data["a1"]["margin_in_use"] is True
    assert data["a1"]["cash"] == -80_000.0
    # A2 unavailable surfaces gracefully
    assert "error" in data["a2"]
