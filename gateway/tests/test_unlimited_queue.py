"""No count or quota refusal: every request is queued; only the physical disk reserve applies."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_queue_parallel import ParallelQueue, route, store


class UnlimitedStore(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_many_requests_from_one_member_and_the_group_are_all_queued_in_order(self):
        for ident in range(1, 26):
            self.assertEqual(store.pending(7, ident, f"member request {ident}", root=self.root), "queued")
        for ident in range(26, 61):
            self.assertEqual(store.pending(ident, ident, f"group request {ident}", root=self.root), "queued")
        claimed = [store.claim_next(root=self.root)[0] for _ in range(3)]
        # A member with work already running yields to members who have none.
        self.assertEqual(claimed, [1, 26, 27])
        self.assertEqual(store.recover(root=self.root), 60)

    def test_documents_have_no_quota_and_stop_only_at_the_disk_reserve(self):
        self.assertFalse(hasattr(store, "MEMBER_LIMIT") or hasattr(store, "TOTAL_LIMIT"))
        data = b"x" * 4096
        needed = store.DISK_RESERVE + 2 * len(data)
        with patch.object(store, "_free_bytes", return_value=needed):
            for i in range(20):
                store.archive(7, 0, f"file {i}", "text", data, message_id=100 + i, root=self.root)
        with patch.object(store, "_free_bytes", return_value=needed - 1):
            with self.assertRaises(store.ArchiveFull):
                store.archive(7, 0, "one more", "text", data, root=self.root)
            # A file already bound to its message is reused and needs no new space.
            self.assertEqual(store.archive(7, 0, "file 0", "text", data, message_id=100, root=self.root), 1)
        self.assertEqual(len(store.listing(7, 0, root=self.root)), 20)

    def test_free_space_is_measured_on_the_database_filesystem(self):
        with store.database(self.root) as con:
            self.assertGreater(store._free_bytes(con), 0)


class UnlimitedAdmission(unittest.IsolatedAsyncioTestCase):
    setUp = ParallelQueue.setUp
    enqueue = ParallelQueue.enqueue

    async def test_a_busy_queue_never_refuses_a_new_message(self):
        release = asyncio.Event()

        async def group(out):
            await release.wait()
            await out.reply_text(f"answer:{out.ident}")

        with patch.object(route, "process_group", group):
            try:
                for ident in range(1, 16):
                    await self.enqueue(ident, f"question {ident}")
                self.assertEqual(self.sent, [])
                self.assertTrue(all(store.state(i) in ("queued", "running") for i in range(1, 16)))
            finally:
                release.set()
                await asyncio.wait_for(asyncio.shield(self.app.adapter._psy_group_task), 5)
        self.assertCountEqual(self.sent, [(i, f"answer:{i}") for i in range(1, 16)])


if __name__ == "__main__":
    unittest.main()
