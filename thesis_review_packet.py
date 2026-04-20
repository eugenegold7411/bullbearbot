"""
thesis_review_packet.py — Weekly Thesis Lab status packet for weekly review (TL-3).

Generates a markdown summary of active theses, trajectory signals, checkpoint
due dates, and recent changes. Consumed by CTO (Agent 5) and Strategy Director
(Agent 6) during the weekly review.

Ring 2 only — advisory shadow, never touches live execution.
Weekly cadence only — not called from the 5-minute cycle.
Gated behind enable_thesis_weekly_packet feature flag.

Zero imports from: bot.py, order_executor.py, risk_kernel.py
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────

_THESIS_LAB_DIR = Path(__file__).parent / "data" / "thesis_lab"
_PACKETS_DIR    = _THESIS_LAB_DIR / "packets"

# ─────────────────────────────────────────────────────────────────────────────
# Feature flag
# ─────────────────────────────────────────────────────────────────────────────

def _is_enabled() -> bool:
    try:
        from feature_flags import is_enabled
        return is_enabled("enable_thesis_weekly_packet", default=False)
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Backtest helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_latest_backtest(thesis_id: str) -> Optional[dict]:
    """Return the most recent backtest record dict for thesis_id, or None."""
    try:
        from thesis_backtest import load_backtest_results
        recs = load_backtest_results(thesis_id=thesis_id)
        return recs[-1] if recs else None
    except Exception as exc:
        log.debug("[THESIS_PACKET] backtest load failed for %s: %s", thesis_id, exc)
        return None


def _classify_trajectory(bt: Optional[dict]) -> str:
    """
    Classify a thesis as 'strengthening', 'weakening', or 'neutral' based on
    its most recent backtest result dict.

    strengthening: final_verdict == "profitable"
    weakening:     final_verdict == "loss"
    neutral:       pending / inconclusive / no data
    """
    if not bt:
        return "neutral"
    verdict = bt.get("final_verdict", "pending")
    if verdict == "profitable":
        return "strengthening"
    if verdict == "loss":
        return "weakening"
    return "neutral"


# ─────────────────────────────────────────────────────────────────────────────
# Section builders
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_roi(bt: Optional[dict]) -> str:
    """Format the most recent available ROI from a backtest dict as a percent string."""
    if not bt:
        return "—"
    for field in ("roi_12m", "roi_9m", "roi_6m", "roi_3m"):
        val = bt.get(field)
        if val is not None:
            return f"{val:+.1%}"
    return "—"


def _build_active_section(trackable: list, backtest_map: dict) -> list[str]:
    lines = ["## 1. Active Theses", ""]
    if not trackable:
        lines.append("*No active theses.*")
        lines.append("")
        return lines

    lines.append("| Title | Status | Opened | Symbols | Verdict | ROI |")
    lines.append("|-------|--------|--------|---------|---------|-----|")
    for t in trackable:
        syms    = ", ".join((t.base_expression or {}).get("symbols", [])[:2])
        bt      = backtest_map.get(t.thesis_id)
        verdict = (bt.get("final_verdict", "—") if bt else "—")
        roi     = _fmt_roi(bt)
        title   = t.title[:38] if len(t.title) > 38 else t.title
        lines.append(
            f"| {title} | {t.status} | {t.date_opened} | {syms} | {verdict} | {roi} |"
        )
    lines.append("")
    return lines


def _build_strengthening_section(items: list) -> list[str]:
    lines = ["## 2. Strengthening Theses", ""]
    if not items:
        lines.append("*No clearly strengthening theses this week.*")
    else:
        for t, bt in items:
            lines.append(
                f"- **{t.title}** — verdict: {bt.get('final_verdict', '?') if bt else '?'}, "
                f"latest ROI: {_fmt_roi(bt)}"
            )
    lines.append("")
    return lines


def _build_weakening_section(items: list) -> list[str]:
    lines = ["## 3. Weakening Theses", ""]
    if not items:
        lines.append("*No clearly weakening theses this week.*")
    else:
        for t, bt in items:
            lines.append(
                f"- **{t.title}** — verdict: {bt.get('final_verdict', '?') if bt else '?'}, "
                f"latest ROI: {_fmt_roi(bt)}"
            )
    lines.append("")
    return lines


def _build_due_section(trackable: list, today_str: str) -> list[str]:
    lines = ["## 4. Due for Checkpoint Review", ""]
    due = [
        t for t in trackable
        if any(d <= today_str for d in (t.review_schedule or []))
    ]
    if not due:
        lines.append("*No theses currently past checkpoint review date.*")
    else:
        for t in due:
            past_dates = [d for d in (t.review_schedule or []) if d <= today_str]
            next_due   = max(past_dates) if past_dates else "?"
            lines.append(
                f"- **{t.title}** (`{t.thesis_id[-12:]}`) — schedule date passed: {next_due}"
            )
    lines.append("")
    return lines


def _build_invalidated_section(all_theses: list, week_ago: str) -> list[str]:
    lines = ["## 5. Recently Invalidated (last 7 days)", ""]
    recently = []
    for t in all_theses:
        if t.status != "invalidated":
            continue
        notes = t.notes or ""
        recent_dates = re.findall(r'\[(\d{4}-\d{2}-\d{2})\]', notes)
        if any(d >= week_ago for d in recent_dates) or t.date_opened >= week_ago:
            recently.append(t)
    if not recently:
        lines.append("*No theses invalidated in the last 7 days.*")
    else:
        for t in recently:
            lines.append(f"- **{t.title}** — invalidated")
    lines.append("")
    return lines


def _build_proposed_section(week_ago: str) -> list[str]:
    lines = ["## 6. Proposed New Theses (this week)", ""]
    try:
        from thesis_registry import list_theses
        proposed = [t for t in list_theses(status="proposed") if t.date_opened >= week_ago]
    except Exception:
        proposed = []
    if not proposed:
        lines.append("*No new thesis proposals this week.*")
    else:
        for t in proposed:
            syms = ", ".join((t.base_expression or {}).get("symbols", [])[:2])
            lines.append(
                f"- **{t.title}** (`{t.thesis_id[-12:]}`) — opened: {t.date_opened}"
                + (f" | {syms}" if syms else "")
            )
    lines.append("")
    return lines


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def build_weekly_thesis_packet() -> str:
    """
    Build a human- and agent-readable weekly thesis packet.
    Returns a markdown string.

    Sections:
    1. Active Theses — title, status, date_opened, latest backtest
    2. Strengthening Theses — final_verdict == "profitable"
    3. Weakening Theses — final_verdict == "loss"
    4. Due for Checkpoint Review — review_schedule date <= today
    5. Recently Invalidated — moved to invalidated this week
    6. Proposed New Theses — from thesis_research within last 7 days

    Non-fatal: returns an error notice string on any internal failure.
    """
    today     = date.today()
    today_str = today.isoformat()
    week_ago  = (today - timedelta(days=7)).isoformat()

    try:
        from thesis_registry import list_theses
    except Exception as exc:
        log.warning("[THESIS_PACKET] thesis_registry unavailable: %s", exc)
        return (
            f"# Thesis Lab Weekly Packet — {today_str}\n\n"
            f"*(thesis_registry unavailable: {exc})*\n"
        )

    # Collect trackable theses
    trackable_statuses = (
        "researched",
        "active_tracking",
        "checkpoint_3m_complete",
        "checkpoint_6m_complete",
        "checkpoint_9m_complete",
    )
    trackable: list = []
    for s in trackable_statuses:
        try:
            trackable.extend(list_theses(status=s))
        except Exception as exc:
            log.warning("[THESIS_PACKET] list_theses(%s) failed: %s", s, exc)

    # Collect invalidated theses for section 5
    try:
        all_invalidated = list_theses(status="invalidated")
    except Exception:
        all_invalidated = []

    # Load backtests for all trackable theses
    backtest_map: dict[str, dict] = {}
    for t in trackable:
        bt = _load_latest_backtest(t.thesis_id)
        if bt:
            backtest_map[t.thesis_id] = bt

    # Classify trajectories
    strengthening: list = []
    weakening:     list = []
    for t in trackable:
        bt   = backtest_map.get(t.thesis_id)
        traj = _classify_trajectory(bt)
        if traj == "strengthening":
            strengthening.append((t, bt))
        elif traj == "weakening":
            weakening.append((t, bt))

    # Assemble header
    lines: list[str] = [
        f"# Thesis Lab Weekly Packet — {today_str}",
        "",
        f"*Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}*  "
        f"Active: {len(trackable)}  "
        f"Strengthening: {len(strengthening)}  "
        f"Weakening: {len(weakening)}",
        "",
        "---",
        "",
    ]

    lines += _build_active_section(trackable, backtest_map)
    lines += _build_strengthening_section(strengthening)
    lines += _build_weakening_section(weakening)
    lines += _build_due_section(trackable, today_str)
    lines += _build_invalidated_section(all_invalidated, week_ago)
    lines += _build_proposed_section(week_ago)

    return "\n".join(lines)


def save_packet(packet: str) -> str:
    """
    Save the thesis packet to data/thesis_lab/packets/weekly_thesis_packet_YYYY-MM-DD.md.
    Returns the file path string.
    Raises OSError on write failure.
    """
    today_str = date.today().isoformat()
    _PACKETS_DIR.mkdir(parents=True, exist_ok=True)
    filename  = f"weekly_thesis_packet_{today_str}.md"
    path      = _PACKETS_DIR / filename
    path.write_text(packet, encoding="utf-8")
    log.info("[THESIS_PACKET] Saved to %s", path)
    return str(path)
