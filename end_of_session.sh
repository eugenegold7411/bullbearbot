#!/bin/bash
# BullBearBot — end of session
# Run from local: bash /Users/eugene.gold/trading-bot/end_of_session.sh

echo "========================================"
echo "  BullBearBot — End of Session"
echo "========================================"

# 1. Restart service
echo "→ Restarting service..."
ssh tradingbot 'systemctl restart trading-bot && sleep 5 && systemctl status trading-bot --no-pager | head -3'

# 2. Confirm one clean cycle
echo "→ Waiting for one clean cycle..."
ssh tradingbot 'timeout 45 tail -f /home/trading-bot/logs/bot.log | grep -m1 "Cycle done"' || true

# 3. Pull local mirror
echo "→ Syncing local mirror..."
rsync -avz -e 'ssh -i ~/.ssh/trading_bot' \
  --exclude .venv --exclude __pycache__ --exclude '*.pyc' \
  --exclude .env --exclude logs/ --exclude data/ \
  tradingbot:/home/trading-bot/ /Users/eugene.gold/trading-bot/
echo "✅ Synced"

# 4. Show positions + cost
ssh tradingbot 'cd /home/trading-bot && source .venv/bin/activate && python3 -c "
from dotenv import load_dotenv; load_dotenv()
from alpaca.trading.client import TradingClient
import os, json
from pathlib import Path
a1 = TradingClient(os.getenv(\"ALPACA_API_KEY\"), os.getenv(\"ALPACA_SECRET_KEY\"), paper=True)
acct = a1.get_account()
print(f\"Equity: \${float(acct.equity):,.2f}  Cash: \${float(acct.cash):,.2f}\")
for p in a1.get_all_positions():
    pnl = float(p.unrealized_pl)
    print(f\"  {p.symbol}: {p.qty} shares  P&L \${pnl:+.2f}\")
cost = Path(\"data/costs/daily_costs.json\")
if cost.exists():
    d = json.loads(cost.read_text())
    print(f\"Today cost: \${d.get(\"daily_cost\",0):.2f}  ({d.get(\"daily_calls\",0)} calls)\")
"'

echo ""
echo "⚠️  TSM exits TODAY at 15:45 ET — gate fires at 15:15 ET"
echo "========================================"
echo "  Done. Good night."
echo "========================================"
