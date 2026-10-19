"""Small durable state store owned by the Telegram bot."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path

TERMINAL_STATES = {'DELIVERED', 'FAILED', 'UNKNOWN'}


class BotStore:
    def __init__(self, path: Path):
        os.umask(0o077)
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError('Bot storage cannot be a symlink')
        descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        os.close(descriptor)
        path.chmod(0o600)
        with self._transaction() as db:
            db.executescript("""
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
                CREATE TABLE IF NOT EXISTS admissions (
                    update_id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS comparisons (
                    update_id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    payload TEXT,
                    models TEXT NOT NULL,
                    next_index INTEGER NOT NULL DEFAULT 0,
                    current_job_id TEXT,
                    finished INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS comparison_steps (
                    update_id INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    hromi_job_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL DEFAULT 'QUEUED',
                    progress_message_id INTEGER,
                    last_status_text TEXT,
                    delivered_message_id INTEGER,
                    PRIMARY KEY(update_id, position)
                );
            """)

    def admit_photo(self, update_id, user_id, max_active, daily_limit):
        with self._transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM admissions WHERE created_at < ?', (time.time() - 86400,))
            if db.execute(
                'SELECT 1 FROM admissions WHERE update_id=? AND user_id=?', (update_id, user_id)
            ).fetchone():
                return True
            active, stored = db.execute(
                'SELECT COUNT(*), COALESCE(SUM(length(payload)), 0) FROM comparisons WHERE finished=0'
            ).fetchone()
            count = db.execute(
                'SELECT COUNT(*) FROM admissions WHERE user_id=?', (user_id,)
            ).fetchone()[0]
            total = db.execute('SELECT COUNT(*) FROM admissions').fetchone()[0]
            if (
                active >= max_active
                or stored >= 128 * 1024 * 1024
                or count >= daily_limit
                or total >= 1000
            ):
                return False
            db.execute('INSERT INTO admissions VALUES (?, ?, ?)', (update_id, user_id, time.time()))
            return True

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def _transaction(self):
        with closing(self._connect()) as db, db:
            yield db

    def offset(self):
        with closing(self._connect()) as db:
            row = db.execute("SELECT value FROM metadata WHERE key = 'telegram_offset'").fetchone()
            return int(row['value']) if row else 0

    def set_offset(self, offset: int):
        with self._transaction() as db:
            db.execute(
                """
                INSERT INTO metadata(key, value) VALUES('telegram_offset', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
                (str(offset),),
            )

    def selected_model(self, user_id: int):
        with closing(self._connect()) as db:
            row = db.execute(
                'SELECT model_id FROM settings WHERE user_id = ?', (user_id,)
            ).fetchone()
            return row['model_id'] if row else None

    def set_model(self, user_id: int, model_id: str):
        with self._transaction() as db:
            db.execute(
                """
                INSERT INTO settings(user_id, model_id, updated_at) VALUES(?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    model_id = excluded.model_id, updated_at = excluded.updated_at
            """,
                (user_id, model_id, time.time()),
            )

    def job_for_update(self, update_id: int):
        with closing(self._connect()) as db:
            row = db.execute('SELECT * FROM jobs WHERE update_id = ?', (update_id,)).fetchone()
            return dict(row) if row else None

    def add_job(
        self,
        update_id: int,
        user_id: int,
        chat_id: int,
        hromi_job_id: str,
        progress_message_id: int | None,
    ):
        now = time.time()
        with self._transaction() as db:
            db.execute(
                """
                INSERT INTO jobs(
                    update_id, user_id, chat_id, hromi_job_id,
                    progress_message_id, state, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, 'QUEUED', ?, ?)
                ON CONFLICT(update_id) DO NOTHING
            """,
                (update_id, user_id, chat_id, hromi_job_id, progress_message_id, now, now),
            )
        return self.job_for_update(update_id)

    def update_job(self, job_id: str, state: str, status_text: str | None = None):
        with self._transaction() as db:
            db.execute(
                """
                UPDATE jobs SET state = ?, last_status_text = COALESCE(?, last_status_text),
                    updated_at = ? WHERE hromi_job_id = ?
            """,
                (state, status_text, time.time(), job_id),
            )
            db.execute(
                """UPDATE comparison_steps SET state = ?,
                          last_status_text = COALESCE(?, last_status_text)
                          WHERE hromi_job_id = ?""",
                (state, status_text, job_id),
            )

    def set_progress_message(self, job_id: str, message_id: int | None):
        with self._transaction() as db:
            db.execute(
                'UPDATE jobs SET progress_message_id = ? WHERE hromi_job_id = ?',
                (message_id, job_id),
            )
            db.execute(
                'UPDATE comparison_steps SET progress_message_id = ? WHERE hromi_job_id = ?',
                (message_id, job_id),
            )

    def has_active_job(self, user_id: int):
        # Only internal '?' placeholders are interpolated; all values remain bound.
        placeholders = ','.join('?' for _ in TERMINAL_STATES)
        with closing(self._connect()) as db:
            if db.execute(
                'SELECT 1 FROM comparisons WHERE user_id = ? AND finished = 0', (user_id,)
            ).fetchone():
                return True
            return (
                db.execute(
                    f'SELECT 1 FROM jobs WHERE user_id = ? AND state NOT IN ({placeholders}) LIMIT 1',  # nosec B608
                    (user_id, *TERMINAL_STATES),
                ).fetchone()
                is not None
            )

    def active_jobs(self):
        # Only internal '?' placeholders are interpolated; all values remain bound.
        placeholders = ','.join('?' for _ in TERMINAL_STATES)
        with closing(self._connect()) as db:
            rows = db.execute(
                f'SELECT * FROM jobs WHERE state NOT IN ({placeholders}) ORDER BY created_at',  # nosec B608
                tuple(TERMINAL_STATES),
            ).fetchall()
            return [dict(row) for row in rows]

    def comparison(self, update_id):
        with closing(self._connect()) as db:
            row = db.execute(
                'SELECT * FROM comparisons WHERE update_id = ?', (update_id,)
            ).fetchone()
            if row is None:
                return None
            value = dict(row)
            value['models'] = json.loads(value['models'])
            value['payload'] = json.loads(value['payload']) if value['payload'] else None
            return value

    def add_comparison(self, update_id, user_id, chat_id, payload, models):
        with self._transaction() as db:
            db.execute(
                """INSERT INTO comparisons(update_id, user_id, chat_id, payload, models)
                          VALUES(?, ?, ?, ?, ?) ON CONFLICT(update_id) DO NOTHING""",
                (update_id, user_id, chat_id, json.dumps(payload), json.dumps(models)),
            )
        return self.comparison(update_id)

    def active_comparisons(self):
        with closing(self._connect()) as db:
            ids = [
                row[0]
                for row in db.execute(
                    'SELECT update_id FROM comparisons WHERE finished = 0 ORDER BY update_id'
                )
            ]
        return [self.comparison(update_id) for update_id in ids]

    def set_comparison_job(self, update_id, job_id):
        with self._transaction() as db:
            db.execute(
                """INSERT INTO comparison_steps(update_id, position, hromi_job_id)
                          SELECT update_id, next_index, ? FROM comparisons WHERE update_id = ?
                          ON CONFLICT(update_id, position) DO NOTHING""",
                (job_id, update_id),
            )
            db.execute(
                'UPDATE comparisons SET current_job_id = ? WHERE update_id = ?', (job_id, update_id)
            )

    def comparison_step(self, update_id, position):
        with closing(self._connect()) as db:
            row = db.execute(
                'SELECT * FROM comparison_steps WHERE update_id = ? AND position = ?',
                (update_id, position),
            ).fetchone()
            return dict(row) if row else None

    def mark_delivered(self, job_id, message_id=None):
        with self._transaction() as db:
            db.execute(
                "UPDATE jobs SET state = 'DELIVERED', updated_at = ? WHERE hromi_job_id = ?",
                (time.time(), job_id),
            )
            db.execute(
                """UPDATE comparison_steps SET state = 'DELIVERED',
                          delivered_message_id = ? WHERE hromi_job_id = ?""",
                (message_id, job_id),
            )

    def comparison_counts(self, update_id):
        with closing(self._connect()) as db:
            return dict(
                db.execute(
                    'SELECT state, COUNT(*) FROM comparison_steps WHERE update_id = ? GROUP BY state',
                    (update_id,),
                ).fetchall()
            )

    def advance_comparison(self, update_id):
        with self._transaction() as db:
            db.execute(
                """UPDATE comparisons SET next_index = next_index + 1,
                          current_job_id = NULL WHERE update_id = ?""",
                (update_id,),
            )

    def finish_comparison(self, update_id):
        with self._transaction() as db:
            db.execute(
                'UPDATE comparisons SET finished = 1, payload = NULL WHERE update_id = ?',
                (update_id,),
            )
