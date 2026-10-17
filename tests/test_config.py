import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hromi_telegram_bot.config import BotConfig


class ConfigTests(TestCase):
    def test_requires_tokens_and_valid_origin(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                BotConfig.from_env(Path(directory))
        values = {
            'TELEGRAM_BOT_TOKEN': 'telegram-token',
            'HROMI_PUBLIC_API_KEY': 'x' * 32,
            'HROMI_BASE_URL': 'http://127.0.0.1:8091/path',
        }
        with TemporaryDirectory() as directory, patch.dict(os.environ, values, clear=True):
            with self.assertRaises(ValueError):
                BotConfig.from_env(Path(directory))

    def test_loads_env_and_allowed_users(self):
        values = {
            'TELEGRAM_BOT_TOKEN': 'telegram-token',
            'HROMI_PUBLIC_API_KEY': 'x' * 32,
            'TELEGRAM_ALLOWED_USER_IDS': '7, 9',
        }
        with TemporaryDirectory() as directory, patch.dict(os.environ, values, clear=True):
            config = BotConfig.from_env(Path(directory))
        self.assertEqual(config.allowed_user_ids, frozenset({7, 9}))
        self.assertEqual(config.hromi_base_url, 'http://127.0.0.1:8091')
