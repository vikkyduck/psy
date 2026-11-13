"""One bounded durable group worker. PTB can immediately dispatch the owner's next update."""
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
            await asyncio.wait_for(process_group(Outbound(update.message,ident)),timeout=850)
        except TimeoutError:
            if await asyncio.to_thread(store.state,ident) == "running":
                try:
                    await Outbound(update.message,ident).reply_text("Запрос занял слишком много времени. Повторите его с более узкой темой.")
                except Exception:
                    await asyncio.to_thread(store.settle,ident,False)
                else:
                    await asyncio.to_thread(store.settle,ident,True)
            else:
                await asyncio.to_thread(store.settle,ident,False)
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
