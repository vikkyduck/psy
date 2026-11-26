import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from migrate_group_history import migrate


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name) / "group.sqlite3"
        self.target = Path(self.tmp.name) / "state.db"
        with sqlite3.connect(self.source) as db:
            db.execute("CREATE TABLE turns(id INTEGER,uid INTEGER,topic INTEGER,prompt TEXT,answer TEXT)")
            db.execute("INSERT INTO turns VALUES(1,42,0,?,?)", ("Точный запрос\n" * 9000, "Полный ответ\n"))
        with sqlite3.connect(self.target) as db:
            db.executescript("""
                CREATE TABLE state_meta(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE sessions(id TEXT PRIMARY KEY,session_key TEXT,ended_at REAL,message_count INTEGER,
                                      last_activity_at REAL,last_activity_provenance TEXT);
                CREATE TABLE gateway_routing(scope TEXT,session_key TEXT,entry_json TEXT,updated_at REAL);
                CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT REFERENCES sessions(id),role TEXT,
                                      content TEXT,timestamp REAL,message_uid TEXT);
                INSERT INTO sessions VALUES('old','agent:main:telegram:group:-202:42',NULL,1,NULL,NULL);
                INSERT INTO messages VALUES(1,'old','user','Existing original',1,NULL);
            """)
            db.execute("INSERT INTO gateway_routing VALUES('test',?,?,1)",
                       ('agent:main:telegram:group:-202:42', json.dumps({'session_id': 'old'})))

    def test_verbatim_and_idempotent_preserves_old_rows(self):
        first = migrate(self.source, self.target, -202, {42})
        second = migrate(self.source, self.target, -202, {42})
        self.assertEqual(first['inserted_now'], 2)
        self.assertEqual(second['inserted_now'], 0)
        with sqlite3.connect(self.target) as db:
            rows = db.execute('SELECT role,content FROM messages ORDER BY id').fetchall()
            self.assertEqual(rows, [('user', 'Existing original'), ('user', 'Точный запрос\n' * 9000), ('assistant', 'Полный ответ\n')])
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_unapproved_sender_has_no_writes(self):
        before = self.target.read_bytes()
        with self.assertRaises(ValueError):
            migrate(self.source, self.target, -202, {77})
        self.assertEqual(self.target.read_bytes(), before)

    def test_routing_conflict_rolls_back(self):
        with sqlite3.connect(self.target) as db:
            db.execute('UPDATE gateway_routing SET entry_json=?', (json.dumps({'session_id': 'different'}),))
        with self.assertRaises(ValueError):
            migrate(self.source, self.target, -202, {42})
        with sqlite3.connect(self.target) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM state_meta').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
