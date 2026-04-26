"""
Tests for macro wire scoring fixes:
- Word-boundary matching prevents substring false positives
- Trigger gate requires score+tier combination OR critical+watchlist symbol hit
- _watchlist_symbols() cache helper works correctly
"""


def _score(headline: str, summary: str = "") -> tuple:
    from macro_wire import _score_article
    return _score_article(headline, summary)


class TestWordBoundaryMatching:
    def test_war_does_not_match_warsh(self):
        """'war' keyword must not fire on 'Warsh'."""
        score, _tier, kws = _score("Who is Kevin Warsh and his policy")
        assert "war" not in kws, f"'Warsh' incorrectly matched 'war' (kws={kws})"
        assert score < 4.0, f"'Warsh' incorrectly scored as critical, score={score}"

    def test_war_does_not_match_warehouse(self):
        """'war' keyword must not fire on 'warehouse'."""
        score, _tier, kws = _score("Amazon warehouse workers strike")
        assert "war" not in kws, f"'warehouse' incorrectly matched 'war' (kws={kws})"
        assert score < 4.0, f"'warehouse' incorrectly scored as critical, score={score}"

    def test_war_does_not_match_warns(self):
        """'war' keyword must not fire on 'warns'."""
        score, _tier, kws = _score("Howard Marks warns about market complacency")
        assert "war" not in kws, f"'warns' incorrectly matched 'war' (kws={kws})"

    def test_war_matches_war(self):
        """'war' keyword must fire on actual 'war'."""
        score, tier, kws = _score("Russia declares war on NATO member state")
        assert "war" in kws, f"'war' keyword failed to match (kws={kws})"
        assert tier == "critical"
        assert score >= 4.0, f"'war' failed to score, score={score}"

    def test_fed_does_not_match_fedex(self):
        """'Fed' must not match 'FedEx'."""
        score, _tier, kws = _score("FedEx earnings beat expectations")
        assert "Fed" not in kws, f"'FedEx' incorrectly matched 'Fed' (kws={kws})"

    def test_fed_matches_federal_reserve(self):
        """'Fed' must match standalone 'Fed'."""
        score, tier, kws = _score("Fed raises rates 50bps in emergency meeting")
        assert "Fed" in kws, f"'Fed' failed to match (kws={kws})"
        assert tier == "critical"
        assert score >= 4.0, f"'Fed' failed to score, score={score}"

    def test_qe_does_not_match_marqeta(self):
        """'QE' must not match 'Marqeta'."""
        score, _tier, kws = _score("Marqeta shares fall after earnings miss")
        assert "QE" not in kws, f"'Marqeta' incorrectly matched 'QE' (kws={kws})"

    def test_genuine_high_score_event(self):
        """Genuine macro crisis headline must score >= 8."""
        score, tier, _kws = _score(
            "Fed announces emergency rate cut amid banking contagion fears",
            "Federal Reserve moves to stabilize financial system",
        )
        assert tier == "critical"
        assert score >= 8.0, f"Genuine crisis headline scored too low: {score}"

    def test_multi_word_phrase_still_substring(self):
        """Multi-word phrases (e.g. 'rate cut') still match via substring."""
        _score_val, _tier, kws = _score("Fed unveils surprise rate cut decision")
        assert "rate cut" in kws


class TestTriggerCondition:
    def test_high_score_critical_tier_triggers(self):
        from macro_wire import _should_trigger_cycle
        assert _should_trigger_cycle(score=8.5, tier="critical", affected_symbols=[]) is True

    def test_high_score_high_tier_triggers(self):
        from macro_wire import _should_trigger_cycle
        assert _should_trigger_cycle(score=8.0, tier="high", affected_symbols=[]) is True

    def test_low_score_critical_tier_no_watchlist_does_not_trigger(self):
        """score < 8 AND tier=critical with no watchlist hit must NOT trigger."""
        from macro_wire import _should_trigger_cycle
        assert _should_trigger_cycle(score=4.0, tier="critical", affected_symbols=[]) is False

    def test_low_score_critical_tier_with_watchlist_triggers(self):
        """score < 8 AND tier=critical WITH watchlist symbol DOES trigger."""
        import macro_wire as mw
        original = mw._watchlist_symbols
        mw._watchlist_symbols = lambda: {"NVDA", "AAPL", "GOOGL"}
        try:
            assert mw._should_trigger_cycle(
                score=4.0, tier="critical", affected_symbols=["NVDA"]
            ) is True
        finally:
            mw._watchlist_symbols = original

    def test_critical_tier_offsymbol_does_not_trigger(self):
        """score < 8, tier=critical, symbols outside watchlist must NOT trigger."""
        import macro_wire as mw
        original = mw._watchlist_symbols
        mw._watchlist_symbols = lambda: {"NVDA", "AAPL"}
        try:
            assert mw._should_trigger_cycle(
                score=5.0, tier="critical", affected_symbols=["SLB", "DXY"]
            ) is False
        finally:
            mw._watchlist_symbols = original

    def test_high_score_low_tier_does_not_trigger(self):
        """score >= 8 but tier=low/medium must NOT trigger."""
        from macro_wire import _should_trigger_cycle
        assert _should_trigger_cycle(score=9.0, tier="low", affected_symbols=[]) is False
        assert _should_trigger_cycle(score=9.0, tier="medium", affected_symbols=[]) is False

    def test_benign_critical_tier_does_not_trigger(self):
        """Benign headline scoring 4.0 critical with no symbols must not trigger."""
        from macro_wire import _should_trigger_cycle
        assert _should_trigger_cycle(score=4.0, tier="critical", affected_symbols=[]) is False


class TestWatchlistCache:
    def test_watchlist_symbols_returns_set(self):
        import macro_wire as mw
        from macro_wire import _watchlist_symbols
        # Reset cache to force a fresh load
        mw._wl_symbols_ts = 0.0
        mw._wl_symbols_cache = set()
        syms = _watchlist_symbols()
        assert isinstance(syms, set)
        assert len(syms) > 0
        assert all(isinstance(s, str) for s in syms)

    def test_watchlist_symbols_excludes_crypto(self):
        """BTC/USD and ETH/USD must not appear in the symbol set."""
        import macro_wire as mw
        mw._wl_symbols_ts = 0.0
        mw._wl_symbols_cache = set()
        syms = mw._watchlist_symbols()
        assert "BTC/USD" not in syms
        assert "ETH/USD" not in syms

    def test_watchlist_cache_hits_on_second_call(self):
        """Second call within TTL does not refresh the timestamp."""
        import macro_wire as mw
        mw._wl_symbols_ts = 0.0
        mw._wl_symbols_cache = set()
        mw._watchlist_symbols()
        ts1 = mw._wl_symbols_ts
        mw._watchlist_symbols()
        ts2 = mw._wl_symbols_ts
        assert ts1 == ts2  # cache was reused, not refreshed
