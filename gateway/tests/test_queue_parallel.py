"""Exercise independent group updates through admission, SQLite and a single drain lease."""
import asyncio
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from psy_modules import psy_group_route as route, psy_group_store as store

if importlib.util.find_spec("telegram") is None:
    telegram = ModuleType("telegram")
    telegram.Update = Mock()
    with patch.dict(sys.modules, {"telegram": telegram}):
        from psy_modules import psy_group_queue as queue
else:
    from psy_modules import psy_group_queue as queue


class ParallelQueue(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        database, lease = store.database, store.lease
        self.enterContext(patch.object(store, "database", lambda *a, **kw: database(root=root)))
        self.enterContext(patch.object(store, "lease", lambda: lease(root=root)))
        self.messages = {}
        self.enterContext(patch.object(queue.Update, "de_json", side_effect=lambda data, bot:
                                       NS(message=self.messages[data["update_id"]], update_id=data["update_id"])))
        self.enterContext(patch.object(queue, "typing", AsyncMock()))
        self.app = NS(running=True, bot=NS(id=route.BOT), adapter=NS(), create_task=asyncio.create_task)
        self.sent = []

    async def enqueue(self, ident, text, uid=17, reply=None):
        data = {"message_id": ident, "text": text, "date": 1700000000, "chat": {"id": -202},
                "from": {"id": uid, "is_bot": False, "first_name": "Test"}}
        if reply:
            data["reply_to_message"] = {"message_id": reply, "chat": data["chat"]}
        async def deliver(text, **kwargs):
            self.sent.append((ident, text))
            return NS(message_id=ident + 100)
        message = NS(chat=NS(id=-202, type="supergroup"), from_user=NS(id=uid, is_bot=False), message_id=ident,
                     text=text, sender_chat=None, business_connection_id=None,
                     is_topic_message=False, message_thread_id=None, reply_text=deliver)
        self.messages[ident] = message
        update = NS(update_id=ident, message=message,
                    to_json=lambda: json.dumps({"message": data, "update_id": ident}))
        await queue.accept(self.app, update)

    async def test_two_long_jobs_leave_capacity_for_same_member_question_and_waiting_reply(self):
        running = {i: asyncio.Event() for i in range(1, 6)}
        parked = asyncio.Event()
        release = asyncio.Event()
        prompt_receipt = asyncio.Event()
        peak, live = 0, 0

        async def group(out):
            nonlocal live, peak
            ident = out.message_id
            # Mirrors the route's durable snapshot and the model's semantic admission
            # after deciding a request needs long-form research.
            await asyncio.to_thread(out.checkpoints.put, "route:input:v1", {"question": out.text})
            if out.text == "long":
                if not await asyncio.to_thread(store.admit_long, out.ident):
                    parked.set()
                    raise store.GroupJobParked()
            live += 1
            peak = max(peak, live)
            running[ident].set()
            try:
                if out.text == "long":
                    await release.wait()
                await out.reply_text("answer:" + str(ident))
                if out.text == "short":
                    prompt_receipt.set()
            finally:
                live -= 1

        with patch.object(route, "process_group", group):
            try:
                await self.enqueue(1, "long")
                await asyncio.wait_for(running[1].wait(), 2)
                await self.enqueue(2, "long", uid=18)
                await asyncio.wait_for(running[2].wait(), 2)
                await self.enqueue(3, "long", uid=19)
                await asyncio.wait_for(parked.wait(), 2)
                # Both updates come from the original member. Only the second
                # refers to the unfinished request by Telegram's reply metadata.
                await self.enqueue(4, "short")
                await self.enqueue(5, "follow-up", reply=1)
                await asyncio.wait_for(prompt_receipt.wait(), 2)
                self.assertEqual(self.sent, [(4, "answer:4")])
                self.assertFalse(running[3].is_set())
                self.assertFalse(running[5].is_set())
            finally:
                release.set()
                await asyncio.wait_for(asyncio.shield(self.app.adapter._psy_group_task), 3)
        self.assertCountEqual(self.sent, [(i, "answer:" + str(i)) for i in range(1, 6)])
        self.assertLess(self.sent.index((1, "answer:1")), self.sent.index((5, "answer:5")))
        self.assertEqual(peak, 3)
        self.assertTrue(all(store.state(i) is None for i in range(1, 6)))

    async def test_input_snapshot_wakes_waiting_request_without_a_new_update(self):
        in_prepare = asyncio.Event()
        allow_snapshot = asyncio.Event()
        release = asyncio.Event()
        answered = asyncio.Event()

        async def group(out):
            if out.ident == 10:
                in_prepare.set()
                await allow_snapshot.wait()
                await asyncio.to_thread(out.checkpoints.put, "route:input:v1", {"question": "demo"})
                await release.wait()
            await out.reply_text("done")
            if out.ident == 11:
                answered.set()

        with patch.object(route, "process_group", group):
            try:
                await self.enqueue(10, "lecture")
                await asyncio.wait_for(in_prepare.wait(), 2)
                await self.enqueue(11, "unrelated question")
                allow_snapshot.set()
                await asyncio.wait_for(answered.wait(), 2)
                self.assertEqual(self.sent, [(11, "done")])
            finally:
                release.set()
                await asyncio.wait_for(asyncio.shield(self.app.adapter._psy_group_task), 3)

    async def test_cancellation_keeps_process_lease_until_all_children_release(self):
        started = {i: asyncio.Event() for i in (31, 32)}
        ending = {i: asyncio.Event() for i in (31, 32)}
        release = asyncio.Event()

        async def group(out):
            started[out.ident].set()
            try:
                await asyncio.Event().wait()
            finally:
                ending[out.ident].set()
                await release.wait()

        with patch.object(route, "process_group", group):
            try:
                await self.enqueue(31, "active")
                await self.enqueue(32, "also active", uid=23)
                await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), 2)
                runner = self.app.adapter._psy_group_task
                runner.cancel()
                await asyncio.wait_for(asyncio.gather(*(event.wait() for event in ending.values())), 2)
                self.assertIsNone(store.lease())
            finally:
                release.set()
                runner = self.app.adapter._psy_group_task
                if not runner.cancelling():
                    runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)
        self.assertEqual(store.recover(), 2)
        self.assertEqual({store.claim_next()[0], store.claim_next()[0]}, {31, 32})
        self.assertEqual(self.sent, [])
