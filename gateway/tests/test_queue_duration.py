import asyncio
import importlib.util
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock, patch

from psy_modules import psy_group_route as route

# The restore CI exercises the gateway boundary without installing a Telegram client.
if importlib.util.find_spec("telegram") is None:
    telegram = ModuleType("telegram")
    telegram.Update = Mock()
    with patch.dict(sys.modules, {"telegram": telegram}):
        from psy_modules import psy_group_queue as queue
else:
    from psy_modules import psy_group_queue as queue


class QueueDuration(unittest.IsolatedAsyncioTestCase):
    async def test_work_finishes_once_after_a_day_in_the_queue_worker(self):
        loop = asyncio.get_running_loop()
        real_time = loop.time
        offset = 0
        pending = iter([(7, "{}")])
        processing = asyncio.Event()
        finish = asyncio.Event()
        message = SimpleNamespace(is_topic_message=False, chat=SimpleNamespace(id=-202, type="supergroup"),
                                  sender_chat=None, business_connection_id=None,
                                  from_user=SimpleNamespace(id=101, is_bot=False), message_id=11,
                                  reply_document=AsyncMock(return_value=SimpleNamespace(message_id=12)),
                                  reply_text=AsyncMock())

        async def write_lecture(outbound):
            processing.set()
            await finish.wait()
            await outbound.reply_document(b"completed lecture")

        with (
            patch.object(loop, "slow_callback_duration", float("inf")),
            patch.object(loop, "time", lambda: real_time() + offset),
            patch.object(queue.Update, "de_json", return_value=SimpleNamespace(message=message, update_id=7)),
            patch.object(queue.store, "claim_next", side_effect=lambda **_: next(pending, None)),
            patch.object(queue.store, "next_retry_delay", return_value=None),
            patch.object(queue.store.Checkpoints, "get", return_value=None),
            patch.object(queue.store, "begin_delivery", return_value=("send", None)) as begin_send,
            patch.object(queue.store, "confirm_delivery") as confirm,
            patch.object(queue.store, "settle") as settle,
            patch.object(queue.store, "owns_running_job", return_value=True),
            patch.object(queue, "typing", AsyncMock()),
            patch.object(route, "process_group", write_lecture),
        ):
            app = SimpleNamespace(running=True, bot=SimpleNamespace(id=route.BOT))
            worker = asyncio.create_task(queue.work(app, asyncio.Event()))
            try:
                await processing.wait()
                offset = 86400
                # Let overdue timers, followed by their cancellation callbacks, execute.
                for _ in range(4):
                    await asyncio.sleep(0)
                finish.set()
                await worker
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
        begin_send.assert_called_once_with(7, 0, ANY)
        confirm.assert_called_once_with(7, 0, 12)
        message.reply_document.assert_awaited_once_with(document=b"completed lecture")
        message.reply_text.assert_not_awaited()
        settle.assert_called_once_with(7, True)

    async def test_typing_follows_live_job_across_former_cutoff(self):
        finished = asyncio.Event()
        count = 0

        async def action(**kwargs):
            nonlocal count
            count += 1
            if count == 465:
                finished.set()

        bot = SimpleNamespace(send_chat_action=action)
        message = SimpleNamespace(chat=SimpleNamespace(id=-202, type="supergroup"),
                                  sender_chat=None, business_connection_id=None, is_topic_message=False)
        with patch.object(asyncio, "sleep", AsyncMock()):
            await queue.typing(bot, message, finished)
        self.assertEqual(count, 465)
