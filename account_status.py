"""
Step 1 — Alpaca paper trading account status.
Prints account balance and open positions to the terminal.
"""

import os
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetAssetsRequest

load_dotenv()

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

if not API_KEY or not SECRET_KEY:
    raise EnvironmentError("Missing ALPACA_API_KEY or ALPACA_SECRET_KEY in .env")

client = TradingClient(API_KEY, SECRET_KEY, paper=True)


def print_account_balance(account) -> None:
    print("=" * 50)
    print("  PAPER ACCOUNT BALANCE")
    print("=" * 50)
    print(f"  Portfolio Value : ${float(account.portfolio_value):>15,.2f}")
    print(f"  Cash            : ${float(account.cash):>15,.2f}")
    print(f"  Buying Power    : ${float(account.buying_power):>15,.2f}")
    print(f"  Equity          : ${float(account.equity):>15,.2f}")
    last_eq = float(account.last_equity or 0)
    equity  = float(account.equity)
    day_pl  = equity - last_eq
    pl_sign = "+" if day_pl >= 0 else ""
    print(f"  Day P&L         : {pl_sign}${day_pl:>14,.2f}")
    print(f"  Account Status  :  {account.status}")
    print("=" * 50)


def print_positions(positions) -> None:
    print("\n" + "=" * 50)
    print("  OPEN POSITIONS")
    print("=" * 50)

    if not positions:
        print("  No open positions.")
        print("=" * 50)
        return

    header = f"  {'Symbol':<8} {'Qty':>8} {'Avg Cost':>12} {'Mkt Price':>12} {'Mkt Value':>12} {'Unrealized P&L':>16}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    for pos in positions:
        symbol      = pos.symbol
        qty         = float(pos.qty)
        avg_cost    = float(pos.avg_entry_price)
        mkt_price   = float(pos.current_price)
        mkt_value   = float(pos.market_value)
        unreal_pl   = float(pos.unrealized_pl)
        pl_sign     = "+" if unreal_pl >= 0 else ""

        print(
            f"  {symbol:<8} {qty:>8.4f} {avg_cost:>12.2f} {mkt_price:>12.2f} "
            f"{mkt_value:>12.2f} {pl_sign}{unreal_pl:>15.2f}"
        )

    total_pl = sum(float(p.unrealized_pl) for p in positions)
    pl_sign = "+" if total_pl >= 0 else ""
    print("  " + "-" * (len(header) - 2))
    print(f"  {'TOTAL':<8} {'':>8} {'':>12} {'':>12} {'':>12} {pl_sign}{total_pl:>15.2f}")
    print("=" * 50)


def main() -> None:
    account = client.get_account()
    positions = client.get_all_positions()

    print_account_balance(account)
    print_positions(positions)
    print()


if __name__ == "__main__":
    main()
