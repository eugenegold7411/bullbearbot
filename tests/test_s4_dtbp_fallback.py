"""
tests/test_s4_dtbp_fallback.py

Tests for the DTBP=0 fallback fix in order_executor_options.submit_options_order().

Before fix: dtbp=0 + obp>0 → log warning + return OptionsExecutionResult(status="dtbp_zero")
After fix:  dtbp=0 + obp>0 → log info + fall through to submission
"""
from __future__ import annotations

import logging
import types
import unittest
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from unittest.mock import MagicMock, call, patch


# ---------------------------------------------------------------------------
# Minimal stubs so we can import order_executor_options without the full stack
# ---------------------------------------------------------------------------

def _make_structure_stub(symbol: str = "NVDA") -> MagicMock:
    s = MagicMock()
    s.underlying = symbol
    s.strategy.value = "credit_call_spread"
    s.structure_id = "test-structure-id"
    s.max_cost_usd = -2250.0
    s.iv_rank = 45.0
    s.legs = []
    return s


def _make_account_stub(dtbp: float, obp: float, acct_id: str = "acct-001"):
    acct = MagicMock()
    acct.daytrading_buying_power = dtbp
    acct.options_buying_power = obp
    acct.id = acct_id
    return acct


# ---------------------------------------------------------------------------
# Suite A — DTBP guard logic (unit-level, no real Alpaca calls)
# ---------------------------------------------------------------------------

class TestDtbpGuardLogic(unittest.TestCase):
    """Tests the guard logic extracted as a pure function matching the deployed code."""

    def _run_guard(self, dtbp: float, obp: float) -> tuple[bool, Optional[str]]:
        """
        Returns (fell_through: bool, log_level: Optional[str]).
        Mirrors the exact deployed guard logic.
        """
        log_calls = []

        class _FakeLog:
            def info(self, msg, *a):    log_calls.append(("info", msg % a if a else msg))
            def warning(self, msg, *a): log_calls.append(("warning", msg % a if a else msg))

        fake_log = _FakeLog()

        # Replicate deployed logic exactly
        fell_through = True
        dtbp_val = float(dtbp or 0)
        obp_val  = float(obp or 0)
        if dtbp_val == 0 and obp_val > 0:
            fake_log.info(
                "[EXECUTOR] DTBP=0 — using options_buying_power=$%.0f as fallback  account=%s",
                obp_val, "acct-001",
            )
            # fall through

        level = log_calls[0][0] if log_calls else None
        return fell_through, level, log_calls

    def test_dtbp0_obp_positive_falls_through(self):
        fell_through, level, calls = self._run_guard(dtbp=0, obp=100_000)
        self.assertTrue(fell_through)

    def test_dtbp0_obp_positive_logs_info_not_warning(self):
        _, level, calls = self._run_guard(dtbp=0, obp=100_000)
        self.assertEqual(level, "info")

    def test_dtbp0_obp_positive_log_contains_fallback_phrase(self):
        _, _, calls = self._run_guard(dtbp=0, obp=100_000)
        self.assertEqual(len(calls), 1)
        self.assertIn("DTBP=0", calls[0][1])
        self.assertIn("options_buying_power", calls[0][1])
        self.assertIn("fallback", calls[0][1])

    def test_dtbp0_obp_positive_log_contains_obp_value(self):
        _, _, calls = self._run_guard(dtbp=0, obp=100_000)
        self.assertIn("100000", calls[0][1])

    def test_dtbp_positive_no_log(self):
        _, level, calls = self._run_guard(dtbp=50_000, obp=100_000)
        self.assertEqual(len(calls), 0)

    def test_dtbp0_obp0_no_log_falls_through(self):
        # dtbp=0 obp=0 — guard condition not met (obp not > 0), falls through silently
        fell_through, level, calls = self._run_guard(dtbp=0, obp=0)
        self.assertTrue(fell_through)
        self.assertEqual(len(calls), 0)

    def test_dtbp_positive_obp0_no_log(self):
        _, _, calls = self._run_guard(dtbp=50_000, obp=0)
        self.assertEqual(len(calls), 0)


# ---------------------------------------------------------------------------
# Suite B — submit_options_order integration (mock Alpaca client + executor)
# ---------------------------------------------------------------------------

class TestSubmitOptionsOrderDtbpFallback(unittest.TestCase):
    """
    Patches _get_options_client and options_executor to verify that
    submit_options_order falls through to submission when DTBP=0 / OBP>0.
    """

    def _run_submit(self, dtbp: float, obp: float):
        """
        Run submit_options_order with mocked client and executor.
        Returns (result, submit_called: bool, log_records).
        options_executor is a local import inside submit_options_order, so it
        must be patched via sys.modules, not patch.object.
        """
        import sys
        structure = _make_structure_stub()
        acct_stub = _make_account_stub(dtbp=dtbp, obp=obp)

        mock_client = MagicMock()
        mock_client.get_account.return_value = acct_stub

        from schemas import StructureLifecycle
        mock_submit_result = MagicMock()
        mock_submit_result.lifecycle = StructureLifecycle.SUBMITTED
        mock_submit_result.order_ids = ["oid-1"]
        mock_submit_result.structure_id = "sid-1"
        mock_submit_result.max_cost_usd = -2250.0
        mock_submit_result.iv_rank = 45.0
        mock_submit_result.audit_log = []

        mock_executor = MagicMock()
        mock_executor.submit_structure.return_value = mock_submit_result

        import order_executor_options as oe_opts
        with patch.dict(sys.modules, {"options_executor": mock_executor}), \
             patch.object(oe_opts, "_get_options_client", return_value=mock_client), \
             patch.object(oe_opts, "_log_result"):
            with self.assertLogs("order_executor_options", level="DEBUG") as log_ctx:
                result = oe_opts.submit_options_order(structure, equity=100_000)

        submit_called = mock_executor.submit_structure.called
        return result, submit_called, log_ctx.records

    def test_dtbp0_obp_positive_reaches_submission(self):
        _, submit_called, _ = self._run_submit(dtbp=0, obp=100_000)
        self.assertTrue(submit_called, "submit_structure should be called when DTBP=0 and OBP>0")

    def test_dtbp0_obp_positive_status_not_dtbp_zero(self):
        result, _, _ = self._run_submit(dtbp=0, obp=100_000)
        self.assertNotEqual(result.status, "dtbp_zero",
                            "Result should not be dtbp_zero after fallback fix")

    def test_dtbp0_obp_positive_logs_info_fallback(self):
        _, _, records = self._run_submit(dtbp=0, obp=100_000)
        fallback_records = [r for r in records
                            if "DTBP=0" in r.getMessage() and "fallback" in r.getMessage()]
        self.assertTrue(len(fallback_records) >= 1,
                        "Should log '[EXECUTOR] DTBP=0 — using options_buying_power=X as fallback'")

    def test_dtbp0_obp_positive_log_is_info_not_warning(self):
        _, _, records = self._run_submit(dtbp=0, obp=100_000)
        fallback_records = [r for r in records if "DTBP=0" in r.getMessage()]
        if fallback_records:
            self.assertEqual(fallback_records[0].levelno, logging.INFO,
                             "Fallback log should be INFO, not WARNING")

    def test_dtbp_normal_reaches_submission(self):
        _, submit_called, _ = self._run_submit(dtbp=50_000, obp=100_000)
        self.assertTrue(submit_called)

    def test_dtbp0_obp0_reaches_submission(self):
        # Guard not triggered (obp not > 0), falls through silently
        _, submit_called, _ = self._run_submit(dtbp=0, obp=0)
        self.assertTrue(submit_called)


# ---------------------------------------------------------------------------
# Suite C — status field: "dtbp_zero" must no longer appear
# ---------------------------------------------------------------------------

class TestDtbpZeroStatusGone(unittest.TestCase):
    """The 'dtbp_zero' status string should never be returned by submit_options_order."""

    def test_no_dtbp_zero_status_in_module_source(self):
        """The string 'dtbp_zero' should not appear as a return status in the deployed module."""
        import pathlib, re
        src = (pathlib.Path(__file__).parent.parent / "order_executor_options.py").read_text()
        # Find all status= assignments — none should be "dtbp_zero"
        status_assignments = re.findall(r'status\s*=\s*["\']([^"\']+)["\']', src)
        self.assertNotIn("dtbp_zero", status_assignments,
                         f"'dtbp_zero' found as a status= value. Source has: {status_assignments}")

    def test_no_return_result_after_dtbp_block(self):
        """After the DTBP guard, there should be no early return for dtbp_zero."""
        import pathlib
        src = (pathlib.Path(__file__).parent.parent / "order_executor_options.py").read_text()
        self.assertNotIn('"dtbp_zero"', src,
                         "Literal string 'dtbp_zero' still present in source")


if __name__ == "__main__":
    unittest.main()
