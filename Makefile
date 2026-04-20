.PHONY: install lint format test test-ci import-check clean

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

import-check:
	python3 -c "\
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
