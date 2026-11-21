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

# The bot compares model families in order; the backend maps each key to the
# variant for the subject gender.
COMPARISON_KEYS = ['sd15-inpaint', 'flux-fill', 'qwen-image-2.1']

MODELS = [
    {
        'id': 'sd15-inpaint-female',
        'label': 'SD 1.5 Inpaint (женщины)',
        'description': 'test',
        'availability': 'ready',
    },
    {
        'id': 'flux-fill-female',
        'label': 'FLUX.1 Fill (женщины)',
        'description': 'test',
        'availability': 'ready',
    },
    {
        'id': 'qwen-image-2.1-female',
        'label': 'Qwen-Image 2.1 (женщины)',
        'description': 'test',
        'availability': 'ready',
    },
]

MALE_MODELS = [
    {
        'id': 'sd15-inpaint-male',
        'label': 'SD 1.5 Inpaint (мужчины)',
        'description': 'test',
        'availability': 'ready',
    },
    {
        'id': 'flux-fill-male',
        'label': 'FLUX.1 Fill (мужчины)',
        'description': 'test',
        'availability': 'ready',
    },
    {
        'id': 'qwen-image-2.1-male',
        'label': 'Qwen-Image 2.1 (мужчины)',
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
        self.usernames = []
        self.genders = []
        self.states = []

    def telegram_access(self, user_id):
        return None

    def models(self, user_id):
        return MODELS + MALE_MODELS

    def create_job(
        self,
        user_id,
        update_id,
        filename,
        content_type,
        contents,
        model,
        telegram_username=None,
        gender=None,
    ):
        self.created.append((user_id, update_id, filename, content_type, contents, model))
        self.usernames.append(telegram_username)
        self.genders.append(gender)
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
        self.store.accept_rules(7, 1)
        self.store.set_gender(7, 'female')
        self.addCleanup(self.bot.close)

    def test_admin_access_overrides_existing_bot_policy(self):
        update = self.photo_update()
        with patch.object(self.hromi, 'telegram_access', return_value=False):
            self.bot.handle_update(update)
        self.assertIn('Ваш Telegram ID: 7', self.telegram.messages[-1]['text'])
        self.assertIsNone(self.store.comparison(update['update_id']))

    def test_new_user_must_accept_rules_before_photo_upload(self):
        update = self.photo_update()
        update['message']['from']['id'] = 9
        self.bot.handle_update(update)
        rules = self.telegram.messages[-1]
        self.assertIn('только люди от 18 лет', rules['text'])
        self.assertIsNone(self.store.comparison(update['update_id']))
        self.assertNotIn(9, self.store.pending_photo_users())

        accept = rules['reply_markup']['inline_keyboard'][0][0]['callback_data']
        self.bot.handle_update(
            {
                'callback_query': {
                    'id': 'accept',
                    'from': {'id': 9},
                    'message': {'chat': {'id': 8}},
                    'data': accept,
                }
            }
        )
        self.assertTrue(BotStore(self.store.path).accepted_rules(9, 1))
        self.assertIn('📷 Отправить фото', str(self.telegram.messages[-1]['reply_markup']))

    def test_model_command_shows_backend_models(self):
        self.bot.handle_update(
            {
                'update_id': 1,
                'message': {
                    'message_id': 1,
                    'from': {'id': 7},
                    'chat': {'id': 8},
                    'text': '/model',
                },
            }
        )
        text = self.telegram.messages[-1]['text']
        self.assertIn('SD 1.5 Inpaint', text)
        self.assertIn('FLUX.1 Fill', text)
        self.assertIn('Qwen-Image 2.1', text)
        self.assertIn('в полный рост', text)
        self.assertIn('одежда в один слой', text)
        self.assertEqual(
            ['gender:male', 'gender:female'],
            [
                button['callback_data']
                for button in self.telegram.messages[-1]['reply_markup']['inline_keyboard'][0]
            ],
        )

    def test_comparison_excludes_diagnostics_and_preserves_model_order(self):
        with patch.object(
            self.hromi,
            'models',
            return_value=[
                {'id': 'passthrough', 'availability': 'ready'},
                MODELS[2],
                MODELS[0],
                MODELS[1],
            ],
        ):
            # The bot sends base keys; labels still come from the catalog.
            self.assertEqual([model['id'] for model in self.bot._models(7)], COMPARISON_KEYS)
            self.assertEqual(
                [model['label'] for model in self.bot._models(7)],
                [model['label'] for model in MODELS],
            )

    def test_incomplete_or_unverified_pair_never_falls_back(self):
        for availability in ('missing', 'unknown', None):
            with (
                self.subTest(availability=availability),
                patch.object(
                    self.hromi,
                    'models',
                    return_value=[
                        MODELS[0],
                        MODELS[1],
                        {**MODELS[2], 'availability': availability},
                        {'id': 'passthrough', 'availability': 'ready'},
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
                    'data': 'model:qwen-image-2.1-female',
                },
            }
        )
        self.assertIn('Отправьте фотографию', self.telegram.messages[-1]['text'])

    def photo_update(self, update_id=42, username=None):
        author = {'id': 7}
        if username is not None:
            author['username'] = username
        return {
            'update_id': update_id,
            'message': {
                'message_id': 10,
                'from': author,
                'chat': {'id': 8},
                'photo': [{'file_id': 'photo', 'file_size': 100}],
            },
        }

    def test_photo_runs_all_models_sequentially_with_labels(self):
        self.bot._start_comparison = lambda comparison: None
        self.bot.handle_update(self.photo_update())
        self.assertTrue(self.store.has_active_job(7))
        self.bot._compare(42)
        self.assertEqual(
            [job[-1] for job in self.hromi.created],
            COMPARISON_KEYS,
        )
        self.assertNotEqual(self.hromi.created[0][1], self.hromi.created[1][1])
        self.assertEqual(len(self.telegram.photos), 3)
        for photo, model in zip(self.telegram.photos, MODELS, strict=False):
            self.assertIn(model['label'], photo[-1])
        self.assertFalse(self.store.has_active_job(7))
        self.assertIsNone(self.store.comparison(42)['payload'])

    def test_telegram_username_is_sent_with_every_model(self):
        self.bot._start_comparison = lambda comparison: None
        self.bot.handle_update(self.photo_update(username='ansalim_01'))
        self.bot._compare(42)
        self.assertEqual(len(self.hromi.created), 3)
        self.assertEqual(self.hromi.usernames, ['ansalim_01'] * 3)

    def test_photo_without_telegram_username_omits_it(self):
        self.bot._start_comparison = lambda comparison: None
        self.bot.handle_update(self.photo_update())
        self.bot._compare(42)
        self.assertEqual(len(self.hromi.created), 3)
        self.assertEqual(self.hromi.usernames, [None] * 3)

    def test_failed_model_does_not_stop_other_models(self):
        self.bot._start_comparison = lambda comparison: None
        self.hromi.states = [{'state': 'FAILED', 'error': 'https://private.example/fixture-secret'}]
        self.bot.handle_update(self.photo_update())
        self.bot._compare(42)
        self.assertEqual(len(self.hromi.created), 3)
        self.assertEqual(len(self.telegram.photos), 2)
        self.assertIn('FLUX.1 Fill', self.telegram.photos[0][-1])
        self.assertIn('Qwen-Image 2.1', self.telegram.photos[1][-1])
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
        self.assertEqual(
            [job[-1] for job in self.hromi.created],
            COMPARISON_KEYS[1:],
        )
        self.assertEqual(len(self.telegram.photos), 3)

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
        self.assertIn('Доступ к этому боту закрыт.', self.telegram.messages[-1]['text'])

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
        self.assertEqual(len(self.telegram.photos), 3)
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
        self.assertEqual(len(self.telegram.photos), 2)
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
        self.assertEqual(len(self.telegram.photos), 3)
        self.assertFalse(self.bot.store.has_active_job(7))

    def test_photo_without_gender_is_held_until_choice(self):
        self.store.set_gender(7, None)
        self.bot.store = BotStore(self.config.database_path)
        self.bot._start_comparison = lambda comparison: None
        self.bot.handle_update(self.photo_update())
        self.assertEqual(self.hromi.created, [])
        self.assertIn('Выберите, кого обрабатываем', self.telegram.messages[-1]['text'])
        self.assertIsNotNone(self.bot.store.pending_photo(7))
        self.bot.handle_update(
            {
                'update_id': 43,
                'callback_query': {
                    'id': 'callback-2',
                    'from': {'id': 7},
                    'message': {'chat': {'id': 8}},
                    'data': 'gender:male',
                },
            }
        )
        self.bot._compare(42)
        self.assertEqual(
            [job[-1] for job in self.hromi.created],
            COMPARISON_KEYS,
        )
        self.assertIsNone(self.bot.store.pending_photo(7))

    def test_gender_choice_changes_model_set(self):
        self.store.set_gender(7, 'male')
        self.assertEqual(
            [model['label'] for model in self.bot._models(7)],
            [model['label'] for model in MALE_MODELS],
        )
        self.store.set_gender(7, 'female')
        self.assertEqual(
            [model['label'] for model in self.bot._models(7)],
            [model['label'] for model in MODELS],
        )

    def test_models_accept_catalog_that_lists_gendered_ids(self):
        self.store.set_gender(7, 'male')
        with patch.object(self.hromi, 'models', return_value=MALE_MODELS + MODELS):
            self.assertEqual([model['id'] for model in self.bot._models(7)], COMPARISON_KEYS)
            self.assertEqual(
                [model['label'] for model in self.bot._models(7)],
                [model['label'] for model in MALE_MODELS],
            )

    def test_each_comparison_job_carries_the_subject_gender(self):
        self.bot._start_comparison = lambda comparison: None
        self.store.set_gender(7, 'male')
        self.bot.handle_update(self.photo_update())
        self.bot._compare(42)
        self.assertEqual(self.hromi.genders, ['male'] * len(COMPARISON_KEYS))
        self.assertEqual(self.store.comparison(42)['gender'], 'male')

    def test_queued_comparison_without_stored_gender_uses_user_setting(self):
        # Comparisons created before the comparison row stored a gender.
        self.bot._start_comparison = lambda comparison: None
        self.store.set_gender(7, 'female')
        self.bot.handle_update(self.photo_update())
        with self.store._transaction() as db:
            db.execute('UPDATE comparisons SET gender = NULL WHERE update_id = 42')
        self.bot._compare(42)
        self.assertEqual(self.hromi.genders, ['female'] * len(COMPARISON_KEYS))

    def test_pending_photo_survives_restart_and_resumes_after_gender(self):
        self.bot._start_comparison = lambda comparison: None
        self.store.set_gender(7, None)
        self.bot.store = BotStore(self.config.database_path)
        self.assertIsNone(self.bot.store.gender(7))
        self.bot.handle_update(self.photo_update())
        self.assertIsNotNone(self.bot.store.pending_photo(7))
        reopened = HromiTelegramBot(
            self.config, self.telegram, self.hromi, BotStore(self.config.database_path)
        )
        self.addCleanup(reopened.close)
        reopened._start_comparison = lambda comparison: None
        reopened.store.set_gender(7, 'female')
        reopened._resume_pending_photo(7)
        reopened._compare(42)
        self.assertEqual(
            [job[-1] for job in self.hromi.created],
            COMPARISON_KEYS,
        )
