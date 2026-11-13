"""Exercise the full model pipeline on a virtual clock with synthetic SSE traffic."""
import json
import sys
import types
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

from psy_modules import psy_group_codex as m


def event(kind, **values):
    return "data: " + json.dumps({"type": kind, **values})


class Duration(unittest.TestCase):
    def setUp(self):
        self.elapsed = 0
        self.streams = []
        module = types.ModuleType("httpx")
        module.RequestError = type("RequestError", (Exception,), {})
        module.TimeoutException = type("TimeoutException", (module.RequestError,), {})
        self.timeout = module.TimeoutException
        self.reset = module.RequestError
        for scope in (patch.dict(sys.modules, {"httpx": module}),
                      patch.object(m.time, "monotonic", side_effect=lambda: self.elapsed),
                      patch.object(m, "_connection", side_effect=self.connection)):
            scope.start()
            self.addCleanup(scope.stop)

    def connection(self):
        # A fresh stage must see the same account's currently usable token.
        return "unused", {"fixture-issued": self.elapsed}

    def compose(self, question, stage, *, research=False):
        def stream(method, endpoint, *, headers, json, timeout):
            self.assertEqual(headers["fixture-issued"], self.elapsed)
            self.assertEqual(timeout, (10, 160 if json["tools"] else 90, 60, 10))
            self.streams.append(json)
            return nullcontext(Mock(status_code=200, iter_lines=lambda: stage(json)))
        client = Mock(stream=stream)
        return m._compose(client, [m._user(question)], research, "test", question)

    def answer(self, text, *, incomplete=False, citation=None):
        # No output deltas during the reasoning period. Each SSE comment arrives
        # inside the read idle interval, but the whole stream takes two hours.
        for _ in range(120):
            self.elapsed += 60
            yield ": keep-alive"
        if citation:
            yield event("response.output_item.done", item={"type": "web_search_call", "status": "completed",
                        "action": {"type": "open_page", "url": citation}})
        yield event("response.output_text.delta", delta=text)
        if incomplete:
            yield event("response.incomplete", response={"incomplete_details": {"reason": "max_output_tokens"}})
        else:
            yield event("response.completed")

    def test_unclassified_deep_answer_continues_as_long_as_text_advances(self):
        def stage(payload):
            index = len(self.streams)
            return self.answer(f"Раздел {index}.", incomplete=index < 8)
        result = self.compose("Проследи ход рассуждения по всей записи", stage)
        self.assertTrue(result.complete)
        self.assertEqual(result.text.splitlines(), [f"Раздел {i}." for i in range(1, 9)])
        self.assertEqual(self.elapsed, 16 * 3600)

    def test_lecture_finishes_planning_writing_resume_and_editorial_review(self):
        plan = {"title": "Лекция", "sections": [{"title": "Теория", "minutes": 30, "activity_minutes": 0,
                                                 "brief": "Исторический пример"}]}
        outputs = iter([('PSY_TASK {"minutes":30}', False), (json.dumps(plan), False),
                        ("Начало речи", True), ("Конец речи", False), ('{"missing":""}', False)])
        def stage(payload):
            text, incomplete = next(outputs)
            return self.answer(text, incomplete=incomplete)
        result = self.compose("Напиши текст лекции", stage)
        self.assertTrue(result.complete)
        self.assertIn("Начало речи\nКонец речи", result.text)
        self.assertEqual(self.elapsed, 10 * 3600)

    def test_research_finishes_search_validation_synthesis_and_cross_check(self):
        url = "https://source.example/work"
        card = dict(author="A", work="W", date="1915", location="part 1", support="a", claim="b",
                    url=url, access="full_text")
        def stage(payload):
            instructions = payload["instructions"]
            if m.DISPATCH in instructions:
                return self.answer('PSY_TASK {"public_topic":"Academic topic"}')
            if instructions == m.WEB_SYSTEM:
                return self.answer("notes", citation=url)
            if instructions == m.SOURCE_CHECK:
                return self.answer(json.dumps({"claims": [card], "gaps": []}), citation=url)
            if instructions == m.REVIEW:
                return self.answer('{"edits":[]}')
            return self.answer("Verified analysis")
        with patch.object(m.EvidenceReader, "locate", return_value=("HTML, абзац 1", "fixture context")):
            result = self.compose("Публичная тема: Academic topic", stage, research=True)
        self.assertTrue(result.complete)
        self.assertEqual(result.text, "Verified analysis")
        self.assertEqual(result.searches, 2)
        self.assertEqual(self.elapsed, 10 * 3600)

    def test_silent_connection_before_first_event_is_an_idle_error(self):
        def silent(payload):
            yield from ()
            raise self.timeout()
        with self.assertRaises(m.ModelIssue) as error:
            self.compose("Разбери источник", silent)
        self.assertEqual((error.exception.kind, error.exception.stage), ("timeout", "stream_idle"))
        self.assertEqual(len(self.streams), 1)

    def test_empty_or_duplicate_continuation_keeps_draft_without_polling(self):
        for final in ("", "Начало"):
            with self.subTest(final=final):
                self.streams.clear()
                outputs = iter([("Начало", True), (final, False)])
                def stage(payload):
                    text, incomplete = next(outputs)
                    return self.answer(text, incomplete=incomplete)
                result = self.compose("Длинный вопрос", stage)
                self.assertFalse(result.complete)
                self.assertTrue(result.text.startswith("Начало"))
                self.assertEqual(len(self.streams), 2)

    def test_resume_after_idle_reset_or_eof_uses_all_public_items_once(self):
        for ending in (self.timeout, self.reset, None):
            with self.subTest(ending=ending):
                self.streams.clear()
                def stage(payload):
                    if len(self.streams) == 2:
                        self.assertEqual(payload["input"][-2]["content"], "Готовый раздел.\nНачало следующего.")
                        yield event("response.output_text.delta", delta="Окончание.")
                        yield event("response.completed")
                        return
                    yield event("response.output_item.done", item={"id":"c", "type":"message", "phase":"commentary",
                                "content":[{"type":"output_text", "text":"Служебный черновик"}]})
                    yield event("response.output_item.done", item={"id":"a", "type":"message", "phase":"final_answer",
                                "content":[{"type":"output_text", "text":"Готовый раздел."}]})
                    yield event("response.output_text.delta", item_id="b", delta="Начало следующего.")
                    if ending is not None:
                        raise ending()
                result = self.compose("Напиши разбор", stage)
                self.assertTrue(result.complete)
                self.assertEqual(result.text, "Готовый раздел.\nНачало следующего.\nОкончание.")
                self.assertEqual(len(self.streams), 2)

    def test_interrupted_refusal_does_not_continue_the_prior_text(self):
        def stage(payload):
            yield event("response.output_text.delta", item_id="a", delta="Черновик")
            yield event("response.refusal.delta", item_id="a", delta="Не могу")
            raise self.timeout()
        with self.assertRaises(m.ModelIssue):
            self.compose("Напиши разбор", stage)
        self.assertEqual(len(self.streams), 1)

    def test_silent_reset_and_eof_without_public_text_keep_their_error(self):
        for ending, stage_name in ((self.reset, "stream_read"), (None, "stream_end")):
            with self.subTest(stage=stage_name):
                self.streams.clear()
                def stage(payload):
                    yield event("response.output_item.added", item={"id":"c", "type":"message", "phase":"commentary"})
                    yield event("response.output_text.delta", item_id="c", delta="Служебная заметка")
                    if ending is not None:
                        raise ending()
                with self.assertRaises(m.ModelIssue) as error:
                    self.compose("Вопрос", stage)
                self.assertEqual((error.exception.kind, error.exception.stage), ("connection", stage_name))
                self.assertEqual(len(self.streams), 1)

    def test_confirmed_text_parts_complete_without_deltas_and_are_not_duplicated(self):
        def stage(payload):
            if len(self.streams) == 2:
                self.assertEqual(payload["input"][-2]["content"], "Раздел 1.\nРаздел 2.")
                yield event("response.output_text.delta", delta="Раздел 3.")
                yield event("response.completed")
                return
            yield event("response.output_text.delta", item_id="a", content_index=0, delta="Раздел")
            yield event("response.output_text.done", item_id="a", content_index=0, text="Раздел 1.")
            yield event("response.content_part.done", item_id="a", content_index=1,
                        part={"type":"output_text", "text":"Раздел 2."})
            raise self.reset()
        self.assertEqual(self.compose("Напиши разбор", stage).text, "Раздел 1.\nРаздел 2.\nРаздел 3.")

    def test_incomplete_dispatch_restarts_without_exposing_its_payload(self):
        for short_marker in ("PSY_T", 'PSY_TASK {"public_topic":"служебный черновик'):
            with self.subTest(marker=short_marker):
                self.streams.clear()
                def stage(payload):
                    if len(self.streams) == 1:
                        yield event("response.output_text.delta", delta=short_marker)
                        raise self.timeout()
                    self.assertIn(m.DISPATCH, payload["instructions"])
                    self.assertEqual(payload["input"], [m._user("Напиши разбор")])
                    yield event("response.output_text.delta", delta="Готовый ответ.")
                    yield event("response.completed")
                result = self.compose("Напиши разбор", stage)
                self.assertEqual(result.text, "Готовый ответ.")
                self.assertTrue(result.complete)
                self.assertEqual(len(self.streams), 2)

    def test_repeated_broken_dispatch_never_becomes_a_public_reply(self):
        def stage(payload):
            yield event("response.output_text.delta", delta='PSY_TASK {"minutes":')
            raise self.timeout()
        with self.assertRaises(m.ModelIssue) as error:
            self.compose("Составь лекцию", stage)
        self.assertNotIn("PSY_TASK", error.exception.public())
        self.assertEqual(len(self.streams), 2)
