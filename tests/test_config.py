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

    def test_credentials_are_not_in_configuration_repr(self):
        config = BotConfig(
            'fixture-telegram-secret', 'http://127.0.0.1', 'fixture-backend-secret', Path('bot.db')
        )
        self.assertNotIn('fixture-telegram-secret', repr(config))
        self.assertNotIn('fixture-backend-secret', repr(config))

    def test_relative_database_is_resolved_from_config_directory(self):
        values = {
            'TELEGRAM_BOT_TOKEN': 'fixture-token',
            'HROMI_PUBLIC_API_KEY': 'x' * 32,
            'TELEGRAM_ALLOWED_USER_IDS': '7',
            'TELEGRAM_BOT_DB': './data/bot.db',
        }
        with TemporaryDirectory() as directory, patch.dict(os.environ, values, clear=True):
            config = BotConfig.from_env(Path(directory))
            self.assertEqual(config.database_path, Path(directory).resolve() / 'data/bot.db')

    def test_control_characters_and_invalid_service_ports_are_rejected(self):
        values = {
            'TELEGRAM_BOT_TOKEN': 'fixture-token',
            'HROMI_PUBLIC_API_KEY': 'x' * 32,
            'TELEGRAM_ALLOWED_USER_IDS': '7',
        }
        with TemporaryDirectory() as directory, patch.dict(os.environ, values, clear=True):
            for url in [
                'https://backend.example:invalid',
                'https://back\nend.example',
                'https://backend.example:65536',
            ]:
                os.environ['HROMI_BASE_URL'] = url
                with self.assertRaises(ValueError):
                    BotConfig.from_env(Path(directory))
            os.environ.pop('HROMI_BASE_URL')
            os.environ['HROMI_PUBLIC_API_KEY'] = 'x' * 32 + '\nunsafe'
            with self.assertRaises(ValueError):
                BotConfig.from_env(Path(directory))
