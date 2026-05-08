"""
tests/test_earnings_layer.py — Earnings layer fixes: Issues 21, 22, 23.

Issue 21 — A1 hard rotation gate (bot_stage3_decision._eda_for_positions + prompt injection):
  1. test_eda_for_positions_returns_near_earnings
  2. test_eda_for_positions_ignores_far_earnings
  3. test_eda_for_positions_ignores_short_positions
  4. test_build_user_prompt_injects_earnings_rule
  5. test_build_user_prompt_no_injection_when_clear

Issue 22 — Straddle direction override (bot_options_stage2_structures.RULE_STRADDLE_STRANGLE):
  6. test_straddle_fires_neutral_without_override
  7. test_straddle_fires_directional_with_override_and_low_eda
  8. test_straddle_blocked_directional_without_override

Issue 23 — Sparse-coverage detection (health_monitor + data_warehouse):
  9.  test_health_calendar_sparse_warning
  10. test_health_calendar_ok_when_sufficient_upcoming
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

# ── Shared helpers ────────────────────────────────────────────────────────────

def _future_iso(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def _make_position(symbol: str, qty: float) -> MagicMock:
    p = MagicMock()
    p.symbol = symbol
    p.qty = qty
    return p


def _mock_cal_path(entries: list[dict]) -> MagicMock:
    """Return a mock Path object for earnings_calendar.json with the given entries."""
    cal_data = json.dumps({"fetched_at": "2026-01-01", "calendar": entries})
    mp = MagicMock()
    mp.exists.return_value = True
    mp.read_text.return_value = cal_data
    return mp


def _patch_cal_path(mock_path: MagicMock):
    """
    Context manager that patches bot_stage3_decision.Path so that
    Path(__file__).parent / "data" / "market" / "earnings_calendar.json"
    resolves to mock_path.
    """
    return patch(
        "bot_stage3_decision.Path",
        side_effect=lambda *a: _ChainMock(mock_path),
    )


class _ChainMock:
    """Thin wrapper: all / operations return self so the final object is mock_path."""
    def __init__(self, terminal):
        self._t = terminal

    @property
    def parent(self):
        return self

    def __truediv__(self, _):
        return self

    def exists(self):
        return self._t.exists()

    def read_text(self):
        return self._t.read_text()


# ── Tests 1–3: _eda_for_positions ────────────────────────────────────────────

def test_eda_for_positions_returns_near_earnings():
    """Held long position with earnings in 2 days → returned with eda=2."""
    import bot_stage3_decision as bsd
    entries = [{"symbol": "NVDA", "earnings_date": _future_iso(2)}]
    pos = [_make_position("NVDA", 10.0)]
    mp = _mock_cal_path(entries)
    with _patch_cal_path(mp):
        result = bsd._eda_for_positions(pos)
    assert len(result) == 1
    sym, eda = result[0]
    assert sym == "NVDA"
    assert eda == 2


def test_eda_for_positions_ignores_far_earnings():
    """Held long position with earnings 10 days away → not returned (>3)."""
    import bot_stage3_decision as bsd
    entries = [{"symbol": "AAPL", "earnings_date": _future_iso(10)}]
    pos = [_make_position("AAPL", 5.0)]
    mp = _mock_cal_path(entries)
    with _patch_cal_path(mp):
        result = bsd._eda_for_positions(pos)
    assert result == [], f"Expected no alerts for eda=10, got {result}"


def test_eda_for_positions_ignores_short_positions():
    """Short position (qty < 0) near earnings → not returned."""
    import bot_stage3_decision as bsd
    entries = [{"symbol": "TSLA", "earnings_date": _future_iso(1)}]
    pos = [_make_position("TSLA", -3.0)]
    mp = _mock_cal_path(entries)
    with _patch_cal_path(mp):
        result = bsd._eda_for_positions(pos)
    assert result == [], f"Short positions must be ignored, got {result}"


# ── Tests 4–5: build_user_prompt earnings injection ──────────────────────────

_TEMPLATE = (
    "tpl {session_tier}{session_instruments}{next_cycle_time}"
    "{equity}{cash}{buying_power}{available_for_new}"
    "{pdt_used}{pdt_remaining}{exposure_pct}{vix}{vix_regime}"
    "{regime_instruction}{market_status}{time_et}{minutes_since_open}"
    "{sector_table}{intermarket_signals}{global_handoff}{earnings_calendar}"
    "{core_watchlist_by_sector}{dynamic_watchlist}{intraday_watchlist}"
    "{positions_table}{recent_decisions}{vector_memories}{ticker_lessons}"
    "{crypto_signals}{crypto_context}{breaking_news}{sector_news}"
    "{strategy_config_note}{conviction_table}{regime_line}{positions_line}"
    "{avoid_line}{insider_section}{reddit_section}{earnings_intel_section}"
    "{economic_calendar_section}{macro_wire_section}{orb_section}"
    "{regime_summary}{signal_scores}{intraday_momentum}{exit_status}"
    "{macro_backdrop}{dynamic_sizes_section}{positions_with_health}"
    "{correlation_section}{thesis_ranking_section}{scratchpad_section}"
)


def _minimal_account():
    a = MagicMock()
    a.equity = "50000.00"
    a.cash = "20000.00"
    a.buying_power = "20000.00"
    a.daytrade_count = 0
    return a


def _minimal_md():
    return {
        "market_status": "open", "time_et": "10:00 AM ET",
        "minutes_since_open": 30, "vix": 20.0, "vix_regime": "normal",
        "regime_instruction": "Standard rules apply.",
    }


def test_build_user_prompt_injects_earnings_rule():
    """EARNINGS RULE block must appear when _eda_for_positions returns alerts."""
    import bot_stage3_decision as bsd
    pos = [_make_position("NVDA", 10.0)]
    with (
        patch("bot_stage3_decision._eda_for_positions", return_value=[("NVDA", 1)]),
        patch("bot_stage3_decision._load_prompts", return_value=("sys", _TEMPLATE)),
        patch("bot_stage3_decision.pi"),
    ):
        result = bsd.build_user_prompt(
            account=_minimal_account(), positions=pos, md=_minimal_md(),
            session_tier="regular", session_instruments="equity",
            recent_decisions="", ticker_lessons="",
        )
    assert "EARNINGS RULE" in result, "EARNINGS RULE block must be injected"
    assert "NVDA" in result
    assert "1 day" in result


def test_build_user_prompt_no_injection_when_clear():
    """EARNINGS RULE block must NOT appear when _eda_for_positions returns []."""
    import bot_stage3_decision as bsd
    pos = [_make_position("AAPL", 5.0)]
    with (
        patch("bot_stage3_decision._eda_for_positions", return_value=[]),
        patch("bot_stage3_decision._load_prompts", return_value=("sys", _TEMPLATE)),
        patch("bot_stage3_decision.pi"),
    ):
        result = bsd.build_user_prompt(
            account=_minimal_account(), positions=pos, md=_minimal_md(),
            session_tier="regular", session_instruments="equity",
            recent_decisions="", ticker_lessons="",
        )
    assert "EARNINGS RULE" not in result


# ── Tests 6–8: RULE_STRADDLE_STRANGLE direction override ─────────────────────

def _make_pack(iv_rank: float = 30.0, direction: str = "bullish",
               earnings_days_away: int = 8) -> MagicMock:
    pack = MagicMock()
    pack.iv_rank            = iv_rank
    pack.a1_direction       = direction
    pack.iv_environment     = "normal"
    pack.a1_signal_score    = 65.0
    pack.symbol             = "NVDA"
    pack.iv_percentile      = iv_rank
    pack.iv_skew            = None   # disable skew override
    pack.dte                = 30
    pack.liquidity_score    = 1.0    # above the 0.3 floor
    pack.earnings_days_away = earnings_days_away
    pack.macro_event_flag   = False  # skip RULE4 macro routing
    pack.vix_regime         = "normal"
    return pack


def _route(pack, rcfg_override: dict, eda: int):
    from bot_options_stage2_structures import _route_strategy
    config = {"a2_router": rcfg_override}
    # earnings_days_away is read from pack directly; calendar arg is used for
    # RULE_POST_EARNINGS timing only — pass empty to avoid that path
    return _route_strategy(pack, config=config, earnings_calendar_data={"calendar": []})


def test_straddle_fires_neutral_without_override():
    """Neutral direction → straddle/strangle regardless of override flag."""
    pack = _make_pack(direction="neutral", iv_rank=30.0)
    result = _route(pack, {"straddle_earnings_direction_override": False}, eda=8)
    assert result is not None and any(s in (result or "") for s in ("straddle", "strangle")), (
        f"Neutral dir should always route to straddle/strangle, got {result!r}"
    )


def test_straddle_fires_directional_with_override_and_low_eda():
    """Bullish + override=True + eda≤10 + iv_rank<45 → straddle/strangle allowed."""
    pack = _make_pack(direction="bullish", iv_rank=30.0)
    result = _route(pack, {"straddle_earnings_direction_override": True}, eda=8)
    assert result is not None and any(s in (result or "") for s in ("straddle", "strangle")), (
        f"Bullish + override=True + low eda should route to straddle/strangle, got {result!r}"
    )


def test_straddle_blocked_directional_without_override():
    """Bullish + override=False → RULE_STRADDLE_STRANGLE does NOT fire."""
    pack = _make_pack(direction="bullish", iv_rank=30.0)
    result = _route(pack, {"straddle_earnings_direction_override": False}, eda=8)
    assert result not in ("straddle", "strangle"), (
        f"Bullish + override=False must NOT route to straddle/strangle, got {result!r}"
    )


# ── Tests 9–10: Issue 23 — health_monitor sparse check ───────────────────────

def _make_health_mock_path(entries: list[dict], refresh_iso: str) -> MagicMock:
    cal_data = {
        "fetched_at":           datetime.now(timezone.utc).isoformat(),
        "last_daily_refresh_at": refresh_iso,
        "calendar":             entries,
    }
    mp = MagicMock()
    mp.exists.return_value = True
    mp.stat.return_value.st_mtime = datetime.now(timezone.utc).timestamp()
    mp.read_text.return_value = json.dumps(cal_data)
    return mp


def _now_et():
    return datetime.now(ZoneInfo("America/New_York"))


def test_health_calendar_sparse_warning():
    """When calendar has <5 upcoming entries, health check must return WARNING."""
    import health_monitor as hm

    now_et = _now_et()
    # Full ISO timestamp — date-only would be parsed as midnight UTC and read
    # as ~27h stale on any CI run after early evening ET.
    refresh_iso = datetime.now(timezone.utc).isoformat()
    entries = [{"symbol": "NVDA", "earnings_date": _future_iso(5)}]  # only 1
    mp = _make_health_mock_path(entries, refresh_iso)

    with patch.object(hm, "_DATA_DIR", MagicMock()) as md:
        md.__truediv__.return_value.__truediv__.return_value = mp
        result = hm._check_earnings_calendar(now_et)

    assert not result.ok and result.severity == "WARNING", (
        f"Sparse calendar should be WARNING, got ok={result.ok} sev={result.severity} "
        f"msg={result.message}"
    )
    assert "sparse" in result.message.lower()


def test_health_calendar_ok_when_sufficient_upcoming():
    """When calendar has ≥5 upcoming entries refreshed today, health check must be OK."""
    import health_monitor as hm

    now_et = _now_et()
    # Full ISO timestamp — date-only would be parsed as midnight UTC and read
    # as ~27h stale on any CI run after early evening ET.
    refresh_iso = datetime.now(timezone.utc).isoformat()
    entries = [
        {"symbol": s, "earnings_date": _future_iso(i + 2)}
        for i, s in enumerate(["NVDA", "AAPL", "MSFT", "GOOG", "AMZN", "META"])
    ]
    mp = _make_health_mock_path(entries, refresh_iso)

    with patch.object(hm, "_DATA_DIR", MagicMock()) as md:
        md.__truediv__.return_value.__truediv__.return_value = mp
        result = hm._check_earnings_calendar(now_et)

    assert result.ok and result.severity == "OK", (
        f"Sufficient upcoming entries should be OK, got ok={result.ok} "
        f"sev={result.severity} msg={result.message}"
    )
