"""Fault injection through the queue with isolated SQLite and synthetic Telegram messages."""
import asyncio
import json
import tempfile
import threading
import re
from collections import Counter
from pathlib import Path
from contextlib import nullcontext
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace as NS
import unittest
import test_delivery_resume as harness
from test_delivery_resume import RetryAfter, queue, route, store
from psy_modules import psy_group_codex as model


class Recovery(unittest.IsolatedAsyncioTestCase):
    setUp = harness.Delivery.setUp
    async def test_signed_out_after_three_completed_chapters(self):
        completed = []
        sent = []
        async def telegram(text, **kwargs):
            sent.append(text)
            return NS(message_id=100+len(sent))
        self.message.reply_text.side_effect = telegram
        signed_in = False
        async def lecture(out):
            out.model_delivery()
            for i in range(4):
                if out.checkpoints.get(f"chapter.{i}") is None:
                    if i == 3 and not signed_in:
                        raise model.ModelIssue("signin", "http")
                    completed.append(i)
                    out.checkpoints.put(f"chapter.{i}", str(i))
            await out.reply_text("0123")
        async def attempt():
            item = store.claim_next()
            self.assertIsNotNone(item)
            await queue.run_job(self.app, asyncio.Event(), *item)
        with patch.object(route, "group_message", lecture):
            await attempt()
            self.assertEqual(completed, [0,1,2])
            self.assertEqual(store.state(self.ident), "queued")
            self.assertEqual(len(sent), 1)
            self.assertIsNone(store.claim_next())
            store.recover()
            self.now += 1 + store.next_retry_delay()
            signed_in = True
            await attempt()
        self.assertEqual(completed, [0,1,2,3])
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[-1], "0123")
        self.assertIsNone(store.state(self.ident))

    async def test_auth_interruption_inside_parallel_manuscript_resumes_remaining_chapter(self):
        self.message.message_thread_id = None
        self.message.is_topic_message = False
        plan = {"title": "Synthetic lecture", "sections": [
            {"title": f"Chapter {i}", "brief": "argument", "minutes": 15, "activity_minutes": 0}
            for i in range(1,5)]}
        counts = Counter()
        accepted = []
        authenticated = False
        def stream(client, headers, messages, web, selected_model, *, instructions, **kwargs):
            if model.DISPATCH in instructions:
                text = 'PSY_TASK {"minutes":60}'
            elif "Сейчас нужен внутренний редакционный" in instructions:
                text = json.dumps(plan)
            elif match := re.search(r"Напиши целиком только раздел (\d+)", instructions):
                i = int(match.group(1))
                if i == 4 and not authenticated:
                    raise model.ModelIssue("signin", "http")
                counts[i] += 1
                text = f"chapter {i} completed. " * 90
            elif "Проверь содержание раздела" in instructions:
                text = '{"missing":""}'
            else:
                self.fail("unexpected manuscript stage")
            return model.Result(text, [], 0)
        def run_model(turns, **opts):
            return model._compose(None, turns, opts["research"], opts["model"], opts["question"],
                                  checkpoints=opts["checkpoints"])
        async def send(*, document, **kwargs):
            accepted.append(document.read().decode())
            return NS(message_id=111)
        self.message.reply_document.side_effect = send
        self.message.reply_text.return_value = NS(message_id=110)
        store.Checkpoints(self.ident).put("route:input:v1", dict(destination=[-202,7,0,100], doc_id=None,
                          question="write lecture",quote="",history=[],research=False,draw=False,model="fixture"))
        with (patch.object(model,"_stream",stream), patch.object(model,"_connection",return_value=("fixture", {}, "fixture-generation")),
              patch.object(model.codex_policy, "record_model_refusal") as refused,
              patch.object(model,"_transport",nullcontext), patch.object(model,"_respond",side_effect=run_model)):
            await queue.run_job(self.app,asyncio.Event(),*store.claim_next())
            self.assertEqual(counts, Counter({1:1,2:1,3:1}))
            self.assertEqual(accepted, [])
            self.assertEqual(store.state(self.ident),"queued")
            refused.assert_called_once_with("fixture-generation")
            self.assertEqual(store.next_retry_delay(), 300)
            # Start a fresh gateway worker with the same private SQLite and no new updates.
            # Its ordinary retry lies five minutes in the future on the frozen clock.
            store.bind_instance(route.BOT, route.GROUP, adopt_legacy=True)
            self.app.adapter = NS()
            self.app.create_task = asyncio.create_task
            authenticated = True
            sleep_started = asyncio.Event()
            next_delay = store.next_retry_delay
            loop = asyncio.get_running_loop()
            def observe_wait(**kwargs):
                delay = next_delay(**kwargs)
                if delay == 300:
                    loop.call_soon_threadsafe(sleep_started.set)
                return delay
            with tempfile.TemporaryDirectory() as lease_dir, patch.object(
                    store, "lease", lambda lock=store.lease: lock(root=Path(lease_dir))), patch.object(
                    store, "next_retry_delay", side_effect=observe_wait):
                queue.schedule(self.app)
                worker = self.app.adapter._psy_group_task
                try:
                    await asyncio.wait_for(sleep_started.wait(), 3)
                    await queue.resume_authorized(self.app, "verified-owner-login")
                    await asyncio.wait_for(worker, 3)
                finally:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
        self.assertEqual(counts, Counter({1:1,2:1,3:1,4:1}))
        self.assertEqual(len(accepted),1)
        self.assertTrue(all(f"chapter {i} completed." in accepted[0] for i in range(1,5)))
        self.assertEqual(self.message.reply_text.await_count,1)

    async def test_interrupted_continuation_keeps_prior_text(self):
        for ident, kind in ((72,"connection"),(73,"signin")):
            body=json.dumps({"update_id":ident})
            store.pending(7,ident,body)
            # The fixture created a distinct incoming update that is not part of this case.
            if ident == 72:
                store.settle(self.ident,True)
            self.message.message_thread_id=None
            self.message.is_topic_message=False
            received=[]
            first_calls=0
            interrupted=True
            def stream(*args, instructions, **kwargs):
                nonlocal first_calls
                if model.DISPATCH in instructions:
                    first_calls += 1
                    return model.Result("Первая глава.",[],0,False)
                self.assertEqual(args[2][-2]["content"],"Первая глава.")
                if interrupted:
                    raise model.ModelIssue(kind,"stream_read" if kind=="connection" else "credentials")
                return model.Result("Вторая глава.",[],0)
            async def deliver(text, **kwargs):
                received.append(text)
                return NS(message_id=300+len(received))
            self.message.reply_text.side_effect=deliver
            store.Checkpoints(ident).put("route:input:v1",dict(destination=[-202,7,0,100],doc_id=None,
                        question="write lecture", quote="",history=[],research=False,draw=False,model="test"))
            def run_model(turns, **opts):
                return model._compose(None,turns,False,"test",opts["question"],checkpoints=opts["checkpoints"])
            with (patch.object(queue.Update,"de_json",return_value=NS(message=self.message,update_id=ident)),
                  patch.object(model,"_stream",stream), patch.object(model,"_connection",return_value=("fixture",{}, "fixture-generation")),
                  patch.object(model,"_transport",nullcontext), patch.object(model,"_respond",side_effect=run_model)):
                await queue.run_job(self.app,asyncio.Event(),*store.claim_next())
                self.assertEqual(store.state(ident),"queued")
                self.assertEqual(len(received),1)
                interrupted=False
                store.recover()
                self.now += 1 + store.next_retry_delay()
                await queue.run_job(self.app,asyncio.Event(),*store.claim_next())
            self.assertEqual(first_calls,1)
            self.assertEqual(received[1:],["Первая глава.\nВторая глава."])

    async def test_three_heartbeat_only_classifications_release_worker_for_fourth(self):
        all_active = threading.Barrier(3, timeout=3)
        answered = asyncio.Event()
        wake = asyncio.Event()
        clocks = threading.local()
        for ident in (32, 33, 34):
            self.assertEqual(store.pending(ident, ident, json.dumps({"update_id": ident})), "queued")
        def restore(data, bot):
            ident = data.get("update_id", 31)
            msg = NS(**vars(self.message))
            msg.from_user = NS(id=7 if ident==31 else ident,is_bot=False)
            msg.message_id = ident
            msg.reply_text = AsyncMock(return_value=NS(message_id=ident+300))
            return NS(message=msg, update_id=ident)
        def classify(checkpoints):
            all_active.wait()
            def heartbeat():
                for _ in range(100):
                    clocks.now += 60
                    yield ": heartbeat"
                self.fail("heartbeat held the worker for 100 minutes")
            clocks.now = 0
            response=NS(status_code=200, iter_lines=heartbeat)
            client=Mock()
            client.stream.return_value=nullcontext(response)
            try:
                model._compose(client, [model._user("synthetic")], False, "fixture", "synthetic", checkpoints=checkpoints)
            except model._ResumeModelJob as interrupted:
                raise interrupted.issue
        async def group(msg):
            if msg.ident == 34:
                await msg.reply_text("fourth answer")
                answered.set()
                return
            await asyncio.to_thread(classify, msg.checkpoints)
        with (patch.object(queue.Update, "de_json", side_effect=restore), patch.object(route, "group_message",group),
              patch.object(model, "_connection", return_value=("fixture", {}, "fixture-generation")), patch.object(model, "_transport", nullcontext),
              patch.object(model, "time", NS(monotonic=lambda: getattr(clocks,"now",0)))):
            worker=asyncio.create_task(queue.work(self.app, wake))
            try:
                await asyncio.wait_for(answered.wait(),5)
                for _ in range(300):
                    if all(store.state(i)=="queued" for i in (31,32,33)):
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(all(store.state(i)=="queued" for i in (31,32,33)))
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    async def test_three_429_responses_release_slots_for_fourth(self):
        wake = asyncio.Event()
        answered = asyncio.Event()
        accepted = []
        attempts = []
        for ident in (32, 33, 34):
            self.assertEqual(store.pending(7, ident, json.dumps({"update_id": ident})), "queued")

        def restore(data, bot):
            ident = data.get("update_id", 31)
            msg = NS(**vars(self.message))
            msg.message_id = ident
            async def send(text, **kwargs):
                first = ident not in attempts
                attempts.append(ident)
                if ident != 34 and first:
                    raise RetryAfter(3600)
                accepted.append(ident)
                if ident == 34:
                    answered.set()
                return NS(message_id=ident + 500)
            msg.reply_text = send
            return NS(message=msg, update_id=ident)

        async def group(msg):
            await msg.reply_text("synthetic answer")
        with patch.object(queue.Update, "de_json", side_effect=restore), patch.object(route, "process_group", group):
            worker = asyncio.create_task(queue.work(self.app, wake))
            try:
                await asyncio.wait_for(answered.wait(), 2)
                self.assertEqual(accepted, [34])
                # Advance the virtual clock only after all three rejections committed.
                # The fourth may legitimately finish while two receipt transactions run.
                for _ in range(300):
                    if all(store.state(i)=="queued" for i in (31,32,33)):
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(all(store.state(i)=="queued" for i in (31,32,33)))
                self.now += 3601
                wake.set()
                await asyncio.wait_for(worker, 3)
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
        self.assertCountEqual(accepted, [31, 32, 33, 34])
        self.assertCountEqual(attempts, [31, 31, 32, 32, 33, 33, 34])

    async def test_restart_at_notice_does_not_complete_or_repeat_it(self):
        async def group(msg):
            raise model.ModelIssue("signin", "credentials")
        self.message.reply_text.side_effect = asyncio.CancelledError
        with patch.object(route, "group_message", group):
            with self.assertRaises(asyncio.CancelledError):
                await queue.run_job(self.app, asyncio.Event(), *store.claim_next())
            self.assertEqual(store.state(self.ident), "queued")
            store.recover()
            self.now += store.next_retry_delay() + 1
            await queue.run_job(self.app, asyncio.Event(), *store.claim_next())
        self.assertEqual(self.message.reply_text.await_count, 1)
        self.assertEqual(store.state(self.ident), "queued")

    async def test_cross_group_replay_never_contacts_telegram(self):
        self.message.chat.id = -9191
        with patch.object(route, "process_group", AsyncMock()) as group:
            await queue.run_job(self.app, asyncio.Event(), *store.claim_next())
        group.assert_not_awaited()
        self.message.reply_text.assert_not_awaited()
        self.assertEqual(store.state(self.ident), "uncertain")

    async def test_generated_manuscript_delivered_when_archive_is_full(self):
        manuscript = "текст главы. " * 700
        sent = []
        async def document(*, document, **kwargs):
            sent.append(document.read())
            return NS(message_id=842)
        self.message.reply_document.side_effect = document
        job = dict(destination=[self.message.chat.id,7,444,100], doc_id=None, question="lecture")
        cp = store.Checkpoints(self.ident)
        cp.put("route:input:v1", job)
        cp.put("route:answer:v1", {"text":manuscript, "images":[]})
        with patch.object(store, "MEMBER_LIMIT", 10), patch.object(route, "respond") as provider:
            await queue.run_job(self.app, asyncio.Event(), *store.claim_next())
        provider.assert_not_called()
        self.assertEqual(sent, [manuscript.encode()])
        self.assertEqual(store.listing(7,444), [])
        self.assertIsNone(store.state(self.ident))


class VaultIdentity(unittest.TestCase):
    def test_empty_populated_and_cross_instance(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            store.archive(7, 0, "source", "text", b"synthetic", root=root)
            with self.assertRaises(RuntimeError):
                store.bind_instance(303, -202, root=root)
            store.bind_instance(303,-202, root=root, adopt_legacy=True)
            store.bind_instance(303,-202, root=root)
            for replacement in [(304,-202), (303,-203)]:
                with self.assertRaises(RuntimeError):
                    store.bind_instance(*replacement, root=root, adopt_legacy=True)
            self.assertEqual(store.document(7,0,1,root=root)[2], b"synthetic")
        with tempfile.TemporaryDirectory() as name:
            store.bind_instance(303,-202,root=Path(name))
