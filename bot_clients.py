"""
bot_clients.py — Lazy-init singletons for Alpaca and Claude API clients.

Shared by bot.py and all bot_stage*.py modules to avoid circular imports
when stage files are extracted from bot.py.
"""

import os

import anthropic
from alpaca.trading.client import TradingClient

try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except ImportError:
    pass

MODEL      = "claude-sonnet-4-6"
MODEL_FAST = "claude-haiku-4-5-20251001"


def _build_alpaca_client() -> TradingClient:
    api_key = os.getenv("ALPACA_API_KEY")
    secret  = os.getenv("ALPACA_SECRET_KEY")
    base    = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    if not api_key or not secret:
        raise EnvironmentError("Missing ALPACA_API_KEY or ALPACA_SECRET_KEY in .env")
    return TradingClient(api_key=api_key, secret_key=secret, paper=("paper" in base))


def _build_claude_client() -> anthropic.Anthropic:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise EnvironmentError("Missing ANTHROPIC_API_KEY in .env")
    return anthropic.Anthropic(api_key=key)


_alpaca: TradingClient | None = None
_claude: anthropic.Anthropic | None = None


def _get_alpaca() -> TradingClient:
    global _alpaca
    if _alpaca is None:
        _alpaca = _build_alpaca_client()
    return _alpaca


def _get_claude() -> anthropic.Anthropic:
    global _claude
    if _claude is None:
        _claude = _build_claude_client()
    return _claude
