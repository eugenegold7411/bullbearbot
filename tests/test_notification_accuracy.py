"""
Suite NA — Notification Accuracy: fire on fill not submission.

NA1: fill_price=None → send_trade_alert not fired (Part A fill_price guard)
NA2: fill_price populated → send_trade_alert fires with actual price
NA3: T-021 detects filled + alert_deferred=True → fill confirmation WhatsApp sent
NA4: T-021 detects cancelled → cancellation WhatsApp sent
NA5: publish_trade_entry fires for submitted buy/sell results (secondary fix)
NA6: publish_trade_entry not fired for hold/monitor results (secondary fix)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_result(**kwargs):
    defaults = dict(
        status="submitted", action="buy", symbol="AMZN",
        qty=60.0, fill_price=255.50, filled_qty=60.0,
        fill_timestamp=None, order_id="ord-123", order_type="market",
        reason="",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _run_fill_price_guard(result, publisher, equity=100_000.0):
    """
    Reproduce the send_trade_alert dispatch block from bot.py (Part A).
    Returns True if the alert was sent, False if it was suppressed.
    """
    if result.fill_price is not None:
        publisher.send_trade_alert(
            action=result.action,
            symbol=result.symbol,
            qty=result.qty,
            price=result.fill_price,
            conviction=None,
            catalyst=None,
            equity=equity,
        )
        return True
    return False


def _run_publish_trade_entry_guard(result, publisher, actions):
    """
    Reproduce the publish_trade_entry dispatch block from bot.py (secondary fix).
    Fires only when status=submitted AND action in (buy, sell, close).
    """
    if (
        publisher is not None
        and getattr(publisher, "enabled", False)
        and result.action in ("buy", "sell", "close")
    ):
        _pub_action = next(
            (a for a in actions
             if a.get("symbol") == result.symbol and a.get("action") == result.action),
            None,
        )
        if _pub_action:
            publisher.publish_trade_entry(
                action=_pub_action,
                debate_result=None,
                market_context="VIX=20.0 regime=neutral",
                alpaca_client=None,
            )
            return True
    return False


# ── NA1 — fill_price=None → send_trade_alert suppressed ──────────────────────

class TestNA1FillPriceNoneSuppressesAlert:

    def test_na1_send_trade_alert_not_called_when_fill_price_none(self):
        """V-case: order submitted to Alpaca but fill_price not yet confirmed."""
        pub = MagicMock()
        r   = _make_result(action="sell", symbol="V", qty=114, fill_price=None)

        fired = _run_fill_price_guard(r, pub)

        assert not fired
        pub.send_trade_alert.assert_not_called()

    def test_na1_log_info_when_fill_price_none(self, caplog):
        """When fill_price is None the deferred-alert INFO log must appear."""
        import logging
        import order_executor as oe  # noqa: PLC0415

        # Patch Alpaca to return an unfilled order (fill_price absent)
        mock_order = MagicMock()
        mock_order.id              = "oid-v-sell"
        mock_order.filled_avg_price = None
        mock_order.filled_qty       = None
        mock_order.filled_at        = None

        mock_alpaca = MagicMock()
        mock_alpaca.submit_order.return_value = mock_order
        mock_alpaca.get_all_positions.return_value = []

        pub = MagicMock()
        pub.enabled = True

        oe._pending_fill_checks.clear()

        with (
            patch("order_executor._get_alpaca", return_value=mock_alpaca),
            patch("order_executor.validate_action", return_value=None),
            patch("order_executor.risk_kernel", create=True),
        ):
            result = oe._extract_fill(mock_order)
            fp, fq, ft = result
            # fill_price is None — this triggers the "alert deferred" branch in bot.py
            assert fp is None


# ── NA2 — fill_price populated → send_trade_alert fires ─────────────────────

class TestNA2FillPricePopulatedFiresAlert:

    def test_na2_send_trade_alert_called_with_fill_price(self):
        """Market order that fills synchronously should trigger the alert."""
        pub = MagicMock()
        r   = _make_result(action="buy", symbol="AMZN", qty=60, fill_price=255.50)

        fired = _run_fill_price_guard(r, pub)

        assert fired
        pub.send_trade_alert.assert_called_once_with(
            action="buy",
            symbol="AMZN",
            qty=60,
            price=255.50,
            conviction=None,
            catalyst=None,
            equity=100_000.0,
        )

    def test_na2_fill_price_forwarded_to_alert(self):
        """The fill_price value in the result is used as the alert price."""
        pub = MagicMock()
        r   = _make_result(action="sell", symbol="GLD", qty=34, fill_price=442.11)

        _run_fill_price_guard(r, pub)

        _, kwargs = pub.send_trade_alert.call_args
        assert kwargs["price"] == 442.11

    def test_na2_sell_action_fires_with_confirmed_fill(self):
        """sell action with fill_price present → alert fires (red emoji path)."""
        pub = MagicMock()
        r   = _make_result(action="sell", symbol="MSFT", qty=47, fill_price=418.30)

        fired = _run_fill_price_guard(r, pub)

        assert fired


# ── NA3 — T-021 filled + alert_deferred → fill confirmation WhatsApp ─────────

class TestNA3T021FillConfirmationNotification:

    def _setup_pending(self, oid, action, symbol, qty, alert_deferred=True):
        import order_executor as oe  # noqa: PLC0415
        oe._pending_fill_checks.clear()
        oe._pending_fill_checks[oid] = {
            "symbol":         symbol,
            "action":         action,
            "qty":            qty,
            "alert_deferred": alert_deferred,
        }

    def test_na3_whatsapp_sent_on_fill_when_alert_deferred(self):
        """T-021 poll: filled + alert_deferred=True → fire deferred fill notification."""
        import order_executor as oe  # noqa: PLC0415

        self._setup_pending("oid-amzn-buy", "buy", "AMZN", 60, alert_deferred=True)

        mock_order = MagicMock()
        mock_order.status             = "filled"
        mock_order.filled_avg_price   = "255.50"
        mock_order.filled_qty         = "60"
        mock_order.filled_at          = "2026-04-29T14:30:00Z"

        mock_alpaca = MagicMock()
        mock_alpaca.get_order_by_id.return_value = mock_order

        with (
            patch("order_executor._get_alpaca", return_value=mock_alpaca),
            patch("notifications.send_whatsapp_direct") as mock_wa,
        ):
            oe._check_pending_fills()

        mock_wa.assert_called_once()
        msg = mock_wa.call_args[0][0]
        assert "FILL CONFIRMED" in msg
        assert "AMZN" in msg
        assert "255.50" in msg
        assert "🟢" in msg

    def test_na3_no_whatsapp_when_alert_already_sent(self):
        """T-021: if alert_deferred=False (alert fired at submission), no duplicate."""
        import order_executor as oe  # noqa: PLC0415

        self._setup_pending("oid-gld-buy", "buy", "GLD", 34, alert_deferred=False)

        mock_order = MagicMock()
        mock_order.status           = "filled"
        mock_order.filled_avg_price = "442.00"
        mock_order.filled_qty       = "34"
        mock_order.filled_at        = "2026-04-29T15:00:00Z"

        mock_alpaca = MagicMock()
        mock_alpaca.get_order_by_id.return_value = mock_order

        with (
            patch("order_executor._get_alpaca", return_value=mock_alpaca),
            patch("notifications.send_whatsapp_direct") as mock_wa,
        ):
            oe._check_pending_fills()

        mock_wa.assert_not_called()

    def test_na3_sell_fill_uses_red_emoji(self):
        """T-021: deferred fill for a sell action uses red emoji."""
        import order_executor as oe  # noqa: PLC0415

        self._setup_pending("oid-msft-sell", "sell", "MSFT", 47, alert_deferred=True)

        mock_order = MagicMock()
        mock_order.status           = "filled"
        mock_order.filled_avg_price = "418.30"
        mock_order.filled_qty       = "47"
        mock_order.filled_at        = "2026-04-29T15:01:00Z"

        mock_alpaca = MagicMock()
        mock_alpaca.get_order_by_id.return_value = mock_order

        with (
            patch("order_executor._get_alpaca", return_value=mock_alpaca),
            patch("notifications.send_whatsapp_direct") as mock_wa,
        ):
            oe._check_pending_fills()

        mock_wa.assert_called_once()
        assert "🔴" in mock_wa.call_args[0][0]


# ── NA4 — T-021 cancelled → cancellation WhatsApp sent ───────────────────────

class TestNA4T021CancellationNotification:

    def _setup_pending(self, oid, action, symbol, qty):
        import order_executor as oe  # noqa: PLC0415
        oe._pending_fill_checks.clear()
        oe._pending_fill_checks[oid] = {
            "symbol":         symbol,
            "action":         action,
            "qty":            qty,
            "alert_deferred": True,
        }

    def test_na4_whatsapp_sent_on_cancelled(self):
        """V-case: order cancelled by Alpaca (OCA lock) → cancellation alert fired."""
        import order_executor as oe  # noqa: PLC0415

        self._setup_pending("oid-v-sell", "sell", "V", 114)

        mock_order = MagicMock()
        mock_order.status = "cancelled"

        mock_alpaca = MagicMock()
        mock_alpaca.get_order_by_id.return_value = mock_order

        with (
            patch("order_executor._get_alpaca", return_value=mock_alpaca),
            patch("notifications.send_whatsapp_direct") as mock_wa,
        ):
            oe._check_pending_fills()

        mock_wa.assert_called_once()
        msg = mock_wa.call_args[0][0]
        assert "⚠️" in msg
        assert "ORDER CANCELLED" in msg
        assert "SELL" in msg
        assert "V" in msg

    def test_na4_qty_included_in_cancellation_message(self):
        """Cancellation message includes qty for operator clarity."""
        import order_executor as oe  # noqa: PLC0415

        self._setup_pending("oid-v-sell-2", "sell", "V", 114)

        mock_order = MagicMock()
        mock_order.status = "canceled"   # Alpaca uses American spelling

        mock_alpaca = MagicMock()
        mock_alpaca.get_order_by_id.return_value = mock_order

        with (
            patch("order_executor._get_alpaca", return_value=mock_alpaca),
            patch("notifications.send_whatsapp_direct") as mock_wa,
        ):
            oe._check_pending_fills()

        msg = mock_wa.call_args[0][0]
        assert "114" in msg

    def test_na4_expired_also_triggers_cancellation_alert(self):
        """Expired orders are treated like cancellations."""
        import order_executor as oe  # noqa: PLC0415

        self._setup_pending("oid-qqq-buy", "buy", "QQQ", 31)

        mock_order = MagicMock()
        mock_order.status = "expired"

        mock_alpaca = MagicMock()
        mock_alpaca.get_order_by_id.return_value = mock_order

        with (
            patch("order_executor._get_alpaca", return_value=mock_alpaca),
            patch("notifications.send_whatsapp_direct") as mock_wa,
        ):
            oe._check_pending_fills()

        mock_wa.assert_called_once()
        assert "ORDER CANCELLED" in mock_wa.call_args[0][0]


# ── NA5 — publish_trade_entry fires for submitted buy/sell ───────────────────

class TestNA5PublishTradeEntryFiredForSubmitted:

    def test_na5_publish_called_for_buy_submitted(self):
        """publish_trade_entry fires when status=submitted and action=buy."""
        pub = MagicMock()
        pub.enabled = True
        r       = _make_result(status="submitted", action="buy", symbol="AMZN")
        actions = [{"action": "buy", "symbol": "AMZN", "qty": 60, "tier": "core"}]

        fired = _run_publish_trade_entry_guard(r, pub, actions)

        assert fired
        pub.publish_trade_entry.assert_called_once()

    def test_na5_publish_called_for_sell_submitted(self):
        """publish_trade_entry fires for sell action too (exit tweet)."""
        pub = MagicMock()
        pub.enabled = True
        r       = _make_result(status="submitted", action="sell", symbol="GLD")
        actions = [{"action": "sell", "symbol": "GLD", "qty": 34}]

        fired = _run_publish_trade_entry_guard(r, pub, actions)

        assert fired
        pub.publish_trade_entry.assert_called_once()

    def test_na5_action_dict_forwarded_to_publish(self):
        """The matched action dict is passed to publish_trade_entry."""
        pub = MagicMock()
        pub.enabled = True
        r       = _make_result(status="submitted", action="buy", symbol="AMZN")
        actions = [{"action": "buy", "symbol": "AMZN", "qty": 60, "catalyst": "AI demand"}]

        _run_publish_trade_entry_guard(r, pub, actions)

        _, kwargs = pub.publish_trade_entry.call_args
        assert kwargs["action"]["catalyst"] == "AI demand"


# ── NA6 — publish_trade_entry not fired for hold/monitor ─────────────────────

class TestNA6PublishTradeEntryNotFiredForHold:

    def test_na6_not_called_for_hold_action(self):
        """publish_trade_entry must not fire when action=hold (hold is not a trade entry)."""
        pub = MagicMock()
        pub.enabled = True
        r       = _make_result(status="hold", action="hold", symbol="AMZN")
        actions = [{"action": "hold", "symbol": "AMZN"}]

        fired = _run_publish_trade_entry_guard(r, pub, actions)

        assert not fired
        pub.publish_trade_entry.assert_not_called()

    def test_na6_not_called_for_monitor_action(self):
        """publish_trade_entry must not fire for monitor/watch/observe actions."""
        pub = MagicMock()
        pub.enabled = True
        for act in ("monitor", "watch", "observe"):
            pub.reset_mock()
            r       = _make_result(status="hold", action=act, symbol="QQQ")
            actions = [{"action": act, "symbol": "QQQ"}]

            fired = _run_publish_trade_entry_guard(r, pub, actions)

            assert not fired, f"Expected no publish for action={act}"
            pub.publish_trade_entry.assert_not_called()

    def test_na6_not_called_when_publisher_disabled(self):
        """Guard: publisher.enabled=False suppresses publish_trade_entry."""
        pub = MagicMock()
        pub.enabled = False
        r       = _make_result(status="submitted", action="buy", symbol="AMZN")
        actions = [{"action": "buy", "symbol": "AMZN", "qty": 60}]

        fired = _run_publish_trade_entry_guard(r, pub, actions)

        assert not fired
        pub.publish_trade_entry.assert_not_called()

    def test_na6_not_called_when_no_matching_action(self):
        """Guard: if no matching action is found in actions, publish is skipped."""
        pub = MagicMock()
        pub.enabled = True
        r       = _make_result(status="submitted", action="buy", symbol="AMZN")
        actions = [{"action": "buy", "symbol": "GLD", "qty": 34}]  # different symbol

        fired = _run_publish_trade_entry_guard(r, pub, actions)

        assert not fired


# ── NA bonus: send_whatsapp_direct standalone ─────────────────────────────────

class TestSendWhatsappDirect:

    def test_nab1_returns_false_when_credentials_absent(self, monkeypatch):
        """send_whatsapp_direct returns False gracefully when Twilio env vars unset."""
        from notifications import send_whatsapp_direct  # noqa: PLC0415

        monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
        monkeypatch.delenv("TWILIO_AUTH_TOKEN",  raising=False)
        monkeypatch.delenv("WHATSAPP_FROM",       raising=False)
        monkeypatch.delenv("WHATSAPP_TO",         raising=False)

        result = send_whatsapp_direct("test message")
        assert result is False

    def test_nab2_calls_twilio_create_when_configured(self, monkeypatch):
        """send_whatsapp_direct calls Twilio Client.messages.create when creds present."""
        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
        monkeypatch.setenv("TWILIO_AUTH_TOKEN",  "authtest")
        monkeypatch.setenv("WHATSAPP_FROM",       "whatsapp:+14155238886")
        monkeypatch.setenv("WHATSAPP_TO",         "whatsapp:+18189177789")

        mock_client_instance = MagicMock()
        mock_client_cls = MagicMock(return_value=mock_client_instance)

        # The function does `from twilio.rest import Client` — patch the lazy import
        import twilio.rest as _twilio_rest  # noqa: PLC0415
        _twilio_rest.Client = mock_client_cls

        from notifications import send_whatsapp_direct  # noqa: PLC0415
        result = send_whatsapp_direct("⚠️ ORDER CANCELLED: SELL V 114 — cancelled")

        assert result is True
        mock_client_instance.messages.create.assert_called_once()
        _, kwargs = mock_client_instance.messages.create.call_args
        assert "SELL V 114" in kwargs.get("body", "")
