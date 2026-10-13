# Интеграция Telegram-бота с Hromi

Это контракт интеграции Telegram-бота с текущим backend на 13 октября 2026 года.
Защищённый backend API `/api/v1` реализован; готового процесса Telegram-бота в
репозитории пока нет.

## Поддерживаемая схема сейчас

Самый простой вариант — запуск процесса бота на том же компьютере, где работает
backend:

```text
Telegram Bot API
       │
       ▼
Процесс бота ── http://127.0.0.1:8091 ──► Hromi backend
                                                    │
                                                    ▼
                                             Intelion + GPU VM
```

Если бот работает на другом сервере, `/api/v1` можно опубликовать через reverse
proxy с TLS. Старые admin endpoints сохраняют локальную проверку Host и не должны
публиковаться. Одного bind на `0.0.0.0` без firewall/TLS недостаточно.

## Пользовательский сценарий

1. `/start` — бот показывает описание и правила обработки фотографий.
2. `/model` — бот вызывает `GET /api/v1/models`; backend уже возвращает только allowlist
   разрешённых workflow.
3. Пользователь выбирает модель; бот сохраняет выбор по `telegram_user_id`.
4. Пользователь отправляет фотографию как `photo` или документ.
5. Бот скачивает файл через Telegram Bot API и проверяет размер/MIME.
6. Бот вызывает `POST /api/v1/jobs` с `Idempotency-Key`, сохраняет связь
   `telegram_chat_id ↔ hromi_job_id`.
7. Бот сообщает позицию и опрашивает `GET /api/v1/jobs/<id>` раз в 2–3 секунды.
8. При `SUCCEEDED` бот скачивает `result_url` и отправляет
   изображение пользователю.
9. При `FAILED` или `UNKNOWN` бот показывает короткое сообщение и сохраняет
   технический код в журнале.

## Какие endpoints нужны боту

| Endpoint | Когда используется |
|---|---|
| `GET /api/v1/health` | Startup/readiness бота |
| `GET /api/v1/models` | Формирование меню моделей |
| `POST /api/v1/jobs` | Создание задания |
| `GET /api/v1/jobs/<id>` | Опрос состояния и позиции |
| `GET /api/v1/jobs/<id>/result` | Получение готового файла |

Боту не нужны `/vm/*`, `/gpu/*`, `/deployment/*`, `/job-records/*` и прямой
доступ к ComfyUI.

## Псевдокод обработчика

```python
async def handle_photo(message, selected_workflow):
    raw, filename, content_type = await download_telegram_photo(message)

    job = await hromi.post_json("/api/v1/jobs", {
        "filename": filename,
        "content_type": content_type,
        "image": base64.b64encode(raw).decode("ascii"),
        "model": selected_workflow,
    }, headers={"Idempotency-Key": str(message.message_id)})

    while True:
        current = await hromi.get_json(f"/api/v1/jobs/{job['id']}")
        if current["state"] == "SUCCEEDED":
            result = await hromi.get_bytes(f"/api/v1/jobs/{job['id']}/result")
            await message.answer_document(result, filename="result.png")
            return
        if current["state"] in {"FAILED", "UNKNOWN"}:
            await message.answer("Не удалось обработать фотографию")
            return
        await asyncio.sleep(2)
```

Это схема, а не готовый запускаемый модуль: методы скачивания и отправки зависят
от выбранной Telegram-библиотеки.

## Выбор модели

Backend возвращает технический `id`, локализованный `label` и `description`.
Бот должен:

- хранить выбранный `id`, а не текст кнопки;
- строить кнопки только из собственной server-side allowlist;
- использовать актуальный `GET /api/v1/models` как серверную allowlist;
- не разрешать пользователю передавать произвольный `workflow` или `prompt`;
- не публиковать `custom` обычным пользователям.

Пример конфигурации будущего бота:

```env
TELEGRAM_BOT_TOKEN=<secret>
HROMI_BASE_URL=http://127.0.0.1:8091
HROMI_PUBLIC_API_KEY=<тот же секрет, что у backend>
HROMI_POLL_INTERVAL_SECONDS=2
```

Telegram token не должен находиться в `gpu-server/.env` и не должен передаваться
на GPU VM.

## Состояние бота

Минимально нужно хранить:

| Поле | Назначение |
|---|---|
| `telegram_user_id` | Владелец настроек и задания |
| `telegram_chat_id` | Куда отправлять статус и результат |
| `workflow_id` | Последний выбранный разрешённый workflow |
| `hromi_job_id` | Связь с очередью backend |
| `telegram_message_id` | Сообщение прогресса для обновления |
| `created_at` | TTL и диагностика |

Эту таблицу лучше хранить в отдельной базе бота. Не изменяйте напрямую
`backend/data/jobs.sqlite3`: Hromi владеет её схемой и worker-состояниями.

## Повторы и таймауты

- После успешного `POST /api/v1/jobs` всегда сохраняйте `job.id` до ответа
  пользователю.
- Потеря соединения при polling не означает ошибку генерации; продолжайте опрос.
- Не создавайте новую job только потому, что Telegram handler перезапустился.
- После рестарта бот должен восстановить незавершённые связи из своей базы.
- `UNKNOWN` требует ручной проверки; автоматический повтор может создать дубль.
- Не удерживайте один Telegram webhook/request до конца генерации: используйте
  фоновую задачу или отдельный worker.

## Ограничения и защита данных

- Принимайте только JPEG, PNG и WebP до 8 МБ после скачивания.
- Ограничивайте количество активных заданий на пользователя и общий rate.
- Проверяйте, что пользователь получает только собственный `job_id` и artifact.
- Не журналируйте base64 фотографий, Telegram token и приватные URL.
- Добавьте понятную политику хранения и команду удаления данных пользователя.
- Разрешайте только несексуальные сценарии редактирования изображений и
  обрабатывайте фотографии людей только с их согласием.

## Что уже реализовано в backend

- Bearer API key;
- обязательный `X-Hromi-Principal`;
- ownership job и результата;
- атомарный `Idempotency-Key` в SQLite;
- общий предел очереди и лимит активных jobs пользователя;
- rate limit запросов пользователя;
- `queue_position`;
- серверная allowlist: сейчас публичен только `passthrough`;
- сохранение очереди и ownership после перезапуска.

## Что ещё нужно для удалённого production-бота

1. TLS reverse proxy и firewall.
2. Ротация API key без простоя.
3. Multipart/потоковая загрузка вместо обязательного base64.
4. Готовый bot worker и его отдельная БД связей Telegram↔Hromi.
5. Метрики, аудит без содержимого фотографий и alerting.
6. TTL/удаление исходников, результатов и записей базы.

Внешний Telegram-сервер должен обращаться только к `/api/v1`, а не к открытому
ComfyUI или административным маршрутам backend.
