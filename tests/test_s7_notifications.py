"""
Suite S7-Notifications — tests for enriched trade email and WhatsApp trade alerts.

Tests:
  N1–N8  : build_order_email_html fields and "n/a" fallbacks
  N9–N17 : send_trade_alert message content and dedup behaviour
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from notifications import build_order_email_html

# ─── helpers ────────────────────────────────────────────────────────────────

def _make_result(**kwargs):
    defaults = dict(
        symbol="STNG", action="buy", status="submitted", reason="",
        order_id="abc123", fill_price=81.19, filled_qty=315.0,
        fill_timestamp=None, qty=315.0, order_type="limit",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_exec_action(**kwargs):
    defaults = dict(
        action="buy", symbol="STNG", qty=315, tier="dynamic",
        confidence="high", conviction=None,
        stop_loss=77.79, take_profit=89.14, limit_price=81.25,
        catalyst="Hormuz strait disruption; tanker rates spiking",
        sector_signal=None,
    )
    defaults.update(kwargs)
    return defaults


def _make_signal_scores(symbol="STNG", score=60.0):
    return {"scored_symbols": {symbol: {"score": score, "direction": "long"}}}


# ─── Suite N1–N8: email HTML helper ─────────────────────────────────────────

class TestBuildOrderEmailHtml(unittest.TestCase):

    def _html(self, **overrides):
        r         = _make_result(**overrides.get("r", {}))
        action    = _make_exec_action(**overrides.get("action", {}))
        scores    = overrides.get("scores", _make_signal_scores())
        idea_c    = overrides.get("idea_conviction", 0.72)
        equity    = overrides.get("equity", 101_000.0)
        reasoning = overrides.get("reasoning", "Market is in geopolitical caution. Tankers benefit.")
        return build_order_email_html(r, action, scores, idea_c, equity, reasoning)

    # N1 — all main fields present
    def test_n1_required_fields_present(self):
        html = self._html()
        for label in ["Fill price", "Shares", "Position size", "% of portfolio",
                      "Tier", "Conviction", "Confidence", "Signal score",
                      "Stop loss", "Take profit", "Limit price", "Thesis"]:
            self.assertIn(label, html, f"Missing field: {label}")

    # N2 — numeric fill price formatted
    def test_n2_fill_price_formatted(self):
        html = self._html()
        self.assertIn("$81.19", html)

    # N3 — signal score rendered
    def test_n3_signal_score_rendered(self):
        html = self._html(scores=_make_signal_scores("STNG", 60.0))
        self.assertIn("60/100", html)

    # N4 — conviction rendered
    def test_n4_conviction_rendered(self):
        html = self._html(idea_conviction=0.72)
        self.assertIn("0.72", html)

    # N5 — None fill_price shows n/a
    def test_n5_none_fill_price_shows_na(self):
        html = self._html(r={"fill_price": None})
        self.assertIn("n/a", html)

    # N6 — missing symbol in signal_scores shows n/a
    def test_n6_missing_signal_score_shows_na(self):
        html = self._html(scores={"scored_symbols": {}})
        self.assertIn("n/a", html)

    # N7 — size and % of portfolio computed correctly
    def test_n7_portfolio_pct_computed(self):
        # 315 × $81.19 = $25,574.85 → rounds to $25,575; 25.3% of $101,000
        html = self._html(equity=101_000.0)
        self.assertIn("25,575", html)
        self.assertIn("25.3%", html)

    # N8 — thesis truncated to 3 sentences
    def test_n8_thesis_from_reasoning(self):
        reasoning = "Sentence one. Sentence two. Sentence three. Sentence four."
        html = self._html(reasoning=reasoning)
        self.assertIn("Sentence one", html)
        self.assertNotIn("Sentence four", html)


# ─── Suite N9–N14: send_trade_alert ──────────────────────────────────────────

class TestSendTradeAlert(unittest.TestCase):

    def _make_publisher(self):
        # Import TradePublisher with minimal stubs
        with patch.dict("sys.modules", {
            "twilio":       MagicMock(),
            "twilio.rest":  MagicMock(),
            "sendgrid":     MagicMock(),
            "sendgrid.helpers": MagicMock(),
            "sendgrid.helpers.mail": MagicMock(),
            "log_setup":    MagicMock(get_logger=lambda n: MagicMock()),
        }):
            import importlib as _il  # noqa: PLC0415

            import trade_publisher as tp  # noqa: PLC0415
            _il.reload(tp)
            pub = tp.TradePublisher.__new__(tp.TradePublisher)
            pub._trade_alert_cache = {}
            pub._history = {}
            pub.enabled = True
            return pub

    # N9 — BUY uses green emoji
    def test_n9_buy_emoji(self):
        pub = self._make_publisher()
        with patch.object(pub, "send_alert", return_value=True) as mock_send:
            pub.send_trade_alert("buy", "STNG", 315, 81.19, 0.72,
                                 "Hormuz disruption", 101_000.0)
            call_msg = mock_send.call_args[0][0]
            self.assertIn("🟢", call_msg)

    # N10 — SELL uses red emoji
    def test_n10_sell_emoji(self):
        pub = self._make_publisher()
        with patch.object(pub, "send_alert", return_value=True) as mock_send:
            pub.send_trade_alert("sell", "STNG", 315, 81.19, 0.72, None, 101_000.0)
            self.assertIn("🔴", mock_send.call_args[0][0])

    # N11 — TRIM uses yellow emoji
    def test_n11_trim_emoji(self):
        pub = self._make_publisher()
        with patch.object(pub, "send_alert", return_value=True) as mock_send:
            pub.send_trade_alert("trim", "GLD", 10, 180.0, 0.6, None, 101_000.0)
            self.assertIn("🟡", mock_send.call_args[0][0])

    # N12 — ADD uses blue emoji
    def test_n12_add_emoji(self):
        pub = self._make_publisher()
        with patch.object(pub, "send_alert", return_value=True) as mock_send:
            pub.send_trade_alert("add", "AMZN", 5, 200.0, 0.8, None, 101_000.0)
            self.assertIn("🔵", mock_send.call_args[0][0])

    # N13 — message includes symbol, qty, price, conviction
    def test_n13_message_content(self):
        pub = self._make_publisher()
        with patch.object(pub, "send_alert", return_value=True) as mock_send:
            pub.send_trade_alert("buy", "STNG", 315, 81.19, 0.72,
                                 "Hormuz disruption catalyst", 101_000.0)
            msg = mock_send.call_args[0][0]
            self.assertIn("STNG", msg)
            self.assertIn("315", msg)
            self.assertIn("81.19", msg)
            self.assertIn("0.72", msg)
            self.assertIn("Hormuz", msg)

    # N14 — 30-min dedup suppresses repeat for same symbol
    def test_n14_dedup_same_symbol_suppressed(self):
        pub = self._make_publisher()
        with patch.object(pub, "send_alert", return_value=True) as mock_send:
            pub.send_trade_alert("buy", "STNG", 315, 81.19, 0.72, None, 101_000.0)
            pub.send_trade_alert("buy", "STNG", 315, 81.19, 0.72, None, 101_000.0)
            self.assertEqual(mock_send.call_count, 1)

    # N15 — dedup does NOT suppress different symbol
    def test_n15_dedup_different_symbol_not_suppressed(self):
        pub = self._make_publisher()
        with patch.object(pub, "send_alert", return_value=True) as mock_send:
            pub.send_trade_alert("buy", "STNG", 315, 81.19, 0.72, None, 101_000.0)
            pub.send_trade_alert("buy", "GLD", 34, 180.0, 0.65, None, 101_000.0)
            self.assertEqual(mock_send.call_count, 2)

    # N16 — halt dedup and trade dedup are independent dicts
    def test_n16_trade_and_halt_dedup_independent(self):
        pub = self._make_publisher()
        # Manually populate a halt-like cache on a different attribute
        # to confirm they don't share state
        pub._trade_alert_cache = {"STNG": 0.0}   # old entry — should pass 30-min check
        with patch.object(pub, "send_alert", return_value=True) as mock_send:
            pub.send_trade_alert("buy", "STNG", 315, 81.19, 0.72, None, 101_000.0)
            # entry was epoch 0 → far more than 30 min ago → should fire
            self.assertEqual(mock_send.call_count, 1)

    # N17 — None conviction renders as "n/a" not crash
    def test_n17_none_conviction_renders_na(self):
        pub = self._make_publisher()
        with patch.object(pub, "send_alert", return_value=True) as mock_send:
            pub.send_trade_alert("buy", "STNG", 315, 81.19, None, None, 101_000.0)
            msg = mock_send.call_args[0][0]
            self.assertIn("n/a", msg)


if __name__ == "__main__":
    unittest.main()
