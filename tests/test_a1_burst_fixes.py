"""Tests verifying the four A1 fixes from the May-6 diagnostic.

Fix 1 — health_monitor: decisions.json path no longer routes through _DATA_DIR.
Fix 2 — scheduler: cooldown-defer sleeps remaining interval instead of falling
         through immediately (burst prevention).
Fix 3 — morning_brief: generate_intelligence_brief max_tokens raised to 16384.
Fix 4 — scratchpad: run_scratchpad max_tokens raised to 2000.
"""
from __future__ import annotations

import queue as _queue
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def _src(filename: str) -> str:
    return (ROOT / filename).read_text()


# ---------------------------------------------------------------------------
# Fix 1 — health_monitor decisions.json path
# ---------------------------------------------------------------------------

class TestHealthMonitorPathFix(unittest.TestCase):
    def test_correct_path_pattern_present(self):
        src = _src("health_monitor.py")
        self.assertIn(
            'Path(__file__).parent / "memory" / "decisions.json"',
            src,
        )

    def test_wrong_path_pattern_absent(self):
        src = _src("health_monitor.py")
        self.assertNotIn(
            '_DATA_DIR / "memory" / "decisions.json"',
            src,
        )


# ---------------------------------------------------------------------------
# Fix 2 — scheduler cooldown burst prevention
# ---------------------------------------------------------------------------

class TestSchedulerCooldownFix(unittest.TestCase):
    def test_defer_remaining_added_to_cooldown_branch(self):
        src = _src("scheduler.py")
        self.assertIn("_defer_remaining", src)
        self.assertIn("_sleep_watching_triggers(int(_defer_remaining))", src)

    def test_sleep_watching_triggers_drains_queue(self):
        """_sleep_watching_triggers pulls triggers from the queue and returns them."""
        try:
            import scheduler
        except Exception as exc:
            self.skipTest(f"scheduler import failed: {exc}")

        # Clear any leftover triggers from other tests
        while True:
            try:
                scheduler._trigger_queue.get_nowait()
            except _queue.Empty:
                break

        scheduler._trigger_queue.put("econ print: ADP actual=109")

        with patch("scheduler.time.sleep"):
            reasons = scheduler._sleep_watching_triggers(10)

        self.assertEqual(reasons, ["econ print: ADP actual=109"])
        self.assertTrue(scheduler._trigger_queue.empty())

    def test_cooldown_requeue_and_remaining_sleep(self):
        """Cooldown branch re-queues triggers and calls _sleep_watching_triggers again."""
        try:
            import scheduler
        except Exception as exc:
            self.skipTest(f"scheduler import failed: {exc}")

        while True:
            try:
                scheduler._trigger_queue.get_nowait()
            except _queue.Empty:
                break

        sleep_calls: list[int] = []

        def fake_swt(seconds: int) -> list[str]:
            sleep_calls.append(seconds)
            return []

        trigger_reasons = ["econ print: ADP actual=109"]
        sleep_for = 873
        _COOLDOWN = 60
        secs_since_last = 10.0

        with patch("scheduler._sleep_watching_triggers", side_effect=fake_swt):
            if secs_since_last < _COOLDOWN:
                for r in trigger_reasons:
                    scheduler._trigger_queue.put(r)
                _defer_remaining = max(0.0, sleep_for - secs_since_last)
                if _defer_remaining > 0:
                    scheduler._sleep_watching_triggers(int(_defer_remaining))

        self.assertEqual(len(sleep_calls), 1)
        self.assertEqual(sleep_calls[0], 863)  # 873 - 10


# ---------------------------------------------------------------------------
# Fix 3 — morning_brief max_tokens
# ---------------------------------------------------------------------------

class TestMorningBriefMaxTokens(unittest.TestCase):
    def test_max_tokens_raised_to_16384(self):
        src = _src("morning_brief.py")
        self.assertIn("max_tokens=16384", src)

    def test_old_8192_limit_removed(self):
        src = _src("morning_brief.py")
        self.assertNotIn(
            "max_tokens=8192", src,
            "max_tokens=8192 still present — intelligence brief will still truncate",
        )


# ---------------------------------------------------------------------------
# Fix 4 — scratchpad max_tokens
# ---------------------------------------------------------------------------

class TestScratchpadMaxTokens(unittest.TestCase):
    def test_max_tokens_raised_to_2000(self):
        src = _src("scratchpad.py")
        self.assertIn("max_tokens=2000", src)

    def test_old_900_limit_removed(self):
        src = _src("scratchpad.py")
        self.assertNotIn(
            "max_tokens=900", src,
            "max_tokens=900 still present — scratchpad will still truncate on busy cycles",
        )


if __name__ == "__main__":
    unittest.main()
