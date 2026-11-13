"""One durable group worker with bounded admission and independent Telegram dispatch."""
import asyncio
import json
import logging
import os

from telegram import Update
from . import psy_group_store as store

log = logging.getLogger("psy.group")


class DeliveryUncertain(Exception):
    pass


class Outbound:
    """Record entry to Telegram's non-idempotent send boundary without serializing a credential."""
    def __init__(self,msg,ident):
        self.msg, self.ident = msg, ident

    def __getattr__(self,key):
        return getattr(self.msg,key)

    async def reply_text(self,*args,**kwargs):
        await asyncio.to_thread(store.outbound,self.ident)
        try:
            return await self.msg.reply_text(*args,**kwargs)
        except Exception:
            raise DeliveryUncertain() from None

    async def reply_document(self,*args,**kwargs):
        await asyncio.to_thread(store.outbound,self.ident)
        try:
            return await self.msg.reply_document(*args,**kwargs)
        except Exception:
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
            if wake.is_set():
                continue
            return
        ident, body = item
        try:
            update=Update.de_json(json.loads(body),app.bot)
            finished = asyncio.Event()
            hint = asyncio.create_task(typing(app.bot, update.message, finished), name="psy-group-typing")
            try:
                await process_group(Outbound(update.message, ident))
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
