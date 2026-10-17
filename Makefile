.PHONY: run test
PYTHON ?= $(if $(wildcard ../.venv/bin/python),$(abspath ../.venv/bin/python),python3)

run:
	$(PYTHON) scripts/hromi-telegram-bot.py

test:
	$(PYTHON) -m unittest discover -s tests -v
