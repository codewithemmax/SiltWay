.PHONY: lint test hardcode-check check inspect reproduce

PYTHON ?= python

lint:
	$(PYTHON) -m ruff check .

test:
	$(PYTHON) -m pytest

hardcode-check:
	$(PYTHON) scripts/check_no_hardcoding.py

check: lint test hardcode-check

inspect:
	$(PYTHON) scripts/inspect_data.py

reproduce: inspect check
