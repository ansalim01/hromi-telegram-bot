"""Transactions must commit, roll back and close without relying on GC."""

import gc
import sqlite3
import sys
import warnings
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hromi_telegram_bot.store import BotStore


class StoreTests(TestCase):
    def test_writes_are_committed_and_connections_closed(self):
        with TemporaryDirectory() as directory, warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter('always', ResourceWarning)
            store = BotStore(Path(directory) / 'bot.db')
            for offset in range(10):
                store.set_offset(offset)
            self.assertEqual(BotStore(store.path).offset(), 9)
            gc.collect()
            self.assertEqual([w for w in recorded if issubclass(w.category, ResourceWarning)], [])

    def test_failed_transaction_rolls_back_and_closes(self):
        with TemporaryDirectory() as directory:
            store = BotStore(Path(directory) / 'bot.db')
            store.set_offset(7)
            with self.assertRaises(RuntimeError), store._transaction() as db:
                db.execute("UPDATE metadata SET value='8' WHERE key='telegram_offset'")
                raise RuntimeError('abort')
            self.assertEqual(store.offset(), 7)
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute('SELECT 1')
