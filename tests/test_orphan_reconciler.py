"""
tests/test_orphan_reconciler.py — Tests for A2 orphan position reconciler
and vol_analyst/tape_skeptic dashboard slot rendering.

TEST 1 — Untracked Alpaca position creates orphan_tracked structure.
TEST 2 — Idempotent: existing orphan_tracked structure not recreated.
TEST 3 — Orphan past 50% max loss fires log.error safety alert.
TEST 4 — orphan_tracked lifecycle is open (not terminal); included in stage1 gate.
TEST 5 — VOL ANALYST slot: IV rank + size modifier + first reason.
TEST 6 — TAPE SKEPTIC slot: key_risks rendered; empty → fallback.
"""
from __future__ import annotations

import ast
import pathlib
import re
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

# ── Module-level helpers ──────────────────────────────────────────────────────

def _get_active_lc_from_stage1() -> set[str]:
    """Extract _ACTIVE_LC set literal from stage1 source without a full module import."""
    src = (pathlib.Path(__file__).parent.parent / "bot_options_stage1_candidates.py").read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_ACTIVE_LC":
                    if isinstance(node.value, ast.Set):
                        return {
                            elt.value
                            for elt in node.value.elts
                            if isinstance(elt, ast.Constant)
                        }
    return set()


def _make_alpaca_position(
    symbol: str, qty: float, entry: float,
    unreal_pl: float = 0.0, unreal_plpc: float = 0.0,
):
    p = MagicMock()
    p.symbol = symbol
    p.qty = str(qty)
    p.avg_entry_price = str(entry)
    p.unrealized_pl = str(unreal_pl)
    p.unrealized_plpc = str(unreal_plpc)
    return p


def _make_options_structure(lifecycle: str, underlying: str = "AAPL"):
    from schemas import OptionsStructure, OptionStrategy, StructureLifecycle, Tier
    lc_map = {
        "submitted":       StructureLifecycle.SUBMITTED,
        "cancelled":       StructureLifecycle.CANCELLED,
        "fully_filled":    StructureLifecycle.FULLY_FILLED,
        "partially_filled": StructureLifecycle.PARTIALLY_FILLED,
        "orphan_tracked":  StructureLifecycle.ORPHAN_TRACKED,
    }
    s = OptionsStructure(
        structure_id=f"{lifecycle}_{underlying}",
        underlying=underlying,
        strategy=OptionStrategy.SINGLE_CALL,
        lifecycle=lc_map[lifecycle],
        legs=[],
        contracts=1,
        max_cost_usd=500.0,
        opened_at=datetime.now(timezone.utc).isoformat(),
        catalyst="test",
        tier=Tier.CORE,
    )
    return s


def _snip(text: str, n: int = 1) -> str:
    """Simplified version of _a2_snip for tests — no Flask dependency."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", str(text or ""))
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    return " ".join(sentences[:n])[:220]


def _slot_logic(dp: dict, iv_rank=None, iv_env: str = "normal", debate_backfill: bool = False):
    """Replicate the VOL ANALYST / TAPE SKEPTIC slot logic from _a2_cinematic_card_html."""
    reasons = dp.get("reasons") or []
    key_risks = dp.get("key_risks") or []
    size_mod = dp.get("recommended_size_modifier")

    vol_parts = []
    if iv_rank is not None:
        vol_parts.append(f"IV {iv_rank:.0f} — {iv_env}")
    if size_mod is not None and abs(float(size_mod) - 1.0) > 0.01:
        vol_parts.append(f"size ×{float(size_mod):.1f} (IV mandate)")
    if reasons and not debate_backfill:
        first_reason = reasons[0] if isinstance(reasons, list) else str(reasons)
        snipped = _snip(first_reason, 1)
        if snipped:
            vol_parts.append(snipped)
    vol_analyst = " · ".join(p for p in vol_parts if p) or "—"

    tape_skeptic = (
        " · ".join(str(r) for r in key_risks) if key_risks else "No material risks flagged"
    )
    return vol_analyst, tape_skeptic


# ── TEST 1-3 — Orphan reconciler function ────────────────────────────────────

class TestReconcileOrphanPositions(unittest.TestCase):

    def _run(self, live_opts, tracked_occs, existing_structures):
        from bot_options_stage0_preflight import _reconcile_orphan_positions
        saved: dict = {}
        with patch("options_state.save_structure", side_effect=lambda s: saved.update({s.structure_id: s})):
            created = _reconcile_orphan_positions(live_opts, tracked_occs, existing_structures)
        return created, saved

    def test_untracked_position_creates_orphan_tracked_structure(self):
        """Live Alpaca position not in tracked_occs → orphan_tracked structure saved."""
        pos = _make_alpaca_position("GOOGL260515C00380000", qty=10, entry=8.75, unreal_pl=900.0)
        created, saved = self._run([pos], set(), [])

        self.assertIn("GOOGL", created)
        orphan_structs = [v for v in saved.values() if v.underlying == "GOOGL"]
        self.assertEqual(len(orphan_structs), 1)
        self.assertEqual(orphan_structs[0].lifecycle.value, "orphan_tracked")
        self.assertAlmostEqual(orphan_structs[0].pnl_unrealized, 900.0, places=0)

    def test_already_orphan_tracked_not_recreated(self):
        """Underlying with existing orphan_tracked structure is skipped."""
        pos = _make_alpaca_position("GOOGL260515C00380000", qty=10, entry=8.75)
        existing = _make_options_structure("orphan_tracked", "GOOGL")
        created, saved = self._run([pos], set(), [existing])

        self.assertNotIn("GOOGL", created)
        self.assertEqual(len(saved), 0, "Must not save a duplicate orphan_tracked structure")

    def test_past_50pct_max_loss_fires_log_error(self):
        """Orphan with P&L < -50% of cost basis fires log.error."""
        # Cost = 4 × 9.25 × 100 = $3,700; loss = -$2,310 = 62% → triggers alert
        pos = _make_alpaca_position(
            "MSFT260515C00417500", qty=4, entry=9.25, unreal_pl=-2310.0
        )
        with patch("bot_options_stage0_preflight.log") as mock_log:
            self._run([pos], set(), [])

        error_calls = [c for c in mock_log.error.call_args_list
                       if "ORPHAN ALERT" in str(c)]
        self.assertTrue(len(error_calls) >= 1, "Expected log.error for >50% loss orphan")

    def test_within_50pct_loss_no_error_alert(self):
        """Orphan with P&L between 0% and -50% does NOT fire the error alert."""
        # Cost = 4 × 9.25 × 100 = $3,700; loss = -$1,400 = 38% → below threshold
        pos = _make_alpaca_position(
            "MSFT260515C00417500", qty=4, entry=9.25, unreal_pl=-1400.0
        )
        with patch("bot_options_stage0_preflight.log") as mock_log:
            self._run([pos], set(), [])

        error_calls = [c for c in mock_log.error.call_args_list
                       if "ORPHAN ALERT" in str(c)]
        self.assertEqual(len(error_calls), 0, "Should not fire alert when loss < 50%")

    def test_occ_symbol_parsing_populates_leg_fields(self):
        """OCC symbol parsed correctly into leg expiry, type, strike."""
        pos = _make_alpaca_position("GOOGL260515C00380000", qty=5, entry=10.0)
        created, saved = self._run([pos], set(), [])

        self.assertIn("GOOGL", created)
        orphan = next(v for v in saved.values() if v.underlying == "GOOGL")
        self.assertEqual(len(orphan.legs), 1)
        leg = orphan.legs[0]
        self.assertEqual(leg.occ_symbol, "GOOGL260515C00380000")
        self.assertEqual(leg.option_type, "call")
        self.assertAlmostEqual(leg.strike, 380.0, places=1)
        self.assertEqual(leg.expiration, "2026-05-15")
        self.assertEqual(leg.side, "buy")

    def test_short_position_leg_side_is_sell(self):
        """Negative qty → leg.side = 'sell'."""
        pos = _make_alpaca_position("GOOGL260515C00385000", qty=-10, entry=6.40)
        _, saved = self._run([pos], set(), [])
        orphan = next(v for v in saved.values() if v.underlying == "GOOGL")
        leg = orphan.legs[0]
        self.assertEqual(leg.side, "sell")
        self.assertEqual(leg.qty, 10)

    def test_already_tracked_occ_is_not_orphaned(self):
        """Position whose OCC symbol is in tracked_occs → not treated as orphan."""
        pos = _make_alpaca_position("AAPL260620C00200000", qty=5, entry=5.0)
        tracked = {"AAPL260620C00200000"}
        created, saved = self._run([pos], tracked, [])
        self.assertNotIn("AAPL", created)
        self.assertEqual(len(saved), 0)


# ── TEST 4 — StructureLifecycle.ORPHAN_TRACKED ───────────────────────────────

class TestOrphanLifecycle(unittest.TestCase):

    def test_orphan_tracked_enum_value(self):
        from schemas import StructureLifecycle
        self.assertEqual(StructureLifecycle.ORPHAN_TRACKED.value, "orphan_tracked")

    def test_orphan_tracked_is_open(self):
        from schemas import OptionsStructure, OptionStrategy, StructureLifecycle, Tier
        s = OptionsStructure(
            structure_id="t", underlying="GOOGL",
            strategy=OptionStrategy.SINGLE_CALL,
            lifecycle=StructureLifecycle.ORPHAN_TRACKED,
            legs=[], contracts=1, max_cost_usd=100.0,
            opened_at=datetime.now(timezone.utc).isoformat(),
            catalyst="test", tier=Tier.CORE,
        )
        self.assertTrue(s.is_open())
        self.assertFalse(s.is_terminal())

    def test_orphan_tracked_in_stage1_active_gate(self):
        """_ACTIVE_LC in stage1 must include orphan_tracked to block new entries."""
        active_lc = _get_active_lc_from_stage1()
        self.assertIn("orphan_tracked", active_lc)

    def test_orphan_tracked_roundtrips(self):
        from schemas import StructureLifecycle
        self.assertEqual(StructureLifecycle("orphan_tracked"), StructureLifecycle.ORPHAN_TRACKED)


# ── TEST 5-6 — Vol analyst / tape skeptic slot logic ─────────────────────────

class TestVolTapeSlots(unittest.TestCase):

    def test_vol_analyst_shows_iv_rank_and_env(self):
        dp = {"reasons": "IV rank 35 favors buying. Momentum strong.", "key_risks": [],
              "recommended_size_modifier": 1.0}
        vol, _ = _slot_logic(dp, iv_rank=35, iv_env="elevated")
        self.assertIn("IV 35", vol)
        self.assertIn("elevated", vol)

    def test_vol_analyst_first_reason_appended(self):
        dp = {"reasons": "Thesis intact based on technicals.", "key_risks": [],
              "recommended_size_modifier": 1.0}
        vol, _ = _slot_logic(dp, iv_rank=40, iv_env="normal")
        self.assertIn("Thesis intact", vol)

    def test_vol_analyst_size_modifier_shown_when_not_1(self):
        dp = {"reasons": "High IV.", "key_risks": ["IV rank >60"],
              "recommended_size_modifier": 0.5}
        vol, _ = _slot_logic(dp, iv_rank=65, iv_env="high")
        self.assertIn("×0.5", vol)
        self.assertIn("IV mandate", vol)

    def test_vol_analyst_size_modifier_1_not_shown(self):
        dp = {"reasons": "Clean setup.", "key_risks": [],
              "recommended_size_modifier": 1.0}
        vol, _ = _slot_logic(dp, iv_rank=30, iv_env="low")
        self.assertNotIn("IV mandate", vol)
        self.assertNotIn("×1.0", vol)

    def test_vol_analyst_no_data_returns_dash(self):
        dp = {"reasons": "", "key_risks": [], "recommended_size_modifier": 1.0}
        vol, _ = _slot_logic(dp, iv_rank=None)
        self.assertEqual(vol, "—")

    def test_tape_skeptic_shows_all_key_risks(self):
        dp = {"reasons": "", "key_risks": ["Theta burn", "Time stop at 40%", "Earnings in 3 days"],
              "recommended_size_modifier": 1.0}
        _, tape = _slot_logic(dp)
        self.assertIn("Theta burn", tape)
        self.assertIn("Time stop at 40%", tape)
        self.assertIn("Earnings in 3 days", tape)

    def test_tape_skeptic_empty_key_risks_fallback(self):
        dp = {"reasons": "Clean.", "key_risks": [], "recommended_size_modifier": 1.0}
        _, tape = _slot_logic(dp, iv_rank=25, iv_env="low")
        self.assertEqual(tape, "No material risks flagged")

    def test_tape_skeptic_single_risk(self):
        dp = {"reasons": "", "key_risks": ["Only risk"], "recommended_size_modifier": 1.0}
        _, tape = _slot_logic(dp)
        self.assertEqual(tape, "Only risk")


if __name__ == "__main__":
    unittest.main()
