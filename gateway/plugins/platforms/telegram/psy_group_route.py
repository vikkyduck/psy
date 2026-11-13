"""Trust boundary before Hermes handlers, hooks and plugins. Group data never enters the owner agent."""
import asyncio
import base64
import io
import logging
import re
import time

from . import psy_group_store as store
from . import psy_group_audio as audio
from .psy_group_codex import ModelIssue, choose_source, research_requested, respond

from .psy_instance import BOT, BOT_USERNAME, GROUP, OWNER

MINI_LINK = f"https://t.me/{BOT_USERNAME}?startapp=psy_group"
UPLOAD_START = re.compile(r"/start(?:@" + re.escape(BOT_USERNAME) + r")? upload_[0-9a-f]{32}", re.I)
HELLO = ("В этой группе я отвечаю на сообщения, исследую публичные источники и разбираю записи. "
         "Ответьте на файл расшифровки, чтобы обсудить его. /audio открывает загрузку до 1 ГБ. "
         "/files показывает Ваши сохраненные в этой группе материалы. /get НОМЕР возвращает выбранный файл. "
         "/read НОМЕР ВОПРОС открывает его для разбора. /research ВОПРОС запускает поиск по источникам. "
         "/image ОПИСАНИЕ создает изображение через Codex.")
MAX_TEXT = 8 * 1024**2
MAX_IMAGE = 5 * 1024**2
log = logging.getLogger("psy.group")


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
    bot_reply = getattr(sender, "id", None) == BOT
    quote = quoted.text or quoted.caption or ""
    if saved:
        doc_id, answer, turn_id = saved
        doc = await asyncio.to_thread(store.document, uid, topic, doc_id) if doc_id is not None else None
        # A long Telegram answer is a document with no text in its envelope.
        if bot_reply and answer:
            quote = answer
        return doc_id, doc, quote, turn_id if bot_reply else None
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
        ext = (msg.document.file_name or "").lower().rsplit(".",1)[-1]
        if ext in ("m4a","mp3","wav","mp4","flac","ogg","mov","mkv","opus","aac","webm"):
            await upload_button(msg)
        else:
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
    model = load_config()["model"]
    if model["provider"] != "openai-codex":
        raise RuntimeError("wrong provider")
    if not source and not quoted:
        active_id, options = await asyncio.to_thread(store.choices, uid, topic, mentions_material(question))
        if options:
            selected = await asyncio.to_thread(choose_source, question, options, active_id, model=model["default"])
            if selected == "clarify" or (selected is not None and
                    (type(selected) is not int or selected not in {item["id"] for item in options})):
                await msg.reply_text(choice_help(options))
                return
            if selected is not None:
                doc_id = selected
                source = await asyncio.to_thread(store.document, uid, topic, doc_id)
                if source is None:
                    await msg.reply_text(choice_help(options))
                    return
    mid = getattr(msg, "message_id", None)
    await asyncio.to_thread(store.set_current, uid, topic, doc_id, mid)
    query = content(question, source, quote)
    sessions = await asyncio.to_thread(store.history, uid, topic, source_id=doc_id, exclude_turn=quoted_turn)
    sessions.append({"role":"user","content":query})
    research = command == "/research" or research_requested(question)
    draw = command == "/image" or bool(re.search(r"(?:нарисуй|сгенерируй\s+(?:изображение|картин\w*)|создай\s+(?:изображение|картин\w*))", question, re.I))
    result = await asyncio.to_thread(respond, sessions, research=research and not draw, draw=draw, model=model["default"],
                                     question=question)
    turn_id = await asyncio.to_thread(store.remember, uid, topic, question, result.text, source_id=doc_id, message_id=mid)
    if result.text:
        if len(result.text) <= 4000:
            sent = await msg.reply_text(result.text, disable_web_page_preview=True)
        else:
            name = "Ответ PSY.txt"
            data = result.text.encode()
            await asyncio.to_thread(store.archive, uid, topic, name, "text", data)
            sent = await deliver(msg, name, data)
        await asyncio.to_thread(store.bind_message, uid, topic, getattr(sent, "message_id", None), doc_id, turn_id)
    for img in result.images:
        name = "Изображение PSY.png"
        await asyncio.to_thread(store.archive, uid, topic, name, "png", img)
        sent = await deliver(msg, name, img)
        await asyncio.to_thread(store.bind_message, uid, topic, getattr(sent, "message_id", None), doc_id, turn_id)


async def intercept(app, update):
    route = scope(update)
    if route == "owner":
        return False
    msg = getattr(update, "message", None)
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


async def process_group(msg):
    start = time.monotonic()
    try:
        await group_message(msg)
        log.info("group action finished elapsed=%.1fs", time.monotonic() - start)
    except ModelIssue as exc:
        log.warning("group model issue kind=%s stage=%s elapsed=%.1fs", exc.kind, exc.stage, time.monotonic() - start)
        await msg.reply_text(exc.public())
    except Exception as exc:
        # A timed-out Telegram send may already have reached the group.
        if type(exc).__name__ == "DeliveryUncertain":
            raise
        tb = exc.__traceback__
        while tb and tb.tb_next:
            tb = tb.tb_next
        # Include only code coordinates. Never include arguments, exception text or member data.
        fn = tb.tb_frame.f_code.co_name if tb else "unknown"
        log.warning("group request failed class=%s function=%s line=%d elapsed=%.1fs",
                    type(exc).__name__, fn, tb.tb_lineno if tb else 0, time.monotonic() - start)
        safe = str(exc) if type(exc) is ValueError and str(exc) in SAFE_ERRORS else None
        await msg.reply_text(safe or "Не удалось завершить запрос. Попробуйте еще раз.")


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
