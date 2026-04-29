"""
notifications.py — lightweight notification helpers for BullBearBot.

No heavy bot-pipeline imports. Importable standalone for testing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    pass


def build_order_email_html(
    r: object,
    exec_action: dict,
    signal_scores_obj: dict,
    idea_conviction: Optional[float],
    equity: Optional[float],
    reasoning: str,
) -> str:
    """Build enriched HTML email body for a submitted order.

    Args:
        r: ExecutionResult (or any object with symbol/action/order_id/fill_price/qty attrs)
        exec_action: BrokerAction.to_dict() for this symbol (confidence, tier, stop_loss, etc.)
        signal_scores_obj: {"scored_symbols": {symbol: {"score": float, ...}}}
        idea_conviction: float conviction from ClaudeDecision.ideas (may be None)
        equity: account equity in dollars (for % calculation)
        reasoning: Stage 3 Sonnet reasoning string
    """

    def _fmt(v: object) -> str:
        return "n/a" if v is None else str(v)

    symbol    = r.symbol  # type: ignore[attr-defined]
    action    = r.action  # type: ignore[attr-defined]
    fill      = f"${r.fill_price:.2f}" if r.fill_price is not None else "n/a"  # type: ignore[attr-defined]
    qty_val   = r.qty or exec_action.get("qty")  # type: ignore[attr-defined]
    qty_str   = _fmt(qty_val)
    size_usd  = (qty_val * r.fill_price) if (qty_val and r.fill_price is not None) else None  # type: ignore[attr-defined]
    size_str  = f"${size_usd:,.0f}" if size_usd is not None else "n/a"
    pct_str   = (
        f"{size_usd / equity * 100:.1f}%" if (size_usd is not None and equity) else "n/a"
    )
    conv_str  = f"{idea_conviction:.2f}" if idea_conviction is not None else "n/a"
    conf_str  = _fmt(exec_action.get("confidence"))
    tier_str  = _fmt(exec_action.get("tier"))
    stop_str  = _fmt(exec_action.get("stop_loss"))
    tp_str    = _fmt(exec_action.get("take_profit"))
    limit_str = _fmt(exec_action.get("limit_price"))
    catalyst  = exec_action.get("catalyst") or ""

    scored    = signal_scores_obj.get("scored_symbols", {})
    sig_entry = scored.get(symbol, {})
    sig_score = sig_entry.get("score")
    sig_str   = f"{sig_score:.0f}/100" if sig_score is not None else "n/a"

    # Thesis: first 3 sentences from reasoning, fall back to catalyst
    thesis_src = reasoning or catalyst
    sentences  = [s.strip() for s in thesis_src.replace(";", ".").split(".") if s.strip()]
    thesis_str = ". ".join(sentences[:3]) + "." if sentences else "n/a"

    rows = [
        ("Action",         action.upper()),
        ("Symbol",         symbol),
        ("Order ID",       _fmt(r.order_id)),  # type: ignore[attr-defined]
        ("Fill price",     fill),
        ("Limit price",    limit_str),
        ("Shares",         qty_str),
        ("Position size",  size_str),
        ("% of portfolio", pct_str),
        ("Tier",           tier_str),
        ("Conviction",     conv_str),
        ("Confidence",     conf_str),
        ("Signal score",   sig_str),
        ("Stop loss",      stop_str),
        ("Take profit",    tp_str),
        ("Thesis",         thesis_str),
    ]
    row_html = "".join(
        f"<tr style='background:{('#f9f9f9' if i % 2 else 'white')}'>"
        f"<td style='padding:6px 10px;width:160px'><strong>{label}</strong></td>"
        f"<td style='padding:6px 10px'>{value}</td></tr>"
        for i, (label, value) in enumerate(rows)
    )
    return (
        "<html><body style='font-family:Arial,sans-serif;max-width:700px'>"
        f"<h2 style='color:#1a1a2e'>Order Submitted: {action.upper()} {symbol}</h2>"
        f"<table style='border-collapse:collapse;width:100%;font-size:14px'>{row_html}</table>"
        "</body></html>"
    )


def send_whatsapp_direct(message: str) -> bool:
    """Send a WhatsApp message via Twilio without a TradePublisher instance.

    Reads TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, WHATSAPP_FROM, WHATSAPP_TO from env.
    Used by order_executor to send fill confirmations and cancellation alerts from the
    T-021 fill-poll loop, where no TradePublisher instance is available.
    Returns True on success, False on any failure. Never raises.
    """
    import os  # noqa: PLC0415
    try:
        from dotenv import load_dotenv  # noqa: PLC0415
        load_dotenv()
        sid   = os.getenv("TWILIO_ACCOUNT_SID")
        token = os.getenv("TWILIO_AUTH_TOKEN")
        from_ = os.getenv("WHATSAPP_FROM")
        to    = os.getenv("WHATSAPP_TO")
        if not all([sid, token, from_, to]):
            return False
        from twilio.rest import Client  # noqa: PLC0415
        Client(sid, token).messages.create(body=message, from_=from_, to=to)
        return True
    except Exception:
        return False
