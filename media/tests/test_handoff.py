"""Exercise sender-bound Telegram links and the group-to-private handoff."""
import importlib.util
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import psy_upload_common as common


class HandoffTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        storage = patch.multiple(common, ROOT=Path(temp.name)/'state', FILES=Path(temp.name)/'files')
        storage.start()
        self.addCleanup(storage.stop)
        # Only Telegram's three outbound markup containers are mocked.
        telegram = ModuleType('telegram')
        telegram.InlineKeyboardButton = lambda label, **kwargs: kwargs
        telegram.InlineKeyboardMarkup = lambda rows: rows
        telegram.WebAppInfo = lambda url: url
        src = Path(__file__).resolve().parent.parent/'telegram_handoff.py'
        spec = importlib.util.spec_from_file_location('fixture_handoff', src)
        self.handoff = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, telegram=telegram), patch.object(sys, 'path', sys.path.copy()):
            spec.loader.exec_module(self.handoff)

    def message(self, uid, chat, text, member='member'):
        bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=SimpleNamespace(
            to_dict=lambda: {'status':member})))
        return SimpleNamespace(
            from_user=SimpleNamespace(id=uid,is_bot=False),
            chat=SimpleNamespace(id=chat,type='private' if chat>0 else 'supergroup'),
            text=text, message_id=51, message_thread_id=None, is_topic_message=False,
            reply_text=AsyncMock(), get_bot=lambda:bot,
        )

    async def test_group_link_is_bound_to_originator_and_rechecks_membership(self):
        msg = self.message(123,common.GROUP, '/audio@'+common.BOT_USERNAME)
        self.assertTrue(await self.handoff.command(msg))
        url = msg.reply_text.await_args.kwargs['reply_markup'][0][0]['url']
        prefix = f'https://t.me/{common.BOT_USERNAME}?start=upload_'
        self.assertTrue(url.startswith(prefix))
        ticket = url.removeprefix(prefix)
        other = self.message(124,124,'/start upload_'+ticket)
        self.assertTrue(await self.handoff.command(other))
        self.assertNotIn('reply_markup',other.reply_text.await_args.kwargs)
        revoked = self.message(123,123,'/start upload_'+ticket,'left')
        self.assertTrue(await self.handoff.command(revoked))
        revoked.reply_text.assert_not_awaited()
        sender = self.message(123,123,'/start upload_'+ticket)
        self.assertTrue(await self.handoff.command(sender))
        dest = sender.reply_text.await_args.kwargs['reply_markup'][0][0]['web_app']
        self.assertEqual(dest, common.PUBLIC_URL+'/?ticket='+ticket)

    async def test_commands_for_other_bots_and_other_chats(self):
        ignored = self.message(123,common.GROUP,'/audio@other_bot')
        self.assertFalse(await self.handoff.command(ignored))
        ignored.reply_text.assert_not_awaited()
        for uid,chat in ((123,123),(123,-404)):
            denied = self.message(uid,chat,'/upload')
            self.assertTrue(await self.handoff.command(denied))
            denied.reply_text.assert_not_awaited()
        owner = self.message(common.OWNER,common.OWNER,'/audio')
        self.assertTrue(await self.handoff.command(owner))
        self.assertIn('web_app',owner.reply_text.await_args.kwargs['reply_markup'][0][0])
