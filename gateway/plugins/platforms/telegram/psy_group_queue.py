"""Run independent Telegram jobs concurrently under one durable queue lease."""
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
MAX_ACTIVE_JOBS = 3
TICK_SECONDS = 30
RETRY_START = 1
RESTART_START = 1


class DeliveryUncertain(Exception):
    pass


async def committed_call(func, *args, **kwargs):
    # Stop may cancel the awaiting coroutine while SQLite is still committing.
    # Keep this drain's lease until the short transaction has really finished.
    operation = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        await asyncio.gather(operation, return_exceptions=True)
        raise


def retry_seconds(exc):
    from telegram.error import RetryAfter
    if not isinstance(exc, RetryAfter):
        return None
    delay = exc.retry_after
    return max(1.0, delay.total_seconds() if isinstance(delay, timedelta) else float(delay))


class _ScheduledCheckpoints(store.Checkpoints):
    def __init__(self, ident, wake):
        super().__init__(ident)
        self.loop = asyncio.get_running_loop()
        self.wake = wake

    def put(self, key, value):
        super().put(key, value)
        if key == "route:input:v1":
            # The next question from this member can now read the chosen source.
            # Stage writes run in worker threads, while wake belongs to the event loop.
            self.loop.call_soon_threadsafe(self.wake.set)


class Outbound:
    """Send each numbered part once, retrying only when Telegram explicitly refused it."""
    def __init__(self, msg, ident, wake=None):
        self.msg, self.ident = msg, ident
        self.checkpoints = store.Checkpoints(ident) if wake is None else _ScheduledCheckpoints(ident, wake)
        self.scheduled = wake is not None
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

    async def defer_model(self, issue):
        if self.part:
            raise DeliveryUncertain()
        notify = await committed_call(store.wait_for_model, self.ident, issue.kind, issue.reset)
        if notify:
            # A notice is informational. Reserve it in the same transaction as the
            # retry and attempt it at most once, even if Telegram loses its receipt.
            text = ("Запрос сохранен. PSY продолжит его автоматически после восстановления доступа к Codex."
                    if issue.kind in ("signin", "quota", "access_wait") else
                    "Запрос сохранен. PSY автоматически повторит обращение к модели.")
            try:
                await self.msg.reply_text(text)
            except Exception as exc:
                log.info("group retry notice unavailable: %s", type(exc).__name__)

    async def defer_failure(self):
        """Requeue after a storage or network failure. Saved stages and delivered parts stay valid."""
        notify = await committed_call(store.wait_for_model, self.ident, "infrastructure", None)
        if notify and not self.part:
            try:
                await self.msg.reply_text("Запрос сохранен. PSY продолжит его автоматически.")
            except Exception as exc:
                log.info("group retry notice unavailable: %s", type(exc).__name__)

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
                await committed_call(self.checkpoints.put, "outbound.single_text", saved)
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
                action, value = await committed_call(store.begin_delivery, self.ident, part, fingerprint)
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
                    if self.scheduled:
                        await committed_call(store.retry_delivery, self.ident, part, time.time() + delay, park=True)
                        raise store.GroupJobParked()
                    await committed_call(store.retry_delivery, self.ident, part, time.time() + delay)
                    continue
                if type(sent.message_id) is not int or sent.message_id <= 0:
                    raise DeliveryUncertain()
                await committed_call(store.confirm_delivery, self.ident, part, sent.message_id)
                return sent
        except store.GroupJobParked:
            raise
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
    """Supervise the queue: a scheduler failure restarts it with backoff, never silently stops it."""
    delay = RESTART_START
    while app.running:
        try:
            lease = store.lease()  # LOCK_NB on a local file, acquired and closed by this task.
        except OSError as exc:
            lease = None
            log.warning("group queue lease unavailable (%s)", type(exc).__name__)
        if lease is None:
            await asyncio.sleep(1)
            continue
        try:
            await ensure_vault(app.bot)
            await committed_call(store.recover)
            await work(app, wake)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("group queue stopped (%s); restarting in %ss", type(exc).__name__, delay)
        finally:
            os.close(lease)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 300)


def group_sender(msg):
    from .psy_group_route import GROUP
    return (msg is not None and msg.chat.id == GROUP and msg.chat.type in ("group", "supergroup")
            and msg.from_user is not None and not msg.from_user.is_bot
            and msg.sender_chat is None and msg.business_connection_id is None)


async def ensure_vault(bot):
    from .psy_group_route import BOT, GROUP
    if bot.id != BOT:
        raise RuntimeError("Connected bot identity mismatch")
    await committed_call(store.bind_instance, BOT, GROUP)


async def durable(app, func, *args, **kwargs):
    """Commit a queue decision, waiting out storage failures instead of losing the job."""
    delay = RETRY_START
    while True:
        try:
            return await committed_call(func, *args, **kwargs)
        except Exception as exc:
            # On shutdown the job stays running in SQLite and startup recovery requeues it.
            if not store.transient(exc) or not app.running:
                raise
            log.warning("group queue write failed (%s); retrying in %ss", type(exc).__name__, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)


async def run_job(app, wake, ident, body):
    from .psy_group_route import process_group
    try:
        update = Update.de_json(json.loads(body), app.bot)
        from .psy_group_route import BOT
        if app.bot.id != BOT or not group_sender(update.message) or update.update_id != ident:
            raise ValueError("Stored group update is outside the configured scope")
        if not await asyncio.to_thread(store.owns_running_job, ident, update.message.from_user.id):
            raise ValueError("Stored group sender mismatch")
        finished = asyncio.Event()
        hint = asyncio.create_task(typing(app.bot, update.message, finished), name="psy-group-typing")
        try:
            outbound = Outbound(update.message, ident, wake)
            if not await outbound.resume_saved_text():
                await process_group(outbound)
        finally:
            finished.set()
            hint.cancel()
            await asyncio.gather(hint, return_exceptions=True)
    except store.GroupJobParked:
        # Its checkpoints and queued state are already committed for the next slot.
        return
    except asyncio.CancelledError:
        # Startup restores pre-send work and quarantines possible duplicate deliveries.
        raise
    except store.GroupJobFailed:
        await durable(app, store.fail, ident)
    except DeliveryUncertain:
        await durable(app, store.settle, ident, False)
    except Exception as exc:
        if store.transient(exc):
            # Even recording the retry failed inside the job. Keep every checkpoint.
            log.warning("group job deferred after %s", type(exc).__name__)
            await durable(app, store.wait_for_model, ident, "infrastructure", None)
            return
        log.warning("group job stopped: %s", type(exc).__name__)
        await durable(app, store.settle, ident, False)
    else:
        # A real answer or a known user-facing error reached the member.
        await durable(app, store.settle, ident, True)


async def _reap(app, task, ident):
    if task.cancelled() or task.exception() is None:
        return
    # run_job contains job failures. Reaching this point means its own bookkeeping failed;
    # treat the job like one interrupted by a restart, but in this process and with a delay.
    log.warning("group job task ended with %s", type(task.exception()).__name__)
    await durable(app, store.interrupt, ident)


async def work(app, wake):
    active = {}
    delay_after_failure = RETRY_START
    try:
        while app.running or active:
            wake.clear()
            for task, ident in tuple(active.items()):
                if task.done():
                    del active[task]
                    await _reap(app, task, ident)
            # Use the same clock edge for both queries: a last 429 can become due
            # between an empty claim and the read of its timer.
            now = time.time()
            delay = None
            try:
                while app.running and len(active) < MAX_ACTIVE_JOBS:
                    item = await committed_call(store.claim_next, now=now)
                    if item is None:
                        break
                    task = asyncio.create_task(run_job(app, wake, *item), name="psy-group-job")
                    task.add_done_callback(lambda _: wake.set())
                    active[task] = item[0]
                # A new update, completed job or committed source selection can unblock a
                # different member/question. A durable Telegram retry also has its own due time.
                if app.running and len(active) < MAX_ACTIVE_JOBS:
                    delay = await asyncio.to_thread(store.next_retry_delay, now=now)
                delay_after_failure = RETRY_START
            except Exception as exc:
                if not store.transient(exc):
                    raise
                # Running jobs continue. Look at the queue again after a short pause.
                log.warning("group queue check failed (%s); retrying in %ss", type(exc).__name__, delay_after_failure)
                delay = delay_after_failure
                delay_after_failure = min(delay_after_failure * 2, 60)
            if wake.is_set():
                continue
            if delay is None:
                if not active:
                    return
                await wake.wait()
            else:
                try:
                    await asyncio.wait_for(wake.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
    finally:
        # No child may deliver after the process-wide lease is handed to another drain.
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)


def schedule(app):
    adapter=app.adapter
    wake=getattr(adapter,"_psy_group_wake",None)
    if wake is None:
        wake=adapter._psy_group_wake=asyncio.Event()
    wake.set()
    task=getattr(adapter,"_psy_group_task",None)
    if task is None or task.done():
        adapter._psy_group_task=app.create_task(drain(app,wake),name="psy-group-queue")


async def check(app):
    """Restart or wake the drain when a queued job is runnable, e.g. a retry timer became due."""
    adapter = app.adapter
    task = getattr(adapter, "_psy_group_task", None)
    idle = task is None or task.done()
    if await asyncio.to_thread(store.runnable) or (idle and await asyncio.to_thread(store.has_running)):
        schedule(app)


async def watch(app):
    # A drain sleeps until its next timer and ends when nothing is runnable. This light
    # check covers a drain that stopped or missed a wake, without new group messages.
    while app.running:
        await asyncio.sleep(TICK_SECONDS)
        try:
            await check(app)
        except Exception as exc:
            log.warning("group queue check failed (%s)", type(exc).__name__)


async def start(app):
    from .psy_owner_recovery import start as start_owner_recovery
    schedule(app)
    start_owner_recovery(app)
    ticker = getattr(app.adapter, "_psy_group_ticker", None)
    if ticker is None or ticker.done():
        app.adapter._psy_group_ticker = asyncio.create_task(watch(app), name="psy-group-tick")


async def resume_authorized(app, incident_id):
    """Called by the owner service after this configured group model accepted the new login."""
    await ensure_vault(app.bot)
    await committed_call(store.release_auth_waits, incident_id)
    # A repeated call after a crash must also wake an existing sleeping drain.
    schedule(app)


async def accept(app,update):
    msg=update.message
    if not group_sender(msg):
        return
    await ensure_vault(app.bot)
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
