import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hromi_telegram_bot.bot import HromiTelegramBot
from hromi_telegram_bot.config import BotConfig
from hromi_telegram_bot.http import ApiError
from hromi_telegram_bot.store import BotStore

MODELS = [
    {'id': 'flux-fill', 'label': 'FLUX.1 Fill', 'description': 'test', 'availability': 'ready'},
    {
        'id': 'qwen-image-2.1',
        'label': 'Qwen-Image 2.1',
        'description': 'test',
        'availability': 'ready',
    },
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
        return {'id': f'{len(self.created):032x}', 'state': 'QUEUED', 'queue_position': 2}

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
            poll_interval_seconds=0.001,
            allow_any_user=True,
            allowed_group_ids=frozenset({8}),
        )
        self.bot = HromiTelegramBot(
            self.config,
            self.telegram,
            self.hromi,
            self.store,
        )
        self.addCleanup(self.bot.close)

    def test_start_shows_backend_models(self):
        self.bot.handle_update(
            {
                'update_id': 1,
                'message': {
                    'message_id': 1,
                    'from': {'id': 7},
                    'chat': {'id': 8},
                    'text': '/start',
                },
            }
        )
        text = self.telegram.messages[-1]['text']
        self.assertIn('FLUX.1 Fill', text)
        self.assertIn('Qwen-Image 2.1', text)
        self.assertIsNone(self.telegram.messages[-1]['reply_markup'])

    def test_comparison_excludes_diagnostics_and_preserves_model_order(self):
        with patch.object(
            self.hromi,
            'models',
            return_value=[
                {'id': 'passthrough', 'availability': 'ready'},
                MODELS[1],
                {'id': 'sd15-inpaint', 'availability': 'ready'},
                MODELS[0],
            ],
        ):
            self.assertEqual(self.bot._models(7), MODELS)

    def test_incomplete_or_unverified_pair_never_falls_back(self):
        for availability in ('missing', 'unknown', None):
            with (
                self.subTest(availability=availability),
                patch.object(
                    self.hromi,
                    'models',
                    return_value=[
                        MODELS[0],
                        {**MODELS[1], 'availability': availability},
                        {'id': 'passthrough', 'availability': 'ready'},
                        {'id': 'sd15-inpaint', 'availability': 'ready'},
                    ],
                ),
            ):
                with patch.object(self.telegram, 'get_file') as get_file:
                    self.bot.handle_update(self.photo_update())
                    get_file.assert_not_called()
                self.assertEqual(self.bot._models(7), [])
                self.assertEqual(self.hromi.created, [])
                self.assertIsNone(self.store.comparison(42))
                self.assertIn('пока недоступно', self.telegram.messages[-1]['text'])

    def test_old_callback_requests_comparison(self):
        self.bot.handle_update(
            {
                'update_id': 2,
                'callback_query': {
                    'id': 'callback-1',
                    'from': {'id': 7},
                    'message': {'chat': {'id': 8}},
                    'data': 'model:qwen-image-2.1',
                },
            }
        )
        self.assertIn('Отправьте фотографию', self.telegram.messages[-1]['text'])

    def photo_update(self, update_id=42):
        return {
            'update_id': update_id,
            'message': {
                'message_id': 10,
                'from': {'id': 7},
                'chat': {'id': 8},
                'photo': [{'file_id': 'photo', 'file_size': 100}],
            },
        }

    def test_photo_runs_all_models_sequentially_with_labels(self):
        self.bot._start_comparison = lambda comparison: None
        self.bot.handle_update(self.photo_update())
        self.assertTrue(self.store.has_active_job(7))
        self.bot._compare(42)
        self.assertEqual([job[-1] for job in self.hromi.created], ['flux-fill', 'qwen-image-2.1'])
        self.assertNotEqual(self.hromi.created[0][1], self.hromi.created[1][1])
        self.assertEqual(len(self.telegram.photos), 2)
        for photo, model in zip(self.telegram.photos, MODELS, strict=False):
            self.assertIn(model['label'], photo[-1])
        self.assertFalse(self.store.has_active_job(7))
        self.assertIsNone(self.store.comparison(42)['payload'])

    def test_failed_model_does_not_stop_other_models(self):
        self.bot._start_comparison = lambda comparison: None
        self.hromi.states = [{'state': 'FAILED', 'error': 'https://private.example/fixture-secret'}]
        self.bot.handle_update(self.photo_update())
        self.bot._compare(42)
        self.assertEqual(len(self.hromi.created), 2)
        self.assertEqual(len(self.telegram.photos), 1)
        self.assertIn('Qwen-Image 2.1', self.telegram.photos[0][-1])
        self.assertTrue(self.store.comparison(42)['finished'])
        self.assertTrue(
            all('fixture-secret' not in message['text'] for message in self.telegram.messages)
        )

    def test_restores_current_model_without_submitting_again(self):
        self.bot._start_comparison = lambda comparison: None
        self.bot.handle_update(self.photo_update())
        self.store.set_comparison_job(42, 'existing-job')
        self.bot.store = BotStore(self.config.database_path)
        self.assertEqual(len(self.bot.store.active_comparisons()), 1)
        self.bot._compare(42)
        self.assertEqual([job[-1] for job in self.hromi.created], ['qwen-image-2.1'])
        self.assertEqual(len(self.telegram.photos), 2)

    def test_second_photo_blocked_for_entire_comparison(self):
        self.bot._start_comparison = lambda comparison: None
        self.bot.handle_update(self.photo_update())
        self.store.advance_comparison(42)
        self.bot.handle_update(self.photo_update(43))
        self.assertIsNone(self.store.comparison(43))
        self.assertIn('Дождитесь результата', self.telegram.messages[-1]['text'])

    def test_duplicate_update_does_not_create_second_backend_job(self):
        self.bot._start_comparison = lambda comparison: None
        update = {
            'update_id': 42,
            'message': {
                'message_id': 10,
                'from': {'id': 7},
                'chat': {'id': 8},
                'photo': [{'file_id': 'photo', 'file_size': 100}],
            },
        }
        self.bot.handle_update(update)
        self.bot.handle_update(update)
        self.assertEqual(len(self.store.active_comparisons()), 1)

    def test_store_recovers_active_jobs_and_offset(self):
        self.store.add_job(1, 7, 8, 'b' * 32, 9)
        self.store.set_offset(12)
        reopened = BotStore(self.config.database_path)
        self.assertEqual(reopened.offset(), 12)
        self.assertEqual([job['hromi_job_id'] for job in reopened.active_jobs()], ['b' * 32])

    def test_second_photo_is_rejected_until_first_is_delivered(self):
        self.bot._start_comparison = lambda comparison: None
        self.store.add_job(1, 7, 8, 'b' * 32, 9)
        update = {
            'update_id': 42,
            'message': {
                'message_id': 10,
                'from': {'id': 7},
                'chat': {'id': 8},
                'photo': [{'file_id': 'photo', 'file_size': 100}],
            },
        }
        self.bot.handle_update(update)
        self.assertEqual(len(self.hromi.created), 0)
        self.assertIn('Дождитесь результата', self.telegram.messages[-1]['text'])
        reopened = BotStore(self.config.database_path)
        self.assertTrue(reopened.has_active_job(7))
        self.assertFalse(reopened.has_active_job(99))
        self.store.update_job('b' * 32, 'DELIVERED')
        self.bot.handle_update(update)
        self.assertIsNotNone(self.store.comparison(42))
        self.assertIn('5–10 минут', self.telegram.messages[-1]['text'])

    def test_status_shows_estimate_without_elapsed_time(self):
        current = {'state': 'RUNNING', 'created_at': 100, 'accepted_at': 200}
        self.assertIn('5–10 минут', self.bot._status_text(current))
        self.assertNotIn('сек', self.bot._status_text(current))
        self.assertEqual(self.bot._status_text({'state': 'SUCCEEDED'}), 'Готово.')

    def test_allowlist_rejects_unknown_user(self):
        config = BotConfig(
            telegram_token='token',
            hromi_base_url='http://backend.test',
            hromi_api_key='x' * 32,
            database_path=self.config.database_path,
            allowed_user_ids=frozenset({99}),
            allowed_group_ids=frozenset({8}),
        )
        bot = HromiTelegramBot(config, self.telegram, self.hromi, self.store)
        self.addCleanup(bot.close)
        bot.handle_update(
            {
                'update_id': 1,
                'message': {
                    'message_id': 1,
                    'from': {'id': 7},
                    'chat': {'id': 8},
                    'text': '/start',
                },
            }
        )
        self.assertEqual(self.telegram.messages[-1]['text'], 'Доступ к этому боту закрыт.')

    def test_permanent_telegram_error_does_not_block_later_updates(self):
        stale_callback = ApiError(
            'telegram',
            400,
            'Bad Request: query is too old and response timeout expired',
        )
        self.assertFalse(self.bot._should_retry_update(stale_callback))
        self.assertTrue(
            self.bot._should_retry_update(
                ApiError('telegram', 429, 'Too Many Requests'),
            )
        )
        self.assertTrue(
            self.bot._should_retry_update(
                ApiError('telegram', None, 'connection_failed'),
            )
        )

    def test_temporary_telegram_failure_retries_without_restart(self):
        self.bot._start_comparison = lambda comparison: None
        self.bot.handle_update(self.photo_update())
        original = self.telegram.send_message
        failed = False

        def send(*args, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise ApiError('telegram', 503, 'temporary_failure')
            return original(*args, **kwargs)

        self.telegram.send_message = send
        with patch.object(self.bot._closed, 'wait', return_value=False):
            self.bot._compare(42)
        self.assertEqual(len(self.telegram.photos), 2)
        self.assertFalse(self.store.has_active_job(7))
        self.assertEqual(self.store.comparison_step(42, 0)['state'], 'DELIVERED')

    def test_missing_job_does_not_block_remaining_models(self):
        self.bot._start_comparison = lambda comparison: None
        self.bot.handle_update(self.photo_update())
        original = self.hromi.job
        failed = False

        def job(*args):
            nonlocal failed
            if not failed:
                failed = True
                raise ApiError('hromi', 404, 'job_not_found')
            return original(*args)

        self.hromi.job = job
        self.bot._compare(42)
        self.assertEqual(len(self.telegram.photos), 1)
        self.assertTrue(self.store.comparison(42)['finished'])

    def test_blocked_telegram_chat_finishes_comparison(self):
        self.bot._start_comparison = lambda comparison: None
        self.bot.handle_update(self.photo_update())
        self.telegram.send_photo = lambda *args: (_ for _ in ()).throw(
            ApiError('telegram', 403, 'bot_was_blocked')
        )
        self.bot._compare(42)
        self.assertFalse(self.store.has_active_job(7))

    def test_restart_after_delivery_does_not_resend_delivered_step(self):
        self.bot._start_comparison = lambda comparison: None
        self.bot.handle_update(self.photo_update())
        with patch.object(self.store, 'advance_comparison', side_effect=RuntimeError('crash')):
            self.bot._compare(42)
        self.assertEqual(len(self.telegram.photos), 1)
        self.bot.store = BotStore(self.config.database_path)
        self.bot._compare(42)
        self.assertEqual(len(self.telegram.photos), 2)
        self.assertFalse(self.bot.store.has_active_job(7))
