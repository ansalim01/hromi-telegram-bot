#!/usr/bin/env python3
"""Validate bot configuration without Telegram requests."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from hromi_telegram_bot.config import BotConfig

if __name__ == '__main__':
    try:
        BotConfig.from_env(ROOT)
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from error
    print('Telegram bot configuration: OK')
