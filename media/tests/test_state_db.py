"""Schema versioning, write-ahead logging and per-thread connection reuse of the upload state."""
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import psy_upload_common as C


LEGACY = """
CREATE TABLE tickets(id TEXT PRIMARY KEY, uid INTEGER NOT NULL, chat INTEGER NOT NULL, thread INTEGER,
  reply INTEGER, expires INTEGER NOT NULL);
CREATE TABLE sessions(hash TEXT PRIMARY KEY, uid INTEGER NOT NULL, chat INTEGER NOT NULL, thread INTEGER,
  reply INTEGER, expires INTEGER NOT NULL);
CREATE TABLE jobs(id TEXT PRIMARY KEY, uid INTEGER NOT NULL, chat INTEGER NOT NULL,
  thread INTEGER, reply INTEGER, filename TEXT NOT NULL, ext TEXT NOT NULL, size INTEGER NOT NULL,
  offset INTEGER NOT NULL DEFAULT 0, fingerprint TEXT NOT NULL, language TEXT NOT NULL,
  state TEXT NOT NULL, created INTEGER NOT NULL, updated INTEGER NOT NULL,
  error TEXT, sha256 TEXT, duration REAL, delivery TEXT, message_id INTEGER, content_id TEXT);
INSERT INTO jobs(id,uid,chat,filename,ext,size,fingerprint,language,state,created,updated)
  VALUES('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',7,7,'old.wav','.wav',3,'f','ru','done',1,1);
"""


class StateDatabase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        scope = patch.multiple(C, ROOT=self.root/'state', FILES=self.root/'files')
        scope.start()
        self.addCleanup(scope.stop)
        self.path = self.root/'state'/'state.sqlite3'

    def other(self):
        conn = sqlite3.connect(self.path, timeout=0.2)
        self.addCleanup(conn.close)
        return conn

    def test_unversioned_database_migrates_once_preserving_rows(self):
        self.path.parent.mkdir()
        with sqlite3.connect(self.path) as legacy:
            legacy.executescript(LEGACY)
        C.initialize()
        conn = self.other()
        self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], C.SCHEMA_VERSION)
        self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
        columns = {row[1] for row in conn.execute('PRAGMA table_info(jobs)')}
        self.assertTrue({'content_id', 'proof', 'started_at', 'retry_generation', 'text_size'} <= columns)
        indexes = {row[1] for row in conn.execute('PRAGMA index_list(jobs)')}
        self.assertTrue({'job_ready', 'job_turn', 'active_content', 'reprocess_parent'} <= indexes)
        self.assertEqual(conn.execute('SELECT filename,attempts FROM jobs').fetchall(), [('old.wav', 0)])
        plan = ' '.join(row[3] for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM jobs WHERE state='queued' AND next_try<=1"))
        self.assertIn('job_ready', plan)

    def test_current_schema_never_waits_for_the_write_lock(self):
        C.initialize()
        writer = self.other()
        writer.execute('BEGIN IMMEDIATE')
        self.addCleanup(writer.rollback)
        # A process starting while another one writes must not run DDL or block.
        errors = []
        def start():
            try:
                C.initialize()
                with C.db() as conn:
                    conn.execute('SELECT count(*) FROM jobs').fetchone()
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=start)
        thread.start()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_connection_reused_per_thread_and_nested_use_shares_one_transaction(self):
        C.initialize()
        with C.db() as first:
            pass
        with C.db() as second:
            self.assertIs(first, second)
            self.assertEqual(second.execute('PRAGMA synchronous').fetchone()[0], 1)
        seen = []
        thread = threading.Thread(target=lambda: seen.append(C.db().__enter__()))
        thread.start(); thread.join()
        self.assertIsNot(seen[0], first)
        ident = 'b' * 32
        with C.db() as outer:
            outer.execute("""INSERT INTO jobs(id,uid,chat,filename,ext,size,fingerprint,language,state,created,updated)
                             VALUES(?,1,1,'n.wav','.wav',1,'f','ru','queued',0,0)""", (ident,))
            with C.db() as inner:
                self.assertTrue(C.transition(inner, ident, 'queued', 'processing', started_at=5))
            self.assertEqual(self.other().execute('SELECT count(*) FROM jobs').fetchone()[0], 0)
        self.assertEqual(self.other().execute('SELECT state,started_at FROM jobs').fetchone(), ('processing', 5))
        with C.db() as conn:
            self.assertFalse(C.transition(conn, ident, 'queued', 'failed'))

    def test_failed_block_rolls_back_and_connection_stays_usable(self):
        C.initialize()
        with self.assertRaises(RuntimeError):
            with C.db() as conn:
                conn.execute('BEGIN IMMEDIATE')
                conn.execute("""INSERT INTO jobs(id,uid,chat,filename,ext,size,fingerprint,language,state,created,updated)
                                VALUES(?,1,1,'n.wav','.wav',1,'f','ru','queued',0,0)""", ('c' * 32,))
                raise RuntimeError('abort')
        with C.db() as conn:
            self.assertFalse(conn.in_transaction)
            self.assertEqual(conn.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
