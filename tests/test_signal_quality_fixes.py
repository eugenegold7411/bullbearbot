"""
tests/test_signal_quality_fixes.py — 12 tests for the 5 signal quality fixes.

Fix 1 (2 tests): blocked_symbols in eligibility_check()
Fix 2 (2 tests): bar staleness watermark (data_stale / bar_age_minutes)
Fix 3 (2 tests): symbol_risk_factors.json injected into L3 prompt
Fix 4 (3 tests): sector-to-ticker expansion in macro_wire.classify_articles()
Fix 5 (3 tests): Form 4 transaction_code filter (open-market P only)
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

# ── stubs needed for Alpaca-dependent modules ─────────────────────────────────

def _ensure_alpaca_stubs():
    if "dotenv" not in sys.modules:
        m = types.ModuleType("dotenv")
        m.load_dotenv = lambda *a, **kw: None
        sys.modules["dotenv"] = m
    for mod in (
        "alpaca", "alpaca.trading", "alpaca.trading.client",
        "alpaca.trading.requests", "alpaca.trading.enums",
        "alpaca.data", "alpaca.data.historical", "alpaca.data.requests",
        "alpaca.data.enums",
    ):
        if mod not in sys.modules:
            sys.modules[mod] = types.ModuleType(mod)
    enums = sys.modules["alpaca.trading.enums"]
    for name, attrs in {
        "OrderSide":   {"BUY": "buy", "SELL": "sell"},
        "TimeInForce": {"DAY": "day", "GTC":  "gtc"},
        "OrderClass":  {"BRACKET": "bracket"},
        "QueryOrderStatus": {"OPEN": "open", "ALL": "all"},
    }.items():
        if not hasattr(enums, name):
            cls = type(name, (), {})
            setattr(enums, name, cls)
        cls = getattr(enums, name)
        for attr, val in attrs.items():
            if not hasattr(cls, attr):
                setattr(cls, attr, val)


# ── Fix 1: blocked_symbols ────────────────────────────────────────────────────

class TestBlockedSymbols:
    def _make_idea(self, symbol="QCOM", action="BUY"):
        from schemas import AccountAction, Direction, Tier, TradeIdea
        return TradeIdea(
            symbol=symbol,
            action=AccountAction.BUY if action == "BUY" else AccountAction.SELL,
            tier=Tier.CORE,
            conviction=0.80,
            direction=Direction.BULLISH,
            catalyst="technical_breakout",
            intent="enter_long" if action == "BUY" else "exit_long",
        )

    def _make_snapshot(self):
        from schemas import BrokerSnapshot
        return BrokerSnapshot(positions=[], open_orders=[], equity=100_000.0,
                              cash=100_000.0, buying_power=100_000.0)

    def _config_with_blocked(self, symbols):
        return {"parameters": {"blocked_symbols": symbols}}

    def test_blocked_symbol_rejects_buy(self):
        from risk_kernel import eligibility_check
        idea     = self._make_idea(symbol="QCOM", action="BUY")
        snap     = self._make_snapshot()
        config   = self._config_with_blocked(["QCOM"])
        result   = eligibility_check(idea, snap, config, session_tier="market", vix=20.0)
        assert result is not None, "Expected rejection for blocked symbol QCOM on BUY"
        assert "blocked_symbol" in result.lower(), f"Rejection reason should mention blocked_symbol: {result}"

    def test_blocked_symbol_does_not_block_sell(self):
        from risk_kernel import eligibility_check
        from schemas import AccountAction, Direction, Tier, TradeIdea
        idea = TradeIdea(
            symbol="QCOM",
            action=AccountAction.SELL,
            tier=Tier.CORE,
            conviction=0.80,
            direction=Direction.BEARISH,
            catalyst="exit",
            intent="exit_long",
        )
        snap   = self._make_snapshot()
        config = self._config_with_blocked(["QCOM"])
        result = eligibility_check(idea, snap, config, session_tier="market", vix=20.0)
        assert result is None or "blocked_symbol" not in str(result).lower(), (
            "blocked_symbols must NOT block SELL/exit orders"
        )

    def test_empty_blocked_list_does_not_reject(self):
        from risk_kernel import eligibility_check
        idea   = self._make_idea(symbol="AAPL", action="BUY")
        snap   = self._make_snapshot()
        config = self._config_with_blocked([])
        result = eligibility_check(idea, snap, config, session_tier="market", vix=20.0)
        assert result is None or "blocked_symbol" not in str(result).lower()


# ── Fix 2: bar staleness watermark ────────────────────────────────────────────

class TestBarStalenessWatermark:
    def _md_with_bar_age(self, age_minutes: int) -> dict:
        stale_at = (datetime.now(timezone.utc) - timedelta(minutes=age_minutes)).isoformat()
        return {
            "ind_by_symbol": {
                "AAPL": {"bar_fetched_at": stale_at, "price": 200.0}
            },
            "intraday_summaries": {},
            "current_prices": {"AAPL": 200.0},
        }

    def test_data_stale_true_when_bar_over_60_min_old(self):
        from bot_stage2_python import score_symbol_python
        md     = self._md_with_bar_age(90)
        result = score_symbol_python("AAPL", md, {})
        assert result["data_stale"] is True, (
            f"Expected data_stale=True for 90-min-old bars, got {result['data_stale']}"
        )
        assert result["bar_age_minutes"] >= 89, (
            f"bar_age_minutes should be ~90, got {result['bar_age_minutes']}"
        )

    def test_data_stale_false_when_bar_fresh(self):
        from bot_stage2_python import score_symbol_python
        md     = self._md_with_bar_age(5)
        result = score_symbol_python("AAPL", md, {})
        assert result["data_stale"] is False, (
            f"Expected data_stale=False for 5-min-old bars, got {result['data_stale']}"
        )

    def test_format_l2_injects_data_stale_line(self):
        import bot_stage2_signal as sig
        l2 = {
            "score": 70, "direction": "bullish", "conviction": "high",
            "signals": [], "conflicts": [], "data_stale": True, "bar_age_minutes": 95,
        }
        with patch.object(sig, "_get_macro_wire_hits_for_symbol", return_value=[]), \
             patch.object(sig, "_load_cached_symbol_news", return_value=[]), \
             patch.object(sig, "_load_symbol_risk_factors", return_value={}):
            block = sig._format_l2_for_l3("AAPL", l2, None, 200.0)
        assert "DATA_STALE" in block, f"Expected DATA_STALE in L3 block:\n{block}"
        assert "95" in block, f"Expected age minutes (95) in DATA_STALE line:\n{block}"


# ── Fix 3: symbol risk factors ────────────────────────────────────────────────

class TestSymbolRiskFactors:
    def test_format_l2_injects_symbol_risk_for_known_symbol(self):
        import bot_stage2_signal as sig
        l2 = {
            "score": 84, "direction": "bullish", "conviction": "high",
            "signals": [], "conflicts": [], "data_stale": False, "bar_age_minutes": None,
        }
        risk_data = {
            "QCOM": {
                "china_revenue_pct": 67,
                "export_control_risk": "high",
                "notes": "test note for QCOM",
            }
        }
        with patch.object(sig, "_get_macro_wire_hits_for_symbol", return_value=[]), \
             patch.object(sig, "_load_cached_symbol_news", return_value=[]), \
             patch.object(sig, "_load_symbol_risk_factors", return_value=risk_data):
            block = sig._format_l2_for_l3("QCOM", l2, None, 185.0)
        assert "SYMBOL_RISK" in block, f"Expected SYMBOL_RISK in L3 block:\n{block}"
        assert "67%" in block, f"Expected china_revenue=67% in block:\n{block}"
        assert "HIGH" in block, f"Expected HIGH export_control in block:\n{block}"

    def test_format_l2_no_symbol_risk_for_unknown_symbol(self):
        import bot_stage2_signal as sig
        l2 = {
            "score": 70, "direction": "bullish", "conviction": "high",
            "signals": [], "conflicts": [], "data_stale": False, "bar_age_minutes": None,
        }
        with patch.object(sig, "_get_macro_wire_hits_for_symbol", return_value=[]), \
             patch.object(sig, "_load_cached_symbol_news", return_value=[]), \
             patch.object(sig, "_load_symbol_risk_factors", return_value={}):
            block = sig._format_l2_for_l3("SPY", l2, None, 570.0)
        assert "SYMBOL_RISK" not in block, f"SYMBOL_RISK should not appear for SPY:\n{block}"


# ── Fix 4: sector-to-ticker expansion ────────────────────────────────────────

class TestSectorTickerExpansion:
    def _article(self, keyword_tier="critical", sectors=None, symbols=None):
        return {
            "title":           "Test article",
            "keyword_tier":    keyword_tier,
            "affected_sectors": sectors or [],
            "affected_symbols": symbols or [],
            "is_market_moving": True,
            "direction":       "bearish",
            "urgency":         "immediate",
            "one_line_summary": "Test",
        }

    def _sector_map(self):
        return {
            "semiconductors": ["QCOM", "NVDA", "AMD", "INTC"],
        }

    def test_critical_tier_expands_semiconductor_sector_to_qcom(self):
        import macro_wire as mw
        article = self._article(keyword_tier="critical", sectors=["semiconductors"])
        with patch.object(mw, "_load_sector_ticker_map", return_value=self._sector_map()):
            mw._apply_sector_expansion([article])
        assert "QCOM" in article["affected_symbols"], (
            f"QCOM should be in affected_symbols after sector expansion: {article['affected_symbols']}"
        )

    def test_medium_tier_skips_sector_expansion(self):
        import macro_wire as mw
        article = self._article(keyword_tier="medium", sectors=["semiconductors"])
        original_symbols = list(article["affected_symbols"])
        with patch.object(mw, "_load_sector_ticker_map", return_value=self._sector_map()):
            mw._apply_sector_expansion([article])
        assert "QCOM" not in article["affected_symbols"], (
            "QCOM should NOT be added for medium-tier articles"
        )
        assert article["affected_symbols"] == original_symbols

    def test_sector_expansion_deduplicates_symbols(self):
        import macro_wire as mw
        article = self._article(
            keyword_tier="critical",
            sectors=["semiconductors"],
            symbols=["NVDA"],  # already present
        )
        with patch.object(mw, "_load_sector_ticker_map", return_value=self._sector_map()):
            mw._apply_sector_expansion([article])
        nvda_count = article["affected_symbols"].count("NVDA")
        assert nvda_count == 1, f"NVDA should appear exactly once, got {nvda_count}: {article['affected_symbols']}"


# ── Fix 5: Form 4 transaction_code filter ────────────────────────────────────

class TestForm4TransactionCodeFilter:
    def _run_cache_with_events(self, form4_events):
        """Run _prepare_cycle_cache() with Form 4 events injected via sys.modules."""
        import bot_stage2_python as s2p
        s2p._CYCLE_CACHE.clear()

        # Build stub for insider_intelligence since it's lazy-imported
        mock_insider = types.ModuleType("insider_intelligence")
        mock_insider.fetch_form4_insider_trades = lambda syms, days_back=2: form4_events
        mock_insider.fetch_congressional_trades = lambda syms, days_back=2: []

        mock_wl = types.ModuleType("watchlist_manager")
        mock_wl.get_active_watchlist = lambda: {
            "all": [{"symbol": "QCOM"}, {"symbol": "TSM"}]
        }

        with patch.dict(sys.modules, {
            "insider_intelligence": mock_insider,
            "watchlist_manager": mock_wl,
        }):
            s2p._prepare_cycle_cache()
        return dict(s2p._CYCLE_CACHE.get("insider_evt", {}))

    def test_award_grant_code_A_does_not_score(self):
        events = [{"ticker": "QCOM", "transaction_code": "A"}]
        counts = self._run_cache_with_events(events)
        assert counts.get("QCOM", 0) == 0, (
            f"Award/grant (code A) should not increment insider_evt, got {counts}"
        )

    def test_option_exercise_code_M_does_not_score(self):
        events = [{"ticker": "QCOM", "transaction_code": "M"}]
        counts = self._run_cache_with_events(events)
        assert counts.get("QCOM", 0) == 0, (
            f"Option exercise (code M) should not increment insider_evt, got {counts}"
        )

    def test_open_market_purchase_code_P_scores(self):
        events = [{"ticker": "TSM", "transaction_code": "P"}]
        counts = self._run_cache_with_events(events)
        assert counts.get("TSM", 0) == 1, (
            f"Open-market purchase (code P) should increment insider_evt to 1, got {counts}"
        )

    def test_insider_purchase_label_in_signal_output(self):
        """score_symbol_python emits 'insider_purchase_48h' (not 'insider_activity_48h')."""
        import bot_stage2_python as s2p
        s2p._CYCLE_CACHE["insider_evt"] = {"AAPL": 1}
        s2p._CYCLE_CACHE["earnings_map"] = {}
        s2p._CYCLE_CACHE["morning_brief"] = {}
        s2p._CYCLE_CACHE["orb_by_sym"] = {}
        s2p._CYCLE_CACHE["pattern_wl"] = {}
        md = {
            "ind_by_symbol":    {"AAPL": {"price": 200.0}},
            "intraday_summaries": {},
            "current_prices":   {"AAPL": 200.0},
        }
        result = s2p.score_symbol_python("AAPL", md, {})
        assert "insider_purchase_48h" in result["signals"], (
            f"Expected 'insider_purchase_48h' in signals, got {result['signals']}"
        )
        assert "insider_activity_48h" not in result["signals"], (
            "Old label 'insider_activity_48h' should not appear"
        )
