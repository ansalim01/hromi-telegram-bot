"""Configuration loading for the standalone Telegram bot."""

from __future__ import annotations

import ipaddress
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


def secure_url(value):
    if not isinstance(value, str) or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise ValueError('Invalid service URL')
    parsed = urlsplit(value)
    try:
        _ = parsed.port
    except ValueError:
        raise ValueError('Invalid service URL port') from None
    if (
        parsed.scheme not in {'http', 'https'}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError('Invalid service URL')
    if parsed.scheme == 'http':
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname == 'localhost'
        if not loopback:
            raise ValueError('Remote services require HTTPS; use loopback for an SSH tunnel')
    return parsed


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
    if not math.isfinite(value) or value <= 0:
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
    telegram_token: str = field(repr=False)
    hromi_base_url: str
    hromi_api_key: str = field(repr=False)
    database_path: Path
    poll_interval_seconds: float = 2.0
    telegram_poll_seconds: int = 30
    max_photo_bytes: int = 8 * 1024 * 1024
    monitor_workers: int = 20
    allowed_user_ids: frozenset[int] = frozenset()
    allow_any_user: bool = False
    allowed_group_ids: frozenset[int] = frozenset()
    max_active_photos: int = 20
    daily_photos_per_user: int = 10

    @classmethod
    def from_env(cls, root: Path) -> 'BotConfig':
        root = root.expanduser().resolve()
        load_dotenv(root / '.env')
        token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
        api_key = os.environ.get('HROMI_PUBLIC_API_KEY', '').strip()
        base_url = os.environ.get('HROMI_BASE_URL', 'http://127.0.0.1:8091').rstrip('/')
        if not token:
            raise ValueError('TELEGRAM_BOT_TOKEN is required')
        if not token.isascii() or any(ord(c) < 33 or ord(c) == 127 for c in token):
            raise ValueError('TELEGRAM_BOT_TOKEN must not contain whitespace or control characters')
        if not api_key:
            raise ValueError('HROMI_PUBLIC_API_KEY is required')
        if (
            len(api_key) < 32
            or not api_key.isascii()
            or any(ord(c) < 33 or ord(c) == 127 for c in api_key)
        ):
            raise ValueError('HROMI_PUBLIC_API_KEY requires at least 32 printable ASCII characters')
        parsed = secure_url(base_url)
        if (
            parsed.scheme not in {'http', 'https'}
            or not parsed.hostname
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError('HROMI_BASE_URL must be an HTTP(S) origin without a path')
        allowed = os.environ.get('TELEGRAM_ALLOWED_USER_IDS', '').strip()
        try:
            allowed_ids = frozenset(
                int(value.strip()) for value in allowed.split(',') if value.strip()
            )
        except ValueError as error:
            raise ValueError('TELEGRAM_ALLOWED_USER_IDS must contain integer IDs') from error
        open_access = os.environ.get('TELEGRAM_ALLOW_ANY_USER', '') == '1'
        if not allowed_ids and not open_access:
            raise ValueError(
                'Configure TELEGRAM_ALLOWED_USER_IDS or explicitly set TELEGRAM_ALLOW_ANY_USER=1'
            )
        groups = frozenset(
            int(value.strip())
            for value in os.environ.get('TELEGRAM_ALLOWED_GROUP_IDS', '').split(',')
            if value.strip()
        )
        database = Path(
            os.environ.get('TELEGRAM_BOT_DB', str(root / 'data' / 'telegram-bot.sqlite3'))
        ).expanduser()
        if not database.is_absolute():
            database = root / database
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
            allow_any_user=open_access,
            allowed_group_ids=groups,
            max_active_photos=_positive_int('TELEGRAM_MAX_ACTIVE_PHOTOS', 20),
            daily_photos_per_user=_positive_int('TELEGRAM_DAILY_PHOTOS_PER_USER', 10),
        )
