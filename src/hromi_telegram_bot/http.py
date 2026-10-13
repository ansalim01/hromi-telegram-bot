"""Dependency-free HTTP clients for Telegram and the Hromi backend."""

from __future__ import annotations

import base64
import json
import mimetypes
import secrets
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class ApiError(RuntimeError):
    def __init__(self, service: str, status: int | None, code: str):
        super().__init__(f'{service}: {code}')
        self.service = service
        self.status = status
        self.code = code


def _request(method, url, *, headers=None, body=None, timeout=40):
    request = Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.headers, response.read()
    except HTTPError as error:
        payload = error.read()
        try:
            code = json.loads(payload).get('error') or json.loads(payload).get('description')
        except (ValueError, AttributeError):
            code = None
        raise ApiError('http', error.code, code or f'http_{error.code}') from error
    except (URLError, TimeoutError, OSError) as error:
        raise ApiError('http', None, 'connection_failed') from error


def _json_request(method, url, payload=None, *, headers=None, timeout=40):
    request_headers = {'Accept': 'application/json', **(headers or {})}
    body = None
    if payload is not None:
        body = json.dumps(payload, separators=(',', ':')).encode()
        request_headers['Content-Type'] = 'application/json'
    status, _, raw = _request(
        method, url, headers=request_headers, body=body, timeout=timeout,
    )
    try:
        data = json.loads(raw)
    except ValueError as error:
        raise ApiError('http', status, 'invalid_json_response') from error
    return data


def _multipart(fields, file_field, filename, content_type, contents):
    boundary = '----hromi-' + secrets.token_hex(16)
    chunks = []
    for name, value in fields.items():
        chunks.extend([
            f'--{boundary}\r\n'.encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            str(value).encode(), b'\r\n',
        ])
    safe_filename = filename.replace('"', '').replace('\r', '').replace('\n', '')
    chunks.extend([
        f'--{boundary}\r\n'.encode(),
        (f'Content-Disposition: form-data; name="{file_field}"; '
         f'filename="{safe_filename}"\r\n').encode(),
        f'Content-Type: {content_type}\r\n\r\n'.encode(),
        contents, b'\r\n', f'--{boundary}--\r\n'.encode(),
    ])
    return f'multipart/form-data; boundary={boundary}', b''.join(chunks)


class HromiClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key

    def _headers(self, user_id: int):
        return {
            'Authorization': 'Bearer ' + self.api_key,
            'X-Hromi-Principal': f'telegram:{user_id}',
        }

    def models(self, user_id: int):
        data = _json_request(
            'GET', self.base_url + '/api/v1/models', headers=self._headers(user_id),
        )
        return data.get('models', [])

    def create_job(self, user_id: int, update_id: int, filename: str,
                   content_type: str, contents: bytes, model: str):
        headers = self._headers(user_id)
        headers['Idempotency-Key'] = f'telegram-update-{update_id}'
        return _json_request('POST', self.base_url + '/api/v1/jobs', {
            'filename': filename,
            'content_type': content_type,
            'image': base64.b64encode(contents).decode('ascii'),
            'model': model,
        }, headers=headers, timeout=60)

    def job(self, user_id: int, job_id: str):
        return _json_request(
            'GET', self.base_url + '/api/v1/jobs/' + quote(job_id, safe=''),
            headers=self._headers(user_id),
        )

    def result(self, user_id: int, job_id: str):
        status, headers, body = _request(
            'GET', self.base_url + '/api/v1/jobs/' + quote(job_id, safe='') + '/result',
            headers=self._headers(user_id), timeout=120,
        )
        return body, headers.get_content_type(), status


class TelegramClient:
    def __init__(self, token: str):
        self._base_url = 'https://api.telegram.org/bot' + token
        self._file_url = 'https://api.telegram.org/file/bot' + token

    def call(self, method: str, payload=None, timeout=40):
        try:
            data = _json_request(
                'POST', self._base_url + '/' + method, payload or {}, timeout=timeout,
            )
        except ApiError as error:
            error.service = 'telegram'
            raise
        if not data.get('ok'):
            raise ApiError('telegram', data.get('error_code'),
                           data.get('description', 'telegram_error'))
        return data.get('result')

    def get_updates(self, offset: int, timeout: int):
        return self.call('getUpdates', {
            'offset': offset,
            'timeout': timeout,
            'allowed_updates': ['message', 'callback_query'],
        }, timeout=timeout + 10)

    def get_file(self, file_id: str):
        return self.call('getFile', {'file_id': file_id})

    def download_file(self, file_path: str):
        _, headers, body = _request(
            'GET', self._file_url + '/' + quote(file_path, safe='/'), timeout=120,
        )
        content_type = headers.get_content_type()
        if content_type == 'application/octet-stream':
            content_type = mimetypes.guess_type(file_path)[0] or content_type
        return body, content_type

    def send_message(self, chat_id: int, text: str, reply_markup=None):
        payload = {'chat_id': chat_id, 'text': text}
        if reply_markup:
            payload['reply_markup'] = reply_markup
        return self.call('sendMessage', payload)

    def edit_message(self, chat_id: int, message_id: int, text: str):
        return self.call('editMessageText', {
            'chat_id': chat_id, 'message_id': message_id, 'text': text,
        })

    def answer_callback(self, callback_id: str, text: str = ''):
        payload = {'callback_query_id': callback_id}
        if text:
            payload['text'] = text
        return self.call('answerCallbackQuery', payload)

    def send_chat_action(self, chat_id: int, action: str):
        return self.call('sendChatAction', {'chat_id': chat_id, 'action': action})

    def _send_file(self, method, field, chat_id, contents, filename, content_type, caption):
        multipart_type, body = _multipart(
            {'chat_id': chat_id, 'caption': caption}, field, filename,
            content_type, contents,
        )
        try:
            _, _, raw = _request(
                'POST', self._base_url + '/' + method,
                headers={'Content-Type': multipart_type}, body=body, timeout=120,
            )
            data = json.loads(raw)
        except ApiError as error:
            error.service = 'telegram'
            raise
        if not data.get('ok'):
            raise ApiError('telegram', data.get('error_code'),
                           data.get('description', 'telegram_error'))
        return data.get('result')

    def send_photo(self, chat_id: int, contents: bytes, filename: str,
                   content_type: str, caption: str):
        try:
            return self._send_file(
                'sendPhoto', 'photo', chat_id, contents, filename, content_type, caption,
            )
        except ApiError as error:
            if error.status != 400:
                raise
            return self._send_file(
                'sendDocument', 'document', chat_id, contents, filename,
                content_type, caption,
            )
