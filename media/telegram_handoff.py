"""Limited upload handoff; it never grants general DM or administration access."""
import importlib.machinery
import importlib.util
from pathlib import Path
import sys
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

UPLOAD = Path('/opt/psy-upload')


class _UploaderModules:
    """Resolves the uploader's own top-level imports from its directory, after every other finder."""
    def __init__(self, directory):
        self.directory = str(directory)

    def find_spec(self, name, path=None, target=None):
        if path is not None or '.' in name:
            return None
        return importlib.machinery.PathFinder.find_spec(name, [self.directory])


def load(directory=UPLOAD):
    """Load psy_upload_common by explicit path. This runs inside the gateway process, so the
    uploader directory never enters sys.path, where its modules could shadow gateway imports."""
    name = 'psy_upload_common'
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(directory)/(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    finder = _UploaderModules(directory)
    sys.meta_path.append(finder)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    finally:
        sys.meta_path.remove(finder)
    return module


C = load()


def allowed_source(msg):
    u=msg.from_user
    if not u or u.is_bot:return False
    return msg.chat.id==C.GROUP or (msg.chat.id==C.OWNER and u.id==C.OWNER)


async def present(msg):
    if not allowed_source(msg):return False
    C.initialize()
    t=C.ticket(msg.from_user.id,msg.chat.id,
        msg.message_thread_id if getattr(msg,'is_topic_message',False) else None,msg.message_id)
    if msg.chat.id==C.GROUP:
        button=InlineKeyboardButton('Загрузить запись',url=f'https://t.me/{C.BOT_USERNAME}?start=upload_{t}')
        text='Большую запись можно загрузить прямо на наш сервер. Кнопка откроет загрузку через личную переписку с PSY; расшифровка вернётся в эту группу. Выберите исходный файл на устройстве. Ссылка предназначена для отправителя записи.'
    else:
        button=InlineKeyboardButton('Загрузить запись',web_app=WebAppInfo(C.PUBLIC_URL+'/?ticket='+t))
        text='Загрузите аудио или видео до 1 ГБ. Выберите файл на устройстве и дождитесь окончания загрузки — затем окно можно закрыть. Расшифровку пришлю сюда.'
    await msg.reply_text(text,reply_markup=InlineKeyboardMarkup([[button]]))
    return True


async def command(msg):
    text=msg.text or ''
    pieces=text.strip().split()
    if not pieces:return False
    cmd=pieces[0].split('@',1)
    if len(cmd)>1 and cmd[1].lower()!=C.BOT_USERNAME.lower():return False
    if cmd[0].lower() in ('/audio','/upload'):
        if allowed_source(msg):await present(msg)
        return True
    if cmd[0].lower()!='/start' or len(pieces)!=2 or not pieces[1].startswith('upload_'):return False
    # A group participant gets only this specific handoff in DMs, never general agent access.
    if msg.chat.type!='private' or not msg.from_user or msg.from_user.is_bot:return True
    C.initialize()
    ident=pieces[1][7:]
    t=C.get_ticket(ident,msg.from_user.id)
    if not t:
        await msg.reply_text('Эта ссылка недоступна или устарела. Отправьте /audio в группе от своего имени.')
        return True
    if t['chat']==C.GROUP:
        try:
            member=await msg.get_bot().get_chat_member(C.GROUP,msg.from_user.id)
            if not C.member_allowed(member.to_dict()):return True
        except Exception:
            await msg.reply_text('Не удалось проверить доступ к группе. Повторите позже.')
            return True
    audience='в группу' if t['chat']==C.GROUP else 'в эту личную переписку'
    await msg.reply_text('Выберите запись до 1 ГБ и дождитесь окончания загрузки. После этого окно можно закрыть. Расшифровка будет отправлена '+audience+'.',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Загрузить запись',web_app=WebAppInfo(C.PUBLIC_URL+'/?ticket='+ident))]]))
    return True
