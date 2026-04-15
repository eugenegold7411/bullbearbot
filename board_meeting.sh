#!/bin/bash
REASON="${1:-Manual emergency session}"
cd /home/trading-bot
source .venv/bin/activate
echo "Calling emergency board meeting: $REASON"
python3 weekly_review.py --emergency --reason "$REASON"
