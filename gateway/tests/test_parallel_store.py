"""Durable scheduling decisions and member history under out-of-order completion."""
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from psy_modules import psy_group_store as store


class ParallelStore(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def put(self, mid, *, member=7, topic=1, parent=None):
        chat = {"id": -201}
        msg = {"message_id": mid, "chat": chat, "is_topic_message": True, "message_thread_id": topic}
        if parent:
            msg["reply_to_message"] = {"chat": chat, "message_id": parent}
        self.assertEqual(store.pending(member, mid, json.dumps({"message": msg}), root=self.root), "queued")

    def claim(self, expected, **kwargs):
        found = store.claim_next(root=self.root, **kwargs)
        self.assertEqual(found and found[0], expected)
        return store.Checkpoints(expected, root=self.root) if found else None

    def test_capacity_survives_restart_and_leaves_room_for_unknown_work(self):
        for mid in (1, 2, 3):
            self.put(mid, member=mid)
            cp = self.claim(mid)
            cp.put("route:input:v1", {})
        with ThreadPoolExecutor(3) as pool:
            admitted = list(pool.map(lambda mid: store.admit_long(mid, root=self.root), (1, 2, 3)))
        self.assertEqual(admitted.count(True), 2)
        parked = admitted.index(False) + 1
        self.claim(None)
        self.put(4)
        self.claim(4)
        store.recover(root=self.root)
        admitted_again = [store.claim_next(root=self.root)[0] for _ in range(3)]
        self.assertIn(4, admitted_again)
        self.assertEqual(set(admitted_again)-{4}, {1, 2})
        self.claim(None)
        store.settle(1, True, root=self.root)
        self.claim(3).admit_long()
        self.assertIn(parked, (1, 2, 3))

    def test_sibling_overtakes_unfinished_reply_target_after_context_is_frozen(self):
        self.put(10)
        original = self.claim(10)
        self.put(11, parent=10)
        self.put(12)
        self.claim(None)
        original.put("route:input:v1", {})
        self.claim(12)
        store.settle(12, True, root=self.root)
        self.claim(None)
        store.settle(10, True, root=self.root)
        self.claim(11)

    def test_topic_and_sender_independence_even_during_context_freeze(self):
        self.put(10)
        self.claim(10)
        self.put(11, topic=2)
        self.put(12, member=8)
        self.claim(12)  # Prefer a member not already consuming a slot.
        self.claim(11)

    def test_retry_due_between_claim_and_timer_with_blocked_due_sibling(self):
        self.put(3)
        self.claim(3)
        store.begin_delivery(3, 0, "bytes", now=0, root=self.root)
        store.retry_delivery(3, 0, 10, root=self.root)
        store.recover(root=self.root)
        self.claim(None, now=9.99)
        self.assertEqual(store.next_retry_delay(now=10.01, root=self.root), 0)
        self.claim(3, now=10.01)
        store.settle(3, True, root=self.root)
        # A blocked reply has a due retry in this synthetic restored queue. Its
        # due time cannot be the event that makes it runnable.
        self.put(12, parent=11)
        self.claim(12)
        store.begin_delivery(12, 0, "rejected", now=1, root=self.root)
        store.retry_delivery(12, 0, 2, root=self.root)
        store.recover(root=self.root)
        self.put(11)
        self.claim(11, now=3)
        self.claim(None, now=3)
        self.assertIsNone(store.next_retry_delay(now=3, root=self.root))
        store.settle(11, True, root=self.root)
        self.assertEqual(store.next_retry_delay(now=3, root=self.root), 0)

    def test_newer_source_and_turns_cannot_move_back_or_leak_into_earlier_context(self):
        a = store.archive(7, 1, "A","text", b"A", message_id=10, root=self.root)
        b = store.archive(7, 1, "B","text", b"B", message_id=20, root=self.root)
        store.set_current(7, 1, b, 20, root=self.root)
        store.set_current(7, 1, a, 10, root=self.root)
        store.remember(7, 1, "twenty","B", source_id=b, message_id=20, root=self.root)
        store.remember(7, 1, "ten","A", source_id=a, message_id=10, root=self.root)
        self.assertEqual(store.choices(7,1,root=self.root)[0], b)
        self.assertEqual(store.choices(7,1,root=self.root,before_message_id=5), (None, []))
        active, options = store.choices(7,1,True,root=self.root,before_message_id=15)
        self.assertEqual((active, [o['name'] for o in options]), (a, ['A']))
        before = store.history(7,1,before_message_id=15,root=self.root)
        self.assertEqual([r['content'] for r in before], ['ten','A'])
        self.assertEqual([r['content'] for r in store.history(7,1,root=self.root)], ['ten','A','twenty','B'])

    def test_legacy_queue_and_current_source_upgrade(self):
        with sqlite3.connect(self.root/'group.sqlite3') as db:
            db.execute("CREATE TABLE requests(id INTEGER PRIMARY KEY,uid INTEGER,body TEXT,state TEXT)")
            db.execute("INSERT INTO requests VALUES(5,7,'old','queued')")
            db.execute("CREATE TABLE current_sources(uid INTEGER,topic INTEGER,doc_id INTEGER,PRIMARY KEY(uid,topic))")
            db.execute("INSERT INTO current_sources VALUES(7,1,77)")
        cp = self.claim(5)
        cp.admit_long()
        store.set_current(7,1,88,120,root=self.root)
        self.assertEqual(store.choices(7,1,before_message_id=110,root=self.root)[0],77)
