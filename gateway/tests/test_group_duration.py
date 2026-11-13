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
        result = self.compose("Research question", stage, research=True)
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
