import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from psy_modules import psy_group_store as store


class CheckpointStoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def request(self, uid, ident):
        body = f"synthetic request {ident}"
        self.assertEqual(store.pending(uid, ident, body, root=self.root), "queued")
        self.assertEqual(store.claim_next(root=self.root), (ident, body))
        return store.Checkpoints(ident, root=self.root)

    def test_parallel_chapters_survive_restart_and_are_scoped_to_the_update(self):
        job = self.request(5, 1)
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda i: job.put(f"chapter.{i}", {"index": i}), range(3)))
        second = self.request(6, 2)
        self.assertIsNone(second.get("chapter.1"))
        second.put("chapter.1", {"index": 99})
        store.recover(root=self.root)
        restored = store.Checkpoints(1, root=self.root)
        for i in range(3):
            self.assertEqual(restored.get(f"chapter.{i}"), {"index": i})
        self.assertEqual(second.get("chapter.1"), {"index": 99})
        self.assertEqual((self.root / "group.sqlite3").stat().st_mode & 0o777, 0o600)

    def test_retry_deadline_and_confirmed_parts_survive_two_crashes(self):
        job = self.request(5, 1)
        job.put("result", "complete lecture")
        self.assertEqual(store.begin_delivery(1, 0, "a", root=self.root), ("send", None))
        store.confirm_delivery(1, 0, 101, root=self.root)
        self.assertEqual(store.begin_delivery(1, 1, "b", root=self.root), ("send", None))
        store.retry_delivery(1, 1, 100, root=self.root)

        store.recover(root=self.root)
        self.assertIsNone(store.claim_next(root=self.root, now=80))
        self.assertEqual(store.next_retry_delay(root=self.root, now=80), 20)
        self.assertEqual(store.claim_next(root=self.root, now=100)[0], 1)
        self.assertEqual(store.begin_delivery(1, 0, "a", root=self.root), ("confirmed", 101))
        self.assertEqual(store.begin_delivery(1, 1, "b", root=self.root, now=90), ("wait", 100))
        self.assertEqual(store.begin_delivery(1, 1, "b", root=self.root, now=100), ("send", None))
        store.confirm_delivery(1, 1, 102, root=self.root)

        store.recover(root=self.root)
        self.assertEqual(store.claim_next(root=self.root)[0], 1)
        self.assertEqual(store.begin_delivery(1, 1, "b", root=self.root), ("confirmed", 102))
        store.settle(1, True, root=self.root)
        self.assertEqual(store.pending(5, 1, "redelivered update", root=self.root), "existing")
        self.assertIsNone(store.claim_next(root=self.root))
        self.assertIsNone(job.get("result"))
        with store.database(self.root) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM delivery_parts").fetchone()[0], 0)
        with self.assertRaises(sqlite3.IntegrityError):
            job.put("late-worker", "cannot orphan the checkpoint")

    def test_ambiguous_sends_and_changed_payloads_cannot_be_replayed(self):
        job = self.request(7, 1)
        job.put("draft", "private")
        self.assertEqual(store.begin_delivery(1, 0, "a", root=self.root), ("send", None))
        self.assertEqual(store.begin_delivery(1, 0, "a", root=self.root), ("uncertain", None))
        store.confirm_delivery(1, 0, 101, root=self.root)
        self.assertEqual(store.begin_delivery(1, 0, "changed", root=self.root), ("uncertain", None))
        self.assertEqual(store.begin_delivery(1, 1, "b", root=self.root), ("send", None))
        store.recover(root=self.root)
        self.assertEqual(store.state(1, root=self.root), "uncertain")
        self.assertIsNone(store.claim_next(root=self.root))
        self.assertEqual(job.get("draft"), "private")

    def test_replayed_archive_and_turn_do_not_duplicate_or_cross_members(self):
        self.request(7, 1)
        def save():
            return (store.remember(7, 8, "q", "a", root=self.root, request_key=(1, "answer")),
                    store.archive(7, 8, "name", "text", b"abc", root=self.root, request_key=(1, "text")))
        with ThreadPoolExecutor(max_workers=3) as pool:
            records = list(pool.map(lambda _: save(), range(3)))
        self.assertEqual(len(set(records)), 1)
        with store.database(self.root) as con:
            for table in ("turns", "documents"):
                self.assertEqual(con.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 1)
        with self.assertRaises(ValueError):
            store.remember(99, 8, "q", "a", request_key=(1, "answer"), root=self.root)
        with self.assertRaises(ValueError):
            store.archive(7, 10, "name", "text", b"x", request_key=(1, "text"), root=self.root)

    def test_existing_database_gains_checkpoint_tables_without_replaying_old_sends(self):
        with sqlite3.connect(self.root / "group.sqlite3") as con:
            con.execute("CREATE TABLE requests(id INTEGER PRIMARY KEY,uid INTEGER,body TEXT,state TEXT)")
            con.executemany("INSERT INTO requests VALUES(?,?,?,?)",
                            [(8, 5, "old A", "running"), (9, 6, "old B", "sending")])
        store.recover(root=self.root)
        self.assertEqual(store.claim_next(root=self.root), (8, "old A"))
        self.assertEqual(store.state(9, root=self.root), "uncertain")
        store.Checkpoints(8, root=self.root).put("resume", "works")
