import ast
import asyncio
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as S
from unittest.mock import AsyncMock, Mock, patch

from psy_modules import psy_group_route as route, psy_group_store as store


def event(chat, uid, text="", *, typ=None, **fields):
    c = S(id=chat,type=typ or ("supergroup" if chat < 0 else "private"))
    u = S(id=uid,is_bot=False)
    m = S(chat=c,from_user=u,text=text,caption=None,business_connection_id=None,sender_chat=None,
          is_topic_message=False,message_thread_id=None,document=None,reply_to_message=None,
          reply_text=AsyncMock(),reply_document=AsyncMock(),**fields)
    return S(effective_chat=c,effective_user=u,message=m)


class TestGuard(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        database = store.database
        self.enterContext(patch.object(store, "database", lambda *a, **kw:
                          database(*a, **kw) if a or kw else database(Path(directory.name))))

    async def test_matrix_and_commands(self):
        self.assertFalse(await route.intercept(None,event(route.OWNER,route.OWNER,"/model")))
        for uid in (route.OWNER, 42):
            bad = event(route.GROUP,uid,"/terminal cat anything")
            self.assertTrue(await route.intercept(None,bad))
            bad.message.reply_text.assert_awaited_once()
        for update in (event(42,42),event(-55,route.OWNER),S(effective_chat=None,effective_user=None,message=None)):
            self.assertTrue(await route.intercept(None,update))
        # Hermes' self-update never reaches Hermes, in any spelling; other owner commands still do.
        for text in ("/update", "/Update", "/update@" + route.BOT_USERNAME, " /update now ", "/update\n"):
            owner = event(route.OWNER,route.OWNER,text)
            self.assertTrue(await route.intercept(None,owner))
            owner.message.reply_text.assert_awaited_once_with(route.SELF_UPDATE_REPLY)
        for text in ("/updates", "/model", "please /update", "update"):
            self.assertFalse(await route.intercept(None,event(route.OWNER,route.OWNER,text)))
        other = event(42,42,"/start upload_"+"a"*32)
        self.assertEqual(route.scope(other),"upload")
        self.assertEqual(route.scope(event(42,42,"/start upload_bad")),"deny")
        for field in ("callback_query","edited_message","message_reaction","inline_query","business_message",
                      "channel_post","my_chat_member","chat_join_request","chosen_inline_result"):
            wrapped = event(route.GROUP,42)
            wrapped.message=None
            setattr(wrapped,field,S())
            self.assertTrue(await route.intercept(None,wrapped))

    async def test_no_paths_and_no_implicit_owner_fallback(self):
        m = event(route.GROUP,42,"/read ../../auth.json").message
        await route.group_message(m)
        m.reply_text.assert_awaited_once()
        update=event(route.GROUP,42,"please use tools")
        with patch.object(route,"group_message", AsyncMock(side_effect=RuntimeError("secret value"))):
            self.assertTrue(await route.intercept(None, update))
            self.assertNotIn("secret", update.message.reply_text.await_args.args[0])

    async def test_reply_only_same_chat(self):
        self.assertIsNone(await route.attachment(event(route.OWNER,route.OWNER).message))
        m=event(route.GROUP,42).message
        m.document=S(file_name="a.txt",file_size=20,get_file=AsyncMock(return_value=S(file_path="/etc/passwd")))
        with self.assertRaises(ValueError):
            await route.attachment(m)

    async def test_outbound_document_bytes_never_paths(self):
        m=event(route.GROUP,42).message
        await route.deliver(m,"Файл.txt",b"file:///etc/passwd")
        doc=m.reply_document.await_args.kwargs["document"]
        self.assertEqual(doc.read(),b"file:///etc/passwd")

    def test_member_and_topic_bound_library_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            ident=store.archive(10,1,"../../test","text",b"demo",root=root)
            store.remember(10,1,"prompt","answer",root=root)
            self.assertEqual(store.document(10,1,ident,root=root)[2],b"demo")
            for member,topic in ((3,1),(10,2)):
                self.assertIsNone(store.document(member,topic,ident,root=root))
                self.assertEqual(store.listing(member,topic,root=root),[])
                self.assertEqual(store.history(member,topic,root=root),[])
            self.assertEqual((root/"group.sqlite3").stat().st_mode & 0o777,0o600)
            self.assertFalse((root/"test").exists())

if __name__ == "__main__":
    unittest.main()
