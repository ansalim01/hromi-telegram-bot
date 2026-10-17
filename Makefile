.PHONY: run test
PYTHON ?= $(if $(wildcard .venv/bin/python),$(abspath .venv/bin/python),python3)

run:
	$(PYTHON) scripts/hromi-telegram-bot.py

test:
	$(PYTHON) -m unittest discover -s tests -v

.PHONY: setup check doctor
setup:
	python3 -m venv .venv
	.venv/bin/python -m pip install -e . -r requirements-dev.txt
check:
	$(PYTHON) -m ruff check src scripts tests
	$(PYTHON) -m ruff format --check src scripts tests
doctor:
	$(PYTHON) scripts/check-local.py
