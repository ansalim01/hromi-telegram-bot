"""Long-polling Telegram bot that submits photos to Hromi."""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import BotConfig
from .http import ApiError, HromiClient, TelegramClient
from .store import TERMINAL_STATES, BotStore

LOG = logging.getLogger('hromi.telegram')
# Comparison runs these model families in order; the backend resolves each
# gender-neutral key to the variant for the selected subject.
COMPARISON_MODELS = ('sd15-inpaint', 'flux-fill', 'qwen-image-2.1')
GENDER_KEYBOARD = {
    'inline_keyboard': [
        [
            {'text': '👨 Мужчина', 'callback_data': 'gender:male'},
            {'text': '👩 Женщина', 'callback_data': 'gender:female'},
        ]
    ]
}
RULES_VERSION = 1
ACCEPT_RULES = f'rules:accept:{RULES_VERSION}'
RULES_TEXT = (
    'Правила Hromi\n\n'
    '• Ботом могут пользоваться только люди от 18 лет.\n'
    '• Отправляйте фото, на которых все люди совершеннолетние.\n'
    '• Если на фото другой человек, получите его согласие на обработку.\n\n'
    'Принимая правила, вы подтверждаете, что вам есть 18 лет и вы будете их соблюдать.'
)
MENU_KEYBOARD = {
    'inline_keyboard': [
        [{'text': '📷 Отправить фото', 'callback_data': 'menu:photo'}],
        [
            {'text': '👤 Кто на фото', 'callback_data': 'menu:gender'},
            {'text': '📋 Правила', 'callback_data': 'menu:rules'},
        ],
    ]
}
BACK_ROW = [{'text': '‹ В меню', 'callback_data': 'menu:home'}]
GENDER_LABELS = {'male': 'мужчина', 'female': 'женщина'}
MODELS_UNAVAILABLE = (
    'Сравнение SD 1.5, FLUX и Qwen пока недоступно: модели ещё не готовы. Попробуйте позже.'
)
AGE_CHECK_MESSAGES = {
    'BLOCKED_UNDERAGE': 'Не удалось подтвердить, что на фото только взрослые.',
    'BLOCKED_UNCERTAIN': (
        'Не удалось подтвердить, что на фото только взрослые. '
        'Попробуйте чёткий снимок одного взрослого человека с открытым лицом.'
    ),
    'BLOCKED_NO_FACE': 'На фото не видно лица. Пришлите чёткий снимок взрослого человека анфас.',
    'BLOCKED_ERROR': 'Проверка возраста временно недоступна. Попробуйте позже.',
}
ALLOWED_TYPES = {'image/jpeg', 'image/png', 'image/webp'}
STATE_LABELS = {
    'QUEUED': 'В очереди',
    'PREPARING': 'Запускаю и подготавливаю GPU',
    'SUBMITTING': 'Передаю фото на обработку',
    'RUNNING': 'Обрабатываю фотографию',
    'SUCCEEDED': 'Готово',
    'FAILED': 'Эта модель не смогла обработать фото.',
    'UNKNOWN': 'Не удалось получить результат от этой модели.',
}


PROCESSING_NOTICE = 'Обычно обработка занимает 5–10 минут. При очереди может быть дольше.'
ACTIVE_PHOTO_NOTICE = (
    'У вас уже есть фото в обработке. Дождитесь результата, затем отправьте следующее.'
)
PHOTO_GUIDANCE = (
    'Для лучшего результата загрузите чёткое фото взрослого человека в полный рост, '
    'вид спереди. Человек должен быть один, а руки, ноги и одежда — хорошо видны. '
    'Лучше всего подходит одежда в один слой, без пальто, многослойных и мешковатых вещей.'
)


class HromiTelegramBot:
    def __init__(self, config: BotConfig, telegram=None, hromi=None, store=None):
        self.config = config
        self.telegram = telegram or TelegramClient(config.telegram_token, config.max_photo_bytes)
        self.hromi = hromi or HromiClient(
            config.hromi_base_url,
            config.hromi_api_key,
        )
        self.store = store or BotStore(config.database_path)
        self.executor = ThreadPoolExecutor(
            max_workers=config.monitor_workers,
            thread_name_prefix='hromi-job',
        )
        self._monitoring = set()
        self._monitor_lock = threading.Lock()
        self._closed = threading.Event()

    def close(self):
        self._closed.set()
        self.executor.shutdown(wait=False, cancel_futures=True)

    def _restore_monitors(self):
        for job in self.store.active_jobs():
            self._start_monitor(job)
        for comparison in self.store.active_comparisons():
            self._start_comparison(comparison)
        for user_id in self.store.pending_photo_users():
            if self.store.gender(user_id) and not self.store.has_active_job(user_id):
                self._resume_pending_photo(user_id)

    def run(self):
        self._restore_monitors()
        offset = self.store.offset()
        LOG.info('Telegram bot started; restored_jobs=%s', len(self.store.active_jobs()))
        while not self._closed.is_set():
            try:
                self._restore_monitors()
                updates = self.telegram.get_updates(
                    offset,
                    self.config.telegram_poll_seconds,
                )
                for update in updates:
                    update_id = int(update['update_id'])
                    try:
                        self.handle_update(update)
                    except ApiError as error:
                        if self._should_retry_update(error):
                            LOG.warning(
                                'Update deferred update_id=%s code=%s', update_id, error.code
                            )
                            self._closed.wait(2)
                            break
                        LOG.warning(
                            'Update discarded update_id=%s status=%s code=%s',
                            update_id,
                            error.status,
                            error.code,
                        )
                    except Exception as error:  # one bad update must not stop polling
                        LOG.error(
                            'Update failed update_id=%s type=%s', update_id, type(error).__name__
                        )
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
        managed_access = self.hromi.telegram_access(user_id)
        if managed_access is not None:
            return managed_access
        return self.config.allow_any_user or user_id in self.config.allowed_user_ids

    def _chat_allowed(self, user_id, chat_id):
        return chat_id == user_id or chat_id in self.config.allowed_group_ids

    def handle_update(self, update):
        if 'callback_query' in update:
            self._handle_callback(update['callback_query'])
            return
        message = update.get('message')
        if not message or 'from' not in message or 'chat' not in message:
            return
        user_id = int(message['from']['id'])
        chat_id = int(message['chat']['id'])
        if not self._chat_allowed(user_id, chat_id):
            return
        if not self._allowed(user_id):
            self.telegram.send_message(
                chat_id,
                f'Доступ к этому боту закрыт. Ваш Telegram ID: {user_id}. '
                'Передайте его администратору, чтобы запросить доступ.',
            )
            return
        text = (message.get('text') or '').strip().split('@', 1)[0]
        if text == '/rules' or not self.store.accepted_rules(user_id, RULES_VERSION):
            self._show_rules(user_id, chat_id)
        elif text in {'/start', '/help'}:
            self._show_menu(chat_id)
        elif text == '/gender':
            self._ask_gender(user_id, chat_id)
        elif text == '/model':
            self._show_models(user_id, chat_id)
        elif message.get('photo') or message.get('document'):
            self._handle_photo(int(update['update_id']), user_id, chat_id, message)
        elif text:
            self._show_menu(chat_id)

    def _models(self, user_id: int, gender: str | None = None):
        gender = gender or self.store.gender(user_id)
        if gender not in GENDER_LABELS:
            return []
        catalog = {
            model.get('id'): model
            for model in self.hromi.models(user_id)
            if model.get('availability') == 'ready'
        }
        if not catalog:
            return []
        # The backend resolves the bare key to the variant for this gender.
        models = []
        for key in COMPARISON_MODELS:
            model = catalog.get(key)
            if model is None:
                variant = f'{key}-{gender}'
                model = catalog.get(variant)
                if model is None:
                    return []
                model = {**model, 'id': key}
            models.append(model)
        return models

    def _show_rules(self, user_id: int, chat_id: int):
        accepted = self.store.accepted_rules(user_id, RULES_VERSION)
        row = (
            BACK_ROW
            if accepted
            else [{'text': 'Мне есть 18, принимаю', 'callback_data': ACCEPT_RULES}]
        )
        self.telegram.send_message(chat_id, RULES_TEXT, reply_markup={'inline_keyboard': [row]})

    def _show_menu(self, chat_id: int):
        self.telegram.send_message(
            chat_id,
            'Отправьте одно фото. Hromi обработает его тремя моделями '
            'и пришлёт результаты по очереди для сравнения.',
            reply_markup=MENU_KEYBOARD,
        )

    def _show_photo_help(self, user_id: int, chat_id: int):
        gender = self.store.gender(user_id)
        selected = (
            f'Сейчас выбрано: {GENDER_LABELS[gender]}.'
            if gender
            else 'Выберите, кто на фото, кнопкой под сообщением.'
        )
        self.telegram.send_message(
            chat_id,
            'Подготовьте фото одного взрослого человека. Лицо должно быть хорошо видно.\n'
            + PHOTO_GUIDANCE
            + '\n\n'
            + selected
            + '\n📎 Нажмите на скрепку, выберите снимок и отправьте его в этот чат. '
            'Подойдут фото и файлы JPEG, PNG или WebP.',
            reply_markup={'inline_keyboard': [*GENDER_KEYBOARD['inline_keyboard'], BACK_ROW]},
        )

    def _ask_gender(self, user_id: int, chat_id: int):
        gender = self.store.gender(user_id)
        selected = f'Сейчас выбрано: {GENDER_LABELS[gender]}.\n' if gender else ''
        self.telegram.send_message(
            chat_id,
            selected + 'Кто на фото? Выбор сохранится для следующих фотографий.',
            reply_markup={'inline_keyboard': [*GENDER_KEYBOARD['inline_keyboard'], BACK_ROW]},
        )

    def _show_models(self, user_id: int, chat_id: int):
        gender = self.store.gender(user_id)
        models = self._models(user_id, gender)
        if gender is None:
            text = (
                'Я обработаю одно фото тремя моделями: SD 1.5, FLUX и Qwen. '
                'Результаты придут отдельно, чтобы их было удобно сравнить.\n\n'
                'Сначала выберите, кто будет на фото.'
            )
        elif not models:
            text = f'Сейчас выбрано: {GENDER_LABELS[gender]}.\n\n' + MODELS_UNAVAILABLE
        else:
            text = (
                f'Сейчас выбрано: {GENDER_LABELS[gender]}.\n'
                'Чтобы изменить выбор, нажмите кнопку под сообщением.\n\n'
                'Отправьте фотографию, на обработку которой у вас есть разрешение. '
                'Я обработаю её по очереди и пришлю каждый результат отдельно:\n'
                + '\n'.join(f'• {model["label"]}' for model in models)
                + '\n\n'
                + PHOTO_GUIDANCE
                + '\nМожно отправить фото или файл JPEG, PNG или WebP.'
            )
        self.telegram.send_message(chat_id, text, reply_markup=GENDER_KEYBOARD)

    def _handle_callback(self, callback):
        user_id = int(callback['from']['id'])
        callback_id = callback['id']
        message = callback.get('message') or {}
        chat_id = int((message.get('chat') or {}).get('id', user_id))
        if not self._chat_allowed(user_id, chat_id):
            self.telegram.answer_callback(callback_id, 'Используйте личный чат с ботом')
            return
        if not self._allowed(user_id):
            self.telegram.answer_callback(callback_id, 'Доступ закрыт')
            return
        data = callback.get('data') or ''
        if data == ACCEPT_RULES:
            self.store.accept_rules(user_id, RULES_VERSION)
            self.telegram.answer_callback(callback_id, 'Правила приняты')
            self._show_menu(chat_id)
            return
        if not self.store.accepted_rules(user_id, RULES_VERSION):
            self.telegram.answer_callback(callback_id, 'Сначала примите правила')
            self._show_rules(user_id, chat_id)
            return
        menu_actions = {
            'menu:home': lambda: self._show_menu(chat_id),
            'menu:photo': lambda: self._show_photo_help(user_id, chat_id),
            'menu:gender': lambda: self._ask_gender(user_id, chat_id),
            'menu:rules': lambda: self._show_rules(user_id, chat_id),
        }
        if data in menu_actions:
            self.telegram.answer_callback(callback_id)
            menu_actions[data]()
            return
        if data.startswith('gender:'):
            gender = data[7:]
            if gender not in GENDER_LABELS:
                self.telegram.answer_callback(callback_id, 'Неизвестный выбор')
                return
            self.store.set_gender(user_id, gender)
            self.telegram.answer_callback(callback_id, 'Выбрано: ' + GENDER_LABELS[gender])
            self._resume_pending_photo(user_id)
            self._show_models(user_id, chat_id)
            return
        if not data.startswith('model:'):
            self.telegram.answer_callback(callback_id)
            return
        model_id = data[6:]
        models = {model['id']: model for model in self._models(user_id)}
        # Accept both the base key and a gender-suffixed id from older keyboards.
        base_id = model_id
        for gender in GENDER_LABELS:
            suffix = '-' + gender
            if base_id.endswith(suffix):
                base_id = base_id[: -len(suffix)]
                break
        if base_id not in models:
            self.telegram.answer_callback(callback_id, 'Модель больше недоступна')
            return
        self.telegram.answer_callback(callback_id, 'Включено сравнение всех моделей')
        self._show_models(user_id, chat_id)

    def _store_pending_photo(self, update_id: int, user_id: int, chat_id: int, message):
        if not self.store.admit_photo(
            update_id, user_id, self.config.max_active_photos, self.config.daily_photos_per_user
        ):
            self.telegram.send_message(
                chat_id, 'Лимит фотографий или очередь исчерпаны. Попробуйте позже.'
            )
            return
        try:
            file_id, filename, declared_type, declared_size = self._photo_reference(
                user_id,
                message,
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
            text = 'Файл больше 8 МБ.' if str(error) == 'too_large' else 'Нужен JPEG, PNG или WebP.'
            self.telegram.send_message(chat_id, text)
            return
        self.store.set_pending_photo(
            user_id,
            update_id,
            chat_id,
            json.dumps(
                {
                    'filename': filename,
                    'telegram_username': (message.get('from') or {}).get('username'),
                    'content_type': content_type,
                    'image': base64.b64encode(contents).decode('ascii'),
                }
            ),
        )
        self.telegram.send_message(
            chat_id,
            'Фото получено. Выберите, кого обрабатываем:',
            reply_markup=GENDER_KEYBOARD,
        )

    def _resume_pending_photo(self, user_id: int):
        if not self.store.accepted_rules(user_id, RULES_VERSION):
            return
        pending = self.store.claim_pending_photo(user_id)
        if not pending:
            return
        update_id = pending['update_id']
        chat_id = pending['chat_id']
        if self.store.comparison(update_id) or self.store.job_for_update(update_id):
            return
        if self.store.has_active_job(user_id):
            return
        if not self.store.admit_photo(
            update_id, user_id, self.config.max_active_photos, self.config.daily_photos_per_user
        ):
            self.telegram.send_message(
                chat_id, 'Лимит фотографий или очередь исчерпаны. Попробуйте позже.'
            )
            return
        comparison = self.store.add_comparison(
            update_id,
            user_id,
            chat_id,
            json.loads(pending['message']),
            self._models(user_id),
            self.store.gender(user_id),
        )
        try:
            self.telegram.send_message(
                chat_id,
                f'Фото принято. Моделей для сравнения: {len(comparison["models"])}. '
                'Результаты будут приходить по очереди.\n'
                'Обычно каждая обработка занимает 5–10 минут; при очереди — дольше.',
            )
        finally:
            self._start_comparison(comparison)

    def _photo_reference(self, user_id: int, message):
        if message.get('photo'):
            photo = max(message['photo'], key=lambda item: item.get('file_size', 0))
            return (
                photo['file_id'],
                f'telegram-{user_id}-{message["message_id"]}.jpg',
                'image/jpeg',
                photo.get('file_size'),
            )
        document = message.get('document') or {}
        content_type = document.get('mime_type')
        if content_type not in ALLOWED_TYPES:
            raise ValueError('unsupported_type')
        filename = Path(document.get('file_name') or 'photo').name
        return document['file_id'], filename, content_type, document.get('file_size')

    def _handle_photo(self, update_id: int, user_id: int, chat_id: int, message):
        comparison = self.store.comparison(update_id)
        if comparison:
            self._start_comparison(comparison)
            return
        existing = self.store.job_for_update(update_id)
        if existing:
            self._start_monitor(existing)
            return
        if self.store.has_active_job(user_id):
            self.telegram.send_message(chat_id, ACTIVE_PHOTO_NOTICE)
            return
        gender = self.store.gender(user_id)
        if gender is None:
            self._store_pending_photo(update_id, user_id, chat_id, message)
            return
        models = self._models(user_id, gender)
        if not models:
            self.telegram.send_message(chat_id, MODELS_UNAVAILABLE)
            return
        if not self.store.admit_photo(
            update_id, user_id, self.config.max_active_photos, self.config.daily_photos_per_user
        ):
            self.telegram.send_message(
                chat_id, 'Лимит фотографий или очередь исчерпаны. Попробуйте позже.'
            )
            return
        try:
            file_id, filename, declared_type, declared_size = self._photo_reference(
                user_id,
                message,
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
            text = 'Файл больше 8 МБ.' if str(error) == 'too_large' else 'Нужен JPEG, PNG или WebP.'
            self.telegram.send_message(chat_id, text)
            return

        comparison = self.store.add_comparison(
            update_id,
            user_id,
            chat_id,
            {
                'filename': filename,
                'telegram_username': (message.get('from') or {}).get('username'),
                'content_type': content_type,
                'image': base64.b64encode(contents).decode('ascii'),
            },
            models,
            gender,
        )
        try:
            self.telegram.send_message(
                chat_id,
                f'Фото принято. Моделей для сравнения: {len(models)}. '
                'Результаты будут приходить по очереди.\n'
                'Обычно каждая обработка занимает 5–10 минут; при очереди — дольше.',
            )
        finally:
            self._start_comparison(comparison)

    def _start_comparison(self, comparison):
        key = ('comparison', comparison['update_id'])
        with self._monitor_lock:
            if comparison['finished'] or key in self._monitoring:
                return
            if len(self._monitoring) >= self.config.monitor_workers:
                return  # Durable work is picked up by the next polling iteration.
            self._monitoring.add(key)
        self.executor.submit(self._compare, comparison['update_id'])

    def _compare(self, update_id):
        key = ('comparison', update_id)
        failures = 0
        try:
            while not self._closed.is_set():
                try:
                    if self._comparison_step(update_id):
                        return
                    failures = 0
                except ApiError as error:
                    if not self._should_retry_update(error):
                        LOG.error(
                            'Comparison stopped update_id=%s service=%s status=%s code=%s',
                            update_id,
                            error.service,
                            error.status,
                            error.code,
                        )
                        self.store.finish_comparison(update_id)
                        return
                    failures += 1
                    LOG.warning(
                        'Comparison retry update_id=%s service=%s status=%s',
                        update_id,
                        error.service,
                        error.status,
                    )
                    self._closed.wait(min(30, 2 ** min(failures, 5)))
        except Exception:
            LOG.error('Comparison interrupted update_id=%s; polling will restore it', update_id)
        finally:
            with self._monitor_lock:
                self._monitoring.discard(key)

    def _comparison_step(self, update_id):
        comparison = self.store.comparison(update_id)
        if comparison['finished']:
            return True
        index = comparison['next_index']
        models = comparison['models']
        if index >= len(models):
            # Release the user before sending a nonessential completion notice.
            counts = self.store.comparison_counts(update_id)
            delivered = counts.get('DELIVERED', 0)
            self.store.finish_comparison(update_id)
            self.telegram.send_message(
                comparison['chat_id'],
                f'Сравнение завершено. Результатов: {delivered} из {len(models)}. '
                'Можно отправить следующее фото или изменить выбор под этим сообщением.',
                reply_markup=GENDER_KEYBOARD,
            )
            return True
        model = models[index]
        label = f'{index + 1}/{len(models)} · {model["label"]}'
        job_id = comparison['current_job_id']
        if job_id is None:
            payload = comparison['payload']
            # Comparisons stored before genders were persisted fall back to the
            # user's current setting so an upgrade does not strand them.
            gender = comparison.get('gender') or self.store.gender(comparison['user_id'])
            try:
                current = self.hromi.create_job(
                    comparison['user_id'],
                    f'{update_id}-model-{model["id"]}',
                    payload['filename'],
                    payload['content_type'],
                    base64.b64decode(payload['image']),
                    model['id'],
                    gender=gender,
                    **(
                        {'telegram_username': payload['telegram_username']}
                        if payload.get('telegram_username')
                        else {}
                    ),
                )
            except ApiError as error:
                if self._should_retry_update(error):
                    raise
                message = AGE_CHECK_MESSAGES.get(error.code)
                if error.code == 'telegram_access_denied':
                    message = 'У вас больше нет доступа к обработке фото в этом боте.'
                self.telegram.send_message(
                    comparison['chat_id'],
                    message or label + ': не удалось поставить в очередь. Код: ' + error.code,
                )
                if error.status in {401, 403}:
                    self.store.finish_comparison(update_id)
                    return True
                self.store.advance_comparison(update_id)
                return False
            job_id = current['id']
            self.store.set_comparison_job(update_id, job_id)
        # Migrate an in-flight comparison created by older bot versions lazily.
        self.store.set_comparison_job(update_id, job_id)
        step = self.store.comparison_step(update_id, index)
        if step['state'] in TERMINAL_STATES:
            self.store.advance_comparison(update_id)
            return False
        if not step['progress_message_id']:
            progress = self.telegram.send_message(
                comparison['chat_id'],
                label + '\n' + PROCESSING_NOTICE,
            )
            step['progress_message_id'] = progress.get('message_id')
            self.store.set_progress_message(job_id, step['progress_message_id'])
        state = self._monitor(
            {
                **step,
                'user_id': comparison['user_id'],
                'chat_id': comparison['chat_id'],
                'model_label': label,
            }
        )
        if state is None:
            return True
        self.store.advance_comparison(update_id)
        return False

    def _start_monitor(self, job):
        job_id = job['hromi_job_id']
        with self._monitor_lock:
            if job_id in self._monitoring or job['state'] in TERMINAL_STATES:
                return
            if len(self._monitoring) >= self.config.monitor_workers:
                return
            self._monitoring.add(job_id)
        self.executor.submit(self._monitor, job)

    def _status_text(self, current):
        age_message = AGE_CHECK_MESSAGES.get(current.get('error'))
        if age_message and current['state'] == 'FAILED':
            return age_message
        text = STATE_LABELS.get(current['state'], current['state'])
        position = current.get('queue_position')
        if current['state'] == 'QUEUED' and position:
            text += f'. Место в очереди: {position}'
        if not text.endswith('.'):
            text += '.'
        if current['state'] in STATE_LABELS and current['state'] not in {
            'SUCCEEDED',
            'FAILED',
            'UNKNOWN',
        }:
            text += '\n' + PROCESSING_NOTICE
        return text

    def _update_progress(self, job, current, persist=True):
        text = self._status_text(current)
        if job.get('model_label'):
            text = job['model_label'] + '\n' + text
        if text == job.get('last_status_text'):
            return
        message_id = job.get('progress_message_id')
        if message_id:
            try:
                self.telegram.edit_message(job['chat_id'], message_id, text)
            except ApiError as error:
                if 'message is not modified' not in error.code.lower():
                    LOG.warning(
                        'Progress update failed job_id=%s code=%s', job['hromi_job_id'], error.code
                    )
        job['last_status_text'] = text
        if persist:
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
                    if not self._should_retry_update(error):
                        self.store.update_job(job_id, 'FAILED', error.code)
                        self.telegram.send_message(
                            job['chat_id'],
                            'Задание недоступно. Код: ' + error.code,
                        )
                        if error.status in {401, 403} and job.get('model_label'):
                            raise
                        return 'FAILED'
                    failures += 1
                    LOG.warning('Job status unavailable job_id=%s code=%s', job_id, error.code)
                    self._closed.wait(min(30, 2 ** min(failures, 5)))
                    continue
                state = current.get('state')
                if state == 'SUCCEEDED':
                    try:
                        try:
                            self.telegram.send_chat_action(job['chat_id'], 'upload_photo')
                        except ApiError:
                            pass  # Typing/upload indicators must not block result delivery.
                        contents, content_type, _ = self.hromi.result(
                            job['user_id'],
                            job_id,
                        )
                        extension = {
                            'image/jpeg': 'jpg',
                            'image/png': 'png',
                            'image/webp': 'webp',
                        }.get(content_type, 'png')
                        delivered = self.telegram.send_photo(
                            job['chat_id'],
                            contents,
                            f'hromi-result.{extension}',
                            content_type,
                            (
                                job.get('model_label', '') + '\n' + self._status_text(current)
                            ).strip(),
                        )
                    except ApiError as error:
                        if not self._should_retry_update(error):
                            self.store.update_job(job_id, 'FAILED', error.code)
                            if error.status in {401, 403} and job.get('model_label'):
                                raise
                            return 'FAILED'
                        failures += 1
                        LOG.warning(
                            'Result delivery unavailable job_id=%s code=%s', job_id, error.code
                        )
                        self._closed.wait(min(30, 2 ** min(failures, 5)))
                        continue
                    self.store.mark_delivered(job_id, (delivered or {}).get('message_id'))
                    self._update_progress(job, current, persist=False)
                    return 'DELIVERED'
                if state in {'FAILED', 'UNKNOWN'}:
                    self._update_progress(job, current)
                    self.store.update_job(job_id, state, state)
                    if not job.get('progress_message_id'):
                        self.telegram.send_message(
                            job['chat_id'],
                            '\n'.join(
                                filter(None, (job.get('model_label'), self._status_text(current)))
                            ),
                        )
                    return state
                self._update_progress(job, current)
                self._closed.wait(self.config.poll_interval_seconds)
        except ApiError:
            raise
        except Exception as error:
            LOG.error('Job monitor stopped job_id=%s type=%s', job_id, type(error).__name__)
            try:
                self.telegram.send_message(
                    job['chat_id'],
                    'Связь временно потеряна. Задание сохранено; бот продолжит после перезапуска.',
                )
            except Exception as notification_error:
                LOG.warning(
                    'Recovery notice unavailable type=%s', type(notification_error).__name__
                )
        finally:
            with self._monitor_lock:
                self._monitoring.discard(job_id)
