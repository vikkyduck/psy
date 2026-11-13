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
    @staticmethod
    def source_card(url="https://example.org/original"):
        return dict(author="А.", work="Опыты", date="1920", location="раздел IV", support="A produces B",
                    claim="A порождает B при заданном условии.", url=url, access="full_text")

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
        auth = patch.object(m, "_connection", return_value=("fixture", {}))
        auth.start()
        self.addCleanup(auth.stop)
        mods.start()
        self.addCleanup(mods.stop)

    def test_semantic_ordinary_and_analysis_use_one_request(self):
        for q in ("merci", "Qu'est-ce qui laisse penser que sa position a changé ?"):
            result = m.Result("réponse", [], 0)
            with patch.object(m, "_stream", return_value=result) as stream:
                self.assertEqual(m._compose(None, [m._user(q)], False, "fake", q).text, "réponse")
                self.assertEqual(stream.call_count, 1)
                self.assertFalse(stream.call_args.args[3])
                self.assertIn("по умолчанию разбирай глубоко", stream.call_args.kwargs["instructions"])
                self.assertNotIn("8000", stream.call_args.kwargs["instructions"])

    def test_web_cannot_see_private_attachment_filename_quote_or_history(self):
        q = "Сопоставь с понятием вытеснения у Фрейда и проверь публикации"
        turns = [m._user("secret-history"), {"role":"user", "content":[
            {"type":"input_text","text":q+"\n\nЦитата из этого чата:\nsecret-quote"},
            {"type":"input_text","text":"Материал: secret-name\nsecret-transcript"}]}]
        def run(client, headers, messages, web, model, *, instructions):
            if m.DISPATCH in instructions:
                return m.Result('PSY_TASK {"public_topic":"secret-private-topic"}', [], 0)
            if "Выдели только" in instructions:
                self.assertEqual(messages, [m._user(q)])
                return m.Result('{"topic":"Freud Repression 1915"}', [], 0)
            if web:
                self.assertEqual(messages[0], m._user("Freud Repression 1915"))
                self.assertNotIn("secret-", str(messages))
                if instructions == m.WEB_SYSTEM:
                    return m.Result("Public notes", [], 1)
                card = self.source_card()
                return m.Result(json.dumps({"claims":[card], "gaps":[]}), [], 1, source_urls=(card["url"],))
            if instructions == m.REVIEW:
                self.assertIn("Готово", str(messages))
                return m.Result('{"edits":[]}', [], 0)
            self.assertIn("secret-transcript", str(messages))
            self.assertIn("https://example.org", str(messages))
            return m.Result("Готово", [], 0)
        with patch.object(m, "_stream", side_effect=run):
            result = m._compose(None, turns, False, "fake", q)
        self.assertEqual(result.searches, 2)

    def test_untraceable_sources_are_not_released_as_confirmed(self):
        fake = self.source_card("https://unknown.invalid/archive")
        public = m.Result(json.dumps({"claims":[fake],"gaps":[]}), [], 1,
                          source_urls=("https://example.org",))
        call = Mock(side_effect=[m.Result("search", [], 1), public, public])
        r = m._primary_sources(call, "Academic topic")
        self.assertFalse(r.complete)
        self.assertNotIn(fake["url"], r.text)
        self.assertTrue(all(c.kwargs.get("web") for c in call.call_args_list))

    def test_source_gaps_trigger_second_lookup_without_losing_verified_claims(self):
        a = self.source_card()
        b = self.source_card("https://archive.example/later")
        b["date"] = "1940"
        def metadata(card, gaps):
            return m.Result(json.dumps({"claims":[card], "gaps":gaps}) + "\n" + card["url"], [], 1,
                            source_urls=(card["url"],))
        call = Mock(side_effect=[m.Result("search", [], 1), metadata(a, ["Позиция в 1940"]),
                                 metadata(b, [])])
        r = m._primary_sources(call, "Academic topic")
        self.assertEqual(json.loads(r.text)["claims"], [a,b])
        self.assertIn("Позиция в 1940", call.call_args.args[0][-1]["content"])

    def test_corrects_original_claim_and_its_recap_in_place(self):
        text = "Первая часть. A порождает C.\n\nИтог второй части: A порождает C."
        verdict = {"edits":[
            {"before":"Первая часть. A порождает C.", "after":"Первая часть. A порождает B."},
            {"before":"Итог второй части: A порождает C.", "after":"Итог второй части: A порождает B."}]}
        call = Mock(return_value=m.Result(json.dumps(verdict), [], 0))
        r = m._verify_draft(call, m.Result(text,[],0), json.dumps(self.source_card()))
        self.assertIn(text, call.call_args.args[0][-1]["content"])
        self.assertNotIn("C",r.text)
        self.assertEqual(r.text.count("порождает B"),2)

    def test_bad_replacement_does_not_pass_as_validated(self):
        response = m.Result('{"edits":[{"before":"ambiguous","after":"altered"}]}', [], 0)
        with self.assertRaises(m.ModelIssue):
            m._verify_draft(Mock(return_value=response), m.Result("ambiguous ambiguous", [], 0), "source")

    def test_full_review_failure_recovers_verified_sections_and_marks_only_failed_section(self):
        doc = "Title\n\n1. A (0-15 мин по плану)\n\nПроверенный раздел.\n\n2. B (15-30 мин по плану)\n\nUNSUPPORTED"
        def check(turns, instructions):
            if "UNSUPPORTED" in str(turns):
                raise m.ModelIssue("unavailable", "section")
            return m.Result('{"edits":[]}', [], 0)
        r = m._recover_verified(check, m.Result(doc, [], 0),
                                m.Result(json.dumps({"claims":[self.source_card()]}), [], 2),
                                m.ModelIssue("context","whole"))
        self.assertIn("Проверенный раздел",r.text)
        self.assertIn("Сверку этого раздела",r.text)
        self.assertNotIn("UNSUPPORTED",r.text)
        self.assertFalse(r.complete)

    def stream(self, events, research=False):
        response = Mock(status_code=200)
        response.iter_lines.return_value = ["data: "+json.dumps(x) for x in events]
        client = Mock()
        client.stream.return_value = nullcontext(response)
        res = m._stream(client, {}, [], research, "model")
        payload = client.stream.call_args.kwargs["json"]
        self.assertNotIn("verbosity", str(payload))
        self.assertNotIn("max_output_tokens", payload)
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
        self.assertEqual(result.source_urls, ("https://example.org",))

    def test_resume_waits_for_terminal_event_and_preserves_earlier_text(self):
        parts = [m.Result("first", [], 0, False),m.Result("last", [], 0, True)]
        with patch.object(m, "_stream", side_effect=parts) as stream:
            r = m._compose(None, [m._user("question")], False, "fake", "question")
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
            if "Проверь содержание раздела" in instructions:
                return m.Result('{"missing":""}', [], 0)
            raise m.ModelIssue("quota","test")
        r=m._lecture(call,[m._user("текст лекции")],30)
        self.assertFalse(r.complete)
        self.assertIn("Этот раздел не сформирован",r.text)
        self.assertIn("Материал Материал",r.text)
        self.assertIn("лимита",r.text)

    def test_lecture_fills_a_concrete_gap_even_in_a_long_draft(self):
        plan = {"title":"Чтение", "sections":[
            {"title":"Текст", "minutes":30, "activity_minutes":10, "brief":"пример"}]}
        draft = "Разбор уже прозвучавшего материала. " * 1500
        def call(turns, instructions):
            if "редакционный" in instructions:
                return m.Result(json.dumps(plan, ensure_ascii=False), [], 0)
            if "только раздел 1" in instructions:
                return m.Result(draft, [], 0)
            if "Проверь содержание раздела" in instructions:
                self.assertEqual(turns[-1]["content"], draft)
                return m.Result('{"missing":"Разбор реплики из источника"}', [], 0)
            self.assertIn("Разбор реплики из источника", turns[-1]["content"])
            return m.Result("Продолжение примера.", [], 0)
        r = m._lecture(call, [m._user("рукопись")], 30)
        self.assertTrue(r.complete)
        self.assertIn(draft + "\n\nПродолжение примера.", r.text)
        self.assertIn("10 мин", r.text)

    def test_lecture_accepts_semantic_completion_regardless_of_length(self):
        for size in (1, 10000):
            with self.subTest(size=size):
                q = "Подготовь занятие на 30 минут. По моей просьбе объем текста - 400 слов."
                doc = "Материал " * size
                calls = []
                def call(turns, instructions):
                    self.assertEqual(turns[0], m._user(q))
                    calls.append(instructions)
                    if "редакционный" in instructions:
                        return m.Result(json.dumps({"title":"Разбор", "sections":[{"title":"Пример",
                            "minutes":30, "activity_minutes":8, "brief":"Разобрать эпизод"}]}), [], 0)
                    if "только раздел 1" in instructions:
                        return m.Result(doc, [], 0)
                    self.assertIn("Проверь содержание раздела", instructions)
                    return m.Result('{"missing":""}', [], 0)
                r = m._lecture(call, [m._user(q)], 30)
                self.assertTrue(r.complete)
                self.assertIn(doc, r.text)
                self.assertEqual(len(calls), 3)
                for text in [*calls, r.text]:
                    self.assertNotRegex(text, r"\d+ (?:произносимых )?слов|слов/мин|словах в минуту")

    def test_lecture_does_not_lose_draft_on_review_failure(self):
        plan = {"title":"Разбор", "sections":[{"title":"Пример", "minutes":30,
                                                  "activity_minutes":0, "brief":"Разобрать эпизод"}]}
        call = Mock(side_effect=[m.Result(json.dumps(plan), [], 0), m.Result("Сохранить текст", [], 0),
                                 m.ModelIssue("quota", "test")])
        r = m._lecture(call, [m._user("Лекция")], 30)
        self.assertFalse(r.complete)
        self.assertIn("Сохранить текст", r.text)
        self.assertIn("лимита", r.text)

    def test_selector_is_toolless_and_rejects_unscoped_ids(self):
        opts=[{"id":16,"name":"пример"}]
        for value, expected in ((16,16),(17,"clarify"),(None,None),(True,"clarify")):
            result=m.Result(json.dumps({"source":value}),[],0)
            with patch.object(m,"_connection",return_value=("fake",{})), patch.object(m,"_stream",return_value=result) as stream:
                self.assertEqual(m.choose_source("почему?",opts,16,model="fake"),expected)
                self.assertFalse(stream.call_args.args[3])


URL = "https://example.org/research.pdf"


def card(number, total):
    return dict(name=f"Observation {number}", number=number, total=total, series="Experiments",
                author="A", work="Article", date="1920", url=URL + f"#page={number}",
                support=f"sample {number}", access="full_text", location=f"section {number}",
                claim=f"distinct mechanism {number}")


def inventory(*numbers, trace=True):
    body = {"scope":"Article, sections 1-3", "items":[card(n, 3) for n in numbers], "gaps":[]}
    return m.Result(json.dumps(body), [], 1, source_urls=(URL,) if trace else ())


class CentralReading(unittest.TestCase):
    def test_finds_all_members_of_series_prior_to_comparative_work(self):
        calls = []
        def call(turns, instructions, *, web):
            self.assertTrue(web)
            calls.append(instructions)
            if instructions == m.CENTRAL_READING:
                if calls.count(m.CENTRAL_READING) == 1:
                    self.assertIn("Die Verdrängung", turns[0]["content"])
                    self.assertIn("#page=8", turns[0]["content"])
                    self.assertNotIn("Lacan", str(turns))
                    return inventory(1)
                self.assertIn("[1] из 3", turns[-1]["content"])
                return inventory(2, 3)
            if instructions == m.WEB_SYSTEM:
                return m.Result("Comparative sources", [], 1)
            self.assertEqual(instructions, m.SOURCE_CHECK)
            self.assertIn("distinct mechanism 3", str(turns))
            return m.Result(json.dumps({"claims":[card(1, 3)], "gaps":[]}), [], 1, source_urls=(URL,))
        result = m._primary_sources(call, "Freud repression and French legacy", read_main=True)
        evidence = json.loads(result.text)
        self.assertCountEqual(calls, [m.CENTRAL_READING, m.CENTRAL_READING, m.WEB_SYSTEM, m.SOURCE_CHECK])
        self.assertEqual(calls[-1], m.SOURCE_CHECK)
        self.assertEqual([c["number"] for c in evidence["central_reading"]], [1,2,3])
        self.assertEqual(evidence["gaps"], [])

    def test_partial_read_preserves_real_cells_and_labels_missing_elements(self):
        mock = Mock(side_effect=[inventory(1), inventory(2,3,trace=False)])
        entries, gaps, count = m._read_central_work(mock, "Another author")
        self.assertEqual([c["number"] for c in entries], [1])
        self.assertTrue(any("[1] из 3" in g for g in gaps))
        self.assertEqual(count, 2)

    def test_french_assignment_points_to_requested_sessions_and_later_publication(self):
        text = m.reading_list("Recherche sur le refoulement chez Freud et les psychanalystes français")
        for locator in ("#page=197", "197-198", "Perrier", "Mannoni", "verworfen", "#page=14", "#page=9"):
            self.assertIn(locator, text)
        self.assertEqual(m.reading_list("Different author and work"), "")


if __name__ == "__main__":
    unittest.main()
