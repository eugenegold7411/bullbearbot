.PHONY: install lint format test test-ci ci-local import-check clean deploy deploy-env

PYTHON ?= $(shell [ -f .venv/bin/python3 ] && echo .venv/bin/python3 || echo python3)

install:
	pip install -e . -r requirements-dev.txt

lint:
	ruff check .

format:
	ruff format .

test:
	pytest tests/

test-ci:
	pytest tests/ -m "not requires_chromadb"

ci-local:
	$(PYTHON) -m py_compile $(shell find . -name "*.py" -not -path "./.venv/*" -not -path "./__pycache__/*")
	make import-check
	pytest tests/ -m "not requires_chromadb" -q
	@echo "CI-local complete — matches blocking CI subset"

import-check:
	$(PYTHON) -c "\
import risk_kernel; \
import schemas; \
import attribution; \
import preflight; \
import versioning; \
import cost_attribution; \
import decision_outcomes; \
import bot; \
import order_executor; \
import weekly_review; \
import bot_options; \
print('import-check OK')"

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true
	find . -name "*.pyc" -delete 2>/dev/null; true
	rm -rf *.egg-info

deploy:
	rsync -avz -e 'ssh -i ~/.ssh/trading_bot' \
		--exclude .venv \
		--exclude __pycache__ \
		--exclude '*.pyc' \
		--exclude 'data/' \
		--exclude '.env' \
		--exclude '.git/' \
		. tradingbot:/home/trading-bot/
	@echo "Syncing server git index to origin/main (no working-tree changes)..."
	ssh tradingbot 'cd /home/trading-bot && git fetch origin --quiet && git reset origin/main --quiet'
	ssh tradingbot 'systemctl restart trading-bot && sleep 3 && systemctl status trading-bot --no-pager | head -5'
	@echo "Deploy complete. Server git index now at origin/main."

deploy-env:
	ssh tradingbot 'cp /home/trading-bot/.env /home/trading-bot/.env.backup.$(shell date +%Y%m%d_%H%M%S)'
	rsync -avz -e 'ssh -i ~/.ssh/trading_bot' \
		/Users/eugene.gold/trading-bot/.env \
		tradingbot:/home/trading-bot/.env
	@echo "Verifying no placeholders..."
	ssh tradingbot 'grep -c "your_" /home/trading-bot/.env && echo "PLACEHOLDERS FOUND" || echo "Clean — 0 placeholders"'
