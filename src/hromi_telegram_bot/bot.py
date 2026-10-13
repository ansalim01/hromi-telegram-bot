"""Long-polling Telegram bot that submits photos to Hromi."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging
import mimetypes
from pathlib import Path
import threading

from .config import BotConfig
from .http import ApiError, HromiClient, TelegramClient
from .store import BotStore, TERMINAL_STATES


LOG = logging.getLogger('hromi.telegram')
ALLOWED_TYPES = {'image/jpeg', 'image/png', 'image/webp'}
STATE_LABELS = {
    'QUEUED': 'В очереди',
    'PREPARING': 'Запускаю и подготавливаю GPU',
    'SUBMITTING': 'Отправляю фото в ComfyUI',
    'RUNNING': 'Обрабатываю фотографию',
    'SUCCEEDED': 'Готово',
    'FAILED': 'Обработка завершилась ошибкой',
    'UNKNOWN': 'Результат обработки неизвестен',
}


PROCESSING_NOTICE = 'Обычно обработка занимает 5–10 минут. При очереди может быть дольше.'
ACTIVE_PHOTO_NOTICE = ('У вас уже есть фото в обработке. '
                       'Дождитесь результата, затем отправьте следующее.')


class HromiTelegramBot:
    def __init__(self, config: BotConfig, telegram=None, hromi=None, store=None):
        self.config = config
        self.telegram = telegram or TelegramClient(config.telegram_token)
        self.hromi = hromi or HromiClient(
            config.hromi_base_url, config.hromi_api_key,
        )
        self.store = store or BotStore(config.database_path)
        self.executor = ThreadPoolExecutor(
            max_workers=config.monitor_workers, thread_name_prefix='hromi-job',
        )
        self._monitoring = set()
        self._monitor_lock = threading.Lock()
        self._closed = threading.Event()

    def close(self):
        self._closed.set()
        self.executor.shutdown(wait=False, cancel_futures=True)

    def run(self):
        for job in self.store.active_jobs():
            self._start_monitor(job)
        offset = self.store.offset()
        LOG.info('Telegram bot started; restored_jobs=%s', len(self.store.active_jobs()))
        while not self._closed.is_set():
            try:
                updates = self.telegram.get_updates(
                    offset, self.config.telegram_poll_seconds,
                )
                for update in updates:
                    update_id = int(update['update_id'])
                    try:
                        self.handle_update(update)
                    except ApiError as error:
                        if self._should_retry_update(error):
                            LOG.warning('Update deferred update_id=%s code=%s',
                                        update_id, error.code)
                            self._closed.wait(2)
                            break
                        LOG.warning('Update discarded update_id=%s status=%s code=%s',
                                    update_id, error.status, error.code)
                    except Exception as error:  # one bad update must not stop polling
                        LOG.exception('Update failed update_id=%s type=%s',
                                      update_id, type(error).__name__)
                    offset = max(offset, update_id + 1)
                    self.store.set_offset(offset)
            except ApiError as error:
                LOG.warning('Telegram polling unavailable code=%s', error.code)
                self._closed.wait(2)

    @staticmethod
    def _should_retry_update(error: ApiError):
        """Retry temporary failures, but consume permanent invalid updates."""
        return error.status is None or error.status == 429 or error.status >= 500

    def _allowed(self, user_id: int):
        return not self.config.allowed_user_ids or user_id in self.config.allowed_user_ids

    def handle_update(self, update):
        if 'callback_query' in update:
            self._handle_callback(update['callback_query'])
            return
        message = update.get('message')
        if not message or 'from' not in message or 'chat' not in message:
            return
        user_id = int(message['from']['id'])
        chat_id = int(message['chat']['id'])
        if not self._allowed(user_id):
            self.telegram.send_message(chat_id, 'Доступ к этому боту закрыт.')
            return
        text = (message.get('text') or '').strip().split('@', 1)[0]
        if text in {'/start', '/help'}:
            self.telegram.send_message(
                chat_id,
                'Отправьте фотографию, и я обработаю её выбранной моделью. '
                'Используйте /model, чтобы изменить модель. Отправляя фото, '
                'подтвердите, что имеете право его обрабатывать.',
            )
            self._show_models(user_id, chat_id)
        elif text == '/model':
            self._show_models(user_id, chat_id)
        elif message.get('photo') or message.get('document'):
            self._handle_photo(int(update['update_id']), user_id, chat_id, message)
        elif text:
            self.telegram.send_message(
                chat_id, 'Сначала выберите модель командой /model, затем отправьте фото.',
            )

    def _models(self, user_id: int):
        models = self.hromi.models(user_id)
        return [model for model in models if isinstance(model.get('id'), str)]

    def _show_models(self, user_id: int, chat_id: int):
        models = self._models(user_id)
        if not models:
            self.telegram.send_message(chat_id, 'Сейчас нет доступных моделей.')
            return
        selected = self.store.selected_model(user_id)
        rows = [[{
            'text': ('✓ ' if model['id'] == selected else '') + model['label'],
            'callback_data': 'model:' + model['id'],
        }] for model in models]
        self.telegram.send_message(
            chat_id, 'Выберите модель, затем отправьте фотографию:',
            {'inline_keyboard': rows},
        )

    def _handle_callback(self, callback):
        user_id = int(callback['from']['id'])
        callback_id = callback['id']
        message = callback.get('message') or {}
        chat_id = int((message.get('chat') or {}).get('id', user_id))
        if not self._allowed(user_id):
            self.telegram.answer_callback(callback_id, 'Доступ закрыт')
            return
        data = callback.get('data') or ''
        if not data.startswith('model:'):
            self.telegram.answer_callback(callback_id)
            return
        model_id = data[6:]
        models = {model['id']: model for model in self._models(user_id)}
        if model_id not in models:
            self.telegram.answer_callback(callback_id, 'Модель больше недоступна')
            return
        self.store.set_model(user_id, model_id)
        self.telegram.answer_callback(callback_id, 'Модель выбрана')
        self.telegram.send_message(
            chat_id, f'Выбрано: {models[model_id]["label"]}. Теперь отправьте фото.',
        )

    def _photo_reference(self, user_id: int, message):
        if message.get('photo'):
            photo = max(message['photo'], key=lambda item: item.get('file_size', 0))
            return photo['file_id'], f'telegram-{user_id}-{message["message_id"]}.jpg', \
                'image/jpeg', photo.get('file_size')
        document = message.get('document') or {}
        content_type = document.get('mime_type')
        if content_type not in ALLOWED_TYPES:
            raise ValueError('unsupported_type')
        filename = Path(document.get('file_name') or 'photo').name
        return document['file_id'], filename, content_type, document.get('file_size')

    def _handle_photo(self, update_id: int, user_id: int, chat_id: int, message):
        existing = self.store.job_for_update(update_id)
        if existing:
            self._start_monitor(existing)
            return
        if self.store.has_active_job(user_id):
            self.telegram.send_message(chat_id, ACTIVE_PHOTO_NOTICE)
            return
        try:
            file_id, filename, declared_type, declared_size = self._photo_reference(
                user_id, message,
            )
            if declared_size and declared_size > self.config.max_photo_bytes:
                raise ValueError('too_large')
            telegram_file = self.telegram.get_file(file_id)
            contents, downloaded_type = self.telegram.download_file(telegram_file['file_path'])
            if len(contents) > self.config.max_photo_bytes:
                raise ValueError('too_large')
            content_type = declared_type or downloaded_type
            if content_type not in ALLOWED_TYPES:
                content_type = mimetypes.guess_type(filename)[0]
            if content_type not in ALLOWED_TYPES:
                raise ValueError('unsupported_type')
        except ValueError as error:
            text = ('Файл больше 8 МБ.' if str(error) == 'too_large'
                    else 'Нужен JPEG, PNG или WebP.')
            self.telegram.send_message(chat_id, text)
            return

        models = self._models(user_id)
        if not models:
            self.telegram.send_message(chat_id, 'Сейчас нет доступных моделей.')
            return
        allowed = {model['id'] for model in models}
        selected = self.store.selected_model(user_id)
        if selected not in allowed:
            selected = models[0]['id']
            self.store.set_model(user_id, selected)
        try:
            job = self.hromi.create_job(
                user_id, update_id, filename, content_type, contents, selected,
            )
        except ApiError as error:
            if error.code == 'principal_active_limit':
                self.telegram.send_message(chat_id, ACTIVE_PHOTO_NOTICE)
                return
            raise
        stored = self.store.add_job(update_id, user_id, chat_id, job['id'], None)
        queue_position = job.get('queue_position')
        suffix = f' Позиция: {queue_position}.' if queue_position else ''
        try:
            progress = self.telegram.send_message(
                chat_id, 'Фото принято и поставлено в очередь.' + suffix + '\n' + PROCESSING_NOTICE,
            )
            self.store.set_progress_message(job['id'], progress.get('message_id'))
            stored['progress_message_id'] = progress.get('message_id')
        finally:
            self._start_monitor(stored)

    def _start_monitor(self, job):
        job_id = job['hromi_job_id']
        with self._monitor_lock:
            if job_id in self._monitoring or job['state'] in TERMINAL_STATES:
                return
            self._monitoring.add(job_id)
        self.executor.submit(self._monitor, job)

    def _status_text(self, current):
        text = STATE_LABELS.get(current['state'], current['state'])
        position = current.get('queue_position')
        text += f'. Позиция: {position}.' if position else '.'
        if current['state'] in STATE_LABELS and current['state'] not in {'SUCCEEDED', 'FAILED', 'UNKNOWN'}:
            text += '\n' + PROCESSING_NOTICE
        return text

    def _update_progress(self, job, current):
        text = self._status_text(current)
        if text == job.get('last_status_text'):
            return
        message_id = job.get('progress_message_id')
        if message_id:
            try:
                self.telegram.edit_message(job['chat_id'], message_id, text)
            except ApiError as error:
                if 'message is not modified' not in error.code.lower():
                    LOG.warning('Progress update failed job_id=%s code=%s',
                                job['hromi_job_id'], error.code)
        job['last_status_text'] = text
        self.store.update_job(job['hromi_job_id'], current['state'], text)

    def _monitor(self, job):
        job_id = job['hromi_job_id']
        failures = 0
        try:
            while not self._closed.is_set():
                try:
                    current = self.hromi.job(job['user_id'], job_id)
                    failures = 0
                except ApiError as error:
                    failures += 1
                    LOG.warning('Job status unavailable job_id=%s code=%s', job_id, error.code)
                    self._closed.wait(min(30, 2 ** min(failures, 5)))
                    continue
                state = current.get('state')
                if state == 'SUCCEEDED':
                    try:
                        self.telegram.send_chat_action(job['chat_id'], 'upload_photo')
                        contents, content_type, _ = self.hromi.result(
                            job['user_id'], job_id,
                        )
                        extension = {'image/jpeg': 'jpg', 'image/png': 'png',
                                     'image/webp': 'webp'}.get(content_type, 'png')
                        self.telegram.send_photo(
                            job['chat_id'], contents, f'hromi-result.{extension}',
                            content_type, self._status_text(current),
                        )
                    except ApiError as error:
                        failures += 1
                        LOG.warning('Result delivery unavailable job_id=%s code=%s',
                                    job_id, error.code)
                        self._closed.wait(min(30, 2 ** min(failures, 5)))
                        continue
                    self._update_progress(job, current)
                    self.store.update_job(job_id, 'DELIVERED', self._status_text(current))
                    return
                if state in {'FAILED', 'UNKNOWN'}:
                    self._update_progress(job, current)
                    self.store.update_job(job_id, state, state)
                    self.telegram.send_message(
                        job['chat_id'], 'Не удалось обработать фотографию. Код: ' + state,
                    )
                    return
                self._update_progress(job, current)
                self._closed.wait(self.config.poll_interval_seconds)
        except Exception as error:
            LOG.exception('Job monitor stopped job_id=%s type=%s',
                          job_id, type(error).__name__)
            try:
                self.telegram.send_message(
                    job['chat_id'], 'Связь временно потеряна. Задание сохранено; '
                    'бот продолжит после перезапуска.',
                )
            except Exception:
                pass
        finally:
            with self._monitor_lock:
                self._monitoring.discard(job_id)
