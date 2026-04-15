"""
cost_tracker.py — Real-time Claude API cost monitoring.

Records every API call's token usage and computes cost based on current
Anthropic pricing. Tracks cache efficiency and distinguishes cache reads,
cache writes, regular input, output, and batch discount tokens.

Log file: data/costs/daily_costs.json
Daily reset happens automatically on first call of each new day.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from log_setup import get_logger

log = get_logger(__name__)

_BASE_DIR   = Path(__file__).parent
_COST_DIR   = _BASE_DIR / "data" / "costs"
_DAILY_FILE = _COST_DIR / "daily_costs.json"

# ── Model pricing (per million tokens, April 2026) ────────────────────────────
_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {
        "input":        3.00,
        "output":      15.00,
        "cache_write":  3.75,   # 1.25× input price
        "cache_read":   0.30,   # 0.10× input price
    },
    "claude-haiku-4-5-20251001": {
        "input":        1.00,
        "output":        5.00,
        "cache_write":   1.25,
        "cache_read":    0.10,
    },
    "claude-opus-4-6": {
        "input":       15.00,
        "output":      75.00,
        "cache_write": 18.75,
        "cache_read":   1.50,
    },
}

_DAILY_ALERT_USD   = 5.00    # send SMS if daily spend crosses this
_MONTHLY_ALERT_USD = 100.00  # send SMS if 30-day projection crosses this


class CostTracker:

    def __init__(self) -> None:
        _COST_DIR.mkdir(parents=True, exist_ok=True)
        self._data = self._load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> dict:
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            if _DAILY_FILE.exists():
                d = json.loads(_DAILY_FILE.read_text())
                if d.get("date") != today:
                    # New day — reset daily counters, preserve cumulative totals
                    d["date"]         = today
                    d["daily_cost"]   = 0.0
                    d["daily_calls"]  = 0
                    d["daily_tokens"] = {"input": 0, "output": 0,
                                         "cache_write": 0, "cache_read": 0}
                    d["by_caller"]    = {}
                    d["daily_alert_sent"] = False
                return d
        except Exception:
            pass
        return {
            "date":             today,
            "daily_cost":       0.0,
            "daily_calls":      0,
            "daily_tokens":     {"input": 0, "output": 0,
                                 "cache_write": 0, "cache_read": 0},
            "daily_alert_sent": False,
            "by_caller":        {},
            "all_time_cost":    0.0,
        }

    def _save(self) -> None:
        try:
            _DAILY_FILE.write_text(json.dumps(self._data, indent=2))
        except Exception as exc:
            log.warning("CostTracker: save failed: %s", exc)

    # ── Core recording ─────────────────────────────────────────────────────────

    def record_api_call(
        self,
        model:    str,
        usage,            # anthropic Usage object or dict with token counts
        caller:   str  = "",
        is_batch: bool = False,
    ) -> float:
        """
        Record one Claude API call. Returns the USD cost.

        Accepts the response.usage object directly. Gracefully handles
        missing attributes (e.g., cache tokens absent on older SDK).
        """
        pricing = _PRICING.get(model, _PRICING["claude-sonnet-4-6"])

        # Extract token counts defensively — some may be absent
        def _tok(attr: str) -> int:
            return int(getattr(usage, attr, None) or 0)

        input_tokens       = _tok("input_tokens")
        output_tokens      = _tok("output_tokens")
        cache_write_tokens = _tok("cache_creation_input_tokens")
        cache_read_tokens  = _tok("cache_read_input_tokens")
        regular_input      = max(0, input_tokens - cache_write_tokens - cache_read_tokens)

        cost = (
            regular_input      / 1_000_000 * pricing["input"]
            + cache_write_tokens / 1_000_000 * pricing["cache_write"]
            + cache_read_tokens  / 1_000_000 * pricing["cache_read"]
            + output_tokens      / 1_000_000 * pricing["output"]
        )
        if is_batch:
            cost *= 0.50   # Batch API 50% discount

        # Update daily totals
        self._data["daily_cost"]  = self._data.get("daily_cost",  0.0) + cost
        self._data["daily_calls"] = self._data.get("daily_calls", 0)   + 1

        dt = self._data.setdefault("daily_tokens",
                                   {"input": 0, "output": 0,
                                    "cache_write": 0, "cache_read": 0})
        dt["input"]       += input_tokens
        dt["output"]      += output_tokens
        dt["cache_write"] += cache_write_tokens
        dt["cache_read"]  += cache_read_tokens

        # Update cumulative total
        self._data["all_time_cost"] = self._data.get("all_time_cost", 0.0) + cost

        # Per-caller breakdown
        if caller:
            bc = self._data.setdefault("by_caller", {})
            bc.setdefault(caller, {"cost": 0.0, "calls": 0})
            bc[caller]["cost"]  = round(bc[caller]["cost"] + cost, 6)
            bc[caller]["calls"] += 1

        self._save()

        log.debug(
            "Cost [%s]: in=%d cw=%d cr=%d out=%d → $%.4f  daily=$%.3f%s",
            caller or model[:20],
            regular_input, cache_write_tokens, cache_read_tokens, output_tokens,
            cost, self._data["daily_cost"],
            " [batch]" if is_batch else "",
        )
        return round(cost, 6)

    # ── Reporting ──────────────────────────────────────────────────────────────

    def get_daily_summary(self) -> dict:
        dt = self._data.get("daily_tokens", {})
        total_in = dt.get("input", 0)
        reads    = dt.get("cache_read", 0)
        hit_rate = (reads / total_in * 100.0) if total_in > 0 else 0.0

        return {
            "date":         self._data.get("date"),
            "daily_cost":   round(self._data.get("daily_cost", 0.0), 4),
            "daily_calls":  self._data.get("daily_calls", 0),
            "cache_hit_pct": round(hit_rate, 1),
            "daily_tokens": dt,
            "by_caller":    self._data.get("by_caller", {}),
            "all_time_cost": round(self._data.get("all_time_cost", 0.0), 2),
        }

    def get_monthly_projection(self) -> float:
        """Extrapolate monthly cost from today's daily spend (× 30)."""
        return round(self._data.get("daily_cost", 0.0) * 30, 2)

    def get_cache_efficiency(self) -> dict:
        """Return cache hit rate and estimated daily savings from cache reads."""
        dt    = self._data.get("daily_tokens", {})
        total = dt.get("input", 0)
        reads = dt.get("cache_read", 0)
        p     = _PRICING["claude-sonnet-4-6"]
        if total == 0:
            return {"hit_rate_pct": 0.0, "savings_usd": 0.0}
        savings = reads / 1_000_000 * (p["input"] - p["cache_read"])
        return {
            "hit_rate_pct": round(reads / total * 100.0, 1),
            "savings_usd":  round(savings, 4),
        }

    def should_alert(self) -> Optional[str]:
        """
        Return an alert message string if daily or monthly thresholds are exceeded.
        Returns None if within limits. Each threshold fires only once per day.
        """
        if self._data.get("daily_alert_sent"):
            return None
        daily   = self._data.get("daily_cost", 0.0)
        monthly = self.get_monthly_projection()
        msg = None
        if daily >= _DAILY_ALERT_USD:
            msg = (f"API cost alert: daily=${daily:.2f} "
                   f"(threshold ${_DAILY_ALERT_USD:.2f})")
        elif monthly >= _MONTHLY_ALERT_USD:
            msg = (f"API cost alert: monthly projection=${monthly:.0f} "
                   f"(threshold ${_MONTHLY_ALERT_USD:.0f})")
        if msg:
            self._data["daily_alert_sent"] = True
            self._save()
        return msg

    def format_report_section(self) -> str:
        """One-paragraph cost summary for report.py emails."""
        s = self.get_daily_summary()
        ce = self.get_cache_efficiency()
        proj = self.get_monthly_projection()
        lines = [
            f"  Daily API cost : ${s['daily_cost']:.4f}  ({s['daily_calls']} calls)",
            f"  Cache hit rate : {ce['hit_rate_pct']:.1f}%  saved ${ce['savings_usd']:.4f} today",
            f"  Monthly proj   : ${proj:.2f}  (all-time ${s['all_time_cost']:.2f})",
        ]
        if s.get("by_caller"):
            top = sorted(s["by_caller"].items(),
                         key=lambda x: x[1]["cost"], reverse=True)[:5]
            lines.append("  Top callers    : " +
                         "  ".join(f"{k}=${v['cost']:.4f}" for k, v in top))
        return "\n".join(lines)


# ── Module-level singleton ────────────────────────────────────────────────────

_tracker: Optional[CostTracker] = None


def get_tracker() -> CostTracker:
    global _tracker
    if _tracker is None:
        _tracker = CostTracker()
    return _tracker
