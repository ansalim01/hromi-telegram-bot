"""Small durable state store owned by the Telegram bot."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import time


TERMINAL_STATES = {'DELIVERED', 'FAILED', 'UNKNOWN'}


class BotStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (
                    user_id INTEGER PRIMARY KEY,
                    model_id TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    update_id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    hromi_job_id TEXT NOT NULL UNIQUE,
                    progress_message_id INTEGER,
                    state TEXT NOT NULL,
                    last_status_text TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            ''')

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def offset(self):
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT value FROM metadata WHERE key = 'telegram_offset'"
            ).fetchone()
            return int(row['value']) if row else 0

    def set_offset(self, offset: int):
        with self._connect() as db:
            db.execute('''
                INSERT INTO metadata(key, value) VALUES('telegram_offset', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
            ''', (str(offset),))

    def selected_model(self, user_id: int):
        with closing(self._connect()) as db:
            row = db.execute(
                'SELECT model_id FROM settings WHERE user_id = ?', (user_id,)
            ).fetchone()
            return row['model_id'] if row else None

    def set_model(self, user_id: int, model_id: str):
        with self._connect() as db:
            db.execute('''
                INSERT INTO settings(user_id, model_id, updated_at) VALUES(?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    model_id = excluded.model_id, updated_at = excluded.updated_at
            ''', (user_id, model_id, time.time()))

    def job_for_update(self, update_id: int):
        with closing(self._connect()) as db:
            row = db.execute(
                'SELECT * FROM jobs WHERE update_id = ?', (update_id,)
            ).fetchone()
            return dict(row) if row else None

    def add_job(self, update_id: int, user_id: int, chat_id: int,
                hromi_job_id: str, progress_message_id: int | None):
        now = time.time()
        with self._connect() as db:
            db.execute('''
                INSERT INTO jobs(
                    update_id, user_id, chat_id, hromi_job_id,
                    progress_message_id, state, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, 'QUEUED', ?, ?)
                ON CONFLICT(update_id) DO NOTHING
            ''', (update_id, user_id, chat_id, hromi_job_id,
                  progress_message_id, now, now))
        return self.job_for_update(update_id)

    def update_job(self, job_id: str, state: str, status_text: str | None = None):
        with self._connect() as db:
            db.execute('''
                UPDATE jobs SET state = ?, last_status_text = COALESCE(?, last_status_text),
                    updated_at = ? WHERE hromi_job_id = ?
            ''', (state, status_text, time.time(), job_id))

    def set_progress_message(self, job_id: str, message_id: int | None):
        with self._connect() as db:
            db.execute('UPDATE jobs SET progress_message_id = ? WHERE hromi_job_id = ?',
                       (message_id, job_id))

    def has_active_job(self, user_id: int):
        placeholders = ','.join('?' for _ in TERMINAL_STATES)
        with closing(self._connect()) as db:
            return db.execute(
                f'SELECT 1 FROM jobs WHERE user_id = ? AND state NOT IN ({placeholders}) LIMIT 1',
                (user_id, *TERMINAL_STATES),
            ).fetchone() is not None

    def active_jobs(self):
        placeholders = ','.join('?' for _ in TERMINAL_STATES)
        with closing(self._connect()) as db:
            rows = db.execute(
                f'SELECT * FROM jobs WHERE state NOT IN ({placeholders}) ORDER BY created_at',
                tuple(TERMINAL_STATES),
            ).fetchall()
            return [dict(row) for row in rows]
