# Контракт Hromi backend

Бот обращается только к `/api/v1` заданного `HROMI_BASE_URL`.
Origin не содержит пути, query, userinfo или fragment. Удалённый origin требует HTTPS;
HTTP разрешён только на loopback. Перенаправления не выполняются.

## Авторизация

```http
Authorization: Bearer <backend-service-key>
X-Hromi-Principal: telegram:<telegram-user-id>
```

Сервисный ключ делегирует боту право утверждать principal. Backend должен ограничить
его namespace `telegram:*`, проверить ownership и применять собственные квоты.
Пользователь не может задать principal через сообщение или callback.

## Endpoints

| Метод | Путь | Ответ |
|---|---|---|
| GET | `/api/v1/models` | `{"models": [{"id": "...", "label": "...", "description": "..."}]}` |
| POST | `/api/v1/jobs` | Объект job с `id`, `state`, необязательным `queue_position` |
| GET | `/api/v1/jobs/<id>` | Состояние своей job |
| GET | `/api/v1/jobs/<id>/result` | JPEG, PNG или WebP; не больше 64 MiB |

`GET /api/v1/health` может использоваться оператором; бот не требует отдельного
startup health-запроса. ID в URL кодируются как один сегмент. Произвольный
`result_url` из ответа не используется для скачивания.

Backend владеет каталогом моделей. Бот исключает `availability=missing`, а
`unknown` допускает, чтобы backend мог запустить выключенную GPU VM.

## Создание job

```http
Idempotency-Key: telegram-update-<update-id>-model-<model-id>
Content-Type: application/json
```

```json
{
  "filename": "photo.jpg",
  "content_type": "image/jpeg",
  "image": "<base64>",
  "model": "<id из каталога>",
  "comparison_id": "telegram:<user-id>:<update-id>"
}
```

На каждую модель сравнения создаётся отдельная job, модели запускаются последовательно.
Backend должен сохранять idempotency: повтор с тем же владельцем, ключом и payload
возвращает прежнюю job; конфликт payload отклоняется. Бот не отправляет произвольные
ComfyUI prompts/workflows.

## Состояния и отказы

`QUEUED`, `PREPARING`, `SUBMITTING`, `RUNNING` — работа продолжается.
`SUCCEEDED` — скачивание результата; `FAILED`/`UNKNOWN` — завершение этапа с ошибкой.
Подтверждённая Telegram доставка сохраняется отдельным состоянием `DELIVERED`.

Сетевые ошибки, HTTP 429 и 5xx повторяются с ограниченным backoff. Постоянные
ошибки завершают этап или серию. Сообщения исключений и ответы сервисов не используются
как произвольный текст ошибки с секретными URL.

Бот ограничивает размеры и MIME. Декодирование изображения, проверка содержимого,
allowlist workflow и защита результатов остаются обязательными обязанностями backend.
