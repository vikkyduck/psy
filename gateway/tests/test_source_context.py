"""Exercise the production group route with SQLite, synthetic messages and zero network I/O."""
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch


from psy_modules import psy_group_route as route

store = route.store


def file(name, data):
    received = NS(file_path="https://api.telegram.org/file/bot-test/document",
                  download_as_bytearray=AsyncMock(return_value=data))
    return NS(file_name=name, file_size=len(data), get_file=AsyncMock(return_value=received))


class SourceContext(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        original_database = store.database
        stack.enter_context(patch.object(store, "database", lambda *a, **kw: original_database(self.root)))
        config = ModuleType("hermes_cli.config")
        config.load_config = lambda: {"model": {"provider": "openai-codex", "default": "fixture"}}
        stack.enter_context(patch.dict(sys.modules, {"hermes_cli.config": config}))
        self.response = Mock(return_value=NS(text="Первый ответ", images=[]))
        self.selector = Mock(side_effect=lambda q, choices, active, model, **kwargs: active)
        stack.enter_context(patch.object(route, "respond", self.response))
        stack.enter_context(patch.object(route, "choose_source", self.selector))
        self.next_id = 1

    def msg(self, text="", doc=None, *, uid=11, topic=0, quoted=None, bot=False, caption=None):
        mid = self.next_id
        self.next_id += 1
        return NS(message_id=mid, chat=NS(id=route.GROUP), from_user=NS(id=route.BOT if bot else uid, is_bot=bot),
                  is_topic_message=bool(topic),message_thread_id=topic or None,sender_chat=None,
                  business_connection_id=None,text=text,caption=caption,document=doc,reply_to_message=quoted,
                  reply_text=AsyncMock(return_value=NS(message_id=10000+mid)),
                  reply_document=AsyncMock(return_value=NS(message_id=10000+mid)))

    def input(self):
        return self.response.call_args.args[0][-1]["content"]

    async def test_configuration_is_read_off_the_event_loop(self):
        on_loop = []
        def load_config():
            on_loop.append(threading.current_thread() is threading.main_thread())
            return {"model": {"provider": "openai-codex", "default": "fixture"}}
        with patch.object(sys.modules["hermes_cli.config"], "load_config", load_config):
            await route.group_message(self.msg("Простой вопрос"))
        self.assertEqual(on_loop, [False])
        self.response.assert_called_once()

    async def test_full_document_and_persistent_continuation(self):
        body = "Первый факт\n" + "ф"*220001 + "\nПоследний факт"
        src = self.msg("Разбери", file("session.txt", body.encode()))
        await route.group_message(src)
        self.assertEqual(self.input()[1]["text"], "Материал: session.txt\n"+body)
        self.response.return_value.text = "Предыдущий ответ без последнего факта"
        await route.group_message(self.msg("А где он остановился?"))
        self.assertEqual(self.input()[1]["text"], "Материал: session.txt\n"+body)
        _, options, active = self.selector.call_args.args
        self.assertNotIn("Последний факт", repr(options))
        self.assertEqual(options[0]["id"], active)
        self.assertEqual(len(store.listing(11, 0)), 1)

    async def test_original_correction_reaches_model_after_eight_more_exchanges(self):
        correction = "x" * 3500 + " Use columns Name and Date."
        self.response.return_value.text = "y" * 4500 + " full answer tail"
        await route.group_message(self.msg(correction))
        self.response.return_value.text = "Next answer"
        for index in range(8):
            await route.group_message(self.msg(f"Clarification {index}"))
        await route.group_message(self.msg("Replace Date with Status and continue"))
        turns = self.response.call_args.args[0]
        self.assertEqual(turns[0]["content"], correction)
        self.assertTrue(turns[1]["content"].endswith("full answer tail"))
        self.assertEqual(len(turns), 19)
        self.assertIn("Replace Date with Status", turns[-1]["content"][0]["text"])
        self.assertEqual(self.response.call_args.kwargs["context_scope"],
                         {"uid": 11, "topic": 0, "source_id": None})

    async def test_private_context_is_saved_but_excluded_from_technical_result_audit(self):
        state = {"memory": {"summary": "SYNTHETIC PRIVATE MEMORY"}}
        self.response.return_value.quality_metadata = {"context_state": state,
                                                       "context_audit": {"compacted_messages": 20}}
        request = self.msg("Use this synthetic memory")
        await route.group_message(request)
        self.assertEqual(store.get_context(11, 0, None), state)
        audit = store.explain_request(route.audit_key(request), uid=11, topic=0)
        self.assertNotIn("SYNTHETIC PRIVATE MEMORY", json.dumps(audit))
        with store.database() as con:
            metadata = con.execute("SELECT metadata FROM turn_results").fetchone()[0]
        self.assertNotIn("SYNTHETIC PRIVATE MEMORY", metadata)
        await route.group_message(self.msg("Continue"))
        self.assertEqual(self.response.call_args.kwargs["context_state"], state)
        await route.group_message(self.msg("Another participant", uid=12))
        self.assertIsNone(self.response.call_args.kwargs["context_state"])

    async def test_mini_app_then_explicit_answer_then_free_followup(self):
        attached = file("Расшифровка.txt", b"full mini-app text")
        transcript = self.msg(doc=attached, bot=True, caption='Расшифровка записи "seminar.m4a" готова. Ответьте на это сообщение, чтобы обсудить текст.')
        original = self.msg("Разбери запись", quoted=transcript)
        await route.group_message(original)
        self.assertEqual(self.input()[1]["text"], "Материал: Расшифровка seminar.m4a.txt\nfull mini-app text")
        reply = self.msg("Первый ответ", bot=True)
        reply.message_id = original.reply_text.return_value.message_id
        await route.group_message(self.msg("А что было дальше?", quoted=reply))
        self.assertIn("full mini-app text", self.input()[1]["text"])
        await route.group_message(self.msg("Теперь разверни второй эпизод"))
        self.assertIn("full mini-app text", self.input()[1]["text"])
        await route.group_message(self.msg("Еще раз", quoted=transcript))
        attached.get_file.assert_awaited_once()
        self.assertEqual(len(store.listing(11, 0)), 1)

    async def test_reply_to_long_answer_uses_source_and_denies_other_member_and_topic(self):
        self.response.return_value.text = "З"*70000 + "конец длинного ответа"
        req = self.msg("Анализ", file("interview.txt", b"unique SOURCE"))
        await route.group_message(req)
        await route.group_message(self.msg("Раскрой последний пункт"))
        self.assertIn("конец длинного ответа", self.response.call_args.args[0][-2]["content"])
        reply = self.msg(doc=file("Ответ PSY.txt", b"answer"), bot=True)
        reply.message_id = req.reply_document.return_value.message_id
        await route.group_message(self.msg("Обоснуй", quoted=reply))
        self.assertIn("unique SOURCE", self.input()[1]["text"])
        self.assertIn("З"*100, self.input()[0]["text"])
        self.assertIn("конец длинного ответа", self.input()[0]["text"])
        self.response.reset_mock()
        for uid, topic in ((12,0),(11,5)):
            denied = self.msg("Обоснуй", quoted=reply, uid=uid, topic=topic)
            await route.process_group(denied)
            denied.reply_text.assert_awaited_once_with(route.UNLINKED_REPLY)
            self.response.assert_not_called()
        reply.document.get_file.assert_not_awaited()

    async def test_multiple_sources_clarify_select_and_then_new_topic(self):
        await route.group_message(self.msg("Разбери A", file("alpha.txt", b"alpha-source")))
        await route.group_message(self.msg("Разбери B", file("beta.txt", b"beta-source")))
        self.selector.side_effect = None
        self.selector.return_value = "clarify"
        unclear = self.msg("Сравни это с предыдущим")
        await route.group_message(unclear)
        self.assertIn("alpha.txt", unclear.reply_text.await_args.args[0])
        self.assertIn("beta.txt", unclear.reply_text.await_args.args[0])
        self.assertEqual(self.response.call_count, 2)
        source_a = store.listing(11,0)[1][0]
        self.selector.return_value = source_a
        await route.group_message(self.msg("Вернемся к alpha.txt"))
        self.assertIn("alpha-source", self.input()[1]["text"])
        self.assertNotIn("Разбери B", repr(self.response.call_args.args[0]))
        self.selector.return_value = None
        await route.group_message(self.msg("Сколько часов в сутках?"))
        self.assertNotIn("source", repr(self.response.call_args.args[0]))
        self.assertNotIn("Разбери A", repr(self.response.call_args.args[0]))
        self.selector.reset_mock()
        await route.group_message(self.msg("А минут?"))
        self.selector.assert_not_called()
        self.assertEqual(len(self.input()), 1)

    async def test_legacy_read_get_and_explicit_source_scope(self):
        with sqlite3.connect(self.root / "group.sqlite3") as con:
            con.executescript("""CREATE TABLE documents(id INTEGER PRIMARY KEY,uid INTEGER,topic INTEGER,name TEXT,kind TEXT,data BLOB);
                INSERT INTO documents VALUES(7,11,5,'legacy.txt','text',X'6f6c64');
                CREATE TABLE turns(id INTEGER PRIMARY KEY,uid INTEGER,topic INTEGER,prompt TEXT,answer TEXT);
                INSERT INTO turns VALUES(1,11,5,'legacy unknown session','ambiguous history');""")
        for uid, topic in ((12,5),(11,8)):
            denied = self.msg("/read 7", uid=uid, topic=topic)
            await route.group_message(denied)
            self.assertIn("не найден", denied.reply_text.await_args.args[0])
        await route.group_message(self.msg("/read 7 Разбери", topic=5))
        self.assertEqual(self.input()[1]["text"], "Материал: legacy.txt\nold")
        self.assertNotIn("ambiguous history", repr(self.response.call_args.args[0]))
        await route.group_message(self.msg("Детальнее", topic=5))
        self.assertEqual(self.input()[1]["text"], "Материал: legacy.txt\nold")
        get = self.msg("/get 7", topic=5)
        await route.group_message(get)
        sent = self.msg(doc=file("legacy.txt", b"wrong"), bot=True, topic=5)
        sent.message_id = get.reply_document.return_value.message_id
        await route.group_message(self.msg("Разбери еще раз", quoted=sent, topic=5))
        self.assertEqual(self.input()[1]["text"], "Материал: legacy.txt\nold")
        sent.document.get_file.assert_not_awaited()

    async def test_reply_to_own_pending_request_uses_specific_completed_answer(self):
        parent = self.msg("Разбери первый текст")
        self.response.return_value.text = "Итог первой работы"
        await route.group_message(parent)
        self.response.return_value.text = "Ответ на независимый вопрос"
        await route.group_message(self.msg("Независимый вопрос"))
        await route.group_message(self.msg("Разверни именно первый вывод", quoted=parent))
        quoted_input = self.input()[0]["text"]
        self.assertIn("Разбери первый текст", quoted_input)
        self.assertIn("Итог первой работы", quoted_input)
        self.assertNotIn("Ответ на независимый вопрос", quoted_input)
        different = self.msg("Что имеется в виду?", uid=12, quoted=parent)
        await route.group_message(different)
        self.assertNotIn("Итог первой работы", repr(self.response.call_args.args[0]))

    async def test_direct_source_overrides_previous_binding_and_preserves_on_failed_analysis(self):
        await route.group_message(self.msg("Первый", file("one.txt", b"111")))
        self.response.side_effect = RuntimeError("synthetic failure")
        current = self.msg("Следующий", file("two.txt", b"222"))
        with self.assertRaises(RuntimeError):
            await route.group_message(current)
        self.response.side_effect = None
        await route.group_message(self.msg("Попробуй снова"))
        self.assertIn("222", self.input()[1]["text"])
        await route.group_message(current)
        self.assertEqual(len(store.listing(11,0)), 2)

    async def test_transcript_size_and_user_scoped_choices(self):
        data = b"A" * (1024**2 + 99) + b"tail"
        intake = self.msg("Разбери", file("large.txt", data))
        await route.group_message(intake)
        self.assertTrue(self.input()[1]["text"].endswith("tail"))
        self.assertEqual(self.input()[1]["text"].partition("\n")[2].encode(), data)
        too_big = file("oversize.txt", b"unused")
        too_big.file_size = route.MAX_TEXT + 1
        refused = self.msg("Разбери", too_big)
        await route.process_group(refused)
        too_big.get_file.assert_not_awaited()
        refused.reply_text.assert_awaited_once_with(route.TEXT_SIZE_ERROR)
        for uid, topic in ((12,0), (11, 5)):
            self.assertEqual(store.choices(uid,topic,True), (None,[]))
            await route.group_message(self.msg("А что дальше?", uid=uid, topic=topic))
            self.assertEqual(len(self.input()), 1)
            self.assertNotIn("large.txt", repr(self.response.call_args.args[0]))


if __name__ == "__main__":
    unittest.main()
