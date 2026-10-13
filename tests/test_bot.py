import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from unittest import TestCase


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hromi_telegram_bot.bot import HromiTelegramBot
from hromi_telegram_bot.config import BotConfig
from hromi_telegram_bot.http import ApiError
from hromi_telegram_bot.store import BotStore


MODELS = [
    {'id': 'flux-fill', 'label': 'FLUX.1 Fill', 'description': 'test'},
    {'id': 'qwen-image-2.1', 'label': 'Qwen-Image 2.1', 'description': 'test'},
]


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.edits = []
        self.callbacks = []
        self.photos = []
        self.actions = []

    def send_message(self, chat_id, text, reply_markup=None):
        message = {
            'message_id': len(self.messages) + 1,
            'chat_id': chat_id,
            'text': text,
            'reply_markup': reply_markup,
        }
        self.messages.append(message)
        return message

    def edit_message(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))

    def answer_callback(self, callback_id, text=''):
        self.callbacks.append((callback_id, text))

    def get_file(self, file_id):
        return {'file_path': 'photos/source.jpg'}

    def download_file(self, file_path):
        return b'\xff\xd8\xffphoto', 'image/jpeg'

    def send_chat_action(self, chat_id, action):
        self.actions.append((chat_id, action))

    def send_photo(self, chat_id, contents, filename, content_type, caption):
        self.photos.append((chat_id, contents, filename, content_type, caption))


class FakeHromi:
    def __init__(self):
        self.created = []
        self.states = []

    def models(self, user_id):
        return MODELS

    def create_job(self, user_id, update_id, filename, content_type, contents, model):
        self.created.append((user_id, update_id, filename, content_type, contents, model))
        return {'id': 'a' * 32, 'state': 'QUEUED', 'queue_position': 2}

    def job(self, user_id, job_id):
        if self.states:
            return self.states.pop(0)
        return {'id': job_id, 'state': 'SUCCEEDED', 'queue_position': None}

    def result(self, user_id, job_id):
        return b'\x89PNG\r\n\x1a\nresult', 'image/png', 200


class BotTests(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.telegram = FakeTelegram()
        self.hromi = FakeHromi()
        self.store = BotStore(Path(self.directory.name) / 'bot.sqlite3')
        self.config = BotConfig(
            telegram_token='token',
            hromi_base_url='http://backend.test',
            hromi_api_key='x' * 32,
            database_path=Path(self.directory.name) / 'bot.sqlite3',
            poll_interval_seconds=.001,
        )
        self.bot = HromiTelegramBot(
            self.config, self.telegram, self.hromi, self.store,
        )
        self.addCleanup(self.bot.close)

    def test_start_shows_backend_models(self):
        self.bot.handle_update({
            'update_id': 1,
            'message': {'message_id': 1, 'from': {'id': 7}, 'chat': {'id': 8},
                        'text': '/start'},
        })
        keyboard = self.telegram.messages[-1]['reply_markup']['inline_keyboard']
        self.assertEqual(
            [row[0]['callback_data'] for row in keyboard],
            ['model:flux-fill', 'model:qwen-image-2.1'],
        )

    def test_callback_persists_model_and_requests_photo(self):
        self.bot.handle_update({
            'update_id': 2,
            'callback_query': {
                'id': 'callback-1', 'from': {'id': 7},
                'message': {'chat': {'id': 8}}, 'data': 'model:qwen-image-2.1',
            },
        })
        self.assertEqual(self.store.selected_model(7), 'qwen-image-2.1')
        self.assertIn('Теперь отправьте фото', self.telegram.messages[-1]['text'])

    def test_photo_creates_durable_job_and_returns_result(self):
        self.store.set_model(7, 'flux-fill')
        captured = []
        self.bot._start_monitor = captured.append
        update = {
            'update_id': 42,
            'message': {
                'message_id': 10, 'from': {'id': 7}, 'chat': {'id': 8},
                'photo': [{'file_id': 'small', 'file_size': 10},
                          {'file_id': 'large', 'file_size': 100}],
            },
        }
        self.bot.handle_update(update)
        self.assertEqual(self.hromi.created[0][1], 42)
        self.assertEqual(self.hromi.created[0][-1], 'flux-fill')
        stored = self.store.job_for_update(42)
        self.assertEqual(stored['hromi_job_id'], 'a' * 32)
        self.assertEqual(len(captured), 1)

        self.bot._monitor(stored)
        self.assertEqual(self.store.job_for_update(42)['state'], 'DELIVERED')
        self.assertEqual(self.telegram.photos[0][1], b'\x89PNG\r\n\x1a\nresult')

    def test_duplicate_update_does_not_create_second_backend_job(self):
        self.bot._start_monitor = lambda job: None
        update = {
            'update_id': 42,
            'message': {'message_id': 10, 'from': {'id': 7}, 'chat': {'id': 8},
                        'photo': [{'file_id': 'photo', 'file_size': 100}]},
        }
        self.bot.handle_update(update)
        self.bot.handle_update(update)
        self.assertEqual(len(self.hromi.created), 1)

    def test_store_recovers_active_jobs_and_offset(self):
        self.store.add_job(1, 7, 8, 'b' * 32, 9)
        self.store.set_offset(12)
        reopened = BotStore(self.config.database_path)
        self.assertEqual(reopened.offset(), 12)
        self.assertEqual([job['hromi_job_id'] for job in reopened.active_jobs()], ['b' * 32])

    def test_second_photo_is_rejected_until_first_is_delivered(self):
        self.bot._start_monitor = lambda job: None
        self.store.add_job(1, 7, 8, 'b' * 32, 9)
        update = {
            'update_id': 42,
            'message': {'message_id': 10, 'from': {'id': 7}, 'chat': {'id': 8},
                        'photo': [{'file_id': 'photo', 'file_size': 100}]},
        }
        self.bot.handle_update(update)
        self.assertEqual(len(self.hromi.created), 0)
        self.assertIn('Дождитесь результата', self.telegram.messages[-1]['text'])
        reopened = BotStore(self.config.database_path)
        self.assertTrue(reopened.has_active_job(7))
        self.assertFalse(reopened.has_active_job(99))
        self.store.update_job('b' * 32, 'DELIVERED')
        self.bot.handle_update(update)
        self.assertEqual(len(self.hromi.created), 1)
        self.assertIn('5–10 минут', self.telegram.messages[-1]['text'])

    def test_status_shows_estimate_without_elapsed_time(self):
        current = {'state': 'RUNNING', 'created_at': 100, 'accepted_at': 200}
        self.assertIn('5–10 минут', self.bot._status_text(current))
        self.assertNotIn('сек', self.bot._status_text(current))
        self.assertEqual(self.bot._status_text({'state': 'SUCCEEDED'}), 'Готово.')

    def test_allowlist_rejects_unknown_user(self):
        config = BotConfig(
            telegram_token='token', hromi_base_url='http://backend.test',
            hromi_api_key='x' * 32, database_path=self.config.database_path,
            allowed_user_ids=frozenset({99}),
        )
        bot = HromiTelegramBot(config, self.telegram, self.hromi, self.store)
        self.addCleanup(bot.close)
        bot.handle_update({
            'update_id': 1,
            'message': {'message_id': 1, 'from': {'id': 7}, 'chat': {'id': 8},
                        'text': '/start'},
        })
        self.assertEqual(self.telegram.messages[-1]['text'], 'Доступ к этому боту закрыт.')

    def test_permanent_telegram_error_does_not_block_later_updates(self):
        stale_callback = ApiError(
            'telegram', 400,
            'Bad Request: query is too old and response timeout expired',
        )
        self.assertFalse(self.bot._should_retry_update(stale_callback))
        self.assertTrue(self.bot._should_retry_update(
            ApiError('telegram', 429, 'Too Many Requests'),
        ))
        self.assertTrue(self.bot._should_retry_update(
            ApiError('telegram', None, 'connection_failed'),
        ))
