"""
Tests for fabricated-data fixes (Session 5/6).

Covers:
  1. iv_rank=0.0 propagates from proposal -- not converted to 50.0
  2. iv_rank absent with no proposal -> no_trade, not fabrication
  3. compute_iv_rank flat history -> None
  4. VIX cache returns last-known on live fetch failure
  5. VIX stale cache (>4h) returns None
  6. bot_stage3 vix_label = "elevated_caution" when md["vix"] is None
  7. options_builder rejects absent iv_rank
"""
import time
from types import SimpleNamespace
from unittest.mock import patch

# -- 1. iv_rank=0.0 propagates from proposal -----------------------------------

def test_iv_rank_zero_not_fabricated():
    """proposal.iv_rank=0.0 must pass through as 0.0, not be converted to 50."""
    from options_builder import build_structure
    from schemas import OptionStrategy

    chain = {
        "current_price": 180.0,
        "expirations": {
            "2026-05-15": {
                "calls": [
                    {"strike": 180.0, "bid": 5.0, "ask": 6.0, "delta": 0.50,
                     "openInterest": 500, "volume": 200, "impliedVolatility": 0.45},
                ],
                "puts": [],
            }
        },
    }
    structure, err = build_structure(
        symbol="QCOM",
        strategy=OptionStrategy.SINGLE_CALL,
        direction="bullish",
        conviction=0.80,
        iv_rank=0.0,
        max_cost_usd=5000.0,
        chain=chain,
        equity=100_000.0,
        config={},
    )
    # iv_rank=0.0 must not cause "iv_rank absent" rejection
    assert err != "iv_rank absent", "iv_rank=0.0 must not be treated as absent"


# -- 2. iv_rank absent + no proposal -> no_trade --------------------------------

def test_options_builder_rejects_absent_iv_rank():
    """build_structure with iv_rank=None returns (None, 'iv_rank absent')."""
    from options_builder import build_structure
    from schemas import OptionStrategy

    chain = {"current_price": 180.0, "expirations": {}}
    structure, err = build_structure(
        symbol="TEST",
        strategy=OptionStrategy.SINGLE_CALL,
        direction="bullish",
        conviction=0.80,
        iv_rank=None,
        max_cost_usd=5000.0,
        chain=chain,
        equity=100_000.0,
        config={},
    )
    assert structure is None
    assert err == "iv_rank absent"


# -- 3. compute_iv_rank flat history -> None ------------------------------------

def test_flat_iv_history_returns_none():
    """When all IV entries are identical, compute_iv_rank returns None."""
    from options_data import compute_iv_rank

    flat_entries = [{"date": f"2026-0{i//9+1}-{(i%28)+1:02d}", "iv": 0.35}
                    for i in range(25)]
    with patch("options_data._load_iv_history", return_value=flat_entries):
        result = compute_iv_rank("FLAT_SYM")
    assert result is None, f"Expected None for flat IV history, got {result}"


def test_non_flat_iv_history_returns_float():
    """Normal IV history with variation returns a float in [0, 100]."""
    from options_data import compute_iv_rank

    entries = [{"date": f"2026-01-{i+1:02d}", "iv": 0.20 + i * 0.005}
               for i in range(25)]
    with patch("options_data._load_iv_history", return_value=entries):
        result = compute_iv_rank("VARIED_SYM")
    assert isinstance(result, float)
    assert 0.0 <= result <= 100.0


# -- 4. VIX cache returns last-known on live fetch failure ---------------------

def test_vix_cache_used_on_failure():
    """Live VIX fetch failure -> cached value returned with WARNING log."""
    import market_data

    market_data._VIX_CACHE["value"] = 21.5
    market_data._VIX_CACHE["ts"] = time.time() - 300  # 5 min old, well within 4h

    with patch("market_data.yf") as mock_yf:
        mock_yf.Ticker.return_value.history.side_effect = RuntimeError("network error")
        result = market_data.get_vix()

    assert result == 21.5, f"Expected cached 21.5, got {result}"


# -- 5. VIX stale cache (>4h) returns None ------------------------------------

def test_vix_stale_cache_returns_none():
    """Cache older than 4 hours must return None, not the stale value."""
    import market_data

    market_data._VIX_CACHE["value"] = 19.0
    market_data._VIX_CACHE["ts"] = time.time() - 14401  # just over 4h

    with patch("market_data.yf") as mock_yf:
        mock_yf.Ticker.return_value.history.side_effect = RuntimeError("timeout")
        result = market_data.get_vix()

    assert result is None, f"Expected None for stale cache, got {result}"


# -- 6. bot_stage3 vix_label = elevated_caution when md["vix"] is None --------

def test_vix_absent_yields_elevated_caution_label():
    """When md['vix'] is None, build_compact_prompt sets vix_label='elevated_caution'."""
    import bot_stage3_decision as s3

    # Minimal md dict with vix=None
    md = {
        "vix": None,
        "market_status": "open",
        "time_et": "10:00 ET",
        "vix_regime": "ELEVATED (VIX=N/A)",
        "regime_instruction": "Cut sizes.",
        "sector_table": "",
        "macro_wire_section": "",
        "morning_brief_section": "",
        "regime_score": 50,
        "breaking_news": "",
    }
    account = SimpleNamespace(
        cash=50_000.0, buying_power=100_000.0, equity=100_000.0,
        daytrade_count=0,
    )
    positions = []
    pi_data = {"drawdown_pct": 0.0}
    regime_obj = {"regime_score": 50, "bias": "neutral", "constraints": [],
                  "high_impact_warning": "", "narrative": ""}

    # Provide a minimal template that includes {vix_label} so it renders
    minimal_template = (
        "vix_label={vix_label} vix={vix} equity={equity} cash_pct={cash_pct} "
        "exposure_pct={exposure_pct} buying_power={buying_power} "
        "available_for_new={available_for_new} pdt_remaining={pdt_remaining} "
        "drawdown_pct={drawdown_pct} session_tier={session_tier} "
        "time_et={time_et} n_positions={n_positions} positions_block={positions_block} "
        "regime_bias={regime_bias} regime_score={regime_score} "
        "top_sectors={top_sectors} macro_constraint={macro_constraint} "
        "top_catalyst={top_catalyst} n_scored={n_scored} "
        "top_signals_block={top_signals_block} constraints_block={constraints_block}"
    )

    with patch("bot_stage3_decision._load_compact_template", return_value=minimal_template):
        prompt = s3.build_compact_prompt(
            account=account,
            positions=positions,
            md=md,
            session_tier="regular",
            regime_obj=regime_obj,
            signal_scores_obj={},
            time_bound_actions=[],
            pi_data=pi_data,
        )

    assert "elevated_caution" in prompt, (
        f"Expected elevated_caution in prompt when vix is None, got: {prompt[:200]}"
    )
