# hromi-telegram-bot

Telegram-клиент Hromi API. Общение с backend происходит по HTTP(S); его исходники и GPU-файлы не требуются.

## Установка и запуск

```bash
python3 -m venv .venv
.venv/bin/python -m pip install . -r requirements-dev.txt
cp .env.example .env
# Заполните свои значения конфигурации.
.venv/bin/hromi-telegram-bot --config-dir "$PWD"
```

Python 3.11+. `.env`, `data/`, credentials, фото и результаты не входят в Git.
При переходе с монорепозитория сохраните прежние абсолютные пути данных.
Не запускайте два экземпляра сервиса с одной очередью/Telegram polling одновременно.

## Проверки

```bash
make check test
make doctor
```

Doctor читает только собственную конфигурацию, не запускает VM и не отправляет сообщений.
Вызов установленной команды с `--help` не требует credentials.
История компонента сохранена; происхождение описано в [MIGRATION.md](MIGRATION.md).

## Backend и данные

Задайте `HROMI_BASE_URL`, сервисный API key, Telegram token и allowlist в `.env`.
Удалённый backend требует HTTPS. Список моделей получается из `/api/v1/models`;
бот не содержит своей копии workflows или модели GPU.

При переходе задайте `TELEGRAM_BOT_DB` на прежний абсолютный путь SQLite.
Вместе с этой БД сохраняются offset, сравнения и состояние доставки.
API-контракт: [docs/README.md](docs/README.md).
