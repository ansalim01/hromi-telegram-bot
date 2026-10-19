# Hromi Telegram Bot

[![Checks](https://github.com/ansalim01/hromi-telegram-bot/actions/workflows/checks.yml/badge.svg)](https://github.com/ansalim01/hromi-telegram-bot/actions/workflows/checks.yml)
[![Security](https://github.com/ansalim01/hromi-telegram-bot/actions/workflows/security.yml/badge.svg)](https://github.com/ansalim01/hromi-telegram-bot/actions/workflows/security.yml)

Telegram-бот для обработки фотографий через **Hromi HTTP API**. Проверяет
готовность FLUX и Qwen, запускает их последовательно для одной фотографии и возвращает
результаты в чат с названием каждого workflow.

Отдельное Python-приложение с постоянным состоянием в SQLite. Использует Telegram
Bot API напрямую через стандартную библиотеку Python: сторонних runtime-зависимостей нет.
Для генерации требуется работающий Hromi backend; модели и GPU обслуживает backend.

## Возможности

- JPEG, PNG и WebP как фотография или документ, по умолчанию до 8 MiB.
- Сравнение FLUX и Qwen: один результат — одно сообщение с названием модели.
- Сравнение запускается только при `availability=ready` у обеих моделей.
- Одно активное сравнение на пользователя, общая очередь и суточные лимиты.
- Сохранение Telegram offset, текущего задания, позиции сравнения и доставки в SQLite.
- Восстановление незавершённых сравнений после перезапуска.
- Идемпотентные запросы к backend, ограниченные повторы временных ошибок.
- Закрытый доступ по allowlist; публичный режим включается явно.
- HTTPS для удалённого backend, запрет редиректов и ограничение скачиваемых ответов.

## Как это работает

```text
Пользователь → Telegram Bot API → этот бот → Hromi /api/v1
                                                │
                                                ▼
                                         очередь / GPU
                                                │
Пользователь ← изображение с названием модели ← результат
```

Бот не получает облачные credentials и не обращается к ComfyUI или административным
маршрутам backend. GPU запускается и останавливается по правилам backend.

## Быстрый запуск

Нужны **Python 3.11+**, созданный через [@BotFather](https://t.me/BotFather) бот
и доступный Hromi backend с включённым публичным API.

```bash
git clone https://github.com/ansalim01/hromi-telegram-bot.git
cd hromi-telegram-bot
python3 -m venv .venv
.venv/bin/python -m pip install .
cp .env.example .env
chmod 600 .env
```

Заполните `.env` своими значениями:

```dotenv
TELEGRAM_BOT_TOKEN=replace-with-token-from-botfather
HROMI_BASE_URL=http://127.0.0.1:8091
HROMI_PUBLIC_API_KEY=replace-with-your-backend-service-key
TELEGRAM_ALLOWED_USER_IDS=123456789
```

`123456789` — пример, замените его своим Telegram user ID. Сервисный ключ backend
должен содержать минимум 32 печатных ASCII-символа и разрешать namespace `telegram:*`.
Он отличается от Telegram token и административного токена backend.

```bash
.venv/bin/hromi-telegram-bot --config-dir "$PWD"
```

Для Windows используйте соответствующие команды из `.venv\Scripts`.
Команда `--help` не требует credentials. `--config-dir` выбирает каталог `.env`;
относительный путь базы разрешается от этого же каталога, независимо от рабочей папки.
Настройки окружения имеют приоритет над `.env`.

Бот использует **long polling**. Перед запуском отключите ранее установленный
webhook через Telegram Bot API. Запускайте один экземпляр на bot token и базу.

## Использование

1. Отправьте `/start` или `/help`: бот покажет описание и доступные модели.
2. `/model` повторно показывает список моделей для сравнения.
3. Пришлите фотографию или image-документ.
4. Дождитесь серии результатов. Каждая модель запускается по очереди;
   продолжительность зависит от backend, очереди и оборудования.
5. После завершения серии можно отправить следующую фотографию.

Сравнение использует `sd15-inpaint`, `flux-fill` и `qwen-image-2.1`, в этом порядке.
Все три модели должны иметь подтверждённый статус `ready` в каталоге backend.
Если хотя бы одна не готова, бот сообщает об этом и не создаёт задания.
Технический `passthrough` и другие workflow в сравнение не попадают.

## Конфигурация

| Переменная | Значение по умолчанию / назначение |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Обязательный token от BotFather |
| `HROMI_BASE_URL` | `http://127.0.0.1:8091`; удалённый origin требует HTTPS |
| `HROMI_PUBLIC_API_KEY` | Обязательный сервисный ключ backend, минимум 32 символа |
| `TELEGRAM_ALLOWED_USER_IDS` | Разрешённые user ID через запятую; пустое значение закрывает доступ |
| `TELEGRAM_ALLOW_ANY_USER` | `0`; значение `1` явно разрешает открытый доступ |
| `TELEGRAM_ALLOWED_GROUP_IDS` | Пусто; по умолчанию только личные чаты |
| `TELEGRAM_BOT_DB` | `./data/telegram-bot.sqlite3` относительно `--config-dir` |
| `TELEGRAM_MAX_PHOTO_BYTES` | `8388608` — 8 MiB |
| `TELEGRAM_MAX_ACTIVE_PHOTOS` | `20` активных сравнений |
| `TELEGRAM_DAILY_PHOTOS_PER_USER` | `10` допусков за скользящие 24 часа |
| `TELEGRAM_MONITOR_WORKERS` | `20` фоновых наблюдателей |
| `HROMI_POLL_INTERVAL_SECONDS` | `2` секунды между проверками job |
| `TELEGRAM_POLL_SECONDS` | `30` секунд ожидания Telegram updates |

Allowlist пользователей действует и в разрешённых группах. Группа не становится
приватным каналом: участники увидят отправленные в неё результаты.
Допуск и бюджет проверяются до скачивания; отклонённая попытка может расходовать
дневной допуск. В SQLite также действует общий предел допусков и сохранённых активных payload.

## Хранение и восстановление

База содержит идентификаторы пользователей/чатов, offset, состояния заданий и фото
активных сравнений. После завершения сравнения payload фотографии очищается;
метаданные остаются. Настройте сроки хранения и защищённые резервные копии.
Подробнее о SQLite/WAL, данных в Telegram/backend и границах удаления — в [SECURITY.md](SECURITY.md).

Для переноса сохраните базу и задайте её прежний абсолютный путь в `TELEGRAM_BOT_DB`.
Для работающей SQLite используйте SQLite backup или остановите писателей и учтите WAL.
Обычное копирование одного открытого `.sqlite3` не гарантирует согласованный backup.

Идемпотентность backend защищает от повторного создания генерации. Доставка Telegram
и запись её подтверждения не являются общей транзакцией: сбой между ними может привести
к повторной доставке. Exactly-once доставка не обещается.

## Разработка и проверки

```bash
make setup
make check test
make doctor
```

`make doctor` проверяет конфигурацию без обращения к Telegram/backend.
Тесты используют поддельные клиенты и не отправляют фотографии или сообщения.
CI проверяет Python 3.11/3.14, сборку пакета, известные уязвимости зависимостей
и Git-историю на секреты. Пример сборки:

```bash
.venv/bin/python -m pip install build
.venv/bin/python -m build
```

## Структура

```text
src/hromi_telegram_bot/
  bot.py      — обработчики, сравнения и восстановление
  http.py     — ограниченные HTTP-клиенты Telegram/Hromi
  store.py    — SQLite-транзакции и постоянное состояние
  config.py   — конфигурация и проверки доступа
  cli.py      — исполняемая команда
scripts/      — запуск из checkout и offline doctor
tests/        — unit/security/regression проверки
docs/         — HTTP-контракт backend
```

[Контракт интеграции](docs/README.md) · [Безопасность и сообщение об уязвимости](SECURITY.md)
