"""Configuration loading for the standalone Telegram bot."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from urllib.parse import urlsplit


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        name, value = line.split('=', 1)
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name:
            os.environ.setdefault(name, value)


def _positive_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError as error:
        raise ValueError(f'{name} must be a positive number') from error
    if value <= 0:
        raise ValueError(f'{name} must be a positive number')
    return value


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as error:
        raise ValueError(f'{name} must be a positive integer') from error
    if value <= 0:
        raise ValueError(f'{name} must be a positive integer')
    return value


@dataclass(frozen=True)
class BotConfig:
    telegram_token: str
    hromi_base_url: str
    hromi_api_key: str
    database_path: Path
    poll_interval_seconds: float = 2.0
    telegram_poll_seconds: int = 30
    max_photo_bytes: int = 8 * 1024 * 1024
    monitor_workers: int = 20
    allowed_user_ids: frozenset[int] = frozenset()

    @classmethod
    def from_env(cls, root: Path) -> 'BotConfig':
        load_dotenv(root / '.env')
        token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
        api_key = os.environ.get('HROMI_PUBLIC_API_KEY', '').strip()
        base_url = os.environ.get('HROMI_BASE_URL', 'http://127.0.0.1:8091').rstrip('/')
        if not token:
            raise ValueError('TELEGRAM_BOT_TOKEN is required')
        if not api_key:
            raise ValueError('HROMI_PUBLIC_API_KEY is required')
        if len(api_key) < 32:
            raise ValueError('HROMI_PUBLIC_API_KEY must contain at least 32 characters')
        parsed = urlsplit(base_url)
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.path:
            raise ValueError('HROMI_BASE_URL must be an HTTP(S) origin without a path')
        allowed = os.environ.get('TELEGRAM_ALLOWED_USER_IDS', '').strip()
        try:
            allowed_ids = frozenset(
                int(value.strip()) for value in allowed.split(',') if value.strip()
            )
        except ValueError as error:
            raise ValueError('TELEGRAM_ALLOWED_USER_IDS must contain integer IDs') from error
        database = Path(os.environ.get(
            'TELEGRAM_BOT_DB', str(root / 'data' / 'telegram-bot.sqlite3')
        )).expanduser()
        return cls(
            telegram_token=token,
            hromi_base_url=base_url,
            hromi_api_key=api_key,
            database_path=database,
            poll_interval_seconds=_positive_float('HROMI_POLL_INTERVAL_SECONDS', 2),
            telegram_poll_seconds=_positive_int('TELEGRAM_POLL_SECONDS', 30),
            max_photo_bytes=_positive_int('TELEGRAM_MAX_PHOTO_BYTES', 8 * 1024 * 1024),
            monitor_workers=_positive_int('TELEGRAM_MONITOR_WORKERS', 20),
            allowed_user_ids=allowed_ids,
        )
