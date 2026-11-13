"""One durable group worker with bounded admission and independent Telegram dispatch."""
import asyncio
from datetime import timedelta
import hashlib
import io
import json
import logging
import os
import time
from types import SimpleNamespace

from telegram import Update
from . import psy_group_store as store

log = logging.getLogger("psy.group")


class DeliveryUncertain(Exception):
    pass


def retry_seconds(exc):
    from telegram.error import RetryAfter
    if not isinstance(exc, RetryAfter):
        return None
    delay = exc.retry_after
    return max(1.0, delay.total_seconds() if isinstance(delay, timedelta) else float(delay))


class Outbound:
    """Send each numbered part once, retrying only when Telegram explicitly refused it."""
    def __init__(self, msg, ident):
        self.msg, self.ident = msg, ident
        self.checkpoints = store.Checkpoints(ident)
        self.part = 0
        self._model = False

    def __getattr__(self, key):
        return getattr(self.msg, key)

    def model_delivery(self):
        # Model text and images may form several parts, and have post-send history bindings.
        self._model = True

    def error_delivery(self):
        if self.part:
            raise DeliveryUncertain()
        self._model = False

    def _destination(self):
        return [self.msg.chat.id, self.msg.from_user.id,
                self.msg.message_thread_id if self.msg.is_topic_message else None, self.msg.message_id]

    async def resume_saved_text(self):
        saved = await asyncio.to_thread(self.checkpoints.get, "outbound.single_text")
        if saved is None:
            return False
        await self._send_saved_text(saved)
        return True

    async def reply_text(self, text, **kwargs):
        options = dict(kwargs)
        markup = options.get("reply_markup")
        if markup is not None:
            options["reply_markup"] = markup.to_dict()
        saved = {"destination": self._destination(), "text": text, "options": options}
        if not self._model:
            # Commands like /files or an error reply can change after a restart.
            # Persist the exact one-part response before contacting Telegram.
            try:
                await asyncio.to_thread(self.checkpoints.put, "outbound.single_text", saved)
            except Exception:
                raise DeliveryUncertain() from None
        return await self._send_saved_text(saved)

    async def _send_saved_text(self, saved):
        if saved["destination"] != self._destination():
            raise DeliveryUncertain()
        options = dict(saved["options"])
        if options.get("reply_markup") is not None:
            from telegram import InlineKeyboardMarkup
            options["reply_markup"] = InlineKeyboardMarkup.de_json(options["reply_markup"], None)
        return await self._send("text", saved, lambda: self.msg.reply_text(saved["text"], **options))

    async def reply_document(self, document, **kwargs):
        # PTB can consume the stream before returning a 429. Reopen it from byte zero each time.
        buffered = isinstance(document, io.BytesIO)
        data = document.getvalue() if buffered else document
        fingerprint = {"sha256": hashlib.sha256(data).hexdigest(), "options": kwargs}
        def send():
            return self.msg.reply_document(document=io.BytesIO(data) if buffered else data, **kwargs)
        return await self._send("document", fingerprint, send)

    async def _send(self, kind, content, send):
        part = self.part
        self.part += 1
        envelope = [self._destination(), kind, content]
        fingerprint = hashlib.sha256(json.dumps(envelope, sort_keys=True, ensure_ascii=False,
                                                separators=(",", ":")).encode()).hexdigest()
        try:
            while True:
                action, value = await asyncio.to_thread(store.begin_delivery, self.ident, part, fingerprint)
                if action == "confirmed":
                    return SimpleNamespace(message_id=value)
                if action == "wait":
                    await asyncio.sleep(max(0, value - time.time()))
                    continue
                if action != "send":
                    raise DeliveryUncertain()
                try:
                    sent = await send()
                except Exception as exc:
                    delay = retry_seconds(exc)
                    if delay is None:
                        raise DeliveryUncertain() from None
                    await asyncio.to_thread(store.retry_delivery, self.ident, part, time.time() + delay)
                    continue
                if type(sent.message_id) is not int or sent.message_id <= 0:
                    raise DeliveryUncertain()
                await asyncio.to_thread(store.confirm_delivery, self.ident, part, sent.message_id)
                return sent
        except Exception:
            # A receipt write can fail after Telegram accepted an answer. The route must
            # never answer that failure with another message or retry an ambiguous send.
            raise DeliveryUncertain() from None


async def typing(bot, msg, finished):
    """Show "typing" in the group while a job runs. Idempotent: a failure never touches the job or its delivery state."""
    thread=msg.message_thread_id if msg.is_topic_message else None
    await asyncio.sleep(0.5)  # Fixed replies finish first and need no indicator.
    while not finished.is_set():
        try:
            await bot.send_chat_action(chat_id=msg.chat.id,action="typing",message_thread_id=thread,
                                       read_timeout=8,write_timeout=8,connect_timeout=8,pool_timeout=3)
        except Exception as exc:
            if getattr(exc,"retry_after",None) is not None:
                return  # Flood control: stop the indicator for this job, the answer matters more.
        if finished.is_set():
            return
        await asyncio.sleep(4.0)


async def drain(app, wake):
    while app.running:
        lease = await asyncio.to_thread(store.lease)
        if lease is not None:
            try:
                await asyncio.to_thread(store.recover)
                await work(app, wake)
            finally:
                os.close(lease)
            return
        await asyncio.sleep(1)


async def work(app, wake):
    from .psy_group_route import process_group
    while app.running:
        wake.clear()
        item = await asyncio.to_thread(store.claim_next)
        if not item:
            delay = await asyncio.to_thread(store.next_retry_delay)
            if wake.is_set():
                continue
            if delay is None:
                return
            try:
                await asyncio.wait_for(wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            continue
        ident, body = item
        try:
            update=Update.de_json(json.loads(body),app.bot)
            finished = asyncio.Event()
            hint = asyncio.create_task(typing(app.bot, update.message, finished), name="psy-group-typing")
            try:
                outbound = Outbound(update.message, ident)
                if not await outbound.resume_saved_text():
                    await process_group(outbound)
            finally:
                finished.set()
                hint.cancel()
                await asyncio.gather(hint, return_exceptions=True)
        except asyncio.CancelledError:
            # Startup restores pre-send work and quarantines possible duplicate deliveries.
            raise
        except Exception as exc:
            log.warning("group job stopped: %s", type(exc).__name__)
            await asyncio.to_thread(store.settle,ident,False)
        else:
            await asyncio.to_thread(store.settle,ident,True)


def schedule(app):
    adapter=app.adapter
    wake=getattr(adapter,"_psy_group_wake",None)
    if wake is None:
        wake=adapter._psy_group_wake=asyncio.Event()
    wake.set()
    task=getattr(adapter,"_psy_group_task",None)
    if task is None or task.done():
        adapter._psy_group_task=app.create_task(drain(app,wake),name="psy-group-queue")


async def start(app):
    schedule(app)


async def accept(app,update):
    msg=update.message
    if not msg.from_user or msg.from_user.is_bot or msg.sender_chat or msg.business_connection_id:
        return
    # Serialize only Telegram data delivered to the named group. Exclude unused forwarded
    # origin, external quotes, animations, service events and other unrelated metadata.
    raw=json.loads(update.to_json())["message"]
    fields={"message_id","date","chat","from","text","caption","photo","document","voice",
            "audio","video","video_note","message_thread_id","is_topic_message"}
    filtered={k:v for k,v in raw.items() if k in fields}
    quoted=raw.get("reply_to_message")
    if quoted and quoted.get("chat",{}).get("id")==msg.chat.id:
        filtered["reply_to_message"]={k:v for k,v in quoted.items() if k in fields}
    if not any(k in filtered for k in ("text","caption","photo","document","voice","audio","video","video_note")):
        return
    data=json.dumps({"update_id":update.update_id,"message":filtered},ensure_ascii=False)
    status=await asyncio.to_thread(store.pending,msg.from_user.id,update.update_id,data)
    if status=="full":
        await msg.reply_text("В очереди уже есть несколько запросов. Дождитесь ответа и повторите.")
    else:
        schedule(app)
