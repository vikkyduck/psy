"""Trust boundary before Hermes handlers, hooks and plugins. Group data never enters the owner agent."""
import asyncio
import base64
import io
import logging
import re
import time

from . import psy_group_store as store
from . import psy_group_audio as audio
from .psy_group_observability import capture_runtime, result_audit
from .psy_group_codex import ModelIssue, choose_source, research_requested, respond

from .psy_instance import BOT, BOT_USERNAME, GROUP, OWNER

MINI_LINK = f"https://t.me/{BOT_USERNAME}?startapp=psy_group"
UPLOAD_START = re.compile(r"/start(?:@" + re.escape(BOT_USERNAME) + r")? upload_[0-9a-f]{32}", re.I)
# Hermes' own /update stashes PSY's edits to the checkout and moves Hermes to another version; the
# boundary check then keeps the bot down until someone restores it by hand. PSY is updated from its
# repository with ops/update_live.py instead, so the command never reaches Hermes.
SELF_UPDATE = re.compile(r"/update(?:@[A-Za-z0-9_]+)?(?:\s.*)?", re.I | re.S)
SELF_UPDATE_REPLY = ("Команда /update отключена: обновление Hermes этой командой остановило бы бота. "
                     "ПСИ обновляется из репозитория через выкатку (ops/update_live.py).")
HELLO = ("В этой группе я отвечаю на сообщения, исследую публичные источники и разбираю записи. "
         "Ответьте на файл расшифровки, чтобы обсудить его. /audio открывает загрузку до 1 ГБ. "
         "/files показывает Ваши сохраненные в этой группе материалы. /get НОМЕР возвращает выбранный файл. "
         "/read НОМЕР ВОПРОС открывает его для разбора. /delete НОМЕР удаляет его из Ваших материалов. "
         "/research ВОПРОС запускает поиск по источникам. "
         "/image ОПИСАНИЕ создает изображение через Codex.")
MAX_TEXT = 8 * 1024**2
MAX_IMAGE = 5 * 1024**2
log = logging.getLogger("psy.group")


def audit_key(msg):
    ident = getattr(msg, "ident", None)
    if type(ident) is int:
        return "update:" + str(ident)
    return "message:" + ":".join(str(x) for x in (msg.chat.id, msg.from_user.id,
                           message_topic(msg), getattr(msg, "message_id", id(msg))))


def runtime_snapshot(model):
    from pathlib import Path
    with store.database() as con:
        vault = Path(con.execute("PRAGMA database_list").fetchone()[2]).parent
    return capture_runtime(model, root=vault / "runtime-snapshots")


async def committed_diagnostic(func, *args, **kwargs):
    # Keep the queue lease until a diagnostic/checkpoint transaction has finished,
    # including cancellation during a SQLite busy wait.
    operation = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        await asyncio.gather(operation, return_exceptions=True)
        raise


async def record_event(msg, event, details=None, **kwargs):
    await committed_diagnostic(store.audit_start, audit_key(msg), msg.from_user.id, message_topic(msg),
                               getattr(msg, "message_id", None))
    await committed_diagnostic(store.audit_event, audit_key(msg), event, details, **kwargs)


async def record_failure(msg, event, details=None, *, action=None, **kwargs):
    checkpoints = getattr(msg, "checkpoints", None)
    if action is not None and checkpoints is not None:
        # The exact safe cause survives a failure of the subsequent audit transaction.
        # A resumed job flushes this record before any new model or delivery action.
        await committed_diagnostic(checkpoints.put, "route:diagnostic:v1",
                                   {"event": event, "details": details, "options": kwargs, "action": action})
    await record_event(msg, event, details, **kwargs)
    if action is not None and checkpoints is not None:
        await committed_diagnostic(checkpoints.put, "route:diagnostic:v1", None)


async def finish_failure(msg, action):
    if action["type"] == "model":
        issue = ModelIssue(action["kind"], action["stage"], action.get("reset"))
        recovery = getattr(msg, "defer_model", None)
        if issue.retryable and recovery is not None:
            await recovery(issue)
            raise store.GroupJobParked()
        await error_reply(msg, issue.public())
    else:
        queued = getattr(msg, "defer_failure", None)
        if queued is not None and action["transient"]:
            await queued()
            raise store.GroupJobParked()
        safe = action.get("safe")
        await error_reply(msg, safe or "Не удалось завершить запрос. Попробуйте еще раз.")
        if queued is not None and safe is None:
            raise store.GroupJobFailed()


async def resume_diagnostic(msg):
    checkpoints = getattr(msg, "checkpoints", None)
    pending = await asyncio.to_thread(checkpoints.get, "route:diagnostic:v1") if checkpoints is not None else None
    if pending is None:
        return False
    await record_event(msg, pending["event"], pending["details"], **pending["options"])
    await committed_diagnostic(checkpoints.put, "route:diagnostic:v1", None)
    await finish_failure(msg, pending["action"])
    return True


def scope(update):
    chat = getattr(update, "effective_chat", None)
    user = getattr(update, "effective_user", None)
    chatid = getattr(chat, "id", None)
    kind = getattr(chat, "type", "")
    if kind in ("group", "supergroup") and chatid == GROUP:
        return "group"
    if any(getattr(update, field, None) is not None for field in (
        "business_message", "edited_business_message", "deleted_business_messages", "business_connection"
    )):
        return "deny"
    if kind == "private" and user and chatid == user.id and not user.is_bot:
        msg = getattr(update, "message", None)
        if msg and getattr(msg, "business_connection_id", None):
            return "deny"
        if chatid == OWNER:
            return "owner"
        if msg and UPLOAD_START.fullmatch(msg.text or ""):
            return "upload"
    return "deny"


def label(name):
    return re.sub(r"[^\w .()-]", "_", name or "Документ").strip(". ")[:120] or "Документ"


async def upload_button(msg):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    await msg.reply_text("Выберите аудио или видео до 1 ГБ. Расшифровка придет в группу.",
                         reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Записи", url=MINI_LINK)]]))


async def attachment(msg):
    """Use only a file present in a Telegram object from this exact group. Never interpret a chat file path."""
    if not msg or getattr(msg.chat, "id", None) != GROUP:
        return None
    doc = getattr(msg, "document", None)
    photos = getattr(msg, "photo", ())
    if photos:
        candidates = [p for p in photos if p.file_size and p.file_size <= MAX_IMAGE]
        if not candidates:
            raise ValueError("Фотография превышает 5 МБ.")
        item, kind, name, limit = candidates[-1], "jpeg", "Фото.jpg", MAX_IMAGE
    elif doc:
        name = label(doc.file_name)
        ext = name.rsplit(".", 1)[-1].casefold()
        if ext in ("txt", "md", "csv", "tsv", "srt", "vtt", "json"):
            item, kind, limit = doc, "text", MAX_TEXT
        elif ext in ("jpg", "jpeg", "png", "webp"):
            item, kind, limit = doc, "jpeg" if ext == "jpg" else ext, MAX_IMAGE
        else:
            return None
    else:
        return None
    if not (type(item.file_size) is int and 0 < item.file_size <= limit):
        if kind == "text":
            raise ValueError(TEXT_SIZE_ERROR)
        raise ValueError("Размер файла больше допустимого для разбора.")
    file = await item.get_file()
    if not isinstance(file.file_path, str) or not file.file_path.startswith("https://api.telegram.org/file/bot"):
        raise ValueError("Telegram не предоставил разрешенную ссылку на файл.")
    data = bytes(await file.download_as_bytearray())
    if not data or len(data) > limit:
        if kind == "text":
            raise ValueError(TEXT_SIZE_ERROR)
        raise ValueError("Размер полученного файла за пределами лимита.")
    if kind == "text":
        try:
            data.decode("utf-8-sig")
        except UnicodeError:
            raise ValueError("Для текстового файла нужна кодировка UTF-8.") from None
    return name, kind, data


def content(question, source, quote):
    text = question or "Разбери приложенный материал."
    if quote:
        text += "\n\nЦитата из этого чата:\n" + quote
    result = [{"type": "input_text", "text": text}]
    if source:
        name, kind, data = source
        if kind == "text":
            body = data.decode("utf-8-sig")
            result.append({"type":"input_text","text":"Материал: " + name + "\n" + body})
        else:
            result.append({"type":"input_image", "image_url": "data:image/" + kind + ";base64," + base64.b64encode(data).decode("ascii")})
    return result


async def deliver(msg, name, data):
    return await msg.reply_document(document=io.BytesIO(data), filename=name)


def message_topic(msg):
    return int(msg.message_thread_id or 0) if getattr(msg, "is_topic_message", False) else 0


def mentions_material(question):
    # The semantic selector handles free-form follow-ups whenever a source is active.
    # After a topic switch, only invite the library back when the user refers to a material.
    return bool(re.search(r"\b(?:расшифровк|стенограмм|транскрипт|приложенн|загруженн|файл|документ|лекци|запис[ьи]|transcript)\w*",
                          question, re.I))


def choice_help(options):
    rows = "\n".join(f'{o["id"]}. {o["name"]}' for o in options)
    return ("Уточните, какой материал разобрать. Ответьте на сообщение с исходным файлом "
            "или отправьте /read НОМЕР и Ваш вопрос." + ("\n" + rows if rows else " Номера доступны в /files."))


async def archive_source(uid, topic, msg, source):
    return await asyncio.to_thread(store.archive, uid, topic, *source, message_id=getattr(msg, "message_id", None))


async def quoted_material(uid, topic, quoted):
    """A referenced answer may expose only a source explicitly bound for this member."""
    mid = getattr(quoted, "message_id", None)
    saved = await asyncio.to_thread(store.message_source, uid, topic, mid)
    sender = getattr(quoted, "from_user", None)
    sender_id = getattr(sender, "id", None)
    bot_reply = sender_id == BOT
    quote = quoted.text or quoted.caption or ""
    if saved:
        doc_id, answer, turn_id = saved
        doc = await asyncio.to_thread(store.document, uid, topic, doc_id) if doc_id is not None else None
        # A long Telegram answer is a document with no text in its envelope.
        if bot_reply and answer:
            quote = answer
        elif sender_id == uid and answer:
            # A reply to the user's own original request can arrive while its analysis
            # is writing. The queue waits for the parent, so include its finished answer.
            quote = f"{quote}\n\nОтвет ПСИ на это сообщение:\n{answer}"
        return doc_id, doc, quote, turn_id if sender_id in (BOT, uid) else None
    if bot_reply:
        # Mini App delivery is an original group-visible transcript. Unknown bot answers,
        # exported cabinet documents and other members' reply IDs never rebuild a binding.
        name = getattr(getattr(quoted, "document", None), "file_name", None)
        recorded = re.fullmatch(r'Расшифровка записи "(.+)" готова\..*', quote, re.S)
        if name != "Расшифровка.txt" or not recorded:
            raise ValueError(UNLINKED_REPLY)
        source = await attachment(quoted)
        if source:
            source = ("Расшифровка " + label(recorded[1])[:100] + ".txt", source[1], source[2])
    else:
        source = await attachment(quoted)
    if source:
        doc_id = await archive_source(uid, topic, quoted, source)
        return doc_id, source, quote, None
    return None, None, quote, None


async def group_message(msg):
    user = msg.from_user
    if not user or user.is_bot or msg.sender_chat or msg.business_connection_id:
        return
    uid = user.id
    topic = message_topic(msg)
    mid = getattr(msg, "message_id", None)
    destination = [msg.chat.id, uid, topic, mid]
    checkpoints = getattr(msg, "checkpoints", None)
    if checkpoints is not None:
        job = await asyncio.to_thread(checkpoints.get, "route:input:v1")
        if job is not None:
            return await prepared_answer(msg, job, checkpoints)
    text = (msg.text or msg.caption or "").strip()
    command = ""
    param = text
    if text.startswith("/"):
        first, _, param = text.partition(" ")
        command, _, handle = first.lower().partition("@")
        if handle and handle != BOT_USERNAME.lower():
            return
    if command in ("/start", "/help", "/status"):
        await msg.reply_text(HELLO)
        return
    if command in ("/audio", "/upload"):
        await upload_button(msg)
        return
    if audio.metadata(msg):
        try:
            submitted = await asyncio.wait_for(audio.submit(msg), timeout=180)
        except Exception as exc:
            log.warning("group recording handoff unavailable: %s", type(exc).__name__)
            submitted = False
        if submitted:
            await msg.reply_text("Запись принята на расшифровку. Текст придет ответом в группу.")
        else:
            await upload_button(msg)
        return
    if command == "/files":
        rows = await asyncio.to_thread(store.listing, uid, topic)
        await msg.reply_text("\n".join(f"{ident}. {name}" for ident,name in rows)[:4000] or "У Вас пока нет сохраненных материалов в этой теме.")
        return
    if command == "/delete":
        numeral = param.partition(" ")[0]
        if not re.fullmatch("[1-9][0-9]{0,9}", numeral):
            await msg.reply_text("Укажите номер из /files.")
            return
        # A replayed job finds its own receipt instead of reporting the file as missing.
        receipt = getattr(msg, "ident", None)
        deleted = await asyncio.to_thread(store.forget, uid, topic, int(numeral),
                                          request_key=(receipt, "delete") if type(receipt) is int else None)
        await msg.reply_text(f"Материал {numeral} удален из Ваших материалов." if deleted else
                             "Материал с этим номером в Вашем кабинете не найден.")
        return
    doc_id = None
    source = None
    if command in ("/get", "/read"):
        numeral, _, param = param.partition(" ")
        if not re.fullmatch("[1-9][0-9]{0,9}", numeral):
            await msg.reply_text("Укажите номер из /files.")
            return
        doc_id = int(numeral)
        source = await asyncio.to_thread(store.document, uid, topic, doc_id)
        if not source:
            await msg.reply_text("Материал с этим номером в Вашем кабинете не найден.")
            return
        if command == "/get":
            sent = await deliver(msg, source[0], source[2])
            await asyncio.to_thread(store.bind_message, uid, topic, getattr(sent, "message_id", None), doc_id)
            return
    elif command and command not in ("/research", "/image", "/ask"):
        await msg.reply_text("Доступные в группе команды перечислены в /help.")
        return
    quoted = getattr(msg, "reply_to_message", None)
    if quoted and (quoted.chat.id != GROUP or message_topic(quoted) != topic):
        if source is None and not msg.document and not getattr(msg, "photo", ()):
            raise ValueError(UNLINKED_REPLY)
        quoted = None
    quote = ""
    quoted_turn = None
    if not source:
        source = await attachment(msg)
        if source:
            doc_id = await archive_source(uid, topic, msg, source)
    if msg.document and not source:
        # Recordings never get here: audio.metadata routed every audio or video document above.
        await msg.reply_text("Для разбора пришлите текст в TXT, Markdown, CSV или фотографию в JPEG, PNG, WebP.")
        return
    if quoted and not source:
        doc_id, source, quote, quoted_turn = await quoted_material(uid, topic, quoted)
    elif quoted and getattr(getattr(quoted, "from_user", None), "id", None) == uid:
        quote = quoted.text or quoted.caption or ""
    if not text and not source:
        return
    question = param if command else text
    from hermes_cli.config import load_config
    # Hermes re-reads config.yaml when it changes. Keep that file I/O and YAML parsing off the loop.
    model = (await asyncio.to_thread(load_config))["model"]
    if model["provider"] != "openai-codex":
        raise RuntimeError("wrong provider")
    if not source and not quoted:
        active_id, options = await asyncio.to_thread(store.choices, uid, topic, mentions_material(question),
                                                     before_message_id=mid)
        if options:
            selection_audit = {}
            # Failed selections carry their client audit in ModelIssue. Do not let
            # a separate diagnostic write mask the original model failure.
            selected = await asyncio.to_thread(choose_source, question, options, active_id,
                                               model=model["default"], checkpoints=checkpoints,
                                               audit_out=selection_audit)
            await record_event(msg, "source_selection", selection_audit)
            if selected == "clarify" or (selected is not None and
                    (type(selected) is not int or selected not in {item["id"] for item in options})):
                await record_event(msg, "clarification_requested", {"candidate_count": len(options)},
                                   state="needs_clarification")
                await msg.reply_text(choice_help(options))
                return
            if selected is not None:
                doc_id = selected
                source = await asyncio.to_thread(store.document, uid, topic, doc_id)
                if source is None:
                    await msg.reply_text(choice_help(options))
                    return
    await asyncio.to_thread(store.set_current, uid, topic, doc_id, mid)
    history = await asyncio.to_thread(store.history, uid, topic, source_id=doc_id, exclude_turn=quoted_turn,
                                      before_message_id=mid)
    research = command == "/research" or research_requested(question)
    draw = command == "/image" or bool(re.search(r"(?:нарисуй|сгенерируй\s+(?:изображение|картин\w*)|создай\s+(?:изображение|картин\w*))", question, re.I))
    job = dict(destination=destination, question=question, doc_id=doc_id, quote=quote, history=history,
               model=model["default"], research=research and not draw, draw=draw)
    job["context_scope"] = {"uid": uid, "topic": topic, "source_id": doc_id}
    job["context_state"] = await asyncio.to_thread(store.get_context, uid, topic, doc_id,
                                                  before_message_id=mid)
    job["runtime"] = await asyncio.to_thread(runtime_snapshot, model["default"])
    if checkpoints is not None:
        await asyncio.to_thread(checkpoints.put, "route:input:v1", job)
    await prepared_answer(msg, job, checkpoints, source=source)


async def prepared_answer(msg, job, checkpoints, *, source=None):
    # Bind recovery to the original member, topic and incoming message. Read source files
    # by their private database IDs rather than copying attachments into checkpoints.
    uid = msg.from_user.id
    topic = message_topic(msg)
    mid = getattr(msg, "message_id", None)
    if job["destination"] != [msg.chat.id, uid, topic, mid]:
        raise RuntimeError("checkpoint destination mismatch")
    doc_id = job["doc_id"]
    question = job["question"]
    runtime = await asyncio.to_thread(runtime_snapshot, job["model"])
    await record_event(msg, "execution", {"runtime": runtime,
                       "route": "group_image" if job["draw"] else "group_text"}, state="running")
    if checkpoints is not None:
        msg.model_delivery()
        final = await asyncio.to_thread(checkpoints.get, "route:answer:v1")
    else:
        final = None
    if final is None:
        if doc_id is not None and source is None:
            source = await asyncio.to_thread(store.document, uid, topic, doc_id)
            if source is None:
                raise ValueError(UNLINKED_REPLY)
        sessions = [*job["history"], {"role": "user", "content": content(question, source, job["quote"])}]
        options = {"checkpoints": checkpoints} if checkpoints is not None else {}
        options.update(context_state=job.get("context_state"), context_scope=job.get("context_scope",
                       {"uid": uid, "topic": topic, "source_id": doc_id}))
        generation = asyncio.create_task(asyncio.to_thread(
            respond, sessions, research=job["research"], draw=job["draw"],
            model=job["model"], question=question, **options))
        try:
            result = await asyncio.shield(generation)
        except asyncio.CancelledError:
            # A cancelled asyncio waiter cannot stop a thread writing checkpoints.
            # Hold the worker lease until it finishes, then exit before delivery.
            await asyncio.gather(generation, return_exceptions=True)
            raise
        text, images = result.text, result.images
        metadata = dict(getattr(result, "quality_metadata", {}))
        context_state = metadata.pop("context_state", None)
        await asyncio.to_thread(store.save_context, uid, topic, doc_id, context_state, message_id=mid)
        metadata.update(runtime=runtime, stop_reason=getattr(result, "stop_reason", "unknown"),
                        stop_stage=getattr(result, "stop_stage", None))
        generation = metadata.get("generation_audit", metadata.get("generation", {}))
        api_complete = generation.get("generation_complete")
        details = {
            "task_status": getattr(result, "task_status", "unknown"),
            "generation_complete": api_complete if type(api_complete) is bool else None,
            "missing_requirements": getattr(result, "missing_requirements", []),
            "quality_metadata": {"model_requested": job["model"],
                                 **metadata},
        }
        if checkpoints is not None:
            final = {"text": text, "images": [base64.b64encode(img).decode("ascii") for img in images],
                     "result_details": details}
            await asyncio.to_thread(checkpoints.put, "route:answer:v1", final)
    else:
        text = final["text"]
        images = [base64.b64decode(encoded, validate=True) for encoded in final["images"]]
        # A legacy checkpoint has no trustworthy completion or generation metadata.
        # Its bytes remain unchanged so already accepted delivery fingerprints still match.
        details = final.get("result_details", {"task_status": "unknown", "generation_complete": None,
                                              "missing_requirements": [], "quality_metadata": {}})

    def receipt(part):
        return {"request_key": (msg.ident, part)} if checkpoints is not None else {}

    turn_id = await asyncio.to_thread(store.remember, uid, topic, question, text, source_id=doc_id, message_id=mid,
                                     result_details=details, **receipt("turn"))
    await record_event(msg, "result_saved", result_audit(details), state="sending", turn_id=turn_id)
    async def archive_answer(name, kind, data, key):
        try:
            await asyncio.to_thread(store.archive, uid, topic, name, kind, data, **receipt(key))
        except store.ArchiveFull:
            # The durable answer and per-part receipts still carry the exact payload.
            # A nearly full disk must not discard work that already finished.
            log.info("group answer not archived: disk reserve reached")

    confirmed = bool(text or images)
    if text:
        if len(text) <= 4000:
            sent = await msg.reply_text(text, disable_web_page_preview=True)
        else:
            name = "Ответ PSY.txt"
            data = text.encode()
            await archive_answer(name, "text", data, "text")
            sent = await deliver(msg, name, data)
        await asyncio.to_thread(store.bind_message, uid, topic, getattr(sent, "message_id", None), doc_id, turn_id)
        confirmed &= type(getattr(sent, "message_id", None)) is int and sent.message_id > 0
    for index, img in enumerate(images):
        name = "Изображение PSY.png"
        await archive_answer(name, "png", img, f"image:{index}")
        sent = await deliver(msg, name, img)
        await asyncio.to_thread(store.bind_message, uid, topic, getattr(sent, "message_id", None), doc_id, turn_id)
        confirmed &= type(getattr(sent, "message_id", None)) is int and sent.message_id > 0

    # All payloads need a receipt. Failure leaves pending even when earlier parts arrived.
    if confirmed:
        await asyncio.to_thread(store.confirm_result_delivery, uid, topic, turn_id)
        await record_event(msg, "delivery_confirmed", {"task_status": details["task_status"],
                           "parts": int(bool(text)) + len(images)}, state="finished", turn_id=turn_id)
    else:
        await record_event(msg, "delivery_pending", state="pending", turn_id=turn_id)


async def intercept(app, update):
    from .psy_owner_recovery import handle as owner_recovery
    if await owner_recovery(app, update):
        return True
    route = scope(update)
    msg = getattr(update, "message", None)
    if route == "owner":
        if msg and SELF_UPDATE.fullmatch((getattr(msg, "text", None) or "").strip()):
            await msg.reply_text(SELF_UPDATE_REPLY)
            return True
        return False
    if route == "upload" and msg:
        from plugins.platforms.telegram.psy_upload_handoff import command
        await command(msg)
    if route == "group" and msg:
        if app is None:  # Also used by offline acceptance tests.
            await process_group(msg)
        else:
            from .psy_group_queue import accept
            await accept(app, update)
    return True


async def error_reply(msg, text):
    mode = getattr(msg, "error_delivery", None)
    if mode is not None:
        mode()
    sent = await msg.reply_text(text)
    await record_event(msg, "error_delivery", {"confirmed": type(getattr(sent, "message_id", None)) is int
                       and sent.message_id > 0})


async def process_group(msg):
    user = getattr(msg, "from_user", None)
    if user is None:
        return
    # Replay a safely recorded cause before starting fresh work. Storage failures here
    # escape to the durable queue instead of replacing the original diagnostic.
    if await resume_diagnostic(msg):
        return
    start = time.monotonic()
    try:
        baseline = await asyncio.to_thread(runtime_snapshot, "see-profile-config.yaml")
        await record_event(msg, "started", {"runtime": baseline, "route": "group"}, state="running")
        await group_message(msg)
        await record_event(msg, "action_finished")
        log.info("group action finished elapsed=%.1fs", time.monotonic() - start)
    except (store.GroupJobParked, store.DeliveryUncertain) as exc:
        # A timed-out Telegram send may already have reached the group.
        await record_failure(msg, "parked" if isinstance(exc, store.GroupJobParked) else "delivery_uncertain",
                           state="waiting" if isinstance(exc, store.GroupJobParked) else "uncertain")
        raise
    except ModelIssue as exc:
        log.warning("group model issue kind=%s stage=%s elapsed=%.1fs", exc.kind, exc.stage, time.monotonic() - start)
        recovery = getattr(msg, "defer_model", None)
        await record_failure(msg, "model_failure", {"kind": exc.kind, "stage": exc.stage,
                           "stop_reason": getattr(exc, "stop_reason", exc.kind),
                           "retryable": exc.retryable,
                           "audit": result_audit({"quality_metadata": getattr(exc, "quality_metadata", {})})},
                           state="waiting" if exc.retryable and recovery is not None else "failed",
                           action={"type": "model", "kind": exc.kind, "stage": exc.stage, "reset": exc.reset})
        await finish_failure(msg, {"type": "model", "kind": exc.kind, "stage": exc.stage, "reset": exc.reset})
    except Exception as exc:
        tb = exc.__traceback__
        while tb and tb.tb_next:
            tb = tb.tb_next
        # Include only code coordinates. Never include arguments, exception text or member data.
        fn = tb.tb_frame.f_code.co_name if tb else "unknown"
        log.warning("group request failed class=%s function=%s line=%d elapsed=%.1fs",
                    type(exc).__name__, fn, tb.tb_lineno if tb else 0, time.monotonic() - start)
        queued = getattr(msg, "defer_failure", None)
        safe = str(exc) if isinstance(exc, ValueError) and str(exc) in SAFE_ERRORS else None
        action = {"type": "execution", "transient": store.transient(exc), "safe": safe}
        await record_failure(msg, "execution_failure", {"class": type(exc).__name__,
                           "function": fn, "line": tb.tb_lineno if tb else 0},
                           state="waiting" if queued is not None and store.transient(exc) else "failed",
                           action=action)
        await finish_failure(msg, action)


UNLINKED_REPLY = ("В этой теме у Вас нет привязки этого сообщения к исходному материалу. "
                  "Ответьте на исходный файл или выберите свой материал командой /read НОМЕР из /files.")
TEXT_SIZE_ERROR = ("Для разбора нужен непустой UTF-8 файл до 8 МиБ. "
                   "Если расшифровка больше, разделите ее на части и укажите нужную для вопроса.")

SAFE_ERRORS = {
    UNLINKED_REPLY, TEXT_SIZE_ERROR,
    "В хранилище недостаточно места для нового файла.",
    "Фотография превышает 5 МБ.", "Размер файла больше допустимого для разбора.",
    "Telegram не предоставил разрешенную ссылку на файл.", "Размер полученного файла за пределами лимита.",
    "Для текстового файла нужна кодировка UTF-8.",
}
