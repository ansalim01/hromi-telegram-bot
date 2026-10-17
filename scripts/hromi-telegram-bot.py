#!/usr/bin/env python3

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from hromi_telegram_bot import BotConfig, HromiTelegramBot  # noqa: E402

if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )
    bot = HromiTelegramBot(BotConfig.from_env(ROOT))
    try:
        bot.run()
    except KeyboardInterrupt:
        logging.info('Hromi Telegram bot stopped by user')
    finally:
        bot.close()
