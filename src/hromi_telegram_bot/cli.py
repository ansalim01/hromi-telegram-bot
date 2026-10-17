"""Installed Telegram bot entrypoint."""

import argparse
import logging
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--config-dir', type=Path, default=Path(os.environ.get('HROMI_BOT_CONFIG_DIR', '.'))
    )
    args = parser.parse_args()
    from . import BotConfig, HromiTelegramBot

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    bot = HromiTelegramBot(BotConfig.from_env(args.config_dir.expanduser().resolve()))
    try:
        bot.run()
    except KeyboardInterrupt:
        logging.info('Telegram bot stopped')
    finally:
        bot.close()


if __name__ == '__main__':
    main()
