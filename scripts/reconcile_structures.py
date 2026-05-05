"""
reconcile_structures.py — Emergency A2 structures.json reconstruction.

Fetches all current A2 Alpaca positions, groups spread legs, and builds
minimal valid OptionsStructure dicts that load_structures() accepts.

Output: data/account2/positions/structures_RECONSTRUCTED.json
Does NOT touch the live structures.json — caller must review and swap.

Usage:
    cd /home/trading-bot && .venv/bin/python scripts/reconcile_structures.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ── Bootstrap path so we can import project modules ───────────────────────────

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_ROOT / ".env")


# ── OCC symbol parser ─────────────────────────────────────────────────────────

def _parse_occ(occ: str) -> dict:
    """
    Parse an Alpaca OCC symbol into its components.

    Format: {UNDERLYING}{YYMMDD}{C|P}{8-digit strike×1000}
    e.g.  GOOGL260515C00380000 → underlying=GOOGL, expiry=2026-05-15,
          type=call, strike=380.0
    """
    m = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", occ)
    if not m:
        raise ValueError(f"Cannot parse OCC symbol: {occ!r}")
    underlying = m.group(1)
    ymd = m.group(2)
    year = 2000 + int(ymd[:2])
    month = int(ymd[2:4])
    day = int(ymd[4:6])
    expiration = f"{year:04d}-{month:02d}-{day:02d}"
    option_type = "call" if m.group(3) == "C" else "put"
    strike = int(m.group(4)) / 1000
    return {
        "underlying": underlying,
        "expiration": expiration,
        "option_type": option_type,
        "strike": strike,
    }


def _is_options_position(symbol: str) -> bool:
    """True if the symbol looks like an OCC options contract (length > 10)."""
    return len(symbol) > 10 and re.search(r"[CP]\d{8}$", symbol) is not None


# ── Alpaca connection ─────────────────────────────────────────────────────────

def _build_client():
    from alpaca.trading.client import TradingClient  # noqa: PLC0415
    api_key = os.environ.get("ALPACA_API_KEY_OPTIONS")
    secret  = os.environ.get("ALPACA_SECRET_KEY_OPTIONS")
    base    = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    if not api_key or not secret:
        raise EnvironmentError(
            "ALPACA_API_KEY_OPTIONS / ALPACA_SECRET_KEY_OPTIONS not set in .env"
        )
    return TradingClient(api_key=api_key, secret_key=secret, paper=("paper" in base))


# ── Spread grouping ────────────────────────────────────────────────────────────

def _group_positions(positions: list) -> list[dict]:
    """
    Group Alpaca positions into logical structures.

    Matching rule: two positions with the same underlying + expiry + option_type,
    one long and one short of equal absolute quantity, are one spread structure.
    Everything else becomes a single-leg structure.

    Returns a list of structure dicts (not yet OptionsStructure objects).
    """
    # Parse and tag every position
    parsed = []
    for pos in positions:
        occ = str(pos.symbol)
        if not _is_options_position(occ):
            print(f"  SKIP (not options): {occ}")
            continue
        try:
            info = _parse_occ(occ)
        except ValueError as exc:
            print(f"  SKIP (parse error): {occ} — {exc}")
            continue
        # Alpaca returns a PositionSide enum; .value gives "long"/"short"
        raw_side = getattr(pos, "side", "long")
        side = (getattr(raw_side, "value", str(raw_side))).lower()
        qty  = abs(int(float(getattr(pos, "qty", 1))))
        parsed.append({
            "occ":          occ,
            "underlying":   info["underlying"],
            "expiration":   info["expiration"],
            "option_type":  info["option_type"],
            "strike":       info["strike"],
            "side":         side,           # "long" | "short" (Alpaca position side)
            "leg_side":     "buy" if side == "long" else "sell",
            "qty":          qty,
            "avg_entry":    float(getattr(pos, "avg_entry_price", 0) or 0),
            "current_price": float(getattr(pos, "current_price", 0) or 0),
            "unrealized_pl": float(getattr(pos, "unrealized_pl", 0) or 0),
        })

    # Build grouping key: (underlying, expiration, option_type)
    # Collect by group key
    from collections import defaultdict  # noqa: PLC0415
    by_group: dict[tuple, list] = defaultdict(list)
    for p in parsed:
        key = (p["underlying"], p["expiration"], p["option_type"])
        by_group[key].append(p)

    structures = []

    for group_key, legs in by_group.items():
        underlying, expiration, option_type = group_key
        longs  = [l for l in legs if l["side"] == "long"]
        shorts = [l for l in legs if l["side"] == "short"]

        # ── Spread: exactly 1 long + 1 short of the same qty ────────────────
        if len(longs) == 1 and len(shorts) == 1 and longs[0]["qty"] == shorts[0]["qty"]:
            long_leg  = longs[0]
            short_leg = shorts[0]
            contracts = long_leg["qty"]

            # Determine spread type:
            # call spread: long lower + short higher → CALL_DEBIT_SPREAD
            # put spread:  long higher + short lower → PUT_DEBIT_SPREAD
            if option_type == "call":
                if long_leg["strike"] < short_leg["strike"]:
                    strategy = "call_debit_spread"
                    direction = "bullish"
                else:
                    strategy = "call_credit_spread"
                    direction = "bearish"
            else:  # put
                if long_leg["strike"] > short_leg["strike"]:
                    strategy = "put_debit_spread"
                    direction = "bearish"
                else:
                    strategy = "put_credit_spread"
                    direction = "bullish"

            net_debit_per_share = long_leg["avg_entry"] - short_leg["avg_entry"]
            max_cost_usd = max(0.0, net_debit_per_share) * contracts * 100

            # Max profit for debit spreads: (short_strike - long_strike - net_debit) * 100
            if "debit" in strategy and option_type == "call":
                width = abs(short_leg["strike"] - long_leg["strike"])
                max_profit_usd = max(0.0, width - abs(net_debit_per_share)) * contracts * 100
            elif "debit" in strategy and option_type == "put":
                width = abs(long_leg["strike"] - short_leg["strike"])
                max_profit_usd = max(0.0, width - abs(net_debit_per_share)) * contracts * 100
            else:
                max_profit_usd = max(0.0, abs(net_debit_per_share)) * contracts * 100

            net_pnl = long_leg["unrealized_pl"] + short_leg["unrealized_pl"]

            structure = {
                "structure_id": str(uuid.uuid4()),
                "underlying":   underlying,
                "strategy":     strategy,
                "lifecycle":    "fully_filled",
                "direction":    direction,
                "expiration":   expiration,
                "contracts":    contracts,
                "max_cost_usd": round(max_cost_usd, 2),
                "max_profit_usd": round(max_profit_usd, 2),
                "long_strike":  long_leg["strike"],
                "short_strike": short_leg["strike"],
                "debit_paid":   round(net_debit_per_share, 4),
                "iv_rank":      None,
                "opened_at":    datetime.now(timezone.utc).isoformat(),
                "catalyst":     "reconstructed",
                "tier":         "core",
                "order_ids":    [],
                "audit_log":    [{"ts": datetime.now(timezone.utc).isoformat(),
                                  "msg": "reconstructed from Alpaca live positions by reconcile_structures.py"}],
                "pnl_unrealized": round(net_pnl, 2),
                "debate":       {},
                "thesis_status": "intact",
                "legs": [
                    {
                        "occ_symbol":  long_leg["occ"],
                        "underlying":  underlying,
                        "side":        "buy",
                        "qty":         contracts,
                        "option_type": option_type,
                        "strike":      long_leg["strike"],
                        "expiration":  expiration,
                        "filled_price": long_leg["avg_entry"],
                        "order_id":    None,
                    },
                    {
                        "occ_symbol":  short_leg["occ"],
                        "underlying":  underlying,
                        "side":        "sell",
                        "qty":         contracts,
                        "option_type": option_type,
                        "strike":      short_leg["strike"],
                        "expiration":  expiration,
                        "filled_price": short_leg["avg_entry"],
                        "order_id":    None,
                    },
                ],
            }
            structures.append(structure)
            print(f"  SPREAD   {underlying} {option_type.upper()} "
                  f"{long_leg['strike']}/{short_leg['strike']} exp={expiration} "
                  f"qty={contracts}  strategy={strategy}  "
                  f"net_pnl=${net_pnl:+.2f}")

        else:
            # ── Single legs (longs or shorts without a matching counterpart) ─
            for leg in legs:
                contracts = leg["qty"]

                if leg["side"] == "long":
                    strategy = "single_call" if option_type == "call" else "single_put"
                    direction = "bullish" if option_type == "call" else "bearish"
                    max_cost_usd = leg["avg_entry"] * contracts * 100
                    max_profit_usd = None  # unlimited for single calls
                else:
                    # Short option without a matching long — treat as short_put or note
                    strategy = "short_put" if option_type == "put" else "single_call"
                    direction = "neutral"
                    # Credit received = entry price
                    max_cost_usd = leg["avg_entry"] * contracts * 100
                    max_profit_usd = leg["avg_entry"] * contracts * 100

                structure = {
                    "structure_id": str(uuid.uuid4()),
                    "underlying":   underlying,
                    "strategy":     strategy,
                    "lifecycle":    "fully_filled",
                    "direction":    direction,
                    "expiration":   expiration,
                    "contracts":    contracts,
                    "max_cost_usd": round(max_cost_usd, 2),
                    "max_profit_usd": round(max_profit_usd, 2) if max_profit_usd is not None else None,
                    "long_strike":  leg["strike"] if leg["side"] == "long" else None,
                    "short_strike": leg["strike"] if leg["side"] == "short" else None,
                    "debit_paid":   leg["avg_entry"] if leg["side"] == "long" else None,
                    "iv_rank":      None,
                    "opened_at":    datetime.now(timezone.utc).isoformat(),
                    "catalyst":     "reconstructed",
                    "tier":         "core",
                    "order_ids":    [],
                    "audit_log":    [{"ts": datetime.now(timezone.utc).isoformat(),
                                      "msg": "reconstructed from Alpaca live positions by reconcile_structures.py"}],
                    "pnl_unrealized": round(leg["unrealized_pl"], 2),
                    "debate":       {},
                    "thesis_status": "intact",
                    "legs": [
                        {
                            "occ_symbol":  leg["occ"],
                            "underlying":  underlying,
                            "side":        leg["leg_side"],
                            "qty":         contracts,
                            "option_type": option_type,
                            "strike":      leg["strike"],
                            "expiration":  expiration,
                            "filled_price": leg["avg_entry"],
                            "order_id":    None,
                        }
                    ],
                }
                structures.append(structure)
                print(f"  SINGLE   {underlying} {leg['side'].upper()} "
                      f"{option_type.upper()} {leg['strike']} exp={expiration} "
                      f"qty={contracts}  strategy={strategy}  "
                      f"net_pnl=${leg['unrealized_pl']:+.2f}")

    return structures


# ── Validate round-trip via OptionsStructure.from_dict ───────────────────────

def _validate_structures(structs: list[dict]) -> bool:
    """Run each dict through OptionsStructure.from_dict() to catch schema issues."""
    from schemas import OptionsStructure  # noqa: PLC0415
    errors = 0
    for i, s in enumerate(structs):
        try:
            obj = OptionsStructure.from_dict(s)
            assert obj.is_open(), f"structure {obj.structure_id} is_open()=False"
        except Exception as exc:
            print(f"  VALIDATION ERROR on entry {i} ({s.get('underlying', '?')}): {exc}")
            errors += 1
    return errors == 0


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    out_path = _ROOT / "data" / "account2" / "positions" / "structures_RECONSTRUCTED.json"
    live_path = _ROOT / "data" / "account2" / "positions" / "structures.json"

    print("=" * 60)
    print("A2 Structures Reconciliation")
    print("=" * 60)

    # Show current state
    current_text = live_path.read_text(encoding="utf-8").strip() if live_path.exists() else "[]"
    print(f"\n[1] Current structures.json (first 200 chars):")
    print(f"    {current_text[:200]}")

    # Fetch Alpaca positions
    print("\n[2] Fetching A2 Alpaca positions...")
    try:
        client = _build_client()
        all_positions = client.get_all_positions()
    except Exception as exc:
        print(f"  ERROR fetching positions: {exc}")
        sys.exit(1)

    opts_positions = [p for p in all_positions if _is_options_position(str(p.symbol))]
    equity_positions = [p for p in all_positions if not _is_options_position(str(p.symbol))]

    print(f"  Total positions: {len(all_positions)}")
    print(f"  Options positions: {len(opts_positions)}")
    print(f"  Equity positions (ignored): {len(equity_positions)}")

    if not opts_positions:
        print("\n  No options positions found — writing empty structures.json")
        out_path.write_text("[]", encoding="utf-8")
        print(f"  Written: {out_path}")
        return

    print("\n  Raw options positions from Alpaca:")
    for p in opts_positions:
        side = getattr(p, "side", "?")
        qty  = getattr(p, "qty", "?")
        entry = getattr(p, "avg_entry_price", "?")
        curr  = getattr(p, "current_price", "?")
        pnl   = getattr(p, "unrealized_pl", "?")
        print(f"    {p.symbol}  side={side} qty={qty} entry={entry} "
              f"current={curr} unreal_pnl={pnl}")

    # Group into structures
    print("\n[3] Grouping positions into structures...")
    structures = _group_positions(opts_positions)
    print(f"\n  Built {len(structures)} structures from {len(opts_positions)} leg positions")

    # Validate round-trip
    print("\n[4] Validating schema round-trip...")
    if not _validate_structures(structures):
        print("  VALIDATION FAILED — do not swap to live file")
        sys.exit(1)
    print(f"  All {len(structures)} structures validated OK")

    # Write output
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(structures, indent=2, default=str),
        encoding="utf-8",
    )

    print(f"\n[5] Written to: {out_path}")
    print("\n[6] Summary:")
    for s in structures:
        underlying = s["underlying"]
        strategy   = s["strategy"]
        expiry     = s["expiration"]
        contracts  = s["contracts"]
        pnl        = s.get("pnl_unrealized", 0) or 0
        ls = s.get("long_strike")
        ss = s.get("short_strike")
        strike_str = f"{ls}/{ss}" if ss else str(ls or s["legs"][0]["strike"])
        print(f"  {underlying:6s} {strategy:22s} {expiry}  {contracts}ct  "
              f"strike={strike_str}  P&L=${pnl:+.2f}")

    print(f"\nTotal unrealized P&L: "
          f"${sum(s.get('pnl_unrealized', 0) or 0 for s in structures):+.2f}")
    print(f"\nNOTE: File written to structures_RECONSTRUCTED.json — NOT the live file.")
    print("      Review the output above, then run the Phase 3 validation step to swap.")
    print("=" * 60)


if __name__ == "__main__":
    main()
