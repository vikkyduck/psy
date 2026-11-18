"""Storage failures inside a running job requeue it with its checkpoints; nothing is settled."""
import asyncio
import json
import sqlite3
import unittest
from contextlib import nullcontext
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import test_delivery_resume as harness
from test_delivery_resume import queue, route, store
from psy_modules import psy_group_client as codex, psy_group_codex as model
from psy_modules import psy_group_pipeline as pipeline, psy_group_prompts as prompts

LOCKED = sqlite3.OperationalError("database is locked")


class InfrastructureRetry(unittest.IsolatedAsyncioTestCase):
    setUp = harness.Delivery.setUp

    async def attempt(self):
        item = store.claim_next()
        self.assertIsNotNone(item)
        await queue.run_job(self.app, asyncio.Event(), *item)

    def received(self):
        sent = []
        async def telegram(text, **kwargs):
            sent.append(text)
            return NS(message_id=700 + len(sent))
        self.message.reply_text.side_effect = telegram
        return sent

    async def test_locked_checkpoint_write_mid_job_keeps_saved_chapters(self):
        sent = self.received()
        completed = []
        locked = True
        save = store.Checkpoints.put

        def flaky(checkpoints, key, value):
            if key == "chapter.2" and locked:
                raise LOCKED
            save(checkpoints, key, value)

        async def lecture(out):
            out.model_delivery()
            for i in range(3):
                if await asyncio.to_thread(out.checkpoints.get, f"chapter.{i}") is None:
                    completed.append(i)
                    await asyncio.to_thread(out.checkpoints.put, f"chapter.{i}", str(i))
            await out.reply_text("012")

        with patch.object(route, "group_message", lecture), patch.object(store.Checkpoints, "put", flaky):
            await self.attempt()
            self.assertEqual(store.state(self.ident), "queued")
            saved = store.Checkpoints(self.ident)
            self.assertEqual([saved.get(f"chapter.{i}") for i in range(3)], ["0", "1", None])
            self.assertEqual(sent, ["Запрос сохранен. PSY продолжит его автоматически."])
            self.assertIsNone(store.claim_next())
            self.assertGreater(store.next_retry_delay(), 0)
            locked = False
            self.now += 1 + store.next_retry_delay()
            await self.attempt()
        self.assertEqual(completed, [0, 1, 2, 2])
        self.assertEqual(sent[1:], ["012"])
        self.assertIsNone(store.state(self.ident))

    async def test_locked_progress_heartbeat_during_continuation_resumes_saved_prefix(self):
        self.message.message_thread_id = None
        self.message.is_topic_message = False
        sent = self.received()
        dispatches = 0
        locked = True
        heartbeat = store.Checkpoints.progress

        def progress(checkpoints, *args, **kwargs):
            if locked:
                raise LOCKED
            heartbeat(checkpoints, *args, **kwargs)

        def stream(*args, instructions, on_progress=None, **kwargs):
            nonlocal dispatches
            if prompts.DISPATCH in instructions:
                dispatches += 1
                return model.Result("Первая часть.", [], 0, False)
            on_progress()
            return model.Result("Вторая часть.", [], 0)

        def run_model(turns, **opts):
            return pipeline._compose(None, turns, False, "fixture", opts["question"], checkpoints=opts["checkpoints"])

        store.Checkpoints(self.ident).put("route:input:v1", dict(
            destination=[-202, 7, 0, 100], doc_id=None, question="write", quote="", history=[],
            research=False, draw=False, model="fixture"))
        with (patch.object(codex, "_stream", stream), patch.object(store.Checkpoints, "progress", progress),
              patch.object(codex, "_connection", return_value=("fixture", {}, "fixture-generation")),
              patch.object(codex, "_transport", nullcontext), patch.object(model, "_respond", side_effect=run_model)):
            await self.attempt()
            self.assertEqual(store.state(self.ident), "queued")
            self.assertEqual(sent, ["Запрос сохранен. PSY продолжит его автоматически."])
            locked = False
            self.now += 1 + store.next_retry_delay()
            await self.attempt()
        self.assertEqual(dispatches, 1)
        self.assertEqual(sent[1:], ["Первая часть.\nВторая часть."])
        self.assertIsNone(store.state(self.ident))

    async def test_failure_to_record_the_retry_still_keeps_the_job(self):
        sent = self.received()
        writes = 0
        wait = store.wait_for_model

        def contended(*args, **kwargs):
            nonlocal writes
            writes += 1
            if writes == 1:
                raise LOCKED
            return wait(*args, **kwargs)

        async def group(out):
            await asyncio.to_thread(out.checkpoints.put, "route:input:v1", {"question": "saved"})
            raise LOCKED

        with patch.object(route, "group_message", group), patch.object(store, "wait_for_model", contended):
            await self.attempt()
        self.assertEqual(writes, 2)
        self.assertEqual(store.state(self.ident), "queued")
        self.assertEqual(store.Checkpoints(self.ident).get("route:input:v1"), {"question": "saved"})
        self.assertEqual(sent, [])

    async def test_unexpected_error_is_reported_once_and_kept_for_the_operator(self):
        sent = self.received()

        async def group(out):
            await asyncio.to_thread(out.checkpoints.put, "chapter.0", "kept")
            raise KeyError("synthetic defect")

        with patch.object(route, "group_message", group):
            await self.attempt()
        self.assertEqual(sent, ["Не удалось завершить запрос. Попробуйте еще раз."])
        self.assertEqual(store.state(self.ident), "failed")
        self.assertEqual(store.Checkpoints(self.ident).get("chapter.0"), "kept")
        self.assertIsNone(store.claim_next())

    async def test_known_errors_and_answers_still_complete_the_request(self):
        sent = self.received()

        async def group(out):
            raise ValueError(route.UNLINKED_REPLY)

        with patch.object(route, "group_message", group):
            await self.attempt()
        self.assertEqual(sent, [route.UNLINKED_REPLY])
        self.assertIsNone(store.state(self.ident))

    def test_only_storage_and_network_failures_are_transient(self):
        self.assertTrue(store.transient(LOCKED))
        self.assertTrue(store.transient(sqlite3.DatabaseError("disk I/O error")))
        self.assertTrue(store.transient(OSError(28, "No space left on device")))
        self.assertTrue(store.transient(harness.RetryAfter(3)))
        for defect in (KeyError("x"), ValueError("x"), RuntimeError("x"), model.ModelIssue("context", "http")):
            self.assertFalse(store.transient(defect))


if __name__ == "__main__":
    unittest.main()
