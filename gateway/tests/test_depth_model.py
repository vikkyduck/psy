"""Synthetic model/stream regressions. No provider or Telegram calls."""
import importlib
import json
import os
import sys
import types
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

if os.environ.get("PSY_GROUP_TEST_INSTALLED"):
    m = importlib.import_module("plugins.platforms.telegram.psy_group_codex")
else:
    try:
        m = importlib.import_module("psy_modules.psy_group_codex")
    except ModuleNotFoundError:
        m = importlib.import_module("psy_group_codex")


class Depth(unittest.TestCase):
    def setUp(self):
        httpx = types.ModuleType("httpx")
        httpx.RequestError = type("RequestError", (Exception,), {})
        httpx.TimeoutException = type("TimeoutException", (httpx.RequestError,), {})
        httpx.Client = Mock()
        httpx.Timeout = Mock()
        httpx.Client.return_value = nullcontext(Mock())
        bootstrap = types.ModuleType("agent.process_bootstrap")
        bootstrap.enable_happy_eyeballs_on_client = lambda client: None
        mods = patch.dict(sys.modules, {"httpx": httpx, "agent.process_bootstrap": bootstrap})
        mods.start()
        self.addCleanup(mods.stop)

    def test_semantic_ordinary_and_analysis_use_one_request(self):
        for q in ("merci", "Qu'est-ce qui laisse penser que sa position a changé ?"):
            result = m.Result("réponse", [], 0)
            with patch.object(m, "_stream", return_value=result) as stream:
                self.assertEqual(m._compose(None, {}, [m._user(q)], False, "fake", q).text, "réponse")
                self.assertEqual(stream.call_count, 1)
                self.assertFalse(stream.call_args.args[3])
                self.assertIn("по умолчанию разбирай глубоко", stream.call_args.kwargs["instructions"])
                self.assertNotIn("8000", stream.call_args.kwargs["instructions"])

    def test_web_cannot_see_private_attachment_filename_quote_or_history(self):
        q = "Сопоставь с понятием вытеснения у Фрейда и проверь публикации"
        turns = [m._user("secret-history"), {"role":"user", "content":[
            {"type":"input_text","text":q+"\n\nЦитата из этого чата:\nsecret-quote"},
            {"type":"input_text","text":"Материал: secret-name\nsecret-transcript"}]}]
        def run(client, headers, messages, web, model, absolute, *, instructions):
            if m.DISPATCH in instructions:
                return m.Result('PSY_TASK {"public_topic":"secret-private-topic"}', [], 0)
            if "Выдели только" in instructions:
                self.assertEqual(messages, [m._user(q)])
                return m.Result('{"topic":"Freud Repression 1915"}', [], 0)
            if web:
                self.assertEqual(messages, [m._user("Freud Repression 1915")])
                return m.Result("Source https://example.org", [], 1)
            self.assertIn("secret-transcript", str(messages))
            self.assertIn("https://example.org", str(messages))
            return m.Result("Готово", [], 0)
        with patch.object(m, "_stream", side_effect=run):
            result = m._compose(None, {}, turns, False, "fake", q)
        self.assertEqual(result.searches, 1)

    def stream(self, events, research=False):
        response = Mock(status_code=200)
        response.iter_lines.return_value = ["data: "+json.dumps(x) for x in events]
        client = Mock()
        client.stream.return_value = nullcontext(response)
        res = m._stream(client, {}, [], research, "model", 180)
        self.assertNotIn("verbosity", str(client.stream.call_args.kwargs["json"]))
        return res

    def test_commentary_never_becomes_a_response_or_a_continuation(self):
        prefix = [{"type":"response.output_item.added","item":{"id":"c","type":"message","phase":"commentary"}},
                  {"type":"response.output_text.delta","item_id":"c","delta":"internal progress"},
                  {"type":"response.output_item.done","item":{"id":"c","type":"message","phase":"commentary",
                     "content":[{"type":"output_text","text":"internal progress"}]}},
                  {"type":"response.output_text.delta","item_id":"a","delta":"final content"}]
        for end, complete in (({"type":"response.completed"}, True),
                ({"type":"response.incomplete","response":{"incomplete_details":{"reason":"max_output_tokens"}}}, False)):
            r = self.stream([*prefix, end])
            self.assertEqual(r.text, "final content")
            self.assertEqual(r.complete, complete)

    def test_http_and_terminal_events_which_must_not_be_resumed(self):
        for event in [{"type":"error","error":{"code":"invalid_token"}},
                      {"type":"response.failed","response":{"error":{"code":"usage_limit_reached"}}},
                      {"type":"response.incomplete","response":{"incomplete_details":{"reason":"content_filter"}}}]:
            with self.assertRaises(m.ModelIssue):
                self.stream([{"type":"response.output_text.delta","delta":"unfinished"},event])
        self.assertEqual(m._failure(400,{"code":"context_length_exceeded"},"http").kind,"context")

    def test_only_genuine_web_result_and_terminal_output_are_accepted(self):
        output=[{"type":"message","phase":"commentary","content":[{"type":"output_text","text":"private progress"}]},
                {"type":"message","phase":"final_answer","content":[{"type":"output_text","text":"A",
                 "annotations":[{"type":"url_citation","url":"https://example.org"}]}]}]
        evt={"type":"response.completed","response":{"output":output}}
        with self.assertRaises(m.ModelIssue):
            self.stream([evt], True)
        output.append({"type":"web_search_call","status":"completed"})
        result=self.stream([evt],True)
        self.assertEqual((result.text,result.searches),("A\nhttps://example.org",1))

    def test_resume_waits_for_terminal_event_and_preserves_earlier_text(self):
        parts = [m.Result("first", [], 0, False),m.Result("last", [], 0, True)]
        with patch.object(m, "_stream", side_effect=parts) as stream:
            r = m._compose(None, {}, [m._user("question")], False, "fake", "question")
            self.assertEqual((r.text,r.complete),("first\nlast",True))
            self.assertIn("first", str(stream.call_args.args[2]))
        call = Mock(side_effect=m.ModelIssue("timeout","upstream"))
        partial = m._complete(call, [], m.SYSTEM, initial=parts[0])
        self.assertTrue(partial.text.startswith("first"))
        self.assertFalse(partial.complete)
        self.assertIn("Продолжение не завершено",partial.text)

    def test_map_reads_every_character_once_and_answers_in_context(self):
        q="А дальше?"
        body=("0-17 " * 31523) + " конец записи"
        turns=[m._user("обсуждали хронологию"), {"role":"user","content":[
            {"type":"input_text","text":q},{"type":"input_text","text":"Материал: f.txt\n"+body}]}]
        read={}
        def run(messages, instructions):
            s=messages[-1]["content"]
            i=int(s.split("Фрагмент ")[1].split()[0])
            read[i]=s.partition("Материал: f.txt\n")[2]
            self.assertIn("обсуждали хронологию", str(messages))
            return m.Result("свидетельство "+str(i),[],0)
        notes=m._map_source(run,turns,q)
        self.assertEqual("".join(read[i] for i in sorted(read)),body)
        self.assertIn("свидетельство "+str(len(read)),notes[-1]["content"])

    def test_partial_section_failure_preserves_the_completed_sections(self):
        plan={"title":"Учебное занятие","brief":"b","sections":[
            {"title":"Начало","minutes":15,"activity_minutes":0,"brief":"a"},
            {"title":"Финал","minutes":15,"activity_minutes":0,"brief":"b"}]}
        def call(turns, instructions):
            if "редакционный" in instructions:
                return m.Result(json.dumps(plan,ensure_ascii=False),[],0)
            if "только раздел 1" in instructions:
                return m.Result("Материал "*1500,[],0)
            raise m.ModelIssue("quota","test")
        r=m._lecture(call,[m._user("текст лекции")],30)
        self.assertFalse(r.complete)
        self.assertIn("Этот раздел не сформирован",r.text)
        self.assertIn("Материал Материал",r.text)
        self.assertIn("лимита",r.text)

    def test_lecture_appends_missing_speech_without_replacing_the_draft(self):
        plan = {"title":"Чтение", "sections":[
            {"title":"Текст", "minutes":30, "activity_minutes":10, "brief":"пример"}]}
        def call(turns, instructions):
            if "редакционный" in instructions:
                return m.Result(json.dumps(plan, ensure_ascii=False), [], 0)
            if "только раздел 1" in instructions:
                return m.Result("основа " * 1700, [], 0)
            self.assertIn("только добавляемый текст", turns[-1]["content"])
            return m.Result("продолжение " * 300, [], 0)
        r = m._lecture(call, [m._user("рукопись")], 30)
        self.assertTrue(r.complete)
        self.assertEqual(r.text.count("основа"), 1700)
        self.assertEqual(r.text.count("продолжение"), 300)

    def test_selector_is_toolless_and_rejects_unscoped_ids(self):
        opts=[{"id":16,"name":"пример"}]
        for value, expected in ((16,16),(17,"clarify"),(None,None),(True,"clarify")):
            result=m.Result(json.dumps({"source":value}),[],0)
            with patch.object(m,"_connection",return_value=("fake",{})), patch.object(m,"_stream",return_value=result) as stream:
                self.assertEqual(m.choose_source("почему?",opts,16,model="fake"),expected)
                self.assertFalse(stream.call_args.args[3])


if __name__ == "__main__":
    unittest.main()
