"""Offline bot admission, transport and credential isolation regressions."""

import io
import json
import os
import sys
import time
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hromi_telegram_bot.bot import HromiTelegramBot
from hromi_telegram_bot.config import BotConfig
from hromi_telegram_bot.http import (
    ApiError,
    HromiClient,
    NoRedirects,
    TelegramClient,
    _read_bounded,
    _request,
)
from hromi_telegram_bot.store import BotStore


class BotSecurityTests(TestCase):
    def test_backend_age_error_codes_survive_safe_error_filter(self):
        for code in [
            'age_check_blocked_underage',
            'age_check_blocked_uncertain',
            'age_check_blocked_no_face',
            'age_check_blocked_error',
        ]:
            error = HTTPError(
                'https://backend.example/api/v1/jobs',
                403,
                'forbidden',
                Message(),
                io.BytesIO(json.dumps({'error': code}).encode()),
            )
            with patch('hromi_telegram_bot.http._OPENER.open', side_effect=error):
                with self.assertRaises(ApiError) as result:
                    _request('POST', 'https://backend.example/api/v1/jobs')
            self.assertEqual(result.exception.code, code)
            self.assertEqual(result.exception.status, 403)

    def test_empty_allowlist_requires_explicit_open_mode(self):
        values = {'TELEGRAM_BOT_TOKEN': 'fake', 'HROMI_PUBLIC_API_KEY': 's' * 40}
        with TemporaryDirectory() as directory, patch.dict(os.environ, values, clear=True):
            with self.assertRaises(ValueError):
                BotConfig.from_env(Path(directory))
            os.environ['TELEGRAM_ALLOW_ANY_USER'] = '1'
            self.assertTrue(BotConfig.from_env(Path(directory)).allow_any_user)

    def test_remote_plain_http_and_credential_urls_are_rejected(self):
        values = {
            'TELEGRAM_BOT_TOKEN': 'fake',
            'HROMI_PUBLIC_API_KEY': 's' * 40,
            'TELEGRAM_ALLOWED_USER_IDS': '7',
        }
        with TemporaryDirectory() as directory, patch.dict(os.environ, values, clear=True):
            for url in [
                'http://example.test',
                'https://user:secret@example.test',
                'https://example.test?secret=1',
            ]:
                os.environ['HROMI_BASE_URL'] = url
                with self.assertRaises(ValueError):
                    BotConfig.from_env(Path(directory))

    def test_groups_are_silent_and_do_not_fetch_models_or_photos(self):
        with TemporaryDirectory() as directory:
            config = BotConfig(
                'fake',
                'http://127.0.0.1',
                's' * 40,
                Path(directory) / 'bot.db',
                allowed_user_ids=frozenset({7}),
            )
            telegram, backend = Mock(), Mock()
            bot = HromiTelegramBot(config, telegram, backend)
            self.addCleanup(bot.close)
            bot.handle_update(
                {
                    'update_id': 1,
                    'message': {
                        'from': {'id': 7},
                        'chat': {'id': -100},
                        'photo': [{'file_id': 'private'}],
                    },
                }
            )
            telegram.get_file.assert_not_called()
            telegram.send_message.assert_not_called()
            backend.models.assert_not_called()

    def test_admission_and_daily_budget_happen_before_download(self):
        with TemporaryDirectory() as directory:
            store = BotStore(Path(directory) / 'bot.db')
            self.assertTrue(store.admit_photo(1, 7, 1, 1))
            self.assertTrue(store.admit_photo(1, 7, 1, 1))
            self.assertFalse(store.admit_photo(2, 7, 1, 1))
            store.add_comparison(1, 7, 7, {'image': 'private'}, [{'id': 'model'}])
            self.assertFalse(store.admit_photo(3, 8, 1, 1))
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)

    def test_zero_disables_daily_admission_limit(self):
        with TemporaryDirectory() as directory:
            store = BotStore(Path(directory) / 'bot.db')
            self.assertTrue(store.admit_photo(1, 7, 20, 0))
            self.assertTrue(store.admit_photo(2, 7, 20, 0))

    def test_monitor_admission_has_no_unbounded_executor_backlog(self):
        with TemporaryDirectory() as directory:
            config = BotConfig(
                'fake', 'http://127.0.0.1', 's' * 40, Path(directory) / 'bot.db', monitor_workers=1
            )
            bot = HromiTelegramBot(config, Mock(), Mock())
            self.addCleanup(bot.close)
            bot.executor.submit = Mock()
            bot._start_comparison({'update_id': 1, 'finished': False})
            bot._start_comparison({'update_id': 2, 'finished': False})
            bot.executor.submit.assert_called_once()


class Response:
    def __init__(self, contents, declared=None):
        self.headers = Message()
        if declared is not None:
            self.headers['Content-Length'] = declared
        self.body = io.BytesIO(contents)

    def read(self, maximum):
        return self.body.read(maximum)


class TransportSecurityTests(TestCase):
    def test_telegram_paths_cannot_escape_file_endpoint(self):
        client = TelegramClient('test-token')
        for path in [
            '/file.jpg',
            '../file.jpg',
            'photos/../file.jpg',
            'photos//file.jpg',
            'photos\\file.jpg',
            'photos/file\n.jpg',
            None,
        ]:
            with self.subTest(path=path), patch('hromi_telegram_bot.http._request') as request:
                with self.assertRaises(ApiError):
                    client.download_file(path)
                request.assert_not_called()

    def test_telegram_path_is_encoded_without_redirecting_to_another_host(self):
        headers = Message()
        headers['Content-Type'] = 'image/jpeg'
        client = TelegramClient('test-token')
        with patch(
            'hromi_telegram_bot.http._request', return_value=(200, headers, b'image')
        ) as request:
            client.download_file('photos/file #1.jpg')
        self.assertEqual(
            request.call_args.args[1],
            'https://api.telegram.org/file/bottest-token/photos/file%20%231.jpg',
        )

    def test_backend_non_image_result_is_not_delivered(self):
        headers = Message()
        headers['Content-Type'] = 'text/html'
        with patch('hromi_telegram_bot.http._request', return_value=(200, headers, b'<script>')):
            with self.assertRaises(ApiError) as error:
                HromiClient('https://backend.example', 'fixture-key').result(7, 'job')
        self.assertEqual(error.exception.code, 'invalid_result_type')

    def test_telegram_malformed_upload_response_becomes_sanitized_api_error(self):
        for raw in [b'not-json', b'[]', b'null']:
            with (
                self.subTest(raw=raw),
                patch('hromi_telegram_bot.http._request', return_value=(200, Message(), raw)),
            ):
                with self.assertRaises(ApiError) as error:
                    TelegramClient('test-token').send_photo(7, b'image', 'x.png', 'image/png', '')
            self.assertEqual(error.exception.code, 'invalid_json_response')

    def test_actual_and_declared_response_limits(self):
        for response in [Response(b'x' * 100), Response(b'', '100'), Response(b'', 'invalid')]:
            with self.assertRaises(ApiError):
                _read_bounded(response, 10, time.monotonic() + 1)

    def test_total_deadline_is_enforced(self):
        with self.assertRaises(ApiError):
            _read_bounded(Response(b'x'), 10, time.monotonic() - 1)

    def test_redirects_are_never_followed_with_credentials(self):
        with self.assertRaises(ValueError):
            _request('GET', 'http://remote.test', headers={'Authorization': 'Bearer secret'})
        self.assertIsNone(
            NoRedirects().redirect_request(None, None, 302, '', {}, 'http://evil.test')
        )
        error = HTTPError(
            'https://service.test/private-secret', 302, 'redirect', Message(), io.BytesIO(b'{}')
        )
        with patch('hromi_telegram_bot.http._OPENER.open', side_effect=error) as opened:
            with self.assertRaises(ApiError) as result:
                _request(
                    'GET',
                    'https://service.test',
                    headers={'Authorization': 'Bearer private-secret'},
                )
            self.assertEqual(opened.call_count, 1)
            self.assertIsNone(result.exception.__cause__)
            self.assertNotIn('private-secret', str(result.exception))

    def test_error_body_does_not_propagate_untrusted_secrets(self):
        error = HTTPError(
            'https://service.test',
            500,
            '',
            Message(),
            io.BytesIO(b'{"error":"https://secret.test/token"}'),
        )
        with patch('hromi_telegram_bot.http._OPENER.open', side_effect=error):
            with self.assertRaises(ApiError) as result:
                _request('GET', 'https://service.test')
        self.assertEqual(result.exception.code, 'http_500')
