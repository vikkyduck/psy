"""Paced SQLite snapshots for psy-backup, on generated databases only."""
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest

spec = importlib.util.spec_from_file_location('sqlite_snapshot', Path(__file__).resolve().parents[1] / 'backup/sqlite_snapshot.py')
snap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snap)


def fill(path, mode, rows=400):
    with sqlite3.connect(path) as db:
        db.execute(f'PRAGMA journal_mode={mode}')
        db.execute('CREATE TABLE fixture(id INTEGER PRIMARY KEY, body TEXT)')
        db.executemany('INSERT INTO fixture(body) VALUES (?)', [('x' * 900,)] * rows)
    db.close()


class Writer(threading.Thread):
    """Another connection committing all the time, like the bot during a backup."""
    def __init__(self, path):
        super().__init__(daemon=True)
        self.path, self.stop, self.commits = path, threading.Event(), 0

    def run(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        while not self.stop.is_set():
            try:
                db.execute("INSERT INTO fixture(body) VALUES ('y')")
                self.commits += 1
            except sqlite3.OperationalError:
                pass
            time.sleep(0.005)
        db.close()


class Snapshot(unittest.TestCase):
    def run_with_writer(self, mode):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder, 'live.sqlite3'), Path(folder, 'copy', 'live.sqlite3')
            target.parent.mkdir()
            fill(source, mode)
            writer = Writer(source)
            writer.start()
            try:
                while writer.commits < 5:
                    time.sleep(0.01)
                result = snap.snapshot(source, target, pages=4, pause=0.002)
            finally:
                writer.stop.set()
                writer.join()
            self.assertEqual(result, 'ok')
            self.assertGreater(writer.commits, 5)
            self.assertEqual([p.name for p in target.parent.iterdir()], ['live.sqlite3'])  # no -wal/-shm
            with sqlite3.connect(target.as_uri() + '?mode=ro', uri=True) as copy:
                self.assertEqual(copy.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
                self.assertGreaterEqual(copy.execute('SELECT count(*) FROM fixture').fetchone()[0], 400)
            with sqlite3.connect(source) as live:
                self.assertEqual(live.execute('PRAGMA journal_mode').fetchone()[0], mode)

    def test_wal_database_copied_from_one_snapshot_while_written(self):
        self.run_with_writer('wal')

    def test_rollback_database_copied_while_written(self):
        self.run_with_writer('delete')

    def commit_on_every_pause(self, mode):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder, 'live.db'), Path(folder, 'copy.db')
            fill(source, mode)
            steps = []
            original = snap.time.sleep
            def sleep_and_write(seconds):
                steps.append(seconds)
                with sqlite3.connect(source) as other:
                    other.execute("INSERT INTO fixture(body) VALUES ('z')")
                other.close()
            snap.time.sleep = sleep_and_write
            try:
                self.assertEqual(snap.snapshot(source, target, pages=8, pause=0), 'ok')
            finally:
                snap.time.sleep = original
            with sqlite3.connect(target) as copy:
                return len(steps), copy.execute('PRAGMA page_count').fetchone()[0], \
                    copy.execute('SELECT count(*) FROM fixture').fetchone()[0]

    def test_wal_copy_never_restarts(self):
        pauses, pages, rows = self.commit_on_every_pause('wal')
        # One pause per step and no restart, although every pause saw a commit; the copy is the snapshot.
        self.assertLessEqual(pauses, pages // 8 + 1)
        self.assertEqual(rows, 400)

    def test_rollback_copy_restarting_on_every_commit_falls_back_to_one_step(self):
        pauses, pages, rows = self.commit_on_every_pause('delete')
        self.assertLessEqual(pauses, (snap.RESTARTS + 1) * (pages // 8 + 1))
        self.assertGreater(rows, 400)

    def test_notes_and_plain_copy_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            root, out = Path(folder, 'home'), Path(folder, 'out')
            (root / 'vault').mkdir(parents=True)
            out.mkdir()
            fill(root / 'vault/good.sqlite3', 'wal', rows=3)
            (root / 'broken.db').write_bytes(b'not a database at all, but keep the bytes')
            snap.main(['x', str(root), str(out), str(root / 'vault/good.sqlite3'), str(root / 'broken.db')])
            notes = (out / 'db-notes.txt').read_text().splitlines()
            self.assertTrue(notes[0].endswith('good.sqlite3: ok'))
            self.assertIn('plain copy', notes[1])
            self.assertEqual((out / 'broken.db').read_bytes(), (root / 'broken.db').read_bytes())


if __name__ == '__main__':
    unittest.main()
