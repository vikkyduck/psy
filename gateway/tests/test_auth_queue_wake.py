"""A verified Codex login may release only retries blocked by authorization."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from psy_modules import psy_group_store as store


class AuthQueueWake(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.now = 1000

    def start_job(self, ident):
        body = json.dumps({"update_id": ident})
        self.assertEqual(store.pending(ident, ident, body, root=self.root), "queued")
        self.assertEqual(store.claim_next(root=self.root, now=self.now), (ident, body))

    def test_only_auth_retries_become_due_and_notice_is_not_repeated(self):
        kinds = ("signin", "access_wait", "connection", "quota", "busy")
        for ident, kind in enumerate(kinds, start=1):
            self.start_job(ident)
            self.assertTrue(store.wait_for_model(ident, kind, root=self.root, now=self.now))
        self.start_job(6)
        store.begin_delivery(6, 0, "file", root=self.root)
        store.retry_delivery(6, 0, self.now + 20, root=self.root, park=True)
        self.assertEqual(store.release_auth_waits("first", root=self.root), 2)
        for ident in (1, 2):
            self.assertEqual(store.claim_next(root=self.root, now=self.now)[0], ident)
            self.assertFalse(store.wait_for_model(ident, "signin", root=self.root, now=self.now))
        self.assertEqual(store.release_auth_waits("first", root=self.root), 0)
        self.assertIsNone(store.claim_next(root=self.root, now=self.now))
        self.assertEqual(store.next_retry_delay(root=self.root, now=self.now), 20)

    def test_late_failure_of_claimed_job_gets_one_retry_after_proof(self):
        self.start_job(11)
        self.assertEqual(store.release_auth_waits("first", root=self.root), 0)
        self.assertTrue(store.wait_for_model(11, "signin", root=self.root, now=self.now))
        self.assertEqual(store.claim_next(root=self.root, now=self.now)[0], 11)
        store.wait_for_model(11, "access_wait", root=self.root, now=self.now)
        self.assertEqual(store.release_auth_waits("first", root=self.root), 0)
        self.assertIsNone(store.claim_next(root=self.root, now=self.now))
        self.assertEqual(store.release_auth_waits("second", root=self.root), 1)
        self.assertEqual(store.claim_next(root=self.root, now=self.now)[0], 11)

    def test_unclassified_legacy_retries_keep_their_timer(self):
        with sqlite3.connect(self.root / "group.sqlite3") as db:
            db.execute("CREATE TABLE requests(id INTEGER PRIMARY KEY,uid INTEGER,body TEXT,state TEXT)")
            db.execute("CREATE TABLE model_retries(request_id INTEGER PRIMARY KEY, attempts INTEGER, retry_at REAL)")
            db.execute("INSERT INTO requests VALUES (10,10,'legacy','queued')")
            db.execute("INSERT INTO model_retries VALUES (10,2,1800)")
        store.recover(root=self.root)
        self.assertEqual(store.release_auth_waits("first", root=self.root), 0)
        self.assertIsNone(store.claim_next(root=self.root, now=self.now))
        self.assertEqual(store.claim_next(root=self.root, now=1800), (10, "legacy"))
