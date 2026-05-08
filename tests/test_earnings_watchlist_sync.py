"""Tests for _sync_earnings_watchlist() in scheduler.py."""
import json
import sys  # noqa: F401 (used in _load_sync via sys.modules)

# ── helpers ──────────────────────────────────────────────────────────────────

def _make_wl(symbols: list[dict], reset_at: str = "2026-04-16T21:00:00") -> dict:
    return {"symbols": symbols, "reset_at": reset_at}


def _make_conviction(sym: str, eda: int, conviction: str = "high") -> dict:
    return {"symbol": sym, "eda": eda, "conviction_level": conviction}


def _load_sync(tmp_path, monkeypatch):
    """Import _sync_earnings_watchlist with CWD patched to tmp_path."""
    monkeypatch.chdir(tmp_path)
    # Force reimport so Path("watchlist_dynamic.json") resolves in tmp_path
    if "scheduler" in sys.modules:
        del sys.modules["scheduler"]
    # Patch heavy imports that scheduler pulls in at module level
    for mod in ("bot", "report", "weekly_review", "log_setup"):
        sys.modules.setdefault(mod, type(sys)("stub"))
    import log_setup as ls_stub
    ls_stub.get_logger = lambda name: __import__("logging").getLogger(name)

    import scheduler as sched
    return sched._sync_earnings_watchlist


# ── test cases ────────────────────────────────────────────────────────────────

class TestSyncEarningsWatchlist:
    """Unit tests for _sync_earnings_watchlist()."""

    def _run(self, tmp_path, monkeypatch, initial_wl: dict, convictions: list[dict]) -> dict:
        (tmp_path / "watchlist_dynamic.json").write_text(json.dumps(initial_wl))
        fn = _load_sync(tmp_path, monkeypatch)
        fn(convictions)
        return json.loads((tmp_path / "watchlist_dynamic.json").read_text())

    # a) eda=3, conviction=high → added, tier=earnings
    def test_add_high_conviction(self, tmp_path, monkeypatch):
        result = self._run(
            tmp_path, monkeypatch,
            initial_wl=_make_wl([]),
            convictions=[_make_conviction("AMAT", 3, "high")],
        )
        syms = {e["symbol"]: e for e in result["symbols"]}
        assert "AMAT" in syms
        assert syms["AMAT"]["tier"] == "earnings"

    # b) eda=3, conviction=low → NOT added
    def test_skip_low_conviction(self, tmp_path, monkeypatch):
        result = self._run(
            tmp_path, monkeypatch,
            initial_wl=_make_wl([]),
            convictions=[_make_conviction("AMAT", 3, "low")],
        )
        assert result["symbols"] == []

    # c) eda=3, conviction=medium → added
    def test_add_medium_conviction(self, tmp_path, monkeypatch):
        result = self._run(
            tmp_path, monkeypatch,
            initial_wl=_make_wl([]),
            convictions=[_make_conviction("UPST", 3, "medium")],
        )
        syms = {e["symbol"] for e in result["symbols"]}
        assert "UPST" in syms

    # d) eda=-4, tier=earnings → removed
    def test_remove_expired_earnings_entry(self, tmp_path, monkeypatch):
        initial = _make_wl([{"symbol": "AMAT", "sector": "technology",
                              "type": "stock", "tier": "earnings"}])
        result = self._run(
            tmp_path, monkeypatch,
            initial_wl=initial,
            convictions=[_make_conviction("AMAT", -4, "high")],
        )
        syms = {e["symbol"] for e in result["symbols"]}
        assert "AMAT" not in syms

    # e) eda=-4, tier=dynamic (manual) → NOT removed
    def test_keep_manual_entry_post_earnings(self, tmp_path, monkeypatch):
        initial = _make_wl([{"symbol": "CRWD", "sector": "technology",
                              "type": "stock", "tier": "dynamic"}])
        result = self._run(
            tmp_path, monkeypatch,
            initial_wl=initial,
            convictions=[_make_conviction("CRWD", -4, "high")],
        )
        syms = {e["symbol"] for e in result["symbols"]}
        assert "CRWD" in syms

    # f) symbol already in watchlist → not duplicated
    def test_no_duplicate_if_already_present(self, tmp_path, monkeypatch):
        initial = _make_wl([{"symbol": "AMAT", "sector": "technology",
                              "type": "stock", "tier": "earnings"}])
        result = self._run(
            tmp_path, monkeypatch,
            initial_wl=initial,
            convictions=[_make_conviction("AMAT", 5, "high")],
        )
        amat_entries = [e for e in result["symbols"] if e["symbol"] == "AMAT"]
        assert len(amat_entries) == 1

    # g) startup sync: existing convictions → watchlist updated
    def test_startup_sync_populates_watchlist(self, tmp_path, monkeypatch):
        (tmp_path / "watchlist_dynamic.json").write_text(json.dumps(_make_wl([])))
        convictions_data = {
            "generated_at": "2026-05-08T04:00:00+00:00",
            "candidates": [_make_conviction("AMAT", 7, "high")],
        }
        conv_path = tmp_path / "data" / "market"
        conv_path.mkdir(parents=True)
        (conv_path / "earnings_convictions.json").write_text(json.dumps(convictions_data))

        monkeypatch.chdir(tmp_path)
        if "scheduler" in sys.modules:
            del sys.modules["scheduler"]
        for mod in ("bot", "report", "weekly_review", "log_setup"):
            sys.modules.setdefault(mod, type(sys)("stub"))
        import log_setup as ls_stub
        ls_stub.get_logger = lambda name: __import__("logging").getLogger(name)

        import scheduler as sched
        # Reset startup guard so it runs
        sched._earnings_watchlist_startup_synced = False
        sched._maybe_sync_earnings_watchlist_on_startup()

        result = json.loads((tmp_path / "watchlist_dynamic.json").read_text())
        syms = {e["symbol"] for e in result["symbols"]}
        assert "AMAT" in syms

    # h) reset_at preserved after write
    def test_reset_at_preserved(self, tmp_path, monkeypatch):
        original_ts = "2026-04-16T21:27:05.410713-04:00"
        result = self._run(
            tmp_path, monkeypatch,
            initial_wl=_make_wl([], reset_at=original_ts),
            convictions=[_make_conviction("AMAT", 3, "high")],
        )
        assert result["reset_at"] == original_ts

    # i) idempotent — running twice doesn't duplicate
    def test_idempotent(self, tmp_path, monkeypatch):
        (tmp_path / "watchlist_dynamic.json").write_text(json.dumps(_make_wl([])))
        fn = _load_sync(tmp_path, monkeypatch)
        convictions = [_make_conviction("AMAT", 5, "high")]
        fn(convictions)
        fn(convictions)
        result = json.loads((tmp_path / "watchlist_dynamic.json").read_text())
        amat_entries = [e for e in result["symbols"] if e["symbol"] == "AMAT"]
        assert len(amat_entries) == 1

    # j) eda=0 (not 1-7) → NOT added regardless of conviction
    def test_skip_eda_zero(self, tmp_path, monkeypatch):
        result = self._run(
            tmp_path, monkeypatch,
            initial_wl=_make_wl([]),
            convictions=[_make_conviction("DDOG", 0, "high")],
        )
        assert result["symbols"] == []

    # k) no-op when nothing to add or remove → file not rewritten (no crash)
    def test_no_changes_is_noop(self, tmp_path, monkeypatch):
        initial = _make_wl([{"symbol": "CRWD", "sector": "technology",
                              "type": "stock", "tier": "dynamic"}])
        result = self._run(
            tmp_path, monkeypatch,
            initial_wl=initial,
            convictions=[_make_conviction("CRWD", 0, "high")],
        )
        # File unchanged — CRWD still present, nothing added
        assert len(result["symbols"]) == 1
        assert result["symbols"][0]["symbol"] == "CRWD"
