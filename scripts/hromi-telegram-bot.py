#!/usr/bin/env python3
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
os.environ.setdefault('HROMI_BOT_CONFIG_DIR', str(ROOT))
from hromi_telegram_bot.cli import main

if __name__ == '__main__':
    main()
