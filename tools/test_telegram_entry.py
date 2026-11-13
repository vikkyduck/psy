import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

os.environ.update(PSY_OWNER_ID="101", PSY_GROUP_ID="-202", PSY_BOT_ID="303",
                  PSY_BOT_USERNAME="fixture_bot", PSY_PUBLIC_URL="https://fixture.invalid")
import telegram_entry as entry


class Launcher(unittest.TestCase):
    def test_repair_existing_pin_after_bot_rename(self):
        target = f"https://t.me/{entry.cfg.BOT_USERNAME}?startapp=psy_group"
        old = "https://t.me/prior_fixture_bot?startapp=psy_group"
        keyboard = lambda link: {"inline_keyboard": [[{"text": "Записи", "url": link}]]}
        message = {"message_id": 5, "reply_markup": keyboard(old)}
        def call(method, data):
            if method == "getMe":
                return {"id": entry.cfg.BOT_ID, "username": entry.cfg.BOT_USERNAME, "has_main_web_app": True}
            if method == "getChatMember":
                return {"status": "administrator", "can_pin_messages": True}
            if method == "getChat":
                return {"type": "supergroup", "pinned_message": message}
            if method == "getChatMenuButton":
                return {"type": "web_app", "text": "Записи", "web_app": {"url": entry.cfg.PUBLIC_URL + "/"}}
            if method == "editMessageReplyMarkup":
                self.assertEqual(data["message_id"], 5)
                message["reply_markup"] = data["reply_markup"]
                return message
            if method == "pinChatMessage":
                self.assertEqual(data["message_id"], 5)
                return True
            self.fail(method)
        with TemporaryDirectory() as tmp:
            path = Path(tmp)
            state = path / "group-entry.json"
            state.write_text(json.dumps({"chat": entry.cfg.GROUP, "bot": entry.cfg.BOT_ID, "message_id": 5, "link": old}))
            self.assertTrue(entry.entries(call, True, path))
            self.assertEqual(json.loads(state.read_text())["link"], target)
            self.assertTrue(entry.entries(call, True, path))
