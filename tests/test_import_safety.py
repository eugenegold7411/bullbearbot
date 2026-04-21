"""
test_import_safety.py — verifies orchestrator modules import cleanly with no env vars set.

Tests import safety only. Does not test runtime behavior or API calls.
Each test patches all known credential env vars to empty strings so the
import cannot succeed by accidentally inheriting a real .env.
"""

import importlib
import os
import sys
import unittest
from unittest.mock import patch

_EMPTY_CREDS = {
    "ALPACA_API_KEY": "",
    "ALPACA_SECRET_KEY": "",
    "ALPACA_API_KEY_OPTIONS": "",
    "ALPACA_SECRET_KEY_OPTIONS": "",
    "ANTHROPIC_API_KEY": "",
    "TWILIO_ACCOUNT_SID": "",
    "TWILIO_AUTH_TOKEN": "",
    "TWILIO_FROM_NUMBER": "",
    "TWILIO_TO_NUMBER": "",
    "WHATSAPP_FROM": "",
    "WHATSAPP_TO": "",
    "SENDGRID_API_KEY": "",
}


def _evict(module_name: str) -> None:
    """Remove a module and its sub-modules from sys.modules."""
    to_remove = [k for k in sys.modules if k == module_name or k.startswith(module_name + ".")]
    for k in to_remove:
        del sys.modules[k]


def _try_import(module_name: str) -> None:
    """
    Import module_name with empty credentials. Raises AssertionError on
    EnvironmentError or any exception that is not an unavailable-package ImportError.
    An ImportError due to a missing optional package causes the test to be skipped.
    """
    _evict(module_name)
    with patch.dict(os.environ, _EMPTY_CREDS, clear=False):
        try:
            importlib.import_module(module_name)
        except ImportError as exc:
            msg = str(exc).lower()
            if "no module named" in msg or "cannot import name" in msg:
                raise unittest.SkipTest(f"Optional dependency unavailable for {module_name}: {exc}")
            raise
        except EnvironmentError as exc:
            raise AssertionError(
                f"{module_name} raised EnvironmentError at import time (env vars were empty): {exc}"
            ) from exc


class TestImportSafety(unittest.TestCase):

    def test_bot_imports_without_env(self):
        """bot.py and its full import chain must not raise at import with empty credentials."""
        _try_import("bot")

    def test_order_executor_imports_without_env(self):
        _try_import("order_executor")

    def test_weekly_review_imports_without_env(self):
        _try_import("weekly_review")

    def test_bot_options_imports_without_env(self):
        _try_import("bot_options")

    def test_bot_no_module_level_alpaca_raise(self):
        """bot_clients._alpaca must be None at import — lazy-init not yet triggered."""
        _try_import("bot_clients")
        import bot_clients  # noqa: PLC0415
        self.assertIsNone(bot_clients._alpaca)

    def test_bot_no_module_level_claude_raise(self):
        """bot_clients._claude must be None at import — lazy-init not yet triggered."""
        _try_import("bot_clients")
        import bot_clients  # noqa: PLC0415
        self.assertIsNone(bot_clients._claude)

    def test_order_executor_no_module_level_client(self):
        """Confirm order_executor._alpaca is None after import (lazy-init)."""
        _try_import("order_executor")
        import order_executor  # noqa: PLC0415
        self.assertIsNone(order_executor._alpaca)

    def test_weekly_review_no_module_level_client(self):
        """Confirm weekly_review._claude is None after import (lazy-init)."""
        _try_import("weekly_review")
        import weekly_review  # noqa: PLC0415
        self.assertIsNone(weekly_review._claude)

    def test_bot_options_no_module_level_client(self):
        """Confirm bot_options._alpaca and ._claude are None after import (lazy-init)."""
        _try_import("bot_options")
        import bot_options  # noqa: PLC0415
        self.assertIsNone(bot_options._alpaca)
        self.assertIsNone(bot_options._claude)


if __name__ == "__main__":
    unittest.main()
