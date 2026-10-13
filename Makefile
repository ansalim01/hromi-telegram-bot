.PHONY: run test

run:
	python3 scripts/hromi-telegram-bot.py

test:
	python3 -m unittest discover -s tests -v
