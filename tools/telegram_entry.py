"""Check or install one owner menu and one pinned group launcher using the instance configuration."""
import argparse
import fcntl
import getpass
import importlib.util
import json
import os
from pathlib import Path
import sys


def _common(default="/usr/local/lib/psy-common/psy_common.py"):
    """The shared PSY helpers, loaded by explicit path and never through sys.path: `default`, the
    root-owned installed copy unless a release tool passes the one shipped with it, or in tests
    the copy PSY_COMMON_PATH names."""
    module = sys.modules.get("psy_common")
    if module is None:
        path = str(os.environ.get("PSY_COMMON_PATH") or default)
        spec = importlib.util.spec_from_file_location("psy_common", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module = sys.modules.setdefault("psy_common", module)
    return module


# This tool runs from a release checkout: it uses the shared module shipped with it, and so does
# the uploader settings module below, which finds it already loaded.
psy_common = _common(Path(__file__).resolve().parents[1] / "common/psy_common.py")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "media"))
import psy_upload_common as cfg


def save(state_path, value):
    # The parent is a private operator-owned directory. Keep the intent if
    # Telegram accepts a send but its reply cannot be received.
    psy_common.atomic_write(state_path, json.dumps(value).encode())


def entries(call, install, folder):
    me = call("getMe", {})
    if me["id"] != cfg.BOT_ID or me["username"].lower() != cfg.BOT_USERNAME.lower():
        raise ValueError("Bot identity does not match the instance configuration")
    if not me.get("has_main_web_app"):
        raise ValueError("Configure this bot's Main Mini App in BotFather first")
    rights = call("getChatMember", {"chat_id": cfg.GROUP, "user_id": cfg.BOT_ID})
    chat = call("getChat", {"chat_id": cfg.GROUP})
    if chat.get("type") != "supergroup" or rights.get("status") != "administrator":
        raise ValueError("Verify bot membership and group administration rights")
    menu = {"type": "web_app", "text": "Записи", "web_app": {"url": cfg.PUBLIC_URL + "/"}}
    menu_ready = call("getChatMenuButton", {"chat_id": cfg.OWNER}) == menu
    link = f"https://t.me/{cfg.BOT_USERNAME}?startapp=psy_group"

    def matches(message):
        rows = (message.get("reply_markup") or {}).get("inline_keyboard") or []
        return any(b.get("text") == "Записи" and b.get("url") == link for row in rows for b in row)

    pinned = matches(chat.get("pinned_message") or {})
    if install and not menu_ready:
        call("setChatMenuButton", {"chat_id": cfg.OWNER, "menu_button": menu})
        menu_ready = call("getChatMenuButton", {"chat_id": cfg.OWNER}) == menu
    if install and not pinned:
        if not rights.get("can_pin_messages"):
            raise ValueError("Bot requires permission to pin its group launcher")
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (folder / "group-entry.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = folder / "group-entry.json"
            if path.exists():
                record = json.loads(path.read_text())
                if record.get("chat") != cfg.GROUP or record.get("bot") != cfg.BOT_ID or not record.get("message_id"):
                    raise ValueError("Review the prior delivery in Telegram before creating another launcher")
                # A renamed bot keeps its numeric ID, but its old group link has changed.
                if record.get("link") and record["link"] != link:
                    call("editMessageReplyMarkup", {
                        "chat_id": cfg.GROUP, "message_id": record["message_id"],
                        "reply_markup": {"inline_keyboard": [[{"text": "Записи", "url": link}]]},
                    })
                    record["link"] = link
                    save(path, record)
            else:
                record = {"chat": cfg.GROUP, "bot": cfg.BOT_ID, "link": link}
                save(path, record)
                message = call("sendMessage", {
                    "chat_id": cfg.GROUP, "text": "Загрузите запись до 1 ГиБ. Расшифровка придет в эту группу.",
                    "disable_notification": True,
                    "reply_markup": {"inline_keyboard": [[{"text": "Записи", "url": link}]]},
                })
                if message["chat"]["id"] != cfg.GROUP or not matches(message):
                    raise ValueError("Unexpected Telegram message destination or launcher")
                record["message_id"] = message["message_id"]
                save(path, record)
            call("pinChatMessage", {"chat_id": cfg.GROUP, "message_id": record["message_id"], "disable_notification": True})
            pinned = matches(call("getChat", {"chat_id": cfg.GROUP}).get("pinned_message") or {})
    print(f"owner_menu={menu_ready} group_pin={pinned}")
    return menu_ready and pinned


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "install"])
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/psy-menu"))
    args = parser.parse_args()
    os.umask(0o077)
    token = cfg.bot_settings()[0] if "CREDENTIALS_DIRECTORY" in os.environ else getpass.getpass("Telegram bot token: ")
    try:
        psy_common.bot_token(token, cfg.BOT_ID)
    except ValueError:
        raise ValueError("Invalid credential for this instance") from None

    def call(method, data):
        try:
            return psy_common.bot_call(token, method, data, timeout=15)
        except psy_common.BotApiError as error:
            raise ValueError(("Telegram declined: " if error.kind == "api" else "Telegram call failed: ") + method) from None

    return 0 if entries(call, args.action == "install", args.state_dir) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, KeyError) as exc:
        sys.exit(f"Launcher setup failed: {exc}")
