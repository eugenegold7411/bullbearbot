"""
tests/test_cr_conviction_reconciliation.py — Conviction Reconciliation (R1/R3/R4)

Covers:
  CR-01: Scratchpad BLOCKING overrides brief HIGH conviction → [AVOID]
  CR-02: Signal score 78 vs brief MEDIUM, signal conviction=high → label diverge → [HIGH upgrade]
  CR-03: Signal direction bearish vs brief bullish → direction flip → USE SIGNAL
  CR-04: Signal consistent with brief → [= consistent] annotation
  CR-05: Symbol in brief but missing from signal scores → [? stale] annotation
  CR-06: Symbol in signal scores but not in brief → [NEW] annotation
  CR-07: Output renders under 600 tokens (~2400 chars)
  CR-08: Sonnet prompt template uses {conviction_table} not {conviction_state}
  CR-09: Missing morning_brief_sonnet.json falls back gracefully — no crash
  CR-10: Missing scratchpad falls back gracefully — brief+signal only
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))
os.chdir(_BOT_DIR)

_THIRD_PARTY_STUBS = {
    "dotenv":                  None,
    "anthropic":               None,
    "alpaca":                  None,
    "alpaca.trading":          None,
    "alpaca.trading.client":   None,
    "alpaca.trading.requests": None,
    "alpaca.trading.enums":    None,
    "chromadb":                None,
    "twilio":                  None,
    "twilio.rest":             None,
    "sendgrid":                None,
    "sendgrid.helpers":        None,
    "sendgrid.helpers.mail":   None,
    "yfinance":                None,
    "requests":                None,
}
for _stub_name in _THIRD_PARTY_STUBS:
    if _stub_name not in sys.modules:
        sys.modules[_stub_name] = mock.MagicMock()


def _make_full_brief(longs=None, bears=None, avoids=None):
    return {
        "high_conviction_longs":    longs or [],
        "high_conviction_bearish":  bears or [],
        "avoid_list":               avoids or [],
    }


def _make_sonnet_brief(avoid_line="AVOID: none", regime_line="", positions_line=""):
    return {
        "generated_at":  "2026-04-30T10:00:00",
        "brief_type":    "market_open",
        "conviction_state": "",
        "regime_line":   regime_line,
        "positions_line": positions_line,
        "avoid_line":    avoid_line,
    }


def _make_sig_syms(entries):
    """entries: list of (symbol, score, direction, conviction, catalyst)"""
    scored = {}
    for sym, score, direction, conviction, catalyst in entries:
        scored[sym] = {
            "score": score,
            "direction": direction,
            "conviction": conviction,
            "primary_catalyst": catalyst,
        }
    return {"scored_symbols": scored}


def _make_scratchpad(watching=None, blocking=None, conviction_ranking=None):
    return {
        "watching":           watching or [],
        "blocking":           blocking or [],
        "conviction_ranking": conviction_ranking or [],
        "summary":            "test scratchpad",
    }


class TestConvictionReconciliation(unittest.TestCase):

    def setUp(self):
        # Import with full brief patched to return empty by default; individual tests override
        from morning_brief import build_conviction_reconciliation
        self.recon = build_conviction_reconciliation

    def _run(self, full_brief=None, sonnet_brief=None, signal_scores=None, scratchpad=None):
        """Helper: patch load_intelligence_brief and call recon."""
        fb = full_brief if full_brief is not None else {}
        sb = sonnet_brief if sonnet_brief is not None else {}
        ss = signal_scores if signal_scores is not None else {}
        sp = scratchpad if scratchpad is not None else {}
        with mock.patch("morning_brief.load_intelligence_brief", return_value=fb):
            return self.recon(sonnet_brief=sb, signal_scores=ss, scratchpad=sp)

    # --- CR-01: Scratchpad BLOCKING overrides brief HIGH conviction ---
    def test_CR01_scratchpad_block_overrides_brief_high(self):
        full_brief = _make_full_brief(longs=[
            {"symbol": "PLTR", "score": 82, "conviction": "HIGH", "catalyst": "strong earnings"},
        ])
        sonnet_brief = _make_sonnet_brief()
        signal_scores = _make_sig_syms([("PLTR", 80, "bullish", "high", "earnings beat")])
        scratchpad = _make_scratchpad(
            watching=["PLTR"],
            blocking=["PLTR: approaching stop, vol collapsing"],
        )
        result = self._run(full_brief, sonnet_brief, signal_scores, scratchpad)
        self.assertIn("AVOID", result)
        self.assertIn("scratchpad block", result)
        self.assertIn("PLTR", result)

    # --- CR-02: Signal score high vs brief MEDIUM → label diverge → upgrade ---
    def test_CR02_signal_upgrade_label_divergence(self):
        full_brief = _make_full_brief(longs=[
            {"symbol": "PLTR", "score": 62, "conviction": "MEDIUM", "catalyst": "thesis holds"},
        ])
        sonnet_brief = _make_sonnet_brief()
        # Signal gives HIGH conviction (score=78 → conviction=high)
        signal_scores = _make_sig_syms([
            ("PLTR", 78, "bullish", "high", "congressional buy confirmed")
        ])
        scratchpad = _make_scratchpad()
        result = self._run(full_brief, sonnet_brief, signal_scores, scratchpad)
        self.assertIn("HIGH", result)
        self.assertIn("signal upgrade", result)
        self.assertIn("PLTR", result)

    # --- CR-03: Signal direction bearish vs brief bullish → direction flip ---
    def test_CR03_direction_flip(self):
        full_brief = _make_full_brief(longs=[
            {"symbol": "CRWV", "score": 68, "conviction": "MEDIUM", "catalyst": "growth story"},
        ])
        sonnet_brief = _make_sonnet_brief()
        signal_scores = _make_sig_syms([
            ("CRWV", 35, "bearish", "low", "momentum broke, volume dropped")
        ])
        scratchpad = _make_scratchpad()
        result = self._run(full_brief, sonnet_brief, signal_scores, scratchpad)
        self.assertIn("direction flip", result)
        self.assertIn("CRWV", result)

    # --- CR-04: Signal consistent with brief → [= consistent] ---
    def test_CR04_consistent(self):
        full_brief = _make_full_brief(longs=[
            {"symbol": "GOOGL", "score": 80, "conviction": "HIGH", "catalyst": "earnings beat"},
        ])
        sonnet_brief = _make_sonnet_brief()
        signal_scores = _make_sig_syms([
            ("GOOGL", 82, "bullish", "high", "earnings beat confirmed")
        ])
        scratchpad = _make_scratchpad()
        result = self._run(full_brief, sonnet_brief, signal_scores, scratchpad)
        self.assertIn("= consistent", result)
        self.assertIn("GOOGL", result)

    # --- CR-05: Symbol in brief but not in signal scores → [? stale] ---
    def test_CR05_brief_only_stale(self):
        full_brief = _make_full_brief(longs=[
            {"symbol": "GRAB", "score": 62, "conviction": "MEDIUM", "catalyst": "SE Asia growth"},
        ])
        sonnet_brief = _make_sonnet_brief()
        signal_scores = _make_sig_syms([])  # GRAB not scored this cycle
        scratchpad = _make_scratchpad()
        result = self._run(full_brief, sonnet_brief, signal_scores, scratchpad)
        self.assertIn("stale", result)
        self.assertIn("GRAB", result)
        self.assertIn("not in today", result)

    # --- CR-06: Symbol in signal scores but not in brief → NEW annotation ---
    def test_CR06_signal_only_new(self):
        full_brief = _make_full_brief()  # no brief symbols
        sonnet_brief = _make_sonnet_brief()
        signal_scores = _make_sig_syms([
            ("NVDA", 75, "bullish", "high", "blackwell demand surge")
        ])
        scratchpad = _make_scratchpad()
        result = self._run(full_brief, sonnet_brief, signal_scores, scratchpad)
        self.assertIn("signal only", result)
        self.assertIn("NVDA", result)

    # --- CR-07: Output under 600 tokens (~2400 chars) ---
    def test_CR07_output_under_token_limit(self):
        many_longs = [
            {"symbol": f"SYM{i:02d}", "score": 70 - i, "conviction": "HIGH", "catalyst": "cat"}
            for i in range(25)
        ]
        many_sigs = [
            (f"SYM{i:02d}", 70 - i, "bullish", "high", "catalyst detail here") for i in range(25)
        ]
        full_brief = _make_full_brief(longs=many_longs)
        signal_scores = _make_sig_syms(many_sigs)
        result = self._run(full_brief, _make_sonnet_brief(), signal_scores, _make_scratchpad())
        self.assertLessEqual(len(result), 2400, f"Output too long: {len(result)} chars")

    # --- CR-08: Prompt template uses {conviction_table} not {conviction_state} ---
    def test_CR08_template_uses_conviction_table(self):
        template_path = _BOT_DIR / "prompts" / "user_template_v1.txt"
        self.assertTrue(template_path.exists(), "user_template_v1.txt not found")
        text = template_path.read_text()
        self.assertIn("{conviction_table}", text, "Template must contain {conviction_table}")
        self.assertNotIn("{conviction_state}", text,
                         "Template must not contain retired {conviction_state}")

    # --- CR-09: Missing morning_brief_sonnet.json falls back gracefully ---
    def test_CR09_missing_sonnet_brief_no_crash(self):
        signal_scores = _make_sig_syms([("AAPL", 65, "bullish", "medium", "analyst upgrade")])
        with mock.patch("morning_brief.load_intelligence_brief", return_value={}):
            result = self.recon(
                sonnet_brief=None,   # simulates missing file returning {}
                signal_scores=signal_scores,
                scratchpad=None,
            )
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)
        self.assertIn("CONVICTION TABLE", result)

    # --- CR-10: Missing scratchpad falls back gracefully ---
    def test_CR10_missing_scratchpad_no_crash(self):
        full_brief = _make_full_brief(longs=[
            {"symbol": "GLD", "score": 74, "conviction": "HIGH", "catalyst": "safe haven"},
        ])
        sonnet_brief = _make_sonnet_brief()
        signal_scores = _make_sig_syms([("GLD", 72, "bullish", "high", "safe haven bid")])
        with mock.patch("morning_brief.load_intelligence_brief", return_value=full_brief):
            result = self.recon(
                sonnet_brief=sonnet_brief,
                signal_scores=signal_scores,
                scratchpad=None,   # missing scratchpad
            )
        self.assertIsInstance(result, str)
        self.assertIn("CONVICTION TABLE", result)
        self.assertIn("GLD", result)

    # --- Extra: Signal conviction=avoid forces AVOID status ---
    def test_signal_avoid_conviction(self):
        full_brief = _make_full_brief(longs=[
            {"symbol": "TSLA", "score": 55, "conviction": "MEDIUM", "catalyst": "margin squeeze"}
        ])
        sonnet_brief = _make_sonnet_brief()
        signal_scores = _make_sig_syms([("TSLA", 25, "bearish", "avoid", "earnings miss")])
        result = self._run(full_brief, sonnet_brief, signal_scores, _make_scratchpad())
        self.assertIn("AVOID", result)
        self.assertIn("signal avoid", result)

    # --- Extra: Score diverge ≥20 within same label tier triggers upgrade annotation ---
    def test_score_divergence_triggers_upgrade(self):
        full_brief = _make_full_brief(longs=[
            {"symbol": "XOM", "score": 55, "conviction": "MEDIUM", "catalyst": "crude above 100"},
        ])
        sonnet_brief = _make_sonnet_brief()
        # Score diverges by 25, but labels happen to agree — secondary score rule fires
        signal_scores = _make_sig_syms([("XOM", 55, "bullish", "medium", "crude above 100")])
        result = self._run(full_brief, sonnet_brief, signal_scores, _make_scratchpad())
        # No divergence here — both MEDIUM with similar scores
        self.assertIn("= consistent", result)
        # Now test actual divergence within same label tier
        signal_scores2 = _make_sig_syms([("XOM", 30, "bullish", "medium", "momentum fading")])
        result2 = self._run(full_brief, sonnet_brief, signal_scores2, _make_scratchpad())
        # |55 - 30| = 25 ≥ 20 → should trigger score divergence
        # Labels: both are MEDIUM but score differs by 25 — label diverge should NOT fire
        # (both still "medium"), score diverge rule fires
        self.assertIn("signal downgrade", result2)

    # --- Extra: build_conviction_reconciliation is exported from morning_brief ---
    def test_function_exported(self):
        import morning_brief
        self.assertTrue(
            hasattr(morning_brief, "build_conviction_reconciliation"),
            "build_conviction_reconciliation must be importable from morning_brief",
        )

    # --- Extra: bot_stage3_decision.build_user_prompt has signal_scores_raw / scratchpad_raw params ---
    def test_build_user_prompt_has_new_params(self):
        import inspect
        with mock.patch.dict(sys.modules, {
            "portfolio_intelligence": mock.MagicMock(),
            "bot_clients": mock.MagicMock(),
            "log_setup": mock.MagicMock(),
        }):
            import importlib
            bsd = importlib.import_module("bot_stage3_decision")
            sig = inspect.signature(bsd.build_user_prompt)
            self.assertIn("signal_scores_raw", sig.parameters)
            self.assertIn("scratchpad_raw", sig.parameters)


if __name__ == "__main__":
    unittest.main()
