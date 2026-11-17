"""One failing job or scheduler step must not stop the queue or cancel sibling jobs."""
import asyncio
import sqlite3
import unittest
from unittest.mock import patch

from test_queue_parallel import ParallelQueue, queue, route, store

LOCKED = sqlite3.OperationalError("database is locked")


async def until(predicate, timeout=3):
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


class QueueResilience(unittest.IsolatedAsyncioTestCase):
    setUp = ParallelQueue.setUp
    enqueue = ParallelQueue.enqueue

    async def stop_drain(self):
        task = getattr(self.app.adapter, "_psy_group_task", None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def finish_drain(self):
        await asyncio.wait_for(asyncio.shield(self.app.adapter._psy_group_task), 3)

    async def test_failed_bookkeeping_of_one_job_keeps_siblings_running(self):
        started = asyncio.Event()
        release = asyncio.Event()
        settle = store.settle

        def broken(ident, success, **kwargs):
            if ident == 1:
                raise RuntimeError("synthetic bookkeeping defect")
            return settle(ident, success, **kwargs)

        async def group(out):
            if out.ident == 2:
                started.set()
                await release.wait()
            await out.reply_text(f"answer:{out.ident}")

        self.addAsyncCleanup(self.stop_drain)
        with patch.object(route, "process_group", group), patch.object(store, "settle", broken):
            await self.enqueue(2, "long", uid=18)
            await asyncio.wait_for(started.wait(), 2)
            await self.enqueue(1, "short", uid=17)
            await until(lambda: (1, "answer:1") in self.sent and store.state(1) == "queued")
            await self.enqueue(3, "next", uid=19)
            await until(lambda: store.state(3) is None)
            self.assertEqual(store.state(2), "running")
            release.set()
            await until(lambda: store.state(2) is None)
        self.assertCountEqual(self.sent, [(1, "answer:1"), (2, "answer:2"), (3, "answer:3")])
        # The job whose receipt could not be committed is retried later, not lost.
        self.assertEqual(store.state(1), "queued")
        self.assertGreater(store.next_retry_delay(), 0)

    async def test_transient_settle_failure_is_retried_until_committed(self):
        calls = []
        settle = store.settle

        def contended(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise LOCKED
            return settle(*args, **kwargs)

        async def group(out):
            await out.reply_text("answer")

        with (patch.object(queue, "RETRY_START", 0.01), patch.object(route, "process_group", group),
              patch.object(store, "settle", contended)):
            await self.enqueue(1, "question")
            await self.finish_drain()
        self.assertEqual(calls, [(1, True), (1, True)])
        self.assertEqual(self.sent, [(1, "answer")])
        self.assertIsNone(store.state(1))

    async def test_scheduler_defect_restarts_the_drain(self):
        claims = 0
        claim = store.claim_next

        def defective(**kwargs):
            nonlocal claims
            claims += 1
            if claims == 1:
                raise RuntimeError("synthetic scheduler defect")
            return claim(**kwargs)

        async def group(out):
            await out.reply_text("answer")

        with (patch.object(queue, "RESTART_START", 0.01), patch.object(route, "process_group", group),
              patch.object(store, "claim_next", defective)):
            await self.enqueue(1, "question")
            await self.finish_drain()
        self.assertEqual(self.sent, [(1, "answer")])
        self.assertIsNone(store.state(1))

    async def test_locked_claim_pauses_scheduling_without_touching_running_jobs(self):
        started = asyncio.Event()
        release = asyncio.Event()
        claims = 0
        claim = store.claim_next

        def contended(**kwargs):
            nonlocal claims
            claims += 1
            if claims == 2:
                raise LOCKED
            return claim(**kwargs)

        async def group(out):
            if out.ident == 1:
                started.set()
                await release.wait()
            await out.reply_text(f"answer:{out.ident}")

        with (patch.object(queue, "RETRY_START", 0.01), patch.object(route, "process_group", group),
              patch.object(store, "claim_next", contended)):
            await self.enqueue(1, "long")
            await asyncio.wait_for(started.wait(), 2)
            await self.enqueue(2, "short", uid=18)
            await until(lambda: store.state(2) is None)
            release.set()
            await self.finish_drain()
        self.assertCountEqual(self.sent, [(1, "answer:1"), (2, "answer:2")])

    async def test_periodic_check_runs_a_due_retry_without_a_new_message(self):
        attempts = 0

        async def group(out):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise LOCKED
            await out.reply_text("answer")

        self.addAsyncCleanup(self.stop_drain)
        with patch.object(route, "process_group", group), patch.object(queue, "TICK_SECONDS", 0.01):
            await self.enqueue(1, "question")
            await until(lambda: attempts == 1 and store.state(1) == "queued")
            # The drain sleeps until the retry or has stopped. Neither must strand the job.
            await self.stop_drain()
            self.assertFalse(store.runnable())
            await queue.check(self.app)
            self.assertTrue(self.app.adapter._psy_group_task.done())
            with store.database() as con:
                con.execute("UPDATE model_retries SET retry_at=0")
            self.assertTrue(store.runnable())
            ticker = asyncio.create_task(queue.watch(self.app))
            try:
                await until(lambda: store.state(1) is None)
            finally:
                self.app.running = False
                await asyncio.wait_for(ticker, 2)
        self.assertEqual(self.sent, [(1, "answer")])
        self.assertEqual(attempts, 2)

    async def test_periodic_check_recovers_a_job_left_running_by_a_stopped_drain(self):
        started = asyncio.Event()
        calls = 0

        async def group(out):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await asyncio.Event().wait()
            await out.reply_text("answer")

        with patch.object(route, "process_group", group):
            await self.enqueue(1, "question")
            await asyncio.wait_for(started.wait(), 2)
            await self.stop_drain()
            self.assertEqual(store.state(1), "running")
            self.assertFalse(store.runnable())
            await queue.check(self.app)
            await self.finish_drain()
        self.assertEqual(self.sent, [(1, "answer")])
        self.assertIsNone(store.state(1))


if __name__ == "__main__":
    unittest.main()
