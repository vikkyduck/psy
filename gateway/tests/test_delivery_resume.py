"""Delivery acceptance using the real queue and SQLite, and an offline Telegram double."""
import asyncio
import importlib.util
import io
import tempfile
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from psy_modules import psy_group_route as route, psy_group_store as store

if importlib.util.find_spec("telegram") is None:
    telegram = ModuleType("telegram")
    telegram.Update = Mock()
    errors = ModuleType("telegram.error")
    class RetryAfter(Exception):
        def __init__(self, delay):
            self.retry_after = delay
    errors.RetryAfter = RetryAfter
    with patch.dict(sys.modules, {"telegram": telegram}):
        from psy_modules import psy_group_queue as queue
else:
    from telegram import error as errors
    from telegram.error import RetryAfter
    from psy_modules import psy_group_queue as queue


class Delivery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        database = store.database
        self.enterContext(patch.object(store, "database", lambda *a, **kw: database(root=Path(tmp.name))))
        self.enterContext(patch.dict(sys.modules, {"telegram.error": errors}))
        self.now = 1_000_000.
        clock = NS(time=lambda: self.now)
        self.enterContext(patch.object(queue, "time", clock))
        self.enterContext(patch.object(store, "time", clock))
        self.message = NS(message_id=100, chat=NS(id=-202, type="supergroup"), from_user=NS(id=7, is_bot=False),
                          sender_chat=None, business_connection_id=None,
                          is_topic_message=True, message_thread_id=444,
                          reply_document=AsyncMock(), reply_text=AsyncMock())
        self.enterContext(patch.object(queue.Update, "de_json", return_value=NS(message=self.message, update_id=31)))
        self.enterContext(patch.object(queue, "typing", AsyncMock()))
        self.app = NS(running=True, bot=NS(id=route.BOT))
        self.ident = 31
        self.body = '{"message":{"message_id":100}}'
        self.assertEqual(store.pending(7, self.ident, self.body), "queued")

    async def test_429_wait_restarts_and_recovers_only_remaining_part_without_new_update(self):
        stored = asyncio.Event()
        loop = asyncio.get_running_loop()
        defer = store.retry_delivery
        def saved(*args, **kwargs):
            defer(*args, **kwargs)
            loop.call_soon_threadsafe(stored.set)
        accepted_ids = []
        bytes_on_wire = []
        self.message.reply_text.return_value = NS(message_id=812)
        async def endpoint(*, document, filename):
            bytes_on_wire.append(document.read())
            if len(bytes_on_wire) == 1:
                raise RetryAfter(timedelta(seconds=17))
            return NS(message_id=813)
        self.message.reply_document.side_effect = endpoint

        async def group(outbound):
            outbound.model_delivery()
            accepted_ids.append((await outbound.reply_text("heading")).message_id)
            accepted_ids.append((await outbound.reply_document(io.BytesIO(b"whole document"),
                                                              filename="lecture.txt")).message_id)

        with patch.object(route, "process_group", group), patch.object(store, "retry_delivery", saved):
            task = asyncio.create_task(queue.work(self.app, asyncio.Event()))
            await asyncio.wait_for(stored.wait(), 2)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(store.recover(), 1)
            self.assertIsNone(store.claim_next())
            self.assertEqual(store.next_retry_delay(), 17)
            async def wake_at_due(awaitable, *, timeout):
                self.assertEqual(timeout, 17)
                self.now += timeout
                awaitable.close()
                raise asyncio.TimeoutError
            with patch.object(asyncio, "wait_for", wake_at_due):
                await queue.work(self.app, asyncio.Event())
        self.assertEqual(accepted_ids, [812, 812, 813])
        self.assertEqual(self.message.reply_text.await_count, 1)
        self.assertEqual(bytes_on_wire, [b"whole document", b"whole document"])
        self.assertIsNone(store.state(self.ident))
        self.assertEqual(store.pending(7, self.ident, self.body), "existing")
        self.assertIsNone(store.claim_next())

    async def test_retry_in_same_process_rewinds_consumed_file_and_obeys_integer_seconds(self):
        self.assertIsNotNone(store.claim_next())
        outbound = queue.Outbound(self.message, self.ident)
        attempts = []
        async def endpoint(*, document, filename):
            attempts.append((self.now, document.read()))
            if len(attempts) == 1:
                raise RetryAfter(23)
            return NS(message_id=94)
        async def elapse(seconds):
            self.now += seconds
        self.message.reply_document.side_effect = endpoint
        with patch.object(asyncio, "sleep", elapse):
            receipt = await outbound.reply_document(io.BytesIO(b"original"), filename="doc.txt")
        self.assertEqual(receipt.message_id, 94)
        self.assertEqual(attempts, [(1_000_000, b"original"), (1_000_023, b"original")])

    async def test_saved_command_text_survives_restart_without_running_command_again(self):
        self.assertIsNotNone(store.claim_next())
        self.message.reply_text.side_effect = RetryAfter(11)
        with patch.object(asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await queue.Outbound(self.message, self.ident).reply_text("original listing")
        store.recover()
        self.now += 11
        self.message.reply_text.side_effect = None
        self.message.reply_text.return_value = NS(message_id=903)
        with patch.object(route, "process_group", AsyncMock()) as route_call:
            await queue.work(self.app, asyncio.Event())
        route_call.assert_not_awaited()
        self.assertEqual([call.args for call in self.message.reply_text.await_args_list],
                         [("original listing",), ("original listing",)])

    async def test_unknown_after_accept_never_replays(self):
        accepted_on_wire = []
        async def ambiguous(text):
            accepted_on_wire.append(text)
            raise TimeoutError("telegram receipt lost")
        self.message.reply_text.side_effect = ambiguous
        async def group(outbound):
            await outbound.reply_text("already accepted")
        with patch.object(route, "process_group", group):
            await queue.work(self.app, asyncio.Event())
            store.recover()
            await queue.work(self.app, asyncio.Event())
        self.assertEqual(store.state(self.ident), "uncertain")
        self.assertEqual(accepted_on_wire, ["already accepted"])

    async def test_missing_receipt_and_content_or_destination_mismatch_do_not_send(self):
        self.assertIsNotNone(store.claim_next())
        self.message.reply_document.return_value = NS(message_id=811)
        first = queue.Outbound(self.message, self.ident)
        await first.reply_document(b"saved", filename="draft")
        changed = queue.Outbound(self.message, self.ident)
        with self.assertRaises(queue.DeliveryUncertain):
            await changed.reply_document(b"modified", filename="draft")
        self.message.message_thread_id = 999
        with self.assertRaises(queue.DeliveryUncertain):
            await queue.Outbound(self.message, self.ident).reply_document(b"saved", filename="draft")
        self.assertEqual(self.message.reply_document.await_count, 1)
        self.message.message_thread_id = 444
        self.message.reply_document.return_value = NS(message_id=None)
        with self.assertRaises(queue.DeliveryUncertain):
            await first.reply_document(b"part two", filename="draft2")
        store.recover()
        self.assertEqual(store.state(self.ident), "uncertain")
